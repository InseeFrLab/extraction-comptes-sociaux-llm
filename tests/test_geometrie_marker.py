"""Tests du diagnostic géométrique (`legacy/geometrie_marker.py`), sans aucun accès S3.

Le script est archivé — l'analyse est close, son résultat est publié par la page
« Améliorations » du site — mais ces tests restent joués : ils ne coûtent rien et documentent
la géométrie sur laquelle repose le levier 01, s'il faut y revenir.

La logique testée est celle qui reconstruit une grille à partir des seules coordonnées :
regroupement en bandes, choix de l'axe des lignes — une page en paysage non redressée
empile ses lignes sur x — et lecture de la hauteur d'en-tête. C'est de là que sortiraient
les chiffres du diagnostic : une erreur d'axe les rendrait tous faux sans rien casser
d'autre.
"""

from geometrie_marker import (
    _regularity,
    bands_along,
    describe_table,
    header_height_from_geometry,
    row_axis,
    rows_from_geometry,
)


def cell(x0: float, y0: float, x1: float, y1: float, html: str = "<td>x</td>") -> dict:
    """Cellule marker minimale : une boîte et un fragment HTML."""
    return {"block_type": "TableCell", "bbox": [x0, y0, x1, y1], "html": html}


def grille_droite(n_lignes: int, n_colonnes: int, textes=None) -> list[dict]:
    """Cellules d'une page droite : colonnes larges de 60, lignes hautes de 20."""
    cells = []
    for i in range(n_lignes):
        for j in range(n_colonnes):
            html = "<td>x</td>"
            if textes:
                html = f"<td>{textes[i][j]}</td>"
            cells.append(cell(j * 60, i * 20, j * 60 + 58, i * 20 + 18, html))
    return cells


def grille_paysage(n_lignes: int, n_colonnes: int, textes=None) -> list[dict]:
    """Les mêmes cellules, page pivotée d'un quart de tour : les lignes s'empilent sur x."""
    return [
        cell(c["bbox"][1], c["bbox"][0], c["bbox"][3], c["bbox"][2], c["html"])
        for c in grille_droite(n_lignes, n_colonnes, textes)
    ]


# ── bandes ────────────────────────────────────────────────────────────────────


def test_bandes_regroupent_les_cellules_qui_se_recouvrent():
    cells = [cell(0, 0, 58, 18), cell(60, 1, 118, 17), cell(0, 20, 58, 38)]
    bandes = bands_along(cells, axis=1)
    assert [len(b) for b in bandes] == [2, 1]


def test_bandes_ne_chainent_pas_sur_un_tableau_de_travers():
    """Le piège du critère par écart : 40 lignes décalées de 2 points chacune.

    Avec un critère d'écart, chaque cellule est à moins de la tolérance de la suivante
    dans l'ordre trié et tout finit dans une bande unique — ce qui est arrivé sur les 478
    cellules d'un tableau du corpus. Le recouvrement, lui, réduit la bande à
    l'intersection : il ne peut pas dériver.
    """
    cells = [cell(0, i * 2.0, 58, i * 2.0 + 18) for i in range(40)]
    bandes = bands_along(cells, axis=1)
    assert len(bandes) > 1


def test_une_cellule_fusionnee_reste_dans_sa_premiere_bande():
    """Une cellule haute de deux lignes appartient à la première, pas aux deux."""
    cells = [cell(0, 0, 58, 38), cell(60, 0, 118, 18), cell(60, 20, 118, 38)]
    bandes = bands_along(cells, axis=1)
    assert [len(b) for b in bandes] == [2, 1]


# ── choix de l'axe des lignes ─────────────────────────────────────────────────


def test_axe_des_lignes_page_droite():
    assert row_axis(grille_droite(5, 3)) == 1


def test_axe_des_lignes_page_en_paysage():
    """Une page pivotée d'un quart de tour empile ses lignes sur x.

    Sans cette détection, colonnes et lignes sont échangées et toutes les mesures du
    diagnostic sont fausses — sans que rien ne le signale.
    """
    assert row_axis(grille_paysage(5, 3)) == 0


def test_axe_suppose_droit_quand_les_cellules_nont_pas_de_taille():
    """Marker positionne ses cellules sans toujours les dimensionner.

    Sur la majorité du corpus, la cellule médiane mesure 1 × 1 point : le rapport de
    forme n'est alors que du bruit, et l'avoir suivi faisait lire deux tableaux en
    travers. La page est supposée droite, cas de loin le plus fréquent.
    """
    plates = [cell(j * 60, i * 20, j * 60 + 1, i * 20 + 3) for i in range(5) for j in range(3)]
    assert row_axis(plates) == 1


def test_les_deux_orientations_donnent_la_meme_grille():
    """L'invariant qui compte : la structure lue ne dépend pas de l'orientation."""
    droite = rows_from_geometry(grille_droite(5, 3))[1]
    paysage = rows_from_geometry(grille_paysage(5, 3))[1]
    assert [len(r) for r in droite] == [len(r) for r in paysage] == [3, 3, 3, 3, 3]


def test_regularite_vaut_un_sur_une_grille_complete():
    _, rows = rows_from_geometry(grille_droite(4, 6))
    assert _regularity(rows) == 1.0


def test_regularite_baisse_avec_une_ligne_incomplete():
    cells = grille_droite(4, 6)
    del cells[7]  # une cellule de la deuxième ligne manque
    _, rows = rows_from_geometry(cells)
    assert _regularity(rows) == 0.75


# ── hauteur d'en-tête ─────────────────────────────────────────────────────────


ENTETE = ["SOCIETES", "Capital", "Résultat"]
DONNEES = ["Entité A", "1 000", "250"]


def test_hauteur_entete_une_ligne():
    textes = [ENTETE, DONNEES, DONNEES]
    _, rows = rows_from_geometry(grille_droite(3, 3, textes))
    assert header_height_from_geometry(rows) == 1


def test_hauteur_entete_deux_lignes():
    textes = [ENTETE, ["", "Brute", "Nette"], DONNEES]
    _, rows = rows_from_geometry(grille_droite(3, 3, textes))
    assert header_height_from_geometry(rows) == 2


def test_hauteur_entete_nulle_sans_ligne_textuelle():
    textes = [DONNEES, DONNEES]
    _, rows = rows_from_geometry(grille_droite(2, 3, textes))
    assert header_height_from_geometry(rows) == 0


def test_hauteur_entete_lue_depuis_lextremite_la_plus_fournie():
    """Le sens de lecture d'une page pivotée est inconnu : les deux bouts sont essayés.

    Ici l'en-tête est en fin de tableau, ce qui arrive quand la page est pivotée dans
    l'autre sens.
    """
    textes = [DONNEES, DONNEES, ENTETE]
    _, rows = rows_from_geometry(grille_droite(3, 3, textes))
    assert header_height_from_geometry(rows) == 1


def test_hauteur_entete_nulle_si_aucune_donnee():
    """Un tableau sans données n'a pas d'en-tête à mesurer."""
    textes = [ENTETE, ENTETE]
    _, rows = rows_from_geometry(grille_droite(2, 3, textes))
    assert header_height_from_geometry(rows) == 0


# ── confrontation à la grille HTML ────────────────────────────────────────────


def test_describe_table_signale_une_colonne_perdue_par_le_html():
    """Le cas réel : la géométrie voit 3 colonnes, le HTML n'en livre que 2.

    C'est le signal cherché — une structure perdue en amont de la conversion, que rien
    dans la grille HTML seule ne permet de soupçonner.
    """
    textes = [ENTETE, DONNEES]
    cells = grille_droite(2, 3, textes)
    grid = [["SOCIETES", "Capital Résultat"], ["Entité A", "1 000 250"]]
    mesures = describe_table(cells, grid, min_overlap=0.5)
    assert mesures["n_colonnes_geom"] == 3
    assert mesures["n_colonnes_html"] == 2
    assert mesures["axe_lignes"] == "y"
    assert mesures["regularite"] == 1.0


def test_describe_table_accord_parfait():
    textes = [ENTETE, DONNEES]
    mesures = describe_table(grille_droite(2, 3, textes), [ENTETE, DONNEES], min_overlap=0.5)
    assert mesures["n_colonnes_geom"] == mesures["n_colonnes_html"] == 3
    assert mesures["n_lignes_geom"] == mesures["n_lignes_html"] == 2
    assert mesures["hauteur_entete_geom"] == mesures["hauteur_entete_html"] == 1
