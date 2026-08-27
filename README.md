# extraction-comptes-sociaux-llm

Extraction automatique de tableaux depuis des PDFs (comptes sociaux) via OCR et LLM.

Le pipeline repose sur [marker-pdf](https://github.com/datalab-to/marker) (OCR neuronal via [Surya](https://github.com/datalab-to/surya)) complété par un LLM pour la correction et la structuration des tableaux.

---

## Architecture

```
extraction-comptes-sociaux-llm/
│
├── api/                                  ← les services (FastAPI), un dossier = une image Docker
│   │
│   ├── api_centrale/         (port 8000) ← orchestration amont : récupère le PDF du bilan
│   │   │                                   depuis l'API INPI (par SIREN + année), sélectionne
│   │   │                                   et extrait la page utile, dépose le résultat sur S3
│   │   ├── main_centrale.py              ← routes /extract/{siren}, /files
│   │   ├── Dockerfile
│   │   ├── requirements.txt              ← dépendances (pip, pas uv)
│   │   └── README.md
│   │
│   ├── marker_proxy/         (port 1324) ← proxy LLM : relaie les appels OpenAI vers le LLM
│   │   │                                   distant, ajoute le tracing Langfuse, et adapte les
│   │   │                                   réponses (json_schema → json_object, dé-emballage JSON)
│   │   ├── src/proxy.py
│   │   ├── pyproject.toml / uv.lock
│   │   └── Dockerfile
│   │
│   ├── api_marker/           (port 8001) ← OCR + structuration JSON via marker-pdf   ⚠ GPU
│   │   │                                   appelle marker_proxy pour la correction LLM
│   │   ├── src/main_marker.py            ← endpoint /extract (charge les modèles au démarrage)
│   │   ├── pyproject.toml / uv.lock      ← torch épinglé cu128 (cf. section GPU)
│   │   └── Dockerfile
│   │
│   ├── api_opendataloader/   (port 8002) ← extraction alternative (Java/OpenDataLoader),
│   │   │                                   renvoie du HTML ; sert de comparatif à marker
│   │   ├── src/main_opendataloader.py
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   │
│   └── api_chandra/          (port 8003) ← extraction via le VLM Chandra (vllm) : /extract prend
│       │                                   un `pdf` (rendu page par page) ou une `image` (envoyée
│       │                                   telle quelle), Chandra renvoie du HTML <table>
│       ├── src/main_chandra.py            ← parsé ensuite en JSON
│       └── pyproject.toml / uv.lock
│
├── libs/                                 ← package partagé `extraction-common` (installé éditable)
│   ├── pyproject.toml
│   └── src/
│       ├── extraction_common/
│       │   └── s3.py                     ← `get_s3_fs()` : client S3 (MinIO/AWS) depuis l'env
│       └── data_management/              ← logique marker
│           ├── extract_image_to_json.py  ← config marker, device GPU/CPU, batch sizes, gestion OOM
│           └── pdf_to_image.py           ← conversion PDF → image (PyMuPDF)
│
├── scripts/                              ← orchestration & évaluation (lancés en local via uv)
│   ├── extraction_pdf_via_api.py         ← pilote : lit les PDFs sur S3 → API → écrit le JSON sur S3
│   ├── json_to_csv.py                    ← convertit les JSON de sortie (marker/ODL) en CSV sur S3
│   ├── comparaison_pdf_csv.py            ← apparie PDFs et annotations XLSX de référence
│   ├── evaluation_extraction.py          ← compare CSV prédits vs annotations XLSX (métriques)
│   └── pyproject.toml / uv.lock
│
├── kubernetes/                           ← déploiement SSP Cloud (namespace projet-extraction-tableaux)
│   ├── deployment-*.yaml                 ← api-centrale, api-marker, marker-proxy
│   ├── service-*.yaml / ingress-*.yaml
│   └── deploy.sh                         ← applique les manifests + crée le secret `app-env` depuis .env
│
├── .github/workflows/
│   └── image-build.yml                   ← CI : build & push des images Docker (api_centrale,
│                                            api_marker, marker_proxy, api_opendataloader)
│
├── legacy/                               ← anciens scripts/PoC (marker_single CLI, vllm batch…),
│                                            conservés pour référence, hors pipeline actuel
│
├── .env                                  ← configuration locale (cf. section Configuration)
└── README.md
```

### Flux principal

```
api_centrale ──(PDF page bilan → S3)──>  scripts/extraction_pdf_via_api.py
                                                  │  POST /extract (PDF)
                                                  ▼
                                            api_marker  ──(appels LLM)──>  marker_proxy ──> LLM distant
                                          (OCR Surya, GPU)                 (+ Langfuse)
                                                  │  JSON structuré → S3
                                                  ▼
                                          scripts/json_to_csv.py ──> scripts/evaluation_extraction.py
```

`api_marker` et `marker_proxy` fonctionnent en tandem : `api_marker` fait tourner l'OCR neuronal (Surya) en local sur GPU et appelle `marker_proxy` pour la correction LLM des tableaux ; le proxy relaie vers le LLM distant en ajoutant le tracing Langfuse. `api_opendataloader` et `api_chandra` sont des moteurs d'extraction alternatifs, comparés à marker via le pipeline d'évaluation.

---

## Prérequis

- **Python 3.13** (géré par `uv`)
- **[uv](https://docs.astral.sh/uv/)** (`pip install uv` ou `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- **GPU NVIDIA avec ≥ 16 Go de VRAM** — voir [section GPU](#gpu--performances) ci-dessous
- Accès S3 (MinIO SSP Cloud ou AWS)
- Accès à un LLM compatible OpenAI (ex. `https://llm.lab.sspcloud.fr`)

---

## Configuration

Copier `.env.example` en `.env` à la racine (ou exporter les variables dans le shell) :

```dotenv
# LLM distant (utilisé par marker_proxy)
REAL_LLM_BASE_URL=https://llm.lab.sspcloud.fr/api
REAL_LLM_API_KEY=<votre-clé>

# Proxy marker (utilisé par api_marker pour joindre marker_proxy)
# Défaut : http://localhost:1324/v1 — ne pas changer en local
PROXY_URL=http://localhost:1324/v1

# S3
AWS_S3_BUCKET=<nom-bucket>
AWS_ACCESS_KEY_ID=<votre-clé>
AWS_SECRET_ACCESS_KEY=<votre-secret>
AWS_SESSION_TOKEN=          # optionnel
AWS_S3_ENDPOINT=<nom-endpoint>
AWS_REGION=us-east-1

# Langfuse (optionnel, désactivé si absent)
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://langfuse.lab.sspcloud.fr
```

---

## Démarrage local

Les deux services doivent être lancés **avant** d'exécuter le script d'extraction.

### 1. marker_proxy (port 1324)

```bash
cd api/marker_proxy
uv run python -m uvicorn proxy:app --host 0.0.0.0 --port 1324 --app-dir src
```

Vérification : `curl http://localhost:1324/health`

### 2. api_marker (port 8001)

```bash
cd api/api_marker
uv run python -m uvicorn main_marker:app --host 0.0.0.0 --port 8001 --app-dir src
```

> **Note :** le démarrage charge tous les modèles Surya en mémoire (~3,5 Go de VRAM). Il peut prendre 1–2 minutes la première fois (téléchargement des poids depuis S3).

### 3. Script d'extraction

```bash
cd scripts

# Traiter tous les PDFs du fichier de correspondances (avec skip automatique des déjà traités)
uv run extraction_pdf_via_api.py --from-parquet

# Tester sur un PDF spécifique
uv run extraction_pdf_via_api.py --pdf-key dossier/fichier.pdf

# Lister les PDFs disponibles dans S3
uv run extraction_pdf_via_api.py --list
```

---

## GPU & Performances

### Pourquoi un GPU est indispensable

`api_marker` utilise les modèles Surya (transformers autorégessifs ~720 M de paramètres).  
Sans GPU, l'inférence OCR est **10 à 50 fois plus lente** qu'avec CUDA.

| Matériel | Vitesse (ordre de grandeur) |
|---|---|
| GPU 16 Go VRAM (ex. A10, RTX 4080) | ~30–60 s / page |
| CPU seul (même 64+ cœurs) | 10–30 min / page |

### Empreinte mémoire GPU (VRAM)

Les modèles sont tous chargés au démarrage de `api_marker` et restent en VRAM :

| Modèle | Dtype | VRAM |
|---|---|---|
| Foundation — recognition (719 M params) | bfloat16 | 1,44 Go |
| Foundation — layout (723 M params) | bfloat16 | 1,45 Go |
| OCR error detection | float16 | 0,27 Go |
| Table recognition | float16 | 0,21 Go |
| Text detection | float16 | 0,08 Go |
| **Total statique** | | **~3,5 Go** |

Pendant l'inférence OCR (KV cache + activations). Mesures sur **NVIDIA A2 16 Go**
(15,36 Go utilisables), LLM déporté sur le proxy, `force_ocr` activé, **sur des PDF de test
synthétiques** (pages peu denses, petites images) :

| `recognition_batch_size` | VRAM pic (reserved) | % de 15,36 Go | Marge |
|---|---|---|---|
| **32 (config actuelle)** | non mesuré sur synthétique | — | choisi pour tenir sur de **vrais scans** |
| 96 | ~10,9 Go | 71 % | ~4,5 Go sur synthétique, mais **OOM sur vrais scans** |
| 128 | ~13,3 Go | 86 % | ~2 Go |
| 160 / 224 | ~14,2 Go | 93 % | ~1 Go — OOM |

⚠️ Ces chiffres sont **optimistes** : ils viennent de PDF synthétiques. Sur de vrais scans
de comptes sociaux, un batch 96 a saturé les 14,6 Go (OOM) — d'où le repli à **32**. Au-delà
de ~160 le débit ne progresse plus de toute façon (le nombre de lignes par page plafonne le
batch effectif), donc monter le batch ne fait que rogner la marge sans gain de vitesse.

**La VRAM ne dépend pas que du batch, mais aussi de la taille des images** : sur de grandes
images haute résolution, chaque crop de ligne est plus lourd, ce qui explique l'OOM à 96 sur
de vrais scans alors que les pages synthétiques (plus petites) tenaient à 11-12 Go.

**Règle : sur un GPU 16 Go traitant de vrais scans, garder `recognition_batch_size` ≤ 32**
(config actuelle). Les chiffres du tableau ci-dessus (PDF synthétiques) sont des planchers
optimistes — sur tes documents, surveille `nvidia-smi` et le message `OOM GPU pendant l'OCR
(VRAM: …)` loggué par le serveur pour calibrer. Sur un GPU 24 Go (A10 / L4), on peut monter
plus haut.

> **Fragmentation entre pages.** Sur un run long, une page dense peut faire OOM faute de
> bloc VRAM contigu alors qu'elle tiendrait à froid. `api_marker` pose donc
> `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` au démarrage et vide le cache CUDA
> entre chaque requête. En cas d'OOM, le serveur loggue la stack complète + l'état VRAM
> (`OOM GPU pendant l'OCR (VRAM: …)`) pour aider à recalibrer le batch.

### Paramètres critiques à vérifier avant de lancer

Dans `libs/src/data_management/extract_image_to_json.py` :

```python
config = {
    # 1. LLM activé/désactivé
    #    False → OCR seul, pas d'amélioration des tableaux par LLM
    #    True  → correction des tableaux par LLM (recommandé)
    "use_llm": True,
    # 2. Nom du modèle LLM — doit correspondre à un modèle disponible
    #    Vérifier avec : curl http://localhost:1324/v1/models
    "openai_model": "gemma4-26b-moe",
    # 3. Batch size OCR — sélectionné AUTOMATIQUEMENT selon le device détecté
    #    (cf. bloc `if use_gpu` dans le même fichier) :
    #    CPU : 64   |   GPU A2 16 Go (vrais scans) : 32   |   GPU 24 Go (A10/L4) : plus haut
    "recognition_batch_size": 32,
    # 4. force_ocr — laissé à False : marker décide page par page (les pages scannées
    #    ou à mauvaise couche texte sont OCR-isées, les pages nées-numériques sont lues
    #    nativement, ~100× plus vite). Passer à True pour tout forcer dans le réseau.
    "force_ocr": False,
}
```

> **Attention :** `marker` ignore silencieusement les valeurs booléennes `False` dans sa configuration interne (`generate_config_dict` filtre toutes les valeurs falsy). `use_llm: False` est donc équivalent à ne pas spécifier la clé — le LLM est désactivé dans les deux cas.

### Checklist avant d'ouvrir le service sur GPU

- [ ] Le pod/service dispose d'un GPU avec ≥ 16 Go de VRAM
- [ ] `recognition_batch_size` ≤ 32 pour 16 Go de VRAM sur de vrais scans (profil GPU auto)
- [ ] `openai_model` correspond à un modèle disponible (`curl http://localhost:1324/v1/models`)
- [ ] `REAL_LLM_API_KEY` est définie et valide
- [ ] `PROXY_URL` pointe vers `marker_proxy` (ex. `http://marker-proxy:1324/v1` en Kubernetes)
- [ ] Le démarrage de `api_marker` s'est terminé sans erreur (les modèles sont bien chargés en VRAM)
- [ ] `curl http://localhost:8001/docs` répond (swagger accessible)
- [ ] `curl http://localhost:1324/health` renvoie `{"status": "ok", "real_llm_configured": true}`

---


## Troubleshooting

| Symptôme | Cause probable | Solution |
|---|---|---|
| OCR très lent (>10 min/page) | Inférence sur CPU, pas de GPU | Vérifier que CUDA est disponible dans le pod |
| `400 - Model not found` dans les logs | `openai_model` incorrect | Vérifier les modèles disponibles via `/v1/models` |
| `use_llm: False` n'a aucun effet | Bug marker : les valeurs `False` sont filtrées | Comportement normal, `False` = LLM désactivé |
| `AssertionError: openai_api_key` au démarrage | `REAL_LLM_API_KEY` non définie | Définir la variable d'environnement |
| OOM GPU pendant l'OCR | `recognition_batch_size` trop élevé | Réduire (24, 16…) et les autres `*_batch_size` à proportion. Le serveur loggue `OOM GPU pendant l'OCR (VRAM: …)` |
| Une modif de `libs/src/**` ne change rien au comportement | `extraction-common` installé en **copie figée** dans le venv | `editable = true` dans `[tool.uv.sources]` puis `uv sync`. Vérifier : `uv run python -c "import data_management.extract_image_to_json as m; print(m.__file__)"` doit pointer vers `libs/src/...`, pas vers `.venv/...` |
| `torch.cuda.is_available() == False` | wheel torch incompatible avec le driver (CUDA build ≠ driver) | Épingler un build torch compatible (voir `pyproject.toml`, index `pytorch-cu128` pour driver CUDA 12.x) |
| Les tableaux ne sont pas corrigés | `use_llm: False` dans la config | Passer à `use_llm: True` |
