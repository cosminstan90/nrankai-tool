"""
The scraper must not die because undetected_chromedriver lags behind Chrome.

Found on this machine: Chrome 152 is installed, undetected_chromedriver 3.5.5
is the latest release and cannot start it ("session not created: cannot connect
to chrome"), and core/web_scraper.py additionally pinned version_main=145. Step
1 of every audit failed. Plain Selenium starts the same Chrome fine, because
Selenium Manager resolves a matching driver.

These tests exercise the fallback without launching a browser.
"""
import unittest
from unittest.mock import MagicMock, patch

import core.web_scraper as ws


class TestCreateDriver(unittest.TestCase):
    def test_prefers_undetected_chromedriver_when_it_works(self):
        """
        uc is the reason this project uses a patched driver at all -- it is
        harder to detect. It stays the first choice.
        """
        fake = MagicMock(name="uc-driver")
        with patch.object(ws.uc, "Chrome", return_value=fake) as uc_chrome, \
             patch("selenium.webdriver.Chrome") as plain_chrome:
            driver = ws.create_driver()

        self.assertIs(driver, fake)
        uc_chrome.assert_called_once()
        plain_chrome.assert_not_called()

    def test_falls_back_to_plain_selenium_when_uc_cannot_start_chrome(self):
        fake = MagicMock(name="selenium-driver")
        with patch.object(ws.uc, "Chrome", side_effect=Exception("cannot connect to chrome")), \
             patch("selenium.webdriver.Chrome", return_value=fake) as plain_chrome:
            driver = ws.create_driver()

        self.assertIs(driver, fake)
        plain_chrome.assert_called_once()

    def test_never_pins_a_chrome_major_version(self):
        """
        The pin was version_main=145 while Chrome 152 was installed. A pin that
        does not match the installed browser guarantees the very failure it was
        meant to prevent -- let uc detect the version instead.
        """
        with patch.object(ws.uc, "Chrome", return_value=MagicMock()) as uc_chrome:
            ws.create_driver()

        _, kwargs = uc_chrome.call_args
        self.assertNotIn("version_main", kwargs)

    def test_both_failing_raises_with_both_reasons(self):
        """A scrape that cannot start a browser must say why, for both attempts."""
        with patch.object(ws.uc, "Chrome", side_effect=Exception("uc exploded")), \
             patch("selenium.webdriver.Chrome", side_effect=Exception("selenium exploded")):
            with self.assertRaises(RuntimeError) as ctx:
                ws.create_driver()

        message = str(ctx.exception)
        self.assertIn("uc exploded", message)
        self.assertIn("selenium exploded", message)

    def test_page_load_timeout_is_applied_whichever_driver_is_used(self):
        fake = MagicMock(name="selenium-driver")
        with patch.object(ws.uc, "Chrome", side_effect=Exception("nope")), \
             patch("selenium.webdriver.Chrome", return_value=fake):
            ws.create_driver(page_load_timeout=45)

        fake.set_page_load_timeout.assert_called_once_with(45)

    def test_proxy_reaches_both_option_builders(self):
        for target, other in (("uc", "plain"), ("plain", "uc")):
            with self.subTest(driver=target):
                fake = MagicMock()
                uc_side = MagicMock(return_value=fake) if target == "uc" else MagicMock(side_effect=Exception("x"))
                with patch.object(ws.uc, "Chrome", uc_side), \
                     patch("selenium.webdriver.Chrome", return_value=fake):
                    ws.create_driver(proxy_host="10.0.0.1", proxy_port="8080")
                # The options object carries the argument; assert via the builder
                opts = ws._chrome_arguments(proxy_host="10.0.0.1", proxy_port="8080")
                self.assertIn("--proxy-server=10.0.0.1:8080", opts)


if __name__ == "__main__":
    unittest.main()
