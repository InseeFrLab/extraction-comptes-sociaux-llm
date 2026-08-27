"""Conventions du corpus « tableaux historiques » : chemins S3 et nommage.

Module volontairement sans dépendance : il est importé aussi bien par
`extraction_historiques.py` (venv `scripts`, avec requests) que, en cascade, par
`website/build_data_historiques.py` (venv `website`, qui ne l'a pas).

Le corpus tient en une règle de nommage : une image et son annotation portent la même
clé, préfixée différemment.

    pdf/tableaux historiques/crop_1970_bis.tiff          ─┐
    annotations/tableaux historiques/                     ├─ clé « 1970_bis »
        ground_truth_1970_bis.html                       ─┘
    tableaux_historiques/output_chandra/crop_1970_bis.json
    tableaux_historiques/output_csv/chandra/crop_1970_bis_1.csv

Le corpus est extrait par notre appel direct au VLM (`api_chandra`), décliné en plusieurs
conditions d'expérience — un réglage change, le reste est identique. Chacune écrit dans son
propre préfixe, et toutes partagent la chaîne de conversion et la mesure. `MOTEURS` les
décrit.
"""

import re

BUCKET = "projet-extraction-tableaux"

S3_IMAGES = f"{BUCKET}/pdf/tableaux historiques"
S3_ANNOTATIONS = f"{BUCKET}/annotations/tableaux historiques"
S3_EVAL_OUTPUT = f"{BUCKET}/tableaux_historiques/eval/evaluation.parquet"

# Grand côté, en pixels, de notre dimensionnement. Valeur mesurée : voir la section
# « Effet de la résolution » de la page Résultats du site.
COTE_MAX = 2200

# Les conditions d'extraction comparées, et tout ce qui les distingue. Toutes passent par
# `api_chandra`, notre appel direct au VLM ; le reste du pipeline ne connaît que les clés de
# ce dict.
#
# `chandra` est la référence, celle dont le site publie les chiffres : image seule, sans
# prompt, réduite au grand côté demandé.
#
# Les clés facultatives `max_tokens`, `prompt`, `prompt_type` et `max_retries` sont
# transmises telles quelles à l'API (`extraction_historiques._champs`) : une condition
# d'expérience — le même appel à un réglage près — s'ajoute donc ici, avec ses préfixes
# `json` et `csv` propres, plus l'entrée correspondante dans `json_to_csv.SOURCES`.
# `evaluation_historiques` dérive les siennes de ce dict et n'a rien à déclarer.
#
# Un prompt se désigne par son nom (`prompt_type`), jamais par son texte : c'est l'API qui
# le résout dans `chandra.prompts`, seule source de vérité, et ce module reste sans
# dépendance. `prompt` accepte un texte libre, pour explorer hors de ces deux-là.
#
# Le client d'appel du paquet `chandra-ocr` a été comparé à cet appel direct, puis retiré de
# la chaîne comme du site. Ses sorties dorment encore sur S3 sous les préfixes
# `*_chandra_client*` ; plus rien ici ne les produit ni ne les lit.
MOTEURS: dict[str, dict] = {
    # `max_tokens` et `max_retries` à 0 figent le comportement qui a produit les chiffres
    # publiés : ni plafond de sortie, ni relance sur répétition. L'API pose désormais les
    # deux par défaut (`CHANDRA_MAX_TOKENS`, `CHANDRA_REPEAT_RETRIES`) ; les expliciter ici
    # garde cette référence reproductible, et `chandra_borne` mesure ce que la borne change.
    "chandra": {
        "cote_max": COTE_MAX,
        "max_tokens": 0,
        "max_retries": 0,
        "json": f"{BUCKET}/tableaux_historiques/output_chandra",
        "csv": f"{BUCKET}/tableaux_historiques/output_csv/chandra",
    },
    # ── Conditions d'expérience ───────────────────────────────────────────────
    #
    # `chandra_prompt_ocr` et `chandra_prompt_layout` mesurent le prompt seul : même client,
    # même échantillonnage, même image, seule la présence du bloc texte change. `ocr` étant
    # `ocr_layout` privé des bbox et des étiquettes de bloc, leur écart chiffre le coût de la
    # tâche de mise en page, séparément des consignes de formatage que les deux partagent.
    "chandra_prompt_ocr": {
        "cote_max": COTE_MAX,
        "prompt_type": "ocr",
        "json": f"{BUCKET}/tableaux_historiques/output_chandra_prompt_ocr",
        "csv": f"{BUCKET}/tableaux_historiques/output_csv/chandra_prompt_ocr",
    },
    "chandra_prompt_layout": {
        "cote_max": COTE_MAX,
        "prompt_type": "ocr_layout",
        "json": f"{BUCKET}/tableaux_historiques/output_chandra_prompt_layout",
        "csv": f"{BUCKET}/tableaux_historiques/output_csv/chandra_prompt_layout",
    },
    # `chandra` borné : le plafond de sortie que l'appel direct n'avait pas. Mesuré sur les
    # 40 images, le plus gros tableau légitime demande 13 644 jetons et une génération
    # dégénérée en consomme 62 000, pour un demi-mégaoctet de HTML stocké ; 16 384 laisse
    # 20 % de marge et coupe les boucles au quart. `max_retries` reste à 0 : les deux boucles
    # du corpus survivent à deux relances, qui ne font que tripler le coût — la répétition
    # est détectée et marquée dans la sortie, pas combattue. À comparer à `chandra`, dont il
    # ne diffère que par le plafond ; s'il ne perd rien, il prend sa place.
    "chandra_borne": {
        "cote_max": COTE_MAX,
        "max_tokens": 16384,
        "max_retries": 0,
        "json": f"{BUCKET}/tableaux_historiques/output_chandra_borne",
        "csv": f"{BUCKET}/tableaux_historiques/output_csv/chandra_borne",
    },
}

IMAGE_EXTENSIONS = (".tif", ".tiff", ".png", ".jpg", ".jpeg")

# Radicaux respectifs des deux corpus de fichiers, retirés pour obtenir la clé commune.
IMAGE_PREFIX = "crop_"
ANNOTATION_PREFIX = "ground_truth_"


def stem(path: str) -> str:
    """Nom de fichier sans son extension.

    Args:
        path: chemin S3 ou local.

    Returns:
        Le nom de base privé de son extension.
    """
    return re.sub(r"\.[^.]+$", "", path.rsplit("/", 1)[-1])


def key_from_image(path: str) -> str:
    """Clé de tableau portée par un fichier image.

    Args:
        path: chemin de l'image (`.../crop_1970_bis.tiff`).

    Returns:
        La clé, ici `1970_bis`.
    """
    return stem(path).removeprefix(IMAGE_PREFIX)


def key_from_annotation(path: str) -> str:
    """Clé de tableau portée par un fichier d'annotation.

    Args:
        path: chemin de l'annotation (`.../ground_truth_1970_bis.html`).

    Returns:
        La clé, ici `1970_bis`.
    """
    return stem(path).removeprefix(ANNOTATION_PREFIX)
