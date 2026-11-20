# Informe de implementación de A09-1

> **Estado: A09-1 implementada; validación en GPU real PENDIENTE (bloqueada por la cuota de instancias G = 0 vCPU); aceptación pendiente del usuario.** Este informe separa tres niveles de evidencia: **simulación** (§2.1), **artefactos y servicios reales fuera de la GPU** (§2.2) y **GPU real** (§2.3, ninguna). Las correcciones de la revisión 1 (sobre `889e2fe`) están en §2.4 y las de la revisión 2 (sobre `de3e8e2`), en §2.5. La receta exacta de la validación pendiente está en §4. Contrato: [inference-deployment.md](inference-deployment.md); matriz: [especificación](features/A09-1-despliegue-de-inferencia-y-operacion.md); decisiones: [A09-decisions](decisions/A09-decisions.md).

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

Suite del controlador (`deploy/inference/controller/tests`, 79 pruebas): reglas puras con reloj controlado (`test_decide.py`, 20), controlador + store + backend falso con concurrencia (`test_controller.py`, 11), compuerta de readiness y despacho de la revisión 1 (`test_readiness_gating.py`, 7), extremo a extremo por HTTP real contra un `llama-server` falso que emula `/health` 503→200, `/v1/models`, `/props`, `/apply-template`, `/tokenize`, `response_format`, slots en cola, error `exceed_context_size_error` y `/metrics` (`test_e2e.py`, 7), evidencia de memoria y veredicto (`test_measure.py`, 17), secuencia de validación en GPU con herramientas simuladas (`test_gpu_validation.py`, 12) y scripts y CLI con un host simulado que arranca `run-server.sh` real con el binario falso (`test_scripts.py`, 5). El modelo es simulado; transporte, readiness, slots, admisión de contexto, timeouts y mapeo de errores son código real. Ninguna prueba usa AWS: la secuencia de validación corre con un `aws` falso (`tests/fake_aws.py`) que ejecuta los comandos SSM en local, los rechaza si el host simulado no está `running` (como el servicio real) y opera el host simulado; `tests/fake_measure.py` envuelve `measure.py` para inyectar una medición fallida o colgada.

| Ambiente | Comando | Resultado |
|---|---|---|
| macOS (Apple M1, Python 3.13, uv) | `cd deploy/inference/controller && uv run pytest -q -rs` | **78 aprobadas**, 1 omitida (guardia de inactividad: necesita `/proc/uptime`, no cuenta como aprobada) |
| Linux local (Docker `python:3.13-bookworm`, aarch64, Python 3.13.18) | `PYTHONPATH=src python3 -m pytest -q -rs -p no:cacheprovider tests` (copia de `deploy/inference` en el contenedor) | **79 aprobadas**, sin omisiones |
| macOS | `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` (estricto) en el controlador | sin hallazgos |
| macOS | `shellcheck -S warning` de todos los scripts de `deploy/inference` | sin hallazgos |

### 2.2 Artefactos y servicios reales (sin GPU)

- **Modelo** (solo metadatos, sin descarga): `HEAD` de la URL fijada → `x-repo-commit fec6ff7c0c86cf07f6e5ae0a1391c3a042f2017f`, `x-linked-size 19403683392`, `x-linked-etag f3ab00cc…aaab9` (coincide con el manifiesto del plan).
- **Runtime**: tag `b12329` = commit `0d69255c5205c3af9eb15d8a7fc027594848041f`, publicado 2026-11-12T22:22:59Z. Descargados los dos tarballs Linux x64 CUDA 13.5 (fuera del repositorio); SHA-256 y tamaños coinciden con los *digests* publicados. En Linux x64 local (Docker `ubuntu:24.04` `linux/amd64`, emulado): `install-runtime.sh` verificó e instaló; `llama-server --version` → `version: 0.9.0-dev (build 12329, commit 0d69255c5)`, `built with GNU 13.3.0 for Linux x86_64`; las flags usadas existen en ese binario. **Hallazgo:** el binario necesita `libgomp.so.1` (paquete `libgomp1`), ausente en Ubuntu base; el instalador ahora lo exige y `bootstrap.sh` lo instala si falta. `libcuda.so.1` lo aporta el driver (aviso sin driver). Un tarball alterado se rechaza (tamaño/hash).
- **No existe** imagen `ghcr.io/ggml-org/llama.cpp:server-cuda-b12329` (ni variantes `cuda12`/`cuda13`); de ahí E-A09-01.
- **AWS (solo lectura, perfil `rodrigo`, `us-east-1`)**: identidad válida; `g6.xlarge` ofrecida en las 5 AZ (L4 22 888 MiB, 4 vCPU, 16 GiB); **cuota «Running On-Demand G and VT instances» = 0 vCPU**; AMI DLAMI Ubuntu 24.04 actual `ami-0fcbed7f428844048` (20261119, raíz 75 GiB); `aws cloudformation validate-template` aceptó la plantilla (requiere `CAPABILITY_IAM`); precios de la API de precios (§4.2 del contrato); `describe-instances` sin instancias en ningún estado (no se creó ningún recurso).
- **Evidencia externa** (ZIP `cawbase_resultados_2026-11-13.zip`, abierto en un directorio temporal fuera del repositorio): `resumen.json` de `qwen3.6-27b@razona512+esquema` confirma perfil `razona_512` (`--reasoning on --reasoning-format deepseek --reasoning-budget 512`), `ctx 8192`, `max_tokens 3072`, `timeout_caso 180`, build `cuda-13.5`, `+19123 MiB` de VRAM, 43/43/43, mediana 7,60 s, ≈ 73,5 tok/s. Los metadatos de cada repetición muestran temperatura 0 y `system_sha256 e0720bce…f866d`, igual al SHA-256 de `prompt_sistema.txt`. El esquema se aplicó **por solicitud** con `response_format` `json_schema` `strict` mediante `proxy_esquema.py` (el informe registra que `--json-schema-file` devolvía HTTP 400 con la plantilla de Qwen en `b12329`). El log del servidor registra `n_slots = 1, n_ctx_slot = 8192, kv_unified = 'false'` y una carga de ≈ 8,3 s desde disco local en Windows. **Reproducción desde las salidas crudas:** `benchmark.py evaluar` sin cambios sobre `evaluacion-r{1,2,3}-i1.jsonl` da **43, 43 y 43** casos estrictos, validez 1,0 y F1 de entidades 0,9358; el archivo de métricas es idéntico al publicado salvo redondeo de punto flotante (`macro_f1_categoria` 0,916 frente a 0,9159999…, `latencia_media_segundos`). Tamaños de la carga de referencia: prompt ≤ 1764 tokens (mediana 1735), salida ≤ 652 (mediana 558), total ≤ 2399; `finish_reason` siempre `stop`.

### 2.3 GPU real

**Ninguna.** No se levantó ninguna instancia ni se ejecutó el modelo de referencia: la cuota de instancias G es 0 vCPU (Q-A09-01) y no hay autorización de gasto (Q-A09-02). El Mac de desarrollo (M1, 16 GiB) no puede alojar los 19,4 GB del GGUF. La «prueba real de inferencia estructurada» de A09-1 queda **pendiente** (§4).

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

## 3. Trazabilidad AC-A09-1

| AC | Prueba / evidencia | Nivel | Resultado |
|---|---|---|---|
| 01 | metadatos HF; `test_install_model_verifies_hash_size_and_is_atomic` | A, S | cumplido en S/A; descarga real de 19,4 GB **pendiente** (G) |
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
| 15 | `validate-template`; `test_cloudformation_template_is_generated_from_the_sources` | A | plantilla válida; **stack real pendiente** (G) |
| 16 | `onprem/install.sh` revisado con shellcheck; mismo runtime/modelo/unidad | D | **pendiente** en hardware |
| 17 | `test_idle_guard_dry_run` | S (Linux) | cumplido en Linux; omitida en macOS |
| 18 | herramientas probadas contra el servidor falso; memoria en el host: `test_measure.py` | S | herramientas listas; mediciones **pendientes** (G) |
| 19 | — | — | **pendiente** (G) |
| 20 | contrato §4.1–§4.2; API de precios | D, A | cumplido |
| 21 | parada protegida probada en simulación (`test_gpu_validation.py`); no se levantó GPU; `describe-instances` vacío | S | cumplido en S; **no aplica todavía** en G |
| 22 | §5 | S | ver §5 |
| 23 | `test_measure.py` (17): host y muestras registrados; `unavailable` sin cifras; veredicto `incomplete`; revisión 2: corridas esperadas por perfil (ausente ⇒ `incomplete`, fallida ⇒ `fail` registrado) | S | cumplido en S; muestreo en la L4 **pendiente** (G) |
| 24 | `test_gpu_validation.py` (12) con `aws` simulado que rechaza Run Command en una instancia detenida; revisión 2: restitución verificada antes de la parada, fallo de restitución (EC2 detenida, perfil `not_restored`, no validada), interrupción durante la campaña | S | cumplido en S; corrida real **pendiente** (G) |

## 4. Validación pendiente en GPU real

Requiere decisiones del usuario: aumento de cuota (Q-A09-01), autorización de gasto (Q-A09-02) y red (Q-A09-03). Costo estimado: preparación (primer arranque con descarga de 19,4 GB) ≈ 0,5 h y validación 1,5–2,5 h de `g6.xlarge` (≈ 2,4 USD en total), más EBS mientras exista el stack (≈ 14,6 USD/mes). Usar siempre `--profile rodrigo`, nunca `fede`. Todas las salidas van a un directorio **fuera del repositorio** (`$EV`). Herramientas del operador: AWS CLI v2, `session-manager-plugin`, `uv` y Python ≥ 3.13.

**A. Preparación (una sola vez; no es la validación).** Termina con la instancia **detenida**.

```bash
export AWS_PROFILE=rodrigo AWS_REGION=us-east-1 EV=$HOME/cce-a09-1-evidence; mkdir -p "$EV"
# A0. cuota (decisión del usuario; puede tardar)
aws service-quotas request-service-quota-increase --service-code ec2 --quota-code L-DB2E81BA --desired-value 8
# A1. API key en SSM (SecureString) y copia local 0600 para el controlador
umask 077; openssl rand -hex 32 > "$EV/api-key"
aws ssm put-parameter --name /cce/inference/api-key --type SecureString --value "file://$EV/api-key"
# A2. AMI fijada (registrar el id usado)
AMI=$(aws ssm get-parameter --name /aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id --query Parameter.Value --output text)
echo "$AMI" > "$EV/ami-id"
# A3. stack (VpcId, SubnetId, AZ y ClientCidr/ClientSecurityGroupId según Q-A09-03)
python3 deploy/inference/aws/build_template.py --check
aws cloudformation deploy --stack-name cce-inference-a09 --template-file deploy/inference/aws/cloudformation.yaml \
  --capabilities CAPABILITY_IAM --parameter-overrides VpcId=vpc-… SubnetId=subnet-… AvailabilityZone=us-east-1a \
  AssignPublicIp=true ClientCidr=10.0.0.0/16 ImageId="$AMI" ServerProfile=reference-8k-1slot
aws cloudformation describe-stacks --stack-name cce-inference-a09 --query 'Stacks[0].Outputs' > "$EV/stack-outputs.json"
IID=$(aws cloudformation describe-stacks --stack-name cce-inference-a09 \
  --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text)
# A4. primer arranque: bootstrap (descarga y SHA-256 de 19,4 GB); esperar «bootstrap done» y el servidor activo
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

1. Verificación previa sin efectos (`preflight.json`): instancia `stopped`, estado del controlador nuevo, evidencia fuera del repositorio, perfiles definidos (con sus slots) e intérprete del controlador (también para los procesos hijos).
2. Ciclo disparado por trabajo: túnel SSM (`tunnel.log`), `serve` (`controller.log`) y un worker (`worker.log`); encola un trabajo sintético (`job.json`) y exige el arranque por demanda, `running`, la readiness verificada por el canario real y el trabajo `succeeded` con salida estructurada (`job-cycle.json` con la línea de tiempo envío → pedido de arranque → `running` observado → `ready` → fin; `events.json`, `job-result.json`). Es la **prueba real de inferencia estructurada** (AC-A09-1-19) y la primera medición del **arranque en frío** desde EBS.
3. Hechos del host y línea de tiempo del arranque (`host-facts.json` con GPU, driver, CUDA, AMI, runtime y modelo; `boot-timings.json` con arranque del kernel, inicio de la unidad, «loading model», «model loaded» y «listening»). Junto con `job-cycle.json` separan arranque EC2, arranque del SO, carga de pesos y tiempo hasta readiness.
4. Campaña con la instancia disponible, en el orden de `--profiles` (por defecto `reference-8k-1slot`, `candidate-16k-1slot`, `candidate-16k-2slot`): `load-P.json` (cambio de perfil y reinicio; memoria en el host), `limits-P.json`, `run-P-c1.json`, `run-P-c2.json` con dos slots, `run-P-overload.json` en el primero (3 a la vez sobre 1 slot: cola), `parity-P/` en el primero, y `verdict-P.json` sobre todos ellos (una medición fallida queda como `status: failed`, nunca se omite). Se detiene en el primer perfil que no pasa y lo registra como escalamiento (D-A09-05).
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
| Suite del controlador, mypy estricto, ruff y shellcheck de `deploy/inference` | §2.1 (macOS 78 + 1 omitida; Linux 79) |

No se ejecutaron `pytest -m dist` ni las validaciones Linux en Docker del motor (`linux_docker_validate.sh`, `sqlite_floor_validate.sh`): el paquete no cambió. GitHub Actions no se ejecutó.

## 6. Limitaciones

- Sin evidencia en GPU: tiempos de arranque, carga de pesos, VRAM/RAM, latencia, throughput, paridad y driver están pendientes; el predeterminado `reference-8k-1slot` es provisional (P-A09-04).
- Riesgos a medir en la L4: margen de VRAM (19,4 GB de pesos frente a 22 888 MiB) y 16 GiB de RAM del host frente a 18 GiB de archivo con `mmap` más la caché de prompts de `llama-server`.
- `bootstrap.sh`, la plantilla y `onprem/install.sh` se validaron estáticamente (shellcheck, `validate-template`, ejecución parcial en contenedores); no se ejecutaron completos en un host real.
- El estado del controlador y el ejecutor de prueba comparten un SQLite local; B01 debe ofrecer la misma serialización entre la decisión de apagado y el despacho.
- La paridad depende del ZIP externo y sus datos de evaluación (fuera de Git).
- El paquete del host en el *user data* ocupa 15 204 bytes en base64 (límite de EC2: 16 384 bytes con el envoltorio); queda poco margen para agregar archivos al paquete.
- El perfil original se restituye en `server.env` sin reiniciar el servidor (`--no-restart`): rige desde el próximo arranque; hasta la parada, el proceso en marcha conserva el último perfil medido. Si la limpieza no logra detener la instancia (salida 4), puede quedar encendida con ese perfil en memoria.
- `gpu-validation.py` detiene el controlador durante la campaña de perfiles para que no apague por inactividad bajo las mediciones (que no pasan por la cola); en ese tramo la parada la garantiza el propio script. El autoapagado real a los 60 min no se mide en la L4: se prueba con reloj controlado (AC-A09-1-10).
- Si la máquina del operador se apaga o pierde la red durante la corrida, la limpieza del script no puede ejecutarse; quedan como respaldo la guardia de inactividad de la instancia (120 min) y la comprobación independiente del final de la receta B.
