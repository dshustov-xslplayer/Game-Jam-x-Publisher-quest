#!/usr/bin/env python3
"""xsolla-upload — upload a game build to Xsolla Launcher with Build Loader. No AI needed.

Run without arguments for a guided wizard, or use subcommands:
  xsolla-upload upload --game <file> --name "<Game>" [--platform mac|win|both] [--until check|upload|ready|launcher]
  xsolla-upload upload --game-mac <file> --game-win <file> --name "<Game>"
  xsolla-upload get-launcher --url <Get installer link> [--open]
  xsolla-upload doctor
The API key comes from XSOLLA_BL_API_KEY, --key-file, the saved key (--remember-key) or a hidden prompt.
Python 3.8+, standard library only. macOS and Windows (Build Loader has no Linux version).
"""
import argparse
import filecmp
import getpass
import hashlib
import json
import locale
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

VERSION = "1.1.0"
IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"
TEST_ANY_OS = bool(os.environ.get("XU_TEST_ANY_OS"))   # test hook: allow running the upload on Linux with a stub
HOME = os.path.expanduser("~")
STOPS = ("check", "upload", "ready", "launcher")
# Build Loader stamps every build with the platform it was compiled for (a constant inside the program, sent as
# ?platform=… to the API): the Mac Build Loader always creates macOS builds, the Windows one Windows builds.
# So a build can only be uploaded for the OS of the computer that runs the upload.
HOST = "mac" if IS_MAC else "win" if IS_WIN else os.environ.get("XU_TEST_HOST", "mac")
OS_NAME = {"mac": "macOS", "win": "Windows", "both": "macOS + Windows", "unknown": "unknown"}
ARCHIVE_EXT = (".zip", ".7z", ".rar", ".tar", ".tgz", ".tar.gz", ".tar.xz", ".tar.bz2", ".dmg", ".exe", ".msi")

# ----------------------------------------------------------------------------------------------------------- i18n
def _detect_lang():
    v = os.environ.get("XU_LANG") or os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    if not v and IS_WIN:
        try:
            import ctypes
            return "ru" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0xFF == 0x19 else "en"
        except Exception:
            pass
    if not v:
        try:
            v = locale.getlocale()[0] or ""
        except Exception:
            v = ""
    return "ru" if v.lower().startswith("ru") else "en"

LANG = _detect_lang()
MSG = {
    "ask_game": ("Path to the game (drag the file or folder into this window) ", "Путь к игре (перетащите файл или папку в это окно) "),
    "ask_second": ("Path to the build for the other OS (Enter = none) ", "Путь к билду для второй ОС (Enter — нет) "),
    "ask_name": ("Game name", "Название игры"),
    "ask_until": ("How far to go: check / upload / ready / launcher", "До какого этапа: check / upload / ready / launcher"),
    "ask_key": ("API key (Publisher Account > Launcher > Edit launcher > Games > Set up > Builds > Game builds): ",
                "API key (Личный кабинет > Launcher > Edit launcher > Games > Set up > Builds > Game builds): "),
    "ask_go": ("Upload now? [Y/n] ", "Загружать? [Y/n] "),
    "ask_remember": ("Remember the key for this game on this computer? [y/N] ", "Запомнить ключ этой игры на этом компьютере? [y/N] "),
    "until_help": ("check = only verify the build · upload = upload to Draft · ready = upload + what to fill in Publisher Account "
                   "(stops one step before launcher generation) · launcher = including the generation steps",
                   "check — только проверить билд · upload — загрузить в Draft · ready — загрузить и подсказать, что заполнить "
                   "в кабинете (за шаг до генерации лаунчера) · launcher — включая генерацию"),
    "uploads_as": ("will upload as", "загрузится как"),
    "pending": ("Still to upload from another computer", "Осталось загрузить с другого компьютера"),
    "prep": ("Preparing the build…", "Готовлю билд…"),
    "found": ("Detected", "Определено"),
    "uploading": ("Uploading — the first upload sends the whole game, later ones only patches. Don't close this window.",
                  "Загрузка — первая отправляет игру целиком, следующие только патчи. Не закрывайте окно."),
    "ok_check": ("The build is ready to upload.", "Билд готов к загрузке."),
    "ok_upload": ("Uploaded to Draft — only testers see it.", "Загружено в Draft — видят только тестировщики."),
    "next": ("Next in Publisher Account", "Дальше в личном кабинете"),
    "stop_ready": ("Stop here: the next step is Generate installer (Launcher > Edit launcher > Settings > Launcher build > "
                   "Generate in Draft). It is unstable right now — run it when you decide.",
                   "Здесь остановка: следующий шаг — Generate installer (Launcher > Edit launcher > Settings > Launcher build > "
                   "Generate в секции Draft). Сейчас он нестабилен — запускайте, когда решите."),
    "never_publish": ("Don't press 'Set as published' or 'Publish' until you have tested — players see that.",
                      "Не нажимайте «Set as published» и «Publish», пока не протестировали — это видят игроки."),
    "cancel": ("Cancelled.", "Отменено."),
}
def T(k):
    return MSG[k][1 if LANG == "ru" else 0]

# ----------------------------------------------------------------------------------------------------------- output
class Fail(Exception):
    def __init__(self, reason, hint="", code=1):
        super().__init__(reason)
        self.reason, self.hint, self.code = reason, hint, code

class Out:
    """Human lines go to stdout, or to stderr when --json is set (stdout then carries only the final JSON)."""
    json_mode = False
    quiet = False
    @classmethod
    def say(cls, *a):
        if not cls.quiet:
            print(*a, file=sys.stderr if cls.json_mode else sys.stdout, flush=True)

def run(cmd, **kw):
    kw.setdefault("stdout", subprocess.PIPE); kw.setdefault("stderr", subprocess.STDOUT)
    try:
        return subprocess.run(cmd, **kw)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, b"", b"")

def clean_path(p):
    """Undo what drag-and-drop adds: quotes, trailing spaces, backslash-escaped spaces (macOS Terminal)."""
    p = (p or "").strip().strip('"').strip("'").strip()
    if not IS_WIN:
        p = re.sub(r"\\(.)", r"\1", p)
    return os.path.expanduser(p)

def slugify(name):
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    return re.sub(r"_+", "_", s) or "game"

def safe_dir(p):
    return re.fullmatch(r"[A-Za-z0-9_./\\:-]+", p) is not None

def default_workdir():
    if IS_WIN and os.path.isdir("C:\\"):
        return "C:\\xsolla_builds"
    return os.path.join(HOME, "xsolla_builds")

def mb(n):
    return int(n / 1048576)

def tree_size(d):
    t = 0
    for root, _, files in os.walk(d):
        for f in files:
            p = os.path.join(root, f)
            if not os.path.islink(p):
                try: t += os.path.getsize(p)
                except OSError: pass
    return t

def rmtree(p):
    def onerr(func, path, _):
        try:
            os.chmod(path, stat.S_IWRITE); func(path)
        except Exception:
            pass
    if os.path.lexists(p):
        shutil.rmtree(p, onerror=onerr) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)

# ----------------------------------------------------------------------------------------------------------- tools
def find_7z(kind, tooldir):
    for t in ("7zz", "7z", "7za"):
        w = shutil.which(t)
        if w: return w
    if IS_WIN:
        for base in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
            p = os.path.join(base, "7-Zip", "7z.exe")
            if base and os.path.isfile(p): return p
    if kind == "7z" and tooldir:
        p = os.path.join(tooldir, "Mac", "7zr")          # Build Loader for Mac ships its own 7zr
        if os.access(p, os.X_OK): return p
    return None

def bsdtar():
    w = shutil.which("tar")
    if not w: return None
    r = run([w, "--version"])
    return w if b"bsdtar" in r.stdout.lower() else None

def installer_kind(path):
    if path.lower().endswith(".msi"): return "msi"
    try:
        with open(path, "rb") as f:
            data = f.read(8 * 1048576)
    except OSError:
        return ""
    for marker, kind in ((b"Inno Setup", "inno"), (b"Nullsoft", "nsis"), (b"InstallShield", "installshield"),
                         (b"WiX Toolset", "wix"), (b"Windows Installer XML", "wix")):
        if marker in data: return kind
    return ""

def fmt_of(path):
    l = path.lower().rstrip("/\\")
    for ext, name in ((".zip", "zip"), (".7z", "7z"), (".rar", "rar"), (".dmg", "dmg"), (".app", "app")):
        if l.endswith(ext): return name
    if l.endswith((".tar", ".tgz", ".tar.gz", ".tar.xz", ".tar.bz2")): return "tar"
    if l.endswith((".exe", ".msi")):
        k = installer_kind(path)
        return "installer-" + k if k else "exe"
    return "folder" if os.path.isdir(path) else "file"

# ----------------------------------------------------------------------------------------------------------- unpack
def unzip(src, dst):
    if IS_MAC:
        r = run(["ditto", "-x", "-k", src, dst])          # keeps permissions and framework symlinks
        if r.returncode == 0: return
        raise Fail("cannot unpack %s" % src, r.stdout.decode(errors="replace")[-300:])
    with zipfile.ZipFile(src) as z:
        for info in z.infolist():
            out = z.extract(info, dst)                   # zipfile strips absolute paths and '..'
            mode = (info.external_attr >> 16) & 0o777
            if mode and not IS_WIN:
                try: os.chmod(out, mode)
                except OSError: pass

def untar(src, dst):
    with tarfile.open(src) as t:
        if hasattr(tarfile, "data_filter"):
            t.extractall(dst, filter="data")
        else:
            base = os.path.realpath(dst)
            for m in t.getmembers():
                if not os.path.realpath(os.path.join(dst, m.name)).startswith(base):
                    raise Fail("unsafe path in archive: %s" % m.name)
            t.extractall(dst)

def extract_7z_like(src, dst, kind, tooldir):
    if kind == "rar" and shutil.which("unar"):
        if run(["unar", "-q", "-f", "-o", dst, src]).returncode == 0: return
    z = find_7z(kind, tooldir)
    if z and run([z, "x", "-y", "-o" + dst, src]).returncode == 0: return
    if kind in ("7z", "rar"):
        t = bsdtar()
        if t and run([t, "-xf", src, "-C", dst]).returncode == 0: return
        if IS_WIN and shutil.which("tar") and run(["tar", "-xf", src, "-C", dst]).returncode == 0: return
    hints = {
        "7z": "macOS: brew install sevenzip · Windows: winget install 7zip.7zip",
        "rar": "macOS: brew install unar · Windows: winget install 7zip.7zip",
        "dmg": "open the .dmg on a Mac, or install 7-Zip",
    }
    raise Fail("no extractor for .%s" % kind, hints.get(kind, "install 7-Zip or unpack the archive yourself"), code=2)

def mount_dmg(src, dst, tooldir):
    if not IS_MAC:
        Out.say("NOTE: .dmg outside macOS is unpacked with 7-Zip; uploading macOS builds from a Mac is safer")
        return extract_7z_like(src, dst, "dmg", tooldir)
    m = tempfile.mkdtemp(prefix="xu_dmg_")
    r = subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", m, src],
                       input=b"Y\n" * 20, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)   # 'Y' accepts a license
    if r.returncode != 0:
        os.rmdir(m)
        raise Fail("hdiutil could not mount %s" % src, "the image may be damaged or encrypted")
    try:
        for e in os.listdir(m):
            p = os.path.join(m, e)
            if e.startswith(".") or os.path.islink(p): continue      # .background, Applications shortcut
            r = run(["ditto", p, os.path.join(dst, e)])
            if r.returncode != 0: raise Fail("cannot copy %s from the .dmg" % e)
    finally:
        if run(["hdiutil", "detach", m, "-quiet"]).returncode != 0:
            run(["hdiutil", "detach", m, "-force", "-quiet"])
        try: os.rmdir(m)
        except OSError: pass

def unpack_installer(src, dst, kind, tooldir):
    Out.say("NOTE: %s is a %s installer, not the game itself — unpacking it" % (os.path.basename(src), kind))
    if kind == "inno":
        ie = shutil.which("innoextract")
        if ie and run([ie, "-s", "-d", dst, src]).returncode == 0: return
        raise Fail("Inno Setup installer needs innoextract",
                   "brew install innoextract / innoextract.constexpr.org — or install the game and pass the installed folder", 2)
    if kind in ("nsis", "msi", "wix"):
        z = find_7z("exe", tooldir)
        if z and run([z, "x", "-y", "-o" + dst, src]).returncode == 0:
            rmtree(os.path.join(dst, "$PLUGINSDIR"))
            for f in os.listdir(dst):
                if f.lower().startswith("uninst") and f.lower().endswith(".exe"): rmtree(os.path.join(dst, f))
            return
        raise Fail("%s installer needs 7-Zip to unpack" % kind,
                   "winget install 7zip.7zip / brew install sevenzip — or install the game and pass the installed folder", 2)
    raise Fail("%s installer cannot be unpacked automatically" % kind, "install the game and pass the installed folder", 2)

def copy_any(src, dst):
    if os.path.isdir(src):
        if IS_MAC:
            if run(["ditto", src, dst]).returncode != 0: raise Fail("cannot copy %s" % src)
        else:
            shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
    else:
        os.makedirs(dst, exist_ok=True); shutil.copy2(src, dst)

def stage_input(src, dst, tooldir):
    """Unpack/copy one game input into dst; returns the build root (a single wrapper folder is lifted)."""
    rmtree(dst); os.makedirs(dst)
    f = fmt_of(src)
    if f == "zip": unzip(src, dst)
    elif f == "tar": untar(src, dst)
    elif f in ("7z", "rar"): extract_7z_like(src, dst, f, tooldir)
    elif f == "dmg": mount_dmg(src, dst, tooldir)
    elif f == "app": copy_any(src, os.path.join(dst, os.path.basename(src.rstrip("/\\"))))
    elif f.startswith("installer-"): unpack_installer(src, dst, f.split("-", 1)[1], tooldir)
    elif f == "folder": copy_any(src, dst)
    else: shutil.copy2(src, dst)                         # portable single-file .exe or any single file
    rmtree(os.path.join(dst, "__MACOSX"))
    for root, dirs, files in os.walk(dst):
        for x in files:
            if x == ".DS_Store": os.remove(os.path.join(root, x))
    items = [e for e in os.listdir(dst)]
    if not items: raise Fail("the game archive/folder is empty: %s" % src)
    if len(items) == 1 and os.path.isdir(os.path.join(dst, items[0])) and not items[0].lower().endswith(".app"):
        return os.path.join(dst, items[0])
    return dst

def list_apps(d):
    return sorted(e for e in os.listdir(d) if e.lower().endswith(".app") and os.path.isdir(os.path.join(d, e)))

def list_exes(d):
    skip = re.compile(r"^(unins|vc_redist|dxsetup)|crashhandler|crashreport|setup", re.I)
    return sorted(e for e in os.listdir(d) if e.lower().endswith(".exe") and os.path.isfile(os.path.join(d, e)) and not skip.search(e))

def detect_platform(d):
    a, w = list_apps(d), list_exes(d)
    return "both" if a and w else "mac" if a else "win" if w else "unknown"

def conflicts(mac_root, win_root):
    out = []
    for root, _, files in os.walk(mac_root):
        for x in files:
            p = os.path.join(root, x); rel = os.path.relpath(p, mac_root); o = os.path.join(win_root, rel)
            if os.path.isfile(o) and not filecmp.cmp(p, o, shallow=False): out.append(rel)
    return out

def fix_app_exec_bits(build):
    fixed = []
    for app in list_apps(build):
        macos = os.path.join(build, app, "Contents", "MacOS")
        if not os.path.isdir(macos): continue
        for b in os.listdir(macos):
            p = os.path.join(macos, b)
            if os.path.isfile(p) and not os.access(p, os.X_OK):
                os.chmod(p, os.stat(p).st_mode | 0o755); fixed.append(os.path.relpath(p, build))
    return fixed

# ----------------------------------------------------------------------------------------------------------- Build Loader
def find_loader(arg, tooldir):
    exe_rel = os.path.join("Win", "build_loader.exe") if IS_WIN else os.path.join("Mac", "build_loader")
    if not arg and os.path.isfile(os.path.join(tooldir, exe_rel)): arg = tooldir
    if not arg:
        other = "mac" if IS_WIN else "win"
        for base in (os.path.join(HOME, "Downloads"), os.path.join(HOME, "Desktop")):
            if not os.path.isdir(base): continue
            for root, dirs, files in os.walk(base):
                if root.count(os.sep) - base.count(os.sep) >= 2: dirs[:] = []
                for f in files:
                    l = f.lower()
                    if l.endswith(".zip") and re.search(r"build.?loader", l) and other not in l:
                        arg = os.path.join(root, f); break
                if arg: break
            if arg: break
    if not arg:
        raise Fail("Build Loader not found",
                   "download BuildLoader.zip (%s) in Publisher Account > your game > Set up > Builds > Game builds into Downloads"
                   % ("Windows" if IS_WIN else "Mac"), 2)
    if os.path.isfile(arg):
        rmtree(tooldir); os.makedirs(tooldir); unzip(arg, tooldir); arg = tooldir
    for c in (os.path.join(arg, exe_rel), os.path.join(arg, os.path.basename(exe_rel))):
        if os.path.isfile(c): break
    else:
        raise Fail("build_loader not found in %s" % arg, "expected %s inside BuildLoader.zip" % exe_rel)
    if IS_MAC:
        run(["xattr", "-dr", "com.apple.quarantine", os.path.dirname(c)])
        if not TEST_ANY_OS and os.uname().machine == "arm64" and b"arm64" not in run(["file", c]).stdout:
            if run(["/usr/bin/arch", "-x86_64", "/usr/bin/true"]).returncode != 0:
                raise Fail("Build Loader is Intel-only and Rosetta is not installed",
                           "softwareupdate --install-rosetta --agree-to-license", 2)
    if IS_WIN:
        for root, _, files in os.walk(os.path.dirname(c)):
            for x in files:
                try: os.remove(os.path.join(root, x) + ":Zone.Identifier")   # 'Unblock-File'
                except OSError: pass
    elif not os.access(c, os.X_OK):
        os.chmod(c, 0o755)
    return c

def run_loader(bl, args, key, log_path, echo):
    mask = (key[:4] + "…" + key[-3:]) if key else None
    with open(log_path, "w", encoding="utf-8") as log:
        p = subprocess.Popen([bl] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        text = []
        for raw in iter(p.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if key: line = line.replace(key, mask)
            log.write(line + "\n"); log.flush(); text.append(line)
            if echo and line.strip(): Out.say("  " + line)
        p.wait()
    results = re.findall(r"Result:\s*([A-Za-z]+)", "\n".join(text))
    return p.returncode, (results[-1] if results else "?"), text

# ----------------------------------------------------------------------------------------------------------- key storage
def _key_store_path(slug):
    base = os.environ.get("APPDATA") or os.path.join(HOME, ".config")
    return os.path.join(base, "xsolla-upload", slug + ".key")

def load_saved_key(slug):
    if IS_MAC:
        r = run(["security", "find-generic-password", "-s", "xsolla-upload", "-a", slug, "-w"], stderr=subprocess.DEVNULL)
        return r.stdout.decode().strip() if r.returncode == 0 else ""
    if IS_WIN:
        p = _key_store_path(slug)
        if not os.path.isfile(p): return ""
        ps = ("$s = Get-Content -LiteralPath '%s' | ConvertTo-SecureString; "
              "[Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))" % p)
        r = run(["powershell", "-NoProfile", "-Command", ps], stderr=subprocess.DEVNULL)
        return r.stdout.decode().strip() if r.returncode == 0 else ""
    return ""

def save_key(slug, key):
    if IS_MAC:   # login Keychain
        run(["security", "add-generic-password", "-U", "-s", "xsolla-upload", "-a", slug, "-w", key])
        return True
    if IS_WIN:   # DPAPI: readable only by this Windows user
        p = _key_store_path(slug); os.makedirs(os.path.dirname(p), exist_ok=True)
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "$input | ConvertTo-SecureString -AsPlainText -Force | ConvertFrom-SecureString | Set-Content -LiteralPath '%s'" % p],
                           input=key.encode(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return r.returncode == 0
    return False

def resolve_key(a, slug, interactive):
    if os.environ.get("XSOLLA_BL_API_KEY"): return os.environ["XSOLLA_BL_API_KEY"].strip(), "env"
    if a.key_file:
        with open(clean_path(a.key_file), encoding="utf-8") as f: return f.read().strip(), "file"
    k = load_saved_key(slug)
    if k: return k, "saved"
    if interactive and sys.stdin.isatty():
        k = getpass.getpass(T("ask_key")).strip()
        if k: return k, "prompt"
    raise Fail("API key is missing",
               "set XSOLLA_BL_API_KEY, use --key-file, or run interactively "
               "(Publisher Account > Launcher > Edit launcher > Games > Set up > Builds > Game builds > API key)", 2)

# ----------------------------------------------------------------------------------------------------------- checklist
def checklist(until, descr, apps, exes):
    steps = []
    if until in ("ready", "launcher"):
        steps.append("Builds › Game builds: row \"%s\", Branch = draft (refresh after a minute if missing)" % descr
                     if LANG != "ru" else "Builds › Game builds: строка «%s», Branch = draft (если нет — обновите через минуту)" % descr)
        ef = []
        if apps: ef.append("macOS: " + apps[0])
        if exes: ef.append("Windows: " + exes[0])
        if ef: steps.append("Executable files → " + ", ".join(ef))
    if until == "ready":
        steps.append(T("stop_ready"))
    if until == "launcher":
        steps.append("Launcher > Edit launcher > Settings > Launcher build > Generate (Draft) — may take hours / может занять часы")
        steps.append("Get installer (Draft) → xsolla-upload get-launcher --url \"<link>\" --open")
    return steps

# ----------------------------------------------------------------------------------------------------------- upload
def cmd_upload(a, interactive=False):
    res = {"tool": "xsolla-upload", "version": VERSION, "result": "FAILED", "stop_point": a.until}
    if not (IS_MAC or IS_WIN or TEST_ANY_OS) and a.until != "check":
        raise Fail("Build Loader runs only on macOS and Windows", "use a Mac or a PC (or --until check to just verify the build)")
    games = {"one": a.game, "mac": a.game_mac, "win": a.game_win}
    if a.game and (a.game_mac or a.game_win): raise Fail("use either --game or --game-mac/--game-win")
    if a.game_mac and a.game_win: platform_req = "both"
    elif a.game_mac: games["one"], platform_req = a.game_mac, "mac"
    elif a.game_win: games["one"], platform_req = a.game_win, "win"
    else: platform_req = a.platform
    if platform_req == "both" and not (a.game_mac and a.game_win):
        raise Fail("--platform both needs --game-mac and --game-win")
    for g in (games["one"] if platform_req != "both" else None, a.game_mac, a.game_win):
        if g and not os.path.exists(g): raise Fail("game not found: %s" % g)
    if platform_req != "both" and not games["one"]: raise Fail("no game given", "pass --game <file>", 2)
    first = games["one"] or a.game_mac
    name = a.name or re.sub(r"(\.app)?(\.zip|\.7z|\.rar|\.dmg|\.tar\.gz|\.tgz|\.tar|\.exe|\.msi)?$", "",
                            os.path.basename(first.rstrip("/\\")), flags=re.I)
    slug = slugify(name)
    workdir = os.path.abspath(a.workdir or default_workdir())
    if not safe_dir(workdir): raise Fail("work folder must be Latin letters/digits without spaces: %s" % workdir, "use --workdir")
    ts = time.strftime("%Y%m%d-%H%M%S")
    build = os.path.join(workdir, slug, "build")          # stable per game -> later uploads are patches
    logdir = os.path.join(workdir, slug, "logs", ts)
    tooldir = os.path.join(workdir, "_build_loader")
    os.makedirs(logdir, exist_ok=True)
    res.update(game_name=name, build_dir=build, log_dir=logdir)

    other = "win" if HOST == "mac" else "mac"
    other_game = None
    if platform_req == "both" and a.until != "check":
        # one upload = one platform: take the part for this computer, leave the other for a computer with that OS
        games["one"] = a.game_mac if HOST == "mac" else a.game_win
        other_game = a.game_win if HOST == "mac" else a.game_mac
        platform_req = "auto"

    Out.say(T("prep"))
    inc = os.path.join(workdir, slug, "_incoming"); rmtree(inc)
    if platform_req == "both":           # check only: verify both parts and that they fit together
        rw = stage_input(a.game_win, os.path.join(inc, "win"), tooldir)
        rm_ = stage_input(a.game_mac, os.path.join(inc, "mac"), tooldir)
        if detect_platform(rw) == "mac" and detect_platform(rm_) == "win":   # given the other way round
            rw, rm_ = rm_, rw; a.game_mac, a.game_win = a.game_win, a.game_mac
        if detect_platform(rw) != "win": raise Fail("--game-win has no .exe in its root (or it is a macOS build)")
        if detect_platform(rm_) != "mac": raise Fail("--game-mac has no .app in its root (or it is a Windows build)")
        root = os.path.join(inc, "merged"); copy_any(rw, root); copy_any(rm_, root)
        res["input_format"] = "mac:%s win:%s" % (fmt_of(a.game_mac), fmt_of(a.game_win)); plat = "both"
        res.setdefault("warnings", []).append(
            "each OS is a separate upload from a computer with that OS: macOS part from a Mac, Windows part from a PC")
    else:
        root = stage_input(games["one"], os.path.join(inc, "one"), tooldir)
        det = detect_platform(root)
        if other_game and det == other:                               # the two files were given the other way round
            games["one"], other_game = other_game, games["one"]
            root = stage_input(games["one"], os.path.join(inc, "one"), tooldir); det = detect_platform(root)
        res["input_format"] = fmt_of(games["one"])
        if platform_req in ("mac", "win") and det not in ("unknown", "both", platform_req):
            raise Fail("you chose %s, but the build looks like %s" % (OS_NAME[platform_req], OS_NAME[det]),
                       "check which file is for which OS")
        plat = det if platform_req == "auto" else platform_req
        target = plat if plat in ("mac", "win") else HOST
        if target != HOST:
            msg = ("this is a %s build, but this computer is %s — Build Loader registers a build for the OS of the "
                   "computer it runs on, so it would appear in the launcher as a %s build"
                   % (OS_NAME[target], OS_NAME[HOST], OS_NAME[HOST]))
            hint = ("upload it from a %s computer: xsolla-upload upload --game \"%s\" --name \"%s\" (with the %s BuildLoader.zip)"
                    % ("Windows" if target == "win" else "Mac", os.path.basename(games["one"]), name,
                       "Windows" if target == "win" else "Mac"))
            if a.until != "check":
                raise Fail(msg, hint, 2)
            res.setdefault("warnings", []).append(msg + ". " + hint)
        if plat == "both":
            res.setdefault("warnings", []).append("the build root has both .app and .exe — it will be registered as a %s build only" % OS_NAME[HOST])
    res["uploads_as"] = OS_NAME[HOST]
    if other_game:
        res["pending"] = {"platform": OS_NAME[other], "game": other_game,
                          "hint": "upload this part from a %s computer with the same API key: xsolla-upload upload --game \"%s\" --name \"%s\""
                                  % ("Windows" if other == "win" else "Mac", os.path.basename(other_game), name)}

    key = ""
    if a.until != "check":
        key, res["key_source"] = resolve_key(a, slug, interactive)
        bl = find_loader(clean_path(a.loader) if a.loader else "", tooldir)
        res["build_loader"] = bl
    rmtree(build); copy_any(root, build); rmtree(inc)
    fixed = fix_app_exec_bits(build)
    for f in fixed: Out.say("FIXED: made executable: " + f)
    apps, exes = list_apps(build), list_exes(build)
    size = tree_size(build); free = shutil.disk_usage(workdir).free
    res.update(platform=plat, executables={"macos": apps, "windows": exes}, size_mb=mb(size), free_disk_mb=mb(free), fixed=fixed)
    if free < size * 1.5: res.setdefault("warnings", []).append("free disk < 1.5 × build size (Build Loader requirement)")
    if plat == "unknown": res.setdefault("warnings", []).append("no .app or .exe in the build root — the launcher starts a file from the root")
    descr = a.descr or "%s [%s] %s" % (name, plat, ts)
    if len(descr) > 512: raise Fail("description longer than 512 characters")
    res["descr"] = descr
    Out.say("%s: %s → %s · macOS: %s · Windows: %s · %d MB · %s: %s" % (T("found"), res["input_format"], OS_NAME[plat],
            ", ".join(apps) or "—", ", ".join(exes) or "—", res["size_mb"], T("uploads_as"), res["uploads_as"]))
    for w in res.get("warnings", []): Out.say("WARNING: " + w)

    if a.until == "check":
        res["result"] = "SUCCESS"; res["message"] = T("ok_check"); return res
    if interactive and sys.stdin.isatty() and input(T("ask_go")).strip().lower() in ("n", "no", "н", "нет"):
        raise Fail(T("cancel"), code=2)

    echo = not Out.json_mode and not a.quiet
    rc, r, txt = run_loader(bl, ["--init", "--api-key", key, "--game-path", build], key, os.path.join(logdir, "init.log"), False)
    res.update(init_exit=rc, init_result=r)
    if rc != 0 or r not in ("success", "?"):
        raise Fail("Build Loader init failed (exit %d, result %s)" % (rc, r),
                   "check the API key of this game; log: %s\n%s" % (os.path.join(logdir, "init.log"), "\n".join(txt[-8:])))
    args = ["--update", "--game-path", build, "--descr", descr, "--no-progressbar"]
    if a.stage == "draft": args.append("--set-build-on-test")
    if a.ignore: args += ["--ignore-file-path", clean_path(a.ignore), "--use-ignore-file"]
    if a.retry_count: args += ["--retry-count", str(a.retry_count)]
    if a.retry_timeout: args += ["--retry-timeout", str(a.retry_timeout)]
    if a.chunk_size: args += ["--chunk-size", a.chunk_size]
    Out.say(T("uploading"))
    t0 = time.time()
    rc, r, txt = run_loader(bl, args, key, os.path.join(logdir, "update.log"), echo)
    res.update(update_exit=rc, update_result=r, update_seconds=int(time.time() - t0))
    run_loader(bl, ["--builds-list"], key, os.path.join(logdir, "builds_list.log"), False)
    if rc != 0 or r not in ("success", "?"):
        net = any(re.search(r"network|timeout|retry|connection", l, re.I) for l in txt[-30:])
        raise Fail("upload failed (exit %d, result %s)" % (rc, r),
                   ("network problem: rerun with --retry-count 10 --retry-timeout 60 --chunk-size 100mb, try another network/VPN off"
                    if net else "see %s" % os.path.join(logdir, "update.log")) + "\n" + "\n".join(txt[-8:]))
    if a.remember_key and res.get("key_source") in ("prompt", "env", "file"):
        res["key_saved"] = save_key(slug, key)
    elif interactive and res.get("key_source") == "prompt" and sys.stdin.isatty() and (IS_MAC or IS_WIN):
        if input(T("ask_remember")).strip().lower() in ("y", "yes", "д", "да"): res["key_saved"] = save_key(slug, key)
    res["result"] = "SUCCESS"; res["message"] = T("ok_upload")
    res["checklist"] = checklist(a.until, descr, apps if HOST == "mac" else [], exes if HOST == "win" else [])
    if a.receipt or os.environ.get("XU_QUEST_RECEIPT"):
        res["receipt"] = build_receipt(res, key, name, games["one"] or a.game_mac or a.game_win, bl, logdir)
    return res


# ----------------------------------------------------------------------------------------------------------- quest receipt
# Game Jam "Launch Quest": after a successful upload the tool prints a receipt the quest page can check
# (genuine Build Loader, key accepted by Xsolla, game name + archive, build in Draft). It holds only a masked key.
QUEST_SALT = "xsolla-launch-quest-v1"

def loader_signature(bl):
    """(signer, valid): macOS TeamIdentifier of the Build Loader binary, Windows Authenticode subject."""
    try:
        if IS_MAC:
            out = run(["codesign", "-dv", "--verbose=2", bl]).stdout.decode(errors="replace")
            m = re.search(r"TeamIdentifier=(\S+)", out)
            return (m.group(1) if m else "none"), run(["codesign", "--verify", "--strict", bl]).returncode == 0
        if IS_WIN:
            ps = "$s=Get-AuthenticodeSignature -LiteralPath '%s'; \"$($s.Status)|$($s.SignerCertificate.Subject)\"" % bl
            o = run(["powershell", "-NoProfile", "-Command", ps]).stdout.decode(errors="replace").strip()
            st, _, subj = o.partition("|")
            return (subj.strip().replace("\n", " ") or "none"), st.strip() == "Valid"
    except Exception:
        pass
    return "none", False

def build_receipt(res, key, name, game, bl, logdir):
    ver = ""
    try:
        with open(os.path.join(logdir, "init.log"), encoding="utf-8", errors="replace") as f:
            m = re.search(r"build loader version:\s*(\S+)", f.read(), re.I); ver = m.group(1) if m else ""
    except OSError:
        pass
    signer, valid = loader_signature(bl)
    try:
        with open(bl, "rb") as f: binsha = hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        binsha = "unknown"
    listed = "unknown"
    try:
        with open(os.path.join(logdir, "builds_list.log"), encoding="utf-8", errors="replace") as f:
            if res.get("descr") and res["descr"] in f.read(): listed = "yes"
    except OSError:
        pass
    size = tree_size(game) if os.path.isdir(game) else (os.path.getsize(game) if os.path.exists(game) else 0)
    plat, ex = res.get("platform", "unknown"), res.get("executables", {})
    pick = ex.get("macos", []) if plat == "mac" else ex.get("windows", []) if plat == "win" else ex.get("macos", []) + ex.get("windows", [])
    lines = ["XSOLLA-QUEST-RECEIPT v1",
             "run_id=" + os.urandom(4).hex(),
             "created_utc=" + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "builder_version=" + (ver or "unknown"),
             "builder_signer=" + signer,
             "builder_signature_valid=" + ("yes" if valid else "no"),
             "builder_sha256_16=" + binsha,
             "key_mask=%s...%s" % (key[:4], key[-3:]),
             "key_length=%d" % len(key),
             "init_result=" + str(res.get("init_result", "?")),
             "game_name=" + name,
             "game_archive=" + os.path.basename(game.rstrip("/\\")),
             "game_bytes=%d" % size,
             "platform=" + plat,
             "executable=" + (pick[0] if pick else "none"),
             "stage=draft",
             "update_result=" + str(res.get("update_result", "?")),
             "descr=" + str(res.get("descr", "")),
             "build_in_list=" + listed,
             "tool_version=" + VERSION]
    body = "\n".join(lines)
    return body + "\ncheck=" + hashlib.sha256((body + QUEST_SALT).encode("utf-8")).hexdigest()[:16]

def emit_receipt(text):
    dest = os.path.join(HOME, "Desktop")
    if not os.path.isdir(dest): dest = HOME
    path = os.path.join(dest, "xsolla-quest-receipt.txt")
    try:
        with open(path, "w", encoding="utf-8") as f: f.write(text + "\n")
    except OSError:
        path = ""
    copied = False
    try:
        if IS_MAC: copied = subprocess.run(["pbcopy"], input=text.encode("utf-8")).returncode == 0
        elif IS_WIN: copied = subprocess.run(["clip"], input=text.encode("utf-8")).returncode == 0
    except Exception:
        pass
    bar = "-" * 60
    print("\n" + bar + "\nLaunch Quest receipt / Квитанция для квеста\n" + bar)
    print(text)
    print(bar)
    print(("Copied to the clipboard. " if copied else "") + ("Saved: " + path if path else "") +
          "\nPaste it into the quest page. It holds only a masked key. / Вставьте её на странице квеста.")

# ----------------------------------------------------------------------------------------------------------- get-launcher
def cmd_get_launcher(a):
    out = os.path.join(a.out or os.path.join(default_workdir(), "_launcher"), time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out, exist_ok=True)
    res = {"tool": "xsolla-upload", "version": VERSION, "result": "FAILED", "out_dir": out, "files": []}
    for u in [x for s in a.url for x in s.split(",") if x.strip()]:
        u = u.strip()
        if not u.startswith("https://"): Out.say("SKIP (not https): " + u); continue
        Out.say("downloading from %s …" % urllib.parse.urlparse(u).netloc)
        req = urllib.request.Request(u, headers={"User-Agent": "xsolla-upload/" + VERSION})
        with urllib.request.urlopen(req, timeout=60) as r:
            cd = r.headers.get("Content-Disposition") or ""
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
            fname = urllib.parse.unquote(m.group(1)) if m else os.path.basename(urllib.parse.urlparse(r.geturl()).path) or "launcher_installer"
            fname = re.sub(r'[\\/:*?"<>|]', "_", fname)
            p = os.path.join(out, fname)
            with open(p, "wb") as f: shutil.copyfileobj(r, f, 1048576)
        res["files"].append({"file": fname, "size_mb": mb(os.path.getsize(p)), "signature": signature(p)})
        Out.say("  %s · %d MB · %s" % (fname, res["files"][-1]["size_mb"], res["files"][-1]["signature"]))
    if not res["files"]:
        raise Fail("nothing downloaded", "the link may have expired — copy it again from Get installer")
    if a.open:
        want = (".exe", ".msi") if IS_WIN else (".dmg", ".pkg", ".zip")
        pick = [f["file"] for f in res["files"] if f["file"].lower().endswith(want)]
        if not pick: raise Fail("no installer for this OS among the downloads")
        p = os.path.join(out, pick[0])
        if IS_WIN: os.startfile(p)          # noqa
        elif IS_MAC: run(["open", p])
        res["opened"] = pick[0]
    res["result"] = "SUCCESS"
    return res

def signature(p):
    l = p.lower()
    if IS_WIN and l.endswith((".exe", ".msi")):
        r = run(["powershell", "-NoProfile", "-Command",
                 "$s=Get-AuthenticodeSignature -LiteralPath '%s'; \"$($s.Status) | $($s.SignerCertificate.Subject)\"" % p])
        return r.stdout.decode(errors="replace").strip()
    if IS_MAC and l.endswith(".pkg"):
        return " ".join(run(["pkgutil", "--check-signature", p]).stdout.decode(errors="replace").split()[:12])
    if IS_MAC and l.endswith((".dmg", ".app")):
        a = [x for x in run(["codesign", "-dv", "--verbose=2", p]).stdout.decode(errors="replace").splitlines() if x.startswith("Authority=")]
        g = run(["spctl", "-a", "-t", "open", "--context", "context:primary-signature", "-v", p]).stdout.decode(errors="replace").strip()
        who = a[0] if a else "unsigned"
        return who + (" | " + g.splitlines()[-1] if g else "")
    return "not checked on this OS"

# ----------------------------------------------------------------------------------------------------------- doctor
def cmd_doctor(a):
    wd = a.workdir or default_workdir()
    tooldir = os.path.join(wd, "_build_loader")
    info = {"tool": "xsolla-upload", "version": VERSION, "os": sys.platform, "python": sys.version.split()[0],
            "workdir": wd, "workdir_ok": safe_dir(os.path.abspath(wd))}
    try:
        info["machine"] = os.uname().machine
    except AttributeError:
        info["machine"] = os.environ.get("PROCESSOR_ARCHITECTURE", "")
    if IS_MAC and info["machine"] == "arm64":
        info["rosetta"] = run(["/usr/bin/arch", "-x86_64", "/usr/bin/true"]).returncode == 0
    try:
        info["build_loader"] = find_loader("", tooldir)
    except Fail as e:
        info["build_loader"] = "MISSING: " + e.hint
    info["7z"] = find_7z("7z", tooldir) or "missing"
    info["unar"] = shutil.which("unar") or "missing"
    info["innoextract"] = shutil.which("innoextract") or "missing"
    try:
        info["free_disk_mb"] = mb(shutil.disk_usage(wd if os.path.isdir(wd) else HOME).free)
    except OSError:
        pass
    info["result"] = "SUCCESS"
    for k, v in info.items():
        Out.say("%-14s %s" % (k, v))
    return info

# ----------------------------------------------------------------------------------------------------------- config + wizard
def load_config(path):
    p = clean_path(path) if path else ("xsolla-build.json" if os.path.isfile("xsolla-build.json") else "")
    if not p: return {}
    with open(p, encoding="utf-8") as f:
        c = json.load(f)
    base = os.path.dirname(os.path.abspath(p))
    for k in ("game", "game_mac", "game_win", "ignore"):
        if c.get(k) and not os.path.isabs(c[k]): c[k] = os.path.join(base, c[k])
    return c

def wizard():
    print("xsolla-upload %s — %s" % (VERSION, "загрузка игры в Xsolla Launcher" if LANG == "ru" else "upload a game to Xsolla Launcher"))
    g = clean_path(input(T("ask_game")))
    while not os.path.exists(g):
        print("  not found / не найдено: %s" % g); g = clean_path(input(T("ask_game")))
    a = build_parser().parse_args(["upload"])
    f = fmt_of(g)
    # a second build only makes sense if the first one is clearly single-OS
    quest = bool(os.environ.get("XU_QUEST_RECEIPT"))
    second = "" if quest else clean_path(input(T("ask_second")))
    if second and os.path.exists(second):
        mac_first = f in ("dmg", "app") or g.lower().rstrip("/").endswith(".app.zip")
        a.game_mac, a.game_win = (g, second) if mac_first else (second, g)
    else:
        a.game = g
    default_name = re.sub(r"(\.app)?(\.zip|\.7z|\.rar|\.dmg|\.tar\.gz|\.tgz|\.tar|\.exe|\.msi)?$", "", os.path.basename(g.rstrip("/\\")), flags=re.I)
    a.name = input("%s [%s]: " % (T("ask_name"), default_name)).strip() or default_name
    if quest:
        u = "ready"
    else:
        print("  " + T("until_help"))
        u = input("%s [ready]: " % T("ask_until")).strip().lower() or "ready"
    a.until = u if u in STOPS else "ready"
    return a

def build_parser():
    p = argparse.ArgumentParser(prog="xsolla-upload", description="Upload a game build to Xsolla Launcher with Build Loader.")
    p.add_argument("--version", action="version", version="xsolla-upload " + VERSION)
    sub = p.add_subparsers(dest="cmd")
    u = sub.add_parser("upload", help="prepare and upload a build")
    u.add_argument("--game", help=".zip .7z .rar .tar.gz .dmg, folder, .app, portable .exe or installer .exe/.msi")
    u.add_argument("--game-mac"); u.add_argument("--game-win")
    u.add_argument("--platform", choices=("auto", "mac", "win", "both"), default="auto")
    u.add_argument("--name"); u.add_argument("--descr")
    u.add_argument("--until", choices=STOPS, default="ready", help="check | upload | ready (default) | launcher")
    u.add_argument("--stage", choices=("draft", "none"), default="draft")
    u.add_argument("--loader", help="BuildLoader.zip or unpacked folder (default: found automatically)")
    u.add_argument("--workdir"); u.add_argument("--ignore", help='JSON {"ignoring_files": [...]}')
    u.add_argument("--key-file", help="file with the API key (or set XSOLLA_BL_API_KEY)")
    u.add_argument("--remember-key", action="store_true", help="save the key (macOS Keychain / Windows DPAPI)")
    u.add_argument("--retry-count", type=int); u.add_argument("--retry-timeout", type=int); u.add_argument("--chunk-size")
    u.add_argument("--config", help="xsolla-build.json (default: ./xsolla-build.json if present)")
    u.add_argument("--json", action="store_true"); u.add_argument("--quiet", action="store_true")
    u.add_argument("--receipt", action="store_true", help="print a Launch Quest receipt after a successful upload (also: XU_QUEST_RECEIPT=1)")
    g = sub.add_parser("get-launcher", help="download + verify the generated launcher installer")
    g.add_argument("--url", action="append", required=True); g.add_argument("--out"); g.add_argument("--open", action="store_true")
    g.add_argument("--json", action="store_true")
    d = sub.add_parser("doctor", help="check this computer: Build Loader, Rosetta, extractors, disk")
    d.add_argument("--workdir"); d.add_argument("--json", action="store_true")
    return p

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    interactive = False
    if not argv:
        if not sys.stdin.isatty():
            build_parser().print_help(); return 2
        try:
            a = wizard()
        except (KeyboardInterrupt, EOFError):
            print("\n" + T("cancel")); return 2
        interactive = True
    else:
        a = build_parser().parse_args(argv)
        if not a.cmd:
            build_parser().print_help(); return 2
    Out.json_mode = bool(getattr(a, "json", False))
    Out.quiet = bool(getattr(a, "quiet", False))
    try:
        if a.cmd == "upload":
            if not interactive:   # xsolla-build.json fills in everything not given on the command line
                explicit = {t.split("=")[0].lstrip("-").replace("-", "_") for t in argv if t.startswith("--")}
                for k, v in load_config(a.config).items():
                    if hasattr(a, k) and k not in explicit:
                        setattr(a, k, v)
            for k in ("game", "game_mac", "game_win"):
                if getattr(a, k): setattr(a, k, clean_path(getattr(a, k)))
            res = cmd_upload(a, interactive)
        elif a.cmd == "get-launcher":
            res = cmd_get_launcher(a)
        else:
            res = cmd_doctor(a)
        code = 0
    except KeyboardInterrupt:
        res, code = {"result": "FAILED", "reason": T("cancel")}, 2
    except Fail as e:
        res, code = {"result": "NEEDS_INPUT" if e.code == 2 else "FAILED", "reason": e.reason, "hint": e.hint}, e.code
    if Out.json_mode:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        if res.get("result") == "SUCCESS" and a.cmd == "upload":
            print("\n✔ " + res.get("message", ""))
            if res.get("checklist"):
                print(T("next") + ":")
                for i, s in enumerate(res["checklist"], 1): print("  %d. %s" % (i, s))
                print("  · " + T("never_publish"))
            if res.get("pending"):
                print("%s (%s): %s" % (T("pending"), res["pending"]["platform"], res["pending"]["hint"]))
            if res.get("receipt"): emit_receipt(res["receipt"])
            print("RESULT: SUCCESS: %s" % (res.get("descr") or res.get("build_dir")))
        elif res.get("result") != "SUCCESS":
            print("\n✖ %s" % res.get("reason", ""))
            if res.get("hint"): print("  → " + res["hint"].replace("\n", "\n    "))
            print("RESULT: %s: %s" % (res["result"], res.get("reason", "")))
    return code

if __name__ == "__main__":
    sys.exit(main())
