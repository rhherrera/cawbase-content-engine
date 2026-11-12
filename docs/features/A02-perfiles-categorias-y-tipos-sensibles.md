# A02 — Perfiles, categorías y tipos sensibles

> **Estado vigente: A02 cerrada y aceptada por el usuario en `0.2.0` (commit aceptado `f4ad3e7`; GitHub Actions pasó sobre ese commit, según la confirmación explícita del usuario; el merge `e149146` del PR #1 en `main` es posterior y no es el commit aceptado).** *Histórico al redactar:* implementada en la rama `feature/a02-profiles-categories-sensitive-types` con commits locales sin push, probada localmente, GitHub Actions no ejecutado y sin aceptación. Este documento define alcance, decisiones y aceptación; los contratos detallados viven en los documentos permanentes enlazados. La trazabilidad de cada criterio a pruebas y los resultados reales por ambiente están en el [informe de implementación](../a02-implementation-report.md).

- Fuente: `STAGE_3_PLAYBOOK.md` revisión 6 (11 de noviembre de 2026), más las decisiones posteriores del usuario registradas en [A01-decisions](../decisions/A01-decisions.md) y [A02-decisions](../decisions/A02-decisions.md) (estas últimas **prevalecen** sobre el playbook donde difieren; ver su [tabla de diferencias](../decisions/A02-decisions.md#4-cambios-respecto-del-playbook-revisión-6)).
- Repositorio: `cawbase-content-engine` · rama: `feature/a02-profiles-categories-sensitive-types`.
- Dependencias: A01 (**cerrada y aceptada**, commit `1b5ffab`). Habilita A03 (contrato de inferencia y mock).

## 1. Objetivo, casos de uso, exclusiones y dependencias

**Objetivo.** Permitir configurar, versionar y consultar todo lo que un análisis necesitará —perfiles, categorías, tipos sensibles y modo de ranking/resolución— mediante la API Python pública, reutilizable por el adaptador HTTP, **sin CAWbase, sin modelo y sin mock**, reutilizando el aislamiento por alcance y las API keys de A01.

**Casos de uso.**

| # | Caso | Camino |
|---|---|---|
| U1 | Un host crea un perfil en un alcance, ajusta categorías y tipos y publica una versión | Python |
| U2 | Un host prepara un perfil **sin categorías** (solo detección) y publica una versión de detección | Python/HTTP |
| U3 | Un cliente HTTP con `profiles:write` administra los perfiles de **su** alcance y no puede tocar los de otro | HTTP |
| U4 | Un consumidor designa un perfil predeterminado y resuelve la versión publicada vigente sin ejecutar análisis | Python/HTTP |
| U5 | Editar un perfil no altera lo ya publicado; las ejecuciones futuras fijan una versión concreta | Python/HTTP |
| U6 | Actualizar el paquete no modifica perfiles existentes ni sus versiones | Python |
| U7 | Un desarrollador ejecuta todo sobre SQLite sin servidor | Python/HTTP |

**Incluye:** perfiles y su ciclo de vida; categorías y tipos sensibles (con definiciones, ejemplos e idioma); configuración de ranking/resolución; versiones publicadas inmutables; perfil predeterminado por alcance; resolución de perfil y versión publicados; catálogo inicial de tipos sensibles versionado en el paquete; permisos `profiles:read|write`; tablas y migración `0002`; límites y eventos de trazabilidad.

**Exclusiones.** Organizaciones, proyectos, membresías, herencia y políticas de destinos o de bloqueo (CAWbase); clasificación, inferencia, el algoritmo de ranking/resolución y el mock de inferencia (**A03**); validación de calidad con inferencia real (**A09**); documentos, protección y restauración; **trabajos** y el tratamiento de los iniciados (**A05**); credenciales por perfil; overrides por ejecución; purga y restauración de eliminados; operación de clonación; OIDC/usuarios/contraseñas; elección de modelo, AWS y publicación. El **código**: esta fase es solo documental.

## 2. Qué es un perfil, en una frase por decisión

Las decisiones aprobadas por el usuario (D-A02-01 a D-A02-06) y su detalle están en [A02-decisions](../decisions/A02-decisions.md#2-decisiones-aprobadas):

1. **Pertenencia:** perfil → un scope; categoría/tipo → un perfil; sin catálogo compartido; reutilizar es copiar.
2. **Versiones:** configuración editable separada de versiones publicadas inmutables; publicar valida y actualiza atómicamente la referencia vigente; las ejecuciones fijan una versión.
3. **Predeterminado y catálogo inicial:** a lo sumo un predeterminado por scope, explícito y opcional; copia versionada de tipos sensibles al crear un perfil.
4. **Desactivación y eliminación:** operaciones distintas, ambas conservan versiones; sin purga ni restauración.
5. **Ranking y resolución:** configuración en el perfil; puntuaciones en [0,1]; prioridades enteras únicas para resolver.
6. **Permisos:** `profiles:read` y `profiles:write`, sin jerarquías implícitas ni ampliación automática.

## 3. Mapa del contrato (sin copias contradictorias)

| Tema | Documento permanente |
|---|---|
| Conceptos, pertenencia, identificadores, estados, revisiones, definiciones/ejemplos/idioma, ranking y resolución, versiones e instantánea, publicabilidad (config vacía), resolución, predeterminado, **catálogo inicial** | [Perfiles y catálogo](../profiles-and-catalog.md) |
| Firmas Python, rutas HTTP, PATCH/null/filtros, errores, concurrencia, publicación e idempotencia, eventos | [API pública §10](../public-api.md#10-a02--perfiles-categorías-y-tipos-sensibles-implementado) |
| Tablas, claves, índices, restricciones, reglas de acceso, migración `0002`, compatibilidad, despliegue mixto | [Persistencia y migraciones §7](../persistence-and-migrations.md#7-a02--modelo-persistente-y-migración-implementado) |
| Permisos (10 = 8 de A01 + 2 de A02), matriz, datos sensibles, retención/eliminación, alcance deshabilitado | [Seguridad y aislamiento §9](../security-and-isolation.md#9-a02--permisos-y-datos-de-perfiles-implementado) |
| Límites iniciales, observabilidad, importación liviana | [Instalación y operación §11](../installation-and-operations.md#11-a02--límites-y-observabilidad-implementado) |
| Módulos y flujos previstos | [Arquitectura §8](../architecture.md#8-a02--perfiles-categorías-y-tipos-sensibles-implementado) |
| Decisiones aprobadas, elecciones técnicas derivadas, diferencias con el playbook y backlog | [A02-decisions](../decisions/A02-decisions.md) |

Resumen de elecciones técnicas derivadas (E-A02, no son decisiones de producto): ids provistos por quien llama; revisiones separadas (`revision`, `draft_revision`, `ranking_revision`, revisión del elemento y de la designación); estados `status`/`state` separados de las revisiones; publicación con `expected_draft_revision` estricta + comparación de la proyección de configuración efectiva (`content_hash` es solo integridad); bloqueos explícitos de las filas de alcance y perfil (`SELECT … FOR UPDATE`; SQLite con la transacción de escritura de A01); `profile_invalid` con `problems` sin contenido; catálogo de 6 tipos; límites iniciales; migración `0002` solo expandir con `min_runtime_level = 1`.

## 4. Relación con A01 y compatibilidad

- No se reabre A01 ni se modifica su código. Se reutilizan sin cambios: `ScopeContext`, `ScopedOperations`, API keys y delegación (D-27), `HostTransaction`, migraciones de una transacción y un bloqueo, envoltorio de errores, paginación, límites HTTP, control optimista estricto (D-30), política de alcance deshabilitado y reglas 404/403.
- El catálogo de permisos pasa de 8 a 10; los grants existentes no cambian ni se reinterpretan.
- El esquema pasa del nivel 1 al 2 (expandir; `min_runtime_level` sigue en 1). Crear un alcance no crea perfiles. La compatibilidad con A01 es **condicionada**: un runtime A01 opera sobre las tablas ampliadas solo con los datos y permisos que conoce; cargar grants nuevos puede fallar (al autenticar y en operaciones administrativas), y volver a A01 tras emitir esas claves no es un rollback transparente.
- **Aviso de despliegue mixto:** no emitir claves con `profiles:*` hasta que todos los runtimes estén en la versión de A02 ([persistencia §7.5](../persistence-and-migrations.md#75-migración-desde-a01-0002_profiles)).

## 5. Riesgos y límites declarados

- La calidad de clasificación o detección **no** se acredita aquí: los valores iniciales de ranking (umbral 0, tolerancia 0) son un punto de partida, no una medida de calidad (A03 implementa; A09 valida).
- Sin purga ni restauración: el contenido eliminado lógicamente permanece en la base y en las versiones.
- `profiles:read` es por alcance, sin granularidad por perfil.
- El crecimiento de versiones está acotado por límites (500 por perfil), no por retención.
- El catálogo inicial es una plantilla acotada de 6 tipos; no es una taxonomía exhaustiva ni promete detección.

## 6. Qué se entregó

Código, migración `0002`, pruebas, ejemplo y documentación, sin dependencias nuevas, sin mock ni inferencia. Versión del paquete: `0.2.0`. No se publicó nada ni se hizo push.

<a id="matriz-aceptacion-a02"></a>
## 7. Matriz de aceptación

Cada criterio se verifica con las pruebas indicadas en el [informe](../a02-implementation-report.md#matriz-ac-a02-trazable). Estado: implementada y probada localmente (ambientes y commits históricos en el informe); **cerrada y aceptada por el usuario** (`f4ad3e7`). «PG» = PostgreSQL real, «SQ» = SQLite real. Sin pruebas de calidad de inferencia ni del algoritmo de A03. Las pruebas de PostgreSQL usan solo bases descartables.

### 7.1 Recorrido, aislamiento y permisos

| ID | Criterio | Backend | Evidencia esperada |
|---|---|---|---|
| AC-A02-01 | Recorrido Python completo **sin CAWbase, sin modelo y sin mock**: crear alcance → perfil → categorías/tipos → ranking → publicar → predeterminado → resolver | PG, SQ | ejemplo ejecutable + prueba |
| AC-A02-02 | Recorrido HTTP equivalente con claves `profiles:*` (mismas operaciones del núcleo) | PG, SQ | pruebas HTTP |
| AC-A02-03 | Dos alcances y varios perfiles cada uno: ninguna operación (lectura, escritura, publicación, resolución, predeterminado) cruza alcances; id ajeno ⇒ error indistinguible de inexistente | PG, SQ | pruebas de acceso cruzado Python y HTTP |
| AC-A02-04 | Dentro de un alcance, categoría/tipo/versión de **otro perfil** referenciado por id ⇒ `resource_not_found` idéntico al inexistente | PG, SQ | pruebas |
| AC-A02-05 | Matriz de permisos completa: cada operación exige `profiles:read` o `profiles:write` (según la matriz); `scope:read/write` y `admin:*` **no** los otorgan; `AdminContext` no accede a perfiles; sin jerarquía read/write | PG, SQ | tabla de casos Python y HTTP |
| AC-A02-06 | HTTP: sin grant ⇒ 404 `resource_not_accessible`; grant sin permiso ⇒ 403; 401 genérico sin clave válida | PG, SQ | pruebas HTTP |
| AC-A02-07 | Delegación (D-27) con `profiles:*`: subconjunto del emisor; `admin:grants:write` para alcances sin acceso directo; **claves emitidas antes de A02 no ganan** `profiles:*` y no se reinterpretan; ampliar exige una clave nueva | PG, SQ | pruebas de emisión y de grants preexistentes |
| AC-A02-08 | Alcance deshabilitado: lecturas y versiones históricas permitidas; escrituras ⇒ `scope_disabled`; **resolver** ⇒ `scope_disabled` | PG, SQ | pruebas |

### 7.2 CRUD, estados y predeterminado

| ID | Criterio | Backend | Evidencia esperada |
|---|---|---|---|
| AC-A02-10 | CRUD de perfiles, categorías y tipos con todos los campos, límites y validaciones (ids, longitudes, ejemplos, traducciones, idioma, texto) | PG, SQ | pruebas de borde por campo |
| AC-A02-11 | `PATCH`: campo omitido ≠ `null`; `null` solo en campos anulables; campos de solo lectura/desconocidos ⇒ 422; sin campos ⇒ 422 | PG, SQ | pruebas |
| AC-A02-12 | Estados de perfil (`active`/`disabled`/`deleted`) y de elemento (`enabled`/`disabled`/`deleted`): transiciones permitidas e inválidas (`invalid_state_transition`); eliminado terminal; ids no reutilizables | PG, SQ | tabla de transiciones |
| AC-A02-13 | Filtros de estado por defecto excluyen eliminados; `status`/`state`/`include_deleted` los incluyen; eliminado fuera de ellos ⇒ 404; versiones de perfil eliminado siguen consultables | PG, SQ | pruebas |
| AC-A02-14 | Perfil `disabled`: no admite edición ni publicación (`profile_disabled`); lecturas sí; no seleccionable ni con versión explícita | PG, SQ | pruebas |
| AC-A02-15 | Unicidad: nombre único entre no eliminados (`name_conflict`, NFKC/casefold; nombre de eliminado liberado); prioridad única entre `enabled` (`priority_conflict`); creación idempotente por clave natural (idéntico ⇒ `created=false`; distinto o id reservado ⇒ `conflict`) | PG, SQ | pruebas + inspección de índices |
| AC-A02-16 | Predeterminado: a lo sumo uno por alcance (también bajo concurrencia); requiere perfil `active`, no eliminado y con versión publicada; puede no existir; `get` devuelve revisión 0 sin designación; designación inicial concurrente (sin fila previa), reemplazo y remoción serializados por el bloqueo del alcance (la segunda recibe 412) | PG, SQ | pruebas incl. carrera de designaciones iniciales, reemplazos y remociones |
| AC-A02-17 | Desactivar/eliminar el predeterminado ⇒ `profile_is_default`; tras quitarlo o reemplazarlo explícitamente, procede; el motor nunca elige reemplazo; nunca queda referencia inválida (carrera designar ∥ desactivar) | PG, SQ | pruebas de carrera |
| AC-A02-18 | Resolver sin predeterminado ⇒ `default_profile_not_set`; con predeterminado ⇒ su versión vigente; con `profile_id` ⇒ vigente; con versión explícita ⇒ esa; `version` sin perfil ⇒ 422; perfil sin publicar ⇒ `profile_not_published`; perfil `disabled`/`deleted` ⇒ `profile_not_selectable` incluso con versión explícita | PG, SQ | tabla de casos |
| AC-A02-19 | Crear un alcance no crea perfiles ni cambia el comportamiento de A01 (suite de A01 intacta) | PG, SQ | regresión de A01 |

### 7.3 Versiones, publicación y concurrencia

| ID | Criterio | Backend | Evidencia esperada |
|---|---|---|---|
| AC-A02-20 | Editar categorías, tipos, ranking o datos del perfil **no altera** versiones publicadas ni la versión vigente; la versión leída es idéntica (mismo `content_hash`, que cubre la instantánea completa) antes y después de editar, desactivar o eliminar elementos y el perfil | PG, SQ | pruebas con comparación de instantáneas |
| AC-A02-21 | Publicar valida el conjunto completo y crea instantánea autocontenida (solo `enabled`, con ids y revisiones de origen), `capabilities` y `content_hash` reproducible; actualiza `current_version_number` atómicamente | PG, SQ | pruebas + recalculo del hash |
| AC-A02-22 | Configuración vacía: perfil sin categorías publicable si tiene tipos (`classification=false`); sin tipos publicable si tiene categorías (`detection=false`); sin elementos `enabled` ⇒ `profile_invalid: no_enabled_elements`; el borrador vacío sí puede existir | PG, SQ | pruebas |
| AC-A02-23 | `revision` desactualizada ⇒ 412 **aunque los valores ya estén aplicados**; revisión correcta sin cambio efectivo ⇒ sin escritura, sin incremento y sin evento | PG, SQ | pruebas por recurso |
| AC-A02-24 | `draft_revision` cambia ante cualquier modificación efectiva del borrador y **no** ante transiciones de perfil, designación ni publicación; publicar con revisión vieja ⇒ 412 | PG, SQ | pruebas |
| AC-A02-25 | Casos de [modelo §7.3.1](../profiles-and-catalog.md#731-proyección-de-configuración-efectiva): (a) publicar y repetir sin editar ⇒ misma versión; (b) editar y restaurar el valor antes de publicar ⇒ se devuelve la vigente con su `source_draft_revision` original, sin versión nueva; (c) modificar solo un elemento deshabilitado ⇒ vigente; (d) cambiar un elemento habilitado ⇒ versión `n+1` completa; (e) `expected_draft_revision` obsoleta ⇒ 412 aunque la configuración coincida. La versión devuelta no se altera; `content_hash` no interviene en la decisión; `UNIQUE (perfil, source_draft_revision)` rechaza duplicados insertados directamente | PG, SQ | pruebas + inspección de filas y de la proyección |
| AC-A02-26 | Carreras: edición ∥ publicación; publicación ∥ publicación (concurrentes reales, por backend); edición ∥ edición del mismo elemento; desactivar/eliminar ∥ publicación; designar ∥ primera publicación; designar ∥ desactivar/eliminar el mismo perfil (nunca queda designado un perfil inválido); instantánea siempre consistente | PG, SQ | pruebas de concurrencia **por backend** con expectativas propias |
| AC-A02-27 | Reintento tras respuesta perdida de cada operación según la tabla de [§10.7](../public-api.md#107-concurrencia-publicación-e-idempotencia) | PG, SQ | pruebas por operación |
| AC-A02-28 | Resolución de modo `resolution`: se rechaza (`profile_invalid`) al cambiar el modo y al publicar si falta alguna prioridad, hay duplicadas (o ninguna categoría); `ranking` no exige prioridades; intercambio de prioridades vía `null` posible en el borrador | PG, SQ | pruebas |
| AC-A02-29 | Umbral/tolerancia: aceptan [0,1] incluidos extremos; rechazan negativos, > 1, NaN, ±∞, booleanos y cadenas; HTTP rechaza `NaN`/`Infinity` en JSON; valores iniciales 0/0 y modo `ranking` | PG, SQ | pruebas de borde |
| AC-A02-30 | Límites: cantidad de perfiles/elementos/versiones, ejemplos, textos, total por elemento y tamaño de instantánea ⇒ `limit_exceeded` sin efecto parcial | PG, SQ | pruebas de borde |
| AC-A02-34 | Límite de perfiles por alcance: creaciones concurrentes de perfiles **distintos** en el mismo alcance no superan el límite (comprobación e inserción bajo el bloqueo del alcance, no con `COUNT` en transacciones independientes) | PG, SQ | prueba concurrente real por backend |
| AC-A02-35 | Límites de categorías, tipos y versiones bajo el bloqueo del perfil: creaciones/publicaciones concurrentes no superan los máximos | PG, SQ | pruebas concurrentes por backend |
| AC-A02-36 | Operaciones sin cambio efectivo (`update_*`, `set/clear_default`, transiciones al mismo estado, publicar sin cambios) con revisión correcta: sin `UPDATE`/`INSERT` de datos de negocio, sin incrementar revisiones y sin evento, aunque hayan adquirido un bloqueo; revisión obsoleta ⇒ 412 | PG, SQ | pruebas con inspección de filas, revisiones y `admin_events` |
| AC-A02-37 | Orden de bloqueo y `HostTransaction`: en PostgreSQL se usa `SELECT … FOR UPDATE`/`FOR NO KEY UPDATE` (no `UPDATE` ficticio); en SQLite no se abre una transacción exterior adicional cuando la aporta el host; un interbloqueo provocado deliberadamente (host que retiene bloqueos) produce el error transaccional de A01 y nunca un estado parcial; el contrato no afirma ausencia universal de interbloqueos | PG, SQ | pruebas por backend |

### 7.4 Catálogo inicial

| ID | Criterio | Backend | Evidencia esperada |
|---|---|---|---|
| AC-A02-31 | Crear un perfil copia los 6 tipos del catálogo con `origin_entry` y registra procedencia (`catalog_id`, `catalog_version`, fecha); categorías vacías; `initial_catalog="empty"` copia nada y deja procedencia nula | PG, SQ | pruebas |
| AC-A02-32 | Cambiar el catálogo del paquete (otra `catalog_version` simulada) **no muta** perfiles existentes ni sus versiones; solo afecta a perfiles creados después | PG, SQ | prueba con catálogo alterno |
| AC-A02-33 | Los tipos copiados son editables, desactivables y eliminables como cualquiera; `starter_catalog()` no accede a la base y el catálogo cumple las reglas de contenido (sintético, límites, ids únicos, 6 tipos); `password` y `api_key_secret` se definen por función y contexto observable, no por quién generó el secreto (una contraseña generada por un gestor es `password`), y se documenta que los tipos pueden superponerse | — | prueba de datos del catálogo |

### 7.5 Transacciones, persistencia y migración

| ID | Criterio | Backend | Evidencia esperada |
|---|---|---|---|
| AC-A02-40 | Transacción propia: éxito y rollback sin escritura parcial (incl. publicación y creación con catálogo) | PG, SQ | pruebas |
| AC-A02-41 | `HostTransaction`: commit/rollback del host afectan también a A02; error recuperable ⇒ solo savepoint (incluido el incremento de `draft_revision`); fallos no recuperables ⇒ `transaction_broken`; nunca commit del motor | PG, SQ | pruebas |
| AC-A02-42 | Migración 1→2 con datos de A01 (alcances, clientes, claves, grants, eventos): conserva todo, no crea perfiles; `schema_level = 2`, `min_runtime_level = 1`; repetir es `already_current`; base vacía aplica `0001`+`0002` en una transacción; fallo intermedio ⇒ rollback completo | PG, SQ | pruebas de migración |
| AC-A02-43 | Objetos ajenos (tablas, vistas, secuencias, nombres próximos al prefijo) intactos tras instalar y tras actualizar 1→2 | PG, SQ | inventario antes/después |
| AC-A02-44 | Compatibilidad condicionada: runtime A02 con esquema 1 ⇒ `schema_incompatible`; runtime A01 con esquema 2 opera con los datos y permisos que conoce; se documenta y prueba que una clave con `profiles:*` hace fallar a un runtime A01 al autenticar y en operaciones administrativas que carguen sus grants, y que volver a A01 tras emitirla no es transparente | PG, SQ | pruebas de compatibilidad |
| AC-A02-45 | Todas las consultas de recursos incluyen `scope_id` y `profile_id` (verificación estática de las consultas) y las versiones no tienen `UPDATE`/`DELETE` en el código | — | análisis estático |
| AC-A02-46 | Restricciones de base: PK/índices parciales rechazan duplicados insertados directamente (nombre, prioridad, versión, fuente de borrador, segundo predeterminado) | PG, SQ | pruebas a nivel de tabla |

### 7.6 Seguridad, observabilidad y entrega

| ID | Criterio | Backend | Evidencia esperada |
|---|---|---|---|
| AC-A02-50 | **Sin contenido sensible en observabilidad**: nombres, definiciones, ejemplos, traducciones, descripciones e instantáneas (con marcadores únicos) no aparecen en logs, errores, eventos de `admin_events`, diagnósticos ni respuestas de error; `problems` solo con códigos e ids | PG, SQ | búsqueda de marcadores en toda la salida |
| AC-A02-51 | `admin_events`: una fila por cambio efectivo, en la misma transacción, con ids/acción/revisiones; ninguna por no-ops; un solo evento por crear perfil con catálogo | PG, SQ | inspección |
| AC-A02-52 | Importación liviana intacta: `import cawbase_content_engine` no importa el catálogo ni las operaciones de perfiles; sin efectos externos; D-33 (≤ 300 ms en caliente, ≤ 40 MiB) | — | suite de importación y `-m dist` |
| AC-A02-53 | Mapeo de errores y estados HTTP de cada código nuevo; envoltorio estable; `ETag`/`If-Match`/428 | PG, SQ | pruebas HTTP |
| AC-A02-54 | Documentación alineada con el código implementado (permisos, rutas, errores, estados, límites), informe `a02-implementation-report.md` y acta del usuario como únicos cierres | — | revisión documental |

No se prueba aquí la calidad de inferencia ni el algoritmo de ranking/resolución (A03/A09).

## 8. Evidencia requerida para la aceptación

El [informe de A02](../a02-implementation-report.md) distingue los niveles (implementado / probado en macOS / probado en Linux local / probado en GitHub Actions / aceptado), resultados reales por criterio y por commit, y lo no ejecutado dicho explícitamente. Una corrida omitida no es una aprobación; una corrida local no es una ejecución de Actions; el acta es del usuario.

## 9. Backlog

Mejoras opcionales fuera de los criterios: [A02-decisions §5](../decisions/A02-decisions.md#5-backlog-fuera-de-los-criterios-de-aceptación).
