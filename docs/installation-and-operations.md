# Instalación y operación

> **Estado: implementado en la versión 0.1.0 (rama `feature/a01-product-foundation`, sin commitear) y probado localmente en macOS; no probado en Linux/GitHub Actions todavía y no aceptado por el usuario.** Evidencia y alcance real de las pruebas: [informe de implementación](a01-implementation-report.md). Los comandos y variables de este documento existen y se ejercitan en `tests/test_processes.py`, `tests/test_distribution.py` y `examples/`. **El paquete no está publicado** (D-05): se instala desde wheel/sdist locales.

Relacionado: [API pública](public-api.md) · [persistencia y migraciones](persistence-and-migrations.md) · [seguridad y aislamiento](security-and-isolation.md) · [desarrollo](development.md).

## 1. Instalación y extras (D-08; nombres de extras adoptados)

| Extra | Aporta | Uso típico |
|---|---|---|
| *(base)* | contratos, configuración, persistencia (SQLAlchemy Core asíncrono, Pydantic) | API de CAWbase en Lambda (con un driver) |
| `postgres` | `asyncpg` | PostgreSQL |
| `sqlite` | `aiosqlite` | SQLite |
| `migrations` | `alembic` | quien ejecute migraciones |
| `http` | `fastapi`, `uvicorn`, `migrations` | adaptador HTTP y servicio independiente |
| `inference`, `documents` | **reservados**, no se crean en A01 | A03+ |

Instalación desde artefactos locales (no hay publicación; `uv build` produce `dist/*.whl` y `dist/*.tar.gz`):

```bash
uv build                                                   # wheel + sdist en ./dist
uv pip install "cawbase-content-engine[postgres] @ ./dist/cawbase_content_engine-0.1.0-py3-none-any.whl"            # host embebido (sin HTTP ni Alembic)
uv pip install "cawbase-content-engine[postgres,migrations] @ ./dist/cawbase_content_engine-0.1.0-py3-none-any.whl"  # trabajo de despliegue que migra
uv pip install "cawbase-content-engine[http,postgres] @ ./dist/cawbase_content_engine-0.1.0-py3-none-any.whl"        # servicio independiente
```

Reglas: la base no depende de FastAPI, Alembic, drivers, inferencia ni librerías de documentos; ningún extra pesado se vuelve dependencia transitiva de la base; ningún `__init__` compartido importa código opcional. Python ≥ 3.13.

<a id="configuracion"></a>
## 2. Configuración explícita

### 2.1 Reglas

- La configuración se construye de forma **explícita**; importar el paquete no lee variables de entorno ni archivos (D-11).
- Precedencia (propuesta): **argumentos explícitos > variables de entorno (solo si el código llama a `EngineConfig.from_env()` o lo hace el CLI) > valores por defecto**. No hay archivo de configuración en A01. Para secretos, la variante `*_FILE` (ruta a un archivo) tiene el mismo rango que la variable y es mutuamente excluyente con ella.
- La validación ocurre al construir `EngineConfig`/`ContentEngine` y falla con `configuration_invalid` sin tocar la red.

### 2.2 Campos

| Campo | Variable (CLI/`from_env`) | Por defecto | Notas |
|---|---|---|---|
| `database_url` | `CAWBASE_CONTENT_ENGINE_DATABASE_URL` / `_FILE` | — | `SecretStr`. Esquemas aceptados: `postgresql+asyncpg://`, `sqlite+aiosqlite://`. Drivers síncronos u otros dialectos ⇒ error |
| `async_engine` | (solo Python) | — | `AsyncEngine` del host. **Exclusivo** con `database_url` |
| `pool_size`, `max_overflow`, `pool_timeout_s` | `…_POOL_SIZE`, `…_MAX_OVERFLOW`, `…_POOL_TIMEOUT_S` | los de SQLAlchemy (5 / 10 / 30) | Solo modo URL; con engine inyectado ⇒ error (no son aplicables) |
| `connect_timeout_s` | `…_CONNECT_TIMEOUT_S` | 10 | Modo URL |
| `statement_timeout_ms` | `…_STATEMENT_TIMEOUT_MS` | 5000 | PostgreSQL: del lado servidor al abrir conexión (modo URL). SQLite: `busy_timeout` |
| `lock_timeout_s` | `…_LOCK_TIMEOUT_S` | 60 | Espera máxima de bloqueo de migración |
| `limits` | `…_LIMIT_*` | ver §7 | |
| *(servicio)* `migrate_on_start` | `…_MIGRATE_ON_START` | `true` | Solo servicio independiente |
| *(servicio)* `migration_database_url` | `…_MIGRATION_DATABASE_URL` / `_FILE` | = `database_url` | Credencial con DDL, usada solo durante la preparación previa a escuchar. Cerrarla no borra las credenciales de la memoria ni del entorno del proceso: para separación estricta, usar migración externa ([persistencia §4.4](persistence-and-migrations.md#44-permisos-ddl-y-consecuencias-del-arranque-automático)) |
| *(servicio)* `host`, `port` | `…_HOST`, `…_PORT` | `127.0.0.1`, `8080` | Escucha local por defecto |
| *(servicio)* `enable_openapi` | `…_ENABLE_OPENAPI` | `false` | |
| *(servicio)* `log_level`, `log_format` | `…_LOG_LEVEL`, `…_LOG_FORMAT` | `INFO`, `json` | Solo el servicio configura logging |

### 2.3 Combinaciones rechazadas

| Caso | Resultado |
|---|---|
| `database_url` **y** `async_engine` | `configuration_invalid` (conexión ambigua) |
| Ninguno | `configuration_invalid` |
| `async_engine` sincrónico, dialecto no soportado o driver distinto de los previstos | `configuration_invalid` |
| Opciones de pool junto a `async_engine` | `configuration_invalid` |
| `database_url` y `database_url_file` a la vez | `configuration_invalid` |
| Límites fuera de rango (≤ 0 o por encima del máximo) | `configuration_invalid` |
| Engine inyectado que incumple las [precondiciones del host](public-api.md#6-transacciones) (driver, nivel de aislamiento, control transaccional de SQLite) | `configuration_invalid` / `transaction_incompatible`; el motor **no** modifica el engine ajeno |

### 2.4 Secretos en la configuración

Todo valor secreto es `SecretStr`: no aparece en `repr`, `str`, logs, errores ni diagnóstico. La URL se muestra, si hace falta, solo con host/puerto/base y sin credenciales; los diagnósticos HTTP ni siquiera eso (ver §5). En A01 no hay opción `echo` de SQL (registraría parámetros).

## 3. Modos de uso

| Modo | Pasos | Migra |
|---|---|---|
| **Embebido (Python)**, p. ej. worker | `EngineConfig(...)` → `await engine.start()` → operaciones → `await engine.aclose()` | no |
| **Lambda/API de CAWbase** | instala base + `postgres`; inicializa fuera del cold start pesado (una consulta de verificación); el esquema ya fue migrado por el despliegue | no |
| **Router montado** | `create_router(engine)` en un host FastAPI propio | no |
| **Servicio independiente** | `cawbase-content-engine serve` | **sí, antes de escuchar** (configurable); sin HTTP durante la preparación |
| **Despliegue** | `cawbase-content-engine migrate` (o `migrations.upgrade`) antes de actualizar workers | sí, explícito |

El ejemplo de aceptación Python (sin CAWbase, sin HTTP, sin modelo) está especificado en la [matriz de aceptación](features/A01-base-del-producto-instalacion-independiente.md#17-matriz-de-aceptacion).

### CLI (implementado)

| Subcomando | Función | Extra necesario |
|---|---|---|
| `version` | versión del paquete | base |
| `check` | estado del esquema (`schema_status`), sin migrar; código de salida ≠ 0 si no es compatible | base + driver |
| `migrate` | `upgrade` con bloqueo y plazo | `migrations` + driver |
| `bootstrap-admin [--secret-file RUTA]` | bootstrap inicial ([seguridad](security-and-isolation.md#bootstrap-administrativo)) | base + driver |
| `recover-admin --confirm [--secret-file RUTA]` | recuperación administrativa (misma sección) | base + driver |
| `serve` | servicio HTTP: valida, migra (por defecto), verifica y solo entonces escucha | `http` + driver |

La URL de base solo por entorno (`CAWBASE_CONTENT_ENGINE_DATABASE_URL`) o `…_FILE`; nunca como argumento visible en la lista de procesos. Códigos de salida: `0` éxito; `2` configuración inválida o extra faltante; `3` migración (fallo o bloqueo vencido, en `serve`); `4` esquema ausente/incompatible o base inaccesible; `1` otro error. Uso real (`python -m cawbase_content_engine` equivale a `cawbase-content-engine`):

```bash
export CAWBASE_CONTENT_ENGINE_DATABASE_URL='sqlite+aiosqlite:////ruta/absoluta/engine.db'   # o postgresql+asyncpg://...
cawbase-content-engine migrate                             # explícito; una transacción y un bloqueo
cawbase-content-engine check                               # JSON; salida 0 solo si el esquema es compatible
cawbase-content-engine bootstrap-admin --secret-file ./admin.key
cawbase-content-engine serve --host 127.0.0.1 --port 8080 --workers 1
```

<a id="estados-de-arranque-y-readiness"></a>
## 4. Estados de arranque y readiness

### 4.1 Secuencia de arranque del servicio independiente (decisión D-29, antes P-14)

El lanzador (`cawbase-content-engine serve`) ejecuta, en este orden y **antes de abrir el puerto**:

1. **Validar la configuración** (ambigüedad, límites, secretos). Fallo ⇒ salida ≠ 0.
2. **Ejecutar las migraciones automáticas**, salvo `migrate_on_start=false` (migración externa). Usa `migration_database_url` si está definida, y el bloqueo único de [persistencia §4.3](persistence-and-migrations.md#43-bloqueo-y-coordinación-de-migraciones-decisión-d-24-antes-p-06).
3. **Comprobar compatibilidad y acceso con las credenciales de runtime** (se abre un `ContentEngine` con esas credenciales y `start()` lee el estado del esquema: conectividad y niveles compatibles; los permisos de escritura no se sondean y se verifican por uso).
4. **Iniciar el servicio HTTP** (y, si se pidieron varios workers, lanzarlos).

Mientras ocurren los pasos 1–3 **el servicio no escucha**: no hay `/livez` ni `/readyz`. El despliegue debe conceder tiempo de arranque suficiente (al menos `lock_timeout_s` más la duración esperada de migración) y usar una sonda/periodo de gracia de **arranque**, no una sonda de vitalidad que mate el proceso por no responder.

Ante fallo de migración, esquema incompatible, bloqueo vencido o runtime sin acceso: **no se inicia tráfico HTTP**, se registra un error saneado (código y motivo genérico, sin URL ni credenciales) y el proceso **termina con código distinto de cero**. No existe una opción para quedarse escuchando tras un fallo de arranque. Códigos de salida propuestos: `2` configuración, `3` migración (fallo o bloqueo), `4` incompatibilidad/acceso de runtime, `1` otro.

### 4.2 Varios workers e instancias

- **Un lanzador, N workers:** el lanzador completa 1–3 **una vez** y recién después inicia los workers. Los workers **nunca migran**; cada uno, antes de atender, comprueba compatibilidad con las credenciales de runtime (paso 3 en su proceso) y termina con código ≠ 0 si falla. No se supone ninguna migración de fondo.
- **Varias instancias del servicio** (réplicas, contenedores): cada lanzador ejecuta 1–3; el bloqueo de migración garantiza que solo una aplica la secuencia y las demás esperan hasta `lock_timeout_s`, releen el esquema y continúan sin escribir (o fallan por timeout).
- La fábrica de aplicación para ejecutarse con un servidor externo (p. ej. `uvicorn módulo:app --workers N`) **no migra**: exige migración externa o previa y solo verifica compatibilidad al arrancar cada worker.

### 4.3 Liveness y readiness (con el servicio ya escuchando)

- `GET /livez`: el proceso responde. **No** consulta la base.
- `GET /readyz`: `200 {"status":"ready"}` solo en estado `ready`; `503 {"status":"not_ready","state":"<estado>"}` en los demás.

| Estado (post-arranque) | Significado | `/readyz` | Rutas `/v1/*` |
|---|---|---|---|
| `ready` | esquema compatible verificado y base accesible | 200 | normales |
| `degraded_database` | estaba listo y la base dejó de responder | 503 | 503 `database_unavailable` |
| `schema_incompatible` | la comprobación periódica detecta un esquema fuera del rango compatible (p. ej. otro despliegue migró hacia un nivel que este runtime no soporta) | 503 | 503 `schema_incompatible` |
| `stopping` | cierre ordenado | 503 | 503 `engine_not_ready` |

Al empezar a escuchar, el servicio ya superó los pasos 1–3 y está en `ready`; no existen estados HTTP «starting», «migrating» ni «failed» (esas fases ocurren antes de escuchar o terminan el proceso).

### 4.4 Condiciones de `ready`

Todas: (1) configuración válida; (2) migración completada o innecesaria; (3) compatibilidad de esquema verificada con credenciales de runtime ([reglas](persistence-and-migrations.md#45-compatibilidad-p-10)); (4) comprobación periódica barata de conectividad y nivel de esquema (`SELECT` con tiempo límite ≈ 1 s, cacheada ≈ 2 s) correcta. El estado de infraestructura «encendido» no equivale a readiness.

### 4.5 Modo embebido

No hay `/readyz`. `await engine.start()` falla con tipo de error (`SchemaMissingError`, `SchemaIncompatibleError`, `DatabaseUnavailableError`); `await engine.readiness()` devuelve el mismo resumen grueso para que el host lo exponga. Nada migra.

<a id="diagnostico"></a>
## 5. Diagnóstico seguro

- Públicos (`/livez`, `/readyz`): solo estados gruesos. No incluyen versión, URL, host, ruta de archivo, nombre de base, usuario ni mensajes del driver.
- Autenticado (`GET /v1/admin/diagnostics`, `admin:diagnostics:read`): versión del paquete, backend (`postgresql` | `sqlite`), `schema_level` y `min_runtime_level` actual y esperado, modo de migración (`on_start` | `external`), estado. **Nunca**: URL de conexión, credenciales, rutas internas, nombre de host/base/schema real, claves, hashes ni conteos de recursos por alcance.
- Los motivos detallados de fallo (excepción original, driver) van solo a logs del servidor con el filtrado de §7.

## 6. Observabilidad

<a id="observabilidad"></a>

Principios (aplican a logs, métricas y eventos): sin contenido de usuario, sin secretos, sin prompts, sin URLs ni encabezados `Authorization`, sin valores de `metadata`/`display_name`/`description`.

- Logging estándar (`logging`) con logger raíz `cawbase_content_engine`. La **librería no configura logging global** (ni handlers, ni niveles, ni formato); solo el servicio independiente lo configura.
- Campos permitidos por evento: `event`, `request_id`, `operation`, `outcome`, `error_code`, `duration_ms`, `key_id`, `client_id`, `scope_id`, `schema_level`. `scope_id` es opaco; el host debe elegir identificadores sin información personal.
- Eventos mínimos: arranque/cierre, transición de estado, inicio/fin/fallo de migración (con revisiones, sin SQL), bloqueo adquirido/vencido, autenticación fallida (solo código genérico y `key_id` si es sintácticamente válido; nunca el secreto), emisión y revocación de claves, errores internos.
- **Métricas: no implementadas en A01** (eran una propuesta). Solo existen los logs de arriba; la limitación de tasa se delega al despliegue (proxy inverso/ingreso): el motor no limita intentos de autenticación ni solicitudes por tiempo, y no escribe `last_used_at` ni cachea la autenticación.
- `X-Request-ID` generado por el servidor y presente en logs y en el cuerpo de error.
- Un test debe demostrar la ausencia de secretos/URLs/metadatos en logs, errores y respuestas ([matriz](features/A01-base-del-producto-instalacion-independiente.md#17-matriz-de-aceptacion)).

<a id="limites-operativos"></a>
## 7. Límites operativos y criterios de instalación liviana

### 7.1 Límites (implementados con estos valores iniciales; ajustables con `Limits`)

| Límite | Valor inicial propuesto |
|---|---|
| Cuerpo HTTP | 64 KiB (413 si se excede) |
| `scope_id` | 1–128 caracteres `[A-Za-z0-9._:-]` |
| `display_name` / `description` | 200 / 1000 caracteres |
| `metadata` | objeto JSON ≤ 4 KiB serializado |
| `name` de cliente | 1–64 `[a-z0-9-]` |
| Grants de alcance por clave | ≤ 100 |
| Claves activas por cliente | ≤ 100 |
| Página de listados | por defecto 50, máximo 200 (paginación por cursor) |
| `lock_timeout_s` | 60 |
| Plazo de conexión / sentencia | 10 s / 5 s |
| Pool (modo URL) | valores por defecto de SQLAlchemy salvo que se configuren |

Superar un límite ⇒ `limit_exceeded` o `validation_failed`, sin efecto parcial.

### 7.2 Criterios medibles de importación e instalación liviana (mediciones en el informe)

| Criterio | Verificación | Umbral inicial propuesto |
|---|---|---|
| `import cawbase_content_engine` no importa módulos prohibidos | subproceso que compara `sys.modules`; lista prohibida: `fastapi`, `starlette`, `uvicorn`, `alembic`, `asyncpg`, `aiosqlite`, `torch`, `numpy`, `transformers`, y librerías de PDF/DOCX | cero coincidencias |
| Sin efectos externos al importar | subproceso con sockets/archivos/hilos vigilados | cero conexiones, cero archivos creados, hilos adicionales = 0, loggers sin handlers añadidos |
| Tiempo de importación | `python -X importtime` en CI de referencia | mediana ≤ valor fijado tras la primera medición (referencia provisional: 300 ms) |
| Tamaño del entorno base | tamaño instalado (base sin extras) | fijar tras medir; referencia provisional ≤ 40 MiB |
| Dependencias transitivas de la base | `pip list` en entorno limpio | solo las declaradas en P-01/P-03 |
| Aislamiento de extras | instalar base sola, base+`http`, base+`postgres`, base+`sqlite` en entornos separados | cada combinación importa y ejecuta su camino sin los otros extras |

Los umbrales marcados «referencia provisional» se confirman con datos antes de aceptar A01; no se eligieron para eliminar un pendiente sino como punto de partida a medir.

## 8. Diferencias PostgreSQL / SQLite y recomendaciones operativas

Detalle en [persistencia y migraciones](persistence-and-migrations.md#6-diferencias-reales-entre-postgresql-y-sqlite). Resumen operativo:

- PostgreSQL: producción y base compartida con CAWbase; roles separados de migración y runtime; copias con las herramientas habituales.
- SQLite: un solo escritor; WAL recomendado; un solo nodo; las migraciones bloquean al resto de escritores del archivo; copia del archivo con la base quiescente o con la API de copia en línea; sin PITR.
- En ninguno de los dos se promete rollback de migraciones: restaurar desde copia.

## 9. Integración con workers y Lambda (distribución de dependencias)

- **API/Lambda de CAWbase**: base + `postgres`. Nunca `http`, `migrations`, inferencia ni documentos. El engine puede inyectarse desde el punto de composición del host. Migraciones fuera del cold start.
- **Worker de CAWbase**: la misma versión del paquete; extras adicionales (`inference`, `documents`) cuando existan.
- **Orden de despliegue**: migrar (credencial DDL) → actualizar API/workers. La versión del paquete que corre debe ser compatible con el esquema ([reglas](persistence-and-migrations.md#45-compatibilidad-p-10)).
- El servidor de inferencia no forma parte de A01.


## 10. Mediciones registradas (macOS, no Linux)

Metodología, versiones y plataforma de cada medición están en el [informe de implementación](a01-implementation-report.md#mediciones). Resumen: importación en procesos nuevos (`scripts/measure_import.py`), tamaño de `site-packages` del entorno base instalado desde el wheel, y módulos de terceros cargados. Las "referencias provisionales" (300 ms, 40 MiB) **no se han relajado**; todavía no hay medición en Linux/CI.
