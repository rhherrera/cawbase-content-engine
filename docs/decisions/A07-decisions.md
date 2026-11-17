# A07 — Registro de decisiones

> **Estado: A07 ESPECIFICADA (revisión 2 de este registro), pendiente de la aprobación explícita del contrato revisado por el usuario. No implementada.** Las decisiones `D-A07-01` a `05` (encargo de A07) y las propuestas `P-A07-01` a `07` (revisión del usuario de `55a39ed`, con el texto que el usuario fijó) están **aprobadas por el usuario**; su aprobación habilita la revisión documental, **no** la implementación. Las elecciones técnicas `E-A07-nn` se derivaron por criterio y **no** son decisiones de producto; las hipótesis `H-A07-nn` están en el [contrato §17](../docx-protection.md#17-hipótesis-por-verificar-h-a07-nn).

Contratos que usan estas decisiones: [especificación de A07](../features/A07-proteccion-y-restauracion-docx.md) · [protección y restauración DOCX](../docx-protection.md). Decisiones heredadas: [A01](A01-decisions.md), [A02](A02-decisions.md), [A03](A03-decisions.md), [A04](A04-decisions.md), [A05](A05-decisions.md), [A06](A06-decisions.md).

Categorías (no mezclarlas): **decisiones aprobadas** (§2) · **elecciones técnicas derivadas** (§3) · **propuestas aprobadas por el usuario** (§4) · **diferencias con el playbook** (§5) · **backlog** (§6) · preguntas, tensiones e incompatibilidades (§7).

## 0. Historial de revisiones

| Rev. | Contenido |
|---|---|
| 1 | Primera redacción (solo documental, `55a39ed`): D-A07-01 a 05 aprobadas y registradas; E-A07-01 a 20; P-A07-01 a 07 pendientes; H-A07-01 a 10; matriz AC-A07-01 a 78. Sin código, esquema, dependencias, pruebas ni versión. |
| 2 | Revisión del usuario de `55a39ed` (solo documental). **P-A07-01 a 07 aprobadas** con el texto del usuario; **P-A07-06 con la alternativa que conserva el vínculo** (sin revocación individual). Cambios: (1) restauración como texto por `restore_text` con todas las condiciones de A06 y distinción con `docx:restore` (contrato §7.1, §9.1–9.2); (2) entrada de restauración = copia del host verificada contra el vínculo (§7.1); (3) lectura del restaurado con instante de autorización tras el spool (§9.3); (4) purga inmediata de `docx_restored` descartado y comportamiento con runtimes anteriores (§11.1); (5) `docx:protect` no autoriza descargar el original (§9.1); (6) `delete_job_data` conserva el vínculo: estados `active`/`ended`, sin rango 14 en `delete_job_data`, F17–F18, sin degradación mixta (§9.2, §10, §11, §12); (7) `NewContext` confirmado junto con la admisión y replay con el contexto original (§8.1). **Correcciones obligatorias:** atribución de coincidencias de residuo a ocurrencias permitidas, incluido el escaneo de bytes crudos (§6.3, §6.3.1); validación de la procedencia simulada del mock (S10b, `analysis_provenance_unsupported`); «validación estructural del paquete» separada de la apertura real con LibreOffice en Linux (§6.1, §15). Nuevas: E-A07-21 y 22; AC-A07-79 a 87; reescritos AC-A07-09, 27, 35, 51, 57, 58, 64, 70 y 71. |

## 1. Fuente, precedencia y herencia

- Referencia: `STAGE_3_PLAYBOOK.md`, revisión 6 (copia local ignorada por Git, idéntica a la copia de referencia del usuario; **no se modifica ni se declara actualizada**): §6 A07, §4, §8 (cierre sin modelo: «transformación real DOCX, residuos de valores señalados y revisión visual»), §9 (principios), §10 (checklist). Las decisiones posteriores prevalecen donde difieren (§5).
- Base verificada: `main` = `origin/main` = `5bcf4b6` (merge del PR #5 de A06, que contiene `e6a2a8b`), verificado con `git fetch`; rama `feature/a07-docx-protection-restoration` (revisión 1 en `55a39ed`).
- Heredado **sin cambios**: A01 (D-06/D-07, D-08, D-11/D-33, D-13/D-14/D-23, D-17/D-27, D-30); A03 (offsets en puntos de código, mock, procedencia); A04 D-A04-01 a 03 (D-A04-04 sigue en A08); A05 D-A05-01 a 03 (con la **única** excepción acotada de P-A07-04 a D-A05-01, aprobada por el usuario); A06 D-A06-01 a 04 y P-A06-01 a 06.
- **A06**: implementada en `0.5.0` y **mergeada** en `main` (verificado en el historial: merge `5bcf4b6`, PR #5); **aceptación pendiente de la declaración expresa del usuario** (§7.1). A07 se apoya en su contrato implementado sin declararla aceptada.

## 2. Decisiones aprobadas por el usuario

### D-A07-01 — Fuente de hallazgos

El trabajo de protección consume un `document_analysis` **vigente y autorizado** de A05, generado con el mock de A03. Se exige **detección completa y concluyente**, **perfil y versión identificables** y **vinculación verificable** del original, el resultado y el `SourceMap` con el **SHA-256 exacto, el extractor y su configuración**. Los hallazgos se resuelven **por unidad** y offsets Unicode `[start,end)` del **texto exacto**. Se rechazan hallazgos aportados directamente por el llamador, mapas ajenos, desactualizados o ambiguos y selecciones imposibles de aplicar sin pérdida colateral. Se reutiliza la política de A06, incluida la **unión determinista** de solapamientos y la **precedencia de la redacción** irreversible. A09 incorpora el modelo real.

### D-A07-02 — Ejecución y permisos

Dos **operaciones durables de A05**, una para proteger DOCX y otra para restaurarlo, expuestas por **API Python**. **Permisos documentales independientes** para protección y restauración y **autorización para consultar o descargar cada clase de artefacto**. El original y el DOCX restaurado son **material sensible**. Los lectores genéricos de trabajos, archivos o artefactos existentes **no deben permitir eludir** las comprobaciones de acceso. Se reutilizan idempotencia, ownership del intento, verificación de storage, límites, aislamiento y limpieza de A05. Los DOCX protegidos y restaurados salen como **artefactos derivados identificables**, con **procedencia y retención explícitas**.

### D-A07-03 — Restauración del derivado exacto

Se admite **únicamente el DOCX protegido exacto emitido por el motor**. Identidad y digest del derivado se vinculan con **scope, contexto y posiciones emitidas**. Se valida que se trate de ese artefacto y que **los marcadores estén en las ubicaciones autorizadas antes de recuperar valores**. Se rechazan archivos editados y marcadores ajenos, alterados o fuera de posición. La restauración depende del **derivado, de su vínculo persistido y de un contexto vigente**, y debe funcionar **aunque el original y el análisis inicial ya no estén disponibles**. Se reutilizan contextos, gramática de marcadores, correspondencias cifradas y claves versionadas de A06. Se restituyen **en una pasada** únicamente los valores seudonimizados autorizados. La redacción irreversible y las clases auxiliares eliminadas **carecen de restitución**.

### D-A07-04 — Fidelidad de runs

Tanto el sustituto como el valor restaurado **adoptan el formato del primer run afectado**. Se admite **reflujo** de líneas y páginas. Se conservan texto, estructura y estilos **fuera de la selección**, incluidas rejillas y combinaciones de tablas, propiedades de párrafo, referencias y partes soportadas. Se **rechazan intervalos que crucen elementos cuya edición segura no pueda justificarse**, como un salto estructural o un límite con semántica de enlace. La garantía de restauración comprende **el valor exacto y la estructura dentro de esos límites**; la distribución de formato mixto dentro del valor queda **fuera** de la garantía.

### D-A07-05 — Cobertura y limpieza

Se **eliminan obligatoriamente** las clases `E` autorizadas por A04 durante la protección y se **registran las clases efectivamente eliminadas**: comentarios conocidos, sus anclajes y partes auxiliares, propiedades descriptivas o personales y miniatura. Se conservan el texto anotado, la información estructural y las fechas que A04 conserva. Se mantienen los rechazos `R` y los elementos estructurales inspeccionados `P` de la matriz aplicable. Se preservan los `N` admitidos con **cobertura parcial explícita**. Si una **copia de un valor seleccionado** aparece como residuo en contenido inspeccionable, se **falla antes de publicar** el DOCX protegido. Una **ocurrencia textual idéntica fuera de la selección** se conserva y se comprueba **por posición**. El texto en píxeles queda **fuera de cobertura**.

## 3. Elecciones técnicas derivadas (`E-A07-nn`; no son decisiones de producto)

| ID | Elección | Dónde |
|---|---|---|
| E-A07-01 | Instante de decisión único: la hora de la base se lee **una vez**, tras adquirir todos los bloqueos de la transacción, y decide juntas posesión, vigencias (análisis, entrada, contexto) y estado del scope | contrato §1, §10.2 |
| E-A07-02 | La entrada es el `job_id` del análisis fuente; el original es su entrada fijada. Ningún parámetro de hallazgos, offsets, resultado ni mapa | §3.1 |
| E-A07-03 | Vinculación al texto exacto por **reextracción aislada** del original con el mismo extractor y configuración y comparación **byte a byte** del JSON canónico del `SourceMap` (el resultado y el mapa de A05 no contienen el texto). Configuración o versión distinta ⇒ `analysis_source_stale` (no se recalcula) | §3.2 |
| E-A07-04 | Unión, política, marcadores y verificación de reutilización: **las mismas funciones** de A06, por unidad | §3.3 |
| E-A07-05 | Reglas de selección R1–R5: solo piezas `w:t`; runs con `w:rPr`/`w:t`/`w:lastRenderedPageBreak`; entre piezas solo runs válidos, `w:proofErr` y anclajes de comentario; un único contenedor (ninguno, un `w:hyperlink` o un control de texto simple); fuera de campos | §4.3 |
| E-A07-06 | Resultados de campo detectados por el **planificador de A07** (pila de `w:fldChar` por parte y descendientes de `w:fldSimple`), sin cambiar el extractor de A05 ni el `SourceMap` | §4.4 |
| E-A07-07 | Sustituto íntegro en un único `w:t` del primer run; runs intermedios vaciados (y eliminados si quedan sin contenido); sufijo en el último; `xml:space="preserve"` según haga falta; aplicación de derecha a izquierda | §4.5 |
| E-A07-08 | Verificador en un proceso aislado distinto, con `zipfile`, `xml.parsers.expat` y `xml.etree.ElementTree` (analizadores distintos del transformador, `lxml`); oráculo por posición, esqueleto y residuos; controles negativos obligatorios. Sus comprobaciones V1–V6 son la **validación estructural del paquete**, no la apertura en un procesador de textos | §6 |
| E-A07-09 | La restauración **no** llama a la inferencia ni evalúa datos nuevos: el digest igual garantiza que el contenido es el emitido por el motor | §7.1 |
| E-A07-10 | Fases propias de A07 (`planning`, `mapping`, `transformation`, `verification`) además de las de A05 | §8.3 |
| E-A07-11 | Los lectores genéricos de A05 sirven solo `document_analysis`/`text_analysis` (comprobación de **operación** además de `kind`); los derivados nunca son `stored_files` | §9.2 |
| E-A07-12 | El vínculo se inserta **en la misma transacción** que publica el trabajo (T3): nunca artefacto sin vínculo ni vínculo sin artefacto | §10.2 |
| E-A07-13 | Limpieza de vínculos propia (`run_docx_cleanup`, CLI `cleanup-docx`), sin cambiar `run_cleanup` (A05) ni `run_protection_cleanup` (A06) | §11 |
| E-A07-14 | `binding_mac` = HMAC-SHA256 con la **clave de índice del contexto** (A06 §9.3) sobre el JSON canónico del vínculo: un atacante con solo la base no puede mover ocurrencias ni cambiar el digest sin que la restauración falle (`binding_integrity_failed`); composición estándar, sin criptografía propia | §12.1 |
| E-A07-15 | Sin cambios en tablas de A01–A06: operaciones y roles nuevos como valores de `jobs.operation`/`job_artifacts.kind`; fijación en `execution_snapshot`; columnas de referencia de A05 reutilizadas según la operación; una tabla nueva `docx_derivatives` (migración `0005`) | §12 |
| E-A07-16 | Límites iniciales: los de A05 §12 (también para derivado y restaurado), 10 000 selecciones y 10 000 ocurrencias por documento, cuotas de contexto de A06 §15 | §13 |
| E-A07-17 | Representaciones de residuo: exacta, NFC, NFD, `casefold`, blancos colapsados; objetivos K1–K8 (texto de unidades, nodos, concatenación por párrafo, atributos, relaciones, comentarios/PI/CDATA, bytes crudos de partes XML y binarias); fallo cerrado en atributos estructurales | §6.3 |
| E-A07-18 | Clases `E` con listas de **conservación** (no de eliminación) para `core.xml` y `app.xml`: lo no listado se elimina | §5 |
| E-A07-19 | Admisión: `context.expires_at ≥ deadline_at` (`context_expiring`) y `result_expires_at ≥ deadline_at` (`analysis_expiring`), por analogía con `file_expiring` de A05 | §3.2, §8.4 |
| E-A07-20 | Rango de bloqueo nuevo 14 (`docx_derivatives`), después de los de A06 | §10.1 |
| E-A07-21 | **(rev. 2)** Atribución de coincidencias de residuo: ocurrencias permitidas `A` (tramos del oráculo con un valor seleccionado fuera de sustitutos) traducidas a su ubicación exacta (`node_path`, offsets) con el `SourceMap` del derivado; mapa de bytes del contenido de cada `w:t` con `expat` (`CurrentByteIndex`); exención **solo** de la coincidencia que cae exactamente en esa ubicación (a lo sumo una por ocurrencia, objetivo y representación); atributos, relaciones, comentarios/PI y partes binarias nunca exentos | §6.3.1 |
| E-A07-22 | **(rev. 2)** Validación de la procedencia simulada del mock con los campos que A05 guarda: igualdad con `inference_provenance` del `execution_snapshot` fijado y valores del mock de A03 (`kind = "mock"`, `simulated = true`, `adapter_id = "cce-mock-fixtures"`, `adapter_version = "1"`, `configuration_id = fxs-<16 hex>`, `model = null`, `inference_contract_version = 1`), también por unidad; si no ⇒ `analysis_provenance_unsupported`. A09 la revisa al incorporar el modelo real | §3.2 (S10b) |

## 4. Propuestas aprobadas por el usuario (`P-A07-nn`)

Elecciones que **cambian lo que un integrador observa**. El usuario las **aprobó** en su revisión de `55a39ed` con el texto que se resume abajo (rev. 2 de este registro). Conservan su identificador por trazabilidad; solo el usuario puede cambiarlas. Su aprobación **no** autoriza la implementación: falta la aprobación explícita del contrato revisado.

| ID | Aprobado (texto del usuario, resumido) | Dónde |
|---|---|---|
| P-A07-01 | **Restauración como texto.** `text:restore` puede recuperar **como texto** un valor cuyo marcador se emitió en un DOCX si se cumplen **todas** las condiciones de A06; en particular, el host incluyó el **marcador exacto** en el material que entregó al modelo para esa respuesta y comprueba el acceso a la conversación o al trabajo concreto. `docx:restore` autoriza **producir un archivo restaurado** | contrato §7.1, §9.1, §9.2 (4); AC-A07-58 |
| P-A07-02 | **Entrada de restauración.** El host presenta su copia del DOCX protegido como archivo de A05 (carga o registro) con las referencias exigidas por el contrato; se verifican **identidad, tamaño y digest** contra el vínculo persistido; funciona con la copia exacta aunque el original y el análisis fuente hayan expirado o se hayan purgado | §7.1, §7.2 (T1–T2); AC-A07-41 a 43 |
| P-A07-03 | **Lectura del restaurado.** `docx:restored:read`, vigencia del resultado y del contexto y scope activo. Para los bytes, el **instante de autorización** es **después de verificar el objeto en el spool e inmediatamente antes de entregar el stream**; si el contexto vence o se elimina, o el scope se deshabilita, mientras se lee storage, la descarga no empieza; una descarga ya iniciada se rige por ese instante. Las mismas condiciones se aplican al informe según su camino de entrega | §9.3; AC-A07-57, 84 |
| P-A07-04 | **Purga de restaurados descartados.** Excepción acotada a las 24 h de D-A05-01: **solo** `docx_restored` es elegible para purga inmediata una vez **confirmado su descarte**; la eliminación física sigue el protocolo de A05 y depende de la ejecución de la limpieza; se documenta qué runtime aplica la excepción y qué hacen los anteriores | §11.1; AC-A07-64, 87 |
| P-A07-05 | **Permisos.** `docx:protect`, `docx:restore`, `docx:protected:read`, `docx:restored:read` (catálogo 25). Enviar una protección exige además `results:read`. `docx:protect` autoriza al motor a usar el original para producir el derivado; descargar el original sigue exigiendo `files:read`. Se conservan los controles sobre todos los lectores genéricos | §9; AC-A07-53 a 55, 85 |
| P-A07-06 | **Alternativa que conserva el vínculo.** `delete_job_data` sobre el trabajo de protección elimina sus artefactos según A05 y **conserva el vínculo**; con el contexto vigente y las demás condiciones, el host puede restaurar su copia exacta. **Eliminar el contexto** termina la restaurabilidad de **todos** sus derivados. **Sin revocación individual** por derivado. Sin degradación de compatibilidad mixta asociada | §9.2 (5), §10.1, §10.3 (F17–F18), §11, §12; AC-A07-51, 83 |
| P-A07-07 | **Contexto creado con el envío.** `NewContext` de tipo `job` bajo `docx:protect` y `ExistingContext` autorizado del mismo scope; creación del contexto y admisión del trabajo **se confirman juntas**; un replay idempotente devuelve el **contexto original** | §8.1, §13; AC-A07-65, 86 |

## 5. Diferencias respecto del playbook (revisión 6)

| # | Playbook | A07 | Motivo |
|---|---|---|---|
| 1 | «usa hallazgos controlados» | Hallazgos de un `document_analysis` de A05 con el mock (procedencia simulada verificada), vinculado al original exacto; nunca del llamador | D-A07-01, E-A07-22 |
| 2 | «tratamiento de comentarios y revisiones» | Comentarios: eliminación obligatoria (E); revisiones: rechazo heredado de A05 (R) | D-A07-05, D-A04-02 |
| 3 | «Restauración verifica valores y estructura dentro del contrato» | Solo el derivado exacto, con vínculo persistido y contexto vigente; sin original ni análisis | D-A07-03 |
| 4 | «Archivo abrible, revisión visual representativa» | Apertura sin reparación y revisión visual con LibreOffice en Linux; Word pendiente como lector | evidencia disponible |

## 6. Backlog (fuera de los criterios de aceptación)

| ID | Tema | Dependencia |
|---|---|---|
| B-A07-01 | Restaurar desde el artefacto que guarda el motor sin presentar copia | P-A07-02 |
| B-A07-02 | Protección dentro de resultados de campo (semántica de campos; B-A04-15) | decisión de producto |
| B-A07-03 | Selecciones que crucen hipervínculos, marcadores de posición o controles no simples | decisión de producto |
| B-A07-04 | Conservar la distribución del formato mixto dentro del valor | decisión de producto |
| B-A07-05 | Varias secciones, `customXml`, cuadros de texto y demás ampliaciones de la matriz de A05 (B-A05-08) | A05 |
| B-A07-06 | Render y apertura con Microsoft Word | entorno disponible |
| B-A07-07 | Recalcular estadísticas de `app.xml` tras la sustitución | — |
| B-A07-08 | HTTP y cola de integración para A07 | decisión de producto |
| B-A07-09 | Extraer `w:sym`/`w:ptab`/`w:softHyphen` como texto cubierto (A05) | A05 |
| B-A07-10 | Revocación individual de un derivado sin eliminar el contexto | decisión de producto (P-A07-06 la excluye) |
| B-A07-11 | Aceptar análisis de un adaptador de modelo como fuente | A09 |

## 7. Preguntas abiertas, tensiones e incompatibilidades

1. **A06: mergeada, aceptación pendiente** de la declaración expresa del usuario. El playbook hace depender A07 de A06; ningún documento declara A06 aceptada y este registro no lo hace. No bloquea la documentación de A07.
2. **Resueltas en la revisión 2:** el cruce con `restore_text` (P-A07-01: admitido con todas las condiciones de A06; `docx:restore` controla el archivo) y la excepción a D-A05-01 (P-A07-04: acotada a `docx_restored`, aprobada por el usuario).
3. **Resultado y mapa de A05 sin texto.** No es una incompatibilidad: la vinculación al texto exacto se obtiene reextrayendo el original exacto (E-A07-03). Consecuencia: el original debe seguir disponible durante la protección (A05 lo exige igual) y un cambio de la configuración de extracción invalida los análisis previos.
4. **Matriz conservadora de A05.** Un DOCX guardado por un editor habitual puede rechazarse (p. ej. Word con `customXml`). Si el fixture de LibreOffice (H-A07-09) resulta rechazado, se informará con el caso concreto antes de cambiar el alcance.
5. **Atributos estructurales y valores cortos.** La regla de residuos no exime atributos (K4): un valor seleccionado con forma de valor estructural (p. ej. un número corto) puede bloquear la publicación (falso positivo, fallo cerrado). Es una limitación declarada, no una incompatibilidad.
6. No se encontró otra incompatibilidad material con D-A07-01 a 05 ni con P-A07-01 a 07.
