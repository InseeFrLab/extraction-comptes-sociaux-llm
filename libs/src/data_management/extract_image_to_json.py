import json
import os
import time
import traceback

import torch
from marker.config.parser import ConfigParser
from marker.converters.pdf import PdfConverter
from marker.models import create_model_dict

from data_management.pdf_to_image import pdf_to_image


class PdfConversionError(Exception):
    """Erreur lors de la conversion PDF -> image."""

    pass


class MarkerConversionError(Exception):
    """Erreur lors de la conversion Marker."""

    pass


def load_models() -> dict:
    """Charge les modèles Marker une seule fois au démarrage."""
    return create_model_dict()


def _is_cuda_oom(exc: BaseException) -> bool:
    """Détecte un OOM CUDA, y compris emballé dans une exception générique.

    surya/marker rattrape parfois l'OOM et le relève en RuntimeError « ordinaire », donc
    un simple `isinstance(exc, torch.cuda.OutOfMemoryError)` ne suffit pas : on remonte la
    chaîne __cause__/__context__ et on cherche aussi la signature dans le message.
    """
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, torch.cuda.OutOfMemoryError):
            return True
        if "out of memory" in str(exc).lower():
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def extract_pdf(pdf_path: str, tmpdir: str, artifact_dict: dict) -> dict:
    """Logique métier d'extraction, indépendante de FastAPI."""

    start = time.time()

    # Conversion du PDF en image
    try:
        image_path = pdf_to_image(pdf_path, tmpdir)
        print(f"PDF converti en image: {image_path}")
    except Exception as e:
        raise PdfConversionError(f"Erreur de conversion PDF vers image: {e}") from e

    # Détection du device : GPU si CUDA disponible, sinon CPU
    use_gpu = torch.cuda.is_available()
    print(f"Device marker : {'cuda (' + torch.cuda.get_device_name(0) + ')' if use_gpu else 'cpu'}")

    # Batch sizes selon le device.
    # NB : le LLM (use_llm) est déporté sur le marker_proxy (PROXY_URL), donc le GPU
    # local ne sert qu'aux modèles surya -> on a de la marge VRAM sur les 16 Go de l'A2.
    if use_gpu:
        # Réglages conservateurs pour NVIDIA A2 16 Go (14,61 Go utilisables), modèles surya
        # uniquement en local (le LLM est déporté sur le marker_proxy).
        #
        # IMPORTANT : la VRAM ne dépend pas que du batch, mais aussi de la TAILLE des images.
        # Sur de vrais scans de comptes sociaux (grandes images haute résolution), chaque crop
        # de ligne est lourd : un batch 96 a saturé les 14,61 Go (OOM, 14,42 Go alloués par
        # PyTorch) sur une page de ~200 lignes. On revient donc à une valeur conservatrice qui
        # tient sur les pages denses réelles, quitte à perdre un peu de débit (la correction
        # OCR reste massivement plus rapide qu'en CPU).
        # En cas d'OOM, le serveur loggue l'état VRAM : baisser encore recognition (24, 16) ;
        # s'il reste de la marge sur tes documents, on peut remonter prudemment vers 48.
        batch_sizes = {
            "detection_batch_size": 6,
            "recognition_batch_size": 32,
            "layout_batch_size": 6,
            "table_rec_batch_size": 6,
            "equation_batch_size": 4,
        }
    else:
        # Réglages conservateurs pour CPU
        batch_sizes = {
            "detection_batch_size": 6,
            "recognition_batch_size": 64,
            "layout_batch_size": 6,
            "table_rec_batch_size": 6,
            "equation_batch_size": 6,
        }

    # Configuration Marker
    config = {
        "output_format": "json",
        # force_ocr=False : marker décide page par page. Les pages scan/image (sans
        # couche texte exploitable) ou à couche texte jugée mauvaise sont quand même
        # OCR-isées (cf. LineBuilder.get_all_lines) ; seules les pages de vrai texte
        # numérique sont lues directement -> gros gain sur des PDFs hybrides.
        "force_ocr": False,
        "use_llm": True,
        "llm_service": "marker.services.openai.OpenAIService",
        "openai_base_url": os.getenv("PROXY_URL"),
        "openai_model": "gemma4-26b-moe",
        "openai_api_key": os.getenv("REAL_LLM_API_KEY"),
        "timeout": 99999,
        # --- parallélisme intra-document ---
        "pdftext_workers": 8,
        # --- batch sizes (adaptés au device détecté) ---
        **batch_sizes,
        # Parallélisme des appels LLM (réseau, via le marker_proxy) : indépendant du GPU.
        # À calibrer selon ce que le LLM distant tolère (sinon 429 / timeouts).
        "max_concurrency": 15,
    }

    parser = ConfigParser(config)
    converter = PdfConverter(
        config=parser.generate_config_dict(),
        artifact_dict=artifact_dict,
        processor_list=parser.get_processors(),
        renderer=parser.get_renderer(),
        llm_service=parser.get_llm_service(),
    )

    try:
        rendered = converter(pdf_path)
    except Exception as e:
        # On loggue la stack complète côté serveur : l'endpoint ne renvoie que str(e)
        # dans la réponse HTTP 500 et uvicorn ne trace pas les HTTPException.
        traceback.print_exc()

        # surya/marker emballe l'OOM CUDA dans une exception générique (RuntimeError),
        # donc `except torch.cuda.OutOfMemoryError` ne suffit pas : on inspecte le message
        # et toute la chaîne de causes pour détecter l'OOM de façon robuste.
        if _is_cuda_oom(e):
            mem = (
                f"VRAM: {torch.cuda.memory_allocated() / 1e9:.2f} Go alloc / "
                f"{torch.cuda.max_memory_reserved() / 1e9:.2f} Go pic reserved / "
                f"{torch.cuda.get_device_properties(0).total_memory / 1e9:.2f} Go total"
            )
            # Libère la VRAM pour que la requête suivante reparte sur un allocateur sain.
            torch.cuda.empty_cache()
            raise MarkerConversionError(
                f"OOM GPU pendant l'OCR ({mem}). Baisser recognition_batch_size "
                f"(et les autres *_batch_size à proportion) dans extract_image_to_json.py."
            ) from e

        raise MarkerConversionError(f"Marker conversion failed: {e}") from e

    elapsed = time.time() - start
    print(f"Extraction terminée en {elapsed:.1f}s ({elapsed / 60:.1f} min)")

    # model_dump() peut échouer sur certains champs Set[...] de pydantic v2
    result = json.loads(rendered.model_dump_json())
    result["image_info"] = {
        "image_generated": True,
        "image_filename": os.path.basename(image_path),
        "image_size_bytes": os.path.getsize(image_path),
    }
    return result
