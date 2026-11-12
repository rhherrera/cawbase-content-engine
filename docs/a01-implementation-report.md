# A01 — Informe de implementación y validación

> **Estado: A01 está implementado (0.1.0). Probado en macOS y en Linux local (Docker). NO ejecutado en GitHub Actions. NO aceptado por el usuario. La feature NO está cerrada.** Este informe no es un acta de aceptación. Las cifras provienen de ejecuciones reales; lo que no se ejecutó se dice explícitamente.

Relacionado: [especificación](features/A01-base-del-producto-instalacion-independiente.md) · [decisiones](decisions/A01-decisions.md) · [desarrollo](development.md) · evidencia cruda resumida en [docs/evidence/](evidence/).

## 0. Estado por nivel de evidencia

| # | Nivel | Estado |
|---|---|---|
| 1 | **Implementado** | Sí: paquete `0.1.0`, núcleo liviano, PostgreSQL y SQLite, transacciones del host, migraciones de un bloqueo, aislamiento y delegación, API keys, bootstrap/recuperación, HTTP, servicio y CLI |
| 2 | **Probado en macOS** | Sí (macOS 27.0.1 arm64, Python 3.13.16): PostgreSQL 15, 16, 17 y 18 + SQLite, cotas inferiores, distribución |
| 3 | **Probado en Linux local** | Sí (Docker: Debian 12 aarch64, Python 3.13.18), **sobre el estado commiteado `7ffbe2a`** (`git archive HEAD`): PostgreSQL 15, 16, 17 y 18 + SQLite, cotas inferiores, distribución, auditoría de artefactos, mediciones, y la suite completa con SQLite 3.38.5 |
| 4 | **Probado en GitHub Actions** | **No.** El workflow está preparado; no se hizo push y no existe ninguna ejecución remota |
| 5 | **Aceptación del usuario** | **Pendiente.** Además queda pendiente técnico (§7): la ejecución remota del workflow |

Una corrida Linux local en Docker **no equivale** a una ejecución de GitHub Actions.

**Commit base de esta revisión:** `d091b49` (cambio del usuario sobre `.gitignore`, posterior a los cuatro commits iniciales `82ddffe`, `c98bda2`, `0dacef3`, `688b89f`). Los commits nuevos de esta revisión se listan en §2. Los resultados de §3 se obtuvieron con el código **posterior** a esas correcciones; no se atribuyen al código anterior.

## 1. Qué se implementó

- Paquete `cawbase-content-engine` (import `cawbase_content_engine`, `0.1.0`), núcleo liviano (SQLAlchemy Core asíncrono + Pydantic) y extras `postgres`, `sqlite`, `migrations`, `http`.
- Configuración por URL **o** engine inyectado (excluyentes), inicio/cierre explícitos con ownership correcto, sin efectos al importar.
- PostgreSQL (schema `cawbase_content_engine`) y SQLite (prefijo `cawbase_content_engine_`), tablas e historial propios, migración inicial `0001`.
- Transacciones propias y del host (`HostTransaction`, savepoint por operación, `transaction_broken`, guarda por conexión subyacente, precondiciones de SQLite y del engine inyectado).
- Migraciones: una transacción y un bloqueo para toda la secuencia pendiente (advisory lock transaccional / `BEGIN IMMEDIATE`), plan calculado tras el bloqueo, sin commits intermedios.
- `Scope` mínimo, aislamiento por contexto, 8 permisos, delegación (`admin:grants:write`), control optimista estricto (D-30), clientes, API keys (formato, hash, presentación única, idempotencia, revocación), eventos administrativos.
- Bootstrap inicial y recuperación administrativa locales con entrega a terminal o archivo `0600` exclusivo.
- Adaptador HTTP (`create_router` montable, no migra), servicio independiente (validar → migrar → verificar → escuchar), CLI, `/livez`, `/readyz`, diagnóstico autenticado.
- Pruebas, ejemplos, `uv.lock`, wheel + sdist, scripts de validación Linux y del piso de SQLite, auditoría de artefactos, y el workflow de GitHub Actions (**no ejecutado**).

No se implementó nada de A02+, ni OIDC/usuarios, ni integración con CAWbase, ni AWS, ni publicación.

## 2. Revisión de validación: defectos encontrados y correcciones

Commits locales de esta revisión (sobre `d091b49`; sin push):

| Commit | Contenido |
|---|---|
| `c147371` | `build:` `uvicorn>=0.50`, `pyyaml>=6.0.2`, `scripts/` y `uv.lock` en el sdist |
| `5ddf814` | `fix(service):` código de salida de un worker que falla al iniciar, logs de Uvicorn, `X-Request-ID` en el 503 de la compuerta |
| `a0894a4` | `test:` AC-26 real, AC-101 con procesos reales, AC-07 en PostgreSQL desde el wheel, contrato de CI, portabilidad Linux |
| `ba025f9`, `7ffbe2a` | `ci:` workflow con acciones fijadas por SHA y PostgreSQL 16, artefactos, auditoría, scripts de validación Linux / piso de SQLite |
| `a834026`, `9aca5d7`, y el commit que actualiza esta tabla | informe y evidencia cruda; README, CLAUDE.md, decisiones, especificación y contratos |

| # | Defecto | Dónde | Corrección |
|---|---|---|---|
| 1 | **El test de AC-26 era débil y dependía del usuario**: `chmod 444` sobre un archivo vacío con aserción que aceptaba varios errores. **Falló como root en Linux** (root ignora permisos) | test (la implementación ya traducía bien) | Fixture `mode=ro` verificado como solo lectura; se exige exactamente `MigrationPermissionError`; hash del archivo antes/después; sin rutas; CLI. El test por `chmod` queda, omitido como root con motivo |
| 2 | **`serve()` devolvía 0** cuando, con varios workers, el supervisor se detenía porque un worker no podía iniciar; con un worker Uvicorn salía con 3, indistinguible de «migración» | producto | Marcador privado lanzador↔worker; el servicio sale con el código real (4) |
| 3 | Uvicorn < 0.50 **reinicia indefinidamente** a un worker que falla al iniciar (release notes de 0.50.0), contradiciendo «fallo ⇒ no iniciar tráfico, salir ≠ 0» | rango declarado | `uvicorn>=0.50` (probado con 0.50.0 y 0.54.0) |
| 4 | Los mensajes del supervisor de Uvicorn no se veían (`log_config=None`) y un fallo de arranque volcaba una traza | producto | Se enrutan por el manejador JSON del servicio, que imprime solo el mensaje (nunca `exc_info`) |
| 5 | El 503 de la compuerta de readiness iba sin `X-Request-ID` | producto | Se agrega encabezado y `request_id` |
| 6 | `pyyaml>=6` resuelve a 6.0, que no compila en Python 3.13: la resolución de cotas inferiores fallaba | dependencia de desarrollo | `pyyaml>=6.0.2` |
| 7 | El helper de procesos usaba `ps` sin `-ww`: bajo `pytest` en Linux (`COLUMNS`) truncaba las líneas y no encontraba workers | test | `ps -A -ww` |
| 8 | La instantánea para Linux incluía archivos AppleDouble (`._*`) de macOS | script | `COPYFILE_DISABLE=1 tar --no-xattrs` |
| 9 | La documentación afirmaba que `docs/STAGE_3_PLAYBOOK.md` «existe en docs/» y que el árbol estaba sin commitear | documentación | Corregido: copia local ignorada por Git; ningún test, enlace ni artefacto la usa |

Sin cambios de decisiones aprobadas. Cambios de rango declarado: `uvicorn` (≥ 0.50) y `pyyaml` de desarrollo (≥ 6.0.2), ambos por evidencia (I-10 en [decisiones](decisions/A01-decisions.md)).

## 3. Checks ejecutados y resultados reales

### 3.1 Linux local (Docker) — estado commiteado `7ffbe2a`

Plataforma: `Linux 7.0.14-linuxkit aarch64 | Debian GNU/Linux 12 (bookworm)`, Python 3.13.18, uv 0.13.7, `sqlite3.sqlite_version` en el proceso de pruebas = 3.40.1. Contenedores exclusivos `cce-linux-pg15/16/17/18` (15.19, 16.15, 17.11, 18.6) en una red dedicada, `CCE_REQUIRE_POSTGRES=1`. Comando: `SNAPSHOT=head bash scripts/linux_docker_validate.sh DIR`. Resumen crudo: [evidence/linux-local-summary.txt](evidence/linux-local-summary.txt).

| Check | Resultado |
|---|---|
| `ruff check .` / `ruff format --check src tests examples scripts` / `mypy` (estricto) | pasan (57 archivos formateados, 34 fuentes tipadas) |
| Suite completa, PostgreSQL 15.19 + SQLite | 330 passed, 8 skipped |
| Suite completa, PostgreSQL 16.15 + SQLite | 330 passed, 8 skipped |
| Suite completa, PostgreSQL 17.11 + SQLite | 330 passed, 8 skipped |
| Suite completa, PostgreSQL 18.6 + SQLite | 330 passed, 8 skipped |
| Cotas inferiores declaradas + PostgreSQL 17 (versiones realmente instaladas: `{'sqlalchemy': '2.0.36', 'pydantic': '2.9.0', 'asyncpg': '0.30.0', 'aiosqlite': '0.20.0', 'alembic': '1.14.0', 'fastapi': '0.115.0', 'uvicorn': '0.50.0', 'pytest': '8.3.0', 'pytest-asyncio': '0.24.0', 'httpx': '0.27.0'}`) | 330 passed, 8 skipped |
| `pytest -m dist` (wheel + sdist en venvs limpios fuera del árbol; AC-07 en PostgreSQL 17) | 7 passed |
| `scripts/audit_artifacts.py` sobre wheel + sdist | 0 hallazgos |
| Suite completa con **SQLite 3.38.5** enlazado en el proceso (+ PostgreSQL 17) | 331 passed, 7 skipped ([evidence/sqlite-floor-summary.txt](evidence/sqlite-floor-summary.txt)) |
| `start()` con **SQLite 3.37.2** (debajo del piso) | rechazado: `SQLite 3.38 or newer is required (this runtime links 3.37.2)` |

**Skips justificados (8 por corrida en Linux):** (a) 6 tests «solo PostgreSQL» que se omiten en la variante SQLite del parámetro (`test_config_and_lifecycle.py:187`, `test_migrations.py:182` y `:292`, `test_processes.py:302`, `test_remaining_acceptance.py:158`, `test_transactions.py:240`); (b) `test_sqlite_file_without_write_permission_*`: **root ignora los permisos de archivo**; el escenario lo cubre el test `mode=ro`; (c) `test_sqlite_library_under_test_is_the_expected_one`: solo se exige cuando se define `CCE_EXPECT_SQLITE_VERSION` (se ejecuta, y pasa, en la corrida de SQLite 3.38.5). En macOS son 7 (no hay el skip de root). Ninguno se cuenta como aprobado. Con `CCE_REQUIRE_POSTGRES=1`, un PostgreSQL ausente o inalcanzable es un **fallo** (verificado por `tests/test_ci_contract.py`).

### 3.2 macOS

macOS 27.0.1 arm64, Python 3.13.16, PostgreSQL 15.19/16.15/17.11/18.6 en contenedores locales, SQLite 3.54.0. Las ejecuciones se hicieron sobre un árbol cuyo `src/` y `tests/` son idénticos a los de `7ffbe2a` (después solo cambiaron `pyproject.toml`—cota de `pyyaml`, la cota inferior se reejecutó—, un script y documentación). Resumen: [evidence/macos-summary.txt](evidence/macos-summary.txt).

| Check | Resultado |
|---|---|
| ruff, formato, mypy | pasan |
| Suite completa, PostgreSQL 15 / 16 / 17 / 18 + SQLite | 331 passed, 7 skipped (cada una) |
| Cotas inferiores (`{'sqlalchemy': '2.0.36', 'pydantic': '2.9.0', 'asyncpg': '0.30.0', 'aiosqlite': '0.20.0', 'alembic': '1.14.0', 'fastapi': '0.115.0', 'uvicorn': '0.50.0', 'pytest': '8.3.0', 'pytest-asyncio': '0.24.0', 'httpx': '0.27.0', 'pyyaml': '6.0.2'}`) + PostgreSQL 17 | 331 passed, 7 skipped (primer intento falló al compilar `pyyaml 6.0`; corregido, ver §2 #6) |
| Solo SQLite (sin PostgreSQL) | 203 passed, 135 skipped — **no acredita PostgreSQL** |
| `pytest -m dist` | 7 passed |

### 3.3 GitHub Actions

**No ejecutado.** `tests/test_ci_contract.py` valida el YAML localmente (acciones fijadas por SHA, servicios PostgreSQL, `CCE_REQUIRE_POSTGRES=1`, artefactos con `if: always()`); eso **no** es una ejecución. Las acciones se contrastaron con su documentación y última *release* oficiales (checkout v7.0.1, setup-uv v10.3.3, upload-artifact v7.0.2; el README de setup-uv recomienda fijar por SHA).

## 4. Compatibilidad: declarada vs. acreditada

| Componente | Declarado | Acreditado (qué se ejecutó) | No acreditado |
|---|---|---|---|
| Python | `>=3.13` (abierto) | 3.13.16 (macOS), 3.13.18 (Linux) | 3.14 y posteriores: sin probar (el rango no tiene tope) |
| PostgreSQL | 15 a 18 | 15.19, 16.15, 17.11, 18.6 en macOS y Linux | 14 no se soporta (fin de soporte 12-nov-2026) |
| SQLite | `>=3.38` (comprobado en ejecución) | 3.54.0 (macOS), 3.40.1 (Linux), **3.38.5 suite completa en Linux**; 3.37.2 rechazado | 3.38.0–3.38.4 y 3.39: sin probar; 3.38.5 es el único punto del piso |
| SQLAlchemy | `>=2.0.36,<2.2` | 2.0.36 y 2.1.8 | versiones intermedias |
| asyncpg / aiosqlite / alembic / pydantic / fastapi / uvicorn / httpx | ver `pyproject.toml` | extremos inferior (`{'sqlalchemy': '2.0.36', 'pydantic': '2.9.0', 'asyncpg': '0.30.0', 'aiosqlite': '0.20.0', 'alembic': '1.14.0', 'fastapi': '0.115.0', 'uvicorn': '0.50.0', 'pytest': '8.3.0', 'pytest-asyncio': '0.24.0', 'httpx': '0.27.0'}`) y superior (SQLAlchemy 2.1.8, asyncpg 0.32.0, aiosqlite 0.22.1, alembic 1.20.1, pydantic 2.14.3, fastapi 0.162.4, uvicorn 0.54.0) | versiones intermedias |

El piso de SQLite se probó con la **misma ruta de driver del producto** (`aiosqlite` → `sqlite3` del intérprete): se compiló SQLite desde la amalgamación oficial de sqlite.org y se enlazó con `LD_LIBRARY_PATH` al módulo `sqlite3` de Python; el propio proceso de pruebas confirma `sqlite3.sqlite_version == 3.38.5` (`CCE_EXPECT_SQLITE_VERSION`, no un binario `sqlite3` externo). No se cambió el mínimo declarado.

## <a id="mediciones"></a>5. Mediciones de importación (umbrales fijados por D-33)

Metodología: `scripts/measure_distribution.py` construye el wheel, lo instala en un **venv limpio** (sin extras) y ejecuta `scripts/measure_import.py` con ese intérprete: **frío** = se borran todos los `__pycache__` de `site-packages` antes de cada muestra (el producto y sus dependencias se compilan en esa primera importación); **caliente** = procesos nuevos con bytecode disponible. No se mezclan.

| | Linux (Docker, aarch64, Py 3.13.18) | macOS (arm64, Py 3.13.16) |
|---|---|---|
| Primera importación **en frío** (n, mediana / mín. / máx.) | 5: 875.2 / 808.0 / 930.1 ms | 5: 938.8 / 860.5 / 3262.9 ms |
| Importación **en caliente** (n, mediana / mín. / máx.) | 15: 197.8 / 179.5 / 228.7 ms | 15: 231.3 / 223.3 / 324.1 ms |
| Tamaño instalado del entorno base (sin `__pycache__`) | 28126 KiB | 17960 KiB |
| Dependencias instaladas | SQLAlchemy 2.1.8, greenlet 3.5.6, pydantic 2.14.3, pydantic_core 2.50.4, annotated-types 0.8.0, typing-inspection 0.4.5, typing_extensions 4.16.0 | igual |
| Módulos de terceros cargados | annotated_types, greenlet, pydantic, pydantic_core, sqlalchemy, typing_extensions, typing_inspection (ningún FastAPI/Alembic/driver/inferencia/documentos) | igual |

Lectura honesta: **los ~200–230 ms son el costo en caliente, no un costo universal de arranque frío**. La primera importación tras instalar cuesta ~0,8–0,9 s (mediana) y en macOS apareció una muestra de 3263 ms (el outlier ya visto antes). **Criterio decidido (D-33, Q-07 cerrada):** la referencia de **300 ms aplica a la mediana en caliente** y el tamaño instalado debe ser **≤ 40 MiB**; el frío se informa **sin umbral** y el máximo no es gate. Resultado: caliente 197,8 ms (Linux) y 231,3 ms (macOS) ⇒ cumple; tamaño 27,5 MiB (Linux) y 17,5 MiB (macOS) ⇒ cumple. (El máximo caliente en macOS, 324,1 ms, supera 300 ms pero no es gate.) El gate está automatizado en `tests/test_distribution.py::test_installed_import_cost_meets_the_decided_references` y pasó en macOS y en un contenedor Linux aparte tras su incorporación; **no se ejecutó en GitHub Actions**.

## <a id="matriz-ac-trazable"></a>6. Matriz AC trazable

IDs sin cambios (no se renumeró ni se eliminó nada). «macOS» = ejecución local del §3.2; «Linux local» = Docker del §3.1 (sobre `7ffbe2a`); «GitHub Actions» = **no ejecutado** en ningún caso.

| ID | Test o procedimiento | Backend | macOS | Linux local | GitHub Actions | Evidencia / motivo de pendiente |
|---|---|---|---|---|---|---|
| AC-01 | `test_distribution::test_base_install_is_light_and_separate_from_http` | wheel en venv limpio, macOS | Pasa (`pytest -m dist`) | Pasa (`pytest -m dist`) | No ejecutado | Salida de `pytest -m dist`; lista de distribuciones instaladas afirmada |
| AC-02 | idem (extra `http`) + `test_installed_service_starts_and_stops` | wheel + `[http,sqlite]`, macOS | Pasa (`pytest -m dist`) | Pasa (`pytest -m dist`) | No ejecutado | idem |
| AC-03 | `test_postgres_extra_alone_has_no_sqlite_driver`, `test_python_host_from_installed_wheel_without_http` | wheel `[postgres]` y `[sqlite,migrations]`, macOS | Pasa (`pytest -m dist`) | Pasa (`pytest -m dist`) | No ejecutado | idem |
| AC-04 | `test_import_light::test_import_loads_no_heavy_*` + sonda en wheel instalado + `measure_import.py` | macOS | Pasa | Pasa | No ejecutado | §4. Linux/macOS: ver §5 (importación en frío y en caliente medidas por separado) |
| AC-05 | `test_import_has_no_external_effects`, `test_package_logger_is_untouched_*` | macOS | Pasa | Pasa | No ejecutado | sondea sockets, archivos, hilos, lecturas de entorno, logging |
| AC-06 | `test_core_does_not_import_http_cli_migrations_fastapi_alembic[*]` | — | Pasa | Pasa | No ejecutado | análisis AST por módulo |
| AC-07 | `test_distribution::test_python_example_from_installed_wheel_runs_on_sqlite_and_postgres` y `test_examples::test_python_host_example` | SQLite y PostgreSQL 17 (Linux y macOS) | Pasa | Pasa | No ejecutado | El wheel se instala en un venv limpio **sin FastAPI/Uvicorn**, fuera del árbol, y ejecuta `examples/python_host.py` en ambos backends; se comprueba que PostgreSQL contiene el schema `cawbase_content_engine`. **Cierra el parcial anterior.** El ejemplo se corrigió para aceptar una URL y no llamar `install_transaction_control` en PostgreSQL |
| AC-10 | `test_url_mode_start_and_close_dispose_pool` | PG, SQ | Pasa | Pasa | No ejecutado | con SQLAlchemy 2.0 + aiosqlite el pool es `NullPool` (sin contabilidad `checkedout`) |
| AC-11 | `test_injected_engine_is_never_closed` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-12 | `TestConfig::*` | — | Pasa | Pasa | No ejecutado | — |
| AC-13 | `test_secret_never_in_repr_or_errors`, `test_failures_never_print_the_connection_url`, `test_public_and_authenticated_diagnostics_leak_no_infrastructure` | PG, SQ, proceso real | Pasa | Pasa | No ejecutado | — |
| AC-14 | `TestOwnTransactions::*` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-15 | `test_commit_confirms_both`, `test_host_rollback_reverts_the_engines_work_after_savepoint_release` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-16 | `test_recoverable_error_is_contained_and_host_can_continue` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-17 | `test_inactive_transaction_is_rejected`, `test_transaction_ended_before_use`, `test_connection_from_another_engine_and_url_mode_are_incompatible` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-18 | `test_commit_confirms_both` (no visible hasta el commit del host) | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-19 | `test_concurrent_tasks_are_independent` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-20 | `test_creates_only_engine_objects_and_preserves_foreign_ones`, `test_foreign_objects_inside_the_engine_namespace_are_refused`, spikes | PG, SQ | Pasa | Pasa | No ejecutado | incluye tabla, índice, vista y tabla "parecida al prefijo" ajenos |
| AC-21 | `TestProductChainUpgrades::test_update_preserves_data_and_foreign_objects` | PG, SQ | Pasa | Pasa | No ejecutado | revisión `0002` de prueba (no enviada) sobre la cadena del producto |
| AC-22 | `test_repeating_is_a_no_op` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-23 | `test_concurrent_startups_apply_once` (5 tareas), `test_parallel_migrate_processes_apply_the_sequence_once` (4 procesos), spike | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-24 | `test_several_pending_revisions_with_intermediate_failure_roll_back_as_one`, spike | PG, SQ | Pasa | Pasa | No ejecutado | falla la 4.ª revisión: ninguna de 0001–0003 ni el historial sobreviven |
| AC-25 | `test_lock_wait_is_bounded`, `test_migration_timeout_exits_nonzero_and_never_listens` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-26 | `TestLockAndPermissions::test_sqlite_readonly_database_gets_exactly_the_permission_error` (`mode=ro`), `test_sqlite_file_without_write_permission_*`, `test_postgres_role_without_ddl_permission_*`, `test_separate_ddl_credentials_for_the_launcher_postgres` | PG, SQ | Pasa | Pasa (el test por `chmod` se omite como root: lo cubre `mode=ro`) | No ejecutado | **Cierra el parcial anterior.** El fixture se verifica como solo lectura; se exige exactamente `MigrationPermissionError` (`migration_permission_denied`, mensaje genérico), archivo con el mismo hash, datos ajenos intactos, sin ruta ni texto del driver, CLI con salida 3. El test previo (`chmod` sobre un archivo vacío, aserción permisiva) **fallaba como root en Linux**: era un defecto del test; la implementación ya traducía bien el error |
| AC-27 | `test_incompatible_schema_levels`, `test_newer_schema_with_compatible_runtime_is_ready`, `test_incompatible_schema_exits_4` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-28 | `test_start_on_empty_database_fails_without_migrating`, `test_embedded_start_and_router_never_migrate` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-29 | `test_migrates_then_listens_and_serves_authenticated_requests`, `test_nothing_listens_until_preparation_completes` | proceso real, PG, SQ | Pasa | Pasa | No ejecutado | el puerto rechaza conexiones durante ≈ 5 s de preparación bloqueada |
| AC-30 | `test_sqlite_single_writer_surfaces_a_retryable_error` | SQ | Pasa | Pasa | No ejecutado | error `retryable`; los lectores continúan en WAL |
| AC-40 | `TestIsolation::test_two_scopes_cannot_cross` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-41 | `test_permissions_are_enforced`, `test_context_from_another_engine_instance_is_rejected` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-42 | `test_admin_and_scope_operations_are_separate` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-43 | `test_authorized_access_and_cross_access_denied` | PG, SQ (ASGI en memoria) | Pasa | Pasa | No ejecutado | 404 byte a byte idéntico (excepto `request_id`) |
| AC-44 | idem | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-45 | `test_all_invalid_credentials_are_indistinguishable`, `test_every_invalid_credential_gives_the_same_error`, `test_unknown_key_still_goes_through_constant_time_comparison` | PG, SQ | Pasa | Pasa | No ejecutado | no se afirma ni mide igualdad de tiempos |
| AC-46 | `TestInitialBootstrap::*`, CLI | PG, SQ, proceso | Pasa | Pasa | No ejecutado | — |
| AC-47 | `test_secret_presented_once_and_never_stored` | PG, SQ | Pasa | Pasa | No ejecutado | revisa todas las tablas y los listados |
| AC-48 | `test_revocation_*`, `test_disabling_a_client_invalidates_its_keys` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-49 | ver AC-90 a AC-94 | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-50 | `test_no_secrets_in_responses_logs_or_errors`, `test_failures_never_print_the_connection_url`, `test_delivery_failure_after_commit_never_leaks_the_secret`, logs del servicio real | PG, SQ, proceso | Pasa | Pasa | No ejecutado | — |
| AC-51 | Todas las pruebas Python usan contextos del host, sin API key; ejemplo | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-60 | `TestAdminFlow::*`, `TestValidationAndLimits::*` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-61 | `test_router_registers_nothing_by_itself_and_never_migrates`, `test_there_is_no_http_bootstrap_surface` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-62 | `test_service_app_health_and_gating`, `test_database_going_away_reports_degraded`, `test_readiness_reports_degraded_when_the_database_stops_answering` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-63 | `test_public_and_authenticated_diagnostics_leak_no_infrastructure` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-64 | `test_every_error_code_has_the_documented_http_status`, `test_internal_errors_are_sanitized`, tests HTTP | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-65 | `test_operational_limits`, `test_active_keys_per_client_limit`, `test_body_limit_counts_actual_bytes_not_content_length` | PG, SQ | Pasa | Pasa | No ejecutado | el tope del cuerpo cuenta bytes recibidos (también sin `Content-Length`) |
| AC-70 | `test_idempotent_by_scope_id_*`, `test_concurrent_creation_makes_one_row`, `test_client_creation_idempotent_*` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-71 | `TestIdempotentIssuance::*` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-72 | `TestStrictOptimisticConcurrency::*`, `test_etag_if_match_and_412_428` | PG, SQ | Pasa | Pasa | No ejecutado | D-30 |
| AC-73 | `test_repeated_state_changes_and_revocations_write_no_new_events` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-74 | `test_sqlite_single_writer_*`, `test_postgres_concurrent_writers_do_not_block_each_other` | PG / SQ por separado | Pasa | Pasa | No ejecutado | — |
| AC-75 | `TestAdminEvents::*` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-80 | `test_host_rollback_reverts_the_engines_work_after_savepoint_release` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-81 | `test_recoverable_error_*`, `test_db_level_error_does_not_poison_the_host_transaction` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-82 | `test_lost_connection_yields_transaction_broken`, `test_failing_savepoint_rollback_*`, `test_aborted_postgres_transaction_is_reported_broken` | PG, SQ | Pasa | Pasa | No ejecutado | conexión cortada con `pg_terminate_backend` (PG) o cerrando el driver (SQLite) |
| AC-83 | `test_two_wrappers_of_the_same_connection_cannot_run_simultaneously` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-84 | `test_sqlite_legacy_transaction_control_is_rejected_without_side_effects`, spike | SQ | Pasa | Pasa | No ejecutado | — |
| AC-85 | `test_sqlite_injected_engine_without_real_begin_is_rejected`, `test_postgres_injected_engine_with_wrong_isolation_is_rejected`, `test_injected_sqlite_engine_must_be_prepared_and_is_not_modified` | PG, SQ | Pasa | Pasa | No ejecutado | el engine del host no se modifica (listeners) |
| AC-86 | `test_no_intermediate_commit_is_observable`, `test_several_pending_revisions_*` | PG (`xmin`), SQ (segunda conexión) | Pasa | Pasa | No ejecutado | — |
| AC-87 | spike Alembic (`test_intermediate_failure_rolls_back_everything`) | PG, SQ | Pasa | Pasa | No ejecutado | Alembic no confirma de forma intermedia |
| AC-88 | `test_creates_only_engine_objects_*`, `test_foreign_objects_are_preserved` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-90 | `TestDelegation::test_delegation_within_own_grants_is_allowed`, `test_grants_write_allows_any_existing_scope_only`, `test_issuing_requires_clients_write` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-91 | `test_administrative_escalation_is_denied`, `test_delegation_over_http` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-92 | `test_scope_not_held_is_denied_without_grants_write`, `test_delegation_over_http` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-93 | `test_grants_write_allows_any_existing_scope_only`, `test_catalog_has_eight_permissions_and_no_wildcards` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-94 | `test_catalog_has_eight_permissions_and_no_wildcards`, `GET /v1/auth/self` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-95 | `TestRecovery::test_recovers_when_all_admin_keys_are_*`, `test_client_name_collisions_are_resolved`, `test_not_allowed_before_bootstrap_*` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-96 | `test_concurrent_recoveries_and_bootstrap_are_serialized`, `test_concurrent_bootstraps_*` | PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-97 | `TestSecretDelivery::*`, CLI `--secret-file` | PG, SQ, proceso | Pasa | Pasa | No ejecutado | permisos `0600`, `O_EXCL`, limpieza ante rollback |
| AC-98 | `test_orphaned_key_procedure_after_lost_output`, `test_delivery_failure_after_commit_*` | PG, SQ | Pasa | Pasa | No ejecutado | procedimiento recuperar → identificar → revocar |
| AC-99 | `TestKeyFormat::*`, `test_revocation_applies_to_subsequent_authentications_only` | — / PG, SQ | Pasa | Pasa | No ejecutado | vectores de codificación canónica y hash de bytes crudos |
| AC-100 | `test_migration_timeout_exits_nonzero_and_never_listens`, `test_external_migration_mode_requires_a_compatible_schema`, `test_incompatible_schema_exits_4` | proceso real, PG, SQ | Pasa | Pasa | No ejecutado | — |
| AC-101 | `TestWorkerFailures::*` (10 casos con proceso real: `service_wrapper.py`, `SIGKILL`), `test_two_workers_migrate_once_and_workers_never_migrate`, `test_launcher_failure_before_listening_is_a_different_event` | PG, SQ | Pasa | Pasa | No ejecutado | **Cierra el parcial anterior.** Fallo de inicialización de un worker ⇒ el servicio termina con 4 (antes devolvía 0 con varios workers: defecto corregido); muerte posterior ⇒ el supervisor lo reinicia sin migrar; reemplazo que no inicializa ⇒ el servicio termina con 4. Semántica del supervisor: Uvicorn ≥ 0.50 (cota elevada). Política decidida: D-32 (lo observado; sin límite ni backoff de reinicios, a cargo del orquestador) |
| AC-102 | `test_separate_ddl_credentials_for_the_launcher_postgres`, `test_postgres_role_without_ddl_permission_*` | PG | Pasa | Pasa | No ejecutado | no se afirma borrado de credenciales de la memoria del proceso (documentado) |

## 7. Pendientes y limitaciones

**Pendiente técnico (impide declarar que «solo falta aceptación»):**
1. **Ejecución del workflow en GitHub Actions** (Linux): no hay ninguna ejecución remota; requiere autorización de push.
2. ~~Q-06 y Q-07~~ **Resueltas por el usuario** (D-32: política de workers = lo observado con Uvicorn ≥ 0.50; D-33: gate = mediana en caliente ≤ 300 ms y ≤ 40 MiB). Ya no son pendientes.

**Pendientes de aceptación:** acta del usuario (este informe no la sustituye).

**Límites declarados, no bloqueantes:**
- Python 3.14+ sin probar (rango abierto); versiones intermedias de dependencias y de SQLite (3.38.0–3.38.4, 3.39) sin probar.
- Mediciones solo en aarch64 (macOS y Linux sobre Docker/VM); sin x86_64.
- Uvicorn < 0.50 queda excluido por su política de reinicio; la política de reinicios tras una muerte posterior (sin límite ni *backoff*) es la de Uvicorn y fue **aceptada por el usuario** (D-32).
- Métricas no implementadas; limitación de tasa delegada al despliegue; sin caché de autenticación ni `last_used_at`.
- Cerrar la conexión DDL no elimina credenciales de la memoria/entorno del proceso; para separación estricta, migración externa.
- El contexto Python (`trusted_*`) no es infalsificable.
- Licencia, publicación y nombre en PyPI siguen pendientes (Q-01); no se declara licencia.
- Purga de `admin_events` y borrado físico no existen (Q-04). Una migración SQLite bloquea a los demás escritores del archivo.
- `ruff format` formatea bloques de código de los `.md` si se aplica a `.`: se aplica solo a `src tests examples scripts`.

## 8. Cómo reproducir

Ver [desarrollo §2, §4 y §7](development.md). Linux: `SNAPSHOT=head bash scripts/linux_docker_validate.sh DIR`; piso de SQLite: `SNAPSHOT=head bash scripts/sqlite_floor_validate.sh DIR`.
