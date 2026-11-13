# A05 — Registro de decisiones

> **Estado: A05 IMPLEMENTADA en `0.4.0`, pendiente de revisión y aceptación del usuario (revisión 4 del registro).** La especificación (rev. 3, `d7ab051`) se implementó en `c057b27`; evidencia y límites en el [informe](../a05-implementation-report.md). Las decisiones `D-A05-nn` fueron **aprobadas por el usuario** con el encargo de A05; las elecciones técnicas `E-A05-nn` se derivaron por criterio al redactar el contrato y **no** son decisiones de producto (revisables sin abrir otra ronda por detalles rutinarios).

Contratos que usan estas decisiones: [especificación de A05](../features/A05-trabajos-archivos-y-ciclo-de-vida.md) · [trabajos, archivos y ciclo de vida](../jobs-files-and-lifecycle.md) (contrato completo). Decisiones heredadas: [A01](A01-decisions.md), [A02](A02-decisions.md), [A03](A03-decisions.md), [A04](A04-decisions.md).

Categorías (no mezclarlas): **decisiones aprobadas** (§2) · **elecciones técnicas derivadas** (§3) · **diferencias con el playbook** (§4) · **backlog** (§5) · preguntas y tensiones (§6).

## 0. Historial de revisiones

| Rev. | Contenido |
|---|---|
| 1 | Primera redacción (solo documental, commit `7fb22cd`): D-A05-01 y D-A05-02 aprobadas; E-A05-01 a E-A05-24; diferencias con el playbook; backlog. Resolución de Q-04 de A01 por referencia. |
| 2 | Corrección documental previa a la implementación, sin cambiar D-A05-01/02 ni los plazos: fence con invariantes de reconciliación previa (E-A05-10 revisada, contraejemplo incluido); orden común de bloqueos y precondiciones temporales completas (E-A05-26); cuotas con bytes reservados/almacenados/inciertos hasta la eliminación confirmada (E-A05-22 revisada); huella con contexto, envío/carga/registro idempotentes completos y retención de metadatos de archivos (E-A05-13 revisada, E-A05-25); escritores tardíos con `not_after`, sellos y tombstones, y fencing de la limpieza (E-A05-05/08 revisadas); validaciones por identidad (E-A05-27); descarga verificada con spool (E-A05-28); `delete_job_data` solo terminal (E-A05-29); cobertura por capacidad y `E` sin afectar el subconjunto `S` (E-A05-18 revisada); hipervínculos externos (E-A05-30); matriz A05 conservadora sin criterios abiertos (E-A05-31). Se corrige la atribución del coste del fence: es una elección propuesta, no un coste aceptado por el usuario. Niveles de migración reevaluados (E-A05-11). |
| 3 | Tres correcciones documentales, sin cambiar decisiones ni plazos: garantías de storage separadas (no publicar / no rematerializar / bytes liberados), evidencia de liberación de escritores con `flock`, `abort_after` como plazo cooperativo (se elimina la equivalencia entre plazo y no visibilidad y el margen de reloj), capacidad finita de sellos y orden real e idempotencia del sellado (E-A05-04/05/06/08 revisadas, E-A05-33); limpieza de spool de procesos muertos (E-A05-32); reconciliación del fence dentro de `HostTransaction` en la misma conexión (E-A05-34); bloqueo de contadores antes de los trabajos en reclamación, recuperación y transiciones terminales (E-A05-22/26 revisadas). Matriz de aceptación ampliada (AC-A05-43 a 52). |

| 5 | Corrección de bloqueantes de la revisión de la implementación `0.4.0` (sin cambiar `D-A05-nn`, `E-A05-nn` de producto, plazos, versión ni migración `0003`): `I-A05-13` a `I-A05-17` (§3.1): hora de decisión posterior a las esperas, conjunto exacto de artefactos, aprobación del aislamiento por instancia y configuración con prueba de memoria, orden de bloqueos de la reclamación y de la expiración. **`E-A05-10` sigue siendo una elección pendiente del usuario.** |
| 4 | Implementación (`0.4.0`): precisiones `I-A05-01` a `I-A05-12` (§3.1) surgidas al implementar y probar. Ninguna `D-A05-nn` ni elección `E-A05-nn` de producto cambia; las que corrigen el texto de la especificación se indican. |

## 1. Fuente, precedencia y herencia

- Referencia: `STAGE_3_PLAYBOOK.md`, revisión 6 (copia local ignorada por Git; **no se modifica ni se declara actualizada**). Las decisiones posteriores (A01–A04 y estas) prevalecen donde difieren (§4).
- A01, A02, A03 y A04 están **cerradas y aceptadas** por el usuario; A04 como investigación de viabilidad acotada (mergeada en `main` con el merge `58001e0`, PR #3). No se reabren ni se cambia ninguna de sus decisiones; D-A04-04 se conserva (comprobación representativa de PDF al inicio de A08; no bloquea A05).
- Heredado **sin cambios**: A01 D-06/D-07, D-08, D-11/D-33, D-13/D-14/D-23, D-15/D-16/D-24, D-17/D-27, D-18/D-25, D-30 (control optimista estricto de `update_scope`, que el fence no altera); A02 D-A02-02 y la selección de A02 §8; A03 D-A03-01 (análisis de texto en la misma llamada, **se conserva**), D-A03-02, D-A03-03; A04 D-A04-01 a 04.

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
| E-A05-01 | La base del motor es la única autoridad sobre estado, intentos, posesión y publicación; sin cola obligatoria; avisos al host solo tras el commit, sin atomicidad base/cola | Playbook A05 («sin segundo sistema de workers») |
| E-A05-02 | Seis estados con la matriz T1–T10 (incluida T5b: cancelar un `running` con lease vencido); cancelación solicitada y ciclo de vida de datos como dimensiones separadas; toda transición terminal fija timestamps, cierra intentos, descarta artefactos y ajusta contadores en la misma transacción | Encargo; sin estados ambiguos ni contadores huérfanos |
| E-A05-03 | Lease finito (60 s) + heartbeat (20 s) + *fencing* (`attempt_no` + `lease_token`); `attempt_started_at` y `attempt_deadline_at` persistidos; lease inicial acotado por el plazo del intento y del trabajo; mutaciones del dueño distintas de la recuperación; posesión vencida no se revive | Un worker obsoleto no puede publicar ni prolongar su posesión |
| E-A05-04 | Autoridad temporal = reloj de la base; `abort_after` es solo un plazo de aborto cooperativo del backend; ninguna garantía depende de sincronizar relojes ni de márgenes | Comprobar la hora y luego publicar el nombre no es atómico |
| E-A05-05 | Protocolo de storage con capacidades obligatorias: *create-only*, `max_bytes`, sello durable que nunca se retira por operaciones normales, evidencia de liberación de escritores (`writers_released`) y `stat` que distingue ausencia de error; backends sin ellas son incompatibles (`configuration_invalid`); compatibles pero temporalmente incapaces dejan la purga pendiente con su cargo | Separar «no publicar», «no rematerializar» y «bytes liberados»; no prometer limpieza universal |
| E-A05-06 | Referencia filesystem: `flock` por clave retenido durante toda la escritura y liberado al final; temporal determinista con `O_EXCL`; `link` por ruta; sello en orden temporal → final con `fsync` de directorios; evidencia de liberación obteniendo el `flock` sin espera tras el sello | Un sello no cierra descriptores abiertos; el `flock` lo libera el núcleo al cerrar o morir el proceso, también ante suspensión prolongada (en cuyo caso no hay evidencia y el cargo se conserva) |
| E-A05-07 | Referencias externas solo por Python; versión fijada una vez, nunca reseleccionada; verificación por versión + SHA-256 + tamaño; materialización si no hay estabilidad | No reenviar archivos; sin URLs arbitrarias |
| E-A05-08 | Protocolo de 5 pasos con dueño en el paso 4; `delete` + `stat` para escrituras completadas; sello + evidencia de liberación para resultados desconocidos; `storage_tombstone` mínimo hasta completar la liberación; fencing de la limpieza; diagnósticos `purge_pending_writer_active` / `purge_pending_backend_error` | Sin huérfanos permanentes, sin éxito de purga inventado, sin borrar la última intención |
| E-A05-09 | Reintentos: catálogo cerrado; 5 intentos; 24 h; backoff 30 s×2 hasta 15 min con *jitter*; reconciliación antes de repetir | Finitud, sin exactamente-una-vez |
| E-A05-10 | Fence de scope con invariantes I1–I6: comprobación bajo bloqueo; **reconciliar antes de avanzar** `observed_scope_revision`; mutaciones A05 tras la reconciliación; reconciliación idempotente; inicialización perezosa que cancela trabajos de generación desconocida; lectores que no deciden ante divergencia. **Elección propuesta (no aceptada por el usuario):** mientras convivan runtimes anteriores, cualquier cambio efectivo suyo sobre un scope, incluida una edición de metadatos, cancela los trabajos no terminales del scope | Comprobar solo el estado no detecta deshabilitar+rehabilitar; la rev. 1 permitía ocultar un cambio desconocido al avanzar la revisión observada; no depende de `admin_events` |
| E-A05-11 | Migración aditiva `0003_jobs_files`: `schema_level` 3; `min_runtime_level` sigue en 1, **reevaluado tras la rev. 2** y justificado por invariantes (runtimes anteriores solo cambian scopes incrementando `scopes.revision`, verificado en el código; sus `UPDATE` se serializan con los bloqueos de A05; no tocan tablas nuevas) | Compatibilidad por garantías, no por poder leer tablas |
| E-A05-12 | Seis permisos de scope nuevos (`files:write`, `files:read`, `jobs:read`, `jobs:cancel`, `results:read`, `data:delete`; catálogo 17) y reutilización de `analysis:execute` para enviar | Separación sin permiso universal |
| E-A05-13 | Idempotencia por (`scope_id`, operación, SHA-256 de la clave); huella con digest del contexto exacto (presencia distinguida); búsqueda antes de materializar entradas o reseleccionar; `INSERT … ON CONFLICT DO NOTHING` (también válido dentro de un savepoint del host); objetos de la solicitud perdedora o no vinculados abandonados; cargas repetidas hasheadas sin almacenar; generación de carga para el reinicio | Repeticiones y carreras sin conflictos de unicidad abortivos ni crecimiento fuera de cuota |
| E-A05-14 | Extra `documents` con importación perezosa; `lxml` endurecido y `pikepdf`/QPDF (MPL-2.0) como candidatas; versiones y licencias se fijan al implementar | Coherente con A04 y D-33 |
| E-A05-15 | Extracción en proceso separado; perfiles `linux_container_reference`, `host_provided`, `unconfined_development` (solo explícito); canario; sin fallback silencioso | «Un subprocess no equivale a sandbox» |
| E-A05-16 | Unidad DOCX = párrafo, PDF = página; unidad excesiva ⇒ `unit_too_large`; vacías sin llamar a A03; documento sin texto ⇒ `no_extractable_text` | Sin truncamiento ni éxito por vacuidad |
| E-A05-17 | Tabla de límites iniciales (25 MiB, 100 páginas, descompresión 200/50 MiB y 100:1, anidación de tablas 2, resultado y mapa 32 MiB, spool 256 MiB, cuotas, tiempos) | Elecciones iniciales, no mediciones |
| E-A05-18 | `E` no analizados = contenido no cubierto que impide `document_coverage_complete` pero **no** afecta `textual_subset_complete`; `authorized_removals_performed` vacío; cobertura por capacidad con los cuatro estados de A03, `complete` solo en capacidades ejecutadas | «Autorizada» ≠ «efectuada»; sin verdad por vacuidad ni falsos ambiguos |
| E-A05-19 | Admisión exige que entrada **y** contexto venzan después de `deadline_at`; la publicación revalida ambos bajo bloqueo | Ningún resultado con entrada vencida |
| E-A05-20 | Gracia de purga física = `download_timeout_s` + 5 min; acceso lógico denegado desde la transición | Descargas iniciadas antes completan |
| E-A05-21 | Purga de `admin_events` local, corte ≤ ahora − retención, lote explícito, previsualización, resumen sin contenido | Resolver Q-04 sin endpoint HTTP |
| E-A05-22 | Cuotas con contadores reservados/almacenados/**inciertos** que cuentan hasta la liberación confirmada (no hasta el sello); reserva no ampliable; `transient_overhead_factor`; una reserva por artefacto; contadores de trabajos bloqueados (scope e instalación) antes del trabajo en todas las transiciones; recálculo por scope completo bajo el bloqueo de uso | Crecimiento acotado aun con limpieza averiada o escritores suspendidos |
| E-A05-23 | `HostTransaction`: permitida en operaciones breves solo de base (savepoint, orden común, `ON CONFLICT`, fence en la misma conexión según E-A05-34); rechazada antes de efectos en operaciones con E/S de storage | A01 D-23/D-14; sin commits ocultos ni conexiones secundarias |
| E-A05-24 | No se conserva el nombre original; descarga como `<file_id>.<ext>` | Nombres seguros |
| E-A05-25 | Metadatos de archivos, validaciones, registros externos (incluida `external_ref`) y su idempotencia: 30 días desde `end_of_life_at` (transición a estado terminal del archivo); después solo sobrevive el tombstone sin contenido si falta confirmar el borrado | Las cargas y registros no tienen `terminated_at` de trabajo; nada se retiene indefinidamente. No modifica el plazo aprobado de trabajos |
| E-A05-26 | Orden común de bloqueos (scope → fence → uso de scope → uso de instalación → idempotencia → archivos → validaciones → trabajos → intentos/artefactos → tombstones); toda transacción que cambie contadores bloquea ambos usos antes de archivos/trabajos/artefactos y no vuelve atrás; candidatos de reclamación leídos sin bloqueo y revalidados; `SKIP LOCKED` solo en trabajos; publicación con todas sus precondiciones | Elimina inversiones trabajo→scope y trabajo→contadores; sin promesa de ausencia de interbloqueos con el host |
| E-A05-27 | Validaciones por identidad (`file_sha256` + validador + versión + configuración), solo de inserción, primer resultado autoritativo, escritas solo por el dueño; descarga y admisión según la identidad vigente | Sin «última escritura gana»; rechazos y aprobaciones de configuraciones distintas coexisten |
| E-A05-28 | Descarga verificada con spool privado acotado antes de enviar encabezados; límite declarado de la garantía | Un hash al final del stream no impide entregar una respuesta aparentemente completa |
| E-A05-29 | `delete_job_data` solo sobre trabajos terminales (`job_not_terminal`); no elimina entradas | Sin carrera con la publicación |
| E-A05-30 | Relación externa de hipervínculo admitida (destino `N`, nunca resuelta); cualquier otra relación externa ⇒ rechazo | Coherencia con A04 DX-07 sin accesos de red |
| E-A05-31 | Matriz A05 conservadora fijada antes de implementar: lo pendiente en A04 se rechaza o se declara `N` (`/Rotate 270`, ángulos oblicuos, object streams, `/Contents` en arreglo, anidación > 2, varias secciones…) | Sin criterios abiertos «soportado si luego pasa»; ampliaciones en backlog |
| E-A05-32 | Spool de descargas por proceso en `spool/<instancia>/` con `flock` de propietario durante la vida del proceso; residuos de procesos muertos eliminados al iniciar y en `run_cleanup` local; disco acotado por procesos vivos × `max_spool_bytes` más residuos hasta la siguiente limpieza | El semáforo de un proceso no ve archivos de procesos anteriores; solución local sin sistema nuevo |
| E-A05-33 | Capacidad finita de sellos: cada reserva administrada consume un sello potencial; `sealed_keys + pending_seals + open_reservations` acotado por scope (10 000) e instalación (1 000 000); al alcanzarlo, `quota_exceeded` (`seal_capacity`) con diagnóstico, sin retirar sellos | Los sellos permanentes no pueden crecer sin límite aunque no ocupen contenido |
| E-A05-34 | Fence dentro de `HostTransaction`: el envío adquiere el scope `FOR NO KEY UPDATE` desde el comienzo del savepoint y crea o reconcilia el fence en la misma conexión; la reversión del host revierte reconciliación y aceptación juntas sin debilitar I1; las lecturas no reconcilian | Una reconciliación en otra conexión esperaría a la transacción del propio llamador |

### 3.1 Precisiones y decisiones de implementación (`I-A05-nn`)

No son decisiones de producto. Cada una está respaldada por código y pruebas (informe).

| ID | Precisión | Motivo / evidencia |
|---|---|---|
| I-A05-01 | La definición de las tablas es única (`persistence/a05_schema.py`) y la migración `0003` la usa, congelada en esa revisión; la migración no inserta filas | Evitar dos copias divergentes; contadores y *fences* se crean de forma perezosa |
| I-A05-02 | `customXml/` se rechaza (DX-17), también los de la plantilla de `python-docx` y de muchos documentos de Word | Matriz conservadora; las fixtures los eliminan. Limitación real (B-A05-08) |
| I-A05-03 | PDF: fuentes sin `/ToUnicode` solo con `/Encoding` estándar sin `/Differences` (o Type1 estándar-14 sin `/Encoding`); `StandardEncoding` se decodifica con `cp1252` | Sin dependencias nuevas (AGL); `/Differences` queda fuera del subconjunto |
| I-A05-04 | PDF: comprobaciones previas por bytes (`/ObjStm`, `/Prev`, `%%EOF` repetido, `/ByteRange`, acciones ejecutables, adjuntos) y flujos solo `FlateDecode` con tope de bytes producidos; `pikepdf` no admite límites al abrir (el hijo aporta el límite duro) | Falsos positivos aceptados; ver contrato §22 |
| I-A05-05 | `RLIMIT_AS`/`RLIMIT_NPROC` solo en Linux; `RLIMIT_CPU`, `RLIMIT_FSIZE` y `RLIMIT_CORE` en todas | No fiables en macOS; `unconfined_development` no es *sandbox* |
| I-A05-06 | La imagen del contenedor de referencia debe incluir `sqlalchemy[asyncio]`, `pydantic`, `lxml` y `pikepdf` (el paquete los importa); el motor no construye ni distribuye la imagen | Descubierto al probar el contenedor |
| I-A05-07 | `NeedReconcile` y `LeaseLost` son `EngineError` internos para atravesar la frontera transaccional; nunca llegan a la API pública | `translate()` convertía las excepciones ajenas en `internal_error` |
| I-A05-08 | El guard del corte de `admin_events` admite 60 s de diferencia de reloj entre operador y base | Un corte «exactamente 90 días» fallaba por microsegundos |
| I-A05-09 | `FilesystemStorage._seal` es idempotente y seguro con sellos concurrentes (nunca retira un directorio sello; reintenta ante `PermissionError`/`FileExistsError`) | Defecto hallado por la prueba de limpiezas concurrentes |
| I-A05-10 | Las fuentes de stream que fallan (cliente que se va, resolutor) se convierten en `upload_aborted`; el contador de bytes aplica el límite real, no el declarado | Pruebas de cargas interrumpidas y `declared_size` falso |
| I-A05-11 | Pruebas heredadas actualizadas solo por constantes nuevas: nivel de esquema 3, cabeza `0003`, 17 permisos, versión `0.4.0`, códigos de error nuevos; en dos pruebas de migración el dato A01 se escribe por SQL porque el camino A05 de `update_scope` requiere las tablas de `0003` | Consecuencia directa de la migración aditiva; ver informe |
| I-A05-12 | `ReferenceExecutor`, `job_worker()` y `maintenance()` son los puntos de integración del host; la CLI `worker` toma la inferencia de la configuración del servicio (mock en A05) | Sin cola adicional; adaptadores reales son de A09 |
| I-A05-13 | **La hora que decide se lee después de las esperas.** En cada mutación que depende de posesión, plazo o expiración (reclamación, heartbeat y cambio de fase, fallo del dueño, reserva y registro de artefactos, validación, publicación, cancelación, recuperación, expiración, purga, subida, envío, reconciliación) se toman primero los bloqueos pertinentes, se lee `clock_timestamp()` (SQLite: la transacción inmediata ya espera al escritor antes de la primera sentencia) y solo entonces se decide; la hora anterior a las esperas se usa solo para columnas de contabilidad. El heartbeat dejó de ser un `UPDATE` condicional con hora previa: bloquea la fila, lee el reloj y decide. Las marcas de reclamo de purga son un único `UPDATE` cuyas comparaciones `<= ahora` con una hora previa solo pueden ser más conservadoras | Regresiones con dos conexiones independientes (`test_a05_review_fixes`): fallan con el código anterior |
| I-A05-14 | **Publicación con conjunto exacto.** `publish` exige, por operación, exactamente los roles `text_analysis → {result}` y `document_analysis → {result, source_map}`; cada referencia debe existir, estar `written`, ser del mismo trabajo e intento, tener `kind` igual a su rol, `sha256` y tamaño registrados y no repetirse entre roles. Si no, el trabajo pasa a `failed` (`internal_error`, sin reintento: es un defecto del ejecutor), se descartan los artefactos del intento y no se publica nada | Pruebas de conjuntos vacíos, incompletos, duplicados, inesperados, con roles intercambiados, ajenos y no escritos |
| I-A05-15 | **Aprobación del aislamiento por instancia y configuración.** No hay caché a nivel de módulo: `ContentEngine` guarda la aprobación y la clave que la ata (instancia del ejecutor, perfil, `untrusted_input`, memoria, procesos, CPU y plazo de extracción). Un motor nuevo, o una configuración distinta, vuelve a verificar; cualquier excepción o `False` ⇒ `configuration_unavailable`, sin extracción y sin *fallback*. `verify` recibe los límites del trabajo | Pruebas con el mismo `DocumentSetup` reutilizado por dos motores y con límites no predeterminados |
| I-A05-16 | **Canario de memoria y procesos del perfil de referencia.** Dentro del contenedor el hijo informa el límite de memoria que ve (cgroup) y se compara con el configurado; un segundo arranque intenta reservar y tocar `memoria + 64 MiB` y el control se considera aplicado solo si el hijo muere (137) o recibe `MemoryError`; el sondeo de procesos usa `extraction_max_pids + 16` hilos. El canario se ejecuta una vez por instancia y configuración (no periódicamente: límite documentado) | Pruebas con Docker: límite visto = 192 MiB, asignación mayor terminada, contenedores con más memoria o sin límite rechazados |
| I-A05-17 | **Orden de bloqueos en reclamación y expiración.** `_claim_tx` lee el trabajo sin bloquear solo para conocer el snapshot, bloquea la entrada y el contexto (6) y después toma el trabajo (8, `SKIP LOCKED`) y revalida; `expire_deadlines` hace lo mismo; las lecturas de archivo de `submit` pasan a bloquear antes de decidir | Registro de las sentencias con bloqueo por transacción (PostgreSQL) y prueba de que una reclamación en espera de una entrada no retiene la fila del trabajo |

## 4. Diferencias respecto del playbook (revisión 6)

| # | Playbook | A05 | Motivo |
|---|---|---|---|
| 1 | «Storage, colas, límites, retención y recuperación» por decidir (§12) | Retención aprobada (D-A05-01); sin cola obligatoria; base como autoridad (E-A05-01) | Decisión del usuario y encargo |
| 2 | «Separar expiración de originales, resultados y correspondencias» | Originales y resultados con plazos propios; correspondencias solo registradas como responsabilidad de A06 | A06 define su retención |
| 3 | «operaciones de análisis de documentos» | `document_analysis` y además `text_analysis` durable; validación como fase | Encargo; `analyze_text` se conserva |
| 4 | «cuarentena/validación según estrategia elegida» | Cuarentena = privado y pendiente de validación; validaciones por configuración; aislamiento con perfiles verificables | Encargo; sin promesa de ausencia de malware |
| 5 | Tratamiento de trabajos ante cambios administrativos no definido | D-A05-02 | Decisión del usuario |
| 6 | Q-04 de A01 diferida a A05 | 90 días, purga local explícita | D-A05-01 / E-A05-21 |

Lo no listado permanece vigente.

## 5. Backlog (fuera de los criterios de aceptación)

| ID | Mejora |
|---|---|
| B-A05-01 | Cargas reanudables o *multipart* |
| B-A05-02 | URLs firmadas opcionales para descarga |
| B-A05-03 | Conservar el nombre original del archivo (saneado) |
| B-A05-04 | Adaptadores de storage adicionales (S3 y compatibles) que cumplan las garantías de E-A05-05 |
| B-A05-05 | Granularidad de autorización por archivo/creador dentro de un scope |
| B-A05-06 | Notificaciones/webhooks salientes del motor |
| B-A05-07 | Prioridades y equidad entre scopes en la reclamación |
| B-A05-08 | Ampliar el subconjunto de lectura (PDF con object streams, `/Contents` en arreglo, `/Rotate 270`, ángulos oblicuos; DOCX con varias secciones o anidación > 2) con evidencia propia; Type0/CID tras D-A04-04 |
| B-A05-09 | Métricas exportables de trabajos y limpieza |
| B-A05-10 | Retención por scope o por trabajo |
| B-A05-11 | Exposición HTTP de estado de workers/aislamiento |
| B-A05-12 | Endurecer la carga de grants desconocidos (B-A02-12/B-A03-10) |
| B-A05-13 | Compactación segura de sellos del filesystem (y liberación de capacidad `seal_capacity`) conservando la garantía de no rematerializar |
| B-A05-14 | Ampliación atómica de la reserva durante una carga sin `declared_size` exacto |
| B-A05-15 | Repetición periódica del canario de aislamiento y verificación del límite de CPU/duración dentro del contenedor |
| B-A05-16 | Verificación estática (o *lint* de pruebas) del orden de bloqueos de todo el código, y evidencia del orden en SQLite más allá de la lectura/escritura serializada |

## 6. Preguntas abiertas y tensiones

Ninguna bloquea la especificación. Tensiones resueltas dentro del alcance, con elecciones propuestas (no aprobaciones del usuario):

1. **Runtimes anteriores y scopes**: el fence (E-A05-10) mantiene la garantía sin subir `min_runtime_level`, a costa de que, durante un despliegue mixto, una edición de metadatos hecha por un runtime anterior cancele los trabajos pendientes del scope. Es una elección técnica (implementada y probada con el código real de `0.3.0`) pendiente de tu revisión; la alternativa sería exigir que todos los runtimes estén actualizados antes de aceptar trabajos.
2. **Primer corte de `admin_events`**: aplicar 90 días a eventos previos puede eliminar historial antiguo en la primera purga; mitigado por previsualización y corte explícito.
3. **SQLite y reclamación concurrente**: un único escritor serializa reclamaciones; aceptable para el uso previsto (un nodo).
4. **Sellos permanentes del filesystem**: una entrada vacía por cada escritura de resultado desconocido, contabilizada y acotada (E-A05-33); su compactación queda en backlog (B-A05-13).
5. **Configuración del validador**: cambiarla hace que los originales validados con otra configuración dejen de ser descargables hasta revalidarse (conservador).
6. **Escritores suspendidos indefinidamente**: mientras retengan el `flock`, su cargo nunca se libera; es conservador y queda visible en el resumen de limpieza.
7. **`HostTransaction` y bloqueo del scope**: el envío embebido retiene el scope exclusivo hasta el commit del host; otras operaciones del scope esperan (elección propuesta E-A05-34).
