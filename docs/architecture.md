# Arquitectura

> **Estado: implementado en la versión 0.1.0; probado en macOS y en Linux local (Docker, PostgreSQL 15–18 + SQLite); NO ejecutado en GitHub Actions; no aceptado por el usuario.** Evidencia y alcance real de las pruebas: [informe de implementación](a01-implementation-report.md). La estructura de paquetes descrita es la real.

Relacionado: [API pública](public-api.md) · [persistencia y migraciones](persistence-and-migrations.md) · [seguridad y aislamiento](security-and-isolation.md) · [instalación y operación](installation-and-operations.md).

## 1. Principios

1. **Producto independiente** (D-01): el motor no conoce CAWbase, sus organizaciones, proyectos ni permisos. CAWbase es un consumidor más; no se copia lógica de CAWbase ni se mantiene una variante privada del motor.
2. **Un núcleo, dos fachadas** (D-07): Python y HTTP invocan las mismas operaciones; el adaptador HTTP no contiene lógica de negocio propia, solo autenticación, traducción y errores.
3. **Núcleo liviano, extras opcionales** (D-08): lo pesado (HTTP, migraciones, inferencia, documentos) no es dependencia de la base.
4. **Sin efectos al importar** (D-11) y **ciclo de vida explícito**.
5. **Ownership explícito** de engine, conexiones y transacciones (D-12, D-13).
6. **Aislamiento desde la base** (D-17): todo acceso a datos pasa por un contexto autorizado.
7. **Asíncrono en lo que hace E/S** (D-06).

## 2. Componentes y dirección de dependencias

```text
          ┌────────────────────────┐   ┌───────────────┐
 extra    │ http (FastAPI)         │   │ cli           │  extra: según subcomando
 http     │ routers, auth, errores │   │ serve/migrate │
          └──────────┬─────────────┘   └───────┬───────┘
                     │ usa                     │ usa
 ┌───────────────────▼─────────────────────────▼────────────────┐
 │ API pública del núcleo: ContentEngine, contextos, operaciones │   base
 │ (scopes, clients, api_keys, bootstrap, auth, health)          │
 └───────────────┬───────────────────────────────┬──────────────┘
                 │                               │
        ┌────────▼────────┐             ┌────────▼─────────┐
        │ persistence     │◄────────────│ migrations       │  extra: migrations
        │ tablas, repos,  │  metadata   │ (Alembic, locks) │
        │ dialectos       │             └──────────────────┘
        └────────┬────────┘
                 ▼
          SQLAlchemy Core async → asyncpg | aiosqlite   (extras postgres | sqlite)
```

Reglas de dependencia (verificables con una prueba del grafo de imports):

- El núcleo **no importa** `http`, `cli`, `migrations`, FastAPI, Alembic ni drivers (los drivers los carga SQLAlchemy por URL, solo al crear el engine en `start()`).
- `http` y `cli` dependen del núcleo, nunca al revés.
- `migrations` depende de la metadata de `persistence`; `persistence` no depende de `migrations`.
- Los futuros módulos (clasificación, documentos, inferencia) dependerán del núcleo y vivirán en extras; **no** se importan desde `__init__` compartidos.

## 3. Estructura de paquete (implementada)

```text
src/cawbase_content_engine/
  __init__.py            # exporta la API pública; sin efectos
  __main__.py, cli.py    # `python -m cawbase_content_engine` / `cawbase-content-engine` (importaciones perezosas)
  _version.py, py.typed
  constants.py           # namespace fijo, niveles de esquema/runtime, clave del advisory lock
  config.py              # EngineConfig, Limits, from_env (explícito), lectura de secretos (*_FILE)
  errors.py              # jerarquía estable de errores
  permissions.py         # catálogo de 8 permisos
  contexts.py            # ScopeContext, AdminContext, AuthenticatedPrincipal
  validation.py          # validación compartida por Python y HTTP
  auth.py                # formato de claves, codificación canónica, hash, comparación
  models.py              # tipos de resultado (inmutables)
  engine.py              # ContentEngine, ScopedOperations, AdminOperations
  transactions.py        # HostTransaction, transacción propia y savepoint del host
  schema.py              # estado y compatibilidad del esquema, check_schema
  bootstrap.py           # create_initial_admin_key, recover_admin_access, SecretFileSink
  operations/            # scopes, clients, api_keys: lógica de negocio única
  persistence/           # dialect, tables (Core), types, engines, sqlite_support (privado)
  migrations/            # upgrade(), runner (un bloqueo, una transacción), ns, versions/0001_initial.py  [extra migrations]
  http/                  # router (create_router, create_health_router), service (lanzador, fábrica)    [extra http]
tests/  examples/  scripts/  .github/workflows/ci.yml
```

Subpaquetes opcionales (`http`, `migrations`) solo se importan cuando el código los pide explícitamente; una prueba de grafo lo verifica (`tests/test_import_light.py`).

## 4. Responsabilidades y ownership

| Pieza | Responsable | Notas |
|---|---|---|
| Autenticación y autorización de usuarios finales | **Host** | El motor no tiene usuarios (D-20) |
| Construcción de `ScopeContext`/`AdminContext` en Python | **Host** | [Límite](security-and-isolation.md#limite-del-contexto-python) |
| Autenticación de API keys y autorización de rutas HTTP | **Adaptador HTTP** (con lógica de `auth` del núcleo) | |
| Validación de pertenencia de recursos | **Núcleo** | Siempre con `scope_id` del contexto |
| Engine y pool | motor (URL) o host (inyectado) | [Ownership](public-api.md#2-ciclo-de-vida) |
| Transacción de operación | motor (propia) o host (provista) | [Contrato](public-api.md#6-transacciones) |
| Esquema y migraciones | motor | Ejecutadas por operación explícita o por el servicio independiente al arrancar |
| Estado de readiness | núcleo (cálculo) y adaptador (exposición) | |

## 5. Flujos principales

**Python embebido (acceso a un alcance):** host autoriza → `engine.trusted_scope_context(...)` → `engine.scoped(ctx).get_scope(id)` → operación valida pertenencia → consulta con `scope_id` → resultado o error tipado.

**HTTP (acceso a un alcance):** solicitud → límite de cuerpo → autenticar clave → determinar grants sobre `{scope_id}` → (sin grant ⇒ 404; grant insuficiente ⇒ 403) → misma operación del núcleo con un `ScopeContext` derivado de la clave → respuesta.

**Arranque del servicio independiente:** `serve` → validar configuración → migrar (salvo `migrate_on_start=false`, con bloqueo y una transacción) → verificar compatibilidad y acceso con credenciales de runtime → **recién entonces** escuchar y lanzar workers (los workers no migran y verifican compatibilidad). Fallo ⇒ sin tráfico HTTP y salida ≠ 0.

## 6. Qué no es parte de A01

Clasificación, detección, ranking, inferencia y mock (A03); PDF/DOCX; protección, correspondencias y restauración; trabajos y almacenamiento documental; políticas y dominio de CAWbase; elección del modelo, despliegue AWS y encendido/apagado de inferencia; publicación pública. A02 (perfiles, categorías, tipos sensibles) se apoyará en los alcances y el aislamiento de A01.

## 7. Extensión prevista

Cada feature futura añade recursos de alcance siguiendo las mismas reglas: tabla con `scope_id`, repositorio que lo exige, operaciones por contexto, permisos en el catálogo y pruebas de acceso cruzado. Los nuevos extras (`inference`, `documents`) deben pasar las mismas comprobaciones de importación liviana.
