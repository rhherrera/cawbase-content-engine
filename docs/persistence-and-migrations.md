# Persistencia y migraciones

> **Estado: implementado en la versión 0.1.0 (rama `feature/a01-product-foundation`, sin commitear) y probado localmente en macOS; no probado en Linux/GitHub Actions todavía y no aceptado por el usuario.** Evidencia y alcance real de las pruebas: [informe de implementación](a01-implementation-report.md). Los nombres de tablas y mecanismos descritos son los implementados (`src/cawbase_content_engine/persistence/`, `migrations/`).

Relacionado: [API pública](public-api.md) · [seguridad y aislamiento](security-and-isolation.md) · [instalación y operación](installation-and-operations.md).

## 1. Namespace y propiedad (D-15)

| Backend | Namespace | Ejemplo |
|---|---|---|
| PostgreSQL | schema fijo `cawbase_content_engine` | `cawbase_content_engine.scopes` |
| SQLite | prefijo fijo `cawbase_content_engine_` en **tablas, índices, restricciones con nombre y tabla de historial** | `cawbase_content_engine_scopes` |

- El namespace es una constante del paquete: **no configurable** (evita colisiones y simplifica la verificación).
- Una base contiene como máximo una instalación del motor (un namespace). Varias aplicaciones pueden convivir con sus propias tablas.
- El motor es dueño de sus tablas y de su historial de migraciones: `cawbase_content_engine.alembic_version` (PostgreSQL) / `cawbase_content_engine_alembic_version` (SQLite).
- **Nunca** se modifican ni eliminan objetos ajenos. El entorno de Alembic filtra objetos (`include_object`/`include_name`) a este namespace y la comparación automática no propone eliminar nada ajeno. Si el schema (PostgreSQL) o el prefijo (SQLite) ya contienen objetos que no pertenecen al motor (sin historial propio), la instalación **falla** con `schema_incompatible` en lugar de «adoptarlos».
- SQLite admite archivo exclusivo o compartido con el host; en ambos casos rige el prefijo.
- El código de negocio del host no consulta estas tablas directamente: usa la API pública.

## 2. Modelo persistente mínimo

Justificación: A01 necesita (a) un recurso cuya pertenencia a un alcance pueda comprobarse de verdad, (b) identidades HTTP con grants, (c) trazabilidad mínima. Todo lo demás (perfiles, categorías, tipos sensibles, trabajos, documentos) es de otras features. No se construye un sistema general de usuarios ni tenants.

Tipos lógicos: `text`, `int`, `bool`, `timestamp` (UTC; `timestamptz` en PostgreSQL; texto `YYYY-MM-DD HH:MM:SS.ffffff` en UTC en SQLite, convertido por el motor), `json` (`jsonb` / texto JSON), `bytes` (`bytea` / `blob`).

### 2.1 `engine_state` (fila única)

| Campo | Tipo | Restricciones |
|---|---|---|
| `id` | int | PK, `CHECK id = 1` |
| `schema_level` | int | NOT NULL, ≥ 1 (sube con cada migración) |
| `min_runtime_level` | int | NOT NULL (nivel mínimo de runtime capaz de operar este esquema) |
| `package_version_at_last_migration` | text | NOT NULL |
| `admin_initialized_at` | timestamp | NULL hasta el bootstrap inicial; **marca persistente** de que la instalación fue inicializada administrativamente (D-26). No se deriva de las claves activas |
| `admin_bootstrap_generation` | int | NOT NULL, ≥ 0; sube con cada bootstrap inicial (→1) y cada recuperación; sirve de centinela de serialización y de trazabilidad |
| `updated_at` | timestamp | NOT NULL |

### 2.2 `scopes` — recurso de alcance (P-13)

| Campo | Tipo | Restricciones |
|---|---|---|
| `scope_id` | text | PK; **inmutable**; 1–128 caracteres `[A-Za-z0-9._:-]`, el primero alfanumérico; aportado por el host u operador; opaco; conocerlo o elegirlo no concede acceso (D-28) |
| `display_name` | text | NOT NULL; 1–200 |
| `description` | text | NULL; ≤ 1000 |
| `metadata` | json | NOT NULL, objeto, ≤ 4 KiB serializado |
| `status` | text | NOT NULL, `CHECK IN ('active','disabled')` |
| `revision` | int | NOT NULL, ≥ 1; control de concurrencia optimista |
| `created_at`, `updated_at` | timestamp | NOT NULL |

Índice: `(status, scope_id)` para listados paginados por cursor.
**Pertenencia:** este recurso *es* el alcance; toda operación de alcance filtra por `scope_id = ctx.scope_id`. Por qué existe en A01: sin un recurso persistido por alcance no hay nada que aislar ni demostrar con acceso cruzado; es la menor entidad que lo permite. No sustituye a organizaciones/proyectos de CAWbase (que no existen aquí).

### 2.3 `clients`

| Campo | Tipo | Restricciones |
|---|---|---|
| `client_id` | text | PK; `cli_` + UUID hex |
| `name` | text | NOT NULL, UNIQUE; 1–64, `[a-z0-9-]` |
| `display_name` | text | NOT NULL; 1–200 |
| `status` | text | NOT NULL, `CHECK IN ('active','disabled')` |
| `created_at`, `updated_at` | timestamp | NOT NULL |

Un cliente agrupa claves de un consumidor; no es un usuario ni una cuenta con contraseña.

### 2.4 `api_keys`

| Campo | Tipo | Restricciones |
|---|---|---|
| `key_id` | text | PK; identificador público (parte del formato de la clave) |
| `client_id` | text | NOT NULL, FK → `clients` |
| `secret_hash` | bytes | NOT NULL; 32 bytes (SHA-256 del secreto); el secreto no se conserva |
| `hash_algorithm` | text | NOT NULL, p. ej. `sha256` (permite migrar el algoritmo) |
| `label` | text | NOT NULL; ≤ 100 |
| `issuance_request_id` | text | NOT NULL; UNIQUE con `client_id` |
| `issuance_fingerprint` | bytes | NOT NULL; huella canónica de los parámetros de emisión |
| `expires_at` | timestamp | NULL |
| `revoked_at` | timestamp | NULL |
| `created_at` | timestamp | NOT NULL |
| `created_by_key_id` | text | NULL (NULL ⇒ bootstrap/operador); sin FK para sobrevivir cambios |

Índices: UNIQUE (`client_id`, `issuance_request_id`); (`client_id`).
Estado derivado: `active` ⇔ `revoked_at IS NULL` y (`expires_at IS NULL` o futuro) y cliente `active`.

### 2.5 `api_key_admin_permissions` y `api_key_scope_grants`

Se separan para que el grant global y el de alcance no compartan columna anulable (la unicidad con `NULL` difiere entre motores).

| Tabla | Campos | Restricciones |
|---|---|---|
| `api_key_admin_permissions` | `key_id`, `permission` | PK (`key_id`, `permission`); FK → `api_keys`; `permission` en el catálogo `admin:*` |
| `api_key_scope_grants` | `key_id`, `scope_id`, `permission` | PK (`key_id`, `scope_id`, `permission`); FK → `api_keys` y → `scopes`; `permission` en el catálogo `scope:*` |

Índice: (`scope_id`) en `api_key_scope_grants`. Los grants son **inmutables** tras la emisión (cambiar ⇒ emitir otra clave y revocar la anterior). Límite: 100 grants de alcance por clave.

### 2.6 `admin_events` (P-12)

| Campo | Tipo | Restricciones |
|---|---|---|
| `event_id` | int/bigint | PK autoincremental |
| `occurred_at` | timestamp | NOT NULL |
| `action` | text | NOT NULL: `scope.created`, `scope.updated`, `scope.disabled`, `scope.enabled`, `client.created`, `client.disabled`, `client.enabled`, `api_key.issued`, `api_key.revoked`, `bootstrap.admin_key_issued`, `admin.recovery_key_issued` |
| `actor_key_id` | text | NULL (operador/Python sin clave) |
| `actor_label` | text | NULL; etiqueta opaca del contexto |
| `target_type`, `target_id` | text | NOT NULL |
| `scope_id` | text | NULL |
| `detail` | json | NOT NULL; solo nombres de campos modificados, nunca valores, secretos ni hashes |

Índices: (`occurred_at`); (`target_type`, `target_id`). Se escribe **en la misma transacción** que el cambio (o ambos o ninguno). Los intentos de autenticación fallidos no se persisten (se cuentan en métricas/logs) para no amplificar escrituras ni abrir un vector de DoS.

### 2.7 Relaciones

```text
clients 1──* api_keys 1──* api_key_admin_permissions
                     1──* api_key_scope_grants *──1 scopes
engine_state (única)      admin_events (anexa, sin FK)
```

### 2.8 Integridad aplicada por el motor

El motor valida referencias y pertenencia en código **además** de las FK: en SQLite las FK dependen de `PRAGMA foreign_keys=ON` por conexión, que en un engine inyectado puede no estar activa. Ninguna garantía de aislamiento depende solo de una FK ni de un `CHECK`.

## 3. Reglas de acceso a datos (contrato interno)

1. Toda consulta de un recurso de alcance incluye `scope_id` del contexto en el `WHERE`; no existe una consulta «por id solamente» expuesta a operaciones de alcance.
2. Sin sesiones globales ni estado compartido entre tareas. Cada operación obtiene su conexión o usa la `HostTransaction`.
3. Transacciones cortas. En A01 no hay inferencia ni E/S remota; cuando aparezca (A03+), se prohíbe mantener la transacción abierta durante ella ([API pública](public-api.md#6-transacciones)).
4. Nivel de aislamiento: el predeterminado del backend (PostgreSQL `READ COMMITTED`; SQLite serializable con un único escritor). La corrección bajo concurrencia se apoya en restricciones únicas, `UPDATE … WHERE` condicionales y la fila centinela, no en niveles de aislamiento especiales.
5. Los `IntegrityError` se traducen a errores tipados; nunca se propaga el texto del driver.

## 4. Migraciones

### 4.1 Reglas generales (D-16)

- Versionadas con el paquete, **solo hacia adelante** (sin `downgrade`). La recuperación ante un fallo o una migración errónea es **restaurar desde copia de seguridad**; no se promete rollback universal.
- Estilo expandir/contraer: una revisión no elimina ni renombra nada que un runtime anterior compatible necesite; los cambios incompatibles suben `min_runtime_level`.
- **Una ejecución de actualización = una transacción y un bloqueo para toda la secuencia pendiente** (comprobación del esquema, aplicación de todas las revisiones e historial). Es atómica: o quedan aplicadas todas las pendientes o ninguna. No hay commits intermedios, ni bloques de autocommit, ni conexiones independientes que escapen al bloqueo. Las migraciones de A01 deben ser compatibles con esta transacción exterior; si una migración futura necesita operaciones no transaccionales (p. ej. `CREATE INDEX CONCURRENTLY`), el contrato se amplía **antes** de incorporarla.
- Cuándo se ejecutan:

| Contexto | ¿Migra? |
|---|---|
| `pip install`, `import`, `ContentEngine(...)`, `start()` | **No** |
| Host embebido (incluido CAWbase worker/API) | **No**, salvo que su despliegue invoque la operación explícita |
| Lambda: cold start y requests | **No** |
| Adaptador HTTP montado en otro host (`create_router`) | **No** |
| Operación explícita (`migrations.upgrade` / `cawbase-content-engine migrate`) | **Sí**, durante el despliegue |
| **Servicio HTTP independiente** (`serve`) | **Sí, al arrancar**, antes de quedar listo para tráfico de negocio |

### 4.2 Operación explícita

```python
# Ejemplo ilustrativo (API implementada en 0.1.0; versión ejecutable verificada: examples/ y tests/)
report = await migrations.upgrade(config, lock_timeout=60.0, target="head")
# MigrationReport(from_revision=None, to_revision='0001', applied=('0001',), already_current=False, to_level=1, duration_s=…)
```

Se ejecuta con credenciales con permisos DDL (puede ser una URL distinta de la de runtime). Repetirla con el esquema ya en el objetivo devuelve `already_current=True` y no escribe. Con un engine inyectado usa una conexión del engine del host; el host es responsable de que sus credenciales permitan DDL.

### 4.3 Bloqueo y coordinación de migraciones (decisión D-24, antes P-06)

Procedimiento común del migrador, en **una sola conexión y una sola transacción**:

1. Abrir una conexión con credencial DDL y comenzar la transacción exterior.
2. Adquirir el bloqueo (abajo), con espera limitada por `lock_timeout`; si vence ⇒ rollback y `MigrationLockTimeoutError`.
3. **Con el bloqueo ya adquirido**, leer el nivel de esquema y **calcular entonces el plan** de revisiones pendientes. Nunca se decide aplicar revisiones con un plan calculado antes del bloqueo.
4. Aplicar todas las revisiones pendientes y escribir el historial (`alembic_version` y `engine_state`) dentro de la misma transacción.
5. `COMMIT` único, ejecutado por el migrador; el bloqueo se libera con la transacción. Cualquier fallo ⇒ `ROLLBACK` completo (nada queda aplicado) y `MigrationError`.

| Backend | Bloqueo | Detalle |
|---|---|---|
| PostgreSQL | `pg_try_advisory_xact_lock(<constante fija del paquete>)` **en esa misma conexión y transacción**, sondeado cada ≈0,5 s hasta `lock_timeout` | Bloqueo de transacción: se libera con commit, rollback o caída de la conexión. La conexión de migración debe usar `READ COMMITTED` para que la relectura tras el bloqueo vea lo confirmado por otro migrador (si el entorno impone un nivel superior, se rechaza). Compatible con *pools* en modo transacción mientras la secuencia dure una sola transacción |
| SQLite | `BEGIN IMMEDIATE` (transacción de escritura) con `busy_timeout = lock_timeout` | Un único escritor por archivo: mientras migra, **los escritores del host sobre el mismo archivo esperan**. Requiere el control transaccional con `BEGIN` real descrito en la [API pública](public-api.md#6-transacciones) |

Garantía y límites: la exclusión rige entre ejecuciones **cooperantes** del migrador del motor. No impide que otro administrador ejecute SQL arbitrario sobre el mismo schema o archivo. Alembic se usa con la conexión y transacción exteriores (`transaction_per_migration=False`, sin `begin_transaction` propio que confirme); que Alembic no confirme de forma intermedia se **verifica con la prueba de fallo intermedio** (AC-87).

Resultado para N arranques concurrentes: uno obtiene el bloqueo y aplica toda la secuencia; los demás esperan, adquieren el bloqueo después, releen el esquema, ven que no hay nada pendiente y terminan sin escribir. Si la espera vence, la migración falla o el esquema queda incompatible ⇒ el proceso **no** pasa a servir (ver [arranque del servicio](installation-and-operations.md#estados-de-arranque-y-readiness)).

El bootstrap/recuperación administrativa usan su propia serialización (fila `engine_state`), no el bloqueo de migración.

### 4.4 Permisos DDL y consecuencias del arranque automático

| Rol | Permisos mínimos (PostgreSQL) |
|---|---|
| Migración | `CREATE` en la base (para crear el schema la primera vez) o ser dueño del schema; `CREATE`/`ALTER`/`DROP` sobre objetos del schema |
| Runtime | `USAGE` en el schema; `SELECT`, `INSERT`, `UPDATE` sobre sus tablas (y `USAGE` en sus secuencias). Sin `DELETE` necesario en A01 |

Consecuencia: si el servicio independiente migra al arrancar con la **misma** credencial que usa en ejecución, esa credencial necesita DDL y un compromiso del servicio otorga capacidad de alterar el esquema. Mitigaciones (D-29): `migration_database_url` separada, usada solo durante la preparación y luego cerrada; o `migrate_on_start=false` con un trabajo de migración aparte. **Cerrar la conexión DDL no garantiza eliminar esas credenciales de la memoria ni del entorno del proceso** (variables de entorno, archivos montados, copias en memoria). Para una separación estricta de privilegios debe usarse migración externa y un runtime que **no tenga acceso** a las credenciales DDL. En SQLite no hay roles: quien abre el archivo puede alterar cualquier cosa; el aislamiento depende de permisos del sistema de archivos.

### 4.5 Compatibilidad (P-10)

| Condición al verificar (`start()`/readiness) | Resultado |
|---|---|
| Schema/prefijo sin `engine_state` | `schema_missing` |
| `schema_level` < el que requiere este runtime | `schema_incompatible` (migrar) |
| `min_runtime_level` > nivel de este runtime | `schema_incompatible` (actualizar el paquete) |
| `schema_level` ≥ requerido y `min_runtime_level` ≤ runtime | compatible (permite despliegue gradual: esquema nuevo + runtime anterior) |

Orden de despliegue recomendado: migrar → actualizar workers/API. Un runtime nuevo nunca opera sobre un esquema más viejo que el que requiere.

### 4.6 Instalación y actualización con objetos ajenos

La migración inicial crea solo los objetos del namespace (en PostgreSQL, `CREATE SCHEMA` si no existe). La verificación de aceptación incluye una base con tablas, vistas y secuencias ajenas (incluyendo nombres parecidos al prefijo) que deben permanecer intactas antes y después de instalar y actualizar.

## 5. Retención de los datos de A01

Ver [seguridad y aislamiento](security-and-isolation.md#retencion-revocacion-y-eliminacion).

## 5.1 Notas de implementación verificadas

- `install_transaction_control` es obligatorio para migrar sobre un engine SQLite **inyectado** (si no, `configuration_invalid`); para operar basta que el driver emita un `BEGIN` real (se comprueba en `start()`).
- En el servicio y la CLI la migración usa un engine propio sin pool (`NullPool`) y credenciales opcionales distintas (`migration_url`).
- La prueba de "sin commits intermedios" usa el `xmin` de una fila creada por la revisión anterior (PostgreSQL) y una segunda conexión que no ve DDL sin confirmar (SQLite): `tests/test_remaining_acceptance.py`.

## 6. Diferencias reales entre PostgreSQL y SQLite

SQLite **no** emula PostgreSQL (D-04). Cada diferencia relevante, tratada explícitamente:

| Aspecto | PostgreSQL | SQLite |
|---|---|---|
| Concurrencia de escritura | MVCC, múltiples escritores | **Un solo escritor** por archivo; el resto espera `busy_timeout` o falla con `database_unavailable (retryable)` |
| Modo recomendado | — | WAL (lectores no bloquean al escritor). En modo URL el motor lo intenta en cada conexión **de forma best-effort** (cambiar el modo exige un bloqueo exclusivo; si otro escritor lo retiene, una conexión posterior lo establece). Con engine inyectado lo decide el host |
| Aislamiento entre schemas | schema propio y roles con permisos | solo prefijo de nombres; sin roles; quien abre el archivo ve todo |
| DDL | transaccional, con bloqueos de tabla específicos | transaccional en la práctica pero toma el bloqueo de escritura de **todo** el archivo; `ALTER` limitado (modo «batch» de Alembic reconstruye tablas) |
| Bloqueo de migración | advisory lock de transacción en la misma conexión que migra | `BEGIN IMMEDIATE` (bloquea también al host) |
| Tipos | `timestamptz`, `jsonb`, `bytea` | texto `YYYY-MM-DD HH:MM:SS.ffffff` (UTC), texto JSON, `blob`; el motor convierte y valida |
| Claves foráneas | siempre aplicadas | solo con `PRAGMA foreign_keys=ON` por conexión |
| Transacciones del driver | estándar en asyncpg | `sqlite3`/`aiosqlite` emiten `BEGIN` de forma diferida en modo legado: se exige control transaccional con `BEGIN` real antes de `BEGIN IMMEDIATE` y de cualquier `SAVEPOINT`, y se comprueba sobre el driver (**riesgo técnico a verificar al implementar**) |
| Recuperación | copia lógica/física, PITR según despliegue | copia del archivo (usando la API de copia en línea o con la base quiescente); sin PITR |
| Conexiones | pool de red | archivo local; no apto para múltiples hosts por red (NFS no soportado) |
| Autoincrement/identidad | identidad | `INTEGER PRIMARY KEY` |
| Uso previsto en A01 | producción, base compartida con CAWbase | desarrollo, pruebas, instalaciones pequeñas de un solo nodo |

Las pruebas de concurrencia se escriben **por backend** con expectativas distintas, no con una sola aserción común.
