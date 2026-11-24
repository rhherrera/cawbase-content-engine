# Validación AWS de A09-1 automatizada con GitHub Actions

> **Estado: IMPLEMENTADA y probada con AWS SIMULADO (macOS y Linux local); NO ejecutada en GitHub Actions ni contra AWS real; la habilitación inicial (stack de acceso, variable del repositorio) NO se aplicó.** Las políticas IAM se validaron como plantilla con la API de AWS (`validate-template`), no con llamadas reales `AssumeRoleWithWebIdentity`/`CreateStack`. La primera solicitud con gasto va en una PR aparte, cuando el usuario autorice la red y el gasto. Pertenece a A09-1 ([contrato](inference-deployment.md), [especificación](features/A09-1-despliegue-de-inferencia-y-operacion.md), [decisiones](decisions/A09-decisions.md), [informe](a09-1-implementation-report.md)). A09-2 y B01 quedan fuera.

Automatiza los pasos A (preparación) y B (validación) del [§4 del informe](a09-1-implementation-report.md#4-validación-pendiente-en-gpu-real): el usuario **mergea una solicitud versionada** y el workflow hace el resto, sin ejecutar CloudFormation ni pasos de despliegue a mano. Reutiliza `deploy/inference/aws/cloudformation.yaml` y `deploy/inference/bench/gpu-validation.py`, y conserva el objetivo de D-A09-08: `g6e.xlarge` en `us-east-1`, Qwen3.6-27B Q8_0 con todos los pesos en GPU y el endpoint de inferencia privado.

| Pieza | Ubicación |
|---|---|
| Workflow de validación | [`.github/workflows/a09-1-aws-gpu-validation.yml`](../.github/workflows/a09-1-aws-gpu-validation.yml) |
| Diagnóstico OIDC (observa el `sub`) | [`.github/workflows/a09-1-oidc-diagnostic.yml`](../.github/workflows/a09-1-oidc-diagnostic.yml) |
| Orquestador (Python estándar, AWS por la CLI) | [`deploy/inference/ci/aws_validation.py`](../deploy/inference/ci/aws_validation.py) |
| IaC de la habilitación inicial | [`deploy/inference/aws/github-validation-access.yaml`](../deploy/inference/aws/github-validation-access.yaml) |
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
         1. reserva atómica del id en SSM (/cce/inference/validation-runs/<id>, sin --overwrite) → ya existe = salida 3
         2. paso A: API key nueva (SecureString), stack con el rol de ejecución, watchdog en AWS, CREATE_COMPLETE, SSM
            online, bootstrap ("units installed; server started"), primera carga (/health 200, journal con todas las
            capas en GPU), instancia DETENIDA (estado inicial)
         3. paso B: gpu-validation.py desde el runner por túnel SSM (ciclo por demanda, evidencia del host, offload,
            campaña de perfiles, veredictos, restitución del perfil, parada verificada)
         4. limpieza según la solicitud (keep-stopped | delete) y su verificación; redacción; artefacto
 └─ cleanup (siempre: también tras fallo, timeout o cancelación; credenciales propias): misma limpieza, idempotente
```

El watchdog es un schedule de **EventBridge Scheduler** en AWS que llama directamente a `ec2:StopInstances` sobre la instancia (sin Lambda) cada 10 min durante 3 h, a partir de `inicio + max_runtime_minutes + 15 min`. No depende del runner: si GitHub no ejecuta ni la limpieza del job ni el job `cleanup`, la instancia igual se detiene. La limpieza lo borra solo después de verificar la instancia detenida; si no pudo verificarla, lo deja armado.

## 2. Garantías por modo de fallo

| Situación | Qué detiene la GPU | Qué se informa |
|---|---|---|
| La validación falla (medición, veredicto, offload parcial, bootstrap) | `gpu-validation.py` y la limpieza del `run` | job `validate` en rojo (salida 1); `cleanup-run.json` y el job `cleanup` verifican aparte |
| Límite de tiempo del job o cancelación | SIGINT/SIGTERM: limpieza del `run` si alcanza; luego el job `cleanup` (`if: always()`) | `validate` cancelado/fallido; `cleanup` con su propio estado |
| Runner muerto (sin `finally`) | job `cleanup` en otro runner; si tampoco corre, el watchdog en AWS | el registro del id queda sin `cleanup_job` |
| Falla la limpieza (stack no se borra, instancia no verificada) | watchdog armado (no se desarma) | job `cleanup` en rojo (salida 5, `CLEANUP FAILED`), `cleanup.json` con lo que quedó |
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
| `stack` | `name` (`cce-inference-…`; las políticas IAM se acotan a ese prefijo), `mode` (`create` = stack nuevo; `reuse` = uno existente, detenido y creado con la misma plantilla), **`after_run`**: `keep-stopped` (se conserva detenido para A09-2) o `delete` (se elimina y se verifica) |
| `limits` | `max_runtime_minutes` 60–480 (límite del job y del watchdog); `bootstrap_timeout_minutes` 15–120 (por defecto 60) |
| `validation` | opcional: `profiles` (el primero es la referencia; por defecto los tres de §4 del informe), `server_profile`, `requests`, `prompt_tokens` |
| `parity` | `null` o `{"s3_uri": "s3://bucket/key.zip", "sha256": "…"}` (§7) |

Ejemplo de la primera corrida (fuera del directorio vigilado: copiarlo como `validation-requests/<id>.json`, completar la red autorizada y `approved_by`): [`request.example.json`](../deploy/inference/ci/request.example.json). Con 240 min, el peor caso de instancia es 1,861 USD/h × (240 + 25) min ≈ 8,22 USD.

## 4. Disparo, idempotencia y serialización

- **Disparo**: `push` a `main` con cambios bajo `deploy/inference/validation-requests/**`. `detect` toma `git diff --name-status --no-renames before..after` y ejecuta solo un `*.json` **agregado** (estado `A`). Un merge normal no dispara el workflow; un cambio al README o a una solicitud existente lo dispara, pero no ejecuta nada. Dos solicitudes nuevas en un mismo push: rechazo, nada se ejecuta. Un renombre aparece como archivo nuevo y lo frena la reserva del id.
- **Sin repetir el gasto**: el id se reserva con `ssm put-parameter` **sin** `--overwrite` (creación atómica) antes de crear nada; la reserva no se borra nunca (el rol no tiene `ssm:DeleteParameter` sobre ese prefijo). Reejecutar el workflow (también «Re-run failed jobs»), re-agregar el archivo o renombrarlo encuentra la reserva: salida 3, sin gasto, y el job `cleanup` vuelve a verificar. Para otra corrida: un id nuevo. Si el preflight rechaza la solicitud, el id **no** se reserva y el mismo run puede reejecutarse.
- **Serialización**: grupo de concurrencia `a09-1-aws-gpu-validation` sin cancelación; además el preflight exige 4 vCPU libres de la cuota G/VT (instancias G/VT `pending`/`running`/`stopping` de la cuenta). Límite de GitHub: un grupo conserva solo **un** run pendiente; uno pendiente más antiguo se cancela antes de empezar (nada reservado ni gastado) y hay que reejecutarlo al terminar el actual.
- **Sin `pull_request`**: una PR nunca obtiene credenciales (su `sub` es `repo:…:pull_request`, que el rol no admite). El workflow no usa GitHub Environments: en un repositorio privado de una cuenta Free sus reglas de aprobación no se aplican (criterio del backend de CAWbase; el plan de este repositorio no se verificó); la barrera es el `sub` de `main` que AWS evalúa en cada `AssumeRoleWithWebIdentity`. Consecuencia declarada: quien puede mergear a `main` puede disparar gasto, acotado por el techo de la solicitud, la cuota y el watchdog.

## 5. Habilitación inicial (una sola vez, identidad ya autorizada)

Separada de las corridas: la ejecuta el usuario con una identidad ya autorizada en la cuenta (perfil `rodrigo`, nunca `fede`); el workflow no puede modificar el stack de acceso (solo lo lee). No tiene costo propio (roles, política, grupo de schedules).

1. **Mergear esta rama** (sin solicitudes `.json`: no se ejecuta ninguna validación). El diagnóstico OIDC corre al llegar su archivo a `main` (y luego a demanda con `workflow_dispatch`, solo en `main`).
2. **Observar el `sub` real**: en el resumen o el log del run de `a09-1-oidc-diagnostic`, copiar `sub` **tal cual**. No se infiere: según el repositorio es `repo:OWNER/REPO:ref:refs/heads/main` o la forma con ids inmutables (`repo:OWNER@ID/REPO@ID:ref:…`), o una plantilla personalizada. El diagnóstico nunca imprime el token ni llama a AWS. Con la plantilla por defecto, un `push` y un `workflow_dispatch` en `main` presentan el mismo `sub`.
3. **Desplegar el stack de acceso** (reutiliza el proveedor OIDC existente de la cuenta; la red es la que el usuario autorice, Q-A09-03):

   ```bash
   export AWS_PROFILE=rodrigo AWS_REGION=us-east-1
   PROVIDER=$(aws iam list-open-id-connect-providers --query "OpenIDConnectProviderList[?ends_with(Arn, '/token.actions.githubusercontent.com')].Arn" --output text)
   aws cloudformation deploy --stack-name cce-a09-github-access \
     --template-file deploy/inference/aws/github-validation-access.yaml --capabilities CAPABILITY_NAMED_IAM \
     --parameter-overrides GitHubOidcProviderArn="$PROVIDER" GitHubSubject='<sub observado>' \
       VpcId=vpc-… SubnetId=subnet-…   # [ParityObjectArn=arn:aws:s3:::BUCKET/clave.zip]
   aws cloudformation describe-stacks --stack-name cce-a09-github-access --query 'Stacks[0].Outputs'
   ```

   El parámetro `GitHubSubject` rechaza comodines, sujetos de `pull_request` y otras ramas. Crea: `cce-a09-github-validation` (rol OIDC, sesión ≤ 12 h), `cce-a09-cfn-execution` (rol con el que CloudFormation crea y borra los stacks `cce-inference-*`), `cce-a09-host-boundary` (límite de permisos del rol del host), `cce-a09-watchdog` y el grupo de schedules `cce-inference-watchdog`.
4. **Variable del repositorio** (Settings → Secrets and variables → Actions → Variables; no es un secreto):

   | Variable | Obligatoria | Valor |
   |---|---|---|
   | `CCE_A09_AWS_ROLE_ARN` | sí | salida `ValidationRoleArn` |
   | `CCE_A09_ACCESS_STACK` | no | nombre del stack de acceso si no es `cce-a09-github-access` |

   No se guarda ninguna credencial de AWS en GitHub.
5. **Verificar el trust**: la salida `GitHubSubject` del stack debe ser idéntica al `sub` del diagnóstico. En cada corrida, `oidc-claims.json` de la evidencia registra el `sub` que presentó el job (solo claims seguros); un `AccessDenied` en `configure-aws-credentials` con un `sub` distinto indica que hay que corregir el parámetro (nunca ampliarlo con comodines).
6. **Endurecimiento opcional, no verificado**: para que solo este workflow (y no otro de `main`) pueda asumir el rol, personalizar la plantilla del `sub` del repositorio para incluir `job_workflow_ref` (API de personalización OIDC de GitHub), volver a observar el `sub` con el diagnóstico y redeplegar con ese valor (el patrón lo admite). No se aplicó ni se probó.

## 6. Permisos (mínimo privilegio, acotado por recurso y etiqueta)

| Principal | Puede | No puede |
|---|---|---|
| `cce-a09-github-validation` (OIDC, temporal) | `CreateStack` de `cce-inference-*` **solo** con el rol de ejecución (`cloudformation:RoleArn`), `DeleteStack`/`Describe*`; leer el stack de acceso; `PassRole` del rol de ejecución (a CloudFormation) y del watchdog (a Scheduler); `Start/StopInstances`, `SendCommand` (`AWS-RunShellScript`) y `StartSession` (`AWS-StartPortForwardingSession`) solo en instancias con `cce:validation-id`; parámetros `/cce/inference/validation-runs/*` (crear y leer) y `/cce/inference/validation/cce-inference-*` (API key por stack); schedules `cce-a09-*`; `Describe*` de lectura; el ZIP de paridad si se configuró | `UpdateStack`; cualquier otra acción IAM; borrar la reserva de un id; tocar el stack de acceso |
| `cce-a09-cfn-execution` (CloudFormation) | crear y borrar los recursos de `cloudformation.yaml`: `RunInstances` solo `g6e.xlarge` y solo en la subred autorizada, grupos de seguridad solo en la VPC autorizada, volúmenes cifrados, rol del host `cce-inference-*` **solo con el límite** `cce-a09-host-boundary` y solo con `AmazonSSMManagedInstanceCore` adjunta | quitar o cambiar el límite de permisos; otros tipos de instancia o subredes |
| Rol del host (por stack) | lo de antes (agente SSM, leer su API key), acotado por el límite | nada fuera de SSM |
| `cce-a09-watchdog` (Scheduler) | `ec2:StopInstances` en instancias `cce:role = inference` con `cce:validation-id` | nada más |

La plantilla del host agrega el parámetro opcional `HostPermissionsBoundaryArn` (vacío = sin límite, como el despliegue manual).

## 7. Evidencia, secretos y paridad

Artefactos del run (90 días): `a09-1-validation-<id>-attempt-<n>` y `a09-1-cleanup-<id>-attempt-<n>`.

| Archivo | Contenido |
|---|---|
| `request.json`, `runner-tools.txt`, `oidc-claims.json` | la solicitud; versiones de `aws`, `session-manager-plugin` y Python; claims seguros del job |
| `preflight.json`, `claim.json` | comprobaciones (cuota y uso, red y ruta de salida, AMI, hash de la plantilla, peor caso de gasto); reserva del id |
| `provision.json`, `watchdog.json` | parámetros del stack (sin secretos), salidas, línea de tiempo (pedido del stack, instancia creada, stack completo, SSM online, bootstrap, primera carga sana, instancia detenida), primera carga en GPU; schedule del watchdog |
| `bootstrap.log`, `cloud-init-status.txt`, `bootstrap-server-journal.log`, `bootstrap-gpu-offload.json` | primer arranque: marcas de `bootstrap.sh`, estado de cloud-init, journal de la primera carga y **`offloaded N/N layers to GPU`** |
| `gpu-validation/` | toda la evidencia de `gpu-validation.py` (contrato §6.2): `job-cycle.json`, `host-facts.json`, `boot-timings.json`, `model-record.json`, `server-journal-{job-cycle,campaign}.log` y `gpu-offload-*.json` (cada carga con todas las capas en GPU), `load-*`, `limits-*`, `run-*`, `verdict-*`, `final-state.json`, `summary.json` |
| `outcome.json`, `cleanup-run.json`, `cleanup.json` | veredicto de la validación, limpieza del `run` y limpieza del job `cleanup`, por separado |

**Secretos**: la API key se genera en el runner, va a SSM como SecureString y a un archivo 0600 en `$RUNNER_TEMP/a09-work/secrets` (fuera de la evidencia); nunca se imprime. Antes de subir, `redact` reemplaza toda aparición de la API key y de las credenciales del entorno (`AWS_*`, tokens de GitHub) y elimina archivos con nombre de clave; si algo quedara, borra el archivo y falla. Las credenciales AWS son temporales (OIDC); la cuenta se enmascara en los logs.

**Paridad (ZIP opcional, fuera de Git)**: si `parity` está configurado, el preflight descarga el ZIP de S3 (permiso de lectura solo sobre ese objeto), verifica su SHA-256 y lo descomprime en el runner fuera de la evidencia (rechazo sin gasto si no coincide); `gpu-validation.py` ejecuta `parity.sh` sobre el perfil de referencia. Las predicciones crudas (`evaluacion-r*-i*.jsonl`) y el log del proxy reproducen el corpus privado: **no se suben** (se registra su SHA-256 en `withheld.json`); se suben métricas y `summary.json`. Ni el corpus ni los pesos entran nunca en Git ni en los artefactos.

**Sin ZIP**, la corrida da: ciclo disparado por un trabajo durable con inferencia estructurada real (canario y trabajo sintético), artefacto Q8_0 verificado en el host, carga de todas las capas en GPU (journal), tiempos de arranque EC2/SO/pesos/readiness, mediciones de memoria en el host, latencia, throughput, límites y veredictos por perfil, restitución del perfil y parada verificada (AC-A09-1-15, 18, 21, 23, 24, 25 y la parte estructurada de 19). **Queda pendiente**: la calidad de Q8_0 con la metodología de la referencia (parte de AC-A09-1-19 y AC-A09-1-26); `outcome.json` lo dice (`parity.status = pending`).

## 8. Costos

Peor caso de instancia por solicitud = 1,861 USD/h (`manifest.json aws.on_demand_usd_per_hour`) × (`max_runtime_minutes` + 15 + 10) / 60; debe ser ≤ `max_spend_usd` o la solicitud se rechaza. Con `keep-stopped`, el stack detenido cuesta ≈ 14,6 USD/mes de EBS (contrato §4.2) hasta que se elimine. El watchdog, los roles, los parámetros estándar de SSM y la reserva de ids no tienen costo apreciable.

## 9. Pruebas y límites

**Probado con AWS simulado** (`deploy/inference/controller/tests/test_ci_validation.py`, con el `aws` falso de `tests/fake_aws.py` y el host simulado): detección sobre historia git real (merge normal, README, solicitud nueva, modificada, renombrada, dos a la vez, push sin commit previo); esquema y techo de gasto; claims OIDC sin token; corrida completa `delete` (stack con los parámetros de la solicitud y el rol de ejecución, watchdog, bootstrap, primera carga en GPU, instancia detenida, `gpu-validation.py`, borrado verificado, API key borrada, watchdog desarmado; el host simulado lee la API key de SSM como el real); `keep-stopped`, reintento del mismo id sin gasto y una solicitud nueva que reutiliza el stack; doce rechazos del preflight sin reservar ni crear nada; fallo de validación con limpieza verificada (separados); offload parcial; fallo de limpieza (salida 5, watchdog armado); SIGTERM durante el bootstrap; runner muerto con SIGKILL y el watchdog que detiene la instancia; redacción. Contrato estático del workflow y de la IaC en `tests/test_a09_aws_validation_ci.py`; `actionlint` (con shellcheck) sin hallazgos; ambas plantillas aceptadas por `aws cloudformation validate-template`.

**No verificado** (requiere la habilitación y la primera corrida real):

- que las políticas IAM alcancen (o no sobren) en una creación real: `validate-template` no evalúa la semántica de IAM; un faltante se vería como `AccessDenied` temprano (antes de la GPU o con la instancia detenida por la limpieza);
- el `sub` real del repositorio y el trust con `AssumeRoleWithWebIdentity`;
- que `session-manager-plugin` esté en la imagen `ubuntu-latest` (el preflight lo exige y rechaza sin gasto; queda en `runner-tools.txt`);
- EventBridge Scheduler real (universal target `ec2:stopInstances`), la propagación de las etiquetas del stack a la instancia y el borrado real del stack;
- GitHub Actions (nunca ejecutado) y el comportamiento real de cancelación/timeout del runner.
