import unittest

import public_share_bot as bot


class ConfigurationTests(unittest.TestCase):
    def test_parse_multiple_admin_ids_and_remove_duplicates(self):
        self.assertEqual(bot.parse_admin_ids("123, 456,123"), {123, 456})

    def test_reject_invalid_admin_id(self):
        with self.assertRaises(ValueError):
            bot.parse_admin_ids("123,username")


class AdvertisementTests(unittest.TestCase):
    def test_ad_url_allowlist(self):
        self.assertTrue(bot.is_valid_ad_url("https://t.me/example"))
        self.assertTrue(bot.is_valid_ad_url("tg://resolve?domain=example"))
        self.assertFalse(bot.is_valid_ad_url("javascript:alert(1)"))
        self.assertFalse(bot.is_valid_ad_url("example.com"))

    def test_ad_html_escapes_admin_content(self):
        rendered = bot.format_ad_html(("<b>not markup</b>", "点&开", "https://example.com?a=1&b=2"))
        self.assertIn("&lt;b&gt;not markup&lt;/b&gt;", rendered)
        self.assertIn("点&amp;开", rendered)
        self.assertIn("a=1&amp;b=2", rendered)

    def test_empty_ad_renders_nothing(self):
        self.assertEqual(bot.format_ad_html(None), "")


class PaginationTests(unittest.TestCase):
    def test_share_pagination_keeps_share_id(self):
        keyboard = bot.create_pagination_keyboard(2, 3, "spage", "share_123")
        callback_values = [button.callback_data for row in keyboard for button in row if button.callback_data]
        self.assertIn("spage:1:share_123", callback_values)
        self.assertIn("spage:3:share_123", callback_values)


if __name__ == "__main__":
    unittest.main()
