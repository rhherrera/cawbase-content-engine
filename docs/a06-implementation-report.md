# A06 — Informe de implementación

> **Estado: IMPLEMENTADA en `0.5.0` (migración `0004`); la aceptación final queda pendiente de la revisión del código y de la evidencia por el usuario.** Este informe describe lo que el código **hace**, lo que las pruebas **demuestran** y lo que **no** demuestran. No es un acta de cierre: no la declara aceptada, ni cerrada, ni ejecutada en GitHub Actions.

Relacionado: [contrato de A06](text-protection.md) · [especificación y matriz AC-A06](features/A06-proteccion-y-restauracion-de-texto.md) · [decisiones de A06](decisions/A06-decisions.md) · [procedimiento de desarrollo](development.md).

## 0. Identificación y estado de Git

| Dato | Valor |
|---|---|
| Rama | `feature/a06-text-protection-restoration` |
| Base | `main` = `origin/main` = `f21baaf` (merge del PR #4 de A05; `0.4.0`, migración `0003`), verificado con `git fetch` al empezar la fase de documentación |
| Versión / migración / nivel | `0.5.0` · `0004_text_protection` · `schema_level` 4 · `min_runtime_level` 1 · extra nuevo `protection` (`cryptography`) · catálogo de permisos 17 → 21 |
| Contrato aprobado | revisión 2, commit `17b545d`, **aprobado explícitamente por el usuario**, que precisó dos filas de la matriz (AC-A06-59 y AC-A06-62(a)); alineadas en `7fa33e3` antes de implementar |
| Commit de evidencia (código y pruebas) | `e0d1adc` (Linux, snapshot exacto del `HEAD` con `git archive`). La prueba del mock de A03 (`test_the_a03_mock_adapter_drives_protection_and_restoration`, `8d4041a`), `tests/test_a06_docs.py` y `scripts/check_doc_links.py` se añadieron **después**; la evidencia macOS es sobre el árbol final, cuyo hash de código (`scripts/code_tree_hash.sh`) coincide con el de lo confirmado en Git (`0358c1dd…`) |
| Commit final | el último de la rama (documentación y este informe); su hash lo informa la entrega (no puede figurar en un archivo que ese mismo commit contiene). Entre `e0d1adc` y el final el **código de `src/` no cambió**; solo se añadieron las dos pruebas y el comprobador de enlaces indicados |

Commits de A06 sobre la base (`git log f21baaf..HEAD`):

- `9673f1d` docs(a05): register the merge of A05 into main (f21baaf, PR #4) and the user-reported Actions antecedent
- `a74e148` docs(a06): specify text protection and restoration (contract, feature spec, decisions) for review
- `17b545d` docs(a06): revise the contract after the user's review of a74e148 (rev. 2)
- `7fa33e3` docs(a06): align AC-A06-59 and AC-A06-62(a) with the tests the approved contract implies
- `9dfd9de` feat(a06): text protection and restoration (Python API, contexts, encrypted mappings) in 0.5.0
- `e0d1adc` test(a06): AC-A06 acceptance tests with real cryptography and persistence on PostgreSQL and SQLite
- `8d4041a` test(a06): drive protection and restoration with the A03 mock adapter (fixtures bound to the exact text)
- *(commits finales: la documentación permanente alineada con este informe, `tests/test_a06_docs.py` y `scripts/check_doc_links.py`, más una corrección de esta lista; el hash del último lo informa la entrega)*

Nada se ha subido (`push`) ni mezclado (`merge`): lo hace el usuario. No se reescribió el historial. `docs/STAGE_3_PLAYBOOK.md` y `.DS_Store` (ignorados por Git) no se tocaron.

## 1. Qué se implementó

| Módulo | Contenido |
|---|---|
| `protection/types.py` | `ProtectionPolicy`, `InlineAnalysis`, `JobAnalysis`, `KeyProvider`/`KeyRing`, `ProtectionLimits`, `ProtectionSetup` y los tipos de salida (`ContextInfo`, `ProtectedText`, `RestoredText`, …); solo biblioteca estándar |
| `protection/markers.py` | gramática del marcador y parser acotado: clases S, R1 y R2 con `re.ASCII` ([contrato §6.2](text-protection.md#62-clases-reconocidas-y-parser-acotado-e-07-revisión-2)); `find_markers` pública y pura |
| `protection/grouping.py` | unión determinista de intervalos y precedencia de la redacción; clasificación de hallazgos en la restauración (ignorados / nuevos / consumidos) |
| `protection/crypto.py` | composición de primitivas de `cryptography` (AES-256-GCM, HKDF-SHA256, HMAC-SHA256): registro con subclave por salt, relleno a 64 B, AAD con el índice de igualdad, clave de índice por contexto envuelta, prueba de respuesta conocida |
| `protection/sources.py` | fuentes de análisis vinculadas al texto exacto; condiciones 1–8 de `JobAnalysis` y re-comprobación en la transacción de decisión |
| `protection/ops.py`, `service.py` | contextos, transacción de decisión de proteger y restaurar, ensamblado en una sola pasada, orden de operaciones del contrato |
| `protection/maintenance.py` | `run_protection_cleanup` y `protection_key_usage` |
| `operations/protection_facade.py` | los 6 métodos nuevos de `ScopedOperations` (autorización, rechazo de `HostTransaction`, despacho perezoso) |
| `persistence/a06_schema.py`, migración `0004` | `protection_contexts`, `protection_mappings`, `protection_scope_usage` (definición compartida con la migración, aditiva) |
| otros | 13 errores nuevos, 4 permisos, `ContentEngine(protection=…)`, CLI `cleanup-protection`, `examples/protection_host.py`, `scripts/check_doc_links.py` |

Sin cambios de contrato en A01–A05: no se tocó el paquete `http`, ni los contratos, la expiración o el *fence* de A05; los cambios en código existente son **aditivos** (4 miembros de `ScopePermission`, 13 clases de error, un parámetro opcional de `ContentEngine`, dos métodos de `Maintenance`, un subcomando de CLI, tres tablas) más la subida de nivel de esquema y de versión.

## 2. Verificado, parcial y sin evidencia

Cada fila de la matriz de [la especificación](features/A06-proteccion-y-restauracion-de-texto.md#5-matriz-de-aceptación-ac-a06-para-la-implementación-posterior) se traza abajo a las pruebas que la ejercen y al ambiente. **Parcial** significa que la evidencia cubre el criterio con una simplificación que se explica; **sin evidencia** que ninguna prueba lo ejerce (hoy: ninguno).

**Verificados** (la prueba ejerce el criterio tal como está redactado, en los ambientes de la tabla): AC-A06-01 a 17, 19 a 42, 44, 46 a 51, 54 y 56 a 63.

**Parciales** (se conservan como parciales; el motivo es parte del informe):

| AC | Motivo |
|---|---|
| AC-A06-18 | La frontera exacta `now == expires_at` y ±1 µs se prueba **sustituyendo `db_now`** (la función que lee la hora de la base) en el punto de decisión, no controlando el reloj de PostgreSQL; el reloj real se ejerce en AC-A06-47 (el contexto vence mientras la restauración espera el bloqueo) |
| AC-A06-43 | El `COMMIT` fallido se inyecta con un evento de SQLAlchemy y el «`COMMIT` desconocido» con una respuesta perdida **después** de un `COMMIT` real a nivel de `run_own`; no se cortó la conexión del driver a mitad del `COMMIT` |
| AC-A06-45 | Se revisan los registros del propio motor, los errores y los diagnósticos; el DEBUG de terceros (aiosqlite, eco de SQLAlchemy) imprime parámetros enlazados y queda fuera: es decisión del host |
| AC-A06-52 | La suite de A01–A05 pasa en macOS y en Linux local (cuatro versiones de PostgreSQL, cotas más bajas, piso de SQLite) con las aserciones heredadas justificadas del §6; **no** se ejecutó en GitHub Actions |
| AC-A06-53 | Wheel, sdist, extra y D-33 se midieron en macOS arm64 y Linux aarch64; **no** en el runner x86_64 de Actions ni en un índice distinto del habitual |
| AC-A06-55 | Enlaces, anclas, nombres de permisos y códigos de error, versión y niveles se comprueban automáticamente; el contenido del contrato no se valida técnicamente con eso |

**Sin evidencia:** ninguno. **Omitidos (*skip*):** los 13 de macOS y los 22 de Linux son variantes SQLite de pruebas «solo PostgreSQL» de A01–A05, la comprobación de versión exacta de SQLite (sin `CCE_EXPECT_SQLITE_VERSION`) y similares: **ninguno es de A06**; las pruebas de A06 solo PostgreSQL no tienen variante SQLite y por eso no generan *skip*.

## 3. Trazabilidad AC-A06 → prueba → ambiente → resultado

Leyenda: «N ✔» = N casos aprobados (cada caso es una prueba por backend o por parámetro); «skip» = omitido (**no** es una aprobación); «—» = la prueba no se ejecutó en ese ambiente. Las pruebas de bloqueo de filas son **solo PostgreSQL** (sin variante SQLite), por lo que en SQLite figuran sin casos.

| AC | Pruebas (nombre) | macOS arm64 (SQLite + PG 17.11) — árbol final | Linux aarch64 PG 15.19 (+SQLite) | Linux PG 16.15 | Linux PG 17.11 | Linux PG 18.6 | Linux cotas más bajas (PG 17) |
|---|---|---|---|---|---|---|---|
| AC-A06-01 | `test_round_trip_is_exact`<br>`test_the_a03_mock_adapter_drives_protection_and_restoration` | 4 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-02 | `test_redaction_creates_no_mapping_and_cannot_be_restored`<br>`test_per_type_actions_and_default` | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ |
| AC-A06-03 | `test_unicode_offsets_and_crlf_are_preserved` | 10 ✔ | 10 ✔ | 10 ✔ | 10 ✔ | 10 ✔ | 10 ✔ |
| AC-A06-04 | `test_same_value_same_marker_in_one_text_across_calls_and_different_per_context`<br>`test_two_contexts_never_share_equality_indexes` | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ |
| AC-A06-05 | `test_no_findings_returns_the_same_text_and_still_validates_the_context` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-06 | `test_regions_outside_groups_are_copied_exactly` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-07 | `test_overlapping_findings_form_one_group_and_redaction_wins`<br>`test_union_transitive_closure_and_adjacency`<br>`test_groups_are_independent_of_input_order_for_every_permutation`<br>`test_duplicate_type_on_same_interval_and_nesting_do_not_change_the_group`<br>`test_random_union_matches_a_reference_implementation`<br>`test_offsets_outside_the_text_are_never_clipped` | 7 ✔ | 7 ✔ | 7 ✔ | 7 ✔ | 7 ✔ | 7 ✔ |
| AC-A06-08 | `test_overlapping_findings_form_one_group_and_redaction_wins`<br>`test_redaction_wins_for_the_whole_group_regardless_of_order` | 3 ✔ | 3 ✔ | 3 ✔ | 3 ✔ | 3 ✔ | 3 ✔ |
| AC-A06-09 | `test_inconclusive_detection_yields_no_text_no_rows`<br>`test_detection_not_configured_in_the_profile` | 8 ✔ | 8 ✔ | 8 ✔ | 8 ✔ | 8 ✔ | 8 ✔ |
| AC-A06-10 | `test_inline_analysis_uses_the_same_text_and_the_api_accepts_no_caller_findings`<br>`test_the_a03_mock_adapter_drives_protection_and_restoration` | 4 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-11 | `test_the_result_of_an_a05_job_protects_and_restores` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-12 | `test_a_result_of_another_text_is_refused`<br>`test_text_length_inside_the_artifact_must_match_too`<br>`test_a_profile_that_differs_from_the_pinned_one_is_refused` | 14 ✔ | 14 ✔ | 14 ✔ | 14 ✔ | 14 ✔ | 14 ✔ |
| AC-A06-13 | `test_jobs_that_cannot_be_a_source`<br>`test_expired_deleted_and_other_scope`<br>`test_the_job_is_rechecked_in_the_decision_transaction` | 6 ✔ | 6 ✔ | 6 ✔ | 6 ✔ | 6 ✔ | 6 ✔ |
| AC-A06-14 | `test_marker_collision_happens_before_inference_and_any_effect`<br>`test_parser_classes` | 32 ✔ | 32 ✔ | 32 ✔ | 32 ✔ | 32 ✔ | 32 ✔ |
| AC-A06-15 | `test_limits_text_fragment_mappings_bytes_contexts` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-16 | `test_creation_defaults_ttl_and_opaque_id`<br>`test_configurable_default_validity` | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ |
| AC-A06-17 | `test_no_use_ever_renews_or_extends_the_validity` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-18 | `test_boundary_uses_the_clock_read_at_the_decision` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-19 | `test_expired_but_not_cleaned_is_unavailable_and_data_stays_until_cleanup` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-20 | `test_cleanup_is_explicit_bounded_repeatable_and_needs_no_keys`<br>`test_cleanup_runs_on_an_engine_without_protection_configured` | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ |
| AC-A06-21 | `test_explicit_delete_is_immediate_irreversible_and_idempotent`<br>`test_an_expired_context_that_was_not_cleaned_can_be_deleted` | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ |
| AC-A06-22 | `test_disabled_scope_blocks_create_protect_restore_but_not_delete_or_metadata` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-23 | `test_isolation_between_scopes_and_kinds`<br>`test_kind_must_match` | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ |
| AC-A06-24 | `test_permission_matrix_is_separated`<br>`test_permissions_are_separate` | 6 ✔ | 6 ✔ | 6 ✔ | 6 ✔ | 6 ✔ | 6 ✔ |
| AC-A06-25 | `test_catalog_has_21_permissions_and_delegation_follows_d27` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-26 | `test_restores_exactly_the_authorized_markers`<br>`test_exactly_one_authorization_declaration_is_required` | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ |
| AC-A06-27 | `test_a_valid_marker_that_was_not_delivered_is_not_authorized_and_nothing_is_consulted` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-28 | `test_unknown_and_foreign_markers_fail_closed_without_partial_text` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-29 | `test_empty_authorized_set` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-30 | `test_recognizable_alterations_fail_closed_before_any_work` | 12 ✔ | 12 ✔ | 12 ✔ | 12 ✔ | 12 ✔ | 12 ✔ |
| AC-A06-31 | `test_unrecognized_alterations_leave_text_unrestored_without_error` | 8 ✔ | 8 ✔ | 8 ✔ | 8 ✔ | 8 ✔ | 8 ✔ |
| AC-A06-32 | `test_redacted_text_and_prose_are_returned_as_is` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-33 | `test_an_original_that_looks_like_markers_is_inserted_literally_once`<br>`test_assembly_is_a_single_pass_and_never_reinterprets_originals` | 3 ✔ | 3 ✔ | 3 ✔ | 3 ✔ | 3 ✔ | 3 ✔ |
| AC-A06-34 | `test_new_sensitive_data_is_redacted_by_default_and_listed_without_values`<br>`test_new_data_is_always_redacted_even_when_the_protection_policy_would_pseudonymize` | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ | 4 ✔ |
| AC-A06-35 | `test_inconclusive_detection_on_the_response_decrypts_nothing` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-36 | `test_findings_equal_to_or_inside_a_marker_are_ignored_in_both_modes`<br>`test_restore_classification_table` | 18 ✔ | 18 ✔ | 18 ✔ | 18 ✔ | 18 ✔ | 18 ✔ |
| AC-A06-37 | `test_context_deleted_or_expired_before_the_lock_is_unavailable` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-38 | `test_a_response_over_the_text_limit_is_rejected_never_truncated` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-39 | `test_nothing_sensitive_is_stored_in_clear_or_as_an_unkeyed_hash`<br>`test_record_round_trip_padding_and_every_aad_field` | 3 ✔ | 3 ✔ | 3 ✔ | 3 ✔ | 3 ✔ | 3 ✔ |
| AC-A06-40 | `test_rotation_keeps_old_records_readable_and_the_marker_stable` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-41 | `test_missing_keys_fail_closed_without_partial_text` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-42 | `test_a_modified_record_fails_authentication`<br>`test_a_changed_version_label_fails_closed`<br>`test_a_row_moved_to_another_token_context_kind_or_scope_fails`<br>`test_tampered_index_key_blob_fails_protection` | 14 ✔ | 14 ✔ | 14 ✔ | 14 ✔ | 14 ✔ | 14 ✔ |
| AC-A06-43 | `test_failure_during_insertion_returns_no_text_and_leaves_nothing`<br>`test_a_failed_commit_and_an_unknown_commit_converge_on_retry`<br>`test_retry_guarantee_is_exactly_the_documented_one` | 6 ✔ | 6 ✔ | 6 ✔ | 6 ✔ | 6 ✔ | 6 ✔ |
| AC-A06-44 | `test_every_operation_rejects_a_host_transaction_before_any_effect` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-45 | `test_logs_errors_and_diagnostics_never_hold_text_markers_keys_or_hashes` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-46 | `test_concurrent_protections_of_the_same_value_share_one_marker_and_one_row`<br>`test_protection_serializes_on_the_context_row_while_it_is_locked` | 3 ✔ | 3 ✔ | 3 ✔ | 3 ✔ | 3 ✔ | 3 ✔ |
| AC-A06-47 | `test_restore_versus_delete_is_whole_or_closed_never_partial`<br>`test_delete_that_commits_before_the_restore_lock_makes_it_fail`<br>`test_a_restore_already_inside_its_lock_finishes_and_delete_waits`<br>`test_expiry_during_the_wait_is_decided_with_the_clock_read_after_it` | 5 ✔ | 5 ✔ | 5 ✔ | 5 ✔ | 5 ✔ | 5 ✔ |
| AC-A06-48 | `test_protect_versus_delete_leaves_no_orphan_rows`<br>`test_protect_versus_expiry_is_all_or_unavailable` | 3 ✔ | 3 ✔ | 3 ✔ | 3 ✔ | 3 ✔ | 3 ✔ |
| AC-A06-49 | `test_every_operation_acquires_locks_in_ascending_rank`<br>`test_job_source_takes_the_job_lock_before_the_context_lock` | 3 ✔ | 3 ✔ | 3 ✔ | 3 ✔ | 3 ✔ | 3 ✔ |
| AC-A06-50 | `test_upgrade_from_0003_keeps_every_a01_to_a05_row_and_object` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-51 | `test_older_runtime_keeps_working_on_level_4_and_a_new_one_refuses_level_3`<br>`test_without_protection_setup_operations_raise_after_authorization`<br>`test_start_runs_the_crypto_known_answer_test_and_validates_setup_type` | 6 ✔ | 6 ✔ | 6 ✔ | 6 ✔ | 6 ✔ | 6 ✔ |
| AC-A06-53 | `test_protection_example`<br>`test_import_loads_no_heavy_optional_or_driver_modules` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-54 | `test_a06_adds_no_http_route_and_no_queue_operation` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |
| AC-A06-55 | `test_every_relative_link_and_anchor_in_the_project_markdown_resolves`<br>`test_contract_permission_names_are_the_implemented_ones`<br>`test_contract_error_codes_are_the_implemented_ones`<br>`test_documented_version_and_levels_match_the_code` | 4 ✔ | — | — | — | — | — |
| AC-A06-56 | `test_fail_mode_rejects_before_decrypting`<br>`test_fail_mode_only_rejects_new_findings` | 14 ✔ | 14 ✔ | 14 ✔ | 14 ✔ | 14 ✔ | 14 ✔ |
| AC-A06-57 | `test_redact_mode_crossings`<br>`test_consumed_markers_are_never_looked_up_or_decrypted`<br>`test_all_markers_consumed_still_reports_redactions` | 14 ✔ | 14 ✔ | 14 ✔ | 14 ✔ | 14 ✔ | 14 ✔ |
| AC-A06-58 | `test_transitive_chains_in_redact_mode`<br>`test_restore_classification_is_order_independent` | 3 ✔ | 3 ✔ | 3 ✔ | 3 ✔ | 3 ✔ | 3 ✔ |
| AC-A06-59 | `test_ordinary_prose_is_accepted_but_not_next_to_a_strict_marker`<br>`test_parser_classes` | 34 ✔ | 34 ✔ | 34 ✔ | 34 ✔ | 34 ✔ | 34 ✔ |
| AC-A06-60 | `test_strict_marker_wins_over_inner_classes`<br>`test_prefix_with_any_continuation_is_recognized`<br>`test_core_needs_exactly_sixteen_isolated_alphanumerics`<br>`test_ascii_only_no_unicode_equivalences`<br>`test_linear_time_on_adversarial_maximum_inputs`<br>`test_delivered_material_extracts_strict_markers_only` | 6 ✔ | 6 ✔ | 6 ✔ | 6 ✔ | 6 ✔ | 6 ✔ |
| AC-A06-61 | `test_index_pointing_to_another_fragments_index_never_reuses_a_marker`<br>`test_authentic_row_with_different_content_is_rejected`<br>`test_reusing_a_marker_authenticates_the_stored_record`<br>`test_changed_version_label_on_reuse`<br>`test_index_moved_to_a_value_nobody_computes_is_the_declared_limit` | 14 ✔ | 14 ✔ | 14 ✔ | 14 ✔ | 14 ✔ | 14 ✔ |
| AC-A06-62 | `test_absent_row_key_on_reuse_is_isolated_from_the_index_key`<br>`test_absent_index_key_version_and_the_cases_that_need_no_keys`<br>`test_reuse_with_the_old_key_present_returns_the_same_marker` | 6 ✔ | 6 ✔ | 6 ✔ | 6 ✔ | 6 ✔ | 6 ✔ |
| AC-A06-63 | `test_text_restore_is_scope_level_with_no_internal_acl` | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ | 2 ✔ |

**AC-A06-52 (regresiones A01–A05):** suite completa sin cambios de garantía; las aserciones heredadas que cambiaron (y por qué) están en el [§6](#6-aserciones-heredadas-modificadas). **AC-A06-53 (distribución, D-33):** [§5](#5-distribución-wheel-sdist-y-d-33). **AC-A06-55 (documentación):** `tests/test_a06_docs.py` y `python scripts/check_doc_links.py` (enlaces y anclas sin romper, nombres de permisos y códigos de error iguales a los del código, versión y niveles); **no valida técnicamente el contrato**.

## 4. Comandos ejecutados y resultados

Entorno: macOS 27.0.1 arm64 · Python 3.13.16 · SQLite 3.54.0 (en proceso) · PostgreSQL 17.11 (contenedor descartable `postgres:17-alpine`, bases `cce_t_*`) · `uv 0.13.1` · `cryptography` 49.0.0, SQLAlchemy 2.1.8, pydantic 2.14.3, asyncpg 0.32.0, aiosqlite 0.22.1. Linux: Debian 12 aarch64 en Docker sobre macOS · Python 3.13.18 · SQLite 3.40.1 · `uv 0.13.9` · PostgreSQL 15.19, 16.15, 17.11 y 18.6.

| Comando | Ambiente | Resultado |
|---|---|---|
| `uv run ruff check .` | macOS y Linux | sin hallazgos |
| `uv run ruff format --check src tests examples scripts` | macOS (146 archivos) y Linux (144, snapshot de `e0d1adc`) | formato correcto |
| `uv run mypy` (estricto) | macOS y Linux | sin errores (89 archivos fuente) |
| `uv run pytest -q -rs` con `CCE_TEST_POSTGRES_URL` y `CCE_REQUIRE_POSTGRES=1` | macOS, árbol final: SQLite + PostgreSQL 17 | **1384 aprobadas, 13 omitidas**, 472 s |
| ídem | Linux (`bash scripts/linux_docker_validate.sh`, `SNAPSHOT=head`, `e0d1adc`): PG 15, 16, 17, 18 | **1369 aprobadas, 22 omitidas** en cada una (474, 448, 407 y 384 s) |
| `uv run --isolated --resolution lowest-direct --group dev pytest` | Linux (PG 17) | 1369 aprobadas, 22 omitidas, 437 s (SQLAlchemy 2.0.36, pydantic 2.9.0, asyncpg 0.30.0, aiosqlite 0.20.0, alembic 1.14.0, fastapi 0.115.0, `cryptography` 44.0.0) |
| `bash scripts/sqlite_floor_validate.sh` (`SNAPSHOT=head`) | Linux, SQLite 3.38.5 compilado de sqlite.org (piso declarado) | **1370 aprobadas, 21 omitidas**; con SQLite 3.37.2 `start()` **se niega** («SQLite 3.38 or newer is required») |
| `uv run pytest -m dist -q` | macOS | **10 aprobadas** en 60 s (incluye D-33 y el extra `protection`) — primera y única ejecución de ese comando sobre este árbol |
| ídem (dentro de `linux_docker_validate.sh`) y `scripts/audit_artifacts.py` | Linux | 10 aprobadas; auditoría de artefactos: 0 hallazgos |
| `python scripts/check_doc_links.py` | macOS | `bad 0` |
| Migraciones | ambos | sobre una base `0003` poblada (A01–A05) y desde cero, en ambos backends (`test_a06_migration`, `test_migrations`, `test_a02_migration`, `test_processes`); CLI `migrate` ⇒ `["0001","0002","0003","0004"]` |

Cada etapa de Linux deja su registro en el directorio de evidencia (`summary.txt`, `junit-*.xml`, registros y mediciones); está fuera del árbol del repositorio (directorio temporal de la sesión), de modo que **no** forma parte de la entrega: lo que queda es este resumen.

## 5. Distribución (wheel, sdist y D-33)

- **Contenido:** el wheel incluye `protection/*.py`, `persistence/a06_schema.py` y la migración `0004`; el sdist las incluye junto a las pruebas; la auditoría de artefactos no halla secretos ni archivos ajenos.
- **Extra `protection`:** instalado en un entorno limpio fuera del árbol, `cryptography` está presente, el HTTP no, y el ejemplo `protection_host.py` corre con el wheel instalado (SQLite). Sin el extra, `import cawbase_content_engine` no importa `cryptography` y usar A06 da un error de configuración claro. En ninguno de los dos casos la importación del paquete carga `cryptography` ni `cawbase_content_engine.protection`.
- **D-33 (intacta):** se conservaron la mediana *warm* ≤ 300 ms con 15 procesos nuevos, la medición *cold* solo informativa y el tamaño del entorno base ≤ 40 MiB. **Primera medición** = la del test original `test_installed_import_cost_meets_the_decided_references` dentro de `pytest -m dist`: **aprobó** (macOS) y también en Linux. Sin umbrales relajados ni reejecución sustituta.
- **Mediciones informativas** (`scripts/measure_distribution.py`, wheel instalado en un entorno limpio, 15 muestras *warm*, 3 *cold*, **mismo equipo y mismo Python**; base = wheel de `f21baaf`, rama = wheel de este árbol):

| | Base `f21baaf` (`0.4.0`) | Rama (`0.5.0`) |
|---|---|---|
| *warm* mediana macOS arm64 | 231,0 ms y 224,3 ms (dos corridas intercaladas; máximos 259 y 446 ms) | 228,7 ms (mín. 221,1, máx. 248,0) |
| *cold* mediana macOS | 919,0 ms y 875,7 ms | 862,8 ms |
| tamaño instalado (sin `__pycache__`) | 18 430 KiB | 18 520 KiB |
| módulos de terceros cargados al importar | sin `cryptography` | sin `cryptography` |
| *warm* mediana Linux aarch64 (Docker) | — | 199,0 ms (máx. 228,0; *cold* 847,9 ms; 28 722 KiB) |

  La rama no añade coste medible de importación (los 0,0 a 3 ms de diferencia están dentro del ruido entre corridas de la propia base). El extra instala `cryptography`, `cffi` y `pycparser`: **≈ 12,6 MiB** adicionales sobre `[sqlite,migrations]` (21 772 → 34 720 KiB); el gate de 40 MiB se aplica a la base y no cambia.

## 6. Aserciones heredadas modificadas

Cambiaron solo constantes que A06 mueve **por diseño**; cada una conserva la garantía que verificaba:

| Prueba | Cambio | Garantía que se conserva |
|---|---|---|
| `test_migrations`, `test_a02_migration`, `test_a05_retention`, `test_processes`, `test_distribution`, `test_examples` | cabeza de la cadena `0003` → `0004`, nivel 3 → 4, `to_level`, aplicadas `(…,"0004")`, versión `0.4.0` → `0.5.0` | una instalación nueva y una actualización aplican la cadena completa en una transacción; la versión instalada es la empaquetada |
| `test_a03_analysis` (`test_no_schema_change`, conteo), `test_credentials` | catálogo 17 → 21 permisos; el nivel de A03 sigue siendo «el de la cabeza» | sin comodines, sin jerarquías, conjunto exacto y delegación D-27 |
| `test_a05_jobs::test_permissions_are_separate` | el conjunto `SP − FULL` incluye los 4 permisos de A06 | el conjunto de A05 es completo y cada permiso es independiente |
| `test_a05_extra::test_static_boundaries_…` | «no hay operaciones de protección» pasa a «exactamente `protect_text` y `restore_text`», y se añade que `jobs/` y `documents/` **no** mencionan `protection` (salvo el puente `maintenance_api.py`) | A05 sigue sin conocer a A06 |
| `test_config_and_lifecycle` | el «runtime que exige un esquema más nuevo» ahora simula nivel 5 | el nivel exigido mayor que el instalado ⇒ `schema_outdated` |
| `test_import_light` | `cryptography` pasa a la lista de módulos prohibidos y `protection` a los paquetes que la importación no carga | importar el paquete no carga el extra |

## 7. Hipótesis H-A06-nn

| ID | Resultado |
|---|---|
| H-A06-01 | Confirmada: `cryptography` 49.0.0 (la fijada en `uv.lock`) ofrece `AESGCM`, `HKDF` y `hmac` (ruedas en macOS arm64 y Linux aarch64 con Python 3.13); con las cotas más bajas (`cryptography` 44.0.0) la suite también pasa; el extra aporta ≈ 12,6 MiB instalados (`cryptography`, `cffi`, `pycparser`). **No** se probó Linux x86_64 |
| H-A06-02 | Medida informativa (no es un umbral): una pasada con `batch_size=200` sobre 200 contextos vencidos de 5 correspondencias cada uno tardó 0,40 s en SQLite y 1,97 s en PostgreSQL 17 (macOS, contenedor local). La cadencia recomendada (≤ 1 h) sigue siendo una recomendación sin medición de carga real |
| H-A06-03 | Confirmada en PostgreSQL con conexión independiente que retiene el bloqueo y `pg_stat_activity` (`test_a06_concurrency`); SQLite serializa con su escritor único (pruebas de carrera sin control fino) |
| H-A06-04 | Confirmada: el trabajo de A05 conserva `input.sha256`, `input.size_bytes` y el perfil fijado (`execution_snapshot` y columnas) y el artefacto trae `input.text_length` y `profile` (`test_a06_jobs`) |
| H-A06-05 | Confirmada: las seis operaciones lanzan `IncompatibleTransactionError` antes de efectos |
| H-A06-06 | Confirmada: `cryptography` y `cawbase_content_engine.protection` no están en `sys.modules` tras importar (también en el wheel instalado, con y sin el extra); la mediana de D-33 no cambia (ver §5) |
| H-A06-07 | Confirmada: `0004` sigue el patrón de `0003` (definición compartida, aditiva) y es idempotente sobre una base `0003` poblada |
| H-A06-08 | **Parcial:** el mock de A03 (`MockInferenceAdapter` con fixtures ligados al texto exacto) se usa en una prueba de punta a punta (proteger y restaurar), pero no puede producir las respuestas con marcadores aleatorios ni los solapamientos de los casos; las demás pruebas usan un adaptador de prueba guionado (`ScriptedAdapter`, declarado `kind="mock"`) que devuelve exactamente los hallazgos que cada caso declara. El ejemplo usa un detector sintético equivalente. Ninguno acredita calidad de detección |
| H-A06-09 | Confirmada: composición estándar (AES-256-GCM + HKDF por registro), sin criptografía propia; pruebas de integridad por cada campo del AAD |
| H-A06-10 | Confirmada: `run_protection_cleanup` y `protection_key_usage` se añadieron a `maintenance()` sin tocar `run_cleanup` ni su resumen (las pruebas de A05 pasan sin cambios) |
| H-A06-11 | Confirmada: tabla de casos del parser, incluido el signo Kelvin, y entradas adversariales de longitud máxima en tiempo despreciable (medido, no un umbral) |
| H-A06-12 | Confirmada: `hmac.compare_digest` para la comparación y descifrado con AAD que incluye el índice; no hizo falta cambiar el esquema |

## 8. Limitaciones y diferencias respecto del contrato

1. **Calidad de detección:** A06 es tan completa como la detección que la alimenta; todo se probó con detección **simulada**. «Sin hallazgos» no es «sin datos sensibles». La calidad es de A09.
2. **Sin GitHub Actions:** nada se ejecutó en GitHub Actions. Linux fue **aarch64** (Docker sobre macOS arm64); el runner de Actions es x86_64: las ruedas de `cryptography` y los tiempos de importación pueden diferir (antecedente de A05 en el mismo gate: ver [A05-decisions](decisions/A05-decisions.md)).
3. **AC-A06-18 (reloj):** la frontera exacta (`now == expires_at`) se prueba sustituyendo `db_now` en el punto de lectura; el reloj real de PostgreSQL se ejerce en la prueba de vencimiento durante la espera de bloqueo (AC-A06-47), no en el borde exacto.
4. **AC-A06-43 (`COMMIT` desconocido):** se simula con una respuesta perdida **después** de un `COMMIT` real a nivel de `run_own` y con un `COMMIT` que falla (evento de SQLAlchemy); no se cortó la conexión del driver en medio del `COMMIT`.
5. **AC-A06-45:** se revisan los registros del propio motor (`cawbase_content_engine.*`). El registro DEBUG de terceros (aiosqlite, eco de SQLAlchemy) imprime parámetros enlazados —incluidos los tokens—: activarlo es decisión del host.
6. **Carreras en SQLite:** la sincronización es la compuerta del adaptador (ambas llamadas dentro del análisis a la vez) y el escritor único; el control fino por bloqueo de fila es solo PostgreSQL.
7. **Discrepancia interna del contrato resuelta:** §8.3 mencionaba HTTP 410 para `context_unavailable`; la tabla de §12.2 lo lista sin HTTP y A06 no tiene rutas: el código usa `http_status = None` ([contrato §18](text-protection.md#18-notas-de-implementación)).
8. **Límites heredados del contrato** que siguen vigentes: respuestas mayores que los límites de texto de A03 no se restauran (P-A06-03), un índice movido a un valor que nadie calcula crea un segundo marcador (la restauración de esa fila falla), sin escape de literales, sin ACL por contexto (el host decide qué usuario ve qué conversación), el motor no borra copias de seguridad.
9. **No cubierto:** DOCX/PDF (A07/A08), modelo real y calidad (A09), integración con CAWbase, UI, streaming, rutas HTTP, KMS, recifrado automático.

## 9. Local frente a GitHub Actions

Se ejecutó localmente: macOS arm64 (SQLite y PostgreSQL 17) y Linux aarch64 en Docker (PostgreSQL 15, 16, 17 y 18, cotas más bajas, distribución, piso de SQLite). **No** se ejecutó el flujo de GitHub Actions: no hubo `push`. `tests/test_ci_contract.py` sigue verificando el contrato del flujo; el flujo no cambió (el grupo de desarrollo ya instala `cryptography`).

## 10. Archivos canónicos

`docs/text-protection.md` (contrato), `docs/features/A06-proteccion-y-restauracion-de-texto.md` (alcance y matriz), `docs/decisions/A06-decisions.md` (decisiones), este informe, `src/cawbase_content_engine/protection/`, `src/cawbase_content_engine/migrations/versions/0004_text_protection.py`, `src/cawbase_content_engine/persistence/a06_schema.py`, `tests/test_a06_*.py` y `tests/a06_helpers.py`, `examples/protection_host.py`.
