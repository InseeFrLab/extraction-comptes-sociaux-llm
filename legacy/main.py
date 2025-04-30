from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
from typing import Optional
import httpx
import uuid

app = FastAPI()


@app.post("/extraction")
async def extraction_endpoint(
    siren: Optional[str] = Form(None),
    pdf: Optional[UploadFile] = File(None)
):
    if siren:
        # Appel API Pappers/INPI avec le numéro SIREN
        async with httpx.AsyncClient() as client:
            try:
                # Remplace l'URL par l'endpoint réel de Pappers ou INPI
                response = await client.get(f"https://api.pappers.fr/v2/entreprise?siren={siren}&api_token=TOKEN")
                data = response.json()
                return JSONResponse(content={
                    "message": "Données récupérées depuis Pappers",
                    "data": data
                })
            except Exception as e:
                return JSONResponse(content={"error": str(e)}, status_code=500)

    elif pdf:
        # Envoie du PDF vers l'API Marker (ex: OCR ou VLLM)
        file_id = str(uuid.uuid4())
        async with httpx.AsyncClient() as client:
            try:
                files = {'file': (pdf.filename, await pdf.read(), pdf.content_type)}
                # Remplace par l'URL réelle de ton API Marker
                response = await client.post("http://localhost:8001/marker", files=files)
                data = response.json()
                return JSONResponse(content={
                    "message": "PDF traité par l'API Marker",
                    "pdf_id": file_id,
                    "result": data
                })
            except Exception as e:
                return JSONResponse(content={"error": str(e)}, status_code=500)

    return JSONResponse(
        content={"error": "Veuillez fournir un numéro SIREN ou un fichier PDF"},
        status_code=400
    )
