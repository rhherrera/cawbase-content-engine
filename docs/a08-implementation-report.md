# A08 — Informe de implementación (etapa 3)

> **Estado: IMPLEMENTADA en `0.7.0` (migración `0006`). La aceptación queda pendiente de la revisión del código y la evidencia por el usuario.** Este informe describe lo que el código **hace**, lo que las pruebas **demuestran** y lo que **no** demuestran:
>
> - **No es un acta:** no declara A08 aceptada ni cerrada.
> - **No se ejecutó en GitHub Actions.**
> - **Qué acredita:** ingeniería sobre el corpus nativo de las etapas 2 y 2B (LibreOffice, Chromium y macOS) y sobre controles sintéticos, con el mock de A03. **No** acredita calidad de detección (A09) ni las exportaciones de Word y Google Docs, que no están verificadas.
> - **Etapas anteriores:** los resultados de la etapa 2 (0/20) y la 2B (2/10) se conservan como se midieron, con sus criterios.

Relacionado: [contrato de A08](pdf-protection.md) · [especificación y matriz AC-A08](features/A08-proteccion-y-restauracion-pdf.md) · [decisiones de A08](decisions/A08-decisions.md) · [informe 2B (histórico)](a08-stage2b-report.md) · [procedimiento de desarrollo](development.md).

## 0. Identificación y estado de Git

| Dato | Valor |
|---|---|
| Rama | `feature/a08-pdf-protection-restoration` |
| Punto de partida | `b6f749d` (etapa 2B), árbol limpio verificado al empezar |
| Versión / migración / nivel | `0.7.0` · `0006_pdf_protection` · `schema_level` 6 · `min_runtime_level` 1 · sin extra nuevo; `documents` añade `pypdf>=7,<8` · catálogo 25 → **29** |
| Commit de evidencia | **`2c7809a`**: macOS y Linux final se ejecutaron sobre este SHA (Linux con `git archive`, `SNAPSHOT=head`). Ver §8 |
| Autorización | encargo de la etapa 3 del usuario: alcance PDF acotado y criterio visual ([decisiones §2.2](decisions/A08-decisions.md#22-etapa-3-alcance-y-criterio-visual-aprobados-por-el-usuario)), sin aprobación intermedia del contrato |

## 1. Estado por nivel de evidencia

| # | Nivel | Estado |
|---|---|---|
| 1 | Especificado | Sí: [contrato](pdf-protection.md), ajustado al alcance de la etapa 3 |
| 2 | Implementado | Sí (§2) |
| 3 | Probado en macOS | Sí: regresión completa (PostgreSQL 17 + SQLite), suites de A08 y renders de PDFium y Poppler (§4) |
| 4 | Probado en Linux local (Docker) | Sí: PostgreSQL 15–18, SQLite, cotas mínimas, distribución y renders (§4.2) |
| 5 | Probado en GitHub Actions | **No** |
| 6 | Aceptado por el usuario | **No** (pendiente) |

## 2. Qué se implementó

- **Extractor PDF v2** (`documents/pdf_extract.py`, `cce-pdf-extract` 1, `document-structures/2`). Lo usan los `document_analysis` nuevos.
  - DOCX conserva su identidad, y el extractor v1 queda congelado para los trabajos aceptados antes.
  - Snapshot con `extractors`, `requirements_key` `document/2` e identidad de validación v2 (P-A08-04).
  - Reextracción determinista: mismo mapa en macOS y en Linux.
- **Planificador, transformador y restaurador** (`documents/pdf_transform.py`), en el proceso aislado:
  - cobertura (RP3, RP5, RP7);
  - localización por offsets;
  - zonas TB, `Z1` y `Z2` calculadas del original, con tinta TrueType (`documents/ttf_ink.py`);
  - fuente candidata empaquetada (OFL, métricas congeladas);
  - eliminación real de los códigos;
  - operadores con extensión completa;
  - un bloque por selección `Z1` o por línea `Z2`;
  - limpieza de `/Info`, XMP, metadatos de objeto y comentarios;
  - restauración en una pasada con RZ.
- **Verificador independiente** (`documents/pdf_verify.py`): `pypdf` estricto, un analizador léxico y un intérprete propios, y sus propias reglas de zonas y cobertura. Cubre VA, VG, VN, VE, VS, VU, VC, VZP, VBND, VT, VB, VZ, VO, VL, Z2, VO1 y VK/VX/VP al proteger, y VM y la geometría restaurada al restaurar.
- **Operaciones durables** (`pdf_protection/`): `pdf_protection` y `pdf_restoration` sobre A05, con la publicación atómica de A07 parametrizada por tabla.
  - Vínculo `pdf_derivatives` con HMAC sobre ocurrencias y layout.
  - Comprobación previa del plan **antes** de crear correspondencias.
- **API, permisos y lectores:**
  - 7 métodos de `ScopedOperations`, 4 permisos nuevos y 4 roles de artefacto;
  - los lectores genéricos y los DOCX no sirven los artefactos de A08;
  - la lectura del restaurado se autoriza tras verificar el spool;
  - `delete_job_data` conserva el vínculo;
  - purga inmediata de `pdf_restored` descartado;
  - `run_pdf_cleanup` y la CLI `cleanup-pdf`.
- **Persistencia:** tabla `pdf_derivatives` y migración `0006`; `0.7.0` y nivel 6.
- **Reutilización sin duplicar:**
  - de A06: unión, precedencia, marcadores, contextos y criptografía;
  - de A07: auxiliares de envío, *hooks* de publicación, limpieza de vínculos, procedencia S10b y la comprobación del instante de autorización. Los *hooks* y la limpieza de A07 se parametrizaron por tabla, sin cambiar su comportamiento DOCX.

## 3. Trazabilidad AC-A08 → pruebas → ambiente

Estados: **verificado**, **parcial** o **sin evidencia**. «PG+SQ» = PostgreSQL y SQLite reales. Archivos:

| Abreviatura | Archivo |
|---|---|
| `pure` | `tests/test_a08_pure.py` (sin base) |
| `jobs` | `tests/test_a08_jobs.py` |
| `acc` | `tests/test_a08_access.py` |
| `life` | `tests/test_a08_lifecycle.py` |
| `mig` | `tests/test_a08_migration.py` |
| `ev` | `tests/test_a08_evidence.py` |

| AC | Estado | Evidencia (prueba · ambiente) | Lo que falta |
|---|---|---|---|
| 30 | verificado | jobs `test_protect_download_and_restore_end_to_end` (L1, C1) y `test_corpus_cases_with_the_approved_visual_criterion` (M1, L5, M5) · PG+SQ; acc `test_synthetic_document_cycle_and_identical_occurrence` | — |
| 31 | verificado | acc `test_submission_checks_idempotency_and_host_transaction`, `test_a_docx_analysis_cannot_feed_pdf_protection_nor_the_reverse` y `test_v1_analysis_is_stale…`; pure `test_extractor_v2_is_deterministic_on_the_corpus`. La procedencia S10b es la función de A07, ya probada allí | sin prueba propia de A08 para un adaptador de modelo (S10b): misma función que A07 |
| 32 | verificado | acc `test_v1_analysis_is_stale_and_a_new_analysis_of_the_same_file_is_valid`, `test_a_file_refused_by_v1_can_be_analysed_again_with_v2` y `test_an_earlier_runtime_does_not_claim_new_jobs`; mig (`0006` conserva filas y objetos; `0.6.0` funciona sobre el nivel 6); regresiones completas de A05–A07 · PG+SQ | el trabajo heredado se simula con la clave y el snapshot de `0.6.0`, no con un binario `0.6.0` |
| 33 | verificado | pure `test_matrix_rejections_of_the_v2_extractor`, `test_v2_admits_what_v1_refused`, `test_safe_rejections` y `test_structural_positives`; A05 `test_pdf_matrix_rejections` (v2) · PG+SQ | — |
| 34 | verificado | pure `test_safe_rejections` (varias líneas, ligadura, RTL, `/ActualText`, Form XObject, imagen, curva); jobs `test_c5_fails_explicitly_and_creates_nothing` (sin artefactos, vínculos ni correspondencias) | — |
| 35 | parcial | pure: una selección en un Form XObject ⇒ `selection_not_transformable` (P-A08-05 no implementada) | no hay clonado de objetos compartidos (fuera del alcance de la etapa 3) |
| 36 | verificado | pure `test_verifier_detects_alterations` (14 controles CV: residuos en `/Info`, comentario, glifo repetido, `/ActualText`; glifo fuera de zona desplazado 1 pt; pintado; enlace; XMP; plan que amplía su zona, factor, `x_orig`, avance, glifo desplazado y banda falseados) y `test_verifier_accepts_the_unaltered_derivative` | — |
| 37 | verificado | jobs y acc (copia exacta, sin inferencia, otro archivo ⇒ `derivative_mismatch`); life `test_restore_after_purging_original_analysis_and_protection_job_data`; pure `test_restorer_needs_only_the_derivative_binding_and_values` y `test_restore_verifier_detects_alterations` · PG+SQ | — |
| 38 | verificado | life: reglas de contexto, contexto eliminado antes de publicar, **expiración mientras la publicación espera el bloqueo** (PostgreSQL, espera observada en `pg_stat_activity`), reintento con reutilización de marcadores y una sola publicación, fallo de storage reintentable, pérdida del lease sin publicación, filas alteradas (layout, ocurrencias, cifrado, clave ausente), carrera con `delete_context` · PG+SQ | la espera de bloqueo solo se observa en PostgreSQL (SQLite serializa los escritores) |
| 39 | verificado | acc `test_permission_matrix_of_every_a08_operation`, `test_generic_readers_never_serve_a08_artifacts` (también los lectores DOCX y `docx:*`) · PG+SQ | — |
| 40 | verificado | life: lectura del restaurado con contexto y scope activo, instante de autorización tras el spool, purga inmediata (también de lo que dejó un runtime anterior), `delete_job_data` conserva el vínculo, `delete_context` y `cleanup-pdf` · PG+SQ | — |
| 41 | verificado | acc `test_existing_context_and_restore_text_cross_path` (sin el marcador entregado ⇒ `MarkerNotAuthorizedError`; `pdf:restore` no habilita texto) | — |
| 42 | verificado | life `test_no_values_in_logs_errors_or_reports`; pure `test_report_and_child_outputs_never_carry_values`; idempotencia en acc. Orden de bloqueos: rangos de A07 + 14 (revisión del código) | sin prueba de log de sentencias para el orden de bloqueos de A08 |
| 43 | §4 | regresiones A01–A07, `-m dist` y auditoría de artefactos | — |
| 44 | verificado | este informe y los documentos permanentes; `scripts/check_doc_links.py` | — |
| [V] | verificado | ev `test_render_acceptance_criterion` (L1, C1, M1, L5, M5): Poppler 0 px y PDFium 0 px fuera de las zonas, original → protegido y protegido → restaurado · macOS (§4.3) | — |

## 4. Resultados por ambiente

### 4.1 macOS

macOS 27.0.1 arm64, Python 3.13.16, PostgreSQL 17.11 (contenedor descartable, bases `cce_t_*`) y `CCE_REQUIRE_POSTGRES=1`. Bibliotecas: pikepdf 10.18.1, pypdf 7.1.0, pypdfium2 5.15.2 y Poppler 26.10.0.

| Comprobación | Resultado |
|---|---|
| `ruff check .`, `ruff format --check`, `mypy` | pasan (113 archivos fuente) |
| `pytest -q -rs` (PostgreSQL + SQLite) | **1649 pasadas, 0 fallidas**, 16 omitidas: variantes SQLite de pruebas solo PostgreSQL, la espera de bloqueo de A08 en SQLite y la evidencia de A07 sin su variable. La corrida completa fue sobre `ed056bd`, con el mismo `src/`; `2c7809a` solo corrige una prueba de A06, repetida aparte |
| `pytest -m dist` | 10 pasadas |
| Importación (D-33, wheel instalada) | mediana en caliente 236,3 ms (≤ 300) |
| `scripts/check_doc_links.py` | 0 enlaces rotos |
| Renders (§4.3) | 0 px en los cinco casos |

### 4.2 Linux local (Docker)

`bash scripts/linux_docker_validate.sh`: Debian 12 aarch64, Python 3.13.18, SQLite 3.40.1 y Poppler 22.12.0 instalado en el contenedor.

| Corrida | Resultado |
|---|---|
| `e174b0d` (mismo `src/` que el final), PostgreSQL 15 | 1640 pasadas, 0 fallidas |
| `e174b0d`, PostgreSQL 16, 17 y 18 | 1639–1638 pasadas; 1 o 2 fallos de una **prueba heredada de A06**: buscaba el carácter `5` en la fila de `engine_state` y dependía de la marca de tiempo. Corregida en `2c7809a` para comparar el nivel exacto |
| `e174b0d`, `pytest -m dist` | 3 fallos: la prueba esperaba hasta la migración `0005`; corregido en `0c763eb` |
| **`2c7809a`**, PostgreSQL 17 | **1640 pasadas, 0 fallidas**, 25 omitidas: las de macOS más el aislamiento Docker dentro del contenedor y un caso de permisos que `root` elude |
| **`2c7809a`**, cotas mínimas declaradas | **1640 pasadas, 0 fallidas** |
| **`2c7809a`**, distribución | 10 pasadas; auditoría de la wheel y del sdist sin hallazgos (`experiments/` excluido; fuente candidata y migración `0006` incluidas) |
| Importación (D-33) | mediana en caliente 188,2 ms |
| Renders con Poppler 22.12 y PDFium | 0 px en los cinco casos (como en macOS) |

### 4.3 Renders: criterio visual [V]

`tests/test_a08_evidence.py`, PDFium (`pypdfium2` 5.15.2) y Poppler (`pdftoppm`) a 150 ppp en gris. Las zonas salen del layout autenticado, dilatadas 3 px.

| Caso | Original → protegido (Poppler / PDFium / PDFium sin explicar) | Protegido → restaurado | Zonas |
|---|---|---|---|
| L1 | 0 / 0 / 0 | 0 / 0 / 0 | 4 × `Z1`, 1 × `Z2` |
| C1 (Type0/CID) | 0 / 0 / 0 | 0 / 0 / 0 | 4 × `Z1`, 1 × `Z2` |
| M1 | 0 / 0 / 0 | 0 / 0 / 0 | 4 × `Z1`, 1 × `Z2` |
| L5 | 0 / 0 / 0 | 0 / 0 / 0 | 3 × `Z1`, 4 × `Z2` (RZ) |
| M5 | 0 / 0 / 0 | 0 / 0 / 0 | 3 × `Z1`, 4 × `Z2` (RZ) |

Antes de E-A08-27, la misma medición dio en PDFium 262 px (M1), 1281 px (L5) y 1429 px (M5) fuera de zona. Los de L5 incluían 36 px que **no** se explicaban con el criterio [V]: el trazo de la «l» de «Llegó» se movía 2 px.

Con la extensión completa de los operadores, la heurística desaparece (Q-A08-14). El criterio [V] queda implementado en la prueba, pero no hizo falta usarlo.

### 4.4 Robustez frente al token aleatorio

El ancho del marcador depende del token aleatorio de A06, así que los planes, factores y bytes varían entre ejecuciones. Bucles puros de ciclo completo:

- **corpus:** 60 a 80 ejecuciones por caso;
- **sintético:** 200 ejecuciones del documento de `test_a08_access`.

Con ellos se detectaron y corrigieron defectos antes de entregar (§6). Resultado final con el código entregado, sin fallos:

- 80 ciclos completos de cada caso positivo, con C5 rechazado 80 de 80;
- 200 ciclos del sintético.

## 5. Matriz de soporte exacta

La del [contrato §5](pdf-protection.md#5-matriz-pdf-de-la-etapa-3-d3-c6) (análisis PA-01 a 18, protección PB-01 a 10, clases eliminadas). Resumen:

- **Transforma:** texto horizontal de izquierda a derecha en el flujo de la página, con fuentes simples o Type0 `Identity-H` y `ToUnicode` (incluido el CID subconjuntado de C1); estado de texto completo; `Tr` 0, 1 y 2; selecciones de una línea, partidas entre operadores u objetos de texto, una o varias por línea; `/ActualText` idéntico a sus glifos fuera de la selección y de la región `Z2`; tablas con rejilla (RP3), rectángulos detrás del texto (RP5) y fondos blancos (RP7).
- **Rechaza (documento entero):**
  - imágenes, curvas, sombreados y patrones (`uncovered_content`);
  - selecciones de varias líneas, en Form XObjects, de derecha a izquierda, que cortan ligaduras o que intersecan `/ActualText`;
  - `Z2` que tendría que mover `/ActualText` (**C5**), otro MCID, un enlace o un objeto pintado;
  - sustitutos que no caben;
  - los rechazos de A04 (cifrado, firmas, formularios, capas, adjuntos, acciones, actualizaciones incrementales, Type3, texto oculto).

## 6. Defectos encontrados y corregidos durante la etapa 3

| # | Defecto | Corrección | Detectado por |
|---|---|---|---|
| 1 | Una `:` con kerning negativo empieza dentro de la caja del valor y la regla «siguiente glifo» la ignoraba: `Z1` llegaba hasta el final de la línea | El siguiente glifo es el primero cuyo centro queda a la derecha del centro del último glifo del valor (planificador y verificador) | VZ del verificador (L1) |
| 2 | Dos selecciones `Z1` en una línea compartían bloque, y la extracción leía `[[A]][[B]], correo …` (2 de cada 60 ejecuciones) | Un bloque por selección `Z1` (E-A08-26) | VO1 en el bucle sintético |
| 3 | Un glifo a caballo del límite de `Z2` se desplazaba y dejaba píxeles fuera de la máscara (L1) | Pertenencia a la zona = centro dentro **y** fin dentro (E-A08-25) | renders (Poppler 10 px) |
| 4 | La tinta de una cursiva («J» de `Joaquín`, M5) empieza antes del origen | Zona extendida a la izquierda hasta la tinta | renders (Poppler 3 px) |
| 5 | Heurística de raster de PDFium al cortar un operador (2 px en L5) | Extensión completa de los operadores (E-A08-27) | renders (PDFium) |
| 6 | Falsos positivos de residuos aleatorios: «bea» dentro de un token, de datos comprimidos o del `/ID` hexadecimal | Atribución al sustituto y barrido sintáctico con flujos y cadenas hexadecimales enmascarados (E-A08-30) | bucles de robustez |
| 7 | Alineación de dos intérpretes: ancho natural con `Tz`, avance de los desplazados sin `Tc`/`Tw` y CTM del bloque cuando la selección empieza en el primer código | Corregido en el verificador y en el restaurador | controles sintéticos (CS) |
| 8 | **Bloqueo informado por el usuario sobre `cd51b91`:** un valor seleccionado de 1–2 caracteres desaparecía del texto visible, pero el verificador aprobaba el derivado aunque el valor quedara en el `/URI` de un enlace, porque varias comprobaciones de `_residues` filtraban por longitud | Superficies de texto decodificado sin longitud mínima; superficies binarias con el mínimo de 3 (E-A08-31) | pure `test_short_value_left_on_a_text_surface_is_a_residue`, `test_one_character_value_in_a_link_is_a_residue` y `test_verifier_finds_a_short_value_copied_into_a_content_stream`; acc `test_worker_refuses_a_short_value_left_in_a_link_and_publishes_nothing` (`residue_detected`, sin artefactos publicados ni vínculo) y el control limpio `test_a_short_value_is_protected_and_restored_when_no_copy_remains` · PG+SQ |

## 7. Limitaciones efectivas

Son las del [contrato §15](pdf-protection.md#15-limitaciones-declaradas). Destacan:

1. **Cobertura limitada.** Los documentos con imágenes (logotipos, fotografías), curvas o sombreados **no se protegen** (`uncovered_content`); P-A08-03 sigue pendiente.
2. **Word y Google Docs sin verificar.** La familia `C` falla con valores cortos cuando hay ligaduras con `/ActualText` en la región `Z2` (C5, Q-A08-18).
3. **Formas no admitidas.** Sin selecciones de varias líneas, en Form XObjects, de derecha a izquierda ni verticales.
4. **Fallo cerrado en la restauración.** Puede fallar cerrada en una línea `Z2` sin sitio para el exceso RZ (`restoration_layout_unavailable`) o cuando un carácter no tiene código único en su fuente (`restoration_encoding_unavailable`).
5. **Falsos positivos posibles del barrido** (fallo cerrado): un valor corto que aparezca como subcadena en una cadena de texto conservada (fecha de `/Info`, `/URI`), y valores de 3 caracteres en flujos decodificados no textuales (por ejemplo, programas de fuente) o decodificados con otras fuentes. Las superficies binarias no se barren para valores de 1–2 caracteres (E-A08-31).
6. **Texto extraído con blancos añadidos.** El texto del derivado y del restaurado contiene espacios y saltos de línea añadidos por los trozos de extensión completa; el orden del texto con tinta es el del original.
7. **Información que revelan.** El layout revela la longitud del valor (puntos de código por tramo y avance), y los marcadores, su posición e igualdad (P-A08-07, A06 §6.4).
8. **Render sin control de runtime.** El render no se controla en ejecución: es evidencia de aceptación (§4.3), y la estructura, la geometría y los residuos se verifican antes de publicar.
9. **Formato de la salida.** Se pierden la linealización y la conformidad PDF/A o PDF/UA.

Ningún problema conocido impide cumplir una garantía de protección dentro del alcance. Las garantías son: ningún valor seleccionado en el derivado salvo las ocurrencias permitidas en su sitio, publicación atómica y restauración solo desde el derivado exacto autenticado. Los fallos conocidos son cerrados: no publican nada.

## 8. Commits

Sobre `b6f749d` (`git log b6f749d..HEAD`), con identidad Git local del repositorio. No se hizo push.

- `c739a0b` feat(a08): PDF protection and restoration as durable A05 jobs (Python API) in 0.7.0
- `5b6d205` test(a08): corpus, controls, durable flows, compatibility and render evidence on PostgreSQL and SQLite
- `e174b0d` docs(a08): implemented contract, decisions rev. 5, support matrix and permanent documentation for 0.7.0
- `0c763eb` test(a08): distribution checks ship migration 0006, the A08 modules and the candidate font data
- `ed056bd` style(a08): ruff format of the distribution test
- `2c7809a` test(a06): check the head schema level in engine_state exactly (the substring check depended on the timestamp)
- (este informe con los resultados) docs(a08): results per environment

## 9. Qué queda para la revisión del usuario

- Aceptar o no A08 con este alcance. Decisiones abiertas:
  - P-A08-03, la lista de imágenes declarada por el host, sin la cual los documentos con logotipo se rechazan;
  - el clonado de Form XObjects (P-A08-05);
  - las exportaciones de Word y Google Docs, que siguen sin verificar.
- GitHub Actions: no ejecutado. El push, la PR y el merge los hace el usuario.
- A09 queda fuera.
