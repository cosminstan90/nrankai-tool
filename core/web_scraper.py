"""
Web scraper that downloads pages from a website based on sitemap.

Supports incremental/delta scraping to only re-download pages that are
new or changed, saving time on large sites.

Supports CLI arguments to override .env configuration.

Author: Cosmin
Created: 2026-01-23
Updated: 2026-02-10 - Added incremental/delta scraping support
"""

import os
import re
import time
import random
import xml.etree.ElementTree as ET
import certifi
import argparse
import requests
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from tqdm import tqdm

# Import configuration module
from core import config

# Import logger
from core.logger import get_logger, setup_logging

# Import scrape state management
from core.scrape_state import (
    ScrapeStateManager,
    PageState,
    compute_content_hash,
    parse_iso_datetime,
    is_page_stale,
)

# Initialize module logger
logger = get_logger(__name__)

# JavaScript to traverse and flatten Shadow DOM into a single HTML string
DEEP_HTML_SCRIPT = """
const getDeepHTML = (node) => {
    let html = '';
    if (node.nodeType === Node.ELEMENT_NODE) {
        html += `<${node.tagName.toLowerCase()}`;
        for (let attr of node.attributes) {
            html += ` ${attr.name}="${attr.value}"`;
        }
        html += '>';
        if (node.shadowRoot) {
            html += getDeepHTML(node.shadowRoot);
        }
        for (let child of node.childNodes) {
            html += getDeepHTML(child);
        }
        html += `</${node.tagName.toLowerCase()}>`;
    } else if (node.nodeType === Node.TEXT_NODE) {
        html += node.textContent;
    } else if (node.nodeType === Node.DOCUMENT_FRAGMENT_NODE) {
        for (let child of node.childNodes) {
            html += getDeepHTML(child);
        }
    }
    return html;
};
return getDeepHTML(document.body);
"""


@dataclass
class SitemapEntry:
    """Represents a URL entry from the sitemap with optional metadata."""
    url: str
    lastmod: Optional[str] = None
    changefreq: Optional[str] = None
    priority: Optional[str] = None


@dataclass
class ScrapeDecision:
    """Decision about whether to scrape a URL and why."""
    should_scrape: bool
    reason: str  # "new", "changed_lastmod", "changed_headers", "stale", "force", "unchanged"
    

@dataclass
class ScrapeSummary:
    """Summary statistics for a scrape run."""
    total_urls: int = 0
    new_pages: int = 0
    changed_pages: int = 0
    unchanged_pages: int = 0
    failed_pages: int = 0
    removed_pages: int = 0
    elapsed_seconds: float = 0.0
    
    def get_time_saved_estimate(self, avg_seconds_per_page: float = 6.0) -> float:
        """
        Estimate time saved by skipping unchanged pages.
        
        Args:
            avg_seconds_per_page: Average time to scrape one page (default: 6 seconds)
            
        Returns:
            Estimated seconds saved
        """
        return self.unchanged_pages * avg_seconds_per_page
    
    def format_summary(self) -> str:
        """Format the summary as a human-readable string."""
        time_saved = self.get_time_saved_estimate()
        time_saved_minutes = int(time_saved // 60)
        
        lines = [
            "",
            "=" * 60,
            "SCRAPE SUMMARY",
            "=" * 60,
            f"  Total URLs in sitemap:       {self.total_urls:>6}",
            f"  New pages (not seen before): {self.new_pages:>6}",
            f"  Changed pages (re-scraped):  {self.changed_pages:>6}",
            f"  Unchanged pages (skipped):   {self.unchanged_pages:>6}",
            f"  Failed:                      {self.failed_pages:>6}",
        ]
        
        if self.removed_pages > 0:
            lines.append(f"  Removed from sitemap:        {self.removed_pages:>6}")
        
        minutes = int(self.elapsed_seconds // 60)
        seconds = int(self.elapsed_seconds % 60)
        lines.append(f"  Elapsed time:                {minutes}m {seconds}s")
        
        if time_saved_minutes > 0:
            lines.append(f"  Time saved (estimated):      ~{time_saved_minutes} minutes")
        
        lines.append("=" * 60)
        
        return "\n".join(lines)


def fetch_sitemap_urls(sitemap_url: str, driver=None) -> List[SitemapEntry]:
    """
    Fetch URLs from sitemap XML, including lastmod dates if available.
    
    Uses requests library first (fast, reliable for XML).
    Falls back to Selenium only if requests fails (e.g., behind auth/WAF).
    
    Args:
        sitemap_url: URL of the sitemap
        driver: Selenium WebDriver instance (optional, used as fallback)
        
    Returns:
        List of SitemapEntry objects with URL and optional metadata
    """
    entries = []
    namespace = {'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9'}

    raw_xml = None
    
    # Method 1: Try requests first (fast, no browser needed)
    try:
        logger.info(f"Fetching sitemap: {sitemap_url}")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = requests.get(sitemap_url, headers=headers, timeout=30, verify=True)
        response.raise_for_status()
        raw_xml = response.text
        logger.info(f"Sitemap fetched via HTTP ({len(raw_xml):,} bytes)")
    except Exception as e:
        logger.warning(f"HTTP fetch failed for sitemap: {e}")
    
    # Method 2: Fall back to Selenium if requests failed
    if raw_xml is None and driver is not None:
        try:
            logger.info("Falling back to Selenium for sitemap fetch...")
            driver.get(sitemap_url)
            time.sleep(10)
            
            # Try multiple extraction methods
            # Method 2a: page source (preserves XML tags)
            raw_xml = driver.page_source
            
            # Method 2b: if page_source is wrapped in HTML, try innerText
            if raw_xml and '<urlset' not in raw_xml.lower() and '<sitemapindex' not in raw_xml.lower():
                raw_text = driver.execute_script("return document.body.innerText;")
                if '<urlset' in raw_text.lower() or '<sitemapindex' in raw_text.lower():
                    raw_xml = raw_text
                else:
                    # Method 2c: try getting XML from pre tag (Chrome XML viewer)
                    try:
                        raw_xml = driver.execute_script(
                            "var pre = document.querySelector('pre'); "
                            "return pre ? pre.textContent : document.documentElement.outerHTML;"
                        )
                    except Exception:
                        pass
            
            logger.info(f"Sitemap fetched via Selenium ({len(raw_xml):,} bytes)")
        except Exception as e:
            logger.error(f"Selenium fetch also failed for sitemap: {e}")
            return entries
    
    if not raw_xml:
        logger.error(f"Could not fetch sitemap from {sitemap_url}")
        return entries
    
    # Parse XML
    try:
        # Try to extract clean XML if embedded in HTML
        match = re.search(r"(<(?:urlset|sitemapindex)\b.*?<\/(?:urlset|sitemapindex)>)", raw_xml, re.DOTALL | re.IGNORECASE)
        
        if match:
            clean_xml = match.group(1)
        else:
            # Maybe the whole response is valid XML
            clean_xml = raw_xml.strip()
        
        # Fix unescaped ampersands
        clean_xml = re.sub(r'&(?![a-zA-Z0-9#]+;)', '&amp;', clean_xml)
        
        root = ET.fromstring(clean_xml)
        
        # Check if this is a sitemap index (contains sitemaps pointing to other sitemaps)
        sitemap_refs = root.findall('.//ns:sitemap/ns:loc', namespace)
        if sitemap_refs:
            # This is a sitemap index, recursively fetch each sitemap
            logger.info(f"Found sitemap index with {len(sitemap_refs)} sitemaps")
            for sitemap_ref in sitemap_refs:
                if sitemap_ref.text:
                    sub_entries = fetch_sitemap_urls(sitemap_ref.text.strip(), driver)
                    entries.extend(sub_entries)
            return entries
        
        # Regular sitemap with URL entries
        for url_elem in root.findall('.//ns:url', namespace):
            loc = url_elem.find('ns:loc', namespace)
            if loc is not None and loc.text:
                entry = SitemapEntry(url=loc.text.strip())
                
                # Extract optional metadata
                lastmod = url_elem.find('ns:lastmod', namespace)
                if lastmod is not None and lastmod.text:
                    entry.lastmod = lastmod.text.strip()
                
                changefreq = url_elem.find('ns:changefreq', namespace)
                if changefreq is not None and changefreq.text:
                    entry.changefreq = changefreq.text.strip()
                
                priority = url_elem.find('ns:priority', namespace)
                if priority is not None and priority.text:
                    entry.priority = priority.text.strip()
                
                entries.append(entry)
        
        if entries:
            # Count entries with lastmod
            with_lastmod = sum(1 for e in entries if e.lastmod)
            logger.info(
                f"Successfully loaded {len(entries)} URLs from sitemap "
                f"({with_lastmod} with lastmod dates)"
            )
        else:
            logger.warning(f"Sitemap parsed but contained 0 URLs. XML root tag: {root.tag}")
            
    except (ET.ParseError, Exception) as e:
        logger.error(f"Error parsing sitemap XML: {e}", exc_info=True)
        # Log a snippet for debugging
        snippet = raw_xml[:500] if raw_xml else "(empty)"
        logger.error(f"First 500 chars of sitemap content: {snippet}")
    
    return entries


def check_http_headers(
    url: str,
    proxy_host: Optional[str] = None,
    proxy_port: Optional[str] = None,
    timeout: int = 10
) -> Tuple[Optional[str], Optional[str]]:
    """
    Perform HEAD request to check ETag and Last-Modified headers.
    
    Args:
        url: URL to check
        proxy_host: Proxy hostname (optional)
        proxy_port: Proxy port (optional)
        timeout: Request timeout in seconds
        
    Returns:
        Tuple of (etag, last_modified) header values (or None if not present)
    """
    proxies = None
    if proxy_host and proxy_port:
        proxy_url = f"http://{proxy_host}:{proxy_port}"
        proxies = {"http": proxy_url, "https": proxy_url}
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    try:
        response = requests.head(
            url,
            headers=headers,
            proxies=proxies,
            timeout=timeout,
            allow_redirects=True
        )
        
        etag = response.headers.get('ETag')
        last_modified = response.headers.get('Last-Modified')
        
        return etag, last_modified
        
    except requests.RequestException as e:
        logger.debug(f"HEAD request failed for {url}: {e}")
        return None, None


def should_scrape_page(
    entry: SitemapEntry,
    state_manager: ScrapeStateManager,
    force_scrape: bool = False,
    changed_since: Optional[str] = None,
    max_age_days: Optional[int] = None,
    check_headers: bool = True,
    proxy_host: Optional[str] = None,
    proxy_port: Optional[str] = None,
) -> ScrapeDecision:
    """
    Determine whether a page should be scraped based on delta detection.
    
    Uses the following strategy (in order of reliability):
    1. Force scrape if requested
    2. New page (not in state) -> scrape
    3. Check sitemap lastmod against stored value
    4. Check HTTP headers (ETag, Last-Modified) via HEAD request
    5. Check if page is stale based on max_age_days
    
    Args:
        entry: Sitemap entry with URL and metadata
        state_manager: State manager instance
        force_scrape: Force re-scrape regardless of state
        changed_since: Only scrape pages changed after this date (ISO format)
        max_age_days: Re-scrape pages older than this many days
        check_headers: Whether to perform HEAD requests for header checking
        proxy_host: Proxy hostname for HEAD requests
        proxy_port: Proxy port for HEAD requests
        
    Returns:
        ScrapeDecision with should_scrape flag and reason
    """
    url = entry.url
    
    # Force scrape requested
    if force_scrape:
        return ScrapeDecision(should_scrape=True, reason="force")
    
    # Get existing state for this URL
    page_state = state_manager.get_page_state(url)
    
    # New page (not seen before)
    if page_state is None:
        return ScrapeDecision(should_scrape=True, reason="new")
    
    # Check changed_since filter
    if changed_since:
        changed_since_dt = parse_iso_datetime(changed_since)
        if changed_since_dt and entry.lastmod:
            lastmod_dt = parse_iso_datetime(entry.lastmod)
            if lastmod_dt and lastmod_dt > changed_since_dt:
                return ScrapeDecision(should_scrape=True, reason="changed_lastmod")
            elif lastmod_dt:
                return ScrapeDecision(should_scrape=False, reason="unchanged")
    
    # Strategy 1: Check sitemap lastmod
    if entry.lastmod and page_state.sitemap_lastmod:
        if entry.lastmod != page_state.sitemap_lastmod:
            logger.debug(f"Lastmod changed for {url}: {page_state.sitemap_lastmod} -> {entry.lastmod}")
            return ScrapeDecision(should_scrape=True, reason="changed_lastmod")
    
    # Strategy 2: Check HTTP headers via HEAD request
    if check_headers:
        etag, last_modified = check_http_headers(url, proxy_host, proxy_port)
        
        # Check ETag
        if etag and page_state.etag:
            if etag != page_state.etag:
                logger.debug(f"ETag changed for {url}")
                return ScrapeDecision(should_scrape=True, reason="changed_headers")
        
        # Check Last-Modified
        if last_modified and page_state.last_modified:
            if last_modified != page_state.last_modified:
                logger.debug(f"Last-Modified changed for {url}")
                return ScrapeDecision(should_scrape=True, reason="changed_headers")
    
    # Strategy 3: Check if page is stale based on max_age
    if max_age_days is not None:
        if is_page_stale(page_state, max_age_days):
            logger.debug(f"Page stale (>{max_age_days} days old): {url}")
            return ScrapeDecision(should_scrape=True, reason="stale")
    
    # No change detected
    return ScrapeDecision(should_scrape=False, reason="unchanged")


def scrape(
    website: str = None,
    sitemap: str = None,
    output_dir: str = None,
    no_proxy: bool = False,
    delay_range: tuple = (1.5, 3.5),
    proxy_host: str = None,
    proxy_port: str = None,
    # Delta scraping options
    full_scrape: bool = False,
    changed_since: Optional[str] = None,
    max_age_days: Optional[int] = None,
    skip_header_check: bool = False,
    # Shadow DOM options
    shadow_root_selector: Optional[str] = None,
    progress_callback = None,
):
    """
    Main scraping function with incremental/delta scraping support.
    
    Args:
        website: Target website domain
        sitemap: Sitemap URL
        output_dir: Directory to save HTML files
        no_proxy: Disable proxy even if configured
        delay_range: Tuple of (min, max) seconds for random delay
        proxy_host: Proxy hostname (if different from config)
        proxy_port: Proxy port (if different from config)
        full_scrape: Force re-scrape of all pages, ignoring state
        changed_since: Only scrape pages changed after this date (YYYY-MM-DD)
        max_age_days: Re-scrape pages older than N days regardless of change detection
        skip_header_check: Skip HTTP HEAD requests for header checking
        shadow_root_selector: CSS selector for Shadow DOM content container (e.g. '#outlet-content').
                              If set, the scraper waits for this element to hydrate before capturing HTML.
                              If None, no Shadow DOM wait is performed (default).
    """
    start_time = time.time()
    os.environ['SSL_CERT_FILE'] = certifi.where()
    
    # Initialize summary
    summary = ScrapeSummary()
    
    # Use provided values or fall back to config
    if website is None:
        website = config.get_website()
    if sitemap is None:
        sitemap = config.get_sitemap()
    if output_dir is None:
        paths = config.get_paths(website_override=website)
        output_dir = paths["input_html_dir"]
    
    # Get proxy settings
    if not no_proxy:
        if proxy_host is None:
            proxy_host = config.get_proxy_host()
        if proxy_port is None:
            proxy_port = config.get_proxy_port()
    
    clean_site_name = re.sub(r'https?://', '', website).replace('/', '_')
    
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    # Initialize state manager (use output_dir — already a valid local path)
    state_manager = ScrapeStateManager(output_dir)
    state_loaded = state_manager.load()
    
    # Handle corrupted state
    if state_manager.is_corrupted():
        logger.warning("State file was corrupted. Performing full scrape to rebuild state.")
        full_scrape = True
    
    # First run detection
    if not state_loaded or not state_manager.has_state():
        logger.info("First run detected (no existing state). Will scrape all pages.")
        full_scrape = True

    options = uc.ChromeOptions()

    # Configure proxy if available
    if proxy_host and proxy_port:
        options.add_argument(f'--proxy-server={proxy_host}:{proxy_port}')
        logger.info(f"Using proxy: {proxy_host}:{proxy_port}")

    options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537")

    # Remove stale cached chromedriver to prevent WinError 183 on rename
    _uc_exe = os.path.join(os.environ.get("APPDATA", ""), "undetected_chromedriver", "undetected_chromedriver.exe")
    if os.path.exists(_uc_exe):
        try:
            os.remove(_uc_exe)
        except OSError:
            pass

    driver = uc.Chrome(options=options, version_main=145)
    driver.set_page_load_timeout(30)

    # Fetch sitemap entries with metadata
    entries = fetch_sitemap_urls(sitemap, driver)
    if not entries:
        logger.error("No URLs found in sitemap. Exiting.")
        driver.quit()
        return
    
    summary.total_urls = len(entries)
    
    # Track URLs in current sitemap for removal detection
    current_sitemap_urls = {e.url for e in entries}
    previously_tracked_urls = state_manager.get_all_tracked_urls()
    
    # Mark removed URLs
    removed_urls = previously_tracked_urls - current_sitemap_urls
    for removed_url in removed_urls:
        state_manager.mark_removed(removed_url)
        summary.removed_pages += 1
    
    if removed_urls:
        logger.info(f"Detected {len(removed_urls)} URLs removed from sitemap (marked in state)")
    
    # Determine which pages to scrape
    scrape_decisions: List[Tuple[SitemapEntry, ScrapeDecision]] = []
    
    logger.info("Analyzing sitemap for changes...")
    for entry in tqdm(entries, desc="Analyzing URLs", unit="url"):
        decision = should_scrape_page(
            entry=entry,
            state_manager=state_manager,
            force_scrape=full_scrape,
            changed_since=changed_since,
            max_age_days=max_age_days,
            check_headers=not skip_header_check,
            proxy_host=proxy_host if not no_proxy else None,
            proxy_port=proxy_port if not no_proxy else None,
        )
        scrape_decisions.append((entry, decision))
    
    # Filter to pages that need scraping
    pages_to_scrape = [(e, d) for e, d in scrape_decisions if d.should_scrape]
    pages_to_skip = [(e, d) for e, d in scrape_decisions if not d.should_scrape]
    
    # Count by reason
    new_count = sum(1 for _, d in scrape_decisions if d.reason == "new")
    changed_count = sum(1 for _, d in scrape_decisions if d.reason in ("changed_lastmod", "changed_headers", "stale"))
    unchanged_count = sum(1 for _, d in scrape_decisions if d.reason == "unchanged")
    force_count = sum(1 for _, d in scrape_decisions if d.reason == "force")
    
    # Log scrape plan
    logger.info(f"Scrape plan: {len(pages_to_scrape)} to scrape, {len(pages_to_skip)} to skip")
    logger.info(f"  - New pages: {new_count}")
    logger.info(f"  - Changed pages: {changed_count}")
    if force_count > 0:
        logger.info(f"  - Forced: {force_count}")
    logger.info(f"  - Unchanged (skipped): {unchanged_count}")
    
    summary.unchanged_pages = unchanged_count
    
    if not pages_to_scrape:
        logger.info("No pages need scraping. Everything is up to date!")
        if progress_callback:
            try:
                progress_callback(len(entries), len(entries))
            except Exception as cb_e:
                logger.debug(f"Progress callback error: {cb_e}")
        summary.elapsed_seconds = time.time() - start_time
        print(summary.format_summary())
        driver.quit()
        state_manager.save()
        return
    
    if progress_callback:
        try:
            progress_callback(unchanged_count, len(entries))
        except Exception as cb_e:
            logger.debug(f"Progress callback error: {cb_e}")
            
    logger.info(f"Starting download of {len(pages_to_scrape)} pages")
    logger.info(f"Delay range: {delay_range[0]:.1f}-{delay_range[1]:.1f} seconds")
    logger.info(f"Output directory: {output_dir}")

    succeeded = 0
    failed = 0

    for entry, decision in tqdm(pages_to_scrape, desc="Scraping Progress", unit="page"):
        url = entry.url
        try:
            # Human-like random delay
            time.sleep(random.uniform(*delay_range))

            driver.get(url)

            # Wait for Body to exist
            WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

            # Wait for content to load
            # If a Shadow DOM selector is configured, wait for it to hydrate
            if shadow_root_selector:
                try:
                    # Support both CSS selector and bare ID formats
                    if shadow_root_selector.startswith('#'):
                        by_method = By.CSS_SELECTOR
                    else:
                        by_method = By.CSS_SELECTOR
                    
                    WebDriverWait(driver, 15).until(
                        EC.presence_of_element_located((by_method, shadow_root_selector))
                    )

                    js_selector = shadow_root_selector.replace("'", "\\'")
                    WebDriverWait(driver, 10).until(
                        lambda d: d.execute_script(
                            f"var el = document.querySelector('{js_selector}'); "
                            f"return el && (el.children.length > 0 || el.shadowRoot !== null);"
                        )
                    )

                    time.sleep(2)
                except Exception as e:
                    logger.debug(f"Shadow DOM hydration warning for {url}: {e}")

            # Capture full HTML including Shadow DOM
            rendered_html = driver.execute_script(DEEP_HTML_SCRIPT)

            # Generate safe filename
            import urllib.parse
            safe_name = re.sub(r'https?://', '', url)
            safe_name = urllib.parse.unquote(safe_name)
            # Remove all invalid Windows filename characters, including control characters
            safe_name = re.sub(r'[\\/*?:"<>|\x00-\x1F\x7F]', '_', safe_name)
            # Strip trailing dots and whitespace
            safe_name = safe_name.strip('. \t\n\r')
            if not safe_name:
                safe_name = "index"
                
            if len(safe_name) > 150: 
                safe_name = safe_name[:150]

            file_path = os.path.join(output_dir, f"{safe_name}.html")
            
            # Compute content hash
            content_hash = compute_content_hash(rendered_html)
            
            # Check if content actually changed (for already-existing pages)
            existing_state = state_manager.get_page_state(url)
            if existing_state and existing_state.content_hash == content_hash:
                # Content hasn't actually changed, just update timestamp
                logger.debug(f"Content unchanged (hash match) for {url}")
                state_manager.update_page_state(
                    url=url,
                    file_path=file_path,
                    content_hash=content_hash,
                    sitemap_lastmod=entry.lastmod,
                    etag=existing_state.etag,
                    last_modified=existing_state.last_modified,
                )
            else:
                # Write the file
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(rendered_html)
                
                # Get HTTP headers for state tracking
                etag, last_modified = None, None
                if not skip_header_check:
                    etag, last_modified = check_http_headers(
                        url,
                        proxy_host if not no_proxy else None,
                        proxy_port if not no_proxy else None
                    )
                
                # Update state
                state_manager.update_page_state(
                    url=url,
                    file_path=file_path,
                    content_hash=content_hash,
                    etag=etag,
                    last_modified=last_modified,
                    sitemap_lastmod=entry.lastmod,
                )
            
            # Save state incrementally
            state_manager.save()
            succeeded += 1
            
            # Track statistics
            if decision.reason == "new":
                summary.new_pages += 1
            else:
                summary.changed_pages += 1
                
            if progress_callback:
                try:
                    progress_callback(unchanged_count + succeeded + failed, len(entries))
                except Exception as cb_e:
                    logger.debug(f"Progress callback error: {cb_e}")

        except Exception as e:
            failed += 1
            summary.failed_pages += 1
            # Use tqdm.write to maintain progress bar display
            tqdm.write(f"    [X] Failed {url}: {str(e)[:100]}")
            logger.error(f"Failed to scrape {url}: {e}", exc_info=False)
            try:
                driver.execute_script("window.stop();")
            except Exception as stop_error:
                logger.debug(f"Could not stop page load: {stop_error}")
            
            if progress_callback:
                try:
                    progress_callback(unchanged_count + succeeded + failed, len(entries))
                except Exception as cb_e:
                    logger.debug(f"Progress callback error: {cb_e}")

    # Save state
    if full_scrape:
        state_manager.mark_full_scrape()
    state_manager.save()

    # Calculate elapsed time
    summary.elapsed_seconds = time.time() - start_time
    minutes = int(summary.elapsed_seconds // 60)
    seconds = int(summary.elapsed_seconds % 60)

    # Summary log
    logger.info(
        f"Scraping complete: {succeeded}/{len(pages_to_scrape)} pages succeeded, "
        f"{failed} failed, {minutes}m {seconds}s elapsed"
    )
    logger.info(f"Files saved in: {output_dir}")
    
    # Print formatted summary
    print(summary.format_summary())
    
    driver.quit()


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Scrape website pages based on sitemap (supports incremental/delta scraping)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Use .env defaults (incremental mode)
  python web_scraper.py
  
  # Force full re-scrape
  python web_scraper.py --full-scrape
  
  # Only scrape pages changed after a date
  python web_scraper.py --scrape-changed-since 2026-01-15
  
  # Re-scrape pages older than 7 days
  python web_scraper.py --max-age 7
  
  # Override website and sitemap
  python web_scraper.py --website example.com --sitemap https://example.com/sitemap.xml
  
  # Disable proxy
  python web_scraper.py --no-proxy
  
  # Custom delay range
  python web_scraper.py --delay 2.0-4.0
  
  # Skip HTTP header checks (faster but less accurate change detection)
  python web_scraper.py --skip-header-check
        '''
    )
    
    parser.add_argument(
        '--website',
        type=str,
        help='Target website domain (overrides WEBSITE in .env)'
    )
    
    parser.add_argument(
        '--sitemap',
        type=str,
        help='Sitemap URL (overrides SITEMAP in .env)'
    )
    
    parser.add_argument(
        '--output-dir',
        type=str,
        help='Directory to save HTML files (default: {website}/input_html)'
    )
    
    parser.add_argument(
        '--no-proxy',
        action='store_true',
        help='Disable proxy even if configured in .env'
    )
    
    parser.add_argument(
        '--delay',
        type=str,
        default='1.5-3.5',
        help='Random delay range between requests (format: "min-max", default: "1.5-3.5")'
    )
    
    parser.add_argument(
        '--log-level',
        type=str,
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        default='INFO',
        help='Set logging level (default: INFO)'
    )
    
    # Delta scraping arguments
    delta_group = parser.add_argument_group('Incremental/Delta Scraping')
    
    delta_group.add_argument(
        '--full-scrape',
        action='store_true',
        help='Force re-scrape of all pages, ignoring state (env: FULL_SCRAPE=true)'
    )
    
    delta_group.add_argument(
        '--scrape-changed-since',
        type=str,
        metavar='DATE',
        help='Only scrape pages with lastmod after this date (format: YYYY-MM-DD)'
    )
    
    delta_group.add_argument(
        '--max-age',
        type=int,
        metavar='DAYS',
        help='Re-scrape pages older than N days regardless of change detection'
    )
    
    delta_group.add_argument(
        '--skip-header-check',
        action='store_true',
        help='Skip HTTP HEAD requests for ETag/Last-Modified checking (faster but less accurate)'
    )
    
    # Shadow DOM options
    parser.add_argument(
        '--shadow-root-selector',
        type=str,
        default=None,
        help='CSS selector for Shadow DOM content container to wait for before capturing HTML '
             '(e.g., "#outlet-content" for Shadow DOM sites). If not set, no Shadow DOM wait is performed.'
    )
    
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    
    # Setup logging with specified level
    setup_logging(level=args.log_level)
    
    # Parse delay range
    try:
        delay_min, delay_max = map(float, args.delay.split('-'))
        delay_range = (delay_min, delay_max)
    except ValueError:
        logger.error(f"Invalid delay format '{args.delay}'. Use format: 'min-max' (e.g., '1.5-3.5')")
        exit(1)
    
    # Check environment variable for full scrape
    full_scrape = args.full_scrape or os.getenv('FULL_SCRAPE', '').lower() in ('true', '1', 'yes')
    
    # Configure with CLI arguments
    config.configure(
        website=args.website,
        sitemap=args.sitemap,
        no_proxy=args.no_proxy,
    )
    
    # Run scraper
    scrape(
        website=args.website,
        sitemap=args.sitemap,
        output_dir=args.output_dir,
        no_proxy=args.no_proxy,
        delay_range=delay_range,
        full_scrape=full_scrape,
        changed_since=args.scrape_changed_since,
        max_age_days=args.max_age,
        skip_header_check=args.skip_header_check,
        shadow_root_selector=args.shadow_root_selector,
    )
