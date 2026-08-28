# Extracción combinada de ventas Caddis — SRL

Este repositorio ejecuta diariamente dos informes de Caddis dentro de una misma sesión:

- `317`: Ventas por PDV con costo.
- `305`, vista detallada `331`: Ventas por formas de pago.

El job descarga únicamente el día cerrado anterior, con la ventana inclusiva
**ayer → ayer**. Conserva los Excel originales y genera una tabla combinada con
control de diferencias.

## Lógica de combinación

`Ventas por PDV con costo` es la tabla principal. Las facturas `X` y cualquier movimiento que no tenga cobranza se conservan siempre.

Cuando el informe base 305 se descarga con la vista detallada 331, la relación utiliza:

```text
Factura Tipo / Fc Tipo
+ Factura Numero / Fc_Nro
+ Articulo Codigo / Parte
+ índice de repetición dentro de la clave
```

El resultado clasifica cada fila como:

- `RELACIONADO`
- `SOLO_PDV`
- `SOLO_FORMA_PAGO`

Si Caddis devuelve la vista resumida (`POS`, `TPago`, `Cantidad`, `Importe`, etc.), el job no inventa una relación. Guarda el resumen por separado y marca las filas PDV como `FORMA_PAGO_RESUMIDA_NO_RELACIONABLE`.

## Histórico sin duplicados

Cada ejecución procesa un único día:

```text
Ejecución 28/08: 27/08 → 27/08
Ejecución 29/08: 28/08 → 28/08
```

La historia local reemplaza esa partición diaria y Cloud Storage mantiene
particiones reemplazables:

```text
caddis/ventas-combinadas-srl/raw/<informe>/<run_id>/attempt-XX/<archivo>.xls
caddis/ventas-combinadas-srl/history/ventas_combinadas/fecha=YYYY-MM-DD/data.csv
caddis/ventas-combinadas-srl/current/ventas_combinadas.csv
caddis/ventas-combinadas-srl/current/pdv_raw.csv
caddis/ventas-combinadas-srl/current/formas_pago_raw.csv
caddis/ventas-combinadas-srl/current/control.csv
```

Repetir una ejecución no duplica la fecha y permite incorporar correcciones o anulaciones de Caddis.

## Google Sheets acumulativo

La pestaña `Ventas combinadas` conserva las filas existentes y agrega únicamente
claves nuevas al final de la siguiente fila disponible. La comparación usa
`Clave Cruce + Indice Coincidencia`, por lo que las reejecuciones del mismo día
no duplican ventas. Los encabezados se crean
sólo cuando la pestaña está vacía y no se vuelven a insertar en cada ejecución.
Antes de anexar, cada lote se ordena por `Fecha` en forma ascendente.

`PDV raw` y `Formas pago raw` siguen representando el último corte descargado y
se reemplazan. `Control` agrega una fila por ejecución para conservar la traza.

## Descargas resilientes

Cada informe se valida antes de combinarlo. El job comprueba HTTP, tamaño,
formato real y columnas mínimas. Todos los intentos se archivan inmediatamente
en Cloud Storage, incluso cuando Caddis devuelve HTML o un XLS inválido.

La política por defecto realiza tres intentos: el primero inmediato y los
siguientes después de 3 y 8 segundos. Cada intento vuelve a preparar la
pantalla, ejecutar `armar_filtroInformesVentas` y descargar el informe. Si
Caddis devuelve HTML, además se renueva la sesión.

Los logs informan código de informe, intento, bytes, tipo de contenido, formato,
hash abreviado, filas, columnas y fila de encabezado. Nunca registran cookies,
credenciales ni contenido de ventas.

La fila de encabezados se busca dentro de las primeras 20 filas mediante
columnas distintivas. Se toleran espacios extra y diferencias de mayúsculas.
Un informe sin registros se considera válido cuando conserva sus encabezados.

## Configuración

`vars.yml` contiene los payloads de ambos informes. Aunque el archivo usa extensión YAML, su contenido actual es JSON válido y también puede leerse como YAML.

Las fechas se renderizan dinámicamente en:

- Los argumentos 5 y 6 de `armar_filtroInformesVentas`.
- `field_desde` y `field_hasta`.
- `SubTitulo`.

Variables requeridas:

```text
CADDIS_USER
CADDIS_PASS
CADDIS_HISTORY_BUCKET
```

Variables opcionales:

```text
CADDIS_GRUPO       # por defecto GPSMUNDO
GOOGLE_SHEET_ID    # configurado por el workflow para Cloud Run
```

No guardar credenciales, cookies, `PHPSESSID` ni JSON de cuentas de servicio en Git.

## Ejecución local

Instalar dependencias y ejecutar la descarga real:

```bash
python -m pip install -r requirements.txt
export CADDIS_USER='usuario'
export CADDIS_PASS='contraseña'
export CADDIS_HISTORY_BUCKET='bucket-existente'
python caddis_combined_job.py --vars vars.yml
```

Para probar solamente la combinación, sin conectarse a Caddis ni Google Cloud:

```bash
python caddis_combined_job.py   --vars vars.yml   --pdv-file /ruta/pdv.xls   --payment-file /ruta/formas_pago.xls   --local-only   --run-date 2026-08-27
```

Las salidas locales se generan en `data/current/` y `data/history/`; la carpeta está excluida de Git.

## Pruebas

```bash
python -m unittest discover -s tests -v
```

Las pruebas cubren:

- Cruce de un informe detallado.
- Conservación de facturas `X` sin cobranza.
- Detección de un informe de pagos resumido.
- Reemplazo idempotente de la ventana histórica.
- Detección de encabezados desplazados.
- Normalización de espacios y mayúsculas.
- Rechazo de HTML y esquemas desconocidos.
- Archivado de cada intento y recuperación en el segundo intento.
- Informes válidos sin registros.

## Google Cloud

El workflow de GitHub Actions:

1. Ejecuta las pruebas.
2. Construye y publica la imagen en Artifact Registry.
3. Crea o actualiza el Cloud Run Job.
4. Crea el bucket histórico si no existe.
5. Crea o actualiza un Cloud Scheduler diario a las `00:30`, zona `America/Argentina/Buenos_Aires`.

Antes del primer despliegue deben existir en Secret Manager:

```text
caddis-web-usuario
caddis-web-password
```

La cuenta de ejecución `cloudrun@storage-entorno-de-desarrollo.iam.gserviceaccount.com` necesita:

- `roles/secretmanager.secretAccessor` sobre ambos secretos.
- Permiso para crear objetos en el bucket histórico, por ejemplo `roles/storage.objectAdmin`.
- `roles/run.invoker` para la invocación programada; el workflow intenta asignarlo al Job.

Documentación oficial:

- https://cloud.google.com/run/docs/execute/jobs-on-schedule
- https://cloud.google.com/run/docs/configuring/jobs/secrets
- https://cloud.google.com/storage/docs/creating-buckets

## Google Sheets

La publicación está habilitada en `vars.yml`. El workflow configura el ID de la planilla y el pipeline crea automáticamente las pestañas que falten:

- `Ventas combinadas`
- `PDV raw`
- `Formas pago raw`
- `Control`

La planilla debe estar compartida como editora con `cloudrun@storage-entorno-de-desarrollo.iam.gserviceaccount.com`.

Sheets muestra la ventana procesada; Cloud Storage conserva el histórico completo y los Excel originales.
Los objetos de este proyecto se guardan bajo el prefijo aislado `caddis/ventas-combinadas-srl`.
