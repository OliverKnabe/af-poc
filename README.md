# af-poc

Application Factory PoC — standalone demo without Coolify.

Shows the full AF user journey: app selection → parameter config → cloud-init generation → server simulation.

## Production Deployment (Docker + Caddy + real af-api)

This fork is configured for a public demo deployment on a VPS with HTTPS, basic auth, and a real af-api backend.

### Prerequisites

- A VPS with Docker + Docker Compose installed
- A DuckDNS (or other) domain pointing to the VPS IP
- Access to the IONOS Harbor registry for the af-api image
- IONOS Cloud credentials for server provisioning

### Setup

```bash
git clone https://github.com/OliverKnabe/af-poc.git
cd af-poc

# Create .env with your credentials
cat > .env << 'EOF'
HARBOR_USER=robot$imagefactory+appfactory-pull
HARBOR_PASS=<harbor-pull-token>
AF_FRONTEND_URL=https://your-domain.duckdns.org
DEFAULT_BASE_DOMAIN=your-domain.duckdns.org
IONOS_USERNAME=<ionos-username>
IONOS_PASSWORD=<ionos-password>
IONOS_DATACENTER_ID=<datacenter-uuid>
IONOS_SERVER_NAME=<server-name>
IONOS_SERVER_TEMPLATE_ID=<cube-template-uuid>
IONOS_IMAGE_ID=<ubuntu-26.04-af-image-uuid>
DUCKDNS_TOKEN=<duckdns-token>
DUCKDNS_DOMAIN=<subdomain>
SSH_PUBLIC_KEY=<your-ssh-public-key>
EOF

# Log in to Harbor
echo "$HARBOR_PASS" | docker login harbor.infra.cluster.ionos.com -u "$HARBOR_USER" --password-stdin

# Start the stack
docker compose up -d --build
```

The stack starts three containers:
- **af-api** — real AF API with JWE token generation (internal only, not exposed)
- **af-poc** — Flask frontend proxy
- **Caddy** — HTTPS reverse proxy with Let's Encrypt + basic auth (admin / strato123)

Open `https://your-domain.duckdns.org` — basic auth: `admin` / `strato123`.

### Update

```bash
git pull && docker compose up -d --build
```

---

## Quick Start (standalone, no af-api needed)

```bash
git clone https://github.com/tirufege/af-poc.git
cd af-poc
pip install -r requirements.txt
python3 app.py
```

Open http://localhost:5050

## With real af-api (recommended)

Run the real AF API alongside the PoC for authentic cloud-init generation with JWE tokens.

```bash
# Clone all repos into the same directory
git clone https://github.com/tirufege/af-poc.git
git clone https://github.com/IONOS-Server-Technology/af-api.git
git clone https://github.com/IONOS-Server-Technology/af-recipes.git
git clone https://github.com/IONOS-Server-Technology/af-core.git

# Switch to the feature branches
cd af-api     && git checkout feature/IF-547-api-implementation && cd ..
cd af-recipes && git checkout feature/IF-545-more-selfhosted-recipes && cd ..
cd af-core    && git checkout feature/IF-547-shared-library && cd ..

# Terminal 1 — af-api
# af-recipes must be a sibling directory; af-core is installed as a local pip dependency
cd af-api
pip install -r requirements.txt   # pulls af-core automatically if listed as git dep,
pip install -e ../af-core          # or install locally if cloned as sibling
DEV_MODE=true uvicorn app.main:app --port 8000

# Terminal 2 — af-poc
cd af-poc
pip install -r requirements.txt
python3 app.py
```

Open http://localhost:5050

The PoC auto-detects whether af-api is running:
- **af-api reachable** → proxies `/api/catalogue` and `/api/compose` to `http://localhost:8000/api/v1/`
- **af-api not running** → falls back to built-in mock (local YAML recipes)

Override the API URL: `AF_API_URL=http://other-host:8000/api/v1 python3 app.py`

> **Note:** af-api loads recipes from `../af-recipes/recipes` by default. All repos must be cloned as siblings. af-core is a shared library extracted from af-api — install it once per virtualenv.

## Sharing with others (public URL)

To give someone outside your network a clickable link, use [Cloudflare Quick Tunnels](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/) — no account or firewall changes needed:

```bash
# Install once
curl -sLo ~/bin/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
chmod +x ~/bin/cloudflared

# Start tunnel (while af-poc is running on port 5050)
cloudflared tunnel --url http://localhost:5050
```

Cloudflare prints a public HTTPS URL (e.g. `https://some-random-name.trycloudflare.com`) that anyone can open directly. The URL is temporary and changes on every restart.

## What it demonstrates

1. **App Catalogue** — 22 apps from af-recipes (n8n, Ollama, Gitea, Immich, Vaultwarden, Pi-hole, and more)
2. **Selection & Config** — max 5 apps, incompatibility handling (e.g. Pi-hole ↔ AdGuard Home), per-app parameter input
3. **Resource Calculation** — OS baseline + app requirements in the summary panel
4. **cloud-init Generation** — POST /api/compose returns a ready-to-use cloud-init YAML
5. **Server Simulation** — animated terminal output simulating the bootstrap process

## Installation Time Benchmarks

Measured on a CUBE server (2 vCPU, 4 GB RAM) with `ubuntu-26.04-af` image, IONOS de/txl datacenter. Times are from "Deploy to Server" click to all application containers healthy.

### End-to-end provisioning timeline

| Phase | Duration |
|---|---|
| Server deletion + IONOS reprovisioning + first boot | ~2m 37s |
| AF bootstrap → Docker pulls → containers started | ~2m 57s |
| **Total (click → apps running)** | **~5m 34s** |

### Per-application install times (Docker pull + start)

| App | Containers | Install time |
|---|---|---|
| n8n 2.20.9 | n8n + PostgreSQL + Traefik | ~2m 52s |
| Immich | server + ML + Redis + PostgreSQL | ~1m 10s |

> Times include Docker image pulls on a fresh server (cold cache). Subsequent installs on a server that already has the images cached will be significantly faster.

## Related

- [af-api](https://github.com/IONOS-Server-Technology/af-api) — Real AF API (FastAPI, JWE tokens, branch: feature/IF-547-api-implementation)
- [af-core](https://github.com/IONOS-Server-Technology/af-core) — Shared library: recipe loading, rendering, validation (branch: feature/IF-547-shared-library)
- [af-recipes](https://github.com/IONOS-Server-Technology/af-recipes) — App recipes (branch: feature/IF-545-more-selfhosted-recipes)
- [af-coolify-poc](https://github.com/tirufege/af-coolify-poc) — Previous PoC (Coolify-based)
