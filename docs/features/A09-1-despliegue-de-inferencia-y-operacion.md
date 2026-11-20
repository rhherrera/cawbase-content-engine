# A09-1 — Despliegue de inferencia y operación

> **Estado: IMPLEMENTADA (infraestructura, controlador y ejecutor de prueba); validación en GPU real PENDIENTE; aceptación pendiente del usuario.**
>
> - **Documentos.** Contrato: [inference-deployment.md](../inference-deployment.md); decisiones: [A09-decisions](../decisions/A09-decisions.md); evidencia: [informe de A09-1](../a09-1-implementation-report.md); plan: [A09](../a09-plan.md).
> - **Ambientes.** Simulación probada en macOS y en Linux local (Docker). Artefactos reales del runtime verificados en Linux x64 local. **Sin** prueba en GPU real: la cuota de instancias G de la cuenta es 0 vCPU (Q-A09-01). **No** ejecutada en GitHub Actions.
> - **Paquete del motor.** Sin cambios (`0.7.0`; sin migración, permisos ni API nuevas).

- Rama `feature/a09-1-inference-infrastructure`, desde `main` en `55fc607`, con el plan documental `cf8fbb7` como primer commit.
- Dependencias: contrato A03 (no cambia); trabajos durables de A05 como modelo (el ejecutor de prueba es propio de A09-1). A09-2 integra el adaptador; B01 conecta la cola y los workers reales de CAWbase.

## 1. Objetivo, alcance y exclusiones

**Objetivo.** Un servidor de inferencia estructurada reproducible (AWS y on-premise) con el modelo y el runtime fijados, y un controlador externo que lo enciende ante trabajo durable, espera su readiness real, despacha solo cuando está listo y lo apaga tras 60 minutos continuos sin trabajo.

**Incluye:** manifiesto fijado; instaladores verificados; perfiles del servidor; unidad systemd; plantilla CloudFormation (`g6.xlarge`, endpoint privado, pesos en EBS); instalación on-premise; controlador con estado durable, lease y comandos `start`/`stop`/`status`; ejecutor de prueba con trabajos durables; guardia de inactividad en la instancia; herramientas de medición y paridad; pruebas con reloj controlado.

**Excluye:** adaptador del motor, prompts, segmentación, posiciones y evaluación formal (A09-2); cola y workers reales de CAWbase (B01); distribución de los despliegues de referencia (A10); cambios en `src/`; TLS del endpoint; multi-instancia o autoescalado.

## 2. Matriz de aceptación AC-A09-1

Niveles de evidencia: **S** = simulación (pruebas automáticas con servidor y backend falsos; macOS y Linux local); **A** = artefacto o servicio real fuera de la GPU (descarga verificada, binario en Linux x64, API de AWS de solo lectura, evaluador externo); **G** = hardware con GPU real; **D** = documental. El resultado está en el [informe](../a09-1-implementation-report.md#3-trazabilidad-ac-a09-1).

| AC | Garantía | Evidencia esperada |
|---|---|---|
| AC-A09-1-01 | Modelo fijado (repositorio, revisión, archivo, tamaño, SHA-256) y verificado al instalar; un hash o tamaño distinto se rechaza sin dejar archivo final | A (metadatos HF), S (instalador), G (descarga real) |
| AC-A09-1-02 | Runtime `b12329` Linux x64 CUDA fijado con procedencia y SHA-256; instalación verificada; dependencias del sistema detectadas; driver registrado | A (Linux x64), G (driver) |
| AC-A09-1-03 | Perfil de referencia reproducido: argumentos del servidor y forma de la solicitud (`response_format` por solicitud, temperatura 0, 3072 tokens) | S |
| AC-A09-1-04 | Evidencia externa conservada fuera de Git y reproducible desde las salidas crudas con el evaluador sin cambios | A |
| AC-A09-1-05 | Readiness real: health + alias + canario estructurado; una clave incorrecta o pesos cargando no son «listo»; los sondeos no son actividad | S |
| AC-A09-1-06 | Varias solicitudes y varios controladores concurrentes coordinan **un solo** arranque | S |
| AC-A09-1-07 | La API no espera el arranque en frío; los trabajos se conservan mientras se espera readiness | S |
| AC-A09-1-08 | Timeout y fallo de readiness: parada, reintento con *backoff*, bloqueo tras N intentos y desbloqueo manual; los trabajos siguen pendientes | S |
| AC-A09-1-09 | Timeout de inferencia separado del de readiness: se reintenta y no cambia la fase | S |
| AC-A09-1-10 | Autoapagado exactamente a los 60 min continuos sin pendientes ni activos; el health check no reinicia ni extiende el reloj; la última terminación de trabajo sí | S (reloj controlado) |
| AC-A09-1-11 | Apagado coordinado con llegadas: nunca hay trabajo activo en un host que se apaga por inactividad; lo que llega durante el apagado queda durable y vuelve a arrancar | S |
| AC-A09-1-12 | Recuperación tras reinicio o fallo del controlador (arranque o parada perdidos); un líder anterior no puede actuar | S |
| AC-A09-1-13 | `stop` pausa el autoarranque y drena; los pendientes siguen durables; `start` reanuda; `status` informa EC2, readiness y pausa | S (reglas + CLI con host simulado) |
| AC-A09-1-14 | Cola durable para el exceso de capacidad; rechazo explícito de entradas que exceden el contexto admitido; contexto por slot con dos slots | S, G |
| AC-A09-1-15 | Despliegue AWS reproducible: plantilla válida, endpoint privado, pesos persistentes en EBS, servidor al iniciar EC2, mínimo privilegio | A (validación de plantilla), G (stack real) |
| AC-A09-1-16 | Instalación on-premise con el mismo contrato, pesos locales y sin AWS | D, G |
| AC-A09-1-17 | Guardia de inactividad en la instancia (respaldo) | S (Linux) |
| AC-A09-1-18 | Mediciones en la L4: arranque EC2, carga de pesos y tiempo hasta readiness por separado; VRAM/RAM; latencia; throughput; un slot primero; elección del predeterminado | G |
| AC-A09-1-19 | Prueba real de inferencia estructurada con el modelo de referencia (canario y paridad) | G |
| AC-A09-1-20 | Permisos y costos residuales documentados | D, A (API de precios) |
| AC-A09-1-21 | Tras cualquier prueba con GPU, la instancia queda detenida | G (no aplica sin prueba) |
| AC-A09-1-22 | El paquete del motor no cambia y sus checks siguen en verde | S |

## 3. Reglas

- Ningún peso, ZIP, respuesta esperada ni resultado crudo entra en Git; las mediciones se escriben fuera del repositorio y se resumen en el informe.
- Un *skip* no es una aprobación; la simulación no sustituye la evidencia en GPU.
- Sin gasto en AWS sin autorización explícita del usuario; tras una prueba autorizada, verificar `stopped`.
