import os
import glob
import secrets
import time
from flask import Flask, request, jsonify, render_template, Response
import yaml
import requests as http

app = Flask(__name__)

RECIPES_DIR = os.path.join(os.path.dirname(__file__), "recipes")
AF_API_URL = os.environ.get("AF_API_URL", "http://localhost:8000/api/v1")
AF_FRONTEND_URL = os.environ.get("AF_FRONTEND_URL", "https://frontendspace.duckdns.org")

def _inject_af_block(cloud_init_str, token):
    """Injects/replaces application_factory block in a cloud-init YAML string."""
    yaml_body = "\n".join(l for l in cloud_init_str.splitlines() if not l.startswith("#cloud-config"))
    try:
        data = yaml.safe_load(yaml_body) or {}
    except Exception:
        data = {}
    data["application_factory"] = {
        "token": token,
        "api_url": AF_FRONTEND_URL,
        "retry_attempts": 3,
        "retry_backoff_seconds": 1,
    }
    return "#cloud-config\n" + yaml.dump(data, default_flow_style=False, allow_unicode=True)

OS_BASELINES_FALLBACK = {
    "ubuntu-24.04": {"os_min_ram_mb": 512, "os_min_cpu_cores": 1, "os_min_disk_mb": 5120}
}

def load_recipes():
    recipes = {}
    for path in glob.glob(os.path.join(RECIPES_DIR, "*.yaml")):
        with open(path) as f:
            r = yaml.safe_load(f)
        if "id" not in r:
            r["id"] = os.path.splitext(os.path.basename(path))[0]
        recipes[r["id"]] = r
    return recipes

def normalise_catalogue(data):
    """Normalises real af-api response to match PoC frontend expectations."""
    for app in data.get("applications", []):
        # Real API has 'name', not 'display_name'
        if "display_name" not in app:
            app["display_name"] = app.get("name", app.get("id", ""))
        # Real API has resources.app_min_ram_mb (nested)
        if "app_min_ram_mb" not in app and "resources" in app:
            app["app_min_ram_mb"] = app["resources"].get("app_min_ram_mb", 0)
            app["app_min_disk_mb"] = app["resources"].get("app_min_disk_mb", 0)
    return data

def catalogue_fallback():
    recipes = load_recipes()
    apps = list(recipes.values())
    for a in apps:
        if "display_name" not in a:
            a["display_name"] = a.get("name", a["id"])
    return {
        "os_baselines": OS_BASELINES_FALLBACK,
        "applications": apps,
        "total": len(apps)
    }

def compose_fallback(data):
    recipes = load_recipes()
    selected = data.get("applications", [])
    base_domain = data.get("base_domain", "example.stratoserver.net")
    credentials = data.get("credentials", {})

    resolved = {}
    http_routes = []
    direct_ports = []

    for item in selected:
        app_id = item["id"]
        recipe = recipes.get(app_id, {})
        app_params = item.get("parameters", {}).copy()

        for p in recipe.get("parameters", []):
            pname = p["name"]
            if pname in ("APP_DOMAIN", "app_domain") and not app_params.get(pname):
                subdomain = app_id.replace("-", "")
                app_params[pname] = f"{subdomain}.{base_domain}"
            elif not app_params.get(pname) and p.get("auto_generate"):
                app_params[pname] = secrets.token_urlsafe(24)
            elif not app_params.get(pname) and p.get("default"):
                app_params[pname] = p["default"]

        resolved[app_id] = app_params

        for port in recipe.get("ports", []):
            if port.get("public"):
                subdomain = app_id.replace("-", "")
                http_routes.append({"application": app_id, "url": f"https://{subdomain}.{base_domain}"})

    root_pw = credentials.get("root_password", "CHANGE_ME")
    ssh_key = credentials.get("ssh_public_key", "")
    token = secrets.token_urlsafe(32)

    cloud_init_data = {
        "hostname": "af-server",
        "runcmd": [
            "apt-get update -q",
            "apt-get install -y -q docker.io docker-compose-v2 curl",
            "systemctl enable --now docker",
        ],
    }
    if root_pw and root_pw != "CHANGE_ME":
        cloud_init_data["chpasswd"] = {"list": [f"root:{root_pw}"], "expire": False}
    if ssh_key:
        cloud_init_data["ssh_authorized_keys"] = [ssh_key]

    cloud_init = _inject_af_block("#cloud-config\n" + yaml.dump(cloud_init_data, default_flow_style=False, allow_unicode=True), token)

    return jsonify({
        "token": token,
        "expires_at": "2026-04-22T16:00:00Z",
        "cloud_init": cloud_init,
        "network": {"traefik_enabled": True, "http_routes": http_routes, "direct_ports": direct_ports},
        "_fallback": True
    })

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/catalogue")
def catalogue():
    try:
        r = http.get(f"{AF_API_URL}/catalogue", params=request.args, timeout=3)
        data = normalise_catalogue(r.json())
        return jsonify(data)
    except Exception:
        return jsonify(catalogue_fallback())

@app.route("/api/compose", methods=["POST"])
def compose():
    body = request.get_json(force=True)
    # Auto-fill APP_DOMAIN for each app — real af-api requires it explicitly
    base_domain = body.get("base_domain", "")
    for app_item in body.get("applications", []):
        params = app_item.setdefault("parameters", {})
        if "APP_DOMAIN" not in params and base_domain:
            params["APP_DOMAIN"] = f"{app_item['id'].replace('-', '')}.{base_domain}"
    try:
        r = http.post(f"{AF_API_URL}/compose", json=body, timeout=10)
        data = r.json()
        if "cloud_init" in data:
            data["cloud_init"] = _inject_af_block(data["cloud_init"], data.get("token", ""))
        return jsonify(data)
    except Exception:
        return compose_fallback(body)

@app.route("/api/health")
def health():
    try:
        r = http.get(f"{AF_API_URL}/health", timeout=2)
        data = r.json()
        data["proxy"] = "ok"
        return jsonify(data)
    except Exception:
        return jsonify({"status": "fallback", "proxy": "af-api not reachable"})

@app.route("/api/simulate-bootstrap", methods=["POST"])
def simulate_bootstrap():
    data = request.get_json(force=True)
    app_ids = data.get("app_ids", [])
    recipes = load_recipes()

    def generate():
        steps = [
            ("info", "🚀 Application Factory Bootstrap started"),
            ("info", "📦 Updating package lists..."),
            ("cmd",  "apt-get update -q"),
            ("ok",   "Package lists updated"),
            ("info", "🐳 Installing Docker Engine..."),
            ("cmd",  "apt-get install -y docker.io docker-compose-v2"),
            ("ok",   "Docker 26.1.3 installed"),
            ("cmd",  "systemctl enable --now docker"),
            ("ok",   "Docker daemon started"),
            ("info", "🔧 Deploying Traefik reverse proxy..."),
            ("cmd",  "docker pull traefik:v3.0"),
            ("ok",   "Traefik ready"),
        ]
        for app_id in app_ids:
            recipe = recipes.get(app_id, {})
            display = recipe.get("display_name", recipe.get("name", app_id))
            version = recipe.get("app_version", "latest")
            steps += [
                ("info", f"📥 Deploying {display} v{version}..."),
                ("cmd",  f"mkdir -p /opt/{app_id}"),
                ("cmd",  f"docker compose -f /opt/{app_id}/docker-compose.yml up -d"),
                ("ok",   f"{display} started successfully"),
            ]
            for port in recipe.get("ports", []):
                if port.get("public"):
                    steps.append(("url", f"  → {display}: port {port['port']}/tcp ({port['description']})"))
        steps += [
            ("info", "✅ All applications deployed"),
            ("info", "🌐 Traefik routing configured"),
            ("done", "🎉 Bootstrap complete!"),
        ]
        for level, msg in steps:
            yield f"data: {level}|{msg}\n\n"
            time.sleep(0.3 if level in ("cmd", "ok") else 0.6)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

if __name__ == "__main__":
    app.run(debug=True, port=5050)
