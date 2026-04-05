# Quick Start - GEO Visibility Monitor

## Installation (v1.5.0)

### 1. Extract & Setup

```bash
# Extract archive
unzip website_llm_analyzer_v1_5_0_geo_monitor.zip
cd website_llm_analyzer_v1_5_0

# Install dependencies (if needed)
pip install -r requirements.txt
```

### 2. Configure API Keys

```bash
# Copy environment template
cp .env.example .env

# Edit .env and add your API keys
nano .env  # or your preferred editor
```

**Required for GEO Monitor (at least one):**
```env
OPENAI_API_KEY=sk-...           # For ChatGPT monitoring
ANTHROPIC_API_KEY=sk-ant-...    # For Claude monitoring
PERPLEXITY_API_KEY=pplx-...     # For Perplexity monitoring
```

### 3. Start Server

```bash
# From project root
python -m uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

**Or using the main.py directly:**
```bash
python api/main.py
```

### 4. Access Application

Open browser: `http://localhost:8000`

Navigate to: **🌐 GEO Monitor** (in navigation bar)

## First Project Setup

### Step-by-Step Example

1. **Click "Create New Project"**

2. **Fill Project Details:**
   - Name: `My Brand GEO Monitor`
   - Website: `example.com`
   - Language: `English` or `Romanian`

3. **Add Brand Keywords:**
   - Type keyword and press Enter
   - Example: `Example`, `Example.com`, `Example Company`
   - Minimum: 1 keyword

4. **Add Test Queries:**
   - Click "✨ Generate Suggested Queries" for auto-generation
   - Or manually enter (one per line):
   ```
   What are the best companies like Example?
   Reviews of Example
   Is Example a good choice?
   Example vs competitors
   ```

5. **Select Providers:**
   - ✅ ChatGPT (if OPENAI_API_KEY configured)
   - ✅ Claude (if ANTHROPIC_API_KEY configured)
   - ✅ Perplexity (if PERPLEXITY_API_KEY configured)

6. **Click "Create Project"**

## Running Your First Scan

### Quick Scan

1. **From Dashboard:**
   - Click "Run Scan" on your project card

2. **Wait for Completion:**
   - Status: `pending` → `running` → `completed`
   - Duration: ~2-3 minutes for 10 queries × 3 providers
   - Progress: Shows "X/Y queries" in real-time

3. **View Results:**
   - Click on project card to see detailed results
   - Review visibility score (0-100%)
   - Check provider breakdown
   - Analyze query-by-query results

### Understanding Results

**Visibility Score:**
- 🟢 ≥70% = Excellent visibility
- 🟡 40-69% = Moderate visibility (needs improvement)
- 🔴 <40% = Poor visibility (urgent optimization needed)

**Result Indicators:**
- ✅ = Mentioned (brand keyword found)
- 🔗 = Cited (URL included)
- ❌ = Not found
- ⚠️ = Error

**Click any cell** to see:
- Full AI response
- Context snippet with mention
- Sentiment analysis
- Position classification

## Cost Estimation

### Per Scan Costs

**Small Project (10 queries × 3 providers = 30 calls):**
- Cost-effective models: ~$0.03
- Premium models: ~$0.40

**Monthly (4 scans/project/month):**
- Cost-effective: ~$0.12/project/month
- Premium: ~$1.60/project/month

**Recommended:** Use cost-effective models (gpt-4o-mini, claude-haiku, perplexity-sonar) for routine monitoring.

## Troubleshooting

### "No providers available"
- **Solution:** Add at least one API key to `.env`
- Check: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `PERPLEXITY_API_KEY`
- Restart server after adding keys

### Scan stuck in "running"
- **Check server logs** for API errors
- **Verify API keys** are correct and have quota
- **Wait 5 minutes** - some scans take longer with rate limits

### Low visibility scores
- **Normal** for new/small brands
- **Action:**
  1. Add more keyword variations
  2. Test with branded queries first
  3. Create more AI-optimized content
  4. Monitor competitors for comparison

### Provider-specific errors
- **OpenAI rate limit:** Wait 60 seconds and retry
- **Anthropic rate limit:** Check billing and quota
- **Perplexity error:** Verify API key is active

## Best Practices

### Keyword Selection
✅ **Do:**
- Include brand name variations
- Add domain name (with and without TLD)
- Include common misspellings
- Add product/service names

❌ **Don't:**
- Use generic industry terms only
- Include competitor names
- Add too many (keep under 10)

### Query Design
✅ **Do:**
- Mix branded and generic queries
- Test comparison queries ("X vs Y")
- Include "best [category]" queries
- Add review/opinion queries
- Use natural language

❌ **Don't:**
- Only use branded queries (inflates scores)
- Use overly technical queries
- Include your keywords in the query

### Scan Frequency
- **New projects:** Daily for first week
- **Established:** Weekly or bi-weekly
- **After content updates:** Immediate
- **Competitor launches:** More frequent

## Advanced Usage

### Monitoring Trends

1. Run scans regularly (weekly recommended)
2. Review trend chart in detail view
3. Look for:
   - ↗️ Improvements after content updates
   - ↘️ Drops (investigate causes)
   - Provider-specific patterns

### Competitive Analysis

Create separate projects for:
- Your brand
- Top 3 competitors
- Compare visibility scores
- Identify gap opportunities

### ROI Reporting

Use for client reports:
1. Screenshot visibility score trends
2. Show before/after optimization
3. Highlight provider-specific wins
4. Export query-by-query results

## Support

### Documentation
- Full guide: `GEO_MONITOR_README.md`
- Changelog: `CHANGELOG_v1_5_0_GEO_MONITOR.md`
- Main docs: `README.md`

### Common Issues
- Check server console for errors
- Verify `.env` configuration
- Ensure Python dependencies installed
- Clear browser cache if UI issues

### Getting Help
1. Check server logs: `uvicorn` console output
2. Check browser console: F12 → Console tab
3. Review documentation files
4. Verify API key quotas

## Next Steps

After successful first scan:
1. ✅ Create projects for all key brands
2. ✅ Set up weekly scan reminders
3. ✅ Baseline current visibility scores
4. ✅ Plan GEO optimization strategies
5. ✅ Monitor trends over time

---

**Questions?** Check `GEO_MONITOR_README.md` for comprehensive documentation.

**Version:** 1.5.0  
**Last Updated:** February 20, 2026
