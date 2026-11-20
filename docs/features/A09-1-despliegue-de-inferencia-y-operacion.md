# A09-1 — Despliegue de inferencia y operación

> **Estado: IMPLEMENTADA (infraestructura, controlador y ejecutor de prueba), con las correcciones de la revisión 1 (readiness y despacho, evidencia de memoria y secuencia de validación) y de la revisión 2 (restitución del perfil antes de la parada e integridad del veredicto), y con el **objetivo AWS cambiado por el usuario a `g6e.xlarge` (L40S) + Q8_0** (D-A09-08; on-premise sigue con Q4_K_M); validación en GPU real PENDIENTE; aceptación pendiente del usuario.**
>
> - **Documentos.** Contrato: [inference-deployment.md](../inference-deployment.md); decisiones: [A09-decisions](../decisions/A09-decisions.md); evidencia: [informe de A09-1](../a09-1-implementation-report.md); plan: [A09](../a09-plan.md).
> - **Ambientes.** Simulación probada en macOS y en Linux local (Docker). Artefactos reales del runtime verificados en Linux x64 local. **Sin** prueba en GPU real: la cuota de instancias G/VT de la cuenta es 0 vCPU, con la solicitud de aumento abierta (Q-A09-01). **No** ejecutada en GitHub Actions.
> - **Paquete del motor.** Sin cambios (`0.7.0`; sin migración, permisos ni API nuevas).

- Rama `feature/a09-1-inference-infrastructure`, desde `main` en `55fc607`, con el plan documental `cf8fbb7` como primer commit.
- Dependencias: contrato A03 (no cambia); trabajos durables de A05 como modelo (el ejecutor de prueba es propio de A09-1). A09-2 integra el adaptador; B01 conecta la cola y los workers reales de CAWbase.

## 1. Objetivo, alcance y exclusiones

**Objetivo.** Un servidor de inferencia estructurada reproducible (AWS y on-premise) con el modelo y el runtime fijados, y un controlador externo que lo enciende ante trabajo durable, espera su readiness real, despacha solo cuando está listo y lo apaga tras 60 minutos continuos sin trabajo.

**Incluye:** manifiesto fijado; instaladores verificados; perfiles del servidor; unidad systemd; plantilla CloudFormation (`g6e.xlarge` con Q8_0; antes `g6.xlarge` con Q4_K_M, histórico; endpoint privado, pesos en EBS); instalación on-premise; controlador con estado durable, lease y comandos `start`/`stop`/`status`; ejecutor de prueba con trabajos durables; guardia de inactividad en la instancia; herramientas de medición (memoria muestreada en el host) y paridad; secuencia de validación en GPU con parada protegida; pruebas con reloj controlado y con herramientas AWS simuladas.

**Excluye:** adaptador del motor, prompts, segmentación, posiciones y evaluación formal (A09-2); cola y workers reales de CAWbase (B01); distribución de los despliegues de referencia (A10); cambios en `src/`; TLS del endpoint; multi-instancia o autoescalado.

## 2. Matriz de aceptación AC-A09-1

Niveles de evidencia: **S** = simulación (pruebas automáticas con servidor y backend falsos; macOS y Linux local); **A** = artefacto o servicio real fuera de la GPU (descarga verificada, binario en Linux x64, API de AWS de solo lectura, evaluador externo); **G** = hardware con GPU real; **D** = documental. El resultado está en el [informe](../a09-1-implementation-report.md#3-trazabilidad-ac-a09-1).

| AC | Garantía | Evidencia esperada |
|---|---|---|
| AC-A09-1-01 | Modelo fijado (repositorio, revisión, archivo, tamaño, SHA-256) y verificado al instalar; un hash o tamaño distinto se rechaza sin dejar archivo final | A (metadatos HF), S (instalador), G (descarga real) |
| AC-A09-1-02 | Runtime `b12329` Linux x64 CUDA fijado con procedencia y SHA-256; instalación verificada; dependencias del sistema detectadas; driver registrado | A (Linux x64), G (driver) |
| AC-A09-1-03 | Perfil de referencia reproducido: argumentos del servidor y forma de la solicitud (`response_format` por solicitud, temperatura 0, 3072 tokens) | S |
| AC-A09-1-04 | Evidencia externa conservada fuera de Git y reproducible desde las salidas crudas con el evaluador sin cambios | A |
| AC-A09-1-05 | Readiness real: health + alias + canario estructurado; una clave incorrecta o pesos cargando no son «listo»; los sondeos no son actividad. El despacho se abre solo con readiness **verificada** antes del plazo del intento (un canario tardío o de un intento anterior no cuenta); el primer sondeo fallido lo cierra y solo un sondeo completo exitoso lo reabre, también al reanudar durante el drenaje; el umbral de fallos decide el reinicio | S, G (canario real) |
| AC-A09-1-06 | Varias solicitudes y varios controladores concurrentes coordinan **un solo** arranque | S |
| AC-A09-1-07 | La API no espera el arranque en frío; los trabajos se conservan mientras se espera readiness | S |
| AC-A09-1-08 | Timeout y fallo de readiness: parada, reintento con *backoff*, bloqueo tras N intentos y desbloqueo manual; los trabajos siguen pendientes | S |
| AC-A09-1-09 | Timeout de inferencia separado del de readiness: se reintenta y no cambia la fase | S |
| AC-A09-1-10 | Autoapagado exactamente a los 60 min continuos sin pendientes ni activos; el health check no reinicia ni extiende el reloj; la última terminación de trabajo sí | S (reloj controlado) |
| AC-A09-1-11 | Apagado coordinado con llegadas: nunca hay trabajo activo en un host que se apaga por inactividad; lo que llega durante el apagado queda durable y vuelve a arrancar | S |
| AC-A09-1-12 | Recuperación tras reinicio o fallo del controlador (arranque o parada perdidos); un líder anterior no puede actuar | S |
| AC-A09-1-13 | `stop` pausa el autoarranque y drena; los pendientes siguen durables; `start` reanuda (durante el drenaje, revalidando readiness sin cortar el trabajo activo); `status` informa EC2, readiness, pausa y sondeos fallidos | S (reglas + CLI con host simulado) |
| AC-A09-1-14 | Cola durable para el exceso de capacidad; rechazo explícito de entradas que exceden el contexto admitido; contexto por slot con dos slots | S, G |
| AC-A09-1-15 | Despliegue AWS reproducible: plantilla válida, endpoint privado, pesos persistentes en EBS, servidor al iniciar EC2, mínimo privilegio | A (validación de plantilla), G (stack real) |
| AC-A09-1-16 | Instalación on-premise con el mismo contrato, pesos locales y sin AWS | D, G |
| AC-A09-1-17 | Guardia de inactividad en la instancia (respaldo) | S (Linux) |
| AC-A09-1-18 | Mediciones en la L40S con Q8_0 (antes L4 con Q4_K_M, histórico): arranque EC2, carga de pesos y tiempo hasta readiness por separado; VRAM/RAM medidas en el host (AC-A09-1-23); latencia; throughput; un slot primero; elección del predeterminado | G |
| AC-A09-1-19 | Prueba real de inferencia estructurada con el modelo del destino (AWS: Q8_0; canario y calidad con la metodología de la referencia) | G |
| AC-A09-1-20 | Permisos y costos residuales documentados | D, A (API de precios) |
| AC-A09-1-21 | Tras cualquier prueba con GPU, la instancia queda detenida: la secuencia de validación la detiene y la verifica también ante fallo o interrupción, y un fallo de la limpieza es visible | S (secuencia simulada), G (no aplica sin prueba) |
| AC-A09-1-22 | El paquete del motor no cambia y sus checks siguen en verde | S |
| AC-A09-1-23 | La VRAM/RAM se muestrea en el host medido durante la misma carga; se registran el host y la cantidad de muestras; sin muestras (o ventana no cubierta, u otro host) el resultado es «medición no disponible» y el criterio de memoria no aprueba el perfil. El veredicto de cada perfil exige evidencia de todas sus corridas esperadas (concurrencia 1; concurrencia 2 con dos slots; sobrecarga en el perfil de referencia): una corrida ausente da `incomplete` y una ejecutada y fallida queda registrada y da `fail` | S, G |
| AC-A09-1-24 | Secuencia de validación reproducible: parte de `stopped`, un trabajo durable dispara el arranque y se verifican readiness y resultado; los perfiles se prueban con la instancia disponible; el perfil original se restituye y verifica con la instancia en `running`, antes de la parada; la parada final está protegida ante fallos e interrupciones (también si falla la restitución) y se verifica `stopped`; perfil restituido y EC2 detenida se registran por separado y una restitución fallida impide declarar validada la corrida | S (herramientas simuladas), G |
| AC-A09-1-25 | El despliegue AWS selecciona y verifica Q8_0 de punta a punta: el manifiesto es la única fuente del artefacto (revisión, archivo, URL inmutable, tamaño, SHA-256); la plantilla (`g6e.xlarge` por defecto, volumen de pesos dimensionado para el artefacto y su descarga temporal, *user data* ≤ 16 KiB), el bootstrap, el instalador (atómico, con tamaño, SHA-256 y espacio), `server.env`, la verificación previa al arranque, el alias del controlador, las mediciones y la validación usan Q8_0; una discrepancia de artefacto (id, archivo, alias, tamaño, hash, registro o espacio) falla de forma explícita; la instalación on-premise Q4_K_M existente sigue funcionando sin AWS | S (herramientas simuladas, manifiesto de prueba), G (descarga y carga reales en la L40S) |
| AC-A09-1-26 | La calidad de Q8_0 se informa por separado del 43/50 histórico de Q4_K_M, con la metodología de la referencia (corpus, prompt, parámetros) y el artefacto exacto registrado junto a cada resultado | D, G |

## 3. Reglas

- Ningún peso, ZIP, respuesta esperada ni resultado crudo entra en Git; las mediciones se escriben fuera del repositorio y se resumen en el informe.
- Un *skip* no es una aprobación; la simulación no sustituye la evidencia en GPU.
- Sin gasto en AWS sin autorización explícita del usuario; tras una prueba autorizada, verificar `stopped`.
