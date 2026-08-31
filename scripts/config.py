"""Chargement de la configuration des corpus, depuis `config/*.yaml`.

Un fichier par corpus d'origine, qui porte **tout ce que le pipeline a de variable** :
chemins S3, moteurs comparés, paramètres transmis aux APIs, seuils de la mesure, réglages
du site. Les scripts n'en gardent aucun en dur — ils lisent d'ici. Ce qui reste codé, et
pourquoi, est documenté dans [README.md](README.md).

    import config

    cfg = config.charger("comptes-sociaux")
    cfg.annotations                     # 'projet-extraction-tableaux/annotations/clean'
    cfg.moteurs["marker"].csv           # …/reprise/output_csv/marker
    cfg.evaluation["seuil_similarite"]  # 0.5

Les chemins déclarés dans le YAML sont relatifs au bucket ; ils sont rendus ici préfixés du
bucket et **sans schéma** `s3://`, forme attendue par `s3fs`. Le schéma est ajouté par les
rares endroits qui l'affichent.

Le dossier lu est `config/` à la racine du dépôt, ou celui que désigne la variable
d'environnement `EXTRACTION_CONFIG_DIR` — de quoi rejouer une chaîne sur d'autres préfixes
sans toucher au dépôt.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Les URLs des APIs sont surchargeables par `.env` : il doit être lu avant que la
# configuration ne résolve ces variables, donc dès l'import de ce module.
load_dotenv()

RACINE = Path(__file__).resolve().parent.parent
DOSSIER = Path(os.getenv("EXTRACTION_CONFIG_DIR") or RACINE / "config")


@dataclass(frozen=True)
class Moteur:
    """Un moteur d'extraction (ou une condition d'expérience) d'un corpus.

    Attributes:
        nom: clé du moteur dans son corpus.
        corpus: nom du corpus auquel il appartient.
        methode: clé de `json_to_csv.py --method`, unique tous corpus confondus.
        json: préfixe S3 des sorties brutes de l'extraction.
        csv: préfixe S3 des CSV convertis.
        extension: extension des fichiers rendus par l'API, et lus par la conversion.
        extracteur: extracteur de `json_to_csv.py` qui sait lire ce format.
        api_url: URL de l'API d'extraction, ou None si plus rien ne produit ce moteur.
        parametres: champs transmis tels quels à l'API d'extraction.
        retirer_prefixe: préfixe à retirer du nom de fichier d'entrée à la conversion.
        appariement: mode d'appariement à l'annotation, s'il diffère de celui du corpus.
        fusion_coupures_page: regrouper les annotations séparées par une coupure de page.
        publie: le site montre ce moteur.
    """

    nom: str
    corpus: str
    methode: str
    json: str
    csv: str
    extension: str
    extracteur: str
    api_url: str | None = None
    parametres: dict = field(default_factory=dict)
    retirer_prefixe: str = ""
    appariement: str = ""
    fusion_coupures_page: bool = False
    publie: bool = False


@dataclass(frozen=True)
class CorpusConfig:
    """Configuration d'un corpus, chemins S3 déjà résolus.

    Attributes:
        nom: nom du corpus, celui des `--corpus` des scripts.
        bucket: bucket S3 qui porte l'ensemble du corpus.
        sources: entrées du corpus (pdf, images, annotations, correspondances).
        nommage: conventions de nommage des fichiers, quand le corpus en a.
        evaluation: réglages de la mesure (sortie, seuils, appariement, affichage).
        site: ce que le site publie du corpus.
        moteurs: moteurs comparés, dans l'ordre du fichier.
    """

    nom: str
    bucket: str
    sources: dict[str, str]
    nommage: dict
    evaluation: dict
    site: dict
    moteurs: dict[str, Moteur]

    def s3(self, chemin: str) -> str:
        """Chemin relatif au bucket, rendu absolu.

        Args:
            chemin: chemin relatif au bucket du corpus.

        Returns:
            Le chemin préfixé du bucket, sans schéma `s3://`.
        """
        return f"{self.bucket}/{chemin.lstrip('/')}"

    @property
    def annotations(self) -> str:
        """Préfixe S3 des annotations de référence."""
        return self.sources["annotations"]

    def publies(self) -> dict[str, str]:
        """Moteurs que le site publie.

        Returns:
            {nom du moteur: préfixe S3 de ses CSV}, dans l'ordre du fichier de config.
        """
        return {nom: m.csv for nom, m in self.moteurs.items() if m.publie}

    def pilotes(self) -> dict[str, Moteur]:
        """Moteurs qu'un script d'extraction sait encore produire (ceux qui ont une API)."""
        return {nom: m for nom, m in self.moteurs.items() if m.api_url}


def _moteur(corpus: str, nom: str, brut: dict, bucket: str, suffixe: str) -> Moteur:
    """Construit un `Moteur` depuis son bloc YAML, chemins résolus.

    Args:
        corpus: nom du corpus.
        nom: clé du moteur.
        brut: bloc YAML du moteur.
        bucket: bucket du corpus.
        suffixe: suffixe des clés de `--method`, propre au corpus.

    Returns:
        Le moteur, prêt à l'emploi.
    """
    api = brut.get("api") or {}
    # L'URL de l'API est surchargeable par environnement : les scripts tournent en local
    # contre des services démarrés à la main, dont le port peut changer.
    url = os.getenv(api["variable_url"], api["url"]) if api else None
    return Moteur(
        nom=nom,
        corpus=corpus,
        methode=f"{nom}{suffixe}",
        json=f"{bucket}/{brut['json']}",
        csv=f"{bucket}/{brut['csv']}",
        extension=brut["extension"],
        extracteur=brut["extracteur"],
        api_url=url,
        parametres=dict(brut.get("parametres") or {}),
        retirer_prefixe=brut.get("retirer_prefixe", ""),
        appariement=brut.get("appariement", ""),
        fusion_coupures_page=bool(brut.get("fusion_coupures_page", False)),
        publie=bool(brut.get("publie", False)),
    )


@cache
def charger(nom: str) -> CorpusConfig:
    """Charge la configuration d'un corpus.

    Args:
        nom: nom du corpus, c'est-à-dire le nom de son fichier dans `config/`.

    Returns:
        La configuration, chemins S3 préfixés du bucket.

    Raises:
        SystemExit: si le corpus n'a pas de fichier de configuration.
    """
    chemin = DOSSIER / f"{nom}.yaml"
    if not chemin.exists():
        raise SystemExit(
            f"Corpus inconnu : {nom!r}. Corpus configurés dans {DOSSIER} : "
            f"{', '.join(corpus_disponibles())}"
        )
    brut = yaml.safe_load(chemin.read_text(encoding="utf-8"))
    bucket = brut["bucket"]
    suffixe = brut.get("suffixe_methode", "")

    evaluation = dict(brut["evaluation"])
    evaluation["sortie"] = f"{bucket}/{evaluation['sortie']}"

    site = {k: dict(v) if isinstance(v, dict) else v for k, v in brut.get("site", {}).items()}
    if "apercus" in site:
        site["apercus"]["prefixe"] = f"{bucket}/{site['apercus']['prefixe']}"
    if "racine" in site:
        site["racine"] = f"{bucket}/{site['racine']}"

    return CorpusConfig(
        nom=brut["nom"],
        bucket=bucket,
        sources={k: f"{bucket}/{v}" for k, v in brut["sources"].items()},
        nommage=dict(brut.get("nommage", {})),
        evaluation=evaluation,
        site=site,
        moteurs={
            cle: _moteur(brut["nom"], cle, bloc, bucket, suffixe)
            for cle, bloc in brut["moteurs"].items()
        },
    )


@cache
def corpus_disponibles() -> tuple[str, ...]:
    """Noms des corpus configurés, dans l'ordre alphabétique de leurs fichiers."""
    return tuple(sorted(p.stem for p in DOSSIER.glob("*.yaml")))


@cache
def tous() -> dict[str, CorpusConfig]:
    """Tous les corpus configurés, indexés par nom."""
    return {nom: charger(nom) for nom in corpus_disponibles()}


@cache
def moteurs() -> dict[str, Moteur]:
    """Tous les moteurs de tous les corpus, indexés par clé de `--method`.

    Les clés sont uniques par construction : le suffixe propre à chaque corpus distingue
    deux moteurs homonymes (`chandra` et `chandra_historiques`).

    Returns:
        {clé de méthode: moteur}, corpus dans l'ordre alphabétique.
    """
    return {m.methode: m for cfg in tous().values() for m in cfg.moteurs.values()}
