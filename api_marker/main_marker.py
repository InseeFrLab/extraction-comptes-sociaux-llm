from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
import shutil
import tempfile
import subprocess
import os
import glob
import json
import fitz 

app = FastAPI(
    root_path="/proxy/8000"
)

@app.post("/extract")
def extract(pdf: UploadFile = File(...)):
    if pdf.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Invalid file type. PDF required.")

    # Prepare temporary working directory
    with tempfile.TemporaryDirectory() as tmpdir:
        # Save uploaded PDF
        input_pdf_path = os.path.join(tmpdir, pdf.filename)
        with open(input_pdf_path, "wb") as f:
            shutil.copyfileobj(pdf.file, f)

        # Convert PDF to PNG 
        try:
            doc = fitz.open(input_pdf_path)
            page = doc.load_page(0)  # first page
            pix = page.get_pixmap()
            image_path = os.path.join(tmpdir, "page0.png")
            pix.save(image_path)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to convert PDF to image: {e}")

        # Run external CLI command with generated image
        cmd = [
            "marker_single", image_path,
            "--force_ocr",
            "--use_llm",
            "--llm_service", "marker.services.openai.OpenAIService",
            "--openai_base_url", "https://projet-extraction-tableaux-vllm.user.lab.sspcloud.fr/v1",
            "--openai_model", "google/gemma-3-27b-it",
            "--openai_api_key", "",
            "--timeout", "99999",
            "--output_dir", tmpdir,
            "--output_format", "json"
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            raise HTTPException(status_code=500, detail=f"Extraction failed: {e.stderr}")

        # Find output JSON file
        json_files = glob.glob(os.path.join(tmpdir, "*.json"))
        if not json_files:
            raise HTTPException(status_code=500, detail="No output JSON generated.")

        # Assuming single JSON
        output_path = json_files[0]
        with open(output_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        return JSONResponse(content=data)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)