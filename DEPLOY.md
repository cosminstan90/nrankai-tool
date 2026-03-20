# GEO Tool — CloudPanel Deployment Guide
## Target: `app.nrankai.com` (or any subdomain)

---

## 1. Server prerequisites (run as root)

```bash
# Install Python 3.12 (more stable than 3.14 on Ubuntu)
add-apt-repository ppa:deadsnakes/ppa -y
apt update
apt install -y python3.12 python3.12-venv python3.12-dev build-essential
```

---

## 2. Create the site in CloudPanel

1. CloudPanel → **Sites → Add Site**
2. Choose **"Python"** (or "Custom") application
3. Domain: `app.nrankai.com`
4. CloudPanel will create a system user (e.g. `nrankai`) and home at `/home/nrankai/`
5. Enable **SSL** via Let's Encrypt in CloudPanel after DNS is pointed

---

## 3. Clone the repo & install the app

```bash
# SSH into the server as the site user
su - nrankai

# Add your SSH deploy key to GitHub (one-time setup):
ssh-keygen -t ed25519 -C "server@app.nrankai.com" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
# → Copy this and add it to GitHub: Settings → SSH Keys → New SSH Key

# Test the connection
ssh -T git@github.com

# Clone the repo
cd /home/nrankai/htdocs/app.nrankai.com
git clone git@github.com:cosminstan90/nrankai-tool.git .

# Create virtualenv and install dependencies
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r api/requirements.txt
```

---

## 4. Configure environment variables

```bash
cp .env.example .env
nano .env
```

Fill in your `.env`:
```env
# LLM providers
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
MISTRAL_API_KEY=...
GEMINI_API_KEY=...

# Basic Auth — protects the entire app (only you can access)
AUTH_USERNAME=yourname
AUTH_PASSWORD=choose_a_strong_password

# Google OAuth for GSC (same credentials as on your laptop)
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=https://app.nrankai.com/api/gsc/oauth/callback

# Database (SQLite file — keep default or set explicit path)
# DATABASE_URL=sqlite+aiosqlite:////home/nrankai/htdocs/app.nrankai.com/geo_tool.db
```

> **Important:** Update `GOOGLE_REDIRECT_URI` in your Google Cloud Console OAuth credentials
> to add `https://app.nrankai.com/api/gsc/oauth/callback` as an authorised redirect URI.

---

## 5. Create the systemd service

```bash
# As root:
nano /etc/systemd/system/geo-tool.service
```

Paste:
```ini
[Unit]
Description=GEO Tool - FastAPI App
After=network.target

[Service]
User=nrankai
Group=nrankai
WorkingDirectory=/home/nrankai/htdocs/app.nrankai.com
EnvironmentFile=/home/nrankai/htdocs/app.nrankai.com/.env
ExecStart=/home/nrankai/htdocs/app.nrankai.com/venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8001 --workers 1
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable geo-tool
systemctl start geo-tool
systemctl status geo-tool   # should show "active (running)"
```

> Use `--workers 1` — SQLite doesn't support concurrent writes across multiple workers.

---

## 6. Configure Nginx in CloudPanel

In CloudPanel → your site → **Nginx Settings** (or Vhost Config), replace the default with:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name app.nrankai.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name app.nrankai.com;

    # SSL — CloudPanel manages these paths automatically
    ssl_certificate     /etc/nginx/ssl/app.nrankai.com/certificate.crt;
    ssl_certificate_key /etc/nginx/ssl/app.nrankai.com/private.key;

    client_max_body_size 50M;

    location /static/ {
        alias /home/nrankai/htdocs/app.nrankai.com/api/static/;
        expires 7d;
        add_header Cache-Control "public, immutable";
    }

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Long timeouts for LLM calls (can take 60-120s)
        proxy_read_timeout    300s;
        proxy_connect_timeout 300s;
        proxy_send_timeout    300s;
    }
}
```

> CloudPanel may have its own SSL cert paths — check under the site's SSL section for the exact file locations.

---

## 7. Verify

```bash
# Check service is running
systemctl status geo-tool

# Tail logs
journalctl -u geo-tool -f

# Test locally on the server
curl http://127.0.0.1:8001/api/gsc/properties
```

Then open `https://app.nrankai.com` in your browser — Basic Auth prompt should appear.

---

## 8. Updating the app

```bash
su - nrankai
cd /home/nrankai/htdocs/app.nrankai.com

# Pull latest changes from GitHub
git pull origin master

# If new dependencies were added:
source venv/bin/activate
pip install -r api/requirements.txt

# Restart service to pick up changes
sudo systemctl restart geo-tool
```

---

## Useful commands

```bash
# View live logs
journalctl -u geo-tool -f

# Restart app
sudo systemctl restart geo-tool

# Stop app
sudo systemctl stop geo-tool

# Check port
ss -tlnp | grep 8001
```

---

## Notes

- **SQLite location:** by default created in the working directory as `geo_tool.db`. Back it up periodically — it contains all your audit data, GSC tokens, guides, etc.
- **Google OAuth redirect:** after deploying, re-authorise GSC in the app at `https://app.nrankai.com/gsc` — the OAuth token from your laptop won't transfer.
- **Port 8001** is used (not 8000) to avoid conflicts if anything else runs on the server.
