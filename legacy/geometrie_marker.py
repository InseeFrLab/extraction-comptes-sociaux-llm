#!/usr/bin/env python3
"""
Diagnostic géométrique des tableaux marker, à partir des `bbox` déjà déposés sur S3.

Le JSON de marker (`output_format: "json"`) porte un `bbox` et un `polygon` par bloc,
cellules de tableau comprises. `json_to_csv.py` n'en lit rien : il ne regarde que le
`html`. Ce script confronte les deux lectures, et les compare à l'annotation :

- la **grille géométrique** — les cellules regroupées en lignes par leurs coordonnées ;
- la **grille HTML** — celle que produit la conversion et qui finit en CSV ;
- l'**annotation** XLSX, quand elle existe pour ce rang.

Deux questions, celles du diagnostic publié :

1. le nombre de colonnes vu par la géométrie est-il celui du HTML ? Un écart signale une
   structure perdue par la mise en forme HTML du moteur, et non par la conversion ;
2. la hauteur d'en-tête lue sur les coordonnées prédit-elle mieux celle de l'annotation
   que `detect_column_header_height`, qui la devine sur le texte ? C'est le levier 01,
   celui dont dépendent 20 points de récupération numérique.

Aucun appel au GPU ni au LLM : les coordonnées sont déjà dans les fichiers.

**Archivé.** Cette analyse a été menée à son terme — son résultat est publié par la page
« Améliorations » du site — et ne fait plus partie de la chaîne. Elle importe `evaluation`
et `json_to_csv` de `scripts/`, d'où le `PYTHONPATH` ci-dessous.

Usage (depuis la racine du dépôt) :
    PYTHONPATH=scripts uv run --project scripts python legacy/geometrie_marker.py
    ... legacy/geometrie_marker.py --recouvrement 0.3
    ... legacy/geometrie_marker.py --save    (dépose aussi le détail en parquet sur S3)
"""

import argparse
import json
import re
from collections import Counter

import pandas as pd
import s3fs
from evaluation import S3_ANNOTATIONS, _load_xlsx, detect_column_header_height
from extraction_common.s3 import get_s3_fs

# Fonctions de la conversion, réutilisées telles quelles : le diagnostic doit décrire la
# grille que le pipeline produit vraiment, pas une réimplémentation qui divergerait.
from json_to_csv import MarkerTableExtractor, _is_numeric_cell, _parse_html_tables

BUCKET = "projet-extraction-tableaux"
S3_MARKER_JSON = f"{BUCKET}/reprise/output_marker"
S3_OUTPUT = f"{BUCKET}/reprise/eval/geometrie_marker.parquet"

# Deux cellules appartiennent à la même ligne si leurs étendues verticales se recouvrent
# d'au moins cette part de la plus petite des deux. La moitié suffit à séparer des lignes
# voisines tout en absorbant le léger travers des pages numérisées.
DEFAULT_OVERLAP = 0.5

# Marker positionne ses cellules mais ne les dimensionne pas toujours : sur 69 des 78
# tableaux du corpus, la cellule médiane mesure 1 × 1 point. Les positions restent
# exploitables — le regroupement en bandes fonctionne — mais la forme des cellules ne dit
# alors plus rien, et l'axe des lignes ne peut plus s'en déduire.
MIN_CELL_SIZE = 5.0

_TAG_RE = re.compile(r"<[^>]+>")


def _cell_text(cell: dict) -> str:
    """Texte d'une cellule, balises retirées.

    Args:
        cell: bloc `TableCell` du JSON marker.

    Returns:
        Le texte de la cellule, espaces normalisés.
    """
    return " ".join(_TAG_RE.sub(" ", cell.get("html") or "").split())


def bands_along(
    cells: list[dict], axis: int, min_overlap: float = DEFAULT_OVERLAP
) -> list[list[dict]]:
    """Regroupe les cellules en bandes le long d'un axe, par recouvrement.

    Le critère est le recouvrement, non l'écart entre coordonnées de début. Un critère
    d'écart chaîne : sur un tableau de 48 lignes légèrement de travers, chaque cellule est
    à moins de quelques points de la suivante dans l'ordre trié, et les 478 cellules
    finissent dans une seule bande. Ici la bande courante se **réduit** à l'intersection
    des cellules qu'elle accueille, ce qui interdit toute dérive.

    Une cellule fusionnée sur deux lignes recouvre entièrement la première : elle y est
    rattachée, et la bande ne s'étend pas jusqu'à la seconde. C'est la convention de la
    conversion, où une fusion ne porte sa valeur qu'à son origine.

    Args:
        cells: blocs `TableCell` d'un tableau.
        axis: 0 pour regrouper sur x (lignes d'une page en paysage), 1 pour y.
        min_overlap: part de la plus petite des deux étendues qui doit être commune.

    Returns:
        Les bandes, dans l'ordre croissant de la coordonnée.
    """
    lo_i, hi_i = axis, axis + 2
    bands: list[tuple[float, float, list[dict]]] = []
    for cell in sorted(cells, key=lambda c: c["bbox"][lo_i]):
        lo, hi = cell["bbox"][lo_i], cell["bbox"][hi_i]
        if bands:
            band_lo, band_hi, members = bands[-1]
            common = min(hi, band_hi) - max(lo, band_lo)
            if common > 0 and common >= min_overlap * min(hi - lo, band_hi - band_lo):
                members.append(cell)
                bands[-1] = (max(lo, band_lo), min(hi, band_hi), members)
                continue
        bands.append((lo, hi, [cell]))
    return [members for _, _, members in bands]


def _regularity(rows: list[list[dict]]) -> float:
    """Part des bandes qui comptent le nombre de cellules le plus fréquent.

    Args:
        rows: bandes de cellules.

    Returns:
        Un score entre 0 et 1. Une grille régulière vaut 1 : toutes ses lignes ont le
        même nombre de cellules. Le score mesure la qualité du regroupement, il ne sert
        pas à choisir l'axe — les deux axes d'une grille bien formée sont réguliers.
    """
    if not rows:
        return 0.0
    counts = Counter(len(r) for r in rows)
    return counts.most_common(1)[0][1] / len(rows)


def row_axis(cells: list[dict]) -> int:
    """Axe le long duquel les lignes du tableau se succèdent.

    Il n'est pas connu d'avance : une page numérisée en paysage et non redressée donne un
    tableau dont les lignes s'empilent sur x. La régularité du regroupement ne permet pas
    de trancher — une grille bien formée est régulière dans les deux sens. La forme des
    cellules, elle, est dissymétrique : une colonne est plus large qu'une ligne n'est
    haute, donc une cellule est plus large que haute dans le sens de lecture.

    Encore faut-il que les cellules aient une taille. Quand elles sont dégénérées, le
    rapport de forme n'est que du bruit — il a fait lire deux tableaux de `411373525` en
    travers, 12×3 au lieu de 3×12 — et la page est supposée droite, cas de loin le plus
    fréquent.

    Args:
        cells: blocs `TableCell` porteurs d'un `bbox`.

    Returns:
        1 si les lignes se succèdent sur y (page droite), 0 si elles se succèdent sur x
        (page en paysage).
    """
    width, height = median_cell_size(cells)
    if min(width, height) < MIN_CELL_SIZE:
        return 1
    return 1 if width >= height else 0


def median_cell_size(cells: list[dict]) -> tuple[float, float]:
    """Largeur et hauteur médianes des cellules, en points.

    Args:
        cells: blocs `TableCell` porteurs d'un `bbox`.

    Returns:
        Le couple (largeur, hauteur). Sert à choisir l'axe des lignes, et à écarter les
        tableaux dont les boîtes sont dégénérées : sous `MIN_CELL_SIZE`, elles ne décrivent
        plus de grille et le diagnostic doit s'abstenir plutôt que publier du bruit.
    """
    widths = sorted(c["bbox"][2] - c["bbox"][0] for c in cells)
    heights = sorted(c["bbox"][3] - c["bbox"][1] for c in cells)
    median = len(cells) // 2
    return widths[median], heights[median]


def rows_from_geometry(
    cells: list[dict], min_overlap: float = DEFAULT_OVERLAP
) -> tuple[int, list[list[dict]]]:
    """Reconstruit les lignes d'un tableau à partir des seules coordonnées.

    Args:
        cells: blocs `TableCell` d'un tableau, tous porteurs d'un `bbox`.
        min_overlap: recouvrement minimal pour deux cellules d'une même ligne.

    Returns:
        L'axe retenu (0 = x, 1 = y) et les bandes de cellules, dans l'ordre croissant de
        la coordonnée. Chaque bande est une ligne du tableau.
    """
    axis = row_axis(cells)
    return axis, bands_along(cells, axis, min_overlap)


def _data_band(row: list[dict]) -> bool:
    """Une bande porte-t-elle des données, c'est-à-dire au moins deux nombres ?"""
    return sum(1 for c in row if _is_numeric_cell(_cell_text(c))) >= 2


def _declared_header_band(row: list[dict]) -> bool:
    """Une bande est-elle déclarée en-tête, c'est-à-dire faite surtout de `<th>` ?

    Marker distingue `<th>` de `<td>` cellule par cellule. La conversion traite les deux
    de la même façon et perd donc cette déclaration, alors qu'elle répond directement à la
    question à laquelle `detect_column_header_height` doit répondre par une heuristique de
    texte : où s'arrête l'en-tête ?
    """
    if not row:
        return False
    th = sum(1 for c in row if (c.get("html") or "").lstrip().startswith("<th"))
    return th * 2 >= len(row)


def header_height_declared(rows: list[list[dict]]) -> int:
    """Nombre de bandes de tête déclarées `<th>`, lues depuis l'extrémité la plus fournie.

    Args:
        rows: bandes de cellules, dans l'ordre croissant de la coordonnée.

    Returns:
        Le nombre de bandes consécutives déclarées en-tête à cette extrémité.
    """
    flags = [_declared_header_band(r) for r in rows]
    if all(flags):
        return 0  # un tableau tout en `<th>` ne déclare rien d'utile
    from_start = next(i for i, is_header in enumerate(flags) if not is_header)
    from_end = next(i for i, is_header in enumerate(reversed(flags)) if not is_header)
    return max(from_start, from_end)


def header_height_from_geometry(rows: list[list[dict]]) -> int:
    """Nombre de bandes d'en-tête, lues depuis l'extrémité qui en porte le plus.

    Les coordonnées ne disent pas dans quel sens se lit le tableau — une page en paysage
    peut l'être dans les deux sens. L'en-tête est donc cherché aux deux extrémités, et
    c'est la plus fournie qui l'emporte.

    Args:
        rows: bandes de cellules, dans l'ordre croissant de la coordonnée.

    Returns:
        Le nombre de bandes sans données consécutives à cette extrémité, ou 0 si le
        tableau ne porte aucune donnée — auquel cas la notion d'en-tête n'a pas de sens.
    """
    flags = [_data_band(r) for r in rows]
    if not any(flags):
        return 0
    from_start = next(i for i, is_data in enumerate(flags) if is_data)
    from_end = next(i for i, is_data in enumerate(reversed(flags)) if is_data)
    return max(from_start, from_end)


def describe_table(cells: list[dict], grid: list[list[str]], min_overlap: float) -> dict:
    """Compare la structure géométrique d'un tableau à celle de sa grille HTML.

    Le nombre de colonnes est celui des bandes de l'autre axe, et non le nombre de
    cellules d'une ligne : une cellule fusionnée sur deux colonnes n'occupe qu'une position
    dans sa bande, alors que la grille HTML compte les positions après développement des
    fusions. Compter les cellules rendrait les deux mesures incomparables.

    Args:
        cells: blocs `TableCell` du bloc `Table`.
        grid: grille produite par la conversion pour ce même bloc.
        min_overlap: recouvrement minimal pour deux cellules d'une même bande.

    Returns:
        Un dict de mesures : axe retenu, régularité, dimensions et hauteur d'en-tête vues
        par la géométrie puis par le HTML.
    """
    axis, rows = rows_from_geometry(cells, min_overlap)
    columns = bands_along(cells, 1 - axis, min_overlap)
    sizes = Counter(len(r) for r in rows)
    width, height = median_cell_size(cells)
    return {
        "axe_lignes": "x" if axis == 0 else "y",
        "bbox_fiable": min(width, height) >= MIN_CELL_SIZE,
        "cellule_med": f"{width:.0f}×{height:.0f}",
        "regularite": round(_regularity(rows), 3),
        "n_cellules": len(cells),
        "n_lignes_geom": len(rows),
        "n_colonnes_geom": len(columns),
        "cellules_par_ligne": sizes.most_common(1)[0][0] if sizes else 0,
        "hauteur_entete_geom": header_height_from_geometry(rows),
        "hauteur_entete_th": header_height_declared(rows),
        "n_lignes_html": len(grid),
        "n_colonnes_html": max((len(r) for r in grid), default=0),
        "hauteur_entete_html": detect_column_header_height(pd.DataFrame(grid, dtype=str)),
    }


# ── Parcours du corpus ────────────────────────────────────────────────────────


def _table_cells(block: dict) -> list[dict]:
    """Cellules d'un bloc `Table` qui portent une boîte englobante."""
    return [
        c
        for c in (block.get("children") or [])
        if c.get("block_type") == "TableCell" and c.get("bbox")
    ]


def _annotations(fs: s3fs.S3FileSystem) -> dict[str, str]:
    """Chemin de chaque annotation, indexé par son stem (`{siren}_{rang}`)."""
    return {
        p.rsplit("/", 1)[-1].removesuffix(".xlsx"): p for p in fs.glob(f"{S3_ANNOTATIONS}/*.xlsx")
    }


def diagnose_file(data: dict, min_overlap: float) -> list[dict]:
    """Mesure chaque tableau d'un JSON marker.

    Le rang suit l'ordre des blocs, donc la numérotation des CSV : c'est ainsi que
    l'évaluation apparie `{stem}_{rang}.xlsx` et `{stem}_{rang}.csv`, et la comparaison
    doit porter sur le même appariement.

    Args:
        data: JSON marker complet.
        min_overlap: recouvrement minimal pour deux cellules d'une même bande.

    Returns:
        Une mesure par tableau produit, dans l'ordre des rangs.
    """
    rows = []
    rank = 0
    for block in MarkerTableExtractor().table_blocks(data):
        grids = _parse_html_tables(block.get("html") or "")
        cells = _table_cells(block)
        for grid in grids:
            rank += 1
            mesures = {"rang": rank, "bloc": block.get("id")}
            mesures.update(
                describe_table(cells, grid, min_overlap)
                if cells
                else {
                    "axe_lignes": None,
                    "n_lignes_html": len(grid),
                    "n_colonnes_html": max((len(r) for r in grid), default=0),
                }
            )
            rows.append(mesures)
    return rows


def run(min_overlap: float, save: bool) -> pd.DataFrame:
    """Mesure tout le corpus marker et affiche la synthèse.

    Args:
        min_overlap: recouvrement minimal pour deux cellules d'une même bande.
        save: déposer aussi le détail en parquet sur S3.

    Returns:
        Le détail, une ligne par tableau.
    """
    fs = get_s3_fs()
    annotations = _annotations(fs)
    files = sorted(fs.glob(f"{S3_MARKER_JSON}/*.json"))
    print(f"{len(files)} fichier(s) marker, {len(annotations)} annotation(s)\n")

    records = []
    for key in files:
        stem = key.rsplit("/", 1)[-1].removesuffix(".json")
        with fs.open(key, "rb") as f:
            data = json.load(f)
        for mesures in diagnose_file(data, min_overlap):
            nom = f"{stem}_{mesures['rang']}"
            mesures |= {"fichier": stem, "nom": nom}
            path = annotations.get(nom)
            if path:
                ann = _load_xlsx(fs, path)
                mesures |= {
                    "n_lignes_ann": len(ann),
                    "n_colonnes_ann": ann.shape[1],
                    "hauteur_entete_ann": detect_column_header_height(ann),
                }
            records.append(mesures)

    df = pd.DataFrame(records)
    _print_summary(df)
    if save:
        _save_parquet(fs, df)
    return df


def _print_summary(df: pd.DataFrame) -> None:
    """Affiche la synthèse des deux questions du diagnostic."""
    geom = df[df["axe_lignes"].notna()]
    print(f"— {len(df)} tableaux, dont {len(geom)} avec des cellules géolocalisées")
    # Les positions restent exploitables même sans taille : c'est l'axe des lignes qui
    # devient indécidable, et la page est alors supposée droite.
    sans_taille = (~geom["bbox_fiable"]).sum()
    print(f"  cellules dimensionnées : {len(geom) - sans_taille}/{len(geom)}, sinon axe supposé")
    print(f"  axe des lignes : {dict(Counter(geom['axe_lignes']))}")
    # La régularité compte les cellules émises : une fusion en fait perdre une à sa ligne,
    # donc un tableau à fusions est légitimement irrégulier. Ce n'est pas un défaut.
    print(f"  lignes de longueur homogène : {(geom['regularite'] == 1.0).sum()}/{len(geom)}")

    accord = geom["n_colonnes_geom"] == geom["n_colonnes_html"]
    lignes = geom["n_lignes_geom"] == geom["n_lignes_html"]
    print("\n1. Structure géométrique == structure HTML :")
    print(f"   colonnes {accord.sum()}/{len(geom)}, lignes {lignes.sum()}/{len(geom)}")
    ecarts = geom[~accord | ~lignes]
    if not ecarts.empty:
        print("   tableaux discordants — la grille HTML ne dit pas ce que disent les cellules :")
        for _, r in ecarts.iterrows():
            print(
                f"     {r['nom']:<28} géométrie {r['n_lignes_geom']:>3}×{r['n_colonnes_geom']:<3}"
                f" HTML {r['n_lignes_html']:>3}×{r['n_colonnes_html']:<3}"
            )

    pairs = (
        geom[geom["hauteur_entete_ann"].notna()] if "hauteur_entete_ann" in geom else geom.iloc[:0]
    )
    if pairs.empty:
        print("\n2. Aucune annotation appariée : pas de comparaison possible.")
        return

    col_geom = (pairs["n_colonnes_geom"] == pairs["n_colonnes_ann"]).sum()
    col_html = (pairs["n_colonnes_html"] == pairs["n_colonnes_ann"]).sum()
    print(f"\n   sur {len(pairs)} tableaux appariés à une annotation :")
    print(f"   colonnes exactes — géométrie {col_geom}, HTML {col_html}")

    print("\n2. Hauteur d'en-tête, écart à l'annotation :")
    ecarts = {
        source: (pairs[f"hauteur_entete_{source}"] - pairs["hauteur_entete_ann"]).abs()
        for source in ("html", "geom", "th")
    }
    libelles = {
        "html": "texte (detect_column_header_height)",
        "geom": "géométrie (bandes sans données)",
        "th": "déclaration <th> de marker",
    }
    for source, ecart in ecarts.items():
        print(
            f"   {libelles[source]:<38} exacte {(ecart == 0).sum():>2}/{len(pairs)}"
            f", écart moyen {ecart.mean():.2f}"
        )
    for source in ("geom", "th"):
        mieux = (ecarts[source] < ecarts["html"]).sum()
        pire = (ecarts[source] > ecarts["html"]).sum()
        print(f"   {source} contre texte : mieux sur {mieux}, moins bien sur {pire}")
    print(
        "\n   Attention : la référence est `detect_column_header_height` appliqué à\n"
        "   l'annotation, donc l'heuristique de texte est comparée à elle-même sur une\n"
        "   autre grille. Le tableau ci-dessus mesure sa cohérence, pas sa justesse, et\n"
        "   ne peut pas départager les trois sources. Il y faudrait des hauteurs\n"
        "   d'en-tête annotées à la main."
    )


def _save_parquet(fs: s3fs.S3FileSystem, df: pd.DataFrame) -> None:
    with fs.open(S3_OUTPUT, "wb") as f:
        df.to_parquet(f, index=False)
    print(f"\nDétail déposé : s3://{S3_OUTPUT}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnostic géométrique des tableaux marker")
    parser.add_argument(
        "--recouvrement",
        type=float,
        default=DEFAULT_OVERLAP,
        help=f"recouvrement minimal de deux cellules d'une même ligne (défaut : {DEFAULT_OVERLAP})",
    )
    parser.add_argument("--save", action="store_true", help="déposer le détail en parquet sur S3")
    args = parser.parse_args()
    run(args.recouvrement, args.save)


if __name__ == "__main__":
    main()
