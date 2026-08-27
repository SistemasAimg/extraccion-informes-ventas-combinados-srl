# Extracción combinada de ventas Caddis — SRL

Este repositorio ejecuta diariamente dos informes de Caddis dentro de una misma sesión:

- `317`: Ventas por PDV con costo.
- `305`: Ventas por formas de pago.

El job usa la ventana inclusiva **ayer → hoy**, conserva los Excel originales y genera una tabla combinada con control de diferencias.

## Lógica de combinación

`Ventas por PDV con costo` es la tabla principal. Las facturas `X` y cualquier movimiento que no tenga cobranza se conservan siempre.

Cuando el informe 305 es detallado, la relación utiliza:

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

Si el informe 305 llega resumido (`POS`, `TPago`, `Cantidad`, `Importe`, etc.), el job no inventa una relación. Guarda el resumen por separado y marca las filas PDV como `FORMA_PAGO_RESUMIDA_NO_RELACIONABLE`.

## Histórico sin duplicados

Las ejecuciones diarias se superponen:

```text
27/08: 26/08 → 27/08
28/08: 27/08 → 28/08
```

Por eso no se hace append ciego. La historia local reemplaza las fechas de la ventana y Cloud Storage mantiene particiones reemplazables:

```text
caddis/srl/raw/<informe>/<run_id>/<archivo>.xls
caddis/srl/history/ventas_combinadas/fecha=YYYY-MM-DD/data.csv
caddis/srl/current/ventas_combinadas.csv
caddis/srl/current/pdv_raw.csv
caddis/srl/current/formas_pago_raw.csv
caddis/srl/current/control.csv
```

Repetir una ejecución no duplica la fecha y permite incorporar correcciones o anulaciones de Caddis.

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
GOOGLE_SHEET_ID    # solo si se habilita google_sheets en vars.yml
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

## Google Cloud

El workflow de GitHub Actions:

1. Ejecuta las pruebas.
2. Construye y publica la imagen en Artifact Registry.
3. Crea o actualiza el Cloud Run Job.
4. Crea el bucket histórico si no existe.
5. Crea o actualiza un Cloud Scheduler diario a las `00:30`, zona `America/Argentina/Buenos_Aires`.

Antes del primer despliegue deben existir en Secret Manager:

```text
caddis-user
caddis-pass
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

La publicación está implementada pero deshabilitada por defecto en `vars.yml`. Para activarla:

1. Crear previamente las pestañas `Ventas combinadas`, `PDV raw`, `Formas pago raw` y `Control`.
2. Configurar `GOOGLE_SHEET_ID`.
3. Cambiar `combined_output.options.google_sheets.enabled` a `true`.

Sheets muestra la ventana procesada; Cloud Storage conserva el histórico completo y los Excel originales.
