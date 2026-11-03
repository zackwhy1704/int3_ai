# Ingress auth proxy (launcher mode): one per user, in the slot the socat forwarder used.
# stdlib only, no pip install. Pinned by digest like the socat image.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
COPY ingress_auth.py /opt/ingress/ingress_auth.py
USER 65534:65534
ENTRYPOINT ["python3", "/opt/ingress/ingress_auth.py"]
