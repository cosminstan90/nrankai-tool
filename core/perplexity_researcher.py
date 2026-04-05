"""
Perplexity Research Module

Enriches page analysis with real-world AI search context by querying
Perplexity's API. This adds a research layer that shows:
- Whether the page/brand appears in AI search results
- What AI considers authoritative for the topic
- Content gaps between the page and AI recommendations
- Competitor mentions in AI responses

Works as Step 2.5 in the pipeline: Scrape → Convert → [Research] → Analyze → Score

Author: Cosmin
Created: 2026-02-13
"""

import os
import re
import json
import asyncio
import hashlib
import time
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

# Use OpenAI SDK since Perplexity API is OpenAI-compatible
try:
    from openai import AsyncOpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

from logger import get_logger

logger = get_logger(__name__)

# Perplexity models
PERPLEXITY_MODEL = "sonar"  # Fast, cheap, good for research queries
PERPLEXITY_MODEL_PRO = "sonar-pro"  # More thorough, 2x cost

# Rate limiting
MAX_CONCURRENT_QUERIES = 3
DELAY_BETWEEN_QUERIES = 0.5  # seconds

# Research query templates per audit type
RESEARCH_TEMPLATES = {
    "GEO_AUDIT": [
        {
            "id": "ai_visibility",
            "query": "What are the best {topic} in {location}?",
            "purpose": "Check if site/brand appears in AI recommendations"
        },
        {
            "id": "topic_authority",
            "query": "What should I know about {topic}? What are the most trusted sources?",
            "purpose": "Identify what AI considers authoritative for this topic"
        },
        {
            "id": "entity_recognition",
            "query": "{brand_or_entity} {topic} - what do people say?",
            "purpose": "Check if brand/entity is recognized by AI"
        }
    ],
    "SEO_AUDIT": [
        {
            "id": "search_landscape",
            "query": "Best {topic} guide - what information should a comprehensive article include?",
            "purpose": "Identify content expectations and gaps"
        },
        {
            "id": "people_also_ask",
            "query": "Common questions about {topic}",
            "purpose": "Find FAQ opportunities the page should address"
        }
    ],
    "INTERNAL_LINKING": [
        {
            "id": "related_topics",
            "query": "Topics related to {topic} that people also search for",
            "purpose": "Identify linking opportunities and topic clusters"
        }
    ],
    "CONTENT_QUALITY": [
        {
            "id": "content_benchmark",
            "query": "What makes a high-quality article about {topic}?",
            "purpose": "Benchmark content against AI quality expectations"
        }
    ]
}

# Default template for audit types without specific templates
DEFAULT_RESEARCH_TEMPLATE = [
    {
        "id": "general_context",
        "query": "What are the key aspects of {topic}?",
        "purpose": "General AI perspective on the topic"
    }
]


@dataclass
class ResearchResult:
    """Result from a single Perplexity query."""
    query_id: str
    query: str
    purpose: str
    response: str
    citations: List[str] = field(default_factory=list)
    mentions_brand: bool = False
    mentions_site: bool = False


@dataclass
class PageResearch:
    """Complete research context for a page."""
    filename: str
    topic: str
    location: Optional[str]
    brand: Optional[str]
    results: List[ResearchResult] = field(default_factory=list)
    research_time: float = 0.0
    
    def to_context_string(self) -> str:
        """Format research results for injection into LLM prompt."""
        if not self.results:
            return ""
        
        parts = [
            "=== AI SEARCH RESEARCH CONTEXT ===",
            f"Topic: {self.topic}",
            f"Brand/Entity: {self.brand or 'N/A'}",
            f"Location: {self.location or 'N/A'}",
            ""
        ]
        
        for result in self.results:
            parts.append(f"--- Research: {result.purpose} ---")
            parts.append(f"Query: \"{result.query}\"")
            parts.append(f"AI Response: {result.response[:2000]}")  # Cap at 2000 chars
            
            if result.citations:
                parts.append(f"Sources cited: {', '.join(result.citations[:5])}")
            
            if result.mentions_brand:
                parts.append("⚡ Brand/entity was MENTIONED in AI response")
            elif result.mentions_site:
                parts.append("⚡ Website was MENTIONED in AI response")
            else:
                parts.append("❌ Brand/site NOT mentioned in AI response")
            
            parts.append("")
        
        parts.append("=== END RESEARCH CONTEXT ===")
        return "\n".join(parts)
    
    def to_dict(self) -> dict:
        """Serialize to dictionary for JSON storage."""
        return {
            "filename": self.filename,
            "topic": self.topic,
            "location": self.location,
            "brand": self.brand,
            "research_time": self.research_time,
            "results": [
                {
                    "query_id": r.query_id,
                    "query": r.query,
                    "purpose": r.purpose,
                    "response": r.response,
                    "citations": r.citations,
                    "mentions_brand": r.mentions_brand,
                    "mentions_site": r.mentions_site
                }
                for r in self.results
            ]
        }


def extract_page_topic(text: str, max_length: int = 100) -> str:
    """
    Extract the main topic from page text.
    
    Uses first heading, title, or first meaningful sentence.
    """
    lines = text.strip().split('\n')
    
    # Try to find a heading or title-like line
    for line in lines[:20]:
        line = line.strip()
        # Skip very short or very long lines
        if len(line) < 5 or len(line) > 200:
            continue
        # Skip navigation-like content
        if any(skip in line.lower() for skip in ['cookie', 'privacy', 'skip to', 'menu', 'toggle', 'search']):
            continue
        # This looks like a title/heading
        if len(line) < 100 and not line.endswith('.'):
            return line[:max_length]
    
    # Fallback: first substantial line
    for line in lines[:30]:
        line = line.strip()
        if len(line) > 20:
            return line[:max_length]
    
    return "general content"


def extract_brand_entity(text: str, website: str) -> Optional[str]:
    """
    Extract the main brand or entity from page text.
    
    Looks for common patterns like "Dr. Name", company names, etc.
    """
    # Try to find doctor/professional names (common in medical sites)
    doc_patterns = [
        r'(?:Dr\.?\s+(?:Med\.?\s+)?)([\w\s]+?)(?:\s*[-–|,]|\s*$)',
        r'(?:Doctor|Medic|Prof\.?)\s+([\w\s]+?)(?:\s*[-–|,]|\s*$)',
    ]
    
    for pattern in doc_patterns:
        match = re.search(pattern, text[:3000])
        if match:
            name = match.group(1).strip()
            if 2 <= len(name.split()) <= 4:
                return name
    
    # Extract from website domain
    domain = website.replace('.ro', '').replace('.com', '').replace('.', ' ')
    return domain


def extract_location(text: str) -> Optional[str]:
    """Extract location mentions from text."""
    # Common Romanian cities
    cities = [
        'București', 'Bucuresti', 'Bucharest', 'Cluj', 'Timișoara', 'Timisoara',
        'Iași', 'Iasi', 'Constanța', 'Constanta', 'Craiova', 'Brașov', 'Brasov',
        'Galați', 'Galati', 'Ploiești', 'Ploiesti', 'Oradea', 'Sibiu', 'Arad',
        'Pitești', 'Pitesti', 'Baia Mare', 'Suceava', 'Târgu Mureș'
    ]
    
    text_lower = text.lower()
    for city in cities:
        if city.lower() in text_lower:
            return city
    
    # Try "Romania" as fallback
    if 'romania' in text_lower or 'românia' in text_lower:
        return "Romania"
    
    return None


class PerplexityResearcher:
    """
    Async Perplexity research engine.
    
    Queries Perplexity API to gather real-world AI search context
    for enriching page audits.
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = PERPLEXITY_MODEL,
        concurrency: int = MAX_CONCURRENT_QUERIES
    ):
        if not OPENAI_AVAILABLE:
            raise ImportError("openai package required for Perplexity research. pip install openai")
        
        self.api_key = api_key or os.getenv("PERPLEXITY_API_KEY")
        if not self.api_key:
            raise ValueError("PERPLEXITY_API_KEY not set. Add it to .env file.")
        
        self.model = model
        self.concurrency = concurrency
        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url="https://api.perplexity.ai"
        )
        
        # Simple cache: topic_hash -> response
        self._cache: Dict[str, str] = {}
    
    async def _query_perplexity(self, query: str) -> Tuple[str, List[str]]:
        """
        Send a single query to Perplexity API.
        
        Returns:
            Tuple of (response_text, citations_list)
        """
        cache_key = hashlib.md5(query.encode()).hexdigest()
        if cache_key in self._cache:
            logger.debug(f"Cache hit for query: {query[:50]}...")
            return self._cache[cache_key], []
        
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a research assistant. Provide factual, comprehensive answers. "
                            "Include specific names, brands, websites and sources when relevant. "
                            "Be concise but thorough."
                        )
                    },
                    {"role": "user", "content": query}
                ],
                max_tokens=1000,
                temperature=0.1
            )
            
            text = response.choices[0].message.content or ""
            
            # Extract citations if available (Perplexity returns them in response)
            citations = []
            if hasattr(response, 'citations') and response.citations:
                citations = response.citations
            
            # Cache the result
            self._cache[cache_key] = text
            
            return text, citations
            
        except Exception as e:
            logger.warning(f"Perplexity query failed: {e}")
            return f"[Research unavailable: {str(e)[:100]}]", []
    
    def _build_queries(
        self,
        audit_type: str,
        topic: str,
        brand: Optional[str],
        location: Optional[str],
        website: str
    ) -> List[dict]:
        """Build research queries from templates."""
        templates = RESEARCH_TEMPLATES.get(audit_type.upper(), DEFAULT_RESEARCH_TEMPLATE)
        
        queries = []
        for template in templates:
            query_text = template["query"].format(
                topic=topic,
                brand_or_entity=brand or website,
                location=location or "Romania",
                website=website
            )
            queries.append({
                "id": template["id"],
                "query": query_text,
                "purpose": template["purpose"]
            })
        
        return queries
    
    async def research_page(
        self,
        filename: str,
        page_text: str,
        website: str,
        audit_type: str
    ) -> PageResearch:
        """
        Run research for a single page.
        
        Args:
            filename: Page filename
            page_text: Converted text content
            website: Website domain
            audit_type: Type of audit being performed
        
        Returns:
            PageResearch with all research results
        """
        start_time = time.time()
        
        # Extract context from page
        topic = extract_page_topic(page_text)
        brand = extract_brand_entity(page_text, website)
        location = extract_location(page_text)
        
        logger.debug(f"Research for {filename}: topic='{topic}', brand='{brand}', location='{location}'")
        
        # Build queries
        queries = self._build_queries(audit_type, topic, brand, location, website)
        
        # Execute queries
        results = []
        for query_info in queries:
            response_text, citations = await self._query_perplexity(query_info["query"])
            
            # Check if brand/site is mentioned
            response_lower = response_text.lower()
            mentions_brand = bool(brand and brand.lower() in response_lower)
            mentions_site = website.lower() in response_lower
            
            results.append(ResearchResult(
                query_id=query_info["id"],
                query=query_info["query"],
                purpose=query_info["purpose"],
                response=response_text,
                citations=citations,
                mentions_brand=mentions_brand,
                mentions_site=mentions_site
            ))
            
            # Small delay between queries
            await asyncio.sleep(DELAY_BETWEEN_QUERIES)
        
        return PageResearch(
            filename=filename,
            topic=topic,
            location=location,
            brand=brand,
            results=results,
            research_time=time.time() - start_time
        )
    
    async def research_all_pages(
        self,
        input_dir: str,
        output_dir: str,
        website: str,
        audit_type: str,
        progress_callback=None
    ) -> Dict[str, PageResearch]:
        """
        Run research for all pages in input directory.
        
        Deduplicates by topic to avoid redundant queries.
        Saves results to output_dir as JSON files.
        
        Args:
            input_dir: Directory with .txt files
            output_dir: Directory to save research JSON files
            website: Website domain
            audit_type: Audit type for query selection
            progress_callback: Optional async callback(filename, done, total)
        
        Returns:
            Dict mapping filename -> PageResearch
        """
        os.makedirs(output_dir, exist_ok=True)
        
        # Get all text files
        txt_files = sorted([f for f in os.listdir(input_dir) if f.endswith('.txt')])
        
        if not txt_files:
            logger.warning(f"No .txt files found in {input_dir}")
            return {}
        
        logger.info(f"Starting Perplexity research for {len(txt_files)} pages")
        logger.info(f"Audit type: {audit_type}, Website: {website}")
        
        results = {}
        semaphore = asyncio.Semaphore(self.concurrency)
        
        # Track unique topics to avoid duplicate queries
        seen_topics = {}  # topic -> PageResearch
        
        async def process_page(filename: str, index: int) -> None:
            async with semaphore:
                # Check if research already exists on disk
                research_file = os.path.join(output_dir, filename.replace('.txt', '.research.json'))
                if os.path.exists(research_file):
                    logger.debug(f"Reusing existing research for {filename}")
                    try:
                        with open(research_file, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                        # Reconstruct PageResearch from saved data
                        research = PageResearch(
                            filename=data['filename'],
                            topic=data['topic'],
                            location=data.get('location'),
                            brand=data.get('brand'),
                            research_time=data.get('research_time', 0),
                            results=[
                                ResearchResult(**r) for r in data.get('results', [])
                            ]
                        )
                        results[filename] = research
                        if progress_callback:
                            await progress_callback(filename, index + 1, len(txt_files))
                        return
                    except Exception as e:
                        logger.warning(f"Failed to load cached research for {filename}: {e}")
                
                # Read page text to extract topic
                file_path = os.path.join(input_dir, filename)
                with open(file_path, 'r', encoding='utf-8') as f:
                    page_text = f.read()
                
                topic = extract_page_topic(page_text)
                
                # Check if we already researched this topic
                topic_key = topic.lower().strip()[:80]
                if topic_key in seen_topics:
                    logger.debug(f"Reusing research for duplicate topic: {topic[:50]}")
                    # Create a copy with this filename
                    original = seen_topics[topic_key]
                    research = PageResearch(
                        filename=filename,
                        topic=original.topic,
                        location=original.location,
                        brand=original.brand,
                        results=original.results,
                        research_time=0
                    )
                else:
                    # Run actual research
                    research = await self.research_page(filename, page_text, website, audit_type)
                    seen_topics[topic_key] = research
                
                results[filename] = research
                
                # Save to disk
                with open(research_file, 'w', encoding='utf-8') as f:
                    json.dump(research.to_dict(), f, indent=2, ensure_ascii=False)
                
                if progress_callback:
                    await progress_callback(filename, index + 1, len(txt_files))
        
        # Process all pages
        tasks = [process_page(f, i) for i, f in enumerate(txt_files)]
        await asyncio.gather(*tasks, return_exceptions=True)
        
        # Summary
        total_brand_mentions = sum(
            1 for r in results.values()
            if any(rr.mentions_brand or rr.mentions_site for rr in r.results)
        )
        unique_topics = len(seen_topics)
        
        logger.info(
            f"Research complete: {len(results)} pages, "
            f"{unique_topics} unique topics, "
            f"{total_brand_mentions} pages with brand/site mentions"
        )
        
        return results
    
    async def close(self):
        """Close the API client."""
        if hasattr(self.client, 'close'):
            await self.client.close()


def load_research_context(research_dir: str, filename: str) -> Optional[str]:
    """
    Load pre-computed research context for a page.
    
    Args:
        research_dir: Directory containing .research.json files
        filename: Original .txt filename
    
    Returns:
        Formatted research context string, or None if not available
    """
    research_file = os.path.join(research_dir, filename.replace('.txt', '.research.json'))
    
    if not os.path.exists(research_file):
        return None
    
    try:
        with open(research_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        research = PageResearch(
            filename=data['filename'],
            topic=data['topic'],
            location=data.get('location'),
            brand=data.get('brand'),
            results=[ResearchResult(**r) for r in data.get('results', [])]
        )
        
        return research.to_context_string()
        
    except Exception as e:
        logger.warning(f"Failed to load research context for {filename}: {e}")
        return None


def is_perplexity_available() -> bool:
    """Check if Perplexity API key is configured."""
    return bool(os.getenv("PERPLEXITY_API_KEY"))
