FROM python:3.12-slim

WORKDIR /app

# Install CA certificate tooling
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy corporate certs early so they are in the build layer cache
COPY certs/netskope-certadmin.crt /usr/local/share/ca-certificates/netskope-certadmin.crt
COPY certs/netskope-goskope.crt   /usr/local/share/ca-certificates/netskope-goskope.crt
COPY certs/slf-ca-bundle.crt      /usr/local/share/ca-certificates/slf-ca-bundle.crt
RUN update-ca-certificates

COPY .github/skills/ /app/.github/skills/
COPY common/ /app/common/
COPY certs/   /app/certs/
COPY jira/    /app/jira/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    CORP_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

CMD ["python3", "jira/app.py"]