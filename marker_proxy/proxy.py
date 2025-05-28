from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
import httpx
import json
import os
from dotenv import load_dotenv
from typing import Any, Dict, Optional
from langfuse import Langfuse
import uuid
from datetime import datetime
import asyncio
import logging

# Configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="LLM Proxy with Langfuse", version="1.0.0")

load_dotenv()

# Configuration Langfuse
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST")

# Configuration du LLM réel
REAL_LLM_BASE_URL = os.getenv("REAL_LLM_BASE_URL")
REAL_LLM_API_KEY = os.getenv("REAL_LLM_API_KEY")
logger.info("LLM_BASE_URL : %s", REAL_LLM_BASE_URL)

# Initialiser Langfuse
langfuse = Langfuse(
    public_key=LANGFUSE_PUBLIC_KEY,
    secret_key=LANGFUSE_SECRET_KEY,
    host=LANGFUSE_HOST
)

class LangfuseTracker:
    def __init__(self):
        self.traces = {}
    
    def create_trace(self, request_data: Dict[str, Any]) -> str:
        """Créer une nouvelle trace Langfuse"""
        trace_id = str(uuid.uuid4())
        
        # Extraire les métadonnées de la requête
        model = request_data.get("model", "unknown")
        messages = request_data.get("messages", [])
        
        # Créer la trace
        trace = langfuse.trace(
            id=trace_id,
            name="llm_completion",
            metadata={
                "model": model,
                "proxy_version": "1.0.0",
                "timestamp": datetime.now().isoformat()
            }
        )
        
        # Créer la génération
        generation = trace.generation(
            name="completion",
            model=model,
            input=messages,
            metadata={
                "temperature": request_data.get("temperature"),
                "max_tokens": request_data.get("max_tokens"),
                "stream": request_data.get("stream", False)
            }
        )
        
        self.traces[trace_id] = {
            "trace": trace,
            "generation": generation,
            "start_time": datetime.now()
        }
        
        return trace_id
    
    def finish_trace(self, trace_id: str, response_data: Any, usage: Optional[Dict] = None):
        """Finaliser une trace avec la réponse"""
        if trace_id not in self.traces:
            return
        
        trace_info = self.traces[trace_id]
        generation = trace_info["generation"]
        
        # Calculer la durée
        end_time = datetime.now()
        duration = (end_time - trace_info["start_time"]).total_seconds()
        
        # Finaliser la génération
        generation.end(
            output=response_data,
            usage=usage,
            metadata={
                "duration_seconds": duration,
                "end_time": end_time.isoformat()
            }
        )
        
        # Nettoyer
        del self.traces[trace_id]

tracker = LangfuseTracker()

def extract_usage_from_response(response_data: Dict) -> Optional[Dict]:
    """Extraire les informations d'usage de la réponse"""
    usage = response_data.get("usage")
    if usage:
        return {
            "input": usage.get("prompt_tokens"),
            "output": usage.get("completion_tokens"),
            "total": usage.get("total_tokens")
        }
    return None

def extract_content_from_response(response_data: Dict) -> Any:
    """Extraire le contenu de la réponse pour Langfuse"""
    choices = response_data.get("choices", [])
    if choices:
        choice = choices[0]
        if "message" in choice:
            return choice["message"]
        elif "delta" in choice:
            return choice["delta"]
    return response_data

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """Proxy pour les completions chat (multimodal supporté)"""
    try:
        # Lire le body de la requête
        request_data = await request.json()
        
        # Créer une trace Langfuse
        trace_id = tracker.create_trace(request_data)
        logger.info(f"Trace créée: {trace_id}")
        
        # Préparer la requête vers le LLM réel
        headers = {
            "Authorization": f"Bearer {REAL_LLM_API_KEY}",
            "Content-Type": "application/json"
        }
        
        # Copier les headers de la requête originale (sauf Authorization)
        for key, value in request.headers.items():
            if key.lower() not in ["authorization", "host", "content-length"]:
                headers[key] = value
        
        # URL du LLM réel
        real_url = f"https://llm.lab.sspcloud.fr/api/chat/completions"
        
        async with httpx.AsyncClient(timeout=300.0) as client:
            if request_data.get("stream", False):
                # Gestion du streaming
                async def stream_proxy():
                    full_response = ""
                    usage_info = None
                    
                    async with client.stream(
                        "POST", 
                        real_url, 
                        headers=headers, 
                        json=request_data
                    ) as response:
                        if response.status_code != 200:
                            error_text = await response.aread()
                            logger.error(f"Erreur LLM: {response.status_code} - {error_text}")
                            raise HTTPException(
                                status_code=response.status_code,
                                detail=f"Erreur du LLM: {error_text.decode()}"
                            )
                        
                        async for chunk in response.aiter_lines():
                            if chunk:
                                chunk_str = chunk.decode('utf-8')
                                if chunk_str.startswith("data: "):
                                    data_str = chunk_str[6:]  # Enlever "data: "
                                    
                                    if data_str.strip() == "[DONE]":
                                        yield f"data: [DONE]\n\n"
                                        break
                                    
                                    try:
                                        chunk_data = json.loads(data_str)
                                        
                                        # Accumuler la réponse pour Langfuse
                                        choices = chunk_data.get("choices", [])
                                        if choices and "delta" in choices[0]:
                                            content = choices[0]["delta"].get("content", "")
                                            if content:
                                                full_response += content
                                        
                                        # Capturer l'usage si présent
                                        if "usage" in chunk_data:
                                            usage_info = extract_usage_from_response(chunk_data)
                                        
                                        yield f"data: {data_str}\n\n"
                                        
                                    except json.JSONDecodeError:
                                        yield f"data: {data_str}\n\n"
                    
                    # Finaliser la trace après le streaming
                    tracker.finish_trace(trace_id, full_response, usage_info)
                
                return StreamingResponse(
                    stream_proxy(),
                    media_type="text/plain",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
                )
            
            else:
                # Requête non-streaming
                response = await client.post(
                    real_url,
                    headers=headers,
                    json=request_data
                )
                
                if response.status_code != 200:
                    logger.error(f"Erreur LLM: {response.status_code} - {response.text}")
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=f"Erreur du LLM: {response.text}"
                    )
                
                response_data = response.json()
                
                # Extraire les données pour Langfuse
                content = extract_content_from_response(response_data)
                usage = extract_usage_from_response(response_data)
                
                # Finaliser la trace
                tracker.finish_trace(trace_id, content, usage)
                
                return response_data
    
    except Exception as e:
        logger.error(f"Erreur dans le proxy: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erreur interne: {str(e)}")

@app.post("/v1/completions")
async def completions(request: Request):
    """Proxy pour les completions texte classiques"""
    try:
        request_data = await request.json()
        trace_id = tracker.create_trace({"messages": [{"role": "user", "content": request_data.get("prompt", "")}], **request_data})
        
        headers = {
            "Authorization": f"Bearer {REAL_LLM_API_KEY}",
            "Content-Type": "application/json"
        }
        
        for key, value in request.headers.items():
            if key.lower() not in ["authorization", "host", "content-length"]:
                headers[key] = value
        
        real_url = f"{REAL_LLM_BASE_URL.rstrip('/')}/v1/completions"
        
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(real_url, headers=headers, json=request_data)
            
            if response.status_code != 200:
                raise HTTPException(status_code=response.status_code, detail=response.text)
            
            response_data = response.json()
            usage = extract_usage_from_response(response_data)
            content = response_data.get("choices", [{}])[0].get("text", "")
            
            tracker.finish_trace(trace_id, content, usage)
            
            return response_data
    
    except Exception as e:
        logger.error(f"Erreur dans le proxy completions: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erreur interne: {str(e)}")

@app.get("/v1/models")
async def list_models(request: Request):
    """Proxy pour lister les modèles disponibles"""
    headers = {"Authorization": f"Bearer {REAL_LLM_API_KEY}"}
    real_url = f"{REAL_LLM_BASE_URL.rstrip('/')}/v1/models"
    
    async with httpx.AsyncClient() as client:
        response = await client.get(real_url, headers=headers)
        return response.json()

@app.get("/health")
async def health_check():
    """Vérification de santé du proxy"""
    return {
        "status": "ok",
        "langfuse_configured": bool(LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY),
        "real_llm_configured": bool(REAL_LLM_API_KEY)
    }

@app.on_event("shutdown")
async def shutdown_event():
    """Nettoyer lors de l'arrêt"""
    langfuse.flush()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=1324)