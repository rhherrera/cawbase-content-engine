# Protección y restauración PDF — contrato de A08

> **Estado: PROPUESTO — revisión 1 (etapa 1 de A08), pendiente de la aprobación del usuario. Nada de este documento está implementado.** Es la **propuesta de contrato experimental**: fija lo que la comprobación D-A04-04 (etapa 2, [protocolo](a08-pdf-corpus-protocol.md)) debe probar y lo que la implementación (etapa 3) heredaría. Las capacidades marcadas **[C]** quedan **condicionadas a la evidencia** de la etapa 2: si la medición no las acredita, salen del alcance productivo o se rechazan, y el alcance resultante requiere la aprobación explícita del usuario antes de implementar. Decisiones: [A08-decisions](decisions/A08-decisions.md). Alcance y matriz: [especificación de A08](features/A08-proteccion-y-restauracion-pdf.md).

Relacionado: [procesamiento de documentos (A04)](document-processing.md) · [trabajos, archivos y ciclo de vida (A05)](jobs-files-and-lifecycle.md) · [protección y restauración de texto (A06)](text-protection.md) · [protección y restauración DOCX (A07)](docx-protection.md) · [seguridad y aislamiento](security-and-isolation.md) · [persistencia y migraciones](persistence-and-migrations.md).

**Convención.** **[D1]**–**[D6]** decisión aprobada por el usuario (`D-A08-01` a `06`); **[E-nn]** elección técnica (`E-A08-nn`); **[P-nn]** propuesta **pendiente** (`P-A08-nn`); **[H-nn]** hipótesis por verificar (`H-A08-nn`); **[C]** capacidad condicionada a evidencia. `A04 §n`, `A05 §n`, `A06 §n` y `A07 §n` apuntan a [document-processing.md](document-processing.md), [jobs-files-and-lifecycle.md](jobs-files-and-lifecycle.md), [text-protection.md](text-protection.md) y [docx-protection.md](docx-protection.md).

## 0. Resumen

A08 replica para PDF, **solo por API Python** [D6], el patrón implementado de A07: dos operaciones durables de A05 —`pdf_protection` y `pdf_restoration`— sobre un `document_analysis` PDF **vigente, autorizado y concluyente** (mock de A03) [D4], con los sustitutos de A06 [D2], limpieza de las clases `E` [D3], verificación independiente antes de publicar [D2, D3, D5] y un vínculo de restauración autenticado [D5]. Lo nuevo respecto de A07 es el **formato**: un extractor PDF versionado que amplía la matriz de A05 (Type0/CID, object streams, `/Contents` en arreglo, estado de texto) [D4], la **representación del sustituto en un área segura** con fuente original o candidata [D2], y la **matriz PDF** de contenido transformable, estructural, eliminable y rechazado [D3].

```text
document_analysis (A05, extractor PDF v2, mock A03)     contexto A06 (vigente, tipo fijo)
        │ resultado + SourceMap v2 + original                   │ correspondencias cifradas
        ▼                                                       ▼
submit_pdf_protection ─► worker: vincular (reextracción) ─► planificar ─► marcadores ─► transformar ─► verificar ─► publicar
                                                                                          └─► PDF protegido + vínculo
submit_pdf_restoration(derivative_id, file_id = copia exacta) ─► digest ─► posiciones ─► descifrar ─► restaurar ─► verificar ─► publicar
```

**No hace:** aceptar hallazgos o mapas del llamador; OCR ni aplanado como respaldo; procesar escaneados o mixtos con regiones fuera de cobertura; inferencia real (A09); HTTP; restaurar un PDF editado; recuperar lo redactado o eliminado; prometer identidad visual o byte a byte.

## 1. Fronteras y principios

Los de A07 §1 sin cambios (host autoriza y comprueba acceso al trabajo o conversación; documento no confiable procesado en el proceso aislado de A05 §10.5; sin LLM; la base nunca guarda valores en claro), más:

- **Fallo cerrado sin parciales.** Una selección que no puede representarse de forma segura y legible, un residuo, una diferencia fuera de zona o una cobertura no justificable hacen fallar **el documento completo** sin publicar [D2, D3].
- **Lo esperado nunca sale del transformador.** El verificador establece el texto, las posiciones y las zonas esperadas a partir del original reextraído, del análisis y de las selecciones; el transformador solo produce el candidato [D2, E-01].
- **Decisión en un instante** (A07 §10.2): hora de la base leída una vez tras adquirir todos los bloqueos.
- **D-33 intacta**: nada PDF ni criptográfico en el camino de importación base.

## 2. Glosario (además del de A07 §2)

| Término | Definición |
|---|---|
| **Extractor PDF v2** | Extractor de A08 con identidad propia (`cce-pdf-extract`, §4.1), distinto del extractor v1 de A05 |
| **Pieza PDF** | Tramo de códigos de **un** operando de cadena de **un** operador de texto, en **un** flujo, con su fuente, su relación código ↔ puntos de código y su geometría (§4.3) |
| **Zona segura** | Rectángulo de página, calculado del original **antes** de transformar, dentro del cual debe caber el sustituto (§7.2) |
| **Ocurrencia** | Posición de un sustituto emitido en el derivado: unidad, `[start,end)`, pieza(s), zona y parámetros de representación (§6) |
| **Fuente candidata** | Fuente OFL incrustada por el motor cuando la original no cubre el alfabeto de los sustitutos (§7.3) |
| **Uso** | Una ejecución concreta de un flujo: el de la página o el de un Form XObject invocado por `Do` en una cadena de invocaciones concreta |

## 3. Fuente de hallazgos y vinculación [D4]

Igual que A07 §3 (condiciones S1–S12, política de A06, unión determinista, precedencia de la redacción, colisión de marcadores), con estas diferencias:

| # | Cambio respecto de A07 |
|---|---|
| S6 | `format = pdf` |
| S7 | El extractor del análisis debe ser **el extractor PDF v2** vigente (`id`, `version`, `config_sha256`, §4.1). Un análisis PDF hecho con el extractor v1 de A05 (`cce-docs-extract` 1) ⇒ `analysis_source_stale` (§4.5) |
| S8 | Reextracción aislada con el extractor v2 que reproduce **byte a byte** el JSON canónico del `SourceMap` v2 publicado [H-01] |
| S12 | `pdf:protect` + `results:read` sobre el análisis fuente [D6] |

Los hallazgos se resuelven **por unidad (página)** y offsets en puntos de código del texto exacto; nunca por búsqueda de texto. La selección ambigua (pieza parcial de una ligadura, intervalo que cruza un separador de línea, piezas sin mapeo fiable) falla el documento (`localization_ambiguous` / `selection_crosses_boundary`).

## 4. Extractor PDF v2 y `SourceMap` versionado [D4]

### 4.1 Identidad propia por formato [E-02, P-A08-04]

El extractor vigente tiene **una sola identidad** para DOCX y PDF (`cce-docs-extract`, versión `1`, `config_sha256` sobre 14 límites que incluyen los de PDF; `jobs/ident.py`), y A07 la compara al proteger (S7) y al restaurar (T5). **Cambiarla invalidaría todos los análisis DOCX y todos los derivados DOCX** (Q-A08-03). Por eso:

- DOCX conserva **exactamente** su identidad actual (`cce-docs-extract`, `1`, mismo `config_sha256` calculado sobre las mismas 14 claves), aunque cambien límites nuevos de PDF.
- PDF adopta una identidad nueva: `cce-pdf-extract`, versión `1` del nuevo extractor, `config_sha256` sobre **sus** límites (los de PDF de A05 §12.1 más los nuevos de §13), y `document_structures = "document-structures/2"` para sus mapas.
- La identidad efectiva de cada análisis es la del extractor **del formato detectado**; `result.provenance.extractor` y `source_map.identity.extractor` siguen siendo un único objeto `{id, version, config_sha256}`.

### 4.2 Fijación en el snapshot y `requirements_key` [P-A08-04]

El formato se detecta en la fase `validation`, **después** de fijar el snapshot. Propuesta: los `document_analysis` nuevos fijan un **conjunto de extractores** `extractors: {docx: {…v1…}, pdf: {…v2…}}` y un `requirements_key` nuevo, de modo que ningún runtime anterior (`0.4.0`–`0.6.0`) reclame un trabajo nuevo y produzca con el extractor v1 un mapa PDF bajo un snapshot que fija v2. Para los trabajos **ya aceptados** por un runtime anterior (snapshot con un solo `extractor` v1, `requirements_key` heredado), el runtime de A08 conserva el extractor PDF v1 **congelado** y anuncia también la clave heredada, de modo que los termina con exactamente la configuración que fijaron (A05 §8.6). Alternativa (no recomendada): no reclamarlos, que terminen por runtimes anteriores o `executor_unavailable`.

### 4.3 Pieza PDF v2 (`document-structures/2`) [E-03, C]

```text
PieceRef (PDF, v2) {
  page_index,
  use { chain: [ {object_ref, kind: "page" | "form", invocation_operator_index} ] },   # cadena de usos desde la página
  stream { object_ref, array_index },          # flujo concreto dentro de /Contents (índice en el arreglo, 0 si es único)
  operator_index,                              # en el flujo lógico concatenado del uso
  operand_index,                               # elemento del arreglo de TJ (0 para Tj, ', ")
  code_start, code_end,                        # bytes dentro de la cadena de ese operando
  font { resource_name, object_ref, kind: "simple" | "type0", code_bytes: 1 | 2 },
  relation: "1:1" | "n:1" | "1:n",
  quad [8 números]                             # cuadrilátero del glifo en espacio de página, redondeo fijo
}
SegmentRef { unit_start, unit_end, kind: "original" | "synthesized_line" | "synthesized_space", pieces[] }
```

- **Separadores por geometría** [C]: `synthesized_line` (`\n`) cuando cambia la línea (desplazamiento de línea base mayor que la mitad de la altura del texto, cambio de dirección, cambio de bloque/Form) y `synthesized_space` (` `) cuando, **en la misma línea**, el hueco entre glifos consecutivos supera un umbral fijo y no hay un código de espacio. Un intervalo seleccionado puede **contener** `synthesized_space` (valores con palabras posicionadas por separado, p. ej. texto justificado) pero **nunca** `synthesized_line` (`selection_crosses_boundary`). A04/A05 sintetizaban `\n` en todo operador de posicionamiento; el cambio altera el texto de las unidades y por eso exige identidad nueva.
- **Determinismo**: el JSON canónico (números con precisión fija, claves ordenadas) es reproducible byte a byte en el mismo extractor y configuración [H-01].
- Los mapas DOCX siguen siendo `document-structures/1`, idénticos.

### 4.4 Lo que el extractor v2 cambia respecto de v1 (por decidir con evidencia)

Todo lo que la etapa 2 acredite de esta lista entra; lo que no, sigue rechazado: fuentes Type0 `Identity-H` con `CIDFontType2`/`CIDFontType0` y `ToUnicode` [C]; fuentes simples con `/Differences` y `ToUnicode` [C]; object streams y xref streams [C]; `/Contents` en arreglo [C]; `Tc`, `Tw`, `Tz`, `Ts`, `TL`, `Tr 1/2` [C]; orientación por la **matriz combinada** de render (texto × CTM) en lugar de exigir escalas positivas en `cm` y `Tm` por separado [C]; `/Rotate 270` [C]; detección **estructural** (no por búsqueda de bytes) de actualizaciones incrementales, acciones y adjuntos, para no rechazar por un `/Prev` de un esquema de marcadores o un `/OpenAction` de destino `GoTo` [C, H-02].

### 4.5 Compatibilidad con trabajos, mapas y derivados anteriores [D4]

| Objeto anterior | Tratamiento en A08 |
|---|---|
| `document_analysis` **DOCX** de `0.4.0`–`0.6.0` | Sin cambios: identidad v1 intacta; A07 los sigue protegiendo |
| Derivados **DOCX** de `0.6.0` y su vínculo | Sin cambios: T5 de A07 compara el extractor DOCX v1, que no cambia |
| `document_analysis` **PDF** de `0.4.0`–`0.6.0` (extractor v1) | Siguen legibles por los lectores genéricos tal como se publicaron (sin reinterpretar offsets ni mapas). **No** pueden alimentar `pdf_protection`: `analysis_source_stale`. Cómo obtener uno válido: un nuevo `submit_document_analysis` sobre el mismo `file_id` (si sigue `stored` y vigente) o sobre una nueva carga del archivo; el runtime de A08 lo analiza con el extractor v2 |
| Trabajos en cola aceptados por runtimes anteriores | §4.2 |
| Mapas v2 frente a runtimes anteriores | Los lectores genéricos de `0.4.0`–`0.6.0` sirven el artefacto `source_map` tal cual (JSON); un host que lea `document-structures/2` debe conocer la nueva forma de la pieza. Se documenta como cambio de contrato para hosts [P-A08-04] |

Nunca se reinterpreta un offset ni un mapa anterior con otra versión del extractor.

## 5. Matriz PDF [D3]

### 5.1 Disposiciones

Las de A04 §2 (`S`, `E`, `P`, `N`, `R`) con una restricción nueva de **D3**: un `N` que pueda portar contenido visual sin cobertura (imagen, trazado no ordinario, patrón) **ya no se admite** como «no cubierto declarado»: el documento se **rechaza** (`uncovered_content`) salvo que cumpla una regla verificable de recurso decorativo u ordinario (§5.4). Siguen siendo `N` admitidos solo los que se **inspeccionan** como cadenas (destinos de enlaces, títulos del esquema, nombres de destinos, etiquetas de página, cadenas de estructura sin `/Alt`/`/ActualText`): un valor seleccionado en ellos ⇒ `residue_detected`.

### 5.2 Matriz

Columnas: **PT** (fila A08) · **PX** (fila A04 §12) · **A05** (disposición en A05 §11.2) · **A08** · **Condición** · **Fallo/rechazo**.

| PT | PX | A05 | A08 | Condición | Fallo/rechazo |
|---|---|---|---|---|---|
| PT-01 Texto con fuente simple (Type1, TrueType, MMType1) y `ToUnicode` o codificación estándar | PX-01 | S | **Transforma** | mapeo fiable de cada código usado en la página | `font_mapping_insufficient` |
| PT-02 Simple con `/Differences` + `ToUnicode`; Type1C (`FontFile3`) | PX-01 | R | **Transforma** [C] | ídem | ídem |
| PT-03 Type0 `Identity-H` + `CIDFontType2` subconjuntada + `ToUnicode` | PX-01 | R | **Transforma** [C] | códigos de 2 bytes; `ToUnicode` completo para los códigos usados | ídem |
| PT-04 Type0 `Identity-H` + `CIDFontType0` (CFF) | PX-01 | R | **Transforma** [C] | ídem | ídem |
| PT-05 Type0 con otro CMap (predefinido o incrustado), `Identity-V`/vertical, Type3, sin `ToUnicode` fiable, códigos a `U+FFFD`/vacío | PX-02 | R | **Rechazo** | — | `font_mapping_insufficient` |
| PT-06 Estado de texto `Tc`, `Tw`, `Tz`, `Ts`, `TL`, tamaños, `Tr 0/1/2` | — | R | **Transforma** [C] conservando el estado fuera del sustituto | `Tw` solo sobre el código 32 de un byte (no en Type0 de 2 bytes) | `unsupported_text_state` |
| PT-07 `Tr 3` (invisible), `Tr 4–7` (recorte), texto fuera de la `CropBox`, recortado por un trazado de recorte, **cubierto** por un relleno opaco pintado después | PX-07 | R (parcial) | **Rechazo** | detección por estado y por geometría de pintado posterior [C para la detección de cubrimiento] | `hidden_text` / `unsupported_text_state` |
| PT-08 Orientación: matriz combinada con rotación 0/90/180/270 y escala positiva (incluidas inversiones dobles tipo Skia) | PX-04 | parcial | **Transforma** [C] | determinante positivo, sin inclinación | `unsupported_structure` |
| PT-09 `/Rotate` 0/90/180/270 | PX-04 | 0/90/180 | **Transforma** (270 [C]) | — | — |
| PT-10 Varias columnas, tablas, texto justificado (palabras posicionadas por separado) | PX-05 | S | **Transforma** por posición; `synthesized_space` seleccionable dentro de una línea [C] | sin `synthesized_line` en la selección | `selection_crosses_boundary` |
| PT-11 Ligaduras (`n:1`) | PX-03 | S | **Transforma** si la selección abarca la ligadura entera | — | `localization_ambiguous` |
| PT-12 Valor partido entre líneas (corte con guion o ajuste) | — | — | **No transforma** | — | `selection_crosses_boundary` |
| PT-13 Escritura de derecha a izquierda en la selección | — | — | **No transforma** | — | `selection_not_transformable` |
| PT-14 `/Contents` en arreglo | PX-20 | R | **Transforma** [C] | cada operador con sus operandos en un solo flujo | `unsupported_structure` (operador partido) |
| PT-15 Object streams y xref streams | PX-20 | R | **Transforma** [C]; la salida se escribe **sin** object streams (estructura normalizada declarada) | — | — |
| PT-16 Linealización, PDF/A, PDF/UA | PX-20 | R / P | **Transforma** [C] declarando la **pérdida** de linealización y de conformidad | — | — |
| PT-17 Actualizaciones incrementales, xref reparada, cifrado, firmas, `DocMDP`/`/Perms` | PX-18 | R | **Rechazo** (detección estructural) | — | `incremental_update` / `parser_repair` / `encrypted` / `signed` |
| PT-18 Form XObject de uso único | PX-06 | S | **Transforma** el objeto | — | — |
| PT-19 Form XObject compartido, todos los usos con la misma selección | PX-06 | S | **Transforma** el objeto (afecta a todos los usos, todos seleccionados) | igualdad de selección en todos los usos | `shared_object_partial` |
| PT-20 Form XObject compartido, selección en parte de los usos | PX-06 | S (fallo al transformar) | **Clona** el objeto para los usos afectados [C, P-A08-05]: los `Do` de esos usos pasan a la copia; el verificador prueba que los usos no afectados siguen apuntando al objeto original **idéntico** | todos los usos enumerados desde páginas; ningún uso desde apariencias, patrones o máscaras | `shared_object_partial` |
| PT-21 Flujo de contenido compartido entre páginas | — | — | Igual que PT-19/PT-20 | — | `shared_object_partial` |
| PT-22 Contenido marcado (`BDC`/`EMC`, `MCID`) de PDF etiquetado | — | (no listado) | **Transforma** conservando el equilibrio y los `MCID`; el sustituto queda en la secuencia del primer carácter | propiedades sin `/ActualText`, `/Alt`, `/E` | `alternate_text_duplication` |
| PT-23 Árbol de estructura sin `/Alt`/`/ActualText`/`/E` | PX-17 | N | **Conserva**; cadenas (`/T`, `/Lang`) inspeccionadas | — | residuo ⇒ `residue_detected` |
| PT-24 `/Alt`, `/ActualText`, `/E` en estructura o contenido marcado | PX-17 | R | **Rechazo** | — | `alternate_text_duplication` |
| PT-25 Metadatos `/Info` | PX-10 | E (no analizado) | **Elimina** las claves descriptivas y de autoría (`/Title`, `/Author`, `/Subject`, `/Keywords`, `/Creator`, `/Producer` y toda clave no listada como conservada); **conserva** `/CreationDate` y `/ModDate` | — | — |
| PT-26 XMP del documento y XMP de objetos (`/Metadata`) | PX-10 | E | **Elimina** los flujos XMP completos y sus referencias [P-A08-06] | — | — |
| PT-27 Anotaciones de comentario `Text`, `Popup` (y sus respuestas `IRT`) | PX-11 | E | **Elimina** con apariencia y referencias (`/Annots`, `/Popup`, `/Parent`, `/IRT`) | ninguna otra anotación las referencia | `unsupported_structure` |
| PT-28 Anotaciones `Link` con `/URI` o destino interno (`GoTo`, destino con nombre) | PX-12 | N | **Conserva**; destino inspeccionado (también decodificado URL) | sin acción de riesgo | residuo ⇒ `residue_detected` |
| PT-29 Otras anotaciones (`FreeText`, `Highlight`, `Stamp`, `Ink`, `Widget`, `FileAttachment`, …) | PX-12 | R | **Rechazo** | — | `unsupported_structure` / `embedded_content` |
| PT-30 Esquema (`/Outlines`), destinos con nombre (`/Dests`, `/Names /Dests`), etiquetas de página (`/PageLabels`), `/OpenAction` de destino `GoTo` | PX-16, PX-20 | R (por búsqueda de bytes en v1) | **Conserva**; cadenas inspeccionadas | — | residuo ⇒ `residue_detected` |
| PT-31 Formularios, XFA, capas, adjuntos, acciones ejecutables o externas | PX-13 a PX-16 | R | **Rechazo** | — | `forms_or_layers` / `embedded_content` / `executable_action` |
| PT-32 Imágenes (XObject o en línea) | PX-08 | N | **Admite** solo las **decorativas verificables** (§5.4); el resto ⇒ **rechazo** del documento [D3] | §5.4 | `uncovered_content` |
| PT-33 Página escaneada; imagen escaneada **con capa de texto** | PX-08 | R / N | **Rechazo** (sin OCR ni aplanado) | — | `scanned_without_text` / `uncovered_content` |
| PT-34 Trazados | PX-09 | N | **Admite** solo los **ordinarios verificables** (§5.4); el resto ⇒ **rechazo** | §5.4 | `uncovered_content` |
| PT-35 Patrones de mosaico, sombreados (`sh`), grupos de transparencia, máscaras suaves | — | (no listado) | Sombreado sin texto ⇒ **conserva** (`P`); patrón o grupo **con** operadores de texto, imagen o trazado no ordinario ⇒ **rechazo** [C] | — | `uncovered_content` |
| PT-36 Fuentes (recursos) | PX-19 | P | **Conserva byte a byte** (también la original de un valor sustituido, necesaria para restaurar); programas de fuente escaneados en residuos (bytes crudos) | — | programa inválido ⇒ `font_mapping_insufficient` |
| PT-37 Datos privados de aplicación (`/PieceInfo`, `/AIPrivateData`, claves desconocidas con cadenas libres) | PX-20 | R | **Rechazo** si contienen *payload*; cadenas libres ⇒ inspeccionadas | — | `unsupported_structure` |
| PT-38 Árbol de páginas, recursos, `MediaBox`/`CropBox`/`BleedBox`/`TrimBox`/`ArtBox`, `UserUnit` | PX-20 | P | **Conserva**; la geometría es parte de la verificación | — | — |

### 5.3 Clases auxiliares eliminadas y registro [D3]

Toda protección elimina las clases `E` presentes —`document_info_descriptive`, `xmp_metadata`, `object_metadata`, `comments` (anotaciones `Text`/`Popup`)— y registra en el informe las **efectivamente eliminadas** (clase y cantidad, nunca contenido). Ninguna otra cosa se elimina: la autorización no se extiende por analogía (A04 §13). Esquema, destinos, enlaces y estructura se **conservan e inspeccionan**. Tras restaurar, las clases eliminadas siguen ausentes.

### 5.4 Recursos decorativos y trazados ordinarios: reglas verificables [D3, C]

Sin OCR, una imagen o un trazado pueden contener texto que nadie evaluó. Se admiten **solo** si cumplen una regla comprobable sobre el archivo; si no, el documento se rechaza (`uncovered_content`):

| Recurso | Regla de admisión (candidata; se fija antes de medir) |
|---|---|
| Imagen | (a) **uniforme**: todos los píxeles decodificados del mismo color; o (b) **diminuta**: ≤ 16 × 16 píxeles de muestra **y** ≤ 6 × 6 pt en la página. En ambos casos no puede quedar bajo ni sobre texto de otra unidad de forma que lo oculte |
| Trazado | (a) solo segmentos rectos (`m`, `l`, `h`) y rectángulos (`re`), con ≤ 8 segmentos por subtrayecto; o (b) con curvas (`c`, `v`, `y`) si su caja es ≤ 6 × 6 pt (viñetas). Un relleno opaco pintado **después** de un glifo que lo cubre ⇒ `hidden_text` (PT-07) |
| Lista del host [P-A08-03] | Imagen cuyo SHA-256 de datos decodificados figura en una lista de recursos decorativos **declarada por el host** en la configuración (p. ej. su logotipo). El motor verifica el digest; la cobertura del contenido de la imagen es **declaración del host**, no evidencia del motor, y se registra como tal |

Imágenes y trazados admitidos se **conservan byte a byte** y se informan en `inspection_scope` como «admitidos por regla» (con la regla), nunca como «cubiertos».

### 5.5 Motivos cerrados

Rechazo de archivo (`file_rejected`, A05 §10.4): los existentes más **`uncovered_content`** (nuevo: imagen, trazado, patrón o capa de texto sobre imagen fuera de las reglas de §5.4). Fallos del trabajo: los de A07 §8.5 aplicables, más `replacement_overflow`, `substitute_glyph_unavailable`, `substitute_illegible`, `shared_object_partial`, `localization_ambiguous` y `restoration_encoding_unavailable` (nuevos, §7–§9). Mensajes genéricos, sin texto, valores, marcadores, nombres de fuente ni posiciones.

## 6. Identidad de ocurrencias [D5]

Cada sustituto **de seudonimización** emitido se registra en el vínculo como una ocurrencia:

```text
Occurrence { unit_id, start, end,                        # en el texto de la unidad del DERIVADO (extractor v2)
             page_index, use_chain, stream {object_ref, array_index}, operator_index, operand_index, code_start, code_end,
             zone { x0, y0, x1, y1 },                     # zona segura en espacio de página (§7.2)
             render { font: "original" | "candidate", font_resource, size, h_scale, zone_kind: "Z1" | "Z2", suffix_scale | null },
             original_advance,                            # avance del valor original en unidades de texto (§9.2)
             token }
```

- **Sin valores**, tipos ni fragmentos. `original_advance` y `zone` permiten restaurar sin el original; revelan **aproximadamente la longitud** del valor, como el propio derivado (la compresión del sustituto la deja inferir): limitación declarada [P-A08-07].
- Las redacciones no se registran como ocurrencias (no se restauran); sus zonas sí se registran en el informe (sin valores) para la verificación.
- **Ocurrencia idéntica no seleccionada**: se conserva y se comprueba **por posición** (pieza exacta del original y del derivado); es la única exención de residuo (A07 §6.3.1 trasladado a piezas PDF).

## 7. Representación del sustituto [D2]

### 7.1 Operación local sobre el operador

La transformación es **local al operador de texto** y no altera la matriz de línea (`Tlm`) ni nada fuera del operador [E-04, C]: el operando se parte en prefijo, valor y sufijo; los códigos del valor **se eliminan** del operando (no se tapan); se inserta, dentro del mismo objeto de texto, el sustituto con su fuente y su compresión (`Tf`, `Tz`), se restituye el estado previo (`Tf`, `Tz`, `Tc`, `Tw`, `Ts`, `Tr`) y un ajuste numérico de `TJ` lleva el punto actual a la posición original del sufijo. Así la posición de todo lo posterior es la misma que en el original (en `Z1`). Varias selecciones se aplican de derecha a izquierda por operando.

### 7.2 Zona segura

Se calcula del original reextraído y de las cajas de sus glifos, **antes** de transformar, con las reglas del [protocolo §7](a08-pdf-corpus-protocol.md#7-zonas-seguras-y-máscaras): `Z1` = valor + espacio libre a su derecha en su línea y bloque; `Z2` (**solo si se aprueba P-A08-01**) = valor + resto de la línea con compresión uniforme del sufijo. El sustituto debe quedar dentro de la zona y fuera de la zona nada cambia (texto, posiciones, render).

### 7.3 Fuente y legibilidad

- **Fuente original** si su programa y su codificación cubren todo el alfabeto `Σ` de los sustitutos (35 caracteres; protocolo §7.3), con correspondencia inversa única; si no, **fuente candidata** OFL incrustada como fuente simple `TrueType` subconjuntada con `WinAnsiEncoding` y `ToUnicode` (los sustitutos son ASCII) [E-05]. La regla depende del alfabeto y no del token aleatorio.
- **Mismo tamaño y línea base** que el primer carácter del valor; mismo color y modo de render; compresión horizontal `s ∈ [0,60; 1,00]`.
- Si no cabe con `s ≥ 0,60` ⇒ `replacement_overflow`; glifo ausente ⇒ `substitute_glyph_unavailable`; cualquier otra condición de ilegibilidad verificada (p. ej. altura renderizada bajo el mínimo) ⇒ `substitute_illegible`. El **documento falla** [D2].
- **Gramática**: la extracción del sustituto por el extractor v2 **y** por el verificador debe ser exactamente el marcador (clase S de A06 §6.2, sin espacios insertados) o `[REDACTED]`.

## 8. Estado gráfico y de texto, Form XObjects y límites de flujo

- **Estado**: el intérprete del extractor y del transformador sigue `q`/`Q`, `cm`, `Tm`, `Td`, `TD`, `T*`, `Tc`, `Tw`, `Tz`, `TL`, `Ts`, `Tf`, `Tr`, color y recorte, también a través de `Do`. El sustituto se dibuja con el estado efectivo en ese punto salvo `Tf`/`Tz` (y `Tc`/`Tw` a 0 para el sustituto), que se restituyen después.
- **Form XObjects**: un uso = una cadena de invocación desde una página; el planificador enumera **todos** los usos de cada objeto (páginas, otros Form XObjects, apariencias, patrones, máscaras). Modificar un objeto exige que todos sus usos tengan la misma selección (PT-19); si no, clonado verificado (PT-20 [C]) o `shared_object_partial`. Lo mismo para flujos de contenido compartidos.
- **Límites de flujo y objeto**: en un `/Contents` en arreglo, los flujos se concatenan lógicamente para interpretar, pero cada pieza conserva su flujo; solo se reescriben los flujos afectados. Un operador cuyos operandos y operador quedan en flujos distintos ⇒ `unsupported_structure`. Los objetos modificados se reescriben como objetos nuevos; los sustituidos no deben quedar en el archivo (VS del protocolo).

## 9. Restauración del derivado exacto [D5]

### 9.1 Entrada, autorización y comprobaciones

Idénticas a A07 §7.1–§7.2 (copia exacta del host como archivo de A05; T1–T6: vínculo `active` del scope y del contexto pedidos, digest y tamaño **antes** de analizar, contexto vigente y scope activo, `binding_mac`, marcadores **exactamente** en las ocurrencias del vínculo con el extractor v2 del vínculo, descifrado autenticado), con `pdf:restore`. No necesita el original, el análisis ni el trabajo de protección; no llama a la inferencia.

### 9.2 Aplicación en una pasada [C]

Por ocurrencia: se eliminan los códigos del marcador, se codifica el **valor Unicode exacto** con la **fuente original** del valor (conservada en el derivado) mediante su correspondencia inversa (la más larga primero, para las ligaduras) y se dibuja en la misma posición, con su tamaño y con la compresión necesaria para ocupar `original_advance`; el ajuste de `TJ` vuelve a llevar el sufijo a su posición; en `Z2` se restituye la escala original del sufijo. Si un carácter no tiene código **único** en esa fuente ⇒ `restoration_encoding_unavailable` (fallo cerrado; la frecuencia se mide en la etapa 2) [C]. Se admite variación del **kerning interno** del valor restaurado [D5]. `[REDACTED]` y las clases `E` permanecen; nada eliminado reaparece.

### 9.3 Verificación del restaurado

Número y geometría de páginas iguales; fuera de las zonas de las ocurrencias, texto, posiciones y render iguales al derivado; en cada zona, el valor exacto (extractor v2 y verificador); sin marcadores restantes; clases `E` ausentes. Fallo ⇒ `derivative_verification_failed`, sin publicar.

## 10. Verificación antes de publicar [D2, D3]

En producción, en un proceso aislado distinto del transformador y con un analizador PDF **distinto** de `pikepdf`/QPDF [E-06, Q-A08-08]: (1) apertura sin reparación; (2) geometría de páginas; (3) **oráculo por posición** — el texto de cada unidad del derivado es el original con cada selección sustituida, y fuera de las zonas los caracteres y sus cajas no cambian; (4) contenido de cada zona; (5) desbordamiento; (6) recorrido de **todos** los objetos alcanzables y flujos decodificados, operandos de texto decodificados con su fuente y con todas las fuentes, y residuos en todas las representaciones de A07 §6.3 más las propias del PDF (UTF-16BE, PDFDocEncoding, nombres `#xx`, decodificación URL, entidades XMP), con atribución de la ocurrencia permitida por pieza exacta; (7) objetos no alcanzables ⇒ fallo; (8) clases `E` ausentes. La comparación de render (PDFium/Poppler) es evidencia de **aceptación** (protocolo §9), no un control de ejecución, salvo que la etapa 2 acredite un renderizador de runtime compatible con D-A04-01 [Q-A08-08].

## 11. API, permisos, lectores y retención [D6]

Patrón de A07 §8–§9 y §11 trasladado sin cambios de garantía (propuesto / no disponible):

```python
# Propuesto (A08, no disponible). Métodos nuevos de ScopedOperations; rechazan HostTransaction.
async def submit_pdf_protection(self, scope_id, *, analysis_job_id, policy, context: ExistingContext | NewContext, idempotency_key) -> PdfProtectionAccepted
async def get_pdf_protection_result(self, scope_id, job_id) -> PdfProtectionResult      # pdf:protected:read
async def open_pdf_protected(self, scope_id, job_id) -> AsyncIterator[bytes]           # pdf:protected:read
async def get_pdf_derivative(self, scope_id, derivative_id) -> PdfDerivativeInfo        # pdf:protected:read
async def submit_pdf_restoration(self, scope_id, *, derivative_id, file_id, context_id, kind, idempotency_key) -> JobAccepted   # pdf:restore
async def get_pdf_restoration_result(self, scope_id, job_id) -> PdfRestorationResult    # pdf:restored:read
async def open_pdf_restored(self, scope_id, job_id) -> AsyncIterator[bytes]            # pdf:restored:read
```

- **Permisos** (catálogo 25 → **29**): `pdf:protect` (+ `results:read` sobre el análisis), `pdf:restore`, `pdf:protected:read`, `pdf:restored:read`. Descargar el original sigue exigiendo `files:read`; cargar la copia a restaurar, `files:write`. Restricción de despliegue heredada y ampliada.
- **Artefactos**: `pdf_protected`, `pdf_protection_report`, `pdf_restored`, `pdf_restoration_report`; los lectores genéricos (`get_job_result`, `open_job_artifact`, rutas HTTP de resultados, `open_file_content`) **no** los sirven (comprobación por operación, A07 §9.2).
- **Lectura del restaurado y su informe**: `pdf:restored:read`, resultado vigente, contexto vigente y scope activo en **un** instante de autorización tomado **después** de verificar el objeto en el spool e inmediatamente antes de entregar el *stream* (A07 §9.3).
- **`delete_job_data`** conserva el vínculo; **`delete_context`** termina la restaurabilidad de todos los derivados del contexto; sin revocación individual. Solo `pdf_restored` cuyo descarte esté confirmado es elegible para **purga inmediata** con la limpieza de A05 (A07 §11.1 extendido al rol `pdf_restored`).
- **`restore_text`** de A06 admite marcadores emitidos en un PDF con **todas** las condiciones de A06 §7 (marcador exacto entregado al modelo, acceso del host a la conversación o trabajo, contexto vigente, scope activo, datos nuevos evaluados); no produce ni da acceso a ningún PDF.
- **Retención**: D-A05-01 (artefactos 7 d); vínculo hasta el fin del contexto; limpieza propia de vínculos PDF (`run_pdf_cleanup`, propuesta) análoga a `run_docx_cleanup`.

## 12. Ejecución durable y reutilización [D6]

| Pieza | Reutilización |
|---|---|
| Reclamación, lease, *fencing*, reintentos, idempotencia, cuotas, fence del scope, storage verificado, limpieza | A05 sin cambios |
| Proceso aislado | Modos nuevos del hijo de A05 (`pdf_plan`, `pdf_protect`, `pdf_restore`, `pdf_verify`) con el mismo perfil de aislamiento, canario y límites |
| Publicación atómica | `JobWorker.publish(..., hook)` con un `PublishHook` propio (`share_jobs`, `lock`, `decide`, `after`) que inserta el vínculo `active` con `binding_mac` **en la misma transacción** que publica el conjunto exacto `{pdf_protected, pdf_protection_report}` (o `{pdf_restored, pdf_restoration_report}`) |
| Instante de decisión | Hora de la base leída una vez tras todos los bloqueos; posesión, vigencias (análisis, entrada, contexto), scope y vínculo comprobados juntos |
| Unión, política, marcadores, contextos, cifrado, verificación de reutilización | Funciones de A06 extraídas para A07 (`obtain_tokens`, `decrypt_tokens`, `insert_context`, `context_index_key`, `protection/grouping.py`, `protection/markers.py`), sin copiarlas |
| Vínculo | `binding_mac = HMAC-SHA256(clave de índice del contexto, "cce.pdf.binding.v1" ‖ JSON canónico de {scope_id, derivative_id, context_id, context_kind, sha256, size_bytes, extractor, occurrences})` |
| Lectores y runtimes anteriores | `requirements_key` de A08 incluye extractor v2 y transformador PDF (ningún runtime anterior lo anuncia); lectores genéricos con guarda por operación |

## 13. Persistencia, versión y límites (propuestos para la etapa 3)

- Tabla nueva **`pdf_derivatives`** (forma de `docx_derivatives`, con `occurrences` de §6), migración **`0006_pdf_protection`**, `schema_level` 5 → **6**, `min_runtime_level` 1 (a justificar como en A07 §12.2), versión **`0.7.0`**, sin extra nuevo salvo lo que decida la etapa 3 sobre el verificador [Q-A08-08]. Sin cambios en tablas de A01–A07.
- **Fuente candidata** como dato del paquete (archivo OFL con su licencia), cargada solo en el proceso aislado [Q-A08-09].
- Límites nuevos (elecciones iniciales): `max_pdf_fonts_per_page`, `max_cmap_entries`, `max_pdf_selections_per_document` (10 000), `max_pdf_occurrences_per_derivative` (10 000), `max_form_xobject_uses` (para el clonado); los de A05 §12 y A06 §15 siguen vigentes.

## 14. Observabilidad

Como A07 §14 (registrador `cawbase_content_engine.pdf`): ids, fase, códigos, conteos, tamaños y duraciones; **nunca** texto, valores, marcadores, tokens, posiciones, zonas, nombres de fuente, `binding_mac` ni hashes de contenido.

## 15. Hipótesis por verificar (`H-A08-nn`)

| ID | Hipótesis | Etapa |
|---|---|---|
| H-A08-01 | El extractor v2 es determinista: la reextracción reproduce byte a byte el `SourceMap` canónico en macOS y Linux | 2 y 3 |
| H-A08-02 | El extractor v1 rechaza exportaciones nativas por razones ajenas al contenido (búsqueda de bytes de `/Prev`, `/OpenAction`, `/ObjStm`; escalas negativas en `cm`/`Tm` por separado; Type0) | 2 (línea base) |
| H-A08-03 | Chromium y Google Docs exportan con fuentes Type0/CID subconjuntadas | 2 (`pdffonts`) |
| H-A08-04 | La operación local sobre el operador (§7.1) deja intactas las posiciones y el render fuera de la zona | 2 |
| H-A08-05 | `pikepdf` reescribe el archivo sin dejar los objetos o flujos sustituidos ni revisiones previas | 2 |
| H-A08-06 | La correspondencia inversa Unicode → código de la fuente original es única para los valores del corpus | 2 |
| H-A08-07 | `pypdf` estricto sirve como recorrido estructural independiente de QPDF para objetos, object streams y flujos | 2 |
| H-A08-08 | PDFium y Poppler extraen el marcador comprimido sin insertar espacios | 2 |
| H-A08-09 | Separar la identidad del extractor PDF no cambia en nada la identidad, el `requirements_key` ni el comportamiento de A07 | 3 |
| H-A08-10 | D-33 intacta con A08 (mediana en caliente ≤ 300 ms) | 3 |

## 16. Capacidades condicionadas a evidencia (resumen)

Entran al alcance productivo **solo** si la etapa 2 las acredita sobre el corpus y los controles, y el usuario aprueba el alcance resultante: Type0/CID (PT-03, PT-04), `/Differences` y Type1C (PT-02), estado de texto (PT-06), orientación por matriz combinada y 270° (PT-08, PT-09), separadores por geometría (§4.3), `/Contents` en arreglo (PT-14), object streams (PT-15), linealizados (PT-16), clonado de Form XObjects (PT-20), detección de texto cubierto (PT-07), reglas de recursos decorativos y trazados (§5.4), restauración por correspondencia inversa (§9.2) y la zona `Z2` (si se aprueba P-A08-01). Lo no acreditado se rechaza con motivo cerrado.
