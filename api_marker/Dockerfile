FROM python:3.11-slim

WORKDIR /app

# Installation des dépendances système (plus importantes pour marker-pdf)
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-fra \
    libreoffice \
    git \
    && rm -rf /var/lib/apt/lists/*

# Installation des dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir fastapi uvicorn[standard]

# Copie du code source
COPY . .

# Exposition du port
EXPOSE 8001

# Sanity check pour marker
RUN python -c "import marker; print('Marker importé avec succès')"

# Commande de démarrage
CMD ["uvicorn", "main_marker:app", "--host", "0.0.0.0", "--port", "8001"]
