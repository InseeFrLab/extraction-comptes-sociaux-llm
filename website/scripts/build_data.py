#!/usr/bin/env python3
"""Construit le jeu de données du site à partir de S3.

Lit les annotations de référence et les CSV prédits, les convertit en grilles de
cellules annotées (appariement ligne/colonne, statut de chaque cellule) puis écrit
`data/comparaisons.json`, consommé par la page « Comparaison » du site.

Les grilles sont publiées telles quelles — raisons sociales, SIREN, montants. Ces
tableaux proviennent de comptes sociaux déposés et publiés en open data par l'INPI :
le site montre donc exactement ce que le pipeline a lu, sans transformation.

Usage :
    uv run --project website python website/scripts/build_data.py
    uv run --project website python website/scripts/build_data.py --limit 5   # itération rapide
"""

from __future__ import annotations

import argparse
import csv as csv_mod
import io
import json
import re
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

# Les fonctions d'évaluation sont réutilisées telles quelles : le site doit décrire
# le pipeline en place, pas une réimplémentation qui divergerait silencieusement.
# `scripts/` est à la racine du dépôt, deux niveaux au-dessus de ce fichier.
RACINE_SITE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE_SITE.parent / "scripts"))

import evaluation as E  # noqa: E402
from extraction_common.s3 import get_s3_fs  # noqa: E402

import config  # noqa: E402

CONFIG = config.charger("comptes-sociaux")

S3_ANNOTATIONS = CONFIG.annotations
S3_APERCUS = CONFIG.site["apercus"]["prefixe"]
# Le site ne publie que les moteurs marqués `publie` : `scripts/evaluation.py` en mesure
# d'autres — moteurs écartés, conditions d'expérience — qui ne sont pas d'autres modèles.
METHODS: dict[str, str] = CONFIG.publies()
# Les données rendues sont lues par le site depuis sa racine, pas depuis ce dossier.
OUT_PATH = RACINE_SITE / CONFIG.site["donnees"]

# Le même seuil d'appariement que la mesure : le site doit montrer ce qu'elle a compté.
MATCH_THRESHOLD = CONFIG.evaluation["seuil_similarite"]


# ── Chargement ────────────────────────────────────────────────────────────────


def load_csv(fs, path: str) -> pd.DataFrame:
    """Charge un CSV prédit en grille de chaînes, lignes complétées à droite.

    Args:
        fs: système de fichiers S3.
        path: chemin du CSV.

    Returns:
        DataFrame de chaînes, colonnes homogènes.
    """
    with fs.open(path, "r", encoding="utf-8-sig") as f:
        rows = list(csv_mod.reader(f, delimiter=";"))
    if not rows:
        return pd.DataFrame()
    max_cols = max(len(r) for r in rows)
    return pd.DataFrame([r + [""] * (max_cols - len(r)) for r in rows], dtype=str).fillna("")


def load_xlsx(fs, path: str) -> pd.DataFrame:
    """Charge une annotation XLSX, lignes entièrement vides retirées.

    Args:
        fs: système de fichiers S3.
        path: chemin du XLSX.

    Returns:
        DataFrame de chaînes, index réinitialisé.
    """
    with fs.open(path, "rb") as f:
        df = pd.read_excel(io.BytesIO(f.read()), header=None, dtype=str).fillna("")
    mask = df.apply(lambda row: row.str.strip().eq("").all(), axis=1)
    return df[~mask].reset_index(drop=True)


# ── Statut des cellules ───────────────────────────────────────────────────────


# Mêmes unités que `E._UNIT_SUFFIX_RE`, **sans** le `%` : celui-ci porte une information
# d'échelle (facteur 100) et doit être converti, pas supprimé. Le retirer avant la
# conversion faisait comparer `70%` à `0,7` comme deux valeurs distinctes, et classait la
# cellule en erreur de lecture (64 cellules à tort chez marker, 69 chez chandra).
_UNIT_SUFFIX_NO_PCT_RE = re.compile(
    r"(?i)\s*(€|eur|euros?|usd|\$|gbp|£|nok|sek|chf|jpy|¥|kr|pp|bps?)\s*$"
)


def _norm_num(value: str) -> str:
    """Normalise un nombre pour la comparaison (espaces, %, signe, parenthèses).

    Args:
        value: cellule brute.

    Returns:
        Forme canonique comparable, ou la chaîne d'origine si ce n'est pas un nombre.
    """
    s = E._unify_dashes(re.sub(r"[   ]", " ", str(value).strip()))
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1].strip()
    s = _UNIT_SUFFIX_NO_PCT_RE.sub("", s).strip()
    if re.match(r"^[-−]\s*", s):
        neg, s = True, re.sub(r"^[-−]\s*", "", s)
    for _ in range(3):
        s = re.sub(r"(\d)\s+(\d)", r"\1\2", s)
    m = re.fullmatch(r"(\d+(?:[,.]\d+)?)\s*%", s)
    if m:
        return ("-" if neg else "") + f"{float(m.group(1).replace(',', '.')) / 100:.6g}"
    if re.fullmatch(r"\d+(?:[,.]\d+)?", s):
        return ("-" if neg else "") + f"{float(s.replace(',', '.')):.10g}"
    return ("-" if neg else "") + s


def _digits(value: str) -> str:
    return re.sub(r"[^0-9]", "", str(value))


def cell_status(expected: str, got: str | None, elsewhere: bool) -> str:
    """Qualifie une cellule de la zone de données de l'annotation.

    Args:
        expected: valeur attendue (annotation).
        got: valeur prédite à la position appariée, ou None si non appariée.
        elsewhere: la valeur attendue existe-t-elle ailleurs dans la prédiction.

    Returns:
        Un des statuts : `ok`, `format`, `ocr`, `deplacee`, `manquante`,
        `differente`, `non-appariee`, `vide-attendue`.
    """
    if E._is_empty(expected):
        return "vide-attendue"
    if got is None:
        return "non-appariee"
    if _norm_num(expected) == _norm_num(got):
        return "ok"
    if E._is_empty(got):
        return "deplacee" if elsewhere else "manquante"
    da, dg = _digits(expected), _digits(got)
    if da and da == dg:
        return "format"
    if elsewhere:
        return "deplacee"
    if da and dg and E._levenshtein_distance(da, dg) <= 2:
        return "ocr"
    return "differente"


def build_grid(df: pd.DataFrame) -> list[list[str]]:
    """Convertit une grille en listes de chaînes, cellule par cellule.

    Args:
        df: grille brute.

    Returns:
        Grille de chaînes, contenu inchangé.
    """
    return [[str(df.iloc[r, c]) for c in range(len(df.columns))] for r in range(len(df))]


def compare_pair(
    ann: pd.DataFrame, pred: pd.DataFrame, threshold: float = MATCH_THRESHOLD
) -> dict | None:
    """Compare une annotation et une prédiction, cellule par cellule.

    Args:
        ann: grille annotée de référence.
        pred: grille prédite.
        threshold: similarité minimale pour apparier deux en-têtes ; celle du corpus des
            comptes sociaux par défaut, l'autre corpus passant la sienne.

    Returns:
        Dict décrivant les deux grilles, l'appariement et le statut de chaque
        cellule de la zone de données, ou None si l'une des grilles est vide.
    """
    if ann.empty or pred.empty:
        return None

    ann_hrows = E.detect_column_header_height(ann)
    ann_hcols = E.detect_row_header_width(ann)
    pred_hrows = E.detect_column_header_height(pred)
    pred_hcols = E.detect_row_header_width(pred)

    col_match = E._match_headers(
        E._build_header_texts(ann, "col", ann_hrows, ann_hcols),
        E._build_header_texts(pred, "col", pred_hrows, pred_hcols),
        threshold,
    )
    row_match = E._match_headers(
        E._build_header_texts(ann, "row", ann_hrows, ann_hcols),
        E._build_header_texts(pred, "row", pred_hrows, pred_hcols),
        threshold,
    )

    pred_values = {_norm_num(v) for v in pred.values.ravel()}
    statuses: dict[str, str] = {}
    counts: Counter = Counter()
    for r in range(ann_hrows, len(ann)):
        for c in range(ann_hcols, len(ann.columns)):
            val = ann.iloc[r, c]
            if not E._looks_numeric(val):
                continue
            got = None
            if r in row_match and c in col_match:
                pr, pc = row_match[r], col_match[c]
                if pr < len(pred) and pc < len(pred.columns):
                    got = pred.iloc[pr, pc]
            status = cell_status(val, got, _norm_num(val) in pred_values)
            statuses[f"{r},{c}"] = status
            if status != "vide-attendue":
                counts[status] += 1

    expected = sum(counts.values())
    return {
        "ann": build_grid(ann),
        "pred": build_grid(pred),
        "annHeaderRows": ann_hrows,
        "annHeaderCols": ann_hcols,
        "predHeaderRows": pred_hrows,
        "predHeaderCols": pred_hcols,
        # _match_headers renvoie des indices numpy (issus d'argsort) : json ne les
        # sérialise pas, d'où la conversion explicite en int.
        "rowMatch": {str(k): int(v) for k, v in row_match.items()},
        "colMatch": {str(k): int(v) for k, v in col_match.items()},
        "status": statuses,
        "counts": dict(counts),
        "expected": expected,
        "recovered": counts.get("ok", 0),
        "recoveryRate": (counts.get("ok", 0) / expected) if expected else None,
        "headerRowsAgree": ann_hrows == pred_hrows,
    }


# ── Aperçus des documents sources ─────────────────────────────────────────────

APERCUS_RACINE = RACINE_SITE / "data" / "apercus"

# Un identifiant de document peut porter espaces et parenthèses — `_2511_431980275_TAB_F164
# - Tableau des filiales et participations 31122022 (002)`. Le nom S3 les garde, pour rester
# traçable ; le fichier publié par le site est ramené à un jeu de caractères sûr en URL.
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(nom: str) -> str:
    """Ramène un nom de fichier à des caractères sûrs en URL.

    Args:
        nom: nom de fichier d'origine.

    Returns:
        Le même nom, tout caractère hors `[A-Za-z0-9._-]` remplacé par `_`.
    """
    return _SLUG_RE.sub("_", nom)


def telecharger_apercus(fs, prefix: str, dossier: str, cle_de_stem) -> dict[str, list[str]]:
    """Rapatrie les aperçus S3 dans `data/apercus/{dossier}` et les indexe par document.

    Les aperçus sont fabriqués une fois par `apercus.py`, à côté de ce fichier, et déposés
    sur S3 : les reconstruire ici ferait retélécharger plusieurs gigaoctets de sources à
    chaque build.

    Args:
        fs: système de fichiers S3.
        prefix: dossier S3 des aperçus.
        dossier: sous-dossier local, sous `data/apercus/`.
        cle_de_stem: fonction rendant, pour le nom d'un aperçu, le document auquel il
            appartient. Plusieurs aperçus par document sont conservés dans l'ordre du nom,
            qui est celui des pages.

    Returns:
        {document: [chemin relatif utilisable dans la page, …]}. Vide si le dossier S3
        n'existe pas — le site reste publiable sans les aperçus.
    """
    cible = APERCUS_RACINE / dossier
    chemins = sorted(fs.glob(f"{prefix}/*.jpg"))
    if not chemins:
        print(f"  [WARN] aucun aperçu dans s3://{prefix} — lancer `uv run apercus.py`")
        return {}

    cible.mkdir(parents=True, exist_ok=True)
    index: dict[str, list[str]] = {}
    octets = 0
    for chemin in chemins:
        nom = chemin.rsplit("/", 1)[-1]
        with fs.open(chemin, "rb") as f:
            contenu = f.read()
        (cible / _slug(nom)).write_bytes(contenu)
        octets += len(contenu)
        index.setdefault(cle_de_stem(nom.removesuffix(".jpg")), []).append(
            f"data/apercus/{dossier}/{_slug(nom)}"
        )
    print(f"  {len(chemins)} aperçus, {octets / 1e6:.1f} Mo → {cible}")
    return index


# ── Assemblage ────────────────────────────────────────────────────────────────


def build(limit: int | None = None) -> dict:
    """Construit le jeu de données complet du site.

    Args:
        limit: si fourni, ne traite que les N premiers tableaux (itération rapide).

    Returns:
        Dict sérialisable : métadonnées, liste des tableaux, comparaisons par méthode.
    """
    fs = get_s3_fs()
    print("Lecture des annotations…", flush=True)
    ann_paths = {Path(p).stem: p for p in fs.glob(f"{S3_ANNOTATIONS}/*.xlsx")}
    pred_paths = {
        method: {Path(p).stem: p for p in fs.glob(f"{prefix}/*.csv")}
        for method, prefix in METHODS.items()
    }
    print(
        f"  {len(ann_paths)} annotations, "
        + ", ".join(f"{m}: {len(p)}" for m, p in pred_paths.items())
    )

    # Traitement par document, et non fichier par fichier : la granularité de la référence
    # est propre au moteur. Marker voit le PDF entier et rend parfois d'un seul tenant un
    # tableau coupé par un saut de page ; ses annotations sont alors regroupées d'autant, ce
    # que la mesure fait aussi (`E.METHODS_MERGING_PAGE_BREAKS`). Celles de chandra, appelé
    # page par page, ne sont jamais touchées.
    by_base: dict[str, list[str]] = {}
    for name in sorted(ann_paths, key=E._rank):
        by_base.setdefault(E._base_stem(name), []).append(name)

    print("Comparaison…", flush=True)
    tables = []
    for base in sorted(by_base):
        if limit and len(tables) >= limit:
            break
        raw_anns = [load_xlsx(fs, ann_paths[n]) for n in by_base[base]]
        par_methode = {}
        for method in METHODS:
            stems = sorted((s for s in pred_paths[method] if E._base_stem(s) == base), key=E._rank)
            anns = raw_anns
            if method in E.METHODS_MERGING_PAGE_BREAKS:
                anns = E._merge_page_breaks(raw_anns, len(stems))
            par_methode[method] = (anns, stems)

        for i in range(max(len(anns) for anns, _ in par_methode.values())):
            # `{siren}_{rang}` sert d'identifiant : il permet de retrouver le PDF d'origine
            # à partir d'un tableau affiché sur le site.
            entry = {"id": f"{base}_{i + 1}", "methods": {}}
            for method, (anns, stems) in par_methode.items():
                if i >= len(anns) or i >= len(stems):
                    continue
                result = compare_pair(anns[i], load_csv(fs, pred_paths[method][stems[i]]))
                if result is not None:
                    entry["methods"][method] = result
            if entry["methods"]:
                # L'annotation est le plus souvent la même d'une méthode à l'autre : on la
                # sort du bloc par méthode pour ne pas la stocker deux fois. Elle ne diffère
                # que si la référence d'un moteur a été regroupée et l'autre non ; le bloc de
                # la méthode garde alors la sienne.
                first = next(iter(entry["methods"].values()))
                entry["ann"] = first.pop("ann")
                entry["annHeaderRows"] = first["annHeaderRows"]
                entry["annHeaderCols"] = first["annHeaderCols"]
                for result in entry["methods"].values():
                    if result.get("ann") == entry["ann"]:
                        result.pop("ann")
                entry["annRows"] = len(entry["ann"])
                entry["annCols"] = len(entry["ann"][0]) if entry["ann"] else 0
                tables.append(entry)
            print(
                f"  {entry['id']:<20} "
                + " ".join(
                    f"{m}={r['recoveryRate']:.0%}"
                    if r.get("recoveryRate") is not None
                    else f"{m}=n/a"
                    for m, r in entry["methods"].items()
                ),
                flush=True,
            )

    print("Aperçus des documents…", flush=True)
    # `{siren}_p{page}.jpg` : le rang après `_p` est le numéro de page, et le tri par nom
    # suffit tant qu'un document ne dépasse pas neuf pages — le corpus plafonne à trois.
    apercus = telecharger_apercus(
        fs, S3_APERCUS, "comptes-sociaux", lambda stem: stem.rsplit("_p", 1)[0]
    )
    for entry in tables:
        images = apercus.get(E._base_stem(entry["id"]), [])
        if images:
            entry["apercus"] = images

    payload = {
        "meta": {
            "source": f"s3://{CONFIG.site['racine']}",
            "methods": list(METHODS),
            "nTables": len(tables),
            "matchThreshold": MATCH_THRESHOLD,
            "note": (
                "Grilles publiées telles quelles : raisons sociales, SIREN et montants "
                "sont ceux extraits, non modifiés. Source : comptes sociaux déposés, "
                "publiés en open data par l'INPI."
            ),
        },
        "tables": tables,
    }
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="ne traiter que N tableaux")
    parser.add_argument("--out", type=Path, default=OUT_PATH, help="chemin de sortie JSON")
    args = parser.parse_args()

    payload = build(limit=args.limit)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    size_kb = args.out.stat().st_size / 1024
    print(f"\n{args.out} — {payload['meta']['nTables']} tableaux, {size_kb:.0f} Ko")


if __name__ == "__main__":
    main()
