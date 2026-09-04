# Site du projet d'extraction

Site Quarto statique présentant le projet : architecture des moteurs d'extraction, étapes
de mise en forme, méthode d'évaluation, résultats mesurés et comparaison tableau annoté /
tableau extrait.

Déployé sur GitHub Pages à chaque push sur `main`
([`.github/workflows/publish-site.yml`](../.github/workflows/publish-site.yml)).

## Structure

Un dossier par nature de fichier ; seuls `_quarto.yml` et la page d'accueil restent à la
racine.

```
website/
├── _quarto.yml   configuration : navigation, thème, ressources
├── index.qmd     page d'accueil
├── pages/        les autres pages
├── scripts/      Python : données du site, chiffres publiés, aperçus
├── styles/       thème clair et thème sombre (SCSS)
├── icons/        favicon et logo de la barre de navigation
└── js/           le comparateur de grilles
```

### Pages

| Fichier | Rôle |
|---|---|
| `index.qmd` | Introduction — objectif, chaîne de traitement, corpus, chiffres-clés, plan du site. |
| `pages/architecture.qmd` | Les deux moteurs (marker, chandra), les services qui les portent, et de vrais exemples de leur sortie sur le même PDF. |
| `pages/conversion.qmd` | L'étape `json_to_csv.py` en six étapes : de la sortie du moteur à la grille CSV. |
| `pages/evaluation.qmd` | La méthode de `scripts/evaluation.py` : constitution des paires, délimitation des zones, normalisation, appariement, métriques. |
| `pages/resultats.qmd` | Toutes les mesures et les cas particuliers rencontrés, corpus des comptes sociaux. |
| `pages/resultats-historiques.qmd` | Les mesures du corpus des tableaux statistiques historiques, en version resserrée. |
| `pages/mise-en-forme.qmd` | Les écarts qui viennent d'une différence d'écriture entre annotation et moteur, pas d'une erreur d'extraction. |
| `pages/ameliorations.qmd` | Cinq leviers classés par impact mesuré, plus les corrections de mesure. |
| `pages/comparaison.qmd` | Visualiseur : grille annotée face à la grille extraite, cellule par cellule (comptes sociaux). |
| `pages/comparaison-historiques.qmd` | Le même visualiseur, sur le corpus des tableaux historiques. |
| `pages/_comparateur.qmd` | Contrôles, conteneur et légende du visualiseur, inclus par les deux pages. |

Les pages `architecture.qmd`, `conversion.qmd` et `evaluation.qmd` décrivent le pipeline ;
`resultats.qmd`, `mise-en-forme.qmd` et `ameliorations.qmd` en donnent la mesure. Les liens
entre pages passent par des ancres explicites (`{#id}`), pour ne pas dépendre de la
translittération automatique des titres accentués par Pandoc.

Les pages `.qmd` ne contiennent **aucun code exécuté** (`execute: enabled: false`) :
Quarto ne fait que rendre du markdown.

### Scripts

| Fichier | Rôle |
|---|---|
| `scripts/build_data.py` | Produit `data/comparaisons.json` depuis S3, et rapatrie les aperçus dans `data/apercus/`. |
| `scripts/build_data_historiques.py` | Produit `data/comparaisons-historiques.json` depuis S3, aperçus compris. |
| `scripts/chiffres_site.py` | Recalcule les chiffres en dur des pages « Résultats », `--corpus comptes-sociaux` ou `--corpus historiques`. |
| `scripts/apercus.py` | Fabrique les vignettes des documents sources et les dépose sur S3 (une seule fois, cf. « Données »). |

Les trois premiers réutilisent les fonctions de `scripts/evaluation.py`, à la racine du
dépôt, plutôt que d'en réimplémenter une variante qui divergerait : le site doit décrire le
pipeline en place. Ils l'ajoutent au `sys.path` par chemin relatif.

### Front

| Fichier | Rôle |
|---|---|
| `js/comparateur.js` | Le visualiseur lui-même. La page hôte pose `window.DX_SOURCE` et `window.DX_BASE` avant de le charger. |
| `styles/styles.scss`, `styles/styles-dark.scss` | Thème clair et thème sombre. |
| `icons/favicon.svg` | Favicon, et logo de la barre de navigation. |

Les pages vivant dans `pages/`, elles atteignent ces fichiers et les données par `../` —
d'où `window.DX_BASE = "../"`, que le comparateur applique aux chemins que porte le JSON
(sa propre URL et celles des aperçus), écrits depuis la racine du site.

## Rendu local

```bash
# 1. Récupérer les données depuis S3 (nécessite les identifiants S3)
uv run --project website python website/scripts/build_data.py
uv run --project website python website/scripts/build_data_historiques.py

# 2. Rendre le site
quarto render website

# ou, avec rechargement automatique
quarto preview website
```

Le site rendu atterrit dans `website/_site/`. `data/` et `_site/` sont ignorés par git.

L'option `--limit N` de `build_data.py` ne traite que les N premiers tableaux, pour
itérer rapidement sur la mise en page.

## Données

`data/comparaisons.json`, `data/comparaisons-historiques.json` et `data/apercus/` ne sont
**jamais versionnés** : le `.gitignore` du dépôt interdit de committer données extraites et
annotations. Tout est reconstruit à chaque build, en CI comme en local.

Les aperçus des documents sources (95 JPEG, ~31 Mo) sont fabriqués **une fois** par
`scripts/apercus.py` et déposés sur S3 ; le build ne fait que les télécharger. Les
reconstruire à chaque fois demanderait de relire plusieurs gigaoctets de scans.

Les grilles sont publiées **telles quelles** — raisons sociales, SIREN, montants. Le
corpus provient de comptes sociaux déposés et publiés en open data par l'INPI : rien
n'y est pseudonymisé, le site montre exactement ce que le pipeline a lu. L'identifiant
d'un tableau est le nom de fichier d'origine (`487772899_2`), ce qui permet de
remonter au PDF source.

Le corpus des tableaux historiques suit la même règle : l'identifiant est l'année du
document, éventuellement suffixée (`1967`, `1967_bis`), et relie l'image `crop_{clé}` à
son annotation `ground_truth_{clé}`.

## Déploiement

Le workflow rend le site et le publie via GitHub Pages (`actions/deploy-pages`), sans
branche `gh-pages`. Prérequis côté dépôt :

1. **Settings → Pages → Source : GitHub Actions.**
2. **Settings → Secrets and variables → Actions**, ajouter les identifiants S3 :
   `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_S3_ENDPOINT`, et
   `AWS_SESSION_TOKEN` si les identifiants sont temporaires.

Sans ces secrets, le workflow rend quand même le site : la page de comparaison affiche
alors un message d'indisponibilité au lieu des grilles, les autres pages étant
complètes.

> **Attention** — les identifiants temporaires du SSP Cloud expirent. Un secret
> `AWS_SESSION_TOKEN` périmé fait échouer l'étape de récupération des données, sans
> empêcher la publication du site.
