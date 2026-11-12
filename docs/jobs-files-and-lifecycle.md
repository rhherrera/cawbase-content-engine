# Trabajos, archivos y ciclo de vida — contrato de A05

> **Estado: ESPECIFICADO / NO IMPLEMENTADO (A05, revisión 2 de la especificación, pendiente de revisión).** Ninguna operación, ruta, permiso, tabla, migración, extra, variable ni comando de este documento existe en `0.3.0`: todo es **propuesto / no disponible** hasta que haya código y pruebas. La versión del paquete no cambia. Las decisiones de producto son `D-A05-nn` (aprobadas por el usuario); las elecciones técnicas derivadas son `E-A05-nn` y **no** son decisiones de producto ([A05-decisions](decisions/A05-decisions.md)). Alcance y aceptación: [especificación de A05](features/A05-trabajos-archivos-y-ciclo-de-vida.md).

Relacionado: [arquitectura](architecture.md) · [API pública](public-api.md) · [persistencia y migraciones](persistence-and-migrations.md) · [seguridad y aislamiento](security-and-isolation.md) · [instalación y operación](installation-and-operations.md) · [perfiles (A02)](profiles-and-catalog.md) · [inferencia y análisis (A03)](inference-and-analysis.md) · [procesamiento de documentos (A04)](document-processing.md).

Los contratos de A01–A03 **siguen implementados y sin cambios**; este documento solo agrega. En particular `analyze_text`, `POST /v1/scopes/{scope_id}/analyze-text`, `AnalysisResult`, los límites de A03, el cuerpo JSON de 64 KiB de las rutas existentes y la semántica pública de `update_scope`/`set_scope_status` (A01 D-30) se conservan íntegramente.

## 0. Resumen

A05 agrega al motor:

1. **Archivos**: carga por stream (Python y HTTP) a un almacenamiento **administrado** por el motor, o **referencias externas** opacas resueltas por adaptadores que configura el host; identidad por SHA-256 y tamaño; cuarentena hasta validar; descarga autorizada y verificada.
2. **Trabajos durables** con fases modeladas explícitamente: **validación documental**, **extracción/mapeo** y **análisis** (reutilizando A03). Dos operaciones: `document_analysis` (DOCX/PDF del subconjunto de §11.2) y `text_analysis` (texto, versión durable de A03).
3. **Ejecución recuperable** sin cola obligatoria: la base del motor es la autoridad sobre estado, intentos, posesión (*lease* con *fencing*) y resultado publicado.
4. **Ciclo de vida de datos**: retención aprobada (D-A05-01), expiración lógica, limpieza explícita por lotes con protección frente a escritores tardíos y purga de `admin_events` (resuelve Q-04 de A01).

No hay protección, redacción ni restauración (A06–A08), modelo real (A09) ni integración con CAWbase.

## 1. Principios y fronteras

| Principio | Regla |
|---|---|
| Autoridad | La base del motor decide estado, intentos, posesión y resultado publicado. Ningún mensaje de cola, archivo ni memoria de un worker prevalece sobre ella |
| Sin cola impuesta | No se exige SQS, Redis, Celery ni otro servicio. El host puede avisar a su orquestador **después del commit**; no hay atomicidad entre la base y una cola externa (§7.6) |
| Sin efectos ocultos | Importar o iniciar `ContentEngine` no crea workers, hilos, tareas, temporizadores ni limpiezas |
| Transacciones cortas | Ninguna transacción ni conexión del motor se retiene durante transferencias, validación, extracción, inferencia ni E/S de storage (A01 D-14) |
| Garantía de ejecución | **Intentos repetibles con efectos idempotentes** y una única publicación autoritativa. **No** se promete «exactamente una vez» de cómputo ni de llamadas al adaptador |
| Aislamiento | Todo recurso nuevo pertenece a un `scope_id`, que forma parte de toda clave primaria y foránea y de todo `WHERE` (A01 D-17) |
| Contenido sensible | Originales, textos de entrada, contexto, resultados y mapas nunca en logs, errores, eventos, diagnósticos ni `admin_events` |
| Independencia de A06–A09 | Ninguna garantía de A05 depende de features posteriores. El mock sustituye **solo** la inferencia |

## 2. Modelo conceptual

```text
scope ─1──1 scope_job_fence      (serialización frente a deshabilitar el scope, §9)
      ─1──1 scope_usage          (cuotas: reservado / almacenado / incierto, §12.3)
      ─1──* idempotency_record ──► job | stored_file
      ─1──* stored_file ─1──* file_validation
      ─1──* job ─1──* job_attempt ─1──* job_artifact
             └── input_file / context_file ──► stored_file
      ─1──* storage_tombstone    (intención mínima de borrado pendiente, §4.4)
engine_usage (fila única)        (cuotas de instalación)
```

Dimensiones **separadas** (no se codifican en un mismo campo): **estado de ejecución** del trabajo (§5.3); **cancelación solicitada** (`cancel_requested_at`, `cancel_reason`); **disponibilidad de datos**: archivo (`stored_file.state`, §3.6), resultados (`job.data_state ∈ {none, available, expired, deleted, lost}`) y metadatos (`metadata_expires_at`), regidos por la retención (§13).

## 3. Archivos y almacenamiento

### 3.1 Clases de objeto

| Clase | Propietario del objeto | Quién lo elimina | Retención |
|---|---|---|---|
| **Administrado**: carga Python/HTTP (`managed_upload`), texto/contexto de entrada (`text_input`), copia materializada (`external_materialized`) | motor | motor (§4.4, §13.5) | 7 días desde la recepción completa y durable (D-A05-01) |
| **Referencia externa** (`external_reference`): objeto que el host ya puede leer | host | **nunca** el motor | el motor deja de permitir su uso 7 días después del registro verificado (mismo plazo que originales) y purga su registro según §13.3 |
| **Artefactos** (`job_artifact`): manifiesto de resultado, mapa documental | motor | motor | 7 días desde la terminación del trabajo |
| **Temporales abandonados**: reservas no completadas, artefactos descartados, temporales del backend | motor | motor | 24 h desde que se determina el abandono |

El motor **nunca** borra, mueve ni modifica un original externo del host, ni promete borrado de copias de seguridad, réplicas, versiones históricas del proveedor de storage ni cachés del host.

### 3.2 Identidad del archivo

`FileIdentity { file_id, sha256, size_bytes, detected_format? }`. SHA-256 y tamaño los calcula el **motor sobre los bytes realmente recibidos o leídos**; un `expected_sha256`/`declared_size` del llamador solo sirve para **rechazar** discrepancias (`content_mismatch`) y para dimensionar la reserva (§12.3). El formato se detecta por contenido durante la validación (§10.1). `file_id` = `fil_` + 32 hex aleatorios; conocerlo no concede acceso.

### 3.3 Protocolo mínimo de storage administrado (`ManagedStorage`)

```python
# Propuesto / no disponible
class ManagedStorage(Protocol):
    @property
    def descriptor(self) -> StorageDescriptor: ...
        # backend_id estable; durabilidad declarada; transient_overhead_factor (≥ 1); inventario opcional
    async def put_new(self, key: str, chunks: AsyncIterable[bytes], *, max_bytes: int, not_after: datetime) -> PutReceipt: ...
        # crea un objeto PRIVADO e INMUTABLE; create-only (falla si la clave existe o está sellada);
        # aborta sin crear el objeto al superar max_bytes; NO hace visible el objeto después de not_after;
        # devuelve size y sha256; retorna solo cuando el objeto es durable según el descriptor
    async def open_read(self, key: str) -> AsyncIterator[bytes]: ...   # ObjectMissing si no existe o está sellada
    async def stat(self, key: str) -> ObjectInfo | None: ...          # None = ausencia confirmada; error = transitorio
    async def delete(self, key: str) -> None: ...                     # objeto registrado: idempotente (ausente = éxito)
    async def delete_and_seal(self, key: str) -> None: ...            # escritura de resultado desconocido (§4.4)
    # opcional (descriptor.inventory = True):
    async def list_keys(self, prefix: str, *, after: str | None, limit: int) -> Sequence[str]: ...
```

- El motor genera **todas** las claves (una por intención, nunca reutilizada); ni clientes ni hosts las eligen.
- **Garantías exigidas** al backend: (a) *create-only*; (b) `max_bytes` aplicado durante la escritura; (c) **no visibilidad tardía**: un objeto cuya escritura no se hizo visible antes de `not_after` no puede hacerse visible después; (d) `delete_and_seal` deja la clave **permanentemente** imposible de crear (un sello que no es un objeto legible), de modo que ninguna escritura en curso puede materializarla tras el sello; (e) `stat` distingue ausencia confirmada (`None`) de error transitorio (excepción). La garantía (c) se considera cumplida si el backend ofrece (d): el motor solo sella después de `not_after`. Un backend que no pueda ofrecer (a)–(e) es **incompatible** con el storage administrado de A05; no se promete limpieza universal.
- **Verificación**: el motor compara el recibo con lo que contó y hasheó él mismo; `stat` cierra el paso 3 de §4. Toda lectura posterior recalcula SHA-256 y tamaño (§3.7, §4.2).
- **Reconciliación sin inventario obligatorio**: la intención (fila con su clave) se registra **antes** de escribir; los huérfanos se descubren desde la base. `list_keys` es opcional (objetos sin fila, p. ej. tras restaurar una copia de la base) y nunca condición para borrar algo referenciado.
- No se exigen versiones de objeto, URLs firmadas, *multipart* ni listados.

### 3.4 Implementación de referencia: sistema de archivos privado

`FilesystemStorage(root)` (propuesta; E-A05-06):

- Raíz exclusiva del motor `0700`, archivos `0600`; no se sirve como estático. Claves `cce/<2 hex>/<32 hex>` (sin datos del cliente). Validación estricta de la forma de la clave; resolución relativa al descriptor de la raíz con `O_NOFOLLOW`/`openat` (o equivalente verificado en macOS): sin traversal ni symlinks.
- **Escritura**: temporal con nombre **determinista por clave** `tmp/<clave>.part`, abierto con `O_CREAT|O_EXCL`; escritura con contador (`max_bytes`); `fsync`; comprobación de `not_after` con el reloj del sistema; `link(tmp, final)` **por ruta** (falla con `EEXIST` si el nombre final existe; nunca `rename` sobre el nombre final, que reemplazaría); `unlink(tmp)`; `fsync` del directorio. No se usa `linkat` sobre descriptores (eludiría el sello del temporal).
- **Sello** (`delete_and_seal`): para el nombre final y para el temporal, en este orden: eliminar el temporal si existe y crear en su lugar un **directorio vacío** (sello); eliminar el final si existe y crear un directorio vacío en su lugar; repetir si una creación falla porque reapareció un archivo. Un directorio en esas rutas hace fallar `O_EXCL` y `link` del escritor tardío; `open_read`/`stat` tratan un sello como ausencia. Los sellos son permanentes (entradas vacías por cada escritura de resultado desconocido); su compactación es backlog (B-A05-13) y solo podría hacerse con un mecanismo que conserve la garantía (d).
- `transient_overhead_factor = 1` (el temporal se convierte en el final por `link`; nunca coexisten dos copias completas).
- **Alcance de despliegue**: un nodo, o varios nodos solo sobre un sistema de archivos compartido con las mismas semánticas (`O_EXCL`, `link` exclusivo, `fsync` efectivo, coherencia tras cierre). NFS u otros sin esas garantías no están soportados.

### 3.5 Referencias externas (`ExternalSourceResolver`, solo Python)

```python
# Propuesto / no disponible
class ExternalSourceResolver(Protocol):
    @property
    def descriptor(self) -> ResolverDescriptor: ...    # resolver_id, stable_versions: bool
    async def describe(self, ref: str) -> ExternalObjectInfo: ...   # version_id inmutable, size; sin bytes
    async def open_read(self, ref: str, version_id: str) -> AsyncIterator[bytes]: ...
```

- `ref` es **opaca** (≤ 1024 bytes); no se interpreta como URL ni ruta, no va a logs ni a HTTP. Conectividad y credenciales son configuración confiable del host.
- **Registro**: `describe` una sola vez; lectura completa con SHA-256 y límites (§12); se fija `{resolver_id, ref, version_id, size, sha256}`. La versión **queda fijada**: el motor nunca vuelve a llamar a `describe` para ese registro ni reselecciona otra versión.
- `stable_versions = True`: cada lectura pide **esa** versión y verifica tamaño y SHA-256 ⇒ discrepancia `source_changed`, ausencia `input_unavailable`. `False`: el registro **materializa** una copia administrada verificada (`external_materialized`).
- **HTTP no admite referencias externas** (§15).

### 3.6 Estados de un archivo

| Estado | Significado | Utilizable como entrada | Descargable |
|---|---|---|---|
| `reserved` | intención registrada; escritura en curso o de resultado desconocido | no | no |
| `stored` | bytes completos, verificados y durables (o registro externo verificado) | si no venció y su validación vigente no es `rejected` (§10.7) | §3.7 |
| `abandoned` | reserva no completada a tiempo | no | no |
| `deleted` | eliminación lógica pedida (`data:delete`) | no | no |
| `expired` | venció `expires_at` (marca de la limpieza; el acceso ya se denegaba desde el vencimiento) | no | no |
| `lost` | ausencia confirmada (`integrity_failure = missing`) o hash/tamaño distinto (`hash_mismatch`) tras haberse registrado | no | no |

- Transiciones: `reserved → stored | abandoned`; `stored → deleted | expired | lost`; los estados distintos de `reserved`/`stored` son **irreversibles**.
- **Ausencia o corrupción ≠ fallo transitorio**: `lost` solo se fija con `stat` = `None` confirmado o con una lectura completa cuyo hash/tamaño no coincide. Un error de backend (timeout, red, permisos transitorios) es `storage_unavailable` y **no** cambia el estado.
- **Recepción** (D-A05-01): `received_at` = hora de la autoridad temporal (§6.1) en la transacción que marca `stored` (paso 4 de §4, tras verificar bytes, hash y durabilidad). `expires_at = received_at + retention_s`, ambos persistidos.

### 3.7 Carga y descarga

- **Carga HTTP** (`POST …/files`): cuerpo binario por stream con límite propio (`max_file_bytes`), independiente del límite JSON de 64 KiB. Se cuentan los bytes **realmente recibidos**; un `Content-Length` mayor que el límite ⇒ 413 antes de leer; superar la reserva (§12.3) o el límite durante el stream ⇒ se corta, `limit_exceeded`/`content_mismatch`, la reserva pasa a abandonada (§4.1). Desconexión, plazo total o inactividad ⇒ `upload_aborted` y reserva abandonada.
- **Cargas repetidas e idempotencia**: §8.4.
- **Descarga verificada** (originales y resultados): antes de enviar encabezados, el motor lee el objeto completo verificando SHA-256 y tamaño hacia un **spool privado** (directorio `0700` del motor o memoria, acotado por `max_spool_bytes` por proceso con semáforo; el objeto mayor posible es ≤ 64 MiB, §12.2). Solo si la verificación pasa se envía `200` con `Content-Length` desde el spool, y el spool se elimina al terminar o fallar. Si falla: hash/tamaño ⇒ `lost` + `stored_object_unavailable`; error transitorio ⇒ `storage_unavailable` (503, reintentable); sin capacidad de spool ⇒ `storage_unavailable` (503, reintentable). Así, un cliente nunca recibe una respuesta completa de un objeto no verificado. **Límite declarado**: la verificación garantiza que el contenido enviado coincide con el hash registrado; no protege frente a corrupción posterior en el cliente o en tránsito (TLS es del despliegue).
- Python (`open_file_content`, `open_job_artifact`) usa el mismo spool verificado antes de entregar el primer fragmento.
- Respuesta: `Cache-Control: no-store`, `Content-Disposition: attachment; filename="<file_id>.<docx|pdf|txt>"` (o `<job_id>-result.json`/`<job_id>-source-map.json`), `X-Content-Type-Options: nosniff`, tipo detectado. Sin URLs firmadas, sin claves de storage, rutas, resolvers ni referencias. No se conserva el nombre original (E-A05-24).
- **Descargable** = original `stored`, no vencido, con validación `passed` vigente (§10.7); o resultado `available` no vencido. Nada en `reserved`, cuarentena (`pending`), `rejected`, `abandoned`, `deleted`, `expired`, `lost` o no publicado es descargable: 404 `resource_not_found` idéntico (sin revelar el motivo), salvo `job_data_expired` (410) para resultados de un trabajo todavía consultable y `stored_object_unavailable` para objetos perdidos detectados en la propia lectura.

## 4. Consistencia entre base y almacenamiento

No hay transacción distribuida. Todo objeto administrado sigue **cinco pasos**:

| Paso | Dónde | Qué |
|---|---|---|
| 1. Reservar | transacción corta | fila (`stored_file` o `job_artifact`) en `reserved` con clave generada, dueño (carga con `upload_generation`; intento con `attempt_no`/`lease_token`), `not_after` y reserva de cuota (§12.3) |
| 2. Transferir | fuera de transacción | `put_new(key, …, max_bytes = reserva, not_after)` |
| 3. Verificar | fuera de transacción | tamaño y hash propios = recibo = esperado (si lo hay); `stat` confirma |
| 4. Registrar | transacción corta | `reserved → stored`/`written`, **condicionado al dueño vigente** (carga: misma generación y plazo no vencido; intento: posesión vigente, §6.3); fija `size`, `sha256`, `received_at`/`written_at`; convierte la reserva en bytes almacenados |
| 5. Publicar | transacción corta | solo artefactos: transición T3 con todas sus precondiciones (§6.5); una **única** referencia autoritativa al manifiesto y al mapa |

Claves **específicas de cada intención**; *create-only*; ningún worker sobrescribe la salida de otro. Nunca se anuncia éxito con referencias incompletas: el paso 5 exige artefactos `written` del mismo intento.

### 4.1 Tabla de fallos y recuperación

| Falla | Estado | Recuperación |
|---|---|---|
| Tras 1, sin objeto | `reserved` | al vencer el dueño (plazo de carga o posesión) la limpieza marca `abandoned`/`discarded` con `abandoned_at`; la reserva pasa a **bytes inciertos** (no se libera); tras 24 h se ejecuta `delete_and_seal` (resultado de escritura desconocido, §4.4) y se libera el cargo al confirmarse |
| Durante 2 (objeto parcial) | `reserved`; temporal del backend | igual; el sello cubre también el temporal |
| Tras 2–3, sin registro | objeto completo, fila `reserved` | igual: no se adopta el objeto «por existir» |
| Tras 4 de un artefacto, sin publicación | `written` de un intento sin posesión | al terminar/perder el intento pasa a `discarded` (bytes inciertos); tras 24 h `delete` normal (escritura completada) y confirmación |
| Publicación confirmada con respuesta perdida | trabajo `succeeded` con los artefactos del intento | reconciliación antes de repetir (§7.3) |
| Objeto perdido o corrupto después de registrarse | `stat` = `None` o hash distinto | archivo `lost` / resultado `data_state = lost`; `stored_object_unavailable`; no se reintenta ni regenera en silencio |
| Eliminación física fallida (error transitorio) | marca de purga vigente o vencida | la limpieza reintenta en lotes posteriores; el cargo incierto se mantiene; el acceso ya estaba denegado |
| Worker obsoleto escribe tras perder la posesión | puede completar `put_new` | el paso 4/5 falla por *fencing*; su fila queda `reserved` (o `written` de un intento sin posesión) ⇒ abandono/descarte y borrado con sello o normal según §4.4 |
| Escritura tardía tras la limpieza | ver contraejemplo de §4.4 | imposible: el sello impide materializar la clave |

### 4.2 Integridad

Toda lectura (validación, extracción, descarga, resultados) verifica hash y tamaño. Un objeto corrupto o ausente nunca se presenta como válido ni como vacío.

### 4.3 Coordinación de limpieza, posesión y descargas

- La limpieza actúa **solo** sobre filas en estados de datos terminales e irreversibles (`abandoned`, `discarded`, `deleted`, `expired`, `lost`) con `delete_after` vencido; un objeto `lost` se registró (escritura completada) y se elimina con `delete` normal por si quedan bytes corruptos. Nunca borra algo porque una consulta momentánea no encuentre su publicación.
- `delete_after`: abandono/descarte ⇒ `abandoned_at + 24 h`; eliminación lógica o expiración de objetos registrados ⇒ transición + `purge_grace_s` (≥ `download_timeout_s` + margen), para que una descarga **iniciada antes** termine (las nuevas se deniegan desde la transición). Una descarga que supere su plazo se corta; como la entrega es desde el spool verificado (§3.7), cortar el origen no altera lo ya verificado.
- **Fencing de la limpieza**: cada fila candidata se reclama con `UPDATE … SET purge_token = :t, purge_claim_expires_at = ahora + purge_claim_ttl WHERE <estado terminal> AND delete_after <= ahora AND (purge_token IS NULL OR purge_claim_expires_at <= ahora)`. Tras la operación de storage, el resultado se registra con `UPDATE … WHERE purge_token = :t AND purge_claim_expires_at > ahora`: si no afecta filas, el limpiador **perdió la posesión** y no registra nada ni toca contadores; como el estado de datos es irreversible y `delete`/`delete_and_seal` son idempotentes, lo que haya hecho en el storage es inocuo y el nuevo dueño lo repetirá.

### 4.4 Escritores tardíos y sellos

Contraejemplo que el protocolo debe cubrir: (1) un worker inicia `put_new` y queda suspendido; (2) pierde el lease, pasa el abandono y el plazo de limpieza; (3) la limpieza borra, encuentra ausencia y marca eliminado; (4) el `put_new` termina y crea el objeto; (5) la intención ya no se revisa: huérfano permanente.

Protocolo:

1. Toda escritura lleva `not_after` = fin de la posesión que la autoriza: carga ⇒ `upload_deadline_at`; artefacto ⇒ `attempt_deadline_at` (§6.3). Un timeout de la corrutina **no** prueba que el backend no pueda completar: solo cuenta la garantía (c)/(d) de §3.3.
2. Filas con **escritura de resultado desconocido** (`reserved` abandonadas, sin registro): la limpieza espera `delete_after` (≥ `not_after`) y ejecuta `delete_and_seal`. Tras el sello ninguna escritura puede materializar esa clave; recién entonces se registra `physically_deleted_at` y se libera el cargo incierto.
3. Filas con **escritura completada** (`stored`/`written` antes de su terminación lógica): `delete` y confirmación por `stat` = `None`. El escritor ya terminó; no hay escritura en curso para esa clave.
4. **Intención mínima**: si los metadatos de la fila (archivo, artefacto, trabajo) deben purgarse (§13.3) antes de confirmar la eliminación física, la fila se reduce a un `storage_tombstone` `{scope_id, tombstone_id, storage_backend_id, storage_key, write_state (completed|unknown), charged_bytes, delete_after, purge_token…}` **sin contenido, hash, referencia externa ni datos del cliente**. El tombstone vive independientemente del plazo de metadatos del trabajo y se elimina **solo** cuando el borrado (o sello) quedó confirmado. Nunca se borra la última intención mientras pueda materializarse una escritura.
5. Si el backend falla de forma persistente, el tombstone permanece y su cargo sigue contando (§12.3): el problema queda visible en el resumen de limpieza, no se oculta.

## 5. Trabajos

### 5.1 Operaciones

| Operación | Entrada | Fases (`phase`) | Resultado publicado |
|---|---|---|---|
| `document_analysis` | archivo DOCX/PDF del scope + selección de perfil/versión + capacidades + contexto opcional (objeto `text_input`) | `validation` → `extraction` → `analysis` → `publication` | `DocumentAnalysisResult` (manifiesto) + `SourceMap` (artefacto separado) |
| `text_analysis` | texto (+ contexto) como en `analyze_text`, guardados como objetos `text_input` | `analysis` → `publication` | `TextAnalysisJobResult` (envuelve un `AnalysisResult` de A03 sin cambios) |

- La **validación documental** es una fase durable de `document_analysis` (no una API genérica); su resultado se registra por configuración (§10.7). Si existe ya una validación `passed` con la identidad fijada, la fase se omite; si existe `rejected`, el envío se rechaza (§8.3).
- No hay operaciones de protección, redacción ni restauración. `text_analysis` no reemplaza `analyze_text`.

### 5.2 Ciclo

```text
submit ──commit──► queued ──claim──► running ──publish──► succeeded
                     ▲  │              │  │ └─ no reintentable / agotado / plazo ─► failed
       available_at  │  │              │  └─ reintentable / lease vencido ─► retry_wait ─┐
                     └──┼──────────────┼─────────────────────────────────────────────────┘
                        └─ cancel/scope ┴─ cancelación observada / scope ─► cancelled
```

### 5.3 Matriz de transiciones

Autoridad temporal §6.1; orden de bloqueos §6.2. Notación: **dueño** = transición del worker condicionada a `attempt_no`, `lease_token`, `lease_expires_at > ahora` y `attempt_deadline_at > ahora`; **recuperación** = transición sobre un `running` con `lease_expires_at ≤ ahora` (o `attempt_deadline_at ≤ ahora`), hecha por reclamación, limpieza, `cancel_job` o `recover_expired_leases`, nunca por el dueño. «Fence ok» = §9.2. «Entrada ok» = §6.5 (archivo principal y contexto).

| # | De → a | Actor | Precondiciones | Efectos | Repetición |
|---|---|---|---|---|---|
| T1 | ∅ → `queued` | API (`submit_*`) | permiso; idempotencia (§8.3); scope activo y fence ok; entrada ok; `input.expires_at ≥ deadline_at` y `context.expires_at ≥ deadline_at`; validación fijada no `rejected`; cuotas | `accepted_at`, `available_at = accepted_at`, `deadline_at`, snapshot, `fence_generation`; `active_jobs += 1` (scope e instalación) | §8.3 |
| T2 | `queued`/`retry_wait` → `running` | worker (`claim`) | `available_at ≤ ahora < deadline_at`; fence ok; entrada ok; sin cancelación; `requirements_key` soportado; `running_jobs < max_running_jobs_per_scope` | `attempt_no += 1`, `lease_token` nuevo, `attempt_started_at = ahora`, `attempt_deadline_at = min(ahora + max_attempt_duration_s, deadline_at)`, `lease_expires_at = min(ahora + lease_ttl_s, attempt_deadline_at)`, `started_at` (si nulo); fila `job_attempt`; `running_jobs += 1` | una sola reclamación gana (§6.4) |
| T3 | `running` → `succeeded` | dueño (`publish`) | §6.5 completo | `terminated_at`, artefactos → `published`, `published_attempt_no`, `result_artifact_id`, `source_map_artifact_id`, `data_state = available`, expiraciones; intento `succeeded`; contadores (§12.3) | reconciliación §7.3 |
| T4 | `queued`/`retry_wait` → `cancelled` | `cancel_job` (`user_requested`) / deshabilitación o reconciliación del fence (`scope_disabled`) | — | `cancel_requested_at`, `cancel_reason`, `terminated_at`, expiraciones; `active_jobs -= 1` | estado actual |
| T5 | `running` (lease vigente) → `running` + marca | `cancel_job` | `lease_expires_at > ahora` | `cancel_requested_at`, `cancel_reason = user_requested` | idempotente |
| T5b | `running` (lease vencido) → `cancelled` | `cancel_job` (recuperación) | `lease_expires_at ≤ ahora` | como T6 | — |
| T6 | `running` → `cancelled` | dueño (al observar la marca en heartbeat/publicación) / recuperación con marca / deshabilitación o reconciliación del fence | marca de cancelación o fence roto | `terminated_at`; intento `cancelled`; artefactos no publicados → `discarded`; `running_jobs -= 1`, `active_jobs -= 1` | — |
| T7 | `running` → `retry_wait` | dueño (`report_retryable`) o recuperación (`lease_lost`) | error reintentable (§7.1); `attempt_no < max_attempts`; `ahora + backoff < deadline_at`; sin cancelación | `available_at`, `last_error_code`; intento `retryable_error`/`lease_lost`; artefactos → `discarded`; `running_jobs -= 1` | — |
| T8 | `running` → `failed` | dueño / recuperación | no reintentable, intentos agotados o plazo | `terminated_at`, `failure_code`; intento cerrado; artefactos → `discarded`; `running_jobs -= 1`, `active_jobs -= 1` | — |
| T9 | `queued`/`retry_wait` → `failed` | reclamación / limpieza | `deadline_at ≤ ahora` (`deadline_exceeded`, o `executor_unavailable` si `attempt_no = 0` y ningún worker compatible lo reclamó) o entrada no disponible (`input_unavailable`/`source_changed`) | `terminated_at`, `failure_code`; `active_jobs -= 1` | — |
| T10 | no terminal → `cancelled` (`scope_disabled`) | deshabilitación o reconciliación del fence | `fence_generation < disable_generation` | desde `running` como T6; desde los demás como T4 | reactivar no revierte |

- **Toda transición terminal** fija `terminated_at`, `result_expires_at` (solo T3) y `metadata_expires_at`, cierra el intento vigente (`ended_at`, `outcome`), pasa a `discarded` los artefactos no publicados de cualquier intento, y ajusta contadores (§12.3) en la **misma** transacción.
- Terminales: `succeeded`, `failed`, `cancelled`; sin salida. Cancelar un terminal devuelve su estado (`cancel_outcome = already_terminal`); un éxito confirmado nunca se reescribe.
- **Mutaciones del dueño** (heartbeat, fase, registro de artefacto, validación, reintento, fallo, publicación) exigen las cuatro condiciones de posesión; **recuperación** exige el vencimiento y no usa el `lease_token`. Tras una recuperación, cualquier mutación del dueño anterior falla (`lease_lost`).

### 5.4 Cancelación

- La carrera con la finalización se arbitra por la fila del trabajo bajo el orden de §6.2: la publicación exige `cancel_requested_at IS NULL`; T5 fija la marca solo con `state = 'running'`. Gana quien confirma primero. Si gana la cancelación, ninguna salida del intento se publica; si ganó el éxito, cancelar no lo reescribe.
- El motor no promete detener cómputo remoto ya iniciado; el worker deja de esperar, termina el proceso de extracción (§10.6) y no publica.
- Cancelación solicitada (`user_requested`, `scope_disabled`) ≠ caída del ejecutor (`lease_lost`, que lleva a T7/T8 o, con marca, a T5b/T6).

## 6. Tiempo, bloqueos, reclamación y publicación

### 6.1 Autoridad temporal

Toda hora que decide estado se toma **de la base** en la misma sentencia/transacción que la compara: PostgreSQL `clock_timestamp()`; SQLite `strftime('%Y-%m-%d %H:%M:%f','now')` (reloj del único nodo). Los relojes de los workers solo planifican heartbeats; `not_after` (§4.4) se pasa al backend como hora absoluta y el backend lo compara con su propio reloj, por lo que se deja un margen (`not_after` = vencimiento de la posesión − `clock_skew_margin_s`, 30 s). Los timestamps de A01–A03 no cambian.

### 6.2 Orden común de bloqueos

Toda transacción que tome más de un bloqueo los adquiere en este orden **ascendente** (puede omitir niveles, nunca volver a uno anterior); dentro de un nivel, por clave ascendente:

| Rango | Recurso | PostgreSQL | SQLite |
|---|---|---|---|
| 1 | `scopes[scope]` | `FOR SHARE` (lecturas que deciden: submit, claim, publicación, cancelación, eliminación) o `FOR NO KEY UPDATE` (cambio de estado o metadatos del scope, reconciliación del fence) | la transacción de escritura inmediata (A01) serializa a todos los escritores; el orden sigue valiendo como orden de lectura/escritura |
| 2 | `scope_job_fences[scope]` | `FOR UPDATE` (escritura del fence) o lectura | ídem |
| 3 | `scope_usage[scope]` | `FOR UPDATE` | ídem |
| 4 | `engine_usage` | `FOR UPDATE` | ídem |
| 5 | `idempotency_records` | inserción `ON CONFLICT DO NOTHING` / lectura | ídem |
| 6 | `stored_files` (por `file_id`) | `FOR SHARE` (uso como entrada) / `FOR UPDATE` (cambio de estado) | ídem |
| 7 | `file_validations` | inserción `ON CONFLICT DO NOTHING` | ídem |
| 8 | `jobs` (por `job_id`) | `FOR UPDATE` (o `FOR UPDATE SKIP LOCKED` en reclamación) | ídem |
| 9 | `job_attempts`, `job_artifacts` (por id) | `FOR UPDATE` | ídem |
| 10 | `storage_tombstones` | `FOR UPDATE` | ídem |

- **Transacciones hoja**: un `UPDATE` condicional de una sola fila sin otros bloqueos previos (heartbeat, cambio de fase, marca de purga) se permite en cualquier nivel: no espera nada tras tomarla, no forma ciclos.
- Uso por operación:

| Operación | Secuencia |
|---|---|
| `submit_*` | 1 share → 2 (lectura; si requiere escritura del fence, se aborta y se repite con 1 exclusivo, §9.2) → 3 → 4 → 5 → 6 share (entrada y contexto) → 7 (lectura) → 8 (inserción) |
| Carga/registro: paso 1 y 4 | 1 share → 3 → 4 → 5 → 6 |
| `claim` | lectura previa **no autoritativa** de candidatos (sin bloqueos); por cada candidato, una transacción: 1 share → 2 → 3 → 6 share → 8 `FOR UPDATE SKIP LOCKED` → revalidación → T2 |
| Publicación | 1 share → 2 → 3 → 4 → 6 share (entrada y contexto) → 7 (lectura de la validación fijada) → 8 → 9 |
| Validación (resultado) | 6 share → 7 inserción → 8 (condición de dueño) |
| Deshabilitación / reconciliación del fence / cambios A05 del scope | 1 `FOR NO KEY UPDATE` → 2 → 3 → 4 → 8 (`UPDATE` de los no terminales del scope) → 9 |
| `cancel_job` | 1 share → 3 → 4 → 8 → 9 |
| `delete_file` | 1 share → 3 → 4 → 6 `FOR UPDATE` |
| `delete_job_data` | 1 share → 3 → 4 → 8 → 9 |
| Limpieza (transiciones de trabajos) | como `cancel_job`; (marcas de purga) hoja; (registro de borrado) 3 → 4 → 6/9/10 |
| Recalcular cuotas | 3 → 4 (lecturas de 6/8/9/10 del scope sin bloquear) |

- **Lotes de reclamación**: una transacción por candidato (o por scope, con candidatos en orden ascendente de `job_id`); nunca se retienen bloqueos de trabajos de un scope mientras se piden los de otro, ni bloqueos de trabajos antes que su scope. `SKIP LOCKED` solo en el nivel 8, tras los niveles superiores: saltar no espera, así que no hay ciclo.
- **Sin garantía universal de ausencia de interbloqueos**: el orden evita ciclos entre operaciones del motor, pero `HostTransaction` (bloqueos retenidos por el host) o SQL ajeno pueden provocarlos. Se conserva la traducción de A02: `40P01`/`40001` ⇒ `database_unavailable` reintentable; con `HostTransaction` se revierte el savepoint y la transacción del host sigue utilizable; el motor no reintenta por su cuenta salvo la repetición acotada de §9.2 dentro de transacciones propias.

### 6.3 Posesión y plazos del intento

- Persistidos por intento (en `jobs` y `job_attempts`): `attempt_started_at`, `attempt_deadline_at = min(attempt_started_at + max_attempt_duration_s, deadline_at)`, `lease_expires_at`.
- **Lease inicial** (T2) = `min(ahora + lease_ttl_s, attempt_deadline_at)`. **Heartbeat** (hoja): `UPDATE jobs SET lease_expires_at = min(ahora + lease_ttl_s, attempt_deadline_at), heartbeat_at = ahora, phase = :fase WHERE … AND state = 'running' AND attempt_no = :n AND lease_token = :t AND lease_expires_at > ahora AND attempt_deadline_at > ahora`; devuelve `cancel_requested` y si el fence cambió (el worker se detiene).
- Cero filas ⇒ `lease_lost`: el worker deja de trabajar y no publica. Una posesión vencida **no se revive**, aunque nadie haya reclamado el trabajo.
- El lease nunca supera `attempt_deadline_at` ni prolonga retenciones (§13.2).

### 6.4 Reclamación

- PostgreSQL: lectura previa de candidatos `SELECT job_id, scope_id FROM jobs WHERE state IN ('queued','retry_wait') AND available_at <= clock_timestamp() AND requirements_key = ANY(:keys) ORDER BY available_at, accepted_at LIMIT :n` (sin bloqueo); luego, por candidato, la secuencia de §6.2 con `FOR UPDATE SKIP LOCKED` y la revalidación completa de T2 (si el trabajo ya no es elegible, se omite). Antes de seleccionar, la reclamación procesa un lote acotado de leases vencidos (recuperación, §6.7).
- SQLite: la misma lógica dentro de transacciones de escritura inmediata; el único escritor serializa a los reclamadores (uso previsto: un nodo).
- Devuelve `ClaimedJob { job_id, scope_id, attempt_no, lease_token, lease_expires_at, attempt_deadline_at, phase, snapshot }` (solo superficie local).

### 6.5 Publicación: precondiciones completas

Una transacción con la secuencia de §6.2 que comprueba, con `ahora` = hora de la base en esa transacción:

1. **Posesión**: `state = 'running'`, `attempt_no`, `lease_token`, `lease_expires_at > ahora`, `attempt_deadline_at > ahora`, `deadline_at > ahora` (expresamente, sin depender de que haya ocurrido un heartbeat).
2. **Cancelación**: `cancel_requested_at IS NULL`.
3. **Scope**: `status = 'active'`, fence sin divergencia y `fence_generation = disable_generation` (§9.2).
4. **Entrada principal y contexto** (bloqueados `FOR SHARE`, lo que serializa con `delete_file`, con la marca de expiración y con `lost`): `state = 'stored'`, `expires_at > ahora`, `sha256`/`size` iguales a los fijados en el snapshot; para `document_analysis`, la validación fijada existe y es `passed`.
5. **Artefactos**: `written`, del mismo `attempt_no`, con tamaño y hash registrados.

Si gana `delete_file` (o la entrada vence o se pierde) antes de este punto, no hay publicación: el trabajo pasa a `failed` (`input_unavailable`). Si gana la publicación, eliminar después el original **no** borra retroactivamente el resultado (que sigue su propia retención). Cualquier otra precondición fallida ⇒ T6/T8/`lease_lost` y ninguna publicación.

### 6.6 Avisos al host y trabajos sin aviso

- `submit_*` devuelve `JobAccepted` tras el commit propio. Con `HostTransaction` (§19) el host avisa **tras su commit**.
- Avisos perdidos o duplicados: `list_runnable_jobs(limit, older_than_s)` para re-avisar; una reclamación duplicada no encuentra el trabajo elegible.

### 6.7 Recuperación de leases vencidos

`running` con `lease_expires_at ≤ ahora` o `attempt_deadline_at ≤ ahora` se recupera (T5b/T6/T7/T8) por la reclamación, la limpieza, `cancel_job` o `recover_expired_leases()`, con el orden de §6.2. No depende de que el worker caído vuelva.

## 7. Reintentos y recuperación

### 7.1 Catálogo cerrado de errores de intento

| Código | Reintentable | Origen |
|---|---|---|
| `inference_unavailable`, `inference_timeout` | sí | A03 |
| `database_unavailable` | sí | A01 (incluye `40P01`/`40001`) |
| `storage_unavailable` | sí | storage o resolver con fallo transitorio |
| `lease_lost` | sí (consume intento) | muerte del proceso, pérdida de conexión, partición |
| `extraction_process_failed` | sí (consume intento) | el proceso aislado terminó por causa no atribuible a un límite, o el escáner opcional falló |
| `file_rejected` (+ motivo §10.4), `unsupported_format` | no | validación determinista |
| `limit_exceeded`, `unit_too_large`, `no_extractable_text` | no | límites deterministas y documento sin texto |
| `inference_output_invalid`, `inference_fixture_not_found`, `capabilities_not_configured` | no | A03 |
| `input_unavailable`, `source_changed`, `stored_object_corrupt` | no | entrada o contexto eliminados o vencidos (`input_unavailable`), referencia externa cambiada (`source_changed`), objeto administrado `lost` por ausencia o hash distinto (`stored_object_corrupt`) |
| `configuration_unavailable` | no | el worker no puede reconstruir la configuración fijada (§8.6) |
| `internal_error` | no | defecto; se registra solo la clase |

Terminales derivados: `retries_exhausted` (conserva `last_error_code`), `deadline_exceeded`, `executor_unavailable`.

### 7.2 Política

`max_attempts` = 5; `deadline_at = accepted_at + max_job_duration_s` (24 h); backoff `d = min(30 s · 2^(n−1), 15 min)` con demora uniforme en `[d/2, d]`; si `ahora + demora ≥ deadline_at` ⇒ `failed` (`deadline_exceeded`). Los reintentos no extienden la retención de la entrada ni cambian la configuración fijada. Intentos repetibles con efectos idempotentes; no exactamente una vez.

### 7.3 Resultado de commit desconocido

El worker **reconcilia antes de repetir**: relee el trabajo.

| Observa | Acción |
|---|---|
| `succeeded` con `published_attempt_no` = su intento y sus artefactos | éxito |
| `running` con su posesión vigente | repite la misma publicación (condicional, idempotente) |
| Otro estado o posesión ajena | no publica; sus artefactos ya pasaron (o pasarán por recuperación) a `discarded` |
| Base no disponible | reintenta la lectura dentro de su lease; si vence, `lease_lost` |

La misma regla aplica a cargas (§8.4) y a `submit_*` con respuesta perdida (§8.3).

## 8. Idempotencia y fijación de ejecución

### 8.1 Clave

- `idempotency_key`: 1–128 caracteres `[A-Za-z0-9._:-]`, primero alfanumérico; obligatoria en `submit_document_analysis`, `submit_text_analysis`, `upload_file` y `register_external_file` (HTTP: `Idempotency-Key`).
- Ámbito (`scope_id`, `operation`), `operation ∈ {document_analysis, text_analysis, file_upload, external_file}`; se guarda `key_hash` = SHA-256 de la clave.
- Autorización y pertenencia antes de consultar; una clave de otro scope no se ve.

### 8.2 Huella ≠ snapshot

- **Huella** (`request_fingerprint`): SHA-256 del JSON canónico de la solicitud lógica:
  - `document_analysis`: `{operation, file_id, profile_id|null, version_number|null, capabilities (ordenadas), context: {"present": false} | {"present": true, "sha256": SHA-256 UTF-8 del contexto exacto}}`.
  - `text_analysis`: `{operation, text_sha256, profile_id|null, version_number|null, capabilities, context: ídem}`.
  - Ausencia de contexto y contexto presente son distintos; cualquier cambio de entrada lógica que pueda cambiar la ejecución cambia la huella.
- **Snapshot efectivo** (§8.5): lo fijado. Se guarda aparte.
- Misma clave y huella ⇒ mismo trabajo; huella distinta ⇒ `idempotency_conflict` (409), sin crear nada.

### 8.3 Envío de trabajos (`submit_*`)

1. Autorización y validación de entrada; cálculo de la huella (no requiere escribir nada).
2. **Búsqueda de la clave** (lectura): si existe ⇒ huella igual: `JobAccepted` del trabajo existente (`replayed = true`), sin crear objetos; distinta: `idempotency_conflict`. Esto ocurre **antes** de reseleccionar perfil o versión y antes de materializar texto o contexto.
3. Si no existe y hay texto/contexto: se guardan como objetos `text_input` con el protocolo de §4 (pasos 1–4), **sin vincular** (`link_deadline_at = registro + submit_link_timeout_s`, 10 min).
4. **Transacción de aceptación** (orden de §6.2): `INSERT … ON CONFLICT DO NOTHING` del registro de idempotencia con el `job_id` pregenerado. Si se insertó: selección y fijación (§8.5), cuotas, inserción del trabajo y vínculo de los objetos de entrada al trabajo. Si no se insertó (otra solicitud concurrente ganó): se lee el registro existente **en la misma transacción** (sin error ni transacción abortada), se responde como en el paso 2 y los objetos de entrada de la solicitud perdedora se marcan `abandoned` en esa transacción (su cargo pasa a incierto y se eliminan por limpieza).
5. **Respuesta perdida**: tras el paso 3 y antes de 4 ⇒ no hay trabajo; un reintento con la misma clave repite desde el paso 3 con objetos nuevos, y los anteriores, no vinculados al vencer `link_deadline_at`, se abandonan. Después del paso 4 ⇒ el reintento obtiene el trabajo en el paso 2.
6. **Dos primeras solicitudes concurrentes**: PostgreSQL — la segunda `INSERT … ON CONFLICT DO NOTHING` espera a la primera y, al confirmarse, no inserta; SQLite — el único escritor las serializa. En ambos casos rige el paso 4.
7. **`HostTransaction`**: solo `submit_document_analysis` sin contexto (no escribe en storage). El `ON CONFLICT DO NOTHING` evita errores de unicidad: el savepoint no queda abortado y la lectura posterior es válida; si el conflicto proviene de una transacción no confirmada que después revierte, la inserción procede tras la espera. Cualquier otro error revierte solo el savepoint (A01 D-23).

### 8.4 Cargas y registros externos

**Cargas** (`file_upload`): el registro de idempotencia guarda `upload_generation` y el `file_id` vigente.

| Situación con la misma clave | Resultado |
|---|---|
| Archivo vigente `stored` | se consume el stream **solo para hashear** (sin escribir en storage ni reservar cuota, con el mismo límite de bytes); `{sha256,size}` igual ⇒ mismo archivo (`replayed`); distinto ⇒ `idempotency_conflict` |
| Archivo vigente `reserved` con `upload_deadline_at > ahora` | `upload_in_progress` (409, reintentable) |
| Archivo vigente `reserved` vencido o `abandoned` | en una transacción: el anterior pasa a `abandoned` (cargo incierto), `upload_generation += 1`, nueva reserva con `file_id` nuevo; el paso 4 de la carga nueva exige `upload_generation` y `file_id` vigentes, de modo que **un escritor antiguo no puede finalizar la reserva nueva** (ni la suya, ya abandonada) |
| Archivo vigente `deleted`/`expired`/`lost` | se devuelve con su estado; no se crea otro mientras exista el registro |

**Registros externos** (`external_file`): huella `{resolver_id, SHA-256(ref)}`. Repetición con registro `stored` ⇒ devuelve el mismo registro **sin** volver a llamar a `describe` (no se reselecciona la versión). Para registrar otra versión del mismo objeto se usa otra clave. Un registro en curso o abandonado sigue la tabla de cargas (generación incluida).

### 8.5 Fijación (snapshot de ejecución)

Inmutable para todos los intentos:

| Elemento | Contenido |
|---|---|
| Entrada | `file_id`, `sha256`, `size_bytes`, origen, `version_id` externa; contexto: `file_id`, `sha256`, `size_bytes` (o ausencia) |
| Validación | identidad de validación fijada (§10.7) para `document_analysis` |
| Perfil | `profile_id`, `version_id`, `version_number`, `content_hash`, `selection` |
| Capacidades | pedidas y ejecutables según la versión |
| Contratos | `analysis_contract_version`, `inference_contract_version`, `job_contract_version = 1`, `document_structures = "document-structures/1"`, `result_contract_version = 1` |
| Extracción | `extractor { id, version, config_sha256 }`, bibliotecas `{name, version}`, perfil de aislamiento exigido |
| Inferencia | procedencia del adaptador (`kind`, `adapter_id`, `adapter_version`, `configuration_id`, `model`), `timeout_s`; mock: `configuration_id` `fxs-…` |
| Límites y política | límites que afectan el resultado; `max_attempts`, `deadline_at` |
| Retención | `retention_results_s`, `retention_metadata_s` vigentes al aceptar |

`execution_snapshot_hash` y `requirements_key` (SHA-256 de `{extractor, configuration_id del adaptador, isolation_profile}`) se persisten. Sin texto, contexto ni definiciones. El camino durable lee la fila inmutable de la versión fijada y verifica `version_id` y `content_hash`; **no** vuelve a la selección pública (que rechaza perfiles desactivados).

### 8.6 Reproducibilidad

Ningún reintento cambia de extractor, adaptador, versión ni límites. Un worker solo reclama trabajos cuyo `requirements_key` satisface; si tras reclamar no puede reconstruir exactamente la configuración ⇒ `configuration_unavailable`. Sin worker compatible antes de `deadline_at` ⇒ `executor_unavailable`. Reproducibilidad = identidad y trazabilidad; no determinismo de un modelo ni repetición con entradas vencidas.

### 8.7 Tras la retención

Durante los 30 días de metadatos de un trabajo, repetir su clave devuelve el trabajo original aunque el resultado haya expirado (sin re-ejecutar). Tras purgar metadatos e idempotencia **no** hay deduplicación histórica: puede crearse otro trabajo. Los registros de cargas y registros externos siguen §13.3.

## 9. Cambios administrativos después de aceptar (D-A05-02)

### 9.1 Reglas

| Cambio | Efecto sobre trabajos aceptados |
|---|---|
| Desactivar/eliminar el perfil, publicar otra versión, cambiar el predeterminado | ninguno: siguen con la versión fijada |
| Deshabilitar el scope | impide reclamar y publicar; los no terminales se cancelan (`scope_disabled`); un resultado publicado antes no se cancela |
| Rehabilitar el scope | no resucita cancelados |
| Revocar una API key / deshabilitar su cliente | impide accesos posteriores con esa clave; no cancela trabajos |

### 9.2 Fence del scope

**Estado**: `scope_job_fences (scope_id, observed_scope_revision O, disable_generation G)`; cada trabajo guarda `fence_generation` = G al aceptarse. **Hecho verificado en el código** (`operations/scopes.py`, `0.1.0`–`0.3.0`): todo cambio **efectivo** de estado o metadatos del scope incrementa `scopes.revision` en el mismo `UPDATE`; las operaciones sin cambio no escriben. Comprobar solo `status` no basta (deshabilitar y rehabilitar deja `active` en ambos extremos).

**Invariantes** (E-A05-10):

- **I1** — Ningún trabajo se reclama ni se publica si `scopes.revision ≠ O` o `fence_generation ≠ G` (comprobado con el scope bloqueado al menos `FOR SHARE`).
- **I2** — **Reconciliar antes de avanzar**: toda operación A05 que vaya a escribir `O` adquiere el scope `FOR NO KEY UPDATE` (SQLite: transacción de escritura) y, **antes** de cualquier otra escritura, si `scopes.revision ≠ O` ejecuta la reconciliación: `G += 1`, T10 sobre todos los no terminales del scope con `fence_generation < G`, `O = scopes.revision`. Recién después aplica su propia mutación. Nunca se sobrescribe `O` sin haber invalidado primero el cambio desconocido que evidenciaba la divergencia.
- **I3** — Mutaciones A05 del scope (en el mismo bloqueo, tras I2): cambio efectivo de metadatos ⇒ `O = nueva revisión`; `set_scope_status` efectivo a `disabled` ⇒ `O = nueva revisión`, `G += 1`, T10 de no terminales; a `active` ⇒ `O = nueva revisión` (G sin cambio); sin cambio efectivo ⇒ no escribe `scopes` ni avanza `O` (I2 puede haber reconciliado igualmente). La semántica pública de A01 (control optimista estricto D-30, errores, eventos) no cambia: la reconciliación no modifica `scopes.revision` ni produce `revision_conflict`.
- **I4** — **Idempotencia de la reconciliación**: dos reconciliadores concurrentes se serializan por el bloqueo del scope; el segundo relee `O = scopes.revision` y no hace nada. Una divergencia ya reconciliada no vuelve a incrementar `G`.
- **I5** — **Inicialización perezosa**: la fila del fence se crea solo con el scope bloqueado `FOR NO KEY UPDATE`, con `O = scopes.revision` y `G = 1 + max(fence_generation)` de los trabajos existentes del scope (o `G = 0` si no hay ninguno), y aplica T10 a los no terminales existentes. Por construcción (todo trabajo se inserta con su fence ya presente) no debería haber ninguno; si los hay (datos restaurados, SQL ajeno), se tratan como de generación desconocida y se cancelan, nunca como nuevos. La fila nunca se elimina.
- **I6** — Lectores con bloqueo compartido (submit, claim, publicación) que detectan divergencia o ausencia de la fila **no deciden**: abortan su transacción, ejecutan la reconciliación (I2/I5) en una transacción propia y repiten una vez; si persiste, `database_unavailable` reintentable.

**Contraejemplo cubierto**: (1) trabajo aceptado con G y revisión R; (2) un runtime anterior deshabilita y rehabilita ⇒ R+2, G sin cambio; (3) un runtime A05 edita metadatos ⇒ por I2, antes de su escritura ve `R+2 ≠ R`, hace `G+1` y cancela el trabajo, y solo entonces escribe R+3 y `O = R+3`; (4) la publicación ve `fence_generation = G ≠ G+1` ⇒ no publica (I1). Sin la edición del paso 3, la publicación ve `R+2 ≠ O` ⇒ I6 reconcilia y cancela.

**Coste** (elección técnica propuesta, no decisión del usuario): mientras convivan runtimes anteriores, cualquier cambio efectivo que hagan sobre un scope —incluida una edición de metadatos— se trata como posible deshabilitación y cancela los trabajos no terminales del scope. No se usa `admin_events` para reconstruir deshabilitaciones.

Límite: SQL arbitrario que modifique el scope sin incrementar `revision` queda fuera de la garantía (A01 §2.1).

### 9.3 Lecturas sobre scopes deshabilitados

Se conserva el contrato heredado: lecturas permitidas (trabajos, resultados publicados, descarga de originales validados) sujetas a permisos y expiración. Escrituras (cargas, registros, `submit_*`) ⇒ `scope_disabled`. Cancelar y eliminar datos siguen permitidos.

## 10. Seguridad del procesamiento documental

### 10.1 Validación (fase `validation`)

Sobre bytes recibidos y verificados, sin confiar en extensión ni MIME:

- **Tipo real**: firma y estructura (`%PDF-`; ZIP/OPC con `[Content_Types].xml` y la parte principal de WordprocessingML). Contenedor OLE (cifrado), macros, otros formatos ⇒ `unsupported_format` o el rechazo correspondiente.
- **Límites antes de abrir** (§10.2) y durante la lectura, sobre bytes recibidos y **producidos** al descomprimir.
- **ZIP/OPC**: entradas, tamaños declarados y producidos, relación de compresión, nombres de parte (sin `..`, absolutos, duplicados por normalización, controles), tipos de contenido coherentes, relaciones internas existentes.
- **Relaciones externas**: la relación de **hipervínculo** (`…/relationships/hyperlink`) con `TargetMode="External"` se admite: se inventaría, su destino es `N` y **nunca** se resuelve ni se accede a la red (A04 DX-07). Cualquier **otra** relación externa (plantilla adjunta, imágenes vinculadas, marcos, `altChunk`, `INCLUDETEXT`/`INCLUDEPICTURE`/`LINK`, etc.) ⇒ `file_rejected: external_reference`.
- **XML**: cualquier `<!DOCTYPE` ⇒ rechazo; sin entidades externas ni red; profundidad y nodos acotados.
- **PDF**: xref, objetos, longitudes declaradas y producidas, filtros permitidos, Form XObjects, actualizaciones incrementales, reparación del parser.
- **Rechazos heredados y del alcance de §11.2**: cifrado (también con contraseña de usuario vacía), firmas, revisiones, incrustados/adjuntos/OLE/macros/ActiveX, texto oculto, cuadros de texto, datos enlazados, formularios, capas, acciones ejecutables, `/ActualText`/`/Alt`, anotaciones no autorizadas, fuentes sin mapeo fiable, Type0/CID/Type3, `Tc/Tw/Tz/Ts/TL`, estructura desconocida, **reparación del parser**, y las variantes no probadas que §11.2 declara `R`.
- Sin ejecutar macros, scripts ni acciones; **sin saneamiento** silencioso.

### 10.2 Límites aplicados antes de abrir

Los límites se entregan al parser antes de abrir (lectores ZIP con contador y aborto; XML sin DTD y con límites; PDF con límites de objetos/flujos y sin reparación). Una comprobación posterior a la apertura no es el único control. El proceso aislado añade límites de memoria, CPU y tiempo.

### 10.3 Antivirus opcional

`FileScanner` local opcional del host: solo puede añadir `file_rejected: scanner_rejected`; no sustituye la validación ni declara «libre de malware». Nunca servicios externos. Un escáner configurado que falla ⇒ `extraction_process_failed` (reintentable); nunca se omite en silencio.

### 10.4 Motivos cerrados (`file_rejected`)

`unsupported_format`, `type_mismatch`, `encrypted`, `signed`, `tracked_changes`, `embedded_content`, `macros`, `external_reference`, `hidden_text`, `unsupported_structure`, `parser_repair`, `font_mapping_insufficient`, `unsupported_text_state`, `incremental_update`, `forms_or_layers`, `executable_action`, `alternate_text_duplication`, `decompression_limit`, `structure_limit`, `scanned_without_text`, `scanner_rejected`. Sin contenido, nombres de parte ni valores.

### 10.5 Aislamiento del proceso de extracción

- Proceso separado con entorno **vacío** (sin credenciales de base, storage ni resolver), Python `-I`, directorio descartable, solo la entrada (pipe o archivo de solo lectura) y salida acotada (`max_extraction_output_bytes`). El padre valida la salida del hijo como no confiable.
- **Un subprocess no equivale a sandbox.** Perfiles:

| Perfil | Controles | Uso |
|---|---|---|
| `linux_container_reference` | contenedor OCI: `--network none`, raíz de solo lectura, `tmpfs` acotado, usuario no privilegiado, `--cap-drop ALL`, `no-new-privileges`, `--pids-limit`, `--memory` = `--memory-swap`, `--cpus`, terminación por plazo; solo entrada/salida montadas | referencia verificable en Linux; no impone Docker a quien aporte equivalente |
| `host_provided` | `IsolationRunner` del host con controles equivalentes | producción con otra tecnología |
| `unconfined_development` | proceso separado + `rlimit` + plazo; sin aislamiento de red ni filesystem | solo desarrollo (p. ej. macOS) y entradas confiables |

- **Canario** al iniciar el worker y periódicamente: dentro del aislamiento intenta red, escritura fuera del `tmpfs`, lectura de variables sensibles, procesos por encima del límite y exceso de memoria; cualquier éxito ⇒ perfil no disponible (`isolation_unavailable`).
- `untrusted_input = True` (predeterminado) exige `linux_container_reference` o `host_provided` verificados; **sin fallback silencioso**. `unconfined_development` requiere configuración explícita y queda en el snapshot y en la procedencia del resultado. macOS: solo separación de proceso, entorno limpio, `rlimit` (con sus limitaciones) y plazo; no es sandbox.

### 10.6 Terminación

Al vencer el plazo, perder el lease o recibir cancelación, el worker termina el proceso (grupo/contenedor) y descarta su salida. Límite propio ⇒ `limit_exceeded`; causa externa ⇒ `extraction_process_failed`.

### 10.7 Validaciones por configuración y disponibilidad

- **Identidad de validación** = SHA-256 de `{file_sha256, validator_id, validator_version, validator_config_sha256}`. La configuración vigente del validador de la instalación define la **identidad vigente** para cada archivo.
- `file_validations (scope_id, file_id, validation_identity)` es **solo de inserción** (`ON CONFLICT DO NOTHING`): el primer resultado registrado para una identidad es **autoritativo**; nunca «gana la última escritura». Una segunda ejecución con la misma identidad que produjera otro resultado (no debería: la validación es determinista) no reemplaza el primero y se registra como evento operativo `validation_divergence` sin contenido.
- **Solo un dueño** escribe validaciones: la inserción ocurre en la transacción del worker con posesión vigente (condición de dueño sobre el trabajo cuya validación fijada coincide con esa identidad). Una escritura de un worker obsoleto falla por *fencing* y no se registra.
- Las validaciones son **afirmaciones relativas a una configuración**: un `rejected` de una identidad no invalida un `passed` de otra (y viceversa); ambas coexisten y se consultan por identidad.
- **Admisión** (`submit_document_analysis`): el trabajo fija la identidad vigente; si para ella existe `rejected` ⇒ `file_not_usable` (motivo `file_rejected`); `passed` ⇒ se omite la fase; ausente ⇒ la fase se ejecuta.
- **Descarga de originales**: permitida si el archivo es `stored`, no vencido, y existe `passed` para la **identidad vigente** al momento de la descarga. Cambiar la configuración del validador hace que los archivos validados con otra configuración vuelvan a no ser descargables hasta revalidarse en un trabajo (comportamiento conservador explícito; `StoredFile` informa `download_validation: passed|rejected|pending` para la identidad vigente).
- `StoredFile` ya no tiene un `validation_state` global: expone las validaciones por identidad (resumen sin contenido) y la vigente.

## 11. Extracción, mapeo y análisis en A05

### 11.1 Incorporación al paquete

Extra **`documents`**, importación perezosa (gate D-33): inventario, extracción, mapeo, validación de identidad y cobertura, orquestación con A03. Implementación mantenible con pruebas propias; `experiments/a04/` no se importa. Sin transformación ni restitución. Bibliotecas candidatas (E-A05-14): `lxml` endurecido (DOCX) y `pikepdf`/QPDF (MPL-2.0, D-A04-01); versiones y licencias transitivas se fijan al implementar.

### 11.2 Matriz de soporte A05 (solo extracción y localización)

El alcance se fija **ahora** de forma conservadora: lo que A04 demostró se admite; lo que A04 dejó pendiente se **rechaza** (`R`) o se declara `N`, sin criterios condicionados al resultado de pruebas futuras. Ampliaciones: backlog B-A05-08. La transformación (A07/A08) no se infiere de esta matriz; D-A04-04 sigue en el inicio de A08.

**DOCX** (unidad = párrafo, incluidos los de celdas, encabezados, pies y notas; orden de A04 §5):

| Disposición | Elementos |
|---|---|
| `S` | párrafos y runs del cuerpo (DX-01); tablas (DX-02) con anidación **hasta profundidad 2** (DX-03); celdas combinadas `gridSpan`/`vMerge` coherentes (DX-04); encabezados/pies default/first/even de un documento con **una sola sección** (DX-05); notas al pie y al final sin separadores personalizados (DX-06); texto visible de hipervínculos (DX-07); resultados de campo (DX-08, extraídos; no transformables); controles de contenido de texto simple (DX-14) |
| `N` (declarado no cubierto) | destinos de hipervínculo (DX-07), instrucciones de campo (DX-09), imágenes (DX-19), texto alternativo (DX-20), ecuaciones (DX-23), cadenas libres fuera de la lista de permitidas en partes `P` (§2.2 de A04), y los `E` no analizados (§11.5) |
| `P` (inspeccionado) | `styles`, `numbering`, `settings`, `fontTable`, `theme`, `webSettings` con la lista de permitidas de A04 §2.2 |
| `R` | revisiones de cualquier tipo, texto oculto, cuadros de texto, `customXml`, `glossary`, gráficos, SmartArt, OLE/paquetes/ActiveX/macros, relaciones externas distintas de hipervínculos, `altChunk`, campos con referencia externa, cifrado, firmas, `docVars`, `dataBinding`, **anidación de tablas > 2**, **más de una sección**, rejilla inconsistente, separadores de notas con texto, partes huérfanas, fuentes incrustadas inválidas, estructura desconocida |

**PDF** (unidad = página; E-A04-25):

| Disposición | Elementos |
|---|---|
| `S` | texto con fuentes simples (Type1 con `/Widths`, TrueType simple incrustada) y `ToUnicode`/codificación estándar fiable; operadores `Tj`, `TJ`, `'`, `"` con `BT/ET`, `Tf`, `Tm`, `Td`, `TD`, `T*`; `/Rotate` **0, 90 o 180** y matrices de texto con rotación 0°, 90° o 180°; varias columnas en orden de flujo; Form XObjects (un segmento por uso); ligaduras (`n:1`) |
| `N` | imágenes en páginas con texto, trazados, anotaciones `Link` y acciones `URI`/`GoTo` internas (inventariadas, nunca resueltas), estructura etiquetada sin `/Alt` ni `/ActualText`, y los `E` no analizados (§11.5) |
| `R` | `/Rotate 270`, matrices de texto con otros ángulos u oblicuas, **object streams**, **`/Contents` en arreglo**, linearización, actualizaciones incrementales, xref reparada, Type0/CID/Type3, fuentes sin mapeo, `Tc/Tw/Tz/Ts/TL`, texto invisible, formularios, capas, adjuntos, acciones ejecutables o externas, `FreeText` y otras anotaciones con apariencia, `/ActualText`/`/Alt`, cifrado, firmas, páginas escaneadas, estructura desconocida |

Texto **exacto**, offsets en **puntos de código**, separadores sintetizados (`kind = synthesized`, no localizables), relaciones con dirección (`1:1`, `n:1`, `1:n`), reutilización de objetos y ambigüedad declarada (detección válida, no localizable). El mapa queda ligado a `file_sha256` + extractor + `config_sha256` (A04 §3).

### 11.3 Reutilización de A03 sin copiar su lógica

Por unidad no vacía, el worker usa las **mismas** funciones internas de A03 (preparación desde la versión fijada, `build_request`, invocación con timeout y protocolo cerrado de errores, `validate_output`, `build_result`) extraídas a una función común sin permisos. `analyze_text` conserva firma, comportamiento, errores y ruta. Límites por unidad = los de A03; `AnalysisResult` sin campos nuevos.

### 11.4 Resultado documental

```jsonc
// Propuesto / no disponible — ejemplo ilustrativo: DOCX con 2 comentarios, solo detección pedida
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
    "extraction": { "textual_subset_complete": true, "document_coverage_complete": false,
                    "declared_not_covered": [ { "element_class": "comments", "count": 2, "reason": "not_analyzed" } ],
                    "authorized_removals_performed": [] },
    "inference": { "units_total": 2, "units_analyzed": 1, "units_empty": 1,
                   "per_capability": {
                     "classification": { "status": "not_requested" },
                     "detection": { "status": "completed", "complete": true, "units_completed": 1, "units_inconclusive": 0 } } }
  },
  "status": "completed"
}
```

- **Sin agregación**: cada unidad conserva su `AnalysisResult`; no se promedian puntuaciones ni hay ganadora global.
- **Unidad excesiva** ⇒ `unit_too_large`. **Unidad vacía** (longitud 0 o solo separadores sintetizados) ⇒ `status = "empty"`, sin llamar a A03; solo espacios **no** es vacía. **Documento sin unidades analizables** ⇒ `no_extractable_text`.
- Salida válida inconclusa se conserva; salida malformada o fallo operativo en **cualquier** unidad ⇒ el intento falla; no se publica un parcial. Se publica solo con todas las unidades `analyzed` o `empty`.
- Sin porcentajes: conteos exactos y listas declaradas.

`TextAnalysisJobResult` = `{ result_contract_version, job_id, operation: "text_analysis", analysis: AnalysisResult }`.

### 11.5 Eliminación autorizada ≠ eliminación efectuada

A04 autoriza eliminar comentarios y metadatos descriptivos **durante una transformación**; **A05 no transforma ni elimina** nada del original. Los elementos `E` (DX-10, DX-15, DX-18, PX-10, PX-11) se inventarían, **no se analizan** y pasan a **contenido no cubierto** en A05 (`declared_not_covered`, `not_analyzed`): impiden `document_coverage_complete`, pero **no** convierten en incompleto el subconjunto `S` correctamente procesado (no afectan `textual_subset_complete`). `authorized_removals_performed` es siempre vacío.

### 11.6 Indicadores

| Indicador | Verdadero si y solo si | No significa |
|---|---|---|
| `textual_subset_complete` | A04 §2.1: todo `S` extraído con mapa consistente, sin `R`, `P` inspeccionados, y toda unidad no vacía analizada con `coverage.complete = true` en **cada capacidad ejecutada** (`completed` o `inconclusive` con cobertura completa no basta si alguna unidad tiene cobertura incompleta) | detección, ausencia de datos sensibles ni protección |
| `document_coverage_complete` | `textual_subset_complete` y ningún `N` (incluidos los `E` no analizados) ni porción desconocida | lo mismo |
| `per_capability.<cap>.status` | `not_requested` (no pedida; solo `status`); `not_configured` (pedida, la versión fijada no la configura; solo `status`); `completed` (todas las unidades analizadas `completed` en esa capacidad); `inconclusive` (alguna `inconclusive`) | — |
| `per_capability.<cap>.complete` | **solo** en `completed`/`inconclusive`: todas las unidades analizadas tienen `coverage.complete = true` en esa capacidad | nada sobre lo no extraído |
| `status` del documento | `completed` ⇔ toda capacidad ejecutada `completed`; `inconclusive` si alguna `inconclusive` | protección ni cobertura documental |

- Los estados son los de A03 por capacidad; no se lee `coverage.complete` de capacidades `not_requested`/`not_configured` (no lo tienen). Al menos una capacidad es ejecutable (si no, `capabilities_not_configured` en la aceptación) y hay al menos una unidad analizada (si no, `no_extractable_text`): no hay verdad por vacuidad.
- Casos: solo clasificación ⇒ `detection: {status: not_requested}`; solo detección ⇒ simétrico; ambas pedidas con una no configurada ⇒ esa `{status: not_configured}` y la otra con su estado; ambas ejecutadas ⇒ ambas con estado y `complete`.

## 12. Límites operativos (E-A05-17)

Elecciones técnicas iniciales, configurables (`Limits`/`JobsSetup`, nombres propuestos), **no mediciones**; a verificar con pruebas de frontera. Exceder un límite ⇒ rechazo explícito, nunca truncamiento.

### 12.1 Entrada y estructura

| Límite | Default | Unidad | Ámbito | Enforcement | Error |
|---|---|---|---|---|---|
| `max_file_bytes` | 25 MiB | bytes recibidos | archivo | contador del stream / `max_bytes` de `put_new` | `limit_exceeded` (HTTP 413) |
| `max_pdf_pages` | 100 | páginas | documento | árbol de páginas, antes de extraer | `structure_limit` |
| `max_zip_entries` | 1000 | entradas | DOCX | directorio central | `structure_limit` |
| `max_docx_parts_processed` | 300 | partes XML | DOCX | inventario | `structure_limit` |
| `max_decompressed_total_bytes` | 200 MiB | bytes producidos | archivo | lector con contador | `decompression_limit` |
| `max_decompressed_entry_bytes` | 50 MiB | bytes producidos | entrada / flujo | ídem | `decompression_limit` |
| `max_compression_ratio` | 100:1 | relación | entrada / flujo | declarado y producido | `decompression_limit` |
| `max_xml_depth` | 64 | niveles | parte | parser | `structure_limit` |
| `max_xml_nodes_total` | 2 000 000 | nodos | documento | parser | `structure_limit` |
| `max_table_nesting` | 2 | niveles | DOCX | inventario (alcance de §11.2) | `unsupported_structure` |
| `max_pdf_objects` | 200 000 | objetos | PDF | xref | `structure_limit` |
| `max_pdf_content_bytes_per_page` | 8 MiB | bytes decodificados | página | decodificación | `structure_limit` |
| `max_form_xobject_depth` | 8 | niveles | página | intérprete | `structure_limit` |
| `max_text_operators_per_page` | 100 000 | operadores | página | intérprete | `structure_limit` |

### 12.2 Extracción, resultado y tiempos

| Límite | Default | Ámbito | Error |
|---|---|---|---|
| Texto por unidad | A03: 8192 puntos de código / 24 576 bytes | unidad | `unit_too_large` |
| `max_units_per_document` | 5000 | documento | `limit_exceeded` |
| `max_document_text_chars` | 2 000 000 | documento | `limit_exceeded` |
| `max_document_findings` | 50 000 (además de 500 por unidad) | documento | `limit_exceeded` |
| `max_source_map_bytes` / `max_result_bytes` | 32 MiB / 32 MiB (JSON canónico) | artefacto | `limit_exceeded` |
| `max_extraction_output_bytes` | 64 MiB | salida del hijo | `limit_exceeded` |
| `extraction_timeout_s` / `extraction_cpu_s` / `extraction_memory_bytes` / `extraction_max_pids` | 120 / 120 / 1 GiB / 32 | proceso aislado | `limit_exceeded` |
| `unit_inference_concurrency` | 4 | intento | — |
| `max_attempt_duration_s` | 3600 | intento | recuperación |
| `max_job_duration_s` | 86 400 | trabajo | `deadline_exceeded` |
| `max_attempts` | 5 | trabajo | `retries_exhausted` |
| `lease_ttl_s` / `heartbeat_interval_s` | 60 / 20 (`≤ lease_ttl_s / 3`) | intento | `lease_lost` |
| `clock_skew_margin_s` | 30 | `not_after` | — |
| `upload_timeout_s` / `upload_idle_timeout_s` | 600 / 60 | carga | `upload_aborted` |
| `submit_link_timeout_s` | 600 | objetos de texto/contexto no vinculados | abandono |
| `download_timeout_s` | 600 | descarga | conexión cortada |
| `max_spool_bytes` | 256 MiB por proceso | descargas verificadas | `storage_unavailable` |
| `purge_grace_s` | `download_timeout_s` + 300 | eliminación física | — |
| `purge_claim_ttl_s` | 300 | marca de limpieza | — |

### 12.3 Cuotas y contadores

**Contadores por scope** (`scope_usage`) **e instalación** (`engine_usage`):

| Contador | Significado |
|---|---|
| `reserved_bytes` | capacidad prometida a escrituras en curso (paso 1 sin paso 4) |
| `stored_bytes` | objetos administrados registrados y no terminales (`stored`, `written`, `published`) |
| `uncertain_bytes` | bytes que **pueden existir** en el storage: reservas abandonadas o descartadas (por su reserva completa), objetos registrados lógicamente eliminados/vencidos/perdidos/descartados (por su tamaño) y tombstones, hasta que la eliminación o el sello se **confirmen** (§4.4) |
| `live_files` | archivos y registros externos no terminales (`reserved`, `stored`) |
| `active_jobs` / `running_jobs` | no terminales / `running` |

**Cargo de storage** = `reserved_bytes + stored_bytes + uncertain_bytes`; debe ser ≤ `max_storage_bytes_per_scope` (2 GiB) y el agregado ≤ `max_storage_bytes_total` (100 GiB). Los bytes cuentan **hasta la eliminación física confirmada**.

| Evento | Efecto |
|---|---|
| Reservar (paso 1) | `reserved += R × transient_overhead_factor`; R = `declared_size` (si ≤ `max_file_bytes`) o `max_file_bytes` para cargas; tamaño del texto para `text_input`; `max_result_bytes`/`max_source_map_bytes` para artefactos |
| Escritura que supera R | el writer aborta (`max_bytes = R`) ⇒ `content_mismatch` si R venía de `declared_size`, `limit_exceeded` si no; **no** se amplía la reserva en A05; nunca se confía en el tamaño declarado |
| Registrar (paso 4) | `reserved −= R·f`; `stored += size` real |
| Abandonar/descartar sin registro | `reserved −= R·f`; `uncertain += R·f` (no se libera porque falló el registro) |
| Eliminar/expirar/perder/descartar un objeto registrado | `stored −= size`; `uncertain += size` |
| Confirmar eliminación o sello (§4.4) | `uncertain −=` cargo de la fila |
| T1 / estados terminales | `active_jobs += 1` / `−= 1` (scope e instalación) |
| T2 / salir de `running` (T3, T5b, T6, T7, T8, T10 desde `running`) | `running_jobs += 1` / `−= 1` |
| Carga/registro: reservar / estado terminal | `live_files += 1` / `−= 1` |

- **Artefactos por intento**: al iniciar la fase de publicación, el intento crea **una vez** sus dos filas `reserved` (resultado y mapa) con reservas fijas; cada fila lleva su propia reserva; no hay una reserva global adicional del intento, por lo que el mismo presupuesto no se cuenta dos veces. Un intento nuevo reserva las suyas; las del intento descartado quedan en `uncertain` hasta confirmarse su borrado (con 5 intentos, el peor caso por trabajo es acotado y cuenta contra la cuota).
- **Temporales del backend** y copias simultáneas: cubiertos por `transient_overhead_factor` del descriptor (filesystem de referencia = 1). La materialización de referencias externas escribe un único objeto administrado (cargo como una carga). La publicación no copia objetos.
- **Referencias externas**: no consumen bytes administrados; sí `live_files`; su materialización consume bytes como una carga.
- Todas las actualizaciones de contadores ocurren en la transacción que cambia el estado, con `scope_usage` y `engine_usage` bloqueados en el orden de §6.2. Comprobación de cuota y reserva son atómicas.
- **Recálculo** (limpieza): por scope, en una transacción que bloquea `scope_usage` (y luego `engine_usage`), recalcula los contadores del scope **completo** desde sus filas (`stored_files`, `job_artifacts`, `storage_tombstones`, `jobs`) y reemplaza los valores; como todo cambio de esas filas exige el mismo bloqueo, ningún cambio concurrente se pierde. `engine_usage` se recalcula como suma de `scope_usage` bajo su bloqueo. Nunca se suma solo un lote como si fuera el total.

| Otras cuotas | Default | Error |
|---|---|---|
| `max_active_jobs_per_scope` / `max_active_jobs_total` | 100 / 10 000 | `quota_exceeded` (429) |
| `max_running_jobs_per_scope` | 4 | (no se reclama) |
| `max_live_files_per_scope` | 1000 | `quota_exceeded` |
| Concurrencia del ejecutor de referencia | 4 trabajos | — |
| `cleanup_batch_size` | 500 | — |
| `admin_events_purge_batch_size` | 1000 (máx. 10 000) | `validation_failed` |

## 13. Retención, expiración y purga (D-A05-01)

### 13.1 Plazos aprobados y persistencia

| Dato | Plazo | Desde | Persistido |
|---|---|---|---|
| Originales administrados (cargas, `text_input`, materializaciones) y registros externos (uso) | 7 días | `received_at` | `retention_s`, `expires_at` |
| Resultados y mapas | 7 días | `terminated_at` | `result_retention_s`, `result_expires_at` |
| Temporales abandonados | 24 h | `abandoned_at` | `abandoned_at`, `delete_after` |
| Metadatos mínimos de trabajos e idempotencia de trabajos | 30 días | `terminated_at` | `metadata_expires_at` (trabajo y registro de idempotencia) |
| `admin_events` | 90 días | `occurred_at` | política (§13.6) |

Configurables por el operador; sin conservación indefinida implícita (máximo configurable 3650 días) ni extensión silenciosa por reintentos, consultas, leases o trabajos nuevos. Los plazos efectivos se copian a cada recurso; cambiar defaults solo afecta recursos nuevos.

### 13.2 Admisión y expiración de la entrada

- La entrada no se prolonga por crear trabajos ni por reintentar.
- **Admisión**: `input.expires_at ≥ deadline_at` y, si hay contexto, `context.expires_at ≥ deadline_at`; si no ⇒ `file_expiring` (409).
- Si la entrada o el contexto vencen, se eliminan, se pierden o cambian antes de publicar ⇒ la reclamación o la publicación (§6.5) lo detectan ⇒ `failed` (`input_unavailable`/`source_changed`).

### 13.3 Metadatos de trabajos, archivos y registros

- **Trabajos** (30 días desde la terminación, aprobado): se conservan solo `job_id`, `scope_id`, `operation`, estado, fase final, timestamps, `cancel_reason`, `failure_code`/`last_error_code`, conteo y resultado de intentos, identidad de entrada y contexto (`file_id`, `sha256`, `size_bytes`), identidad fijada (perfil, versión, `content_hash`, extractor, procedencia, contratos, `execution_snapshot_hash`), `request_fingerprint`, `key_hash`, `data_state`. Nunca texto, contexto, fragmentos, resultados, mapas ni definiciones. Hashes e identificadores sujetos a `jobs:read`; no van a logs.
- **Archivos, validaciones, registros externos y su idempotencia** (sin trabajo asociado del que tomar `terminated_at`) — elección técnica E-A05-25: `file_metadata_expires_at = end_of_life_at + 30 días`, donde `end_of_life_at` = momento de la transición a un estado terminal del archivo (`abandoned`, `deleted`, `expired`, `lost`), registrado con la autoridad temporal. Al vencer se purgan la fila del archivo (incluida `external_ref`), sus `file_validations` y el registro de idempotencia de carga/registro. **Ni `external_ref` ni los registros de cargas fallidas se retienen indefinidamente.**
- **Intenciones mínimas**: si al purgar metadatos (de archivo, artefacto o trabajo) la eliminación física aún no está confirmada, solo sobrevive el `storage_tombstone` (§4.4), sin contenido, hash, referencia ni datos del cliente, hasta la confirmación; puede sobrevivir al plazo de metadatos del trabajo sin conservar nada de él.

### 13.4 Expiración lógica

Al vencer `expires_at`/`result_expires_at` el acceso se deniega de inmediato (comparación con la autoridad temporal en cada lectura), aunque la purga física esté pendiente: archivos ⇒ 404 `resource_not_found`; resultados de un trabajo consultable ⇒ `job_data_expired` (410). La transición persistida la hace la limpieza.

### 13.5 Limpieza explícita (`run_cleanup`)

Explícita, repetible, por lotes, recuperable; nunca automática al importar o iniciar. La invocan el host, el ejecutor de referencia (`cleanup_interval_s`) o la CLI propuesta `cawbase-content-engine cleanup`. Por lote, en transacciones cortas con el orden de §6.2:

1. recuperar leases vencidos y trabajos con `deadline_at` vencido;
2. marcar reservas y objetos `text_input` no vinculados vencidos como `abandoned`, y artefactos de intentos terminados como `discarded`;
3. marcar archivos y resultados vencidos (`expired`);
4. reclamar filas elegibles (fencing de §4.3), ejecutar `delete` (escritura completada) o `delete_and_seal` (resultado desconocido) fuera de transacción y registrar la confirmación;
5. purgar metadatos vencidos (trabajos e idempotencia a 30 días; archivos/validaciones/registros según §13.3), dejando tombstones donde la eliminación física no esté confirmada;
6. recalcular cuotas de los scopes tocados (§12.3).

Devuelve un resumen sin contenido. Nunca elimina originales externos ni filas de scopes, clientes, claves, grants, perfiles o versiones.

### 13.6 `admin_events`: retención y purga (resuelve Q-04 de A01)

- Política: 90 días desde `occurred_at`, aplicada a **todos** los eventos, incluidos los anteriores a A05. No hay columna de vencimiento por fila: la purga es explícita con corte; la primera ejecución puede eliminar eventos históricos de más de 90 días (previsualizar primero).
- Operación local de operador / Python privilegiado (`engine.maintenance().purge_admin_events(older_than=…, batch_size=…, dry_run=True)`; CLI propuesta `cawbase-content-engine purge-admin-events --older-than-days N --batch-size M [--execute]`). **Sin endpoint HTTP.**
- Corte y lote explícitos; corte `≤ ahora − admin_events_retention` (90 días configurables); previsualización por defecto; lotes con transacciones cortas, repetible y reanudable; resumen sin contenido (cantidad, rangos de `occurred_at` y `event_id`).
- La migración no purga. `admin_events` sigue append-only para operaciones ordinarias; esta purga es la única excepción. No se eliminan scopes, clientes, claves, perfiles ni versiones. Sin promesa de archivo histórico ni de cumplimiento normativo.
- El fence (§9.2) no depende de `admin_events`.

### 13.7 Correspondencias de A06

Tendrán autorización, retención y eliminación propias (A06). A05 no crea tablas, APIs, criptografía ni semántica de restauración. El `SourceMap` no es una tabla reversible de valores sensibles.

## 14. API Python (propuesta / no disponible)

```python
# Propuesto / no disponible — ejemplo ilustrativo
engine = ContentEngine(
    config,
    inference=InferenceSetup(adapter),
    storage=StorageSetup(managed=FilesystemStorage("/var/lib/cce/objects"),
                         external_resolvers={"host-assets": HostAssetsResolver(...)}),
    documents=DocumentSetup(isolation=LinuxContainerIsolation(...), untrusted_input=True),
    jobs=JobsSetup(),
)
```

### 14.1 `ScopedOperations` (nuevos métodos)

| Método | Permiso | Resultado | `HostTransaction` |
|---|---|---|---|
| `upload_file(scope_id, chunks, *, idempotency_key, expected_sha256=None, declared_size=None)` | `files:write` | `UpsertResult[StoredFile]` | rechazada |
| `register_external_file(scope_id, *, resolver_id, ref, idempotency_key)` | `files:write` | `UpsertResult[StoredFile]` | rechazada |
| `get_file` / `list_files(scope_id, *, state=None, limit=50, cursor=None)` | `files:read` | `StoredFile` / `Page[StoredFile]` | permitida |
| `open_file_content(scope_id, file_id)` | `files:read` | `AsyncIterator[bytes]` (verificado antes de entregar) | rechazada |
| `delete_file(scope_id, file_id)` | `data:delete` | `StoredFile` (`deleted`) | permitida |
| `submit_document_analysis(scope_id, *, file_id, idempotency_key, profile_id=None, version_number=None, capabilities=("classification","detection"), context=None)` | `analysis:execute` | `JobAccepted` | permitida solo sin `context` |
| `submit_text_analysis(scope_id, *, text, idempotency_key, profile_id=None, version_number=None, capabilities=…, context=None)` | `analysis:execute` | `JobAccepted` | rechazada |
| `get_job` / `list_jobs(scope_id, *, state=None, operation=None, limit=50, cursor=None)` | `jobs:read` | `JobInfo` / `Page[JobInfo]` | permitida |
| `cancel_job(scope_id, job_id)` | `jobs:cancel` | `JobInfo` (+ `cancel_outcome`) | permitida |
| `get_job_result(scope_id, job_id)` | `results:read` | `DocumentAnalysisResult` \| `TextAnalysisJobResult` | rechazada |
| `open_job_artifact(scope_id, job_id, kind)` | `results:read` | `AsyncIterator[bytes]` (verificado) | rechazada |
| `delete_job_data(scope_id, job_id)` | `data:delete` | `JobInfo` | permitida |

- `delete_job_data`: solo trabajos **terminales**; no terminal ⇒ `job_not_terminal` (409). `succeeded` con `available` ⇒ `deleted`; ya `deleted`/`expired`/`lost` o sin datos (`failed`/`cancelled`) ⇒ éxito sin cambio. No elimina entradas ni contexto (que siguen su retención o `delete_file`; pueden ser compartidas por otros trabajos). Como solo actúa sobre terminales, no compite con la publicación; frente a la expiración, ambas llevan a estados terminales irreversibles y la lectura comprueba `data_state` y `result_expires_at` en el instante de decisión.
- `get_job_result`/`open_job_artifact`: exigen `succeeded`, `data_state = available` y `result_expires_at > ahora` al decidir; la entrega sigue §3.7.
- Sin `InferenceSetup` / `StorageSetup` / `DocumentSetup` ⇒ `inference_not_configured` / `storage_not_configured` / `documents_not_configured`. `analyze_text` no cambia.

### 14.2 Superficies confiables locales (sin HTTP ni `ScopeContext`)

Código confiable del host/operador con acceso a la base (límite de A01 §2.1). `AdminContext` no da acceso a ellas ni a contenido.

```python
# Propuesto / no disponible
worker = engine.job_worker(worker_id="w-1")
claimed = await worker.claim(limit=1)
outcome = await worker.run_job(job_id)          # reclama ESE trabajo si es elegible y lo ejecuta completo
await worker.run_claimed(claimed[0])

executor = ReferenceExecutor(engine, concurrency=4, poll_interval_s=5, cleanup_interval_s=300)
await executor.start(); ...; await executor.aclose()      # CLI propuesta: `cawbase-content-engine worker`

maint = engine.maintenance()
await maint.list_runnable_jobs(limit=100, older_than_s=60)
await maint.recover_expired_leases(limit=100)
await maint.run_cleanup(batch_size=500)
await maint.purge_admin_events(older_than=cutoff, batch_size=1000, dry_run=True)
```

### 14.3 Tipos (ilustrativos)

```python
class StoredFile:  scope_id; file_id; origin; state; integrity_failure; rejection_reason_current
                   download_validation  # passed | rejected | pending para la identidad vigente
                   validations          # [{validation_identity, result, reason?, validated_at}]
                   size_bytes; sha256; detected_format; received_at; expires_at; created_at
                   # sin storage_key, ref externa, resolver ni nombre original
class JobAccepted: job: JobInfo; replayed: bool
class JobInfo:     scope_id; job_id; operation; state; phase; attempt_count; max_attempts
                   cancel_requested_at; cancel_reason; failure_code; last_error_code
                   accepted_at; started_at; terminated_at; deadline_at
                   input: {file_id, sha256, size_bytes}; context: {file_id, sha256, size_bytes} | None
                   profile: ProfileRef; data_state; result_expires_at; metadata_expires_at
class ClaimedJob:  job_id; scope_id; attempt_no; lease_token; lease_expires_at; attempt_deadline_at; phase
```

## 15. HTTP (propuesto / no disponible)

Mismas operaciones del núcleo (D-07), orden de A01 (límite → autenticación → contexto 404 → validación → operación), bajo `/v1/scopes/{scope_id}`. Rutas JSON: 64 KiB.

| Método y ruta | Permiso | Cuerpo / respuesta | Notas |
|---|---|---|---|
| `POST /files` | `files:write` | binario por stream → `201 StoredFile` / `200` + `Idempotent-Replay: true` | `Idempotency-Key`; `application/octet-stream`; `X-Content-SHA256` y `Content-Length` solo para rechazar discrepancias y dimensionar la reserva; 413; 409 `upload_in_progress` |
| `GET /files` · `GET /files/{file_id}` | `files:read` | `200` | |
| `GET /files/{file_id}/content` | `files:read` | `200` binario verificado (§3.7) | `no-store`, `attachment`, `nosniff` |
| `DELETE /files/{file_id}` | `data:delete` | `200 StoredFile` | repetible |
| `POST /document-analysis-jobs` | `analysis:execute` | `{file_id, profile_id?, version_number?, capabilities?, context?}` → `202` / `200` replay | `Idempotency-Key`; `Location` |
| `POST /text-analysis-jobs` | `analysis:execute` | `{text, profile_id?, version_number?, capabilities?, context?}` → `202` / `200` | ídem |
| `GET /jobs` · `GET /jobs/{job_id}` | `jobs:read` | `200` | `state`, `operation`, `limit`, `cursor` |
| `POST /jobs/{job_id}/cancel` | `jobs:cancel` | `200 JobInfo` | repetible |
| `GET /jobs/{job_id}/result` · `/source-map` | `results:read` | `200` JSON verificado | 409 `job_not_succeeded`; 410 `job_data_expired` |
| `DELETE /jobs/{job_id}/data` | `data:delete` | `200 JobInfo` | 409 `job_not_terminal` |

Sin rutas de referencias externas, reclamación, heartbeat, publicación, limpieza ni purga; ninguna acepta URLs, rutas locales, resolvers ni credenciales; sin URLs firmadas. `analyze-text` no cambia. Recurso de otro scope o inexistente ⇒ `404 resource_not_found` idéntico.

## 16. Permisos (elección técnica E-A05-12)

El catálogo pasaría de **11** a **17** (6 nuevos de scope). Sin jerarquías ni comodines; `AdminContext`/`admin:*` no acceden a archivos, trabajos ni resultados.

| Permiso | Habilita |
|---|---|
| `analysis:execute` (A03) | además de `analyze_text`: enviar trabajos con archivos del propio scope; no lee archivos, trabajos ni resultados |
| `files:write` | cargar y registrar referencias externas (Python) |
| `files:read` | metadatos y descarga de originales validados |
| `jobs:read` | estado y metadatos de trabajos |
| `jobs:cancel` | cancelar |
| `results:read` | resultados y mapas (sensibles) |
| `data:delete` | eliminar lógicamente archivos y datos de resultados |

El scope es la unidad de aislamiento (granularidad por archivo: B-A05-05). **Restricción de despliegue** heredada: no emitir claves con permisos nuevos hasta actualizar todos los runtimes; revocar no corrige un runtime anterior.

## 17. Errores nuevos (propuestos)

| Código | HTTP | Reintentable | Cuándo |
|---|---|---|---|
| `storage_not_configured` / `documents_not_configured` | 503 | no | falta `StorageSetup` / `DocumentSetup` o el extra |
| `storage_unavailable` | 503 | sí | fallo transitorio de storage/resolver, o sin capacidad de spool |
| `stored_object_unavailable` | 503 | no | objeto `lost` (ausente o corrupto) |
| `isolation_unavailable` | — (superficie local) | no | aislamiento exigido no verificable |
| `upload_in_progress` | 409 | sí | carga/registro con la misma clave aún vigente |
| `upload_aborted` | — (conexión) / 400 | sí (reiniciar) | desconexión, plazo o inactividad |
| `content_mismatch` | 422 | no | hash o tamaño distinto del declarado, o carga que supera `declared_size` |
| `file_not_usable` | 409 | no | archivo no `stored`, vencido, eliminado, perdido o con validación vigente `rejected` |
| `file_expiring` | 409 | no | entrada o contexto vencen antes de `deadline_at` |
| `quota_exceeded` | 429 | sí | §12.3 |
| `job_not_succeeded` | 409 | no | resultado de un trabajo no `succeeded` |
| `job_not_terminal` | 409 | no | `delete_job_data` sobre un no terminal |
| `job_data_expired` | 410 | no | resultado vencido, eliminado o perdido |
| `idempotency_conflict` (A01) | 409 | no | misma clave, solicitud distinta |
| `transaction_incompatible` (A01) | — | no | operación con E/S de storage en `HostTransaction` |

Los `failure_code` de trabajos (§7.1, §10.4) se informan en `JobInfo`. Se reutilizan `permission_denied`, `resource_not_accessible`, `resource_not_found`, `validation_failed`, `limit_exceeded`, `scope_disabled`, `default_profile_not_set`, `profile_not_selectable`, `profile_not_published`, `capabilities_not_configured`, `inference_not_configured`, `database_unavailable`, `internal_error`.

## 18. Persistencia y migración `0003` (propuesta)

Namespace fijo; `scope_id` en toda PK y FK; ninguna garantía depende solo de FK/`CHECK`.

| Tabla | Clave | Columnas principales | Índices / restricciones |
|---|---|---|---|
| `stored_files` | (`scope_id`, `file_id`) | `origin`, `state`, `integrity_failure`, `storage_backend_id`, `storage_key`, `write_state`, `resolver_id`, `external_ref`, `external_version`, `size_bytes`, `sha256`, `detected_format`, `reserved_bytes`, `upload_generation`, `upload_deadline_at`, `link_job_id`, `link_deadline_at`, `reserved_at`, `received_at`, `retention_s`, `expires_at`, `abandoned_at`, `deleted_at`, `end_of_life_at`, `file_metadata_expires_at`, `delete_after`, `purge_token`, `purge_claim_expires_at`, `physically_deleted_at`, `created_at` | UNIQUE `file_id`; UNIQUE (`storage_backend_id`, `storage_key`); (`state`, `delete_after`); (`expires_at`); (`file_metadata_expires_at`); `CHECK` de estados |
| `file_validations` | (`scope_id`, `file_id`, `validation_identity`) | `validator_id`, `validator_version`, `validator_config_sha256`, `result`, `reason`, `job_id`, `attempt_no`, `validated_at` | solo inserción; FK → `stored_files` |
| `jobs` | (`scope_id`, `job_id`) | `operation`, `state`, `phase`, `input_file_id`, `context_file_id`, `validation_identity`, perfil fijado, `capabilities`, `execution_snapshot`, `execution_snapshot_hash`, `requirements_key`, `request_fingerprint`, `fence_generation`, `attempt_no`, `max_attempts`, `lease_token`, `lease_owner`, `attempt_started_at`, `attempt_deadline_at`, `lease_expires_at`, `heartbeat_at`, `available_at`, `accepted_at`, `started_at`, `terminated_at`, `deadline_at`, `cancel_requested_at`, `cancel_reason`, `failure_code`, `last_error_code`, `published_attempt_no`, `result_artifact_id`, `source_map_artifact_id`, `data_state`, `result_retention_s`, `result_expires_at`, `metadata_retention_s`, `metadata_expires_at`, `updated_at` | UNIQUE `job_id`; (`state`, `requirements_key`, `available_at`); (`state`, `lease_expires_at`); (`scope_id`, `state`, `accepted_at`); (`metadata_expires_at`); `CHECK` terminal ⇔ `terminated_at` |
| `job_attempts` | (`scope_id`, `job_id`, `attempt_no`) | `worker_id`, `lease_token_hash`, `attempt_started_at`, `attempt_deadline_at`, `ended_at`, `outcome`, `error_code` | FK → `jobs` |
| `job_artifacts` | (`scope_id`, `artifact_id`) | `job_id`, `attempt_no`, `kind`, `state` (`reserved`/`written`/`published`/`discarded`/`expired`/`deleted`/`lost`), `storage_backend_id`, `storage_key`, `write_state`, `reserved_bytes`, `not_after`, `size_bytes`, `sha256`, `reserved_at`, `written_at`, `abandoned_at`, `delete_after`, `purge_token`, `purge_claim_expires_at`, `physically_deleted_at` | UNIQUE (`storage_backend_id`, `storage_key`); (`state`, `delete_after`); FK → `jobs` |
| `storage_tombstones` | (`scope_id`, `tombstone_id`) | `storage_backend_id`, `storage_key`, `write_state` (`completed`/`unknown`), `charged_bytes`, `delete_after`, `purge_token`, `purge_claim_expires_at`, `created_at` | UNIQUE (`storage_backend_id`, `storage_key`); sin contenido |
| `idempotency_records` | (`scope_id`, `operation`, `key_hash`) | `request_fingerprint`, `job_id` \| `file_id`, `upload_generation`, `created_at`, `expires_at` | (`expires_at`) |
| `scope_job_fences` | `scope_id` | `observed_scope_revision`, `disable_generation`, `updated_at` | FK → `scopes`; nunca se elimina |
| `scope_usage` | `scope_id` | `reserved_bytes`, `stored_bytes`, `uncertain_bytes`, `live_files`, `active_jobs`, `running_jobs`, `updated_at` | `CHECK ≥ 0` |
| `engine_usage` | `id = 1` | `reserved_bytes`, `stored_bytes`, `uncertain_bytes`, `active_jobs`, `running_jobs`, `updated_at` | fila única |

**Migración `0003_jobs_files`** (aditiva, solo hacia adelante): crea tablas e índices; no altera tablas existentes; no migra ni purga datos (los fences se crean perezosamente, §9.2 I5); `schema_level` 2 → **3**; mismo procedimiento de A01.

**Niveles y compatibilidad semántica** (E-A05-11, revisada tras las correcciones): el runtime A05 requiere `schema_level ≥ 3`; `min_runtime_level` **permanece en 1**. Justificación por invariantes, no por poder leer tablas: (a) un runtime `0.1.0`–`0.3.0` no conoce trabajos ni archivos y no puede aceptar, reclamar, publicar, validar ni tocar contadores; (b) todo cambio efectivo suyo sobre un scope incrementa `scopes.revision` (verificado en `operations/scopes.py`) y su `UPDATE` espera a los bloqueos `FOR SHARE` de A05 (PostgreSQL) o al único escritor (SQLite), de modo que I1, I2 e I6 de §9.2 detectan y reconcilian todo cambio antes de cualquier reclamación o publicación; (c) desactivar perfiles o revocar claves no afecta trabajos por diseño (D-A05-02); (d) no ejecuta limpieza ni purga. Si una revisión futura dependiera de que todos los runtimes apliquen reglas nuevas, deberá subir `min_runtime_level`, sabiendo que eso se comprueba en `start()`/readiness y no detiene procesos ya iniciados.

**Orden de despliegue**: migrar → actualizar todos los runtimes → emitir claves con permisos nuevos y habilitar workers/limpieza. **Permisos de base**: el runtime A05 necesita `DELETE` sobre sus tablas; la purga de `admin_events`, `DELETE` sobre esa tabla (recomendado solo para mantenimiento).

## 19. Transacciones por operación (`HostTransaction`)

| Operación | Con `HostTransaction` | Motivo |
|---|---|---|
| `upload_file`, `register_external_file`, `submit_text_analysis`, `submit_document_analysis` con `context`, `open_file_content`, `get_job_result`, `open_job_artifact` | rechazada antes de efectos (`transaction_incompatible`) | E/S de storage; un rollback del host dejaría objetos sin intención |
| `submit_document_analysis` sin `context`, `cancel_job`, `delete_file`, `delete_job_data`, `get_file`, `list_files`, `get_job`, `list_jobs` | permitida en un savepoint (A01 D-23), con el orden de §6.2 y `ON CONFLICT DO NOTHING` (§8.3) | solo base; el host avisa tras su commit |
| `job_worker`, `maintenance` | no aceptan transacción | propias y cortas |

Compartir engine no implica compartir transacción. Las filas bloqueadas permanecen bloqueadas hasta el commit del host.

## 20. Observabilidad

- Registradores propuestos: `cawbase_content_engine.jobs`, `.files`, `.storage`, `.maintenance`. Permitidos: ids (`job_id`, `file_id`, `artifact_id`, `scope_id`), operación, estado, fase, `attempt_no`, `worker_id`, códigos, conteos, tamaños, duraciones, `request_id`. Prohibidos: contenido, hashes de contenido, claves de storage, referencias externas, `lease_token`, `purge_token`, claves de idempotencia.
- `admin_events`: sin eventos por trabajo o archivo; solo `maintenance.admin_events_purged` (conteos), `scope.job_fence_reconciled` (sin contenido) y `file.validation_divergence`.
- Readiness no cambia.

## 21. Fuera de alcance

Protección, redacción, seudonimización, correspondencias, criptografía y restauración (A06–A08); transformación; OCR; modelo real, calidad, segmentación (A09); integración con CAWbase; AWS; URLs firmadas; cargas *multipart*/reanudables; ampliación de la reserva durante una carga; colas obligatorias; exactamente una vez; archivo histórico de eventos.
