# Validación AWS de A09-1 desde la máquina del operador

> **Estado: IMPLEMENTADA y probada con AWS SIMULADO (macOS); lecturas reales contra AWS (`setup` y `plan`, sin escrituras) el 2026-11-24; NO ejecutada con gasto.** La validación en GPU se ejecuta **localmente**, con la identidad AWS del operador; el repositorio público no tiene acceso a la cuenta ni datos de ella. La automatización anterior por GitHub Actions (merge de una solicitud versionada, OIDC) se **retiró** el 2026-11-24 por decisión del usuario (D-A09-09, [§11](#11-historial-automatización-por-github-actions-retirada)); la revocación de su confianza en IAM y la variable del repositorio requieren la autorización del usuario ([§5.2](#52-revocación-de-la-confianza-oidc-del-stack-anterior)). Pertenece a A09-1 ([contrato](inference-deployment.md), [especificación](features/A09-1-despliegue-de-inferencia-y-operacion.md), [decisiones](decisions/A09-decisions.md), [informe](a09-1-implementation-report.md)). A09-2 y B01 quedan fuera.

Automatiza los pasos A (preparación) y B (validación) del [§4 del informe](a09-1-implementation-report.md#4-validación-pendiente-en-gpu-real) con **un comando** que el operador ejecuta en su máquina: no se despliega ni se opera nada a mano. Reutiliza `deploy/inference/aws/cloudformation.yaml` y `deploy/inference/bench/gpu-validation.py` y conserva el objetivo de D-A09-08: `g6e.xlarge` en `us-east-1`, Qwen3.6-27B Q8_0 con todas las capas en GPU, endpoint de inferencia privado y túnel SSM.

| Pieza | Ubicación |
|---|---|
| Comando (`check`, `plan`, `setup`, `run`, `cleanup`) | [`deploy/inference/bin/a09-aws-validation`](../deploy/inference/bin/a09-aws-validation) (ejecuta el orquestador en el entorno del controlador, con boto3) |
| Orquestador (Python estándar; AWS por la CLI; sonda de identidad con boto3) | [`deploy/inference/validation/aws_validation.py`](../deploy/inference/validation/aws_validation.py) |
| IaC del stack base (rol de ejecución, límite del host, watchdog) | [`deploy/inference/aws/validation-base.yaml`](../deploy/inference/aws/validation-base.yaml) |
| Ejemplo **ficticio** de solicitud | [`deploy/inference/validation/request.example.json`](../deploy/inference/validation/request.example.json) |
| Solicitud real, evidencia y directorio de trabajo | **fuera del checkout** (§3, §8) |

## 1. Flujo

```text
a09-aws-validation plan  --request ~/…/<id>.json [--profile P]   solo lecturas: identidad, stack base, preflight completo
a09-aws-validation setup --request ~/…/<id>.json [--profile P]   plan del stack base (solo lecturas)
a09-aws-validation setup --request … --apply                     crea o actualiza el stack base (idempotente) y lo verifica
a09-aws-validation run   --request ~/…/<id>.json [--profile P] [--parity-zip ZIP]
 0. solicitud válida y fuera del checkout                              → si no: salida 2, ninguna llamada a AWS
 1. identidad (solo lecturas, ANTES de cualquier escritura): STS por la AWS CLI y por boto3 en el intérprete del
    controlador; misma ARN, us-east-1, cuenta = aws.account_id         → si no: salida 2, nada escrito
 2. preflight sin efectos: reserva previa del id, plantilla al día, stack base listo (exactamente sus recursos, sin
    confianza federada, red de la solicitud), cuota G/VT, oferta en la AZ, subred/VPC/AZ y ruta de salida, AMI DLAMI,
    stack (create|reuse), session-manager-plugin, techo de gasto, ZIP de paridad (si se da)   → rechazo = salida 2
 3. reserva atómica del id en SSM (/cce/inference/validation-runs/<id>, sin --overwrite, con token) → ya existe = 3
 4. paso A: API key nueva (SecureString); registro de propiedad del stack (antes de CreateStack); stack con el rol de
    ejecución y, DENTRO del stack, el schedule de parada (CloudFormation lo crea con la instancia); CREATE_COMPLETE,
    SSM online, bootstrap, primera carga (/health 200, journal con todas las capas en GPU), instancia DETENIDA
 5. paso B: gpu-validation.py por túnel SSM (ciclo por demanda, evidencia del host, offload, campaña de los perfiles,
    veredictos, restitución del perfil, parada verificada)
 6. SIEMPRE (también tras fallo, Ctrl-C, SIGTERM o SIGHUP; señales ignoradas durante la limpieza): limpieza de lo
    PROPIO según la solicitud (keep-stopped | delete) y su verificación; redacción; informe con la VALIDACIÓN y la
    LIMPIEZA por separado
a09-aws-validation cleanup --request …   la misma limpieza, idempotente (tras un proceso perdido o una limpieza fallida)
```

**Salidas de `run`**: 0 validada; 1 la validación falló o quedó incompleta (también un aprovisionamiento fallido); 2 rechazada antes de escribir (solicitud, identidad, cuenta o preflight: nada reservado ni creado); 3 id ya reservado (sin gasto nuevo); 130 interrumpida; **5 LIMPIEZA FALLIDA**, que prevalece sobre el resultado de la validación (los recursos no quedaron verificados). `cleanup`: 0 verificada, 5 fallida. `plan`/`setup`: 0 listo o plan escrito; 2 rechazado; 1 `setup --apply` no verificado; `plan` de un id reservado: 3.

**Informe final** (en pantalla y en `outcome.json`/`cleanup-run.json`), siempre con dos líneas separadas:

```text
== A09-1 AWS validation <id> (stack <stack>, after_run keep-stopped)
   VALIDATION (tests): validated | failed: … | interrupted: … | refused (exit N)
   CLEANUP:            verified | FAILED | not_needed  instance={…} stack=…
   PARITY:             pending | ran | not_run
```

**Parada en AWS ligada a la creación.** El schedule de parada es un recurso del propio stack (`Watchdog` de `cloudformation.yaml`): EventBridge Scheduler llama a `ec2:StopInstances` sobre `!Ref Host` cada 10 min durante 3 h desde `reserva + max_runtime_minutes + 15 min`. Lo crea CloudFormation en la misma creación que la instancia (si no pudiera, el stack vuelve atrás); no depende del proceso local: si la máquina del operador se apaga, pierde la red o el proceso muere con SIGKILL, la instancia igual queda detenida. Un stack **reutilizado** (`reuse`) ya tiene su instancia, detenida: el proceso arma un schedule propio (`cce-a09-<id>`) antes de cualquier arranque y la limpieza lo borra solo tras verificarla detenida. Respaldo adicional: la guardia de inactividad de la instancia (120 min, E-A09-11).

**Propiedad** (sin cambios respecto de la versión anterior, E-A09-28). La limpieza solo detiene o borra lo que la solicitud **creó o adoptó**: el id debe estar reservado; el registro `/cce/inference/validation-stacks/<stack>` debe nombrar a esa reserva (id y token); el stack debe ser ese (`StackId`, o la etiqueta `cce:claim-token` si el proceso se perdió antes de conocerlo); solo se toca la instancia concreta de ese stack, y el borrado es por `StackId`. `reuse` exige un registro coincidente: un stack desplegado a mano no se puede adoptar.

## 2. Identidad y credenciales

- **Cadena estándar de AWS** (variables `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN`, `AWS_PROFILE`, SSO, credenciales temporales de un rol asumido) o `--profile NOMBRE`. Admite la sesión activa (por ejemplo, tras `aws sso login`). **Nunca** se copian claves al repositorio ni a la solicitud.
- **Misma identidad y región en todo**: el orquestador construye un único entorno (`AWS_REGION`/`AWS_DEFAULT_REGION` = `us-east-1`; con `--profile`, `AWS_PROFILE`) y lo pasa a la AWS CLI, a la sonda de boto3, a `gpu-validation.py`, a `ssm-run.sh`, al túnel y al controlador (cuyo `profile` queda vacío: cadena estándar de ese mismo entorno). `--profile` junto con claves en el entorno es ambiguo (la CLI y boto3 podrían resolver identidades distintas): se rechaza sin llamar a AWS.
- **Verificación con STS antes de cualquier escritura**: `sts get-caller-identity` por la CLI y por boto3 (en el intérprete del controlador, como los procesos hijos); deben coincidir la ARN y la cuenta, la región de boto3 debe ser `us-east-1` y la cuenta debe ser `aws.account_id` de la solicitud. Si no, salida 2 y nada escrito (con otra cuenta, ni siquiera se hacen lecturas adicionales). Lo verifican `plan`, `setup`, `run` y `cleanup`.
- **Sin GitHub ni OIDC**: el flujo no llama a `gh`, no usa variables de GitHub ni `AssumeRoleWithWebIdentity`.

**Permisos de la identidad del operador.** Para `run` y `cleanup`: lo que hacía el antiguo rol de CI, ahora con la identidad del operador: `cloudformation:CreateStack` de `cce-inference-*` con el rol de ejecución (`iam:PassRole` de `cce-a09-cfn-execution` a CloudFormation y de `cce-a09-watchdog` a Scheduler), `DeleteStack`/`Describe*`/`ListStackResources`/`GetTemplate`; `ec2:Describe*`, `Start/StopInstances` de las instancias con `cce:validation-id`; `ssm:SendCommand` (`AWS-RunShellScript`), `ssm:StartSession` (`AWS-StartPortForwardingSession`), `ssm:GetCommandInvocation`, `DescribeInstanceInformation`; `ssm:Get/PutParameter` de `/cce/inference/validation-runs/*`, `ssm:Get/Put/DeleteParameter` de `/cce/inference/validation-stacks/cce-inference-*` y `/cce/inference/validation/cce-inference-*` (SecureString: `kms:Encrypt`/`Decrypt` vía SSM); `ssm:GetParameter` del parámetro público de la DLAMI; `scheduler:Create/Delete/GetSchedule` del grupo `cce-inference-watchdog`; `servicequotas:GetServiceQuota`; `iam:GetRole` (verificación del stack base); `sts:GetCallerIdentity`. Para `setup --apply`, además, crear y actualizar el stack base (`CAPABILITY_NAMED_IAM`: roles, política gestionada y grupo de schedules). Un administrador de la cuenta o un permission set de SSO equivalente alcanza.

## 3. La solicitud local

Un archivo JSON **fuera del checkout** (por ejemplo `~/.config/cce-inference/a09-validation/<id>.json`, modo 0600): contiene la cuenta, la red y el presupuesto, que no entran en Git. El comando rechaza una solicitud dentro del checkout. Todo campo de gasto o limpieza es explícito; las claves desconocidas se rechazan. Comprobación sin AWS: `a09-aws-validation check RUTA` (sin argumentos valida el ejemplo ficticio).

| Campo | Regla |
|---|---|
| `schema_version` | `2` |
| `id` | `[a-z0-9-]`, 3–32; un id se ejecuta una sola vez (reserva en SSM) |
| `purpose` | texto |
| `authorization` | `spend_approved` y `network_approved` en `true`, `approved_by`, `approved_on` (AAAA-MM-DD), `max_spend_usd` ∈ (0, 100] y ≥ peor caso del límite de tiempo (§9) |
| `aws` | `account_id` (12 dígitos, como texto: la cuenta esperada, verificada con STS), `region` = `us-east-1`, `instance_type` = `manifest.json aws.instance_type` (`g6e.xlarge`); opcional `base_stack` (por defecto `cce-a09-validation-base`) |
| `model` | = `manifest.json targets.aws` (`qwen3.6-27b-q8_0`) |
| `network` | `vpc_id`, `subnet_id`, `availability_zone` (`us-east-1[a-f]`), `assign_public_ip`; opcionales `client_cidr` (privada) y `client_security_group_id`. Debe coincidir con la red del stack base. Sin `client_*`, el puerto 8080 no admite a nadie: el operador llega por túnel SSM |
| `ami` | `"resolve-latest"` (parámetro público de la DLAMI, resuelto y registrado) o un `ami-…` fijado |
| `stack` | `name` (`cce-inference-…`), `mode` (`create` = stack nuevo; `reuse` = adoptar uno creado por otra solicitud, detenido, con la misma plantilla y registro de propiedad coincidente), **`after_run`**: `keep-stopped` (se conserva detenido para A09-2) o `delete` (se elimina y se verifica) |
| `limits` | `max_runtime_minutes` 60–480 (límite de la corrida y del watchdog); `bootstrap_timeout_minutes` 15–120 (por defecto 60) |
| `validation` | opcional: `profiles` (el primero es la referencia; por defecto los tres del §4 del informe), `server_profile`, `requests`, `prompt_tokens` |
| `parity` | `null` o `{"zip_path": "/ruta/absoluta/local.zip", "sha256": "…"}` (`sha256` opcional; §8). `--parity-zip RUTA` lo da por línea de comandos |

Ejemplo ficticio: [`request.example.json`](../deploy/inference/validation/request.example.json) (cuenta `123456789012`, `vpc-0123456789abcdef0`): copiarlo fuera del checkout y completar la cuenta, la red y la autorización reales. Con 240 min, el peor caso de instancia es 1,861 USD/h × (240 + 25) min ≈ 8,22 USD.

## 4. Idempotencia, duplicados y serialización

- **Sin repetir el gasto**: el id se reserva con `ssm put-parameter` **sin** `--overwrite` antes de crear nada; la reserva no se borra. Repetir `run` con el mismo id encuentra la reserva: salida 3, sin gasto; `cleanup` vuelve a verificar. Para otra corrida: un id nuevo. Un rechazo del preflight no reserva el id.
- **Una corrida a la vez**: el preflight exige 4 vCPU libres de la cuota G/VT (instancias G/VT `pending`/`running`/`stopping` de la cuenta).
- **`plan` nunca reserva ni crea**: solo lecturas en AWS (escribe `plan.json` en el directorio local de evidencia).

## 5. Stack base (`setup`)

Recursos que las corridas reutilizan y que **no** dependen de ningún mecanismo de confianza federada: `cce-a09-cfn-execution` (CloudFormation crea y borra los stacks `cce-inference-*` y su schedule), `cce-a09-host-boundary` (límite de permisos del rol del host), `cce-a09-watchdog` y el grupo de schedules `cce-inference-watchdog` (parada de respaldo). Cada rol solo confía en un servicio de AWS de la misma cuenta (`aws:SourceAccount`). Parámetros: `VpcId`, `SubnetId`, `InstanceType` (`g6e.xlarge`).

| Paso de `setup` | Qué hace y verifica | Si falla |
|---|---|---|
| Identidad | §2 (CLI = boto3, cuenta esperada) | salida 2, nada cambia |
| Red | VPC existente, subred en esa VPC y AZ, `g6e.xlarge` ofrecida en la AZ, ruta `0.0.0.0/0` activa (IGW o NAT) | salida 2 |
| Plan (siempre, solo lecturas) | estado del stack; recursos a agregar o **quitar** respecto de `validation-base.yaml` (por id lógico); roles del stack cuya confianza es **federada** (`Principal.Federated` o `AssumeRoleWithWebIdentity`); parámetros que cambian o se descartan; si la plantilla desplegada difiere | — |
| `--apply` | `cloudformation deploy --no-fail-on-empty-changeset` (sin cambios = no-op) | salida 1 |
| Verificación | `ready`: stack completo, **exactamente** los recursos de la plantilla, ningún rol con confianza federada, salidas presentes, red = la de la solicitud | salida 1 (`SETUP NOT VERIFIED`) |

`run` y `plan` rechazan un stack base que no esté `ready`. Repetir `setup --apply` no cambia nada y vuelve a verificar.

### 5.1 Adoptar el stack de acceso anterior

Los roles del antiguo stack de acceso de GitHub tienen los **mismos ids lógicos y nombres** que los de `validation-base.yaml`. Con `aws.base_stack` = nombre de ese stack (por defecto era `cce-a09-github-access`), `setup --apply` lo **actualiza en el lugar**: CloudFormation elimina solo el rol de CI (`ValidationRole`, `cce-a09-github-validation`) y con él su confianza OIDC; conserva `cce-a09-cfn-execution`, `cce-a09-host-boundary`, `cce-a09-watchdog` y el grupo de schedules; descarta los parámetros `GitHubOidcProviderArn`, `GitHubSubject` y `ParityObjectArn`. El **proveedor OIDC** de la cuenta no pertenece al stack y no se toca (puede estar en uso por otros repositorios).

### 5.2 Revocación de la confianza OIDC del stack anterior

Cambio externo, reversible, que se ejecuta **solo con la autorización del usuario**:

1. **AWS** (con la solicitud local cuyo `aws.base_stack` es el stack anterior):

   ```bash
   deploy/inference/bin/a09-aws-validation setup --request ~/.config/cce-inference/a09-validation/<id>.json --profile <perfil>
   ```

   ```bash
   deploy/inference/bin/a09-aws-validation setup --request ~/.config/cce-inference/a09-validation/<id>.json --profile <perfil> --apply
   ```

   El primero muestra el plan (`to_remove: ValidationRole`, `federated_trust`); el segundo lo aplica y verifica que no quede ningún rol con confianza federada. **Reversión**: volver a desplegar la plantilla anterior con sus parámetros (`git show <commit>:deploy/inference/aws/github-validation-access.yaml > /tmp/old.yaml`, luego `aws cloudformation deploy --stack-name <stack> --template-file /tmp/old.yaml --capabilities CAPABILITY_NAMED_IAM --parameter-overrides GitHubOidcProviderArn=… GitHubSubject=…`), que recrea el rol con el mismo trust.
2. **GitHub**: `gh variable delete CCE_A09_AWS_ROLE_ARN --repo <owner>/<repo>`. **Reversión**: `gh variable set CCE_A09_AWS_ROLE_ARN --repo <owner>/<repo> --body <arn>`. Sin el workflow (retirado), la variable ya no tiene uso; su valor contiene el ARN del rol (y por lo tanto el número de cuenta).

## 6. Garantías por modo de fallo

| Situación | Qué detiene la GPU | Qué se informa |
|---|---|---|
| La validación falla (medición, veredicto, offload parcial, bootstrap) | `gpu-validation.py` y la limpieza del `run` | `VALIDATION: failed …`, salida 1; `CLEANUP` aparte (`cleanup-run.json`) |
| Ctrl-C, SIGTERM o SIGHUP | limpieza del `run` (señales ignoradas mientras corre) | `VALIDATION: interrupted`, salida 130; `CLEANUP` aparte |
| Proceso muerto con SIGKILL, máquina apagada o sin red (también apenas aceptado `CreateStack`) | el schedule del stack (creado por CloudFormation con la instancia); luego `cleanup`, que reconoce el stack por la etiqueta de la reserva | la reserva queda sin `cleanup`; `cleanup` la completa |
| Falla la limpieza (stack no se borra, instancia no verificada) | schedule del stack (sigue con él) o del proceso (no se desarma) | salida **5** (`CLEANUP FAILED`, con el comando para repetirla) aunque la validación haya pasado; `outcome.json` conserva el resultado de la validación |
| Solicitud no reservada (rechazo) y luego `cleanup` | — | `cleanup.json`: `not_claimed`, nada detenido ni borrado |
| Reintento de una solicitud cuyo stack adoptó otra | — | `cleanup.json`: `not_owned`, recursos preservados |
| Mismo id otra vez | — (nada creado) | `DUPLICATE … no new spend`, salida 3 |
| Cuenta equivocada, CLI ≠ boto3, sin credenciales, `--profile` + claves en el entorno, solicitud inválida o dentro del checkout | — (nada escrito) | salida 2 |

## 7. Permisos de los roles (mínimo privilegio, acotado por recurso y etiqueta)

| Principal | Puede | No puede |
|---|---|---|
| Identidad del operador | §2 | — (es la del usuario; el repositorio no la conoce) |
| `cce-a09-cfn-execution` (CloudFormation) | crear y borrar los recursos de `cloudformation.yaml`: `RunInstances` solo `g6e.xlarge` y solo en la subred autorizada, grupos de seguridad solo en la VPC autorizada, volúmenes cifrados, rol del host `cce-inference-*` **solo con el límite** `cce-a09-host-boundary` y solo con `AmazonSSMManagedInstanceCore`, el schedule `cce-inference-*` del grupo y `PassRole` del rol del watchdog a Scheduler | quitar o cambiar el límite de permisos; otros tipos de instancia o subredes |
| Rol del host (por stack) | agente SSM y leer su API key, acotado por el límite | nada fuera de SSM |
| `cce-a09-watchdog` (Scheduler) | `ec2:StopInstances` en instancias `cce:role = inference` con `cce:validation-id` | nada más |

## 8. Evidencia, secretos y paridad

Evidencia y trabajo **fuera del checkout** (el comando lo exige): por defecto `${XDG_STATE_HOME:-~/.local/state}/cce-inference/a09-validation/<id>/<comando>-<UTC>/` y `…-work/` al lado; `--evidence` y `--workdir` los cambian.

| Archivo | Contenido |
|---|---|
| `request.json`, `plan.json`, `setup.json`, `identity.json` | la solicitud; el plan; el stack base antes y después; la identidad verificada |
| `preflight.json`, `claim.json` | identidad (CLI y boto3), herramientas locales (`aws`, `session-manager-plugin`, Python), cuota y uso, red y ruta de salida, AMI, stack base, hash de la plantilla, peor caso de gasto, paridad; reserva del id (operador, commit del checkout, token) |
| `provision.json`, `watchdog.json` | parámetros del stack, salidas, registro de propiedad (sin token), línea de tiempo, primera carga en GPU; schedule de parada |
| `bootstrap.log`, `cloud-init-status.txt`, `bootstrap-server-journal.log`, `bootstrap-gpu-offload.json` | primer arranque y **`offloaded N/N layers to GPU`** |
| `gpu-validation/` | toda la evidencia de `gpu-validation.py` (contrato §6.2) |
| `outcome.json`, `cleanup-run.json`, `cleanup.json` | resultado de la validación, limpieza del `run` y limpieza de `cleanup`, por separado |

**Secretos**: la API key se genera localmente, va a SSM como SecureString y a un archivo 0600 en el directorio de trabajo (fuera de la evidencia); nunca se imprime. Al terminar, `redact` reemplaza toda aparición de la API key y de las credenciales del entorno (`AWS_*`) en la evidencia y elimina archivos con nombre de clave.

**Paridad (ZIP opcional, local, fuera de Git)**: con `parity.zip_path` o `--parity-zip`, el preflight verifica que exista fuera del checkout, su SHA-256 (si la solicitud lo fija; si no, lo registra), rutas seguras y la estructura (`CAWbase_benchmark_v1.1/` y `corrida_nocturna/`); el `run` lo descomprime en el directorio de trabajo antes de reservar el id, y `gpu-validation.py` ejecuta `parity.sh` en el perfil de referencia. Las predicciones crudas y el log del proxy reproducen el corpus privado: se mueven fuera de la evidencia (`withheld.json` con su SHA-256). **Sin ZIP**, la paridad queda **pendiente** (`parity.status = pending`): la corrida da el resto (AC-A09-1-15, 18, 21, 23, 24, 25 y la parte estructurada de 19); la calidad de Q8_0 con la metodología de la referencia (parte de AC-A09-1-19 y AC-A09-1-26) sigue pendiente.

## 9. Costos

Peor caso de instancia por solicitud = 1,861 USD/h (`manifest.json aws.on_demand_usd_per_hour`) × (`max_runtime_minutes` + 15 + 10) / 60; debe ser ≤ `max_spend_usd` o la solicitud se rechaza. Con `keep-stopped`, el stack detenido cuesta ≈ 14,6 USD/mes de EBS (contrato §4.2) hasta que se elimine. El stack base, el watchdog, los parámetros estándar de SSM y la reserva de ids no tienen costo apreciable.

## 10. Pruebas y límites

**Probado con AWS simulado** (`deploy/inference/controller/tests/test_aws_validation.py`, 67 pruebas; el `aws` falso de `tests/fake_aws.py` resuelve la identidad como la cadena estándar —claves del entorno, luego `AWS_PROFILE` en el archivo de credenciales— y un STS falso responde a **boto3 real** por `AWS_ENDPOINT_URL_STS`; una trampa `gh` falla si se la llama): corrida completa `delete` y `keep-stopped` con un perfil nombrado (todas las llamadas, también las de los procesos hijos y del host simulado, con la misma clave y `us-east-1`; sin `gh` ni OIDC); credenciales temporales en el entorno sin perfil; cuenta equivocada en `plan`, `setup`, `run` y `cleanup` (una sola llamada, a STS); CLI ≠ boto3; sin credenciales; `--profile` con claves en el entorno; solicitud inválida o dentro del checkout (ninguna llamada a AWS); `plan` sin escrituras y con id reservado; `setup` que crea, es idempotente y **revoca un rol de CI con confianza federada** conservando los demás; `run` rechazado mientras ese rol exista; red inutilizable y `deploy` fallido; reintento sin gasto; `reuse`; catorce rechazos del preflight sin escribir nada; paridad local (SHA-256, rutas inseguras, estructura, `--parity-zip` en el plan); fallo de validación ≠ fallo de limpieza (salida 5 y dos líneas); offload parcial; SIGTERM y SIGINT durante el bootstrap; proceso muerto con SIGKILL y la parada por el schedule del stack; proceso perdido apenas aceptado `CreateStack`; propiedad (no reservado, recreado, adoptado, reintento antiguo, `reuse` sin registro); redacción. Contrato estático en `tests/test_a09_aws_validation_local.py` (ningún workflow con `id-token` ni credenciales de AWS; stack base sin confianza federada; mismos nombres de roles; ningún identificador real de cuenta o red en el árbol versionado).

**Verificado contra AWS real, solo lecturas (2026-11-24)**: `setup` (plan) y `plan` con la identidad del operador: identidad igual por la CLI y por boto3; el plan del stack anterior muestra exactamente `to_remove: ValidationRole` con su confianza federada, parámetros GitHub descartados y la red sin cambios; `plan` rechaza la corrida mientras ese rol exista; `get-template` devuelve el cuerpo original, comparable con la plantilla del repositorio.

**No verificado** (requiere la primera corrida autorizada): `setup --apply` real (actualización del stack), que las políticas IAM alcancen en una creación real, EventBridge Scheduler y el recurso `AWS::Scheduler::Schedule` reales, la propagación de las etiquetas del stack, el túnel SSM real (en la máquina del operador falta `session-manager-plugin`: el preflight lo exige), el borrado real del stack y todo lo que depende de la GPU.

## 11. Historial: automatización por GitHub Actions (retirada)

Del 2026-11-24 (merge del PR #10) hasta este cambio existió una variante en la que mergear a `main` una solicitud versionada `deploy/inference/validation-requests/<id>.json` ejecutaba la validación en GitHub Actions con un rol OIDC (`cce-a09-github-validation`) del stack de acceso `cce-a09-github-access`, una variable del repositorio `CCE_A09_AWS_ROLE_ARN` y un workflow de diagnóstico del `sub`. Nunca se ejecutó con gasto: el rol no se asumió nunca. Por decisión del usuario (D-A09-09) se retiraron el workflow, el diagnóstico, el directorio vigilado, la operación de habilitación para GitHub (`enable_access.py`) y la plantilla con el rol OIDC; el orquestador, la plantilla del host, la parada en AWS, la propiedad, las garantías de limpieza y las pruebas simuladas se conservan, adaptados a la ejecución local. Elecciones de aquella variante: E-A09-21 a E-A09-30 (las que siguen vigentes, adaptadas, se indican en el registro de decisiones). El historial de Git conserva aquellos archivos y la evidencia de la habilitación aplicada; este cambio no reescribe el historial.
