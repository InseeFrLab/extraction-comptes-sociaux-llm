"""Tests du parseur HTML de `json_to_csv`, sans aucun accès S3.

Le parseur produit la grille sur laquelle repose toute la chaîne de mesure : un
décalage d'une colonne introduit ici se propage à l'appariement des en-têtes puis aux
métriques. Les cas couverts sont ceux réellement rencontrés dans les tableaux de
filiales et participations du corpus : en-têtes sur deux niveaux, libellé de société
fusionné sur plusieurs lignes, cellules fusionnées dans les deux sens.
"""

import pytest
from json_to_csv import (
    ChandraTableExtractor,
    MarkerTableExtractor,
    _normalize_chandra_table,
    _normalize_grid,
    _parse_html_tables,
    _rectangularize,
    _stale_csv_paths,
)


def parse_one(html: str) -> list[list[str]]:
    """Parse un HTML contenant un seul tableau et retourne sa grille."""
    tables = _parse_html_tables(html)
    assert len(tables) == 1, f"attendu 1 tableau, obtenu {len(tables)}"
    return tables[0]


# ── rowspan ───────────────────────────────────────────────────────────────────


def test_rowspan_reserve_la_position_sur_les_lignes_couvertes():
    """Une cellule `rowspan=2` occupe sa colonne sur les deux lignes.

    Sans report, la deuxième ligne ne contient que ['1 000', '250'] : le montant se
    retrouve dans la colonne du libellé et tout le reste glisse d'un cran à gauche.

    La position de continuation reçoit une chaîne vide, pas la valeur répétée : c'est la
    convention des annotations de référence (une fusion Excel n'écrit la valeur que dans
    sa première cellule) et celle déjà appliquée à `colspan`.
    """
    html = """
    <table><tbody>
      <tr><td rowspan="2">Entité A</td><td>2022</td><td>500</td></tr>
      <tr><td>1 000</td><td>250</td></tr>
    </tbody></table>
    """
    assert parse_one(html) == [
        ["Entité A", "2022", "500"],
        ["", "1 000", "250"],
    ]


def test_rowspan_au_milieu_de_la_ligne():
    """Une fusion verticale hors première colonne décale aussi ce qui la suit."""
    html = """
    <table><tbody>
      <tr><td>Entité A</td><td rowspan="2">EUR</td><td>500</td></tr>
      <tr><td>Entité B</td><td>250</td></tr>
    </tbody></table>
    """
    assert parse_one(html) == [
        ["Entité A", "EUR", "500"],
        ["Entité B", "", "250"],
    ]


def test_rowspan_expire_apres_le_nombre_de_lignes_annonce():
    """Le report cesse exactement après les lignes couvertes, sans déborder."""
    html = """
    <table><tbody>
      <tr><td rowspan="2">Groupe</td><td>a</td></tr>
      <tr><td>b</td></tr>
      <tr><td>Entité C</td><td>c</td></tr>
    </tbody></table>
    """
    assert parse_one(html) == [
        ["Groupe", "a"],
        ["", "b"],
        ["Entité C", "c"],
    ]


def test_rowspan_de_trois_lignes():
    html = """
    <table><tbody>
      <tr><td rowspan="3">Total</td><td>1</td></tr>
      <tr><td>2</td></tr>
      <tr><td>3</td></tr>
    </tbody></table>
    """
    assert parse_one(html) == [["Total", "1"], ["", "2"], ["", "3"]]


def test_rowspan_au_dela_de_la_derniere_cellule_ecrite():
    """Une position reportée peut suivre la dernière cellule du HTML de la ligne.

    La ligne doit être complétée jusqu'à cette position, sinon la colonne reportée se
    décale sur toutes les lignes suivantes.
    """
    html = """
    <table><tbody>
      <tr><td>a</td><td>b</td><td rowspan="2">note</td></tr>
      <tr><td>c</td></tr>
    </tbody></table>
    """
    assert parse_one(html) == [
        ["a", "b", "note"],
        ["c", "", ""],
    ]


def test_rowspan_et_colspan_combines():
    """Une cellule fusionnée dans les deux sens occupe un bloc rectangulaire."""
    html = """
    <table><tbody>
      <tr><td colspan="2" rowspan="2">Capitaux propres</td><td>2022</td></tr>
      <tr><td>2021</td></tr>
    </tbody></table>
    """
    assert parse_one(html) == [
        ["Capitaux propres", "", "2022"],
        ["", "", "2021"],
    ]


def test_entete_sur_deux_niveaux_reste_aligne():
    """Cas réel : « Brute | Nette » sous « Valeur d'inventaire », le reste en rowspan.

    C'est la structure des tableaux de filiales du corpus. Les sous-en-têtes doivent
    tomber sous la colonne qu'ils qualifient, et les montants sous leur sous-en-tête.
    """
    html = """
    <table><tbody>
      <tr>
        <th rowspan="2">SOCIETES</th>
        <th rowspan="2">Capital</th>
        <th colspan="2">Valeur d'inventaire</th>
      </tr>
      <tr><th>Brute</th><th>Nette</th></tr>
      <tr><td>Entité A</td><td>877 668</td><td>1 734 110</td><td>1 734 110</td></tr>
    </tbody></table>
    """
    grille = parse_one(html)
    assert grille == [
        ["SOCIETES", "Capital", "Valeur d'inventaire", ""],
        ["", "", "Brute", "Nette"],
        ["Entité A", "877 668", "1 734 110", "1 734 110"],
    ]
    # L'invariant qui compte : toutes les lignes ont la même largeur, donc les colonnes
    # de données sont alignées avec leur sous-en-tête.
    assert len({len(ligne) for ligne in grille}) == 1


# ── colspan (comportement préexistant, protégé contre les régressions) ────────


@pytest.mark.parametrize(
    ("attribut", "largeur_attendue"),
    [
        ('colspan="1"', 2),
        ('colspan="3"', 4),
        ("", 2),
        ('colspan="0"', 2),  # valeur invalide ramenée à 1
        ('colspan="abc"', 2),  # idem
    ],
)
def test_colspan_developpe_en_cellules_vides(attribut, largeur_attendue):
    html = f"<table><tbody><tr><td {attribut}>x</td><td>y</td></tr></tbody></table>"
    ligne = parse_one(html)[0]
    assert len(ligne) == largeur_attendue
    assert ligne[0] == "x"
    assert ligne[-1] == "y"


def test_rowspan_invalide_ne_reporte_rien():
    html = """
    <table><tbody>
      <tr><td rowspan="0">a</td><td>b</td></tr>
      <tr><td>c</td><td>d</td></tr>
    </tbody></table>
    """
    assert parse_one(html) == [["a", "b"], ["c", "d"]]


# ── autres comportements du parseur ──────────────────────────────────────────


def test_br_devient_une_espace_dans_la_cellule():
    html = "<table><tbody><tr><td>Entité A<br/>Paris</td></tr></tbody></table>"
    assert parse_one(html) == [["Entité A Paris"]]


# ── enregistrements empilés sur deux lignes physiques ────────────────────────


def test_deux_valeurs_empilees_donnent_deux_lignes():
    """Un enregistrement composé sur deux lignes ressort sur deux lignes.

    Cas de `_0334_394331946_TAB` : l'en-tête est sur deux niveaux — « Dénomination /
    Siège Social », « Capital / Capitaux Propres » — et chaque société occupe deux lignes
    sans filet entre elles. Le moteur rend une seule `<tr>` dont chaque cellule porte ses
    deux valeurs séparées par un `<br>`. Aplaties en une espace, les deux montants se
    retrouvent soudés dans la même cellule et aucun ne s'apparie : la récupération
    numérique du fichier tombe à 0,044 au lieu de 1,000.
    """
    html = (
        "<table><tbody><tr>"
        "<td>MDHA<br/>6 West 18th Street, New York 10011</td>"
        "<td>14 498 145<br/>12 789 320</td>"
        "<td>10 601 766<br/>10 601 766</td>"
        "</tr></tbody></table>"
    )
    assert parse_one(html) == [
        ["MDHA", "14 498 145", "10 601 766"],
        ["6 West 18th Street, New York 10011", "12 789 320", "10 601 766"],
    ]


def test_cellule_sans_coupure_reste_sur_la_premiere_ligne():
    """Une colonne renseignée une seule fois par enregistrement n'est pas dupliquée.

    « Prêts, avances » ne porte qu'une valeur là où les autres colonnes en portent deux :
    elle appartient à la première ligne, et la seconde reste vide — c'est la convention
    de l'annotation.
    """
    html = (
        "<table><tbody><tr>"
        "<td>MDHA<br/>New York</td>"
        "<td>14 498 145<br/>12 789 320</td>"
        "<td>16 843 046</td>"
        "<td>18 344 543<br/>1 387 889</td>"
        "</tr></tbody></table>"
    )
    assert parse_one(html) == [
        ["MDHA", "14 498 145", "16 843 046", "18 344 543"],
        ["New York", "12 789 320", "", "1 387 889"],
    ]


def test_libelle_replie_ne_coupe_pas_la_ligne():
    """Un libellé replié en fin de ligne reste une seule ligne.

    Cas de `411373525` : la raison sociale et l'adresse tiennent sur plusieurs lignes
    dans leur cellule, mais elles seules portent un `<br>`. Couper ici scinderait une
    ligne de données parfaitement formée.
    """
    html = (
        "<table><tbody><tr>"
        "<td>VALLOUREC TUBES<br/>France<br/>27, avenue du Général-Leclerc</td>"
        "<td>918 466</td><td>253 625</td>"
        "</tr></tbody></table>"
    )
    assert parse_one(html) == [
        ["VALLOUREC TUBES France 27, avenue du Général-Leclerc", "918 466", "253 625"]
    ]


def test_deux_libelles_coupes_sans_nombres_ne_coupent_pas():
    """Deux cellules repliées ne suffisent pas : il faut des nombres empilés.

    Sans cette exigence, un en-tête dont deux libellés se replient serait scindé en deux
    lignes d'en-tête, et toute la grille glisserait d'un cran.
    """
    html = (
        "<table><tbody><tr>"
        "<th>Filiales et<br/>participations</th>"
        "<th>Capital<br/>social</th>"
        "<th>Résultat</th>"
        "</tr></tbody></table>"
    )
    assert parse_one(html) == [["Filiales et participations", "Capital social", "Résultat"]]


def test_nombres_empiles_dans_une_seule_cellule_ne_coupent_pas():
    """Une seule cellule empilant deux nombres ne fait pas un enregistrement double."""
    html = (
        "<table><tbody><tr>"
        "<td>Entité A</td><td>1 000<br/>2 000</td><td>500</td>"
        "</tr></tbody></table>"
    )
    assert parse_one(html) == [["Entité A", "1 000 2 000", "500"]]


def test_lignes_vides_ignorees():
    html = "<table><tbody><tr></tr><tr><td>a</td></tr></tbody></table>"
    assert parse_one(html) == [["a"]]


def test_plusieurs_tableaux_sont_independants():
    """Un `rowspan` ouvert dans un tableau ne doit pas fuir dans le suivant."""
    html = """
    <table><tbody><tr><td rowspan="5">A</td><td>1</td></tr></tbody></table>
    <table><tbody><tr><td>B</td><td>2</td></tr></tbody></table>
    """
    tables = _parse_html_tables(html)
    assert tables == [[["A", "1"]], [["B", "2"]]]


def test_marker_extractor_traverse_les_blocs_imbriques():
    data = {
        "children": [
            {
                "block_type": "TableGroup",
                "html": (
                    '<table><tbody><tr><td rowspan="2">Entité A</td><td>1</td></tr>'
                    "<tr><td>2</td></tr></tbody></table>"
                ),
                "children": [],
            }
        ]
    }
    assert MarkerTableExtractor().extract(data) == [[["Entité A", "1"], ["", "2"]]]


# ── rectangularité de la grille produite ─────────────────────────────────────


def test_ligne_courte_completee_a_droite():
    """Une ligne comptant moins de cellules que les autres est complétée sur place.

    Sans cela, la grille sort irrégulière et c'est `_load_csv`, côté évaluation, qui
    décide où vont les cellules manquantes — alors qu'il ne voit qu'un CSV et n'a aucun
    moyen de savoir d'où elles viennent.
    """
    html = """
    <table><tbody>
      <tr><td>a</td><td>b</td><td>c</td></tr>
      <tr><td>1</td></tr>
    </tbody></table>
    """
    assert parse_one(html) == [
        ["a", "b", "c"],
        ["1", "", ""],
    ]


def test_toutes_les_grilles_produites_sont_rectangulaires():
    """L'invariant de sortie du parseur, sur un tableau mêlant fusions et lignes courtes."""
    html = """
    <table><tbody>
      <tr><th rowspan="2">SOCIETES</th><th colspan="2">Valeur d'inventaire</th><th>Note</th></tr>
      <tr><th>Brute</th><th>Nette</th></tr>
      <tr><td>Entité A</td><td>1</td></tr>
    </tbody></table>
    """
    grille = parse_one(html)
    assert len({len(ligne) for ligne in grille}) == 1


def test_rectangularize_laisse_une_grille_deja_reguliere_inchangee():
    grille = [["a", "b"], ["c", "d"]]
    assert _rectangularize(grille) == grille
    assert _rectangularize([]) == []


# ── sous-lignes d'en-tête : repositionnement sous le libellé détaillé ─────────


def test_sous_ligne_entete_replacee_sous_le_libelle_couvrant():
    """Cas réel chandra : « Brute | Nette » sous « Valeur d'inventaire des titres ».

    La ligne parente compte 11 libellés pour 12 colonnes : un libellé en couvre deux et
    sa cellule de continuation n'a pas été émise. Complétée à droite, la sous-ligne
    tomberait en colonnes 0-1, sous « SOCIETES » et « Capital ».
    """
    table = [
        ["SOCIETES", "Capital", "Valeur d'inventaire", "Dividendes"],
        ["Brute", "Nette"],
        ["Entité A", "877 668", "1 734 110", "1 700 000", "12"],
    ]
    assert _normalize_chandra_table(table) == [
        ["SOCIETES", "Capital", "Valeur d'inventaire", "", "Dividendes"],
        ["", "", "Brute", "Nette", ""],
        ["Entité A", "877 668", "1 734 110", "1 700 000", "12"],
    ]


def test_sous_ligne_entete_de_trois_colonnes():
    """Le décalage vaut k-1, quel que soit le nombre de sous-colonnes."""
    table = [
        ["Filiales", "Valeurs comptables", "Quote-part"],
        ["Brute", "Nette", "Fair Value"],
        ["Entité A", "1", "2", "3", "4"],
    ]
    assert _normalize_chandra_table(table) == [
        ["Filiales", "Valeurs comptables", "", "", "Quote-part"],
        ["", "Brute", "Nette", "Fair Value", ""],
        ["Entité A", "1", "2", "3", "4"],
    ]


def test_sous_ligne_entete_libelle_couvrant_ambigu_reste_a_droite():
    """Deux libellés candidats : impossible de trancher, le repli à droite s'applique.

    Mieux vaut le comportement connu qu'un placement arbitraire : ici « Valeur brute »
    et « Valeur nette » sont déjà deux colonnes distinctes, et rien ne dit laquelle la
    sous-ligne détaillerait.
    """
    table = [
        ["Filiales", "Valeur brute", "Valeur nette", "Dividendes"],
        ["a", "b"],
        ["Entité A", "1", "2", "3", "4"],
    ]
    assert _normalize_chandra_table(table)[1] == ["a", "b", "", "", ""]


def test_sous_ligne_entete_non_appliquee_si_la_ligne_parente_est_complete():
    """Une ligne parente déjà à la bonne largeur ne signale aucun libellé couvrant."""
    table = [
        ["SOCIETES", "Capital", "Valeur d'inventaire"],
        ["Brute", "Nette"],
        ["Entité A", "877 668", "1 734 110"],
    ]
    assert _normalize_chandra_table(table) == [
        ["SOCIETES", "Capital", "Valeur d'inventaire"],
        ["Brute", "Nette", ""],
        ["Entité A", "877 668", "1 734 110"],
    ]


def test_ligne_de_donnees_courte_reste_completee_a_droite():
    """Le repositionnement ne concerne que la zone d'en-tête, jamais les données."""
    table = [
        ["SOCIETES", "Capital", "Valeur d'inventaire"],
        ["Entité A", "877 668"],
        ["Entité B", "1 000", "2 000"],
    ]
    assert _normalize_chandra_table(table)[1] == ["Entité A", "877 668", ""]


# ── chandra : rejet des blocs sans données ────────────────────────────────────


def test_chandra_ignore_les_tableaux_sans_ligne_de_donnees():
    assert _normalize_chandra_table([["titre seul"], ["autre"]]) is None


def test_chandra_ligne_label_plus_longue_que_les_donnees():
    """Une ligne-label ne fixe pas la largeur du tableau.

    Certains moteurs émettent l'intertitre de section en fin de ligne, avec autant de
    cellules vides avant lui. Retenu comme largeur, il ajouterait ces colonnes fantômes
    à toutes les lignes : c'est l'inverse qui doit se produire, l'intertitre revient en
    première colonne et le tableau garde la largeur de ses données.
    """
    table = [
        ["", "", "", "intertitre"],
        ["Entité A", "877 668"],
    ]
    assert _normalize_chandra_table(table) == [
        ["intertitre", ""],
        ["Entité A", "877 668"],
    ]


def test_chandra_extractor_parcourt_les_pages():
    """Deux tableaux d'en-têtes différents restent deux tableaux, page à page."""
    data = {
        "pages": [
            {"page": 1, "tables": [[["a", "b"], ["1", "2"]]]},
            {"page": 2, "tables": [[["c", "d"], ["3", "4"]]]},
        ]
    }
    assert ChandraTableExtractor().extract(data) == [
        [["a", "b"], ["1", "2"]],
        [["c", "d"], ["3", "4"]],
    ]


# ── sous-lignes d'en-tête : positions déduites sans vocabulaire ───────────────


def test_sous_ligne_entete_placee_dans_le_trou_des_cellules_de_continuation():
    """La ligne parente est complète : le trou qu'elle porte désigne la position.

    Le moteur a émis les cellules de continuation du libellé couvrant mais livre quand
    même la sous-ligne à plat. Aucun libellé n'a besoin d'être interprété.
    """
    table = [
        ["SOCIETES", "Valeur d'inventaire", "", "Dividendes"],
        ["Brute", "Nette"],
        ["Entité A", "1 734 110", "1 700 000", "12"],
    ]
    assert _normalize_chandra_table(table)[:2] == [
        ["SOCIETES", "Valeur d'inventaire", "", "Dividendes"],
        ["", "Brute", "Nette", ""],
    ]


def test_sous_ligne_entete_trou_ambigu_reste_a_droite():
    """Deux trous de la bonne longueur : rien ne dit lequel la sous-ligne détaille."""
    table = [
        ["Filiales", "Brut", "", "Net", ""],
        ["a", "b"],
        ["Entité A", "1", "2", "3", "4"],
    ]
    assert _normalize_chandra_table(table)[1] == ["a", "b", "", "", ""]


def test_sous_ligne_entete_placee_sans_vocabulaire_si_un_seul_candidat():
    """Hors du tableau réglementaire, un unique libellé candidat suffit à trancher.

    La première colonne est exclue : elle porte les libellés de lignes, jamais un
    en-tête à sous-colonnes.
    """
    table = [
        ["SOCIETES", "Répartition"],
        ["N", "N-1"],
        ["Entité A", "1", "2"],
    ]
    assert _normalize_chandra_table(table) == [
        ["SOCIETES", "Répartition", ""],
        ["", "N", "N-1"],
        ["Entité A", "1", "2"],
    ]


def test_sous_ligne_entete_deux_candidats_sans_vocabulaire_reste_a_droite():
    table = [
        ["SOCIETES", "Alpha", "Beta"],
        ["a", "b"],
        ["Entité A", "1", "2", "3"],
    ]
    assert _normalize_chandra_table(table)[1] == ["a", "b", "", ""]


# ── chandra : les deux formats de sortie de l'API ─────────────────────────────


def test_chandra_html_conserve_les_fusions():
    """Le format HTML porte les fusions : elles sont développées, pas devinées.

    Le format historique, aplati par l'API, obligeait à replacer la sous-ligne au moyen
    d'heuristiques ; ici `colspan` et `rowspan` donnent la réponse.
    """
    data = {
        "metadata": {"model": "datalab-to/chandra-ocr-2", "dpi": 200},
        "pages": [
            {
                "page": 1,
                "html": (
                    "<table><tbody>"
                    "<tr><th rowspan='2'>SOCIETES</th><th rowspan='2'>Capital</th>"
                    "<th colspan='2'>Valeur d'inventaire</th></tr>"
                    "<tr><th>Brute</th><th>Nette</th></tr>"
                    "<tr><td>Entité A</td><td>877 668</td><td>1 734 110</td><td>1 700 000</td></tr>"
                    "</tbody></table>"
                ),
            }
        ],
    }
    assert ChandraTableExtractor().extract(data) == [
        [
            ["SOCIETES", "Capital", "Valeur d'inventaire", ""],
            ["", "", "Brute", "Nette"],
            ["Entité A", "877 668", "1 734 110", "1 700 000"],
        ]
    ]


def test_chandra_html_separe_les_lignes_dune_meme_cellule():
    """`<br>` devient une espace au lieu de souder les mots.

    C'est ce que l'aplatissement en amont détruisait : « Prêts etavancesconsentis ».
    """
    html = (
        "<table><tbody>"
        "<tr><th>SOCIETES</th><th>Prêts et<br/>avances<br/>consentis</th><th>Capital</th></tr>"
        "<tr><td>Entité A</td><td>1 000</td><td>250</td></tr>"
        "</tbody></table>"
    )
    grille = ChandraTableExtractor().extract({"pages": [{"page": 1, "html": html}]})
    assert grille[0][0] == ["SOCIETES", "Prêts et avances consentis", "Capital"]


def test_chandra_les_pages_donnent_des_tableaux_distincts():
    """Sans recollement, chaque page produit son propre tableau."""
    entete = "<tr><th>SOCIETES</th><th>Capital</th><th>Résultat</th></tr>"
    data = {
        "pages": [
            {
                "page": 1,
                "html": f"<table><tbody>{entete}"
                "<tr><td>Entité A</td><td>1 000</td><td>250</td></tr></tbody></table>",
            },
            {
                "page": 2,
                "html": "<table><tbody>"
                "<tr><td>Entité B</td><td>2 000</td><td>500</td></tr></tbody></table>",
            },
        ]
    }
    assert ChandraTableExtractor().extract(data) == [
        [["SOCIETES", "Capital", "Résultat"], ["Entité A", "1 000", "250"]],
        [["Entité B", "2 000", "500"]],
    ]


def test_chandra_les_deux_formats_donnent_la_meme_grille():
    """Sur un tableau sans fusion, l'ancien et le nouveau format se rejoignent."""
    entete = ["SOCIETES", "Capital", "Résultat"]
    plat = {"pages": [{"page": 1, "tables": [[entete, ["Entité A", "1 000", "250"]]]}]}
    html = {
        "pages": [
            {
                "page": 1,
                "html": (
                    "<table><tbody>"
                    "<tr><th>SOCIETES</th><th>Capital</th><th>Résultat</th></tr>"
                    "<tr><td>Entité A</td><td>1 000</td><td>250</td></tr>"
                    "</tbody></table>"
                ),
            }
        ]
    }
    extractor = ChandraTableExtractor()
    assert extractor.extract(html) == extractor.extract(plat)


def test_normalize_grid_est_idempotent():
    """Une grille déjà normalisée traverse la normalisation sans changer.

    C'est ce qui permet aux grilles issues du parseur HTML — déjà normalisées — de suivre
    ensuite le même chemin que les matrices à plat.
    """
    brut = [
        ["SOCIETES", "Valeur d'inventaire", "Dividendes"],
        ["Brute", "Nette"],
        ["Entité A", "1", "2", "3"],
        ["", "", "", "intertitre"],
    ]
    une_fois = _normalize_grid(brut)
    assert _normalize_grid(une_fois) == une_fois


# ── marker : un seul tableau par `TableGroup`, garanti par le code ────────────


def test_table_group_ecarte_au_profit_de_ses_blocs_table():
    """Un `TableGroup` et ses enfants `Table` décrivent le même tableau.

    Le `html` d'un `TableGroup` ne porte en principe que des pointeurs `<content-ref>`,
    qui ne produisent aucune cellule — mais rien dans le format ne le garantit. La
    déduplication doit venir du code, pas de la forme de la sortie du moteur.
    """
    data = {
        "block_type": "Page",
        "children": [
            {
                "block_type": "TableGroup",
                "html": "<content-ref src='/page/0/Table/12'></content-ref>",
                "children": [
                    {
                        "block_type": "Table",
                        "html": "<table><tbody><tr><td>Entité A</td><td>1</td></tr></tbody></table>",
                        "children": [],
                    }
                ],
            }
        ],
    }
    assert MarkerTableExtractor().extract(data) == [[["Entité A", "1"]]]


def test_table_group_portant_lui_meme_un_table_ne_duplique_pas():
    """Même quand le groupe porte un `<table>` complet, le tableau ne sort qu'une fois."""
    html = "<table><tbody><tr><td>Entité A</td><td>1</td></tr></tbody></table>"
    data = {
        "block_type": "Page",
        "children": [
            {
                "block_type": "TableGroup",
                "html": html,
                "children": [{"block_type": "Table", "html": html, "children": []}],
            }
        ],
    }
    assert MarkerTableExtractor().extract(data) == [[["Entité A", "1"]]]


def test_table_group_sans_bloc_table_reste_retenu():
    """Un groupe qui porte seul le HTML du tableau doit être conservé."""
    data = {
        "block_type": "Page",
        "children": [
            {
                "block_type": "TableGroup",
                "html": "<table><tbody><tr><td>Entité A</td><td>1</td></tr></tbody></table>",
                "children": [],
            }
        ],
    }
    assert MarkerTableExtractor().extract(data) == [[["Entité A", "1"]]]


# ── recollage des blocs chandra d'une même page ───────────────────────────────


def chandra_page(html: str) -> list[list[list[str]]]:
    """Passe une page chandra à l'extracteur et retourne ses tableaux."""
    return ChandraTableExtractor().extract({"pages": [{"page": 1, "html": html}]})


ENTETE = "<thead><tr><th>Sociétés</th><th>Capital</th><th>Résultat</th></tr></thead>"


def test_chandra_entete_orphelin_recolle_avec_les_blocs_suivants():
    """Un `<thead>` sans `<tbody>` est un tableau inachevé, complété par la suite.

    Cas de `468_316701416_TAB` : chandra coupe le tableau à chaque intertitre de
    section et rend l'en-tête de colonnes seul, puis trois corps sans en-tête. Sans
    recollage, la conversion écrit quatre CSV pour un tableau, et les rangs se
    désynchronisent de ceux de l'annotation pour tout le SIREN.
    """
    html = (
        f'<div data-bbox="71 75 565 125" data-label="Table"><table>{ENTETE}</table></div>'
        '<div data-bbox="71 138 565 278" data-label="Table"><table><tbody>'
        "<tr><td>Entité A</td><td>1 000</td><td>250</td></tr></tbody></table></div>"
        '<div data-bbox="71 291 565 338" data-label="Table"><table><tbody>'
        "<tr><td>Entité B</td><td>2 000</td><td>500</td></tr></tbody></table></div>"
    )
    assert chandra_page(html) == [
        [
            ["Sociétés", "Capital", "Résultat"],
            ["Entité A", "1 000", "250"],
            ["Entité B", "2 000", "500"],
        ]
    ]


def test_chandra_entete_reimprime_reste_un_autre_tableau():
    """Deux blocs portant chacun un vrai en-tête de colonnes sont deux tableaux.

    Cas de `_1465_652027384_TAB` : quatre tableaux d'une même page rouvrent tous sur
    « (en milliers d'euros) | 31/12/2021 | 31/12/2020 ». Un en-tête réimprimé dans une
    même page désigne un autre tableau de même forme, pas une suite — et les deux dates
    de cet en-tête interdisent de s'en remettre au comptage des cellules numériques.
    """
    bloc = (
        '<div data-label="Table"><table>'
        "<thead><tr><th>(en milliers d'euros)</th><th>31/12/2021</th>"
        "<th>31/12/2020</th></tr></thead>"
        "<tbody><tr><td>{nom}</td><td>100 270</td><td>61 286</td></tr></tbody>"
        "</table></div>"
    )
    tables = chandra_page(bloc.format(nom="Entité A") + bloc.format(nom="Entité B"))
    assert len(tables) == 2
    assert tables[0][1] == ["Entité A", "100 270", "61 286"]
    assert tables[1][1] == ["Entité B", "100 270", "61 286"]


def test_chandra_intertitre_dans_le_thead_ne_fait_pas_un_tableau():
    """Un `th` unique en `colspan` pleine largeur est un intertitre, pas un en-tête.

    Cas de `552142200` : le bloc de suite s'ouvre sur
    `<thead><tr><th colspan="3">I. Filiales</th></tr></thead>`. Compté comme en-tête de
    colonnes, il ferait passer la suite du tableau pour un tableau autonome.
    """
    html = (
        f'<div data-label="Table"><table>{ENTETE}</table></div>'
        '<div data-label="Table"><table>'
        '<thead><tr><th colspan="3">I. Filiales (50 % au moins)</th></tr></thead>'
        "<tbody><tr><td>Entité A</td><td>1 000</td><td>250</td></tr></tbody>"
        "</table></div>"
    )
    assert chandra_page(html) == [
        [
            ["Sociétés", "Capital", "Résultat"],
            ["I. Filiales (50 % au moins)", "", ""],
            ["Entité A", "1 000", "250"],
        ]
    ]


def test_chandra_toutes_les_lignes_dans_le_thead_reste_un_tableau_complet():
    """Un bloc qui met ses données dans le `thead` est complet, malgré l'absence de tbody.

    Cas de `974_380097881_TAB` : chandra n'ouvre jamais de `<tbody>` et écrit les lignes
    de données en `<td>` dans le `<thead>`. C'est la présence de lignes de données qui
    fait le tableau entier, pas la balise qui les entoure — sans quoi ce bloc passerait
    pour un en-tête orphelin et absorberait le tableau suivant.
    """
    html = (
        '<div data-label="Table"><table><thead>'
        "<tr><th>Sociétés</th><th>Capital</th><th>Résultat</th></tr>"
        "<tr><td>Entité A</td><td>1 000</td><td>250</td></tr>"
        "</thead></table></div>"
        f'<div data-label="Table"><table>{ENTETE}'
        "<tbody><tr><td>Entité B</td><td>2 000</td><td>500</td></tr></tbody>"
        "</table></div>"
    )
    assert len(chandra_page(html)) == 2


def test_chandra_largeurs_differentes_ne_se_recollent_pas():
    """Un tableau coupé en largeur ne se recolle jamais par lignes."""
    html = (
        f'<div data-label="Table"><table>{ENTETE}</table></div>'
        '<div data-label="Table"><table><tbody>'
        "<tr><td>Entité A</td><td>1 000</td></tr></tbody></table></div>"
    )
    assert len(chandra_page(html)) == 2


def test_chandra_intertitre_devient_une_ligne_label():
    """L'intertitre entre deux blocs recollés revient dans le tableau.

    Il est hors de toute balise `<table>` : sans reprise, il est perdu. L'annotation le
    porte comme ligne-label, à cette place exacte.
    """
    html = (
        f'<div data-bbox="71 75 565 125" data-label="Table"><table>{ENTETE}</table></div>'
        '<div data-bbox="71 125 565 138" data-label="Section-Header">'
        "<p><b>A. Renseignements détaillés</b></p></div>"
        '<div data-bbox="71 138 565 278" data-label="Table"><table><tbody>'
        "<tr><td>Entité A</td><td>1 000</td><td>250</td></tr></tbody></table></div>"
    )
    assert chandra_page(html) == [
        [
            ["Sociétés", "Capital", "Résultat"],
            ["A. Renseignements détaillés", "", ""],
            ["Entité A", "1 000", "250"],
        ]
    ]


def test_chandra_titre_du_tableau_nest_pas_une_ligne():
    """Un intertitre devant un bloc qui a son propre en-tête est le titre du tableau.

    L'annotation ne le porte pas : le tableau commence à son en-tête de colonnes.
    """
    html = (
        '<div data-bbox="71 62 1000 75" data-label="Section-Header">'
        "<p><b>Liste des filiales et participations</b></p></div>"
        f'<div data-bbox="71 75 565 125" data-label="Table"><table>{ENTETE}'
        "<tbody><tr><td>Entité A</td><td>1 000</td><td>250</td></tr></tbody>"
        "</table></div>"
    )
    assert chandra_page(html) == [
        [["Sociétés", "Capital", "Résultat"], ["Entité A", "1 000", "250"]]
    ]


def test_chandra_libelle_extrait_du_tableau_revient_dans_sa_cellule():
    """Une raison sociale sortie du tableau est recollée en tête de sa cellule.

    Cas de `411373525` : chandra place chaque raison sociale dans un `Section-Header` et
    ne laisse que l'adresse dans la ligne, une ligne par bloc `Table`. Le bloc chevauche
    le bord supérieur du tableau — il commence au-dessus et finit dedans — là où un titre
    s'arrête avant. L'annotation réunit nom et adresse dans la même cellule.
    """
    html = (
        '<div data-bbox="81 138 218 159" data-label="Section-Header">'
        "VALLOUREC TUBES<br/>France</div>"
        '<div data-bbox="81 157 584 216" data-label="Table"><table>'
        "<tr><td>27, avenue du Général-Leclerc</td><td>918 466</td><td>-</td></tr>"
        "</table></div>"
    )
    assert chandra_page(html) == [
        [["VALLOUREC TUBES France 27, avenue du Général-Leclerc", "918 466", "-"]]
    ]


def test_chandra_titre_lateral_dune_page_paysage_nentre_pas_dans_une_cellule():
    """Sur une page en paysage, le titre latéral couvre la hauteur du tableau.

    Son ordonnée est comprise dans celle du tableau au lieu d'en chevaucher le bord
    supérieur, et il ne le recouvre pas horizontalement : ce n'est pas un libellé de
    ligne, et il ne doit pas être collé en tête de cellule. Cinq titres du corpus y
    atterriraient sans ces deux garde-fous. Il retombe sur le sort commun des
    intertitres, une ligne-label, qui laisse les données intactes.
    """
    html = (
        '<div data-bbox="36 469 56 948" data-label="Section-Header">'
        "NOTE 14. LISTE DES FILIALES</div>"
        '<div data-bbox="161 8 829 990" data-label="Table"><table>'
        "<tr><td>Entité A</td><td>918 466</td><td>-</td></tr>"
        "</table></div>"
    )
    assert chandra_page(html) == [
        [["NOTE 14. LISTE DES FILIALES", "", ""], ["Entité A", "918 466", "-"]]
    ]


def test_chandra_intertitre_separe_par_un_paragraphe_ne_revient_pas():
    """Un bloc quelconque entre l'intertitre et le tableau rompt le voisinage."""
    html = (
        f'<div data-label="Table"><table>{ENTETE}</table></div>'
        '<div data-label="Section-Header"><p>A. Renseignements détaillés</p></div>'
        '<div data-label="Text"><p>Les montants sont exprimés en euros.</p></div>'
        '<div data-label="Table"><table><tbody>'
        "<tr><td>Entité A</td><td>1 000</td><td>250</td></tr></tbody></table></div>"
    )
    assert chandra_page(html) == [
        [["Sociétés", "Capital", "Résultat"], ["Entité A", "1 000", "250"]]
    ]


def test_chandra_titre_courant_nentre_pas_dans_le_tableau():
    """Un titre courant reste dehors, même étiqueté `Section-Header` sur sa page.

    Cas de `411373525` : chandra étiquette « Vallourec Tubes » et la date des comptes
    `Page-Header` en page 2, et `Section-Header` en page 1, à texte identique. Sans le
    recoupement, ces deux lignes entraient en tête du premier tableau de la page 1, que
    l'annotation donne sans elles — un tableau jusque-là parfaitement extrait.
    """
    data = {
        "pages": [
            {
                "page": 1,
                "html": (
                    '<div data-bbox="70 32 200 46" data-label="Section-Header">'
                    "Vallourec Tubes</div>"
                    '<div data-bbox="60 85 555 195" data-label="Table"><table><tbody>'
                    "<tr><td>Provisions pour risques</td><td>5 235</td></tr>"
                    "</tbody></table></div>"
                ),
            },
            {
                "page": 2,
                "html": (
                    '<div data-bbox="448 30 580 44" data-label="Page-Header">'
                    "Vallourec Tubes</div>"
                    '<div data-bbox="81 157 584 216" data-label="Table"><table><tbody>'
                    "<tr><td>Entité A</td><td>918 466</td></tr></tbody></table></div>"
                ),
            },
        ]
    }
    assert ChandraTableExtractor().extract(data) == [
        [["Provisions pour risques", "5 235"]],
        [["Entité A", "918 466"]],
    ]


def test_chandra_tableau_hors_bloc_etiquete_est_conserve():
    """Un `<table>` hors de tout `data-label` reste un tableau à part entière.

    Cas de `380129866` : chandra sort ses `div` avec un `data-bbox` en double et aucun
    `data-label`. Faire dépendre la recherche des tableaux de l'étiquetage perdrait ici
    la page entière.
    """
    html = (
        '<div data-bbox="89 133 924 529" data-bbox="89 133 924 529">'
        f"<table>{ENTETE}"
        "<tbody><tr><td>Entité A</td><td>1 000</td><td>250</td></tr></tbody>"
        "</table></div>"
    )
    assert chandra_page(html) == [
        [["Sociétés", "Capital", "Résultat"], ["Entité A", "1 000", "250"]]
    ]


# ── régénération : purge des rangs surnuméraires ──────────────────────────────


def test_stale_csv_paths_ne_retient_que_les_rangs_au_dela_du_compte():
    existing = [
        "bucket/out/123456789_1.csv",
        "bucket/out/123456789_2.csv",
        "bucket/out/123456789_3.csv",
    ]
    assert _stale_csv_paths(existing, "123456789", 2) == ["bucket/out/123456789_3.csv"]
    assert _stale_csv_paths(existing, "123456789", 3) == []


def test_stale_csv_paths_ignore_les_autres_radicaux():
    """Un radical qui en préfixe un autre ne doit pas emporter ses fichiers."""
    existing = [
        "bucket/out/TAB_123_1.csv",
        "bucket/out/TAB_123_bis_4.csv",
        "bucket/out/TAB_1234_9.csv",
    ]
    assert _stale_csv_paths(existing, "TAB_123", 1) == []


def test_table_non_fermee_est_quand_meme_publiee():
    """Un `</table>` manquant ne doit pas faire disparaître ce qui a été lu.

    Le cas se présente des deux côtés de la mesure : une annotation saisie à la main dont
    la balise finale manque, et une réponse de VLM coupée par sa limite de jetons. Sans
    reprise en fin de flux, le fichier ne produit aucun tableau et quitte l'évaluation en
    silence, au lieu d'y figurer pour ce qu'il vaut.
    """
    html = "<table><tr><td>a</td><td>1</td></tr><tr><td>b</td><td>2</td></tr>"
    assert _parse_html_tables(html) == [[["a", "1"], ["b", "2"]]]


def test_table_non_fermee_ne_duplique_pas_la_precedente():
    """La reprise ne republie pas le tableau déjà clos qui la précède."""
    html = "<table><tr><td>a</td><td>1</td></tr></table><table><tr><td>b</td><td>2</td></tr>"
    assert _parse_html_tables(html) == [[["a", "1"]], [["b", "2"]]]


def test_table_fermee_nest_publiee_quune_fois():
    """Un document bien formé est parsé exactement comme avant la reprise."""
    html = "<table><tr><td>a</td><td>1</td></tr></table>"
    assert _parse_html_tables(html) == [[["a", "1"]]]


# ── Récupération sur HTML malformé (règles de fermeture implicite HTML5) ──────
#
# Dix des quarante annotations du corpus historique portent une balise en trop ou en
# moins. Ces règles sont celles d'un navigateur ; elles ne se déclenchent que sur du HTML
# invalide, et la sortie du corpus `reprise/` est inchangée au bit près.


def test_tr_en_double_ne_consomme_pas_une_ligne_de_rowspan():
    """`<tr> <tr>` est une balise en double, pas une ligne vide.

    Sans cette règle, la ligne fantôme décomptait le `rowspan` en cours : la fusion
    cessait une ligne trop tôt et tout ce qui suivait glissait d'un cran. Sur
    `ground_truth_1876`, cela inventait huit colonnes entièrement vides.
    """
    html = (
        "<table>"
        '<tr><td rowspan="2">A</td><td>1</td></tr>'
        "<tr> <tr><td>2</td></tr>"
        "<tr><td>B</td><td>3</td></tr>"
        "</table>"
    )
    assert _parse_html_tables(html) == [[["A", "1"], ["", "2"], ["B", "3"]]]


def test_tr_implicite_ferme_la_ligne_ouverte():
    """Un `<tr>` alors qu'une ligne est ouverte referme celle-ci, contenu conservé."""
    html = "<table><tr><td>a</td><td>1</td><tr><td>b</td><td>2</td></tr></table>"
    assert _parse_html_tables(html) == [[["a", "1"], ["b", "2"]]]


def test_td_non_referme_ne_fuit_pas_sur_la_ligne_suivante():
    """Une cellule laissée ouverte est fermée par la fin de sa ligne, pas par la suivante."""
    html = "<table><tr><td>a</td><td>1<tr><td>b</td><td>2</td></tr></table>"
    assert _parse_html_tables(html) == [[["a", "1"], ["b", "2"]]]


def test_td_non_referme_est_ferme_par_le_td_suivant():
    """Deux `<td>` d'affilée : le premier est refermé, sa valeur reste à sa place."""
    html = "<table><tr><td>a<td>1</td></tr></table>"
    assert _parse_html_tables(html) == [[["a", "1"]]]


def test_balise_fermante_orpheline_nest_pas_une_cellule():
    """Un `</td>` sans ouvrant n'écrit pas de cellule fantôme."""
    html = "<table><tr><td>a</td></td><td>1</td></tr></table>"
    assert _parse_html_tables(html) == [[["a", "1"]]]


def test_cellule_hors_ligne_ouvre_une_ligne():
    """Un `<td>` rencontré hors de toute `<tr>` en ouvre une."""
    html = "<table><td>a</td><td>1</td></table>"
    assert _parse_html_tables(html) == [[["a", "1"]]]


def test_ligne_ouverte_a_la_fermeture_de_la_table():
    """Un `</table>` referme la ligne et la cellule restées ouvertes."""
    html = "<table><tr><td>a</td><td>1</td></tr><tr><td>b</td><td>2</table>"
    assert _parse_html_tables(html) == [[["a", "1"], ["b", "2"]]]
