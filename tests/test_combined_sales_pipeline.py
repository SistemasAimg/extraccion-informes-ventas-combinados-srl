import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from combined_sales_pipeline import (
    STATUS_COLUMN,
    classify_payment_schema,
    combine_sales_dataframes,
    filter_new_rows_for_append,
    update_history_dataframe,
)


class CombinedSalesPipelineTest(unittest.TestCase):
    def setUp(self):
        self.pdv = pd.DataFrame(
            [
                {
                    "Fecha": "2026-08-26",
                    "Factura Tipo": "EA",
                    "Factura Numero": "0013-0001",
                    "Articulo Codigo": "010-AAA",
                    "Cantidad": 1,
                    "Total": 100,
                    "Costo Total": 60,
                },
                {
                    "Fecha": "2026-08-26",
                    "Factura Tipo": "X",
                    "Factura Numero": "9999-0002",
                    "Articulo Codigo": "MKT-001",
                    "Cantidad": 1,
                    "Total": 0,
                    "Costo Total": 10,
                },
            ]
        )
        self.payment_detail = pd.DataFrame(
            [
                {
                    "Fecha": "2026-08-26",
                    "Fc Tipo": "EA",
                    "Fc_Nro": "0013-0001",
                    "Parte": "010-AAA",
                    "Pago Tipo": "T.CREDITO",
                    "Credito": 100,
                }
            ]
        )
        self.payment_summary = pd.DataFrame(
            [
                {
                    "POS": "1-UDAONDO",
                    "TPago": "T.CREDITO",
                    "Cantidad": 1,
                    "Importe": 100,
                    "Total": 100,
                }
            ]
        )

    def test_detailed_payment_is_joined_without_losing_invoice_x(self):
        result = combine_sales_dataframes(
            self.pdv,
            self.payment_detail,
            extracted_at=datetime(2026, 8, 27, 0, 30),
            window_start=date(2026, 8, 26),
            window_end=date(2026, 8, 27),
        )
        combined = result["combined"]
        self.assertEqual(result["payment_schema"], "detalle")
        self.assertEqual(len(combined), 2)
        self.assertEqual((combined[STATUS_COLUMN] == "RELACIONADO").sum(), 1)
        self.assertEqual((combined[STATUS_COLUMN] == "SOLO_PDV").sum(), 1)
        self.assertIn("Pago Pago Tipo", combined.columns)

    def test_summary_is_kept_but_not_force_joined(self):
        result = combine_sales_dataframes(self.pdv, self.payment_summary)
        self.assertEqual(classify_payment_schema(self.payment_summary), "resumen")
        self.assertEqual(result["payment_schema"], "resumen")
        self.assertEqual(len(result["combined"]), len(self.pdv))
        self.assertTrue(
            (result["combined"][STATUS_COLUMN] == "FORMA_PAGO_RESUMIDA_NO_RELACIONABLE").all()
        )

    def test_history_replaces_overlapping_date_partition(self):
        old = self.pdv.iloc[[0]].copy()
        old["Total"] = 90
        old_result = combine_sales_dataframes(old, self.payment_detail)["combined"]
        new_result = combine_sales_dataframes(self.pdv.iloc[[0]], self.payment_detail)["combined"]
        history = update_history_dataframe(
            old_result,
            new_result,
            window_start=date(2026, 8, 26),
            window_end=date(2026, 8, 27),
        )
        self.assertEqual(len(history), 1)
        self.assertEqual(float(history.iloc[0]["Total"]), 100.0)

    def test_empty_reports_with_valid_headers_are_accepted(self):
        empty_pdv = self.pdv.iloc[0:0].copy()
        empty_payments = self.payment_detail.iloc[0:0].copy()
        result = combine_sales_dataframes(empty_pdv, empty_payments)
        self.assertEqual(result["payment_schema"], "detalle")
        self.assertTrue(result["combined"].empty)
        self.assertEqual(int(result["control"].iloc[0]["Filas PDV"]), 0)

    def test_sheet_append_filters_existing_keys_without_repeating_headers(self):
        current = combine_sales_dataframes(self.pdv, self.payment_detail)["combined"]
        first_key = (
            str(current.iloc[0]["Clave Cruce"]),
            str(current.iloc[0]["Indice Coincidencia"]),
        )
        filtered = filter_new_rows_for_append(
            current,
            existing_keys={first_key},
            key_columns=["Clave Cruce", "Indice Coincidencia"],
        )

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered.iloc[0]["Factura Tipo"], "X")

    def test_sheet_append_deduplicates_repeated_rows_in_same_batch(self):
        current = combine_sales_dataframes(self.pdv, self.payment_detail)["combined"]
        repeated = pd.concat([current.iloc[[0]], current.iloc[[0]]], ignore_index=True)
        filtered = filter_new_rows_for_append(
            repeated,
            existing_keys=set(),
            key_columns=["Clave Cruce", "Indice Coincidencia"],
        )

        self.assertEqual(len(filtered), 1)


if __name__ == "__main__":
    unittest.main()
