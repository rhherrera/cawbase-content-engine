# Validación AWS de A09-1 automatizada con GitHub Actions

> **Estado: IMPLEMENTADA y probada con AWS y GitHub SIMULADOS (macOS y Linux local), con las correcciones de la revisión 1 (limpieza solo de lo propio, parada en AWS desde la creación de la instancia, habilitación inicial como operación idempotente); NO ejecutada en GitHub Actions ni contra AWS real; la habilitación inicial (stack de acceso, variable del repositorio) NO se aplicó.** Las políticas IAM se validaron como plantilla con la API de AWS (`validate-template`), no con llamadas reales `AssumeRoleWithWebIdentity`/`CreateStack`. La primera solicitud con gasto va en una PR aparte, cuando el usuario autorice la red y el gasto. Pertenece a A09-1 ([contrato](inference-deployment.md), [especificación](features/A09-1-despliegue-de-inferencia-y-operacion.md), [decisiones](decisions/A09-decisions.md), [informe](a09-1-implementation-report.md)). A09-2 y B01 quedan fuera.

Automatiza los pasos A (preparación) y B (validación) del [§4 del informe](a09-1-implementation-report.md#4-validación-pendiente-en-gpu-real): el usuario **mergea una solicitud versionada** y el workflow hace el resto, sin ejecutar CloudFormation ni pasos de despliegue a mano. Reutiliza `deploy/inference/aws/cloudformation.yaml` y `deploy/inference/bench/gpu-validation.py`, y conserva el objetivo de D-A09-08: `g6e.xlarge` en `us-east-1`, Qwen3.6-27B Q8_0 con todos los pesos en GPU y el endpoint de inferencia privado.

| Pieza | Ubicación |
|---|---|
| Workflow de validación | [`.github/workflows/a09-1-aws-gpu-validation.yml`](../.github/workflows/a09-1-aws-gpu-validation.yml) |
| Diagnóstico OIDC (observa el `sub`) | [`.github/workflows/a09-1-oidc-diagnostic.yml`](../.github/workflows/a09-1-oidc-diagnostic.yml) |
| Orquestador (Python estándar, AWS por la CLI) | [`deploy/inference/ci/aws_validation.py`](../deploy/inference/ci/aws_validation.py) |
| IaC de la habilitación inicial | [`deploy/inference/aws/github-validation-access.yaml`](../deploy/inference/aws/github-validation-access.yaml) |
| Operación de habilitación (idempotente, perfil `rodrigo`) | [`deploy/inference/ci/enable_access.py`](../deploy/inference/ci/enable_access.py) |
| Solicitudes versionadas (vigiladas) | [`deploy/inference/validation-requests/`](../deploy/inference/validation-requests/README.md) |
| Ejemplo de la primera solicitud (no vigilado) | [`deploy/inference/ci/request.example.json`](../deploy/inference/ci/request.example.json) |

## 1. Flujo

```text
merge a main que AGREGA validation-requests/<id>.json
 └─ detect (sin AWS): `check` de todas las solicitudes, `detect` del archivo agregado (o nada)
     └─ validate (OIDC, credenciales temporales; timeout = limits.max_runtime_minutes)
         0. preflight sin efectos: reserva previa del id, plantilla al día, stack de acceso, red autorizada, cuota G/VT,
            oferta en la AZ, subred/VPC/AZ y ruta de salida, AMI DLAMI, stack (create|reuse), herramientas del runner,
            techo de gasto, ZIP de paridad (si se configuró)            → rechazo = salida 2, nada reservado ni creado
         1. reserva atómica del id en SSM (/cce/inference/validation-runs/<id>, sin --overwrite, con un token propio)
            → ya existe = salida 3
         2. paso A: API key nueva (SecureString); registro de propiedad del stack (antes de CreateStack); stack con el rol
            de ejecución y, DENTRO del stack, el schedule de parada (creado por CloudFormation con la instancia);
            CREATE_COMPLETE, SSM online, bootstrap ("units installed; server started"), primera carga (/health 200,
            journal con todas las capas en GPU), instancia DETENIDA (estado inicial). Con reuse: adopción explícita del
            stack y schedule armado por el runner con la instancia detenida, antes de cualquier arranque
         3. paso B: gpu-validation.py desde el runner por túnel SSM (ciclo por demanda, evidencia del host, offload,
            campaña de perfiles, veredictos, restitución del perfil, parada verificada)
         4. limpieza de lo PROPIO según la solicitud (keep-stopped | delete) y su verificación; redacción; artefacto
 └─ cleanup (siempre: también tras fallo, timeout o cancelación; credenciales propias): misma limpieza, idempotente
```

**Parada en AWS ligada a la creación.** El vencimiento y la parada de respaldo son un recurso del propio stack (`AWS::Scheduler::Schedule` `Watchdog` de `cloudformation.yaml`, parámetros `WatchdogStart`/`WatchdogEnd`/`WatchdogRoleArn`): EventBridge Scheduler llama directamente a `ec2:StopInstances` sobre `!Ref Host` (sin Lambda) cada 10 min durante 3 h, a partir de `reserva + max_runtime_minutes + 15 min`. Lo crea **CloudFormation** en la misma creación que la instancia; si no pudiera crearlo, el stack vuelve atrás (`--on-failure DELETE`) y la instancia se elimina. No hay ventana entre `CreateStack` y un armado hecho por el runner: si el runner se pierde apenas AWS aceptó `CreateStack` (antes de conocer el `StackId`), la instancia que aparece después igual queda detenida, sin el runner ni el job `cleanup`. El límite de gasto no cambia (el inicio se calcula desde la reserva, antes de `CreateStack`). Con `keep-stopped`, el schedule queda con el stack y deja de actuar en `WatchdogEnd`; con `delete`, se va con él. Un stack **reutilizado** (`reuse`) ya tiene su instancia, detenida: el runner arma un schedule propio (`cce-a09-<id>`) antes de que nada pueda arrancarla, y la limpieza lo borra solo tras verificarla detenida (si no, lo deja armado).

**Propiedad.** La limpieza solo detiene o borra lo que la solicitud **creó o adoptó**: (1) el id debe estar reservado (una solicitud rechazada por el preflight no toca nada); (2) el registro de propiedad del stack (`/cce/inference/validation-stacks/<stack>`) debe nombrar a esta reserva (id y token): lo escribe la ejecución `create` **antes** de `CreateStack` y lo sobrescribe una ejecución `reuse` al **adoptar** el stack; (3) el stack debe ser ese (`StackId` registrado o, si el runner se perdió antes de conocerlo, la etiqueta `cce:claim-token`); (4) solo se toca la instancia concreta de ese stack (etiqueta `aws:cloudformation:stack-id`), y el borrado es por `StackId`, nunca por nombre. Así, el reintento de una solicitud antigua no detiene ni borra un stack que otra adoptó después, ni un stack ajeno recreado con el mismo nombre; los parámetros por nombre (API key, registro) solo se borran si el nombre quedó libre. `reuse` exige un registro de propiedad coincidente: un stack desplegado a mano no se puede adoptar.

## 2. Garantías por modo de fallo

| Situación | Qué detiene la GPU | Qué se informa |
|---|---|---|
| La validación falla (medición, veredicto, offload parcial, bootstrap) | `gpu-validation.py` y la limpieza del `run` | job `validate` en rojo (salida 1); `cleanup-run.json` y el job `cleanup` verifican aparte |
| Límite de tiempo del job o cancelación | SIGINT/SIGTERM: limpieza del `run` si alcanza; luego el job `cleanup` (`if: always()`) | `validate` cancelado/fallido; `cleanup` con su propio estado |
| Runner muerto (sin `finally`), también apenas aceptado `CreateStack` | el schedule del stack (creado por CloudFormation con la instancia); el job `cleanup` en otro runner, que reconoce el stack por la etiqueta de la reserva | el registro del id queda sin `cleanup_job` |
| Solicitud no reservada (rechazo del preflight, p. ej. un stack preexistente) y luego el job `cleanup` | — | `cleanup.json`: `not_claimed`, nada detenido ni borrado |
| Reintento de una solicitud cuyo stack adoptó otra | — | `cleanup.json`: `not_owned`, recursos preservados |
| Falla la limpieza (stack no se borra, instancia no verificada) | schedule del stack (sigue con él) o del runner (no se desarma) | job `cleanup` en rojo (salida 5, `CLEANUP FAILED`), `cleanup.json` con lo que quedó |
| Reintento del mismo id (re-run o archivo re-agregado) | — (no se crea nada) | `validate` en rojo con `DUPLICATE … no new spend` (salida 3); `cleanup` vuelve a verificar |
| Rechazo del preflight | — (nada creado ni reservado) | `validate` en rojo (salida 2); se puede reejecutar el mismo run cuando se corrija la causa |

**Fallo de validación ≠ fallo de limpieza:** son jobs distintos con estados distintos; además `outcome.json` (`validation`, `cleanup_in_run`) y `cleanup.json` (`outcome`) los separan, y el resumen del job los muestra en líneas separadas. El registro del id en SSM guarda `status` (validación), `cleanup` (limpieza del `run`) y `cleanup_job`.

Respaldo adicional ya existente: la guardia de inactividad de la instancia (120 min sin actividad de inferencia, E-A09-11).

## 3. La solicitud

Un archivo `deploy/inference/validation-requests/<id>.json` por corrida; **todo campo de gasto o limpieza es explícito** (no hay valores por defecto silenciosos). Las claves desconocidas se rechazan. Comprobación local: `python3 deploy/inference/ci/aws_validation.py check` (sin AWS; Python ≥ 3.11).

| Campo | Regla |
|---|---|
| `schema_version` | `1` |
| `id` | `[a-z0-9-]`, 3–32, único; el archivo se llama `<id>.json` |
| `purpose` | texto |
| `authorization` | `spend_approved` y `network_approved` en `true`, `approved_by`, `approved_on` (AAAA-MM-DD), `max_spend_usd` ∈ (0, 100] y ≥ peor caso del límite de tiempo (§8) |
| `aws` | `region` = `us-east-1`; `instance_type` = `manifest.json aws.instance_type` (`g6e.xlarge`) |
| `model` | = `manifest.json targets.aws` (`qwen3.6-27b-q8_0`) |
| `network` | `vpc_id`, `subnet_id`, `availability_zone` (`us-east-1[a-f]`), `assign_public_ip`; opcionales `client_cidr` (privada) y `client_security_group_id`. Debe coincidir con la red del stack de acceso. Sin `client_*`, el puerto 8080 no admite a nadie: el runner llega por túnel SSM |
| `ami` | `"resolve-latest"` (parámetro público de la DLAMI, resuelto y registrado) o un `ami-…` fijado |
| `stack` | `name` (`cce-inference-…`; las políticas IAM se acotan a ese prefijo), `mode` (`create` = stack nuevo; `reuse` = adoptar uno creado por otra solicitud, detenido, con la misma plantilla y registro de propiedad coincidente), **`after_run`**: `keep-stopped` (se conserva detenido para A09-2) o `delete` (se elimina y se verifica) |
| `limits` | `max_runtime_minutes` 60–480 (límite del job y del watchdog); `bootstrap_timeout_minutes` 15–120 (por defecto 60) |
| `validation` | opcional: `profiles` (el primero es la referencia; por defecto los tres de §4 del informe), `server_profile`, `requests`, `prompt_tokens` |
| `parity` | `null` o `{"s3_uri": "s3://bucket/key.zip", "sha256": "…"}` (§7) |

Ejemplo de la primera corrida (fuera del directorio vigilado: copiarlo como `validation-requests/<id>.json`, completar la red autorizada y `approved_by`): [`request.example.json`](../deploy/inference/ci/request.example.json). Con 240 min, el peor caso de instancia es 1,861 USD/h × (240 + 25) min ≈ 8,22 USD.

## 4. Disparo, idempotencia y serialización

- **Disparo**: `push` a `main` con cambios bajo `deploy/inference/validation-requests/**`. `detect` toma `git diff --name-status --no-renames before..after` y ejecuta solo un `*.json` **agregado** (estado `A`). Un merge normal no dispara el workflow; un cambio al README o a una solicitud existente lo dispara, pero no ejecuta nada. Dos solicitudes nuevas en un mismo push: rechazo, nada se ejecuta. Un renombre aparece como archivo nuevo y lo frena la reserva del id.
- **Sin repetir el gasto**: el id se reserva con `ssm put-parameter` **sin** `--overwrite` (creación atómica) antes de crear nada; la reserva no se borra nunca (el rol no tiene `ssm:DeleteParameter` sobre ese prefijo). Reejecutar el workflow (también «Re-run failed jobs»), re-agregar el archivo o renombrarlo encuentra la reserva: salida 3, sin gasto, y el job `cleanup` vuelve a verificar. Para otra corrida: un id nuevo. Si el preflight rechaza la solicitud, el id **no** se reserva y el mismo run puede reejecutarse.
- **Serialización**: grupo de concurrencia `a09-1-aws-gpu-validation` sin cancelación; además el preflight exige 4 vCPU libres de la cuota G/VT (instancias G/VT `pending`/`running`/`stopping` de la cuenta). Límite de GitHub: un grupo conserva solo **un** run pendiente; uno pendiente más antiguo se cancela antes de empezar (nada reservado ni gastado) y hay que reejecutarlo al terminar el actual.
- **Sin `pull_request`**: una PR nunca obtiene credenciales (su `sub` es `repo:…:pull_request`, que el rol no admite). El workflow no usa GitHub Environments: en un repositorio privado de una cuenta Free sus reglas de aprobación no se aplican (criterio del backend de CAWbase; el plan de este repositorio no se verificó); la barrera es el `sub` de `main` que AWS evalúa en cada `AssumeRoleWithWebIdentity`. Consecuencia declarada: quien puede mergear a `main` puede disparar gasto, acotado por el techo de la solicitud, la cuota y el watchdog.

## 5. Habilitación inicial (una sola operación idempotente, perfil `rodrigo`)

Separada de las corridas y sin costo propio (roles, política, grupo de schedules, variable). El usuario la **autoriza una vez**; la ejecuta un agente autorizado con el perfil `rodrigo` (nunca `fede`), sin comandos de despliegue a mano. El workflow no puede modificar el stack de acceso (solo lo lee).

1. **Mergear la rama** (sin solicitudes `.json`: no se ejecuta ninguna validación). El diagnóstico OIDC corre solo al llegar su archivo a `main` e imprime los claims seguros y una línea `CCE_OIDC_CLAIMS={…}` (nunca el token, nunca AWS).
2. **Plan** (solo lectura) y **aplicación** con la misma operación:

   ```bash
   python3 deploy/inference/ci/enable_access.py plan  --repo OWNER/REPO --vpc-id vpc-… --subnet-id subnet-… --availability-zone us-east-1a --out /ruta/fuera/del/repo
   ```

   ```bash
   python3 deploy/inference/ci/enable_access.py apply --repo OWNER/REPO --vpc-id vpc-… --subnet-id subnet-… --availability-zone us-east-1a --out /ruta/fuera/del/repo
   ```

   Requisitos del agente: Python ≥ 3.11, AWS CLI v2 con el perfil `rodrigo`, `gh` autenticado con permiso para leer los runs y escribir variables del repositorio. Opcionales: `--dispatch` (si el diagnóstico nunca corrió, lo dispara en `main` y espera), `--diagnostic-run ID`, `--parity-object-arn`, `--stack-name`.

| Paso | Qué hace y verifica | Si falla |
|---|---|---|
| Identidad | `sts get-caller-identity` con el perfil `rodrigo`; las credenciales estáticas o de sesión del entorno y otro `AWS_PROFILE` se descartan | salida 2, nada cambia |
| Subject real | último run exitoso del diagnóstico en `main` (o el indicado), su línea `CCE_OIDC_CLAIMS`: `repository` = `--repo`, `ref` = `refs/heads/main`, `aud` = `sts.amazonaws.com`, `sub` con el patrón de la IaC (`SUB_PATTERN` = `AllowedPattern` de `GitHubSubject`, sin comodines, `pull_request` ni otras ramas); rechaza una plantilla de `sub` con `job_workflow_ref` (nombraría al diagnóstico, no al workflow de validación) | salida 2 |
| Proveedor OIDC | exactamente un `token.actions.githubusercontent.com` en la cuenta (se reutiliza) que acepta `sts.amazonaws.com` | salida 2 |
| Red elegida | VPC existente, subred en esa VPC y AZ, `g6e.xlarge` ofrecida en la AZ, ruta `0.0.0.0/0` activa (IGW o NAT) | salida 2 |
| Plan | parámetros del stack y diferencias con el stack actual (`create`, ninguna o las claves que cambian) | — |
| Despliegue (`apply`) | `cloudformation deploy --no-fail-on-empty-changeset` (sin cambios = no-op) | salida 1 |
| Verificación del trust y la red | `iam get-role`: una sola sentencia, `Federated` = proveedor reutilizado, `sts:AssumeRoleWithWebIdentity`, `StringEquals` **exactamente** `aud` + el `sub` observado; ARN = salida `ValidationRoleArn`; sesión ≤ 12 h; salidas `GitHubSubject`, `AllowedVpcId` y `AllowedSubnetId` = lo observado y lo elegido | salida 1 y **la variable no se escribe** |
| Variable del repositorio | `gh variable set CCE_A09_AWS_ROLE_ARN` (y `CCE_A09_ACCESS_STACK` si el stack no es `cce-a09-github-access`) solo si cambió, y lectura de vuelta | salida 1 |

Repetirla no cambia nada (stack sin cambios, variable igual) y vuelve a verificar todo. Deja `enablement.json` en `--out` (claims seguros, proveedor, red, plan, salidas, verificaciones; sin secretos). El stack de acceso crea `cce-a09-github-validation` (rol OIDC, sesión ≤ 12 h), `cce-a09-cfn-execution` (CloudFormation crea y borra los stacks `cce-inference-*` y su schedule), `cce-a09-host-boundary`, `cce-a09-watchdog` y el grupo de schedules `cce-inference-watchdog`.

| Variable del repositorio | Obligatoria | Valor |
|---|---|---|
| `CCE_A09_AWS_ROLE_ARN` | sí | salida `ValidationRoleArn` (la escribe la operación) |
| `CCE_A09_ACCESS_STACK` | no | nombre del stack de acceso si no es `cce-a09-github-access` |

No se guarda ninguna credencial de AWS en GitHub. En cada corrida, `oidc-claims.json` registra el `sub` que presentó el job; un `AccessDenied` en `configure-aws-credentials` con un `sub` distinto se corrige repitiendo la operación con un diagnóstico nuevo (nunca ampliando el trust con comodines).

**Endurecimiento opcional, no verificado (P-A09-06)**: personalizar la plantilla del `sub` para incluir `job_workflow_ref` haría que solo el workflow de validación pueda asumir el rol; necesita observar el `sub` del workflow de validación (no el del diagnóstico), por eso la operación lo rechaza hoy.

## 6. Permisos (mínimo privilegio, acotado por recurso y etiqueta)

| Principal | Puede | No puede |
|---|---|---|
| `cce-a09-github-validation` (OIDC, temporal) | `CreateStack` de `cce-inference-*` **solo** con el rol de ejecución (`cloudformation:RoleArn`), `DeleteStack`/`Describe*`; leer el stack de acceso; `PassRole` del rol de ejecución (a CloudFormation) y del watchdog (a Scheduler, para stacks reutilizados); `Start/StopInstances`, `SendCommand` (`AWS-RunShellScript`) y `StartSession` (`AWS-StartPortForwardingSession`) solo en instancias con `cce:validation-id`; parámetros `/cce/inference/validation-runs/*` (crear y leer), `/cce/inference/validation-stacks/cce-inference-*` (registros de propiedad) y `/cce/inference/validation/cce-inference-*` (API key por stack); crear y borrar schedules `cce-a09-*`, leer los del grupo; `Describe*` de lectura; el ZIP de paridad si se configuró | `UpdateStack`; cualquier otra acción IAM; borrar la reserva de un id; tocar el stack de acceso |
| `cce-a09-cfn-execution` (CloudFormation) | crear y borrar los recursos de `cloudformation.yaml`: `RunInstances` solo `g6e.xlarge` y solo en la subred autorizada, grupos de seguridad solo en la VPC autorizada, volúmenes cifrados, rol del host `cce-inference-*` **solo con el límite** `cce-a09-host-boundary` y solo con `AmazonSSMManagedInstanceCore` adjunta, el schedule `cce-inference-*` del grupo `cce-inference-watchdog` y `PassRole` del rol del watchdog a Scheduler | quitar o cambiar el límite de permisos; otros tipos de instancia o subredes |
| Rol del host (por stack) | lo de antes (agente SSM, leer su API key), acotado por el límite | nada fuera de SSM |
| `cce-a09-watchdog` (Scheduler) | `ec2:StopInstances` en instancias `cce:role = inference` con `cce:validation-id` | nada más |

La plantilla del host agrega los parámetros opcionales `HostPermissionsBoundaryArn` y `WatchdogStart`/`WatchdogEnd`/`WatchdogRoleArn`/`WatchdogScheduleGroup` (vacíos = sin límite ni schedule, como el despliegue manual; el *user data* no cambia).

## 7. Evidencia, secretos y paridad

Artefactos del run (90 días): `a09-1-validation-<id>-attempt-<n>` y `a09-1-cleanup-<id>-attempt-<n>`.

| Archivo | Contenido |
|---|---|
| `request.json`, `runner-tools.txt`, `oidc-claims.json` | la solicitud; versiones de `aws`, `session-manager-plugin` y Python; claims seguros del job |
| `preflight.json`, `claim.json` | comprobaciones (cuota y uso, red y ruta de salida, AMI, hash de la plantilla, peor caso de gasto); reserva del id |
| `provision.json`, `watchdog.json` | parámetros del stack (sin secretos), salidas, registro de propiedad (sin token), línea de tiempo (pedido del stack, instancia creada, stack completo, SSM online, bootstrap, primera carga sana, instancia detenida), primera carga en GPU; schedule de parada (`in_stack` con la instancia a la que apunta, o `runner` en reuse) |
| `bootstrap.log`, `cloud-init-status.txt`, `bootstrap-server-journal.log`, `bootstrap-gpu-offload.json` | primer arranque: marcas de `bootstrap.sh`, estado de cloud-init, journal de la primera carga y **`offloaded N/N layers to GPU`** |
| `gpu-validation/` | toda la evidencia de `gpu-validation.py` (contrato §6.2): `job-cycle.json`, `host-facts.json`, `boot-timings.json`, `model-record.json`, `server-journal-{job-cycle,campaign}.log` y `gpu-offload-*.json` (cada carga con todas las capas en GPU), `load-*`, `limits-*`, `run-*`, `verdict-*`, `final-state.json`, `summary.json` |
| `outcome.json`, `cleanup-run.json`, `cleanup.json` | veredicto de la validación, limpieza del `run` y limpieza del job `cleanup`, por separado; la limpieza registra `ownership` (`owned`, `not_claimed`, `not_owned`) y lo que preservó |

**Secretos**: la API key se genera en el runner, va a SSM como SecureString y a un archivo 0600 en `$RUNNER_TEMP/a09-work/secrets` (fuera de la evidencia); nunca se imprime. Antes de subir, `redact` reemplaza toda aparición de la API key y de las credenciales del entorno (`AWS_*`, tokens de GitHub) y elimina archivos con nombre de clave; si algo quedara, borra el archivo y falla. Las credenciales AWS son temporales (OIDC); la cuenta se enmascara en los logs.

**Paridad (ZIP opcional, fuera de Git)**: si `parity` está configurado, el preflight descarga el ZIP de S3 (permiso de lectura solo sobre ese objeto), verifica su SHA-256 y lo descomprime en el runner fuera de la evidencia (rechazo sin gasto si no coincide); `gpu-validation.py` ejecuta `parity.sh` sobre el perfil de referencia. Las predicciones crudas (`evaluacion-r*-i*.jsonl`) y el log del proxy reproducen el corpus privado: **no se suben** (se registra su SHA-256 en `withheld.json`); se suben métricas y `summary.json`. Ni el corpus ni los pesos entran nunca en Git ni en los artefactos.

**Sin ZIP**, la corrida da: ciclo disparado por un trabajo durable con inferencia estructurada real (canario y trabajo sintético), artefacto Q8_0 verificado en el host, carga de todas las capas en GPU (journal), tiempos de arranque EC2/SO/pesos/readiness, mediciones de memoria en el host, latencia, throughput, límites y veredictos por perfil, restitución del perfil y parada verificada (AC-A09-1-15, 18, 21, 23, 24, 25 y la parte estructurada de 19). **Queda pendiente**: la calidad de Q8_0 con la metodología de la referencia (parte de AC-A09-1-19 y AC-A09-1-26); `outcome.json` lo dice (`parity.status = pending`).

## 8. Costos

Peor caso de instancia por solicitud = 1,861 USD/h (`manifest.json aws.on_demand_usd_per_hour`) × (`max_runtime_minutes` + 15 + 10) / 60; debe ser ≤ `max_spend_usd` o la solicitud se rechaza. Con `keep-stopped`, el stack detenido cuesta ≈ 14,6 USD/mes de EBS (contrato §4.2) hasta que se elimine. El watchdog, los roles, los parámetros estándar de SSM y la reserva de ids no tienen costo apreciable.

## 9. Pruebas y límites

**Probado con AWS simulado** (`deploy/inference/controller/tests/test_ci_validation.py`, con el `aws` falso de `tests/fake_aws.py` y el host simulado; revisión 1: solicitud no reservada sobre un stack preexistente, stack ajeno recreado con el mismo nombre, stack adoptado por otra solicitud, reintento antiguo que preserva lo adoptado y deja que el dueño actual limpie, `reuse` de un stack sin registro, runner perdido apenas AWS aceptó `CreateStack` y la instancia detenida solo por el schedule del stack; `test_enable_access.py` con el `gh` falso de `tests/fake_gh.py`: plan sin cambios, aplicación verificada e idempotente, diagnóstico disparado, doce rechazos sin cambios, trust que no coincide sin escribir la variable, cambio de red): detección sobre historia git real (merge normal, README, solicitud nueva, modificada, renombrada, dos a la vez, push sin commit previo); esquema y techo de gasto; claims OIDC sin token; corrida completa `delete` (stack con los parámetros de la solicitud y el rol de ejecución, watchdog, bootstrap, primera carga en GPU, instancia detenida, `gpu-validation.py`, borrado verificado, API key borrada, watchdog desarmado; el host simulado lee la API key de SSM como el real); `keep-stopped`, reintento del mismo id sin gasto y una solicitud nueva que reutiliza el stack; doce rechazos del preflight sin reservar ni crear nada; fallo de validación con limpieza verificada (separados); offload parcial; fallo de limpieza (salida 5, watchdog armado); SIGTERM durante el bootstrap; runner muerto con SIGKILL y el watchdog que detiene la instancia; redacción. Contrato estático del workflow y de la IaC en `tests/test_a09_aws_validation_ci.py`; `actionlint` (con shellcheck) sin hallazgos; ambas plantillas aceptadas por `aws cloudformation validate-template`.

**No verificado** (requiere la habilitación y la primera corrida real):

- que las políticas IAM alcancen (o no sobren) en una creación real: `validate-template` no evalúa la semántica de IAM; un faltante se vería como `AccessDenied` temprano (antes de la GPU o con la instancia detenida por la limpieza);
- el `sub` real del repositorio y el trust con `AssumeRoleWithWebIdentity`;
- que `session-manager-plugin` esté en la imagen `ubuntu-latest` (el preflight lo exige y rechaza sin gasto; queda en `runner-tools.txt`);
- EventBridge Scheduler real (universal target `ec2:stopInstances`), el recurso `AWS::Scheduler::Schedule` creado por CloudFormation (formato de `StartDate`/`EndDate`, rollback si falla), la propagación de las etiquetas del stack a la instancia y el borrado real del stack;
- la operación de habilitación contra AWS y GitHub reales (formato real del log de `gh run view --log`, `gh variable list`, `iam get-role`);
- GitHub Actions (nunca ejecutado) y el comportamiento real de cancelación/timeout del runner.
