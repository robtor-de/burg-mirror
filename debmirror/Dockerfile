FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        debmirror \
        gnupg \
        ca-certificates \
        python3 \
        python3-pip \
        python3-yaml \
        python3-rich \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY deb_mirror.py .

# Config und Targets werden zur Laufzeit gemountet:
#   -v /host/deb_mirror.yaml:/config/deb_mirror.yaml:ro
#   -v /host/mirrors:/srv/mirrors
#   -v /host/gpg-keys:/etc/mirror/gpg-keys

ENTRYPOINT ["python3", "/app/deb_mirror.py", "/config/deb_mirror.yaml"]
CMD []
