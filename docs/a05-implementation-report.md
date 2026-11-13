# Informe de implementación de A05 — trabajos, archivos y ciclo de vida

> **Estado: A05 está IMPLEMENTADA en `0.4.0` y probada localmente (macOS y Linux local en Docker); pendiente de revisión y aceptación del usuario. NO está cerrada ni aceptada. GitHub Actions NO se ejecutó.** Este informe no es un acta. Solo acredita ingeniería (estado, posesión, consistencia base/storage, aislamiento, retención) sobre documentos **sintéticos** y un adaptador de inferencia simulado; **no** acredita calidad de detección (A09) ni protección (A06–A08). 29 de 52 criterios están verificados, 23 parcialmente y 0 sin evidencia (matriz §3).

Relacionado: [contrato](jobs-files-and-lifecycle.md) · [especificación](features/A05-trabajos-archivos-y-ciclo-de-vida.md) · [decisiones](decisions/A05-decisions.md) · [desarrollo](development.md).

## 0. Estado por nivel de evidencia

| # | Nivel | Estado |
|---|---|---|
| 1 | Especificado | Sí (rev. 3, commit `d7ab051`) |
| 2 | Implementado | Sí, versión `0.4.0` (commit `c057b27` más la documentación de este informe) |
| 3 | Probado en macOS | Sí: suite completa (SQLite 3.54.0 y PostgreSQL real) **989 passed, 13 skipped, 0 failed** contra PostgreSQL 15.19 y **989 passed, 13 skipped, 0 failed** contra PostgreSQL 18.6; `-m dist` 9 passed; ruff, ruff format y mypy (77 archivos) sin hallazgos |
| 4 | Probado en Linux local (Docker, Debian 12) | Sí (Docker Desktop, contenedor Debian 12 aarch64, Python 3.13.18, SQLite 3.40.1, instantánea `git archive` de `2e4a88e`): suite completa **985 passed, 17 skipped, 0 failed** contra PostgreSQL 15.19, 16.15, 17.11 y 18.6; cotas inferiores declaradas (`lowest-direct`) 985 passed; piso de SQLite 3.38.5: 986 passed, 16 skipped (3.37.2 es rechazado al arrancar); `-m dist` 9 passed sobre `b21d86f`/`9431f3c` (la primera corrida de `-m dist` sobre `2e4a88e` falló 4 pruebas por constantes de versión y migración heredadas, ya corregidas); auditoría de artefactos 0 hallazgos; importación en frío 801,5 ms (sin umbral), en caliente 191,8 ms (≤ 300 ms, D-33), 28 571 KiB instalados (≤ 40 MiB) |
| 5 | Probado en GitHub Actions | **No** (no se hizo push; el workflow no se ejecutó) |
| 6 | Aceptado por el usuario | **No** |

Un *skip* no es una aprobación (ver §5). Una corrida Linux local no es una ejecución de GitHub Actions.

## 1. Qué se implementó

- **Persistencia** (`persistence/a05_schema.py`, migración `0003_jobs_files`): `stored_files`, `file_validations`, `jobs`, `job_attempts`, `job_artifacts`, `storage_tombstones`, `idempotency_records`, `scope_job_fences`, `scope_usage`, `engine_usage`; `schema_level` 3, `min_runtime_level` 1.
- **Núcleo de trabajos** (`jobs/`): `db.py` (reloj de la base, orden de bloqueos, contadores, *fence*, transiciones terminales), `ops.py` (envío idempotente, lectura, cancelación, resultados, eliminación de datos), `worker.py` (reclamación, *heartbeat*, reintentos, artefactos, publicación), `execute.py` (ejecución con A03 y análisis documental), `maintenance.py` (recuperación, limpieza, purga física y de metadatos, `admin_events`), `executor.py` (ejecutor de referencia), `files.py`, `storage.py` (disco privado con sellos y evidencia de liberación), `spool.py`, `setup.py`, `setup_env.py`.
- **Documentos** (`documents/`, extra `documents`): validación y extracción DOCX/PDF en un proceso hijo aislado (`runner.py`: `UnconfinedDevelopmentRunner`, `LinuxContainerRunner`, canario).
- **Superficies**: métodos de `ScopedOperations` (`operations/jobs_facade.py`), `engine.job_worker()`, `engine.maintenance()`, rutas HTTP (`http/jobs_routes.py`), CLI `worker`, `cleanup`, `purge-admin-events`, configuración por entorno del servicio.
- **Permisos** (17): `files:write|read`, `jobs:read|cancel`, `results:read`, `data:delete` más los 11 anteriores. **Versión** `0.4.0` (SemVer `0.x`: cambio menor por la migración y los permisos).
- **Contratos previos**: `analyze_text`, su ruta y `AnalysisResult` sin cambios (la lógica de A03 se reutiliza mediante `inference.service.execute_prepared`, extraída de `run_analysis`); `update_scope`/`set_scope_status` conservan su semántica pública y ahora reconcilian el *fence*.

## 2. Comandos y resultados

Entorno de macOS: macOS 27.0.1 arm64, Python 3.13.16, SQLite 3.54.0 (`sqlite3.sqlite_version`), SQLAlchemy 2.1.8, asyncpg 0.32.0, aiosqlite 0.22.1, lxml 6.1.3, pikepdf 10.18.1, python-docx 1.2.0, pypdf 7.1.0, Docker 29.8.2. PostgreSQL: servidores descartables `postgres:15-alpine` (**15.19**) y `postgres:18-alpine` (**18.6**) en contenedores propios (`cce-a05-pg15`, `cce-a05-pg18`); cada prueba usa su base `cce_t_*`.

```bash
uv run ruff check . && uv run ruff format --check src tests examples scripts && uv run mypy
CCE_REQUIRE_POSTGRES=1 CCE_TEST_POSTGRES_URL=postgresql+asyncpg://…@127.0.0.1:<puerto>/postgres uv run pytest -q -rs     # 15.19 y 18.6
CCE_REQUIRE_POSTGRES=1 CCE_TEST_POSTGRES_URL=… uv run pytest -m dist -q -rs                                          # wheel y sdist en venvs limpios
SNAPSHOT=head bash scripts/linux_docker_validate.sh DIR     # Linux: PostgreSQL 15, 16, 17 y 18 + cotas inferiores + dist
SNAPSHOT=head bash scripts/sqlite_floor_validate.sh DIR     # SQLite 3.38.5 (piso) y 3.37.2 (rechazo)
```

| Ambiente | Comando | Resultado |
|---|---|---|
| macOS arm64, PostgreSQL 15.19 + SQLite 3.54.0 | `pytest -q -rs` (árbol final) | **989 passed, 13 skipped, 0 failed**, 9 deselected (`dist`) |
| macOS arm64, PostgreSQL 18.6 + SQLite 3.54.0 | ídem | **989 passed, 13 skipped, 0 failed** |
| macOS | `pytest -m dist` | 9 passed |
| Linux (Docker), PostgreSQL 15.19 / 16.15 / 17.11 / 18.6 | `linux_docker_validate.sh` sobre `2e4a88e` | 985 passed, 17 skipped, 0 failed en cada uno |
| Linux, cotas inferiores declaradas (SQLAlchemy 2.0.36, pydantic 2.9.0, asyncpg 0.30.0, aiosqlite 0.20.0, alembic 1.14.0, fastapi 0.115.0, uvicorn 0.50.0) | ídem (`lowest-direct`) | 985 passed, 17 skipped |
| Linux, SQLite 3.38.5 (piso) | `sqlite_floor_validate.sh` | 986 passed, 16 skipped; 3.37.2 rechazado |
| Linux | `pytest -m dist` sobre `9431f3c` | 9 passed |
| Linux | auditoría y medición de la wheel | 0 hallazgos; caliente 191,8 ms, 28 571 KiB |

Los *skips* de macOS (13) son pruebas «solo PostgreSQL» en la variante SQLite, `CCE_EXPECT_SQLITE_VERSION` sin definir y similares: ninguna es de A05. Los de Linux (17) son esas mismas más cuatro de A05/migración: **las tres pruebas del contenedor de referencia (`test_a05_isolation`) se omiten porque dentro del contenedor de validación no hay Docker**, y una de migración (el usuario es root). En macOS esas pruebas **sí** se ejecutaron contra Docker Desktop (máquina virtual Linux).

## 3. Matriz de aceptación trazada a pruebas

«Verificado»: el criterio, tal como está redactado, está cubierto por pruebas que pasaron en macOS con PostgreSQL y SQLite reales. «Parcial»: lo indicado quedó probado y lo que falta se nombra. «Sin evidencia»: no hay prueba. Las rutas son relativas a `tests/`; los nombres abreviados con `…` están completos en el archivo.

| AC | Estado | Evidencia y límites |
|---|---|---|
| AC-A05-01 | **Verificado** | Encadenado en varias pruebas (no hay una sola prueba que una todo): carga → validación → extracción → inferencia → resultado → lectura autorizada (test_a05_jobs::test_text_analysis_end_to_end, test_a05_documents::test_docx_end_to_end…, test_a05_documents::test_pdf_end_to_end…, test_a05_files::test_upload_roundtrip…) → expiración y limpieza (test_a05_retention::test_original_retention…, test_a05_retention::test_results_expire…). Con el mock real de A03: test_a05_jobs::test_text_job_with_the_real_a03_mock_adapter. Las demás pruebas usan un adaptador determinista propio (`StubAdapter`) |
| AC-A05-02 | **Parcial** | test_a05_files::test_external_reference_is_pinned_never_deleted…, test_unstable_resolver_is_materialized. **No** se ejecutó un trabajo completo sobre una referencia externa |
| AC-A05-03 | **Parcial** | Manifiesto y oráculos escritos a mano en las pruebas; Unicode no BMP y combinante en DOCX (test_a05_documents). **No** hay pruebas de ligaduras ni Form XObject compartido en PDF ni de localización ambigua (cubiertas en A04, no repetidas) |
| AC-A05-04 | **Parcial** | Suites de A03 completas verdes en ambos backends (corridas completas); test_a05_http::test_a03_endpoint_is_unchanged…. **No** hay una comparación sistemática Python/HTTP de los resultados de A05 |
| AC-A05-05 | **Parcial** | T1–T10 ejercitadas (test_a05_jobs, test_a05_lifecycle, test_a05_faults, test_a05_retention); **no** se probó exhaustivamente que cada transición inválida sea rechazada |
| AC-A05-06 | **Verificado** | test_a05_lifecycle::test_one_claimant_wins_a_job (6 reclamadores) |
| AC-A05-07 | **Verificado** | test_a05_lifecycle::test_stale_worker_cannot_register_or_publish_after_losing_possession, test_heartbeat_never_extends… |
| AC-A05-08 | **Verificado** | test_a05_lifecycle::test_cancel_wins_over_publication_and_success_is_not_rewritten (ambos órdenes) |
| AC-A05-09 | **Verificado** | test_a05_lifecycle::test_disable_cancels…, test_published_result_survives…, test_old_runtime_disable_enable…, test_unobserved_change_is_reconciled…, test_reconciliation_is_idempotent…, test_lazy_fence…; el «runtime anterior» se emula con el mismo `UPDATE` que ejecutaban `0.1.0`–`0.3.0` (no con binarios antiguos) |
| AC-A05-10 | **Parcial** | test_a05_jobs::test_pinned_version_survives_profile_deactivation (desactivar + quitar predeterminado). **No** se probó eliminar el perfil ni publicar otra versión |
| AC-A05-11 | **Verificado** | test_a05_extra::test_revoking_a_key_stops_access_but_not_accepted_jobs |
| AC-A05-12 | **Parcial** | test_a05_jobs::test_replay_does_not_reselect…, test_concurrent_first_requests…; test_a05_lifecycle (réplica en `HostTransaction`); test_a05_retention (tras expirar y tras purgar); test_a05_files (cargas, generación). **No** se probó la respuesta perdida de un envío antes/después de aceptarse |
| AC-A05-13 | **Parcial** | test_a05_faults::test_a_worker_that_cannot_reproduce_the_pinned_configuration_never_claims (no reclama; `executor_unavailable`). `source_changed` solo se probó a nivel de lectura verificada (test_a05_files), no como fallo de un trabajo |
| AC-A05-14 | **Parcial** | test_a05_faults, test_a05_files, test_a05_lifecycle: reserva sin objeto, escritura parcial, escritor tardío, publicación con respuesta perdida, objeto corrupto, borrado fallido. **No** se inyectaron cortes de proceso en cada uno de los cinco pasos |
| AC-A05-15 | **Parcial** | test_a05_jobs::test_retry_then_success_and_exhaustion, test_deterministic_failures_are_not_retried, test_a05_faults::…storage_failure_retries…. No se aserta la curva de *backoff* ni el límite por plazo |
| AC-A05-16 | **Verificado** | test_a05_retention::test_cleanup_is_bounded_repeatable…, test_tombstone_survives…; test_a05_faults::test_failing_cleanup_never_releases_the_charge… |
| AC-A05-17 | **Verificado** | test_a05_retention::test_admission_requires_the_input_to_outlive…, test_in_flight_download_completes…; test_a05_faults::test_input_deleted_or_expired_while_running… |
| AC-A05-18 | **Verificado** | test_a05_retention::test_admin_events_purge_is_explicit_previewed…, test_the_migration_does_not_purge_events |
| AC-A05-19 | **Parcial** | test_a05_documents: bomba ZIP por bytes producidos, DOCTYPE, tipo falso, OLE, matriz DOCX/PDF por familia. **No** hay pruebas de frontera ±1 de los límites ni bomba Flate de PDF |
| AC-A05-20 | **Parcial** | test_a05_isolation: canario bloqueado en el contenedor Linux de referencia y DOCX/PDF procesados dentro (Docker Desktop local); sin *fallback* (`untrusted_input`); aislamiento no verificable ⇒ `configuration_unavailable`. El canario no mide memoria/CPU/duración; la prueba del contenedor se **omite** donde no hay Docker (p. ej. dentro de la validación Linux en Docker) |
| AC-A05-21 | **Parcial** | test_a05_documents::test_unit_larger_than_a03_limits…, test_documents_without_text…; test_a05_jobs (salida inválida ⇒ falla). **No** se probó conservar una salida válida inconclusa por unidad |
| AC-A05-22 | **Verificado** | test_a05_documents::test_docx_end_to_end_with_unicode_and_non_covered_content |
| AC-A05-23 | **Verificado** | test_a05_faults::test_concurrent_uploads_cannot_exceed_the_storage_quota; test_a05_files (reservas, sellos); test_a05_lifecycle::test_counters_stay_exact_under_concurrent_activity |
| AC-A05-24 | **Parcial** | test_a05_http: subida por stream (> 64 KiB), descarga verificada, `no-store`/`attachment`, JSON 64 KiB intacto, aislamiento por scope. **No** se probó desconexión del cliente ni el 413 por `max_file_bytes` sobre HTTP |
| AC-A05-25 | **Parcial** | test_a05_jobs::test_permissions_are_separate; test_a05_http (403/404). **No** se recorrió la matriz completa de los 17 permisos por operación |
| AC-A05-26 | **Verificado** | test_a05_extra::test_logs_errors_and_events_never_carry_content (marcador único en texto, contexto, comentarios; logs en DEBUG, errores, eventos, tombstones y metadatos) |
| AC-A05-27 | **Parcial** | tests/test_import_light.py (sin `jobs`/`documents`/`lxml`/`pikepdf` al importar). `-m dist` y el gate D-33 con la wheel: ver §2 |
| AC-A05-28 | **Parcial** | tests/test_migrations.py, test_a02_migration.py, test_a05_retention::test_the_migration_does_not_purge_events. La compatibilidad con runtimes `0.1.0`–`0.3.0` se verificó **emulando sus escrituras SQL**, no ejecutando sus binarios |
| AC-A05-29 | **Verificado** | test_a05_lifecycle: pruebas con `HostTransaction` (primer envío, divergencia, bloqueos previos del host, *rollback*, operaciones rechazadas antes de efectos) |
| AC-A05-30 | **Verificado** | test_a05_extra::test_static_boundaries_experiments_not_imported… |
| AC-A05-31 | **Parcial** | test_a05_extra::test_mixed_concurrency_never_deadlocks_between_engine_operations (carga mixta en ambos backends). No es una prueba de ausencia de interbloqueos |
| AC-A05-32 | **Verificado** | test_a05_faults::test_input_deleted_or_expired_while_running_prevents_publication (ambos órdenes) |
| AC-A05-33 | **Verificado** | test_a05_extra::test_publication_checks_every_deadline…; test_a05_lifecycle::test_cancelling_a_running_job_with_an_expired_lease…, test_heartbeat_never_extends… |
| AC-A05-34 | **Verificado** | test_a05_files::test_seal_blocks_a_suspended_writer…, test_pause_between_deadline_check_and_publish…; test_a05_retention::test_tombstone_survives… |
| AC-A05-35 | **Parcial** | test_a05_jobs, test_a05_lifecycle, test_a05_files, test_a05_extra::test_unlinked_text_objects…. No se probó la pérdida de respuesta de un envío ni la unicidad concurrente con objetos de texto perdedores |
| AC-A05-36 | **Parcial** | test_a05_extra::test_a_second_validator_configuration_gets_its_own_authoritative_result. **No** se probó el rechazo de una validación escrita por un worker obsoleto |
| AC-A05-37 | **Verificado** | test_a05_retention::test_delete_job_data_is_terminal_only_and_leaves_inputs_alone |
| AC-A05-38 | **Verificado** | test_a05_documents::test_capability_states_without_ghost_coverage; test_a05_extra::test_per_capability_states_with_one_capability_not_configured |
| AC-A05-39 | **Verificado** | test_a05_documents::test_external_hyperlink_is_inventoried_not_resolved; DOCX_CASES[external_reference]. El procesamiento corre sin red solo en el contenedor (test_a05_isolation); en el perfil de desarrollo no hay aislamiento de red |
| AC-A05-40 | **Parcial** | test_a05_documents::test_pdf_matrix_rejections (cifrado, *object streams*, `/Rotate 270`, `/Contents` en arreglo, Type0, `Tc`, incremental); test_a05_extra::test_matrix_edges…. **No** se probaron texto oblicuo ni PDF linearizado |
| AC-A05-41 | **Verificado** | test_a05_retention::test_original_retention…, test_results_expire…, test_tombstone_survives… |
| AC-A05-42 | **Verificado** | test_a05_faults::test_failing_cleanup…, test_concurrent_uploads…, test_a05_files::test_false_declared_size…, test_seal_capacity… |
| AC-A05-43 | **Verificado** | test_a05_files::test_seal_blocks_a_suspended_writer_and_release_needs_evidence |
| AC-A05-44 | **Verificado** | test_a05_files::test_pause_between_deadline_check_and_publish_cannot_materialize |
| AC-A05-45 | **Verificado** | test_a05_files::test_concurrent_cleaners_on_a_sealed_key_are_harmless (halló y corrigió un defecto, I-A05-09) |
| AC-A05-46 | **Verificado** | test_a05_files::test_seal_capacity_is_finite_and_rejects_without_removing_seals |
| AC-A05-47 | **Verificado** | test_a05_files::test_spool_sweeps_dead_owners_but_never_a_live_one (procesos reales, `SIGKILL`). La carrera arranque/barrido concurrente se evita por construcción (directorio oculto + `flock` + `rename`); **no** hay prueba de esa carrera |
| AC-A05-48 | **Parcial** | test_a05_lifecycle::test_first_submit_inside_host_transaction_creates_the_fence…. Sin conteo de conexiones del pool: la ausencia de conexión secundaria se infiere de que SQLite (un escritor) no se bloquea |
| AC-A05-49 | **Verificado** | test_a05_lifecycle::test_divergence_inside_host_transaction_is_reconciled_in_the_same_savepoint |
| AC-A05-50 | **Parcial** | test_a05_lifecycle::test_host_holding_locks_before_the_savepoint_does_not_deadlock_the_engine (orden compatible). **No** se forzó un interbloqueo real con orden incompatible |
| AC-A05-51 | **Verificado** | test_a05_lifecycle::test_host_rollback_reverts_reconciliation_and_acceptance_together |
| AC-A05-52 | **Parcial** | test_a05_lifecycle::test_counters_stay_exact…, test_a05_extra::test_mixed_concurrency…. **No** hay verificación estática del orden de bloqueo en el código |

## 4. Cambios en pruebas heredadas

Solo constantes que dependen de la migración, los permisos y la versión: cabeza `0003` y `schema_level` 3 (`test_a02_migration`, `test_migrations`, `test_processes`, `test_remaining_acceptance`, `test_examples`, `test_a03_analysis::test_no_schema_change`, `test_config_and_lifecycle`), 17 permisos (`test_credentials`, `test_a03_analysis`), versión `0.4.0` (`test_processes`), códigos de error nuevos (`test_remaining_acceptance`) y los módulos prohibidos al importar (`test_import_light`). En `test_a02_migration` el dato «A01» se escribe con SQL (el camino A05 de `update_scope` necesita las tablas de `0003`, que no existen en un esquema de nivel 1) y, en `test_remaining_acceptance`, el runtime del escenario fija su nivel requerido. Ninguna aserción de comportamiento de A01–A04 se relajó.

## 5. Límites, *skips* y lo no ejecutado

- **GitHub Actions no se ejecutó** (no hubo push ni merge); `.github/workflows/ci.yml` no cambió. Nada de lo anterior es un resultado de Actions.
- **Plataformas**: solo arm64 (macOS y Debian 12 en Docker); **no hay x86_64**. Python 3.13.x únicamente.
- **Contenedor de referencia**: probado con Docker Desktop sobre macOS (imagen `python:3.13-bookworm` + `sqlalchemy[asyncio]`, `pydantic`, `lxml`, `pikepdf`, construida por la prueba), no en un host Linux nativo ni con `gVisor`/similares. El canario comprueba red, escritura fuera del `tmpfs`, entorno sensible y límite de procesos; **no** mide memoria, CPU ni duración (el plazo y los límites duros se aplican, pero la prueba no los excede a propósito).
- **Runtimes mixtos**: se emularon con las escrituras SQL que hacían `0.1.0`–`0.3.0`; no se ejecutaron binarios antiguos.
- **Inferencia**: simulada (adaptador determinista propio en casi todas las pruebas; el mock real de A03 en una prueba de extremo a extremo). No hay evidencia de calidad de detección ni de modelo real.
- **Documentos**: solo sintéticos generados por `python-docx`/`pikepdf` (con `customXml/` retirado, I-A05-02); no se probaron archivos de Word, Acrobat ni otros generadores, ni documentos grandes, ni Type0/CID (rechazados). No se repitieron los experimentos de A04.
- **Rendimiento y capacidad**: no se midieron; los límites de [contrato §12](jobs-files-and-lifecycle.md#12-límites-operativos-e-a05-17) son elecciones iniciales. No se hicieron pruebas de frontera ±1 ni de carga sostenida.
- **Fallos**: no se inyectaron cortes de proceso en cada uno de los cinco pasos base/storage; se cubrieron fallos de storage, escritor suspendido, respuesta perdida y objetos corruptos (ver AC-A05-14).
- **HTTP**: no se probó la desconexión del cliente durante una subida.
- **Garantías que NO se prometen**: ejecución exactamente una vez (el adaptador puede invocarse más de una vez por entrada), borrado de copias de seguridad/réplicas/versiones del proveedor de storage ni de originales externos, ausencia universal de interbloqueos con `HostTransaction` o SQL ajeno, ausencia de malware (cuarentena = privado y pendiente de validación), soporte documental fuera de la matriz conservadora (en particular los DOCX con `customXml/`).
- **Defectos hallados al probar** (corregidos): `NeedReconcile`/`LeaseLost` se traducían a `internal_error` (I-A05-07); el sellado concurrente podía fallar con `PermissionError` (I-A05-09); el guard del corte de `admin_events` fallaba por microsegundos (I-A05-08); el contenedor necesitaba las dependencias base del paquete (I-A05-06); el resultado del intento recuperado se registraba como `retryable_error` en vez de `lease_lost`.

## 6. Pendientes y decisiones

- **Decisión del usuario pendiente:** la elección técnica E-A05-10 (durante un despliegue mixto, un cambio efectivo de un runtime anterior sobre un scope cancela los trabajos pendientes de ese scope) es una elección propuesta, ahora implementada y probada con las escrituras de `0.3.0`; el usuario puede preferir exigir que todos los runtimes estén actualizados antes de aceptar trabajos.
- **Aceptación:** pendiente. No se hizo push ni merge. Los criterios parciales y sin evidencia de §3 deben revisarse antes del acta.
- **Backlog:** [A05-decisions §5](decisions/A05-decisions.md#5-backlog-fuera-de-los-criterios-de-aceptación) (B-A05-01 a 14); en particular ampliar la matriz (B-A05-08, incluido `customXml/`).
