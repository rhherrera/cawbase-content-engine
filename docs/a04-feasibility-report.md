# Informe de viabilidad de A04 — documentos DOCX y PDF

> **Estado: experimentos EJECUTADOS en macOS y en Linux local (Docker) sobre documentos SINTÉTICOS; A04 PENDIENTE de aceptación del usuario. GitHub Actions NO se ejecutó para A04.** La viabilidad se demostró **solo dentro del subconjunto probado y con las restricciones enumeradas**; este informe no es un acta de aceptación, no habilita por sí solo A07/A08 y no acredita protección universal. El mock no mide calidad de detección y la ausencia de los valores sintéticos seleccionados no acredita ausencia universal de datos sensibles. No hay soporte documental en `0.3.0`.

Relacionado: [especificación](features/A04-viabilidad-y-contrato-de-documentos.md) · [contrato](document-processing.md) · [decisiones](decisions/A04-decisions.md) · código y evidencia: [`experiments/a04/`](../experiments/a04/README.md) · [resumen de cada comprobación](../experiments/a04/outputs/results-summary.md).

## 0. Estado por nivel de evidencia

| # | Nivel | Estado |
|---|---|---|
| 1 | Especificado | Sí: contrato y plan (commits `f638b13`, `7c7af7e`) |
| 2 | Implementado | Solo como **código experimental** en `experiments/a04/` (separado del motor); **no** como capacidad del motor |
| 3 | Probado en macOS | Sí: 468 comprobaciones pasan, 0 fallan, 13 pendientes (renders DOCX: no hay LibreOffice en macOS) |
| 4 | Probado en Linux local (Docker, Debian 12 aarch64) | Sí: 481 pasan, 0 fallan, 0 pendientes; procesamiento con `--network none` |
| 5 | Probado en GitHub Actions | **No** (ningún flujo se ejecutó ni se infiere de los otros ambientes) |
| 6 | Aceptado por el usuario | **Pendiente** |

macOS y Linux son ambientes distintos y se informan por separado. Las 91 huellas de fixtures y salidas coincidieron entre ambos (observación, no aprobación cruzada).

## 1. Identidad de la ejecución

- **Código ejecutado:** commit `b7608e7334faec9532d53e689a7b97a8305fd14d` (rama `feature/a04-document-feasibility-contract`), árbol de código **limpio** en ambas corridas (`code_tree_dirty = false`; las salidas se excluyen de esa comprobación). Registrado en `outputs/results-<env>.json`.
- **macOS:** macOS 27.0.1 arm64, CPython 3.13.16; Poppler 26.10.0 (Homebrew). **Linux:** imagen `cce-a04-render` (basada en `python:3.13-bookworm`, Debian 12 aarch64, CPython 3.13.18), LibreOffice 7.4.7 (`4:7.4.7-1+deb12u14`), Poppler 22.12.0 (`22.12.0-2+deb12u3`), `fonts-liberation 1:1.07.4-11`, `fonts-dejavu-core 2.37-6` (`outputs/linux-packages.txt`).
- **Bibliotecas y licencias efectivas, binarios y componentes transitivos:** [contrato §15.1](document-processing.md#151-versiones-y-licencias-efectivas-de-los-experimentos-observadas) (`python-docx 1.2.0`, `lxml 6.1.3`, `pikepdf 10.18.0` con QPDF 12.4.2, `Pillow 13.0.0`, `pypdfium2 5.15.2` con PDFium 159.0.8149.0, `pypdf 7.0.1`, `fonttools 4.68.0`; bloqueo en `experiments/a04/uv.lock`). `python-docx` requiere **`lxml`** (obligatoria) y `pikepdf` requiere **`lxml` y `Pillow`** (obligatorias). Fuentes oficiales consultadas el 12 de noviembre de 2026 (documentación `latest`/`stable` sin versión fijada: las versiones de arriba son las **instaladas**). **No se fijó la licencia del motor** (A10). PyMuPDF **no se usó**.
- **Hashes:** `experiments/a04/outputs/hashes-macos.sha256` y `hashes-linux.sha256` (91 archivos: fixtures de entrada y salidas de muestra) y `results-<env>.json`. Fixtures principales:

| Fixture | SHA-256 (16 hex) |
|---|---|
| `fixtures/docx/D01_runs.docx` | `137a21f95870fb74…` |
| `fixtures/docx/D02_tables.docx` | `4fe1df9614dede26…` |
| `fixtures/docx/D03_headers_notes.docx` | `1e34c272bf1e4009…` |
| `fixtures/docx/D04_unicode.docx` | `fa52a3f0884900c3…` |
| `fixtures/docx/D05_field.docx` | `fb7e3b6c97ffb44d…` |
| `fixtures/docx/D06_comments_meta.docx` | `420ff0758717509c…` |
| `fixtures/docx/D07_aux_copy.docx` | `2eb969eb782dd391…` |
| `fixtures/docx/D08_all_selected.docx` | `ec12d0df49aecfb7…` |
| `fixtures/docx/D09_no_values.docx` | `04802ee548bc2a67…` |
| `fixtures/docx/D10_sdt.docx` | `1dd116d6dfd12a0f…` |
| `fixtures/pdf/P01_basic.pdf` | `777bfc44ba0723f1…` |
| `fixtures/pdf/P02_columns.pdf` | `e6040d82fe8dba72…` |
| `fixtures/pdf/P03_rotated.pdf` | `004ff12d8f4b9181…` |
| `fixtures/pdf/P04_unicode_ligature.pdf` | `1864cc7828d4ff93…` |
| `fixtures/pdf/P05_shared_xobject.pdf` | `8f8650f238c27b78…` |
| `fixtures/pdf/P06_artificial_concat.pdf` | `580cacf83b718a08…` |
| `fixtures/pdf/P07_replacements.pdf` | `416a53ac01dabe32…` |
| `fixtures/pdf/P08_meta_comments.pdf` | `d09259b7496ee4e7…` |
| `fixtures/pdf/P09_all_selected.pdf` | `6bf698f4474b9fdd…` |
| `fixtures/pdf/P10_no_values.pdf` | `50605040befbd33d…` |
| `fixtures/pdf/P11_embedded_subset.pdf` | `4e779a1219974f5b…` |

(Los 43 fixtures de rechazo y las 27 salidas de muestra —15 DOCX y 12 PDF— están en los archivos de huellas.)

- **Comandos:**

```bash
bash experiments/a04/run_macos.sh
bash experiments/a04/run_linux.sh
cd experiments/a04 && uv run python make_summary.py
```

## 2. Resultados por experimento

Entradas: 10 DOCX y 11 PDF sintéticos con manifiesto independiente (oráculo generado **por construcción**, nunca por el extractor), 43 fixtures de rechazo/cobertura parcial. Cada fila de [`results-summary.md`](../experiments/a04/outputs/results-summary.md) es una comprobación; abajo, conteos por experimento.

| Exp | macOS (pass / fail / limitación / pendiente) | Linux (ídem) |
|---|---|---|
| EXP-01 | 40 / 0 / 3 / 0 | 40 / 0 / 3 / 0 |
| EXP-02 | 67 / 0 / 0 / 0 | 67 / 0 / 0 / 0 |
| EXP-03 | 7 / 0 / 0 / 0 | 7 / 0 / 0 / 0 |
| EXP-04 | 66 / 0 / 0 / 0 | 66 / 0 / 0 / 0 |
| EXP-05 | 138 / 0 / 0 / 0 | 138 / 0 / 0 / 0 |
| EXP-06 | 9 / 0 / 0 / 0 | 9 / 0 / 0 / 0 |
| EXP-07 | 41 / 0 / 0 / 0 | 41 / 0 / 0 / 0 |
| EXP-08 | 66 / 0 / 0 / 13 | 79 / 0 / 0 / 0 |
| EXP-09 | 12 / 0 / 0 / 0 | 12 / 0 / 0 / 0 |
| EXP-10 | 1 / 0 / 0 / 0 | 1 / 0 / 0 / 0 |
| CN-01 | 3 / 0 / 0 / 0 | 3 / 0 / 0 / 0 |
| CN-02 | 2 / 0 / 0 / 0 | 2 / 0 / 0 / 0 |
| CN-03 | 1 / 0 / 0 / 0 | 1 / 0 / 0 / 0 |
| CN-04 | 2 / 0 / 0 / 0 | 2 / 0 / 0 / 0 |
| CN-05 | 2 / 0 / 0 / 0 | 2 / 0 / 0 / 0 |
| CN-06 | 6 / 0 / 0 / 0 | 6 / 0 / 0 / 0 |
| CN-07 | 5 / 0 / 0 / 0 | 5 / 0 / 0 / 0 |

(`limitación` = comportamiento de `python-docx` que se **documentó y se confirmó** (ver EXP-01); `pendiente` en macOS = renders DOCX que requieren LibreOffice.)

**Orden de ejecución.** Se empezó por una muestra DOCX (D01) y la sonda PDF mínima (P01: valor partido en tres `Tj`, `TJ` con kerning, repetición no seleccionada); ninguna encontró un impedimento material (extracción igual al manifiesto, eliminación real, apertura sin reparación, render sin diferencias fuera de la banda afectada), por lo que se completó la batería. Durante la iteración aparecieron y se corrigieron **defectos del propio código experimental** (compensación de varios valores en una misma cadena PDF, máscara de render en páginas rotadas, fixtures mal diseñados); no quedan fallos abiertos. Esos defectos no se ocultan: indican que la vía PDF exige cuidado fino de ancho y estado de texto (ver §6).

- **EXP-01 (extracción y mapeo DOCX).** Los textos por párrafo, el orden de partes (cuerpo, encabezados/pies por tipo, notas) y la resolución de cada ocurrencia a piezas originales coinciden con el manifiesto en los 10 documentos (valor repartido en 3 runs, tablas anidadas y combinadas, encabezados default/first/even, notas, `w:sdt`, campos). Determinista en dos extracciones. **Observaciones confirmadas de `python-docx`** (limitaciones documentadas): `Document.tables` no lista la tabla anidada, `Document.paragraphs` omite párrafos de celdas y no ve el texto dentro de `w:ins`.
- **EXP-02 (extracción y mapeo PDF).** Los textos por página (con separadores sintetizados) coinciden con el manifiesto; PDFium, pypdf y Poppler coinciden con el texto del manifiesto sin espacios; páginas rotadas, columnas, Form XObject compartido (2 usos detectados) y fuentes simples/incrustadas resueltas a piezas del flujo.
- **EXP-03 (Unicode).** Puntos de código ≠ unidades UTF-16 ≠ grafemas ≠ glifos confirmados con datos (😀: 1/2/1; NFC y NFD distintos); ligadura `fi` = 1 código de fuente → 2 puntos de código (`n:1`); la selección parcial de la ligadura se rechaza; la ocurrencia idéntica no seleccionada se conserva.
- **EXP-04 (A03 y mock).** Todas las unidades (21 documentos) pasan por `analyze_text` con el mock (`capabilities=("detection",)`); los hallazgos A03 `[start,end)` coinciden con las ocurrencias del manifiesto; la procedencia es simulada; `AnalysisResult` **no cambia** (sin páginas, nodos ni `scope_note`). Cobertura parcial controlada ⇒ estado `inconclusive`, `textual_subset_complete = false`, `document_coverage_complete = false`. Con cobertura completa: `textual_subset_complete = true` en los 21 documentos; `document_coverage_complete = false` en D05 (instrucciones de campo `N`), D07 (destino de enlace, imagen y texto alternativo `N`) y P08 (anotación `Link` `N`), y `true` en los otros 18 **solo porque no contienen elementos `N`**: no implica detección real.
- **EXP-05 (eliminación y sustitución).** DOCX: sustitutos cortos, largos (reflujo), vacíos (eliminación) y estándar. PDF: sustituto corto, de igual ancho, **vacío**, **más ancho (rechazo `replacement_overflow`)**, **con carácter sin glifo (rechazo `substitute_glyph_unavailable`)**, y **glifo ausente del subconjunto de una TrueType incrustada** (P11). Salida abierta sin reparación; texto posterior igual al oráculo (PDFium, pypdf, Poppler; XML independiente en DOCX); sin residuos de los valores seleccionados. Los verificadores se probaron con un control negativo (archivo sin modificar **falla**).
- **EXP-06 (comentarios y metadatos).** DOCX (comentarios con hilos, `commentsExtended/Ids`, `people`, propiedades núcleo/aplicación/personalizadas, miniatura) y PDF (`/Info`, XMP, anotaciones `Text`+`Popup`): clases eliminadas registradas, partes/relaciones/tipos de contenido coherentes, sin copias residuales en ninguna parte/objeto (UTF-8/16, referencias de carácter, codificación porcentual, hexadecimal, flujos descomprimidos). Las fechas de propiedades se **conservan**.
- **EXP-07 (rechazos).** 43 familias: cada una se rechaza con su motivo cerrado (revisiones, texto oculto, cuadro de texto, `customXml`, OLE, gráfico, relación externa, campo `INCLUDETEXT`, contenedor OLE, firma, macros, ZIP truncado, bombas, DOCTYPE, encabezado huérfano, parte desconocida, `dataBinding`, `docVars`, tipo de medio falso; PDF: cifrado con contraseña vacía, firma, actualización incremental, xref reparada, JavaScript, adjuntos, formulario, capas, `/ActualText`, texto invisible, escaneado, `FreeText`, fuente sin mapeo, Type0, bomba Flate, catálogo desconocido, `Tz`, truncado). **Bombas:** `R13` se aborta por tamaño declarado; `R14` (cabeceras ZIP manipuladas a 1000 bytes) se aborta por **bytes realmente producidos**; `Q16` (≈ 60 KiB → 60 MiB) por bytes producidos de Flate.
- **EXP-08 (fidelidad y residuos).** DOCX: esqueleto de estructura/estilos (etiquetas+atributos) idéntico en las partes editadas, partes no editadas idénticas byte a byte. PDF: páginas, `MediaBox`, `CropBox` y `Rotate` idénticos; **0 píxeles** de diferencia fuera de las bandas afectadas con tolerancia 0 (PDFium, escala 1,5) y también con Poppler (90 ppp). Renders y revisión: §7.
- **EXP-09 (restitución mecánica).** Solo prueba de viabilidad con correspondencias sintéticas locales (**no** es el contrato de A06). PDF (P01, P03, P04, P09): sustitutos relocalizados **por posición validada** y reemplazados por los originales; texto igual al original; 0 píxeles de diferencia fuera de los valores restituidos; dentro de ellos quedan diferencias de **kerning** (hasta 1495 píxeles en P01). Una posición registrada errónea se rechaza (sin búsqueda). DOCX (D01–D04, D08, D10): textos y esqueleto iguales al original. Lo eliminado por D-A04-02 **no** se recupera.
- **EXP-10 (sonda de esfuerzo PDF).** Ver §6.

## 3. Criterios de aceptación (AC-A04)

Resultados completos en la [matriz de la especificación](features/A04-viabilidad-y-contrato-de-documentos.md#7-matriz-de-aceptación): AC-A04-01 a 18 y 21 a 24 **cumplidos en el subconjunto probado** (con las salvedades de su columna: verificador DOCX comparte el XML, DOCX renderizado solo con LibreOffice, Actions no ejecutado); AC-A04-19 cumplido; **AC-A04-20 pendiente de decisión del usuario**.

## 4. Controles negativos (CN-01 a CN-07)

| CN | Resultado |
|---|---|
| CN-01 documento sin los valores | DOCX D09 y PDF P10: texto sin cambios, sin hallazgos inventados; selección sobre intervalo inexistente rechazada (`localization_ambiguous`) |
| CN-02 ocurrencia idéntica no seleccionada | conservada en D01, D04 (NFD) y P01 (`v1.b`), P04 (`v5.b`); no se exige ausencia global |
| CN-03 todas las ocurrencias seleccionadas | D08 y P09: ausencia **global** verificada (motores + escaneo bruto/inflado) |
| CN-04 rectángulo superpuesto sin eliminación | los tres motores siguen extrayendo el texto; la verificación **falla** como se espera |
| CN-05 valor copiado en parte auxiliar | D07 (destino de hipervínculo, texto alternativo) y P08 (URI de `Link`): el escaneo de residuos los **detecta** (son `N`, no se eliminan) |
| CN-06 geometría ambigua / sustituto que no cabe | P06 (valor partido en dos líneas: concatenación artificial), `replacement_overflow`, `substitute_glyph_unavailable`, ligadura parcial, uso parcial de objeto compartido, resultado de campo: **fallo explícito** |
| CN-07 cobertura parcial | imagen (DOCX y PDF), ecuación, texto libre en numeración, trazados pintados: se procesan con `N` declarado y `document_coverage_complete = false` |

## 5. Conclusiones separadas

Clasificación: **(V)** viable dentro del subconjunto probado · **(R)** viable con restricciones enumeradas · **(I)** no viable o evidencia insuficiente. «Subconjunto probado» = los fixtures sintéticos de este informe; **ninguna conclusión se extiende a documentos de Word/Acrobat reales**.

| Formato | Eliminación (valor retirado sin sustituto) | Sustitución sintética | Restitución mecánica mínima |
|---|---|---|---|
| **DOCX** | **V** | **V** (el sustituto toma el formato del primer run del valor; reflujo admitido) | **R**: texto y estructura restituidos por posición validada; el formato de los runs **dentro** del valor (p. ej. negrita en medio de un valor repartido) **no** se restituye (por construcción; no se midió con una prueba dedicada) |
| **PDF** | **R** | **R** | **R** |

**Restricciones enumeradas (PDF):** (1) subconjunto de fuentes simples con código→Unicode fiable y `/Widths` (Type1 no incrustada; TrueType simple incrustada); (2) sustituto **no más ancho** que el original y con glifos disponibles en la fuente; (3) unidad = página; intervalos que cruzan líneas/columnas/objetos distintos no se transforman; (4) objeto Form compartido: todos los usos o ninguno; (5) sin `Tc/Tw/Tz/Ts/TL`; (6) la restitución no recupera el kerning interno de los valores; (7) rechazos D-A04-02 vigentes (cifrado, firma, incremental, reparación, formularios, capas, adjuntos, acciones ejecutables, anotaciones no autorizadas, `/ActualText`).
**Restricciones enumeradas (DOCX):** rechazo de revisiones, texto oculto, cuadros de texto, objetos incrustados, datos enlazados, referencias externas, firmas, cifrado, macros, gráficos; resultados de campos **no transformables**; imágenes, destinos de enlace, instrucciones de campo y texto alternativo permanecen `N` (sin cobertura).

**No viable o evidencia insuficiente (I):** (a) fuentes **Type0/CID**, subconjuntos CFF y Type3 en PDF (rechazadas; no se evaluó su viabilidad: **riesgo material para documentos reales**, ver §9); (b) documentos reales de Word/Acrobat/otros generadores, PDF con object streams o `/Contents` en arreglo, DOCX con varias secciones, rotaciones oblicuas y de 270°, documentos grandes; (c) renderizado de DOCX con **Word**; (d) cifrado y firma reales (los fixtures son sintéticos); (e) **texto convertido a trazados**: no se puede detectar que unos trazados representen texto (limitación observada; queda declarado `N`); (f) GitHub Actions.

## 6. Esfuerzo observado de la vía PDF (EXP-10)

La vía `pikepdf`/QPDF, sin intérprete PDF general y sin dependencia comercial, **funcionó para el subconjunto probado**. Indicadores (no son un límite de producto ni una prueba): el módulo `pdf_tool.py` tiene **606 líneas no vacías y 477 sentencias** (inventario, extracción, reescritura, restitución y comprobación del programa de fuente), tras formateo estándar, sin código comprimido. Capacidades **necesarias e implementadas**: análisis del flujo de contenido, mapa código→Unicode (`ToUnicode`/WinAnsi/Differences+AGL), anchos por glifo, separadores sintetizados, recursión en Form XObjects con conteo de usos, reconstrucción de `TJ` con compensación, comprobación del cmap de la fuente incrustada, restitución por posición. Capacidades **necesarias y no implementadas** (fuera del subconjunto; esfuerzo no medido): Type0/CID y subconjuntos CFF (mapa CID→GID inverso, `/W`, disponibilidad de glifos), estado de texto `Tc/Tw/Tz/Ts/TL`, recortes y CTM no triviales para geometría, clonado de Form XObjects para uso parcial, fuentes sin `/Widths` (métricas AFM), escritura vertical/derecha a izquierda. **Conclusión:** para el subconjunto probado no hubo esfuerzo material fuera de alcance; extenderlo a las fuentes más comunes en PDF reales (Type0 con subconjunto) **sí** exigiría trabajo adicional no estimado aquí.

## 7. Fidelidad, residuos y renders revisados

- **Método de comparación:** PDF — PDFium escala 1,5 (108 ppp) y Poppler 90 ppp, escala de grises, tolerancia 0; máscara = banda completa de la línea de cada ocurrencia seleccionada (caja de caracteres de PDFium alineada con el oráculo; para P08 también la anotación eliminada, con margen). DOCX — LibreOffice 7.4.7 (`--convert-to pdf`) + PDFium escala 1,0; fuentes: Liberation Sans/DejaVu. **Un porcentaje global de similitud no se usó como evidencia.**
- **Revisión manual (efectuada por quien implementó, observando las imágenes):** se inspeccionaron las **16 páginas de PDF** de cada ambiente (macOS y Linux) y las **14 imágenes de DOCX** (13 casos; solo Linux; `outputs/renders/<env>/`, montaje antes | después). Observaciones: (1) los valores seleccionados desaparecen y el texto no seleccionado queda en su posición; (2) los sustitutos cortos y la eliminación dejan **huecos visibles** en PDF (compensación de avance); (3) en DOCX la tabla combinada/anidada se conserva, los encabezados `first/default/even` y las notas se mantienen y los sustitutos largos reflujan la línea; (4) en D01 el formato mixto del valor se reduce al del primer run; (5) el icono del comentario PDF desaparece y el enlace permanece; (6) P04 muestra los códigos sintéticos (viñeta/acento) que representan emoji y combinante: la correspondencia con glifos es artificial por diseño; (7) P11 muestra la fuente DejaVu incrustada intacta. Los renders de Poppler no se guardaron (solo sus métricas).
- **Residuos:** búsquedas en todas las partes (DOCX) y en archivo + flujos inflados (PDF) en UTF-8/16, referencias de carácter, codificación porcentual y hexadecimal; **no sustituyen** la extracción de varios motores (los valores partidos en `TJ`/runs no aparecen como bytes literales).

## 8. Límites, *skips* y lo no ejecutado

- **No ejecutado:** GitHub Actions; Word; PDF/DOCX de generadores reales; Type0/CID; macOS sin renders DOCX (13 comprobaciones pendientes por falta de LibreOffice); x86_64.
- **Límites de experimento** ([contrato §16](document-processing.md#16-límites-conservadores-para-los-experimentos-e-a04-20)): tamaño, partes, páginas, descompresión (declarada y producida) y unidades se hacen cumplir en código; duración y memoria se **midieron** (pico 405,6 MB en macOS y 256,3 MB en Linux; experimento completo 4,8 s y 22,7 s) pero no se hacen cumplir.
- **Suites de A01/A02/A03:** no se repitieron. Verificación focalizada de empaquetado: `uv build` en una carpeta temporal; la wheel no contiene `experiments/`; el sdist **sí** incluía `experiments/a04/pyproject.toml` y `uv.lock` por los patrones sin ruta del `include`, y se añadió `exclude = ["experiments"]` a su sección en `pyproject.toml` (sin cambios de versión, dependencias ni `uv.lock`); el sdist resultante tiene 0 entradas de `experiments/`. No se ejecutó la prueba `-m dist` de A03.
- Las comprobaciones comparten el código del mismo repositorio; los verificadores son independientes del **transformador** (otros motores o parser), pero no de las decisiones de diseño del autor.

## 9. Consecuencia para A05, A07 y A08 y decisiones pendientes

- **A05 (trabajos, archivos):** no está bloqueada. Debe conservar el contrato de identidad (el mapa se liga a `file_sha256`, versión y configuración del extractor), la unidad (párrafo DOCX / página PDF), los motivos de rechazo cerrados y límites **de producto** propios (los de §16 son de experimento); debe tratar el documento como entrada no confiable (bombas por bytes producidos, DOCTYPE).
- **A07 (DOCX):** puede apoyarse en lo demostrado dentro del subconjunto, con rechazos de D-A04-02 y las restricciones de §5; requiere definir en su alcance el tratamiento del formato del sustituto, los resultados de campos y las partes `N` (imágenes, destinos de enlace, texto alternativo).
- **A08 (PDF):** viable **con restricciones** sobre el subconjunto probado, pero **la cobertura de fuentes reales (Type0/CID con subconjuntos) está sin evidencia y es la principal incertidumbre material**: sin ella, un conjunto grande de PDF reales se rechazaría. No hay un bloqueo demostrado; es evidencia insuficiente.
- **Decisión del usuario (no imprescindible para continuar, pero necesaria para cerrar A04):** aceptar o no el cierre de A04 con estas conclusiones y decidir si antes de A08 se exige una comprobación con un corpus de PDF reales (B-A04-11/12) o se acepta el riesgo de rechazo por fuentes no soportadas. **No se cierra A04 ni se habilita A07/A08 sin esa decisión** (AC-A04-20).
- **Backlog:** [B-A04-01 a 19](decisions/A04-decisions.md#5-backlog-fuera-de-los-criterios-de-aceptación).
