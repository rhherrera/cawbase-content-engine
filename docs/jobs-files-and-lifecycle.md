# Trabajos, archivos y ciclo de vida — contrato de A05

> **Estado: ESPECIFICADO / NO IMPLEMENTADO (A05, pendiente de revisión).** Ninguna operación, ruta, permiso, tabla, migración, extra, variable ni comando de este documento existe en `0.3.0`: todo es **propuesto / no disponible** hasta que haya código y pruebas. La versión del paquete no cambia. Las decisiones de producto son `D-A05-nn` (aprobadas por el usuario); las elecciones técnicas derivadas son `E-A05-nn` y **no** son decisiones de producto ([A05-decisions](decisions/A05-decisions.md)). Alcance y aceptación: [especificación de A05](features/A05-trabajos-archivos-y-ciclo-de-vida.md).

Relacionado: [arquitectura](architecture.md) · [API pública](public-api.md) · [persistencia y migraciones](persistence-and-migrations.md) · [seguridad y aislamiento](security-and-isolation.md) · [instalación y operación](installation-and-operations.md) · [perfiles (A02)](profiles-and-catalog.md) · [inferencia y análisis (A03)](inference-and-analysis.md) · [procesamiento de documentos (A04)](document-processing.md).

Los contratos de A01–A03 **siguen implementados y sin cambios**; este documento solo agrega. En particular `analyze_text`, `POST /v1/scopes/{scope_id}/analyze-text`, `AnalysisResult`, los límites de A03 y el cuerpo JSON de 64 KiB de las rutas existentes se conservan íntegramente.

## 0. Resumen

A05 agrega al motor:

1. **Archivos**: carga por stream (Python y HTTP) a un almacenamiento **administrado** por el motor, o **referencias externas** opacas resueltas por adaptadores que configura el host; identidad por SHA-256 y tamaño; cuarentena hasta validar; descarga autorizada.
2. **Trabajos durables** con tres fases modeladas explícitamente: **validación documental**, **extracción/mapeo** y **análisis** (reutilizando A03). Dos operaciones: `document_analysis` (DOCX/PDF del subconjunto A04) y `text_analysis` (texto, versión durable de A03).
3. **Ejecución recuperable** sin cola obligatoria: la base del motor es la autoridad sobre estado, intentos, posesión (*lease* con *fencing*) y resultado publicado. El host ejecuta trabajos concretos desde sus workers o usa el **ejecutor de referencia**.
4. **Ciclo de vida de datos**: retención aprobada (D-A05-01), expiración lógica, limpieza explícita por lotes y purga de `admin_events` (resuelve Q-04 de A01).

No hay protección, redacción ni restauración (A06–A08), modelo real (A09) ni integración con CAWbase.

## 1. Principios y fronteras

| Principio | Regla |
|---|---|
| Autoridad | La base del motor decide estado, intentos, posesión y resultado publicado. Ningún mensaje de cola, archivo ni memoria de un worker prevalece sobre ella |
| Sin cola impuesta | No se exige SQS, Redis, Celery ni otro servicio. El host puede avisar a su orquestador **después del commit**; el motor no promete atomicidad entre su base y una cola externa (§6.6) |
| Sin efectos ocultos | Importar o iniciar `ContentEngine` no crea workers, hilos, tareas, temporizadores ni limpiezas. Workers, ejecutor y mantenimiento se inician y cierran explícitamente |
| Transacciones cortas | Ninguna transacción ni conexión del motor se retiene durante transferencias, validación, extracción, inferencia ni escritura en storage (A01 D-14) |
| Garantía de ejecución | **Intentos repetibles con efectos idempotentes** y una única publicación autoritativa. **No** se promete «exactamente una vez» de cómputo ni de llamadas al adaptador |
| Aislamiento | Todo recurso nuevo (archivo, trabajo, intento, artefacto, idempotencia, uso) pertenece a un `scope_id`, que forma parte de toda clave primaria y foránea y de todo `WHERE` (A01 D-17) |
| Contenido sensible | Originales, textos de entrada, contexto, resultados y mapas son datos sensibles: nunca en logs, errores, eventos, diagnósticos ni `admin_events` |
| Independencia de A06–A09 | Ninguna garantía de A05 depende de features posteriores. El mock sustituye **solo** la inferencia |

## 2. Modelo conceptual

```text
scope ─1──* stored_file ─────────────┐
      ─1──* job ─1──* job_attempt     │ input_file / context_file
             └─1──* job_artifact      │
             └──── input ─────────────┘
      ─1──* idempotency_record ──► job | stored_file
      ─1──1 scope_job_fence           (serialización frente a deshabilitar el scope, §9)
      ─1──1 scope_usage               (cuotas reservadas y consumidas, §12.3)
engine_usage (fila única)             (cuotas de instalación)
```

Dos dimensiones **separadas** (no se codifican en un mismo campo):

- **Estado de ejecución** del trabajo (`queued` … `cancelled`, §5.3) y la **cancelación solicitada** (`cancel_requested_at`, `cancel_reason`).
- **Ciclo de vida de datos**: disponibilidad de entrada (`stored_file.state`), de resultados (`job.data_state ∈ {none, available, expired, deleted, lost}`) y de metadatos (`metadata_expires_at`), regidos por la retención (§13).

## 3. Archivos y almacenamiento

### 3.1 Clases de objeto

| Clase | Propietario del objeto | Quién lo elimina | Retención del motor |
|---|---|---|---|
| **Administrado** (`managed`): carga Python/HTTP, texto de entrada de `text_analysis`, copias materializadas | motor | motor (limpieza, §13.5) | 7 días desde la recepción completa y durable (D-A05-01) |
| **Referencia externa** (`external`): objeto que el host ya puede leer | host | **nunca** el motor | el motor solo retiene su **registro** (identidad, versión, hash) y deja de permitir su uso al vencer (7 días desde el registro verificado) |
| **Materialización** (`external_materialized`): copia administrada de una referencia que no garantiza estabilidad | motor | motor | como administrado |
| **Artefactos** (`job_artifact`): manifiesto de resultado, mapa documental, temporales de intento | motor | motor | resultados/mapas: 7 días desde la terminación; temporales abandonados: 24 h desde que se determina el abandono |

El motor **nunca** borra, mueve ni modifica un original externo del host, ni promete borrado de copias de seguridad, réplicas, versiones históricas del proveedor de storage ni cachés del host.

### 3.2 Identidad del archivo

`FileIdentity { file_id, sha256, size_bytes, detected_format? }`. El SHA-256 y el tamaño se calculan **por el motor sobre los bytes realmente recibidos o leídos**, nunca se toman del cliente: un `expected_sha256`/`declared_size` del llamador solo sirve para **rechazar** una discrepancia (`content_mismatch`). El formato se detecta por contenido durante la validación (§10.1), no por extensión ni MIME.

`file_id` = `fil_` + 32 hex aleatorios. Conocerlo no concede acceso: toda consulta exige el `scope_id` del contexto.

### 3.3 Protocolo mínimo de storage administrado (`ManagedStorage`)

Interfaz asíncrona estrecha que el operador configura (E-A05-05). El motor genera **todas** las claves; ni clientes ni hosts eligen claves de objetos administrados.

```python
# Propuesto / no disponible
class ManagedStorage(Protocol):
    @property
    def descriptor(self) -> StorageDescriptor: ...       # backend_id estable, durabilidad declarada, capacidad opcional de inventario
    async def put_new(self, key: str, chunks: AsyncIterable[bytes], *, max_bytes: int) -> PutReceipt: ...
        # crea un objeto PRIVADO e INMUTABLE; falla si la clave ya existe (create-only, nunca sobrescribe);
        # devuelve size y sha256 calculados al escribir; retorna solo cuando el objeto es durable según el descriptor
    async def open_read(self, key: str) -> AsyncIterator[bytes]: ...   # lectura por stream; ObjectMissing si no existe
    async def stat(self, key: str) -> ObjectInfo | None: ...          # existencia y tamaño (verificación barata)
    async def delete(self, key: str) -> None: ...                     # idempotente: ausente = éxito
    # opcional (descriptor.inventory = True):
    async def list_keys(self, prefix: str, *, after: str | None, limit: int) -> Sequence[str]: ...
```

- **Verificación**: tras `put_new`, el motor compara `size`/`sha256` del recibo con lo que contó él mismo durante el stream; para cerrar el paso 3 de §4 vuelve a `stat`. Una lectura posterior se verifica recalculando el SHA-256 mientras se consume (lecturas para validación, extracción y descarga); una discrepancia ⇒ `stored_object_corrupt` (§4.2).
- **Reconciliación sin inventario obligatorio**: el motor registra la **intención** (fila `reserved` con su clave) **antes** de escribir. Los huérfanos se descubren desde la base (filas `reserved`/`discarded`/`expired` con clave) y se eliminan con `delete` idempotente. `list_keys` es una mejora opcional para detectar objetos sin fila (p. ej. tras restaurar una copia de la base): se usa por prefijo y por lotes acotados, nunca como condición para borrar algo que la base todavía referencia (§4.3).
- No se exigen versiones de objeto, URLs firmadas, cifrado del proveedor, *multipart* ni listados a todos los backends.

### 3.4 Implementación de referencia: sistema de archivos privado

`FilesystemStorage(root)` (propuesto; E-A05-06):

- Directorio raíz exclusivo del motor, creado con `0700`; archivos `0600`; el proceso no lo sirve como estático y la documentación prohíbe exponerlo por un servidor web.
- Claves generadas `cce/<2 hex>/<32 hex>` (sin datos del cliente, sin `scope_id`, sin nombre original). Validación estricta de la forma de la clave antes de construir rutas; resolución relativa al descriptor de la raíz (`openat`/`O_NOFOLLOW` donde exista; equivalente verificado en macOS) — **sin traversal ni seguimiento de symlinks**.
- Escritura en archivo temporal único dentro de la raíz (`O_CREAT|O_EXCL`), `fsync` del archivo, `rename`/`link` exclusivo sin reemplazo al nombre final y `fsync` del directorio: el objeto es durable al volver `put_new` (en el alcance de durabilidad del sistema de archivos y del hardware; no se promete más).
- **Alcance de despliegue**: un nodo, o varios nodos **solo** sobre un sistema de archivos compartido que garantice estas semánticas (exclusividad de `link`/`rename`, `fsync` efectivo, coherencia de lectura tras cierre). NFS u otros sistemas sin esas garantías **no** están soportados. No se promete acceso multinodo sin almacenamiento compartido apropiado.
- Temporales de escritura huérfanos (proceso caído a mitad) se eliminan por limpieza: prefijo `tmp/`, más antiguos que el plazo de abandono.

### 3.5 Referencias externas (`ExternalSourceResolver`, solo Python)

Para no obligar a reenviar por HTTP archivos que el worker del host ya puede leer (playbook A05), el host configura resolvers con nombre:

```python
# Propuesto / no disponible
class ExternalSourceResolver(Protocol):
    @property
    def descriptor(self) -> ResolverDescriptor: ...    # resolver_id, stable_versions: bool
    async def describe(self, ref: str) -> ExternalObjectInfo: ...   # version_id inmutable, size; sin bytes
    async def open_read(self, ref: str, version_id: str) -> AsyncIterator[bytes]: ...
```

- `ref` es una cadena **opaca** del host (≤ 1024 bytes); el motor no la interpreta como URL ni ruta, no la registra en logs y no la devuelve por HTTP. Toda conectividad remota (credenciales, red, permisos) es **configuración confiable del host**.
- **Registro** (`register_external_file`): el motor obtiene `version_id` y `size`, **lee el objeto completo** calculando SHA-256 (con los límites de §12) y registra `{resolver_id, ref, version_id, size, sha256}`.
- **Estabilidad**: si `stable_versions = True`, cada lectura posterior pide **esa** versión y verifica tamaño y SHA-256 contra lo registrado; una discrepancia o ausencia ⇒ `source_changed` / `input_unavailable` (no reintentable, §7.1). Si el resolver **no** garantiza versiones inmutables, el registro **materializa** una copia administrada antes de aceptarla como entrada estable (`origin = external_materialized`), verificada con el mismo hash.
- **HTTP no admite referencias externas**: no se aceptan URLs, rutas locales, nombres de resolver ni credenciales desde clientes HTTP (§15).

### 3.6 Estados de un archivo

| Estado | Significado | ¿Utilizable como entrada? | ¿Descargable? |
|---|---|---|---|
| `reserved` | intención registrada; bytes en tránsito o no confirmados | no | no |
| `stored` | bytes completos, verificados y durables; `received_at` fijado | sí, si `validation_state ≠ rejected` y no venció | solo si `validation_state = passed` |
| `abandoned` | reserva no completada en su plazo (§13.3) | no | no |
| `deleted` | eliminación lógica pedida por el usuario (`data:delete`) | no | no |
| `expired` | venció su retención (`expires_at ≤ ahora`) | no | no |

`validation_state ∈ {pending, passed, rejected}` es independiente: **cuarentena** = `stored` + `pending` (privado y pendiente de validación; **no** significa libre de malware). `rejected` guarda un motivo cerrado (§10.4). La purga física (`physically_deleted_at`) es posterior y asíncrona respecto de los estados lógicos.

**Timestamp de recepción** (D-A05-01): `received_at` = hora de la **autoridad temporal** (§6.1) en la transacción que marca el archivo `stored`, es decir, **después** de verificar bytes, hash y durabilidad (paso 4 de §4). La retención de originales se cuenta desde ahí: `expires_at = received_at + retention_originals_s`, ambos persistidos (§13.1).

### 3.7 Carga y descarga HTTP

Ver rutas en §15. Reglas:

- **Carga** (`POST …/files`): cuerpo binario `application/octet-stream` transmitido por stream; **límite propio** (`max_file_bytes`, 25 MiB) independiente del límite JSON de 64 KiB (que se conserva en todas las rutas existentes y en las rutas JSON nuevas). Se cuenta lo **realmente recibido**: un `Content-Length` declarado mayor que el límite ⇒ 413 antes de leer; uno menor que lo recibido o un cuerpo *chunked* que supera el límite ⇒ se corta la lectura y 413, sin conservar bytes. Desconexión del cliente, plazo total (`upload_timeout_s`) o inactividad (`upload_idle_timeout_s`) ⇒ la reserva se marca `abandoned`, se libera la cuota y no queda archivo utilizable.
- **Idempotencia y reinicio** (sin protocolo *multipart*): `Idempotency-Key` obligatorio. Una carga interrumpida **no se reanuda**: se reinicia con la misma clave. Reglas en §8.4.
- **Descarga**: solo de originales `stored` + `passed` y de resultados publicados no vencidos (§13.4), autorizada por scope; respuesta por stream con `Cache-Control: no-store`, `Content-Disposition: attachment; filename="<file_id>.<docx|pdf>"` (o `<job_id>-result.json` / `<job_id>-source-map.json`), `X-Content-Type-Options: nosniff` y el tipo detectado. **Sin URLs firmadas por defecto**, sin exponer claves de storage, rutas, resolvers ni referencias externas. No se conserva ni se devuelve el nombre original del archivo (B-A05-03).
- **Nada en cuarentena, rechazado, reservado, abandonado, eliminado, vencido o no publicado es descargable** por el flujo ordinario: la respuesta es la misma 404 `resource_not_found` (no revela el motivo).
- La descarga se verifica mientras se transmite; si el SHA-256 final no coincide, la conexión se aborta sin cerrar limpiamente el cuerpo (el cliente no recibe una respuesta completa válida) y el objeto queda `stored_object_corrupt` (§4.2).

## 4. Consistencia entre base y almacenamiento

No hay transacción distribuida. Todo objeto escrito por el motor sigue **cinco pasos**:

| Paso | Dónde | Qué |
|---|---|---|
| 1. Reservar | transacción corta | crea la fila (`stored_file` o `job_artifact`) en `reserved` con identidad, clave generada, dueño (carga o intento con su *fencing token*), `reserved_at` y reserva de cuota |
| 2. Transferir | **fuera** de transacción | `put_new(key, …)` por stream, contando bytes y calculando SHA-256 |
| 3. Verificar | fuera de transacción | tamaño y hash propios = recibo del storage = esperado (si lo hay); `stat` confirma existencia y tamaño |
| 4. Registrar | transacción corta | `reserved → stored`/`written` condicionado al dueño vigente (carga no abandonada; intento con posesión, §6.3); fija `size`, `sha256`, `received_at`/`written_at`; ajusta la cuota a lo real |
| 5. Publicar | transacción corta | solo artefactos: con posesión y precondiciones vigentes (§6.4), el trabajo pasa a `succeeded` y apunta a **una única** referencia autoritativa (manifiesto + mapa); los artefactos quedan `published` |

Las claves de artefactos son **específicas del intento** (incluyen el `artifact_id` aleatorio); un worker nunca sobrescribe la salida de otro (`put_new` es *create-only*). El resultado publicado es exclusivamente el que referencia `job.result_artifact_id`/`source_map_artifact_id`; cualquier otro artefacto del trabajo es descartable.

**Nunca se anuncia éxito con referencias incompletas**: el paso 5 exige artefactos `written` del mismo intento, verificados.

### 4.1 Tabla de fallos y recuperación

| Falla entre / durante | Estado resultante | Recuperación |
|---|---|---|
| Tras 1, sin objeto (proceso caído, desconexión) | fila `reserved` sin objeto | la limpieza marca `abandoned` cuando vence el plazo del dueño (carga: `upload_timeout_s`; intento: pérdida de posesión), registra `abandoned_at`, libera cuota; tras la retención de temporales (24 h) llama a `delete` (idempotente: ausente = éxito) y marca purgado |
| Durante 2: objeto parcial | `reserved`; temporal parcial en el storage (o nada, si el backend es atómico) | igual que arriba; en el filesystem de referencia el temporal `tmp/` también se elimina por antigüedad. Nunca se marca `stored` sin el paso 3 |
| Tras 2–3, sin registro | objeto completo con fila `reserved` | igual que arriba: no se adopta el objeto «por existir»; una carga se reinicia (§8.4) |
| Tras 4 de un artefacto, sin publicación (worker obsoleto o caído) | `written` de un intento sin posesión | al terminar o perder el intento, sus artefactos no publicados pasan a `discarded` (`abandoned_at`); se eliminan tras 24 h |
| Publicación confirmada con respuesta perdida | trabajo `succeeded` apuntando a los artefactos del intento | el worker **reconcilia antes de repetir**: relee el trabajo; si `succeeded` con `published_attempt_no` = su intento y los ids de sus artefactos ⇒ éxito; si no, sus artefactos se descartan. Nunca se repite la publicación a ciegas (§7.3) |
| Objeto perdido o corrupto **después** de publicarse / registrarse | lectura o `stat` falla, o hash distinto | se marca `stored_object_corrupt` (archivo) o `data_state = lost` (resultado) en una transacción corta; la descarga/lectura responde `stored_object_unavailable` (503 no reintentable para ese objeto); **no** se reintenta el trabajo ni se regenera en silencio; queda evento operativo sin contenido. El host decide re-ejecutar con otra clave |
| Eliminación física fallida | fila con `delete_after` vencido y sin `physically_deleted_at` | la limpieza reintenta en lotes posteriores (`delete` idempotente); se cuenta y registra el fallo sin contenido; el acceso lógico ya estaba denegado |
| Worker obsoleto escribe tras perder posesión | puede crear un objeto (clave nueva propia) | el paso 4/5 falla por *fencing* (`lease_lost`); su fila queda `reserved`/`written` sin posesión ⇒ `discarded`; jamás sustituye la referencia publicada |

### 4.2 Integridad del objeto publicado

La verificación de lectura (§3.3) aplica a validación, extracción, descarga y lectura de resultados. Un objeto corrupto o ausente nunca se presenta como válido ni como vacío.

### 4.3 Coordinación de limpieza, posesión y descargas

- La limpieza elimina **solo** objetos cuya fila está en un estado terminal de datos (`abandoned`, `discarded`, `deleted`, `expired`) **y** cuyo `delete_after` venció, leídos y marcados (`purge_claimed_at` con *fencing* de la limpieza) en una transacción corta. **Nunca** borra algo porque una consulta momentánea no encuentre su publicación, ni por aparecer en un inventario sin fila hasta que esa ausencia persista por encima del plazo de abandono y se confirme dentro de una transacción (objetos sin fila: solo con `list_keys` y tras el plazo).
- `delete_after = momento de la transición lógica + gracia`: para datos vencidos o eliminados, gracia = `download_timeout_s` + margen (§12) para que una descarga **iniciada antes** de la expiración/eliminación pueda completar; las descargas nuevas se deniegan desde la transición lógica. Una descarga que supere su plazo se corta.
- Un artefacto `written` del intento vigente nunca es candidato de limpieza mientras el intento conserve posesión; los de intentos terminados sí, como `discarded`.
- Dos limpiezas concurrentes se excluyen por el marcado condicional (`UPDATE … WHERE purge_claimed_at IS NULL OR purge_claim_expires_at < ahora`); repetir una eliminación es inocuo.

## 5. Trabajos

### 5.1 Operaciones A05

| Operación (`operation`) | Entrada | Fases (`phase`) | Resultado publicado |
|---|---|---|---|
| `document_analysis` | archivo DOCX/PDF (`file_id` del scope) + selección de perfil/versión + capacidades + contexto opcional | `validation` → `extraction` → `analysis` → `publication` | `DocumentAnalysisResult` (manifiesto) + `SourceMap` (artefacto separado) |
| `text_analysis` | texto (+ contexto) como en `analyze_text` | `analysis` → `publication` | `TextAnalysisJobResult` (envuelve un `AnalysisResult` de A03 sin cambios) |

- **Validación documental** es una **fase durable** de `document_analysis` (no una API genérica): su resultado se registra en el archivo (`validation_state`, validador y configuración) y, si rechaza, el trabajo falla con el motivo cerrado (§10.4). Un archivo ya validado con **la misma identidad de validador y configuración** no se vuelve a validar estructuralmente en otro trabajo; con otra configuración, se valida de nuevo y se registra por separado (`file_validations`).
- **No hay** operaciones ejecutables de protección, redacción ni restauración, ni un tipo de trabajo genérico extensible por el cliente.
- `text_analysis` **no reemplaza** `analyze_text`: es una operación nueva y explícita para quien necesita durabilidad, idempotencia y consulta posterior.

### 5.2 Ciclo de un trabajo

```text
submit (scope ops) ──commit──► queued ──claim──► running ──publish──► succeeded
                                 ▲  │              │  │ └──fallo no reintentable / agotado──► failed
                    available_at │  │              │  └──fallo reintentable / lease vencido──► retry_wait ─┐
                                 └──┼──────────────┼──────────────────────────────────────────────────────┘
                                    └─cancel/scope─┴─cancel observado / scope deshabilitado──► cancelled
```

### 5.3 Matriz de transiciones

Autoridad temporal: §6.1. «Fence ok» = §9. «Entrada ok» = archivo(s) de entrada `stored`, no vencidos ni eliminados y (si aplica) no `rejected`. Todas las transiciones son `UPDATE` condicionales sobre el estado esperado (y, en las del worker, sobre `attempt_no` y `lease_token`), en transacciones cortas.

| # | De → a | Actor | Precondiciones | Efectos y timestamps | Error si no se cumple | Repetición |
|---|---|---|---|---|---|---|
| T1 | ∅ → `queued` | API (`submit_*`) | permiso; scope activo y fence leído; entrada ok; `input.expires_at ≥ deadline_at`; cuotas; selección y fijación (§8.5) | crea trabajo, `accepted_at`, `available_at = accepted_at`, `deadline_at`, snapshot fijado, idempotencia, reserva de cuota | ver §17 | misma clave + misma solicitud ⇒ mismo trabajo (§8) |
| T2 | `queued`/`retry_wait` → `running` | worker (`claim`) | `available_at ≤ ahora < deadline_at`; fence ok; entrada ok; sin `cancel_requested_at`; `requirements_key` soportado; cupo de scope | `attempt_no += 1`, nuevo `lease_token`, `lease_expires_at = ahora + lease_ttl`, `started_at` (primer intento), fila `job_attempt` | no se reclama (el trabajo no se devuelve); si falla fence/entrada/deadline ⇒ T4/T9/T10 en la misma transacción | dos reclamaciones concurrentes: exactamente una gana (§6.2) |
| T3 | `running` → `succeeded` | worker (`publish`) | `attempt_no`/`lease_token` iguales, `lease_expires_at > ahora`, sin cancelación, fence ok, entrada ok, artefactos `written` del intento | `terminated_at`, `result_artifact_id`, `source_map_artifact_id`, `published_attempt_no`, `data_state = available`, `result_expires_at`, `metadata_expires_at`; artefactos `published`; libera cuota de trabajo | `lease_lost` / `job_cancelled` / `input_unavailable` (y la transición correspondiente) | reconciliación §7.3; nunca dos publicaciones |
| T4 | `queued`/`retry_wait` → `cancelled` | usuario (`cancel_job`) / deshabilitación del scope | — | `cancel_requested_at`, `cancel_reason`, `terminated_at`, expiraciones | — | repetir ⇒ devuelve el estado actual |
| T5 | `running` → (marca) | usuario (`cancel_job`) | lease vigente | fija `cancel_requested_at`, `cancel_reason = user_requested`; el estado sigue `running` hasta T6 | — | idempotente |
| T6 | `running` → `cancelled` | worker (heartbeat/publish observa la marca) / limpieza al vencer el lease / deshabilitación del scope | marca de cancelación o fence roto | `terminated_at`; intento `cancelled`; artefactos `discarded` | — | — |
| T7 | `running` → `retry_wait` | worker (`report_retryable`) o recuperación de lease vencido | error del catálogo reintentable (§7.1); `attempt_no < max_attempts`; `available_at` calculado `< deadline_at`; sin cancelación | `available_at = ahora + backoff`, `last_error_code`, intento `retryable_error`/`lease_lost`; artefactos del intento `discarded` | si no quedan intentos o plazo ⇒ T8 | — |
| T8 | `running` → `failed` | worker / recuperación | error no reintentable, intentos agotados (`retries_exhausted`) o plazo (`deadline_exceeded`) | `terminated_at`, `failure_code`, expiraciones; artefactos `discarded` | — | — |
| T9 | `queued`/`retry_wait` → `failed` | limpieza / reclamación | `deadline_at ≤ ahora` (`deadline_exceeded`, o `executor_unavailable` si nunca hubo ejecutor compatible), entrada no disponible (`input_unavailable`) | `terminated_at`, `failure_code` | — | — |
| T10 | no terminal → `cancelled` (`scope_disabled`) | deshabilitación del scope (A05) o reconciliación del fence (§9) | fence roto | `cancel_reason = scope_disabled` | — | reactivar el scope **no** lo revierte |

Estados **terminales**: `succeeded`, `failed`, `cancelled`; no tienen transiciones de salida. Cancelar un trabajo terminal devuelve su estado sin cambios (`cancel_outcome = already_terminal`); un éxito confirmado **nunca** se reescribe.

**Timestamps** del trabajo: `accepted_at`, `available_at`, `started_at` (primer `running`), `heartbeat_at`, `lease_expires_at`, `cancel_requested_at`, `terminated_at`, `deadline_at`, `result_expires_at`, `metadata_expires_at`, `updated_at`. Todos con la autoridad temporal (§6.1).

### 5.4 Cancelación

- **Carrera con la finalización**, resuelta por la fila del trabajo: la publicación (T3) exige `cancel_requested_at IS NULL` en su `UPDATE` condicional; la cancelación de un trabajo en `running` fija la marca con `UPDATE … WHERE state = 'running'`. Quien confirma primero gana. Si gana la cancelación, **ninguna** salida del intento se publica (sus artefactos se descartan). Si el éxito ya se confirmó, cancelar devuelve `succeeded` y no reescribe el resultado.
- El motor **no** promete detener cómputo remoto ya iniciado (adaptador de inferencia, proceso de extracción en curso): el worker cancela su espera local, termina el proceso de extracción (§10.6) y no publica.
- **Cancelación solicitada ≠ caída del ejecutor**: la primera tiene `cancel_reason` (`user_requested`, `scope_disabled`) y termina en `cancelled`; la caída del ejecutor es pérdida de posesión (`lease_lost`) y lleva a reintento (T7) o a `failed`.
- Los artefactos de intentos cancelados o abandonados pasan a `discarded` y siguen la retención de temporales (§13.3).

## 6. Reclamación, posesión y tiempo

### 6.1 Autoridad temporal

Toda hora que decide estado (vencimientos de lease, `available_at`, `deadline_at`, expiraciones) se toma **de la base** dentro de la misma sentencia o transacción que la compara:

- PostgreSQL: `clock_timestamp()` (hora del servidor; no `now()`, fijo al inicio de la transacción) para escribir vencimientos y compararlos.
- SQLite: `strftime('%Y-%m-%d %H:%M:%f', 'now')` de la conexión (reloj del nodo que abre el archivo; SQLite es de un solo nodo, §6 de persistencia).

Los relojes de los workers **no** deciden vencimientos; solo planifican cuándo enviar *heartbeats* (con margen). Los timestamps de A01–A03 (hora de la aplicación) no cambian.

### 6.2 Reclamación (`claim`)

- **PostgreSQL**: una transacción corta `SELECT … FROM jobs WHERE state IN ('queued','retry_wait') AND available_at <= clock_timestamp() AND requirements_key = ANY(:keys) … ORDER BY available_at, accepted_at LIMIT :n FOR UPDATE SKIP LOCKED`, luego la comprobación del fence (lectura del scope `FOR SHARE`, §9) y del cupo de scope (`scope_usage` `FOR UPDATE`) y el `UPDATE` a `running`. `SKIP LOCKED` evita que dos workers esperen la misma fila; la condición del `UPDATE` garantiza que solo uno gana.
- **SQLite**: transacción de escritura inmediata (`BEGIN IMMEDIATE`, A01) — un único escritor serializa a todos los reclamadores; misma selección y `UPDATE` condicional. Con muchos workers sobre SQLite la reclamación se serializa: es el comportamiento real del backend (uso previsto: un nodo, baja concurrencia).
- La reclamación devuelve `ClaimedJob { job_id, scope_id, attempt_no, lease_token, lease_expires_at, phase, pinned snapshot }`. El *fencing token* es el par (`attempt_no`, `lease_token`): `attempt_no` crece monótonamente por trabajo y `lease_token` es aleatorio (128 bits) por intento.

### 6.3 Posesión, *heartbeat* y *fencing*

- `lease_ttl_s` (60 s) finito; *heartbeat* cada `heartbeat_interval_s` (20 s). `heartbeat` = `UPDATE jobs SET lease_expires_at = least(ahora + lease_ttl, attempt_deadline_at), heartbeat_at = ahora, phase = :fase WHERE job_id = … AND state = 'running' AND attempt_no = :n AND lease_token = :t AND lease_expires_at > ahora`. Devuelve `cancel_requested` para que el worker se detenga.
- **Toda** renovación, progreso, registro de artefacto (paso 4), reintento y publicación verifica la posesión con la misma condición. Cero filas afectadas ⇒ `lease_lost`: el worker deja de trabajar y **no publica**, aunque termine el cálculo.
- **Una posesión vencida no se revive**: aunque nadie haya reclamado el trabajo, un *heartbeat* con `lease_expires_at ≤ ahora` falla.
- El lease nunca supera `attempt_deadline_at = min(started_attempt_at + max_attempt_duration_s, deadline_at)` ni prolonga retenciones (§13.2).
- **Sin transacciones ni conexiones retenidas** durante inferencia, extracción o transferencias: cada heartbeat y cada paso es su propia transacción corta.

### 6.4 Publicación

Una transacción corta que, en este orden: (1) bloquea la fila del trabajo (`FOR UPDATE` en PostgreSQL; escritura inmediata en SQLite) y comprueba posesión y ausencia de cancelación; (2) comprueba el fence del scope (§9) y la entrada (no vencida ni eliminada, hash registrado); (3) comprueba que los artefactos referidos son `written` del mismo intento; (4) aplica T3 y marca los artefactos `published`. Cualquier precondición fallida ⇒ la transición correspondiente (T6/T8/`lease_lost`) y ninguna publicación.

### 6.5 Recuperación de trabajos huérfanos

Trabajos `running` con `lease_expires_at ≤ ahora` se recuperan (T7/T8/T6) por: la reclamación (antes de seleccionar, procesa un lote acotado de vencidos), la limpieza (§13.5) o `recover_expired_leases()`. Ninguna depende de que el worker caído vuelva.

### 6.6 Avisos al host y trabajos pendientes sin aviso

- `submit_*` devuelve `JobAccepted` **después del commit** (en transacción propia). El host puede entonces avisar a su orquestador (SQS, cola propia, etc.). Con `HostTransaction` (solo donde está permitido, §19) el motor no conoce el commit: el host debe avisar **después de su propio commit**.
- Como no hay atomicidad entre base y cola, el aviso puede perderse o duplicarse. Contramedidas: `list_runnable_jobs(limit, older_than_s)` (trabajos elegibles cuyo `available_at` pasó hace más de `older_than_s`) para re-avisar, y la reclamación idempotente (un aviso duplicado no produce dos ejecuciones simultáneas: la segunda reclamación no encuentra el trabajo elegible).

## 7. Reintentos y recuperación

### 7.1 Catálogo cerrado de errores de intento

| Código | Reintentable | Origen |
|---|---|---|
| `inference_unavailable`, `inference_timeout` | sí | A03 (adaptador apagado, cargando, inalcanzable, fallo transitorio, timeout) |
| `database_unavailable` | sí | A01 (incluye `40P01`/`40001`) |
| `storage_unavailable` | sí | storage o resolver con fallo transitorio |
| `lease_lost` | sí (consume intento) | muerte del proceso, pérdida de conexión, partición: lease vencido |
| `extraction_process_failed` | sí (consume intento) | el proceso aislado terminó por causa **no** atribuible a un límite (señal externa, fallo del runtime de aislamiento) |
| `file_rejected` (+ motivo §10.4), `unsupported_format` | no | validación determinista |
| `limit_exceeded`, `unit_too_large`, `no_extractable_text` | no | límites deterministas y documento sin texto (§11.4) |
| `inference_output_invalid`, `inference_fixture_not_found`, `capabilities_not_configured` | no | A03 |
| `input_unavailable`, `source_changed`, `stored_object_corrupt` | no | entrada vencida, eliminada, cambiada o corrupta |
| `configuration_unavailable` | no | el worker no puede reconstruir la configuración fijada (§8.6) |
| `internal_error` | no | defecto; se registra solo la clase |

Terminales derivados: `retries_exhausted` (último error reintentable con intentos agotados; conserva `last_error_code`), `deadline_exceeded`, `executor_unavailable`.

### 7.2 Política

- `max_attempts` = 5 por trabajo (finito), `deadline_at = accepted_at + max_job_duration_s` (24 h).
- *Backoff* exponencial con *jitter*: `d = min(base · 2^(n−1), max)`, `base = 30 s`, `max = 15 min`, demora efectiva uniforme en `[d/2, d]`. Si `ahora + demora ≥ deadline_at` ⇒ `failed` (`deadline_exceeded`).
- Los reintentos **no** extienden la retención de la entrada (§13.2) ni cambian la configuración fijada (§8.6).
- **Garantía**: cada intento es repetible y sus efectos (artefactos con claves propias, publicación condicional) son idempotentes; la publicación es única. No se promete exactamente una vez: el adaptador puede ser invocado varias veces para la misma entrada.

### 7.3 Resultado de commit desconocido

Si la publicación (o cualquier transición del worker) falla con resultado incierto (conexión perdida tras enviar `COMMIT`), el worker **reconcilia antes de repetir**: relee el trabajo con su `attempt_no`/`lease_token`.

| Observa | Acción |
|---|---|
| `succeeded`, `published_attempt_no` = su intento y sus artefactos | éxito; nada más |
| `running` con su posesión vigente | repite la misma publicación (condicional; idempotente) |
| Otro estado o posesión ajena | no publica; marca sus artefactos `discarded` solo si no son los publicados (comparación por id) |
| Base no disponible | reintenta la lectura con espera acotada dentro de su lease; si el lease vence, `lease_lost` |

La misma regla aplica a la carga de archivos (§8.4) y a `submit_*` con respuesta perdida (§8.3).

## 8. Idempotencia y fijación de ejecución

### 8.1 Clave

- `idempotency_key`: 1–128 caracteres `[A-Za-z0-9._:-]`, primero alfanumérico; obligatoria en `submit_document_analysis`, `submit_text_analysis`, `upload_file` y `register_external_file`. HTTP: encabezado `Idempotency-Key`.
- Ámbito: (`scope_id`, `operation`) con `operation ∈ {document_analysis, text_analysis, file_upload, external_file}`. Se almacena **su SHA-256** (`key_hash`), no el texto: la clave es del cliente y podría contener datos.
- **Autorización antes de consultar**: permiso y pertenencia del scope se comprueban antes de buscar la clave; una clave de otro scope no se ve ni se revela.

### 8.2 Huella de la solicitud ≠ snapshot efectivo

- **Huella** (`request_fingerprint`): SHA-256 del JSON canónico de la **solicitud lógica**: operación; `file_id` (o, para texto, SHA-256 del texto y del contexto); `profile_id` **pedido** (o `null`); `version_number` **pedido** (o `null`); capacidades pedidas (conjunto ordenado). No incluye lo que el motor seleccionó.
- **Snapshot efectivo** (§8.5): lo realmente fijado (versión concreta, extractor, adaptador, límites…). Se guarda aparte.
- Misma clave y misma huella ⇒ **mismo trabajo**. Misma clave y huella distinta ⇒ `idempotency_conflict` (409), sin crear nada.

### 8.3 Orden, concurrencia y respuesta perdida

1. Autorización y validación de entrada.
2. **Buscar la clave antes de resolver** el perfil predeterminado o la versión vigente: una repetición devuelve el trabajo original aunque después haya cambiado el predeterminado o se haya publicado otra versión, y aunque el perfil esté ahora desactivado o el scope deshabilitado (la lectura del trabajo sigue las reglas de §9.3).
3. Solo si no existe: selección, fijación, cuotas e inserción **en la misma transacción** que el registro de idempotencia (`UNIQUE (scope_id, operation, key_hash)`).
4. **Dos primeras solicitudes concurrentes**: PostgreSQL — la segunda espera en el índice único y, al confirmarse la primera, recibe conflicto de unicidad: el motor revierte su intento y lee el registro existente (huella igual ⇒ mismo trabajo; distinta ⇒ `idempotency_conflict`). SQLite — el único escritor las serializa y la segunda encuentra el registro en el paso 2.
5. **Reintento tras perder la respuesta**: misma clave y huella ⇒ `JobAccepted` del mismo trabajo con `replayed = true` (HTTP `200` + `Idempotent-Replay: true`; la primera respuesta es `202`).

### 8.4 Cargas (`file_upload`)

La huella de una carga es `{sha256, size}` y solo se conoce **al terminar** el stream:

| Situación con la misma clave | Resultado |
|---|---|
| Carga previa `stored` con igual `{sha256,size}` | devuelve el mismo archivo (`replayed`); los bytes nuevos se descartan |
| Carga previa `stored` con distinto contenido | `idempotency_conflict`; los bytes nuevos se descartan |
| Carga previa `reserved` cuyo plazo **no** venció | `upload_in_progress` (409, reintentable) |
| Carga previa `reserved` vencida o `abandoned` | se marca `abandoned` (si no lo estaba) y comienza una carga nueva con la misma clave (reinicio) |
| Carga previa `deleted`/`expired` (registro de idempotencia aún vigente) | devuelve el archivo con su estado (no descargable ni utilizable); **no** se crea otro mientras el registro exista |

`register_external_file` usa como huella `{resolver_id, SHA-256(ref)}` y, al terminar, compara también `{version_id, sha256, size}`.

### 8.5 Fijación (snapshot de ejecución)

En la aceptación (T1) se fija y persiste, inmutable para todos los intentos:

| Elemento | Contenido |
|---|---|
| Entrada | `file_id`, `sha256`, `size_bytes`, origen (`managed`/`external`/`external_materialized`), `version_id` externa; para texto: `file_id` del objeto de texto y su hash |
| Perfil | `profile_id`, `version_id`, `version_number`, `content_hash` (integridad), `selection` |
| Capacidades y contexto | capacidades **pedidas** y ejecutables según la versión; `context` como objeto administrado (hash), si existe |
| Contratos | `analysis_contract_version`, `inference_contract_version`, `job_contract_version = 1`, `document_structures = "document-structures/1"`, `result_contract_version = 1` |
| Extracción | `extractor { id, version, config_sha256 }`, bibliotecas pertinentes `{name, version}`, perfil de aislamiento exigido (§10.5) |
| Inferencia | procedencia del adaptador configurado (`kind`, `adapter_id`, `adapter_version`, `configuration_id`, `model`) y `timeout_s`; para el mock, su `configuration_id` (`fxs-…`) |
| Límites y política | los límites de §12 que afectan el resultado (archivo, páginas, unidades, texto, hallazgos) y la política de reintentos (`max_attempts`, `deadline_at`) |
| Retención | `retention_results_s`, `retention_metadata_s` vigentes al aceptar (§13.1) |

`execution_snapshot_hash` = SHA-256 del snapshot canónico; `requirements_key` = SHA-256 de `{extractor, adapter configuration_id, isolation_profile}` (lo que un worker debe poder reproducir). El snapshot no contiene texto, contexto ni definiciones (solo identidades y hashes).

**Durable sin reselección**: el camino durable usa **la versión ya fijada**; no vuelve a llamar a la selección pública por cada unidad ni por intento. Se lee la fila inmutable de la versión por `(scope_id, profile_id, version_number)` y se verifica `version_id` y `content_hash`. Por eso desactivar/eliminar el perfil **no** afecta a trabajos aceptados (D-A05-02).

### 8.6 Reproducibilidad y configuración no disponible

- Ningún reintento cambia silenciosamente de extractor, configuración de inferencia, versión de perfil ni límites.
- Un worker **solo reclama** trabajos cuyo `requirements_key` puede satisfacer (anuncia los suyos al reclamar). Si tras reclamar comprueba que no puede reconstruir exactamente la configuración (versión de biblioteca distinta, adaptador con otro `configuration_id`, aislamiento no disponible) ⇒ `configuration_unavailable` (no reintentable). Un trabajo que ningún worker compatible reclama antes de `deadline_at` ⇒ `failed` (`executor_unavailable`).
- **Reproducibilidad** = identidad y trazabilidad de entradas y configuración. **No** promete determinismo de un modelo futuro ni repetir una ejecución cuyas entradas ya expiraron.

### 8.7 Tras la retención

- Durante los 30 días de metadatos (D-A05-01), repetir la clave devuelve el trabajo original aunque su resultado haya expirado (`data_state = expired`): **no** provoca una nueva ejecución.
- Tras purgar los metadatos y el registro de idempotencia **no hay deduplicación histórica**: repetir la clave puede crear **otro** trabajo. Se documenta como comportamiento esperado.

## 9. Cambios administrativos después de aceptar (D-A05-02)

### 9.1 Reglas

| Cambio | Efecto sobre trabajos ya aceptados |
|---|---|
| Desactivar/eliminar el perfil | **ninguno**: siguen con la versión fijada (§8.5) |
| Publicar otra versión / cambiar el predeterminado | ninguno |
| Deshabilitar el scope | impide nuevas reclamaciones y publicaciones; los trabajos **no terminales** se cancelan (`cancel_reason = scope_disabled`). Un resultado **publicado antes** no se cancela retroactivamente |
| Rehabilitar el scope | no resucita trabajos cancelados; solo permite trabajos nuevos |
| Revocar una API key / deshabilitar su cliente | impide accesos posteriores con esa clave; **no** cancela trabajos aceptados |

### 9.2 Serialización frente a deshabilitar/rehabilitar (fence del scope)

Comprobar solo el estado actual no basta: un *disable* seguido de *enable* rápido entre la aceptación y la publicación dejaría el scope `active` en ambos instantes. Mecanismo (E-A05-10):

- `scope_job_fences (scope_id, observed_scope_revision, disable_generation)`. Cada trabajo guarda, al aceptarse, la `disable_generation` vigente (`fence_generation`).
- La aceptación, la reclamación y la publicación leen la fila del scope **bloqueándola contra cambios** (PostgreSQL `SELECT … FOR SHARE`, que excluye el `UPDATE` de un *disable* concurrente; SQLite: transacción de escritura) y comprueban: `status = 'active'`, `fence.observed_scope_revision = scopes.revision` y `job.fence_generation = fence.disable_generation`.
- Toda operación de estado del scope en un runtime A05 (`set_scope_status`, `update_scope`) actualiza el fence **en la misma transacción**: `observed_scope_revision = nueva revisión` y, si deshabilita, `disable_generation += 1` y cancela con un único `UPDATE` los trabajos no terminales del scope (acotados por la cuota de pendientes).
- **Runtimes anteriores a A05** (`0.1.0`–`0.3.0`) también incrementan `scopes.revision` en cada cambio de estado o metadatos, pero no tocan el fence. Si un runtime A05 observa `scopes.revision ≠ observed_scope_revision`, **reconcilia** en una transacción con el scope bloqueado (`FOR NO KEY UPDATE`): trata el cambio desconocido como posible deshabilitación (`disable_generation += 1`, cancela los no terminales, fija `observed_scope_revision`). Consecuencia documentada: durante un despliegue mixto, una edición de metadatos hecha por un runtime anterior también cancela trabajos pendientes del scope (conservador, nunca permisivo).
- Rehabilitar no reduce `disable_generation`: un trabajo aceptado antes jamás vuelve a coincidir.
- Límite: SQL arbitrario sobre las tablas del motor (fuera del paquete) queda fuera de la garantía (A01 §2.1).

### 9.3 Lecturas sobre scopes deshabilitados

Se conserva el contrato heredado: las **lecturas** permitidas siguen disponibles con scope deshabilitado (consultar trabajos, leer resultados publicados, descargar originales validados), sujetas a permisos y a expiración. Las **escrituras** (cargas, registros, `submit_*`) ⇒ `scope_disabled`. Cancelar y eliminar datos siguen permitidos (reducen exposición).

## 10. Seguridad del procesamiento documental

### 10.1 Validación (fase `validation`)

Sobre bytes **recibidos y verificados**, sin confiar en extensión, nombre ni MIME:

- **Tipo real**: firma y estructura (`%PDF-` y estructura de objetos; ZIP/OPC con `[Content_Types].xml` y la parte principal de WordprocessingML). Contenedores OLE (cifrado), `.docm`/macros, otros formatos ⇒ `unsupported_format` o el rechazo correspondiente.
- **Límites antes de abrir** (§10.2) y durante la lectura: bytes recibidos y bytes **realmente descomprimidos**.
- **ZIP/OPC**: número de entradas, tamaño declarado **y** producido por entrada y total, relación de compresión, nombres de parte (sin `..`, absolutos, duplicados por normalización, caracteres de control), relaciones (destinos internos existentes; `TargetMode="External"` ⇒ rechazo), tipos de contenido coherentes.
- **XML**: sin DTD (cualquier `<!DOCTYPE` ⇒ rechazo), sin entidades externas ni red, profundidad y cantidad de nodos acotadas, tamaño de atributos/texto acotado.
- **PDF**: tamaño de xref, cantidad de objetos, longitud de flujos declarada y producida, filtros permitidos, anidamiento de Form XObjects, actualizaciones incrementales, reparación del parser.
- **Rechazos heredados de A04** (D-A04-02 y matrices): cifrado (aunque la contraseña de usuario esté vacía), firmas, revisiones pendientes, objetos incrustados/adjuntos/OLE/macros, texto oculto, cuadros de texto, datos enlazados, referencias externas, formularios, capas, acciones ejecutables, `/ActualText`/`/Alt`, anotaciones no autorizadas, fuentes sin mapeo fiable, Type0/CID/Type3, estado de texto `Tc/Tw/Tz/Ts/TL`, estructura desconocida, **reparación del parser** (nunca es éxito normal).
- **Sin ejecutar** macros, scripts, acciones ni resolver recursos externos. **Sin saneamiento** que altere la entrada en silencio: la validación acepta o rechaza el archivo tal cual.

### 10.2 Límites aplicados antes de abrir

Los límites se entregan al parser **antes** de abrir (lectores ZIP con contador de bytes producidos y aborto; parser XML configurado sin DTD y con límites; apertura PDF con límites de objetos/flujos y sin reparación). Una comprobación posterior a la apertura no contiene el trabajo ya hecho y **no** se acepta como único control. Además, el proceso aislado tiene límites de memoria, CPU y tiempo (§10.5) como defensa en profundidad.

### 10.3 Antivirus opcional

`FileScanner` (protocolo opcional del host, **local**): puede añadir un rechazo (`file_rejected: scanner_rejected`) pero **no** sustituye la validación ni declara «libre de malware». Nunca se envían archivos a servicios externos de escaneo. Un escáner configurado que falla o no responde ⇒ `extraction_process_failed` (reintentable); nunca se omite en silencio.

### 10.4 Motivos cerrados de rechazo (`file_rejected`)

`unsupported_format`, `type_mismatch`, `encrypted`, `signed`, `tracked_changes`, `embedded_content`, `macros`, `external_reference`, `hidden_text`, `unsupported_structure`, `parser_repair`, `font_mapping_insufficient`, `unsupported_text_state`, `incremental_update`, `forms_or_layers`, `executable_action`, `alternate_text_duplication`, `decompression_limit`, `structure_limit`, `scanned_without_text`, `scanner_rejected`. Ningún motivo incluye contenido, nombres de parte ni valores.

### 10.5 Aislamiento del proceso de extracción

La validación estructural, el inventario y la extracción corren en un **proceso separado** controlado por el worker:

- Entorno **vacío** (sin heredar variables; sin credenciales de base, storage ni resolver; sin `HOME`), Python en modo aislado (`-I`), directorio de trabajo descartable propio, **solo** la entrada (por *pipe* o archivo de solo lectura) y una salida acotada (por *pipe*, ≤ `max_extraction_output_bytes`). Sin descriptores heredados salvo los necesarios.
- El padre **valida también la salida** del hijo (esquema estricto, límites, coherencia de offsets e identidad con el SHA-256 de la entrada) como si fuera no confiable.
- **Un subprocess no equivale a sandbox.** Perfiles de aislamiento (`isolation_profile`):

| Perfil | Controles | Uso |
|---|---|---|
| `linux_container_reference` | contenedor OCI restringido: sin red (`--network none`), raíz de solo lectura, `tmpfs` acotado para trabajo, usuario no privilegiado, `--cap-drop ALL`, `no-new-privileges`, `--pids-limit`, memoria y *swap* acotadas (`--memory` = `--memory-swap`), CPU acotada (`--cpus`), duración con terminación forzada; sin montajes salvo entrada/salida | **referencia verificable en Linux** (aceptación); no impone Docker a quien aporte equivalente |
| `host_provided` | el host aporta un `IsolationRunner` con controles equivalentes (gVisor, Firecracker, namespaces/bwrap, etc.) | producción en hosts con otra tecnología |
| `unconfined_development` | solo proceso separado + `rlimit` (memoria, CPU, archivos, procesos, core = 0) y plazo; **sin** aislamiento de red ni de filesystem garantizado | **solo desarrollo** (p. ej. macOS) y entradas confiables |

- **Verificación**: al iniciar el worker y periódicamente, un *canario* dentro del aislamiento intenta abrir red, escribir fuera del `tmpfs`, leer variables sensibles, crear procesos por encima del límite y exceder memoria; cualquier éxito ⇒ el perfil se declara no disponible.
- `untrusted_input = True` (predeterminado): exige `linux_container_reference` o `host_provided` verificados. **No hay fallback silencioso** a `unconfined_development`: sin aislamiento verificado el worker no reclama trabajos documentales (`isolation_unavailable` al iniciar). `unconfined_development` exige configuración explícita del operador y queda en el snapshot y en el resultado (`provenance.isolation_profile`).
- **macOS**: el desarrollo usa `unconfined_development`; sus garantías reales son solo separación de proceso, entorno limpio, `rlimit` (con las limitaciones de macOS para `RLIMIT_AS`) y plazo. No se presenta como sandbox.

### 10.6 Terminación

Al vencer el plazo, perder el lease o recibir cancelación, el worker termina el proceso (y su grupo/contenedor) y descarta su salida. Un hijo terminado por un límite propio ⇒ `limit_exceeded` (no reintentable); por causa externa ⇒ `extraction_process_failed` (reintentable).

## 11. Extracción, mapeo y análisis en A05

### 11.1 Incorporación al paquete

Se incorporan al paquete, en el extra **`documents`** y con **importación perezosa** (el núcleo liviano y `import cawbase_content_engine` no los cargan; gate D-33): **inventario**, **extracción**, **mapeo al original**, **validación de identidad y cobertura** y **orquestación con A03**. Implementación mantenible con pruebas propias de A05; `experiments/a04/` **no** se importa como runtime (se pueden reutilizar criterios, fixtures y diseño). La **transformación y restitución experimentales no** se incorporan. Bibliotecas candidatas (E-A05-14): `lxml` (parser endurecido) para DOCX y `pikepdf`/QPDF (MPL-2.0, D-A04-01) para PDF; versiones y licencias transitivas se fijan al implementar.

### 11.2 Matriz de soporte A05 (extracción y localización)

Solo **extracción/localización**; la capacidad de transformación es de A07/A08 y **no** se infiere de esta matriz.

| Formato | Soportado en A05 | Unidad | No cubierto (`N`, declarado) | Rechazo (`R`) |
|---|---|---|---|---|
| **DOCX** | cuerpo: párrafos y runs (DX-01); tablas, anidadas hasta profundidad 3 (DX-02/03) y celdas combinadas (DX-04); encabezados/pies default/first/even referenciados (DX-05); notas al pie y al final (DX-06); texto visible de hipervínculos (DX-07); resultados de campo extraídos (DX-08, no transformables); controles de contenido de texto simple (DX-14) | párrafo (incluidos los de celdas, encabezados, pies y notas), en el orden determinista de A04 §5 | imágenes, texto alternativo, destinos de enlace, instrucciones de campo, ecuaciones; **comentarios y metadatos/miniatura (ver §11.5)** | DX-11/12/13/17/22/24/25/26/27, partes huérfanas, `docVars`, `dataBinding`, profundidad o rejilla inválidas, estructura desconocida |
| **PDF** | subconjunto conservador de A04: fuentes simples (Type1 con `/Widths`, TrueType simple incrustada) con `ToUnicode`/codificación estándar fiable; `Tj`, `TJ`, `'`, `"`; páginas rotadas 0/90/180/270 y matrices de texto múltiplos de 90°; columnas; Form XObjects (con conteo de usos); ligaduras (`n:1`) | **página** (E-A04-25) | imágenes, trazados, anotaciones `Link`, ángulos no múltiplos de 90° (extraídos, no transformables), metadatos/comentarios (§11.5) | PX-02/07/13/14/15/16/17/18, Type0/CID/Type3, `Tc/Tw/Tz/Ts/TL`, escaneado, `FreeText` y otras anotaciones con apariencia, estructura desconocida |

- **No** se amplía automáticamente a Type0/CID ni a otras variantes pendientes; la comprobación representativa de **D-A04-04** pertenece al inicio de A08 y no bloquea A05. Entradas con object streams, `/Contents` en arreglo o linearizadas: soportadas para **lectura** solo si la extracción se demuestra con pruebas propias de A05; si no, `R` (`unsupported_structure`).
- Texto **exacto** (sin normalizar), offsets en **puntos de código Unicode**, separadores sintetizados declarados (`kind = synthesized`, no localizables), relaciones no uno-a-uno con dirección (`1:1`, `n:1`, `1:n`), reutilización de objetos (Form XObject: un segmento por uso) y **ambigüedad** declarada (intervalo no localizable ⇒ el hallazgo es válido como detección, no localizable para transformar). El mapa queda ligado a `file_sha256` + extractor + `config_sha256` (A04 §3).

### 11.3 Reutilización de A03 sin copiar su lógica

- Por cada unidad con texto, el worker construye la solicitud con las **mismas** funciones internas de A03 (preparación desde la versión fijada, `build_request`, invocación con timeout y protocolo cerrado de errores, `validate_output`, `build_result`), extraídas a una función interna común **sin** comprobación de permisos (como `select_published_version` en A03). `analyze_text` conserva su firma, su comportamiento, sus errores y su ruta; su fachada solo pasa a llamar a la función común.
- La preparación del camino durable parte de la **versión fijada** (§8.5), no de la selección pública (que rechaza perfiles desactivados).
- Límites por unidad = los de A03 (`max_analysis_text_chars` 8192, `max_analysis_text_bytes` 24 576, hallazgos, regiones, salida). `AnalysisResult` no recibe páginas, nodos ni campos nuevos.

### 11.4 Construcción del resultado documental

`DocumentAnalysisResult` (manifiesto, `result_contract_version = 1`) **envuelve** resultados por unidad; el `SourceMap` es un artefacto **separado** (`document-structures/1`):

```jsonc
// Propuesto / no disponible — esquema ilustrativo
{
  "result_contract_version": 1,
  "job_id": "job_…", "operation": "document_analysis",
  "document": { "file_id": "fil_…", "sha256": "…", "size_bytes": 48211, "format": "docx" },
  "profile": { "scope_id": "…", "profile_id": "…", "version_id": "pvr_…", "version_number": 3, "content_hash": "…", "selection": "default" },
  "provenance": { "inference": { /* Provenance de A03 */ }, "extractor": { "id": "cce-docx", "version": "1", "config_sha256": "…" },
                  "isolation_profile": "linux_container_reference" },
  "units": [
    { "unit_id": "u0001", "kind": "docx_paragraph", "ordinal": 1, "text_length": 87,
      "status": "analyzed", "analysis": { /* AnalysisResult de A03 sin cambios */ } },
    { "unit_id": "u0002", "kind": "docx_paragraph", "ordinal": 2, "text_length": 0, "status": "empty" }
  ],
  "coverage": {
    "extraction": { "textual_subset_complete": false, "document_coverage_complete": false,
                    "declared_not_covered": [ { "element_class": "comments", "count": 2, "reason": "not_analyzed" } ],
                    "authorized_removals_performed": [] },
    "inference": { "units_total": 2, "units_analyzed": 1, "units_empty": 1,
                   "per_capability": { "classification": { "complete": true, "units_inconclusive": 0 },
                                       "detection": { "complete": true, "units_inconclusive": 0 } } }
  },
  "status": "completed"   // completed | inconclusive (derivado; §11.6)
}
```

Reglas:

- **Sin agregación**: no se promedian puntuaciones ni se inventa una categoría ganadora global; cada unidad conserva su `AnalysisResult` (con su ranking/resolución locales) y su alcance local.
- **Unidad excesiva** (supera límites de A03) ⇒ el trabajo falla con `unit_too_large` (sin truncamiento ni segmentación semántica anticipada; segmentación productiva: A09/B-A04-08).
- **Unidad vacía** (texto de longitud 0 o solo separadores sintetizados) ⇒ `status = "empty"`, **no** se llama a A03 con texto vacío y no cuenta como analizada. Un texto solo de espacios **no** es vacío (se analiza, como en A03). **Documento sin texto** (todas las unidades vacías) ⇒ `failed` con `no_extractable_text`: nunca éxito por vacuidad.
- Una salida **válida** inconclusa (A03 `inconclusive`) se conserva con su cobertura; el documento queda `inconclusive`.
- Una salida **malformada** o un fallo operativo en **cualquier** unidad ⇒ el intento falla (§7.1); **no** se publica un resultado parcial como válido.
- Se publica el informe **solo** cuando todas las unidades tienen resultado (`analyzed` o `empty`) conforme al contrato completo.
- Sin porcentajes que oculten contenido no cubierto: los conteos son exactos y van acompañados de las listas declaradas.

`TextAnalysisJobResult` = `{ result_contract_version, job_id, operation: "text_analysis", analysis: AnalysisResult }`.

### 11.5 Eliminación autorizada ≠ eliminación efectuada (adaptación de `E`)

A04 autoriza eliminar comentarios y metadatos descriptivos **durante una transformación** (D-A04-02). **A05 no transforma ni elimina nada del original.** Por eso:

- Los elementos de clase `E` (comentarios DX-10/PX-11, propiedades DX-15/PX-10, miniatura DX-18) se **inventarían**, **no se analizan** y cuentan como **contenido no cubierto** (`declared_not_covered`, motivo `not_analyzed`), lo que impide `document_coverage_complete` (y `textual_subset_complete` si su clase contiene texto libre: comentarios, título/asunto/palabras clave, propiedades personalizadas).
- `authorized_removals_performed` es **siempre vacío** en A05; nunca se marca como eliminado algo que no se eliminó. La disposición `E` describe una **autorización** para A07/A08, no un hecho.

### 11.6 Indicadores con significado inequívoco

| Indicador | Verdadero si y solo si | No significa |
|---|---|---|
| `textual_subset_complete` | A04 §2.1 con la adaptación de §11.5: todo `S` extraído con mapa consistente, sin `R`, `P` inspeccionados, toda unidad no vacía con cobertura A03 completa en las capacidades pedidas, y ningún texto `E` sin analizar | calidad de detección, ausencia de datos sensibles ni protección |
| `document_coverage_complete` | `textual_subset_complete` y ningún `N` (incluidos los `E` no analizados) ni porción desconocida | lo mismo |
| `coverage.inference.per_capability.<cap>.complete` | todas las unidades analizadas tienen `coverage.complete = true` en esa capacidad | cobertura de lo no extraído |
| `status` del documento | `completed` ⇔ todas las unidades analizadas `completed`; si alguna `inconclusive` ⇒ `inconclusive` | protección ni cobertura documental completa |

Ninguno acredita calidad de detección (A09) ni protección (A06–A08). El mock solo acredita ingeniería.

## 12. Límites operativos (E-A05-17)

Elecciones técnicas iniciales de A05, configurables por el operador (`Limits`/`JobsSetup`; nombres propuestos). **No son mediciones** ni se heredan automáticamente de los experimentos A04 (§16 de A04). Deberán verificarse con pruebas de frontera reales; elegir un número no acredita capacidad ni rendimiento. Exceder un límite ⇒ rechazo explícito, nunca truncamiento.

### 12.1 Entrada y estructura

| Límite (propuesto) | Default | Unidad | Ámbito | Validación | Enforcement | Error |
|---|---|---|---|---|---|---|
| `max_file_bytes` | 25 MiB | bytes recibidos | archivo | 1 KiB–256 MiB | contador del stream (HTTP/Python/resolver) | `limit_exceeded` (HTTP 413) |
| `max_pdf_pages` | 100 | páginas | documento | 1–1000 | antes de extraer, al leer el árbol de páginas | `file_rejected: structure_limit` |
| `max_zip_entries` | 1000 | entradas | DOCX | 10–10 000 | al leer el directorio central | `file_rejected: structure_limit` |
| `max_docx_parts_processed` | 300 | partes XML | DOCX | ≤ `max_zip_entries` | inventario | `structure_limit` |
| `max_decompressed_total_bytes` | 200 MiB | bytes producidos | archivo | ≥ `max_file_bytes` | lector con contador y aborto | `decompression_limit` |
| `max_decompressed_entry_bytes` | 50 MiB | bytes producidos | entrada ZIP / flujo PDF | — | ídem | `decompression_limit` |
| `max_compression_ratio` | 100:1 | relación | entrada / flujo | 10–1000 | declarado **y** producido | `decompression_limit` |
| `max_xml_depth` | 64 | niveles | parte XML | 16–256 | parser | `structure_limit` |
| `max_xml_nodes_total` | 2 000 000 | nodos | documento | — | parser con contador | `structure_limit` |
| `max_table_nesting` | 3 | niveles | DOCX | 1–8 | inventario | `structure_limit` |
| `max_pdf_objects` | 200 000 | objetos | PDF | — | xref | `structure_limit` |
| `max_pdf_content_bytes_per_page` | 8 MiB | bytes decodificados | página | — | decodificación | `structure_limit` |
| `max_form_xobject_depth` | 8 | niveles | página | 1–32 | intérprete | `structure_limit` |
| `max_text_operators_per_page` | 100 000 | operadores | página | — | intérprete | `structure_limit` |

### 12.2 Extracción, resultado y tiempos

| Límite | Default | Unidad | Ámbito | Enforcement | Error |
|---|---|---|---|---|---|
| Texto por unidad | A03: 8192 / 24 576 | puntos de código / bytes | unidad | antes de invocar A03 | `unit_too_large` |
| `max_units_per_document` | 5000 | unidades | documento | extracción | `limit_exceeded` |
| `max_document_text_chars` | 2 000 000 | puntos de código | documento | extracción | `limit_exceeded` |
| `max_document_findings` | 50 000 | hallazgos | documento | acumulación (además de 500 por unidad de A03) | `limit_exceeded` |
| `max_source_map_bytes` | 64 MiB | bytes JSON canónico | artefacto | serialización | `limit_exceeded` |
| `max_result_bytes` | 32 MiB | bytes JSON canónico | artefacto | serialización | `limit_exceeded` |
| `max_extraction_output_bytes` | 96 MiB | bytes | salida del hijo | *pipe* con contador | `limit_exceeded` |
| `extraction_timeout_s` | 120 | segundos de pared | proceso aislado | terminación forzada | `limit_exceeded` |
| `extraction_cpu_s` / `extraction_memory_bytes` / `extraction_max_pids` | 120 / 1 GiB / 32 | — | proceso aislado | `rlimit` / contenedor | `limit_exceeded` |
| `unit_inference_concurrency` | 4 | llamadas | intento | semáforo | — |
| Timeout por unidad | `InferenceSetup.timeout_s` (A03, 30 s) | s | llamada | A03 | `inference_timeout` |
| `max_attempt_duration_s` | 3600 | s | intento | lease acotado | `lease_lost`/T7 |
| `max_job_duration_s` | 86 400 (24 h) | s | trabajo, desde `accepted_at` | `deadline_at` | `deadline_exceeded` |
| `max_attempts` | 5 | intentos | trabajo | T7/T8 | `retries_exhausted` |
| Backoff | 30 s base, ×2, máx. 15 min, *jitter* [d/2, d] | — | reintento | T7 | — |
| `lease_ttl_s` / `heartbeat_interval_s` | 60 / 20 | s | intento | §6.3 (`heartbeat_interval_s ≤ lease_ttl_s / 3`) | `lease_lost` |
| `upload_timeout_s` / `upload_idle_timeout_s` | 600 / 60 | s | carga | servidor / lector | `upload_aborted` |
| `download_timeout_s` | 600 | s | descarga | servidor | conexión abortada |
| `purge_grace_s` | `download_timeout_s` + 300 | s | eliminación física | §4.3 | — |

### 12.3 Concurrencia, pendientes y storage (cuotas)

| Límite | Default | Ámbito | Enforcement | Error |
|---|---|---|---|---|
| `max_active_jobs_per_scope` (`queued`+`retry_wait`+`running`) | 100 | scope | contador en `scope_usage` bajo bloqueo en la aceptación | `quota_exceeded` (429) |
| `max_active_jobs_total` | 10 000 | instalación | `engine_usage` | `quota_exceeded` |
| `max_running_jobs_per_scope` | 4 | scope | reclamación | (no reclama) |
| Concurrencia del ejecutor de referencia | 4 trabajos | proceso | ejecutor | — |
| `max_live_files_per_scope` | 1000 | scope | aceptación de carga/registro | `quota_exceeded` |
| `max_storage_bytes_per_scope` | 2 GiB | scope (originales, texto, resultados, mapas, temporales **incluidos** los reservados) | reserva en paso 1 (§4) | `quota_exceeded` |
| `max_storage_bytes_total` | 100 GiB | instalación | ídem | `quota_exceeded` |
| `cleanup_batch_size` | 500 | lote | limpieza | — |
| `admin_events_purge_batch_size` | 1000 (máx. 10 000) | lote | purga | `validation_failed` fuera de rango |

- **Reserva concurrente segura**: una carga reserva `max_file_bytes` (o `declared_size` si es menor) **antes** de recibir bytes; un intento reserva `max_result_bytes + max_source_map_bytes` antes de escribir artefactos. La reserva y el chequeo se hacen en una transacción corta con la fila `scope_usage` bloqueada (PostgreSQL `FOR UPDATE`; SQLite transacción de escritura) y la de `engine_usage` (orden fijo: scope → instalación). Al registrar (paso 4) se ajusta al tamaño real; ante fallo, abandono o descarte la reserva se **libera**. La limpieza recalcula los contadores desde las filas (reparación de deriva) en cada lote.
- Los bytes cuentan hasta la **eliminación física**, no hasta la expiración lógica: aceptar archivos no permite crecimiento ilimitado antes de la limpieza. Si la limpieza no corre, las cuotas bloquean nuevas cargas.

## 13. Retención, expiración y purga (D-A05-01)

### 13.1 Plazos y persistencia de plazos efectivos

| Dato | Plazo predeterminado | Desde | Persistido |
|---|---|---|---|
| Originales administrados (y texto/contexto de entrada, materializaciones); registros de referencias externas | 7 días | `received_at` (§3.6): hora de la autoridad temporal al confirmar la recepción completa y durable | `retention_s`, `expires_at` |
| Resultados y mapas documentales | 7 días | `terminated_at` (terminación del trabajo) | `result_retention_s`, `result_expires_at` |
| Temporales abandonados (reservas no completadas, artefactos `discarded`, temporales del filesystem) | 24 horas | `abandoned_at`: momento en que se **determina** el abandono según la posesión (vencimiento del plazo de carga o del lease, terminación del intento) | `abandoned_at`, `delete_after` |
| Metadatos mínimos de trabajos y registros de idempotencia | 30 días | `terminated_at` | `metadata_expires_at` (trabajo e idempotencia) |
| `admin_events` | 90 días | `occurred_at` | política (§13.6) |

- Configurables por el operador. **No hay conservación indefinida implícita** (un valor «infinito» no se admite; máximo configurable 3650 días) **ni extensión silenciosa** por reintentos, consultas, leases o nuevos trabajos.
- Los plazos **efectivos** se copian a cada recurso al crearlo/terminarlo; cambiar los defaults solo afecta recursos nuevos (no reinterpreta retrospectivamente). Un trabajo copia en su snapshot (§8.5) los plazos de resultados y metadatos vigentes al aceptarse y los aplica al terminar.
- Para trabajos no terminales, la fila sigue viva mientras exista; su `deadline_at` (24 h) acota su vida activa.

### 13.2 Admisión y expiración de la entrada

- La entrada **no** se prolonga por crear trabajos ni por reintentar.
- **Admisión**: `submit_document_analysis` exige `file.expires_at ≥ deadline_at` del nuevo trabajo; si no ⇒ `file_expiring` (409). Con los defaults, un original con más de 6 días ya no admite trabajos nuevos.
- Si la entrada vence, se elimina o cambia durante `queued`/`retry_wait`/`running` ⇒ la reclamación o la publicación lo detectan (§6.4) y el trabajo pasa a `failed` (`input_unavailable`/`source_changed`): **no** se publica un resultado obtenido de una entrada cuya disponibilidad autorizada venció.

### 13.3 Metadatos mínimos (30 días)

Tras expirar resultados (7 días) el trabajo conserva **solo**: `job_id`, `scope_id`, `operation`, estado, fase final, timestamps, `cancel_reason`, `failure_code`/`last_error_code`, conteo de intentos, `attempt` (outcome, códigos, tiempos), identidad de la entrada (`file_id`, `sha256`, `size_bytes`), identidad fijada (perfil, versión, `content_hash`, extractor, procedencia del adaptador, contratos, `execution_snapshot_hash`), `request_fingerprint`, `key_hash` y `data_state`. **Nunca**: texto, contexto, fragmentos, resultados, copias de mapas ni definiciones. Los hashes e identificadores siguen sujetos a autorización (`jobs:read`) y **no** se escriben en logs por defecto.

### 13.4 Expiración lógica

Al vencer `expires_at`/`result_expires_at`, el acceso se **deniega** de inmediato (comparación con la autoridad temporal en cada lectura), aunque la purga física esté pendiente: descargas y lecturas responden `resource_not_found` (archivos) o `job_data_expired` (410, resultados de un trabajo todavía consultable). La transición persistida (`expired`) la hace la limpieza.

### 13.5 Limpieza explícita (`run_cleanup`)

- Operación **explícita, repetible, por lotes y recuperable**; no nace al importar ni al iniciar el motor. Se invoca desde el host (`engine.maintenance().run_cleanup(...)`) o desde el ejecutor de referencia (opción `cleanup_interval_s`) o la CLI propuesta `cawbase-content-engine cleanup`.
- Cada lote, en transacciones cortas: (1) recupera leases vencidos (§6.5) y trabajos con `deadline_at` vencido; (2) marca reservas/artefactos abandonados (`abandoned_at`, `delete_after = abandoned_at + 24 h`); (3) marca expirados archivos y resultados (`delete_after = expiración + purge_grace_s`); (4) reclama con marca condicional y elimina físicamente los objetos con `delete_after ≤ ahora` (fuera de transacción; `delete` idempotente) y registra `physically_deleted_at`; (5) borra metadatos de trabajos e idempotencia con `metadata_expires_at ≤ ahora` (y sus intentos y artefactos ya purgados); (6) recalcula cuotas.
- Compatible con procesos caídos: toda marca tiene plazo; otra limpieza retoma lo que quedó a medias. Devuelve un resumen **sin contenido** (conteos por clase, fallos por código).
- **Nunca** elimina originales externos del host, ni filas de scopes, clientes, claves, grants, perfiles o versiones.

### 13.6 `admin_events`: retención y purga (resuelve Q-04 de A01)

- Política: 90 días desde `occurred_at`, aplicada a **todos** los eventos, **incluidos los anteriores a A05** (hasta ahora conservados indefinidamente sin purga). Como la tabla no tiene una columna de vencimiento por fila, el plazo no se persiste por evento: la purga es explícita y el operador la aplica con un corte. La **primera** ejecución tras A05 puede eliminar eventos históricos de más de 90 días; se recomienda ejecutarla primero en previsualización.
- Operación local de operador / Python privilegiado (`engine.maintenance().purge_admin_events(older_than=…, batch_size=…, dry_run=True)`; CLI propuesta `cawbase-content-engine purge-admin-events --older-than-days N --batch-size M [--execute]`). **Sin endpoint HTTP.**
- Corte y tamaño de lote **explícitos**; el corte debe ser `≤ ahora − admin_events_retention` (90 días configurables; impide borrar eventos recientes por error). **Previsualización** predeterminada (cuenta lo que se eliminaría, sin borrar). Ejecución en lotes con transacciones cortas (`DELETE … WHERE event_id IN (SELECT … ORDER BY event_id LIMIT :n)`), repetible y reanudable.
- Resumen **sin contenido**: cantidad eliminada, rango de `occurred_at` y de `event_id` afectados.
- La **migración no purga** datos. `admin_events` sigue **append-only para operaciones ordinarias**; esta purga autorizada es la única excepción documentada. No se eliminan scopes, clientes, claves, perfiles ni versiones por esta política. Sin promesa de archivo histórico externo ni de cumplimiento normativo.
- El fence de §9.2 **no** depende de `admin_events`, por lo que la purga no afecta la serialización.

### 13.7 Correspondencias de A06

Las correspondencias de seudonimización (A06) tendrán **autorización, retención y eliminación propias**, definidas en A06. A05 no crea tablas, APIs, criptografía ni semántica de restauración. El `SourceMap` documental **no** es una tabla reversible de valores sensibles: contiene posiciones y referencias estructurales, no correspondencias valor↔sustituto.

## 14. API Python (propuesta / no disponible)

```python
# Propuesto / no disponible — ejemplo ilustrativo
engine = ContentEngine(
    config,
    inference=InferenceSetup(adapter),                       # A03, sin cambios
    storage=StorageSetup(managed=FilesystemStorage("/var/lib/cce/objects"),
                         external_resolvers={"host-assets": HostAssetsResolver(...)}),
    documents=DocumentSetup(isolation=LinuxContainerIsolation(...), untrusted_input=True),   # extra documents
    jobs=JobsSetup(),                                        # límites y retenciones (§12, §13)
)
```

### 14.1 `ScopedOperations` (nuevos métodos)

| Método | Permiso | Resultado | `HostTransaction` |
|---|---|---|---|
| `upload_file(scope_id, chunks: AsyncIterable[bytes], *, idempotency_key, expected_sha256=None, declared_size=None)` | `files:write` | `UpsertResult[StoredFile]` | rechazada |
| `register_external_file(scope_id, *, resolver_id, ref, idempotency_key)` | `files:write` | `UpsertResult[StoredFile]` | rechazada |
| `get_file(scope_id, file_id)` / `list_files(scope_id, *, state=None, limit=50, cursor=None)` | `files:read` | `StoredFile` / `Page[StoredFile]` | permitida |
| `open_file_content(scope_id, file_id)` | `files:read` | `AsyncIterator[bytes]` (verificado) | rechazada |
| `delete_file(scope_id, file_id)` | `data:delete` | `StoredFile` (`deleted`) | permitida |
| `submit_document_analysis(scope_id, *, file_id, idempotency_key, profile_id=None, version_number=None, capabilities=("classification","detection"), context=None)` | `analysis:execute` | `JobAccepted` | permitida (sin `context`) |
| `submit_text_analysis(scope_id, *, text, idempotency_key, profile_id=None, version_number=None, capabilities=…, context=None)` | `analysis:execute` | `JobAccepted` | rechazada (escribe en storage) |
| `get_job(scope_id, job_id)` / `list_jobs(scope_id, *, state=None, operation=None, limit=50, cursor=None)` | `jobs:read` | `JobInfo` / `Page[JobInfo]` | permitida |
| `cancel_job(scope_id, job_id)` | `jobs:cancel` | `JobInfo` (+ `cancel_outcome`) | permitida |
| `get_job_result(scope_id, job_id)` | `results:read` | `DocumentAnalysisResult` \| `TextAnalysisJobResult` | rechazada |
| `open_job_artifact(scope_id, job_id, kind)` (`result` \| `source_map`) | `results:read` | `AsyncIterator[bytes]` | rechazada |
| `delete_job_data(scope_id, job_id)` | `data:delete` | `JobInfo` (`data_state = deleted`) | permitida |

`context` en `submit_document_analysis` se guarda como objeto administrado de texto (por eso no admite `HostTransaction` cuando se envía). `submit_*` sin `InferenceSetup` ⇒ `inference_not_configured`; sin `StorageSetup` ⇒ `storage_not_configured`; `document_analysis` sin `DocumentSetup` (o sin el extra) ⇒ `documents_not_configured`. `analyze_text` **no cambia**.

### 14.2 Superficies confiables locales (sin HTTP, sin `ScopeContext`)

Workers y mantenimiento operan sobre todos los scopes; son código confiable del host/operador con acceso a la base (mismo límite que A01 §2.1 y que bootstrap). **No** se exponen por HTTP (ni reclamación, ni *fencing tokens*, ni publicación arbitraria). `AdminContext` **no** da acceso a ellas ni a contenido.

```python
# Propuesto / no disponible
worker = engine.job_worker(worker_id="w-1")            # sin E/S al crear
claimed = await worker.claim(limit=1)                    # -> list[ClaimedJob]
outcome = await worker.run_job(job_id)                   # reclama ESE trabajo si es elegible y lo ejecuta completo
await worker.run_claimed(claimed[0])                     # ejecuta uno ya reclamado (heartbeat, fases, publicación)

executor = ReferenceExecutor(engine, concurrency=4, poll_interval_s=5, cleanup_interval_s=300)
await executor.start(); ...; await executor.aclose()     # explícito; CLI propuesta: `cawbase-content-engine worker`

maint = engine.maintenance()
await maint.list_runnable_jobs(limit=100, older_than_s=60)
await maint.recover_expired_leases(limit=100)
await maint.run_cleanup(batch_size=500)                  # -> CleanupSummary (sin contenido)
await maint.purge_admin_events(older_than=cutoff, batch_size=1000, dry_run=True)   # -> PurgeSummary
```

`run_job` devuelve `JobRunOutcome { job_id, state, attempt_no, outcome_code }`: el host puede llamarlo desde su propio worker al recibir el aviso de su cola, sin usar el ejecutor de referencia.

### 14.3 Tipos (esquemas ilustrativos)

```python
class StoredFile:      scope_id; file_id; origin; state; validation_state; rejection_reason
                       size_bytes; sha256; detected_format; received_at; expires_at; created_at
                       # sin storage_key, ref externa, resolver ni nombre original
class JobAccepted:     job: JobInfo; replayed: bool
class JobInfo:         scope_id; job_id; operation; state; phase; attempt_count; max_attempts
                       cancel_requested_at; cancel_reason; failure_code; last_error_code
                       accepted_at; started_at; terminated_at; deadline_at
                       input: {file_id, sha256, size_bytes}; profile: ProfileRef (A03)
                       data_state; result_expires_at; metadata_expires_at
                       # sin contenido; sin lease_token
class ClaimedJob:      job_id; scope_id; attempt_no; lease_token; lease_expires_at; phase   # solo superficie local
```

## 15. HTTP (propuesto / no disponible)

Mismas operaciones del núcleo (D-07), extra `http`, orden de A01 (límite de cuerpo → autenticación → contexto de scope 404 → validación → operación). Todas bajo `/v1/scopes/{scope_id}`. Las rutas JSON conservan el límite de **64 KiB**.

| Método y ruta | Permiso | Cuerpo / respuesta | Notas |
|---|---|---|---|
| `POST /files` | `files:write` | cuerpo binario por stream (≤ `max_file_bytes`) → `201 StoredFile` / `200` + `Idempotent-Replay: true` | `Idempotency-Key` obligatorio; `Content-Type: application/octet-stream`; `X-Content-SHA256` opcional (solo para rechazar discrepancias); 413 por límite; 409 `upload_in_progress` |
| `GET /files` · `GET /files/{file_id}` | `files:read` | `200 Page[StoredFile]` / `StoredFile` | |
| `GET /files/{file_id}/content` | `files:read` | `200` binario por stream | solo `stored`+`passed`, no vencido; `no-store`, `attachment`, `nosniff` |
| `DELETE /files/{file_id}` | `data:delete` | `200 StoredFile` | eliminación lógica; repetible |
| `POST /document-analysis-jobs` | `analysis:execute` | `{file_id, profile_id?, version_number?, capabilities?, context?}` → `202 JobAccepted` / `200` replay | `Idempotency-Key` obligatorio; `Location: …/jobs/{job_id}` |
| `POST /text-analysis-jobs` | `analysis:execute` | `{text, profile_id?, version_number?, capabilities?, context?}` (≤ 64 KiB) → `202` / `200` | ídem |
| `GET /jobs` · `GET /jobs/{job_id}` | `jobs:read` | `200 Page[JobInfo]` / `JobInfo` | `state`, `operation`, `limit`, `cursor` |
| `POST /jobs/{job_id}/cancel` | `jobs:cancel` | `200 JobInfo` | estado objetivo; repetible |
| `GET /jobs/{job_id}/result` | `results:read` | `200` JSON (manifiesto) | `no-store`, `attachment`; 409 `job_not_succeeded`; 410 `job_data_expired` |
| `GET /jobs/{job_id}/source-map` | `results:read` | `200` JSON | ídem; solo `document_analysis` |
| `DELETE /jobs/{job_id}/data` | `data:delete` | `200 JobInfo` | elimina lógicamente resultado y mapa |

No hay rutas de referencias externas, reclamación, *heartbeat*, publicación, limpieza ni purga; ninguna ruta acepta URLs, rutas locales, nombres de resolver ni credenciales. No hay URLs firmadas. `POST /v1/scopes/{scope_id}/analyze-text` no cambia. Un recurso de otro scope o inexistente ⇒ `404 resource_not_found` idéntico.

## 16. Permisos (elección técnica E-A05-12)

El catálogo pasa de **11** a **17** permisos (6 nuevos, todos de scope). Sin jerarquías ni comodines; ninguno concede a otro; `AdminContext` y `admin:*` no conceden acceso a archivos, trabajos ni resultados.

| Permiso | Habilita |
|---|---|
| `analysis:execute` (A03, reutilizado) | además de `analyze_text`: **enviar** trabajos `document_analysis`/`text_analysis` con archivos **del propio scope**. No concede leer el archivo, consultar trabajos ni leer resultados |
| `files:write` | cargar archivos y registrar referencias externas (Python) |
| `files:read` | metadatos de archivos y **descarga de originales** validados |
| `jobs:read` | consultar estado y metadatos de trabajos (sin resultados) |
| `jobs:cancel` | cancelar trabajos |
| `results:read` | leer resultados publicados y mapas documentales (**sensibles**) |
| `data:delete` | eliminar lógicamente archivos y datos de resultados |

- El scope es la unidad de aislamiento: con `analysis:execute` + `results:read` un llamador puede derivar información de cualquier archivo del scope; una granularidad por archivo/usuario es responsabilidad del host (B-A05-05).
- **Restricción de despliegue** (heredada): no emitir claves con los permisos nuevos hasta actualizar **todos** los runtimes que acceden a la base; un runtime anterior falla al cargar grants desconocidos y revocar no lo corrige. No se modifican grants existentes ni se implementa ahora B-A02-12/B-A03-10.

## 17. Errores nuevos (propuestos)

Se reutilizan la jerarquía `EngineError` y el envoltorio de A01. Mensajes genéricos, sin contenido.

| Código | HTTP | Reintentable | Cuándo |
|---|---|---|---|
| `storage_not_configured` / `documents_not_configured` | 503 | no | falta `StorageSetup` / `DocumentSetup` o el extra |
| `storage_unavailable` | 503 | sí | fallo transitorio de storage/resolver |
| `isolation_unavailable` | — (superficie local) | no | el worker no puede verificar el perfil de aislamiento exigido; no reclama trabajos documentales (§10.5) |
| `stored_object_unavailable` | 503 | no | objeto perdido o corrupto (§4.1) |
| `upload_in_progress` | 409 | sí | carga con la misma clave aún vigente |
| `upload_aborted` | — (conexión) / 400 | sí (reiniciar) | desconexión, plazo o inactividad |
| `content_mismatch` | 422 | no | hash/tamaño declarados no coinciden |
| `file_not_usable` | 409 | no | archivo no `stored`, rechazado, eliminado o vencido |
| `file_expiring` | 409 | no | `expires_at < deadline_at` (§13.2) |
| `quota_exceeded` | 429 | sí (tras liberar) | cuotas de §12.3 |
| `job_not_succeeded` | 409 | no | resultado pedido de un trabajo no `succeeded` |
| `job_data_expired` | 410 | no | resultado vencido o eliminado |
| `idempotency_conflict` (A01) | 409 | no | misma clave, solicitud distinta |
| `transaction_incompatible` (A01) | — | no | operación larga con `HostTransaction` |
| Códigos de trabajo (`failure_code`) | — | — | §7.1 y §10.4 (no son errores de API; se informan en `JobInfo`) |

Se reutilizan `permission_denied`, `resource_not_accessible`, `resource_not_found`, `validation_failed`, `limit_exceeded` (413/422), `scope_disabled`, `default_profile_not_set`, `profile_not_selectable`, `profile_not_published`, `capabilities_not_configured`, `inference_not_configured`, `database_unavailable`, `internal_error`.

## 18. Persistencia y migración `0003` (propuesta)

Todas las tablas en el namespace fijo (A01 D-15). `scope_id` forma parte de **toda** PK y FK. Ninguna garantía depende solo de FK o `CHECK` (SQLite con FK inactivas): el motor valida pertenencia en código.

| Tabla | Clave | Columnas principales | Índices / restricciones |
|---|---|---|---|
| `stored_files` | (`scope_id`, `file_id`) | `origin`, `state`, `validation_state`, `rejection_reason`, `storage_backend_id`, `storage_key` (NULL si externa), `resolver_id`, `external_ref`, `external_version`, `size_bytes`, `sha256` (32 B), `detected_format`, `reserved_at`, `received_at`, `retention_s`, `expires_at`, `abandoned_at`, `deleted_at`, `delete_after`, `purge_claimed_at`, `purge_claim_expires_at`, `physically_deleted_at`, `upload_deadline_at`, `created_at` | UNIQUE `file_id`; UNIQUE (`storage_backend_id`, `storage_key`) donde no nulo; (`state`, `delete_after`); (`expires_at`); (`scope_id`, `state`, `created_at`); `CHECK` de estados |
| `file_validations` | (`scope_id`, `file_id`, `validator_config_sha256`) | `validator_id`, `validator_version`, `result` (`passed`/`rejected`), `reason`, `validated_at` | FK → `stored_files` |
| `jobs` | (`scope_id`, `job_id`) | `operation`, `state`, `phase`, `input_file_id`, `context_file_id`, `profile_id`, `version_id`, `version_number`, `profile_content_hash`, `selection`, `capabilities`, `execution_snapshot` (json, sin contenido), `execution_snapshot_hash`, `requirements_key`, `request_fingerprint`, `fence_generation`, `attempt_no`, `max_attempts`, `lease_token`, `lease_owner`, `lease_expires_at`, `heartbeat_at`, `available_at`, `accepted_at`, `started_at`, `terminated_at`, `deadline_at`, `cancel_requested_at`, `cancel_reason`, `failure_code`, `last_error_code`, `published_attempt_no`, `result_artifact_id`, `source_map_artifact_id`, `data_state`, `result_retention_s`, `result_expires_at`, `metadata_retention_s`, `metadata_expires_at`, `updated_at` | UNIQUE `job_id`; (`state`, `requirements_key`, `available_at`) para reclamar; (`state`, `lease_expires_at`) para recuperar; (`scope_id`, `state`, `accepted_at`); (`metadata_expires_at`); `CHECK` de estados y coherencia (terminal ⇔ `terminated_at` no nulo) |
| `job_attempts` | (`scope_id`, `job_id`, `attempt_no`) | `worker_id`, `lease_token_hash`, `claimed_at`, `ended_at`, `outcome`, `error_code` | FK → `jobs` |
| `job_artifacts` | (`scope_id`, `artifact_id`) | `job_id`, `attempt_no`, `kind`, `state`, `storage_backend_id`, `storage_key`, `size_bytes`, `sha256`, `reserved_at`, `written_at`, `abandoned_at`, `expires_at`, `delete_after`, `purge_claimed_at`, `purge_claim_expires_at`, `physically_deleted_at` | UNIQUE (`storage_backend_id`, `storage_key`); (`state`, `delete_after`); FK → `jobs` |
| `idempotency_records` | (`scope_id`, `operation`, `key_hash`) | `request_fingerprint`, `job_id` \| `file_id`, `created_at`, `expires_at` | (`expires_at`) |
| `scope_job_fences` | `scope_id` | `observed_scope_revision`, `disable_generation`, `updated_at` | FK → `scopes`; fila creada perezosamente bajo el bloqueo del scope |
| `scope_usage` | `scope_id` | `reserved_bytes`, `stored_bytes`, `live_files`, `active_jobs`, `running_jobs`, `updated_at` | FK → `scopes`; `CHECK ≥ 0` |
| `engine_usage` | `id = 1` | `reserved_bytes`, `stored_bytes`, `active_jobs`, `updated_at` | fila única |

**Migración `0003_jobs_files`** (aditiva, solo hacia adelante, estilo *expandir*): crea las tablas e índices; no altera tablas existentes (ningún `ALTER` de `scopes`, `admin_events` ni grants); no migra ni purga datos; sube `schema_level` de 2 a **3**. Se ejecuta con el procedimiento de A01 (una transacción, un bloqueo, plan tras el bloqueo; objetos ajenos intactos).

**Niveles y compatibilidad semántica** (E-A05-11):

- Runtime A05 requiere `schema_level ≥ 3`.
- `min_runtime_level` **permanece en 1**: un runtime `0.1.0`–`0.3.0` puede operar sobre el esquema 3 **sin romper las garantías de A05**, por razones verificables, no solo porque puede leer tablas: (a) no conoce las operaciones de trabajos y no puede aceptar, reclamar ni publicar; (b) sus cambios de estado o metadatos del scope incrementan `scopes.revision`, que el fence de §9.2 detecta y trata conservadoramente; (c) desactivar/eliminar perfiles o revocar claves no afecta trabajos aceptados por diseño (D-A05-02); (d) no ejecuta limpieza ni purga.
- Si una revisión posterior hiciera depender una garantía de que todos los runtimes apliquen reglas nuevas, deberá subir `min_runtime_level`, sabiendo que esa comprobación ocurre en `start()`/readiness y **no** detiene procesos ya iniciados.
- **Orden de despliegue**: migrar (credencial DDL) → actualizar todos los runtimes (API, workers, servicios) → recién entonces emitir claves con permisos nuevos y habilitar workers/ejecutor/limpieza.
- **Permisos de base del runtime**: A05 necesita además `DELETE` sobre sus propias tablas (limpieza de metadatos). La purga de `admin_events` necesita `DELETE` sobre esa tabla: se recomienda concederlo solo al rol de mantenimiento.

## 19. Transacciones por operación (`HostTransaction`)

| Operación | Con `HostTransaction` | Motivo |
|---|---|---|
| `upload_file`, `register_external_file`, `submit_text_analysis`, `submit_document_analysis` con `context`, `open_file_content`, `get_job_result`, `open_job_artifact` | **rechazada antes de efectos** (`transaction_incompatible`) | transferencias/E/S externa; un rollback del host dejaría objetos sin intención registrada |
| `submit_document_analysis` sin `context`, `cancel_job`, `delete_file`, `delete_job_data`, `get_file`, `list_files`, `get_job`, `list_jobs` | permitida, en un **savepoint** (A01 D-23) | operaciones breves solo de base. El host avisa a su cola **tras su commit** (§6.6); si revierte, el trabajo no existe |
| Superficies locales (`job_worker`, `maintenance`) | no aceptan transacción | propias y cortas |

Compartir engine **no** implica compartir transacción. Con `HostTransaction` las filas bloqueadas (scope, uso) quedan bloqueadas hasta el commit del host: el host mantiene corta su transacción.

## 20. Observabilidad

- Registradores propuestos: `cawbase_content_engine.jobs`, `.files`, `.storage`, `.maintenance`. Campos permitidos: `job_id`, `file_id`, `artifact_id`, `scope_id`, `operation`, `state`, `phase`, `attempt_no`, `worker_id`, `error_code`, `failure_code`, conteos, tamaños, duraciones, `request_id`. **Prohibidos**: contenido, texto, contexto, fragmentos, resultados, mapas, hashes de contenido, claves de storage, referencias externas, nombres de resolver con datos del host, `lease_token`, claves de idempotencia.
- `admin_events`: A05 **no** escribe eventos por trabajo o archivo (volumen y contenido); solo las acciones de mantenimiento con efectos globales (`maintenance.admin_events_purged` con conteos) y la reconciliación del fence (`scope.job_fence_reconciled`).
- Readiness no cambia (motor y base). Estados de worker/aislamiento se informan por la superficie local.

## 21. Fuera de alcance

Protección, redacción, seudonimización, correspondencias, criptografía y restauración (A06–A08); transformación DOCX/PDF; OCR; modelo real, calidad, segmentación por modelo (A09); integración con CAWbase (B01); infraestructura AWS y encendido/apagado; URLs firmadas; protocolos de carga *multipart*/reanudable; colas externas obligatorias; garantía de exactamente una vez; archivo histórico de eventos.
