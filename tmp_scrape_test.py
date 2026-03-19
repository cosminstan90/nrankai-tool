import asyncio
import web_scraper

def test_scrape():
    try:
        web_scraper.scrape(
            website="bancatransilvania.ro",
            sitemap="https://bancatransilvania.ro/sitemap.xml",
            output_dir="bancatransilvania.ro/input_html_test",
            no_proxy=True,
            delay_range=(1.0, 2.0),
            full_scrape=False
        )
    except Exception as e:
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_scrape()
