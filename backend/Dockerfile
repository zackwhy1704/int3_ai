# ---- production ----
# Build context must be the repo root (context: . in docker-compose.yml)
# so COPY libs/ can reach libs/authcore.
FROM python:3.12-slim AS production

WORKDIR /app

# Install shared authcore package first (provides authlib, joserfc, cryptography).
COPY libs/ /app/libs/
RUN pip install --no-cache-dir /app/libs/authcore

# Install locked runtime dependencies (does NOT include pytest or dev tools).
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Download the embedding and reranking models at build time so startup needs no network.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/models')"
RUN python -c "from fastembed.rerank.cross_encoder import TextCrossEncoder; TextCrossEncoder('Xenova/ms-marco-MiniLM-L-6-v2', cache_dir='/models')"

COPY backend/app/ /app/app/
COPY seed/ /app/seed/

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ---- test (for tools container) ----
FROM production AS test

COPY backend/requirements-dev.txt /app/requirements-dev.txt
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY backend/pytest.ini /app/pytest.ini
COPY backend/tests/ /app/tests/
