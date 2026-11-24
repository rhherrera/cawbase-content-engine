# Informe de implementación de A09-1

> **Estado: A09-1 implementada; objetivo AWS cambiado por el usuario a `g6e.xlarge` (NVIDIA L40S) + Qwen3.6-27B Q8_0 con todos los pesos en GPU (D-A09-08), PENDIENTE de validación real (cuota G/VT = 0 vCPU con la solicitud de aumento abierta; el despliegue y la corrida requieren autorización de gasto aparte); on-premise sigue con Q4_K_M; aceptación pendiente del usuario. Los pasos A y B de §4 pueden ejecutarse ahora con una solicitud versionada mergeada a `main` (§2.7), probado solo con AWS simulado.** Los datos de L4/Q4_K_M de este informe son **históricos**; el cambio de objetivo está en §2.6. Este informe separa tres niveles de evidencia: **simulación** (§2.1), **artefactos y servicios reales fuera de la GPU** (§2.2) y **GPU real** (§2.3, ninguna). Las correcciones de la revisión 1 (sobre `889e2fe`) están en §2.4 y las de la revisión 2 (sobre `de3e8e2`), en §2.5. La receta exacta de la validación pendiente está en §4. Contrato: [inference-deployment.md](inference-deployment.md); matriz: [especificación](features/A09-1-despliegue-de-inferencia-y-operacion.md); decisiones: [A09-decisions](decisions/A09-decisions.md).

## 1. Qué se entregó

| Pieza | Ubicación |
|---|---|
| Manifiesto fijado (modelo, runtime, perfil y hashes de la referencia externa, AWS) | `deploy/inference/manifest.json` |
| Instaladores verificados del runtime y del modelo; `run-server.sh`; perfiles; unidad systemd; guardia de inactividad | `deploy/inference/server/` |
| Plantilla CloudFormation generada, `bootstrap.sh` del primer arranque | `deploy/inference/aws/` |
| Instalación on-premise | `deploy/inference/onprem/install.sh` |
| Controlador `cce-inference-controller` 0.1.0 (reglas puras, store SQLite con lease, backends EC2/comando/falso, cliente del contrato, ejecutor de prueba, CLI) | `deploy/inference/controller/` |
| Script de operación `start`/`stop`/`status` | `deploy/inference/bin/cce-inference` |
| Medición (`measure.py`, con veredicto por perfil), muestreo de memoria en el host (`mem-sample.sh`), comando por SSM (`ssm-run.sh`), hechos del host, línea de tiempo de arranque y paridad | `deploy/inference/bench/` |
| Secuencia de validación en GPU con parada protegida (`gpu-validation.py`) y cambio de perfil en el host (`server/switch-profile.sh`) | `deploy/inference/bench/`, `deploy/inference/server/` |

El paquete del motor no cambia (sin cambios en `src/`, versión, esquema, permisos ni dependencias). La única modificación fuera de `deploy/` es la configuración de ruff de la raíz (`per-file-ignores` para `deploy/*`).

## 2. Evidencia

### 2.1 Simulación (pruebas automáticas)

Suite del controlador (`deploy/inference/controller/tests`, 99 pruebas): reglas puras con reloj controlado (`test_decide.py`, 20), controlador + store + backend falso con concurrencia (`test_controller.py`, 11), compuerta de readiness y despacho de la revisión 1 (`test_readiness_gating.py`, 7), extremo a extremo por HTTP real contra un `llama-server` falso que emula `/health` 503→200, `/v1/models`, `/props`, `/apply-template`, `/tokenize`, `response_format`, slots en cola, error `exceed_context_size_error` y `/metrics` (`test_e2e.py`, 7), evidencia de memoria y veredicto (`test_measure.py`, 17), secuencia de validación en GPU con herramientas simuladas (`test_gpu_validation.py`, 12) scripts y CLI con un host simulado que arranca `run-server.sh` real con el binario falso, para el destino AWS (Q8_0) y para una instalación on-premise Q4_K_M anterior a la selección (`test_scripts.py`, 6) y selección del modelo (`test_model_selection.py`, 17). El modelo es simulado; transporte, readiness, slots, admisión de contexto, timeouts y mapeo de errores son código real. Ninguna prueba usa AWS: la secuencia de validación corre con un `aws` falso (`tests/fake_aws.py`) que ejecuta los comandos SSM en local, los rechaza si el host simulado no está `running` (como el servicio real) y opera el host simulado; `tests/fake_measure.py` envuelve `measure.py` para inyectar una medición fallida o colgada.

| Ambiente | Comando | Resultado |
|---|---|---|
| macOS (Apple M1, Python 3.13, uv) | `cd deploy/inference/controller && uv run pytest -q -rs` | **98 aprobadas**, 1 omitida (guardia de inactividad: necesita `/proc/uptime`, no cuenta como aprobada) |
| Linux local (Docker `python:3.13-bookworm`, aarch64, Python 3.13.18) | `PYTHONPATH=src python3 -m pytest -q -rs -p no:cacheprovider tests` (copia de `deploy/inference` en el contenedor) | **99 aprobadas**, sin omisiones |
| macOS | `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` (estricto) en el controlador | sin hallazgos |
| macOS | `shellcheck -S warning` de todos los scripts de `deploy/inference` | sin hallazgos |

### 2.2 Artefactos y servicios reales (sin GPU)

- **Modelo** (solo metadatos, sin descarga): `HEAD` de la URL fijada → `x-repo-commit fec6ff7c0c86cf07f6e5ae0a1391c3a042f2017f`, `x-linked-size 19403683392`, `x-linked-etag f3ab00cc…aaab9` (coincide con el manifiesto del plan).
- **Runtime**: tag `b12329` = commit `0d69255c5205c3af9eb15d8a7fc027594848041f`, publicado 2026-11-12T22:22:59Z. Descargados los dos tarballs Linux x64 CUDA 13.5 (fuera del repositorio); SHA-256 y tamaños coinciden con los *digests* publicados. En Linux x64 local (Docker `ubuntu:24.04` `linux/amd64`, emulado): `install-runtime.sh` verificó e instaló; `llama-server --version` → `version: 0.9.0-dev (build 12329, commit 0d69255c5)`, `built with GNU 13.3.0 for Linux x86_64`; las flags usadas existen en ese binario. **Hallazgo:** el binario necesita `libgomp.so.1` (paquete `libgomp1`), ausente en Ubuntu base; el instalador ahora lo exige y `bootstrap.sh` lo instala si falta. `libcuda.so.1` lo aporta el driver (aviso sin driver). Un tarball alterado se rechaza (tamaño/hash).
- **No existe** imagen `ghcr.io/ggml-org/llama.cpp:server-cuda-b12329` (ni variantes `cuda12`/`cuda13`); de ahí E-A09-01.
- **AWS (solo lectura, perfil `rodrigo`, `us-east-1`)**: identidad válida; `g6.xlarge` ofrecida en las 5 AZ (L4 22 888 MiB, 4 vCPU, 16 GiB); **cuota «Running On-Demand G and VT instances» = 0 vCPU**; AMI DLAMI Ubuntu 24.04 actual `ami-0fcbed7f428844048` (20261119, raíz 75 GiB); `aws cloudformation validate-template` aceptó la plantilla (requiere `CAPABILITY_IAM`); precios de la API de precios (§4.2 del contrato); `describe-instances` sin instancias en ningún estado (no se creó ningún recurso).
- **Evidencia externa** (ZIP `cawbase_resultados_2026-11-13.zip`, abierto en un directorio temporal fuera del repositorio): `resumen.json` de `qwen3.6-27b@razona512+esquema` confirma perfil `razona_512` (`--reasoning on --reasoning-format deepseek --reasoning-budget 512`), `ctx 8192`, `max_tokens 3072`, `timeout_caso 180`, build `cuda-13.5`, `+19123 MiB` de VRAM, 43/43/43, mediana 7,60 s, ≈ 73,5 tok/s. Los metadatos de cada repetición muestran temperatura 0 y `system_sha256 e0720bce…f866d`, igual al SHA-256 de `prompt_sistema.txt`. El esquema se aplicó **por solicitud** con `response_format` `json_schema` `strict` mediante `proxy_esquema.py` (el informe registra que `--json-schema-file` devolvía HTTP 400 con la plantilla de Qwen en `b12329`). El log del servidor registra `n_slots = 1, n_ctx_slot = 8192, kv_unified = 'false'` y una carga de ≈ 8,3 s desde disco local en Windows. **Reproducción desde las salidas crudas:** `benchmark.py evaluar` sin cambios sobre `evaluacion-r{1,2,3}-i1.jsonl` da **43, 43 y 43** casos estrictos, validez 1,0 y F1 de entidades 0,9358; el archivo de métricas es idéntico al publicado salvo redondeo de punto flotante (`macro_f1_categoria` 0,916 frente a 0,9159999…, `latencia_media_segundos`). Tamaños de la carga de referencia: prompt ≤ 1764 tokens (mediana 1735), salida ≤ 652 (mediana 558), total ≤ 2399; `finish_reason` siempre `stop`.

### 2.3 GPU real

**Ninguna.** No se levantó ninguna instancia ni se ejecutó ningún modelo: la cuota de instancias G/VT es 0 vCPU (Q-A09-01, solicitud abierta) y no hay autorización de gasto (Q-A09-02). El Mac de desarrollo (M1, 16 GiB) no puede alojar los GGUF (19,4 GB Q4_K_M; 28,6 GB Q8_0). La «prueba real de inferencia estructurada» de A09-1 queda **pendiente** (§4).

### 2.4 Correcciones de la revisión 1 (sobre `889e2fe`)

Tres hallazgos del usuario. Cada defecto se **reprodujo antes de corregirlo**; las regresiones nuevas fallan sobre el código anterior y pasan ahora. Todo es evidencia **S** (simulación); lo que requiere la L4 está separado al final.

**1. Readiness y despacho.** Reproducción con un script descartable (fuera del repositorio) contra el código de `889e2fe`:

| Caso | Antes (`889e2fe`) | Ahora |
|---|---|---|
| `start` → `stop` → `start` mientras el modelo carga | `ready` y un trabajo reclamado **sin canario exitoso** (`apply_command` pasaba de `draining` a `ready`) | `starting` con nuevo plazo; despacho cerrado hasta un sondeo completo exitoso |
| Un sondeo fallido con EC2 `running` en `ready` | fase `ready` y reclamación **permitida** hasta el umbral de 3 | despacho cerrado desde el primer fallo (`failed_probes > 0`); el umbral solo decide volver a `starting` |
| Canario exitoso que termina 5 s después del plazo | `ready` y reclamación permitida (se evaluaba `ready` antes del plazo) | `readiness_timeout`: `stopping`, intento contado, sin despacho |

Cambios: `Readiness.verified` (solo un sondeo completo, alias + canario, verifica; el de vida nunca); `dispatch_allowed = ready ∧ failed_probes = 0`, usado también por la reclamación del ejecutor y por `status`; `probe_is_full` pide el sondeo completo en `starting` y en `ready` con fallos; en `starting` el plazo se evalúa primero con la hora tomada después del sondeo; `start` durante `draining` vuelve a `starting`; el controlador descarta un sondeo si la generación del estado cambió mientras corría (`stale_probe_discarded`); eventos `dispatch_closed` y `dispatch_reopened`. Un estado persistido con el nombre anterior (`unhealthy_count`) se lee como `failed_probes`. Regresiones: `test_readiness_gating.py` (reanudación durante la carga, reanudación durante el drenaje con trabajo activo que termina bajo su lease, cierre y reapertura solo con sondeo verificado, umbral con trabajo activo que sobrevive, sin autoapagado con trabajo activo, canario tardío, sondeo de un intento anterior) y tres reglas puras nuevas en `test_decide.py`; `test_manual_start_resumes_a_drain_and_starts_a_stopped_host` afirmaba el comportamiento defectuoso y se corrigió.

**2. Medición de VRAM/RAM.** El `Sampler` de `measure.py` leía `nvidia-smi` y `/proc/meminfo` de la máquina que ejecutaba `measure.py`; con la receta anterior era el operador, no EC2, y sin GPU devolvía picos 0. Ahora `mem-sample.sh` muestrea **en el host medido** (arrancado y detenido por SSM alrededor de la misma carga, o en local con `--memory-local`); el resultado registra host, cantidad de muestras y ventana, y es `unavailable` sin cifras cuando no hay muestras, la ventana no cubre la carga o el host no es el esperado (contrato §6.1). `measure.py verdict` aplica los criterios fijados y da `incomplete` (no `pass`) con memoria sin evidencia. Pruebas sin GPU: muestreo con `nvidia-smi` simulado (≥ 3 muestras, pico 17 000 de 23 034 MiB, host `i-0sim`) y sin `nvidia-smi` (`no_samples`, sin picos, veredicto `incomplete`), más los motivos de `unavailable` y el umbral de 1024 MiB.

**3. Receta de validación real.** La receta anterior anunciaba un ciclo disparado por trabajo pero solo ejecutaba `stop --wait`, y probaba perfiles después de detener la instancia. Ahora `bench/gpu-validation.py` (contrato §6.2) parte de `stopped`, encola un trabajo durable y exige el arranque por demanda, la readiness verificada y el trabajo exitoso; prueba los perfiles con la instancia disponible; detiene por el controlador y, **siempre**, verifica `stopped` (parada de respaldo si hace falta) o falla de forma visible (`CLEANUP FAILED`, salida 4). Probado con herramientas simuladas, sin recursos AWS: ciclo completo con dos perfiles (salida 0; arranque solo por el controlador, `start-instances` directo = 0; perfil original restituido), memoria sin muestras (salida 1, veredicto `incomplete`, escalamiento), fallo en la campaña (salida 1, parada de respaldo, perfil restituido), interrupción por SIGTERM, SIGINT y SIGHUP mientras se espera readiness (salida 130, instancia detenida por la limpieza), fallo de la limpieza (salida 4, `CLEANUP FAILED` en stderr, instancia `running` registrada), rechazo si la instancia no está detenida (nada tocado) y rechazo de evidencia dentro del repositorio.

**Verificado localmente** (S, macOS y Linux local): todo lo anterior. **Requiere la L4** (G): que el canario real verifique el modelo, que el muestreador lanzado por SSM sobreviva al fin del comando y produzca muestras de la GPU real, el túnel SSM real, la secuencia completa de `gpu-validation.py` contra EC2 y la parada verificada; ninguno de estos puntos se ejecutó.

### 2.5 Correcciones de la revisión 2 (sobre `de3e8e2`)

Cada defecto se reprodujo sobre `de3e8e2` antes de corregirlo; las regresiones nuevas fallan sobre `de3e8e2` y pasan con la corrección.

**1. Restitución del perfil.** `gpu-validation.py` ejecutaba `stop --wait` y recién después intentaba restituir el perfil original por Run Command, que no llega a una instancia detenida. Reproducción: con un `aws` falso que, como el servicio real, rechaza Run Command si la instancia no está `running`, la corrida completa de `de3e8e2` terminó con salida 0 y `validated`, pero la restitución falló (`InvalidInstanceId`) y el host quedó con el último perfil medido. Corrección (contrato §6.2, pasos 5 y «Siempre»): el perfil se restituye (`switch-profile.sh ORIGINAL --no-restart`) y se **verifica** (`--current`) con la instancia en `running`, antes de la parada del controlador; ante fallo o interrupción, la limpieza lo intenta antes de detener, solo si la instancia sigue `running`, y un fallo se registra sin impedir la parada ni la verificación de `stopped`. `final-state.json` registra por separado EC2 (`state`, `instance_stopped`, `fallback_stop`) y el perfil (`profile.status`, perfil leído, intentos); una restitución fallida impide declarar validada la corrida (salida 1). Regresiones en `test_gpu_validation.py`: ciclo completo (restitución verificada antes de la parada del controlador, un solo intento, sin parada de respaldo), restitución que falla (EC2 detenida por la limpieza, perfil `not_restored` con dos intentos, salida 1, nunca `validated`) e interrupción durante la campaña (perfil restituido y luego instancia detenida, salida 130).

**2. Integridad del veredicto.** `gpu-validation.py` solo pasaba a `measure.py verdict` los archivos de las mediciones con salida 0, y el veredicto no sabía qué corridas esperar. Reproducción: sobre `de3e8e2`, `measure.py verdict` con la carga de un perfil de dos slots, `limits` y una única corrida a concurrencia 1 dio `pass` (salida 0). Corrección (contrato §6.1 y §6.2): `verdict --slots N [--reference]` exige cada corrida esperada (`c1`; `c2` con dos slots; `overload` en la referencia), identificada por `tag` y a la concurrencia esperada, y que los slots del servidor coincidan con los de la definición del perfil; una corrida ausente o un archivo ilegible dan `incomplete`; una medición ejecutada y fallida queda registrada (`status: failed`, salida, error) y da `fail`, con el detalle en `failures`. `gpu-validation.py` toma los slots de `server/profiles/` (rechaza perfiles desconocidos sin efectos), deja siempre el archivo de cada medición y pasa todos al veredicto. Regresiones en `test_measure.py` (perfil de dos slots solo con `c1` ⇒ `incomplete`; referencia sin `overload` ⇒ `incomplete`; corrida, `limits` o `load` fallidos ⇒ `fail` registrado; concurrencia, slots o evidencia ilegible ⇒ nunca `pass`) y en `test_gpu_validation.py` (la corrida `c2` falla de verdad en la campaña: queda `status: failed`, el veredicto es `fail` con `runs = {c1: pass, c2: fail}` y el perfil se restituye antes de detener).

**Intérprete.** La receta ejecutaba `gpu-validation.py` con `python3` del sistema mientras `cce-inference` usaba `CCE_PYTHON`; ahora el script se ejecuta con `"$CCE_PYTHON"` (el entorno del controlador), fija ese mismo intérprete para `cce-inference` y `measure.py`, lo registra en `preflight.json` y rechaza sin efectos un `CCE_PYTHON` distinto o un Python < 3.13.

**Verificado localmente** (S, macOS y Linux local): todo lo anterior, con `aws` y mediciones simulados. **Requiere la L4** (G): la restitución por Run Command real con la instancia encendida y su verificación, y los veredictos con mediciones reales; no se ejecutó ninguno.

### 2.6 Cambio de objetivo AWS: `g6e.xlarge` + Q8_0 (decisión del usuario, D-A09-08)

Rama `fix/a09-1-g6e-q8-inference`, creada desde `origin/main` en `5a1943e` (merge del PR #8, verificado como ancestro de `origin/main` tras `git fetch`).

**Artefacto (A).** `ggml-org/Qwen3.6-27B-GGUF`, revisión `fec6ff7c0c86cf07f6e5ae0a1391c3a042f2017f` (la misma de Q4_K_M; `main` apunta a ella), `Qwen3.6-27B-Q8_0.gguf`, **28 595 762 752 bytes**, SHA-256 **`7cdc25ae00036f204a5748a63456f81ea11eec1225c2336064db157adc6f1c7c`**, URL inmutable `…/resolve/fec6ff7c…/Qwen3.6-27B-Q8_0.gguf`. Verificado el 2026-11-20 en tres fuentes coincidentes: el árbol de la revisión (`/api/models/…/tree/<rev>`: tamaño y `lfs.oid`), la API de blobs (`lfs.sha256`) y el HEAD de la URL inmutable (`x-repo-commit` = la revisión, `x-linked-size`, `x-linked-etag`). La licencia declarada en la ficha de esa revisión es `apache-2.0`. No se descargó el archivo: la descarga completa y su SHA-256 se verifican en el primer arranque (G).

**Qué cambió.**
- `manifest.json` v2: `models` (Q8_0 y Q4_K_M), `targets` (`aws` = Q8_0, `onprem` = Q4_K_M), `aws` con `g6e.xlarge` y el objetivo L4 como `historical`; el 43/50 queda rotulado como resultado **solo de Q4_K_M**.
- `install-model.sh --model ID` lee el manifiesto (sin valores fijos en el script), comprueba el espacio libre, elimina un archivo final que no verifica y conserva la instalación atómica y la verificación de tamaño y SHA-256; `--check` y `run-server.sh --check` (`ExecStartPre`) validan id, ruta, alias, tamaño y registro antes de arrancar.
- `bootstrap.sh` exige `--model`; la plantilla le pasa `qwen3.6-27b-q8_0` (de `targets.aws`) y escribe `CCE_MODEL_ID`, la ruta y el alias del manifiesto en `server.env`.
- Plantilla: `InstanceType` por defecto `g6e.xlarge`; volumen de pesos con mínimo **30 GiB** generado del tamaño exacto (antes 25 GiB, por debajo de los 26,63 GiB del GGUF); `mkfs.ext4 -m 0`.
- *User data*: con el manifiesto completo y las cabeceras de los scripts la plantilla superaba el límite (19 293 bytes); ahora el paquete lleva un manifiesto reducido al destino AWS y los scripts sin cabecera de documentación, y `build_template.py` calcula el peor caso con los valores más largos de los parámetros: **13 849 de 16 384 bytes**; plantilla de 21,5 KB (límite en línea de CloudFormation: 51 200 bytes).
- Alias real: `controller.aws.example.toml` usa `qwen3.6-27b-q8_0`; `measure.py --model` lo envía y exige en `/v1/models`, y el veredicto agrega el criterio `model`; `gpu-validation.py` rechaza otro alias del controlador y falla si el host sirve otro artefacto; `parity.sh` ya no tiene `qwen3.6-27b-q4km` fijo: exige `--model`, lo comprueba, registra el artefacto exacto y los hashes de las herramientas, y reporta el 43/50 de Q4_K_M aparte.
- On-premise: `onprem/install.sh` usa `targets.onprem` (Q4_K_M) por defecto y escribe `CCE_MODEL_ID`; un `server.env` anterior sin `CCE_MODEL_ID` resuelve a Q4_K_M y el registro de verificación anterior sigue siendo válido. Requisito nuevo: `python3` en el host.

**Regresiones** (`test_model_selection.py`, 17; además `test_scripts.py` y `test_gpu_validation.py` corren ahora con un manifiesto de prueba que conserva ids, archivos y alias reales y reemplaza tamaño, hash y URL por un archivo diminuto): manifiesto y plantilla seleccionan Q8_0 y `g6e.xlarge`; el volumen alcanza el artefacto; el paquete lleva la entrada Q8_0 idéntica y scripts válidos; el bootstrap exige `--model`; la unidad verifica antes de arrancar; el controlador y las herramientas usan el alias servido; discrepancias que fallan de forma explícita (archivo Q4_K_M con id Q8_0, alias distinto, id desconocido, sin registro, tamaño alterado; archivo que no es el fijado, sin espacio, alias no servido); instalación on-premise Q4_K_M anterior a la selección; instalador on-premise sin AWS. En la secuencia simulada: alias del controlador distinto ⇒ rechazo sin efectos; host que sirve otro artefacto ⇒ salida 1 e instancia detenida; ciclo completo con Q8_0 verificado en el host, en cada carga y en los veredictos. Todas fallan sobre `5a1943e` (9 fallos y 26 errores) y pasan con el cambio.

**Verificado localmente** (S y A): todo lo anterior, sin AWS ni GPU. **Requiere la L40S** (G): la descarga real de 28,6 GB y su SHA-256, la carga con `-ngl all --fit off` en la L40S, VRAM/RAM medidas en el host, tiempos de arranque y carga, la campaña de perfiles desde `reference-8k-1slot`, la calidad de Q8_0 con la metodología de la referencia y la parada verificada. Nada de esto se ejecutó.

### 2.7 Validación AWS automatizada con GitHub Actions (encargo del usuario, 2026-11-24)

Rama `ci/a09-1-aws-gpu-validation`, creada desde `origin/main` en `1c7385b` (merge del PR #9, verificado tras `git fetch`). Contrato, habilitación inicial y garantías: [validación AWS automatizada](a09-1-aws-validation-automation.md).

**Qué se entregó.** Workflow `a09-1-aws-gpu-validation` (jobs `detect` sin AWS, `validate` y `cleanup` con OIDC); diagnóstico `a09-1-oidc-diagnostic` (imprime el `sub` observado, nunca el token); orquestador `deploy/inference/ci/aws_validation.py` (Python estándar, AWS por la CLI); IaC de la habilitación inicial `deploy/inference/aws/github-validation-access.yaml` (rol OIDC, rol de ejecución de CloudFormation, límite de permisos del host, watchdog); directorio vigilado `deploy/inference/validation-requests/` (sin solicitudes) y ejemplo no vigilado `deploy/inference/ci/request.example.json`. Cambios en lo existente: `cloudformation.template.yaml` acepta `HostPermissionsBoundaryArn` (opcional; el *user data* no cambia: 13 849 de 16 384 bytes); `gpu-validation.py` guarda el journal del servidor y exige `offloaded N/N layers to GPU` en cada carga (cold start y campaña); `manifest.json` registra el precio por hora usado para el techo de gasto; `fake_llama_server.py` imprime la línea de offload y `fake_aws.py` simula la cuenta (CloudFormation, SSM, Scheduler, cuotas, red, AMI, S3).

**Evidencia S (AWS simulado).** `test_ci_validation.py` (44) y `test_gpu_validation.py` (18, cuatro nuevas de offload): detección sobre historia git real (merge normal, README, solicitud nueva, modificada, renombrada, dos a la vez, push sin commit previo); esquema y techo de gasto (15 solicitudes inválidas); claims OIDC sin token; corrida completa con `delete` (preflight, reserva, stack con los parámetros pedidos y el rol de ejecución, watchdog armado apenas existe la instancia, bootstrap, primera carga con todas las capas en GPU, instancia detenida, `gpu-validation.py` validado, borrado verificado de stack, instancia, volumen, API key y watchdog; el host simulado lee la API key de SSM como el real); `keep-stopped`, reintento del mismo id (salida 3, ningún `create-stack` nuevo) y otra solicitud que reutiliza el stack; 12 rechazos del preflight sin reservar ni crear nada (cuota, otra GPU encendida, AZ, ruta, IP pública, oferta, stack existente, red del stack de acceso, stack de acceso ausente, AMI, `session-manager-plugin`, SHA-256 del ZIP); paridad (ZIP verificado y predicciones crudas retenidas); fallo de validación con limpieza verificada; offload parcial en la primera carga; fallo de limpieza (salida 5, volumen restante, watchdog armado); SIGTERM durante el bootstrap (salida 130, limpieza verificada); runner muerto con SIGKILL y el watchdog que detiene la instancia por su cuenta; redacción de la API key y de credenciales (también en binarios). `tests/test_a09_aws_validation_ci.py` (22, raíz) fija el contrato estático del workflow, del diagnóstico y de la IaC (trust `aud` + `sub` exacto, patrón que rechaza comodines, `pull_request` y otras ramas; sin `UpdateStack`; límite de permisos; tipo y subred).

| Ambiente | Comando | Resultado |
|---|---|---|
| macOS (Apple M1, Python 3.13, uv) | `cd deploy/inference/controller && uv run pytest -q -rs` | **146 aprobadas**, 1 omitida (guardia de inactividad: necesita `/proc/uptime`) |
| Linux local (Docker `python:3.13-bookworm`, aarch64, Python 3.13.18) | `PYTHONPATH=src python3 -m pytest -q -rs -p no:cacheprovider tests` (copia de `.github` y `deploy`) | **147 aprobadas**, sin omisiones |
| macOS | `uv run pytest -q tests/test_a09_aws_validation_ci.py tests/test_ci_contract.py` (raíz) | 30 aprobadas |
| macOS | `actionlint` (con shellcheck) de los dos workflows nuevos; ruff, `ruff format --check` y `mypy --strict` del orquestador | sin hallazgos |

**Evidencia A (servicios reales, solo lectura, perfil `rodrigo`, 2026-11-24).** `aws cloudformation validate-template` aceptó `github-validation-access.yaml` (`CAPABILITY_NAMED_IAM`) y la plantilla regenerada del host (`CAPABILITY_IAM`, con `HostPermissionsBoundaryArn`); la cuenta ya tiene el proveedor OIDC de GitHub (`token.actions.githubusercontent.com`, se reutiliza); la cuota G/VT `L-DB2E81BA` en `us-east-1` se leyó en **4 vCPU** (antes 0; Q-A09-01). `aws-actions/configure-aws-credentials` v6.3.0 = commit `e1253824…`, comprobado con `git ls-remote --tags`. No se creó ningún recurso.

**No verificado (G y GitHub).** Ninguna ejecución en GitHub Actions ni contra AWS real; la habilitación inicial no se había aplicado (se aplicó después: §2.9); la semántica real de las políticas IAM (`validate-template` no la evalúa), el `sub` real del repositorio, EventBridge Scheduler, la propagación de las etiquetas del stack, el borrado real y `session-manager-plugin` en `ubuntu-latest` se comprueban en la primera corrida. La primera solicitud con gasto va en una PR aparte, cuando el usuario autorice la red y el gasto.

### 2.8 Revisión 1 de la validación automatizada (sobre `09c9429`)

Rama `ci/a09-1-aws-gpu-validation`; los commits previos se conservan.

**1. Limpieza con verificación de propiedad.** Reproducción (prueba descartable fuera del repositorio, AWS simulado, código de `09c9429`): una solicitud `create`+`delete` que nombra un stack preexistente con su instancia encendida → preflight rechazado (salida 2, nada reservado) → el job `cleanup` salió 0 y dejó el stack en `DELETE_COMPLETE`, la instancia detenida y el volumen borrado. Corrección (E-A09-28): token en la reserva, registro de propiedad por stack escrito antes de `CreateStack`, adopción explícita en `reuse`, limpieza solo de lo propio (por `StackId` y por la instancia concreta). Regresiones: `test_an_unclaimed_request_never_stops_or_deletes_anything` (el caso reproducido: `not_claimed`, sin `stop-instances` ni `delete-stack`), `test_a_claimed_request_preserves_a_stack_recreated_under_its_name`, `test_a_claimed_request_preserves_a_stack_adopted_by_another`, `test_retry_of_an_old_request_preserves_resources_adopted_later` (id1 crea y conserva, id2 adopta con `reuse`, la instancia queda en uso; el reintento de id1 da salida 3 y su limpieza, `keep-stopped` o `delete`, no detiene ni borra nada; el dueño actual sí puede) y `test_reuse_refuses_a_stack_without_a_matching_ownership_record`.

**2. Parada en AWS desde el aprovisionamiento.** El schedule pasa a ser un recurso del stack (E-A09-29): CloudFormation lo crea con la instancia, con el inicio fijado desde la reserva (límite de gasto sin cambios). Regresión `test_runner_lost_right_after_create_stack_the_instance_is_stopped_without_runner_or_cleanup`: el `aws` simulado acepta `CreateStack` y mata al runner con SIGKILL antes de devolverle el `StackId`; CloudFormation simulado termina la creación por su cuenta 1,5 s después; la instancia aparece encendida junto con su schedule (inicio ≤ reserva + 75 min) y el schedule la detiene sin `stop-instances` del runner ni del job `cleanup`; un job `cleanup` posterior reconoce el stack por la etiqueta `cce:claim-token` y lo borra por `StackId`.

**3. Habilitación automatizada.** `deploy/inference/ci/enable_access.py` (E-A09-30) con `tests/test_enable_access.py` (19, `gh` simulado en `tests/fake_gh.py`): plan sin cambios; aplicación verificada (todas las llamadas con el perfil `rodrigo`, aunque el entorno traiga una clave estática y `AWS_PROFILE=fede`) e idempotente (segunda vez: `no_changes`, la variable no se reescribe); diagnóstico disparado si nunca corrió; doce rechazos sin desplegar ni escribir la variable (perfil, repositorio, rama, `pull_request`, audiencia, plantilla con `job_workflow_ref`, log sin claims, run fallido, AZ, ruta, proveedor, oferta); trust distinto del observado ⇒ salida 1 sin escribir la variable; cambio de subred que actualiza y verifica.

| Ambiente | Comando | Resultado |
|---|---|---|
| macOS (Apple M1, Python 3.13, uv) | `cd deploy/inference/controller && uv run pytest -q -rs` | **171 aprobadas**, 1 omitida (guardia de inactividad: necesita `/proc/uptime`) |
| Linux local (Docker `python:3.13-bookworm`, aarch64, Python 3.13.18) | `PYTHONPATH=src python3 -m pytest -q -rs -p no:cacheprovider tests` (copia de `.github` y `deploy`) | **172 aprobadas**, sin omisiones |
| macOS | `uv run pytest -q tests/test_a09_aws_validation_ci.py tests/test_ci_contract.py` (raíz) | 33 aprobadas (patrón del `sub` idéntico en orquestador e IaC; schedule en la plantilla; permisos de los registros) |
| macOS | `actionlint` de los workflows nuevos; ruff; `mypy --strict` de `aws_validation.py` y `enable_access.py`; `build_template.py --check`; shellcheck | sin hallazgos |
| AWS, solo lectura (perfil `rodrigo`, 2026-11-24) | `aws cloudformation validate-template` de `github-validation-access.yaml` y de la plantilla del host regenerada | válidas (`CAPABILITY_NAMED_IAM`, `CAPABILITY_IAM`); *user data* sin cambios (13 849 de 16 384 bytes) |

**No verificado**: igual que §2.7, más el recurso `AWS::Scheduler::Schedule` real dentro del stack (formato de fechas, rollback) y la operación de habilitación contra AWS y GitHub reales. No se creó ningún recurso ni se escribió ninguna variable.

### 2.9 Habilitación inicial aplicada (2026-11-24)

Autorizada por el usuario (stack de acceso y variable del repositorio; la corrida con GPU se autoriza aparte). Ejecutada sobre `main` en `f7bf33d` con `deploy/inference/ci/enable_access.py` (perfil `rodrigo`, `us-east-1`, `gh` autenticado, Python 3.13): `plan` (salida 0, `create`), `apply` (salida 0, `enabled_and_verified`) y una repetición de `apply` (salida 0, `no_changes`). Detalle en el [§5.1 del contrato](a09-1-aws-validation-automation.md#51-habilitación-aplicada-2026-11-24).

- **Evidencia A (servicios reales, con cambios autorizados):** diagnóstico OIDC `45459844898` exitoso en `main`; `sub` observado `repo:rhherrera@1641310/cawbase-content-engine@1454429155:ref:refs/heads/main` (formato con identificadores inmutables, aceptado por el patrón de la IaC); proveedor OIDC de la cuenta reutilizado; red VPC por defecto `vpc-06b68c7fb3834181b`, subred `subnet-0ccdf328252d0fa4c` en `us-east-1a` (salida por IGW; única VPC de la cuenta); stack `cce-a09-github-access` en `CREATE_COMPLETE` con el trust exacto `aud` + `sub` comprobado aparte con `iam get-role`; variable `CCE_A09_AWS_ROLE_ARN` registrada y releída con `gh variable list`. Informes `enablement.json` fuera del repositorio.
- **Sigue sin verificar:** `AssumeRoleWithWebIdentity` desde el workflow de validación, la semántica real de las políticas IAM y todo lo que depende de la primera corrida (§2.7). Ninguna solicitud creada ni instancia encendida.

## 3. Trazabilidad AC-A09-1

| AC | Prueba / evidencia | Nivel | Resultado |
|---|---|---|---|
| 01 | metadatos HF (Q4_K_M; Q8_0 en §2.6); `test_install_model_verifies_hash_size_and_is_atomic`; `test_model_selection.py` | A, S | cumplido en S/A; descarga real de 28,6 GB (Q8_0) **pendiente** (G) |
| 02 | descarga y hashes de los tarballs; instalación y `--version` en Linux x64; chequeo de `libgomp` | A | cumplido; **driver pendiente** (G) |
| 03 | `test_run_server_command_line_is_the_reference_profile`; `test_full_cycle_over_http` (forma de la solicitud) | S | cumplido |
| 04 | re-evaluación 43/43/43 con el evaluador sin cambios; ZIP fuera de Git | A | cumplido |
| 05 | `test_readiness_is_required_running_instance_is_not_enough`, `test_wrong_api_key_is_not_ready`, `test_full_cycle_over_http`; revisión 1: `test_readiness_gating.py` (7), `test_entering_ready_needs_a_verified_probe_before_the_deadline`, `test_failed_probe_closes_dispatch_and_verified_probe_reopens` | S | cumplido en S; canario real **pendiente** (G) |
| 06 | `test_concurrent_requests_and_controllers_coordinate_one_start` (20 envíos, 4 controladores: 1 arranque efectivo, 1 llamada) | S | cumplido |
| 07 | `test_submit_is_durable_and_never_touches_the_cloud`, `test_readiness_never_reached_stops_the_host_and_keeps_work` | S | cumplido |
| 08 | `test_readiness_timeout_stops_retries_with_backoff_then_blocks` (300/600/1200 s, bloqueo, desbloqueo manual); e2e de readiness nunca alcanzada | S | cumplido |
| 09 | `test_inference_timeout_is_retried_and_does_not_change_readiness` | S | cumplido |
| 10 | `test_idle_stop_exactly_after_sixty_continuous_minutes` (3599 s / 3600 s), `test_idle_clock_restarts_with_work_and_ignores_health_checks` (1000 sondeos), `test_full_cycle_with_sixty_minute_idle_stop` | S | cumplido |
| 11 | `test_stop_decision_and_claims_are_serialized` (carrera aleatoria), `test_arrival_during_shutdown_is_kept_and_restarts`, regla pura equivalente | S | cumplido |
| 12 | `test_controller_crash_and_restart_resume_the_cycle`, `test_restart_in_the_middle_of_a_stop` | S | cumplido |
| 13 | `test_manual_stop_drains_active_work_and_keeps_pending`, `test_status_reports_instance_readiness_pause_and_idle_timer`, `test_cli_start_stop_status_cycle_with_a_simulated_host`, `test_resume_during_drain_with_active_work_revalidates_before_dispatch` | S | cumplido |
| 14 | `test_input_over_the_admitted_context_is_rejected_explicitly`, `test_work_beyond_capacity_waits_durably_in_the_queue`, `test_two_slots_admit_two_in_flight` | S | cumplido en S; límites reales **pendientes** (G) |
| 15 | `validate-template` (también la plantilla regenerada para `g6e.xlarge` + Q8_0, 2026-11-20: válida, `CAPABILITY_IAM`, `InstanceType` = `g6e.xlarge`, `WeightsVolumeGiB` = 40); `test_cloudformation_template_is_generated_from_the_sources`; `test_template_selects_g6e_and_q8_0_with_a_volume_that_fits_it` | A, S | plantilla válida; **stack real pendiente** (G) |
| 16 | `onprem/install.sh` revisado con shellcheck; mismo runtime/unidad; Q4_K_M por defecto: `test_existing_on_premise_q4_k_m_installation_keeps_working`, `test_on_premise_installer_defaults_to_q4_k_m_without_aws`, ciclo CLI `onprem-legacy-q4km` | D, S | cumplido en S; **pendiente** en hardware |
| 17 | `test_idle_guard_dry_run` | S (Linux) | cumplido en Linux; omitida en macOS |
| 18 | herramientas probadas contra el servidor falso; memoria en el host: `test_measure.py`; alias servido: `test_measurements_require_the_served_alias` | S | herramientas listas; mediciones en la L40S con Q8_0 **pendientes** (G) |
| 19 | — | — | **pendiente** (G), con Q8_0 |
| 20 | contrato §4.1–§4.2; API de precios | D, A | cumplido |
| 21 | parada protegida probada en simulación (`test_gpu_validation.py`); no se levantó GPU; `describe-instances` vacío | S | cumplido en S; **no aplica todavía** en G |
| 22 | §5 | S | ver §5 |
| 23 | `test_measure.py` (17): host y muestras registrados; `unavailable` sin cifras; veredicto `incomplete`; revisión 2: corridas esperadas por perfil (ausente ⇒ `incomplete`, fallida ⇒ `fail` registrado) | S | cumplido en S; muestreo en la L4 **pendiente** (G) |
| 24 | `test_gpu_validation.py` (12) con `aws` simulado que rechaza Run Command en una instancia detenida; revisión 2: restitución verificada antes de la parada, fallo de restitución (EC2 detenida, perfil `not_restored`, no validada), interrupción durante la campaña | S | cumplido en S; corrida real **pendiente** (G) |
| 25 | `test_model_selection.py` (17), `test_scripts.py` (ciclos `aws-q8_0` y `onprem-legacy-q4km`), `test_gpu_validation.py` (alias del controlador y artefacto del host); metadatos HF de Q8_0 (§2.6) | S, A | cumplido en S/A; descarga y carga reales **pendientes** (G) |
| 26 | `parity.sh` con `--model`, artefacto registrado y 43/50 aparte (revisión y prueba estática) | D | **pendiente** (G) |
| 27 | §2.7: `test_ci_validation.py` (detección, esquema, corrida completa, reintento sin gasto, reutilización, rechazos del preflight, paridad, offload); `test_gpu_validation.py` (offload); `test_a09_aws_validation_ci.py`; `actionlint`; `validate-template` | S, A | cumplido en S/A; ejecución en GitHub Actions y primera corrida real **pendientes** (G) |
| 28 | §2.7 y §2.8: trust, límite y alcance de la IaC (`test_a09_aws_validation_ci.py`); limpieza solo de lo propio, parada creada con la instancia y runner perdido tras `CreateStack`, fallo de validación ≠ fallo de limpieza (`test_ci_validation.py`); habilitación idempotente (`test_enable_access.py`) | S, A | cumplido en S/A; habilitación real y semántica IAM real **pendientes** (G) |

## 4. Validación pendiente en GPU real

**Automatizada (§2.7).** Los pasos A y B se pueden ejecutar mergeando a `main` una solicitud `deploy/inference/validation-requests/<id>.json`, tras la habilitación inicial ([validación AWS automatizada](a09-1-aws-validation-automation.md)); la receta manual de abajo sigue siendo válida y es la que el workflow automatiza.

Objetivo: `g6e.xlarge` (L40S) + Q8_0, todos los pesos en GPU (D-A09-08). Requiere: la aprobación de la cuota G/VT (Q-A09-01; la solicitud ya está abierta), la autorización de gasto (Q-A09-02) y la red (Q-A09-03). Costo estimado: preparación (primer arranque con descarga de 28,6 GB) ≈ 0,5 h y validación 1,5–2,5 h de `g6e.xlarge` a 1,861 USD/h (≈ 3,7–5,6 USD en total), más EBS mientras exista el stack (≈ 14,6 USD/mes con 80 + 40 GiB). Usar siempre `--profile rodrigo`, nunca `fede`. Todas las salidas van a un directorio **fuera del repositorio** (`$EV`). Herramientas del operador: AWS CLI v2, `session-manager-plugin`, `uv` y Python ≥ 3.13.

**A. Preparación (una sola vez; no es la validación).** Termina con la instancia **detenida**.

```bash
export AWS_PROFILE=rodrigo AWS_REGION=us-east-1 EV=$HOME/cce-a09-1-evidence; mkdir -p "$EV"
# A0. cuota G/VT: la solicitud ya está abierta; comprobar que la cuota aplicada sea >= 4 vCPU antes de seguir
aws service-quotas get-service-quota --service-code ec2 --quota-code L-DB2E81BA --query Quota.Value
# A1. API key en SSM (SecureString) y copia local 0600 para el controlador
umask 077; openssl rand -hex 32 > "$EV/api-key"
aws ssm put-parameter --name /cce/inference/api-key --type SecureString --value "file://$EV/api-key"
# A2. AMI fijada (registrar el id usado)
AMI=$(aws ssm get-parameter --name /aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id --query Parameter.Value --output text)
echo "$AMI" > "$EV/ami-id"
# A3. stack (VpcId, SubnetId, AZ y ClientCidr/ClientSecurityGroupId según Q-A09-03)
python3 deploy/inference/aws/build_template.py --check
python3 deploy/inference/aws/build_template.py --report   # g6e.xlarge, qwen3.6-27b-q8_0, pesos >= 30 GiB, user data < 16384
aws cloudformation deploy --stack-name cce-inference-a09 --template-file deploy/inference/aws/cloudformation.yaml \
  --capabilities CAPABILITY_IAM --parameter-overrides VpcId=vpc-… SubnetId=subnet-… AvailabilityZone=us-east-1a \
  AssignPublicIp=true ClientCidr=10.0.0.0/16 ImageId="$AMI" ServerProfile=reference-8k-1slot
aws cloudformation describe-stacks --stack-name cce-inference-a09 --query 'Stacks[0].Outputs' > "$EV/stack-outputs.json"
IID=$(aws cloudformation describe-stacks --stack-name cce-inference-a09 \
  --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text)
# A4. primer arranque: bootstrap (descarga y SHA-256 de 28,6 GB de Q8_0); esperar «units installed; server started»,
#     «model selected: qwen3.6-27b-q8_0» en el log y el servidor activo (la unidad verifica el artefacto antes de arrancar)
aws ssm start-session --target "$IID"   # en el host: tail -f /var/lib/cce-inference/bootstrap.log; systemctl is-active cce-inference
# A5. dejar la instancia DETENIDA: la validación parte de stopped
aws ec2 stop-instances --instance-ids "$IID"; aws ec2 wait instance-stopped --instance-ids "$IID"
# A6. operador: boto3 para el backend EC2 del controlador (entorno local, ignorado por Git)
uv sync --project deploy/inference/controller --extra aws
export CCE_PYTHON=$PWD/deploy/inference/controller/.venv/bin/python
```

**B. Validación (repetible; cada corrida con un estado del controlador nuevo).**

```bash
RUN=$EV/run-$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$RUN"
sed -e "s|^db_path = .*|db_path = \"$RUN/state.db\"|" \
    -e "s|^base_url = .*|base_url = \"http://127.0.0.1:18080\"|" \
    -e "s|^api_key_file = .*|api_key_file = \"$EV/api-key\"|" \
    -e "s|^instance_id = .*|instance_id = \"$IID\"|" \
    -e "s|^profile = .*|profile = \"$AWS_PROFILE\"|" \
    deploy/inference/controller/controller.aws.example.toml > "$RUN/controller.toml"
"$CCE_PYTHON" deploy/inference/bench/gpu-validation.py --config "$RUN/controller.toml" --instance-id "$IID" \
  --key-file "$EV/api-key" --evidence "$RUN" --parity-pkg /ruta/cawbase_resultados   # ZIP descomprimido, fuera de Git
echo "exit=$?"   # 0 validado · 1 falló/incompleta · 2 rechazada sin efectos · 130 interrumpida · 4 CLEANUP FAILED
# comprobación independiente al terminar, cualquiera haya sido la salida
aws ec2 describe-instances --instance-ids "$IID" --query 'Reservations[0].Instances[0].State.Name' --output text   # stopped
```

Qué hace `gpu-validation.py` (contrato §6.2) y qué deja en `$RUN`:

1. Verificación previa sin efectos (`preflight.json`, con el artefacto objetivo): instancia `stopped`, estado del controlador nuevo, evidencia fuera del repositorio, perfiles definidos (con sus slots), `model_alias` del controlador = alias de Q8_0 e intérprete del controlador (también para los procesos hijos).
2. Ciclo disparado por trabajo: túnel SSM (`tunnel.log`), `serve` (`controller.log`) y un worker (`worker.log`); encola un trabajo sintético (`job.json`) y exige el arranque por demanda, `running`, la readiness verificada por el canario real y el trabajo `succeeded` con salida estructurada (`job-cycle.json` con la línea de tiempo envío → pedido de arranque → `running` observado → `ready` → fin; `events.json`, `job-result.json`). Es la **prueba real de inferencia estructurada** (AC-A09-1-19) y la primera medición del **arranque en frío** desde EBS.
3. Hechos del host y línea de tiempo del arranque (`host-facts.json` con GPU, driver, CUDA, AMI, runtime, `CCE_MODEL_ID` y el registro de verificación del archivo servido, que debe coincidir con Q8_0 del manifiesto; `model-record.json`; `boot-timings.json` con arranque del kernel, inicio de la unidad, «loading model», «model loaded» y «listening»). Junto con `job-cycle.json` separan arranque EC2, arranque del SO, carga de pesos y tiempo hasta readiness.
4. Campaña con la instancia disponible, en el orden de `--profiles` (por defecto `reference-8k-1slot`, `candidate-16k-1slot`, `candidate-16k-2slot`): `load-P.json` (cambio de perfil y reinicio; memoria en el host), `limits-P.json`, `run-P-c1.json`, `run-P-c2.json` con dos slots, `run-P-overload.json` en el primero (3 a la vez sobre 1 slot: cola), `parity-P/` en el primero (calidad de Q8_0 con la metodología del ZIP y el artefacto registrado, aparte del 43/50 de Q4_K_M), y `verdict-P.json` sobre todos ellos (una medición fallida queda como `status: failed`, nunca se omite). Se detiene en el primer perfil que no pasa y lo registra como escalamiento (D-A09-05).
5. Con la instancia en `running`: perfil original restituido en `server.env` por Run Command y verificado (rige desde el próximo arranque).
6. Parada final por el controlador (`stop --wait`); siempre: restitución pendiente intentada antes de detener (si la instancia sigue `running`), instancia verificada `stopped`, `final-state.json` con el perfil y EC2 por separado, `summary.json`.

Criterios para elegir el predeterminado (fijados antes de medir; `measure.py verdict`, contrato §6.1): el perfil debe cargar con `--fit off` sin error de memoria, dejar ≥ 1024 MiB de VRAM libre en el pico **medido en el host** (memoria `unavailable` ⇒ veredicto `incomplete`, nunca `pass`), completar `limits` (prompt admitido con 3072 de salida) y **cada corrida esperada** (`c1`; `c2` con dos slots; `overload` en la referencia) sin errores ni `finish_reason = length` (corrida ausente ⇒ `incomplete`; fallida ⇒ `fail`); entre los que cumplan, se prefiere el que dé más contexto por solicitud con un slot; dos slots solo si la latencia mediana con concurrencia 2 no supera el doble de la de un slot. Si `reference-8k-1slot` no cumple, se aplica D-A09-05 (elevar al usuario). La paridad es evidencia preliminar (A09-2 hace la evaluación formal): se informa la diferencia con 43/50 sin ajustar nada.

## 5. Checks del repositorio

| Check | Resultado |
|---|---|
| `uv run ruff check .` (raíz, incluye `deploy/`) | sin hallazgos |
| `uv run ruff format --check src tests examples scripts deploy` | sin cambios |
| `uv run mypy` (paquete del motor) | sin hallazgos (113 archivos) |
| `uv run pytest -q -rs` con PostgreSQL 17 descartable (`cce_t_*`) y `CCE_REQUIRE_POSTGRES=1` (macOS) | **1662 aprobadas**, 16 omitidas (variantes SQLite de pruebas solo PostgreSQL, generación opcional de evidencia de A07, versión de SQLite no pedida), 10 deseleccionadas (`dist`), 0 fallos, 15 min |
| `python scripts/check_doc_links.py` | `bad 0` |
| `python3 deploy/inference/aws/build_template.py --check` | al día |
| Suite del controlador, mypy estricto, ruff y shellcheck de `deploy/inference` | §2.1 (macOS 98 + 1 omitida; Linux 99) |

Checks de la validación automatizada (§2.7): `actionlint .github/workflows/a09-1-*.yml` sin hallazgos (el aviso SC2035 de `ci.yml` es anterior y no se tocó); `tests/test_a09_aws_validation_ci.py` y `tests/test_ci_contract.py` 30 aprobadas; `python3 deploy/inference/ci/aws_validation.py check` sin errores.

No se ejecutaron `pytest -m dist` ni las validaciones Linux en Docker del motor (`linux_docker_validate.sh`, `sqlite_floor_validate.sh`): el paquete no cambió. GitHub Actions no se ejecutó.

## 6. Limitaciones

- Sin evidencia en GPU: tiempos de arranque, carga de pesos, VRAM/RAM, latencia, throughput, paridad y driver están pendientes; el predeterminado `reference-8k-1slot` es provisional (P-A09-04).
- Riesgos a medir en la L40S: margen de VRAM (28,6 GB de pesos Q8_0 más la caché KV frente a 48 GB) y 32 GiB de RAM del host frente a 26,6 GiB de archivo con `mmap` más la caché de prompts de `llama-server`. Los riesgos previstos para la L4 con Q4_K_M quedan como históricos.
- La calidad de Q8_0 no está medida: el 43/50 es de Q4_K_M (corrida externa) y no se traslada.
- Los scripts empaquetados en el *user data* no llevan su cabecera de documentación (límite de 16 KiB); la documentación está en el repositorio. El manifiesto del host contiene solo el modelo del destino AWS.
- On-premise necesita ahora `python3` en el host (la verificación previa al arranque lee el manifiesto).
- `bootstrap.sh`, la plantilla y `onprem/install.sh` se validaron estáticamente (shellcheck, `validate-template`, ejecución parcial en contenedores); no se ejecutaron completos en un host real.
- El estado del controlador y el ejecutor de prueba comparten un SQLite local; B01 debe ofrecer la misma serialización entre la decisión de apagado y el despacho.
- La paridad depende del ZIP externo y sus datos de evaluación (fuera de Git).
- El paquete del host en el *user data* ocupa 15 204 bytes en base64 (límite de EC2: 16 384 bytes con el envoltorio); queda poco margen para agregar archivos al paquete.
- El perfil original se restituye en `server.env` sin reiniciar el servidor (`--no-restart`): rige desde el próximo arranque; hasta la parada, el proceso en marcha conserva el último perfil medido. Si la limpieza no logra detener la instancia (salida 4), puede quedar encendida con ese perfil en memoria.
- `gpu-validation.py` detiene el controlador durante la campaña de perfiles para que no apague por inactividad bajo las mediciones (que no pasan por la cola); en ese tramo la parada la garantiza el propio script. El autoapagado real a los 60 min no se mide en la L4: se prueba con reloj controlado (AC-A09-1-10).
- Si la máquina del operador se apaga o pierde la red durante la corrida, la limpieza del script no puede ejecutarse; quedan como respaldo la guardia de inactividad de la instancia (120 min) y la comprobación independiente del final de la receta B. Con la validación automatizada (§2.7) se suman el job `cleanup` y el watchdog en AWS.
- La red de la validación automatizada es pública (subred por defecto con salida por IGW, §2.9): el host recibe IP pública para la salida, con el puerto 8080 cerrado salvo `client_*`; cambiarla exige repetir la habilitación.
- Validación automatizada: GitHub conserva un solo run pendiente por grupo de concurrencia (uno más antiguo se cancela sin gasto y hay que reejecutarlo); quien puede mergear a `main` dispara gasto acotado por la solicitud (Q-A09-06); entre la creación de la instancia y el armado del watchdog hay unos segundos cubiertos solo por el runner, el job `cleanup` y la guardia de la instancia; si el stack no se borra, el EBS sigue costando hasta corregirlo (el job `cleanup` falla visible).
