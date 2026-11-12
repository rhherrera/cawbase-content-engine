# Perfiles, categorías, tipos sensibles y versiones (modelo de A02)

> **Estado: especificado para A02; no implementado.** Nada de este documento existe en el código (`0.1.0`, que solo contiene A01). Las decisiones de producto son de [A02-decisions](decisions/A02-decisions.md) (`D-A02-nn`, aprobadas por el usuario); las elecciones técnicas derivadas son `E-A02-nn` (adoptadas por criterio, revisables). Alcance y aceptación: [feature A02](features/A02-perfiles-categorias-y-tipos-sensibles.md).

Este documento fija la **semántica** del modelo (qué es cada cosa, estados, revisiones, versiones, catálogo inicial, ranking/resolución). Los demás contratos viven en: [API pública §10](public-api.md#10-a02--perfiles-categorías-y-tipos-sensibles-especificado-no-implementado) (firmas, rutas, errores), [persistencia §7](persistence-and-migrations.md#7-a02--modelo-persistente-y-migración-especificado-no-implementado) (tablas, índices, migración), [seguridad §9](security-and-isolation.md#9-a02--permisos-y-datos-de-perfiles-especificado-no-implementado) (permisos, datos sensibles), [operación §11](installation-and-operations.md#11-a02--límites-y-observabilidad-especificado-no-implementado) (límites, observabilidad).

## 1. Conceptos y pertenencia (D-A02-01)

| Concepto | Qué es | Pertenece a |
|---|---|---|
| **Perfil** | Agrupa una configuración de análisis. No conoce organizaciones ni proyectos. | Exactamente un `scope` existente |
| **Categoría** | Definición temática para *clasificar* contenido. Puede tener prioridad (solo para resolución). | Exactamente un perfil |
| **Tipo sensible** | Definición de *detección* de un tipo de información (p. ej. contraseñas). **No** es una política: no define bloqueo, destinos, severidad ni transformación. | Exactamente un perfil |
| **Configuración editable** (borrador) | Estado mutable de un perfil: nombre/descripción, ranking, categorías y tipos. Es lo único que se edita. | El perfil |
| **Versión publicada** | Instantánea completa, coherente e **inmutable** de la configuración editable en un momento. | El perfil |
| **Predeterminado** | Referencia opcional, a lo sumo una por scope, a un perfil. | El scope |

Reglas de pertenencia:

- **No hay catálogo editable compartido** entre perfiles. Reutilizar una definición es **copiar sus valores** explícitamente (leer de un perfil, crear en otro); no hay propagación. A02 no define una operación de clonación (backlog B-A02-03).
- El motor **no interpreta el dominio** del scope: no sabe si es un proyecto, un equipo u otra cosa.
- Todo acceso exige `scope_id` del contexto y, para categorías/tipos/versiones, también `profile_id` (ver [persistencia](persistence-and-migrations.md#74-reglas-de-acceso)). Conocer un id nunca concede acceso.
- Crear un scope **no** crea perfiles y no cambia el comportamiento de A01.

## 2. Identificadores (E-A02-01)

| Identificador | Quién lo elige | Formato | Unicidad | Mutabilidad |
|---|---|---|---|---|
| `profile_id` | quien llama (como `scope_id`) | `^[a-z0-9][a-z0-9_-]{0,63}$` | por scope | inmutable; **no se reutiliza** tras eliminar |
| `category_id`, `type_id` | quien llama | igual | por perfil | inmutable; no se reutiliza tras eliminar |
| `version_number` | el motor | entero 1, 2, 3… sin huecos | por perfil, monótono | inmutable |
| `version_id` | el motor | `pvr_` + UUID hex (32) | global | inmutable; referencia canónica opaca para fijar una versión |

Elegir ids provistos por quien llama permite **creación con clave natural** (repetir una creación idéntica no duplica; ver [idempotencia](public-api.md#107-concurrencia-publicación-e-idempotencia)) y que el catálogo inicial use ids estables. Los ids pueden aparecer en logs y eventos: no deben contener información personal ni sensible.

## 3. Estados (separados de las revisiones)

Cuatro cosas distintas, que no se mezclan:

| Qué | Campo | Valores / tipo | Para qué |
|---|---|---|---|
| Estado operativo del **perfil** | `status` | `active` · `disabled` · `deleted` | ciclo de vida y selección para ejecuciones |
| Estado de un **elemento** (categoría/tipo) en la configuración editable | `state` | `enabled` · `disabled` · `deleted` | qué entra en la próxima publicación |
| **Concurrencia** | `revision` (+ `draft_revision`, `ranking_revision`) | entero ≥ 1 | control optimista (§4) |
| **Versión publicada** | `version_number` / `version_id` | ver §2 | identidad de una instantánea |

### 3.1 Perfil (`status`)

```text
        disable            delete
active ───────► disabled ───────► deleted      (terminal en A02)
   ▲  ◄─────────  │                  ▲
   │    enable    └──────────────────┘
   └───────────────── delete ─────────┘
```

| Estado | Lectura | Edición del borrador / publicar | Seleccionable para ejecuciones nuevas | Versiones |
|---|---|---|---|---|
| `active` | sí | sí | sí (si tiene versión publicada) | consultables |
| `disabled` | sí | **no** (`profile_disabled`) | **no**, ni con versión explícita (`profile_not_selectable`) | consultables |
| `deleted` | solo con filtro explícito (`status=deleted` / `include_deleted`); en otro caso se comporta como inexistente (404) | no (404) | no | consultables |

- **Desactivar** (reversible) y **eliminar** (lógico) son operaciones distintas. Ambas conservan las versiones.
- **Eliminar** retira el recurso de la administración habitual. **No hay purga física** y **no hay restauración** en A02 (backlog B-A02-01/02). Es terminal: `enable`/`disable` sobre un perfil eliminado ⇒ `invalid_state_transition`.
- Un perfil **predeterminado** no puede desactivarse ni eliminarse: hay que reemplazar o quitar antes la designación (`profile_is_default`). El motor nunca elige un reemplazo.
- Efecto sobre trabajos ya iniciados: **corresponde a A05**. A02 no cancela nada retroactivamente: la selección se evalúa al *resolver* (§8), no durante una ejecución.

### 3.2 Elemento (`state`)

`enabled ⇄ disabled`; `enabled|disabled → deleted` (terminal, lógico, el id queda reservado). Solo los elementos `enabled` entran en una publicación (§6). Cambiar el estado de un elemento modifica la configuración editable y por tanto afecta **solo a publicaciones posteriores**, nunca a las instantáneas ya publicadas.

## 4. Revisiones (E-A02-02)

| Campo | Vive en | Cambia cuando | Se usa como |
|---|---|---|---|
| `revision` | perfil | cambia el registro del perfil: `display_name`, `description` o `status` | `If-Match` de `PATCH` del perfil |
| `draft_revision` | perfil | **cualquier** cambio efectivo del conjunto editable: nombre/descripción del perfil, ranking, y crear/editar/cambiar estado de cualquier categoría o tipo | precondición de **publicar** (`expected_draft_revision`) |
| `ranking_revision` | perfil | cambia `mode`, `eligibility_threshold` o `tie_tolerance` | `If-Match` de `PATCH …/ranking` |
| `revision` | categoría / tipo | cambia ese elemento (campos o `state`) | `If-Match` de `PATCH` del elemento |
| `revision` | designación predeterminada | se designa o se quita | `If-Match` de `PUT`/`DELETE` de la designación |

- **Un cambio efectivo incrementa**; un cambio sin efecto (valores idénticos) con revisión correcta **no escribe ni incrementa** (D-30 extendido). Una revisión desactualizada produce `revision_conflict` (412) **aunque los valores ya estén aplicados**.
- `draft_revision` **no** cambia por: transiciones de `status` del perfil, designación como predeterminado ni publicación. Así publicar dos veces sin editar no produce versiones nuevas (§7).
- Un cambio de `display_name`/`description` del perfil incrementa a la vez `revision` y `draft_revision` (forman parte de la instantánea).
- Las revisiones **no** son números de versión publicada ni se relacionan con ellos.

## 5. Definiciones, ejemplos e idioma (E-A02-03)

Campos comunes de categorías y tipos sensibles:

| Campo | Regla |
|---|---|
| `name` | 1–100 caracteres; único entre los elementos **no eliminados** del mismo tipo en el perfil (comparación por NFKC + *casefold*); `name_conflict` si choca |
| `definition` | 1–1500 caracteres. Describe **qué pertenece** y **qué no** al concepto, con sus límites. Texto libre del consumidor |
| `examples` | `{"positive": [str…], "negative": [str…]}`; hasta 5 por lista, 1–300 caracteres cada uno. Ambas listas opcionales (vacías por defecto) |
| `translations` | `null` o un objeto con **como máximo una** entrada, para el idioma que no es el del perfil: `{"en": {"name": …, "definition": …}}`. Opcional |

Semántica de los ejemplos:

- **Positivos**: textos *sintéticos* que ilustran contenido que **sí** pertenece. **Negativos**: textos sintéticos *parecidos* que **no** pertenecen (casos límite). Sirven de guía de definición (A03 decidirá cómo usarlos); el motor no los valida contra la definición, no los ejecuta y no garantiza detección.
- Los ejemplos del paquete y de la documentación son **sintéticos**: dominios reservados (`example.test`), nombres ficticios, números evidentemente falsos; nunca secretos reales ni datos personales reales. El motor **no inspecciona** el contenido aportado por el consumidor; este es responsable de no cargar datos reales. Lo aportado puede ser sensible y se trata como tal (nunca en logs, errores, eventos ni diagnósticos).

Idioma:

- `Profile.language` ∈ {`es`, `en`} (por defecto `es`), **inmutable**. `name`, `definition` y `examples` se redactan en ese idioma; el motor no valida el idioma del texto.
- `translations` permite añadir `name` y `definition` en el *otro* idioma **sin exigirlo**: una definición personalizada sin traducción es válida. Los ejemplos no se traducen (cada ejemplo puede estar en cualquiera de los dos idiomas).
- El catálogo inicial sí trae ambos idiomas (§9).

Validación de texto: se rechazan caracteres de control salvo `\n` y `\t` en `definition`; el texto se almacena con espacios de extremo recortados y normalizado a NFC; vacío tras recortar ⇒ `validation_failed`. Total de texto por elemento (nombre, definición, ejemplos y traducción) ≤ 8 KiB en UTF-8 (`limit_exceeded`).

### 5.1 Campos específicos

| Elemento | Campo | Regla |
|---|---|---|
| Categoría | `priority` | `null` o entero 1–10000; **menor número = mayor prioridad**. Único entre las categorías `enabled` del perfil (siempre, `priority_conflict`). La *presencia* solo se exige en modo `resolution` (§6) |
| Tipo sensible | `origin_entry` | `null` o la clave de la entrada del catálogo inicial de la que se copió (solo informativo; no se modifica al editar) |

## 6. Ranking y resolución: configuración (D-A02-05)

La configuración vive **en el perfil** (y por tanto en cada versión publicada). **No hay overrides por ejecución** en A02.

| Campo | Tipo | Valor inicial | Regla |
|---|---|---|---|
| `mode` | `ranking` \| `resolution` | `ranking` | cambiar a `resolution` valida §6.2 |
| `eligibility_threshold` | número | `0` | finito, en [0, 1]; NaN, ±∞ y booleanos se rechazan |
| `tie_tolerance` | número | `0` (empate exacto) | finito, en [0, 1]; tolerancia **absoluta** |

### 6.1 Significado (contrato que A03 implementará)

- Las puntuaciones de pertenencia están en [0, 1]. **No son probabilidades calibradas** y **no deben sumar uno**.
- **Elegible** ⇔ puntuación **≥** `eligibility_threshold`.
- **Ranking:** categorías ordenadas por puntuación descendente; se conservan los empates como tales; no exige prioridades. Un orden técnico estable entre puntuaciones iguales **no** es prioridad de negocio. La prioridad nunca modifica puntuaciones ni el ranking de relevancia.
- **Resolución (`resolution`):** entre las categorías elegibles, sea `m` la puntuación máxima; el conjunto de candidatas son las elegibles con `m − puntuación ≤ tie_tolerance` (distancia **respecto del máximo**, **sin empates transitivos encadenados**); se selecciona la de **mayor prioridad** (menor número). Las prioridades únicas hacen la elección inequívoca.
- Sin categorías elegibles, o con clasificación inconclusa: **no se fuerza una ganadora**.
- El consumidor puede obtener **todas** las categorías evaluadas: sin exclusiones silenciosas ni top-k obligatorio.
- Los valores iniciales no acreditan calidad. El algoritmo y el contrato de resultados son de **A03**; la validación con inferencia real, de **A09**. A02 solo almacena y valida la configuración.

### 6.2 Validación de la resolución

El modo `resolution` es **válido** si y solo si, entre las categorías `enabled`: hay al menos una; todas tienen `priority`; y las prioridades son únicas. Se comprueba (a) al cambiar `mode` a `resolution` y (b) al publicar. Entre una y otra comprobación el borrador puede quedar temporalmente inválido (p. ej. para intercambiar prioridades mediante `null`); la publicación lo rechaza con `profile_invalid`. El modo `ranking` no exige prioridades (si existen, siguen siendo únicas).

## 7. Versiones publicadas (D-A02-02)

- **Publicar** crea una versión nueva con una instantánea completa, coherente e inmutable de la configuración editable, y actualiza **atómicamente** `current_version_number` del perfil en la misma transacción. Validación previa del conjunto completo (§7.2).
- **Editar no cambia la versión vigente.** Las ejecuciones futuras (A03/A05) fijarán una versión concreta (`version_id`); nunca consultarán la configuración editable.
- La versión **vigente** de un perfil es la última publicada. No hay reversión ni despublicación en A02 (backlog B-A02-04).
- Las versiones no se modifican ni se eliminan (ni al desactivar o eliminar el perfil); siguen consultables con `profiles:read`. Consultar una versión **no** autoriza usarla en una ejecución nueva: eso lo decide la [resolución](#8-resolver-un-perfil-y-una-versión-publicados).

### 7.1 Instantánea (`snapshot`, `snapshot_schema_version = 1`)

```jsonc
{
  "snapshot_schema_version": 1,
  "profile": { "profile_id": "…", "display_name": "…", "description": null, "language": "es" },
  "catalog_provenance": { "catalog_id": "cce-starter-sensitive-types", "catalog_version": 1,
                          "copied_at": "2026-01-01T00:00:00Z" },   // null si el perfil empezó vacío
  "ranking": { "mode": "ranking", "eligibility_threshold": 0, "tie_tolerance": 0 },
  "capabilities": { "classification": true, "detection": true },
  "categories": [ { "category_id": "…", "name": "…", "definition": "…",
                   "examples": { "positive": [], "negative": [] }, "translations": null,
                   "priority": null, "source_revision": 3 } ],
  "sensitive_types": [ { "type_id": "…", "name": "…", "definition": "…",
                         "examples": { "positive": [], "negative": [] }, "translations": null,
                         "origin_entry": "personal_data", "source_revision": 1 } ]
}
```

- Solo incluye elementos `enabled`, ordenados por id. Conserva sus **ids** (identidad estable entre versiones) y la revisión del elemento al publicar.
- **Autocontenida:** leer una versión no consulta definiciones editables actuales ni requiere que los elementos existan, estén activos o no hayan sido eliminados.
- `content_hash` = SHA-256 de la serialización canónica del `snapshot` (JSON UTF-8 con claves ordenadas, sin espacios, sin escapes innecesarios). Cabecera de versión: `version_id`, `version_number`, `source_draft_revision`, `content_hash` (hex), `snapshot_bytes`, `published_at`, `published_by` (etiqueta opaca del contexto).
- Contiene definiciones del consumidor: es contenido potencialmente sensible (nunca a logs ni eventos; ver [seguridad](security-and-isolation.md#9-a02--permisos-y-datos-de-perfiles-especificado-no-implementado)).

### 7.2 Qué configuración puede publicarse (E-A02-04)

| Caso | ¿Publicable? | Significado |
|---|---|---|
| ≥ 1 categoría y ≥ 1 tipo `enabled` | sí | clasificación y detección configuradas |
| Solo tipos `enabled` (sin categorías) | **sí** | caso de uso **solo de detección**: `capabilities.classification = false` |
| Solo categorías `enabled` (sin tipos) | sí | solo clasificación: `capabilities.detection = false` |
| **Ningún** elemento `enabled` | **no** (`profile_invalid`: `no_enabled_elements`) | una versión vacía no se publica |
| `mode = resolution` inválido (§6.2) | no (`profile_invalid`: `resolution_requires_categories`, `priority_missing`, `priority_duplicate`) | |
| Instantánea > 1 MiB | no (`limit_exceeded`) | |

**«No configurado» ≠ «sin hallazgos».** Una versión sin categorías significa *no hay clasificación configurada*; una sin tipos, *no hay detección configurada*. Los consumidores (A03+) deben devolver `not_configured` para una capacidad ausente y **nunca** una lista vacía de hallazgos o de categorías que pueda leerse como «se evaluó y no se encontró nada». Por eso `capabilities` forma parte de la instantánea.

Preparar el borrador sí puede estar vacío (p. ej. un perfil creado con `initial_catalog="empty"` y aún sin elementos); simplemente no se publica.

### 7.3 Publicaciones duplicadas (E-A02-05)

Mecanismo local, sin marco general de idempotencia: la publicación se identifica por la clave natural **(perfil, `expected_draft_revision`)**, con respaldo en la base:

1. Se bloquea la fila del perfil y se exige `draft_revision == expected_draft_revision`; si no, `revision_conflict` (412).
2. Se calcula la instantánea. Si su `content_hash` es igual al de la versión vigente ⇒ **no se escribe nada** y se devuelve esa versión (`created = false`).
3. En otro caso se inserta la versión `n+1` con `source_draft_revision` y se actualiza `current_version_number`. `UNIQUE (perfil, source_draft_revision)` impide dos versiones para la misma revisión del borrador aunque falle el paso 2.

Efecto: reintentar una publicación (respuesta perdida, doble clic, dos clientes con la misma revisión) devuelve la misma versión; publicar sin cambios efectivos no crea versiones. Si entre tanto se editó el borrador, el reintento da 412 y el llamador relee.

## 8. Resolver un perfil y una versión publicados

Operación **de consulta** (no ejecuta análisis ni crea registros): devuelve la versión publicada concreta que una ejecución nueva debería fijar.

| Entrada | Resultado |
|---|---|
| `profile_id` y `version_number` | esa versión (selección `explicit`) |
| solo `profile_id` | la versión **vigente** del perfil (`explicit`) |
| ninguno | la del perfil **predeterminado** del scope (`default`); si no hay predeterminado ⇒ `default_profile_not_set`. **Nunca** se elige otro perfil en silencio |
| `version_number` sin `profile_id` | `validation_failed` |

Falla (sin efectos) con: `scope_disabled` (alcance deshabilitado: **no se resuelve para análisis nuevo**), `profile_not_selectable` (perfil `disabled`/`deleted`, **también con versión explícita**), `profile_not_published` (sin versión vigente), `resource_not_found` (perfil o versión inexistentes). Leer una versión histórica con `get_profile_version` **no** equivale a poder resolverla.

## 9. Perfil predeterminado (D-A02-03)

- A lo sumo **uno por scope**; opcional. Se designa explícitamente (`set_default_profile`) y se quita explícitamente (`clear_default_profile`).
- El perfil designado debe estar `active`, no eliminado y tener **al menos una versión publicada**. Como las versiones no se eliminan, la condición se mantiene mientras sea predeterminado.
- La designación referencia al **perfil** (su versión vigente se resuelve al consultar), no a una versión.
- La designación tiene su propia `revision` (0 si nunca se designó) para control optimista.

## 10. Catálogo inicial de tipos sensibles (D-A02-04, E-A02-06)

- Es una **plantilla versionada incluida en el paquete** (`catalog_id = cce-starter-sensitive-types`, `catalog_version = 1`), no una tabla compartida.
- **Crear un perfil** copia los tipos del catálogo (con `origin_entry`) y registra su **procedencia** (`catalog_id`, `catalog_version`, `copied_at`) en el perfil, salvo `initial_catalog = "empty"`. Las **categorías** temáticas empiezan **vacías**.
- Los tipos copiados son del perfil: editables, desactivables y eliminables como cualquier otro. Una **actualización del paquete nunca modifica perfiles existentes ni sus versiones**; una versión nueva del catálogo solo afecta a perfiles creados después. Incorporar a mano entradas nuevas es copiar valores (lectura de `starter_catalog()` + `create_sensitive_type`).
- Son **definiciones de detección**, no políticas, y el catálogo **no es exhaustivo** ni promete detección. Los tipos **no son mutuamente excluyentes**: un fragmento puede corresponder a más de uno (p. ej. un número de tarjeta en una frase sobre un pago). El catálogo separa conceptos por *qué es el fragmento*, no por su contexto.

Datos del catálogo `cce-starter-sensitive-types` v1 (todos los ejemplos son sintéticos; `P` = positivo, `N` = negativo):

| `type_id` | Es | Límite con los demás | Ejemplos (es / en) |
|---|---|---|---|
| `personal_data` — Datos personales / Personal data | Información que identifica o hace identificable a una persona física, sola o combinada: nombre y apellidos, correo, teléfono, dirección postal, fecha de nacimiento, imagen o ubicación personal. | Excluye números de documentos oficiales (`government_id`), credenciales (`password`, `api_key_secret`) y datos económicos (`salary_compensation`, `financial_information`). | P: «Contactar a Ana Ejemplo (ana.ejemplo@example.test), tel. +00 555 0100.» · N: «Escribir a soporte@example.test para dudas generales.» / P: «Contact Alex Sample at alex.sample@example.test, phone +00 555 0100.» · N: «Write to support@example.test for general questions.» |
| `government_id` — Identificadores oficiales / Official identifiers | Números o códigos asignados por una autoridad a una persona o entidad: documento nacional, pasaporte, número fiscal, seguridad social, licencia de conducir. | Distinto de `personal_data` (atributos descriptivos o de contacto) y de `financial_information` (cuentas y tarjetas). | P: «Documento de identidad: 00000000-X (ejemplo).» · N: «Pedido número 00000000 del catálogo.» / P: «Passport number: X0000000 (sample).» · N: «Order number 00000000 in the catalog.» |
| `salary_compensation` — Salarios y compensación / Salary and compensation | Remuneración individual vinculada a una persona o puesto identificable: salario, bonos, ajustes, comisiones. | No incluye bandas salariales o cifras agregadas sin persona ni puesto identificable. Cuentas y saldos son `financial_information`. | P: «El salario bruto anual de Ana Ejemplo es de 00.000 EUR.» · N: «Las bandas salariales del sector se publican cada año.» / P: «Alex Sample's gross annual salary is 00,000 USD.» · N: «Industry salary bands are published yearly.» |
| `financial_information` — Información financiera / Financial information | Datos de instrumentos y posición financiera de una persona o entidad: números de cuenta (IBAN u otros), tarjetas, saldos, movimientos, deudas. | Los pagos de salario son `salary_compensation`; los números de documentos, `government_id`. Una cuenta o tarjeta es financiera aunque aparezca junto a datos personales. | P: «Cuenta ES00 0000 0000 0000 0000 0000, saldo 0.000,00 EUR.» · N: «La entidad ofrece cuentas de ahorro.» / P: «Card number 0000 0000 0000 0000, balance 0,000.00 USD.» · N: «The bank offers savings accounts.» |
| `password` — Contraseñas / Passwords | Secretos de autenticación **elegidos por una persona**: contraseñas, PIN, frases de paso, respuestas a preguntas de seguridad. | Los secretos **generados por una máquina** (claves, tokens) son `api_key_secret`. | P: «Mi contraseña es EjemploNoReal-123.» · N: «La contraseña debe tener al menos ocho caracteres.» / P: «My password is NotARealPassword-123.» · N: «The password must be at least eight characters.» |
| `api_key_secret` — API keys y tokens / API keys and tokens | Credenciales **generadas por una máquina**: claves de API, tokens de acceso o refresco, claves privadas, cadenas de conexión con credenciales. | Las contraseñas elegidas por personas son `password`. | P: «Authorization: Bearer EJEMPLO_TOKEN_NO_REAL_0000» · N: «Use un token en el encabezado Authorization.» / P: «api_key = EXAMPLE_NOT_A_REAL_KEY_0000» · N: «Send the token in the Authorization header.» |

Las definiciones completas (es/en) de cada entrada se redactan en el paquete al implementar, respetando los límites de §5, estas fronteras y la política de ejemplos sintéticos. `starter_catalog()` devuelve el catálogo sin acceso a base de datos.

## 11. Fuera de este modelo

Clasificación/inferencia y mock (A03), documentos, protección/restauración, trabajos (A05), políticas/destinos/bloqueo, herencia, organizaciones, proyectos y membresías, credenciales por perfil, overrides por ejecución, purga y restauración. Ver [backlog](decisions/A02-decisions.md#5-backlog-fuera-de-los-criterios-de-aceptación).
