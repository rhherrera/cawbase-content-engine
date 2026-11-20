# A09 — Registro de decisiones

> **Estado: revisión 2 — A09-1 implementada, con las correcciones de la revisión de código 1 (infraestructura, controlador y ejecutor de prueba), pendiente de la validación en GPU real y de la aceptación del usuario.** A09-2 no empezó.
>
> - **Mandato.** El usuario pidió en una sola sesión implementar A09-1 usando como alcance el [plan versionado](../a09-plan.md) (commit `cf8fbb7`), probar, documentar y entregar commits locales. Ese pedido autoriza la implementación sin una ronda documental previa; el contrato se entrega junto con el código y queda para su revisión.
> - **Documentos.** Contrato: [despliegue de inferencia y operación](../inference-deployment.md); alcance y matriz: [especificación de A09-1](../features/A09-1-despliegue-de-inferencia-y-operacion.md); evidencia: [informe de A09-1](../a09-1-implementation-report.md).

Categorías (no mezclarlas): **requisitos fijados por el usuario** (§2) · **elecciones técnicas** (§3) · **propuestas pendientes** (§4) · **preguntas y bloqueos** (§5).

## 0. Historial de revisiones

| Rev. | Contenido |
|---|---|
| 1 | A09-1 implementada sobre `cf8fbb7` en `feature/a09-1-inference-infrastructure`. D-A09-01 a 07 registran lo que el usuario fijó en el encargo. E-A09-01 a 14. P-A09-01 a 05 pendientes. Q-A09-01 a 05 abiertas (Q-A09-01 bloquea la validación en GPU). |
| 2 | Correcciones de la revisión del usuario sobre `889e2fe` (tres hallazgos): E-A09-06 precisada; E-A09-15 (compuerta de despacho por readiness verificada), E-A09-16 (memoria muestreada en el host medido) y E-A09-17 (secuencia de validación con parada protegida). Sin cambios en D-, P- ni Q-. Instancia, modelo, autoapagado de 60 min y alcance se conservan. |
| 3 | Correcciones de la revisión del usuario sobre `de3e8e2` (dos defectos del script de validación): E-A09-17 precisada (restitución del perfil verificada con la instancia en `running`, antes de la parada) y E-A09-18 (veredicto con evidencia de todas las corridas esperadas). Sin cambios en D-, P- ni Q-. |

## 1. Base

- `main` = `55fc607` (merge del PR #7 de A08). Rama `feature/a09-1-inference-infrastructure` creada desde `main` y avanzada por *fast-forward* al commit documental del plan `cf8fbb7`, traído con `git fetch` desde el checkout local indicado por el usuario (autor del commit: Codex). Identidad Git local del repositorio configurada.
- A08 está cerrada por el usuario y mergeada (según el plan); A06 y A07 siguen con su aceptación pendiente. A09-1 **no modifica** el paquete del motor.

## 2. Requisitos fijados por el usuario (encargo de A09-1)

Solo el usuario los cambia.

### D-A09-01 — Modelo, runtime y referencia externa

Qwen3.6-27B Q4_K_M según el manifiesto del plan (repositorio, revisión, archivo y SHA-256 verificado al instalar). llama.cpp `b12329` como referencia, fijando el artefacto Linux/CUDA usado, su procedencia, el driver y la configuración. Referencia externa: `razona512+esquema`, un slot, 8192 de contexto, hasta 3072 tokens de salida, 43/50 casos estrictos en español en una RTX 5090; se conserva como **evidencia externa**. El ZIP, las respuestas esperadas, los resultados crudos y los pesos quedan **fuera de Git**.

### D-A09-02 — Objetivo AWS y presupuesto de memoria

`g6.xlarge` (NVIDIA L4) con Q4_K_M como perfil inicial; Q8_0 documentado para hardware con más VRAM. Medir primero un slot; si hay margen, más contexto por solicitud y dos slots. Registrar contexto total y efectivo por slot, espacio de salida y razonamiento, pico de VRAM/RAM, latencia y throughput. Encolar el exceso de capacidad y rechazar explícitamente las entradas que excedan el contexto admitido. Elegir el predeterminado a partir de mediciones.

### D-A09-03 — Ciclo de vida

Controlador fuera de la GPU y del núcleo Python. El primer trabajo durable que requiere inferencia dispara el arranque; varias solicitudes coordinan un solo arranque; los trabajos se conservan mientras se espera readiness real, con un límite de espera separado del timeout de inferencia; la API no espera el arranque en frío. Apagado tras **60 minutos continuos** sin trabajo de inferencia pendiente ni activo; el health check no cuenta como actividad; la decisión de apagar se coordina con la llegada de trabajos; el despacho se recupera tras reinicios o fallos del controlador.

### D-A09-04 — Comandos manuales

Script con `start`, `stop` y `status`. `stop` pausa el autoarranque y drena los trabajos activos; los pendientes permanecen durables. `start` arranca o reanuda la operación. `status` informa el estado de EC2, la readiness y si el autoarranque está pausado. Documentar permisos y costos residuales.

### D-A09-05 — Escalamiento

Si la configuración base no entra o no cumple, documentar el límite y **elevar la decisión al usuario** antes de cambiar instancia, cuantización o introducir offload a CPU.

### D-A09-06 — Alcance

Ejecutor de prueba con trabajos durables para demostrar el ciclo. La cola y los workers reales de CAWbase son de B01; el adaptador del motor, la segmentación, las posiciones y la evaluación formal son de A09-2. A09-1 termina en el contrato de servicio, el despliegue y una prueba real de inferencia estructurada. Despliegue reproducible en AWS (endpoint privado, pesos persistentes en EBS, servidor al iniciar EC2) e instalación on-premise con el mismo contrato, pesos locales y sin AWS.

### D-A09-07 — Cuenta, gasto y verificación

Usar la cuenta o perfil `rodrigo` si corresponde, nunca `fede`. Si faltan cuenta, región, cuota o autorización de gasto, completar la implementación y las pruebas locales y dejar una receta exacta. Si se autoriza una prueba que levanta la GPU, comprobar al terminar que la instancia quedó detenida. Push, PR y merge los hace el usuario.

## 3. Elecciones técnicas (no son decisiones de producto)

| Id | Elección | Motivo |
|---|---|---|
| E-A09-01 | Runtime: **tarballs oficiales** `llama-b12329-bin-ubuntu-cuda-13.5-x64.tar.gz` + `cudart-…-cuda-13.5-x64.tar.gz` (SHA-256 publicado y verificado), no imagen Docker | No existe imagen `server-cuda-b12329` en `ghcr.io`; los tarballs son el artefacto oficial del tag y evitan nvidia-container-toolkit. La referencia usó el binario Windows CUDA 13.5 del mismo tag. Respaldo documentado: variante `cuda-12.8` |
| E-A09-02 | AMI DLAMI *Base OSS Nvidia Driver GPU (Ubuntu 24.04)* **fijada por id** como parámetro (resuelta del parámetro público de SSM) | Driver NVIDIA preinstalado y soportado en G6; la fijación por id hace reproducible el despliegue |
| E-A09-03 | CloudFormation con el paquete del host (tar.gz determinista en base64) en el *user data*, generado y verificado por `build_template.py` | Una sola plantilla autocontenida, sin bucket intermedio; `--check` evita desincronización |
| E-A09-04 | `--fit off` además de `-ngl all` y `-c` explícito | En `b12329` `--fit` (activo por defecto) ajusta argumentos no fijados; apagado, lo que no entra falla de forma explícita (D-A09-05) |
| E-A09-05 | Estado del controlador en SQLite (WAL, `BEGIN IMMEDIATE`), una fila con fase, generación y lease del líder; reglas puras en `decide.py` | Serializa la decisión de apagado con la reclamación de trabajos; prueba determinista con reloj controlado |
| E-A09-06 | Readiness = `/health` 200 + alias + **canario estructurado** al arrancar y para reabrir el despacho tras un fallo (solo ese sondeo es «verificado»); liveness = solo `/health` | «Readiness real del modelo» y sondeos que nunca decodifican |
| E-A09-07 | Admisión por tokens con `/apply-template` + `/tokenize` y `n_ctx` por slot de `/props`, antes de enviar | Rechazo explícito y sin reintento; el 400 del servidor queda como respaldo con el mismo código |
| E-A09-08 | Reloj de inactividad = `max(ready_at, última terminación de trabajo)`; pendiente incluye reintentos y leases vencidos | Un trabajo completo entre dos pasadas también reinicia el reloj; un worker muerto no mantiene el host |
| E-A09-09 | Llegada durante `stopping`: se completa la parada y luego se vuelve a arrancar | Comportamiento simple y verificable; costo: un ciclo |
| E-A09-10 | En `auto`, una instancia encendida desde fuera se adopta; en `paused` el controlador no la toca | Evita dejarla encendida indefinidamente sin impedir el mantenimiento manual |
| E-A09-11 | Guardia de inactividad en la instancia (120 min, contadores de `/metrics`) con apagado como *stop* | Respaldo de costos ante un controlador caído |
| E-A09-12 | API key en SSM Parameter Store (SecureString), leída en cada arranque; on-premise, archivo 0600 | Ningún secreto en *user data*, plantilla ni repositorio |
| E-A09-13 | Endpoint privado por grupo de seguridad (8080 solo desde el SG o la CIDR de los clientes), sin SSH; IP pública opcional solo para salida sin NAT | «Endpoint privado» sin exigir NAT gateway |
| E-A09-14 | Todo en `deploy/inference/` con `pyproject.toml`/`uv.lock` propios; fuera de la wheel y del sdist | El controlador no es parte del núcleo (regla 2 de CLAUDE.md) |
| E-A09-15 | Despacho abierto = `ready` ∧ ningún sondeo fallido desde la última verificación. El primer fallo lo cierra; solo un sondeo completo exitoso lo reabre; `start` durante el drenaje vuelve a `starting`; el plazo de readiness se evalúa antes que el resultado del sondeo y con la hora posterior a él; un sondeo de un estado que cambió mientras corría se descarta | Revisión 1, hallazgo 1: el despacho seguía abierto hasta el umbral de fallos y la reanudación podía saltar el canario. El umbral (3) se conserva solo para volver a `starting` |
| E-A09-16 | VRAM/RAM muestreadas por `mem-sample.sh` **en el host medido**, arrancado y detenido por SSM alrededor de la misma carga; el resultado registra host, muestras y ventana, y es `unavailable` sin cifras si falta evidencia; el veredicto del perfil es `incomplete` en ese caso | Revisión 1, hallazgo 2: el muestreador leía la máquina del operador y devolvía 0 sin GPU |
| E-A09-17 | `bench/gpu-validation.py` (Python estándar, con `finally` y señales ignoradas durante la limpieza) ejecuta la validación de punta a punta desde `stopped`, disparada por un trabajo durable, y siempre verifica `stopped` (salida 4 visible si no). El perfil original se restituye y verifica por Run Command **con la instancia en `running`, antes de la parada** (en la limpieza, solo si sigue `running`); un fallo de la restitución se registra, no impide la parada y la corrida no se declara validada. `final-state.json` registra por separado el perfil y EC2. El script y sus hijos corren con el intérprete del entorno del controlador | Revisión 1, hallazgo 3: la receta anterior no tenía ciclo disparado por trabajo ni parada protegida y probaba perfiles con la instancia detenida. Revisión 2, defecto 1: la restitución corría después de `stop --wait`, cuando Run Command ya no llega a la instancia |
| E-A09-18 | `measure.py verdict --slots N [--reference]` exige evidencia de cada corrida esperada (`c1`; `cN` con N > 1 slots; `overload` en la referencia), identificada por `tag` y a su concurrencia, y que los slots del servidor coincidan con la definición del perfil. Corrida esperada ausente o archivo ilegible ⇒ `incomplete`; medición ejecutada y fallida ⇒ registro `status: failed` que da `fail`. `gpu-validation.py` deja siempre el archivo de cada medición y pasa todos al veredicto | Revisión 2, defecto 2: una corrida fallida se omitía de los archivos del veredicto, y un perfil de dos slots daba `pass` con evidencia de concurrencia 1 únicamente |

## 4. Propuestas pendientes de revisión del usuario

| Id | Propuesta |
|---|---|
| P-A09-01 | `readiness_timeout_s` = 1200 en AWS (900 on-premise), 3 intentos con *backoff* 300 s × 2ⁿ (tope 3600 s); después, bloqueo hasta un `start` manual. Se ajusta con la medición real de arranque y carga |
| P-A09-02 | `drain_timeout_s` = 1800; vencido, se detiene igual y el trabajo activo vuelve a la cola por vencimiento de su lease |
| P-A09-03 | Guardia de la instancia a 120 min |
| P-A09-04 | Predeterminado **provisional** `reference-8k-1slot` hasta medir en la L4 |
| P-A09-05 | Volumen de pesos gp3 40 GiB a 250 MiB/s (≈ 5 USD/mes de rendimiento extra a cambio de cargar 18 GiB en ≈ 75 s en lugar de ≈ 150 s; a medir) |

## 5. Preguntas y bloqueos

| Id | Cuestión | Estado |
|---|---|---|
| Q-A09-01 | La cuota «Running On-Demand G and VT instances» de la cuenta en `us-east-1` es **0 vCPU** (consultada con el perfil `rodrigo`); `g6.xlarge` necesita 4. Pedir el aumento es una acción sobre la cuenta que decide el usuario | **Bloquea** la validación en GPU real |
| Q-A09-02 | Autorización de gasto para la prueba en GPU (≈ 0,80 USD/h más EBS; receta estimada en 2–3 h) | abierta |
| Q-A09-03 | VPC/subred y grupo de seguridad de los clientes para el stack (privada con NAT, o pública con IP pública solo de salida) | abierta |
| Q-A09-04 | Licencia de los artefactos: el metadato de la corrida externa declara `apache-2.0` para el modelo; llama.cpp es MIT (archivo `LICENSE` del tarball). Confirmar la ficha del modelo antes de uso productivo | abierta |
| Q-A09-05 | Compatibilidad del driver de la DLAMI con la compilación CUDA 13.5; si no, usar la variante `cuda-12.8` y registrarlo | se resuelve al medir |
