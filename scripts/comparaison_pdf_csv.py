#!/usr/bin/env python3
"""
Appariement des PDFs de tableaux et des annotations XLSX de référence, par SIREN.

Sources et sortie sont celles déclarées par `config/comptes-sociaux.yaml`, section
`sources` : les PDF du corpus, les annotations XLSX, et le parquet de correspondances
(consommé ensuite par extraction_pdf_via_api.py, evaluation.py et les aperçus du site).

Ce que ce parquet fixe — un SIREN, plusieurs tableaux, un seul PDF — est expliqué dans
[README.md](README.md).

Usage :
    uv run comparaison_pdf_csv.py
"""

import re
from pathlib import Path

import pandas as pd
from extraction_common.s3 import get_s3_fs

import config

CONFIG = config.charger("comptes-sociaux")

PATH_XLSX = f"{CONFIG.annotations}/*.xlsx"
PATH_PDF = f"{CONFIG.sources['pdf']}/*.pdf"
OUTPUT_PATH = CONFIG.sources["correspondances"]

# Suffixe _1, _2… ajouté quand un SIREN porte plusieurs tableaux annotés.
_SUFFIXE_RE = re.compile(r"_\d+$")


def siren_base(stem: str) -> str:
    """Retire le suffixe numérique d'un nom de fichier pour retrouver le SIREN.

    Args:
        stem: nom de fichier sans extension (ex. "123456789_2").

    Returns:
        Le SIREN sans suffixe (ex. "123456789").
    """
    return _SUFFIXE_RE.sub("", stem)


def build_correspondances(
    xlsx_files: list[str], pdf_files: list[str]
) -> dict[str, dict[str, str | list[str] | None]]:
    """Apparie annotations et PDFs par SIREN.

    Un SIREN peut porter plusieurs annotations (suffixes _1, _2…) mais un seul PDF.
    Les clés renvoyées par `fs.glob` sont sans schéma : on les repréfixe en `s3://`.

    Args:
        xlsx_files: clés S3 des annotations XLSX.
        pdf_files: clés S3 des PDFs.

    Returns:
        {siren: {"pdf": chemin | None, "xlsx": [chemins] | None}}, trié par SIREN,
        les listes de xlsx étant triées pour un ordre de sortie stable.
    """
    xlsx_par_siren: dict[str, list[str]] = {}
    for f in xlsx_files:
        xlsx_par_siren.setdefault(siren_base(Path(f).stem), []).append(f"s3://{f}")
    for chemins in xlsx_par_siren.values():
        chemins.sort()

    pdf_par_siren = {Path(f).stem: f"s3://{f}" for f in pdf_files}

    return {
        siren: {"pdf": pdf_par_siren.get(siren), "xlsx": xlsx_par_siren.get(siren)}
        for siren in sorted(set(xlsx_par_siren) | set(pdf_par_siren))
    }


def to_dataframe(correspondances: dict) -> pd.DataFrame:
    """Met les correspondances à plat, une ligne par SIREN.

    Args:
        correspondances: sortie de `build_correspondances`.

    Returns:
        DataFrame à trois colonnes : siren, pdf, xlsx.
    """
    return pd.DataFrame(
        [
            {"siren": siren, "pdf": vals["pdf"], "xlsx": vals["xlsx"]}
            for siren, vals in correspondances.items()
        ]
    )


def main() -> None:
    """Construit les correspondances depuis S3 et écrit le parquet de sortie."""
    fs = get_s3_fs()
    correspondances = build_correspondances(fs.glob(PATH_XLSX), fs.glob(PATH_PDF))
    df = to_dataframe(correspondances)

    nb_pdf = sum(1 for v in correspondances.values() if v["pdf"] is not None)
    nb_xlsx = sum(len(v["xlsx"]) for v in correspondances.values() if v["xlsx"] is not None)
    print(f"Nombre de siren détectés : {len(df)}")
    print(f"Nombre de pdf détectés : {nb_pdf}")
    print(f"Nombre de xlsx d'annotations détectés : {nb_xlsx}")

    with fs.open(OUTPUT_PATH, "wb") as f:
        df.to_parquet(f, index=False)


if __name__ == "__main__":
    main()
