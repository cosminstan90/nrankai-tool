# Quick Start Guide

## Installation

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create .env file (copy from .env.example or create new)
cat > .env << 'EOF'
# Website Configuration
WEBSITE=example.com
SITEMAP=https://www.example.com/sitemap.xml

# Audit Type
QUESTION=SEO_AUDIT

# LLM Provider API Keys (set at least one)
ANTHROPIC_API_KEY=sk-ant-your-key-here
# OPENAI_API_KEY=sk-your-key-here
# MISTRAL_API_KEY=your-key-here

# Optional: Proxy Configuration
# PROXY_HOST=proxy.company.com
# PROXY_PORT=8080
EOF
```

## Quick Examples

### Example 1: Run Complete Pipeline

```bash
# Using .env defaults
python main.py

# Override website and audit type
python main.py --website yoursite.com --audit GEO_AUDIT --sitemap https://yoursite.com/sitemap.xml
```

### Example 2: Run Individual Steps

```bash
# 1. Scrape website
python web_scraper.py --website example.com --sitemap https://example.com/sitemap.xml

# 2. Convert HTML to text
python html2llm_converter.py --website example.com

# 3. Analyze with LLM
python website_llm_analyzer.py --website example.com --audit SEO_AUDIT

# 4. Generate report
python determine_score.py --root-dir ./example.com
```

### Example 3: Test Without Submitting

```bash
# Create batch file without submitting to LLM API
python website_llm_analyzer.py --audit SEO_AUDIT --dry-run
```

### Example 4: Multiple Audits

```bash
# Run different audits on same website
python main.py --website example.com --audit SEO_AUDIT --sitemap https://example.com/sitemap.xml
python main.py --website example.com --audit GEO_AUDIT --steps analyze
python main.py --website example.com --audit ACCESSIBILITY_AUDIT --steps analyze

# Generate combined report
python determine_score.py --root-dir ./example.com
```

## Available Audit Types

Run `python website_llm_analyzer.py --help` to see all available audit types, or check the `prompts/` directory:

- **SEO_AUDIT** - Search Engine Optimization analysis
- **GEO_AUDIT** - Generative Engine Optimization (AI search)
- **ACCESSIBILITY_AUDIT** - WCAG compliance check
- **CONTENT_QUALITY** - Content depth and quality
- **UX_CONTENT** - User experience and readability
- **LEGAL_GDPR** - GDPR compliance audit
- **BRAND_VOICE** - Brand consistency analysis
- **E_COMMERCE** - E-commerce optimization
- **TRANSLATION_QUALITY** - Translation accuracy
- **COMPETITOR_ANALYSIS** - Competitive analysis
- And more...

## Common Options

### All Scripts Accept

- `--website DOMAIN` - Target website domain
- `-h, --help` - Show detailed help

### main.py Options

- `--steps scrape,convert,analyze,score` - Run specific steps
- `--audit TYPE` - Audit type
- `--provider anthropic|openai|mistral` - Force specific LLM provider
- `--dry-run` - Test without submitting to API
- `--max-chars N` - Max characters per LLM request

## Directory Structure

After running the pipeline:

```
example.com/
├── input_html/              # Scraped HTML files
├── input_llm/               # Converted text files
├── output_seo_audit/        # SEO audit results (JSON)
├── output_geo_audit/        # GEO audit results (JSON)
└── example.com_anthropic.jsonl  # Batch request file

audit_scores.xlsx            # Final Excel report
```

## Help & Documentation

```bash
# Get help for any script
python web_scraper.py --help
python html2llm_converter.py --help
python website_llm_analyzer.py --help
python determine_score.py --help
python main.py --help
```

## Troubleshooting

### Issue: "WEBSITE environment variable is not set"
**Solution**: Either set in `.env` or use `--website` argument

### Issue: "No API key found"
**Solution**: Set at least one API key in `.env`:
```bash
ANTHROPIC_API_KEY=sk-ant-...
```

### Issue: "No URLs found. Exiting."
**Solution**: Verify sitemap URL is correct and accessible

### Issue: Proxy connection fails
**Solution**: Use `--no-proxy` flag to bypass proxy:
```bash
python web_scraper.py --no-proxy
```

## Next Steps

1. ✅ Install dependencies: `pip install -r requirements.txt`
2. ✅ Create `.env` with your API keys
3. ✅ Test with dry-run: `python main.py --audit SEO_AUDIT --dry-run`
4. ✅ Run full pipeline: `python main.py`
5. ✅ Check results in Excel report

For detailed documentation, see:
- **CLI_USAGE_GUIDE.md** - Comprehensive CLI documentation
- **IMPLEMENTATION_SUMMARY.md** - Technical implementation details
