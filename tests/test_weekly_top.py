import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import app


def sample_car(index: int, model: str, *, crossover: bool = False) -> dict:
    return {
        "source_row": index,
        "page_url": f"https://example.com/car/{index}",
        "stock_number": f"S{index:03d}",
        "model": model,
        "configuration": "2024 Premium",
        "production_year": 2024,
        "production_month": index,
        "mileage_km": 10_000 + index * 1_000,
        "body_color": "Черный",
        "interior_color": "Черный",
        "paint_condition": "Оригинальный цвет",
        "horsepower": 150,
        "price_cny": 40_000 + index * 1_000,
        "engine_cc": 999,
        "engine_display": "бензиновый, 1,0 л",
        "status": "Готово",
        "transmission": "7-ступенчатая роботизированная DCT",
        "_crossover": crossover,
    }


class WeeklyTopTest(unittest.TestCase):
    def test_schedule_runs_monday_through_saturday_at_eleven(self) -> None:
        for day_number in range(3, 9):
            day = date(2026, 8, day_number)
            slot = app.scheduled_weekly_top_slot_for_day(day)
            self.assertIsNotNone(slot)
            self.assertEqual((slot.hour, slot.minute), (11, 0))
            self.assertEqual(slot.tzinfo, ZoneInfo("Asia/Yekaterinburg"))
        self.assertIsNone(app.scheduled_weekly_top_slot_for_day(date(2026, 8, 9)))

    def test_budget_top_selects_five_different_models(self) -> None:
        original_db = app.DB_PATH
        original_seed = app.SEED_PATH
        original_content_seed = app.CONTENT_SEED_PATH
        cars = [sample_car(i, f"Model {i}") for i in range(1, 11)]
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                app.DB_PATH = Path(temp_dir) / "queue.db"
                app.SEED_PATH = Path(temp_dir) / "queue.json"
                app.CONTENT_SEED_PATH = Path(temp_dir) / "missing-content.json"
                app.SEED_PATH.write_text(
                    json.dumps({"cars": cars, "replace_active_queue": True}),
                    encoding="utf-8",
                )
                app.initialize_queue_database()
                slot = datetime(2026, 8, 4, 11, 0, tzinfo=ZoneInfo("Asia/Yekaterinburg"))
                selected = app.select_weekly_top_cars(
                    slot, 1_500_000, False, 10.0, 100.0
                )
                self.assertEqual(len(selected), 5)
                self.assertEqual(len({car["model"] for car in selected}), 5)
                self.assertTrue(
                    all(car["rounded_total_rub"] <= 1_500_000 for car in selected)
                )
                app.ensure_weekly_top_slot_record(slot)
                app.mark_weekly_top_success(
                    slot,
                    123,
                    [str(car["page_url"]) for car in selected],
                )
                next_slot = datetime(
                    2026, 8, 5, 11, 0, tzinfo=ZoneInfo("Asia/Yekaterinburg")
                )
                next_selected = app.select_weekly_top_cars(
                    next_slot, 2_000_000, False, 10.0, 100.0
                )
                self.assertEqual(len(next_selected), 5)
                self.assertFalse(
                    {car["page_url"] for car in selected}
                    & {car["page_url"] for car in next_selected}
                )
            finally:
                app.DB_PATH = original_db
                app.SEED_PATH = original_seed
                app.CONTENT_SEED_PATH = original_content_seed

    def test_crossover_filter_and_caption(self) -> None:
        car = sample_car(1, "BMW X1", crossover=True)
        self.assertTrue(app.is_crossover(car))
        self.assertFalse(app.is_crossover(sample_car(2, "Volkswagen Golf")))
        cars = []
        for index in range(1, 6):
            item = sample_car(index, "BMW X1" if index == 1 else f"Model {index}")
            item["rounded_total_rub"] = 1_500_000 + index * 10_000
            cars.append(item)
        caption = app.build_weekly_top_caption("ТОП-5 КРОССОВЕРОВ", cars)
        self.assertIn("ТОП-5 КРОССОВЕРОВ", caption)
        self.assertIn("7-DCT", caption)
        self.assertIn("Доставка до города и ГЛОНАСС", caption)
        self.assertLessEqual(len(caption), 1024)


if __name__ == "__main__":
    unittest.main()
