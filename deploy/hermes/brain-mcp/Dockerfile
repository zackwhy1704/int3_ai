FROM python:3.12-slim
WORKDIR /srv
# Install from the fully-pinned, hashed lockfile (pip-compile --generate-hashes).
# --require-hashes fails the build if any package's content does not match,
# closing the substituted-dependency supply-chain risk.
COPY requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY server.py /srv/server.py
EXPOSE 9000
CMD ["python", "server.py"]
