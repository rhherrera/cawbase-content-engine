# A01 — Base del producto e instalación independiente

> **Estado: implementado en 0.1.0 y probado localmente en macOS (PostgreSQL 15/17/18 + SQLite); pendiente de ejecución en Linux/GitHub Actions y de aceptación del usuario. No cerrado.** Este documento define alcance y aceptación; el resultado trazable por criterio está en el [informe de implementación](../a01-implementation-report.md). Los contratos detallados viven en los documentos permanentes enlazados.

- Fuente: `STAGE_3_PLAYBOOK.md` revisión 6 (11 de noviembre de 2026), más las decisiones posteriores del usuario registradas en [A01-decisions](../decisions/A01-decisions.md).
- Repositorio: `cawbase-content-engine` · rama de especificación: `docs/a01-product-foundation`.
- Dependencias: ninguna (primera feature del Bloque A). Habilita A02.

## 1. Objetivo, casos de uso, exclusiones y dependencias

**Objetivo.** Establecer un producto instalable y funcional, independiente de CAWbase, utilizable por Python y, opcionalmente, por HTTP, con persistencia propia, aislamiento real por alcance y autenticación por API keys en el adaptador HTTP.

**Casos de uso.**

| # | Caso | Camino |
|---|---|---|
| U1 | Un host Python (p. ej. un worker de CAWbase) instala el paquete, inicializa el motor con una URL o con su engine, y crea/lee alcances | API Python |
| U2 | Un host monta el adaptador HTTP en su aplicación, sin migraciones implícitas | `create_router` |
| U3 | Un operador despliega el servicio independiente: migra, hace bootstrap, crea clientes y claves | CLI + HTTP |
| U4 | Un cliente HTTP con clave acotada opera solo sobre sus alcances y recibe denegación en los demás | HTTP |
| U5 | Un despliegue Lambda usa solo la parte liviana, sin migrar en cold start | API Python |
| U6 | Un desarrollador ejecuta todo sobre SQLite sin servidor | Python/HTTP |

**Incluye** (ver §2–§17): empaquetado, versión y superficie pública; configuración, inicialización y cierre; persistencia y migraciones; contexto autorizado y aislamiento; alcance mínimo; clientes/API keys/permisos HTTP; diagnóstico, liveness y readiness; errores, observabilidad, límites y verificaciones.

**Justificación del alcance mínimo.** El aislamiento solo se puede acreditar con recursos persistidos pertenecientes a alcances distintos. El recurso `Scope` (nombre, descripción, metadatos acotados, estado, revisión) y su administración son lo mínimo para ello y para anclar los grants de las claves. No es un sistema de usuarios ni de tenants, ni adelanta perfiles, categorías ni tipos sensibles (A02) ([modelo](../persistence-and-migrations.md#22-scopes--recurso-de-alcance-p-13)).

**Exclusiones.** Clasificación, detección, ranking y mock de inferencia (A03); PDF/DOCX; protección, correspondencias y restauración; trabajos y almacenamiento documental; políticas, proveedores y dominio de CAWbase; elección de modelo, despliegue AWS, encendido/apagado de inferencia; OIDC, usuarios, contraseñas y autenticación de CAWbase (D-20); publicación pública y licencia definitiva; pesos o dependencias pesadas.

## 2. Arquitectura y dirección de dependencias

Un núcleo con dos fachadas (Python y HTTP); el núcleo no importa HTTP, CLI ni migraciones. Diagrama y reglas en [arquitectura §2](../architecture.md#2-componentes-y-dirección-de-dependencias).

## 3. Estructura de paquete y extras

Árbol en [arquitectura §3](../architecture.md#3-estructura-de-paquete-implementada). Extras (adoptados): base · `postgres` · `sqlite` · `migrations` · `http` (reservados, no creados: `inference`, `documents`). Detalle en [instalación §1](../installation-and-operations.md#1-instalación-y-extras-d-08-nombres-de-extras-adoptados).

## 4. API Python pública

Ciclo de vida (`ContentEngine`, `start`, `aclose`), contextos (`trusted_scope_context`, `trusted_admin_context`), fachadas (`scoped`, `admin`), operaciones, resultados, ownership y errores en [API pública §1–§5 y §8](../public-api.md#1-superficie-pública-y-versionado). Ejemplo de aceptación Python (sin CAWbase, HTTP ni modelo). La versión ejecutable y verificada es [examples/python_host.py](../../examples/python_host.py) (cubierta por `tests/test_examples.py`); el siguiente bloque es ilustrativo:

```python
# Ilustrativo (ver examples/python_host.py para el ejemplo ejecutable)
import asyncio
from cawbase_content_engine import ContentEngine, EngineConfig, ScopePermission, AdminPermission
from cawbase_content_engine.migrations import upgrade


async def main():
    config = EngineConfig(database_url="sqlite+aiosqlite:///./demo.db")
    await upgrade(config)  # explícito
    async with ContentEngine(config) as engine:
        admin = engine.trusted_admin_context({AdminPermission.ADMIN_SCOPES_WRITE, AdminPermission.ADMIN_SCOPES_READ})
        await engine.admin(admin).create_scope("a", "Alcance A")
        await engine.admin(admin).create_scope("b", "Alcance B")
        ctx_a = engine.trusted_scope_context("a", {ScopePermission.SCOPE_READ})
        print(await engine.scoped(ctx_a).get_scope("a"))  # OK
        await engine.scoped(ctx_a).get_scope("b")  # ResourceNotAccessibleError


asyncio.run(main())
```

## 5. Modelo persistente mínimo

Entidades `engine_state`, `scopes`, `clients`, `api_keys`, `api_key_admin_permissions`, `api_key_scope_grants`, `admin_events`: campos, restricciones, índices, relaciones y pertenencia al alcance en [persistencia §2](../persistence-and-migrations.md#2-modelo-persistente-mínimo).

## 6. Matriz de permisos (Python y HTTP)

Catálogo de 8 permisos (`scope:read|write`, `admin:scopes:read|write`, `admin:clients:read|write`, `admin:grants:write`, `admin:diagnostics:read`), reglas de delegación (D-27) y matriz completa en [seguridad §3](../security-and-isolation.md#matriz-de-permisos).

## 7. Contrato HTTP

Rutas, autenticación, permisos, schemas ilustrativos, códigos de estado y envoltorio de error en [API pública §9](../public-api.md#9-contrato-http). Catálogo de errores estables y su correspondencia HTTP en [API pública §8](../public-api.md#8-errores).

## 8. Ciclo de vida de API keys y bootstrap seguro

Formato y codificación canónica, hash, verificación, presentación única, rotación, revocación, bootstrap inicial y recuperación administrativa (sin clave universal ni endpoint abierto) en [seguridad §4](../security-and-isolation.md#4-ciclo-de-vida-de-las-api-keys-d-18).

## 9. Configuración, precedencia, validación y secretos

[Instalación y operación §2](../installation-and-operations.md#configuracion).

## 10. Transacciones propias y del host

[API pública §6](../public-api.md#6-transacciones) (contrato, ownership, errores recuperables vs. `transaction_broken`, concurrencia por conexión, precondiciones de SQLite y del engine inyectado). Diferencias de backend en [persistencia §6](../persistence-and-migrations.md#6-diferencias-reales-entre-postgresql-y-sqlite).

## 11. Migraciones, compatibilidad y coordinación

[Persistencia §4](../persistence-and-migrations.md#4-migraciones): cuándo migra cada contexto, una transacción y un bloqueo por secuencia, permisos DDL, compatibilidad, sin rollback universal.

## 12. Estados de arranque y readiness

[Instalación y operación §4](../installation-and-operations.md#estados-de-arranque-y-readiness): secuencia previa a escuchar (validar → migrar → verificar → servir; sin `/livez` ni `/readyz` durante la preparación; fallo ⇒ salida ≠ 0) y estados posteriores `ready`, `degraded_database`, `schema_incompatible`, `stopping`.

## 13. Concurrencia, reintentos e idempotencia

[API pública §7](../public-api.md#7-idempotencia-por-operación): qué ocurre al repetir cada operación y cómo se evita duplicar recursos o credenciales.

## 14. Retención, revocación y eliminación

[Seguridad §7](../security-and-isolation.md#retencion-revocacion-y-eliminacion): solo los datos de A01; sin borrado físico; copias de seguridad.

## 15. Observabilidad

Logs sin contenido ni secretos, métricas propuestas y reglas en [Instalación y operación §6](../installation-and-operations.md#observabilidad).

## 16. Límites operativos y diferencias PostgreSQL/SQLite

Límites iniciales y criterios de instalación liviana en [Instalación y operación §7](../installation-and-operations.md#limites-operativos); diferencias reales de concurrencia, DDL, bloqueo y recuperación en [Persistencia §6](../persistence-and-migrations.md#6-diferencias-reales-entre-postgresql-y-sqlite).

## 17. Matriz de aceptación

<a id="17-matriz-de-aceptacion"></a>

Las pruebas están **implementadas y fueron ejecutadas localmente en macOS** (ver el [informe](../a01-implementation-report.md#matriz-ac-trazable) para el test que cubre cada ID, backend y resultado). La columna «Estado» significa: probado en el entorno local; **no** probado en Linux ni en GitHub Actions, y **no** aceptado. «Evidencia» indica el artefacto que debe adjuntarse al acta definitiva (salida de la ejecución en Linux, versión y ambiente). Ninguna prueba requiere el mock de A03 ni modelo. Ningún control se difiere a A10 (D-21).

Ambientes: **L** = Linux automatizado (obligatorio para aceptar); **M** = macOS de desarrollo (informativo). Backends: **PG** = PostgreSQL (mínima y más reciente), **SQ** = SQLite (mínima y más reciente).

### 17.1 Instalación e importación

| ID | Prueba | Backend/Env | Evidencia requerida | Estado |
|---|---|---|---|---|
| AC-01 | Instalación limpia del paquete base (sin extras) en un entorno vacío; importa y expone `__version__` | L, M | salida de instalación y de `import`; lista de dependencias instaladas | Probado local (macOS); Linux/CI pendiente |
| AC-02 | Instalación limpia de base + `http` por separado; el adaptador funciona sin extras no declarados | L | idem, y listado | Probado local (macOS); Linux/CI pendiente |
| AC-03 | Instalaciones `postgres` y `sqlite` por separado; cada una opera sin el otro driver | L | idem | Probado local (macOS); Linux/CI pendiente |
| AC-04 | Importación liviana: `import cawbase_content_engine` no carga FastAPI, Starlette, Uvicorn, Alembic, drivers, inferencia ni librerías de documentos | L | diff de `sys.modules` vacío frente a la lista prohibida; tiempo medido ([criterios](../installation-and-operations.md#7-límites-operativos-y-criterios-de-instalación-liviana)) | Probado local (macOS); Linux/CI pendiente |
| AC-05 | Sin efectos al importar: sin sockets, archivos, hilos, rutas registradas, migraciones, handlers/niveles de logging, lectura de entorno | L | traza de un subproceso vigilado | Probado local (macOS); Linux/CI pendiente |
| AC-06 | Grafo de dependencias: núcleo no importa `http`, `cli`, `migrations`, FastAPI ni Alembic | L | resultado de la prueba de grafo | Probado local (macOS); Linux/CI pendiente |
| AC-07 | Ejemplo Python real (§4) con SQLite y con PostgreSQL, sin CAWbase, sin HTTP, sin modelo | L | ejecución del ejemplo con salida | Probado local (macOS); Linux/CI pendiente |

### 17.2 Configuración, ciclo de vida y transacciones

| ID | Prueba | Backend/Env | Evidencia | Estado |
|---|---|---|---|---|
| AC-10 | Modo URL: `start` crea pool; `aclose` lo libera | PG, SQ | prueba de recursos liberados | Probado local (macOS); Linux/CI pendiente |
| AC-11 | Engine inyectado: el motor **no** lo cierra ni hace `dispose`; el host lo sigue usando tras `aclose` | PG, SQ | aserción sobre el engine del host | Probado local (macOS); Linux/CI pendiente |
| AC-12 | Configuración ambigua (URL + engine), vacía, dialecto/driver no soportado, pool con engine inyectado, `*_FILE` + variable: se rechazan sin E/S | — | tabla de casos y errores `configuration_invalid` | Probado local (macOS); Linux/CI pendiente |
| AC-13 | URL con credenciales nunca aparece en `repr`, logs, errores ni diagnóstico | PG, SQ | búsqueda del secreto en todas las salidas capturadas | Probado local (macOS); Linux/CI pendiente |
| AC-14 | Transacción propia: éxito (commit visible a otra conexión) y rollback ante excepción; sin escritura parcial | PG, SQ | pruebas de atomicidad | Probado local (macOS); Linux/CI pendiente |
| AC-15 | Transacción del host: operación + trabajo del host; `commit` confirma ambos; `rollback` del host revierte también la operación del motor | PG, SQ | pruebas de ambos desenlaces | Probado local (macOS); Linux/CI pendiente |
| AC-16 | Error del motor dentro de transacción del host: se revierte solo el savepoint; la transacción del host sigue utilizable; el motor nunca confirma ni revierte la externa | PG, SQ | prueba con `ConflictError` y continuación del host | Probado local (macOS); Linux/CI pendiente |
| AC-17 | Precondiciones fallidas ⇒ errores explícitos: transacción inactiva ⇒ `transaction_state_invalid`; conexión de otro engine o motor en modo URL ⇒ `transaction_incompatible` | PG, SQ | tabla de casos | Probado local (macOS); Linux/CI pendiente |
| AC-18 | Engine compartido ≠ transacción compartida: operación en modo propio no ve cambios no confirmados del host y viceversa | PG | prueba de visibilidad | Probado local (macOS); Linux/CI pendiente |
| AC-80 | Rollback **exterior tras liberar el savepoint**: la operación del motor se completa (savepoint liberado) y luego el host hace rollback ⇒ los cambios del motor desaparecen | PG, SQ | prueba | Probado local (macOS); Linux/CI pendiente |
| AC-81 | Error **recuperable** (conflicto, validación, `IntegrityError` traducido): se revierte solo el savepoint y la transacción del host sigue utilizable y confirmable. La garantía **no** se afirma para otros errores | PG, SQ | prueba | Probado local (macOS); Linux/CI pendiente |
| AC-82 | **Pérdida de conexión**, transacción invalidada (PostgreSQL abortada) y fallo al revertir/liberar el savepoint ⇒ `transaction_broken` explícito; el motor no oculta ni repara el estado | PG, SQ | pruebas con conexión cortada y savepoint forzado a fallar | Probado local (macOS); Linux/CI pendiente |
| AC-83 | **Dos envoltorios de la misma conexión**: una operación en curso por uno; la del otro falla de inmediato con `transaction_state_invalid` (la guarda es por conexión subyacente) | PG, SQ | prueba concurrente | Probado local (macOS); Linux/CI pendiente |
| AC-84 | SQLite: con control transaccional legado (sin `BEGIN` real anterior al savepoint) el motor **rechaza** con `transaction_incompatible` sin ejecutar nada ni confirmar datos; con `BEGIN` real funciona. Se comprueba el estado del driver, no solo el de SQLAlchemy | SQ | prueba con ambos modos de driver | Probado local (macOS); Linux/CI pendiente |
| AC-85 | Engine inyectado incompatible (síncrono, driver no soportado, nivel de aislamiento PostgreSQL distinto de `READ COMMITTED`, SQLite sin control adecuado): rechazo explícito y el engine del host **queda sin modificar** (sin listeners, `isolation_level` ni pragmas cambiados) | PG, SQ | inspección del engine antes/después | Probado local (macOS); Linux/CI pendiente |
| AC-19 | Sin sesión global ni compartida entre tareas: N tareas concurrentes con operaciones independientes sin interferencia | PG, SQ | prueba concurrente | Probado local (macOS); Linux/CI pendiente |

### 17.3 Migraciones

| ID | Prueba | Backend/Env | Evidencia | Estado |
|---|---|---|---|---|
| AC-20 | Instalación en base con tablas/vistas/secuencias ajenas (incluyendo nombres próximos al prefijo): solo se crean objetos del namespace; ajenos intactos antes y después; el schema/prefijo con objetos no propios hace fallar la instalación | PG, SQ | inventario de objetos antes/después | Probado local (macOS); Linux/CI pendiente |
| AC-21 | Actualización de esquema (n → n+1 con una revisión de prueba) conserva datos y tablas ajenas | PG, SQ | idem | Probado local (macOS); Linux/CI pendiente |
| AC-22 | Repetir `upgrade` es no-op (`already_current`) | PG, SQ | informe de migración | Probado local (macOS); Linux/CI pendiente |
| AC-23 | Arranque concurrente (varios procesos/workers) con **varias revisiones pendientes**: uno aplica toda la secuencia en una transacción; los demás esperan, releen el esquema **tras** el bloqueo y no repiten ni deciden con un plan obsoleto | PG, SQ | contador de revisiones aplicadas, logs y verificación de que el plan se calcula después del bloqueo | Probado local (macOS); Linux/CI pendiente |
| AC-24 | Fallo **intermedio** (p. ej. falla la 2.ª de 3 revisiones): rollback completo, ninguna revisión ni historial queda aplicada, bloqueo liberado, proceso/servicio sin servir y salida ≠ 0 | PG, SQ | prueba con revisión que falla en el medio; esquema idéntico antes/después | Probado local (macOS); Linux/CI pendiente |
| AC-86 | Una ejecución de `upgrade` con **varias revisiones pendientes** usa una sola transacción y un solo bloqueo: ningún commit intermedio observable desde otra conexión; no hay autocommit ni conexión independiente | PG, SQ | trazas de sentencias y de transacciones | Probado local (macOS); Linux/CI pendiente |
| AC-87 | Alembic no confirma de forma intermedia con la transacción exterior (verifica el supuesto de §4.3 de persistencia) | PG, SQ | prueba con revisión de prueba que falla tras otra exitosa | Probado local (macOS); Linux/CI pendiente |
| AC-88 | Preservación de objetos ajenos también tras un fallo intermedio y tras la actualización multi-revisión | PG, SQ | inventario antes/después | Probado local (macOS); Linux/CI pendiente |
| AC-25 | Vencimiento de espera del bloqueo ⇒ `migration_lock_timeout`, servicio no listo | PG, SQ | prueba con bloqueo retenido | Probado local (macOS); Linux/CI pendiente |
| AC-26 | Permisos insuficientes: rol runtime sin DDL no puede migrar; con `migrate_on_start=false` el servicio arranca si el esquema es compatible; con `migration_database_url` separada, el runtime opera sin DDL | PG | pruebas con roles distintos | Probado local (macOS); Linux/CI pendiente |
| AC-27 | Versiones incompatibles: esquema ausente, anterior, con `min_runtime_level` mayor ⇒ errores y no-ready; esquema nuevo con runtime anterior compatible ⇒ ready | PG, SQ | tabla de casos | Probado local (macOS); Linux/CI pendiente |
| AC-28 | Uso embebido: `import`, `ContentEngine(...)`, `start()` y `create_router` **no** ejecutan migraciones | PG, SQ | base sin esquema permanece sin esquema | Probado local (macOS); Linux/CI pendiente |
| AC-29 | Servicio independiente: valida → migra → verifica con credenciales de runtime → escucha. **Mientras migra el puerto no escucha** (conexión rechazada, sin `/livez` ni `/readyz`); tras completar, `/readyz` 200. Con `migrate_on_start=false` y esquema compatible arranca sin migrar | PG, SQ | secuencia temporizada: intentos de conexión durante la migración y después | Probado local (macOS); Linux/CI pendiente |
| AC-30 | SQLite: la migración bloquea escritores del mismo archivo (documentado) y el host recibe error `retryable`; WAL permite lectores | SQ | prueba de concurrencia específica | Probado local (macOS); Linux/CI pendiente |

### 17.4 Aislamiento, claves y permisos

| ID | Prueba | Backend/Env | Evidencia | Estado |
|---|---|---|---|---|
| AC-40 | Python: dos alcances con recursos persistidos; un contexto de A no lee ni modifica B; `scope_id` ajeno ⇒ `ResourceNotAccessibleError` | PG, SQ | pruebas con filas reales | Probado local (macOS); Linux/CI pendiente |
| AC-41 | Contexto sin el permiso requerido ⇒ `PermissionDeniedError`; contexto de otra instancia rechazado | PG, SQ | tabla de casos | Probado local (macOS); Linux/CI pendiente |
| AC-42 | Operaciones administrativas inalcanzables con `ScopeContext` y viceversa (tipos y permisos) | PG, SQ | pruebas + verificación de tipos | Probado local (macOS); Linux/CI pendiente |
| AC-43 | HTTP: la clave de A opera sobre A (GET/PATCH autorizados) y recibe 404 idéntico sobre B existente y sobre alcance inexistente | PG, SQ | comparación byte a byte de respuestas | Probado local (macOS); Linux/CI pendiente |
| AC-44 | HTTP: clave válida con solo `scope:read` ⇒ PATCH 403; sin grants administrativos ⇒ 403 en `/v1/admin/*` | PG, SQ | tabla de casos | Probado local (macOS); Linux/CI pendiente |
| AC-45 | HTTP: sin clave, clave malformada (incl. variantes base64url no canónicas), desconocida, revocada, vencida y de cliente deshabilitado ⇒ 401 con estado, cuerpo y encabezados idénticos. No se afirma ni se mide igualdad de tiempos; una prueba unitaria comprueba que se usa comparación de tiempo constante y hash ficticio para `key_id` desconocido | PG, SQ | respuestas comparadas y prueba unitaria | Probado local (macOS); Linux/CI pendiente |
| AC-46 | Bootstrap inicial: solo con `admin_initialized_at` NULL; crea cliente, clave (catálogo completo de 6 permisos `admin:*`), evento y marca en una transacción; segundo intento ⇒ `bootstrap_not_allowed` **aunque no queden claves activas**; dos simultáneos ⇒ exactamente uno; sin endpoint HTTP de bootstrap | PG, SQ | pruebas de concurrencia | Probado local (macOS); Linux/CI pendiente |
| AC-47 | Emisión: secreto presentado una sola vez; solo el hash en base; listados sin secreto ni hash | PG, SQ | inspección de filas y respuestas | Probado local (macOS); Linux/CI pendiente |
| AC-48 | Revocación efectiva en la solicitud siguiente; deshabilitar cliente invalida todas sus claves | PG, SQ | pruebas HTTP | Probado local (macOS); Linux/CI pendiente |
| AC-49 | Delegación (D-27): ver AC-90 a AC-94 | PG, SQ | pruebas | Probado local (macOS); Linux/CI pendiente |
| AC-90 | **Delegación permitida:** emisor con `admin:clients:write` y grants propios emite una clave con un subconjunto de ellos; emisor con `admin:grants:write` concede `scope:read` sobre un alcance existente al que no tiene acceso directo | PG, SQ | pruebas Python y HTTP | Probado local (macOS); Linux/CI pendiente |
| AC-91 | **Escalada administrativa denegada:** emisor sin `admin:diagnostics:read` no puede conceder ese permiso; sin `admin:grants:write` no puede delegarlo; con ella tampoco puede conceder un permiso `admin:*` que no posee | PG, SQ | 403 `permission_denied` | Probado local (macOS); Linux/CI pendiente |
| AC-92 | **Alcance no autorizado denegado:** emisor sin `admin:grants:write` pide grants sobre un alcance sobre el que no tiene grant ⇒ 403, con respuesta idéntica exista o no el alcance | PG, SQ | comparación de respuestas | Probado local (macOS); Linux/CI pendiente |
| AC-93 | Con `admin:grants:write`, alcance inexistente ⇒ 422 `validation_failed`; sin comodines implícitos (`*` rechazado) | PG, SQ | pruebas | Probado local (macOS); Linux/CI pendiente |
| AC-94 | Catálogo: `GET /v1/auth/self` y `diagnostics` reflejan 8 permisos; bootstrap y recuperación emiten los 6 `admin:*` | PG, SQ | inspección | Probado local (macOS); Linux/CI pendiente |
| AC-95 | Recuperación administrativa: con todas las claves admin perdidas/vencidas/limitadas/de cliente deshabilitado emite clave nueva sin autenticarse, en cliente nuevo `recovery-…` sin colisión (incluso ante nombre repetido), con evento `admin.recovery_key_issued`; **no revoca ni deshabilita nada**; rechazada sin `--confirm` o si nunca hubo bootstrap | PG, SQ | pruebas | Probado local (macOS); Linux/CI pendiente |
| AC-96 | Recuperación y bootstrap concurrentes se serializan; no se crean dos administradores iniciales | PG, SQ | prueba de concurrencia | Probado local (macOS); Linux/CI pendiente |
| AC-97 | Entrega del secreto: archivo creado con `O_EXCL` y `0600` antes del commit; ruta inutilizable ⇒ falla sin emitir; commit fallido ⇒ archivo eliminado; el secreto no aparece en logs ni en la base | PG, SQ | inspección de permisos, logs y filas | Probado local (macOS); Linux/CI pendiente |
| AC-98 | Pérdida de salida tras el commit: el procedimiento documentado (recuperar, identificar, revocar la clave huérfana) funciona de extremo a extremo | PG, SQ | ejecución del procedimiento | Probado local (macOS); Linux/CI pendiente |
| AC-99 | Revocación: una revocación confirmada afecta a autenticaciones posteriores; una operación ya autorizada y en curso no se cancela retroactivamente; formato de clave: codificación canónica y hash de los 32 bytes crudos coinciden entre emisión y verificación (vectores de prueba) | PG, SQ | pruebas | Probado local (macOS); Linux/CI pendiente |
| AC-50 | Ausencia de secretos (clave, URL, hash, `Authorization`) y de valores de `metadata` en base (salvo hash), logs, errores y respuestas, excepto la entrega inicial autorizada | PG, SQ | búsqueda automática del secreto en todas las salidas | Probado local (macOS); Linux/CI pendiente |
| AC-51 | Ninguna llamada Python interna requiere API key | PG, SQ | ejemplo AC-07 | Probado local (macOS); Linux/CI pendiente |

### 17.5 HTTP, readiness y operación

| ID | Prueba | Backend/Env | Evidencia | Estado |
|---|---|---|---|---|
| AC-60 | Contrato HTTP de cada ruta: éxito, validación, límites (413/422), `If-Match` (412/428), conflicto, deshabilitado | PG, SQ | pruebas de contrato | Probado local (macOS); Linux/CI pendiente |
| AC-61 | `create_router` no registra rutas ni migra por sí mismo; el host decide el montaje | PG, SQ | prueba | Probado local (macOS); Linux/CI pendiente |
| AC-62 | `/livez` independiente de la base y `/readyz` según estados posteriores a escuchar; base caída tras `ready` ⇒ 503 `degraded_database`; esquema cambiado a incompatible ⇒ 503 `schema_incompatible` | PG | prueba | Probado local (macOS); Linux/CI pendiente |
| AC-63 | Diagnósticos (públicos y autenticado) no contienen URL, credenciales, rutas internas, hosts ni nombres de base | PG, SQ | verificación sobre todos los cuerpos | Probado local (macOS); Linux/CI pendiente |
| AC-64 | Mapeo de errores: cada código estable devuelve el estado HTTP definido; sin filtración de existencia entre alcances | PG, SQ | tabla de errores | Probado local (macOS); Linux/CI pendiente |
| AC-100 | Fallo de migración, incompatibilidad o timeout al arrancar el servicio: no se abre el puerto, error saneado en logs (sin URL ni credenciales) y salida ≠ 0; no existe opción de permanecer escuchando | PG, SQ | prueba de proceso | Probado local (macOS); Linux/CI pendiente |
| AC-101 | Múltiples workers: el lanzador prepara una vez y luego inicia workers; ningún worker migra; cada worker comprueba compatibilidad con credenciales de runtime y termina ≠ 0 si falla | PG | prueba con N workers | Probado local (macOS); Linux/CI pendiente |
| AC-102 | `migration_database_url` separada: el runtime opera sin DDL; documentado (y verificado como límite) que cerrar la conexión DDL no elimina las credenciales de la memoria ni del entorno | PG | prueba de roles + revisión | Probado local (macOS); Linux/CI pendiente |
| AC-65 | Límites operativos: cuerpo, metadatos, grants, páginas | PG, SQ | pruebas de borde | Probado local (macOS); Linux/CI pendiente |

### 17.6 Idempotencia y concurrencia

| ID | Prueba | Backend/Env | Evidencia | Estado |
|---|---|---|---|---|
| AC-70 | `create_scope`/`create_client`: repetición idéntica ⇒ mismo recurso; atributos distintos ⇒ conflicto; creación simultánea ⇒ un solo registro | PG, SQ | pruebas | Probado local (macOS); Linux/CI pendiente |
| AC-71 | `issue_api_key`: mismo `Idempotency-Key`+parámetros ⇒ una sola clave y respuesta sin secreto; parámetros distintos ⇒ 409; reintento concurrente ⇒ una sola fila | PG, SQ | recuento de filas | Probado local (macOS); Linux/CI pendiente |
| AC-72 | `update_scope` estricta (P-20, **pendiente de revisión**): actualizaciones concurrentes con la misma revisión ⇒ una gana, la otra 412; reintento con revisión antigua ⇒ 412 **aunque los valores ya estén aplicados**; revisión coincidente con valores iguales ⇒ éxito sin incrementar `revision`; sin `If-Match` ⇒ 428 | PG, SQ | pruebas | Probado local (macOS); Linux/CI pendiente |
| AC-73 | `revoke_api_key` y cambios de estado repetidos ⇒ éxito sin nuevos eventos | PG, SQ | recuento de `admin_events` | Probado local (macOS); Linux/CI pendiente |
| AC-74 | Diferencias y límites de concurrencia de SQLite (único escritor, `busy_timeout`, `retryable`) frente a PostgreSQL | PG, SQ | pruebas por backend con expectativas distintas | Probado local (macOS); Linux/CI pendiente |
| AC-75 | `admin_events` registra cada cambio administrativo en la misma transacción y sin secretos ni valores | PG, SQ | inspección | Probado local (macOS); Linux/CI pendiente |

### 17.7 Criterio de cierre de A01

A01 podrá proponerse para aceptación solo cuando **todas** las filas estén ejecutadas con evidencia en Linux, la documentación permanente esté alineada con lo implementado y el usuario emita el acta. **Este documento no es esa acta.**

## 18. Decisiones aprobadas, propuestas técnicas y pendientes

> Actualización de la implementación: P-20 se adoptó como D-30 y GitHub Actions como D-31; las elecciones técnicas P-01–P-04, P-10–P-12 y P-15–P-19 quedaron **adoptadas para esta implementación** (no como decisiones históricas del usuario) en [A01-decisions §4.3](../decisions/A01-decisions.md#43-elecciones-técnicas-adoptadas-para-esta-implementación). Lo que sigue es el estado previo a la implementación, conservado por trazabilidad.

| Categoría | Resumen | Detalle |
|---|---|---|
| Decisiones aprobadas | D-01 a D-29 (D-23 a D-29 resuelven P-05, P-06, P-07, P-08, P-09, P-13 y P-14): identidad, Python 3.13, macOS/Linux, PostgreSQL+SQLite, API asíncrona, núcleo liviano y extras, HTTP en A01 opcional, sin efectos al importar, URL xor engine, dos modos transaccionales, namespaces, migraciones, aislamiento, API keys | [A01-decisions §2](../decisions/A01-decisions.md#2-decisiones-aprobadas) |
| Propuestas que bloqueaban la implementación | Ninguna pendiente: P-05, P-06, P-07, P-08, P-09, P-13 y P-14 se resolvieron (D-23 a D-29) y P-20 se adoptó (D-30) | [A01-decisions §4.1](../decisions/A01-decisions.md#41-propuestas-resueltas-en-la-revisión-2) |
| Elecciones técnicas adoptadas para la implementación | P-01–P-04, P-10–P-12, P-15–P-19 (ver §4.3 de decisiones); revisables | [A01-decisions §4.3](../decisions/A01-decisions.md#43-elecciones-técnicas-adoptadas-para-esta-implementación) |
| Preguntas abiertas | Q-01 licencia y publicación, Q-04 purga de eventos, Q-05 otros drivers (Q-02 cerrada por D-31; Q-03 cerrada por D-25) | [A01-decisions §5](../decisions/A01-decisions.md#5-preguntas-abiertas) |


**Próximo paso.** Ejecutar el workflow en GitHub Actions (Linux), revisar el informe de implementación y, si procede, emitir el acta de aceptación (solo el usuario). Hasta entonces A01 **no está cerrada**.
