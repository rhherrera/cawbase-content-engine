# API pública (Python y HTTP)

> **Estado: implementado en la versión 0.1.0 (rama `feature/a01-product-foundation`, sin commitear) y probado localmente en macOS; no probado en Linux/GitHub Actions todavía y no aceptado por el usuario.** Evidencia y alcance real de las pruebas: [informe de implementación](a01-implementation-report.md). Los nombres y firmas siguen el código (`src/cawbase_content_engine/`); los bloques de código son ejemplos ilustrativos. Las decisiones aprobadas están en [A01-decisions](decisions/A01-decisions.md) (D-nn). Las propuestas P-05, P-06, P-07, P-08, P-09, P-13 y P-14 fueron **resueltas** en la revisión 2 (D-23 a D-29) y P-20 fue adoptada como D-30.

Relacionado: [arquitectura](architecture.md) · [persistencia y migraciones](persistence-and-migrations.md) · [seguridad y aislamiento](security-and-isolation.md) · [instalación y operación](installation-and-operations.md).

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
5. **Errores no recuperables**: conexión perdida, transacción invalidada (p. ej. PostgreSQL ya abortada), o fallo al hacer `ROLLBACK TO` / `RELEASE` del savepoint ⇒ `TransactionBrokenError` (`transaction_broken`). Significa explícitamente que **el host no puede seguir asumiendo una transacción sana**: debe hacer rollback exterior y descartar la conexión. El motor no intenta reparar ni ocultar el estado.
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
| `database_unavailable` | `DatabaseUnavailableError` | 503 | Sin conexión, tiempo vencido, base ocupada (`retryable=True`) |
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
