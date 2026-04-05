"""
Scrape state management for incremental/delta scraping.

Tracks page metadata to enable efficient delta scraping by detecting
which pages are new, changed, or unchanged since the last scrape.

Author: Cosmin
Created: 2026-02-10
"""

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from logger import get_logger

logger = get_logger(__name__)


@dataclass
class PageState:
    """State information for a single scraped page."""
    last_scraped: str  # ISO format timestamp
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    content_hash: Optional[str] = None
    sitemap_lastmod: Optional[str] = None
    file_path: Optional[str] = None
    status: str = "active"  # "active" or "removed"
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {k: v for k, v in asdict(self).items() if v is not None}
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PageState':
        """Create PageState from dictionary."""
        return cls(
            last_scraped=data.get('last_scraped', ''),
            etag=data.get('etag'),
            last_modified=data.get('last_modified'),
            content_hash=data.get('content_hash'),
            sitemap_lastmod=data.get('sitemap_lastmod'),
            file_path=data.get('file_path'),
            status=data.get('status', 'active')
        )


@dataclass
class ScrapeState:
    """Complete state for a website scrape."""
    last_full_scrape: Optional[str] = None
    pages: Dict[str, PageState] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'last_full_scrape': self.last_full_scrape,
            'pages': {url: page.to_dict() for url, page in self.pages.items()}
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ScrapeState':
        """Create ScrapeState from dictionary."""
        pages = {}
        for url, page_data in data.get('pages', {}).items():
            pages[url] = PageState.from_dict(page_data)
        return cls(
            last_full_scrape=data.get('last_full_scrape'),
            pages=pages
        )


class ScrapeStateManager:
    """
    Manages the scrape state file for incremental scraping.
    
    The state file tracks metadata about previously scraped pages
    to enable efficient delta detection.
    """
    
    STATE_FILENAME = "scrape_state.json"
    
    def __init__(self, website_dir: str):
        """
        Initialize the state manager.
        
        Args:
            website_dir: Directory where the state file will be stored
        """
        self.website_dir = website_dir
        self.state_file_path = os.path.join(website_dir, self.STATE_FILENAME)
        self.state: ScrapeState = ScrapeState()
        self._loaded = False
        self._corrupted = False
    
    def load(self) -> bool:
        """
        Load state from disk.
        
        Returns:
            True if state was loaded successfully, False otherwise
        """
        if not os.path.exists(self.state_file_path):
            logger.info(f"No existing state file found at {self.state_file_path}")
            self._loaded = True
            return False
        
        try:
            with open(self.state_file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            self.state = ScrapeState.from_dict(data)
            self._loaded = True
            logger.info(f"Loaded state with {len(self.state.pages)} tracked pages")
            return True
            
        except json.JSONDecodeError as e:
            logger.warning(f"State file corrupted (JSON error): {e}")
            self._corrupted = True
            self.state = ScrapeState()
            self._loaded = True
            return False
            
        except Exception as e:
            logger.warning(f"Failed to load state file: {e}")
            self._corrupted = True
            self.state = ScrapeState()
            self._loaded = True
            return False
    
    def save(self) -> bool:
        """
        Save state to disk.
        
        Returns:
            True if saved successfully, False otherwise
        """
        try:
            # Ensure directory exists
            os.makedirs(self.website_dir, exist_ok=True)
            
            # Write to temp file first, then rename (atomic operation)
            temp_path = self.state_file_path + '.tmp'
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(self.state.to_dict(), f, indent=2, ensure_ascii=False)
            
            # Atomic rename
            os.replace(temp_path, self.state_file_path)
            
            logger.debug(f"Saved state with {len(self.state.pages)} pages")
            return True
            
        except Exception as e:
            logger.error(f"Failed to save state file: {e}")
            return False
    
    def is_corrupted(self) -> bool:
        """Check if the state file was corrupted on load."""
        return self._corrupted
    
    def has_state(self) -> bool:
        """Check if there is existing state data."""
        return len(self.state.pages) > 0
    
    def get_page_state(self, url: str) -> Optional[PageState]:
        """Get the state for a specific URL."""
        return self.state.pages.get(url)
    
    def update_page_state(
        self,
        url: str,
        file_path: str,
        content_hash: Optional[str] = None,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
        sitemap_lastmod: Optional[str] = None
    ) -> None:
        """
        Update or create state for a page.
        
        Args:
            url: The page URL
            file_path: Path to the saved HTML file
            content_hash: SHA-256 hash of the content
            etag: ETag header value
            last_modified: Last-Modified header value
            sitemap_lastmod: lastmod value from sitemap
        """
        now = datetime.now(timezone.utc).isoformat()
        
        self.state.pages[url] = PageState(
            last_scraped=now,
            file_path=file_path,
            content_hash=content_hash,
            etag=etag,
            last_modified=last_modified,
            sitemap_lastmod=sitemap_lastmod,
            status="active"
        )
    
    def mark_removed(self, url: str) -> None:
        """Mark a URL as removed from sitemap (without deleting its data)."""
        if url in self.state.pages:
            self.state.pages[url].status = "removed"
            logger.debug(f"Marked as removed: {url}")
    
    def mark_full_scrape(self) -> None:
        """Mark that a full scrape has been completed."""
        self.state.last_full_scrape = datetime.now(timezone.utc).isoformat()
    
    def get_all_tracked_urls(self) -> set:
        """Get all URLs currently tracked in state."""
        return set(self.state.pages.keys())
    
    def get_active_urls(self) -> set:
        """Get URLs that are not marked as removed."""
        return {
            url for url, page in self.state.pages.items()
            if page.status == "active"
        }


def compute_content_hash(content: str) -> str:
    """
    Compute SHA-256 hash of content.
    
    Args:
        content: The content to hash
        
    Returns:
        Hash string in format "sha256:hexdigest"
    """
    hash_obj = hashlib.sha256(content.encode('utf-8'))
    return f"sha256:{hash_obj.hexdigest()}"


def parse_iso_datetime(date_str: str) -> Optional[datetime]:
    """
    Parse an ISO format datetime string.
    
    Handles various formats including:
    - Full ISO: 2026-02-05T14:30:00Z
    - Date only: 2026-02-05
    - With timezone: 2026-02-05T14:30:00+00:00
    
    Args:
        date_str: The datetime string to parse
        
    Returns:
        Parsed datetime object or None if parsing fails
    """
    if not date_str:
        return None
    
    # Try various formats
    formats = [
        '%Y-%m-%dT%H:%M:%S%z',      # Full ISO with timezone
        '%Y-%m-%dT%H:%M:%SZ',        # Full ISO with Z
        '%Y-%m-%dT%H:%M:%S',         # Full ISO without timezone
        '%Y-%m-%d',                  # Date only
    ]
    
    # Handle Z timezone indicator
    date_str = date_str.replace('Z', '+00:00')
    
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    
    # Try fromisoformat as fallback (Python 3.7+)
    try:
        return datetime.fromisoformat(date_str)
    except ValueError:
        pass
    
    logger.debug(f"Could not parse date: {date_str}")
    return None


def is_page_stale(
    page_state: PageState,
    max_age_days: Optional[int] = None
) -> bool:
    """
    Check if a page is stale based on max age.
    
    Args:
        page_state: The page state to check
        max_age_days: Maximum age in days before page is considered stale
        
    Returns:
        True if page is stale and should be re-scraped
    """
    if max_age_days is None:
        return False
    
    last_scraped = parse_iso_datetime(page_state.last_scraped)
    if not last_scraped:
        return True  # Can't determine age, re-scrape to be safe
    
    # Make timezone-aware if needed
    if last_scraped.tzinfo is None:
        last_scraped = last_scraped.replace(tzinfo=timezone.utc)
    
    now = datetime.now(timezone.utc)
    age = now - last_scraped
    
    return age.days >= max_age_days
