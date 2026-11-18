# A08 — Comprobación D-A04-04 (experimentos)

**Estado: etapa 1 de A08. Solo contiene el manifiesto de datos del corpus (`corpus/manifest.json`), propuesto para congelar y pendiente de la aprobación del usuario. No hay código experimental, corpus exportado ni mediciones.**

- Protocolo: [`docs/a08-pdf-corpus-protocol.md`](../../docs/a08-pdf-corpus-protocol.md) · contrato propuesto: [`docs/pdf-protection.md`](../../docs/pdf-protection.md) · decisiones: [`docs/decisions/A08-decisions.md`](../../docs/decisions/A08-decisions.md).
- La etapa 2 (tras la aprobación) añadirá aquí su propio `pyproject.toml`/`uv.lock`, los generadores, la línea base, el recorrido candidato, el verificador, los PDF exportados con su procedencia y los resultados por entorno (protocolo §12).
- Este directorio está fuera de la wheel (que solo empaqueta `src/cawbase_content_engine`) y excluido del sdist (`exclude = ["experiments"]` en `pyproject.toml`). Nada de `src/` lo importa.
- El manifiesto es **dato**: valores sintéticos evidentemente ficticios, texto de relleno, plantillas, selecciones y ocurrencias no seleccionadas con su contexto (oráculo de autoría). Tras la aprobación, su SHA-256 se registra en el primer commit de la etapa 2 y no se modifica sin una nueva revisión aprobada.
