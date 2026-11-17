# Protección y restauración DOCX — contrato de A07

> **Estado: ESPECIFICADO (revisión 2), PENDIENTE DE LA APROBACIÓN EXPLÍCITA DEL USUARIO. No implementado.** Nada de lo que describe este documento existe en el código: ni API, ni operación de trabajo, ni permiso, ni tabla, ni migración, ni versión. Todo nombre, firma, código de error, tabla o comando que aparece aquí es **propuesto / no disponible** hasta que exista con código y pruebas. Las cinco decisiones de producto de A07 (`D-A07-01` a `05`) y las propuestas `P-A07-01` a `07` (con el texto que el usuario fijó al aprobarlas en la revisión de `55a39ed`) están **aprobadas por el usuario** y se registran en [A07-decisions](decisions/A07-decisions.md); las elecciones técnicas `E-A07-nn` no son decisiones de producto; las hipótesis `H-A07-nn` se verifican en el código. Cambios de la revisión 2: [A07-decisions §0](decisions/A07-decisions.md#0-historial-de-revisiones). Alcance y matriz de aceptación: [especificación de A07](features/A07-proteccion-y-restauracion-docx.md).

Relacionado: [procesamiento de documentos (A04)](document-processing.md) · [trabajos, archivos y ciclo de vida (A05)](jobs-files-and-lifecycle.md) · [protección y restauración de texto (A06)](text-protection.md) · [inferencia y análisis (A03)](inference-and-analysis.md) · [seguridad y aislamiento](security-and-isolation.md) · [persistencia y migraciones](persistence-and-migrations.md).

**Convención.** Cada regla indica su origen entre corchetes: **[D1]**–**[D5]** decisión aprobada (`D-A07-01` a `05`); **[E-nn]** elección técnica (`E-A07-nn`); **[P-nn]** propuesta **aprobada por el usuario** (`P-A07-nn`, revisión 2); **[H-nn]** hipótesis por verificar (`H-A07-nn`). Las referencias `A04 §n`, `A05 §n` y `A06 §n` apuntan a [document-processing.md](document-processing.md), [jobs-files-and-lifecycle.md](jobs-files-and-lifecycle.md) y [text-protection.md](text-protection.md).

## 0. Resumen

A07 añade, **solo por API Python** [D2], dos operaciones durables sobre el sistema de trabajos de A05:

1. **`docx_protection`**: toma un trabajo `document_analysis` de A05 **vigente, autorizado y concluyente** (detección con el mock de A03) [D1], aplica a cada hallazgo la política estricta de A06 (unión determinista, precedencia de la redacción, marcadores del contexto) [D1], elimina las clases `E` autorizadas por A04 [D5], verifica el resultado con un oráculo por posición y una inspección del paquete completo [D5] y publica un **DOCX protegido** con un **vínculo de restauración** persistido [D3].
2. **`docx_restoration`**: toma el **DOCX protegido exacto** emitido por el motor y su vínculo, comprueba que cada marcador esté en su posición autorizada, recupera los valores seudonimizados del contexto de A06 (vigente) y publica un **DOCX restaurado** [D3, D4].

```text
document_analysis (A05, mock A03)        contexto A06 (vigente, tipo fijo)
        │ resultado + SourceMap + original          │ correspondencias cifradas
        ▼                                           ▼
submit_docx_protection ──► worker: vincular ─► planificar ─► marcadores (A06) ─► transformar ─► verificar ─► publicar
                                                                                         └─► DOCX protegido + vínculo (derivative_id)
submit_docx_restoration(derivative_id, file_id = copia exacta) ──► worker: digest ─► posiciones ─► descifrar ─► restaurar ─► verificar ─► publicar
                                                                                         └─► DOCX restaurado
```

Lo que **no** hace: no acepta hallazgos del llamador, no procesa PDF (A08), no usa inferencia real ni mide calidad (A09), no ofrece HTTP ni rutas nuevas, no restaura lo eliminado irreversiblemente, no promete identidad visual ni byte a byte con el original, no promete anonimización.

## 1. Fronteras, confianza y principios

| Frontera | Regla |
|---|---|
| Host → motor | Igual que A01 §2.1 y A06 §1: el host construye el `ScopeContext` tras autorizar; además **comprueba el acceso a la conversación o trabajo concreto** al que pertenece un contexto **antes** de delegar (A06 P-A06-06). Los permisos del motor tienen alcance de scope |
| Motor → documento | El DOCX es **entrada no confiable**: se valida, transforma y verifica en el **proceso aislado** de A05 §10.5 (mismo perfil exigido y canario), con los límites de A05 §12 |
| Motor → LLM | Ninguno: A07 no llama a la inferencia. Los hallazgos vienen del análisis de A05 (mock en A01–A08) |
| Motor → base | Guarda metadatos, posiciones y marcadores del derivado; **nunca** valores originales en claro (las correspondencias son las de A06, cifradas) |

Principios:

- **Fallo cerrado y sin parciales.** Cualquier selección no transformable, residuo, discrepancia del oráculo, clave ausente, registro alterado o contexto no vigente produce un fallo **sin publicar** ningún artefacto ni vínculo [D1, D3, D5].
- **Decidir en el punto de decisión.** Identidad, posesión, vigencia (contexto, análisis, entrada) y estado del scope se comprueban **en la transacción que publica**, con la hora de la base leída **una sola vez, después de adquirir todos los bloqueos** (A05 §6.1, I-A05-13; A06 §11.1) [E-01].
- **Reutilizar sin duplicar.** Fases, lease, *fencing*, reintentos, idempotencia, verificación de storage, cuotas, fence del scope y limpieza son los de A05; unión, política, marcadores, contextos y criptografía son los de A06 (mismas funciones, no copias) [D2, D3].
- **Sin efectos al importar** (D-33 intacta): nada de A07 se importa con el paquete base.
- **Contenido sensible** (original, valores, DOCX restaurado, posiciones de hallazgos) nunca en logs, errores, eventos ni diagnósticos.

## 2. Glosario

| Término | Definición |
|---|---|
| **Análisis fuente** | Trabajo `document_analysis` de A05 `succeeded` cuyo resultado y `SourceMap` alimentan la protección (§3) |
| **Unidad** | `ExtractionUnit` DOCX de A05 (un párrafo: cuerpo, celda, encabezado/pie, nota) con texto exacto y segmentos |
| **Pieza** | `PieceRef` DOCX de A04 §4: `part_name`, `node_path`, `char_start`, `char_end` de un nodo `w:t` (o `w:tab`/`w:br`/`w:cr`/`w:noBreakHyphen`) |
| **Grupo** | Unión de hallazgos solapados de **una** unidad (A06 §5.2); unidad de sustitución |
| **Selección** | Un grupo con su acción (`pseudonymize` o `redact`) y sus piezas |
| **Sustituto** | Marcador `[[CCE1:…]]` de A06 §6.1 (seudonimización) o `[REDACTED]` (redacción) |
| **Derivado protegido** | DOCX emitido por `docx_protection`, identificado por `derivative_id` y su SHA-256 |
| **Vínculo** | Registro persistido que liga el derivado (id, digest, tamaño) con scope, contexto, tipo y las **ocurrencias** de marcadores emitidas (§12.1) |
| **Ocurrencia** | Posición de un marcador en el derivado: unidad, `[start,end)` en puntos de código, parte, nodo y offsets dentro del nodo |
| **Oráculo por posición** | Texto esperado de cada unidad del derivado, calculado de forma pura a partir del texto de la unidad original y de las selecciones (§6.2) |

## 3. Fuente de hallazgos y vinculación [D1]

### 3.1 Entrada

La protección se pide con el `job_id` de un análisis fuente. **No** existe un parámetro para aportar hallazgos, offsets, `AnalysisResult` ni `SourceMap` del llamador [D1, E-02]. El original es la entrada fijada del análisis fuente (`input_file_id`): el llamador no lo elige por separado, lo que impide combinar un análisis con otro archivo.

### 3.2 Condiciones de la fuente

Todas se exigen; cualquier fallo ⇒ error sin efectos (en el envío) o fallo no reintentable del trabajo (en el intento), sin publicar nada.

| # | Condición | Cuándo se comprueba | Error |
|---|---|---|---|
| S1 | El trabajo existe en **este scope**, `operation = document_analysis` | envío | `resource_not_found` / `analysis_source_invalid` |
| S2 | `state = succeeded`, `data_state = available`, `result_expires_at > ahora` | envío, intento y **publicación** | `job_not_succeeded` / `job_data_expired` (envío); `analysis_unavailable` (trabajo) |
| S3 | `result_expires_at ≥ deadline_at` del trabajo nuevo | envío | `analysis_expiring` |
| S4 | El original (`input_file_id` del análisis) es `stored`, no vencido, con la validación fijada `passed`, y `expires_at ≥ deadline_at` | envío, reclamación y publicación (A05 §6.5 punto 4) | `file_not_usable` / `file_expiring` (envío); `input_unavailable` (trabajo) |
| S5 | Resultado y mapa leídos íntegros (SHA-256 verificado por A05 §4.2) y sus `job_id` coinciden | intento | `stored_object_unavailable` / `analysis_source_invalid` |
| S6 | **Identidad del original**: `SHA-256` y tamaño de los bytes leídos = `result.document.sha256/size_bytes` = `source_map.identity.file_sha256/size_bytes` = entrada fijada del análisis; `format = docx` | intento | `analysis_source_invalid` |
| S7 | **Extractor**: `source_map.identity.extractor` y `result.provenance.extractor` = `{id, version, config_sha256}` del extractor que ejecuta el worker (A05 §8.5) | intento | `analysis_source_stale` |
| S8 | **Mapa exacto**: la reextracción aislada del original con ese extractor produce un `SourceMap` cuyo JSON canónico es **idéntico byte a byte** al artefacto publicado, y cada `units[i].text_length` del resultado = longitud del texto reextraído | intento | `analysis_source_stale` |
| S9 | **Detección concluyente**: `per_capability.detection.status = completed`, `complete = true`; cada unidad analizada con `detection.status = completed` y `coverage.complete = true`; ninguna unidad `inconclusive` | intento | `detection_inconclusive` / `capabilities_not_configured` |
| S10 | **Perfil identificable**: `result.profile` (scope, perfil, versión, `content_hash`, `selection`) = identidad fijada del análisis (columnas de `jobs`) | intento | `analysis_source_invalid` |
| S10b | **Procedencia simulada del mock de A03** [E-22] (A01–A08 solo aceptan el mock): `result.provenance.inference` = `inference_provenance` del `execution_snapshot` fijado del análisis (igualdad campo a campo) **y** cumple `kind = "mock"`, `simulated = true`, `adapter_id = "cce-mock-fixtures"`, `adapter_version = "1"`, `configuration_id` con forma `fxs-` + 16 hex, `model = null`, `inference_contract_version = 1`; además, en cada unidad analizada, `analysis.provenance` es igual a esa procedencia | intento | `analysis_provenance_unsupported` |
| S11 | Cada hallazgo cumple `0 ≤ start < end ≤ len(unit.text)` sobre el texto reextraído, con `type_id` válido | intento | `analysis_source_invalid` |
| S12 | El llamador tiene `results:read` además de `docx:protect` (§9) | envío | `permission_denied` |

- **Por qué la reextracción (S8).** El resultado y el mapa de A05 **no contienen el texto** de las unidades (solo `text_length` y posiciones): la única forma de aplicar los offsets al **texto exacto** es reextraer el original exacto con el mismo extractor y configuración y demostrar que el mapa resultante es el publicado. Una configuración de extracción distinta (p. ej. otros límites de A05 §12.1) o una versión distinta del extractor vuelven **desactualizado** el análisis: se rechaza, no se recalcula [D1, E-03].
- **Por qué S10b y no la mera presencia de procedencia**. A05 guarda con igual forma el análisis de un adaptador de modelo (`kind = "model"`, `simulated = false`) y el del mock; la presencia de una procedencia no distingue uno de otro. Hasta A09 el contrato de A07 solo se acredita con el mock: un análisis cuya procedencia no sea exactamente la simulada del mock de A03, o cuya procedencia por unidad o del manifiesto difiera de la fijada en el trabajo, se rechaza con `analysis_provenance_unsupported`, sin crear correspondencias ni artefactos. A09 revisará esta condición al incorporar el modelo real.
- **Qué demuestra y qué no.** Demuestra que los hallazgos pertenecen al texto exacto del original exacto y que el análisis vigente y autorizado se hizo con un perfil identificable. **No** demuestra calidad de detección (A09) ni qué ocurrencia idéntica habría señalado un modelo real (A06 §5.6).
- Los hallazgos se resuelven **por unidad** y offsets `[start,end)` en **puntos de código** del texto de la unidad [D1]; nunca por búsqueda de texto.

### 3.3 Política, unión y precedencia (reutilización de A06)

- `policy: ProtectionPolicy` (A06 §3.1): `default_action` obligatoria y `actions` por `type_id`; solo `pseudonymize` y `redact`; sin «dejar pasar».
- Por unidad: unión determinista de A06 §5.2 (orden, solapamiento estricto, cierre transitivo, adyacentes separados) y precedencia de la redacción de A06 §5.3 (un miembro `redact` ⇒ el grupo entero se redacta, sin correspondencia). **Mismas funciones puras** de `protection/grouping.py` [D1, E-04].
- Las unidades no se unen entre sí (un grupo nunca cruza unidades, porque los hallazgos de A03 son por unidad).
- Seudonimizar un grupo usa el **fragmento exacto** `unit.text[g_start:g_end]` como valor de la correspondencia de A06 (mismo fragmento ⇒ mismo marcador en el contexto, verificado según A06 §9.3.1).
- **Colisión** (A06 §5.7): si el texto de **cualquier** unidad contiene un marcador estricto o una alteración reconocible (clases S, R1, R2 de A06 §6.2) ⇒ `marker_collision`, antes de crear correspondencias.

## 4. Matriz de transformación DOCX [D4, D5]

### 4.1 Precondiciones del documento

El original debe estar admitido por la matriz de extracción de A05 (A05 §11.2): no cifrado, sin firmas, sin macros, sin revisiones, sin texto oculto, sin cuadros de texto, sin objetos incrustados ni relaciones externas distintas de hipervínculos, **una sola sección**, anidación de tablas ≤ 2, sin estructuras desconocidas. **A07 no amplía** esa matriz: todo `R` de A05 sigue siendo `R` y el trabajo de análisis ya habría fallado [D5]. A07 añade estas precondiciones propias de la transformación (comprobadas en el proceso aislado):

| ID | Precondición | Error |
|---|---|---|
| PC-01 | Las partes de comentarios presentes son exactamente del conjunto conocido (`word/comments.xml`, `commentsExtended.xml`, `commentsIds.xml`, `commentsExtensible.xml`, `people.xml` y sus `.rels`) y cada anclaje (`w:commentRangeStart/End`, `w:commentReference`) referencia un comentario existente | `unsupported_structure` |
| PC-02 | Las relaciones de las partes eliminables solo apuntan a partes que, tras eliminarlas, quedan **sin otra referencia** o siguen referenciadas por partes conservadas (no se elimina nada que otra parte use) | `unsupported_structure` |
| PC-03 | `docProps/app.xml` y `docProps/core.xml`, si existen, son XML válidos con los espacios de nombres estándar | `unsupported_structure` |
| PC-04 | Ninguna unidad contiene S/R1/R2 (§3.3) | `marker_collision` |

### 4.2 Matriz

Columnas: **TX** (fila de A07) · **DX** (fila de A04 §11) · **A05** (disposición en A05 §11.2) · **A07** (tratamiento) · **Precondición de transformación** · **Fallo** (código del trabajo; §8.5). «Selección» = un grupo de §3.3. «Contenedor admitido» = §4.3.

| TX | DX | A05 | A07 | Precondición de transformación | Fallo |
|---|---|---|---|---|---|
| TX-01 Párrafo, valor en un run | DX-01 | S | **Transforma** | piezas `w:t` de un solo contenedor admitido | — |
| TX-02 Valor repartido entre runs (formato mixto) | DX-01 | S | **Transforma**; el sustituto adopta el formato del **primer run afectado**; runs intermedios vaciados; sufijo del último run conservado | entre las piezas solo nodos permitidos (§4.3 R3) | `selection_not_transformable` |
| TX-03 Varias selecciones en un párrafo, un run o un mismo `w:t` | DX-01 | S | **Transforma** de derecha a izquierda por nodo | selecciones disjuntas (garantizado por la unión) | — |
| TX-04 Unicode (no BMP, NFD, ZWJ, espacios iniciales/finales) | DX-01 | S | **Transforma** en puntos de código; `xml:space="preserve"` cuando el resultado empieza o termina en espacio | — | — |
| TX-05 Tablas, celdas, tablas anidadas hasta profundidad 2 | DX-02, DX-03 | S (≤ 2) | **Transforma** el texto de la celda; rejilla (`w:tblGrid`), `w:tcPr` y anidación intactas | la selección está en un párrafo de una celda | — |
| TX-06 Celdas combinadas `gridSpan`/`vMerge` | DX-04 | S | **Transforma** en la celda que contiene el texto (las de continuación no tienen texto); combinación intacta | — | — |
| TX-07 Encabezados y pies `default`/`first`/`even` de la única sección | DX-05 | S | **Transforma** en cada parte; referencias, `w:titlePg` y `w:evenAndOddHeaders` intactos | — | — |
| TX-08 Notas al pie y al final (sin separadores con texto) | DX-06 | S | **Transforma** el texto de la nota; `w:footnoteReference`/`w:endnoteReference` y numeración intactos | la selección no contiene el run de referencia (§4.3 R2) | `selection_not_transformable` |
| TX-09 Texto visible de hipervínculo | DX-07 | S | **Transforma** si la selección está **enteramente dentro** del texto visible de **un** `w:hyperlink`; destino y `r:id`/`w:anchor` intactos | contenedor = ese `w:hyperlink` | `selection_crosses_boundary` |
| TX-10 Destino de hipervínculo | DX-07 | N | **Conserva**; **inspeccionado** en residuos (§6.3) | — | residuo ⇒ `residue_detected` |
| TX-11 Resultado de campo simple (`w:fldSimple`) | DX-08 | S (extracción) | **No transforma** (§4.4) | — | `field_result_not_transformable` |
| TX-12 Resultado de campo complejo (`w:fldChar` begin/separate/end, incluido `HYPERLINK`) | DX-08 | S (extracción) | **No transforma** (§4.4), también si el campo cruza párrafos o está anidado | — | `field_result_not_transformable` |
| TX-13 Instrucciones de campo | DX-09 | N | **Conserva**; inspeccionadas en residuos | — | residuo ⇒ `residue_detected` |
| TX-14 Control de contenido de texto simple (`w:sdt` sin tipo o con `w:text`) | DX-14 | S | **Transforma** si la selección está enteramente dentro de un `w:sdtContent`; `w:sdtPr` intacto | sin `w:showingPlcHdr`, sin `w:lock` de contenido (`sdtContentLocked`, `contentLocked`) | `selection_crosses_boundary` / `selection_not_transformable` |
| TX-15 Otros controles admitidos por A05 (`w:richText`, casilla, cita, grupo, etc.) | DX-14 | S | **No transforma** una selección dentro de ellos | — | `selection_not_transformable` |
| TX-16 Comentarios, hilos, personas, anclajes | DX-10 | E (no analizado) | **Elimina** (§5) | PC-01, PC-02 | `unsupported_structure` |
| TX-17 Propiedades del documento descriptivas/personales y personalizadas | DX-15 | E (no analizado) | **Elimina/vacía** (§5); fechas y estadísticas conservadas | PC-03 | `unsupported_structure` |
| TX-18 Miniatura | DX-18 | E (no analizado) | **Elimina** parte, relación y tipo de contenido | — | — |
| TX-19 Partes estructurales (`styles`, `numbering`, `settings`, `fontTable`, `theme`, `webSettings`) | DX-16 | P | **Conserva byte a byte**; inspeccionadas en residuos | — | residuo ⇒ `residue_detected` |
| TX-20 Imágenes | DX-19 | N | **Conserva byte a byte**; bytes inspeccionados (§6.3); texto en píxeles **fuera de cobertura** | — | residuo ⇒ `residue_detected` |
| TX-21 Texto alternativo (`wp:docPr/@descr/@title`) | DX-20 | N | **Conserva**; inspeccionado en residuos | — | residuo ⇒ `residue_detected` |
| TX-22 Ecuaciones (`m:oMath`) | DX-23 | N | **Conserva**; texto `m:t` inspeccionado en residuos; una selección no puede tocarlas (no son unidad) | — | residuo ⇒ `residue_detected` |
| TX-23 Fuentes incrustadas | DX-21 | (no listada en A05 §11.2) | Lo que A05 admita se **conserva byte a byte**; bytes inspeccionados; contenido ofuscado **fuera de cobertura** | — | — |
| TX-24 Marcadores de posición (`w:bookmarkStart/End`), permisos (`w:permStart/End`) | — | P (no inventariado) | **Conserva**; una selección que **contenga** un extremo estrictamente dentro de ella cambia el rango con semántica de enlace | ningún extremo dentro de la selección | `selection_crosses_boundary` |
| TX-25 Contenedores en línea (`w:smartTag`, `w:customXml` en línea, `w:ruby`, `w:dir`, `w:bdo`) | — | S (texto extraído) | **No transforma** una selección dentro de ellos | — | `selection_not_transformable` |
| TX-26 Contenido de run no textual dentro de la selección (`w:tab`, `w:br`, `w:cr`, `w:noBreakHyphen`, `w:sym`, `w:softHyphen`, `w:ptab`, `w:drawing`, `w:footnoteReference`, `w:endnoteReference`, `w:fldChar`, `w:instrText`, `w:object`, etc.) | DX-01 | S / N | **No transforma** (salto o elemento estructural cuya edición no se justifica) | §4.3 R2 | `selection_not_transformable` |
| TX-27 Revisiones, texto oculto, cuadros de texto, `customXml`, glosario, gráficos, SmartArt, OLE/ActiveX/macros, `altChunk`, referencias externas, cifrado, firmas, `docVars`, `dataBinding`, varias secciones, anidación > 2, rejilla inconsistente, separadores con texto, partes huérfanas, estructura desconocida | DX-11 a 13, 17, 22, 24 a 27 | R | **Rechazo heredado**: el análisis fuente no existe (A05 rechazó el archivo) | — | — |

### 4.3 Reglas de selección [D4, E-05]

Una selección es **transformable** si y solo si cumple todas:

- **R1 — Piezas textuales.** Todas sus piezas son nodos `w:t` (ningún `w:tab`, `w:br`, `w:cr`, `w:noBreakHyphen`: TX-26) y ninguna es sintetizada.
- **R2 — Runs.** Cada `w:t` está en un `w:r` cuyos hijos, además de `w:rPr` y de los `w:t`, solo pueden ser `w:lastRenderedPageBreak` (pista de render, se conserva). Un run con cualquier otro hijo (TX-26) en cualquier posición entre la primera y la última pieza hace la selección no transformable.
- **R3 — Contigüidad.** Entre el run de la primera pieza y el de la última, en orden de documento, solo puede haber: otros runs que cumplan R2, `w:proofErr` (se conserva) y anclajes de comentario (`w:commentRangeStart/End`, que se eliminan por §5). Cualquier otro nodo —en especial `w:bookmarkStart/End` y `w:permStart/End` (TX-24)— ⇒ `selection_crosses_boundary`.
- **R4 — Un contenedor.** Todas las piezas tienen la **misma cadena de contenedores** entre el `w:p` y el `w:r`, y esa cadena está vacía o es exactamente un `w:hyperlink` (TX-09) o un `w:sdt`/`w:sdtContent` de texto simple (TX-14). Una selección que empieza fuera y termina dentro (o al revés) de un hipervínculo o control ⇒ `selection_crosses_boundary`; cualquier otro contenedor (TX-25, `w:fldSimple`) ⇒ `selection_not_transformable` o `field_result_not_transformable`.
- **R5 — Campos.** Ninguna pieza está en un resultado de campo (§4.4).

Una sola selección no transformable hace fallar **todo** el trabajo (`selection_not_transformable`, `selection_crosses_boundary` o `field_result_not_transformable`); no se publica un documento parcialmente protegido ni se degrada a otra acción [D1, D4].

### 4.4 Resultados de campos [D1, D4, E-06]

El extractor de A05 (`documents/docx.py`, `add_unit`) recorre `p.iter()` y mapea **todo** `w:t` del párrafo, también los de resultados de campo, sin marcarlos. A07 **no cambia** el extractor ni el `SourceMap` (cambiaría la identidad de los análisis existentes, S7–S8); hace la distinción en su propio planificador, sobre el XML del original y por `node_path`:

- **Campo simple:** todo `w:t` descendiente de un `w:fldSimple` es resultado de campo.
- **Campo complejo:** en el orden de documento **de cada parte** (no por párrafo: un campo puede cruzar párrafos), una pila de campos: `w:fldChar w:fldCharType="begin"` apila; `separate` marca el campo superior como «en resultado»; `end` desapila. Todo `w:t` encontrado con la pila no vacía es contenido de campo (resultado si el campo superior está en resultado; en otro caso, contenido inválido de instrucción): ambos ⇒ **no transformable**. Anidamiento admitido; una pila desequilibrada al final de la parte ⇒ `unsupported_structure`.
- Una selección con **al menos una pieza** en contenido de campo ⇒ `field_result_not_transformable` (también si la selección cruza desde texto normal hacia el resultado o lo abarca entero). Sustituir el resultado visible no protegería un valor que la instrucción pueda regenerar (E-A04-27).
- Las **instrucciones** (`w:instrText`, `@w:instr`) siguen siendo `N`: se conservan y se inspeccionan en residuos (TX-13). Los campos con referencia externa siguen rechazados por A05 (`external_reference`).

### 4.5 Aplicación de la sustitución [D4, E-07]

Por selección, con piezas `p₁ … pₙ` en orden de documento (offsets de cada pieza relativos a su nodo):

1. En `p₁`: el texto del nodo pasa a ser `prefijo(p₁) + sustituto + (sufijo(p₁) si n = 1)`. El sustituto queda **íntegro en un único `w:t`**, dentro del **primer run afectado**, y adopta su formato (`w:rPr`).
2. En `p₂ … pₙ₋₁`: se elimina el nodo `w:t`; si su run queda sin `w:t`, sin `w:lastRenderedPageBreak` y solo con `w:rPr`, se elimina el run.
3. En `pₙ` (si n > 1): el texto del nodo pasa a ser `sufijo(pₙ)`; si queda vacío se aplica el paso 2.
4. Todo `w:t` modificado cuyo texto empieza o termina en espacio en blanco lleva `xml:space="preserve"`; no se cambia ningún otro atributo.
5. Las selecciones de una misma parte se aplican de la última a la primera, para que los offsets no se desplacen.
6. Nada fuera de las piezas de las selecciones cambia: texto, `w:pPr`, `w:rPr` de otros runs, tablas, secciones, referencias, numeración y partes no editadas se conservan **byte a byte** (§6.4). Se admite reflujo de líneas y páginas (A04 D-A04-03).

La garantía sobre el **formato del valor**: el sustituto y, al restaurar, el valor completo llevan el formato del primer run afectado; la distribución de formato mixto **dentro** del valor original **no** se conserva ni se restituye [D4].

## 5. Limpieza obligatoria de clases `E` y conservación [D5]

Toda protección elimina las clases `E` de A04 §13 presentes y registra las **efectivamente eliminadas** (clase y cantidad, nunca contenido) en el informe (`authorized_removals_performed`). Ninguna otra cosa se elimina: la autorización no se extiende por analogía (A04 §13).

| Clase registrada | Qué se elimina | Qué se conserva |
|---|---|---|
| `comments` | `word/comments.xml`, `commentsExtended.xml`, `commentsIds.xml`, `commentsExtensible.xml` y sus `.rels`; en **todas** las partes con texto (documento, encabezados, pies, notas): `w:commentRangeStart`, `w:commentRangeEnd` y `w:commentReference` (y su run si queda vacío); relaciones y `Override` de tipo de contenido correspondientes; partes alcanzables **solo** desde comentarios (p. ej. imágenes de un comentario) | el **texto anotado**, sus runs y su formato |
| `comment_people` | `word/people.xml`, su relación y su tipo de contenido | — |
| `core_descriptive_properties` | en `docProps/core.xml`, todo elemento fuera de la lista de conservados: `dc:title`, `dc:subject`, `dc:creator`, `cp:keywords`, `dc:description`, `cp:lastModifiedBy`, `cp:category`, `cp:contentStatus`, `dc:identifier` y cualquier otro | `dcterms:created`, `dcterms:modified`, `cp:lastPrinted`, `cp:revision`, `dc:language` (fechas y datos estructurales que A04 conserva) |
| `app_descriptive_properties` | en `docProps/app.xml`, todo elemento fuera de la lista de conservados: `Company`, `Manager`, `Template`, `HyperlinkBase`, `TitlesOfParts`, `HeadingPairs`, `HLinks` y cualquier otro | `Application`, `AppVersion`, `DocSecurity`, `ScaleCrop`, `LinksUpToDate`, `SharedDoc`, `HyperlinksChanged`, `TotalTime`, `Pages`, `Words`, `Characters`, `CharactersWithSpaces`, `Lines`, `Paragraphs` (las estadísticas pueden quedar desactualizadas tras la sustitución; no se recalculan) |
| `custom_properties` | `docProps/custom.xml`, su relación y su tipo de contenido | — |
| `thumbnail` | `docProps/thumbnail.*`, su relación y su `Override` (un `Default` por extensión solo si ninguna otra parte lo usa) | — |

- **Coherencia del paquete** tras eliminar: ninguna relación apunta a una parte inexistente; ninguna parte queda sin ser alcanzable desde `_rels/.rels`; `[Content_Types].xml` no tiene `Override` de partes eliminadas; `w:id` de comentarios no quedan referenciados.
- **Conservado** (no se elimina ni se modifica): todo `P` (TX-19, TX-24), todo `N` admitido (TX-10, 13, 20, 21, 22, 23) con **cobertura parcial explícita** en el informe, el texto anotado y las fechas.
- **Tras restaurar**, las clases `E` siguen ausentes: la restauración **no** recupera nada eliminado (no hay correspondencia para ello) [D3, D5].

## 6. Verificación antes de publicar [D5, E-08]

La verificación corre en un proceso aislado **distinto** del que transforma, con un **analizador XML distinto** (biblioteca estándar `zipfile` + `xml.etree.ElementTree` y `xml.parsers.expat` sin DTD ni entidades, frente a `lxml` del transformador) y recibe por *pipe* el original, el derivado, las selecciones (con sus valores) y el oráculo. Devuelve un informe **sin valores**. Cualquier comprobación fallida ⇒ fallo del trabajo sin publicar (`derivative_verification_failed` o `residue_detected`), sin reintento (determinista).

### 6.1 Validación estructural del paquete

V1: ZIP legible sin errores; sin entradas duplicadas ni nombres inválidos (A05 §10.1). V2: toda parte XML se analiza sin `DOCTYPE`, sin entidades y sin errores. V3: `[Content_Types].xml` cubre todas las partes y no menciona eliminadas. V4: cada relación interna apunta a una parte existente; cada parte es alcanzable. V5: el derivado vuelve a pasar la **validación y la extracción de A05** (mismo extractor) sin rechazo. V6: las clases `E` registradas están ausentes (partes, relaciones, tipos de contenido, anclajes, elementos de propiedades).

**Validación estructural ≠ apertura real.** V1–V6 son la **validación estructural del paquete** que el motor ejecuta en cada trabajo antes de publicar; **no** acreditan que un procesador de textos abra el archivo sin reparación. La **apertura sin reparación** y la **inspección visual** del DOCX protegido y restaurado se acreditan como **evidencia de aceptación** (no en tiempo de ejecución) con **LibreOffice Writer en Linux** (Docker), registrando versión, plataforma y fuentes en el momento de ejecutar las pruebas (§15). **Microsoft Word queda pendiente** como lector: no está disponible y su apertura no se afirma.

### 6.2 Oráculo por posición (texto de las unidades)

- **Oráculo:** para cada unidad, `oracle(u) = texto_original(u)` con cada selección `[g_start,g_end)` reemplazada por su sustituto, calculado de forma **pura** en el proceso padre a partir del texto reextraído (S8) y de las selecciones.
- **Comprobación O1:** la extracción independiente (verificador) **y** la del extractor de A05 sobre el derivado producen exactamente las mismas unidades, en el mismo orden, con `text = oracle(u)`. Toda diferencia (texto no seleccionado perdido o alterado, sustituto ausente o desplazado, unidad nueva o perdida) ⇒ `derivative_verification_failed`.
- **Ocurrencia idéntica no seleccionada** [D5]: si el valor de una selección aparece en otra posición de una unidad que **no** está seleccionada, el oráculo la contiene en esa posición y O1 exige que siga allí. Es una **ocurrencia permitida** (§6.3.1): se conserva y se comprueba **por posición**; la excepción cubre **solo** esa coincidencia en esa ubicación exacta del XML.
- **Comprobación O2 (marcadores):** las coincidencias de la clase S de A06 en las unidades del derivado son exactamente los sustitutos de seudonimización emitidos, en las posiciones del oráculo; no hay R1 ni R2. Las ocurrencias del vínculo (§12.1) se calculan con el **extractor de A05** sobre el derivado (el mismo que usará la restauración).

### 6.3 Residuos en el contenido inspeccionable

Los **valores seleccionados** son los fragmentos de **todas** las selecciones (seudonimizadas y redactadas). Se busca cada valor en estas **representaciones**: (a) exacta; (b) NFC; (c) NFD; (d) plegado de mayúsculas (`casefold`) de (a)–(c); (e) con los blancos colapsados a un espacio. Se descartan valores vacíos tras normalizar.

**Todo** el paquete se escanea; ninguna región se excluye en bloque. Cada coincidencia se **atribuye** (§6.3.1): solo una coincidencia atribuida a una ocurrencia permitida o a un sustituto emitido queda exenta; **cualquier otra** ⇒ `residue_detected`, sin publicar.

| Objetivo de escaneo (K) | Cómo | Unidad de atribución |
|---|---|---|
| K1 **Texto de las unidades** (contenido `S`) | O1 (oráculo, §6.2) **y** búsqueda de (a)–(e) en `oracle(u)` | `(unit_id, [start,end))` |
| K2 **Texto de cada nodo** (`w:t`, `w:instrText`, `m:t`, cualquier nodo de texto, *tails*) | valor del nodo tras analizar (referencias de carácter y entidades predefinidas resueltas) | `(parte, node_path, [c0,c1))` en caracteres del nodo |
| K3 **Concatenación por párrafo** de **todos** sus nodos de texto (S y no S) en orden de documento | cubre valores **partidos entre nodos** (entre runs, entre `w:instrText` de un campo) | tramo de la concatenación → lista de `(node_path, [c0,c1))` |
| K4 **Atributos** de todas las partes XML (incluidos `wp:docPr/@descr/@title`, `@w:instr`, `@w:val` y los estructurales) | valor de cada atributo; además decodificado como URL (`%XX` en UTF-8) | nunca exenta (fallo cerrado aun en atributos estructurales: falso positivo aceptado) |
| K5 **Relaciones** (`Target` internos y externos, incluidos destinos de hipervínculo) | valor y valor decodificado como URL | nunca exenta |
| K6 **Comentarios XML, instrucciones de procesamiento y CDATA** | texto | nunca exenta |
| K7 **Bytes crudos de cada parte XML** | UTF-8, UTF-16LE y UTF-16BE de (a)–(c) sobre los bytes tal como están en el ZIP (descomprimidos) | tramo de bytes `[b0,b1)` de la parte |
| K8 **Bytes crudos de cada parte binaria** (imágenes, fuentes, otras) | ídem | nunca exenta |

#### 6.3.1 Atribución de coincidencias y ocurrencias permitidas [D5, E-21]

1. **Ocurrencias permitidas** `A`: los tramos `(unit_id, [start,end))` de `oracle(u)` donde aparece un valor seleccionado en cualquier representación (a)–(e) y que **no** se solapan con un sustituto. Por construcción son texto no seleccionado (D5: ocurrencias idénticas fuera de la selección). Cada una se traduce, con las piezas del `SourceMap` del **derivado** (extractor de A05), a su **ubicación exacta**: la lista de `(parte, node_path, [c0,c1))` de los `w:t` que la contienen.
2. **Tramos de sustituto** `M`: los de cada marcador o `[REDACTED]` emitido; una coincidencia **enteramente dentro** de un sustituto (p. ej. un valor que es subcadena de `[REDACTED]`) se atribuye al sustituto: no es copia del valor.
3. **Mapa de bytes de nodos**: el verificador recorre cada parte XML con `xml.parsers.expat` (biblioteca estándar) y registra, para cada elemento `w:t`, su `node_path` y el tramo de bytes `[b0,b1)` de su **contenido de caracteres** en la parte (entre el fin de la etiqueta de apertura y el inicio de la de cierre, con `CurrentByteIndex`), y si ese contenido contiene referencias de carácter o entidades.
4. **Regla de exención** (una coincidencia se exime **solo** si se cumple su caso; cada ocurrencia permitida exime **a lo sumo una** coincidencia por objetivo y por representación):

| Objetivo | La coincidencia está exenta si y solo si |
|---|---|
| K1 | su tramo es exactamente una ocurrencia de `A` o está dentro de un tramo de `M` |
| K2 | está en un nodo `w:t` que es pieza de unidad y su `(node_path, [c0,c1))` es exactamente la ubicación de una ocurrencia de `A` contenida en ese único nodo, o cae dentro de un sustituto |
| K3 | su tramo corresponde exactamente a la lista de ubicaciones de una ocurrencia de `A` (también si está partida entre varios `w:t`), o cae dentro de un sustituto |
| K7 | su tramo de bytes `[b0,b1)` está **dentro** del contenido de un único `w:t` cuya ubicación coincide con una ocurrencia de `A` contenida en ese nodo, y `[b0,b1)` es exactamente la codificación UTF-8 de los caracteres `[c0,c1)` de esa ocurrencia (contenido literal, sin referencias); o cae dentro de los bytes de un sustituto |
| K4, K5, K6, K8 | nunca |

5. **Consecuencias.** (i) Una ocurrencia permitida dentro de un **solo** `w:t` aparece en K1, K2, K3 y K7 y en todos se atribuye a su ubicación: **pasa**. (ii) Una **copia adicional** en la **misma parte** —en un atributo (`wp:docPr/@descr`), en un `w:instrText`, en un comentario XML o en otro nodo que no sea esa ubicación— produce coincidencias en K2/K3/K4/K6/K7 que **no** se atribuyen a ninguna ocurrencia permitida: **falla**. (iii) Una ocurrencia permitida **partida entre runs** no produce coincidencia contigua en K2 ni en K7; K1 y K3 la atribuyen a su lista de ubicaciones: **pasa**; un valor **seleccionado** que reapareciera partido entre runs fuera de las ocurrencias permitidas rompe O1 (el texto difiere del oráculo) y produce en K3 una coincidencia no atribuible: **falla** (comprobación independiente de valores partidos, conservada). (iv) Una ocurrencia permitida escrita con referencias de carácter no produce coincidencia literal en K7; K2/K3 la atribuyen.
6. La atribución se calcula **en el verificador** con su propio analizador (`zipfile`, `expat`, `ElementTree`) y el `SourceMap` del derivado producido por el extractor de A05; las posiciones nunca salen del proceso ni van al informe.

- **Una búsqueda literal de bytes no basta** y no es la única comprobación: valores partidos entre runs solo aparecen en el oráculo y en las concatenaciones; codificaciones y entidades solo tras analizar.
- **Ausencia global** (CN-03): cuando el análisis selecciona **todas** las ocurrencias de un valor, el oráculo no lo contiene en ninguna unidad y las demás reglas exigen su ausencia: el valor queda ausente de **todo** el alcance inspeccionable.
- **Fuera de cobertura** (declarado en el informe, nunca «verificado»): texto dibujado en píxeles de imágenes, glifos de fuentes incrustadas u ofuscadas, contenido de partes que A05 no extrae como texto (p. ej. `w:sym`, H-A07-08), y cualquier transformación del valor no listada en (a)–(e) (traducción, abreviatura, codificaciones distintas de las anteriores).
- **Alcance inspeccionado explícito:** el informe incluye `inspection_scope` con, por categoría de la tabla, las partes inspeccionadas y su número, las representaciones usadas y la lista `not_covered` (clases fuera de cobertura y su cantidad). Nunca contiene valores ni posiciones de residuos.

### 6.4 Conservación de la estructura (esqueleto)

E1: partes no editadas (incluidas `styles`, `numbering`, `settings`, `fontTable`, `theme`, `webSettings`, medios y fuentes) **idénticas byte a byte** (contenido descomprimido). E2: en cada parte editada, el árbol del derivado es igual al del original salvo (i) el texto de las piezas editadas y su `xml:space`, (ii) los `w:t`/runs eliminados por §4.5, (iii) los anclajes de comentario eliminados, (iv) los elementos de propiedades eliminados por §5; en particular `w:tblGrid`, `w:gridSpan`, `w:vMerge`, `w:pPr`, `w:sectPr`, referencias de notas, encabezados y pies son iguales. Toda otra diferencia ⇒ `derivative_verification_failed`.

## 7. Restauración del derivado exacto [D3, D4]

### 7.1 Entrada y autorización

- El host presenta **la copia que posee** del derivado como archivo de A05 (`file_id`, cargado con `upload_file` o registrado con `register_external_file`, ambos con `files:write`) junto con las referencias que exige el contrato: `derivative_id`, `context_id` y `kind` del contexto [P-02]. El motor verifica **identidad, tamaño y digest** de esa copia contra el **vínculo persistido** (T1–T2) y **no** restaura a partir de un DOCX que no sea byte a byte el emitido. La restauración funciona con la copia exacta del host **aunque el original y el análisis fuente ya hayan expirado o se hayan purgado** (y aunque se hayan eliminado los datos del trabajo de protección, P-06).
- Requiere `docx:restore` (distinto de `docx:protect`) [D2, P-05]: **`docx:restore` autoriza producir un archivo restaurado**. El host comprueba el acceso a la conversación o trabajo concreto (A06 P-A06-06).
- **Restauración como texto** [P-01]: un valor cuyo marcador se emitió en un DOCX también puede recuperarse **como texto** con `restore_text` de A06 (`text:restore`), si se cumplen **todas** las condiciones de A06 §7: el host incluyó ese **marcador exacto** en el material que entregó al modelo para esa respuesta (`delivered_material`/`delivered_markers`), el host comprobó el acceso a la conversación o al trabajo concreto, el contexto está vigente y del tipo pedido, el scope activo y se evaluaron los datos nuevos de la respuesta. Esa vía no produce ni da acceso a ningún archivo: `docx:restore` sigue siendo necesario para un DOCX restaurado y `docx:restored:read` para leerlo. A07 no cambia `restore_text`.
- La restauración depende **solo** de: el derivado (sus bytes), su **vínculo** persistido y un **contexto vigente** con sus correspondencias. **No** necesita el original, el análisis fuente ni el trabajo de protección: funciona aunque estén vencidos, eliminados o purgados [D3].
- **No llama a la inferencia** ni evalúa «datos nuevos» (a diferencia de A06 §7.3): la igualdad del digest garantiza que el contenido es exactamente el emitido por el motor, sin texto de terceros [E-09].

### 7.2 Comprobaciones, en este orden

| # | Comprobación | Error |
|---|---|---|
| T1 | El vínculo existe en **este scope**, `state = active` (su contexto no terminó), su `context_id` y `kind` son los pedidos. El estado de los datos del trabajo de protección **no** interviene [P-06] | `resource_not_found` / `derivative_binding_unavailable` / `context_kind_mismatch` |
| T2 | El archivo de entrada es `stored`, no vencido; su SHA-256 y tamaño (calculados por A05 sobre los bytes recibidos) son **iguales** al digest y tamaño del vínculo. Se comprueba **en el envío** (metadatos) y de nuevo **sobre los bytes leídos** en el intento, **antes de analizar** nada | `derivative_mismatch` |
| T3 | Contexto vigente (`ahora < expires_at`, no eliminado), `kind` correcto, scope activo | `context_unavailable` / `scope_disabled` |
| T4 | **Integridad del vínculo**: `binding_mac` verificado (§12.1) con la clave de índice del contexto | `binding_integrity_failed` / `protection_key_unavailable` |
| T5 | En el proceso aislado, la extracción de A05 del archivo produce marcadores S **exactamente** en las ocurrencias del vínculo (misma unidad, `[start,end)`, parte, `node_path`, offsets y token), sin marcadores S adicionales, sin R1/R2 en ninguna unidad, y el extractor tiene el `id`/`version` del vínculo | `derivative_markers_invalid` |
| T6 | Cada token tiene correspondencia en el contexto; descifrado autenticado con el AAD de A06 §9.2 | `marker_unknown` / `mapping_integrity_failed` / `protection_key_unavailable` |

- **Archivo editado** (cualquier byte distinto) ⇒ T2 lo rechaza antes de abrirlo. **Marcadores ajenos, alterados o fuera de posición** ⇒ T5 (defensa en profundidad frente a un vínculo, un archivo de storage o una fila manipulados; con digest idéntico, solo puede ocurrir por manipulación). Ningún valor se recupera antes de T1–T5.
- **Autorización por construcción:** el conjunto restaurable es exactamente el de las ocurrencias del vínculo (emitidas por el motor); no hay material declarado por el host ni marcadores aportados [D3].

### 7.3 Aplicación (una pasada)

- Cada ocurrencia se sustituye por el original **en el mismo `w:t`** donde está el marcador (el marcador está íntegro en un nodo, §4.5), adoptando el formato de ese run (= el del primer run afectado al proteger) [D4]; `xml:space="preserve"` si hace falta. El original es texto puro (R1 de §4.3 garantiza que solo había `w:t`), de modo que el **valor exacto** se restituye en un solo run.
- **Una sola pasada** de izquierda a derecha por nodo; el texto reinsertado **no** se vuelve a examinar: un original que contenga sintaxis de marcador aparece literal (A06 §7.4).
- `[REDACTED]` permanece; las clases `E` siguen ausentes; nada eliminado irreversiblemente reaparece [D3, D5].

### 7.4 Verificación del restaurado

R-V1 a V4 de §6.1 y V6 (clases `E` ausentes). R-O1: la extracción independiente y la de A05 del restaurado producen, por unidad, el texto del derivado con cada ocurrencia sustituida por su original (oráculo calculado en el padre). R-E2: el árbol de cada parte es igual al del derivado salvo el texto de los nodos de las ocurrencias y su `xml:space`; las demás partes, byte a byte. R-M: ninguna coincidencia S de las ocurrencias restauradas permanece (las de los originales que contengan sintaxis de marcador se admiten solo dentro del tramo restaurado). Fallo ⇒ `derivative_verification_failed`, sin publicar.

### 7.5 Garantía y límites

**Garantiza** el valor exacto de cada seudonimización autorizada y la estructura dentro de los límites de §4: texto, párrafos, tablas, encabezados, pies, notas, estilos y referencias del derivado. **No** garantiza: la distribución del formato mixto dentro del valor original (D4); la recuperación de redacciones ni de clases `E` (no hay correspondencia); la identidad byte a byte con el original (A04 D-A04-03; playbook §9: restaurar valores no es reconstruir el archivo).

## 8. API Python, tipos, estados y errores [D2]

### 8.1 Firmas (propuestas / no disponibles)

```python
# Propuesto (A07, no disponible). Métodos nuevos de ScopedOperations; todos rechazan HostTransaction.
async def submit_docx_protection(self, scope_id: str, *, analysis_job_id: str, policy: ProtectionPolicy,
                                 context: ExistingContext | NewContext, idempotency_key: str
                                 ) -> DocxProtectionAccepted: ...
async def get_docx_protection_result(self, scope_id: str, job_id: str) -> DocxProtectionResult: ...
async def open_docx_protected(self, scope_id: str, job_id: str) -> AsyncIterator[bytes]: ...     # verificado (A05 §3.7)
async def get_docx_derivative(self, scope_id: str, derivative_id: str) -> DocxDerivativeInfo: ...

async def submit_docx_restoration(self, scope_id: str, *, derivative_id: str, file_id: str,
                                  context_id: str, kind: Literal["conversation", "job"],
                                  idempotency_key: str) -> JobAccepted: ...
async def get_docx_restoration_result(self, scope_id: str, job_id: str) -> DocxRestorationResult: ...
async def open_docx_restored(self, scope_id: str, job_id: str) -> AsyncIterator[bytes]: ...      # verificado

@dataclass(frozen=True)
class ExistingContext:            # contexto de A06 ya creado (create_context), vigente, del tipo indicado
    context_id: str
    kind: Literal["conversation", "job"]

@dataclass(frozen=True)
class NewContext:                 # el envío crea un contexto `job` de A06 en la misma transacción [P-07]
    ttl_s: int | None = None      # misma regla que create_context (A06 §8.2)
```

- **`NewContext`** [P-07]: con `docx:protect` (sin `text:protect`), el envío crea un contexto de A06 de tipo `job` (cuota, clave de índice envuelta con la versión activa, `expires_at` fijo) **en la misma transacción** que admite el trabajo: se confirman **juntos** o ninguno (un fallo o una reversión no deja contexto sin trabajo ni trabajo sin contexto). Un **replay idempotente** (misma clave y huella) devuelve el **contexto original** (mismo `context_id`, `kind` y `expires_at`), sin crear otro. **`ExistingContext`**: contexto de A06 del **mismo scope**, vigente y del tipo indicado; el host autoriza el acceso a esa conversación o trabajo antes de delegar (A06 P-A06-06).

- Consulta de estado, cancelación y eliminación de datos: **las de A05** (`get_job`, `list_jobs`, `cancel_job`, `delete_job_data`), con su permiso y su `JobInfo` sin cambios (`operation ∈ {docx_protection, docx_restoration}`).
- Sin `DocumentSetup`, `StorageSetup` o `ProtectionSetup` ⇒ `documents_not_configured` / `storage_not_configured` / `protection_not_configured` (antes de efectos).

### 8.2 Tipos de salida (sin valores originales)

```python
class DocxProtectionAccepted:   job: JobInfo; replayed: bool; context_id: str; context_kind: str; context_expires_at: datetime

class DocxProtectionResult:     # artefacto `docx_protection_report` (JSON canónico)
    contract_version: Literal[1]; job_id: str; derivative_id: str
    derivative: {sha256, size_bytes, format: "docx"}
    source: {analysis_job_id, file_id, file_sha256, extractor{id, version, config_sha256}, profile: ProfileRef,
             provenance: Provenance}        # Provenance de A03 (mock: simulated = true)
    context: {context_id, kind, expires_at}
    policy: {default_action, actions}
    units: [{unit_id, groups: [{action, input_start, input_end, output_start, output_end, marker | None}]}]
    counts: {findings, groups, pseudonymized, redacted, new_markers, reused_markers, occurrences}
    authorized_removals_performed: [{element_class, count}]
    coverage: {textual_subset_complete, document_coverage_complete,     # del derivado (§11 de A05, tras eliminar E)
               declared_not_covered: [{element_class, count, reason}]}
    inspection_scope: {...}                                             # §6.3
    verification: {package, oracle, structure, residues: "passed"}      # nunca valores
    transformer: {id, version}

class DocxDerivativeInfo:       scope_id; derivative_id; protection_job_id; context_id; context_kind
                                sha256; size_bytes; occurrence_count; state: Literal["active", "ended"]   # sin revocación individual [P-06]
                                created_at; ended_at | None
class DocxRestorationResult:    # artefacto `docx_restoration_report`
    contract_version: Literal[1]; job_id: str; derivative_id: str; restored: {sha256, size_bytes}
    counts: {occurrences_restored, distinct_markers}; removed_classes_absent: [...]; verification: {...}
```

### 8.3 Estados

Los de A05 §5.3 (`queued`, `running`, `retry_wait`, `succeeded`, `failed`, `cancelled`). Fases: protección `validation` → `extraction` (reextracción y vinculación) → `planning` → `mapping` (correspondencias de A06) → `transformation` → `verification` → `publication`; restauración `validation` (digest) → `extraction` (posiciones) → `mapping` (descifrado) → `transformation` → `verification` → `publication` [E-10]. Un trabajo `succeeded` tiene sus artefactos `published` **y**, si es de protección, su vínculo `active` (misma transacción).

### 8.4 Errores del envío (Python)

| Código | Reintentable | Cuándo |
|---|---|---|
| `analysis_source_invalid` (A06) | no | S1 en el envío |
| `analysis_expiring` (nuevo) | no | S3 |
| `job_not_succeeded`, `job_data_expired`, `file_not_usable`, `file_expiring` (A05) | no | S2, S4 |
| `context_unavailable`, `context_kind_mismatch` (A06) | no | contexto existente vencido/eliminado o de otro tipo |
| `context_expiring` (nuevo) | no | `context.expires_at < deadline_at` del trabajo |
| `derivative_binding_unavailable` (nuevo) | no | vínculo `ended` (su contexto terminó y la limpieza lo marcó) o purgado |
| `derivative_mismatch` (nuevo) | no | T2 en el envío |
| `idempotency_conflict`, `quota_exceeded`, `scope_disabled`, `permission_denied`, `resource_not_found`, `transaction_incompatible`, `*_not_configured` | según A01–A06 | — |

### 8.5 Códigos de fallo del trabajo (amplían el catálogo cerrado de A05 §7.1)

| Código | Reintentable | Origen |
|---|---|---|
| `analysis_unavailable` | no | S2 en intento o publicación |
| `analysis_source_invalid`, `analysis_source_stale`, `detection_inconclusive`, `capabilities_not_configured` | no | S5–S11 |
| `analysis_provenance_unsupported` (nuevo) | no | S10b: análisis no producido por el mock de A03 (p. ej. adaptador de modelo, `simulated = false`) o procedencia distinta de la fijada |
| `marker_collision` | no | PC-04 |
| `field_result_not_transformable`, `selection_crosses_boundary`, `selection_not_transformable` | no | §4.3–§4.4 |
| `unsupported_structure` (A05) | no | PC-01 a PC-03 |
| `residue_detected`, `derivative_verification_failed` | no | §6, §7.4 |
| `derivative_mismatch`, `derivative_markers_invalid`, `derivative_binding_unavailable`, `binding_integrity_failed` | no | §7.2 |
| `context_unavailable`, `context_kind_mismatch`, `protection_key_unavailable`, `mapping_integrity_failed`, `marker_unknown` | no | A06 |
| `quota_exceeded` (cuotas de contexto de A06 §15) | no | `max_mappings_per_context`, `max_context_bytes` |
| `limit_exceeded` | no | §13 |
| los reintentables de A05 (`storage_unavailable`, `database_unavailable`, `lease_lost`, `extraction_process_failed`) | sí | A05 §7.1 |

Mensajes genéricos; nunca texto, valores, marcadores, tokens, posiciones ni nombres de parte.

## 9. Permisos y acceso a artefactos [D2, P-05]

### 9.1 Permisos nuevos (catálogo 21 → **25**; aprobados por el usuario, no implementados)

| Permiso | Habilita |
|---|---|
| `docx:protect` | `submit_docx_protection` (con `ExistingContext` o `NewContext`); autoriza al **motor** a usar el original fijado del análisis para producir el derivado. **No** autoriza descargar el original, que sigue exigiendo `files:read` (A05) |
| `docx:restore` | `submit_docx_restoration` (**distinto** de `docx:protect`): autoriza **producir un archivo restaurado**. Recuperar un valor **como texto** es `text:restore` de A06 (§7.1, P-01) |
| `docx:protected:read` | `get_docx_protection_result`, `open_docx_protected`, `get_docx_derivative` |
| `docx:restored:read` | `get_docx_restoration_result`, `open_docx_restored` (material **sensible**), con las condiciones de §9.3 [P-03] |

- Combinaciones: `submit_docx_protection` exige además `results:read` (consume un resultado sensible de A05, como `JobAnalysis` en A06 §12.1). La carga o registro de la copia a restaurar exige `files:write` (A05). Sin jerarquías ni comodines; `AdminContext`/`admin:*` no acceden.
- **Restricción de despliegue** heredada y ampliada: no emitir claves con estos cuatro permisos hasta actualizar **todos** los runtimes que acceden a la base (un runtime anterior falla al cargar grants desconocidos y revocar no lo corrige). Orden: migrar → actualizar runtimes → configurar → emitir claves.

### 9.2 Clases de artefacto y lectores

| Clase | Rol (`job_artifacts.kind`) | Sensibilidad | Lector y permiso | Lectores genéricos de A05 |
|---|---|---|---|---|
| DOCX protegido | `docx_protected` | contiene el contenido **no seleccionado** del original | `open_docx_protected` · `docx:protected:read` | **denegados** |
| Informe de protección | `docx_protection_report` | posiciones de grupos y marcadores (no valores) | `get_docx_protection_result` · `docx:protected:read` | **denegados** |
| DOCX restaurado | `docx_restored` | **sensible** (= valores originales) | `open_docx_restored` · `docx:restored:read` [P-03] | **denegados** |
| Informe de restauración | `docx_restoration_report` | conteos | `get_docx_restoration_result` · `docx:restored:read` | **denegados** |

**Lectores genéricos que no deben eludir los controles** [D2, E-11]:

1. `get_job_result` / `open_job_artifact` (Python, `results:read`) y `GET /jobs/{id}/result` · `/source-map` (HTTP): sirven **solo** trabajos `document_analysis` y `text_analysis`; para `docx_protection`/`docx_restoration` ⇒ `resource_not_found` (404 idéntico en HTTP). Se comprueba la **operación** además del `kind`. En runtimes anteriores, que solo buscan roles `result`/`source_map` por `kind`, los artefactos de A07 tampoco se sirven [H-03].
2. `open_file_content` (`files:read`): los derivados **nunca** son `stored_files` (son artefactos de trabajo), de modo que no pueden descargarse como archivos. La copia que el host carga para restaurar es un archivo suyo (A05 §3.7: solo descargable con validación `passed`).
3. `get_job`/`list_jobs` (`jobs:read`, Python y HTTP): exponen solo los metadatos de `JobInfo` (sin contenido, sin `context_id`, sin `derivative_id`).
4. `restore_text` de A06 (`text:restore`): **admitido** para marcadores emitidos en un DOCX [P-01], con **todas** las condiciones de A06 §7 (marcador exacto en el material que el host entregó al modelo para esa respuesta, acceso del host a la conversación o trabajo concreto, contexto vigente, scope activo, evaluación de datos nuevos). Devuelve **texto**; no produce, no lee y no habilita ningún DOCX restaurado. No es una vía para eludir `docx:restore`/`docx:restored:read`: esos permisos controlan el **archivo**; `text:restore` controla la **restauración de texto** que A06 ya define para cualquier marcador del contexto.
5. `delete_job_data` (`data:delete`) sobre un trabajo de A07: elimina sus artefactos según A05. En uno de protección **conserva el vínculo** [P-06]: mientras el contexto siga vigente, el host puede restaurar presentando su copia exacta. A07 **no ofrece revocación individual** por derivado; eliminar el contexto (`delete_context`) termina la restaurabilidad de **todos** sus derivados.
6. `cancel_job` (`jobs:cancel`): A05 sin cambios.

### 9.3 Lectura del DOCX restaurado y de su informe [P-03]

Leer cualquiera de los dos exige, **en un único instante de autorización** con la hora de la base: `docx:restored:read`; trabajo `succeeded` con `data_state = available` y `result_expires_at > ahora` (vigencia del resultado, D-A05-01); el **contexto** del vínculo vigente (no vencido ni eliminado) y del tipo registrado; y el **scope activo** (`status = active`). Si falta cualquiera ⇒ `resource_not_found` (permiso presente, recurso no disponible), `job_data_expired`, `context_unavailable` o `scope_disabled`, sin entregar ningún byte. Las lecturas de A05 sobre scopes deshabilitados (A05 §9.3) **no** se extienden al restaurado.

| Camino | Pasos | Instante de autorización |
|---|---|---|
| `open_docx_restored` (bytes) | (1) comprobación previa de permisos y existencia; (2) lectura del objeto al **spool** privado verificando SHA-256 y tamaño (A05 §3.7), sin entregar nada; (3) **transacción corta de autorización** (lee la hora de la base, comprueba todas las condiciones de arriba); (4) solo si pasa, se entrega el *stream* al llamador | **(3)**: después de verificar el objeto en el spool e **inmediatamente antes** de entregar el *stream*. Si el contexto vence o se elimina, o el scope se deshabilita, **mientras se lee el storage** (paso 2), la descarga **no empieza** y el spool se libera. Una descarga ya entregada en (4) se rige por ese instante: un `delete_context` posterior no la interrumpe (se sirve desde el spool verificado) |
| `get_docx_restoration_result` (informe JSON) | lectura y verificación del artefacto en el spool; deserialización; transacción de autorización; devolución del objeto | la transacción de autorización, **después** de leer y verificar el informe e inmediatamente antes de devolverlo |

El DOCX protegido y su informe (`docx:protected:read`) siguen la regla de lectura de resultados de A05 (vigencia del resultado; permitidos con el scope deshabilitado, A05 §9.3), con el mismo orden de pasos: verificación en el spool y luego la decisión.

## 10. Ejecución durable, bloqueos y publicación [D2]

### 10.1 Orden común de bloqueos (extiende A05 §6.2 y A06 §11.2)

| Rango | Recurso | Uso en A07 |
|---|---|---|
| 1 | `scopes` | `FOR SHARE` |
| 2 | `scope_job_fences` | lectura / reconciliación (A05) |
| 3, 4 | `scope_usage`, `engine_usage` | `FOR UPDATE` (contadores de A05) |
| 5 | `idempotency_records` | `ON CONFLICT DO NOTHING` |
| 6 | `stored_files` | `FOR SHARE` (original o copia a restaurar) |
| 7 | `file_validations` | lectura |
| 8 | `jobs` (por `job_id` ascendente) | análisis fuente `FOR SHARE`; trabajo propio `FOR UPDATE` o `FOR SHARE` (comprobación de posesión) |
| 9 | `job_attempts`, `job_artifacts` | `FOR UPDATE` |
| 11 | `protection_scope_usage` | `FOR UPDATE` (solo `NewContext`) |
| 12 | `protection_contexts` | `FOR SHARE` (envío, publicación, descifrado) o `FOR NO KEY UPDATE` (correspondencias) |
| 13 | `protection_mappings` | inserción / lectura |
| **14** | **`docx_derivatives`** (nuevo) | inserción (publicación) / `FOR SHARE` (restauración) / `FOR UPDATE` (limpieza) |

Secuencias (siempre ascendentes; hora de la base leída **después** del último bloqueo):

| Operación | Secuencia |
|---|---|
| `submit_docx_protection` | 1 → 2 → 3 → 4 → 5 → 6 (original) → 7 → 8 (análisis `FOR SHARE`; inserción del trabajo) → [11 → 12 inserción] o [12 `FOR SHARE`] |
| `submit_docx_restoration` | 1 → 2 → 3 → 4 → 5 → 6 (copia) → 8 (inserción) → 12 `FOR SHARE` → 14 `FOR SHARE` |
| Reclamación, heartbeat, reintento, fallo, cancelación | A05 sin cambios |
| Correspondencias (protección) | 1 → 8 (propio, `FOR SHARE`, posesión) → 12 `FOR NO KEY UPDATE` → 13 |
| Descifrado (restauración) | 1 → 8 (propio, `FOR SHARE`, posesión) → 12 `FOR SHARE` → 13 (lectura) → 14 `FOR SHARE` |
| Publicación (protección) | 1 → 2 → 3 → 4 → 6 (original) → 7 → 8 (análisis `FOR SHARE` y propio `FOR UPDATE`, por `job_id`) → 9 → 12 `FOR SHARE` → 13 (lectura de los tokens usados) → 14 (inserción) |
| Publicación (restauración) | 1 → 2 → 3 → 4 → 6 (copia) → 8 (propio `FOR UPDATE`) → 9 → 12 `FOR SHARE` → 14 `FOR SHARE` |
| `delete_job_data` (protección) | A05 sin cambios (1 → 3 → 4 → 8 → 9); **no** toma 14: el vínculo no cambia [P-06] |
| Limpieza de vínculos | 1 → 12 `FOR SHARE` → 14 `FOR UPDATE` |
| `delete_context` (A06) | sin cambios (1 → 11 → 12 → 13); no toma 14, de modo que no hay ciclo |

### 10.2 Instante de decisión de la publicación

En la transacción de publicación, tras adquirir **todos** los bloqueos de su secuencia, se lee la hora de la base **una vez** y con ella se comprueban juntas: (a) las precondiciones de A05 §6.5 (posesión, plazos, cancelación, scope activo y fence, entrada `stored` y vigente con su validación, conjunto exacto de artefactos: protección `{docx_protected, docx_protection_report}`, restauración `{docx_restored, docx_restoration_report}`); (b) S2 (análisis fuente) en la protección; (c) contexto vigente, no eliminado y del tipo correcto; (d) en la protección, que cada token usado tenga su fila en `protection_mappings`; en la restauración, que el vínculo siga `active`. Solo si todo se cumple se inserta (protección) el vínculo `active` con `binding_mac` y se aplica T3; ambas cosas en la **misma** transacción. Un artefacto **nunca** se vuelve visible sin su vínculo, ni un vínculo sin su artefacto publicado [D3, E-12].

### 10.3 Ciclo, fallos y recuperación

| # | Situación | Resultado |
|---|---|---|
| F1 | Contexto eliminado o vencido **antes** de la transacción de correspondencias | `failed` `context_unavailable`; nada creado |
| F2 | Contexto eliminado o vencido **después** de crear correspondencias y antes de publicar (también mientras la publicación espera bloqueos: la hora se lee después) | sin publicación: `failed` `context_unavailable`; artefactos `discarded`; las correspondencias nuevas desaparecen con `delete_context` o con la limpieza del contexto vencido (A06 §8.4–8.5) |
| F3 | Análisis fuente vencido o eliminado (`delete_job_data`) antes de publicar | `failed` `analysis_unavailable`; sin publicación |
| F4 | Original eliminado, vencido o perdido antes de publicar | `failed` `input_unavailable` (A05) |
| F5 | Scope deshabilitado (o fence divergente) en cualquier momento | T10 `cancelled` `scope_disabled` (A05 §9); sin publicación; rehabilitar no resucita |
| F6 | Pérdida del lease en cualquier fase | A05: el dueño anterior no registra ni publica; nuevo intento; `lease_lost` reintentable |
| F7 | Reintento tras un intento fallido | rehace todo; las correspondencias **se reutilizan** (mismo fragmento ⇒ mismo marcador, verificado: A06 §9.3.1); artefactos del intento anterior `discarded` y limpiados (A05 §4.4) |
| F8 | Fallo de base durante las correspondencias o resultado de `COMMIT` desconocido | reintentable; el reintento reutiliza las filas confirmadas (A06 §11.3) |
| F9 | Respuesta perdida en la publicación | reconciliación de A05 §7.3; artefacto y vínculo se confirmaron juntos o ninguno |
| F10 | Fallo de storage al escribir o al leer (original, derivado, copia) | `storage_unavailable` reintentable; objeto corrupto ⇒ `stored_object_corrupt` (A05) |
| F11 | Proceso aislado muere o excede un límite | `extraction_process_failed` (reintentable) o `limit_exceeded` |
| F12 | Verificación fallida (residuo, oráculo, estructura) | `failed`, no reintentable; ningún artefacto publicado |
| F13 | Clave ausente o registro alterado | `protection_key_unavailable` / `mapping_integrity_failed` / `binding_integrity_failed`; sin publicación |
| F14 | Restauración: contexto eliminado o vencido (o vínculo marcado `ended`) entre el descifrado y la publicación, también mientras la publicación espera bloqueos | sin publicación (`context_unavailable` / `derivative_binding_unavailable`); el DOCX restaurado escrito queda `discarded` y es elegible para purga inmediata (§11.1, P-04) |
| F15 | Cancelación | A05 §5.4 |
| F16 | Purga de metadatos del trabajo de protección (30 d) | el vínculo **sobrevive** (es independiente del trabajo) hasta que termina el contexto (§11) |
| F17 | `delete_job_data` del trabajo de protección (antes o después de emitir el derivado al host) | artefactos del trabajo eliminados (A05); el vínculo sigue `active`; la restauración con la copia exacta del host **sigue funcionando** mientras el contexto esté vigente [P-06] |
| F18 | `delete_context` del contexto de un derivado | todas las correspondencias desaparecen (A06 §8.4); toda restauración posterior de **cualquier** derivado de ese contexto ⇒ `context_unavailable` (envío o intento); la limpieza marca los vínculos `ended` |

**Escrituras parciales.** Las únicas escrituras previas a la publicación son: correspondencias de A06 (confirmadas, sin referencia hasta publicar; reutilizables; viven y mueren con el contexto) y artefactos `reserved`/`written` (invisibles; `discarded` al terminar el intento; sello o borrado según A05 §4.4). El vínculo **solo** se escribe en la publicación.

## 11. Ciclo de vida y retención

| Dato | Vive | Se elimina |
|---|---|---|
| DOCX protegido e informe | D-A05-01: 7 d desde la terminación | limpieza de A05; `delete_job_data` |
| DOCX restaurado e informe | D-A05-01: 7 d desde la terminación; **además**, solo legibles con contexto vigente y scope activo (§9.3) [P-03] | limpieza de A05; `delete_job_data`; un `docx_restored` descartado, de inmediato (§11.1) [P-04] |
| Vínculo (`docx_derivatives`) | hasta el fin del contexto (vencimiento o eliminación); **no** lo afectan `delete_job_data` ni la purga del trabajo de protección [P-06] | limpieza de vínculos: se marca `ended`, se borran ocurrencias y `binding_mac` y queda una lápida de metadatos 30 d (como A06 §8.5) |
| Correspondencias | A06 (contexto) | A06 |
| Copia cargada para restaurar | A05 (7 d) | A05 |
| Metadatos de trabajos e idempotencia | A05 (30 d) | A05 |

- La vigencia del contexto **no se extiende** por proteger, restaurar ni reintentar (A06 §8.2) [D3].
- Limpieza de vínculos: `engine.maintenance().run_docx_cleanup(batch_size=…)` y CLI `cawbase-content-engine cleanup-docx` (propuestos): explícita, por lotes, repetible, sin claves; marca `ended` los vínculos cuyo contexto ya no está vigente, borra sus ocurrencias y `binding_mac`, y purga lápidas vencidas. **No** cambia `run_cleanup` (A05) ni `run_protection_cleanup` (A06) [E-13].
- Mientras la limpieza no corra, un vínculo de un contexto terminado **no sirve**: la restauración comprueba el contexto en cada decisión (T3) y las correspondencias ya no existen.
- **Sin revocación individual** [P-06]: `delete_job_data` del trabajo de protección elimina sus artefactos y deja el vínculo `active`; la purga de los metadatos del trabajo tampoco lo toca. La única forma de terminar la restaurabilidad de un derivado es que termine su contexto (vencimiento o `delete_context`), lo que la termina para **todos** los derivados de ese contexto.

### 11.1 Purga inmediata de un `docx_restored` descartado [P-04]

- **Excepción acotada a D-A05-01** (aprobada por el usuario): **solo** el artefacto de rol `docx_restored` queda **elegible para purga inmediata** una vez **confirmado su descarte** (fila `discarded` confirmada por el fin del intento, una recuperación, una cancelación o la limpieza). Para él `delete_after = discarded_at` en lugar de `abandoned_at + 24 h`. Todos los demás roles y objetos (incluidos `docx_protected` y los informes) conservan las 24 h de D-A05-01.
- La **eliminación física** sigue el protocolo de A05 §4.3–§4.4 sin atajos: escritura completada ⇒ `delete` + `stat = None`; resultado desconocido ⇒ `delete_and_seal` + `writers_released`; cargo incierto hasta la confirmación. **Depende de que se ejecute la limpieza** (`run_cleanup`): elegible no significa borrado; el motor no borra fuera de una limpieza.
- **Qué runtime aplica la excepción.** La aplica la limpieza de un runtime **`0.6.0` o posterior**, que considera elegible todo `docx_restored` en estado `discarded` **independientemente** del `delete_after` grabado (también los descartados por un runtime anterior, que grabó 24 h). La limpieza de un runtime **anterior** (`0.4.0`/`0.5.0`) no conoce el rol y aplica la regla genérica: los purga cuando vence el `delete_after` grabado (24 h si lo descartó él; inmediato si lo descartó `0.6.0`, porque el valor grabado ya es `discarded_at`). En ningún caso un runtime anterior publica, sirve ni retiene más allá de la regla genérica un `docx_restored`.

## 12. Persistencia y migración `0005`

### 12.1 Tabla nueva `docx_derivatives`

| Columna | Contenido |
|---|---|
| `scope_id`, `derivative_id` (PK; `ddv_` + 32 hex) | identidad; `UNIQUE derivative_id` |
| `protection_job_id`, `attempt_no` | trabajo e intento que lo publicó; `UNIQUE (scope_id, protection_job_id)` |
| `context_id`, `context_kind` | contexto de A06 (sin FK a `protection_contexts`: la lápida de A06 se purga por su lado; la vigencia se comprueba en cada uso) |
| `artifact_id`, `sha256`, `size_bytes` | artefacto `docx_protected` y su digest |
| `extractor_id`, `extractor_version`, `transformer_id`, `transformer_version` | identidad de los procesos |
| `occurrences` (JSON) | lista ordenada `{unit_id, start, end, part_name, node_path, char_start, char_end, token}`; **sin** valores, tipos ni fragmentos |
| `occurrence_count` | tamaño de la lista |
| `binding_mac`, `mac_key_version` | `HMAC-SHA256(clave de índice del contexto, "cce.docx.binding.v1" ‖ JSON canónico de {scope_id, derivative_id, context_id, context_kind, sha256, size_bytes, extractor, occurrences})`; versión con la que está envuelta esa clave (A06 §9.3) [E-14] |
| `state` (`active` / `ended`), `created_at`, `ended_at`, `metadata_expires_at` | ciclo (sin revocación individual, P-06); `ended` lo fija la limpieza cuando el contexto terminó |

- `scope_id` en PK y FK (→ `scopes`); `CHECK` de estados; índices por `(state)`, `(scope_id, context_id)`, `(metadata_expires_at)`.
- **Sin cambios** en tablas de A01–A06: las operaciones y los roles nuevos son valores de texto en `jobs.operation` y `job_artifacts.kind` (sin `CHECK` que los limite, verificado en `persistence/a05_schema.py`); la fijación de A07 (contexto, política, análisis fuente, `derivative_id`) vive en `execution_snapshot`; `result_artifact_id` apunta al informe y `source_map_artifact_id` al DOCX del trabajo de A07 (la operación determina el significado del rol) [E-15, H-04].
- **Migración `0005_docx_protection`**: aditiva, solo hacia adelante; crea la tabla e índices; `schema_level` 4 → **5**; `min_runtime_level` **permanece en 1** (justificación en §12.2); versión **`0.6.0`**; sin extra nuevo (A07 exige los extras `documents` y `protection`).

### 12.2 Compatibilidad con runtimes anteriores (`0.1.0`–`0.5.0`)

- No reclaman trabajos de A07: su `requirements_key` incluye el transformador de A07, que ningún runtime anterior anuncia [H-02].
- Sus lectores genéricos no sirven artefactos de A07 (§9.2 punto 1) [H-03].
- Sus cancelaciones, deshabilitaciones del scope (fence), limpiezas y purgas operan sobre los trabajos y artefactos de A07 con las reglas genéricas de A05 (correctas para ellos).
- **Durante un despliegue mixto**: `delete_job_data` de un runtime anterior tiene el mismo efecto que el de `0.6.0` (elimina artefactos según A05 y no toca el vínculo), de modo que no hay degradación; la purga inmediata de `docx_restored` la aplica solo la limpieza de `0.6.0` (§11.1).

## 13. Idempotencia y límites

- Idempotencia de A05 §8 (clave obligatoria; misma clave y huella ⇒ mismo trabajo; huella distinta ⇒ `idempotency_conflict`). Huellas: protección `{operation, analysis_job_id, policy canónica, context: {existing, context_id, kind} | {new, ttl_s}}`; restauración `{operation, derivative_id, file_id, context_id, kind}`. Una repetición con `NewContext` devuelve el **mismo** contexto (creado una sola vez, en la transacción de admisión del trabajo; §8.1).
- Límites (elecciones iniciales, no mediciones) [E-16]: los de A05 §12 para leer, aislar y escribir (`max_file_bytes` también acota el derivado y el restaurado: exceder ⇒ `limit_exceeded`); `max_docx_selections_per_document` = 10 000; los de A06 §15 por contexto (`max_mappings_per_context`, `max_context_bytes`, `max_fragment_chars`); `max_docx_occurrences_per_derivative` = 10 000; informes ≤ `max_result_bytes`.

## 14. Observabilidad

Registrador `cawbase_content_engine.docx`. Permitido: ids (`job_id`, `derivative_id`, `context_id`, `scope_id`), operación, fase, códigos, conteos (selecciones, ocurrencias, clases eliminadas), tamaños, duraciones. **Prohibido**: texto, valores, marcadores, tokens, posiciones y offsets, nombres de parte y `node_path`, hashes de contenido, `binding_mac`. Sin eventos en `admin_events`.

## 15. Evidencia prevista: muestras, lectores y ambientes

- **Fixtures sintéticos** con manifiesto independiente (oráculo escrito por el generador, nunca por el extractor), valores evidentemente falsos (`example.test`), copiados a `tests/` (sin importar `experiments/a04`): equivalentes a D01–D10 de A04 aptos para la matriz de A05 (sin `customXml`), más casos propios de A07 (campos complejos que cruzan párrafos, controles con bloqueo, marcadores de posición, hipervínculos con la selección dentro y cruzando, CN-02/03/05).
- **Al menos un fixture guardado por un editor habitual**: documento sintético generado y **guardado con LibreOffice Writer** en la imagen Linux de referencia (versión registrada). Si la matriz de A05 lo rechazara (p. ej. por una parte `customXml` o una estructura no admitida), se registra el motivo y se trae al usuario antes de cambiar el alcance [D5].
- **Validación estructural del paquete** (V1–V6, §6.1): en cada trabajo y en las pruebas, con `zipfile`/`expat`/`ElementTree`, `lxml`, `python-docx` (apertura de biblioteca) y el extractor de A05. **No** acredita la apertura en un procesador de textos.
- **Apertura sin reparación e inspección visual** del DOCX **protegido y restaurado** (y del original, para comparar): **LibreOffice Writer en Linux** (Docker). Apertura = carga y conversión a PDF en modo *headless* con éxito y **sin aviso de reparación** ni de formato dañado en la salida del proceso; renders comparables con Poppler (`pdftoppm`). Al **ejecutar** las pruebas se registran: versión de LibreOffice y Poppler, plataforma (SO, arquitectura, imagen), fuentes instaladas usadas en el render y comando. **Microsoft Word queda pendiente** como lector: no está disponible y su apertura no se afirma (como en A04).
- **Ambientes:** macOS (sin renders DOCX si no hay LibreOffice: pendientes declarados) y Linux local (Docker, con aislamiento `linux_container_reference`, PostgreSQL y SQLite reales). **GitHub Actions** no se ejecuta salvo que lo haga el usuario; un *skip* no es aprobación.
- **Fuera de A07:** varias secciones y demás formatos fuera de la matriz; PDF (A08); inferencia real (A09).

## 16. Fuera de alcance y limitaciones declaradas

**Fuera:** PDF (A08); modelo real, calidad y segmentación (A09); HTTP y rutas nuevas; integración con CAWbase (bloque B); ampliar la matriz de A05; restaurar desde el artefacto que guarda el motor sin presentar la copia (B-A07-01); restauración de un DOCX editado; OCR; políticas de salida.

**Limitaciones:** (1) la protección es tan completa como el análisis que la alimenta (mock en A01–A08); (2) por ocurrencia señalada, no por valor (A06 §5.6); (3) selecciones en resultados de campo, controles no simples, contenedores en línea o que crucen hipervínculos y marcadores de posición **fallan** (no se protegen parcialmente); (4) el formato mixto dentro del valor se reduce al del primer run; (5) residuos en atributos estructurales pueden dar falsos positivos (fallo cerrado); (6) texto en píxeles, glifos y contenido no extraído por A05 fuera de cobertura; (7) render solo con LibreOffice; Word no probado; (8) los marcadores revelan posición e igualdad de valores (A06 §6.4); (9) no es anonimización ni cumplimiento normativo.

## 17. Hipótesis por verificar (H-A07-nn)

| ID | Hipótesis | Cómo se verifica |
|---|---|---|
| H-A07-01 | Reextraer el original con el extractor de A05 en el proceso aislado reproduce byte a byte el JSON canónico del `SourceMap` publicado (determinismo) | prueba sobre los fixtures, dos ambientes |
| H-A07-02 | El `requirements_key` de A07 difiere de todos los que anuncian los runtimes `0.4.0`/`0.5.0`, que no reclaman sus trabajos | prueba con el worker de la base anterior simulado por su conjunto de claves |
| H-A07-03 | El código de `get_job_result`/`open_job_artifact` de `0.4.0`/`0.5.0` no sirve artefactos con `kind` distinto de `result`/`source_map` | lectura del código y prueba con el lector genérico anterior a la guarda de operación |
| H-A07-04 | `jobs.operation`, `job_artifacts.kind` e `idempotency_records.operation` no tienen `CHECK` que limite valores; las columnas `NOT NULL` de `jobs` (perfil, entrada) pueden llenarse con la identidad fijada del análisis o del vínculo sin semántica falsa | lectura del esquema congelado y prueba de migración |
| H-A07-05 | Las funciones puras de A06 (unión, política, parser S/R1/R2, cifrado, verificación de reutilización) se pueden llamar desde el worker sin duplicarlas ni cambiar sus contratos | revisión del código |
| H-A07-06 | `lxml` puede editar los nodos por `node_path` sin reserializar de forma distinta las partes no editadas (que se copian sin pasar por el analizador) y conservando la declaración y los espacios de nombres de las editadas | prueba de esqueleto (§6.4) |
| H-A07-07 | LibreOffice 7.4.7 abre y convierte original, protegido y restaurado sin aviso de reparación | renders en Linux con registro de la salida |
| H-A07-08 | El extractor de A05 no extrae `w:sym`, `w:ptab` ni `w:softHyphen`; el contenido que representan queda fuera del texto de las unidades | lectura de `documents/docx.py` y fixture |
| H-A07-09 | Un DOCX guardado por LibreOffice con contenido sintético pasa la matriz de A05 | fixture del editor |
| H-A07-10 | D-33 intacta: importar el paquete no importa nada de A07 | `-m dist` y `scripts/measure_import.py` |
| H-A07-11 | `xml.parsers.expat` (`CurrentByteIndex`) da tramos de bytes exactos del contenido de cada `w:t` en la parte descomprimida, también con referencias de carácter y espacios de nombres, sin `DOCTYPE` ni entidades | prueba pura del mapa de bytes contra fixtures construidos byte a byte |
| H-A07-12 | La lógica de descarte de A05 permite fijar `delete_after = discarded_at` solo para el rol `docx_restored` sin cambiar el comportamiento de los demás roles; la limpieza de `0.4.0`/`0.5.0` purga según el `delete_after` grabado | lectura de `jobs/db.py`/`jobs/maintenance.py` y prueba con filas preparadas |
| H-A07-13 | El resultado de A05 conserva en cada unidad la procedencia de A03 y el `execution_snapshot` fijado conserva `inference_provenance`, de modo que S10b es verificable sin datos nuevos | lectura de `jobs/execute.py` y `jobs/ops.py`; prueba |
