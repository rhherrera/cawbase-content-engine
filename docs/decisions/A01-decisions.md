# A01 — Registro de decisiones

> **Estado: implementado en la versión 0.1.0; probado en macOS y en Linux local (Docker, PostgreSQL 15–18 + SQLite); NO ejecutado en GitHub Actions; no aceptado por el usuario.** Ver el [informe de implementación](../a01-implementation-report.md).

Este registro separa tres categorías. No mezclarlas.

- **Decisiones aprobadas (D-nn):** fijadas por el usuario. Cambiarlas requiere decisión explícita del usuario.
- **Propuestas técnicas (P-nn):** opciones concretas redactadas para esta especificación. **Pendientes de revisión**; no son decisiones aceptadas.
- **Preguntas abiertas (Q-nn):** sin propuesta suficiente o que dependen de otro hito.

Contratos que usan estas decisiones: [feature A01](../features/A01-base-del-producto-instalacion-independiente.md), [arquitectura](../architecture.md), [API pública](../public-api.md), [persistencia y migraciones](../persistence-and-migrations.md), [seguridad y aislamiento](../security-and-isolation.md), [instalación y operación](../installation-and-operations.md).

## 0. Historial de revisiones de esta especificación

| Rev. | Contenido |
|---|---|
| 1 | Primera redacción de la especificación A01 con decisiones D-01 a D-22 y propuestas P-01 a P-19. |
| 2 | El usuario resolvió P-05, P-06, P-07, P-08, P-09, P-13 y P-14 (ahora D-23 a D-29, ver §4.1), cerró Q-03, aclaró Q-02 y pidió una única regla coherente para `update_scope` (nueva propuesta P-20, pendiente). Se conservan las propuestas originales (§4.1) por trazabilidad. |

| 3 | Implementación de A01. P-20 adoptada (D-30), GitHub Actions adoptado (D-31), elecciones técnicas P-01 a P-04, P-10 a P-12 y P-15 a P-19 **adoptadas para esta implementación** (§4.3) y decisiones de implementación registradas (§4.4). |

| 4 | Revisión de validación (base `d091b49`): validación en Linux local, PostgreSQL 16, piso de SQLite, AC-26/AC-101/AC-07, CI y artefactos, mediciones. Correcciones I-10 a I-15 (§4.4) y la pregunta Q-06 (§5). No cambia decisiones aprobadas. |

Esto numera revisiones **de esta especificación**; no es una revisión del playbook.

## 1. Fuente y precedencia

- Referencia: `STAGE_3_PLAYBOOK.md`, **revisión 6**, fecha de actualización 11 de noviembre de 2026. Versión comprobada al redactar: la misma (el archivo externo y la copia local `docs/STAGE_3_PLAYBOOK.md` son idénticos byte a byte).
- Las decisiones del usuario recibidas con el encargo de A01 son posteriores a la revisión 6 y **prevalecen** sobre ella.
- Este registro **no crea una nueva revisión global ni declara actualizada la fuente canónica**. La fuente canónica vive en el proyecto Backend; la copia local en `docs/` (ignorada por Git, no versionada) es una copia identificada por fecha y revisión, y no debe evolucionar por separado.
- Lo que sigue sin cambios respecto de la revisión 6 permanece vigente (por ejemplo: exclusiones de A01, mock de inferencia en A03, librería con extras, CAWbase como consumidor).

## 2. Decisiones aprobadas

| ID | Decisión |
|---|---|
| D-01 | Repositorio y distribución: `cawbase-content-engine`. Import Python: `cawbase_content_engine`. |
| D-02 | Python 3.13 como base inicial. |
| D-03 | Desarrollo en macOS; aceptación automatizada en Linux. |
| D-04 | PostgreSQL y SQLite soportados. SQLite no se considera emulación de PostgreSQL. |
| D-05 | No se afirma disponibilidad del nombre en PyPI ni publicación alguna. Licencia definitiva y publicación pública quedan para su hito (A10). |
| D-06 | API pública asíncrona para operaciones con E/S; funciones puramente computacionales pueden ser síncronas. |
| D-07 | Una única lógica de negocio, reutilizada desde Python y HTTP. |
| D-08 | Parte liviana (configuración, contratos, persistencia) y extras separados para capacidades opcionales. |
| D-09 | HTTP se implementa en A01, pero es opcional para instalar y usar el paquete. |
| D-10 | El núcleo no depende de FastAPI ni exige recibir una aplicación web. |
| D-11 | Importar no registra rutas, no abre conexiones, no migra, no carga modelos ni configura logging global. Inicialización y cierre son explícitos. |
| D-12 | Conexión: URL (el motor crea y administra su engine) **o** engine asíncrono inyectado (propiedad y cierre del host). Se rechaza la configuración que aporta ambos. |
| D-13 | Dos modos transaccionales: transacción propia (por defecto) y transacción provista por el host (el motor participa sin confirmar ni revertir unilateralmente la transacción completa). Compartir engine no implica compartir transacción. |
| D-14 | Sin transacciones abiertas durante inferencia, llamadas remotas o transformaciones documentales largas. Sin sesiones globales; una sesión no se comparte entre tareas concurrentes. |
| D-15 | PostgreSQL: schema fijo `cawbase_content_engine`. SQLite: prefijo fijo `cawbase_content_engine_` para tablas e historial de migraciones; archivo exclusivo o compartido. Tablas e historial pertenecen al motor; no se modifican ni eliminan tablas ajenas. |
| D-16 | Migraciones: el servicio HTTP independiente migra automáticamente al arrancar, antes de quedar listo para tráfico de negocio. Importar/inicializar la librería no migra. Montar el adaptador en otro host no migra. Existe operación/comando explícito para despliegues. Lambda mantiene migraciones fuera de cold start y requests. Coordinación de arranques concurrentes y múltiples workers. Fallo, incompatibilidad o espera vencida ⇒ no listo. Se distinguen permisos DDL de los de runtime. Mecanismos concretos por backend. Sin promesa de rollback universal. |
| D-17 | Aislamiento implementado y probado en A01: alcances opacos (`scope_id`); el motor no conoce organizaciones, proyectos ni membresías de CAWbase; en Python el host autentica y autoriza, construye el contexto autorizado y el motor valida pertenencia de recursos; nunca se resuelve autorización desde un identificador libremente enviado por un frontend; operaciones administrativas globales separadas de las de un alcance; el contexto Python no es una credencial infalsificable y el límite frente a código arbitrario del host se documenta. |
| D-18 | Adaptador HTTP en A01 con API keys por cliente, asociación explícita con alcances y permisos, almacenamiento por hash, revocación, autenticación y autorización antes de acceder a recursos. Las llamadas internas Python no requieren API key. Bootstrap administrativo seguro, sin clave universal por defecto ni endpoint de bootstrap abierto. Las API keys del servicio HTTP no son credenciales del futuro servidor de inferencia. |
| D-19 | A01 incluye un recurso mínimo de alcance y su administración, justificado por la necesidad de demostrar aislamiento real. No incluye CRUD de perfiles, categorías ni tipos sensibles (A02), ni un sistema general de usuarios o tenants. |
| D-20 | Las API keys de A01 no autorizan desarrollar OIDC, usuarios, contraseñas ni autenticación de CAWbase. |
| D-21 | Los controles necesarios para aceptar A01 (aislamiento, seguridad, migraciones, concurrencia) se prueban en A01; no se difieren a A10. El mock de A03 no es necesario. |
| D-22 | El modelo de inferencia será un servidor intercambiable posterior; A01 no introduce pesos ni dependencias pesadas. |
| D-23 | **(resuelve P-05)** `HostTransaction` envuelve una `AsyncConnection` con transacción activa del **mismo engine inyectado**; cada operación usa un savepoint; el motor no confirma ni revierte la transacción exterior. La garantía de que la transacción del host sigue utilizable aplica **solo** a errores recuperables contenidos por el savepoint; conexión perdida, transacción invalidada o fallo al revertir/liberar el savepoint ⇒ error explícito (`transaction_broken`). La guarda de concurrencia es por conexión subyacente (dos envoltorios no permiten operaciones simultáneas) y el host no usa la conexión concurrentemente por fuera. SQLite exige un `BEGIN` real del driver anterior al savepoint, comprobado en el driver. Precondiciones del engine inyectado documentadas, rechazo explícito y sin alterar el engine ajeno. Contrato: [API pública §6](../public-api.md#6-transacciones). |
| D-24 | **(resuelve P-06)** Una ejecución de actualización usa **una transacción y un bloqueo para toda la secuencia pendiente**: PostgreSQL `pg_try_advisory_xact_lock` en esa misma conexión y transacción con espera limitada; SQLite `BEGIN IMMEDIATE` con espera limitada; relectura del esquema y cálculo del plan **tras** el bloqueo; sin commits intermedios, autocommit ni conexiones independientes; migraciones de A01 compatibles con la transacción exterior (operaciones no transaccionales exigen ampliar el contrato antes); la exclusión rige entre migradores cooperantes. Contrato: [persistencia §4](../persistence-and-migrations.md#4-migraciones). |
| D-25 | **(resuelve P-07)** Clave `cce1.<key_id>.<secret>`; secreto de 32 bytes aleatorios criptográficamente seguros, codificación canónica base64url sin relleno (43 caracteres) y SHA-256 sobre los **32 bytes crudos**; comparación con función de tiempo constante; sin pepper ni KMS (**Q-03 cerrada**); presentación única; sin secreto ni hash en listados, logs, errores o eventos; sin caché de autenticación en A01. No se promete duración constante de la petición ni ausencia de canales temporales; las respuestas a credenciales inválidas son genéricas e idénticas. Una revocación confirmada afecta a autenticaciones posteriores, no a solicitudes ya autorizadas. Contrato: [seguridad §4.1](../security-and-isolation.md#41-formato-codificación-canónica-y-almacenamiento-decisión-d-25-antes-p-07). |
| D-26 | **(resuelve P-08)** Dos operaciones locales de operador sin HTTP: **bootstrap inicial** (solo si `admin_initialized_at` es NULL, marca persistente, transacción serializada; `bootstrap-admin` / `create_initial_admin_key`) y **recuperación administrativa** (`recover-admin --confirm` / `recover_admin_access`; siempre crea un cliente nuevo `recovery-…`, mismo bloqueo, evento de auditoría, no revoca nada). Entrega única por terminal o archivo exclusivo `0600`; pérdida tras el commit ⇒ el secreto no se recupera y se recupera acceso, se identifica y se revoca la clave huérfana. Contrato: [seguridad §4.2](../security-and-isolation.md#bootstrap-administrativo). |
| D-27 | **(resuelve P-09)** Se mantienen los permisos y se agrega `admin:grants:write` (capacidad privilegiada: administrar globalmente la asignación de permisos sobre alcances). Emitir claves requiere `admin:clients:write`; sin `admin:grants:write` los grants de alcance deben ser subconjunto de los del emisor; con ella puede conceder permisos sobre alcances existentes sin acceso directo; los permisos administrativos (incluida la delegación de `admin:grants:write`) siempre por subconjunto; sin comodines; grants inmutables; bootstrap y recuperación reciben el catálogo administrativo completo (6). Contrato: [seguridad §3](../security-and-isolation.md#matriz-de-permisos). |
| D-28 | **(resuelve P-13)** Recurso `Scope` mínimo: `scope_id` opaco aportado por el host u operador, único e **inmutable**, creado por operación administrativa autorizada, con validación estricta de formato y tamaño; conocerlo o elegirlo no concede acceso; el contexto autorizado determina el alcance operativo; sin organizaciones, proyectos ni usuarios de CAWbase; sin perfiles ni categorías de A02. Modelo: [persistencia §2.2](../persistence-and-migrations.md#22-scopes--recurso-de-alcance-p-13). |
| D-29 | **(resuelve P-14)** Arranque del servicio independiente: (1) validar configuración, (2) migrar automáticamente salvo desactivación explícita, (3) comprobar compatibilidad y acceso con las credenciales de runtime, (4) iniciar el servicio HTTP. Durante 1–3 el servicio no escucha (sin `/livez`/`/readyz`). Fallo, incompatibilidad o timeout ⇒ sin tráfico HTTP, error saneado y salida ≠ 0; sin opción de permanecer escuchando. Se conservan migración automática por defecto, `migrate_on_start=false`, `migration_database_url` y la ausencia de migración en importación, librería embebida, router montado y Lambda. Un lanzador prepara una vez y luego inicia workers que solo verifican; otras instancias coordinan por el bloqueo de D-24. Cerrar la conexión DDL no elimina credenciales de memoria/entorno: para separación estricta, migración externa. Contrato: [operación §4](../installation-and-operations.md#estados-de-arranque-y-readiness). |
| D-30 | **(resuelve P-20, decisión del usuario)** Control optimista estricto: `expected_revision`/`If-Match` debe coincidir siempre con la revisión actual; si no coincide ⇒ 412 aunque los valores ya estén aplicados; si coincide y no hay cambios efectivos ⇒ respuesta sin escritura ni incremento; tras perder la respuesta el consumidor relee y reconcilia (no reintenta a ciegas); la comprobación y la escritura son atómicas (un único `UPDATE … WHERE revision = ? AND status = 'active'`). Implementación: `operations/scopes.py::update_scope`. |
| D-31 | **(decisión del usuario)** GitHub Actions como CI de aceptación automatizada en Linux, con PostgreSQL real y SQLite. El workflow está escrito (`.github/workflows/ci.yml`); **no se hizo push y no se ejecutó en GitHub**. Cierra Q-02. |

## 3. Cambios respecto de la revisión 6

Registro local. No reemplaza al playbook ni lo modifica.

| # | Revisión 6, sección | Texto/estado en rev. 6 | Decisión nueva | Ref. |
|---|---|---|---|---|
| 1 | §6 A01 «nombre provisional»; §12 «Nombre y ubicación del repo» | Nombre provisional y por decidir | Identidad definida: distribución `cawbase-content-engine`, import `cawbase_content_engine`; Python 3.13; macOS/Linux | D-01–D-03 |
| 2 | §2 «Base de datos» («misma base PostgreSQL de CAWbase») y §2 «Distribución» | Se menciona solo PostgreSQL | PostgreSQL **y SQLite** soportados, con namespaces por backend y sin tratar SQLite como emulación | D-04, D-15 |
| 3 | §2 «Transacciones» («definir si cada operación administra su transacción o participa en una provista») | Pendiente | Ambos modos aprobados; compartir engine ≠ compartir transacción; contrato en [API pública](../public-api.md#6-transacciones) | D-13, D-14 |
| 4 | §6 A01, §8 y §12 «Aislamiento… A01–A02»; §6 A01 «Definir quién autoriza cada llamada» | A01 define quién autoriza; la implementación del aislamiento se reparte entre A01 y A02 | El aislamiento se **implementa y prueba en A01** con alcances opacos y un recurso mínimo de alcance; A02 lo reutiliza para perfiles | D-17, D-19, D-21 |
| 5 | §2 «HTTP opcional»; §6 A01 «Diseñar la API HTTP»; §6 A10 «El adaptador HTTP opcional, si se entrega» | A01 solo **diseña** el adaptador; su entrega es condicional (A10) | El adaptador HTTP se **implementa en A01** (sigue siendo opcional de instalar/usar). A10 pasa a ser release/imágenes/Compose del adaptador, no su creación | D-09, D-10 |
| 6 | §2 «Motor independiente» («El modo HTTP opcional sí debe autenticar y autorizar»); §6 A02 «credenciales por recurso solo para modo HTTP» | Mecanismo no definido; credenciales por recurso en A02 | **API keys por cliente** con grants explícitos de alcance y permiso, hash, revocación y bootstrap, definidos en A01. A02 podrá ampliar el catálogo de permisos | D-18, D-20 |
| 7 | §2 «Migraciones» («se ejecutan explícitamente durante despliegue, no durante pip install, importación, cold start de Lambda o requests») | Siempre explícitas | Explícitas **salvo** el servicio HTTP independiente, que migra al arrancar antes de quedar listo. Librería, host embebido y Lambda siguen sin migrar implícitamente | D-16 |
| 8 | §3 «Contexto entre repositorios» | Copia del playbook «cuando corresponda» | Existe una copia local de la revisión 6 en `docs/STAGE_3_PLAYBOOK.md`, ignorada por Git desde el commit `d091b49` (no versionada; ningún artefacto ni test depende de ella). No se modifica; la fuente canónica sigue siendo la del proyecto Backend | — |
| 9 | §6 A02 «credenciales por recurso solo para modo HTTP» y §2 «Motor independiente» | Sin catálogo de permisos | Catálogo de 8 permisos y reglas de delegación (`admin:grants:write`) definidos en A01 | D-27 |
| 10 | §2 «Migraciones» (ejecución explícita) | Sin contrato de bloqueo | Una transacción y un bloqueo por secuencia de migración; arranque del servicio independiente: migrar → verificar → escuchar; sin HTTP durante la preparación | D-24, D-29 |
| 11 | §2 «Transacciones» | «Evitar commits ocultos» | Contrato de `HostTransaction`, errores recuperables vs. `transaction_broken`, precondiciones de SQLite e inyección | D-23 |
| 12 | §6 A01 «Excluir: lógica de CAWbase…»; §1 | Sin bootstrap | Bootstrap inicial y recuperación administrativa como operaciones locales de operador | D-26 |
| 13 | §6 A02 y §10 «checks automáticos»; §4 paso 5 | CI sin proveedor ni control optimista definido | GitHub Actions (D-31) y control optimista estricto de `update_scope` (D-30) | D-30, D-31 |
| 14 | §2 «Transacciones»; §6 A01 | «Evitar commits ocultos» | Un engine SQLite inyectado debe emitir un `BEGIN` real; helper `install_transaction_control` invocado por el host; el motor no reconfigura engines ajenos | I-01 |

## 4. Propuestas técnicas

Cada propuesta indica si **bloquea la implementación** (debe confirmarse antes de redactar el prompt de implementación) o es **revisable durante** ella. Las propuestas pendientes no son decisiones aceptadas.

### 4.1 Propuestas resueltas en la revisión 2

Se conserva el texto original (histórico, **superado** en lo que la decisión indica) para trazabilidad. La versión vigente es la decisión enlazada, nunca la propuesta original.

| ID | Propuesta | Recomendación y motivo | Alternativas | Bloquea |
|---|---|---|---|---|
| P-05 | `HostTransaction`: envoltorio de una `AsyncConnection` con transacción activa; el motor ejecuta cada operación dentro de un `SAVEPOINT`; exige que la conexión proceda del **mismo `AsyncEngine` inyectado**; guarda contra uso concurrente. | Aísla errores del motor sin abortar la transacción del host (en PostgreSQL un error abortaría toda la transacción sin savepoint) y no depende de `AsyncSession`. | Aceptar `AsyncSession`; aceptar conexiones de cualquier engine de la misma base. | **Resuelta ⇒ D-23** |
| P-06 | Bloqueo de migraciones: PostgreSQL `pg_try_advisory_xact_lock` con sondeo y plazo (compatible con pools en modo transacción); SQLite `BEGIN IMMEDIATE` con `busy_timeout`; ambos con **recomprobación del nivel de esquema tras obtener el bloqueo**. | El bloqueo de transacción se libera solo ante fallo; no se asume equivalencia entre backends. | Advisory lock de sesión; tabla de lease en ambos. | **Resuelta ⇒ D-24** |
| P-07 | Formato `cce1.<key_id>.<secret>`; `secret` = 32 bytes aleatorios (`secrets`) en base64url; se almacena SHA-256 del secreto con comparación en tiempo constante; sin *pepper* en A01. | Secreto de alta entropía: un hash rápido es suficiente y evita gestionar otra clave; el prefijo versionado permite detección de fugas. | HMAC con pepper; Argon2. | **Resuelta ⇒ D-25** |
| P-08 | Bootstrap: operación de operador (comando CLI o función Python, con acceso a la base) que crea el primer cliente y una clave administrativa; solo si **no existe clave administrativa activa**; serializada por una fila centinela; el secreto se entrega una sola vez (stdout o archivo `0600`). Sin endpoint HTTP de bootstrap. | Sin credencial universal ni endpoint abierto; la recuperación tras pérdida de todas las claves queda dentro del límite de confianza «tiene acceso a la base». | Variable de entorno con clave inicial (rechazada: secreto estático). | **Resuelta ⇒ D-26** |
| P-09 | Catálogo de permisos: alcance `scope:read`, `scope:write`; administración `admin:scopes:read|write`, `admin:clients:read|write`, `admin:diagnostics:read`. Regla anti-escalada: una clave solo emite grants administrativos que ella misma posee. Grants inmutables por clave (cambiar ⇒ emitir nueva y revocar). | Mínimo para A01; la inmutabilidad simplifica auditoría e idempotencia. | Grants editables; roles. | **Resuelta ⇒ D-27** |
| P-13 | Recurso mínimo: **Scope** con `scope_id` aportado por el llamador (opaco, único), nombre, descripción, metadatos acotados, estado y `revision`. | Es la menor entidad que ancla grants, aislamiento y pruebas sin adelantar A02. | `scope_id` generado por el motor (obliga a mantener mapeo en el host). | **Resuelta ⇒ D-28** |
| P-14 | Servicio HTTP: la migración corre en una tarea de fondo del *lifespan* para que `/livez` responda mientras tanto; `/readyz` es 503 hasta `ready`; ante fallo se termina con código ≠ 0. Opciones `migrate_on_start=false` y `migration_database_url` separada para no dar DDL al runtime. | Cumple D-16 y permite separar permisos DDL. | Migrar antes de escuchar (sin liveness durante la migración). | **Resuelta ⇒ D-29** |

### 4.2 Propuestas pendientes de revisión

| ID | Propuesta | Recomendación y motivo | Alternativas | Bloquea |
|---|---|---|---|---|
| P-01 | SQLAlchemy 2.x **Core** asíncrono (sin ORM), `asyncpg` para PostgreSQL, `aiosqlite` para SQLite, Alembic para migraciones. Rangos de versión mínimos verificados en CI al implementar. | Core evita estado de sesión implícito, encaja con D-14 y con unidad de trabajo explícita; un único conjunto de tablas sirve a ambos backends. | ORM con `AsyncSession`; `psycopg` 3 async. | No |
| P-02 | FastAPI + Pydantic v2 (+ `uvicorn`) para el adaptador HTTP, en el extra `http`. | Es el framework que el playbook ya menciona como caso del host y facilita montar routers (D-10). El núcleo usa Pydantic o dataclasses sin importar FastAPI. | Starlette puro; Litestar. | No |
| P-03 | Extras: `postgres` (asyncpg), `sqlite` (aiosqlite), `migrations` (alembic), `http` (fastapi, uvicorn y `migrations`). Base: SQLAlchemy y Pydantic. Los extras `inference` y `documents` quedan **reservados** y no se crean en A01. | Permite una Lambda con `[postgres]` sin Alembic ni FastAPI; el servicio independiente usa `[http,postgres]` o `[http,sqlite]`. | Alembic en la base; extra único `service`. | No |
| P-04 | PostgreSQL 15 (mínima) a 18 (más reciente) soportadas; SQLite ≥ 3.38 (JSON integrado, `RETURNING`). Verificar fechas de fin de soporte al implementar. | Cubre versiones upstream vigentes sin atarse a una sola; 3.38 evita extensiones opcionales. | Solo últimas dos mayores de PostgreSQL. | No |
| P-10 | Versionado: SemVer `0.x` para el paquete (rupturas documentadas en cambios menores hasta `1.0`); HTTP bajo `/v1`; esquema con `schema_level` y `min_runtime_level`; migraciones solo hacia adelante, estilo expandir/contraer. | Permite arranques mixtos durante despliegues sin prometer rollback. | CalVer; sin niveles propios (solo revisión Alembic). | No |
| P-11 | Límites iniciales y criterios de importación liviana (ver [instalación y operación](../installation-and-operations.md#limites-operativos)). Valores a **calibrar con la primera medición**. | Evita valores arbitrarios: se fijan con datos antes de aceptar. | — | No |
| P-12 | Tabla `admin_events` append-only (sin contenido ni secretos) para altas, revocaciones y cambios de estado. | Trazabilidad mínima de seguridad dentro de A01. | Solo logs. | No |
| P-15 | Envoltorio de error `{"error": {"code", "message", "request_id"}}`; regla 404/403 para evitar filtración de existencia entre alcances. | Ver [API pública](../public-api.md#8-errores). | — | No |
| P-16 | CLI `cawbase-content-engine` con subcomandos `version`, `check`, `migrate`, `bootstrap-admin`, `serve`. La URL de base solo por variable de entorno o `*_FILE`, nunca por argumento. | Evita secretos en la lista de procesos. | Solo API Python. | No |
| P-17 | ~~Sin caché de autenticación~~ (aprobada en D-25); **sigue como propuesta:** sin `last_used_at` y limitación de tasa delegada al proxy inverso en A01. | Evita estado y escrituras por solicitud; SQLite tiene un único escritor. | Caché con TTL; límite en proceso. | No |
| P-18 | Herramientas de desarrollo: `uv`, `ruff`, verificador de tipos y `pytest` (ver [desarrollo](../development.md)). | Convención corriente; se confirma al implementar. | — | No |
| P-19 | CI en Linux con PostgreSQL como servicio y SQLite local; proveedor de CI sin aprobar; **se recomienda GitHub Actions si el repositorio se aloja en GitHub** (no se crean workflows ni se afirma aprobación). | Necesario para D-03; el proveedor no está decidido. | — | No |
| P-20 | **`update_scope` estricta:** `expected_revision`/`If-Match` se compara siempre; si no coincide con la revisión actual ⇒ 412, **aunque los valores solicitados ya estén aplicados**. Con revisión coincidente y valores iguales ⇒ éxito sin escritura ni incremento. Reemplaza el texto previo de la revisión 1 que permitía éxito convergente con revisión antigua. | Regla única y predecible: sin ambigüedad entre idempotencia y control optimista; el reintento tras respuesta perdida da 412 y el llamador relee. | Éxito convergente cuando los valores coinciden (descartado por esconder ediciones concurrentes y por requerir dos reglas). | **Adoptada ⇒ D-30** |

### 4.3 Elecciones técnicas adoptadas para esta implementación

Adoptadas por el encargo de implementación de A01. **No son decisiones históricas del usuario sobre el producto**: se registran aquí para distinguirlas y pueden revisarse. Versiones verificadas con PyPI (consulta del 11-nov-2026) y con la suite.

| Propuesta | Elección implementada | Verificación |
|---|---|---|
| P-01 | SQLAlchemy 2.x Core asíncrono (sin ORM), `asyncpg`, `aiosqlite`, Alembic | Suite completa con SQLAlchemy 2.0.36 (cota inferior) y 2.1.8 (última); asyncpg 0.30.0/0.32.0; aiosqlite 0.20.0/0.22.1; alembic 1.14.0/1.20.1 |
| P-02 | FastAPI + Pydantic v2 + Uvicorn (extra `http`) | FastAPI 0.115.0/0.162.4; Pydantic 2.9.0/2.14.3; Uvicorn 0.32.0/0.54.0 |
| P-03 | Extras `postgres`, `sqlite`, `migrations`, `http` (`http` incluye alembic y uvicorn). `inference` y `documents` siguen reservados | `tests/test_distribution.py` instala cada combinación en entornos limpios |
| P-04 | PostgreSQL ≥ 15 (probado 15.19, 17.11, 18.6); SQLite ≥ 3.38 (comprobado en ejecución; **probado solo con la versión enlazada por la plataforma de pruebas**, 3.54.0) | PostgreSQL 14 llega a EOL el 12-nov-2026 (fuente: postgresql.org/support/versioning); 15 hasta 11-nov-2027. **No se probó el piso de SQLite** |
| P-10 | SemVer `0.x`; HTTP `/v1`; `schema_level` y `min_runtime_level`; migraciones solo hacia adelante | `tests/test_config_and_lifecycle.py` (casos de compatibilidad) |
| P-11 | Límites iniciales de [operación](../installation-and-operations.md); criterios de importación medidos, no relajados | [Informe](../a01-implementation-report.md#mediciones) |
| P-12 | Tabla `admin_events` append-only sin valores ni secretos | `tests/test_scopes_and_isolation.py::TestAdminEvents` |
| P-15 | Envoltorio de error `{"error":{"code","message","request_id"}}` y regla 404/403 | `tests/test_http.py` |
| P-16 | CLI `cawbase-content-engine` (`version`, `check`, `migrate`, `bootstrap-admin`, `recover-admin`, `serve`) | `tests/test_processes.py` |
| P-17 | Sin caché de autenticación ni `last_used_at`; limitación de tasa delegada al despliegue (**límite documentado**: el motor no limita intentos) | Revisión de código; sin test de tasa (no existe la función) |
| P-18 | `uv`, `ruff`, `mypy` (único verificador), `pytest` | [Desarrollo](../development.md) |
| P-19 | GitHub Actions (D-31) | Workflow escrito; no ejecutado |

### 4.4 Decisiones tomadas durante la implementación

| ID | Decisión | Motivo |
|---|---|---|
| I-01 | `install_transaction_control(async_engine)` es un helper público **invocado por el host**; el motor lo aplica solo a engines que crea. Un engine SQLite inyectado debe emitir `BEGIN` real; `start()` lo comprueba sobre el driver y rechaza (`configuration_invalid`) sin modificar el engine ajeno | Reproducido (`tests/test_spike_sqlite.py`): en modo legado de `sqlite3` un `SAVEPOINT` previo al primer DML abre su propia transacción y su `RELEASE` confirma, de modo que un rollback exterior no revierte nada |
| I-02 | `PRAGMA journal_mode=WAL` es best-effort y no se ejecuta en el engine de migración | Cambiar a WAL exige un bloqueo exclusivo y fallaba de inmediato mientras otro migrador retenía el bloqueo de escritura (hallado por `test_nothing_listens_until_preparation_completes`) |
| I-03 | Sin éxito convergente en `update_scope` (D-30) | Ver D-30 |
| I-04 | `LimitExceededError` es 422 para límites de campo y `PayloadTooLargeError` (mismo código `limit_exceeded`) es 413 para el cuerpo | La tabla de errores admitía ambos; así se distinguen |
| I-05 | Fallar al abrir un savepoint en una transacción activa ⇒ `transaction_broken` | Cubre conexión perdida y PostgreSQL "aborted" sin ambigüedad; verificado en ambos backends |
| I-06 | `SecretDeliveryError` se lanza fuera del `except` | Evita que `__context__` arrastre un mensaje de error de E/S con el secreto (hallado por `test_delivery_failure_after_commit_never_leaks_the_secret`) |
| I-07 | Cursor de paginación decodificado de forma estricta | `b64decode` indulgente aceptaba `@@@` |
| I-08 | La relectura de esquema tras el bloqueo exige `READ COMMITTED` en la conexión de migración (PostgreSQL) | Con un nivel superior la instantánea sería anterior al bloqueo |
| I-09 | Códigos de salida 2/3/4/1 en CLI y servicio | Distinguir configuración, migración y compatibilidad |
| I-10 | La cota declarada de Uvicorn pasa de `>=0.32` a `>=0.50` | Release notes de Uvicorn 0.50.0: hasta entonces el supervisor reinicia indefinidamente los workers que fallan al iniciar; con ≥ 0.50 se detiene. Cambia un rango declarado por evidencia; no es una decisión de producto |
| I-11 | El lanzador devuelve el código del fallo de inicialización de un worker (p. ej. 4) mediante un marcador privado, y nunca 0 | Defecto encontrado: con varios workers `serve()` devolvía 0 tras detenerse el supervisor por un fallo de arranque, y con un worker Uvicorn salía con 3 (indistinguible de «migración») |
| I-12 | Los registros de Uvicorn se enrutan por el manejador JSON del servicio (sin `exc_info`) | Antes los mensajes del supervisor ni se veían (`log_config=None`) y un fallo de arranque volcaba una traza |
| I-13 | La respuesta 503 de la compuerta de readiness lleva `X-Request-ID` y `request_id` | Defecto: iba vacío |
| I-14 | AC-26 usa una base SQLite realmente de solo lectura (`mode=ro`) y exige exactamente `MigrationPermissionError` | El test anterior (chmod sobre un archivo vacío, aserción permisiva) **fallaba como root en Linux** y aceptaba cualquier error |
| I-15 | Auditoría de artefactos (`scripts/audit_artifacts.py`) y `scripts` + `uv.lock` incluidos en el sdist | Reproducibilidad del sdist y control de archivos accidentales |

## 5. Preguntas abiertas

| ID | Pregunta | Cuándo |
|---|---|---|
| Q-01 | Licencia definitiva y publicación (D-05). | A10 |
| Q-02 | ~~Proveedor de CI~~ **Cerrada por D-31 (GitHub Actions).** (P-19). Recomendación: GitHub Actions si el repositorio se aloja en GitHub; **no aprobado**, no se crean workflows. | Antes del prompt de implementación |
| Q-03 | ~~¿Pepper o KMS para el hash de API keys?~~ **Cerrada por D-25: sin pepper ni KMS en A01.** | Cerrada |
| Q-04 | ¿Política de retención/purga de `admin_events`? En A01 se conserva indefinidamente sin purga. | A05 (retención) |
| Q-05 | ¿Soporte de otros drivers (`psycopg` 3)? Fuera de A01. | Posterior |
| Q-06 | **Política ante el fallo de un worker posterior a la preparación.** Comportamiento observado con Uvicorn ≥ 0.50 (ver [operación §4.2](../installation-and-operations.md#42-varios-workers-e-instancias)): fallo de inicialización ⇒ el servicio se detiene con código ≠ 0; muerte tras servir ⇒ reinicio sin límite ni *backoff*. D-29 no define la segunda situación. **Aclaración mínima propuesta (pendiente, no una política nueva):** declarar en el contrato exactamente lo observado y dejar un límite/*backoff* de reinicios fuera de A01. | Revisión del usuario |
| Q-07 | **Qué importación debe cumplir la referencia provisional de 300 ms.** Medido en Linux/aarch64: en frío (sin bytecode) ≈ 875 ms, en caliente ≈ 198 ms (máx. 229 ms); en macOS: frío ≈ 939 ms (máx. 3263 ms), caliente ≈ 231 ms (máx. 324 ms). La referencia no distinguía frío de caliente y **no se modificó**. | Revisión del usuario |

## 6. Conflictos o tensiones detectadas

1. **Migraciones en arranque vs. rev. 6 §2.** Resuelto por precedencia (D-16); la rev. 6 debe reflejar la excepción del servicio independiente.
2. **A02 «credenciales por recurso solo para modo HTTP»** queda parcialmente adelantada: A01 define grants por alcance y permiso. A02 debe partir del catálogo de [seguridad y aislamiento](../security-and-isolation.md#matriz-de-permisos) y ampliarlo, no redefinirlo.
3. **A10 «adaptador HTTP, si se entrega»**: ya no es condicional; debe actualizarse para no duplicar su construcción.

No hay contradicción que impida un contrato coherente.

## 7. Qué debe incorporar la próxima revisión canónica del playbook

Los puntos 1–14 de la tabla de §3, más la actualización de §6 A01 (incluir HTTP, SQLite, alcances mínimos y API keys), §6 A02 (partir de los alcances y grants de A01), §6 A10 (HTTP ya implementado) y §12 (filas de «Nombre y ubicación» y «Aislamiento» resueltas). La incorporación la hace quien mantiene la versión canónica; este repositorio no la declara.
