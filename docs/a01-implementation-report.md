# A01 — Informe de implementación y validación

> **Estado: A01 implementado en `0.1.0` y probado localmente en macOS. No probado en Linux ni en GitHub Actions. No aceptado por el usuario. La feature NO está cerrada.** Este informe no es un acta de aceptación. Todas las cifras de esta página provienen de ejecuciones reales hechas durante esta implementación; lo que no se ejecutó se dice explícitamente.

Relacionado: [especificación](features/A01-base-del-producto-instalacion-independiente.md) · [decisiones](decisions/A01-decisions.md) · [desarrollo](development.md).

## 1. Qué se implementó

- Paquete `cawbase-content-engine` (import `cawbase_content_engine`, `0.1.0`), núcleo liviano (SQLAlchemy Core asíncrono + Pydantic) y extras `postgres`, `sqlite`, `migrations`, `http`.
- Configuración por URL **o** engine inyectado (excluyentes), inicialización/cierre explícitos con ownership correcto, sin efectos al importar.
- PostgreSQL (schema `cawbase_content_engine`) y SQLite (prefijo `cawbase_content_engine_`), con tablas e historial propios y migración inicial `0001`.
- Transacciones propias y del host (`HostTransaction`, savepoint por operación, `transaction_broken`, guarda por conexión subyacente, precondiciones de SQLite y del engine inyectado).
- Migraciones: una transacción y un bloqueo para toda la secuencia pendiente (advisory lock transaccional / `BEGIN IMMEDIATE`), plan calculado tras el bloqueo, sin commits intermedios.
- `Scope` mínimo, aislamiento por contexto, 8 permisos, delegación (`admin:grants:write`), control optimista estricto (D-30), clientes, API keys (formato, hash, presentación única, idempotencia, revocación), eventos administrativos.
- Bootstrap inicial y recuperación administrativa locales (sin HTTP), con entrega a terminal o a archivo exclusivo `0600`.
- Adaptador HTTP (`create_router` montable, que no migra) y servicio independiente (validar → migrar → verificar con credenciales de runtime → recién entonces escuchar), CLI, `/livez` y `/readyz`, diagnóstico autenticado.
- Pruebas, ejemplos reproducibles, `uv.lock`, empaquetado (wheel + sdist) y workflow de GitHub Actions (**no ejecutado**).

No se implementó nada de A02+ (perfiles, categorías, clasificación, inferencia, documentos), ni OIDC/usuarios, ni integración con CAWbase, ni AWS, ni publicación.

## 2. Verificaciones técnicas previas (spikes, mantenidas como tests)

| Incertidumbre | Resultado | Test |
|---|---|---|
| SQLite: `BEGIN` real, savepoints y rollback exterior | **Reproducido el riesgo**: en modo legado el driver no tiene `BEGIN` real antes del primer DML; un `SAVEPOINT` inicial abre su propia transacción y su `RELEASE` confirma, así que el rollback exterior no revierte los datos. Con `isolation_level=None` y un evento `begin` que emite `BEGIN`/`BEGIN IMMEDIATE` funciona. El motor comprueba `in_transaction` en el driver (no el estado lógico de SQLAlchemy) y rechaza el modo legado | `tests/test_spike_sqlite.py` |
| Alembic: varias revisiones en una única transacción exterior | **Funciona** en PostgreSQL y SQLite con `EnvironmentContext` + conexión exterior + `transaction_per_migration=False`: el fallo de la 3.ª revisión deja 0 tablas y 0 historial; dos migradores concurrentes ⇒ uno aplica, el otro relee y no hace nada | `tests/test_spike_alembic.py`, `tests/test_remaining_acceptance.py` |

## 3. Checks ejecutados y resultados reales

Entorno: macOS 27.0.1 arm64, Python 3.13.16, uv 0.13.1, ruff 0.17.4, mypy 2.4.1, pytest 9.2.0. PostgreSQL en contenedores Docker dedicados y descartables (`postgres:15-alpine` = 15.19, `postgres:17-alpine` = 17.11, `postgres:18-alpine` = 18.6; puertos 55415/55417/55418, solo loopback). SQLite 3.54.0 (el enlazado por el Python de la plataforma). **No se usó ninguna base existente**; cada test crea y elimina su propia base `cce_t_<uuid>`.

| Check | Resultado |
|---|---|
| `ruff check .` | `All checks passed!` |
| `ruff format --check src tests examples scripts` | `53 files already formatted` |
| `mypy` (estricto, 34 archivos fuente) | `Success: no issues found in 34 source files` |
| Suite completa, PostgreSQL 15 + SQLite | 312 passed, 6 skipped, 6 deselected in 82.97s (0:01:22) |
| Suite completa, PostgreSQL 17 + SQLite | 312 passed, 6 skipped, 6 deselected in 82.36s (0:01:22) |
| Suite completa, PostgreSQL 18 + SQLite | 312 passed, 6 skipped, 6 deselected in 83.65s (0:01:23) |
| Suite con cotas inferiores declaradas (`--resolution lowest-direct`: SQLAlchemy 2.0.36, FastAPI 0.115.0, aiosqlite 0.20.0, asyncpg 0.30.0, alembic 1.14.0, pydantic 2.9.0, uvicorn 0.32.0), PostgreSQL 17 + SQLite | 312 passed, 6 skipped, 6 deselected in 87.78s (0:01:27) |
| Suite solo SQLite (sin `CCE_TEST_POSTGRES_URL`) | 189 passed, 129 skipped, 6 deselected in 34.10s (las omisiones son las variantes PostgreSQL y las pruebas inaplicables al backend) |
| `pytest -m dist` (wheel + sdist, entornos limpios fuera del árbol) | 6 passed, 318 deselected in 25.79s |
| Ejemplos (`tests/test_examples.py`) | incluidos en las suites anteriores |

Versiones «altas» usadas en las corridas normales: SQLAlchemy 2.1.8, FastAPI 0.162.4, aiosqlite 0.22.1, asyncpg 0.32.0, alembic 1.20.1, pydantic 2.14.3, uvicorn 0.54.0.

**Sobre los *skips* (6 en una corrida con PostgreSQL):** son pruebas que no aplican al backend parametrizado (p. ej. «solo PostgreSQL» ejecutándose con SQLite). Con `CCE_REQUIRE_POSTGRES=1` una base PostgreSQL ausente es un **fallo**, no un skip. Sin PostgreSQL (corrida «solo SQLite») se omiten todas las variantes PostgreSQL: esa corrida **no** acredita PostgreSQL.

**Rangos soportados y lo realmente probado:**

| Componente | Declarado | Probado |
|---|---|---|
| Python | ≥ 3.13 | 3.13.16 |
| PostgreSQL | 15 a 18 | 15.19, 17.11, 18.6 (no 16) |
| SQLite | ≥ 3.38 (comprobado en ejecución) | **solo 3.54.0**; el piso 3.38 **no** se ejecutó |
| SQLAlchemy | 2.0.36 a < 2.2 | 2.0.36 y 2.1.8 (extremos; no versiones intermedias) |
| asyncpg / aiosqlite / alembic / pydantic / fastapi / uvicorn | ver `pyproject.toml` | extremos inferior y superior de la tabla de arriba |

## <a id="mediciones"></a>4. Mediciones

Metodología: `scripts/measure_import.py --runs 15` ejecuta `python -I -c "import cawbase_content_engine"` en **procesos nuevos** y registra la mediana; tamaño = `du -sk` de `site-packages` de un venv recién creado con `uv venv` e instalado desde el wheel. Plataforma: macOS 27.0.1 arm64, Python 3.13.16. **No hay mediciones en Linux ni en CI**.

| Medición | Resultado (macOS) | Referencia provisional | Estado |
|---|---|---|---|
| Importación (mediana, 15 procesos) | 210,0 (mín. 206,5; máx. 254,3) ms | 300 ms | por debajo en esta máquina; sin evidencia en Linux; la referencia **no** se modificó |
| `site-packages` del entorno base (wheel sin extras) | 19 212 KiB (≈ 18,8 MiB) | 40 MiB | por debajo en esta máquina |
| Entorno `[sqlite,migrations]` / `[postgres]` / `[http,postgres]` | 21 112 / 21 688 / 27 292 KiB | — | informativo |
| Dependencias instaladas del base | SQLAlchemy 2.1.8, greenlet 3.5.6, pydantic 2.14.3, pydantic_core 2.50.4, annotated-types 0.8.0, typing-inspection 0.4.5, typing_extensions 4.16.0 | solo las declaradas | coincide |
| Módulos de terceros cargados al importar | annotated_types, greenlet, pydantic, pydantic_core, sqlalchemy, typing_extensions, typing_inspection (ningún FastAPI/Alembic/driver/inferencia/documentos) | cero prohibidos | cumple (`tests/test_import_light.py`) |

La primera importación de un venv recién creado puede incluir compilación de bytecode (una corrida aislada de ≈ 2,9 s en un venv nuevo); la mediana descarta ese outlier, pero el máximo se informa en el JSON de `measure_import.py`.

## <a id="matriz-ac-trazable"></a>5. Matriz AC trazable

«Local» = macOS, ejecutado en esta sesión (PostgreSQL 15/17/18 y SQLite salvo que se indique). **Ninguna fila está acreditada en Linux/GitHub Actions ni aceptada por el usuario.** Los IDs son los de la especificación (no se renumeraron ni eliminaron).

| ID | Test o procedimiento | Backend/entorno | Resultado | Evidencia / motivo de pendiente |
|---|---|---|---|---|
| AC-01 | `test_distribution::test_base_install_is_light_and_separate_from_http` | wheel en venv limpio, macOS | Pasa | Salida de `pytest -m dist`; lista de distribuciones instaladas afirmada; Linux pendiente |
| AC-02 | idem (extra `http`) + `test_installed_service_starts_and_stops` | wheel + `[http,sqlite]`, macOS | Pasa | idem |
| AC-03 | `test_postgres_extra_alone_has_no_sqlite_driver`, `test_python_host_from_installed_wheel_without_http` | wheel `[postgres]` y `[sqlite,migrations]`, macOS | Pasa | idem |
| AC-04 | `test_import_light::test_import_loads_no_heavy_*` + sonda en wheel instalado + `measure_import.py` | macOS | Pasa | §4; Linux pendiente |
| AC-05 | `test_import_has_no_external_effects`, `test_package_logger_is_untouched_*` | macOS | Pasa | sondea sockets, archivos, hilos, lecturas de entorno, logging |
| AC-06 | `test_core_does_not_import_http_cli_migrations_fastapi_alembic[*]` | — | Pasa | análisis AST por módulo |
| AC-07 | `test_examples::test_python_host_example`, host desde wheel instalado | **SQLite**; PostgreSQL solo vía la suite | Parcial | El ejemplo se ejecutó con SQLite (también desde el wheel instalado); con PostgreSQL el mismo flujo está cubierto por las pruebas de integración, **no** por el ejemplo |
| AC-10 | `test_url_mode_start_and_close_dispose_pool` | PG, SQ | Pasa | con SQLAlchemy 2.0 + aiosqlite el pool es `NullPool` (sin contabilidad `checkedout`) |
| AC-11 | `test_injected_engine_is_never_closed` | PG, SQ | Pasa | |
| AC-12 | `TestConfig::*` | — | Pasa | |
| AC-13 | `test_secret_never_in_repr_or_errors`, `test_failures_never_print_the_connection_url`, `test_public_and_authenticated_diagnostics_leak_no_infrastructure` | PG, SQ, proceso real | Pasa | |
| AC-14 | `TestOwnTransactions::*` | PG, SQ | Pasa | |
| AC-15 | `test_commit_confirms_both`, `test_host_rollback_reverts_the_engines_work_after_savepoint_release` | PG, SQ | Pasa | |
| AC-16 | `test_recoverable_error_is_contained_and_host_can_continue` | PG, SQ | Pasa | |
| AC-17 | `test_inactive_transaction_is_rejected`, `test_transaction_ended_before_use`, `test_connection_from_another_engine_and_url_mode_are_incompatible` | PG, SQ | Pasa | |
| AC-18 | `test_commit_confirms_both` (no visible hasta el commit del host) | PG, SQ | Pasa | |
| AC-19 | `test_concurrent_tasks_are_independent` | PG, SQ | Pasa | |
| AC-20 | `test_creates_only_engine_objects_and_preserves_foreign_ones`, `test_foreign_objects_inside_the_engine_namespace_are_refused`, spikes | PG, SQ | Pasa | incluye tabla, índice, vista y tabla "parecida al prefijo" ajenos |
| AC-21 | `TestProductChainUpgrades::test_update_preserves_data_and_foreign_objects` | PG, SQ | Pasa | revisión `0002` de prueba (no enviada) sobre la cadena del producto |
| AC-22 | `test_repeating_is_a_no_op` | PG, SQ | Pasa | |
| AC-23 | `test_concurrent_startups_apply_once` (5 tareas), `test_parallel_migrate_processes_apply_the_sequence_once` (4 procesos), spike | PG, SQ | Pasa | |
| AC-24 | `test_several_pending_revisions_with_intermediate_failure_roll_back_as_one`, spike | PG, SQ | Pasa | falla la 4.ª revisión: ninguna de 0001–0003 ni el historial sobreviven |
| AC-25 | `test_lock_wait_is_bounded`, `test_migration_timeout_exits_nonzero_and_never_listens` | PG, SQ | Pasa | |
| AC-26 | `test_postgres_role_without_ddl_permission_*`, `test_separate_ddl_credentials_for_the_launcher_postgres`, `test_sqlite_readonly_database_*` | PG; SQLite solo solo-lectura | Pasa (PG) / **débil** (SQLite) | La aserción de SQLite de solo-lectura admite varios tipos de error; no distingue con precisión `migration_permission_denied` |
| AC-27 | `test_incompatible_schema_levels`, `test_newer_schema_with_compatible_runtime_is_ready`, `test_incompatible_schema_exits_4` | PG, SQ | Pasa | |
| AC-28 | `test_start_on_empty_database_fails_without_migrating`, `test_embedded_start_and_router_never_migrate` | PG, SQ | Pasa | |
| AC-29 | `test_migrates_then_listens_and_serves_authenticated_requests`, `test_nothing_listens_until_preparation_completes` | proceso real, PG, SQ | Pasa | el puerto rechaza conexiones durante ≈ 5 s de preparación bloqueada |
| AC-30 | `test_sqlite_single_writer_surfaces_a_retryable_error` | SQ | Pasa | error `retryable`; los lectores continúan en WAL |
| AC-40 | `TestIsolation::test_two_scopes_cannot_cross` | PG, SQ | Pasa | |
| AC-41 | `test_permissions_are_enforced`, `test_context_from_another_engine_instance_is_rejected` | PG, SQ | Pasa | |
| AC-42 | `test_admin_and_scope_operations_are_separate` | PG, SQ | Pasa | |
| AC-43 | `test_authorized_access_and_cross_access_denied` | PG, SQ (ASGI en memoria) | Pasa | 404 byte a byte idéntico (excepto `request_id`) |
| AC-44 | idem | PG, SQ | Pasa | |
| AC-45 | `test_all_invalid_credentials_are_indistinguishable`, `test_every_invalid_credential_gives_the_same_error`, `test_unknown_key_still_goes_through_constant_time_comparison` | PG, SQ | Pasa | no se afirma ni mide igualdad de tiempos |
| AC-46 | `TestInitialBootstrap::*`, CLI | PG, SQ, proceso | Pasa | |
| AC-47 | `test_secret_presented_once_and_never_stored` | PG, SQ | Pasa | revisa todas las tablas y los listados |
| AC-48 | `test_revocation_*`, `test_disabling_a_client_invalidates_its_keys` | PG, SQ | Pasa | |
| AC-49 | ver AC-90 a AC-94 | PG, SQ | Pasa | |
| AC-50 | `test_no_secrets_in_responses_logs_or_errors`, `test_failures_never_print_the_connection_url`, `test_delivery_failure_after_commit_never_leaks_the_secret`, logs del servicio real | PG, SQ, proceso | Pasa | |
| AC-51 | Todas las pruebas Python usan contextos del host, sin API key; ejemplo | PG, SQ | Pasa | |
| AC-60 | `TestAdminFlow::*`, `TestValidationAndLimits::*` | PG, SQ | Pasa | |
| AC-61 | `test_router_registers_nothing_by_itself_and_never_migrates`, `test_there_is_no_http_bootstrap_surface` | PG, SQ | Pasa | |
| AC-62 | `test_service_app_health_and_gating`, `test_database_going_away_reports_degraded`, `test_readiness_reports_degraded_when_the_database_stops_answering` | PG, SQ | Pasa | |
| AC-63 | `test_public_and_authenticated_diagnostics_leak_no_infrastructure` | PG, SQ | Pasa | |
| AC-64 | `test_every_error_code_has_the_documented_http_status`, `test_internal_errors_are_sanitized`, tests HTTP | PG, SQ | Pasa | |
| AC-65 | `test_operational_limits`, `test_active_keys_per_client_limit`, `test_body_limit_counts_actual_bytes_not_content_length` | PG, SQ | Pasa | el tope del cuerpo cuenta bytes recibidos (también sin `Content-Length`) |
| AC-70 | `test_idempotent_by_scope_id_*`, `test_concurrent_creation_makes_one_row`, `test_client_creation_idempotent_*` | PG, SQ | Pasa | |
| AC-71 | `TestIdempotentIssuance::*` | PG, SQ | Pasa | |
| AC-72 | `TestStrictOptimisticConcurrency::*`, `test_etag_if_match_and_412_428` | PG, SQ | Pasa | D-30 |
| AC-73 | `test_repeated_state_changes_and_revocations_write_no_new_events` | PG, SQ | Pasa | |
| AC-74 | `test_sqlite_single_writer_*`, `test_postgres_concurrent_writers_do_not_block_each_other` | PG / SQ por separado | Pasa | |
| AC-75 | `TestAdminEvents::*` | PG, SQ | Pasa | |
| AC-80 | `test_host_rollback_reverts_the_engines_work_after_savepoint_release` | PG, SQ | Pasa | |
| AC-81 | `test_recoverable_error_*`, `test_db_level_error_does_not_poison_the_host_transaction` | PG, SQ | Pasa | |
| AC-82 | `test_lost_connection_yields_transaction_broken`, `test_failing_savepoint_rollback_*`, `test_aborted_postgres_transaction_is_reported_broken` | PG, SQ | Pasa | conexión cortada con `pg_terminate_backend` (PG) o cerrando el driver (SQLite) |
| AC-83 | `test_two_wrappers_of_the_same_connection_cannot_run_simultaneously` | PG, SQ | Pasa | |
| AC-84 | `test_sqlite_legacy_transaction_control_is_rejected_without_side_effects`, spike | SQ | Pasa | |
| AC-85 | `test_sqlite_injected_engine_without_real_begin_is_rejected`, `test_postgres_injected_engine_with_wrong_isolation_is_rejected`, `test_injected_sqlite_engine_must_be_prepared_and_is_not_modified` | PG, SQ | Pasa | el engine del host no se modifica (listeners) |
| AC-86 | `test_no_intermediate_commit_is_observable`, `test_several_pending_revisions_*` | PG (`xmin`), SQ (segunda conexión) | Pasa | |
| AC-87 | spike Alembic (`test_intermediate_failure_rolls_back_everything`) | PG, SQ | Pasa | Alembic no confirma de forma intermedia |
| AC-88 | `test_creates_only_engine_objects_*`, `test_foreign_objects_are_preserved` | PG, SQ | Pasa | |
| AC-90 | `TestDelegation::test_delegation_within_own_grants_is_allowed`, `test_grants_write_allows_any_existing_scope_only`, `test_issuing_requires_clients_write` | PG, SQ | Pasa | |
| AC-91 | `test_administrative_escalation_is_denied`, `test_delegation_over_http` | PG, SQ | Pasa | |
| AC-92 | `test_scope_not_held_is_denied_without_grants_write`, `test_delegation_over_http` | PG, SQ | Pasa | |
| AC-93 | `test_grants_write_allows_any_existing_scope_only`, `test_catalog_has_eight_permissions_and_no_wildcards` | PG, SQ | Pasa | |
| AC-94 | `test_catalog_has_eight_permissions_and_no_wildcards`, `GET /v1/auth/self` | PG, SQ | Pasa | |
| AC-95 | `TestRecovery::test_recovers_when_all_admin_keys_are_*`, `test_client_name_collisions_are_resolved`, `test_not_allowed_before_bootstrap_*` | PG, SQ | Pasa | |
| AC-96 | `test_concurrent_recoveries_and_bootstrap_are_serialized`, `test_concurrent_bootstraps_*` | PG, SQ | Pasa | |
| AC-97 | `TestSecretDelivery::*`, CLI `--secret-file` | PG, SQ, proceso | Pasa | permisos `0600`, `O_EXCL`, limpieza ante rollback |
| AC-98 | `test_orphaned_key_procedure_after_lost_output`, `test_delivery_failure_after_commit_*` | PG, SQ | Pasa | procedimiento recuperar → identificar → revocar |
| AC-99 | `TestKeyFormat::*`, `test_revocation_applies_to_subsequent_authentications_only` | — / PG, SQ | Pasa | vectores de codificación canónica y hash de bytes crudos |
| AC-100 | `test_migration_timeout_exits_nonzero_and_never_listens`, `test_external_migration_mode_requires_a_compatible_schema`, `test_incompatible_schema_exits_4` | proceso real, PG, SQ | Pasa | |
| AC-101 | `test_two_workers_migrate_once_and_workers_never_migrate` | proceso real, PG, SQ | Pasa | solo el camino feliz con 2 workers (el comportamiento de un worker que falla depende del supervisor de Uvicorn y **no** se probó) |
| AC-102 | `test_separate_ddl_credentials_for_the_launcher_postgres`, `test_postgres_role_without_ddl_permission_*` | PG | Pasa | no se afirma borrado de credenciales de la memoria del proceso (documentado) |

## 6. Pendientes y limitaciones observadas

**Pendientes (bloquean el cierre):**
1. Ejecución del workflow en GitHub Actions (Linux). No se hizo push; **ninguna cifra de este informe es de Linux ni de CI**.
2. Aceptación del usuario (acta). Este informe no la sustituye.

**Pendientes no bloqueantes / límites declarados:**
- SQLite se probó solo en 3.54.0; PostgreSQL 16 no se probó; las versiones de SQLAlchemy/etc. se probaron solo en los extremos.
- Las versiones de las acciones de GitHub (`checkout@v4`, `setup-uv@v5`) no se verificaron contra su documentación oficial.
- AC-07: el ejemplo ejecutable usa SQLite.
- AC-26 (SQLite solo-lectura) y AC-101 (worker que falla) con cobertura parcial, ver la matriz.
- Métricas no implementadas; la limitación de tasa se delega al despliegue (el motor no limita intentos de autenticación). Sin caché de autenticación ni `last_used_at`.
- Cerrar la conexión DDL no elimina credenciales de la memoria/entorno del proceso; para separación estricta, migración externa y runtime sin esas credenciales.
- El contexto Python (`trusted_*`) no es infalsificable; no protege frente a código arbitrario del mismo proceso.
- Licencia, publicación y nombre en PyPI siguen pendientes (Q-01); `pyproject.toml` no declara licencia a propósito.
- Purga de `admin_events` y borrado físico no existen (Q-04).
- Una migración SQLite bloquea a los demás escritores del archivo mientras dura.
- `ruff format` formatea los bloques de código de los `.md` si se aplica a `.`: se aplica solo a `src tests examples scripts`.

## 7. Cómo reproducir

Ver [desarrollo §2 y §4](development.md) y [instalación y operación](installation-and-operations.md).
