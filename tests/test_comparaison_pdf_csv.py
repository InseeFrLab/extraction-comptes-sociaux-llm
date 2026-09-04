"""Tests de l'appariement PDF ↔ annotations XLSX, sans aucun appel réseau.

Les fonctions testées reçoivent directement les listes de clés S3 : l'accès à S3
reste confiné à `main()`. Le simple import de ce module vérifie donc aussi qu'aucun
appel S3 n'a lieu au chargement de `comparaison_pdf_csv`.
"""

import pytest
from comparaison_pdf_csv import build_correspondances, siren_base, to_dataframe

BUCKET = "projet-extraction-tableaux"
PDF = f"{BUCKET}/pdf/tableaux_representatifs"
XLSX = f"{BUCKET}/annotations/clean"


@pytest.mark.parametrize(
    ("stem", "attendu"),
    [
        ("123456789", "123456789"),  # pas de suffixe
        ("123456789_1", "123456789"),  # suffixe simple
        ("123456789_12", "123456789"),  # suffixe à deux chiffres
        ("123456789_1_2", "123456789_1"),  # seul le dernier suffixe est retiré
        ("bilan_2023", "bilan"),  # le suffixe est numérique, pas forcément un indice
    ],
)
def test_siren_base(stem, attendu):
    assert siren_base(stem) == attendu


def test_apparie_pdf_et_annotations():
    correspondances = build_correspondances(
        xlsx_files=[f"{XLSX}/123456789.xlsx"],
        pdf_files=[f"{PDF}/123456789.pdf"],
    )
    assert correspondances == {
        "123456789": {
            "pdf": f"s3://{PDF}/123456789.pdf",
            "xlsx": [f"s3://{XLSX}/123456789.xlsx"],
        }
    }


def test_regroupe_les_annotations_multiples_et_les_trie():
    """Un SIREN peut porter plusieurs tableaux annotés (_1, _2…) pour un seul PDF."""
    correspondances = build_correspondances(
        xlsx_files=[f"{XLSX}/123456789_2.xlsx", f"{XLSX}/123456789_1.xlsx"],
        pdf_files=[f"{PDF}/123456789.pdf"],
    )
    assert correspondances["123456789"]["xlsx"] == [
        f"s3://{XLSX}/123456789_1.xlsx",
        f"s3://{XLSX}/123456789_2.xlsx",
    ]


def test_pdf_sans_annotation_et_annotation_sans_pdf():
    """Les deux orphelins doivent apparaître, avec None du côté manquant."""
    correspondances = build_correspondances(
        xlsx_files=[f"{XLSX}/111111111.xlsx"],
        pdf_files=[f"{PDF}/222222222.pdf"],
    )
    assert correspondances["111111111"]["pdf"] is None
    assert correspondances["222222222"]["xlsx"] is None


def test_sirens_tries():
    correspondances = build_correspondances(
        xlsx_files=[f"{XLSX}/333.xlsx", f"{XLSX}/111.xlsx", f"{XLSX}/222.xlsx"],
        pdf_files=[],
    )
    assert list(correspondances) == ["111", "222", "333"]


def test_entrees_vides():
    assert build_correspondances([], []) == {}


def test_to_dataframe():
    df = to_dataframe(
        build_correspondances(
            xlsx_files=[f"{XLSX}/111111111.xlsx"],
            pdf_files=[f"{PDF}/111111111.pdf", f"{PDF}/222222222.pdf"],
        )
    )
    assert list(df.columns) == ["siren", "pdf", "xlsx"]
    assert len(df) == 2
    assert df.loc[df["siren"] == "222222222", "xlsx"].isna().all()
