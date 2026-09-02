"""Ligne de commande de `json_to_csv.py` : choix de la méthode et lancement du pipeline."""

import argparse

from extraction_common.s3 import get_s3_fs

from .pipeline import methods, run_pipeline


def _parse_args(available: list[str]) -> argparse.Namespace:
    """Analyse les arguments de la commande.

    Args:
        available: clés de méthode acceptées par `--method`, hors `all`.

    Returns:
        Les arguments analysés (`method`, `list`, `overwrite`).
    """
    parser = argparse.ArgumentParser(description="Conversion des sorties de moteurs en CSV")
    parser.add_argument(
        "--method",
        choices=[*available, "all"],
        default="all",
        help="Méthode à convertir, c'est-à-dire un couple corpus × moteur (défaut : all)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="énumérer les méthodes configurées, avec leurs préfixes S3",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Réécrire les sorties existantes et supprimer les rangs surnuméraires. "
            "Nécessaire dès que le code de conversion change, sans quoi les fichiers déjà "
            "convertis sont ignorés et le dossier mélange deux générations."
        ),
    )
    return parser.parse_args()


def _print_methods(sources: dict) -> None:
    """Énumère les méthodes configurées, corpus et préfixes S3 compris.

    Args:
        sources: méthodes convertibles, telles que `methods()` les rend.
    """
    width = max(len(m) for m in sources)
    for name, engine in sources.items():
        print(f"  {name:<{width}}  [{engine.corpus}]  {engine.json}  →  {engine.csv}")


def main() -> None:
    """Point d'entrée : convertit une méthode, toutes, ou énumère les méthodes."""
    sources = methods()
    args = _parse_args(list(sources))

    if args.list:
        _print_methods(sources)
        return

    fs = get_s3_fs()
    selected = list(sources) if args.method == "all" else [args.method]
    for method in selected:
        run_pipeline(method, fs, overwrite=args.overwrite)
