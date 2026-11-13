# Contrato de inferencia y análisis con mock (A03)

> **Estado: A03 IMPLEMENTADA en `0.3.0` y **cerrada y aceptada por el usuario en `0.3.0`**, según su declaración explícita: commit de implementación y regresiones `20d9211`; `e3ca6da` contiene la documentación y evidencia posterior; ya mergeada en `main` (merge `ae19ef3`, PR #2; el merge es posterior y distinto de los commits mencionados). Resultado de GitHub Actions no verificado con la evidencia disponible (durante la entrega local no se había ejecutado).** Este documento describe el código real; evidencia por ambiente en el [informe de A03](a03-implementation-report.md). Las decisiones de producto son `D-A03-nn` (aprobadas por el usuario); las elecciones técnicas derivadas son `E-A03-nn` y las precisiones/decisiones de implementación `P-A03-nn`/`I-A03-nn` (no son decisiones de producto). Ver [A03-decisions](decisions/A03-decisions.md) y la [especificación de feature](features/A03-contrato-de-inferencia-y-analisis-con-mock.md). A01 y A02 están implementadas y aceptadas; este documento no las modifica.

Este documento conserva el contrato completo de A03. Los demás documentos permanentes solo lo referencian ([API §11](public-api.md#11-a03--análisis-de-texto-implementado), [seguridad §10](security-and-isolation.md#10-a03--permiso-y-datos-del-análisis-implementado), [persistencia §8](persistence-and-migrations.md#8-a03--sin-modelo-persistente-implementado), [operación §12](installation-and-operations.md#12-a03--límites-observabilidad-y-ciclo-de-vida-implementado), [arquitectura §9](architecture.md#9-a03--inferencia-y-análisis-implementado)).

## 0. Resumen

Se especifica **una operación**: analizar un texto con una **versión publicada** de un perfil (A02) y devolver **en la misma respuesta** clasificación y/o hallazgos sensibles localizados. No se persiste nada, no hay trabajos ni consultas posteriores (D-A03-01). La inferencia la hace un **adaptador** detrás de una interfaz estrecha; en A03 solo existe un **mock determinista** sobre fixtures sintéticos. El núcleo valida estrictamente la salida del adaptador y construye él mismo el resultado público (ranking, resolución, cobertura, estados).

Dos fronteras distintas (§1):

| Frontera | Quién la usa | Qué recibe | Qué devuelve |
|---|---|---|---|
| **API pública de análisis** (§2) | host Python / cliente HTTP autorizado | texto + selección de perfil/versión + capacidades + contexto auxiliar | `AnalysisResult` ya validado y calculado por el núcleo |
| **Interfaz del adaptador** (§4) | solo el núcleo | solicitud ya preparada (`InferenceRequest`) | salida **cruda** aún sujeta a validación (`RawInferenceOutput`) |

## 1. Fronteras y responsabilidades

```text
llamador ──► ScopedOperations.analyze_text / POST …/analyze-text
              │ 1 autoriza (analysis:execute) ─ 2 valida entrada ─ 3 PREPARA (lectura corta; fija versión; copia defensiva)
              │ 4 libera la conexión ─ 5 invoca al adaptador (timeout, cancelable) ─ 6 VALIDA la salida cruda
              │ 7 calcula ranking/resolución/cobertura/estados ─ 8 devuelve AnalysisResult
              ▼
        InferenceAdapter  (no ve tablas, perfiles, permisos, credenciales ni scope)
```

**El adaptador:** no consulta tablas ni resuelve perfiles; no autentica ni aplica permisos del motor; no elige una categoría ganadora por prioridad (ni recibe las prioridades ni la configuración de ranking); no decide políticas del consumidor; recibe solo los datos necesarios (§4.1), nunca credenciales, conexión de base, identificadores de scope/perfil/versión ni contexto administrativo. **Sus salidas se validan siempre**, también las del mock.

**El núcleo:** construye el resultado público; calcula elegibilidad, ranking y resolución (D-A02-05); deriva cobertura y estados globales; fija la procedencia desde la **configuración confiable** del host (nunca desde la salida del adaptador ni desde un campo del cliente). **No confía** en una ganadora, una cobertura global ni metadatos de autorización declarados por el adaptador: tales campos son desconocidos y la salida se rechaza (§5).

### 1.1 Versionado del contrato y procedencia (E-A03-01)

- `analysis_contract_version = 1` (forma del resultado público) e `inference_contract_version = 1` (forma de `InferenceRequest`/`RawInferenceOutput`). Cambios incompatibles incrementan el número; el núcleo rechaza al componer un adaptador cuyo `descriptor.inference_contract_version` no soporte (`configuration_invalid`) y rechaza una salida con `contract_version` distinto (`inference_output_invalid`).
- **Procedencia** (`Provenance`), derivada del `AdapterDescriptor` que el host entrega al componer el motor:

| Campo | Significado |
|---|---|
| `kind` | `mock` \| `model` (en A03 solo `mock`; `model` queda reservado para el adaptador real de A09) |
| `simulated` | `true` ⇔ `kind = "mock"`; lo calcula el núcleo, no el adaptador |
| `adapter_id`, `adapter_version` | slug (`^[a-z0-9][a-z0-9._-]{0,63}$`) del adaptador y su versión |
| `configuration_id` | identifica la configuración efectiva del adaptador (en el mock, `fxs-` + 16 hex del resumen del conjunto de fixtures); `null` si no aplica |
| `model` | `null` en el mock (**no se inventa modelo ni pesos**); para un adaptador real, `{"id", "version"}` cuando existan |
| `inference_contract_version` | versión del contrato del adaptador |

El cliente **no** puede enviar ni influir en la procedencia.

## 2. API pública de análisis

### 2.1 Firma Python

```python
# Ejemplo ilustrativo — implementado en 0.3.0
class ScopedOperations:
    async def analyze_text(
        self,
        scope_id: str,
        *,
        text: str,
        profile_id: str | None = None,
        version_number: int | None = None,
        capabilities: Collection[str] = ("classification", "detection"),
        context: str | None = None,
        request_id: str | None = None,       # correlación; HTTP pasa el X-Request-ID
    ) -> AnalysisResult: ...

class ContentEngine:
    def __init__(self, config: EngineConfig, *, inference: InferenceSetup | None = None) -> None: ...
    async def inference_status(self) -> InferenceStatus: ...
```

- Requiere `ScopeContext` con **`analysis:execute`** (§2.5). Con `HostTransaction` se rechaza (§3.3).
- La llamada es asíncrona (D-06). «Asíncrona» describe la API Python: el resultado se obtiene en la misma llamada/petición; no hay 202, polling, cola ni trabajo duradero.
- `AnalysisResult` es un modelo inmutable (como los modelos de A01/A02); sus listas son tuplas.

### 2.2 Entrada

| Campo | Tipo | Obligatorio | Regla |
|---|---|---|---|
| `text` | `str` | sí | Texto **exacto** a analizar (§2.3) |
| `profile_id` | slug | no | Semántica de A02 [§8](profiles-and-catalog.md#8-resolver-un-perfil-y-una-versión-publicados): sin él, perfil predeterminado del scope |
| `version_number` | entero ≥ 1 | no | Sin él, versión publicada vigente. **Sin `profile_id` es inválido** (`validation_failed`) |
| `capabilities` | lista de `classification` \| `detection` | no | Predeterminado: **ambas**. Sin duplicados ni valores desconocidos; vacía ⇒ `validation_failed` (E-A03-04) |
| `context` | `str` \| `null` | no | Contexto auxiliar (§2.4). Cadena vacía ⇒ `validation_failed` (omitir o `null` para «sin contexto») |

No existen overrides de ranking, prioridades, umbrales, definiciones ni procedencia por solicitud (D-A02-05; B-A02-08 sigue en backlog).

### 2.3 El texto objetivo (E-A03-03)

- **Exacto:** no se normaliza (ni NFC/NFKC), recorta, reemplaza ni altera de ningún modo; `\r\n` son dos puntos de código; los espacios y saltos de línea cuentan.
- **Vacío:** longitud 0 ⇒ `validation_failed`. Un texto compuesto solo de espacios es válido (se analiza tal cual; el motor no decide que «no tiene contenido»).
- **Unicode inválido:** un `str` con sustitutos aislados (U+D800–U+DFFF) no es codificable en UTF-8 ⇒ `validation_failed`. En HTTP, un escape `\ud800` aislado en el JSON produce el mismo error. Se acepta cualquier otro punto de código (incluidos controles y no caracteres).
- **Unidad de medida:** la longitud y todos los índices se expresan en **puntos de código Unicode** (los de `len(str)` en Python), no en bytes, unidades UTF-16 ni grafemas.

### 2.4 El contexto auxiliar (E-A03-05)

Texto opcional que acompaña al objetivo para desambiguar (p. ej. el título del documento del que proviene el fragmento). Contrato:

- Es **dato no confiable**: nunca es una instrucción que modifique el contrato, la configuración, la selección de perfil, el ranking ni las políticas. Puede contener órdenes dirigidas «al modelo»; el motor lo entrega al adaptador rotulado como `auxiliary_context` y el adaptador debe tratarlo como dato.
- **No es contenido cubierto:** la cobertura se calcula solo sobre `text`; ningún hallazgo puede localizarse en el contexto (los índices son siempre del texto objetivo); no se analiza ni se protege.
- Si el consumidor necesita analizarlo, debe **incluirlo en `text`**.
- No sirve para introducir overrides ni políticas; los campos desconocidos del cuerpo se rechazan.
- Se valida con las mismas reglas Unicode que `text` y tiene sus propios límites (§2.6).

### 2.5 Permiso `analysis:execute` (D-A03-02)

- Nuevo permiso **de scope**: suficiente para ejecutar análisis con un perfil publicado **sin** exigir `profiles:read`.
- **No concede** lectura de definiciones, ejemplos, instantáneas ni versiones: el resultado no devuelve las definiciones del perfil (solo la identidad de la versión fijada, §6.1).
- `profiles:read`, `profiles:write`, `scope:*` y `admin:*` **no** conceden análisis implícitamente; `analysis:execute` **no** concede ninguno de ellos. Sin jerarquías.
- `profiles:read` conserva su significado y sus operaciones (incluida `resolve_profile`).
- Se reutilizan `ScopeContext`, grants, API keys y delegación (D-27): es un permiso de scope más (el catálogo pasa de 10 a **11**); emitir una clave con él sigue las mismas reglas de subconjunto del emisor. Los grants existentes no cambian ni se reinterpretan.
- Un `AdminContext` no ejecuta análisis.
- **Restricción de despliegue** (E-A03-14): ver [seguridad §10.3](security-and-isolation.md#103-compatibilidad-y-despliegue-del-permiso-nuevo).

### 2.6 Límites iniciales (E-A03-06)

Valores iniciales, configurables mediante `Limits` (campos de `Limits`; **no se amplían** los límites de otras rutas: el cuerpo HTTP sigue en 64 KiB y los límites de A02 no cambian). No son una medida de capacidad de ningún modelo.

| Límite (`Limits`) | Valor | Se mide en |
|---|---|---|
| `max_analysis_text_chars` | 8192 | puntos de código Unicode |
| `max_analysis_text_bytes` | 24576 | bytes UTF-8 del texto |
| `max_analysis_context_chars` | 2048 | puntos de código |
| `max_analysis_context_bytes` | 6144 | bytes UTF-8 |
| `max_analysis_findings` | 500 | hallazgos por solicitud (salida del adaptador) |
| `max_finding_chars` | 2048 | longitud de un hallazgo (`end − start`) |
| `max_analysis_regions` | 256 | regiones de cobertura por capacidad |
| `max_inference_output_bytes` | 1 048 576 | JSON canónico UTF-8 de la salida cruda |
| `max_body_bytes` (existente) | 65 536 | bytes del cuerpo HTTP **serializado** |

- Se distinguen **tres unidades**: puntos de código (longitud lógica del texto), bytes UTF-8 (tamaño de transporte/almacenamiento) y, en el futuro, **tokens** del modelo concreto (A09). Un límite de tokens **no existe** en A03; cuando exista, un texto que quepa en el motor pero no en el modelo se resolverá con procesamiento declarado parcial (`inconclusive`, §6.5) o con un error del adaptador real, nunca con truncamiento silencioso.
- Exceder un límite de entrada ⇒ `limit_exceeded` (422); **nunca se trunca**. Un cuerpo HTTP mayor de 64 KiB ⇒ `limit_exceeded` con HTTP 413 (`PayloadTooLargeError`, el código vigente de A01/A02; no se introduce otro código ni se cambia el middleware) antes de interpretar el JSON. Como el límite HTTP cuenta la serialización, un cliente que envíe escapes ASCII (`\u00e9`, pares sustitutos de 12 bytes por punto de código no BMP) o muchos caracteres de control (6 bytes cada uno) alcanzará el 413 antes que `max_analysis_text_chars`; se recomienda enviar UTF-8 directo. Con UTF-8 directo, texto (≤ 24 576 B) + contexto (≤ 6144 B) + marco JSON caben en 64 KiB salvo escapes de control.
- Exceder un límite de salida del adaptador (hallazgos, regiones, bytes) ⇒ `inference_output_invalid` (§5): **no** se recortan hallazgos para hacerlos caber.

## 3. Preparación, versiones y transacciones

### 3.1 Orden de operaciones (E-A03-07)

1. **Autorización:** `scope_id` bien formado, igual al del contexto (si no, `resource_not_accessible`) y permiso `analysis:execute` (si no, `permission_denied`). Igual que en A02: el id enviado no autoriza nada.
2. **Transacción del host:** si la operación se creó con `HostTransaction` ⇒ `transaction_incompatible` (§3.3); no se toca la transacción.
3. **Validación de entrada** (§2.2–§2.6) ⇒ `validation_failed` / `limit_exceeded`.
4. **Inferencia configurada:** sin `InferenceSetup` ⇒ `inference_not_configured` (503).
5. **Preparación** (una lectura corta con transacción propia; §3.2).
6. **Capacidades ejecutables** (§6.2); si ninguna ⇒ `capabilities_not_configured`.
7. Liberación de la conexión; invocación del adaptador (§4); validación (§5); construcción del resultado (§6).

### 3.2 Preparación y fijación de la versión

La preparación es **una transacción propia de solo lectura, corta**, que comparte con `resolve_profile` la lógica interna de selección. Se extrae una función interna (`select_published_version`, **sin** comprobación de permisos y no pública) que aplica exactamente las reglas de A02 §8 y de la que cuelgan dos operaciones con autorizaciones **distintas**:

| Operación pública | Permiso | Usa la selección para |
|---|---|---|
| `resolve_profile` (A02, sin cambios de contrato) | `profiles:read` | devolver la versión completa |
| `analyze_text` (A03) | `analysis:execute` | fijar la versión y **solo** su identidad y las definiciones necesarias para el adaptador |

No hay ruta pública que salte permisos: la comprobación vive en la fachada de cada operación, antes de llamar a la función compartida. Regresión obligatoria: `resolve_profile` conserva su comportamiento, errores y permiso.

Reglas de selección heredadas (sin cambios): sin `profile_id` ⇒ predeterminado (`default_profile_not_set` si falta); sin `version_number` ⇒ versión publicada vigente (`profile_not_published` si no hay); `version_number` sin `profile_id` ⇒ `validation_failed`; alcance deshabilitado ⇒ `scope_disabled`; perfil `disabled`/`deleted` ⇒ `profile_not_selectable` **incluso con versión explícita**; perfil o versión inexistente ⇒ `resource_not_found`. Que una versión histórica sea **consultable** (A02) no implica que sea **ejecutable** en una versión explícita de un perfil no seleccionable. **Nunca** se lee configuración editable: solo la fila de versión publicada (inmutable).

**Fijación.** El resultado de la preparación es un `PreparedAnalysis` interno e inmutable con la identidad de la versión (`scope_id`, `profile_id`, `version_id`, `version_number`, `content_hash`, `selection`), `ranking` (modo, umbral, tolerancia), `capabilities` de la versión y los elementos habilitados con sus prioridades. Todo el análisis posterior usa **solo** esos datos.

**Punto de autorización y concurrencia (precisión de implementación P-A03-06).** La selección compartida es una **secuencia de lecturas sucesivas** (estado del alcance, predeterminado si aplica, fila del perfil, fila de la versión) dentro de una transacción propia de solo lectura. **No es una instantánea atómica** ni hay una comprobación final de todos los estados: en PostgreSQL (`READ COMMITTED`) cada sentencia ve lo confirmado hasta ese momento; en SQLite la lectura ve la base tal como esté. Consecuencias:

- Una desactivación/eliminación del perfil, o una deshabilitación del alcance, **confirmada antes de iniciar la preparación** impide una ejecución nueva (`profile_not_selectable` / `scope_disabled`).
- Un cambio **confirmado mientras se ejecutan las lecturas** puede observarse o no, según la sentencia a la que llegue; se rige por las lecturas documentadas (un alcance deshabilitado después de leerse su estado no cancela la preparación en curso). La versión devuelta es siempre una versión **publicada real** (las filas de versión son inmutables) y seleccionable en el momento de la lectura de la fila del perfil.
- Los cambios **posteriores** a la preparación no cancelan retroactivamente: publicar una versión nueva no cambia un análisis ya preparado; desactivar/eliminar el perfil o deshabilitar el alcance solo impide análisis **nuevos** (no se inventa cancelación; el tratamiento de trabajos es de A05).
- No se introducen bloqueos de fila ni se mantiene conexión durante la inferencia.

### 3.3 Sin transacciones ni conexiones durante la inferencia

- La preparación termina (commit de la lectura propia y **devolución de la conexión al pool**) **antes** de invocar al adaptador. Ninguna conexión ni transacción del motor permanece abierta durante la inferencia.
- `analyze_text` sobre un `ScopedOperations` creado con `HostTransaction` se **rechaza antes** de invocar al adaptador con `transaction_incompatible` (`IncompatibleTransactionError`, sin estado HTTP, no reintentable): una llamada larga no debe retener la transacción del host, y el motor no sabría si la transacción exterior se confirma o revierte. El motor **no confirma, no revierte ni invalida** la transacción del host; no emite `SAVEPOINT`.

### 3.4 Aislamiento de datos preparados (copia defensiva, E-A03-08)

`ProfileVersion.snapshot` es un `dict` local mutable (la fila publicada es inmutable, pero el objeto devuelto por A02 no). A03 **no cambia ese contrato**. En la preparación el núcleo:

1. toma los datos de la fila y construye `PreparedAnalysis` como estructuras **inmutables propias** (dataclasses congeladas, tuplas, `str`/`float`/`int`), sin conservar referencias al `dict` original;
2. genera el `InferenceRequest` (§4.1) como **otra copia independiente** hecha desde `PreparedAnalysis`;
3. valida la salida contra `PreparedAnalysis`, nunca contra el `InferenceRequest` ni contra objetos del llamador o del adaptador.

Así, mutar el `dict` de una versión leída por el llamador, o cualquier objeto que reciba o devuelva el adaptador, no altera el análisis ni sus validaciones. Los parámetros del llamador (`capabilities`, `text`, `context`) se copian a valores inmutables en la entrada.

## 4. Interfaz del adaptador de inferencia

```python
# Ejemplo ilustrativo — implementado en 0.3.0
class InferenceAdapter(Protocol):
    @property
    def descriptor(self) -> AdapterDescriptor: ...          # síncrono, sin E/S
    async def probe(self) -> AdapterState: ...              # no enciende nada ni espera readiness
    async def infer(self, request: InferenceRequest) -> Mapping[str, Any]: ...   # salida CRUDA

@dataclass(frozen=True)
class InferenceSetup:
    adapter: InferenceAdapter
    timeout_s: float = 30.0          # finito, 0 < timeout_s ≤ 600
```

- **Selección explícita al componer:** `ContentEngine(config, inference=InferenceSetup(adapter))`. Sin `inference`, el motor sigue sirviendo A01/A02 normalmente (E-A03-12).
- **Ownership:** el adaptador es del **host**; el motor nunca lo inicia ni lo cierra (`aclose()` no lo toca). Construir `InferenceSetup`/el motor no hace E/S; solo valida el descriptor.
- **Concurrencia:** el motor puede llamar a `infer` concurrentemente; el adaptador debe ser reentrante. El motor no mantiene estado mutable compartido entre solicitudes. Un límite de concurrencia, si hiciera falta, es del adaptador real (A09).
- **`probe()` → `AdapterState`:** `ready` \| `loading` \| `stopped` \| `unavailable`. Es de solo lectura, acotado por un plazo corto (mín(`timeout_s`, 5 s)) y **no** enciende infraestructura ni espera readiness. `ContentEngine.inference_status()` devuelve `InferenceStatus(configured, state, provenance)` con `state ∈ {not_configured, ready, loading, stopped, unavailable, unknown}` (`unknown` si `probe` excede el plazo o falla). **`readiness()` y `/readyz` no cambian**: «listo» significa motor y base de datos (A01); el motor con base disponible pero sin inferencia **no se declara listo para inferencia** (E-A03-12). La exposición HTTP del estado de inferencia queda en backlog (B-A03-01).

### 4.1 `InferenceRequest` (lo único que recibe el adaptador)

Objeto congelado; solo contiene lo necesario:

| Campo | Contenido |
|---|---|
| `contract_version` | `1` |
| `request_id` | id de correlación (el mismo del resultado) |
| `text` | el texto exacto |
| `auxiliary_context` | `str` o `null`; **dato no confiable; no cubierto** (§2.4) |
| `capabilities` | solo las capacidades **ejecutables** (pedidas ∩ configuradas), ordenadas (`classification` < `detection`) |
| `language` | idioma del perfil (`es`/`en`) |
| `categories` | si `classification` ejecutable: tuplas `ElementDefinition(element_id, name, definition, examples_positive, examples_negative, translations)` de las categorías **habilitadas de la versión fijada**, ordenadas por id. **Sin prioridad** |
| `sensitive_types` | si `detection` ejecutable: ídem para los tipos |
| `definitions_digest` | SHA-256 (hex) del JSON canónico (UTF-8, claves ordenadas, separadores `,`/`:`, sin escapes ASCII) de `{"language", "categories", "sensitive_types"}`: el **idioma** del perfil y, de las capacidades ejecutables (una capacidad omitida aporta lista vacía), `{id, name, definition, examples:{positive,negative}, translations}` por elemento, ordenado por id. **Sin** prioridades, modo, umbral, tolerancia ni procedencia: cambiar solo el ranking no invalida un fixture; cambiar cualquier definición o el idioma sí. Las capacidades ejecutables forman parte de la clave del fixture por separado |
| `limits` | `max_findings`, `max_finding_chars`, `max_regions`, `max_output_bytes` |

**No** se envían: scope, perfil, versión, hash de la versión, modo/umbral/tolerancia, prioridades, permisos, credenciales, conexión ni identidad del cliente. `definitions_digest` es lo que asocia de forma verificable una respuesta a una configuración (§7).

### 4.2 `RawInferenceOutput` (JSON-compatible, sujeta a validación)

```jsonc
{
  "contract_version": 1,
  "classification": {                 // presente ⇔ la capacidad se envió en capabilities
    "status": "completed",            // completed | inconclusive
    "reason": null,                   // null si completed; partial_coverage | abstained | truncated si inconclusive
    "coverage": { "groups": [ { "ids": ["finance", "hr", "legal"], "regions": [ { "start": 0, "end": 87 } ] } ] },
    "scores": [ { "category_id": "finance", "score": 0.82 }, { "category_id": "hr", "score": 0.31 }, { "category_id": "legal", "score": 0.80 } ]
  },
  "detection": {                      // presente ⇔ la capacidad se envió
    "status": "completed",
    "reason": null,
    "coverage": { "groups": [ { "ids": ["password", "personal_data"], "regions": [ { "start": 0, "end": 87 } ] } ] },
    "findings": [ { "type_id": "personal_data", "start": 12, "end": 23, "text": "Ana Ejemplo" } ]
  }
}
```

- `findings[].text` es **solo para verificación** (§6.4): el núcleo lo comprueba y **lo descarta**; no aparece en el resultado público.
- No hay campos de procedencia, ganadora, cobertura global, estado global, permisos ni explicaciones libres: cualquier campo no listado se rechaza (`unknown_field`). No se admiten razonamientos ni cadenas de pensamiento como parte del contrato.
- La salida es un `Mapping` serializable a JSON (str, int, float, bool, null, listas, mapas con claves `str`). Valores no serializables ⇒ `output_not_serializable`.

### 4.3 Cobertura en la salida (E-A03-09)

Una capacidad declara su cobertura como **grupos**: cada grupo es un conjunto de ids (categorías o tipos de esa capacidad) evaluados sobre **las mismas regiones** del texto. Cada id aparece en **a lo sumo un** grupo; un id que no aparece en ninguno está **no evaluado**. Esto permite que distintos elementos tengan cobertura distinta sin ambigüedad, y una cobertura común conservadora es el caso de un solo grupo.

- Región: `{start, end}` enteros con `0 ≤ start < end ≤ len(text)` (semiabierta `[start, end)`).
- Las regiones de un grupo son **canónicas**: ordenadas, disjuntas y **no adyacentes** (`end_i < start_{i+1}`). Una lista no canónica se **rechaza** (`regions_not_canonical`), no se normaliza.
- Un grupo no puede tener `ids` ni `regions` vacíos.
- «Cobertura» (texto procesado) y «elementos evaluados» son cosas distintas: el primero son las regiones; el segundo, los ids de los grupos.

## 5. Validación estricta de la salida (E-A03-10)

El núcleo valida **antes** de calcular nada. Toda violación ⇒ `inference_output_invalid` (502, no reintentable) con `problems` de **solo códigos** (§8.2). No se rescatan fragmentos, no se corrigen offsets, no se descartan elementos «malos» para devolver los demás.

| Grupo | Reglas (código del problema) |
|---|---|
| Forma | La salida es un mapa serializable (`output_not_object`, `output_not_serializable`); `contract_version == 1` (`contract_version_mismatch`); campos desconocidos (`unknown_field`) o faltantes (`missing_field`); tipos erróneos, incluidos booleanos donde se espera número y floats donde se espera entero (`invalid_type`); tamaño canónico ≤ `max_inference_output_bytes` (`output_too_large`) |
| Capacidades | Toda capacidad enviada está presente (`capability_missing`); ninguna no enviada lo está (`capability_not_executed`) |
| Estado | `status ∈ {completed, inconclusive}` (`invalid_status`); `reason` nulo si `completed` y obligatorio y del conjunto cerrado si `inconclusive` (`reason_invalid`); `completed` exige cobertura completa de la capacidad (todos los ids, todo el texto) (`status_coverage_mismatch`); `reason = partial_coverage` exige cobertura **incompleta** (`reason_coverage_mismatch`) |
| Cobertura | ids conocidos de **esa** capacidad (`unknown_id`, sin repetir el id en el error) y sin duplicados entre grupos (`duplicate_id`); grupos y regiones no vacíos (`empty_group`); regiones en rango y canónicas (`region_out_of_range`, `regions_not_canonical`); máximo de regiones (`regions_limit_exceeded`) |
| Puntuaciones | números reales en [0, 1]; se rechazan booleanos, NaN, ±∞, cadenas y **cualquier entero o flotante fuera de rango, incluidos enteros enormes de cualquier signo** (`invalid_score`; se comparan sin convertir a `float`, de modo que no hay desbordamiento ni se convierten en 500); no se recortan ni normalizan; ids conocidos y sin duplicados (`unknown_id`, `duplicate_id`); el conjunto de ids puntuados **coincide exactamente** con el de ids evaluados en la cobertura (`scores_coverage_mismatch`) |
| Hallazgos | `type_id` conocido (`unknown_id`); `start`/`end` enteros con `0 ≤ start < end ≤ len(text)` (`invalid_offset`, `offset_out_of_range`); `end − start ≤ max_finding_chars` (`finding_too_long`); `text == texto[start:end]` por puntos de código (`fragment_mismatch`); el hallazgo está contenido en una región de cobertura de **su** tipo (`finding_outside_coverage`, también si el tipo no fue evaluado); sin duplicados exactos `(type_id, start, end)` (`duplicate_finding`); total ≤ `max_analysis_findings` (`findings_limit_exceeded`) |

Aclaraciones:

- **JSON válido pero incorrecto** (offsets, ids, puntuaciones, contradicciones internas) y **JSON cortado/estructura inválida** son ambos «salida inválida». Un adaptador remoto futuro que no pueda siquiera producir un mapa (JSON cortado) levanta `inference_output_invalid` con `output_not_object`/`output_not_serializable`.
- Una respuesta **válida** que declara procesamiento parcial, abstención o truncamiento semántico **no** es «salida inválida»: se conserva (§6.5).
- La validación usa `PreparedAnalysis` (copia propia) y no confía en el objeto devuelto tras validarlo: se copia a estructuras inmutables antes de usarlo.

## 6. Construcción del resultado público

### 6.1 `AnalysisResult` (esquema ilustrativo)

```jsonc
{
  "analysis_contract_version": 1,
  "request_id": "9f1c…",                     // correlación; igual al X-Request-ID en HTTP; NO deduplica
  "status": "completed",                     // derivado (§6.6)
  "profile": {                               // versión fijada; sin snapshot ni definiciones
    "scope_id": "acme-team", "profile_id": "contratos", "version_id": "pvr_…",
    "version_number": 3, "content_hash": "…64 hex…", "selection": "default"
  },
  "input": { "text_length": 87, "context_provided": false },     // puntos de código
  "provenance": { "kind": "mock", "simulated": true, "adapter_id": "cce-mock-fixtures",
                  "adapter_version": "1", "configuration_id": "fxs-0123456789abcdef",
                  "model": null, "inference_contract_version": 1 },
  "classification": { … },                   // §6.3
  "detection": { … }                         // §6.4
}
```

`profile.selection ∈ {explicit, default}` (como `ResolvedProfile`). No se devuelven definiciones, ejemplos, instantáneas, el texto, el contexto ni fragmentos.

### 6.2 Estados por capacidad (E-A03-04)

Cada capacidad (`classification`, `detection`) tiene exactamente **uno** de cuatro estados, siempre presente en el resultado:

| Estado | Significado | Contenido |
|---|---|---|
| `not_requested` | el llamador no la pidió en `capabilities` | solo `{"status": "not_requested"}` |
| `not_configured` | pedida, pero la versión fijada no la configura (sin categorías / sin tipos `enabled`; `capabilities` de la instantánea) | solo `{"status": "not_configured"}` |
| `completed` | ejecutada, salida válida con cobertura completa de lo configurado | resultados + cobertura |
| `inconclusive` | ejecutada, salida válida que declara procesamiento parcial, abstención o truncamiento | `reason` + resultados verificables parciales + cobertura |

- **Precedencia:** `not_requested` prevalece sobre `not_configured` (si no se pidió, no se evalúa si estaba configurada).
- Las capacidades `not_requested`/`not_configured` **no tienen** `ranking`, `findings` ni listas vacías: la ausencia del campo significa «no se evaluó». Una lista vacía solo aparece con `completed`/`inconclusive` y significa «evaluado y no encontrado **dentro de la cobertura declarada**».
- **Ninguna capacidad ejecutable:** si todas las pedidas están `not_configured` ⇒ error `capabilities_not_configured` (409; el adaptador **no** se invoca; sin resultado vacío exitoso). Si al menos una es ejecutable, la otra se informa con su estado y la respuesta es 200.

### 6.3 Clasificación

```jsonc
"classification": {
  "status": "completed", "reason": null,
  "scale": { "min": 0, "max": 1, "calibrated": false },
  "mode": "resolution", "eligibility_threshold": 0.5, "tie_tolerance": 0.05,   // de la versión fijada
  "coverage": { "complete": true, "text_length": 87,
                "groups": [ { "ids": ["finance", "hr", "legal"], "regions": [ { "start": 0, "end": 87 } ] } ],
                "not_evaluated_ids": [] },
  "ranking": [
    { "rank": 1, "category_id": "finance", "score": 0.82, "eligible": true },
    { "rank": 2, "category_id": "legal",   "score": 0.80, "eligible": true },
    { "rank": 3, "category_id": "hr",      "score": 0.31, "eligible": false }
  ],
  "resolution": { "selected_category_id": "finance", "reason": "priority_tiebreak",
                  "candidate_category_ids": ["finance", "legal"] }
}
```

(Ejemplo con una versión fijada en modo `resolution`, umbral 0.5, tolerancia 0.05 y prioridades `finance = 1`, `legal = 2`, `hr = 3`: `finance` y `legal` son elegibles y `0.82 − 0.80 = 0.02 ≤ 0.05`, así que ambas son candidatas y gana la de menor número de prioridad aunque la diferencia de puntuación sea mínima; `hr` (0.31) no es elegible. El adaptador solo devolvió las tres puntuaciones; no vio prioridades ni modo.)

Reglas (D-A02-05 sin cambios; E-A03-11 para los bordes numéricos):

1. **Puntuación:** finita en [0, 1], no probabilidad calibrada, no suma uno (`scale.calibrated = false`).
2. **Elegible** ⇔ `score ≥ eligibility_threshold`.
3. **Ranking:** todas las categorías **evaluadas**, por puntuación descendente; `rank = 1 + (cantidad de categorías con puntuación estrictamente mayor)` (los empates comparten `rank` y se conservan como tales); desempate **técnico** estable por `category_id` ascendente, **sin** significado de prioridad de negocio. Sin top-k, sin umbral que excluya filas: las no elegibles aparecen con `eligible = false`. **En modo `ranking` no hay ganadora implícita**: la clasificación ejecutada en modo `ranking` lleva `"resolution": null` (el campo siempre está presente en una clasificación ejecutada). En modo `resolution` es el objeto `{selected_category_id, reason, candidate_category_ids}`. Las capacidades `not_requested`/`not_configured` llevan únicamente `status`.
4. **Resolución** (`mode = resolution`): sea `m` la puntuación máxima **entre las elegibles**; las **candidatas** son las elegibles con `m − score ≤ tie_tolerance` (distancia respecto del **máximo**, sin empates transitivos encadenados); se selecciona la de **menor número de prioridad** (prioridades únicas ⇒ inequívoco). La prioridad no modifica puntuaciones ni el orden del ranking.
5. **Motivo de resolución** (`resolution.reason`, conjunto cerrado): `highest_score` (una sola candidata), `priority_tiebreak` (varias candidatas; decidió la prioridad), `no_eligible_category` (clasificación `completed` sin elegibles; `selected_category_id = null`, `candidate_category_ids = []`), `classification_inconclusive` (clasificación `inconclusive`; **nunca hay ganadora** aunque existan puntuaciones; `candidate_category_ids = []`).
6. **No confundir:** *ninguna elegible* (`completed`, `no_eligible_category`) ≠ *inconclusa* (`inconclusive`) ≠ *no solicitada* (`not_requested`) ≠ *no configurada* (`not_configured`). Son cuatro situaciones distintas y no se codifican con el mismo valor.
7. **Categorías no evaluadas:** una clasificación `completed` da cuenta de **todas** las categorías habilitadas de la versión (cada una exactamente una vez; sin ids desconocidos; `not_evaluated_ids = []`). En una `inconclusive`, las no evaluadas aparecen en `coverage.not_evaluated_ids` y **no** en `ranking`: nunca con puntuación cero inventada.
8. **Cobertura:** §6.5.

**Comparación numérica (E-A03-11).** Las comparaciones se hacen en aritmética **racional exacta** sobre la representación decimal más corta que recupera cada número: cada valor `x` se convierte con `Fraction(repr(x))` (un entero JSON como `1` es `Fraction(1)`; `Fraction` interpreta la cadena decimal sin pérdida, incluidas las formas con exponente) y se compara con `≥`/`≤` sin épsilon, sin tolerancias ocultas y **sin redondeo previo**: `Fraction(repr(score)) ≥ Fraction(repr(umbral))` para elegibilidad y `Fraction(repr(m)) − Fraction(repr(score)) ≤ Fraction(repr(tolerancia))` para candidatas. Se evita `decimal.Decimal` para la resta porque redondea a la precisión del contexto decimal (configurable por el host), lo que no garantiza exactitud universal; los racionales son exactos e independientes de cualquier contexto, y el motor **no** modifica el contexto decimal global. Motivo del enfoque decimal: la aritmética binaria daría `0.9 − 0.7 = 0.20000000000000007 > 0.2`, contradiciendo lo que cualquiera lee en el JSON. Casos de borde obligatorios: `score == umbral` (elegible), `m − score == tolerancia` (candidata, p. ej. `0.9`, `0.7`, `0.2`), un paso por encima (no candidata), `tolerancia = 0` (solo empate exacto), umbral `0` y `1`, puntuaciones `0` y `1`, números finitos extremos (`5e-324`, `1.1102230246251565e-16`, `0.9999999999999999`) y independencia frente al contexto decimal del host. Los valores se devuelven tal como los entregó el adaptador (sin redondeo); las puntuaciones públicas y los valores almacenados en A02 no cambian.

### 6.4 Detección y localizadores

```jsonc
"detection": {
  "status": "completed", "reason": null,
  "coverage": { "complete": true, "text_length": 87,
                "groups": [ { "ids": ["password", "personal_data"], "regions": [ { "start": 0, "end": 87 } ] } ],
                "not_evaluated_ids": [] },
  "findings": [
    { "type_id": "personal_data", "start": 12, "end": 23 },
    { "type_id": "personal_data", "start": 25, "end": 49 },
    { "type_id": "password",      "start": 69, "end": 86 }
  ]
}
```

(Texto del ejemplo, 87 puntos de código: `Contactar a Ana Ejemplo (ana.ejemplo@example.test). Mi contraseña es EjemploNoReal-123.`; `[12, 23)` = «Ana Ejemplo», `[25, 49)` = el correo, `[69, 86)` = «EjemploNoReal-123».)

- **Intervalo semiabierto** `[start, end)` en puntos de código del texto exacto; `0 ≤ start < end ≤ len(text)`.
- **Verificación:** el adaptador entrega además el fragmento (`text`); el núcleo exige `text == texto[start:end]`. **No hay búsqueda aproximada ni reparación de offsets** (E-A03-13): si el fragmento no coincide textualmente en esa posición, la salida entera se rechaza (`fragment_mismatch`), aunque exista una coincidencia cercana. La comparación verifica **correspondencia textual**, no la intención semántica: un desplazamiento hacia texto **distinto** falla; un desplazamiento hacia **otra ocurrencia idéntica** del mismo fragmento puede pasar la validación estructural. La calidad de localización semántica (cuál ocurrencia quiso señalar el modelo) pertenece a la evaluación real de A09.
- **Fragmento solo en la salida del adaptador**, no en el resultado público (E-A03-13): evita duplicar contenido sensible en respuestas, cachés y registros; el llamador ya posee el texto y puede cortarlo con los índices. Tampoco hay explicaciones libres ni razonamientos en el contrato.
- **Casos Unicode** (texto `😀 Ana Ejemplo\r\né ok`): `😀` es **1** punto de código (2 unidades UTF-16, 4 bytes); «Ana Ejemplo» está en `[2, 13)`; `\r\n` ocupa 2 índices; «é» descompuesta (`e` + U+0301) ocupa 2 y un hallazgo puede legalmente terminar entre ambos (la unidad es el punto de código, no el grafema); una secuencia con ZWJ (👨‍👩‍👧) son 5 puntos de código.
- **Repeticiones:** si «Ana Ejemplo» aparece en `[40, 51)` y `[53, 64)` de un texto, cada ocurrencia es un hallazgo con su propia ubicación; reportar la segunda ocurrencia con los índices de la primera **pasa** la validación estructural (ambos fragmentos son idénticos; es un error semántico que el motor no puede detectar), mientras que un índice que apunta a texto distinto (p. ej. `start = 41`) falla `fragment_mismatch`. No se deduplica por contenido ni se busca la «ocurrencia correcta».
- **Superposiciones:** hallazgos que se solapan, o de **tipos distintos sobre el mismo fragmento**, son válidos y se conservan todos. **Duplicado exacto** `(type_id, start, end)` ⇒ salida inválida (`duplicate_finding`): no se fusiona ni se descarta en silencio.
- **Orden estable del resultado:** `(start, end, type_id)` ascendente, independiente del orden del adaptador.
- **No se resuelve en A03** cómo fusionar o sustituir superposiciones al proteger (protección: feature posterior). **No hay** páginas, bounding boxes ni *runs*: el mapeo documental es de A04.
- Un hallazgo pertenece a una región cubierta **de su tipo** (§5). Con `inconclusive`, los hallazgos son «lo verificable encontrado en lo cubierto»; **no acreditan ausencia de otros** y, en particular, no hay hallazgos fuera de la cobertura declarada.

### 6.5 Cobertura, resultados parciales e inconclusos (D-A03-03)

`coverage` (público) lo **deriva el núcleo** de los grupos validados:

| Campo | Significado |
|---|---|
| `text_length` | longitud del texto (puntos de código) |
| `groups` | grupos `{ids, regions}` tal como se validaron (ids ascendentes; grupos ordenados por su primer id; regiones ascendentes) |
| `not_evaluated_ids` | ids configurados de **esa capacidad** que no figuran en ningún grupo (calculado por el núcleo) |
| `complete` | `true` ⇔ `not_evaluated_ids` está vacío **y** todo grupo cubre exactamente `[0, text_length)` |

- **Sin porcentajes agregados** (ocultarían huecos y tipos no evaluados). La cobertura se verifica con regiones e ids.
- **Cobertura completa** significa procesamiento completo del **alcance declarado** (todo el texto y todos los elementos configurados de la capacidad), **no** calidad ni exhaustividad de detección.
- `completed` exige `complete = true` (§5). `inconclusive` puede tener cobertura completa (abstención) o incompleta.
- **Motivos** (`reason`, conjunto cerrado, nunca texto libre): `partial_coverage` (procesamiento parcial; exige cobertura incompleta), `abstained` (el adaptador se abstiene de concluir), `truncated` (truncamiento semántico, p. ej. salida del modelo cortada con contenido verificable conservado).
- Una capacidad puede estar `completed` y la otra `inconclusive`.
- **Ausencia de hallazgos con cobertura incompleta no equivale a resultado negativo completo**; con `completed` equivale a «sin hallazgos dentro del alcance declarado», no a una garantía de ausencia.
- **Clasificación inconclusa:** se conservan las puntuaciones verificables (ranking de las evaluadas con su elegibilidad) pero `selected_category_id` es `null`.

**Ejemplo `inconclusive`** (mismo texto; el adaptador solo evaluó `finance` y declara cobertura parcial en detección):

```jsonc
"classification": {
  "status": "inconclusive", "reason": "partial_coverage",
  "scale": { "min": 0, "max": 1, "calibrated": false },
  "mode": "resolution", "eligibility_threshold": 0.5, "tie_tolerance": 0.05,
  "coverage": { "complete": false, "text_length": 87,
                "groups": [ { "ids": ["finance"], "regions": [ { "start": 0, "end": 87 } ] } ],
                "not_evaluated_ids": ["hr", "legal"] },
  "ranking": [ { "rank": 1, "category_id": "finance", "score": 0.82, "eligible": true } ],
  "resolution": { "selected_category_id": null, "reason": "classification_inconclusive", "candidate_category_ids": [] }
},
"detection": {
  "status": "inconclusive", "reason": "partial_coverage",
  "coverage": { "complete": false, "text_length": 87,
                "groups": [ { "ids": ["password"], "regions": [ { "start": 0, "end": 87 } ] },
                            { "ids": ["personal_data"], "regions": [ { "start": 0, "end": 50 } ] } ],
                "not_evaluated_ids": [] },
  "findings": [ { "type_id": "personal_data", "start": 12, "end": 23 },
                { "type_id": "personal_data", "start": 25, "end": 49 },
                { "type_id": "password", "start": 69, "end": 86 } ]
}
```

(El tipo `personal_data` se evaluó solo sobre `[0, 50)`; ninguno de sus hallazgos queda fuera. Aunque `finance` es elegible, no hay ganadora porque la clasificación es inconclusa.)

### 6.6 Estado global derivado

`status` del resultado se **deriva** (no lo declara el adaptador) y **no sustituye** los estados por capacidad: `completed` ⇔ todas las capacidades **ejecutadas** (pedidas y configuradas) están `completed`; `inconclusive` ⇔ al menos una ejecutada está `inconclusive`. `not_requested`/`not_configured` no influyen. Siempre hay al menos una ejecutada (§6.2).

### 6.7 Diferenciación de fallos (resumen; códigos en §8)

| Situación | Resultado |
|---|---|
| Respuesta **válida** con procesamiento parcial, abstención o truncamiento semántico | 200, capacidad `inconclusive`, resultados verificables conservados |
| JSON cortado, estructura inválida, ids desconocidos, offsets incorrectos, puntuaciones inválidas, contradicciones internas, límites de salida excedidos | error `inference_output_invalid` (502); **sin** resultado parcial |
| Timeout, adaptador apagado/cargando/inalcanzable, fallo recuperable de transporte, sin respuesta válida | error operativo (`inference_timeout` / `inference_unavailable`); **sin** resultado inventado |
| El mock no tiene fixture para la entrada | error `inference_fixture_not_found` (501); **nunca** respuesta vacía exitosa |

Los motivos son códigos acotados; ninguno incluye texto del contenido.

## 7. Adaptador mock determinista

Adaptador de **fixtures sintéticos**. No es un clasificador ni un detector: no hay heurísticas, expresiones regulares presentadas como detección general, clasificación por palabras clave ni inferencia ficticia.

### 7.1 Activación y procedencia

- Solo para **desarrollo y pruebas**; lo configura el **host/operador** al componer el motor (`InferenceSetup(MockInferenceAdapter(fixtures, acknowledge_simulated=True))`; el argumento es obligatorio y deja explícita la simulación). El servicio independiente lo habilita únicamente con configuración del operador (E-A03-12): `CAWBASE_CONTENT_ENGINE_INFERENCE_ADAPTER=mock` + `CAWBASE_CONTENT_ENGINE_INFERENCE_MOCK_FIXTURES=<ruta a JSON>` (valor por defecto `none`); al arrancar emite un aviso sin contenido. Un valor `mock` **no** se puede elegir por parámetro de solicitud pública, ni por cuerpo, cabecera ni consulta.
- **Sin fallback automático:** si falta un adaptador ⇒ `inference_not_configured`; si falla un adaptador real, el motor **no** cambia al mock (ni a nada). El mock solo existe si el host lo compuso.
- Toda respuesta **exitosa** lleva `provenance.kind = "mock"`, `simulated = true`, `model = null`.
- No habilita uso productivo ni afirma calidad: la validación con mock en A03 acredita **ingeniería del contrato**, no capacidad de inferencia (A09).

### 7.2 Fixtures

Un fixture asocia de forma verificable una entrada a un resultado crudo (o a un fallo):

```jsonc
{
  "fixture_id": "fx-contacto-completo",
  "match": {
    "text_sha256": "…",                  // SHA-256 del texto en UTF-8
    "context_sha256": null,              // o SHA-256 del contexto
    "capabilities": ["classification", "detection"],   // capacidades ejecutables enviadas
    "definitions_digest": "…"            // el InferenceRequest.definitions_digest (§4.1)
  },
  "outcome": { "kind": "output", "output": { /* RawInferenceOutput */ } }
}
```

- Coincidencia **exacta** de los cuatro componentes. Cambiar una definición, un ejemplo, un nombre o el conjunto de categorías/tipos cambia `definitions_digest` y el fixture **deja de coincidir** (no se reutiliza un fixture incompatible). Cambiar prioridades, umbral o tolerancia **no** afecta al fixture (el adaptador no los ve) y el núcleo recalcula ranking/resolución.
- **Sin coincidencia** ⇒ `inference_fixture_not_found`. Dos fixtures con la misma clave ⇒ error de configuración al cargar (`configuration_invalid`).
- **Extensible:** no se acopla a los seis tipos iniciales ni a categorías de ningún benchmark; los ids, nombres y definiciones salen de la versión fijada, de modo que sirve para categorías y tipos personalizados.
- `outcome.kind`: `output` (salida cruda a devolver; **puede ser inválida** para ejercitar el validador, y el núcleo la rechazará), `unavailable` (+ `reason ∈ {loading, stopped, unreachable, transient_failure}`), `hang` (no responde: provoca `inference_timeout`), `crash` (excepción inesperada ⇒ `internal_error`).
- Utilidades de autoría (públicas, puras): `build_inference_request(resolved, text=…, context=…, capabilities=…)` devuelve el `InferenceRequest` exacto que el motor enviaría (su clave fija el fixture); `MockFixture.for_request(fixture_id, request, outcome)` lo ata a un resultado; `locate(text, fragmento, occurrence=0)` y `full_coverage(ids, text)` (en `cawbase_content_engine.inference`) calculan offsets y cobertura desde el texto, de modo que las posiciones de los fixtures válidos están **verificadas por construcción**. Los fixtures se copian al cargarse y cada llamada devuelve una copia profunda del resultado.
- **Determinismo:** mismo fixture + misma entrada ⇒ mismo **resultado semántico**. Quedan fuera de esa garantía `request_id` y cualquier tiempo operativo. No es una promesa de determinismo de un modelo real.
- `configuration_id` = `fxs-` + 16 hex del SHA-256 del conjunto canónico de fixtures.
- **No** hay API pública (Python ni HTTP) para inyectar respuestas arbitrarias: los fixtures se cargan al componer el adaptador (código del host o archivo del operador).

### 7.3 Escenarios mínimos

| # | Escenario | Fixture / resultado esperado |
|---|---|---|
| M1 | Éxito (clasificación + detección completas) | 200 `completed` |
| M2 | Ausencia real de hallazgos dentro del fixture | detección `completed`, `findings = []`, cobertura completa |
| M3 | Superposición y mismo fragmento con tipos distintos | ambos conservados, orden `(start, end, type_id)` |
| M4 | Empate dentro de la tolerancia | candidatas múltiples, `priority_tiebreak` |
| M5 | Resolución por prioridad | prioridad menor gana aunque su puntuación sea algo menor |
| M6 | Ninguna categoría elegible | `completed`, `no_eligible_category`, sin ganadora |
| M7 | Clasificación inconclusa | `inconclusive`, `classification_inconclusive`, sin ganadora con puntuaciones presentes |
| M8 | Cobertura parcial / tipos no evaluados | `not_evaluated_ids`, `complete = false` |
| M9 | Una capacidad no configurada / otra no solicitada | estados `not_configured` / `not_requested` |
| M10 | Respuesta inválida (offsets, ids, puntuaciones…) | `inference_output_invalid` |
| M11 | Truncamiento semántico | `inconclusive`, `truncated` |
| M12 | Timeout (`hang`) | `inference_timeout` |
| M13 | Apagado / cargando (`unavailable`) | `inference_unavailable` (`stopped`, `loading`) |
| M14 | Fallo recuperable | `inference_unavailable` (`transient_failure`) |
| M15 | Sin fixture | `inference_fixture_not_found` |
| M16 | Fixture con categorías/tipos personalizados | éxito sin depender del catálogo inicial |

## 8. Errores, timeout y cancelación

### 8.1 Tabla de errores (se reutilizan jerarquía `EngineError` y envoltorio)

| Código | Excepción Python | HTTP | Reintentable | Cuándo | ¿Adaptador invocado? |
|---|---|---|---|---|---|
| `authentication_failed` | `AuthenticationError` | 401 | no | clave HTTP ausente/inválida | no |
| `resource_not_accessible` | `ResourceNotAccessibleError` | 404 | no | `scope_id` ajeno / clave sin grants en el scope | no |
| `permission_denied` | `PermissionDeniedError` | 403 | no | falta `analysis:execute` | no |
| `limit_exceeded` (HTTP 413) | `PayloadTooLargeError` | 413 | no | cuerpo > 64 KiB (mismo código que `limit_exceeded` 422; se distinguen por el estado HTTP) | no |
| `validation_failed` | `ValidationFailedError` | 422 | no | cuerpo/campos inválidos; `text`/`context` vacío o con sustitutos aislados; capacidades inválidas; `version_number` sin `profile_id` | no |
| `limit_exceeded` | `LimitExceededError` | 422 | no | texto/contexto sobre los límites de §2.6 | no |
| `transaction_incompatible` | `IncompatibleTransactionError` | — (Python) | no | `HostTransaction` | no |
| `inference_not_configured` | `InferenceNotConfiguredError` (nuevo) | 503 | no | el motor no tiene `InferenceSetup` | no |
| `scope_disabled` | `ScopeDisabledError` | 409 | no | scope deshabilitado | no |
| `default_profile_not_set` | `DefaultProfileNotSetError` | 404 | no | sin `profile_id` y sin predeterminado | no |
| `resource_not_found` | `ResourceNotFoundError` | 404 | no | perfil o versión inexistente | no |
| `profile_not_selectable` | `ProfileNotSelectableError` | 409 | no | perfil `disabled`/`deleted` (también con versión explícita) | no |
| `profile_not_published` | `ProfileNotPublishedError` | 409 | no | perfil sin versión vigente | no |
| `capabilities_not_configured` | `CapabilitiesNotConfiguredError` (nuevo, `ConflictError`) | 409 | no | ninguna capacidad pedida está configurada en la versión fijada | no |
| `inference_fixture_not_found` | `InferenceFixtureNotFoundError` (nuevo) | 501 | no | el **mock** no tiene fixture para la entrada | sí (el mock) |
| `inference_unavailable` | `InferenceUnavailableError` (nuevo; atributo `reason ∈ {loading, stopped, unreachable, transient_failure}`) | 503 | **sí** | adaptador apagado, cargando, inalcanzable o con fallo recuperable | sí |
| `inference_timeout` | `InferenceTimeoutError` (nuevo) | 504 | **sí** | el adaptador no respondió en `timeout_s` | sí |
| `inference_output_invalid` | `InferenceOutputInvalidError` (nuevo; `problems` solo con códigos) | 502 | no | salida del adaptador rechazada por §5 | sí |
| `database_unavailable` | `DatabaseUnavailableError` | 503 | sí | la lectura de preparación falló | no |
| `internal_error` | `InternalError` | 500 | no | defecto del motor o excepción inesperada del adaptador (se registra solo el nombre de la clase) | según el caso |

- **422 de entrada ≠ salida defectuosa del adaptador:** una entrada inválida del llamador es 422 y nunca invoca al adaptador; una salida inválida del adaptador es 502 y se atribuye al adaptador, no al llamador.
- «Reintentable» es una **indicación** (`retryable`): el motor **no reintenta automáticamente**, no espera indefinidamente a que el adaptador esté listo y **no enciende ni apaga infraestructura**. Repetir una solicitud la ejecuta de nuevo.
- El orden de las comprobaciones es el de §3.1 (autorización antes que validación, validación antes que acceso a datos, configuración de inferencia antes que preparación).
- Todos los mensajes son genéricos: nunca incluyen texto, contexto, fragmentos, definiciones, salidas crudas, ids provistos por el adaptador ni secretos.

### 8.2 `problems` de `inference_output_invalid`

Lista acotada (≤ 20) de objetos `{"code": …, "capability": "classification"|"detection"?, "index": "n"?}` con **solo** códigos del catálogo cerrado de §5 (más `adapter_diagnostic_invalid`, ver §8.4) y posiciones numéricas (≤ 6 dígitos); nunca contenido, fragmentos ni ids provistos por el adaptador.

### 8.4 Frontera de excepciones del adaptador (precisión de implementación I-A03-13)

El adaptador **no puede imponer** códigos, estados HTTP, mensajes ni detalles públicos. El protocolo de errores de inferencia es **cerrado** y consta solo de:

| Lo que `infer()` puede lanzar | Lo que el núcleo reconstruye |
|---|---|
| `InferenceUnavailableError(reason)` | `inference_unavailable` con mensaje genérico; `reason` solo si pertenece a `loading`, `stopped`, `unreachable`, `transient_failure` (otro valor ⇒ `unreachable`) |
| `InferenceTimeoutError` y `TimeoutError` | `inference_timeout` con mensaje genérico |
| `InferenceOutputInvalidError(problems)` | `inference_output_invalid` con mensaje genérico; `problems` solo si es una lista ≤ 20 de objetos con claves `code` (del catálogo cerrado), `capability` (`classification`/`detection`) e `index` (dígitos ASCII, ≤ 6); si algo no cumple, se sustituye **toda** la lista por `[{"code": "adapter_diagnostic_invalid"}]` |
| `InferenceFixtureNotFoundError` (error previsto del mock) | `inference_fixture_not_found` con mensaje genérico |
| `asyncio.CancelledError` | se propaga sin cambios |

Cualquier otra excepción —incluidos otros `EngineError` como `ValidationFailedError`, `PermissionDeniedError`, `LimitExceededError`, errores de base de datos o `InferenceNotConfiguredError`— es un **fallo inesperado del adaptador**: `internal_error` (500) con mensaje genérico, sin `field`, `problems` ni `request_id` del adaptador, y se registra únicamente el **nombre de la clase**. El error público se levanta fuera del manejador, de modo que no queda cadena de excepciones (`__cause__`/`__context__`) con texto del adaptador. La jerarquía de errores y el manejo HTTP de A01/A02 no cambian.

### 8.3 Timeout y cancelación (E-A03-15)

- **Timeout finito** configurable por el operador (`InferenceSetup.timeout_s`, predeterminado 30 s; 0 < t ≤ 600 s). Mide solo la invocación del adaptador (la lectura de preparación se rige por `statement_timeout_ms`). No hay parámetro por solicitud (ni en Python ni en HTTP: equivalencia).
- Al vencer, el núcleo cancela la tarea de `infer` y levanta `inference_timeout`; nunca devuelve éxito ni «sin hallazgos».
- **Cancelación cooperativa:** si el llamador cancela (`asyncio.CancelledError`; en HTTP, desconexión del cliente o cierre del servidor) la cancelación se propaga: el núcleo cancela la tarea del adaptador y **no** produce resultado. Una cancelación del llamador **nunca** se convierte en éxito, en «sin hallazgos» ni en un error de timeout.
- **Implementación:** el adaptador se invoca **en la tarea del llamador** bajo `asyncio.timeout`, de modo que la cancelación y el vencimiento le llegan por cancelación de su corrutina y no se crean tareas locales huérfanas con adaptadores cooperativos. Un `TimeoutError` levantado por el propio adaptador también produce `inference_timeout`. En HTTP, Starlette no cancela el manejador al desconectarse el cliente: la ruta ejecuta la operación como tarea y sondea `request.is_disconnected()` (cada 50 ms); si el cliente se fue, cancela la tarea, espera a que termine y responde con estado 499 sin cuerpo (nadie lo lee).
- **Límites de la garantía:** un adaptador arbitrario que **bloquea el bucle de eventos** o **ignora la cancelación** queda fuera de esta garantía (el timeout no podría ni dispararse).  el motor solo puede cancelar la espera local y, si el adaptador lo implementa, avisarle por cancelación de la corrutina. Un futuro servidor remoto puede seguir procesando trabajo ya iniciado; A03 no promete detenerlo ni su coste. El motor no mantiene transacciones abiertas, así que cancelar no deja estado en la base.

## 9. Contrato HTTP

`POST /v1/scopes/{scope_id}/analyze-text` — extra `http`, mismas operaciones del núcleo (D-07). La ruta se registra con el router de A01/A02 (`create_router(engine)`) siempre; sin inferencia configurada responde `503 inference_not_configured` tras autenticar. No se crea ruta bajo `/v1/admin`.

**Solicitud** (`Content-Type: application/json`, UTF-8; cuerpo ≤ 64 KiB; campos desconocidos ⇒ 422):

```json
{ "text": "Contactar a Ana Ejemplo (ana.ejemplo@example.test). Mi contraseña es EjemploNoReal-123.",
  "profile_id": "contratos", "version_number": 3,
  "capabilities": ["classification", "detection"], "context": null }
```

**Respuesta 200** (`application/json`): el `AnalysisResult` de §6.1. Estado **200 tanto para `completed` como para `inconclusive`**; el estado está en el cuerpo. **No** hay 202, `Location`, recurso persistente, `GET` de resultados ni idempotencia duradera.

**Errores:** envoltorio de A01 (`{"error": {"code", "message", "request_id", "problems"?}}`) con los códigos y estados de §8.1.

- **Orden y reglas actuales** (se conservan como en las rutas de escritura de A02): límite de cuerpo (413) → autenticación (401) → contexto de scope (404 sin grants, indistinguible de inexistente) → forma JSON (422) → operación (permiso 403, validación, etc.). Un `scope_id` ajeno es siempre 404; una clave sin `analysis:execute` en su propio scope es 403.
- **Caché y datos sensibles:** la respuesta contiene ubicaciones de hallazgos sensibles y es potencialmente sensible: lleva `Cache-Control: no-store` (solo esta ruta; no se cambian las demás). La solicitud contiene el texto: no se admite en query string ni cabeceras.
- **`request_id`** = `X-Request-ID` del servidor; sirve para correlación, **no** para deduplicación. Repetir la solicitud ejecuta de nuevo.
- **Observabilidad:** ver §10.3.
- Estados HTTP añadidos al catálogo general (A01 §9.4): `501`, `502`, `504` (solo esta ruta).

## 10. Persistencia, ciclo de vida y arquitectura

### 10.1 Sin persistencia (D-A03-01)

A03 **no almacena** texto, contexto, resultados ni solicitudes; no requiere tablas de análisis. **No hay migración**: `0002_profiles` sigue siendo la cabeza (`schema_level = 2`, `min_runtime_level = 1`); no se crea una migración vacía. El permiso nuevo es una cadena en `api_key_scope_grants.permission` (columna `TEXT` sin restricción de valores). No se escribe contenido ni resultados en `admin_events` (A03 no emite eventos de análisis; la trazabilidad técnica es el registro acotado de §10.3).

Implicación de niveles: ningún cambio de esquema ⇒ `SCHEMA_LEVEL`/`RUNTIME_LEVEL` no cambian por A03. La compatibilidad **operacional** con binarios anteriores no depende del nivel sino de los grants: ver [seguridad §10.3](security-and-isolation.md#103-compatibilidad-y-despliegue-del-permiso-nuevo).

### 10.2 Ciclo de vida del adaptador y arquitectura

- **Composición explícita:** `ContentEngine(config, inference=InferenceSetup(adapter))`; el servicio independiente según §7.1. Sin efectos al importar: `import cawbase_content_engine` **no** importa el módulo de inferencia, el validador ni el mock (carga perezosa; el gate de importación liviana D-33 se mantiene).
- **Propiedad:** el adaptador y sus recursos son del host; `ContentEngine.aclose()` no los cierra.
- **Sin estado compartido mutable** entre solicitudes en el motor; cada solicitud crea sus propias estructuras (`PreparedAnalysis`, `InferenceRequest`).
- **Núcleo sin FastAPI ni Alembic:** el módulo de análisis y el validador viven en el núcleo; solo la ruta HTTP usa el extra `http`. Sin dependencias pesadas ni extras vacíos por anticipación (no se agrega extra «inference»).
- El motor se usa para A01/A02 sin configurar inferencia.
- Módulos: `inference/` (`adapter.py` contrato y tipos, `prepared.py`, `output.py` validador, `build.py` ranking/resolución/cobertura, `results.py`, `service.py`, `mock.py`) (tipos y protocolo, validación, mock), `operations/selection.py` (selección compartida), `operations/analysis.py` (fachada de `analyze_text`).

### 10.3 Observabilidad y datos sensibles

- **Nunca** se registran: texto, contexto, fragmentos, definiciones, salidas crudas del adaptador, ids provistos por el adaptador, ni secretos; tampoco en errores, diagnósticos ni respuestas de error.
- Se pueden registrar (registro `cawbase_content_engine.analysis`, eventos `analysis_completed`/`analysis_failed`): `request_id`, código de error, estado global, estados por capacidad, conteos (hallazgos, categorías), longitudes, duración, `adapter_kind` y ids del scope/perfil/versión (ya admitidos en A02). Los registradores de terceros en `DEBUG` quedan fuera del control del motor (I-A02-07 de A02).
- El adaptador y el host son responsables de lo que ellos mismos registren.

## 11. Fuera de alcance y límites declarados

Modelo real, comparación o selección de modelos, proveedor, runtime, pesos, tokenizer, prompts concretos, ventana de contexto y protocolo remoto; segmentación y reconciliación específica de un modelo; PDF/DOCX, OCR y mapeo documental (A04); protección, redacción, seudonimización y restauración; trabajos, colas, almacenamiento de originales/resultados y retención (A05); AWS, encendido/apagado de infraestructura y credenciales de administración; integración con CAWbase; overrides de ranking, prioridades o definiciones por ejecución; calidad de inferencia real y uso productivo del mock (A09).

A03 contempla **errores abstractos de disponibilidad** (`inference_unavailable`, `inference_timeout`) y la **procedencia futura** del adaptador (`kind = model`, `model`), sin diseñar infraestructura ni seleccionar runtime. La calidad real se valida en A09, que **debe fijar los umbrales de precisión/recall antes de evaluar**; el cierre de A03 **no depende** de ellos. Ver las diferencias con el playbook en [A03-decisions §4](decisions/A03-decisions.md#4-diferencias-respecto-del-playbook-revisión-6).

## 12. A05 — Uso durable del contrato (implementado en 0.4.0; pendiente de aceptación)

> **IMPLEMENTADO en `0.4.0` (A05), pendiente de revisión y aceptación del usuario.** Evidencia por ambiente y trazabilidad de criterios en el [informe de A05](a05-implementation-report.md); **no aceptada**. La viabilidad documental se limita al subconjunto conservador de [§11.2](jobs-files-and-lifecycle.md#112-matriz-de-soporte-a05-solo-extracción-y-localización). Contrato: [trabajos, archivos y ciclo de vida §11](jobs-files-and-lifecycle.md#11-extracción-mapeo-y-análisis-en-a05). **Nada de §1–§11 cambia**: `analyze_text`, su firma, su ruta HTTP con respuesta en la misma petición, `AnalysisResult` (sin páginas ni nodos), los límites y la semántica de cobertura se preservan.

- A05 propone `text_analysis` y `document_analysis` como trabajos durables que **reutilizan** la preparación, la invocación (timeout y protocolo cerrado de errores), la validación y la construcción de resultados de A03 mediante una función interna común sin permisos; no copian su lógica.
- El camino durable parte de la **versión fijada** al aceptar (identidad + `content_hash`), no de la selección pública: desactivar o eliminar el perfil después no afecta al trabajo (D-A05-02). La selección pública sigue rechazando perfiles desactivados para análisis nuevos.
- Los resultados documentales **envuelven** un `AnalysisResult` por unidad, sin agregar puntuaciones ni elegir ganadora global; unidades vacías no invocan al adaptador; una salida inválida en cualquier unidad impide publicar.
