# A07 — Informe de implementación

> **Estado: IMPLEMENTADA en `0.6.0` (migración `0005`); la aceptación final queda pendiente de la revisión del código y de la evidencia por el usuario.** Este informe describe lo que el código **hace**, lo que las pruebas **demuestran** y lo que **no** demuestran. No es un acta: no declara A07 aceptada ni cerrada, ni ejecutada en GitHub Actions. Solo acredita **ingeniería** sobre documentos **sintéticos** con el mock de A03; **no** acredita calidad de detección (A09) ni soporte de DOCX reales fuera de la matriz conservadora de A05.

Relacionado: [contrato de A07](docx-protection.md) · [especificación y matriz AC-A07](features/A07-proteccion-y-restauracion-docx.md) · [decisiones de A07](decisions/A07-decisions.md) · [procedimiento de desarrollo](development.md).

## 0. Identificación y estado de Git

| Dato | Valor |
|---|---|
| Rama | `feature/a07-docx-protection-restoration` |
| Base | `main` = `origin/main` = `5bcf4b6` (merge del PR #5 de A06, contiene `e6a2a8b`), verificado con `git fetch` al empezar la Fase 1 |
| Versión / migración / nivel | `0.6.0` · `0005_docx_protection` · `schema_level` 5 · `min_runtime_level` 1 · sin extra nuevo (exige `documents` y `protection`) · catálogo 21 → **25** |
| Contrato aprobado | revisión 3, commit `044efaf`, **aprobado explícitamente por el usuario** (aprobación registrada en `df64895`) |
| Commit de evidencia (código y pruebas) | **`8af64a1`** (las corridas de Linux usan `git archive` de ese SHA); la regresión completa de macOS se ejecutó sobre `ae00ed4` (mismo código de `src/`; `8af64a1` solo añade pruebas, evidencia y un script) y las pruebas añadidas en `8af64a1` se ejecutaron aparte en macOS |
| Commit final | el último de la rama; desde `8af64a1` cambian la documentación y el margen de lease de una prueba (`2d04c4c`, §4.2) |

Commits de A07 sobre la base (`git log 5bcf4b6..HEAD`):

- `55a39ed` docs(a07): specify DOCX protection and restoration (contract, feature spec, decisions) for review
- `387fe53` docs(a07): revise the contract after the user's review of 55a39ed (rev. 2)
- `044efaf` docs(a07): define S10b on the provenance A05 actually persists; runtime deps of V1-V6 (rev. 3)
- `df64895` docs(a07): record the user's explicit approval of contract revision 3 (044efaf); Phase 2 authorized
- `406f871` feat(a07): DOCX protection and restoration as durable A05 jobs (Python API) in 0.6.0
- `ae00ed4` test(a07): AC-A07 tests with real storage, isolated child, cryptography and persistence on PostgreSQL and SQLite
- `8af64a1` test(a07): permission matrix, redaction, source identity/profile, collision and LibreOffice render evidence
- (este informe) docs(a07): implementation report and permanent documentation aligned with the implemented code

## 1. Estado por nivel de evidencia

| # | Nivel | Estado |
|---|---|---|
| 1 | Especificado | Sí: contrato rev. 3 (`044efaf`), aprobado por el usuario |
| 2 | Implementado | Sí: `406f871` |
| 3 | Probado en macOS | Sí: regresión completa (PostgreSQL 17 + SQLite) y suites de A07; renders **no** (no hay LibreOffice en macOS) |
| 4 | Probado en Linux local (Docker) | Sí: suites completas (PostgreSQL 15–18 + SQLite), piso de SQLite y renders de LibreOffice; ver §4 |
| 5 | Probado en GitHub Actions | **No** |
| 6 | Aceptado por el usuario | **No** (pendiente) |

## 2. Qué se implementó

- **Operaciones durables** `docx_protection` y `docx_restoration` sobre el sistema de trabajos de A05 (reclamación, lease, *fencing*, reintentos, idempotencia, storage verificado, cuotas, fence del scope, limpieza), expuestas solo por Python (`submit_docx_protection`, `get_docx_protection_result`, `open_docx_protected`, `get_docx_derivative`, `submit_docx_restoration`, `get_docx_restoration_result`, `open_docx_restored`).
- **Vinculación al texto exacto** (S1–S11): reextracción aislada y comparación byte a byte del `SourceMap`; **S10b** con las dos comparaciones (cabecera = descriptor fijado; cada unidad = su proyección a `Provenance`).
- **Planificación y transformación** en el proceso aislado (`documents/docx_transform.py`, `lxml`): reglas R1–R5, pila de campos por parte, sustituto en el primer run, limpieza obligatoria de `E`, partes no editadas byte a byte.
- **Verificador independiente** (`documents/docx_verify.py`, solo biblioteca estándar): validación estructural V1–V6, extracción propia, oráculo por posición, esqueleto E1/E2 y escaneo K1–K8 con atribución por ubicación exacta (mapa de bytes con `expat`).
- **Publicación** con `PublishHook`: el vínculo `docx_derivatives` (HMAC con la clave de índice del contexto) se inserta en la misma transacción que T3, con una sola hora de decisión tras todos los bloqueos (rangos 1–14).
- **Restauración** del derivado exacto: digest y tamaño, posiciones (T5), MAC del vínculo (T4), descifrado de A06, una pasada, verificación y publicación condicionada al contexto vigente.
- **Lectores**: los genéricos de A05 solo sirven operaciones de análisis; la lectura del restaurado se autoriza tras verificar el objeto en el spool (P-A07-03).
- **Ciclo de vida**: P-A07-04 (purga inmediata de `docx_restored` descartado, también los que dejó un runtime anterior); P-A07-06 (`delete_job_data` conserva el vínculo); `run_docx_cleanup` / CLI `cleanup-docx`.
- **Reutilización de A06** sin duplicar: `obtain_tokens`, `decrypt_tokens`, `insert_context`, `context_index_key`.

## 3. Matriz de aceptación trazada a pruebas

Estados: **verificado** (prueba que lo demuestra, en los ambientes indicados) · **parcial** (parte demostrada; se indica lo que falta) · **sin evidencia**. «PG+SQ» = PostgreSQL y SQLite reales. Archivos: `pure` = `tests/test_a07_pure.py` (sin base), `jobs` = `tests/test_a07_jobs.py`, `life` = `tests/test_a07_lifecycle.py`, `mig` = `tests/test_a07_migration.py`.

| AC | Estado | Evidencia (prueba · ambiente) | Lo que falta |
|---|---|---|---|
| 01 | verificado | jobs `test_protect_and_restore_end_to_end` · PG+SQ | — |
| 02 | verificado | jobs `test_submission_checks_and_idempotency` (firma sin hallazgos/mapa) | — |
| 03 | parcial | jobs: otro scope/inexistente, `text_analysis`, sin `results:read` | `job_not_succeeded`, `job_data_expired`, `analysis_expiring` sin prueba propia |
| 04 | parcial | jobs `test_source_identity_profile_original_and_collision`: original eliminado ⇒ `file_not_usable` | vencido, perdido, `rejected`, `file_expiring`; caso entre envío y reclamación |
| 05 | verificado | jobs: digest del original alterado en el manifiesto ⇒ `analysis_source_invalid` | — |
| 06 | verificado | life `test_stale_map_and_inconclusive_detection`: worker con otra configuración ⇒ `analysis_source_stale` | — |
| 07 | verificado | life: segmento alterado ⇒ `analysis_source_stale`; determinismo de la reextracción en todas las protecciones exitosas (macOS y Linux) | — |
| 08 | parcial | life: unidad `inconclusive` ⇒ `detection_inconclusive` | cobertura incompleta, `not_requested`, `not_configured` sin prueba propia |
| 09 | verificado | jobs: perfil distinto y hallazgo fuera de rango ⇒ `analysis_source_invalid` | — |
| 10 | parcial | unión y precedencia: funciones de A06 (suites de A06); jobs `test_existing_context_...` (contexto compartido) | prueba explícita de «mismo fragmento ⇒ mismo marcador» entre DOCX y `protect_text` |
| 11 | verificado | jobs: DOCX con marcador ⇒ `marker_collision`; pure: S/R1/R2 y prosa | — |
| 12 | verificado | pure `test_value_across_three_runs_takes_the_first_run_format` | — |
| 13 | verificado | pure `test_several_selections_in_one_text_node_and_unicode` | — |
| 14 | verificado | ídem | — |
| 15 | verificado | pure `test_tables_nested_merged_headers_footers_notes` | — |
| 16 | verificado | ídem + render F03 (Linux) | encabezados `default`/`even` no visibles en renders de una página |
| 17 | verificado | pure `test_hyperlink_inside_is_transformed_and_crossing_fails` | — |
| 18 | verificado | pure `test_content_controls` (6 variantes) y cruce | — |
| 19 | verificado | pure `test_field_results_are_not_transformable` (simple, complejo, hacia el resultado, `HYPERLINK`, anidado) y campo entre párrafos | — |
| 20 | verificado | pure `test_field_crossing_paragraphs_and_unbalanced_stack` | — |
| 21 | parcial | pure: `w:tab`, `w:br`, `w:sym`, `w:footnoteReference`, `w:noBreakHyphen` | `w:drawing`, `w:cr`, `w:fldChar` aislado (cubierto por campos) |
| 22 | parcial | pure: marcador de posición dentro y fuera | `w:permStart/End` |
| 23 | parcial | pure: `w:smartTag`, `w:customXml` en línea, `w:dir` | `w:ruby`, `w:bdo` |
| 24 | parcial | rechazos de A05 cubiertos por sus suites; una protección sobre un análisis no `succeeded` ⇒ `job_not_succeeded` (validación del envío) | prueba de cada familia a través del flujo completo |
| 25 | verificado | pure `test_one_bad_selection_fails_the_whole_document` | — |
| 26 | verificado | pure `test_verifier_negative_controls` (estructura) y E1 en `test_tables_...` | — |
| 27 | verificado | pure `test_verifier_uses_runtime_dependencies_only_and_python_docx_opens_outputs`; V5 en todo E2E | — |
| 28 | verificado | pure `test_e_classes_removed_and_annotated_text_kept`; jobs (clases en el informe) | — |
| 29 | verificado | ídem | — |
| 30 | verificado | ídem + `test_unknown_comment_part_is_refused` | — |
| 31 | parcial | jobs: `document_coverage_complete = true` sin `N`; `N` declarados del análisis se copian al informe | caso con `N` a través del flujo completo |
| 32 | verificado | pure `test_verifier_negative_controls` | — |
| 33 | verificado | ídem (original como derivado falla) | — |
| 34 | verificado | pure `test_copies_of_a_selected_value_block_publication` (texto alternativo, destino `%XX`, instrucción partida, propiedad conservada) | — |
| 35 | verificado | pure `test_permitted_occurrence_...`; jobs E2E (mención libre conservada) | — |
| 36 | verificado | pure `test_all_occurrences_selected_means_absence_everywhere` | — |
| 37 | verificado | pure `test_document_without_values_is_unchanged_but_cleaned` | flujo durable sin hallazgos no probado aparte |
| 38 | parcial | pure `test_representations_...`: referencias de carácter, mayúsculas, blancos colapsados; la búsqueda ingenua no los ve | NFD explícito y UTF-16 en partes binarias |
| 39 | verificado | jobs: `inspection_scope` (K7, `not_covered`) en el informe | — |
| 40 | verificado | pure `test_restore_puts_values_back_...`; renders F07 | — |
| 41 | verificado | jobs E2E + evidencia (7 documentos) | — |
| 42 | parcial | life `test_restore_after_purging_...`: original y análisis eliminados, datos del trabajo eliminados, limpieza y metadatos del trabajo purgados ⇒ restauración `succeeded` · PG+SQ | vencimiento por reloj (se usó eliminación explícita) |
| 43 | parcial | jobs `test_restoration_rejects_another_file` (otro DOCX, un byte cambiado) en el envío | comprobación en el intento e instrumentación «no se invoca el proceso aislado» |
| 44 | verificado | pure `test_position_check_rejects_...` (a); life `test_key_absent_and_tampered_rows_fail_closed` (vínculo manipulado) (b) | — |
| 45 | verificado | life `test_context_rules_on_submission`; life `test_deleting_the_context_...` (entre envío y ejecución) | — |
| 46 | parcial | life: versión de clave retirada ⇒ `protection_key_unavailable` | ausencia solo de la versión de la clave de índice |
| 47 | parcial | life: ciphertext alterado ⇒ `mapping_integrity_failed` | índice/token/versión (cubiertos por suites de A06) |
| 48 | verificado | jobs `test_permission_matrix_of_every_a07_operation` | — |
| 49 | verificado | jobs `test_redaction_never_comes_back_...` | — |
| 50 | verificado | pure `test_restore_puts_values_back_...` (original con sintaxis de marcador) | — |
| 51 | verificado | life `test_restore_after_purging_...` (tras `delete_job_data` del trabajo de protección) | — |
| 52 | verificado | jobs: restauración con un motor **sin** adaptador de inferencia | — |
| 53 | verificado | jobs `test_permission_matrix_...` (cada permiso, `results:read`, contexto sin permisos) | delegación D-27 con los permisos nuevos (cubierta genéricamente por A01) |
| 54 | parcial | jobs `test_generic_readers_never_serve_a07_artifacts` (Python) | rutas HTTP no ejercitadas (usan la misma función con la guarda) |
| 55 | verificado | mig `test_older_runtimes_...` (lógica por `kind` del lector anterior) | — |
| 56 | sin evidencia | la descarga verificada es la de A05 (`spool_artifact`, suites de A05) | objeto de A07 corrupto |
| 57 | parcial | life `test_reading_the_restored_docx_...`: permiso, scope deshabilitado, contexto eliminado | resultado vencido |
| 58 | verificado | jobs `test_existing_context_and_restore_text_cross_path` | — |
| 59 | parcial | life `test_retry_reuses_markers_and_publishes_once` (un vínculo, dos artefactos publicados) | inyección alrededor del `COMMIT` de publicación |
| 60 | sin evidencia | — | espera de bloqueo controlada con `pg_stat_activity` |
| 61 | parcial | life `test_context_deleted_before_publication_...` y `test_concurrent_protection_and_context_deletion` · PG+SQ | vencimiento y deshabilitación del scope en mitad del intento |
| 62 | parcial | life `test_retry_reuses_markers_and_publishes_once` (reintento tras fallo, marcadores reutilizados; con margen de lease desde `2d04c4c`, §4.2) | prueba de pérdida de lease (el ajuste del margen no la acredita) |
| 63 | sin evidencia | — (protocolo de A05, sus suites) | inyección en cada frontera para trabajos de A07 |
| 64 | verificado | life `test_context_deleted_before_publication_and_immediate_purge` | — |
| 65 | verificado | jobs `test_submission_checks_and_idempotency` | dos primeras concurrentes |
| 66 | sin evidencia | — | registro de sentencias con bloqueo |
| 67 | parcial | life `test_deleting_the_context_...` (`run_docx_cleanup` marca `ended`) | purga de lápidas a 30 días |
| 68 | sin evidencia | — | fronteras de límites de A07 |
| 69 | verificado | life `test_no_values_in_logs` | — |
| 70 | verificado | Linux: LibreOffice 7.4.7 abre y convierte los 21 DOCX (original, protegido, restaurado de 7 casos) sin avisos de reparación (§5) | Word pendiente |
| 71 | verificado | Linux: renders revisados (§5) | Word pendiente; macOS sin LibreOffice |
| 72 | verificado | E01 reguardado por LibreOffice: protegido y restaurado (§5) | — |
| 73 | verificado | mig `test_upgrade_from_0004_keeps_every_row_and_object` · PG+SQ | — |
| 74 | verificado | mig `test_older_runtimes_and_a_new_one_on_an_old_schema` · PG+SQ | — |
| 75 | verificado | regresión completa macOS y Linux (§4) | — |
| 76 | verificado | `pytest -m dist` y `measure_distribution.py` (§4) | — |
| 77 | verificado | mig `test_public_surface_has_no_new_http_routes` | — |
| 78 | verificado | `check_doc_links.py` + `test_a06_docs.py` | — |
| 79 | verificado | pure `test_permitted_occurrence_in_one_text_node_passes_every_target` | — |
| 80 | verificado | pure `test_an_extra_copy_in_the_same_part_fails` (atributo, instrucción, comentario XML) | — |
| 81 | verificado | pure `test_split_values_permitted_and_selected` | — |
| 82 | verificado | life `test_model_analysis_and_provenance_mismatches_are_refused` (positivo real con el mock, cabecera, unidad, modelo) · PG+SQ; pure `test_mock_provenance_rules` | — |
| 83 | verificado | life `test_deleting_the_context_ends_restorability_of_every_derivative` · PG+SQ | — |
| 84 | verificado | life `test_authorization_instant_of_the_restored_download` · PG+SQ | — |
| 85 | verificado | jobs `test_generic_readers_...` (`docx:protect` no descarga el original) | — |
| 86 | parcial | jobs: replay devuelve el mismo contexto | inyección de fallo en la transacción de admisión |
| 87 | verificado | life `test_purge_of_a_restored_docx_discarded_by_an_older_runtime` · PG+SQ | — |

Resumen: **61 verificados**, **21 parciales**, **5 sin evidencia** (AC-A07-56, 60, 63, 66 y 68). Los parciales y sin evidencia se conservan como tales; ninguno se presenta como cumplido.

## 4. Comandos, ambientes y resultados

| Comando | macOS 27.0.1 arm64, Python 3.13.16 | Linux local (Docker, `python:3.13-bookworm`, aarch64) |
|---|---|---|
| `uv run ruff check .` / `ruff format --check src tests examples scripts` | sin errores | sin errores |
| `uv run mypy` | 100 archivos, sin errores | 100 archivos, sin errores |
| `uv run pytest -q -rs` (PostgreSQL 17 + SQLite, `CCE_REQUIRE_POSTGRES=1`) | **1487 passed, 15 skipped**, 0 failed (sobre `ae00ed4`, 10 min 25 s) | 1485 passed por versión de PostgreSQL 15–18 (§4.1) |
| `uv run pytest -m dist -q -rs` | 10 passed (con PostgreSQL) | 10 passed |
| `scripts/measure_distribution.py` (D-33) | mediana warm **262,5 ms** (15 procesos; máx 274,4), cold 941,4 ms (informativa); instalado **18,2 MiB**; ni `cryptography`, ni `lxml`, ni `pikepdf` al importar | 195,9 ms / 28,2 MiB (§4.1) |
| `bash scripts/sqlite_floor_validate.sh` | — | 3.38.5 pasa; 3.37.2 rechazado (§4.1) |
| `bash scripts/a07_render_evidence.sh` | — | §5 |

Omisiones en macOS (15): pruebas que exigen Linux (contenedor de aislamiento de A05, permisos de archivos del sistema operativo) y la generación de evidencia de A07 (`CCE_A07_EVIDENCE_DIR` no definido); ninguna omisión es un criterio de A07.

### 4.1 Linux local (Docker)

`SNAPSHOT=head bash scripts/linux_docker_validate.sh` sobre **`8af64a1`** (`git archive`), Debian 12 aarch64 (Docker sobre macOS, kernel `linuxkit`), Python 3.13.18, SQLite 3.40.1. Resumen: [`evidence/a07-linux-local-summary.txt`](evidence/a07-linux-local-summary.txt).

| Paso | Resultado |
|---|---|
| `ruff check` / `ruff format --check` / `mypy` | sin errores (164 archivos formateados; 100 archivos tipados) |
| Suite completa con PostgreSQL 15.19 (+ SQLite) | **1485 passed, 24 skipped**, 0 failed (12 min 56 s) |
| Ídem PostgreSQL 16.15 | 1485 passed, 24 skipped (11 min 25 s) |
| Ídem PostgreSQL 17.11 | 1485 passed, 24 skipped (9 min 26 s) |
| Ídem PostgreSQL 18.6 | 1485 passed, 24 skipped (9 min 35 s) |
| Cotas mínimas declaradas (SQLAlchemy 2.0.36, pydantic 2.9.0, asyncpg 0.30.0, aiosqlite 0.20.0, …) | 1485 passed, 24 skipped |
| `pytest -m dist` (wheel y sdist en entornos limpios) | 10 passed; auditoría de artefactos: 0 hallazgos |
| D-33 (wheel instalado, Linux) | mediana warm **195,9 ms** (15 procesos), cold 871,8 ms (informativa), 28,2 MiB |
| Piso de SQLite (`scripts/sqlite_floor_validate.sh`) | SQLite 3.38.5: 1486 passed, 23 skipped; 3.37.2: `start()` rechazado como se espera ([resumen](evidence/a07-sqlite-floor-summary.txt)) |

Las omisiones (24) son las mismas familias que en macOS sin LibreOffice/evidencia de A07 y pruebas que dependen del entorno (contenedor de aislamiento dentro del contenedor, generación de evidencia); ninguna corresponde a un criterio de A07 marcado como verificado.

### 4.2 Revisión de los dos fallos de GitHub Actions sobre `c4faa40` (informados por el usuario)

El agente **no tuvo acceso a los logs** de Actions (`gh` no está instalado en este equipo): lo de abajo parte de lo que el usuario informó y de lo que se reprodujo localmente.

**1. `test_retry_reuses_markers_and_publishes_once[sqlite]`: `state="running"`, `outcome_code="lease_lost"` en el primer intento (se esperaba `retry_wait`).**

- **Qué demuestra el log informado:** el worker ya no tenía la posesión cuando quiso decidir el fallo inyectado (o en una comprobación de posesión anterior): o `LeaseLost`, o `_finish_failure` encontró el lease vencido y devolvió `lease_lost`, dejando el trabajo `running` para que lo recupere otra reclamación (camino seguro de A05 §6.3: un dueño sin posesión no decide ni publica).
- **Qué se reprodujo:** el síntoma exacto (`running` / `lease_lost`) aparece si las renovaciones se interrumpen más que `lease_ttl_s` (3 s en `fast_limits()`): experimento ad hoc, no commiteado, que bloquea el *event loop* 3,5 s antes de lanzar el fallo inyectado. **No** se reprodujo el fallo de CI: 5 corridas bajo carga de CPU (10 procesos ocupados en 8 núcleos) y 4 corridas instrumentadas pasaron; la mayor separación entre renovaciones fue 0,65 s, cada renovación tardó ≤ 0,014 s y con `PYTHONASYNCIODEBUG=1` no hubo ninguna llamada que bloqueara el *event loop* más de 100 ms. La causa en el runner (escrituras lentas de SQLite, planificación) **no se pudo determinar**.
- **Defecto del worker:** no se encontró; el comportamiento observado es el previsto ante un lease vencido.
- **Cambio (`2d04c4c`):** solo esa prueba usa `lease_ttl_s=30` (heartbeat 1 s; el backoff rápido y todas las aserciones se conservan). Sigue ejercitando el fallo inyectado en `docx_verify`, el reintento, la reutilización de marcadores (`reused_markers` = filas previas, `new_markers` = 0) y una sola publicación (un vínculo, dos artefactos publicados). Corrida dirigida: **3 de 3** en SQLite y PostgreSQL 17 (macOS). **Este ajuste no acredita una prueba de pérdida de lease**: AC-A07-62 sigue **parcial**.

**2. `test_installed_import_cost_meets_the_decided_references`: 320,7 ms warm frente al máximo de 300 ms.**

- **Comparación** de wheels limpios de la base `5bcf4b6` (0.5.0) y de HEAD (0.6.0), el mismo Python 3.13.18 (`python:3.13-bookworm`), dependencias **idénticas** (las 7 resueltas para la base, congeladas y usadas como restricción para HEAD; comprobado con `uv pip freeze`) y **mediciones intercaladas** (6 rondas base/HEAD alternadas, 15 procesos *warm* por ronda con `scripts/measure_import.py`):

| Plataforma | Base: medianas por ronda (ms) | HEAD: medianas por ronda (ms) | Mediana de rondas base → HEAD |
|---|---|---|---|
| Linux **x86_64** (Docker `--platform linux/amd64`, **emulado** en Apple silicon) | 408,0 · 407,7 · 416,4 · 409,2 · 458,4 · 416,7 | 411,6 · 417,2 · 411,4 · 405,1 · 418,5 · 419,2 | 412,8 → 414,4 (+1,6) |
| Linux aarch64 (Docker nativo) | 202,9 · 199,4 · 192,6 · 193,2 · 193,4 · 194,0 | 195,6 · 200,3 · 194,6 · 196,0 · 194,3 · 191,9 | 193,7 → 195,1 (+1,4) |

- **Qué agrega A07 al camino de importación:** exactamente dos módulos, `operations/docx_facade.py` y `persistence/a07_schema.py` (452 → 454 módulos), con **≈ 0,10 ms y ≈ 0,07 ms** de tiempo propio (media de 15 `-X importtime`, aarch64). No importa `lxml`, `cryptography`, `pikepdf` ni el subpaquete `docx_protection`.
- **Conclusión:** la comparación **no identifica una regresión reproducible** atribuible a A07 (diferencias de ~1,5 ms, dentro del ruido entre rondas); no se aplicó ninguna corrección. Los 320,7 ms de Actions no se explican por A07 con esta evidencia.
- **Diferencias con el runner de Actions (declaradas):** el x86_64 de esta comparación es **emulado** (sus valores absolutos no son comparables con el umbral; solo vale la comparación relativa) y el runner `ubuntu-latest` es x86_64 nativo con otro kernel, disco y carga. No se tuvo acceso a un Linux x86_64 nativo.
- **Check original** (umbral 300 ms, 15 muestras, configuración de CI sin cambios): `uv run pytest -m dist -q -rs` en macOS sobre HEAD: **10 passed**; en Linux aarch64 (Docker, `8af64a1`): mediana *warm* 195,9 ms (§4.1).

**Checks de CI a repetir:** el trabajo de pruebas con SQLite (ya con `2d04c4c`) y el trabajo `distribution` (sin cambios de código: hay que volver a ejecutarlo para obtener una medición nueva en el runner; si vuelve a superar 300 ms, conviene medir base y HEAD en el mismo runner).

## 5. Apertura sin reparación e inspección visual (LibreOffice en Linux)

- **Comando:** `bash scripts/a07_render_evidence.sh OUT` (imagen `cce-a04-render`, de A04). Resumen con hashes: [`evidence/a07-libreoffice-linux-summary.txt`](evidence/a07-libreoffice-linux-summary.txt); montajes original | protegido | restaurado: `docs/evidence/a07-renders/`.
- **Entorno registrado al ejecutar:** Debian 12.15 aarch64 (Docker sobre macOS, kernel `linuxkit`); **LibreOffice 7.4.7.2**; Poppler `pdftoppm` 22.12.0; fuentes incrustadas en los PDF: DejaVu Sans y DejaVu Sans Bold (las únicas instaladas en la imagen además de DejaVu Sans Mono).
- **Documentos:** F01 (valor en 3 runs con cursiva, negrita y color; Unicode), F02 (tablas anidadas y combinadas), F03 (encabezados `default`/`first`/`even`, pie, nota al pie), F05 (hipervínculo), F06 (control de texto simple), F07 (comentarios, personas, propiedades, miniatura) y **E01** (F01 **reguardado por LibreOffice Writer 7.4.7** con el filtro «MS Word 2007 XML»).
- **Resultado:** los 21 DOCX (original, protegido y restaurado de cada caso) se abrieron y convirtieron a PDF sin fallos y **sin ninguna línea de reparación, corrupción ni error** en la salida (la única línea es el aviso genérico de Java `javaldx`, ajeno al documento). La cadena real (SQLite, mock de A03) protegió y restauró los 7 casos; el texto restaurado es igual al original en todos; E01 **pasó la matriz de A05** (no tiene `customXml`) — H-A07-09 confirmada.
- **Revisión manual** (hecha por quien implementó, sobre las imágenes): (1) los valores seleccionados se ven sustituidos por marcadores y el texto no seleccionado (incluida la mención libre idéntica) queda en su lugar; (2) el formato fuera del valor se conserva (cursiva, negrita, color, párrafo centrado); en F01/E01 el sustituto toma la cursiva del primer run y la línea reflujea; (3) la tabla combinada y anidada de F02 conserva su estructura; el solapamiento de «Ejemplo» con la celda anidada está **ya en el original** (diseño del fixture, no de la transformación); (4) F03 conserva el encabezado de primera página, la llamada y el texto de la nota al pie; (5) F07 muestra el texto anotado sin el comentario; (6) los restaurados coinciden visualmente con los originales salvo el formato mixto dentro del valor (D4, fuera de garantía).
- **No probado:** Microsoft Word (no disponible: **pendiente** como lector); renders en macOS (sin LibreOffice); encabezados `default`/`even` y pie `default` no aparecen porque los documentos tienen una página.

## 6. Hipótesis H-A07

| H | Resultado |
|---|---|
| 01 | Confirmada: la reextracción reprodujo el `SourceMap` byte a byte en todas las protecciones exitosas (macOS y Linux) |
| 02 | Confirmada: `requirements_key` de A07 distinta; un motor sin `ProtectionSetup` no la anuncia (mig) |
| 03 | Confirmada: el lector por `kind` de `0.5.0` no encuentra `result`/`source_map` en trabajos de A07 (mig) |
| 04 | Confirmada: sin `CHECK` en `operation`/`kind`; columnas `NOT NULL` llenadas con la identidad del análisis o del vínculo |
| 05 | Confirmada: funciones de A06 extraídas sin cambio de comportamiento (suites de A06 verdes) |
| 06 | Confirmada: partes no editadas copiadas sin pasar por el analizador; las editadas conservan declaración y espacios de nombres (E1/E2 en pure) |
| 07 | Confirmada en LibreOffice 7.4.7 (§5) |
| 08 | Confirmada: `w:sym`/`w:ptab`/`w:softHyphen` no son texto de unidad; una selección que los cruza falla (pure) |
| 09 | Confirmada (E01) |
| 10 | Confirmada (D-33, §4) |
| 11 | Confirmada: tramos de bytes exactos (pure, K7) |
| 12 | Confirmada: descarte y purga (life) |
| 13 | Confirmada: P1/P2 verificables con los datos de A05 (life, caso positivo real) |

## 7. Aserciones heredadas modificadas (justificadas)

Versión `0.5.0` → `0.6.0`, `schema_level` 4 → 5, lista de migraciones con `0005`, catálogo 21 → 25 (`test_credentials`, `test_a03_analysis`, `test_a05_jobs`, `test_a06_contexts`, `test_a06_docs`, `test_a02_migration`, `test_a06_migration`, `test_migrations`, `test_processes`, `test_examples`, `test_distribution`, `test_config_and_lifecycle`, `test_a05_retention`). `test_a05_extra` amplía la lista de operaciones de la familia «protect/restore» con las de A07 y la de puntos de unión de `jobs/` y `documents/` con A07 (despacho, claves, limpieza, modos del proceso aislado). Ninguna garantía que esas pruebas verificaban se debilitó.

## 8. Limitaciones y pendientes

- **Criterios parciales o sin evidencia** de §3 (sin evidencia: AC-A07-56, 60, 63, 66 y 68): necesitan pruebas de concurrencia con espera controlada, inyección de fallos de storage para A07, registro del orden de bloqueos y pruebas de límites.
- Solo DOCX **sintéticos** dentro de la matriz conservadora de A05; un DOCX de Word con `customXml` o varias secciones se rechaza antes de llegar a A07.
- Word no probado; render solo con LibreOffice en Linux.
- El mock de A03 no mide detección; la ausencia de los valores sintéticos no acredita ausencia de datos sensibles (A09).
- GitHub Actions no ejecutado.
