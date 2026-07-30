import unittest

from app import (
    PHOTO_SOURCE_POSITIONS,
    extract_page_details,
    shorten_equipment,
)


class SourceDetailsTest(unittest.TestCase):
    def test_extracts_russian_labeled_values(self) -> None:
        source_html = """
        <html><body>
          <table>
            <tr><th>Привод</th><td>Полный AWD</td></tr>
            <tr><th>Оснащение</th>
                <td>Панорамная крыша; камеры 360°; кожаный салон;
                    подогрев сидений; бесключевой доступ; парктроники;
                    Apple CarPlay</td></tr>
            <tr><th>Состояние автомобиля</th><td>Без ДТП</td></tr>
          </table>
        </body></html>
        """

        details = extract_page_details(source_html)

        self.assertEqual(details["drive"], "полный")
        self.assertEqual(details["condition"], "без ДТП")
        self.assertEqual(len(details["equipment"].split(", ")), 6)
        self.assertIn("Панорамная крыша", details["equipment"])
        self.assertNotIn("Apple CarPlay", details["equipment"])

    def test_extracts_chinese_values_and_detects_equipment_keywords(self) -> None:
        source_html = """
        <html><body>
          <div>驱动方式：前驱</div>
          <div>车况：原版原漆</div>
          <p>车辆配有全景天窗、座椅加热、无钥匙进入和倒车影像。</p>
        </body></html>
        """

        details = extract_page_details(source_html)

        self.assertEqual(details["drive"], "передний")
        self.assertEqual(details["condition"], "оригинальный окрас")
        self.assertEqual(
            set(details["equipment"].split(", ")),
            {
                "панорамная крыша",
                "подогрев сидений",
                "бесключевой доступ",
                "камера заднего вида",
            },
        )
        self.assertNotIn("люк", details["equipment"])

    def test_equipment_is_empty_when_page_has_no_supported_data(self) -> None:
        self.assertEqual(
            shorten_equipment("", "Обычное описание автомобиля без списка опций."),
            "",
        )

    def test_required_gallery_positions(self) -> None:
        self.assertEqual(PHOTO_SOURCE_POSITIONS, (1, 2, 3, 4, 7, 8, 9, 10))


if __name__ == "__main__":
    unittest.main()
