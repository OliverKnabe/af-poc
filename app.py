import os
import glob
import secrets
import time
import base64
from flask import Flask, request, jsonify, render_template, Response
import yaml
import requests as http

app = Flask(__name__)

RECIPES_DIR = os.path.join(os.path.dirname(__file__), "recipes")
AF_API_URL = os.environ.get("AF_API_URL", "http://localhost:8000/api/v1")
AF_FRONTEND_URL = os.environ.get("AF_FRONTEND_URL", "https://frontendspace.duckdns.org")

def _inject_af_block(cloud_init_str, fallback_token="", http_routes=None):
    """Updates application_factory block: sets api_url, preserves existing token, adds app domains."""
    yaml_body = "\n".join(l for l in cloud_init_str.splitlines() if not l.startswith("#cloud-config"))
    try:
        data = yaml.safe_load(yaml_body) or {}
    except Exception:
        data = {}
    existing_token = (data.get("application_factory") or {}).get("token", fallback_token)
    af_block = {
        "token": existing_token,
        "api_url": AF_FRONTEND_URL,
        "retry_attempts": 3,
        "retry_backoff_seconds": 1,
    }
    if http_routes:
        af_block["applications"] = [
            {"id": r["application"], "domain": r["url"].replace("https://", "").replace("http://", "")}
            for r in http_routes if r.get("url")
        ]
    data["application_factory"] = af_block
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

def _strip_params(apps):
    for a in apps:
        a.pop("parameters", None)
    return apps

def normalise_catalogue(data):
    """Normalises real af-api response to match PoC frontend expectations."""
    for app in data.get("applications", []):
        if "display_name" not in app:
            app["display_name"] = app.get("name", app.get("id", ""))
        if "app_min_ram_mb" not in app and "resources" in app:
            app["app_min_ram_mb"] = app["resources"].get("app_min_ram_mb", 0)
            app["app_min_disk_mb"] = app["resources"].get("app_min_disk_mb", 0)
    _strip_params(data.get("applications", []))
    return data

def catalogue_fallback():
    recipes = load_recipes()
    apps = list(recipes.values())
    for a in apps:
        if "display_name" not in a:
            a["display_name"] = a.get("name", a["id"])
    _strip_params(apps)
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
    body.setdefault("base_os", "ubuntu-26.04")
    body.setdefault("credentials", {"root_password": secrets.token_urlsafe(16)})
    try:
        r = http.post(f"{AF_API_URL}/compose", json=body, timeout=10)
        data = r.json()
        base_domain = body.get("base_domain", "")
        for route in data.get("network", {}).get("http_routes", []):
            url = route.get("url", "")
            app_id = route.get("application", "")
            subdomain = app_id.replace("-", "")
            expected = f"{subdomain}.{base_domain}"
            if base_domain and subdomain and expected not in url:
                scheme = "https://" if url.startswith("https") else "http://"
                route["url"] = f"{scheme}{expected}"
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

IONOS_API = "https://api.ionos.com/cloudapi/v6"
IONOS_USERNAME = os.environ.get("IONOS_USERNAME", "")
IONOS_PASSWORD = os.environ.get("IONOS_PASSWORD", "")
IONOS_DATACENTER_ID = os.environ.get("IONOS_DATACENTER_ID", "")
IONOS_SERVER_ID = os.environ.get("IONOS_SERVER_ID", "")
IONOS_SERVER_TEMPLATE_ID = os.environ.get("IONOS_SERVER_TEMPLATE_ID", "")
IONOS_SERVER_NAME = os.environ.get("IONOS_SERVER_NAME", "af-server")
IONOS_IMAGE_ALIAS = os.environ.get("IONOS_IMAGE_ALIAS", "ubuntu:24.04")

def _wait_vm_state(auth, dc, srv, target_state, label, interval=5, retries=30):
    """Polls server vmState until it matches target_state."""
    for _ in range(retries):
        time.sleep(interval)
        r = http.get(f"{IONOS_API}/datacenters/{dc}/servers/{srv}", auth=auth)
        vm_state = r.json().get("properties", {}).get("vmState", "UNKNOWN")
        yield f"data: cmd|{label} (vmState: {vm_state})\n\n"
        if vm_state == target_state:
            return
    yield f"data: error|Timeout waiting for {target_state}\n\n"
    raise StopIteration

def _wait_request(auth, req_location, label, interval=4, retries=60):
    """Polls request status; yields SSE lines. Returns True on DONE, False on FAILED/timeout."""
    req_id = req_location.split("/requests/")[-1].rstrip("/status")
    for _ in range(retries):
        time.sleep(interval)
        sr = http.get(f"{IONOS_API}/requests/{req_id}/status", auth=auth)
        status = sr.json().get("metadata", {}).get("status", "UNKNOWN")
        yield f"data: cmd|{label} ({status})\n\n"
        if status == "DONE":
            return
        if status == "FAILED":
            yield f"data: error|{label} — request FAILED\n\n"
            raise StopIteration

@app.route("/api/reinstall", methods=["POST"])
def reinstall():
    data = request.get_json(force=True)
    cloud_init = data.get("cloud_init", "")
    auth = (IONOS_USERNAME, IONOS_PASSWORD)
    dc = IONOS_DATACENTER_ID
    srv = IONOS_SERVER_ID

    def generate():
        # 1. Get current boot volume
        yield "data: info|🔍 Getting current boot volume...\n\n"
        r = http.get(f"{IONOS_API}/datacenters/{dc}/servers/{srv}/volumes", auth=auth)
        items = r.json().get("items", [])
        if not items:
            yield "data: error|No volumes attached to server\n\n"
            return
        old_vol_id = items[0]["id"]
        yield f"data: ok|Found volume {old_vol_id[:8]}...\n\n"

        # 2. Suspend server (CUBE servers use suspend/resume, not stop/start)
        yield "data: info|⏸️  Suspending server...\n\n"
        vm_check = http.get(f"{IONOS_API}/datacenters/{dc}/servers/{srv}", auth=auth)
        vm_state = vm_check.json().get("properties", {}).get("vmState", "")
        if vm_state == "SUSPENDED":
            yield "data: ok|Server already suspended\n\n"
        else:
            r = http.post(f"{IONOS_API}/datacenters/{dc}/servers/{srv}/suspend", auth=auth)
            if r.status_code not in (202, 204):
                yield f"data: error|Suspend failed: {r.status_code} — {r.text[:120]}\n\n"
                return
            try:
                yield from _wait_vm_state(auth, dc, srv, "SUSPENDED", "Waiting for suspend")
            except StopIteration:
                return
            yield "data: ok|Server suspended\n\n"

        # 3. Patch DAS volume with new image + cloud-init (DAS cannot be detached)
        yield "data: info|💾 Reimaging volume with cloud-init...\n\n"
        user_data = base64.b64encode(cloud_init.encode()).decode()
        r = http.patch(f"{IONOS_API}/datacenters/{dc}/volumes/{old_vol_id}",
                       auth=auth, json={"imageAlias": IONOS_IMAGE_ALIAS, "userData": user_data})
        if r.status_code not in (200, 201, 202):
            yield f"data: error|Volume patch failed: {r.status_code} — {r.text[:200]}\n\n"
            return
        try:
            yield from _wait_request(auth, r.headers.get("Location",""), "Reimaging", interval=5, retries=40)
        except StopIteration:
            return
        yield "data: ok|Volume reimaged with cloud-init\n\n"

        # 7. Resume server (CUBE uses /resume, not /start)
        yield "data: info|▶️  Resuming server...\n\n"
        r = http.post(f"{IONOS_API}/datacenters/{dc}/servers/{srv}/resume", auth=auth)
        if r.status_code not in (202, 204):
            yield f"data: error|Resume failed: {r.status_code}\n\n"
            return
        try:
            yield from _wait_vm_state(auth, dc, srv, "RUNNING", "Waiting for boot")
        except StopIteration:
            return
        yield "data: info|🌐 Server booting — cloud-init applying...\n\n"
        yield "data: done|🎉 Reinstall complete! IP address preserved.\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

if __name__ == "__main__":
    app.run(debug=True, port=5050)
