# A08 — Protocolo de la etapa 2B (PDF digital con selecciones de una línea)

> **Estado: revisión 1, FIJADO antes de la primera medición candidata (`c89b202`); medición EJECUTADA en macOS y Linux local: [informe 2B](a08-stage2b-report.md) (2/10, piso no cumplido, sin bloqueos; faltan Word y Google Docs).** Fijado (autorización del usuario sobre el estado local de la etapa 2, último commit `2952f4f`). Fija los criterios, el corpus, el piso, los controles y el procedimiento. **No** reabre la etapa 2: su resultado oficial (`0/20`) y sus variantes informativas (`5/20`, `6/20` con RT) quedan como evidencia histórica en el [informe de la etapa 2](a08-feasibility-report.md) y su [protocolo](a08-pdf-corpus-protocol.md). Esta etapa **no** autoriza la etapa 3 (runtime, migraciones, permisos, dependencias de producto), que requiere la aprobación posterior del usuario. Ningún criterio de este documento se ajusta después de medir.

Documentos: [informe de la etapa 2](a08-feasibility-report.md) · [protocolo de la etapa 2](a08-pdf-corpus-protocol.md) · [contrato propuesto](pdf-protection.md) · [decisiones](decisions/A08-decisions.md) · [especificación](features/A08-proteccion-y-restauracion-pdf.md). Datos: `experiments/a08/corpus2b/` (manifiesto, fuentes, instructivo de Word y Google Docs, congelamiento).

## 1. Objetivo y alcance

Determinar si hay base para una primera versión de protección y restauración de **PDF con texto digital y selecciones contenidas en una sola línea**. Hallazgos controlados (oráculo), sustitutos exactos de A06 (`[[CCE1:…]]` de 25 caracteres y `[REDACTED]`).

**Fuera de la etapa 2B:** runtime, migraciones, permisos, dependencias de producto, A09 (inferencia y calidad de detección), selecciones de varias líneas (criterio 6), ampliar reglas de filetes, subrayados, trazados o imágenes sin cobertura demostrada (criterio 6), y la lista de imágenes declarada por el host (P-A08-03, pendiente).

## 2. Criterios aprobados por el usuario (C-2B-01 a 06) y cómo se aplican

| Criterio | Regla fijada | Dónde |
|---|---|---|
| **C-2B-01 — TB** | La banda vertical de cada zona es la **caja tipográfica** de la línea: unión de las cajas «loose» de PDFium (ascendente y descendente de la fuente) de los caracteres de la línea, recortada contra la tinta de cualquier otra línea. Los límites horizontales (`r_z1`, `r_line`) se calculan con esa banda. El margen de 0,3 em usa el **em efectivo** (tamaño de `Tf` × escala vertical de `Tm` × `CTM`; §8, N-2B-1). Orden de intentos sin cambios: (1) fuente original `Z1`, (2) candidata `Z1`, (3) original `Z2`, (4) candidata `Z2`; `Z2` solo si `Z1` no encaja, con un factor `f ∈ [0,60; 1]` común a toda la línea. | `oracle.zone_for(band_mode="typographic")`, `transform._line_plan` |
| **C-2B-01 (verificación)** | El verificador comprueba, sin usar números del plan: el texto no seleccionado dentro de `Z2` (Unicode, orden), su **legibilidad** (cada avance comprimido por el mismo `f` y `f ∈ [0,60; 1]`; el sustituto comprimido por ese mismo `f`), su **geometría esperada** (§3) y la línea base y altura de cada glifo. Los elementos gráficos y los enlaces ajenos se conservan **aunque caigan dentro de la máscara**: operadores no textuales idénticos y render de la página sin texto idéntico en toda la página (VB), anotaciones idénticas (VN). | `verify.z2_geometry`, VB, VN |
| **C-2B-02 — RZ** | Al restaurar una línea `Z2`, `[REDACTED]` acompaña a su línea: vuelve al inicio original del valor redactado, comprimido al ancho original del valor si es más ancho, **nunca por debajo de 0,60**. Si aun a 0,60 es más ancho, el exceso desplaza a la derecha lo que le sigue en la línea; si eso saca algún glifo del límite autorizado de la línea, la restauración **falla cerrada** (`restoration_layout_unavailable`). Conserva su texto y su orden; el valor redactado sigue ausente (VM). Controles: redacción y seudonimización en ambas secuencias y dos seudonimizaciones en una línea (CS12, CS13, CS14, CS16, CS17 y la plantilla T5). | `transform.restore`, `verify.restored_excess` |
| **C-2B-03 — AT** | `/ActualText` se admite **solo** si coincide exactamente con el texto de sus glifos **y** el tramo queda íntegramente fuera de toda selección. Una discrepancia rechaza el documento (`alternate_text_duplication`); una intersección, total o parcial, rechaza la selección (`selection_not_transformable`). Un glifo de un tramo `/ActualText` tampoco puede moverse en `Z2` (el tramo dejaría de coincidir; `zone_plan_unjustified`). El verificador busca residuos también en las propiedades de contenido marcado (`/ActualText`, `/Alt`, `/E`) y exige que esas propiedades no cambien. `/Alt` y `/E` siguen rechazándose (etapa 2). | `pdfmodel` (EMC), `transform.localize`, `verify.marked_content` |
| **C-2B-04 — RT** | **Diagnóstico, nunca éxito.** La comparación oficial fuera de las zonas sigue siendo **0 px** en PDFium y en Poppler. Cuando solo falla PDFium, se registra si cada píxel distinto se explica por el raster de un glifo **sin cambio de posición extraída** desplazado como máximo 1 px: por glifo, el desplazamiento `s ∈ {−1, 0, 1}²` que mejor reproduce su región (caja ajustada + 2 px, es decir, ≤ 1 px de desplazamiento más ≤ 1 px de suavizado) y diferencia ≤ 32 niveles; donde dos regiones se tocan, también vale la coincidencia con el original a ≤ 1 px. El caso cuenta como **fallo** y se informa aparte; si algún píxel queda sin explicar, es **pérdida colateral** (bloqueo). | `verify.rt_diagnostic` |
| **C-2B-05 — independencia de las zonas** | Las zonas autorizadas salen **solo del original y del oráculo congelado**: `Z1` = `[x0, banda, r_z1]`; `Z2` = `[min x0, banda, min r_line]` de las selecciones de la línea. El plan solo aporta qué selecciones agrupa y si declara `Z1` o `Z2`; nunca fija un límite. Se rechaza (VZP) todo plan que exceda la zona autorizada, que coloque un sustituto fuera de ella o que reparta las selecciones de una línea `Z2` entre planes. La geometría esperada se deriva de las cajas «loose» del original (origen y avance), del oráculo y de lo que el derivado dibuja; el factor del plan solo se **contrasta** (|f_plan − f_medido| ≤ 0,002). Controles: CV31 (el plan amplía su zona para ocultar una alteración) y CV32, CV35 (avance o factor del plan falseados). | `verify.zones_of`, `check_plan`, `z2_geometry` |
| **C-2B-06 — fuera de alcance** | Sin selecciones de varias líneas ni reglas nuevas de recursos. Reglas activas, fijadas: **`RP3`, `RP5`, `RP7`** (las que sobrevivieron a los controles adversarios de la etapa 2). Los rechazos explícitos y los controles adversarios CA01–CA07 se conservan y deben rechazarse (`uncovered_content`). P-A08-03 sigue pendiente. | `run2b.RULES`, `controls2b` |

## 3. Geometría esperada de `Z2` (protegido)

Coordenadas de origen (borde izquierdo de la caja «loose» de PDFium = origen del glifo; ancho = avance). Para las selecciones `k` de la línea, en orden: `a_k` = inicio de la zona de la selección (máximo entre `x0` del oráculo y el origen del primer carácter); `e_k` = fin del avance del último carácter seleccionado; `w'_k` = ancho medido del sustituto en el derivado. Para todo carácter no seleccionado `c` de la zona, con origen original `x_c`:

`x'_c = a_1 + f·(x_c − a_1) + Σ_{k a la izquierda} (w'_k − f·(e_k − a_k))`, con tolerancia 0,05 pt + `f·g_k` por selección (`g_k`: hueco original medido entre el último carácter seleccionado y el siguiente, que absorbe `Tc`).

`f` se mide por mínimos cuadrados sobre pares de glifos consecutivos no seleccionados; el sustituto debe estar comprimido por el mismo `f` (±0,003, respecto de su ancho natural en la fuente candidata congelada); el primer sustituto empieza en `a_1` (±0,05 pt); cada avance de glifo movido mide `f` veces el original (±0,05 pt). Un sustituto en una fuente que no sea la candidata congelada no se puede medir con este verificador y falla cerrado (VL). En `Z1`, la compresión medida del sustituto debe estar en `[0,60; 1]` y empezar dentro de la zona.

**Restaurado:** cada carácter no seleccionado vuelve a su posición original (±0,05 pt), más el exceso RZ de las redacciones a su izquierda, derivado del original y de la fuente candidata (§2, C-2B-02).

## 4. Corpus (10 casos positivos, PDF exportados nativamente)

Dos casos por familia. Plantilla reutilizada **T1** (carta de la etapa 2; todas sus selecciones son de una línea y ejercita `Z2` dentro de un párrafo justificado) y plantilla **nueva T5** (ficha de registro, una página, Noto Sans) en las cinco familias. T2 no se reutiliza, para que la plantilla repetida sea la misma en todas las familias; T3 y T4 quedan fuera por el criterio 6. La elección se registró en el manifiesto **antes** de ejecutar el candidato sobre cualquier PDF de la etapa 2B.

| Caso | Familia | Plantilla | PDF | Estado al fijar |
|---|---|---|---|---|
| `W1`, `W5` | Word | T1, T5 | exportación del usuario (`corpus2b/EXPORT-W-G.md`) | pendientes |
| `G1`, `G5` | Google Docs | T1, T5 | exportación del usuario | pendientes |
| `L1` | LibreOffice | T1 | **reutilizado**: `corpus/L1/L1.pdf` (congelado en `5f1af03`) | — |
| `L5` | LibreOffice | T5 | `exporters/export_2b.sh` (misma imagen y opciones que la etapa 2) | — |
| `C1` | Chromium | T1 | **reutilizado**: `corpus/C1/C1.pdf` | — |
| `C5` | Chromium | T5 | `exporters/export_2b.sh` | — |
| `M1` | macOS | T1 | **reutilizado**: `corpus/M1/M1.pdf` | — |
| `M5` | macOS | T5 | `exporters/export_2b.sh` (ruta AppKit docx, como `M1`) | — |

**Transparencia sobre los reutilizados:** los PDF `L1`, `C1` y `M1` ya se midieron en la etapa 2. En su variante informativa TB+AT+RZ, `L1` y `C1` completaron el ciclo y `M1` falló solo por el rasterizado estricto de PDFium. Su oráculo 2B se reconstruye con la banda tipográfica y el em efectivo.

**T5** (una página; cada línea con valores es un párrafo propio, alineado a la izquierda y más corto que la caja de texto): valor fragmentado entre operadores (`Joaquín` en cursiva dentro de «Joaquín Ortega Lázaro»), Unicode (`Zoë Ångström-Łęcka`), valor corto a mitad de línea (`Bea`), dos seudonimizaciones en una línea (`Teo Vidal`, `Ruth Sanz`), redacción y luego seudonimización (`X0000001` y `Lucía Prieto`), seudonimización y luego redacción corta (`Pablo Ferrer` y `350 €`), un correo seleccionado (`recepcion@example.test`) y su **ocurrencia idéntica no seleccionada** al pie. T1 aporta el nombre con su primer tramo en negrita, el valor corto `Gil` a mitad de una línea justificada, el correo con su ocurrencia permitida, la redacción del documento y la celda de tabla.

**Fijación por caso:** antes de ejecutar el candidato sobre un PDF se registran procedencia, SHA-256, oráculo y zonas (`a08/intake2b.py` → `corpus2b/<caso>/{provenance,oracle}.json` y `corpus2b/FREEZE.json`), en un commit. Un caso congelado no se reemplaza. Los PDF con fuentes incrustadas no redistribuibles van a `corpus2b-private/` (fuera de Git) con su hash y su receta.

## 5. Piso y bloqueos

El piso se cumple solo si se dan todas estas condiciones:

- **≥ 8/10 ciclos completos.** Un ciclo es: apertura, admisión, localización, plan, transformación, reapertura, verificación estricta del protegido, restauración solo desde el derivado y el vínculo, y verificación estricta del restaurado.
- **≥ 1 éxito en cada familia.**
- **≥ 1 caso Type0/CID subconjuntado nativo** en el que el texto **seleccionado** esté en esa fuente y complete el ciclo.
- **Cero negativos aceptados indebidamente.**
- **Cero bloqueos.**
- **Todos los controles del verificador detectados.**

Las demás reglas del cómputo:

- **Denominador fijo de 10.** Un rechazo seguro de un caso positivo cuenta como fallo de cobertura.
- **Familias faltantes.** Un caso faltante cuenta como fallo, y una familia sin ningún éxito impide declarar cumplido el piso; el resultado parcial se entrega igual.
- **RT nunca cuenta.**
- **Bloqueos.** Bloquea el avance cualquiera de estos hallazgos en una salida del candidato:
  - un residuo (VK, VX, VP, VE o VS);
  - una pérdida colateral (VT, VB, VN, VG, VA, Poppler fuera de la máscara, o un píxel de PDFium sin explicar por el diagnóstico RT);
  - un negativo aceptado.

## 6. Controles (fuera del denominador)

| Grupo | Controles | Resultado esperado |
|---|---|---|
| **CA**: objetos que representan texto | CA01–CA07 (etapa 2) | rechazo `uncovered_content` con las reglas activas |
| **CR**: rechazos seguros | CR01–CR23 (etapa 2) | el motivo fijado en la etapa 2 |
| | CR27: `/ActualText` distinto de sus glifos | `alternate_text_duplication` |
| | CR28: `/ActualText` que contiene la selección | `selection_not_transformable` |
| | CR29: `/ActualText` que se solapa parcialmente con la selección | `selection_not_transformable` |
| | CR30: restauración con un derivado que no es el exacto (un byte distinto) | `derivative_mismatch` |
| | CR31: restauración con el vínculo alterado | `binding_invalid` |
| **CS**: positivos estructurales, se informan | CS01–CS13 (etapa 2; CS12 dos seudonimizaciones, CS13 seudonimización y luego redacción) | — |
| | CS14: redacción y luego seudonimización | — |
| | CS15: `/ActualText` fuera de la selección pero dentro de la región que `Z2` movería | rechazo del plan `Z2` |
| | CS16: redacción corta sin sitio | `restoration_layout_unavailable` |
| | CS17: redacción corta con sitio | ciclo RZ con exceso |
| **CV**: alteraciones de una salida válida que el verificador debe rechazar | CV01–CV30 (etapa 2), sobre `L1` 2B y sobre `L2` de la etapa 2 con oráculo tipográfico de control | detectados |
| | CV31: el plan amplía su zona `Z2` para ocultar una alteración | detectado |
| | CV32: sufijo `Z2` desplazado 1 pt con el avance del plan falseado | detectado |
| | CV33: valor copiado en un `/ActualText` | detectado; se registra además el escaneo de residuos de contenido marcado por sí solo |
| | CV34: `/ActualText` ajeno añadido | detectado |
| | CV35: factor del plan falseado | detectado |

## 7. Entradas por componente (separación oracle / restaurador)

- **Verificador:** lee el original y el oráculo congelado (para expectativas), además del derivado o del restaurado.
- **Restaurador:** recibe **solo** los bytes del derivado, el vínculo autenticado (SHA-256 del derivado exacto + HMAC-SHA256 con una clave de custodia simulada que nunca se escribe) y la tabla token → valor. Nunca recibe el original ni el análisis.

Ambas cosas se registran por caso en `inputs`.

## 8. Notas fijadas antes de medir

- **N-2B-1 (defecto del oráculo de la etapa 2):** el margen de 0,3 em usaba el tamaño de `Tf` de PDFium, que no es el em cuando el exportador escala con `Tm` o `cm`. En Chromium el margen salía 4,4 pt en vez de 3,3 pt, y en macOS (Quartz, `Tf 1`) 0,3 pt en vez de unos 3,6 pt. Los oráculos 2B usan el em efectivo. Los de la etapa 2 no se reescriben.
- **N-2B-2 (rasterizado de PDFium, evidencia previa a la medición):** en un control sintético, el prefijo «Árbol:» se rasteriza 1 px desplazado cuando el resto de su operador cambia, aun con los mismos bytes y las mismas posiciones extraídas. Basta conservar solo «Árbol: A» como texto del operador para que ocurra; con «Árbol: Ana Gil», no. Es una heurística de PDFium, no un cambio de geometría. Por eso RT es solo diagnóstico (C-2B-04).
- **Desarrollo y medición:** el prototipo se depuró contra los controles y contra PDF que **no** forman parte del corpus 2B (`L2`, `C2` y `M2` de la etapa 2). La medición informada es la ejecución completa con el código de un commit identificado. Si un defecto del código obliga a repetir una medición, se conserva la anterior y se declara.
- **Reproducción de la etapa 2:** sus resultados se reproducen con el código de `2cd779a`. Esta etapa cambia el verificador y el planificador compartidos.

## 9. Procedimiento y reproducción

```bash
cd experiments/a08 && exporters/export_2b.sh work/stage2b-export
```

```bash
cd experiments/a08 && for c in L1 C1 M1; do .venv/bin/python -m a08.intake2b $c; done
```

```bash
cd experiments/a08 && ./run2b_macos.sh
```

```bash
cd experiments/a08 && ./run2b_linux.sh
```

Exportaciones de Word y Google Docs: `corpus2b/EXPORT-W-G.md`. Al recibirlas, cada una se ingresa con `a08.intake2b <caso> <pdf> <procedencia.json>`, se congela en un commit y se mide con el mismo código.
