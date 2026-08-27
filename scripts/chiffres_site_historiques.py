#!/usr/bin/env python3
"""Recalcule les chiffres publiés par la page « Résultats — tableaux historiques ».

Même rôle que `chiffres_site.py`, sur l'autre corpus et sur les seules sections que la page
publie : périmètre, métriques, distribution du score, décomposition des cellules attendues.
Le classement des cellules est celui de `chiffres_site.classer`, importé tel quel — la page
et la mesure ne doivent pas pouvoir diverger.

Source des cellules : `website/data/comparaisons-historiques.json`
    (`uv run --project website python website/build_data_historiques.py`)
Source des métriques : `tableaux_historiques/eval/evaluation.parquet`
    (`uv run evaluation_historiques.py`)

Usage :
    uv run chiffres_site_historiques.py
    uv run chiffres_site_historiques.py --sans-s3   # saute les métriques, qui relisent S3
"""

import argparse
import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

# `build_data` vit dans website/ : même chemin d'accès que dans `chiffres_site.py`, dont
# la classification des cellules et la normalisation des nombres sont reprises telles quelles.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "website"))

from build_data import _norm_num  # noqa: E402
from chiffres_site import DECOMPOSITION, classer  # noqa: E402
from corpus_historiques import (  # noqa: E402
    MOTEURS,
    S3_EVAL_OUTPUT,
    key_from_image,
    stem,
)
from evaluation_historiques import _split_rank  # noqa: E402
from extraction_common.s3 import get_s3_fs  # noqa: E402

COMPARAISONS = (
    Path(__file__).resolve().parents[1] / "website" / "data" / "comparaisons-historiques.json"
)

# Bornes des tranches de l'histogramme, en pourcents. Identiques à celles de la page des
# comptes sociaux, pour que les deux distributions se lisent l'une contre l'autre.
TRANCHES = [(i / 10, (i + 1) / 10) for i in range(10)]


def score_table(table: dict, methode: str) -> float | None:
    """Score d'extraction correcte d'un tableau, structure et valeurs agrégées.

    Les trois termes sont pondérés par leur effectif réel : colonnes appariées, lignes
    appariées et cellules numériques récupérées, rapportés au total attendu. Les cellules
    vides de l'annotation sont hors du compte — `expected` les exclut déjà.

    Args:
        table: entrée de `comparaisons-historiques.json`.
        methode: moteur dont on veut le score.

    Returns:
        Le score dans [0, 1], ou None si le moteur n'a rien produit pour ce tableau.
    """
    resultat = table["methods"].get(methode)
    if not resultat:
        return None
    ann = resultat.get("ann") or table["ann"]
    n_lignes = len(ann)
    n_colonnes = max((len(r) for r in ann), default=0)
    numerateur = len(resultat["rowMatch"]) + len(resultat["colMatch"]) + resultat["recovered"]
    denominateur = n_lignes + n_colonnes + resultat["expected"]
    return numerateur / denominateur if denominateur else None


def distribution(scores: list[float]) -> list[int]:
    """Effectifs par tranche de score.

    Args:
        scores: scores des tableaux d'un moteur.

    Returns:
        Un effectif par tranche de `TRANCHES`, la dernière étant fermée à droite.
    """
    effectifs = [0] * len(TRANCHES)
    for score in scores:
        for i, (bas, haut) in enumerate(TRANCHES):
            if bas <= score < haut or (i == len(TRANCHES) - 1 and score == 1.0):
                effectifs[i] += 1
                break
    return effectifs


def afficher_perimetre(data: dict) -> None:
    """Imprime le périmètre mesuré : effectifs de tableaux et de cellules."""
    print("\n" + "=" * 78)
    print("## Périmètre mesuré")
    print("=" * 78)
    print(f"  tableaux comparés               {data['meta']['nTables']}")
    for methode in data["meta"]["methods"]:
        sur_decoupes = [
            t["id"]
            for t in data["tables"]
            if (t["methods"].get(methode) or {}).get("nPredTables", 1) > 1
        ]
        print(
            f"  {methode} — images découpées en >1 tableau : {len(sur_decoupes)}"
            + (f" — {', '.join(sur_decoupes)}" if sur_decoupes else "")
        )
    lignes = sum(t["annRows"] for t in data["tables"])
    colonnes = sum(t["annCols"] for t in data["tables"])
    print(f"  lignes annotées (cumul)         {lignes}")
    print(f"  colonnes annotées (cumul)       {colonnes}")


def afficher_scores(data: dict) -> None:
    """Imprime la distribution du score d'extraction correcte."""
    print("\n" + "=" * 78)
    print("## Distribution du score d'extraction correcte")
    print("=" * 78)
    for methode in data["meta"]["methods"]:
        scores = [s for t in data["tables"] if (s := score_table(t, methode)) is not None]
        if not scores:
            continue
        effectifs = distribution(scores)
        print(f"\n  {methode} — {len(scores)} tableaux")
        print(
            f"    moyenne {statistics.mean(scores):.3f}   médiane {statistics.median(scores):.3f}"
        )
        print(f"    score exactement 100 %          {sum(1 for s in scores if s == 1.0)}")
        print(f"    score sous 5 %                  {sum(1 for s in scores if s < 0.05)}")
        extremes = sum(1 for s in scores if s < 0.2 or s >= 0.8)
        print(f"    tranches extrêmes (<20 % ou ≥80 %)  {extremes} ({extremes / len(scores):.0%})")
        milieu = len(scores) - extremes
        print(f"    milieu (20–80 %)                {milieu} ({milieu / len(scores):.0%})")
        print("\n    | Tranche | effectif |")
        for (bas, _), n in zip(TRANCHES, effectifs, strict=True):
            print(f"    | {bas * 100:.0f}–{bas * 100 + 10:.0f} % | {n} |")
        print(f"    | **Total** | **{sum(effectifs)}** |")


def histogramme_html(data: dict, methode: str) -> str:
    """Compose la figure de distribution, prête à coller dans la page.

    L'histogramme du site est du HTML statique, hauteurs de barres en pourcents : à dix
    tranches et un moteur, le composer à la main serait une source d'écarts entre la page
    et les données. On l'écrit donc ici, avec les classes `dx-hist` déjà stylées.

    Args:
        data: contenu de `comparaisons-historiques.json`.
        methode: moteur à représenter.

    Returns:
        Le bloc `<figure>` complet.
    """
    scores = [s for t in data["tables"] if (s := score_table(t, methode)) is not None]
    effectifs = distribution(scores)
    # L'axe monte à la dizaine supérieure, pour que la barre la plus haute ne touche pas
    # le bord du cadre.
    sommet = max(10, -(-max(effectifs) // 10) * 10)
    # Un pas visant cinq graduations : à dix tableaux dans la tranche la plus fournie,
    # graduer de un couvrirait l'axe de traits sans rien ajouter à la lecture.
    graduations = list(range(0, sommet + 1, max(1, round(sommet / 5))))

    lignes = [
        '<figure class="dx-fig">',
        '  <p class="dx-fig-title">Distribution des tableaux selon leur score '
        "d'extraction correcte</p>",
        '  <p class="dx-fig-sub">Nombre de tableaux par tranche de score. Le score agrège, '
        "sur un même tableau, les colonnes appariées, les lignes appariées et les cellules "
        "numériques récupérées, rapportés au total attendu.</p>",
        '  <div class="dx-legend">',
        f'    <span><span class="dx-swatch" style="background:var(--dx-{methode})"></span>'
        f"{methode} — {len(scores)} tableaux</span>",
        "  </div>",
        '  <div class="dx-hist">',
        '    <div class="dx-hist-yaxis">',
    ]
    for tick in graduations:
        lignes.append(
            f'      <div class="dx-hist-ytick" style="bottom:{100 * tick / sommet:.1f}%">{tick}</div>'
        )
    lignes += ["    </div>", '    <div class="dx-hist-plot">']
    for tick in graduations[1:]:
        lignes.append(
            f'      <div class="dx-hist-grid" style="bottom:{100 * tick / sommet:.1f}%"></div>'
        )
    lignes.append('      <div class="dx-hist-bins">')
    for (bas, _), n in zip(TRANCHES, effectifs, strict=True):
        borne = f"{bas * 100:.0f}–{bas * 100 + 10:.0f} %"
        pluriel = "tableau" if n <= 1 else "tableaux"
        # Étiquette directe sur les tranches extrêmes seulement, comme sur l'autre page.
        etiquette = f' data-label="{n}"' if bas in (0.0, 0.9) else ""
        lignes += [
            '        <div class="dx-hist-bin">',
            f'          <div class="dx-hist-bar" style="height:{100 * n / sommet:.1f}%;'
            f'background:var(--dx-{methode})" tabindex="0" '
            f'data-tip="{methode} — {borne} de score : {n} {pluriel}"{etiquette}></div>',
            "        </div>",
        ]
    lignes += ["      </div>", "    </div>", '    <div class="dx-hist-xaxis">']
    for bas, _ in TRANCHES:
        lignes.append(
            f'      <div class="dx-hist-xtick">{bas * 100:.0f}–{bas * 100 + 10:.0f}</div>'
        )
    lignes += [
        "    </div>",
        '    <div class="dx-hist-axis-title">score d\'extraction correcte, en&nbsp;%</div>',
        "  </div>",
        f'  <figcaption class="dx-fig-note">Moyenne {statistics.mean(scores):.3f}, '
        f"médiane {statistics.median(scores):.3f}. Effectifs complets dans la table "
        "ci-dessous et au survol de chaque barre.</figcaption>",
        "</figure>",
    ]
    return "\n".join(lignes)


def afficher_decomposition(classement: dict) -> None:
    """Imprime la décomposition des cellules attendues, poste par poste."""
    print("\n" + "=" * 78)
    print("## Décomposition des cellules attendues")
    print("=" * 78)
    methodes = list(classement)
    for m in methodes:
        print(f"  {m}: {classement[m]['total']} cellules numériques non vides")

    print(f"\n| Devenir de la cellule | {' | '.join(f'{m} | %' for m in methodes)} | Nature |")
    for poste, nature in DECOMPOSITION:
        cases = []
        for m in methodes:
            n = classement[m]["postes"][poste]
            cases.append(f"{n} | {100 * n / classement[m]['total']:.1f}")
        print(f"| {poste} | {' | '.join(cases)} | {nature} |")
    print(f"| **Total** | {' | '.join(f'{classement[m]["total"]} | 100' for m in methodes)} | |")

    print("\n  Poids par nature :")
    for m in methodes:
        parts: Counter = Counter()
        for poste, nature in DECOMPOSITION:
            parts[nature] += classement[m]["postes"][poste]
        total = classement[m]["total"]
        detail = ", ".join(
            f"{nature} {100 * n / total:.1f} %" for nature, n in parts.items() if nature != "—"
        )
        print(f"    {m}: {detail}")

    print("\n  Nature des erreurs de lecture :")
    for m in methodes:
        erreurs = classement[m]["erreurs"]
        total = sum(erreurs.values())
        detail = ", ".join(f"{nature} {n}" for nature, n in erreurs.most_common()) or "aucune"
        print(f"    {m}: {total} au total — {detail}")

    print("\n  Bien lu, écrit autrement :")
    for m in methodes:
        ecritures = classement[m]["ecritures"]
        n, rec = sum(ecritures.values()), classement[m]["recuperees"]
        detail = ", ".join(f"{nature} {k}" for nature, k in ecritures.most_common()) or "aucun"
        print(f"    {m}: {n} des {rec} cellules récupérées ({100 * n / rec:.1f} %) — {detail}")


def union_par_image(fs, prefixe: str) -> dict[str, set[str]]:
    """Valeurs normalisées présentes dans **tous** les CSV d'une même image.

    Les rangs surnuméraires en font partie : quand le moteur découpe une image en
    plusieurs grilles, une valeur attendue peut n'exister que dans un rang que la
    comparaison ne regarde pas. C'est ce que le poste « lue quelque part » isole.

    Args:
        fs: système de fichiers S3.
        prefixe: dossier des CSV du moteur.

    Returns:
        {clé d'image: ensemble des valeurs normalisées}.
    """
    union: dict[str, set[str]] = defaultdict(set)
    for chemin in fs.glob(f"{prefixe}/*.csv"):
        radical, _ = _split_rank(stem(chemin))
        cle = key_from_image(radical)
        with fs.open(chemin, "r", encoding="utf-8-sig") as f:
            for ligne in csv.reader(f, delimiter=";"):
                union[cle] |= {_norm_num(v) for v in ligne}
    return union


def afficher_entonnoir(data: dict, unions: dict[str, dict[str, set[str]]]) -> None:
    """Imprime l'entonnoir d'attrition : lue quelque part, puis à la bonne cellule.

    Sans lui, le taux de récupération ne dit pas si une valeur manque parce qu'elle a été
    mal lue ou parce qu'elle a été bien lue et mal placée — la distinction qui commande
    l'effort à fournir.
    """
    print("\n" + "=" * 78)
    print("## Entonnoir d'attrition")
    print("=" * 78)
    for methode in data["meta"]["methods"]:
        attendues = lues = bien_placees = 0
        for table in data["tables"]:
            resultat = table["methods"].get(methode)
            if not resultat:
                continue
            ann = resultat.get("ann") or table["ann"]
            valeurs_image = unions.get(methode, {}).get(table["id"], set())
            for position, statut in resultat["status"].items():
                if statut == "vide-attendue":
                    continue
                ligne, col = (int(x) for x in position.split(","))
                attendues += 1
                lues += _norm_num(ann[ligne][col]) in valeurs_image
                bien_placees += statut == "ok"
        print(f"\n  {methode}")
        for libelle, n in [
            ("Attendues (référence)", attendues),
            ("Lues quelque part dans l'image", lues),
            ("À la bonne cellule", bien_placees),
        ]:
            print(f"    {libelle:32} {100 * n / attendues:5.1f} %  · {n}")


def afficher_metriques(ev: pd.DataFrame) -> None:
    """Imprime les métriques du parquet, une ligne par méthode."""
    print("\n" + "=" * 78)
    print("## Métriques publiées")
    print("=" * 78)
    for m in sorted(ev.methode.unique()):
        s = ev[ev.methode == m]
        cellule = s.n_recovered_numeric.sum() / s.n_numeric_cells.sum()
        print(
            f"  {m:10} n={len(s):3} col={s.col_recovery.mean():.3f} "
            f"row={s.row_recovery.mean():.3f} num={s.numeric_recovery.mean():.3f} "
            f"/cellule={cellule:.3f} total={s.total_extraction.mean():.3f} "
            f"médiane_num={s.numeric_recovery.median():.3f}"
        )
        parfaits = int(s.total_extraction.sum())
        print(
            f"  {'':10} tableaux parfaits {parfaits}/{len(s)}, "
            f"un seul tableau prédit pour {(s.n_pred_tables == 1).sum()}/{len(s)} images"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sans-s3", action="store_true", help="saute les métriques, qui relisent S3"
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help="n'imprimer que la figure de distribution, à coller dans resultats-historiques.qmd",
    )
    args = parser.parse_args()

    if not COMPARAISONS.exists():
        raise SystemExit(
            f"{COMPARAISONS} est absent — lancer d'abord `uv run --project website "
            "python website/build_data_historiques.py`."
        )
    data = json.loads(COMPARAISONS.read_text(encoding="utf-8"))

    if args.html:
        for methode in data["meta"]["methods"]:
            print(histogramme_html(data, methode))
        return

    print(f"{data['meta']['nTables']} tableaux, moteurs {data['meta']['methods']}")

    afficher_perimetre(data)
    afficher_scores(data)
    afficher_decomposition(classer(data))

    if not args.sans_s3:
        fs = get_s3_fs()
        unions = {m: union_par_image(fs, cfg["csv"]) for m, cfg in MOTEURS.items()}
        afficher_entonnoir(data, unions)
        with fs.open(S3_EVAL_OUTPUT, "rb") as f:
            afficher_metriques(pd.read_parquet(f))


if __name__ == "__main__":
    main()
