FROM python:3.14-slim

# Java 11+ requis par opendataloader-pdf
RUN apt-get update && apt-get install -y \
    default-jdk \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip

# Reproduction de la structure du repo pour que les chemins relatifs (../../libs) fonctionnent
WORKDIR /repo
COPY libs/ ./libs/
COPY api/api_opendataloader/requirements.txt ./api/api_opendataloader/

WORKDIR /repo/api/api_opendataloader

RUN pip install --no-cache-dir -r requirements.txt

# Vérification des imports
RUN python -c "import opendataloader_pdf, fastapi; print('All modules OK')"

COPY api/api_opendataloader/src/ ./src/

EXPOSE 8002

CMD ["uvicorn", "main_opendataloader:app", "--host", "0.0.0.0", "--port", "8002", "--app-dir", "src"]
