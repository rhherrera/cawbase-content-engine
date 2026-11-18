# A08 — Protección y restauración PDF

> **Estado: ETAPA 1 (solo documental), revisión 1 — pendiente de la aprobación del usuario del contrato experimental y del protocolo fijado.** No hay código experimental ni productivo, corpus exportado, medición, migración, dependencia ni versión nuevos. Todo lo que este documento describe está **especificado**; nada está implementado, probado ni aceptado.

- Fuente: `STAGE_3_PLAYBOOK.md` revisión 6 (§6 A08; copia local ignorada por Git), más las decisiones posteriores ([A01](../decisions/A01-decisions.md) a [A07](../decisions/A07-decisions.md) y [A08](../decisions/A08-decisions.md)), que prevalecen. D-A04-04 ([A04-decisions](../decisions/A04-decisions.md)) exige la comprobación representativa antes del pipeline productivo.
- Repositorio: `cawbase-content-engine` · rama `feature/a08-pdf-protection-restoration`, creada desde `main` en `c6590ec` (merge del PR #6 de A07; `main` = `origin/main` según las referencias locales; árbol limpio).
- Dependencias: A01–A05 cerradas y aceptadas; A04 aceptada como investigación acotada; **A06 y A07 implementadas y mergeadas, aceptación pendiente** (ver [A08-decisions §1](../decisions/A08-decisions.md#1-fuente-base-y-herencia)). No requiere modelo real (mock de A03).
- Documentos de A08: [contrato PDF propuesto](../pdf-protection.md) · [protocolo y corpus de D-A04-04](../a08-pdf-corpus-protocol.md) · [manifiesto del corpus](../../experiments/a08/corpus/manifest.json) · [decisiones](../decisions/A08-decisions.md).
- Previsto para la etapa 3 (propuesto / no disponible): versión `0.7.0`, migración `0006_pdf_protection`, `schema_level` 6, permisos 25 → 29.

## 1. Objetivo, alcance y exclusiones

**Objetivo.** Entregar un PDF protegido que conserve una presentación utilizable —con eliminación real del contenido seleccionado, sustitutos de A06 legibles dentro de un área segura y limpieza auxiliar registrada— y restaurar las seudonimizaciones sobre el derivado exacto, dentro de una matriz PDF acreditada por evidencia.

**Incluye:** etapa 1 (este documento, el contrato propuesto y el protocolo congelado); etapa 2 (comprobación D-A04-04 en `experiments/a08/`); etapa 3 (implementación productiva del alcance que el usuario apruebe tras la etapa 2).

**Excluye:** inferencia real, Qwen, AWS, encendido/apagado y evaluación de detección (A09); OCR y aplanado; formularios, capas, adjuntos y anotaciones con apariencia; escaneados y mixtos con regiones fuera de cobertura; escritura vertical y de derecha a izquierda; HTTP y rutas nuevas; integración con CAWbase (bloque B); restaurar un PDF editado; recuperar lo redactado o eliminado; identidad visual o byte a byte con el original.

## 2. Decisiones

Aprobadas por el usuario: **D-A08-01** (utilidad y comprobación previa), **D-A08-02** (sustitutos, fuentes y área segura), **D-A08-03** (cobertura, recursos y matriz), **D-A08-04** (análisis fuente y compatibilidad), **D-A08-05** (restauración del derivado exacto), **D-A08-06** (operaciones, permisos y lectura): texto en [A08-decisions §2](../decisions/A08-decisions.md#2-decisiones-aprobadas-por-el-usuario). Elecciones `E-A08-01` a `15`; propuestas **pendientes** `P-A08-01` a `07`; cuestiones materiales `Q-A08-01` a `11` en [§7](../decisions/A08-decisions.md#7-preguntas-abiertas-tensiones-y-cuestiones-materiales).

## 3. Etapas y compuertas

| Etapa | Entrega | Compuerta para avanzar |
|---|---|---|
| 1 | Alcance, decisiones, contrato PDF propuesto, protocolo y corpus congelados, matriz de aceptación (este conjunto) | **Aprobación explícita** del usuario del contrato experimental y del protocolo (incluidas, o no, las propuestas `P-A08-nn`) y respuesta a Q-A08-01 y Q-A08-02 |
| 2 | Prototipo aislado en `experiments/a08/` (fuera de la wheel y del sdist); corpus exportado con procedencia; oráculo y zonas congelados **antes** de medir; línea base con el extractor vigente; recorrido candidato completo; controles; informe `docs/a08-feasibility-report.md` (propuesto) con propuesta de matriz y contrato productivos limitados a la evidencia | Piso de [protocolo §11](../a08-pdf-corpus-protocol.md#11-criterio-de-avance-fijado-antes-de-medir) sin bloqueos **y** aprobación explícita del usuario del alcance productivo resultante. Si el piso no se cumple: opciones concretas para el usuario, sin ajustar denominador, máscaras ni reglas |
| 3 | Implementación productiva del alcance aprobado, documentación permanente, informe `docs/a08-implementation-report.md` (propuesto) | Aceptación final del usuario |

Reglas para las etapas 2 y 3: las de [A07 §3](A07-proteccion-y-restauracion-docx.md#3-reglas-de-implementación-fase-2-tras-la-aprobación) (reutilizar A05/A06/A07 sin duplicar; importación perezosa; proceso aislado; mock solo para la inferencia; PostgreSQL y SQLite reales con bases `cce_t_*` descartables; carreras con sincronización controlada; evidencia separada por nivel; sin datos reales ni secretos) más: el código experimental no se importa desde `src/`; las dependencias de la etapa 2 viven en un `pyproject.toml`/`uv.lock` propios; los cambios de criterio posteriores a la medición siguen la regla de congelamiento del protocolo.

## 4. Muestras, lectores y ambientes previstos

| Elemento | Previsto |
|---|---|
| Corpus nativo | 20 casos, 4 por familia (`W`, `G`, `L`, `C`, `M`), plantillas T1–T4 comunes ([protocolo §2–§5](../a08-pdf-corpus-protocol.md#2-familias-y-flujos-de-exportación)) |
| Controles | 11 estructurales positivos (`CS`), 26 de rechazo (`CR`), 23 del verificador (`CV`) ([protocolo §8](../a08-pdf-corpus-protocol.md#8-controles-no-cuentan-en-la-tasa-de-utilidad)); no cuentan en la tasa |
| Lectores y verificadores | PDFium (`pypdfium2`), Poppler (`pdftotext`, `pdftoppm`, `pdfinfo`, `pdffonts`), `pypdf` estricto (recorrido estructural), `qpdf --check` (apertura), barrido de bytes propio |
| Ambientes | macOS (exportadores `C` y `M`; verificación) y Linux local en Docker (exportador `L`, `qpdf`, verificación). Word y Google Docs dependen del usuario (Q-A08-02). GitHub Actions no se ejecuta salvo que lo haga el usuario; un *skip* no es aprobación |

## 5. Matriz de aceptación AC-A08

Cada fila: **garantía** → **caso** → **evidencia esperada**. El resultado real irá en el informe de la etapa correspondiente, no aquí. «PG+SQ» = PostgreSQL y SQLite reales.

### 5.1 Etapa 1 (documental)

| AC | Garantía | Evidencia esperada |
|---|---|---|
| AC-A08-01 | Corpus de 20 casos nativos (4 por familia) con flujo, opciones, fuentes, procedencia y hash a registrar, contenido sintético y representatividad justificada | protocolo §2–§5 y manifiesto |
| AC-A08-02 | Oráculo independiente del extractor del motor definido antes de exportar, con textos, contextos y posiciones esperados | protocolo §6, manifiesto |
| AC-A08-03 | Controles estructurales, de rechazo y del verificador con resultado esperado prefijado y separados de la tasa | protocolo §8 |
| AC-A08-04 | Criterio de avance (16/20, 3/4 por familia, ≥ 1 Type0/CID nativo, bloqueos) fijado antes de medir | protocolo §11 |
| AC-A08-05 | Zonas seguras, máscaras, geometría, legibilidad, desbordamiento, apertura sin reparación, diferencias fuera de zona y equivalencia de texto definidas de antemano; responsabilidades de PDFium, Poppler y el recorrido estructural separadas | protocolo §7, §9 |
| AC-A08-06 | Contrato PDF propuesto con identidad de ocurrencias, fuentes y mapeos, estado gráfico y de texto, Form XObjects, límites, metadatos, anotaciones, cobertura, motivos cerrados, compatibilidad y reutilización de A05–A07; capacidades condicionadas marcadas | contrato §3–§16 |
| AC-A08-07 | Disponibilidad de exportadores registrada y casos faltantes conservados en el denominador | protocolo §5, §15 |
| AC-A08-08 | Documentación alineada: enlaces y anclas sin romper | `python scripts/check_doc_links.py` |

### 5.2 Etapa 2 (comprobación D-A04-04)

| AC | Garantía | Caso | Evidencia esperada |
|---|---|---|---|
| AC-A08-10 | Corpus exportado por los flujos fijados, con procedencia completa y hashes congelados **antes** de medir; faltantes registrados | 20 casos | `provenance.json` por caso; commit de congelamiento anterior al de medición |
| AC-A08-11 | Oráculo geométrico y zonas congelados antes de ejecutar el recorrido candidato; desacuerdos registrados como no exitosos | 20 casos | hashes en el commit de congelamiento; registro `oracle` |
| AC-A08-12 | Línea base del extractor vigente: admisión, rechazo con motivo, extracción y localización por caso (H-A08-02) | 20 casos + controles | tabla por etapa |
| AC-A08-13 | Recorrido candidato registrado por documento y variante en cada etapa (`open` … `verify_restore`) con motivos cerrados; comparación fuente original/candidata donde aplique | 20 casos + `CS` | tabla por etapa y variante |
| AC-A08-14 | Reapertura sin reparación (PDFium, Poppler, `pypdf` estricto, `qpdf --check`) de original, protegido y restaurado | todos los procesados | salidas registradas |
| AC-A08-15 | Extracción independiente concordante con el oráculo; texto y cajas fuera de zonas iguales; contenido exacto de cada zona y gramática del marcador | todos los procesados | VT, VZ, VO |
| AC-A08-16 | Objetos y flujos decodificados, restos omitidos por la extracción y residuos en todas las representaciones; ocurrencia idéntica permitida solo en su posición | todos los procesados | VS, VX, VK, VP |
| AC-A08-17 | Render comparado con PDFium y Poppler: 0 píxeles distintos fuera de la máscara; hojas de contacto de todas las zonas | todos los procesados | VR; imágenes revisables |
| AC-A08-18 | Restauración mecánica de todas las seudonimizaciones con valor Unicode exacto, geometría y presentación fuera de zonas; redacciones y clases `E` definitivas | todos los protegidos | VM |
| AC-A08-19 | Todos los `CR` rechazados con el motivo esperado y todos los `CV` detectados por el verificador | controles | ninguno aceptado indebidamente |
| AC-A08-20 | Piso de utilidad evaluado sin ajustar denominador, máscaras ni reglas; bloqueos evaluados aparte | resultados | tabla de conteo por familia; lista de bloqueos |
| AC-A08-21 | Comandos, versiones, fuentes, licencias, hashes, tiempos y memoria registrados en macOS y Linux | ambos ambientes | `environment.json` |
| AC-A08-22 | Propuesta de matriz y contrato productivos limitados a la evidencia; si el piso no se cumple, opciones con cobertura, coste y efecto sobre garantías | informe | `docs/a08-feasibility-report.md` |
| AC-A08-23 | Experimentos fuera de la wheel y del sdist | `uv build` | listado de entradas |

### 5.3 Etapa 3 (implementación; preliminar, se ajusta al alcance que se apruebe tras la etapa 2)

| AC | Garantía | Caso | Evidencia esperada |
|---|---|---|---|
| AC-A08-30 | Protección E2E desde un `document_analysis` PDF con mock: protegido publicado, vínculo `active`, informe sin valores | caso del corpus aprobado | E2E, PG+SQ |
| AC-A08-31 | Fuente: S1–S12 de A07 con `format = pdf`, extractor v2, mapa reproducido byte a byte, detección concluyente, perfil y procedencia del mock; sin hallazgos ni mapas del llamador | un caso por condición | errores del contrato, PG+SQ |
| AC-A08-32 | Compatibilidad: análisis y derivados DOCX anteriores siguen protegiéndose y restaurándose; análisis PDF v1 ⇒ `analysis_source_stale` y un análisis nuevo del mismo archivo es válido; trabajos en cola de runtimes anteriores terminan con su configuración (según P-A08-04); runtimes anteriores no reclaman trabajos nuevos | bases pobladas por `0.6.0` | PG+SQ |
| AC-A08-33 | Matriz PDF aprobada: cada fila transformable, conservada, eliminada o rechazada con su código | fixtures por fila | códigos y verificador |
| AC-A08-34 | Selección ambigua, que cruza líneas o con cambios colaterales ⇒ fallo de todo el documento sin publicar | fixtures | cero artefactos y vínculos |
| AC-A08-35 | Objetos compartidos: la reescritura solo afecta los usos autorizados (o falla cerrado) | fixtures `CS07`, `CR20` | verificador |
| AC-A08-36 | Verificación previa a publicar con oráculo por posición, recorrido estructural independiente del transformador y residuos; controles negativos fallan | corpus y `CV` | `residue_detected` / `derivative_verification_failed` |
| AC-A08-37 | Restauración del derivado exacto: copia del host, digest antes de analizar, marcadores en posiciones autorizadas antes de descifrar, una pasada, funciona tras purgar original, análisis y datos del trabajo de protección | casos de A07 trasladados | PG+SQ |
| AC-A08-38 | Derivado alterado, vínculo o mapa alterado, contexto ajeno o vencido (también mientras espera un bloqueo), lease perdido, reintento con reutilización de marcadores, fallos de storage, publicación sin duplicados | carreras con sincronización controlada que **demuestran** la espera o la pérdida de posesión | PG+SQ |
| AC-A08-39 | Permisos (29), lectores genéricos sin acceso, scope ajeno, `docx:*` no habilitan PDF ni al revés, `pdf:protect` no descarga el original | matriz | PG+SQ |
| AC-A08-40 | Lectura del restaurado con instante de autorización tras el spool; purga inmediata solo de `pdf_restored` descartado; `delete_job_data` conserva el vínculo; `delete_context` termina la restaurabilidad | casos de A07 trasladados | PG+SQ |
| AC-A08-41 | `restore_text` con marcadores emitidos en PDF solo con todas las condiciones de A06 | casos | códigos de A06 |
| AC-A08-42 | Límites, idempotencia, orden de bloqueos y ausencia de contenido sensible en logs y errores | casos | PG+SQ |
| AC-A08-43 | Regresiones A05–A07, corpus aprobado, controles, checks del repositorio y distribución (D-33 intacta) | suites completas | macOS y Linux Docker |
| AC-A08-44 | Documentación permanente, contrato, informe y matriz alineados con el código; por criterio, qué se verificó en macOS, Linux local, SQLite y PostgreSQL | informe | trazabilidad AC → prueba → ambiente |

## 6. Riesgos y límites declarados

- **Piso en riesgo por construcción** sin `Z2` (Q-A08-01) y sin las exportaciones de Word y Google Docs (Q-A08-02).
- El corpus nativo no incluye imágenes rasterizadas: documentos reales con logotipos o fotografías se rechazarán salvo P-A08-03.
- El render en tiempo de ejecución no está previsto (evidencia de aceptación); la independencia del verificador en producción requiere una decisión de dependencias (Q-A08-08).
- El mock no mide detección: la ausencia de los valores sintéticos no acredita ausencia de datos sensibles.
