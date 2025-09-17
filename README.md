### WIP

1. Vue d'ensemble

Un projet expérimental visant à automatiser l'extraction de tableaux à partir de documents PDF, notamment les tableaux "Filliale et participation" des comptes sociaux d'entreprises.

Le projet met en œuvre une architecture de microservices conteneurisés et orchestrés par Kubernetes. Il combine des appels à des API externes (INPI), le traitement de PDF, et l'utilisation de modèles de langage (LLM) pour l'analyse et l'extraction de données.

2. Architecture

Le système est composé de trois services principaux qui communiquent entre eux :

API Centrale (api_centrale): C'est le point d'entrée principal de l'application. Elle orchestre le workflow :

Récupère les documents PDF depuis l'API de l'INPI à partir d'un SIREN et d'une année.

Fait appel à un service externe pour sélectionner la page la plus pertinente du document.

Extrait cette page unique.

Envoie la page extraite à l'API Marker pour l'analyse.

(Optionnel) Sauvegarde les résultats au format JSON dans un bucket S3.

API Marker (api_marker): Ce service encapsule la bibliothèque marker-pdf. Son rôle est de traiter un fichier PDF d'une seule page pour en extraire le contenu sous forme structurée.

Il reçoit un PDF, le convertit en image pour analyse.

Il utilise marker-pdf configuré pour forcer l'OCR et faire appel à un LLM via un proxy.

Il retourne le contenu du PDF au format JSON.

Proxy Marker (marker_proxy): Un proxy intelligent placé devant l'API du LLM.

Il intercepte les requêtes de l'API Marker vers le LLM.

Il ajoute une couche d'observabilité en traçant les requêtes et les réponses avec Langfuse.

Il transfère ensuite la requête à l'API du LLM réel et retourne la réponse.

3. Description des Composants
api_centrale/

Rôle : Orchestrateur du processus d'extraction.

Framework : FastAPI.

Dépendances notables : fastapi, requests, PyMuPDF, s3fs.

api_marker/

Rôle : Wrapper spécialisé pour marker-pdf.

Framework : FastAPI.

Dépendances notables : marker-pdf, fastapi, PyMuPDF, Pillow.

marker_proxy/

Rôle : Proxy d'observabilité pour les appels LLM.

Framework : FastAPI.

Dépendances notables : fastapi, httpx, langfuse.

kubernetes/

Ce répertoire contient tous les manifestes nécessaires pour déployer l'infrastructure sur un cluster Kubernetes (Deployment, Service, Ingress).

legacy/

Contient des scripts et des expérimentations des phases antérieures du projet, utiles pour comprendre l'historique et pour des tests locaux.

.github/workflows/

Définit le pipeline de CI/CD avec GitHub Actions pour construire et publier automatiquement les images Docker des trois services sur Docker Hub.

4. Déploiement
Prérequis

Un accès à un cluster Kubernetes avec kubectl configuré.

Un Ingress Controller (ex: Nginx) installé sur le cluster.

Un fichier .env contenant toutes les variables d'environnement nécessaires.

Variables d'environnement

Créez un fichier .env à la racine du projet en vous basant sur l'exemple suivant. Remplissez les valeurs vides avec vos propres informations d'identification.

code
Bash
download
content_copy
expand_less

# Configuration S3 (pour api_centrale)
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_SESSION_TOKEN=
AWS_REGION=us-east-1
AWS_S3_BUCKET=
AWS_S3_ENDPOINT=minio.lab.sspcloud.fr

# Accès INPI (pour api_centrale)
INPI_USERNAME=
INPI_PASSWORD=

# Endpoints des services
LEGACY_SELECTOR_URL=http://extraction-cs.lab.sspcloud.fr/select_page
MARKER_API_URL=http://extraction-tableau-marker.lab.sspcloud.fr/ # Note: URL interne au cluster
PROXY_URL=http://marker-proxy/v1/ # Note: URL interne au cluster

# Configuration du LLM (pour marker_proxy)
REAL_LLM_BASE_URL=https://llm.lab.sspcloud.fr/api/chat/completions
REAL_LLM_API_KEY=

# Configuration Langfuse (pour marker_proxy)
LANGFUSE_HOST=https://langfuse.lab.sspcloud.fr
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
Étapes de déploiement

Créer le Secret Kubernetes
Assurez-vous que votre fichier .env est complet, puis exécutez la commande suivante pour créer ou mettre à jour la configuration dans le cluster :

code
Sh
download
content_copy
expand_less
IGNORE_WHEN_COPYING_START
IGNORE_WHEN_COPYING_END
kubectl delete secret app-env -n projet-extraction-tableaux --ignore-not-found
kubectl create secret generic app-env \
  --from-env-file=./.env \
  --namespace=projet-extraction-tableaux

Appliquer les manifestes Kubernetes
Cette commande déploie ou met à jour tous les composants de l'application (déploiements, services, et routes d'accès) :

code
Sh
download
content_copy
expand_less
IGNORE_WHEN_COPYING_START
IGNORE_WHEN_COPYING_END
kubectl apply -f kubernetes/ --namespace=projet-extraction-tableaux
5. Utilisation

Une fois l'application déployée, vous pouvez interroger le point d'entrée principal de api-centrale.

Exemple avec curl :

code
Sh
download
content_copy
expand_less
IGNORE_WHEN_COPYING_START
IGNORE_WHEN_COPYING_END
curl -X GET "http://extraction-tableau-centrale.lab.sspcloud.fr/extract/552032534?year=2022"

siren (552032534) : Le SIREN de l'entreprise.

year (2022) : L'année des comptes sociaux à extraire.

La réponse attendue est un objet JSON contenant les informations de la requête et le résultat de l'extraction.

code
JSON
download
content_copy
expand_less
IGNORE_WHEN_COPYING_START
IGNORE_WHEN_COPYING_END
{
  "siren": "552032534",
  "year": "2022",
  "page": 15,
  "marker": {
    // Contenu JSON complet et structuré retourné par la bibliothèque marker-pdf.
    // La structure exacte (présence de tableaux, paragraphes, etc.)
    // dépend du document analysé.
  }
}
6. Endpoints Déployés

Les services sont exposés à l'extérieur du cluster via les URLs suivantes, définies dans les fichiers Ingress :

API Centrale : http://extraction-tableau-centrale.lab.sspcloud.fr

C'est le point d'entrée principal pour lancer une extraction.

API Marker : http://extraction-tableau-marker.lab.sspcloud.fr

Service de traitement de PDF. Généralement appelé par l'API Centrale.

Proxy LLM : http://extraction-tableau-proxy.lab.sspcloud.fr

Proxy d'observabilité pour le modèle de langage. Généralement appelé par l'API Marker.
