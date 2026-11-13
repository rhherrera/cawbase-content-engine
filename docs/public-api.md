# API pública (Python y HTTP)

> **Estado: A01 implementada en `0.1.0`, cerrada y aceptada por el usuario (commit aceptado `1b5ffab`; GitHub Actions aprobado en `main` y en `feature/a01-product-foundation`, según confirmación explícita del usuario). A02 implementada en `0.2.0`: evidencia y niveles en el [informe de A02](a02-implementation-report.md); A02 cerrada y aceptada por el usuario (commit aceptado `f4ad3e7`; GitHub Actions pasó sobre ese commit, según su confirmación explícita).** Evidencia y alcance real de las pruebas: [informe de implementación](a01-implementation-report.md). Los nombres y firmas siguen el código (`src/cawbase_content_engine/`); los bloques de código son ejemplos ilustrativos. Las decisiones aprobadas están en [A01-decisions](decisions/A01-decisions.md) (D-nn). Las propuestas P-05, P-06, P-07, P-08, P-09, P-13 y P-14 fueron **resueltas** en la revisión 2 (D-23 a D-29) y P-20 fue adoptada como D-30.

Relacionado: [perfiles y catálogo (A02)](profiles-and-catalog.md) · [arquitectura](architecture.md) · [persistencia y migraciones](persistence-and-migrations.md) · [seguridad y aislamiento](security-and-isolation.md) · [instalación y operación](installation-and-operations.md).

## 1. Superficie pública y versionado

- Superficie pública Python: los nombres exportados en `cawbase_content_engine.__all__` y los módulos `cawbase_content_engine.migrations`, `cawbase_content_engine.bootstrap` y `cawbase_content_engine.http`. Todo lo demás (`_internal`, `persistence`, tablas, repositorios) es privado: CAWbase y otros hosts **no** consultan las tablas ni importan módulos privados.
- `cawbase_content_engine.__version__` expone la versión del paquete.
- Versionado del paquete (elección adoptada para esta implementación): SemVer `0.x` hasta estabilizar; las rupturas de la API pública se documentan y se anuncian en cambios menores antes de `1.0`.
- HTTP versionado por ruta (`/v1`), independiente de la versión del paquete.
- Importar `cawbase_content_engine` **no tiene efectos** (D-11): no abre conexiones, no migra, no registra rutas, no carga modelos, no configura logging, no lee variables de entorno ni crea archivos o hilos. Ver [verificación de importación liviana](features/A01-base-del-producto-instalacion-independiente.md#17-matriz-de-aceptacion).
- Funciones con E/S: `async`. Funciones puramente computacionales (validación de `scope_id`, formato y parseo de claves, construcción de contextos) pueden ser síncronas.

## 2. Ciclo de vida

```python
# Ejemplo ilustrativo (API implementada en 0.1.0; versión ejecutable verificada: examples/ y tests/)
from cawbase_content_engine import ContentEngine, EngineConfig

config = EngineConfig(database_url="postgresql+asyncpg://…")  # solo valida; no hace E/S
engine = ContentEngine(config)  # no hace E/S
await engine.start()  # abre el pool (modo URL), verifica esquema; NO migra
...
await engine.aclose()  # libera lo que el motor posee; no toca un engine inyectado

# o: async with ContentEngine(config) as engine: ...
```

| Operación | Contrato |
|---|---|
| `ContentEngine(config)` | Sin E/S. Valida la configuración y falla con `InvalidConfigurationError`. |
| `await start()` | Modo URL: crea el `AsyncEngine` y su pool. Ambos modos: una consulta de verificación de esquema (`SchemaMissingError`, `SchemaIncompatibleError`, `DatabaseUnavailableError`). **No ejecuta migraciones.** Llamarlo dos veces es un no-op; tras `aclose()` falla con `EngineStateError`. |
| `await aclose()` | Modo URL: libera el pool (`dispose`). Modo engine inyectado: **no** hace `dispose` ni cierra conexiones del host. Repetible sin efecto. Las operaciones en curso reciben `EngineStateError` o terminan según el pool; el host debe quiescer antes. |
| `await schema_status()` | Devuelve `SchemaStatus` (sin migrar). Disponible en el paquete base (sin Alembic). |
| `await readiness()` / `liveness()` | `ReadinessReport` / `LivenessReport` con estados gruesos; sin URLs ni detalles de infraestructura. |

Propiedad (ownership):

| Recurso | URL (D-12.1) | Engine inyectado (D-12.2) |
|---|---|---|
| `AsyncEngine` y pool | motor | host |
| Cierre del engine | `aclose()` | host |
| Conexiones de operación | motor (las abre/cierra por operación) | motor las toma del engine del host y las devuelve al pool |
| Transacción de operación | motor (modo propio) | motor (modo propio) o host (modo provisto) |

Una `ContentEngine` es segura para usar desde múltiples tareas asyncio concurrentes (cada operación toma su propia conexión). Una `HostTransaction` no lo es.

## 2.1 Piezas públicas adicionales

| Nombre | Función |
|---|---|
| `install_transaction_control(async_engine)` | Helper **explícito, invocado por el host**, que hace que un engine `aiosqlite` emita un `BEGIN` real (y `BEGIN IMMEDIATE` con la opción `sqlite_begin="IMMEDIATE"`). El motor lo aplica a su propio engine en modo URL y **nunca** a un engine inyectado. |
| `check_schema(config)` | Inspección de solo lectura del esquema (`SchemaStatus`); no migra. |
| `SecretFileSink(path)` | Archivo exclusivo (`O_EXCL`, `0600`) para entregar el secreto de bootstrap/recuperación. |
| `ReadinessReport.state` | `ready`, `degraded_database`, `schema_incompatible`, `not_started`. |

## 3. Configuración (resumen)

El detalle (campos, precedencia, validación, secretos) está en [instalación y operación](installation-and-operations.md#configuracion). Regla de contrato: exactamente una de `database_url` o `async_engine`; ambas o ninguna ⇒ `InvalidConfigurationError` (código `configuration_invalid`).

## 4. Contextos autorizados

Dos tipos distintos, no intercambiables, para que una operación de alcance no acepte por error un contexto administrativo (y viceversa).

```python
# Ejemplo ilustrativo (API implementada en 0.1.0; versión ejecutable verificada: examples/ y tests/)
ctx = engine.trusted_scope_context(
    scope_id="proj-7f3a",  # opaco; lo determina el HOST, no un frontend
    permissions={ScopePermission.SCOPE_READ},
    actor="svc:worker-1",  # etiqueta opaca para auditoría/logs
)
admin = engine.trusted_admin_context(
    permissions={AdminPermission.ADMIN_SCOPES_WRITE, AdminPermission.ADMIN_CLIENTS_WRITE},
    scope_grants={
        "proj-7f3a": {ScopePermission.SCOPE_READ}
    },  # grants de alcance del emisor (regla de subconjunto); por defecto vacío
    actor="ops:deploy",
)
```

- Los nombres `trusted_*` indican que **el host asume la responsabilidad** de haber autenticado y autorizado antes de construirlos (D-17). El motor no puede verificarlo.
- Son objetos de valor inmutables, vinculados a la instancia de `ContentEngine` que los creó (otra instancia los rechaza con `PermissionDeniedError`). Esto previene confusiones, **no** falsificación: ver [límite del contexto Python](security-and-isolation.md#limite-del-contexto-python).
- `AdminContext` incluye `scope_grants` (los grants de alcance que posee el emisor): se usan solo para la [regla de delegación](security-and-isolation.md#32-reglas-de-delegacion-d-27) al emitir claves.
- `scope_id` no se acepta de un frontend sin que el host haya comprobado la pertenencia del usuario.
- El adaptador HTTP obtiene contextos de la clave autenticada: `principal = await engine.authenticate_api_key(raw)` → `principal.scope_context(scope_id)` / `principal.admin_context()`.

## 5. Operaciones

Las operaciones se obtienen de una fachada ligera (sin E/S al crearla):

```python
engine.scoped(ctx, transaction=None) -> ScopedOperations   # ctx: ScopeContext
engine.admin(admin_ctx, transaction=None) -> AdminOperations   # admin_ctx: AdminContext
```

### 5.1 `ScopedOperations` (un alcance)

| Método | Permiso | Resultado | Errores principales |
|---|---|---|---|
| `await get_scope(scope_id)` | `scope:read` | `Scope` | `ResourceNotAccessibleError` si `scope_id != ctx.scope_id` o no existe; `PermissionDeniedError` |
| `await update_scope(scope_id, *, expected_revision, display_name=…, description=…, metadata=…)` | `scope:write` | `Scope` | además `RevisionConflictError`, `ScopeDisabledError`, `ValidationFailedError` |

`scope_id` es parámetro explícito aunque el contexto ya lo fija: representa la regla que seguirán los recursos de A02 (el identificador llega de quien llama; **el motor valida pertenencia** contra el contexto en cada consulta, nunca confía en él).

### 5.2 `AdminOperations` (global)

| Método | Permiso | Resultado |
|---|---|---|
| `create_scope(scope_id, display_name, *, description=None, metadata=None)` | `admin:scopes:write` | `UpsertResult[Scope]` (`.created`) |
| `get_scope(scope_id)` / `list_scopes(*, status=None, limit=50, cursor=None)` | `admin:scopes:read` | `Scope` / `Page[Scope]` |
| `set_scope_status(scope_id, status)` | `admin:scopes:write` | `Scope` |
| `create_client(name, display_name)` | `admin:clients:write` | `UpsertResult[Client]` |
| `get_client(client_id)` / `list_clients(...)` | `admin:clients:read` | `Client` / `Page[Client]` |
| `set_client_status(client_id, status)` | `admin:clients:write` | `Client` |
| `issue_api_key(client_id, *, request_id, grants, label, expires_at=None)` | `admin:clients:write` + [reglas de delegación D-27](security-and-isolation.md#32-reglas-de-delegacion-d-27) (`admin:grants:write` para conceder alcances que el emisor no posee) | `IssuedApiKey` (con secreto, **solo aquí**) |
| `list_api_keys(client_id)` | `admin:clients:read` | `list[ApiKeyInfo]` |
| `revoke_api_key(key_id)` | `admin:clients:write` | `ApiKeyInfo` |
| `diagnostics()` | `admin:diagnostics:read` | `Diagnostics` |

`status` ∈ {`active`, `disabled`}. No existen operaciones de borrado físico en A01 (ver [retención](security-and-isolation.md#retencion-revocacion-y-eliminacion)).

### 5.3 Bootstrap inicial y recuperación administrativa (operador local)

Dos operaciones **distintas**, de operador con acceso autorizado a la base, **sin contexto, sin API key y sin ruta HTTP** (D-26). No forman una plataforma de administración: son las únicas dos operaciones locales de este tipo.

```python
# Ejemplo ilustrativo (API implementada en 0.1.0; versión ejecutable verificada: examples/ y tests/)
from cawbase_content_engine.bootstrap import create_initial_admin_key, recover_admin_access

issued = await create_initial_admin_key(engine, label="bootstrap")  # solo si la instalación nunca fue inicializada
issued = await recover_admin_access(engine, label="recovery-2026-11")  # solo si ya fue inicializada
```

| Función (CLI) | Precondición | Errores propios |
|---|---|---|
| `create_initial_admin_key` (`bootstrap-admin`) | esquema compatible y `admin_initialized_at IS NULL` | `BootstrapNotAllowedError` (`bootstrap_not_allowed`) si ya fue inicializada |
| `recover_admin_access` (`recover-admin`) | esquema compatible y `admin_initialized_at IS NOT NULL`; confirmación explícita del operador (`--confirm`) | `RecoveryNotAllowedError` (`recovery_not_allowed`) si nunca fue inicializada (usar bootstrap) |

Ambas devuelven un `IssuedApiKey` con el secreto **una sola vez** y un catálogo administrativo completo. Procedimiento, serialización, auditoría, entrega del secreto y pérdida de la salida: [seguridad y aislamiento](security-and-isolation.md#bootstrap-administrativo).

### 5.4 Migraciones (extra `migrations`)

```python
# Ejemplo ilustrativo (API implementada en 0.1.0; versión ejecutable verificada: examples/ y tests/)
from cawbase_content_engine.migrations import upgrade
report = await upgrade(config, lock_timeout=60.0)   # explícito; nunca implícito
```

Ver [persistencia y migraciones](persistence-and-migrations.md#4-migraciones).

### 5.5 Tipos de resultado (esquemas ilustrativos)

```python
class Scope:
    scope_id
    display_name
    description
    metadata
    status
    revision
    created_at
    updated_at


class Client:
    client_id
    name
    display_name
    status
    created_at
    updated_at


class ApiKeyInfo:
    key_id
    client_id
    label
    grants
    created_at
    expires_at
    revoked_at  # sin secreto, sin hash


class IssuedApiKey:
    info: ApiKeyInfo
    secret: SecretStr | None  # secret None en repetición idempotente


class ApiKeyGrants:
    admin: frozenset[AdminPermission]
    scopes: Mapping[str, frozenset[ScopePermission]]


class UpsertResult[T]:
    value: T
    created: bool


class Page[T]:
    items: list[T]
    next_cursor: str | None
```

`SecretStr` oculta el valor en `repr`/logs; el valor se obtiene con `.get_secret_value()` una sola vez por quien lo emite.

## 6. Transacciones

Dos modos (D-13), compatibles con SQLAlchemy asíncrono y con ambos backends. Compartir engine **no** implica compartir transacción.

### 6.1 Modo propio (predeterminado)

Si no se pasa `transaction`, cada operación abre su conexión, ejecuta en una transacción corta, confirma al terminar bien y revierte ante cualquier excepción; devuelve la conexión al pool. En SQLite las operaciones de escritura inician la transacción como escritura inmediata para evitar fallos por promoción de bloqueo.

```python
# Ejemplo ilustrativo (API implementada en 0.1.0; versión ejecutable verificada: examples/ y tests/)
await engine.admin(admin).create_scope("proj-1", "Proyecto 1")  # commit propio
```

Efecto: la operación es visible para otras conexiones al volver. **No** ve ni es vista por cambios no confirmados que el host tenga en otra conexión.

### 6.2 Modo provisto por el host

El host abre su transacción y pasa un `HostTransaction`:

```python
# Ejemplo ilustrativo (API implementada en 0.1.0; versión ejecutable verificada: examples/ y tests/)
from cawbase_content_engine import HostTransaction

async with host_async_engine.begin() as conn:  # el host controla commit/rollback
    tx = HostTransaction(conn)
    await engine.admin(admin, transaction=tx).create_scope("proj-1", "Proyecto 1")
    await host_own_table_insert(conn)  # otro trabajo del host, misma transacción
# sale sin excepción ⇒ el host confirma TODO; con excepción ⇒ revierte TODO, incluido el alcance
```

Contrato (decisión D-23, antes propuesta P-05):

1. `HostTransaction(conn)` envuelve una `AsyncConnection` con una transacción **ya activa**; si no, `TransactionStateError`.
2. La conexión debe pertenecer al **mismo `AsyncEngine` inyectado** con el que se creó la `ContentEngine`. Un motor en modo URL o un engine distinto ⇒ `IncompatibleTransactionError`.
3. Cada operación del motor se ejecuta dentro de su propio `SAVEPOINT`. El motor **nunca** llama `commit`, `rollback`, `close` ni `begin` sobre la transacción exterior (solo `ROLLBACK TO`/`RELEASE` de **su** savepoint).
4. **Errores recuperables** (validación, conflicto, `IntegrityError` traducido, permisos): se revierte el savepoint, se libera, la excepción tipada sube y — solo en este caso — la transacción del host sigue utilizable.
5. **Errores no recuperables**: conexión perdida, transacción invalidada (p. ej. PostgreSQL ya abortada), o fallo al hacer `ROLLBACK TO` / `RELEASE` del savepoint ⇒ `TransactionBrokenError` (`transaction_broken`). *Medido en `0.2.0` (A02):* un **interbloqueo** de PostgreSQL (SQLSTATE `40P01`) no es por sí mismo `transaction_broken`: aborta la sentencia, el `ROLLBACK TO SAVEPOINT` de la operación lo recupera y la transacción exterior del host sigue utilizable; la operación falla con el error transitorio `database_unavailable` (reintentable). Antes de `0.2.0` esa excepción caía en `internal_error` por la envoltura del driver (asyncpg). Significa explícitamente que **el host no puede seguir asumiendo una transacción sana**: debe hacer rollback exterior y descartar la conexión. El motor no intenta reparar ni ocultar el estado.
6. **Concurrencia sobre la conexión subyacente.** La guarda se asocia a la conexión de base de datos, no a la instancia de `HostTransaction`: dos envoltorios de la misma conexión no habilitan operaciones simultáneas (la segunda falla de inmediato con `TransactionStateError`, sin esperar). El contrato exige además que el **host no use esa conexión concurrentemente por fuera del motor** mientras una operación está en curso; el motor no puede detectar ese uso y no lo garantiza.
7. **SQLite.** La transacción exterior debe estar respaldada por un `BEGIN` real del driver **anterior** al savepoint; no basta con el estado lógico de SQLAlchemy. El motor lo comprueba sobre la conexión del driver (`in_transaction` real) antes de cada savepoint y, si no se cumple, falla con `IncompatibleTransactionError` sin ejecutar nada (en modo legado de `sqlite3`/`aiosqlite` el `BEGIN` se emite de forma diferida, y un `SAVEPOINT` fuera de transacción abriría una propia y su `RELEASE` confirmaría datos). Ver precondiciones del engine inyectado más abajo.
8. El motor no inicia inferencia ni llamadas remotas dentro de la transacción del host (en A01 no existen). El host no debe mantenerla abierta durante trabajo largo.
9. Las escrituras de la operación son visibles para el host dentro de su transacción; para otros, solo tras el `commit` del host.

**Precondiciones del host para un engine inyectado** (se validan en `start()` y en `HostTransaction`; incumplirlas produce rechazo explícito, nunca una modificación silenciosa del engine ajeno: el motor **no** instala listeners, no cambia `isolation_level` ni pragmas del engine del host):

| Precondición | Si no se cumple |
|---|---|
| `AsyncEngine` (no síncrono) con driver `asyncpg` o `aiosqlite` | `InvalidConfigurationError` |
| Para transacciones del host: la conexión procede de ese mismo engine | `IncompatibleTransactionError` |
| PostgreSQL: nivel de aislamiento `READ COMMITTED` (el predeterminado) para operaciones del motor | `InvalidConfigurationError` |
| SQLite: control transaccional del driver que emita `BEGIN` real al iniciar la transacción (el host lo configura; p. ej. `isolation_level=None` en el driver más un evento `begin` que emita `BEGIN`, a verificar al implementar; el modo `autocommit` de `sqlite3` de Python 3.12+ es otra vía a evaluar) | `IncompatibleTransactionError` / `InvalidConfigurationError` |
| SQLite: `busy_timeout` razonable; `foreign_keys=ON` recomendado (no es requisito: el motor valida referencias por código) | advertencia en diagnóstico |
| Pool con más de una conexión si se usará el modo propio mientras el host retiene una conexión | `DatabaseUnavailableError` por `pool_timeout` |

En modo URL el motor configura su propio engine con estas condiciones.

Ejemplo de error recuperable contenido:

```python
# Ejemplo ilustrativo (API implementada en 0.1.0; versión ejecutable verificada: examples/ y tests/)
async with host_async_engine.begin() as conn:
    tx = HostTransaction(conn)
    try:
        await engine.admin(admin, transaction=tx).create_scope("proj-1", "Distinto")  # ya existe con otros atributos
    except ConflictError:
        pass  # savepoint revertido; la transacción sigue sana
    except TransactionBrokenError:
        raise  # el host NO puede asumir una transacción sana: rollback exterior
    await host_own_table_insert(conn)
```

Advertencias de interacción (probadas en la [matriz de aceptación](features/A01-base-del-producto-instalacion-independiente.md#17-matriz-de-aceptacion)):

- **SQLite, modo propio con transacción de escritura abierta del host en otra conexión:** el motor esperará `busy_timeout` y fallará con `DatabaseUnavailableError(retryable=True)`; con un único escritor puede haber autobloqueo si el host espera al motor. Usar el modo provisto.
- **Pool de tamaño 1 y transacción del host abierta:** una operación en modo propio no obtiene conexión (`pool_timeout`).
- Compartir engine con transacciones separadas es válido pero no atómico entre ambas.

## 7. Idempotencia por operación

«Idempotente» se describe siempre por operación. Ninguna operación hace reintentos ocultos; la causa de fallo transitorio se señala con `retryable=True` y reintenta el llamador.

| Operación | Clave natural | Al repetir | Evita duplicados mediante |
|---|---|---|---|
| `start()` / `aclose()` | instancia | no-op | estado interno |
| Migrar (`upgrade`) | nivel de esquema | en el objetivo ⇒ sin cambios; concurrente: el segundo espera el bloqueo, **vuelve a leer** el esquema y no repite lo ya aplicado | una transacción y un bloqueo para toda la secuencia ([persistencia](persistence-and-migrations.md#4-migraciones)) |
| `create_scope` | `scope_id` | mismos atributos ⇒ devuelve el existente (`created=False`); atributos distintos ⇒ `ConflictError` (409). Si el alcance fue modificado después, la repetición original da `ConflictError`: el llamador debe leer | PK `scope_id` |
| `update_scope` | `scope_id` + `expected_revision` | **Estricta (D-30, adoptada):** `expected_revision`/`If-Match` se compara siempre. Distinta de la revisión actual ⇒ `RevisionConflictError` (412), **aunque los valores solicitados ya estén aplicados**; el llamador relee y comprueba. Revisión coincidente y valores iguales a los actuales ⇒ éxito sin escribir ni incrementar `revision`. Revisión coincidente y valores distintos ⇒ se aplica y `revision` += 1. Por tanto, reintentar tras un éxito cuya respuesta se perdió produce 412 (no duplica ni corrompe) | `UPDATE … WHERE scope_id=? AND revision=?` |
| `set_scope_status` / `set_client_status` | recurso | estado ya igual ⇒ éxito sin escritura ni evento | estado objetivo |
| `create_client` | `name` | igual que `create_scope` | UNIQUE `name` |
| `issue_api_key` | (`client_id`, `request_id`) | mismos parámetros (huella) ⇒ **no** crea otra clave: devuelve `ApiKeyInfo` con `secret=None` y marca de repetición (HTTP `200` + `Idempotent-Replay: true`); parámetros distintos ⇒ `IdempotencyConflictError` (409). Si el secreto original se perdió, el llamador revoca esa clave y emite otra con un `request_id` nuevo | UNIQUE (`client_id`, `issuance_request_id`) + huella canónica de parámetros |
| `revoke_api_key` | `key_id` | ya revocada ⇒ éxito; conserva el primer `revoked_at` | `UPDATE … WHERE revoked_at IS NULL` |
| Bootstrap inicial | — | **no idempotente**: con `admin_initialized_at` ya fijado falla (`BootstrapNotAllowedError`); concurrente: exactamente uno gana (los demás fallan) | fila `engine_state` bloqueada + marca persistente |
| Recuperación administrativa | — | **no idempotente y deliberadamente repetible**: cada ejecución confirmada emite otra clave y otro cliente `recovery-…`; concurrentes se serializan y ambas pueden tener éxito (auditadas por separado). No revoca nada | fila `engine_state` bloqueada; nombre de cliente único |
| `authenticate_api_key`, lecturas | — | sin efectos; repetir es seguro | — |

`request_id` de emisión: UUID generado por el llamador y reutilizado en reintentos; HTTP lo toma del encabezado `Idempotency-Key`.

## 8. Errores

Jerarquía (propuesta): `EngineError(code, message, retryable, request_id?)` y subclases. Los mensajes son genéricos y **no** incluyen secretos, URLs, rutas, SQL ni existencia de recursos de otros alcances.

| Código estable | Excepción Python | HTTP | Cuándo |
|---|---|---|---|
| `authentication_failed` | `AuthenticationError` | 401 | Clave ausente, malformada, desconocida, revocada, vencida o cliente deshabilitado (respuesta idéntica en todos los casos) |
| `permission_denied` | `PermissionDeniedError` | 403 | Autenticado y con algún grant sobre el recurso, pero sin el permiso requerido; contexto de otra instancia |
| `resource_not_accessible` | `ResourceNotAccessibleError` | 404 | Ruta de alcance: el alcance no existe **o** la clave no tiene ningún grant sobre él (indistinguibles) |
| `resource_not_found` | `ResourceNotFoundError` | 404 | Ruta administrativa: el recurso no existe (el llamador ya tiene permiso global) |
| `conflict` | `ConflictError` | 409 | Recurso existente con atributos distintos |
| `idempotency_conflict` | `IdempotencyConflictError` | 409 | Misma clave de emisión con parámetros distintos |
| `scope_disabled` | `ScopeDisabledError` | 409 | Escritura sobre alcance deshabilitado (lecturas siguen permitidas) |
| `revision_conflict` | `RevisionConflictError` | 412 | `If-Match`/`expected_revision` no coincide con la revisión actual (siempre, ver `update_scope`) |
| `precondition_required` | `ValidationFailedError` (subcódigo) | 428 | Falta `If-Match` en `PATCH` |
| `validation_failed` | `ValidationFailedError` | 422 | Campos inválidos, tamaños excedidos, grants inválidos |
| `limit_exceeded` | `LimitExceededError` / `PayloadTooLargeError` | 413 (cuerpo HTTP) / 422 (campos) | Cuerpo, metadatos, cantidad de grants, claves activas o página superan los límites |
| `configuration_invalid` | `InvalidConfigurationError` | — (no llega a HTTP: el servicio no arranca) | Configuración ambigua o inválida |
| `engine_not_ready` | `EngineStateError` | 503 | Motor no iniciado, cerrado o servicio no listo |
| `database_unavailable` | `DatabaseUnavailableError` | 503 | Sin conexión, tiempo vencido, base ocupada, e interbloqueo o fallo de serialización de PostgreSQL (SQLSTATE `40P01`/`40001`, desde `0.2.0`) (`retryable=True`) |
| `schema_missing` | `SchemaMissingError` | 503 | El esquema del motor no está instalado |
| `schema_incompatible` | `SchemaIncompatibleError` | 503 | Esquema fuera del rango compatible (anterior o requiere versión mayor) |
| `migration_failed` | `MigrationError` | — | Una migración falló (CLI/servicio: no listo) |
| `migration_lock_timeout` | `MigrationLockTimeoutError` | — | Venció la espera del bloqueo de migración (`retryable`) |
| `migration_permission_denied` | `MigrationPermissionError` | — | Las credenciales de migración carecen de permisos DDL |
| `transaction_state_invalid` | `TransactionStateError` | — | Precondición fallida antes de operar: transacción del host inactiva, o conexión en uso concurrente (por otro envoltorio) |
| `transaction_broken` | `TransactionBrokenError` | — | Conexión perdida, transacción invalidada o fallo al revertir/liberar el savepoint: **el host no puede asumir una transacción sana** |
| `transaction_incompatible` | `IncompatibleTransactionError` | — | Conexión de otro engine, motor en modo URL, o SQLite sin `BEGIN` real previo al savepoint |
| `bootstrap_not_allowed` | `BootstrapNotAllowedError` | — | La instalación ya fue inicializada administrativamente |
| `recovery_not_allowed` | `RecoveryNotAllowedError` | — | Recuperación solicitada en una instalación nunca inicializada |
| `secret_delivery_failed` | `SecretDeliveryError` | — | La credencial se emitió pero su secreto no pudo entregarse (no contiene el secreto; incluye `key_id`) |
| `internal_error` | `InternalError` | 500 | Falla no clasificada (detalle solo en logs) |

Reglas contra filtración de existencia (D-17, 404/403): en rutas de alcance, **sin ningún grant ⇒ 404 `resource_not_accessible`** idéntico exista o no el alcance; **con algún grant pero permiso insuficiente ⇒ 403**. Las respuestas no varían en cuerpo ni encabezados entre ambos casos de 404. En autenticación, un cliente deshabilitado, una clave vencida, revocada, malformada y una clave inexistente producen la misma respuesta 401. Se evitan diferencias de tiempo evitables (p. ej. se compara contra un hash ficticio si el `key_id` no existe), pero **no se promete duración idéntica ni ausencia absoluta de canales temporales**: comparar el hash en tiempo constante no hace constante la petición completa.

## 9. Contrato HTTP

Adaptador opcional (extra `http`, D-09/D-10). Dos formas de uso (decisión D-29):

- `cawbase_content_engine.http.create_router(engine)` → `APIRouter` para montar explícitamente. **No** migra, no registra nada por sí mismo, no recibe una aplicación.
- Servicio independiente (`cawbase-content-engine serve`): el lanzador valida la configuración, **migra** (salvo `migrate_on_start=false`), comprueba compatibilidad con las credenciales de runtime y **solo entonces** empieza a escuchar. Durante la migración no hay `/livez` ni `/readyz` ([arranque](installation-and-operations.md#estados-de-arranque-y-readiness)). La fábrica de aplicación que puede usarse con un servidor externo **no migra** nunca.

Convenciones: JSON (`application/json`), UTF-8; `Authorization: Bearer cce1.<key_id>.<secret>`; la clave **nunca** en query ni cuerpo; cada respuesta incluye `X-Request-ID` generado por el servidor; CORS deshabilitado por defecto (no es una API para navegadores); documentación OpenAPI desactivada por defecto en el servicio.

### 9.1 Rutas

| Método y ruta | Auth | Permiso | Éxito | Notas |
|---|---|---|---|---|
| `GET /livez` | no | — | `200 {"status":"alive"}` | No toca la base. No existe durante la migración de arranque (el servicio aún no escucha) |
| `GET /readyz` | no | — | `200 {"status":"ready"}` / `503 {"status":"not_ready","state":"<estado>"}` | Estados gruesos de [operación](installation-and-operations.md#estados-de-arranque-y-readiness) |
| `GET /v1/auth/self` | sí | cualquier clave válida | `200` identidad de la clave | Devuelve `key_id`, `client_id`, `grants`; nunca el secreto |
| `GET /v1/scopes/{scope_id}` | sí | `scope:read` | `200 Scope` + `ETag: "<revision>"` | 404 si sin grant |
| `PATCH /v1/scopes/{scope_id}` | sí | `scope:write` | `200 Scope` | `If-Match` obligatorio (428 si falta); **412 siempre que no coincida con la revisión actual**, incluso si los valores ya están aplicados (P-20); 409 si alcance deshabilitado |
| `POST /v1/admin/scopes` | sí | `admin:scopes:write` | `201` creado / `200` existente idéntico | 409 si difiere |
| `GET /v1/admin/scopes` | sí | `admin:scopes:read` | `200 Page` | `status`, `limit`, `cursor` |
| `GET /v1/admin/scopes/{scope_id}` | sí | `admin:scopes:read` | `200 Scope` | |
| `POST /v1/admin/scopes/{scope_id}/disable` · `/enable` | sí | `admin:scopes:write` | `200 Scope` | Estado objetivo; repetible |
| `POST /v1/admin/clients` | sí | `admin:clients:write` | `201` / `200` | Igual que alcances |
| `GET /v1/admin/clients` · `GET /v1/admin/clients/{client_id}` | sí | `admin:clients:read` | `200` | |
| `POST /v1/admin/clients/{client_id}/disable` · `/enable` | sí | `admin:clients:write` | `200 Client` | Deshabilitar invalida todas sus claves de inmediato |
| `POST /v1/admin/clients/{client_id}/api-keys` | sí | `admin:clients:write` + delegación (D-27) | `201` con secreto / `200` repetición sin secreto | `Idempotency-Key` obligatorio; 403 si los grants exceden lo delegable |
| `GET /v1/admin/clients/{client_id}/api-keys` | sí | `admin:clients:read` | `200 {"items": [ApiKeyInfo]}` | Sin secreto ni hash |
| `POST /v1/admin/api-keys/{key_id}/revoke` | sí | `admin:clients:write` | `200 ApiKeyInfo` | Repetible |
| `GET /v1/admin/diagnostics` | sí | `admin:diagnostics:read` | `200 Diagnostics` | Ver [operación](installation-and-operations.md#diagnostico) |

Orden de procesamiento: límite de tamaño de cuerpo → autenticación → resolución de permiso/alcance → validación del cuerpo → operación. Ningún recurso se consulta antes de autenticar y autorizar.

### 9.2 Schemas ilustrativos

```jsonc
// POST /v1/admin/scopes
{ "scope_id": "proj-7f3a", "display_name": "Proyecto 7", "description": null, "metadata": {"tier": "demo"} }

// 201 / 200
{ "scope_id": "proj-7f3a", "display_name": "Proyecto 7", "description": null,
  "metadata": {"tier": "demo"}, "status": "active", "revision": 1,
  "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z" }

// POST /v1/admin/clients/{client_id}/api-keys   (Idempotency-Key: 6f1c…)
{ "label": "worker-a", "expires_at": null,
  "grants": { "admin": [], "scopes": [ { "scope_id": "proj-7f3a", "permissions": ["scope:read","scope:write"] } ] } }

// 201 — el secreto aparece UNA sola vez
{ "key": { "key_id": "k_…", "client_id": "cli_…", "label": "worker-a", "grants": {…},
           "created_at": "…", "expires_at": null, "revoked_at": null },
  "secret": "cce1.k_….…" }

// 200 (repetición) — sin secreto
{ "key": {…}, "secret": null }
```

Los valores del ejemplo son ficticios. Los datos de `metadata` no deben contener información sensible ni personal ([retención](security-and-isolation.md#retencion-revocacion-y-eliminacion)).

### 9.3 Envoltorio de error

```json
{ "error": { "code": "resource_not_accessible", "message": "Resource not accessible.", "request_id": "…" } }
```

`401` incluye `WWW-Authenticate: Bearer`. El cuerpo nunca revela si un alcance existe, qué grants tiene la clave sobre otros alcances, ni detalles de infraestructura.

### 9.4 Códigos de estado generales

`200/201` éxito · `401` autenticación · `403` permiso · `404` no accesible/no encontrado · `409` conflicto · `412` revisión · `413` cuerpo demasiado grande · `422` validación · `428` precondición requerida · `429` reservado (limitación delegada al proxy en A01) · `500` interno · `503` no listo, base no disponible o esquema incompatible.

## 10. A02 — Perfiles, categorías y tipos sensibles (implementado)

> **Implementado en `0.2.0` (A02); cerrado y aceptado por el usuario (commit `f4ad3e7`).** Los nombres, firmas y rutas de esta sección siguen el código (`src/cawbase_content_engine/operations/profile_facade.py`, `http/router.py`); los bloques de código son ilustrativos. Modelo y semántica: [perfiles y catálogo](profiles-and-catalog.md). Decisiones: [A02-decisions](decisions/A02-decisions.md). Todo lo de A01 (§1–§9) sigue vigente y no cambia; esta sección **solo agrega**.

### 10.1 Convenciones heredadas de A01

- Mismas operaciones del núcleo desde Python y HTTP (D-07); API `async`; `engine.scoped(ctx, transaction=None) -> ScopedOperations`; `scope_id` explícito, validado contra `ctx.scope_id` (otro ⇒ `ResourceNotAccessibleError`). Las operaciones **no** están en `AdminOperations`: un `AdminContext` no da acceso a perfiles ([seguridad](security-and-isolation.md#9-a02--permisos-y-datos-de-perfiles-implementado)).
- Transacción propia o `HostTransaction` ([§6](#6-transacciones)): cada operación en un savepoint, sin commit del motor sobre la transacción del host. Cada operación es **una** transacción corta; nada se mantiene abierto entre llamadas HTTP.
- Paginación `limit`/`cursor` → `Page[T]` (por defecto 50, máximo 200). Orden: perfiles, categorías y tipos por su id ascendente; versiones por `version_number` **descendente**.
- Alcance `disabled`: lecturas permitidas; escrituras ⇒ `ScopeDisabledError` (409); **resolver** ⇒ `ScopeDisabledError`.
- Permisos de alcance **nuevos**: `profiles:read` y `profiles:write` ([catálogo](security-and-isolation.md#9-a02--permisos-y-datos-de-perfiles-implementado)). `ScopePermission.PROFILES_READ` / `PROFILES_WRITE`.

### 10.2 Semántica de edición (`PATCH`)

| Entrada | Efecto |
|---|---|
| Campo **omitido** (Python: `UNSET`) | sin cambio |
| Campo con valor | reemplaza el valor (las listas y objetos, como `examples`/`translations`, se reemplazan **completos**; no hay fusión) |
| `null` (Python: `None`) | solo en campos anulables (`description`, `translations`, `priority`): borra el valor. En campos no anulables (`display_name`, `name`, `definition`, `mode`, umbrales) ⇒ `validation_failed` |
| Campos desconocidos o de solo lectura (`profile_id`, `status`, `state`, `revision`, ids, `language`) | `validation_failed` |
| Ningún campo | `validation_failed` |
| Valores idénticos a los actuales, revisión correcta | `200`, sin escritura, sin incrementar revisión, sin evento |
| `expected_revision`/`If-Match` distinto de la actual | `revision_conflict` (412) **siempre**, aunque los valores ya estén aplicados |

HTTP: `If-Match: "<revision>"` obligatorio en `PATCH`, `PUT` y `DELETE` de recursos con revisión (428 `precondition_required` si falta). Las respuestas de `GET` y de escritura llevan `ETag: "<revision>"`. Las transiciones de estado (`disable`, `enable`, `delete`) son de **estado objetivo**: sin `If-Match`, repetibles, sin efecto si ya están en ese estado.

### 10.3 Firmas Python (`ScopedOperations`)

Todas `await`. `Page`, `UpsertResult` y `UNSET` como en A01. Tipos de resultado en §10.5.

**Perfiles** (`profiles:write` salvo lecturas)

| Método | Permiso | Resultado | Errores principales |
|---|---|---|---|
| `create_profile(scope_id, profile_id, display_name, *, description=None, language="es", initial_catalog="starter")` | write | `UpsertResult[Profile]` | `ConflictError`, `LimitExceededError`, `ScopeDisabledError`, `ValidationFailedError`. `initial_catalog` ∈ `"starter"` \| `"empty"` |
| `get_profile(scope_id, profile_id, *, include_deleted=False)` | read | `Profile` | `ResourceNotFoundError` |
| `list_profiles(scope_id, *, status=None, limit=50, cursor=None)` | read | `Page[Profile]` | `status`: colección de `active`/`disabled`/`deleted`; por defecto `active`+`disabled` |
| `update_profile(scope_id, profile_id, *, expected_revision, display_name=UNSET, description=UNSET)` | write | `Profile` | `RevisionConflictError`, `ProfileDisabledError` |
| `disable_profile` / `enable_profile` / `delete_profile` `(scope_id, profile_id)` | write | `Profile` | `ProfileIsDefaultError` (al desactivar/eliminar el predeterminado), `InvalidStateTransitionError` |

**Ranking/resolución**

| Método | Permiso | Resultado |
|---|---|---|
| `get_ranking_config(scope_id, profile_id)` | read | `RankingConfig` |
| `update_ranking_config(scope_id, profile_id, *, expected_revision, mode=UNSET, eligibility_threshold=UNSET, tie_tolerance=UNSET)` | write | `RankingConfig`; `ProfileInvalidError` si `mode="resolution"` incumple [§6.2](profiles-and-catalog.md#62-validación-de-la-resolución) |

**Categorías** y **tipos sensibles** (mismas operaciones; los tipos usan `sensitive_type` en el nombre y `type_id`, sin `priority`)

| Método | Permiso | Resultado |
|---|---|---|
| `create_category(scope_id, profile_id, category_id, name, definition, *, examples=None, translations=None, priority=None)` | write | `UpsertResult[Category]` |
| `get_category(scope_id, profile_id, category_id, *, include_deleted=False)` | read | `Category` |
| `list_categories(scope_id, profile_id, *, state=None, limit=50, cursor=None)` | read | `Page[Category]`; `state`: colección de `enabled`/`disabled`/`deleted`; por defecto `enabled`+`disabled` |
| `update_category(scope_id, profile_id, category_id, *, expected_revision, name=UNSET, definition=UNSET, examples=UNSET, translations=UNSET, priority=UNSET)` | write | `Category` |
| `disable_category` / `enable_category` / `delete_category` `(scope_id, profile_id, category_id)` | write | `Category` |
| `create_sensitive_type` / `get_sensitive_type` / `list_sensitive_types` / `update_sensitive_type` / `disable_sensitive_type` / `enable_sensitive_type` / `delete_sensitive_type` | igual | `SensitiveType` |

Errores comunes de elementos: `NameConflictError`, `PriorityConflictError` (categorías), `ConflictError` (id existente con atributos distintos o id reservado por un elemento eliminado), `LimitExceededError`, `ProfileDisabledError`, `ResourceNotFoundError` (perfil inexistente o eliminado, o elemento que no pertenece a ese perfil: respuesta idéntica).

**Versiones**

| Método | Permiso | Resultado |
|---|---|---|
| `publish_profile(scope_id, profile_id, *, expected_draft_revision)` | write | `UpsertResult[ProfileVersion]` (`created=False` si no hay cambios que publicar o es una repetición) · `ProfileInvalidError`, `RevisionConflictError`, `LimitExceededError`, `ProfileDisabledError` |
| `get_profile_version(scope_id, profile_id, version_number)` | read | `ProfileVersion` (con `snapshot`); funciona también con el perfil desactivado o eliminado |
| `list_profile_versions(scope_id, profile_id, *, limit=50, cursor=None)` | read | `Page[ProfileVersionSummary]` (sin `snapshot`) |

**Predeterminado y resolución**

| Método | Permiso | Resultado | Errores |
|---|---|---|---|
| `get_default_profile(scope_id)` | read | `DefaultProfile` (`profile_id=None` y `revision=0` si nunca se designó) | — |
| `set_default_profile(scope_id, profile_id, *, expected_revision)` | write | `DefaultProfile` | `ProfileNotSelectableError` (perfil `disabled`/`deleted`), `ProfileNotPublishedError`, `RevisionConflictError`. Mismo perfil ya predeterminado y revisión correcta ⇒ sin escritura |
| `clear_default_profile(scope_id, *, expected_revision)` | write | `DefaultProfile` | `RevisionConflictError`. Sin designación y revisión correcta ⇒ sin escritura |
| `resolve_profile(scope_id, *, profile_id=None, version_number=None)` | read | `ResolvedProfile` | `DefaultProfileNotSetError`, `ProfileNotSelectableError`, `ProfileNotPublishedError`, `ScopeDisabledError`, `ResourceNotFoundError`, `ValidationFailedError` |

**Catálogo del paquete** (sin E/S ni permisos; no pertenece a ningún alcance): `cawbase_content_engine.starter_catalog() -> StarterCatalog`, función síncrona que devuelve los datos versionados del paquete; se importa de forma perezosa (no pesa en la importación del paquete).

### 10.4 Rutas HTTP (`/v1`, extra `http`)

Todas bajo `/v1/scopes/{scope_id}`; autenticación por API key; sin grant alguno sobre `{scope_id}` ⇒ 404 `resource_not_accessible`; grant sin el permiso requerido ⇒ 403 `permission_denied`. Un recurso inexistente (o de otro perfil) dentro de un alcance accesible ⇒ 404 `resource_not_found`, idéntico en ambos casos. Mismo orden de procesamiento que A01 (tamaño de cuerpo → autenticación → permiso/alcance → validación → operación).

| Método y ruta | Permiso | Éxito | Notas |
|---|---|---|---|
| `POST /profiles` | write | `201` creado / `200` existente idéntico | cuerpo `{profile_id, display_name, description?, language?, initial_catalog?}`; `409 conflict` si difiere |
| `GET /profiles` | read | `200 Page` | `status` (repetible o lista separada por comas), `limit`, `cursor` |
| `GET /profiles/{profile_id}` | read | `200 Profile` + `ETag` | `?include_deleted=true` |
| `PATCH /profiles/{profile_id}` | write | `200 Profile` | `If-Match` |
| `POST /profiles/{profile_id}/disable` · `/enable` · `/delete` | write | `200 Profile` | estado objetivo |
| `GET /profiles/{profile_id}/ranking` | read | `200 RankingConfig` + `ETag` | |
| `PATCH /profiles/{profile_id}/ranking` | write | `200 RankingConfig` | `If-Match` (`ranking_revision`) |
| `POST /profiles/{profile_id}/categories` | write | `201` / `200` | cuerpo `{category_id, name, definition, examples?, translations?, priority?}` |
| `GET /profiles/{profile_id}/categories` | read | `200 Page` | `state` (repetible o lista), `limit`, `cursor` |
| `GET /profiles/{profile_id}/categories/{category_id}` | read | `200 Category` + `ETag` | `?include_deleted=true` |
| `PATCH /profiles/{profile_id}/categories/{category_id}` | write | `200 Category` | `If-Match` |
| `POST …/categories/{category_id}/disable` · `/enable` · `/delete` | write | `200 Category` | estado objetivo |
| `…/sensitive-types` (mismas seis rutas con `{type_id}`) | read/write | igual | sin `priority`; cuerpo de creación con `type_id` |
| `POST /profiles/{profile_id}/publish` | write | `201` versión creada / `200` + `Idempotent-Replay: true` si ya existía o no había cambios | cuerpo `{expected_draft_revision}`; `412` si no coincide; `422 profile_invalid` |
| `GET /profiles/{profile_id}/versions` | read | `200 Page` (sin `snapshot`) | |
| `GET /profiles/{profile_id}/versions/{version_number}` | read | `200 ProfileVersion` | funciona con el perfil desactivado/eliminado |
| `GET /default-profile` | read | `200 DefaultProfile` + `ETag` | siempre `200`, aunque no haya designación |
| `PUT /default-profile` | write | `200 DefaultProfile` | cuerpo `{profile_id}`; `If-Match` |
| `DELETE /default-profile` | write | `200 DefaultProfile` | quita la designación (no borra ningún perfil); `If-Match` |
| `GET /resolved-profile` | read | `200 ResolvedProfile` | `?profile_id=&version=`; ver [resolución](profiles-and-catalog.md#8-resolver-un-perfil-y-una-versión-publicados) |

No existen rutas administrativas (`/v1/admin/…`) de perfiles ni consultas entre alcances. No hay endpoints genéricos de filtros, búsqueda ni lotes. Límite de cuerpo: el mismo de A01 (64 KiB); los cuerpos de A02 (un elemento por solicitud) caben en él.

Ejemplos ilustrativos (valores ficticios):

```jsonc
// POST /v1/scopes/proj-7f3a/profiles
{ "profile_id": "general-es", "display_name": "Perfil general", "language": "es", "initial_catalog": "starter" }

// 201
{ "scope_id": "proj-7f3a", "profile_id": "general-es", "display_name": "Perfil general", "description": null,
  "language": "es", "status": "active", "revision": 1, "draft_revision": 1, "current_version_number": null,
  "is_default": false,
  "ranking": { "mode": "ranking", "eligibility_threshold": 0, "tie_tolerance": 0, "revision": 1 },
  "catalog_provenance": { "catalog_id": "cce-starter-sensitive-types", "catalog_version": 1, "copied_at": "2026-01-01T00:00:00Z" },
  "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z", "deleted_at": null }

// POST …/profiles/general-es/publish        { "expected_draft_revision": 1 }
// 201
{ "version": { "version_id": "pvr_…", "version_number": 1, "source_draft_revision": 1, "content_hash": "…",
               "capabilities": { "classification": false, "detection": true }, "snapshot_bytes": 5120,
               "published_at": "2026-01-01T00:00:00Z", "published_by": "key:k_…", "snapshot": { "…": "…" } },
  "created": true }

// 422 (publicar con resolución inválida; sin contenido de definiciones)
{ "error": { "code": "profile_invalid", "message": "Profile configuration is not valid.", "request_id": "…",
             "problems": [ { "code": "priority_missing", "category_id": "billing" } ] } }
```

Los valores de los ejemplos (ids, fechas, tamaños, hashes) son ficticios.

### 10.5 Tipos de resultado (esquemas ilustrativos)

```python
class Profile:
    scope_id; profile_id; display_name; description; language
    status  # active | disabled | deleted
    revision; draft_revision
    current_version_number  # None hasta la primera publicación
    is_default
    ranking: RankingConfig
    catalog_provenance: CatalogProvenance | None  # catalog_id, catalog_version, copied_at
    created_at; updated_at; deleted_at


class RankingConfig:
    mode  # ranking | resolution
    eligibility_threshold; tie_tolerance; revision


class Category:
    scope_id; profile_id; category_id; name; definition
    examples  # Examples(positive: tuple[str, ...], negative: tuple[str, ...])
    translations  # Mapping[str, Translation(name, definition)] | None
    priority  # int | None
    state  # enabled | disabled | deleted
    revision; created_at; updated_at; deleted_at


class SensitiveType:  # como Category, sin priority; con origin_entry: str | None
    ...


class ProfileVersionSummary:
    scope_id; profile_id; version_id; version_number; source_draft_revision
    content_hash  # hex SHA-256
    capabilities  # {"classification": bool, "detection": bool}
    snapshot_bytes; published_at; published_by


class ProfileVersion(ProfileVersionSummary):
    snapshot  # Mapping inmutable (profiles-and-catalog §7.1)


class DefaultProfile:
    scope_id; profile_id  # None si no hay
    revision; updated_at  # revision 0 / updated_at None si nunca se designó


class ResolvedProfile:
    selection  # "explicit" | "default"
    version: ProfileVersion
```

### 10.6 Códigos de error nuevos

Se agregan a la [tabla de errores](#8-errores); el resto de códigos se reutiliza con su significado actual (`resource_not_found` también para recursos hijos dentro de un alcance accesible).

| Código | Excepción | HTTP | Cuándo |
|---|---|---|---|
| `profile_disabled` | `ProfileDisabledError` | 409 | Edición del borrador, publicación o designación sobre un perfil `disabled` |
| `profile_not_selectable` | `ProfileNotSelectableError` | 409 | Resolver o designar un perfil `disabled`/`deleted` (también con versión explícita) |
| `profile_not_published` | `ProfileNotPublishedError` | 409 | Resolver o designar un perfil sin versión vigente |
| `profile_is_default` | `ProfileIsDefaultError` | 409 | Desactivar o eliminar el perfil predeterminado sin reemplazar/quitar antes la designación |
| `default_profile_not_set` | `DefaultProfileNotSetError` | 404 | Resolver sin `profile_id` cuando el alcance no tiene predeterminado |
| `invalid_state_transition` | `InvalidStateTransitionError` | 409 | `enable`/`disable` sobre un recurso eliminado |
| `name_conflict` | `NameConflictError` | 409 | Nombre ya usado por otro elemento no eliminado del mismo perfil |
| `priority_conflict` | `PriorityConflictError` | 409 | Prioridad ya usada por otra categoría `enabled` del perfil |
| `profile_invalid` | `ProfileInvalidError` | 422 | Publicar o pasar a `resolution` con configuración inválida. Lleva `problems`: lista de `{code, category_id?}` con códigos `no_enabled_elements`, `resolution_requires_categories`, `priority_missing`, `priority_duplicate`; **nunca** nombres ni definiciones |

Reutilizados: `revision_conflict` (412; también para `expected_draft_revision` desactualizada), `precondition_required` (428), `validation_failed` (422), `limit_exceeded` (422/413), `scope_disabled` (409), `conflict` (409), `permission_denied` (403), `resource_not_accessible` (404), `resource_not_found` (404). Los mensajes son genéricos: no incluyen nombres, definiciones, ejemplos ni valores.

### 10.7 Concurrencia, publicación e idempotencia

Complementa [§7](#7-idempotencia-por-operación). Cada fila describe qué ocurre al **repetir** la operación o **perder la respuesta**; no hay idempotencia genérica ni reintentos ocultos.

| Operación | Clave natural | Al repetir / perder la respuesta |
|---|---|---|
| `create_profile`, `create_category`, `create_sensitive_type` | `profile_id` (por alcance) / id (por perfil) | (los límites de cantidad se comprueban bajo el bloqueo del alcance o del perfil, [persistencia §7.4](persistence-and-migrations.md#74-reglas-de-acceso)) atributos idénticos a los **actuales** ⇒ devuelve el existente (`created=False`, HTTP 200). Distintos ⇒ `ConflictError`. Si se editó después, repetir la creación original da conflicto: el llamador lee. Un id eliminado sigue reservado (conflicto). En `create_profile`, `initial_catalog` idéntico a lo registrado (procedencia presente ⇔ `starter`) |
| `update_*`, `update_ranking_config`, `set_default_profile`, `clear_default_profile`, `PUT/DELETE default-profile` | recurso + revisión | **estricto (D-30)**: revisión desactualizada ⇒ 412 aunque los valores ya estén aplicados; tras perder la respuesta el llamador relee y reconcilia. Revisión correcta y sin cambio efectivo ⇒ sin `UPDATE`/`INSERT` de datos de negocio, sin incrementar revisiones y sin evento (se pudo adquirir un bloqueo, que no modifica el recurso) |
| `disable_*`, `enable_*`, `delete_*` | recurso | estado objetivo: ya en ese estado ⇒ éxito sin escritura, sin evento. `delete` repetido sobre eliminado ⇒ éxito. `enable`/`disable` sobre eliminado ⇒ `invalid_state_transition` |
| `publish_profile` | (perfil, `expected_draft_revision`) | `expected_draft_revision` obsoleta ⇒ 412 (siempre primero). Si coincide y la **configuración efectiva** (proyección, [modelo §7.3.1](profiles-and-catalog.md#731-proyección-de-configuración-efectiva)) es igual a la de la versión vigente ⇒ devuelve esa versión **sin alterarla** (`created=False`; conserva su `source_draft_revision`, que puede ser anterior a la revisión actual del borrador). Si difiere ⇒ crea la versión `n+1`. Respaldo: `UNIQUE (perfil, source_draft_revision)`. `content_hash` es solo integridad, no se usa para decidir |
| `resolve_profile`, lecturas | — | sin efectos |

**Revisión del conjunto editable.** `draft_revision` ([modelo §4](profiles-and-catalog.md#4-revisiones-e-a02-02)) cambia ante cualquier modificación efectiva del borrador; publicar la comprueba y captura una instantánea consistente en la misma transacción.

**Carreras (resolución).** Cada operación de escritura comienza **adquiriendo el bloqueo de serialización** que le corresponde y recién después comprueba estado y revisiones sobre los datos vigentes; adquirir un bloqueo no modifica nada. El detalle (filas bloqueadas por operación, orden de adquisición, `SELECT … FOR UPDATE` en PostgreSQL, transacción de escritura de A01 en SQLite, ausencia inicial de la fila del predeterminado) vive en [persistencia §7.4](persistence-and-migrations.md#74-reglas-de-acceso). Resumen: operaciones sobre un perfil bloquean **la fila del perfil**; crear perfiles y designar/quitar el predeterminado bloquean **la fila del alcance** (y, al designar, también la del perfil elegido), siempre en el orden alcance → perfil.

| Carrera | Resultado |
|---|---|
| Edición ∥ edición (elementos distintos) | se serializan; ambas aplican; cada una incrementa `draft_revision` |
| Edición ∥ edición (mismo elemento) | gana una; la otra recibe 412 (comparación y escritura atómicas por `revision` del elemento) |
| Edición ∥ publicación | si gana la publicación, la edición aplica después (el borrador supera la versión). Si gana la edición, la publicación con `expected_draft_revision` vieja recibe 412 |
| Publicación ∥ publicación | misma revisión ⇒ una crea y la otra devuelve esa versión; revisiones distintas ⇒ 412 en la rezagada |
| Desactivar/eliminar elemento ∥ publicación | igual que una edición |
| Desactivar/eliminar perfil ∥ edición/publicación | gana quien toma antes la fila; si gana la transición, la otra falla (`profile_disabled` o 404); si gana la publicación, la versión queda y el perfil pasa después a su estado |
| Desactivar/eliminar perfil ∥ designación como predeterminado | la fila del perfil arbitra: si gana la designación, la transición (que lee la designación tras bloquear el perfil) falla con `profile_is_default`; si gana la transición, la designación (que revalida el perfil tras bloquearlo) falla con `profile_not_selectable`. Nunca queda un predeterminado inválido |
| Designación ∥ designación (incluida la inicial, sin fila previa) | ambas toman el bloqueo del alcance: se serializan; la segunda compara la `revision` ya actualizada y recibe 412. `PK scope_id` es el respaldo: garantiza un solo predeterminado |
| Designación ∥ primera publicación | la designación revalida `current_version_number` tras bloquear el perfil; si pierde la carrera, `profile_not_published` |
| Crear perfiles distintos ∥ crear perfiles distintos (mismo alcance) | se serializan por el bloqueo del alcance; el límite de perfiles se comprueba y la inserción se hace bajo ese bloqueo: no se supera el límite |
| Crear elementos ∥ crear elementos / publicar (mismo perfil) | se serializan por el bloqueo del perfil; los límites de elementos y de versiones se comprueban bajo él |
| Deshabilitar alcance ∥ escritura | como A01: el estado del alcance se comprueba dentro de la transacción; una deshabilitación confirmada después no deshace una escritura ya confirmada |

Con `HostTransaction` las filas bloqueadas permanecen bloqueadas hasta que el host confirme o revierta: el host mantiene su transacción corta. Un error recuperable revierte solo el savepoint de la operación (incluido el incremento de `draft_revision`), pero **no libera** bloqueos tomados por operaciones anteriores de la misma transacción. Por eso **no se afirma ausencia de interbloqueos**: el orden fijo evita ciclos entre operaciones del motor, pero un host que retiene bloqueos o accede a las mismas tablas por su cuenta puede provocarlos. Un interbloqueo detectado por PostgreSQL se traduce a `database_unavailable` (reintentable), el savepoint de la operación se revierte y la transacción del host sigue siendo utilizable; **no** se clasifica como `transaction_broken` (ver [§6.2](#62-modo-provisto-por-el-host) y [persistencia §7.4.1](persistence-and-migrations.md#741-bloqueos-de-serialización)). El motor no reintenta por su cuenta.

### 10.8 Eventos de trazabilidad

Se reutiliza `admin_events` ([persistencia §2.6](persistence-and-migrations.md#26-admin_events-p-12)) sin renombrarlo: acciones `profile.created|updated|disabled|enabled|deleted|ranking_updated|published|default_set|default_cleared`, `category.created|updated|disabled|enabled|deleted`, `sensitive_type.<igual>`. `detail` contiene solo ids, nombres de campos modificados y revisiones/`version_number` (antes/después); **nunca** nombres, definiciones, ejemplos ni traducciones. Crear un perfil con catálogo inicial registra un solo evento (`profile.created`, con `catalog_version`), no uno por tipo copiado. Sin cambio efectivo ⇒ sin evento. Se escriben en la misma transacción que el cambio.

### 10.9 Notas de implementación (0.2.0)

- Las operaciones de A02 son métodos de `ScopedOperations` (mixin `ProfileOperations`); no hay clases ni módulos públicos nuevos salvo los tipos de resultado exportados y `starter_catalog()` (importación perezosa del catálogo).
- Un único centinela `UNSET` para «campo omitido» en todo el paquete (el mismo de `update_scope`).
- `Examples` guarda tuplas; `Translation` es `{name, definition}`; `ProfileVersion.snapshot` es un `dict` (no se protege contra mutación local: la inmutabilidad es la de la fila persistida).
- `ETag` también se devuelve en las respuestas `POST` que crean o devuelven un recurso con revisión. `ETag: "0"` en `GET /default-profile` significa «nunca designado»; `If-Match: "0"` se acepta solo en `PUT`/`DELETE` de `/default-profile`.
- El análisis JSON del adaptador HTTP rechaza `NaN` e `Infinity` en **todas** las rutas (también las de A01): 422 `validation_failed`.
- Los filtros `status`/`state` aceptan el parámetro repetido o una lista separada por comas; `include_deleted` solo existe en los `GET` individuales.
- Los límites de la [operación §11](installation-and-operations.md#111-límites) son campos de `Limits` (`max_profiles_per_scope`, `max_profile_rows_per_scope`, `max_elements_per_profile`, `max_element_rows_per_profile`, `max_versions_per_profile`, `max_snapshot_bytes`, `max_element_name`, `max_element_definition`, `max_example`, `max_examples_per_list`, `max_element_text_bytes`, `max_priority`).

## 11. A03 — Análisis de texto (implementado)

> **Implementado en `0.3.0` (A03); A03 cerrada y aceptada por el usuario ([informe](a03-implementation-report.md#0-estado-por-nivel-de-evidencia)).** Los nombres, firmas, rutas, permisos y códigos de esta sección siguen el código (`src/cawbase_content_engine/operations/analysis.py`, `inference/`, `http/router.py`); evidencia en el [informe de A03](a03-implementation-report.md). Todo lo de A01 (§1–§9) y A02 (§10) sigue vigente y **no cambia**; esta sección solo agrega. Contrato completo y ejemplos: [inferencia y análisis](inference-and-analysis.md). Decisiones: [A03-decisions](decisions/A03-decisions.md).

### 11.1 Firma Python (`ScopedOperations`)

```python
# Ejemplo ilustrativo — implementado en 0.3.0 (versión ejecutable: examples/analysis_host.py)
result = await engine.scoped(ctx).analyze_text(
    "acme-team",
    text="Contactar a Ana Ejemplo (ana.ejemplo@example.test).",
    profile_id=None,            # None ⇒ perfil predeterminado del scope (A02 §8)
    version_number=None,        # None ⇒ versión publicada vigente; sin profile_id ⇒ 422
    capabilities=("classification", "detection"),   # predeterminado: ambas
    context=None,               # contexto auxiliar: dato no confiable, no cubierto
    request_id=None,            # correlación opcional (HTTP pasa el X-Request-ID); no deduplica
)   # -> AnalysisResult
# El motor se compone con el adaptador (propiedad del host):  ContentEngine(config, inference=InferenceSetup(adapter))
```

Requiere `ScopeContext` con `analysis:execute`. Con `HostTransaction` ⇒ `transaction_incompatible` antes de invocar al adaptador. Sin `InferenceSetup` ⇒ `inference_not_configured`. `ContentEngine.inference_status()` informa de la capacidad de inferencia; `readiness()` no cambia.

### 11.2 Ruta HTTP

| Método y ruta | Permiso | Cuerpo | Respuesta |
|---|---|---|---|
| `POST /v1/scopes/{scope_id}/analyze-text` | grant `analysis:execute` en `{scope_id}` | `{text, profile_id?, version_number?, capabilities?, context?}` (≤ 64 KiB; campos desconocidos ⇒ 422) | `200` con el `AnalysisResult` (también si es `inconclusive`); `Cache-Control: no-store`; sin 202, sin `GET` de resultados, sin idempotencia duradera |

`request_id` (= `X-Request-ID`) sirve para correlación, no para deduplicación; repetir la solicitud la ejecuta de nuevo.

### 11.3 Tipos de resultado

`AnalysisResult`: `analysis_contract_version`, `request_id`, `status` (`completed`|`inconclusive`, derivado), `profile` (`scope_id`, `profile_id`, `version_id`, `version_number`, `content_hash`, `selection`; **sin** snapshot), `input` (`text_length`, `context_provided`), `provenance`, `classification`, `detection`. Cada capacidad tiene `status ∈ {not_requested, not_configured, completed, inconclusive}`; solo las ejecutadas llevan `coverage`, `ranking`/`resolution` o `findings`. Esquema y ejemplos: [contrato §6](inference-and-analysis.md#6-construcción-del-resultado-público).

### 11.4 Códigos de error nuevos

| Código | HTTP | Retryable | Cuándo |
|---|---|---|---|
| `inference_not_configured` | 503 | no | el motor no tiene adaptador de inferencia |
| `capabilities_not_configured` | 409 | no | ninguna capacidad pedida está configurada en la versión fijada |
| `inference_unavailable` | 503 | sí | adaptador apagado, cargando, inalcanzable o con fallo recuperable (`reason`) |
| `inference_timeout` | 504 | sí | el adaptador no respondió dentro del timeout del operador |
| `inference_output_invalid` | 502 | no | la salida del adaptador no pasó la validación (`problems` solo con códigos) |
| `inference_fixture_not_found` | 501 | no | el mock no tiene fixture para la entrada |

Se reutilizan `permission_denied`, `resource_not_accessible`, `validation_failed`, `limit_exceeded` (también HTTP 413 por cuerpo > 64 KiB), `scope_disabled`, `default_profile_not_set`, `resource_not_found`, `profile_not_selectable`, `profile_not_published`, `transaction_incompatible` y `internal_error`. Tabla completa (con excepción Python y si se invocó al adaptador): [contrato §8.1](inference-and-analysis.md#81-tabla-de-errores-se-reutilizan-jerarquía-engineerror-y-envoltorio). Estados HTTP añadidos al catálogo de §9.4 solo para esta ruta: `501`, `502`, `504`.

## 12. A04 — Sin operaciones nuevas

> **A04 está CERRADA Y ACEPTADA por el usuario como investigación de viabilidad acotada ([informe de viabilidad](a04-feasibility-report.md)); no hay soporte documental productivo en `0.3.0`.** **A04 no añade operaciones, rutas, permisos, errores ni tipos públicos disponibles.** El catálogo sigue en 11 permisos; `AnalysisResult` y `analyze_text` no cambian. Las estructuras de [procesamiento de documentos](document-processing.md) son conceptuales (propuesto / no disponible); cualquier endpoint documental pertenece a A05 y posteriores.

## 13. A05 — Trabajos, archivos y ciclo de vida (implementado en 0.4.0; pendiente de aceptación)

> **IMPLEMENTADO en `0.4.0` (A05), pendiente de revisión y aceptación del usuario.** Evidencia por ambiente y trazabilidad de criterios en el [informe de A05](a05-implementation-report.md); **no aceptada**. La viabilidad documental se limita al subconjunto conservador de [§11.2](jobs-files-and-lifecycle.md#112-matriz-de-soporte-a05-solo-extracción-y-localización). Contrato completo: [trabajos, archivos y ciclo de vida](jobs-files-and-lifecycle.md); decisiones: [A05-decisions](decisions/A05-decisions.md). Todo lo de §1–§12 sigue vigente y **no cambia**: `analyze_text`, su ruta, `AnalysisResult`, los límites de A03 y el cuerpo JSON de 64 KiB de las rutas existentes se conservan.

- **Composición**: `ContentEngine(config, inference=…, storage=StorageSetup(...), documents=DocumentSetup(...), jobs=JobsSetup(...))`; sin efectos al crear; ningún worker se inicia implícitamente ([contrato §14](jobs-files-and-lifecycle.md#14-api-python)).
- **`ScopedOperations`**: `upload_file`, `register_external_file`, `get_file`, `list_files`, `open_file_content`, `delete_file`, `submit_document_analysis`, `submit_text_analysis`, `get_job`, `list_jobs`, `cancel_job`, `get_job_result`, `open_job_artifact`, `delete_job_data` (permisos en [contrato §16](jobs-files-and-lifecycle.md#16-permisos-elección-técnica-e-a05-12)).
- **Superficies locales confiables** (sin HTTP ni `ScopeContext`; `engine.job_worker(id)`, `engine.maintenance()`): `engine.job_worker(...)` (`claim`, `run_job`, `run_claimed`), `ReferenceExecutor` y `engine.maintenance()` (`list_runnable_jobs`, `recover_expired_leases`, `run_cleanup`, `purge_admin_events`).
- **HTTP**: `POST/GET /v1/scopes/{scope_id}/files`, `GET …/files/{file_id}[/content]`, `DELETE …/files/{file_id}`, `POST …/document-analysis-jobs`, `POST …/text-analysis-jobs`, `GET …/jobs[/{job_id}]`, `POST …/jobs/{job_id}/cancel`, `GET …/jobs/{job_id}/result|source-map`, `DELETE …/jobs/{job_id}/data` ([contrato §15](jobs-files-and-lifecycle.md#15-http)). La carga binaria tiene su propio límite (25 MiB) y transmisión por stream; `202` + `Location` al aceptar un trabajo; `200` + `Idempotent-Replay: true` al repetir.
- **Idempotencia** por `Idempotency-Key` en cargas y envíos ([contrato §8](jobs-files-and-lifecycle.md#8-idempotencia-y-fijación-de-ejecución)); complementa §7 sin cambiar las filas existentes.
- **Errores nuevos**: [contrato §17](jobs-files-and-lifecycle.md#17-errores-nuevos) (`storage_not_configured`, `documents_not_configured`, `storage_unavailable`, `stored_object_unavailable`, `upload_in_progress`, `upload_aborted`, `content_mismatch`, `file_not_usable`, `file_expiring`, `quota_exceeded` 429, `job_not_succeeded`, `job_not_terminal`, `job_data_expired` 410). Las descargas de originales y resultados se verifican (hash y tamaño) antes de enviar encabezados ([contrato §3.7](jobs-files-and-lifecycle.md#37-carga-y-descarga)).
- **`HostTransaction`**: permitida solo en operaciones breves de base; rechazada antes de efectos en cargas, registros externos, envío de texto y lecturas de contenido ([contrato §19](jobs-files-and-lifecycle.md#19-transacciones-por-operación-hosttransaction)).
