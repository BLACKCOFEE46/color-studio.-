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
APP_VERSION = "5.0"

import csv
import ctypes
import fnmatch
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
                   segundo_plano=False, update_url="", auto_update=False,
                   jogos=["cs2.exe", "GTAProcess"], auto_prioridade=True)


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
                profiles=perfis, cfg=CFG, defaults=DEFAULTS, admin=is_admin())


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


# ============================================================ Otimizacao do sistema
CREATE_NO_WINDOW = 0x08000000
BACKUP_FILE = os.path.join(BASE, "_backup.json")
REG_SZ, REG_BINARY, REG_DWORD = 1, 3, 4
FAKE_REG = {}          # so usado na previa fora do Windows

if IS_WIN:
    import winreg
    k32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    shell32 = ctypes.windll.shell32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    k32.GetTickCount64.restype = ctypes.c_ulonglong
    psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    shell32.ShellExecuteW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                      ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
    HIVES = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}


def run(cmd, timeout=25):
    kw = {"creationflags": CREATE_NO_WINDOW} if IS_WIN else {}
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout, **kw)
    return r.returncode, r.stdout or "", r.stderr or ""


def is_admin():
    if not IS_WIN:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def fmt_bytes(n):
    if n >= 2 ** 30:
        return "%.1f GB" % (n / 2 ** 30)
    if n >= 2 ** 20:
        return "%d MB" % round(n / 2 ** 20)
    if n >= 1024:
        return "%d KB" % round(n / 1024)
    return "%d B" % n


# ---------------------------------------------------- estatisticas
class _MEMSTATUS(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class _FT(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]


_last_cpu = [None]


def mem_info():
    if IS_WIN:
        m = _MEMSTATUS()
        m.dwLength = ctypes.sizeof(m)
        k32.GlobalMemoryStatusEx(ctypes.byref(m))
        return int(m.ullTotalPhys), int(m.ullTotalPhys - m.ullAvailPhys)
    return 16 * 2 ** 30, int(9.4 * 2 ** 30)


def cpu_pct():
    if not IS_WIN:
        return 23
    i, k, u = _FT(), _FT(), _FT()
    k32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u))
    cur = tuple((x.hi << 32) | x.lo for x in (i, k, u))
    prev, _last_cpu[0] = _last_cpu[0], cur
    if not prev:
        return 0
    di, dk, du = (cur[n] - prev[n] for n in range(3))
    tot = dk + du
    return max(0, min(100, int(100 * (tot - di) / tot))) if tot > 0 else 0


def uptime_s():
    return int(k32.GetTickCount64() / 1000) if IS_WIN else 93600


def disk_info():
    root = (os.environ.get("SystemDrive", "C:") + "\\") if IS_WIN else "/"
    return shutil.disk_usage(root)


def mem_release():
    """Reduz o working set dos processos (EmptyWorkingSet). Nao fecha nada."""
    before = mem_info()[1]
    n = 0
    if IS_WIN:
        arr = (ctypes.c_ulong * 4096)()
        need = ctypes.c_ulong()
        psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(need))
        me = os.getpid()
        for pid in arr[: need.value // ctypes.sizeof(ctypes.c_ulong)]:
            if pid in (0, 4, me):
                continue
            h = k32.OpenProcess(0x0100 | 0x0400, False, pid)   # SET_QUOTA | QUERY_INFORMATION
            if h:
                try:
                    if psapi.EmptyWorkingSet(h):
                        n += 1
                finally:
                    k32.CloseHandle(h)
        time.sleep(0.6)
        freed = max(0, before - mem_info()[1])
    else:
        n, freed = 137, 412 * 2 ** 20
    return dict(freed=freed, procs=n)


# ---------------------------------------------------- processos
PROTECTED = {
    "system", "system idle process", "registry", "memory compression", "smss.exe", "csrss.exe", "wininit.exe",
    "winlogon.exe", "services.exe", "lsass.exe", "lsaiso.exe", "svchost.exe", "dwm.exe", "explorer.exe",
    "fontdrvhost.exe", "sihost.exe", "ctfmon.exe", "taskhostw.exe", "audiodg.exe", "runtimebroker.exe",
    "textinputhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe", "searchhost.exe",
    "securityhealthservice.exe", "msmpeng.exe", "conhost.exe", "wudfhost.exe", "dllhost.exe", "spoolsv.exe",
    "smartscreen.exe", "applicationframehost.exe", "systemsettings.exe", "python.exe", "pythonw.exe",
}
PRIO = {"baixa": 0x40, "abaixo": 0x4000, "normal": 0x20, "acima": 0x8000, "alta": 0x80}
NAME_RE = r"[\w .()\[\]\-]{1,80}"

FAKE_PROCS = (
    [("chrome.exe", 4100 + i, (170 + i * 28) * 2 ** 20) for i in range(9)]
    + [("cs2.exe", 6000, 2800 * 2 ** 20), ("Discord.exe", 5000, 420 * 2 ** 20), ("Teams.exe", 5300, 380 * 2 ** 20),
       ("steamwebhelper.exe", 5101, 310 * 2 ** 20), ("steam.exe", 5100, 260 * 2 ** 20),
       ("explorer.exe", 900, 210 * 2 ** 20), ("OneDrive.exe", 5200, 120 * 2 ** 20),
       ("svchost.exe", 800, 90 * 2 ** 20), ("Spotify.exe", 5400, 240 * 2 ** 20)]
)


def proc_rows():
    if not IS_WIN:
        return FAKE_PROCS
    rc, out, _ = run(["tasklist", "/fo", "csv", "/nh"], 25)
    rows = []
    for r in csv.reader(out.splitlines()):
        if len(r) >= 5 and r[1].isdigit():
            rows.append((r[0], int(r[1]), int(re.sub(r"\D", "", r[4]) or 0) * 1024))
    return rows


def host_browser():
    b = find_browser()
    return os.path.basename(b).lower() if b else ""


def set_prio_pid(pid, cls):
    h = k32.OpenProcess(0x0200 | 0x1000, False, pid)   # SET_INFORMATION | QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        return bool(k32.SetPriorityClass(h, cls))
    finally:
        k32.CloseHandle(h)


def check_name(name):
    if not isinstance(name, str) or not re.fullmatch(NAME_RE, name):
        raise ValueError("Nome inválido.")
    n = name.lower()
    if n in PROTECTED or n == host_browser():
        raise ValueError("Esse processo é protegido para não derrubar o Windows nem este app.")
    return n


def kill_by_name(name):
    n = check_name(name)
    pids = [pid for nm, pid, _ in proc_rows() if nm.lower() == n]
    if not pids:
        raise ValueError("Esse processo já não está em execução.")
    if not IS_WIN:
        return len(pids)
    ok = 0
    for pid in pids:
        rc, _, _ = run(["taskkill", "/F", "/PID", str(pid)], 10)
        ok += rc == 0
    if not ok:
        raise PermissionError("Não foi possível fechar (pode exigir administrador).")
    return ok


def prio_by_name(name, level):
    n = check_name(name)
    if level not in PRIO:
        raise ValueError("Prioridade inválida.")
    pids = [pid for nm, pid, _ in proc_rows() if nm.lower() == n]
    if not pids:
        raise ValueError("Esse processo já não está em execução.")
    if not IS_WIN:
        return len(pids)
    ok = sum(1 for pid in pids if set_prio_pid(pid, PRIO[level]))
    if not ok:
        raise PermissionError("O Windows recusou a mudança (pode exigir administrador).")
    return ok


BG_APPS = ["OneDrive.exe", "Teams.exe", "ms-teams.exe", "Skype.exe", "YourPhone.exe", "PhoneExperienceHost.exe",
           "GameBar.exe", "GameBarFTServer.exe", "Widgets.exe", "WidgetService.exe", "EpicWebHelper.exe"]


def bg_scan():
    want = {a.lower(): a for a in BG_APPS}
    found = {}
    for nm, pid, mem in proc_rows():
        if nm.lower() in want:
            d = found.setdefault(nm.lower(), dict(name=want[nm.lower()], mem=0, pids=[]))
            d["mem"] += mem
            d["pids"].append(pid)
    return list(found.values())


def watcher_loop():
    seen = set()
    while True:
        time.sleep(6)
        try:
            pats = [g.lower() for g in CFG.get("jogos", []) if g]
            if not (CFG.get("auto_prioridade") and pats):
                continue
            alive = set()
            for nm, pid, _ in proc_rows():
                if nm.lower() not in PROTECTED and any(p in nm.lower() for p in pats):
                    alive.add(pid)
                    if pid not in seen:
                        set_prio_pid(pid, PRIO["alta"])
            seen = alive
        except Exception:
            pass


# ---------------------------------------------------- registro
def reg_get(hive, path, name):
    if not IS_WIN:
        return FAKE_REG.get((hive, path.lower(), name.lower()))
    try:
        with winreg.OpenKey(HIVES[hive], path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            v, t = winreg.QueryValueEx(k, name)
            return (v, t)
    except OSError:
        return None


def reg_set(hive, path, name, value, typ):
    if not IS_WIN:
        FAKE_REG[(hive, path.lower(), name.lower())] = (value, typ)
        return
    with winreg.CreateKeyEx(HIVES[hive], path, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
        winreg.SetValueEx(k, name, 0, typ, value)


def reg_del(hive, path, name):
    if not IS_WIN:
        FAKE_REG.pop((hive, path.lower(), name.lower()), None)
        return
    try:
        with winreg.OpenKey(HIVES[hive], path, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
            winreg.DeleteValue(k, name)
    except OSError:
        pass


def load_backup():
    try:
        with open(BACKUP_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    d.setdefault("tweaks", {})
    d.setdefault("power", None)
    return d


def save_backup(d):
    with open(BACKUP_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def V(hive, path, name, on, off, typ=REG_DWORD):
    return (hive, path, name, on, off, typ)


CV = r"Software\Microsoft\Windows\CurrentVersion"
TWEAKS = [
    dict(id="game_mode", grupo="Jogos", titulo="Modo de Jogo do Windows", admin=False,
         desc="Prioriza o jogo ativo e impede o Windows Update de instalar drivers ou avisar de reinício durante a partida.",
         vals=[V("HKCU", r"Software\Microsoft\GameBar", "AutoGameModeEnabled", 1, 0),
               V("HKCU", r"Software\Microsoft\GameBar", "AllowAutoGameMode", 1, 0)]),
    dict(id="game_dvr", grupo="Jogos", titulo="Desligar gravação em segundo plano (Game DVR)", admin=False,
         desc="Impede o Windows de gravar clipes dos jogos por conta própria.",
         vals=[V("HKCU", r"System\GameConfigStore", "GameDVR_Enabled", 0, 1),
               V("HKCU", CV + r"\GameDVR", "AppCaptureEnabled", 0, 1)]),
    dict(id="sticky", grupo="Jogos", titulo="Desativar aviso de Teclas de Aderência", admin=False, relogin=True,
         desc="Evita o pop-up ao apertar Shift várias vezes no meio do jogo.",
         vals=[V("HKCU", r"Control Panel\Accessibility\StickyKeys", "Flags", "506", "510", REG_SZ),
               V("HKCU", r"Control Panel\Accessibility\ToggleKeys", "Flags", "58", "62", REG_SZ),
               V("HKCU", r"Control Panel\Accessibility\Keyboard Response", "Flags", "122", "126", REG_SZ)]),
    dict(id="mouse_accel", grupo="Entrada", titulo="Desligar aceleração do mouse", admin=False,
         desc="Movimento 1:1: a mesma distância da mão vira sempre a mesma distância na tela (mira mais previsível).",
         vals=[V("HKCU", r"Control Panel\Mouse", "MouseSpeed", "0", "1", REG_SZ),
               V("HKCU", r"Control Panel\Mouse", "MouseThreshold1", "0", "6", REG_SZ),
               V("HKCU", r"Control Panel\Mouse", "MouseThreshold2", "0", "10", REG_SZ)]),
    dict(id="bg_apps", grupo="Desempenho", titulo="Bloquear apps da Microsoft Store em segundo plano", admin=False,
         desc="Apps da Store deixam de rodar escondidos quando você não está usando.",
         vals=[V("HKCU", CV + r"\BackgroundAccessApplications", "GlobalUserDisabled", 1, 0)]),
    dict(id="startup_delay", grupo="Desempenho", titulo="Abrir programas de inicialização sem atraso", admin=False,
         relogin=True, desc="Remove a espera artificial que o Windows aplica aos programas ao entrar.",
         vals=[V("HKCU", CV + r"\Explorer\Serialize", "StartupDelayInMSec", 0, None)]),
    dict(id="transparency", grupo="Visual", titulo="Desligar efeitos de transparência", admin=False, relogin=True,
         desc="Menos trabalho para a placa de vídeo na área de trabalho.",
         vals=[V("HKCU", CV + r"\Themes\Personalize", "EnableTransparency", 0, 1)]),
    dict(id="animations", grupo="Visual", titulo="Desligar animações de janelas", admin=False, relogin=True,
         desc="Janelas abrem e fecham sem animação.",
         vals=[V("HKCU", r"Control Panel\Desktop\WindowMetrics", "MinAnimate", "0", "1", REG_SZ),
               V("HKCU", CV + r"\Explorer\Advanced", "TaskbarAnimations", 0, 1)]),
    dict(id="ads", grupo="Privacidade", titulo="Desligar ID de publicidade", admin=False,
         desc="Apps deixam de usar um identificador seu para anúncios.",
         vals=[V("HKCU", CV + r"\AdvertisingInfo", "Enabled", 0, 1)]),
    dict(id="suggestions", grupo="Privacidade", titulo="Remover sugestões e anúncios do Windows", admin=False,
         desc="Tira dicas, apps sugeridos e propaganda do menu Iniciar e das configurações.",
         vals=[V("HKCU", CV + r"\ContentDeliveryManager", n, 0, 1) for n in (
             "SystemPaneSuggestionsEnabled", "SubscribedContent-338388Enabled", "SubscribedContent-338389Enabled",
             "SubscribedContent-353694Enabled", "SubscribedContent-353696Enabled", "SubscribedContent-310093Enabled")]),
    dict(id="telemetry", grupo="Privacidade", titulo="Reduzir telemetria do Windows", admin=True,
         desc="Pede o mínimo de dados de diagnóstico (no Windows Home o mínimo possível é o nível básico).",
         vals=[V("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\DataCollection", "AllowTelemetry", 0, None)]),
]
TWEAK_BY_ID = {t["id"]: t for t in TWEAKS}
if not IS_WIN:      # estado inicial realista na previa
    reg_set("HKCU", r"Software\Microsoft\GameBar", "AutoGameModeEnabled", 1, REG_DWORD)
    reg_set("HKCU", r"Software\Microsoft\GameBar", "AllowAutoGameMode", 1, REG_DWORD)
    reg_set("HKCU", CV + r"\AdvertisingInfo", "Enabled", 0, REG_DWORD)


def tweak_state(t):
    for (h, p, n, onv, offv, typ) in t["vals"]:
        cur = reg_get(h, p, n)
        if cur is None or str(cur[0]) != str(onv):
            return False
    return True


def apply_mouse():
    if not IS_WIN:
        return
    vals = []
    for n in ("MouseThreshold1", "MouseThreshold2", "MouseSpeed"):
        cur = reg_get("HKCU", r"Control Panel\Mouse", n)
        vals.append(int(cur[0]) if cur else 0)
    arr = (ctypes.c_int * 3)(*vals)
    user32.SystemParametersInfoW(0x0004, 0, arr, 3)      # SPI_SETMOUSE, grava e avisa


def tweak_apply(t, on):
    if t["admin"] and not is_admin():
        raise PermissionError("admin")
    bk = load_backup()
    if t["id"] not in bk["tweaks"]:       # guarda o valor original uma unica vez
        bk["tweaks"][t["id"]] = {f"{h}|{p}|{n}": reg_get(h, p, n) for (h, p, n, *_r) in t["vals"]}
        save_backup(bk)
    for (h, p, n, onv, offv, typ) in t["vals"]:
        if on:
            reg_set(h, p, n, onv, typ)
        elif offv is None:
            reg_del(h, p, n)
        else:
            reg_set(h, p, n, offv, typ)
    if t["id"] == "mouse_accel":
        apply_mouse()


# ---------------------------------------------------- plano de energia
GUID_RE = re.compile(r"([0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})")
P_BAL, P_HIGH, P_ULT = ("381b4222-f694-41f0-9685-ff5bb260df2e", "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
                        "e9a42b02-d5df-448d-aa00-03f14749eb61")
_fake_plan = ["balanced"]


def power_list():
    rc, out, _ = run(["powercfg", "/list"])
    items = []
    for line in out.splitlines():
        m = GUID_RE.search(line)
        if m:
            nm = re.search(r"\((.+?)\)", line)
            items.append((m.group(1).lower(), nm.group(1) if nm else ""))
    return items


def power_active():
    rc, out, _ = run(["powercfg", "/getactivescheme"])
    m = GUID_RE.search(out)
    nm = re.search(r"\((.+?)\)", out)
    return (m.group(1).lower() if m else None), (nm.group(1) if nm else "")


def power_plan_id(guid, name):
    n = name.lower()
    if guid == P_BAL:
        return "balanced"
    if guid == P_HIGH or "high performance" in n or "alto desempenho" in n:
        return "high"
    if guid == P_ULT or "ultimate" in n or "máximo" in n or "maximo" in n:
        return "ultimate"
    return "other"


def power_get():
    if not IS_WIN:
        return dict(plan=_fake_plan[0], name="Equilibrado")
    g, n = power_active()
    return dict(plan=power_plan_id(g, n), name=n or "Desconhecido")


def power_set(plan):
    if plan not in ("balanced", "high", "ultimate"):
        raise ValueError("Plano inválido.")
    if not IS_WIN:
        _fake_plan[0] = plan
        return
    cur, _ = power_active()
    bk = load_backup()
    if bk["power"] is None and cur:
        bk["power"] = cur
        save_backup(bk)

    def activate(g):
        return run(["powercfg", "/setactive", g])[0] == 0

    items = power_list()
    ok = False
    if plan == "balanced":
        ok = activate(P_BAL)
    else:
        target = next((g for g, n in items if power_plan_id(g, n) == plan), None)
        ok = bool(target) and activate(target)
        if not ok:
            m = GUID_RE.search(run(["powercfg", "/duplicatescheme", P_HIGH if plan == "high" else P_ULT])[1])
            ok = bool(m) and activate(m.group(1))
    if not ok:
        raise RuntimeError("O Windows recusou a troca do plano de energia (este PC pode não oferecer esse plano).")


# ---------------------------------------------------- limpeza de arquivos
class _RBI(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("cbSize", ctypes.c_ulong), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]


def junk_defs():
    la = os.environ.get("LOCALAPPDATA", "")
    tmp = os.environ.get("TEMP", "")
    win = os.environ.get("SystemRoot", r"C:\Windows")

    def j(*p):
        return os.path.join(la, *p) if la else ""

    return [
        dict(id="temp", nome="Arquivos temporários do usuário", desc="Pasta TEMP do seu usuário",
             roots=[tmp], need="temp", default=True),
        dict(id="wintemp", nome="Temporários do Windows", desc="Windows\\Temp (alguns itens exigem administrador)",
             roots=[os.path.join(win, "Temp")], need="temp", default=True),
        dict(id="crash", nome="Relatórios de erro e crash dumps", desc="Despejos de memória e relatórios do Windows",
             roots=[j("CrashDumps"), j("Microsoft", "Windows", "WER")], default=True),
        dict(id="thumbs", nome="Cache de miniaturas", desc="O Explorer recria sozinho quando precisar",
             globs=[(j("Microsoft", "Windows", "Explorer"), ["thumbcache_*.db", "iconcache_*.db"])], default=True),
        dict(id="shader", nome="Cache de shaders (NVIDIA / AMD / DirectX)", desc="Arquivos de shader dos jogos",
             roots=[j("NVIDIA", "DXCache"), j("NVIDIA", "GLCache"), j("D3DSCache"), j("AMD", "DxCache")], default=False,
             warn="Os jogos podem travar um pouco na primeira abertura, enquanto o cache é recriado."),
        dict(id="fivem", nome="Cache do FiveM", desc="Pastas server-cache e server-cache-priv",
             roots=[j("FiveM", "FiveM.app", "data", "server-cache"), j("FiveM", "FiveM.app", "data", "server-cache-priv")],
             default=False, warn="Na próxima conexão, os arquivos dos servidores são baixados de novo."),
        dict(id="recycle", nome="Lixeira", desc="Esvazia a Lixeira de todas as unidades", special="recycle",
             default=False, warn="Não dá para desfazer."),
    ]


def _root_ok(d, r):
    if not r or not os.path.isdir(r):
        return False
    rr = r.rstrip("\\/")
    if not rr or os.path.dirname(rr) == rr:
        return False
    if d.get("need") == "temp" and os.path.basename(rr).lower() not in ("temp", "tmp"):
        return False
    return True


def _is_link(e):
    try:
        return e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction())
    except OSError:
        return True


def _walk_size(path, st):
    try:
        entries = list(os.scandir(path))
    except OSError:
        return
    for e in entries:
        if _is_link(e):
            continue
        try:
            if e.is_dir(follow_symlinks=False):
                _walk_size(e.path, st)
            else:
                st[0] += e.stat(follow_symlinks=False).st_size
                st[1] += 1
        except OSError:
            pass


def _clear_dir(path, st):
    """Apaga o CONTEUDO da pasta (nunca a pasta em si) e nunca segue atalhos/junctions."""
    try:
        entries = list(os.scandir(path))
    except OSError:
        return
    for e in entries:
        if _is_link(e):
            continue
        try:
            if e.is_dir(follow_symlinks=False):
                _clear_dir(e.path, st)
                try:
                    os.rmdir(e.path)
                except OSError:
                    pass
            else:
                sz = e.stat(follow_symlinks=False).st_size
                try:
                    os.chmod(e.path, 0o666)
                except OSError:
                    pass
                os.remove(e.path)
                st[0] += sz
                st[1] += 1
        except OSError:
            pass


def _glob_files(folder, pats):
    if not folder or not os.path.isdir(folder):
        return []
    out = []
    for f in os.listdir(folder):
        if any(fnmatch.fnmatch(f.lower(), p) for p in pats):
            p = os.path.join(folder, f)
            if os.path.isfile(p) and not os.path.islink(p):
                out.append(p)
    return out


def _recycle_info():
    if not IS_WIN:
        return 38 * 2 ** 20, 12
    try:
        i = _RBI()
        i.cbSize = ctypes.sizeof(i)
        if shell32.SHQueryRecycleBinW(None, ctypes.byref(i)) == 0:
            return int(i.i64Size), int(i.i64NumItems)
    except Exception:
        pass
    return 0, 0


FAKE_JUNK = {"temp": (1.4 * 2 ** 30, 2210), "wintemp": (212 * 2 ** 20, 340), "crash": (96 * 2 ** 20, 14),
             "thumbs": (58 * 2 ** 20, 6), "shader": (734 * 2 ** 20, 95), "fivem": (1.9 * 2 ** 30, 4102)}


def junk_scan():
    out = []
    for d in junk_defs():
        size = files = 0
        if d.get("special") == "recycle":
            size, files = _recycle_info()
        elif not IS_WIN:
            size, files = FAKE_JUNK.get(d["id"], (0, 0))
        else:
            for r in d.get("roots", []):
                if _root_ok(d, r):
                    st = [0, 0]
                    _walk_size(r, st)
                    size += st[0]
                    files += st[1]
            for folder, pats in d.get("globs", []):
                for p in _glob_files(folder, pats):
                    try:
                        size += os.lstat(p).st_size
                        files += 1
                    except OSError:
                        pass
        out.append(dict(id=d["id"], nome=d["nome"], desc=d["desc"], size=int(size), files=int(files),
                        default=d["default"], warn=d.get("warn", "")))
    return out


def junk_clean(ids):
    defs = {d["id"]: d for d in junk_defs()}
    freed = files = 0
    for i in ids:
        d = defs.get(i)
        if not d:
            continue
        if d.get("special") == "recycle":
            sz, n = _recycle_info()
            if IS_WIN:
                shell32.SHEmptyRecycleBinW(None, None, 7)    # sem confirmacao, sem som, sem barra
            freed += sz
            files += n
            continue
        if not IS_WIN:
            sz, n = FAKE_JUNK.get(i, (0, 0))
            freed += sz
            files += n
            continue
        for r in d.get("roots", []):
            if _root_ok(d, r):
                st = [0, 0]
                _clear_dir(r, st)
                freed += st[0]
                files += st[1]
        for folder, pats in d.get("globs", []):
            for p in _glob_files(folder, pats):
                try:
                    sz = os.lstat(p).st_size
                    os.remove(p)
                    freed += sz
                    files += 1
                except OSError:
                    pass
    return int(freed), int(files)


# ---------------------------------------------------- inicializacao do Windows
RUN_KEY = CV + r"\Run"
APPR_KEY = CV + r"\Explorer\StartupApproved\Run"
FAKE_STARTUP = [dict(id="HKCU|Discord", name="Discord", scope="Usuário", cmd="C:\\Users\\voce\\AppData\\Local\\Discord\\Update.exe --processStart Discord.exe", enabled=True),
                dict(id="HKCU|Spotify", name="Spotify", scope="Usuário", cmd="C:\\Users\\voce\\AppData\\Roaming\\Spotify\\Spotify.exe /background", enabled=True),
                dict(id="HKCU|OneDrive", name="OneDrive", scope="Usuário", cmd="C:\\Users\\voce\\AppData\\Local\\Microsoft\\OneDrive\\OneDrive.exe /background", enabled=False),
                dict(id="HKLM|SecurityHealth", name="SecurityHealth", scope="Sistema", cmd="C:\\Windows\\system32\\SecurityHealthSystray.exe", enabled=True)]


def startup_list():
    if not IS_WIN:
        return [dict(x) for x in FAKE_STARTUP]
    items = []
    for hive, label in (("HKCU", "Usuário"), ("HKLM", "Sistema")):
        try:
            with winreg.OpenKey(HIVES[hive], RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                i = 0
                while True:
                    try:
                        name, val, _t = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    i += 1
                    if name == "ColorStudio":
                        continue
                    ap = reg_get(hive, APPR_KEY, name)
                    dis = bool(ap and isinstance(ap[0], (bytes, bytearray)) and len(ap[0]) > 0 and ap[0][0] & 1)
                    items.append(dict(id=f"{hive}|{name}", name=name, scope=label, cmd=str(val)[:110], enabled=not dis))
        except OSError:
            pass
    return items


def startup_toggle(sid, on):
    hive, _, name = str(sid).partition("|")
    if hive not in ("HKCU", "HKLM") or not any(i["id"] == sid for i in startup_list()):
        raise ValueError("Programa não encontrado.")
    if not IS_WIN:
        for x in FAKE_STARTUP:
            if x["id"] == sid:
                x["enabled"] = on
        return
    try:
        reg_set(hive, APPR_KEY, name, bytes([2 if on else 3]) + bytes(11), REG_BINARY)
    except OSError:
        raise PermissionError("Esse item é do sistema e exige o Color Studio como administrador.")


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
        elif k in ("reaplicar", "segundo_plano", "auto_update", "auto_prioridade"):
            CFG[k] = clamp_bool(v)
        elif k == "jogos" and isinstance(v, list):
            lst = []
            for g in v[:30]:
                g = str(g).strip()[:60]
                if re.fullmatch(r"[\w .()\[\]\-]+", g) and g.lower() not in [x.lower() for x in lst]:
                    lst.append(g)
            CFG[k] = lst
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


# ---------------------------------------------------- API de otimizacao
def api_sys(b):
    tot, used = mem_info()
    dt, du, df = disk_info()
    return dict(ram=dict(total=tot, used=used, pct=round(used * 100 / tot)), cpu=cpu_pct(),
                disk=dict(total=dt, free=df, pct=round(du * 100 / dt)), uptime=uptime_s())


def api_mem(b):
    return mem_release()


def api_power(b):
    return power_get()


def api_power_set(b):
    try:
        power_set(b.get("plan"))
    except (ValueError, RuntimeError) as ex:
        return {"error": str(ex)}
    return power_get()


def api_tweaks(b):
    return dict(admin=is_admin(), tweaks=[
        dict(id=t["id"], grupo=t["grupo"], titulo=t["titulo"], desc=t["desc"], admin=t["admin"],
             relogin=t.get("relogin", False), on=tweak_state(t)) for t in TWEAKS])


def api_tweaks_set(b):
    t = TWEAK_BY_ID.get(b.get("id"))
    if not t:
        return {"error": "Ajuste desconhecido."}
    try:
        tweak_apply(t, bool(b.get("on")))
    except PermissionError:
        return {"error": "Este ajuste exige o Color Studio como administrador (Início → Reabrir como administrador)."}
    except OSError as ex:
        return {"error": f"O Windows recusou a alteração: {ex}"}
    return dict(ok=True, on=tweak_state(t))


def api_tweaks_restore(b):
    bk = load_backup()
    n = 0
    for tid, vals in bk["tweaks"].items():
        t = TWEAK_BY_ID.get(tid)
        if not t or (t["admin"] and not is_admin()):
            continue
        for key, orig in vals.items():
            h_, p_, nm = key.split("|", 2)
            try:
                if orig is None:
                    reg_del(h_, p_, nm)
                else:
                    reg_set(h_, p_, nm, orig[0], orig[1])
            except OSError:
                pass
        n += 1
    apply_mouse()
    if bk.get("power"):
        try:
            if IS_WIN:
                run(["powercfg", "/setactive", bk["power"]])
            else:
                _fake_plan[0] = "balanced"
        except Exception:
            pass
    save_backup({"tweaks": {}, "power": None})
    return dict(ok=True, restored=n)


def api_junk_scan(b):
    return dict(items=junk_scan())


def api_junk_clean(b):
    ids = b.get("ids")
    if not isinstance(ids, list):
        ids = [d["id"] for d in junk_defs() if d["default"]]
    freed, files = junk_clean([str(i) for i in ids])
    return dict(ok=True, freed=freed, files=files)


def api_startup(b):
    return dict(items=startup_list())


def api_startup_toggle(b):
    on = bool(b.get("on"))
    try:
        startup_toggle(b.get("id"), on)
    except (ValueError, PermissionError) as ex:
        return {"error": str(ex)}
    return dict(ok=True, on=on)


def api_procs(b):
    hb = host_browser()
    g = {}
    for nm, pid, mem in proc_rows():
        d = g.setdefault(nm.lower(), dict(name=nm, count=0, mem=0))
        d["count"] += 1
        d["mem"] += mem
    out = sorted(g.values(), key=lambda d: -d["mem"])[:40]
    for d in out:
        d["protected"] = d["name"].lower() in PROTECTED or d["name"].lower() == hb
    return dict(procs=out)


def api_proc_kill(b):
    try:
        n = kill_by_name(b.get("name"))
    except (ValueError, PermissionError) as ex:
        return {"error": str(ex)}
    return dict(ok=True, closed=n)


def api_proc_prio(b):
    try:
        n = prio_by_name(b.get("name"), b.get("level"))
    except (ValueError, PermissionError) as ex:
        return {"error": str(ex)}
    return dict(ok=True, changed=n)


def api_bg_scan(b):
    return dict(apps=[dict(name=a["name"], mem=a["mem"]) for a in bg_scan()])


def api_bg_close(b):
    closed = 0
    for a in bg_scan():
        for pid in a["pids"]:
            if IS_WIN:
                closed += run(["taskkill", "/F", "/PID", str(pid)], 10)[0] == 0
            else:
                closed += 1
    return dict(ok=True, closed=closed)


def api_dns(b):
    if IS_WIN and run(["ipconfig", "/flushdns"], 15)[0] != 0:
        return {"error": "Não foi possível limpar o cache de DNS."}
    return {"ok": True}


def api_gamemode(b):
    steps = []

    def step(name, fn):
        try:
            steps.append(dict(name=name, ok=True, detail=fn()))
        except Exception as ex:
            steps.append(dict(name=name, ok=False, detail=str(ex) or "falhou"))

    def s_mem():
        return fmt_bytes(mem_release()["freed"]) + " liberados"

    def s_power():
        power_set("high")
        return "plano Alto desempenho ativo"

    def s_tw():
        for tid in ("game_mode", "game_dvr"):
            tweak_apply(TWEAK_BY_ID[tid], True)
        return "Modo de Jogo ligado e gravação em segundo plano desligada"

    def s_prio():
        pats = [g.lower() for g in CFG.get("jogos", []) if g]
        n = 0
        for nm, pid, _ in proc_rows():
            if nm.lower() not in PROTECTED and any(p in nm.lower() for p in pats):
                n += set_prio_pid(pid, PRIO["alta"]) if IS_WIN else 1
        return f"{n} processo(s) de jogo em prioridade alta" if n else "nenhum jogo monitorado aberto agora"

    step("Liberar memória", s_mem)
    step("Plano de energia", s_power)
    step("Ajustes do Windows", s_tw)
    step("Prioridade dos jogos", s_prio)
    return dict(steps=steps)


def api_admin(b):
    if is_admin():
        return {"error": "O Color Studio já está como administrador."}
    if not IS_WIN:
        return {"error": "Só funciona no Windows."}
    r = shell32.ShellExecuteW(None, "runas", pythonw(), f'"{os.path.abspath(__file__)}" --wait', HERE, 1)
    if int(r or 0) <= 32:
        return {"error": "O Windows não recebeu a permissão de administrador."}
    threading.Timer(0.8, shutdown_app).start()
    return {"ok": True}


ROUTES = {
    "/api/sys": api_sys, "/api/mem": api_mem, "/api/power": api_power, "/api/power/set": api_power_set,
    "/api/tweaks": api_tweaks, "/api/tweaks/set": api_tweaks_set, "/api/tweaks/restore": api_tweaks_restore,
    "/api/junk/scan": api_junk_scan, "/api/junk/clean": api_junk_clean, "/api/startup": api_startup,
    "/api/startup/toggle": api_startup_toggle, "/api/procs": api_procs, "/api/procs/kill": api_proc_kill,
    "/api/procs/priority": api_proc_prio, "/api/bg/scan": api_bg_scan, "/api/bg/close": api_bg_close,
    "/api/dns": api_dns, "/api/gamemode": api_gamemode, "/api/admin": api_admin,
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
            subprocess.Popen([exe, f"--app={url}", "--window-size=1240,860", "--no-first-run",
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
    waitarg = "--wait" in sys.argv          # reaberto como administrador: espera a copia antiga fechar
    if restart or waitarg:
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
    if IS_WIN:
        threading.Thread(target=watcher_loop, daemon=True).start()
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
.tab{border:0;background:transparent;padding:9px 15px;border-radius:999px;font-weight:800;font-size:11.5px;letter-spacing:.09em;color:var(--muted);transition:.2s}
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
/* ---- Início / Otimizar / Processos ---- */
.btn:disabled{opacity:.55;cursor:wait;transform:none}
.btn.sm{padding:7px 14px;font-size:11.5px}
.tag.sm{font-size:9px;padding:3px 8px;margin-left:6px;vertical-align:2px}
.hero{display:flex;align-items:center;justify-content:space-between;gap:26px;margin-top:18px;padding:26px 28px;border-radius:28px;
  background:linear-gradient(135deg,color-mix(in srgb,var(--acc) 34%,var(--box)),color-mix(in srgb,var(--acc2) 26%,var(--box)));
  border:1px solid color-mix(in srgb,var(--acc) 50%,transparent);box-shadow:0 14px 40px var(--glow)}
.hero h2{font-size:25px;font-weight:900;margin-bottom:6px}
.hero p{color:color-mix(in srgb,var(--fg) 80%,transparent);font-size:13px;line-height:1.55;max-width:560px}
.btn.big{padding:17px 32px;font-size:15px;flex:none}
.steps{margin-top:12px;display:flex;flex-direction:column;gap:6px;font-size:12.5px}
.steps div{display:flex;gap:8px}.steps .ok{color:#4ade80}.steps .no{color:#f87171}
.gauges{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:16px}
.gauge{padding:20px 12px 16px;border-radius:26px;text-align:center;background:color-mix(in srgb,var(--box) 88%,transparent);
  border:1px solid color-mix(in srgb,var(--border) 75%,transparent);display:flex;flex-direction:column;align-items:center;justify-content:flex-start}
.ring{--p:0;width:116px;height:116px;margin:0 auto 12px;border-radius:50%;display:grid;place-items:center;position:relative;
  background:conic-gradient(var(--acc2),var(--acc) calc(var(--p)*1%),var(--track) 0)}
.ring::before{content:"";position:absolute;inset:12px;border-radius:50%;background:color-mix(in srgb,var(--box) 100%,transparent)}
.ring span{position:relative;font-weight:900;font-size:22px}
.gauge b{display:block;font-size:13.5px}.gauge small{display:block;color:var(--muted);font-size:11.5px;margin-top:3px}
.gauge .bigtxt{height:116px;margin-bottom:12px;display:grid;place-items:center;font-size:30px;font-weight:900;color:var(--acc)}
.acts{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:16px}
.act{padding:20px;border-radius:26px;display:flex;flex-direction:column;gap:8px;align-items:flex-start;
  background:color-mix(in srgb,var(--box) 88%,transparent);border:1px solid color-mix(in srgb,var(--border) 75%,transparent)}
.act h4{font-size:15px;margin:0;letter-spacing:0;color:var(--fg);font-weight:800}.act p{color:var(--muted);font-size:12.5px;line-height:1.5;flex:1}.act small{color:#4ade80;font-size:12px;min-height:16px}
.grp{margin:16px 0 2px;font-size:11px;letter-spacing:.14em;color:var(--acc);font-weight:900}
.grp:first-child{margin-top:4px}
.orow{display:flex;align-items:center;gap:14px;padding:12px 0;border-top:1px solid color-mix(in srgb,var(--border) 45%,transparent)}
.grp + .orow,.bd > .orow:first-child{border-top:0}
.orow .t{flex:1;min-width:0}.orow b{font-size:13.5px;display:block}
.orow small{color:var(--muted);font-size:11.5px;line-height:1.45;display:block;margin-top:2px}
.orow small.warn{color:#fbbf24}.orow .size{font-weight:800;font-size:13px;color:var(--acc);white-space:nowrap}
.btnrow{display:flex;gap:10px;flex-wrap:wrap;padding-top:14px}
.seg{display:flex;gap:12px;flex-wrap:wrap}
.opt{flex:1;min-width:180px;padding:15px 18px;border-radius:20px;cursor:pointer;transition:.18s;
  background:var(--card);border:1px solid color-mix(in srgb,var(--border) 70%,transparent)}
.opt:hover{transform:translateY(-1px)}.opt b{display:block;font-size:14px}.opt small{display:block;color:var(--muted);font-size:11.5px;margin-top:3px}
.opt.on{border-color:var(--acc);background:color-mix(in srgb,var(--card) 80%,var(--acc) 20%);box-shadow:0 0 0 1px var(--acc),0 10px 30px var(--glow)}
.chk{width:24px;height:24px;border-radius:9px;border:2px solid var(--border);flex:none;display:grid;place-items:center;cursor:pointer;transition:.15s}
.chk.on{background:linear-gradient(135deg,var(--acc),var(--acc2));border-color:transparent;box-shadow:0 0 12px var(--glow)}
.chk.on::after{content:"✓";color:#fff;font-size:14px;font-weight:900}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:2px 0 12px}
.chip{display:inline-flex;align-items:center;gap:8px;padding:7px 8px 7px 14px;border-radius:99px;font-size:12.5px;font-weight:700;
  background:color-mix(in srgb,var(--acc) 14%,transparent);border:1px solid color-mix(in srgb,var(--acc) 40%,transparent)}
.chip button{border:0;width:20px;height:20px;border-radius:50%;background:color-mix(in srgb,var(--fg) 16%,transparent);line-height:1}
.chip button:hover{background:#fb3b4d;color:#fff}
.ptr{display:grid;grid-template-columns:minmax(150px,1.2fr) 56px minmax(150px,1fr) 240px;align-items:center;gap:14px;padding:9px 4px;
  border-top:1px solid color-mix(in srgb,var(--border) 45%,transparent);font-size:13px}
.ptr.head{border-top:0;color:var(--muted);font-size:11px;letter-spacing:.1em;font-weight:800;text-transform:uppercase}
.ptr .pn{font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ptr .pm{position:relative;height:24px;border-radius:99px;background:var(--track);overflow:hidden}
.ptr .pm i{position:absolute;left:0;top:0;bottom:0;border-radius:99px;background:linear-gradient(90deg,var(--acc2),var(--acc));opacity:.85}
.ptr .pm em{position:absolute;inset:0;display:grid;place-items:center;font-style:normal;font-weight:800;font-size:11.5px;text-shadow:0 1px 3px #0008;color:#fff}
.ptr .pa{display:flex;gap:8px;justify-content:flex-end}
@media (max-width:1300px){.hdtag{display:none}}
@media (max-width:900px){.row{grid-template-columns:1fr;gap:4px}.val{width:62px}.gauges{grid-template-columns:repeat(2,1fr)}.acts{grid-template-columns:1fr}.hero{flex-direction:column;align-items:flex-start}}
</style></head><body>
<div id="bg"></div>
<div id="app">
  <header class="bar glass">
    <div class="logo">CS</div>
    <div class="brand"><b>Color Studio</b><span id="ver"></span></div>
    <span class="tag hdtag">EDIÇÃO LIVRE</span>
    <nav class="tabs">
      <button class="tab on" data-tab="inicio">INÍCIO</button>
      <button class="tab" data-tab="otimizar">OTIMIZAR</button>
      <button class="tab" data-tab="processos">PROCESSOS</button>
      <button class="tab" data-tab="ajustes">CORES</button>
      <button class="tab" data-tab="personalizacao">PERSONALIZAÇÃO</button>
    </nav>
    <div class="sp"></div>
    <div id="dots"></div>
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

let S=null, tab="inicio", alive=true, timer=null;
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
    <div class="subttl">${esc(S.sub)} · global</div></div><div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;justify-content:flex-end"><div class="pill" id="mon">MONITOR  1 · ${Math.round(screen.width*devicePixelRatio)}×${Math.round(screen.height*devicePixelRatio)}</div><button class="btn" id="cmp">Segure para comparar</button></div></div>`));
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
/* ================= Início / Otimizar / Processos ================= */
const fmtB=b=>b>=2**30?(b/2**30).toFixed(1)+" GB":b>=2**20?Math.round(b/2**20)+" MB":b>=1024?Math.round(b/1024)+" KB":b+" B";
const fmtUp=s=>{const d=Math.floor(s/86400),hh=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return d?`${d}d ${hh}h`:`${hh}h ${m}min`};
const act=async(path,body)=>{const r=await api(path,body);if(r&&r.error){toast(r.error);return null}return r};
const boxEl=(title,desc)=>h(`<section class="box"><h3>${title}</h3>${desc?`<p class="desc">${desc}</p>`:""}<div class="bd"></div></section>`);
const titleRow=(t,s,extra="")=>h(`<div class="titlerow"><div><div class="ttl">${t}</div><div class="subttl">${s}</div></div>${extra}</div>`);

function renderInicio(){
  const t=$("#tab");t.innerHTML="";
  t.appendChild(titleRow("Bem-vindo de volta","Otimize o sistema, libere memória e reduza processos em segundo plano",`<div id="adm"></div>`));
  const adm=$("#adm");
  if(S.admin)adm.appendChild(h(`<span class="tag">ADMINISTRADOR</span>`));
  else{const b=h(`<button class="btn">Reabrir como administrador</button>`);
    b.onclick=async()=>{if(!await modal({title:"Reabrir como administrador",label:"O Color Studio fecha e abre de novo com permissão extra (o Windows vai pedir confirmação). Libera a limpeza completa de Windows\\Temp e o ajuste de telemetria.",ok:"Continuar"}))return;
      const r=await act("/api/admin");if(r)overlay("Reabrindo como administrador…")};adm.appendChild(b)}

  t.appendChild(h(`<section class="hero"><div><h2>Modo Jogo</h2><p>Libera memória, ativa o plano Alto desempenho, liga o Modo de Jogo do Windows, desliga a gravação em segundo plano e coloca seus jogos monitorados em prioridade alta.</p><div class="steps" id="gsteps"></div></div><button class="btn primary big" id="gm">Ativar Modo Jogo</button></section>`));
  $("#gm").onclick=async e=>{const b=e.currentTarget;b.disabled=true;b.textContent="Aplicando…";const r=await act("/api/gamemode");b.disabled=false;b.textContent="Aplicar de novo";
    if(r)$("#gsteps").innerHTML=r.steps.map(s=>`<div class="${s.ok?"ok":"no"}"><span>${s.ok?"✓":"✕"}</span><span>${esc(s.name)} — ${esc(s.detail)}</span></div>`).join("")};

  t.appendChild(h(`<div class="gauges">
    ${[["ram","Memória RAM"],["cpu","Processador"],["disk","Disco do sistema"]].map(([id,n])=>`<div class="gauge"><div class="ring" id="r-${id}"><span>–</span></div><b>${n}</b><small id="s-${id}">&nbsp;</small></div>`).join("")}
    <div class="gauge"><div class="bigtxt" id="up">–</div><b>Tempo ligado</b><small>desde a última inicialização</small></div></div>`));
  const setR=(id,p,txt,sub)=>{const r=$("#r-"+id);if(!r)return;r.style.setProperty("--p",p);$("span",r).textContent=txt;$("#s-"+id).textContent=sub};
  const refresh=async()=>{try{const s=await api("/api/sys");
    setR("ram",s.ram.pct,s.ram.pct+"%",fmtB(s.ram.used)+" de "+fmtB(s.ram.total));setR("cpu",s.cpu,s.cpu+"%","uso agora");
    setR("disk",s.disk.pct,s.disk.pct+"%",fmtB(s.disk.free)+" livres");const u=$("#up");if(u)u.textContent=fmtUp(s.uptime)}catch(e){}};
  refresh();timer=setInterval(refresh,2000);

  t.appendChild(h(`<div class="acts">
    <div class="act"><h4>Liberar memória</h4><p>Reduz a memória usada pelos apps abertos para o Windows reaproveitar. Não fecha nada.</p><button class="btn" id="a-mem">Liberar agora</button><small id="a-mem-r"></small></div>
    <div class="act"><h4>Limpar arquivos</h4><p>Temporários, relatórios de erro e miniaturas — só as opções seguras. Há mais opções em Otimizar.</p><button class="btn" id="a-junk">Limpar agora</button><small id="a-junk-r"></small></div>
    <div class="act"><h4>Apps em segundo plano</h4><p>OneDrive, Teams, Widgets e outros que ficam abertos sem você usar. Você confirma antes de fechar.</p><button class="btn" id="a-bg">Ver e fechar</button><small id="a-bg-r"></small></div></div>`));
  const say=(id,m)=>{const e=$(id);if(e)e.textContent=m};
  $("#a-mem").onclick=async e=>{const b=e.currentTarget;b.disabled=true;const r=await act("/api/mem");b.disabled=false;if(r)say("#a-mem-r",`${fmtB(r.freed)} liberados em ${r.procs} processos`)};
  $("#a-junk").onclick=async e=>{const b=e.currentTarget;b.disabled=true;const r=await act("/api/junk/clean",{});b.disabled=false;if(r)say("#a-junk-r",`${fmtB(r.freed)} removidos (${r.files} arquivos)`)};
  $("#a-bg").onclick=async()=>{const r=await act("/api/bg/scan");if(!r)return;
    if(!r.apps.length)return say("#a-bg-r","Nenhum desses apps está aberto.");
    if(!await modal({title:"Fechar apps em segundo plano",label:"Serão fechados: "+r.apps.map(a=>`${a.name} (${fmtB(a.mem)})`).join(", ")+". Salve o que estiver aberto neles.",ok:"Fechar"}))return;
    const c=await act("/api/bg/close");if(c)say("#a-bg-r",`${c.closed} processo(s) fechado(s)`)};
}

function renderOtimizar(){
  const t=$("#tab");t.innerHTML="";
  t.appendChild(titleRow("Otimizações","Ajustes reversíveis do Windows — nada é instalado e dá para restaurar tudo"));
  [["PLANO DE ENERGIA","Controla o quanto o processador pode trabalhar.",secPower],
   ["AJUSTES DO WINDOWS","Cada interruptor muda só uma configuração do seu usuário (o original fica guardado para restaurar).",secTweaks],
   ["LIMPEZA DE ARQUIVOS","Marque o que quer apagar. Arquivos em uso são ignorados.",secJunk],
   ["PROGRAMAS NA INICIALIZAÇÃO","Programas que abrem sozinhos quando o Windows liga.",secStartup],
   ["JOGOS MONITORADOS","",secGames],["REDE","",secNet]
  ].forEach(([ti,de,fn])=>{const b=boxEl(ti,de);t.appendChild(b);fn($(".bd",b)).catch(()=>{$(".bd",b).innerHTML=`<div class="res err">Não foi possível carregar esta seção.</div>`})});
}
async function secPower(bd){
  let r=await api("/api/power");
  const opts=[["balanced","Equilibrado","Economiza energia quando o PC está ocioso"],["high","Alto desempenho","Processador pronto o tempo todo"],["ultimate","Desempenho máximo","Sem nenhuma economia de energia"]];
  const draw=()=>{bd.innerHTML="";const seg=h(`<div class="seg"></div>`);
    opts.forEach(([id,n,d])=>{const o=h(`<div class="opt ${r.plan===id?"on":""}"><b>${n}</b><small>${d}</small></div>`);
      o.onclick=async()=>{const x=await act("/api/power/set",{plan:id});if(x){r=x;draw();toast("Plano de energia: "+n)}};seg.appendChild(o)});
    bd.appendChild(seg);
    bd.appendChild(h(`<div class="res">${r.plan==="other"?"Plano atual: "+esc(r.name)+". ":""}Em notebooks, planos mais fortes gastam mais bateria e esquentam mais.</div>`))};
  draw();
}
async function secTweaks(bd){
  const r=await api("/api/tweaks"),groups={};r.tweaks.forEach(x=>(groups[x.grupo]=groups[x.grupo]||[]).push(x));
  bd.innerHTML="";
  for(const [g,list] of Object.entries(groups)){
    bd.appendChild(h(`<h5 class="grp">${g.toUpperCase()}</h5>`));
    list.forEach(tw=>{
      const row=h(`<div class="orow"><div class="t"><b>${esc(tw.titulo)}${tw.admin?'<span class="tag sm">ADMIN</span>':""}</b><small>${esc(tw.desc)}${tw.relogin?" (vale depois de sair e entrar no Windows)":""}</small></div><div class="sw ${tw.on?"on":""}"></div></div>`);
      const sw=$(".sw",row);
      sw.onclick=async()=>{const on=!sw.classList.contains("on");const x=await act("/api/tweaks/set",{id:tw.id,on});if(x)sw.classList.toggle("on",x.on)};
      bd.appendChild(row)});
  }
  const f=h(`<div class="btnrow"><button class="btn">Restaurar padrões do Windows</button></div>`);
  $("button",f).onclick=async()=>{if(!await modal({title:"Restaurar padrões",label:"Desfaz os ajustes acima para o valor que estava antes de você mexer e volta o plano de energia ao original.",ok:"Restaurar"}))return;
    const x=await act("/api/tweaks/restore");if(x){toast("Padrões restaurados");renderOtimizar()}};
  bd.appendChild(f);
}
async function secJunk(bd){
  bd.innerHTML=`<div class="res">Analisando…</div>`;
  const r=await api("/api/junk/scan"),sel=new Set(r.items.filter(i=>i.default).map(i=>i.id));
  const draw=()=>{bd.innerHTML="";
    r.items.forEach(i=>{const row=h(`<div class="orow" style="cursor:pointer"><div class="chk ${sel.has(i.id)?"on":""}"></div><div class="t"><b>${esc(i.nome)}</b><small>${esc(i.desc)}</small>${i.warn?`<small class="warn">${esc(i.warn)}</small>`:""}</div><div class="size">${fmtB(i.size)}</div></div>`);
      row.onclick=()=>{sel.has(i.id)?sel.delete(i.id):sel.add(i.id);draw()};bd.appendChild(row)});
    const total=r.items.filter(i=>sel.has(i.id)).reduce((a,i)=>a+i.size,0);
    const f=h(`<div class="btnrow"><button class="btn primary" id="jc">Limpar selecionados (${fmtB(total)})</button><button class="btn" id="jr">Analisar de novo</button></div>`);
    $("#jc",f).onclick=async e=>{if(!sel.size)return toast("Marque pelo menos um item");
      if(sel.has("recycle")&&!await modal({title:"Esvaziar a Lixeira",label:"Os arquivos da Lixeira serão apagados de vez.",ok:"Esvaziar",danger:true}))return;
      e.currentTarget.disabled=true;const x=await act("/api/junk/clean",{ids:[...sel]});if(x){toast(`${fmtB(x.freed)} removidos (${x.files} arquivos)`);secJunk(bd)}else e.currentTarget.disabled=false};
    $("#jr",f).onclick=()=>secJunk(bd);bd.appendChild(f)};
  draw();
}
async function secStartup(bd){
  const r=await api("/api/startup");bd.innerHTML="";
  if(!r.items.length){bd.innerHTML=`<div class="res">Nenhum programa encontrado na inicialização.</div>`;return}
  r.items.forEach(i=>{const row=h(`<div class="orow"><div class="t"><b>${esc(i.name)}<span class="tag sm">${esc(i.scope.toUpperCase())}</span></b><small>${esc(i.cmd)}</small></div><div class="sw ${i.enabled?"on":""}"></div></div>`);
    const sw=$(".sw",row);sw.onclick=async()=>{const on=!sw.classList.contains("on");const x=await act("/api/startup/toggle",{id:i.id,on});if(x)sw.classList.toggle("on",x.on)};bd.appendChild(row)});
  bd.appendChild(h(`<div class="res">Desligar só impede de abrir com o Windows — o programa continua instalado.</div>`));
}
async function secGames(bd){
  const draw=()=>{bd.innerHTML="";
    bd.appendChild(h(`<p class="desc" style="margin-top:0">Nomes (ou parte do nome) dos programas dos seus jogos. Enquanto o Color Studio estiver aberto, eles vão para prioridade alta assim que abrirem.</p>`));
    const chips=h(`<div class="chips"></div>`);
    (S.cfg.jogos||[]).forEach((g,ix)=>{const c=h(`<span class="chip">${esc(g)}<button title="Remover">×</button></span>`);
      $("button",c).onclick=async()=>{const x=await act("/api/cfg",{jogos:S.cfg.jogos.filter((_,j)=>j!==ix)});if(x){S.cfg.jogos=x.cfg.jogos;draw()}};chips.appendChild(c)});
    bd.appendChild(chips);
    const row=h(`<div class="urlrow"><input class="inp" placeholder="Ex.: cs2.exe"><button class="btn">Adicionar</button></div>`);
    const add=async()=>{const v=$("input",row).value.trim();if(!v)return;const x=await act("/api/cfg",{jogos:[...(S.cfg.jogos||[]),v]});if(x){S.cfg.jogos=x.cfg.jogos;draw()}};
    $("button",row).onclick=add;$("input",row).onkeydown=e=>{if(e.key==="Enter")add()};bd.appendChild(row);
    const p=h(`<div class="orow"><div class="t"><b>Prioridade alta automática</b><small>Liga ou desliga esse monitoramento.</small></div><div class="sw ${S.cfg.auto_prioridade?"on":""}"></div></div>`);
    const sw=$(".sw",p);sw.onclick=async()=>{const on=!sw.classList.contains("on");const x=await act("/api/cfg",{auto_prioridade:on});if(x){S.cfg.auto_prioridade=on;sw.classList.toggle("on",on)}};bd.appendChild(p)};
  draw();
}
async function secNet(bd){
  bd.innerHTML=`<div class="orow"><div class="t"><b>Limpar cache de DNS</b><small>Ajuda quando sites não abrem depois de trocar de rede ou de DNS.</small></div><button class="btn sm">Limpar</button></div>`;
  $("button",bd).onclick=async()=>{if(await act("/api/dns"))toast("Cache de DNS limpo")};
}

function renderProcessos(){
  const t=$("#tab");t.innerHTML="";
  t.appendChild(titleRow("Processos","Programas que mais usam memória agora, agrupados por nome",`<button class="btn" id="pr">Atualizar</button>`));
  const bx=boxEl("EM EXECUÇÃO","Prioridade alta vale só até o programa fechar. Processos do Windows e deste app ficam protegidos.");t.appendChild(bx);
  const bd=$(".bd",bx);let busy=false;
  const load=async()=>{if(busy||$("#modal").classList.contains("show"))return;busy=true;
    try{const r=await api("/api/procs"),main=$(".main"),y=main.scrollTop,max=Math.max(1,...r.procs.map(p=>p.mem));
      bd.innerHTML="";bd.appendChild(h(`<div class="ptr head"><span>Programa</span><span>Qtd.</span><span>Memória</span><span></span></div>`));
      r.procs.forEach(p=>{
        const row=h(`<div class="ptr"><span class="pn" title="${esc(p.name)}">${esc(p.name)}</span><span>${p.count}</span><span class="pm"><i style="width:${Math.max(3,p.mem/max*100)}%"></i><em>${fmtB(p.mem)}</em></span><span class="pa"></span></div>`),pa=$(".pa",row);
        if(p.protected)pa.appendChild(h(`<span class="tag sm">PROTEGIDO</span>`));
        else{const b1=h(`<button class="btn sm">Prioridade alta</button>`),b2=h(`<button class="btn sm danger">Fechar</button>`);
          b1.onclick=async()=>{if(await act("/api/procs/priority",{name:p.name,level:"alta"}))toast(p.name+": prioridade alta")};
          b2.onclick=async()=>{if(!await modal({title:"Fechar "+p.name,label:`Isso encerra ${p.count} processo(s). Trabalho não salvo nele será perdido.`,ok:"Fechar",danger:true}))return;
            if(await act("/api/procs/kill",{name:p.name})){toast(p.name+" fechado");load()}};
          pa.append(b1,b2)}
        bd.appendChild(row)});
      main.scrollTop=y}catch(e){}busy=false};
  $("#pr").onclick=load;load();timer=setInterval(load,5000);
}

function render(){applyTheme();renderHeader();renderProfiles();
  $(".side").style.display=tab==="ajustes"?"":"none";
  if(render.last!==tab){$(".main").scrollTop=0;render.last=tab}
  clearInterval(timer);timer=null;
  ({inicio:renderInicio,otimizar:renderOtimizar,processos:renderProcessos,ajustes:renderAjustes,personalizacao:renderPersonalizacao})[tab]()}

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
