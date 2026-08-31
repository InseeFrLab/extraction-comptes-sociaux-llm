#!/usr/bin/env python3
"""Construit le jeu de données du corpus « tableaux historiques » pour le site.

Même sortie que `build_data.py`, même structure de JSON, donc même comparateur côté
navigateur (`js/comparateur.js`). Ne changent que la référence et l'appariement, tous deux
importés de la branche « historiques » de `scripts/evaluation.py` pour que le site et la
mesure ne puissent pas diverger :

- l'annotation est un fichier HTML, `annotations/tableaux historiques/ground_truth_*.html` ;
- elle est appariée à `tableaux_historiques/output_csv/chandra/{crop}_1.csv` par le nom de
  fichier, chaque image portant un tableau entier.

Usage :
    uv run --project website python website/scripts/build_data_historiques.py
    uv run --project website python website/scripts/build_data_historiques.py --limit 5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RACINE_SITE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE_SITE.parent / "scripts"))

import evaluation as E  # noqa: E402
from build_data import compare_pair, load_csv, telecharger_apercus  # noqa: E402
from extraction_common.s3 import get_s3_fs  # noqa: E402

import config  # noqa: E402

CONFIG = config.charger("historiques")

S3_APERCUS = CONFIG.site["apercus"]["prefixe"]
OUT_PATH = RACINE_SITE / CONFIG.site["donnees"]
MATCH_THRESHOLD = CONFIG.evaluation["seuil_similarite"]

# Le site ne publie que des **moteurs**, pas les conditions d'expérience — d'où le drapeau
# `publie` de la config. `evaluation.py` en mesure quatre sur ce corpus : trois variantes de
# chandra qui ne changent qu'un réglage (prompt, plafond de sortie) et sont là pour être
# comparées entre elles, pas pour être montrées comme d'autres modèles. Le site s'en tient
# donc à `chandra`, notre appel direct, le seul moteur joué sur ce corpus : deux modèles au
# total sur le site, marker et chandra, marker n'ayant jamais tourné sur les historiques.
METHODS: dict[str, str] = CONFIG.publies()


def _pct(taux: float | None) -> str:
    """Taux de récupération en pourcent, ou `n/a` s'il n'y en a pas."""
    return "n/a" if taux is None else f"{taux:.0%}"


def build(limit: int | None = None) -> dict:
    """Construit le jeu de données du corpus historique.

    Args:
        limit: si fourni, ne traite que les N premiers tableaux (itération rapide).

    Returns:
        Dict sérialisable : métadonnées et liste des tableaux comparés.
    """
    fs = get_s3_fs()
    print("Lecture des annotations et des prédictions…", flush=True)
    par_moteur: dict[str, dict] = {}
    for methode, prefixe in METHODS.items():
        pairs, counts = E.list_pairs_historiques(fs, CONFIG.annotations, prefixe)
        par_moteur[methode] = {
            "pairs": {cle: (ann, chemin) for cle, ann, chemin in pairs},
            "counts": counts,
        }
        print(f"  {methode:<16} {len(pairs)} paire(s)")

    print("Comparaison…", flush=True)
    tables = []
    # L'union des clés, et non l'intersection : un tableau qu'un seul moteur a su extraire
    # doit rester visible, avec la colonne de l'autre vide.
    cles = sorted({cle for m in par_moteur.values() for cle in m["pairs"]})
    for cle in cles:
        if limit and len(tables) >= limit:
            break
        entry: dict = {"id": cle, "methods": {}}
        for methode, donnees in par_moteur.items():
            if cle not in donnees["pairs"]:
                continue
            ann, chemin = donnees["pairs"][cle]
            result = compare_pair(ann, load_csv(fs, chemin), MATCH_THRESHOLD)
            if result is None:
                continue
            # Le découpage est propre au moteur : deux moteurs ne segmentent pas pareil.
            result["nPredTables"] = donnees["counts"].get(cle, 0)
            entry["methods"][methode] = result
        if not entry["methods"]:
            print(f"  {cle:<16} aucune grille exploitable, ignoré")
            continue

        # L'annotation ne dépend d'aucun moteur — pas de recollage de saut de page sur ce
        # corpus — donc elle est portée une seule fois par le tableau.
        premier = next(iter(entry["methods"].values()))
        entry["ann"] = premier.pop("ann")
        entry["annHeaderRows"] = premier["annHeaderRows"]
        entry["annHeaderCols"] = premier["annHeaderCols"]
        for result in entry["methods"].values():
            result.pop("ann", None)
        entry["annRows"] = len(entry["ann"])
        entry["annCols"] = len(entry["ann"][0]) if entry["ann"] else 0
        tables.append(entry)
        resume = "  ".join(f"{m}={_pct(r['recoveryRate'])}" for m, r in entry["methods"].items())
        print(f"  {cle:<16} {resume}", flush=True)

    print("Aperçus des documents…", flush=True)
    # Une image par tableau : le nom de l'aperçu est la clé, sans regroupement à faire.
    apercus = telecharger_apercus(fs, S3_APERCUS, "historiques", lambda stem: stem)
    for entry in tables:
        images = apercus.get(entry["id"], [])
        if images:
            entry["apercus"] = images

    return {
        "meta": {
            "source": f"s3://{CONFIG.site['racine']}",
            "methods": list(METHODS),
            "nTables": len(tables),
            "matchThreshold": MATCH_THRESHOLD,
            "note": (
                "Grilles publiées telles quelles. Source : tableaux statistiques "
                "historiques scannés, annotés à la main en HTML. `chandra` est notre "
                "appel direct à l'API, le seul moteur joué sur ce corpus. Les conditions "
                "d'expérience qui n'en changent qu'un réglage sont mesurées par "
                "`scripts/evaluation.py` et ne sont pas publiées ici."
            ),
        },
        "tables": tables,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="ne traiter que N tableaux")
    parser.add_argument("--out", type=Path, default=OUT_PATH, help="chemin de sortie JSON")
    args = parser.parse_args()

    payload = build(limit=args.limit)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    size_kb = args.out.stat().st_size / 1024
    print(f"\n{args.out} — {payload['meta']['nTables']} tableaux, {size_kb:.0f} Ko")


if __name__ == "__main__":
    main()
