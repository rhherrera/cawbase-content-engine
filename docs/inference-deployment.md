# Despliegue de inferencia y operación (A09-1)

> **Estado: A09-1 IMPLEMENTADA (infraestructura y controlador; validación AWS automatizable por GitHub Actions, §6.3, probada solo con AWS simulado), pendiente de la validación en GPU real y de la aceptación del usuario.** Objetivo AWS vigente: **`g6e.xlarge` (NVIDIA L40S) con Qwen3.6-27B Q8_0, todos los pesos en GPU, pendiente de validación real** (D-A09-08); on-premise sigue con Q4_K_M. Probada con simulación en macOS y en Linux local (Docker); **no** probada en hardware con GPU (cuota de instancias G/VT en `us-east-1` en 0 vCPU con la solicitud de aumento abierta, ver [informe §4](a09-1-implementation-report.md#4-validación-pendiente-en-gpu-real)); **no** ejecutada en GitHub Actions. El paquete del motor (`0.7.0`) no cambia: no hay migración, permisos, dependencias ni API nuevas. Alcance y matriz: [especificación de A09-1](features/A09-1-despliegue-de-inferencia-y-operacion.md); decisiones: [A09-decisions](decisions/A09-decisions.md); plan: [A09](a09-plan.md).

Este documento conserva el contrato de despliegue y operación. Todo vive en [`deploy/inference/`](../deploy/inference/), fuera de la wheel y del sdist.

## 1. Piezas y fronteras

```text
 workers / API (host de CAWbase, B01)        controlador (fuera de la GPU y del núcleo)          host GPU (EC2 g6e.xlarge u on-premise)
 ─────────────────────────────────────       ─────────────────────────────────────────           ─────────────────────────────────────
 submit ─► trabajo durable "pending" ───────► demanda ─► decide ─► StartInstances / StopInstances ─► systemd: cce-inference.service
 claim (solo si fase = ready) ◄──────────── estado durable (SQLite): fase, lease, generación          llama-server b12329 + GGUF verificado
 POST /v1/chat/completions ───────────────────────────────────────────────────────────────────────► endpoint privado :8080 (API key)
                                             sondeo de readiness (health + alias + canario) ────────► /health, /v1/models, canario
```

- **El motor (`src/`) no cambia.** El adaptador real del contrato A03 es de A09-2; la conexión con la cola y los workers reales de CAWbase es de B01. A09-1 entrega el servicio, el despliegue, el controlador y un **ejecutor de prueba** con trabajos durables que demuestra el ciclo.
- **El controlador** (`deploy/inference/controller`, paquete `cce-inference-controller`) corre fuera del host GPU y fuera del núcleo Python del motor. Solo usa la biblioteca estándar; `boto3` únicamente para el backend EC2.
- **La API no espera el arranque en frío**: enviar trabajo solo escribe una fila durable `pending`; el controlador reacciona en su siguiente pasada.

## 2. Contrato del servicio de inferencia

Servidor: `llama-server` de llama.cpp `b12329` (binario oficial Linux x64 CUDA 13.5, ver [manifiesto](../deploy/inference/manifest.json)). El contrato HTTP es igual en AWS y on-premise; el **artefacto** depende del destino y lo fija el manifiesto (`models` y `targets`, única fuente de revisión, archivo, URL inmutable, tamaño exacto y SHA-256):

| Destino | Modelo (`CCE_MODEL_ID` = alias) | Archivo | Bytes | SHA-256 | Estado |
|---|---|---|---|---|---|
| AWS (`targets.aws`) | `qwen3.6-27b-q8_0` (Q8_0) | `Qwen3.6-27B-Q8_0.gguf` | 28 595 762 752 | `7cdc25ae00036f204a5748a63456f81ea11eec1225c2336064db157adc6f1c7c` | objetivo **pendiente de validación real** en `g6e.xlarge` (L40S), todos los pesos en GPU (D-A09-08) |
| On-premise (`targets.onprem`) | `qwen3.6-27b-q4km` (Q4_K_M) | `Qwen3.6-27B-Q4_K_M.gguf` | 19 403 683 392 | `f3ab00cc923590947877bad882e09ada67789f747f5d3cb3c178fdf91b9aaab9` | instalaciones existentes; artefacto del 43/50 histórico |

Ambos de `ggml-org/Qwen3.6-27B-GGUF`, revisión `fec6ff7c0c86cf07f6e5ae0a1391c3a042f2017f` (metadatos del Q8_0 verificados el 2026-11-20 en el árbol de la revisión, la API de blobs y el HEAD de la URL inmutable: `x-repo-commit`, `x-linked-etag`, `x-linked-size`). BF16, offload a CPU y otras cuantizaciones quedan fuera.

**Selección y verificación del artefacto.** `server.env` declara `CCE_MODEL_ID`, `CCE_MODEL_PATH` y `CCE_MODEL_ALIAS`. `install-model.sh --model ID` descarga (o copia, on-premise) a un nombre temporal, verifica tamaño y SHA-256 contra el manifiesto, renombra de forma atómica y escribe el registro de verificación; antes de descargar comprueba el espacio libre y elimina un archivo final que no verifica. **Antes de cada arranque** (`ExecStartPre=run-server.sh --check`, y otra vez en `run-server.sh` antes de ejecutar el servidor) el id debe existir en el manifiesto, la ruta y el alias deben ser los suyos y el archivo debe tener su tamaño y su registro; cualquier discrepancia falla de forma explícita y el servidor no arranca. Un `server.env` anterior a la selección (sin `CCE_MODEL_ID`) toma `targets.onprem` (Q4_K_M), que es lo que esas instalaciones tienen; el registro de verificación anterior sigue siendo válido. El host necesita `python3` para leer el manifiesto.

| Operación | Uso | Autenticación |
|---|---|---|
| `GET /health` | 503 mientras cargan los pesos; 200 `{"status":"ok"}` con el modelo cargado. **No decodifica** | no |
| `GET /v1/models` | lista el alias configurado | no (se envía la clave igual) |
| `GET /props` | `default_generation_settings.n_ctx` = **contexto efectivo por slot**; `total_slots` | sí |
| `POST /apply-template` + `POST /tokenize` | cuenta exacta de tokens del prompt con la plantilla del modelo | sí |
| `POST /v1/chat/completions` | inferencia estructurada (abajo) | sí (`Authorization: Bearer`) |
| `GET /metrics` | contadores Prometheus (`--metrics`), usados por la guardia de inactividad | sí |

**Solicitud estructurada** (reproduce la referencia externa `razona512+esquema`): `temperature: 0`, `max_tokens` del perfil (3072), mensajes del llamador y `response_format = {"type": "json_schema", "json_schema": {"name": …, "strict": true, "schema": …}}` **por solicitud**. En la corrida externa el esquema lo agregaba `proxy_esquema.py` a cada pedido porque `--json-schema-file` devolvía HTTP 400 con la plantilla de Qwen en `b12329`; aquí lo envía el cliente directamente, con la misma forma. La salida estructurada es `choices[0].message.content` (el razonamiento va a `reasoning_content` con `--reasoning-format deepseek`).

**Errores (conjunto cerrado del cliente, `llama.py`)**: `TransientInferenceError` (sin conexión, timeout, 429/5xx: reintentable), `ContextExceededError` (`input_exceeds_context`: no reintentable), `OutputInvalidError` (JSON inválido, truncado por `length`, forma inesperada, 4xx: no reintentable). Los mensajes no llevan contenido.

### 2.1 Readiness real

`listo` = `/health` 200 **y** el alias presente en `/v1/models` **y**, al arrancar, **un canario estructurado** (esquema `{"ok": boolean}`, `max_tokens` 1024) cuya salida parsea. Un 503 o un error transitorio del canario es `loading`; un 401 o una salida inválida es `unhealthy` (una clave equivocada **no** declara el servicio listo). Mientras está listo, la vida se comprueba solo con `/health`, que no decodifica: **los sondeos nunca cuentan como actividad**.

Solo un **sondeo completo** (alias + canario) produce una readiness **verificada** (`Readiness.verified`); el sondeo de vida puede responder `ready` pero nunca verifica. Reglas de despacho (corregidas tras la revisión 1, §3.1):

- Entrar en `ready` exige una readiness verificada **observada antes del plazo del intento**: el plazo se evalúa primero y con la hora tomada **después** de que el sondeo respondió, de modo que un canario que termina después del plazo no habilita el despacho de ese intento vencido.
- El **primer** sondeo fallido en `ready` **cierra el despacho**; solo un sondeo completo exitoso lo reabre (el controlador pide el sondeo completo mientras haya fallos). El umbral de fallos seguidos (`unhealthy_threshold`) solo decide cuándo volver a `starting`.
- Un sondeo tomado para un estado que cambió mientras corría (por ejemplo `stop` + `start` manuales que inician otro intento) se **descarta** (evento `stale_probe_discarded`).

### 2.2 Límites, contexto y cola

- **Contexto total y por slot**: `-c` es el contexto total; con `--parallel N` y KV no unificado, el contexto por slot es `c / N` (la referencia registra `n_slots = 1, n_ctx_slot = 8192, kv_unified = 'false'`).
- **Admisión explícita**: antes de enviar, el ejecutor cuenta los tokens del prompt y rechaza **sin reintentar** (`input_exceeds_context`, con `n_prompt_tokens`, `max_tokens` y `n_ctx_slot` en el detalle) toda entrada con `n_prompt + max_tokens > n_ctx_slot`. Si aun así el servidor rechaza (HTTP 400 `exceed_context_size_error`) se informa el mismo código. **Nunca se trunca.** Con el perfil de referencia (8192 por slot, 3072 de salida) el prompt admitido es ≤ 5120 tokens; la referencia externa usó ≤ 1764 tokens de prompt y ≤ 652 de salida (máximo total 2399).
- **Cola**: el ejecutor no despacha más trabajos simultáneos que slots (`max_in_flight` = `--parallel`); el excedente espera **durable** en la cola y no en la memoria del servidor (que también encola, como respaldo).
- **Timeout de inferencia** (`inference_timeout_s`, 180 s como la referencia) es por solicitud y **distinto** del límite de readiness (§3.3): un timeout de inferencia se reintenta y no cambia la fase.

### 2.3 Perfiles del servidor

`run-server.sh` construye siempre: `-m <CCE_MODEL_PATH> --alias <CCE_MODEL_ALIAS> --host … --port 8080 -c <total> --parallel <slots> -ngl all --fit off --reasoning on --reasoning-format deepseek --reasoning-budget 512 --api-key-file … --metrics --no-webui`.

- `--fit off` es una **diferencia deliberada** con la referencia: en `b12329` `--fit` está activo por defecto y ajusta los argumentos no fijados para entrar en memoria; apagado, un perfil que no entra en la VRAM **falla de forma explícita** en lugar de derivar a CPU o reducir el contexto en silencio (E-A09-04).
- Perfiles (`deploy/inference/server/profiles/`): `reference-8k-1slot` (predeterminado inicial; idéntico a la referencia) y candidatos **pendientes de medición** `candidate-16k-1slot`, `candidate-16k-2slot` (2 × 8192) y `candidate-32k-2slot` (2 × 16384). El predeterminado definitivo se elige con las mediciones de §6.

## 3. Controlador

### 3.1 Fases y reglas

Estado durable (una fila): `mode` (`auto` | `paused`), `phase`, `generation` y lease del líder. Las reglas son una función pura (`decide.py`) probada con reloj controlado.

| Fase | Despacho | Sale a | Cuándo |
|---|---|---|---|
| `stopped` | no | `starting` | `mode = auto`, hay trabajo pendiente o `start` manual, sin bloqueo y pasado el *backoff*. En `auto`, una instancia encendida desde fuera se **adopta** (queda sujeta a readiness y autoapagado). En `paused` el controlador no toca la instancia |
| `starting` | no | `ready` | instancia `running` y readiness **verificada** (§2.1) antes del plazo |
| | | `stopping` | venció `readiness_timeout_s` (intento fallido: *backoff* 300 s, 600 s, 1200 s…; tras `max_start_attempts` = 3 queda **bloqueado** hasta un `start` manual). Si la instancia sigue detenida (llamada perdida, reinicio del controlador) se **reemite** el arranque |
| `ready` | **sí**, salvo tras un sondeo fallido | `stopping` | `mode = auto`, **sin trabajo pendiente ni activo** y 60 min continuos desde `max(ready_at, última terminación de trabajo)` |
| | | *(sigue en `ready`, despacho cerrado)* | el primer sondeo fallido cierra el despacho (`failed_probes > 0`, evento `dispatch_closed`); un sondeo completo exitoso lo reabre (`dispatch_reopened`). El trabajo activo conserva su lease |
| | | `starting` | `unhealthy_threshold` = 3 sondeos fallidos seguidos (el servidor se reinicia por systemd); la instancia no se detiene |
| | | `stopped`/`stopping` | la instancia se detuvo desde fuera (consola, guardia de la instancia) |
| `draining` | no | `stopping` | sin trabajo activo, o venció `drain_timeout_s` (1800 s; el trabajo activo pierde su lease y vuelve a la cola) |
| | | `starting` | `start` manual (reanudación): el modelo se **verifica de nuevo** antes de reabrir el despacho, porque el drenaje pudo empezar con el modelo aún cargando. El trabajo activo sigue con su lease y el host no se detiene; si vence el nuevo plazo de readiness se detiene igual y ese trabajo vuelve a la cola al vencer su lease |
| `stopping` | no | `stopped` | la instancia llegó a `stopped`; si sigue encendida se **reemite** la detención |

- **Trabajo pendiente** incluye el que espera reintento y el de leases vencidos; **activo** es el que corre con lease vigente.
- **Un solo arranque**: los envíos no llaman a la nube; la transición `stopped → starting` la decide un único líder dentro de una transacción serializada, y la llamada `StartInstances` ocurre después del commit (idempotente).
- **Apagado coordinado con llegadas**: la decisión de autoapagado relee la demanda **en la misma transacción serializada** (`BEGIN IMMEDIATE`) en la que se cambia la fase; la reclamación de un trabajo exige el despacho abierto (`phase = ready` y ningún sondeo fallido desde la última verificación) en otra transacción serializada. Por eso nunca se apaga por inactividad un host con trabajo activo ni se despacha a un host que se está apagando. Un trabajo que llega durante `stopping` queda `pending`; al llegar a `stopped`, la regla de `stopped` vuelve a arrancar (coste: un ciclo de parada y arranque).
- **Recuperación tras reinicios o fallos**: la fase es durable y cada pasada reconcilia el estado real de la instancia hacia ella. Un segundo controlador espera mientras el lease (60 s) del líder esté vigente y lo toma al vencer; un líder anterior que vuelve ya no puede confirmar decisiones (*fencing* por dueño del lease).

### 3.2 Comandos (`deploy/inference/bin/cce-inference`)

| Comando | Efecto |
|---|---|
| `status [--json]` | estado de la instancia (EC2), readiness (solo `/health`), **autoarranque pausado o no**, fase, despacho permitido, sondeos fallidos (`failed_probes`), bloqueo, pendientes/activos, inactividad y tiempo hasta el autoapagado, líder |
| `start [--wait S]` | reanuda el autoarranque (`mode = auto`), limpia un bloqueo y pide capacidad: arranca el host, o, si estaba drenando, vuelve a `starting` y reabre el despacho solo con readiness verificada |
| `stop [--wait S]` | **pausa el autoarranque** y **drena**: no se despacha más, se espera al trabajo activo y se detiene el host. Los pendientes **siguen durables** y no encienden el host mientras esté pausado |
| `serve` | bucle del controlador (cada 10 s; elección de líder por lease) |
| `tick`, `probe [--full]`, `events` | una pasada, sondeo de readiness, bitácora de eventos |
| `submit`, `work [--once]`, `job` | ejecutor de prueba: encolar, ejecutar, consultar |

Con `--wait`, el comando también ejecuta pasadas si no hay otro líder. Configuración TOML: `controller.aws.example.toml` y `controller.onprem.example.toml`.

### 3.3 Tiempos (valores iniciales)

| Parámetro | Valor | Naturaleza |
|---|---|---|
| `idle_timeout_s` | 3600 | fijado por el usuario (D-A09-03) |
| `readiness_timeout_s` | 1200 (AWS), 900 (on-premise) | propuesta P-A09-01 (arranque EC2 + carga de 28,6 GB de Q8_0 + canario; a 250 MiB/s la lectura en frío ronda 110 s, a medir), distinta del timeout de inferencia |
| `inference_timeout_s` | 180 | por solicitud, como la referencia |
| `drain_timeout_s` | 1800 | propuesta P-A09-02 |
| `max_start_attempts`, *backoff* | 3; 300 s × 2ⁿ, tope 3600 s | propuesta P-A09-01 |
| Guardia de la instancia | 120 min | propuesta P-A09-03 (respaldo si el controlador muere) |

## 4. AWS

Plantilla [`deploy/inference/aws/cloudformation.yaml`](../deploy/inference/aws/cloudformation.yaml), generada por `build_template.py` desde `cloudformation.template.yaml` con los archivos del host embebidos en el *user data* (tar.gz determinista; `--check` verifica que esté al día).

- **Instancia**: `g6e.xlarge` (NVIDIA L40S 48 GB, 4 vCPU, 32 GiB), predeterminado de la plantilla tomado del manifiesto; objetivo **pendiente de validación real** con Q8_0 (D-A09-08). Otro tipo debe alojar todo el modelo en VRAM (`-ngl all --fit off`): si no entra, el servidor falla de forma explícita. Lo medido o planeado para `g6.xlarge` (L4) con Q4_K_M queda como histórico. AMI DLAMI *Base OSS Nvidia Driver GPU (Ubuntu 24.04)* **fijada por id** como parámetro, `InstanceInitiatedShutdownBehavior: stop`, IMDSv2 obligatorio, sin par de claves (acceso por SSM Session Manager), raíz gp3 80 GiB cifrada.
- **Pesos persistentes**: volumen gp3 cifrado de 40 GiB por defecto (rendimiento configurable, 250 MiB/s por defecto), montado en `/srv/cce-inference` por UUID; se formatea solo la primera vez (ext4 sin bloques reservados, `-m 0`) y sobrevive a paradas y a reemplazos de instancia dentro del stack. **Mínimo 30 GiB**, generado por `build_template.py` desde el tamaño exacto del artefacto: ⌈26,63 GiB / 0,95 + 1 GiB⌉ (metadatos de ext4 y margen; una sola copia a la vez, porque la descarga temporal se renombra y un archivo que no verifica se elimina antes). El mínimo anterior, 25 GiB, quedaba por debajo del GGUF Q8_0. El GGUF se descarga una vez y se verifica por SHA-256; los arranques posteriores verifican tamaño y registro.
- **Selección del modelo**: la plantilla pasa `--model qwen3.6-27b-q8_0` (de `targets.aws`) a `bootstrap.sh`, que valida el id en el manifiesto antes de tocar nada, instala ese artefacto, escribe `CCE_MODEL_ID`, la ruta y el alias en `server.env` y ejecuta `run-server.sh --check`. El controlador usa `model_alias = "qwen3.6-27b-q8_0"` (`controller.aws.example.toml`).
- **Endpoint privado**: grupo de seguridad sin SSH; el puerto 8080 solo admite el grupo de seguridad del controlador/workers o una CIDR privada. Salida 443 (GitHub, Hugging Face, APIs de AWS) y 80 (espejos de Ubuntu para `libgomp1`). La IP privada principal no cambia entre paradas y arranques (salida `EndpointUrl`).
- **Arranque del servidor al iniciar EC2**: `cce-inference.service` (systemd, habilitado) ejecuta `run-server.sh`; trae la API key de SSM Parameter Store (SecureString) en cada arranque.
- **Guardia de la instancia** (`cce-inference-idle-guard.timer`, cada 5 min): apaga (detiene) el host tras 120 min sin cambios en los contadores de tokens ni solicitudes en curso. Es un respaldo ante un controlador caído; `/health` no lo afecta.
- **Primer arranque** (`bootstrap.sh`, idempotente): monta el volumen, instala `libgomp1` si falta, instala el runtime y el modelo verificados, escribe `server.env`, instala las unidades y registra marcas de tiempo en `/var/lib/cce-inference/bootstrap.log`.
- **Límite del *user data*** (16 KiB en bruto): `build_template.py` incluye en el paquete un `manifest.json` reducido al modelo del destino AWS (mismos id, archivo, URL, tamaño y SHA-256, generado del manifiesto completo) y quita a los scripts empaquetados su cabecera de documentación (el código no cambia; la documentación está en el repositorio). Calcula el *user data* con los valores más largos que admiten los parámetros (`ApiKeyParameterName` ≤ 256, `GuardIdleMinutes` ≤ 1440) y falla si supera el límite; `--report` lo muestra (hoy 13 849 de 16 384 bytes).

### 4.1 Permisos

| Principal | Permisos |
|---|---|
| Controlador (fuera de la GPU) | `ec2:StartInstances` y `ec2:StopInstances` **solo** sobre la instancia del stack; `ec2:DescribeInstances` (no admite restricción por recurso); `ssm:GetParameter` sobre el parámetro de la API key. Política gestionada `ControllerPolicyArn` |
| Host GPU (rol de instancia) | `AmazonSSMManagedInstanceCore`; `ssm:GetParameter` sobre el parámetro de la API key |
| Operador que despliega | crear el stack (`CAPABILITY_IAM`: rol, perfil de instancia y política gestionada), crear el parámetro SecureString |
| Operador que valida (`gpu-validation.py`) | los del controlador, más `ssm:SendCommand` (documento `AWS-RunShellScript`), `ssm:GetCommandInvocation` y `ssm:StartSession` (documento `AWS-StartPortForwardingSession`) sobre la instancia del stack. No se agregan a `ControllerPolicyArn`: el controlador productivo no los necesita |
| Validación automatizada (GitHub Actions, OIDC) | roles del stack de acceso `cce-a09-github-access` (rol OIDC con credenciales temporales, rol de ejecución de CloudFormation, límite de permisos del host, watchdog): [§6 de la automatización](a09-1-aws-validation-automation.md#6-permisos-mínimo-privilegio-acotado-por-recurso-y-etiqueta). La plantilla acepta `HostPermissionsBoundaryArn` (opcional; vacío = sin límite) |

### 4.2 Costos (precios públicos `us-east-1` consultados el 2026-11-20 con la API de precios de AWS)

| Concepto | Precio | Con la configuración por defecto |
|---|---|---|
| `g6e.xlarge` bajo demanda (Linux; vigencia 2026-11-01) | 1,861 USD/h | solo encendida |
| `g6.xlarge` (histórico, L4) | 0,8048 USD/h | ya no es el objetivo |
| EBS gp3 | 0,08 USD/GB-mes | raíz 80 + pesos 40 = 9,60 USD/mes, **también detenida** |
| Rendimiento gp3 sobre 125 MiB/s | 40,96 USD/GiBps-mes (≈ 0,04 USD por MiB/s-mes) | 125 MiB/s extra en el volumen de pesos ≈ 5 USD/mes |
| SSM Parameter Store estándar | sin costo | — |

- **Costo residual detenida** ≈ 14,6 USD/mes (EBS y rendimiento; no cambia con la instancia: 80 + 40 GiB). Con el mínimo de 30 GiB para los pesos serían ≈ 13,8 USD/mes. Con `AssignPublicIp = true`, la IPv4 pública se cobra solo mientras la instancia está encendida (se libera al detenerse). Un NAT gateway propio, si la subred lo usa, tiene su costo aparte.
- **Costo mínimo por despertar**: cada encendido dura al menos 60 min de inactividad tras el último trabajo más el arranque (≈ 1,86 USD + el trabajo; con la L4 eran ≈ 0,81 USD). El controlador caído deja el host encendido hasta la guardia de 120 min.
- Eliminar el stack borra la instancia, los volúmenes (política `Delete`) y la política; los pesos se vuelven a descargar en un stack nuevo.

## 5. On-premise

`deploy/inference/onprem/install.sh` instala el mismo runtime, perfiles, endpoint, API key y contrato de readiness, con **pesos locales** (`--weights-from`, verificados por SHA-256) y **sin servicios ni credenciales de AWS**. El modelo por defecto es `targets.onprem` del manifiesto (Q4_K_M, `qwen3.6-27b-q4km`; `--model` elige otro del manifiesto); escribe `CCE_MODEL_ID` en `server.env` y ejecuta `run-server.sh --check`. Una instalación existente con Q4_K_M sigue funcionando sin reinstalar: su `server.env` sin `CCE_MODEL_ID` resuelve a Q4_K_M y su registro de verificación sigue siendo válido. Requisito nuevo: `python3` en el host (lee el manifiesto en la verificación previa al arranque). Requisitos: Linux x64 con systemd, GPU NVIDIA con ≥ 24 GB para el perfil de referencia, driver apto para CUDA 13.5 (o `--variant cuda-12.8`), `libgomp1`. El endpoint escucha en `--bind` (por defecto `127.0.0.1`); el control de acceso es la API key (generada si no se provee, 0600) más el cortafuegos del sitio. El controlador usa el backend `command` (`systemctl start/stop/is-active cce-inference.service`), de modo que el ciclo de readiness, despacho, autoapagado (del servicio) y los comandos son los mismos. No se instala la guardia de inactividad.

## 6. Mediciones y elección del perfil

Herramientas: `bench/measure.py` (`load`, `run`, `limits`, `ec2-cycle`, `verdict`), `bench/mem-sample.sh` (muestreo de memoria **en el host medido**), `bench/ssm-run.sh` (comando en la instancia por SSM, sin SSH), `server/switch-profile.sh` (cambio de perfil persistente con reinicio), `bench/host-facts.sh` (GPU, driver, CUDA, AMI, runtime y modelo instalados), `bench/boot-timings.sh` (línea de tiempo del arranque) y `bench/parity.sh` (paridad con la referencia usando sin cambios `benchmark.py` y `proxy_esquema.py` del ZIP, que queda fuera de Git). La secuencia completa la ejecuta `bench/gpu-validation.py` (§6.2; receta en el [informe §4](a09-1-implementation-report.md#4-validación-pendiente-en-gpu-real)). Orden obligatorio:

En AWS todo se mide sobre el objetivo `g6e.xlarge` + Q8_0 (todos los pesos en GPU):

1. `reference-8k-1slot` (8k, un slot) primero: arranque EC2, carga de pesos y tiempo hasta readiness por separado; pico de VRAM/RAM en el host; latencia y throughput con 1 slot; sobrecarga; límites de contexto; calidad con la metodología de la referencia.
2. Solo si hay margen de VRAM: `candidate-16k-1slot` (más contexto por solicitud) y luego `candidate-16k-2slot` (dos slots). `candidate-32k-2slot` solo si los anteriores entran con margen.
3. Se registra por perfil: contexto total y efectivo por slot, espacio de salida y razonamiento (3072 / 512), pico de VRAM y RAM, latencia (mediana, p95), throughput, respuesta a sobrecarga (cola) y a entradas fuera de límite.
4. El predeterminado se elige con esas mediciones. **Si la configuración base no entra o no cumple, se documenta el límite y se eleva la decisión al usuario** antes de cambiar instancia, cuantización o introducir offload a CPU (D-A09-05). Con `-ngl all --fit off` un perfil que no entra en VRAM falla de forma explícita.
5. **Calidad de Q8_0 por separado.** `parity.sh` corre el ZIP externo sin cambios (mismo corpus y prompt, con hashes comprobados; mismos parámetros: temperatura 0, `max_tokens` 3072, timeout 180 s, 3 repeticiones, `reference-8k-1slot`) contra el alias servido (`--model`, comprobado en `/v1/models`) y registra junto al resultado el **artefacto exacto** (registro de verificación del archivo servido) y los hashes de `benchmark.py`, `proxy_esquema.py` y `esquema_salida.json`. El resultado de Q8_0 se informa aparte del 43/50 histórico, que pertenece a Q4_K_M: son artefactos distintos y nunca se combinan.

### 6.1 Evidencia de memoria

La VRAM y la RAM se muestrean **en el host medido** mientras corre **la misma carga**: `measure.py load|run` ejecuta `--memory-start-cmd` antes de la carga y `--memory-stop-cmd` después; desde el operador ambos corren `mem-sample.sh start|stop` en EC2 por `ssm-run.sh` (en el propio host, `--memory-local`). El resumen registra el **host** (hostname, `instance_id`, tipo, GPU), la **cantidad de muestras** de VRAM y de RAM y la ventana de muestreo. La memoria queda `measured` solo si hay muestras de ambas, la ventana cubre la carga (tolerancia 2 s) y, con `--expect-instance-id`, el host es el esperado; en cualquier otro caso es `unavailable` con un motivo (`not_requested`, `no_samples`, `host_mismatch`, `window_not_covered`, …) y **sin cifras**: una muestra ausente nunca se informa como 0.

Criterios del perfil (`measure.py verdict --slots N [--reference] --model ALIAS`, fijados antes de medir): carga con `--fit off` hasta readiness; cada carga **sirvió el alias esperado** (`/v1/models`; las mediciones envían y exigen `--model`, y un alias distinto nunca llega a `ready`); los slots que informa el servidor coinciden con los de la definición del perfil (`CCE_PARALLEL`); **≥ 1024 MiB de VRAM libre en el pico medido** en todas las cargas y corridas; `limits` (prompt admitido con 3072 de salida responde; fuera de contexto, 400 `exceed_context_size_error`); y **cada corrida esperada** sin errores, con JSON válido, sin `finish_reason = length` y a la concurrencia esperada. Corridas esperadas (`expected_runs`): `c1` (concurrencia 1) en todo perfil; `cN` (concurrencia N) en un perfil de N > 1 slots; `overload` (concurrencia N + 2, cola más allá de los slots) en el perfil de referencia. Cada corrida se identifica por su `tag`. El veredicto es `pass` solo si todos los criterios pasan **con evidencia de todas las corridas esperadas**; una corrida esperada sin evidencia (`missing`), un archivo ilegible o un criterio sin evidencia (por ejemplo, memoria `unavailable`) dan `incomplete`, que no aprueba el perfil; una medición que se ejecutó y falló queda registrada como `status: failed` (con su salida y su error) y da `fail`, con el detalle en `failures`.

### 6.2 Secuencia de validación en GPU (`bench/gpu-validation.py`)

Desde la máquina del operador, con herramientas reemplazables para la prueba simulada:

1. **Verificación previa sin efectos**: la instancia está `stopped`, el estado del controlador es nuevo (`auto`, sin trabajo), el directorio de evidencia está fuera del repositorio, cada perfil pedido está definido en `server/profiles/` (de ahí salen sus slots), el `model_alias` del controlador es el alias del modelo `targets.aws` del manifiesto y el intérprete es el del entorno del controlador (`CCE_PYTHON`, si está definido, debe ser el mismo que ejecuta el script; Python ≥ 3.13); si no, se rechaza sin tocar nada (salida 2). Ese intérprete ejecuta también los procesos hijos: `cce-inference` (vía `CCE_PYTHON`) y `measure.py`.
2. **Ciclo disparado por trabajo**: túnel SSM, `serve` y un worker en segundo plano; se encola **un** trabajo durable y se exige en la evidencia el arranque pedido por demanda (sin `start` manual), la instancia en `running`, la readiness verificada y el trabajo `succeeded` con salida estructurada. Se registra la línea de tiempo (envío, pedido de arranque, `running` observado, `ready`, fin del trabajo).
3. **Evidencia del host** (hechos y línea de tiempo del arranque en frío) por SSM. El artefacto servido (id, archivo y alias de `server.env`; archivo, tamaño y SHA-256 de su registro de verificación) debe ser el del destino AWS del manifiesto; si no, la corrida falla de forma explícita (y se detiene la instancia). El registro queda en `model-record.json` y acompaña a cada resultado de calidad.
4. **Campaña de perfiles con la instancia disponible**: el controlador se detiene para que no apague por inactividad bajo las mediciones; por perfil, `load` (cambio + reinicio), `limits` y **todas** las corridas esperadas (§6.1) con memoria en el host, y `verdict` sobre **todos** sus archivos. Cada medición deja siempre su archivo: la medición o, si se ejecutó y falló (salida ≠ 0, tiempo agotado o sin salida legible), un registro `status: failed`; nunca se omite. Si `load` falla, no se mide nada más de ese perfil (las cifras serían de otro perfil). La campaña se detiene en el primer perfil que no pasa (D-A09-05).
5. **Restitución del perfil con la instancia en `running`**: el perfil original se vuelve a escribir en `server.env` por Run Command (`switch-profile.sh ORIGINAL --no-restart`) y se **verifica** con `switch-profile.sh --current`; rige desde el próximo arranque. Debe hacerse antes de la parada: Run Command no llega a una instancia detenida.
6. **Parada final por el controlador** (`stop --wait`: pausa, drenaje, parada).

**Siempre** (también ante fallo, Ctrl-C, SIGTERM o SIGHUP, con las señales ignoradas durante la limpieza): se detienen los procesos de fondo; si el perfil cambió y aún no está restituido, se restituye y verifica **antes de detener**, solo si la instancia está `running` (si no, se registra `profile_restore_skipped`); un fallo de la restitución se registra y **nunca impide** la parada. Después se detiene la instancia si no lo está, se **verifica `stopped`** y se escribe `final-state.json`, que registra **por separado** el estado de EC2 (`state`, `instance_stopped`, `fallback_stop`) y el del perfil (`profile.status`: `not_changed`, `restored` o `not_restored`, con el perfil leído y cada intento). Si no queda verificada, imprime `CLEANUP FAILED` con el comando para detenerla y sale con 4. Salidas: 0 validado (todos los perfiles pasan, perfil restituido e instancia detenida); 1 falló, quedó incompleta o **el perfil no quedó restituido** (instancia detenida; nunca se declara validada); 2 rechazada sin efectos; 130 interrumpida (instancia detenida).

**Carga en GPU (D-A09-08).** En el paso 3 se guarda el journal del servidor de ese arranque (`server-journal-job-cycle.log`) y, tras la campaña, otra vez (`server-journal-campaign.log`); cada carga del modelo debe decir `offloaded N/N layers to GPU` (`gpu-offload-*.json`: `verified`, `partial` o `no_evidence`). Una carga parcial o sin evidencia hace fallar la corrida. Ante fallo o interrupción, la limpieza guarda el journal si la instancia sigue encendida (`server-journal-final.log`).

### 6.3 Validación automatizada en GitHub Actions

Los pasos A y B del [informe §4](a09-1-implementation-report.md#4-validación-pendiente-en-gpu-real) se ejecutan al mergear a `main` una solicitud versionada `deploy/inference/validation-requests/<id>.json` (id único, configuración, límite de tiempo y `after_run` explícito): preflight sin efectos, reserva atómica del id (no se repite el gasto), stack con un rol de ejecución acotado, watchdog en AWS (EventBridge Scheduler) independiente del runner, bootstrap y primera carga en GPU, instancia detenida, `gpu-validation.py` por túnel SSM, limpieza verificada (`keep-stopped` o `delete`) en un job propio. Contrato, habilitación inicial y garantías: [validación AWS automatizada](a09-1-aws-validation-automation.md). Implementada y probada con AWS simulado; **no** ejecutada en GitHub Actions ni contra AWS real.

## 7. Límites declarados

- Sin GPU real todavía: en el objetivo `g6e.xlarge` + Q8_0, tiempos de arranque, carga, VRAM, latencia, throughput y calidad están **pendientes**. También queda por comprobar en la L40S que el muestreador lanzado por SSM sobrevive al fin del comando (`setsid nohup`); si no sobreviviera, la medición queda `unavailable` (`no_samples` o `window_not_covered`), no aprobada. Riesgos a medir: 28,6 GB de pesos Q8_0 más la caché KV frente a los 48 GB de la L40S (margen esperado amplio; no medido) y 32 GiB de RAM del host frente a un archivo de 26,6 GiB cargado por `mmap` (y la caché de prompts de `llama-server`, 8192 MiB por defecto). Lo previsto para la L4 con Q4_K_M (19,4 GB frente a 22 888 MiB) queda como histórico.
- La calidad de Q8_0 no está medida; el 43/50 corresponde solo a Q4_K_M en la corrida externa y no se traslada a Q8_0.
- El estado del controlador vive en SQLite en un único host; el ejecutor de prueba comparte ese archivo para serializar la decisión de apagado con el despacho. B01 debe dar la misma garantía con la cola real.
- Las horas de las decisiones son del reloj del host del controlador (un único líder); no dependen de relojes de los workers.
- No se detiene cómputo remoto ya iniciado al vencer un timeout; el trabajo se reintenta.
- Sin TLS en el endpoint (red privada + API key); TLS terminado en el sitio es responsabilidad del despliegue.
- La validación automatizada (§6.3) no se ejecutó en GitHub Actions ni contra AWS: la semántica real de sus políticas IAM, el trust OIDC, EventBridge Scheduler y la presencia de `session-manager-plugin` en el runner se comprueban en la primera corrida ([límites](a09-1-aws-validation-automation.md#9-pruebas-y-límites)).
