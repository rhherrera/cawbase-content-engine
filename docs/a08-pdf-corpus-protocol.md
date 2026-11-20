# A08 — Protocolo de la comprobación D-A04-04 y corpus PDF

> **Estado: REVISIÓN 2 — CONGELADA; medición ejecutada** ([informe](a08-feasibility-report.md)); cualquier cambio de criterio requiere una revisión 3 aprobada y una medición nueva. La revisión 1 (`1afd04d`) fue aprobada por el usuario para la etapa 2, con `Z1` como primera opción y **P-A08-01 (`Z2`) aprobada solo para la comprobación experimental** (su incorporación al producto se decide tras la evidencia). La revisión 2 añade las **precisiones del usuario** ([§0](#0-precisiones-de-la-revisión-2-prevalecen-sobre-el-resto-del-documento)), registradas en un commit documental **anterior** a la primera medición candidata. Lo demás del documento sigue vigente donde §0 no lo modifica. Contrato propuesto: [protección y restauración PDF](pdf-protection.md). Alcance y matriz: [especificación de A08](features/A08-proteccion-y-restauracion-pdf.md). Decisiones: [A08-decisions](decisions/A08-decisions.md). Manifiesto de datos: [`experiments/a08/corpus/manifest.json`](../experiments/a08/corpus/manifest.json). Instructivo de Word y Google Docs: [`experiments/a08/corpus/EXPORT-W-G.md`](../experiments/a08/corpus/EXPORT-W-G.md).

**Regla de congelamiento.** Una vez aprobada esta revisión, el primer commit de la etapa 2 registra el SHA-256 de este documento y del manifiesto, y el SHA-256 de cada PDF del corpus, del oráculo geométrico y de las zonas **antes** de ejecutar el recorrido candidato. Cualquier cambio de corpus, oráculo, zonas, máscaras, umbrales, reglas o denominador **posterior a la primera medición** exige una nueva revisión de este documento aprobada explícitamente por el usuario **y** una nueva medición completa; la medición anterior se conserva como evidencia y no se reemplaza. No se ajustan reglas para convertir un fallo en éxito.

## 0. Precisiones de la revisión 2 (prevalecen sobre el resto del documento)

### 0.1 Exportación macOS y fuentes efectivas

- **`M1`, `M2`, `M4`**: el **mismo `.docx`** de las demás familias, leído por AppKit (`NSAttributedString`, tipo `officeOpenXML`, como lo abre TextEdit) y maquetado como el modo «ajustar a la página» de TextEdit (un `NSTextContainer` por página A4 compartiendo un `NSLayoutManager`; el salto de página del `.docx` llega como avance de página y pasa al contenedor siguiente), impreso con `NSPrintOperation` (`jobDisposition = save`). Sustituye al RTF de la revisión 1. El camino de TextEdit **no** reproduce encabezados ni pies del `.docx`: en `M1`, `M2` y `M4` no hay encabezado ni pie (no hay valores en ellos). `M3`: el HTML de `C3` con `WKWebView.printOperation`. Programa: `experiments/a08/exporters/export_m.swift`.
- El generador del `.docx` fija la fuente **también en cada tramo** (`w:rFonts`), porque AppKit no aplica las fuentes de estilo; Word, Google Docs y Writer muestran lo mismo.
- **T4, «fuente predeterminada»**: para las familias que parten del `.docx` son las fuentes del **tema** del documento base de `python-docx` (Cambria en el cuerpo, Calibri en títulos) tal como las resuelve cada exportador (Writer: Caladea/Carlito; macOS: sustitución del sistema); para Chromium, la predeterminada del navegador. Los PDF cuyas fuentes incrustadas no son redistribuibles (`C4` y `M4`, Times de Apple) van a `corpus-private/` (fuera de Git) con hash, procedencia y receta en Git.

### 0.2 Localización de los hallazgos controlados

Sustituye a §6.3: cada `S` se traduce a `(unit_id, start, end)` buscando el **valor exacto** en el texto de la unidad del extractor evaluado y eligiendo la ocurrencia cuyas piezas tienen el centro geométrico dentro de las cajas del oráculo geométrico de esa `S` (tolerancia 1 pt). Cero o más de una candidata ⇒ **no localizable** (etapa `localize`). Los contextos del oráculo de autoría se siguen usando solo para construir el oráculo geométrico (§6.2).

### 0.3 Zona `Z1`: límite derecho

Sustituye a la definición de «derecha» de §7.1: `R` es el mínimo, a la derecha del inicio del valor y dentro de la banda de su línea, de (a) el borde izquierdo del siguiente glifo que **no pertenece a la misma selección** (PDFium), (b) el borde izquierdo de cualquier trazado pintado o imagen que corte la banda (p. ej. un borde de celda) y el borde derecho de cualquier trazado pintado o imagen que **contenga** el inicio del valor (p. ej. un recuadro), (c) el borde derecho del área de texto de la página (máximo borde derecho de todos los glifos de la página) y (d) la `CropBox`; menos un margen de `0,3 ×` el tamaño de la fuente del valor. Los trazados y las imágenes salen de los objetos de página de PDFium del **original**. Se calcula y se congela antes de transformar.

### 0.4 Zona `Z2` verificable (P-A08-01, solo experimental)

- **Plan por línea.** `Z2` se plantea por **línea** (banda de §7.1), nunca por selección aislada: si alguna selección de la línea no cabe en su `Z1` con los criterios de §0.5, **todas** las selecciones de esa línea se planifican juntas. La **zona de línea** va desde el inicio de la primera selección hasta `R` de la última (§0.3, con (a) referido a glifos **posteriores a la última selección**).
- **Qué se mueve.** Solo los glifos **no seleccionados** de la misma línea situados **después** del inicio de la primera selección y antes de `R`: conservan contenido (Unicode), orden, fuente, tamaño, color, modo de render y línea base; se **desplazan** y se **comprimen** horizontalmente con un factor uniforme `f`. Los sustitutos de la línea se comprimen con el mismo `f`. Disposición: sustitutos y tramos no seleccionados se colocan en orden y **contiguos según su avance original** (los huecos originales entre ellos también se escalan por `f`), empezando en el inicio de la primera selección.
- **Qué se conserva** (y se verifica como fuera de zona aunque caiga dentro de la máscara): todo glifo anterior a la primera selección; todas las demás líneas; todo trazado, imagen y relleno (bordes, filetes, sombreados); todas las anotaciones (`/Rect`, acción y destino); el estado gráfico fuera de los operadores de texto reescritos.
- **Plan no justificable ⇒ el documento falla** (`replacement_overflow` si `f < 0,60`; `zone_plan_unjustified` en los demás casos): un enlace (`Link`) cuyo `/Rect` corta la región de glifos que se moverían; un trazado o imagen pintado **dentro** de esa región (entre glifos de la línea); glifos de la región en escritura no horizontal o con distinta orientación; glifos de la región dentro de un Form XObject; una selección de la línea que cruza a otra línea.
- **Varias selecciones en la misma línea** (incluida una redacción junto a una seudonimización): las zonas `Z1` no se solapan por construcción (§0.3 (a)); si alguna no cabe, el plan de línea `Z2` las reúne y una sola `f` se aplica a todos los sustitutos y tramos. **Zonas superpuestas** de líneas distintas no existen (bandas recortadas, §7.1); si dos bandas se solapan geométricamente ⇒ `zone_plan_unjustified`.
- **Oráculo de `Z2`** (calculado del original y del plan, antes de transformar): secuencia esperada de la zona de línea = sustitutos y caracteres no seleccionados en su orden; para cada carácter no seleccionado, su posición esperada (`x' = x₀ + f·(x − x₀) + Δ`, con `Δ` el desplazamiento acumulado por los sustitutos), su altura y su línea base. Verificación con PDFium: el texto de la zona es exactamente el esperado (marcador exacto incluido), cada carácter no seleccionado está en su posición esperada (±0,05 pt) con su altura y línea base originales (±0,01 pt), y `f ≥ 0,60`; con Poppler, el texto de la zona es el esperado.
- **Restauración en `Z2`**: devuelve los valores y la escala y posición **originales** de los caracteres no seleccionados (±0,05 pt frente al original), con las redacciones `[REDACTED]` intactas en su posición del derivado.

### 0.5 Orden de intentos y criterios de encaje (fijados antes de medir)

Para cada selección (o plan de línea), se prueba en este orden y se usa el **primero** que encaja: **(1)** fuente original en `Z1`; **(2)** fuente candidata en `Z1`; **(3)** fuente original en `Z2`; **(4)** fuente candidata en `Z2`. La **fuente original es elegible** solo si cubre todo `Σ` (§7.3); si no, se omiten (1) y (3). **Encaje en `Z1`**: el mayor `s ≤ 1` con `ancho(sustituto)·s ≤ ancho(Z1)` cumple `s ≥ 0,60`. **Encaje en `Z2`**: `f = min(1, ancho(zona de línea) / ancho natural del contenido reubicado)` cumple `f ≥ 0,60`. Así la candidata se usa también cuando la original tiene los glifos pero el sustituto no cabe de forma legible (D-A08-02). El informe separa los **éxitos que solo usan `Z1`** de los que **dependen de `Z2`**, con renders de cada zona, el intento usado y el factor `s` o `f` aplicado (al sustituto y al sufijo).

### 0.6 Conservación de lo ajeno a la transformación (verificaciones nuevas)

- **VB — pintura no textual**: (i) con `pypdf`, la secuencia de operadores **fuera** de los objetos de texto (`BT`…`ET`) de cada flujo es idéntica en original, protegido y restaurado, y la secuencia de `BT`/`ET` y de contenido marcado (`BDC`/`BMC`/`EMC`) también; (ii) con PDFium, el render de cada página **sin sus objetos de texto** es idéntico píxel a píxel en toda la página (también dentro de la máscara). Así un borde o filete dentro de la máscara no puede perderse.
- **VN — anotaciones**: salvo las clases `E` eliminadas, mismas anotaciones por página con igual subtipo, `/Rect`, acción y destino.

### 0.7 Recursos decorativos y trazados: reglas candidatas y controles adversarios

P-A08-03 (lista del host) **queda pendiente**: no se usa. Reglas **candidatas** evaluadas: `RI1` imagen uniforme; `RI2` imagen diminuta (≤ 16 × 16 muestras y ≤ 6 × 6 pt); `RP1` trazado de solo segmentos rectos con ≤ 8 segmentos por subtrayecto; `RP2` trazado con curvas de caja ≤ 6 × 6 pt; `RP3` **rejilla rectangular completa** (≥ 2 horizontales y ≥ 2 verticales finas, ≤ 2 pt de grosor, cada horizontal de lado a lado de la rejilla y cada vertical de arriba abajo; las celdas resultantes contienen texto extraído o están vacías); `RP4` **filete separador** (un único segmento o rectángulo fino ≤ 2 pt, horizontal o vertical, de longitud ≥ 50 % del ancho del área de texto); `RP5` **relleno de fondo** (rectángulo relleno, sin trazo, que contiene por completo al menos un glifo extraído y a ningún otro trazado ni imagen); `RP6` **recorte** (`W n`, no pinta); `RP7` **relleno blanco del área de página** (rectángulo blanco que contiene el área de texto).

**Controles adversarios `CA`** (una imagen o un conjunto de trazados que **representan texto**): `CA01` imagen de 16 × 16 muestras con dos dígitos legibles a 6 pt; `CA02` letras «GIL» con segmentos rectos (≤ 8 por subtrayecto); `CA03` letras «EF» con filetes largos y finos no alineados en rejilla; `CA04` letras de píxeles hechas con muchas imágenes uniformes de 2 × 2 pt; `CA05` letras de píxeles hechas con rectángulos rellenos; `CA06` cifras dentro de una «tabla» cuyas celdas pintan los trazos de las cifras como bordes adicionales; `CA07` letras de curvas pequeñas (≤ 6 pt). **Orden de ejecución**: primero los `CA`; toda regla que **admita** algún `CA` se registra como **refutada** y se **excluye** del alcance recomendado y de la compuerta de los casos nativos; luego se ejecutan los nativos con las reglas **no refutadas**. Superar los `CA` no prueba que una regla sea segura; solo que no fue refutada por esos controles.

### 0.8 Controles negativos nuevos

| ID | Alteración | Debe detectarlo |
|---|---|---|
| CV24 | `Z2`: un carácter del sufijo perdido | VZ (texto de la zona) |
| CV25 | `Z2`: un carácter del sufijo cambiado | VZ |
| CV26 | `Z2`: sufijo desplazado 1 pt respecto de su posición esperada | oráculo `Z2` (posiciones) |
| CV27 | Borde de celda dentro de la máscara eliminado | VB (i) y (ii) |
| CV28 | `/Rect` de un enlace desplazado | VN |
| CV29 | Restauración `Z2` con el sufijo en las posiciones comprimidas | VM (posiciones originales) |
| CV30 | Restauración que reemplaza o elimina un `[REDACTED]` | VM (redacciones intactas) |
| CS12 | Dos seudonimizaciones en la misma línea | ciclo completo (positivo) |
| CS13 | Redacción y seudonimización contiguas en la misma línea | ciclo completo (positivo) |

### 0.9 Identidad del extractor (registro del usuario)

El prototipo usa la identidad **propia de PDF** (`cce-pdf-extract`, versión `1`, `document-structures/2`); **DOCX conserva** exactamente su identidad y su huella actuales (`cce-docs-extract` `1` y el mismo `config_sha256`). La política completa de compatibilidad de trabajos en cola y mapas (P-A08-04), P-A08-05 a 07 y la dependencia del verificador en producción quedan para la revisión del contrato productivo; el prototipo puede evaluarlas e informar qué condicionan.

### 0.10 Exportaciones pendientes y denominador

Los `.docx` de Word y Google Docs y su instructivo están en [`experiments/a08/corpus/EXPORT-W-G.md`](../experiments/a08/corpus/EXPORT-W-G.md). Mientras falten, `W1`–`W4` y `G1`–`G4` cuentan como **faltantes**; el denominador sigue siendo 20, el piso 16/20 y 3/4 por familia, con el requisito Type0/CID nativo y los bloqueos. Cada entrada se **congela** (hash, oráculo y zonas, en un commit) **antes** de ejecutar el candidato sobre ella; si al terminar faltan exportaciones, se entregan los resultados parciales y el estado exacto del piso.

### 0.11 Desarrollo y medición

El prototipo se desarrolla y depura contra los controles y el corpus congelado **sin cambiar ningún criterio** de este protocolo; la medición informada es la ejecución completa final con el código de un commit identificado. Cualquier cambio de criterio sigue la regla de congelamiento.

## 1. Qué mide y qué no

| Mide | No mide |
|---|---|
| Qué PDF nativos de cinco familias de exportación abre, admite, extrae, localiza, transforma, verifica y restaura el recorrido candidato, y por qué falla o rechaza cada uno | Calidad de detección (A09): los hallazgos son **controlados** (oráculo + mock de A03) |
| La línea base: qué admite y extrae el extractor vigente (`cce-docs-extract` v1, rama PDF de `documents/pdf.py` en `c6590ec`) | Soporte universal de PDF ni de otros generadores |
| Que los controles negativos hacen fallar al verificador | Durabilidad, permisos, persistencia, concurrencia (etapa 3) |
| Que los controles estructurales cubren variantes ausentes de las exportaciones nativas y que los rechazos son seguros | Identidad visual exacta entre lectores (D-A04-03) |

El recorrido candidato de la etapa 2 va **desde la apertura y la localización hasta la escritura, reapertura, verificación y restauración mecánica**, con los sustitutos **reales** de A06 (marcadores `[[CCE1:…]]` generados por `protection/markers.py` y `[REDACTED]`), sin contextos durables ni criptografía (la restauración mecánica usa una tabla local token → valor, como E-A04-22).

## 2. Familias y flujos de exportación

Cinco familias, **cuatro casos por familia**, **20 casos nativos** en el denominador. El flujo de cada familia es fijo; un PDF producido por otro flujo **no** sustituye a un caso de la familia.

| Familia | Código | Flujo concreto | Opciones fijadas | Fuente del contenido | Quién lo ejecuta |
|---|---|---|---|---|---|
| Microsoft Word | `W` | **Exportador de Microsoft**: Word para Windows (Microsoft 365) `Archivo › Exportar › Crear documento PDF/XPS` **o** Word para Mac `Archivo › Guardar como › PDF › Mejor para distribución electrónica y accesibilidad (usa el servicio en línea de Microsoft)`. **No** vale «Mejor para impresión» de Word para Mac (usa el sistema de impresión de macOS: sería familia `M`) ni Word para la web | Optimizar para: estándar; «Crear marcadores usando: Títulos» activado; «Etiquetas de estructura del documento para accesibilidad» activado (predeterminado); «Compatible con ISO 19005-1 (PDF/A)» desactivado; «Propiedades del documento» activado (para que haya metadatos que eliminar) | `.docx` generado a partir del manifiesto (§3) por el generador de la etapa 2, con las fuentes de §13 instaladas en el equipo de Word | El usuario (o quien disponga de Word), siguiendo esta tabla; el agente no dispone de Word (§15) |
| Google Docs | `G` | Subir el mismo `.docx` (`Archivo › Abrir › Subir`), abrirlo **como documento de Google** (conversión), comprobar contra el manifiesto que saltos de página, tablas, columnas, notas y estilos se conservaron (corrigiendo a mano solo lo que la conversión alteró, con registro), y `Archivo › Descargar › Documento PDF (.pdf)` | Sin opciones; se registran fecha y hora UTC, navegador y versión, idioma de la interfaz y el `Producer` del PDF | `.docx` del manifiesto | El usuario (requiere su cuenta de Google); el agente puede asistir en el navegador integrado solo si el usuario inicia sesión y lo autoriza |
| LibreOffice Writer | `L` | `soffice --headless --convert-to 'pdf:writer_pdf_Export:{…}'` sobre el `.odt` producido al abrir el `.docx` del manifiesto en Writer y guardarlo (`soffice --headless --convert-to odt`), en la imagen Linux de referencia | Filtro `writer_pdf_Export` con las opciones predeterminadas de la versión salvo: `ExportBookmarks=true`, `UseTaggedPDF=false` (predeterminado), `SelectPdfVersion=0`; la cadena exacta de opciones se registra | `.docx` del manifiesto | El agente (Docker) |
| Impresión de Chromium | `C` | `"Google Chrome" --headless --disable-gpu --no-pdf-header-footer --run-all-compositor-stages-before-draw --print-to-pdf=<salida> <archivo.html>` (Chrome estable en macOS, motor Chromium/Skia) | Tamaño A4 por CSS `@page`; márgenes por CSS; fondos activados por CSS (`print-color-adjust: exact`); sin cabecera/pie del navegador; perfil temporal vacío (`--user-data-dir` descartable) y sin red (`file://`) | HTML generado a partir del manifiesto, con `@font-face` a archivos locales de §13 | El agente |
| Exportación PDF de macOS | `M` | Sistema de impresión de macOS (Quartz `PDFContext`) por la API que usan TextEdit y Safari: `NSPrintOperation` con `jobDisposition = save` desde un `NSTextView` cargado con RTF (casos `M1`, `M2`, `M4`, ruta de TextEdit) y `WKWebView.printOperation` desde HTML (caso `M3`, ruta de WebKit para columnas), ejecutado por un programa Swift reproducible | Tamaño A4, márgenes del manifiesto, sin encabezado/pie del sistema; fuentes registradas **solo para el proceso** (`CTFontManagerRegisterFontsForURL`, ámbito `.process`), sin instalar nada en el sistema | RTF y HTML generados a partir del manifiesto | El agente (macOS) |

- **Por qué un programa para `M`**: reproduce el camino de impresión de AppKit/WebKit que usan TextEdit y Safari («Archivo › Exportar como PDF…» o «Imprimir › PDF › Guardar como PDF»), sin automatizar la interfaz. El `Producer` resultante se registra; si no es el de Quartz, el caso se declara **no representativo** y cuenta como faltante.
- **Registro obligatorio por caso** (`experiments/a08/corpus/<caso>/provenance.json`, etapa 2): familia, flujo, aplicación y versión/compilación exactas, sistema operativo y arquitectura, opciones, fuentes (nombre, archivo, versión, SHA-256, licencia), fecha y hora UTC, persona o programa que exportó, SHA-256 del documento fuente y del PDF, `Producer`/`Creator` leídos del PDF (antes de cualquier eliminación), salida de `pdffonts` y `pdfinfo` (Poppler) y lista de tipos de fuente (para identificar los Type0/CID subconjuntados).
- **Representatividad**: cada familia usa su exportador real con opciones habituales (las predeterminadas, salvo marcadores/etiquetado/propiedades donde se fijan para ejercitar limpieza y estructura); el contenido ejercita prosa justificada, tablas con bordes, columnas, encabezados y pies, notas, listas, enlaces, cambios de estilo dentro de un valor, acentos, ligaduras y valores cortos (§3–§5). Las plantillas son las mismas en todas las familias, de modo que las diferencias observadas son del exportador y no del contenido.

## 3. Plantillas de contenido

Cuatro plantillas, una por caso de cada familia (`<familia>1` … `<familia>4`). Los saltos de página son **explícitos** en la fuente para fijar la página de cada valor; el texto de relleno es fijo (manifiesto, `filler`) y no contiene ningún valor de §4. Fuentes del cuerpo: §13.

| Plantilla | Estructura | Rasgos obligatorios |
|---|---|---|
| **T1 — Carta** (2 páginas) | Encabezado en todas las páginas («Cooperativa Ficticia del Ejemplo · Expediente 2026/017»), pie «Página N de 2»; saludo con un nombre largo cuyo **primer tramo va en negrita** (cambio de estilo dentro del valor ⇒ fragmentación entre operadores); párrafos **justificados**; valor **corto en mitad de línea**; correo; documento de identidad; página 2 con una tabla de contacto de 2×3 con bordes y una firma que **repite** el correo (ocurrencia idéntica no seleccionada) | prosa, tabla, valor corto, acentos, fragmentación, ocurrencia idéntica, redacción y seudonimización |
| **T2 — Formulario en tabla** (2 páginas) | Título; párrafo que **menciona** el nombre completo (ocurrencia idéntica no seleccionada); tabla de 2 columnas × 8 filas con bordes: nombre (primer tramo en negrita), alias **corto** en celda, teléfono, correo, documento, cuenta, retribución **corta** en celda, observaciones; página 2 con prosa y una tabla de historial sin valores | prosa, tabla, valores cortos en celda, acentos, fragmentación, ocurrencia idéntica |
| **T3 — Boletín a dos columnas** (2 páginas) | Título a todo el ancho; **dos columnas** de prosa justificada con: nombre largo con el primer tramo en negrita, pasaporte en mitad de línea, apellido **corto en mitad de línea**, dirección con `º`; la misma dirección **repetida** en la otra columna (ocurrencia idéntica no seleccionada); página 2 con un recuadro sombreado (rectángulo relleno) con un teléfono | prosa, columnas, valor corto, acentos, fragmentación, ocurrencia idéntica, trazados ordinarios |
| **T4 — Informe estructurado** (3 páginas) | Título y subtítulos (marcadores/esquema donde el exportador los genere); lista con viñetas y lista numerada; nota al pie (o nota final en `C`/`M`, que no tienen notas al pie); enlace visible «portal de ayuda» a `https://example.test/ayuda` (el destino **no** contiene valores); filete horizontal; banda de título sombreada; nombre largo en un elemento de lista (primer tramo en negrita); alias **corto en mitad de línea** y el mismo alias **repetido** en otra sección (ocurrencia idéntica no seleccionada); cuenta en la nota; pasaporte; correo con ligadura `fi`. **Cuerpo con la fuente predeterminada del exportador** (§13) | prosa, listas, notas, enlace, esquema, valores cortos, ligadura, fuente predeterminada |

**Sin imágenes rasterizadas en los casos nativos.** La decisión aprobada 3 rechaza los mixtos con regiones fuera de cobertura y solo admite recursos decorativos bajo reglas verificables; para imágenes rasterizadas no hay regla verificable general (contrato §5.4, P-A08-03). Las imágenes se ejercitan en los **controles** (§8). Consecuencia declarada: el corpus no representa documentos con logotipos o fotografías, que el recorrido rechazaría salvo aprobación de P-A08-03.

## 4. Valores sintéticos y política

Todos los valores son **evidentemente ficticios** (dominio reservado `example.test`, documentos y cuentas de ceros). `type_id` del catálogo de A02. Política fija para todo el corpus: `default_action = pseudonymize`; `government_id`, `financial_information` y `salary_compensation` ⇒ `redact`.

| ID | Valor exacto | `type_id` | Acción | Puntos de código | Rasgos |
|---|---|---|---|---|---|
| V01 | `María José Núñez Ibáñez` | `personal_data` | seudonimizar | 23 | acentos, `ñ`; primer tramo `María José` en negrita |
| V02 | `ana.soler@example.test` | `personal_data` | seudonimizar | 22 | correo |
| V03 | `Gil` | `personal_data` | seudonimizar | 3 | **corto**, en mitad de línea |
| V04 | `00000000-X` | `government_id` | redactar | 10 | — |
| V05 | `ES00 0000 0000 0000 0000 0000` | `financial_information` | redactar | 29 | espacios internos |
| V07 | `Íñigo` | `personal_data` | seudonimizar | 5 | **corto**, `Í` y `ñ` |
| V08 | `4 200 €` | `salary_compensation` | redactar | 7 | **corto**, `€` |
| V09 | `Calle Ficticia 123, 4.º B` | `personal_data` | seudonimizar | 25 | `º` |
| V10 | `+00 600 000 000` | `personal_data` | seudonimizar | 15 | — |
| V11 | `José Ángel Pérez-Ruiz` | `personal_data` | seudonimizar | 21 | acentos, guion; primer tramo `José Ángel` en negrita |
| V12 | `X0000000` | `government_id` | redactar | 8 | — |
| V13 | `fidel.ruiz@example.test` | `personal_data` | seudonimizar | 23 | `fi` inicial (ligadura posible) |

(`V06` no se usa; se conserva la numeración del borrador.)

**Regla de diseño del corpus** (comprobada por el generador de la etapa 2 **antes** de exportar y registrada): ningún valor aparece, en ninguna de las representaciones de VK (§9), dentro de otro valor ni del texto fijo de las plantillas, salvo en las ocurrencias `O` declaradas. Así una coincidencia accidental (p. ej. un apellido dentro de un correo, que con `casefold` sería un residuo y haría fallar el caso en cerrado) no distorsiona lo que se mide. Por esa regla el correo de V02 no contiene el apellido de V03.

## 5. Manifiesto de los 20 casos nativos

Selecciones `S` (todas se proponen como hallazgos controlados) y ocurrencias idénticas **no seleccionadas** `O`, con página (base 1). El contexto exacto de cada una (palabras contiguas en la fuente) y el texto completo de cada plantilla están en el [manifiesto](../experiments/a08/corpus/manifest.json). Las plantillas son idénticas entre familias; la tabla lista la plantilla y las selecciones una vez y las aplica a las cinco familias.

| Plantilla | Casos | Selecciones (valor, página, acción) | Ocurrencias no seleccionadas |
|---|---|---|---|
| T1 | `W1` `G1` `L1` `C1` `M1` | S1 V01 p1 seud. (fragmentado); S2 V03 p1 seud. (corto, mitad de línea); S3 V02 p1 seud.; S4 V04 p1 redact.; S5 V10 p2 seud. (celda) | O1 V02 p2 (firma) |
| T2 | `W2` `G2` `L2` `C2` `M2` | S1 V11 p1 seud. (celda, fragmentado); S2 V07 p1 seud. (corto, celda); S3 V10 p1 seud.; S4 V02 p1 seud.; S5 V04 p1 redact.; S6 V05 p1 redact.; S7 V08 p1 redact. (corto, celda) | O1 V11 p1 (prosa) |
| T3 | `W3` `G3` `L3` `C3` `M3` | S1 V01 p1 seud. (columna 1, fragmentado); S2 V12 p1 redact. (mitad de línea); S3 V03 p1 seud. (corto, mitad de línea, columna 2); S4 V09 p1 seud. (columna 1); S5 V10 p2 seud. (recuadro) | O1 V09 p1 (columna 2) |
| T4 | `W4` `G4` `L4` `C4` `M4` | S1 V01 p1 seud. (lista, fragmentado); S2 V07 p2 seud. (corto, mitad de línea); S3 V05 p2 redact. (nota); S4 V12 p3 redact.; S5 V13 p3 seud. (ligadura) | O1 V07 p3 |

Para `M3` la plantilla T3 se compone con columnas CSS (WebKit), porque TextEdit no tiene columnas; para `C` y `M` la nota al pie de T4 es una nota final al pie de la misma página (bloque de texto), porque HTML/RTF no tienen notas al pie nativas.

**Faltantes.** Si un exportador no está disponible o su caso no puede producirse por el flujo fijado, el caso se registra como **faltante** con su motivo y **sigue en el denominador** (cuenta como no exitoso). No se sustituye por otro flujo, otra familia ni un PDF sintético.

## 6. Oráculo independiente del extractor del motor

El oráculo tiene dos capas; ninguna usa el extractor del motor (`documents/pdf.py`) ni el recorrido candidato.

1. **Oráculo de autoría** (congelado en esta revisión, en el manifiesto): para cada `S` y `O`, valor exacto, `type_id`, acción, página, **contexto izquierdo y derecho exactos** en la fuente (la palabra o palabras contiguas), y el ordinal de esa tripleta (contexto + valor + contexto) en la página. Se escribe a mano, antes de exportar.
2. **Oráculo geométrico** (congelado en el primer commit de la etapa 2, **antes** de ejecutar el recorrido candidato): por cada caso exportado, dos lectores independientes del motor —**PDFium** (`pypdfium2`, caracteres con su caja) y **Poppler** (`pdftotext -bbox-layout`, palabras, líneas y bloques)— localizan cada tripleta del oráculo de autoría en la página indicada. El oráculo geométrico de una ocurrencia es: página, caja de cada carácter del valor (PDFium), caja de línea y de bloque (Poppler) y la secuencia de caracteres del valor tal como la extrae cada lector.
   - **Búsqueda**: en el texto de la página de cada lector, con las secuencias de espacios en blanco (incluidos saltos de línea) colapsadas a un espacio **solo en los contextos**, de modo que el corte de línea que elija el exportador entre contexto y valor no impida encontrarlo; el **valor** se busca exacto.
   - **Acuerdo exigido**: ambos lectores encuentran **exactamente una** tripleta en la página y la caja del valor de Poppler contiene los centros de las cajas PDFium del valor (tolerancia 0,5 pt). Las diferencias de normalización de Poppler (ligaduras expandidas, espacios sintetizados) se registran por caso y se aplican igual al original, al protegido y al restaurado.
   - **Valor partido entre líneas** por el exportador: el oráculo lo registra tal cual (dos tramos); no tiene zona definida y el caso no puede tener éxito (fallo esperado `selection_crosses_boundary` en la etapa `plan`). No es un fallo del oráculo.
   - **Desacuerdo o ausencia** (el exportador alteró el texto, lo partió entre líneas, lo convirtió en trazados o lo duplicó): el caso se marca `oracle_unavailable` en la etapa `oracle`, **cuenta como no exitoso** y no se reemplaza ni se reescribe su fuente.
3. **Hallazgos controlados para el motor**: la etapa 2 traduce cada `S` a `(unit_id, start, end)` buscando la tripleta exacta en el texto de la unidad que produce el extractor evaluado (línea base o candidato). Si no aparece exactamente una vez, la selección **no es localizable** por ese extractor (etapa `localize`): es un fallo del extractor, no del oráculo. Las `O` nunca se convierten en hallazgos.

## 7. Zonas seguras y máscaras

Se calculan **solo** a partir del PDF original y del oráculo geométrico (nunca del resultado del transformador), y se congelan con él.

### 7.1 Zona primaria `Z1` (vigente)

Para cada `S`: rectángulo en coordenadas de página no rotada (transformado si la página o el texto están rotados) con

- **izquierda** = borde izquierdo (en la dirección de escritura) de la caja PDFium del primer carácter del valor;
- **derecha** = el mínimo entre (a) el borde izquierdo de la caja del siguiente carácter **no seleccionado** de la misma línea (PDFium; banda de línea = cajas cuyo centro vertical dista menos de la mitad de la altura de la línea), menos 0,25 pt, y (b) el borde derecho del **bloque** que contiene el valor según Poppler y según el agrupamiento de caracteres de PDFium (el menor de los dos), y (c) el borde de la `CropBox`;
- **abajo/arriba** = la unión de las cajas PDFium de los caracteres de la línea (no solo del valor), recortada para no solapar las cajas de las líneas vecinas.

`Z1` es el valor más el espacio libre a su derecha en su línea y bloque. Fuera de `Z1` la presentación debe conservarse.

### 7.2 Zona extendida `Z2` (**propuesta P-A08-01, no vigente sin aprobación**)

Igual que `Z1`, pero la derecha es solo (b)–(c): incluye **el resto de la línea** a la derecha del valor dentro de su bloque. El texto no seleccionado de ese tramo conserva su contenido, orden y selección, pero puede **comprimirse horizontalmente** con el mismo factor que el sustituto para que ambos quepan; las demás líneas no cambian. Se aplica **solo** si el sustituto no cabe en `Z1` con los umbrales de §7.3. Si el usuario no aprueba P-A08-01 antes de la etapa 2, `Z2` **no** se usa y la medición se hace solo con `Z1`.

**Consecuencia calculada antes de medir (no es una medición).** Con el marcador de 25 caracteres, un valor corto en mitad de línea (V03 `Gil`, V07 `Íñigo` en T1, T3 y T4) no tiene espacio libre propio: aun con la compresión mínima admitida (§7.3) el marcador necesita varias veces el ancho del valor. Con solo `Z1`, **T1, T3 y T4 fallarían previsiblemente en las cinco familias** (15 casos), lo que haría **inalcanzable** el piso de §11 por construcción. Ver la cuestión material Q-A08-01 en [A08-decisions §7](decisions/A08-decisions.md#7-preguntas-abiertas-tensiones-y-cuestiones-materiales).

### 7.3 Representación del sustituto: encaje y legibilidad

- **Misma línea base, mismo tamaño de fuente** que el primer carácter del valor; mismo color de relleno y modo de render 0.
- **Compresión horizontal** (`Tz`) del sustituto (y del resto de línea en `Z2`): factor `s ∈ [0,60; 1,00]`; se usa el mayor `s` que hace caber el sustituto en la zona. `s < 0,60` ⇒ `replacement_overflow`.
- **Fuente**: la **original** si su programa incrustado y su codificación cubren **todo** el alfabeto posible de los sustitutos `Σ = { [ ] : } ∪ {0–9} ∪ {A–Z sin I, L, O, U}` (35 caracteres; cubre `[[CCE1:…]]` con cualquier token y `[REDACTED]`), con correspondencia inversa Unicode → código única y glifo distinto de `.notdef`; en otro caso, la **fuente candidata** incrustada (§13). La regla depende del alfabeto, **no** del token aleatorio, para que el resultado sea determinista. La comparación de ambas fuentes (decisión 2) se registra como **variante informativa** cuando la original cubre `Σ`; el éxito se decide con la regla.
- **Legibilidad automática**: altura de los glifos del sustituto renderizados a 150 ppp ≥ 8 px; ningún glifo `.notdef`; la extracción PDFium y Poppler del sustituto en la zona es **exactamente** el sustituto y, para un marcador, coincide con la clase S de A06 (`\[\[CCE1:[0-9A-HJKMNP-TV-Z]{16}\]\]`) sin espacios ni caracteres insertados.
- **Legibilidad visual**: se generan hojas de contacto de **todas** las zonas (original, protegido y restaurado) a 300 ppp con PDFium y Poppler para revisión humana. La revisión no cambia el resultado automático; un reparo de legibilidad se registra y se trae al usuario como revisión de criterio (regla de congelamiento).

### 7.4 Máscaras de comparación

- **Máscara de zona** = unión de las zonas del documento, dilatada **3 px** a 150 ppp (≈ 1,44 pt) para cubrir el suavizado de bordes. Igual para PDFium y Poppler.
- **Fuera de la máscara**: el render del protegido es **idéntico píxel a píxel** al del original con el **mismo** renderizador (0 píxeles distintos, escala de grises de 8 bits, 150 ppp). Lo mismo para restaurado frente a protegido y restaurado frente a original.
- **Geometría del texto fuera de las zonas**: la secuencia de caracteres PDFium cuyo centro cae fuera de toda zona es **igual** (mismo Unicode, mismo orden) en original y protegido, con cajas iguales en ±0,01 pt.

## 8. Controles (no cuentan en la tasa de utilidad)

Se generan en la etapa 2 con código propio (generador sintético) o derivando un caso nativo con herramientas registradas (`qpdf` CLI, `pikepdf`, `fontTools`), con manifiesto propio y **resultado esperado fijado aquí**. Un control **estructural** que falle informa el alcance (no bloquea); un control de **rechazo** o del **verificador** aceptado indebidamente **bloquea** el avance (§11).

### 8.1 Estructurales positivos (`CS`): variantes ausentes de las exportaciones nativas

| ID | Variante | Construcción | Esperado |
|---|---|---|---|
| CS01 | Type0 `Identity-H` + `CIDFontType2` subconjuntada, `ToUnicode` | generador + `fontTools` (fuente OFL de §13) | ciclo completo |
| CS02 | Type0 `Identity-H` + `CIDFontType0` (CFF) subconjuntada | generador + `fontTools` | ciclo completo o rechazo con motivo cerrado registrado (capacidad condicionada) |
| CS03 | Object streams y xref stream | `qpdf --object-streams=generate` sobre el original de `L1` | ciclo completo; salida sin restos de los objetos sustituidos |
| CS04 | `/Contents` en arreglo (3 flujos), con un valor cuyas piezas están en dos flujos distintos | `pikepdf` partiendo el flujo de `C1` en límites de operador | ciclo completo |
| CS05 | Estado de texto `Tc`, `Tw`, `Tz`, `Ts`, `TL` con `T*`, `'` y `"` sobre fuente simple | generador | ciclo completo; `Tw` aplicado solo al código 32 de un byte |
| CS06 | `Tw` con fuente Type0 de dos bytes (no aplica `Tw`) y `Tc` negativo | generador | ciclo completo |
| CS07 | Form XObject compartido por dos páginas: (a) valor seleccionado en ambos usos; (b) solo en uno | generador | (a) ciclo completo; (b) clonado del objeto para el uso afectado **con prueba** de que el otro uso queda idéntico, o fallo `shared_object_partial` (capacidad condicionada) |
| CS08 | `/Rotate 270` y `Tm` a 270° | generador | ciclo completo |
| CS09 | Inversión vertical en `cm` y en `Tm` (determinante combinado positivo, como Skia) | generador | ciclo completo |
| CS10 | Entrada linealizada | `qpdf --linearize` sobre `L2` | ciclo completo con pérdida de linealización declarada |
| CS11 | `Tr 1` y `Tr 2` (contorno visible) | generador | ciclo completo conservando el modo |

### 8.2 Rechazos seguros (`CR`)

| ID | Caso | Esperado |
|---|---|---|
| CR01 | Type0 `Identity-V` (escritura vertical) | rechazo `font_mapping_insufficient` o `unsupported_structure` |
| CR02 | Fuente Type3 | rechazo `font_mapping_insufficient` |
| CR03 | Type0 sin `ToUnicode` | rechazo `font_mapping_insufficient` |
| CR04 | Type0 con CMap predefinido distinto de `Identity-H` (`UniGB-UCS2-H`) | rechazo `font_mapping_insufficient` |
| CR05 | Página escaneada (solo imagen) | rechazo `scanned_without_text` |
| CR06 | Imagen escaneada **con capa de texto** (OCR invisible, `Tr 3`) | rechazo (`hidden_text` o `uncovered_content`); nunca protegido |
| CR07 | Página con texto y una imagen rasterizada con texto en píxeles | rechazo `uncovered_content` |
| CR08 | Texto convertido a trazados (curvas grandes) | rechazo `uncovered_content` |
| CR09 | Cifrado con contraseña de usuario vacía | rechazo `encrypted` |
| CR10 | Firmado (`/ByteRange`, `/Sig`) | rechazo `signed` |
| CR11 | Actualización incremental real (dos revisiones) | rechazo `incremental_update` |
| CR12 | xref rota que el parser repararía | rechazo `parser_repair` |
| CR13 | `/AcroForm` con campo y apariencia | rechazo `forms_or_layers` |
| CR14 | Capas (`/OCProperties`) | rechazo `forms_or_layers` |
| CR15 | Adjunto (`/EmbeddedFiles`) | rechazo `embedded_content` |
| CR16 | `/OpenAction` JavaScript | rechazo `executable_action` |
| CR17 | Anotación `FreeText` | rechazo `unsupported_structure` |
| CR18 | `/ActualText` (estructura o contenido marcado) | rechazo `alternate_text_duplication` |
| CR19 | Operador partido entre dos flujos de `/Contents` | rechazo `unsupported_structure` |
| CR20 | Form XObject compartido con un uso en la apariencia de una anotación | rechazo o fallo `shared_object_partial` |
| CR21 | Texto recortado fuera de la `CropBox`, `Tr 7` (recorte) | rechazo `hidden_text`/`unsupported_text_state` |
| CR22 | Rectángulo opaco pintado **después** del texto sobre un valor (tachado previo del autor) | rechazo `hidden_text` |
| CR23 | Valor partido entre dos líneas (guion de corte) | fallo `selection_crosses_boundary` (detección válida, no transformable) |
| CR24 | Selección con escritura de derecha a izquierda | fallo `selection_not_transformable` |
| CR25 | Valor seleccionado presente también en un título del esquema (`/Outlines`) | fallo `residue_detected`, nada publicado |
| CR26 | Valor seleccionado en el destino `/URI` de un enlace | fallo `residue_detected` |

### 8.3 Controles del verificador (`CV`): deben hacerlo **fallar**

Cada uno se construye a partir de un protegido **válido** de un caso nativo (o de su original) alterándolo; el verificador debe rechazarlo con el código indicado.

| ID | Alteración | Debe detectarlo |
|---|---|---|
| CV01 | **Superposición**: rectángulo opaco sobre el valor con el texto original intacto debajo | residuo en extracción (PDFium, Poppler, recorrido estructural) |
| CV02 | Superposición y texto original movido fuera de la `CropBox` | recorrido estructural (operandos decodificados) |
| CV03 | Copia del valor en `/Info /Title` | recorrido estructural (diccionarios, PDFDocEncoding y UTF-16BE) |
| CV04 | Copia en XMP `dc:title` | recorrido estructural (flujo XMP decodificado, entidades XML) |
| CV05 | Copia en un título de `/Outlines` | recorrido estructural |
| CV06 | Copia en `/URI` de un enlace, codificada `%XX` | recorrido estructural con decodificación URL |
| CV07 | Copia en `/Contents` de una anotación conservada | recorrido estructural |
| CV08 | Copia en un **objeto huérfano** no alcanzable | recorrido estructural (objetos no alcanzables ⇒ fallo) y barrido de bytes |
| CV09 | Copia dentro de un **object stream** | recorrido estructural (objetos comprimidos) |
| CV10 | Copia como texto `Tr 3` en otra posición | operandos decodificados por fuente |
| CV11 | Copia codificada con los códigos de otra fuente del documento en un Form XObject no usado | decodificación por todas las fuentes |
| CV12 | **Alteración fuera de zona**: un carácter no seleccionado cambiado | geometría/texto fuera de zona y render |
| CV13 | Línea no seleccionada desplazada 0,5 pt | geometría fuera de zona y render |
| CV14 | Color de una palabra no seleccionada cambiado | render fuera de máscara |
| CV15 | Trazado (filete) eliminado | render fuera de máscara |
| CV16 | **Pérdida de selección**: sustituto dibujado como trazados (no extraíble) | extracción en zona ≠ sustituto |
| CV17 | Sustituto con una fuente sin `ToUnicode` | extracción en zona ≠ sustituto |
| CV18 | Primer carácter del valor original conservado | residuo parcial y extracción en zona |
| CV19 | Marcador con un espacio insertado por un ajuste `TJ` grande | gramática del marcador extraído |
| CV20 | **Restauración en ubicación incorrecta**: dos valores intercambiados | oráculo de restauración por posición |
| CV21 | Valor restaurado en la posición de la ocurrencia no seleccionada | oráculo de restauración por posición |
| CV22 | Original sin modificar presentado como protegido | residuos y oráculo |
| CV23 | Restaurado de **otro** derivado | identidad (digest) y oráculo |

## 9. Verificaciones y responsables

Toda verificación se aplica al **original** (como compuerta y referencia), al **protegido** y al **restaurado**. Ninguna usa el resultado del transformador para establecer lo esperado: lo esperado sale del original, del oráculo y de las zonas congeladas.

| ID | Verificación | PDFium | Poppler | Recorrido estructural | Regla |
|---|---|---|---|---|---|
| VA | **Apertura sin reparación** | carga del documento y de cada página sin error | `pdfinfo` y `pdftotext` sin nada en `stderr` | `pypdf` con `strict=True` sin advertencias; `qpdf --check` (CLI, en Linux) sin `WARNING` | cualquier aviso ⇒ fallo (en el original ⇒ rechazo `parser_repair` del caso) |
| VG | **Geometría** | número de páginas; `MediaBox`, `CropBox`, `/Rotate` | `pdfinfo -box` | cajas, `/Rotate`, `UserUnit` | iguales en original, protegido y restaurado |
| VT | **Texto fuera de la selección** | secuencia y cajas de caracteres fuera de zonas (§7.4) | texto de `pdftotext -raw` por página con las zonas sustituidas por el texto esperado | — | igualdad exacta (con la normalización Poppler registrada en el oráculo) |
| VZ | **Contenido de cada zona** | caracteres en la zona = sustituto exacto (o valor exacto al restaurar); cajas dentro de la zona (+0,25 pt) | palabra(s) de la zona = sustituto/valor | — | igualdad exacta; gramática S de A06 para marcadores; sin `.notdef` |
| VO | **Desbordamiento** | cajas del sustituto dentro de la zona, de la `CropBox` y sin intersección con cajas no seleccionadas | — | — | cualquier intersección ⇒ `replacement_overflow` |
| VR | **Render** | 150 ppp, gris 8 bits | `pdftoppm -r 150 -gray` | — | 0 píxeles distintos fuera de la máscara; hojas de contacto de zonas (§7.3) |
| VS | **Objetos y flujos decodificados** | — | — | todos los objetos alcanzables (incluidos los de object streams); todos los flujos decodificados con sus filtros (`FlateDecode`, `LZWDecode`, `ASCIIHexDecode`, `ASCII85Decode`, `RunLengthDecode`; los de imagen se escanean en bruto); objetos **no alcanzables** ⇒ fallo | — |
| VX | **Restos omitidos por la extracción** | — | — | operandos de **todos** los operadores de texto de todos los flujos (también invisibles, recortados, fuera de página y en Form XObjects no usados) decodificados con su fuente y, además, con **cada** fuente del documento | ningún valor seleccionado fuera de su ocurrencia permitida |
| VK | **Residuos por representación** | — | — | cada valor seleccionado en: exacta, NFC, NFD, `casefold`, blancos colapsados; UTF-8, UTF-16BE/LE, PDFDocEncoding; cadenas literales y hexadecimales ya decodificadas; nombres con `#xx` decodificados; decodificación URL (`%XX`); entidades XML del XMP; bytes crudos del archivo y de cada flujo descomprimido | cualquier coincidencia no atribuida ⇒ `residue_detected` |
| VP | **Ocurrencia idéntica permitida** | caja PDFium = oráculo geométrico de la `O` | palabra en la misma posición | coincidencia **solo** en el mismo flujo, operador y rango de códigos que en el original | exenta **solo** en esa ubicación; cualquier otra coincidencia es residuo |
| VE | **Clases auxiliares eliminadas** | — | `pdfinfo` sin metadatos descriptivos | `/Info` sin claves descriptivas; sin `/Metadata` de documento ni de objetos; sin anotaciones `Text`/`Popup` | clases registradas presentes antes y ausentes después |
| VM | **Restauración** | en cada zona, el valor Unicode exacto; fuera de zonas igual al protegido; texto del resto de línea en `Z2` en sus posiciones originales (±0,05 pt frente al original) | ídem | sin marcadores (clase S) restantes; `[REDACTED]` presentes; clases `E` ausentes | igualdad exacta; el kerning interno del valor restaurado puede variar |

**Separación de responsabilidades.** PDFium y Poppler prueban lo que **ve y extrae un lector** (texto, posiciones, render, apertura). El **recorrido estructural** (con `pypdf`, parser distinto de `pikepdf`/QPDF que usa el transformador, más barrido de bytes propio) prueba lo que **contiene el archivo** aunque ningún lector lo muestre. `qpdf --check` (misma biblioteca que el transformador) solo cuenta para la apertura, no como verificador independiente. Poppler es una herramienta de verificación (GPL): no se distribuye ni se enlaza.

## 10. Registro por documento y variante

Para cada caso nativo y control, y para cada extractor (línea base y candidato) y variante de fuente (original/candidata cuando aplique), se registra el resultado de cada etapa con su motivo cerrado:

`export` → `oracle` → `open` → `gate` (matriz de admisión) → `extract` → `localize` → `plan` → `remove` → `substitute` → `write` → `reopen` → `verify` (VA…VE, una entrada por verificación) → `restore` → `verify_restore` (VA, VG, VT, VZ, VR, VM).

La **línea base** registra solo `open`, `gate`, `extract` y `localize` (el extractor vigente no transforma). Se guardan: comandos exactos, versiones (Python, bibliotecas, Poppler, QPDF, Chrome, LibreOffice, macOS, Word, navegador de Google Docs), fuentes, SHA-256 de cada entrada y salida, tiempos y memoria pico, renders, hojas de contacto y diferencias de píxeles.

## 11. Criterio de avance (fijado antes de medir)

El recorrido candidato **avanza** a la propuesta de alcance productivo solo si se cumplen **todas**:

1. **≥ 16 de 20** casos nativos con **éxito completo**.
2. **≥ 3 de 4** por **cada** familia.
3. **≥ 1** caso nativo cuyas selecciones están en una fuente **Type0/CID subconjuntada** (según `pdffonts` del original) con éxito completo.
4. **Ningún bloqueo**: (a) ningún residuo de un valor seleccionado en ningún protegido emitido (nativo o control); (b) ninguna pérdida colateral (VT, VR o VG fallidos en un protegido o restaurado que el recorrido dio por bueno); (c) ningún control de rechazo (`CR`) o del verificador (`CV`) aceptado indebidamente.

**Éxito completo** de un caso = en el **documento completo**: todas sus `S` transformadas con los sustitutos reales de A06 (redacción y seudonimización según la política de §4), clases `E` presentes eliminadas y registradas, VA–VE superadas, y restauración de **todas** las seudonimizaciones con VM superada. Un caso rechazado por la matriz, con oráculo no disponible, faltante o con cualquier etapa fallida **no** es éxito y **sigue en el denominador**. El porcentaje no compensa un bloqueo.

Si el piso no se cumple, la etapa 2 entrega opciones concretas (cobertura ganada o perdida, coste técnico, efecto sobre las garantías) **sin** modificar denominador, máscaras, zonas ni reglas; el usuario decide.

## 12. Organización y reproducibilidad (etapa 2)

`experiments/a08/` (fuera de la wheel y del sdist, como `experiments/a04/`; `pyproject.toml` y `uv.lock` propios): `corpus/manifest.json` (este manifiesto, congelado), `corpus/<caso>/` (fuente, PDF exportado, `provenance.json`, oráculo geométrico, zonas), `controls/`, `a08/` (generadores, línea base, recorrido candidato, verificador), `outputs/<entorno>/` (resultados, renders, hojas de contacto, `environment.json` con versiones y licencias), `run_macos.sh`, `run_linux.sh` y `docker/`. Un comando por entorno reproduce la medición completa desde los PDF congelados. Los PDF cuyas fuentes incrustadas no sean redistribuibles (§13) se guardan **fuera de Git** (`experiments/a08/corpus-private/`, ignorado) con su SHA-256, procedencia y receta registrados en Git.

## 13. Fuentes tipográficas y licencias

| Uso | Fuente | Licencia | Registro |
|---|---|---|---|
| Cuerpo de T1–T3 (todas las familias) | Noto Serif y Noto Sans (Regular, Bold, Italic) | SIL OFL 1.1 | archivo, versión, SHA-256 y origen oficial, fijados en el primer commit de la etapa 2 antes de exportar |
| Cuerpo de T4 | **Fuente predeterminada** del exportador (p. ej. la del documento en blanco de Word, Google Docs o Writer; la predeterminada de Chrome y de TextEdit) | la de cada proveedor | se registra la fuente efectiva (nombre del subconjunto en `pdffonts`); si su licencia no permite redistribuir el PDF, el PDF va a `corpus-private/` |
| Fuente candidata del sustituto | **Noto Sans Condensed** Regular; alternativa si no puede obtenerse de la distribución oficial: IBM Plex Sans Condensed Regular | SIL OFL 1.1 (ambas) | archivo, versión, SHA-256 y licencia fijados **antes de medir**; se incrusta subconjuntada en el PDF protegido |
| Fixtures sintéticos de controles | Noto Sans / DejaVu Sans | OFL 1.1 / licencia Bitstream Vera-DejaVu (permisiva) | ídem |

Las fuentes se registran solo para el proceso que exporta (macOS) o se instalan en la imagen Docker; no se modifica la configuración del sistema del usuario. En el equipo de Word, el usuario instala las Noto de esta tabla antes de exportar (registro en `provenance.json`).

## 14. Dependencias de prueba (etapa 2) y su licencia

Las del entorno de A04 ([document-processing §15.1](document-processing.md#151-versiones-y-licencias-efectivas-de-los-experimentos-observadas)), fijadas en un `uv.lock` propio: `pikepdf`/QPDF (transformador, MPL-2.0/Apache-2.0), `pypdfium2`/PDFium (Apache-2.0 o BSD-3-Clause; componentes en `BUILD_LICENSES/`), `pypdf` (BSD-3-Clause, recorrido estructural), `fontTools` (MIT, generación y subconjuntos), `Pillow` (MIT-CMU), Poppler y `qpdf` CLI (herramientas externas de verificación, no distribuidas), Chrome, LibreOffice y Swift/AppKit (exportadores). Versiones y licencias transitivas se registran al ejecutar (`environment.json`); D-A04-01 se respeta (PyMuPDF **no** se usa).

## 15. Disponibilidad observada de exportadores (18 de noviembre de 2026, equipo del agente)

| Familia | Disponible para el agente | Observado |
|---|---|---|
| `W` Word | **No** | Word no está instalado en este macOS (27.0.1, arm64). Los 4 casos dependen de que el usuario (o quien disponga de Word para Windows o Mac) exporte según §2 |
| `G` Google Docs | **No sin el usuario** | Requiere la cuenta de Google del usuario; hay navegador (Chrome 157.0.8103.79 y el navegador integrado) |
| `L` LibreOffice | Sí (Docker) | No instalado en macOS; imagen local `cce-a04-render` con LibreOffice 7.4.7 (Debian 12). La etapa 2 puede construir una imagen con una versión más reciente; la versión usada se registra |
| `C` Chromium | Sí | Google Chrome 157.0.8103.79 (macOS arm64), modo `--headless` |
| `M` macOS | Sí | macOS 27.0.1 (26A434), Swift 6.4 (Command Line Tools), AppKit/WebKit del sistema |
| Verificación | Parcial | Poppler 26.10.0 (Homebrew) en macOS; `qpdf` CLI **no** instalado en macOS (sí instalable en la imagen Linux); `pypdfium2`/`pypdf`/`pikepdf` en el entorno de experimentos |

**Sin los casos `W` y `G` (8 de 20), el piso de §11 es inalcanzable** (máximo 12/20 y 0/4 en dos familias). Ver Q-A08-02.
