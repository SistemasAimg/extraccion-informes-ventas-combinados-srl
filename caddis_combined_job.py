#!/usr/bin/env python3
"""Job diario Caddis: descarga 317 + 305, combina e historiza."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Sequence

try:
    import yaml
except Exception:
    yaml = None

legacy = None

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ReportDownloadValidationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: str = "invalid",
        diagnostics: Mapping[str, Any] | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.diagnostics = dict(diagnostics or {})


def _load_legacy():
    global legacy
    if legacy is None:
        import caddis_replay_all_steps as legacy_module
        legacy = legacy_module
    return legacy


def _expand_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if not isinstance(value, str):
        return value
    exact = _ENV_PATTERN.fullmatch(value)
    if exact:
        return os.getenv(exact.group(1), "")
    return _ENV_PATTERN.sub(lambda match: os.getenv(match.group(1), ""), value)


def load_config(path: str) -> Dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    try:
        config = json.loads(text)
    except json.JSONDecodeError:
        if yaml is None:
            raise RuntimeError("La configuración no es JSON y PyYAML no está instalado.")
        config = yaml.safe_load(text) or {}
    return _expand_env(config)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def build_context(config: Mapping[str, Any], code: str, *, now: datetime | None = None):
    tz_name = str(config.get("timezone") or "America/Argentina/Buenos_Aires")
    tz = None
    if ZoneInfo:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = None
    now = now or (datetime.now(tz) if tz else datetime.now())
    today = now.date()
    yesterday = today - timedelta(days=1)
    month_start = today.replace(day=1)
    if today.month == 12:
        next_month_start = today.replace(year=today.year + 1, month=1, day=1)
    else:
        next_month_start = today.replace(month=today.month + 1, day=1)
    month_end = next_month_start - timedelta(days=1)
    last_month_end = month_start - timedelta(days=1)
    return {
        "code": str(code),
        "now": now,
        "today": today,
        "yesterday": yesterday,
        "month_start": month_start,
        "month_end": month_end,
        "last_month_start": last_month_end.replace(day=1),
        "last_month_end": last_month_end,
    }


def _configure_legacy(config: Mapping[str, Any], args) -> None:
    credentials = config.get("credenciales") or {}
    user = os.getenv("CADDIS_USER") or credentials.get("user") or ""
    password = os.getenv("CADDIS_PASS") or credentials.get("pass") or ""
    group = os.getenv("CADDIS_GRUPO") or credentials.get("grupo") or ""
    missing = [
        name
        for name, value in (
            ("CADDIS_USER", user),
            ("CADDIS_PASS", password),
            ("CADDIS_GRUPO", group),
        )
        if not str(value).strip()
    ]
    if missing:
        raise ValueError(f"Faltan credenciales de entorno: {', '.join(missing)}")

    legacy.BASE = str(config.get("base_url") or legacy.BASE_DEFAULT).rstrip("/")
    legacy.UA = str(config.get("ua") or legacy.UA_DEFAULT)
    legacy.USER = str(user)
    legacy.PASS = str(password)
    legacy.GRUPO = str(group)
    legacy.s.headers.update({"User-Agent": legacy.UA})
    legacy._log_setup(bool(args.debug))

    if args.insecure:
        import urllib3

        legacy.s.verify = False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    elif args.extra_ca:
        legacy.s.verify = args.extra_ca


def _common_browser_flow(config: Mapping[str, Any]) -> None:
    base = legacy.BASE
    legacy.STEP("GET", f"{base}/gestion/index_main.php")
    legacy.login()
    legacy.STEP(
        "GET",
        f"{base}/gestion/herramientas/class.ajax.php",
        params={"ajaxagent": "js", "this_url": ""},
        referer=f"{base}/gestion/index_main.php",
    )
    legacy.STEP(
        "GET",
        f"{base}/gestion/herramientas/class.ajax.php",
        params={"ajaxagent": "js", "this_url": ""},
        referer=f"{base}/gestion/index_main.php",
    )

    for preflight in config.get("preflights_index_main", legacy.PRE_INDEX_MAIN):
        if isinstance(preflight, str):
            body = preflight
        else:
            body = legacy.build_aa_body(
                preflight.get("sfunc", ""),
                preflight.get("args_raw"),
                preflight.get("args"),
            )
        legacy.STEP_RAW(
            "POST",
            f"{base}/gestion/herramientas/metodos/ajax-celulares.php",
            referer=f"{base}/gestion/index_main.php",
            body_str=body,
        )

    base_select = str(config.get("base_select", "179"))
    legacy.STEP(
        "POST",
        f"{base}/gestion/index_main.php",
        referer=f"{base}/gestion/index_main.php",
        data={"codigoPantalla": "", "baseSelect": base_select, "destroySession": ""},
    )
    legacy.STEP(
        "POST",
        f"{base}/gestion/index_main.php",
        referer=f"{base}/gestion/index_main.php",
        data={"hidden": "", "codigoPantalla": "7001", "baseSelect": "", "destroySession": ""},
    )
    legacy.STEP(
        "GET",
        f"{base}/gestion/herramientas/class.ajax.php",
        params={"ajaxagent": "js", "this_url": ""},
        referer=f"{base}/gestion/index_main.php",
    )


def _normalise_header(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value).strip())


def _accepted_column_sets(report_cfg: Mapping[str, Any]) -> list[list[str]]:
    validation_cfg = report_cfg.get("validation") or {}
    configured = validation_cfg.get("accepted_column_sets")
    if isinstance(configured, Sequence) and not isinstance(configured, (str, bytes)):
        result = []
        for candidate in configured:
            if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
                columns = [_normalise_header(value) for value in candidate if str(value).strip()]
                if columns:
                    result.append(columns)
        if result:
            return result

    code = str(report_cfg.get("code") or report_cfg.get("report_code") or "")
    if code == "317":
        return [["Factura Tipo", "Factura Numero", "Articulo Codigo"]]
    if code == "305":
        return [
            ["Fc Tipo", "Fc_Nro", "Parte"],
            ["POS", "TPago", "Cantidad", "Importe", "Total"],
        ]
    return []


def _response_diagnostics(response) -> Dict[str, Any]:
    content = bytes(getattr(response, "content", b"") or b"")
    head = content.lstrip()[:4096].lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<form" in head:
        format_hint = "html"
    elif content.startswith(b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"):
        format_hint = "excel_xls"
    elif content.startswith(b"PK\x03\x04"):
        format_hint = "excel_xlsx"
    else:
        format_hint = "unknown"

    headers = getattr(response, "headers", {}) or {}
    return {
        "http_status": int(getattr(response, "status_code", 0) or 0),
        "bytes": len(content),
        "content_type": str(headers.get("Content-Type") or ""),
        "sha256": hashlib.sha256(content).hexdigest()[:16] if content else "",
        "format_hint": format_hint,
    }


def _validate_downloaded_report(
    report_cfg: Mapping[str, Any],
    response,
    *,
    policy: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    policy = policy or {}
    validation_cfg = report_cfg.get("validation") or {}
    diagnostics = _response_diagnostics(response)
    content = bytes(getattr(response, "content", b"") or b"")
    report_name = str(report_cfg.get("name") or report_cfg.get("code") or "informe")

    if diagnostics["http_status"] != 200:
        raise ReportDownloadValidationError(
            f"{report_name}: HTTP {diagnostics['http_status']}.",
            kind="http",
            diagnostics=diagnostics,
        )

    if diagnostics["format_hint"] == "html":
        raise ReportDownloadValidationError(
            f"{report_name}: Caddis devolvió HTML en lugar del Excel.",
            kind="html",
            diagnostics=diagnostics,
        )

    min_bytes = int(validation_cfg.get("min_bytes", policy.get("min_bytes", 512)) or 0)
    if not content or len(content) < max(1, min_bytes):
        raise ReportDownloadValidationError(
            f"{report_name}: respuesta demasiado pequeña ({len(content)} bytes).",
            kind="empty",
            diagnostics=diagnostics,
        )

    accepted_sets = _accepted_column_sets(report_cfg)
    from report_pipeline import read_report_bytes_to_df

    try:
        frame = read_report_bytes_to_df(
            content,
            options={
                "input_format": "auto",
                "header_signatures": accepted_sets,
                "header_scan_rows": int(validation_cfg.get("header_scan_rows", 20)),
            },
        )
    except Exception as exc:
        raise ReportDownloadValidationError(
            f"{report_name}: no se pudo abrir el archivo: {exc}",
            kind="parse",
            diagnostics=diagnostics,
        ) from exc

    normalised_columns = [_normalise_header(column) for column in frame.columns]
    canonical_headers = {
        _normalise_header(column).casefold(): _normalise_header(column)
        for candidate in accepted_sets
        for column in candidate
    }
    frame.columns = [
        canonical_headers.get(column.casefold(), column)
        for column in normalised_columns
    ]
    normalised_columns = [str(column) for column in frame.columns]
    column_set = {column.casefold() for column in normalised_columns}
    diagnostics.update(
        {
            "rows": int(len(frame)),
            "column_count": int(len(frame.columns)),
            "columns": normalised_columns,
            "source_format": str(frame.attrs.get("source_format") or ""),
            "header_row": frame.attrs.get("header_row_index_in_source"),
        }
    )

    allowed_formats = validation_cfg.get(
        "allowed_formats", ["excel_xls", "excel_xlsx"]
    )
    if allowed_formats and diagnostics["source_format"] not in set(allowed_formats):
        raise ReportDownloadValidationError(
            f"{report_name}: formato detectado {diagnostics['source_format']!r} no permitido.",
            kind="format",
            diagnostics=diagnostics,
        )

    if accepted_sets and not any(
        {
            _normalise_header(column).casefold()
            for column in candidate
        }.issubset(column_set)
        for candidate in accepted_sets
    ):
        preview = normalised_columns[:25]
        suffix = "..." if len(normalised_columns) > len(preview) else ""
        raise ReportDownloadValidationError(
            f"{report_name}: esquema inesperado. Columnas detectadas: {preview}{suffix}",
            kind="schema",
            diagnostics=diagnostics,
        )

    diagnostics["dataframe"] = frame
    return diagnostics


def _archive_raw_attempt(
    config: Mapping[str, Any],
    report_cfg: Mapping[str, Any],
    ctx: Mapping[str, Any],
    response,
    *,
    attempt: int,
    filename: str,
    diagnostics: Mapping[str, Any],
) -> str | None:
    combined_cfg = config.get("combined_output") or {}
    options = combined_cfg.get("options") or {}
    gcs_cfg = options.get("gcs") or {}
    if not _as_bool(gcs_cfg.get("enabled"), False):
        return None

    bucket_name = str(
        gcs_cfg.get("bucket") or os.getenv("CADDIS_HISTORY_BUCKET", "")
    ).strip()
    if not bucket_name:
        logging.warning("[RAW] No se archivó el intento: falta CADDIS_HISTORY_BUCKET.")
        return None

    try:
        from google.cloud import storage

        client = storage.Client(project=gcs_cfg.get("project") or None)
        bucket = client.bucket(bucket_name)
        prefix = str(
            gcs_cfg.get("prefix", "caddis/ventas-combinadas-srl")
        ).strip("/")
        report_name = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "_",
            str(report_cfg.get("name") or report_cfg.get("code") or "report").strip(),
        ).strip("._") or "report"
        safe_filename = re.sub(
            r"[^A-Za-z0-9_.-]+", "_", Path(filename).name
        ).strip("._") or "informe.xls"
        now = ctx.get("now")
        run_id = (
            now.strftime("%Y%m%dT%H%M%S")
            if isinstance(now, datetime)
            else datetime.now().strftime("%Y%m%dT%H%M%S")
        )
        object_name = (
            f"{prefix}/raw/{report_name}/{run_id}/"
            f"attempt-{attempt:02d}/{safe_filename}"
        )
        blob = bucket.blob(object_name)
        blob.metadata = {
            "attempt": str(attempt),
            "http_status": str(diagnostics.get("http_status", "")),
            "bytes": str(diagnostics.get("bytes", "")),
            "sha256": str(diagnostics.get("sha256", "")),
            "format_hint": str(diagnostics.get("format_hint", "")),
        }
        blob.upload_from_string(
            bytes(getattr(response, "content", b"") or b""),
            content_type=str(diagnostics.get("content_type") or "application/octet-stream"),
        )
        uri = f"gs://{bucket_name}/{object_name}"
        logging.info("[RAW] informe=%s intento=%s uri=%s", report_name, attempt, uri)
        return uri
    except Exception as exc:
        logging.warning(
            "[RAW] No se pudo archivar informe=%s intento=%s: %s",
            report_cfg.get("name") or report_cfg.get("code"),
            attempt,
            exc,
        )
        return None


def _download_with_retry(
    *,
    report_cfg: Mapping[str, Any],
    policy: Mapping[str, Any],
    attempt_fn: Callable[[int], Any],
    archive_fn: Callable[[Any, int, Mapping[str, Any]], str | None] | None = None,
    refresh_session_fn: Callable[[], None] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
):
    max_attempts = max(1, int(policy.get("max_attempts", 3)))
    raw_delays = policy.get("retry_delays_seconds", [3, 8])
    if not isinstance(raw_delays, Sequence) or isinstance(raw_delays, (str, bytes)):
        raw_delays = [raw_delays]
    delays = [max(0.0, float(value)) for value in raw_delays if value is not None]
    last_error: Exception | None = None
    report_name = str(report_cfg.get("name") or report_cfg.get("code") or "informe")

    for attempt in range(1, max_attempts + 1):
        if attempt > 1 and delays:
            delay = delays[min(attempt - 2, len(delays) - 1)]
            if delay:
                logging.info(
                    "[REINTENTO] informe=%s intento=%s espera=%.1fs",
                    report_name,
                    attempt,
                    delay,
                )
                sleep_fn(delay)

        try:
            response = attempt_fn(attempt)
            diagnostics = _response_diagnostics(response)
            logging.info(
                "[DESCARGA] informe=%s intento=%s http=%s bytes=%s tipo=%s "
                "formato=%s sha256=%s",
                report_name,
                attempt,
                diagnostics["http_status"],
                diagnostics["bytes"],
                diagnostics["content_type"],
                diagnostics["format_hint"],
                diagnostics["sha256"],
            )
            archive_uri = (
                archive_fn(response, attempt, diagnostics) if archive_fn else None
            )
            validation = _validate_downloaded_report(
                report_cfg, response, policy=policy
            )
        except ReportDownloadValidationError as exc:
            last_error = exc
            found = exc.diagnostics.get("columns") or []
            logging.warning(
                "[VALIDACION] informe=%s intento=%s error=%s columnas=%s",
                report_name,
                attempt,
                exc,
                found[:25],
            )
            if (
                attempt < max_attempts
                and exc.kind == "html"
                and refresh_session_fn is not None
            ):
                try:
                    logging.warning(
                        "[SESION] Caddis devolvió HTML; renovando sesión para %s.",
                        report_name,
                    )
                    refresh_session_fn()
                except Exception as refresh_exc:
                    logging.warning(
                        "[SESION] No se pudo renovar la sesión: %s", refresh_exc
                    )
            continue
        except Exception as exc:
            last_error = exc
            logging.warning(
                "[DESCARGA] informe=%s intento=%s excepción=%s",
                report_name,
                attempt,
                exc,
            )
            continue

        validation["archive_uri"] = archive_uri
        validation["attempt"] = attempt
        logging.info(
            "[VALIDACION] informe=%s intento=%s filas=%s columnas=%s "
            "formato=%s encabezado_fila=%s estado=OK",
            report_name,
            attempt,
            validation["rows"],
            validation["column_count"],
            validation["source_format"],
            validation.get("header_row"),
        )
        return response, validation

    raise RuntimeError(
        f"El informe {report_name} falló después de {max_attempts} intentos. "
        f"Último error: {last_error}"
    ) from last_error


def _prepare_report_screen(
    config: Mapping[str, Any],
    report_cfg: Mapping[str, Any],
    *,
    code: str,
    pantalla_url: str,
    pantalla_ref: str,
) -> None:
    base = legacy.BASE
    legacy.STEP(
        "GET",
        pantalla_url,
        params={"Informe_Codigo": code},
        referer=f"{base}/gestion/index_main.php",
    )
    legacy.STEP(
        "GET",
        f"{base}/gestion/herramientas/class.ajax.php",
        params={"ajaxagent": "js", "this_url": ""},
        referer=pantalla_ref,
    )
    legacy.STEP(
        "GET",
        f"{base}//gestion//js/gestion_functions.js",
        params={"3029a8fe4ed0faa8e8f3be93ec2f2647": "null"},
        referer=pantalla_ref,
    )
    legacy.STEP(
        "GET",
        f"{base}/gestion/herramientas/informes/filtros/filtro_informes.js",
        params={"fccec067a474b0826aa37dc7d7ef7d62": "null"},
        referer=pantalla_ref,
    )
    legacy.STEP(
        "GET",
        f"{base}/gestion/herramientas/class.ajax.php",
        params={"ajaxagent": "js", "this_url": ""},
        referer=pantalla_ref,
    )

    for preflight in report_cfg.get(
        "preflights_pantalla", config.get("preflights_pantalla", legacy.PRE_PANTALLA)
    ):
        if isinstance(preflight, str):
            body = preflight
        else:
            body = legacy.build_aa_body(
                preflight.get("sfunc", ""),
                preflight.get("args_raw"),
                preflight.get("args"),
            )
        legacy.STEP_RAW(
            "POST",
            f"{base}/gestion/herramientas/metodos/ajax-celulares.php",
            referer=pantalla_ref,
            body_str=body,
        )


def _run_report(config: Mapping[str, Any], report_cfg: Mapping[str, Any], base_ctx):
    code = str(report_cfg.get("code") or report_cfg.get("report_code") or "")
    if not code:
        raise ValueError("Cada elemento de reports debe incluir code.")
    name = str(report_cfg.get("name") or f"report_{code}")
    ctx = dict(base_ctx)
    ctx["code"] = code
    base = legacy.BASE
    pantalla_url = f"{base}/gestion/herramientas/pantallas/pantalla_informes_filtro.php"
    pantalla_ref = f"{pantalla_url}?Informe_Codigo={code}"

    filter_cfg = report_cfg.get("armar_filtro") or {}
    flow_args = legacy._parse_flow_args_if_needed(
        filter_cfg.get("args"), config.get("date_macros"), code
    )
    rendered_args = legacy.replace_placeholders(flow_args, ctx) if flow_args is not None else None
    rendered_raw_args = (
        legacy._render_str_with_ctx(filter_cfg.get("args_raw", ""), ctx)
        if filter_cfg.get("args_raw")
        else None
    )
    if rendered_args is not None or rendered_raw_args is not None:
        filter_body = legacy.build_aa_body(
            filter_cfg.get("sfunc", "armar_filtroInformesVentas"),
            args_raw=rendered_raw_args,
            args=rendered_args,
        )
    elif filter_cfg.get("raw"):
        filter_body = legacy._render_str_with_ctx(str(filter_cfg["raw"]), ctx)
    else:
        raise ValueError(f"El informe {name} no tiene armar_filtro.args/raw configurado.")

    download_cfg = report_cfg.get("download") or {}
    endpoint = download_cfg.get(
        "endpoint", "/gestion/herramientas/informes/informes_excel.php"
    )
    filename_template = str(
        report_cfg.get("save_as")
        or f"data/raw/{name}/informe_{code}_{{today:%Y%m%d}}.xls"
    )
    filename = legacy._render_str_with_ctx(filename_template, ctx)

    def attempt_download(_attempt: int):
        _prepare_report_screen(
            config,
            report_cfg,
            code=code,
            pantalla_url=pantalla_url,
            pantalla_ref=pantalla_ref,
        )
        legacy.STEP_RAW(
            "POST",
            f"{base}{filter_cfg.get('endpoint', '/gestion/herramientas/metodos/ajax-articulos.php')}",
            headers=filter_cfg.get("headers"),
            referer=pantalla_ref,
            body_str=filter_body,
        )

        raw_body = download_cfg.get("raw")
        if raw_body:
            return legacy.STEP_RAW(
                "POST",
                f"{base}{endpoint}",
                headers=download_cfg.get("headers"),
                referer=pantalla_ref,
                body_str=legacy._render_str_with_ctx(str(raw_body), ctx),
            )
        data = legacy.replace_placeholders(dict(download_cfg.get("data") or {}), ctx)
        data.setdefault("Informe", code)
        return legacy.STEP(
            "POST",
            f"{base}{endpoint}",
            headers=download_cfg.get("headers"),
            referer=pantalla_ref,
            data=data,
        )

    policy = dict(config.get("download_policy") or {})
    policy.update(report_cfg.get("download_policy") or {})
    response, validation = _download_with_retry(
        report_cfg=report_cfg,
        policy=policy,
        attempt_fn=attempt_download,
        archive_fn=lambda response, attempt, diagnostics: _archive_raw_attempt(
            config,
            report_cfg,
            ctx,
            response,
            attempt=attempt,
            filename=filename,
            diagnostics=diagnostics,
        ),
        refresh_session_fn=lambda: _common_browser_flow(config),
    )

    if _as_bool(report_cfg.get("save_file"), True):
        output_path = Path(filename)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(response.content)
        print(f"  -> guardado en {output_path}")

    try:
        legacy.STEP(
            "POST",
            f"{base}/gestion/herramientas/metodos/ajax-celulares.php",
            referer=pantalla_ref,
            data={"aa_afunc": "call", "aa_sfunc": "estado_Reporte", "aa_cfunc": ""},
        )
    except Exception:
        logging.warning("No se pudo consultar estado_Reporte para %s", name)

    validation_metadata = {
        key: value for key, value in validation.items() if key != "dataframe"
    }
    return {
        "name": name,
        "code": code,
        "filename": Path(filename).name,
        "saved_path": filename,
        "content": response.content,
        "content_type": response.headers.get("Content-Type"),
        "headers": dict(response.headers),
        "dataframe": validation["dataframe"],
        "validation": validation_metadata,
        "raw_archived_uri": validation.get("archive_uri"),
    }


def _run_combined_callback(config, reports, ctx, *, local_only: bool = False):
    combined_cfg = copy.deepcopy(config.get("combined_output") or {})
    if not _as_bool(combined_cfg.get("enabled"), True):
        return None
    module_name = combined_cfg.get("module", "combined_sales_pipeline")
    function_name = combined_cfg.get("function", "process_combined_reports")
    options = combined_cfg.get("options") or {}
    options.setdefault("timezone", config.get("timezone"))
    if local_only:
        options.setdefault("gcs", {})["enabled"] = False
        options.setdefault("google_sheets", {})["enabled"] = False
    callback = getattr(importlib.import_module(module_name), function_name)
    return callback(reports=reports, meta=ctx, options=options)


def _run_from_files(config, args, ctx):
    reports = [
        {
            "name": "pdv_costo",
            "code": "317",
            "filename": Path(args.pdv_file).name,
            "content": Path(args.pdv_file).read_bytes(),
            "content_type": "application/vnd.ms-excel",
        },
        {
            "name": "formas_pago",
            "code": "305",
            "filename": Path(args.payment_file).name,
            "content": Path(args.payment_file).read_bytes(),
            "content_type": "application/vnd.ms-excel",
        },
    ]
    return _run_combined_callback(config, reports, ctx, local_only=args.local_only)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vars", default=os.getenv("VARS_FILE", "vars.yml"))
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--extra-ca")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--pdv-file")
    parser.add_argument("--payment-file")
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument(
        "--run-date",
        help="Fecha local YYYY-MM-DD usada para probar macros; omitir en producción.",
    )
    args = parser.parse_args()

    config = load_config(args.vars)
    now = None
    if args.run_date:
        now = datetime.strptime(args.run_date, "%Y-%m-%d").replace(hour=0, minute=30)
    ctx = build_context(config, "", now=now)

    if bool(args.pdv_file) != bool(args.payment_file):
        parser.error("--pdv-file y --payment-file deben usarse juntos.")
    if args.pdv_file and args.payment_file:
        result = _run_from_files(config, args, ctx)
        print(f"Listo: {result}")
        return 0

    _load_legacy()
    _configure_legacy(config, args)
    _common_browser_flow(config)
    reports_cfg = config.get("reports") or []
    if not reports_cfg:
        raise ValueError("vars.yml debe incluir reports con los códigos 317 y 305.")
    results = [_run_report(config, report_cfg, ctx) for report_cfg in reports_cfg]
    result = _run_combined_callback(config, results, ctx, local_only=False)
    print(f"Listo: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
