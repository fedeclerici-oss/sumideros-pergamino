#!/usr/bin/env python3
"""
Sumideros · Pergamino — Servidor local/Railway
Uso: python3 server.py  ->  abrir http://localhost:8766
"""
import json, os, secrets, uuid, email, urllib.request, urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

from google.oauth2 import service_account
import google.auth.transport

HTML_FILE = "index.html"
GEOJSON_FILE = "sumideros.geojson"
SHEET_RANGE = "Controles!A:F"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# ---- Config: env vars (Railway/Render) con fallback a config.json (local) ----
CONFIG = {}
for key, env in [
    ("login_user", "LOGIN_USER"),
    ("login_pass", "LOGIN_PASS"),
    ("users_json", "USERS_JSON"),
    ("sheet_id", "SHEET_ID"),
    ("drive_folder_id", "DRIVE_FOLDER_ID"),
    ("google_service_account_json", "GOOGLE_SERVICE_ACCOUNT_JSON"),
]:
    if os.environ.get(env):
        CONFIG[key] = os.environ[env]
try:
    with open("config.json", encoding="utf-8") as f:
        file_cfg = json.load(f)
    for k, v in file_cfg.items():
        CONFIG.setdefault(k, v)
    print("  Config cargada desde config.json")
except FileNotFoundError:
    pass

SESSIONS = {}  # token -> {"user": ...}


def get_users():
    """Lista de usuarios validos: USERS_JSON (varios) o LOGIN_USER/LOGIN_PASS (uno solo)."""
    if CONFIG.get("users_json"):
        return json.loads(CONFIG["users_json"])
    if CONFIG.get("login_user") and CONFIG.get("login_pass"):
        return [{"user": CONFIG["login_user"], "pass": CONFIG["login_pass"]}]
    return []


def make_token():
    return secrets.token_hex(32)


def get_session_user(handler):
    cookie = handler.headers.get("Cookie", "")
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("session="):
            token = part[8:]
            if token in SESSIONS:
                return SESSIONS[token]
    return None


# ---- Google auth: transporte minimo con urllib (sin dependencia de 'requests') ----
class _Resp:
    def __init__(self, status, headers, data):
        self.status = status
        self.headers = headers
        self.data = data


class UrllibAuthRequest(google.auth.transport.Request):
    def __call__(self, url, method="GET", body=None, headers=None, timeout=30, **kw):
        req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return _Resp(r.status, dict(r.getheaders()), r.read())
        except urllib.error.HTTPError as e:
            return _Resp(e.code, dict(e.headers or {}), e.read())


_AUTH_REQUEST = UrllibAuthRequest()
_creds = None


def get_access_token():
    global _creds
    if _creds is None:
        info = json.loads(CONFIG["google_service_account_json"])
        _creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    if not _creds.valid:
        _creds.refresh(_AUTH_REQUEST)
    return _creds.token


def sheets_append(row):
    token = get_access_token()
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{CONFIG['sheet_id']}"
        f"/values/{urllib.parse.quote(SHEET_RANGE)}:append"
        f"?valueInputOption=USER_ENTERED"
    )
    body = json.dumps({"values": [row]}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def sheets_read_all():
    token = get_access_token()
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{CONFIG['sheet_id']}"
        f"/values/{urllib.parse.quote(SHEET_RANGE)}"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    return data.get("values", [])


def drive_upload(filename, mime_type, file_bytes):
    token = get_access_token()
    boundary = uuid.uuid4().hex
    metadata = json.dumps({"name": filename, "parents": [CONFIG["drive_folder_id"]]})
    body = (
        f"--{boundary}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{metadata}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8") + file_bytes + f"\r\n--{boundary}--".encode("utf-8")
    req = urllib.request.Request(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,webViewLink",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/related; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def parse_multipart(headers, body):
    ct = headers.get("Content-Type", "")
    boundary = None
    for p in ct.split(";"):
        p = p.strip()
        if p.startswith("boundary="):
            boundary = p[9:].strip('"')
    if not boundary:
        raise ValueError("No boundary in Content-Type")
    msg = email.message_from_bytes(f"Content-Type: {ct}\r\n\r\n".encode() + body)
    fields, files = {}, {}
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        cd = part.get("Content-Disposition", "")
        name = filename = None
        for item in cd.split(";"):
            item = item.strip()
            if item.startswith("name="):
                name = item[5:].strip('"')
            elif item.startswith("filename="):
                filename = item[9:].strip('"')
        if name is None:
            continue
        payload = part.get_payload(decode=True)
        if filename:
            files[name] = (filename, part.get_content_type(), payload)
        else:
            fields[name] = payload.decode() if payload else ""
    return fields, files


import urllib.parse


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        print(f"  {a[0]} {a[1]}")

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/logout":
            cookie = self.headers.get("Cookie", "")
            for part in cookie.split(";"):
                part = part.strip()
                if part.startswith("session="):
                    SESSIONS.pop(part[8:], None)
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", "session=; Max-Age=0; Path=/")
            self.end_headers()
            return

        if self.path == "/login" or not get_session_user(self):
            html = self._login_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        if self.path == "/sumideros.geojson":
            self.send_file(GEOJSON_FILE, "application/geo+json")
            return

        if self.path == "/api/controles":
            try:
                rows = sheets_read_all()
            except Exception as ex:
                self.send_json(500, {"error": str(ex)})
                return
            controles = {}
            for row in rows[1:] if rows and rows[0] and rows[0][0] == "timestamp" else rows:
                if len(row) < 4:
                    continue
                timestamp, usuario, sumidero_id, estado = row[0], row[1], row[2], row[3]
                observacion = row[4] if len(row) > 4 else ""
                foto_link = row[5] if len(row) > 5 else ""
                controles[sumidero_id] = {
                    "timestamp": timestamp, "usuario": usuario, "estado": estado,
                    "observacion": observacion, "foto_link": foto_link,
                }
            self.send_json(200, controles)
            return

        # Main app
        self.send_file(HTML_FILE, "text/html; charset=utf-8")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            if self.path == "/api/login":
                body = json.loads(raw)
                u, p = body.get("user", "").strip(), body.get("pass", "")
                matched = next((usr for usr in get_users() if usr["user"] == u and usr["pass"] == p and u), None)
                if matched:
                    token = make_token()
                    SESSIONS[token] = {"user": matched["user"]}
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Set-Cookie", f"session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400")
                    body_resp = json.dumps({"ok": True}).encode()
                    self.send_header("Content-Length", str(len(body_resp)))
                    self.end_headers()
                    self.wfile.write(body_resp)
                else:
                    self.send_json(401, {"ok": False, "error": "Usuario o contraseña incorrectos"})
                return

            usr = get_session_user(self)
            if not usr:
                self.send_json(401, {"error": "No autorizado"})
                return

            if self.path == "/api/control":
                fields, files = parse_multipart(self.headers, raw)
                sumidero_id = fields.get("sumidero_id", "")
                estado = fields.get("estado", "")
                observacion = fields.get("observacion", "")
                if not sumidero_id or not estado:
                    self.send_json(400, {"error": "Falta sumidero_id o estado"})
                    return
                foto_link = ""
                if "foto" in files:
                    filename, mime_type, data = files["foto"]
                    up = drive_upload(f"sumidero_{sumidero_id}_{uuid.uuid4().hex[:8]}_{filename}", mime_type, data)
                    foto_link = up.get("webViewLink", "")
                import datetime as _dt
                timestamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
                sheets_append([timestamp, usr["user"], sumidero_id, estado, observacion, foto_link])
                self.send_json(200, {
                    "ok": True,
                    "control": {"timestamp": timestamp, "usuario": usr["user"], "estado": estado,
                                "observacion": observacion, "foto_link": foto_link},
                })
                return

            self.send_json(404, {"error": "Ruta no encontrada"})
        except Exception as ex:
            import traceback
            traceback.print_exc()
            self.send_json(500, {"error": str(ex)})

    def _login_page(self):
        return """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Sumideros · Acceso</title>
<meta name="theme-color" content="#ffffff">
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#ffffff;color:#17171a;font-family:'DM Sans',sans-serif;font-weight:300;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:20px;}
.card{background:#ffffff;border:1px solid rgba(0,0,0,0.1);border-radius:12px;padding:40px 36px;width:100%;max-width:380px;box-shadow:0 4px 20px rgba(0,0,0,.06);}
.logo{font-family:'Bebas Neue',sans-serif;font-size:36px;letter-spacing:.03em;text-align:center;margin-bottom:4px;}
.logo span{color:#e63329;}
.subtitle{font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.15em;text-transform:uppercase;color:#8a8a94;text-align:center;margin-bottom:32px;}
.field{margin-bottom:16px;}
label{font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:#8a8a94;display:block;margin-bottom:6px;}
input{width:100%;background:#f0f0f2;border:1px solid rgba(0,0,0,0.1);border-radius:6px;padding:12px 14px;font-family:'DM Mono',monospace;font-size:14px;color:#17171a;outline:none;}
input:focus{border-color:#e63329;}
.btn{width:100%;background:#e63329;color:#fff;border:none;border-radius:6px;padding:14px;font-family:'Bebas Neue',sans-serif;font-size:18px;letter-spacing:.1em;cursor:pointer;margin-top:8px;}
.btn:hover{background:#ff4a3f;}
.error{background:rgba(230,51,41,.08);border:1px solid rgba(230,51,41,.3);border-radius:6px;padding:10px 14px;font-family:'DM Mono',monospace;font-size:10px;color:#c92a1f;margin-bottom:16px;display:none;}
.error.visible{display:block;}
</style>
</head>
<body>
<div class="card">
  <div class="logo">Sumideros<span>·Pergamino</span></div>
  <div class="subtitle">Acceso al panel de control</div>
  <div class="error" id="err">Usuario o contraseña incorrectos</div>
  <form onsubmit="doLogin(event)">
    <div class="field">
      <label>Usuario</label>
      <input type="text" id="u" autocomplete="username" autofocus>
    </div>
    <div class="field">
      <label>Contraseña</label>
      <input type="password" id="p" autocomplete="current-password">
    </div>
    <button class="btn" type="submit">Ingresar →</button>
  </form>
</div>
<script>
async function doLogin(e) {
  e.preventDefault();
  const u = document.getElementById('u').value.trim();
  const p = document.getElementById('p').value;
  const res = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({user:u, pass:p})});
  const data = await res.json();
  if (data.ok) window.location.href = '/';
  else document.getElementById('err').classList.add('visible');
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 8766))
    HOST = "0.0.0.0"
    server = HTTPServer((HOST, PORT), Handler)
    print(f"Servidor · puerto {PORT}")
    server.serve_forever()
