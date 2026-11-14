# A06 — Protección y restauración de texto

> **Estado: ESPECIFICADA, EN REVISIÓN. Solo documentación: no hay código, esquema, dependencias, pruebas ni versión de A06.** La implementación (Fase 2) empieza únicamente tras la **aprobación explícita del usuario** de este contrato. La aceptación final de A06 queda pendiente de su revisión.

- Fuente: `STAGE_3_PLAYBOOK.md` revisión 6 (§6 A06, §§4, 8–10 y 12; copia local ignorada por Git), más las decisiones posteriores ([A01](../decisions/A01-decisions.md), [A02](../decisions/A02-decisions.md), [A03](../decisions/A03-decisions.md), [A04](../decisions/A04-decisions.md), [A05](../decisions/A05-decisions.md), [A06](../decisions/A06-decisions.md)), que prevalecen.
- Repositorio: `cawbase-content-engine` · rama: `feature/a06-text-protection-restoration`, creada desde `main` en `f21baaf` (merge del PR #4 de A05; `main` = `origin/main` verificado con `git fetch`).
- Dependencias: A01–A05 cerradas y aceptadas; A05 mergeada (`0.4.0`, migración `0003`). No requiere modelo real (usa el mock de A03).
- Contrato permanente: [protección y restauración de texto](../text-protection.md). Este documento define alcance y aceptación y **enlaza**; no copia el contrato.
- Versión propuesta: `0.5.0` · migración propuesta: `0004_text_protection` · `schema_level` 4 · `min_runtime_level` 1 · extra nuevo: `protection` (`cryptography`) · permisos: 17 → 21.

## 1. Objetivo, alcance y exclusiones

**Objetivo.** Producir texto protegido y restaurarlo cuando corresponda, **por API Python**, con contextos persistentes, correspondencias cifradas y política estricta.

**Incluye:** contextos `conversation`/`job` con vigencia fija ([contrato §8](../text-protection.md#8-contextos)); `protect_text` con redacción irreversible y seudonimización reversible, unión determinista de hallazgos y precedencia de redacción ([§5](../text-protection.md#5-protección-protect_text)); marcadores y su parser ([§6](../text-protection.md#6-marcadores)); `restore_text` con autorización por respuesta, evaluación de datos nuevos y una sola pasada ([§7](../text-protection.md#7-restauración-restore_text)); fuentes de análisis vinculadas al texto exacto, también desde A05 ([§4](../text-protection.md#4-fuentes-de-análisis-y-vinculación-al-texto-exacto-4a)); cifrado autenticado, claves versionadas y rotación ([§9](../text-protection.md#9-criptografía-y-claves-3a)); persistencia y migración `0004` ([§10](../text-protection.md#10-persistencia-y-migración-0004-propuesta)); transacciones, bloqueos y concurrencia ([§11](../text-protection.md#11-transacciones-bloqueos-tiempo-y-concurrencia)); permisos y errores ([§12](../text-protection.md#12-permisos-y-errores)); limpieza y retención ([§8.5–8.6](../text-protection.md#85-limpieza-física-y-retención-e-15)).

**Excluye:** DOCX/PDF y aplicar sustituciones a archivos (A07/A08); inferencia real, calidad de detección y segmentación por modelo (A09); integración con CAWbase, UI, streaming y políticas de salida (bloque B); rutas HTTP y operaciones de protección/restauración en la cola durable; KMS obligatorio, recifrado automático y criptografía propia; ACL por contexto; borrado en copias de seguridad; promesas de anonimización o cumplimiento.

## 2. Decisiones

Aprobadas: **D-A06-01 (1A), D-A06-02 (2A), D-A06-03 (3A), D-A06-04 (4A)**, texto en [A06-decisions §2](../decisions/A06-decisions.md#2-decisiones-aprobadas-por-el-usuario). Elecciones técnicas `E-A06-01` a `28` en [§3](../decisions/A06-decisions.md#3-elecciones-técnicas-derivadas-e-a06-nn-no-son-decisiones-de-producto) (no son decisiones de producto). **Propuestas pendientes de revisión** `P-A06-01` a `06` en [§4](../decisions/A06-decisions.md#4-propuestas-pendientes-de-su-revisión-p-a06-nn). Hipótesis por verificar: [contrato §17](../text-protection.md#17-hipótesis-por-verificar-en-el-código-h-a06-nn).

## 3. Mapa del contrato

| Tema | Contrato |
|---|---|
| Fronteras, confianza y principios | [§1](../text-protection.md#1-fronteras-confianza-y-principios) |
| API Python y tipos | [§3](../text-protection.md#3-api-python-propuesta-no-disponible) |
| Fuentes de análisis y vinculación | [§4](../text-protection.md#4-fuentes-de-análisis-y-vinculación-al-texto-exacto-4a) |
| Protección: unión, acción, redacción, vacíos | [§5](../text-protection.md#5-protección-protect_text) |
| Marcadores, candidatos, literales | [§6](../text-protection.md#6-marcadores) |
| Restauración, autorización, datos nuevos | [§7](../text-protection.md#7-restauración-restore_text) |
| Contextos: creación, vigencia, eliminación, limpieza, backups | [§8](../text-protection.md#8-contextos) |
| Cifrado, claves, índice de igualdad, rotación | [§9](../text-protection.md#9-criptografía-y-claves-3a) |
| Persistencia, `0004`, compatibilidad, despliegue | [§10](../text-protection.md#10-persistencia-y-migración-0004-propuesta) |
| Tiempo, bloqueos, concurrencia, fallos | [§11](../text-protection.md#11-transacciones-bloqueos-tiempo-y-concurrencia) |
| Permisos y errores | [§12](../text-protection.md#12-permisos-y-errores) |
| Configuración y dependencias | [§13](../text-protection.md#13-configuración-y-dependencias) |
| Observabilidad y retención | [§14](../text-protection.md#14-observabilidad-y-retención) |
| Límites | [§15](../text-protection.md#15-límites-iniciales-elecciones-técnicas-no-mediciones-e-27) |
| Fuera de alcance y limitaciones | [§16](../text-protection.md#16-fuera-de-alcance-y-limitaciones-declaradas) |

## 4. Reglas de implementación (Fase 2, tras la aprobación)

1. Primer paso: incorporar las correcciones acordadas y **un commit local separado** para el contrato aprobado; después implementar sobre ese contrato.
2. El código vive en `src/cawbase_content_engine/protection/` con importación **perezosa**; `cryptography` solo en el extra `protection` y fuera de la importación base (H-A06-06). Reutiliza las capas de alcance, autorización, persistencia, errores, hora de la base y migraciones existentes; no copia lógica de A03 ni de A05.
3. El mock de A03 sustituye **solo** la inferencia. Criptografía, persistencia, bloqueos y permisos son **reales**; PostgreSQL y SQLite reales (bases descartables `cce_t_*`), incluido el piso de SQLite y PostgreSQL 15–18 según A01.
4. Sin cambios en los contratos públicos de A01–A05 ni en sus aserciones; si una aserción heredada cambia (p. ej. el tamaño del catálogo de permisos, la versión, el nivel de esquema) se **justifica** y se **preserva** la garantía que verificaba.
5. D-33 intacta (no se relajan umbrales; una reejecución no sustituye la primera medición; si falla, comparar base y rama en el mismo ambiente).
6. La evidencia separa **especificado ≠ implementado ≠ probado en macOS ≠ probado en Linux local ≠ probado en GitHub Actions ≠ aceptado**; un *skip* no es una aprobación; el informe será `docs/a06-implementation-report.md`.
7. Ningún fixture contiene datos reales ni secretos (`example.test`, valores evidentemente falsos). Las claves de las pruebas son aleatorias y descartables.
8. Pruebas de carrera con **sincronización controlada** (conexión independiente que retiene el bloqueo y detección de espera en `pg_stat_activity` en PostgreSQL; escritor único de SQLite), no con `sleep`.

## 5. Matriz de aceptación AC-A06 (para la implementación posterior)

Cada fila: **precondición** → **resultado esperado** → **evidencia prevista** (prueba y ambiente). «PG+SQ» = PostgreSQL y SQLite reales. El estado real (verificado, parcial, sin evidencia) irá en el informe de implementación; **nada de esta tabla está implementado todavía**.

### 5.1 Protección

| AC | Precondición | Resultado esperado | Evidencia prevista |
|---|---|---|---|
| AC-A06-01 | Contexto `conversation` vigente; texto con 3 hallazgos seudonimizables (mock) | `protect_text` devuelve texto con marcadores; `restore_text` (autorizado) devuelve el texto original **idéntico** (ida y vuelta) | E2E por backend, PG+SQ |
| AC-A06-02 | Hallazgo con acción `redact` | Aparece `[REDACTED]`; **no se crea ninguna fila** en `protection_mappings`; ningún camino restaura ese valor | prueba + inspección de tablas, PG+SQ |
| AC-A06-03 | Texto con no BMP, NFD (`e`+U+0301), ZWJ y `\r\n`, hallazgos en esas zonas | Offsets en puntos de código; sustitución exacta; CRLF preservados fuera de hallazgos | tabla de casos Unicode, PG+SQ |
| AC-A06-04 | El mismo valor 3 veces en un texto, y en dos llamadas del mismo contexto | Mismo marcador (3 sustituciones, 1 fila); en otro contexto, marcador distinto | prueba, PG+SQ |
| AC-A06-05 | Texto con detección `completed` y sin hallazgos | Texto idéntico, `outcome=no_findings`, sin filas; el contexto se valida igualmente | prueba, PG+SQ |
| AC-A06-06 | Texto con hallazgos | Regiones fuera de los grupos **byte-idénticas** (comparación de segmentos) | prueba basada en propiedades, PG+SQ |
| AC-A06-07 | Hallazgos con solapamiento parcial, anidado y transitivo (`[0,10)`,`[8,20)`,`[19,30)`) | Un grupo `[0,30)`; adyacentes `[0,5)`,`[5,9)` **no** se unen; idéntico resultado para **todas las permutaciones** del orden de entrada | prueba de la unión (pura) + E2E, PG+SQ |
| AC-A06-08 | Unión con un tipo `redact` y otro `pseudonymize` | Grupo entero `[REDACTED]`; sin correspondencia para ninguno de sus miembros | prueba, PG+SQ |
| AC-A06-09 | Detección `inconclusive` (`partial_coverage`, `abstained`, `truncated`), `not_requested` y `not_configured` | `detection_inconclusive` / `capabilities_not_configured`; sin texto, sin filas, sin contexto modificado | prueba por motivo, PG+SQ |
| AC-A06-10 | `InlineAnalysis` | El análisis usa el mismo `text`; `AnalysisRef` identifica perfil/versión y procedencia; la API **no** admite `AnalysisResult` ni offsets del llamador | prueba + comprobación de la firma |
| AC-A06-11 | `JobAnalysis` con un trabajo `text_analysis` de A05 vigente y el mismo texto | Éxito; `AnalysisRef.source = job` con la identidad del trabajo | E2E con trabajo real de A05, PG+SQ |
| AC-A06-12 | `JobAnalysis` con el resultado de **otro texto** (hash distinto), longitud distinta, o perfil inconsistente | `analysis_text_mismatch` / `analysis_source_invalid`; sin efectos | prueba, PG+SQ |
| AC-A06-13 | `JobAnalysis` con trabajo vencido (`result_expires_at`), eliminado (`delete_job_data`), `failed`/`cancelled`, de otro scope, operación `document_analysis`, o sin `results:read` | `job_data_expired` / `job_not_succeeded` / `resource_not_found` / `analysis_source_invalid` / `permission_denied` según el caso; sin efectos | prueba por caso, PG+SQ |
| AC-A06-14 | Entrada con marcador estricto o candidato laxo | `marker_collision` **antes** de la inferencia y de cualquier efecto | prueba, PG+SQ |
| AC-A06-15 | Límites: texto, fragmento, mappings por contexto, bytes por contexto, contextos por scope | `limit_exceeded`/`quota_exceeded`; nunca se trunca; el límite no se supera por carrera | prueba + carrera, PG+SQ |

### 5.2 Contextos y vigencia

| AC | Precondición | Resultado esperado | Evidencia prevista |
|---|---|---|---|
| AC-A06-16 | `create_context` sin y con `ttl_s`; fuera de rango | Vigencia fija 7 días por defecto (configurable); `ttl_s` en rango; fuera de rango `validation_failed`; id opaco `ctx_`+32 hex; tipo inmutable | prueba, PG+SQ |
| AC-A06-17 | Operaciones repetidas, lecturas y reintentos | `expires_at` **no cambia** nunca; no existe API de renovación | prueba de invariancia, PG+SQ |
| AC-A06-18 | Hora de la base exactamente en `expires_at` y a ±1 s | En `expires_at` ya vencido (`context_unavailable`); un instante antes, vigente | prueba con reloj de la base controlado, PG+SQ |
| AC-A06-19 | Contexto vencido sin limpieza | Efectivamente vencido: protect/restore fallan; `get_context` informa `expired`; los datos siguen físicamente hasta la limpieza | prueba, PG+SQ |
| AC-A06-20 | `run_protection_cleanup` | Elimina correspondencias y clave envuelta de vencidos; repetible (segunda pasada = 0); sin resurrección; purga lápidas tras 30 días; resumen sin contenido | prueba, PG+SQ |
| AC-A06-21 | `delete_context` en contexto activo; en vencido sin limpiar; repetido | Correspondencias y clave envuelta eliminadas de inmediato; idempotente; `restore_text` posterior falla aunque las claves existan; el id no se reactiva ni se reasigna | prueba + inspección de tablas, PG+SQ |
| AC-A06-22 | Scope deshabilitado | `create`/`protect`/`restore` ⇒ `scope_disabled`; `delete_context` y lecturas de metadatos permitidos | prueba, PG+SQ |

### 5.3 Aislamiento y permisos

| AC | Precondición | Resultado esperado | Evidencia prevista |
|---|---|---|---|
| AC-A06-23 | Dos scopes y dos contextos del mismo scope de distinto `kind` | Contexto de otro scope ⇒ `resource_not_found` (sin oráculo); `kind` equivocado ⇒ `context_kind_mismatch`; un marcador de otro contexto no se restaura | prueba, PG+SQ |
| AC-A06-24 | Claves con cada permiso por separado | `text:protect` sin `text:restore` (y viceversa) denegados; `contexts:read`/`contexts:delete` separados; `AdminContext` no accede; faltan permisos combinados (`analysis:execute`/`results:read`) ⇒ `permission_denied` | prueba de matriz, PG+SQ |
| AC-A06-25 | Catálogo y delegación | 21 permisos; emisión con subconjunto del emisor (D-27); grants previos sin cambio | prueba de regresión de permisos |

### 5.4 Restauración

| AC | Precondición | Resultado esperado | Evidencia prevista |
|---|---|---|---|
| AC-A06-26 | Respuesta con marcadores del contexto y `delivered_material`/`delivered_markers` que los incluyen | Se restauran exactamente esos marcadores; `counts` correctos | prueba, PG+SQ |
| AC-A06-27 | Marcador válido del contexto **no** presente en el material entregado | `marker_not_authorized`; sin texto; sin consulta a la base antes de la comprobación (verificado con registro de sentencias) | prueba, PG |
| AC-A06-28 | Marcador desconocido (sintaxis válida sin correspondencia) y marcador de otro contexto autorizado por el host | `marker_unknown`; sin texto parcial | prueba, PG+SQ |
| AC-A06-29 | Conjunto autorizado vacío con y sin marcadores en la respuesta | Con marcadores ⇒ `marker_not_authorized`; sin marcadores ⇒ `no_markers` (se evalúa igualmente) | prueba, PG+SQ |
| AC-A06-30 | Marcadores alterados reconocibles (minúsculas, espacios, un corchete, sin corchetes) | `marker_malformed`; sin texto parcial | prueba por variante, PG+SQ |
| AC-A06-31 | Alteraciones **indetectables** (traducción, ZWJ intercalado, corchetes de ancho completo) | Sin error; el texto queda **sin restaurar**; `authorized_unused` lo refleja; ningún original se concede | prueba que documenta el límite, PG+SQ |
| AC-A06-32 | Literal estricto no autorizado en la respuesta; `[REDACTED]` y texto que no es candidato | Literal ⇒ `marker_not_authorized`; `[REDACTED]` y prosa se devuelven tal cual | prueba, PG+SQ |
| AC-A06-33 | Original que contiene sintaxis de marcador | Se inserta **literal** y no se reinterpreta (una pasada) | prueba, PG+SQ |
| AC-A06-34 | Respuesta con datos sensibles **nuevos**, detección completa | `restored_with_redactions`: originales autorizados restaurados, nuevos `[REDACTED]`, `new_findings` sin valores; con `fail` ⇒ `new_sensitive_data` | prueba (adaptador de prueba), PG+SQ |
| AC-A06-35 | Respuesta con detección inconclusa | `detection_inconclusive`; **no se descifra nada** (verificado: ninguna lectura de correspondencias ni llamada a la clave) | prueba con instrumentación, PG+SQ |
| AC-A06-36 | Hallazgos dentro de, cruzando y cubriendo marcadores; cadenas | Dentro ⇒ ignorado; cruzando/cubriendo ⇒ grupo redactado, marcador no restaurado (`markers_consumed_by_redaction`) | prueba de la unión de restauración, PG+SQ |
| AC-A06-37 | Contexto borrado o vencido durante la restauración (antes del bloqueo) | `context_unavailable`; nunca texto restaurado | prueba, PG+SQ |
| AC-A06-38 | Respuesta mayor que el límite de texto de A03 | `limit_exceeded` (limitación documentada); nunca se trunca | prueba |

### 5.5 Criptografía, claves y persistencia

| AC | Precondición | Resultado esperado | Evidencia prevista |
|---|---|---|---|
| AC-A06-39 | Valores centinela sintéticos protegidos | Ningún centinela en claro ni su SHA-256 sin clave en ninguna tabla de A06, logs, errores ni diagnósticos; ciphertext distinto del claro; tamaños en múltiplos de 64 | barrido de tablas y logs, PG+SQ |
| AC-A06-40 | Rotación `v1`→`v2` | Registros viejos legibles; nuevos con `v2`; **marcador reutilizado** para el mismo valor tras rotar; un contexto con ambas versiones restaura todo | prueba, PG+SQ |
| AC-A06-41 | Clave de una versión ausente (restaurar, proteger, crear) | `protection_key_unavailable`; sin texto parcial; sin versión activa no se crea contexto | prueba, PG+SQ |
| AC-A06-42 | Registro alterado (ciphertext, nonce, salt, versión) y fila movida a otro scope/contexto/token/`kind` | `mapping_integrity_failed`; sin texto parcial | prueba de manipulación directa de filas, PG+SQ |
| AC-A06-43 | Fallo de persistencia (inyectado) durante la inserción y en el `COMMIT` (resultado desconocido) | `database_unavailable`; **sin texto devuelto**; sin marcadores visibles a medias; el reintento converge | inyección de fallos, PG+SQ |
| AC-A06-44 | Operación bajo `HostTransaction` | `transaction_incompatible` antes de efectos | prueba |
| AC-A06-45 | Diagnósticos y logs durante todos los casos | Sin texto, fragmentos, marcadores, claves, hashes ni offsets | captura de logs + barrido, PG+SQ |

### 5.6 Concurrencia

| AC | Precondición | Resultado esperado | Evidencia prevista |
|---|---|---|---|
| AC-A06-46 | Dos `protect_text` concurrentes del **mismo valor** en el mismo contexto | Un solo marcador y una sola fila; ambos textos contienen el mismo marcador | carrera con sincronización controlada, PG+SQ |
| AC-A06-47 | `restore_text` concurrente con `delete_context` y con el vencimiento | O restaura entero (decidió con el contexto vivo) o falla cerrado; **nunca** parcial ni tras una eliminación ya efectiva | carreras controladas, PG+SQ |
| AC-A06-48 | `protect_text` concurrente con `delete_context`/vencimiento | O todo confirmado antes de la eliminación o `context_unavailable`; no quedan filas huérfanas | carrera controlada, PG+SQ |
| AC-A06-49 | Registro de sentencias de las operaciones de A06 | Orden de bloqueos 1 → [8] → 11 → 12 → 13 ascendente en todos los caminos | registro de sentencias, PG |

### 5.7 Migración, compatibilidad y distribución

| AC | Precondición | Resultado esperado | Evidencia prevista |
|---|---|---|---|
| AC-A06-50 | Base en `0003` con datos de A01–A05 (scopes, perfiles, archivos, trabajos) | `0004` aditiva: datos intactos; `schema_level` 4; idempotente; reejecutar no cambia nada | migración sobre bases pobladas, PG+SQ |
| AC-A06-51 | Compatibilidad hacia atrás | Un runtime `0.4.0` arranca sobre nivel 4 (simulado con su constante) y no ve tablas de A06; un runtime `0.5.0` sobre nivel 3 ⇒ `schema_outdated`; sin `ProtectionSetup` el motor arranca y A06 ⇒ `protection_not_configured` | prueba, PG+SQ |
| AC-A06-52 | Regresiones A01–A05 | Suite completa sin cambios de garantía; aserciones heredadas modificadas (catálogo, versión, nivel) justificadas | suite completa, PG+SQ, macOS y Linux Docker |
| AC-A06-53 | Distribución: wheel base en venv limpio | `import cawbase_content_engine` no importa `cryptography`; D-33 intacta (mediana warm ≤ 300 ms en 15 procesos nuevos; cold informativa; ≤ 40 MiB); el wheel + extra `protection` ejecuta el ejemplo; sin HTTP ni cola nuevos | `pytest -m dist` + `scripts/measure_import.py` |
| AC-A06-54 | Superficie pública | Sin rutas HTTP nuevas ni operaciones de cola de A06; `analyze_text` y A05 sin cambios | prueba de enumeración de rutas y operaciones |
| AC-A06-55 | Documentación | Enlaces/anclas sin romper, nombres de permisos/códigos/estados/rutas consistentes en todos los documentos | comprobador de enlaces + revisión |

## 6. Entregables de la implementación

Código bajo `src/cawbase_content_engine/protection/`, migración `0004`, ejemplo de host Python, pruebas AC-A06, `docs/a06-implementation-report.md`, documentación permanente alineada, README/CLAUDE.md con el estado real, commits locales coherentes (contrato aprobado, implementación, pruebas, documentación final). El push y el merge los hace el usuario.
