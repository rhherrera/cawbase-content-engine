# A08 Linux image: LibreOffice exporter (family L), qpdf CLI, Poppler and the experiment runtime. Not part of the engine.
FROM python:3.13-trixie
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-writer fonts-crosextra-carlito fonts-crosextra-caladea fonts-dejavu-core fonts-liberation poppler-utils qpdf \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.13.1
RUN useradd -m -u 1000 runner
USER runner
