FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8001

EXPOSE 8001

# Use 1 worker in Cloud Run (one container = one process).
# Cloud Run handles scaling by spawning more containers.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]
