"""Un lecteur par format de sortie de moteur : marker, chandra, opendataloader.

Le format d'une sortie tient au moteur qui l'a produite, pas au corpus : c'est parmi les
clés d'`EXTRACTORS` que la clé `extracteur` d'un moteur configuré choisit son lecteur.
Ajouter un moteur qui rend l'un de ces formats ne demande rien ici (cf. `config/*.yaml`).
"""

from abc import ABC, abstractmethod

from .chandra import (
    _Block,
    _chandra_page_blocks,
    _merge_chandra_blocks,
    _normalize_chandra_table,
    _running_titles,
)
from .grid import Table
from .html_tables import _parse_html_tables

# Sortie brute d'un moteur, telle que `pipeline._load` la rend : un dict pour un `.json`,
# une str pour un `.html`. Chaque extracteur n'en reçoit que la forme que son moteur
# produit, l'extension et l'extracteur étant déclarés ensemble dans la configuration.
RawOutput = dict | str


class TableExtractor(ABC):
    @abstractmethod
    def extract(self, data: RawOutput) -> list[Table]:
        """Extraire les tableaux d'une sortie de moteur.

        Args:
            data: sortie brute chargée depuis S3.

        Returns:
            Une matrice de chaînes par tableau, dans l'ordre de lecture du document.
        """


class MarkerTableExtractor(TableExtractor):
    """
    Extrait les tableaux HTML imbriqués produits par l'API Marker.
    Parcourt récursivement les blocs de type Table / TableGroup.
    """

    def extract(self, data: RawOutput) -> list[Table]:
        """Grilles du document, dans l'ordre de lecture.

        Args:
            data: JSON marker (dict), arborescence de blocs.

        Returns:
            Une grille par `<table>` des blocs de tableau retenus.
        """
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

    def extract(self, data: RawOutput) -> list[Table]:
        """Grilles du document, recollage des blocs compris.

        Args:
            data: JSON chandra (dict), dans l'un ou l'autre de ses deux formats.

        Returns:
            Les grilles normalisées, celles sans ligne de données écartées.
        """
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
    def _page_tables(page: dict, blocks: list[_Block] | None, titles: set[str]) -> list[Table]:
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

    def extract(self, data: RawOutput) -> list[Table]:
        """Grilles du document.

        Args:
            data: HTML du document (str), tel que déposé par le service.

        Returns:
            Une grille par `<table>` du document.
        """
        return _parse_html_tables(data)


EXTRACTORS: dict[str, TableExtractor] = {
    "marker": MarkerTableExtractor(),
    "opendataloader": OpenDataLoaderTableExtractor(),
    "chandra": ChandraTableExtractor(),
}
