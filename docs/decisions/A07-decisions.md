# A07 — Registro de decisiones

> **Estado: A07 ESPECIFICADA (revisión 1 de este registro), pendiente de la aprobación explícita del contrato por el usuario. No implementada.** Las decisiones `D-A07-01` a `05` fueron **aprobadas por el usuario** en el encargo de A07 y se registran aquí; las elecciones técnicas `E-A07-nn` se derivaron por criterio y **no** son decisiones de producto; las propuestas `P-A07-nn` cambian lo que observa un integrador y están **pendientes de revisión**; las hipótesis `H-A07-nn` están en el [contrato §17](../docx-protection.md#17-hipótesis-por-verificar-h-a07-nn).

Contratos que usan estas decisiones: [especificación de A07](../features/A07-proteccion-y-restauracion-docx.md) · [protección y restauración DOCX](../docx-protection.md). Decisiones heredadas: [A01](A01-decisions.md), [A02](A02-decisions.md), [A03](A03-decisions.md), [A04](A04-decisions.md), [A05](A05-decisions.md), [A06](A06-decisions.md).

Categorías (no mezclarlas): **decisiones aprobadas** (§2) · **elecciones técnicas derivadas** (§3) · **propuestas pendientes de revisión** (§4) · **diferencias con el playbook** (§5) · **backlog** (§6) · preguntas, tensiones e incompatibilidades (§7).

## 0. Historial de revisiones

| Rev. | Contenido |
|---|---|
| 1 | Primera redacción (solo documental): D-A07-01 a 05 aprobadas y registradas; E-A07-01 a 20; P-A07-01 a 07 pendientes; H-A07-01 a 10; matriz AC-A07-01 a 78. Sin código, esquema, dependencias, pruebas ni versión. |

## 1. Fuente, precedencia y herencia

- Referencia: `STAGE_3_PLAYBOOK.md`, revisión 6 (copia local ignorada por Git, idéntica a la copia de referencia del usuario; **no se modifica ni se declara actualizada**): §6 A07, §4, §8 (cierre sin modelo: «transformación real DOCX, residuos de valores señalados y revisión visual»), §9 (principios), §10 (checklist). Las decisiones posteriores prevalecen donde difieren (§5).
- Base verificada: `main` = `origin/main` = `5bcf4b6` (merge del PR #5 de A06, que contiene `e6a2a8b`), verificado con `git fetch`; árbol limpio; rama nueva `feature/a07-docx-protection-restoration`.
- Heredado **sin cambios**: A01 (D-06/D-07, D-08, D-11/D-33, D-13/D-14/D-23, D-17/D-27, D-30); A03 (offsets en puntos de código, mock, procedencia); A04 D-A04-01 a 03 (dependencias, contenido auxiliar y rechazos, fidelidad mínima; D-A04-04 sigue en A08); A05 D-A05-01 a 03 (retención, cambios administrativos, fence); A06 D-A06-01 a 04 y P-A06-01 a 06 (contextos, claves, política estricta, alcance de scope de los permisos con control del host).
- **A06**: implementada en `0.5.0` y mergeada en `main` (verificado en el historial: merge `5bcf4b6`, PR #5). Su **aceptación no está registrada** (ver §7.1). A07 se apoya en su contrato implementado sin declararla aceptada.

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
| E-A07-08 | Verificador en un proceso aislado distinto, con `zipfile` + `xml.etree.ElementTree` (analizador distinto del transformador, `lxml`); oráculo por posición, esqueleto y residuos; controles negativos obligatorios | §6 |
| E-A07-09 | La restauración **no** llama a la inferencia ni evalúa datos nuevos: el digest igual garantiza que el contenido es el emitido por el motor | §7.1 |
| E-A07-10 | Fases propias de A07 (`planning`, `mapping`, `transformation`, `verification`) además de las de A05 | §8.3 |
| E-A07-11 | Los lectores genéricos de A05 sirven solo `document_analysis`/`text_analysis` (comprobación de **operación** además de `kind`); los derivados nunca son `stored_files` | §9.2 |
| E-A07-12 | El vínculo se inserta **en la misma transacción** que publica el trabajo (T3): nunca artefacto sin vínculo ni vínculo sin artefacto | §10.2 |
| E-A07-13 | Limpieza de vínculos propia (`run_docx_cleanup`, CLI `cleanup-docx`), sin cambiar `run_cleanup` (A05) ni `run_protection_cleanup` (A06) | §11 |
| E-A07-14 | `binding_mac` = HMAC-SHA256 con la **clave de índice del contexto** (A06 §9.3) sobre el JSON canónico del vínculo: un atacante con solo la base no puede mover ocurrencias ni cambiar el digest sin que la restauración falle (`binding_integrity_failed`); composición estándar, sin criptografía propia | §12.1 |
| E-A07-15 | Sin cambios en tablas de A01–A06: operaciones y roles nuevos como valores de `jobs.operation`/`job_artifacts.kind`; fijación en `execution_snapshot`; columnas de referencia de A05 reutilizadas según la operación; una tabla nueva `docx_derivatives` (migración `0005`) | §12 |
| E-A07-16 | Límites iniciales: los de A05 §12 (también para derivado y restaurado), 10 000 selecciones y 10 000 ocurrencias por documento, cuotas de contexto de A06 §15 | §13 |
| E-A07-17 | Representaciones de residuo: exacta, NFC, NFD, `casefold`, blancos colapsados; concatenación por párrafo de nodos no `S`; atributos y destinos decodificados como URL; bytes crudos en UTF-8/UTF-16LE/BE; fallo cerrado también en atributos estructurales | §6.3 |
| E-A07-18 | Clases `E` con listas de **conservación** (no de eliminación) para `core.xml` y `app.xml`: lo no listado se elimina | §5 |
| E-A07-19 | Admisión: `context.expires_at ≥ deadline_at` (`context_expiring`) y `result_expires_at ≥ deadline_at` (`analysis_expiring`), por analogía con `file_expiring` de A05 | §3.2, §8.4 |
| E-A07-20 | Rango de bloqueo nuevo 14 (`docx_derivatives`), después de los de A06 | §10.1 |

## 4. Propuestas pendientes de revisión (`P-A07-nn`)

Elecciones que **cambian lo que un integrador observa**. Ninguna está aprobada; la recomendación figura primero. Se implementarán según lo que el usuario decida.

| ID | Pregunta | Recomendación | Alternativas |
|---|---|---|---|
| P-A07-01 | **Cruce con `restore_text` de A06.** Los marcadores de un DOCX son correspondencias del contexto: un llamador con `text:restore` que entregue esos marcadores como material puede obtener los valores **como texto** sin `docx:restore`. ¿Se admite? | **(A) Admitirlo y declararlo**: el contexto es compartido por diseño (A06: un fragmento ⇒ un marcador por contexto), el flujo conversacional lo necesita (un modelo cita un marcador del DOCX y la respuesta debe restaurarse) y `restore_text` sigue exigiendo `text:restore`, material declarado por el host y contexto vigente. `docx:restore` controla la **producción de un archivo restaurado** | (B) Etiquetar el origen de cada correspondencia y que `restore_text` rechace las de origen DOCX: exige una columna nueva en `protection_mappings` (cambio de una tabla de A06) y, para que una fila no pueda «cambiar de origen» con acceso a la base, incluirla en el AAD solo para filas nuevas. (C) Contextos de DOCX exclusivos (solo `NewContext`) y rechazo en `restore_text` de contextos con vínculos |
| P-A07-02 | **Forma de la entrada de restauración.** | La copia del derivado se presenta como **archivo de A05** (`file_id`: `upload_file` o `register_external_file`) más `derivative_id`; el motor compara digest | Permitir además restaurar desde el artefacto que guarda el motor sin presentar copia (B-A07-01): requiere una entrada sin `stored_file` (la columna `jobs.input_file_id` es `NOT NULL`) |
| P-A07-03 | **Acceso al DOCX restaurado publicado.** | Leerlo (y su informe) exige, en el instante de la lectura, **contexto vigente y scope activo**, además de `docx:restored:read` y de la retención de D-A05-01: eliminar o vencer el contexto revoca de inmediato el acceso | Solo `docx:restored:read` + retención de 7 d (como cualquier resultado de A05) |
| P-A07-04 | **Plazo de purga de un DOCX restaurado no publicado** (`discarded`). D-A05-01 fija 24 h para temporales abandonados y **solo el usuario la cambia** | Excepción para el rol `docx_restored`: elegible para borrado **inmediato** al descartarse (sigue el protocolo de sello y evidencia de A05 §4.4) | Mantener las 24 h de D-A05-01 sin excepción |
| P-A07-05 | **Permisos.** | Cuatro nuevos (catálogo 25): `docx:protect`, `docx:restore`, `docx:protected:read`, `docx:restored:read`; el envío de protección exige además `results:read`; no exige `files:read` (el derivado está pensado para quien no lee el original) | Exigir también `files:read` para proteger; o dos permisos (`docx:protect`, `docx:restore`) con la lectura bajo `results:read` (no separa la clase sensible) |
| P-A07-06 | **`delete_job_data` sobre un trabajo de protección.** | Elimina los artefactos (A05) **y revoca el vínculo** en la misma transacción: tras eliminar los datos del resultado no queda restauración posible para ese derivado | Mantener el vínculo hasta el fin del contexto (el host conserva su copia y podría restaurarla) |
| P-A07-07 | **Contexto creado por el envío** (`NewContext`, tipo `job`) bajo `docx:protect`, sin `text:protect` | Admitirlo: mantiene independientes los permisos documentales; además se admite un contexto existente (`ExistingContext`) del mismo scope | Exigir que el contexto exista (creado con `text:protect` de A06) |

## 5. Diferencias respecto del playbook (revisión 6)

| # | Playbook | A07 | Motivo |
|---|---|---|---|
| 1 | «usa hallazgos controlados» | Hallazgos de un `document_analysis` de A05 con el mock, vinculado al original exacto; nunca del llamador | D-A07-01 |
| 2 | «tratamiento de comentarios y revisiones» | Comentarios: eliminación obligatoria (E); revisiones: rechazo heredado de A05 (R) | D-A07-05, D-A04-02 |
| 3 | «Restauración verifica valores y estructura dentro del contrato» | Solo el derivado exacto, con vínculo persistido y contexto vigente; sin original ni análisis | D-A07-03 |
| 4 | «revisión visual representativa» | LibreOffice en Linux; Word no disponible (declarado) | evidencia disponible |

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

## 7. Preguntas abiertas, tensiones e incompatibilidades

1. **A06 no está registrada como aceptada.** El playbook hace depender A07 de A06. La rama parte del merge de A06 en `main`; ningún documento declara A06 aceptada y este registro no lo hace. No bloquea la documentación de A07; conviene que el usuario registre la aceptación (o sus condiciones) antes del cierre de A07.
2. **P-A07-01 (cruce con `restore_text`)** es la única tensión con una decisión aprobada: D-A07-02 pide permisos documentales independientes y A06 (aprobada) define contextos compartidos donde cualquier marcador emitido es restaurable con `text:restore`. Requiere decisión del usuario.
3. **P-A07-04** tocaría una decisión aprobada (D-A05-01); solo el usuario puede autorizar la excepción.
4. **Resultado y mapa de A05 sin texto.** No es una incompatibilidad: la vinculación al texto exacto se obtiene reextrayendo el original exacto (E-A07-03). Consecuencia: el original debe seguir disponible durante la protección (A05 lo exige igual) y un cambio de la configuración de extracción invalida los análisis previos.
5. **Matriz conservadora de A05.** Un DOCX guardado por un editor habitual puede rechazarse (p. ej. Word con `customXml`). Si el fixture de LibreOffice (H-A07-09) resulta rechazado, se informará con el caso concreto antes de cambiar el alcance.
6. No se encontró otra incompatibilidad material con D-A07-01 a 05.
