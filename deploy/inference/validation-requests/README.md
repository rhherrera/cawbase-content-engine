# Solicitudes de validación A09-1 en AWS

Agregar aquí **un** archivo `<id>.json` y mergearlo a `main` dispara el workflow
[`a09-1-aws-gpu-validation`](../../../.github/workflows/a09-1-aws-gpu-validation.yml): validación previa sin
efectos, reserva del id, stack, bootstrap, validación con `gpu-validation.py` y limpieza verificada. **Cada
archivo nuevo es gasto autorizado en AWS**: va en su propia PR, cuando el usuario autorizó la red y el gasto.

- Un id se ejecuta **una sola vez**: reejecutar el workflow, modificar o renombrar el archivo no vuelve a gastar
  (la reserva vive en SSM Parameter Store). Para otra corrida, agregar un archivo con un id nuevo.
- Una solicitud por push. Este README y los cambios a solicitudes existentes nunca ejecutan nada.
- Esquema, campos y ejemplo: [contrato](../../../docs/a09-1-aws-validation-automation.md#3-la-solicitud) y
  [`ci/request.example.json`](../ci/request.example.json) (fuera de este directorio a propósito).
- Comprobación local sin AWS: `python3 deploy/inference/ci/aws_validation.py check`.
