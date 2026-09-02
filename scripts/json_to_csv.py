#!/usr/bin/env python3
"""
Conversion des sorties brutes des moteurs (JSON marker/chandra, HTML OpenDataLoader) en
tableaux CSV, vers S3.

Ce module est **l'entrée de l'étape 2** : la CLI, et le point d'import des autres scripts.
Le code vit dans le package [`conversion/`](conversion/__init__.py), un module par étape de
la chaîne — `grid` (mise en forme), `html_tables` (parseur), `chandra` (blocs et
recollage), `extractors` (un lecteur par moteur), `pipeline` (S3), `cli` (arguments).

Une **méthode** est un couple (corpus, moteur) : les préfixes d'entrée et de sortie,
l'extension lue et l'extracteur qui sait la lire viennent tous de `config/*.yaml`, section
`moteurs`. Les moteurs d'un corpus qui en déclare un sont suffixés (`chandra` pour les
comptes sociaux, `chandra_historiques` pour les tableaux historiques) : convertir un
nouveau moteur ne demande donc qu'une entrée dans le fichier de configuration de son
corpus, et rien ici.

**Les choix de conversion et les mesures qui les fondent sont dans [README.md](README.md)**,
section « Étape 2 » : traitement des fusions, tolérance du parseur, découpage des lignes
empilées, placement des sous-lignes d'en-tête, recollage des blocs chandra.

Usage (`--method all` par défaut ; `--list` énumère les méthodes configurées) :
    uv run json_to_csv.py --list
    uv run json_to_csv.py --method marker
    uv run json_to_csv.py --method chandra_historiques
    uv run json_to_csv.py --method all
    uv run json_to_csv.py --method marker --overwrite   (régénère au lieu d'ignorer)
"""

from conversion.chandra import _merge_chandra_blocks, _normalize_chandra_table
from conversion.cli import main
from conversion.extractors import (
    EXTRACTORS,
    ChandraTableExtractor,
    MarkerTableExtractor,
    OpenDataLoaderTableExtractor,
    TableExtractor,
)
from conversion.grid import Table, _is_numeric_cell, _normalize_grid, _rectangularize
from conversion.html_tables import _parse_html_tables
from conversion.pipeline import _stale_csv_paths, _to_csv_bytes, methods, run_pipeline

# Ce que les autres scripts, les tests et le site nomment. Les réexports gardent
# `from json_to_csv import …` valable quel que soit le module qui porte désormais le code.
__all__ = [
    "EXTRACTORS",
    "ChandraTableExtractor",
    "MarkerTableExtractor",
    "OpenDataLoaderTableExtractor",
    "Table",
    "TableExtractor",
    "_is_numeric_cell",
    "_merge_chandra_blocks",
    "_normalize_chandra_table",
    "_normalize_grid",
    "_parse_html_tables",
    "_rectangularize",
    "_stale_csv_paths",
    "_to_csv_bytes",
    "main",
    "methods",
    "run_pipeline",
]


if __name__ == "__main__":
    main()
