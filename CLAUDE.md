# CLAUDE.md

Contexte pour Claude Code sur `extraction-comptes-sociaux-llm`.
Le [README.md](README.md) reste la référence pour l'installation, la config `.env`, le GPU et le troubleshooting.

## Objectif

1. **Extraire** les données des tableaux issus de documents scannés (comptes sociaux en PDF), via OCR neuronal (marker/Surya) puis correction et structuration par LLM.
2. **Mettre en forme** le résultat extrait dans un fichier CSV (un CSV par tableau, déposé sur S3).
3. **Comparer** ces CSV aux tableaux annotés manuellement (XLSX de référence) pour mesurer la qualité de l'extraction.

Le repo est donc un pipeline en trois temps : `PDF → JSON → CSV → métriques`.

Deux corpus le traversent, mêmes étapes et mêmes métriques :

| Corpus | Entrée | Référence | Moteurs | Préfixe S3 |
|---|---|---|---|---|
| **Comptes sociaux** | PDF scannés | XLSX (`annotations/clean/`) | marker, chandra | `reprise/` |
| **Tableaux historiques** | images TIFF/PNG, un tableau par fichier | HTML (`annotations/tableaux historiques/`) | chandra | `tableaux_historiques/` |

## Architecture

```
PDF (S3) ──> api_marker (OCR GPU) ──> marker_proxy ──> LLM distant
                    │ JSON (S3)
                    ▼
             json_to_csv.py ──> CSV (S3) ──> evaluation_extraction.py ──> métriques (parquet)
                                                        ▲
                                              annotations XLSX (S3)
```

| Dossier | Rôle |
|---|---|
| `api/` | Services FastAPI, un sous-dossier = une image Docker. `api_marker` (OCR + structuration, **GPU**), `marker_proxy` (relais LLM + tracing Langfuse), `api_opendataloader` et `api_chandra` (moteurs alternatifs, pour comparaison). |
| `libs/` | Package partagé `extraction-common`, installé **en éditable** partout. `extraction_common/s3.py` (client S3), `data_management/` (config marker, batch sizes, PDF → image). |
| `scripts/` | Orchestration et évaluation, lancés en local via `uv`. `extraction_pdf_via_api.py` (étape 1), `json_to_csv.py` (étape 2), `comparaison_pdf_csv.py` + `evaluation_extraction.py` (étape 3). Le corpus historique a ses variantes d'étape 1 et 3 — `extraction_historiques.py`, `evaluation_historiques.py`, `chiffres_site_historiques.py` — et ses conventions dans `corpus_historiques.py` ; l'étape 2 est commune. `apercus.py` fabrique les vignettes des documents sources publiées par le site, pour les deux corpus. |
| `tests/` | Tests unitaires (voir plus bas). |
| `legacy/`, `api/api_centrale/`, `kubernetes/` | **Legacy — ne pas modifier.** Anciens PoC, ancien service de récupération INPI (pip/`requirements.txt`, Python 3.11) et ancien déploiement SSP Cloud. Conservés pour référence, hors périmètre de travail. `legacy/` est exclu du lint. |

Points structurants à connaître avant de modifier du code :

- **Chaque sous-projet a son propre `pyproject.toml` + `uv.lock` et son propre venv.** Toujours lancer les commandes depuis le bon dossier (`cd scripts`, `cd api/api_marker`…).
- **`extraction-common` doit rester en `editable = true`** dans `[tool.uv.sources]` : sinon les modifs de `libs/src/**` ne sont pas prises en compte.
- **La config OCR est centralisée** dans `libs/src/data_management/extract_image_to_json.py` (`use_llm`, `openai_model`, `recognition_batch_size`). Sur GPU 16 Go, garder `recognition_batch_size` ≤ 32 sous peine d'OOM.
- Les chemins S3 sont des constantes en tête de chaque script (`BUCKET`, `METHODS`, `SOURCES`) — ajouter une méthode d'extraction = ajouter une entrée dans ces dicts.
- **`corpus_historiques.py` ne doit dépendre de rien.** Il est importé en cascade par `website/build_data_historiques.py`, dont le venv n'a ni `requests` ni `Pillow` : y ajouter un import tiers casserait le rendu du site.
- **`api_chandra` accepte soit un `pdf`, soit une `image`.** Le dpi (`CHANDRA_DPI`) ne concerne que les PDF, où il est **fixe à 200** — le mode adaptatif page par page qui a existé était mesuré perdant (−149 cellules sur 31 paires) et a été retiré. Une image n'a pas de taille physique, donc pas de dpi : c'est le nombre de pixels envoyés qui décide de ce que le modèle voit, via `cote_max` (2 200 px de grand côté, soit à peu près une A4 lue à 200 dpi).

## Consignes de dev

### Lint & format — ruff

Config partagée à la racine dans [`ruff.toml`](ruff.toml). Avant tout commit :

```bash
uvx ruff check .          # lint (--fix pour corriger l'auto-fixable)
uvx ruff format .         # formatage
```

- Ne pas ajouter de `# noqa` sans commentaire justifiant.
- Le repo entier est formaté par `ruff format` : pas d'alignement manuel (dicts, commentaires en colonnes), le formateur est la référence.

### Tests — pytest

Les tests vivent dans `tests/` à la racine et s'exécutent depuis le venv de `scripts` :

```bash
uv run --project scripts pytest          # depuis la racine du repo
uv run --project scripts pytest -k eval  # un sous-ensemble
```

- Cibler en priorité la **logique pure** : normalisation des nombres, détection des en-têtes, appariement de colonnes, parsing HTML/JSON. C'est là que se jouent les métriques.
- **Aucun test ne doit toucher S3, le GPU ou le LLM.** Les I/O se testent avec des fixtures locales ou des mocks.
- Un correctif sur les métriques ou le parsing s'accompagne d'un test qui échoue sans le correctif.

### Style de code

- **Commentaires succincts** : expliquer le *pourquoi* (contrainte métier, contournement d'un bug de marker, choix de seuil), pas le *quoi* que le code dit déjà.
- **Docstring sur chaque fonction**, avec au minimum les **arguments** et ce que la fonction **retourne** :

```python
def evaluate_pair(
    prediction: pd.DataFrame, annotation: pd.DataFrame, threshold: float = 0.5
) -> dict:
    """Compare un tableau prédit à son annotation de référence.

    Args:
        prediction: tableau extrait (CSV converti).
        annotation: tableau de référence (XLSX).
        threshold: similarité minimale pour apparier deux en-têtes.

    Returns:
        dict des métriques (col_recovery, row_recovery, numeric_recovery, total_extraction).
    """
```

- Annotations de type sur les signatures publiques.
- Français pour les commentaires et docstrings (cohérent avec l'existant), anglais pour les noms de variables et fonctions.
