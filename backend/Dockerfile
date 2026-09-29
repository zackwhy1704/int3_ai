FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Download the embedding and reranking models at build time so startup needs no network.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/models')"
RUN python -c "from fastembed.rerank.cross_encoder import TextCrossEncoder; TextCrossEncoder('Xenova/ms-marco-MiniLM-L-6-v2', cache_dir='/models')"
COPY pytest.ini .
COPY app ./app
COPY tests ./tests
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
