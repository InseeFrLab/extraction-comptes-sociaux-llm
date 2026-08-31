#!/usr/bin/env python3
"""
Évaluation de l'extraction de tableaux, commune aux deux corpus.

Compare les tableaux prédits (CSV) aux annotations de référence et dépose les métriques en
parquet sur S3. Le calcul est **le même pour tous les corpus** — mêmes fonctions, mêmes
seuils : seules changent la référence et la façon de l'apparier à une prédiction.

Chemins, moteurs mesurés et seuils viennent de `config/{corpus}.yaml` ; ce module n'ajoute
que le code d'appariement propre à chaque corpus, déclaré dans `TRAITEMENTS`.

| Corpus            | Référence                            | Appariement            |
|-------------------|--------------------------------------|------------------------|
| `comptes-sociaux` | XLSX, `annotations/clean/`           | par SIREN, rang à rang |
| `historiques`     | HTML, `annotations/tableaux histo…/` | par nom de fichier     |

Métriques (type rappel, une ligne par couple fichier × méthode) : `col_recovery`,
`row_recovery`, `numeric_recovery`, `total_extraction`, `table_count_accuracy`.

**Leur définition, la normalisation appliquée aux valeurs, les seuils et les mesures qui
les fondent sont dans [README.md](README.md)**, section « Étape 3 ».

Usage :
    uv run evaluation.py --corpus comptes-sociaux
    uv run evaluation.py --corpus historiques
    uv run evaluation.py --corpus all [--threshold 0.5 --cell-delta 0]
"""

import argparse
import io
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import corpus_historiques as CH
import numpy as np
import pandas as pd
from extraction_common.s3 import get_s3_fs
from json_to_csv import _parse_html_tables

import config
from config import CorpusConfig, Moteur

_SIREN_RE = re.compile(r"\d{9}")

# Les annotations des comptes sociaux, exposées ici pour `legacy/geometrie_marker.py`, qui
# les relit. La mesure, elle, passe par la configuration du corpus qu'elle traite.
S3_ANNOTATIONS = config.charger("comptes-sociaux").annotations


# ── Chargement S3 ─────────────────────────────────────────────────────────────


def _load_correspondances(fs, chemin: str) -> dict[str, list[str]]:
    """
    Charge le parquet de correspondances.
    Retourne {pure_siren: [xlsx_path_1, ...]} (chemins s3fs sans schéma s3://).
    Le SIREN pur (9 chiffres) est extrait du stem PDF (colonne 'siren').
    """
    with fs.open(chemin, "rb") as f:
        df = pd.read_parquet(f)
    mapping: dict[str, list[str]] = {}
    for _, row in df.iterrows():
        m = _SIREN_RE.search(str(row["siren"]))
        if not m:
            continue
        try:
            xlsx_paths = sorted(p.removeprefix("s3://") for p in row["xlsx"])
        except TypeError:
            continue
        if xlsx_paths:
            mapping[m.group()] = xlsx_paths
    return mapping


def _load_csv(fs, path: str) -> pd.DataFrame:
    """Charge un CSV prédit en DataFrame de chaînes.

    La complétion à droite n'est qu'un filet : les extracteurs de `json_to_csv.py` rendent
    des grilles déjà rectangulaires (cf. README).

    Args:
        fs: système de fichiers S3.
        path: chemin du CSV.

    Returns:
        DataFrame de chaînes, colonnes homogènes.
    """
    import csv as _csv

    with fs.open(path, "r", encoding="utf-8-sig") as f:
        rows = list(_csv.reader(f, delimiter=";"))
    if not rows:
        return pd.DataFrame()
    max_cols = max(len(r) for r in rows)
    padded = [r + [""] * (max_cols - len(r)) for r in rows]
    return pd.DataFrame(padded, dtype=str).fillna("")


def _load_xlsx(fs, path: str) -> pd.DataFrame:
    with fs.open(path, "rb") as f:
        df = pd.read_excel(io.BytesIO(f.read()), header=None, dtype=str).fillna("")
    mask = df.apply(lambda row: row.str.strip().eq("").all(), axis=1)
    return df[~mask].reset_index(drop=True)


def load_html(fs, path: str) -> pd.DataFrame:
    """Charge une annotation HTML en grille de chaînes.

    La référence du corpus historique est lue par le parseur de `json_to_csv.py`, celui-là
    même qui lit la sortie des moteurs : mêmes conventions de fusion (cf. README).

    Args:
        fs: système de fichiers S3.
        path: chemin du fichier HTML.

    Returns:
        DataFrame de chaînes, lignes entièrement vides retirées et index réinitialisé —
        même forme que `_load_xlsx` pour le corpus des comptes sociaux.

    Raises:
        ValueError: si le fichier ne contient aucun `<table>`.
    """
    with fs.open(path, "r", encoding="utf-8") as f:
        html = f.read()
    tables = _parse_html_tables(html)
    if not tables:
        raise ValueError("aucun <table> dans l'annotation")
    if len(tables) > 1:
        # Une annotation porte un tableau : plusieurs signalent une référence à revoir.
        print(
            f"    [WARN] {CH.stem(path)} : {len(tables)} <table> dans l'annotation, le 1er est pris"
        )
    df = pd.DataFrame(tables[0], dtype=str).fillna("")
    mask = df.apply(lambda row: row.str.strip().eq("").all(), axis=1)
    return df[~mask].reset_index(drop=True)


_BASE_STEM_RE = re.compile(r"^(.*?)_(\d+)$")


def _base_stem(name: str) -> str:
    """Retire le suffixe _N d'un nom de fichier pour obtenir l'identifiant SIREN."""
    m = _BASE_STEM_RE.match(name)
    return m.group(1) if m else name


def _rank(stem: str) -> int:
    """Rang `{siren}_{n}` d'un nom de fichier, 0 s'il n'en porte pas.

    Le tri lexicographique placerait `_10` avant `_2`, et l'appariement se fait par rang.
    """
    m = _BASE_STEM_RE.match(stem)
    return int(m.group(2)) if m else 0


# ── Coupures de page : granularité de la référence, propre au moteur ───────────
#
# Un tableau à cheval sur deux pages est annoté une page par fichier, et un moteur qui voit
# le PDF entier le rend parfois d'un seul tenant. Les annotations sont alors regroupées pour
# sa seule référence, et seulement s'il a produit moins de tableaux. Le drapeau se pose
# moteur par moteur, dans `config/{corpus}.yaml` ; les mesures sont dans le README.
METHODS_MERGING_PAGE_BREAKS = frozenset(
    m.nom for cfg in config.tous().values() for m in cfg.moteurs.values() if m.fusion_coupures_page
)


def _same_row(left: pd.Series, right: pd.Series) -> bool:
    """Deux lignes portent-elles exactement le même texte, à la graphie près ?"""

    def clef(serie):
        return [_unify_dashes(re.sub(r"\s+", " ", str(c)).strip().casefold()) for c in serie]

    return clef(left) == clef(right)


def _is_label_row(row: pd.Series) -> bool:
    """La ligne ne porte-t-elle qu'une seule cellule non vide ?

    C'est la signature d'un intertitre de section — « 2. Participations (détenues à moins
    de 50 %) » — et non celle d'un en-tête de colonnes.
    """
    return sum(1 for c in row if str(c).strip()) == 1


def _has_column_header(df: pd.DataFrame) -> bool:
    """Le tableau porte-t-il un en-tête de colonnes, intertitres mis à part ?

    `detect_column_header_height` compte comme en-tête toute ligne majoritairement non
    numérique, intertitre compris : on écarte d'abord ces lignes-là (cf. README).

    Args:
        df: tableau annoté.

    Returns:
        True si un en-tête de colonnes subsiste après avoir ignoré les intertitres de tête.
    """
    i = 0
    while i < len(df) and _is_label_row(df.iloc[i]):
        i += 1
    if i >= len(df):
        return False
    return detect_column_header_height(df.iloc[i:].reset_index(drop=True)) > 0


def _page_break_offset(current: pd.DataFrame, nxt: pd.DataFrame) -> int | None:
    """`nxt` est-il la suite de `current` après une coupure de page ?

    Deux formes, et deux seulement, à largeur identique : `nxt` n'a pas d'en-tête de
    colonnes, ou il réimprime exactement le même. Cf. README.

    Args:
        current: tableau annoté précédent.
        nxt: tableau annoté candidat à la suite.

    Returns:
        Le nombre de lignes de tête à écarter de `nxt` avant concaténation, ou None si les
        deux sont deux tableaux distincts.
    """
    if current.empty or nxt.empty or len(current.columns) != len(nxt.columns):
        return None
    if not _has_column_header(nxt):
        return 0
    if not _same_row(current.iloc[0], nxt.iloc[0]):
        return None
    # L'en-tête réimprimé peut tenir sur plusieurs lignes, et les deux fichiers n'en
    # détectent pas toujours la même hauteur : on écarte le plus long préfixe commun, ce
    # qui s'ajuste sans dépendre de l'heuristique de détection.
    offset = 0
    while (
        offset < len(nxt)
        and offset < len(current)
        and _same_row(current.iloc[offset], nxt.iloc[offset])
    ):
        offset += 1
    return offset


def _merge_page_breaks(anns: list[pd.DataFrame], target: int) -> list[pd.DataFrame]:
    """Regroupe les annotations qu'une coupure de page a séparées, jusqu'à `target`.

    Args:
        anns: annotations d'un SIREN, dans l'ordre des rangs.
        target: nombre de tableaux produits par le moteur pour ce SIREN.

    Returns:
        Les annotations après regroupement. Inchangées si le moteur n'en produit pas moins,
        ou si aucune coupure n'est reconnue : deux tableaux réellement distincts ne sont
        jamais fusionnés pour faire tomber le compte (cf. README).
    """
    if target >= len(anns):
        return anns
    merged = list(anns)
    i = 0
    while len(merged) > target and i < len(merged) - 1:
        offset = _page_break_offset(merged[i], merged[i + 1])
        if offset is None:
            i += 1
            continue
        suite = merged[i + 1].iloc[offset:]
        suite.columns = merged[i].columns
        merged[i] = pd.concat([merged[i], suite], ignore_index=True)
        del merged[i + 1]
    return merged


# ── Appariement : corpus des comptes sociaux ───────────────────────────────────


def _list_pairs(
    fs, annotations: str, pred_prefix: str, merge_page_breaks: bool = False
) -> tuple[list[tuple[str, pd.DataFrame, str]], Counter]:
    """Apparie annotations (.xlsx) et prédictions (.csv), rang à rang au sein d'un SIREN.

    Args:
        fs: système de fichiers S3.
        annotations: dossier des annotations XLSX de référence.
        pred_prefix: dossier des CSV prédits.
        merge_page_breaks: regrouper les annotations qu'une coupure de page a séparées,
            quand le moteur rend le tableau d'un seul tenant. Réservé aux moteurs qui
            voient le PDF entier (voir `METHODS_MERGING_PAGE_BREAKS`).

    Returns:
        La liste des (nom de la prédiction, annotation chargée, chemin de la prédiction),
        et le nombre de tableaux annotés par SIREN, qui fait référence pour
        `table_count_accuracy` — après regroupement le cas échéant, puisque c'est à cette
        granularité que la comparaison a lieu.
    """
    ann = {Path(p).stem: p for p in fs.glob(f"{annotations}/*.xlsx")}
    pred = {Path(p).stem: p for p in fs.glob(f"{pred_prefix}/*.csv")}

    by_base: dict[str, list[str]] = {}
    for name in ann:
        by_base.setdefault(_base_stem(name), []).append(name)

    pairs: list[tuple[str, pd.DataFrame, str]] = []
    ann_counts: Counter = Counter()
    matched_preds: set[str] = set()
    unpaired_ann = 0
    for base, names in sorted(by_base.items()):
        pred_stems = sorted((s for s in pred if _base_stem(s) == base), key=_rank)
        anns = [_load_xlsx(fs, ann[n]) for n in sorted(names, key=_rank)]
        if merge_page_breaks:
            anns = _merge_page_breaks(anns, len(pred_stems))
        ann_counts[base] = len(anns)
        for i, ann_df in enumerate(anns):
            if i >= len(pred_stems):
                unpaired_ann += 1
                continue
            pairs.append((pred_stems[i], ann_df, pred[pred_stems[i]]))
            matched_preds.add(pred_stems[i])

    only_pred = sorted(set(pred) - matched_preds)
    if unpaired_ann:
        print(f"    [WARN] {unpaired_ann} annotation(s) sans prédiction.")
    if only_pred:
        print(f"    [WARN] {len(only_pred)} prédiction(s) sans annotation.")

    return pairs, ann_counts


def _list_pairs_from_correspondances(
    fs, correspondances_path: str, pred_prefix: str
) -> list[tuple[str, pd.DataFrame, str]]:
    """
    Apparie annotations et prédictions via le parquet de correspondances.
    La i-ème annotation (triée) d'un SIREN est appariée à la prédiction {siren}_{i}.csv.
    Retourne une liste de (nom, annotation chargée, prediction_path).
    """
    correspondances = _load_correspondances(fs, correspondances_path)
    pred = {Path(p).stem: p for p in fs.glob(f"{pred_prefix}/*.csv")}

    pairs = []
    matched_preds: set[str] = set()
    for pure_siren, xlsx_paths in correspondances.items():
        for rank, xlsx_path in enumerate(xlsx_paths, start=1):
            stem = f"{pure_siren}_{rank}"
            if stem in pred:
                pairs.append((stem, _load_xlsx(fs, xlsx_path), pred[stem]))
                matched_preds.add(stem)

    unmatched = sorted(set(pred) - matched_preds)
    if unmatched:
        print(f"    [WARN] {len(unmatched)} prédiction(s) sans annotation.")

    # Tri sur le seul nom : les DataFrames du tuple ne sont pas comparables.
    return sorted(pairs, key=lambda pair: pair[0])


def _count_per_base(fs, prefix: str, ext: str) -> Counter:
    """Compte le nombre de fichiers par SIREN (base_stem) dans un dossier S3."""
    stems = [Path(p).stem for p in fs.glob(f"{prefix}/*{ext}")]
    return Counter(_base_stem(s) for s in stems)


def paires_comptes_sociaux(cfg: CorpusConfig, fs, moteur: Moteur) -> "Paires":
    """Constitue les paires du corpus des comptes sociaux, pour un moteur.

    Args:
        cfg: configuration du corpus, qui porte les chemins des annotations et du parquet.
        fs: système de fichiers S3.
        moteur: le moteur mesuré. Son mode d'appariement et son drapeau de coupures de page
            décident de la façon dont l'annotation lui est appariée.

    Returns:
        Les paires et les deux comptes de tableaux par SIREN, annotés et prédits.
    """
    if moteur.appariement == "correspondances":
        chemin = cfg.sources["correspondances"]
        pairs = _list_pairs_from_correspondances(fs, chemin, moteur.csv)
        correspondances = _load_correspondances(fs, chemin)
        ann_counts = Counter({siren: len(paths) for siren, paths in correspondances.items()})
    else:
        pairs, ann_counts = _list_pairs(
            fs, cfg.annotations, moteur.csv, merge_page_breaks=moteur.fusion_coupures_page
        )
    return Paires(pairs, ann_counts, _count_per_base(fs, moteur.csv, ".csv"))


# ── Appariement : corpus des tableaux historiques ──────────────────────────────

# `{clé}_{rang}.csv`, où la clé porte elle-même des chiffres et des `_` (`1970_bis`).
_RANK_RE = re.compile(r"^(.*)_(\d+)$")


def _split_rank(nom: str) -> tuple[str, int]:
    """Sépare le radical d'un CSV prédit de son rang.

    `_base_stem` ne convient pas ici : sur un radical qui est lui-même une année
    (`crop_1970`), il prendrait `1970` pour le rang.

    Args:
        nom: nom du CSV sans extension, de la forme `{radical}_{rang}`.

    Returns:
        Le couple (radical, rang) ; (nom, 0) si le nom ne porte pas de rang.
    """
    m = _RANK_RE.match(nom)
    return (m.group(1), int(m.group(2))) if m else (nom, 0)


def list_pairs_historiques(
    fs, annotations: str, pred_prefix: str
) -> tuple[list[tuple[str, pd.DataFrame, str]], Counter]:
    """Apparie annotations HTML et CSV prédits par clé de tableau.

    L'appariement se fait par nom de fichier, `ground_truth_1970_bis.html` ↔
    `crop_1970_bis_{rang}.csv` : il n'y a ni SIREN ni parquet de correspondances. Le rang 1
    seul est comparé, les suivants sont signalés (cf. README).

    Args:
        fs: système de fichiers S3.
        annotations: dossier des annotations HTML de référence.
        pred_prefix: dossier des CSV prédits.

    Returns:
        La liste des (clé du tableau, annotation chargée, chemin de la prédiction), et le
        nombre de tableaux prédits par clé — un corpus où chaque image porte un tableau
        entier, ce compte dit la sur-segmentation du moteur.
    """
    references = {CH.key_from_annotation(p): p for p in fs.glob(f"{annotations}/*.html")}

    predictions: dict[str, dict[int, str]] = {}
    for path in fs.glob(f"{pred_prefix}/*.csv"):
        radical, rang = _split_rank(CH.stem(path))
        predictions.setdefault(CH.key_from_image(radical), {})[rang] = path

    pairs: list[tuple[str, pd.DataFrame, str]] = []
    pred_counts: Counter = Counter()
    sans_prediction: list[str] = []
    sur_decoupes: list[str] = []
    for cle in sorted(references):
        rangs = predictions.get(cle, {})
        pred_counts[cle] = len(rangs)
        if 1 not in rangs:
            sans_prediction.append(cle)
            continue
        if len(rangs) > 1:
            sur_decoupes.append(f"{cle} ({len(rangs)})")
        pairs.append((cle, load_html(fs, references[cle]), rangs[1]))

    orphelines = sorted(set(predictions) - set(references))
    if sans_prediction:
        print(
            f"    [WARN] {len(sans_prediction)} annotation(s) sans prédiction : "
            f"{', '.join(sans_prediction)}"
        )
    if sur_decoupes:
        print(
            f"    [WARN] {len(sur_decoupes)} image(s) découpée(s) en plusieurs tableaux, "
            f"seul le rang 1 est comparé : {', '.join(sur_decoupes)}"
        )
    if orphelines:
        print(
            f"    [WARN] {len(orphelines)} prédiction(s) sans annotation : {', '.join(orphelines)}"
        )

    return pairs, pred_counts


def paires_historiques(cfg: CorpusConfig, fs, moteur: Moteur) -> "Paires":
    """Constitue les paires du corpus des tableaux historiques, pour une condition.

    Args:
        cfg: configuration du corpus, qui porte le dossier des annotations.
        fs: système de fichiers S3.
        moteur: la condition d'extraction mesurée. Sans effet sur l'appariement, qui ne
            dépend que du nommage des fichiers.

    Returns:
        Les paires et les deux comptes de tableaux par clé d'image. Le compte annoté vaut
        1 partout : une image porte un tableau entier.
    """
    pairs, pred_counts = list_pairs_historiques(fs, cfg.annotations, moteur.csv)
    return Paires(pairs, Counter({cle: 1 for cle, _, _ in pairs}), pred_counts)


# ── Helpers cellules ──────────────────────────────────────────────────────────

# Toutes les variantes de tiret jouent le même rôle dans ces tableaux et ne doivent donc pas
# distinguer deux valeurs : cf. README.
_DASHES = str.maketrans(dict.fromkeys("‐‑‒–—―−﹘﹣－", "-"))


def _unify_dashes(value: str) -> str:
    """Ramène toute variante de tiret au trait d'union.

    Args:
        value: chaîne brute.

    Returns:
        La même chaîne, tirets unifiés.
    """
    return value.translate(_DASHES)


def _normalize_label(value: str) -> str:
    """Ramène un libellé à sa graphie canonique, pour la seule comparaison.

    Casse, accents, espaces et variantes de tiret ne distinguent pas deux libellés : ce que
    coûtait leur comparaison brute est mesuré dans le README.

    Args:
        value: libellé brut.

    Returns:
        Le libellé sans accents, en bas de casse, espaces réduits, tirets unifiés.
    """
    sans_accents = "".join(
        c
        for c in unicodedata.normalize("NFKD", _unify_dashes(value))
        if not unicodedata.combining(c)
    )
    return " ".join(sans_accents.casefold().split())


def _is_empty(value: str) -> bool:
    return value.strip() == ""


_NUMERIC_PLACEHOLDERS = {
    "-",
    "–",
    "—",
    "n.a.",
    "n/a",
    "nd",
    "n.d.",
    "ns",
    "nc",
    "n.c.",
}
_UNIT_SUFFIX_RE = re.compile(
    r"(?i)\s*(€|eur|euros?|usd|\$|gbp|£|nok|sek|chf|jpy|¥|kr|%|pp|bps?)\s*$"
)


def _looks_numeric(value: str) -> bool:
    """La cellule relève-t-elle de la zone de données, plutôt que d'un en-tête ?

    Volontairement permissive : elle décide du dénominateur de `numeric_recovery`, pas de
    l'égalité de deux valeurs — comparer est l'affaire de `_normalize_numeric_str`. Ce
    qu'elle accepte en plus des nombres purs et de la cellule vide : cf. README.

    Args:
        value: cellule brute.

    Returns:
        True si la cellule ressemble à une donnée numérique.
    """
    s = value.strip()
    if not s:
        return True
    if s.lower() in _NUMERIC_PLACEHOLDERS:
        return True
    s = _UNIT_SUFFIX_RE.sub("", s).strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1].strip()
    s = s.replace(",", ".").replace(" ", "").replace(" ", "")
    if not s:
        return True
    try:
        float(s)
        return True
    except ValueError:
        return False


# Les écarts purement typographiques que la comparaison doit absorber : cf. README.
_ACCOUNTING_RE = re.compile(r"^\((.+)\)$")
_LEADING_SIGN_RE = re.compile(r"^-[\s\xa0\u202f]*")
_THOUSANDS_RE = re.compile(r"(\d)[\s\xa0\u202f]+(\d)")
_PERCENT_RE = re.compile(r"(\d+(?:[,.]\d+)?)[\s\xa0\u202f]*%")
_PLAIN_RE = re.compile(r"\d+(?:[,.]\d+)?")


def _canonical_number(body: str) -> str | None:
    """Forme canonique d'un nombre non signé, ou None si ce n'en est pas un.

    Le cas général n'emprunte pas `float`, qui arrondirait les grands montants : cf. README.

    Args:
        body: chaîne sans signe, espaces séparateurs de milliers déjà retirés.

    Returns:
        La forme canonique (point décimal, zéros non significatifs retirés), ou None.
    """
    percent = _PERCENT_RE.fullmatch(body)
    if percent:
        return f"{float(percent.group(1).replace(',', '.')) / 100:.6g}"
    if not _PLAIN_RE.fullmatch(body):
        return None
    integer, _, fraction = body.replace(",", ".").partition(".")
    integer = integer.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    return f"{integer}.{fraction}" if fraction else integer


def _normalize_numeric_str(val: str) -> str:
    """Ramène une cellule à une forme canonique, pour la seule comparaison.

    Absorbe les écarts d'écriture, ceux où les deux côtés portent les mêmes chiffres — la
    liste complète est dans le README. Une cellule qui n'est pas un nombre est rendue telle
    quelle, tirets unifiés et espaces entre chiffres retirés : la comparaison reste alors
    textuelle. **Signe et parenthèses ne sont interprétés que devant un nombre.**

    Args:
        val: cellule brute.

    Returns:
        La forme canonique comparable.
    """
    text = _unify_dashes(val.strip())

    body, negative = text, False
    accounting = _ACCOUNTING_RE.match(body)
    if accounting:
        body, negative = accounting.group(1).strip(), True
    if _LEADING_SIGN_RE.match(body):
        body, negative = _LEADING_SIGN_RE.sub("", body, count=1), True

    # '1 234 567' porte deux espaces séparateurs, et les correspondances de `re.sub` ne se
    # chevauchent pas : une seule passe n'en retirerait pas forcément tous.
    def strip_thousands(value: str) -> str:
        for _ in range(3):
            value = _THOUSANDS_RE.sub(r"\1\2", value)
        return value

    canonical = _canonical_number(strip_thousands(body))
    if canonical is not None:
        return ("-" if negative else "") + canonical
    return strip_thousands(text)


def _cell_recovered(prediction: pd.DataFrame, pr: int, pc: int, val: str, delta: int) -> bool:
    """La valeur attendue est-elle présente dans la prédiction en (pr, pc±delta) ?

    Args:
        prediction: grille prédite.
        pr: ligne appariée dans la prédiction.
        pc: colonne appariée dans la prédiction.
        val: valeur attendue.
        delta: tolérance en colonnes (±).

    Returns:
        True dès qu'une des colonnes balayées porte la même valeur, après normalisation.
    """
    target = _normalize_numeric_str(val)
    for dc in range(-delta, delta + 1):
        c = pc + dc
        if 0 <= c < len(prediction.columns):
            if _normalize_numeric_str(prediction.iloc[pr, c]) == target:
                return True
    return False


def _non_numeric_rate(series: pd.Series) -> float:
    non_empty = [v for v in series if not _is_empty(v)]
    if not non_empty:
        return 1.0
    return 1.0 - sum(1 for v in non_empty if _looks_numeric(v)) / len(non_empty)


# ── Étape 1 : Détection des en-têtes ─────────────────────────────────────────


def detect_column_header_height(df: pd.DataFrame) -> int:
    """Nombre de lignes formant l'en-tête des colonnes.

    Intègre les lignes numériques initiales, puis les lignes majoritairement non
    numériques. Les deux garde-fous — aucune ligne textuelle absorbée, ou toutes — sont
    justifiés dans le README.
    """
    n, i = len(df), 0
    while i < n and _non_numeric_rate(df.iloc[i]) < 0.5:
        i += 1
    phase1_end = i
    while i < n and _non_numeric_rate(df.iloc[i]) >= 0.5:
        i += 1
    if i == phase1_end:
        return 0
    if i == n:
        j = phase1_end
        while j < n and _non_numeric_rate(df.iloc[j]) >= 1.0:
            j += 1
        return j
    return i


def detect_row_header_width(df: pd.DataFrame) -> int:
    """
    Nombre de colonnes formant l'en-tête des lignes.
    Première colonne toujours incluse, puis les suivantes tant qu'elles
    contiennent au moins une cellule numérique stricte ET sont
    majoritairement non-numériques (taux non-numérique > 0.5).
    """
    n_cols = len(df.columns)
    if n_cols == 0:
        return 0
    width = 1
    for c in range(1, n_cols):
        col = df.iloc[:, c]
        if not any(_looks_numeric(v) and not _is_empty(v) for v in col):
            break
        if _non_numeric_rate(col) <= 0.5:
            break
        width += 1
    return width


# ── Étape 2 : Matching Levenshtein + Gale-Shapley ────────────────────────────


def _levenshtein_distance(s: str, t: str) -> int:
    m, n = len(s), len(t)
    if m < n:
        s, t, m, n = t, s, n, m
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        curr = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if s[i - 1] == t[j - 1] else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[n]


def _lev_similarity(a: str, b: str) -> float:
    """Similarité de Levenshtein, libellés ramenés à leur graphie canonique."""
    a, b = _normalize_label(a), _normalize_label(b)
    if not a and not b:
        return 1.0
    return 1.0 - _levenshtein_distance(a, b) / max(len(a), len(b))


def _build_header_texts(
    df: pd.DataFrame, axis: str, n_header_rows: int, n_header_cols: int
) -> list[str]:
    """
    Texte représentatif de chaque colonne (axis='col') ou ligne (axis='row'),
    formé en concaténant les cellules de l'en-tête correspondant.
    """
    texts = []
    if axis == "col":
        for c in range(len(df.columns)):
            parts = [df.iloc[r, c] for r in range(n_header_rows) if not _is_empty(df.iloc[r, c])]
            texts.append(" | ".join(parts))
    else:
        for r in range(len(df)):
            parts = [df.iloc[r, c] for c in range(n_header_cols) if not _is_empty(df.iloc[r, c])]
            texts.append(" | ".join(parts))
    return texts


def _gale_shapley(scores: np.ndarray) -> dict[int, int]:
    """
    Matching stable (Gale-Shapley) entre proposeurs (annotation) et accepteurs (prédiction).
    Retourne {ann_idx: pred_idx}.
    """
    n_a, n_b = scores.shape
    prefs_a = [list(np.argsort(-scores[i])) for i in range(n_a)]
    rank_b = {j: {i: r for r, i in enumerate(np.argsort(-scores[:, j]))} for j in range(n_b)}
    free_a = list(range(n_a))
    next_prop = [0] * n_a
    match_b: dict[int, int] = {}
    match_a: dict[int, int] = {}

    while free_a:
        a = free_a.pop(0)
        if next_prop[a] >= n_b:
            continue
        b = prefs_a[a][next_prop[a]]
        next_prop[a] += 1
        if b not in match_b:
            match_b[b] = a
            match_a[a] = b
        else:
            cur = match_b[b]
            if rank_b[b].get(a, n_a) < rank_b[b].get(cur, n_a):
                match_b[b] = a
                match_a[a] = b
                del match_a[cur]
                free_a.append(cur)
            else:
                free_a.append(a)
    return match_a


def _match_headers(ann_texts: list[str], pred_texts: list[str], threshold: float) -> dict[int, int]:
    """Retourne {ann_idx: pred_idx} pour les paires dont la similarité >= threshold."""
    n_a, n_p = len(ann_texts), len(pred_texts)
    if n_a == 0 or n_p == 0:
        return {}
    scores = np.array(
        [[_lev_similarity(ann_texts[i], pred_texts[j]) for j in range(n_p)] for i in range(n_a)]
    )
    raw = _gale_shapley(scores)
    return {i: j for i, j in raw.items() if scores[i, j] >= threshold}


# ── Étape 3 : Métriques ───────────────────────────────────────────────────────


def evaluate_pair(
    annotation: pd.DataFrame,
    prediction: pd.DataFrame,
    threshold: float = 0.5,
    cell_delta: int = 0,
) -> dict:
    """
    Calcule les métriques de rappel pour une paire (annotation, prédiction).
    Les valeurs sont comparées en tant que chaînes de caractères (.strip()).
    cell_delta : tolérance en nombre de colonnes (±) pour total_extraction uniquement.
    """
    ann_hrows = detect_column_header_height(annotation)
    ann_hcols = detect_row_header_width(annotation)
    pred_hrows = detect_column_header_height(prediction)
    pred_hcols = detect_row_header_width(prediction)

    col_match = _match_headers(
        _build_header_texts(annotation, "col", ann_hrows, ann_hcols),
        _build_header_texts(prediction, "col", pred_hrows, pred_hcols),
        threshold,
    )
    row_match = _match_headers(
        _build_header_texts(annotation, "row", ann_hrows, ann_hcols),
        _build_header_texts(prediction, "row", pred_hrows, pred_hcols),
        threshold,
    )

    n_ann_rows, n_ann_cols = len(annotation), len(annotation.columns)
    col_recovery = len(col_match) / n_ann_cols if n_ann_cols else 0.0
    row_recovery = len(row_match) / n_ann_rows if n_ann_rows else 0.0

    # Cellules numériques (zone de données, hors en-têtes) : toutes celles que
    # `_looks_numeric` retient, comparées par un seul chemin (cf. README).
    total_num = recovered_num = 0
    for r in range(ann_hrows, n_ann_rows):
        for c in range(ann_hcols, n_ann_cols):
            val = annotation.iloc[r, c]
            if not _looks_numeric(val):
                continue
            total_num += 1
            if r in row_match and c in col_match:
                pr, pc = row_match[r], col_match[c]
                if pr < len(prediction) and pc < len(prediction.columns):
                    if _normalize_numeric_str(prediction.iloc[pr, pc]) == _normalize_numeric_str(
                        val
                    ):
                        recovered_num += 1

    numeric_recovery = recovered_num / total_num if total_num else float("nan")  # cas sans cellules

    # Indicatrice d'extraction totale :
    # (1) structure complète : toutes les colonnes ET lignes de l'annotation sont matchées
    # (2) toutes les cellules numériques de la zone de données sont récupérées (normalisées)
    total_ok = len(col_match) == n_ann_cols and len(row_match) == n_ann_rows
    if total_ok:
        for r in range(ann_hrows, n_ann_rows):
            if not total_ok:
                break
            for c in range(ann_hcols, n_ann_cols):
                val = annotation.iloc[r, c]
                if not _looks_numeric(val):
                    continue
                if r not in row_match or c not in col_match:
                    total_ok = False
                    break
                pr, pc = row_match[r], col_match[c]
                if pr >= len(prediction) or pc >= len(prediction.columns):
                    total_ok = False
                    break
                if not _cell_recovered(prediction, pr, pc, val, cell_delta):
                    total_ok = False
                    break

    return {
        "col_recovery": col_recovery,
        "row_recovery": row_recovery,
        "numeric_recovery": numeric_recovery,
        "total_extraction": int(total_ok),
        "n_ann_rows": n_ann_rows,
        "n_ann_cols": n_ann_cols,
        "n_matched_rows": len(row_match),
        "n_matched_cols": len(col_match),
        "n_numeric_cells": total_num,
        "n_recovered_numeric": recovered_num,
        "ann_header_rows": ann_hrows,
        "ann_header_cols": ann_hcols,
    }


# ── Résumés, propres à chaque corpus ──────────────────────────────────────────


def _resume_comptes_sociaux(sub: pd.DataFrame) -> None:
    """Imprime le comptage de tableaux d'un moteur, à la granularité du SIREN.

    Un SIREN porte plusieurs tableaux : le compte se fait donc après déduplication, une
    ligne par SIREN, et non par paire évaluée.
    """
    if "n_ann_tables" not in sub.columns or "n_pred_tables" not in sub.columns:
        return
    siren_df = sub.copy()
    siren_df["_siren"] = siren_df["fichier"].apply(_base_stem)
    siren_df = siren_df.drop_duplicates("_siren")
    n_total = len(siren_df)
    n_match = (siren_df["n_pred_tables"] == siren_df["n_ann_tables"]).sum()
    print(f"    {'---':<25}")
    print(f"    {'table_count_accuracy':<25}: {n_match / n_total:.4f}  ({n_match}/{n_total} SIREN)")
    print(f"    {'moy. tableaux annotés':<25}: {siren_df['n_ann_tables'].mean():.2f}")
    print(f"    {'moy. tableaux détectés':<25}: {siren_df['n_pred_tables'].mean():.2f}")


def _resume_historiques(sub: pd.DataFrame) -> None:
    """Imprime la récupération par cellule et le comptage de tableaux d'une condition.

    Une image porte un tableau : `table_count_accuracy` compte donc les images dont le
    moteur a rendu exactement une grille.
    """
    cellule = sub["n_recovered_numeric"].sum() / sub["n_numeric_cells"].sum()
    print(f"    {'récup. par cellule':<25}: {cellule:.4f}")
    n_match = (sub["n_pred_tables"] == 1).sum()
    print(f"    {'table_count_accuracy':<25}: {n_match / len(sub):.4f}  ({n_match}/{len(sub)})")


# ── Corpus : ce qui distingue une mesure de l'autre ───────────────────────────


@dataclass(frozen=True)
class Paires:
    """Paires d'un moteur, et les deux comptes de tableaux qui font `table_count_accuracy`.

    Attributes:
        pairs: les (nom du tableau, annotation chargée, chemin de la prédiction).
        ann_counts: nombre de tableaux annotés par clé de document.
        pred_counts: nombre de tableaux prédits par clé de document.
    """

    pairs: list[tuple[str, pd.DataFrame, str]]
    ann_counts: Counter
    pred_counts: Counter


@dataclass(frozen=True)
class Corpus:
    """Tout ce qu'un corpus apporte de particulier à la mesure.

    Ce que le corpus a de *configurable* — chemins, moteurs, seuils — vit dans `cfg` ;
    ce qu'il a de *codé* — appariement, clé de comptage, résumé — vit dans les trois
    callables.

    Attributes:
        cfg: configuration du corpus, lue depuis `config/{nom}.yaml`.
        lister_paires: (config, fs, moteur) → `Paires`.
        cle_document: du nom d'un tableau à la clé sous laquelle les tableaux sont comptés
            — le SIREN pour les comptes sociaux, la clé d'image pour les historiques.
        resume: complément de résumé imprimé sous les moyennes de chaque méthode.
    """

    cfg: CorpusConfig
    lister_paires: Callable[..., Paires]
    cle_document: Callable[[str], str]
    resume: Callable[[pd.DataFrame], None]

    @property
    def eval_output(self) -> str:
        """Chemin S3 du parquet des métriques."""
        return self.cfg.evaluation["sortie"]

    @property
    def largeur_nom(self) -> int:
        """Largeur de la colonne des noms à l'affichage."""
        return self.cfg.evaluation["largeur_nom"]


# Le code que chaque mode d'appariement apporte à la mesure. La configuration d'un corpus
# désigne le sien par `evaluation.appariement` : ajouter un corpus qui s'apparie comme un
# corpus existant ne demande donc qu'un fichier YAML.
TRAITEMENTS: dict[str, tuple] = {
    # Par SIREN, rang à rang.
    "rang": (paires_comptes_sociaux, _base_stem, _resume_comptes_sociaux),
    # Par nom de fichier. Le nom d'un tableau *est* la clé de son image : rien à en retirer.
    "cle_fichier": (paires_historiques, lambda nom: nom, _resume_historiques),
}


def _corpus(cfg: CorpusConfig) -> Corpus:
    """Assemble la mesure d'un corpus : sa configuration, et le code de son appariement.

    Args:
        cfg: configuration du corpus.

    Returns:
        Le corpus prêt à mesurer.

    Raises:
        SystemExit: si le mode d'appariement déclaré n'est pas implémenté ici.
    """
    mode = cfg.evaluation["appariement"]
    if mode not in TRAITEMENTS:
        raise SystemExit(
            f"Corpus {cfg.nom} : appariement {mode!r} inconnu. "
            f"Modes implémentés : {', '.join(TRAITEMENTS)}."
        )
    lister, cle, resume = TRAITEMENTS[mode]
    return Corpus(cfg=cfg, lister_paires=lister, cle_document=cle, resume=resume)


CORPUS: dict[str, Corpus] = {nom: _corpus(cfg) for nom, cfg in config.tous().items()}


# ── Évaluation par lot ────────────────────────────────────────────────────────


def evaluate_dataset(
    corpus: str = "comptes-sociaux", threshold: float | None = None, cell_delta: int | None = None
) -> pd.DataFrame:
    """Évalue tout un corpus et dépose le parquet des métriques sur S3.

    Args:
        corpus: clé de `CORPUS`, c'est-à-dire un fichier de `config/`.
        threshold: similarité minimale pour apparier deux en-têtes. Par défaut, le
            `seuil_similarite` déclaré par le corpus.
        cell_delta: tolérance en colonnes (±) pour `total_extraction`. Par défaut, la
            `tolerance_colonnes` déclarée par le corpus.

    Returns:
        Le tableau des métriques, une ligne par couple tableau × méthode.
    """
    conf = CORPUS[corpus]
    if threshold is None:
        threshold = conf.cfg.evaluation["seuil_similarite"]
    if cell_delta is None:
        cell_delta = conf.cfg.evaluation["tolerance_colonnes"]
    fs = get_s3_fs()
    all_results: list[dict] = []

    for method, moteur in conf.cfg.moteurs.items():
        paires = conf.lister_paires(conf.cfg, fs, moteur)
        print(f"\n[{method}] {len(paires.pairs)} paire(s) trouvée(s)")

        for name, ann_df, pred_path in paires.pairs:
            try:
                pred_df = _load_csv(fs, pred_path)
                metrics = evaluate_pair(ann_df, pred_df, threshold=threshold, cell_delta=cell_delta)
                metrics.update({"fichier": name, "methode": method})
                print(
                    f"  {name:<{conf.largeur_nom}} "
                    f"col={metrics['col_recovery']:.3f}  "
                    f"row={metrics['row_recovery']:.3f}  "
                    f"num={metrics['numeric_recovery']:.3f}  "
                    f"total={metrics['total_extraction']}"
                )
            except Exception as e:
                print(f"  [ERR] {name}: {e}")
                metrics = {
                    "fichier": name,
                    "methode": method,
                    "col_recovery": None,
                    "row_recovery": None,
                    "numeric_recovery": None,
                    "total_extraction": None,
                }
            cle = conf.cle_document(name)
            metrics["n_ann_tables"] = paires.ann_counts.get(cle, 0)
            metrics["n_pred_tables"] = paires.pred_counts.get(cle, 0)
            all_results.append(metrics)

    df = pd.DataFrame(all_results)
    _save_parquet(fs, df, conf.eval_output)
    _print_summary(df, conf)
    return df


def _save_parquet(fs, df: pd.DataFrame, path: str) -> None:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    fs.pipe(path, buf.getvalue())
    print(f"\nRésultats sauvegardés : s3://{path}")


def _print_summary(df: pd.DataFrame, conf: Corpus) -> None:
    """Imprime les moyennes par méthode, puis le complément propre au corpus."""
    metric_cols = ["col_recovery", "row_recovery", "numeric_recovery", "total_extraction"]
    print("\n=== Moyennes par méthode ===")
    for method in df["methode"].dropna().unique():
        sub = df[df["methode"] == method]
        print(f"\n  {method}  (n={len(sub)})")
        for col in metric_cols:
            print(f"    {col:<25}: {sub[col].dropna().mean():.4f}")
        conf.resume(sub)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Évaluation de l'extraction de tableaux")
    parser.add_argument(
        "--corpus",
        choices=[*CORPUS, "all"],
        default="comptes-sociaux",
        help="corpus à évaluer (défaut : comptes-sociaux)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Seuil de similarité Levenshtein pour le matching (défaut : celui du corpus)",
    )
    parser.add_argument(
        "--cell-delta",
        type=int,
        default=None,
        help=(
            "Tolérance en colonnes (±) pour numeric_recovery et total_extraction "
            "(défaut : celle du corpus)"
        ),
    )
    args = parser.parse_args()

    for nom in CORPUS if args.corpus == "all" else [args.corpus]:
        print(f"\n{'=' * 78}\n=== Corpus : {nom}\n{'=' * 78}")
        evaluate_dataset(nom, threshold=args.threshold, cell_delta=args.cell_delta)
