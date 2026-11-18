# Protección y restauración PDF — contrato de A08

> **Estado: IMPLEMENTADO en `0.7.0` (migración `0006`, `schema_level` 6, catálogo de permisos 29). Pendiente de la revisión del código y la evidencia, y de la aceptación del usuario.**
>
> - **Base y alcance:** la etapa 3 la autorizó el usuario con un alcance PDF acotado (§0.1), que este contrato fija sin una aprobación intermedia. Las decisiones `D-A08-01` a `06` siguen vigentes, y los criterios C-2B y el criterio visual de la etapa 3 están en [A08-decisions](decisions/A08-decisions.md).
> - **Ambientes:** probado en macOS y en Linux local (Docker), con PostgreSQL y SQLite reales. **No** se ejecutó en GitHub Actions.
> - **Qué no acredita:** no acredita calidad de detección, porque los hallazgos vienen del mock de A03 (A09).
> - **Revisiones anteriores:** la revisión 1 (contrato experimental de la etapa 1) y las etapas 2 y 2B quedan como evidencia histórica ([informe de la etapa 2](a08-feasibility-report.md), [informe 2B](a08-stage2b-report.md)).
> - **Evidencia:** [informe de A08](a08-implementation-report.md).

Relacionado: [procesamiento de documentos (A04)](document-processing.md) · [trabajos, archivos y ciclo de vida (A05)](jobs-files-and-lifecycle.md) · [protección de texto (A06)](text-protection.md) · [protección DOCX (A07)](docx-protection.md) · [seguridad y aislamiento](security-and-isolation.md) · [persistencia y migraciones](persistence-and-migrations.md) · [especificación de A08](features/A08-proteccion-y-restauracion-pdf.md).

**Convención:**

- **[D1]**–**[D6]**: decisiones aprobadas (`D-A08-01` a `06`).
- **[C1]**–**[C6]**: criterios C-2B-01 a 06.
- **[V]**: el criterio visual de la etapa 3.
- **[E-nn]**: elecciones técnicas (`E-A08-nn`).

`A05 §n`, `A06 §n` y `A07 §n` apuntan a los contratos de esas features.

## 0. Resumen

A08 añade, **solo por API Python** [D6], dos operaciones durables sobre el sistema de trabajos de A05, siguiendo el patrón implementado de A07:

1. **`pdf_protection`**:
   - Parte de un `document_analysis` PDF vigente, autorizado y concluyente: extractor v2 y mock de A03 [D4].
   - Aplica la política estricta de A06: unión, precedencia de la redacción y marcadores del contexto.
   - **Elimina** los códigos seleccionados de sus operadores y dibuja cada sustituto dentro de una zona calculada del original (TB, primero `Z1` y `Z2` solo si hace falta) [C1].
   - Elimina las clases auxiliares autorizadas [D3].
   - Verifica el resultado con un verificador independiente [D2, D3, C5].
   - Publica el PDF protegido y un vínculo autenticado [D5].
2. **`pdf_restoration`**:
   - Parte del **PDF protegido exacto**, incluida la copia del host, y de su vínculo.
   - Valida el digest y los marcadores **antes** de descifrar.
   - Restituye en una pasada los valores Unicode exactos, sin el original ni el análisis.
   - Verifica y publica el PDF restaurado [D5].

```text
document_analysis (A05, extractor v2, mock A03)        contexto A06 (vigente, tipo fijo)
        │ resultado + SourceMap v2 + original                  │ correspondencias cifradas
        ▼                                                      ▼
submit_pdf_protection ─► worker: vincular (reextracción v2) ─► plan (cobertura, zonas) ─► marcadores ─► transformar
                          ─► verificar (extractor v2 + verificador pypdf) ─► publicar PDF protegido + vínculo
submit_pdf_restoration(derivative_id, file_id = copia exacta) ─► digest ─► posiciones ─► descifrar ─► restaurar
                          ─► verificar ─► publicar PDF restaurado
```

**No hace:**

- aceptar hallazgos, mapas ni geometría del llamador;
- OCR ni aplanado;
- procesar escaneados, mixtos o recursos sin cobertura verificable;
- selecciones de varias líneas;
- inferencia real (A09) ni HTTP;
- restaurar un PDF editado;
- recuperar lo redactado o eliminado;
- prometer identidad byte a byte.

### 0.1 Alcance aprobado para la etapa 3 (encargo del usuario)

- PDF con texto digital y selecciones localizables **dentro de una línea**, solo con las variantes estructurales que el verificador puede comprobar (§5), incluido el Type0/CID subconjuntado de `C1`.
- Word y Google Docs quedan como exportadores **sin verificar**: no hay exportaciones y su ausencia no bloquea la entrega.
- **TB**, `Z1` primero y `Z2` cuando haga falta, con los sustitutos exactos de A06. **RZ** al restaurar `Z2`.
- `/ActualText` solo si coincide con sus glifos y queda fuera de la selección y de lo que `Z2` debe mover. `C5` falla de forma explícita.
- El archivo se acepta o falla entero.
- Los renders de aceptación aplican el criterio visual **[V]** (§10.3). Los resultados de las etapas 2 y 2B no se reinterpretan.

## 1. Fronteras y principios

Los de A07 §1 sin cambios, más estos:

- **Lo esperado nunca sale del transformador [E-01, C5].** El texto, las zonas y la geometría esperados los calcula el verificador desde el original y las selecciones. Cada número que el transformador registra para el restaurador (el *layout* del vínculo) se contrasta con ese cálculo antes de publicar (VBND, §10).
- **Fallo cerrado sin parciales.** Cualquier selección no transformable, residuo, diferencia fuera de zona, cobertura no justificable o plan no justificado hace fallar el documento entero sin publicar.
- **Decisión en un instante** (A07 §10.2): la hora de la base se lee una vez, después de adquirir todos los bloqueos.
- **D-33 intacta:** nada de A08 se importa con el paquete base. El extractor, el transformador y el verificador solo se ejecutan en el proceso aislado.

## 2. Glosario (además de A07 §2)

| Término | Definición |
|---|---|
| **Extractor v2** | `cce-pdf-extract` 1 (`documents/pdf_extract.py`): intérprete de contenido sobre `pikepdf` con estado de texto completo y geometría por glifo |
| **Caja tipográfica** | caja de un glifo: horizontalmente, del origen al ancho (`/Widths` × tamaño × `Tz`); verticalmente, de `Descent` a `Ascent` del descriptor de la fuente |
| **Tinta** | caja real del glifo leída del programa TrueType incrustado (`glyf`; `documents/ttf_ink.py`). Si no se puede leer, se usa la caja tipográfica (conservador) |
| **Zona** | rectángulo autorizado de una selección (`Z1`) o de una línea (`Z2`), calculado del original (§6) |
| **Layout** | geometría que el transformador registra en el vínculo, una entrada por bloque. Incluye zonas, posiciones, avances, tramos de fuente del valor (fuente, tamaño, em, `Tz`, número de puntos de código, avance) y glifos desplazados. **Nunca** contiene valores |
| **Bloque** | operadores insertados en el mismo objeto de texto que dibujan el sustituto y, en `Z2`, el texto desplazado |

## 3. Fuente de hallazgos y vinculación [D4]

Son las condiciones S1–S12 de A07 §3.2, con estos cambios:

| # | Cambio respecto de A07 |
|---|---|
| S6 | `format = pdf`. Un análisis DOCX ⇒ `analysis_source_invalid` |
| S7 | El extractor del mapa y del resultado debe ser el v2 vigente (`id`, `version`, `config_sha256` de §4.1). Un análisis PDF hecho con el v1 de A05 ⇒ `analysis_source_stale` (§4.4) |
| S8 | La reextracción aislada con v2 reproduce **byte a byte** el JSON canónico del `SourceMap` v2 publicado |
| S12 | Exige `pdf:protect` + `results:read` |

Los hallazgos se resuelven **por unidad (página)** y por offsets en puntos de código del texto exacto, nunca por búsqueda de texto. Se reutilizan la unión, la precedencia de la redacción, la colisión de marcadores (PC-04), los contextos y la criptografía de A06, con las mismas funciones que en A07.

## 4. Extractor v2, `SourceMap` versionado y compatibilidad [D4]

### 4.1 Identidad por formato [E-02]

- **DOCX** conserva exactamente su identidad (`cce-docs-extract` 1 y su `config_sha256` sobre las 14 claves de A05). A07 no cambia.
- **PDF** usa `cce-pdf-extract` 1, con `config_sha256` sobre los límites PDF de A05 §12.1 y las constantes geométricas de los separadores (`line_tol` 0,5 em, `space_gap` 0,15 em, `column_gap` 1,5 em). Sus mapas son `document-structures/2`.

### 4.2 Snapshot, clave de requisitos e identidad de validación (P-A08-04, implementada)

Un `document_analysis` nuevo fija:

- el conjunto `extractors = {docx: v1, pdf: v2}` y `contracts.document_structures_by_format`;
- un `requirements_key` nuevo, `document/2`, que ningún runtime `0.4.0`–`0.6.0` anuncia, de modo que esos runtimes no reclaman trabajos nuevos;
- una identidad de validación nueva, `validation_identity_v2`, que incluye la configuración del extractor v2 [E-23]. Así, un PDF rechazado por v1 puede volver a validarse y ningún resultado v1 se reutiliza.

En la descarga (A05 §3.7), la validación v2 prevalece sobre la v1 del mismo archivo.

`provenance.extractor` del resultado y `identity.extractor` del mapa son la identidad del **formato detectado**.

El runtime `0.7.0` también anuncia la clave heredada (`document_analysis_v1`). Los trabajos aceptados por runtimes anteriores, con snapshot sin `extractors`, terminan con el extractor PDF v1 **congelado** (`documents/pdf.py`, sin cambios) y su mapa `document-structures/1`.

### 4.3 Pieza y separadores (`document-structures/2`)

```text
PieceRef (PDF v2) { page_index, stream ("page:<p>:<k>" | "xobj:<num>:<gen>"), text_object, operator_index,
                    operand_index, code_start, code_end, font {resource_name, object_ref, code_bytes},
                    relation ("1:1" | "n:1"), box [4 números, redondeo a 0,001] }
SegmentRef { unit_start, unit_end, kind: "original" | "synthesized_line" | "synthesized_space", pieces[] }
```

Las reglas de los separadores son estas:

- **`synthesized_line`:** cuando el glifo cambia de dirección, cuando se desplaza en perpendicular a la línea más de 0,5 em, o cuando el avance es negativo más allá de 0,5 em o positivo más allá de 1,5 em.
- **`synthesized_space`:** un hueco de más de 0,15 em entre dos glifos que no son espacios.

El JSON canónico tiene claves ordenadas y es determinista. El mismo archivo produce el mismo mapa en macOS y en Linux (H-A08-01).

### 4.4 Compatibilidad con objetos anteriores

| Objeto anterior | Tratamiento |
|---|---|
| `document_analysis` DOCX de `0.4.0`–`0.6.0` | Sin cambios: identidad v1 intacta y A07 los sigue protegiendo |
| Derivados DOCX y sus vínculos | Sin cambios |
| `document_analysis` PDF de `0.4.0`–`0.6.0` (v1) | Siguen legibles por los lectores genéricos tal como se publicaron, sin reinterpretar nada. No alimentan `pdf_protection`: dan `analysis_source_stale`. **Cómo obtener uno válido:** un nuevo `submit_document_analysis` sobre el mismo `file_id` (si sigue `stored` y vigente) o sobre una nueva carga; se analiza con v2 |
| Trabajos en cola aceptados por un runtime anterior | Los reclama `0.7.0` (clave heredada) y los termina con v1. Un runtime anterior nunca reclama un trabajo nuevo |
| Hosts que leen mapas PDF | Los análisis nuevos publican `document-structures/2`: cambio visible para hosts (P-A08-04) |

## 5. Matriz PDF de la etapa 3 [D3, C6]

### 5.1 Análisis (extractor v2: el documento se rechaza al validar)

| Fila | Contenido | v2 | Motivo de rechazo |
|---|---|---|---|
| PA-01 | Fuente simple (`Type1`, `TrueType`, `MMType1`) con `ToUnicode` de 1 byte, o `WinAnsi`/`MacRoman`/`Standard` sin `/Differences` | **Admite** | — |
| PA-02 | Base-14 sin `/Widths` | Admite **solo para analizar** (métricas aproximadas); no transformable (PB-07) | — |
| PA-03 | Type0 `Identity-H` con descendiente `CIDFontType2` o `CIDFontType0` y `ToUnicode` de 2 bytes (C1) | **Admite** | — |
| PA-04 | Type3, otro CMap, `/Differences` sin `ToUnicode`, código sin correspondencia o a `U+FFFD` | Rechazo | `font_mapping_insufficient` |
| PA-05 | `Tc`, `Tw`, `Tz`, `TL`, `Ts`, `Tr 0/1/2` | **Admite** | — |
| PA-06 | `Tr 3`; glifo fuera de la `CropBox`; glifo cubierto por un relleno pintado después | Rechazo | `hidden_text` |
| PA-07 | `Tr 4–7`; fuente en `ExtGState` | Rechazo | `unsupported_text_state` |
| PA-08 | Object streams, xref streams, `/Contents` en arreglo, linealizado, `/Rotate` múltiplo de 90 | **Admite** (la salida se escribe sin object streams ni linealización, pérdida declarada) | — |
| PA-09 | Actualización incremental, reparación, cifrado, firmas o `/Perms` | Rechazo | `incremental_update` / `parser_repair` / `encrypted` / `signed` |
| PA-10 | `AcroForm`, `Widget`, `/OCProperties`, XFA, contenido marcado `/OC` | Rechazo | `forms_or_layers` |
| PA-11 | Acciones ejecutables o externas en cualquier objeto (detección **estructural**: `/JS`, `/S` de riesgo, `/AA`, `OpenAction` que no sea `GoTo`) | Rechazo | `executable_action` |
| PA-12 | Adjuntos, `/EF`, `Filespec`, `FileAttachment` | Rechazo | `embedded_content` |
| PA-13 | `/Alt`, `/E` en contenido marcado; `/Alt`, `/ActualText`, `/E` en el árbol de estructura | Rechazo | `alternate_text_duplication` |
| PA-14 | `/ActualText` en contenido marcado | Admite si su texto es **idéntico** a sus glifos; si no, rechazo | `alternate_text_duplication` |
| PA-15 | Anotaciones `Link` (`URI`/`GoTo`), `Text`, `Popup` | Admite | — |
| PA-16 | Otras anotaciones, `/PieceInfo`, operadores desconocidos o mal formados | Rechazo | `unsupported_structure` |
| PA-17 | Página solo de imagen (escaneada) | Rechazo | `scanned_without_text` |
| PA-18 | Imágenes y trazados | Admite para analizar, declarados `images` / `vector_graphics` (`no_extraction`); la cobertura se decide al proteger (PB-01) [E-22] | — |

### 5.2 Protección (planificación en el proceso aislado; el trabajo falla sin publicar)

| Fila | Contenido | Tratamiento | Fallo |
|---|---|---|---|
| PB-01 | Recurso pintado que no admite ninguna regla congelada: RP3 (rejillas de tabla), RP5 (rectángulo relleno detrás de texto) o RP7 (fondo blanco). Imágenes, curvas, sombreados y patrones **nunca** se admiten (sin OCR ni aplanado; P-A08-03 sigue pendiente) | **Rechazo del documento** | `uncovered_content` |
| PB-02 | Selección que contiene un `synthesized_line` (varias líneas) | Rechazo | `selection_crosses_boundary` |
| PB-03 | Selección que corta una ligadura (`n:1`) | Rechazo | `localization_ambiguous` |
| PB-04 | Selección con escritura de derecha a izquierda | Rechazo | `selection_not_transformable` |
| PB-05 | Selección que interseca un tramo `/ActualText` | Rechazo [C3] | `selection_not_transformable` |
| PB-06 | Selección dentro de un Form XObject, repartida entre varios flujos o con matriz combinada no horizontal (rotada, espejada o inclinada) | Rechazo (no hay clonado de Form XObjects: P-A08-05 no implementada) | `selection_not_transformable` |
| PB-07 | Selección en fuente con métricas aproximadas (PA-02) | Rechazo | `font_mapping_insufficient` |
| PB-08 | `Z2` que tendría que mover un glifo de un tramo `/ActualText` (**C5**), de otro flujo, de otra secuencia de contenido marcado (MCID), no horizontal o con métricas aproximadas; o un objeto pintado o un enlace sobre la región movida | Rechazo [C3] | `zone_plan_unjustified` |
| PB-09 | Sustituto que no cabe en `Z1` ni en `Z2` con compresión ≥ 0,60, o cuya tinta excede la banda | Rechazo | `replacement_overflow` |
| PB-10 | Texto simple o Type0 horizontal en el flujo de la página: Z1, Z2, varias selecciones por línea, valores partidos entre operadores u objetos de texto, estado de texto, `Tr 2` | **Transforma** (§7) | — |

### 5.3 Clases auxiliares eliminadas [D3]

| Clase | Qué se elimina | Qué se conserva |
|---|---|---|
| `document_info_descriptive` | Toda clave de `/Info` | `/CreationDate`, `/ModDate` |
| `xmp_metadata` | El XMP del catálogo, completo (P-A08-06) | — |
| `object_metadata` | `/Metadata` de cualquier otro objeto | — |
| `comments` | Anotaciones `Text` y `Popup`, y la referencia `/Popup` de las conservadas | Enlaces |

Se registran las clases **efectivamente eliminadas** (clase y cantidad). Tras restaurar siguen ausentes.

## 6. Zonas: TB, `Z1` y `Z2` [C1, C5]

Las calculan el planificador (extractor v2) y, por separado, el verificador (pypdf) con la **misma regla** sobre el **original**:

- **Línea:** glifos sin espacios cuyo centro vertical dista del centro del valor menos de la mitad de la altura tipográfica del valor.
- **Banda (TB):** unión de las cajas tipográficas de la línea, **recortada contra la tinta** de los glifos de otras líneas que la solapan. Si la tinta es desconocida, se usa la caja tipográfica.
- **Izquierda:** el origen del primer glifo, o la tinta si empieza antes (cursivas). El sustituto empieza en el origen del valor.
- **Fin del valor:** el máximo entre su caja y su tinta.
- **`Z1`:** termina en el mínimo de cuatro límites:
  - el siguiente glifo de la línea (el primero cuyo centro queda a la derecha del centro del último glifo del valor, robusto frente al kerning);
  - un objeto pintado que cruza la banda (los fondos blancos no cuentan);
  - el borde derecho del texto de la página;
  - la `CropBox`.

  A ese mínimo se resta un margen de 0,3 em efectivos, y la zona nunca termina antes que el valor.
- **`Z2`:** el mismo cálculo sin el siguiente glifo. En una línea con varias selecciones, se toma la más restrictiva.
- **Pertenencia:** un glifo pertenece a la zona si su centro está dentro **y** termina dentro (0,01 pt). Un glifo a caballo del límite se queda donde está.

Planificación:

- `Z1` con compresión `s = min(1, disponible / ancho natural) ≥ 0,60`, y la tinta del sustituto dentro de la banda con una tolerancia de 0,25 pt. Cada selección `Z1` tiene **su propio bloque**, dibujado en su posición, para conservar el orden de lectura [E-26].
- Si no cabe, `Z2` para la línea entera:
  - el texto no seleccionado que cabe entero en la zona se comprime con un único factor `f ≥ 0,60`, con la geometría de C-2B-05;
  - lo que queda fuera de la zona no cambia.

## 7. Transformación y sustitutos [D2, E-24..E-27]

- **Fuente:** siempre la **candidata**. Es el subconjunto OFL de Noto Sans Condensed con el alfabeto Σ (`[]:` + el alfabeto de A06) y el espacio, empaquetado en `documents/data/` con sus métricas congeladas (`cce-candidate.json`: `/Widths` enteros, extensión vertical por glifo, SHA-256).
  - Se incrusta como `TrueType` simple con `WinAnsiEncoding` y `ToUnicode` identidad.
  - Ni el runtime ni las pruebas parsean el programa de la fuente: lo genera `scripts/build_pdf_candidate_font.py`, una herramienta de desarrollo.
  - En 2B solo la candidata era verificable [E-24].
- **Eliminación real:** los códigos del valor (y, en `Z2`, los del texto desplazado) **desaparecen** de su operador.
  - Si la fuente del operador tiene un código de espacio **sin tinta comprobada**, cada trozo del operador conserva la **extensión completa** del original, sustituyendo los códigos ajenos o eliminados por ese espacio con una corrección `TJ` exacta. Los bloques se dibujan entre trozos y cada uno devuelve la matriz al origen del operador.
  - Si no, se cortan con correcciones `TJ` numéricas.
  - Conservar la extensión completa elimina la heurística de raster de PDFium descrita en 2B §3.1 (§10.3) [E-27].
- **Bloque:** se inserta en el mismo objeto de texto y con la misma CTM. Contiene:
  - el color de cada glifo;
  - `Tf` candidata, con `Tz` = compresión, `Tc`/`Tw` 0 y el `Ts`/`Tr` del valor;
  - un `Tm` explícito por sustituto y por glifo desplazado;
  - la restitución del estado del operador.
- **Varias selecciones** de un mismo operador: un bloque por selección `Z1` o por línea `Z2`. No puede haber dos bloques en el mismo código.
- **Salida:** `pikepdf` sin object streams ni linealización, con flujos comprimidos e identificador determinista. Las clases de §5.3 eliminadas.

## 8. Ocurrencias, layout y vínculo [D5]

- **Ocurrencias:** cada marcador de seudonimización emitido, localizado por el extractor v2 en el derivado. Se registran la unidad, `[start, end)`, el flujo, el operador, el operando, el rango de códigos y el token.
- **Layout:** por bloque, se registran estos datos:
  - página, flujo, rango `[b0, b1)` del bloque, tipo, `f`, `x0`, `r`, izquierda y banda;
  - por selección: `sid`, acción, offsets, escala, `x_new`, `x_orig`, avance original, ancho natural del sustituto, tamaño, tramos de fuente, sustituto, banda, izquierda y límite de zona;
  - por glifo desplazado: `x_orig`, `x_new` y `Tz`.

  **Sin valores**, tipos ni fragmentos. Revela la longitud del valor en puntos de código y su avance: es una limitación declarada, comparable con la longitud del cifrado de A06 (P-A08-07).
- **`binding_mac`:** HMAC-SHA256 con la clave de índice del contexto sobre `"cce.pdf.binding.v1"` ‖ JSON canónico de `{scope_id, derivative_id, context_id, context_kind, sha256, size_bytes, extractor [id, version, config_sha256], occurrences, layout}`.

## 9. Restauración del derivado exacto [D5]

### 9.1 Comprobaciones, en este orden

Son T1–T6 de A07 §7.2, con `pdf:restore` y la tabla `pdf_derivatives`:

- **T1:** vínculo `active`, del contexto y del tipo pedidos.
- **T2:** SHA-256 y tamaño de la copia **antes** de analizarla.
- **T3:** contexto vigente y scope activo.
- **T4:** `binding_mac` válido; ocurrencias y layout idénticos a los de la fila.
- **T5:** la reextracción v2 encuentra los marcadores estrictos **exactamente** en las ocurrencias del vínculo, sin R1/R2 y con la identidad v2 del vínculo.
- **T6:** descifrado autenticado de A06.

No usa el original, el análisis ni el trabajo de protección, y no llama a la inferencia.

### 9.2 Aplicación (una pasada)

El restaurador recibe **solo** el derivado, el layout y los valores de las seudonimizaciones:

- Cada valor se codifica con su **fuente original**, que se conserva en el derivado, por correspondencia inversa única. Va primero lo más largo, por las ligaduras.
  - Si un carácter no tiene un código único, falla con `restoration_encoding_unavailable`.
- El valor se dibuja tramo a tramo, con el em de cada tramo, desde `x_orig` y dentro de su avance original. El kerning interno del valor se reparte, como admite D-A08-05.
- En `Z2`:
  - los glifos desplazados vuelven a su `x_orig` con su `Tz` original;
  - **RZ**: `[REDACTED]` vuelve al inicio del valor redactado, comprimido a su avance con un mínimo de 0,60. El exceso a ese mínimo desplaza lo que sigue. Si la línea se saldría de su límite, falla con `restoration_layout_unavailable` (CS16).
- En `Z1`, la redacción queda donde la dibujó el derivado.
- Las clases eliminadas y lo redactado no reaparecen.

## 10. Verificación antes de publicar [D2, D3, C5]

### 10.1 Verificador independiente (`documents/pdf_verify.py`, proceso aislado propio)

- **Parser:** `pypdf` en modo estricto para objetos, *xref* y flujos decodificados.
- **Analizador léxico propio de flujos de contenido**: las cadenas se leen como bytes. Ni el de pypdf (que reescribe cadenas) ni el de QPDF.
- **Intérprete, lector de `ToUnicode` y reglas propios** de zonas y cobertura.
- **No importa** el extractor ni el transformador. Solo comparte con el planificador:
  - el lector TrueType de cajas `ttf_ink.py`, que lee bytes y no es un parser PDF;
  - las métricas congeladas de la fuente candidata.

### 10.2 Comprobaciones (protección y restauración)

| Id | Comprobación | Fallo |
|---|---|---|
| VA | Apertura estricta sin avisos de ambos PDF | `derivative_verification_failed` |
| VG / VN / VE / VS | Número y cajas de página; anotaciones iguales (salvo las eliminadas); clases eliminadas ausentes; ningún objeto inalcanzable | ídem |
| VU | La interpretación propia del original da exactamente el texto de unidades del extractor v2 | ídem |
| VC | Cobertura D-A08-03 (RP3, RP5, RP7) sobre el original | `uncovered_content` |
| VZP | Cada selección en exactamente una entrada del layout. Una línea `Z2` agrupa todas las selecciones de su línea. El plan **nunca** amplía la zona del original (C-2B-05, CV31) | `derivative_verification_failed` |
| VBND | Cada número del layout que usará el restaurador (`x_orig`, avance, ancho natural, tamaño, tramos de fuente, banda, izquierda, límite de zona, glifos desplazados) igual al cálculo propio sobre el original | ídem |
| VT | Fuera de las zonas, cada glifo con tinta es idéntico: texto, origen, avance, em, fuente, color, `Tr`, MCID y `/ActualText`. Cada blanco original sigue en su sitio, y un blanco **añadido** fuera de zona tiene que tener la tinta vacía comprobada (E-A08-27) | ídem |
| VB | Operadores no textuales, objetos pintados y listas de propiedades de contenido marcado iguales | ídem |
| VZ / VO / VL | Texto de cada zona en el orden esperado. El sustituto usa la fuente candidata exacta (SHA-256, `/Widths`, `ToUnicode`). Su tinta queda dentro de la banda y de la zona. Compresión en `[0,60; 1]` | ídem |
| Z2 | Geometría de C-2B-05: `x' = a1 + f (x − a1) + Σ(w'_k − f · span_k)`, con un único `f` por mínimos cuadrados. Sustitutos comprimidos con el mismo `f`. Avances desplazados = `f ×` el ancho de glifo original. Línea base, em, fuente, color y MCID iguales. El `f` del plan se contrasta con una tolerancia de ±0,002 | ídem |
| VO1 | El texto del derivado, sin blancos, es el oráculo (el original con cada selección sustituida). Marcadores estrictos exactamente los emitidos, en orden. Ningún R1/R2 | ídem |
| VK / VX / VP | Barrido de residuos, sin ningún valor seleccionado fuera de sus ocurrencias permitidas (§10.4) | `residue_detected` |
| VM (restaurar) | Sin marcadores. `[REDACTED]` intactos. Valores en `x_orig` (+ exceso RZ) y con su avance. Glifos desplazados en su `x_orig` con su `Tz`. Fuera de las zonas, idéntico al derivado | `derivative_verification_failed` |

### 10.3 Render: evidencia de aceptación, no control de ejecución [V]

En runtime no hay renderizador (B-A08-07). La aceptación se mide en `tests/test_a08_evidence.py` con PDFium (`pypdfium2`) y Poppler (`pdftoppm`) a 150 ppp en gris, sobre `L1`, `C1`, `M1`, `L5` y `M5`, comparando original → protegido y protegido → restaurado. Fuera de las zonas, dilatadas 3 px:

- **Poppler:** 0 px.
- **PDFium:** cada píxel distinto debe reproducirse con el raster del glifo trasladado de forma rígida, como máximo 1 px y con **una** traslación por glifo (entera o subpíxel, en pasos de 1/8, bilineal), con |Δ| ≤ 32. Además, el glifo tiene que conservar texto y caja extraída (±0,01 pt).

Una caja ampliada por sí sola no explica nada. **Resultado medido:** 0 px en PDFium y en Poppler en los cinco casos y en ambas comparaciones. El criterio [V] queda disponible, pero no hizo falta usarlo (§11 del informe).

### 10.4 Barrido de residuos y ocurrencias permitidas

- **Valores y representaciones:** los valores seleccionados se buscan en sus representaciones exacta, NFC, NFD, *casefold* y con blancos colapsados.
- **Ocurrencias permitidas:** las del texto del original que no solapan una selección, y solo esas. Su posición queda verificada por VT.
- **Dónde se busca:**
  - el texto del derivado, con los sustitutos emitidos enmascarados (una coincidencia **dentro** de un sustituto se le atribuye a él, como en A07 §6.3.1);
  - los códigos de cada tramo de fuente decodificados con **todas** las fuentes del documento, salvo los tramos ya verificados como sustitutos;
  - toda cadena y nombre del grafo de objetos, también decodificados como URL;
  - las listas de propiedades de contenido marcado;
  - comentarios, cadenas de operadores no textuales y datos de imágenes en línea de los flujos de contenido;
  - los flujos que no son de contenido, decodificados;
  - los bytes del archivo con los cuerpos de los flujos enmascarados (un dato comprimido no puede contener texto legible y su contenido decodificado ya se barrió).
- **Fuera de cobertura** (declarado en `inspection_scope`): formas de glifo de fuentes incrustadas y píxeles de imágenes, que nunca se admiten (PB-01).

## 11. API, permisos, lectores y retención [D6]

```python
# Métodos de ScopedOperations (solo Python; todos rechazan HostTransaction)
async def submit_pdf_protection(scope_id, *, analysis_job_id, policy, context: ExistingContext | NewContext,
                                idempotency_key) -> PdfProtectionAccepted        # pdf:protect + results:read
async def get_pdf_protection_result(scope_id, job_id) -> PdfProtectionResult    # pdf:protected:read
async def open_pdf_protected(scope_id, job_id) -> SpooledStream                 # pdf:protected:read
async def get_pdf_derivative(scope_id, derivative_id) -> PdfDerivativeInfo      # pdf:protected:read
async def submit_pdf_restoration(scope_id, *, derivative_id, file_id, context_id, kind,
                                 idempotency_key) -> JobAccepted                # pdf:restore
async def get_pdf_restoration_result(scope_id, job_id) -> PdfRestorationResult  # pdf:restored:read
async def open_pdf_restored(scope_id, job_id) -> SpooledStream                 # pdf:restored:read
```

- **Tipos:** `pdf_protection/types.py`. `ExistingContext` y `NewContext` son los de A07.
- **Identificador:** `derivative_id` sigue el formato `pdv_` + 32 hexadecimales.
- **Informe:** `PdfProtectionResult` incluye fuente, contexto, política, grupos por unidad, conteos, `zones {lines, z1, z2}`, clases eliminadas, cobertura (con `resources_admitted_by_rule`), `inspection_scope` y verificación (`render: not_run_in_runtime`). Nunca contiene valores, posiciones en la página ni nombres de fuente.
- **Permisos** (catálogo 25 → **29**): `pdf:protect`, `pdf:restore`, `pdf:protected:read`, `pdf:restored:read`.
  - Descargar el original exige `files:read`, y cargar la copia, `files:write`.
  - `docx:*` no habilitan PDF ni al revés.
  - **Restricción de despliegue** heredada: no emitir claves con estos permisos hasta actualizar todos los runtimes.
- **Artefactos:** `pdf_protected`, `pdf_protection_report`, `pdf_restored`, `pdf_restoration_report`.
  - Los lectores genéricos (`get_job_result`, `open_job_artifact`, rutas HTTP de resultados, `open_file_content`) **no** los sirven: comprueban la operación.
  - Los lectores DOCX tampoco.
- **Lectura del restaurado y de su informe:** exige `pdf:restored:read`, resultado vigente, contexto vigente y scope activo, en **un** instante de autorización tomado **después** de verificar el objeto en el spool e inmediatamente antes de entregarlo.
- **`delete_job_data`** conserva el vínculo. **`delete_context`** termina la restaurabilidad de todos los derivados del contexto. No hay revocación individual.
- **Purga inmediata:** un `pdf_restored` cuyo descarte está confirmado es elegible de inmediato con la limpieza de A05 (`RESTORED_ROLES`). La aplica la limpieza de `0.7.0`, también a los descartados por un runtime anterior.
- **`restore_text`** de A06 devuelve como texto el valor de un marcador emitido en un PDF, con **todas** sus condiciones. No produce ni lee archivos.
- **Retención:**
  - Artefactos según D-A05-01.
  - El vínculo vive hasta el fin del contexto. `engine.maintenance().run_pdf_cleanup()` y la CLI `cleanup-pdf` lo marcan `ended`, eliminan ocurrencias, layout y MAC, y dejan una lápida de 30 días (las reglas de A07, con la misma función).

## 12. Ejecución durable

| Pieza | Implementación |
|---|---|
| Reclamación, lease, *fencing*, reintentos, idempotencia, cuotas, fence del scope, storage verificado, limpieza | A05, sin cambios |
| Proceso aislado | Modos `pdf_plan`, `pdf_protect`, `pdf_restore` y `pdf_verify` del hijo de A05 (mismo perfil y canario), y `pdf_extractor: v2` para extraer |
| Publicación atómica | Los *hooks* de A07, parametrizados por tabla (`pdf_derivatives`): el vínculo se inserta en la misma transacción que publica `{pdf_protected, pdf_protection_report}` o `{pdf_restored, pdf_restoration_report}` |
| Orden de bloqueos | El de A07 §10.1, con el rango 14 = `pdf_derivatives` |
| Comprobación previa | `pdf_plan` corre **antes** de crear correspondencias, con un token de relleno del glifo **más estrecho** del alfabeto: es condición necesaria, porque un token real solo puede necesitar más sitio. Un rechazo ahí (por ejemplo `C5`) no crea nada |
| Fases | Protección: `validation` → `extraction` → `planning` → `mapping` → `transformation` → `verification` → `publication`. Restauración: `validation` → `extraction` → `mapping` → `transformation` → `verification` → `publication` |

## 13. Persistencia, versión y límites

- **Tabla `pdf_derivatives`** (`persistence/a08_schema.py`, congelada en `0006`): tiene la forma de `docx_derivatives`, más `extractor_config_sha256` y `layout`. Sin valores.
- **Migración `0006_pdf_protection`:** aditiva y solo hacia adelante. `schema_level` 5 → **6**, `min_runtime_level` **1**, versión **`0.7.0`**.
  - `0.6.0` funciona sobre el nivel 6, no reclama trabajos de A08 y sus lectores genéricos no sirven sus artefactos.
  - `0.7.0` sobre el nivel 5 ⇒ `schema_outdated`.
- **Dependencias:** el extra `documents` añade `pypdf>=7,<8` (BSD-3-Clause). No hay extra nuevo, y la fuente candidata (OFL-1.1, con su licencia) viaja como dato del paquete.
- **Límites:** los de A05 §12 y A06 §15, más `MAX_SELECTIONS` 10 000 y `MAX_OCCURRENCES` 10 000 por documento. `max_file_bytes` también acota derivado y restaurado.

## 14. Observabilidad

Registrador `cawbase_content_engine.pdf`:

- **Permitido:** ids, fase, códigos, conteos (líneas, ocurrencias), tamaños y duraciones.
- **Nunca:** texto, valores, marcadores, tokens, posiciones, zonas, nombres de fuente, `binding_mac` ni hashes de contenido.

## 15. Limitaciones declaradas

1. **Detección.** La protección es tan completa como el análisis (mock), y se aplica por ocurrencia señalada (A06 §5.6). No es anonimización.
2. **Escrituras y orientación.** Solo escritura horizontal de izquierda a derecha; las selecciones no pueden estar en Form XObjects ni ocupar varias líneas.
3. **Recursos sin cobertura.** Las imágenes, curvas, sombreados y patrones hacen fallar la protección: los documentos con logotipos o fotografías no se protegen (P-A08-03 pendiente).
4. **Restauración.** Puede fallar cerrada en una línea `Z2` sin sitio para el exceso RZ, o cuando un carácter no tiene código único en su fuente.
5. **Tintas desconocidas.** Sin un programa TrueType legible, la banda se recorta con cajas tipográficas (más zonas `Z2` o `replacement_overflow`), y el operador se corta con números (sin la extensión completa).
6. **Falsos positivos del barrido.** El barrido de flujos decodificados (por ejemplo programas de fuente) puede dar falsos positivos con valores cortos (fallo cerrado), y la decodificación cruzada con valores de 3 caracteres también.
7. **Blancos añadidos.** El texto extraído del derivado y del restaurado contiene blancos y saltos de línea añadidos. El orden de lectura del texto con tinta es el del original.
8. **Formato de la salida.** Pierde la linealización y la conformidad PDF/A o PDF/UA; las fuentes originales se conservan.
9. **Exportadores sin verificar.** Word y Google Docs no se han medido.
10. **Información que revelan.** Los marcadores y el layout revelan la posición, la igualdad y la longitud de los valores (A06 §6.4, P-A08-07).
11. **Render.** No se renderiza en runtime: el render es evidencia de aceptación.
