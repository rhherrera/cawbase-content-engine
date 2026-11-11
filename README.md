# cawbase-content-engine

> ## Estado: A01 implementado, probado localmente, no aceptado
>
> La **base del producto (A01)** está implementada en la versión `0.1.0` y se probó en macOS contra PostgreSQL 15/17/18 y SQLite (resultados reales en el [informe de implementación](docs/a01-implementation-report.md)). **No se ha ejecutado en Linux/GitHub Actions, no hay commits ni push, y el usuario no ha aceptado la feature.** El paquete **no está publicado** (no se afirma disponibilidad del nombre en PyPI) y la licencia definitiva está pendiente. Quedan fuera A02 en adelante (perfiles, clasificación, inferencia, documentos).

Motor independiente de clasificación, detección y transformación de contenido, preparado para distribuirse como paquete Python y, más adelante, abrirse como código abierto. CAWbase será un consumidor del paquete; el motor no depende de CAWbase.

- Distribución: `cawbase-content-engine` · Import Python: `cawbase_content_engine` · Python 3.13 · PostgreSQL y SQLite.
- API pública asíncrona; núcleo liviano con extras opcionales; adaptador HTTP opcional con API keys.

## Qué se está especificando (A01 — Base del producto e instalación independiente)

La primera feature define la base instalable: empaquetado, configuración e inicialización explícitas, persistencia y migraciones propias, contexto autorizado y aislamiento por alcance, adaptador HTTP con API keys, diagnóstico y readiness, errores, observabilidad y límites. Excluye clasificación, inferencia, documentos y todo lo propio de CAWbase.

Estado de A01: **implementado y probado localmente; no probado en Linux/CI; no aceptado; no cerrado.**

## Probar rápidamente

```bash
uv sync --group dev
uv run python examples/python_host.py          # host Python: migra, crea alcances, aislamiento, transacción del host (SQLite)
bash examples/standalone_service.sh            # servicio HTTP real: migra, bootstrap, escucha (SQLite)
uv run pytest -q                               # SQLite; con CCE_TEST_POSTGRES_URL también PostgreSQL (ver docs/development.md)
```

## Documentación

| Documento | Contenido |
|---|---|
| [Especificación de A01](docs/features/A01-base-del-producto-instalacion-independiente.md) | Alcance, matriz de aceptación y evidencia requerida |
| [Arquitectura](docs/architecture.md) | Componentes, dependencias, estructura propuesta |
| [API pública](docs/public-api.md) | API Python, transacciones, idempotencia, errores, contrato HTTP |
| [Persistencia y migraciones](docs/persistence-and-migrations.md) | Modelo, namespaces, migraciones, PostgreSQL vs. SQLite |
| [Seguridad y aislamiento](docs/security-and-isolation.md) | Alcances, permisos, API keys, bootstrap, retención |
| [Instalación y operación](docs/installation-and-operations.md) | Extras, configuración, readiness, observabilidad, límites |
| [Desarrollo](docs/development.md) | Proceso, entorno y verificaciones planificadas |
| [Decisiones de A01](docs/decisions/A01-decisions.md) | Decisiones aprobadas, propuestas pendientes y diferencias con el playbook |
| [Informe de implementación](docs/a01-implementation-report.md) | Qué se implementó, checks ejecutados, matriz AC trazable, mediciones y límites |
| [Reglas para agentes](CLAUDE.md) | Instrucciones de trabajo del repositorio |

`docs/STAGE_3_PLAYBOOK.md` es una copia de referencia del playbook (revisión 6, 11 de noviembre de 2026); la fuente canónica está fuera de este repositorio.

## Convención de estado

Los comandos de este README existen y se ejecutaron. Los ejemplos en los documentos permanentes son ilustrativos; la versión ejecutable y verificada está en `examples/` y `tests/`. «Implementado» no significa aceptado.
