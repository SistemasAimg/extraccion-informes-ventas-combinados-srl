#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Runner parametrizable por variables (YAML).

Editás SOLO un archivo de variables (por defecto: vars.yml) para cambiar:
- credenciales
- código de informe
- preflights AJAX (aa_sfunc + aa_sfunc_args)
- armar_filtro (raw o con lista de args)
- data del POST de descarga y nombre de archivo

Uso:
  python caddis_replay_all_steps.py --vars vars.yml
"""

import os
import sys
import json
import argparse
import logging
import importlib
from pathlib import Path
from urllib.parse import urlencode, quote
from datetime import datetime, timedelta
import re
try:
    from zoneinfo import ZoneInfo
except Exception:  # fallback si no hay tzdata
    ZoneInfo = None

import requests

try:
    import yaml  # PyYAML
except Exception:
    yaml = None

DEBUG = False

def _log_setup(enabled: bool):
    global DEBUG
    DEBUG = enabled
    level = logging.DEBUG if enabled else logging.INFO
    logging.basicConfig(level=level, format="[%(levelname)s] %(message)s")

# ========================= Helpers =========================
UA_DEFAULT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36"
)
BASE_DEFAULT = "https://www.caddis.com.ar"

DROP_HEADERS = {
    "Host",
    "Connection",
    "Accept-Encoding",
    "Content-Length",
    "Cookie",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
}

def clean_headers(h):
    h = dict(h or {})
    return {k: v for k, v in h.items() if k not in DROP_HEADERS and v not in (None, "")}

# Session global (se reconfigura en main con verify, UA, etc)
s = requests.Session()

# Valores globales parametrizables (se setean desde config)
BASE = BASE_DEFAULT
UA = UA_DEFAULT
USER = os.getenv("CADDIS_USER", "")
PASS = os.getenv("CADDIS_PASS", "")
GRUPO = os.getenv("CADDIS_GRUPO", "GPSMUNDO")
REPORT = os.getenv("CADDIS_REPORT", "301")
SAVE_AS = f"informe_{REPORT}.xls"

# Preflights por defecto (se pueden reemplazar en YAML)
PRE_INDEX_MAIN = [
    {"sfunc": "unsetVariableSession", "args_raw": "%5B%22msg_error%22%5D"},
    {"sfunc": "unsetVariableSession", "args_raw": "%5B%22SQL_Grilla%22%5D"},
    {"sfunc": "unsetVariableSession", "args_raw": "%5B%22Filtro%22%5D"},
    {"sfunc": "dVariableSesion", "args_raw": "%5B%22msg_error%22%5D"},
    {"sfunc": "unsetVariableSession", "args_raw": "%5B%22msg_error%22%5D"},
    {"sfunc": "unsetVariableSession", "args_raw": "%5B%22SQL_Grilla%22%5D"},
    {"sfunc": "dVariableSesion", "args_raw": "%5B%22userid%22%5D"},
]
PRE_PANTALLA = [
    {"sfunc": "dVariableSesion", "args_raw": "%5B%22userid%22%5D"},
    # armar_filtro va aparte
    {"sfunc": "dVariableSesion", "args_raw": "%5B%22userid%22%5D"},
    {"sfunc": "unsetVariableSession", "args_raw": "%5B%22SQL_Grilla%22%5D"},
]

# armar_filtro por defecto (raw, percent-encodeado tal como capturaste)
ARMAR_RAW = (
    "aa_afunc=call&aa_sfunc=armar_filtroInformes&aa_cfunc=&aa_sfunc_args%5B%5D="
    "%5B%22301%22,%22%22,%22%22,%22%22,%22%22,%22%22,%22%22,%22%22,%22%22,%22NULL%22,%22undefined%22,%22NULL%22,%22NULL%22,%22NULL%22,%22NULL%22,%22NULL%22,%22%22,%22%22,%22%22,%22%22,%22NULL%22,%22NULL%22,%22NULL%22,%22NULL%22,%22NULL%22,%22NULL%22,%22NULL%22,%22NULL%22,%22%22,%22%22,%22%22,%22NULL%22,%22NULL%22,%22NULL%22,%22NULL%22,%22NULL%22,%22NULL%22,%22%22,%220%22,%22%22,%22undefined%22,%22NULL%22,%22NULL%22,%22undefined%22,%220%22,%220%22,%22NULL%22,%22%22,%22NULL%22,1,%22NULL%22,%22NULL%22,%22NULL%22,%22%22,%22NULL%22,%22NULL%22,%22NULL%22,%22NULL%22,%22NULL%22,%22%22,%22%22,%22%22,%22%22,%22%22,%22%22,%22%22,%22%22%5D"
)

# Data de descarga por defecto
DL_DATA = {
    "field_negocio": "NULL",
    "field_arttipo": "NULL",
    "field_arttipos": "",
    "field_arttipo_chk": "0",
    "field_artgrupo": "NULL",
    "field_artmarca": "NULL",
    "field_proveedor": "NULL",
    "field_articulo": "",
    "field_artactivo": "NULL",
    "accion": "",
    "Informe": "301",
    "SubTitulo": "",
    "Filtro": "",
    "Filtro_A1": "",
    "Filtro_A2": "",
    "Filtro_A3": "",
}

# --- Extra: parse flow args helper ---
def _parse_flow_args_if_needed(val, date_macros: dict, report_code: str):
    """
    Permite pegar args como una lista en una sola línea estilo:
    ["303","","",*yesterday_ymd,...,NULL,undefined,1]

    Soporta atajos *today_ymd, *today_dmy, *yesterday_ymd, *yesterday_dmy
    convirtiéndolos en placeholders de fecha tomados de `date_macros`.
    Convierte tokens crudos NULL/undefined a strings JSON válidos.
    Si el primer elemento es un código numérico o coincide con report_code,
    lo reemplaza por "{code}" para que quede dinámico.
    """
    # Si ya es lista, devolver tal cual
    if isinstance(val, list):
        return val
    # Si no es string, devolver tal cual
    if not isinstance(val, str):
        return val
    s = val.strip()
    if not (s.startswith('[') and s.endswith(']')):
        return val

    dm = date_macros or {}
    reps = {
        '*today_ymd': f'"{dm.get("today_ymd", "{today:%Y-%m-%d}")}"',
        '*today_dmy': f'"{dm.get("today_dmy", "{today:%d/%m/%Y}")}"',
        '*yesterday_ymd': f'"{dm.get("yesterday_ymd", "{yesterday:%Y-%m-%d}")}"',
        '*yesterday_dmy': f'"{dm.get("yesterday_dmy", "{yesterday:%d/%m/%Y}")}"',
    }
    for k, v in reps.items():
        s = s.replace(k, v)

    # Convertir tokens crudos NULL/undefined (no entrecomillados) a strings JSON
    s = re.sub(r'(?<=\[|,)\s*NULL\s*(?=,|\])', '"NULL"', s)
    s = re.sub(r'(?<=\[|,)\s*undefined\s*(?=,|\])', '"undefined"', s)

    try:
        arr = json.loads(s)
    except Exception:
        # Si falla, devolver original para no romper
        return val

    # Normalizar primer elemento a {code}
    if isinstance(arr, list) and arr:
        first = arr[0]
        if (isinstance(first, (int, float)) and float(first).is_integer()) or (
            isinstance(first, str) and first.isdigit()
        ):
            arr[0] = "{code}"
        if isinstance(first, str) and first == str(report_code):
            arr[0] = "{code}"
    return arr

# ========================= Core HTTP =========================

def STEP(method, url, *, headers=None, params=None, data=None, referer=None, expect=(200, 302), save=None):
    h = clean_headers(headers)
    if referer:
        h["Referer"] = referer
    if method.upper() in ("POST", "PUT", "PATCH"):
        h.setdefault("Origin", BASE)
    if "/metodos/" in url or url.endswith("class.ajax.php"):
        h.setdefault("X-Requested-With", "XMLHttpRequest")
    resp = s.request(method, url, headers=h, params=params, data=data, allow_redirects=True, timeout=120)
    if DEBUG:
        if isinstance(data, dict):
            safe_data = {k: ("***" if any(token in str(k).lower() for token in ("pass", "password", "cookie", "session")) else v) for k, v in data.items()}
            data_preview = urlencode(safe_data)[:1000]
        else:
            data_preview = str(data)[:1000]
        logging.debug(f"STEP data preview: {data_preview}")
        logging.debug(f"Headers preview: referer={h.get('Referer')} xreq={h.get('X-Requested-With')}")
    print(f"{method} {resp.status_code} {url}")
    if DEBUG:
        cd = resp.headers.get('Content-Disposition')
        ct = resp.headers.get('Content-Type')
        cl = resp.headers.get('Content-Length')
        logging.debug(f"Resp headers: CT={ct} CD={cd} CL={cl}")
        try:
            if cd and "filename=" in cd:
                logging.debug(f"CD filename: {cd}")
        except Exception:
            pass
        # Si parece HTML y el contenido es chico, mostrar un snippet para ver errores del servidor
        try:
            if ct and 'text/html' in ct and len(resp.content) < 50000:
                snippet = resp.text[:1000].replace('\n', ' ')
                logging.debug(f"Resp body snippet: {snippet}")
        except Exception:
            pass
    if expect and resp.status_code not in expect:
        print(f"  !! HTTP inesperado: {resp.status_code} (esperado {expect})")
        ct = resp.headers.get('Content-Type')
        if ct and 'text/html' in ct:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"error_response_{ts}.html"
            with open(filename, "wb") as f:
                f.write(resp.content)
            print(f"  --> HTML de error guardado en {filename}")
    if save:
        with open(save, "wb") as f:
            f.write(resp.content)
        print(f"  -> guardado en {save}")
    return resp

def STEP_RAW(method, url, *, headers=None, params=None, body_str="", referer=None, expect=(200, 302), save=None):
    """Envía EXACTAMENTE el cuerpo form-urlencoded provisto (ya encodeado)."""
    h = clean_headers(headers)
    if referer:
        h["Referer"] = referer
    if method.upper() in ("POST", "PUT", "PATCH"):
        h.setdefault("Origin", BASE)
    h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    if "/metodos/" in url or url.endswith("class.ajax.php"):
        h.setdefault("X-Requested-With", "XMLHttpRequest")
    if DEBUG:
        logging.debug(f"STEP_RAW body preview (first 1500): {body_str[:1500]}")
    resp = s.request(method, url, headers=h, params=params, data=body_str.encode("utf-8"), allow_redirects=True, timeout=120)
    print(f"{method} {resp.status_code} {url}")
    if DEBUG:
        cd = resp.headers.get('Content-Disposition')
        ct = resp.headers.get('Content-Type')
        cl = resp.headers.get('Content-Length')
        logging.debug(f"Resp headers: CT={ct} CD={cd} CL={cl}")
        try:
            if cd and "filename=" in cd:
                logging.debug(f"CD filename: {cd}")
        except Exception:
            pass
        try:
            if ct and 'text/html' in ct and len(resp.content) < 50000:
                snippet = resp.text[:1000].replace('\n', ' ')
                logging.debug(f"Resp body snippet: {snippet}")
        except Exception:
            pass
    if expect and resp.status_code not in expect:
        print(f"  !! HTTP inesperado: {resp.status_code} (esperado {expect})")
        ct = resp.headers.get('Content-Type')
        if ct and 'text/html' in ct:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"error_response_{ts}.html"
            with open(filename, "wb") as f:
                f.write(resp.content)
            print(f"  --> HTML de error guardado en {filename}")
    if save:
        with open(save, "wb") as f:
            f.write(resp.content)
        print(f"  -> guardado en {save}")
    return resp

# Construye el body de aa_* a partir de sfunc + args (args_raw o lista no-encodeada)
def build_aa_body(sfunc, args_raw=None, args=None):
    if args_raw is None and args is not None:
        # args es una lista -> encodeamos JSON y luego percent-encode
        args_raw = quote(json.dumps(args, ensure_ascii=False))
    if args_raw is None:
        args_raw = ""
    if DEBUG:
        logging.debug(f"AA build -> sfunc={sfunc} args_raw_len={len(args_raw) if args_raw else 0}")
    return f"aa_afunc=call&aa_sfunc={sfunc}&aa_cfunc=&aa_sfunc_args%5B%5D={args_raw}"

# --- Placeholders dinámicos ---
# {code} -> código de informe
# {today:%d/%m/%Y} | {yesterday:%Y-%m-%d} | {month_start:%d/%m/%Y} | {month_end:%d/%m/%Y}
# {last_month_start:...} | {last_month_end:...}
# Si no se especifica formato, usa "%d/%m/%Y".

def _render_str_with_ctx(s: str, ctx: dict) -> str:
    if not isinstance(s, str):
        return s
    out = s.replace("{code}", str(ctx.get("code", "")))

    pattern = re.compile(r"\{(today|yesterday|month_start|month_end|last_month_start|last_month_end)(?::([^}]+))?\}")

    def _repl(m):
        key = m.group(1)
        fmt = m.group(2) or "%d/%m/%Y"
        dt = ctx.get(key)
        if not dt:
            return m.group(0)
        # dt puede ser date o datetime; ambos tienen strftime
        return dt.strftime(fmt)

    return pattern.sub(_repl, out)



def replace_placeholders(obj, ctx: dict):
    if isinstance(obj, dict):
        return {k: replace_placeholders(v, ctx) for k, v in obj.items()}
    if isinstance(obj, list):
        return [replace_placeholders(v, ctx) for v in obj]
    if isinstance(obj, str):
        return _render_str_with_ctx(obj, ctx)
    return obj

# ========================= Post-proceso opcional =========================

def _run_postprocess_if_any(pp_cfg: dict | None, resp: requests.Response, ctx: dict, save_as: str):
    """Ejecuta un callback de post-proceso si está configurado.

    pp_cfg debe tener la forma:
      {"module": "report_pipeline", "function": "process_and_upload", "options": { ... }}

    Le pasamos al callback:
      - content: bytes del XLS/CSV
      - content_type: content-type HTTP
      - meta: dict con info útil (save_as sugerido, report code, fechas renderizadas, headers, etc.)
      - options: dict con las opciones del YAML (sheet_id, sheet_name, etc.)
    """
    if not pp_cfg:
        return False
    module_name = pp_cfg.get("module")
    func_name = pp_cfg.get("function")
    options = pp_cfg.get("options") or {}
    if not module_name or not func_name:
        logging.warning("[POST] Config incompleta: falta module/function")
        return False
    try:
        mod = importlib.import_module(module_name)
    except Exception as e:
        logging.error(f"[POST] No pude importar módulo '{module_name}': {e}")
        return False
    func = getattr(mod, func_name, None)
    if not callable(func):
        logging.error(f"[POST] La función '{func_name}' no existe en '{module_name}'")
        return False

    content = resp.content or b""
    ct = (resp.headers or {}).get("Content-Type")
    meta = {
        "suggested_filename": save_as,
        "report_code": str(ctx.get("code", "")),
        "today": ctx.get("today"),
        "yesterday": ctx.get("yesterday"),
        "month_start": ctx.get("month_start"),
        "month_end": ctx.get("month_end"),
        "last_month_start": ctx.get("last_month_start"),
        "last_month_end": ctx.get("last_month_end"),
        "headers": dict(resp.headers or {}),
        "url": getattr(resp, 'url', None),
        "status_code": getattr(resp, 'status_code', None),
    }
    try:
        logging.info("[POST] Ejecutando post-proceso...")
        func(content=content, content_type=ct, meta=meta, options=options)
        logging.info("[POST] Hecho.")
        return True
    except Exception as e:
        logging.exception(f"[POST] Falló el post-proceso: {e}")
        return False

# ========================= Config =========================

def default_config():
    return {
        "base_url": BASE_DEFAULT,
        "ua": UA_DEFAULT,
        "credenciales": {"user": USER, "pass": PASS, "grupo": GRUPO},
        "report_code": str(REPORT),
        "base_select": "179",
        "save_as": SAVE_AS,
        "preflights_index_main": PRE_INDEX_MAIN,
        "preflights_pantalla": PRE_PANTALLA,
        "armar_filtro": {"endpoint": "/gestion/herramientas/metodos/ajax-articulos.php", "raw": ARMAR_RAW},
        "download": {
            "endpoint": "/gestion/herramientas/informes/informes_excel.php",
            "data": DL_DATA,
        },
        "output": {
            "save_file": True,
            "postprocess": None  # {"module": "report_pipeline", "function": "process_and_upload", "options": {...}}
        },
    }

def load_config(path):
    cfg = default_config()
    if path and Path(path).exists() and yaml is not None:
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
        # merge superficial
        for k, v in user_cfg.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg

# ========================= Login =========================

def login():
    # Igual que navegador
    STEP("GET", f"{BASE}/gestion/index_login.php")
    STEP("GET", f"{BASE}/gestion/index_login.php", params={"Ancho": "1920", "Alto": "1080"}, referer=f"{BASE}/gestion/index_login.php")
    STEP(
        "POST",
        f"{BASE}/gestion/index_login.php",
        params={"Ancho": "1920", "Alto": "1080"},
        referer=f"{BASE}/gestion/index_login.php?Ancho=1920&Alto=1080",
        data={
            "userlogincj": USER,
            "passwdlogincj": PASS,
            "baselogincj": GRUPO,
            "accion": "Ingresar",
        },
    )
    STEP("GET", f"{BASE}/gestion/index_main.php", referer=f"{BASE}/gestion/index_login.php?Ancho=1920&Alto=1080")

# ========================= Main =========================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vars", default="vars.yml", help="Archivo YAML con variables (por defecto: vars.yml)")
    parser.add_argument("--insecure", action="store_true", help="Deshabilita verificación SSL (solo pruebas)")
    parser.add_argument("--extra-ca", default=None, help="Ruta a bundle PEM adicional para SSL")
    parser.add_argument("--debug", action="store_true", help="Log detallado de headers, payloads y respuestas")
    args = parser.parse_args()

    cfg = load_config(args.vars)

    _log_setup(args.debug)

    # Reconfigurar globals desde config
    global BASE, UA, USER, PASS, GRUPO, REPORT, SAVE_AS
    BASE = cfg.get("base_url", BASE_DEFAULT)
    UA = cfg.get("ua", UA_DEFAULT)
    cred = cfg.get("credenciales") or {}
    USER = cred.get("user", USER)
    PASS = cred.get("pass", PASS)
    GRUPO = cred.get("grupo", GRUPO)
    REPORT = str(cfg.get("report_code", REPORT))
    SAVE_AS = (cfg.get("save_as") or f"informe_{REPORT}.xls").replace("{code}", REPORT)
    base_select = str(cfg.get("base_select", "179"))

    # Session headers / verify
    s.headers.update({"User-Agent": UA})
    if args.insecure:
        import urllib3
        s.verify = False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    elif args.extra_ca:
        s.verify = args.extra_ca

    # --- Contexto de placeholders (código + fechas) ---
    tzname = cfg.get("timezone")
    if tzname and ZoneInfo:
        try:
            tz = ZoneInfo(tzname)
        except Exception:
            tz = None
    else:
        tz = None
    now = datetime.now(tz) if tz else datetime.now()

    today = now.date()
    yesterday = today - timedelta(days=1)
    month_start = today.replace(day=1)
    if today.month == 12:
        next_month_start = today.replace(year=today.year + 1, month=1, day=1)
    else:
        next_month_start = today.replace(month=today.month + 1, day=1)
    month_end = next_month_start - timedelta(days=1)
    last_month_end = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)

    ctx = {
        "code": REPORT,
        "today": today,
        "yesterday": yesterday,
        "month_start": month_start,
        "month_end": month_end,
        "last_month_start": last_month_start,
        "last_month_end": last_month_end,
    }
    if DEBUG:
        logging.debug(f"CTX -> code={REPORT} today={today} yesterday={yesterday} month_start={month_start} month_end={month_end}")
        logging.debug(f"base_select={base_select}")

    # 1) Pre GET a index_main (como en tu flujo)
    STEP("GET", f"{BASE}/gestion/index_main.php")

    # 2) Login
    login()

    # 3) JS/AJAX de soporte (con report dinámico en referers cuando aplica)
    STEP("GET", f"{BASE}/gestion/herramientas/class.ajax.php", params={"ajaxagent": "js", "this_url": ""}, referer=f"{BASE}/gestion/index_main.php")
    STEP("GET", f"{BASE}/gestion/herramientas/class.ajax.php", params={"ajaxagent": "js", "this_url": ""}, referer=f"{BASE}/gestion/index_main.php")

    # 4) Preflights en index_main.php
    for p in cfg.get("preflights_index_main", []):
        if isinstance(p, str):
            body = p
        else:
            body = build_aa_body(p.get("sfunc", ""), p.get("args_raw"), p.get("args"))
        STEP_RAW("POST", f"{BASE}/gestion/herramientas/metodos/ajax-celulares.php", referer=f"{BASE}/gestion/index_main.php", body_str=body)

    # 5) Cambios en index_main (según tu flujo original, los dejamos fijos)
    STEP("POST", f"{BASE}/gestion/index_main.php", referer=f"{BASE}/gestion/index_main.php", data={"codigoPantalla": "", "baseSelect": base_select, "destroySession": ""})
    STEP("POST", f"{BASE}/gestion/index_main.php", referer=f"{BASE}/gestion/index_main.php", data={"hidden": "", "codigoPantalla": "7001", "baseSelect": "", "destroySession": ""})

    # 6) Otro GET de soporte
    STEP("GET", f"{BASE}/gestion/herramientas/class.ajax.php", params={"ajaxagent": "js", "this_url": ""}, referer=f"{BASE}/gestion/index_main.php")

    # 7) Abrir pantalla de filtros del informe REPORT
    pantalla_url = f"{BASE}/gestion/herramientas/pantallas/pantalla_informes_filtro.php"
    pantalla_ref = f"{pantalla_url}?Informe_Codigo={REPORT}"
    STEP("GET", pantalla_url, params={"Informe_Codigo": REPORT}, referer=f"{BASE}/gestion/index_main.php")

    # 8) JS de filtros
    STEP("GET", f"{BASE}/gestion/herramientas/class.ajax.php", params={"ajaxagent": "js", "this_url": ""}, referer=pantalla_ref)
    STEP("GET", f"{BASE}//gestion//js/gestion_functions.js", params={"3029a8fe4ed0faa8e8f3be93ec2f2647": "null"}, referer=pantalla_ref)
    STEP("GET", f"{BASE}/gestion/herramientas/informes/filtros/filtro_informes.js", params={"fccec067a474b0826aa37dc7d7ef7d62": "null"}, referer=pantalla_ref)
    STEP("GET", f"{BASE}/gestion/herramientas/class.ajax.php", params={"ajaxagent": "js", "this_url": ""}, referer=pantalla_ref)

    # 9) Preflights con referer de pantalla
    for p in cfg.get("preflights_pantalla", []):
        if isinstance(p, str):
            body = p
        else:
            body = build_aa_body(p.get("sfunc", ""), p.get("args_raw"), p.get("args"))
        STEP_RAW("POST", f"{BASE}/gestion/herramientas/metodos/ajax-celulares.php", referer=pantalla_ref, body_str=body)

    # 10) armar_filtro (raw o con sfunc/args)
    af = cfg.get("armar_filtro") or {}
    af_ep = af.get("endpoint", "/gestion/herramientas/metodos/ajax-articulos.php")
    use_args_mode = (af.get("args") is not None) or (af.get("args_raw") is not None)
    if use_args_mode:
        if DEBUG:
            logging.debug("armar_filtro mode: args/args_raw (preferido)")
        sfunc = af.get("sfunc", "armar_filtroInformes")
        flow_or_list = af.get("args")
        flow_or_list = _parse_flow_args_if_needed(flow_or_list, cfg.get("date_macros"), REPORT)
        args_list = replace_placeholders(flow_or_list, ctx) if flow_or_list is not None else None
        args_raw = _render_str_with_ctx(af.get("args_raw", ""), ctx) if af.get("args_raw") else None
        body = build_aa_body(sfunc, args_raw=args_raw, args=args_list)
    elif af.get("raw"):
        if DEBUG:
            logging.debug("armar_filtro mode: raw (fallback)")
        raw = _render_str_with_ctx(af["raw"], ctx)
        # Por compatibilidad si el raw trae el código como %22301%22
        raw = raw.replace("%22301%22", f"%22{REPORT}%22")
        body = raw
    else:
        if DEBUG:
            logging.debug("armar_filtro mode: default args (sin configuración específica)")
        sfunc = af.get("sfunc", "armar_filtroInformes")
        body = build_aa_body(sfunc, args_raw=None, args=None)
    if DEBUG and body:
        try:
            from urllib.parse import parse_qs, unquote
            qs = parse_qs(body)
            raw = qs.get('aa_sfunc_args[]') or qs.get('aa_sfunc_args%5B%5D')
            if raw:
                raw0 = raw[0]
                decoded = unquote(raw0)
                arr = json.loads(decoded)
                logging.debug(f"armar_filtro args parsed ({len(arr)} items):")
                for i, v in enumerate(arr[:50]):
                    logging.debug(f"  [{i}] = {v}")
                if len(arr) > 50:
                    logging.debug(f"  ... ({len(arr)-50} más)")
        except Exception as e:
            logging.debug(f"No pude parsear args de armar_filtro: {e}")
    STEP_RAW(
        "POST",
        f"{BASE}{af_ep}",
        headers=af.get("headers"),
        referer=pantalla_ref,
        body_str=body,
    )

    # 11) Descarga
    dl = cfg.get("download") or {}
    dl_ep = dl.get("endpoint", "/gestion/herramientas/informes/informes_excel.php")

    # Reemplazo de placeholders dinámicos ({code}, {today:...}, etc.)
    save_as_tpl = cfg.get("save_as") or dl.get("save_as") or "informe_{code}.xls"
    save_as = _render_str_with_ctx(save_as_tpl, ctx)
    out_cfg = cfg.get("output") or {}
    save_file = bool(out_cfg.get("save_file", True))
    post_cfg = out_cfg.get("postprocess")

    # Modo A: body RAW (percent-encodeado tal cual sale de Proxyman)
    dl_raw = dl.get("raw")
    if isinstance(dl_raw, str):
        dl_raw = dl_raw.strip()
    if dl_raw:
        if DEBUG:
            logging.debug("download mode: RAW body")
        if DEBUG:
            logging.debug(f"Download RAW -> referer={pantalla_ref} endpoint={dl_ep}")
        body_raw = _render_str_with_ctx(dl_raw, ctx)
        resp_dl = STEP_RAW(
            "POST",
            f"{BASE}{dl_ep}",
            headers=dl.get("headers"),
            referer=pantalla_ref,
            body_str=body_raw,
        )
        if DEBUG:
            ct = resp_dl.headers.get('Content-Type')
            logging.debug(f"Download resp CT={ct} size={len(resp_dl.content) if resp_dl.content else 0}")
        if post_cfg:
            _run_postprocess_if_any(post_cfg, resp_dl, ctx, save_as)
        if save_file:
            try:
                with open(save_as, "wb") as f:
                    f.write(resp_dl.content)
                print(f"  -> guardado en {save_as}")
            except Exception as e:
                logging.error(f"No pude guardar {save_as}: {e}")
    else:
        # Modo B: formulario (dict)
        if DEBUG:
            logging.debug("download mode: FORM data")
        if DEBUG:
            logging.debug(f"Download FORM -> referer={pantalla_ref} endpoint={dl_ep}")
        data = dict(dl.get("data") or {})
        data = replace_placeholders(data, ctx)
        # Ajustes comunes
        data.setdefault("Informe", str(REPORT))

        if DEBUG:
            try:
                logging.debug("Download data (form):")
                for k in sorted(data.keys()):
                    if any(sens in k.lower() for sens in ("passwd", "password")):
                        continue
                    logging.debug(f"  {k} = {data[k]}")
            except Exception:
                pass
        resp_dl = STEP(
            "POST",
            f"{BASE}{dl_ep}",
            headers=dl.get("headers"),
            referer=pantalla_ref,
            data=data,
        )
        if DEBUG:
            ct = resp_dl.headers.get('Content-Type')
            logging.debug(f"Download resp CT={ct} size={len(resp_dl.content) if resp_dl.content else 0}")
        if post_cfg:
            _run_postprocess_if_any(post_cfg, resp_dl, ctx, save_as)
        if save_file:
            try:
                with open(save_as, "wb") as f:
                    f.write(resp_dl.content)
                print(f"  -> guardado en {save_as}")
            except Exception as e:
                logging.error(f"No pude guardar {save_as}: {e}")

    # 12) estado_Reporte (opcional, no bloqueante)
    try:
        STEP("POST", f"{BASE}/gestion/herramientas/metodos/ajax-celulares.php", referer=pantalla_ref, data={"aa_afunc": "call", "aa_sfunc": "estado_Reporte", "aa_cfunc": ""})
    except Exception:
        pass

    print("Listo.")
    if DEBUG:
        # Mostrar cookie de sesión reducida (para chequear rotación)
        try:
            c = s.cookies.get('PHPSESSID')
            if c:
                logging.debug(f"PHPSESSID presente: {c[:8]}... (oculto)")
        except Exception:
            pass

if __name__ == "__main__":
    raise SystemExit(main())