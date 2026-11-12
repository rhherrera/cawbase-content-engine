# A02 — Informe de implementación y validación

> **Estado: A02 está implementada (`0.2.0`) en la rama `feature/a02-profiles-categories-sensitive-types` (commits locales, sin push). Probada localmente en macOS y en Linux local (Docker). GitHub Actions NO se ejecutó sobre A02. NO aceptada por el usuario. La feature NO está cerrada.** Este informe no es un acta de aceptación: la aceptación corresponde al usuario. Las cifras provienen de ejecuciones reales; lo que no se ejecutó se dice explícitamente.

Relacionado: [especificación](features/A02-perfiles-categorias-y-tipos-sensibles.md) · [decisiones](decisions/A02-decisions.md) · [modelo y catálogo](profiles-and-catalog.md) · [informe de A01](a01-implementation-report.md) · [desarrollo](development.md).

## 0. Estado por nivel de evidencia

| # | Nivel | Estado |
|---|---|---|
| 1 | **Implementado** | Sí (`0.2.0`): perfiles, categorías, tipos sensibles, ranking/resolución, versiones inmutables, predeterminado, resolución, permisos `profiles:*`, migración `0002`, catálogo, Python y HTTP |
| 2 | **Probado en macOS** | Sí: ver §3.1 |
| 3 | **Probado en Linux local (Docker)** | Sí, sobre `f2bdb92`: ver §3.2 |
| 4 | **Probado en GitHub Actions** | **No.** No se hizo push; el workflow está listo para que el usuario lo active con su push manual. No se informa ningún resultado de Actions sobre A02 |
| 5 | **Aceptación del usuario** | **Pendiente.** No la emite este informe |

Una corrida Linux local en Docker **no equivale** a una ejecución de GitHub Actions. Un *skip* no es una aprobación (§3.4).

**Commits de esta entrega** (sobre `1b5ffab`, A01 aceptada): `a815edd` (cierre documental de A01), `9ecbfb6` (contrato de A02), `c31b12c` (correcciones del contrato), `acce216` (código, migración, tests y ejemplo de A02), `f2bdb92` (corrección de una prueba de carrera que asumía qué ids ganaban) y el commit de documentación e informe que cierra esta entrega (el siguiente a `f2bdb92`; un commit no puede citar su propio hash). **Toda la evidencia de §3 se obtuvo sobre el código de `f2bdb92`** (el commit de documentación solo toca `docs/`, `README.md` y `CLAUDE.md`), salvo la primera corrida Linux, atribuida a `acce216` en §3.2.

## 1. Qué se implementó

- **Modelo y operaciones** (`ScopedOperations`, mixin `ProfileOperations`): perfiles (crear con clave natural, leer, listar con filtro de estado, editar, desactivar/reactivar, eliminar lógicamente); categorías y tipos sensibles (misma implementación parametrizada por clase); configuración de ranking/resolución; publicación de versiones inmutables; consulta y listado de versiones; perfil predeterminado (consultar, designar, quitar); resolución de perfil y versión publicados. Todo con `scope_id` + `profile_id` en cada consulta.
- **Publicación:** estado y `expected_draft_revision` bajo bloqueo; 412 aunque la configuración coincida; **proyección de configuración efectiva** (sin revisiones, marcas de tiempo ni procedencia) comparada con la versión vigente; igual ⇒ se devuelve esa versión sin alterarla (conserva su `source_draft_revision` y `source_revision` originales); distinta ⇒ instantánea completa nueva autocontenida con `content_hash` (SHA-256 de la instantánea completa, solo integridad); `UNIQUE (perfil, source_draft_revision)` como respaldo. Solo los elementos `enabled` entran; no se publica una versión sin elementos habilitados.
- **Concurrencia:** PostgreSQL `SELECT … FOR UPDATE` (perfil) y `FOR NO KEY UPDATE` (alcance), sin `UPDATE` ficticio; SQLite reutiliza la transacción de escritura de A01 y la del host. Orden alcance → perfil. Crear perfiles bajo el bloqueo del alcance; elementos y versiones bajo el del perfil; designar: alcance → perfil elegido; quitar: alcance; desactivar/eliminar: perfil + lectura de la designación. Fila de designación ausente ⇒ revisión lógica 0, inserción serializada por el alcance.
- **Permisos:** `profiles:read` y `profiles:write` (10 en total), sin jerarquía; `AdminContext` sin acceso; delegación D-27 sin cambios; claves anteriores no se amplían.
- **Catálogo:** `starter_catalog()` (sin E/S, carga perezosa) con seis tipos (`personal_data`, `government_id`, `salary_compensation`, `financial_information`, `password`, `api_key_secret`), ES/EN, ejemplos sintéticos; `password` vs `api_key_secret` por función y contexto, no por origen; copia con procedencia al crear el perfil (`initial_catalog="empty"` la omite).
- **Persistencia:** cinco tablas nuevas (`profiles`, `profile_categories`, `profile_sensitive_types`, `profile_versions`, `scope_profile_defaults`) con índices parciales únicos, `CHECK` y claves foráneas compuestas; migración aditiva `0002_profiles` (`schema_level = 2`, `min_runtime_level = 1`); el runtime exige nivel 2.
- **HTTP:** todas las rutas de [API pública §10.4](public-api.md#104-rutas-http-v1-extra-http), con `ETag`/`If-Match`, `Idempotent-Replay`, `problems` en `profile_invalid`; rechazo de `NaN`/`Infinity`.
- **Errores nuevos:** `profile_disabled`, `profile_not_selectable`, `profile_not_published`, `profile_is_default`, `default_profile_not_set`, `invalid_state_transition`, `name_conflict`, `priority_conflict`, `profile_invalid`. Interbloqueos de PostgreSQL ⇒ `database_unavailable` reintentable.
- **Eventos** en `admin_events` (ids, acción, campos y revisiones; sin contenido).
- Versión `0.2.0`; sin dependencias nuevas; `uv.lock` sin cambios de paquetes. Ejemplo `examples/profiles_host.py`.

**No se implementó (fuera de alcance):** inferencia, clasificación efectiva, el algoritmo de ranking/resolución sobre puntuaciones y el mock (A03); trabajos, archivos, PDF/DOCX, protección y restauración; organizaciones, proyectos, membresías, herencia y políticas; credenciales por perfil; purga y restauración; mejoras del backlog; AWS, despliegue, publicación, push.

## 2. Elecciones técnicas necesarias y desviaciones justificadas

Detalle y motivos en [A02-decisions §3.1](decisions/A02-decisions.md#31-decisiones-de-implementación) (I-A02-01 a 09). Las relevantes:

1. **Interbloqueos y `HostTransaction` (precisión obligatoria 1).** *Verificado con PostgreSQL real y `asyncpg`:* un interbloqueo (SQLSTATE `40P01`) aborta la sentencia; el `ROLLBACK TO SAVEPOINT` de la operación recupera la transacción exterior del host, que sigue utilizable y confirmable. Se corrigió el texto del contrato que afirmaba lo contrario. **Defecto real hallado:** la excepción llegaba envuelta en una excepción de SQLAlchemy que no es `OperationalError`, por lo que `translate()` la clasificaba como `internal_error`. Corrección mínima en `transactions.py`: `40P01` y `40001` ⇒ `DatabaseUnavailableError` (reintentable). `transaction_broken` se mantiene para conexión perdida/invalidada y fallo de recuperación o liberación (verificado también: `pg_terminate_backend` ⇒ `transaction_broken`). No se refactorizó el sistema transaccional ni se agregaron reintentos.
2. **Revocación y compatibilidad con A01 (precisión obligatoria 2).** Revocar no elimina los grants almacenados; el cargador de A01 (`ScopePermission(row.permission)`) sigue fallando en los listados administrativos incluso para claves revocadas. Reproducido sustituyendo el enum por el de A01 (dos permisos) en `tests/test_a02_migration.py::TestCompatibilityWithA01`. No se implementó tolerancia a permisos desconocidos ni se eliminan grants (B-A02-12 sigue en el backlog). `schema_level = 2` y `min_runtime_level = 1` se mantienen.
3. **Un solo centinela `UNSET`.** Las pruebas HTTP detectaron que dos centinelas distintos (`update_scope` y A02) rompían los `PATCH`; se unificó.
4. **JSON estricto en todo el adaptador:** `NaN`/`Infinity` se rechazan también en rutas de A01 (cambio de comportamiento menor y deliberado, necesario para D-A02-05).
5. **Tests de A01 actualizados por la nueva cabeza** (`("0001","0002")`, nivel 2, `0.2.0`, 10 permisos, y una línea base A01 para las pruebas de cadenas): ninguna aserción se debilitó; ver I-A02-06.
6. **Registros de terceros:** con `DEBUG` en `aiosqlite` aparecen parámetros enlazados (incluido contenido de definiciones). Los registros del motor no los contienen; se documenta como restricción operativa (I-A02-07).

## 3. Checks ejecutados y resultados reales

### 3.1 macOS (local, no es Linux ni Actions)

macOS 27.0.1 arm64, Python 3.13.16, SQLite 3.54.0, PostgreSQL 17.11 (contenedor descartable propio `cce-test-pg17`; nunca una base existente). Resumen crudo: [a02-macos-summary.txt](evidence/a02-macos-summary.txt). Código `f2bdb92`.

| Comando | Resultado |
|---|---|
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check src tests examples scripts` | 70 files already formatted |
| `uv run mypy` | Success: no issues found in 41 source files |
| `CCE_REQUIRE_POSTGRES=1 CCE_TEST_POSTGRES_URL=… uv run pytest -q -rs` | **478 passed, 13 skipped, 8 deselected** (143,6 s) |
| `uv run pytest -m dist -q` | **8 passed** (incluye el gate D-33 de importación y el ejemplo de A02 desde el wheel instalado). Ejecutado sobre `acce216` más la edición de la prueba de distribución; el commit posterior solo cambia `test_a02_concurrency.py` |

Suite A02 nueva: 146 casos (contando cada parámetro de backend) en 4 archivos (`test_a02_profiles.py` 77, `test_a02_concurrency.py` 36, `test_a02_http.py` 16, `test_a02_migration.py` 17) más `test_examples.py::test_profiles_example`; el resto son las de A01 (actualizadas por la nueva cabeza, §2.5).

### 3.2 Linux local (Docker; **no es GitHub Actions**)

`SNAPSHOT=head bash scripts/linux_docker_validate.sh DIR` (instantánea exacta de `git archive HEAD`): Debian 12 aarch64, Python 3.13.18, SQLite 3.40.1 enlazado, `uv` 0.13.7, `CCE_REQUIRE_POSTGRES=1`. Resumen crudo: [a02-linux-local-summary.txt](evidence/a02-linux-local-summary.txt) (código `f2bdb92`).

| Etapa | Resultado |
|---|---|
| ruff, ruff format, mypy | exit 0 (los tres) |
| PostgreSQL 15.19 / 16.15 / 17.11 / 18.6 + SQLite | **477 passed, 14 skipped, 8 deselected** en cada versión (exit 0) |
| Cotas inferiores declaradas (SQLAlchemy 2.0.36, Pydantic 2.9.0, asyncpg 0.30.0, aiosqlite 0.20.0, Alembic 1.14.0, FastAPI 0.115.0, Uvicorn 0.50.0) | **477 passed, 14 skipped** (exit 0) |
| `pytest -m dist` (wheel + sdist en venvs limpios) | **8 passed** |
| `scripts/audit_artifacts.py` | 2 artefactos, **0 hallazgos** |
| Importación desde el wheel (venv limpio) | caliente: mediana 207,4 ms (≤ 300 ms, D-33); frío 870,4 ms (se informa sin umbral); tamaño 28 245 KiB ≈ 27,6 MiB (≤ 40 MiB) — [a02-measure-import-linux.json](evidence/a02-measure-import-linux.json) |

**Primera corrida (sobre `acce216`), conservada por honestidad:** [a02-linux-local-run1-summary.txt](evidence/a02-linux-local-run1-summary.txt). PostgreSQL 15–18: 477 passed; **cotas inferiores: 1 failed, 476 passed**. El fallo era un defecto **de la prueba** `test_element_and_version_limits_under_the_profile_lock[sqlite]` (daba por hecho que `c0` figuraba entre las tres creaciones concurrentes ganadoras; el planificador decide cuáles). No era un defecto del producto. Se corrigió en `f2bdb92` (sin debilitar la aserción: ahora usa un id de los resultados) y la validación se repitió completa sobre `f2bdb92`, con el resultado de la tabla anterior. La prueba corrió 6 veces seguidas en macOS tras el cambio, sin fallos.

### 3.3 Piso de SQLite (Linux, Docker)

`SNAPSHOT=head bash scripts/sqlite_floor_validate.sh DIR`, sobre `f2bdb92`: [a02-sqlite-floor-summary.txt](evidence/a02-sqlite-floor-summary.txt).

| Verificación | Resultado |
|---|---|
| Suite completa con SQLite **3.38.5** (piso declarado, compilado desde sqlite.org y enlazado al intérprete) | **478 passed, 13 skipped, 8 deselected** (índices parciales únicos incluidos) |
| `start()` con SQLite 3.37.2 (por debajo del piso) | rechazado: «SQLite 3.38 or newer is required» |

### 3.4 Interbloqueos con PostgreSQL real (precisión obligatoria 1)

Medido con PostgreSQL 15–18 y `asyncpg` (`tests/test_a02_concurrency.py::TestDeadlocks`, ejecutado en macOS y en las cuatro versiones de Linux): dos transacciones del host toman dos perfiles en orden opuesto ⇒ PostgreSQL aborta una sentencia (SQLSTATE `40P01`); la operación víctima falla con `DatabaseUnavailableError` (`retryable=True`); su `ROLLBACK TO SAVEPOINT` recupera la transacción del host (`SELECT 1` funciona y se puede confirmar); el sobreviviente espera a que termine la transacción de la víctima, relee datos vigentes y recibe 412 por revisión obsoleta. Con una conexión terminada (`pg_terminate_backend`) el resultado sigue siendo `transaction_broken`. Antes de la corrección, la víctima recibía `internal_error` (comprobado con un experimento previo, no conservado como prueba).

### 3.5 GitHub Actions

**No ejecutado sobre A02.** No se hizo push. El workflow (`.github/workflows/ci.yml`) no cambió salvo su comentario de cabecera, que ahora distingue A01 (aprobado, según confirmación del usuario) de A02 (sin ejecutar); `tests/test_ci_contract.py` pasa. Queda para que el usuario lo active con su push manual; no se informa «Actions aprobado» sobre A02 hasta que exista esa evidencia.


## 4. <a id="matriz-ac-a02-trazable"></a>Matriz AC-A02 trazable

«Backend» indica dónde corre la prueba (`PG`/`SQ`: PostgreSQL y SQLite reales). «Actions» = **no ejecutado** en todos los casos. Las rutas de prueba son relativas a `tests/`.

| ID | Prueba(s) | Backend | Resultado local (macOS y Linux local) |
|---|---|---|---|
| AC-A02-01 | test_a02_profiles.py::TestJourney::test_full_python_journey_without_cawbase_model_or_mock; test_examples.py::test_profiles_example; test_distribution.py (ejemplo desde el wheel) | PG, SQ | Pasa |
| AC-A02-02 | test_a02_http.py::TestJourney::test_full_http_journey | PG, SQ | Pasa |
| AC-A02-03 | test_a02_profiles.py::TestIsolationAndPermissions::test_two_scopes_never_cross; test_a02_http.py::TestAuthorizationAndErrors::test_matrix_401_403_404 | PG, SQ | Pasa |
| AC-A02-04 | test_a02_profiles.py::…::test_two_scopes_never_cross (elemento/versión de otro perfil ⇒ 404 idéntico); test_a02_http.py::…::test_matrix_401_403_404 | PG, SQ | Pasa |
| AC-A02-05 | test_a02_profiles.py::…::test_permission_matrix; test_credentials.py::…::test_catalog_has_ten_permissions_and_no_wildcards; test_a02_http.py::…::test_admin_keys_get_no_profile_content_and_pre_existing_keys_are_not_widened | PG, SQ | Pasa |
| AC-A02-06 | test_a02_http.py::TestAuthorizationAndErrors::test_matrix_401_403_404 | PG, SQ | Pasa |
| AC-A02-07 | test_a02_profiles.py::…::test_grants_delegation_and_pre_existing_keys; test_a02_http.py::…::test_admin_keys_get_no_profile_content_and_pre_existing_keys_are_not_widened | PG, SQ | Pasa |
| AC-A02-08 | test_a02_profiles.py::…::test_disabled_scope_reads_history_but_refuses_writes_and_resolution; test_a02_http.py::…::test_scope_disabled_over_http | PG, SQ | Pasa |
| AC-A02-10 | test_a02_profiles.py::TestCrudValidationAndStates::test_profile_crud_patch_and_idempotent_create, test_id_validation, test_text_examples_translations_and_priority_validation, test_pagination_cursor | PG, SQ | Pasa |
| AC-A02-11 | test_a02_profiles.py::…::test_element_patch_semantics_and_conflicts; test_a02_http.py::…::test_write_preconditions_bodies_and_error_envelope | PG, SQ | Pasa |
| AC-A02-12 | test_a02_profiles.py::…::test_state_machine_filters_and_deleted_visibility | PG, SQ | Pasa |
| AC-A02-13 | test_a02_profiles.py::…::test_state_machine_filters_and_deleted_visibility; test_a02_http.py::TestJourney::test_filters_pagination_and_query_validation | PG, SQ | Pasa |
| AC-A02-14 | test_a02_profiles.py::…::test_state_machine_filters_and_deleted_visibility; …::test_resolution_rules | PG, SQ | Pasa |
| AC-A02-15 | test_a02_profiles.py::…::test_element_patch_semantics_and_conflicts, test_enabling_a_category_cannot_break_priority_uniqueness; test_a02_migration.py::TestDatabaseConstraints | PG, SQ | Pasa |
| AC-A02-16 | test_a02_profiles.py::TestDefaultAndResolution::test_default_lifecycle; test_a02_concurrency.py::TestDefaultProfileRaces::test_concurrent_initial_designations_leave_exactly_one_default, test_replace_and_clear_race_on_the_same_revision | PG, SQ | Pasa |
| AC-A02-17 | test_a02_profiles.py::…::test_default_lifecycle; test_a02_concurrency.py::TestDefaultProfileRaces::test_disable_or_delete_versus_designation_never_leaves_an_invalid_default; TestLockBehaviourOnPostgres::test_designation_locks_scope_then_profile | PG, SQ (bloqueo: PG) | Pasa |
| AC-A02-18 | test_a02_profiles.py::TestDefaultAndResolution::test_resolution_rules, test_default_lifecycle | PG, SQ | Pasa |
| AC-A02-19 | test_a02_profiles.py::TestJourney::test_nothing_is_created_with_a_scope_and_a_scope_without_profiles_is_valid; suite completa de A01 | PG, SQ | Pasa |
| AC-A02-20 | test_a02_profiles.py::TestVersions::test_snapshot_is_self_contained_and_immutable | PG, SQ | Pasa |
| AC-A02-21 | test_a02_profiles.py::TestVersions::test_snapshot_is_self_contained_and_immutable, test_version_listing_and_lookup | PG, SQ | Pasa |
| AC-A02-22 | test_a02_profiles.py::TestVersions::test_publishable_and_unpublishable_configurations; test_a02_http.py::…::test_empty_configuration_cannot_be_published_and_detection_only_can | PG, SQ | Pasa |
| AC-A02-23 | test_a02_profiles.py::TestNoEffectiveChange::test_noops_write_nothing_bump_nothing_emit_nothing; test_a02_http.py::…::test_write_preconditions_bodies_and_error_envelope | PG, SQ | Pasa |
| AC-A02-24 | test_a02_profiles.py::TestNoEffectiveChange::test_revision_counters_move_only_where_documented | PG, SQ | Pasa |
| AC-A02-25 | test_a02_profiles.py::TestVersions::test_effective_comparison_cases_a_to_e, test_projection_ignores_revisions_provenance_and_timestamps; test_a02_concurrency.py::TestPublicationRaces::test_concurrent_publications_of_one_revision_create_one_version; test_a02_migration.py::TestDatabaseConstraints (UNIQUE fuente) | PG, SQ | Pasa |
| AC-A02-26 | test_a02_concurrency.py::TestPublicationRaces (5 pruebas), TestDefaultProfileRaces::test_designation_versus_first_publication; TestLockBehaviourOnPostgres::test_a_writer_waits_for_the_profile_lock_and_then_rechecks_current_data | PG, SQ (espera por bloqueo: PG) | Pasa |
| AC-A02-27 | test_a02_profiles.py::TestCrudValidationAndStates (creación idempotente), TestVersions::test_effective_comparison_cases_a_to_e, TestDefaultAndResolution::test_default_lifecycle, TestNoEffectiveChange; test_a02_http.py::TestJourney (repetición de publicar) | PG, SQ | Pasa |
| AC-A02-28 | test_a02_profiles.py::TestRankingConfig::test_resolution_requires_unique_priorities_ranking_does_not | PG, SQ | Pasa |
| AC-A02-29 | test_a02_profiles.py::TestRankingConfig::test_defaults_and_validation; test_a02_http.py::…::test_write_preconditions_bodies_and_error_envelope (NaN/Infinity) | PG, SQ | Pasa |
| AC-A02-30 | test_a02_profiles.py::TestCrudValidationAndStates::test_limits, test_text_examples_translations_and_priority_validation; test_a02_http.py::… (413) | PG, SQ | Pasa |
| AC-A02-31 | test_a02_profiles.py::TestStarterCatalog::test_profile_creation_copies_with_provenance_and_empty_starts_clean | PG, SQ | Pasa |
| AC-A02-32 | test_a02_profiles.py::TestStarterCatalog::test_catalog_update_never_mutates_existing_profiles_or_versions | PG, SQ | Pasa |
| AC-A02-33 | test_a02_profiles.py::TestStarterCatalog::test_catalog_is_pure_data_with_six_synthetic_entries, test_copied_types_are_ordinary_editable_types | —, PG, SQ | Pasa |
| AC-A02-34 | test_a02_concurrency.py::TestLimitsUnderConcurrency::test_profile_limit_is_enforced_across_distinct_profiles, test_total_rows_limit_counts_deleted_profiles_too; TestLockBehaviourOnPostgres::test_creating_profiles_serializes_on_the_scope_row | PG, SQ | Pasa |
| AC-A02-35 | test_a02_concurrency.py::TestLimitsUnderConcurrency::test_element_and_version_limits_under_the_profile_lock | PG, SQ | Pasa |
| AC-A02-36 | test_a02_profiles.py::TestNoEffectiveChange; test_a02_concurrency.py::TestLockBehaviourOnPostgres::test_lock_only_operations_do_not_rewrite_the_row (xmin) | PG, SQ | Pasa |
| AC-A02-37 | test_a02_profiles.py::TestStaticChecks::test_locks_render_as_select_for_update_on_postgres_and_nothing_on_sqlite; test_a02_concurrency.py::TestDeadlocks::test_deadlock_is_a_retryable_database_error_and_the_savepoint_recovers, test_lost_connection_is_still_transaction_broken | PG (interbloqueo real); SQLite: sin bloqueos de fila | Pasa |
| AC-A02-40 | test_a02_profiles.py::TestTransactions::test_own_transaction_rolls_back_without_partial_writes | PG, SQ | Pasa |
| AC-A02-41 | test_a02_profiles.py::TestTransactions::test_host_transaction_commit_rollback_and_recoverable_errors (incluye `transaction_broken` con conexión invalidada) | PG, SQ | Pasa |
| AC-A02-42 | test_a02_migration.py::TestUpgradeFromA01, TestSchemaMatchesMigration; test_migrations.py; test_processes.py (CLI, procesos paralelos) | PG, SQ | Pasa |
| AC-A02-43 | test_a02_migration.py::TestUpgradeFromA01::test_existing_data_is_untouched_and_no_profiles_are_created, TestForeignObjectsOnFreshInstall | PG, SQ | Pasa |
| AC-A02-44 | test_a02_migration.py::TestCompatibilityWithA01 (cargador A01 reproducido; revocar no corrige; nivel 2 compatible con runtime que exige 1); test_a02_migration::TestUpgradeFromA01 (esquema 1 ⇒ `schema_incompatible`) | PG, SQ | Pasa |
| AC-A02-45 | test_a02_profiles.py::TestStaticChecks (parcial: versiones sin UPDATE/DELETE, nunca `sa.delete`, bloqueos); el aislamiento por `scope_id`/`profile_id` se verifica por comportamiento (AC-A02-03/04), **no** por análisis estático de todas las consultas | — | Pasa (parcial: ver §6) |
| AC-A02-46 | test_a02_migration.py::TestDatabaseConstraints | PG, SQ | Pasa |
| AC-A02-50 | test_a02_profiles.py::TestObservability; test_a02_http.py::TestObservabilityOverHttp | PG, SQ | Pasa |
| AC-A02-51 | test_a02_profiles.py::TestObservability, TestStarterCatalog::test_profile_creation_copies_with_provenance_and_empty_starts_clean (un evento por perfil), TestNoEffectiveChange (sin eventos en no-ops) | PG, SQ | Pasa |
| AC-A02-52 | test_a02_migration.py::TestLightImport; test_import_light.py; test_distribution.py (`-m dist`, gate D-33) | — | Pasa |
| AC-A02-53 | test_a02_http.py::* ; test_remaining_acceptance.py::…::test_every_error_code_has_the_documented_http_status | PG, SQ | Pasa |
| AC-A02-54 | Revisión documental manual (enlaces validados con un script; nombres de permisos, errores, rutas, estados y límites cotejados con el código). No es una prueba automática | — | Revisión manual (sin prueba automática) |

## 5. Restricción de despliegue de permisos nuevos y *rollback*

- **No emitir claves con `profiles:read|write` hasta haber actualizado todos los runtimes (API, workers, servicios) que acceden a esa base.** El runtime de A01 falla al cargar grants con permisos desconocidos, al autenticar la clave y en operaciones administrativas que carguen esos grants (por ejemplo `list_api_keys`).
- Orden de despliegue: migrar (credencial DDL) → actualizar todos los runtimes → recién entonces emitir claves con `profiles:*`.
- **Revocar no recupera la compatibilidad:** `revoked_at` no elimina los grants almacenados, que son inmutables; los listados de A01 pueden seguir fallando incluso para claves revocadas.
- Volver a A01 después de emitir esas claves **no es un *rollback* transparente**. La migración `0002` es solo hacia adelante y no hay *downgrade*: la vía de recuperación es restaurar desde copia o actualizar los runtimes atrasados. Cambiar el código de A02 no corrige binarios A01 ya desplegados.
- Antes de emitir claves con `profiles:*` el esquema 2 sí es compatible con un runtime A01 (ignora las tablas nuevas; `min_runtime_level = 1`).

## 6. Skips, límites y checks no ejecutados

- **Skips (no son aprobaciones):** 13 en macOS y 14 en cada corrida de PostgreSQL en Linux, y 13 con SQLite 3.38.5. Son pruebas exclusivas de PostgreSQL recogidas también para el parámetro SQLite (bloqueos de fila, interbloqueos, etc.) y una prueba que exige `CCE_EXPECT_SQLITE_VERSION` (cubierta por el script del piso de SQLite). Con `CCE_REQUIRE_POSTGRES=1` un PostgreSQL ausente falla en lugar de omitir.
- **AC-A02-45** se verifica solo parcialmente de forma estática (versiones sin `UPDATE`/`DELETE`, sin `sa.delete`, formas de bloqueo); la inclusión de `scope_id` y `profile_id` en cada consulta se acredita por comportamiento (acceso cruzado), no por un análisis estático de todas las consultas.
- **AC-A02-54** (alineación documental) es una revisión manual con verificación de enlaces por script; no es una prueba automática.
- **AC-A02-27** (reintento tras respuesta perdida) se cubre operación por operación en pruebas existentes (creación idempotente, 412 estricto, estado objetivo, republicación, no-ops), no con un arnés que simule literalmente la pérdida de una respuesta HTTP.
- **GitHub Actions** no se ejecutó sobre A02; macOS, Linux en Docker (Debian 12 aarch64) y el piso de SQLite en Docker son los únicos ambientes. **No hay x86_64**, ni Python 3.14+, ni versiones de SQLite entre 3.38.5 y la enlazada por la plataforma salvo las ya mencionadas.
- Versiones de PostgreSQL probadas: 15.19, 16.15, 17.11 y 18.6 (Linux) y 17.11 (macOS). PostgreSQL 14 no se soporta (A01).
- Las pruebas de carrera con `asyncio.gather` dependen del planificador y de dos mecanismos distintos por backend (PostgreSQL: espera real por bloqueo, verificada con `pg_stat_activity`; SQLite: un solo escritor). Se escribieron con expectativas por backend e invariantes (no con tiempos).
- Los registros de terceros en `DEBUG` pueden imprimir contenido (§2.6); no se prueba lo que un host configure fuera del motor.
- Compatibilidad con A01: probada con una reproducción del cargador de A01, **no** con un binario A01 real ejecutándose contra el esquema 2.
- Backlog no implementado (B-A02-01 a 13), incluida la tolerancia a permisos desconocidos (B-A02-12).
- La aceptación del usuario sigue **pendiente** y no la emite este informe.

## 7. Cómo reproducir

Ver [desarrollo §2, §4 y §7](development.md). A02: `uv run pytest -q tests/test_a02_profiles.py tests/test_a02_concurrency.py tests/test_a02_http.py tests/test_a02_migration.py`; `uv run python examples/profiles_host.py`; Linux: `SNAPSHOT=head bash scripts/linux_docker_validate.sh DIR`; piso de SQLite: `SNAPSHOT=head bash scripts/sqlite_floor_validate.sh DIR`.
