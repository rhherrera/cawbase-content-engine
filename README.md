# cawbase-content-engine

> ## Estado: A01 cerrada y aceptada · A02 en especificación
>
> | Feature | Estado |
> |---|---|
> | **A01** — base del producto (`0.1.0`) | Implementada, **cerrada y aceptada por el usuario**. Commit aceptado: `1b5ffab`. GitHub Actions aprobado en `main` y `feature/a01-product-foundation`, según confirmación explícita del usuario |
> | **A02** — perfiles, categorías y tipos sensibles | **En especificación**: contrato documentado en la rama `feature/a02-profiles-categories-sensitive-types`; **no implementada, no probada, no aceptada**. Nada de A02 existe en el código ni en la versión del paquete (`0.1.0`) |
>
> Detalle histórico de las pruebas de A01 en el [informe de implementación](docs/a01-implementation-report.md). El paquete **no está publicado** (no se afirma disponibilidad del nombre en PyPI) y la licencia definitiva está pendiente.

Motor independiente de clasificación, detección y transformación de contenido, preparado para distribuirse como paquete Python y, más adelante, abrirse como código abierto. CAWbase será un consumidor del paquete; el motor no depende de CAWbase.

- Distribución: `cawbase-content-engine` · Import Python: `cawbase_content_engine` · Python 3.13 · PostgreSQL y SQLite.
- API pública asíncrona; núcleo liviano con extras opcionales; adaptador HTTP opcional con API keys.

## A01 — Base del producto e instalación independiente (cerrada)

La primera feature define la base instalable: empaquetado, configuración e inicialización explícitas, persistencia y migraciones propias, contexto autorizado y aislamiento por alcance, adaptador HTTP con API keys, diagnóstico y readiness, errores, observabilidad y límites. Excluye clasificación, inferencia, documentos y todo lo propio de CAWbase.

Estado de A01: **implementada, cerrada y aceptada** (commit `1b5ffab`).

## A02 — Perfiles, categorías y tipos sensibles (en especificación)

> **Especificado para A02; no implementado.** Es solo documentación: no hay código, migraciones, pruebas ni mock, y la versión del paquete sigue en `0.1.0`. Ninguna ruta, operación ni permiso de A02 está disponible.

Contrato: perfiles que pertenecen a un scope, con categorías y tipos sensibles propios; configuración editable separada de **versiones publicadas inmutables**; perfil predeterminado opcional por scope; catálogo inicial versionado de tipos sensibles copiado al crear el perfil; configuración de ranking/resolución; permisos `profiles:read|write` (2 especificados sobre los 8 implementados en A01). Entrada: [especificación de A02](docs/features/A02-perfiles-categorias-y-tipos-sensibles.md).

## Probar rápidamente

```bash
uv sync --group dev
uv run python examples/python_host.py          # host Python: migra, crea alcances, aislamiento, transacción del host (SQLite)
bash examples/standalone_service.sh            # servicio HTTP real: migra, bootstrap, escucha (SQLite)
uv run pytest -q                               # SQLite; con CCE_TEST_POSTGRES_URL también PostgreSQL (ver docs/development.md)
bash scripts/linux_docker_validate.sh /tmp/ev  # validación en Linux (Docker): Python 3.13 + PostgreSQL 15-18
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
| [Especificación de A02](docs/features/A02-perfiles-categorias-y-tipos-sensibles.md) | *Especificado; no implementado.* Alcance, decisiones y matriz de aceptación futura |
| [Perfiles y catálogo](docs/profiles-and-catalog.md) | *Especificado; no implementado.* Modelo, estados, revisiones, versiones, ranking/resolución y catálogo inicial |
| [Decisiones de A02](docs/decisions/A02-decisions.md) | Decisiones aprobadas, elecciones técnicas derivadas, diferencias con el playbook y backlog |
| [Reglas para agentes](CLAUDE.md) | Instrucciones de trabajo del repositorio |

`docs/STAGE_3_PLAYBOOK.md` puede existir en el árbol de trabajo como **copia local de referencia** del playbook (revisión 6, 11 de noviembre de 2026), pero está en `.gitignore`: **no forma parte del repositorio**, un checkout limpio no la contiene y nada (tests, enlaces, wheel, sdist) la necesita. La fuente canónica está fuera de este repositorio.

## Convención de estado

Los comandos de este README existen y se ejecutaron (A01). Los ejemplos en los documentos permanentes son ilustrativos; la versión ejecutable y verificada está en `examples/` y `tests/`. «Implementado» no significa aceptado. Lo rotulado «especificado para A02; no implementado» no existe todavía.
