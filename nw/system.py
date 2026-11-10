"""Operating-system specifics (Windows vs Linux) in one place.

The *target* platform is chosen by the user (GUI startup dialog / CLI --platform).
A few low-level things always follow the real OS because getting them wrong would
crash (e.g. Windows-only process flags), and the config folder must be known before
any settings are loaded.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

PLATFORMS = ("windows", "linux")
REAL_OS = "windows" if os.name == "nt" else "linux"

_state = {"platform": REAL_OS, "ffmpeg_dir": ""}
_cache: dict = {}

# Decode subprocess text as UTF-8 regardless of the Windows code page.
TEXT = dict(text=True, encoding="utf-8", errors="replace")


def detected_platform() -> str:
    return REAL_OS


def configure(platform: str | None = None, ffmpeg_dir: str | None = None) -> list[str]:
    """Set the target platform and an optional ffmpeg folder. Returns warnings to show."""
    warnings = []
    if platform:
        p = platform.strip().lower()
        if p not in PLATFORMS:
            raise ValueError(f"platform must be one of {', '.join(PLATFORMS)}")
        _state["platform"] = p
        if p != REAL_OS:
            warnings.append(f"Platform is set to {p.title()}, but this computer is running "
                            f"{REAL_OS.title()}. Fonts, video player, ffmpeg locations and GPU "
                            "backends are chosen for the wrong system and may not work.")
    if ffmpeg_dir is not None:
        _state["ffmpeg_dir"] = ffmpeg_dir.strip()
    _cache.clear()
    return warnings


def target() -> str:
    return _state["platform"]


def is_windows() -> bool:
    return _state["platform"] == "windows"


# --------------------------------------------------------------------------- processes


def popen_kw() -> dict:
    """Extra Popen/run kwargs: stop every ffmpeg call flashing a console window on Windows."""
    if REAL_OS == "windows":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def detached_kw() -> dict:
    """Kwargs for launching a long-lived app (video player) independent of Nightwatch."""
    if REAL_OS == "windows":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        return {"creationflags": flags}
    return {"start_new_session": True}


# --------------------------------------------------------------------------- ffmpeg


def _exe(name: str) -> str:
    return name + ".exe" if REAL_OS == "windows" else name


def _search_dirs() -> list[Path]:
    dirs: list[Path] = []
    if is_windows():
        la = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        pd = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
        dirs += [la / "Microsoft" / "WinGet" / "Links",                 # winget
                 Path.home() / "scoop" / "shims",                       # scoop
                 pd / "chocolatey" / "bin",                             # chocolatey
                 pf / "ffmpeg" / "bin", Path(r"C:\ffmpeg\bin")]
        pkgs = la / "Microsoft" / "WinGet" / "Packages"
        if pkgs.is_dir():
            dirs += sorted(pkgs.glob("Gyan.FFmpeg*/*/bin"), reverse=True)
    else:
        dirs += [Path("/usr/bin"), Path("/usr/local/bin"), Path.home() / ".local" / "bin"]
    return dirs


def tool(name: str) -> str | None:
    """Full path to ffmpeg/ffprobe: custom folder, then PATH, then common install locations."""
    key = "tool:" + name
    if key in _cache:
        return _cache[key]
    exe = _exe(name)
    found = None
    custom = _state["ffmpeg_dir"] or os.environ.get("NIGHTWATCH_FFMPEG_DIR", "")
    if custom:
        for d in (Path(custom), Path(custom) / "bin"):
            if (d / exe).is_file():
                found = str(d / exe)
                break
    if found is None:
        found = shutil.which(name)
    if found is None:
        for d in _search_dirs():
            if (d / exe).is_file():
                found = str(d / exe)
                break
    _cache[key] = found
    return found


def ffmpeg() -> str:
    return tool("ffmpeg") or _exe("ffmpeg")


def ffprobe() -> str:
    return tool("ffprobe") or _exe("ffprobe")


def install_hint() -> str:
    if is_windows():
        return ("Install it with:  winget install Gyan.FFmpeg   (then open a new window), "
                "or set the ffmpeg folder in Nightwatch's setup window / --ffmpeg-dir.")
    return "Install it with:  sudo pacman -S ffmpeg"


def ffmpeg_version() -> str | None:
    if "version" in _cache:
        return _cache["version"]
    v = None
    try:
        out = subprocess.run([ffmpeg(), "-hide_banner", "-version"], capture_output=True, timeout=15,
                             **TEXT, **popen_kw())
        if out.returncode == 0 and out.stdout:
            v = out.stdout.splitlines()[0]
    except (OSError, subprocess.TimeoutExpired):
        pass
    _cache["version"] = v
    return v


# --------------------------------------------------------------------------- config


def config_dir() -> Path:
    if REAL_OS == "windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "nightwatch"


def default_output_dir() -> Path:
    return Path.home() / "Videos" / "nightwatch"


# --------------------------------------------------------------------------- fonts


_WIN_FONTS = ["segoeui.ttf", "arial.ttf", "calibri.ttf", "tahoma.ttf", "consola.ttf"]
_LINUX_FONTS = [
    "/usr/share/fonts/TTF/DejaVuSans.ttf",                       # Arch ttf-dejavu
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",                # Arch noto-fonts
    "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",    # Arch ttf-liberation
    "/usr/share/fonts/TTF/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",           # Debian/Ubuntu
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",                    # Fedora
]


def font_files() -> list[Path]:
    """Candidate TTF files for the burned-in overlay, best first."""
    if is_windows():
        fdir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        cands = [fdir / f for f in _WIN_FONTS]
        cands += [Path(p) for p in _LINUX_FONTS]       # harmless if the platform choice is wrong
    else:
        cands = [Path(p) for p in _LINUX_FONTS]
        fdir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        cands += [fdir / f for f in _WIN_FONTS]
    return [c for c in cands if c.is_file()]


# --------------------------------------------------------------------------- playback


def _win_app(*rel: str) -> str | None:
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            p = Path(base, *rel)
            if p.is_file():
                return str(p)
    return None


def player_command(path: str, t: float = 0.0) -> list[str] | None:
    """Command that opens `path` at `t` seconds, or None if no seekable player is found."""
    t = max(0.0, t)
    mpv = shutil.which("mpv")
    vlc = shutil.which("vlc")
    if is_windows():
        mpv = mpv or _win_app("mpv", "mpv.exe") or (str(Path.home() / "scoop" / "shims" / "mpv.exe")
                                                     if (Path.home() / "scoop" / "shims" / "mpv.exe").is_file() else None)
        vlc = vlc or _win_app("VideoLAN", "VLC", "vlc.exe")
    if mpv:
        return [mpv, f"--start={t:.2f}", "--force-window=yes", path]
    if vlc:
        return [vlc, f"--start-time={t:.2f}", path]
    if not is_windows():
        for p in ("celluloid", "totem"):
            if shutil.which(p):
                return [p, path]
    return None


def open_with_default_app(path: str) -> bool:
    """Open a file or folder with the system default handler (no seeking)."""
    try:
        if REAL_OS == "windows":
            os.startfile(path)          # noqa: S606 - user's own file
            return True
        if shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             **detached_kw())
            return True
    except OSError:
        pass
    return False
