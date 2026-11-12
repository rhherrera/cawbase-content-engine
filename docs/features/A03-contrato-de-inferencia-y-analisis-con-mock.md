# A03 — Contrato de inferencia y análisis con mock

> **Estado: IMPLEMENTADA en `0.3.0` (rama `feature/a03-inference-contract-mock`, commits locales, sin push); NO aceptada por el usuario; no cerrada.** Este documento define alcance, decisiones y aceptación; el contrato completo vive en [inferencia y análisis](../inference-and-analysis.md). La trazabilidad de cada criterio a pruebas y los resultados reales por ambiente están en el [informe de implementación](../a03-implementation-report.md).

- Fuente: `STAGE_3_PLAYBOOK.md` revisión 6 (11 de noviembre de 2026), más las decisiones posteriores registradas en [A01-decisions](../decisions/A01-decisions.md), [A02-decisions](../decisions/A02-decisions.md) y [A03-decisions](../decisions/A03-decisions.md) (estas últimas **prevalecen** sobre el playbook donde difieren; ver su [tabla de diferencias](../decisions/A03-decisions.md#4-diferencias-respecto-del-playbook-revisión-6)).
- Repositorio: `cawbase-content-engine` · rama: `feature/a03-inference-contract-mock`.
- Dependencias: A01 y A02 (**cerradas y aceptadas**; A02 en `0.2.0`, commit `f4ad3e7`). Habilita A04 (contrato de documentos) y A09 (inferencia real).

## 1. Objetivo, casos de uso, exclusiones y dependencias

**Objetivo.** Estabilizar la interfaz de análisis de **texto** que usarán las features siguientes: una operación Python asíncrona (y su equivalente HTTP) que, con una **versión publicada** de un perfil, devuelve clasificación (ranking o resolución) y/o hallazgos sensibles localizados, con cobertura y estados explícitos, a través de un **adaptador de inferencia** estrecho y un **mock determinista** sobre fixtures sintéticos. **No** es un clasificador ficticio y **no** afirma calidad de detección.

**Casos de uso.**

| # | Caso | Camino |
|---|---|---|
| U1 | Un host analiza un texto con el perfil predeterminado de su scope y obtiene ranking y hallazgos | Python |
| U2 | Un cliente HTTP con `analysis:execute` (sin `profiles:read`) analiza con una versión explícita y no puede leer el perfil | HTTP |
| U3 | Un desarrollador valida su configuración de categorías/tipos propios contra fixtures sintéticos, sin modelo | Python/HTTP |
| U4 | Un consumidor distingue «no solicitada», «no configurada», «completa», «inconclusa» y «sin elegibles» | Python/HTTP |
| U5 | Una publicación o desactivación concurrente no altera un análisis ya preparado | Python |
| U6 | Un adaptador real futuro (A09) se acopla sin cambiar el contrato público | — |

**Incluye:** contratos de entrada/salida del adaptador y del análisis público; texto y contexto auxiliar; selección y fijación de versiones publicadas; clasificación, hallazgos, localizadores y cobertura; ranking y resolución de A02; resultados inconclusos y parciales; validación estricta, límites, errores, timeout y cancelación; mock determinista explícito; equivalencia Python/HTTP, aislamiento y criterios de aceptación.

**Exclusiones.** Modelo real, comparación o selección de modelos, proveedor, runtime, pesos, tokenizer, prompts concretos, ventana de contexto y protocolo remoto; segmentación y reconciliación específica de un modelo; PDF/DOCX, OCR, extracción y mapeo documental (A04); protección, redacción, seudonimización y restauración; trabajos, colas, almacenamiento de originales/resultados y retención (A05); AWS, encendido/apagado de infraestructura y credenciales de administración; integración con CAWbase (organizaciones, proyectos, membresías, destinos, políticas del consumidor); overrides de ranking, prioridades o definiciones por ejecución; calidad de inferencia real y autorización de uso productivo del mock (A09). A03 contempla **errores abstractos de disponibilidad** y la **procedencia futura** del adaptador, sin diseñar infraestructura ni elegir runtime. **El código:** esta fase es solo documental.

## 2. Decisiones (resumen)

Aprobadas por el usuario (`D-A03-nn`, detalle en [A03-decisions §2](../decisions/A03-decisions.md#2-decisiones-aprobadas)):

1. **D-A03-01 Superficie:** llamada Python asíncrona + equivalente HTTP en la misma petición; sin persistencia, trabajos ni consultas posteriores (A05).
2. **D-A03-02 Autorización:** permiso `analysis:execute`, sin exigir `profiles:read`; no concede lectura de definiciones; resultados sin definiciones.
3. **D-A03-03 Resultados parciales:** se conservan resultados verificables de respuestas válidas con cobertura incompleta o inconclusas; estados y cobertura por capacidad; clasificación inconclusa sin ganadora; salida inválida se rechaza; fallo operativo ⇒ error tipado.

Elecciones técnicas derivadas (`E-A03-01` a `E-A03-18`; **no son decisiones de producto**): ver [A03-decisions §3](../decisions/A03-decisions.md#3-elecciones-técnicas-derivadas). Principales: dos fronteras (API pública / adaptador con salida cruda validada); índices en puntos de código; cuatro estados por capacidad; cobertura por grupos con regiones canónicas; aritmética decimal exacta; fragmento solo para verificar; selección compartida con copia defensiva; preparación corta sin conexiones durante la inferencia; rechazo de `HostTransaction`; timeout del operador; mock por fixtures asociados a `definitions_digest`; sin migración.

## 3. Mapa del contrato (sin copias contradictorias)

| Tema | Documento permanente |
|---|---|
| **Contrato completo**: fronteras, entrada, texto/contexto, límites, preparación y versiones, copia defensiva, interfaz del adaptador, validación de salida, estados, cobertura, clasificación, hallazgos, mock, errores, timeout, HTTP, persistencia y ciclo de vida | [Inferencia y análisis](../inference-and-analysis.md) |
| Firma `analyze_text`, ruta HTTP, tabla de errores nuevos, resumen | [API pública §11](../public-api.md#11-a03--análisis-de-texto-implementado) |
| Permiso `analysis:execute` (11 permisos), matriz, datos sensibles, restricción de despliegue | [Seguridad §10](../security-and-isolation.md#10-a03--permiso-y-datos-del-análisis-implementado) |
| Ausencia de tablas y de migración; niveles de esquema/runtime | [Persistencia §8](../persistence-and-migrations.md#8-a03--sin-modelo-persistente-implementado) |
| Límites, configuración del adaptador, observabilidad, readiness vs. capacidad | [Operación §12](../installation-and-operations.md#12-a03--límites-observabilidad-y-ciclo-de-vida-implementado) |
| Módulos y flujos | [Arquitectura §9](../architecture.md#9-a03--inferencia-y-análisis-implementado) |
| Proceso de implementación previsto | [Desarrollo §11](../development.md#11-a03--proceso-previsto-implementado) |
| Semántica de perfiles, versiones y ranking que A03 consume | [Perfiles y catálogo](../profiles-and-catalog.md) |
| Decisiones aprobadas, elecciones derivadas, diferencias con el playbook y backlog | [A03-decisions](../decisions/A03-decisions.md) |

## 4. Relación con A01 y A02 y compatibilidad

- No se reabre A01 ni A02 ni se cambia ninguna de sus decisiones. Se reutilizan sin cambios `ScopeContext`, `ScopedOperations`, API keys y delegación, `HostTransaction`, envoltorio y jerarquía de errores, límites HTTP, ocultación 404/403 y la resolución de perfil/versión de A02 §8.
- `resolve_profile` conserva contrato, permiso (`profiles:read`) y comportamiento; solo se extrae internamente su lógica de selección.
- El catálogo de permisos pasa de **10 a 11**; los grants existentes no cambian. **No hay migración** (`schema_level = 2`, `min_runtime_level = 1`).
- **Restricción de despliegue:** no emitir claves con `analysis:execute` hasta que **todos** los runtimes que acceden a la base estén actualizados (los de A01 y A02 fallan al cargar grants desconocidos, incluso de claves revocadas); volver atrás no es transparente. No se exige el endurecimiento opcional B-A02-12.

## 5. Riesgos y límites declarados

- La calidad de inferencia **no** se acredita: el mock devuelve lo que el fixture dice; validar con mock acredita la **ingeniería del contrato** (validador, ranking, cobertura), no la capacidad de detectar o clasificar. A09 debe fijar los umbrales de precisión/recall **antes** de evaluar; el cierre de A03 **no depende** de ellos.
- Una cobertura completa no es exhaustividad; hallazgos parciales no acreditan ausencia de otros.
- Trabajo ya iniciado en un servidor remoto futuro puede continuar tras una cancelación o timeout; A03 no promete detenerlo.
- Sin persistencia: repetir una solicitud la ejecuta de nuevo; no hay reanudación ni consulta posterior.
- El límite de 64 KiB del cuerpo HTTP puede alcanzarse antes que el límite lógico de caracteres con escapes JSON.

## 6. Qué se entregó

Código del núcleo (permiso, errores, selección compartida, `analyze_text`, validador, adaptador y mock cargados de forma perezosa), ruta HTTP con detección de desconexión, composición del mock en el servicio independiente, pruebas (SQLite y PostgreSQL reales), ejemplo ejecutable (`examples/analysis_host.py`), documentación alineada e informe, sin dependencias nuevas ni migración. Versión del paquete: `0.3.0`. No se hizo push ni se publicó nada.

<a id="matriz-aceptacion-a03"></a>
## 7. Matriz de aceptación (para la implementación posterior)

Casos verificables; las pruebas que los cubren y sus resultados reales por ambiente están en el [informe](../a03-implementation-report.md#matriz-ac-a03-trazable). «PG» = PostgreSQL real, «SQ» = SQLite real, «—» = independiente del backend. Las pruebas de PostgreSQL usan solo servidores/bases descartables (`cce_t_*`). Las pruebas de esta matriz se hacen con el **mock** y con adaptadores de prueba que devuelven salidas crudas arbitrarias; no miden calidad real.

### 7.1 Python/HTTP, permisos y aislamiento

| ID | Criterio | Backend |
|---|---|---|
| AC-A03-01 | Misma entrada ⇒ mismo resultado semántico por Python y por HTTP (misma lógica; `request_id` aparte); recorrido completo sin CAWbase ni modelo | PG, SQ |
| AC-A03-02 | `analysis:execute` **sin** `profiles:read` ejecuta análisis (predeterminado, explícito, versión); la lectura de perfiles/versiones/`resolve_profile` con esa clave sigue denegada (403) | PG, SQ |
| AC-A03-03 | `profiles:read`, `profiles:write`, `scope:read/write` y `admin:*` **no** conceden análisis (403/404 según la regla vigente); `AdminContext` no analiza; sin jerarquías | PG, SQ |
| AC-A03-04 | Delegación (D-27) con `analysis:execute`: subconjunto del emisor; claves anteriores no lo ganan; grants preexistentes sin cambios; el catálogo tiene 11 permisos | PG, SQ |
| AC-A03-05 | Acceso cruzado: scope ajeno ⇒ 404 indistinguible de inexistente; perfil/versión ajenos ⇒ `resource_not_found`; clave sin grants en el scope ⇒ 404; sin clave ⇒ 401 | PG, SQ |
| AC-A03-06 | Orden de comprobaciones: autorización antes que validación, validación antes que acceso a datos, y orden HTTP (413 → 401 → 404 → 422 → operación) sin filtrar existencia | PG, SQ |

### 7.2 Selección, versiones y transacciones

| ID | Criterio | Backend |
|---|---|---|
| AC-A03-10 | Sin perfil ⇒ predeterminado (`selection = default`; sin predeterminado ⇒ `default_profile_not_set`); `profile_id` ⇒ vigente; versión explícita ⇒ esa; `version_number` sin `profile_id` ⇒ 422; sin publicación ⇒ `profile_not_published`; inexistentes ⇒ `resource_not_found` | PG, SQ |
| AC-A03-11 | Scope deshabilitado ⇒ `scope_disabled`; perfil `disabled`/`deleted` ⇒ `profile_not_selectable` **también con versión explícita**; una versión histórica consultable no es ejecutable en esos casos | PG, SQ |
| AC-A03-12 | El resultado registra `scope_id`, `profile_id`, `version_id`, `version_number`, `content_hash` correctos y **no** contiene snapshot, definiciones, ejemplos, texto, contexto ni fragmentos | PG, SQ |
| AC-A03-13 | Versión fijada frente a cambios concurrentes: publicar, desactivar, eliminar el perfil o deshabilitar el scope **después** de la preparación (adaptador de prueba bloqueado) no cambia el análisis en curso; los análisis nuevos sí ven el cambio | PG, SQ |
| AC-A03-14 | Ninguna transacción ni conexión del motor permanece abierta durante `infer` (verificable: el pool tiene todas sus conexiones disponibles; en SQLite una escritura concurrente no se bloquea mientras el adaptador espera) | PG, SQ |
| AC-A03-15 | `HostTransaction`: `analyze_text` ⇒ `transaction_incompatible` **antes** de invocar al adaptador (contador de llamadas = 0); la transacción del host queda sin alterar y utilizable; el motor no la confirma ni la revierte | PG, SQ |
| AC-A03-16 | Copia defensiva: mutar el `dict` de `ProfileVersion.snapshot` leído por el llamador, o mutar la solicitud/ salida del adaptador tras recibirla/devolverla, no cambia los datos preparados, la validación ni el resultado | PG, SQ |
| AC-A03-17 | La lógica de selección compartida no abre una vía pública: `resolve_profile` sigue exigiendo `profiles:read` y sin él no hay forma de obtener una versión; regresión de A02 intacta | PG, SQ |

### 7.3 Clasificación, ranking y resolución

| ID | Criterio | Backend |
|---|---|---|
| AC-A03-20 | Puntuaciones finitas en [0, 1]; se rechazan booleanos, NaN, ±∞, cadenas, valores fuera de rango y **enteros enormes de cualquier signo sin desbordar** (`inference_output_invalid` 502, nunca 500) | — |
| AC-A03-21 | Elegibilidad `score ≥ umbral` con bordes (`==` umbral elegible; un paso por debajo no; umbral 0 y 1; puntuaciones 0 y 1; números extremos); comparación racional exacta independiente del contexto decimal del host | — |
| AC-A03-22 | Ranking descendente, empates con el mismo `rank`, desempate técnico por `category_id`; sin top-k ni exclusiones; no elegibles presentes con `eligible = false`; **sin ganadora implícita** en modo `ranking` (`resolution: null`) | — |
| AC-A03-23 | Resolución: candidatas = elegibles con `max − score ≤ tolerancia` (borde `==` incluido, p. ej. 0.9/0.7/0.2; un paso por encima excluido), sin empates transitivos; gana la menor prioridad; prioridades no alteran puntuaciones ni ranking; motivos `highest_score` / `priority_tiebreak` | — |
| AC-A03-24 | Sin elegibles ⇒ `no_eligible_category`, `selected_category_id = null`; clasificación inconclusa ⇒ `classification_inconclusive`, sin ganadora aunque existan puntuaciones; los cuatro casos («sin elegibles», «inconclusa», «no solicitada», «no configurada») son distinguibles | — |
| AC-A03-25 | Una clasificación completa cubre **todas** las categorías habilitadas, sin duplicados ni ids desconocidos; en una parcial, las ausentes van a `not_evaluated_ids` y nunca con puntuación inventada | — |
| AC-A03-26 | Ranking/resolución usan la configuración **de la versión fijada** (no la editable): editar el borrador tras publicar no cambia el resultado; el adaptador no recibe prioridades ni configuración de ranking | PG, SQ |

### 7.4 Hallazgos, localizadores y cobertura

| ID | Criterio | Backend |
|---|---|---|
| AC-A03-30 | Localizadores `[start, end)` en puntos de código: emoji (1), combinantes, `\r\n`, ZWJ, repeticiones; `0 ≤ start < end ≤ len`; el fragmento debe coincidir textualmente; sin búsqueda aproximada ni reparación; un desplazamiento hacia texto distinto ⇒ `fragment_mismatch`; un desplazamiento hacia otra ocurrencia idéntica **puede pasar** (la localización semántica es de A09) | — |
| AC-A03-31 | Superposiciones y distintos tipos sobre el mismo fragmento válidos; duplicado exacto `(type_id, start, end)` ⇒ salida inválida; orden `(start, end, type_id)` estable | — |
| AC-A03-32 | El fragmento no aparece en el resultado público; el contexto auxiliar no admite hallazgos (offsets solo del texto); ningún campo de página/bounding box/run | — |
| AC-A03-33 | Cobertura por grupos: regiones canónicas aceptadas, no canónicas/fuera de rango/vacías/solapadas rechazadas; `not_evaluated_ids` y `complete` derivados por el núcleo; un id en dos grupos rechazado | — |
| AC-A03-34 | Hallazgos solo dentro de las regiones de **su** tipo (también tipo no evaluado ⇒ `finding_outside_coverage`) | — |
| AC-A03-35 | `completed` exige cobertura completa; `partial_coverage` exige incompleta; ausencia de hallazgos con cobertura incompleta **no** se presenta como negativo completo; una capacidad `completed` y la otra `inconclusive` coexisten; el estado global se deriva por regla y no sustituye los de capacidad | — |
| AC-A03-36 | Estados: `not_requested` prevalece sobre `not_configured`; ambos sin `ranking`/`findings`/listas vacías; `capabilities` vacía/duplicada/desconocida ⇒ 422; ninguna ejecutable ⇒ `capabilities_not_configured` sin invocar al adaptador | PG, SQ |

### 7.5 Validación de salida, límites y errores

| ID | Criterio | Backend |
|---|---|---|
| AC-A03-40 | Salidas malformadas (no mapa, no serializable, campos desconocidos/faltantes, tipos, versión de contrato, capacidad faltante o no ejecutada, ids desconocidos/duplicados, offsets, puntuaciones, contradicciones estado/cobertura/razón) ⇒ `inference_output_invalid` (502) con `problems` solo de códigos; ninguna se transforma en resultado parcial | — |
| AC-A03-41 | Límites sin truncamiento silencioso: texto/contexto (caracteres y bytes), hallazgos, longitud de hallazgo, regiones, bytes de salida; entrada ⇒ `limit_exceeded`, salida ⇒ `inference_output_invalid`; cuerpo > 64 KiB ⇒ `limit_exceeded` con HTTP 413 (código vigente); los límites de otras rutas no cambian | PG, SQ |
| AC-A03-42 | Texto vacío ⇒ 422; solo espacios válido; sustitutos aislados (Python y `\ud800` en JSON) ⇒ 422; el texto no se normaliza ni recorta (el resultado y los offsets lo confirman); contexto vacío ⇒ 422 | PG, SQ |
| AC-A03-43 | Timeout (`hang`) ⇒ `inference_timeout` (504, retryable); apagado/cargando/inalcanzable/fallo recuperable ⇒ `inference_unavailable` (503, retryable, `reason`); sin reintentos automáticos (contador de llamadas = 1), sin fallback, sin esperar readiness | PG, SQ |
| AC-A03-44 | Cancelación del llamador: `CancelledError` se propaga, el adaptador recibe la cancelación, no hay resultado ni se convierte en éxito/timeout; HTTP: desconexión del cliente tampoco produce éxito | PG, SQ |
| AC-A03-45 | Tabla de errores completa (código, excepción, HTTP, retryable, adaptador invocado sí/no) verificada por caso; 422 de entrada distinto de 502 de salida del adaptador; excepción inesperada del adaptador ⇒ `internal_error`; **frontera de excepciones**: solo el protocolo cerrado cruza (reconstruido con mensajes genéricos y detalles validados); cualquier otro `EngineError` lanzado por el adaptador (p. ej. `ValidationFailedError`) ⇒ `internal_error` 500 sin filtrar su mensaje, por Python y HTTP | PG, SQ |

### 7.6 Mock, observabilidad, persistencia y entrega

| ID | Criterio | Backend |
|---|---|---|
| AC-A03-50 | El mock solo existe si el host/operador lo compone (con `acknowledge_simulated`); ningún parámetro de solicitud lo activa; sin adaptador ⇒ `inference_not_configured`; si un adaptador falla, **no hay fallback** al mock | PG, SQ |
| AC-A03-51 | Toda respuesta exitosa lleva procedencia `mock`/`simulated = true`/`model = null`, derivada de la configuración confiable (no de la salida del adaptador ni del cliente) | PG, SQ |
| AC-A03-52 | Fixture inexistente ⇒ `inference_fixture_not_found` (nunca respuesta vacía exitosa); fixture incompatible por cambio de definiciones (otro `definitions_digest`) no se reutiliza; cambiar prioridades/umbral/tolerancia **sí** reutiliza el fixture y recalcula; claves duplicadas ⇒ `configuration_invalid` | PG, SQ |
| AC-A03-53 | Los 16 escenarios mínimos M1–M16 del [contrato §7.3](../inference-and-analysis.md#73-escenarios-mínimos) pasan, incluidos fixtures con categorías y tipos **personalizados** (no acoplados al catálogo inicial) | PG, SQ |
| AC-A03-54 | Determinismo: mismo fixture + entrada ⇒ mismo resultado semántico (excluidos `request_id` y tiempos); no se expone API pública para inyectar respuestas; las salidas inválidas de fixtures de prueba solo producen errores | PG, SQ |
| AC-A03-55 | **Sin contenido en observabilidad**: marcadores únicos en texto, contexto, fragmentos, definiciones y salida cruda no aparecen en logs, errores, diagnósticos ni `admin_events`; `problems` solo con códigos | PG, SQ |
| AC-A03-56 | **Sin persistencia accidental**: tras analizar, las tablas del esquema (y `admin_events`) no contienen texto, contexto ni resultados; el recuento de filas de negocio no cambia | PG, SQ |
| AC-A03-57 | Sin migración: `schema_level = 2`, `min_runtime_level = 1`, cabeza `0002`; sin tablas nuevas; objetos ajenos intactos | PG, SQ |
| AC-A03-58 | Importación liviana: `import cawbase_content_engine` no importa el módulo de inferencia, el validador ni el mock; D-33 se mantiene; el núcleo no depende de FastAPI ni de Alembic; el motor sirve A01/A02 sin inferencia configurada; `readiness()` no cambia y `inference_status()` distingue la capacidad | — |
| AC-A03-59 | Regresión de contratos A01/A02 (suites completas, `-m dist`); compatibilidad condicionada: una clave con `analysis:execute` hace fallar a un cargador de grants que no conoce el permiso (reproducción como en `tests/test_a02_migration.py::TestCompatibilityWithA01`), y se documenta | PG, SQ |
| AC-A03-60 | Distinción documentada entre validación con mock en A03 y calidad real (A09); no se fijan umbrales de precisión/recall; informe y acta del usuario como únicos cierres | — |

## 8. Evidencia requerida para la aceptación

El [informe de A03](../a03-implementation-report.md) distingue **implementado / probado en macOS / probado en Linux local / probado en GitHub Actions / aceptado**, con resultados reales por criterio y por commit y lo no ejecutado dicho explícitamente. Un *skip* no es una aprobación; una corrida local no es una ejecución de Actions; el acta es del usuario.

## 9. Backlog

Mejoras opcionales fuera de los criterios: [A03-decisions §5](../decisions/A03-decisions.md#5-backlog-fuera-de-los-criterios-de-aceptación).
