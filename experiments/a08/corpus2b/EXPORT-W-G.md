# Etapa 2B — exportaciones de Word (`W1`, `W5`) y Google Docs (`G1`, `G5`)

Cuatro PDF, dos por familia. Protocolo: [`docs/a08-stage2b-protocol.md`](../../../docs/a08-stage2b-protocol.md). Las entradas de A08 son los **PDF**; los `.docx` son solo insumos de exportación.

| Caso | Plantilla | Archivo fuente | SHA-256 del fuente | ¿Reutilizable de la etapa 2? | Guardar como |
|---|---|---|---|---|---|
| `W1` | T1 Carta (2 págs.) | `experiments/a08/corpus/sources/T1.docx` | `ca41d130d013a0c4353207933f67db0bbf843e2a66f0aa6a71a6a84a8a747036` | **Sí**: si ya exportaste `W1.pdf` con el instructivo de la etapa 2 (`corpus/EXPORT-W-G.md`), sirve tal cual; si no, exportarlo ahora | `experiments/a08/corpus2b/W/W1.pdf` |
| `W5` | T5 Ficha (1 pág.) | `experiments/a08/corpus2b/sources/T5.docx` | `b59873f92241785c8a7b0c87dd4b27c1f6a777f83c2a8b641545e4f260f0df93` | **No**: exportación nueva | `experiments/a08/corpus2b/W/W5.pdf` |
| `G1` | T1 Carta (2 págs.) | `experiments/a08/corpus/sources/T1.docx` | `ca41d130d013a0c4353207933f67db0bbf843e2a66f0aa6a71a6a84a8a747036` | **Sí**, igual que `W1` | `experiments/a08/corpus2b/G/G1.pdf` |
| `G5` | T5 Ficha (1 pág.) | `experiments/a08/corpus2b/sources/T5.docx` | `b59873f92241785c8a7b0c87dd4b27c1f6a777f83c2a8b641545e4f260f0df93` | **No**: exportación nueva | `experiments/a08/corpus2b/G/G5.pdf` |

`W2`–`W4` y `G2`–`G4` de la etapa 2 **no** se usan en la 2B. Ninguna exportación de Word ni de Google Docs llegó durante la etapa 2, así que hoy las cuatro están pendientes.

**Antes de empezar (Word):** instalar en el equipo las fuentes de `experiments/a08/fonts/` (`fonts/fetch_fonts.sh` las descarga y verifica): Noto Serif (T1) y **Noto Sans Regular, Bold e Italic** (T5). No cambiar ninguna fuente del documento.

## Qué comprobar antes de exportar (sin editar)

- **T1**: 2 páginas, con el salto donde está en la fuente; «María José» en negrita dentro del saludo.
- **T5**: 1 página. Cada línea de «Responsable:» a «Avisos a…» es un párrafo propio y **no** debe partirse en dos renglones; «Joaquín» va en cursiva. Si alguna de esas líneas se parte, **no la corrijas**: exportá igual y anotalo en `notes`.

## Word (familia `W`)

1. Abrir el `.docx` en **Word de escritorio** (Windows o Mac). No editar nada; si Word ofrece actualizar campos, aceptar.
2. Exportar:
   - **Windows**: `Archivo › Exportar › Crear documento PDF/XPS`. En **Opciones**: «Crear marcadores usando: Títulos» activado; «Propiedades del documento» activado; «Etiquetas de estructura del documento para accesibilidad» activado; «Compatible con ISO 19005-1 (PDF/A)» **desactivado**. Optimizar para: **Estándar**.
   - **Mac**: `Archivo › Guardar como… › Formato: PDF`, opción **«Mejor para distribución electrónica y accesibilidad (usa el servicio en línea de Microsoft)»**. **No** usar «Mejor para impresión», que pertenece a la familia macOS.
3. Guardar con el nombre exacto de la tabla. No abrir y volver a guardar el PDF con otro programa.

## Google Docs (familia `G`)

1. En Google Drive: `Nuevo › Subir archivo` con el `.docx`; abrirlo con **Documentos de Google** (`Abrir con › Documentos de Google`).
2. Comparar con lo indicado arriba (páginas, negrita o cursiva dentro del nombre, líneas de T5 sin partir). Si la conversión alteró algo, corregir **solo** lo alterado y anotarlo en `manual_corrections`. Si una fuente Noto aparece sustituida, elegir «Noto Serif» o «Noto Sans» en `Más fuentes` y anotarlo.
3. `Archivo › Descargar › Documento PDF (.pdf)`; guardar con el nombre exacto.

## Procedencia (un `provenance-input.json` junto a cada PDF, o un mensaje con estos datos)

```json
{
  "case_id": "W5",
  "family": "W",
  "flow": "Word para Windows › Exportar › Crear PDF/XPS (opciones del instructivo)",
  "application": "Microsoft Word para Microsoft 365",
  "application_version": "versión y compilación exactas (Archivo › Cuenta › Acerca de Word)",
  "os": "Windows 11 … / macOS …",
  "arch": "x64 / arm64",
  "options": ["Crear marcadores usando: Títulos", "Propiedades del documento", "Etiquetas de estructura", "sin PDF/A", "Estándar"],
  "fonts_installed": ["Noto Serif 2.015", "Noto Sans 2.015"],
  "source_sha256": "SHA-256 del .docx usado (columna de la tabla)",
  "exported_at_utc": "2026-11-…T…Z",
  "exported_by": "persona",
  "reused_from_stage2": false,
  "manual_corrections": [],
  "notes": ""
}
```

Para Google Docs: `application` = «Google Docs (web)»; `application_version` = fecha de exportación, navegador y versión, idioma de la interfaz.

## Qué hace el agente al recibirlos

Por cada PDF, y **antes** de ejecutar el candidato sobre él: registra el SHA-256, el `Producer`, `pdffonts` y `pdfinfo`, construye el oráculo y las zonas (banda tipográfica, criterio 1) y los congela en un commit (`corpus2b/FREEZE.json`). Después lo mide con el mismo código de la medición informada. Mientras falten, cuentan como **faltantes** en el denominador de 10, y una familia sin éxitos impide declarar cumplido el piso.
