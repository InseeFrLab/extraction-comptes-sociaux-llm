"""Blocs de mise en page d'une page chandra, et recollage des tableaux qu'ils coupent.

Chandra coupe une région `Table` dès qu'un autre bloc l'interrompt : un tableau que
traverse un intertitre ressort en plusieurs `<table>`. Le modèle n'annonce aucune
continuation, mais son balisage dit lequel de ces blocs est un tableau entier — c'est ce
que lisent `_Markup` (cf. `html_tables.py`) et `_merge_chandra_blocks`. Mesures et
règles : cf. [README.md](../README.md), section « Étape 2 ».
"""

from dataclasses import dataclass, field

from .grid import Table, _canonical_width, _norm, _normalize_grid
from .html_tables import _Markup, _TableHTMLParser


@dataclass
class _Block:
    """Bloc de mise en page d'une page chandra.

    Attributes:
        label: `data-label` du bloc, ou "" pour un `<table>` hors de tout bloc étiqueté —
            chandra omet parfois l'étiquette (cf. README).
        bbox: `data-bbox` (x0, y0, x1, y1), ou None si absent ou illisible.
        text: texte du bloc hors tableaux, `<br>` ramenés à des espaces.
        tables: grilles brutes portées par le bloc, non normalisées.
        markups: balisage de chacune, dans le même ordre.
    """

    label: str
    bbox: tuple[int, int, int, int] | None = None
    text: str = ""
    tables: list[Table] = field(default_factory=list)
    markups: list[_Markup] = field(default_factory=list)


def _read_bbox(value: str | None) -> tuple[int, int, int, int] | None:
    """Lit un `data-bbox`, ou None s'il est absent ou n'a pas quatre entiers."""
    try:
        x0, y0, x1, y1 = (int(v) for v in (value or "").split())
    except ValueError:
        return None
    return x0, y0, x1, y1


def _straddles_top(header: _Block, table: _Block) -> bool:
    """L'intertitre déborde-t-il sur le haut du tableau qui le suit ?

    Le bloc doit commencer au-dessus du tableau et finir dedans, et le recouvrir
    horizontalement : cf. README.

    Args:
        header: bloc `Section-Header`.
        table: bloc `Table` qui le suit immédiatement.

    Returns:
        True si le texte de l'intertitre appartient à la première ligne du tableau.
    """
    if not header.bbox or not table.bbox:
        return False
    hx0, hy0, hx1, hy1 = header.bbox
    tx0, ty0, tx1, ty1 = table.bbox
    return hy0 < ty0 < hy1 and min(hx1, tx1) > max(hx0, tx0)


def _continues(group: Table, group_has_data: bool, table: Table, markup: _Markup) -> bool:
    """Le tableau prolonge-t-il celui en cours, ou en ouvre-t-il un autre ?

    Deux lectures du balisage, à largeur de colonnes identique : le bloc n'a pas d'en-tête
    propre, ou le tableau en cours n'a pas encore de ligne de données. Cf. README.

    Args:
        group: grille du tableau en cours de constitution.
        group_has_data: le tableau en cours porte-t-il déjà une ligne de données ?
        table: grille brute du bloc candidat.
        markup: balisage du bloc candidat.

    Returns:
        True s'il faut concaténer le bloc au tableau en cours.
    """
    if _canonical_width(group) != _canonical_width(table):
        return False
    return not markup.has_column_header or not group_has_data


def _running_titles(pages: list[list[_Block]]) -> set[str]:
    """Textes que le document porte en titre courant, quelle que soit la page.

    Chandra ne les étiquette pas de la même façon partout : un intertitre vu ailleurs comme
    titre ou pied de page n'appartient pas au tableau (cf. README).

    Args:
        pages: blocs de chaque page du document.

    Returns:
        Les textes normalisés vus au moins une fois en `Page-Header` ou `Page-Footer`.
    """
    return {
        _norm(block.text).strip()
        for page in pages
        for block in page
        if block.label in ("Page-Header", "Page-Footer") and block.text.strip()
    }


def _merge_chandra_blocks(blocks: list[_Block], titles: set[str] = frozenset()) -> list[Table]:
    """Recolle les blocs d'une page chandra en tableaux, intertitres réinjectés.

    Le recollage est borné à la page, et précède la normalisation : la largeur canonique se
    lit mieux sur le tableau entier que sur un fragment. Le texte des `Section-Header` est
    rendu au tableau, collé en tête de première cellule (`_straddles_top`) ou posé en
    ligne-label. Cf. README.

    Args:
        blocks: blocs d'une page, dans l'ordre de lecture.
        titles: textes que le document porte en titre courant (`_running_titles`), écartés
            quelle que soit l'étiquette que chandra leur donne sur cette page-ci.

    Returns:
        Une grille brute par tableau reconstitué.
    """
    tables: list[Table] = []
    has_data: list[bool] = []
    pending: list[_Block] = []
    for block in blocks:
        if not block.tables:
            if _norm(block.text).strip() in titles:
                continue
            # Un bloc sans tableau qui n'est pas un intertitre rompt le voisinage.
            pending = pending + [block] if block.label == "Section-Header" else []
            continue

        labels = []
        for header in pending:
            if _straddles_top(header, block) and block.tables[0] and block.tables[0][0]:
                first = block.tables[0][0]
                first[0] = f"{header.text} {first[0]}".strip()
            else:
                labels.append([header.text])
        pending = []

        for table, markup in zip(block.tables, block.markups, strict=True):
            head = labels if not markup.has_column_header else []
            labels = []
            if tables and _continues(tables[-1], has_data[-1], table, markup):
                tables[-1] = tables[-1] + head + table
                has_data[-1] = has_data[-1] or markup.has_data_row
            else:
                tables.append(head + table)
                has_data.append(markup.has_data_row)
    return tables


def _normalize_chandra_table(table: Table) -> Table | None:
    """Écarte un tableau Chandra sans données, met les autres en forme.

    La mise en forme est commune à tous les moteurs ; seul le rejet est propre à chandra
    (cf. README).

    Args:
        table: tableau brut d'une page chandra.

    Returns:
        La grille rectangulaire, ou None si le tableau ne contient aucune ligne de
        données (≥ 2 cellules non vides).
    """
    data_rows = [row for row in table if sum(1 for c in row if c.strip()) > 1]
    if not data_rows:
        return None
    return _normalize_grid(table)


class _ChandraPageParser(_TableHTMLParser):
    """Découpe une page chandra en blocs de mise en page, tableaux rattachés.

    Le découpage borne le recollage et récupère le texte des intertitres, qui appartient au
    tableau sans être dedans. Les blocs ne s'imbriquant jamais, un bloc court jusqu'à
    l'ouverture du suivant ; un `<table>` hors de tout bloc étiqueté forme son propre bloc,
    sans voisinage. Cf. README.
    """

    def __init__(self):
        super().__init__()
        self.blocks: list[_Block] = []
        # Pour chaque bloc, l'index du premier de ses tableaux dans `tables`.
        self._starts: list[int] = []

    def handle_starttag(self, tag, attrs):
        if tag == "div":
            attrs_dict = dict(attrs)
            if "data-label" in attrs_dict:
                self.blocks.append(
                    _Block(
                        label=attrs_dict["data-label"],
                        bbox=_read_bbox(attrs_dict.get("data-bbox")),
                    )
                )
                self._starts.append(len(self.tables))
        elif tag == "br" and not self._in_cell and self.blocks:
            self.blocks[-1].text += " "
        super().handle_starttag(tag, attrs)

    def handle_data(self, data):
        if not self._in_cell and self.blocks:
            self.blocks[-1].text += data
        super().handle_data(data)

    def page_blocks(self) -> list[_Block]:
        """Blocs de la page, dans l'ordre de lecture.

        Returns:
            Les blocs, chacun portant ses grilles brutes et leur balisage. Les tableaux
            rencontrés avant tout bloc étiqueté forment autant de blocs sans étiquette,
            placés en tête.
        """
        bounds = self._starts + [len(self.tables)]
        for i, block in enumerate(self.blocks):
            block.tables = self.tables[bounds[i] : bounds[i + 1]]
            block.markups = self.markups[bounds[i] : bounds[i + 1]]
            block.text = " ".join(block.text.split())
        unlabelled = [
            _Block(label="", tables=[table], markups=[markup])
            for table, markup in zip(
                self.tables[: bounds[0]], self.markups[: bounds[0]], strict=True
            )
        ]
        return unlabelled + self.blocks


def _chandra_page_blocks(html: str) -> list[_Block]:
    """Parse une page chandra et retourne ses blocs de mise en page.

    Args:
        html: HTML brut d'une page, tel que rendu par le VLM.

    Returns:
        Les blocs, dans l'ordre de lecture, tableaux et balisage rattachés. Le recollage
        vient après, une fois les titres courants du document connus.
    """
    parser = _ChandraPageParser()
    parser.feed(html)
    # `close()` publie le `<table>` qu'une réponse coupée par la limite de jetons laisse
    # ouvert : sans lui, `page_blocks` ne verrait rien de ce qui a été lu.
    parser.close()
    return parser.page_blocks()
