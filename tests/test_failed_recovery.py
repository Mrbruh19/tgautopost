import json
import tempfile
import unittest
from pathlib import Path

import app


class FailedPublicationRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db = app.DB_PATH
        self.original_seed = app.SEED_PATH
        self.original_content_seed = app.CONTENT_SEED_PATH
        app.DB_PATH = Path(self.temp_dir.name) / "queue.db"
        app.SEED_PATH = Path(self.temp_dir.name) / "queue.json"
        app.CONTENT_SEED_PATH = Path(self.temp_dir.name) / "content.json"
        app.SEED_PATH.write_text(
            json.dumps(
                {
                    "replace_active_queue": True,
                    "cars": [
                        {
                            "page_url": "https://example.com/active",
                            "stock_number": "ACTIVE-1",
                            "model": "Audi A3",
                            "configuration": "2024 Premium",
                            "production_year": 2024,
                            "production_month": 6,
                            "mileage_km": 10_000,
                            "horsepower": 150,
                            "price_cny": 80_000,
                            "engine_cc": 1498,
                            "engine_display": "бензиновый, 1,5 л",
                            "transmission": "7-DCT",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        app.CONTENT_SEED_PATH.write_text(
            json.dumps(
                [
                    {
                        "id": "failed-content",
                        "sequence": 1,
                        "title": "Полезный пост",
                        "body": "Текст {contact}",
                        "image_file": "",
                    }
                ]
            ),
            encoding="utf-8",
        )
        app.initialize_queue_database()

    def tearDown(self) -> None:
        app.DB_PATH = self.original_db
        app.SEED_PATH = self.original_seed
        app.CONTENT_SEED_PATH = self.original_content_seed
        self.temp_dir.cleanup()

    def test_active_failed_items_are_recovered_once(self) -> None:
        with app.db_connect() as connection:
            connection.execute(
                "UPDATE cars SET status='failed', attempts=3, last_error='temporary'"
            )
            connection.execute(
                "UPDATE content_posts SET status='failed', attempts=3, last_error='temporary'"
            )
            connection.execute(
                "DELETE FROM maintenance_state WHERE key='resume_failed_publications_20260906'"
            )

        app.initialize_queue_database()

        with app.db_connect() as connection:
            car = connection.execute(
                "SELECT status, attempts, last_error FROM cars"
            ).fetchone()
            content = connection.execute(
                "SELECT status, attempts, last_error FROM content_posts"
            ).fetchone()
        self.assertEqual(
            (car["status"], car["attempts"], car["last_error"]),
            ("pending", 0, None),
        )
        self.assertEqual(
            (content["status"], content["attempts"], content["last_error"]),
            ("pending", 0, None),
        )

        with app.db_connect() as connection:
            connection.execute(
                "UPDATE cars SET status='failed', attempts=3, last_error='permanent'"
            )
        app.initialize_queue_database()
        with app.db_connect() as connection:
            car = connection.execute("SELECT status FROM cars").fetchone()
        self.assertEqual(car["status"], "failed")


if __name__ == "__main__":
    unittest.main()
