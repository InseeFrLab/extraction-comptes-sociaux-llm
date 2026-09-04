FROM python:3.14-slim

# installer uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Installation des dépendances système (nécessaires pour marker-pdf)
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

# Reproduction de la structure du repo pour que les chemins relatifs (../../libs) fonctionnent
WORKDIR /repo
COPY libs/ ./libs/
COPY api/api_marker/pyproject.toml api/api_marker/uv.lock ./api/api_marker/

WORKDIR /repo/api/api_marker

# Installation des dépendances Python (sans le projet lui-même)
RUN uv sync --frozen --no-install-project

# Code source
COPY api/api_marker/src/ ./src/

# Installation du projet
RUN uv sync --frozen

# Exposition du port
EXPOSE 8001

# Vérification
RUN uv run python -c "import marker; print('Marker OK')"

# Commande de démarrage
CMD ["uv", "run", "uvicorn", "main_marker:app", "--host", "0.0.0.0", "--port", "8001", "--app-dir", "/repo/api/api_marker/src"]
