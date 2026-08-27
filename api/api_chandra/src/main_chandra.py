"""
API d'extraction de tableaux via le VLM Chandra (vllm, compatible OpenAI).

`/extract` accepte **soit un PDF, soit une image**. Le PDF est rendu page par page à
`CHANDRA_DPI` (200 dpi, fixe) ; l'image est envoyée telle quelle. Dans les deux cas
`cote_max` peut borner le grand côté après coup. Dans les deux cas le VLM reçoit une image et rend du HTML natif
(<table>) : **c'est ce HTML qui est renvoyé tel quel**.

    # PDF
    {"metadata": {"model": ..., "source": "pdf", "dpi": 200},
     "pages": [{"page": 1, "html": "<table>…</table>", "dpi": 200}]}

    # image
    {"metadata": {"model": ..., "source": "image", "cote_max": 2200},
     "pages": [{"page": 1, "html": "<table>…</table>", "pixels": [2200, 1287],
                "pixels_source": [13565, 7935]}]}

**Une image n'a pas de dpi**, et c'est la raison d'être de cette entrée. Un scan rogné sur
un tableau ne porte aucune taille physique : le convertir en PDF pour le faire rendre à
tant de points par pouce revient à inventer une page, puis à rééchantillonner deux fois une
image qui n'en avait pas besoin. La seule grandeur qui décide de ce que le modèle voit est
le nombre de pixels, et c'est donc elle que l'appelant fixe (`cote_max`).

L'API ne structure plus la réponse. Elle le faisait auparavant, en aplatissant le HTML en
listes de listes de chaînes (`pages[].tables`), ce qui détruisait au passage tout ce que le
modèle savait de la mise en page : `colspan`, `rowspan` — donc les cellules fusionnées —
et les `<br>`, dont la disparition soudait les mots de deux lignes d'un même libellé
(« Prêts etavancesconsentispar laSociété », 48 cellules du corpus `reprise/`).

La structuration appartient à l'étape suivante, `scripts/json_to_csv.py`, qui dispose déjà
d'un parseur HTML traitant les fusions et sert le même office pour marker et
opendataloader. Conserver le HTML brut a un second avantage : aucune information n'est
perdue à l'écriture, et un changement d'avis sur la mise en forme ne demande pas de
relancer le GPU. Les JSON du format historique (`pages[].tables`) restent lus par
`json_to_csv.py`.

Lancement :
    uv run uvicorn main_chandra:app --host 0.0.0.0 --port 8003 --app-dir src

Variables d'environnement :
    CHANDRA_BASE_URL    URL du serveur vllm  (défaut: https://llm.lab.sspcloud.fr/api)
    CHANDRA_MODEL       Nom du modèle        (défaut: chandra-ocr-2)
    CHANDRA_API_KEY     Clé API              (défaut: EMPTY, convention vllm ; sur llm.lab,
                        passer REAL_LLM_API_KEY — l'endpoint est authentifié)
    CHANDRA_DPI         Résolution PDF→image, en dpi (défaut: 200). **Ne s'applique qu'aux
                        PDF** : une image est envoyée à sa taille en pixels, cf. CHANDRA_COTE_MAX.
    CHANDRA_COTE_MAX    Grand côté maximal d'une image soumise directement, en pixels
                        (défaut: 0 = résolution native, aucune réduction). Le champ de
                        formulaire `cote_max` le surcharge requête par requête, et
                        s'applique aussi aux PDF, après leur rendu.
    CHANDRA_RETRIES     Tentatives par page sur erreur de transport (défaut: 5),
                        indépendamment des relances sur répétition (cf. `max_retries`).
    CHANDRA_RETRY_DELAY Délai initial (s)    (défaut: 2, multiplié par le numéro de tentative)
    CHANDRA_MAX_TOKENS  Plafond de génération, en jetons (défaut: 16 384 ; 0 pour aucun).
                        Mesuré : 13 644 jetons pour le plus gros tableau légitime du corpus
                        historique, 62 000 pour une génération dégénérée.
    CHANDRA_REPEAT_RETRIES
                        Relances sur détection de répétition (défaut: 0 — la page est
                        marquée, pas relancée ; mesure à l'appui, cf. le commentaire du
                        réglage).

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

os.environ.setdefault("CHANDRA_BASE_URL", "https://llm.lab.sspcloud.fr/api")
os.environ.setdefault("CHANDRA_MODEL", "chandra-ocr-2")
os.environ.setdefault("CHANDRA_API_KEY", "EMPTY")
# Résolution de rendu des PDF, fixe. Le mode adaptatif qui a existé ici est documenté dans
# `_pdf_to_b64_images` : mesuré perdant, il a été retiré.
os.environ.setdefault("CHANDRA_DPI", "200")
# 0 : on envoie l'image telle qu'elle arrive. Réduire est une décision qui appartient à
# l'appelant, seul à savoir ce que son corpus contient.
os.environ.setdefault("CHANDRA_COTE_MAX", "0")
os.environ.setdefault("CHANDRA_RETRIES", "5")
os.environ.setdefault("CHANDRA_RETRY_DELAY", "2")
# Plafond de génération, que l'API n'a longtemps pas eu. Valeur mesurée sur le corpus
# historique : le plus gros tableau légitime demande 13 644 jetons, une génération dégénérée
# en consomme 62 000 — jusqu'au bout du contexte, pour un demi-mégaoctet de HTML stocké.
# 16 384 laisse 20 % de marge au-dessus du besoin réel et coupe les boucles au quart de ce
# qu'elles coûtaient. 0 pour n'imposer aucun plafond.
os.environ.setdefault("CHANDRA_MAX_TOKENS", "16384")
# Relances sur détection de répétition. **0 par défaut : on détecte et on marque, on ne
# relance pas.** Mesuré sur les deux seules boucles du corpus historique (crop_1902,
# 16 384 jetons ; crop_1989, 4 882) : deux relances à 0,2 puis 0,4 ne cassent ni l'une ni
# l'autre, et coûtent trois appels au lieu d'un — 329 s contre 110 s sur crop_1902. La
# boucle est un attracteur trop fort pour un desserrage timide ; il faut six relances
# jusqu'à 0,8 pour en sortir, et le seul tableau ainsi sauvé lors des mesures avait *perdu*
# en précision (−0,013 de numeric_recovery, seul tableau déplacé sur 38). Relever ce réglage
# n'a donc de sens qu'à 5 ou 6, en sachant que ça achète une sortie propre et non meilleure.
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

    Un mode `auto` a existé, qui déduisait le dpi de la résolution réelle du scan page par
    page. Mesuré sur 17 documents (31 paires), il améliorait 2 paires, en dégradait 4 et
    laissait 22 inchangées, pour un solde de −149 cellules : il n'y a pas de relation
    monotone entre résolution et qualité, et choisir le bon dpi demanderait un critère dont
    on ne dispose pas sans annotation. La résolution est donc fixe, et 200 dpi est le
    réglage retenu.

    `cote_max` s'applique **après** le rendu : à 200 dpi une A4 fait 1 654 × 2 339 px, donc
    un plafond au-delà de 2 339 ne mord pas. Il ne sert pas à choisir la finesse — c'est le
    dpi qui la fixe — mais à borner un grand format, et à obtenir des images de mêmes
    dimensions que celles d'un autre moteur, ce qui est la condition pour comparer deux
    prompts sans comparer en même temps deux résolutions.

    Args:
        pdf_path: chemin du PDF.
        dpi: résolution de rendu, en points par pouce.
        cote_max: grand côté maximal après rendu, en pixels ; 0 pour n'en imposer aucun.

    Returns:
        Un dict par page — `b64`, `dpi`, `pixels` — dans l'ordre du document. La taille
        envoyée est remontée jusqu'à la sortie : une extraction n'est comparable à une autre
        que si l'on sait ce que le modèle a vu.
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

# Les scans d'archives dépassent la garde anti-bombe de Pillow (89 Mpx) : le corpus des
# tableaux historiques monte à 889 Mpx pour un seul fichier. Ces images sont déposées par
# nos soins, la garde n'a rien à protéger ici.
Image.MAX_IMAGE_PIXELS = None


def _reduire(image: Image.Image, cote_max: int) -> Image.Image:
    """Ramène le grand côté d'une image à `cote_max`, sans jamais l'agrandir.

    Args:
        image: image source.
        cote_max: grand côté maximal en pixels ; 0 ou négatif pour n'en imposer aucun.

    Returns:
        L'image réduite, ou l'originale si elle tient déjà dans le budget. Ajouter des
        pixels interpolés ne montre rien de plus au modèle, donc on n'agrandit pas.
    """
    largeur, hauteur = image.size
    if cote_max <= 0 or max(largeur, hauteur) <= cote_max:
        return image
    facteur = cote_max / max(largeur, hauteur)
    # LANCZOS sur du bilevel produirait des niveaux de gris : c'est voulu, un texte
    # réduit au plus proche voisin perd ses jambages et devient illisible.
    return image.resize((round(largeur * facteur), round(hauteur * facteur)), Image.LANCZOS)


def _image_to_b64(
    image_bytes: bytes, cote_max: int
) -> tuple[str, tuple[int, int], tuple[int, int]]:
    """Prépare une image reçue telle quelle pour le VLM.

    Aucun aller-retour par le PDF : l'image n'a pas de taille physique, donc pas de dpi, et
    lui en inventer une la ferait rééchantillonner deux fois — une fois au rendu, une fois
    par le modèle. On se contente de la réduire si l'appelant l'a demandé, et de l'encoder.

    Args:
        image_bytes: contenu du fichier reçu, format quelconque lisible par Pillow.
        cote_max: grand côté maximal en pixels ; 0 ou négatif pour n'en imposer aucun.
            Une image déjà plus petite n'est jamais agrandie : ajouter des pixels
            interpolés ne montre rien de plus au modèle.

    Returns:
        (image PNG encodée en base64, taille envoyée, taille du fichier d'origine).
    """
    image = Image.open(io.BytesIO(image_bytes))
    taille_source = image.size

    # Un scan bilevel ou en niveaux de gris reste en `L` : le passer en RGB triplerait le
    # PNG sans rien montrer de plus. Le reste — CMJN, palette, 16 bits, canal alpha — n'est
    # pas écrivable en PNG tel quel, ou ne porte aucune information sur un scan.
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

    Nommer le prompt plutôt que le recopier laisse le paquet `chandra-ocr` seule source de
    vérité : ses consignes évoluent avec le modèle, et une copie dans nos scripts dériverait
    en silence. C'est aussi ce qui permet à `corpus_historiques.py` de désigner une
    condition d'expérience tout en restant sans dépendance.

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

    Le critère est emprunté au paquet `chandra-ocr` plutôt que réécrit : c'est celui pour
    lequel le modèle a été réglé, et le reprendre évite d'inventer un seuil qu'il faudrait
    justifier. Il ne déclenche rien par défaut — `CHANDRA_REPEAT_RETRIES` vaut 0, la page
    est marquée et non relancée — mais la marque suffit à écarter des mesures une sortie
    dégénérée.

    Args:
        raw: sortie brute du modèle.

    Returns:
        True si la fin de la génération se répète, le test étant refait en ignorant les 50
        derniers caractères — le double test rattrape les boucles suivies d'une fin de
        sortie propre.
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
                # Mesuré sans effet sur chandra-ocr-2 — sortie identique au caractère près,
                # aucun contenu de raisonnement rendu. Conservé pour que le réglage soit
                # explicite si un jour le modèle servi en tient compte.
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

    La politique de relance est celle du paquet `chandra-ocr` : température desserrée de 0,2
    par tentative, plafonnée à 0,8, et `top_p` à 0,95. Le premier appel reste déterministe
    (température 0) ; on ne desserre que pour sortir d'une boucle, jamais par défaut.

    Args:
        client: client OpenAI asynchrone pointant sur le serveur vllm.
        b64_image: page rendue en PNG, encodée en base64.
        model: nom du modèle servi par vllm.
        prompt: consigne à joindre à l'image ; None pour n'envoyer que l'image, qui est le
            mode par défaut. La consigne est placée **après** l'image dans le même message
            utilisateur, dans l'ordre pour lequel le modèle a été réglé.
        max_tokens: plafond de génération ; 0 pour n'en imposer aucun.
        max_retries: relances autorisées sur détection de répétition ; 0 pour n'en faire
            aucune et se contenter de marquer la page.

    Returns:
        `html` (le texte du modèle, sans retouche), `jetons`, `repetition` (l'état après
        toutes les relances), `tronque` et `relances`. Aucune structuration ici, pour ne
        rien perdre à l'écriture.
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
        Une entrée `pages[]` par rendu, HTML du modèle inclus, avec `jetons`, `repetition`,
        `tronque` et `relances`. Une page encore marquée `repetition` après ses relances est
        une sortie dégénérée conservée telle quelle, à écarter des comparaisons.

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

    Args:
        pdf: document PDF, rendu page par page à `CHANDRA_DPI`.
        image: image déjà rognée sur le tableau, envoyée telle quelle. Exclusif de `pdf`.
        cote_max: grand côté maximal en pixels, appliqué **après** le rendu. Pour une
            image, par défaut `CHANDRA_COTE_MAX`, soit la résolution native. Pour un PDF,
            il borne le rendu fait à `CHANDRA_DPI` — sans plafond par défaut.
        max_tokens: plafond de génération, `CHANDRA_MAX_TOKENS` (16 384) par défaut. `0`
            le retire — le comportement qu'a eu l'API jusqu'à sa mesure, et qui laissait une
            génération dégénérée courir jusqu'au bout du contexte.
        prompt: consigne libre ; sans elle, seule l'image est envoyée. Quatre prompts
            maison ont été mesurés sur les deux corpus et tous font moins bien que
            `ocr_layout`, notamment parce qu'une injonction de complétude (« never stop
            early ») fait boucler la génération jusqu'au plafond de jetons.
        prompt_type: `ocr` ou `ocr_layout`, pour employer un prompt **du paquet
            `chandra-ocr`** sans le recopier — ignoré si `prompt` est fourni. Sans l'un ni
            l'autre, aucune consigne n'est envoyée. `ocr` est la même consigne que
            `ocr_layout` privée des bbox et des 19 étiquettes de bloc : les opposer sépare
            le coût de la tâche de mise en page de celui des consignes de formatage.
        max_retries: relances sur détection de répétition — température +0,2 par tentative,
            plafonnée à 0,8, `top_p` 0,95. Par défaut `CHANDRA_REPEAT_RETRIES` (0), où la
            page est donc marquée sans être relancée : deux relances ne cassent aucune des
            deux boucles du corpus historique et triplent le coût. Les erreurs de transport
            restent retentées dans tous les cas, `CHANDRA_RETRIES` les gouverne.

    Returns:
        Le HTML du modèle, une entrée par page (une seule pour une image), assortie de
        `jetons`, `repetition`, `tronque` et `relances`.
    """
    if (pdf is None) == (image is None):
        raise HTTPException(status_code=400, detail="Fournir exactement un `pdf` ou une `image`.")

    model = os.getenv("CHANDRA_MODEL")

    # `prompt` l'emporte sur `prompt_type`. Le nom est résolu ici, avant tout appel : une
    # clé inconnue doit ressortir en 400, pas en `KeyError` au milieu d'une extraction.
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
        # La taille réellement envoyée est conservée avec la sortie : deux extractions de la
        # même image ne sont comparables que si l'on sait ce que le modèle a vu.
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
    # Le modèle et la résolution sont conservés avec la sortie : deux extractions du même
    # PDF ne sont comparables que si l'on sait ce qui les a produites.
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
