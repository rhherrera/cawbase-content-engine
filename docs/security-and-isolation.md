# Seguridad y aislamiento

> **Estado: A01 implementada en `0.1.0`, cerrada y aceptada por el usuario (commit aceptado `1b5ffab`; GitHub Actions aprobado en `main` y en `feature/a01-product-foundation`, según confirmación explícita del usuario). A02 implementada en `0.2.0`: evidencia y niveles en el [informe de A02](a02-implementation-report.md); A02 cerrada y aceptada por el usuario (commit aceptado `f4ad3e7`; GitHub Actions pasó sobre ese commit, según su confirmación explícita).** Evidencia y alcance real de las pruebas: [informe de implementación](a01-implementation-report.md). Los contratos descritos están implementados y cubiertos por pruebas (`tests/test_credentials.py`, `test_bootstrap_and_recovery.py`, `test_scopes_and_isolation.py`, `test_http.py`).

Relacionado: [API pública](public-api.md) · [persistencia y migraciones](persistence-and-migrations.md) · [instalación y operación](installation-and-operations.md).

## 1. Modelo de amenazas y límites de confianza

```text
 Frontend / usuarios ──(auth del HOST: sesión, OIDC, etc.)──► Host Python (p. ej. CAWbase)
                                                                   │ construye ScopeContext / AdminContext
                                                                   ▼
                                                           ContentEngine (librería)  ──► Base de datos
 Cliente HTTP externo ──(API key)──► Adaptador HTTP ──► ContentEngine (mismo núcleo) ──► Base de datos
 Operador (acceso a la base) ──► CLI migrate / bootstrap-admin
```

| Frontera | Quién confía en quién |
|---|---|
| Frontend → host | El host autentica y autoriza. El motor **no** ve usuarios, organizaciones ni membresías (D-17). |
| Host → motor (Python) | El motor confía en que el host construyó el contexto tras autorizar. **Esta confianza no es verificable** por el motor. |
| Cliente HTTP → adaptador | El adaptador autentica la API key y autoriza cada ruta antes de acceder a recursos (D-18). |
| Motor → base | Credenciales con los mínimos privilegios descritos en [permisos DDL](persistence-and-migrations.md#44-permisos-ddl-y-consecuencias-del-arranque-automático). |

Fuera de alcance de A01: contenido documental, correspondencias, servidor de inferencia, OIDC/usuarios/contraseñas (D-20).

## 2. Aislamiento por alcance (D-17, D-19)

- Un **alcance** se identifica por `scope_id`, cadena opaca elegida por el host o el operador. El motor no le da semántica: no sabe si representa un proyecto, una organización u otra cosa.
- Todo recurso de alcance tiene `scope_id` y toda consulta lo exige en el `WHERE` ([reglas de acceso](persistence-and-migrations.md#3-reglas-de-acceso-a-datos-contrato-interno)).
- Las operaciones administrativas **globales** (crear/deshabilitar alcances, clientes, claves, diagnóstico) están en `AdminOperations`, con `AdminContext`; las de un alcance en `ScopedOperations`, con `ScopeContext`. Son tipos distintos y no intercambiables.
- Un identificador de recurso conocido o adivinado no concede acceso: el motor valida que el recurso pertenece al alcance del contexto.
- **Nunca** se resuelve autorización a partir de un `scope_id` enviado libremente por un frontend. El host deduce el alcance autorizado del llamador autenticado (p. ej. membresía del usuario en el proyecto) y recién entonces construye el contexto.

<a id="limite-del-contexto-python"></a>
### 2.1 Límite del contexto Python

El `ScopeContext`/`AdminContext` es un **objeto de valor con un contrato de uso**, no una credencial infalsificable:

- Cualquier código que corra en el mismo proceso y pueda importar el paquete puede construir un contexto para cualquier `scope_id`.
- Compartir proceso **y credenciales de base de datos** con el host significa que código arbitrario del host puede leer y escribir directamente las tablas del motor, saltándose el paquete.
- Por tanto el aislamiento en modo embebido protege frente a **errores y confusión de alcance** en código honesto, y frente a clientes externos mediante la autorización del host; **no** protege contra código malicioso o comprometido dentro del propio host.
- Si se necesita aislamiento fuerte frente al host, hay que separar procesos y credenciales de base (por ejemplo el servicio HTTP con su propia base y roles). Eso es una decisión de despliegue, no algo que el contexto Python pueda garantizar.

Los nombres `trusted_*` de los constructores de contexto existen para que esta responsabilidad quede visible en el código del host.

<a id="matriz-de-permisos"></a>
## 3. Matriz de permisos

### 3.1 Catálogo (decisión D-27, antes P-09)

**Ocho** permisos en total: 2 de alcance y 6 globales (`admin:*`).

| Permiso | Nivel | Habilita |
|---|---|---|
| `scope:read` | alcance | Leer el recurso del alcance |
| `scope:write` | alcance | Modificar nombre, descripción y metadatos del alcance (no su estado) |
| `admin:scopes:read` | global | Leer/listar cualquier alcance |
| `admin:scopes:write` | global | Crear, habilitar y deshabilitar alcances |
| `admin:clients:read` | global | Leer/listar clientes y metadatos de claves |
| `admin:clients:write` | global | Crear/habilitar/deshabilitar clientes; emitir y revocar claves |
| `admin:grants:write` | global | **Administrar globalmente la asignación de permisos sobre alcances**: conceder, al emitir una clave, permisos de alcance sobre alcances existentes aunque el emisor no tenga acceso directo a ellos |
| `admin:diagnostics:read` | global | Leer el diagnóstico autenticado |

> **`admin:grants:write` es una capacidad privilegiada.** Quien la posee puede emitir una clave que le concede (o concede a otro) acceso de lectura/escritura a **cualquier** alcance existente. No es administración inocua de metadatos: equivale a poder concederse acceso a todos los alcances. Debe asignarse solo a operadores, de forma nominal y con revocación planificada.

`admin:*` no implica permisos de alcance: un administrador que quiera operar dentro de un alcance necesita un grant de alcance explícito (excepto las lecturas administrativas de `admin:scopes:read`, que devuelven el mismo registro del alcance y son una decisión deliberada de operador). No existen comodines implícitos (`*`, `admin:*`) ni jerarquías entre permisos: cada permiso se lista de forma explícita.

<a id="32-reglas-de-delegacion-d-27"></a>
### 3.2 Reglas de delegación (D-27)

Aplican al **emitir una API key** (Python o HTTP). Los grants permanecen **inmutables por clave**.

1. Emitir una clave requiere siempre `admin:clients:write`.
2. **Grants administrativos:** el emisor solo puede conceder permisos `admin:*` que él mismo posee (regla de subconjunto, siempre). Esto incluye delegar `admin:grants:write`: solo la concede quien ya la tiene.
3. **Grants de alcance sin `admin:grants:write`:** cada par (alcance, permiso) concedido debe estar contenido en los grants de alcance que posee el emisor (subconjunto).
4. **Grants de alcance con `admin:grants:write`:** el emisor puede conceder permisos de alcance sobre cualquier alcance **existente**, aunque no tenga acceso directo a él.
5. Violar 2 o 3 ⇒ `PermissionDeniedError` (403); la respuesta es la misma exista o no el alcance pedido. Alcance inexistente con `admin:grants:write` ⇒ `validation_failed` (422).
6. Sin comodines; sin editar grants (para cambiarlos: emitir otra clave y revocar la anterior).
7. El bootstrap inicial y la recuperación administrativa reciben el **catálogo administrativo completo** (los seis `admin:*`), sin grants de alcance.

### 3.3 Matriz Python y HTTP

| Operación | Python: contexto y permiso | HTTP: ruta | Clave HTTP |
|---|---|---|---|
| Leer su alcance | `ScopeContext` + `scope:read` | `GET /v1/scopes/{id}` | grant `scope:read` en `{id}` |
| Modificar su alcance | `ScopeContext` + `scope:write` | `PATCH /v1/scopes/{id}` | grant `scope:write` en `{id}` |
| Crear alcance | `AdminContext` + `admin:scopes:write` | `POST /v1/admin/scopes` | `admin:scopes:write` |
| Leer/listar alcances (global) | `AdminContext` + `admin:scopes:read` | `GET /v1/admin/scopes[/{id}]` | `admin:scopes:read` |
| Habilitar/deshabilitar alcance | `AdminContext` + `admin:scopes:write` | `POST …/disable` `…/enable` | `admin:scopes:write` |
| Clientes: crear/estado | `AdminContext` + `admin:clients:write` | `POST /v1/admin/clients…` | `admin:clients:write` |
| Clientes: leer/listar | `AdminContext` + `admin:clients:read` | `GET /v1/admin/clients…` | `admin:clients:read` |
| Emitir clave (grants dentro de lo que posee el emisor) | `AdminContext` + `admin:clients:write` | `POST …/api-keys` | `admin:clients:write` |
| Emitir clave concediendo alcances que el emisor no posee | `AdminContext` + `admin:clients:write` + `admin:grants:write` | `POST …/api-keys` | `admin:clients:write` + `admin:grants:write` |
| Listar claves | `AdminContext` + `admin:clients:read` | `GET …/api-keys` | `admin:clients:read` |
| Revocar clave | `AdminContext` + `admin:clients:write` | `POST /v1/admin/api-keys/{id}/revoke` | `admin:clients:write` |
| Diagnóstico autenticado | `AdminContext` + `admin:diagnostics:read` | `GET /v1/admin/diagnostics` | `admin:diagnostics:read` |
| Identidad propia | — | `GET /v1/auth/self` | cualquier clave válida |
| Liveness / readiness | — | `GET /livez` · `GET /readyz` | ninguna |
| Bootstrap inicial | operador con acceso a la base | **sin ruta HTTP** | — |
| Migrar | operador con credencial DDL | **sin ruta HTTP** | — |

Llamadas internas Python **no requieren API key** (D-18): la autorización la hace el host.

## 4. Ciclo de vida de las API keys (D-18)

### 4.1 Formato, codificación canónica y almacenamiento (decisión D-25, antes P-07)

- Formato textual: `cce1.<key_id>.<secret_text>`, tres partes separadas por `.`. `cce1` identifica producto y versión de formato (ayuda a detectar fugas).
- `key_id`: `k_` + 24 caracteres hexadecimales en minúscula (12 bytes aleatorios). Público.
- **Secreto:** 32 bytes (256 bits) de un generador criptográficamente seguro (`secrets.token_bytes(32)`).
- **Codificación canónica del secreto:** `secret_text` = base64url sin relleno (RFC 4648 §5, alfabeto `A-Za-z0-9-_`, sin `=`), siempre **43 caracteres**.
- **Qué bytes se hashean:** el hash es SHA-256 sobre los **32 bytes crudos** del secreto (el valor decodificado), **no** sobre el texto, ni sobre el prefijo, ni sobre el `key_id`. Se guarda el resumen de 32 bytes y `hash_algorithm='sha256'`.
- **Emisión:** generar 32 bytes → `secret_text` = codificación canónica → hash de los 32 bytes → guardar hash → entregar `secret_text` dentro de la clave completa.
- **Verificación:** (1) separar en exactamente 3 partes y comprobar prefijo `cce1`, formato de `key_id` y que `secret_text` cumpla `^[A-Za-z0-9_-]{43}$`; (2) decodificar base64url **de forma estricta** y volver a codificar: si no coincide con el texto recibido (variantes no canónicas) se rechaza; (3) comprobar que son 32 bytes; (4) buscar por `key_id`; (5) calcular SHA-256 de los 32 bytes y comparar con `hmac.compare_digest`. Cualquier fallo ⇒ el mismo `authentication_failed`.
- Un hash rápido es aceptable porque el secreto es aleatorio de 256 bits (no una contraseña elegida por una persona). **Sin *pepper* ni KMS en A01** (Q-03 cerrada).
- Las claves van en `Authorization: Bearer`; nunca en URL ni cuerpo. No son credenciales del futuro servidor de inferencia ni de CAWbase.

**Sobre tiempos.** Se usa comparación de tiempo constante para el hash y, si el `key_id` no existe, se compara contra un hash ficticio para reducir diferencias evitables. Esto **no** hace que la petición completa tenga duración constante, y no se promete tiempo idéntico ni ausencia absoluta de canales temporales (la búsqueda en base, el formato inválido que se rechaza antes de la base y la red introducen diferencias). Lo que sí se garantiza es una respuesta genérica idéntica (estado, cuerpo y encabezados) para toda credencial inválida.

<a id="bootstrap-administrativo"></a>
### 4.2 Bootstrap inicial y recuperación administrativa (decisión D-26, antes P-08)

Problema: la primera clave administrativa no puede crearla una API que exige autenticación, y no debe existir clave universal ni endpoint abierto. Se definen **dos operaciones locales de operador** distintas. No tienen ruta HTTP, no usan API key y no constituyen una segunda plataforma de administración. El operador necesita **acceso autorizado a la base** (ese es el límite de confianza) y la URL de base llega por entorno o `*_FILE`, nunca por argumento.

#### 4.2.1 Bootstrap inicial

- Comando `cawbase-content-engine bootstrap-admin [--secret-file RUTA]`; función `create_initial_admin_key(engine, *, label="bootstrap")`.
- **Precondición persistente:** `engine_state.admin_initialized_at IS NULL`. No depende de cuántas claves queden activas.
- **Una transacción serializada:** se bloquea la fila `engine_state` (`SELECT … FOR UPDATE` en PostgreSQL; transacción de escritura inmediata en SQLite), se recomprueba la precondición y se crea el cliente `initial-admin`, la clave con el catálogo administrativo completo, el evento `bootstrap.admin_key_issued`, y se fija `admin_initialized_at` y `admin_bootstrap_generation = 1`. Todo o nada.
- Dos bootstraps concurrentes: exactamente uno tiene éxito; el otro recibe `BootstrapNotAllowedError`. No se crean dos administradores iniciales.
- Si `initial-admin` ya existe como cliente (creado fuera del motor) ⇒ `BootstrapNotAllowedError` sin cambios.

#### 4.2.2 Recuperación administrativa

- Comando `cawbase-content-engine recover-admin --confirm [--secret-file RUTA]`; función `recover_admin_access(engine, *, label="recovery")`.
- **Precondición:** `admin_initialized_at IS NOT NULL` (si no, `RecoveryNotAllowedError`: usar bootstrap). `--confirm` es obligatorio (acto deliberado del operador).
- Funciona **aunque** las claves administrativas existentes estén perdidas, vencidas, limitadas a menos permisos o pertenezcan a clientes deshabilitados: no se autentica con ninguna clave ni requiere editar SQL a mano.
- Misma serialización que el bootstrap (bloqueo de la fila `engine_state`) y mismo catálogo administrativo completo. Sube `admin_bootstrap_generation` y deja el evento `admin.recovery_key_issued` (con `generation`, sin secretos).
- **Cliente:** siempre se **crea un cliente nuevo** `recovery-<AAAAMMDD>-<8 hex aleatorios>`; nunca se reutiliza ni se rehabilita uno existente. Si el nombre colisiona se reintenta con otro sufijo (hasta 3 veces) dentro de la transacción. Así no hay colisión de nombres ni dependencia del estado de clientes previos.
- **No revoca ni deshabilita nada**, ni silenciosamente ni como efecto secundario.
- Es deliberadamente repetible: cada ejecución confirmada emite otra clave; ejecuciones concurrentes se serializan y quedan auditadas por separado.

#### 4.2.3 Entrega del secreto

- El secreto se entrega **una sola vez**, por terminal (stdout) o a un archivo creado con `O_CREAT|O_EXCL` y permiso `0600`. Nunca se registra en logs, eventos ni base.
- Con `--secret-file`, el archivo se crea exclusivamente **antes** de confirmar la transacción (si la ruta no es utilizable, falla sin emitir nada). Si el archivo se creó pero la transacción no confirma, se elimina. Tras el commit se escribe el secreto y se hace `fsync`.
- **Pérdida tras el commit** (se perdió la salida del comando, o falló la escritura del archivo después de confirmar): el secreto **no se recupera** porque no existe almacenado. Procedimiento: (1) ejecutar `recover-admin --confirm` para obtener una clave nueva; (2) con esa clave, identificar la clave huérfana por su cliente y fecha (`GET /v1/admin/clients`, `GET …/api-keys` o los eventos `bootstrap.admin_key_issued` / `admin.recovery_key_issued`); (3) revocarla con `POST /v1/admin/api-keys/{key_id}/revoke`.

#### 4.2.4 Revocar credenciales administrativas después de recuperar acceso

La recuperación no revoca nada; hacerlo es un paso aparte y explícito: con la clave nueva, listar clientes y claves, revocar (`revoke`) las claves perdidas o sospechosas y deshabilitar los clientes asociados si corresponde. Dejar activas las claves administrativas anteriores es una decisión consciente del operador.

Después, el administrador usa la API (Python o HTTP) para crear clientes, alcances y claves con grants acotados, y debería revocar o rotar la clave de bootstrap/recuperación cuando disponga de claves administrativas nominales.

No existe: clave por defecto, variable de entorno con clave estática, endpoint de bootstrap o de recuperación, ni modo «sin autenticación».

### 4.3 Emisión, presentación única y rotación

- Emitir requiere `admin:clients:write`, las [reglas de delegación](#32-reglas-de-delegacion-d-27) y un `request_id` (UUID) para idempotencia ([contrato](public-api.md#7-idempotencia-por-operación)). El secreto se devuelve en la respuesta `201` y **nunca más**: ni en listados, ni en repeticiones, ni en logs, ni en eventos ni en el hash expuesto.
- Rotación: emitir una clave nueva con los mismos grants, migrar al consumidor, revocar la anterior. Los grants no se editan.
- Vencimiento opcional (`expires_at`).

### 4.4 Verificación, revocación y administración

- Cada solicitud HTTP autentica consultando la base (**sin caché de autenticación en A01**).
- **Alcance de la revocación:** una revocación (o deshabilitación de cliente) **confirmada** afecta a las autenticaciones posteriores. No cancela retroactivamente solicitudes que ya fueron autenticadas y autorizadas ni operaciones en curso.
- Revocar: `revoked_at` queda fijado; repetible. Deshabilitar un cliente invalida sus claves para autenticaciones posteriores.
- Listados muestran solo metadatos (`key_id`, etiqueta, grants, fechas), nunca secreto ni hash. Tampoco aparecen en logs, errores ni eventos.
- Una clave con `scope:read` en A no puede nada en B, y una clave sin grants administrativos recibe 403 en rutas `/v1/admin/*`.

## 5. Autorización antes de acceso a recursos

Orden fijo: límite de cuerpo → autenticación → resolución de permiso y alcance → validación del cuerpo → operación. Un 404/403 no se decide leyendo el recurso de otro alcance. Reglas de respuesta: ver [errores](public-api.md#8-errores).

## 6. Secretos y datos sensibles en A01

| Dato | Tratamiento |
|---|---|
| URL de conexión | `SecretStr`; nunca en `repr`, logs, errores, diagnóstico ni `/readyz` |
| Secreto de API key | solo en la respuesta de emisión; en la base solo el hash |
| Hash de API key | nunca devuelto por API |
| Encabezado `Authorization` | nunca registrado |
| `metadata`/`display_name`/`description` de alcance y cliente | datos del host; **no deben contener información personal ni sensible**; no se escriben en logs ni eventos (solo nombres de campos) |
| Mensajes de error del driver | traducidos; no se propagan |

Como en A01 no se procesa contenido de usuarios, no existen prompts ni documentos que proteger; las reglas de [observabilidad](installation-and-operations.md#observabilidad) ya excluyen contenido para mantener ese principio cuando aparezcan.

<a id="retencion-revocacion-y-eliminacion"></a>
## 7. Retención, revocación y eliminación

Alcanza únicamente los datos que A01 introduce.

| Dato | Retención en A01 | Revocación/eliminación |
|---|---|---|
| `scopes` | indefinida | se **deshabilita** (`status=disabled`): lecturas siguen, escrituras bloqueadas; **sin borrado físico** |
| `clients` | indefinida | se deshabilita; invalida sus claves; sin borrado |
| `api_keys` (hash + metadatos) | indefinida, incluidas revocadas/vencidas | revocación inmediata; la fila se conserva para auditoría e idempotencia; el secreto nunca existió almacenado |
| grants | mientras exista la clave | inmutables |
| `admin_events` | indefinida; sin purga en A01 (Q-04) | no hay operación de borrado |
| `engine_state` | permanente | — |

- El motor no ofrece en A01 eliminación física. El retiro completo de la instalación (borrar el schema o las tablas con prefijo) es una operación manual del DBA, destructiva, documentada en la guía de operación de A10; el motor no la ejecuta nunca por sí mismo ni en migraciones.
- Las copias de seguridad de la base conservan estos datos según la política del operador; no se afirma borrado inmediato en copias.
- Cuando aparezcan datos documentales y correspondencias (A05/A06) tendrán su propia retención; no se anticipa aquí.

## 8. Riesgos residuales conocidos

- El contexto Python no es infalsificable (ver §2.1).
- Sin limitación de tasa interna: la fuerza bruta sobre `key_id`/`secret` se mitiga por entropía de 256 bits y por limitación en el proxy inverso; un atacante que pueda llamar al servicio puede generar carga de lecturas de autenticación.
- TLS lo termina el proxy inverso/ingreso; el servicio no gestiona certificados en A01.
- Sin caché de autenticación: cada solicitud lee la base (costo aceptado a cambio de revocación inmediata).
- En SQLite, la seguridad entre aplicaciones que comparten archivo depende del sistema de archivos.

## 9. A02 — Permisos y datos de perfiles (implementado)

> **Implementado en `0.2.0` (A02); cerrado y aceptado por el usuario (commit `f4ad3e7`).** El catálogo pasa de **8** permisos (A01) a **10**: A02 agrega 2 y ningún otro cambio de modelo de seguridad. Decisión: D-A02-06 en [A02-decisions](decisions/A02-decisions.md). Todo lo de §1–§8 sigue vigente.

### 9.1 Catálogo ampliado

| Permiso | Nivel | Estado | Habilita |
|---|---|---|---|
| `scope:read`, `scope:write`, `admin:scopes:read`, `admin:scopes:write`, `admin:clients:read`, `admin:clients:write`, `admin:grants:write`, `admin:diagnostics:read` | — | **implementados en A01** | Ver §3.1 (sin cambios) |
| `profiles:read` | alcance | **implementado en 0.2.0 (A02)** | Leer perfiles, sus categorías y tipos sensibles, la configuración de ranking, las versiones publicadas y la designación predeterminada; **resolver** un perfil y versión |
| `profiles:write` | alcance | **implementado en 0.2.0 (A02)** | Crear, editar, desactivar/reactivar y eliminar (lógicamente) perfiles, categorías y tipos; modificar el ranking; **publicar** versiones; designar y quitar el predeterminado |

Reglas:

- **Sin jerarquías implícitas.** `profiles:write` no incluye `profiles:read`: una clave que escribe y también necesita leer recibe ambos grants. No hay comodines.
- `scope:read` y `scope:write` **conservan su significado**: no otorgan `profiles:*` automáticamente (ni al revés). `admin:*` (incluido `admin:grants:write` o `admin:scopes:read`) **no concede acceso al contenido de perfiles**: un `AdminContext` no puede invocar las operaciones de perfiles, y las rutas `/v1/admin/*` no exponen perfiles. Quien administra alcances no lee sus definiciones sin un grant de alcance explícito.
- Se reutilizan `ScopeContext`, `ScopedOperations`, API keys y las [reglas de delegación D-27](#32-reglas-de-delegacion-d-27) sin cambios: un permiso `profiles:*` sobre un alcance es un grant de alcance como cualquier otro (subconjunto de lo que posee el emisor, o `admin:grants:write` para alcances existentes sin acceso directo). **No hay credenciales por perfil.**
- **Los grants existentes son inmutables y no se reinterpretan.** Las claves emitidas antes de A02 no ganan `profiles:*`. Para ampliar el acceso HTTP se emite una clave nueva (y se revoca la anterior) conforme a las reglas vigentes. Ver el aviso de despliegue mixto, que incluye por qué **revocar no recupera la compatibilidad con A01**, en [persistencia §7.5](persistence-and-migrations.md#75-migración-desde-a01-0002_profiles).
- Los contextos Python se construyen con los nuevos permisos: `engine.trusted_scope_context(scope_id, {ScopePermission.PROFILES_READ, …})`; el límite del contexto Python (§2.1) se mantiene.
- Bootstrap y recuperación administrativa siguen emitiendo solo los seis `admin:*`: no incluyen permisos de alcance.

### 9.2 Matriz Python y HTTP

| Operación | Python: contexto y permiso | HTTP | Clave |
|---|---|---|---|
| Leer/listar perfiles, categorías, tipos, ranking, versiones | `ScopeContext` + `profiles:read` | `GET /v1/scopes/{id}/profiles…` | grant `profiles:read` en `{id}` |
| Leer designación predeterminada / resolver | `ScopeContext` + `profiles:read` | `GET …/default-profile`, `GET …/resolved-profile` | `profiles:read` |
| Crear, editar, cambiar estado de perfiles/categorías/tipos; ranking | `ScopeContext` + `profiles:write` | `POST/PATCH …/profiles…` | `profiles:write` |
| Publicar | `ScopeContext` + `profiles:write` | `POST …/publish` | `profiles:write` |
| Designar / quitar predeterminado | `ScopeContext` + `profiles:write` | `PUT/DELETE …/default-profile` | `profiles:write` |
| Perfiles con `AdminContext` | **no disponible** (tipo distinto) | sin rutas `/v1/admin` | — |

Reglas de respuesta: sin ningún grant sobre el alcance ⇒ 404 `resource_not_accessible` (idéntico exista o no); con algún grant pero sin el permiso requerido (p. ej. `scope:read` para listar perfiles) ⇒ 403 `permission_denied`. Un perfil, categoría, tipo o versión inexistente **o perteneciente a otro perfil o alcance** produce el mismo 404 `resource_not_found`: ninguna consulta se resuelve por id solo.

### 9.3 Datos sensibles aportados por el consumidor

Nombres, definiciones, ejemplos, traducciones e instantáneas **pueden contener información sensible** (los ejemplos describen justamente qué se considera sensible). Reglas:

| Dato | Tratamiento |
|---|---|
| `display_name`, `description` del perfil; `name`, `definition`, `examples`, `translations`; `snapshot` de versiones | Datos del consumidor. **Nunca** en logs, errores, eventos, diagnósticos, métricas ni mensajes de validación; los errores citan el nombre del campo y el límite, no el valor. Se almacenan en la base en claro (el cifrado de datos en reposo es del despliegue) |
| Ids (`profile_id`, `category_id`, `type_id`), `version_id`, `version_number`, revisiones, nombres de campos | Permitidos en logs y eventos. Los ids son elegidos por el consumidor: deben ser opacos o neutros, sin datos personales (igual que `scope_id`) |
| `problems` de `profile_invalid` | Solo código e id de categoría |
| `published_by` / `actor` | Etiqueta opaca del contexto (no un usuario final) |
| Fixtures, ejemplos y catálogo del paquete | Sintéticos: nunca secretos ni datos personales reales |

El motor **no escanea** el contenido de las definiciones en busca de datos reales y no promete evitar que el consumidor los cargue.

**Registros de terceros.** Los registros propios del motor (`cawbase_content_engine.*`) no incluyen contenido (probado con marcadores únicos en `tests/test_a02_profiles.py::TestObservability` y `tests/test_a02_http.py::TestObservabilityOverHttp`). Los *drivers* y SQLAlchemy, en cambio, pueden imprimir parámetros enlazados (incluido el contenido de definiciones) si se activa su nivel `DEBUG` (se observó con `aiosqlite` en `DEBUG`). Esos registradores no deben activarse en `DEBUG` en producción; el servicio independiente solo configura sus propios registradores.

### 9.4 Retención, eliminación y alcance deshabilitado

| Dato | Retención en A02 | Desactivación / eliminación |
|---|---|---|
| `profiles`, `profile_categories`, `profile_sensitive_types` | indefinida | `disabled` (reversible) y `deleted` (lógica, terminal). **Sin purga física ni restauración** |
| `profile_versions` | indefinida; **inmutables** | no se modifican ni eliminan, tampoco al desactivar/eliminar el perfil o sus elementos |
| `scope_profile_defaults` | permanente por alcance | se quita la designación; la fila se conserva |
| Eventos de A02 en `admin_events` | indefinida (Q-04 de A01 sigue abierta) | sin borrado |

- **Eliminar lógicamente no borra el contenido**: las definiciones, ejemplos y traducciones permanecen en la fila (eliminada) y en **todas** las instantáneas que las incluyeron. Quien necesite retirar contenido sensible de la base debe esperar la política de purga (backlog B-A02-01): hoy solo es posible con intervención manual del DBA, fuera del motor.
- Las copias de seguridad conservan estos datos según la política del operador.
- **Alcance deshabilitado** (política de A01): las lecturas siguen permitidas, incluidas las versiones históricas; las escrituras se rechazan (`scope_disabled`); **resolver para un análisis nuevo se rechaza**. Consultar una versión histórica **no** equivale a autorizar su uso en una ejecución nueva.
- Un perfil `disabled`/`deleted` no puede seleccionarse para ejecuciones nuevas ni con versión explícita; sus versiones siguen consultables con `profiles:read`.
- El tratamiento de trabajos ya iniciados al desactivar/eliminar corresponde a A05; A02 no cancela nada retroactivamente (la selección se evalúa en la resolución, no durante la ejecución).

### 9.5 Riesgos residuales añadidos

- Un `profiles:read` expone todas las definiciones del alcance (sin granularidad por perfil: no hay credenciales por perfil).
- Un host con acceso directo a la base (§2.1) puede leer y alterar las instantáneas; la inmutabilidad la garantiza el código del motor, no el motor de base de datos.
- Las definiciones eliminadas lógicamente siguen en la base y en las versiones hasta que exista purga.
