# Website LLM Analyzer - CLI Usage Guide

## Overview

All scripts now support CLI arguments that override `.env` defaults. This allows you to:
- Run different audits without editing `.env`
- Process multiple websites in sequence
- Customize behavior per execution
- Use the tool in automation/CI pipelines

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run full pipeline with CLI arguments
python main.py --website example.com --audit SEO_AUDIT --sitemap https://example.com/sitemap.xml

# Run individual scripts
python web_scraper.py --website example.com --sitemap https://example.com/sitemap.xml
python html2llm_converter.py --website example.com
python website_llm_analyzer.py --website example.com --audit GEO_AUDIT
python determine_score.py --root-dir ./example.com --output results.xlsx
```

## Configuration Hierarchy

**Priority order:** CLI arguments > Environment variables (.env)

- ✅ `.env` file remains as defaults
- ✅ CLI arguments override `.env` values
- ✅ All existing behavior preserved when running without CLI args

## Script-Specific Arguments

### 1. web_scraper.py

Downloads HTML pages from website based on sitemap.

```bash
python web_scraper.py [OPTIONS]

Options:
  --website DOMAIN          Target website (overrides WEBSITE in .env)
  --sitemap URL            Sitemap URL (overrides SITEMAP in .env)
  --output-dir PATH        Custom output directory (default: {website}/input_html)
  --no-proxy               Disable proxy even if configured
  --delay MIN-MAX          Delay range in seconds (default: "1.5-3.5")
  -h, --help               Show help message

Examples:
  # Use .env defaults
  python web_scraper.py
  
  # Override website and sitemap
  python web_scraper.py --website example.com --sitemap https://example.com/sitemap.xml
  
  # Disable proxy
  python web_scraper.py --no-proxy
  
  # Custom delay range (faster scraping)
  python web_scraper.py --delay 0.5-1.5
```

### 2. html2llm_converter.py

Converts HTML files to LLM-optimized text format.

```bash
python html2llm_converter.py [OPTIONS]

Options:
  --website DOMAIN         Target website (overrides WEBSITE in .env)
  --input-dir PATH         Input directory with HTML files
  --output-dir PATH        Output directory for text files
  -h, --help               Show help message

Examples:
  # Use .env defaults
  python html2llm_converter.py
  
  # Override website
  python html2llm_converter.py --website example.com
  
  # Custom paths
  python html2llm_converter.py --input-dir ./raw_html --output-dir ./processed_text
```

### 3. website_llm_analyzer.py

Creates and processes LLM batch requests.

```bash
python website_llm_analyzer.py [OPTIONS]

Options:
  --website DOMAIN         Target website (overrides WEBSITE in .env)
  --audit TYPE             Audit type (overrides QUESTION in .env)
  --question TYPE          Same as --audit (alias)
  --provider PROVIDER      Force provider: anthropic/openai/mistral
  --model MODEL_NAME       Override model name
  --max-chars NUM          Max characters per request (default: 30000)
  --dry-run                Create batch file but don't submit
  -h, --help               Show help message

Available Audit Types:
  SEO_AUDIT                Search Engine Optimization audit
  GEO_AUDIT                Generative Engine Optimization audit
  ACCESSIBILITY_AUDIT      WCAG accessibility compliance
  CONTENT_QUALITY          Content depth and quality
  UX_CONTENT               User experience and readability
  LEGAL_GDPR               GDPR compliance check
  BRAND_VOICE              Brand consistency analysis
  E_COMMERCE               E-commerce optimization
  TRANSLATION_QUALITY      Translation accuracy
  COMPETITOR_ANALYSIS      Competitive analysis
  (and more - see prompts/ directory)

Examples:
  # Use .env defaults
  python website_llm_analyzer.py
  
  # Run SEO audit
  python website_llm_analyzer.py --audit SEO_AUDIT
  
  # Force specific provider and model
  python website_llm_analyzer.py --audit GEO_AUDIT --provider anthropic --model claude-sonnet-4-20250514
  
  # Increase character limit for long pages
  python website_llm_analyzer.py --audit CONTENT_QUALITY --max-chars 50000
  
  # Test without submitting
  python website_llm_analyzer.py --audit SEO_AUDIT --dry-run
```

### 4. determine_score.py

Aggregates audit results into Excel report.

```bash
python determine_score.py [OPTIONS]

Options:
  --root-dir PATH          Directory to scan for audit results (default: .)
  --output FILENAME        Output Excel filename (default: audit_scores.xlsx)
  -h, --help               Show help message

Examples:
  # Scan current directory
  python determine_score.py
  
  # Scan specific website directory
  python determine_score.py --root-dir ./example.com
  
  # Custom output filename
  python determine_score.py --output my_report.xlsx
  
  # Process multiple sites
  python determine_score.py --root-dir ./all_sites --output combined_results.xlsx
```

### 5. main.py (Pipeline Orchestrator)

Runs the complete pipeline or specific steps.

```bash
python main.py [OPTIONS]

Options:
  --website DOMAIN         Target website
  --audit TYPE             Audit type
  --steps STEPS            Steps to run (comma-separated, default: all)
  --sitemap URL            Sitemap URL (required for scrape)
  --provider PROVIDER      LLM provider
  --model MODEL_NAME       Model name
  --no-proxy               Disable proxy
  --delay MIN-MAX          Scraping delay range
  --max-chars NUM          Max chars per LLM request
  --dry-run                Batch creation only (no submission)
  --score-output FILE      Excel output filename
  -h, --help               Show help message

Available Steps:
  scrape                   Download HTML pages
  convert                  Convert HTML to text
  analyze                  Create and process LLM batches
  score                    Generate Excel report

Examples:
  # Full pipeline
  python main.py --website example.com --audit SEO_AUDIT --sitemap https://example.com/sitemap.xml
  
  # Run specific steps only
  python main.py --website example.com --audit GEO_AUDIT --steps scrape,convert,analyze
  
  # Skip scraping (use existing HTML)
  python main.py --website example.com --audit ACCESSIBILITY_AUDIT --steps convert,analyze,score
  
  # Process multiple audits sequentially
  python main.py --website example.com --audit SEO_AUDIT --steps analyze,score
  python main.py --website example.com --audit GEO_AUDIT --steps analyze,score
  
  # Custom provider with dry run
  python main.py --website example.com --audit CONTENT_QUALITY --provider openai --dry-run
```

## Common Workflows

### Workflow 1: Quick Single-Site Audit

```bash
# Complete pipeline in one command
python main.py \
  --website example.com \
  --audit SEO_AUDIT \
  --sitemap https://example.com/sitemap.xml
```

### Workflow 2: Multi-Site Analysis

```bash
# Process multiple sites with same audit
for site in site1.com site2.com site3.com; do
  python main.py \
    --website $site \
    --audit GEO_AUDIT \
    --sitemap https://$site/sitemap.xml
done

# Generate combined report
python determine_score.py --root-dir . --output all_sites_report.xlsx
```

### Workflow 3: Multiple Audits on Same Site

```bash
# Run different audits on the same scraped content
python main.py --website example.com --audit SEO_AUDIT --sitemap https://example.com/sitemap.xml

# Reuse scraped data for additional audits
python main.py --website example.com --audit GEO_AUDIT --steps analyze
python main.py --website example.com --audit ACCESSIBILITY_AUDIT --steps analyze
python main.py --website example.com --audit CONTENT_QUALITY --steps analyze

# Generate combined report
python determine_score.py --root-dir ./example.com
```

### Workflow 4: Development/Testing

```bash
# Test scraping without analysis
python main.py --website example.com --sitemap https://example.com/sitemap.xml --steps scrape

# Test conversion
python main.py --website example.com --steps convert

# Test batch creation without submission
python main.py --website example.com --audit SEO_AUDIT --steps analyze --dry-run

# Quick score check
python determine_score.py --root-dir ./example.com
```

### Workflow 5: Incremental Processing

```bash
# Day 1: Scrape and convert
python main.py --website example.com --sitemap https://example.com/sitemap.xml --steps scrape,convert

# Day 2: Analyze with LLM (after content review)
python main.py --website example.com --audit SEO_AUDIT --steps analyze

# Day 3: Generate report (after batch completes)
python main.py --website example.com --steps score
```

## Environment Variables (.env)

The `.env` file provides defaults that CLI arguments can override:

```env
# Website Configuration
WEBSITE=example.com
SITEMAP=https://www.example.com/sitemap.xml

# Audit Configuration
QUESTION=SEO_AUDIT

# LLM Provider (set at least one)
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
MISTRAL_API_KEY=...

# Proxy (optional)
PROXY_HOST=proxy.company.com
PROXY_PORT=8080
```

## Tips & Best Practices

1. **Start with dry-run**: Test batch creation before submitting expensive LLM requests
   ```bash
   python website_llm_analyzer.py --audit SEO_AUDIT --dry-run
   ```

2. **Use shorter delays for internal sites**: Speed up scraping for sites without rate limiting
   ```bash
   python web_scraper.py --delay 0.5-1.0
   ```

3. **Increase max-chars for detailed audits**: Some audits benefit from more context
   ```bash
   python website_llm_analyzer.py --audit CONTENT_QUALITY --max-chars 50000
   ```

4. **Disable proxy for local testing**: Skip proxy for development
   ```bash
   python web_scraper.py --no-proxy
   ```

5. **Process multiple sites in parallel**: Use background jobs or process managers
   ```bash
   python main.py --website site1.com --audit SEO_AUDIT &
   python main.py --website site2.com --audit SEO_AUDIT &
   wait
   ```

## Help & Documentation

Get help for any script:
```bash
python web_scraper.py --help
python html2llm_converter.py --help
python website_llm_analyzer.py --help
python determine_score.py --help
python main.py --help
```

## Migration Guide

**For existing users:** Your workflows remain unchanged!

```bash
# Old way (still works)
python web_scraper.py
python html2llm_converter.py
python website_llm_analyzer.py
python determine_score.py

# New way (with flexibility)
python main.py --website example.com --audit SEO_AUDIT --sitemap https://example.com/sitemap.xml
```

The `.env` file is still the primary configuration source. CLI arguments simply provide overrides when needed.
