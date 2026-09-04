"""Parseur HTML interne : un fragment → une grille par `<table>`, fusions comprises.

Le parseur sert les trois moteurs — marker et opendataloader rendent du HTML, chandra
aussi dans son format courant. Il est volontairement tolérant, comme un navigateur : cf.
[README.md](../README.md), section « Étape 2 ».
"""

from dataclasses import dataclass
from html.parser import HTMLParser

from .grid import _BR, Table, _normalize_grid, _split_stacked_rows

# Le balisage sert au recollage des blocs chandra (cf. `chandra.py`) : il dit lequel des
# `<table>` d'une page est un tableau entier, là où le modèle n'annonce aucune
# continuation. Mesures et règles : cf. README.


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

    def _end_row(self, implicit: bool = False) -> None:
        """Termine la ligne courante et la verse dans la grille.

        Args:
            implicit: la fermeture est déduite d'une balise ouvrante, non écrite dans le
                document. Une ligne implicite sans aucun contenu est alors **abandonnée
                sans décompter les fusions en cours** : elle vient d'un `<tr>` en double,
                pas d'une ligne du tableau (cf. README).
        """
        # Fin de ligne implique fin de cellule : sans cela, un `<td>` non refermé en fin de
        # ligne serait écrit dans la ligne suivante, qu'il décalerait d'un cran.
        self._close_cell()
        if implicit and not any(cell.strip() for cell in self._row):
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
                self._end_row(implicit=True)
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
