# COLOR-STUDIO
"""
Color Studio - ajuste de cores da tela inteira no Windows.
Somente Python 3 (sem instalar nada). A interface abre numa janela do Edge/Chrome.

- Saturacao, intensidade, temperatura, R/G/B e tonalidade: matriz de cor do compositor
  do Windows (Magnification API), aplicada em toda a tela.
- Brilho, contraste e gama: tabela de gama da tela (SetDeviceGammaRamp).
- Funciona na area de trabalho, janelas e jogos em janela / tela cheia sem bordas.
  Nao aparece em tela cheia exclusiva.

Como usar: de dois cliques em color_studio.pyw (ou: pythonw color_studio.pyw)
"""
APP_VERSION = "4.0"

import ctypes
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

IS_WIN = sys.platform == "win32"
if not IS_WIN and os.environ.get("CS_PREVIEW") != "1":
    raise SystemExit("Este app funciona apenas no Windows.")

PORT = 47615
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, "perfis")
os.makedirs(BASE, exist_ok=True)
CFG_FILE = os.path.join(BASE, "_config.json")

# ============================================================ Windows API
if IS_WIN:
    mag = ctypes.windll.Magnification
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    class MAGCOLOREFFECT(ctypes.Structure):
        _fields_ = [("transform", (ctypes.c_float * 5) * 5)]

    mag.MagSetFullscreenColorEffect.argtypes = [ctypes.POINTER(MAGCOLOREFFECT)]
    mag.MagSetFullscreenColorEffect.restype = ctypes.c_int
    user32.GetDC.restype = ctypes.c_void_p
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.SetDeviceGammaRamp.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.SetDeviceGammaRamp.restype = ctypes.c_int


def apply_matrix(m5):
    if not IS_WIN:
        return True
    eff = MAGCOLOREFFECT()
    for i in range(5):
        for j in range(5):
            eff.transform[i][j] = m5[i][j]
    return bool(mag.MagSetFullscreenColorEffect(ctypes.byref(eff)))


def apply_ramp(brilho, contraste, gama):
    if not IS_WIN:
        return True
    ramp = ((ctypes.c_ushort * 256) * 3)()
    g = max(gama, 0.1)
    for i in range(256):
        x = (i / 255.0) ** (1.0 / g)
        x = (x - 0.5) * contraste + 0.5 + brilho
        v = int(max(0.0, min(1.0, x)) * 65535)
        for ch in range(3):
            ramp[ch][i] = v
    hdc = user32.GetDC(None)
    ok = gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp))
    user32.ReleaseDC(None, hdc)
    return bool(ok)


def pythonw():
    exe = sys.executable
    pw = exe.replace("python.exe", "pythonw.exe")
    return pw if os.path.exists(pw) else exe


def set_startup(on):
    if not IS_WIN:
        return
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                         r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
    try:
        if on:
            winreg.SetValueEx(key, "ColorStudio", 0, winreg.REG_SZ,
                              f'"{pythonw()}" "{os.path.abspath(__file__)}"')
        else:
            try:
                winreg.DeleteValue(key, "ColorStudio")
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)


# ============================================================ Matematica de cor
def mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def build_matrix(p):
    sat = p["saturacao"] / 100.0
    contr = p["intensidade"] / 100.0
    temp = p["temperatura"] / 100.0
    gr, gg, gb = p["vermelho"] / 100.0, p["verde"] / 100.0, p["azul"] / 100.0
    hue = math.radians(p["tonalidade"])
    w = (0.2126, 0.7152, 0.0722)
    S = [[(1 - sat) * w[j] + (sat if i == j else 0) for j in range(3)] for i in range(3)]
    c, s = math.cos(hue), math.sin(hue)
    H = [
        [.213 + c * .787 - s * .213, .715 - c * .715 - s * .715, .072 - c * .072 + s * .928],
        [.213 - c * .213 + s * .143, .715 + c * .285 + s * .140, .072 - c * .072 - s * .283],
        [.213 - c * .213 - s * .787, .715 - c * .715 + s * .715, .072 + c * .928 + s * .072],
    ]
    rt, bt, gt = 1 + 0.25 * temp, 1 - 0.25 * temp, 1 - 0.05 * abs(temp)
    G = [[gr * rt, 0, 0], [0, gg * gt, 0], [0, 0, gb * bt]]
    C = mul(G, mul(H, S))
    C = [[v * contr for v in row] for row in C]
    off = 0.5 * (1 - contr)
    M = [[0.0] * 5 for _ in range(5)]
    for i in range(3):
        for j in range(3):
            M[i][j] = C[j][i]
        M[4][i] = off
    M[3][3] = 1.0
    M[4][4] = 1.0
    return M


# ============================================================ Perfis e configuracao
PARAMS = {  # chave: (minimo, maximo, padrao)
    "saturacao": (0, 200, 100), "intensidade": (50, 150, 100), "temperatura": (-100, 100, 0),
    "vermelho": (0, 200, 100), "verde": (0, 200, 100), "azul": (0, 200, 100), "tonalidade": (-180, 180, 0),
    "brilho": (-50, 50, 0), "contraste": (50, 150, 100), "gama": (50, 200, 100),
}
DEFAULTS = {k: v[2] for k, v in PARAMS.items()}

PRESETS = [
    ("Competitivo", "Sombras abertas e alta visibilidade",
     dict(DEFAULTS, saturacao=140, intensidade=112, brilho=4, gama=110)),
    ("Cores naturais", "Tons equilibrados e leve realce", dict(DEFAULTS, saturacao=108, intensidade=102)),
    ("Personalizado", "Seu ajuste livre", dict(DEFAULTS)),
]


def sanitize(values):
    out = {}
    for k, (lo, hi, d) in PARAMS.items():
        try:
            v = float((values or {}).get(k, d))
        except (TypeError, ValueError):
            v = d
        out[k] = int(round(max(lo, min(hi, v))))
    return out


def perfil_path(nome):
    return os.path.join(BASE, nome + ".json")


def listar():
    arqs = [f[:-5] for f in os.listdir(BASE) if f.endswith(".json") and not f.startswith("_")]
    return sorted(arqs, key=lambda n: os.path.getctime(perfil_path(n)))


def carregar(nome):
    with open(perfil_path(nome), encoding="utf-8") as f:
        d = json.load(f)
    return sanitize(d), d.get("_sub", "Perfil do usuário")


def salvar(nome, p, sub=None):
    if sub is None:
        try:
            sub = carregar(nome)[1]
        except Exception:
            sub = "Perfil do usuário"
    with open(perfil_path(nome), "w", encoding="utf-8") as f:
        json.dump(dict(p, _sub=sub), f, indent=2, ensure_ascii=False)


def limpar_nome(n):
    n = re.sub(r'[\\/:*?"<>|]', "", str(n or "")).strip()[:40]
    return n if n and not n.startswith("_") and not n.startswith(".") else ""


for _n, _sub, _p in PRESETS:
    if not os.path.exists(perfil_path(_n)):
        salvar(_n, _p, _sub)

CFG_DEFAULT = dict(tema="Roxo Neon", accent="#e040fb", ultimo=None, iniciar=False, reaplicar=True,
                   segundo_plano=False, update_url="", auto_update=False)


def load_cfg():
    try:
        with open(CFG_FILE, encoding="utf-8") as f:
            return dict(CFG_DEFAULT, **json.load(f))
    except Exception:
        return dict(CFG_DEFAULT)


def save_cfg():
    try:
        with open(CFG_FILE, "w", encoding="utf-8") as f:
            json.dump(CFG, f, indent=2)
    except Exception:
        pass


CFG = load_cfg()
TOKEN = secrets.token_hex(16)
LOCK = threading.Lock()
ST = dict(atual=None, values=dict(DEFAULTS), sub="", last_ping=0.0, bye_at=0.0, started=time.time())
SERVER = None


def apply_values(v):
    with LOCK:
        ok1 = apply_matrix(build_matrix(v))
        ok2 = apply_ramp(v["brilho"] / 100.0, v["contraste"] / 100.0, v["gama"] / 100.0)
    return ok1, ok2


def reset_screen():
    with LOCK:
        apply_matrix([[1, 0, 0, 0, 0], [0, 1, 0, 0, 0], [0, 0, 1, 0, 0], [0, 0, 0, 1, 0], [0, 0, 0, 0, 1]])
        apply_ramp(0.0, 1.0, 1.0)


def load_current():
    nomes = listar()
    ST["atual"] = CFG["ultimo"] if CFG["ultimo"] in nomes else nomes[0]
    ST["values"], ST["sub"] = carregar(ST["atual"])


def state_json():
    perfis = []
    for n in listar():
        try:
            sub = carregar(n)[1]
        except Exception:
            sub = "Perfil do usuário"
        perfis.append({"name": n, "sub": sub})
    return dict(version=APP_VERSION, atual=ST["atual"], values=ST["values"], sub=ST["sub"],
                profiles=perfis, cfg=CFG, defaults=DEFAULTS)


# ============================================================ Atualizacao automatica
def fetch_text(url, timeout=12):
    if not url.lower().startswith("https://"):
        raise ValueError("O link precisa começar com https://")
    req = urllib.request.Request(url, headers={"User-Agent": "ColorStudio-Updater", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def vtuple(v):
    return tuple(int(x) for x in v.split("."))


def inspect_update(url):
    src = fetch_text(url)
    m = re.search(r'^APP_VERSION\s*=\s*"([\d.]+)"', src, re.M)
    if not m or not src.lstrip().startswith("# COLOR-STUDIO"):
        raise ValueError("Esse link não é um arquivo do Color Studio.")
    compile(src, "update", "exec")
    return src, m.group(1), vtuple(m.group(1)) > vtuple(APP_VERSION)


def install_update(src):
    path = os.path.abspath(__file__)
    shutil.copy2(path, path + ".bak")
    tmp = path + ".new"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(src)
    os.replace(tmp, path)


def relaunch(with_window_existing):
    env = dict(os.environ)
    if with_window_existing:
        env["COLOR_STUDIO_RESTART"] = "1"
    subprocess.Popen([pythonw(), os.path.abspath(__file__)], env=env, cwd=HERE)


def restart_after_update():
    try:
        if SERVER:
            SERVER.shutdown()
            SERVER.server_close()
    except Exception:
        pass
    relaunch(True)
    os._exit(0)


def auto_update_on_start():
    if not (CFG.get("auto_update") and CFG.get("update_url")):
        return
    try:
        src, nv, novo = inspect_update(CFG["update_url"])
        if novo:
            install_update(src)
            relaunch(False)
            os._exit(0)
    except Exception:
        pass


# ============================================================ API
def clamp_bool(v):
    return bool(v)


def api_state(b):
    return state_json()


def api_ping(b):
    ST["last_ping"], ST["bye_at"] = time.time(), 0.0
    return {"ok": True}


def api_apply(b):
    v = sanitize(b.get("values"))
    ST["values"] = v
    ok1, ok2 = apply_values(v)
    return dict(ok1=ok1, ok2=ok2)


def api_save(b):
    v = sanitize(b.get("values"))
    ST["values"] = v
    salvar(ST["atual"], v)
    return {"ok": True}


def api_select(b):
    n = b.get("name")
    if n in listar():
        ST["atual"] = n
        ST["values"], ST["sub"] = carregar(n)
        CFG["ultimo"] = n
        save_cfg()
        apply_values(ST["values"])
    return state_json()


def api_new(b):
    n = limpar_nome(b.get("name"))
    if not n:
        return {"error": "Nome inválido."}
    if n in listar():
        return {"error": "Já existe um perfil com esse nome."}
    salvar(n, ST["values"], "Perfil do usuário")
    return api_select({"name": n})


def api_rename(b):
    n = limpar_nome(b.get("name"))
    if not n:
        return {"error": "Nome inválido."}
    if n in listar():
        return {"error": "Já existe um perfil com esse nome."}
    os.rename(perfil_path(ST["atual"]), perfil_path(n))
    ST["atual"] = CFG["ultimo"] = n
    save_cfg()
    return state_json()


def api_delete(b):
    if len(listar()) <= 1:
        return {"error": "Mantenha ao menos um perfil."}
    os.remove(perfil_path(ST["atual"]))
    return api_select({"name": listar()[0]})


def api_cfg(b):
    for k, v in b.items():
        if k == "tema" and isinstance(v, str):
            CFG[k] = v[:30]
        elif k == "accent" and re.fullmatch(r"#[0-9a-fA-F]{6}", str(v)):
            CFG[k] = v
        elif k == "iniciar":
            set_startup(clamp_bool(v))
            CFG[k] = clamp_bool(v)
        elif k in ("reaplicar", "segundo_plano", "auto_update"):
            CFG[k] = clamp_bool(v)
        elif k == "update_url":
            v = str(v).strip()
            if v and not v.lower().startswith("https://"):
                return {"error": "O link precisa começar com https://"}
            CFG[k] = v
    save_cfg()
    return {"ok": True, "cfg": CFG}


def api_openfolder(b):
    if IS_WIN:
        os.startfile(BASE)
    return {"ok": True}


def api_quit(b):
    threading.Timer(0.4, shutdown_app).start()
    return {"ok": True}


def api_update_check(b):
    url = CFG.get("update_url", "")
    if not url:
        return {"error": "Cole primeiro o link da atualização."}
    _, nv, novo = inspect_update(url)
    return dict(ok=True, atual=APP_VERSION, nova=nv, disponivel=novo)


def api_update_apply(b):
    src, nv, novo = inspect_update(CFG.get("update_url", ""))
    if not novo:
        return dict(ok=True, disponivel=False, nova=nv)
    install_update(src)
    threading.Timer(0.6, restart_after_update).start()
    return dict(ok=True, disponivel=True, nova=nv)


ROUTES = {
    "/api/state": api_state, "/api/ping": api_ping, "/api/apply": api_apply, "/api/save": api_save,
    "/api/select": api_select, "/api/new": api_new, "/api/rename": api_rename, "/api/delete": api_delete,
    "/api/cfg": api_cfg, "/api/openfolder": api_openfolder, "/api/quit": api_quit,
    "/api/update/check": api_update_check, "/api/update/apply": api_update_apply,
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _host_ok(self):
        h = self.headers.get("Host", "")
        return h.startswith("127.0.0.1") or h.startswith("localhost")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "forbidden", "text/plain")
        path = urlparse(self.path).path
        if path == "/":
            html = HTML.replace("__TOKEN__", TOKEN).replace("__VERSION__", APP_VERSION)
            return self._send(200, html, "text/html; charset=utf-8")
        if path == "/api/ping":
            return self._send(200, "colorstudio", "text/plain")
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        q = parse_qs(u.query)
        tok = self.headers.get("X-Token") or (q.get("t") or [""])[0]
        if tok != TOKEN:
            return self._send(403, {"error": "token"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            body = {}
        if u.path == "/api/bye":
            ST["bye_at"] = time.time()
            return self._send(200, {"ok": True})
        fn = ROUTES.get(u.path)
        if not fn:
            return self._send(404, {"error": "not found"})
        try:
            self._send(200, fn(body if isinstance(body, dict) else {}))
        except Exception as ex:
            self._send(200, {"error": str(ex)})


# ============================================================ Janela e ciclo de vida
def find_browser():
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    la = os.environ.get("LocalAppData", "")
    cands = [
        os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(la, r"Google\Chrome\Application\chrome.exe"),
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def open_window(port):
    url = f"http://127.0.0.1:{port}/"
    exe = find_browser()
    if exe:
        try:
            subprocess.Popen([exe, f"--app={url}", "--window-size=1180,820", "--no-first-run",
                              "--disable-features=Translate",
                              f"--user-data-dir={os.path.join(BASE, '_janela')}"])
            return
        except Exception:
            pass
    webbrowser.open(url)


def is_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
            return r.read() == b"colorstudio"
    except Exception:
        return False


def shutdown_app():
    try:
        salvar(ST["atual"], ST["values"])
    except Exception:
        pass
    reset_screen()
    if IS_WIN:
        try:
            mag.MagUninitialize()
        except Exception:
            pass
    os._exit(0)


def window_closed():
    ST["last_ping"], ST["bye_at"] = 0.0, 0.0
    if not CFG.get("segundo_plano"):
        shutdown_app()


def watchdog():
    while True:
        time.sleep(2)
        now = time.time()
        if ST["bye_at"] and now - ST["bye_at"] > 4:
            window_closed()
        elif ST["last_ping"] and now - ST["last_ping"] > 150:
            window_closed()
        elif not ST["last_ping"] and now - ST["started"] > 120 and not CFG.get("segundo_plano"):
            shutdown_app()


def main():
    restart = os.environ.get("COLOR_STUDIO_RESTART") == "1"
    nowin = restart or os.environ.get("COLOR_STUDIO_NOWINDOW") == "1"
    if restart:
        for _ in range(40):
            if not is_running(PORT):
                break
            time.sleep(0.25)
    elif is_running(PORT):
        open_window(PORT)
        return
    auto_update_on_start()

    global SERVER
    server, port = None, PORT
    for _ in range(30):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
            break
        except OSError:
            time.sleep(0.25)
    if server is None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
    server.daemon_threads = True
    SERVER = server

    if IS_WIN:
        mag.MagInitialize()
    load_current()
    if CFG.get("reaplicar"):
        apply_values(ST["values"])
    threading.Thread(target=watchdog, daemon=True).start()
    if not nowin:
        threading.Timer(0.4, open_window, (port,)).start()
    server.serve_forever()


# ============================================================ Interface (HTML)
HTML = r'''<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>Color Studio</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{
  --bg1:#2b12a8;--bg2:#6a1fc2;--panel:#140e5e;--fg:#f2f0ff;--muted:#a9a3ea;--acc:#e040fb;--acc2:#7c4dff;--border:#3a2cc0;
  --card:color-mix(in srgb,var(--panel) 62%,var(--bg1));
  --box:color-mix(in srgb,var(--panel) 70%,var(--bg1));
  --track:color-mix(in srgb,var(--box) 82%,var(--fg) 18%);
  --glow:color-mix(in srgb,var(--acc) 30%,transparent);
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{font-family:"Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;color:var(--fg);overflow:hidden;
  background:linear-gradient(155deg,var(--bg1),var(--bg2));user-select:none;font-size:14px}
#bg{position:fixed;inset:0;pointer-events:none;
  background:radial-gradient(620px 420px at 88% 6%,color-mix(in srgb,var(--acc) 38%,transparent),transparent 70%),
             radial-gradient(560px 560px at 4% 100%,color-mix(in srgb,var(--acc2) 40%,transparent),transparent 70%)}
#app{position:relative;height:100%;display:flex;flex-direction:column;padding:14px;gap:12px}
.glass{background:color-mix(in srgb,var(--panel) 84%,transparent);border:1px solid color-mix(in srgb,var(--border) 80%,transparent);
  border-radius:26px;backdrop-filter:blur(20px);box-shadow:0 14px 44px #0006,inset 0 1px 0 #ffffff16}
button{font:inherit;color:inherit;cursor:pointer}
/* cabecalho */
.bar{display:flex;align-items:center;gap:14px;height:66px;padding:0 18px;border-radius:999px;flex:none}
.logo{width:42px;height:42px;border-radius:14px;display:grid;place-items:center;font-weight:900;letter-spacing:.5px;color:#fff;
  background:linear-gradient(135deg,var(--acc),var(--acc2));box-shadow:0 0 0 1px #ffffff40 inset,0 6px 20px var(--glow)}
.brand{display:flex;flex-direction:column;line-height:1.15}.brand b{font-size:15px}.brand span{font-size:11px;color:var(--muted)}
.tag{font-size:10px;font-weight:800;letter-spacing:.1em;padding:5px 11px;border-radius:999px;color:var(--acc);
  background:color-mix(in srgb,var(--acc) 16%,transparent);border:1px solid color-mix(in srgb,var(--acc) 45%,transparent)}
.tabs{display:flex;gap:6px;margin-left:10px;padding:5px;border-radius:999px;background:color-mix(in srgb,var(--panel) 60%,#000)}
.tab{border:0;background:transparent;padding:9px 18px;border-radius:999px;font-weight:800;font-size:11.5px;letter-spacing:.09em;color:var(--muted);transition:.2s}
.tab:hover{color:var(--fg)}
.tab.on{color:#fff;background:linear-gradient(135deg,var(--acc),var(--acc2));box-shadow:0 4px 16px var(--glow)}
.sp{flex:1}
#dots{display:flex;gap:7px;align-items:center}
.dot-t{width:20px;height:20px;border-radius:50%;border:0;transition:.2s;box-shadow:0 0 0 1px #0004 inset}
.dot-t:hover{transform:scale(1.2)}
.dot-t.on{box-shadow:0 0 0 3px var(--panel),0 0 0 5px var(--fg)}
.pill{padding:9px 16px;border-radius:999px;font-size:11.5px;font-weight:700;letter-spacing:.04em;
  background:color-mix(in srgb,var(--panel) 60%,#000);border:1px solid var(--border)}
/* corpo */
.body{display:flex;gap:12px;flex:1;min-height:0}
.side{width:296px;flex:none;padding:18px 16px;display:flex;flex-direction:column;min-height:0}
.main{flex:1;min-width:0;overflow-y:auto;padding:26px 28px 30px}
h4{font-size:11px;letter-spacing:.14em;color:var(--muted);font-weight:800;margin:0 4px 10px}
#plist{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:10px;padding:4px 4px 10px;margin:0 -4px;min-height:0}
.pcard{display:flex;align-items:center;gap:13px;padding:12px 14px;border-radius:20px;cursor:pointer;transition:.18s;
  background:var(--card);border:1px solid color-mix(in srgb,var(--border) 70%,transparent)}
.pcard:hover{transform:translateY(-1px);background:color-mix(in srgb,var(--card) 86%,var(--fg) 14%)}
.pcard.sel{border-color:var(--acc);background:color-mix(in srgb,var(--card) 80%,var(--acc) 20%);
  box-shadow:0 0 0 1px var(--acc),0 10px 30px var(--glow)}
.badge{width:42px;height:42px;border-radius:14px;display:grid;place-items:center;font-weight:900;font-size:13px;color:#fff;flex:none;
  box-shadow:inset 0 1px 0 #ffffff55,inset 0 -8px 14px #0003,0 4px 12px #0004;text-shadow:0 1px 2px #0006}
.pcard b{display:block;font-size:14px}.pcard small{display:block;color:var(--muted);font-size:11.5px;margin-top:2px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:178px}
.btns{display:flex;gap:7px;margin:6px 0 8px}.btns .btn{flex:1;padding:10px 6px;font-size:12px}
.wide{width:100%;margin-bottom:14px}
.prefs{border-top:1px solid color-mix(in srgb,var(--border) 70%,transparent);padding-top:14px}
.pref{display:flex;align-items:center;gap:12px;margin:0 0 12px;font-size:12px;font-weight:700;line-height:1.25}
/* botoes */
.btn{font-weight:800;font-size:13px;padding:11px 20px;border-radius:999px;transition:.18s;
  border:1px solid color-mix(in srgb,var(--acc) 38%,var(--border));
  background:linear-gradient(180deg,color-mix(in srgb,var(--card) 78%,#fff 14%),var(--card));
  box-shadow:inset 0 1px 0 #ffffff2a,0 5px 16px #0004}
.btn:hover{border-color:var(--acc);transform:translateY(-1px);
  box-shadow:inset 0 1px 0 #ffffff40,0 0 0 4px var(--glow),0 8px 22px #0005}
.btn:active{transform:translateY(1px) scale(.97)}
.btn.primary{border-color:transparent;color:#fff;background:linear-gradient(135deg,var(--acc),var(--acc2));
  box-shadow:inset 0 1px 0 #ffffff50,0 6px 20px var(--glow)}
.btn.danger:hover{border-color:#fb3b4d;box-shadow:0 0 0 4px #fb3b4d40,0 8px 22px #0005}
.icon{border:0;background:color-mix(in srgb,var(--fg) 10%,transparent);width:34px;height:34px;border-radius:50%;font-size:15px;transition:.18s}
.icon:hover{background:var(--acc);color:#fff;box-shadow:0 0 0 4px var(--glow)}
.link{border:0;background:none;color:var(--muted);font-size:12px;text-decoration:underline;text-underline-offset:3px}
.link:hover{color:var(--fg)}
/* switch */
.sw{position:relative;width:48px;height:27px;border-radius:99px;flex:none;border:1px solid var(--border);background:var(--track);transition:.22s}
.sw::after{content:"";position:absolute;top:2px;left:2px;width:21px;height:21px;border-radius:50%;background:#fff;box-shadow:0 2px 6px #0006;transition:.22s}
.sw.on{border-color:transparent;background:linear-gradient(90deg,var(--acc2),var(--acc));box-shadow:0 0 14px var(--glow)}
.sw.on::after{left:23px}
/* conteudo */
.titlerow{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:6px}
.ttl{display:flex;align-items:center;gap:12px;font-size:34px;font-weight:900;letter-spacing:-.5px}
.subttl{color:var(--muted);font-size:13px;margin-top:2px}
.box{margin-top:18px;padding:20px 22px 14px;border-radius:26px;background:color-mix(in srgb,var(--box) 88%,transparent);
  border:1px solid color-mix(in srgb,var(--border) 75%,transparent);box-shadow:inset 0 1px 0 #ffffff10}
.box h3{font-size:12px;letter-spacing:.15em;font-weight:900;display:flex;align-items:center;gap:10px}
.box h3::before{content:"";width:6px;height:16px;border-radius:9px;background:linear-gradient(var(--acc),var(--acc2))}
.desc{color:var(--muted);font-size:12.5px;line-height:1.5;margin:8px 0 10px}
.row{display:grid;grid-template-columns:210px 1fr 62px;align-items:center;gap:16px;padding:10px 0}
.row .l b{display:block;font-size:14px}.row .l small{color:var(--muted);font-size:11.5px}
.val{text-align:center;font-weight:900;font-size:14px;color:var(--acc);padding:6px 0;border-radius:12px;
  background:color-mix(in srgb,var(--acc) 13%,transparent);border:1px solid color-mix(in srgb,var(--acc) 30%,transparent)}
.note{margin:6px 0 8px;padding:13px 16px;border-radius:18px;font-size:12.5px;line-height:1.5;
  background:color-mix(in srgb,var(--acc) 10%,transparent);border:1px solid color-mix(in srgb,var(--acc) 40%,transparent)}
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:28px;background:transparent;cursor:pointer;--r:.5}
input[type=range]::-webkit-slider-runnable-track{height:9px;border-radius:99px;
  background:linear-gradient(90deg,var(--acc2),var(--acc)) 0 0/calc(11px + (100% - 22px)*var(--r)) 100% no-repeat,var(--track);
  box-shadow:inset 0 1px 3px #0006}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:22px;height:22px;margin-top:-6.5px;border-radius:50%;background:#fff;
  border:4px solid var(--acc);box-shadow:0 0 0 6px var(--glow),0 4px 12px #0007;transition:box-shadow .15s}
input[type=range]:hover::-webkit-slider-thumb{box-shadow:0 0 0 9px var(--glow),0 4px 12px #0007}
.inp{width:100%;padding:12px 16px;border-radius:16px;border:1px solid var(--border);background:var(--track);color:var(--fg);font:inherit;user-select:text;outline:0}
.inp:focus{border-color:var(--acc);box-shadow:0 0 0 4px var(--glow)}
.tgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:16px;margin:14px 0 10px}
.tcard{height:116px;border-radius:22px;overflow:hidden;cursor:pointer;position:relative;border:2px solid color-mix(in srgb,var(--border) 70%,transparent);
  transition:.2s;box-shadow:0 8px 22px #0004}
.tcard:hover{transform:translateY(-3px)}
.tcard.on{border-color:var(--acc);box-shadow:0 0 0 3px var(--glow),0 10px 28px #0005}
.tcard .top{height:68px;padding:14px 16px;font-weight:900;font-size:14px}
.tcard .bot{height:48px;display:flex;align-items:center;gap:8px;padding:0 16px}
.tcard .bot i{display:block;border-radius:99px;height:12px}
.tcard .ck{position:absolute;top:10px;right:10px;width:24px;height:24px;border-radius:50%;display:none;place-items:center;font-size:13px;color:#fff;font-weight:900}
.tcard.on .ck{display:grid}
.colorrow{display:flex;align-items:center;gap:14px;margin:10px 0 14px}
input[type=color]{-webkit-appearance:none;border:0;width:56px;height:44px;border-radius:16px;padding:0;background:none;cursor:pointer}
input[type=color]::-webkit-color-swatch-wrapper{padding:0}input[type=color]::-webkit-color-swatch{border:3px solid #fff;border-radius:16px}
.urlrow{display:flex;gap:10px;margin:10px 0}.urlrow .inp{flex:1}
.res{font-size:13px;margin:8px 0 12px;min-height:18px;color:var(--muted)}
.res.ok{color:#4ade80}.res.err{color:#f87171}
/* rodape / modal / toast */
.foot{display:flex;align-items:center;gap:10px;font-size:12px;color:var(--muted);padding:0 14px;flex:none;height:22px}
.sdot{width:10px;height:10px;border-radius:50%;background:#4ade80;box-shadow:0 0 10px #4ade80}.sdot.bad{background:#f87171;box-shadow:0 0 10px #f87171}
#modal{display:none}#modal.show{display:block}
.mback{position:fixed;inset:0;background:#0009;backdrop-filter:blur(6px);display:grid;place-items:center;z-index:50}
.mbox{width:min(420px,90vw);padding:26px;border-radius:28px}.mbox h2{font-size:20px;margin-bottom:6px}.mbox p{color:var(--muted);margin-bottom:14px}
.mbtn{display:flex;justify-content:flex-end;gap:10px;margin-top:20px}
#toast{position:fixed;left:50%;bottom:34px;transform:translate(-50%,30px);opacity:0;transition:.3s;padding:12px 22px;border-radius:999px;z-index:60;
  background:var(--panel);border:1px solid var(--acc);box-shadow:0 10px 30px #0008;font-weight:700;pointer-events:none}
#toast.show{opacity:1;transform:translate(-50%,0)}
#over{position:fixed;inset:0;display:none;place-items:center;text-align:center;z-index:99;background:#000c;backdrop-filter:blur(10px);font-size:18px;font-weight:800;padding:30px}
#over.show{display:grid}
::-webkit-scrollbar{width:11px}::-webkit-scrollbar-thumb{background:color-mix(in srgb,var(--fg) 22%,transparent);border-radius:99px;border:3px solid transparent;background-clip:content-box}
@media (max-width:900px){.row{grid-template-columns:1fr;gap:4px}.val{width:62px}}
</style></head><body>
<div id="bg"></div>
<div id="app">
  <header class="bar glass">
    <div class="logo">CS</div>
    <div class="brand"><b>Color Studio</b><span id="ver"></span></div>
    <span class="tag">EDIÇÃO LIVRE</span>
    <nav class="tabs">
      <button class="tab on" data-tab="ajustes">AJUSTES</button>
      <button class="tab" data-tab="personalizacao">PERSONALIZAÇÃO</button>
    </nav>
    <div class="sp"></div>
    <div id="dots"></div>
    <div class="pill" id="mon"></div>
  </header>
  <div class="body">
    <aside class="side glass">
      <h4>PERFIS</h4>
      <div id="plist"></div>
      <div class="btns">
        <button class="btn" id="bnew">+ Novo</button><button class="btn" id="bren">Renomear</button><button class="btn danger" id="bdel">Excluir</button>
      </div>
      <button class="btn wide" id="bfolder">Abrir pasta de perfis</button>
      <div class="prefs">
        <h4>PREFERÊNCIAS</h4>
        <div class="pref"><div class="sw" data-k="iniciar"></div><span>Iniciar com o Windows</span></div>
        <div class="pref"><div class="sw" data-k="reaplicar"></div><span>Reaplicar a última configuração ao iniciar</span></div>
        <div class="pref"><div class="sw" data-k="segundo_plano"></div><span>Continuar ativo ao fechar a janela</span></div>
      </div>
    </aside>
    <main class="main glass"><div id="tab"></div></main>
  </div>
  <footer class="foot"><span class="sdot" id="sdot"></span><span id="status">Iniciando…</span><span class="sp"></span>
    <button class="link" id="quit">Encerrar e restaurar as cores</button></footer>
</div>
<div id="modal"></div><div id="toast"></div><div id="over"></div>
<script>
const TOKEN="__TOKEN__", VERSION="__VERSION__";
const THEMES={
 "Roxo Neon":{bg1:"#2b12a8",bg2:"#6a1fc2",panel:"#140e5e",fg:"#f2f0ff",muted:"#a9a3ea",acc:"#e040fb",acc2:"#7c4dff",border:"#3a2cc0"},
 "Ciano Gelo":{bg1:"#062a44",bg2:"#0a6a8a",panel:"#061a2e",fg:"#eaf9ff",muted:"#8fc3d9",acc:"#22d3ee",acc2:"#3b82f6",border:"#14527a"},
 "Verde Matrix":{bg1:"#052e16",bg2:"#0b6a30",panel:"#031a0c",fg:"#eafff1",muted:"#8fd1a6",acc:"#4ade80",acc2:"#16a34a",border:"#166534"},
 "Vermelho Gamer":{bg1:"#3b0710",bg2:"#8a1224",panel:"#220408",fg:"#fff0f1",muted:"#e09aa2",acc:"#fb3b4d",acc2:"#ff8a3d",border:"#7a1a2a"},
 "Pôr do Sol":{bg1:"#3a1204",bg2:"#9a3d0c",panel:"#240a02",fg:"#fff4ea",muted:"#e0b08c",acc:"#fb923c",acc2:"#f43f5e",border:"#8a4a1a"},
 "AMOLED":{bg1:"#000000",bg2:"#121212",panel:"#050505",fg:"#fafafa",muted:"#8a8a8a",acc:"#e5e5e5",acc2:"#737373",border:"#2a2a2a"},
 "Claro":{bg1:"#e8ecff",bg2:"#c6d0ff",panel:"#f6f8ff",fg:"#1b1b3a",muted:"#6b6f99",acc:"#6366f1",acc2:"#a855f7",border:"#c5cdf5"}
};
const CORES=[
 ["saturacao","Saturação",0,200,100,"100 = original · até 200"],
 ["intensidade","Intensidade das cores",50,150,100,"Força geral das cores"],
 ["temperatura","Temperatura de cor",-100,100,0,"Menor = fria · maior = quente"],
 ["vermelho","Vermelho (R)",0,200,100,"Intensidade do canal vermelho"],
 ["verde","Verde (G)",0,200,100,"Intensidade do canal verde"],
 ["azul","Azul (B)",0,200,100,"Intensidade do canal azul"],
 ["tonalidade","Tonalidade",-180,180,0,"Rotação de matiz"]];
const IMAGEM=[
 ["brilho","Brilho",-50,50,0,"Mais claro / mais escuro"],
 ["contraste","Contraste",50,150,100,"Diferença entre claros e escuros"],
 ["gama","Gama",50,200,100,"Curva de gama da tela"]];
const BADGES=[["#ff3dac","#a855f7"],["#a855f7","#6366f1"],["#22d3ee","#3b82f6"],["#3b82f6","#6366f1"],["#fbbf24","#f97316"],["#4ade80","#16a34a"],["#fb7185","#ef4444"]];

const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const h=html=>{const t=document.createElement("template");t.innerHTML=html.trim();return t.content.firstElementChild};
const esc=s=>String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const hex2=x=>[1,3,5].map(i=>parseInt(x.slice(i,i+2),16));
const toHex=a=>"#"+a.map(v=>Math.round(Math.max(0,Math.min(255,v))).toString(16).padStart(2,"0")).join("");
const mixc=(a,b,t)=>{const x=hex2(a),y=hex2(b);return toHex(x.map((v,i)=>v+(y[i]-v)*t))};
function shiftHue(hex,d){let [r,g,b]=hex2(hex).map(v=>v/255);const mx=Math.max(r,g,b),mn=Math.min(r,g,b);let H=0,Sa=0,L=(mx+mn)/2;const dd=mx-mn;
 if(dd){Sa=L>.5?dd/(2-mx-mn):dd/(mx+mn);H=mx==r?(g-b)/dd+(g<b?6:0):mx==g?(b-r)/dd+2:(r-g)/dd+4;H/=6}
 H=(H+d)%1;if(!Sa)return hex;const q=L<.5?L*(1+Sa):L+Sa-L*Sa,p=2*L-q;
 const f=t=>{t=(t+1)%1;return t<1/6?p+(q-p)*6*t:t<1/2?q:t<2/3?p+(q-p)*(2/3-t)*6:p};return toHex([f(H+1/3)*255,f(H)*255,f(H-1/3)*255])}
function fromAccent(acc){const k="#000000";return{bg1:mixc(k,acc,.30),bg2:mixc(k,acc,.5),panel:mixc(k,acc,.10),fg:"#f4f4ff",muted:mixc(acc,"#ffffff",.55),acc:acc,acc2:shiftHue(acc,.1),border:mixc(k,acc,.38)}}
const themeOf=(n,acc)=>n==="Personalizado"?fromAccent(acc):(THEMES[n]||THEMES["Roxo Neon"]);
function applyTheme(){const t=themeOf(S.cfg.tema,S.cfg.accent),r=document.documentElement.style;for(const k in t)r.setProperty("--"+k,t[k])}

let S=null, tab="ajustes", alive=true;
async function api(path,body){
  const r=await fetch(path,{method:"POST",headers:{"X-Token":TOKEN,"Content-Type":"application/json"},body:JSON.stringify(body||{})});
  return r.json();
}
function toast(msg){const t=$("#toast");t.textContent=msg;t.classList.add("show");clearTimeout(toast.t);toast.t=setTimeout(()=>t.classList.remove("show"),2600)}
function overlay(msg){const o=$("#over");o.textContent=msg;o.classList.add("show")}
function setStatus(r){
  const ok=r&&r.ok1&&r.ok2,d=$("#sdot");
  d.classList.toggle("bad",!(r&&r.ok1));
  $("#status").textContent=!r?"Pronto":ok?"Aplicado — as cores ficam ativas em toda a tela"+(S.cfg.reaplicar?" e serão reaplicadas ao iniciar o Color Studio":""):
    r.ok1?"Cores aplicadas — a tela/driver recusou brilho, contraste e gama":"Falha ao aplicar as cores";
}
function modal(o){return new Promise(res=>{
  const m=$("#modal");
  m.innerHTML=`<div class="mback"><div class="mbox glass"><h2>${esc(o.title)}</h2>${o.label?`<p>${esc(o.label)}</p>`:""}${o.input?`<input class="inp" id="mi" maxlength="40" value="${esc(o.value||"")}">`:""}
  <div class="mbtn"><button class="btn" id="mc">Cancelar</button><button class="btn ${o.danger?"danger":"primary"}" id="mo">${o.ok||"OK"}</button></div></div></div>`;
  m.classList.add("show");const inp=$("#mi");if(inp){inp.focus();inp.select()}
  const close=v=>{m.classList.remove("show");m.innerHTML="";res(v)};
  $("#mc").onclick=()=>close(null);$("#mo").onclick=()=>close(o.input?inp.value:true);
  m.onkeydown=e=>{if(e.key==="Enter")$("#mo").click();if(e.key==="Escape")close(null)};
})}

/* ---- aplicar valores (sem acumular requisicoes) ---- */
let applying=false,dirty=false,saveT=null;
async function pump(){
  if(applying){dirty=true;return}applying=true;
  do{dirty=false;try{setStatus(await api("/api/apply",{values:S.values}))}catch(e){}}while(dirty);
  applying=false;
}
function changed(){pump();clearTimeout(saveT);saveT=setTimeout(()=>api("/api/save",{values:S.values}),400)}

/* ---- renderizacao ---- */
function renderHeader(){
  $("#ver").textContent="versão "+VERSION;
  $("#mon").textContent="MONITOR  1 · "+Math.round(screen.width*devicePixelRatio)+"×"+Math.round(screen.height*devicePixelRatio);
  const names=[...Object.keys(THEMES),"Personalizado"];
  $("#dots").innerHTML="";
  names.forEach(n=>{const t=themeOf(n,S.cfg.accent);const b=h(`<button class="dot-t ${n===S.cfg.tema?"on":""}" title="${esc(n)}" style="background:linear-gradient(135deg,${t.acc},${t.acc2})"></button>`);
    b.onclick=()=>setTheme(n);$("#dots").appendChild(b)});
  $$(".tab").forEach(t=>t.classList.toggle("on",t.dataset.tab===tab));
  $$(".sw[data-k]").forEach(s=>s.classList.toggle("on",!!S.cfg[s.dataset.k]));
}
function renderProfiles(){
  const l=$("#plist");l.innerHTML="";
  S.profiles.forEach(p=>{
    const c=BADGES[[...p.name].reduce((a,ch)=>a+ch.charCodeAt(0),0)%BADGES.length];
    const w=p.name.split(/\s+/);const ini=(w.length>1?w[0][0]+w[1][0]:p.name.slice(0,2)).toUpperCase();
    const el=h(`<div class="pcard ${p.name===S.atual?"sel":""}"><div class="badge" style="background:linear-gradient(135deg,${c[0]},${c[1]})">${esc(ini)}</div><div><b>${esc(p.name)}</b><small>${esc(p.sub)}</small></div></div>`);
    el.onclick=()=>selectProfile(p.name);l.appendChild(el);
  });
}
function sliderRow(p){
  const [key,label,lo,hi,def,sub]=p;
  const row=h(`<div class="row"><div class="l"><b>${label}</b><small>${sub}</small></div><input type="range" min="${lo}" max="${hi}" step="1"><div class="val"></div></div>`);
  const inp=$("input",row),val=$(".val",row);
  const paint=()=>{inp.style.setProperty("--r",(inp.value-lo)/(hi-lo));val.textContent=inp.value};
  inp.value=S.values[key];paint();
  inp.oninput=()=>{S.values[key]=+inp.value;paint();changed()};
  inp.ondblclick=()=>{inp.value=def;inp.dispatchEvent(new Event("input"))};
  return row;
}
function renderAjustes(){
  const t=$("#tab");t.innerHTML="";
  t.appendChild(h(`<div class="titlerow"><div><div class="ttl"><span>${esc(S.atual)}</span><button class="icon" id="pen" title="Renomear">✎</button></div>
    <div class="subttl">${esc(S.sub)} · global</div></div><button class="btn" id="cmp">Segure para comparar</button></div>`));
  const b1=h(`<section class="box"><h3>CORES</h3><p class="desc">Temperatura, intensidade e RGB aplicados pelo compositor do Windows, sem custo de desempenho. Dê dois cliques em um controle para voltar ao valor original.</p></section>`);
  CORES.forEach(p=>{b1.appendChild(sliderRow(p));
    if(p[0]==="saturacao")b1.appendChild(h(`<div class="note">Saturação avançada ativa — aplicada na área de trabalho, janelas e jogos em janela / tela cheia sem bordas, em todos os monitores. Não aparece em tela cheia exclusiva.</div>`))});
  t.appendChild(b1);
  const b2=h(`<section class="box"><h3>IMAGEM</h3><p class="desc">Tabela de gama da tela — aplicada na saída de vídeo, sem custo de FPS e sem latência.</p></section>`);
  IMAGEM.forEach(p=>b2.appendChild(sliderRow(p)));
  b2.appendChild(h(`<div style="padding:10px 0 8px"><button class="btn" id="reset">Resetar este perfil</button></div>`));
  t.appendChild(b2);
  $("#pen").onclick=renameProfile;
  $("#reset").onclick=()=>{S.values={...S.defaults};renderAjustes();changed()};
  const cmp=$("#cmp");
  const off=()=>api("/api/apply",{values:S.defaults}),on=()=>api("/api/apply",{values:S.values});
  cmp.onpointerdown=e=>{cmp.setPointerCapture(e.pointerId);off()};cmp.onpointerup=on;cmp.onpointercancel=on;
}
function renderPersonalizacao(){
  const t=$("#tab");t.innerHTML="";
  t.appendChild(h(`<div class="titlerow"><div><div class="ttl">Personalização</div><div class="subttl">Escolha o visual do aplicativo</div></div></div>`));
  const bt=h(`<section class="box"><h3>TEMAS</h3><p class="desc">Clique em um tema para aplicar. Também dá para trocar pelas bolinhas coloridas no topo.</p><div class="tgrid"></div></section>`);
  [...Object.keys(THEMES),"Personalizado"].forEach(n=>{
    const th=themeOf(n,S.cfg.accent);
    const c=h(`<div class="tcard ${n===S.cfg.tema?"on":""}"><div class="top" style="color:${th.fg};background:linear-gradient(160deg,${th.bg1},${th.bg2})">${n}</div>
      <div class="bot" style="background:${th.panel}"><i style="width:44%;background:linear-gradient(90deg,${th.acc2},${th.acc})"></i><i style="width:12px;background:${th.acc}"></i><i style="width:12px;background:${th.acc2}"></i></div>
      <div class="ck" style="background:${th.acc}">✓</div></div>`);
    c.onclick=()=>setTheme(n);$(".tgrid",bt).appendChild(c)});
  t.appendChild(bt);
  const bc=h(`<section class="box"><h3>COR DE DESTAQUE PERSONALIZADA</h3><p class="desc">Crie seu próprio tema a partir de uma única cor.</p>
    <div class="colorrow"><input type="color" id="pick" value="${S.cfg.accent}"><button class="btn" id="usepick">Usar esta cor</button></div></section>`);
  t.appendChild(bc);
  $("#usepick").onclick=()=>{S.cfg.accent=$("#pick").value;api("/api/cfg",{accent:S.cfg.accent});setTheme("Personalizado")};
  const bu=h(`<section class="box"><h3>ATUALIZAÇÃO AUTOMÁTICA</h3>
    <p class="desc">Versão instalada: <b>${VERSION}</b>. Cole o link “raw” do arquivo color_studio.pyw (GitHub ou Gist). Com a atualização automática ligada, o app se atualiza sozinho ao abrir — sem precisar baixar nada.</p>
    <div class="urlrow"><input class="inp" id="url" placeholder="https://raw.githubusercontent.com/seu-usuario/seu-repo/main/color_studio.pyw" value="${esc(S.cfg.update_url||"")}"><button class="btn" id="usave">Salvar link</button></div>
    <div class="pref" style="margin:12px 0"><div class="sw ${S.cfg.auto_update?"on":""}" id="autoupd"></div><span>Atualizar automaticamente ao abrir</span></div>
    <div style="display:flex;gap:10px"><button class="btn primary" id="ucheck">Verificar atualização</button><button class="btn" id="uapply" style="display:none">Atualizar agora</button></div>
    <div class="res" id="ures"></div></section>`);
  t.appendChild(bu);
  const res=$("#ures");
  $("#usave").onclick=async()=>{const r=await api("/api/cfg",{update_url:$("#url").value});if(r.error){res.className="res err";res.textContent=r.error}else{S.cfg.update_url=$("#url").value.trim();toast("Link salvo")}};
  $("#autoupd").onclick=async e=>{const on=!e.currentTarget.classList.contains("on");e.currentTarget.classList.toggle("on",on);S.cfg.auto_update=on;await api("/api/cfg",{auto_update:on})};
  $("#ucheck").onclick=async()=>{
    await $("#usave").onclick();res.className="res";res.textContent="Verificando…";
    const r=await api("/api/update/check");
    if(r.error){res.className="res err";res.textContent=r.error;return}
    if(r.disponivel){res.className="res ok";res.textContent="Nova versão disponível: "+r.nova;$("#uapply").style.display=""}
    else{res.className="res ok";res.textContent="Você já está na versão mais recente ("+r.atual+").";$("#uapply").style.display="none"}};
  $("#uapply").onclick=async()=>{overlay("Atualizando o Color Studio…");const r=await api("/api/update/apply");if(r.error){$("#over").classList.remove("show");toast(r.error)}};
}
function render(){applyTheme();renderHeader();renderProfiles();
  if(render.last!==tab){$(".main").scrollTop=0;render.last=tab}
  tab==="ajustes"?renderAjustes():renderPersonalizacao()}

/* ---- acoes ---- */
async function setTheme(n){S.cfg.tema=n;await api("/api/cfg",{tema:n});render()}
async function selectProfile(n){tab="ajustes";S=Object.assign(S,await api("/api/select",{name:n}));render();setStatus(await api("/api/apply",{values:S.values}))}
async function newProfile(){
  const n=await modal({title:"Novo perfil",label:"Ele começa com os ajustes atuais.",input:true,ok:"Criar"});if(!n)return;
  const r=await api("/api/new",{name:n});if(r.error)return toast(r.error);tab="ajustes";S=Object.assign(S,r);render()}
async function renameProfile(){
  const n=await modal({title:"Renomear perfil",input:true,value:S.atual,ok:"Salvar"});if(!n||n===S.atual)return;
  const r=await api("/api/rename",{name:n});if(r.error)return toast(r.error);S=Object.assign(S,r);render()}
async function deleteProfile(){
  if(!await modal({title:"Excluir perfil",label:`Excluir "${S.atual}"? Isso não pode ser desfeito.`,ok:"Excluir",danger:true}))return;
  const r=await api("/api/delete");if(r.error)return toast(r.error);S=Object.assign(S,r);render();setStatus(await api("/api/apply",{values:S.values}))}

async function init(){
  S=await api("/api/state");applyTheme();
  $$(".tab").forEach(t=>t.onclick=()=>{tab=t.dataset.tab;render()});
  $("#bnew").onclick=newProfile;$("#bren").onclick=renameProfile;$("#bdel").onclick=deleteProfile;
  $("#bfolder").onclick=()=>api("/api/openfolder");
  $$(".sw[data-k]").forEach(s=>s.onclick=async()=>{const k=s.dataset.k,v=!s.classList.contains("on");
    const r=await api("/api/cfg",{[k]:v});if(r.error)return toast(r.error);S.cfg[k]=v;s.classList.toggle("on",v);
    if(k==="segundo_plano"&&v)toast("Ao fechar a janela, abra o atalho de novo para voltar aqui")});
  $("#quit").onclick=async()=>{alive=false;await api("/api/quit");overlay("Color Studio encerrado. As cores voltaram ao normal — você já pode fechar esta janela.");try{window.close()}catch(e){}};
  render();setStatus(await api("/api/apply",{values:S.values}));
  let fails=0;
  setInterval(async()=>{if(!alive)return;
    try{await api("/api/ping");if(fails>=2)location.reload();fails=0}catch(e){fails++;if(fails>=2)overlay("Reconectando…")}},2000);
  window.addEventListener("pagehide",()=>{if(alive)navigator.sendBeacon("/api/bye?t="+TOKEN)});
}
init();
</script></body></html>
'''

if __name__ == "__main__":
    main()
