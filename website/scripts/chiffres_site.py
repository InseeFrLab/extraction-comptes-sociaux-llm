#!/usr/bin/env python3
"""Recalcule les chiffres publiés par les pages « Résultats » du site, pour les deux corpus.

Les pages du site sont rédigées à la main : leurs tableaux et leurs graphiques portent des
valeurs en dur. Ce script les recalcule toutes depuis les données courantes, pour qu'une
régénération du pipeline ne laisse pas une page décrire un état révolu.

Source des cellules : le JSON de comparaison du corpus, produit par `build_data*.py`. Il
porte les grilles annotées **et** prédites, l'appariement des lignes et des colonnes, et le
statut de chaque cellule — donc tout ce qu'il faut pour reclasser sans relire S3. Deux
mesures y échappent et lisent S3 : la présence d'une valeur ailleurs dans les CSV du même
document, et les métriques du parquet.

    comptes-sociaux  data/comparaisons.json             page « Résultats »
    historiques      data/comparaisons-historiques.json page « Résultats — tableaux
                                                        historiques »

Le classement des cellules est **commun aux deux corpus** : il reprend exactement celui de
`cell_status` (`build_data.py`), en le poussant d'un cran — `non-appariee` se scinde selon
ce qui manque (la ligne, la colonne ou les deux) et `deplacee` selon où la valeur a atterri.
C'est cette granularité que publie la section « Décomposition des cellules attendues ». Ne
diffèrent que les sections publiées, parce que les deux pages ne publient pas les mêmes.

Usage (depuis la racine du dépôt, `C =` abrège la commande) :
    C="uv run --project website python website/scripts/chiffres_site.py"
    $C --corpus comptes-sociaux
    $C --corpus historiques
    $C --corpus comptes-sociaux --sans-s3   # saute entonnoir et métriques, qui relisent S3
    $C --corpus historiques --html          # la figure de distribution, à coller dans la page
"""

import argparse
import csv
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

# `evaluation` et les conventions du corpus historique vivent dans `scripts/`, à la racine
# du dépôt ; `build_data` est à côté de ce fichier.
RACINE_SITE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE_SITE.parent / "scripts"))

import build_data_historiques as BDH  # noqa: E402
import corpus_historiques as CH  # noqa: E402
import evaluation as E  # noqa: E402
from build_data import METHODS as METHODS_COMPTES  # noqa: E402
from build_data import _digits, _norm_num  # noqa: E402
from extraction_common.s3 import get_s3_fs  # noqa: E402

import config  # noqa: E402

# Ordre de publication de la décomposition, et rattachement de chaque poste à sa nature.
# « Structure » désigne un échec de placement, « Lecture » un échec de transcription : c'est
# l'opposition que les pages mettent en avant, et elle ne se lit pas dans les statuts bruts.
DECOMPOSITION = [
    ("Récupérée", "—"),
    ("Ligne *et* colonne non appariées", "Structure"),
    ("Colonne non appariée", "Structure"),
    ("Ligne non appariée", "Structure"),
    ("Bonne ligne, colonne décalée", "Structure"),
    ("Cellule vide, valeur présente ailleurs", "Structure"),
    ("Autre décalage", "Structure"),
    ("Cellule vide, valeur nulle part", "Lecture"),
    ("Format seul", "Normalisation"),
    ("Erreur de lecture de chiffres", "Lecture"),
    ("Texte à la place du nombre", "Lecture"),
    ("Valeur franchement différente", "Lecture"),
]

# Bornes des tranches de l'histogramme de score, en pourcents.
TRANCHES = [(i / 10, (i + 1) / 10) for i in range(10)]


# ── Classement des cellules, commun aux deux corpus ───────────────────────────


def _cellule_predite(pred: list[list[str]], rmatch: dict, cmatch: dict, ligne: int, col: int):
    """Valeur prédite à la position appariée d'une cellule annotée.

    Args:
        pred: grille prédite.
        rmatch: appariement des lignes, annotation → prédiction.
        cmatch: appariement des colonnes.
        ligne: indice de ligne dans l'annotation.
        col: indice de colonne dans l'annotation.

    Returns:
        Le couple (valeur prédite, position appariée), ou (None, None) si la ligne ou la
        colonne n'est pas appariée, ou si la position tombe hors de la grille.
    """
    pr, pc = rmatch.get(str(ligne)), cmatch.get(str(col))
    if pr is None or pc is None:
        return None, None
    if pr >= len(pred) or pc >= len(pred[pr]):
        return None, None
    return pred[pr][pc], (pr, pc)


def _poste(statut: str, attendu: str, obtenu, position, pred, rmatch, cmatch, ligne, col) -> str:
    """Poste de la décomposition auquel une cellule appartient.

    Args:
        statut: statut rendu par `cell_status`.
        attendu: valeur annotée.
        obtenu: valeur prédite à la position appariée, ou None.
        position: position appariée, ou None.
        pred: grille prédite.
        rmatch, cmatch: appariements.
        ligne, col: position dans l'annotation.

    Returns:
        Le libellé du poste, tel que la page le publie.
    """
    if statut == "ok":
        return "Récupérée"
    if statut == "non-appariee":
        ligne_ok = str(ligne) in rmatch
        col_ok = str(col) in cmatch
        if not ligne_ok and not col_ok:
            return "Ligne *et* colonne non appariées"
        return "Colonne non appariée" if ligne_ok else "Ligne non appariée"
    if statut == "manquante":
        return "Cellule vide, valeur nulle part"
    if statut == "format":
        return "Format seul"
    if statut == "ocr":
        return "Erreur de lecture de chiffres"
    if statut == "differente":
        return (
            "Texte à la place du nombre" if not _digits(obtenu) else "Valeur franchement différente"
        )
    if statut == "deplacee":
        if obtenu is not None and not obtenu.strip():
            return "Cellule vide, valeur présente ailleurs"
        # La valeur est-elle sur la même ligne prédite, à une autre colonne ?
        cible = _norm_num(attendu)
        if position is not None:
            pr = position[0]
            if any(_norm_num(v) == cible for v in pred[pr]):
                return "Bonne ligne, colonne décalée"
        return "Autre décalage"
    return "Autre décalage"


def _nature_erreur(attendu: str, obtenu: str) -> str:
    """Nature d'une erreur de lecture de chiffres.

    Args:
        attendu: valeur annotée.
        obtenu: valeur prédite.

    Returns:
        Un des quatre libellés publiés par la section « Nature des erreurs ».
    """
    a, b = _digits(attendu), _digits(obtenu)
    if len(a) != len(b):
        court, long_ = sorted((a, b), key=len)
        if long_.startswith(court):
            return "Troncature en fin de cellule"
        return "Autre écart de un ou deux chiffres"
    if sorted(a) == sorted(b) and a != b:
        return "Chiffres permutés"
    if sum(1 for x, y in zip(a, b, strict=True) if x != y) == 1:
        return "Un chiffre substitué"
    return "Autre écart de un ou deux chiffres"


def _nature_ecriture(attendu: str, obtenu: str) -> str | None:
    """Nature d'un écart de convention entre deux valeurs pourtant identiques.

    Args:
        attendu: valeur annotée.
        obtenu: valeur prédite, égale après normalisation.

    Returns:
        Le libellé de l'écart, ou None si les deux chaînes sont déjà identiques.
    """
    if attendu.strip() == obtenu.strip():
        return None
    sans_espace = (re.sub(r"[\s  ]", "", attendu), re.sub(r"[\s  ]", "", obtenu))
    if sans_espace[0] == sans_espace[1]:
        return "Séparateur de milliers seul"
    if sans_espace[0].replace(",", ".") == sans_espace[1].replace(",", "."):
        return "Virgule contre point décimal"
    return "Autre convention"


def classer(data: dict) -> dict:
    """Reclasse toutes les cellules d'un JSON de comparaison.

    Args:
        data: contenu du JSON de comparaison, l'un ou l'autre corpus.

    Returns:
        {methode: {"postes": Counter, "erreurs": Counter, "ecritures": Counter,
                   "total": int, "recuperees": int, "attendues_4": [(base, valeur, ok)]}}
    """
    out = {}
    for methode in data["meta"]["methods"]:
        postes: Counter = Counter()
        erreurs: Counter = Counter()
        ecritures: Counter = Counter()
        attendues_4: list[tuple[str, str, bool, bool]] = []
        for table in data["tables"]:
            resultat = table["methods"].get(methode)
            if not resultat:
                continue
            ann = resultat.get("ann") or table["ann"]
            pred = resultat["pred"]
            rmatch, cmatch = resultat["rowMatch"], resultat["colMatch"]
            valeurs_pred = {_norm_num(v) for ligne in pred for v in ligne}
            for position, statut in resultat["status"].items():
                if statut == "vide-attendue":
                    continue
                ligne, col = (int(x) for x in position.split(","))
                attendu = ann[ligne][col]
                obtenu, pos = _cellule_predite(pred, rmatch, cmatch, ligne, col)
                postes[_poste(statut, attendu, obtenu, pos, pred, rmatch, cmatch, ligne, col)] += 1
                if statut == "ocr":
                    erreurs[_nature_erreur(attendu, obtenu)] += 1
                if statut == "ok":
                    nature = _nature_ecriture(attendu, obtenu)
                    if nature:
                        ecritures[nature] += 1
                if len(_digits(attendu)) >= 4:
                    attendues_4.append(
                        (
                            E._base_stem(table["id"]),
                            attendu,
                            _norm_num(attendu) in valeurs_pred,
                            statut == "ok",
                        )
                    )
        out[methode] = {
            "postes": postes,
            "erreurs": erreurs,
            "ecritures": ecritures,
            "total": sum(postes.values()),
            "recuperees": postes["Récupérée"],
            "attendues_4": attendues_4,
        }
    return out


# ── Valeurs lues ailleurs : union des CSV d'un même document ───────────────────


def union_par_document(fs, prefixe: str, cle_csv) -> dict[str, set[str]]:
    """Valeurs normalisées présentes dans **tous** les CSV d'un même document.

    Les CSV surnuméraires — ceux qui n'ont pas d'annotation en face, ou les rangs que la
    comparaison ne regarde pas — en font partie : une valeur attendue peut n'exister que
    là, et c'est ce que le poste « lue quelque part » isole.

    Args:
        fs: système de fichiers S3.
        prefixe: dossier des CSV d'un moteur.
        cle_csv: du chemin d'un CSV à la clé du document dont il provient.

    Returns:
        {clé de document: ensemble des valeurs normalisées}.
    """
    union: dict[str, set[str]] = defaultdict(set)
    for chemin in fs.glob(f"{prefixe}/*.csv"):
        cle = cle_csv(chemin)
        with fs.open(chemin, "r", encoding="utf-8-sig") as f:
            for ligne in csv.reader(f, delimiter=";"):
                union[cle] |= {_norm_num(v) for v in ligne}
    return union


def _cle_csv_comptes(chemin: str) -> str:
    """Le SIREN dont provient un CSV de comptes sociaux (`{siren}_{rang}.csv`)."""
    return E._base_stem(chemin.rsplit("/", 1)[-1].removesuffix(".csv"))


def _cle_csv_historiques(chemin: str) -> str:
    """La clé d'image dont provient un CSV historique (`crop_{clé}_{rang}.csv`)."""
    radical, _ = E._split_rank(CH.stem(chemin))
    return CH.key_from_image(radical)


# ── Sections communes aux deux pages ──────────────────────────────────────────


def afficher_decomposition(classement: dict, libelle: str, *, avec_details: bool) -> None:
    """Imprime la décomposition des cellules attendues, poste par poste.

    Args:
        classement: sortie de `classer`.
        libelle: nature des cellules comptées, telle que la page la nomme.
        avec_details: ajouter les deux sous-blocs « nature des erreurs » et « bien lu,
            écrit autrement ». La page des comptes sociaux leur consacre des sections
            entières ; celle des tableaux historiques les résume ici.
    """
    print("\n" + "=" * 78)
    print("## Décomposition des cellules attendues")
    print("=" * 78)
    methodes = list(classement)
    for m in methodes:
        print(f"  {m}: {classement[m]['total']} {libelle}")

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

    if not avec_details:
        return

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


def afficher_metriques(ev: pd.DataFrame, largeur: int, *, avec_comptage: bool) -> None:
    """Imprime les métriques du parquet, une ligne par méthode.

    Args:
        ev: parquet des métriques, lu tel quel.
        largeur: largeur de la colonne des noms de méthode.
        avec_comptage: ajouter le compte des tableaux parfaits et des images rendues en une
            seule grille — une mesure qui n'a de sens qu'à un tableau par document.
    """
    print("\n" + "=" * 78)
    print("## Métriques publiées")
    print("=" * 78)
    for m in sorted(ev.methode.unique()):
        s = ev[ev.methode == m]
        cellule = s.n_recovered_numeric.sum() / s.n_numeric_cells.sum()
        print(
            f"  {m:{largeur}} n={len(s):3} col={s.col_recovery.mean():.3f} "
            f"row={s.row_recovery.mean():.3f} num={s.numeric_recovery.mean():.3f} "
            f"/cellule={cellule:.3f} total={s.total_extraction.mean():.3f} "
            f"médiane_num={s.numeric_recovery.median():.3f}"
        )
        if avec_comptage:
            parfaits = int(s.total_extraction.sum())
            print(
                f"  {'':{largeur}} tableaux parfaits {parfaits}/{len(s)}, "
                f"un seul tableau prédit pour {(s.n_pred_tables == 1).sum()}/{len(s)} images"
            )


# ── Sections propres à la page « Résultats » (comptes sociaux) ─────────────────


def afficher_erreurs(classement: dict) -> None:
    """Imprime la nature des erreurs de transcription, dans la forme de la page."""
    methodes = list(classement)
    print("\n" + "=" * 78)
    print("## Erreurs de transcription — nature des erreurs")
    print("=" * 78)
    natures = [
        "Un chiffre substitué",
        "Autre écart de un ou deux chiffres",
        "Troncature en fin de cellule",
        "Chiffres permutés",
    ]
    print(f"| Nature | {' | '.join(methodes)} |")
    for nature in natures:
        print(
            f"| {nature} | {' | '.join(str(classement[m]['erreurs'][nature]) for m in methodes)} |"
        )
    print(
        f"| **Total** | {' | '.join(str(sum(classement[m]['erreurs'].values())) for m in methodes)} |"
    )


def afficher_ecritures(classement: dict) -> None:
    """Imprime la section « Bien lu, écrit autrement »."""
    methodes = list(classement)
    print("\n" + "=" * 78)
    print("## Bien lu, écrit autrement")
    print("=" * 78)
    for m in methodes:
        c = classement[m]["ecritures"]
        n, rec = sum(c.values()), classement[m]["recuperees"]
        print(f"  {m}: {n} des {rec} cellules récupérées ({100 * n / rec:.1f} %)")
    print(f"\n| Écart | {' | '.join(methodes)} |")
    for nature in [
        "Séparateur de milliers seul",
        "Virgule contre point décimal",
        "Autre convention",
    ]:
        print(
            f"| {nature} | {' | '.join(str(classement[m]['ecritures'][nature]) for m in methodes)} |"
        )


def afficher_entonnoir_siren(classement: dict, unions: dict) -> None:
    """Imprime l'entonnoir d'attrition des valeurs de quatre chiffres et plus.

    Quatre niveaux, parce qu'un SIREN porte plusieurs tableaux : une valeur peut être lue
    dans un autre CSV du même SIREN avant même la question du bon CSV, puis de la bonne
    cellule.
    """
    print("\n" + "=" * 78)
    print("## Entonnoir d'attrition (valeurs de 4 chiffres et plus)")
    print("=" * 78)
    for m in classement:
        valeurs = classement[m]["attendues_4"]
        total = len(valeurs)
        siren = sum(1 for base, v, _, _ in valeurs if _norm_num(v) in unions[m][base])
        bon_csv = sum(1 for _, _, dans_csv, _ in valeurs if dans_csv)
        cellule = sum(1 for _, _, _, ok in valeurs if ok)
        print(f"\n  {m}")
        for libelle, n in [
            ("Attendues (référence)", total),
            ("Lues quelque part dans le SIREN", siren),
            ("Dans le bon CSV", bon_csv),
            ("À la bonne cellule", cellule),
        ]:
            print(f"    {libelle:34} {100 * n / total:5.1f} %  · {n}")


def afficher_comptes_sociaux(data: dict, unions: dict | None, ev: pd.DataFrame | None) -> None:
    """Imprime les chiffres de la page « Résultats », section par section, dans son ordre."""
    classement = classer(data)
    afficher_erreurs(classement)
    afficher_decomposition(classement, "cellules non vides", avec_details=False)
    afficher_ecritures(classement)
    if unions is not None:
        afficher_entonnoir_siren(classement, unions)
    if ev is not None:
        afficher_metriques(ev, 18, avec_comptage=False)


# ── Sections propres à la page « Résultats — tableaux historiques » ────────────


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


def afficher_entonnoir_image(data: dict, unions: dict[str, dict[str, set[str]]]) -> None:
    """Imprime l'entonnoir d'attrition : lue quelque part, puis à la bonne cellule.

    Sans lui, le taux de récupération ne dit pas si une valeur manque parce qu'elle a été
    mal lue ou parce qu'elle a été bien lue et mal placée — la distinction qui commande
    l'effort à fournir. Trois niveaux seulement : une image porte un tableau, il n'y a donc
    pas de « bon CSV » à distinguer.
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


def afficher_historiques(data: dict, unions: dict | None, ev: pd.DataFrame | None) -> None:
    """Imprime les chiffres de la page « Résultats — tableaux historiques », dans son ordre."""
    afficher_perimetre(data)
    afficher_scores(data)
    afficher_decomposition(classer(data), "cellules numériques non vides", avec_details=True)
    if unions is not None:
        afficher_entonnoir_image(data, unions)
    if ev is not None:
        afficher_metriques(ev, 10, avec_comptage=True)


# ── Corpus : ce que chaque page publie, et d'où ça vient ──────────────────────
#
# `csv` reprend la liste de moteurs du script qui construit les données de la page, et ne la
# redéclare pas : les chiffres doivent porter sur exactement ce que la page montre. Le site
# publie deux modèles — marker et chandra — là où `scripts/evaluation.py` mesure en plus les
# moteurs écartés (opendataloader) et les conditions d'expérience (variantes de prompt, de
# plafond de sortie), qui ne sont pas d'autres modèles.


def _entree(nom: str, csv: dict[str, str], cle_csv, afficher) -> dict:
    """Assemble ce qu'il faut pour recalculer les chiffres d'un corpus.

    Args:
        nom: nom du corpus, celui de son fichier dans `config/`.
        csv: moteurs publiés par la page, et le préfixe S3 de leurs CSV.
        cle_csv: du chemin d'un CSV à la clé du document dont il vient.
        afficher: fonction qui imprime les sections de la page.

    Returns:
        L'entrée de `CORPUS`, chemins et sorties tirés de la configuration du corpus.
    """
    cfg = config.charger(nom)
    return {
        "comparaisons": RACINE_SITE / cfg.site["donnees"],
        "build": cfg.site["build"],
        "csv": csv,
        "cle_csv": cle_csv,
        "eval_output": cfg.evaluation["sortie"],
        "afficher": afficher,
    }


CORPUS: dict[str, dict] = {
    "comptes-sociaux": _entree(
        "comptes-sociaux", METHODS_COMPTES, _cle_csv_comptes, afficher_comptes_sociaux
    ),
    "historiques": _entree("historiques", BDH.METHODS, _cle_csv_historiques, afficher_historiques),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        choices=list(CORPUS),
        default="comptes-sociaux",
        help="corpus dont on recalcule les chiffres (défaut : comptes-sociaux)",
    )
    parser.add_argument(
        "--sans-s3",
        action="store_true",
        help="saute l'entonnoir et les métriques, qui relisent S3",
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help="n'imprimer que la figure de distribution (corpus historiques)",
    )
    args = parser.parse_args()
    conf = CORPUS[args.corpus]

    chemin = conf["comparaisons"]
    if not chemin.exists():
        raise SystemExit(
            f"{chemin} est absent — lancer d'abord "
            f"`uv run --project website python {conf['build']}`."
        )
    data = json.loads(chemin.read_text(encoding="utf-8"))

    if args.html:
        if args.corpus != "historiques":
            raise SystemExit("--html n'existe que pour le corpus historiques.")
        for methode in data["meta"]["methods"]:
            print(histogramme_html(data, methode))
        return

    print(f"{data['meta']['nTables']} tableaux, moteurs {data['meta']['methods']}")

    unions = ev = None
    if not args.sans_s3:
        fs = get_s3_fs()
        unions = {
            m: union_par_document(fs, prefixe, conf["cle_csv"])
            for m, prefixe in conf["csv"].items()
        }
        with fs.open(conf["eval_output"], "rb") as f:
            ev = pd.read_parquet(f)

    conf["afficher"](data, unions, ev)


if __name__ == "__main__":
    main()
