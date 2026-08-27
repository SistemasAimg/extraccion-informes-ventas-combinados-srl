#!/usr/bin/env python3
"""Job diario Caddis: descarga 317 + 305, combina e historiza."""

from __future__ import annotations

import argparse
import json
import copy
import importlib
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Mapping

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
        body = legacy.build_aa_body(
            filter_cfg.get("sfunc", "armar_filtroInformesVentas"),
            args_raw=rendered_raw_args,
            args=rendered_args,
        )
    elif filter_cfg.get("raw"):
        body = legacy._render_str_with_ctx(str(filter_cfg["raw"]), ctx)
    else:
        raise ValueError(f"El informe {name} no tiene armar_filtro.args/raw configurado.")

    legacy.STEP_RAW(
        "POST",
        f"{base}{filter_cfg.get('endpoint', '/gestion/herramientas/metodos/ajax-articulos.php')}",
        headers=filter_cfg.get("headers"),
        referer=pantalla_ref,
        body_str=body,
    )

    download_cfg = report_cfg.get("download") or {}
    endpoint = download_cfg.get(
        "endpoint", "/gestion/herramientas/informes/informes_excel.php"
    )
    raw_body = download_cfg.get("raw")
    if raw_body:
        response = legacy.STEP_RAW(
            "POST",
            f"{base}{endpoint}",
            headers=download_cfg.get("headers"),
            referer=pantalla_ref,
            body_str=legacy._render_str_with_ctx(str(raw_body), ctx),
        )
    else:
        data = legacy.replace_placeholders(dict(download_cfg.get("data") or {}), ctx)
        data.setdefault("Informe", code)
        response = legacy.STEP(
            "POST",
            f"{base}{endpoint}",
            headers=download_cfg.get("headers"),
            referer=pantalla_ref,
            data=data,
        )

    if response.status_code != 200 or not response.content:
        raise RuntimeError(
            f"Falló la descarga {name}: HTTP {response.status_code}, bytes={len(response.content or b'')}"
        )

    filename_template = str(
        report_cfg.get("save_as") or f"data/raw/{name}/informe_{code}_{{today:%Y%m%d}}.xls"
    )
    filename = legacy._render_str_with_ctx(filename_template, ctx)
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

    return {
        "name": name,
        "code": code,
        "filename": Path(filename).name,
        "saved_path": filename,
        "content": response.content,
        "content_type": response.headers.get("Content-Type"),
        "headers": dict(response.headers),
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
