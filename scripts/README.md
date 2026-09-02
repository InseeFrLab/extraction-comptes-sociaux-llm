# scripts/ — le pipeline

Orchestration et évaluation, lancées en local via `uv` depuis ce dossier. Le pipeline va du
PDF (ou du scan) aux métriques, en quatre étapes que **deux corpus traversent à l'identique**.

Tout ce qui est *configurable* — chemins S3, moteurs comparés, paramètres d'API, seuils —
est déclaré dans [`config/*.yaml`](../config/) et lu par `config.py`. Ce document porte ce
qui est *codé* : les **choix de conception et les mesures qui les fondent**. Le code s'y
réfère plutôt que de les répéter.

| Fichier | Étape | Rôle |
|---|---|---|
| `config.py` | — | Lecture de `config/*.yaml`, chemins S3 résolus. |
| `comparaison_pdf_csv.py` | 0 | Apparie PDF et annotations XLSX par SIREN → `correspondances.parquet`. |
| `extraction_pdf_via_api.py` | 1 | PDF S3 → API du moteur → JSON/HTML brut sur S3. |
| `extraction_historiques.py` | 1 | Le même pilote, sur les images du corpus historique. |
| `corpus_historiques.py` | — | Conventions de nommage de ce corpus (image ↔ annotation). |
| `json_to_csv.py` | 2 | Sorties brutes des moteurs → un CSV par tableau, sur S3. CLI et point d'import ; le code est dans `conversion/`. |
| `conversion/` | 2 | Le code de l'étape 2, un module par étape de la chaîne (cf. ci-dessous). |
| `evaluation.py` | 3 | CSV prédits vs annotations → métriques en parquet, sur S3. |

Les scripts du site vivent dans [`website/scripts/`](../website/) et ont leur propre README.

## Enchaînement

```bash
cd scripts

# 0. appariement PDF ↔ annotations (corpus des comptes sociaux)
uv run comparaison_pdf_csv.py

# 1. extraction — l'API du moteur doit tourner (cf. « Démarrer les APIs » ci-dessous)
uv run extraction_pdf_via_api.py --from-parquet          # --api marker|opendataloader|chandra
uv run extraction_historiques.py --all                   # --moteur chandra|chandra_borne|…

# 2. conversion
uv run json_to_csv.py --list                             # les méthodes configurées
uv run json_to_csv.py --method all [--overwrite]

# 3. mesure
uv run evaluation.py --corpus all [--threshold 0.5 --cell-delta 0]
```

`--overwrite` est **nécessaire dès que le code de conversion change** : sans lui les fichiers
déjà convertis sont ignorés et le dossier de sortie mélange deux générations.

## Démarrer les APIs

Chaque service a son propre venv et son propre port ; les scripts les joignent en HTTP.

```bash
# api_marker (port 8001) — OCR + structuration, GPU. Nécessite marker_proxy.
cd api/marker_proxy && uv run python -m uvicorn proxy:app --host 0.0.0.0 --port 1324 --app-dir src
cd api/api_marker  && uv run python -m uvicorn main_marker:app --host 0.0.0.0 --port 8001 --app-dir src

# api_opendataloader (port 8002) — Java 11+ requis
cd api/api_opendataloader && uvicorn main_opendataloader:app --host 0.0.0.0 --port 8002 --app-dir src

# api_chandra (port 8003) — relais vers le VLM distant, aucun GPU local
cd api/api_chandra && CHANDRA_API_KEY="$REAL_LLM_API_KEY" \
  uv run uvicorn main_chandra:app --host 0.0.0.0 --port 8003 --app-dir src
```

| Variable | Défaut | Pour |
|---|---|---|
| `API_MARKER_URL` | `http://localhost:8001` | `extraction_pdf_via_api.py` |
| `API_OPENDATALOADER_URL` | `http://localhost:8002` | idem |
| `API_CHANDRA_URL` | `http://localhost:8003` | idem, et `extraction_historiques.py` |
| `REAL_LLM_BASE_URL`, `REAL_LLM_API_KEY` | — | `marker_proxy`, `api_chandra` |
| `PROXY_URL` | `http://localhost:1324/v1` | `api_marker` → `marker_proxy` |
| `LANGFUSE_*` | — | tracing, optionnel |
| `AWS_*` | cf. [README racine](../README.md#env--accès-et-secrets) | accès S3 |

Ces URLs surchargent celles déclarées dans `config/*.yaml`. Les réglages propres à chaque
service sont documentés chez lui — voir [`api/api_chandra/README.md`](../api/api_chandra/README.md)
pour le VLM.

---

# Choix de conception

## Étape 0-1 — appariement et extraction

**Un SIREN, plusieurs tableaux, un seul PDF.** Les annotations d'un même document sont
suffixées `_1`, `_2`… ; `correspondances.parquet` porte donc une ligne par SIREN, avec un
chemin PDF et une liste de chemins XLSX. C'est cette liste, triée, qui définit le rang de
chaque annotation, et donc son appariement à une prédiction.

**L'extraction ne rejoue pas ce qui existe.** Une sortie déjà présente sur S3 est ignorée, et
signalée comme telle. C'est ce qui rend un lot interrompu reprenable sans tout relancer — une
extraction marker prend 10 à 30 minutes par document, d'où l'absence de timeout côté client.

**Le corpus historique n'a pas de parquet de correspondances** : l'appariement s'y fait par le
nom de fichier, `crop_1970_bis.tiff` ↔ `ground_truth_1970_bis.html`, une clé commune obtenue
en retirant le préfixe. C'est tout ce que porte `corpus_historiques.py`.

**Ses images ne sont pas converties en PDF.** Une image n'a pas de taille physique donc pas de
dpi ; le réglage est un nombre de pixels (`cote_max`). Les fichiers vont de 37 à 889 mégapixels
et leur tag de résolution est un remplissage. Le détail des mesures est dans le
[README d'`api_chandra`](../api/api_chandra/README.md).

## Étape 2 — conversion (`json_to_csv.py`)

### Un module par étape de la chaîne

`json_to_csv.py` ne porte que la docstring d'usage et les réexports : le code vit dans le
package `conversion/`, découpé dans l'ordre où la sortie d'un moteur le traverse. Chaque
module ne dépend que des précédents, ce qui donne la chaîne complète en un coup d'œil et
permet de tester chaque étape sans les suivantes.

| Module | Rôle |
|---|---|
| `conversion/grid.py` | Mise en forme des matrices de chaînes : largeur canonique, sous-lignes d'en-tête, lignes empilées, rectangularisation. Ne connaît ni HTML, ni moteur, ni S3. |
| `conversion/html_tables.py` | Parseur HTML tolérant : un fragment → une matrice par `<table>`, fusions comprises. |
| `conversion/chandra.py` | Blocs de mise en page d'une page chandra, et recollage des tableaux qu'un intertitre a coupés. |
| `conversion/extractors.py` | Un lecteur par format de sortie (marker, chandra, opendataloader), et le registre `EXTRACTORS` que la config désigne. |
| `conversion/pipeline.py` | Lecture S3 → conversion → un CSV par tableau, et compte rendu du passage. |
| `conversion/cli.py` | Arguments de la commande. |

Les autres scripts (`evaluation.py`) et les tests importent depuis `json_to_csv` : c'est
lui qui reste la surface publique de l'étape, quel que soit le module qui porte le code.

### La grille est rendue rectangulaire ici, et nulle part ailleurs

Chaque extracteur rend une grille rectangulaire. C'est ici, et seulement ici, que l'on sait
d'où viennent les cellules manquantes : `_load_csv` côté évaluation ne voit qu'un CSV et ne
peut que compléter à droite. Laisser la grille irrégulière reviendrait à lui déléguer une
décision de structure qu'il n'a pas les moyens de prendre.

### Les fusions : la valeur à la première cellule, vide ailleurs

`colspan` est développé en cellules vides à droite, `rowspan` reporté sur les lignes
suivantes. Une cellule fusionnée ne porte sa valeur qu'à sa position d'origine ; les positions
de continuation reçoivent une chaîne vide, **dans les deux directions**. C'est la convention
des annotations de référence, où une fusion Excel n'écrit la valeur que dans sa première
cellule.

Le choix n'est pas cosmétique. Mesuré sur les 69 paires du corpus `reprise/` :

| Traitement du `rowspan` | Récupération numérique |
|---|---:|
| Aucun report | 42,0 % |
| **Report en cellules vides** | **48,4 %** |
| Report en répétant la valeur | 29,7 % |

Sans report, chaque ligne suivant une fusion verticale perd une cellule et tout ce qui la suit
glisse d'un cran à gauche — décalage qui se propage à l'ensemble du tableau. **77 % des
documents marker du corpus contiennent au moins un `rowspan`.** Répéter la valeur, à l'inverse,
rend les lignes indiscernables à l'appariement des en-têtes.

### Le parseur est tolérant, comme un navigateur

Une balise ouvrante referme ce qui doit l'être : un `<tr>` ferme la ligne ouverte, un `<td>` la
cellule ouverte, et une cellule hors ligne en ouvre une. Les annotations saisies à la main en
ont besoin — **dix des quarante** du corpus historique portent une balise en trop ou en moins.

Deux conséquences réglées explicitement :

- un `<tr>` en double (« `<tr> <tr> <td…` », dix annotations) produirait une ligne vide qui
  consommerait une ligne de `rowspan` à un rang qui n'existe pas ; une ligne implicite sans
  contenu est donc **abandonnée sans décompter les fusions** ;
- un `</table>` manquant — annotation tronquée, sortie de VLM coupée par sa limite de jetons —
  est republié en fin de flux, sans quoi tout ce qui a été lu serait perdu et le fichier
  disparaîtrait de la mesure au lieu d'y figurer pour ce qu'il vaut. Mesuré sur les 96 JSON du
  corpus `reprise/`, aucun n'a de `<table>` non fermé : la reprise n'y change rien, elle sert
  aux deux autres cas.

### `<br>` : une espace par défaut, une coupure de ligne par exception

L'espace est le comportement d'un libellé replié en fin de ligne ; sans elle, les mots des deux
lignes se soudent (« Prêts etavancesconsentispar laSociété »).

Mais certains tableaux composent un enregistrement sur deux lignes physiques sans filet entre
elles, l'en-tête l'annonçant sur deux niveaux — « Dénomination / Siège Social », « Capital /
Capitaux Propres » (`_0334_394331946_TAB`). Les moteurs rendent alors une seule `<tr>` dont
chaque cellule porte ses deux valeurs séparées par un `<br>` : lecture fidèle de la page, mais
l'annotation garde une ligne par ligne physique.

La signature qui déclenche la coupure doit rester **étroite**, un libellé replié portant lui
aussi un `<br>`. Trois conditions cumulées, qu'une cellule isolée ne peut pas remplir :
plusieurs cellules coupées, toutes du même nombre de parties, et au moins deux d'entre elles
empilant deux nombres. Sur le corpus `reprise/`, cela concerne **9 lignes sur 1 209** chez
chandra et **10 sur 1 234** chez marker.

### La largeur du tableau se mesure hors lignes-labels

Un intertitre de section — seule cellule non vide de sa ligne — ne doit pas fixer la largeur :
certains moteurs le placent en fin de ligne, ce qui ajouterait autant de colonnes fantômes à
tout le tableau. Il est ensuite ramené en première colonne, sa colonne d'origine ne portant
aucune information.

### Les sous-lignes d'en-tête sont replacées sous le libellé qu'elles détaillent

Sans traitement, une sous-ligne d'en-tête est complétée à droite et atterrit en colonnes
`0..k-1`, donc sous les mauvais en-têtes — ce qui fausse l'appariement de **toutes** les
colonnes du tableau. Deux configurations, selon que le moteur a émis ou non les cellules de
continuation du libellé couvrant :

| Ligne parente | Signal utilisé |
|---|---|
| Courte de `k - 1` | Le libellé couvrant, reconnu lexicalement puis, à défaut, comme seul candidat de la ligne. |
| Déjà à la largeur | Le trou de `k - 1` cellules vides, qui désigne la position sans interpréter aucun libellé. |

Le signal lexical est le vocabulaire du tableau réglementaire des filiales et participations
(« valeurs comptables » / « valeur d'inventaire », scindé en « Brute » / « Nette ») : c'est le
**seul en-tête à sous-colonnes de ce corpus**, et c'est sa cellule de continuation que les
moteurs omettent. La première colonne est exclue des candidats : elle porte les raisons
sociales.

Quand la position ne peut pas être tranchée, le repli à droite s'applique — **mieux vaut le
comportement connu qu'un placement arbitraire**.

### Chandra : les blocs d'un même tableau sont recollés

Chandra rend une page comme une suite de `<div data-label=…>` jamais imbriqués (vérifié : 55
pages, profondeur de `div` maximale de 1), et **coupe une région `Table` dès qu'un autre bloc
l'interrompt**. Un tableau que traverse un intertitre ressort donc en plusieurs `<table>`.
Mesuré sur les 88 tableaux du corpus `reprise/` : **73 blocs complets, 14 incomplets**,
concentrés sur les seuls fichiers sur-découpés.

Le modèle ne recolle rien et n'annonce aucune continuation, mais son balisage dit lequel de ces
blocs est un tableau entier. Deux lectures, à largeur de colonnes identique — condition
nécessaire, un tableau coupé en largeur ne se recolle jamais par lignes :

- le bloc n'a pas d'en-tête de colonnes propre : il reprend en pleine matière ;
- le tableau en cours n'a pas encore de ligne de données : c'est un en-tête orphelin.

Un bloc qui réimprime un vrai en-tête n'entre dans aucun des deux cas et reste un tableau
distinct : sur une même page, un en-tête répété désigne deux tableaux de même forme
(`_1465_652027384_TAB`), pas une suite. Un `th` unique en `colspan` pleine largeur ne compte
pas comme en-tête — chandra met les intertitres dans le `thead`, et les prendre pour un en-tête
ferait passer une suite de tableau pour un tableau autonome.

Le recollage est **borné à la page** : chandra est appelé page par page et l'annotation suit
cette granularité.

### Chandra : le texte des intertitres est rendu au tableau

Il est hors de toute balise `<table>` et serait perdu. Deux sorts, selon la géométrie et le
balisage :

- **collé en tête de la première cellule** quand le bloc chevauche le haut du tableau. Chandra
  sort parfois un libellé de ligne hors du tableau : sur `411373525`, chaque raison sociale
  part dans un bloc `Section-Header` et seule l'adresse reste dans la ligne. Le bloc commence
  alors au-dessus du tableau et finit dedans, là où un titre de tableau s'arrête avant. Le
  recouvrement horizontal est exigé en plus — sur une page en paysage, un titre latéral couvre
  toute la hauteur du tableau sans rien avoir à y faire ;
- **posé en ligne-label** sinon, mais seulement devant un bloc sans en-tête de colonnes propre.
  Devant un bloc qui a le sien, c'est le titre du tableau et non une de ses lignes :
  l'annotation ne le porte pas non plus.

Un bloc sans tableau qui n'est pas un intertitre **rompt le voisinage** : un intertitre séparé
de son tableau par un paragraphe ne lui appartient plus.

### Chandra : les titres courants sont écartés

Le modèle n'étiquette pas ces textes de la même façon partout — sur `411373525`, la page 2 les
donne en `Page-Header` et la page 1 en `Section-Header`, à texte identique. Un texte vu au
moins une fois en `Page-Header`/`Page-Footer` **dans le document** est donc écarté partout : le
verser en ligne-label ajouterait des lignes que l'annotation n'a pas. C'est pourquoi les blocs
de toutes les pages sont lus avant qu'aucune ne soit recollée.

### Deux formats de sortie chandra, tous deux lus

| Format | Contenu | Traitement |
|---|---|---|
| Courant | HTML brut du VLM, page par page | Fusions, `<br>` et balisage de blocs disponibles : recollage possible. |
| Historique | Matrices de chaînes déjà aplaties par l'API | Tout est perdu ; seules les sous-lignes d'en-tête peuvent être replacées au mieux. |

Les JSON déjà déposés sur S3 sont au format historique : ils restent lus. Pourquoi l'API a
cessé d'aplatir : voir le [README d'`api_chandra`](../api/api_chandra/README.md).

### Marker : un `TableGroup` cède la place à ses `Table`

Son `html` ne contient en principe que des pointeurs `<content-ref>`, mais rien dans le format
ne le garantit, et le retenir en plus de ses enfants dupliquerait le tableau. Un fragment sans
balise `<table>` est enveloppé dans une ligne artificielle ; s'il ne porte aucune cellule — le
cas des blocs vides et des `<content-ref>` — il ne produit aucune grille.

### Un bloc chandra sans ligne de données n'est pas un tableau

Ses pages produisent des blocs qui n'en sont pas ; un bloc dont aucune ligne ne porte deux
cellules est écarté. C'est le seul rejet propre à un moteur — la mise en forme, elle, est
commune à tous.

## Étape 3 — mesure (`evaluation.py`)

### Une mesure, plusieurs corpus

Le calcul est **rigoureusement le même** : mêmes fonctions, mêmes seuils. Ne changent que la
référence (XLSX ou HTML) et la façon de l'apparier à une prédiction (par SIREN, rang à rang, ou
par nom de fichier). La configuration du corpus désigne son mode d'appariement, `TRAITEMENTS`
en porte le code : ajouter un corpus qui s'apparie comme un corpus existant ne demande donc
aucune ligne de Python.

### Les métriques sont de type rappel

Une ligne par couple fichier × méthode. Le dénominateur est toujours l'annotation : ce qui est
mesuré, c'est ce que le moteur a **retrouvé**, jamais ce qu'il a inventé en plus.

| Métrique | Définition |
|---|---|
| `col_recovery` | Colonnes de l'annotation retrouvées. |
| `row_recovery` | Lignes de l'annotation retrouvées. |
| `numeric_recovery` | Cellules numériques (hors en-têtes) bien récupérées. |
| `total_extraction` | 1 si structure complète **et** toutes les cellules numériques récupérées. |
| `table_count_accuracy` | Documents dont le nombre de tableaux détectés est le bon. |

`numeric_recovery` vaut `NaN` — et non 0 — pour un tableau sans cellule numérique : une
moyenne ne doit pas être tirée vers le bas par un tableau qui n'avait rien à récupérer.

### La normalisation absorbe l'écriture, jamais les chiffres

Deux valeurs aux chiffres différents restent différentes. Ce qui est absorbé :

| Écart | Exemple |
|---|---|
| Espaces séparateurs de milliers, insécables compris | `25 000` → `25000` |
| Séparateur décimal | `2,08` = `2.08` |
| Signe négatif détaché | `- 30 000` → `-30000` |
| Parenthèses comptables | `(1 976)` → `-1976` |
| Pourcentages, convertis en décimales | `100 %` = `100,00%` = `1` |
| Variantes de tiret | `—` → `-`, `−30` → `-30` |

Deux garde-fous :

- **signe et parenthèses ne sont interprétés que devant un nombre**, sans quoi
  `(en milliers d'euros)` se verrait attribuer un signe négatif ;
- **le cas général n'emprunte pas `float`.** Sur un montant de plus de dix chiffres
  significatifs — `10 640 226 396` existe dans le corpus — un formatage en `.10g` arrondirait,
  et deux montants voisins se confondraient.

### Les tirets sont unifiés

Toutes les variantes jouent le même rôle dans ces tableaux — marque d'absence, signe négatif,
séparateur dans un libellé — et ne doivent donc pas distinguer deux valeurs. Les annotations
écrivent `-` là où les moteurs rendent `—`. Sans cette équivalence, **14 cellules** de
`TAB_552096281_2` étaient comptées comme du texte à la place d'un nombre, et « A - FILIALES
DETENUES » ne s'appariait pas à « A – FILIALES DETENUES » : similarité 0,25, pour un seul
demi-cadratin d'écart.

### Les libellés sont ramenés à une graphie canonique

Casse, accents, espaces et tirets ne distinguent pas deux libellés : un moteur qui compose ses
en-têtes en capitales décrit les mêmes colonnes qu'une annotation en bas de casse. Comparées
telles quelles par distance de Levenshtein, « Capital » et « CAPITAL » tombent à **0,14** de
similarité — sous le seuil, donc non appariées. Sur `TAB_300221017_1`, où chandra rend l'en-tête
entier en capitales, les **11 colonnes** échouaient à s'apparier et **350 cellules** de données
étaient comptées perdues, pour une extraction pourtant juste.

### L'appariement des en-têtes : Levenshtein, puis Gale-Shapley

Un texte représentatif est formé par colonne (et par ligne) en concaténant les cellules de
l'en-tête correspondant, puis un **matching stable** apparie les deux listes. Seules les paires
dont la similarité atteint le seuil sont retenues — `seuil_similarite`, 0,5 dans les deux
corpus. Sous 0,5, des libellés sans rapport s'apparient ; au-dessus, une abréviation légitime
ne s'apparie plus.

Le matching stable, plutôt qu'un simple « meilleur score » : il interdit qu'une colonne rafle
l'appariement d'une autre qui la préférait, ce qui arrive dès que deux colonnes portent des
libellés voisins — le cas des tableaux de filiales, où plusieurs colonnes déclinent le même mot.

### La zone de données est délimitée par deux heuristiques

La hauteur d'en-tête absorbe les lignes numériques initiales, puis les lignes majoritairement
non numériques. Deux garde-fous, tous deux nés de cas réels :

- si **aucune** ligne textuelle n'est absorbée, il n'y a pas d'en-tête de colonnes — le cas des
  tableaux de filiales qui commencent directement par les données ;
- si la phase absorbe **toutes** les lignes jusqu'à la fin, typique des tableaux text-heavy où
  chaque ligne dépasse le seuil sans être un en-tête, on retombe sur les seules lignes 100 %
  non numériques.

Un intertitre de section — « 2. Participations (détenues à moins de 50 %) », seule cellule non
vide de sa ligne — n'est pas un en-tête de colonnes et est écarté avant la détection.

Le test « cette cellule est-elle une donnée ? » est **volontairement permissif** : il décide du
dénominateur de `numeric_recovery`, pas de l'égalité de deux valeurs. Sont acceptés, en plus des
nombres purs et de la cellule vide, les parenthèses comptables, les unités en suffixe
(`15,24 €`, `344 369 NOK`, `100,00%`) et les marques d'absence (`-`, `NC`, `ND`, `N/A`). Toutes
partent ensuite par le **même** chemin de comparaison : les cellules à unité ou à parenthèses
passaient autrefois en comparaison de chaîne brute, ce qui comptait fausses `100%` contre
`100,00%`.

### Les coupures de page sont recollées, pour marker seulement

Un tableau à cheval sur deux pages est annoté une page par fichier. Chandra est appelé page par
page et rend la même segmentation. Marker reçoit le PDF entier et son `TableGroup` enjambe
parfois la coupure — **sur 3 des 6 documents multi-pages du corpus**. Face à un tableau marker
d'un seul tenant, l'annotation `_2` n'est appariée à rien et ses cellules quittent le
dénominateur en silence.

Les annotations sont donc regroupées pour la seule référence de marker (drapeau
`fusion_coupures_page` dans la config), et **seulement quand le moteur a effectivement produit
moins de tableaux**. Deux tableaux réellement distincts ne sont jamais fusionnés pour faire
tomber le compte : la sous-détection du moteur doit rester visible dans la mesure.

Deux formes, et deux seulement, distinguent une coupure d'une succession de tableaux distincts,
à largeur identique dans les deux cas : la suite n'a pas d'en-tête de colonnes propre, ou elle
réimprime exactement le même en-tête. Un tableau coupé **en largeur** n'entre dans aucun des
deux cas — les largeurs diffèrent, et concaténer par lignes serait faux.

Le corpus historique n'est pas concerné : chaque image porte un tableau entier, déjà rogné.

### Le corpus historique : sur-découpage signalé, rang 1 seul comparé

Chaque image porte un tableau entier : un moteur qui en rend plusieurs a sur-découpé. Seul le
rang 1 est comparé, et **les rangs suivants sont signalés, non silencieusement écartés** — le
compte de tableaux prédits porte cette information dans la mesure. De même, une annotation
portant plusieurs `<table>` déclenche un avertissement : la mesure ne doit pas trancher à la
place de l'annotateur laquelle compte.

### `marker_last_work` s'apparie autrement

Cette campagne antérieure, conservée pour comparaison, passe par le parquet de correspondances
plutôt que par le rang des fichiers présents. Son mode d'appariement est déclaré dans la config
(`appariement: correspondances`), pas testé sur le nom du moteur.

---

## Tests

Les tests vivent dans [`tests/`](../tests/) à la racine et s'exécutent depuis ce venv :

```bash
uv run --project scripts pytest          # depuis la racine du dépôt
uv run --project scripts pytest -k eval
```

Ils ciblent la **logique pure** — normalisation des nombres, détection des en-têtes,
appariement, parsing HTML/JSON — là où se jouent les métriques. **Aucun ne touche S3, le GPU ou
le LLM.** `tests/test_config.py` vérifie en plus que la configuration des corpus dit ce que le
code attend d'elle : extracteur connu, appariement implémenté, clés de méthode uniques.

Un correctif sur les métriques ou le parsing s'accompagne d'un test qui échoue sans lui.

## Voir aussi

- [`config/`](../config/) — ce que chaque corpus déclare (chemins, moteurs, seuils).
- [`api/api_chandra/README.md`](../api/api_chandra/README.md) — les choix du service VLM.
- [`website/`](../website/) — le site de diagnostic, et les scripts qui le nourrissent.
