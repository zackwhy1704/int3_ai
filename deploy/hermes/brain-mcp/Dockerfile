FROM python:3.12-slim
WORKDIR /srv
RUN pip install --no-cache-dir "mcp>=1.2,<2" "httpx>=0.27"
COPY server.py /srv/server.py
EXPOSE 9000
CMD ["python", "server.py"]
