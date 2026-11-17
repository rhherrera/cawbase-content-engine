# A07 — Protección y restauración DOCX

> **Estado: ESPECIFICADA (revisión 1), PENDIENTE DE LA APROBACIÓN EXPLÍCITA DEL USUARIO. No implementada.** Esta entrega es **solo documental**: no hay código, pruebas, dependencias, esquema, permisos ni versión nuevos. Las cinco decisiones de producto (`D-A07-01` a `05`) están **aprobadas por el usuario** y habilitan esta documentación; la implementación, las pruebas de A07 y sus commits empiezan **después** de la aprobación explícita del contrato escrito. Las propuestas `P-A07-01` a `07` están **pendientes de revisión** ([A07-decisions §4](../decisions/A07-decisions.md#4-propuestas-pendientes-de-revisión-p-a07-nn)).

- Fuente: `STAGE_3_PLAYBOOK.md` revisión 6 (§6 A07, §§4, 8–10 y 12; copia local ignorada por Git), más las decisiones posteriores ([A01](../decisions/A01-decisions.md) a [A06](../decisions/A06-decisions.md) y [A07](../decisions/A07-decisions.md)), que prevalecen.
- Repositorio: `cawbase-content-engine` · rama: `feature/a07-docx-protection-restoration`, creada desde `main` en `5bcf4b6` (merge del PR #5 de A06, que contiene `e6a2a8b`; `main` = `origin/main` verificado con `git fetch`).
- Dependencias: A01–A05 cerradas y aceptadas (A05 con 22 criterios parciales); A04 aceptada como investigación de viabilidad acotada (D-A04-04 pertenece a A08); **A06 implementada en `0.5.0` y mergeada en `main` (merge `5bcf4b6`, PR #5); su aceptación no está registrada** (ver [A07-decisions §7](../decisions/A07-decisions.md#7-preguntas-abiertas-tensiones-e-incompatibilidades)). No requiere modelo real (usa el mock de A03).
- Contrato permanente: [protección y restauración DOCX](../docx-protection.md). Este documento define alcance y aceptación y **enlaza**; no copia el contrato.
- Versión prevista: `0.6.0` · migración prevista: `0005_docx_protection` · `schema_level` 5 · `min_runtime_level` 1 · sin extra nuevo (exige `documents` y `protection`) · permisos previstos: 21 → 25 (P-A07-05). Todo **propuesto / no disponible**.

## 1. Objetivo, alcance y exclusiones

**Objetivo.** Entregar un DOCX protegido del mismo formato a partir de un análisis concluyente de A05 y permitir la restauración autorizada de las seudonimizaciones sobre el derivado exacto, con limpieza del contenido auxiliar, conservación de estilos y estructura, verificación de residuos y cobertura explícita.

**Incluye:** fuente de hallazgos y vinculación al texto exacto ([contrato §3](../docx-protection.md#3-fuente-de-hallazgos-y-vinculación-d1)); matriz de transformación cruzada con A04 y A05 y reglas de selección y de campos ([§4](../docx-protection.md#4-matriz-de-transformación-docx-d4-d5)); limpieza obligatoria de `E` ([§5](../docx-protection.md#5-limpieza-obligatoria-de-clases-e-y-conservación-d5)); verificación con oráculo por posición, esqueleto y residuos ([§6](../docx-protection.md#6-verificación-antes-de-publicar-d5-e-08)); restauración del derivado exacto ([§7](../docx-protection.md#7-restauración-del-derivado-exacto-d3-d4)); API Python, tipos y errores ([§8](../docx-protection.md#8-api-python-tipos-estados-y-errores-d2)); permisos y lectores genéricos ([§9](../docx-protection.md#9-permisos-y-acceso-a-artefactos-d2-p-05)); bloqueos, instante de decisión y fallos ([§10](../docx-protection.md#10-ejecución-durable-bloqueos-y-publicación-d2)); retención ([§11](../docx-protection.md#11-ciclo-de-vida-y-retención)); persistencia y migración `0005` ([§12](../docx-protection.md#12-persistencia-y-migración-0005)); límites, observabilidad y evidencia ([§13–§15](../docx-protection.md#15-evidencia-prevista-muestras-lectores-y-ambientes)).

**Excluye:** PDF (A08); inferencia real, calidad y segmentación por modelo (A09); HTTP y rutas nuevas; integración con CAWbase, UI y políticas de salida (bloque B); ampliar la matriz de extracción de A05 (varias secciones, anidación > 2, `customXml`, etc.); OCR y texto en píxeles; restaurar un DOCX editado; recuperar contenido eliminado irreversiblemente; identidad visual o byte a byte con el original.

## 2. Decisiones

Aprobadas por el usuario: **D-A07-01** (fuente de hallazgos), **D-A07-02** (ejecución y permisos), **D-A07-03** (restauración del derivado exacto), **D-A07-04** (fidelidad de runs), **D-A07-05** (cobertura y limpieza), texto en [A07-decisions §2](../decisions/A07-decisions.md#2-decisiones-aprobadas-por-el-usuario). Elecciones técnicas `E-A07-01` a `20` en [§3](../decisions/A07-decisions.md#3-elecciones-técnicas-derivadas-e-a07-nn-no-son-decisiones-de-producto). Propuestas **pendientes** `P-A07-01` a `07` en [§4](../decisions/A07-decisions.md#4-propuestas-pendientes-de-revisión-p-a07-nn). Hipótesis: [contrato §17](../docx-protection.md#17-hipótesis-por-verificar-h-a07-nn).

## 3. Reglas de implementación (Fase 2, tras la aprobación)

1. Implementar desde las entradas normales (`submit_*`, worker de A05, mantenimiento) y **reutilizar** A05 (fases, lease, *fencing*, reintentos, idempotencia, storage, cuotas, fence, limpieza) y A06 (unión, política, parser, contextos, correspondencias, claves) **sin duplicar su lógica**; no importar `experiments/a04`.
2. Código bajo un subpaquete propio con importación **perezosa**; transformación y verificación en el **proceso aislado** de A05 §10.5 con sus límites; D-33 intacta (sin relajar umbrales; una reejecución no sustituye la primera medición).
3. El mock de A03 sustituye **solo** la inferencia del análisis fuente. Storage, extracción, transformación, verificación, criptografía, persistencia, bloqueos y permisos son **reales**; PostgreSQL y SQLite reales (bases descartables `cce_t_*`).
4. Sin cambios en contratos públicos de A01–A06 ni en sus aserciones salvo los justificados (catálogo, versión, nivel), preservando la garantía que verificaban.
5. Si una garantía aprobada resulta inviable, se trae el caso reproducible al usuario **antes** de modificar el alcance.
6. Evidencia separada: **especificado ≠ implementado ≠ probado en macOS ≠ probado en Linux local ≠ probado en GitHub Actions ≠ aceptado**; un *skip* no es aprobación. Informe: `docs/a07-implementation-report.md` (propuesto).
7. Fixtures sintéticos con manifiesto independiente; ningún dato real ni secreto; claves de prueba aleatorias y descartables. Carreras con sincronización controlada (conexión que retiene el bloqueo y espera detectada en `pg_stat_activity`; escritor único en SQLite), no con `sleep`.

## 4. Muestras, lectores y ambientes previstos

| Elemento | Previsto |
|---|---|
| Fixtures | Sintéticos generados por código propio en `tests/` con manifiesto independiente: F01 valor entre 3 runs con formato mixto (negrita/cursiva/color) y Unicode (no BMP, NFD, ZWJ, espacios); F02 tablas anidadas (profundidad 2) y celdas `gridSpan`/`vMerge`; F03 encabezados/pies `default`/`first`/`even` y notas al pie y al final; F04 campos simples y complejos (incluido uno que cruza párrafos y uno anidado, y `HYPERLINK` complejo); F05 hipervínculo con selección dentro y otra cruzando el borde; F06 control de texto simple y controles no simples/bloqueados; F07 comentarios con hilos, `people`, propiedades núcleo/aplicación/personalizadas y miniatura; F08 copia del valor en destino de hipervínculo, texto alternativo, instrucción de campo y propiedad conservada (CN-05); F09 ocurrencia idéntica no seleccionada (CN-02); F10 todas las ocurrencias seleccionadas (CN-03); F11 documento sin los valores (CN-01); F12 marcadores de posición y elementos de run no textuales dentro de un valor; F13 entrada con sintaxis de marcador |
| Fixture de editor | **E01**: contenido sintético guardado por **LibreOffice Writer** (versión registrada) en la imagen Linux; generador y lector registrados. Si A05 lo rechaza, se informa al usuario (H-A07-09) |
| Lectores | `python-docx` (apertura), `lxml` (transformador), biblioteca estándar (verificador independiente), extractor de A05; **LibreOffice Writer headless** + Poppler para renders (Linux). **Word: no disponible** ⇒ pendiente declarado |
| Ambientes | macOS (renders DOCX pendientes si no hay LibreOffice) y Linux local (Docker: aislamiento `linux_container_reference`, PostgreSQL 15–18 y SQLite reales, LibreOffice). GitHub Actions: no ejecutado salvo que lo haga el usuario |

## 5. Matriz de aceptación AC-A07

Cada fila: **garantía** → **caso de prueba** → **evidencia esperada**. «PG+SQ» = PostgreSQL y SQLite reales. El estado real (verificado, parcial, sin evidencia) irá en el informe de implementación, no aquí.

### 5.1 Fuente de hallazgos y vinculación (D-A07-01)

| AC | Garantía | Caso de prueba | Evidencia esperada |
|---|---|---|---|
| AC-A07-01 | Protección E2E desde un análisis de A05 con mock: DOCX protegido publicado, vínculo `active`, informe sin valores | F01 cargado → `document_analysis` (mock) → `submit_docx_protection` → worker → descarga | prueba E2E, PG+SQ; inspección del informe (sin valores) |
| AC-A07-02 | La API no admite hallazgos, offsets, `AnalysisResult` ni `SourceMap` del llamador | inspección de la firma y llamada con argumentos extra | prueba de la firma (`TypeError`), prueba estática |
| AC-A07-03 | Fuente inválida: otro scope, `text_analysis`, no `succeeded`, datos `expired`/`deleted`/`lost`, vence antes de `deadline_at`, sin `results:read` | un caso por condición S1–S3, S12 | errores del contrato §8.4 sin crear trabajo ni contexto, PG+SQ |
| AC-A07-04 | Original no utilizable (eliminado, vencido, perdido, validación vigente `rejected`, vence antes de `deadline_at`) | casos S4 en envío y entre envío y reclamación | `file_not_usable`/`file_expiring` en envío; `input_unavailable` en el trabajo, PG+SQ |
| AC-A07-05 | Identidad del original exacta: un resultado y un mapa ligados a otro archivo, a otro tamaño o a otro formato se rechazan | manipulación controlada de los artefactos de prueba (escritos con hash válido por el storage de prueba) y de la fila del análisis | `analysis_source_invalid`, sin publicar, PG+SQ |
| AC-A07-06 | Extractor y configuración: otro `id`/`version`/`config_sha256` (p. ej. límites de A05 cambiados tras el análisis) ⇒ análisis desactualizado | worker con límites distintos; mapa con extractor alterado | `analysis_source_stale`, PG+SQ |
| AC-A07-07 | Mapa exacto: un `SourceMap` que difiere en un segmento, o un `text_length` distinto, se rechaza; la reextracción reproduce el mapa byte a byte (H-A07-01) | mapa alterado en una pieza; comparación en dos ambientes | `analysis_source_stale`; igualdad de hashes registrada |
| AC-A07-08 | Detección no concluyente en cualquier unidad (`inconclusive`, cobertura incompleta, `not_requested`, `not_configured`) | fixtures de mock por motivo | `detection_inconclusive`/`capabilities_not_configured`, sin correspondencias ni artefactos, PG+SQ |
| AC-A07-09 | Perfil y procedencia identificables; hallazgo fuera de rango o `type_id` inválido | resultado con perfil distinto del fijado; hallazgo `end > len` | `analysis_source_invalid` |
| AC-A07-10 | Política de A06: unión transitiva, adyacentes separados, precedencia de redacción, independencia del orden; mismo fragmento ⇒ mismo marcador en el contexto (también entre DOCX y `protect_text` del mismo contexto) | hallazgos `[0,10)`,`[8,20)`,`[19,30)`; adyacentes; mezcla `redact`/`pseudonymize`; permutaciones; dos DOCX y un texto con el mismo valor | grupos y marcadores esperados; una fila por fragmento, PG+SQ |
| AC-A07-11 | Colisión: texto de cualquier unidad con marcador estricto o alteración R1/R2 ⇒ rechazo antes de correspondencias; la prosa ordinaria («[CCE report]») se acepta | F13 por clase S/R1/R2 y prosa | `marker_collision` sin filas; prosa protegida normalmente |

### 5.2 Matriz de transformación, runs y campos (D-A07-04)

| AC | Garantía | Caso de prueba | Evidencia esperada |
|---|---|---|---|
| AC-A07-12 | Valor en un run y repartido en 2–5 runs con formato mixto: sustituto íntegro en un `w:t` del primer run con su `w:rPr`; runs intermedios vaciados; prefijo y sufijo intactos (TX-01, 02) | F01 | oráculo O1 y esqueleto E2; comparación de `w:rPr` del primer run |
| AC-A07-13 | Unicode y espacios: offsets en puntos de código; `xml:space="preserve"` cuando corresponde; regiones fuera de las selecciones idénticas (TX-04) | F01 (no BMP, NFD, ZWJ, espacios iniciales/finales, CRLF no aplica a DOCX) | O1; inspección de atributos |
| AC-A07-14 | Varias selecciones en un mismo párrafo, run y `w:t`, aplicadas sin desplazamientos (TX-03) | F01 con 3 grupos en un `w:t` | O1 |
| AC-A07-15 | Tablas, anidación 2, `gridSpan`/`vMerge`: texto transformado; `w:tblGrid`, `w:tcPr` y combinaciones intactos (TX-05, 06) | F02 | O1; E2 de la parte del documento |
| AC-A07-16 | Encabezados y pies `default`/`first`/`even` y notas: texto transformado; referencias, `w:titlePg`, `w:evenAndOddHeaders`, numeración de notas intactos (TX-07, 08) | F03 | O1; E1/E2; render (AC-A07-71) |
| AC-A07-17 | Hipervínculo: selección dentro del texto visible se transforma con destino intacto; selección que cruza el borde ⇒ fallo (TX-09) | F05 | O1 y `Target` intacto; `selection_crosses_boundary` |
| AC-A07-18 | Control de texto simple: selección dentro se transforma con `w:sdtPr` intacto; dentro de `richText`/casilla/cita, con `showingPlcHdr` o con bloqueo de contenido ⇒ fallo; cruzando el borde ⇒ fallo (TX-14, 15) | F06 | O1; códigos `selection_not_transformable`/`selection_crosses_boundary` |
| AC-A07-19 | Campos: selección dentro de un resultado simple o complejo, que cruza desde texto normal hacia un resultado, en un campo que cruza párrafos, anidado o `HYPERLINK` complejo ⇒ `field_result_not_transformable`; las instrucciones se conservan (TX-11 a 13) | F04 | código por caso; documento sin publicar; prueba pura del planificador con la pila de campos |
| AC-A07-20 | Pila de campos desequilibrada ⇒ `unsupported_structure` | variante de F04 | código |
| AC-A07-21 | Elementos de run no textuales dentro de la selección (`w:tab`, `w:br`, `w:cr`, `w:noBreakHyphen`, `w:sym`, `w:footnoteReference`, `w:drawing`, `w:fldChar`) ⇒ `selection_not_transformable` (TX-26) | F12 por elemento | código por elemento |
| AC-A07-22 | Marcadores de posición o permisos con un extremo dentro de la selección ⇒ `selection_crosses_boundary`; extremos fuera de la selección no la afectan (TX-24) | F12 | códigos y caso positivo |
| AC-A07-23 | Contenedores en línea (`w:smartTag`, `w:customXml` en línea, `w:ruby`, `w:dir`, `w:bdo`) ⇒ `selection_not_transformable` (TX-25) | fixtures por contenedor | código |
| AC-A07-24 | Rechazos heredados de A05 (revisiones, texto oculto, cuadros de texto, OLE/macros, referencias externas, cifrado, firmas, varias secciones, anidación > 2, `customXml`, etc.): no llegan a protegerse (TX-27) | una familia por fixture de A05 a través del flujo completo | el análisis falla con su motivo cerrado; `submit_docx_protection` ⇒ `job_not_succeeded` |
| AC-A07-25 | Fallo de una selección ⇒ fallo de todo el trabajo: nada publicado, ningún vínculo, sin degradar a redacción | documento con una selección válida y otra no transformable | trabajo `failed`; cero artefactos `published`; cero vínculos |
| AC-A07-26 | Estructura y estilos fuera de las selecciones conservados: partes no editadas byte a byte; en las editadas, solo cambian las piezas, su `xml:space`, los runs vaciados y las `E` eliminadas (E1, E2) | todos los fixtures | verificador independiente; prueba de que el verificador **falla** con un derivado alterado fuera de la selección (control negativo) |
| AC-A07-27 | El derivado vuelve a pasar la validación y la extracción de A05 (V5) | todos los fixtures | prueba |

### 5.3 Limpieza, cobertura y residuos (D-A07-05)

| AC | Garantía | Caso de prueba | Evidencia esperada |
|---|---|---|---|
| AC-A07-28 | Eliminación obligatoria de comentarios (partes, hilos, `people`, anclajes en documento, encabezados y notas, relaciones, tipos de contenido y partes solo alcanzables desde comentarios), conservando el texto anotado | F07 | inspección independiente del paquete; O1 del texto anotado; clases registradas |
| AC-A07-29 | Propiedades: descriptivas/personales y personalizadas eliminadas; fechas y estadísticas conservadas; miniatura eliminada | F07 | inspección de `docProps/*`, `_rels/.rels`, `[Content_Types].xml`; clases registradas con conteos |
| AC-A07-30 | Paquete coherente tras eliminar: sin relaciones colgantes, sin partes inalcanzables, sin tipos de contenido huérfanos (V3, V4); estructuras de comentarios desconocidas ⇒ `unsupported_structure` (PC-01) | F07 y variante con parte de comentarios desconocida | verificador; código |
| AC-A07-31 | `N` admitidos conservados con cobertura parcial explícita (`declared_not_covered`) y `P` conservados byte a byte; `document_coverage_complete` del derivado verdadero solo sin `N` | F07, F08 y un documento sin `N` | informe; E1 |
| AC-A07-32 | Oráculo por posición: el texto del derivado es exactamente el esperado en ambas extracciones (verificador y A05); un derivado con texto no seleccionado perdido o un sustituto desplazado **falla** la verificación | todos los fixtures; dos controles negativos inyectados en el verificador | O1; controles negativos fallan |
| AC-A07-33 | Control: el **original sin transformar** presentado como derivado falla la verificación (oráculo y residuos) | original como entrada del verificador | `derivative_verification_failed`/`residue_detected` |
| AC-A07-34 | Copia del valor seleccionado en destino de hipervínculo (también codificada `%XX`), texto alternativo, instrucción de campo o propiedad conservada ⇒ `residue_detected` y nada publicado | F08, un caso por ubicación | código; cero artefactos publicados |
| AC-A07-35 | Ocurrencia idéntica deliberadamente **no seleccionada** permanece en su posición y no es residuo | F09 | O1 la exige en su posición; trabajo `succeeded` |
| AC-A07-36 | Todas las ocurrencias seleccionadas ⇒ ausencia en **todo** el alcance inspeccionable (incluidas las representaciones de §6.3) | F10 | barrido completo: cero coincidencias |
| AC-A07-37 | Documento sin los valores: sin hallazgos ⇒ derivado con texto idéntico, `E` eliminadas, sin marcadores ni correspondencias | F11 | O1; cero filas en `protection_mappings` |
| AC-A07-38 | Representaciones: valor partido entre nodos no `S` (instrucción en varios `w:instrText`), referencias de carácter, NFC/NFD, mayúsculas distintas, blancos colapsados y UTF-16 en partes binarias se detectan; una búsqueda solo de bytes literales **no** los detectaría | fixtures por representación, con comparación frente a una búsqueda ingenua de bytes | detección por el verificador; la búsqueda ingenua falla en al menos los casos partidos y codificados |
| AC-A07-39 | El alcance inspeccionado y lo no cubierto (píxeles, fuentes, contenido no extraído) se informan explícitamente, sin valores | todos los fixtures | `inspection_scope` del informe |
| AC-A07-40 | Clases `E` siguen ausentes tras restaurar | F07 protegido y restaurado | inspección del restaurado |

### 5.4 Restauración del derivado exacto (D-A07-03)

| AC | Garantía | Caso de prueba | Evidencia esperada |
|---|---|---|---|
| AC-A07-41 | Restauración E2E: copia exacta cargada → `submit_docx_restoration` → DOCX restaurado con cada valor seudonimizado exacto en su posición y con el formato del primer run | F01–F03, F05, F06 | oráculo R-O1; comparación del texto con el original (mientras existe) |
| AC-A07-42 | Funciona tras **expirar y purgar** el original, el análisis fuente y los metadatos del trabajo de protección | reloj de la base adelantado + `run_cleanup` + purga; luego restaurar | trabajo `succeeded`, PG+SQ |
| AC-A07-43 | Digest distinto (un byte cambiado, archivo rezipeado con el mismo contenido, otro DOCX) ⇒ rechazo antes de analizar | variantes del archivo | `derivative_mismatch` en el envío y en el intento; el proceso aislado **no** se invoca (instrumentación) |
| AC-A07-44 | Marcador alterado, ajeno o fuera de posición ⇒ rechazo antes de recuperar valores | (a) prueba pura del comprobador T5 con ocurrencias desplazadas, añadidas, quitadas, token cambiado y R1/R2; (b) vínculo manipulado en la base (ocurrencias o digest) | (a) `derivative_markers_invalid`; (b) `binding_integrity_failed`; sin lecturas de correspondencias |
| AC-A07-45 | Contexto ajeno (otro `context_id`, otro scope), de otro tipo, eliminado o vencido ⇒ rechazo | casos por condición, en el envío y entre envío y publicación | `resource_not_found`/`context_kind_mismatch`/`context_unavailable`; sin artefacto publicado |
| AC-A07-46 | Clave ausente (versión de la fila, de la clave de índice) ⇒ fallo cerrado | `KeyRing` sin la versión | `protection_key_unavailable`; sin publicar |
| AC-A07-47 | Fallo de integridad de una correspondencia (ciphertext, índice, token, versión alterados) ⇒ fallo cerrado | manipulación directa de filas | `mapping_integrity_failed` |
| AC-A07-48 | Permisos insuficientes: sin `docx:restore`, sin `files:write` para la copia, sin `docx:restored:read` para leer | matriz | `permission_denied` |
| AC-A07-49 | Las redacciones irreversibles no reaparecen: `[REDACTED]` permanece y ninguna fila de correspondencia existe para ellas | documento con grupos redactados | inspección del restaurado y de las tablas |
| AC-A07-50 | Una sola pasada: un original que contiene sintaxis de marcador aparece literal y no se reinterpreta | valor sintético con `[[CCE1:…]]` dentro | R-O1 |
| AC-A07-51 | Vínculo revocado (`delete_job_data` del trabajo de protección, P-A07-06) o terminado (contexto vencido + limpieza) ⇒ `derivative_binding_unavailable` | casos | código |
| AC-A07-52 | La restauración no llama a la inferencia | adaptador instrumentado | cero llamadas |

### 5.5 Permisos y lectores genéricos (D-A07-02)

| AC | Garantía | Caso de prueba | Evidencia esperada |
|---|---|---|---|
| AC-A07-53 | Matriz de los 25 permisos para cada operación nueva (con y sin cada uno); combinaciones (`docx:protect` + `results:read`); `AdminContext` sin acceso; delegación D-27 | matriz completa | prueba, PG+SQ |
| AC-A07-54 | **Lectores genéricos** no eluden los controles: `get_job_result`/`open_job_artifact` (Python), `GET /jobs/{id}/result` y `/source-map` (HTTP) sobre trabajos de A07 ⇒ `resource_not_found`/404 aun con `results:read`; `open_file_content` no alcanza derivados; `list_jobs`/`get_job` solo metadatos | cada lector con una clave con todos los permisos genéricos y sin los de A07 | códigos; ningún byte de DOCX protegido o restaurado ni de los informes |
| AC-A07-55 | El código genérico de `0.5.0` (antes de la guarda por operación) tampoco sirve artefactos de A07 (H-A07-03) | lector genérico de la base ejecutado sobre filas de A07 | `resource_not_found` |
| AC-A07-56 | Descargas verificadas (A05 §3.7): objeto corrupto o ausente ⇒ sin `200` completo / sin bytes en Python | corrupción en el storage de prueba | `stored_object_unavailable`; artefacto `lost` |
| AC-A07-57 | DOCX restaurado: acceso según P-A07-03 (contexto vigente y scope activo en el instante de la lectura) | lectura tras `delete_context`, tras vencimiento y con scope deshabilitado | denegado según la decisión del usuario sobre P-A07-03 |
| AC-A07-58 | Cruce con `restore_text` de A06 según la decisión del usuario sobre P-A07-01 | marcador de un DOCX entregado como material a `restore_text` | comportamiento decidido, probado |

### 5.6 Publicación, ciclo de vida, concurrencia y fallos

| AC | Garantía | Caso de prueba | Evidencia esperada |
|---|---|---|---|
| AC-A07-59 | Un resultado solo es visible con todos sus artefactos y, en la protección, con su vínculo `active` (misma transacción); nunca uno sin el otro | inyección de fallo antes, durante y después del `COMMIT` de publicación | tablas consistentes en todos los casos; respuesta perdida reconciliada (A05 §7.3) |
| AC-A07-60 | Instante de decisión único tras los bloqueos: contexto que vence, análisis que vence u original que vence **mientras la publicación espera** un bloqueo ⇒ sin publicar | conexión independiente que retiene el bloqueo del contexto (PG, `pg_stat_activity`) y reloj de la base | `context_unavailable`/`analysis_unavailable`/`input_unavailable`; cero vínculos |
| AC-A07-61 | `delete_context`, vencimiento y deshabilitación del scope antes de las correspondencias, entre correspondencias y publicación y mientras un intento espera bloqueos | carreras controladas en ambos órdenes | F1, F2, F5 de §10.3; correspondencias nuevas eliminadas con el contexto; sin restos visibles |
| AC-A07-62 | Pérdida del lease, reintentos y publicación repetida: el dueño anterior no publica; el reintento reutiliza los marcadores (mismo fragmento ⇒ mismo marcador) y no duplica filas; artefactos de intentos fallidos `discarded` y limpiados según A05 §4.4 | lease vencido por reloj de la base; fallo inyectado; repetición de la publicación | una sola publicación; conteo de filas; limpieza confirmada |
| AC-A07-63 | Fallos entre base y storage en cada frontera (escritura parcial, objeto sin registro, registro sin publicación, objeto corrupto) | inyección en el storage de prueba | estados de A05 §4.1; nada visible a medias |
| AC-A07-64 | Restauración frente a `delete_context`/vencimiento/revocación entre el descifrado y la publicación: sin publicar; el restaurado escrito queda `discarded` y se purga según P-A07-04 | carreras controladas | F14 de §10.3; objeto purgado |
| AC-A07-65 | Idempotencia: misma clave y huella ⇒ mismo trabajo (y mismo contexto con `NewContext`); huella distinta ⇒ `idempotency_conflict`; dos primeras concurrentes | casos | prueba, PG+SQ |
| AC-A07-66 | Orden de bloqueos ascendente (§10.1) en todos los caminos y sin interbloqueos entre operaciones del motor (protección, restauración, `delete_context`, `delete_job_data`, limpieza concurrentes) | registro de sentencias con bloqueo (PG) y carreras | registro; ausencia de `40P01` entre operaciones del motor |
| AC-A07-67 | Retención: artefactos 7 d (D-A05-01); vínculo hasta el fin del contexto; `run_docx_cleanup` marca `ended`, borra ocurrencias y purga lápidas; repetible | reloj de la base | prueba, PG+SQ |
| AC-A07-68 | Límites: derivado mayor que `max_file_bytes`, más selecciones u ocurrencias que el máximo, cuotas del contexto ⇒ rechazo explícito, sin truncar | fronteras ±1 | `limit_exceeded`/`quota_exceeded` |
| AC-A07-69 | Sin contenido sensible (valores, marcadores, tokens, posiciones, `node_path`, hashes de contenido) en logs, errores, eventos ni diagnósticos | marcadores únicos en todos los casos | barrido de logs y errores |

### 5.7 Fidelidad, apertura y renders

| AC | Garantía | Caso de prueba | Evidencia esperada |
|---|---|---|---|
| AC-A07-70 | Apertura sin reparación del protegido y del restaurado con los lectores disponibles (`python-docx`, biblioteca estándar, LibreOffice); Word declarado no disponible | todos los fixtures y E01 | salida de cada lector registrada; LibreOffice sin aviso de reparación (H-A07-07) |
| AC-A07-71 | Inspección visual representativa de original, protegido y restaurado con renders comparables: legibilidad, formato fuera del tramo, tablas, encabezados, pies, notas y reflujo | F01–F03, F05, F06, E01 en Linux (LibreOffice + Poppler) | imágenes y montaje antes/después; editor/lector, versión, plataforma y fuentes registrados; revisión manual descrita; macOS pendiente si no hay LibreOffice |
| AC-A07-72 | Fixture guardado por un editor habitual protegido y restaurado | E01 (LibreOffice) | generador y lector registrados; resultado o motivo de rechazo informado al usuario |

### 5.8 Persistencia, compatibilidad, distribución y documentación

| AC | Garantía | Caso de prueba | Evidencia esperada |
|---|---|---|---|
| AC-A07-73 | Migración `0005` aditiva sobre una base `0004` con datos de A01–A06: datos intactos, `schema_level` 5, idempotente | migración sobre bases pobladas | prueba, PG+SQ |
| AC-A07-74 | Compatibilidad: runtimes `0.4.0`/`0.5.0` (simulados por sus constantes y claves) arrancan sobre nivel 5, no reclaman trabajos de A07 y no sirven sus artefactos; `0.6.0` sobre nivel 4 ⇒ `schema_outdated`; sin `ProtectionSetup`/`DocumentSetup` el motor arranca y A07 ⇒ `*_not_configured` | casos | prueba |
| AC-A07-75 | Regresiones A01–A06 sin cambios de garantía; aserciones heredadas modificadas (catálogo, versión, nivel) justificadas | suites completas | macOS y Linux Docker |
| AC-A07-76 | Distribución: base sin importar A07, `lxml` ni `cryptography`; D-33 intacta (mediana warm ≤ 300 ms en 15 procesos nuevos; ≤ 40 MiB); wheel/sdist sin `experiments/` | `pytest -m dist` + `scripts/measure_import.py` | mediciones por ambiente |
| AC-A07-77 | Superficie: sin rutas HTTP nuevas; `analyze-text`, A05 y A06 sin cambios de contrato | enumeración de rutas y operaciones | prueba |
| AC-A07-78 | Documentación alineada: enlaces y anclas sin romper; permisos, códigos, estados y nombres consistentes | `scripts/check_doc_links.py` + prueba de documentación | sin errores |

## 6. Entregables de la implementación (Fase 2)

Código con importación perezosa, migración `0005`, ejemplo de host Python, pruebas AC-A07, `docs/a07-implementation-report.md` con la trazabilidad AC → prueba → ambiente → resultado (SHA probado, comandos, versiones, omisiones), documentación permanente alineada, README/CLAUDE.md con el estado real y commits locales revisables. El push, la PR y el merge los hace el usuario.

## 7. Riesgos y límites declarados

- La matriz de A05 es conservadora: muchos DOCX reales se rechazan antes de llegar a A07 (`customXml`, varias secciones); A07 no la amplía.
- Las selecciones en campos, controles no simples, contenedores en línea o que crucen hipervínculos y marcadores de posición **fallan**: con detección real (A09) pueden ser frecuentes.
- El render y la apertura se prueban con LibreOffice; Word no está disponible.
- El mock no mide detección: la ausencia de los valores sintéticos no acredita ausencia de datos sensibles.
