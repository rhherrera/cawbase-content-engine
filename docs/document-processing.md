# Procesamiento de documentos — contrato de A04

> **Estado: ESPECIFICADO y COMPROBADO EN EXPERIMENTOS ACOTADOS (A04); NO implementado como capacidad del motor.** Los experimentos (`experiments/a04/`, entorno independiente) se ejecutaron sobre documentos **sintéticos** en macOS y en Linux local (Docker): resultados y conclusiones en el [informe de viabilidad](a04-feasibility-report.md). **Ningún soporte documental existe en `0.3.0`** (sin API, endpoint, permiso, migración ni dependencia del paquete). Las estructuras de este documento siguen siendo **conceptuales**. La columna «Evidencia» de las matrices refleja lo **observado en esos fixtures**; «demostrado» significa «demostrado sobre los fixtures sintéticos indicados, en el subconjunto probado», no soporte general. GitHub Actions **no** se ejecutó para A04. A04 está pendiente de aceptación del usuario.

Relacionado: [especificación de A04](features/A04-viabilidad-y-contrato-de-documentos.md) · [decisiones de A04](decisions/A04-decisions.md) · [inferencia y análisis (A03)](inference-and-analysis.md) · [arquitectura](architecture.md) · [seguridad](security-and-isolation.md).

Decisiones aprobadas que rigen: **D-A04-01** (dependencias para productos cerrados), **D-A04-02** (contenido auxiliar y rechazos iniciales), **D-A04-03** (fidelidad mínima). Elecciones técnicas: `E-A04-nn` (no son decisiones de producto).

## 1. Seis capas, seis afirmaciones distintas

| # | Capa | Responde a | Nunca implica |
|---|---|---|---|
| 1 | **Inventario estructural** | ¿Qué partes/elementos tiene el archivo y qué disposición recibe cada uno? | que el contenido se extrajo |
| 2 | **Extracción de texto** | ¿Qué texto se obtuvo, en qué unidades y orden? | que sea todo el texto relevante |
| 3 | **Mapeo al original** | ¿A qué piezas exactas del archivo corresponde cada intervalo de texto? | que se pueda eliminar el contenido |
| 4 | **Cobertura de extracción** | ¿Qué contenido relevante del documento fue (o no) extraído? | que fue evaluado por inferencia |
| 5 | **Cobertura de inferencia** | ¿Qué texto extraído evaluó A03 (por capacidad e identificadores)? | que el documento esté protegido |
| 6 | **Transformación y verificación** | ¿Se eliminó/sustituyó y se verificó el resultado? | que no existan otros datos sensibles |

Cuatro afirmaciones que **no se infieren entre sí**: (a) *todo el texto extraído fue evaluado*; (b) *todo el contenido relevante fue extraído*; (c) *todos los hallazgos seleccionados pueden transformarse*; (d) *la transformación pasó las verificaciones previstas*. Los resultados parciales útiles **no autorizan** presentar el documento completo como protegido. El mock acredita ingeniería del contrato, nunca calidad de detección; la ausencia de los valores sintéticos seleccionados **no** acredita ausencia universal de datos sensibles.

## 2. Disposición de cada elemento (E-A04-02, revisada por E-A04-23)

| Código | Nombre | Significado | Efecto en la cobertura |
|---|---|---|---|
| **S** | Contenido cubierto | Contenido textual (o visual con texto) cuya cobertura es **necesaria**: se identifica, se extrae, se localiza y puede (o se declara que no puede) transformarse | Es el **subconjunto textual evaluado** |
| **E** | Eliminación autorizada | Comentarios y metadatos descriptivos/personales autorizados por D-A04-02: se eliminan registrando clases, sin restauración | No impide cobertura; el informe lista las clases eliminadas |
| **P** | Recurso estructural preservado y validado | Estilos, numeración, configuración, fuentes, árbol de páginas, relaciones, tablas de referencias: se **preservan** y se **inspeccionan** (§2.2) | No impide cobertura **solo por existir**, pero no queda exento de inspección |
| **N** | Contenido no cubierto | Contenido o payload que puede portar información relevante y para el que no hay extracción/cobertura (imágenes, destinos de enlaces, instrucciones de campos, texto alternativo, ecuaciones, trazados, porciones desconocidas o no evaluadas) | Se preserva y se **declara**; impide la cobertura completa del documento |
| **R** | Rechazo | El documento (o la unidad) no se procesa; se informa el motivo | Error tipado conceptual; nunca éxito parcial silencioso |

Un elemento **jamás queda sin disposición**: si el inventario no puede clasificarlo es `N` (si puede preservarse sin riesgo de alterar el significado) o `R` («estructura desconocida»). Las **porciones desconocidas o no evaluadas mantienen su disposición conservadora**. Si una precondición no puede verificarse, **no se asume**. Imágenes, destinos de enlaces, instrucciones de campos y texto alternativo **no** se reclasifican como `P` («inocuos»): son `N`, y una búsqueda de valores conocidos en ellos (verificación de residuos) **no demuestra cobertura de detección** sobre esos contenidos.

### 2.1 Condiciones de cobertura (inequívocas)

- **`textual_subset_complete`** (cobertura completa del subconjunto textual evaluado) es verdadera si y solo si: (1) todo elemento `S` del inventario fue extraído en una unidad con texto exacto y mapa **consistente** con el archivo (hash) — sin porción `S` no extraída, ilegible o con mapeo insuficiente; (2) no hay elementos `R`; (3) todo elemento `P` fue inspeccionado conforme a §2.2 sin reclasificarse; y (4) cada unidad fue evaluada por A03 con cobertura `complete = true` en las capacidades requeridas.
- **`document_coverage_complete`** es verdadera si y solo si `textual_subset_complete` **y** no existe ningún elemento `N` ni porción desconocida. Un documento con una imagen, un destino de enlace, una instrucción de campo o un texto alternativo **nunca** tiene `document_coverage_complete = true`.
- Ambos indicadores son **independientes** de la ausencia de residuos (verificación de la salida, capa 6) y **no prometen detección real**: significan «todo lo declarado `S` se extrajo y se entregó a A03 y A03 declaró cobertura completa de su alcance». El mock no mide detección.

### 2.2 Inspección de recursos estructurales (`P`)

Un `P` no está exento: se enumeran las **cadenas, propiedades, relaciones y payloads** que pueda contener (DOCX: nombres de estilo, `w:lvlText` de numeración, `w:docVars`, rsid, nombres de fuente, `Target` de relaciones, atributos de `settings`, partes de tema; PDF: diccionarios de página/recursos, nombres de fuente, `/Info`/XMP fuera de lo `E`, outlines, nombres de destinos, valores de diccionarios desconocidos). Cada cadena o propiedad debe pertenecer a una **lista de permitidas** (propiedades estructurales conocidas con valor de forma válida: números, identificadores de fuente/estilo, booleanos, enumerados); una cadena libre fuera de esa lista se reclasifica `N` (o `R` si es un payload), y **no** cuenta como `P`. La lista de permitidas y su resultado se registran en el informe.

## 3. Identidad y versionado (E-A04-04)

Toda estructura lleva `schema = "document-structures/1"` (conceptual).

```text
DocumentIdentity {
  file_sha256            # SHA-256 del archivo exacto, en bytes
  file_size_bytes
  format                 # "docx" | "pdf" (detectado por contenido, no por extensión)
  extractor { id, version, config_sha256 }   # versión y configuración del extractor
  libraries [ { name, version, license } ]   # versiones fijadas (§15)
}
```

- El **mapa** (`SourceMap`, §6) y el **inventario** quedan **ligados** a esa identidad. No se aplican a otro archivo ni a uno modificado: ante cualquier cambio de `file_sha256`, de la versión del extractor o de su configuración, se **recalculan** o se rechazan (`identity_mismatch`, conceptual).
- La transformación produce un archivo nuevo con su propia identidad; el mapa del original **no** describe la salida.

## 4. Estructuras conceptuales

```text
DocumentInventory { identity, parts[], elements[ {element_id, kind, location, disposition(S|E|N|R), reason?} ],
                    resources[ {kind: font|image|…, disposition, reason?} ],
                    embedded_payloads[], findings_of_preconditions[ {check, result} ] }

ExtractionUnit    { unit_id, kind, text, scope_note, ordinal, source_refs[] }
                  # scope_note y demás metadatos documentales viven aquí, NUNCA en AnalysisResult (A03 no cambia)
                  # text = exactamente lo que se entrega a A03 (§8); unit_id estable dentro de la identidad

SourceMap         { identity, units[ { unit_id, segments[ SegmentRef ] } ] }
SegmentRef        { unit_start, unit_end,                # [start,end) en puntos de código de unit.text
                    kind: "original"|"synthesized",      # synthesized: separador, salto, tabulación sintetizados
                    pieces[ PieceRef ] }                 # 0..n piezas originales; vacío ⇒ no localizable
PieceRef (DOCX)   { part_name, node_path, run_or_text_index, char_start, char_end, relation: "1:1"|"n:1"|"1:n" }
PieceRef (PDF)    { page_index, object_ref, content_stream_ref, operator_index, glyph_range, geometry{quad, rotation}, relation }

ExtractionCoverage{ identity, covered_elements[], preserved_structural[ {element_id, inspected: bool} ],
                    authorized_removals[], declared_not_covered[ {element_id, reason} ], rejected[],
                    textual_subset_complete: bool, document_coverage_complete: bool }   # definiciones: §2.1
InferenceCoverage { unit_id, per_capability: A03 Coverage }   # reutiliza la cobertura de A03 por unidad
TransformPlan     { identity, selections[ {unit_id, type_id, start, end, replacement_class} ], preconditions[] }
VerificationReport{ checks[ {id, result, evidence_ref} ], residue_scan, fidelity, mechanical_restore? }
```

Los indicadores de `ExtractionCoverage` son **independientes** de `Coverage.complete` de A03 (que solo describe el texto de una unidad) y de la ausencia de residuos. No se agregan porcentajes que oculten huecos.

## 5. Unidades Unicode y orden de extracción

- **Unidad:** punto de código Unicode (como `len(str)` en Python y como A03). No bytes, no unidades UTF-16, no grafemas, no índices de glifos. Son cuatro nociones **distintas** y ninguna se deduce de otra:

| Noción | Qué cuenta | Ejemplo (`😀`, `é` descompuesta, `ﬁ`) |
|---|---|---|
| Punto de código | escalar Unicode (`len(str)`) | `😀` = 1; `e`+U+0301 = 2; `ﬁ` (U+FB01) = 1 |
| Unidad UTF-16 | unidad de 16 bits (XML/Java/JS, UCS-2 de algunas APIs) | `😀` = **2** (par sustituto); `e`+U+0301 = 2; `ﬁ` = 1 |
| Grafema | unidad percibida por el usuario | `😀` = 1; `e`+U+0301 = 1; `ﬁ` = 1 |
| Glifo | forma dibujada por la fuente | **sin correspondencia fija**: `😀` puede ser 1 glifo, 2 o un sustituto de fuente; `e`+U+0301 puede ser 1 glifo compuesto o 2; `ﬁ` ≥ 1 |

Un punto de código fuera del BMP ocupa dos unidades UTF-16 y **no** se asume 1 glifo.
- **Conversión explícita:** cada biblioteca expresa índices en su unidad (XML: caracteres del nodo; PDFium: índices de carácter de su textpage, con límites de UCS-2 documentados para `get_text_range`; pypdf: cadenas Python; QPDF/pikepdf: bytes de operandos). La capa de extracción **convierte y registra** la conversión; una conversión no demostrada ⇒ el segmento es `no localizable`.
- **Texto exacto:** lo entregado a A03 es el `unit.text` sin normalización posterior (ni NFC/NFKC, ni recorte). Cambiar el texto invalida el mapa.
- **Relaciones no uno-a-uno**, todas representadas con su **dirección** `relation = <puntos de código> : <piezas/glifos del original>`: separadores sintetizados (`kind=synthesized`, sin piezas, no seleccionables), saltos de línea/párrafo, tabulaciones; **ligadura** (un glifo/código de fuente → varios puntos de código: `n:1`, p. ej. `fi` ← 1 glifo); **glifo compuesto o descomposición** (varios puntos de código ← 1 glifo `n:1`, o 1 punto de código → varios glifos `1:n`); **código de fuente que mapea a un par sustituto** (1 punto de código fuera del BMP ← 1 código `1:1` a nivel de punto de código, aunque el `ToUnicode` lo exprese en dos unidades UTF-16); cadenas fragmentadas en varios runs/operadores (un intervalo → varias piezas). Una selección que cubre **parte** de una pieza indivisible (`n:1`, p. ej. media ligadura o un punto de código dentro de un código de fuente de varios puntos) no es localizable ⇒ fallo.
- **Un intervalo → varias piezas** cuando corresponde (un valor repartido entre runs o entre operadores `TJ`).
- **Ambigüedad o localización insuficiente ⇒ fallo explícito de la transformación** (`localization_ambiguous`, conceptual): sin búsqueda aproximada, sin elegir la primera coincidencia, sin «reparar» offsets. Un hallazgo cuya ocurrencia idéntica no seleccionada existe en otra pieza se localiza **por posición**, no por contenido.
- **Orden determinista** (no necesariamente el de lectura): DOCX — orden de documento del cuerpo (`w:body`), luego encabezados/pies por orden de sección y tipo (default, first, even), luego notas al pie y notas al final por identificador; dentro de celdas, fila a fila y celda a celda. PDF — por página (índice ascendente) y, dentro de la página, orden de ejecución del flujo de contenido (con Form XObjects expandidos en el punto de uso). Se **promete estabilidad**, no coincidencia con el orden semántico (columnas, tablas, texto rotado pueden diferir).
- **Unidad PDF = la página (E-A04-25).** El texto de la unidad es la concatenación, en orden de ejecución, de las cadenas decodificadas de los operadores de visualización de texto, con **separadores sintetizados** (`kind=synthesized`, un `\n`) insertados entre dos visualizaciones consecutivas separadas por cualquier operador de posicionamiento (`Td`, `TD`, `Tm`, `T*`, `BT`) o por un cambio de objeto/Form XObject; dentro de un `TJ` no se sintetiza ningún espacio por el kerning. **No se introduce segmentación semántica**: columnas, tablas y bloques se concatenan en orden de flujo.
- **Resolución de intervalos a piezas:** un intervalo `[start,end)` de la unidad se resuelve por **posición** a las cadenas/operadores que lo contienen (varias piezas si cruza operandos de un mismo `TJ` o visualizaciones consecutivas). Un intervalo que **incluye un separador sintetizado**, o que une piezas de objetos de contenido, líneas o columnas distintos (**concatenación artificial**), o cuyas piezas no tienen mapeo `ToUnicode` fiable, es **ambiguo para transformar** ⇒ fallo (`localization_ambiguous`); el hallazgo sigue siendo válido como detección. Si el texto de la página excede los límites de A03 (§8), la página se rechaza (`límite excedido`): no se parte semánticamente.

## 6. DOCX: referencias

- **Parte:** nombre de parte OPC (`/word/document.xml`, `/word/header1.xml`, `/word/footnotes.xml`, …) más la relación que la referencia (`r:id` desde la sección, no solo existencia del archivo).
- **Nodo:** ruta determinista de índices de hijos desde la raíz de la parte (`w:body/w:tbl[2]/w:tr[1]/w:tc[3]/w:p[1]/w:r[4]/w:t[1]`), con el **espacio de nombres** completo.
- **Segmento de texto:** `w:t` (y `w:tab`, `w:br`, `w:cr`, `w:noBreakHyphen`, `w:sym` según su regla), con `char_start/char_end` dentro del nodo. Un valor repartido entre runs ⇒ varias piezas; en celdas, la pieza referencia la celda concreta (fila, columna, `gridSpan`, `vMerge`); en celdas combinadas el texto se cuenta **una vez**.
- Revisiones (`w:ins`, `w:del`, `w:moveFrom/To`, `w:*PrChange`) ⇒ `R` (§11). `python-docx` no expone párrafos/tablas dentro de revisiones ni tablas anidadas en `Document.tables` (documentación consultada, §15): la inspección debe hacerse sobre el XML.

## 7. PDF: referencias

- **Página** (también unidad de extracción, §5): índice, `MediaBox`/`CropBox`, `/Rotate`, herencia de recursos.
- **Geometría:** cuadrilátero en coordenadas de página no rotada y en coordenadas de visualización, con la matriz de transformación aplicada (CTM, `Tm`, `Tz`, `Ts`, rotación de página). **Una caja geométrica no demuestra por sí sola que se pueda eliminar el contenido correcto**: es un indicio de localización, no un mapa de eliminación.
- **Caracteres/glifos:** código de carácter del flujo, fuente, `ToUnicode`/codificación, glifos por carácter (ligaduras 1→n). Sin `ToUnicode` fiable ⇒ mapeo insuficiente (`R`/fallo).
- **Objetos de contenido:** flujo de contenido (referencia de objeto + posición del operador `Tj/TJ/'/"`), pertenencia a **Form XObject** (un mismo objeto usado en varias páginas o varias veces: modificar el objeto modifica **todos** los usos).
- La eliminación debe actuar sobre los **operandos de texto** del flujo (y no solo ocultar): tapar con un rectángulo **no** es eliminación y debe fallar la verificación.

## 8. Integración con A03

Se reutiliza **sin modificar** el contrato de A03: `analyze_text` y el mock, `MockFixture.for_request(...)`, `build_inference_request`, `locate(text, fragmento, occurrence)` y `full_coverage(ids, text)` para fixtures ligados a la entrada exacta y a `definitions_digest`, procedencia simulada, cobertura por capacidad e identificadores, hallazgos como `(type_id, start, end)` y límites vigentes (`max_analysis_text_chars` 8192, `max_analysis_text_bytes` 24 576, `max_analysis_regions` 256, `max_analysis_findings` 500, `max_finding_chars` 2048).

- `AnalysisResult` **no** se modifica con páginas ni nodos. El `SourceMap` es una estructura separada; un hallazgo A03 se resuelve como `(unit_id, start, end)` → `SegmentRef`.
- **Unidades acotadas** (solo para los experimentos): cada `ExtractionUnit.text` cumple los límites de A03 (≤ 8192 puntos de código y ≤ 24 576 bytes UTF-8). Sin segmentación productiva, sin agregación de puntuaciones entre unidades. Cada resultado A03 declara su **alcance local** (`scope_note`: «unidad N de M, no clasificación global del documento»).
- La cobertura de inferencia por unidad no suma ni promedia; una unidad `inconclusive` o con cobertura parcial deja el documento **no completo** (§9).

## 9. Qué puede afirmarse y qué no

| Afirmación | Condición mínima | No se deduce |
|---|---|---|
| «Todo el texto extraído fue evaluado» | todas las unidades con cobertura A03 `complete=true` por capacidad requerida | que se extrajo todo el contenido relevante |
| «El subconjunto textual evaluado fue extraído por completo» | `textual_subset_complete` (§2.1) | detección real, ausencia de residuos, ni cobertura de `N` |
| «Todo el contenido relevante fue extraído» | `document_coverage_complete` (§2.1): además ningún `N` ni porción desconocida | que fue evaluado ni que es localizable |
| «Todos los hallazgos seleccionados pueden transformarse» | cada selección tiene localización sin ambigüedad y precondiciones verificadas | que la transformación pasó verificación |
| «La transformación pasó las verificaciones previstas» | todos los checks de la aceptación | que no haya otros datos sensibles |
| «Documento protegido completo» | **todas** las anteriores | — (en esta etapa nunca se emite como conclusión de producto) |

**Ausencia de las ocurrencias seleccionadas ≠ ausencia global del valor.** La ausencia global solo puede exigirse cuando **todas** las ocurrencias del valor están incluidas en la transformación prevista. Sin OCR, las regiones de imagen o trazados capaces de contener texto quedan **fuera** de toda garantía de cobertura visual; la existencia de una capa de texto u OCR no declara cobertura visual completa.

## 10. Reglas generales de aceptación y rechazo del documento

| Condición | Regla | Motivo |
|---|---|---|
| Archivo cifrado (aunque la biblioteca pueda abrirlo, p. ej. contraseña de usuario vacía) | `R` | D-A04-02; abrirlo no equivale a poder transformarlo con garantías |
| Archivo firmado | `R` (no se eliminan firmas) | D-A04-02; la transformación invalidaría la firma |
| Corrupto o **reparado por el parser** (xref reconstruida, advertencias de reparación, relaciones rotas) | `R`; la reparación nunca es éxito normal | E-A04-15 |
| Contenido incrustado (OLE, paquetes, adjuntos) | `R` | D-A04-02 |
| Estructura desconocida o sin regla de cobertura | `R` | Si no puede cubrirse, no se asume |
| Escaneado (sin texto extraíble; página = imagen) | `R` (sin OCR no hay extracción) | B-A04-03 |
| Mixto (texto + imágenes/trazados capaces de contener texto) | procesable solo con las regiones de imagen/trazado **declaradas `N`**; `document_coverage_complete` nunca es verdadero | cobertura visual no verificable |
| Excede un límite del experimento (§16) | `R` («límite excedido») | sin truncamiento silencioso |
| Precondición no verificable | `R`/fallo | no se asume |

## 11. Matriz de soporte DOCX

Disposiciones: **S** cubierto, **E** eliminación autorizada, **P** estructural preservado e inspeccionado (§2.2), **N** no cubierto, **R** rechazo (§2). Columna «Evidencia» (valores: `demostrado` | `limitación observada` | `no probado (pendiente)`): resultados de los experimentos ejecutados; el identificador entre paréntesis es el fixture ([informe](a04-feasibility-report.md)). Experimentos: ver [A04 §6](features/A04-viabilidad-y-contrato-de-documentos.md#6-batería-de-experimentos). Aceptación previa de documento: no cifrado, sin firmas, sin macros, sin revisiones, ZIP/XML válidos sin reparación.

| ID | Elemento | Identificación / precondición | Extracción y localización | Transformación prevista | Conserva / elimina | Disp. | Rechazo y motivo | Exp. | Evidencia |
|---|---|---|---|---|---|---|---|---|---|
| DX-01 | Cuerpo, párrafos y runs | `w:body/w:p/w:r/w:t` | Texto por párrafo; run → pieza; valor entre runs ⇒ varias piezas | Sustituir texto de `w:t` afectados; ajustar `xml:space` | Conserva propiedades de run/párrafo del primer run afectado | S | — | EXP-01, 05 | demostrado (EXP-01/05/08/09; D01, D03, D04; el sustituto toma el formato del primer run del valor) |
| DX-02 | Tablas | `w:tbl/w:tr/w:tc` | Celda → unidad (o párrafos de celda); posición fila/columna | Como DX-01 dentro de la celda | Conserva estructura de tabla | S | — | EXP-01 | demostrado (D02) |
| DX-03 | Tablas anidadas | `w:tbl` dentro de `w:tc` (no visibles en `Document.tables`) | Recorrido XML recursivo, profundidad máx. 3 en experimento | Como DX-02 | Conserva anidación | S | Profundidad > límite ⇒ R | EXP-01 | demostrado hasta profundidad 2 (D02; profundidad 3 y rechazo por profundidad: no probado (pendiente)); `python-docx` `Document.tables` no las lista (limitación observada) |
| DX-04 | Celdas combinadas | `w:gridSpan`, `w:vMerge` | Texto contado una vez; celdas de continuación sin texto | Como DX-02; no se altera la rejilla | Conserva combinación | S | Rejilla inconsistente ⇒ R | EXP-01 | demostrado para `gridSpan`/`vMerge` (D02); rejilla inconsistente: no probado (pendiente) |
| DX-05 | Encabezados y pies y variantes (default/first/even; vinculados a la sección anterior) | Referencias `w:headerReference`/`w:footerReference` y `w:titlePg`, `w:evenAndOddHeaders` | Una unidad por parte referenciada, en el orden de §5 | Como DX-01 en cada parte | Conserva referencias y variantes | S | Parte de encabezado/pie **sin referencia** ⇒ R (contenido huérfano) | EXP-01 | demostrado para default/first/even de una sección con `titlePg` (D03; render LibreOffice); varias secciones y «vinculado al anterior»: no probado (pendiente); parte huérfana ⇒ R demostrado (R16) |
| DX-06 | Notas al pie / notas al final | `footnotes.xml`, `endnotes.xml`, `w:footnoteReference` | Una unidad por nota (excepto separadores) | Como DX-01 | Conserva referencias y numeración | S | Separadores personalizados con texto ⇒ R | EXP-01 | demostrado para nota al pie y final (D03); separadores con texto ⇒ R: no probado (pendiente) |
| DX-07 | Hipervínculos | `w:hyperlink`, relaciones externas | Texto visible: S. **Destino** (relación): no extraído | Texto visible como DX-01; destino no se modifica | Conserva destino; se **escanea** en la verificación de residuos | S (texto) / N (destino) | Destino que contiene un valor seleccionado ⇒ fallo de verificación | EXP-01, 08 | destino N y su copia detectada como residuo demostrado (D07, CN-05); texto del hipervínculo extraído demostrado; transformar texto dentro de `w:hyperlink`: no probado (pendiente) |
| DX-08 | Campos: resultado | `w:fldSimple`, `w:fldChar` begin/separate/end | Texto del resultado: extraído y mapeado (S, para que A03 lo evalúe) | **No se transforma**: sustituir el resultado visible no protege un valor que la instrucción pueda regenerar, y no se implementa un evaluador de campos | Conserva el campo | S (extracción) | Una selección dentro de un resultado de campo ⇒ fallo explícito (`field_result_not_transformable`); el documento no se declara transformado | EXP-01, 05 | demostrado: resultado extraído y mapeado; selección dentro del resultado rechazada (`field_result_not_transformable`, D05); sin evaluador de campos |
| DX-09 | Campos: instrucciones | `w:instrText`, `w:fldSimple/@w:instr` | No extraído | No se modifica | Conserva | N | Campos con referencia externa (`INCLUDETEXT`, `INCLUDEPICTURE`, `LINK`, `DDE`, `AUTOTEXT`…) ⇒ R | EXP-01, 07 | demostrado: instrucciones N (D05); campo `INCLUDETEXT` ⇒ R (R08); otros tipos externos: no probado (pendiente) |
| DX-10 | Comentarios | `comments.xml`, `commentsExtended`, `commentsIds`, `commentRangeStart/End`, `commentReference`, `people.xml` | Inventariados; texto no se entrega a A03 | **Eliminar** parte(s), anclajes y relaciones | Elimina clase «comentarios» (autor, fecha, texto); conserva el texto anotado; quita relaciones y `[Content_Types]` huérfanos | E | Respuestas/hilos desconocidos ⇒ R | EXP-06 | demostrado (D06; partes, anclajes, relaciones y tipos de contenido, sin residuos) |
| DX-11 | Revisiones | `w:ins`, `w:del`, `w:moveFrom/To`, `w:rPrChange`, `w:pPrChange`, `w:sectPrChange`, `w:tblPrChange` | — | — | No se aceptan ni rechazan cambios | R | D-A04-02: revisiones pendientes | EXP-07 | R demostrado para `w:ins`/`w:del` (R01); otros tipos de cambio: no probado (pendiente); `python-docx` omite su texto (limitación observada) |
| DX-12 | Texto oculto | `w:vanish` / `w:specVanish` | — | — | — | R | Contenido que no se ve podría ocultar un valor; sin regla de cobertura (B-A04-02) | EXP-07 | R demostrado (R02) |
| DX-13 | Cuadros de texto | `w:txbxContent`, `mc:AlternateContent` (Choice/Fallback), VML | — | — | — | R | Contenido duplicado en Choice/Fallback sin regla de cobertura (B-A04-02) | EXP-07 | R demostrado (R03) |
| DX-14 | Controles de contenido | `w:sdt` | Con contenido de texto simple: texto de `w:sdtContent` | Como DX-01 | Conserva el `w:sdtPr` | S (texto simple) | Con `w:dataBinding`/`customXml` o de tipo estructurado desconocido ⇒ R | EXP-01, 07 | demostrado para control de texto simple (D10); `dataBinding` ⇒ R demostrado (R18) |
| DX-15 | Propiedades del documento | `docProps/core.xml`, `app.xml`, `custom.xml` | Inventariadas (creador, última modificación, título, empresa, propiedades personalizadas) | **Eliminar/vaciar** campos descriptivos/personales | Elimina clases «autoría», «título/asunto/palabras clave», «empresa/administrador», «propiedades personalizadas»; **conserva** campos estructurales (versión de aplicación, estadísticas no descriptivas) | E | — | EXP-06 | demostrado para núcleo, aplicación y personalizadas (D06); las fechas se **conservan** (no se declararon eliminables) |
| DX-16 | Partes XML estructurales | `styles`, `numbering`, `settings`, `fontTable`, `theme`, `webSettings` | Inspección de cadenas/propiedades/relaciones (§2.2): lista de permitidas | No se modifican | Conserva | P | Cadena libre fuera de la lista ⇒ N; `settings` con `attachedTemplate` externo, `docVars` o payload ⇒ N/R | EXP-01, 08 | demostrado para `styles`, `settings`, `fontTable` (lista de permitidas); `docVars` ⇒ R (R19) y `lvlText` con texto libre ⇒ N (R20) demostrado; `theme`/`webSettings`: no probado (pendiente) |
| DX-17 | Partes personalizadas / glosario | `customXml/*`, `glossary/*` | — | — | — | R | Pueden alojar datos sin regla de cobertura | EXP-07 | R demostrado para `customXml` (R04); `glossary`: no probado (pendiente) |
| DX-18 | Miniatura | `docProps/thumbnail.*` | Inventariada | **Eliminar** | Elimina clase «miniatura»; quita relación y tipo de contenido | E | — | EXP-06 | demostrado (D06) |
| DX-19 | Imágenes (recurso ordinario) | `word/media/*`, `a:blip` | No hay OCR; el texto dentro de la imagen **no está cubierto** | No se modifican | Conserva; se verifica que sea una imagen y no un payload (tipo real) | N | Tipo real distinto del declarado ⇒ R | EXP-01, 07 | N demostrado (R21); tipo real distinto ⇒ R demostrado (R23) |
| DX-20 | Texto alternativo de imágenes y formas | `wp:docPr/@descr/@title` | No extraído | No se modifica | Conserva; escaneado en residuos | N | Contiene un valor seleccionado ⇒ fallo de verificación | EXP-08 | N y copia detectada demostrado (D07, CN-05) |
| DX-21 | Fuentes incrustadas (recurso ordinario) | `word/fonts/*` (ofuscadas) | Se valida tipo real y tamaño; no se extrae texto | No se modifican | Conserva | P | Tipo real distinto o fuente inválida ⇒ R | EXP-07 | no probado (pendiente) |
| DX-22 | Gráficos | `word/charts/*` (+ libro incrustado) | — | — | — | R | Incluye texto y datos; con libro incrustado es objeto incrustado | EXP-07 | R demostrado para gráfico (R06); libro incrustado por separado: no probado (pendiente) |
| DX-23 | Ecuaciones | `m:oMath`, `m:t` | — | — | Conserva | N | — | EXP-01 | N demostrado (R22) |
| DX-24 | SmartArt / diagramas | `word/diagrams/*` | — | — | — | R | Sin regla de cobertura | EXP-07 | regla implementada (`diagrams/`), sin fixture: no probado (pendiente) |
| DX-25 | Objetos incrustados (OLE, paquetes, adjuntos, ActiveX, macros `vbaProject.bin`) | `w:object`, `*/embeddings/*`, `.docm` | — | — | — | R | D-A04-02 | EXP-07 | R demostrado para OLE y macros (R05, R11); ActiveX: no probado (pendiente) |
| DX-26 | Referencias externas | `altChunk`, relaciones `TargetMode="External"` (plantilla, `INCLUDEPICTURE`, marcos) | — | — | — | R | Cargas externas no permitidas en el experimento | EXP-07 | R demostrado para relación externa (R07); `altChunk`: no probado (pendiente) |
| DX-27 | Cifrado (OLE `EncryptedPackage`) / firmas (`_xmlsignatures`) | Detección por contenido | — | — | — | R | D-A04-02 | EXP-07 | R demostrado para contenedor OLE (R09) y origen de firma (R10) **sintéticos**; un archivo cifrado/firmado real de Word: no probado (pendiente) |

## 12. Matriz de soporte PDF

Disposiciones como en §11. Evidencia: ver el [informe](a04-feasibility-report.md). El subconjunto objetivo para la primera viabilidad es acotado: PDF no cifrado, sin firmas, sin actualizaciones incrementales, con fuentes que exponen `ToUnicode`, texto latino con página sin rotar y rotada en múltiplos de 90°. **La transformación de texto en PDF es el punto de mayor riesgo técnico**: se decide con los experimentos, no se asume.

| ID | Elemento | Identificación / precondición | Extracción y localización | Transformación prevista | Conserva / elimina | Disp. | Rechazo y motivo | Exp. | Evidencia |
|---|---|---|---|---|---|---|---|---|---|
| PX-01 | Texto con fuente simple o Type0 con `ToUnicode` | Operadores de texto; fuente con mapeo a Unicode | Unidades por página; glifo→punto de código; geometría por carácter | Reescribir operandos de texto en el flujo de contenido (sustituto con glifos disponibles en la fuente) | Conserva páginas, geometría y contenido fuera de la zona | S (a confirmar) | Sustituto sin glifos / no cabe ⇒ fallo | EXP-02, 05 | demostrado para Type1 no incrustada y TrueType simple incrustada (subconjunto) con `ToUnicode`/WinAnsi (P01–P11); Type0/CID ⇒ fuera del subconjunto (limitación observada, Q15) |
| PX-02 | Fuentes y codificaciones sin `ToUnicode` fiable, CID sin mapeo, Type3 | Verificación del mapeo | — | — | — | R | Mapeo insuficiente (D-A04-03) | EXP-02, 07 | R demostrado (Q14, Q15) |
| PX-03 | Ligaduras | Un glifo → varios puntos de código | `relation=1:n`; seleccionar parte de una ligadura no es localizable | Sustituir el glifo completo solo si la selección lo abarca entero | Conserva | S (condicionado) | Selección parcial de ligadura ⇒ fallo (`localization_ambiguous`) | EXP-03 | demostrado: `n:1` representado; selección parcial rechazada (P04) |
| PX-04 | Texto rotado (rotación de página 90/180/270 y matriz de texto) | `/Rotate`, `Tm` | Geometría en coordenadas de visualización | Como PX-01 | Conserva rotación | S (a confirmar) | Ángulos no múltiplos de 90° o inclinación ⇒ N (extraído sin transformar) | EXP-02 | demostrado para `/Rotate` 90 y matrices de texto 90°/180° (P03); 270° y ángulos oblicuos: no probado (pendiente) |
| PX-05 | Múltiples columnas | Texto en bloques separados | Unidad = página; orden de flujo (no semántico); sin segmentación por columnas (§5) | Como PX-01 dentro de una misma cadena/`TJ` | Conserva | S (a confirmar) | Un hallazgo que une piezas de columnas/líneas distintas (concatenación artificial) ⇒ no transformable | EXP-02 | demostrado (P02): concatenación en orden de flujo; unión de líneas distintas rechazada (P06) |
| PX-06 | Contenido repetido y Form XObjects | `Do` sobre `/Subtype /Form`, mismo objeto en varias páginas | Un uso del objeto = un segmento por uso; la pieza apunta al objeto compartido | Modificar el objeto afecta todos los usos | — | S solo si **todos** los usos están seleccionados | Uso parcial de un objeto compartido ⇒ fallo (no se clona implícitamente) | EXP-02, 08 | demostrado (P05): uso parcial rechazado; todos los usos ⇒ transformable |
| PX-07 | Texto invisible (`Tr 3`, recortado, fuera de página, color de fondo) | Análisis de estado gráfico | Inventariado | — | — | R | Contenido que no se ve; sin regla de cobertura | EXP-07 | R demostrado para `Tr 3` (Q10); recortado/blanco/fuera de página: no probado (pendiente) |
| PX-08 | Imágenes (recurso ordinario) | `/Subtype /Image`, XObject o inline | No hay OCR: texto en imagen **no cubierto** | No se modifican | Conserva; declaradas por página | N | Imagen a página completa sin texto extraíble (escaneado) ⇒ R | EXP-02, 07 | N demostrado (Q12); página escaneada ⇒ R demostrado (Q11) |
| PX-09 | Texto convertido a trazados | No detectable de forma fiable | **No verificable** | — | — | N (limitación declarada, no verificada) | Nunca se afirma cobertura visual completa | EXP-02 | limitación observada: los trazados pintados se declaran N (Q17) pero **no se puede detectar** que representen texto |
| PX-10 | Metadatos | `/Info`, XMP (`/Metadata`) | Inventariados | **Eliminar/vaciar** campos descriptivos/personales | Elimina clases «autoría», «título/asunto/palabras clave», «productor/creador», «XMP descriptivo»; conserva campos estructurales (versión PDF, longitudes) | E | XMP con extensiones no reconocidas ⇒ R | EXP-06 | demostrado para `/Info` y XMP (P08) |
| PX-11 | Anotaciones de comentario (`Text`, `Popup`) | `/Annots` | Inventariadas | **Eliminar** (con su apariencia) | Elimina clase «comentarios»; limpia `/Annots`, `/Parent`, `/Popup` | E | — | EXP-06 | demostrado (P08) |
| PX-12 | Otras anotaciones y sus apariencias (`FreeText`, `Highlight`, `Stamp`, `Ink`, `Link`, etc.) | `/Annots` | `Link`: solo inventario | No se modifican | `Link` sin acción de riesgo: se conserva | N (`Link`) / R (resto) | La apariencia (`/AP`) puede contener texto sin cobertura | EXP-07 | `Link` N demostrado (P08); `FreeText` ⇒ R demostrado (Q13); apariencias y otros subtipos: no probado (pendiente) |
| PX-13 | Formularios, valores y apariencias | `/AcroForm`, `/Widget`, `/V`, `/DV`, `/AP`, XFA | — | — | — | R | Valor y apariencia pueden divergir; sin regla de cobertura (B-A04-04) | EXP-07 | R demostrado a nivel de catálogo (Q07); campos y widgets con valores/apariencias: no probado (pendiente) |
| PX-14 | Capas | `/OCProperties`, `/OC` | — | — | — | R | Contenido opcional oculto | EXP-07 | R demostrado (Q08) |
| PX-15 | Adjuntos | `/EmbeddedFiles`, `FileAttachment`, portfolio | — | — | — | R | Objeto incrustado (D-A04-02) | EXP-07 | R demostrado para árbol de nombres (Q06); anotación `FileAttachment`: no probado (pendiente) |
| PX-16 | Acciones | `/OpenAction`, `/AA`, `/A` (`JavaScript`, `Launch`, `SubmitForm`, `ImportData`, `GoToR`, `Rendition`) | — | — | — | R (las listadas) / N (`GoTo` interno y `URI`: preservadas e inspeccionadas) | Ejecutable o con referencia externa | EXP-07 | R demostrado para `OpenAction` JavaScript (Q05); otras acciones y `URI`/`GoTo`: no probado (pendiente) |
| PX-17 | Estructura etiquetada y texto alternativo | `/StructTreeRoot`, `/Alt`, `/ActualText`, `/E` | Inventariados | — | Estructura sin `/Alt`/`/ActualText`: se conserva | N | Con `/Alt` o `/ActualText`: R (duplican texto sin mapeo) | EXP-07, 08 | R demostrado para `/ActualText` (Q09); estructura etiquetada sin texto alternativo: no probado (pendiente) |
| PX-18 | Cifrado, firmas, actualizaciones incrementales, reparación del parser | `/Encrypt`, `/Sig`, `/ByteRange`, `DocMDP`, varios `%%EOF`/`/Prev`, avisos de reconstrucción | — | — | — | R | D-A04-02; revisiones previas conservarían el contenido «eliminado» (E-A04-16) | EXP-07 | demostrado (Q01 cifrado con contraseña de usuario vacía, Q02, Q03, Q04, Q20) |
| PX-19 | Fuentes (recurso ordinario) | `/FontDescriptor`, `FontFile*`, codificación | Se valida el diccionario de fuente y los glifos disponibles; no se extrae texto de ellas | Los glifos del sustituto deben estar disponibles en la fuente | Conserva | P | Falta del glifo del sustituto ⇒ fallo; programa de fuente inválido ⇒ R | EXP-02, 05 | demostrado: carácter no codificable (P07) y glifo ausente del subconjunto incrustado (P11) |
| PX-20 | Estructura del archivo: árbol de páginas, recursos, flujos comprimidos, object streams, linearización, PDF/A y PDF/UA | Lectura del archivo; inspección §2.2 | Se normaliza al leer | Se reescribe con la biblioteca estructural | Se **declara** la pérdida de linearización y de conformidad PDF/A/PDF/UA (no se promete preservar) | P | Diccionario con cadenas libres no permitidas ⇒ N; estructura desconocida ⇒ R | EXP-05, 08 | parcialmente demostrado: salida válida en PDFium, pypdf estricto y Poppler; entradas con object streams/`/Contents` en arreglo/linearizadas: no probado (pendiente) |

## 13. Comentarios y metadatos eliminables (D-A04-02)

- **Qué se elimina:** únicamente las clases listadas como `E` (DX-10, DX-15, DX-18, PX-10, PX-11). Cada eliminación se registra como **clase** (no contenido) en el informe; no se promete restaurarla. No se elimina nada clasificado `N`/`R`, ni cualquier otra anotación, formulario o contenido visible: **la autorización no se extiende por analogía**.
- **Qué debe permanecer válido:** DOCX — paquete OPC (relaciones sin destinos huérfanos, `[Content_Types].xml` coherente, referencias `commentReference` eliminadas junto con sus anclajes, `w:id` sin colisiones); PDF — `/Annots`, `/Info`, `/Metadata` y referencias cruzadas coherentes, tabla xref y trailer válidos.
- **Cómo se comprueba la ausencia de copias residuales:** (1) reapertura sin reparación; (2) recorrido estructural de **todas** las partes/objetos (no solo del cuerpo) buscando los valores sintéticos de comentarios/metadatos en claro, en UTF-16 y como entidades XML o secuencias PDF (`\ooo`, hexadecimales, `UTF-16BE` con BOM), incluidos flujos descomprimidos y object streams; (3) comprobación de partes/relaciones/objetos huérfanos; (4) comparación con el manifiesto independiente (no con la misma extracción). La búsqueda de bytes literales **no** es suficiente.

## 14. Reglas específicas por tipo de documento

- **Escaneados y mixtos:** ver §10; sin OCR, escaneado ⇒ `R`; mixto ⇒ solo con imágenes/trazados `N` y sin afirmar cobertura completa.
- **Cifrados:** `R` aunque la biblioteca los abra. **Firmados:** `R`; no se eliminan firmas.
- **Corruptos/reparados:** `R`; si la biblioteca repara en silencio, el experimento debe **detectar** la reparación (avisos del parser, comparación de la estructura declarada con la leída) o registrar la limitación de detección.
- **Contenido incrustado o estructuras desconocidas:** `R`.
- Para recursos ordinarios (fuentes, imágenes): regla propia en la matriz (`N` + comprobación de tipo real); su presencia no acredita inocuidad.

## 15. Bibliotecas candidatas, licencias y criterio de selección

Consulta de fuentes oficiales: **12 de noviembre de 2026**. «Versión» identifica solo lo que la página mostraba; **una página `latest`/`stable` no es una versión fijada**. Las licencias son las declaradas por el proyecto; la **licencia de los binarios y componentes transitivos debe registrarse al fijar versiones** (no basta la del wrapper Python). Lo siguiente es un punto de partida, **no** una selección.

| Rol | Candidata | Licencia declarada | Versión identificada en la consulta | Función | Limitaciones relevantes (según la documentación oficial) |
|---|---|---|---|---|---|
| Transformación DOCX | `python-docx` | MIT (archivo `LICENSE`, rama `master`) | Documentación de 1.2.0 (alias `latest`) | Acceso a párrafos, tablas, secciones, propiedades, comentarios y a la parte del documento | `Document.paragraphs` y `.tables` no incluyen contenido dentro de revisiones; `.tables` lista solo tablas de primer nivel (no anidadas). Requiere inspección OOXML adicional |
| Inspección DOCX | ZIP/XML de la biblioteca estándar (verificación independiente) y `lxml` (transformador) | `lxml` 6.1.3: BSD-3-Clause; la rueda enlaza libxml2 2.14.6 y libxslt 1.1.43 (MIT). **`lxml` es dependencia obligatoria de `python-docx` y de `pikepdf`**, no opcional | 6.1.3 | Recorrido de partes, relaciones, nodos | Parser endurecido en los experimentos (`resolve_entities=False`, sin red, sin DTD; cualquier `DOCTYPE` ⇒ rechazo) |
| Extracción, geometría y render PDF | `pypdfium2` / PDFium | pypdfium2: Apache-2.0 o BSD-3-Clause (a elección); PDFium: estilo BSD; los binarios incluidos pueden traer licencias de terceros (`BUILD_LICENSES/`) *[según la página del proyecto; resumen parcial]* | Página `stable` sin versión de paquete; ejemplos con PDFium 159.0.8138.0 | Texto con rectángulos y caracteres, objetos de página (con recursión en Form XObjects), render | PDFium no es seguro entre hilos; `get_text_range` limitado a UCS-2 según la documentación; **no se documenta redacción segura** y no se asume |
| Transformación estructural PDF | `pikepdf` / QPDF | pikepdf: MPL-2.0; las ruedas incluyen QPDF, libjpeg-turbo y otros componentes (ver `third-party-licenses/`); **MPL-2.0 no es una licencia permisiva equivalente a MIT/BSD** (D-A04-01) | no identificada | Lectura/escritura estructural, flujos de contenido (`parse_content_stream`, `TokenFilter`), cifrado, metadatos | No extrae texto (se recomienda otro extractor); los filtros no descienden a Form XObjects; quitar un clip cambia lo posterior; resultado debe verificarse |
| Extractor/verificador independiente PDF | `pypdf` | BSD de tres cláusulas (`LICENSE`, rama `main`) | no identificada | Extracción de texto (modos, layout), verificación adicional | Sin capa semántica: el orden de lectura no está garantizado; no hace OCR; extracción de contenidos grandes puede requerir mucha memoria |
| Alternativa técnica (no elegida) | PyMuPDF | AGPL o licencia comercial (la página indica contactar al agente comercial si no se cumplen los requisitos de AGPL) | Documentación de 2.1 (`latest`) | Redacciones nativas (`add_redact_annot`/`apply_redactions` documentadas con opciones para texto, imágenes y gráficos) | **No cumple D-A04-01 como vía inicial**: queda documentada como alternativa, no se incorpora ni se usa en el experimento principal |
| Renderizador | PDFium (vía pypdfium2) para PDF; para DOCX un renderizador a identificar (p. ej. LibreOffice) | PDFium: ver arriba; LibreOffice: MPL-2.0 (a confirmar al fijar versión) | no fijada | Renders antes/después | Los renders **no son idénticos** entre lectores (D-A04-03); se registran renderizador, versión y fuentes |
| Generador de fixtures | `python-docx` y/o escritura directa de OOXML; `pikepdf`/escritura de PDF a mano para PDF sintéticos | ver arriba | no fijada | Documentos sintéticos con estructura de referencia **independiente** | El generador no puede ser la fuente del oráculo de la extracción que se valida (§16) |
| Herramientas de inspección | `qpdf --check`/`--qdf`, `unzip -l`, visor XML, `pdfinfo`-equivalentes | a registrar | no fijada | Revisión estructural independiente | Su disponibilidad en macOS/Linux/Actions debe verificarse |

**Criterio de selección posterior:** (1) cumple D-A04-01 (sin licencia comercial obligatoria; obligaciones de MPL-2.0 cumplibles y documentadas); (2) pasa los experimentos con evidencia; (3) hay verificador independiente viable; (4) cuota de esfuerzo acotada (§EXP-10 de la especificación); (5) licencias transitivas y binarios registrados; (6) soporte de macOS y Linux (y Actions). Si ninguna vía lo satisface, se **informa la limitación** (no se introduce una dependencia comercial ni se reduce la garantía en silencio).

### 15.1 Versiones y licencias efectivas de los experimentos (observadas)

Fijadas en `experiments/a04/uv.lock` (entorno independiente del paquete). Fuente: metadatos instalados, archivos de licencia de las ruedas y SBOM de Pillow, registrados por `experiments/a04/licenses.py` (`outputs/environment-macos.json`, `outputs/environment-linux.json`). Versiones iguales en macOS (arm64, Python 3.13.16) y Linux (Debian 12 aarch64, Python 3.13.18).

| Componente | Versión efectiva | Licencia (declarada/observada) | Binarios y componentes transitivos observados | Rol |
|---|---|---|---|---|
| `python-docx` | 1.2.0 | MIT | requiere `lxml>=3.1`, `typing_extensions>=4.9` | generador-alternativa / lectura (no usado para editar) |
| `lxml` | 6.1.3 | BSD-3-Clause (más licencias de código derivado de ElementTree en `LICENSES.txt`) | libxml2 2.14.6 y libxslt 1.1.43 enlazadas en la rueda | transformador DOCX |
| `pikepdf` | 10.18.0 | **MPL-2.0** (no equivale a MIT/BSD: D-A04-01) | QPDF 12.4.2 (Apache-2.0) como biblioteca compartida; libjpeg-turbo (IJG + BSD-3-Clause + Zlib); requiere **`Pillow>=10.0.1`** y `lxml>=4.8`; datos: perfil sRGB (Zlib), reglas veraPDF (CC-BY-4.0), tabla pdfminer.six (MIT) | transformador PDF |
| `Pillow` | 13.0.0 | MIT-CMU | ruedas con FreeType (FTL), HarfBuzz (MIT), Little CMS (MIT), OpenJPEG (BSD-2), libtiff, libwebp (BSD-3), libavif (BSD-2), libpng, zlib-ng/zlib, liblzma, libxcb/libXau (X11), brotli; el SBOM de la rueda de macOS **menciona** FriBiDi (LGPL-2.1+) y libimagequant (GPL-3.0+) pero **no figuran entre las bibliotecas empaquetadas observadas** en las ruedas de macOS ni de Linux (listado de archivos): su presencia en otras plataformas/versiones debe revisarse al fijar el producto | dependencia obligatoria de `pikepdf`; render/comparación de píxeles en los experimentos |
| `pypdfium2` | 5.15.2 | Apache-2.0 o BSD-3-Clause (a elección; documentación CC-BY-4.0) | PDFium 159.0.8149.0 (`sourcebuild-toolchained`): licencias de `pdfium` (BSD-3), abseil/highway/dragonbox (Apache-2.0; Boost), AGG 2.3, FreeType (se elige FTL), HarfBuzz (Old MIT), ICU (Unicode License v3), Little CMS (MIT), OpenJPEG (BSD-2), libpng, libtiff, zlib, fast_float (MIT), simdutf (MIT), libjpeg-turbo (IJG/BSD) y, solo en variantes no-musl, llvm-libc; todos en `BUILD_LICENSES/` de la rueda | extracción independiente, geometría, render |
| `pypdf` | 7.0.1 | BSD-3-Clause | sin dependencias en Python ≥ 3.11 | verificador independiente |
| `fonttools` | 4.68.0 | MIT | — | herramienta (AGL, lectura de fuentes incrustadas, generación de P11) |
| Poppler (`pdftotext`, `pdfinfo`, `pdftoppm`) | macOS 26.10.0 (Homebrew); Linux 22.12.0-2+deb12u3 | GPL (herramienta externa) | — | **solo herramienta de verificación**; no se distribuye ni se enlaza |
| LibreOffice Writer | 7.4.7 (Debian 12, 4:7.4.7-1+deb12u14) | MPL-2.0/LGPL (herramienta externa) | — | **solo renderizador de DOCX en Linux**; no se distribuye |
| Fuentes | DejaVu Sans 2.37-6 (subconjunto incrustado en P11; licencia Bitstream Vera/DejaVu, permisiva); Liberation 1.07.4 (renderizado de DOCX en LibreOffice) | — | — | fixtures y renders |

**Separación producto/experimento:** si se eligiera esta vía para el producto, la cadena de dependencias de transformación sería `pikepdf` (MPL-2.0) → QPDF + `Pillow` + `lxml`; la verificación independiente (`pypdf`, `pypdfium2`, Poppler, LibreOffice) y `fonttools` son **herramientas exclusivamente experimentales** salvo decisión posterior. Esta comprobación **no fija la licencia definitiva del motor** (A10) ni autoriza dependencias: informa lo observado.

## 16. Límites conservadores para los experimentos (E-A04-20)

Elecciones técnicas de los experimentos, **no límites productivos** (A05 los definirá). Exceder uno ⇒ rechazo registrado, no truncamiento.

| Concepto | Límite |
|---|---|
| Tamaño de archivo de entrada | ≤ 2 MiB |
| Páginas PDF | ≤ 20 por documento |
| Partes ZIP de un DOCX | ≤ 100; profundidad de tablas anidadas ≤ 3 |
| Descompresión | tamaño total descomprimido ≤ 50 MiB y relación ≤ 100:1 por entrada (se comprueba **antes** de expandir) |
| Texto por unidad | ≤ límites de A03 (8192 puntos de código, 24 576 bytes) |
| Unidades por documento | ≤ 500; hallazgos seleccionados ≤ 100 |
| Duración por documento/experimento | ≤ 60 s por documento; ≤ 30 min por experimento |
| Memoria del proceso | ≤ 1 GiB (se aborta el experimento si se supera) |
| Ejecución | local; **sin red**; sin macros, scripts ni referencias externas; proceso aislado; directorio de trabajo descartable |

**Aplicación en los experimentos:** tamaño de archivo, partes ZIP, páginas, descompresión (sobre tamaños declarados **y sobre bytes realmente producidos**, con aborto), texto por unidad, unidades y selecciones se **hacen cumplir en código** (`a04/common.py`); la duración (≤ 60 s por documento) y la memoria (≤ 1 GiB) **no se hacen cumplir** sino que se **miden**: documento más lento < 1 s; pico de memoria del proceso 405,6 MB (macOS) y 256,3 MB (Linux), incluidas las bombas de 60 MiB; el experimento completo tardó 4,8 s (macOS) y 22,7 s (Linux).

## 17. Artefactos experimentales (implementados)

`experiments/a04/` (existe): `a04/` (generadores, herramientas y verificadores), `fixtures/` (documentos sintéticos + manifiestos independientes, con hash verificado al cargar), `outputs/` (resultados por entorno, registros de entorno, renders y muestras antes/después), `docker/`, `run_macos.sh`, `run_linux.sh`. Debe permanecer **fuera de `src/`** (la wheel solo empaqueta `src/cawbase_content_engine`) y **fuera del `include` del sdist** (que enumera directorios concretos), y sus bibliotecas están en un `pyproject.toml`/`uv.lock` **separados** del runtime. Se verificó con `uv build` que la wheel no contiene archivos de `experiments/` y que el sdist solo los incluía por los patrones sin ruta `pyproject.toml`/`uv.lock`; se agregó `exclude = ["experiments"]` a la sección sdist del `pyproject.toml` del motor (único cambio en ese archivo; sin cambios de versión, dependencias ni `uv.lock`) y el sdist resultante tiene 0 entradas de `experiments/`.

## 18. Fuera de alcance de este contrato

Trabajos, colas, cargas y endpoints documentales, almacenamiento, retención e idempotencia (A05); sustituciones productivas, criptografía, custodia y restauración autorizada (A06); protección/restauración completas de DOCX/PDF (A07/A08); modelo real, calidad, tokenizer y segmentación por modelo (A09); integración con CAWbase, infraestructura y publicación.

## 19. Restricciones observadas en los experimentos

Derivadas de la ejecución sobre **documentos sintéticos** ([informe](a04-feasibility-report.md)); **no** son decisiones de producto (ver `E-A04-28` a `E-A04-33` en [A04-decisions](decisions/A04-decisions.md)) y acotan las conclusiones.

1. **PDF — ancho del sustituto.** Para preservar la presentación fuera de la zona afectada se compensa el avance con un ajuste de `TJ`; por ello el sustituto **no puede ser más ancho** que el texto reemplazado (ancho de la fuente + ajustes internos): si lo es, fallo `replacement_overflow`. Un sustituto más corto deja un hueco visible. Un sustituto con un carácter sin glifo en la fuente (codificación o programa de fuente incrustado) falla con `substitute_glyph_unavailable`.
2. **PDF — subconjunto probado.** Fuentes simples (Type1 no incrustada con `/Widths`; TrueType simple incrustada) con código→Unicode fiable, operadores `Tj`/`TJ`/`'`/`"`, sin `Tc/Tw/Tz/Ts/TL`. **Type0/CID, Type3 y fuentes sin `ToUnicode` ni codificación estándar quedan fuera** (rechazo); no se evaluó su viabilidad.
3. **PDF — unidad y localización.** Unidad = página; un hallazgo que cruza un separador sintetizado (línea o columna distinta) es detectable pero **no transformable**; un objeto Form compartido solo se transforma si se seleccionan **todos** sus usos.
4. **DOCX — formato.** El sustituto adopta el formato del primer run del valor; en valores repartidos entre runs de formato distinto, el formato del resto del valor se pierde dentro del valor (la estructura y los estilos fuera del valor se conservan). Sustitutos largos provocan reflujo (admitido por D-A04-03).
5. **DOCX — campos.** Se extrae el resultado visible, pero **no se transforma** (sin evaluador de campos).
6. **Verificación.** La búsqueda de bytes literales **no basta** (valores partidos en `TJ`/runs); se exige extracción independiente con varios motores contra un oráculo del manifiesto, más búsqueda en todas las partes/objetos y codificaciones. Las copias auxiliares (destino de hipervínculo, texto alternativo, URI de enlace) **se detectan** pero no se eliminan: son `N` y quedan como residuo declarado.
7. **Restitución mecánica.** Posible con correspondencias locales sintéticas por **posición** validada; en PDF el kerning interno del valor original no se restituye (diferencias de píxeles solo dentro de los valores restituidos); lo eliminado por D-A04-02 (comentarios/metadatos) **no** se recupera.
8. **Lectores.** DOCX se renderizó solo con LibreOffice 7.4.7 (Linux); **no** se probó Word. PDF con PDFium y Poppler. No se promete identidad entre lectores (D-A04-03).
9. **Alcance de la evidencia.** Documentos generados por el propio equipo (estructura limpia). No se probaron archivos producidos por Word, Acrobat u otros generadores reales, PDF con object streams o `/Contents` en arreglo, DOCX con varias secciones, ni documentos grandes.
10. **Cadena de dependencias.** La vía `pikepdf` arrastra `Pillow` y `lxml` como dependencias obligatorias ([§15.1](#151-versiones-y-licencias-efectivas-de-los-experimentos-observadas)).
