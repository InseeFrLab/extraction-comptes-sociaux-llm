"""Tests de la configuration des corpus (`config/*.yaml`, lue par `scripts/config.py`).

Ces tests ne lisent pas S3 : ils vérifient que les fichiers de configuration disent bien ce
que le pipeline attend d'eux, et que ce qu'ils désignent existe côté code — extracteur,
mode d'appariement. C'est là que se joue une faute de frappe dans un YAML, invisible tant
qu'un script n'a pas tourné pour de bon.
"""

import evaluation as E
import pytest
from json_to_csv import EXTRACTORS

import config

CORPUS = config.tous()


def test_les_deux_corpus_sont_configures():
    assert set(CORPUS) == {"comptes-sociaux", "historiques"}


@pytest.mark.parametrize("nom", CORPUS)
def test_reglages_de_mesure_presents(nom):
    """Chaque corpus déclare tout ce que `evaluation.py` lui demande."""
    evaluation = CORPUS[nom].evaluation
    assert evaluation.keys() >= {
        "sortie",
        "seuil_similarite",
        "tolerance_colonnes",
        "appariement",
        "largeur_nom",
    }
    assert 0 < evaluation["seuil_similarite"] <= 1
    assert evaluation["tolerance_colonnes"] >= 0


@pytest.mark.parametrize("nom", CORPUS)
def test_chemins_prefixes_du_bucket(nom):
    """Les chemins rendus sont absolus et sans schéma : c'est la forme qu'attend s3fs."""
    cfg = CORPUS[nom]
    chemins = [
        *cfg.sources.values(),
        cfg.evaluation["sortie"],
        cfg.site["apercus"]["prefixe"],
        *(m.json for m in cfg.moteurs.values()),
        *(m.csv for m in cfg.moteurs.values()),
    ]
    for chemin in chemins:
        assert chemin.startswith(f"{cfg.bucket}/"), chemin
        assert "//" not in chemin, chemin


@pytest.mark.parametrize("nom", CORPUS)
def test_annotations_declarees(nom):
    assert CORPUS[nom].annotations


@pytest.mark.parametrize("nom", CORPUS)
def test_appariement_implemente(nom):
    """Le mode d'appariement déclaré doit exister dans la mesure."""
    assert CORPUS[nom].evaluation["appariement"] in E.TRAITEMENTS


@pytest.mark.parametrize("nom", CORPUS)
def test_extracteurs_connus(nom):
    """Chaque moteur désigne un extracteur que `json_to_csv` sait instancier."""
    for moteur in CORPUS[nom].moteurs.values():
        assert moteur.extracteur in EXTRACTORS, moteur.nom


@pytest.mark.parametrize("nom", CORPUS)
def test_extensions_lisibles(nom):
    """`_load` ne sait lire que du JSON ou du texte, et la glob attend un point."""
    for moteur in CORPUS[nom].moteurs.values():
        assert moteur.extension.startswith("."), moteur.nom


def test_cles_de_methode_uniques():
    """Deux moteurs homonymes de corpus différents ne doivent pas se recouvrir.

    `chandra` existe dans les deux corpus : c'est le suffixe déclaré par le corpus qui les
    distingue dans `json_to_csv.py --method`. Sans lui, une conversion écraserait l'autre.
    """
    methodes = [m.methode for cfg in CORPUS.values() for m in cfg.moteurs.values()]
    assert len(methodes) == len(set(methodes))
    assert set(methodes) == set(config.moteurs())


def test_prefixes_de_sortie_distincts():
    """Deux moteurs n'écrivent jamais dans le même dossier de CSV, ni de JSON."""
    for cle in ("json", "csv"):
        prefixes = [getattr(m, cle) for cfg in CORPUS.values() for m in cfg.moteurs.values()]
        assert len(prefixes) == len(set(prefixes)), cle


@pytest.mark.parametrize("nom", CORPUS)
def test_moteurs_publies_sont_des_moteurs(nom):
    """Ce que le site publie est un sous-ensemble de ce que la mesure connaît."""
    cfg = CORPUS[nom]
    assert set(cfg.publies()) <= set(cfg.moteurs)
    assert cfg.publies(), f"{nom} ne publie aucun moteur"


def test_corpus_inconnu():
    with pytest.raises(SystemExit, match="Corpus inconnu"):
        config.charger("corpus-qui-n-existe-pas")


def test_la_mesure_couvre_les_corpus_configures():
    """`evaluation.CORPUS` est construit depuis la configuration, sans liste en dur."""
    assert set(E.CORPUS) == set(CORPUS)
