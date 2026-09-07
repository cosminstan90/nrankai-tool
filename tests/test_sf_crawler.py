"""
Etapa 5 of docs/IMPROVEMENTS_PLAN.md -- the Screaming Frog crawl runner.

These tests never launch Screaming Frog. They cover the two things that are
easy to get wrong and expensive to discover later: refusing to crawl without
crawl limits configured, and building an argv list that survives arguments
containing spaces.
"""
import unittest
from unittest.mock import patch

from core.sf_crawler import SfConfigMissing, build_argv, run_crawl


class TestCrawlRefusesWithoutConfig(unittest.TestCase):
    def test_missing_config_raises_rather_than_crawling_unthrottled(self):
        """
        docs/IMPROVEMENTS_PLAN.md: "Rate limiting si robots.txt nu sunt
        optionale." Screaming Frog's own defaults are unlimited depth,
        unlimited pages and 5 concurrent threads, so a crawl with no config
        file would hit a real site far harder than intended. Refusing is the
        safe behaviour; there is deliberately no fallback.
        """
        with patch.dict("os.environ", {"SCREAMING_FROG_CONFIG": ""}, clear=False):
            with self.assertRaises(SfConfigMissing):
                run_crawl("https://example.com", output_dir="/tmp/should-not-be-created")

    def test_config_pointing_at_a_nonexistent_file_also_refuses(self):
        """A stale path in .env is as dangerous as no path at all."""
        with patch.dict("os.environ", {"SCREAMING_FROG_CONFIG": "/no/such/file.seospiderconfig"}):
            with self.assertRaises(SfConfigMissing):
                run_crawl("https://example.com", output_dir="/tmp/should-not-be-created")


class TestArgvConstruction(unittest.TestCase):
    def test_spaced_arguments_stay_single_elements(self):
        """
        Regression guard. PowerShell's -ArgumentList split
        "Response Codes:Internal Client Error (4xx)" on spaces and Screaming
        Frog exited with "SeoSpider failed to start". An argv list handed to
        subprocess must keep it as one element.
        """
        argv = build_argv("https://example.com", r"C:\out", r"C:\cfg.seospiderconfig")
        tabs = argv[argv.index("--export-tabs") + 1]
        self.assertIn("Response Codes:Internal Client Error (4xx)", tabs)

    def test_config_is_always_passed(self):
        argv = build_argv("https://example.com", r"C:\out", r"C:\cfg.seospiderconfig")
        self.assertIn("--config", argv)
        self.assertEqual(argv[argv.index("--config") + 1], r"C:\cfg.seospiderconfig")

    def test_runs_headless_and_overwrites(self):
        argv = build_argv("https://example.com", r"C:\out", r"C:\cfg.seospiderconfig")
        self.assertIn("--headless", argv)
        self.assertIn("--overwrite", argv)
        self.assertEqual(argv[argv.index("--crawl") + 1], "https://example.com")


if __name__ == "__main__":
    unittest.main()
