#!/usr/bin/env python3
"""
Pipeline d'évaluation pour l'extraction de tableaux depuis images.

Compare les tableaux prédits (CSV) produits par Marker et OpenDataLoader
avec les annotations de référence (XLSX).

Sources CSV :
  s3://projet-extraction-tableaux/reprise/output_csv/marker/
  s3://projet-extraction-tableaux/reprise/output_csv/opendataloader/
  s3://projet-extraction-tableaux/reprise/output_csv/chandra/
Annotations :
  s3://projet-extraction-tableaux/annotations/clean/
Résultats :
  s3://projet-extraction-tableaux/reprise/eval/evaluation.parquet

Métriques (type rappel, une ligne par couple fichier × méthode) :
  col_recovery      – taux de colonnes de l'annotation retrouvées
  row_recovery      – taux de lignes de l'annotation retrouvées
  numeric_recovery  – taux de cellules numériques (hors en-têtes) bien récupérées
  total_extraction  – 1 si structure complète (toutes colonnes/lignes matchées) et toutes
                      les cellules numériques de la zone de données sont récupérées

Les valeurs sont comparées après normalisation (`_normalize_numeric_str`), qui absorbe les
écarts d'écriture — espaces séparateurs de milliers, séparateur décimal, signe négatif
détaché, parenthèses comptables, pourcentages — mais rien d'autre : deux valeurs aux
chiffres différents restent différentes.

Usage :
    uv run evaluation_extraction.py [--threshold 0.5 --cell-delta 0]
"""

import argparse
import io
import re
import unicodedata
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
from extraction_common.s3 import get_s3_fs

BUCKET = "projet-extraction-tableaux"
S3_ANNOTATIONS = f"{BUCKET}/annotations/clean"
S3_EVAL_OUTPUT = f"{BUCKET}/reprise/eval/evaluation.parquet"
S3_CORRESPONDANCES = f"{BUCKET}/reprise/correspondances.parquet"

_SIREN_RE = re.compile(r"\d{9}")

METHODS: dict[str, str] = {
    "marker": f"{BUCKET}/reprise/output_csv/marker",
    "opendataloader": f"{BUCKET}/reprise/output_csv/opendataloader",
    "chandra": f"{BUCKET}/reprise/output_csv/chandra",
    "marker_last_work": f"{BUCKET}/LLM_eval/output_csv/marker_last_work",
}


# ── Chargement S3 ─────────────────────────────────────────────────────────────


def _load_correspondances(fs) -> dict[str, list[str]]:
    """
    Charge le parquet de correspondances.
    Retourne {pure_siren: [xlsx_path_1, ...]} (chemins s3fs sans schéma s3://).
    Le SIREN pur (9 chiffres) est extrait du stem PDF (colonne 'siren').
    """
    with fs.open(S3_CORRESPONDANCES, "rb") as f:
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

    La complétion à droite n'est qu'un filet : depuis `_rectangularize`, les extracteurs
    de `json_to_csv.py` rendent des grilles déjà rectangulaires. Elle ne sert plus que
    pour les CSV produits avant ce correctif (`output_csv/marker_pre_rowspan/`). La
    supprimer ne changerait d'ailleurs rien au comportement — `pd.DataFrame` complète
    aussi les lignes courtes, et `fillna` remplace les manquants par des chaînes vides.

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


_BASE_STEM_RE = re.compile(r"^(.*?)_(\d+)$")


def _base_stem(name: str) -> str:
    """Retire le suffixe _N d'un nom de fichier pour obtenir l'identifiant SIREN."""
    m = _BASE_STEM_RE.match(name)
    return m.group(1) if m else name


def _rank(stem: str) -> int:
    """Rang `{siren}_{n}` d'un nom de fichier, 0 s'il n'en porte pas.

    Le tri lexicographique placerait `_10` avant `_2` : à sept tableaux pour un même SIREN
    le corpus n'y touche pas, mais l'appariement se fait par rang et ne doit pas en
    dépendre.
    """
    m = _BASE_STEM_RE.match(stem)
    return int(m.group(2)) if m else 0


# ── Coupures de page : granularité de la référence, propre au moteur ───────────
#
# Un tableau à cheval sur deux pages est annoté une page par fichier. Chandra est appelé
# page par page et rend la même segmentation : rien à faire. Marker reçoit le PDF entier et
# son `TableGroup` enjambe parfois la coupure — sur 3 des 6 documents multi-pages du
# corpus. Face à un tableau marker d'un seul tenant, l'annotation `_2` n'est appariée à
# rien et ses cellules quittent le dénominateur en silence.
#
# On regroupe donc les annotations pour la seule référence de marker, et seulement quand
# marker a effectivement produit moins de tableaux. Les annotations de chandra ne sont
# jamais touchées : sa mesure reste celle du découpage par page.
METHODS_MERGING_PAGE_BREAKS = frozenset({"marker"})


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
    numérique, intertitre compris : un tableau qui s'ouvre sur « 2. Participations » puis
    enchaîne sur des données paraît donc en avoir un. On écarte d'abord ces lignes-là.

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

    Deux formes, et deux seulement, distinguent une coupure d'une succession de tableaux
    distincts — à largeur identique, condition nécessaire dans les deux cas :

    - `nxt` **n'a pas d'en-tête de colonnes** et reprend directement sur du contenu, qu'il
      s'agisse de données ou d'un intertitre de section : ce n'est pas un tableau autonome ;
    - `nxt` **réimprime exactement le même en-tête**, mêmes libellés dans le même ordre,
      comme le PDF le réimprime en tête de page.

    Un tableau coupé **en largeur** — mêmes lignes, d'autres colonnes sur la page suivante
    — n'entre dans aucun des deux cas : les largeurs diffèrent, et concaténer par lignes
    serait faux.

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
        ou si aucune coupure n'est reconnue — deux tableaux réellement distincts ne sont
        jamais fusionnés pour faire tomber le compte, la sous-détection du moteur restant
        alors visible dans la mesure.
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


def _list_pairs(
    fs, pred_prefix: str, merge_page_breaks: bool = False
) -> tuple[list[tuple[str, pd.DataFrame, str]], Counter]:
    """Apparie annotations (.xlsx) et prédictions (.csv), rang à rang au sein d'un SIREN.

    Args:
        fs: système de fichiers S3.
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
    ann = {Path(p).stem: p for p in fs.glob(f"{S3_ANNOTATIONS}/*.xlsx")}
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


def _list_pairs_from_correspondances(fs, pred_prefix: str) -> list[tuple[str, pd.DataFrame, str]]:
    """
    Apparie annotations et prédictions via le parquet de correspondances.
    La i-ème annotation (triée) d'un SIREN est appariée à la prédiction {siren}_{i}.csv.
    Retourne une liste de (nom, annotation chargée, prediction_path).
    """
    correspondances = _load_correspondances(fs)
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


# ── Helpers cellules ──────────────────────────────────────────────────────────

# Toutes les variantes de tiret jouent le même rôle dans ces tableaux — marque d'absence
# dans une cellule de données, signe négatif devant un montant, séparateur dans un
# libellé — et ne doivent donc pas distinguer deux valeurs. Les annotations écrivent
# « - » là où les moteurs rendent « — » : sans cette équivalence, 14 cellules de
# `TAB_552096281_2` étaient comptées comme du texte à la place d'un nombre, et
# « A - FILIALES DETENUES » ne s'appariait pas à « A – FILIALES DETENUES » (similarité
# 0,25, l'annotation et la prédiction ne différant que par le demi-cadratin).
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

    Casse, accents, espaces et variantes de tiret ne distinguent pas deux libellés : un
    moteur qui compose ses en-têtes en capitales décrit les mêmes colonnes qu'une
    annotation en bas de casse. Comparées telles quelles par distance de Levenshtein,
    « Capital » et « CAPITAL » tombent à 0,14 de similarité — sous le seuil de 0,5, donc
    non appariées. Sur `TAB_300221017_1`, chandra rend l'en-tête entier en capitales :
    les 11 colonnes échouaient à s'apparier et les 350 cellules de données étaient
    comptées perdues, pour une extraction pourtant juste.

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
    l'égalité de deux valeurs — comparer est l'affaire de `_normalize_numeric_str`. Sont
    donc acceptés, en plus des nombres purs et de la cellule vide :

    - parenthèses comptables : (14) → -14
    - unités en suffixe      : 15,24 €, 344 369 NOK, 100,00%
    - placeholders d'absence : -, NC, ND, N/A

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


# Les trois familles d'écarts purement typographiques que la comparaison doit absorber :
# une valeur entre parenthèses est un négatif en écriture comptable, un signe peut être
# détaché du nombre par une espace, et le séparateur décimal s'écrit indifféremment
# virgule ou point. Dans les trois cas les deux côtés portent les mêmes chiffres.
_ACCOUNTING_RE = re.compile(r"^\((.+)\)$")
_LEADING_SIGN_RE = re.compile(r"^-[\s\xa0\u202f]*")
_THOUSANDS_RE = re.compile(r"(\d)[\s\xa0\u202f]+(\d)")
_PERCENT_RE = re.compile(r"(\d+(?:[,.]\d+)?)[\s\xa0\u202f]*%")
_PLAIN_RE = re.compile(r"\d+(?:[,.]\d+)?")


def _canonical_number(body: str) -> str | None:
    """Forme canonique d'un nombre non signé, ou None si ce n'en est pas un.

    Le cas général n'emprunte pas `float` : sur un montant de plus de dix chiffres
    significatifs — `10 640 226 396` existe dans le corpus — un formatage en `.10g`
    arrondirait, et deux montants voisins se confondraient.

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

    Absorbe les écarts d'écriture, ceux où les deux côtés portent les mêmes chiffres :

    - espaces séparateurs de milliers, insécables compris : '25 000' → '25000' ;
    - séparateur décimal : '2.08' et '2,08' → '2.08' ;
    - signe négatif détaché du nombre : '- 30 000' → '-30000' ;
    - parenthèses comptables : '(1 976)' → '-1976' ;
    - pourcentages, convertis en décimales : '100%' et '100,00%' → '1' ;
    - variantes de tiret : '—' → '-', '−30' → '-30'.

    Une cellule qui n'est pas un nombre est rendue telle quelle, tirets unifiés et espaces
    entre chiffres retirés : la comparaison reste alors textuelle. **Signe et parenthèses
    ne sont interprétés que devant un nombre** — sans quoi '(en milliers d'euros)' se
    verrait attribuer un signe négatif.

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
    """
    Nombre de lignes formant l'en-tête des colonnes.
    Intègre les lignes numériques initiales, puis les lignes majoritairement
    non-numériques (taux non-numérique >= 0.5).

    Garde-fous :
    - Si aucune ligne textuelle n'est absorbée par la phase 2, on considère qu'il
      n'y a pas d'en-tête de colonnes (ex. tableaux filiales sans ligne d'en-tête).
    - Si la phase 2 absorbe TOUTES les lignes restantes jusqu'à la fin (typique
      des tableaux text-heavy où chaque ligne a `rate >= 0.5` sans pour autant
      être un en-tête), on retombe sur les seules lignes 100% non-numériques.
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

    # Cellules numériques (zone de données, hors en-têtes)
    # On compte toutes les cellules `_looks_numeric` (nombres purs, unités, parenthèses,
    # placeholders), et on les compare toutes par `_normalize_numeric_str`. Un seul chemin :
    # les cellules à unité ou à parenthèses partaient autrefois en comparaison de chaîne
    # brute, ce qui comptait fausses `100%` contre `100,00%` ou `2.08` contre `2,08` — des
    # écarts d'écriture, à chiffres identiques de part et d'autre.
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


# ── Comptage de tableaux par SIREN ────────────────────────────────────────────


def _count_per_base(fs, prefix: str, ext: str) -> Counter:
    """Compte le nombre de fichiers par SIREN (base_stem) dans un dossier S3."""
    stems = [Path(p).stem for p in fs.glob(f"{prefix}/*{ext}")]
    return Counter(_base_stem(s) for s in stems)


# ── Évaluation par lot ────────────────────────────────────────────────────────


def evaluate_dataset(threshold: float = 0.5, cell_delta: int = 0) -> pd.DataFrame:
    fs = get_s3_fs()
    all_results: list[dict] = []

    for method, pred_prefix in METHODS.items():
        if method == "marker_last_work":
            pairs = _list_pairs_from_correspondances(fs, pred_prefix)
            correspondances = _load_correspondances(fs)
            ann_counts = Counter({siren: len(paths) for siren, paths in correspondances.items()})
        else:
            pairs, ann_counts = _list_pairs(
                fs, pred_prefix, merge_page_breaks=method in METHODS_MERGING_PAGE_BREAKS
            )
        print(f"\n[{method}] {len(pairs)} paire(s) trouvée(s)")

        pred_counts = _count_per_base(fs, pred_prefix, ".csv")

        for name, ann_df, pred_path in pairs:
            base = _base_stem(name)
            try:
                pred_df = _load_csv(fs, pred_path)
                metrics = evaluate_pair(ann_df, pred_df, threshold=threshold, cell_delta=cell_delta)
                metrics.update({"fichier": name, "methode": method})
                print(
                    f"  {name:<30} "
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
            metrics["n_ann_tables"] = ann_counts.get(base, 0)
            metrics["n_pred_tables"] = pred_counts.get(base, 0)
            all_results.append(metrics)

    df = pd.DataFrame(all_results)
    _save_parquet(fs, df)
    _print_summary(df)
    return df


def _save_parquet(fs, df: pd.DataFrame) -> None:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    fs.pipe(S3_EVAL_OUTPUT, buf.getvalue())
    print(f"\nRésultats sauvegardés : s3://{S3_EVAL_OUTPUT}")


def _print_summary(df: pd.DataFrame) -> None:
    metric_cols = ["col_recovery", "row_recovery", "numeric_recovery", "total_extraction"]
    print("\n=== Moyennes par méthode ===")
    for method in df["methode"].dropna().unique():
        sub = df[df["methode"] == method]
        print(f"\n  {method}")
        for col in metric_cols:
            print(f"    {col:<25}: {sub[col].dropna().mean():.4f}")

        if "n_ann_tables" in sub.columns and "n_pred_tables" in sub.columns:
            siren_df = sub.copy()
            siren_df["_siren"] = siren_df["fichier"].apply(_base_stem)
            siren_df = siren_df.drop_duplicates("_siren")
            n_total = len(siren_df)
            n_match = (siren_df["n_pred_tables"] == siren_df["n_ann_tables"]).sum()
            print(f"    {'---':<25}")
            print(
                f"    {'table_count_accuracy':<25}: {n_match / n_total:.4f}  ({n_match}/{n_total} SIREN)"
            )
            print(f"    {'moy. tableaux annotés':<25}: {siren_df['n_ann_tables'].mean():.2f}")
            print(f"    {'moy. tableaux détectés':<25}: {siren_df['n_pred_tables'].mean():.2f}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Évaluation de l'extraction de tableaux")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="Seuil de similarité Levenshtein pour le matching (défaut : 0.5)",
    )
    parser.add_argument(
        "--cell-delta",
        type=int,
        default=0,
        help="Tolérance en colonnes (±) pour numeric_recovery et total_extraction (défaut : 0)",
    )
    args = parser.parse_args()
    evaluate_dataset(threshold=args.threshold, cell_delta=args.cell_delta)
