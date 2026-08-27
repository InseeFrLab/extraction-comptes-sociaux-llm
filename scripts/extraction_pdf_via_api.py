#!/usr/bin/env python3
"""
Extraction des PDFs depuis S3 via les APIs d'extraction (api_marker ou api_opendataloader).

═══════════════════════════════════════════════════════════════
 DÉMARRAGE LOCAL DES APIs
═══════════════════════════════════════════════════════════════

  --- api_marker (LLM, port 8001) ---

  Terminal 1 — marker_proxy (port 1324, LLM proxy + Langfuse)
  ────────────────────────────────────────────────────────────
    cd api/marker_proxy
    uv run python -m uvicorn proxy:app --host 0.0.0.0 --port 1324 --app-dir src

  Terminal 2 — api_marker (port 8001)
  ────────────────────────────────────────────────────────────
    cd api/api_marker
    uv run python -m uvicorn main_marker:app --host 0.0.0.0 --port 8001 --app-dir src

  Variables d'environnement pour api_marker / marker_proxy :
    REAL_LLM_BASE_URL       URL du LLM (défaut: https://llm.lab.sspcloud.fr/v1)
    REAL_LLM_API_KEY        Clé API du LLM
    PROXY_URL               URL de marker_proxy (défaut: http://localhost:1324/v1)
    LANGFUSE_PUBLIC_KEY     (optionnel)
    LANGFUSE_SECRET_KEY     (optionnel)
    LANGFUSE_HOST           (optionnel)

  --- api_opendataloader (Java, port 8002) ---

  Terminal 1 — api_opendataloader (port 8002, nécessite Java 11+)
  ────────────────────────────────────────────────────────────
    cd api/api_opendataloader
    uvicorn main_opendataloader:app --host 0.0.0.0 --port 8002 --app-dir src

  --- api_chandra (VLM vllm, port 8003) ---

  Terminal 1 — api_chandra (port 8003)
  ────────────────────────────────────────────────────────────
    cd api/api_chandra
    uv run uvicorn main_chandra:app --host 0.0.0.0 --port 8003 --app-dir src

  Variables d'environnement pour api_chandra :
    CHANDRA_BASE_URL   URL vllm  (défaut: https://llm.lab.sspcloud.fr/api, sans /v1)
    CHANDRA_MODEL      Nom du modèle (défaut: chandra-ocr-2)
    CHANDRA_API_KEY    Clé API (défaut: EMPTY ; sur llm.lab, passer REAL_LLM_API_KEY)
    CHANDRA_DPI        Résolution PDF→image, en dpi (défaut: 200, fixe)

  Voir le docstring de api/api_chandra/src/main_chandra.py pour ce que le déploiement du
  modèle doit fournir de son côté (chat_template.jinja, défauts d'échantillonnage).

═══════════════════════════════════════════════════════════════
 USAGE DU SCRIPT
═══════════════════════════════════════════════════════════════

    # Avec api_marker (défaut)
    uv run extraction_pdf_via_api.py --from-parquet
    uv run extraction_pdf_via_api.py --pdf-key dossier/fichier.pdf

    # Avec api_chandra (VLM)
    uv run extraction_pdf_via_api.py --api chandra --from-parquet

    # Avec api_opendataloader
    uv run extraction_pdf_via_api.py --api opendataloader --from-parquet
    uv run extraction_pdf_via_api.py --api opendataloader --pdf-key dossier/fichier.pdf

    # Lister les PDFs S3
    uv run extraction_pdf_via_api.py --list

  Variables d'environnement du script :
    API_MARKER_URL              URL de api_marker       (défaut: http://localhost:8001)
    API_OPENDATALOADER_URL      URL de api_opendataloader (défaut: http://localhost:8002)
    AWS_S3_BUCKET
    AWS_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY
    AWS_SESSION_TOKEN           (optionnel)
    AWS_S3_ENDPOINT             (optionnel, ex: minio.lab.sspcloud.fr)
    AWS_REGION                  (défaut: us-east-1)
"""

import argparse
import json
import os

import pandas as pd
import requests
import s3fs
from dotenv import load_dotenv
from extraction_common.s3 import get_s3_fs

load_dotenv()

AWS_S3_BUCKET = os.getenv("AWS_S3_BUCKET")

API_URLS = {
    "marker": os.getenv("API_MARKER_URL", "http://localhost:8001"),
    "opendataloader": os.getenv("API_OPENDATALOADER_URL", "http://localhost:8002"),
    "chandra": os.getenv("API_CHANDRA_URL", "http://localhost:8003"),
}

S3_BASE = "s3://projet-extraction-tableaux"
PARQUET_PATH = f"{S3_BASE}/reprise/correspondances.parquet"
OUTPUT_PREFIXES = {
    "marker": "reprise/output_marker",
    "opendataloader": "reprise/output_opendataloader",
    "chandra": "reprise/output_chandra",
}
OUTPUT_EXTENSIONS = {
    "marker": ".json",
    "opendataloader": ".html",
    "chandra": ".json",
}


S3_BUCKET = S3_BASE.removeprefix("s3://")


def list_pdfs(fs: s3fs.S3FileSystem) -> list[str]:
    all_keys = fs.ls(S3_BUCKET, detail=False)
    return [k.removeprefix(f"{S3_BUCKET}/") for k in all_keys if k.lower().endswith(".pdf")]


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
    base_url = API_URLS[api]
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
    ext = OUTPUT_EXTENSIONS[api]
    path = f"{S3_BASE}/{OUTPUT_PREFIXES[api]}/{siren}{ext}"
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
    prefix = OUTPUT_PREFIXES[api]
    ext = OUTPUT_EXTENSIONS[api]
    for _, row in df_to_process.iterrows():
        siren = row["siren"]
        pdf_path = row["pdf"]
        output_path = f"{S3_BASE}/{prefix}/{siren}{ext}"

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
        choices=list(API_URLS),
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
            print(f"Aucun PDF trouvé dans {S3_BASE}")
            return
        print(f"\n{len(pdfs)} PDF(s) dans {S3_BASE} :\n")
        for k in pdfs:
            print(f"  {k}")
        return

    print(f"API sélectionnée : {args.api} ({API_URLS[args.api]})\n")

    if args.pdf_key:
        result = extract_pdf_via_api(fs, f"{S3_BASE}/{args.pdf_key}", args.api)
        if result:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    if args.from_parquet:
        process_from_parquet(fs, args.api)


if __name__ == "__main__":
    main()
