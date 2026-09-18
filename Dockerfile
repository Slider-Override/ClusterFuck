FROM python:3.12-slim-trixie
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data WEB_PORT=8443 IPERF_PORT=5201
RUN apt-get update && apt-get install -y --no-install-recommends iperf3 openssl tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home clusterfuck \
    && mkdir /data && chown clusterfuck:clusterfuck /data \
    && iperf3 --help | grep -q -- --json-stream
WORKDIR /app
COPY --chown=clusterfuck:clusterfuck clusterfuck /app/clusterfuck
USER clusterfuck
EXPOSE 8443/tcp 5201/tcp 5201/udp
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import os,ssl,urllib.request; scheme='https' if os.getenv('TLS_ENABLED','true').lower()=='true' else 'http'; urllib.request.urlopen(scheme+'://127.0.0.1:'+os.getenv('WEB_PORT','8443')+'/healthz',context=ssl._create_unverified_context(),timeout=3)" || exit 1
CMD ["python", "-m", "clusterfuck.server"]
