# Deploying the web app

The web app is one process: FastAPI serves the API, runs review jobs as separate `litscout`
processes, and serves the built frontend. Data: `backend/data/web.db` (users, sessions, jobs) and
`runs/<run>/` (each review's pipeline store and outputs). Uploaded PDFs are deleted once their seeds
are imported.

## First install (server)

```bash
cd /opt/litscout
.venv/bin/pip install -e ".[web]"
# Node 20 for the frontend build
curl -fsSL https://nodejs.org/dist/v20.18.1/node-v20.18.1-linux-x64.tar.xz | tar -xJ -C /opt
ln -sf /opt/node-v20.18.1-linux-x64/bin/{node,npm,npx} /usr/local/bin/
(cd frontend && npm ci && npm run build)
cp deploy/litscout-web.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now litscout-web
# accounts (there is no sign-up)
(cd backend && ../.venv/bin/python -m app.cli create-user --random)
```

## Update

```bash
cd /opt/litscout && git pull
.venv/bin/pip install -e ".[web]"
(cd frontend && npm ci && npm run build)
systemctl restart litscout-web   # running reviews keep going (KillMode=process)
```

## Settings (environment, optional)

| Variable | Default | Meaning |
|---|---|---|
| `WEB_MAX_CONCURRENT_JOBS` | 2 | reviews running at the same time; others wait in the queue |
| `WEB_MAX_COST_USD` | `MAX_COST_USD` or 5 | highest cost limit a user can set per review |
| `WEB_MAX_PAPERS` | `MAX_PAPERS` or 20000 | paper cap per review |
| `WEB_MAX_PDFS`, `WEB_MAX_PDF_MB` | 20, 25 | upload limits |
| `WEB_SESSION_DAYS` | 7 | sign-in lifetime |
| `WEB_COOKIE_SECURE` | false | set to true once the site is served over HTTPS |
| `WEB_DATA_DIR`, `WEB_RUNS_DIR`, `WEB_FRONTEND_DIST` | `backend/data`, `runs`, `frontend/dist` | paths |

The app speaks plain HTTP on port 8080. For HTTPS, put a reverse proxy with a certificate in front
(for example Caddy with a domain name) and set `WEB_COOKIE_SECURE=true`.

## Development

```bash
.venv/bin/uvicorn app.main:app_factory --factory --app-dir backend --port 8080 --reload
(cd frontend && npm run dev)        # http://localhost:5173, proxies /api to :8080
.venv/bin/python -m pytest -q        # pipeline and API tests
```
