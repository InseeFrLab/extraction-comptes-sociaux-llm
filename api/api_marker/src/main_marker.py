import os
import shutil
import tempfile
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

# Réduit la fragmentation de l'allocateur CUDA entre requêtes : sur un run long, la VRAM
# réservée se fragmente et une page dense peut OOM faute de bloc contigu, alors qu'elle
# tiendrait à froid. À définir AVANT toute initialisation du contexte CUDA (import torch).
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import torch
from data_management.extract_image_to_json import (
    MarkerConversionError,
    PdfConversionError,
    extract_pdf,
    load_models,
)
from dotenv import load_dotenv

load_dotenv()

# Valeur par défaut : marker_proxy en local
os.environ.setdefault("PROXY_URL", "http://localhost:1324/v1")

artifact_dict: dict = {}

# Sérialise l'inférence GPU : l'endpoint /extract est synchrone (exécuté dans le
# threadpool de FastAPI), donc deux requêtes concurrentes lanceraient les modèles
# surya en parallèle sur la même VRAM (contention + risque d'OOM sur une T4 16 Go).
# On traite un PDF à la fois sur le GPU ; les requêtes en excès attendent leur tour.
gpu_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global artifact_dict
    artifact_dict = load_models()
    yield


app = FastAPI(
    title="API Marker PDF Extraction",
    version="1.0.0",
    description="API for PDF processing using Marker",
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)


@app.post("/extract")
def extract(pdf: UploadFile = File(...)):
    # Vérification du type
    if pdf.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Invalid file type. PDF required.")

    # Création d'un répertoire de travail temporaire
    with tempfile.TemporaryDirectory() as tmpdir:
        # Sauvegarde du PDF
        input_pdf_path = os.path.join(tmpdir, pdf.filename)
        with open(input_pdf_path, "wb") as f:
            shutil.copyfileobj(pdf.file, f)

        try:
            with gpu_lock:
                try:
                    result = extract_pdf(input_pdf_path, tmpdir, artifact_dict)
                finally:
                    # Libère la VRAM réservée non utilisée entre deux PDFs pour limiter
                    # la fragmentation (cause d'OOM sur une page dense après plusieurs pages).
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
        except PdfConversionError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except MarkerConversionError as e:
            raise HTTPException(status_code=500, detail=str(e))

        return JSONResponse(content=result)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
