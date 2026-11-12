# Desarrollo

> **Estado: A01 implementada en `0.1.0`, cerrada y aceptada por el usuario (commit aceptado `1b5ffab`; GitHub Actions aprobado en `main` y en `feature/a01-product-foundation`, según confirmación explícita del usuario). A02 implementada en `0.2.0`: evidencia y niveles en el [informe de A02](a02-implementation-report.md); A02 cerrada y aceptada por el usuario (commit aceptado `f4ad3e7`; GitHub Actions pasó sobre ese commit, según su confirmación explícita).** Los comandos de este documento existen y fueron ejecutados; el resultado real está en el [informe de implementación](a01-implementation-report.md).

Relacionado: [feature A01](features/A01-base-del-producto-instalacion-independiente.md) · [instalación y operación](installation-and-operations.md) · [CLAUDE.md](../CLAUDE.md).

## 1. Proceso por feature

1. Documentación y contrato primero. El primer prompt de cada feature **no** implementa.
2. Revisión del usuario y resolución de las propuestas que bloquean ([decisiones](decisions/A01-decisions.md)).
3. Prompt de implementación autocontenido; implementación; ejecución de checks.
4. Instalación en ambiente de aceptación, pruebas con evidencia y acta de cierre (solo la emite el usuario).

Distinguir siempre: **especificado** (documentado) ≠ **implementado** (hay código) ≠ **probado** (checks ejecutados con evidencia) ≠ **aceptado** (acta del usuario). Un test omitido (*skip*) no cuenta como aprobado.

## 2. Entorno (D-02, D-03)

- Desarrollo local: macOS. Aceptación automatizada: Linux (GitHub Actions; aprobado sobre `1b5ffab` según confirmación del usuario). Python 3.13, gestionado con `uv`.
- Backends reales en las pruebas: PostgreSQL (15, 17 y 18 ejecutados localmente) y SQLite. Ningún test de persistencia usa dobles.

```bash
uv sync --group dev          # entorno de desarrollo (usa uv.lock)
```

### PostgreSQL de pruebas (descartable y aislado)

Las pruebas **nunca** usan una base existente. `CCE_TEST_POSTGRES_URL` apunta a un servidor descartable (usuario administrador); cada test crea una base propia `cce_t_<uuid>` y la elimina al terminar, sin tocar nada más. Ejemplo con un contenedor local dedicado (nombre y puerto propios de este repo):

```bash
docker run -d --name cce-test-pg17 -e POSTGRES_USER=cce_admin -e POSTGRES_PASSWORD=cce_test_pw \
  -p 127.0.0.1:55417:5432 postgres:17-alpine
export CCE_TEST_POSTGRES_URL='postgresql+asyncpg://cce_admin:cce_test_pw@127.0.0.1:55417/postgres'
export CCE_REQUIRE_POSTGRES=1      # falta de PostgreSQL = fallo, no un skip silencioso
```

Sin `CCE_TEST_POSTGRES_URL` las pruebas de PostgreSQL se omiten y se informan como omitidas; con `CCE_REQUIRE_POSTGRES=1` fallan.

## 3. Herramientas (adoptadas)

`uv`, `ruff` (lint y formato), `mypy` en modo estricto (único verificador de tipos), `pytest` + `pytest-asyncio`, `httpx` (pruebas ASGI), `build` vía `uv build`. Configuración en [pyproject.toml](../pyproject.toml). `ruff format` se aplica a `src tests examples scripts` (no al Markdown).

## 4. Checks

```bash
uv run ruff check .
uv run ruff format --check src tests examples scripts
uv run mypy
uv run pytest -q -rs                 # SQLite siempre; PostgreSQL si CCE_TEST_POSTGRES_URL está definida
uv run pytest -m dist -q             # construye wheel+sdist y los instala en entornos limpios fuera del árbol (necesita índice de paquetes)
uv run python scripts/measure_import.py --runs 15      # importación en frío/caliente y módulos cargados (registra; el gate D-33 lo aplica el test de `-m dist`)
uv run --isolated --resolution lowest-direct --group dev pytest -q   # cotas inferiores declaradas
bash examples/standalone_service.sh  # servicio real con SQLite
uv run python examples/python_host.py
```

Suites (en `tests/`): `test_spike_sqlite.py` y `test_spike_alembic.py` (incertidumbres técnicas de base), `test_config_and_lifecycle.py`, `test_transactions.py`, `test_scopes_and_isolation.py`, `test_credentials.py`, `test_bootstrap_and_recovery.py`, `test_migrations.py`, `test_http.py`, `test_processes.py` (CLI y servicio reales), `test_import_light.py`, `test_remaining_acceptance.py`, `test_examples.py`, `test_distribution.py` (marca `dist`).

## 5. Compatibilidad de versiones

Cotas declaradas en `pyproject.toml`: `sqlalchemy>=2.0.36,<2.2`, `pydantic>=2.9,<3`, `asyncpg>=0.30,<1`, `aiosqlite>=0.20,<1`, `alembic>=1.14,<2`, `fastapi>=0.115,<1`, `uvicorn>=0.32,<1`. Se ejecutó la suite con la resolución **más baja** de esas cotas y con la **más alta** disponible (versiones exactas en el informe). PostgreSQL se probó en 15, 17 y 18; PostgreSQL 14 llega a fin de soporte el 12-nov-2026 y no se soporta. De SQLite solo se probó la versión enlazada por el Python de la plataforma de pruebas; el piso 3.38 se comprueba en tiempo de ejecución pero **no** se ejecutó en esa versión.

## 6. Convenciones de código

- API pública explícita; lo demás privado.
- Sin efectos al importar (verificado en un subproceso); ninguna configuración global de logging, lectura de entorno ni conexiones en módulos.
- Todo acceso a datos de alcance pasa por el contexto; cualquier consulta de recurso de alcance lleva `scope_id`.
- Errores tipados con código estable; nunca se propaga texto del driver; se evita `__context__` que pueda arrastrar un secreto (ver `SecretDeliveryError`).
- Sin secretos ni valores de `metadata` en logs, excepciones ni fixtures versionados; las claves de prueba se generan en tiempo de ejecución.
- Pruebas de concurrencia separadas por backend con expectativas distintas.
- Los enlaces sin romper solo acreditan la coherencia de referencias entre documentos; **no validan técnicamente el contrato**, que se valida ejecutando la matriz de aceptación.

## 7. CI y validación Linux

**GitHub Actions: aprobado en `main` y `feature/a01-product-foundation` sobre `1b5ffab`, según confirmación explícita del usuario (D-34).** [.github/workflows/ci.yml](../.github/workflows/ci.yml) define `static`, `tests` (PostgreSQL 15, 16, 17 y 18 + SQLite, `CCE_REQUIRE_POSTGRES=1`), `tests-lowest-bounds`, `distribution` (construye y audita wheel/sdist, los prueba fuera del árbol y mide la importación) y sube resultados como artefactos (`junit-*`, `distribution`) incluso si un job falla. Este repositorio no conserva URL, ID ni fecha de esa ejecución. **Sobre A02 (`0.2.0`) no se ha ejecutado Actions** (no se hizo push; lo hace el usuario): la evidencia de A02 es macOS y Linux local ([informe de A02](a02-implementation-report.md)). `tests/test_ci_contract.py` verifica que las acciones estén fijadas por SHA, que cada job de PostgreSQL exija el servicio (`CCE_REQUIRE_POSTGRES=1`) y que un PostgreSQL ausente o inalcanzable **falle** la suite.

Versiones de las acciones comprobadas el 2026-11-11 contra la última *release* de cada repositorio y el README de `setup-uv` (que recomienda fijar por SHA): `actions/checkout` v7.0.1, `astral-sh/setup-uv` v10.3.3, `actions/upload-artifact` v7.0.2.

**Linux local (Docker), no es GitHub Actions:**

```bash
bash scripts/linux_docker_validate.sh /ruta/evidencia      # Python 3.13 (Debian bookworm) + PostgreSQL 15/16/17/18 en contenedores exclusivos
SNAPSHOT=head bash scripts/linux_docker_validate.sh ...    # exactamente el estado commiteado (git archive HEAD)
bash scripts/sqlite_floor_validate.sh /ruta/evidencia      # SQLite 3.38.5 (piso declarado) y 3.37.2 (debajo del piso) enlazados al sqlite3 del intérprete
```

Los scripts crean la red `cce-linux-net` y los contenedores `cce-linux-pg*`; elimínelos con `docker rm -f cce-linux-pg15 cce-linux-pg16 cce-linux-pg17 cce-linux-pg18 && docker network rm cce-linux-net`.

Auditoría de artefactos: `uv run python scripts/audit_artifacts.py dist/*` (archivos accidentales y patrones de secretos). Mediciones de importación desde un wheel instalado en un venv limpio: `uv run python scripts/measure_distribution.py`.

## 8. Documentación

- Los documentos permanentes conservan contratos; la especificación de feature define alcance y aceptación y enlaza, no copia.
- Todo cambio de contrato actualiza en el mismo cambio: [API pública](public-api.md), [persistencia](persistence-and-migrations.md), [seguridad](security-and-isolation.md), [operación](installation-and-operations.md), [arquitectura](architecture.md), [decisiones](decisions/A01-decisions.md), la matriz de aceptación y el [informe](a01-implementation-report.md).
- Antes de cerrar una tarea: revisar enlaces relativos, nombres de permisos, códigos de error y estados entre documentos.

## 9. Commits y ramas

Rama de implementación de A01: `feature/a01-product-foundation` (cerrada). Rama de especificación de A02: `feature/a02-profiles-categories-sensitive-types` (commits locales; crear commits locales **no** autoriza push ni publicar). `.DS_Store` y `docs/STAGE_3_PLAYBOOK.md` están en `.gitignore` (commit `d091b49`, del usuario) y no se agregan a Git; la segunda es una copia local de referencia que **ningún test, enlace ni artefacto necesita**. Un checkout limpio no la contiene y construye wheel y sdist idénticos.

## 10. A02 — Proceso previsto (implementado)

- A02 está **implementada, cerrada y aceptada** (`0.2.0`; commit aceptado `f4ad3e7`; Actions según confirmación del usuario), sin mock ni inferencia. Los criterios `AC-A02-nn` de la [matriz](features/A02-perfiles-categorias-y-tipos-sensibles.md#7-matriz-de-aceptación) se trazan a pruebas en el [informe de A02](a02-implementation-report.md). Suites nuevas: `tests/test_a02_profiles.py`, `test_a02_concurrency.py` (PostgreSQL y SQLite reales; incluye interbloqueos reales), `test_a02_http.py`, `test_a02_migration.py`; ejemplo ejecutable: `examples/profiles_host.py`.
- Cada feature usa **sus propios** documentos de decisiones, especificación e informe: A02 → `docs/decisions/A02-decisions.md`, `docs/features/A02-…md` y, al implementarse, un informe `docs/a02-implementation-report.md`.
- Los checks de §4 se mantienen; las pruebas de PostgreSQL siguen usando solo bases descartables `cce_t_*`; las pruebas de concurrencia de A02 se escriben por backend; se verifica el esquema 1→2 con objetos ajenos; se cuida que ningún fixture contenga secretos ni datos personales reales (usar dominios `example.test` y valores evidentemente falsos).
- La importación liviana y el gate D-33 deben seguir cumpliéndose (el catálogo se importa de forma perezosa).

## 11. A03 — Proceso previsto (implementado)

- A03 está **implementada** (`0.3.0`), **cerrada y aceptada por el usuario** (commit de implementación `20d9211`; documentación y evidencia posterior `e3ca6da`; merge `ae19ef3` del PR #2; resultado de GitHub Actions no verificado con la evidencia disponible). Suites nuevas: `tests/test_a03_contract.py` (sin base de datos: numérica exacta, ranking/resolución, validador, localizadores, mock), `test_a03_analysis.py` (SQLite y PostgreSQL reales), `test_a03_http.py` (incluye desconexión del cliente), ejemplo `examples/analysis_host.py`; informe: [`docs/a03-implementation-report.md`](a03-implementation-report.md). Documentos propios: [`docs/decisions/A03-decisions.md`](decisions/A03-decisions.md), [`docs/features/A03-…md`](features/A03-contrato-de-inferencia-y-analisis-con-mock.md) y el contrato permanente [`docs/inference-and-analysis.md`](inference-and-analysis.md).
- La matriz de aceptación ([feature A03 §7](features/A03-contrato-de-inferencia-y-analisis-con-mock.md#7-matriz-de-aceptación-para-la-implementación-posterior)) se verificará con SQLite y PostgreSQL reales (solo bases descartables `cce_t_*`), un adaptador de pruebas que devuelve salidas crudas arbitrarias y el mock; sin modelo real ni medición de calidad (A09).
- Los checks de §4 se mantienen. La importación liviana (D-33) debe seguir cumpliéndose; no se agregan dependencias pesadas. Ningún fixture contiene secretos ni datos personales reales (dominios `example.test`, valores evidentemente falsos).
