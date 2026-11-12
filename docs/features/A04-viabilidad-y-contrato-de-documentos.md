# A04 — Viabilidad y contrato de documentos

> **Estado: DOCUMENTADA (solo documentación). Experimentos PENDIENTES; viabilidad NO demostrada; ninguna biblioteca validada; sin código, dependencias, versión (`0.3.0`), esquema ni permisos nuevos.** Ningún soporte de PDF/DOCX existe en `0.3.0`. Este documento define alcance, exclusiones, experimentos y aceptación; el contrato permanente vive en [procesamiento de documentos](../document-processing.md). No hay informe de viabilidad: se creará **después** de ejecutar los experimentos (estructura en §8).

- Fuente: `STAGE_3_PLAYBOOK.md` revisión 6 (11 de noviembre de 2026), más las decisiones posteriores ([A01](../decisions/A01-decisions.md), [A02](../decisions/A02-decisions.md), [A03](../decisions/A03-decisions.md), [A04](../decisions/A04-decisions.md)); estas **prevalecen** sobre el playbook donde difieren.
- Repositorio: `cawbase-content-engine` · rama: `feature/a04-document-feasibility-contract` (desde `ae19ef3`, merge de A03).
- Dependencias: A01, A02 y A03 **cerradas y aceptadas** (A03 `0.3.0`: commit de implementación `20d9211`, documentación/evidencia `e3ca6da`, merge `ae19ef3`; resultado de GitHub Actions sobre A03 no verificado con la evidencia disponible). Habilita A05 (trabajos) y A07/A08 **solo si** el resultado de viabilidad lo permite y el usuario lo decide.

## 1. Objetivo, alcance y exclusiones

**Objetivo.** Definir el contrato documental y comprobar **después**, con documentos sintéticos y hallazgos controlados, si DOCX y PDF pueden protegerse con el nivel de conservación exigido por D-A04-03, con bibliotecas compatibles con D-A04-01.

**Esta entrega (documental) incluye:** contrato permanente (seis capas, identidad, mapeo, unidades, cobertura); matrices de soporte/rechazo DOCX y PDF; evaluación de bibliotecas y licencias con fuentes oficiales; batería de experimentos con fixtures y controles negativos; criterios de aceptación verificables antes de ejecutar; estructura del informe futuro.

**Excluye (hoy y en A04):** código, fixtures reales, generación de documentos, instalación de dependencias, ejecución de experimentos o suites. Fuera de alcance de la feature: A05 (trabajos, colas, endpoints documentales, almacenamiento, cargas, consulta, retención, idempotencia, recuperación); A06 (sustituciones y correspondencias productivas, criptografía, custodia, restauración autorizada); A07/A08 (protección/restauración completas); A09 (modelo real, calidad de detección, tokenizer, segmentación por modelo); integración con CAWbase, infraestructura y publicación. No se promete soporte universal ni pipeline productivo.

## 2. Decisiones

Aprobadas (`D-A04-01` a `03`, texto en [A04-decisions §2](../decisions/A04-decisions.md#2-decisiones-aprobadas)): dependencias compatibles con productos cerrados (MPL-2.0 admitida con obligaciones; PyMuPDF solo alternativa; sin dependencia comercial ni reducción silenciosa de garantía; licencia definitiva en A10); contenido auxiliar eliminable y rechazos iniciales (diferenciando adjuntos/objetos incrustados de recursos ordinarios); fidelidad mínima por formato con fallo explícito. Elecciones técnicas `E-A04-01` a `22`: [A04-decisions §3](../decisions/A04-decisions.md#3-elecciones-técnicas-derivadas) (no son decisiones de producto).

## 3. Mapa del contrato

| Tema | Documento permanente |
|---|---|
| Seis capas, afirmaciones separadas | [procesamiento §1, §9](../document-processing.md#1-seis-capas-seis-afirmaciones-distintas) |
| Disposiciones S/E/N/R | [§2](../document-processing.md#2-disposición-de-cada-elemento-e-a04-02) |
| Identidad y versionado del mapa | [§3](../document-processing.md#3-identidad-y-versionado-e-a04-04) |
| Estructuras conceptuales | [§4](../document-processing.md#4-estructuras-conceptuales) |
| Unidades Unicode, orden, ambigüedad | [§5](../document-processing.md#5-unidades-unicode-y-orden-de-extracción) |
| Referencias DOCX / PDF | [§6](../document-processing.md#6-docx-referencias), [§7](../document-processing.md#7-pdf-referencias) |
| Integración con A03 | [§8](../document-processing.md#8-integración-con-a03) |
| Reglas generales de rechazo | [§10](../document-processing.md#10-reglas-generales-de-aceptación-y-rechazo-del-documento) |
| Matrices DOCX / PDF | [§11](../document-processing.md#11-matriz-de-soporte-docx), [§12](../document-processing.md#12-matriz-de-soporte-pdf) |
| Comentarios y metadatos eliminables | [§13](../document-processing.md#13-comentarios-y-metadatos-eliminables-d-a04-02) |
| Bibliotecas y licencias | [§15](../document-processing.md#15-bibliotecas-candidatas-licencias-y-criterio-de-selección) |
| Límites de experimento | [§16](../document-processing.md#16-límites-conservadores-para-los-experimentos-e-a04-20) |

## 4. Reglas comunes de los experimentos

1. **Ejecución posterior y local.** Sin red durante el procesamiento; sin activar macros, scripts ni referencias externas; límites de [§16 del contrato](../document-processing.md#16-límites-conservadores-para-los-experimentos-e-a04-20). Artefactos en la ubicación **propuesta** `experiments/a04/` (no existe; ver [contrato §17](../document-processing.md#17-estructura-propuesta-de-los-artefactos-experimentales-no-disponible)): fuera de `src/`, fuera de la wheel y del `include` del sdist; no se cambia el empaquetado ahora.
2. **Fixtures sintéticos con manifiesto independiente.** Cada fixture lleva un manifiesto escrito **a mano o por el generador del fixture** (nunca por la extracción que se valida) con: `fixture_id`, hash del archivo, valores sintéticos y sus **ocurrencias** (parte/página, posición esperada), partes afectadas, **hallazgos seleccionados** (cuáles ocurrencias se transforman), resultado esperado y **estructura de referencia** (partes/objetos, páginas, estilos). Valores evidentemente falsos (`example.test`, nombres ficticios); ningún dato real.
3. **Oráculo independiente:** la verificación compara contra el manifiesto y contra una inspección estructural con herramienta distinta de la transformadora cuando sea viable; si comparten parser o motor, se **declara la limitación**.
4. **Resultados registrados como** `demostrado`, `limitación observada` o `pendiente` por fila de la matriz; un resultado negativo completa la investigación pero no habilita A07/A08.
5. **Unidades A03:** los textos pasan por `analyze_text` con el mock en **unidades acotadas** (≤ 8192 puntos de código); alcance local declarado; sin agregación de puntuaciones; fixtures de mock con `MockFixture.for_request`, `locate` y `full_coverage` de A03. El mock no mide calidad.
6. **Versiones fijadas** de bibliotecas, binarios y fuentes en cada ejecución; se registran licencias transitivas.

## 5. Controles negativos (obligatorios en la batería)

| ID | Control | Resultado esperado |
|---|---|---|
| CN-01 | Documento **sin** los valores seleccionados | La transformación no cambia el contenido; el informe indica «sin ocurrencias»; no inventa hallazgos |
| CN-02 | **Ocurrencia idéntica no seleccionada** | La ocurrencia no seleccionada permanece intacta (precisión de localización); la ausencia global **no** se exige |
| CN-03 | **Todas** las ocurrencias de un valor seleccionadas | Se verifica ausencia **global** del valor en todo el archivo (incluidas partes auxiliares) |
| CN-04 | **Rectángulo superpuesto** sin eliminación real (PDF) | La verificación **falla** (el texto sigue extraíble/presente en el flujo) |
| CN-05 | Valor copiado en **parte auxiliar** (propiedad, texto alternativo, destino de hiperenlace, metadato, `ActualText`) | La verificación de residuos lo **detecta** y falla |
| CN-06 | **Geometría ambigua** o **sustituto que no cabe** | Fallo **explícito** de la transformación (sin reducir fuente ni desbordar en silencio) |
| CN-07 | Valor en elemento `R`/`N` (p. ej. revisión, cuadro de texto, imagen) | `R`: rechazo; `N`: declarado no cubierto, jamás «protegido» |

## 6. Batería de experimentos

Cada experimento: entradas, hipótesis, procedimiento, verificaciones y resultado esperado. **Ninguno se ejecutó.** Estado de todos: `pendiente`.

### EXP-01 — Extracción y mapeo DOCX

- **Entradas:** 8–12 DOCX sintéticos: valor repartido en 2–5 runs; tabla simple, anidada (profundidad 2–3) y con celdas combinadas; encabezado/pie default, first y even; notas al pie/final; hipervínculo; campo simple; control de contenido de texto; fixtures que contienen cada elemento DX de la matriz.
- **Hipótesis:** los elementos `S` se extraen con orden determinista y cada intervalo se mapea a sus piezas; `N` y `R` se identifican en el inventario.
- **Procedimiento:** inventario → extracción → mapa; resolver cada ocurrencia del manifiesto a `SegmentRef`; comprobar `[start,end)` en puntos de código contra `unit.text`.
- **Verificaciones:** (a) el texto de cada unidad coincide con el manifiesto; (b) cada ocurrencia se resuelve a las piezas esperadas; (c) todos los elementos del manifiesto tienen disposición; (d) ninguno `N` figura como cubierto; (e) determinismo en dos extracciones.
- **Esperado:** `S` demostrados; `N`/`R` correctamente clasificados; limitaciones enumeradas.

### EXP-02 — Extracción y mapeo PDF

- **Entradas:** 8–12 PDF sintéticos ≤ 20 páginas: valor fragmentado en operadores `TJ`; múltiples columnas; página rotada 90/180/270 y texto con matriz rotada; fuentes simples y Type0 con `ToUnicode`, y un caso sin `ToUnicode`; Form XObject compartido entre páginas; imagen y página mixta; texto como trazados (control).
- **Hipótesis:** extractor(es) independientes coinciden en el texto; geometría y objetos de contenido permiten localizar; el mapeo insuficiente se detecta.
- **Procedimiento:** extracción con dos vías (p. ej. PDFium y pypdf), conversión de unidades a puntos de código, localización por página/objeto/operador.
- **Verificaciones:** coincidencia de textos entre vías o discrepancias explicadas; ocurrencias resueltas a objeto de contenido (no solo caja); detección del Form XObject compartido; `ToUnicode` ausente ⇒ rechazo/fallo.
- **Esperado:** subconjunto `S` demostrado o `limitación observada` por fila de matriz.

### EXP-03 — Unicode, ligaduras y ocurrencias idénticas

- **Entradas:** texto con compuesto/descompuesto (`é` vs `e`+U+0301), fuera del BMP (😀), ZWJ, `\r\n`, tabulaciones, ligaduras `fi`/`ffi` en PDF y ocurrencias idénticas.
- **Hipótesis:** los índices en puntos de código se conservan en ambos formatos; las relaciones 1:n están representadas; la selección parcial de una ligadura falla.
- **Verificaciones:** `[start,end)` coincide con `len(str)` de Python; separadores sintetizados no localizables; ocurrencias idénticas se distinguen por posición; la selección parcial de una ligadura produce `localization_ambiguous`.
- **Esperado:** conversión de unidades correcta o limitación enumerada.

### EXP-04 — Unidades por A03 y su mock

- **Entradas:** unidades de EXP-01/02 (≤ límites de A03).
- **Procedimiento:** fixtures del mock con `build_inference_request`, `MockFixture.for_request`, `locate` y `full_coverage`; casos con cobertura completa y parcial controlada (`not_evaluated_ids`, regiones parciales); resolución de hallazgos A03 a `SegmentRef`.
- **Verificaciones:** `AnalysisResult` sin cambios; un hallazgo localizado por A03 se resuelve al mismo intervalo del manifiesto; con cobertura parcial el documento **no** se declara completo; las unidades no se agregan; alcance local declarado.
- **Esperado:** el flujo funciona con el contrato aceptado; si requiere cambios en A03, se **informa** (no se reabre A03 sin decisión del usuario).

### EXP-05 — Eliminación real y sustitutos de distintas longitudes

- **Entradas:** DOCX y PDF con valores seleccionados; sustitutos sintéticos más cortos, iguales y más largos, con y sin glifos disponibles.
- **Procedimiento:** transformar con la vía candidata (D-A04-01); para PDF reescribir operandos de texto, no superponer; para DOCX sustituir en runs/celdas.
- **Verificaciones:** apertura sin reparación; validez estructural; extracción posterior sin el valor seleccionado; contenido no seleccionado intacto; CN-02, CN-04, CN-06; sustituto ilegible o desbordado ⇒ fallo.
- **Esperado:** viable dentro del subconjunto probado, viable con restricciones o no viable, **por formato**.

### EXP-06 — Eliminación de comentarios/metadatos y copias auxiliares

- **Entradas:** DOCX con comentarios (hilos), propiedades núcleo/aplicación/personalizadas y miniatura; PDF con `/Info`, XMP y anotaciones de comentario.
- **Verificaciones:** clases eliminadas registradas; paquete/archivo válido; búsqueda de residuos en todas las partes/objetos (codificaciones y compresión); partes huérfanas; CN-05; el contenido visible y las demás anotaciones no cambian.
- **Esperado:** eliminación completa de las clases `E` o limitaciones enumeradas.

### EXP-07 — Rechazos por familia excluida

- **Entradas:** un fixture por familia: revisiones; texto oculto; cuadros de texto; datos enlazados; OLE/paquete/adjunto/macro; gráficos con libro; referencias externas; cifrado (incl. contraseña de usuario vacía); firma; corrupto/reparable; escaneado; mixto; formularios; capas; acciones; `ActualText`/`Alt`; actualización incremental; estructura desconocida.
- **Verificaciones:** rechazo **reproducible** con motivo cerrado, sin reparación silenciosa ni éxito parcial; los `N` se declaran.
- **Esperado:** cada fila `R`/`N` de la matriz confirmada.

### EXP-08 — Fidelidad y ausencia de residuos

- **Procedimiento:** inspección visual **de todas las páginas** de las muestras pequeñas con renders antes/después y regiones afectadas identificadas; DOCX: comparación de estructura y estilos fuera de lo autorizado (admite reflujo); PDF: misma cantidad de páginas, mismas `MediaBox/CropBox/Rotate`, comparación fuera de regiones afectadas con método y tolerancias explícitos; análisis de residuos por parte/objeto/flujo descomprimido en varias codificaciones; CN-03 y CN-05.
- **Registro:** renderizador, versión, fuentes, resolución y condiciones de comparación. **Un porcentaje global de similitud visual no es evidencia suficiente por sí solo.**
- **Esperado:** pérdidas de fidelidad enumeradas y dentro de D-A04-03, o `limitación observada`.

### EXP-09 — Restitución mecánica mínima

- **Alcance:** únicamente **prueba de viabilidad para A07/A08**: correspondencias sintéticas, locales al experimento; **sin** sistema de restauración, cifrado, persistencia ni autorización (A06); no es contrato de restauración.
- **Procedimiento:** tras EXP-05, relocalizar los sustitutos por posición (no por búsqueda ambigua) y reemplazarlos por los valores originales sintéticos; comparar estructura y valores con el original **dentro de los límites acordados**.
- **Verificaciones:** los valores y la estructura se restituyen donde hubo sustitución; el contenido eliminado irreversiblemente (comentarios/metadatos) **no** se recupera; un sustituto modificado o duplicado ⇒ fallo.
- **Esperado:** viable / con restricciones / no viable por formato.

### EXP-10 — Sonda de esfuerzo de la vía PDF de bajo nivel

- **Propósito:** detectar si transformar PDF con `pikepdf`/QPDF exige un esfuerzo **material fuera de alcance**; **no** se desarrolla un motor PDF general.
- **Alcance acotado:** subconjunto PX-01/PX-04/PX-06 (operadores `Tj`, `TJ`, `'`, `"`, `Tf`, `Tm`, `Td`, `T*`, `BT/ET`, `Do`), una fuente por prueba.
- **Indicadores de esfuerzo material** (cualquiera ⇒ se informa): necesidad de añadir glifos a fuentes subconjunto o incrustar fuentes nuevas; necesidad de un intérprete completo del estado gráfico más allá del subconjunto; clonado de Form XObjects; más de ~600 líneas de código experimental para el subconjunto (indicador de esfuerzo, no un límite de producto); imposibilidad de detectar la reparación del parser.
- **Esperado:** informe del esfuerzo observado y de las restricciones; si es material, se solicita decisión del usuario (no se introduce dependencia comercial; no se baja la garantía en silencio).

## 7. Matriz de aceptación

Estado actual de todos los criterios: **pendiente** (se evalúan al ejecutar los experimentos; esta entrega solo los fija **antes** de ejecutar).

| AC | Criterio verificable | Experimento / evidencia | Estado |
|---|---|---|---|
| AC-A04-01 | Archivo de salida abrible con la herramienta designada, estructuralmente válido y sin reparación silenciosa (se registran avisos del parser) | EXP-05, 06, 08 | pendiente |
| AC-A04-02 | Localizadores y mapa consistentes con el archivo exacto (hash) y con el texto exacto (puntos de código); un mapa no se aplica a otro archivo | EXP-01, 02, 03 | pendiente |
| AC-A04-03 | Eliminación **real** del contenido seleccionado; superponer rectángulos no cumple (CN-04) | EXP-05 | pendiente |
| AC-A04-04 | Extracción posterior y revisión estructural (XML, relaciones, objetos, streams) sin el valor seleccionado; no depende solo de buscar bytes literales (fragmentación, codificaciones, compresión) | EXP-05, 08 | pendiente |
| AC-A04-05 | Verificador independiente cuando sea viable; limitaciones declaradas si comparte parser o motor | EXP-02, 08 | pendiente |
| AC-A04-06 | Inspección visual de **todas** las páginas de muestras pequeñas con renders antes/después y regiones identificadas; renderizador, versión, fuentes, resolución y condiciones registrados | EXP-08 | pendiente |
| AC-A04-07 | DOCX: estructura y estilos preservados fuera de cambios autorizados (admite reflujo) | EXP-05, 08 | pendiente |
| AC-A04-08 | PDF: misma cantidad y geometría de páginas; comparación fuera de regiones afectadas con método y tolerancias explícitos | EXP-05, 08 | pendiente |
| AC-A04-09 | Sin texto no seleccionado perdido, sin sustitutos ilegibles ni desbordamientos; si ocurre ⇒ fallo explícito (CN-02, CN-06) | EXP-05, 08 | pendiente |
| AC-A04-10 | Prueba mecánica de restitución de valores y estructura dentro del contrato, sin recuperar contenido eliminado irreversiblemente | EXP-09 | pendiente |
| AC-A04-11 | Rechazos reproducibles para cada familia excluida; cobertura incompleta nunca presentada como éxito de protección completa (CN-07) | EXP-07 | pendiente |
| AC-A04-12 | Ausencia de las ocurrencias seleccionadas distinguida de ausencia global; la global solo se exige con todas las ocurrencias incluidas (CN-02, CN-03) | EXP-05, 08 | pendiente |
| AC-A04-13 | Comentarios/metadatos: clases eliminadas registradas, estructura válida, sin copias residuales (CN-05) | EXP-06 | pendiente |
| AC-A04-14 | Unidades A03 con alcance local, sin agregación; `AnalysisResult` sin cambios | EXP-04 | pendiente |
| AC-A04-15 | Bibliotecas con versión fijada y licencias transitivas, binarios y fuentes registrados; cumplimiento de D-A04-01 | Informe de viabilidad | pendiente |
| AC-A04-16 | Evidencia con commit, SO, Python, bibliotecas, hashes de fixtures y salidas, comandos y resultados; macOS, Linux local y GitHub Actions **por separado**, sin aprobación cruzada | Informe de viabilidad | pendiente |
| AC-A04-17 | Conclusiones **por separado** para DOCX/PDF y para eliminación/sustitución/restitución: viable en el subconjunto probado / viable con restricciones enumeradas / no viable o evidencia insuficiente | Informe de viabilidad | pendiente |
| AC-A04-18 | Esfuerzo de la vía PDF evaluado (EXP-10) y reportado | EXP-10 | pendiente |
| AC-A04-19 | Ningún cambio de versión, esquema, permisos ni contratos de A03; sin extras ni APIs anticipadas **en esta entrega documental** | Revisión del diff de esta entrega | cumplido en la entrega documental (verificado por revisión del diff; sin ejecución de suites) |
| AC-A04-20 | Un resultado negativo no habilita A07/A08 ni cierra A04 sin decisión del usuario | Informe de viabilidad + decisión del usuario | pendiente |

## 8. Estructura del informe de viabilidad (se completará después; hoy no existe)

Archivo futuro: `docs/a04-feasibility-report.md` (**propuesto / no disponible**). Secciones: **0.** estado por nivel de evidencia (especificado ≠ implementado ≠ probado en macOS ≠ en Linux local ≠ en GitHub Actions ≠ aceptado); **1.** identidad de la ejecución (commit, SO, Python, bibliotecas con versiones y licencias transitivas, binarios, fuentes, renderizador y versión, hashes de fixtures y salidas, comandos); **2.** resultados por experimento (EXP-01 a 10) con estado por fila de las matrices; **3.** controles negativos CN-01 a 07; **4.** hallazgos de fidelidad y residuos (con renders); **5.** conclusiones separadas: DOCX / PDF × eliminación / sustitución / restitución mecánica ∈ {viable en el subconjunto probado, viable con restricciones enumeradas, no viable o evidencia insuficiente}; **6.** esfuerzo de la vía PDF; **7.** límites, *skips* y lo no ejecutado; **8.** recomendación para A07/A08 y **decisión pendiente del usuario**. Cada resultado separa macOS, Linux local (Docker) y GitHub Actions.

## 9. Riesgos y límites declarados

- La viabilidad PDF depende de que existan mapeos fiables de fuente y de poder reescribir operandos; es el principal riesgo (EXP-02, 05, 10).
- Los renders no son idénticos entre lectores (D-A04-03); la inspección visual es evidencia, no prueba de identidad.
- Un documento con elementos `N` o `R` no se presenta como protegido completo; la ausencia de los valores seleccionados no es ausencia de datos sensibles.
- El mock no mide calidad (A09).
