# Protección y restauración de texto — contrato de A06

> **Estado: ESPECIFICADO, EN REVISIÓN (revisión 1 del contrato). No hay código, esquema, dependencias, pruebas ni versión de A06.** El motor sigue en `0.4.0` / migración `0003` (A05, aceptada y mergeada en `main`, merge `f21baaf`). Todo lo que aquí figura como «propuesto» pasa a «implementado» solo cuando exista código y pruebas, y solo tras la aprobación explícita del usuario de este contrato. Alcance y matriz de aceptación: [especificación de A06](features/A06-proteccion-y-restauracion-de-texto.md). Decisiones: [A06-decisions](decisions/A06-decisions.md) (**1A–4A aprobadas por el usuario**; `E-A06-nn` elecciones técnicas propuestas; `P-A06-nn` pendientes de revisión; `H-A06-nn` hipótesis por verificar en código).

Relacionado: [API pública](public-api.md) · [seguridad y aislamiento](security-and-isolation.md) · [persistencia y migraciones](persistence-and-migrations.md) · [instalación y operación](installation-and-operations.md) · [inferencia y análisis (A03)](inference-and-analysis.md) · [trabajos, archivos y ciclo de vida (A05)](jobs-files-and-lifecycle.md) · [procesamiento documental](document-processing.md).

**Convención de este documento.** Cada regla indica su origen entre corchetes: **[1A]**–**[4A]** decisión aprobada por el usuario; **[E-nn]** elección técnica propuesta (`E-A06-nn`); **[P-nn]** propuesta pendiente de su revisión; **[H-nn]** hipótesis que el código debe confirmar o refutar. Los bloques de código son **ilustrativos** (propuestos / no disponibles).

## 0. Resumen

A06 añade, **solo como API Python** [1A], la capacidad de:

1. crear **contextos** persistentes opacos (`conversation` o `job`) ligados a un scope y a un tipo, con vigencia fija desde su creación [2A];
2. **proteger** un texto: cada hallazgo validado se **redacta** (irreversible) o se **seudonimiza** (marcador reversible dentro del contexto) según una política estricta explícita [4A];
3. **restaurar** los marcadores de una respuesta de IA a sus valores originales, solo con un permiso distinto, solo con marcadores emitidos en ese contexto y presentes en el material que el host entregó al modelo, y solo después de evaluar los datos sensibles nuevos de la respuesta [4A];
4. guardar las correspondencias **cifradas** con claves versionadas suministradas por el host u operador fuera de la base [3A];
5. **eliminar** un contexto de forma explícita e irreversible, o dejarlo vencer; la limpieza física es posterior y repetible [2A].

```text
host ──► ScopedOperations.create_context / protect_text / restore_text / delete_context  (Python, sin HTTP, sin cola)
            │ autoriza (permiso + scope + tipo) ─ valida entrada ─ obtiene hallazgos vinculados al texto exacto (§4)
            │ ── sin transacción abierta: inferencia (inline) o lectura del resultado de A05 ──
            └─► UNA transacción de decisión: scope → [trabajo] → contexto → correspondencias; hora de la base DESPUÉS de las esperas
                     │ cifra / descifra con las claves del host (en memoria, fuera de la base)
                     └─► commit ─► solo entonces se devuelve el texto
```

Lo que **no** hace: no restaura sin datos sensibles evaluados, no usa al LLM como almacén ni como mecanismo de restauración (el LLM no recibe claves ni correspondencias), no ofrece rutas HTTP ni operaciones de cola, no procesa DOCX/PDF (A07/A08), no mide calidad de detección (A09), no decide políticas de salida del producto (bloque B), no promete anonimización ni cumplimiento normativo.

## 1. Fronteras, confianza y principios

| Frontera | Quién confía en quién |
|---|---|
| Host → motor | El motor confía en que el host construyó el `ScopeContext` tras autorizar (A01 §2.1). **El host es la fuente confiable de la autorización por respuesta** [4A]: qué marcadores se entregaron al modelo para esa respuesta (§7.2). El contenido devuelto por el modelo **nunca** establece ni amplía esa autorización |
| Operador → motor | Suministra las claves de cifrado **fuera de la base** [3A]; el motor no las genera, no las guarda y no las registra |
| Motor → LLM | El LLM solo recibe texto protegido. Nunca recibe claves, correspondencias, valores originales ni contexto administrativo |
| Motor → base | Solo almacena texto cifrado, parámetros criptográficos y metadatos de contexto; nunca valores en claro ni hashes sin clave de valores sensibles |

Principios (heredados y propios):

- **Fallo cerrado.** Cualquier duda (detección inconclusa, marcador no autorizado, clave ausente, registro alterado, contexto vencido) produce un error **sin texto parcial**. Un resultado inconcluso nunca equivale a «sin datos sensibles» (playbook §9).
- **Decidir en el punto de decisión.** La autorización, la vigencia del contexto, el estado del scope y la vigencia del análisis se comprueban **dentro de la transacción que decide**, con la hora de la base leída después de las esperas de bloqueo (A05 §6.1, I-A05-13).
- **Sin efectos al importar.** Importar el paquete no importa la biblioteca criptográfica, no lee claves ni toca la base (regla 5 de CLAUDE.md; D-33 intacta).
- **Una respuesta restaurada es sensible.** Su acceso se resuelve antes de entregarla (playbook §9).
- **Aislamiento por scope en cada consulta.** Toda lectura/escritura de A06 lleva `scope_id` en el `WHERE`; los identificadores conocidos no conceden acceso.

## 2. Glosario

| Término | Definición |
|---|---|
| **Contexto** | Registro persistente opaco (`context_id`) que agrupa las correspondencias de una conversación o de un trabajo. Tiene `scope_id`, `kind`, `created_at` y `expires_at` fijos (§8) |
| **Seudonimización** | Sustituir un fragmento por un **marcador** cuyo original se guarda cifrado en el contexto. Reversible solo vía `restore_text` |
| **Redacción** | Sustituir un fragmento por la constante `[REDACTED]`. **No** se crea correspondencia: no es reversible |
| **Marcador** | Cadena `[[CCE1:<token>]]` (§6) |
| **Hallazgo** | `FindingLocation` de A03: `type_id`, `start`, `end` en puntos de código, intervalo semiabierto [A03 §6.4] |
| **Grupo** | Unión de hallazgos que se solapan, incluido el solapamiento transitivo (§5.2). Es la unidad que se sustituye |
| **Material entregado** | Los textos (con marcadores) que el host efectivamente envió al modelo para generar una respuesta |
| **Versión de clave** | Etiqueta (`key_version`) de una clave que el host suministra; la activa cifra lo nuevo |

## 3. API Python (propuesta; no disponible)

Nuevos métodos de `ScopedOperations` (mismo patrón que A05: `scope_id` explícito + `ScopeContext`; ver §12 para permisos). Todos son asíncronos, **rechazan `HostTransaction`** (`transaction_incompatible`) [E-23] y no existen en HTTP ni en la cola durable [1A].

```python
# Ejemplo ilustrativo — propuesto, no disponible
class ScopedOperations:
    async def create_context(self, scope_id: str, *, kind: Literal["conversation", "job"],
                             ttl_s: int | None = None) -> ContextInfo: ...
    async def get_context(self, scope_id: str, context_id: str) -> ContextInfo: ...
    async def list_contexts(self, scope_id: str, *, kind: str | None = None, state: str | None = None,
                            limit: int | None = None, cursor: str | None = None) -> Page[ContextInfo]: ...
    async def delete_context(self, scope_id: str, context_id: str) -> ContextInfo: ...   # idempotente

    async def protect_text(self, scope_id: str, *, context_id: str, kind: Literal["conversation", "job"],
                           text: str, policy: ProtectionPolicy, analysis: AnalysisSource,
                           request_id: str | None = None) -> ProtectedText: ...

    async def restore_text(self, scope_id: str, *, context_id: str, kind: Literal["conversation", "job"],
                           text: str, analysis: AnalysisSource,
                           delivered_material: Sequence[str] | None = None,
                           delivered_markers: Collection[str] | None = None,    # exactamente uno de los dos
                           on_new_findings: Literal["redact", "fail"] = "redact",
                           request_id: str | None = None) -> RestoredText: ...

# Funciones puras (sin permisos ni E/S), útiles al host
def find_markers(text: str) -> tuple[str, ...]: ...        # marcadores estrictos, orden de aparición, sin duplicados
```

### 3.1 Tipos de entrada

```python
@dataclass(frozen=True)
class ProtectionPolicy:
    default_action: Literal["pseudonymize", "redact"]            # obligatoria [E-03]
    actions: Mapping[str, Literal["pseudonymize", "redact"]] = field(default_factory=dict)   # type_id -> acción

@dataclass(frozen=True)
class InlineAnalysis:                      # el motor analiza el texto exacto en esta llamada (§4.1)
    profile_id: str | None = None          # selección de A02 §8, igual que analyze_text
    version_number: int | None = None
    context: str | None = None             # contexto auxiliar de A03 §2.4 (no es contenido protegido)

@dataclass(frozen=True)
class JobAnalysis:                         # resultado de un trabajo text_analysis de A05 (§4.2)
    job_id: str

AnalysisSource = InlineAnalysis | JobAnalysis
```

- **No existe** una variante que acepte un `AnalysisResult` ni offsets suministrados por el llamador [E-01, P-A06-01]: un resultado en memoria no demuestra a qué texto pertenece (A03 §6.4: la validez de los offsets no acredita pertenencia).
- Las acciones aceptadas son exactamente `pseudonymize` y `redact`. **No existe** una acción «dejar pasar»: la política es estricta, todo hallazgo se protege [4A].
- Un `type_id` de `actions` ausente de los hallazgos es inocuo; un `type_id` con forma inválida ⇒ `validation_failed`. Un hallazgo cuyo tipo no figure en `actions` usa `default_action`.

### 3.2 Tipos de salida (inmutables; sin fragmentos originales)

```python
class ContextInfo:
    scope_id: str; context_id: str; kind: str
    state: Literal["active", "expired", "deleted"]     # EFECTIVO: comparado con la hora de la base en la lectura
    created_at: datetime; expires_at: datetime; deleted_at: datetime | None
    mapping_count: int

class AnalysisRef:                          # procedencia del análisis que decidió la protección
    source: Literal["inline", "job"]; job_id: str | None
    profile: ProfileRef                     # scope_id, profile_id, version_id, version_number, content_hash, selection (A03 §6.1)
    provenance: Provenance                  # A03 §1.1 (mock/model, simulated, adaptador, configuración)
    detection_status: Literal["completed"]  # cualquier otro estado es error (§5.4)

class ProtectedGroup:                       # una sustitución; nunca incluye el valor original
    action: Literal["pseudonymize", "redact"]
    input_start: int; input_end: int        # [start, end) en el texto de entrada, puntos de código
    output_start: int; output_end: int      # [start, end) de la sustitución en el texto de salida
    marker: str | None                      # el marcador (referencia, no el valor); None si se redactó

class ProtectedText:
    contract_version: Literal[1]
    scope_id: str; context_id: str; kind: str
    text: str
    outcome: Literal["protected", "no_findings"]
    groups: tuple[ProtectedGroup, ...]
    markers: tuple[str, ...]                # marcadores distintos presentes en `text`, por primera aparición
    counts: Mapping[str, int]               # findings, groups, pseudonymized, redacted, new_markers, reused_markers
    analysis: AnalysisRef
    context_expires_at: datetime

class RestoredText:
    contract_version: Literal[1]
    scope_id: str; context_id: str; kind: str
    text: str
    outcome: Literal["restored", "restored_with_redactions", "no_markers"]
    counts: Mapping[str, int]               # markers_restored, distinct_markers, authorized_unused,
                                            # markers_consumed_by_redaction, new_findings_redacted
    new_findings: tuple[NewFindingRegion, ...]   # type_id, start, end sobre el texto RECIBIDO; nunca el valor
    analysis: AnalysisRef
```

`ProtectedText.outcome = "no_findings"` significa «detección **completa** sin hallazgos dentro del alcance declarado» (A03 §6.5), **no** una garantía de ausencia de datos sensibles. Nunca se informa `protected` sin que la detección haya sido `completed`.

## 4. Fuentes de análisis y vinculación al texto exacto [4A]

El requisito: los hallazgos que se aplican deben pertenecer **a ese texto exacto**. La validez estructural de los offsets no lo acredita. A06 lo garantiza **por construcción** o **por comprobación de identidad**, nunca confiando en un resultado suministrado por el llamador.

### 4.1 `InlineAnalysis` (vinculación por construcción)

1. El motor llama internamente a la misma función de análisis de A03 que usa `analyze_text` (preparación, adaptador, validación de la salida, cobertura), con `capabilities = ("detection",)` y el **mismo objeto `text`** que luego se transforma.
2. El texto no se copia ni se modifica entre el análisis y la sustitución; el motor aplica los offsets del resultado a ese mismo `str`.
3. A03 ya comprueba `texto[start:end] == fragmento` del adaptador (`fragment_mismatch`). Quedan **fuera de garantía** los desplazamientos hacia **otra ocurrencia idéntica** del mismo fragmento (A03 §6.4): el motor protegerá la ocurrencia señalada, no las demás (§5.6).
4. Requiere `analysis:execute` además del permiso de protección. Sin transacción abierta durante la inferencia (A03 §3.3).
5. Si `detection` no está configurada en el perfil ⇒ `capabilities_not_configured` (A03). Si el adaptador no está configurado ⇒ `inference_not_configured`.

### 4.2 `JobAnalysis` (vinculación por identidad del texto)

Para el resultado `text_analysis` de A05 (`TextAnalysisJobResult`, [A05 §11.4](jobs-files-and-lifecycle.md#114-resultado-documental)) el motor exige **todas** estas condiciones; cualquier fallo ⇒ error sin efectos:

| # | Condición | Error |
|---|---|---|
| 1 | El trabajo existe en **este scope** y `operation = text_analysis` | `resource_not_found` / `analysis_source_invalid` |
| 2 | `state = succeeded` y `data_state = available` | `job_not_succeeded` / `job_data_expired` |
| 3 | `result_expires_at` posterior a la hora de la base **en la transacción de decisión** (vigente) | `job_data_expired` |
| 4 | El artefacto publicado se leyó íntegro (sha256 verificado por A05) y su `job_id` coincide | `stored_object_unavailable` / `analysis_source_invalid` |
| 5 | **Identidad del texto:** `SHA-256(UTF-8(text))` y el tamaño en bytes UTF-8 de `text` son iguales a los de la entrada fijada del trabajo (`input.sha256`, `input.size_bytes`, que A05 conserva en los metadatos del trabajo), y `analysis.input.text_length == len(text)` | `analysis_text_mismatch` |
| 6 | `analysis.profile` (scope, perfil, versión, `content_hash`) coincide con la identidad fijada del trabajo | `analysis_source_invalid` |
| 7 | `analysis.detection.status = completed` | `detection_inconclusive` / `capabilities_not_configured` |
| 8 | El llamador tiene `results:read` (lectura autorizada del resultado, sensible por el playbook §9) además del permiso de protección | `permission_denied` |

- **Re-comprobación en el punto de decisión:** el resultado se lee antes de la transacción de decisión (sin retener bloqueos durante E/S de almacenamiento). En la transacción de decisión se bloquea la fila del trabajo `FOR SHARE` (bloqueo 8 del orden común) y se **re-verifican** 2, 3 y que `result_artifact_id` y el sha256 del artefacto sean los leídos. Un `delete_job_data` o una expiración que ocurra antes de ese bloqueo hace fallar la operación; uno posterior no cambia lo ya decidido (sus bytes ya se aplicaron en esa transacción).
- La entrada de texto (`text_input`) puede haber vencido (7 días) o haberse limpiado: no importa, porque la identidad se compara con los **metadatos** del trabajo (30 días) y no con el objeto de texto. El plazo efectivo lo marca el resultado (7 días desde la terminación, D-A05-01).
- **Qué demuestra y qué no:** demuestra que este texto es **byte a byte** el que A05 analizó y que el resultado vigente y accesible pertenece a esa ejecución. **No** demuestra calidad de detección, ni que el trabajo se haya hecho con el perfil que el host *quería* (la política del host decide qué trabajo acepta) ni qué ocurrencia idéntica señaló el modelo.
- Los trabajos `document_analysis` **no** son fuente (sus unidades son documentales; A07/A08) ⇒ `analysis_source_invalid`. Un `SourceMap` sigue representando posiciones documentales y A06 no lo consume [1A].
- No se modifican los contratos, la expiración ni el *fence* de A05: A06 solo **lee** trabajos y artefactos publicados (D-A05-01, D-A05-02, D-A05-03 intactas).

## 5. Protección (`protect_text`)

### 5.1 Entrada y límites

- `text`: exacto (sin normalizar), no vacío, sin sustitutos aislados, mismos límites que A03 (`max_analysis_text_chars` 8192, `max_analysis_text_bytes` 24576): el texto debe poder analizarse. Exceder ⇒ `limit_exceeded`; nunca se trunca.
- `context_id` válido, `kind` igual al del contexto (§8). `policy` válida (§3.1).
- Los límites propios de A06 figuran en §15. La salida puede ser **más larga** que la entrada (un marcador mide 25 puntos de código); no hay límite de salida adicional más allá de `max_findings` × 25.

### 5.2 Unión determinista de intervalos [4A, E-04]

1. Se toman los hallazgos de la fuente (cada uno con `0 ≤ start < end ≤ len(text)`).
2. **Se ordenan** por `(start, end, type_id)` — el resultado de A03 ya viene ordenado, pero el algoritmo **no depende** del orden recibido (se ordena siempre).
3. Barrido: un hallazgo **se une** al grupo actual si `start < group_end` (**solapamiento estricto**); entonces `group_end = max(group_end, end)`. Esto produce el cierre transitivo: `[0,10)`, `[8,20)`, `[19,30)` forman un único grupo `[0,30)` aunque `[0,10)` y `[19,30)` no se toquen.
4. Dos intervalos **adyacentes** (`[0,5)` y `[5,9)`) **no se unen** [E-04]: no se solapan y cada uno se sustituye por separado.
5. Un grupo contiene al menos un hallazgo; sus miembros conservan sus `type_id` solo para elegir la acción.

Propiedades exigidas (pruebas): idempotencia respecto del orden de entrada, de la duplicación de un tipo sobre el mismo intervalo y del anidamiento (un hallazgo contenido en otro no cambia el grupo).

### 5.3 Acción por grupo y precedencia de la redacción [4A]

- Acción de un hallazgo: `policy.actions[type_id]` o `policy.default_action`.
- **Si algún miembro del grupo resuelve a `redact`, se redacta el grupo entero** y **no** se crea correspondencia reversible para él (ni siquiera para los miembros que pedían seudonimizar).
- Si todos resuelven a `pseudonymize`, el grupo completo `texto[g_start:g_end]` se seudonimiza como **un único fragmento** con un solo marcador.
- La redacción sustituye el grupo por la constante `[REDACTED]`, idéntica para todos los tipos [E-05]. Es **inerte** para la restauración (no tiene forma de marcador; §6.3).

### 5.4 Detección inconclusa [4A]

La fuente debe entregar `detection.status = completed`. `inconclusive` (cualquier motivo: `partial_coverage`, `abstained`, `truncated`), `not_requested` o `not_configured` ⇒ `detection_inconclusive` / `capabilities_not_configured` y **no se devuelve texto**, aunque haya hallazgos verificables: «lo verificable encontrado» no acredita ausencia de otros (A03 §6.5). No se crean contexto, marcadores ni filas.

### 5.5 Resultado vacío y texto sin cambios

- Detección completa **sin hallazgos**: el texto se devuelve idéntico, `outcome = no_findings`, sin filas nuevas. Aun así se valida el contexto (existe, vigente, tipo correcto, scope activo) en la transacción de decisión.
- Las regiones **fuera de los grupos** se copian sin ningún cambio (CRLF, normalización, espacios, controles, no BMP): la salida es la concatenación exacta de esas regiones y las sustituciones.

### 5.6 Límite por ocurrencia [4A]

Redactar un intervalo **no borra** copias del mismo valor que existan en **otros textos**, en **otras ocurrencias del mismo texto no señaladas** por la detección (A03 no deduplica ni busca otras ocurrencias), ni los valores ya **seudonimizados y autorizados previamente** en el contexto: si el valor `V` se seudonimizó antes con el marcador `M` y ahora se redacta otra ocurrencia de `V`, `M` sigue siendo restaurable a `V` mientras el contexto exista. La protección es **por ocurrencia señalada**, no por valor. El motor no promete anonimización.

### 5.7 Colisión con marcadores en la entrada

Si `text` contiene un **candidato a marcador** (§6.2, estricto o laxo) ⇒ `marker_collision` **antes** de cualquier efecto (también antes de la inferencia). Razón: sin escape no se puede distinguir un literal del emitido por el motor y la restauración lo reinterpretaría. Limitación conocida: prosa legítima como «[CCE report]» también colisiona (§16, B-A06).

## 6. Marcadores

### 6.1 Formato [E-06, P-A06-02]

```text
[[CCE1:7Q4K9M2XJ8WH3NV5]]
```

- Prefijo `[[CCE1:` (`1` = versión del formato), token de **16 caracteres** del alfabeto Crockford base32 sin I, L, O, U (`0123456789ABCDEFGHJKMNPQRSTVWXYZ`), sufijo `]]`. Longitud fija: **25** puntos de código, todo ASCII.
- El token son 80 bits de `secrets` (aleatorio, **no derivado** del valor ni de la clave); único por `(scope_id, context_id)`; se reintenta ante colisión.
- **El marcador no lleva el tipo del hallazgo** ni ninguna pista del valor: el modelo no aprende la categoría y la unión de grupos no necesita elegir una etiqueta. Consecuencia: la identidad de un marcador es el **fragmento exacto**, independiente del tipo.
- Regex estricta: `\[\[CCE1:[0-9A-HJKMNP-TV-Z]{16}\]\]`.

### 6.2 Candidatos y parser [E-07]

`find_markers` y el motor reconocen:

| Clase | Patrón | Tratamiento |
|---|---|---|
| **Estricto** | la regex de §6.1 | marcador sintácticamente válido |
| **Candidato laxo L1** | `(?i)\[\s*\[?\s*cce` (corchete, espacios opcionales, otro corchete opcional, `CCE` en cualquier capitalización) | si no es estricto ⇒ **alterado** |
| **Candidato laxo L2** | `(?i)\bcce\d*\s*:\s*[0-9a-z]{8,}` (sin corchetes) | si no es estricto ⇒ **alterado** |

Un candidato laxo que no sea estricto es un **marcador alterado que el parser reconoce**: en restauración ⇒ `marker_malformed` (fallo cerrado, sin devolución parcial); en protección ⇒ `marker_collision`.

**Alteraciones fuera de lo detectable** (documentadas, no son un error): traducción o transliteración del marcador; separación en varias líneas o con caracteres invisibles/ZWJ/homoglifos que escapen de L1/L2; sustitución de `[[`/`]]` por otros corchetes (p. ej. de ancho completo); marcador citado dentro de base64/URL; pérdida total del marcador. En estos casos el texto queda **sin restaurar** y la respuesta **no falla**; `counts.authorized_unused` informa cuántos marcadores autorizados no aparecieron, de modo que el host pueda detectar ausencias. Ningún marcador alterado no detectable **concede** acceso a un original.

### 6.3 Tratamiento de literales

- Un literal estricto en la respuesta del modelo se trata como un marcador: se acepta **solo** si está autorizado (§7.2); si no ⇒ `marker_not_authorized`. No hay forma de «escapar» un literal [E-07].
- `[REDACTED]` y cualquier otro texto no son candidatos: se devuelven tal cual.
- Los originales reinsertados **no** se vuelven a analizar (§7.4), de modo que un original que parezca un marcador no se reinterpreta.

### 6.4 Exposición de información

Lo que un observador del texto protegido (incluido el modelo) **sí** aprende: la **posición** de cada sustitución, **cuáles apariciones son iguales** (mismo marcador), cuántos valores distintos hay y que existe un valor sensible en esa posición. Lo que **no** aprende por el marcador: el valor, su tipo, su longitud (el marcador mide siempre 25 puntos de código y `[REDACTED]` es constante), ni relación alguna con otros contextos (tokens aleatorios independientes). La **longitud del texto protegido** difiere de la original, lo que no revela la longitud de cada original. En la base, el observador ve el número de correspondencias por contexto, su clase de tamaño (relleno a 64 B) y las fechas de creación (§16, límite 6). Los errores y los logs no incluyen marcadores, tokens ni valores (§14); `ProtectedGroup.marker` y `RestoredText` **sí** entregan marcadores al llamador autorizado, que ya posee el texto.

## 7. Restauración (`restore_text`)

Requiere `text:restore` (permiso **distinto** de `text:protect`), más `analysis:execute` (si `InlineAnalysis`) o `results:read` (si `JobAnalysis`), acceso al scope y **el contexto específico** (§8.6).

### 7.1 Orden de operaciones (la clave es: validar y evaluar antes de descifrar)

1. **Autorizar** (permiso, scope, `HostTransaction`), validar entrada y límites (§15). Texto de respuesta con los mismos límites de A03 (§16).
2. **Analizar sintaxis** (puro, sin E/S): extraer candidatos (§6.2). Cualquier alterado ⇒ `marker_malformed`.
3. **Autorización por respuesta** (puro): cada marcador estricto de la respuesta debe estar en el conjunto autorizado (§7.2); si no ⇒ `marker_not_authorized`. **Ninguna consulta a la base ocurre antes de esta comprobación**: no hay oráculo sobre contextos o marcadores ajenos.
4. **Evaluar datos sensibles nuevos** sobre la respuesta **con los marcadores aún puestos** (§7.3): análisis inline (sin transacción abierta) o lectura del resultado de A05 (§4.2, con `text` = la respuesta tal como llegó). Detección distinta de `completed` ⇒ `detection_inconclusive`: **no se descifra nada ni se devuelve texto**.
5. **Transacción de decisión** (§11): scope activo → [fila del trabajo] → contexto (`FOR SHARE`, vigente según la hora de la base **leída después de la espera**, `kind` correcto) → correspondencias de **los marcadores autorizados presentes** (cada una debe existir; ausente ⇒ `marker_unknown`; descifrado con AAD vinculante; clave ausente ⇒ `protection_key_unavailable`; fallo de autenticación ⇒ `mapping_integrity_failed`).
6. **Ensamblar** (puro) en **una sola pasada** (§7.4) tras la confirmación de que **todo** estuvo disponible. Cualquier fallo anterior ⇒ ningún texto, ni parcial.

### 7.2 Autorización por respuesta [4A, E-08]

El **host** declara el material que entregó al modelo, mediante **exactamente uno** de:

- `delivered_material`: los textos (con marcadores) enviados al modelo; el motor extrae sus marcadores estrictos con el mismo parser;
- `delivered_markers`: la colección exacta de marcadores.

Reglas:

- El conjunto autorizado `A` = marcadores estrictos del material/lista. Solo pueden restaurarse marcadores presentes **a la vez** en la respuesta, en `A` **y** emitidos en **este** contexto (existe su correspondencia).
- Un marcador de la respuesta que no está en `A` ⇒ `marker_not_authorized`, **aunque** exista en el contexto (el modelo pudo adivinarlo o repetirlo de un turno que el host no incluyó).
- Un marcador en `A` sin correspondencia en este contexto (p. ej. de otro contexto) y presente en la respuesta ⇒ `marker_unknown`.
- `A` vacío con marcadores en la respuesta ⇒ `marker_not_authorized`; `A` vacío sin marcadores ⇒ `outcome = no_markers` (se sigue evaluando la respuesta: §7.1 paso 4).
- Marcadores en `A` ausentes de la respuesta no son un error: se cuentan en `authorized_unused`.
- El motor **no puede verificar** que el host entregó realmente ese material al modelo (frontera de confianza; A01 §2.1). El contrato obliga a que `delivered_*` sea la entrada real; el contenido del modelo jamás se usa para construir `A`.

### 7.3 Datos sensibles nuevos y marcadores existentes [4A, E-09, P-A06-03]

La respuesta se evalúa **tal como la produjo el modelo** (con marcadores), antes de reinsertar ningún original; los originales reinsertados **no** se analizan (el usuario ya los autorizó al seudonimizarlos en este contexto y no son «nuevos»).

| Situación | Tratamiento |
|---|---|
| Detección `completed`, sin hallazgos | restaurar los marcadores autorizados |
| Detección distinta de `completed` | `detection_inconclusive`; sin texto, sin descifrar |
| Hallazgo **fuera** de todo marcador | dato **nuevo**: con `on_new_findings="redact"` (por defecto) se **redacta** (`[REDACTED]`, **siempre irreversible**, sin importar la política de protección ni `default_action`); con `"fail"` ⇒ `new_sensitive_data` y no se devuelve texto |
| Hallazgo **contenido completamente dentro** de un marcador | se **ignora**: el marcador es un token opaco, no contiene un valor del modelo |
| Hallazgo que **cruza** el borde de un marcador o lo **cubre** | el hallazgo y los marcadores que toca se unen (misma unión de §5.2): el grupo se redacta entero; el marcador **no** se restaura (cuenta en `markers_consumed_by_redaction`) y su original no se reinserta |
| Dos o más hallazgos/marcadores que se encadenan | unión transitiva (§5.2) |

- Los valores nuevos **no se seudonimizan ni se guardan**: venir del modelo no los vuelve autorizados ni reversibles. El consumidor recibe `outcome = restored_with_redactions`, el texto con los originales autorizados reinsertados y `[REDACTED]` en las regiones nuevas, y `new_findings` (tipo y posiciones **en la respuesta recibida**, nunca el valor).
- Con `"fail"` el host decide la política de salida (bloque B); el motor no la impone.

### 7.4 Una sola pasada

El ensamblado recorre la respuesta de izquierda a derecha una vez, construyendo la salida a partir de: tramos literales sin cambios, `[REDACTED]` por cada grupo redactado y el original (descifrado) por cada marcador restaurado. **Nunca se vuelve a examinar** el texto ya reinsertado ni la salida parcial; un original que contenga `[[CCE1:…]]` aparece literalmente en la salida.

## 8. Contextos

### 8.1 Creación y tipo [2A]

- `create_context(kind, ttl_s=None)` (permiso `text:protect`): genera `context_id = "ctx_" + 32 hex` (128 bits de `secrets`), `created_at` = hora de la base, `expires_at = created_at + ttl`. **Opaco:** no lleva semántica, nombre ni referencia del host (el host guarda su propia asociación). Wrapa una clave de índice nueva con la **versión activa** (§9.3). Cuota: `max_active_contexts_per_scope` (§15).
- `kind ∈ {conversation, job}` **ligado al contexto de forma inmutable** y parte del AAD criptográfico (§9.4). Toda operación recibe `kind` y exige coincidencia; un desajuste ⇒ `context_kind_mismatch` [E-11]. El motor no ata el contexto a un `job_id` de A05: A06 y A05 tienen vigencias independientes.
- **No existe** ACL por contexto: «acceso al contexto específico» significa **scope del llamador + `context_id` + `kind` + estado vigente**. El `context_id` es un identificador, no una credencial (un id conocido no concede acceso: se exige permiso y scope). Un control más fino (por usuario/conversación) es del host y de B-A06 [P-A06-06].

### 8.2 Vigencia fija [2A]

- `ttl` por defecto **7 días** (`ProtectionSetup.context_ttl_s`), independiente de los plazos de originales y resultados de A05. `ttl_s` por llamada: entero en `[60, max_context_ttl_s]` (máximo configurable, 30 días por defecto, tope duro 3650 días) [E-12].
- `expires_at` se **persiste** y no cambia jamás: **ningún** uso, reintento, lectura, rotación de clave ni operación nueva lo renueva ni lo extiende. No hay API de renovación.
- Cambiar los valores por defecto afecta solo a contextos nuevos.

### 8.3 Expiración efectiva

Un contexto está **vencido** si `hora_de_la_base ≥ expires_at` (en el instante exacto ya está vencido) [E-13]. La comprobación se hace **en cada decisión** con la hora de la base leída tras las esperas de bloqueo, **aunque la limpieza física no haya corrido**. Un contexto vencido o eliminado:

- no admite `protect_text` ni `restore_text` (⇒ `context_unavailable`, 410);
- no admite `create`/uso; **no se reactiva ni se reasigna**: los ids son aleatorios de 128 bits, la fila lápida conserva el id hasta su purga y ningún camino de código escribe `state='active'` sobre una fila vencida o eliminada.
- sigue visible por `get_context`/`list_contexts` (metadatos) con `state = expired | deleted` hasta que se purgue su fila.

### 8.4 Eliminación explícita [2A, E-14]

`delete_context` (permiso `contexts:delete`): irreversible e **idempotente**. En una sola transacción (orden §11) se eliminan **todas** las filas de correspondencias del contexto, se anula la clave de índice envuelta, se marca `state = deleted`, `deleted_at` y `metadata_expires_at`, y se decrementa el contador de cuota. Una vez confirmada, el motor **no puede** restaurar nada de ese contexto, ni siquiera con las claves. Se puede eliminar un contexto ya vencido y aún no limpiado. Eliminar un contexto eliminado devuelve su `ContextInfo` sin cambios. Un contexto inexistente o de otro scope ⇒ `resource_not_found`.

### 8.5 Limpieza física y retención [E-15]

`engine.maintenance().run_protection_cleanup(batch_size=...)` (y CLI propuesta `cawbase-content-engine cleanup-protection`): explícita, repetible, por lotes de transacciones cortas, **nunca** automática al importar ni al iniciar; **no requiere claves**. Por contexto vencido: elimina sus correspondencias y la clave de índice envuelta, marca `state = expired`, fija `metadata_expires_at`; después purga las filas de contextos eliminados/vencidos cuyo `metadata_expires_at` pasó (por defecto **30 días** tras el fin de vida, `ProtectionSetup.metadata_retention_s`). El resumen devuelto solo tiene conteos. Sin cleanup, los textos cifrados de contextos vencidos **permanecen en la base** (ilegibles para el motor, no para quien tenga base **y** clave) hasta la primera pasada: se recomienda ejecutarla con una cadencia ≤ 1 hora [H-A06-02: el valor se fijará al medir].

No toca A05: `run_cleanup`, su resumen y sus tablas no cambian.

### 8.6 Qué se elimina, cuándo y backups [2A]

| Dato | Se elimina | Cuándo |
|---|---|---|
| Correspondencias cifradas y clave de índice envuelta | filas borradas de la base viva | al confirmarse `delete_context`; o en la pasada de limpieza posterior al vencimiento |
| Metadatos del contexto (id, scope, kind, tiempos, estado) | lápida conservada | hasta `metadata_expires_at` (30 días por defecto) y luego purga |
| Claves de cifrado | **no las gestiona el motor** | las retira el operador (§9.5) |
| Copias de seguridad, réplicas, WAL/PITR, instantáneas del volumen, páginas muertas de PostgreSQL/SQLite | **el motor no puede borrarlas** | según la política del operador |

**Honestidad sobre backups:** tras una eliminación confirmada, una copia anterior **puede conservar el texto cifrado**; seguirá siendo descifrable por quien tenga esa copia **y** la versión de clave correspondiente. Por eso el contrato **no afirma borrado inmediato en backups**. La mitigación operativa es retirar una versión de clave **solo después** de que venzan los contextos que la usan **y** de que caduquen las copias que la contengan; el «borrado criptográfico» por contexto **no** existe (las versiones de clave son compartidas). PostgreSQL y SQLite pueden retener versiones de fila hasta `VACUUM`/reutilización: no se promete borrado físico inmediato.

## 9. Criptografía y claves [3A]

### 9.1 Suministro de claves

```python
# Ejemplo ilustrativo — propuesto, no disponible
class KeyProvider(Protocol):
    def active_version(self) -> str: ...
    def key(self, version: str) -> bytes | None: ...        # 32 bytes; None si la versión no está disponible

@dataclass(frozen=True)
class KeyRing:                                             # implementación estática en memoria
    active: str
    keys: Mapping[str, bytes]                              # versión -> 32 bytes; repr oculta los valores
    @classmethod
    def from_directory(cls, path: Path, *, active: str) -> KeyRing: ...   # archivos «<versión>.key»

@dataclass(frozen=True)
class ProtectionSetup:
    keys: KeyProvider
    context_ttl_s: int = 7 * 86400
    max_context_ttl_s: int = 30 * 86400
    metadata_retention_s: int = 30 * 86400
    limits: ProtectionLimits = ProtectionLimits()

ContentEngine(config, inference=..., protection=ProtectionSetup(keys=KeyRing(...)))
```

- Las claves son **del host/operador**, de 32 bytes aleatorios, **fuera de la base de correspondencias** [3A]. Versión: `^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$`.
- `key(version)` es **síncrona, en memoria y sin E/S** [E-18]: se invoca dentro de la transacción de decisión. Un host que recargue claves (KMS, archivos) debe hacerlo fuera y exponer el resultado en memoria; **no hay KMS obligatorio** [3A].
- El motor nunca registra, serializa ni incluye claves en `repr`, errores, diagnósticos o eventos. Python no garantiza borrado de memoria.

### 9.2 Cifrado autenticado de cada correspondencia [E-16, E-19, H-A06-01, H-A06-09]

Biblioteca mantenida: **`cryptography`** (extra opcional `protection`; versión a fijar al implementar). Sin criptografía propia: solo se **componen** primitivas estándar de la biblioteca.

Por cada correspondencia (fragmento `F`, marcador con token `T`):

1. `pt = u32_be(len(F_utf8)) ‖ F_utf8 ‖ ceros` rellenado hasta múltiplo de **64 bytes** (oculta la longitud exacta, no la clase de tamaño).
2. `salt` = 16 bytes aleatorios; `subkey = HKDF-SHA256(ikm = clave_maestra[versión], salt = salt, info = "cce.protection.record.v1")` → 32 bytes.
3. `nonce` = 12 bytes aleatorios; `ct = AES-256-GCM(subkey, nonce, pt, AAD)`.
4. **AAD** (vinculación criptográfica) = codificación con prefijo de longitud de: `"cce.protection.map.v1"`, `scope_id`, `context_id`, `kind`, `T`, `versión`. Mover una fila a otro scope, contexto, tipo o marcador, o cambiar su versión, **falla la autenticación**.
5. Se guarda `(versión, salt, nonce, ct)`.

La subclave por registro hace irrelevante la colisión de nonces con una misma clave maestra.

### 9.3 Índice de igualdad y rotación [E-17]

Para que el **mismo fragmento exacto** reutilice su marcador (también ante operaciones concurrentes) hace falta buscar por igualdad **sin guardar valores ni hashes sin clave**:

- Al crear el contexto se genera una **clave de índice** de 32 bytes aleatorios, se **envuelve** (AES-256-GCM, AAD = `scope_id`, `context_id`, `"index-key"`, versión) con la **versión activa de ese momento** y se guarda `index_key_version`/`index_key_wrapped` en el contexto.
- El índice de un fragmento es `HMAC-SHA256(clave_de_índice, "cce.protection.eq.v1" ‖ F_utf8)`: **con clave** (un atacante con solo la base no puede probar candidatos), por contexto (índices distintos entre contextos) y **no** contiene el valor.
- Único `(scope_id, context_id, equality_index)`: **un fragmento ⇒ un marcador** por contexto.

**Cómo se conserva la identidad del marcador al rotar la clave de cifrado:** la clave de índice **no cambia** con la versión activa; solo se desenvuelve con la versión con la que se envolvió. Tras rotar de `v1` a `v2`, un fragmento ya seudonimizado produce el **mismo** índice (misma clave de índice), se encuentra la fila existente y se reutiliza su marcador, aunque esa fila esté cifrada con `v1`. Los registros **nuevos** del contexto se cifran con `v2` (la versión activa cifra lo nuevo [3A]); los viejos siguen legibles. Un contexto puede, por tanto, usar `v1` (clave de índice y registros previos) y `v2` (registros nuevos) hasta su vencimiento. No hay recifrado automático [3A]; el marcador nunca depende de la versión de clave.

### 9.4 Disponibilidad de versiones y fallo cerrado [3A]

- La versión **activa** cifra lo nuevo. Las anteriores siguen **disponibles para leer hasta el vencimiento de los contextos que las usan**: el operador debe conservarlas en el `KeyProvider` hasta entonces. `engine.maintenance().protection_key_usage()` (propuesto) informa, por versión, el número de contextos vigentes que la referencian y su `expires_at` máximo (sin contenido), para decidir cuándo retirarla.
- **Clave ausente** (`key(version) is None`) al descifrar o desenvolver ⇒ `protection_key_unavailable`; **fallo de autenticación** (registro o AAD alterados, clave equivocada bajo la misma etiqueta) ⇒ `mapping_integrity_failed`. En ambos casos la operación completa falla **sin texto parcial**.
- Crear un contexto o proteger **sin** versión activa utilizable ⇒ `protection_key_unavailable`.
- Con `start()` y un `ProtectionSetup`, se verifica que la biblioteca criptográfica cargue y pase una prueba de respuesta conocida (`configuration_invalid` si no) [H-A06-01]; sin `ProtectionSetup`, `start()` no cambia.

### 9.5 Procedimiento de rotación (operativo)

1. Añadir `v2` al `KeyProvider` conservando `v1`; 2. marcar `v2` como activa; 3. los contextos nuevos usan `v2`; los existentes siguen funcionando; 4. cuando `protection_key_usage()` muestre cero contextos vigentes con `v1` **y** hayan caducado los backups que la requieran, retirar `v1`. Quitar `v1` antes deja los contextos que la usan en fallo cerrado (`protection_key_unavailable`), por diseño.

## 10. Persistencia y migración `0004` (propuesta)

Namespace fijo, `scope_id` en toda PK y FK; ninguna garantía depende solo de FK/`CHECK`. Definición compartida con la migración y congelada en su revisión (patrón de `a05_schema.py`) [H-A06-07].

| Tabla | Clave | Columnas principales | Índices / restricciones |
|---|---|---|---|
| `protection_contexts` | (`scope_id`, `context_id`) | `kind`, `state` (`active`/`expired`/`deleted`), `ttl_s`, `created_at`, `expires_at`, `deleted_at`, `index_key_version`, `index_key_wrapped` (nulo tras eliminar/limpiar), `mapping_count`, `mapping_bytes`, `metadata_expires_at`, `updated_at` | UNIQUE `context_id`; (`state`, `expires_at`); (`metadata_expires_at`); `CHECK` de estados; FK → `scopes` |
| `protection_mappings` | (`scope_id`, `context_id`, `token`) | `key_version`, `salt`, `nonce`, `ciphertext`, `equality_index` (32 bytes), `created_at` | UNIQUE (`scope_id`, `context_id`, `equality_index`); FK → `protection_contexts`; **sin** valor, tipo, offsets ni texto |
| `protection_scope_usage` | `scope_id` | `active_contexts`, `updated_at` | `CHECK ≥ 0`; creada perezosamente con el scope bloqueado |

- **Migración `0004_text_protection`** (aditiva, solo hacia adelante, mismo procedimiento de A01): crea las tablas e índices; **no altera** tablas existentes; no migra ni purga datos; `schema_level` 3 → **4**; `min_runtime_level` **permanece en 1** [E-24].
- **Instalaciones actualizadas desde `0003`** conservan todos sus datos de A01–A05 (la migración no los toca). Un runtime `0.5.0` sobre una base en nivel 3 falla `start()` con `schema_outdated` hasta migrar (mismo patrón que A02/A05).
- **Compatibilidad con runtimes anteriores** (`0.1.0`–`0.4.0`): cada uno compara `schema_level ≥ el suyo` y `min_runtime_level ≤ 1`, de modo que **siguen arrancando** sobre el nivel 4. No conocen las tablas de A06 ni pueden crear, leer o eliminar contextos o correspondencias; no ejecutan limpieza de A06. Un runtime anterior que deshabilite el scope no necesita fence: A06 comprueba el estado del scope **en cada decisión** (§11), sin cachés. **Restricción de despliegue ampliada** (A02/A03/A05): no emitir claves con `text:protect`, `text:restore`, `contexts:read` ni `contexts:delete` hasta actualizar **todos** los runtimes que acceden a la base; un runtime anterior falla al cargar grants desconocidos y revocar no lo corrige. Orden: migrar → actualizar runtimes → configurar `ProtectionSetup` y claves → emitir claves con permisos nuevos.
- **Instalaciones que todavía no habiliten A06**: sin `ProtectionSetup` (o sin el extra) el motor arranca igual, no importa `cryptography` y toda operación de A06 que pase la autorización lanza `protection_not_configured`; las tablas permanecen vacías.
- Permisos de base: el runtime necesita `DELETE` sobre las tablas de A06 (borrado de correspondencias y purga de lápidas).

## 11. Transacciones, bloqueos, tiempo y concurrencia

### 11.1 Autoridad temporal

Toda hora que decide se toma **de la base** (PostgreSQL `clock_timestamp()`; SQLite `strftime(…, 'now')`; helper `db_now` de A05) **después** de tomar los bloqueos que pueden hacer esperar a la transacción [A05 §6.1, I-A05-13]. La vigencia del contexto (`now < expires_at`), la del resultado de A05 (`now < result_expires_at`) y el estado del scope se evalúan con esa hora, **dentro de la transacción de decisión**.

### 11.2 Orden común de bloqueos (extiende A05 §6.2)

Ver [A05 §6.2](jobs-files-and-lifecycle.md#62-orden-común-de-bloqueos).

| Rango | Recurso | PostgreSQL |
|---|---|---|
| 1 | `scopes[scope]` | `FOR SHARE` |
| 8 | `jobs[job]` (solo `JobAnalysis`) | `FOR SHARE` (más lectura del artefacto, rango 9) |
| 11 | `protection_scope_usage[scope]` | `FOR UPDATE` (solo crear/eliminar/limpiar) |
| 12 | `protection_contexts[context]` | `FOR NO KEY UPDATE` (proteger), `FOR SHARE` (restaurar), `FOR UPDATE` (eliminar/limpiar) |
| 13 | `protection_mappings` (por `token` ascendente) | inserción / lectura / borrado |

Secuencias: `create_context`: 1 → 11 → 12 (inserción). `protect_text`: 1 → [8] → 12 `FOR NO KEY UPDATE` → 13. `restore_text`: 1 → [8] → 12 `FOR SHARE` → 13 (lectura). `delete_context` y limpieza: 1 → 11 → 12 `FOR UPDATE` → 13. Siempre ascendente; ninguna toma 2–7 ni 9–10 salvo la lectura del artefacto de `JobAnalysis`. En SQLite el escritor único serializa todo y el orden vale como orden de lectura/escritura.

### 11.3 Garantías

| Escenario | Garantía [E-20] |
|---|---|
| **Protección concurrente del mismo valor** | Dos `protect_text` en el mismo contexto se **serializan** por el bloqueo del contexto (`FOR NO KEY UPDATE`); el segundo relee y reutiliza el marcador del primero. El `UNIQUE (…, equality_index)` es la red de seguridad final: un choque inesperado revierte y se reintenta una vez. Resultado: **un fragmento ⇒ un marcador** |
| **Restauración frente a borrado/vencimiento** | `restore_text` toma el contexto `FOR SHARE`, lee la hora de la base **después** y descifra dentro de esa transacción. `delete_context` (`FOR UPDATE`) espera a que termine la restauración en curso o la precede. Si la eliminación se confirma **antes** del bloqueo de la restauración, esta falla (`context_unavailable`); si se confirma **después**, la restauración ya decidió con el contexto vivo. Nunca hay texto restaurado a partir de un contexto cuya eliminación o vencimiento ya fuese efectivo en el punto de decisión |
| **Disponibilidad de correspondencias ≡ entrega del texto protegido** | `protect_text` devuelve el texto **solo después** del `COMMIT` exitoso de las correspondencias. Si el `COMMIT` falla o su resultado es desconocido ⇒ `database_unavailable` reintentable (A05 §7.3, A01 D-14) **sin texto**; las correspondencias quizá existan pero no están referenciadas (se reutilizarán al reintentar, o vencerán con el contexto). Por eso se **rechaza `HostTransaction`**: el host podría revertir tras recibir el texto |
| **Reintentos** | `protect_text` es **convergente**: repetirlo reutiliza los marcadores ya creados (mismo texto ⇒ mismo resultado) y no crea correspondencias duplicadas; no necesita clave de idempotencia. `restore_text` es de solo lectura |
| **Fallo de persistencia a mitad de la protección** | Una sola transacción: todo o nada; sin marcadores huérfanos visibles ni texto devuelto |
| **Cuota/tope por contexto** | `mapping_count`/`mapping_bytes` se actualizan con el contexto bloqueado `FOR NO KEY UPDATE`: no se excede el límite por carrera |
| **Vigencia del análisis (`JobAnalysis`)** | Re-comprobada con el trabajo bloqueado `FOR SHARE` en la transacción de decisión (§4.2) |
| **Interbloqueos** | El orden evita ciclos entre operaciones del motor; `40P01`/`40001` ⇒ `database_unavailable` reintentable (A01). Sin garantía universal frente a SQL ajeno |

La inferencia (`InlineAnalysis`) y la lectura del artefacto ocurren **sin** transacción ni conexión retenida; el cifrado/descifrado y las consultas ocurren dentro de la transacción de decisión (cientos de operaciones de microsegundos, acotadas por `max_analysis_findings`).

### 11.4 Scope deshabilitado [E-21, P-A06-04]

En la transacción de decisión, `scopes.status ≠ active` ⇒ `scope_disabled` para `create_context`, `protect_text` y `restore_text` (se retiene el acceso a datos reversibles). `delete_context`, `get_context` y `list_contexts` siguen permitidos (A05 §9.3 por analogía). No hay *fence* propio: no se aceptan trabajos A06 que reclamar.

## 12. Permisos y errores

### 12.1 Permisos nuevos [E-22, P-A06-05]

El catálogo pasaría de **17** a **21** (4 nuevos de scope). Sin jerarquías ni comodines; `AdminContext`/`admin:*` **no** acceden a contextos, correspondencias ni textos protegidos.

| Permiso | Habilita |
|---|---|
| `text:protect` | `create_context`, `protect_text` |
| `text:restore` | `restore_text` (**distinto** de `text:protect` [4A]) |
| `contexts:read` | `get_context`, `list_contexts` (solo metadatos) |
| `contexts:delete` | `delete_context` |

Combinaciones necesarias: `protect_text` con `InlineAnalysis` ⇒ + `analysis:execute`; con `JobAnalysis` ⇒ + `results:read`. `restore_text` ⇒ `text:restore` + (`analysis:execute` | `results:read`). Se reutilizan `ScopeContext`, grants, API keys y delegación D-27 (subconjunto del emisor); los grants existentes no cambian. Sin ruta HTTP, no hay clave HTTP asociada.

### 12.2 Errores nuevos (propuestos)

| Código | HTTP | Reintentable | Cuándo |
|---|---|---|---|
| `protection_not_configured` | — (sin HTTP) | no | falta `ProtectionSetup` o el extra `protection` |
| `protection_key_unavailable` | — | no (corregir la configuración) | falta la versión activa o la de un registro |
| `mapping_integrity_failed` | — | no | falla la autenticación AEAD (registro, AAD o clave alterados) |
| `context_unavailable` | — | no | contexto vencido o eliminado |
| `context_kind_mismatch` | — | no | `kind` distinto del del contexto |
| `detection_inconclusive` | — | no | detección ≠ `completed` |
| `analysis_text_mismatch` | — | no | el resultado de A05 no pertenece a este texto |
| `analysis_source_invalid` | — | no | el trabajo no es una fuente válida (operación, perfil, contrato) |
| `marker_collision` | — | no | la entrada de `protect_text` contiene candidatos a marcador |
| `marker_malformed` | — | no | candidato laxo no estricto en la respuesta |
| `marker_not_authorized` | — | no | marcador estricto fuera del conjunto autorizado |
| `marker_unknown` | — | no | autorizado pero sin correspondencia en este contexto |
| `new_sensitive_data` | — | no | `on_new_findings="fail"` y hay hallazgos nuevos |

Se reutilizan `permission_denied`, `resource_not_accessible`, `resource_not_found`, `validation_failed`, `limit_exceeded`, `quota_exceeded`, `scope_disabled`, `job_not_succeeded`, `job_data_expired`, `stored_object_unavailable`, `capabilities_not_configured`, `inference_*`, `transaction_incompatible`, `database_unavailable`, `internal_error`. Los mensajes son genéricos y nunca incluyen texto, fragmentos, marcadores, claves ni tokens. Un contexto de otro scope o inexistente ⇒ `resource_not_found` (no se distingue).

## 13. Configuración y dependencias

- **Extra nuevo `protection`** = `cryptography` (rango por fijar con `uv lock`) [E-16, H-A06-01]. **No** forma parte de las dependencias base ni de `documents`; el módulo criptográfico se importa **perezosamente** al construir/arrancar con `ProtectionSetup` y en la primera operación, nunca al importar el paquete. Prueba exigida: `import cawbase_content_engine` no deja `cryptography` en `sys.modules` y no cambia la medición de D-33 [H-A06-06].
- `ProtectionSetup` se entrega a `ContentEngine(..., protection=...)` (explícito; no se lee el entorno). El host posee el `KeyProvider`; el motor no lo cierra.
- `Limits`/`ProtectionLimits` (§15) por configuración explícita; ningún límite de A01–A05 cambia.

## 14. Observabilidad y retención

- Registrador `cawbase_content_engine.protection`. **Permitido:** `scope_id`, `context_id`, `kind`, operación, resultado, códigos de error, conteos (`findings`, `groups`, `markers`), tamaños, duraciones, `request_id`, versiones de clave (etiqueta). **Prohibido** en logs, errores, eventos y diagnósticos: texto, fragmentos, valores originales, marcadores/tokens, claves, claves envueltas, ciphertext, hashes/índices de igualdad, `type_id` por hallazgo y offsets.
- **Sin eventos en `admin_events`** [E-26]. Los diagnósticos (`diagnostics()`) no incluyen datos de A06 (se reutiliza A01 sin cambios); `protection_key_usage()` y el resumen de limpieza solo contienen conteos y etiquetas de versión.
- **Retención** (resumen): correspondencias, hasta eliminación/vencimiento más la pasada de limpieza; metadatos de contexto, 30 días tras el fin de vida; backups fuera del alcance del motor (§8.6). Los informes con hallazgos siguen siendo datos sensibles (A05 §13).

## 15. Límites iniciales (elecciones técnicas, no mediciones) [E-27]

| Límite | Valor | Notas |
|---|---|---|
| Texto de `protect_text` / respuesta de `restore_text` | los de A03 (8192 puntos de código, 24 576 B) | mismos `Limits` de A03; el análisis los exige |
| `max_active_contexts_per_scope` | 1000 | `quota_exceeded` (retryable tras vencer/eliminar) |
| `max_mappings_per_context` | 10 000 | `quota_exceeded` |
| `max_context_bytes` (suma de fragmentos en claro) | 4 MiB | `quota_exceeded` |
| `max_fragment_chars` | 8192 | un grupo unido puede igualar el texto |
| `max_delivered_material_chars` | 262 144 | suma de `delivered_material` |
| `max_delivered_markers` | 10 000 | |
| `ttl_s` por llamada | `[60, max_context_ttl_s]` | |
| `cleanup_batch_size` | 200 contextos | |

## 16. Fuera de alcance y limitaciones declaradas

**Fuera de A06:** DOCX/PDF y aplicar sustituciones a archivos (A07/A08); modelo real, calidad de detección y segmentación por modelo (A09); integración con CAWbase, UI, streaming y políticas de salida (bloque B); rutas HTTP y operaciones de cola de protección/restauración; KMS obligatorio, recifrado automático, criptografía propia; ACL por contexto; borrado en backups; URLs firmadas.

**Limitaciones (declaradas, no defectos):**

1. **Calidad:** A06 es tan completa como la detección que la alimenta. Cualquier cierre con el mock no acredita detección real (A09); «sin hallazgos» no es «sin datos sensibles».
2. **Límite por ocurrencia** (§5.6).
3. **Respuestas largas:** `restore_text` evalúa la respuesta entera con A03: una respuesta mayor que `max_analysis_text_chars` (8192) no puede restaurarse sin ampliar esos límites del operador o segmentar fuera del motor (la segmentación por modelo es de A09). Troncar o trocear en el motor rompería marcadores y cobertura [P-A06-03].
4. **Alteraciones indetectables** (§6.2) y **literales** que parecen marcadores (§5.7, §6.3): la política estricta prefiere fallar a restaurar mal; no hay escape.
5. **El host es fuente de verdad** de la autorización por respuesta y del material entregado (A01 §2.1); un host comprometido en el mismo proceso/credenciales puede leer las tablas y, si tiene las claves, descifrarlas.
6. **Fuga de estructura:** el texto cifrado revela la cantidad de correspondencias y la clase de tamaño (relleno a 64 B) y, por el marcador reutilizado, qué apariciones son iguales; el modelo ve la repetición de un marcador.
7. **Backups y copias** (§8.6). **SQLite/PostgreSQL** pueden retener versiones de filas.
8. **Sin recifrado**: una versión de clave comprometida expone los registros cifrados con ella hasta que venzan sus contextos.
9. Python no garantiza borrar de memoria claves ni valores.
10. No es anonimización ni cumplimiento normativo universal.

## 17. Hipótesis por verificar en el código (H-A06-nn)

| ID | Hipótesis | Cómo se verifica |
|---|---|---|
| H-A06-01 | `cryptography` ofrece `AESGCM`, `HKDF`, `HMAC` y ruedas para macOS arm64 y Linux x86_64/arm64 con Python 3.13; el tamaño del extra es aceptable | `uv lock`, instalación del wheel + extra en entornos limpios, prueba de respuesta conocida |
| H-A06-02 | Una pasada de limpieza por lotes de 200 contextos es corta; la cadencia recomendada | medición local, informada como medición, no como umbral |
| H-A06-03 | `FOR SHARE` / `FOR NO KEY UPDATE` de SQLAlchemy sobre las filas de A06 reproducen las esperas que describe §11.3 (PostgreSQL) y SQLite serializa con el escritor único | pruebas con conexión independiente y `pg_stat_activity`, como las de A05 |
| H-A06-04 | El trabajo de A05 conserva `input.sha256`, `input.size_bytes` y el perfil fijado en sus metadatos hasta `metadata_expires_at`; el artefacto trae `analysis.input.text_length` y `analysis.profile` | leer `jobs/ops.py` y probar con trabajos reales |
| H-A06-05 | El `HostTransaction` se rechaza con el mismo patrón `IncompatibleTransactionError` de A05 | prueba |
| H-A06-06 | Importar el paquete sigue sin cargar `cryptography` ni cambiar la mediana de D-33 | `sys.modules` + `scripts/measure_import.py` + comparación base/rama en el mismo ambiente |
| H-A06-07 | La migración `0004` sigue el patrón de `0003` (definiciones compartidas congeladas, `schema_level` 4) y es idempotente | migración sobre bases `0003` con datos de A05 |
| H-A06-08 | El mock de A03 y/o un adaptador de prueba permiten producir los hallazgos de cada caso, incluidas respuestas con marcadores | fixtures propios |
| H-A06-09 | AES-256-GCM con subclave HKDF por registro (sin criptografía propia) es una composición estándar y suficiente | revisión de la implementación; pruebas de integridad/AAD |
| H-A06-10 | Se puede añadir `run_protection_cleanup` y `protection_key_usage` a `engine.maintenance()` sin cambiar el contrato ni el resumen de `run_cleanup` de A05 | revisar `jobs/maintenance.py` y `engine.maintenance()`; regresión de A05 |
