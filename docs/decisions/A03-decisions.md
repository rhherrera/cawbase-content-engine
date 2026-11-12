# A03 — Registro de decisiones

> **Estado: A03 ESPECIFICADA (solo documentación, rama `feature/a03-inference-contract-mock`); no implementada, no probada, no aceptada.** Las decisiones de producto `D-A03-nn` fueron **aprobadas por el usuario**; las elecciones técnicas `E-A03-nn` se derivaron por criterio al redactar el contrato (no son decisiones de producto y pueden revisarse). No hay código, migración, dependencias ni cambio de versión (`0.2.0`).

Este registro separa categorías; no mezclarlas.

- **Decisiones aprobadas (`D-A03-nn`):** fijadas por el usuario. Solo el usuario las cambia.
- **Elecciones técnicas derivadas (`E-A03-nn`):** completan el contrato con criterio, sin abrir otra ronda por detalles rutinarios.
- **Diferencias con el playbook (§4)** y **backlog (§5)**: fuera de los criterios de aceptación.
- No hay propuestas pendientes (`P-nn`) ni preguntas abiertas que bloqueen el contrato (§6).

Contratos que usan estas decisiones: [feature A03](../features/A03-contrato-de-inferencia-y-analisis-con-mock.md), [inferencia y análisis](../inference-and-analysis.md) (contrato completo), [API §11](../public-api.md#11-a03--análisis-de-texto-especificado), [seguridad §10](../security-and-isolation.md#10-a03--permiso-y-datos-del-análisis-especificado), [persistencia §8](../persistence-and-migrations.md#8-a03--sin-modelo-persistente-especificado), [operación §12](../installation-and-operations.md#12-a03--límites-observabilidad-y-ciclo-de-vida-especificado), [arquitectura §9](../architecture.md#9-a03--inferencia-y-análisis-especificado). Decisiones heredadas: [A01](A01-decisions.md), [A02](A02-decisions.md).

## 0. Historial de revisiones

| Rev. | Contenido |
|---|---|
| 1 | Primera redacción del contrato de A03: D-A03-01 a D-A03-03 (aprobadas), E-A03-01 a E-A03-18, diferencias con el playbook y backlog. Solo documentación. |

## 1. Fuente, precedencia y herencia

- Referencia: `STAGE_3_PLAYBOOK.md`, revisión 6 (copia local ignorada por Git; **no se modifica ni se declara actualizada**). Las decisiones posteriores (A01, A02 y esta) prevalecen sobre ella donde difieren (§4).
- A01 y A02 están **cerradas y aceptadas** por el usuario (A01: `1b5ffab`; A02: `0.2.0`, commit `f4ad3e7`, GitHub Actions pasó sobre ese commit según su confirmación). No se reabren ni se cambia ninguna de sus decisiones.
- Decisiones heredadas que A03 **reutiliza sin cambios**:

| Origen | Uso en A03 |
|---|---|
| A01 D-06, D-07 | API asíncrona; Python y HTTP comparten las mismas operaciones |
| A01 D-11, D-33 | Sin efectos al importar; importación liviana |
| A01 D-13, D-14, D-23 | Transacción propia o `HostTransaction` con savepoints; transacciones cortas |
| A01 D-17, D-19, D-27 | Aislamiento por `scope_id`; alcance mínimo; delegación de permisos |
| A01 D-18, D-20, D-25 | API keys por cliente y grants inmutables |
| A02 D-A02-02, D-A02-03, D-A02-04 | Versiones publicadas, predeterminado y estados seleccionables; resolución de A02 §8 |
| A02 D-A02-05 | Ranking y resolución (puntuaciones [0,1], umbral, tolerancia, prioridades) **sin cambios** |
| A02 D-A02-06 | Permisos por scope sin jerarquías ni ampliación implícita |
| A02 E-A02-04 | `capabilities` de la instantánea; «no configurado ≠ sin hallazgos» |

## 2. Decisiones aprobadas

| ID | Decisión (aprobada por el usuario) |
|---|---|
| **D-A03-01** | **Superficie.** Análisis de texto mediante una llamada Python asíncrona y su equivalente HTTP. La respuesta contiene el resultado de esa llamada. No se persisten entradas ni resultados, no se crean trabajos ni se ofrecen consultas posteriores. Trabajos, archivos y su ciclo de vida pertenecen a A05. «Asíncrona» describe la API Python: el endpoint HTTP devuelve el resultado en la misma petición, con límites y timeout; no significa 202, polling, cola ni trabajo duradero. Contrato: [inferencia y análisis §2, §9, §10.1](../inference-and-analysis.md). |
| **D-A03-02** | **Autorización.** Nuevo permiso de scope `analysis:execute`, suficiente para ejecutar análisis con un perfil publicado sin exigir `profiles:read`. No concede lectura de definiciones, ejemplos ni snapshots. La operación resuelve internamente una versión aplicando las reglas existentes. Los resultados no devuelven las definiciones del perfil. `profiles:read`, `profiles:write`, `scope:*` y `admin:*` no conceden análisis implícitamente; `profiles:read` conserva su significado y operaciones. Se reutilizan `ScopeContext`, grants, API keys y delegación. Contrato: [inferencia y análisis §2.5, §3.2](../inference-and-analysis.md), [seguridad §10](../security-and-isolation.md#10-a03--permiso-y-datos-del-análisis-especificado). |
| **D-A03-03** | **Resultados parciales.** Se conservan los resultados verificables de una respuesta estructural y semánticamente válida aunque la cobertura sea incompleta o una capacidad resulte inconclusa. Estado y cobertura explícitos por capacidad. Clasificación inconclusa: nunca hay categoría ganadora. Una salida malformada o inválida se rechaza; no se transforma en un resultado parcial. Un fallo operativo sin respuesta válida produce error tipado, no un resultado inventado. Tener hallazgos parciales no acredita ausencia de otros. Cobertura completa significa procesamiento completo del alcance declarado, no calidad ni exhaustividad de detección. Contrato: [inferencia y análisis §5, §6.5](../inference-and-analysis.md). |

## 3. Elecciones técnicas derivadas

Adoptadas con criterio al completar el contrato; **no son decisiones de producto** y son revisables.

| ID | Elección | Motivo | Contrato |
|---|---|---|---|
| E-A03-01 | `analysis_contract_version = 1` e `inference_contract_version = 1`; procedencia (`kind` mock/model, `simulated`, `adapter_id`, `adapter_version`, `configuration_id`, `model`) derivada del descriptor confiable del adaptador compuesto por el host, nunca de la salida ni del cliente; el mock tiene `model = null` | Versionar ambas fronteras por separado; no inventar modelo ni pesos; no confiar en metadatos declarados por el adaptador | [§1.1](../inference-and-analysis.md#11-versionado-del-contrato-y-procedencia-e-a03-01) |
| E-A03-02 | Dos fronteras: API pública autorizada y adaptador sin acceso a tablas, perfiles, permisos, prioridades ni configuración de ranking; el adaptador devuelve una salida **cruda** JSON-compatible (mapa) que el núcleo valida y de la que construye el resultado | Mínimo privilegio; el mismo formato sirve a un adaptador remoto futuro y a fixtures inválidos de pruebas; el ranking/resolución quedan en el núcleo | [§1, §4](../inference-and-analysis.md#1-fronteras-y-responsabilidades) |
| E-A03-03 | Texto exacto sin normalización; vacío inválido, solo espacios válido; sustitutos aislados inválidos; todos los índices en puntos de código Unicode | Unidad inequívoca y estable entre Python y JSON; sin alterar el contenido analizado | [§2.3](../inference-and-analysis.md#23-el-texto-objetivo-e-a03-03) |
| E-A03-04 | Capacidades `classification`/`detection`, predeterminado ambas, lista vacía/duplicada/desconocida inválida; cuatro estados (`not_requested`, `not_configured`, `completed`, `inconclusive`), `not_requested` prevalece, estados no ejecutados sin listas vacías; ninguna ejecutable ⇒ `capabilities_not_configured` (409) | Evitar que una capacidad no ejecutada se lea como «evaluado sin hallazgos» (E-A02-04); sin resultado vacío exitoso | [§6.2](../inference-and-analysis.md#62-estados-por-capacidad-e-a03-04) |
| E-A03-05 | Contexto auxiliar: dato no confiable, no cubierto, sin hallazgos localizados, sin overrides; entregado rotulado como `auxiliary_context`; vacío inválido | Separar función del contexto de la del objetivo; evitar inyección de políticas | [§2.4](../inference-and-analysis.md#24-el-contexto-auxiliar-e-a03-05) |
| E-A03-06 | Límites iniciales: texto 8192 puntos de código / 24 576 B; contexto 2048 / 6144 B; 500 hallazgos; hallazgo ≤ 2048; 256 regiones; salida cruda ≤ 1 MiB; cuerpo HTTP **sin cambios** (64 KiB); sin truncamiento; tres unidades (puntos de código, bytes, tokens futuros) | Coherentes con el límite HTTP de A01 y los de A02; no son capacidad medida de un modelo; no amplían otras rutas | [§2.6](../inference-and-analysis.md#26-límites-iniciales-e-a03-06) |
| E-A03-07 | Orden: autorización → rechazo de `HostTransaction` → validación → inferencia configurada → preparación → capacidades ejecutables → adaptador → validación → resultado; la preparación es una lectura propia corta que termina (conexión devuelta) antes del adaptador | No retener conexiones durante la inferencia; autorizar antes de revelar nada | [§3.1–§3.3](../inference-and-analysis.md#31-orden-de-operaciones-e-a03-07) |
| E-A03-08 | Función interna de selección compartida con `resolve_profile`, sin permisos, no pública; cada fachada comprueba su propio permiso; copia defensiva: `PreparedAnalysis` inmutable, `InferenceRequest` como copia independiente, validación contra `PreparedAnalysis` | `analysis:execute` sin `profiles:read` sin abrir una vía que salte permisos; `ProfileVersion.snapshot` sigue siendo un `dict` mutable local y no se cambia el contrato de A02 | [§3.2, §3.4](../inference-and-analysis.md#34-aislamiento-de-datos-preparados-copia-defensiva-e-a03-08) |
| E-A03-09 | Cobertura por **grupos** `{ids, regions}` con regiones canónicas (ordenadas, disjuntas, no adyacentes; no se normalizan), cada id en a lo sumo un grupo, `not_evaluated_ids` y `complete` derivados por el núcleo; sin porcentajes agregados | Permite cobertura distinta por elemento sin ambigüedad; la cobertura común conservadora es el caso de un grupo; verificable | [§4.3, §6.5](../inference-and-analysis.md#43-cobertura-en-la-salida-e-a03-09) |
| E-A03-10 | Validación estricta con códigos de problema cerrados (`inference_output_invalid`, 502, sin rescate parcial); `completed` exige cobertura completa; `partial_coverage` exige incompleta; puntuaciones ⇔ ids evaluados | La salida del adaptador no es de confianza, ni la del mock; separa «parcial válido» de «inválido» (D-A03-03) | [§5](../inference-and-analysis.md#5-validación-estricta-de-la-salida-e-a03-10) |
| E-A03-11 | Comparaciones numéricas en aritmética decimal exacta sobre la representación más corta de cada número (`repr`), sin épsilon ni redondeo previo; ranking con `rank` de competición, desempate técnico por `category_id`; motivos de resolución `highest_score`, `priority_tiebreak`, `no_eligible_category`, `classification_inconclusive` | Evita que `0.9 − 0.7 > 0.2` por binario; sin tolerancias ocultas; aplica D-A02-05 sin cambios | [§6.3](../inference-and-analysis.md#63-clasificación) |
| E-A03-12 | Adaptador compuesto por el host (`ContentEngine(config, inference=InferenceSetup(adapter))`), propiedad del host, reentrante; sin inferencia el motor funciona para A01/A02; `readiness()`/`/readyz` no cambian y `inference_status()` es aparte; ruta HTTP siempre registrada (503 si no hay inferencia); el servicio independiente habilita el mock solo por configuración del operador (`…INFERENCE_ADAPTER=mock` + `…INFERENCE_MOCK_FIXTURES`) | Distinguir disponibilidad del motor y de la capacidad; sin fallback; sin efectos al importar | [§4, §7.1, §10.2](../inference-and-analysis.md#10-persistencia-ciclo-de-vida-y-arquitectura) |
| E-A03-13 | El fragmento del hallazgo existe solo en la salida del adaptador (verificación) y se descarta; el resultado público devuelve solo ubicaciones; sin búsqueda aproximada ni reparación de offsets; duplicado exacto `(type_id, start, end)` es salida inválida; orden `(start, end, type_id)`; sin explicaciones libres | No duplicar contenido sensible en respuestas/cachés/registros; localizadores verificables | [§6.4](../inference-and-analysis.md#64-detección-y-localizadores) |
| E-A03-14 | El permiso nuevo se emite solo tras actualizar **todos** los runtimes que acceden a la base (A01 y A02 fallan al cargar grants desconocidos, incluso de claves revocadas); sin rollback transparente; no se exige el endurecimiento B-A02-12 | Mismo riesgo operativo ya registrado en I-A02-02; no ampliar el alcance | [seguridad §10.3](../security-and-isolation.md#103-compatibilidad-y-despliegue-del-permiso-nuevo) |
| E-A03-15 | Timeout finito del operador (`InferenceSetup.timeout_s`, predeterminado 30 s, máx. 600 s), sin parámetro por solicitud; cancelación cooperativa que nunca produce éxito; sin reintentos, sin esperar readiness, sin encender infraestructura | Equivalencia Python/HTTP; honestidad sobre trabajo ya iniciado en un servidor remoto | [§8.3](../inference-and-analysis.md#83-timeout-y-cancelación-e-a03-15) |
| E-A03-16 | Ruta `POST /v1/scopes/{scope_id}/analyze-text`; 200 para `completed` e `inconclusive`; `Cache-Control: no-store` solo en esta ruta; `request_id` solo para correlación | Reutiliza el núcleo; sin recursos persistentes; no cambia otras rutas | [§9](../inference-and-analysis.md#9-contrato-http-propuesto--no-disponible) |
| E-A03-17 | Códigos nuevos: `inference_not_configured` (503), `capabilities_not_configured` (409), `inference_unavailable` (503, retryable, con `reason`), `inference_timeout` (504, retryable), `inference_output_invalid` (502, `problems` solo con códigos), `inference_fixture_not_found` (501); se reutiliza `transaction_incompatible` para `HostTransaction` y `internal_error` para fallos inesperados del adaptador | 422 queda para entrada del llamador; 5xx distintos para fallos del adaptador; sin filtrar contenido | [§8.1](../inference-and-analysis.md#81-tabla-de-errores-se-reutilizan-jerarquía-engineerror-y-envoltorio) |
| E-A03-18 | Sin migración ni cambio de niveles: `schema_level = 2`, `min_runtime_level = 1`; sin tablas de análisis; sin eventos en `admin_events`; sin extra de dependencias | A03 no persiste; el permiso es una cadena en una columna `TEXT` | [§10.1](../inference-and-analysis.md#101-sin-persistencia-d-a03-01) |

## 4. Diferencias respecto del playbook (revisión 6)

Registro local; no reemplaza al playbook ni lo modifica. Referencia: sección «A03», «Perfiles e integración» y «Clasificación: ranking o resolución por desempate».

| # | Playbook (rev. 6) | Decisión / delimitación | Ref. |
|---|---|---|---|
| 1 | A03: «Aceptación: contrato Python y flujo de validación probados con fixtures» | Además del contrato Python, se especifica su **equivalente HTTP** en la misma petición, sin persistencia ni trabajos | D-A03-01 |
| 2 | A03: ejecución de análisis sin permiso definido | Permiso de scope nuevo `analysis:execute` independiente de `profiles:read` | D-A03-02 |
| 3 | A03: «tratamiento de salidas truncadas o malformadas» | Malformada/inválida ⇒ rechazo; válida con procesamiento parcial/abstención/truncamiento semántico ⇒ resultado parcial con estados y cobertura explícitos por capacidad | D-A03-03 |
| 4 | Ranking: «escala/método y versión de configuración» | `scale` (`[0,1]`, no calibrada) y modo/umbral/tolerancia de la versión fijada, más la identidad de la versión, en el resultado | D-A02-05, E-A03-11 |
| 5 | Pendientes: «Instancia/runtime de inferencia, disparador de encendido/apagado, inactividad, readiness y espera: Diseño A03» | **A03 solo contempla errores abstractos de disponibilidad y la procedencia futura del adaptador**; no diseña infraestructura ni selecciona runtime. Disparador, inactividad, readiness del servidor y espera quedan para A09 (medición/decisión) y A10 (entrega) | §1.1 y §11 del contrato |
| 6 | A03: «contratos de entrada/salida del adaptador… texto y contexto» | El contexto es auxiliar, no confiable y no cubierto; no admite hallazgos localizados | E-A03-05 |
| 7 | Mock: «simular timeout, servidor apagado/cargando y fallo recuperable» | Se simulan con fixtures de resultado `hang`/`unavailable`; sin API para inyectar respuestas | E-A03-12 |
| 8 | Pendientes: «Umbrales de calidad…: criterios de producto desde A03; fijar antes de evaluación real en A09» | A03 **no fija** umbrales de precisión/recall; **A09 debe fijarlos antes de evaluar**; el cierre de A03 no depende de ellos | §5 backlog |
| 9 | Clasificación: «La selección y sus motivos no borran hallazgos ni categorías alternativas» | Se conserva: ranking completo de las evaluadas y todos los hallazgos, con la resolución como campo adicional | E-A03-11 |

Lo no listado permanece vigente. La incorporación a la próxima revisión canónica del playbook la hace quien la mantiene; este repositorio no la declara.

## 5. Backlog (fuera de los criterios de aceptación)

Mejoras opcionales listadas solo para no perderlas; **no** son requisitos de A03 ni bloquean su aceptación.

| ID | Mejora |
|---|---|
| B-A03-01 | Exponer el estado de inferencia por HTTP o en el diagnóstico autenticado |
| B-A03-02 | Timeout por solicitud (acotado por el operador) |
| B-A03-03 | Límite de concurrencia de análisis por motor o por scope |
| B-A03-04 | Análisis por lotes y/o por flujo (streaming) |
| B-A03-05 | Persistencia de resultados y consulta posterior (A05) |
| B-A03-06 | Idempotencia duradera de análisis |
| B-A03-07 | Reglas de fusión/sustitución de superposiciones al proteger (feature de protección) |
| B-A03-08 | Límite de tokens y segmentación por modelo (A09) |
| B-A03-09 | Herramienta de validación de archivos de fixtures y generador desde CLI |
| B-A03-10 | Endurecer la carga de grants desconocidos en el runtime (relacionado con B-A02-12) |
| B-A03-11 | Overrides por ejecución (B-A02-08; requiere decisión del usuario) |
| B-A03-12 | Umbrales de calidad de inferencia (precisión/recall por idioma, tipo y categoría): **los fija A09 antes de evaluar** |

## 6. Preguntas abiertas y tensiones

No hay contradicción entre las decisiones aprobadas, ni con A01/A02, que impida el contrato.

- **Dependencias en otras features (no bloquean A03):** el tratamiento de trabajos ya iniciados (A05); mapeo documental (A04); fusión de superposiciones al proteger (feature de protección); modelo real, runtime, umbrales de calidad (A09); entrega y encendido/apagado de infraestructura (A10).
- **Riesgo operativo señalado, no una contradicción:** los runtimes anteriores fallan al cargar grants con permisos desconocidos (también de claves revocadas). Se mitiga operacionalmente (E-A03-14).
