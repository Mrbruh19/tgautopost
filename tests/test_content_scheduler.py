import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import app


class ContentSchedulerTest(unittest.TestCase):
    def test_content_schedule_uses_selected_weekdays_at_ten(self) -> None:
        original_weekdays = app.CONTENT_PUBLISH_WEEKDAYS
        original_time = app.CONTENT_PUBLISH_TIME
        try:
            app.CONTENT_PUBLISH_WEEKDAYS = (0, 3, 5)
            app.CONTENT_PUBLISH_TIME = "10:00"

            monday = app.scheduled_content_slot_for_day(date(2026, 8, 3))
            tuesday = app.scheduled_content_slot_for_day(date(2026, 8, 4))
            thursday = app.scheduled_content_slot_for_day(date(2026, 8, 6))
            saturday = app.scheduled_content_slot_for_day(date(2026, 8, 8))

            self.assertIsNotNone(monday)
            self.assertEqual((monday.hour, monday.minute), (10, 0))
            self.assertEqual(monday.tzinfo, ZoneInfo("Asia/Yekaterinburg"))
            self.assertIsNone(tuesday)
            self.assertIsNotNone(thursday)
            self.assertIsNotNone(saturday)
        finally:
            app.CONTENT_PUBLISH_WEEKDAYS = original_weekdays
            app.CONTENT_PUBLISH_TIME = original_time

    def test_seed_starts_empty_until_user_approves_a_post(self) -> None:
        project_root = Path(app.__file__).parent
        seed = json.loads(
            (project_root / "content_seed.json").read_text(encoding="utf-8")
        )
        self.assertEqual(seed, [])

    def test_generated_cover_has_required_7_by_5_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "cover.jpg"
            app.generate_content_cover(
                "Почему одинаковые автомобили стоят по-разному",
                destination,
            )
            with app.Image.open(destination) as image:
                self.assertEqual(image.size, (1400, 1000))
                self.assertEqual(image.format, "JPEG")

    def test_queue_selects_first_post_and_does_not_duplicate_slot(self) -> None:
        original_db_path = app.DB_PATH
        original_seed_path = app.SEED_PATH
        original_content_seed_path = app.CONTENT_SEED_PATH

        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                app.DB_PATH = Path(temp_dir) / "queue.db"
                app.SEED_PATH = Path(temp_dir) / "missing-cars.json"
                app.CONTENT_SEED_PATH = Path(temp_dir) / "approved-content.json"
                app.CONTENT_SEED_PATH.write_text(
                    json.dumps(
                        [
                            {
                                "id": "approved_post",
                                "sequence": 1,
                                "title": "Одобренный пост",
                                "body": "Текст одобренного поста. {contact}",
                                "image_file": "",
                            }
                        ],
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                app.initialize_queue_database()

                self.assertEqual(app.content_queue_counts()["pending"], 1)
                slot = datetime(
                    2026,
                    8,
                    3,
                    10,
                    0,
                    tzinfo=ZoneInfo("Asia/Yekaterinburg"),
                )
                first = app.choose_content_for_slot(slot)
                self.assertIsNotNone(first)
                self.assertEqual(first["id"], "approved_post")

                app.mark_content_success(slot, first["id"], 12345)
                self.assertIsNone(app.choose_content_for_slot(slot))
                self.assertEqual(app.content_queue_counts()["published"], 1)
            finally:
                app.DB_PATH = original_db_path
                app.SEED_PATH = original_seed_path
                app.CONTENT_SEED_PATH = original_content_seed_path


if __name__ == "__main__":
    unittest.main()
