from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
import httpx
import json
import os
from dotenv import load_dotenv
from typing import Any, Dict, Optional
from datetime import datetime
import logging
from langfuse import get_client

# Charger les variables d'environnement
load_dotenv()

# Configuration LLM réel
REAL_LLM_BASE_URL = os.getenv("REAL_LLM_BASE_URL")
REAL_LLM_API_KEY = os.getenv("REAL_LLM_API_KEY")

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# FastAPI & Langfuse client
app = FastAPI(
    title="LLM Proxy with Langfuse",
    version="1.0.0",
    description="Proxy for LLM requests with Langfuse tracing",
    root_path="/proxy",
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc"
)
langfuse = get_client()


def extract_usage_from_response(response_data: Dict) -> Optional[Dict]:
    """Extraire les informations d'usage de la réponse."""
    usage = response_data.get("usage")
    if not usage:
        return None
    return {
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def extract_content_from_response(response_data: Dict) -> Any:
    """Extraire le contenu de la réponse pour Langfuse."""
    choices = response_data.get("choices", [])
    if not choices:
        return response_data
    choice = choices[0]
    if "message" in choice:
        return choice["message"]
    if "delta" in choice:
        return choice["delta"]
    return choice


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """Proxy pour les chat-completions (streaming supporté)."""
    request_data = await request.json()
    model = request_data.get("model", "unknown")
    messages = request_data.get("messages", [])

    headers = {
        "Authorization": f"Bearer {REAL_LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    # Copier les headers de la requête originale (sauf Authorization/Host/Content-Length)
    for key, value in request.headers.items():
        if key.lower() not in ["authorization", "host", "content-length"]:
            headers[key] = value

    real_url = f"{REAL_LLM_BASE_URL.rstrip('/')}/v1/chat/completions"

    # Trace entière du proxy
    with langfuse.start_as_current_span(
        name="llm_proxy_chat",
        input={"model": model, "messages": messages},
    ) as root_span:
        root_span.update_trace(metadata={
            "proxy_version": "1.0.0",
            "timestamp": datetime.utcnow().isoformat(),
        })

        async with httpx.AsyncClient(timeout=300.0) as client:
            if request_data.get("stream", False):
                # Streaming : on crée la génération DANS le generator
                async def stream_proxy():
                    full_response = ""
                    usage_info = None

                    # Génération Langfuse contextuelle
                    with langfuse.start_as_current_generation(
                        name="completion",
                        model=model,
                        input=messages,
                        metadata={
                            "temperature": request_data.get("temperature"),
                            "max_tokens": request_data.get("max_tokens"),
                            "stream": True,
                        },
                    ) as gen:
                        async with client.stream(
                            "POST",
                            real_url,
                            headers=headers,
                            json=request_data,
                        ) as response:
                            if response.status_code != 200:
                                text = await response.aread()
                                logger.error(f"LLM error {response.status_code}: {text}")
                                raise HTTPException(
                                    status_code=response.status_code,
                                    detail=text.decode(),
                                )

                            async for line in response.aiter_lines():
                                if not line:
                                    continue
                                # OpenAI-style "data: " prefix
                                data_str = line.removeprefix("data: ").strip()
                                if data_str == "[DONE]":
                                    yield "data: [DONE]\n\n"
                                    break

                                try:
                                    chunk = json.loads(data_str)
                                    # Accumuler le contenu
                                    choice = chunk.get("choices", [{}])[0]
                                    delta = choice.get("delta", {})
                                    content = delta.get("content")
                                    if content:
                                        full_response += content
                                    # Capturer l’usage
                                    if "usage" in chunk:
                                        usage_info = extract_usage_from_response(chunk)
                                except json.JSONDecodeError:
                                    pass

                                yield f"data: {data_str}\n\n"

                        # À la fin du streaming, terminer la génération
                        gen.update(output=full_response, usage=usage_info)

                return StreamingResponse(
                    stream_proxy(),
                    media_type="text/plain",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                    },
                )

            else:
                # Non-streaming : simple POST
                response = await client.post(real_url, headers=headers, json=request_data)
                if response.status_code != 200:
                    logger.error(f"LLM error {response.status_code}: {response.text}")
                    raise HTTPException(response.status_code, response.text)

                data = response.json()
                content = extract_content_from_response(data)
                usage = extract_usage_from_response(data)

                # Tracer la génération LLM
                with langfuse.start_as_current_generation(
                    name="completion",
                    model=model,
                    input=messages,
                    metadata={
                        "temperature": request_data.get("temperature"),
                        "max_tokens": request_data.get("max_tokens"),
                        "stream": False,
                    },
                ) as gen:
                    gen.update(output=content, usage=usage)

                return data


@app.post("/v1/completions")
async def completions(request: Request):
    """Proxy pour les text-completions classiques."""
    request_data = await request.json()
    prompt = request_data.get("prompt", "")
    model = request_data.get("model", "unknown")

    headers = {
        "Authorization": f"Bearer {REAL_LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    for key, value in request.headers.items():
        if key.lower() not in ["authorization", "host", "content-length"]:
            headers[key] = value

    real_url = f"{REAL_LLM_BASE_URL.rstrip('/')}/v1/completions"

    with langfuse.start_as_current_span(
        name="llm_proxy_completion",
        input={"model": model, "prompt": prompt},
    ) as root_span:
        root_span.update_trace(metadata={
            "proxy_version": "1.0.0",
            "timestamp": datetime.utcnow().isoformat(),
        })

        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(real_url, headers=headers, json=request_data)
            if resp.status_code != 200:
                logger.error(f"LLM error {resp.status_code}: {resp.text}")
                raise HTTPException(resp.status_code, resp.text)

            data = resp.json()
            text = data.get("choices", [{}])[0].get("text", "")
            usage = extract_usage_from_response(data)

            with langfuse.start_as_current_generation(
                name="completion",
                model=model,
                input=[{"role": "user", "content": prompt}],
                metadata={"stream": False},
            ) as gen:
                gen.update(output=text, usage=usage)

            return data


@app.get("/v1/models")
async def list_models(request: Request):
    """Proxy pour lister les modèles disponibles."""
    headers = {"Authorization": f"Bearer {REAL_LLM_API_KEY}"}
    for key, value in request.headers.items():
        if key.lower() not in ["authorization", "host", "content-length"]:
            headers[key] = value

    real_url = f"{REAL_LLM_BASE_URL.rstrip('/')}/v1/models"
    async with httpx.AsyncClient() as client:
        resp = await client.get(real_url, headers=headers)
        return resp.json()


@app.get("/health")
async def health_check():
    """Vérification de santé du proxy."""
    return {
        "status": "ok",
        "langfuse_configured": True,
        "real_llm_configured": bool(REAL_LLM_API_KEY),
    }


@app.on_event("shutdown")
async def shutdown_event():
    """Flush avant arrêt."""
    langfuse.flush()
