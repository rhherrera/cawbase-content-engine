# A08 — Informe de la etapa 2B (PDF digital con selecciones de una línea)

> **Estado: etapa 2B EJECUTADA en macOS y en Linux local (Docker) sobre el corpus fijado. Resultado parcial: faltan las 4 exportaciones de Word y Google Docs.** Con el piso fijado, **el piso no se cumple: 2/10** ciclos completos (`L1`, `C1`). **Recomendación binaria: la evidencia sigue siendo insuficiente** para revisar un contrato productivo acotado (§6). No hubo bloqueos:
>
> - ningún residuo ni pérdida colateral en lo emitido;
> - 35/35 controles negativos rechazados por el motivo esperado;
> - 35/35 controles del verificador detectados.
>
> El piso **no es alcanzable con este corpus** aunque llegaran las cuatro exportaciones pendientes: la cota es 6/10 y la familia macOS queda en 0. Las causas son dos limitaciones identificadas (§3). RT es solo diagnóstico y no cuenta. Esto **no** es un acta, **no** autoriza la etapa 3 y **no** acredita calidad de detección (hallazgos controlados). GitHub Actions no se ejecutó.

Documentos: [protocolo 2B (fijado)](a08-stage2b-protocol.md) · [informe de la etapa 2 (histórico)](a08-feasibility-report.md) · [contrato propuesto](pdf-protection.md) · [decisiones](decisions/A08-decisions.md). Evidencia en `experiments/a08/outputs/`:

- `stage2b-macos/` y `stage2b-linux/`: `results.json`, `summary.json`, `controls.json` y `cv.json`; PDF protegidos y restaurados y vínculos sin valores en `cases/`; hojas de contacto de macOS en `sheets/`.
- `stage2b-macos-run1/`: la primera corrida, conservada (§5, N-2B-3).

## 0. Identificación

| Elemento | Valor |
|---|---|
| Punto de partida | estado local de la etapa 2, `2952f4f`; rama `feature/a08-pdf-protection-restoration` |
| Protocolo, manifiesto, criterios y controles fijados **antes** de medir | `c89b202` |
| Entradas `L1`, `C1`, `M1` (reutilizadas) y `L5`, `C5`, `M5` congeladas **antes** de ejecutar el candidato | `33787d3` (`experiments/a08/corpus2b/FREEZE.json`) |
| Código de la medición informada | `3d332b4`: corrección del verificador tras la corrida 1 (N-2B-3), sin cambios en el candidato |
| macOS | 27.0.1 (26A434), arm64, Python 3.13.16; Poppler 26.10.0; `qpdf --check` no disponible (registrado como «no ejecutado») |
| Linux local | Docker `cce-a08:1` (Debian 13, aarch64), Python 3.13.18; LibreOffice 25.2.3.2, `qpdf` 12.2.0, Poppler 25.03.0 |
| Bibliotecas (ambos) | pikepdf 10.18.2 (QPDF 12.4.2), pypdfium2 5.15.2 / PDFium 159.0.8149.0, pypdf 7.1.1, fontTools 4.68.0, Pillow 13.0.0, numpy 2.5.5 (`experiments/a08/uv.lock`) |
| Exportadores | Chrome 157.0.8103.79 (headless), LibreOffice 25.2.3.2 (`cce-a08:1`), AppKit de macOS 27.0.1 (Swift 6.4) |

## 1. Piso (protocolo 2B §5)

| Medición | Total | W | G | L | C | M | Type0/CID con texto seleccionado | Negativos aceptados | CV no detectados | Bloqueos | ¿Avanza? |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **macOS** | **2/10** | 0/2 (faltan) | 0/2 (faltan) | 1/2 | 1/2 | **0/2** | **sí** (`C1`) | 0 de 35 | 0 de 35 | ninguno | **no** |
| **Linux local** | **2/10** | 0/2 (faltan) | 0/2 (faltan) | 1/2 | 1/2 | **0/2** | **sí** (`C1`) | 0 de 35 | 0 de 35 | ninguno | **no** |

- **Coincidencia entre entornos:** macOS y Linux coinciden caso por caso en el resultado, la etapa y el motivo, el tipo de zona y el intento de cada selección, y el `SourceMap`, que es determinista e idéntico byte a byte. `qpdf --check` pasó en Linux en todas las salidas verificadas.
- **El factor `Z2` no es determinista entre ejecuciones:** depende del ancho del marcador, y el token de A06 es aleatorio. Por ejemplo, `L1` S2 dio 0,674 en macOS y 0,678 en Linux, y `M1` S2 0,657 y 0,671; la corrida 1 de macOS dio otros valores. Por la misma razón, en CV18 la primera comprobación que falla es VZ en macOS y VT en Linux. Ambas lo detectan.
- **Diagnóstico RT, no cuenta:** `L5`, `M1` y `M5` completan todo lo demás y fallan **solo** por el render estricto de PDFium fuera de las zonas. Todos sus píxeles distintos se explican por glifos sin cambio de posición extraída cuyo raster se desplazó como máximo 1 px; Poppler da 0 px.
- **Cota superior con este corpus:** si las cuatro exportaciones de Word y Google Docs completaran el ciclo, el total sería **6/10** y la familia macOS seguiría en 0. El piso no es alcanzable sin resolver las limitaciones del §3, y no se rebaja.

## 2. Resultados por caso

| Caso | Línea base (extractor v1) | Resultado: etapa y motivo | Zonas: tipo y factor (intento) | Restauración |
|---|---|---|---|---|
| `W1`, `W5`, `G1`, `G5` | — | **faltante** (`export_unavailable`) | — | — |
| `L1` (T1, reutilizado) | rechazo `executable_action` | **ciclo completo** | S1 `Z1`; S2 `Gil` en `Z2` f = 0,674 (macOS; 0,678 en Linux); S3–S5 `Z1` (todos con el intento 2 o el 4, fuente candidata) | exacta desde el derivado |
| `L5` (T5) | rechazo `executable_action` | `verify` · VR de PDFium, 1281 px (**RT: todos explicados**) | S1 y S2 `Z1` (compresión 0,890 y 0,824); S3, S4+S5, S6+S7 y S8+S9 en `Z2` con f = 1,000; S10 `Z1` | verificada; RZ: `350 €` restaurado como `[REDACTED]` a 0,60 con 3,16 pt de exceso |
| `C1` (T1, reutilizado) | rechazo `unsupported_structure` | **ciclo completo**, con el **texto seleccionado en Type0/CID `Identity-H` subconjuntado** | S1 `Z1`; S2 `Z2` f = 0,673 (0,670 en Linux); S3–S5 `Z1` | exacta |
| `C5` (T5) | rechazo `unsupported_structure` | `plan` · `zone_plan_unjustified`: `Z2` tendría que mover glifos de un tramo `/ActualText` de ligadura («fi» en «firmó» y «verificado») | — | — |
| `M1` (T1, reutilizado) | rechazo `unsupported_structure` | `verify` · VR de PDFium, 262 px (**RT: todos explicados**) | S1 `Z1`; S2 `Z2` f = 0,657 (0,671 en Linux); S3–S5 `Z1` | verificada |
| `M5` (T5) | rechazo `unsupported_structure` | `verify` · VR de PDFium, 1460 px (**RT: todos explicados**) | como `L5` (`Z1` a 0,911 y 0,808; `Z2` a f = 1,000) | verificada; RZ con 3,39 pt de exceso |

En las seis exportaciones medidas no hubo rechazos del oráculo ni de la puerta.

**Evidencia en cada caso procesado:**

- **Eliminación real:** VK, VX y VP pasaron. Ningún valor seleccionado aparece en los operadores de texto (decodificados con la fuente actual y con todas las demás), en las cadenas y los nombres del grafo de objetos (también decodificados como URL), en los flujos, en los bytes del archivo ni en las propiedades de contenido marcado. La ocurrencia permitida (`O1`) quedó exactamente en su posición.
- **Fidelidad:** VT, VB, VN, VE y VS pasaron, y VR también salvo el diagnóstico RT:
  - texto fuera de las zonas idéntico (±0,01 pt);
  - operadores no textuales y render sin texto idénticos en toda la página;
  - anotaciones idénticas;
  - `/Info` descriptivo y XMP eliminados y registrados;
  - ningún objeto inalcanzable.
- **Geometría `Z2`** medida desde el original (protocolo 2B §3): residuo máximo de 0,0001 pt; el factor medido coincide con el del plan y el sustituto está comprimido por el mismo factor.
- **Restauración:** el restaurador recibió **solo** el derivado, el vínculo autenticado y la tabla token → valor. Las entradas de cada componente quedan en `results.json`, campo `inputs`; el verificador es el único que lee el original y el oráculo. Restituyó todos los valores Unicode exactos, incluidos el tramo en cursiva de `Joaquín`, `Zoë Ångström-Łęcka` y el formato mixto en negrita de T1. Las redacciones siguieron ausentes (VM).
- **Recursos y limpieza:** reglas usadas `RP3` (rejillas de tabla) y `RP7` (relleno blanco de Quartz). Clases eliminadas: `document_info_descriptive` (2–6 claves) y `xmp_metadata` (LibreOffice).
- **Coste:** de 1,5 a 2,2 s por caso en macOS y de 1,6 a 2,6 s en Linux; memoria pico del proceso de 286 MB en macOS y 245 MB en Linux.

## 3. Limitaciones que explican el resultado (sin rebajar el piso ni sustituir casos)

1. **Rasterizado estricto de PDFium** (afecta a `L5`, `M1` y `M5`: 3 de los 6 casos medidos y toda la familia macOS). Cuando el transformador acorta un operador de texto porque saca el valor, PDFium rasteriza los glifos **anteriores** de ese operador desplazados 1 px. Sus bytes no cambian y sus posiciones extraídas tampoco (0,0000 pt).
   - **Prueba reproducible** en `a08/pdfium_raster_probe.py`: el mismo prefijo «Árbol:» difiere en 439 px si el operador conserva «Árbol: A» o «Árbol: Ana», y en 0 px si conserva «Árbol: Ana Gil» o la línea entera. Poppler da 0 px en todos los casos.
   - Es una heurística de PDFium que depende del contenido del operador, **no** un cambio de geometría. Con 0 px estrictos en PDFium (C-2B-04), todo valor que comparta operador con texto anterior corto falla. `L1` y `C1` pasan porque sus prefijos no caen en ese umbral.
   - Hipótesis **no medida en el corpus**: conservar la extensión del operador rellenando lo eliminado con códigos en blanco evitaría el efecto. Afectaría a la extracción (espacios dentro de la zona) y exigiría un protocolo nuevo.
2. **`/ActualText` de ligaduras de Chromium dentro de la región que mueve `Z2`** (afecta a `C5`). Con C-2B-03 un tramo `/ActualText` no se puede mover sin que deje de coincidir con sus glifos, así que el plan `Z2` se rechaza, y `Z1` no basta para los valores cortos de T5.
   - `C1` pasa porque ninguna ligadura cae en la región movida.
   - Hipótesis no medida: mover el tramo marcado completo dentro del bloque, conservando su lista de propiedades.
3. **Exportaciones de Word y Google Docs pendientes** (4 casos, dos familias). Dependen del usuario (`corpus2b/EXPORT-W-G.md`). Sin ellas no puede haber éxito en esas familias.

## 4. Controles (no cuentan en la tasa)

- **CA01–CA07:** los 7 se rechazan con `uncovered_content` con las reglas activas (`RP3`, `RP5` y `RP7`).
- **CR (rechazos seguros):** los 28 dan el resultado esperado en ambos entornos: CR01–CR23 de la etapa 2, y CR27–CR31.
  - `/ActualText` distinto de sus glifos: `alternate_text_duplication`.
  - `/ActualText` que contiene la selección o que se solapa con ella parcialmente: `selection_not_transformable`.
  - Derivado que no es el exacto: `derivative_mismatch`.
  - Vínculo alterado: `binding_invalid`.
- **CS (positivos estructurales):** ninguno produjo bloqueos.

| Control | Resultado (macOS = Linux) |
|---|---|
| CS05 (`Tc`, `Tw`, `Tz`, `Ts`, `TL`), CS11 (`Tr 2`), CS14 (redacción y luego seudonimización) | **ciclo completo** en `Z2` |
| CS01, CS03, CS06, CS09, CS10, CS12 (dos seudonimizaciones), CS13 (seudonimización y luego redacción), CS17 (redacción corta con sitio, exceso RZ) | todo verificado salvo el render estricto de PDFium (diagnóstico RT, efecto del §3.1) |
| CS16 (redacción corta sin sitio) | la restauración falla cerrada, como estaba previsto (`restoration_layout_unavailable`) |
| CS15 (`/ActualText` en la región movida) | plan rechazado (`zone_plan_unjustified`) |
| CS04, CS07a/b, CS08 | sin soporte, igual que en la etapa 2 |

- **CV (controles del verificador):** **35/35 detectados** en ambos entornos. La comprobación que detecta CV18 cambia con el token (§1). La primera comprobación que falla en los nuevos:

| Control | Detectado por |
|---|---|
| CV31 (el plan amplía su zona) | **VZP** |
| CV32 (sufijo desplazado con el avance del plan falseado) | **Z2** |
| CV33 (valor en un `/ActualText` vacío) | VB; el escaneo de residuos del contenido marcado, por sí solo, también lo detecta |
| CV34 (`/ActualText` ajeno) | VT |
| CV35 (factor del plan falseado) | **Z2** |

## 5. Notas declaradas

- **N-2B-1 (oráculo de la etapa 2):** el margen de 0,3 em usaba el tamaño de `Tf`, no el em efectivo. Corregido en los oráculos 2B (E-A08-17). Los de la etapa 2 no se reescriben.
- **N-2B-2:** evidencia del §3.1, registrada en el protocolo antes de medir.
- **N-2B-3 (repetición de la medición en macOS):** la primera corrida, con el código de `33787d3` = `c89b202`, **no detectó CV32**. La tolerancia de la geometría `Z2` usaba el hueco hasta el siguiente glifo, que en una línea justificada es un espacio entre palabras (unos 2,5 pt), y absorbía un desplazamiento de 1 pt. Se corrigió en `3d332b4`: solo se tolera el espaciado entre los dos últimos glifos del valor (`Tc`). Después se repitió la medición completa. Los resultados por caso no cambiaron; CV pasó de 34/35 a 35/35. La corrida 1 se conserva en `outputs/stage2b-macos-run1/`. Linux se ejecutó solo con `3d332b4`.
- **Huellas de entrada:** `inputs.*.oracle_sha256` es el SHA-256 del JSON canónico del oráculo cargado. El hash del archivo congelado está en `FREEZE.json` y el ejecutor lo comprueba antes de medir.
- **Etapa 2:** sus resultados no cambian y se reproducen con el código de `2cd779a`. Esta etapa modificó el verificador y el planificador compartidos.

## 6. Recomendación

**La evidencia sigue siendo insuficiente.** El piso fijado exige 8/10, al menos un éxito por familia, Type0/CID y cero bloqueos. Se obtuvo 2/10, la familia macOS sin éxitos y dos familias sin exportar. Las condiciones de seguridad sí se cumplieron (sin bloqueos, negativos y CV completos, Type0/CID con texto seleccionado), y la ingeniería de los criterios funciona:

- zonas y geometría independientes del plan;
- RZ con exceso acotado;
- `/ActualText` acotado;
- vínculo autenticado y restauración solo desde el derivado.

Lo que falla es la cobertura, por las dos limitaciones del §3. No propongo una matriz de soporte para el producto. Pasos posibles, todos **decisión del usuario** y ninguno ejecutado:

- **Si se aportan las exportaciones de Word y Google Docs:** se congelan y se miden con el mismo código, sin cambiar el piso. La cota sigue siendo 6/10.
- **Una etapa nueva con protocolo propio** que pruebe las hipótesis del §3.1 (conservar la extensión del operador) y del §3.2 (mover tramos `/ActualText` completos), con controles nuevos y una medición nueva, sin reutilizar estos resultados como si fueran de esa etapa.
- **Revisar el criterio de render de PDFium (C-2B-04).** Hoy RT es diagnóstico por decisión del usuario; esta evidencia muestra que, en estos casos, la diferencia es un desplazamiento del raster ≤ 1 px sin cambio de geometría. Cambiarlo es una decisión de producto, no algo que este informe pueda hacer.

## 7. Reproducción

```bash
cd experiments/a08 && ./run2b_macos.sh
```

```bash
cd experiments/a08 && ./run2b_linux.sh
```

```bash
cd experiments/a08 && .venv/bin/python -m a08.pdfium_raster_probe
```

- **Exportaciones e ingreso:** `exporters/export_2b.sh` y `a08/intake2b.py` (protocolo 2B §9).
- **Fuentes:** `fonts/fetch_fonts.sh`, con los SHA-256 verificados.
- **Licencias:** iguales que en la etapa 2 ([informe §8](a08-feasibility-report.md#8-reproducción)).
