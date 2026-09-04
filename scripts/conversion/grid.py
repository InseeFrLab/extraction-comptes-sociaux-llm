"""Mise en forme des grilles extraites : largeur, sous-lignes d'en-tête, lignes empilées.

Ce module ne connaît ni HTML, ni moteur, ni S3 : il ne manipule que des matrices de
chaînes, et c'est le seul endroit qui décide de leur forme. Les choix et les mesures qui
les fondent sont dans [README.md](../README.md), section « Étape 2 ».
"""

import re
import unicodedata

Table = list[list[str]]


# Une cellule « numérique » commence par un chiffre ou un signe et ne contient que des
# chiffres, séparateurs et symboles de montant. Les libellés d'en-tête qui portent un
# appel de note (« Capital (3) ») ou une date (« 31-déc-21 ») en relèvent aussi, d'où la
# règle « au moins deux » pour qualifier une ligne de données.
_NUMERIC_CELL_RE = re.compile(r"^[(\-−+]?\d[\d\s  .,%()€$/–—-]*$")

# Le vocabulaire du seul en-tête à sous-colonnes du corpus : cf. README.
_GROUP_HEADER_RE = re.compile(r"valeur|inventaire")

# Marque interne d'un `<br>` dans une cellule, le temps du parsing. Elle ne survit pas à
# `_split_stacked_rows`, qui la rend soit à une espace, soit à une coupure de ligne.
_BR = "\x00"


def _norm(value: str) -> str:
    """Minuscule sans accents, pour reconnaître un libellé quelle que soit sa graphie."""
    stripped = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return stripped.lower()


def _is_numeric_cell(value: str) -> bool:
    return bool(_NUMERIC_CELL_RE.match(value.strip()))


def _label_index(row: list[str]) -> int | None:
    """Position de l'unique cellule non vide d'une ligne, ou None s'il y en a 0 ou ≥2."""
    filled = [i for i, c in enumerate(row) if c.strip()]
    return filled[0] if len(filled) == 1 else None


def _first_data_row(table: Table) -> int:
    """Rang de la première ligne de données, c'est-à-dire d'au moins deux nombres.

    Args:
        table: grille brute.

    Returns:
        L'indice de la première ligne de données, ou `len(table)` s'il n'y en a aucune.
    """
    for i, row in enumerate(table):
        if sum(1 for c in row if _is_numeric_cell(c)) >= 2:
            return i
    return len(table)


def _canonical_width(table: Table) -> int:
    """Largeur du tableau, mesurée sur les seules lignes porteuses de plusieurs cellules.

    Une ligne-label ne doit pas fixer la largeur : cf. README.

    Args:
        table: grille brute.

    Returns:
        La largeur retenue, ou 0 pour une grille vide.
    """
    body = [row for row in table if _label_index(row) is None and row]
    return max((len(r) for r in body), default=max((len(r) for r in table), default=0))


def _covering_label_index(parent: list[str]) -> int | None:
    """Position, dans une ligne d'en-tête, du libellé qui couvre plusieurs colonnes.

    Deux signaux, dans cet ordre : lexical, puis « seul candidat de la ligne ». La première
    colonne en est exclue, elle porte les raisons sociales. Cf. README.

    Args:
        parent: ligne d'en-tête, courte des cellules de continuation du libellé couvrant.

    Returns:
        L'indice du libellé couvrant, ou None si la ligne parente ne permet pas de
        trancher — auquel cas le repli à droite s'applique.
    """
    lexical = [j for j, c in enumerate(parent) if _GROUP_HEADER_RE.search(_norm(c))]
    if len(lexical) == 1:
        return lexical[0]
    candidates = [
        j for j, c in enumerate(parent) if j > 0 and c.strip() and not _is_numeric_cell(c)
    ]
    return candidates[0] if len(candidates) == 1 else None


def _continuation_run_index(parent: list[str], k: int) -> int | None:
    """Position du libellé suivi d'exactement `k - 1` cellules de continuation vides.

    Le trou dans la ligne parente désigne la position sans interpréter aucun libellé :
    cf. README.

    Args:
        parent: ligne d'en-tête, déjà à la largeur du tableau.
        k: nombre de cellules de la sous-ligne à replacer.

    Returns:
        L'indice du libellé couvrant, ou None si aucun trou de la bonne longueur n'existe
        ou si plusieurs sont candidats.
    """
    matches = [
        j
        for j in range(len(parent) - k + 1)
        if parent[j].strip() and all(not c.strip() for c in parent[j + 1 : j + k])
    ]
    return matches[0] if len(matches) == 1 else None


def _align_header_subrows(table: Table, width: int) -> Table:
    """Replace une sous-ligne d'en-tête sous le libellé qu'elle détaille.

    Deux configurations, selon que le moteur a émis ou non les cellules de continuation du
    libellé couvrant : ligne parente courte de `k - 1` (`_covering_label_index`), ou déjà à
    la largeur du tableau (`_continuation_run_index`). Quand la position ne peut pas être
    tranchée, le repli à droite s'applique. Ce que ce traitement évite : cf. README.

    Args:
        table: grille brute.
        width: largeur canonique.

    Returns:
        La grille, sous-lignes d'en-tête repositionnées.
    """
    rows = [list(r) for r in table]
    for i in range(1, _first_data_row(rows)):
        row = rows[i]
        k = len(row)
        if not (2 <= k < width) or any(_is_numeric_cell(c) for c in row):
            continue
        parent = rows[i - 1]
        if len(parent) == width - (k - 1):
            j = _covering_label_index(parent)
            if j is None:
                continue
            rows[i - 1] = parent[: j + 1] + [""] * (k - 1) + parent[j + 1 :]
        elif len(parent) == width:
            j = _continuation_run_index(parent, k)
            if j is None:
                continue
        else:
            continue
        rows[i] = [""] * j + row + [""] * (width - j - k)
    return rows


def _normalize_grid(table: Table) -> Table:
    """Met une grille extraite en forme rectangulaire, colonnes alignées.

    Trois règles, dans l'ordre : largeur mesurée hors lignes-labels, sous-lignes
    d'en-tête replacées sous le libellé qu'elles détaillent, lignes-labels ramenées en
    première colonne. Ce qui reste court est complété à droite, faute de mieux.

    Args:
        table: grille brute issue d'un extracteur.

    Returns:
        Grille rectangulaire.
    """
    if not table:
        return table
    width = _canonical_width(table)
    rows = _align_header_subrows(table, width)

    normalized: Table = []
    for row in rows:
        label = _label_index(row)
        # Un intertitre occupe la ligne entière : sa colonne d'origine ne porte rien.
        if label is not None and len(row) > width:
            normalized.append([row[label]] + [""] * (width - 1))
        else:
            normalized.append(row)
    return _rectangularize(normalized)


def _stacked_parts(row: list[str]) -> list[list[str]] | None:
    """La ligne empile-t-elle plusieurs enregistrements, un par ligne physique ?

    Signature volontairement étroite — plusieurs cellules coupées, toutes du même nombre de
    parties, et au moins deux d'entre elles empilant deux nombres : cf. README.

    Args:
        row: ligne brute, cellules portant encore leurs marques `_BR`.

    Returns:
        Les parties de chaque cellule si la ligne empile des enregistrements, None sinon.
    """
    parts = [[p.strip() for p in cell.split(_BR)] for cell in row]
    stacked = [p for p in parts if len(p) > 1]
    if len(stacked) < 2 or len({len(p) for p in stacked}) != 1:
        return None
    numeric = sum(1 for p in stacked if sum(1 for q in p if q and _is_numeric_cell(q)) >= 2)
    return parts if numeric >= 2 else None


def _split_stacked_rows(table: Table) -> Table:
    """Rend chaque `<br>` d'une cellule, soit à une espace, soit à une coupure de ligne.

    L'espace est le comportement par défaut ; la coupure ne s'applique qu'aux lignes que
    `_stacked_parts` reconnaît, et une cellule non coupée y garde sa valeur sur la première
    ligne produite. Pourquoi ces deux sorts : cf. README.

    Args:
        table: grille brute sortie du parseur, marques `_BR` comprises.

    Returns:
        La grille sans aucune marque `_BR`.
    """
    rows: Table = []
    for row in table:
        parts = _stacked_parts(row)
        if parts is None:
            rows.append([cell.replace(_BR, " ").strip() for cell in row])
            continue
        height = max(len(p) for p in parts)
        for i in range(height):
            rows.append([p[i] if len(p) == height else (p[0] if i == 0 else "") for p in parts])
    return rows


def _rectangularize(table: Table) -> Table:
    """Complète les lignes courtes pour que toutes aient la largeur de la plus longue.

    C'est ici, et seulement ici, que l'on sait d'où viennent les cellules manquantes :
    cf. README.

    Args:
        table: grille éventuellement irrégulière.

    Returns:
        La même grille, toutes lignes portées à la largeur maximale par des cellules
        vides à droite. Une grille déjà rectangulaire est retournée inchangée.
    """
    if not table:
        return table
    width = max(len(row) for row in table)
    return [row + [""] * (width - len(row)) for row in table]
