import os
from dotenv import load_dotenv
import logging
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Dict, Any
import fitz  # PyMuPDF

# --- Charger .env ---
load_dotenv()
PAPPERS_URL      = os.getenv("PAPPERS_COMPTES_URL", "https://api.pappers.fr/v2/entreprise/comptes")
PAPPERS_API_KEY  = os.getenv("PAPPERS_API_TOKEN")
LEGACY_SELECTOR  = os.getenv("LEGACY_SELECTOR_URL", "http://extraction-cs.lab.sspcloud.fr/select_page")
MARKER_API_URL   = os.getenv("MARKER_API_URL", "PLACEHOLDER")
FIXED_YEAR       = os.getenv("FIXED_YEAR", "2022") #pour pappers, pas de problème avec inpi

if not PAPPERS_API_KEY:
    raise RuntimeError("Vous devez définir PAPPERS_API_TOKEN dans le .env")

# --- FastAPI setup ---
app = FastAPI(
    title="API Centrale PDF→Marker",
    version="0.1.0",
    description="1 endpoint : PDF Pappers → select_page → Marker"
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Modèle de réponse ---
class ExtractionResponse(BaseModel):
    siren: str
    page: int
    marker: Dict[str, Any]

# --- Helpers ---
def fetch_pdf(siren: str) -> bytes:
    """Récupère un PDF (comptes annuels) depuis Pappers."""
    headers = {
        "api-key": PAPPERS_API_KEY,
        "Accept": "application/pdf"
    }
    params = {"siren": siren, "annee": FIXED_YEAR}
    r = requests.get(PAPPERS_URL, headers=headers, params=params, timeout=60)
    if r.status_code != 200:
        logger.error("Pappers PDF error %s: %s", r.status_code, r.text)
        raise HTTPException(502, "Impossible de récupérer le PDF Pappers")
    return r.content


def select_page(pdf: bytes) -> int:
    """Appelle le legacy selector pour n'obtenir qu'un numéro de page."""
    files = {"pdf_file": ("report.pdf", pdf, "application/pdf")}
    headers = {"accept": "application/json"}
    r = requests.post(LEGACY_SELECTOR, files=files, headers=headers, timeout=30)
    if r.status_code != 200:
        logger.error("Selector error %s: %s", r.status_code, r.text)
        raise HTTPException(502, "Sélection de la page échouée")
    data = r.json()
    if data.get("result") != "success" or "page_number" not in data:
        logger.error("Selector returned error: %s", data)
        raise HTTPException(502, "Le sélecteur a renvoyé une erreur")
    return data["page_number"]


def extract_page(pdf_bytes: bytes, page_number: int) -> bytes:
    """Extrait une seule page du PDF via PyMuPDF."""
    # Ouvre le PDF source
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    # Crée un nouveau document contenant uniquement la page désirée
    new_doc = fitz.open()
    new_doc.insert_pdf(doc, from_page=page_number - 1, to_page=page_number - 1)
    # Retourne en bytes
    snippet = new_doc.write()
    doc.close()
    new_doc.close()
    return snippet

# --- Endpoint unique ---
@app.get("/extract/{siren}", response_model=ExtractionResponse)
def extract(siren: str):
    """
    1) Télécharger le PDF Pappers
    2) Sélectionner la page
    3) Extraire la page avec PyMuPDF
    4) Envoyer au Marker
    5) Retourner le JSON avec numéro de page et résultat Marker
    """
    # 1. PDF
    pdf = fetch_pdf(siren)
    # 2. Sélecteur
    page = select_page(pdf)
    # 3. Extraction
    snippet = extract_page(pdf, page)
    # 4. Marker
    files = {"file": ("snippet.pdf", snippet, "application/pdf")}
    r = requests.post(MARKER_API_URL, files=files, timeout=60)
    if r.status_code != 200:
        logger.error("Marker error %s: %s", r.status_code, r.text)
        raise HTTPException(502, "Traitement Marker échoué")
    marker_data = r.json()

    # 5. Réponse
    return ExtractionResponse(siren=siren, page=page, marker=marker_data)
