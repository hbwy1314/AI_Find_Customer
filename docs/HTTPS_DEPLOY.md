# AI Hunter — Production HTTPS Deployment

Step-by-step checklist for shipping the user-auth + Microsoft Graph upgrade to a
Linux server behind nginx + Let's Encrypt. The dev workflow on macOS / local is
unchanged (just `uvicorn api.app:app`).

> Prereqs: a Linux server (Ubuntu 22.04+ recommended), Python 3.11+ in
> `/opt/ai-hunter/backend/.venv/`, Node 20+ for the frontend build, a public
> DNS A record pointing at the server, ports 80/443 open.

## 1. Install the application

```bash
sudo useradd --system --home /opt/ai-hunter --shell /bin/bash aihunter
sudo mkdir -p /opt/ai-hunter
sudo chown -R aihunter:aihunter /opt/ai-hunter

# Backend
sudo -u aihunter git clone <repo-url> /opt/ai-hunter/repo
sudo -u aihunter python3.11 -m venv /opt/ai-hunter/backend/.venv
cd /opt/ai-hunter/backend
sudo -u aihunter .venv/bin/pip install -r requirements.txt

# Frontend
cd /opt/ai-hunter/frontend
sudo -u aihunter npm ci
sudo -u aihunter npm run build            # outputs dist/
```

## 2. Generate secrets

```bash
# Fernet key (used to encrypt SMTP/Graph secrets at rest)
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Session cookie HMAC secret (itsdangerous)
python3 -c "import secrets; print(secrets.token_urlsafe(48))"

# Optional: API access token for non-cookie clients (curl, SDK)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 3. Author the environment file

```bash
sudo tee /etc/ai-hunter.env >/dev/null <<'EOF'
APP_ENV=production
PUBLIC_BASE_URL=https://aih.example.com
CORS_ORIGINS=["https://aih.example.com"]
TRUSTED_HOSTS=["aih.example.com"]

# 32-byte Fernet key (44 url-safe base64 chars)
SECRETS_ENCRYPTION_KEY=<paste-fernet-key>

# Session integrity secret
SESSION_SECRET=<paste-session-secret>

# Optional fallback for non-cookie clients (curl, SDK). Leave empty to disable.
API_ACCESS_TOKEN=<paste-api-token>

# LLM / search keys (fill what you use)
LLM_MODEL=minimax/MiniMax-M2.1-highspeed
REASONING_MODEL=minimax/MiniMax-M2.5
MINIMAX_API_KEY=
TAVILY_API_KEY=

# Microsoft Graph (Application permission / client_credentials)
GRAPH_TENANT_ID=
GRAPH_CLIENT_ID=
GRAPH_CLIENT_SECRET=
GRAPH_MAILBOX_UPN=sales@company.com
EMAIL_PROVIDER_TYPE=graph
EOF
sudo chmod 600 /etc/ai-hunter.env
sudo chown root:root /etc/ai-hunter.env
```

## 4. One-time Azure AD admin consent

Visit the URL the Settings page prints (Settings → Microsoft Graph → "在 Azure
AD 同意"), or hit directly:

```
https://login.microsoftonline.com/<tenant_id>/adminconsent?client_id=<client_id>
```

Sign in as a tenant admin, click Accept. Required once per Azure AD App. After
that `client_credentials` tokens can be acquired silently.

## 5. Install nginx + certbot

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo cp /opt/ai-hunter/repo/deploy/nginx/ai-hunter.conf /etc/nginx/sites-available/
sudo ln -sf /etc/nginx/sites-available/ai-hunter.conf /etc/nginx/sites-enabled/
sudo sed -i 's/aih\.example\.com/<your-real-host>/g' /etc/nginx/sites-enabled/ai-hunter.conf

# Get a Let's Encrypt cert (will adjust the nginx config to add 443 / TLS)
sudo certbot --nginx -d <your-real-host>

# Patch the static root to point at the freshly-built bundle
sudo sed -i 's|/opt/ai-hunter/frontend/dist|/opt/ai-hunter/frontend/dist|g' /etc/nginx/sites-enabled/ai-hunter.conf

sudo nginx -t && sudo systemctl reload nginx
```

## 6. Install the systemd service

```bash
sudo cp /opt/ai-hunter/repo/deploy/systemd/ai-hunter-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-hunter-api
sudo systemctl status ai-hunter-api    # should show "active (running)"
journalctl -u ai-hunter-api -f        # live logs
```

## 7. First admin signup

The first time you visit `https://<your-host>/`, the frontend will render the
`/signup` page (because `users` is empty). Sign up with your email + an 8+ char
password — that account becomes the admin. Subsequent registrations are blocked
(admin must add new users manually, e.g. via `sqlite3 /opt/ai-hunter/backend/email_automation.db "INSERT INTO users(email, password_hash, role, created_at) VALUES(...)"` for v1; a UI is on the roadmap).

## 8. Roll-forward maintenance

```bash
# Update code
cd /opt/ai-hunter/repo
sudo -u aihunter git pull
cd /opt/ai-hunter/backend
sudo -u aihunter .venv/bin/pip install -r requirements.txt
cd /opt/ai-hunter/frontend
sudo -u aihunter npm ci && sudo -u aihunter npm run build
sudo rsync -a dist/ /opt/ai-hunter/frontend/dist/
sudo systemctl restart ai-hunter-api
```

## 9. Verify

```bash
# HTTPS redirect
curl -I http://<host>/                 # 301 → https
curl -I https://<host>/                # 200 (SPA shell)

# Auth flow
curl -i -c c.txt -X POST https://<host>/api/auth/signup \
  -H 'Content-Type: application/json' \
  -d '{"email":"x@y.com","password":"CorrectHorse!9"}'   # 201 + Set-Cookie
curl -b c.txt https://<host>/api/auth/me                  # {"user":{...}}
curl -b c.txt -X POST https://<host>/api/v1/email-accounts -d '{}'   # 403 csrf
```

## 10. Backup

The on-disk state that matters:

- `/opt/ai-hunter/backend/email_automation.db` — users, sessions, email_accounts (with encrypted secrets)
- `/opt/ai-hunter/backend/email_sessions.db`, `automation_queue.db` — runtime state
- `/opt/ai-hunter/backend/data/hunts/` — hunt results (JSON files)
- `/etc/ai-hunter.env` — config + encryption key (KEEP THIS SAFE; losing it means re-encrypting every account)

A nightly `tar czf` of the four paths + off-host sync is sufficient.
