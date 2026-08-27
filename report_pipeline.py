# report_pipeline.py
import io
import pandas as pd
from typing import Any, Dict, Tuple, Optional
from typing import List

# --- Google auth helpers ---
try:
    import google.auth as _google_auth  # Application Default Credentials
except Exception:
    _google_auth = None
try:
    from google.oauth2.service_account import Credentials as _SA_Credentials  # optional SA JSON support
except Exception:  # keep import optional
    _SA_Credentials = None

# ---------- Helpers for column resolution & patterns ----------
import re as _re_mod

def _norm_name(s: str) -> str:
    """Normalize a header/column name: strip, lower, collapse spaces."""
    if s is None:
        return ""
    s2 = str(s).strip().lower()
    s2 = _re_mod.sub(r"\s+", " ", s2)
    return s2

def _resolve_col_to_index(col_spec, colnames: list[str]) -> int | None:
    """
    Resolve a column spec (header string, A1 letter(s) or 0-based index) to a 0-based index.
    Tries exact match, case-insensitive normalized match, A1 letters, and int index.
    Returns None if not found.
    """
    if not colnames:
        return None
    # 1) Header string exact
    if isinstance(col_spec, str):
        if col_spec in colnames:
            return colnames.index(col_spec)
        # 2) Header string normalized (case-insensitive, spaces collapsed)
        try:
            norm_target = _norm_name(col_spec)
            norm_map = {_norm_name(n): i for i, n in enumerate(colnames)}
            if norm_target in norm_map:
                return norm_map[norm_target]
        except Exception:
            pass
        # 3) A1 letters → index
        if _re_mod.fullmatch(r"[A-Za-z]+", col_spec):
            idx = 0
            for ch in col_spec.strip().upper():
                idx = idx * 26 + (ord(ch) - ord('A') + 1)
            return max(0, idx - 1)
    # 4) Direct integer index
    if isinstance(col_spec, int) and 0 <= col_spec < len(colnames):
        return col_spec
    return None


def _pattern_from_decimals(decimals: int) -> str:
    """Builds a Sheets pattern like '#,##0.00' for given decimals."""
    d = max(0, int(decimals))
    return "#,##0" + ("." + ("0" * d) if d > 0 else "")

# --- Locale-aware number parser for DataFrames ---
def _coerce_series_number_locale(series: pd.Series) -> pd.Series:
    """
    Robustly convert strings like "1.234,56" or "1,234.56" or "123456" to floats.
    Leaves true NaN as NaN. Returns a numeric pandas Series.
    """
    s = series.astype(str).str.strip()

    def _fix_token(t: str) -> str:
        if t == "" or t.lower() in {"nan", "none"}:
            return ""
        # Both separators present
        if "," in t and "." in t:
            # 1.234.567,89 -> decimal is comma
            if _re_mod.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d+", t):
                t = t.replace(".", "").replace(",", ".")
            # 1,234,567.89 -> decimal is dot
            elif _re_mod.fullmatch(r"\d{1,3}(?:,\d{3})+\.\d+", t):
                t = t.replace(",", "")
            else:
                # Heuristic: last separator is decimal
                if t.rfind(",") > t.rfind("."):
                    t = t.replace(".", "").replace(",", ".")
                else:
                    t = t.replace(",", "")
        elif "," in t:
            # Assume comma is decimal, any dots are thousands
            t = t.replace(".", "").replace(",", ".")
        else:
            # Only dot (decimal) or only digits (integer) → leave as-is
            t = t
        return t

    s2 = s.map(_fix_token)
    return pd.to_numeric(s2, errors="coerce")

# ---------- 1) Cargar tablas desde el binario ----------
def read_report_bytes_to_df(raw_bytes: bytes, *, options: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """
    Lector robusto:
      - Respeta options['input_format'] si viene ("csv"|"html"|"excel"|"auto").
      - CSV: intenta encoding provisto; si no, latin1/utf-8-sig/utf-8/cp1252; separador ; o , (heurística).
      - HTML: usa pandas.read_html y elige la tabla "más grande".
      - Excel:
          * autodetect con ExcelFile
          * xlsx → openpyxl
          * xls (OLE2) → xlrd
    Lanza RuntimeError si no puede parsear.
    """
    import re
    opts = options or {}
    hint = str(opts.get("input_format", "auto")).lower().strip()

    head = raw_bytes[:4096] if raw_bytes else b""

    def looks_like_xlsx(b: bytes) -> bool:
        return b.startswith(b"PK\x03\x04")  # ZIP header

    def looks_like_xls(b: bytes) -> bool:
        return b.startswith(b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1")  # OLE2/CFB

    def looks_like_html(b: bytes) -> bool:
        s = b.lstrip()[:2048].lower()
        return s.startswith(b"<!doctype html") or s.startswith(b"<html") or b"<table" in s

    def looks_like_csv(b: bytes) -> bool:
        if not b or b"\x00" in b[:8192]:
            return False
        s = b[:16384]
        has_nl = (b"\n" in s) or (b"\r" in s)
        has_sep = any(sep in s for sep in (b";", b",", b"\t"))
        if looks_like_html(s):
            return False
        return has_nl and has_sep

    def decode_candidates(b: bytes, encodings=("latin1","utf-8-sig","utf-8","cp1252")) -> Optional[str]:
        for enc in encodings:
            try:
                return b.decode(enc)
            except Exception:
                continue
        return None

    def _promote_headers_if_needed(df_in: pd.DataFrame) -> tuple[pd.DataFrame, int | None]:
        """
        Heurística para normalizar encabezados cuando el XLS trae:
          - Fila 1 = título del informe
          - Fila 2 = vacía
          - Fila 3 = encabezados reales
        También maneja casos con muchas columnas 'Unnamed'.
        Devuelve (df_normalizado, header_row_index_1based_o_None).
        """
        df = df_in.copy()
        if df.empty:
            return df, None

        # 0) Cuando el caller conoce columnas distintivas, buscamos la fila de
        # encabezados por firma en vez de asumir que siempre está en la fila 3.
        raw_signatures = opts.get("header_signatures") or []
        signatures = []
        for candidate in raw_signatures:
            if not isinstance(candidate, (list, tuple, set)):
                continue
            signature = {
                re.sub(r"\s+", " ", str(value).strip()).casefold()
                for value in candidate
                if value is not None and str(value).strip()
            }
            if signature:
                signatures.append(signature)

        if signatures:
            scan_rows = max(1, int(opts.get("header_scan_rows", 20)))
            for header_idx0 in range(min(scan_rows, len(df))):
                row = df.iloc[header_idx0]
                header_values = []
                for value in row.tolist():
                    if value is None or pd.isna(value):
                        header_values.append("")
                    else:
                        header_values.append(
                            re.sub(r"\s+", " ", str(value).strip())
                        )
                row_set = {value.casefold() for value in header_values if value}
                if any(signature.issubset(row_set) for signature in signatures):
                    df = df.iloc[header_idx0 + 1 :].reset_index(drop=True)
                    df.columns = header_values
                    return df, header_idx0 + 1

        # 1) Si la primera fila es idéntica a las columnas, esa fila es un encabezado repetido: drop(0)
        try:
            first_row_vals = [str(x) for x in df.iloc[0].tolist()]
            col_vals = [str(c) for c in df.columns.tolist()]
            if first_row_vals == col_vals:
                df = df.iloc[1:].reset_index(drop=True)
                return df, 1
        except Exception:
            pass

        # 2) Si hay muchas columnas 'Unnamed', promovemos una fila con más no-nulos como encabezado
        col_names = [str(c) for c in df.columns]
        if len(col_names) > 0:
            unnamed_ratio = sum(name.lower().startswith("unnamed") for name in col_names) / len(col_names)
        else:
            unnamed_ratio = 0.0

        if unnamed_ratio >= 0.5:
            head_block = df.head(5)
            # Índice (0-based) de la fila con más celdas no-nulas en el bloque inicial
            non_null_counts = head_block.notna().sum(axis=1)
            header_idx0 = int(non_null_counts.idxmax())
            new_cols = [str(x).strip() if x is not None else "" for x in df.iloc[header_idx0].tolist()]
            df = df.iloc[header_idx0 + 1 :].reset_index(drop=True)
            df.columns = new_cols
            return df, header_idx0 + 1  # 1-based

        # 3) Patrón Caddis clásico: [título][fila vacía][encabezados]
        if df.shape[0] >= 3:
            r0, r1, r2 = df.iloc[0], df.iloc[1], df.iloc[2]
            if (r0.count() <= 3) and (r1.count() == 0) and (r2.count() >= max(3, int(0.5 * df.shape[1]))):
                new_cols = [str(x).strip() if x is not None else "" for x in r2.tolist()]
                df = df.iloc[3:].reset_index(drop=True)
                df.columns = new_cols
                return df, 3

        return df, None

    # Orden de detección si es AUTO
    if hint == "auto":
        sniff_order: List[str] = []
        if looks_like_xlsx(head):
            sniff_order.append("excel_xlsx")
        if looks_like_xls(head):
            sniff_order.append("excel_xls")
        if looks_like_html(head):
            sniff_order.append("html")
        if looks_like_csv(head):
            sniff_order.append("csv")
        if not sniff_order:
            sniff_order = ["html", "csv", "excel_xlsx", "excel_xls"]
    elif hint == "csv":
        sniff_order = ["csv"]
    elif hint == "html":
        sniff_order = ["html"]
    elif hint == "excel":
        sniff_order = ["excel_xlsx", "excel_xls"]
    else:
        sniff_order = ["html", "csv", "excel_xlsx", "excel_xls"]

    last_err: Optional[Exception] = None

    for mode in sniff_order:
        try:
            bio = io.BytesIO(raw_bytes)

            if mode == "csv":
                # Si el contenido parece HTML, avisar explícitamente
                if looks_like_html(head):
                    raise RuntimeError("Se solicitó CSV, pero el servidor devolvió HTML (posible 'XLS' simulado). Cambiá input_format a 'html' o corregí el endpoint.")

                # encoding
                enc = opts.get("csv_encoding")
                if enc:
                    try:
                        text = raw_bytes.decode(enc)
                    except Exception as e:
                        raise RuntimeError(f"No pude decodificar el CSV con encoding {enc}: {e}")
                else:
                    text = decode_candidates(raw_bytes)
                    if text is None:
                        text = raw_bytes.decode("latin1", errors="replace")

                # delimitador
                delim = opts.get("csv_delimiter")
                if not delim:
                    sample = text[:2048]
                    delim = ";" if sample.count(";") >= sample.count(",") else ","

                try:
                    df = pd.read_csv(io.StringIO(text), delimiter=delim)
                    df.attrs["source_format"] = "csv"
                    return df
                except Exception as e:
                    # Si el usuario *forzó* CSV, no sigas silenciosamente: explota con contexto
                    if hint == "csv":
                        preview = text[:200].replace("\n", "\\n")
                        raise RuntimeError(f"Fallo leyendo CSV (delim='{delim}'). Primeros bytes: {preview!r}. Error: {e}")
                    # Si era AUTO, seguimos probando otros formatos
                    raise

            if mode == "html":
                tables = pd.read_html(bio, thousands='.', decimal=',')
                tables = [t for t in tables if t is not None and not t.empty]
                if tables:
                    sizes = [int(t.shape[0]) * int(t.shape[1]) for t in tables]
                    best = tables[int(max(range(len(sizes)), key=lambda i: sizes[i]))]
                    df_best = best.copy()
                    try:
                        first_equals_cols = [str(x) for x in df_best.iloc[0].tolist()] == [str(c) for c in df_best.columns.tolist()]
                        if first_equals_cols:
                            df_best = df_best.iloc[1:].reset_index(drop=True)
                    except Exception:
                        pass
                    df_best.attrs["source_format"] = "html"
                    return df_best
                else:
                    raise ValueError("read_html no encontró tablas útiles")

            if mode == "excel_xlsx":
                df_raw = pd.read_excel(bio, engine="openpyxl", header=None)
                df, hdr_row = _promote_headers_if_needed(df_raw)
                df.attrs["source_format"] = "excel_xlsx"
                if hdr_row is not None:
                    df.attrs["header_row_index_in_source"] = hdr_row  # 1-based
                return df

            if mode == "excel_xls":
                # Primer intento: xlrd directo
                try:
                    df_raw = pd.read_excel(bio, engine="xlrd", header=None)
                    df, hdr_row = _promote_headers_if_needed(df_raw)
                    df.attrs["source_format"] = "excel_xls"
                    if hdr_row is not None:
                        df.attrs["header_row_index_in_source"] = hdr_row  # 1-based
                    return df
                except Exception as e_xlrd:
                    # Fallback 1: algunos "XLS" vienen como OLE2 pero con estructura rara;
                    # intentamos abrir el stream "Workbook" manualmente con olefile.
                    try:
                        from olefile import OleFileIO  # requiere paquete "olefile"
                        with OleFileIO(io.BytesIO(raw_bytes)) as ole:
                            if ole.exists("Workbook"):
                                with ole.openstream("Workbook") as stream:
                                    stream_bytes = stream.read()
                                df_raw = pd.read_excel(io.BytesIO(stream_bytes), engine="xlrd", header=None)
                                df, hdr_row = _promote_headers_if_needed(df_raw)
                                df.attrs["source_format"] = "excel_xls"
                                if hdr_row is not None:
                                    df.attrs["header_row_index_in_source"] = hdr_row  # 1-based
                                return df
                    except Exception:
                        pass

                    # Fallback 2: muchos servidores devuelven HTML o CSV con content-type XLS.
                    if looks_like_html(head):
                        try:
                            tables = pd.read_html(io.BytesIO(raw_bytes), thousands='.', decimal=',')
                            tables = [t for t in tables if t is not None and not t.empty]
                            if tables:
                                sizes = [int(t.shape[0]) * int(t.shape[1]) for t in tables]
                                best = tables[int(max(range(len(sizes)), key=lambda i: sizes[i]))]
                                best.attrs["source_format"] = "html"
                                return best
                        except Exception:
                            pass

                    if looks_like_csv(head):
                        # Intentar como CSV (heurística de delimitador/encoding)
                        enc = opts.get("csv_encoding")
                        if enc:
                            try:
                                text = raw_bytes.decode(enc)
                            except Exception:
                                text = None
                        else:
                            text = decode_candidates(raw_bytes)
                        if text is None:
                            text = raw_bytes.decode("latin1", errors="replace")
                        sample = text[:2048]
                        delim = opts.get("csv_delimiter") or (";" if sample.count(";") >= sample.count(",") else ",")
                        try:
                            df = pd.read_csv(io.StringIO(text), delimiter=delim)
                            df.attrs["source_format"] = "csv"
                            return df
                        except Exception:
                            pass

                    # Si todo falla, re-levantar el error original de xlrd con contexto.
                    raise RuntimeError(f"Fallo leyendo XLS (xlrd). Posible 'XLS' malformado o HTML disfrazado. Error original: {e_xlrd!r}")

        except Exception as e:
            last_err = e
            continue

    # Todos fallaron → guardar snapshot y fallar con detalle
    try:
        with open("/tmp/report_first_512.bin", "wb") as f:
            f.write(raw_bytes[:512])
    except Exception:
        pass

    msg = (
        "No pude parsear el informe como CSV, HTML ni Excel. "
        f"Intentos: {sniff_order}. Último error: {last_err!r}"
    )
    raise RuntimeError(msg)

# ---------- 2) Procesamiento por informe ----------
def _apply_generic_format(df: pd.DataFrame, fmt: Dict[str, Any]) -> pd.DataFrame:
    """
    Reglas genéricas de formateo tomadas desde YAML (output.postprocess.options.format).
    Soporta:
      - select_columns: [list] → mantiene ese orden
      - rename: {old:new, ...}
      - drop_rows_where: {col: [vals,...], ...}
      - keep_rows_where: {col: [vals,...], ...}
      - strip_whitespace: true|false → aplica strip() a todas las columnas string
      - parse_numbers: [col1, col2, ...] → convierte "1.234,56" a 1234.56
      - order_by: [colA, -colB] (- indica descendente)
    """
    if not isinstance(fmt, dict) or not fmt:
        return df

    df = df.copy()

    # rename
    ren = fmt.get("rename") or fmt.get("renombres") or {}
    if isinstance(ren, dict) and ren:
        df = df.rename(columns=ren)

    # strip_whitespace (preserva NA sin convertirlos en "nan")
    if fmt.get("strip_whitespace", True):
        text_cols = list(df.select_dtypes(include=["object", "string"]).columns)
        for c in text_cols:
            df[c] = df[c].astype("string").str.strip()

    # drop_rows_contains
    drc = fmt.get("drop_rows_contains", {})
    if isinstance(drc, dict) and drc:
        for col, substrs in drc.items():
            if col in df.columns and isinstance(substrs, list):
                for s in substrs:
                    df = df[~df[col].astype(str).str.contains(str(s), na=False, case=False)]

    # keep_rows_where
    krw = fmt.get("keep_rows_where", {})
    if isinstance(krw, dict) and krw:
        for col, vals in krw.items():
            if col in df.columns and isinstance(vals, list):
                df = df[df[col].astype(str).isin([str(v) for v in vals])]

    # parse_numbers (locale-aware & non-destructive)
    num_cols: List[str] = fmt.get("parse_numbers", []) or []
    for c in num_cols:
        if c in df.columns:
            series = df[c]
            if not pd.api.types.is_numeric_dtype(series):
                df[c] = _coerce_series_number_locale(series)

    # select_columns
    sel = fmt.get("select_columns") or []
    if isinstance(sel, list) and sel:
        keep = [c for c in sel if c in df.columns]
        if keep:
            df = df[keep]

    # order_by
    order = fmt.get("order_by") or []
    if isinstance(order, list) and order:
        cols, asc = [], []
        for item in order:
            if isinstance(item, str):
                if item.startswith("-"):
                    col = item[1:]
                    if col in df.columns:
                        cols.append(col); asc.append(False)
                else:
                    col = item
                    if col in df.columns:
                        cols.append(col); asc.append(True)
        if cols:
            df = df.sort_values(cols, ascending=asc, kind="mergesort")

    return df


def process_dataframe(df: pd.DataFrame, report_code: str, cfg: Dict[str, Any]) -> pd.DataFrame:
    """
    Dejá esta función genérica y creá ramas livianas por código de informe.
    Usá cfg['output'] o cfg['format'] si querés leer listas de PDVs a excluir, renombres, etc.
    """
    # Normalizaciones básicas
    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(how="all")
    df = df.loc[:, ~df.columns.duplicated()]

    # Reglas específicas por informe (opcionales)
    if report_code == "301":
        excluir = set(cfg.get("filters", {}).get("excluir_pdv", []))
        col_pdv = next((c for c in df.columns if "pdv" in c.lower() or "punto" in c.lower()), None)
        if col_pdv and excluir:
            df = df[~df[col_pdv].astype(str).isin(excluir)]

    if report_code == "327":
        # ejemplo: podría no hacer nada aquí; usar reglas genéricas
        pass

    # Reglas genéricas desde YAML
    fmt = cfg.get("format", {})
    df = _apply_generic_format(df, fmt)

    return df

# ---------- Google Credentials helper ----------
def get_google_credentials(out_cfg: Dict[str, Any]):
    """
    Returns google credentials using (in order):
      1) Service Account info dict passed in out_cfg['service_account_info']
      2) Service Account file path in out_cfg['service_account_file']
      3) Application Default Credentials (ADC): gcloud auth application-default login, or
         GOOGLE_APPLICATION_CREDENTIALS env var, or GCE/Cloud Run metadata.
    Respects the 'scopes' list in out_cfg (defaults to Sheets scope).
    """
    scopes = out_cfg.get("scopes") or ["https://www.googleapis.com/auth/spreadsheets"]

    # 1) Service Account JSON (in-memory dict)
    try:
        if _SA_Credentials is not None:
            sa_info = out_cfg.get("service_account_info")
            if isinstance(sa_info, dict) and sa_info:
                return _SA_Credentials.from_service_account_info(sa_info, scopes=scopes)
    except Exception:
        pass

    # 2) Service Account JSON (file path)
    try:
        if _SA_Credentials is not None:
            sa_file = out_cfg.get("service_account_file")
            if isinstance(sa_file, str) and sa_file:
                return _SA_Credentials.from_service_account_file(sa_file, scopes=scopes)
    except Exception:
        pass

    # 3) Application Default Credentials (ADC)
    if _google_auth is None:
        raise RuntimeError("google-auth no está instalado; es requerido para publicar en Google Sheets.")
    creds, _ = _google_auth.default(scopes=scopes)
    return creds
# ---------- 6) Wrapper para postprocess automático ----------
def process_and_upload(*, content: bytes, content_type: Optional[str], meta: Dict[str, Any], options: Dict[str, Any]) -> None:
    """
    Wrapper llamado por caddis_replay_all_steps._run_postprocess_if_any
    options esperadas (desde vars.yml → output.postprocess.options):
      - sheet_id (str)   [requerido]
      - sheet_name (str) [requerido]
      - scopes (list)    [opcional]
      - format (dict)    [opcional] ver _apply_generic_format()
    """
    if not content:
        raise ValueError("No se recibió contenido a procesar.")

    # Construir una cfg mínima para reutilizar process_dataframe()
    report_code = str(meta.get("report_code", ""))
    cfg: Dict[str, Any] = {}
    # Si vienen reglas de formateo en options, colgarlas en cfg['format']
    if isinstance(options, dict):
        if "format" in options:
            cfg["format"] = options.get("format") or {}
        # Permitir alias históricos
        if "renombres" in options and "format" not in options:
            cfg["format"] = {"rename": options.get("renombres")}

    # Parsear el binario a DataFrame
    df = read_report_bytes_to_df(content, options=options)

    # Log mínimo de respuesta
    ct_lower = (content_type or "").lower()
    print(f"[POST] payload bytes={len(content)} content_type={ct_lower}")

    # Proteger por si el lector no devolvió DataFrame
    if df is None:
        # Mostrar primeros bytes para diagnóstico
        sample = repr(content[:200])
        raise RuntimeError(f"Lector devolvió None. CT={ct_lower}. Primeros 200 bytes={sample}")

    print(f"[POST] DataFrame cargado: shape={df.shape} cols={list(df.columns)[:10]}")
    try:
        print("[POST] Primeras filas:\n", df.head(5).to_string())
    except Exception:
        pass

    # Procesar
    df = process_dataframe(df, report_code, cfg)

    # Ensure columns marked as number/currency become numeric before upload
    try:
        fmt_types_opt = (options or {}).get("format_types") if isinstance(options, dict) else None
        if isinstance(fmt_types_opt, dict):
            cols_to_num = []
            for key in ("number", "currency"):
                vals = fmt_types_opt.get(key) or []
                if isinstance(vals, list):
                    cols_to_num.extend(vals)
                elif isinstance(vals, (str, int)):
                    cols_to_num.append(vals)

            if cols_to_num:
                colnames = list(df.columns)
                targets = []
                for spec in cols_to_num:
                    idx = _resolve_col_to_index(spec, colnames)
                    if idx is not None and 0 <= idx < len(colnames):
                        targets.append(colnames[idx])

                # Locale-aware parser: keeps decimal, strips thousands
                def _to_number_locale(series: pd.Series) -> pd.Series:
                    s = series.astype(str).str.strip()

                    def _fix_token(t: str) -> str:
                        if t == "" or t.lower() in {"nan", "none"}:
                            return ""
                        # Both separators present
                        if "," in t and "." in t:
                            # Pattern like 1.234.567,89 -> decimal is comma
                            if _re_mod.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d+", t):
                                t = t.replace(".", "").replace(",", ".")
                            # Pattern like 1,234,567.89 -> decimal is dot
                            elif _re_mod.fullmatch(r"\d{1,3}(?:,\d{3})+\.\d+", t):
                                t = t.replace(",", "")
                            else:
                                # Heuristic: last separator wins as decimal
                                if t.rfind(",") > t.rfind("."):
                                    t = t.replace(".", "").replace(",", ".")
                                else:
                                    t = t.replace(",", "")
                        elif "," in t:
                            # Assume comma is decimal, dot (if any) is thousands
                            t = t.replace(".", "").replace(",", ".")
                        else:
                            # Only dot or only digits -> leave as-is
                            # (dot is decimal, digits are integers)
                            pass
                        return t

                    s2 = s.map(_fix_token)
                    return pd.to_numeric(s2, errors="coerce")

                for c in set(targets):
                    if c in df.columns:
                        ser = df[c]
                        if pd.api.types.is_numeric_dtype(ser):
                            continue  # already numeric
                        df[c] = _to_number_locale(ser)
    except Exception as _fmt_num_e:
        print(f"[POST] aviso: no pude normalizar numéricos por format_types: {_fmt_num_e}")

    # Pasar timezone (si viene en options) como hint para el timestamp
    try:
        tz_opt = None
        if isinstance(options, dict):
            tz_opt = options.get("timezone")
        if tz_opt:
            if not hasattr(df, "attrs"):
                df.attrs = {}
            df.attrs["timezone"] = tz_opt
    except Exception:
        pass

    # Subir a Sheets
    sheet_id = options.get("sheet_id")
    sheet_name = options.get("sheet_name")
    if not sheet_id or not sheet_name:
        raise ValueError("sheet_id y sheet_name son obligatorios en output.postprocess.options")

    # Credenciales con scopes si fueron provistos
    out_cfg = {
        "scopes": options.get("scopes") or ["https://www.googleapis.com/auth/spreadsheets"]
    }
    creds = get_google_credentials(out_cfg)

    write_mode = options.get("write_mode", "replace")
    append_header = bool(options.get("append_header", False))
    start_cell = options.get("start_cell", "A1")

    # Plumb format_types from options to df.attrs
    fmt_types = None
    if isinstance(options, dict) and options.get("format_types"):
        if not hasattr(df, "attrs"):
            df.attrs = {}
        df.attrs["format_types"] = options.get("format_types")

    # Optional currency formatting settings from options
    cur_symbol = None
    cur_pattern = None
    try:
        if isinstance(options, dict):
            cur_symbol = options.get("format_currency_symbol") or options.get("currency_symbol")
            cur_pattern = options.get("format_currency_pattern")
    except Exception:
        pass
    if not hasattr(df, "attrs"):
        df.attrs = {}
    if cur_symbol:
        df.attrs["format_currency_symbol"] = cur_symbol
    if cur_pattern:
        df.attrs["format_currency_pattern"] = cur_pattern

    # Optional: decimals for number/currency formats (default 2)
    try:
        decs = None
        if isinstance(options, dict):
            decs = options.get("format_decimals")
        if isinstance(decs, int):
            if not hasattr(df, "attrs"):
                df.attrs = {}
            df.attrs["format_decimals"] = decs
    except Exception:
        pass

    upload_to_sheet(
        df,
        sheet_id,
        sheet_name,
        creds,
        write_mode=write_mode,
        append_header=append_header,
        start_cell=start_cell,
    )

# ---------- 3) Subir a Google Sheets ----------
def upload_to_sheet(
    df: pd.DataFrame,
    sheet_id: str,
    sheet_name: str,
    creds,
    *,
    write_mode: str = "replace",
    append_header: bool = False,
    start_cell: str = "A1",
    header_row_index: int | None = None
) -> None:
    """
    write_mode:
      - "replace": limpia el rango completo de la hoja y escribe todo desde start_cell (default A1)
      - "append": busca la última fila con datos en la hoja y agrega debajo
    append_header: si write_mode=append, define si se incluye la fila de encabezados en la primera inserción; si es False, nunca se agrega encabezado
    start_cell: celda A1 para la esquina superior izquierda de escritura (p.ej., "A1", "C1")

    start_cell ahora se respeta tanto en "replace" como en "append". En append, define la **columna** y la **fila mínima** (por ejemplo, "A3" ⇒ como mínimo fila 3).
    En "append", el script **no escribe encabezado** si la hoja ya tiene datos; solo lo agrega cuando la hoja está vacía y `append_header`=True.
    Siempre escribe en `A1` el texto "Última ejecución: YYYY-MM-DD HH:MM:SS" (timezone configurable por `options.timezone`, default America/Argentina/Buenos_Aires).
    """
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    import re
    from datetime import datetime
    try:
        import pytz
    except Exception:
        pytz = None

    def _a1_range(sheet: str, a1: str) -> str:
        need_quotes = any(ch in sheet for ch in " '!:,[](){}+-*/\\")
        sheet_quoted = f"'{sheet}'" if need_quotes else sheet
        return f"{sheet_quoted}!{a1}"

    def _parse_a1_cell(a1: str):
        """
        Returns (col_letters, col_index_0based, row_1based). If row missing -> 1.
        """
        if not isinstance(a1, str) or not a1:
            return "A", 0, 1
        m = re.match(r"^([A-Za-z]+)(\d+)?$", a1.strip())
        if not m:
            return "A", 0, 1
        col_letters = m.group(1).upper()
        row = int(m.group(2)) if m.group(2) else 1
        idx = 0
        for ch in col_letters:
            idx = idx * 26 + (ord(ch) - ord('A') + 1)
        return col_letters, idx - 1, row

    def _write_timestamp(service, sheet_id: str, sheet_name: str, tz_str: str | None):
        tzname = tz_str or "America/Argentina/Buenos_Aires"
        if pytz:
            now_local = datetime.now(pytz.timezone(tzname))
        else:
            now_local = datetime.now()
        stamp = now_local.strftime("%Y-%m-%d %H:%M:%S")
        value = [[f"Última ejecución: {stamp}"]]
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=_a1_range(sheet_name, "A1"),
            valueInputOption="RAW",
            body={"values": value}
        ).execute()

    def _last_non_empty_row(values_2d):
        last = 0
        for i, row in enumerate(values_2d, start=1):
            if any(cell not in (None, "", " ") for cell in row):
                last = i
        return last

    def _norm_header(lst):
        out = []
        for x in (lst or []):
            s = "" if x is None else str(x)
            s = s.strip().lower()
            s = re.sub(r"\s+", " ", s)
            out.append(s)
        return out

    colnames_for_numeric = list(df.columns)
    fmt_types_attr = getattr(df, "attrs", {}).get("format_types") if hasattr(df, "attrs") else None
    numeric_targets: set[str] = set()
    if isinstance(fmt_types_attr, dict):
        for _typ in ("number", "currency"):
            cols = fmt_types_attr.get(_typ)
            if isinstance(cols, (list, tuple)):
                for spec in cols:
                    idx = _resolve_col_to_index(spec, colnames_for_numeric)
                    if idx is not None and 0 <= idx < len(colnames_for_numeric):
                        numeric_targets.add(colnames_for_numeric[idx])

    def _coerce_number_locale(val):
        import math
        if val is None or (isinstance(val, float) and (math.isnan(val) or math.isinf(val))):
            return None
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val).strip()
        if s == "" or s.lower() in {"nan", "none"}:
            return None
        if "," in s and "." in s:
            if _re_mod.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d+", s):
                s2 = s.replace(".", "").replace(",", ".")
            elif _re_mod.fullmatch(r"\d{1,3}(?:,\d{3})+\.\d+", s):
                s2 = s.replace(",", "")
            else:
                if s.rfind(",") > s.rfind("."):
                    s2 = s.replace(".", "").replace(",", ".")
                else:
                    s2 = s.replace(",", "")
        elif "," in s:
            s2 = s.replace(".", "").replace(",", ".")
        else:
            s2 = s
        try:
            return float(s2)
        except Exception:
            return None

    def _df_to_values(df):
        out = []
        col_names = list(df.columns)
        for _, row in df.iterrows():
            out_row = []
            for j, val in enumerate(row):
                col_name = col_names[j] if j < len(col_names) else None
                if pd.isna(val):
                    out_row.append("")
                    continue
                if col_name in numeric_targets:
                    num = _coerce_number_locale(val)
                    if num is not None:
                        out_row.append(num)
                        continue
                if isinstance(val, str) and val.strip().lower() in {"nan", "none"}:
                    out_row.append("")
                elif isinstance(val, (int, float)):
                    out_row.append(val)
                else:
                    out_row.append(str(val))
            out.append(out_row)
        return out

    def _ensure_sheet_id():
        sheets_meta = service.spreadsheets().get(
            spreadsheetId=sheet_id,
            fields="sheets.properties"
        ).execute()
        for s in sheets_meta.get("sheets", []):
            props = s.get("properties", {})
            if props.get("title") == sheet_name:
                return props.get("sheetId")
        raise RuntimeError(f"No se encontró sheetId para la hoja '{sheet_name}'")

    def _get_sheet_props():
        meta = service.spreadsheets().get(
            spreadsheetId=sheet_id,
            fields="sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))"
        ).execute()
        for s in meta.get("sheets", []):
            props = s.get("properties", {})
            if props.get("title") == sheet_name:
                grid = props.get("gridProperties", {}) or {}
                return props.get("sheetId"), int(grid.get("rowCount", 1000)), int(grid.get("columnCount", 26))
        raise RuntimeError(f"No se encontró la hoja '{sheet_name}' para leer props")

    def _ensure_capacity(start_row_1based: int, n_rows: int, start_col_idx0: int, n_cols: int):
        sh_id, row_count, col_count = _get_sheet_props()
        last_needed_row = max(1, start_row_1based + max(0, n_rows) - 1)
        last_needed_col_idx0 = max(0, start_col_idx0 + max(0, n_cols) - 1)

        requests = []
        if last_needed_row > row_count:
            requests.append({
                "insertDimension": {
                    "range": {
                        "sheetId": sh_id,
                        "dimension": "ROWS",
                        "startIndex": row_count,
                        "endIndex": last_needed_row
                    },
                    "inheritFromBefore": True
                }
            })
        if last_needed_col_idx0 + 1 > col_count:
            requests.append({
                "insertDimension": {
                    "range": {
                        "sheetId": sh_id,
                        "dimension": "COLUMNS",
                        "startIndex": col_count,
                        "endIndex": last_needed_col_idx0 + 1
                    },
                    "inheritFromBefore": True
                }
            })
        if requests:
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": requests}
            ).execute()

    service = build("sheets", "v4", credentials=creds)

    tz_opt = None
    try:
        tz_opt = getattr(df, "attrs", {}).get("timezone")
    except Exception:
        tz_opt = None

    start_col_letters, start_col_idx, start_row_min = _parse_a1_cell(start_cell or "A1")

    if header_row_index is None:
        hdr_attr = None
        try:
            hdr_attr = (getattr(df, "attrs", {}) or {}).get("header_row_index_in_source")
        except Exception:
            hdr_attr = None
        if isinstance(hdr_attr, int) and hdr_attr > 0:
            header_row_index = hdr_attr
        else:
            src_fmt = ""
            try:
                src_fmt = (getattr(df, "attrs", {}) or {}).get("source_format", "")
            except Exception:
                src_fmt = ""
            src = (src_fmt or "").lower()
            if src in ("excel_xls", "excel_xlsx"):
                header_row_index = 3
            else:
                header_row_index = 1

    # ---------------------------
    # MODO REPLACE
    # ---------------------------
    if write_mode == "replace":
        print(f"[SHEETS] write_mode=replace sheet_id={sheet_id} sheet_name={sheet_name} start_cell={start_cell} rows={df.shape[0]+1}")
        try:
            service.spreadsheets().values().clear(
                spreadsheetId=sheet_id,
                range=sheet_name
            ).execute()
            values = [df.columns.tolist()] + _df_to_values(df)
            _, start_col_idx0, start_row_1b = _parse_a1_cell(start_cell or "A1")
            total_rows_to_write = len(values)
            total_cols_to_write = len(values[0]) if values else 0
            _ensure_capacity(start_row_1b, total_rows_to_write, start_col_idx0, total_cols_to_write)
            res = service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=_a1_range(sheet_name, start_cell),
                valueInputOption="RAW",
                body={"values": values}
            ).execute()
            print(f"[SHEETS] replace -> updatedRange={res.get('updatedRange')} updatedRows={res.get('updatedRows')}")
            fmt_types = getattr(df, "attrs", {}).get("format_types")
            if isinstance(fmt_types, dict):
                decimals_opt = 2
                try:
                    dec_attr = getattr(df, "attrs", {}).get("format_decimals")
                    if isinstance(dec_attr, int):
                        decimals_opt = dec_attr
                except Exception:
                    pass
                pattern_number = _pattern_from_decimals(decimals_opt)
                requests_fmt = []
                header_row = header_row_index or 1
                colnames = list(df.columns)
                for typ, cols in fmt_types.items():
                    if typ not in ("number", "text", "currency"):
                        continue
                    if not isinstance(cols, list):
                        continue
                    for col in cols:
                        idx = _resolve_col_to_index(col, colnames)
                        if idx is None:
                            print(f"[SHEETS] format_types: columna '{col}' no coincide con headers ni con letra A1")
                            continue
                        try:
                            resolved = colnames[idx] if 0 <= idx < len(colnames) else f"<idx {idx}>"
                            print(f"[SHEETS] format_types: '{col}' → col_index={idx} header='{resolved}'")
                        except Exception:
                            pass
                        fmt_symbol = getattr(df, "attrs", {}).get("format_currency_symbol") or "$"
                        fmt_pattern_override = getattr(df, "attrs", {}).get("format_currency_pattern")
                        if typ == "number":
                            number_format = {"type": "NUMBER", "pattern": pattern_number}
                        elif typ == "currency":
                            number_format = {"type": "CURRENCY", "pattern": fmt_pattern_override or f"{fmt_symbol}{pattern_number}"}
                        else:
                            number_format = {"type": "TEXT"}
                        req = {
                            "repeatCell": {
                                "range": {
                                    "sheetId": _ensure_sheet_id(),
                                    "startRowIndex": max((header_row or 1), start_row_min) - 1,
                                    "startColumnIndex": idx,
                                    "endColumnIndex": idx + 1,
                                },
                                "cell": {"userEnteredFormat": {"numberFormat": number_format}},
                                "fields": "userEnteredFormat.numberFormat"
                            }
                        }
                        requests_fmt.append(req)
                if requests_fmt:
                    service.spreadsheets().batchUpdate(
                        spreadsheetId=sheet_id,
                        body={"requests": requests_fmt}
                    ).execute()
                    print(f"[SHEETS] formats applied")
        finally:
            _write_timestamp(service, sheet_id, sheet_name, tz_opt)
        return

    # ---------------------------
    # MODO APPEND
    # ---------------------------
    print(f"[SHEETS] write_mode=append sheet_id={sheet_id} sheet_name={sheet_name} start_cell={start_cell}")
    try:
        read = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=_a1_range(sheet_name, "A:AZ")
        ).execute()
        rows = read.get("values", [])
        last_row_any = _last_non_empty_row(rows)
        wanted_header_raw = [str(c) for c in df.columns.tolist()]
        wanted_header_norm = _norm_header(wanted_header_raw)
        header_exists = False
        header_row_found = 0
        try:
            max_scan = 200
            scan_rows = rows[:max_scan]
            for idx, r in enumerate(scan_rows, start=1):
                row_slice = [str(x) for x in (r[:len(wanted_header_raw)])]
                if _norm_header(row_slice) == wanted_header_norm:
                    header_exists = True
                    header_row_found = idx
                    break
        except Exception:
            header_exists = False
            header_row_found = 0
        header_protect_row = header_row_found if header_row_found else header_row_index
        min_data_row = max(start_row_min, (header_protect_row + 1 if header_protect_row else start_row_min))
        next_after_last = (last_row_any + 1) if last_row_any else min_data_row
        start_row_write = max(next_after_last, min_data_row)
        n_rows = df.shape[0]
        n_cols = df.shape[1]
        _ensure_capacity(start_row_write, n_rows, start_col_idx, n_cols)
        values = []
        wrote_header = False
        if (not header_exists) and append_header:
            values.append(df.columns.tolist())
            wrote_header = True
        values += _df_to_values(df)
        start_cell_effective = f"{start_col_letters}{start_row_write}"
        res = service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=_a1_range(sheet_name, start_cell_effective),
            valueInputOption="RAW",
            body={"values": values}
        ).execute()
        print(f"[SHEETS] append -> updatedRange={res.get('updatedRange')} updatedRows={res.get('updatedRows')}")
        fmt_types = getattr(df, "attrs", {}).get("format_types")
        if isinstance(fmt_types, dict):
            decimals_opt = 2
            try:
                dec_attr = getattr(df, "attrs", {}).get("format_decimals")
                if isinstance(dec_attr, int):
                    decimals_opt = dec_attr
            except Exception:
                pass
            pattern_number = _pattern_from_decimals(decimals_opt)
            requests_fmt = []
            if wrote_header:
                header_row = start_row_write
            elif header_row_found:
                header_row = header_row_found
            elif header_row_index:
                header_row = header_row_index
            else:
                header_row = 1
            colnames = list(df.columns)
            for typ, cols in fmt_types.items():
                if typ not in ("number", "text", "currency"):
                    continue
                if not isinstance(cols, list):
                    continue
                for col in cols:
                    idx = _resolve_col_to_index(col, colnames)
                    if idx is None:
                        print(f"[SHEETS] format_types: columna '{col}' no coincide con headers ni con letra A1")
                        continue
                    try:
                        resolved = colnames[idx] if 0 <= idx < len(colnames) else f"<idx {idx}>"
                        print(f"[SHEETS] format_types: '{col}' → col_index={idx} header='{resolved}'")
                    except Exception:
                        pass
                    fmt_symbol = getattr(df, "attrs", {}).get("format_currency_symbol") or "$"
                    fmt_pattern_override = getattr(df, "attrs", {}).get("format_currency_pattern")
                    if typ == "number":
                        number_format = {
                            "type": "NUMBER",
                            "pattern": pattern_number
                        }
                    elif typ == "currency":
                        number_format = {
                            "type": "CURRENCY",
                            "pattern": fmt_pattern_override or f"{fmt_symbol}{pattern_number}"
                        }
                    else:
                        number_format = {
                            "type": "TEXT"
                        }
                    req = {
                        "repeatCell": {
                            "range": {
                                "sheetId": _ensure_sheet_id(),
                                "startRowIndex": max((header_row or 1), start_row_min) - 1,
                                "startColumnIndex": idx,
                                "endColumnIndex": idx + 1,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "numberFormat": number_format
                                }
                            },
                            "fields": "userEnteredFormat.numberFormat"
                        }
                    }
                    requests_fmt.append(req)
            if requests_fmt:
                service.spreadsheets().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"requests": requests_fmt}
                ).execute()
                print(f"[SHEETS] formats applied")
    finally:
        _write_timestamp(service, sheet_id, sheet_name, tz_opt)
    return
