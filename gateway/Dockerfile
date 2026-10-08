FROM python:3.12-slim

WORKDIR /app

# Copy shared libs first (authcore) so gateway can import authcore.verifier.
# Build context must be the repo root (context: . in docker-compose.yml).
COPY libs/ /app/libs/
RUN pip install --no-cache-dir -e libs/authcore

COPY gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy gateway source into /app so absolute imports (from auth, from translate, …) work.
COPY gateway/ .

# PORT is injected by Cloud Run; default to 8001 for docker compose.
ENV PORT=8001

EXPOSE 8001

# Single worker — Cloud Run scales by spawning more containers, not more threads.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
