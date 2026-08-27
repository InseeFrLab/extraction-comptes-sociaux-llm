#!/usr/bin/env python3
"""Fabrique les aperçus des documents sources, pour les pages « Comparaison » du site.

Le site montre deux grilles — l'annotation et l'extraction — sans jamais montrer ce dont
elles sont tirées. Un décalage de colonne ou un libellé abrégé ne se comprend souvent qu'en
regardant le scan : c'est ce que ces aperçus rendent possible.

Ils sont déposés sur S3 et **non régénérés à chaque build du site** : les sources pèsent
plusieurs gigaoctets (jusqu'à 889 mégapixels pour un seul TIFF du corpus historique), et la
CI ne fait plus que télécharger les JPEG déjà réduits.

    Comptes sociaux — une image par page du PDF source
      reprise/correspondances.parquet → reprise/apercus/{siren}_p{page}.jpg
    Tableaux historiques — une image par scan
      pdf/tableaux historiques/crop_{clé}.* → tableaux_historiques/apercus/{clé}.jpg

Le grand côté est ramené à `--cote-max` (défaut 2200 px), soit la résolution à laquelle le
VLM lit ces documents : l'aperçu montre donc ce que le moteur a vu, ce qui est exactement
ce qu'on veut pour un diagnostic.

Usage :
    uv run apercus.py --corpus all
    uv run apercus.py --corpus historiques --overwrite
    uv run apercus.py --corpus comptes-sociaux --cote-max 1600
"""

import argparse
import io

import pandas as pd
import pymupdf as fitz
import s3fs
from corpus_historiques import (
    BUCKET,
    IMAGE_EXTENSIONS,
    S3_IMAGES,
    key_from_image,
)
from extraction_common.s3 import get_s3_fs
from PIL import Image

S3_APERCUS_HISTORIQUES = f"{BUCKET}/tableaux_historiques/apercus"
S3_APERCUS_COMPTES = f"{BUCKET}/reprise/apercus"
S3_CORRESPONDANCES = f"{BUCKET}/reprise/correspondances.parquet"

# Grand côté de l'aperçu, en pixels, et qualité JPEG. Le couple est choisi pour que les
# 95 aperçus du site tiennent dans quelques dizaines de mégaoctets tout en restant lisibles
# sur un tableau dense : la page les charge de toute façon à la demande.
COTE_MAX = 2200
QUALITE = 78

# Ces scans dépassent la garde anti-bombe de Pillow (89 Mpx). Le corpus est déposé par nos
# soins, la garde n'a rien à protéger ici.
Image.MAX_IMAGE_PIXELS = None


def _en_jpeg(image: Image.Image, cote_max: int) -> bytes:
    """Réduit une image et l'encode en JPEG.

    Args:
        image: image source, mode quelconque.
        cote_max: grand côté maximal, en pixels. Une image plus petite n'est pas agrandie.

    Returns:
        Le JPEG en octets.
    """
    if image.mode != "RGB":
        image = image.convert("RGB")
    largeur, hauteur = image.size
    if max(largeur, hauteur) > cote_max:
        facteur = cote_max / max(largeur, hauteur)
        image = image.resize((round(largeur * facteur), round(hauteur * facteur)), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=QUALITE, optimize=True, progressive=True)
    return buffer.getvalue()


def _ecrire(fs: s3fs.S3FileSystem, chemin: str, contenu: bytes) -> None:
    """Dépose un aperçu sur S3 et journalise son poids."""
    fs.pipe(chemin, contenu)
    print(f"  -> {chemin.rsplit('/', 1)[-1]} ({len(contenu) / 1024:.0f} Ko)")


def apercus_historiques(fs: s3fs.S3FileSystem, cote_max: int, overwrite: bool) -> int:
    """Fabrique un aperçu par scan du corpus historique.

    Args:
        fs: système de fichiers S3.
        cote_max: grand côté maximal, en pixels.
        overwrite: refaire les aperçus déjà présents.

    Returns:
        Le nombre d'aperçus écrits.
    """
    sources = {
        key_from_image(p): p
        for p in sorted(fs.ls(S3_IMAGES, detail=False))
        if p.lower().endswith(IMAGE_EXTENSIONS)
    }
    print(f"[historiques] {len(sources)} image(s)\n")

    ecrits = 0
    for cle, chemin in sources.items():
        cible = f"{S3_APERCUS_HISTORIQUES}/{cle}.jpg"
        if not overwrite and fs.exists(cible):
            print(f"  [SKIP]  {cle}")
            continue
        print(f"  {cle}")
        with fs.open(chemin, "rb") as f:
            image = Image.open(io.BytesIO(f.read()))
        _ecrire(fs, cible, _en_jpeg(image, cote_max))
        ecrits += 1
    return ecrits


def apercus_comptes_sociaux(fs: s3fs.S3FileSystem, cote_max: int, overwrite: bool) -> int:
    """Fabrique un aperçu par page des PDF de comptes sociaux.

    L'aperçu est rattaché au document, pas au tableau : un PDF de deux pages en produit
    deux, et la page « Comparaison » les montre toutes pour le document dont vient le
    tableau affiché. Deviner quelle page porte le tableau de rang *n* demanderait une
    correspondance que le corpus ne fournit pas.

    Args:
        fs: système de fichiers S3.
        cote_max: grand côté maximal, en pixels.
        overwrite: refaire les aperçus déjà présents.

    Returns:
        Le nombre d'aperçus écrits.
    """
    with fs.open(S3_CORRESPONDANCES, "rb") as f:
        correspondances = pd.read_parquet(f)
    documents = correspondances[correspondances.pdf.notna() & correspondances.xlsx.notna()]
    print(f"[comptes-sociaux] {len(documents)} document(s)\n")

    ecrits = 0
    for _, ligne in documents.iterrows():
        siren = str(ligne["siren"])
        # Une seule sonde suffit : les pages d'un document sont écrites d'un bloc.
        if not overwrite and fs.exists(f"{S3_APERCUS_COMPTES}/{siren}_p1.jpg"):
            print(f"  [SKIP]  {siren}")
            continue
        print(f"  {siren}")
        with fs.open(ligne["pdf"].removeprefix("s3://"), "rb") as f:
            document = fitz.open(stream=f.read(), filetype="pdf")
        for numero, page in enumerate(document, start=1):
            # Le zoom vise directement le grand côté voulu : passer par un dpi supposerait
            # une taille de page, qui varie d'un document à l'autre.
            zoom = cote_max / max(page.rect.width, page.rect.height)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            image = Image.open(io.BytesIO(pixmap.tobytes("png")))
            _ecrire(fs, f"{S3_APERCUS_COMPTES}/{siren}_p{numero}.jpg", _en_jpeg(image, cote_max))
            ecrits += 1
        document.close()
    return ecrits


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        choices=["historiques", "comptes-sociaux", "all"],
        default="all",
        help="corpus à traiter (défaut : all)",
    )
    parser.add_argument(
        "--cote-max",
        type=int,
        default=COTE_MAX,
        help=f"grand côté de l'aperçu, en pixels (défaut : {COTE_MAX})",
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="refaire les aperçus déjà présents"
    )
    args = parser.parse_args()

    fs = get_s3_fs()
    total = 0
    if args.corpus in ("historiques", "all"):
        total += apercus_historiques(fs, args.cote_max, args.overwrite)
    if args.corpus in ("comptes-sociaux", "all"):
        total += apercus_comptes_sociaux(fs, args.cote_max, args.overwrite)
    print(f"\n{total} aperçu(s) écrit(s).")
    if not args.overwrite:
        print("  aperçus déjà présents non refaits — voir --overwrite")


if __name__ == "__main__":
    main()
