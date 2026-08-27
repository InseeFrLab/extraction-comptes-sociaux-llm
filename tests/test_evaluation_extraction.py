"""Tests de la logique pure d'évaluation (normalisation et détection de numériques).

Ces fonctions conditionnent directement les métriques `numeric_recovery` et
`total_extraction` : une régression ici fausse silencieusement l'évaluation.
"""

import pytest
from evaluation_extraction import (
    _lev_similarity,
    _looks_numeric,
    _normalize_label,
    _normalize_numeric_str,
    _rank,
    _unify_dashes,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("25 000", "25000"),  # espace séparateur de milliers
        ("25 000", "25000"),  # espace insécable (fréquent dans les scans)
        ("100%", "1"),
        ("66,67%", "0.6667"),
        ("1234", "1234"),
        ("Capital social", "Capital social"),
    ],
)
def test_normalize_numeric_str(value, expected):
    assert _normalize_numeric_str(value) == expected


# ── écarts purement typographiques ───────────────────────────────────────────
#
# Trois familles où les deux côtés portent les mêmes chiffres et ne diffèrent que par
# l'écriture. Elles étaient comptées fausses : 38 cellules marker et 37 chandra.


@pytest.mark.parametrize(
    ("annote", "predit"),
    [
        ("2.08", "2,08"),  # séparateur décimal
        ("0.6667", "0,6667"),
        ("- 30 000", "-30 000"),  # signe négatif détaché du nombre
        ("-30 000", "\u221230000"),
        ("(1 976)", "-1 976"),  # parenthèses comptables
        ("(14)", "-14"),
        ("100%", "100,00%"),  # pourcentage, deux écritures
        ("0,7", "70%"),  # pourcentage contre décimale
        ("3342864", "3 342 864"),  # séparateur de milliers
    ],
)
def test_les_ecarts_d_ecriture_sont_absorbes(annote, predit):
    """Mêmes chiffres, écriture différente : la comparaison doit conclure à l'égalité."""
    assert _normalize_numeric_str(annote) == _normalize_numeric_str(predit)


@pytest.mark.parametrize(
    ("annote", "predit"),
    [
        ("1 976", "(1 976)"),  # signe opposé : ce n'est pas un écart d'écriture
        ("21 163", "22 163"),  # un chiffre mal lu
        ("6 817 282", "6 817 28"),  # troncature en fin de cellule
        ("469 453", "469 543"),  # deux chiffres permutés
        ("2,08", "20,8"),  # décimale mal placée
        ("10 640 226 396", "10 640 226 397"),  # onze chiffres, un seul diffère
    ],
)
def test_la_normalisation_ne_confond_pas_deux_valeurs_distinctes(annote, predit):
    """La normalisation absorbe l'écriture, jamais une différence de valeur.

    Le dernier cas garde la précision : un formatage en `.10g` arrondirait ces deux
    montants à la même chaîne, et créditerait une erreur de lecture.
    """
    assert _normalize_numeric_str(annote) != _normalize_numeric_str(predit)


def test_le_signe_n_est_interprete_que_devant_un_nombre():
    """« (en milliers d'euros) » n'est pas un négatif et ne doit pas gagner de signe."""
    assert _normalize_numeric_str("(en milliers d'euros)") == "(en milliers d'euros)"
    assert _normalize_numeric_str("- FILIALES") == "- FILIALES"


def test_les_zeros_non_significatifs_ne_distinguent_pas_deux_montants():
    assert _normalize_numeric_str("0042") == _normalize_numeric_str("42")
    assert _normalize_numeric_str("2,080") == _normalize_numeric_str("2,08")


@pytest.mark.parametrize(
    "value",
    ["(14)", "15,24 €", "344 369 NOK", "100,00%", "NC", "n/a", "-"],
)
def test_looks_numeric_tolere_unites_et_placeholders(value):
    assert _looks_numeric(value)


@pytest.mark.parametrize(
    "value",
    ["Capital social", "Total du bilan", "Exercice N-1 (12 mois)"],
)
def test_looks_numeric_refuse_les_entetes(value):
    assert not _looks_numeric(value)


@pytest.mark.parametrize(
    ("stem", "expected"),
    [("790256671_2", 2), ("TAB_301462602_10", 10), ("sans_rang", 0)],
)
def test_rank(stem, expected):
    """Le tri lexicographique placerait `_10` avant `_2`, l'appariement se ferait de travers."""
    assert _rank(stem) == expected


# ── équivalence des tirets ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("annote", "predit"),
    [
        ("-", "—"),  # cadratin : la marque d'absence la plus courante des moteurs
        ("-", "–"),  # demi-cadratin
        ("-", "−"),  # signe moins Unicode
        ("-30 000", "−30 000"),  # signe négatif devant un montant
        ("- 30 000", "— 30 000"),
    ],
)
def test_les_variantes_de_tiret_sont_equivalentes(annote, predit):
    """Une marque d'absence ou un signe négatif ne doit pas dépendre du glyphe.

    L'annotation écrit « - » là où les moteurs rendent « — » : 14 cellules de
    `TAB_552096281_2` étaient comptées « texte à la place du nombre » pour cette seule
    raison.
    """
    assert _normalize_numeric_str(annote) == _normalize_numeric_str(predit)
    assert _unify_dashes(annote.strip()) == _unify_dashes(predit.strip())


def test_le_tiret_demi_cadratin_ne_casse_plus_l_appariement_des_libelles():
    """« A - FILIALES » et « A – FILIALES » désignent la même colonne.

    Sans unification, la similarité tombe à 0,26 — sous le seuil de 0,5 — et la colonne
    n'est appariée à rien.
    """
    assert _lev_similarity("A - FILIALES DETENUES", "A – FILIALES DETENUES") == 1.0


def test_l_unification_ne_touche_que_les_tirets():
    """Le reste du texte est rendu tel quel, accents et casse compris."""
    assert _unify_dashes("Prêts et avances — Société") == "Prêts et avances - Société"
    assert _unify_dashes("Capital social") == "Capital social"


# ── graphie des libellés ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("annote", "predit"),
    [
        ("Capital", "CAPITAL"),
        ("Résultat dernier exercice clos", "RÉSULTAT DERNIER EXERCICE CLOS"),
        ("Quote part du capital", "QUOTE PART DU CAPITAL"),
        ("Chiffre d'affaires  HT", "CHIFFRE D'AFFAIRES HT"),
        ("Prêts et avances", "PRETS ET AVANCES"),
    ],
)
def test_la_graphie_ne_distingue_pas_deux_libelles(annote, predit):
    """Casse, accents et espaces ne changent pas la colonne désignée.

    Comparés tels quels, « Capital » et « CAPITAL » tombent à 0,14 de similarité, sous le
    seuil de 0,5 : sur `TAB_300221017_1`, dont chandra compose l'en-tête en capitales, les
    11 colonnes échouaient à s'apparier et les 350 cellules de données étaient comptées
    perdues alors que l'extraction est juste.
    """
    assert _lev_similarity(annote, predit) == 1.0


def test_la_normalisation_ne_confond_pas_deux_libelles_distincts():
    """Deux colonnes réellement différentes restent distinctes."""
    assert _lev_similarity("Valeur brute", "Valeur nette") < 0.9
    assert _lev_similarity("Capital", "Capitaux propres") < 0.6


def test_normalize_label_ne_touche_que_la_graphie():
    """Le libellé garde ses mots : seule sa graphie est canonisée."""
    assert _normalize_label("  RÉSULTAT   dernier  Exercice ") == "resultat dernier exercice"
    assert _normalize_label("A — FILIALES") == "a - filiales"
