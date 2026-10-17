# Build context is the repo root (see docker-compose.yml): the image also ships
# scripts/build_index.py, which regenerates INDEX.md in the data repo.
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY importer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY importer/importer.py importer/images.py importer/prompt.md scripts/build_index.py ./
ENV PYTHONUNBUFFERED=1 TZ=Europe/Berlin
CMD ["python", "/app/importer.py"]
