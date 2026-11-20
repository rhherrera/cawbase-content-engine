# Exportaciones de Word (`W1`–`W4`) y Google Docs (`G1`–`G4`) — instructivo

Fuentes (generadas del manifiesto por `a08/gen_docx.py`, deterministas; SHA-256 en `corpus/sources/SHA256SUMS`):

| Plantilla | Archivo | PDF de Word | PDF de Google Docs |
|---|---|---|---|
| T1 Carta | `experiments/a08/corpus/sources/T1.docx` | `corpus/W/W1.pdf` | `corpus/G/G1.pdf` |
| T2 Formulario | `experiments/a08/corpus/sources/T2.docx` | `corpus/W/W2.pdf` | `corpus/G/G2.pdf` |
| T3 Boletín | `experiments/a08/corpus/sources/T3.docx` | `corpus/W/W3.pdf` | `corpus/G/G3.pdf` |
| T4 Informe | `experiments/a08/corpus/sources/T4.docx` | `corpus/W/W4.pdf` | `corpus/G/G4.pdf` |

**Antes de empezar (Word):** instalar en el equipo las fuentes de `experiments/a08/fonts/` (Noto Serif y Noto Sans, OFL-1.1; se descargan y verifican con `fonts/fetch_fonts.sh`). T4 usa a propósito las fuentes del tema del documento (Cambria/Calibri): no cambiarlas.

## Word (familia `W`)

1. Abrir el `.docx` en **Word de escritorio** (Windows o Mac). No editar nada; si Word ofrece actualizar campos, aceptar (número de página).
2. Comprobar a ojo que haya **2 páginas** en T1, T2 y T3 y **3** en T4, con los saltos donde están en la fuente.
3. Exportar:
   - **Windows**: `Archivo › Exportar › Crear documento PDF/XPS`. En **Opciones**: «Crear marcadores usando: Títulos» activado; «Propiedades del documento» activado; «Etiquetas de estructura del documento para accesibilidad» activado; «Compatible con ISO 19005-1 (PDF/A)» **desactivado**. Optimizar para: **Estándar**.
   - **Mac**: `Archivo › Guardar como… › Formato: PDF` y elegir **«Mejor para distribución electrónica y accesibilidad (usa el servicio en línea de Microsoft)»**. **No** usar «Mejor para impresión» (sería la familia macOS).
4. Guardar con el nombre exacto de la tabla (`W1.pdf` … `W4.pdf`). No abrir y volver a guardar el PDF con otro programa.

## Google Docs (familia `G`)

1. En Google Drive: `Nuevo › Subir archivo` con el `.docx`; abrirlo con **Documentos de Google** (`Abrir con › Documentos de Google`, que crea una copia convertida).
2. Comparar con el manifiesto: páginas (2/2/2/3), tablas, dos columnas en T3, nota al pie en T4, negritas dentro de los nombres. Si la conversión alteró algo, corregirlo a mano **solo** en lo alterado y anotar qué se corrigió. Si una fuente Noto aparece sustituida, elegir «Noto Serif»/«Noto Sans» del menú de fuentes (Más fuentes) y anotarlo.
3. `Archivo › Descargar › Documento PDF (.pdf)`; guardar con el nombre exacto (`G1.pdf` … `G4.pdf`).

## Procedencia a registrar (un `provenance.json` por PDF, o un mensaje con estos datos)

```json
{
  "case_id": "W1",
  "family": "W",
  "flow": "Word para Windows › Exportar › Crear PDF/XPS (opciones del instructivo)",
  "application": "Microsoft Word para Microsoft 365", "application_version": "versión y compilación exactas (Archivo › Cuenta › Acerca de Word)",
  "os": "Windows 11 24H2 / macOS 27.0.1 …", "arch": "x64 / arm64",
  "options": ["Crear marcadores usando: Títulos", "Propiedades del documento", "Etiquetas de estructura", "sin PDF/A", "Estándar"],
  "fonts_installed": ["Noto Serif 2.015", "Noto Sans 2.015"],
  "source_sha256": "SHA-256 del .docx usado (debe coincidir con SHA256SUMS)",
  "exported_at_utc": "2026-11-…T…Z",
  "exported_by": "persona",
  "manual_corrections": [],
  "notes": ""
}
```

Para Google Docs: `application` = «Google Docs (web)», `application_version` = fecha de exportación, navegador y versión, idioma de la interfaz; `manual_corrections` con lo corregido tras la conversión.

Dejar los PDF en `experiments/a08/corpus/W/` y `experiments/a08/corpus/G/` (o enviarlos). El agente registrará el SHA-256, el `Producer` y `pdffonts`, construirá el oráculo y las zonas y los **congelará antes** de ejecutar el recorrido candidato sobre ellos. Mientras falten, cuentan como **faltantes** en el denominador de 20.
