"""
API d'extraction de tableaux via le VLM Chandra (vllm, compatible OpenAI).

`/extract` accepte **soit un PDF, soit une image**. Le PDF est rendu page par page à
`CHANDRA_DPI` (200, fixe), l'image est envoyée telle quelle ; dans les deux cas `cote_max`
peut borner le grand côté, et le HTML natif (<table>) du modèle est **renvoyé tel quel**.

    # PDF
    {"metadata": {"model": ..., "source": "pdf", "dpi": 200},
     "pages": [{"page": 1, "html": "<table>…</table>", "dpi": 200}]}

    # image
    {"metadata": {"model": ..., "source": "image", "cote_max": 2200},
     "pages": [{"page": 1, "html": "<table>…</table>", "pixels": [2200, 1287],
                "pixels_source": [13565, 7935]}]}

Lancement :
    uv run uvicorn main_chandra:app --host 0.0.0.0 --port 8003 --app-dir src

**Les choix de conception, les réglages et les mesures qui les fondent sont dans
[README.md](../README.md)** : pourquoi le HTML n'est pas structuré ici, pourquoi une image
n'a pas de dpi, pourquoi la résolution est fixe, d'où viennent le plafond de jetons et la
politique de relance, et la liste complète des variables d'environnement (`CHANDRA_*`).
"""

import asyncio
import base64
import io
import os
import shutil
import tempfile

import pymupdf as fitz
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI
from PIL import Image

load_dotenv()

# Valeurs mesurées, chacune justifiée dans le README : résolution fixe, plafond de
# génération, et détection de répétition qui marque sans relancer.
os.environ.setdefault("CHANDRA_BASE_URL", "https://llm.lab.sspcloud.fr/api")
os.environ.setdefault("CHANDRA_MODEL", "chandra-ocr-2")
os.environ.setdefault("CHANDRA_API_KEY", "EMPTY")
os.environ.setdefault("CHANDRA_DPI", "200")
os.environ.setdefault("CHANDRA_COTE_MAX", "0")
os.environ.setdefault("CHANDRA_RETRIES", "5")
os.environ.setdefault("CHANDRA_RETRY_DELAY", "2")
os.environ.setdefault("CHANDRA_MAX_TOKENS", "16384")
os.environ.setdefault("CHANDRA_REPEAT_RETRIES", "0")

app = FastAPI(
    title="API Chandra PDF Extraction",
    version="1.0.0",
    description="Extraction de tableaux PDF via le VLM Chandra",
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
)


# ── Conversion PDF → images base64 ───────────────────────────────────────────


def _pdf_to_b64_images(pdf_path: str, dpi: int, cote_max: int = 0) -> list[dict]:
    """Rend chaque page du PDF en PNG encodé en base64, à résolution fixe.

    Résolution fixe et non adaptative, et `cote_max` appliqué **après** le rendu : cf. README.

    Args:
        pdf_path: chemin du PDF.
        dpi: résolution de rendu, en points par pouce.
        cote_max: grand côté maximal après rendu, en pixels ; 0 pour n'en imposer aucun.

    Returns:
        Un dict par page — `b64`, `dpi`, `pixels` — dans l'ordre du document. La taille
        envoyée est remontée jusqu'à la sortie, qui doit dire ce que le modèle a vu.
    """
    doc = fitz.open(pdf_path)
    rendus = []
    for numero, page in enumerate(doc, start=1):
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72))
        rendu = Image.open(io.BytesIO(pix.tobytes("png")))
        reduit = _reduire(rendu, cote_max)
        buffer = io.BytesIO()
        reduit.save(buffer, "PNG", optimize=True)
        suffixe = (
            "" if reduit.size == rendu.size else f", ramené de {rendu.size[0]}×{rendu.size[1]}"
        )
        print(f"  page {numero} : {dpi} dpi, {reduit.size[0]}×{reduit.size[1]} px{suffixe}")
        rendus.append(
            {
                "b64": base64.b64encode(buffer.getvalue()).decode("utf-8"),
                "dpi": dpi,
                "pixels": list(reduit.size),
            }
        )
    doc.close()
    return rendus


# ── Image soumise directement ────────────────────────────────────────────────

# Les scans d'archives dépassent la garde anti-bombe de Pillow, et sont déposés par nos
# soins : cf. README.
Image.MAX_IMAGE_PIXELS = None


def _reduire(image: Image.Image, cote_max: int) -> Image.Image:
    """Ramène le grand côté d'une image à `cote_max`, sans jamais l'agrandir.

    Args:
        image: image source.
        cote_max: grand côté maximal en pixels ; 0 ou négatif pour n'en imposer aucun.

    Returns:
        L'image réduite, ou l'originale si elle tient déjà dans le budget.
    """
    largeur, hauteur = image.size
    if cote_max <= 0 or max(largeur, hauteur) <= cote_max:
        return image
    facteur = cote_max / max(largeur, hauteur)
    # LANCZOS, y compris sur du bilevel : cf. README.
    return image.resize((round(largeur * facteur), round(hauteur * facteur)), Image.LANCZOS)


def _image_to_b64(
    image_bytes: bytes, cote_max: int
) -> tuple[str, tuple[int, int], tuple[int, int]]:
    """Prépare une image reçue telle quelle pour le VLM.

    Aucun aller-retour par le PDF, et conversion de mode minimale : cf. README.

    Args:
        image_bytes: contenu du fichier reçu, format quelconque lisible par Pillow.
        cote_max: grand côté maximal en pixels ; 0 ou négatif pour n'en imposer aucun. Une
            image déjà plus petite n'est jamais agrandie.

    Returns:
        (image PNG encodée en base64, taille envoyée, taille du fichier d'origine).
    """
    image = Image.open(io.BytesIO(image_bytes))
    taille_source = image.size

    if image.mode in ("1", "I;16", "I"):
        image = image.convert("L")
    elif image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    image = _reduire(image, cote_max)

    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("utf-8"), image.size, taille_source


# ── Diagnostic de génération ─────────────────────────────────────────────────


def _prompt_texte(prompt_type: str) -> str:
    """Résout un prompt de chandra par son nom.

    Désigner un prompt plutôt que le recopier : cf. README.

    Args:
        prompt_type: clé de `PROMPT_MAPPING` — `ocr` ou `ocr_layout`.

    Returns:
        Le texte du prompt.

    Raises:
        HTTPException: si la clé est inconnue du paquet installé.
    """
    from chandra.prompts import PROMPT_MAPPING

    if prompt_type not in PROMPT_MAPPING:
        raise HTTPException(
            status_code=400,
            detail=f"`prompt_type` attend l'un de {sorted(PROMPT_MAPPING)}.",
        )
    return PROMPT_MAPPING[prompt_type]


def _repetition(raw: str) -> bool:
    """Dit si une génération s'est terminée en boucle.

    Critère emprunté au paquet `chandra-ocr`, et double test : cf. README.

    Args:
        raw: sortie brute du modèle.

    Returns:
        True si la fin de la génération se répète.
    """
    from chandra.model.util import detect_repeat_token

    return bool(
        detect_repeat_token(raw) or (len(raw) > 50 and detect_repeat_token(raw, cut_from_end=50))
    )


# ── Appel VLM ────────────────────────────────────────────────────────────────


async def _appel_vlm(
    client: AsyncOpenAI,
    model: str,
    contenu: list[dict],
    max_tokens: int,
    temperature: float,
    top_p: float | None,
) -> tuple[str, int | None]:
    """Un appel au VLM, retenté sur erreur de transport.

    Args:
        client: client OpenAI asynchrone pointant sur le serveur vllm.
        model: nom du modèle servi par vllm.
        contenu: parts du message utilisateur — l'image, puis la consigne s'il y en a une.
        max_tokens: plafond de génération ; 0 pour n'en imposer aucun.
        temperature: température d'échantillonnage.
        top_p: noyau d'échantillonnage ; None pour le défaut du serveur.

    Returns:
        (texte produit par le modèle, jetons de sortie). Le compte de jetons est None si le
        serveur ne le rapporte pas.

    Raises:
        Exception: celle du dernier essai, si tous échouent.
    """
    retries = int(os.getenv("CHANDRA_RETRIES"))
    delay = float(os.getenv("CHANDRA_RETRY_DELAY"))
    last_exc: Exception | None = None

    reglages: dict = {"temperature": temperature}
    if max_tokens:
        reglages["max_tokens"] = max_tokens
    if top_p is not None:
        reglages["top_p"] = top_p

    for attempt in range(retries):
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": contenu}],
                # Sans effet mesuré sur chandra-ocr-2, conservé pour rester explicite : README.
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                **reglages,
            )
            jetons = response.usage.completion_tokens if response.usage else None
            return response.choices[0].message.content or "", jetons
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                await asyncio.sleep(delay * (attempt + 1))

    raise last_exc


async def _extract_html_from_image(
    client: AsyncOpenAI,
    b64_image: str,
    model: str,
    prompt: str | None = None,
    max_tokens: int = 0,
    max_retries: int = 0,
) -> dict:
    """Soumet une image au VLM, en relançant si la génération part en boucle.

    Premier appel déterministe, puis politique de relance du paquet `chandra-ocr` —
    température +0,2 par tentative, plafonnée à 0,8, `top_p` 0,95 : cf. README.

    Args:
        client: client OpenAI asynchrone pointant sur le serveur vllm.
        b64_image: page rendue en PNG, encodée en base64.
        model: nom du modèle servi par vllm.
        prompt: consigne à joindre à l'image ; None pour n'envoyer que l'image, le mode par
            défaut. Elle est placée **après** l'image dans le même message utilisateur,
            l'ordre pour lequel le modèle a été réglé.
        max_tokens: plafond de génération ; 0 pour n'en imposer aucun.
        max_retries: relances autorisées sur détection de répétition ; 0 pour se contenter de
            marquer la page.

    Returns:
        `html` (le texte du modèle, sans retouche), `jetons`, `repetition` (l'état après
        relances), `tronque` et `relances`.
    """
    contenu: list[dict] = [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_image}"}}
    ]
    if prompt:
        contenu.append({"type": "text", "text": prompt})

    temperature: float = 0
    top_p: float | None = None
    relances = 0
    while True:
        html, jetons = await _appel_vlm(client, model, contenu, max_tokens, temperature, top_p)
        boucle = _repetition(html)
        if not boucle or relances >= max_retries:
            break
        relances += 1
        temperature, top_p = min(0.2 * relances, 0.8), 0.95

    return {
        "html": html,
        "jetons": jetons,
        "repetition": boucle,
        "tronque": bool(max_tokens and jetons and jetons >= max_tokens),
        "relances": relances,
    }


# ── Endpoint /extract ─────────────────────────────────────────────────────────


def _client() -> AsyncOpenAI:
    """Client vllm configuré depuis l'environnement."""
    return AsyncOpenAI(
        base_url=os.getenv("CHANDRA_BASE_URL"),
        api_key=os.getenv("CHANDRA_API_KEY"),
        timeout=None,
    )


async def _pages_from_rendus(
    client: AsyncOpenAI,
    model: str,
    rendus: list[dict],
    prompt: str | None = None,
    max_tokens: int = 0,
    max_retries: int = 0,
) -> list[dict]:
    """Soumet chaque rendu au VLM et compose les pages de la réponse.

    Args:
        client: client vllm.
        model: nom du modèle.
        rendus: un dict par page, portant `b64` et les champs à recopier dans la sortie.
        prompt: consigne à joindre à l'image ; None pour l'image seule.
        max_tokens: plafond de génération ; 0 pour n'en imposer aucun.
        max_retries: relances sur détection de répétition ; 0 pour n'en faire aucune.

    Returns:
        Une entrée `pages[]` par rendu, HTML du modèle inclus, avec ses marques de
        diagnostic : `jetons`, `repetition`, `tronque` et `relances`.

    Raises:
        HTTPException: si le VLM échoue sur une page, après ses propres tentatives.
    """
    pages = []
    for numero, rendu in enumerate(rendus, start=1):
        try:
            resultat = await _extract_html_from_image(
                client, rendu["b64"], model, prompt, max_tokens, max_retries
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Erreur VLM page {numero} : {e}")
        marques = "".join(
            [
                f" — {resultat['relances']} RELANCE(S)" if resultat["relances"] else "",
                " — RÉPÉTITION" if resultat["repetition"] else "",
                " — TRONQUÉE" if resultat["tronque"] else "",
            ]
        )
        plafond = f"/{max_tokens}" if max_tokens else ""
        print(f"  page {numero} : {resultat['jetons']}{plafond} jetons{marques}")
        pages.append({"page": numero, **resultat, **{k: v for k, v in rendu.items() if k != "b64"}})
    return pages


@app.post("/extract")
async def extract(
    pdf: UploadFile = File(None),
    image: UploadFile = File(None),
    cote_max: int = Form(None),
    max_tokens: int = Form(None),
    prompt: str = Form(None),
    prompt_type: str = Form(None),
    max_retries: int = Form(None),
):
    """Extrait les tableaux d'un PDF ou d'une image.

    Ce que valent ces réglages et pourquoi : cf. README.

    Args:
        pdf: document PDF, rendu page par page à `CHANDRA_DPI`.
        image: image déjà rognée sur le tableau, envoyée telle quelle. Exclusif de `pdf`.
        cote_max: grand côté maximal en pixels, appliqué **après** le rendu. Défaut :
            `CHANDRA_COTE_MAX` (natif) pour une image, aucun plafond pour un PDF.
        max_tokens: plafond de génération ; défaut `CHANDRA_MAX_TOKENS`, `0` pour aucun.
        prompt: consigne libre ; sans elle, seule l'image est envoyée.
        prompt_type: `ocr` ou `ocr_layout`, prompt du paquet `chandra-ocr` désigné par son
            nom ; ignoré si `prompt` est fourni.
        max_retries: relances sur détection de répétition ; défaut `CHANDRA_REPEAT_RETRIES`.
            Les erreurs de transport sont retentées à part, sous `CHANDRA_RETRIES`.

    Returns:
        Le HTML du modèle, une entrée par page (une seule pour une image), assortie de
        `jetons`, `repetition`, `tronque` et `relances`.
    """
    if (pdf is None) == (image is None):
        raise HTTPException(status_code=400, detail="Fournir exactement un `pdf` ou une `image`.")

    model = os.getenv("CHANDRA_MODEL")

    # `prompt` l'emporte sur `prompt_type`, résolu avant tout appel : une clé inconnue doit
    # ressortir en 400, pas en `KeyError` au milieu d'une extraction.
    consigne = prompt or (_prompt_texte(prompt_type) if prompt_type else None)

    # Les champs de formulaire l'emportent sur l'environnement, y compris à 0, qui est une
    # valeur significative — pas de plafond, pas de relance.
    plafond = int(os.getenv("CHANDRA_MAX_TOKENS")) if max_tokens is None else max_tokens
    relances_max = int(os.getenv("CHANDRA_REPEAT_RETRIES")) if max_retries is None else max_retries

    client = _client()

    if image is not None:
        if not (image.content_type or "").startswith("image/"):
            raise HTTPException(status_code=400, detail="Le champ `image` attend un fichier image.")
        budget = int(os.getenv("CHANDRA_COTE_MAX")) if cote_max is None else cote_max
        try:
            b64, taille, taille_source = _image_to_b64(await image.read(), budget)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Erreur de lecture de l'image : {e}")
        print(
            f"  image {taille_source[0]}×{taille_source[1]} px envoyée en {taille[0]}×{taille[1]}"
        )
        pages = await _pages_from_rendus(
            client,
            model,
            [{"b64": b64, "pixels": list(taille), "pixels_source": list(taille_source)}],
            consigne,
            plafond,
            relances_max,
        )
        await client.close()
        return JSONResponse(
            content={
                "metadata": {
                    "model": model,
                    "source": "image",
                    "cote_max": budget or "natif",
                    "prompt": "adapté" if prompt else (prompt_type or "aucun"),
                    "max_tokens": plafond or "sans plafond",
                    "max_retries": relances_max,
                },
                "pages": pages,
            }
        )

    if pdf.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Le champ `pdf` attend un fichier PDF.")
    dpi = int(os.getenv("CHANDRA_DPI"))

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, pdf.filename)
        with open(pdf_path, "wb") as f:
            shutil.copyfileobj(pdf.file, f)

        try:
            rendus = _pdf_to_b64_images(pdf_path, dpi, cote_max or 0)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Erreur conversion PDF→image : {e}")

        pages = await _pages_from_rendus(client, model, rendus, consigne, plafond, relances_max)

    await client.close()
    return JSONResponse(
        content={
            "metadata": {
                "model": model,
                "source": "pdf",
                "dpi": dpi,
                "cote_max": cote_max or "sans plafond",
                "prompt": "adapté" if prompt else (prompt_type or "aucun"),
                "max_tokens": plafond or "sans plafond",
                "max_retries": relances_max,
            },
            "pages": pages,
        }
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8003)
