# A08 — Informe de la comprobación D-A04-04 (etapa 2)

> **Estado: etapa 2 EJECUTADA en macOS y en Linux local (Docker) sobre el corpus congelado; resultados parciales porque faltan las 8 exportaciones de Word y Google Docs.** Esto **no** es un acta, **no** autoriza la etapa 3 y **no** acredita calidad de detección (hallazgos controlados con el oráculo; el mock de A03 no interviene en el prototipo). Con los criterios congelados, **el piso no se cumple: 0/20**. Las variantes informativas, fuera del cómputo, llegan a 5/20 (6/20 con tolerancia de rasterizado), y ni con todas ellas el piso es alcanzable con el corpus fijado. **No hubo bloqueos**: ningún residuo en lo emitido, todos los rechazos de control resultaron como se esperaba y los 30 controles del verificador fueron detectados. El alcance productivo y cualquier cambio de criterio son decisión del usuario (§7). GitHub Actions no se ejecutó.

Documentos: [protocolo (rev. 2, congelado)](a08-pdf-corpus-protocol.md) · [contrato propuesto](pdf-protection.md) · [especificación](features/A08-proteccion-y-restauracion-pdf.md) · [decisiones](decisions/A08-decisions.md). Evidencia: `experiments/a08/outputs/` (resultados por entorno, `summary.json`, `controls.json`, `cv.json`, `rules.json`, PDF protegidos y restaurados, vínculos sin valores, hojas de contacto en `outputs/macos-inf/sheets/`).

## 0. Identificación

| Elemento | Valor |
|---|---|
| Base | `main` = `c6590ec`; rama `feature/a08-pdf-protection-restoration` |
| Etapa 1 aprobada | `1afd04d` |
| Precisiones del protocolo (rev. 2), **antes** de medir | `c9c1104` |
| Congelamiento de entradas C/L/M (PDF, procedencia, oráculo y zonas), **antes** de ejecutar el candidato | `5f1af03` (hashes en `experiments/a08/corpus/FREEZE.json`) |
| Prototipo (primera versión completa) | `71128a5` |
| Código de la medición informada | `2cd779a` (código, resultados y este informe en el mismo commit) |
| macOS | 27.0.1 (26A434), arm64, Python 3.13.16 |
| Linux local | Docker `cce-a08:1` (Debian 13.x, aarch64, glibc 2.41), Python 3.13.18; LibreOffice 25.2.3.2, `qpdf` 12.2.0, Poppler 25.03.0 |
| Bibliotecas (ambos) | pikepdf 10.18.2 (QPDF 12.4.2), pypdfium2 5.15.2 / PDFium 159.0.8149.0, pypdf 7.1.1, fontTools 4.68.0, Pillow 13.0.0, numpy 2.5.5 (`experiments/a08/uv.lock`) |
| Verificación en macOS | Poppler 26.10.0; `qpdf --check` **no** disponible (registrado como «no ejecutado»; sí en Linux) |

## 1. Piso de avance (protocolo §11)

| Medición | Total | W | G | L | C | M | Type0/CID nativo | Bloqueos | ¿Avanza? |
|---|---|---|---|---|---|---|---|---|---|
| **Oficial (criterios congelados)**, macOS y Linux | **0/20** | 0/4 (faltan) | 0/4 (faltan) | 0/4 | 0/4 | 0/4 | no | ninguno | **no** |
| Informativa TB+AT+RZ, render estricto | 5/20 | 0/4 | 0/4 | 2/4 | 2/4 | 1/4 | **sí** (`C1`, `C2`) | ninguno | no |
| Informativa TB+AT+RZ + tolerancia RT | 6/20 | 0/4 | 0/4 | 2/4 | 2/4 | 2/4 | sí | ninguno | no |

Los resultados de macOS y de Linux coinciden caso por caso (`outputs/{macos,linux}{,-inf}/summary.json`). **Cota superior con el corpus fijado:** aun si Word y Google Docs completaran 8/8 bajo las variantes, el máximo sería 14/20 y ninguna familia medida llega a 3/4, porque T3 (valores partidos entre líneas) y T4 (oráculo y filetes) fallan en todas.

**Variantes informativas** (fijadas en archivos aparte y con hash en `FREEZE.json` **antes** de ejecutarlas; nunca cuentan): **TB**: banda vertical de la zona = caja tipográfica de la línea, en vez de la tinta; **AT**: `/ActualText` admitido solo si coincide con el texto de sus glifos; **RZ**: en la restauración `Z2`, la redacción vuelve con su línea; **RT**: tolerancia del rasterizado de PDFium (§3.4).

## 2. Resultados por caso

| Caso | Línea base (extractor v1) | Oficial: etapa y motivo | Informativa (TB+AT+RZ) | Zona y factor |
|---|---|---|---|---|
| W1–W4, G1–G4 | — | **faltante** (`export_unavailable`) | faltante | — |
| L1 | rechazo `executable_action` | `plan` · `replacement_overflow` (sustituto más alto que la banda de tinta) | **éxito** | `Z2` en S2 (f = 0,672), resto `Z1` |
| L2 | rechazo `executable_action` | `plan` · `replacement_overflow` (altura) | **éxito** | solo `Z1` |
| L3 | rechazo `executable_action` | `plan` · `selection_crosses_boundary` (S1 y S4 partidos entre líneas) | igual | — |
| L4 | rechazo `incremental_update` | `oracle` · `oracle_unavailable` (§3.5) | igual | — |
| C1 | rechazo `unsupported_structure` | `gate` · `alternate_text_duplication` (`/ActualText` de ligaduras) | **éxito** (Type0/CID) | `Z2` en S2 (f = 0,665) |
| C2 | rechazo `unsupported_structure` | `gate` · `alternate_text_duplication` | **éxito** (Type0/CID) | solo `Z1` |
| C3 | rechazo `unsupported_structure` | `gate` · `alternate_text_duplication` | `gate` · `uncovered_content` (bordes del recuadro) y valores partidos | — |
| C4 | rechazo `unsupported_structure` | `gate` · `alternate_text_duplication` | `gate` · `uncovered_content` (filete `<hr>`, regla refutada) | — |
| M1 | rechazo `unsupported_structure` | `plan` · `replacement_overflow` (altura) | falla **solo** el rasterizado estricto de PDFium (RT: éxito) | `Z2` en S2 (f = 0,642) |
| M2 | rechazo `unsupported_structure` | `plan` · `replacement_overflow` (altura) | **éxito** | solo `Z1` |
| M3 | rechazo `unsupported_structure` | `plan` · `selection_crosses_boundary` | igual | — |
| M4 | rechazo `unsupported_structure` | `oracle` · `oracle_unavailable` (TextEdit no importa notas al pie) | igual | — |

- En todos los casos procesados, la fuente usada fue la **candidata** (intentos 2 y 4): ningún subconjunto original cubría el alfabeto `Σ` con glifos reales. En Quartz, la codificación MacRoman «cubría» `Σ` pero el subconjunto incrustado no tenía esos glifos; una primera versión los aceptó y salieron huecos, y desde entonces se exige el glifo en el programa de fuente. La fuente original no se usó en ningún caso.
- Clases eliminadas y registradas: `document_info_descriptive` (2–6 claves) y `xmp_metadata` (LibreOffice). Reglas de recursos usadas: `RP3` (rejillas de tablas) y `RP7` (relleno blanco de página de Quartz).
- Restauración: en los 6 casos procesados se restauraron **todos** los valores Unicode exactos, incluido el formato mixto («**María José** Núñez Ibáñez»), sin el original ni el análisis, solo con el derivado y el vínculo. El vínculo guarda los tramos de fuente del valor (§5, P-A08-07). Las redacciones y las clases `E` siguen ausentes.
- Tiempo por caso: 1,6–2,1 s (macOS). Memoria pico del proceso: 166 MB (macOS) y 124 MB (Linux). El `SourceMap` v2 es determinista y **byte a byte idéntico** entre macOS y Linux (H-A08-01).

## 3. Causas, por orden de impacto

1. **Banda vertical de la zona (criterio congelado §7.1).** La zona mide la **tinta** de la línea, y los corchetes del sustituto suben y bajan más que las cifras y las mayúsculas. En las líneas sin descendentes («Documento 00000000-X», casi todas las celdas de T2) el sustituto se sale verticalmente: ningún intento cabe. Afecta a `L1`, `L2`, `M1` y `M2`. Con la banda tipográfica (TB) todos encajan sin pisar la tinta de otras líneas, porque la banda se recorta contra ella.
2. **`/ActualText` de Chromium.** Skia marca cada ligadura `ﬁ` con `/Span <</ActualText (fi)>>`. La regla heredada de A04/A05 (PT-24) rechaza todo `/ActualText`, así que la familia entera queda fuera. Con AT se admite solo si coincide con el texto de sus glifos, y una selección dentro de ese tramo falla.
3. **Valores partidos entre líneas (T3).** En columnas estrechas, el nombre largo y la dirección se cortan en las tres familias. El protocolo lo prevé como fallo (`selection_crosses_boundary`). Transformar selecciones en varias líneas no está en el alcance del prototipo.
4. **Rasterizado de PDFium.** PDFium vuelve a rasterizar todos los glifos de un objeto de texto modificado y desplaza su suavizado hasta 1 px (diferencias de hasta 255 niveles). Las posiciones extraídas no cambian (±0,01 pt), Poppler da 0 px y el render sin texto es idéntico. Con 0 px estrictos para PDFium, cualquier valor que comparta operador con texto anterior falla (`M1`). RT admite esa diferencia solo si todo píxel distinto cae sobre la tinta de un glifo cuya posición extraída no cambió.
5. **Oráculo no disponible.** En `L4`, mi contexto de autoría omitió la viñeta (U+F0B7) que Writer dibuja como glifo. Es un defecto del corpus, no del exportador; por la regla congelada cuenta como fallo y no se reescribe. En `M4`, el camino de TextEdit pierde la nota al pie que contiene S3.
6. **Recursos.** Los controles adversarios refutaron `RI1`, `RI2`, `RP1`, `RP2` y `RP4` (§4.1). Sin ellas, los subrayados de enlace, los filetes (`<hr>`, separadores de nota) y los recuadros dibujados con lados sueltos se rechazan (`C3`, `C4` y los T4).
7. **Inconsistencia en la regla congelada de restauración `Z2`** (§0.4): pide devolver los caracteres no seleccionados a su posición original y, a la vez, dejar `[REDACTED]` donde lo dibujó el derivado. Si una línea `Z2` tiene una redacción después de una seudonimización, ambas cosas chocan («[REDACTE,Dp]ago»). Ningún caso nativo la tiene; sí los controles CS01/03/05/06/09/10/11/13, que con RZ completan el ciclo.

## 4. Controles (no cuentan en la tasa)

### 4.1 Adversarios de reglas de recursos (§0.7)

| Control | Representa texto con | Admitido por | Regla refutada |
|---|---|---|---|
| CA01 | imagen 16×16 a 6 pt con «74» | `RI2` | `RI2` |
| CA02 | «GIL» con segmentos rectos | `RP1` | `RP1` |
| CA03 | «EF» con filetes largos | `RP1`, `RP4` | `RP4` |
| CA04 | «H» con imágenes uniformes de 1 px | `RI1` | `RI1` |
| CA05 | «H» con rectángulos rellenos | `RP1` | — |
| CA06 | cifra «1» con bordes extra dentro de una tabla | `RP1` (no `RP3`) | — |
| CA07 | «SO» con curvas de ≤ 6 pt | `RP2` | `RP2` |

Sobreviven `RP3` (rejilla rectangular completa), `RP5` (relleno de fondo que contiene un glifo) y `RP7` (relleno blanco de página). Que sobrevivan **no** prueba que sean seguras: por ejemplo, una rejilla completa de barras verticales podría leerse como «1111», y eso no se probó.

### 4.2 Rechazos seguros (CR) y estructurales (CS)

- **CR01–CR23**: los 23 dieron el resultado esperado en macOS y en Linux (cifrado, firmas, actualización incremental real, xref rota, formularios, capas, adjuntos, JavaScript, `FreeText`, `/ActualText`, Type3, `Identity-V`, CMap no Identity, Type0 sin `ToUnicode`, escaneado, escaneado con capa OCR invisible, imagen con texto, texto en trazados, operador partido entre flujos, Form XObject usado por una anotación, texto fuera de la `CropBox`, texto tapado después, valor cortado con guion). **No construidos:** CR24 (derecha a izquierda; la condición está en el planificador, sin control), y CR25/CR26 (residuo en el esquema o en `/URI`), cuya detección se probó como CV05 y CV06.
- **CS** (banda oficial / informativa TB+RZ):

| Control | Variante | Oficial | Informativa |
|---|---|---|---|
| CS01 | Type0 `CIDFontType2` sintético | falla la restauración (RZ) | ciclo (RT) |
| CS02 | `CIDFontType0` (CFF) | **no construido** (sin conversión TrueType→CFF en el entorno) | — |
| CS03 | object streams + xref stream | ídem CS01 | ciclo (RT) |
| CS04 | valor repartido entre dos flujos de `/Contents` | `selection_not_transformable` (no soportado) | ídem |
| CS05 | `Tc`, `Tw`, `Tz`, `Ts`, `TL`, `T*` | ídem CS01 | **ciclo** |
| CS06 | `Tw` con Type0 y `Tc` negativo | ídem CS01 | ciclo (RT) |
| CS07a/b | Form XObject compartido (todos los usos / uno) | `selection_not_transformable` (sin soporte de formularios ni clonado) | ídem |
| CS08 | `/Rotate 270` y `Tm` a 270° | `oracle_unavailable` (los lectores no coinciden en el texto rotado) | ídem |
| CS09 | inversión vertical tipo Skia | ídem CS01 | ciclo (RT) |
| CS10 | entrada linealizada | ídem CS01 | ciclo (RT) |
| CS11 | `Tr 2` | ídem CS01 | **ciclo** |
| CS12 | dos seudonimizaciones en una línea | ciclo (RT) | ciclo (RT) |
| CS13 | redacción y seudonimización contiguas | ídem CS01 | ciclo (RT) |

### 4.3 Controles del verificador (CV01–CV30)

Los **30** se detectaron en macOS y en Linux (base: `L1` y `L2` de la variante TB, que pasan todas las comprobaciones de forma estricta). La primera comprobación que falló en cada uno: VZ (superposición, sustituto como trazados, `ToUnicode` erróneo, original presentado como protegido, carácter del sufijo `Z2` perdido o cambiado, `[REDACTED]` eliminado al restaurar); VT (copia fuera de la `CropBox`, copia `Tr 3`, carácter cambiado, línea desplazada 0,5 pt, primer carácter conservado, marcador con espacio, restaurado en otra posición o de otro derivado); VE (`/Info /Title`, XMP); VK (título del esquema); VS (copia en un object stream); VG (objeto huérfano añadido); VB (copia con otra fuente en un Form XObject no usado); VR (enlace con `/URI` o `/Contents`, color cambiado, trazado o borde eliminado, anotación desplazada); el oráculo `Z2` (sufijo desplazado 1 pt, restauración con el sufijo comprimido); y el restaurador (valores intercambiados, por la estructura de tramos del vínculo). **Corrección durante el desarrollo:** CV17, CV24 y CV26 estaban mal construidos en una primera versión (actuaban sobre un espacio o sobre una fuente WinAnsi que sigue siendo extraíble); se rehicieron sobre la alteración que dice el protocolo. CV19 destapó que VZ quitaba los espacios antes de comprobar la gramática; ahora comprueba el marcador con los espacios en el orden de lectura de PDFium.

## 5. Hipótesis

| ID | Resultado |
|---|---|
| H-A08-01 | **Confirmada:** extracción determinista y `SourceMap` idénticos entre macOS y Linux |
| H-A08-02 | **Confirmada:** el extractor v1 rechaza las 12 exportaciones medidas por razones ajenas al contenido: `/OpenAction` de destino y `/Prev` de esquema detectados como bytes (L), escalas negativas de `cm`/`Tm` (C y M) |
| H-A08-03 | **Parcial:** Chromium sí (CID TrueType `Identity-H`, subconjuntadas); Google Docs sin evidencia (faltante) |
| H-A08-04 | **Refutada para PDFium** (rasterizado, §3.4); confirmada para posiciones (±0,01 pt) y para Poppler (0 px) |
| H-A08-05 | **Confirmada:** sin objetos inalcanzables ni residuos tras reescribir |
| H-A08-06 | **Confirmada en el corpus:** correspondencia inversa única en todos los valores restaurados |
| H-A08-07 | **Confirmada con salvedad:** `pypdf` estricto sirve como parser independiente, pero su extracción de texto modifica objetos en caché; el recorrido usa un lector nuevo |
| H-A08-08 | **Confirmada:** PDFium y Poppler extraen el marcador comprimido sin espacios internos |

## 6. Notas del verificador y del desarrollo (declaradas)

- **N-1 (VT):** los espacios fuera de las zonas se comparan como separación de palabras, porque PDFium decide de forma heurística si un espacio sale como carácter real o generado. Los caracteres con tinta se comparan con Unicode, orden y caja (±0,01 pt).
- **N-2 (VZ, Poppler):** Poppler agrupa por palabras; el texto esperado de la zona debe aparecer en el texto de Poppler de la página. Las posiciones se comprueban carácter a carácter con PDFium.
- **N-3 (VR):** se registra cuántos píxeles difieren con PDFium y si todos caen sobre la tinta de glifos sin cambio de posición (dato para RT); el resultado oficial sigue siendo 0 px.
- **N-4:** las comprobaciones estructurales usan un lector `pypdf` nuevo (H-A08-07), y la identidad de los flujos de contenido se toma de ese mismo lector. Un falso positivo de VK en `M1` venía de mezclar dos lectores.
- **N-5 (repetición de la medición oficial):** la primera corrida oficial (`outputs/macos-run1/`, conservada) usaba en `RP5` la caja **tipográfica** del glifo, en vez de la de tinta que define el protocolo, y rechazaba `L3` por el recuadro sombreado. Tras corregirlo se repitió todo; el resultado agregado no cambió (0/20) y `L3` pasó a fallar por valores partidos.
- **Desarrollo:** el prototipo se depuró contra el corpus y los controles congelados (protocolo §0.11). Correcciones de implementación sin cambiar ningún criterio: localización por caja de tinta en vez de avance; inicio del sustituto en el borde de la zona; bloque insertado **dentro** del operador; conjunto de glifos movidos de `Z2` calculado con cajas de tinta; tramos de fuente en el vínculo; glifos reales de la fuente original; operador malformado convertido en rechazo cerrado.

## 7. Propuesta limitada a la evidencia y opciones

**Lo que la evidencia sostiene hoy** (macOS y Linux, corpus sintético, 6 nativos procesados y controles), si el usuario aprueba los criterios correspondientes:

- Extractor v2 con identidad propia: fuentes simples (TrueType WinAnsi/MacRoman con o sin `ToUnicode`) y Type0 `Identity-H` con `CIDFontType2`; estado de texto completo; inversiones de ejes de Skia y Quartz; object streams; linealizados; detección estructural de incrementales, acciones y adjuntos. Determinista.
- Transformación local por operador con fuente candidata Noto Sans Condensed, en `Z1` y `Z2` (f entre 0,64 y 0,68); restauración exacta y en una pasada desde derivado y vínculo; limpieza de `/Info` y XMP; verificación independiente (PDFium, Poppler y `pypdf`) con 30/30 controles detectados y 23/23 rechazos correctos.
- **Sin evidencia o refutado:** CFF (`CIDFontType0`), Form XObjects y su clonado (P-A08-05), valores repartidos entre flujos, rotación 270°, selecciones en varias líneas, reglas de imágenes y trazados salvo `RP3`/`RP5`/`RP7`, fuente original como sustituto, Word y Google Docs.

**Opciones** (el denominador, las máscaras y las reglas congeladas no se cambiaron; cualquiera de estas opciones exige una nueva revisión del protocolo y una medición completa nueva):

| Opción | Gana (con este corpus) | Coste técnico | Efecto sobre las garantías |
|---|---|---|---|
| **O1 — TB:** banda vertical tipográfica recortada contra la tinta ajena | `L1`, `L2`, `M2` (y `M1` con RT); necesaria para T1/T2 en todas las familias | bajo (ya prototipado) | el sustituto puede ocupar el interlineado propio; sigue sin tocar la tinta de otras líneas (VT/VR/VB) |
| **O2 — AT:** `/ActualText` igual al texto de sus glifos | `C1`, `C2`; probablemente Google Docs si usa Skia (sin evidencia) | bajo | se inspecciona como residuo; una selección dentro de ese tramo falla |
| **O3 — RT:** tolerancia de rasterizado PDFium (§3.4) | `M1` y los CS | bajo | diferencia subpíxel visible al ampliar fuera de la zona; posiciones y Poppler siguen estrictos |
| **O4 — RZ:** la redacción acompaña a su línea al restaurar `Z2` | resuelve la inconsistencia del §3.7 | bajo | la redacción conserva texto y orden; su posición pasa a ser la original |
| **O5:** selecciones en varias líneas | T3 (3–5 casos) | alto: zonas y planes por varias líneas, reflujo entre ellas | nuevas reglas de presentación |
| **O6:** reglas estructurales para subrayados, filetes y recuadros (no geométricas por tamaño) | T4/C3/C4 | medio; deben superar nuevos controles adversarios | sin prueba de seguridad: solo «no refutadas» |
| **O7:** revisión del oráculo de `L4` (contexto con viñeta) | `L4` como medible | trivial | ninguno (es un defecto de autoría) |
| **O8:** exportaciones de Word y Google Docs | 8 casos y 2 familias | depende del usuario | — |

**Lectura:** con O1–O4 y O7–O8 el piso **sigue** sin alcanzarse mientras T3 y T4 fallen en todas las familias. Alcanzarlo exige O5 y O6, o revisar el corpus o el piso con el usuario (D-A04-04: «si el subconjunto resulta insuficiente, se revisa el alcance»). La etapa 3 queda detenida a la espera de esa decisión.

## 8. Reproducción

```bash
cd experiments/a08 && ./run_macos.sh
```

```bash
cd experiments/a08 && ./run_linux.sh
```

```bash
cd experiments/a08 && .venv/bin/python -m a08.summary macos macos-inf linux linux-inf
```

```bash
cd experiments/a08 && .venv/bin/python -m a08.sheets macos-inf
```

Exportadores: `exporters/export_c.sh`, `exporters/export_l.sh`, `swift exporters/export_m.swift fonts corpus/sources corpus/M`; congelamiento: `a08/intake.py` y `a08/oracle.py`. Fuentes: `fonts/fetch_fonts.sh` (SHA-256 verificados). Los PDF `C4` y `M4` (Times de Apple) están en `corpus-private/`, fuera de Git; `FREEZE.json` y `provenance.json` registran su hash y su receta. Licencias: pikepdf (MPL-2.0) y QPDF (Apache-2.0), pypdfium2 (Apache-2.0 o BSD-3-Clause) y PDFium (componentes en `BUILD_LICENSES/`), pypdf (BSD-3-Clause), fontTools (MIT), Pillow (MIT-CMU), Noto (OFL-1.1); Poppler, `qpdf` CLI y LibreOffice son herramientas externas de verificación o exportación, no se distribuyen. PyMuPDF no se usó (D-A04-01).

## 9. Pendiente

- **Exportaciones de Word y Google Docs** según [`experiments/a08/corpus/EXPORT-W-G.md`](../experiments/a08/corpus/EXPORT-W-G.md). Al recibirlas, se congelan (hash, oráculo y zonas) y se miden con el mismo código; el denominador sigue siendo 20.
- Decisión del usuario sobre §7 antes de la etapa 3 (runtime, migraciones, permisos). P-A08-04 a 07 y la dependencia del verificador en producción quedan para la revisión del contrato productivo; el prototipo aporta: P-A08-04 (identidad propia, sin tocar DOCX: hecho en el prototipo); P-A08-05 (clonado: sin evidencia); P-A08-06 (eliminar el XMP completo: funcionó); P-A08-07 (zona y avance en el vínculo: necesarios, y además los **tramos de fuente** para restituir formato mixto). Verificador de producción: `pypdf` como parser independiente funcionó; la comparación de render requiere PDFium y Poppler y, de momento, es evidencia de aceptación.
