import json
from pathlib import Path
import unittest

import pandas as pd

from combined_sales_pipeline import align_sheet_columns


class SheetColumnOrderTest(unittest.TestCase):
    def setUp(self):
        config = json.loads((Path(__file__).resolve().parents[1] / 'vars.yml').read_text())
        self.columns = config['combined_output']['options']['google_sheets']['column_order']['combined']

    def test_original_positions(self):
        self.assertEqual(len(self.columns), 84)
        for index, name in {3: 'POS', 4: 'Vendedor', 8: 'Articulo Codigo',
                            29: 'Total', 49: 'Costo Unitario', 50: 'Costo Total',
                            54: 'Clave Cruce', 55: 'Indice Coincidencia',
                            83: 'Estado Cruce'}.items():
            self.assertEqual(self.columns[index], name)

    def test_aligns_by_name_and_preserves_raw_frame(self):
        values = {name: f'value:{name}' for name in reversed(self.columns)}
        values.update({'Zona': 'new-zone', 'Hora': '12:30'})
        source = pd.DataFrame([values])
        original = source.copy(deep=True)
        result = align_sheet_columns(source, self.columns)
        self.assertEqual(result.columns.tolist(), self.columns)
        self.assertEqual(result.iloc[0].tolist(), [f'value:{name}' for name in self.columns])
        pd.testing.assert_frame_equal(source, original)

    def test_missing_column_fails(self):
        source = pd.DataFrame(columns=self.columns[:-1])
        with self.assertRaisesRegex(ValueError, 'Estado Cruce'):
            align_sheet_columns(source, self.columns)

    def test_duplicate_columns_fail(self):
        with self.assertRaisesRegex(ValueError, 'duplicados'):
            align_sheet_columns(pd.DataFrame(), ['Fecha', 'Fecha'])
        with self.assertRaisesRegex(ValueError, 'duplicados'):
            align_sheet_columns(pd.DataFrame(columns=['Fecha', 'Fecha']), ['Fecha'])
