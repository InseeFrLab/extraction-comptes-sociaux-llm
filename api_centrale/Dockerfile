FROM python:3.11-slim

WORKDIR /app

# Installation des dépendances système nécessaires
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Mise à jour de pip
RUN pip install --upgrade pip

# Installation des dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Vérification que les modules sont bien installés
RUN python -c "import requests, fastapi, uvicorn, dotenv, fitz, s3fs, PyPDF2; print('All modules imported successfully')"

# Copie du code source
COPY . .

# Exposition du port
EXPOSE 8000

# Commande de démarrage
CMD ["uvicorn", "main_centrale:app", "--host", "0.0.0.0", "--port", "8000", "--root-path", "/centrale"]