#!/usr/bin/env python3
"""
Évaluation de l'extraction sur le corpus « tableaux historiques ».

Mêmes métriques que `evaluation_extraction.py`, dont tout le calcul est importé : ce
script ne fait que brancher un corpus dont **la référence et l'appariement** diffèrent.

  Annotations : s3://projet-extraction-tableaux/annotations/tableaux historiques/*.html
  Prédictions : s3://projet-extraction-tableaux/tableaux_historiques/output_csv/{moteur}/*.csv
                (une entrée par condition de `corpus_historiques.MOTEURS`)
  Résultats   : s3://projet-extraction-tableaux/tableaux_historiques/eval/evaluation.parquet

Trois écarts avec le corpus des comptes sociaux, et trois seulement :

- **la référence est du HTML**, pas un XLSX. Elle est lue par le parseur de
  `json_to_csv.py`, celui-là même qui lit la sortie des moteurs : `rowspan` et `colspan`
  y sont développés selon la convention des annotations Excel — la valeur à la première
  cellule de la fusion, les cellules de continuation vides ;
- **l'appariement se fait par nom de fichier**, `ground_truth_1970_bis.html` ↔
  `crop_1970_bis_{rang}.csv` : il n'y a ni SIREN ni parquet de correspondances ;
- **il n'y a pas de coupure de page à recoller** : chaque image porte un tableau entier,
  déjà rogné. Un moteur qui en rend plusieurs a sur-découpé, et le rang 1 seul est
  comparé — les rangs suivants sont signalés, non silencieusement écartés.

Usage :
    uv run evaluation_historiques.py [--threshold 0.5 --cell-delta 0]
"""

import argparse
import io
import re
from collections import Counter

import evaluation_extraction as E
import pandas as pd
from corpus_historiques import (
    MOTEURS,
    S3_ANNOTATIONS,
    S3_EVAL_OUTPUT,
    key_from_annotation,
    key_from_image,
    stem,
)
from extraction_common.s3 import get_s3_fs
from json_to_csv import _parse_html_tables

METHODS: dict[str, str] = {nom: cfg["csv"] for nom, cfg in MOTEURS.items()}

# `{clé}_{rang}.csv`, où la clé porte elle-même des chiffres et des `_` (`1970_bis`).
_RANK_RE = re.compile(r"^(.*)_(\d+)$")


def load_html(fs, path: str) -> pd.DataFrame:
    """Charge une annotation HTML en grille de chaînes.

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
        # Une annotation porte un tableau : plusieurs signalent une référence à revoir,
        # et la mesure ne doit pas trancher à sa place laquelle compte.
        print(f"    [WARN] {stem(path)} : {len(tables)} <table> dans l'annotation, le 1er est pris")
    df = pd.DataFrame(tables[0], dtype=str).fillna("")
    mask = df.apply(lambda row: row.str.strip().eq("").all(), axis=1)
    return df[~mask].reset_index(drop=True)


def _split_rank(nom: str) -> tuple[str, int]:
    """Sépare le radical d'un CSV prédit de son rang.

    `E._base_stem` ne convient pas ici : sur un radical qui est lui-même une année
    (`crop_1970`), il prendrait `1970` pour le rang.

    Args:
        nom: nom du CSV sans extension, de la forme `{radical}_{rang}`.

    Returns:
        Le couple (radical, rang) ; (nom, 0) si le nom ne porte pas de rang.
    """
    m = _RANK_RE.match(nom)
    return (m.group(1), int(m.group(2))) if m else (nom, 0)


def list_pairs(fs, pred_prefix: str) -> tuple[list[tuple[str, pd.DataFrame, str]], Counter]:
    """Apparie annotations HTML et CSV prédits par clé de tableau.

    Args:
        fs: système de fichiers S3.
        pred_prefix: dossier des CSV prédits.

    Returns:
        La liste des (nom de la prédiction, annotation chargée, chemin de la prédiction),
        et le nombre de tableaux prédits par clé — un corpus où chaque image porte un
        tableau entier, ce compte dit la sur-segmentation du moteur.
    """
    annotations = {key_from_annotation(p): p for p in fs.glob(f"{S3_ANNOTATIONS}/*.html")}

    predictions: dict[str, dict[int, str]] = {}
    for path in fs.glob(f"{pred_prefix}/*.csv"):
        radical, rang = _split_rank(stem(path))
        predictions.setdefault(key_from_image(radical), {})[rang] = path

    pairs: list[tuple[str, pd.DataFrame, str]] = []
    pred_counts: Counter = Counter()
    sans_prediction: list[str] = []
    sur_decoupes: list[str] = []
    for cle in sorted(annotations):
        rangs = predictions.get(cle, {})
        pred_counts[cle] = len(rangs)
        if 1 not in rangs:
            sans_prediction.append(cle)
            continue
        if len(rangs) > 1:
            sur_decoupes.append(f"{cle} ({len(rangs)})")
        pairs.append((cle, load_html(fs, annotations[cle]), rangs[1]))

    orphelines = sorted(set(predictions) - set(annotations))
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


def evaluate_dataset(threshold: float = 0.5, cell_delta: int = 0) -> pd.DataFrame:
    """Évalue tout le corpus et dépose le parquet des métriques sur S3.

    Args:
        threshold: similarité minimale pour apparier deux en-têtes.
        cell_delta: tolérance en colonnes (±) pour `total_extraction`.

    Returns:
        Le tableau des métriques, une ligne par couple tableau × méthode.
    """
    fs = get_s3_fs()
    all_results: list[dict] = []

    for method, pred_prefix in METHODS.items():
        pairs, pred_counts = list_pairs(fs, pred_prefix)
        print(f"\n[{method}] {len(pairs)} paire(s) trouvée(s)")

        for cle, ann_df, pred_path in pairs:
            try:
                pred_df = E._load_csv(fs, pred_path)
                metrics = E.evaluate_pair(
                    ann_df, pred_df, threshold=threshold, cell_delta=cell_delta
                )
                metrics.update({"fichier": cle, "methode": method})
                print(
                    f"  {cle:<16} "
                    f"col={metrics['col_recovery']:.3f}  "
                    f"row={metrics['row_recovery']:.3f}  "
                    f"num={metrics['numeric_recovery']:.3f}  "
                    f"total={metrics['total_extraction']}"
                )
            except Exception as e:
                print(f"  [ERR] {cle}: {e}")
                metrics = {
                    "fichier": cle,
                    "methode": method,
                    "col_recovery": None,
                    "row_recovery": None,
                    "numeric_recovery": None,
                    "total_extraction": None,
                }
            # Une image porte un tableau : la référence vaut 1 pour toutes.
            metrics["n_ann_tables"] = 1
            metrics["n_pred_tables"] = pred_counts.get(cle, 0)
            all_results.append(metrics)

    df = pd.DataFrame(all_results)

    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    fs.pipe(S3_EVAL_OUTPUT, buf.getvalue())
    print(f"\nRésultats sauvegardés : s3://{S3_EVAL_OUTPUT}")

    _print_summary(df)
    return df


def _print_summary(df: pd.DataFrame) -> None:
    """Imprime les moyennes par méthode, dans la forme de `evaluation_extraction`."""
    print("\n=== Moyennes par méthode ===")
    for method in df["methode"].dropna().unique():
        sub = df[df["methode"] == method]
        print(f"\n  {method}  (n={len(sub)})")
        for col in ["col_recovery", "row_recovery", "numeric_recovery", "total_extraction"]:
            print(f"    {col:<25}: {sub[col].dropna().mean():.4f}")
        cellule = sub["n_recovered_numeric"].sum() / sub["n_numeric_cells"].sum()
        print(f"    {'récup. par cellule':<25}: {cellule:.4f}")
        n_match = (sub["n_pred_tables"] == 1).sum()
        print(f"    {'table_count_accuracy':<25}: {n_match / len(sub):.4f}  ({n_match}/{len(sub)})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
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
        help="Tolérance en colonnes (±) pour total_extraction (défaut : 0)",
    )
    args = parser.parse_args()
    evaluate_dataset(threshold=args.threshold, cell_delta=args.cell_delta)
