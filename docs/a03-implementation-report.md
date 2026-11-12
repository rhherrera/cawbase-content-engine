# Informe de implementación de A03 — Contrato de inferencia y análisis con mock

> **Estado: A03 está implementada (`0.3.0`) en la rama `feature/a03-inference-contract-mock` (commits locales, sin push). Probada localmente en macOS y en Linux local (Docker). GitHub Actions NO se ejecutó sobre A03. NO aceptada por el usuario. La feature NO está cerrada.** Este informe no es un acta de aceptación: la aceptación corresponde al usuario. Las cifras provienen de ejecuciones reales; lo que no se ejecutó se dice explícitamente.

Relacionado: [especificación](features/A03-contrato-de-inferencia-y-analisis-con-mock.md) · [decisiones](decisions/A03-decisions.md) · [contrato](inference-and-analysis.md) · [informe de A02](a02-implementation-report.md) · [desarrollo](development.md)

## 0. Estado por nivel de evidencia

| # | Nivel | Estado |
|---|---|---|
| 1 | **Implementado** | Sí (`0.3.0`): permiso `analysis:execute`, `analyze_text`, ruta HTTP, adaptador y mock, validador, resultado, errores, composición del servicio |
| 2 | **Probado en macOS** | Sí: §3.1 |
| 3 | **Probado en Linux local (Docker)** | Sí, sobre `86bae54`: §3.2 |
| 4 | **Probado en GitHub Actions** | **No.** No se hizo push ni se ejecutó el workflow; no se informa ningún resultado de Actions sobre A03 |
| 5 | **Aceptación del usuario** | **Pendiente.** No la emite este informe |

Una corrida Linux local en Docker **no equivale** a una ejecución de GitHub Actions. Un *skip* no es una aprobación (§6).

**Diferencia fundamental:** estas pruebas validan la **ingeniería del contrato** (autorización, selección y fijación de versiones, validador, ranking/resolución, cobertura, errores, límites, cancelación, mock) con un **mock** y adaptadores de prueba. **No** miden ni acreditan calidad de detección o clasificación reales (pendiente de **A09**, que debe fijar umbrales de precisión/recall antes de evaluar). El cierre de A03 no depende de ellos.

**Commits de esta entrega** (sobre `e149146`, merge de A02): `a3993a2` (cierre documental de A02), `61813ad` (especificación de A03), `2e30c65` (precisiones P-A03-01 a 06), `ba21db4` (código, pruebas y ejemplo), `86bae54` (ajuste de una prueba del catálogo de permisos) y el commit de documentación/informe que contiene este archivo. El código y las pruebas no cambian después de `86bae54` (verificado con `git diff 86bae54 -- src tests scripts examples pyproject.toml uv.lock`: vacío; hash de árbol de código `6cca12af7b6133b1e5b5e5ce340dcabc308a22d1`).

## 1. Qué se implementó

- **Permiso `analysis:execute`** (11 permisos), sin jerarquías; delegación D-27 y grants sin cambios.
- **`ScopedOperations.analyze_text`** (mixin `AnalysisOperations`) y **`POST /v1/scopes/{scope_id}/analyze-text`** sobre la misma lógica; `ContentEngine(config, inference=InferenceSetup(...))` e `inference_status()`.
- **Selección compartida** `operations/selection.py` (sin permisos, no pública) usada por `resolve_profile` (`profiles:read`, sin cambios) y por el análisis (`analysis:execute`).
- **Preparación** en una lectura propia corta cuya conexión se devuelve antes de invocar al adaptador; `PreparedAnalysis` inmutable y `InferenceRequest` como copia independiente; `HostTransaction` rechazada con `transaction_incompatible` antes de cualquier otra acción.
- **Validador estricto** (`inference/output.py`) con códigos de problema cerrados; **ranking, resolución, cobertura y estados** calculados solo por el núcleo (`inference/build.py`), con comparación racional exacta.
- **Mock determinista** (`inference/mock.py`): fixtures atados a texto, contexto, capacidades y `definitions_digest` (que incluye el idioma); resultados `output`/`unavailable`/`hang`/`crash`; carga desde archivo; sin heurísticas ni fallback.
- **Timeout y cancelación** en la tarea del llamador; **detección de desconexión HTTP** con cancelación y respuesta 499.
- **Servicio independiente:** `…INFERENCE_ADAPTER`, `…INFERENCE_MOCK_FIXTURES`, `…INFERENCE_TIMEOUT_S`.
- **Errores nuevos:** `capabilities_not_configured`, `inference_not_configured`, `inference_unavailable`, `inference_timeout`, `inference_output_invalid`, `inference_fixture_not_found`.
- Versión `0.3.0`; sin dependencias nuevas; **sin migración** (`schema_level = 2`, `min_runtime_level = 1`); sin persistencia de solicitudes ni resultados; sin eventos en `admin_events`. Ejemplo `examples/analysis_host.py`.

**No se implementó:** modelo real, proveedor/runtime, prompts, tokenizer, segmentación específica de modelos, PDF/DOCX, protección/restauración, trabajos/colas, infraestructura, integración con CAWbase, overrides por ejecución, calidad real de inferencia.

## 2. Precisiones y decisiones de implementación

Detalle en [A03-decisions §3.1–§3.2](decisions/A03-decisions.md#31-precisiones-técnicas-de-implementación). Precisiones documentales aplicadas antes de implementar: **P-A03-01** (413 se mantiene como `limit_exceeded`), **P-A03-02** (`resolution: null` en modo `ranking`), **P-A03-03** (ejemplo crudo con tres puntuaciones), **P-A03-04** (el fragmento verifica correspondencia textual, no la ocurrencia semántica), **P-A03-05** (`Fraction(repr(x))`, independiente del contexto decimal), **P-A03-06** (selección = lecturas sucesivas, no instantánea atómica). Decisiones de implementación **I-A03-01 a 12**. Ninguna cambia D-A03-01 a 03.

## 3. Checks ejecutados y resultados reales

### 3.1 macOS (local; no es Linux ni Actions)

PostgreSQL de las pruebas: contenedor descartable dedicado `cce-test-pg17` (`postgres:17-alpine`, puerto 55417) con bases `cce_t_*` propias; **no** se usó ninguna base existente.

- `uv run ruff check .` · `uv run ruff format --check src tests examples scripts` · `uv run mypy`: sin hallazgos (51 archivos fuente) sobre el código final.
- `uv run pytest -q -rs` (SQLite siempre; PostgreSQL con `CCE_REQUIRE_POSTGRES=1`): **728 passed, 13 skipped, 8 deselected (marca `dist`)**, sobre el código de `86bae54` (sin cambios de código posteriores).
- `uv run pytest -m dist -q`: **8 passed**, ejecutado sobre `ba21db4`; el único cambio posterior de código es una prueba (`86bae54`, `tests/test_credentials.py`), y la corrida de `dist` en Linux (§3.2) es posterior.
- Una corrida intermedia sobre `ba21db4` tuvo 2 fallos de pruebas existentes que enumeraban 10 permisos y el catálogo de códigos; se corrigieron las pruebas (no el código) en `ba21db4`/`86bae54`.

### 3.2 Linux local (Docker; **no es GitHub Actions**)

`SNAPSHOT=head bash scripts/linux_docker_validate.sh DIR` sobre `86bae54` (Debian 12 aarch64, Python 3.13.18, SQLite 3.40.1): resumen íntegro en [`evidence/a03-linux-local-summary.txt`](evidence/a03-linux-local-summary.txt).

- ruff check/format y mypy: sin hallazgos.
- PostgreSQL 15.19, 16.15, 17.11 y 18.6: **727 passed, 14 skipped, 8 deselected** en cada una.
- Cotas inferiores declaradas (`--resolution lowest-direct`): 727 passed, 14 skipped.
- `-m dist` (wheel + sdist en entornos limpios): **8 passed**; auditoría de artefactos: 0 hallazgos.
- Importación (wheel, Linux): frío mediana 818,8 ms; **caliente mediana 186,7 ms** (gate D-33: ≤ 300 ms); 28 308 KiB (gate ≤ 40 MiB). [`evidence/a03-measure-import-linux.json`](evidence/a03-measure-import-linux.json).
- **Corrida previa sobre `ba21db4`** ([`evidence/a03-linux-local-run1-summary.txt`](evidence/a03-linux-local-run1-summary.txt)): 1 fallo por versión de PostgreSQL, todos en una prueba existente (`test_credentials.py`, catálogo de permisos) que aún no contaba `analysis:execute`; se corrigió la prueba y se repitió la validación completa. Se conserva como evidencia histórica de ese commit.

### 3.3 Piso de SQLite (Linux, Docker)

`SNAPSHOT=head bash scripts/sqlite_floor_validate.sh DIR` sobre `86bae54`: con SQLite 3.38.5, **728 passed, 13 skipped**; con 3.37.2 el arranque se rechaza como corresponde. [`evidence/a03-sqlite-floor-summary.txt`](evidence/a03-sqlite-floor-summary.txt).

### 3.4 GitHub Actions

**No ejecutado sobre A03.** No se hizo push; no se afirma ningún resultado de Actions.

## 4. <a id="matriz-ac-a03-trazable"></a>Matriz AC-A03 trazable

Todas las pruebas de la matriz corren sobre SQLite y PostgreSQL reales cuando dependen de la base (parámetro `db`); las puras (`test_a03_contract.py`) no usan base de datos. Estado de todas: implementadas y pasan en los ambientes de §3; el nivel «aceptado» no aplica.

| AC | Evidencia (pruebas en `tests/`) |
|---|---|
| AC-A03-01 | test_a03_http.py::TestJourney::test_http_equals_python; test_a03_analysis.py::TestJourney::test_python_journey; test_examples.py::test_analysis_example |
| AC-A03-02 | test_a03_analysis.py::TestPermissionsAndIsolation::test_execute_without_profiles_read; test_a03_http.py::TestAuthorizationOrder::test_matrix |
| AC-A03-03 | test_a03_analysis.py::TestPermissionsAndIsolation::test_other_permissions_do_not_grant_analysis, ::test_analysis_permission_grants_nothing_else; test_a03_http.py::TestAuthorizationOrder::test_matrix (admin key ⇒ 404) |
| AC-A03-04 | test_a03_analysis.py::TestPermissionsAndIsolation::test_issued_key_with_analysis_permission, ::test_delegation_cannot_exceed_issuer; test_credentials.py::test_catalog_has_eleven_permissions_and_no_wildcards |
| AC-A03-05 | test_a03_analysis.py::TestPermissionsAndIsolation::test_cross_scope_and_unknown_scope; test_a03_http.py::TestAuthorizationOrder::test_matrix |
| AC-A03-06 | test_a03_http.py::TestAuthorizationOrder::test_matrix, ::test_validation_errors, ::test_limits_and_existing_413_code; test_a03_http.py::TestErrorMapping::test_not_configured_service |
| AC-A03-10 | test_a03_analysis.py::TestSelection::test_default_explicit_version_and_states, ::test_unpublished_and_disabled_scope; test_a03_http.py::TestErrorMapping::test_profile_and_capability_errors |
| AC-A03-11 | test_a03_analysis.py::TestSelection::test_default_explicit_version_and_states, ::test_unpublished_and_disabled_scope; test_a03_http.py::TestErrorMapping::test_profile_and_capability_errors |
| AC-A03-12 | test_a03_analysis.py::TestJourney::test_python_journey (identidad de versión; sin definiciones, texto ni fragmentos) |
| AC-A03-13 | test_a03_analysis.py::TestSelection::test_pinned_version_survives_concurrent_changes, ::test_new_publication_is_used_by_new_analyses |
| AC-A03-14 | test_a03_analysis.py::TestTransactionsAndConnections::test_no_connection_or_transaction_held_during_infer |
| AC-A03-15 | test_a03_analysis.py::TestTransactionsAndConnections::test_host_transaction_rejected_untouched |
| AC-A03-16 | test_a03_analysis.py::TestTransactionsAndConnections::test_defensive_copy_of_version_and_output; test_a03_contract.py::TestValidatorRejections::test_output_is_copied_before_use; TestMockConfiguration::test_fixtures_copied_on_load_and_per_call |
| AC-A03-17 | test_a03_analysis.py::TestPermissionsAndIsolation::test_execute_without_profiles_read (resolve_profile sigue exigiendo profiles:read); suites A02 completas (regresión) |
| AC-A03-20 | test_a03_contract.py::TestValidatorRejections::test_rejected_whole (score booleano, NaN, ∞, cadena, fuera de rango) |
| AC-A03-21 | test_a03_contract.py::TestExactNumerics (todas); TestRankingAndResolution::test_boundaries_of_tolerance |
| AC-A03-22 | test_a03_contract.py::TestRankingAndResolution::test_ranking_order_ties_and_technical_tiebreak; test_a03_analysis.py::TestJourney::test_ranking_mode_has_resolution_null |
| AC-A03-23 | test_a03_contract.py::TestRankingAndResolution (boundaries, transitive, priority); test_a03_analysis.py::TestJourney::test_python_journey |
| AC-A03-24 | test_a03_contract.py::TestRankingAndResolution::test_no_eligible_category; test_a03_analysis.py::TestCapabilities::test_inconclusive_and_global_status, ::test_states_not_requested_not_configured |
| AC-A03-25 | test_a03_analysis.py::TestCapabilities::test_partial_classification_lists_unevaluated; test_a03_contract.py::TestValidatorRejections (scores_coverage_mismatch) |
| AC-A03-26 | test_a03_analysis.py::TestJourney::test_edit_after_publish_does_not_change_result, ::test_adapter_receives_only_what_it_needs, ::TestAdapterFailures::test_ranking_changes_reuse_fixture_definitions_digest |
| AC-A03-30 | test_a03_contract.py::TestLocators (emoji, UTF-16, combinantes, CRLF, ZWJ, repeticiones) |
| AC-A03-31 | test_a03_contract.py::TestLocators::test_overlaps_and_two_types_on_one_fragment; TestValidatorRejections (duplicate_finding) |
| AC-A03-32 | test_a03_analysis.py::TestJourney::test_python_journey (sin fragmento ni texto en el resultado); test_a03_http.py::TestJourney::test_http_equals_python |
| AC-A03-33 | test_a03_contract.py::TestValidatorRejections::test_rejected_whole (regiones, grupos, ids) |
| AC-A03-34 | test_a03_contract.py::TestValidatorRejections::test_finding_outside_declared_coverage_of_its_type |
| AC-A03-35 | test_a03_analysis.py::TestCapabilities::test_inconclusive_and_global_status, ::test_one_completed_one_inconclusive; test_a03_http.py::TestJourney::test_inconclusive_is_200_and_ranking_null_resolution |
| AC-A03-36 | test_a03_analysis.py::TestCapabilities::test_states_not_requested_not_configured, ::test_invalid_capabilities |
| AC-A03-40 | test_a03_contract.py::TestValidatorRejections (≈45 mutaciones, forma, serialización, tamaño); test_a03_analysis.py::TestAdapterFailures::test_invalid_output_rejected_whole |
| AC-A03-41 | test_a03_analysis.py::TestInputsAndLimits (límites, en el límite, salida); test_a03_contract.py::TestValidatorRejections::test_regions_and_finding_length_limits; test_a03_http.py::TestAuthorizationOrder::test_limits_and_existing_413_code |
| AC-A03-42 | test_a03_analysis.py::TestInputsAndLimits::test_invalid_text, ::test_whitespace_text_is_valid_and_exact, ::test_context_rules; test_a03_http.py::TestAuthorizationOrder::test_validation_errors |
| AC-A03-43 | test_a03_analysis.py::TestAdapterFailures::test_unavailable_reasons, ::test_timeout_without_retry_or_orphans, ::test_mock_hang_and_crash, ::test_adapter_own_timeout_error_is_a_timeout; test_a03_http.py::TestErrorMapping::test_adapter_errors |
| AC-A03-44 | test_a03_analysis.py::TestAdapterFailures::test_caller_cancellation_propagates; test_a03_http.py::TestDisconnect::test_client_disconnect_cancels_the_adapter |
| AC-A03-45 | test_a03_http.py::TestErrorMapping; test_remaining_acceptance.py (catálogo de códigos); test_a03_analysis.py::TestAdapterFailures::test_unexpected_adapter_exception_is_internal_error |
| AC-A03-50 | test_a03_contract.py::TestMockConfiguration::test_must_be_explicit; test_a03_http.py::TestStandaloneServiceComposition, ::TestErrorMapping::test_not_configured_service; test_a03_analysis.py::TestAdapterFailures::test_not_configured_engine_still_serves_a01_a02 |
| AC-A03-51 | test_a03_analysis.py::TestJourney::test_python_journey; test_a03_http.py::TestJourney::test_http_equals_python; test_a03_contract.py::TestMockConfiguration::test_file_loading_and_descriptor |
| AC-A03-52 | test_a03_analysis.py::TestAdapterFailures::test_fixture_missing_and_incompatible, ::test_ranking_changes_reuse_fixture_definitions_digest; test_a03_contract.py::TestMockConfiguration (duplicados, estructura, digest, claves) |
| AC-A03-53 | test_a03_analysis.py (M1 journey, M2 espacios/sin hallazgos, M5/M6 ranking y sin elegibles, M7 inconcluso, M8 cobertura parcial, M9 estados, M10 inválida, M11 truncated, M12 hang, M13/M14 unavailable, M15 sin fixture); test_a03_contract.py (M3 superposición, M4 empate, M16 categorías/tipos personalizados vía synthetic()) |
| AC-A03-54 | test_a03_analysis.py::TestTransactionsAndConnections::test_concurrent_requests_do_not_contaminate; test_a03_contract.py::TestMockConfiguration::test_fixtures_copied_on_load_and_per_call |
| AC-A03-55 | test_a03_analysis.py::TestPersistenceAndObservability::test_no_content_in_logs_or_errors; test_a03_http.py::TestObservabilityAndPersistence; test_a03_contract.py::TestValidatorRejections::test_problems_are_bounded_and_contentless |
| AC-A03-56 | test_a03_analysis.py::TestPersistenceAndObservability::test_nothing_persisted; test_a03_http.py::TestObservabilityAndPersistence |
| AC-A03-57 | test_a03_analysis.py::TestPersistenceAndObservability::test_no_schema_change; test_migrations.py / test_a02_migration.py (sin cambios) |
| AC-A03-58 | test_a03_analysis.py::TestPersistenceAndObservability::test_import_does_not_load_inference, ::TestAdapterFailures::test_inference_status_states, ::test_not_configured_engine_still_serves_a01_a02; test_import_light.py (completo); pytest -m dist |
| AC-A03-59 | test_a03_analysis.py::TestCompatibilityOfTheNewPermission; suites A01/A02 completas |
| AC-A03-60 | Este informe §6 y la documentación (no se fijan umbrales de precisión/recall) |

## 5. Restricción de despliegue del permiso nuevo

Los runtimes anteriores (A01 y A02) fallan al cargar grants con permisos desconocidos (autenticación y listados administrativos), **también de claves revocadas** (revocar no elimina los grants). **Actualizar todos los runtimes que acceden a la base antes de emitir claves con `analysis:execute`**; volver atrás no es un rollback transparente. Reproducido con el cargador de A02 en `TestCompatibilityOfTheNewPermission` (no con un binario anterior real). No se implementó tolerancia a permisos desconocidos (B-A02-12 / B-A03-10).

## 6. Skips, límites y checks no ejecutados

- **Skips (no son aprobaciones):** 13 en macOS y en el piso de SQLite, 14 en cada corrida de PostgreSQL en Linux: pruebas exclusivas de PostgreSQL recogidas también para el parámetro SQLite (bloqueos de fila, interbloqueos, etc.) y la que exige `CCE_EXPECT_SQLITE_VERSION`. Con `CCE_REQUIRE_POSTGRES=1` un PostgreSQL ausente falla.
- **GitHub Actions** no se ejecutó. No hay x86_64, ni Python 3.14+. En macOS se probó solo PostgreSQL 17.11.
- La **desconexión HTTP** se verifica con una invocación ASGI directa (`receive` que entrega `http.disconnect`), no con un servidor Uvicorn real con un cliente que corte el socket.
- Los adaptadores que **bloquean el bucle de eventos o ignoran la cancelación** quedan fuera de la garantía de cancelación/timeout; no se promete detener trabajo ya iniciado en un futuro servidor remoto.
- La **concurrencia de preparación** (lecturas sucesivas) se prueba con operaciones confirmadas mientras el adaptador está bloqueado (después de preparar), no con intercalados forzados entre las sentencias de la selección.
- La compatibilidad con runtimes anteriores se prueba con una reproducción del cargador, no con binarios reales.
- **Calidad de inferencia real: no evaluada** (A09). No se fijaron umbrales de precisión/recall.
- Backlog no implementado: [B-A03-01 a 12](decisions/A03-decisions.md#5-backlog-fuera-de-los-criterios-de-aceptación).
- La aceptación del usuario sigue **pendiente** y no la emite este informe.

## 7. Cómo reproducir

Ver [desarrollo §2, §4 y §7](development.md). A03: `uv run pytest -q tests/test_a03_contract.py tests/test_a03_analysis.py tests/test_a03_http.py`; `uv run python examples/analysis_host.py`; Linux: `SNAPSHOT=head bash scripts/linux_docker_validate.sh DIR`; piso de SQLite: `SNAPSHOT=head bash scripts/sqlite_floor_validate.sh DIR`.
