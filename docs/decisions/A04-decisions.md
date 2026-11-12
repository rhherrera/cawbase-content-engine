# A04 — Registro de decisiones

> **Estado: A04 DOCUMENTADA y EXPERIMENTADA (experimentos acotados ejecutados en macOS y Linux local sobre documentos sintéticos); pendiente de aceptación del usuario.** Conclusiones de viabilidad en el [informe](../a04-feasibility-report.md): no son una aceptación ni habilitan A07/A08 por sí solas. Las decisiones `D-A04-nn` fueron **aprobadas por el usuario** y no cambian; las elecciones técnicas `E-A04-nn` no son decisiones de producto. Ningún soporte documental existe en `0.3.0`.

Contratos que usan estas decisiones: [especificación de A04](../features/A04-viabilidad-y-contrato-de-documentos.md) · [procesamiento de documentos](../document-processing.md).

Este registro separa categorías; no mezclarlas.

- **Decisiones aprobadas (`D-A04-nn`):** solo el usuario las cambia.
- **Elecciones técnicas derivadas (`E-A04-nn`):** completan el contrato; justificadas, revisables sin abrir otra ronda por detalles rutinarios.
- **Diferencias con el playbook (§4)** y **backlog (§5)**: fuera de los criterios de aceptación.

## 0. Historial de revisiones

| Rev. | Contenido |
|---|---|
| 1 | Primera redacción (solo documental): D-A04-01 a 03 aprobadas; E-A04-01 a 22; diferencias con el playbook; backlog. Registro del cierre y aceptación de A03 en los documentos de A03 (no es una decisión de A04). |
| 2 | Precisiones previas a los experimentos (sin cambiar `D-A04-nn`): alcance temporal, nuevas disposiciones (`P` estructural inspeccionado) y condiciones de cobertura, Unicode, unidad PDF = página, campos DOCX. E-A04-23 a 27. |
| 3 | Ejecución de los experimentos (rev. 3): E-A04-28 a 33 (derivadas de lo observado) y B-A04-11 a 19. Ninguna `D-A04-nn` cambia. |

## 1. Fuente, precedencia y herencia

- Referencia: `STAGE_3_PLAYBOOK.md`, revisión 6 (copia local ignorada por Git; **no se modifica ni se declara actualizada**). Las decisiones posteriores prevalecen donde difieren (§4).
- A01, A02 y A03 están **cerradas y aceptadas** por el usuario (A03: `0.3.0`; commit de implementación y regresiones `20d9211`; `e3ca6da` documentación y evidencia posterior; merge `ae19ef3`; resultado de GitHub Actions sobre A03 no verificado con la evidencia disponible). No se reabren ni se cambia ninguna decisión suya.
- Heredado **sin cambios**: A01 D-01 (independencia), D-06/D-07 (asincronía, núcleo único), D-08 (núcleo liviano, extras), D-11/D-33 (sin efectos al importar), D-17 (aislamiento); A03 `[start,end)` en puntos de código, `locate`/`full_coverage`/`MockFixture.for_request`/`build_inference_request`, límites de `Limits` y cobertura por capacidad e identificadores.

## 2. Decisiones aprobadas

### D-A04-01 — Dependencias compatibles con productos cerrados

- La vía inicial debe poder incorporarse en productos cerrados **sin licencia comercial obligatoria**. Se admite **MPL-2.0 cumpliendo sus obligaciones**; no se describe como licencia permisiva equivalente a MIT/BSD.
- **PyMuPDF** queda como **alternativa documentada** por su capacidad técnica y su esquema AGPL/comercial; **no** es la dependencia elegida ni la base del experimento principal.
- Si la vía evaluada no logra la transformación requerida, se **informa la imposibilidad o limitación**. No se introduce una dependencia comercial ni se reduce silenciosamente la garantía.
- La **licencia definitiva del motor** sigue pendiente para **A10**.

### D-A04-02 — Contenido auxiliar y rechazos iniciales

- Se **permite eliminar** comentarios y metadatos descriptivos/personales, **registrando las clases de información eliminadas** y sin prometer restaurarlas. No se confunden con información estructural necesaria para interpretar el archivo.
- Se **rechazan inicialmente** documentos con revisiones pendientes, firmas, cifrado, objetos incrustados o estructuras que no puedan cubrirse según el contrato. No se aceptan cambios, no se eliminan firmas y no se aplana contenido silenciosamente.
- Se **diferencian** los adjuntos y objetos incrustados (OLE, archivos adjuntos, paquetes u otros payloads) de los **recursos ordinarios** necesarios para representar el documento (fuentes, imágenes). Estos últimos requieren su propia regla de cobertura; su presencia no acredita que sean inocuos.

### D-A04-03 — Fidelidad mínima

- **DOCX:** conservar estructura y estilos de las partes soportadas; se admite redistribución de líneas y páginas por las sustituciones.
- **PDF:** conservar cantidad y geometría de páginas y presentación fuera de las zonas afectadas.
- Sustitutos ilegibles, desbordamientos, pérdida colateral de contenido o mapeo insuficiente producen **fallo explícito**.
- **No** convertir el documento completo en imágenes como alternativa silenciosa. **No** prometer identidad visual exacta ni recuperación byte a byte.
- La inspección visual identifica renderizador, versión y fuentes; **no** promete resultados idénticos entre Word, LibreOffice u otros lectores.

## 3. Elecciones técnicas derivadas

| ID | Elección | Justificación |
|---|---|---|
| E-A04-01 | Seis capas separadas: inventario, extracción, mapeo, cobertura de extracción, cobertura de inferencia, transformación y verificación | Cada afirmación (§ contrato 9) tiene un responsable y evidencia distintos; ninguna implica otra |
| E-A04-02 | Disposiciones por elemento (revisada por E-A04-23: S, E, P, N, R) | Satisface «ninguno ignorado como cubierto» sin exigir soportar todo |
| E-A04-03 | Estructuras conceptuales versionadas (`document-structures/1`), no clases ni endpoints | El encargo prohíbe código y APIs anticipadas |
| E-A04-04 | Identidad = SHA-256 del archivo exacto + identificador y versión del extractor + hash de su configuración; el mapa no es transferible | Evita aplicar un mapa a otro archivo o a uno modificado |
| E-A04-05 | Unidad de texto = punto de código (como A03); conversión explícita desde UTF-16/glifos/bytes en cada biblioteca | PyMuPDF/PDFium/XML usan unidades distintas; los errores de unidad son silenciosos |
| E-A04-06 | El texto de una unidad se entrega a A03 sin normalizar; separadores sintetizados se declaran como tales y no son localizables | Alineado con A03 §2.3 |
| E-A04-07 | El mapa documental es una estructura **separada** de `AnalysisResult`; un hallazgo A03 `(type_id,start,end)` se resuelve contra `unit_id` | No se modifica el contrato aceptado de A03 |
| E-A04-08 | Unidades de extracción acotadas (≤ límites de A03) solo para los experimentos; sin agregación de puntuaciones; alcance local declarado | Evita presentar un análisis por fragmentos como clasificación global; la segmentación productiva queda fuera |
| E-A04-09 | Orden de extracción determinista y documentado, sin prometer orden semántico de lectura | El PDF no tiene capa semántica fiable |
| E-A04-10 | Ambigüedad de localización ⇒ fallo para transformar; sin búsqueda aproximada ni «primera coincidencia» | Coherente con A03 E-A03-13 |
| E-A04-11 | Biblioteca principal DOCX candidata: `python-docx` (MIT) + inspección OOXML por ZIP/XML; `lxml` solo si el experimento la justifica | `python-docx` no cubre por sí solo revisiones ni tablas anidadas |
| E-A04-12 | Vía PDF principal candidata: `pikepdf` (MPL-2.0)/QPDF para inspección y modificación estructural; `pypdfium2`/PDFium para extracción, geometría y render; `pypdf` como verificador adicional | Compatibles con D-A04-01; ninguna elegida hasta los experimentos |
| E-A04-13 | Versiones y licencias transitivas se fijan al ejecutar los experimentos; la documentación `latest`/`stable` no equivale a versión fijada | El encargo lo exige; ver contrato §15 |
| E-A04-14 | Rechazos conservadores para estructuras sin regla de cobertura (revisiones, texto oculto, cuadros de texto, formularios, capas, etc.) | Reducir superficie en la primera viabilidad; ampliables vía backlog |
| E-A04-15 | Reparación del parser = rechazo (no éxito normal) | «Sin reparación silenciosa» |
| E-A04-16 | Para PDF, las actualizaciones incrementales con revisiones previas se rechazan | Las revisiones anteriores conservarían el contenido «eliminado» |
| E-A04-17 | Verificador independiente: debe usar parser distinto del transformador cuando sea viable; si no, se declara la limitación | Un verificador con el mismo motor hereda sus errores |
| E-A04-18 | Inspección visual de **todas** las páginas de muestras pequeñas; sin porcentaje global de similitud como única evidencia | Requisito de aceptación |
| E-A04-19 | Ubicación de los artefactos experimentales (implementada; ver E-A04-31): `experiments/a04/` fuera de `src/`; no entra en la wheel (que solo empaqueta `src/…`) y no figura en el `include` del sdist. Propuesto / no disponible; no se cambia el empaquetado ahora | Separar experimentos del runtime y los distribuibles |
| E-A04-20 | Límites de experimento conservadores (contrato §16), no límites productivos de A05 | Protegen la máquina y acotan el análisis |
| E-A04-21 | Procesamiento local, sin red, sin macros/scripts/referencias externas | Seguridad de los experimentos |
| E-A04-22 | Restitución mecánica = prueba de viabilidad con correspondencias sintéticas locales; no es contrato de A06 | Fuera de alcance: criptografía, custodia, autorización |
| E-A04-23 | Cinco disposiciones: **S** cubierto, **E** eliminación autorizada, **P** estructural preservado e inspeccionado, **N** no cubierto, **R** rechazo (sustituye a las cuatro de E-A04-02) | Los recursos estructurales no deben impedir la cobertura solo por existir, pero tampoco quedar exentos de inspección |
| E-A04-24 | Dos indicadores: `textual_subset_complete` y `document_coverage_complete`, independientes de la ausencia de residuos y de la detección real | Condición inequívoca de cobertura sin prometer detección |
| E-A04-25 | Unidad PDF = página; separadores sintetizados por operadores de posicionamiento; intervalos con concatenación artificial no se transforman; sin segmentación semántica | Resuelve la contradicción entre unidades, orden y columnas sin nueva segmentación |
| E-A04-26 | Distinguir punto de código, unidad UTF-16, grafema y glifo; relaciones con dirección explícita | Un punto fuera del BMP son 2 unidades UTF-16 y no equivale a 1 glifo |
| E-A04-27 | Campos DOCX: se extrae el resultado; una selección dentro de él se rechaza (sin evaluador de campos) | Reemplazar el resultado no protege un valor que la instrucción pueda regenerar |
| E-A04-28 | PDF: la transformación preserva la posición del texto posterior compensando el avance con un ajuste de `TJ`; el sustituto no puede ser más ancho que lo reemplazado (`replacement_overflow`) | Cumple «conservar presentación fuera de la zona afectada» sin reflujo ni medición de líneas libres; un sustituto más ancho exigiría decidir espacio disponible (B-A04-16) |
| E-A04-29 | PDF: los caracteres del sustituto deben existir en la codificación **y** en el programa de fuente incrustado (cmap de `FontFile2`) | Un glifo ausente del subconjunto se dibujaría como `.notdef`: sustituto ilegible (D-A04-03) |
| E-A04-30 | Verificación PDF con tres motores independientes del transformador (PDFium, pypdf estricto, Poppler) contra un oráculo del manifiesto; DOCX con XML de la biblioteca estándar; los verificadores se prueban con un control negativo (archivo sin modificar debe fallar) | Evita que una búsqueda de bytes o un solo motor acredite la eliminación |
| E-A04-31 | `exclude = ["experiments"]` en la sección sdist del `pyproject.toml` del motor | Los patrones sin ruta `pyproject.toml`/`uv.lock` del `include` incluían los de `experiments/a04`; es configuración de empaquetado, no una dependencia, versión ni lockfile |
| E-A04-32 | DOCX: el sustituto adopta el formato del primer run del valor; sin reconstrucción de runs | Mínima intervención sobre estructura y estilos fuera del valor |
| E-A04-33 | Fixtures byte-deterministas (excepto `Q01_encrypted`, con sal aleatoria) con manifiesto independiente y hash verificado al cargar; `P11` generado en la imagen Linux con DejaVu y commiteado | Reproducibilidad sin dependencia de fuentes del sistema en macOS |

## 4. Diferencias respecto del playbook (revisión 6)

| # | Playbook | A04 | Motivo |
|---|---|---|---|
| 1 | «Librerías documentales, licencias y límites de formato» sin criterio de licencia | D-A04-01: vía inicial sin licencia comercial obligatoria; MPL-2.0 admitida con obligaciones; PyMuPDF solo alternativa | Decisión del usuario |
| 2 | «Metadatos y comentarios» sin regla de tratamiento | D-A04-02: eliminación permitida y registrada; rechazos iniciales enumerados | Decisión del usuario |
| 3 | Fidelidad sin umbral | D-A04-03: mínimos por formato y fallo explícito | Decisión del usuario |
| 4 | «Imposibilidad material exige revisar el alcance con el usuario» | Se conserva; además un resultado negativo no habilita A07/A08 ni cierra A04 sin decisión del usuario | Coherente con el playbook |

Lo no listado permanece vigente.

## 5. Backlog (fuera de los criterios de aceptación)

| ID | Mejora |
|---|---|
| B-A04-01 | Aceptar revisiones (w:ins/w:del) con política explícita |
| B-A04-02 | Texto oculto, cuadros de texto y controles de contenido enlazados a datos |
| B-A04-03 | OCR y cobertura de imágenes/trazados con texto |
| B-A04-04 | Formularios, anotaciones, capas y adjuntos en PDF |
| B-A04-05 | Firmas: reglas de invalidación explícita y consentida |
| B-A04-06 | Formatos adicionales (XLSX, PPTX, ODF, RTF) |
| B-A04-07 | Fuentes sin ToUnicode, texto convertido a trazados, texto vertical/CJK complejo |
| B-A04-08 | Segmentación productiva y agregación entre unidades (con A09) |
| B-A04-09 | Evaluación de PyMuPDF si el usuario revisa D-A04-01 |
| B-A04-10 | Comparación visual automatizada más rica (con tolerancias validadas) |
| B-A04-11 | Fuentes Type0/CID, subconjuntos CFF y Type3 en la transformación PDF |
| B-A04-12 | Corpus de documentos reales (Word, Acrobat, otros generadores) y renderizado con Word; varias secciones y documentos grandes |
| B-A04-13 | Entradas PDF con object streams, `/Contents` en arreglo, linearizadas, PDF/A; rotaciones oblicuas y 270° |
| B-A04-14 | Flujo de GitHub Actions para los experimentos (no se ejecutó ninguno) |
| B-A04-15 | Protección de valores dentro de resultados de campos DOCX (requiere decidir semántica de campos) |
| B-A04-16 | Sustitutos más anchos en PDF mediante reflujo/medición de espacio libre |
| B-A04-17 | Aplicación en código de los límites de duración y memoria de los experimentos |
| B-A04-18 | Restitución PDF que conserve el kerning interno de los valores |
| B-A04-19 | Evaluar las plataformas de las ruedas (musl, x86_64) para licencias de binarios (FriBiDi/libimagequant en Pillow) |

## 6. Preguntas abiertas y tensiones

Ninguna bloquea la documentación. Dependencias en otras features: trabajos y almacenamiento (A05), sustituciones y restauración productivas (A06), protección/restauración DOCX/PDF completas (A07/A08), modelo real y segmentación (A09), licencia definitiva y publicación (A10). Si los experimentos concluyen «no viable» o «evidencia insuficiente» para un área, **decide el usuario** (no se cierra A04 con una imposibilidad material por inercia).
