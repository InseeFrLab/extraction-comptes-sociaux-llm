"""Pipeline S3 : sorties brutes d'une méthode → un CSV par tableau, et compte rendu.

Une **méthode** est un couple (corpus, moteur) : les préfixes d'entrée et de sortie,
l'extension lue et l'extracteur qui sait la lire viennent tous de `config/*.yaml`, section
`moteurs`. Convertir un nouveau moteur ne demande donc qu'une entrée dans le fichier de
configuration de son corpus, et rien ici.
"""

import csv
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import s3fs

import config

from .extractors import EXTRACTORS, RawOutput
from .grid import Table


def methods() -> dict[str, config.Moteur]:
    """Méthodes convertibles, c'est-à-dire les moteurs de tous les corpus configurés.

    Returns:
        {clé de `--method`: moteur}. Les moteurs d'un corpus qui en déclare un sont
        suffixés (`chandra` pour les comptes sociaux, `chandra_historiques` pour les
        tableaux historiques).
    """
    return config.moteurs()


def _to_csv_bytes(table: Table) -> bytes:
    """Sérialise une grille en CSV.

    Args:
        table: grille rectangulaire.

    Returns:
        Le CSV encodé en UTF-8 avec BOM, séparateur `;` — la forme qu'Excel ouvre sans
        boîte de dialogue d'import.
    """
    buf = io.StringIO()
    csv.writer(buf, delimiter=";").writerows(table)
    return buf.getvalue().encode("utf-8-sig")


def _load(fs: s3fs.S3FileSystem, path: str, ext: str) -> RawOutput:
    """Charge un fichier S3.

    Args:
        fs: système de fichiers S3.
        path: clé du fichier à lire.
        ext: extension du fichier, telle que déclarée par le moteur.

    Returns:
        Un dict pour un `.json`, une str pour toute autre extension (HTML).
    """
    if ext == ".json":
        with fs.open(path, "rb") as f:
            return json.load(f)
    else:
        with fs.open(path, "r", encoding="utf-8") as f:
            return f.read()


def _output_stem(file_key: str, strip_prefix: str) -> str:
    """Radical des CSV de sortie, déduit du nom du fichier d'entrée.

    Args:
        file_key: clé S3 du fichier d'entrée.
        strip_prefix: préfixe de nommage à retirer, déclaré par le moteur.

    Returns:
        Le radical, qui numéroté donne `{radical}_{n}.csv`.
    """
    return Path(file_key).stem.removeprefix(strip_prefix)


def _stale_csv_paths(existing: list[str], siren: str, kept: int) -> list[str]:
    """Chemins des CSV d'un passage antérieur devenus surnuméraires.

    Une régénération produisant moins de tableaux qu'avant laisserait sinon les rangs
    excédentaires en place, et le dossier de sortie mélangerait deux générations de
    conversion.

    Args:
        existing: chemins présents dans le dossier de sortie.
        siren: radical du fichier source.
        kept: nombre de tableaux écrits par le passage courant.

    Returns:
        Les chemins `{siren}_{n}.csv` dont le rang dépasse `kept`. Les fichiers d'un autre
        radical ne sont jamais retournés, y compris quand un radical en préfixe un autre.
    """
    pattern = re.compile(rf"^{re.escape(siren)}_(\d+)\.csv$")
    stale = []
    for path in existing:
        match = pattern.match(Path(path).name)
        if match and int(match.group(1)) > kept:
            stale.append(path)
    return stale


def _write_tables(
    fs: s3fs.S3FileSystem,
    cfg: config.Moteur,
    siren: str,
    tables: list[Table],
    overwrite: bool,
) -> None:
    """Écrit un CSV par tableau, et nettoie les rangs d'un passage antérieur.

    Args:
        fs: système de fichiers S3.
        cfg: moteur converti, qui porte le préfixe de sortie.
        siren: radical des fichiers de sortie.
        tables: grilles à écrire, dans l'ordre.
        overwrite: le passage courant régénère une sortie existante — les rangs
            surnuméraires du passage précédent sont alors supprimés.
    """
    for i, table in enumerate(tables, start=1):
        fs.pipe(f"{cfg.csv}/{siren}_{i}.csv", _to_csv_bytes(table))
    if overwrite:
        for path in _stale_csv_paths(fs.glob(f"{cfg.csv}/{siren}_*.csv"), siren, len(tables)):
            fs.rm(path)


@dataclass
class _Report:
    """Ce qu'un passage de conversion a fait de ses fichiers d'entrée.

    Attributes:
        method: clé de la méthode convertie.
        converted: fichiers convertis, CSV écrits.
        skipped: fichiers ignorés parce que leur sortie existait déjà.
        empty: radicaux des fichiers dont aucun tableau n'a été tiré.
        failed: radicaux des fichiers en erreur de lecture ou de conversion.
    """

    method: str
    converted: int = 0
    skipped: int = 0
    empty: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def print_summary(self) -> None:
        """Affiche le compte rendu du passage, cas particuliers nommés."""
        print(
            f"\n[{self.method}] terminé — {self.converted} traité(s), {self.skipped} ignoré(s), "
            f"{len(self.empty)} sans tableau, {len(self.failed)} en erreur"
        )
        # Un fichier sans tableau ne produit aucun CSV : il disparaît de l'évaluation, qui
        # ne compare que les paires existantes. Le nommer est le seul moyen de le voir.
        if self.empty:
            print(f"  sans tableau, donc absents de l'évaluation : {', '.join(self.empty)}")
        if self.failed:
            print(f"  en erreur de lecture ou de conversion : {', '.join(self.failed)}")
        if self.skipped:
            print(
                f"  sortie déjà présente, non régénérée : {self.skipped} fichier(s) "
                "— voir --overwrite"
            )
        print()


def run_pipeline(method: str, fs: s3fs.S3FileSystem, overwrite: bool = False) -> None:
    """Convertit en CSV toutes les sorties brutes d'une méthode.

    Args:
        method: clé de `methods()`, c'est-à-dire un couple (corpus, moteur).
        fs: système de fichiers S3.
        overwrite: réécrire les CSV déjà présents, et supprimer les rangs surnuméraires.
    """
    cfg = methods()[method]
    extractor = EXTRACTORS[cfg.extracteur]

    input_files = sorted(fs.glob(f"{cfg.json}/*{cfg.extension}"))
    print(f"[{method}] {len(input_files)} fichier(s) trouvé(s)\n")

    report = _Report(method)
    for file_key in input_files:
        siren = _output_stem(file_key, cfg.retirer_prefixe)
        if not overwrite and fs.exists(f"{cfg.csv}/{siren}_1.csv"):
            print(f"  [SKIP]  {siren}")
            report.skipped += 1
            continue

        try:
            data = _load(fs, file_key, cfg.extension)
            tables = extractor.extract(data)
        # Un fichier illisible ou inattendu ne doit pas arrêter le passage : il est
        # nommé dans le compte rendu, et la conversion continue.
        except Exception as e:
            print(f"  [ERR]   {siren}: {e}")
            report.failed.append(siren)
            continue

        if not tables:
            print(f"  [VIDE]  {siren}: aucun tableau détecté")
            report.empty.append(siren)
            continue

        _write_tables(fs, cfg, siren, tables, overwrite=overwrite)
        print(f"  [OK]    {siren}: {len(tables)} tableau(x)")
        report.converted += 1

    report.print_summary()
