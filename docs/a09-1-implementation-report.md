# Informe de implementación de A09-1

> **Estado: A09-1 implementada; validación en GPU real PENDIENTE (bloqueada por la cuota de instancias G = 0 vCPU); aceptación pendiente del usuario.** Este informe separa tres niveles de evidencia: **simulación** (§2.1), **artefactos y servicios reales fuera de la GPU** (§2.2) y **GPU real** (§2.3, ninguna). La receta exacta de la validación pendiente está en §4. Contrato: [inference-deployment.md](inference-deployment.md); matriz: [especificación](features/A09-1-despliegue-de-inferencia-y-operacion.md); decisiones: [A09-decisions](decisions/A09-decisions.md).

## 1. Qué se entregó

| Pieza | Ubicación |
|---|---|
| Manifiesto fijado (modelo, runtime, perfil y hashes de la referencia externa, AWS) | `deploy/inference/manifest.json` |
| Instaladores verificados del runtime y del modelo; `run-server.sh`; perfiles; unidad systemd; guardia de inactividad | `deploy/inference/server/` |
| Plantilla CloudFormation generada, `bootstrap.sh` del primer arranque | `deploy/inference/aws/` |
| Instalación on-premise | `deploy/inference/onprem/install.sh` |
| Controlador `cce-inference-controller` 0.1.0 (reglas puras, store SQLite con lease, backends EC2/comando/falso, cliente del contrato, ejecutor de prueba, CLI) | `deploy/inference/controller/` |
| Script de operación `start`/`stop`/`status` | `deploy/inference/bin/cce-inference` |
| Medición, hechos del host, línea de tiempo de arranque y paridad | `deploy/inference/bench/` |

El paquete del motor no cambia (sin cambios en `src/`, versión, esquema, permisos ni dependencias). La única modificación fuera de `deploy/` es la configuración de ruff de la raíz (`per-file-ignores` para `deploy/*`).

## 2. Evidencia

### 2.1 Simulación (pruebas automáticas)

Suite del controlador (`deploy/inference/controller/tests`, 40 pruebas): reglas puras con reloj controlado (17), controlador + store + backend falso con concurrencia (11), extremo a extremo por HTTP real contra un `llama-server` falso que emula `/health` 503→200, `/v1/models`, `/props`, `/apply-template`, `/tokenize`, `response_format`, slots en cola, error `exceed_context_size_error` y `/metrics` (7), y scripts y CLI con un host simulado que arranca `run-server.sh` real con el binario falso (5). El modelo es simulado; transporte, readiness, slots, admisión de contexto, timeouts y mapeo de errores son código real.

| Ambiente | Comando | Resultado |
|---|---|---|
| macOS (Apple M1, Python 3.13, uv) | `cd deploy/inference/controller && uv run pytest -q -rs` | 39 aprobadas, 1 omitida (guardia de inactividad: necesita `/proc/uptime`, no cuenta como aprobada) |
| Linux local (Docker `python:3.13-bookworm`, aarch64, Python 3.13.18) | `PYTHONPATH=src python3 -m pytest -q -rs tests` | **40 aprobadas**, sin omisiones |
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

## 3. Trazabilidad AC-A09-1

| AC | Prueba / evidencia | Nivel | Resultado |
|---|---|---|---|
| 01 | metadatos HF; `test_install_model_verifies_hash_size_and_is_atomic` | A, S | cumplido en S/A; descarga real de 19,4 GB **pendiente** (G) |
| 02 | descarga y hashes de los tarballs; instalación y `--version` en Linux x64; chequeo de `libgomp` | A | cumplido; **driver pendiente** (G) |
| 03 | `test_run_server_command_line_is_the_reference_profile`; `test_full_cycle_over_http` (forma de la solicitud) | S | cumplido |
| 04 | re-evaluación 43/43/43 con el evaluador sin cambios; ZIP fuera de Git | A | cumplido |
| 05 | `test_readiness_is_required_running_instance_is_not_enough`, `test_wrong_api_key_is_not_ready`, `test_full_cycle_over_http` | S | cumplido |
| 06 | `test_concurrent_requests_and_controllers_coordinate_one_start` (20 envíos, 4 controladores: 1 arranque efectivo, 1 llamada) | S | cumplido |
| 07 | `test_submit_is_durable_and_never_touches_the_cloud`, `test_readiness_never_reached_stops_the_host_and_keeps_work` | S | cumplido |
| 08 | `test_readiness_timeout_stops_retries_with_backoff_then_blocks` (300/600/1200 s, bloqueo, desbloqueo manual); e2e de readiness nunca alcanzada | S | cumplido |
| 09 | `test_inference_timeout_is_retried_and_does_not_change_readiness` | S | cumplido |
| 10 | `test_idle_stop_exactly_after_sixty_continuous_minutes` (3599 s / 3600 s), `test_idle_clock_restarts_with_work_and_ignores_health_checks` (1000 sondeos), `test_full_cycle_with_sixty_minute_idle_stop` | S | cumplido |
| 11 | `test_stop_decision_and_claims_are_serialized` (carrera aleatoria), `test_arrival_during_shutdown_is_kept_and_restarts`, regla pura equivalente | S | cumplido |
| 12 | `test_controller_crash_and_restart_resume_the_cycle`, `test_restart_in_the_middle_of_a_stop` | S | cumplido |
| 13 | `test_manual_stop_drains_active_work_and_keeps_pending`, `test_status_reports_instance_readiness_pause_and_idle_timer`, `test_cli_start_stop_status_cycle_with_a_simulated_host` | S | cumplido |
| 14 | `test_input_over_the_admitted_context_is_rejected_explicitly`, `test_work_beyond_capacity_waits_durably_in_the_queue`, `test_two_slots_admit_two_in_flight` | S | cumplido en S; límites reales **pendientes** (G) |
| 15 | `validate-template`; `test_cloudformation_template_is_generated_from_the_sources` | A | plantilla válida; **stack real pendiente** (G) |
| 16 | `onprem/install.sh` revisado con shellcheck; mismo runtime/modelo/unidad | D | **pendiente** en hardware |
| 17 | `test_idle_guard_dry_run` | S (Linux) | cumplido en Linux; omitida en macOS |
| 18 | herramientas `measure.py`/`boot-timings.sh` probadas contra el servidor falso | — | **pendiente** (G) |
| 19 | — | — | **pendiente** (G) |
| 20 | contrato §4.1–§4.2; API de precios | D, A | cumplido |
| 21 | no se levantó GPU; `describe-instances` vacío | — | no aplica todavía |
| 22 | §5 | S | ver §5 |

## 4. Validación pendiente en GPU real

Requiere decisiones del usuario: aumento de cuota (Q-A09-01), autorización de gasto (Q-A09-02) y red (Q-A09-03). Costo estimado de la receta: 2–3 h de `g6.xlarge` (≈ 2,4 USD) más EBS mientras exista el stack (≈ 14,6 USD/mes). Usar siempre `--profile rodrigo`, nunca `fede`. Todas las salidas van a un directorio **fuera del repositorio** (`$EV`).

```bash
export AWS_PROFILE=rodrigo AWS_REGION=us-east-1 EV=$HOME/cce-a09-1-evidence; mkdir -p "$EV"
# 0. cuota (decisión del usuario; puede tardar)
aws service-quotas request-service-quota-increase --service-code ec2 --quota-code L-DB2E81BA --desired-value 8
# 1. API key en SSM (SecureString) y copia local 0600 para el controlador
umask 077; openssl rand -hex 32 > "$EV/api-key"
aws ssm put-parameter --name /cce/inference/api-key --type SecureString --value "file://$EV/api-key"
# 2. AMI fijada (registrar el id usado)
AMI=$(aws ssm get-parameter --name /aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id --query Parameter.Value --output text)
# 3. stack (VpcId, SubnetId, AZ y ClientCidr/ClientSecurityGroupId según Q-A09-03)
python3 deploy/inference/aws/build_template.py --check
aws cloudformation deploy --stack-name cce-inference-a09 --template-file deploy/inference/aws/cloudformation.yaml \
  --capabilities CAPABILITY_IAM --parameter-overrides VpcId=vpc-… SubnetId=subnet-… AvailabilityZone=us-east-1a \
  AssignPublicIp=true ClientCidr=10.0.0.0/16 ImageId="$AMI" ServerProfile=reference-8k-1slot
aws cloudformation describe-stacks --stack-name cce-inference-a09 --query 'Stacks[0].Outputs' > "$EV/stack-outputs.json"
IID=$(aws cloudformation describe-stacks --stack-name cce-inference-a09 \
  --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text)
# controller.toml: copiar deploy/inference/controller/controller.aws.example.toml a $EV y completar instance_id,
# base_url (http://127.0.0.1:18080 con el túnel del paso 5), api_key_file ($EV/api-key), db_path ($EV/state.db)
# 4. primer arranque: seguir bootstrap (descarga y SHA-256 de 19,4 GB) y registrar hechos del host
aws ssm start-session --target "$IID"   # en el host: tail -f /var/lib/cce-inference/bootstrap.log; luego:
#   sudo /opt/cce-inference/src/bench/host-facts.sh; journalctl -u cce-inference -b --no-pager | tail -50
# 5. endpoint privado hacia el operador por SSM (requiere session-manager-plugin)
aws ssm start-session --target "$IID" --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8080"],"localPortNumber":["18080"]}' &
# 6. prueba real de inferencia estructurada y límites (perfil de referencia)
deploy/inference/bin/cce-inference --config "$EV/controller.toml" probe --full      # base_url http://127.0.0.1:18080
python3 deploy/inference/bench/measure.py limits --base-url http://127.0.0.1:18080 --key-file "$EV/api-key" --out "$EV/limits-8k-1slot.json"
python3 deploy/inference/bench/measure.py run --base-url http://127.0.0.1:18080 --key-file "$EV/api-key" \
  --requests 20 --concurrency 1 --prompt-tokens 1800 --out "$EV/run-8k-1slot-c1.json"
python3 deploy/inference/bench/measure.py run --base-url http://127.0.0.1:18080 --key-file "$EV/api-key" \
  --requests 6 --concurrency 3 --prompt-tokens 1800 --out "$EV/run-8k-1slot-overload.json"   # cola: 1 slot, 3 a la vez
# 7. paridad con la referencia (ZIP fuera de Git)
deploy/inference/bench/parity.sh --pkg /ruta/cawbase_resultados --base-url http://127.0.0.1:18080 \
  --key-file "$EV/api-key" --out "$EV/parity-8k-1slot"
# 8. arranque en frío medido por separado: parar, arrancar y cronometrar desde el operador...
aws ec2 stop-instances --instance-ids "$IID"; aws ec2 wait instance-stopped --instance-ids "$IID"
date +%s.%N > "$EV/t-start"; aws ec2 start-instances --instance-ids "$IID"
aws ec2 wait instance-running --instance-ids "$IID"; date +%s.%N > "$EV/t-running"
#    ...y en el host: /opt/cce-inference/src/bench/boot-timings.sh > boot-timings.json (arranque del SO, carga de pesos)
#    readiness = primer `probe --full` exitoso tras reabrir el túnel (paso 5)
# 9. ciclo del controlador en real (cfg AWS con base_url del túnel o desde la VPC): start --wait, trabajo, stop --wait
deploy/inference/bin/cce-inference --config "$EV/controller.toml" stop --wait 900
# 10. solo si el paso 6 dejó margen de VRAM: repetir 6 con candidate-16k-1slot y luego candidate-16k-2slot
#     (cambiar CCE_PROFILE en /etc/cce-inference/server.env y `sudo systemctl restart cce-inference`)
# 11. al terminar, VERIFICAR que la instancia quedó detenida
aws ec2 describe-instances --instance-ids "$IID" --query 'Reservations[0].Instances[0].State.Name' --output text   # stopped
```

Criterios para elegir el predeterminado (fijados antes de medir): el perfil debe cargar con `--fit off` sin error de memoria, dejar ≥ 1024 MiB de VRAM libre en el pico medido, completar `limits` (prompt admitido con 3072 de salida) y la carga `run` sin errores ni `finish_reason = length`; entre los que cumplan, se prefiere el que dé más contexto por solicitud con un slot; dos slots solo si la latencia mediana con concurrencia 2 no supera el doble de la de un slot. Si `reference-8k-1slot` no cumple, se aplica D-A09-05 (elevar al usuario). La paridad es evidencia preliminar (A09-2 hace la evaluación formal): se informa la diferencia con 43/50 sin ajustar nada.

## 5. Checks del repositorio

| Check | Resultado |
|---|---|
| `uv run ruff check .` (raíz, incluye `deploy/`) | sin hallazgos |
| `uv run ruff format --check src tests examples scripts deploy` | sin cambios |
| `uv run mypy` (paquete del motor) | sin hallazgos (113 archivos) |
| `uv run pytest -q -rs` con PostgreSQL 17 descartable (`cce_t_*`) y `CCE_REQUIRE_POSTGRES=1` (macOS) | **1662 aprobadas**, 16 omitidas (variantes SQLite de pruebas solo PostgreSQL, generación opcional de evidencia de A07, versión de SQLite no pedida), 10 deseleccionadas (`dist`), 0 fallos, 15 min |
| `python scripts/check_doc_links.py` | `bad 0` |
| `python3 deploy/inference/aws/build_template.py --check` | al día |
| Suite del controlador, mypy estricto, ruff y shellcheck de `deploy/inference` | §2.1 (macOS 39 + 1 omitida; Linux 40) |

No se ejecutaron `pytest -m dist` ni las validaciones Linux en Docker del motor (`linux_docker_validate.sh`, `sqlite_floor_validate.sh`): el paquete no cambió. GitHub Actions no se ejecutó.

## 6. Limitaciones

- Sin evidencia en GPU: tiempos de arranque, carga de pesos, VRAM/RAM, latencia, throughput, paridad y driver están pendientes; el predeterminado `reference-8k-1slot` es provisional (P-A09-04).
- Riesgos a medir en la L4: margen de VRAM (19,4 GB de pesos frente a 22 888 MiB) y 16 GiB de RAM del host frente a 18 GiB de archivo con `mmap` más la caché de prompts de `llama-server`.
- `bootstrap.sh`, la plantilla y `onprem/install.sh` se validaron estáticamente (shellcheck, `validate-template`, ejecución parcial en contenedores); no se ejecutaron completos en un host real.
- El estado del controlador y el ejecutor de prueba comparten un SQLite local; B01 debe ofrecer la misma serialización entre la decisión de apagado y el despacho.
- La paridad depende del ZIP externo y sus datos de evaluación (fuera de Git).
