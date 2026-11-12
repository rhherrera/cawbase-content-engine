# A05 — Trabajos, archivos y ciclo de vida

> **Estado: ESPECIFICADA / NO IMPLEMENTADA, pendiente de revisión del usuario.** Entrega solo documental: sin código, migraciones, tests, scripts, dependencias, configuración ejecutable ni cambio de versión (`0.3.0`). Ninguna operación, ruta, permiso, tabla ni comando de A05 está disponible. No está cerrada ni aceptada.

- Fuente: `STAGE_3_PLAYBOOK.md` revisión 6 (§6 A05), más las decisiones posteriores ([A01](../decisions/A01-decisions.md), [A02](../decisions/A02-decisions.md), [A03](../decisions/A03-decisions.md), [A04](../decisions/A04-decisions.md), [A05](../decisions/A05-decisions.md)), que prevalecen.
- Repositorio: `cawbase-content-engine` · rama: `feature/a05-jobs-files-lifecycle`, creada desde `main` en `58001e0` (merge del PR #3 de A04).
- Dependencias: A01–A03 cerradas y aceptadas; A04 cerrada y aceptada como investigación de viabilidad acotada (condición D-A04-04 para A08; no bloquea A05). Habilita A06.
- Contrato permanente: [trabajos, archivos y ciclo de vida](../jobs-files-and-lifecycle.md). Este documento define alcance y aceptación y **enlaza**; no copia el contrato.

## 1. Objetivo, alcance y exclusiones

**Objetivo.** Ejecutar operaciones largas de forma recuperable y aislada sin imponer un segundo sistema de workers: cargar o referenciar archivos de forma segura, validarlos, extraer y mapear su texto (subconjunto A04), analizarlo con el contrato de A03 en trabajos durables, consultar, cancelar y leer resultados autorizados, y gestionar retención y limpieza.

**Incluye:** archivos administrados y referencias externas ([contrato §3](../jobs-files-and-lifecycle.md#3-archivos-y-almacenamiento)); consistencia base/storage (§4); trabajos `document_analysis` y `text_analysis`, estados y transiciones (§5); reclamación con lease y *fencing* (§6); reintentos y recuperación (§7); idempotencia y fijación (§8); cambios administrativos posteriores (§9); validación y aislamiento del parser (§10); extracción, mapeo y resultados documentales (§11); límites y cuotas (§12); retención, expiración, limpieza y purga de `admin_events` (§13); API Python, HTTP, permisos, errores, persistencia y migración `0003`, transacciones y observabilidad (§14–§20).

**Excluye:** protección, redacción, seudonimización, correspondencias y restauración (A06–A08); transformación DOCX/PDF y la transformación/restitución experimental de A04; OCR; modelo real, calidad de detección y segmentación por modelo (A09); integración con CAWbase, infraestructura y publicación; URLs firmadas, cargas *multipart*, colas obligatorias y exactamente-una-vez.

## 2. Decisiones

Aprobadas: **D-A05-01** (retención predeterminada) y **D-A05-02** (cambios administrativos después de aceptar), texto en [A05-decisions §2](../decisions/A05-decisions.md#2-decisiones-aprobadas). Elecciones técnicas `E-A05-01` a `24` en [§3](../decisions/A05-decisions.md#3-elecciones-técnicas-derivadas) (no son decisiones de producto). Diferencias con el playbook y backlog: §4–§5 del registro.

## 3. Mapa del contrato

| Tema | Contrato |
|---|---|
| Principios, garantía de ejecución | [§1](../jobs-files-and-lifecycle.md#1-principios-y-fronteras) |
| Archivos, storage, referencias, HTTP de archivos | [§3](../jobs-files-and-lifecycle.md#3-archivos-y-almacenamiento) |
| Protocolo base/storage y tabla de fallos | [§4](../jobs-files-and-lifecycle.md#4-consistencia-entre-base-y-almacenamiento) |
| Operaciones, matriz de transiciones, cancelación | [§5](../jobs-files-and-lifecycle.md#5-trabajos) |
| Autoridad temporal, reclamación, lease, publicación | [§6](../jobs-files-and-lifecycle.md#6-reclamación-posesión-y-tiempo) |
| Reintentos y commit incierto | [§7](../jobs-files-and-lifecycle.md#7-reintentos-y-recuperación) |
| Idempotencia y fijación | [§8](../jobs-files-and-lifecycle.md#8-idempotencia-y-fijación-de-ejecución) |
| Scope, perfil y clave tras la aceptación | [§9](../jobs-files-and-lifecycle.md#9-cambios-administrativos-después-de-aceptar-d-a05-02) |
| Validación, aislamiento | [§10](../jobs-files-and-lifecycle.md#10-seguridad-del-procesamiento-documental) |
| Matriz de soporte, resultados, coverage | [§11](../jobs-files-and-lifecycle.md#11-extracción-mapeo-y-análisis-en-a05) |
| Límites y cuotas | [§12](../jobs-files-and-lifecycle.md#12-límites-operativos-e-a05-17) |
| Retención, limpieza, `admin_events` | [§13](../jobs-files-and-lifecycle.md#13-retención-expiración-y-purga-d-a05-01) |
| API Python / HTTP / permisos / errores | [§14](../jobs-files-and-lifecycle.md#14-api-python-propuesta--no-disponible)–[§17](../jobs-files-and-lifecycle.md#17-errores-nuevos-propuestos) |
| Persistencia, `0003`, compatibilidad | [§18](../jobs-files-and-lifecycle.md#18-persistencia-y-migración-0003-propuesta) |
| `HostTransaction` por operación | [§19](../jobs-files-and-lifecycle.md#19-transacciones-por-operación-hosttransaction) |

## 4. Reglas para la implementación posterior

1. Un prompt de implementación separado, autocontenido; esta entrega no implementa.
2. El código documental vive en el extra `documents` con importación perezosa; **no** importa `experiments/a04`. Las pruebas de A05 son propias (se pueden reutilizar fixtures y criterios de A04 copiándolos a `tests/` con su manifiesto).
3. El mock sustituye **solo** la inferencia: base, storage, extracción, aislamiento, cuotas y permisos se prueban reales.
4. PostgreSQL y SQLite reales (bases descartables `cce_t_*`), incluido el piso de SQLite según la política vigente (`scripts/sqlite_floor_validate.sh`) y PostgreSQL 15–18 según A01.
5. La evidencia separa **especificado ≠ implementado ≠ probado en macOS ≠ probado en Linux local ≠ probado en GitHub Actions ≠ aceptado**; un *skip* no es aprobación; el informe será `docs/a05-implementation-report.md`.
6. Ningún fixture contiene datos reales ni secretos (`example.test`, valores evidentemente falsos).

## 5. Matriz de aceptación (para la implementación posterior)

Criterios trazables al contrato. Estado de todos: **especificado / no implementado / no probado**.

| AC | Criterio verificable | Contrato | Evidencia exigida |
|---|---|---|---|
| AC-A05-01 | E2E real (PG y SQLite): carga Python → validación → extracción (DOCX y PDF soportados) → mock → resultado publicado → lectura autorizada → expiración → limpieza (objeto eliminado, metadatos conservados, luego purgados) | §3–§13 | prueba E2E por backend con storage filesystem real y aislamiento verificado |
| AC-A05-02 | Lo mismo con referencia externa estable (sin reenvío) y con referencia inestable materializada; el motor **nunca** borra el original externo | §3.5, §13.5 | resolver de prueba que registra llamadas de borrado (debe ser cero) |
| AC-A05-03 | Oráculos independientes DOCX/PDF (manifiesto escrito por el generador del fixture, no por el extractor), con Unicode (no BMP, NFD, ZWJ, `\r\n`, ligaduras), separadores sintetizados, Form XObject compartido y ambigüedad | §11.2 | fixtures sintéticos con manifiesto |
| AC-A05-04 | `analyze_text`, su ruta HTTP, `AnalysisResult` y los límites de A03 sin cambios (regresión completa de A03) y equivalencia Python/HTTP de las operaciones A05 pertinentes | §11.3, §15 | suites de A03 + pruebas de equivalencia |
| AC-A05-05 | Matriz de transiciones T1–T10: cada transición válida probada y cada inválida rechazada; terminales sin salida; repetición de llamadas según la tabla | §5.3 | pruebas por backend |
| AC-A05-06 | Reclamación concurrente (N workers): una sola posesión por trabajo; PostgreSQL con `SKIP LOCKED`, SQLite serializado | §6.2 | pruebas de concurrencia por backend |
| AC-A05-07 | Pérdida de lease: heartbeat, registro de artefacto y publicación de un worker obsoleto fallan aunque nadie haya reclamado; la posesión vencida no se revive; otro worker publica | §6.3, §6.4 | reloj de la base manipulado por plazos cortos, no *mocks* de tiempo del motor |
| AC-A05-08 | Cancelación frente a éxito: carrera en ambos órdenes; si gana la cancelación ninguna salida se publica; si ganó el éxito, cancelar no lo reescribe | §5.4 | pruebas con barreras |
| AC-A05-09 | Deshabilitar el scope cancela no terminales e impide reclamar/publicar; deshabilitar+rehabilitar rápido entre aceptación y publicación **no** permite publicar; rehabilitar no resucita; resultados previos intactos; lecturas permitidas | §9 | pruebas por backend, incluido cambio hecho por el código de A03 (runtime anterior simulado sin el fence) |
| AC-A05-10 | Desactivar/eliminar el perfil y publicar otra versión después de aceptar no afectan al trabajo (usa la versión fijada) | §8.5, §9 | pruebas |
| AC-A05-11 | Revocar la clave tras aceptar: accesos posteriores 401; el trabajo sigue | §9.1 | prueba HTTP |
| AC-A05-12 | Idempotencia: misma clave/solicitud ⇒ mismo trabajo (incluso tras cambiar el predeterminado o desactivar el perfil); clave igual y solicitud distinta ⇒ 409; dos primeras concurrentes; respuesta perdida; repetición tras expirar el resultado no re-ejecuta; tras purgar puede crear otro | §8 | pruebas por backend |
| AC-A05-13 | Cambios de archivo/configuración fijada: hash externo distinto ⇒ `source_changed`; worker sin la configuración ⇒ no reclama / `configuration_unavailable`; sin ejecutor compatible ⇒ `executor_unavailable` al vencer el plazo | §3.5, §8.6 | pruebas |
| AC-A05-14 | Caídas en **cada** frontera de §4.1 (inyectadas en el storage de prueba y cortando el proceso) y commit de resultado incierto con reconciliación | §4.1, §7.3 | pruebas de inyección de fallos |
| AC-A05-15 | Reintentos: solo el catálogo reintentable; backoff y límites; agotamiento ⇒ `retries_exhausted`; plazo ⇒ `deadline_exceeded`; deterministas no se reintentan | §7 | pruebas |
| AC-A05-16 | Limpieza: repetible, por lotes, recuperable tras caída a mitad; fallos de eliminación física reintentados; nunca borra objetos referenciados ni externos | §4.3, §13.5 | pruebas |
| AC-A05-17 | Expiración con trabajo pendiente/activo (falla `input_unavailable`, no publica) y descarga en curso (completa si empezó antes dentro de la gracia; nuevas denegadas) | §13.2, §13.4, §4.3 | pruebas |
| AC-A05-18 | Purga de `admin_events`: corte y lote explícitos, previsualización, guarda de retención, transacciones cortas, eventos anteriores a A05 incluidos, ninguna otra tabla afectada; sin endpoint HTTP; la migración no purga | §13.6 | pruebas con conteo de todas las tablas antes/después |
| AC-A05-19 | Seguridad de archivos: bombas ZIP/Flate (declarado y producido), XML con DTD/entidades, estructura inválida, tipo falso (extensión/MIME), límites efectivos (frontera ±1), rechazos heredados de A04 con motivo cerrado | §10, §12 | fixtures por familia |
| AC-A05-20 | Aislamiento del parser: el canario falla en red, escritura fuera de tmpfs, variables sensibles, procesos y memoria en `linux_container_reference` (Linux); sin fallback silencioso; `unconfined_development` solo explícito y registrado | §10.5 | prueba Linux (Docker local) y comprobación en macOS de la negativa por defecto |
| AC-A05-21 | Resultados: unidades válidas inconclusas conservadas; salida malformada o fallo operativo en una unidad ⇒ sin publicación parcial; unidad excesiva ⇒ `unit_too_large`; vacías sin llamar a A03; documento sin texto ⇒ `no_extractable_text`; sin agregación de puntuaciones | §11.4 | pruebas con mock |
| AC-A05-22 | Indicadores: `textual_subset_complete` y `document_coverage_complete` según §11.6; comentarios/metadatos no analizados cuentan como no cubiertos; `authorized_removals_performed` vacío | §11.5–§11.6 | fixtures D06/P08 equivalentes |
| AC-A05-23 | Cuotas concurrentes (cargas y trabajos simultáneos no superan límites; reservas liberadas tras fallos; contadores reparados por la limpieza) | §12.3 | pruebas por backend |
| AC-A05-24 | HTTP: carga por stream con conteo real, 413, desconexión; límite JSON de 64 KiB intacto en rutas existentes; descargas `no-store`/`attachment`; nada no publicado/cuarentena/rechazado descargable; 404 indistinguible entre scopes | §3.7, §15 | pruebas HTTP |
| AC-A05-25 | Permisos: matriz completa de los 17 (cada operación con y sin su permiso); `analysis:execute` no lee archivos ni resultados; `AdminContext` sin acceso; sin jerarquías | §16 | pruebas |
| AC-A05-26 | Ausencia de contenido sensible (texto, contexto, fragmentos, resultados, hashes de contenido, claves de storage, referencias externas, tokens, claves de idempotencia) en logs, errores, eventos y diagnósticos | §20 | marcadores únicos |
| AC-A05-27 | Instalación liviana: base sin `documents` no importa bibliotecas documentales; gate D-33 se mantiene; wheel/sdist sin `experiments/` | §11.1 | `-m dist` y prueba de grafo de imports |
| AC-A05-28 | Migración `0003` aditiva sobre esquema 2 con datos y objetos ajenos; niveles 3/1; runtime A03 sobre esquema 3 no rompe garantías (AC-A05-09); orden de despliegue documentado | §18 | pruebas de migración por backend |
| AC-A05-29 | `HostTransaction`: operaciones largas rechazadas antes de efectos; breves en savepoint, sin commit del motor | §19 | pruebas por backend |
| AC-A05-30 | Ningún import de `experiments/a04` en `src`; ninguna operación de protección/restauración expuesta | §11.1 | prueba estática |

## 6. Riesgos y límites declarados

- La extracción PDF se limita al subconjunto A04; muchos PDF reales (Type0/CID) se rechazarán hasta A08 (D-A04-04).
- La garantía de aislamiento depende del perfil configurado; en macOS no hay sandbox.
- Los límites son elecciones iniciales sin medición; la capacidad no se acredita por haberlos fijado.
- El mock no mide calidad de detección; ningún indicador de cobertura acredita protección.
- SQLite serializa escrituras y reclamaciones: apto para un nodo y baja concurrencia.
