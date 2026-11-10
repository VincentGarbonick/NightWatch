"""ffmpeg / ffprobe helpers."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

VIDEO_EXTS = {".ts", ".m2ts", ".mts", ".mp4", ".mkv", ".mov", ".avi", ".flv"}


class MediaError(RuntimeError):
    pass


def require_tools():
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        raise MediaError("Missing required tool(s): " + ", ".join(missing) +
                         ". Install with: sudo pacman -S ffmpeg")


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
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "format=duration,start_time:stream=codec_type,codec_name,width,height,"
           "avg_frame_rate,r_frame_rate,start_time,duration",
           "-of", "json", path]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise MediaError(f"ffprobe timed out on {path}")
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
        has_audio=any(s.get("codec_type") == "audio" for s in streams))


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
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-ss", f"{pre:.3f}", "-i", path, "-ss", f"{t - pre:.3f}",
           "-frames:v", "1", "-vf", vf, "-f", "image2pipe", "-vcodec", "png", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        return None
    return out.stdout if out.returncode == 0 and out.stdout else None


@lru_cache(maxsize=1)
def drawtext_available() -> bool:
    """True if this ffmpeg can render text (needs freetype + a font)."""
    with tempfile.TemporaryDirectory() as td:
        tf = Path(td) / "t.txt"
        tf.write_text("test %{pts:hms:1.0}")
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
               "-i", "color=c=black:s=160x90:d=0.1", "-vf", f"drawtext=textfile={tf}:fontcolor=white",
               "-frames:v", "1", "-f", "null", "-"]
        try:
            return subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0
        except Exception:
            return False


def which_player() -> list[str] | None:
    for p in ("mpv", "vlc", "celluloid"):
        if shutil.which(p):
            return [p]
    if shutil.which("xdg-open"):
        return ["xdg-open"]
    return None


def player_command(path: str, t: float = 0.0) -> list[str] | None:
    p = which_player()
    if not p:
        return None
    if p[0] == "mpv":
        return ["mpv", f"--start={max(0.0, t):.2f}", "--force-window=yes", path]
    if p[0] == "vlc":
        return ["vlc", f"--start-time={max(0.0, t):.2f}", path]
    return p + [path]


def fmt_hms(t: float, ms: bool = False) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    if ms:
        return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d}"
