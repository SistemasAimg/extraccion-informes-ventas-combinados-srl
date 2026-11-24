"""Regression checks for append writes, without accessing Google Sheets."""
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from report_pipeline import upload_to_sheet


class SheetHeadersTest(unittest.TestCase):
    def upload(self, rows):
        service = MagicMock()
        api = service.spreadsheets.return_value
        values = api.values.return_value
        values.get.return_value.execute.return_value = {"values": rows}
        api.get.return_value.execute.return_value = {"sheets": [{"properties": {
            "title": "Ventas combinadas", "sheetId": 0,
            "gridProperties": {"rowCount": 1000, "columnCount": 26},
        }}]}
        discovery = types.ModuleType("googleapiclient.discovery")
        discovery.build = MagicMock(return_value=service)
        errors = types.ModuleType("googleapiclient.errors")
        errors.HttpError = Exception
        modules = {"googleapiclient": types.ModuleType("googleapiclient"),
                   "googleapiclient.discovery": discovery,
                   "googleapiclient.errors": errors}
        with patch.dict(sys.modules, modules):
            upload_to_sheet(pd.DataFrame([["2026-11-19", 100]], columns=["Fecha", "Total"]),
                            "test", "Ventas combinadas", None,
                            write_mode="append", append_header=True, start_cell="A3")
        writes = [call.kwargs for call in values.update.call_args_list
                  if not call.kwargs["range"].endswith("!A1")]
        self.assertEqual(len(writes), 1)
        return writes[0]

    def test_first_write_then_second_write(self):
        first = self.upload([["Última ejecución"], []])
        self.assertEqual(first["range"], "'Ventas combinadas'!A3")
        self.assertEqual(first["body"]["values"], [["Fecha", "Total"], ["2026-11-19", 100]])
        second = self.upload([["Última ejecución"], []] + first["body"]["values"])
        self.assertEqual(second["range"], "'Ventas combinadas'!A5")
        self.assertEqual(second["body"]["values"], [["2026-11-19", 100]])

    def test_existing_renamed_headers(self):
        result = self.upload([[], [], ["Fecha de venta", "Importe"], ["2026-11-18", 50]])
        self.assertEqual(result["body"]["values"], [["2026-11-19", 100]])

    def test_existing_data_without_matching_header_in_first_200_rows(self):
        result = self.upload([[]] * 201 + [["Fecha", "Total"], ["2026-11-18", 50]])
        self.assertEqual(result["range"], "'Ventas combinadas'!A204")
        self.assertEqual(result["body"]["values"], [["2026-11-19", 100]])

    def test_header_only_sheet(self):
        result = self.upload([[], [], ["Fecha", "Total"]])
        self.assertEqual(result["body"]["values"], [["2026-11-19", 100]])
