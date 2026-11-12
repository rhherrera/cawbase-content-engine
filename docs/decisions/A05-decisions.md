# A05 — Registro de decisiones

> **Estado: A05 ESPECIFICADA / NO IMPLEMENTADA, pendiente de revisión del usuario.** No hay código, migración, permisos ni versión nuevos (`0.3.0` sin cambios). Las decisiones `D-A05-nn` fueron **aprobadas por el usuario** con el encargo de A05; las elecciones técnicas `E-A05-nn` se derivaron por criterio al redactar el contrato y **no** son decisiones de producto (revisables sin abrir otra ronda por detalles rutinarios).

Contratos que usan estas decisiones: [especificación de A05](../features/A05-trabajos-archivos-y-ciclo-de-vida.md) · [trabajos, archivos y ciclo de vida](../jobs-files-and-lifecycle.md) (contrato completo). Decisiones heredadas: [A01](A01-decisions.md), [A02](A02-decisions.md), [A03](A03-decisions.md), [A04](A04-decisions.md).

Categorías (no mezclarlas): **decisiones aprobadas** (§2) · **elecciones técnicas derivadas** (§3) · **diferencias con el playbook** (§4) · **backlog** (§5) · preguntas y tensiones (§6).

## 0. Historial de revisiones

| Rev. | Contenido |
|---|---|
| 1 | Primera redacción (solo documental): D-A05-01 y D-A05-02 aprobadas; E-A05-01 a E-A05-24; diferencias con el playbook; backlog. Resolución de Q-04 de A01 por referencia (sin reescribir el registro histórico de A01). |

## 1. Fuente, precedencia y herencia

- Referencia: `STAGE_3_PLAYBOOK.md`, revisión 6 (copia local ignorada por Git; **no se modifica ni se declara actualizada**). Las decisiones posteriores (A01–A04 y estas) prevalecen donde difieren (§4).
- A01, A02, A03 y A04 están **cerradas y aceptadas** por el usuario; A04 como investigación de viabilidad acotada (mergeada en `main` con el merge `58001e0`, PR #3). No se reabren ni se cambia ninguna de sus decisiones; D-A04-04 se conserva (comprobación representativa de PDF al inicio de A08; no bloquea A05).
- Heredado **sin cambios**: A01 D-06/D-07 (asincronía, núcleo único), D-08 (núcleo liviano y extras), D-11/D-33 (sin efectos al importar; gate de importación), D-13/D-14/D-23 (transacciones propias o `HostTransaction`, sin transacciones durante trabajo largo), D-15/D-16/D-24 (namespace, migraciones explícitas con un bloqueo y una transacción), D-17/D-27 (aislamiento por scope, delegación), D-18/D-25 (API keys); A02 D-A02-02 (versiones inmutables) y la selección de A02 §8; A03 D-A03-01 (análisis de texto en la misma llamada, **se conserva**), D-A03-02 (`analysis:execute`), D-A03-03 (cobertura y estados); A04 D-A04-01 a 04.

## 2. Decisiones aprobadas

### D-A05-01 — Retención predeterminada

| Dato | Plazo | Desde |
|---|---|---|
| Originales administrados por el motor | 7 días | su recepción completa y durable (`received_at`: hora de la autoridad temporal en la transacción que confirma bytes, hash y durabilidad) |
| Resultados y mapas documentales | 7 días | la terminación del trabajo (`terminated_at`) |
| Temporales abandonados | 24 horas | el momento en que se determina su abandono conforme al protocolo de posesión (`abandoned_at`) |
| Metadatos mínimos de trabajos e idempotencia | 30 días | la terminación del trabajo |
| `admin_events` | 90 días | `occurred_at`, incluidos los eventos anteriores a A05 |

- Configurables por el operador. Sin conservación indefinida implícita ni extensión silenciosa por reintentos.
- Los plazos efectivos se persisten por recurso; cambiar defaults no reinterpreta recursos existentes (para `admin_events` la purga es explícita con corte, [contrato §13.6](../jobs-files-and-lifecycle.md#136-admin_events-retención-y-purga-resuelve-q-04-de-a01)).
- La expiración lógica deniega el acceso aunque la purga física esté pendiente. No se promete borrado de backups, réplicas, versiones históricas del storage ni originales externos del host.

### D-A05-02 — Cambios administrativos después de aceptar un trabajo

- Desactivar o eliminar un perfil **no** invalida trabajos aceptados: siguen con la versión fijada.
- Deshabilitar el scope impide nuevas reclamaciones y la publicación de trabajos pendientes; esos trabajos se cancelan. Reactivarlo no los resucita.
- Revocar una API key impide accesos posteriores con esa clave, pero no cancela por sí sola trabajos aceptados.
- Las lecturas permitidas sobre scopes deshabilitados conservan el contrato heredado, sujetas a permisos y expiración.
- Un resultado publicado antes de deshabilitar el scope no se cancela retroactivamente.
- Deshabilitar y rehabilitar rápidamente un scope no permite publicar un trabajo que debía cancelarse (serialización: E-A05-10).

## 3. Elecciones técnicas derivadas

| ID | Elección | Justificación |
|---|---|---|
| E-A05-01 | La base del motor es la única autoridad sobre estado, intentos, posesión y publicación; sin cola obligatoria; avisos al host solo tras el commit, sin atomicidad base/cola | Playbook A05 («sin segundo sistema de workers»); permite SQS/colas del host sin acoplarlas |
| E-A05-02 | Seis estados de ejecución con la matriz T1–T10; cancelación solicitada y ciclo de vida de datos como dimensiones separadas | Encargo; evita estados combinados ambiguos |
| E-A05-03 | Lease finito (60 s) + heartbeat (20 s) + *fencing* (`attempt_no` monótono + `lease_token` aleatorio); posesión vencida no se revive | Un worker obsoleto no puede publicar aunque termine |
| E-A05-04 | Autoridad temporal = reloj de la base (`clock_timestamp()` en PostgreSQL; `strftime('now')` en SQLite) | Los relojes de workers pueden divergir; la comparación ocurre donde se decide |
| E-A05-05 | Protocolo de storage mínimo (`put_new` create-only y durable, `open_read`, `stat`, `delete` idempotente, `list_keys` opcional) y registro de intención en la base antes de escribir | No exige capacidades innecesarias a todos los proveedores; los huérfanos se descubren desde la base |
| E-A05-06 | Implementación de referencia en filesystem privado (`0700`/`0600`, claves generadas, `O_EXCL`, `fsync`, sin symlinks ni traversal); un nodo o FS compartido con semánticas equivalentes | Modo independiente sin infraestructura adicional; límites de despliegue explícitos |
| E-A05-07 | Referencias externas solo por Python mediante resolvers configurados por el host; verificación por versión inmutable + SHA-256 + tamaño; materialización si no hay estabilidad | No reenviar archivos que el worker ya puede leer; sin URLs arbitrarias |
| E-A05-08 | Protocolo de 5 pasos (reservar, transferir, verificar, registrar, publicar) con claves de artefacto por intento y una única referencia autoritativa | Sin transacción distribuida; tabla de fallos completa |
| E-A05-09 | Reintentos: catálogo cerrado; 5 intentos; plazo total 24 h; backoff 30 s×2 hasta 15 min con *jitter*; reconciliación antes de repetir una publicación de resultado incierto | Finitud, sin exactamente-una-vez |
| E-A05-10 | Fence de scope (`scope_job_fences`: `observed_scope_revision`, `disable_generation`) comprobado con el scope bloqueado contra cambios en aceptación, reclamación y publicación; reconciliación conservadora ante cambios de runtimes anteriores (detectados por `scopes.revision`) | Comprobar solo el estado actual no detecta disable+enable; no depende de `admin_events` (purgable) ni de que runtimes viejos cooperen |
| E-A05-11 | Migración aditiva `0003_jobs_files`: `schema_level` 3, `min_runtime_level` sigue en 1 con justificación semántica; orden migrar → actualizar runtimes → emitir permisos nuevos | Compatibilidad evaluada por garantías, no por poder leer tablas |
| E-A05-12 | Seis permisos de scope nuevos (`files:write`, `files:read`, `jobs:read`, `jobs:cancel`, `results:read`, `data:delete`; catálogo 17) y reutilización de `analysis:execute` para enviar trabajos | Separa ejecutar, cargar, leer originales, consultar, leer resultados, cancelar y eliminar sin un permiso universal |
| E-A05-13 | Idempotencia por (`scope_id`, operación, SHA-256 de la clave); huella de solicitud separada del snapshot efectivo; búsqueda de la clave antes de reseleccionar perfil/versión | Encargo; no almacenar claves del cliente en claro |
| E-A05-14 | Extra `documents` con importación perezosa; bibliotecas candidatas `lxml` (parser endurecido) y `pikepdf`/QPDF (MPL-2.0, D-A04-01); versiones y licencias transitivas se fijan al implementar | Coherente con A04 y con D-33 |
| E-A05-15 | Extracción en proceso separado con entorno vacío; perfiles de aislamiento `linux_container_reference` (verificable), `host_provided`, `unconfined_development` (solo explícito); canario de verificación; sin fallback silencioso | «Un subprocess no equivale a sandbox» |
| E-A05-16 | Unidad DOCX = párrafo, PDF = página (A04); unidad excesiva ⇒ `unit_too_large`; unidades vacías sin llamar a A03; documento sin texto ⇒ `no_extractable_text` | Sin truncamiento, segmentación anticipada ni éxito por vacuidad |
| E-A05-17 | Tabla de límites iniciales (25 MiB, 100 páginas, descompresión 200 MiB/50 MiB/100:1, cuotas, tiempos) | Elecciones iniciales, no mediciones; a verificar con pruebas de frontera |
| E-A05-18 | Clases `E` de A04 sin analizar = contenido no cubierto en A05; `authorized_removals_performed` siempre vacío | A05 no transforma ni elimina; «autorizada» ≠ «efectuada» |
| E-A05-19 | Admisión exige `file.expires_at ≥ deadline_at`; la publicación revalida la entrada | Ningún resultado con entrada vencida; sin prórrogas implícitas |
| E-A05-20 | Gracia de purga física = `download_timeout_s` + 5 min; acceso lógico denegado desde la transición | Descargas iniciadas antes pueden completar; borrado coordinado |
| E-A05-21 | Purga de `admin_events` local, con corte ≤ ahora − retención, lote explícito, previsualización por defecto y resumen sin contenido | Resolver Q-04 sin endpoint HTTP ni promesas de archivo |
| E-A05-22 | Cuotas con reserva previa (`scope_usage`/`engine_usage`, orden scope → instalación) y recálculo en la limpieza | Crecimiento acotado antes de la limpieza; deriva reparable |
| E-A05-23 | `HostTransaction`: permitida en operaciones breves solo de base (savepoint); rechazada antes de efectos en operaciones con E/S de storage o transferencias | A01 D-23/D-14 |
| E-A05-24 | No se conserva el nombre original del archivo; descarga como `<file_id>.<ext>` | Nombres seguros; el nombre puede ser sensible |

## 4. Diferencias respecto del playbook (revisión 6)

| # | Playbook | A05 | Motivo |
|---|---|---|---|
| 1 | «Storage, colas, límites, retención y recuperación» por decidir (§12) | Retención aprobada (D-A05-01); sin cola obligatoria; base como autoridad (E-A05-01) | Decisión del usuario y encargo |
| 2 | «Separar expiración de originales, resultados y correspondencias» | Originales y resultados con plazos propios; correspondencias solo **registradas** como responsabilidad de A06 (sin tablas ni semántica) | A06 define su retención |
| 3 | «operaciones de análisis de documentos» | `document_analysis` y además `text_analysis` durable; validación como fase, no operación genérica | Encargo; `analyze_text` de A03 se conserva |
| 4 | «cuarentena/validación según estrategia elegida» | Cuarentena = privado y pendiente de validación; aislamiento del parser con perfiles verificables | Encargo; no se promete ausencia de malware |
| 5 | Tratamiento de trabajos ante cambios administrativos no definido | D-A05-02 | Decisión del usuario |
| 6 | Q-04 de A01 (retención de `admin_events`) diferida a A05 | 90 días, purga local explícita | D-A05-01 / E-A05-21 |

Lo no listado permanece vigente.

## 5. Backlog (fuera de los criterios de aceptación)

| ID | Mejora |
|---|---|
| B-A05-01 | Cargas reanudables o *multipart* |
| B-A05-02 | URLs firmadas opcionales para descarga, con caducidad corta |
| B-A05-03 | Conservar el nombre original del archivo (saneado, con política de privacidad) |
| B-A05-04 | Adaptadores de storage adicionales (S3 y compatibles) y resolvers de referencia |
| B-A05-05 | Granularidad de autorización por archivo/creador dentro de un scope |
| B-A05-06 | Notificaciones/webhooks salientes del motor (hoy: avisos del host tras el commit) |
| B-A05-07 | Prioridades y equidad entre scopes en la reclamación |
| B-A05-08 | Ampliar el subconjunto PDF de lectura (object streams, `/Contents` en arreglo) más allá de lo que se pruebe en A05; Type0/CID tras D-A04-04 |
| B-A05-09 | Métricas exportables de trabajos y limpieza |
| B-A05-10 | Retención por scope o por trabajo (hoy: por instalación) |
| B-A05-11 | Exposición HTTP de estado de workers/aislamiento (diagnóstico autenticado) |
| B-A05-12 | Endurecer la carga de grants desconocidos (B-A02-12/B-A03-10) |

## 6. Preguntas abiertas y tensiones

Ninguna bloquea la especificación. Tensiones resueltas dentro del alcance:

1. **Runtimes anteriores y deshabilitación del scope**: resuelto sin subir `min_runtime_level` mediante el fence y la reconciliación conservadora (E-A05-10/11). Coste aceptado: durante un despliegue mixto, una edición de metadatos hecha por un runtime anterior cancela trabajos pendientes de ese scope.
2. **Primer corte de `admin_events`**: aplicar 90 días a eventos previos puede eliminar historial antiguo en la primera purga; mitigado por previsualización y corte explícito.
3. **SQLite y reclamación concurrente**: un único escritor serializa reclamaciones; aceptable para el uso previsto (un nodo).
