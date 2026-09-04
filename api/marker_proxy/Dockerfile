FROM python:3.14-slim

# installer uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Installation des dépendances système
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /repo
COPY api/marker_proxy/pyproject.toml api/marker_proxy/uv.lock ./api/marker_proxy/

WORKDIR /repo/api/marker_proxy

# Installation des dépendances Python
RUN uv sync --frozen --no-install-project

# Copie du code source
COPY api/marker_proxy/src/ ./src/

# Installation du projet
RUN uv sync --frozen

# Exposition du port
EXPOSE 1324

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:1324/health || exit 1

# Commande de démarrage
CMD ["uv", "run", "uvicorn", "proxy:app", "--host", "0.0.0.0", "--port", "1324", "--app-dir", "/repo/api/marker_proxy/src"]
