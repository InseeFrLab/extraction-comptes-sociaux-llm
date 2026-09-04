"""Tests de la classification des cellules qui alimente les chiffres du site.

`_norm_num` et `cell_status` décident de la frontière entre erreur de mise en forme et
erreur de transcription : une régression ici déplace silencieusement le chiffre publié
d'erreurs de lecture.
"""

import pytest
from build_data import _digits, _norm_num, cell_status


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("25 000", "25000"),  # espace séparateur de milliers
        ("25 000", "25000"),  # espace insécable
        ("2.08", "2.08"),
        ("2,08", "2.08"),  # virgule décimale
        ("(1 976)", "-1976"),  # parenthèses comptables
        ("- 30 000", "-30000"),  # signe détaché
        ("15,24 €", "15.24"),  # suffixe d'unité
        ("344 369 NOK", "344369"),
        # Le `%` porte une échelle : il doit être converti, pas supprimé comme une unité.
        ("70%", "0.7"),
        ("100 %", "1"),
        ("66,67%", "0.6667"),
        ("(12%)", "-0.12"),
        ("Capital social", "Capital social"),
    ],
)
def test_norm_num(value, expected):
    assert _norm_num(value) == expected


def test_norm_num_pourcentage_et_decimale_convergent():
    """Un pourcentage prédit et sa décimale attendue doivent être la même valeur.

    Sans cette égalité, `cell_status` classe la cellule en `ocr` : c'est ce qui gonflait
    de 64 cellules (marker) le compte d'erreurs de lecture publié.
    """
    assert _norm_num("0.7") == _norm_num("70%")
    assert _norm_num("1") == _norm_num("100%")


def test_digits_ne_garde_que_les_chiffres():
    assert _digits("(1 976) €") == "1976"
    assert _digits("n.a.") == ""


@pytest.mark.parametrize(
    ("expected", "got", "elsewhere", "status"),
    [
        ("", "", False, "vide-attendue"),  # exclue du dénominateur
        ("25 000", None, False, "non-appariee"),  # ligne ou colonne non appariée
        ("25 000", "25 000", False, "ok"),
        ("0,7", "70%", False, "ok"),  # même valeur, autre échelle d'écriture
        ("25 000", "", True, "deplacee"),  # cellule vide, valeur ailleurs
        ("25 000", "", False, "manquante"),  # cellule vide, valeur nulle part
        ("21 163", "22 163", False, "ocr"),  # un chiffre substitué
        ("469 453", "469 543", False, "ocr"),  # chiffres permutés
        ("150", "15", False, "ocr"),  # troncature en fin de cellule
        ("25 000", "31 500", True, "deplacee"),  # présente ailleurs → structure
        ("25 000", "999 999 999", False, "differente"),  # trop loin pour conclure
    ],
)
def test_cell_status(expected, got, elsewhere, status):
    assert cell_status(expected, got, elsewhere) == status


def test_structure_prime_sur_lecture():
    """Une valeur présente ailleurs est un défaut de placement, jamais de lecture.

    Deux valeurs numériquement voisines (décalage d'une colonne) passeraient le test de
    distance de Levenshtein : l'ordre des tests doit les classer `deplacee`.
    """
    assert cell_status("1 412 104", "1 412 103", elsewhere=True) == "deplacee"
    assert cell_status("1 412 104", "1 412 103", elsewhere=False) == "ocr"


def test_format_seul_quand_seuls_les_chiffres_concordent():
    """Chiffres identiques mais forme normalisée différente → mise en forme, pas lecture."""
    assert cell_status("1 000", "1 000 kg", elsewhere=False) == "format"
