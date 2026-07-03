FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        skopeo \
        git \
        ca-certificates \
        python3 \
        python3-pip \
        python3-yaml \
        python3-rich \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY project_mirror.py .

# Config und Targets werden zur Laufzeit gemountet:
#   -v /host/project_mirror.yaml:/config/project_mirror.yaml:ro
#   -v /host/mirrors:/srv/mirrors

ENTRYPOINT ["python3", "/app/project_mirror.py", "/config/project_mirror.yaml"]
CMD []
