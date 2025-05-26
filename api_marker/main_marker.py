from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
import shutil
import tempfile
import os
import json

from marker.converters.pdf import PdfConverter
from marker.models import create_model_dict
from marker.config.parser import ConfigParser

app = FastAPI(
    root_path="/proxy/8001" # Parametrage du root path pour coller au proxy du ssp cloud
)

@app.post("/extract")
def extract(pdf: UploadFile = File(...)):
    # Vérification du type
    if pdf.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Invalid file type. PDF required.")

    # Création d’un répertoire de travail temporaire
    with tempfile.TemporaryDirectory() as tmpdir:
        # Sauvegarde du PDF
        input_pdf_path = os.path.join(tmpdir, pdf.filename)
        with open(input_pdf_path, "wb") as f:
            shutil.copyfileobj(pdf.file, f)

        # Configuration de Marker pour produire du JSON et forcer l’OCR + LLM
        config = {
            "output_format": "json",
            "force_ocr": True,
            "use_llm": True,
            "llm_service": "marker.services.openai.OpenAIService",
            "openai_base_url": "https://llm.lab.sspcloud.fr/api",
            "openai_model": "gemma3:27b",
            "openai_api_key": "",
            "timeout": 99999,
        }
        parser = ConfigParser(config)

        # Instanciation du converter
        converter = PdfConverter(
            config=parser.generate_config_dict(),
            artifact_dict=create_model_dict(),
            processor_list=parser.get_processors(),
            renderer=parser.get_renderer(),
            llm_service=parser.get_llm_service()
        )

        # Exécution de la conversion
        try:
            rendered = converter(input_pdf_path)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Marker conversion failed: {e}")

        # Le rendu JSON complet
        result = rendered.dict()

        return JSONResponse(content=result)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
