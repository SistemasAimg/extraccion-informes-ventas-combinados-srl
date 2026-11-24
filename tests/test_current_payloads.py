import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import caddis_combined_job as job

class CurrentPayloadTests(unittest.TestCase):
    def test_sales_uses_303_and_requires_costs(self):
        cfg=json.loads((Path(__file__).resolve().parents[1]/'vars.yml').read_text())
        sales,payments=cfg['reports']
        self.assertEqual(sales['code'],'303')
        self.assertEqual(sales['armar_filtro']['sfunc'],'armar_filtroInformes')
        self.assertEqual(sales['armar_filtro']['args'][:2],['303','303'])
        self.assertEqual(len(sales['armar_filtro']['args']),67)
        self.assertEqual(sales['download']['data']['field_vista'],'303')
        self.assertEqual(sales['download']['data']['Informe'],'303')
        self.assertEqual(sales['download']['data']['field_cliente'],'NULL')
        required=sales['validation']['accepted_column_sets'][0]
        self.assertTrue({'Costo Unitario','Costo Total'}.issubset(required))
        self.assertEqual(payments['armar_filtro']['sfunc'],'armar_filtroInformesVentas')

    def test_marker_is_fresh_and_replaces_stale_marker(self):
        fake=SimpleNamespace(replace_placeholders=lambda value,ctx:value,
                             _render_str_with_ctx=lambda value,ctx:value)
        for config in [{'data':{'Informe':'303','Filtro':'','marca_descarga':'stale'}},
                       {'raw':'Informe=331&Filtro=&marca_descarga=stale'}]:
            with self.subTest(config=config),patch.object(job,'legacy',fake),patch.object(job.time,'time_ns',side_effect=[1000000,2000000]):
                first=job._download_form(config,{},'303')
                second=job._download_form(config,{},'303')
                self.assertEqual(dict(first)['marca_descarga'],'d1')
                self.assertEqual(dict(second)['marca_descarga'],'d2')
                self.assertEqual(dict(first)['Filtro'],'')
                self.assertEqual(sum(k=='marca_descarga' for k,v in first),1)

    def test_ajax_rejects_html_and_unsuccessful_response(self):
        for body,status in [(b'<html>login</html>',200),(b'[0,"error"]',200),(b'{}',200),(b'[1]',500)]:
            with self.subTest(body=body),self.assertRaises(RuntimeError):
                job._validate_filter_response(SimpleNamespace(content=body,status_code=status))
        job._validate_filter_response(SimpleNamespace(content=b'[1,"ok"]',status_code=200))
