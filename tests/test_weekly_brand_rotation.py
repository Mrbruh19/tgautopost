import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import app


def sample_car(index: int, model: str) -> dict:
    return {
        "source_row": index,
        "page_url": f"https://example.com/car/{index}",
        "stock_number": f"S{index:03d}",
        "model": model,
        "configuration": "2024 1.5L Premium",
        "production_year": 2024,
        "production_month": 6,
        "mileage_km": 10_000,
        "body_color": "Черный",
        "interior_color": "Черный",
        "paint_condition": "Оригинальный цвет",
        "horsepower": 150,
        "price_cny": 80_000,
        "engine_cc": 1498,
        "engine_display": "бензиновый, 1,5 л",
        "status": "Готово",
        "transmission": "вариатор (CVT)",
    }


class WeeklyBrandRotationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db = app.DB_PATH
        self.original_seed = app.SEED_PATH
        self.original_content_seed = app.CONTENT_SEED_PATH
        app.DB_PATH = Path(self.temp_dir.name) / "queue.db"
        app.SEED_PATH = Path(self.temp_dir.name) / "queue.json"
        app.CONTENT_SEED_PATH = Path(self.temp_dir.name) / "missing-content.json"
        cars = [
            sample_car(1, "Toyota Corolla"),
            sample_car(2, "Toyota Yaris"),
            sample_car(3, "Honda Vezel"),
            sample_car(4, "Honda Civic"),
        ]
        app.SEED_PATH.write_text(
            json.dumps({"cars": cars, "replace_active_queue": True}),
            encoding="utf-8",
        )
        app.initialize_queue_database()

    def tearDown(self) -> None:
        app.DB_PATH = self.original_db
        app.SEED_PATH = self.original_seed
        app.CONTENT_SEED_PATH = self.original_content_seed
        self.temp_dir.cleanup()

    def test_daily_slots_do_not_repeat_brand_in_same_week(self) -> None:
        timezone = ZoneInfo("Asia/Yekaterinburg")
        first_slot = datetime(2026, 8, 17, 12, 0, tzinfo=timezone)
        first = app.choose_car_for_slot(first_slot)
        self.assertIsNotNone(first)
        app.mark_slot_success(first_slot, first["id"], [101])

        second_slot = datetime(2026, 8, 17, 16, 0, tzinfo=timezone)
        second = app.choose_car_for_slot(second_slot)
        self.assertIsNotNone(second)
        self.assertNotEqual(
            app.normalize_car_brand(first["model"]),
            app.normalize_car_brand(second["model"]),
        )

    def test_brand_can_be_used_again_next_monday(self) -> None:
        timezone = ZoneInfo("Asia/Yekaterinburg")
        first_slot = datetime(2026, 8, 17, 12, 0, tzinfo=timezone)
        first = app.choose_car_for_slot(first_slot)
        self.assertIsNotNone(first)
        app.mark_slot_success(first_slot, first["id"], [101])

        selected_brand = app.normalize_car_brand(first["model"])
        with app.db_connect() as connection:
            rows = connection.execute("SELECT id, model FROM cars").fetchall()
            for row in rows:
                if app.normalize_car_brand(row["model"]) != selected_brand:
                    connection.execute(
                        "UPDATE cars SET status='published' WHERE id=?",
                        (row["id"],),
                    )
        next_week = datetime(2026, 8, 24, 12, 0, tzinfo=timezone)
        selected = app.choose_car_for_slot(next_week)
        self.assertIsNotNone(selected)
        self.assertEqual(
            selected_brand,
            app.normalize_car_brand(selected["model"]),
        )

    def test_brand_aliases_are_normalized(self) -> None:
        self.assertEqual(app.normalize_car_brand("ŠKODA Karoq"), "skoda")
        self.assertEqual(app.normalize_car_brand("Skoda Octavia"), "skoda")
        self.assertEqual(app.normalize_car_brand("Мазда CX-4"), "mazda")


if __name__ == "__main__":
    unittest.main()
