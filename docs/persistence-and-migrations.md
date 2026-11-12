# Persistencia y migraciones

> **Estado: A01 implementada en `0.1.0`, cerrada y aceptada por el usuario (commit aceptado `1b5ffab`; GitHub Actions aprobado en `main` y en `feature/a01-product-foundation`, según confirmación explícita del usuario). A02 implementada en `0.2.0`: evidencia y niveles en el [informe de A02](a02-implementation-report.md); A02 cerrada y aceptada por el usuario (commit aceptado `f4ad3e7`; GitHub Actions pasó sobre ese commit, según su confirmación explícita).** Evidencia y alcance real de las pruebas: [informe de implementación](a01-implementation-report.md). Los nombres de tablas y mecanismos descritos son los implementados (`src/cawbase_content_engine/persistence/`, `migrations/`).

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

## 7. A02 — Modelo persistente y migración (implementado)

> **Implementado en `0.2.0` (A02); cerrado y aceptado por el usuario (commit `f4ad3e7`).** Las tablas y la revisión `0002_profiles` son las del código (`persistence/tables.py`, `migrations/versions/0002_profiles.py`); el nivel de esquema es 2. Semántica: [perfiles y catálogo](profiles-and-catalog.md); decisiones: [A02-decisions](decisions/A02-decisions.md). Todo lo de §1–§6 (namespaces, propiedad, bloqueo, una transacción, objetos ajenos) **se mantiene sin cambios**.

Mismos tipos lógicos que §2 (`text`, `int`, `bool`, `timestamp`, `json`, `bytes`) más `real` (`double precision` / `REAL`). Todos los nombres reciben el namespace fijo (schema o prefijo, §1), incluidos índices y restricciones. Las tablas heredan el modelo de pertenencia: **`scope_id` forma parte de toda clave primaria y toda clave foránea**.

### 7.1 Tablas

**`profiles`** — perfil y configuración editable de nivel perfil.

| Campo | Tipo | Restricciones |
|---|---|---|
| `scope_id` | text | PK (1/2); FK → `scopes` |
| `profile_id` | text | PK (2/2); `^[a-z0-9][a-z0-9_-]{0,63}$` validado por el motor + `CHECK length BETWEEN 1 AND 64` |
| `display_name` | text | NOT NULL; 1–200 |
| `description` | text | NULL; ≤ 1000 |
| `language` | text | NOT NULL, `CHECK IN ('es','en')`; inmutable |
| `status` | text | NOT NULL, `CHECK IN ('active','disabled','deleted')` |
| `deleted_at` | timestamp | NULL; `CHECK (status = 'deleted') = (deleted_at IS NOT NULL)` |
| `revision` | int | NOT NULL, ≥ 1 |
| `draft_revision` | int | NOT NULL, ≥ 1 |
| `ranking_mode` | text | NOT NULL, `CHECK IN ('ranking','resolution')` |
| `eligibility_threshold`, `tie_tolerance` | real | NOT NULL, `CHECK BETWEEN 0 AND 1` (el motor rechaza NaN/±∞ antes: SQLite almacenaría NaN como NULL) |
| `ranking_revision` | int | NOT NULL, ≥ 1 |
| `current_version_number` | int | NULL hasta la primera publicación; ≥ 1; solo crece |
| `catalog_id`, `catalog_version`, `catalog_copied_at` | text, int, timestamp | los tres NULL o los tres informados (`CHECK`) |
| `created_at`, `updated_at` | timestamp | NOT NULL |

Índice: (`scope_id`, `status`, `profile_id`) para listados.

**`profile_categories`** y **`profile_sensitive_types`**

| Campo | Tipo | Restricciones |
|---|---|---|
| `scope_id`, `profile_id` | text | FK compuesta → `profiles (scope_id, profile_id)` |
| `category_id` / `type_id` | text | PK = (`scope_id`, `profile_id`, id) |
| `name` / `name_key` | text | `name` 1–100; `name_key` = NFKC + *casefold* de `name` (lo calcula el motor) |
| `definition` | text | NOT NULL; 1–1500 |
| `examples` | json | NOT NULL; `{"positive":[…],"negative":[…]}` |
| `translations` | json | NULL |
| `priority` | int | solo categorías; NULL o `CHECK BETWEEN 1 AND 10000` |
| `origin_entry` | text | solo tipos; NULL o clave del catálogo |
| `state` | text | NOT NULL, `CHECK IN ('enabled','disabled','deleted')` |
| `deleted_at` | timestamp | NULL; coherente con `state` |
| `revision` | int | NOT NULL, ≥ 1 |
| `created_at`, `updated_at` | timestamp | NOT NULL |

Índices **parciales únicos** (PostgreSQL y SQLite ≥ 3.8; el piso 3.38 los soporta):

- `UNIQUE (scope_id, profile_id, name_key) WHERE state <> 'deleted'` — nombre único entre los no eliminados; el id eliminado sigue reservado por la PK, su nombre queda libre.
- Solo categorías: `UNIQUE (scope_id, profile_id, priority) WHERE state = 'enabled' AND priority IS NOT NULL`.
- Listados: (`scope_id`, `profile_id`, `state`, id).

**`profile_versions`** — instantáneas inmutables.

| Campo | Tipo | Restricciones |
|---|---|---|
| `scope_id`, `profile_id` | text | FK compuesta → `profiles` |
| `version_number` | int | PK = (`scope_id`, `profile_id`, `version_number`); ≥ 1 |
| `version_id` | text | NOT NULL, UNIQUE global; `pvr_` + 32 hex |
| `source_draft_revision` | int | NOT NULL; **UNIQUE** (`scope_id`, `profile_id`, `source_draft_revision`) |
| `snapshot_schema_version` | int | NOT NULL |
| `snapshot` | json | NOT NULL; ≤ 1 MiB canónico |
| `content_hash` | bytes | NOT NULL; 32 bytes (SHA-256 de la serialización canónica de la instantánea **completa** almacenada; integridad, no igualdad de configuración: [modelo §7.3.1](profiles-and-catalog.md#731-proyección-de-configuración-efectiva)) |
| `snapshot_bytes` | int | NOT NULL |
| `capabilities` | json | NOT NULL (`classification`, `detection`; derivado, para listar sin leer la instantánea) |
| `published_at` | timestamp | NOT NULL |
| `published_by` | text | NULL; etiqueta opaca del contexto |

Sin `UPDATE` ni `DELETE`: el código del motor solo hace `INSERT` y `SELECT` sobre esta tabla (verificable por prueba estática). Un rol de runtime puede restringirse a `SELECT, INSERT` sobre ella como defensa adicional (no es requisito: quien comparte credenciales con el motor puede alterar cualquier tabla; ver el [límite del contexto](security-and-isolation.md#limite-del-contexto-python)).

**`scope_profile_defaults`** — predeterminado por scope.

| Campo | Tipo | Restricciones |
|---|---|---|
| `scope_id` | text | PK; FK → `scopes`. **Una fila por scope ⇒ a lo sumo un predeterminado** |
| `profile_id` | text | NULL (sin designación); FK compuesta (`scope_id`, `profile_id`) → `profiles` |
| `revision` | int | NOT NULL, ≥ 1; sube en cada designación o remoción |
| `updated_at` | timestamp | NOT NULL |

Sin fila ⇒ nunca designado (`revision` lógica 0). La fila se crea en la primera designación y se conserva (con `profile_id` NULL al quitarla) para que `revision` sea monótona.

`admin_events` ([§2.6](#26-admin_events-p-12)) se reutiliza: se agregan acciones (`profile.*`, `category.*`, `sensitive_type.*`, [API pública §10.8](public-api.md#108-eventos-de-trazabilidad)); la columna `action` es texto sin `CHECK`, así que no se altera la tabla. `target_type` ∈ `profile`, `category`, `sensitive_type`, `profile_version`, `default_profile`; `target_id` = id del recurso (`profile_id` o `profile_id/elemento`).

### 7.2 Relaciones

```text
scopes 1──* profiles 1──* profile_categories
                     1──* profile_sensitive_types
                     1──* profile_versions
scopes 1──0..1 scope_profile_defaults ──► profiles (scope_id, profile_id)
```

### 7.3 Integridad aplicada por el motor

Igual que §2.8: en SQLite las FK dependen de `PRAGMA foreign_keys=ON` por conexión y un engine inyectado puede no activarlas; por eso **ninguna garantía depende solo de una FK o `CHECK`**. El motor valida en código: pertenencia (`scope_id` + `profile_id`) en cada consulta, estados permitidos, límites de cantidad, forma de los ids, rango/finitud de números y reglas de §6.2 del modelo. Las restricciones únicas parciales y la PK de `scope_profile_defaults` sí son garantía de base y se prueban bajo concurrencia en ambos backends.

### 7.4 Reglas de acceso

Se aplican las de §3 y además:

1. Toda consulta de perfiles incluye `scope_id = ctx.scope_id`; toda consulta de categorías, tipos y versiones incluye `scope_id` **y** `profile_id` en el `WHERE` (nunca «por id de elemento solamente»). Un elemento de otro perfil es indistinguible de uno inexistente.
2. **Bloqueo primero, comprobación después.** Cada escritura adquiere el bloqueo de serialización que figura en §7.4.1 y **recién entonces** lee estado, revisiones y límites sobre los datos vigentes. Un bloqueo no es una escritura: si no hay cambio efectivo, la operación termina sin `UPDATE`/`INSERT` de datos de negocio, sin incrementar revisiones y sin evento. Los cambios efectivos usan además `UPDATE … WHERE revision = ?` (comparación y escritura atómicas, control optimista estricto de A01) como defensa. Ver [API pública §10.7](public-api.md#107-concurrencia-publicación-e-idempotencia).
3. La publicación lee categorías y tipos `enabled` **dentro de la misma transacción**, después de bloquear la fila del perfil, de modo que la instantánea es consistente (PostgreSQL `READ COMMITTED` basta porque todo escritor del conjunto editable pasa antes por ese bloqueo).
4. Ninguna transacción abarca llamadas HTTP, inferencia ni E/S remota. Publicar es CPU y una escritura de ≤ 1 MiB.
5. Los `IntegrityError` (PK, índices parciales) se traducen a `ConflictError`, `NameConflictError` o `PriorityConflictError`; nunca se propaga texto del driver.
6. **Lecturas coherentes.** `resolve_profile` y las consultas de versiones no toman bloqueos: leen en una transacción corta (`READ COMMITTED`, cada sentencia ve lo confirmado). La coherencia no depende de un bloqueo: las versiones son inmutables; el estado y `current_version_number` del perfil se leen en una sola fila; la designación se lee antes que el perfil, de modo que si el perfil se desactiva entre ambas lecturas el resultado es `profile_not_selectable` (la verdad vigente al releer); no se devuelve la versión de un perfil que ya se observa desactivado o eliminado. Una resolución exitosa describe el estado en el instante de la lectura: **no autoriza indefinidamente** una ejecución futura ni crea trabajos (A05 decide qué hacer con las ya iniciadas).
7. Dialectos: PostgreSQL usa `jsonb`; SQLite texto JSON. El `content_hash` se calcula en el motor sobre la serialización canónica, no con funciones de la base, de modo que es idéntico en ambos backends.

<a id="741-bloqueos-de-serialización"></a>
#### 7.4.1 Bloqueos de serialización (E-A02-08)

Solución mínima, sin servicio de locks ni infraestructura nueva: se bloquean **filas que ya existen** (`scopes` y `profiles`).

| Backend | Cómo se bloquea |
|---|---|
| PostgreSQL | `SELECT 1 FROM … WHERE <clave completa> FOR UPDATE` sobre la fila de `profiles`; `… FOR NO KEY UPDATE` sobre la fila de `scopes` (no estorba las comprobaciones de FK de otras tablas). Sin `UPDATE` ficticio. Tras obtenerlo se relee en `READ COMMITTED` (A01 lo exige) y se comprueba sobre datos vigentes |
| SQLite | No hay bloqueo de fila: la **transacción de escritura de A01** ya serializa a todos los escritores. En modo propio, las operaciones de escritura empiezan como escritura inmediata ([API §6.1](public-api.md#61-modo-propio-predeterminado)); con `HostTransaction`, la operación usa la transacción y el savepoint del host y **no abre otra transacción exterior**: se aplican las precondiciones de A01 (`BEGIN` real previo; el host decide `sqlite_begin="IMMEDIATE"`). Si el host inició la transacción en modo diferido y la promoción a escritura choca con otro escritor, falla con el error transitorio de base ocupada de A01 (`database_unavailable`, reintentable) |

| Operación | Filas bloqueadas (en este orden) | Comprobado bajo el bloqueo |
|---|---|---|
| `create_profile` | `scopes[scope]` | alcance activo; límites de perfiles (no eliminados y filas totales: `COUNT` bajo el bloqueo); id libre |
| `update_profile`, `disable/enable/delete_profile` | `profiles[perfil]` | estado y revisión; al desactivar/eliminar: lectura (sin bloqueo) de `scope_profile_defaults` ⇒ `profile_is_default` |
| Crear/editar/cambiar estado de categoría o tipo; `update_ranking_config` | `profiles[perfil]` | perfil `active`; límites de elementos (no eliminados y filas totales); revisión del elemento; unicidad |
| `publish_profile` | `profiles[perfil]` | estado; `draft_revision`; proyección frente a la vigente; límite de versiones; tamaño de instantánea |
| `set_default_profile` | `scopes[scope]` → `profiles[perfil elegido]` | perfil `active`, no eliminado, con `current_version_number`; revisión de la designación |
| `clear_default_profile` | `scopes[scope]` | revisión de la designación |
| Lecturas, `resolve_profile` | ninguna | — |

Reglas:

- **Orden de adquisición:** alcance → perfil(es) → (filas hijas solo mediante `UPDATE … WHERE revision = ?`, sin bloqueos explícitos). Nunca se toma el bloqueo del alcance después del de un perfil. Si una operación necesitara bloquear varios perfiles, se bloquean en orden ascendente de `profile_id`. Las operaciones actuales bloquean como máximo un perfil.
- **Ausencia inicial de `scope_profile_defaults`:** como el bloqueo del alcance serializa toda designación, no hace falta bloquear una fila inexistente. Con el bloqueo tomado se lee la fila: si falta, la revisión esperada es 0; `set` inserta con `revision = 1` y `clear` con la fila ausente y revisión esperada 0 no escribe. La `PK scope_id` es el respaldo: un `INSERT` duplicado se traduce a `revision_conflict`. Reemplazar (A→B) bloquea el perfil B, no el A; al quitar o reemplazar no se toca el perfil anterior.
- **Desactivar/eliminar vs. designar:** la transición bloquea el perfil y lee la designación; la designación bloquea el alcance y luego el perfil y revalida su estado. Quien toma primero el perfil gana y el otro ve el estado confirmado: **nunca queda designado un perfil desactivado o eliminado**. No hay ciclo porque la transición no pide el bloqueo del alcance.
- **Límites de cantidad:** nunca con un `COUNT` fuera del bloqueo común (alcance para perfiles, perfil para elementos y versiones); transacciones distintas no pueden superarlos concurrentemente.
- **Sin garantía universal de ausencia de interbloqueos.** El orden fijo evita ciclos entre operaciones del motor, pero una `HostTransaction` puede conservar bloqueos de operaciones anteriores y el host accede a la base por su cuenta. **Comportamiento medido** (PostgreSQL real, driver `asyncpg`; `tests/test_a02_concurrency.py::TestDeadlocks`): PostgreSQL aborta una sentencia (SQLSTATE `40P01`); el driver la entrega envuelta en una excepción de SQLAlchemy que no es `OperationalError`, por lo que hasta `0.1.0` caía en `internal_error`. Desde `0.2.0` `translate()` clasifica `40P01` y `40001` como el error transitorio `database_unavailable` (`retryable=True`). Con `HostTransaction` el `ROLLBACK TO SAVEPOINT` de la operación **sí** recupera la transacción (PostgreSQL permite revertir a un savepoint tras un error de sentencia) y la transacción exterior del host sigue utilizable; no se clasifica como `transaction_broken`, que se reserva (A01, D-23) para conexión perdida o invalidada y fallo de la recuperación o liberación del savepoint. El motor no confirma ni revierte la transacción del host y no reintenta. Los bloqueos tomados antes por el host o por operaciones anteriores se conservan hasta que el host termine su transacción: la víctima puede reintentar, y el sobreviviente espera a que la transacción de la víctima finalice. Se mantienen los plazos (`statement_timeout`, `busy_timeout`).

### 7.5 Migración desde A01 (`0002_profiles`)

Revisión **`0002_profiles`** (implementada), solo hacia adelante, estilo **expandir** (no altera ni elimina nada existente):

1. Crea las cinco tablas de §7.1 con sus índices y restricciones, en orden de dependencia. No usa `ALTER` de tablas existentes (evita reconstrucciones «batch» de SQLite).
2. No migra datos: los alcances, clientes, claves, grants y eventos de A01 quedan intactos; **no** se crean perfiles para los alcances existentes y su comportamiento no cambia. Un alcance sin perfiles es un estado válido.
3. Sube `schema_level` de 1 a 2. `min_runtime_level` **permanece en 1**: un runtime `0.1.0` puede seguir operando sobre el esquema 2 porque ignora tablas que no conoce.
4. Se ejecuta con el procedimiento de §4 sin cambios: una transacción, un bloqueo, plan calculado tras el bloqueo, atómica, repetirla es `already_current`. Un arranque concurrente con varias revisiones pendientes (A01 y A02 juntas, p. ej. una base vacía) aplica `0001` y `0002` en la misma transacción.
5. Objetos ajenos: la verificación de instalación/actualización de §4.6 se repite en la actualización 1→2 (tablas, vistas y secuencias ajenas, incluso con nombres próximos al prefijo, permanecen intactas).

Compatibilidad schema/runtime ([§4.5](#45-compatibilidad-p-10)) tras A02: el runtime de A02 requiere `schema_level ≥ 2` (con 1 ⇒ `schema_incompatible`, hay que migrar antes); `min_runtime_level = 1` mantiene el despliegue gradual «migrar → actualizar». **La compatibilidad con A01 es condicionada**: un runtime A01 puede operar sobre las tablas ampliadas (ignora las nuevas) con los datos y permisos que conoce, nada más.

**Permisos nuevos y despliegue mixto.** Los permisos `profiles:read|write` se guardan como filas de `api_key_scope_grants` (columna `permission` de texto libre; no hay `CHECK`, así que no hay cambio de esquema). **El runtime de A01 (`0.1.0`) falla al cargar los grants de una clave que contenga un permiso desconocido** (`ScopePermission(...)` lanza `ValueError`). Por tanto: **no deben emitirse claves con `profiles:*` hasta que todos los runtimes del despliegue estén en la versión de A02.** Las claves ya emitidas no se amplían ni se reinterpretan (los grants son inmutables, D-27): para dar acceso HTTP a perfiles se emite una clave nueva. Precisiones:

- El fallo puede ocurrir **al autenticar** esa clave y también en **operaciones administrativas que carguen sus grants** (p. ej. listar las claves de su cliente).
- Tras emitir claves con permisos nuevos, **volver a ejecutar A01 no es un rollback transparente**: el esquema 2 es compatible, pero esas claves y los listados que las incluyan fallan en A01.
- **Revocar no lo corrige.** Revocar una clave fija `revoked_at` y no elimina los grants almacenados (son inmutables y se conservan para auditoría): un runtime A01 sigue fallando al cargar permisos desconocidos en los listados administrativos, **incluso de claves revocadas**. Probado reproduciendo el cargador de A01 en `tests/test_a02_migration.py::TestCompatibilityWithA01`. No se eliminan grants para ocultarlo. Restaurar el estado anterior sigue siendo restaurar desde copia ([§4.1](#41-reglas-generales-d-16)) o actualizar todos los runtimes.
- Cambiar el código de A02 **no corrige los binarios A01 ya desplegados**.
- Endurecer la carga de permisos desconocidos permanece en el backlog (B-A02-12), **fuera de los requisitos de A02**. No se modifica A01 ni se añaden mecanismos de despliegue.

Operación: el despliegue de A02 sigue «migrar (credencial DDL) → actualizar API/workers». El servicio independiente migra al arrancar como en A01. No se introduce purga ni infraestructura nueva; el crecimiento de `profile_versions` es acotado por los [límites](installation-and-operations.md#11-a02--límites-y-observabilidad-implementado) (no hay retención automática).

### 7.6 Diferencias PostgreSQL / SQLite relevantes para A02

| Aspecto | PostgreSQL | SQLite |
|---|---|---|
| Arbitraje de carreras | `SELECT … FOR UPDATE` / `FOR NO KEY UPDATE` de la fila de `profiles` / `scopes` ([§7.4.1](#741-bloqueos-de-serialización)); los demás esperan y releen (`READ COMMITTED`) | la transacción de escritura de A01 (único escritor): el segundo escritor espera `busy_timeout` o falla `database_unavailable (retryable)`; sin bloqueo de fila |
| Índices parciales únicos | sí | sí (≥ 3.8; piso 3.38) |
| Números reales | `double precision`; JSON numérico exacto | `REAL`/texto; NaN → NULL (por eso se rechaza antes) |
| FK compuestas | siempre aplicadas | solo con `foreign_keys=ON` (el motor valida igual) |
| Tamaño de `snapshot` | `jsonb` (reordena claves) | texto; el `content_hash` no depende del almacenamiento |

Las pruebas de concurrencia se escriben **por backend** (§6, última línea).

## 8. A03 — Sin modelo persistente (implementado)

> **Implementado en `0.3.0` (A03); A03 cerrada y aceptada por el usuario ([informe](a03-implementation-report.md#0-estado-por-nivel-de-evidencia)).** Todo lo de §1–§7 sigue vigente sin cambios.

- A03 **no almacena** texto, contexto, resultados ni solicitudes: no hay tablas de análisis ni migración. No se crea una migración vacía.
- Niveles: `schema_level = 2` y `min_runtime_level = 1` **no cambian**; la cabeza de migraciones sigue en `0002_profiles`. Los niveles de código (`SCHEMA_LEVEL`, `RUNTIME_LEVEL`) tampoco cambian por A03.
- El permiso `analysis:execute` se guarda como una cadena en `api_key_scope_grants.permission` (columna `TEXT` sin restricción de valores): no requiere cambio de esquema. La compatibilidad con runtimes anteriores depende de los **grants**, no del nivel: ver [seguridad §10.3](security-and-isolation.md#103-compatibilidad-y-despliegue-del-permiso-nuevo).
- La lectura de preparación (selección de la versión publicada) usa las tablas de A02 con el mismo patrón de acceso (`scope_id`, `profile_id`) en una transacción propia de solo lectura, que termina antes de la inferencia ([contrato §3](inference-and-analysis.md#3-preparación-versiones-y-transacciones)).
- No se escribe contenido ni resultados en `admin_events`.

## 9. A04 — Sin persistencia ni migraciones (solo documentado)

> **A04 está CERRADA Y ACEPTADA por el usuario como investigación de viabilidad acotada ([informe de viabilidad](a04-feasibility-report.md)); no hay soporte documental productivo en `0.3.0`.** **No hay tablas, migraciones ni cambios de esquema** (`schema_level = 2`, `min_runtime_level = 1` se mantienen). Los inventarios, mapas e informes de A04 son estructuras conceptuales y artefactos de experimento; su persistencia, la retención de originales/resultados y las referencias opacas corresponden a A05.

## 10. A05 — Modelo de trabajos y archivos y migración `0003` (especificado; no implementado)

> **ESPECIFICADO / NO IMPLEMENTADO (A05, pendiente de revisión).** Nada de esta sección existe en `0.3.0` (propuesto / no disponible); la versión no cambia. Detalle: [contrato §18](jobs-files-and-lifecycle.md#18-persistencia-y-migración-0003-propuesta). Todo lo de §1–§9 sigue vigente.

- Tablas propuestas (namespace fijo; `scope_id` en toda PK y FK; ninguna garantía depende solo de FK/`CHECK`): `stored_files`, `file_validations`, `jobs`, `job_attempts`, `job_artifacts`, `idempotency_records`, `scope_job_fences`, `scope_usage`, `engine_usage`. Ninguna guarda texto, contexto, resultados ni mapas: el contenido vive en el storage administrado.
- Migración **`0003_jobs_files`** propuesta: aditiva, solo hacia adelante, sin `ALTER` de tablas existentes, sin migrar ni purgar datos; `schema_level` 2 → **3**; `min_runtime_level` **permanece en 1** con justificación semántica (un runtime anterior no puede aceptar ni publicar trabajos y sus cambios de scope se detectan por `scopes.revision`, [contrato §9.2](jobs-files-and-lifecycle.md#92-serialización-frente-a-deshabilitarrehabilitar-fence-del-scope)). Mismo procedimiento de §4 (una transacción, un bloqueo, objetos ajenos intactos).
- Orden de despliegue: migrar → actualizar todos los runtimes → emitir claves con permisos nuevos y habilitar workers/limpieza.
- Concurrencia por backend: PostgreSQL — reclamación con `FOR UPDATE SKIP LOCKED`, scope `FOR SHARE` contra deshabilitaciones concurrentes, `READ COMMITTED` como en A01; SQLite — transacción de escritura inmediata (un único escritor). Autoridad temporal: reloj de la base ([contrato §6.1](jobs-files-and-lifecycle.md#61-autoridad-temporal)).
- Permisos de base: el runtime A05 necesita además `DELETE` sobre sus propias tablas; la purga de `admin_events` necesita `DELETE` sobre esa tabla (recomendado solo para el rol de mantenimiento). La migración no purga datos.
