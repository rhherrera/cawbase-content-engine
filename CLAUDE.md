# CLAUDE.md — cawbase-content-engine

Instrucciones para trabajar en este repositorio. Idioma de la documentación: español; identificadores de código: inglés.

## Estado actual

- Fase: **A01 implementado (0.1.0) y probado localmente en macOS; no probado en Linux/GitHub Actions; no aceptado**. Rama `feature/a01-product-foundation`, cambios sin commitear. Informe con evidencia: `docs/a01-implementation-report.md`.
- Checks que existen (ejecutarlos antes de afirmar nada): `uv run ruff check .`, `uv run ruff format --check src tests examples scripts`, `uv run mypy`, `uv run pytest -q -rs` (con `CCE_TEST_POSTGRES_URL` y `CCE_REQUIRE_POSTGRES=1` para PostgreSQL), `uv run pytest -m dist -q`. Ver `docs/development.md`. El workflow de GitHub Actions está escrito pero **nunca se ejecutó**.
- Distingue siempre: **especificado** ≠ **implementado** (hay código) ≠ **probado** (checks ejecutados con evidencia) ≠ **aceptado** (acta del usuario). Un *skip* no es una aprobación. Nunca declares cerrada una feature ni simules un acta.
- Las pruebas de PostgreSQL usan solo servidores descartables y bases `cce_t_*` propias: nunca una base existente (p. ej. de CAWbase).

## Reglas permanentes

1. **Independencia de CAWbase.** El motor es un producto separado. No copies lógica de CAWbase, no trabajes en su backend, no mantengas variantes privadas. El motor no conoce organizaciones, proyectos ni membresías.
2. **Arquitectura y ownership.** Un núcleo liviano reutilizado por Python y HTTP; el núcleo no depende de FastAPI ni de Alembic. Engine por URL (lo posee el motor) o inyectado (lo posee el host). Transacción propia o provista por el host; el motor no confirma ni revierte la transacción del host. Detalle en `docs/architecture.md` y `docs/public-api.md`.
3. **Documentación antes de implementación.** No implementes una feature sin contrato revisado; el primer prompt de cada feature solo documenta. Una vez implementada, la documentación describe el código real: «propuesto» se sustituye por «implementado» solo cuando hay código y pruebas. Los documentos permanentes conservan los contratos; la especificación de feature define alcance y aceptación y los enlaza.
4. **Preserva los cambios del usuario.** No sobrescribas, descartes ni reviertas archivos ajenos (p. ej. `docs/STAGE_3_PLAYBOOK.md`, `.DS_Store`). Lee antes de editar. No hagas commit, push ni publiques paquetes salvo pedido explícito.
5. **Sin efectos al importar.** Importar no abre conexiones, no migra, no registra rutas, no carga modelos, no lee el entorno ni configura logging global. Inicialización y cierre son explícitos.
6. **API asíncrona y ambos backends.** E/S asíncrona. PostgreSQL (schema `cawbase_content_engine`) y SQLite (prefijo `cawbase_content_engine_`) se soportan y se prueban de verdad; SQLite no es una emulación de PostgreSQL. Nunca modifiques ni elimines tablas ajenas.
7. **Seguridad y aislamiento desde A01.** Todo acceso a datos de alcance exige el `scope_id` del contexto autorizado. Nunca autorices desde un identificador enviado por un frontend. API keys: solo hash, secreto mostrado una vez, sin clave universal ni endpoint de bootstrap abierto. Sin secretos, URLs de conexión ni contenido sensible en logs, errores o diagnósticos.
8. **Migraciones.** Solo el servicio HTTP independiente migra al arrancar, y **antes de escuchar** (sin HTTP durante la preparación; fallo ⇒ salida ≠ 0); la librería, el host embebido, el router montado y Lambda no migran implícitamente. Una ejecución de actualización = una transacción y un bloqueo. Solo hacia adelante; sin promesa de rollback universal.
9. **Fuera de alcance de A01:** clasificación/inferencia (A03), PDF/DOCX, protección y restauración, trabajos, CRUD de perfiles/categorías (A02), OIDC/usuarios/contraseñas, elección de modelo, AWS, publicación.

## Preferencias de colaboración

- En el flujo de revisión y preparación de tareas para otro agente, entregar siempre un único prompt completo, autocontenido y listo para copiar que incorpore las correcciones o el siguiente trabajo acordado. No terminar únicamente con recomendaciones ni preguntar si el usuario quiere el prompt. Esta regla no autoriza implementación cuando la tarea está limitada a documentación.
- No volver a pedir confirmación de decisiones que el usuario ya resolvió (D-nn); aplicarlas de forma consistente.

## Mantener la documentación alineada

- Cualquier cambio de contrato se refleja en el mismo cambio en: `docs/public-api.md`, `docs/persistence-and-migrations.md`, `docs/security-and-isolation.md`, `docs/installation-and-operations.md`, `docs/architecture.md`, `docs/decisions/A01-decisions.md` y la matriz de aceptación de `docs/features/A01-…md`.
- Revisa nombres de permisos (catálogo de 8), códigos de error, estados, rutas y enlaces relativos entre documentos antes de terminar. Enlaces sin romper **no** validan técnicamente el contrato.
- Las decisiones aprobadas (D-nn) solo las cambia el usuario; las propuestas (P-nn) se marcan como pendientes de revisión. No presentes una propuesta como decisión.
- Comandos, rutas o nombres futuros se rotulan «propuesto / no disponible» hasta que existan y estén probados. Cada cambio de código que altere un contrato actualiza la documentación y `docs/a01-implementation-report.md` en el mismo cambio.
- `docs/STAGE_3_PLAYBOOK.md` es una copia de referencia; no la modifiques ni la declares actualizada. Las diferencias se registran solo en `docs/decisions/A01-decisions.md`.

## Mapa de documentos

`README.md` · `docs/a01-implementation-report.md` · `docs/features/A01-base-del-producto-instalacion-independiente.md` · `docs/architecture.md` · `docs/public-api.md` · `docs/persistence-and-migrations.md` · `docs/security-and-isolation.md` · `docs/installation-and-operations.md` · `docs/development.md` · `docs/decisions/A01-decisions.md`
