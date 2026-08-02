import unittest

import app


class AutoCaptionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.car = {
            "model": "Volkswagen Golf",
            "configuration": "2023 280TSI DSG R-Line",
            "production_year": 2023,
            "production_month": 5,
            "mileage_km": 18000,
            "body_color": "Черный",
            "interior_color": "Черный",
            "paint_condition": "Оригинальный цвет",
            "horsepower": 150,
            "engine_display": "бензиновый турбированный, 1,4 л",
        }

    def test_extracts_and_normalizes_transmission_from_listing(self) -> None:
        details = app.extract_page_details(
            "<html><body><div>Коробка передач: 7-ступенчатая DSG</div></body></html>"
        )
        self.assertEqual(
            details["transmission"],
            "7-ступенчатая роботизированная DSG",
        )

    def test_normalizes_supported_transmission_types(self) -> None:
        cases = {
            "6挡手自一体": "6-ступенчатая автоматическая (AT)",
            "CVT无级变速(模拟8挡)": "вариатор (CVT)",
            "7-speed S tronic": "7-ступенчатая роботизированная S tronic (DCT)",
            "6-ступенчатая механическая MT": "6-ступенчатая механическая (MT)",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(app.normalize_transmission(source), expected)

    def test_caption_always_contains_confirmed_transmission(self) -> None:
        caption = app.build_auto_caption(
            self.car,
            1_950_000,
            {"transmission": "7-ступенчатая роботизированная DSG"},
        )
        self.assertIn(
            "▫️ Коробка передач: 7-ступенчатая роботизированная DSG",
            caption,
        )

    def test_caption_uses_verified_queue_value_when_listing_omits_it(self) -> None:
        self.car["transmission"] = "7-ступенчатая роботизированная DSG"
        caption = app.build_auto_caption(self.car, 1_950_000, {})
        self.assertIn(
            "▫️ Коробка передач: 7-ступенчатая роботизированная DSG",
            caption,
        )

    def test_caption_is_blocked_when_transmission_is_missing(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "коробку передач"):
            app.build_auto_caption(self.car, 1_950_000, {})


if __name__ == "__main__":
    unittest.main()
