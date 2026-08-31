#!/usr/bin/env python3
"""
Conversion des sorties brutes des moteurs (JSON marker/chandra, HTML OpenDataLoader) en
tableaux CSV, vers S3.

Une **méthode** est un couple (corpus, moteur) : les préfixes d'entrée et de sortie,
l'extension lue et l'extracteur qui sait la lire viennent tous de `config/*.yaml`, section
`moteurs`. Les moteurs d'un corpus qui en déclare un sont suffixés (`chandra` pour les
comptes sociaux, `chandra_historiques` pour les tableaux historiques) : convertir un
nouveau moteur ne demande donc qu'une entrée dans le fichier de configuration de son
corpus, et rien ici.

**Les choix de conversion et les mesures qui les fondent sont dans [README.md](README.md)**,
section « Étape 2 » : traitement des fusions, tolérance du parseur, découpage des lignes
empilées, placement des sous-lignes d'en-tête, recollage des blocs chandra.

Usage (`--method all` par défaut ; `--list` énumère les méthodes configurées) :
    uv run json_to_csv.py --list
    uv run json_to_csv.py --method marker
    uv run json_to_csv.py --method chandra_historiques
    uv run json_to_csv.py --method all
    uv run json_to_csv.py --method marker --overwrite   (régénère au lieu d'ignorer)
"""

import argparse
import csv
import io
import json
import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import s3fs
from extraction_common.s3 import get_s3_fs

import config

# Les méthodes convertibles : un moteur de chaque corpus configuré, indexé par la clé que
# prend `--method`. Ajouter une condition d'expérience se fait dans `config/*.yaml`.
SOURCES = config.moteurs()

Table = list[list[str]]


# Une cellule « numérique » commence par un chiffre ou un signe et ne contient que des
# chiffres, séparateurs et symboles de montant. Les libellés d'en-tête qui portent un
# appel de note (« Capital (3) ») ou une date (« 31-déc-21 ») en relèvent aussi, d'où la
# règle « au moins deux » pour qualifier une ligne de données.
_NUMERIC_CELL_RE = re.compile(r"^[(\-−+]?\d[\d\s  .,%()€$/–—-]*$")

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


# ── Extracteurs ───────────────────────────────────────────────────────────────


class TableExtractor(ABC):
    @abstractmethod
    def extract(self, data: dict) -> list[Table]:
        """Extraire les tableaux d'un JSON ; retourne une liste de matrices de chaînes."""


class MarkerTableExtractor(TableExtractor):
    """
    Extrait les tableaux HTML imbriqués produits par l'API Marker.
    Parcourt récursivement les blocs de type Table / TableGroup.
    """

    def extract(self, data: dict) -> list[Table]:
        return [table for block in self.table_blocks(data) for table in self._block_tables(block)]

    @staticmethod
    def _block_tables(block: dict) -> list[Table]:
        """Grilles portées par un bloc.

        Args:
            block: bloc `Table` ou `TableGroup`.

        Returns:
            Une grille par `<table>` du fragment HTML. Un fragment sans balise `<table>` est
            enveloppé dans une ligne artificielle, et n'en produit aucune s'il ne porte pas
            de cellule (cf. README).
        """
        html = block.get("html", "")
        if "<table" not in html.lower():
            html = f"<table><tbody><tr>{html}</tr></tbody></table>"
        return _parse_html_tables(html)

    def table_blocks(self, node) -> list[dict]:
        """Blocs de tableau du document, dans l'ordre de lecture.

        Un `TableGroup` dont la descendance porte des blocs `Table` est écarté au profit de
        ceux-ci : cf. README.

        Args:
            node: racine du JSON marker, ou tout sous-arbre.

        Returns:
            La liste des blocs `Table` retenus.
        """
        blocks: list[dict] = []

        def walk(current, sink: list[dict]) -> None:
            if isinstance(current, list):
                for item in current:
                    walk(item, sink)
                return
            if not isinstance(current, dict):
                return
            block_type = current.get("block_type")
            children = current.get("children") or []
            if block_type == "Table":
                # Les enfants d'un `Table` sont ses cellules : rien à y chercher.
                sink.append(current)
            elif block_type == "TableGroup":
                nested: list[dict] = []
                walk(children, nested)
                sink.extend(nested or [current])
            else:
                walk(children, sink)

        walk(node, blocks)
        return blocks


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


class ChandraTableExtractor(TableExtractor):
    """
    Extrait les tableaux depuis la sortie JSON de l'API Chandra, dans ses deux formats.

    Format courant — le HTML brut du VLM, page par page :
    {
      "metadata": {"model": ..., "dpi": ...},
      "pages": [{"page": 1, "html": "<table><tr><td colspan='2'>…"}, ...]
    }

    Format historique — matrices de chaînes déjà aplaties par l'API :
    {
      "pages": [
        {"page": 1, "tables": [[["col1", "col2"], ["val1", "val2"]], ...]},
        ...
      ]
    }

    Le premier emprunte le parseur de marker et permet le recollage des blocs ; le second a
    tout perdu. Les deux restent lus, les JSON déjà déposés sur S3 étant au format
    historique. Cf. README.
    """

    def extract(self, data: dict) -> list[Table]:
        pages = data.get("pages", [])
        # Les blocs de toutes les pages avant d'en recoller aucune : les titres courants
        # ne se reconnaissent qu'à l'échelle du document.
        blocks = {i: _chandra_page_blocks(p["html"]) for i, p in enumerate(pages) if p.get("html")}
        titles = _running_titles(list(blocks.values()))

        tables = []
        for i, page in enumerate(pages):
            for table in self._page_tables(page, blocks.get(i), titles):
                normalized = _normalize_chandra_table(table)
                if normalized:
                    tables.append(normalized)
        return tables

    @staticmethod
    def _page_tables(page: dict, blocks: "list[_Block] | None", titles: set[str]) -> list[Table]:
        """Grilles d'une page, quel que soit le format de la sortie chandra.

        Args:
            page: entrée de `pages`, portant soit `html`, soit `tables`.
            blocks: blocs de la page, ou None pour le format historique.
            titles: titres courants du document.

        Returns:
            Les grilles brutes de la page, recollées quand le HTML dit qu'un tableau se
            poursuit d'un bloc à l'autre. La normalisation vient après, dans `extract`.
        """
        if blocks is not None:
            return _merge_chandra_blocks(blocks, titles)
        return [table for table in page.get("tables") or [] if table]


class OpenDataLoaderTableExtractor(TableExtractor):
    """
    Extrait les tableaux depuis la sortie HTML d'OpenDataLoader en mode hybrid docling-fast.
    Docling reconstruit les cellules individuellement → HTML avec vraies balises <table>/<td>.
    Réutilise le même parseur HTML que Marker.
    """

    def extract(self, data: str) -> list[Table]:
        return _parse_html_tables(data)


# ── Balisage des blocs chandra ────────────────────────────────────────────────

# Chandra coupe une région `Table` dès qu'un autre bloc l'interrompt : un tableau que
# traverse un intertitre ressort en plusieurs `<table>`. Le modèle n'annonce aucune
# continuation, mais son balisage dit lequel de ces blocs est un tableau entier — c'est ce
# que lisent `_Markup` et `_merge_chandra_blocks`. Mesures et règles : cf. README.


@dataclass
class _Markup:
    """Ce que le balisage d'un `<table>` dit de sa complétude.

    Attributes:
        has_column_header: plusieurs `th` sur une même ligne. Un `th` unique en `colspan`
            pleine largeur est un intertitre et ne compte pas (cf. README).
        has_data_row: le tableau porte au moins une ligne de `td`. Un bloc qui n'a que son
            en-tête est un tableau inachevé, que la suite de la page complète.
    """

    has_column_header: bool = False
    has_data_row: bool = False


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


def _read_markup(tags: list[list[tuple[str, int]]]) -> _Markup:
    """Lit le balisage d'un tableau à partir des balises de ses cellules.

    Args:
        tags: pour chaque ligne, la liste des (balise, colspan) de ses cellules.

    Returns:
        Le `_Markup` correspondant.
    """
    header = any(len(row) > 1 and all(tag == "th" for tag, _ in row) for row in tags)
    data = any(any(tag == "td" for tag, _ in row) for row in tags)
    return _Markup(has_column_header=header, has_data_row=data)


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


# ── Parser HTML interne (Marker) ──────────────────────────────────────────────


class _TableHTMLParser(HTMLParser):
    """Convertit un tableau HTML en matrice de chaînes, fusions comprises.

    Les fusions étant traitées ligne par ligne, une ligne dont le HTML compte moins de
    cellules que les autres ressort plus courte : c'est `_parse_html_tables` qui achève la
    grille en la passant par `_rectangularize`.

    `colspan` est développé en cellules vides à droite, `rowspan` reporté sur les lignes
    suivantes via `_carried`. Une cellule fusionnée ne porte sa valeur qu'à sa position
    d'origine, les continuations reçoivent une chaîne vide — dans les deux directions. Ce
    que ce choix vaut, mesures à l'appui : cf. README.
    """

    def __init__(self):
        super().__init__()
        self.tables: list[Table] = []
        # Parallèle à `tables` : ce que le balisage dit de chaque grille.
        self.markups: list[_Markup] = []
        self._rows: list[list[str]] = []
        self._row: list[str] = []
        self._cell: str = ""
        self._in_cell: bool = False
        self._colspan: int = 1
        self._rowspan: int = 1
        # {index de colonne: nombre de lignes que la fusion couvre encore}
        self._carried: dict[int, int] = {}
        # (balise, colspan) de chaque cellule, ligne par ligne, pour le tableau courant.
        self._tags: list[list[tuple[str, int]]] = []
        self._row_tags: list[tuple[str, int]] = []
        # Un `<table>` — et un `<tr>` — sont-ils ouverts ? Ni `_rows` ni `_row` ne le
        # disent : tous deux gardent le contenu du dernier élément clos, et les relire en
        # fin de flux le publierait une seconde fois.
        self._in_table: bool = False
        self._in_row: bool = False

    @staticmethod
    def _span(value) -> int:
        """Lit un attribut colspan/rowspan.

        Args:
            value: valeur brute de l'attribut, éventuellement absente ou invalide.

        Returns:
            L'entier lu, ramené à 1 s'il est absent, non numérique ou < 1.
        """
        try:
            span = int(value)
        except (ValueError, TypeError):
            return 1
        return max(span, 1)

    def _fill_carried(self) -> None:
        """Occupe les positions de la ligne courante tenues par un `rowspan` en cours."""
        while len(self._row) in self._carried:
            self._row.append("")

    def _close_row(self) -> None:
        """Termine la ligne courante : positions reportées restantes, puis décompte.

        Une cellule reportée peut se situer au-delà de la dernière cellule écrite dans
        le HTML — la ligne est alors complétée par des cellules vides jusqu'à elle,
        sinon la position serait décalée sur toutes les lignes suivantes.
        """
        if self._carried:
            last = max(self._carried)
            while len(self._row) <= last:
                self._fill_carried()
                if len(self._row) <= last and len(self._row) not in self._carried:
                    self._row.append("")
        self._fill_carried()

        for col in list(self._carried):
            self._carried[col] -= 1
            if self._carried[col] <= 0:
                del self._carried[col]

    def _open_row(self) -> None:
        """Ouvre une ligne, en lui pourvoyant les positions qu'une fusion occupe déjà."""
        self._row = []
        self._row_tags = []
        self._in_row = True
        # Une fusion verticale ouverte sur une ligne précédente occupe déjà le
        # début de celle-ci : ces positions sont pourvues avant la première cellule.
        self._fill_carried()

    def _end_row(self, implicite: bool = False) -> None:
        """Termine la ligne courante et la verse dans la grille.

        Args:
            implicite: la fermeture est déduite d'une balise ouvrante, non écrite dans le
                document. Une ligne implicite sans aucun contenu est alors **abandonnée
                sans décompter les fusions en cours** : elle vient d'un `<tr>` en double,
                pas d'une ligne du tableau (cf. README).
        """
        # Fin de ligne implique fin de cellule : sans cela, un `<td>` non refermé en fin de
        # ligne serait écrit dans la ligne suivante, qu'il décalerait d'un cran.
        self._close_cell()
        if implicite and not any(cellule.strip() for cellule in self._row):
            self._row, self._row_tags, self._in_row = [], [], False
            return
        self._close_row()
        self._in_row = False
        if self._row:
            self._rows.append(self._row)
            self._tags.append(self._row_tags)

    def _close_cell(self) -> None:
        """Termine la cellule courante et l'écrit dans la ligne, fusions comprises."""
        if not self._in_cell:
            return
        self._in_cell = False
        value = self._cell.strip()
        start = len(self._row)
        for offset in range(self._colspan):
            self._row.append(value if offset == 0 else "")
        if self._rowspan > 1:
            # Le compteur vaut le rowspan entier, pas rowspan - 1 : `_close_row`
            # décompte aussi la ligne de déclaration, et le report doit lui survivre
            # pour couvrir les rowspan - 1 lignes suivantes.
            for offset in range(self._colspan):
                self._carried[start + offset] = self._rowspan
        self._colspan = 1
        self._rowspan = 1
        self._fill_carried()

    # Les balises ouvrantes ci-dessous referment ce qui doit l'être, comme le fait
    # l'algorithme de parsing HTML5 : un `<tr>` ferme la ligne ouverte, un `<td>` la cellule
    # ouverte, et une cellule hors ligne en ouvre une. Pourquoi cette tolérance : cf. README.

    def handle_starttag(self, tag, attrs):
        if tag in ("th", "td"):
            self._close_cell()
            if not self._in_row:
                self._open_row()
            self._in_cell = True
            self._cell = ""
            attrs_dict = dict(attrs)
            self._colspan = self._span(attrs_dict.get("colspan", 1))
            self._rowspan = self._span(attrs_dict.get("rowspan", 1))
            self._row_tags.append((tag, self._colspan))
        elif tag == "br" and self._in_cell:
            self._cell += _BR
        elif tag == "tr":
            if self._in_row:
                self._end_row(implicite=True)
            self._open_row()
        elif tag == "table":
            self._rows = []
            self._tags = []
            self._carried = {}
            self._in_table = True

    def handle_endtag(self, tag):
        if tag in ("th", "td"):
            # Une balise fermante orpheline ne doit pas écrire de cellule fantôme.
            self._close_cell()
        elif tag == "tr":
            if self._in_row:
                self._end_row()
        elif tag == "table":
            self._publish_table()

    def handle_data(self, data):
        if self._in_cell:
            self._cell += data.replace("\n", " ")

    def _publish_table(self) -> None:
        """Verse le tableau courant dans `tables`, et referme son état."""
        self._close_cell()
        if self._in_row:
            self._end_row()
        self._in_table = False
        if not self._rows:
            return
        # Le découpage vient après le traitement des fusions : il ajoute des lignes,
        # et `_carried` compte en lignes du HTML.
        self.tables.append(_split_stacked_rows(self._rows))
        self.markups.append(_read_markup(self._tags))
        self._carried = {}

    def close(self):
        """Termine le parsing, en publiant un `<table>` que le document n'a pas fermé.

        Cas d'un HTML tronqué — annotation saisie à la main, sortie de VLM coupée par sa
        limite de jetons. Cf. README.
        """
        super().close()
        if self._in_table:
            self._publish_table()


def _parse_html_tables(html: str) -> list[Table]:
    """Parse un fragment HTML et retourne ses tableaux, chacun rectangulaire.

    Args:
        html: fragment HTML pouvant contenir plusieurs `<table>`.

    Returns:
        Une grille de chaînes par `<table>` rencontrée.
    """
    parser = _TableHTMLParser()
    parser.feed(html)
    parser.close()
    return [_normalize_grid(table) for table in parser.tables]


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


# ── Sérialisation ─────────────────────────────────────────────────────────────


def _to_csv_bytes(table: Table) -> bytes:
    buf = io.StringIO()
    csv.writer(buf, delimiter=";").writerows(table)
    return buf.getvalue().encode("utf-8-sig")


# ── Pipeline S3 ───────────────────────────────────────────────────────────────

# Le format d'une sortie tient au moteur qui l'a produite, pas au corpus : c'est parmi ces
# noms que la clé `extracteur` d'un moteur configuré choisit son lecteur.
EXTRACTORS: dict[str, TableExtractor] = {
    "marker": MarkerTableExtractor(),
    "opendataloader": OpenDataLoaderTableExtractor(),
    "chandra": ChandraTableExtractor(),
}


def _load(fs: s3fs.S3FileSystem, path: str, ext: str):
    """Charge un fichier S3 : renvoie un dict (JSON) ou une str (HTML)."""
    if ext == ".json":
        with fs.open(path, "rb") as f:
            return json.load(f)
    else:
        with fs.open(path, "r", encoding="utf-8") as f:
            return f.read()


def _stale_csv_paths(existing: list[str], siren: str, kept: int) -> list[str]:
    """Chemins des CSV d'un passage antérieur devenus surnuméraires.

    Une régénération produisant moins de tableaux qu'avant laisserait sinon les rangs
    excédentaires en place, et le dossier de sortie mélangerait deux générations de
    conversion.

    Args:
        existing: chemins présents dans le dossier de sortie.
        siren: radical du fichier source.
        kept: nombre de tableaux écrits par le passage courant.

    Returns:
        Les chemins `{siren}_{n}.csv` dont le rang dépasse `kept`. Les fichiers d'un autre
        radical ne sont jamais retournés, y compris quand un radical en préfixe un autre.
    """
    pattern = re.compile(rf"^{re.escape(siren)}_(\d+)\.csv$")
    stale = []
    for path in existing:
        match = pattern.match(Path(path).name)
        if match and int(match.group(1)) > kept:
            stale.append(path)
    return stale


def run_pipeline(method: str, fs: s3fs.S3FileSystem, overwrite: bool = False) -> None:
    """Convertit en CSV toutes les sorties brutes d'une méthode.

    Args:
        method: clé de `SOURCES`, c'est-à-dire un couple (corpus, moteur).
        fs: système de fichiers S3.
        overwrite: réécrire les CSV déjà présents, et supprimer les rangs surnuméraires.
    """
    cfg = SOURCES[method]
    ext = cfg.extension
    strip_prefix = cfg.retirer_prefixe
    extractor = EXTRACTORS[cfg.extracteur]

    input_files = sorted(fs.glob(f"{cfg.json}/*{ext}"))
    print(f"[{method}] {len(input_files)} fichier(s) trouvé(s)\n")

    ok = skipped = 0
    empty: list[str] = []
    failed: list[str] = []
    for file_key in input_files:
        raw_stem = Path(file_key).stem
        siren = raw_stem.removeprefix(strip_prefix) if strip_prefix else raw_stem
        if not overwrite and fs.exists(f"{cfg.csv}/{siren}_1.csv"):
            print(f"  [SKIP]  {siren}")
            skipped += 1
            continue

        try:
            data = _load(fs, file_key, ext)
            tables = extractor.extract(data)
        except Exception as e:
            print(f"  [ERR]   {siren}: {e}")
            failed.append(siren)
            continue

        if not tables:
            print(f"  [VIDE]  {siren}: aucun tableau détecté")
            empty.append(siren)
            continue

        for i, table in enumerate(tables, start=1):
            fs.pipe(f"{cfg.csv}/{siren}_{i}.csv", _to_csv_bytes(table))
        if overwrite:
            for path in _stale_csv_paths(fs.glob(f"{cfg.csv}/{siren}_*.csv"), siren, len(tables)):
                fs.rm(path)

        print(f"  [OK]    {siren}: {len(tables)} tableau(x)")
        ok += 1

    print(
        f"\n[{method}] terminé — {ok} traité(s), {skipped} ignoré(s), "
        f"{len(empty)} sans tableau, {len(failed)} en erreur"
    )
    # Un fichier sans tableau ne produit aucun CSV : il disparaît de l'évaluation, qui ne
    # compare que les paires existantes. Le nommer est le seul moyen de le voir.
    if empty:
        print(f"  sans tableau, donc absents de l'évaluation : {', '.join(empty)}")
    if failed:
        print(f"  en erreur de lecture ou de conversion : {', '.join(failed)}")
    if skipped:
        print(f"  sortie déjà présente, non régénérée : {skipped} fichier(s) — voir --overwrite")
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Conversion des sorties de moteurs en CSV")
    parser.add_argument(
        "--method",
        choices=[*SOURCES, "all"],
        default="all",
        help="Méthode à convertir, c'est-à-dire un couple corpus × moteur (défaut : all)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="énumérer les méthodes configurées, avec leurs préfixes S3",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Réécrire les sorties existantes et supprimer les rangs surnuméraires. "
            "Nécessaire dès que le code de conversion change, sans quoi les fichiers déjà "
            "convertis sont ignorés et le dossier mélange deux générations."
        ),
    )
    args = parser.parse_args()

    if args.list:
        largeur = max(len(m) for m in SOURCES)
        for nom, moteur in SOURCES.items():
            print(f"  {nom:<{largeur}}  [{moteur.corpus}]  {moteur.json}  →  {moteur.csv}")
        return

    fs = get_s3_fs()
    methods = list(SOURCES) if args.method == "all" else [args.method]
    for method in methods:
        run_pipeline(method, fs, overwrite=args.overwrite)


if __name__ == "__main__":
    main()
