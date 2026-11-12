# cawbase-content-engine

> ## Estado: A01 y A02 cerradas y aceptadas · A03 implementada (`0.3.0`), sin aceptar
>
> | Feature | Estado |
> |---|---|
> | **A01** — base del producto (`0.1.0`) | Implementada, **cerrada y aceptada por el usuario**. Commit aceptado: `1b5ffab`. GitHub Actions aprobado en `main` y `feature/a01-product-foundation`, según confirmación explícita del usuario |
> | **A02** — perfiles, categorías y tipos sensibles (`0.2.0`) | Implementada, **cerrada y aceptada por el usuario**. Commit aceptado: `f4ad3e7`. GitHub Actions pasó sobre ese commit, según confirmación explícita del usuario. El merge `e149146` (PR #1) en `main` es posterior y no es el commit aceptado. Evidencia histórica y límites en el [informe de A02](docs/a02-implementation-report.md) |
> | **A03** — contrato de inferencia y análisis con mock (`0.3.0`) | **Implementada** en la rama `feature/a03-inference-contract-mock` (commits locales, **sin push**). Probada localmente (macOS) y en Linux local (Docker; última validación sobre el código corregido `20d9211`) según el [informe de A03](docs/a03-implementation-report.md); **GitHub Actions no ejecutado sobre A03**; **no aceptada por el usuario** (la aceptación es suya). Solo ingeniería del contrato con un mock: **no** acredita calidad de inferencia (A09) |
>
> El paquete **no está publicado** (no se afirma disponibilidad del nombre en PyPI) y la licencia definitiva está pendiente. Fuera de lo implementado: A04 en adelante (documentos, protección, trabajos, modelo real).

Motor independiente de clasificación, detección y transformación de contenido, preparado para distribuirse como paquete Python y, más adelante, abrirse como código abierto. CAWbase será un consumidor del paquete; el motor no depende de CAWbase.

- Distribución: `cawbase-content-engine` · Import Python: `cawbase_content_engine` · Python 3.13 · PostgreSQL y SQLite.
- API pública asíncrona; núcleo liviano con extras opcionales; adaptador HTTP opcional con API keys.

## A01 — Base del producto e instalación independiente (cerrada)

La primera feature define la base instalable: empaquetado, configuración e inicialización explícitas, persistencia y migraciones propias, contexto autorizado y aislamiento por alcance, adaptador HTTP con API keys, diagnóstico y readiness, errores, observabilidad y límites. Excluye clasificación, inferencia, documentos y todo lo propio de CAWbase.

Estado de A01: **implementada, cerrada y aceptada** (commit `1b5ffab`).

## A02 — Perfiles, categorías y tipos sensibles (cerrada y aceptada)

> **Implementada en `0.2.0`; cerrada y aceptada (commit `f4ad3e7`).** Sin inferencia, sin clasificación efectiva y sin mock: solo configura, versiona y resuelve la configuración de análisis. Ejemplo ejecutable: `uv run python examples/profiles_host.py`.

Contrato: perfiles que pertenecen a un scope, con categorías y tipos sensibles propios; configuración editable separada de **versiones publicadas inmutables**; perfil predeterminado opcional por scope; catálogo inicial versionado de tipos sensibles copiado al crear el perfil; configuración de ranking/resolución; permisos `profiles:read|write` (2 nuevos sobre los 8 de A01). Entrada: [especificación de A02](docs/features/A02-perfiles-categorias-y-tipos-sensibles.md).

## A03 — Contrato de inferencia y análisis con mock (implementada, sin aceptar)

> **Implementada en `0.3.0`; no aceptada.** Una operación de análisis de texto (`ScopedOperations.analyze_text` y `POST /v1/scopes/{scope_id}/analyze-text`, mismo núcleo, resultado en la misma llamada, sin persistencia ni trabajos) sobre versiones publicadas de A02, con el permiso nuevo `analysis:execute`, una interfaz de adaptador de inferencia, validación estricta de su salida y un **mock determinista** sobre fixtures sintéticos. No elige modelo, no incluye inferencia real y **no acredita calidad de detección** (A09). Ejemplo ejecutable: `uv run python examples/analysis_host.py`. Contrato: [inferencia y análisis](docs/inference-and-analysis.md); entrada: [especificación de A03](docs/features/A03-contrato-de-inferencia-y-analisis-con-mock.md).

## Probar rápidamente

```bash
uv sync --group dev
uv run python examples/analysis_host.py        # A03: análisis de texto con el mock determinista (SQLite)
uv run python examples/profiles_host.py        # A02: perfil, categorías, publicación, predeterminado y resolución (SQLite)
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
| [Especificación de A02](docs/features/A02-perfiles-categorias-y-tipos-sensibles.md) | Alcance, decisiones y matriz de aceptación de A02 |
| [Perfiles y catálogo](docs/profiles-and-catalog.md) | Modelo, estados, revisiones, versiones, ranking/resolución y catálogo inicial |
| [Decisiones de A02](docs/decisions/A02-decisions.md) | Decisiones aprobadas, elecciones técnicas derivadas, diferencias con el playbook y backlog |
| [Informe de implementación de A02](docs/a02-implementation-report.md) | Qué se implementó, correspondencia con la matriz, comandos y resultados reales, límites y restricción de despliegue de permisos |
| [Especificación de A03](docs/features/A03-contrato-de-inferencia-y-analisis-con-mock.md) | Alcance, decisiones y matriz de aceptación de A03 (solo especificada) |
| [Inferencia y análisis](docs/inference-and-analysis.md) | Contrato completo de A03: fronteras, entrada, adaptador, validación, resultado, mock, errores y HTTP |
| [Decisiones de A03](docs/decisions/A03-decisions.md) | Decisiones aprobadas, elecciones técnicas derivadas, diferencias con el playbook y backlog |
| [Informe de implementación de A03](docs/a03-implementation-report.md) | Qué se implementó, matriz AC-A03 trazable a pruebas, comandos y resultados reales por ambiente, límites |
| [Reglas para agentes](CLAUDE.md) | Instrucciones de trabajo del repositorio |

`docs/STAGE_3_PLAYBOOK.md` puede existir en el árbol de trabajo como **copia local de referencia** del playbook (revisión 6, 11 de noviembre de 2026), pero está en `.gitignore`: **no forma parte del repositorio**, un checkout limpio no la contiene y nada (tests, enlaces, wheel, sdist) la necesita. La fuente canónica está fuera de este repositorio.

## Convención de estado

Los comandos de este README existen y se ejecutaron (A01). Los ejemplos en los documentos permanentes son ilustrativos; la versión ejecutable y verificada está en `examples/` y `tests/`. «Implementado» no significa aceptado.
