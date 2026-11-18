# A08 — Registro de decisiones

> **Estado: revisión 1 (etapa 1, solo documental), pendiente de la aprobación del usuario del contrato experimental y del protocolo fijado.** Las decisiones `D-A08-01` a `06` fueron **aprobadas por el usuario** en el encargo de A08 y se registran aquí sin cambios de fondo. Las propuestas `P-A08-nn` están **pendientes de revisión**; las elecciones técnicas `E-A08-nn` no son decisiones de producto; las hipótesis `H-A08-nn` están en el [contrato §15](../pdf-protection.md#15-hipótesis-por-verificar-h-a08-nn). No hay código, corpus exportado ni medición.

Contratos y documentos que usan estas decisiones: [especificación de A08](../features/A08-proteccion-y-restauracion-pdf.md) · [contrato PDF propuesto](../pdf-protection.md) · [protocolo y corpus de D-A04-04](../a08-pdf-corpus-protocol.md). Decisiones heredadas: [A01](A01-decisions.md) a [A07](A07-decisions.md).

Categorías (no mezclarlas): **decisiones aprobadas** (§2) · **elecciones técnicas** (§3) · **propuestas pendientes** (§4) · **diferencias con el playbook** (§5) · **backlog** (§6) · preguntas, tensiones y cuestiones materiales (§7).

## 0. Historial de revisiones

| Rev. | Contenido |
|---|---|
| 1 | Etapa 1 (solo documental): D-A08-01 a 06 registradas; E-A08-01 a 15; P-A08-01 a 07 pendientes; H-A08-01 a 10; protocolo y corpus de D-A04-04 propuestos para congelar; contrato PDF propuesto con capacidades condicionadas a evidencia; cuestiones materiales Q-A08-01 a 11. Sin código, esquema, dependencias, versión ni experimentos. |

## 1. Fuente, base y herencia

- Referencia: `STAGE_3_PLAYBOOK.md`, revisión 6 (copia local ignorada por Git; **no se modifica ni se declara actualizada**): §6 A08 («no basta superponer rectángulos; verificar extracción/copia y estructura relevante además del render»; «los documentos escaneados o mixtos no pasan como protegidos si contienen regiones fuera de cobertura»), §4, §8, §9 y §10. Las decisiones posteriores prevalecen donde difieren (§5).
- **Base verificada**: `main` = `origin/main` = `c6590ec` (merge del PR #6 de A07), árbol limpio, sin rama de A08 previa; rama `feature/a08-pdf-protection-restoration` creada desde ese commit. Identidad Git local del repositorio configurada (no se usó la inferida por el sistema).
- **Estado de las dependencias** (distinguiendo merge, aceptación y ambientes): A01–A05 cerradas y aceptadas; A04 aceptada como investigación acotada con D-A04-04 pendiente para el inicio de A08; **A06** implementada en `0.5.0` y mergeada (merge `5bcf4b6`, PR #5), **aceptación pendiente**; **A07** implementada en `0.6.0` y mergeada (merge `c6590ec`, PR #6, verificado en el historial), **aceptación pendiente**; ambas probadas en macOS y Linux local; según el [informe de A07](../a07-implementation-report.md) no se ejecutaron en GitHub Actions por el agente, y los dos fallos de Actions sobre `c4faa40` fueron informados por el usuario y revisados sin acceso a los logs. A08 se apoya en el código implementado y mergeado de A06 y A07 **sin declararlos aceptados** (Q-A08-07).
- Heredado sin cambios: A01 (D-06/D-07, D-08, D-11/D-33, D-17), A03 (offsets en puntos de código, mock, procedencia), A04 D-A04-01 a 04, A05 D-A05-01 a 03, A06 D-A06-01 a 04 y P-A06-01 a 06, A07 D-A07-01 a 05 y P-A07-01 a 07 (como patrón; A08 no cambia A07).

## 2. Decisiones aprobadas por el usuario

### D-A08-01 — Utilidad medida y comprobación previa

La utilidad se evalúa con **cinco familias de exportación** (Microsoft Word, Google Docs, LibreOffice Writer, impresión de Chromium y exportación PDF de macOS) y un **mínimo global y por familia**. La comprobación inicial de **D-A04-04 precede al pipeline productivo**.

### D-A08-02 — Sustitutos, fuentes y área segura

Se conservan los sustitutos de A06, incluido el **marcador reversible ASCII de 25 caracteres** y `[REDACTED]`. Se compara el uso de la **fuente original** con una **fuente incrustada de licencia compatible** cuando falten glifos o la original no permita un encaje legible. El sustituto debe **caber en el área segura definida de antemano** y **conservar la presentación fuera de ella**. Si no hay una representación segura y legible, **falla el documento**. El marcador extraído debe **conservar exactamente su gramática**.

### D-A08-03 — Cobertura, recursos y matriz

Se **rechazan los escaneados y los mixtos con regiones fuera de cobertura**, incluida una imagen escaneada con capa de texto. Se admiten **recursos decorativos y trazados ordinarios solo bajo reglas verificables**; si no puede justificarse la cobertura de un recurso, se rechaza el caso. Se define la **matriz PDF** de contenido transformable, estructural inspeccionado, eliminable y rechazado. Se aplican los rechazos de A04, incluida la **ausencia de OCR y de aplanado** como solución de respaldo. Se **eliminan las clases auxiliares autorizadas** que admita la matriz y se **registran las clases eliminadas**.

### D-A08-04 — Análisis fuente y compatibilidad

Se amplía la ruta durable existente de `document_analysis` para PDF, con **identidad del extractor, configuración y `SourceMap` versionados**. Se mantiene la **unidad por página** y los **offsets Unicode sobre el texto exacto**. Se demuestra **compatibilidad con trabajos, mapas y derivados anteriores, incluidos los de DOCX**. Si un análisis anterior no puede verificarse bajo el nuevo contrato, se define el **rechazo** y **cómo se obtiene uno válido**. No se reinterpretan offsets ni mapas antiguos de forma silenciosa.

### D-A08-05 — Restauración del derivado exacto

Se restauran **valores Unicode exactos** únicamente desde el **derivado exacto emitido por el motor**, incluida una copia exacta presentada por el host. Se vinculan **scope, contexto, tamaño, digest, formato, versión y posiciones autorizadas** con **autenticación del vínculo**. Se validan **identidad y marcadores antes de descifrar**. Se restaura **en una pasada** aunque el original y el análisis inicial ya no estén disponibles, siempre que el contexto siga vigente. Se conservan **cantidad y geometría de páginas** y la **presentación fuera de las zonas afectadas**; se admite variación del **kerning interno** del valor restaurado. La redacción irreversible y la limpieza auxiliar son **definitivas**.

### D-A08-06 — Operaciones, permisos y lectura

Protección y restauración son **dos operaciones durables por API Python**, con `pdf:protect`, `pdf:restore`, `pdf:protected:read` y `pdf:restored:read`. Proteger exige además **`results:read`** sobre el análisis fuente. La descarga del original conserva **`files:read`**. Se aplican a PDF las garantías de A07 para **lectores, contexto, copia del host y publicación**. Leer el restaurado o su informe exige **resultado y contexto vigentes y scope activo**; para la descarga de bytes se autoriza **después de verificar el spool e inmediatamente antes de entregar el stream**. `delete_job_data` **conserva el vínculo**. Eliminar el contexto **termina la restaurabilidad** de sus derivados. Solo el **PDF restaurado cuyo descarte ya fue confirmado** es elegible para **purga inmediata** bajo el mecanismo de limpieza de A05. `restore_text` puede usar marcadores emitidos en PDF si se cumplen **todas** las condiciones de A06, incluida la evidencia de que el host entregó ese marcador exacto al modelo y de que el llamador tiene acceso al trabajo o la conversación correspondiente.

**Alcance de A08 (del encargo):** hallazgos controlados con el mock de A03; Qwen, inferencia real, AWS, encendido/apagado y evaluación de detección son de A09; se conservan las restricciones de licencia de D-A04-01 (pikepdf/QPDF como punto de partida; PyMuPDF no). Etapas: (1) contrato, corpus y protocolo congelados; (2) comprobación D-A04-04 en `experiments/a08/`; (3) implementación productiva. Cada etapa requiere la aprobación explícita del usuario sobre la anterior; incluso si la etapa 2 cumple el piso, la implementación requiere aprobar el alcance productivo resultante.

## 3. Elecciones técnicas derivadas (`E-A08-nn`; no son decisiones de producto)

| ID | Elección | Dónde |
|---|---|---|
| E-A08-01 | Lo esperado (texto, posiciones, zonas) sale siempre del original, del oráculo y de las selecciones; nunca del transformador | contrato §1, §10; protocolo §6, §9 |
| E-A08-02 | La identidad del extractor DOCX (`cce-docs-extract` 1 y su `config_sha256` sobre las 14 claves actuales) **no cambia**; PDF adopta identidad propia (`cce-pdf-extract`). Es la única forma de no invalidar análisis y derivados DOCX (Q-A08-03) | contrato §4.1 |
| E-A08-03 | Pieza PDF v2 con cadena de usos, flujo del arreglo, rango de códigos, fuente, relación y cuadrilátero; separadores por geometría (`synthesized_line` no seleccionable, `synthesized_space` seleccionable dentro de una línea) [C] | contrato §4.3 |
| E-A08-04 | Transformación local al operador: eliminación de los códigos del valor, sustituto con `Tf`/`Tz` propios, restitución del estado y ajuste de `TJ` para que el sufijo quede en su posición sin tocar `Tlm` [C] | contrato §7.1 |
| E-A08-05 | Fuente del sustituto: la original solo si cubre el alfabeto completo `Σ` (35 caracteres) con correspondencia inversa única; si no, la candidata OFL como TrueType simple subconjuntada con `WinAnsiEncoding` y `ToUnicode`. Regla independiente del token aleatorio (determinismo) | contrato §7.3; protocolo §7.3 |
| E-A08-06 | Verificador estructural con un parser distinto del transformador (`pypdf` estricto frente a `pikepdf`/QPDF) más barrido de bytes propio; PDFium y Poppler como lectores; `qpdf --check` solo para apertura | contrato §10; protocolo §9 |
| E-A08-07 | Corpus: las mismas cuatro plantillas en las cinco familias; sin imágenes rasterizadas en los nativos (se ejercitan en controles); regla de diseño de valores sin coincidencias accidentales | protocolo §3–§5 |
| E-A08-08 | Oráculo en dos capas (autoría escrita antes de exportar; geométrico con PDFium y Poppler congelado antes de medir); desacuerdo ⇒ caso no exitoso, sin reemplazo | protocolo §6 |
| E-A08-09 | Zona `Z1` (valor + espacio libre a la derecha en línea y bloque); máscara = zonas dilatadas 3 px a 150 ppp; 0 píxeles distintos fuera de la máscara por renderizador; cajas fuera de zona ±0,01 pt | protocolo §7 |
| E-A08-10 | Legibilidad: mismo tamaño y línea base, compresión `s ∈ [0,60; 1,00]`, altura ≥ 8 px a 150 ppp, sin `.notdef`, gramática exacta en la extracción; hojas de contacto de todas las zonas para revisión humana | protocolo §7.3 |
| E-A08-11 | La salida se escribe sin object streams ni linealización (estructura normalizada, pérdida declarada) | contrato PT-15, PT-16 |
| E-A08-12 | Flujos de exportación fijados: Microsoft (Word), descarga PDF (Google Docs), `writer_pdf_Export` headless (LibreOffice), `--print-to-pdf` headless (Chrome), `NSPrintOperation`/`WKWebView` (macOS) | protocolo §2 |
| E-A08-13 | T4 usa la fuente predeterminada de cada exportador (representatividad); los PDF con fuentes incrustadas no redistribuibles se guardan fuera de Git con hash y receta | protocolo §12, §13 |
| E-A08-14 | Controles `CS`, `CR` y `CV` con resultado esperado fijado de antemano; un `CR`/`CV` aceptado indebidamente bloquea | protocolo §8, §11 |
| E-A08-15 | Limpieza de `/Info`: elimina toda clave salvo `/CreationDate` y `/ModDate` (paralelo a A07, que conserva fechas) | contrato PT-25 |

## 4. Propuestas pendientes de revisión (`P-A08-nn`)

| ID | Propuesta | Por qué | Si no se aprueba |
|---|---|---|---|
| **P-A08-01** | **Zona extendida `Z2`**: si el sustituto no cabe en `Z1`, la zona incluye el resto de la línea dentro de su bloque y el texto no seleccionado de ese tramo se comprime horizontalmente con el mismo factor (contenido, orden y selección intactos; demás líneas sin cambios). La restauración devuelve la escala original | Con el marcador de 25 caracteres, un valor corto en mitad de línea no cabe en su propio espacio con ninguna compresión legible (Q-A08-01) | Solo `Z1`: los valores cortos en mitad de línea fallan siempre; con el corpus fijado, el piso es inalcanzable por construcción |
| **P-A08-02** | (Reservada; ver Q-A08-02 sobre las exportaciones de Word y Google Docs) | — | — |
| **P-A08-03** | **Lista de recursos decorativos declarada por el host** (SHA-256 de los datos decodificados de imágenes, p. ej. su logotipo): imágenes con ese digest se admiten como decorativas; su contenido queda como **declaración del host**, no como cobertura verificada | Sin OCR no hay regla verificable general para imágenes rasterizadas; sin esta vía, todo documento con logotipo se rechaza | Solo las reglas de §5.4 (uniforme, diminuta); documentos con logotipos o fotografías se rechazan (`uncovered_content`) |
| **P-A08-04** | **Snapshot con conjunto de extractores** `{docx: v1, pdf: v2}` y `requirements_key` nuevo para los `document_analysis` nuevos; el runtime de A08 conserva el extractor PDF v1 **congelado** solo para terminar trabajos ya aceptados por runtimes anteriores; mapas PDF nuevos en `document-structures/2` (cambio visible para hosts que lean mapas) | Ningún runtime anterior debe producir un mapa v1 bajo un snapshot v2, y los trabajos en cola deben terminar con la configuración que fijaron | Alternativa: no reclamar trabajos heredados (terminan por runtimes anteriores o `executor_unavailable`) |
| **P-A08-05** | **Clonado de Form XObjects compartidos** cuando la selección afecta solo a parte de los usos, con prueba de que los demás usos siguen apuntando al objeto original idéntico [C] | Encabezados y elementos repetidos suelen compartirse; A04 solo admitía «todos los usos» | `shared_object_partial` (fallo) |
| **P-A08-06** | Clase `E` **`xmp_metadata`**: se elimina el XMP del documento **completo** (no campo a campo) y el XMP de objetos (`object_metadata`) | D-A04-02 autoriza eliminar metadatos; editar XMP campo a campo exige un analizador XMP y deja más superficie de residuos | Edición campo a campo con lista de conservados (más código y verificación) |
| **P-A08-07** | El vínculo guarda por ocurrencia la **zona** y el **avance del valor original** (`original_advance`), sin valores, para restaurar sin el original; se declara que el derivado y el vínculo dejan inferir la **longitud aproximada** del valor | Conservar la presentación exige reponer el valor en su ancho original | Restaurar con el ancho del sustituto: la posición del sufijo no volvería a la original (incumple D-A08-05) |

## 5. Diferencias respecto del playbook (revisión 6)

| # | Playbook | A08 | Motivo |
|---|---|---|---|
| 1 | «Los documentos escaneados o mixtos no pasan como protegidos si contienen regiones fuera de cobertura» | D-A08-03: además se **rechazan** (no solo «no pasan como protegidos») y los recursos decorativos y trazados se admiten solo por reglas verificables | Decisión del usuario |
| 2 | Sin criterio cuantitativo de utilidad | D-A08-01: cinco familias, mínimo global (16/20) y por familia (3/4), al menos un Type0/CID nativo | Decisión del usuario; D-A04-04 |
| 3 | «Fuentes, espacios y desbordamientos» sin regla | D-A08-02: área segura definida de antemano, fuente original o candidata, fallo del documento si no hay representación segura y legible | Decisión del usuario |

Lo no listado permanece vigente.

## 6. Backlog (fuera de los criterios de aceptación)

| ID | Mejora |
|---|---|
| B-A08-01 | OCR y cobertura de imágenes y trazados con texto (sigue fuera: D-A08-03) |
| B-A08-02 | Formularios, anotaciones con apariencia, capas y adjuntos |
| B-A08-03 | Escritura vertical, CJK, escrituras de derecha a izquierda |
| B-A08-04 | Reflujo real de párrafos (más allá de la línea) para sustitutos largos |
| B-A08-05 | Conservar linealización y conformidad PDF/A/PDF/UA en la salida |
| B-A08-06 | Firmas: invalidación explícita y consentida |
| B-A08-07 | Renderizador de runtime para comparar píxeles en cada trabajo (hoy evidencia de aceptación) |
| B-A08-08 | Microsoft Word como lector para la inspección visual |

## 7. Preguntas abiertas, tensiones y cuestiones materiales

| ID | Cuestión | Evidencia | Efecto / qué se necesita del usuario |
|---|---|---|---|
| **Q-A08-01** | **Marcador de 25 caracteres frente a valores cortos en mitad de línea** | Cálculo previo (no medición): `Gil` (3) o `Íñigo` (5) a 11 pt ocupan del orden de 17–30 pt; el marcador en la fuente candidata condensada, aun comprimido al 60 %, del orden de 80 pt; `[REDACTED]` tampoco cabe. Las plantillas T1, T3 y T4 tienen un valor corto en mitad de línea | Con solo `Z1`, 15 de 20 casos fallarían por construcción y el piso (16/20, 3/4 por familia) sería **inalcanzable**. Decidir antes de aprobar: aprobar **P-A08-01** (`Z2`), o aceptar que la medición con `Z1` informará previsiblemente un piso no cumplido. No se propone mover los valores cortos a posiciones favorables: sería ajustar el corpus para aprobar |
| **Q-A08-02** | **Exportadores no disponibles para el agente**: Word (no instalado) y Google Docs (requiere la cuenta del usuario) | Inventario del equipo, 18-11-2026 ([protocolo §15](../a08-pdf-corpus-protocol.md#15-disponibilidad-observada-de-exportadores-18-de-noviembre-de-2026-equipo-del-agente)) | Sin los 8 casos `W`/`G`, el piso es inalcanzable (máximo 12/20). El usuario exporta (o hace exportar) los casos `W1`–`W4` y `G1`–`G4` siguiendo el protocolo §2 a partir de los `.docx` que genere la etapa 2, o acepta que cuenten como faltantes |
| **Q-A08-03** | **Identidad de extractor compartida DOCX/PDF** | `jobs/ident.py`: `cce-docs-extract` 1 y `config_sha` sobre 14 claves (incluidas las de PDF); A07 la compara en S7 (`docx_protection/execute.py`) y en T5 al restaurar | Subir la versión rompería los análisis DOCX y la restauración de **todos** los derivados DOCX. Resuelto en la propuesta con E-A08-02 y P-A08-04 |
| Q-A08-04 | Token aleatorio y cobertura de glifos | El token es aleatorio (`secrets`); un subconjunto de fuente puede cubrir unos tokens y otros no | Resuelto con la regla del alfabeto completo (E-A08-05); informativo |
| Q-A08-05 | Imágenes rasterizadas en documentos reales | D-A08-03 y ausencia de OCR | Los documentos con logotipos o fotografías se rechazan salvo P-A08-03; el corpus nativo no los incluye y lo declara |
| Q-A08-06 | Longitud inferible del valor | La compresión del sustituto y `original_advance` | Limitación a aprobar con P-A08-07 (A06 §6.4 ya declara que los marcadores revelan posición e igualdad) |
| Q-A08-07 | A06 y A07 no aceptadas | CLAUDE.md, informes de A06 y A07 | A08 se construye sobre su código mergeado; un cambio que surja de su aceptación puede afectar a A08 |
| Q-A08-08 | **Independencia del verificador en producción** | `pikepdf` usa QPDF; `qpdf --check` es la misma biblioteca | La etapa 3 necesita un parser PDF de runtime distinto (`pypdf`, BSD-3-Clause, hoy solo dependencia de desarrollo) o declarar la limitación; la comparación de render en runtime exigiría `pypdfium2` (binario PDFium, licencias en `BUILD_LICENSES/`). Decisión de dependencias en la aprobación del alcance productivo (D-A04-01) |
| Q-A08-09 | Fuente candidata empaquetada | OFL-1.1 permite incrustarla y distribuirla con software, con su licencia | Aumenta la wheel; la licencia del motor sigue pendiente para A10 |
| Q-A08-10 | Margen de D-33 | El informe de A07 registra 320,7 ms informados por el usuario en Actions frente al umbral de 300 ms (no reproducido localmente) | A08 debe mantener todo lo PDF y criptográfico fuera de la importación base; riesgo a vigilar en la etapa 3 |
| Q-A08-11 | Restauración por correspondencia inversa | Una fuente puede mapear varios códigos al mismo carácter | Fallo cerrado `restoration_encoding_unavailable` si no es única; la etapa 2 mide la frecuencia [C] |
