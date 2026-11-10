"""Low-light motion / light-event detector.

Pipeline per analysed frame (default 5 fps, 320 px wide grey):

1. Dark boost: gamma 0.5 lifts the shadows so dim changes register.
2. Slow background (EMA, ~4 s) with a *per-pixel* noise estimate, so noisy
   areas need a bigger change and clean areas can trigger on small ones.
3. Chronic-motion suppression: pixels that are "changed" a large fraction of
   the time (OSD clock digits, blinking LEDs, swaying branches, flicker) are
   learnt and ignored automatically.
4. Blobs are tracked across frames. A blob only becomes an event once it has
   either *moved* a meaningful distance (cars, people, animals) or it is a
   large lighting change (porch light, headlights sweeping a wall). This kills
   one-off static changes such as clock digits rolling over and compression pops.
5. Active frames are merged into events (with a gap tolerance), and each event
   gets a brightened thumbnail of its strongest frame with boxes drawn on it.
"""
from __future__ import annotations

import math
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from . import gpu, system
from .media import MediaError, ProbeInfo, file_clock, probe

# --------------------------------------------------------------------------- settings


@dataclass
class Settings:
    # detection
    sensitivity: int = 6                 # 1 (only obvious) .. 10 (very twitchy)
    min_object_pct: float = 0.05         # smallest blob, % of frame area
    analysis_fps: float = 5.0
    analysis_width: int = 320
    dark_boost: bool = True
    auto_suppress: bool = True           # learn & ignore constantly-changing pixels
    night_only: bool = False             # drop events in bright (daytime) scenes
    night_threshold: int = 70            # mean luma (0-255) above which a scene is "bright"
    ignore_regions: list = field(default_factory=list)   # [[x, y, w, h], ...] normalised 0-1
    min_event_s: float = 0.8
    merge_gap_s: float = 3.0
    clock_source: str = "auto"           # auto | filename | mtime | none
    # platform / hardware
    platform: str = ""                   # windows | linux ("" = not chosen yet)
    gpu: str = "auto"                    # auto | nvidia | amd | intel | none
    ffmpeg_dir: str = ""                 # optional folder containing ffmpeg/ffprobe
    show_setup: bool = True              # show the platform/GPU window when the GUI starts
    jobs: int = 2                        # files analysed in parallel
    # output
    mode: str = "timestamps"             # timestamps | compile
    pre_roll_s: float = 2.0
    post_roll_s: float = 3.0
    overlay: bool = True                 # burn file name + clock into compilation
    brighten_output: bool = True
    fast_copy: bool = False              # stream-copy (keyframe-aligned) instead of re-encode
    keep_clips: bool = False
    crf: int = 23

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        s = cls()
        for k, v in (d or {}).items():
            if hasattr(s, k):
                default = getattr(s, k)
                try:
                    if isinstance(default, bool):
                        v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes")
                    elif isinstance(default, int):
                        v = int(v)
                    elif isinstance(default, float):
                        v = float(v)
                    elif isinstance(default, list):
                        v = [list(map(float, r)) for r in v]
                except (TypeError, ValueError):
                    continue
                setattr(s, k, v)
        return s

    # derived detector parameters
    @property
    def k_noise(self) -> float:
        return 7.0 - 0.5 * max(1, min(10, self.sensitivity))      # 6.5 .. 2.0

    @property
    def floor(self) -> float:
        return 12.0 - 1.0 * max(1, min(10, self.sensitivity))     # 11 .. 2 (boosted grey levels)


# --------------------------------------------------------------------------- results


@dataclass
class Event:
    file: str
    index: int
    start: float              # seconds from start of file (unpadded activity)
    end: float
    peak_t: float
    peak_area_pct: float
    kind: str
    kinds: list
    clock_start: datetime | None = None
    thumb_jpeg: bytes | None = field(default=None, repr=False)
    thumb_path: str | None = None
    comp_t: float | None = None     # position in the compilation video, if one was made

    @property
    def duration(self) -> float:
        return self.end - self.start

    def clock_at(self, t: float) -> datetime | None:
        if self.clock_start is None:
            return None
        return self.clock_start + timedelta(seconds=t - self.start)


@dataclass
class FileResult:
    path: str
    info: ProbeInfo | None
    clock: datetime | None
    events: list
    error: str | None = None
    elapsed: float = 0.0
    frames: int = 0
    dark_fraction: float = 0.0
    suppressed_pct: float = 0.0
    decoder: str = "CPU"


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- tracking


class _Track:
    __slots__ = ("id", "birth", "last", "ox", "oy", "cx", "cy", "bbox", "age", "max_disp",
                 "big_frames", "confirmed", "bright", "max_area", "dsign", "history",
                 "first_active", "sudden")

    def __init__(self, tid, idx, cx, cy, bbox, area, bright, dsign, area_pct):
        self.id = tid
        self.birth = idx
        self.last = idx
        self.ox, self.oy = cx, cy
        self.cx, self.cy = cx, cy
        self.bbox = bbox
        self.age = 1
        self.max_disp = 0.0
        self.big_frames = 0
        self.confirmed = False
        self.bright = bright
        self.max_area = area
        self.dsign = dsign
        self.history = [(idx, area_pct)]
        self.first_active = None
        self.sudden = False       # became large within its first two frames (light switched)


def _overlap(a, b, pad):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax - pad > bx + bw or bx > ax + aw + pad or ay - pad > by + bh or by > ay + ah + pad)


# --------------------------------------------------------------------------- analysis


def _gamma_lut(g: float) -> np.ndarray:
    return np.array([((i / 255.0) ** g) * 255.0 for i in range(256)], np.uint8)


_BOOST = _gamma_lut(0.5)
_THUMB_LUT = _gamma_lut(0.45)


def _even(x: float) -> int:
    return max(2, int(round(x / 2.0)) * 2)


def analyze(path: str, settings: Settings,
            progress: Callable[[float], None] | None = None,
            cancel: threading.Event | None = None,
            debug_video: str | None = None,
            log: Callable[[str], None] | None = None) -> FileResult:
    """Analyse one video file and return its events."""
    t_start = time.time()
    path = str(path)
    try:
        info = probe(path)
    except MediaError as e:
        return FileResult(path, None, None, [], error=str(e))
    clock = file_clock(path, info, settings.clock_source)
    if info.width <= 0 or info.height <= 0:
        return FileResult(path, info, clock, [], error="Could not read video dimensions")

    S = settings
    afps = max(1.0, min(float(S.analysis_fps), info.fps or 30.0))
    tw = _even(min(max(640, _even(min(S.analysis_width, info.width)) * 2), info.width))
    th = _even(tw * info.height / info.width)
    chain = gpu.choose_decoder(path, info, S.gpu, afps, tw, th, log)
    res, rc = _analyze_with(path, S, info, clock, chain, t_start, progress, cancel, debug_video)
    if chain.is_gpu and rc not in (0, None) and (res.error or res.frames < 0.9 * info.duration * afps):
        if log:
            log(f"{Path(path).name}: GPU decode ({chain.name}) stopped early; re-running on the CPU.")
        res, rc = _analyze_with(path, S, info, clock, gpu.CPU_DECODE, t_start, progress, cancel, debug_video)
    return res


def _analyze_with(path, S, info, clock, chain, t_start, progress, cancel, debug_video):
    afps = max(1.0, min(float(S.analysis_fps), info.fps or 30.0))
    aw = _even(min(S.analysis_width, info.width))
    ah = _even(aw * info.height / info.width)
    tw = _even(min(max(640, aw * 2), info.width))
    th = _even(tw * info.height / info.width)
    frame_bytes = tw * th * 3
    expected = max(1, int(info.duration * afps))

    cmd = [system.ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", *chain.input_args,
           "-fflags", "+discardcorrupt", "-err_detect", "ignore_err", "-i", path,
           "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", chain.vf(afps, tw, th),
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    errf = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, bufsize=frame_bytes * 2,
                                **system.popen_kw())
    except OSError as e:
        errf.close()
        return FileResult(path, info, clock, [], error=f"Could not run ffmpeg: {e}"), None

    dbg = None
    if debug_video:
        dbg = subprocess.Popen(
            [system.ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
             "-pix_fmt", "bgr24", "-s", f"{aw * 3}x{ah}", "-r", f"{afps:g}", "-i", "-",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p", debug_video],
            stdin=subprocess.PIPE, **system.popen_kw())

    # ---- parameters in analysis pixels / frames
    area_px = aw * ah
    diag = math.hypot(aw, ah)
    min_area = max(4, int(area_px * S.min_object_pct / 100.0))
    big_area = int(area_px * 0.015)
    disp_min = 0.015 * diag
    confirm_frames = max(2, int(0.5 * afps + 0.5))
    lost_frames = max(2, int(0.8 * afps + 0.5))
    pad = max(4, int(0.03 * aw))
    a_bg = 1.0 / (4.0 * afps)            # background time constant ~4 s
    a_noise = 1.0 / (10.0 * afps)
    a_chronic = 1.0 / (120.0 * afps)     # chronic-activity window ~2 min
    warm_frames = int(2.0 * afps)
    suppress_after = int(5 * afps)
    k, floor = S.k_noise, S.floor
    k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    ignore = np.zeros((ah, aw), np.uint8)
    for r in S.ignore_regions or []:
        x, y, w, h = r
        cv2.rectangle(ignore, (int(x * aw), int(y * ah)),
                      (int(math.ceil((x + w) * aw)), int(math.ceil((y + h) * ah))), 1, -1)
    keep = (ignore == 0)

    bg = noise = chronic = None
    active: list[bool] = []
    scores: list[float] = []
    brightness: list[float] = []
    gshift: list[float] = []
    global_runs: list[list] = []        # [start_idx, end_idx, sign]
    tracks: list[_Track] = []
    done_tracks: list[_Track] = []
    next_id = 0
    buckets: dict[int, tuple[float, bytes]] = {}   # 1 s bucket -> (score, jpeg)
    sup_acc = 0.0
    idx = 0
    last_prog = 0.0

    def confirm(tr: _Track, now: int):
        tr.confirmed = True
        tr.first_active = tr.birth
        for (i, a) in tr.history:
            if i < len(active):
                active[i] = True
                scores[i] += a

    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            bgr = np.frombuffer(buf, np.uint8).reshape(th, tw, 3)
            small = cv2.resize(bgr, (aw, ah), interpolation=cv2.INTER_AREA)
            raw = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            brightness.append(float(raw.mean()))
            g = cv2.LUT(raw, _BOOST) if S.dark_boost else raw
            f = cv2.GaussianBlur(g.astype(np.float32), (0, 0), 1.0)

            active.append(False)
            scores.append(0.0)

            if bg is None:
                bg = f.copy()
                noise = np.full_like(f, 1.5)
                chronic = np.zeros_like(f)
            d = f - bg
            # Global gain compensation: cheap webcams "breathe" (auto-exposure, keyframe
            # pops). Remove the frame-wide median shift; flag big jumps as global light changes.
            shift = float(np.median(d[::3, ::3]))
            d -= shift
            gshift.append(shift)
            ad = np.abs(d)
            thr = k * noise + floor
            fg_raw = ad > thr

            # learn statistics
            if idx < warm_frames:
                noise += (ad - noise) / (idx + 1)
                np.maximum(noise, 0.5, out=noise)
            else:
                upd = ~fg_raw
                noise[upd] += a_noise * (np.minimum(ad[upd], thr[upd]) - noise[upd])
                np.maximum(noise, 0.5, out=noise)
            bg += a_bg * (f - bg)
            ac = max(1.0 / (idx + 1), a_chronic)
            chronic += ac * (fg_raw.astype(np.float32) - chronic)

            fg = (fg_raw & keep).astype(np.uint8)
            if S.auto_suppress and idx >= suppress_after:
                sup = cv2.dilate((chronic > 0.3).astype(np.uint8), k_open)
                fg[sup > 0] = 0
                sup_acc += float(sup.mean())
            fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, k_open)
            fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, k_close)

            blobs = []
            if idx >= warm_frames:
                n, labels, stats, cents = cv2.connectedComponentsWithStats(fg, connectivity=8)
                if n > 1:
                    bright_cnt = np.bincount(labels[(raw >= 200) & (fg > 0)], minlength=n)
                    dsum = np.bincount(labels.ravel(), weights=d.ravel(), minlength=n)
                    for j in range(1, n):
                        area = int(stats[j, cv2.CC_STAT_AREA])
                        if area < min_area:
                            continue
                        bb = tuple(int(v) for v in stats[j, :4])
                        blobs.append(dict(cx=float(cents[j][0]), cy=float(cents[j][1]), bbox=bb,
                                          area=area, bright=bright_cnt[j] > 0,
                                          dsign=1 if dsum[j] >= 0 else -1))

            # ---- associate blobs with tracks
            assigned: dict[int, list] = {}
            new_blobs = []
            for b in blobs:
                best, best_d = None, 1e9
                for ti, tr in enumerate(tracks):
                    if _overlap(tr.bbox, b["bbox"], pad):
                        dd = math.hypot(tr.cx - b["cx"], tr.cy - b["cy"])
                        if dd < best_d:
                            best, best_d = ti, dd
                if best is None:
                    new_blobs.append(b)
                else:
                    assigned.setdefault(best, []).append(b)

            frame_conf_area = 0
            frame_cand_area = 0
            boxes = []
            for ti, bl in assigned.items():
                tr = tracks[ti]
                tot = sum(b["area"] for b in bl)
                tr.cx = sum(b["cx"] * b["area"] for b in bl) / tot
                tr.cy = sum(b["cy"] * b["area"] for b in bl) / tot
                xs = [b["bbox"][0] for b in bl] + [b["bbox"][0] + b["bbox"][2] for b in bl]
                ys = [b["bbox"][1] for b in bl] + [b["bbox"][1] + b["bbox"][3] for b in bl]
                tr.bbox = (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
                tr.last = idx
                tr.age += 1
                tr.max_disp = max(tr.max_disp, math.hypot(tr.cx - tr.ox, tr.cy - tr.oy))
                tr.max_area = max(tr.max_area, tot)
                tr.bright = tr.bright or any(b["bright"] for b in bl)
                if tot >= big_area:
                    tr.big_frames += 1
                    if tr.age <= 2:
                        tr.sudden = True
                    tr.dsign = 1 if sum(b["dsign"] * b["area"] for b in bl) >= 0 else -1
                tr.history.append((idx, tot / area_px * 100.0))
                if len(tr.history) > 600:
                    tr.history = tr.history[-600:]
                if not tr.confirmed and (
                        (tr.age >= confirm_frames and tr.max_disp >= disp_min) or tr.big_frames >= 2):
                    confirm(tr, idx)
                elif tr.confirmed:
                    active[idx] = True
                    scores[idx] += tot / area_px * 100.0
                if tr.confirmed:
                    frame_conf_area += tot
                else:
                    frame_cand_area += tot
                boxes.append((tr.bbox, tr.confirmed))
            for b in new_blobs:
                tr = _Track(next_id, idx, b["cx"], b["cy"], b["bbox"], b["area"], b["bright"], b["dsign"],
                            b["area"] / area_px * 100.0)
                if b["area"] >= big_area:
                    tr.big_frames = 1
                    tr.sudden = True
                next_id += 1
                tracks.append(tr)
                frame_cand_area += b["area"]
                boxes.append((b["bbox"], False))

            # whole-frame light change (IR switch, floodlight, big auto-exposure step)
            if idx >= warm_frames and abs(shift) > max(10.0, 2.0 * floor):
                active[idx] = True
                scores[idx] += 100.0 * min(1.0, abs(shift) / 50.0)
                sign = 1 if shift > 0 else -1
                if global_runs and global_runs[-1][1] >= idx - 1 and global_runs[-1][2] == sign:
                    global_runs[-1][1] = idx
                else:
                    global_runs.append([idx, idx, sign])

            # retire lost tracks
            alive = []
            for tr in tracks:
                if idx - tr.last > lost_frames:
                    if tr.confirmed:
                        done_tracks.append(tr)
                else:
                    alive.append(tr)
            tracks = alive

            # ---- thumbnail candidate for this 1-second bucket
            score = frame_conf_area + 0.25 * frame_cand_area
            if score > 0:
                bkt = int(idx / afps)
                prev = buckets.get(bkt)
                if prev is None or score > prev[0]:
                    buckets[bkt] = (score, _thumb(bgr, boxes, aw, ah))
            if idx % int(30 * afps) == 0 and buckets:
                _prune_buckets(buckets, active, idx, afps, S.merge_gap_s)

            if dbg is not None:
                _debug_frame(dbg, small, fg, chronic, boxes, S.auto_suppress and idx >= suppress_after)

            idx += 1
            if progress is not None:
                p = min(0.999, idx / expected)
                if p - last_prog >= 0.005:
                    last_prog = p
                    progress(p)
    except Cancelled:
        proc.kill()
        raise
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait()
        if dbg is not None:
            dbg.stdin.close()
            dbg.wait()

    rc = proc.returncode
    if idx == 0:
        errf.seek(0)
        msg = errf.read().decode(errors="replace").strip()[:400]
        errf.close()
        return FileResult(path, info, clock, [], error="No frames decoded" + (f": {msg}" if msg else ""),
                          decoder=chain.name), rc
    errf.close()

    done_tracks.extend(t for t in tracks if t.confirmed)
    events = _build_events(path, info, clock, S, afps, active, scores, brightness, buckets, done_tracks, diag,
                           global_runs)
    if progress is not None:
        progress(1.0)
    br = np.asarray(brightness)
    return FileResult(path, info, clock, events, elapsed=time.time() - t_start, frames=idx,
                      dark_fraction=float((br < S.night_threshold).mean()),
                      suppressed_pct=100.0 * sup_acc / max(1, idx - suppress_after), decoder=chain.name), rc


def _thumb(bgr: np.ndarray, boxes, aw: int, ah: int) -> bytes:
    img = cv2.LUT(bgr, _THUMB_LUT)
    sx = bgr.shape[1] / aw
    sy = bgr.shape[0] / ah
    for (x, y, w, h), conf in boxes:
        col = (60, 220, 255) if conf else (150, 150, 150)
        cv2.rectangle(img, (int(x * sx) - 2, int(y * sy) - 2), (int((x + w) * sx) + 2, int((y + h) * sy) + 2),
                      col, 2 if conf else 1)
    ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return enc.tobytes() if ok else b""


def _prune_buckets(buckets, active, idx, afps, gap):
    """Drop thumbnail candidates that are far from any confirmed activity."""
    horizon = idx - int(40 * afps)
    for b in list(buckets):
        lo = int(b * afps)
        if lo >= horizon:
            continue
        a0 = max(0, lo - int((gap + 2) * afps))
        a1 = min(len(active), lo + int((gap + 3) * afps))
        if not any(active[a0:a1]):
            del buckets[b]


def _debug_frame(proc, small, fg, chronic, boxes, sup_on):
    a = cv2.LUT(small, _THUMB_LUT)
    for (x, y, w, h), conf in boxes:
        cv2.rectangle(a, (x, y), (x + w, y + h), (60, 220, 255) if conf else (140, 140, 140), 1)
    m = cv2.cvtColor(fg * 255, cv2.COLOR_GRAY2BGR)
    c = np.clip(chronic * 255, 0, 255).astype(np.uint8)
    c = cv2.applyColorMap(c, cv2.COLORMAP_INFERNO)
    if sup_on:
        c[chronic > 0.3] = (255, 0, 255)
    try:
        proc.stdin.write(np.hstack([a, m, c]).tobytes())
    except BrokenPipeError:
        pass


_KIND_PRIORITY = ["headlights", "motion", "bright light", "light on", "light off"]


def _track_kind(tr: _Track, afps: float, diag: float) -> str:
    secs = max(1e-6, tr.age / afps)
    speed = tr.max_disp / diag / secs            # fraction of diagonal per second
    if tr.sudden and tr.big_frames >= 2 and tr.max_disp < 0.12 * diag:
        return "light on" if tr.dsign > 0 else "light off"
    if tr.bright:
        return "headlights" if speed >= 0.06 else "bright light"
    return "motion"


def _build_events(path, info, clock, S, afps, active, scores, brightness, buckets, tracks, diag, global_runs=()):
    n = len(active)
    if n == 0:
        return []
    act = np.asarray(active, bool)
    runs = []
    i = 0
    while i < n:
        if act[i]:
            j = i
            while j + 1 < n and act[j + 1]:
                j += 1
            runs.append([i, j])
            i = j + 1
        else:
            i += 1
    gap = int(S.merge_gap_s * afps)
    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][1] - 1 <= gap:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    min_len = S.min_event_s * afps
    sc =np.asarray(scores)
    br = np.asarray(brightness)
    events = []
    off = info.video_offset
    for (a, b) in merged:
        if (b - a + 1) < min_len:
            continue
        if S.night_only:
            pre = br[max(0, a - int(2 * afps)):a + 1]
            if pre.size and float(np.median(pre)) > S.night_threshold:
                continue
        peak = a + int(np.argmax(sc[a:b + 1]))
        kinds = set()
        for tr in tracks:
            if tr.first_active is not None and tr.first_active <= b and tr.last >= a:
                kinds.add(_track_kind(tr, afps, diag))
        for g0, g1, sign in global_runs:
            if g0 <= b and g1 >= a:
                kinds.add("light on" if sign > 0 else "light off")
        kinds = sorted(kinds, key=lambda k: _KIND_PRIORITY.index(k) if k in _KIND_PRIORITY else 99)
        start = off + a / afps
        end = off + (b + 1) / afps
        thumb = None
        best = -1.0
        for bkt in range(int(a / afps), int(b / afps) + 1):
            v = buckets.get(bkt)
            if v and v[0] > best:
                best, thumb = v
        ev = Event(file=path, index=len(events) + 1, start=start, end=end, peak_t=off + peak / afps,
                   peak_area_pct=float(sc[peak]), kind=kinds[0] if kinds else "motion", kinds=kinds,
                   clock_start=(clock + timedelta(seconds=start)) if clock else None, thumb_jpeg=thumb)
        events.append(ev)
    return events
