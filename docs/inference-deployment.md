# Despliegue de inferencia y operación (A09-1)

> **Estado: A09-1 IMPLEMENTADA (infraestructura y controlador), pendiente de la validación en GPU real y de la aceptación del usuario.** Probada con simulación en macOS y en Linux local (Docker); **no** probada en hardware con GPU (la cuota de instancias G de la cuenta es 0 vCPU en `us-east-1`, ver [informe §4](a09-1-implementation-report.md#4-validación-pendiente-en-gpu-real)); **no** ejecutada en GitHub Actions. El paquete del motor (`0.7.0`) no cambia: no hay migración, permisos, dependencias ni API nuevas. Alcance y matriz: [especificación de A09-1](features/A09-1-despliegue-de-inferencia-y-operacion.md); decisiones: [A09-decisions](decisions/A09-decisions.md); plan: [A09](a09-plan.md).

Este documento conserva el contrato de despliegue y operación. Todo vive en [`deploy/inference/`](../deploy/inference/), fuera de la wheel y del sdist.

## 1. Piezas y fronteras

```text
 workers / API (host de CAWbase, B01)        controlador (fuera de la GPU y del núcleo)          host GPU (EC2 g6.xlarge u on-premise)
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

Servidor: `llama-server` de llama.cpp `b12329` (binario oficial Linux x64 CUDA 13.5, ver [manifiesto](../deploy/inference/manifest.json)), modelo `Qwen3.6-27B-Q4_K_M.gguf` con alias `qwen3.6-27b-q4km`. Igual en AWS y on-premise.

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

### 2.2 Límites, contexto y cola

- **Contexto total y por slot**: `-c` es el contexto total; con `--parallel N` y KV no unificado, el contexto por slot es `c / N` (la referencia registra `n_slots = 1, n_ctx_slot = 8192, kv_unified = 'false'`).
- **Admisión explícita**: antes de enviar, el ejecutor cuenta los tokens del prompt y rechaza **sin reintentar** (`input_exceeds_context`, con `n_prompt_tokens`, `max_tokens` y `n_ctx_slot` en el detalle) toda entrada con `n_prompt + max_tokens > n_ctx_slot`. Si aun así el servidor rechaza (HTTP 400 `exceed_context_size_error`) se informa el mismo código. **Nunca se trunca.** Con el perfil de referencia (8192 por slot, 3072 de salida) el prompt admitido es ≤ 5120 tokens; la referencia externa usó ≤ 1764 tokens de prompt y ≤ 652 de salida (máximo total 2399).
- **Cola**: el ejecutor no despacha más trabajos simultáneos que slots (`max_in_flight` = `--parallel`); el excedente espera **durable** en la cola y no en la memoria del servidor (que también encola, como respaldo).
- **Timeout de inferencia** (`inference_timeout_s`, 180 s como la referencia) es por solicitud y **distinto** del límite de readiness (§3.3): un timeout de inferencia se reintenta y no cambia la fase.

### 2.3 Perfiles del servidor

`run-server.sh` construye siempre: `-m … --alias qwen3.6-27b-q4km --host … --port 8080 -c <total> --parallel <slots> -ngl all --fit off --reasoning on --reasoning-format deepseek --reasoning-budget 512 --api-key-file … --metrics --no-webui`.

- `--fit off` es una **diferencia deliberada** con la referencia: en `b12329` `--fit` está activo por defecto y ajusta los argumentos no fijados para entrar en memoria; apagado, un perfil que no entra en la VRAM **falla de forma explícita** en lugar de derivar a CPU o reducir el contexto en silencio (E-A09-04).
- Perfiles (`deploy/inference/server/profiles/`): `reference-8k-1slot` (predeterminado inicial; idéntico a la referencia) y candidatos **pendientes de medición** `candidate-16k-1slot`, `candidate-16k-2slot` (2 × 8192) y `candidate-32k-2slot` (2 × 16384). El predeterminado definitivo se elige con las mediciones de §6.

## 3. Controlador

### 3.1 Fases y reglas

Estado durable (una fila): `mode` (`auto` | `paused`), `phase`, `generation` y lease del líder. Las reglas son una función pura (`decide.py`) probada con reloj controlado.

| Fase | Despacho | Sale a | Cuándo |
|---|---|---|---|
| `stopped` | no | `starting` | `mode = auto`, hay trabajo pendiente o `start` manual, sin bloqueo y pasado el *backoff*. En `auto`, una instancia encendida desde fuera se **adopta** (queda sujeta a readiness y autoapagado). En `paused` el controlador no toca la instancia |
| `starting` | no | `ready` | instancia `running` y readiness real (§2.1) |
| | | `stopping` | venció `readiness_timeout_s` (intento fallido: *backoff* 300 s, 600 s, 1200 s…; tras `max_start_attempts` = 3 queda **bloqueado** hasta un `start` manual). Si la instancia sigue detenida (llamada perdida, reinicio del controlador) se **reemite** el arranque |
| `ready` | **sí** | `stopping` | `mode = auto`, **sin trabajo pendiente ni activo** y 60 min continuos desde `max(ready_at, última terminación de trabajo)` |
| | | `starting` | `unhealthy_threshold` = 3 sondeos fallidos seguidos (el servidor se reinicia por systemd); el despacho se suspende |
| | | `stopped`/`stopping` | la instancia se detuvo desde fuera (consola, guardia de la instancia) |
| `draining` | no | `stopping` | sin trabajo activo, o venció `drain_timeout_s` (1800 s; el trabajo activo pierde su lease y vuelve a la cola) |
| `stopping` | no | `stopped` | la instancia llegó a `stopped`; si sigue encendida se **reemite** la detención |

- **Trabajo pendiente** incluye el que espera reintento y el de leases vencidos; **activo** es el que corre con lease vigente.
- **Un solo arranque**: los envíos no llaman a la nube; la transición `stopped → starting` la decide un único líder dentro de una transacción serializada, y la llamada `StartInstances` ocurre después del commit (idempotente).
- **Apagado coordinado con llegadas**: la decisión de autoapagado relee la demanda **en la misma transacción serializada** (`BEGIN IMMEDIATE`) en la que se cambia la fase; la reclamación de un trabajo exige `phase = ready` en otra transacción serializada. Por eso nunca se apaga por inactividad un host con trabajo activo ni se despacha a un host que se está apagando. Un trabajo que llega durante `stopping` queda `pending`; al llegar a `stopped`, la regla de `stopped` vuelve a arrancar (coste: un ciclo de parada y arranque).
- **Recuperación tras reinicios o fallos**: la fase es durable y cada pasada reconcilia el estado real de la instancia hacia ella. Un segundo controlador espera mientras el lease (60 s) del líder esté vigente y lo toma al vencer; un líder anterior que vuelve ya no puede confirmar decisiones (*fencing* por dueño del lease).

### 3.2 Comandos (`deploy/inference/bin/cce-inference`)

| Comando | Efecto |
|---|---|
| `status [--json]` | estado de la instancia (EC2), readiness (solo `/health`), **autoarranque pausado o no**, fase, despacho permitido, bloqueo, pendientes/activos, inactividad y tiempo hasta el autoapagado, líder |
| `start [--wait S]` | reanuda el autoarranque (`mode = auto`), limpia un bloqueo y pide capacidad: arranca el host, o, si estaba drenando, vuelve a `ready` |
| `stop [--wait S]` | **pausa el autoarranque** y **drena**: no se despacha más, se espera al trabajo activo y se detiene el host. Los pendientes **siguen durables** y no encienden el host mientras esté pausado |
| `serve` | bucle del controlador (cada 10 s; elección de líder por lease) |
| `tick`, `probe [--full]`, `events` | una pasada, sondeo de readiness, bitácora de eventos |
| `submit`, `work [--once]`, `job` | ejecutor de prueba: encolar, ejecutar, consultar |

Con `--wait`, el comando también ejecuta pasadas si no hay otro líder. Configuración TOML: `controller.aws.example.toml` y `controller.onprem.example.toml`.

### 3.3 Tiempos (valores iniciales)

| Parámetro | Valor | Naturaleza |
|---|---|---|
| `idle_timeout_s` | 3600 | fijado por el usuario (D-A09-03) |
| `readiness_timeout_s` | 1200 (AWS), 900 (on-premise) | propuesta P-A09-01 (arranque EC2 + carga de 19,4 GB + canario), distinta del timeout de inferencia |
| `inference_timeout_s` | 180 | por solicitud, como la referencia |
| `drain_timeout_s` | 1800 | propuesta P-A09-02 |
| `max_start_attempts`, *backoff* | 3; 300 s × 2ⁿ, tope 3600 s | propuesta P-A09-01 |
| Guardia de la instancia | 120 min | propuesta P-A09-03 (respaldo si el controlador muere) |

## 4. AWS

Plantilla [`deploy/inference/aws/cloudformation.yaml`](../deploy/inference/aws/cloudformation.yaml), generada por `build_template.py` desde `cloudformation.template.yaml` con los archivos del host embebidos en el *user data* (tar.gz determinista; `--check` verifica que esté al día).

- **Instancia**: `g6.xlarge` (L4 22 888 MiB, 4 vCPU, 16 GiB), AMI DLAMI *Base OSS Nvidia Driver GPU (Ubuntu 24.04)* **fijada por id** como parámetro, `InstanceInitiatedShutdownBehavior: stop`, IMDSv2 obligatorio, sin par de claves (acceso por SSM Session Manager), raíz gp3 80 GiB cifrada.
- **Pesos persistentes**: volumen gp3 cifrado de 40 GiB (rendimiento configurable, 250 MiB/s por defecto), montado en `/srv/cce-inference` por UUID; se formatea solo la primera vez y sobrevive a paradas y a reemplazos de instancia dentro del stack. El GGUF se descarga una vez y se verifica por SHA-256; los arranques posteriores verifican tamaño y registro.
- **Endpoint privado**: grupo de seguridad sin SSH; el puerto 8080 solo admite el grupo de seguridad del controlador/workers o una CIDR privada. Salida 443 (GitHub, Hugging Face, APIs de AWS) y 80 (espejos de Ubuntu para `libgomp1`). La IP privada principal no cambia entre paradas y arranques (salida `EndpointUrl`).
- **Arranque del servidor al iniciar EC2**: `cce-inference.service` (systemd, habilitado) ejecuta `run-server.sh`; trae la API key de SSM Parameter Store (SecureString) en cada arranque.
- **Guardia de la instancia** (`cce-inference-idle-guard.timer`, cada 5 min): apaga (detiene) el host tras 120 min sin cambios en los contadores de tokens ni solicitudes en curso. Es un respaldo ante un controlador caído; `/health` no lo afecta.
- **Primer arranque** (`bootstrap.sh`, idempotente): monta el volumen, instala `libgomp1` si falta, instala el runtime y el modelo verificados, escribe `server.env`, instala las unidades y registra marcas de tiempo en `/var/lib/cce-inference/bootstrap.log`.

### 4.1 Permisos

| Principal | Permisos |
|---|---|
| Controlador (fuera de la GPU) | `ec2:StartInstances` y `ec2:StopInstances` **solo** sobre la instancia del stack; `ec2:DescribeInstances` (no admite restricción por recurso); `ssm:GetParameter` sobre el parámetro de la API key. Política gestionada `ControllerPolicyArn` |
| Host GPU (rol de instancia) | `AmazonSSMManagedInstanceCore`; `ssm:GetParameter` sobre el parámetro de la API key |
| Operador que despliega | crear el stack (`CAPABILITY_IAM`: rol, perfil de instancia y política gestionada), crear el parámetro SecureString |

### 4.2 Costos (precios públicos `us-east-1` consultados el 2026-11-20 con la API de precios de AWS)

| Concepto | Precio | Con la configuración por defecto |
|---|---|---|
| `g6.xlarge` bajo demanda (Linux) | 0,8048 USD/h | solo encendida |
| EBS gp3 | 0,08 USD/GB-mes | raíz 80 + pesos 40 = 9,60 USD/mes, **también detenida** |
| Rendimiento gp3 sobre 125 MiB/s | 40,96 USD/GiBps-mes (≈ 0,04 USD por MiB/s-mes) | 125 MiB/s extra en el volumen de pesos ≈ 5 USD/mes |
| SSM Parameter Store estándar | sin costo | — |

- **Costo residual detenida** ≈ 14,6 USD/mes (EBS y rendimiento). Con `AssignPublicIp = true`, la IPv4 pública se cobra solo mientras la instancia está encendida (se libera al detenerse). Un NAT gateway propio, si la subred lo usa, tiene su costo aparte.
- **Costo mínimo por despertar**: cada encendido dura al menos 60 min de inactividad tras el último trabajo más el arranque (≈ 0,81 USD + el trabajo). El controlador caído deja el host encendido hasta la guardia de 120 min.
- Eliminar el stack borra la instancia, los volúmenes (política `Delete`) y la política; los pesos se vuelven a descargar en un stack nuevo.

## 5. On-premise

`deploy/inference/onprem/install.sh` instala el mismo runtime, modelo, perfiles, endpoint, API key y contrato de readiness, con **pesos locales** (`--weights-from`, verificados por SHA-256) y **sin servicios ni credenciales de AWS**. Requisitos: Linux x64 con systemd, GPU NVIDIA con ≥ 24 GB para el perfil de referencia, driver apto para CUDA 13.5 (o `--variant cuda-12.8`), `libgomp1`. El endpoint escucha en `--bind` (por defecto `127.0.0.1`); el control de acceso es la API key (generada si no se provee, 0600) más el cortafuegos del sitio. El controlador usa el backend `command` (`systemctl start/stop/is-active cce-inference.service`), de modo que el ciclo de readiness, despacho, autoapagado (del servicio) y los comandos son los mismos. No se instala la guardia de inactividad.

## 6. Mediciones y elección del perfil

Herramientas: `bench/measure.py` (`load`, `run`, `limits`, `ec2-cycle`), `bench/host-facts.sh` (GPU, driver, CUDA, AMI, runtime y modelo instalados) y `bench/parity.sh` (paridad con la referencia usando sin cambios `benchmark.py` y `proxy_esquema.py` del ZIP, que queda fuera de Git). Orden obligatorio (receta exacta en el [informe §4](a09-1-implementation-report.md#4-validación-pendiente-en-gpu-real)):

1. `reference-8k-1slot`: arranque EC2, carga de pesos y tiempo hasta readiness por separado; pico de VRAM/RAM; latencia y throughput con 1 slot; límites de contexto; paridad.
2. Solo si hay margen de VRAM: `candidate-16k-1slot` (más contexto por solicitud) y luego `candidate-16k-2slot` (dos slots). `candidate-32k-2slot` solo si los anteriores entran con margen.
3. Se registra por perfil: contexto total y efectivo por slot, espacio de salida y razonamiento (3072 / 512), pico de VRAM y RAM, latencia (mediana, p95), throughput, respuesta a sobrecarga (cola) y a entradas fuera de límite.
4. El predeterminado se elige con esas mediciones. **Si la configuración base no entra o no cumple, se documenta el límite y se eleva la decisión al usuario** antes de cambiar instancia, cuantización o introducir offload a CPU (D-A09-05). Q8_0 (≈ 2× los pesos) no entra en la L4 y queda documentado solo para hardware con más VRAM.

## 7. Límites declarados

- Sin GPU real todavía: tiempos de arranque, carga, VRAM, latencia, throughput y paridad están **pendientes**. Los riesgos conocidos a medir: 19,4 GB de pesos frente a 22 888 MiB de VRAM (la referencia usó +19 123 MiB en la 5090 con 8192 de contexto) y 16 GiB de RAM del host frente a un archivo de 18 GiB cargado por `mmap` (y la caché de prompts de `llama-server`, 8192 MiB por defecto).
- El estado del controlador vive en SQLite en un único host; el ejecutor de prueba comparte ese archivo para serializar la decisión de apagado con el despacho. B01 debe dar la misma garantía con la cola real.
- Las horas de las decisiones son del reloj del host del controlador (un único líder); no dependen de relojes de los workers.
- No se detiene cómputo remoto ya iniciado al vencer un timeout; el trabajo se reintenta.
- Sin TLS en el endpoint (red privada + API key); TLS terminado en el sitio es responsabilidad del despliegue.
