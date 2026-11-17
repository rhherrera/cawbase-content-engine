# A06 — Registro de decisiones

> **Estado: A06 IMPLEMENTADA en `0.5.0` (revisión 3 del registro); la aceptación queda pendiente de la revisión del usuario.** Las decisiones **1A, 2A, 3A y 4A** (`D-A06-01` a `D-A06-04`) y las propuestas `P-A06-01` a `06` (con las precisiones del usuario, §4) están **aprobadas por el usuario**, que además **aprobó explícitamente el contrato revisado (rev. 2, commit `17b545d`)** y autorizó la Fase 2. Las elecciones técnicas `E-A06-nn` se derivaron por criterio y **no** son decisiones de producto; las hipótesis `H-A06-nn` se verificaron en código (resultado en el [informe](../a06-implementation-report.md)). Ninguna decisión aprobada se modificó al implementar.

Contratos que usan estas decisiones: [especificación de A06](../features/A06-proteccion-y-restauracion-de-texto.md) · [protección y restauración de texto](../text-protection.md) (contrato completo). Decisiones heredadas: [A01](A01-decisions.md), [A02](A02-decisions.md), [A03](A03-decisions.md), [A04](A04-decisions.md), [A05](A05-decisions.md).

Categorías (no mezclarlas): **decisiones aprobadas** (§2) · **elecciones técnicas derivadas** (§3) · **propuestas aprobadas por el usuario** (§4) · **diferencias con el playbook** (§5) · **backlog** (§6) · preguntas y tensiones (§7).

## 0. Historial de revisiones

| Rev. | Contenido |
|---|---|
| 1 | Primera redacción (solo documental): D-A06-01 a 04 (1A–4A) aprobadas; E-A06-01 a 28; P-A06-01 a 06 pendientes de revisión; H-A06-01 a 10; diferencias con el playbook y backlog. No se encontró una incompatibilidad material con 1A–4A que exija otra decisión de producto; sí limitaciones que se señalan (§7) y seis propuestas (§4). |
| 2 | Revisión del usuario de `a74e148`: **P-A06-01, 02, 04 y 05 aprobadas**; **P-A06-03 aprobada** con la semántica de `fail` detallada (§4); **P-A06-06 aprobada** con la aclaración de que `text:restore` tiene alcance de scope en el motor y el host comprueba el acceso a la conversación o trabajo concreto, sin ACL internas. Cambios al contrato: (1) restauración: clasificación de hallazgos ignorados/nuevos, modos `fail` y `redact`, cruces transitivos (contrato §7.1, §7.3); (2) parser acotado con clases S/R1/R2 que acepta prosa ordinaria como «[CCE report]» (§5.7, §6.2–6.3); (3) índice de igualdad autenticado como AAD y verificación previa a toda reutilización de marcador (§9.2–9.4, §12.2); (4) garantía de reintento acotada a la deduplicación del fragmento exacto (§11.3). Nuevas: E-A06-29, E-A06-30, H-A06-11/12, AC-A06-56 a 63; reescritos AC-A06-14, 30–32, 34, 36, 42 y 43; B-A06-02 acotada. **No** se tocó código ni se abrió la Fase 2. |
| 3 | Implementación (Fase 2) sobre el contrato aprobado: sin cambios de decisiones; las dos filas de la matriz que el usuario precisó al aprobar (AC-A06-59 y AC-A06-62(a)) se alinearon en `7fa33e3`; E-A06-01 a 30 aplicadas tal como están; H-A06-01 a 12 con su resultado en el informe. Notas de implementación en [contrato §18](../text-protection.md#18-notas-de-implementación). |

## 1. Fuente, precedencia y herencia

- Referencia: `STAGE_3_PLAYBOOK.md`, revisión 6 (copia local ignorada por Git; **no se modifica ni se declara actualizada**): §6 A06, §4 (protocolo), §8 (límites de la secuencia), §9 (principios), §10 (checklist), §12 (decisiones pendientes: «claves, correspondencias y alcance exacto de restauración» → A06). Las decisiones posteriores prevalecen donde difieren (§5).
- Base verificada: `main` = `origin/main` = `f21baaf` (merge del PR #4 de A05, `0.4.0`, migración `0003`); `git fetch origin` exitoso. El remoto era accesible.
- A01–A05 **cerradas y aceptadas**; A05 mergeada. No se reabre ninguna decisión. Heredado **sin cambios**: A01 D-06/D-07, D-08, D-11/D-33, D-13/D-14/D-23, D-15/D-16/D-24, D-17/D-27, D-18/D-25, D-30; A02 D-A02-02 y la selección de A02 §8; A03 D-A03-01 (`analyze_text` en la misma llamada), D-A03-02, D-A03-03 y los offsets en puntos de código; A04 D-A04-01 a 04 (en particular D-A04-04: A06 no lo toca); A05 D-A05-01, D-A05-02 y D-A05-03 (retención, cambios administrativos y *fence*: **intactos**).
- **D-33 intacta:** mediana warm ≤ 300 ms en 15 procesos nuevos, cold informativa, tamaño del entorno base ≤ 40 MiB. **Antecedente informado por el usuario (no verificado por el agente):** la primera ejecución del check de distribución en GitHub Actions sobre A05 midió una mediana por encima de 300 ms y pasó al reejecutarlo con el límite original. Una reejecución no sustituye la primera medición; si A06 repite el fallo se conserva el resultado y se compara base y rama en el mismo ambiente.

## 2. Decisiones aprobadas por el usuario

### D-A06-01 — 1A: API Python, contextos persistentes y análisis de A05

API pública **Python** para proteger y restaurar texto, usando el aislamiento y las vías de autorización existentes del motor. Contextos persistentes de tipo **conversación** o **trabajo**. El resultado **autorizado y vigente** de `text_analysis` de A05 puede aportar el análisis. A06 queda acotada a la API Python: **sin rutas HTTP nuevas ni operaciones nuevas de protección/restauración en la cola durable**. Se conservan los contratos, la expiración y el *fence* de A05. Un `SourceMap` sigue representando posiciones documentales.

### D-A06-02 — 2A: contexto opaco, ligado a scope y tipo, vigencia fija

Contexto opaco **ligado al scope y al tipo**, con **vigencia fija desde su creación**: 7 días por defecto, configurable, **independiente** de los plazos de originales y resultados de A05. El uso, los reintentos y la lectura **no renuevan** el plazo. El **mismo fragmento exacto** seudonimizado **reutiliza su marcador** dentro del contexto, también ante operaciones concurrentes. Expiración **efectiva al vencer** aunque la limpieza física se ejecute después. Eliminación **explícita e irreversible** desde el motor. Un contexto borrado o vencido **no se reactiva ni se reasigna**. (Qué se elimina, cuándo y qué ocurre con backups: [contrato §8.6](../text-protection.md#86-qué-se-elimina-cuándo-y-backups-2a).)

### D-A06-03 — 3A: claves versionadas del host, cifrado autenticado

**Claves versionadas** que suministra el host u operador **fuera de la base de correspondencias**. **Cifrado autenticado** con una **biblioteca mantenida**. Cada registro se vincula criptográficamente a su **scope, contexto y marcador**. La **versión activa** cifra las correspondencias nuevas; las **anteriores** siguen disponibles para leer hasta el vencimiento de los contextos que las usan. **Clave ausente o autenticación fallida ⇒ fallo cerrado.** **Sin KMS obligatorio, recifrado automático ni criptografía propia.** Si se necesita un índice de igualdad, sin valores en claro ni hashes sin clave de valores sensibles; el contrato explica cómo conserva la identidad del marcador al rotar ([contrato §9.3](../text-protection.md#93-índice-de-igualdad-y-rotación-e-17)).

### D-A06-04 — 4A: política estricta

**Política estricta.** Se consumen hallazgos **validados sobre el texto exacto**, con offsets de A03 en **puntos de código Unicode** y versión de perfil identificable; la vinculación se garantiza por construcción (análisis inline) o por identidad del texto (resultado de A05), **no** por la mera validez de los offsets. **Unión determinista** de intervalos solapados, incluidos los transitivos; si **algún** hallazgo de la unión exige redacción, se redacta la unión entera **sin** correspondencia reversible. Una detección **inconclusa** no habilita una salida declarada protegida. **Límite por ocurrencia** documentado. **Restauración** con permiso distinto al de proteger y acceso al contexto específico; en respuestas de IA, **solo** marcadores exactos emitidos en ese contexto **y** presentes en el material protegido que el host entregó al modelo para esa respuesta (el host es la fuente confiable; el contenido del modelo no la establece ni la amplía); marcador desconocido, alterado reconocido como candidato, de otro contexto o ajeno al conjunto autorizado ⇒ **fallo cerrado sin devolución parcial**; gramática, literales y alteraciones no detectables definidos; restauración **en una sola pasada**; **datos sensibles nuevos** de la respuesta evaluados **antes** de reinsertar originales, integrados con la política de protección; detección inconclusa ⇒ no se entrega texto restaurado; con detección completa y datos nuevos se mantiene la política estricta sin asumir que están autorizados por venir del modelo. A06 cubre texto; DOCX/PDF y sustituciones en archivos son de A07/A08; inferencia real y calidad, de A09; integración con CAWbase, UI, streaming y políticas de salida, del bloque B. **El LLM no recibe claves ni correspondencias y no restaura originales.**

## 3. Elecciones técnicas derivadas (`E-A06-nn`; no son decisiones de producto)

| ID | Elección | Dónde |
|---|---|---|
| E-A06-01 | Fuentes de análisis: `InlineAnalysis` y `JobAnalysis`; **no** se aceptan hallazgos ni `AnalysisResult` suministrados por el llamador (ver P-A06-01) | contrato §3.1, §4 |
| E-A06-02 | Vinculación de `JobAnalysis`: SHA-256 y tamaño de los bytes UTF-8 del texto contra la entrada fijada del trabajo, longitud en puntos de código, perfil fijado, estado y vigencia re-comprobados **en la transacción de decisión** con el trabajo bloqueado `FOR SHARE` | §4.2 |
| E-A06-03 | Política explícita: `ProtectionPolicy(default_action obligatoria, actions por type_id)`; solo `pseudonymize` y `redact`; **sin acción «dejar pasar»** | §3.1 |
| E-A06-04 | Unión: ordenar siempre, solapamiento **estricto** (`start < group_end`), cierre transitivo; los **adyacentes no se unen** | §5.2 |
| E-A06-05 | Redacción = constante `[REDACTED]` (igual para todos los tipos; inerte en la restauración) | §5.3 |
| E-A06-06 | Marcador `[[CCE1:<16 base32 Crockford>]]` (80 bits aleatorios, ASCII, 25 puntos de código), **sin tipo** (ver P-A06-02) | §6.1 |
| E-A06-07 | **(rev. 2)** Parser acotado de tres clases con `re.ASCII`: S (estricto), R1 (núcleo `cce<versión>:<token de 16>`) y R2 (prefijo `[[CCE<versión>:`); la prosa ordinaria («[CCE report]», «CCE1», `[[CCE1]]`) no es candidata. Alteración reconocible (R1/R2) ⇒ `marker_malformed` en la respuesta y `marker_collision` en la entrada; literal estricto en la entrada ⇒ `marker_collision`; sin mecanismo de escape. Sustituye a los candidatos laxos L1/L2 de la revisión 1, que rechazaban cualquier `[CCE…` | §5.7, §6.2–6.3 |
| E-A06-08 | Autorización por respuesta: `delivered_material` o `delivered_markers` (exactamente uno); comprobada **antes** de cualquier consulta a la base | §7.2 |
| E-A06-09 | **(rev. 2)** Datos nuevos: un hallazgo que coincide exactamente con un marcador o está enteramente dentro de **uno** se ignora; cualquier otro es nuevo. `fail` ⇒ `new_sensitive_data` antes de descifrar; `redact` (por defecto, siempre irreversible) ⇒ unión transitiva de los hallazgos nuevos y los marcadores que tocan; los marcadores consumidos no se consultan ni se restauran (P-A06-03, aprobada) | §7.1, §7.3 |
| E-A06-10 | La respuesta restaurada se evalúa con A03 ⇒ mismos límites de texto (8192 puntos de código por defecto) | §7.1, §16 |
| E-A06-11 | `kind` obligatorio en cada operación y parte del AAD; sin ACL por contexto: acceso = scope + `context_id` + `kind` + estado; el host comprueba el acceso a la conversación/trabajo concreto antes de delegar (P-A06-06, aprobada) | §8.1 |
| E-A06-12 | `ttl_s` por llamada en `[60, max_context_ttl_s]`; máximo 30 días por defecto; tope duro 3650 días | §8.2 |
| E-A06-13 | Vencido ⇔ `hora_de_la_base ≥ expires_at`; `expires_at` inmutable | §8.2–8.3 |
| E-A06-14 | `delete_context`: borrado de correspondencias y de la clave de índice envuelta en la misma transacción; lápida de metadatos 30 días | §8.4 |
| E-A06-15 | `maintenance().run_protection_cleanup()` y CLI `cleanup-protection` propios; no modifican `run_cleanup` de A05; no requieren claves | §8.5 |
| E-A06-16 | `cryptography` (extra `protection`), AES-256-GCM, subclave HKDF-SHA256 por registro, relleno a 64 B; importación perezosa | §9.2, §13 |
| E-A06-17 | Índice de igualdad = HMAC-SHA256 con **clave de índice por contexto** envuelta bajo la versión activa al crear el contexto; estable ante la rotación. **(rev. 2)** La columna solo **encuentra** candidatos: su valor es AAD del registro y toda reutilización se verifica (E-A06-29) | §9.3 |
| E-A06-18 | `KeyProvider` **síncrono y en memoria** (`active_version`, `key`); `KeyRing` estático y `from_directory` | §9.1 |
| E-A06-19 | AAD = (`cce.protection.map.v1`, scope, contexto, `kind`, token, versión, **`equality_index`** [rev. 2]) con prefijos de longitud | §9.2 |
| E-A06-20 | Bloqueos: rangos 11–13 tras el orden de A05; proteger `FOR NO KEY UPDATE` sobre el contexto, restaurar `FOR SHARE`, eliminar `FOR UPDATE` | §11.2–11.3 |
| E-A06-21 | Scope deshabilitado bloquea crear/proteger/restaurar; permite eliminar y leer metadatos (ver P-A06-04) | §11.4 |
| E-A06-22 | Cuatro permisos nuevos (`text:protect`, `text:restore`, `contexts:read`, `contexts:delete`; catálogo 21) y combinaciones con `analysis:execute`/`results:read` (ver P-A06-05) | §12.1 |
| E-A06-23 | Todas las operaciones de A06 rechazan `HostTransaction` (`transaction_incompatible`) | §3, §11.3 |
| E-A06-24 | Versión `0.5.0`, migración `0004_text_protection`, `schema_level` 4, `min_runtime_level` 1 | §10 |
| E-A06-25 | Errores nuevos (13 códigos, solo Python) | §12.2 |
| E-A06-26 | Sin eventos en `admin_events`; observabilidad solo con conteos, ids y códigos | §14 |
| E-A06-27 | Límites iniciales de §15 (elecciones, no mediciones) | §15 |
| E-A06-28 | **(rev. 2)** `protect_text` sin clave de idempotencia: la deduplicación conserva el marcador del **mismo fragmento exacto** en el mismo contexto; que el resultado completo se repita depende además del análisis efectivo y de la política | §11.3 |
| E-A06-29 | **(rev. 2)** Antes de reutilizar un marcador por coincidencia de índice: obtener la clave de la versión de la fila, descifrar y autenticar con el AAD completo y comparar el original con el fragmento exacto (tiempo constante). Fallo de autenticación o de comparación ⇒ `mapping_integrity_failed`; clave anterior ausente (también la de la clave de índice) ⇒ `protection_key_unavailable`; en ambos casos sin texto, sin marcador nuevo «en su lugar» y sin degradar a redacción. La clave de índice solo se necesita si hay algo que seudonimizar. Límite declarado: un índice movido a un valor que nadie calcula crea un segundo marcador, pero la restauración de esa fila falla | §9.3.1, §9.4 |
| E-A06-30 | **(rev. 2)** Clasificación y unión de hallazgos en restauración como funciones **puras** y deterministas (ordenar siempre; solapamiento estricto; adyacentes no se tocan; ignorados no participan), ejecutadas **antes** de la transacción de decisión; `outcome`: `restored_with_redactions` si hay ≥ 1 grupo redactado | §7.1, §7.3 |

## 4. Propuestas aprobadas por el usuario (`P-A06-nn`)

Elecciones que **cambian lo que un integrador observa**. El usuario las **aprobó** en la revisión de `a74e148` (rev. 2 de este registro). Conservan su identificador `P-A06-nn` por trazabilidad; solo el usuario puede cambiarlas. **Su aprobación no autoriza la implementación**: falta la aprobación explícita del contrato revisado.

| ID | Propuesta aprobada | Precisión del usuario (rev. 2) | Alternativa descartada |
|---|---|---|---|
| P-A06-01 | Solo `InlineAnalysis` y `JobAnalysis`: el motor **no** acepta hallazgos o `AnalysisResult` suministrados por el host | Aprobada tal cual | Aceptar un `AnalysisResult` del llamador (un resultado en memoria no demuestra a qué texto pertenece, A03 §6.4) |
| P-A06-02 | Marcador **sin tipo** (`[[CCE1:TOKEN]]`) | Aprobada tal cual | Marcador con el tipo (backlog B-A06-03) |
| P-A06-03 | Datos nuevos en respuestas: **redactar** por defecto (u `on_new_findings="fail"`); la respuesta se limita a los límites de texto de A03 (8192 puntos de código) | **Semántica de `fail` y de la clasificación:** un hallazgo que coincide exactamente con un marcador o está enteramente dentro de él **se ignora**; si abarca texto fuera del marcador hay **dato nuevo**. Con `fail` se rechaza **toda** la respuesta **antes de descifrar**. Con `redact` se redacta la unión y los marcadores consumidos **no se restauran**. Debe especificarse y trazarse en los criterios la coincidencia exacta, el hallazgo interior y los cruces transitivos, en ambos modos (contrato §7.3; AC-A06-34, 36, 56–58) | Segmentar respuestas largas dentro del motor (A09); `fail` como único modo |
| P-A06-04 | Scope deshabilitado ⇒ `scope_disabled` para crear, proteger **y restaurar**; permitir eliminar y leer metadatos | Aprobada tal cual | Permitir restaurar como lectura |
| P-A06-05 | **Cuatro** permisos nuevos (catálogo 21), con combinaciones explícitas | Aprobada tal cual | Reutilizar `data:delete` y `scope:read` |
| P-A06-06 | **Sin ACL por contexto**: acceso = scope + id + tipo + vigencia | **`text:restore` tiene alcance de scope en el motor;** el **host comprueba el acceso a la conversación o trabajo concreto antes de delegar** la operación. **No se agregan ACL internas** (contrato §1, §8.1; AC-A06-63) | Propietario opaco (`owner_ref`) por contexto (B-A06-07) |

## 5. Diferencias respecto del playbook (revisión 6)

| # | Playbook | A06 | Motivo |
|---|---|---|---|
| 1 | «ámbitos de conversación/trabajo» | Contextos `conversation` y `job` con vigencia propia, no atados a trabajos de A05 | D-A06-01/02 |
| 2 | «cifrado y custodia de claves» | Claves del host fuera de la base, sin KMS obligatorio; no hay custodia por el motor | D-A06-03 |
| 3 | «concurrencia y sustituciones deterministas» | Un fragmento exacto ⇒ un marcador por contexto; unión determinista de intervalos | D-A06-02/04 |
| 4 | «A06 usa hallazgos controlados» (§8) | Hallazgos de A03 (mock o modelo) o resultado de A05; nunca suministrados | E-A06-01 / P-A06-01 |
| 5 | §9: «no afirmar borrado inmediato si backups conservan datos» | El contrato lo declara explícitamente (§8.6) | §9 del playbook |

## 6. Backlog (fuera de los criterios de aceptación)

| ID | Tema | Dependencia |
|---|---|---|
| B-A06-01 | Rutas HTTP y operaciones de cola para proteger/restaurar | decisión posterior (1A las excluye) |
| B-A06-02 | Mecanismo de escape de literales tipo marcador (los falsos positivos de prosa como «[CCE report]» ya quedaron acotados en la rev. 2; quedan los residuales de §6.2 del contrato) | decisión de producto |
| B-A06-03 | Etiquetas semánticas en el marcador | P-A06-02 |
| B-A06-04 | Segmentación de respuestas largas y reconciliación de marcadores entre segmentos | A09 |
| B-A06-05 | Restauración de texto emitido por el propio motor sin evaluar datos nuevos | decisión de producto |
| B-A06-06 | Protección de todas las apariciones iguales de un valor (por valor, no por ocurrencia) | decisión de producto + A09 |
| B-A06-07 | ACL por contexto / propietario opaco | P-A06-06 |
| B-A06-08 | Recifrado asistido, KMS/envelope gestionado, verificación de custodia | decisión del usuario |
| B-A06-09 | Aplicar sustituciones a DOCX/PDF y restitución | A07/A08 |
| B-A06-10 | Calidad de detección y umbrales de recall para datos sensibles | A09 |
| B-A06-11 | Integración con CAWbase, UI, streaming y política de salida | bloque B |
| B-A06-12 | Verificación estática del orden de bloqueos (heredado de B-A05-16) | ingeniería |

## 7. Preguntas abiertas y tensiones

1. **Sin incompatibilidad material con 1A–4A.** Se señalan dos **tensiones de alcance**, no resueltas con una decisión nueva: (a) restaurar respuestas de IA exige evaluar la respuesta con A03, cuyo límite de 8192 puntos de código puede ser menor que el de una respuesta real (P-A06-03); (b) «acceso al contexto específico» no puede ser más fino que scope + id + tipo sin una identidad de usuario que el motor no tiene: resuelta por el usuario en P-A06-06 (alcance de scope en el motor; el host comprueba el acceso a la conversación o trabajo concreto antes de delegar).
2. **La atomicidad entre disponibilidad de correspondencias y entrega del texto** obliga a rechazar `HostTransaction` en todas las operaciones de A06 (E-A06-23). Un host que ya trabaje con su propia transacción debe confirmar las correspondencias en la del motor antes de usar el texto.
3. **Cuota de contextos y cadencia de limpieza**: valores iniciales (§15 del contrato); no son mediciones (H-A06-02).
4. **La rotación obliga a conservar claves viejas** durante la vida de los contextos y de los backups que las necesiten; la política de retiro es del operador.
5. La aceptación final de A06 queda **pendiente de la revisión del usuario**; ninguna fase la declara.
