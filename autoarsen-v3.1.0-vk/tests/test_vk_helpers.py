import unittest

from app import vk_message_from_html, vk_random_id


class VKHelpersTest(unittest.TestCase):
    def test_vk_message_removes_telegram_html(self) -> None:
        caption = "🚘 <b>AUTO ARSEN</b>\nЦена: 1&nbsp;500&nbsp;000 ₽"
        self.assertEqual(
            vk_message_from_html(caption),
            "🚘 AUTO ARSEN\nЦена: 1\u00a0500\u00a0000 ₽",
        )

    def test_vk_random_id_is_stable_and_positive(self) -> None:
        first = vk_random_id("2026-07-28T12:00:00+05:00:42")
        second = vk_random_id("2026-07-28T12:00:00+05:00:42")
        other = vk_random_id("2026-07-28T16:00:00+05:00:42")

        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertGreaterEqual(first, 0)
        self.assertLessEqual(first, 0x7FFFFFFF)


if __name__ == "__main__":
    unittest.main()
