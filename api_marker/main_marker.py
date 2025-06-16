from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
import shutil
import tempfile
import os
import json
from dotenv import load_dotenv
from marker.converters.pdf import PdfConverter
from marker.models import create_model_dict
from marker.config.parser import ConfigParser
import fitz  # PyMuPDF
from PIL import Image
import io

app = FastAPI(
    root_path="/marker" # Parametrage du root path pour coller au proxy du ssp cloud
)

def pdf_to_image(pdf_path, output_dir, dpi=300):
    """
    Convertit un PDF monopage en image
    
    Args:
        pdf_path (str): Chemin vers le fichier PDF
        output_dir (str): Répertoire de sortie pour l'image
        dpi (int): Résolution de l'image (défaut: 300 DPI)
    
    Returns:
        str: Chemin vers l'image générée
    """
    try:
        # Ouvrir le PDF
        pdf_document = fitz.open(pdf_path)
        
        # Vérifier que c'est bien un PDF monopage
        if len(pdf_document) != 1:
            raise ValueError(f"Le PDF contient {len(pdf_document)} pages. Seuls les PDFs monopages sont supportés.")
        
        # Récupérer la première (et unique) page
        page = pdf_document[0]
        
        # Définir la matrice de transformation pour la résolution
        mat = fitz.Matrix(dpi/72, dpi/72)
        
        # Convertir la page en image
        pix = page.get_pixmap(matrix=mat)
        
        # Convertir en PIL Image
        img_data = pix.tobytes("png")
        img = Image.open(io.BytesIO(img_data))
        
        # Sauvegarder l'image
        image_filename = os.path.splitext(os.path.basename(pdf_path))[0] + ".png"
        image_path = os.path.join(output_dir, image_filename)
        img.save(image_path, "PNG", optimize=True)
        
        # Fermer le document PDF
        pdf_document.close()
        
        return image_path
        
    except Exception as e:
        raise Exception(f"Erreur lors de la conversion PDF vers image: {str(e)}")

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
        
        # Conversion du PDF en image
        try:
            image_path = pdf_to_image(input_pdf_path, tmpdir)
            print(f"PDF converti en image: {image_path}")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Erreur de conversion PDF vers image: {str(e)}")
        
        # Configuration de Marker pour produire du JSON et forcer l'OCR + LLM
        config = {
            "output_format": "json",
            "force_ocr": True,
            "use_llm": True,
            "llm_service": "marker.services.openai.OpenAIService",
            "openai_base_url": "https://llm.lab.sspcloud.fr/api",
            "openai_model": "gemma3:27b",
            "openai_api_key": os.getenv("LAB_LLM_API_KEY"),
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
        
        # Exécution de la conversion (on continue à utiliser le PDF original pour Marker)
        try:
            rendered = converter(input_pdf_path)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Marker conversion failed: {e}")
        
        # Le rendu JSON complet
        result = rendered.dict()
        
        # Ajout des informations sur l'image générée dans la réponse
        result["image_info"] = {
            "image_generated": True,
            "image_filename": os.path.basename(image_path),
            "image_size_bytes": os.path.getsize(image_path)
        }
        
        return JSONResponse(content=result)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)