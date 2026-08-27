"""Tests du corpus « tableaux historiques » : lecture de la référence et appariement.

Deux points de logique pure y décident du dénominateur des métriques :

- la référence est du HTML, et ses fusions doivent être développées comme celles d'un
  XLSX — valeur à la première cellule, continuations vides ;
- le radical d'un CSV prédit se sépare de son rang alors que le radical est lui-même une
  suite de chiffres (`crop_1970_1.csv`), là où `_base_stem` prendrait l'année pour le rang.
"""

import pandas as pd
import pytest
from corpus_historiques import key_from_annotation, key_from_image, stem
from evaluation_historiques import _split_rank, load_html


class FauxFS:
    """Système de fichiers minimal : rend le contenu passé au constructeur."""

    def __init__(self, contenu: str):
        self._contenu = contenu

    def open(self, path, mode="r", encoding=None):  # noqa: ARG002 - signature de s3fs
        import io

        return io.StringIO(self._contenu)


def test_load_html_developpe_les_fusions():
    """Un `rowspan` porte sa valeur à sa position d'origine, et rien aux suivantes."""
    html = """
    <table>
      <tr><td rowspan="2">Distributeur</td><td>En possédant</td><td>230</td></tr>
      <tr><td>N'en possédant pas</td><td>292</td></tr>
    </table>
    """
    df = load_html(FauxFS(html), "ground_truth_test.html")
    assert df.shape == (2, 3)
    assert list(df.iloc[0]) == ["Distributeur", "En possédant", "230"]
    # La ligne couverte reçoit une cellule vide, pas la valeur répétée : c'est la
    # convention des annotations Excel, et deux libellés identiques rendraient les lignes
    # indiscernables à l'appariement.
    assert list(df.iloc[1]) == ["", "N'en possédant pas", "292"]


def test_load_html_developpe_les_colspan():
    """Un `colspan` occupe ses colonnes, valeur à gauche et continuations vides."""
    html = (
        '<table><tr><td colspan="3">S.A.u en ha</td><td>- de 5</td></tr>'
        "<tr><td>a</td><td>b</td><td>c</td><td>1</td></tr></table>"
    )
    df = load_html(FauxFS(html), "ground_truth_test.html")
    assert list(df.iloc[0]) == ["S.A.u en ha", "", "", "- de 5"]


def test_load_html_retire_les_lignes_vides():
    """Une ligne entièrement vide quitte la grille, comme pour un XLSX."""
    html = (
        "<table><tr><td>a</td><td>1</td></tr><tr><td></td><td></td></tr>"
        "<tr><td>b</td><td>2</td></tr></table>"
    )
    df = load_html(FauxFS(html), "ground_truth_test.html")
    assert len(df) == 2
    assert list(df.index) == [0, 1]  # index réinitialisé
    assert isinstance(df, pd.DataFrame)


def test_load_html_sans_table():
    """Un fichier sans `<table>` est une erreur, pas une grille vide."""
    with pytest.raises(ValueError, match="aucun <table>"):
        load_html(FauxFS("<p>Table with 28 columns: …</p>"), "ground_truth_test.html")


@pytest.mark.parametrize(
    ("nom", "attendu"),
    [
        ("crop_1970_1", ("crop_1970", 1)),
        ("crop_1970_bis_1", ("crop_1970_bis", 1)),
        ("crop_1885_ter_12", ("crop_1885_ter", 12)),
        # Sans rang final, le nom est rendu tel quel.
        ("crop_bis", ("crop_bis", 0)),
    ],
)
def test_split_rank(nom, attendu):
    assert _split_rank(nom) == attendu


def test_split_rank_ne_prend_pas_lannee_pour_un_rang():
    """`crop_1970_1` : le rang est 1, pas 1970 — le radical porte lui-même des chiffres."""
    radical, rang = _split_rank("crop_1970_1")
    assert (radical, rang) == ("crop_1970", 1)


@pytest.mark.parametrize(
    ("chemin", "cle"),
    [
        ("bucket/pdf/tableaux historiques/crop_1970_bis.tiff", "1970_bis"),
        ("bucket/pdf/tableaux historiques/crop_1929.png", "1929"),
    ],
)
def test_key_from_image(chemin, cle):
    assert key_from_image(chemin) == cle


def test_key_from_annotation():
    chemin = "bucket/annotations/tableaux historiques/ground_truth_1885_ter.html"
    assert key_from_annotation(chemin) == "1885_ter"


def test_stem_ne_coupe_que_lextension():
    """Le radical porte des `_` et des chiffres : seule l'extension finale est retirée."""
    assert stem("a/b/crop_1885_ter.tiff") == "crop_1885_ter"
    assert stem("crop_1885_ter") == "crop_1885_ter"
