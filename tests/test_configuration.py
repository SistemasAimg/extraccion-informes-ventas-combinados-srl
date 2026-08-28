import json
import unittest
from pathlib import Path


class ConfigurationTest(unittest.TestCase):
    def test_both_reports_use_yesterday_as_start_and_end(self):
        config_path = Path(__file__).resolve().parents[1] / "vars.yml"
        config = json.loads(config_path.read_text(encoding="utf-8"))

        for report in config["reports"]:
            with self.subTest(report=report["name"]):
                self.assertEqual(
                    report["armar_filtro"]["args"][5],
                    "{yesterday:%Y-%m-%d}",
                )
                self.assertEqual(
                    report["armar_filtro"]["args"][6],
                    "{yesterday:%Y-%m-%d}",
                )
                self.assertEqual(
                    report["download"]["data"]["field_desde"],
                    "{yesterday:%d/%m/%Y}",
                )
                self.assertEqual(
                    report["download"]["data"]["field_hasta"],
                    "{yesterday:%d/%m/%Y}",
                )
                self.assertEqual(
                    report["download"]["data"]["SubTitulo"],
                    "Del {yesterday:%d/%m/%Y} al {yesterday:%d/%m/%Y}",
                )

    def test_formas_pago_uses_base_report_305_and_detailed_view_331(self):
        config_path = Path(__file__).resolve().parents[1] / "vars.yml"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        report = next(item for item in config["reports"] if item["name"] == "formas_pago")

        self.assertEqual(report["code"], "305")
        self.assertEqual(report["armar_filtro"]["args"][0], "305")
        self.assertEqual(report["armar_filtro"]["args"][1], "331")
        self.assertEqual(report["armar_filtro"]["args"][5], "{yesterday:%Y-%m-%d}")
        self.assertEqual(report["armar_filtro"]["args"][6], "{yesterday:%Y-%m-%d}")
        self.assertEqual(report["download"]["data"]["field_vista"], "331")
        self.assertEqual(report["download"]["data"]["Informe"], "331")
        self.assertEqual(
            report["download"]["data"]["field_desde"],
            "{yesterday:%d/%m/%Y}",
        )
        self.assertEqual(
            report["download"]["data"]["field_hasta"],
            "{yesterday:%d/%m/%Y}",
        )


if __name__ == "__main__":
    unittest.main()
