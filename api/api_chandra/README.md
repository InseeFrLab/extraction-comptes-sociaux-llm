# api_chandra

Service FastAPI d'extraction de tableaux par le VLM **Chandra**, servi par un vllm distant
au protocole OpenAI. Aucun GPU local : le service prépare l'image, appelle le modèle et rend
sa sortie.

```bash
cd api/api_chandra
CHANDRA_API_KEY="$REAL_LLM_API_KEY" \
  uv run uvicorn main_chandra:app --host 0.0.0.0 --port 8003 --app-dir src
```

Ce document porte les **choix de conception et les mesures qui les fondent**. Le code s'y
réfère plutôt que de les répéter.

## `POST /extract`

Exactement un des deux champs de fichier, jamais les deux :

| Champ | Type | Rôle |
|---|---|---|
| `pdf` | fichier | Document PDF, rendu page par page à `CHANDRA_DPI`. |
| `image` | fichier | Image déjà rognée sur le tableau, envoyée telle quelle. |
| `cote_max` | int | Grand côté maximal en pixels, appliqué **après** le rendu. Défaut : `CHANDRA_COTE_MAX` (natif) pour une image, aucun plafond pour un PDF. |
| `max_tokens` | int | Plafond de génération. Défaut `CHANDRA_MAX_TOKENS` ; `0` le retire. |
| `prompt` | str | Consigne libre. Sans elle, seule l'image est envoyée. |
| `prompt_type` | str | `ocr` ou `ocr_layout`, prompt du paquet `chandra-ocr`. Ignoré si `prompt` est fourni. |
| `max_retries` | int | Relances sur détection de répétition. Défaut `CHANDRA_REPEAT_RETRIES`. |

Un champ de formulaire l'emporte toujours sur la variable d'environnement correspondante,
**y compris à 0**, qui est une valeur significative — pas de plafond, pas de relance.

### Réponse

```jsonc
// PDF
{"metadata": {"model": "…", "source": "pdf", "dpi": 200, "cote_max": "sans plafond",
              "prompt": "aucun", "max_tokens": 16384, "max_retries": 0},
 "pages": [{"page": 1, "html": "<table>…</table>", "dpi": 200,
            "jetons": 4210, "repetition": false, "tronque": false, "relances": 0}]}

// image
{"metadata": {"model": "…", "source": "image", "cote_max": 2200, …},
 "pages": [{"page": 1, "html": "<table>…</table>",
            "pixels": [2200, 1287], "pixels_source": [13565, 7935], …}]}
```

Les métadonnées suivent la sortie parce que **deux extractions ne sont comparables que si
l'on sait ce que le modèle a vu et avec quels réglages** : le modèle, la taille réellement
envoyée, le prompt et les plafonds sont donc écrits à côté du HTML, jusque sur S3.

`repetition`, `tronque` et `relances` sont des marques de diagnostic : une page encore
marquée `repetition` est une sortie dégénérée, conservée telle quelle mais à écarter des
comparaisons.

## Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `CHANDRA_BASE_URL` | `https://llm.lab.sspcloud.fr/api` | Serveur vllm (sans `/v1`). |
| `CHANDRA_MODEL` | `chandra-ocr-2` | Nom du modèle servi. |
| `CHANDRA_API_KEY` | `EMPTY` | Convention vllm ; sur llm.lab, passer `REAL_LLM_API_KEY` — l'endpoint est authentifié. |
| `CHANDRA_DPI` | `200` | Résolution PDF→image. **PDF uniquement.** |
| `CHANDRA_COTE_MAX` | `0` (natif) | Grand côté maximal d'une image, en pixels. |
| `CHANDRA_RETRIES` | `5` | Tentatives par page sur erreur de transport. |
| `CHANDRA_RETRY_DELAY` | `2` | Délai initial en secondes, × le numéro de tentative. |
| `CHANDRA_MAX_TOKENS` | `16384` | Plafond de génération ; `0` pour aucun. |
| `CHANDRA_REPEAT_RETRIES` | `0` | Relances sur détection de répétition. |

---

# Choix de conception

## 1. Le HTML du modèle est renvoyé tel quel

**L'API ne structure rien.** Elle l'a fait, en aplatissant le HTML en listes de listes de
chaînes (`pages[].tables`), et cela détruisait tout ce que le modèle savait de la mise en
page : `colspan`, `rowspan` — donc les cellules fusionnées — et les `<br>`, dont la
disparition soudait les mots de deux lignes d'un même libellé (« Prêts
etavancesconsentispar laSociété », **48 cellules** du corpus `reprise/`).

La structuration appartient à l'étape suivante, `scripts/json_to_csv.py`, qui dispose déjà
d'un parseur HTML traitant les fusions et rend le même office à marker et opendataloader.
Garder le HTML brut a deux avantages de plus : rien n'est perdu à l'écriture, et un
changement d'avis sur la mise en forme ne demande pas de relancer le GPU.

Les JSON déposés au format historique (`pages[].tables`) restent lus par `json_to_csv.py`.

## 2. Une image n'a pas de dpi — d'où l'entrée `image`

C'est la raison d'être de ce second point d'entrée. Un scan rogné sur un tableau ne porte
aucune taille physique : le convertir en PDF pour le faire rendre à tant de points par pouce
revient à inventer une page, puis à rééchantillonner deux fois une image qui n'en avait pas
besoin. Les fichiers du corpus historique le confirment — leur tag de résolution est un
remplissage (`(1, 1)` en TIFF, `96 dpi` en PNG quand il existe).

La seule grandeur qui décide de ce que le modèle voit est le **nombre de pixels**, et c'est
donc elle que l'appelant fixe, par `cote_max`.

## 3. Résolution fixe, jamais adaptative

Un mode `auto` a existé côté PDF, qui déduisait le dpi de la résolution réelle du scan page
par page. Mesuré sur 17 documents (31 paires) :

| Effet | Paires |
|---|---:|
| Améliorées | 2 |
| Dégradées | 4 |
| Inchangées | 22 |
| **Solde** | **−149 cellules** |

Côté image, le grand côté a été mesuré sur huit tableaux couvrant tous les modes d'échec, à
2 200, 3 300 et 4 400 px (tableau complet sur la
[page Résultats du site](../../website/pages/resultats-historiques.qmd)) :

- **Monter en résolution n'améliore pas.** 2 200 px est le meilleur ou à égalité sur cinq
  tableaux sur huit ; 4 400 px ne gagne que sur deux, et perd 33 points sur un troisième.
- **La finesse aggrave la sur-segmentation.** Un tableau passe de 1 à 4 grilles entre 2 200
  et 4 400 px : plus l'image est grande, plus chandra y distingue de blocs de mise en page.
- **Le découpage n'est même pas stable** — 5 grilles à 2 200 px, 1 à 3 300, 5 à nouveau à
  4 400, pour un taux identique.

**Il n'y a pas de relation monotone entre résolution et qualité**, et choisir le bon réglage
document par document demanderait un critère dont on ne dispose pas sans annotation. Les
deux corpus sont donc extraits à résolution fixe : **200 dpi** pour les PDF, **2 200 px** de
grand côté pour les images — soit à peu près ce que donne une A4 lue à 200 dpi.
`config/historiques.yaml` porte cette valeur, et `--cote-max` reste offert pour en rejouer
une autre.

`cote_max` s'applique **après** le rendu du PDF : à 200 dpi une A4 fait 1 654 × 2 339 px,
donc un plafond au-delà de 2 339 ne mord pas. Il ne sert pas à choisir la finesse — c'est le
dpi qui la fixe — mais à borner un grand format, et à obtenir des images de mêmes dimensions
que celles d'un autre moteur : c'est la condition pour comparer deux prompts sans comparer
en même temps deux résolutions.

## 4. Plafond de génération : 16 384 jetons

L'API n'en a longtemps pas eu, et une génération dégénérée courait jusqu'au bout du
contexte. Mesuré sur le corpus historique :

| | Jetons |
|---|---:|
| Plus gros tableau légitime | 13 644 |
| Génération dégénérée | ~62 000 (un demi-mégaoctet de HTML stocké) |
| **Plafond retenu** | **16 384** |

16 384 laisse 20 % de marge au-dessus du besoin réel et coupe les boucles au quart de ce
qu'elles coûtaient. `max_tokens=0` retire le plafond, ce qui reproduit le comportement
d'avant la mesure — c'est ainsi que le moteur de référence `chandra` du corpus historique
est figé, pour que ses chiffres publiés restent reproductibles.

## 5. Répétition : on détecte et on marque, on ne relance pas

`CHANDRA_REPEAT_RETRIES` vaut **0** par défaut. Mesuré sur les deux seules boucles du corpus
historique (`crop_1902`, 16 384 jetons ; `crop_1989`, 4 882) :

- deux relances, à température 0,2 puis 0,4, **ne cassent ni l'une ni l'autre**, et coûtent
  trois appels au lieu d'un — 329 s contre 110 s sur `crop_1902` ;
- la boucle est un attracteur trop fort pour un desserrage timide : il faut **six** relances,
  jusqu'à 0,8, pour en sortir ;
- le seul tableau ainsi sauvé lors des mesures avait *perdu* en précision (−0,013 de
  `numeric_recovery`, seul tableau déplacé sur 38).

Relever ce réglage n'a donc de sens qu'à 5 ou 6, en sachant que **ça achète une sortie
propre, pas une meilleure**. La marque `repetition` suffit à écarter des mesures une sortie
dégénérée, ce qui est le besoin réel.

Quand des relances sont demandées, la politique est celle du paquet `chandra-ocr` :
température desserrée de 0,2 par tentative, plafonnée à 0,8, et `top_p` à 0,95.

## 6. Échantillonnage déterministe au premier appel

Le premier appel est à **température 0**, sans `top_p` : une extraction de tableau doit être
reproductible d'un lancement à l'autre. On ne desserre que pour sortir d'une boucle, jamais
par défaut.

Le mode « thinking » est désactivé (`chat_template_kwargs.enable_thinking`). Mesuré sans
effet sur `chandra-ocr-2` — sortie identique au caractère près, aucun contenu de raisonnement
rendu — le réglage est conservé pour rester explicite si le modèle servi en tient compte un
jour.

## 7. Les prompts ne sont pas recopiés

Par défaut, **aucune consigne n'est envoyée** : seule l'image part au modèle, ce pour quoi
il a été réglé. `prompt_type` permet d'employer un prompt **du paquet `chandra-ocr`** en le
désignant par son nom, jamais par son texte : le paquet reste seule source de vérité, ses
consignes suivent le modèle, et une copie dans nos scripts dériverait en silence. C'est aussi
ce qui permet à `config/historiques.yaml` de déclarer une condition d'expérience par un nom.

| `prompt_type` | Contenu |
|---|---|
| `ocr_layout` | Consignes de formatage, bbox et 19 étiquettes de bloc. |
| `ocr` | La même consigne, **privée** des bbox et des étiquettes. |

Les opposer sépare le coût de la tâche de mise en page de celui des consignes de formatage,
que les deux partagent — c'est l'objet des conditions `chandra_prompt_ocr` et
`chandra_prompt_layout`.

`prompt` accepte un texte libre, pour explorer hors de ces deux-là. Quatre prompts maison ont
été mesurés sur les deux corpus et **tous font moins bien que `ocr_layout`**, notamment parce
qu'une injonction de complétude (« never stop early ») fait boucler la génération jusqu'au
plafond de jetons.

Le nom est résolu à l'entrée de `/extract`, avant tout appel : une clé inconnue doit ressortir
en `400`, pas en `KeyError` au milieu d'une extraction.

## 8. Deux emprunts au paquet `chandra-ocr`, et rien d'autre

Le client d'appel du paquet a été comparé à notre appel direct au serveur vllm, puis retiré
de la chaîne comme du site. Ses sorties dorment encore sur S3 sous les préfixes
`*_chandra_client*` ; plus rien ne les produit ni ne les lit.

Il reste dépendance pour deux fonctions seulement :

| Emprunt | Pourquoi |
|---|---|
| `chandra.prompts.PROMPT_MAPPING` | Ne pas recopier des consignes qui dériveraient (cf. § 7). |
| `chandra.model.util.detect_repeat_token` | Le critère de répétition pour lequel le modèle a été réglé, plutôt qu'un seuil maison à justifier. |

Le test de répétition est refait en ignorant les 50 derniers caractères : le double test
rattrape les boucles suivies d'une fin de sortie propre.

## 9. Préparation de l'image

- **Mode couleur.** Un scan bilevel ou en niveaux de gris reste en `L` : le passer en RGB
  triplerait le PNG sans rien montrer de plus. Le reste — CMJN, palette, 16 bits, canal alpha
  — n'est pas écrivable en PNG tel quel, ou ne porte aucune information sur un scan.
- **Réduction en LANCZOS.** Sur du bilevel, cela produit des niveaux de gris : c'est voulu,
  un texte réduit au plus proche voisin perd ses jambages et devient illisible.
- **Jamais d'agrandissement.** Ajouter des pixels interpolés ne montre rien de plus au
  modèle : une image déjà plus petite que `cote_max` part telle quelle.
- **Garde anti-bombe de Pillow levée.** Les scans d'archives dépassent ses 89 Mpx — le corpus
  historique monte à 889 Mpx pour un seul fichier, et part de 37. Ces images sont déposées
  par nos soins, la garde n'a rien à protéger ici.

## 10. Deux mécanismes de reprise, à ne pas confondre

| | Déclencheur | Réglage | Défaut |
|---|---|---|---|
| Tentatives de transport | Erreur réseau ou serveur | `CHANDRA_RETRIES`, `CHANDRA_RETRY_DELAY` | 5, délai 2 s × la tentative |
| Relances de génération | Sortie détectée en boucle | `CHANDRA_REPEAT_RETRIES` / `max_retries` | 0 |

Les erreurs de transport sont retentées dans tous les cas, indépendamment des relances sur
répétition. Le client n'a **aucun timeout** : une page dense peut demander plusieurs minutes
au VLM, et l'appelant est déjà maître de son propre délai.

---

## Ce que le déploiement du modèle doit fournir

Le service parle à un vllm déjà en place. Côté serveur, le modèle doit être servi avec son
`chat_template.jinja` et ses défauts d'échantillonnage ; côté client, seuls `temperature`,
`top_p` et `max_tokens` sont posés, et uniquement lorsqu'ils ont une valeur à dire.

## Voir aussi

- [`scripts/extraction_historiques.py`](../../scripts/extraction_historiques.py) — le pilote
  qui soumet le corpus des tableaux historiques à ce service.
- [`config/historiques.yaml`](../../config/historiques.yaml) — les conditions d'expérience
  comparées, et les paramètres transmis ici.
- [`website/pages/resultats-historiques.qmd`](../../website/pages/resultats-historiques.qmd)
  — les mesures publiées, dont l'effet de la résolution en détail.
