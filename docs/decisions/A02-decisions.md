# A02 — Registro de decisiones

> **Estado: A02 en especificación. No implementada, no probada, no aceptada.** Las decisiones de producto `D-A02-nn` fueron **aprobadas por el usuario** antes de redactar el contrato; las elecciones técnicas `E-A02-nn` fueron **derivadas por criterio** al redactarlo (no son decisiones de producto y pueden revisarse). Nada de lo aquí registrado existe todavía en el código (`0.1.0`).

Este registro separa cuatro categorías. No mezclarlas.

- **Decisiones aprobadas (`D-A02-nn`):** fijadas por el usuario. Solo el usuario las cambia.
- **Elecciones técnicas derivadas (`E-A02-nn`):** completan el contrato con criterio, sin abrir otra ronda por detalles rutinarios. Adoptadas para esta especificación; revisables.
- **Diferencias con el playbook (§4)** y **backlog (§5)**: fuera de los criterios de aceptación.
- No hay propuestas pendientes (`P-nn`) ni preguntas abiertas que bloqueen el contrato (§6).

Contratos que usan estas decisiones: [feature A02](../features/A02-perfiles-categorias-y-tipos-sensibles.md), [perfiles y catálogo](../profiles-and-catalog.md), [API pública §10](../public-api.md#10-a02--perfiles-categorías-y-tipos-sensibles-especificado-no-implementado), [persistencia §7](../persistence-and-migrations.md#7-a02--modelo-persistente-y-migración-especificado-no-implementado), [seguridad §9](../security-and-isolation.md#9-a02--permisos-y-datos-de-perfiles-especificado-no-implementado), [operación §11](../installation-and-operations.md#11-a02--límites-y-observabilidad-especificado-no-implementado), [arquitectura §8](../architecture.md#8-a02--perfiles-categorías-y-tipos-sensibles-especificado-no-implementado). Decisiones heredadas: [A01-decisions](A01-decisions.md).

## 0. Historial de revisiones

| Rev. | Contenido |
|---|---|
| 1 | Primera redacción del contrato de A02: decisiones D-A02-01 a D-A02-06 (aprobadas), elecciones E-A02-01 a E-A02-20, diferencias con el playbook y backlog. Solo documentación. |

## 1. Fuente, precedencia y herencia de A01

- Referencia: `STAGE_3_PLAYBOOK.md`, revisión 6 (copia local ignorada por Git; **no se modifica ni se declara actualizada**).
- Las decisiones posteriores registradas en A01 **prevalecen** sobre la revisión 6, y las de este registro prevalecen sobre ambas donde difieran (§4).
- A01 está **cerrada y aceptada** por el usuario (commit `1b5ffab`; GitHub Actions aprobado en `main` y `feature/a01-product-foundation`, según su confirmación explícita: D-34 de A01). No se reabre.
- Decisiones de A01 que A02 **reutiliza sin cambios**:

| A01 | Uso en A02 |
|---|---|
| D-04, D-15 | PostgreSQL y SQLite reales; mismo namespace y propiedad de tablas; objetos ajenos intactos |
| D-06, D-07 | API asíncrona; Python y HTTP comparten las mismas operaciones |
| D-11 | Sin efectos al importar; el catálogo y las operaciones se importan de forma perezosa |
| D-13, D-14, D-23 | Transacción propia o `HostTransaction` con savepoints; transacciones cortas; sin sesiones globales |
| D-16, D-24, D-29 | Migración solo hacia adelante, una transacción y un bloqueo; solo el servicio independiente migra al arrancar |
| D-17, D-19, D-28 | Aislamiento por `scope_id`; alcance mínimo; el motor no conoce el dominio del scope |
| D-18, D-20, D-25, D-27 | API keys por cliente, grants inmutables, reglas de delegación; sin usuarios ni OIDC |
| D-30 | Control optimista estricto (`expected_revision`/`If-Match`) |
| D-33 | Gate de importación liviana (mediana en caliente ≤ 300 ms; ≤ 40 MiB) |

## 2. Decisiones aprobadas

| ID | Decisión (aprobada por el usuario) |
|---|---|
| **D-A02-01** | **Pertenencia.** Cada perfil pertenece a un único scope existente. Cada categoría y tipo sensible pertenece a un único perfil. No hay catálogo editable compartido entre perfiles. Reutilizar una definición supone copiar sus valores explícitamente; no hay propagación automática. No se crea una operación especial de clonación en A02. El motor no interpreta el dominio del scope. Contrato: [modelo §1](../profiles-and-catalog.md#1-conceptos-y-pertenencia-d-a02-01). |
| **D-A02-02** | **Versiones.** Se separa la configuración editable de las versiones publicadas. Una publicación explícita crea una instantánea completa, coherente e inmutable. Editar no cambia la versión publicada vigente. Las futuras ejecuciones fijan una versión publicada concreta y nunca consultan una configuración editable cambiante. La publicación valida el conjunto completo y actualiza atómicamente la referencia a la versión vigente. No se implementan ejecuciones, inferencia ni mock. Contrato: [modelo §7](../profiles-and-catalog.md#7-versiones-publicadas-d-a02-02). |
| **D-A02-03** | **Perfil predeterminado y configuración inicial.** Como máximo un perfil predeterminado por scope; se designa explícitamente y debe estar activo, no eliminado y tener una versión publicada. Puede no existir; resolverlo cuando falta devuelve un error tipado y no se elige otro silenciosamente. Crear un scope no crea perfiles ni cambia el comportamiento de A01. Crear un perfil copia el catálogo inicial de tipos sensibles versionado del paquete, salvo opción explícita de empezar vacío; las categorías temáticas empiezan vacías. Las actualizaciones del paquete nunca modifican perfiles existentes ni sus versiones. Se registran la procedencia y la versión del catálogo copiado. Contrato: [modelo §9–§10](../profiles-and-catalog.md#9-perfil-predeterminado-d-a02-03). |
| **D-A02-04** | **Desactivación y eliminación.** Desactivación reversible y eliminación lógica son operaciones distintas; ambas conservan las versiones históricas. Eliminar retira el recurso de la administración habitual; no hay purga física en A02. Desactivar o eliminar una categoría o tipo cambia la configuración editable y afecta las publicaciones posteriores, no las instantáneas ya publicadas. Desactivar o eliminar un perfil impide seleccionarlo para nuevas ejecuciones, incluso por versión explícita; sus versiones siguen consultables con permisos. Si es predeterminado, se exige reemplazarlo o quitar explícitamente su designación; nunca se deja una referencia inválida ni se elige reemplazo automáticamente. Restauración de eliminados y purga no son requisitos de A02 (backlog). El tratamiento de trabajos ya iniciados corresponde a A05; no se inventa cancelación retroactiva. Contrato: [modelo §3](../profiles-and-catalog.md#3-estados-separados-de-las-revisiones). |
| **D-A02-05** | **Ranking y resolución.** Configuración almacenada en la versión del perfil, sin overrides por ejecución. Modo inicial: ranking. Puntuaciones de pertenencia entre 0 y 1, no probabilidades calibradas ni deben sumar uno. Umbral de elegibilidad configurable, inicial 0. Tolerancia absoluta de empate configurable, inicial 0 (empate exacto). Valores finitos dentro de [0,1]; se rechazan NaN e infinitos. En resolución, todas las categorías habilitadas tienen prioridades enteras únicas; menor número implica mayor prioridad; el ranking no exige prioridades. Elegibilidad: puntuación ≥ umbral. Resolución: entre las elegibles, las que estén a distancia ≤ tolerancia del máximo; se selecciona la de mayor prioridad; sin empates transitivos encadenados. La prioridad no modifica puntuaciones ni el ranking de relevancia; un orden técnico estable entre puntuaciones iguales no es prioridad de negocio. Sin categorías elegibles o con clasificación inconclusa no se fuerza una ganadora. El consumidor puede obtener todas las categorías evaluadas (sin exclusiones silenciosas ni top-k obligatorio). Estos valores iniciales no acreditan calidad: A03 implementa el algoritmo y el contrato de resultados; A09 valida la inferencia real. Contrato: [modelo §6](../profiles-and-catalog.md#6-ranking-y-resolución-configuración-d-a02-05). |
| **D-A02-06** | **Permisos.** Se agregan `profiles:read` y `profiles:write` como permisos de scope; abarcan perfiles, sus categorías y tipos, versiones y designación predeterminada; publicar requiere `profiles:write`. Sin jerarquías implícitas entre read y write. `scope:read` y `scope:write` conservan su significado: no otorgan los nuevos permisos automáticamente. `AdminPermission` no concede acceso automático al contenido de perfiles. Se reutilizan `ScopeContext`, `ScopedOperations`, API keys y reglas de delegación de A01; no hay credenciales por perfil. Los grants existentes permanecen inmutables; para ampliar acceso HTTP hay que emitir una nueva clave conforme a las reglas vigentes. Contrato: [seguridad §9](../security-and-isolation.md#9-a02--permisos-y-datos-de-perfiles-especificado-no-implementado). |

## 3. Elecciones técnicas derivadas

Adoptadas con criterio al completar el contrato; **no son decisiones de producto**. Cada una indica el motivo y en qué documento vive.

| ID | Elección | Motivo | Contrato |
|---|---|---|---|
| E-A02-01 | `profile_id`, `category_id` y `type_id` los elige quien llama (slug `^[a-z0-9][a-z0-9_-]{0,63}$`), inmutables y no reutilizables tras eliminar; la versión tiene `version_number` (monótono por perfil) y `version_id` (`pvr_` + UUID) | Creación con clave natural como `create_scope` (sin marco de idempotencia nuevo); ids estables para el catálogo; identidad persistente de elementos dentro de las instantáneas | [modelo §2](../profiles-and-catalog.md#2-identificadores-e-a02-01) |
| E-A02-02 | Revisiones separadas: `revision` (registro del perfil), `draft_revision` (conjunto editable), `ranking_revision`, `revision` por elemento y por designación | Concurrencia específica y legible; la publicación depende solo de `draft_revision`, que no cambia por estados ni por publicar | [modelo §4](../profiles-and-catalog.md#4-revisiones-e-a02-02) |
| E-A02-03 | Definición ≤ 1500, ejemplos positivos y negativos (≤ 5 cada uno, ≤ 300), `language` del perfil `es`/`en` inmutable y traducción opcional del otro idioma; texto sintético; sin traducciones obligatorias | Guía de definición acotada y bilingüe sin forzar traducciones; negativos aclaran fronteras | [modelo §5](../profiles-and-catalog.md#5-definiciones-ejemplos-e-idioma-e-a02-03) |
| E-A02-04 | Publicable con ≥ 1 elemento `enabled`; solo detección y solo clasificación son válidas; una versión sin elementos no se publica; la instantánea lleva `capabilities` y los consumidores deben distinguir `not_configured` de «sin hallazgos» | Evitar que «no configurado» se lea como «nada encontrado» | [modelo §7.2](../profiles-and-catalog.md#72-qué-configuración-puede-publicarse-e-a02-04) |
| E-A02-05 | Publicación sin duplicados por clave natural `(perfil, expected_draft_revision)` + comparación de `content_hash` con la versión vigente + `UNIQUE (perfil, source_draft_revision)` | Mecanismo local suficiente; reintentos devuelven la misma versión; sin tabla de claves de idempotencia | [modelo §7.3](../profiles-and-catalog.md#73-publicaciones-duplicadas-e-a02-05) |
| E-A02-06 | Catálogo `cce-starter-sensitive-types` v1 con 6 tipos (`personal_data`, `government_id`, `salary_compensation`, `financial_information`, `password`, `api_key_secret`), módulo del paquete sin E/S, ejemplos sintéticos, tipos no excluyentes entre sí | Cubre los conceptos del playbook sin duplicidades; fronteras explícitas; sin prometer detección | [modelo §10](../profiles-and-catalog.md#10-catálogo-inicial-de-tipos-sensibles-d-a02-04-e-a02-06) |
| E-A02-07 | Límites: 50 perfiles/alcance, 50+50 elementos/perfil (filas totales 500/250), 500 versiones/perfil, texto por elemento ≤ 8 KiB, instantánea ≤ 1 MiB, prioridad 1–10000; cuerpo HTTP sin cambios (64 KiB) | Razonables y coherentes con los límites HTTP de A01; acotan el crecimiento sin purga | [operación §11](../installation-and-operations.md#111-límites) |
| E-A02-08 | Carreras arbitradas por la fila del perfil (`UPDATE` condicional que bloquea) con orden de bloqueo perfil → elemento/versión/designación | Una sola regla para edición, publicación, estados y predeterminado; sin interbloqueos; equivalente en SQLite por escritor único | [API §10.7](../public-api.md#107-concurrencia-publicación-e-idempotencia) |
| E-A02-09 | Transiciones de estado de perfil y elementos son de estado objetivo (sin `If-Match`, repetibles); eliminado es terminal y se comporta como inexistente salvo filtros explícitos y lectura de versiones | Coherente con `set_scope_status` de A01; deja sin ambigüedad la respuesta perdida | [modelo §3](../profiles-and-catalog.md#3-estados-separados-de-las-revisiones) |
| E-A02-10 | Un perfil `disabled` no admite edición ni publicación (`profile_disabled`); sí lectura, `enable` y `delete` | Misma política que alcance deshabilitado en A01 | [modelo §3.1](../profiles-and-catalog.md#31-perfil-status) |
| E-A02-11 | La unicidad de prioridad entre categorías `enabled` se impone siempre (índice parcial); la presencia solo en modo `resolution`, validada al cambiar el modo y al publicar | Permite intercambiar prioridades en el borrador; garantiza precedencia inequívoca al activar la resolución | [modelo §6.2](../profiles-and-catalog.md#62-validación-de-la-resolución) |
| E-A02-12 | Predeterminado: una fila por scope (`scope_profile_defaults`) con `revision`; `PUT`/`DELETE` con `If-Match`; referencia al perfil, no a una versión | Un solo predeterminado por PK incluso bajo concurrencia; la versión vigente se resuelve al consultar | [modelo §9](../profiles-and-catalog.md#9-perfil-predeterminado-d-a02-03) |
| E-A02-13 | Resolución como consulta (`GET /resolved-profile`, `resolve_profile`) con `explicit`/`default`; `version` sin `profile_id` es inválida; rechaza alcance deshabilitado y perfil `disabled`/`deleted` | Cumple «resolver sin ejecutar análisis» y «consultar ≠ usar» | [modelo §8](../profiles-and-catalog.md#8-resolver-un-perfil-y-una-versión-publicados) |
| E-A02-14 | Códigos nuevos (`profile_disabled`, `profile_not_selectable`, `profile_not_published`, `profile_is_default`, `default_profile_not_set`, `invalid_state_transition`, `name_conflict`, `priority_conflict`, `profile_invalid`); `profile_invalid` lleva `problems` solo con códigos e ids | Errores tipados sin filtrar contenido | [API §10.6](../public-api.md#106-códigos-de-error-nuevos) |
| E-A02-15 | Trazabilidad en `admin_events` (sin renombrar), con ids, acción y revisiones, sin contenido; un solo evento al crear un perfil con catálogo | Reutiliza la trazabilidad existente | [API §10.8](../public-api.md#108-eventos-de-trazabilidad) |
| E-A02-16 | Migración `0002_profiles`: solo expandir, sin migrar datos ni crear perfiles; `schema_level` 1→2; `min_runtime_level` sigue en 1; no emitir claves `profiles:*` hasta que todos los runtimes sean de A02 (un runtime A01 falla al cargar grants con permisos desconocidos) | Despliegue gradual «migrar → actualizar» sin ampliar ni reinterpretar grants | [persistencia §7.5](../persistence-and-migrations.md#75-migración-desde-a01-plan-no-escrita) |
| E-A02-17 | Las operaciones viven solo en `ScopedOperations` (no en `AdminOperations`); HTTP bajo `/v1/scopes/{scope_id}/…`; sin rutas `/v1/admin` de perfiles | `AdminPermission` no da acceso al contenido (D-A02-06) | [API §10.3–§10.4](../public-api.md#103-firmas-python-scopedoperations) |
| E-A02-18 | `PATCH`: omitido = sin cambio; `null` solo en campos anulables; listas y objetos se reemplazan completos; campos de solo lectura o desconocidos ⇒ 422 | Semántica simple, sin fusión profunda | [API §10.2](../public-api.md#102-semántica-de-edición-patch) |
| E-A02-19 | Filtros de estado mínimos: `status` (perfiles) y `state` (elementos) como colección; por defecto excluyen eliminados; `include_deleted` en lecturas individuales; sin API genérica de filtros | Alcance acotado | [API §10.3–§10.4](../public-api.md#103-firmas-python-scopedoperations) |
| E-A02-20 | `starter_catalog()` es síncrona, sin E/S ni permisos, y se importa de forma perezosa | Función computacional (D-06) que no pesa en la importación (D-11, D-33) | [API §10.3](../public-api.md#103-firmas-python-scopedoperations) |

## 4. Cambios respecto del playbook (revisión 6)

Registro local; no reemplaza al playbook ni lo modifica. La referencia son las secciones «A02», «Perfiles e integración» y «Clasificación: ranking o resolución por desempate».

| # | Playbook (rev. 6) | Decisión nueva | Ref. |
|---|---|---|---|
| 1 | A02: «credenciales por recurso solo para modo HTTP» | No hay credenciales por perfil. Se reutilizan las API keys y grants de A01 con dos permisos de alcance nuevos; ya adelantado por A01 (cambio #6 y #9 de la tabla de [A01-decisions §3](A01-decisions.md#3-cambios-respecto-de-la-revisión-6)) | D-A02-06, A01 D-18/D-27 |
| 2 | A02: «Tipos sensibles predeterminados editables sin mutar silenciosamente perfiles existentes al actualizar el software» | Catálogo versionado **copiado al crear** el perfil, con procedencia; sin tabla compartida; las actualizaciones del paquete no tocan perfiles | D-A02-03, E-A02-06 |
| 3 | Clasificación: «ubicación del ajuste (perfil y posibles overrides explícitos)» | El ajuste vive en el perfil y en cada versión; **sin overrides por ejecución** en A02 | D-A02-05 |
| 4 | Perfiles: «una instalación puede usar un perfil predeterminado o varios perfiles aislados» | A lo sumo un predeterminado **por scope**, opcional y explícito; resolverlo ausente es un error tipado | D-A02-03 |
| 5 | A02: «versiones inmutables utilizadas por ejecuciones» | Configuración editable separada de versiones publicadas; publicación explícita; las ejecuciones fijan una versión concreta | D-A02-02 |
| 6 | Integración: «materializar y sincronizar perfiles efectivos sin mantener dos copias editables contradictorias» | Una sola configuración editable por perfil, sin catálogo compartido ni propagación; la configuración efectiva por organización/proyecto es de CAWbase | D-A02-01 |
| 7 | A02: «CRUD y desactivación» | Desactivación reversible y eliminación **lógica** distintas; sin purga ni restauración en A02 | D-A02-04 |
| 8 | Ranking/resolución: umbrales, tolerancia y escala «se definen antes de implementar» | Fijados: puntuaciones [0,1], umbral inicial 0, tolerancia absoluta inicial 0, prioridades enteras únicas (menor = mayor prioridad), sin empates encadenados | D-A02-05 |
| 9 | A02: «aislamiento por alcance explícito validado por el host» | Se mantiene; implementado sobre `ScopeContext` de A01. Un `AdminContext` no accede a perfiles | D-A02-06, E-A02-17 |

Lo no listado permanece vigente (por ejemplo: A02 excluye organizaciones/proyectos y reglas de destinos; el mock y el algoritmo son de A03). La incorporación a la próxima revisión canónica del playbook la hace quien la mantiene; este repositorio no la declara.

## 5. Backlog (fuera de los criterios de aceptación)

Mejoras opcionales listadas solo para no perderlas; **no** son requisitos de A02 ni bloquean su aceptación.

| ID | Mejora |
|---|---|
| B-A02-01 | Política de purga física (perfiles, elementos y versiones eliminados; retención de contenido sensible) |
| B-A02-02 | Restauración de perfiles y elementos eliminados |
| B-A02-03 | Operación explícita de clonación/copia de definiciones entre perfiles |
| B-A02-04 | Reversión a una versión anterior (republicar una versión histórica) y comparación entre versiones |
| B-A02-05 | Sincronización asistida con versiones nuevas del catálogo inicial (diferencias y copia selectiva) |
| B-A02-06 | Reordenamiento atómico de prioridades en una sola operación |
| B-A02-07 | Importación/exportación masiva de perfiles |
| B-A02-08 | Overrides por ejecución (requiere decisión del usuario y A03/A05) |
| B-A02-09 | Notas o etiquetas en versiones; consulta de eventos de perfiles por API |
| B-A02-10 | Cambio de idioma del perfil; traducciones de ejemplos; más idiomas |
| B-A02-11 | Granularidad de lectura por perfil (credenciales o grants por perfil, hoy descartados) |
| B-A02-12 | Endurecer en el runtime la carga de grants con permisos desconocidos (compatibilidad hacia adelante) |
| B-A02-13 | Retención/archivado de versiones antiguas al acercarse al límite de 500 |

## 6. Preguntas abiertas y tensiones

No hay contradicción entre las decisiones aprobadas ni con A01 que impida el contrato.

- **Dependencias en otras features (no bloquean A02):** el tratamiento de trabajos ya iniciados (A05); el algoritmo y el contrato de resultados (A03); la validación con inferencia real (A09); la licencia y publicación (A10, Q-01 de A01).
- **Riesgo operativo señalado, no una contradicción:** un runtime de A01 falla al cargar los grants de una clave con permisos desconocidos. Se mitiga operacionalmente (no emitir claves `profiles:*` hasta actualizar todos los runtimes) y queda el endurecimiento en el backlog (B-A02-12).
