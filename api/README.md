# Website LLM Analyzer - Web Interface

A modern web interface for running website audits using Large Language Models. This FastAPI application wraps the existing CLI pipeline, providing a user-friendly interface for non-technical users.

## Features

- **Dashboard**: View all audits with status badges, quick stats, and recent activity
- **Real-time Progress**: Server-Sent Events (SSE) for live status updates and log streaming
- **Multiple Audit Types**: 15+ audit types including SEO, accessibility, content quality, and more
- **Multi-Provider Support**: Anthropic Claude, OpenAI GPT-4, and Mistral AI
- **Export Reports**: Download Excel reports with detailed analysis results
- **Score Visualization**: Interactive charts showing score distributions
- **Rate Limiting**: Configurable concurrent audit limits
- **Docker Ready**: Easy deployment with Docker and docker-compose

## Quick Start

### Option 1: Docker (Recommended)

1. **Clone and configure:**
   ```bash
   git clone <repository>
   cd website_llm_analyzer_refactored
   cp .env.example .env
   # Edit .env with your API keys
   ```

2. **Start with Docker Compose:**
   ```bash
   docker-compose up -d
   ```

3. **Access the web interface:**
   Open http://localhost:8000 in your browser

### Option 2: Local Development

1. **Install dependencies:**
   ```bash
   # Create virtual environment
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   
   # Install base requirements
   pip install -r requirements.txt
   
   # Install API requirements
   pip install -r api/requirements.txt
   ```

2. **Configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env with your API keys
   ```

3. **Run the application:**
   ```bash
   uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
   ```

4. **Access the web interface:**
   Open http://localhost:8000 in your browser

## API Endpoints

### Audits

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/audits` | Create a new audit |
| GET | `/api/audits` | List all audits (paginated) |
| GET | `/api/audits/{id}` | Get audit details |
| DELETE | `/api/audits/{id}` | Delete an audit |
| GET | `/api/audits/{id}/results` | Get audit results |
| GET | `/api/audits/{id}/export` | Download Excel report |
| GET | `/api/audits/{id}/stream` | SSE stream for real-time updates |

### System

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/audit-types` | List available audit types |
| GET | `/api/health` | Health check and system status |

## Web Pages

| URL | Description |
|-----|-------------|
| `/` | Dashboard with recent audits and stats |
| `/new` | Create a new audit form |
| `/audits/{id}` | Audit detail page with real-time updates |
| `/audits/{id}/results` | Results table with pagination |

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `ANTHROPIC_API_KEY` | Anthropic Claude API key | - |
| `OPENAI_API_KEY` | OpenAI API key | - |
| `MISTRAL_API_KEY` | Mistral AI API key | - |
| `DEFAULT_PROVIDER` | Default LLM provider | `anthropic` |
| `DEFAULT_MODEL` | Default model for provider | `claude-sonnet-4-20250514` |
| `PORT` | Web server port | `8000` |
| `MAX_CONCURRENT_AUDITS` | Max simultaneous audits | `3` |
| `AUTO_CLEANUP_DAYS` | Delete old data after N days | `30` |
| `AUTH_USERNAME` | Basic auth username (optional) | - |
| `AUTH_PASSWORD` | Basic auth password (optional) | - |

### Available Audit Types

- `seo_audit` - Search Engine Optimization analysis
- `geo_audit` - Generative Engine Optimization for AI search
- `accessibility_audit` - WCAG accessibility compliance
- `content_quality` - Content quality assessment
- `ux_content` - User experience content review
- `spelling_grammar` - Spelling and grammar check
- `brand_voice` - Brand voice consistency
- `legal_gdpr` - Legal and GDPR compliance
- `translation_quality` - Translation quality analysis
- `e_commerce` - E-commerce optimization
- `competitor_analysis` - Competitor comparison
- `greenwashing` - Greenwashing detection
- `advertisment` - Advertisement content review
- `relevancy_audit` - Content relevancy assessment
- `kantar` - Kantar brand metrics

## Architecture

```
api/
├── main.py              # FastAPI application entry point
├── requirements.txt     # Python dependencies
├── models/
│   ├── database.py      # SQLAlchemy models (SQLite)
│   └── schemas.py       # Pydantic validation schemas
├── routes/
│   ├── audits.py        # Audit CRUD endpoints
│   ├── results.py       # Results and streaming endpoints
│   └── health.py        # Health check endpoint
├── workers/
│   └── audit_worker.py  # Background task runner
└── templates/
    ├── base.html        # Base template with navigation
    ├── index.html       # Dashboard
    ├── new_audit.html   # New audit form
    ├── audit_detail.html # Audit details with SSE
    ├── results.html     # Results table
    └── partials/        # HTMX partial templates
```

## Development

### Running Tests

```bash
pytest api/tests/ -v
```

### Code Style

```bash
# Format code
black api/
isort api/

# Lint
flake8 api/
mypy api/
```

### Database Migrations

The application uses SQLite with automatic schema creation. The database file is stored in the `data/` directory.

To reset the database:
```bash
rm data/analyzer.db
# Restart the application
```

## Troubleshooting

### "No LLM providers configured"

Make sure at least one API key is set in your `.env` file:
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `MISTRAL_API_KEY`

### "Maximum concurrent audits reached"

Wait for running audits to complete, or increase `MAX_CONCURRENT_AUDITS` in your configuration.

### Chrome/Selenium errors

For Docker deployments, Chrome is included in the image. For local development:
```bash
# Ubuntu/Debian
sudo apt-get install google-chrome-stable

# macOS
brew install --cask google-chrome
```

### Slow analysis

- Use "Direct Mode" for small sites (< 50 pages)
- Use "Batch Mode" for larger sites
- Consider using a faster model (e.g., Claude Haiku for initial scans)

## License

[Your License Here]

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request
