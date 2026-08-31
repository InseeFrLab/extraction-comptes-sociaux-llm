"""Conventions du corpus « tableaux historiques » : nommage des fichiers.

Les chemins, les moteurs et leurs réglages sont déclarés dans
[`config/historiques.yaml`](../config/historiques.yaml) ; ce module ne fait que les exposer
sous les noms qu'emploie le pipeline, et porter les règles de nommage, qui sont du code.

Module volontairement sans dépendance autre que `config` (et donc PyYAML) : il est importé
aussi bien par `extraction_historiques.py` (venv `scripts`, avec requests) que, en cascade,
par `website/scripts/build_data_historiques.py` (venv `website`, qui ne l'a pas).

Le corpus tient en une règle de nommage : une image et son annotation portent la même clé,
préfixée différemment.

    pdf/tableaux historiques/crop_1970_bis.tiff          ─┐
    annotations/tableaux historiques/                     ├─ clé « 1970_bis »
        ground_truth_1970_bis.html                       ─┘
    tableaux_historiques/output_chandra/crop_1970_bis.json
    tableaux_historiques/output_csv/chandra/crop_1970_bis_1.csv

Le corpus est extrait par notre appel direct au VLM (`api_chandra`), décliné en plusieurs
conditions d'expérience déclarées dans la section `moteurs` du fichier de configuration ;
le reste du pipeline n'en connaît que les clés. Cf. [README.md](README.md).
"""

import re

import config

CONFIG = config.charger("historiques")

BUCKET = CONFIG.bucket

S3_IMAGES = CONFIG.sources["images"]
S3_ANNOTATIONS = CONFIG.sources["annotations"]
S3_EVAL_OUTPUT = CONFIG.evaluation["sortie"]

# Les conditions d'extraction comparées, et tout ce qui les distingue.
MOTEURS = CONFIG.moteurs

IMAGE_EXTENSIONS = tuple(CONFIG.nommage["extensions_image"])

# Radicaux respectifs des deux corpus de fichiers, retirés pour obtenir la clé commune.
IMAGE_PREFIX = CONFIG.nommage["prefixe_image"]
ANNOTATION_PREFIX = CONFIG.nommage["prefixe_annotation"]


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
