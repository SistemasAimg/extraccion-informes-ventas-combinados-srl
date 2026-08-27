"""Pipeline para combinar e historizar informes de ventas de Caddis.

El informe 317 (Ventas por PDV c/Costo) es la tabla principal. El informe 305
puede llegar en dos esquemas distintos:

* detalle: contiene factura y artículo, por lo que puede relacionarse;
* resumen: contiene POS/medio de pago/totales y se conserva sin forzar un join.

La persistencia histórica reemplaza las particiones de fecha incluidas en la
ventana descargada. Esto hace que una ejecución diaria ``ayer -> hoy`` sea
idempotente y permita capturar correcciones o anulaciones posteriores.
"""

from __future__ import annotations

import io
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence

import pandas as pd



PDV_REQUIRED = {"Factura Tipo", "Factura Numero", "Articulo Codigo"}
PAYMENT_DETAIL_REQUIRED = {"Fc Tipo", "Fc_Nro", "Parte"}
PAYMENT_SUMMARY_REQUIRED = {"POS", "TPago", "Cantidad", "Importe", "Total"}

KEY_COLUMN = "Clave Cruce"
OCCURRENCE_COLUMN = "Indice Coincidencia"
STATUS_COLUMN = "Estado Cruce"
SCHEMA_COLUMN = "Esquema Formas Pago"


def _clean_headers(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(col).strip() for col in out.columns]
    out = out.dropna(how="all").reset_index(drop=True)
    out = out.loc[:, ~out.columns.duplicated()].copy()
    return out


def _normalise_key_part(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip().upper()
    text = re.sub(r"\s+", "", text)
    return text


def _add_join_key(
    df: pd.DataFrame,
    *,
    type_col: str,
    invoice_col: str,
    article_col: str,
) -> pd.DataFrame:
    out = df.copy()
    missing = [c for c in (type_col, invoice_col, article_col) if c not in out.columns]
    if missing:
        raise ValueError(f"Faltan columnas para construir la clave: {missing}")

    key_parts = [
        out[type_col].map(_normalise_key_part),
        out[invoice_col].map(_normalise_key_part),
        out[article_col].map(_normalise_key_part),
    ]
    out[KEY_COLUMN] = key_parts[0] + "|" + key_parts[1] + "|" + key_parts[2]
    # No ocultamos claves incompletas: quedan identificadas y el control las cuenta.
    out[OCCURRENCE_COLUMN] = out.groupby(KEY_COLUMN, dropna=False).cumcount() + 1
    return out


def classify_payment_schema(df: pd.DataFrame) -> str:
    columns = set(_clean_headers(df).columns)
    if PAYMENT_DETAIL_REQUIRED.issubset(columns):
        return "detalle"
    if PAYMENT_SUMMARY_REQUIRED.issubset(columns):
        return "resumen"
    return "desconocido"


def _prefix_payment_columns(df: pd.DataFrame) -> pd.DataFrame:
    protected = {KEY_COLUMN, OCCURRENCE_COLUMN}
    return df.rename(
        columns={col: col if col in protected else f"Pago {col}" for col in df.columns}
    )


def _count_duplicate_rows(df: pd.DataFrame) -> int:
    if KEY_COLUMN not in df.columns:
        return 0
    return int(df.duplicated(KEY_COLUMN, keep=False).sum())


def combine_sales_dataframes(
    pdv_df: pd.DataFrame,
    payment_df: pd.DataFrame,
    *,
    extracted_at: datetime | None = None,
    window_start: date | None = None,
    window_end: date | None = None,
) -> Dict[str, pd.DataFrame | str]:
    """Combina dos DataFrames y devuelve tablas de salida y control."""

    pdv = _clean_headers(pdv_df)
    payments = _clean_headers(payment_df)
    missing_pdv = sorted(PDV_REQUIRED.difference(pdv.columns))
    if missing_pdv:
        raise ValueError(f"El informe PDV no tiene el esquema esperado. Faltan: {missing_pdv}")

    pdv = _add_join_key(
        pdv,
        type_col="Factura Tipo",
        invoice_col="Factura Numero",
        article_col="Articulo Codigo",
    )
    payment_schema = classify_payment_schema(payments)
    extracted_at = extracted_at or datetime.now()

    duplicate_pdv = _count_duplicate_rows(pdv)
    duplicate_payments = 0

    if payment_schema == "detalle":
        payments_keyed = _add_join_key(
            payments,
            type_col="Fc Tipo",
            invoice_col="Fc_Nro",
            article_col="Parte",
        )
        duplicate_payments = _count_duplicate_rows(payments_keyed)
        payments_prefixed = _prefix_payment_columns(payments_keyed)
        combined = pdv.merge(
            payments_prefixed,
            how="outer",
            on=[KEY_COLUMN, OCCURRENCE_COLUMN],
            indicator=True,
            sort=False,
        )
        combined[STATUS_COLUMN] = combined["_merge"].map(
            {
                "both": "RELACIONADO",
                "left_only": "SOLO_PDV",
                "right_only": "SOLO_FORMA_PAGO",
            }
        ).astype("string")
        combined = combined.drop(columns=["_merge"])
    elif payment_schema == "resumen":
        combined = pdv.copy()
        combined[STATUS_COLUMN] = "FORMA_PAGO_RESUMIDA_NO_RELACIONABLE"
    else:
        combined = pdv.copy()
        combined[STATUS_COLUMN] = "FORMA_PAGO_ESQUEMA_DESCONOCIDO"

    combined.insert(0, SCHEMA_COLUMN, payment_schema.upper())
    combined.insert(0, "Fecha Extraccion", extracted_at.isoformat(timespec="seconds"))

    status_counts = combined[STATUS_COLUMN].value_counts(dropna=False).to_dict()
    key_text = combined[KEY_COLUMN].astype(str)
    incomplete_keys = int((key_text.str.startswith("|") | key_text.str.contains("||", regex=False) | key_text.str.endswith("|")).sum())
    control = pd.DataFrame(
        [
            {
                "Fecha Ejecucion": extracted_at.isoformat(timespec="seconds"),
                "Desde": window_start.isoformat() if window_start else "",
                "Hasta": window_end.isoformat() if window_end else "",
                "Filas PDV": len(pdv),
                "Filas Formas Pago": len(payments),
                "Esquema Formas Pago": payment_schema,
                "Relacionadas": int(status_counts.get("RELACIONADO", 0)),
                "Solo PDV": int(status_counts.get("SOLO_PDV", 0)),
                "Solo Formas Pago": int(status_counts.get("SOLO_FORMA_PAGO", 0)),
                "No relacionables por resumen": int(
                    status_counts.get("FORMA_PAGO_RESUMIDA_NO_RELACIONABLE", 0)
                ),
                "Duplicados PDV": duplicate_pdv,
                "Duplicados Formas Pago": duplicate_payments,
                "Claves incompletas": incomplete_keys,
            }
        ]
    )

    return {
        "pdv": pdv,
        "payments": payments,
        "combined": combined,
        "control": control,
        "payment_schema": payment_schema,
    }


def _effective_date_series(df: pd.DataFrame) -> pd.Series:
    candidates = []
    for column in ("Fecha", "Pago Fecha"):
        if column in df.columns:
            parsed = pd.to_datetime(df[column], errors="coerce", dayfirst=False)
            candidates.append(parsed)
    if not candidates:
        return pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    result = candidates[0]
    for candidate in candidates[1:]:
        result = result.fillna(candidate)
    return result


def update_history_dataframe(
    existing: pd.DataFrame | None,
    current: pd.DataFrame,
    *,
    window_start: date,
    window_end: date,
) -> pd.DataFrame:
    """Reemplaza la ventana de fechas y conserva el resto del histórico."""

    current = current.copy()
    if existing is None or existing.empty:
        merged = current
    else:
        existing = existing.copy()
        existing_dates = _effective_date_series(existing).dt.date
        inside_window = existing_dates.between(window_start, window_end)
        retained = existing.loc[~inside_window].copy()
        if retained.empty:
            merged = current
        elif current.empty:
            merged = retained
        else:
            merged = pd.concat([retained, current], ignore_index=True, sort=False)

    dedupe_cols = [c for c in (KEY_COLUMN, OCCURRENCE_COLUMN) if c in merged.columns]
    if len(dedupe_cols) == 2:
        merged = merged.drop_duplicates(dedupe_cols, keep="last")

    dates = _effective_date_series(merged)
    merged = merged.assign(_fecha_orden=dates)
    sort_cols = ["_fecha_orden"]
    for candidate in ("Factura Numero", "Pago Fc_Nro", KEY_COLUMN, OCCURRENCE_COLUMN):
        if candidate in merged.columns:
            sort_cols.append(candidate)
    merged = merged.sort_values(sort_cols, kind="mergesort", na_position="last")
    return merged.drop(columns=["_fecha_orden"]).reset_index(drop=True)


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _csv_bytes(df: pd.DataFrame) -> bytes:
    buffer = io.StringIO()
    df.to_csv(buffer, index=False)
    return buffer.getvalue().encode("utf-8")


def _iter_dates(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip())
    return value.strip("._") or "report"


def _find_report(reports: Sequence[Mapping[str, Any]], *, code: str, name: str):
    for report in reports:
        if str(report.get("code", "")) == code or str(report.get("name", "")) == name:
            return report
    raise ValueError(f"No se recibió el informe requerido {name!r} (código {code}).")


def _upload_gcs_outputs(
    *,
    reports: Sequence[Mapping[str, Any]],
    tables: Mapping[str, Any],
    options: Mapping[str, Any],
    window_start: date,
    window_end: date,
    run_id: str,
) -> None:
    gcs_cfg = options.get("gcs") or {}
    if not gcs_cfg.get("enabled"):
        return
    bucket_name = str(gcs_cfg.get("bucket") or os.getenv("CADDIS_HISTORY_BUCKET", "")).strip()
    if not bucket_name:
        raise ValueError("GCS está habilitado pero falta gcs.bucket o CADDIS_HISTORY_BUCKET.")

    try:
        from google.cloud import storage
    except Exception as exc:
        raise RuntimeError("Falta instalar google-cloud-storage para usar el histórico GCS.") from exc

    prefix = str(gcs_cfg.get("prefix", "caddis/ventas-combinadas-srl")).strip("/")
    client = storage.Client(project=gcs_cfg.get("project") or None)
    bucket = client.bucket(bucket_name)

    for report in reports:
        raw = report.get("content") or b""
        if not raw:
            continue
        report_name = _safe_name(report.get("name") or report.get("code") or "report")
        filename = _safe_name(report.get("filename") or f"informe_{report_name}.xls")
        blob = bucket.blob(f"{prefix}/raw/{report_name}/{run_id}/{filename}")
        blob.upload_from_string(raw, content_type=report.get("content_type") or "application/octet-stream")

    combined = tables["combined"]
    effective_dates = _effective_date_series(combined).dt.date
    for partition_date in _iter_dates(window_start, window_end):
        partition = combined.loc[effective_dates == partition_date].copy()
        blob = bucket.blob(
            f"{prefix}/history/ventas_combinadas/fecha={partition_date.isoformat()}/data.csv"
        )
        blob.upload_from_string(_csv_bytes(partition), content_type="text/csv; charset=utf-8")

    current_tables = {
        "ventas_combinadas": tables["combined"],
        "pdv_raw": tables["pdv"],
        "formas_pago_raw": tables["payments"],
        "control": tables["control"],
    }
    for name, df in current_tables.items():
        bucket.blob(f"{prefix}/current/{name}.csv").upload_from_string(
            _csv_bytes(df), content_type="text/csv; charset=utf-8"
        )
    bucket.blob(f"{prefix}/runs/{run_id}/control.csv").upload_from_string(
        _csv_bytes(tables["control"]), content_type="text/csv; charset=utf-8"
    )


def _upload_google_sheets(tables: Mapping[str, Any], options: Mapping[str, Any]) -> None:
    sheets_cfg = options.get("google_sheets") or {}
    if not sheets_cfg.get("enabled"):
        return
    sheet_id = str(sheets_cfg.get("sheet_id") or os.getenv("GOOGLE_SHEET_ID", "")).strip()
    if not sheet_id:
        raise ValueError("Google Sheets está habilitado pero falta sheet_id o GOOGLE_SHEET_ID.")

    from report_pipeline import get_google_credentials, upload_to_sheet

    tabs = sheets_cfg.get("tabs") or {}
    credentials = get_google_credentials(sheets_cfg)
    mapping = {
        "combined": tabs.get("combined", "Ventas combinadas"),
        "pdv": tabs.get("pdv", "PDV raw"),
        "payments": tabs.get("payments", "Formas pago raw"),
        "control": tabs.get("control", "Control"),
    }

    # Una planilla recién creada normalmente contiene sólo "Hoja 1". Creamos
    # las pestañas de salida faltantes para que el primer despliegue no dependa
    # de una preparación manual adicional.
    from googleapiclient.discovery import build

    service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    metadata = service.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="sheets.properties.title",
    ).execute()
    existing_tabs = {
        str(item.get("properties", {}).get("title", ""))
        for item in metadata.get("sheets", [])
    }
    missing_tabs = [
        str(title)
        for title in dict.fromkeys(mapping.values())
        if str(title) not in existing_tabs
    ]
    if missing_tabs:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "requests": [
                    {"addSheet": {"properties": {"title": title}}}
                    for title in missing_tabs
                ]
            },
        ).execute()

    for key, tab_name in mapping.items():
        frame = tables[key].copy()
        frame.attrs["timezone"] = options.get("timezone", "America/Argentina/Buenos_Aires")
        upload_to_sheet(
            frame,
            sheet_id,
            str(tab_name),
            credentials,
            write_mode="replace",
            append_header=False,
            start_cell="A3",
        )


def process_combined_reports(
    *,
    reports: Sequence[Mapping[str, Any]],
    meta: Mapping[str, Any],
    options: Mapping[str, Any],
) -> Dict[str, Any]:
    """Callback ejecutado por el runner después de descargar ambos informes."""

    from report_pipeline import read_report_bytes_to_df

    pdv_report = _find_report(reports, code="317", name="pdv_costo")
    payment_report = _find_report(reports, code="305", name="formas_pago")
    pdv_df = read_report_bytes_to_df(pdv_report.get("content") or b"", options={"input_format": "auto"})
    payment_df = read_report_bytes_to_df(payment_report.get("content") or b"", options={"input_format": "auto"})

    now = meta.get("now")
    if not isinstance(now, datetime):
        now = datetime.now()
    window_start = meta.get("yesterday")
    window_end = meta.get("today")
    if not isinstance(window_start, date) or not isinstance(window_end, date):
        raise ValueError("El runner no informó la ventana yesterday/today.")

    tables = combine_sales_dataframes(
        pdv_df,
        payment_df,
        extracted_at=now,
        window_start=window_start,
        window_end=window_end,
    )

    output_dir = Path(str(options.get("local_dir", "data")))
    current_dir = output_dir / "current"
    _write_csv(tables["combined"], current_dir / "ventas_combinadas.csv")
    _write_csv(tables["pdv"], current_dir / "pdv_raw.csv")
    _write_csv(tables["payments"], current_dir / "formas_pago_raw.csv")
    _write_csv(tables["control"], current_dir / "control.csv")

    history_cfg = options.get("history") or {}
    history_path = output_dir / str(history_cfg.get("file", "history/ventas_combinadas.csv"))
    if history_cfg.get("enabled", True):
        existing = None
        if history_path.exists():
            existing = pd.read_csv(history_path, low_memory=False)
        history = update_history_dataframe(
            existing,
            tables["combined"],
            window_start=window_start,
            window_end=window_end,
        )
        _write_csv(history, history_path)
        tables["history"] = history

    run_id = now.strftime("%Y%m%dT%H%M%S")
    _upload_gcs_outputs(
        reports=reports,
        tables=tables,
        options=options,
        window_start=window_start,
        window_end=window_end,
        run_id=run_id,
    )
    _upload_google_sheets(tables, options)

    logging.info(
        "[COMBINADO] esquema_pago=%s filas=%s historia=%s",
        tables["payment_schema"],
        len(tables["combined"]),
        len(tables.get("history", [])),
    )
    return {
        "payment_schema": tables["payment_schema"],
        "combined_rows": len(tables["combined"]),
        "history_rows": len(tables.get("history", [])),
        "output_dir": str(output_dir),
    }
