#!/usr/bin/env python3
"""
Extraction des PDFs depuis S3 via les APIs d'extraction (marker, opendataloader, chandra).

L'API du moteur doit tourner : les commandes de démarrage, les variables d'environnement de
chaque service et les choix du pipeline sont dans [README.md](README.md).

Usage :
    # Avec api_marker (défaut)
    uv run extraction_pdf_via_api.py --from-parquet
    uv run extraction_pdf_via_api.py --pdf-key dossier/fichier.pdf

    # Avec api_chandra (VLM)
    uv run extraction_pdf_via_api.py --api chandra --from-parquet

    # Avec api_opendataloader
    uv run extraction_pdf_via_api.py --api opendataloader --from-parquet
    uv run extraction_pdf_via_api.py --api opendataloader --pdf-key dossier/fichier.pdf

    # Lister les PDFs du corpus
    uv run extraction_pdf_via_api.py --list

Les chemins S3, les moteurs et leurs URLs d'API sont déclarés dans
`config/comptes-sociaux.yaml` : rien de tout cela n'est en dur ici.
"""

import argparse
import json

import pandas as pd
import requests
import s3fs
from extraction_common.s3 import get_s3_fs

import config

# Chemins S3, moteurs et URLs d'API : tout vient de `config/comptes-sociaux.yaml`.
CONFIG = config.charger("comptes-sociaux")
# Seuls les moteurs dotés d'une API sont pilotables ici ; les autres ne sont plus produits.
MOTEURS = CONFIG.pilotes()
PARQUET_PATH = CONFIG.sources["correspondances"]
S3_PDF = CONFIG.sources["pdf"]


def list_pdfs(fs: s3fs.S3FileSystem) -> list[str]:
    """Clés des PDFs du corpus, relatives au bucket."""
    all_keys = fs.ls(S3_PDF, detail=False)
    return [k.removeprefix(f"{CONFIG.bucket}/") for k in all_keys if k.lower().endswith(".pdf")]


def extract_pdf_via_api(fs: s3fs.S3FileSystem, pdf_s3_path: str, api: str) -> str | None:
    """
    Télécharge un PDF depuis S3 et le soumet à l'API choisie via HTTP POST.
    Retourne le contenu brut de la réponse (JSON str pour Marker, HTML str pour OpenDataLoader).
    """
    print(f"  Lecture de {pdf_s3_path} ...")
    with fs.open(pdf_s3_path, "rb") as f:
        pdf_bytes = f.read()
    print(f"  PDF lu ({len(pdf_bytes):,} octets)")

    filename = pdf_s3_path.split("/")[-1]
    base_url = MOTEURS[api].api_url
    endpoint = f"{base_url}/extract"
    print(f"  Envoi à {endpoint} ...")

    try:
        response = requests.post(
            endpoint,
            files={"pdf": (filename, pdf_bytes, "application/pdf")},
            timeout=None,  # pas de timeout : l'extraction peut prendre 10-30 min
        )
    except requests.exceptions.ConnectionError:
        print(f"  [ERREUR] Impossible de joindre {base_url}. L'API est-elle démarrée ?")
        return None

    if response.status_code != 200:
        print(f"  [ERREUR] L'API a répondu {response.status_code} : {response.text[:300]}")
        return None

    print(f"  Extraction réussie ({response.elapsed.total_seconds():.1f}s)")
    return response.text


def save_output(fs: s3fs.S3FileSystem, siren: str, content: str, api: str):
    """Sauvegarde le résultat brut dans le préfixe S3 correspondant à l'API."""
    moteur = MOTEURS[api]
    path = f"{moteur.json}/{siren}{moteur.extension}"
    fs.pipe(path, content.encode("utf-8"))
    print(f"  -> Sauvegardé : {path}")


def process_from_parquet(fs: s3fs.S3FileSystem, api: str):
    """Lit le parquet de correspondances et traite les PDFs ayant un xlsx associé."""
    print(f"Lecture du fichier de correspondances : {PARQUET_PATH}")
    with fs.open(PARQUET_PATH, "rb") as f:
        df = pd.read_parquet(f)

    df_to_process = df[df["pdf"].notna() & df["xlsx"].notna()].copy()
    print(f"{len(df_to_process)} PDF(s) à traiter (avec xlsx associé)\n")

    ok, skipped, errors = 0, 0, 0
    moteur = MOTEURS[api]
    for _, row in df_to_process.iterrows():
        siren = row["siren"]
        pdf_path = row["pdf"]
        output_path = f"{moteur.json}/{siren}{moteur.extension}"

        if fs.exists(output_path):
            print(f"[SKIP]    {siren} — déjà traité")
            skipped += 1
            continue

        print(f"\n{'=' * 60}\n[TRAITEMENT] {siren}\n{'=' * 60}")
        content = extract_pdf_via_api(fs, pdf_path, api)

        if content is None:
            print(f"[ERREUR] Extraction échouée pour {siren}")
            errors += 1
            continue

        save_output(fs, siren, content, api)
        ok += 1

    print(f"\n{'=' * 60}")
    print(f"Terminé : {ok} traité(s), {skipped} ignoré(s) (déjà faits), {errors} erreur(s)")


def main():
    parser = argparse.ArgumentParser(
        description="Extraction des PDFs via API (marker ou opendataloader)"
    )
    parser.add_argument(
        "--api",
        choices=list(MOTEURS),
        default="marker",
        help="API à utiliser pour l'extraction (défaut: marker)",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="Lister les PDFs disponibles dans S3")
    group.add_argument(
        "--pdf-key",
        metavar="KEY",
        help="Chemin du PDF dans S3 (ex: dossier/fichier.pdf) — affiche le JSON en sortie",
    )
    group.add_argument(
        "--from-parquet",
        action="store_true",
        help="Traiter tous les PDFs de correspondances.parquet ayant un xlsx associé",
    )
    args = parser.parse_args()

    fs = get_s3_fs()

    if args.list:
        pdfs = list_pdfs(fs)
        if not pdfs:
            print(f"Aucun PDF trouvé dans s3://{S3_PDF}")
            return
        print(f"\n{len(pdfs)} PDF(s) dans s3://{S3_PDF} :\n")
        for k in pdfs:
            print(f"  {k}")
        return

    print(f"API sélectionnée : {args.api} ({MOTEURS[args.api].api_url})\n")

    if args.pdf_key:
        result = extract_pdf_via_api(fs, f"{CONFIG.bucket}/{args.pdf_key}", args.api)
        if result:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    if args.from_parquet:
        process_from_parquet(fs, args.api)


if __name__ == "__main__":
    main()
