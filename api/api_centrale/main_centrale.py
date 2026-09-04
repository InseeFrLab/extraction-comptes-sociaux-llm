import json
import logging
import os
from io import BytesIO
from typing import Any

import requests
import s3fs
from dotenv import load_dotenv
from extraction_common.s3 import get_s3_fs
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from PyPDF2 import PdfReader, PdfWriter

# Charger .env
load_dotenv()

# INPI config (à définir dans .env)
INPI_USERNAME = os.getenv("INPI_USERNAME")
INPI_PASSWORD = os.getenv("INPI_PASSWORD")
INPI_LOGIN_URL = "https://registre-national-entreprises.inpi.fr/api/sso/login"
INPI_ATTACHMENTS_URL = (
    "https://registre-national-entreprises.inpi.fr/api/companies/{siren}/attachments"
)
INPI_DOWNLOAD_URL = "https://registre-national-entreprises.inpi.fr/api/bilans/{identifier}/download"

AWS_S3_BUCKET = os.getenv("AWS_S3_BUCKET")

# Vérifications
if not INPI_USERNAME or not INPI_PASSWORD:
    raise RuntimeError("Vous devez définir INPI_USERNAME et INPI_PASSWORD dans le .env")
if not AWS_S3_BUCKET:
    raise RuntimeError("Vous devez définir AWS_S3_BUCKET")

app = FastAPI(
    title="API Centrale PDF→Marker (INPI)",
    version="0.3.0",
    description="Endpoints : PDF INPI → select_page → Marker, liste des fichiers S3",
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Modèles de réponse
class ExtractionResponse(BaseModel):
    siren: str
    year: str
    page: int
    marker: dict[str, Any]


class S3FileListResponse(BaseModel):
    files: list[str]


# Auth INPI
class BearerAuth(requests.auth.AuthBase):
    def __init__(self, token: str):
        self.token = token

    def __call__(self, r: requests.PreparedRequest):
        r.headers["Authorization"] = f"Bearer {self.token}"
        return r


_token_cache: dict[str, Any] = {}


def get_inpi_token() -> str:
    resp = requests.post(
        INPI_LOGIN_URL, json={"username": INPI_USERNAME, "password": INPI_PASSWORD}, timeout=30
    )
    if resp.status_code != 200:
        logger.error("Échec auth INPI %s: %s", resp.status_code, resp.text)
        raise HTTPException(502, "Authentification INPI échouée")
    token = resp.json().get("token")
    if not token:
        logger.error("INPI n'a pas retourné de token : %s", resp.text)
        raise HTTPException(502, "Token INPI manquant")
    _token_cache["token"] = token
    return token


# Helpers S3


def file_exists_s3(fs: s3fs.S3FileSystem, filename: str) -> bool:
    return fs.exists(f"{AWS_S3_BUCKET}/{filename}")


def upload_to_s3(fs: s3fs.S3FileSystem, filename: str, content: bytes):
    fs.pipe(f"{AWS_S3_BUCKET}/{filename}", content)


# Download PDF INPI


def fetch_pdf_inpi(siren: str, year: str) -> bytes:
    token = get_inpi_token()
    url = INPI_ATTACHMENTS_URL.format(siren=siren)
    resp = requests.get(url, auth=BearerAuth(token), timeout=30)
    if resp.status_code != 200:
        logger.error("INPI attachments error %s: %s", resp.status_code, resp.text)
        raise HTTPException(502, "Impossible de récupérer la liste des actes INPI")
    docs = resp.json()

    actes = docs.get("bilans", [])
    candidats = [a for a in actes if a.get("dateDepot", "").startswith(str(year))]
    if not candidats:
        raise HTTPException(404, f"Aucun acte INPI trouvé pour l'année {year}")
    identifier = candidats[0].get("id")
    logger.info("Document identifier : %s", identifier)

    dl_url = INPI_DOWNLOAD_URL.format(identifier=identifier)
    r = requests.get(dl_url, auth=BearerAuth(token), timeout=60)
    if r.status_code != 200:
        logger.error("INPI download error %s: %s", r.status_code, r.text)
        raise HTTPException(502, "Téléchargement du PDF INPI échoué")
    return r.content


# Sélection et extraction de page


def select_page(pdf: bytes) -> int:
    files = {"pdf_file": ("report.pdf", pdf, "application/pdf")}
    headers = {"accept": "application/json"}
    r = requests.post(os.getenv("LEGACY_SELECTOR_URL"), files=files, headers=headers, timeout=120)
    if r.status_code != 200:
        logger.error("Selector error %s: %s", r.status_code, r.text)
        raise HTTPException(502, "Sélection de la page échouée")
    data = r.json()
    if data.get("result") != "success" or "page_number" not in data:
        logger.error("Selector returned error: %s", data)
        raise HTTPException(502, "Le sélecteur a renvoyé une erreur")
    logger.info("Page selected : %s", data["page_number"])
    return data["page_number"]


def extract_page(pdf_bytes: bytes, page_number: int) -> bytes:
    """
    Extrait une page spécifique d'un PDF sous forme d'un nouveau PDF.

    :param pdf_bytes: Le PDF source en bytes
    :param page_number: Le numéro de la page à extraire (0-indexed)
    :return: Bytes du PDF résultant contenant seulement la page spécifiée
    """
    pdf_reader = PdfReader(BytesIO(pdf_bytes))

    if page_number < 0 or page_number >= len(pdf_reader.pages):
        raise ValueError("Invalid page number")

    pdf_writer = PdfWriter()
    pdf_writer.add_page(pdf_reader.pages[page_number])

    output_stream = BytesIO()
    pdf_writer.write(output_stream)

    logger.info("Page extracted succesfully")
    return output_stream.getvalue()


# Endpoint extraction
@app.get("/extract/{siren}", response_model=ExtractionResponse)
def extract(siren: str, year: str = Query(..., description="Année du bilan à récupérer")):
    fs = get_s3_fs()
    filename = f"{siren}_{year}.json"

    if False:  # file_exists_s3(fs, filename):
        logger.info(f"Le fichier {filename} existe déjà sur S3.")
        raw = fs.open(f"{AWS_S3_BUCKET}/{filename}", "rb").read()
        data = json.loads(raw)
        page = data.get("page", -1)
        marker_data = data.get("marker", {})
    else:
        pdf = fetch_pdf_inpi(siren, year)
        page = select_page(pdf)
        snippet = extract_page(pdf, page)

        files = {"pdf": ("snippet.pdf", snippet, "application/pdf")}
        r = requests.post(os.getenv("MARKER_API_URL"), files=files, timeout=120)
        if r.status_code != 200:
            logger.error("Marker error %s: %s", r.status_code, r.text)
            raise HTTPException(502, "Traitement Marker échoué")
        marker_data = r.json()

        # upload_to_s3(fs, filename, json.dumps({"page": page, "marker": marker_data}).encode())
        logger.info(f"Résultat {filename} ajouté à S3.")

    return ExtractionResponse(siren=siren, year=year, page=page, marker=marker_data)


# Endpoint liste fichiers S3
@app.get("/files", response_model=S3FileListResponse)
def list_s3_files():
    fs = get_s3_fs()
    try:
        keys = fs.ls(AWS_S3_BUCKET)
        files = [
            key.split("/", 1)[1] if key.startswith(f"{AWS_S3_BUCKET}/") else key for key in keys
        ]
        return S3FileListResponse(files=files)
    except Exception as e:
        logger.error("Erreur d'accès à S3 : %s", str(e))
        raise HTTPException(500, "Impossible de lister les fichiers S3")
