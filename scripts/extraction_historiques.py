#!/usr/bin/env python3
"""
Extraction du corpus « tableaux historiques » : images scannées → JSON chandra, vers S3.

Ce corpus diffère de `reprise/` sur deux points, et c'est tout ce que ce script traite :

- **l'entrée est une image**, pas un PDF (TIFF ou PNG, un tableau par fichier, déjà rogné
  sur le tableau). Elle est postée telle quelle au champ `image` de `api_chandra`, qui
  l'envoie au VLM sans la faire passer par un PDF ;
- **plusieurs conditions sont comparées** : `chandra`, notre appel direct, et les variantes
  qui en bougent un réglage à la fois — prompt, plafond de sortie, relances. Elles écrivent
  dans des préfixes séparés (`MOTEURS`), et la suite de la chaîne les traite à l'identique ;
- **il n'y a pas de parquet de correspondances** : l'appariement avec l'annotation se fait
  par le nom de fichier (`crop_1970_bis.tiff` ↔ `ground_truth_1970_bis.html`).

    s3://projet-extraction-tableaux/pdf/tableaux historiques/*.{tiff,png}
      → s3://projet-extraction-tableaux/tableaux_historiques/output_chandra/{stem}.json

Le JSON déposé est celui de `api_chandra`, format inchangé : `scripts/json_to_csv.py
--method chandra_historiques` le convertit ensuite en CSV avec le même extracteur que le
corpus des comptes sociaux.

═══════════════════════════════════════════════════════════════
 DÉMARRAGE DE L'API
═══════════════════════════════════════════════════════════════

`api_chandra` relaie vers le VLM distant : aucun GPU local n'est nécessaire.

    cd api/api_chandra
    CHANDRA_API_KEY="$REAL_LLM_API_KEY" \
      uv run uvicorn main_chandra:app --host 127.0.0.1 --port 8003 --app-dir src

Aucune variable de résolution à poser : `CHANDRA_DPI` ne concerne que les PDF. Ce qui
décide de ce que le modèle voit est le nombre de pixels envoyés, et il est fixé ici par
`--cote-max`, requête par requête.

**Sur la résolution.** Les fichiers du corpus vont de 37 à 889 mégapixels et ne portent
aucune résolution exploitable — le tag TIFF vaut `(1, 1)`, le tag PNG `96 dpi`, quand il
existe. Un dpi n'est de toute façon pas définissable sur un rognage : il n'a pas de taille
physique. Le réglage est donc exprimé en pixels de grand côté, la seule grandeur que le
modèle consomme réellement.

═══════════════════════════════════════════════════════════════
 USAGE DU SCRIPT
═══════════════════════════════════════════════════════════════

    uv run extraction_historiques.py --all
    uv run extraction_historiques.py --all --moteur chandra_prompt_layout
    uv run extraction_historiques.py --all --overwrite     # rejoue les images déjà traitées
    uv run extraction_historiques.py --all --cote-max 0    # résolution native, sans réduction
    uv run extraction_historiques.py --image crop_1970.png
    uv run extraction_historiques.py --list                # inventaire et appariement

  Variables d'environnement :
    API_CHANDRA_URL   URL de api_chandra (défaut: http://localhost:8003)
    AWS_*             cf. extraction_common.s3
"""

import argparse
import os

import requests
import s3fs
from corpus_historiques import (
    IMAGE_EXTENSIONS,
    MOTEURS,
    S3_ANNOTATIONS,
    S3_IMAGES,
    key_from_annotation,
    key_from_image,
    stem,
)
from dotenv import load_dotenv
from extraction_common.s3 import get_s3_fs

load_dotenv()

API_URL = os.getenv("API_CHANDRA_URL", "http://localhost:8003")


def list_images(fs: s3fs.S3FileSystem) -> dict[str, str]:
    """Inventaire des images du corpus, indexé par clé de tableau.

    Args:
        fs: système de fichiers S3.

    Returns:
        {clé: chemin S3}, la clé étant le nom du fichier sans `crop_` ni extension.
    """
    return {
        key_from_image(p): p
        for p in sorted(fs.ls(S3_IMAGES, detail=False))
        if p.lower().endswith(IMAGE_EXTENSIONS)
    }


def list_annotations(fs: s3fs.S3FileSystem) -> dict[str, str]:
    """Inventaire des annotations HTML, indexé par clé de tableau.

    Args:
        fs: système de fichiers S3.

    Returns:
        {clé: chemin S3}, la clé étant le nom du fichier sans `ground_truth_` ni extension.
    """
    return {key_from_annotation(p): p for p in sorted(fs.glob(f"{S3_ANNOTATIONS}/*.html"))}


def _champs(moteur: str, cote_max: int, image_path: str) -> dict:
    """Champs de formulaire de la requête, selon ce que le moteur demande.

    Une clé absente laisse l'API décider : ni consigne, et le plafond de jetons et les
    relances de ses variables d'environnement. Une condition d'expérience se déclare donc
    entièrement dans `MOTEURS`, sans toucher à ce script.

    Args:
        moteur: clé de `MOTEURS`.
        cote_max: grand côté à envoyer, 0 pour la résolution native.
        image_path: chemin de l'image, pour les moteurs qui en dépendent.

    Returns:
        Le dict à passer en `data` de la requête.
    """
    cfg = MOTEURS[moteur]
    champs = {"cote_max": cote_max}
    champs.update(
        {k: cfg[k] for k in ("max_tokens", "prompt", "prompt_type", "max_retries") if k in cfg}
    )
    return champs


def extract_image(fs: s3fs.S3FileSystem, image_path: str, cote_max: int, moteur: str) -> str | None:
    """Soumet une image du corpus à `api_chandra`, telle qu'elle est stockée.

    Le fichier part au champ `image` de l'API, sans conversion locale : c'est l'API qui
    décode, réduit au besoin et encode pour le VLM. Le script n'a donc plus à ouvrir les
    scans, dont certains dépassent les 800 mégapixels.

    Args:
        fs: système de fichiers S3.
        image_path: chemin S3 de l'image.
        cote_max: grand côté maximal envoyé au modèle, en pixels ; 0 pour la résolution
            native.
        moteur: clé de `MOTEURS`.

    Returns:
        Le corps brut de la réponse (JSON sérialisé), ou None si l'appel a échoué.
    """
    with fs.open(image_path, "rb") as f:
        image_bytes = f.read()
    suffixe = image_path.rsplit(".", 1)[-1].lower()
    print(f"  image lue ({len(image_bytes):,} octets, .{suffixe})")

    try:
        response = requests.post(
            f"{API_URL}/extract",
            files={
                "image": (
                    image_path.rsplit("/", 1)[-1],
                    image_bytes,
                    # `image/tiff` et `image/png` : l'API ne vérifie que le préfixe `image/`,
                    # et c'est Pillow qui reconnaît le format sur le contenu.
                    f"image/{'tiff' if suffixe in ('tif', 'tiff') else suffixe}",
                )
            },
            data=_champs(moteur, cote_max, image_path),
            timeout=None,  # l'appel au VLM distant peut être long, et il est déjà retenté côté API
        )
    except requests.exceptions.ConnectionError:
        print(f"  [ERREUR] Impossible de joindre {API_URL}. L'API est-elle démarrée ?")
        return None

    if response.status_code != 200:
        print(f"  [ERREUR] L'API a répondu {response.status_code} : {response.text[:300]}")
        return None

    page = response.json()["pages"][0]
    source = page["pixels_source"]
    # L'API rapporte la taille qu'elle a réellement envoyée au modèle : c'est elle qui décide
    # de ce qu'il a vu, donc la seule grandeur qui rende deux extractions comparables.
    print(
        f"  {source[0]}×{source[1]} px, envoyés en {page['pixels'][0]}×{page['pixels'][1]} — "
        f"extraction réussie ({response.elapsed.total_seconds():.1f}s)"
    )
    return response.text


def process_all(fs: s3fs.S3FileSystem, cote_max: int, overwrite: bool, moteur: str) -> None:
    """Traite toutes les images du corpus, en ignorant celles déjà extraites.

    Args:
        fs: système de fichiers S3.
        cote_max: grand côté maximal envoyé au modèle, en pixels ; 0 pour la native.
        overwrite: rejouer les images dont le JSON existe déjà.
        moteur: clé de `MOTEURS`.
    """
    images = list_images(fs)
    sortie = MOTEURS[moteur]["json"]
    print(f"{len(images)} image(s) dans s3://{S3_IMAGES} — moteur {moteur}\n")

    ok = skipped = errors = 0
    for cle, image_path in images.items():
        output_path = f"{sortie}/{stem(image_path)}.json"

        if not overwrite and fs.exists(output_path):
            print(f"[SKIP]    {cle} — déjà traité")
            skipped += 1
            continue

        print(f"\n{'=' * 60}\n[TRAITEMENT] {cle}\n{'=' * 60}")
        content = extract_image(fs, image_path, cote_max, moteur)
        if content is None:
            errors += 1
            continue

        fs.pipe(output_path, content.encode("utf-8"))
        print(f"  -> {output_path}")
        ok += 1

    print(f"\n{'=' * 60}")
    print(f"Terminé : {ok} traité(s), {skipped} ignoré(s) (déjà faits), {errors} erreur(s)")
    if skipped and not overwrite:
        print("  sortie déjà présente, non régénérée — voir --overwrite")


def show_inventory(fs: s3fs.S3FileSystem) -> None:
    """Imprime l'inventaire du corpus et l'appariement image ↔ annotation."""
    images, annotations = list_images(fs), list_annotations(fs)
    cles = sorted(set(images) | set(annotations))
    print(f"{len(images)} image(s), {len(annotations)} annotation(s)\n")
    for cle in cles:
        image = "image" if cle in images else "  —  "
        annotation = "annotation" if cle in annotations else "    —     "
        print(f"  {cle:<16} {image}  {annotation}")

    orphelines = sorted(set(images) ^ set(annotations))
    if orphelines:
        print(f"\n[WARN] {len(orphelines)} fichier(s) sans vis-à-vis : {', '.join(orphelines)}")
    else:
        print("\nToutes les images ont leur annotation, et réciproquement.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Extraction du corpus « tableaux historiques »")
    groupe = parser.add_mutually_exclusive_group(required=True)
    groupe.add_argument("--list", action="store_true", help="inventaire et appariement du corpus")
    groupe.add_argument("--all", action="store_true", help="traiter toutes les images")
    groupe.add_argument("--image", metavar="NOM", help="traiter une seule image (nom du fichier)")
    parser.add_argument(
        "--cote-max",
        type=int,
        default=None,
        help=(
            "grand côté de l'image envoyée au modèle, en pixels ; 0 pour laisser le moteur "
            "dimensionner. Par défaut, la valeur du moteur choisi (cf. MOTEURS)."
        ),
    )
    parser.add_argument(
        "--moteur",
        choices=list(MOTEURS),
        default="chandra",
        help="moteur d'extraction (défaut : chandra, notre appel direct)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="rejouer les images dont le JSON de sortie existe déjà",
    )
    args = parser.parse_args()

    fs = get_s3_fs()
    cote_max = MOTEURS[args.moteur]["cote_max"] if args.cote_max is None else args.cote_max

    if args.list:
        show_inventory(fs)
        return

    if args.image:
        image_path = f"{S3_IMAGES}/{args.image}"
        if not fs.exists(image_path):
            raise SystemExit(f"Image introuvable : s3://{image_path}")
        content = extract_image(fs, image_path, cote_max, args.moteur)
        if content is None:
            raise SystemExit(1)
        output_path = f"{MOTEURS[args.moteur]['json']}/{stem(image_path)}.json"
        fs.pipe(output_path, content.encode("utf-8"))
        print(f"  -> {output_path}")
        return

    process_all(fs, cote_max, args.overwrite, args.moteur)


if __name__ == "__main__":
    main()
