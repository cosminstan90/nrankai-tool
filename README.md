# Website LLM Analyzer

A comprehensive tool for analyzing websites using Large Language Models (LLMs). Supports automated audits for SEO, GEO (Generative Engine Optimization), accessibility, content quality, and more.

## ✨ Features

- **15+ Audit Types**: SEO, GEO, accessibility, GDPR compliance, content quality, and more
- **Multi-Provider Support**: Works with Anthropic Claude, OpenAI GPT-4, and Mistral AI
- **CLI-First Design**: Flexible command-line arguments with .env defaults
- **Batch Processing**: Efficient bulk processing via LLM batch APIs
- **Automated Pipeline**: Complete workflow from scraping to reporting
- **Excel Reports**: Comprehensive score aggregation and bucket analysis

## 🚀 Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
# Copy template
cp .env.example .env

# Edit .env and add your API key
nano .env
```

**Required in .env:**
```env
WEBSITE=example.com
SITEMAP=https://www.example.com/sitemap.xml
QUESTION=SEO_AUDIT
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

### 3. Run the Pipeline

```bash
# Option 1: Run full pipeline with .env defaults
python main.py

# Option 2: Override with CLI arguments
python main.py --website yoursite.com --audit GEO_AUDIT --sitemap https://yoursite.com/sitemap.xml
```

## 📖 Documentation

- **[QUICK_START.md](QUICK_START.md)** - Get started in 5 minutes
- **[CLI_USAGE_GUIDE.md](CLI_USAGE_GUIDE.md)** - Comprehensive CLI documentation with examples
- **[IMPLEMENTATION_SUMMARY.md](IMPLEMENTATION_SUMMARY.md)** - Technical implementation details
- **[REFACTORING_SUMMARY.md](REFACTORING_SUMMARY.md)** - Original refactoring notes

## 🎯 Available Audit Types

Run any of these audits by setting `QUESTION` in `.env` or using `--audit` CLI argument:

| Audit Type | Description |
|------------|-------------|
| `SEO_AUDIT` | Search Engine Optimization analysis |
| `GEO_AUDIT` | Generative Engine Optimization (AI search) |
| `ACCESSIBILITY_AUDIT` | WCAG compliance and accessibility |
| `CONTENT_QUALITY` | Content depth and quality assessment |
| `UX_CONTENT` | User experience and readability |
| `LEGAL_GDPR` | GDPR compliance check |
| `BRAND_VOICE` | Brand consistency analysis |
| `E_COMMERCE` | E-commerce optimization |
| `TRANSLATION_QUALITY` | Translation accuracy audit |
| `COMPETITOR_ANALYSIS` | Competitive analysis |
| And more... (check `prompts/` directory) |

## 🛠️ Usage Examples

### Run Complete Pipeline

```bash
# Using .env defaults
python main.py

# Override with CLI args
python main.py --website example.com --audit SEO_AUDIT --sitemap https://example.com/sitemap.xml
```

### Run Specific Steps

```bash
# Run only scraping and conversion
python main.py --website example.com --steps scrape,convert --sitemap URL

# Run only analysis
python main.py --website example.com --audit GEO_AUDIT --steps analyze

# Run only scoring
python main.py --website example.com --steps score
```

### Run Individual Scripts

```bash
# 1. Scrape website
python web_scraper.py --website example.com --sitemap https://example.com/sitemap.xml

# 2. Convert HTML to LLM format
python html2llm_converter.py --website example.com

# 3. Analyze with LLM
python website_llm_analyzer.py --website example.com --audit SEO_AUDIT

# 4. Generate Excel report
python determine_score.py --root-dir ./example.com
```

### Advanced Usage

```bash
# Dry-run mode (test without API submission)
python website_llm_analyzer.py --audit SEO_AUDIT --dry-run

# Force specific provider and model
python website_llm_analyzer.py --audit GEO_AUDIT --provider anthropic --model claude-sonnet-4-20250514

# Custom character limit
python website_llm_analyzer.py --audit CONTENT_QUALITY --max-chars 50000

# Disable proxy
python web_scraper.py --no-proxy

# Custom delay range for scraping
python web_scraper.py --delay 2.0-4.0
```

## 📁 Project Structure

```
website_llm_analyzer/
├── config.py                    # Configuration management with CLI override support
├── main.py                      # Pipeline orchestrator (NEW)
├── web_scraper.py              # Website scraping with Selenium
├── html2llm_converter.py       # HTML to LLM text conversion
├── website_llm_analyzer.py     # LLM batch processing
├── determine_score.py          # Score aggregation and Excel reporting
├── prompt_loader.py            # YAML-based prompt management
├── monitor_completion_LLM_batch.py  # Batch job monitoring
├── requirements.txt            # Python dependencies
├── .env.example               # Environment template
└── prompts/                    # Audit prompt definitions (YAML)
    ├── seo_audit.yaml
    ├── geo_audit.yaml
    ├── accessibility_audit.yaml
    └── ...
```

## 🔄 Pipeline Workflow

```
1. SCRAPE      → Download HTML pages from sitemap
2. CONVERT     → Transform HTML to LLM-optimized text
3. ANALYZE     → Process with LLM batch API
4. SCORE       → Aggregate results into Excel report
```

## 🎨 Configuration Options

### Environment Variables (.env)

All scripts use `.env` as the default configuration source:

```env
WEBSITE=example.com
SITEMAP=https://www.example.com/sitemap.xml
QUESTION=SEO_AUDIT
ANTHROPIC_API_KEY=sk-ant-...
```

### CLI Arguments (Override .env)

Every script accepts CLI arguments to override `.env` defaults:

```bash
# Override any configuration at runtime
python main.py --website different.com --audit GEO_AUDIT --provider openai
```

**Priority:** CLI arguments > Environment variables

## 📊 Output

### Directory Structure

```
example.com/
├── input_html/              # Scraped HTML files
├── input_llm/               # Converted text files
├── output_seo_audit/        # SEO audit results (JSON)
├── output_geo_audit/        # GEO audit results (JSON)
└── example.com_anthropic.jsonl  # Batch request file

audit_scores.xlsx            # Final Excel report with score buckets
```

### Excel Report

The `audit_scores.xlsx` file contains:
- Multiple sheets (one per audit type)
- Score distribution across buckets
- Site-by-site comparison
- Total file counts

## 🔧 Requirements

- Python 3.8+
- Chrome/Chromium (for web scraping)
- API key from at least one provider:
  - Anthropic (Claude) - Recommended
  - OpenAI (GPT-4)
  - Mistral AI

## 💡 Tips & Best Practices

1. **Start with dry-run**: Test batch creation before submitting
   ```bash
   python website_llm_analyzer.py --audit SEO_AUDIT --dry-run
   ```

2. **Use shorter delays for internal sites**: Speed up scraping
   ```bash
   python web_scraper.py --delay 0.5-1.0
   ```

3. **Process multiple sites**: Run audits in sequence
   ```bash
   for site in site1 site2 site3; do
     python main.py --website $site.com --audit GEO_AUDIT --sitemap https://$site.com/sitemap.xml
   done
   ```

4. **Multiple audits on same content**: Reuse scraped data
   ```bash
   python main.py --website example.com --audit SEO_AUDIT --sitemap URL
   python main.py --website example.com --audit GEO_AUDIT --steps analyze
   python main.py --website example.com --audit ACCESSIBILITY_AUDIT --steps analyze
   ```

## 📝 Help & Support

For detailed documentation:
```bash
python main.py --help
python web_scraper.py --help
python website_llm_analyzer.py --help
```

See documentation files for comprehensive guides:
- [QUICK_START.md](QUICK_START.md)
- [CLI_USAGE_GUIDE.md](CLI_USAGE_GUIDE.md)
- [IMPLEMENTATION_SUMMARY.md](IMPLEMENTATION_SUMMARY.md)

## 🎖️ Credits

**Author:** Cosmin  
**Created:** 2026-01-23  
**CLI Enhancement:** 2026-02-10

---

**Tip:** Run `python main.py --help` to see all available options!
