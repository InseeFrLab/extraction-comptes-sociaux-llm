"""Tests du regroupement des annotations séparées par une coupure de page, sans accès S3.

Ce regroupement ne concerne que la référence des moteurs qui voient le PDF entier — marker —
et seulement quand le moteur a effectivement rendu le tableau d'un seul tenant. La référence
de chandra, appelé page par page, n'est jamais touchée.

Deux formes de coupure, et deux seulement : la suite sans en-tête de colonnes, et la suite
qui réimprime exactement le même en-tête.
"""

import pandas as pd
import pytest
from evaluation import (
    METHODS_MERGING_PAGE_BREAKS,
    _has_column_header,
    _merge_page_breaks,
    _page_break_offset,
    detect_column_header_height,
)

ENTETE = ["Nom de la société", "% Intérêt", "Méthode"]


def df(rows: list[list[str]]) -> pd.DataFrame:
    return pd.DataFrame(rows, dtype=str).fillna("")


def rows(d: pd.DataFrame) -> list[list[str]]:
    return d.astype(str).values.tolist()


# ── cas 1 : la suite n'a pas d'en-tête ────────────────────────────────────────


def test_suite_sans_entete_est_regroupee():
    """Cas `_2511_431980275…` : la deuxième page reprend directement sur des données."""
    page1 = df([ENTETE, ["Alpha", "100,00", "IG"]])
    page2 = df([["Beta", "95,00", "IG"], ["Gamma", "50,00", "MEE"]])
    assert detect_column_header_height(page2) == 0
    (res,) = _merge_page_breaks([page1, page2], target=1)
    assert rows(res) == [
        ENTETE,
        ["Alpha", "100,00", "IG"],
        ["Beta", "95,00", "IG"],
        ["Gamma", "50,00", "MEE"],
    ]


# ── cas 2 : la suite réimprime exactement le même en-tête ─────────────────────


def test_suite_reimprimant_l_entete_est_regroupee_sans_le_dupliquer():
    """Cas `_0334_802617647_TAB` : seule la ligne d'en-tête est commune."""
    page1 = df([ENTETE, ["Alpha", "100,00", "IG"]])
    page2 = df([ENTETE, ["Beta", "95,00", "IG"]])
    (res,) = _merge_page_breaks([page1, page2], target=1)
    assert rows(res) == [ENTETE, ["Alpha", "100,00", "IG"], ["Beta", "95,00", "IG"]]


def test_entete_sur_deux_lignes_le_prefixe_commun_est_ecarte():
    """Cas `790256671` : l'en-tête tient sur deux lignes, les deux sont réimprimées.

    Le préfixe commun s'ajuste sans dépendre de la hauteur détectée, qui diffère parfois
    d'un fichier à l'autre alors que l'en-tête est le même.
    """
    tete = [["En k euros", "Quote-part", "Capital"], ["", "", ""]]
    page1 = df([*tete, ["Alpha", "100%", "15 672"]])
    page2 = df([*tete, ["Beta", "100%", "4 000"]])
    (res,) = _merge_page_breaks([page1, page2], target=1)
    assert rows(res) == [*tete, ["Alpha", "100%", "15 672"], ["Beta", "100%", "4 000"]]


def test_la_casse_et_les_espaces_ne_bloquent_pas_le_regroupement():
    page1 = df([ENTETE, ["Alpha", "100,00", "IG"]])
    page2 = df([["NOM  DE LA SOCIÉTÉ", "% intérêt ", "Méthode"], ["Beta", "95,00", "IG"]])
    assert len(_merge_page_breaks([page1, page2], target=1)) == 1


def test_suite_ouvrant_sur_un_intertitre_est_regroupee():
    """Cas `974_722049459_TAB` : la suite est la section « 2. Participations » du tableau.

    Un intertitre n'est pas un en-tête de colonnes — c'est du contenu, et il est conservé
    tel quel dans le tableau regroupé. Sans cette distinction, `detect_column_header_height`
    le compte comme un en-tête et la section passe pour un tableau autonome.
    """
    page1 = df([ENTETE, ["1. Filiales", "", ""], ["Alpha", "100,00", "IG"]])
    page2 = df([["2. Participations", "", ""], ["Beta", "49,00", "MEE"]])
    assert not _has_column_header(page2)
    (res,) = _merge_page_breaks([page1, page2], target=1)
    assert rows(res) == [
        ENTETE,
        ["1. Filiales", "", ""],
        ["Alpha", "100,00", "IG"],
        ["2. Participations", "", ""],
        ["Beta", "49,00", "MEE"],
    ]


def test_un_intertitre_seul_n_est_pas_un_entete():
    assert not _has_column_header(df([["1. Filiales", "", ""]]))
    assert _has_column_header(df([ENTETE, ["Alpha", "100,00", "IG"]]))
    assert _has_column_header(df([["1. Filiales", "", ""], ENTETE, ["Alpha", "1", "IG"]]))


# ── ce qui ne doit pas être regroupé ──────────────────────────────────────────


def test_entetes_differents_ne_sont_pas_regroupes():
    """Deux tableaux distincts : la sous-détection du moteur reste visible."""
    page1 = df([ENTETE, ["Alpha", "100,00", "IG"]])
    page2 = df([["Dénomination", "Capital", "Résultat"], ["Beta", "1 000", "50"]])
    assert len(_merge_page_breaks([page1, page2], target=1)) == 2


def test_largeurs_differentes_ne_sont_pas_regroupees():
    """Cas `411373525` : ses tableaux passent de 2 à 4 puis 12 colonnes."""
    page1 = df([["Provisions", "5235"], ["Autre", "12"]])
    page2 = df([["", "Résultat", "Impôt", "Net"], ["x", "1", "2", "3"]])
    assert len(_merge_page_breaks([page1, page2], target=1)) == 2


def test_coupure_en_largeur_n_est_pas_regroupee():
    """Cas `TAB_676250111_atypique` : mêmes lignes, d'autres colonnes sur la page suivante.

    Concaténer par lignes serait faux — ce sont les colonnes qui se prolongent. La largeur
    suffit à l'écarter.
    """
    page1 = df([["Sociétés", "Monnaies", "Capital"], ["Frefin", "HKD", "5 003"]])
    page2 = df([["Sociétés", "Valeur brute", "Prêts", "Cautions"], ["Frefin", "31", "-", "-"]])
    assert len(_merge_page_breaks([page1, page2], target=1)) == 2


def test_aucun_regroupement_si_le_moteur_ne_produit_pas_moins():
    """Cas `TAB_682024096` : la signature est là, mais marker rend bien deux tableaux."""
    page1 = df([ENTETE, ["Alpha", "100,00", "IG"]])
    page2 = df([ENTETE, ["Beta", "95,00", "IG"]])
    anns = [page1, page2]
    assert _merge_page_breaks(anns, target=2) is anns
    assert _merge_page_breaks(anns, target=3) is anns


def test_regroupement_s_arrete_a_la_cible():
    """Trois annotations, deux tableaux prédits : un seul regroupement est nécessaire."""
    pages = [
        df([ENTETE, ["Alpha", "1", "IG"]]),
        df([ENTETE, ["Beta", "2", "IG"]]),
        df([ENTETE, ["Gamma", "3", "IG"]]),
    ]
    res = _merge_page_breaks(pages, target=2)
    assert len(res) == 2
    assert rows(res[0]) == [ENTETE, ["Alpha", "1", "IG"], ["Beta", "2", "IG"]]


# ── le périmètre : marker seulement ───────────────────────────────────────────


def test_seul_marker_regroupe_ses_annotations():
    """Chandra est appelé page par page : sa référence doit rester découpée."""
    assert "marker" in METHODS_MERGING_PAGE_BREAKS
    assert "chandra" not in METHODS_MERGING_PAGE_BREAKS


# ── cas dégénérés ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("gauche", "droite"),
    [(df([]), df([ENTETE])), (df([ENTETE]), df([])), (df([ENTETE]), df([["a", "b"]]))],
)
def test_offset_refuse_les_cas_degeneres(gauche, droite):
    assert _page_break_offset(gauche, droite) is None
