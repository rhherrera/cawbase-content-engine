# A06 — Registro de decisiones

> **Estado: A06 ESPECIFICADA, EN REVISIÓN (revisión 1 del registro; solo documentación; sin código, esquema, dependencias, pruebas ni versión).** Las decisiones **1A, 2A, 3A y 4A** (`D-A06-01` a `D-A06-04`) fueron **aprobadas por el usuario** en el encargo de A06. Las elecciones técnicas `E-A06-nn` se derivaron por criterio al redactar el contrato y **no** son decisiones de producto; las propuestas `P-A06-nn` son las que afectan el comportamiento público y están **pendientes de su revisión**; las hipótesis `H-A06-nn` están por verificar en código ([contrato §17](../text-protection.md#17-hipótesis-por-verificar-en-el-código-h-a06-nn)).

Contratos que usan estas decisiones: [especificación de A06](../features/A06-proteccion-y-restauracion-de-texto.md) · [protección y restauración de texto](../text-protection.md) (contrato completo). Decisiones heredadas: [A01](A01-decisions.md), [A02](A02-decisions.md), [A03](A03-decisions.md), [A04](A04-decisions.md), [A05](A05-decisions.md).

Categorías (no mezclarlas): **decisiones aprobadas** (§2) · **elecciones técnicas derivadas** (§3) · **propuestas pendientes de revisión** (§4) · **diferencias con el playbook** (§5) · **backlog** (§6) · preguntas y tensiones (§7).

## 0. Historial de revisiones

| Rev. | Contenido |
|---|---|
| 1 | Primera redacción (solo documental): D-A06-01 a 04 (1A–4A) aprobadas; E-A06-01 a 28; P-A06-01 a 06 pendientes de revisión; H-A06-01 a 10; diferencias con el playbook y backlog. No se encontró una incompatibilidad material con 1A–4A que exija otra decisión de producto; sí limitaciones que se señalan (§7) y seis propuestas (§4). |

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
| E-A06-07 | Candidatos laxos L1/L2; alterado reconocido ⇒ `marker_malformed`; literal en la entrada ⇒ `marker_collision`; sin mecanismo de escape | §6.2–6.3 |
| E-A06-08 | Autorización por respuesta: `delivered_material` o `delivered_markers` (exactamente uno); comprobada **antes** de cualquier consulta a la base | §7.2 |
| E-A06-09 | Datos nuevos: `on_new_findings = redact` (por defecto, siempre irreversible) o `fail`; hallazgos dentro de un marcador se ignoran; los que cruzan o cubren un marcador forman un grupo redactado (ver P-A06-03) | §7.3 |
| E-A06-10 | La respuesta restaurada se evalúa con A03 ⇒ mismos límites de texto (8192 puntos de código por defecto) | §7.1, §16 |
| E-A06-11 | `kind` obligatorio en cada operación y parte del AAD; sin ACL por contexto: acceso = scope + `context_id` + `kind` + estado (ver P-A06-06) | §8.1 |
| E-A06-12 | `ttl_s` por llamada en `[60, max_context_ttl_s]`; máximo 30 días por defecto; tope duro 3650 días | §8.2 |
| E-A06-13 | Vencido ⇔ `hora_de_la_base ≥ expires_at`; `expires_at` inmutable | §8.2–8.3 |
| E-A06-14 | `delete_context`: borrado de correspondencias y de la clave de índice envuelta en la misma transacción; lápida de metadatos 30 días | §8.4 |
| E-A06-15 | `maintenance().run_protection_cleanup()` y CLI `cleanup-protection` propios; no modifican `run_cleanup` de A05; no requieren claves | §8.5 |
| E-A06-16 | `cryptography` (extra `protection`), AES-256-GCM, subclave HKDF-SHA256 por registro, relleno a 64 B; importación perezosa | §9.2, §13 |
| E-A06-17 | Índice de igualdad = HMAC-SHA256 con **clave de índice por contexto** envuelta bajo la versión activa al crear el contexto; estable ante la rotación | §9.3 |
| E-A06-18 | `KeyProvider` **síncrono y en memoria** (`active_version`, `key`); `KeyRing` estático y `from_directory` | §9.1 |
| E-A06-19 | AAD = (`cce.protection.map.v1`, scope, contexto, `kind`, token, versión) con prefijos de longitud | §9.2 |
| E-A06-20 | Bloqueos: rangos 11–13 tras el orden de A05; proteger `FOR NO KEY UPDATE` sobre el contexto, restaurar `FOR SHARE`, eliminar `FOR UPDATE` | §11.2–11.3 |
| E-A06-21 | Scope deshabilitado bloquea crear/proteger/restaurar; permite eliminar y leer metadatos (ver P-A06-04) | §11.4 |
| E-A06-22 | Cuatro permisos nuevos (`text:protect`, `text:restore`, `contexts:read`, `contexts:delete`; catálogo 21) y combinaciones con `analysis:execute`/`results:read` (ver P-A06-05) | §12.1 |
| E-A06-23 | Todas las operaciones de A06 rechazan `HostTransaction` (`transaction_incompatible`) | §3, §11.3 |
| E-A06-24 | Versión `0.5.0`, migración `0004_text_protection`, `schema_level` 4, `min_runtime_level` 1 | §10 |
| E-A06-25 | Errores nuevos (13 códigos, solo Python) | §12.2 |
| E-A06-26 | Sin eventos en `admin_events`; observabilidad solo con conteos, ids y códigos | §14 |
| E-A06-27 | Límites iniciales de §15 (elecciones, no mediciones) | §15 |
| E-A06-28 | `protect_text` sin clave de idempotencia: convergente por deduplicación de marcadores | §11.3 |

## 4. Propuestas pendientes de su revisión (`P-A06-nn`)

Son elecciones resueltas por criterio que **cambian lo que un integrador observa**. Se presentan con su recomendación; **no se tratan como decisiones** hasta que las apruebe o las cambie.

| ID | Propuesta | Alternativa | Consecuencia de cada camino |
|---|---|---|---|
| P-A06-01 | Solo `InlineAnalysis` y `JobAnalysis`: el motor **no** acepta hallazgos o `AnalysisResult` suministrados por el host | Aceptar un `AnalysisResult` del llamador | Un resultado en memoria no demuestra a qué texto pertenece (A03 §6.4); aceptarlo debilita la vinculación de 4A. Costo del camino propuesto: un host que ya analizó debe volver a analizar (inline) o enviar un trabajo de A05 |
| P-A06-02 | Marcador **sin tipo** (`[[CCE1:TOKEN]]`) | Marcador con el tipo (`[[CCE1:tipo:TOKEN]]`) | Sin tipo: el modelo no recibe la categoría, la unión de grupos de distintos tipos es trivial y la identidad es el fragmento exacto. Con tipo: el modelo razona mejor sobre qué representa cada marcador, pero filtra la clasificación y complica la identidad en las uniones (backlog B-A06-03) |
| P-A06-03 | Datos nuevos en respuestas: **redactar** por defecto (u `on_new_findings="fail"`), y la respuesta se limita a los **límites de texto de A03** (8192 puntos de código) | Segmentar respuestas largas dentro del motor; o hacer `fail` el único modo | Restaurar respuestas mayores de 8192 requiere que el operador amplíe los límites de análisis (consistentes con el modelo) o segmentar fuera del motor; la segmentación segura pertenece a A09. Redactar es el comportamiento más conservador que no rechaza la respuesta |
| P-A06-04 | Scope deshabilitado ⇒ `scope_disabled` para crear, proteger **y restaurar**; permitir eliminar y leer metadatos | Permitir restaurar (lectura) como A05 permite leer resultados | Restaurar devuelve valores reversibles sensibles: se prefiere cerrar el acceso con el scope. Consecuencia: la respuesta pendiente de un scope deshabilitado no se restaura |
| P-A06-05 | **Cuatro** permisos nuevos (catálogo 21), con combinaciones explícitas | Reutilizar `data:delete` para eliminar y `scope:read` para metadatos | Más precisión (restaurar ≠ proteger ≠ borrar contextos ≠ leerlos) a costa de más grants que emitir y de ampliar la restricción de despliegue |
| P-A06-06 | **Sin ACL por contexto**: acceso = scope + id + tipo + vigencia | Un propietario opaco (`owner_ref`) por contexto comprobado en cada operación | Con ACL por contexto el motor podría impedir que un usuario del mismo scope use la conversación de otro, pero necesitaría que el host entregue y mantenga una identidad que el motor no conoce (regla 1 de CLAUDE.md). Propuesto: es responsabilidad del host decidir qué contextos entrega a cada usuario |

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
| B-A06-02 | Mecanismo de escape de literales tipo marcador; reducir falsos positivos de «CCE» | decisión de producto |
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

1. **Sin incompatibilidad material con 1A–4A.** Se señalan dos **tensiones de alcance**, no resueltas con una decisión nueva: (a) restaurar respuestas de IA exige evaluar la respuesta con A03, cuyo límite de 8192 puntos de código puede ser menor que el de una respuesta real (P-A06-03); (b) «acceso al contexto específico» no puede ser más fino que scope + id + tipo sin una identidad de usuario que el motor no tiene (P-A06-06).
2. **La atomicidad entre disponibilidad de correspondencias y entrega del texto** obliga a rechazar `HostTransaction` en todas las operaciones de A06 (E-A06-23). Un host que ya trabaje con su propia transacción debe confirmar las correspondencias en la del motor antes de usar el texto.
3. **Cuota de contextos y cadencia de limpieza**: valores iniciales (§15 del contrato); no son mediciones (H-A06-02).
4. **La rotación obliga a conservar claves viejas** durante la vida de los contextos y de los backups que las necesiten; la política de retiro es del operador.
5. La aceptación final de A06 queda **pendiente de la revisión del usuario**; ninguna fase la declara.
