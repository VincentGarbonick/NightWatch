"""ffmpeg / ffprobe helpers."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path

from . import system

VIDEO_EXTS = {".ts", ".m2ts", ".mts", ".mp4", ".mkv", ".mov", ".avi", ".flv"}


class MediaError(RuntimeError):
    pass


def require_tools():
    missing = [t for t in ("ffmpeg", "ffprobe") if system.tool(t) is None]
    if missing:
        raise MediaError("Missing required tool(s): " + ", ".join(missing) + ". " + system.install_hint())


@dataclass
class ProbeInfo:
    path: str
    duration: float
    width: int
    height: int
    fps: float
    codec: str
    video_offset: float      # first video frame time relative to file start
    has_audio: bool
    pix_fmt: str = ""

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"


def _fraction(s: str | None) -> float:
    try:
        f = float(Fraction(s))
        return f if f > 0 else 0.0
    except Exception:
        return 0.0


def probe(path: str | os.PathLike) -> ProbeInfo:
    path = str(path)
    cmd = [system.ffprobe(), "-v", "error", "-show_entries",
           "format=duration,start_time:stream=codec_type,codec_name,width,height,pix_fmt,"
           "avg_frame_rate,r_frame_rate,start_time,duration",
           "-of", "json", path]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=120, **system.TEXT, **system.popen_kw())
    except subprocess.TimeoutExpired:
        raise MediaError(f"ffprobe timed out on {path}")
    except OSError as e:
        raise MediaError(f"Could not run ffprobe ({e}). {system.install_hint()}")
    if out.returncode != 0:
        raise MediaError(f"ffprobe failed on {path}: {out.stderr.strip()[:300]}")
    data = json.loads(out.stdout or "{}")
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    vids = [s for s in streams if s.get("codec_type") == "video"]
    if not vids:
        raise MediaError(f"No video stream in {path}")
    v = vids[0]
    fstart = float(fmt.get("start_time") or 0.0)
    vstart = float(v.get("start_time") or fstart)
    duration = float(fmt.get("duration") or v.get("duration") or 0.0)
    fps = _fraction(v.get("avg_frame_rate")) or _fraction(v.get("r_frame_rate")) or 15.0
    if fps > 120:   # bogus timebase-derived rates show up in some .ts files
        fps = _fraction(v.get("avg_frame_rate")) if _fraction(v.get("avg_frame_rate")) <= 120 else 15.0
    return ProbeInfo(
        path=path, duration=duration, width=int(v.get("width") or 0),
        height=int(v.get("height") or 0), fps=fps, codec=v.get("codec_name", "?"),
        video_offset=max(0.0, vstart - fstart),
        has_audio=any(s.get("codec_type") == "audio" for s in streams), pix_fmt=v.get("pix_fmt", ""))


# --------------------------------------------------------------------------- clock

_TS_PATTERNS = [
    # MediaMTX default: 2026-11-02_23-00-00-123456
    re.compile(r"(?P<Y>20\d\d)-(?P<m>\d\d)-(?P<d>\d\d)[_T ](?P<H>\d\d)[-:.](?P<M>\d\d)[-:.](?P<S>\d\d)"),
    # 20261102_230000 / 20261102T230000 / 20261102-230000
    re.compile(r"(?P<Y>20\d\d)(?P<m>\d\d)(?P<d>\d\d)[_T-]?(?P<H>\d\d)(?P<M>\d\d)(?P<S>\d\d)"),
    # 2026_11_02_23_00_00
    re.compile(r"(?P<Y>20\d\d)_(?P<m>\d\d)_(?P<d>\d\d)_(?P<H>\d\d)_(?P<M>\d\d)_(?P<S>\d\d)"),
]


def clock_from_filename(path: str | os.PathLike) -> datetime | None:
    name = Path(path).name
    for pat in _TS_PATTERNS:
        m = pat.search(name)
        if m:
            try:
                return datetime(*(int(m.group(k)) for k in "YmdHMS"))
            except ValueError:
                continue
    return None


def file_clock(path: str, info: ProbeInfo, source: str = "auto") -> datetime | None:
    """Wall-clock time of the start of the file, if it can be determined.

    source: 'auto' (filename), 'filename', 'mtime' (modified time minus duration), 'none'.
    """
    if source in ("auto", "filename"):
        return clock_from_filename(path)
    if source == "mtime":
        try:
            return datetime.fromtimestamp(os.path.getmtime(path)) - timedelta(seconds=info.duration)
        except OSError:
            return None
    return None


# --------------------------------------------------------------------------- frames

def grab_frame(path: str, t: float, width: int = 960, brighten: bool = True) -> bytes | None:
    """Return a PNG of the frame at t seconds (accurate seek), optionally brightened."""
    pre = max(0.0, t - 15.0)
    vf = f"scale='min({width},iw)':-2"
    if brighten:
        vf += ",eq=gamma=1.8"
    cmd = [system.ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error",
           "-ss", f"{pre:.3f}", "-i", path, "-ss", f"{t - pre:.3f}",
           "-frames:v", "1", "-vf", vf, "-f", "image2pipe", "-vcodec", "png", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=60, **system.popen_kw())
    except (subprocess.TimeoutExpired, OSError):
        return None
    return out.stdout if out.returncode == 0 and out.stdout else None


_font_lock = threading.Lock()
_font_cache: dict = {}


def _drawtext_ok(font: Path | None) -> bool:
    """Render one frame of text. Runs inside a temp dir with *relative* file names so
    Windows paths (C:\\...) never appear inside the filter string."""
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "t.txt").write_text("test %{pts:hms:1.0}", encoding="utf-8")
        opt = "drawtext=textfile=t.txt:fontcolor=white"
        if font is not None:
            try:
                shutil.copyfile(font, Path(td) / "font.ttf")
            except OSError:
                return False
            opt += ":fontfile=font.ttf"
        cmd = [system.ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
               "-i", "color=c=black:s=160x90:d=0.1", "-vf", opt, "-frames:v", "1", "-f", "null", "-"]
        try:
            return subprocess.run(cmd, capture_output=True, timeout=30, cwd=td,
                                  **system.popen_kw()).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False


def overlay_font() -> tuple[str, Path | None] | None:
    """How to draw the compilation overlay.

    ('default', None)  ffmpeg finds a font itself (fontconfig, typical on Linux)
    ('file', path)     copy this TTF next to the job and pass fontfile=font.ttf
    None               this ffmpeg can't draw text at all
    """
    key = (system.target(), system.ffmpeg())
    with _font_lock:
        if key in _font_cache:
            return _font_cache[key]
        files = system.font_files()
        # Windows ffmpeg builds rarely have a working fontconfig, so try real font files first.
        order = ([("file", f) for f in files] + [("default", None)] if system.is_windows()
                 else [("default", None)] + [("file", f) for f in files])
        res = None
        for kind, f in order:
            if _drawtext_ok(f):
                res = (kind, f)
                break
        _font_cache[key] = res
        return res


def drawtext_available() -> bool:
    return overlay_font() is not None


def fmt_hms(t: float, ms: bool = False) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    if ms:
        return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d}"
