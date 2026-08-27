import io
import unittest

from openpyxl import Workbook

import caddis_combined_job as job
from caddis_combined_job import (
    ReportDownloadValidationError,
    _download_with_retry,
    _validate_downloaded_report,
)


class FakeResponse:
    def __init__(self, content: bytes, *, status_code: int = 200, content_type: str = ""):
        self.content = content
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}


def build_xlsx(headers, rows=(), *, title_rows=0) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    for index in range(title_rows):
        sheet.append([f"Titulo {index + 1}"])
    sheet.append(list(headers))
    for row in rows:
        sheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def pdv_config():
    return {
        "name": "pdv_costo",
        "code": "317",
        "validation": {
            "header_scan_rows": 20,
            "allowed_formats": ["excel_xlsx"],
            "accepted_column_sets": [
                ["Factura Tipo", "Factura Numero", "Articulo Codigo"]
            ],
        },
    }


class DownloadResilienceTest(unittest.TestCase):
    def test_common_browser_flow_runs_all_session_steps(self):
        class FakeLegacy:
            BASE = "https://caddis.test"
            PRE_INDEX_MAIN = []

            def __init__(self):
                self.calls = []

            def login(self):
                self.calls.append(("login", ""))

            def STEP(self, method, url, **_kwargs):
                self.calls.append((method, url))

            def STEP_RAW(self, method, url, **_kwargs):
                self.calls.append((f"{method}_RAW", url))

        fake = FakeLegacy()
        previous = job.legacy
        job.legacy = fake
        try:
            job._common_browser_flow(
                {
                    "preflights_index_main": [],
                    "base_select": "179",
                }
            )
        finally:
            job.legacy = previous

        self.assertEqual(sum(call[0] == "login" for call in fake.calls), 1)
        self.assertEqual(sum(call[0] == "GET" for call in fake.calls), 4)
        self.assertEqual(sum(call[0] == "POST" for call in fake.calls), 2)
        self.assertTrue(fake.calls[-1][1].endswith("/gestion/herramientas/class.ajax.php"))

    def test_shifted_header_is_detected_by_signature(self):
        content = build_xlsx(
            ["Fecha", " factura   tipo ", "FACTURA NUMERO", "articulo codigo"],
            [["2026-08-27", "EA", "0001", "ART-1"]],
            title_rows=5,
        )
        result = _validate_downloaded_report(
            pdv_config(),
            FakeResponse(
                content,
                content_type=(
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                ),
            ),
            policy={"min_bytes": 1},
        )
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["header_row"], 6)
        self.assertIn("Factura Numero", result["columns"])

    def test_valid_empty_report_keeps_headers(self):
        content = build_xlsx(
            ["Fecha", "Factura Tipo", "Factura Numero", "Articulo Codigo"],
            title_rows=4,
        )
        result = _validate_downloaded_report(
            pdv_config(),
            FakeResponse(content),
            policy={"min_bytes": 1},
        )
        self.assertEqual(result["rows"], 0)
        self.assertEqual(result["header_row"], 5)

    def test_unknown_schema_is_rejected_with_columns(self):
        content = build_xlsx(["Columna A", "Columna B"], [[1, 2]])
        with self.assertRaises(ReportDownloadValidationError) as raised:
            _validate_downloaded_report(
                pdv_config(),
                FakeResponse(content),
                policy={"min_bytes": 1},
            )
        self.assertEqual(raised.exception.kind, "schema")
        self.assertIn("columns", raised.exception.diagnostics)

    def test_html_is_archived_then_second_attempt_succeeds(self):
        html = FakeResponse(
            b"<!doctype html><html><form action='index_login.php'></form></html>",
            content_type="text/html",
        )
        valid = FakeResponse(
            build_xlsx(
                ["Fecha", "Factura Tipo", "Factura Numero", "Articulo Codigo"],
                [["2026-08-27", "EA", "0001", "ART-1"]],
            )
        )
        responses = [html, valid]
        archived_attempts = []
        refreshed = []

        response, validation = _download_with_retry(
            report_cfg=pdv_config(),
            policy={
                "max_attempts": 2,
                "retry_delays_seconds": [0],
                "min_bytes": 1,
            },
            attempt_fn=lambda attempt: responses[attempt - 1],
            archive_fn=lambda response, attempt, diagnostics: (
                archived_attempts.append(
                    (attempt, diagnostics["format_hint"], diagnostics["bytes"])
                )
                or f"gs://bucket/attempt-{attempt}"
            ),
            refresh_session_fn=lambda: refreshed.append(True),
            sleep_fn=lambda _delay: None,
        )

        self.assertIs(response, valid)
        self.assertEqual(validation["attempt"], 2)
        self.assertEqual([item[0] for item in archived_attempts], [1, 2])
        self.assertEqual(archived_attempts[0][1], "html")
        self.assertEqual(len(refreshed), 1)


if __name__ == "__main__":
    unittest.main()
