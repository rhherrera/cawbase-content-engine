# Linux validation + DOCX rendering image for the A04 experiments (not part of the engine).
FROM python:3.13-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-writer fonts-dejavu-core fonts-liberation poppler-utils qpdf \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.13.1
