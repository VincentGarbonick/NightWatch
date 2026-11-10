#!/usr/bin/env python3
"""Generate synthetic dark CCTV .ts footage with known events, for testing.

Simulates a cheap webcam at night: very dark, noisy colour, low-bitrate H.264
with keyframe pulsing, an OSD clock, a swaying branch and a blinking LED
(chronic distractors), plus a set of real events with ground-truth times.
"""
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

FPS = 15


def add_blob(img, cx, cy, sx, sy, amp, ch=None):
    """Add an anisotropic gaussian blob to img (float32 HxWx3) in place."""
    h, w = img.shape[:2]
    x0, x1 = int(max(0, cx - 3 * sx)), int(min(w, cx + 3 * sx + 1))
    y0, y1 = int(max(0, cy - 3 * sy)), int(min(h, cy + 3 * sy + 1))
    if x0 >= x1 or y0 >= y1:
        return
    yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
    g = amp * np.exp(-(((xx - cx) / sx) ** 2 + ((yy - cy) / sy) ** 2) / 2)
    if ch is None:
        img[y0:y1, x0:x1, :] += g[..., None]
    else:
        img[y0:y1, x0:x1, ch] += g


def make_background(w, h, rng):
    """A dark suburban street: sky, house silhouette, yard, road, kerb."""
    s = w / 640.0
    bg = np.zeros((h, w), np.float32)
    yy = np.arange(h, dtype=np.float32)[:, None]
    bg += 6 + 6 * (1 - yy / h)                       # faint sky glow
    road_top, road_bot = int(230 * s), int(290 * s)
    bg[road_top:road_bot, :] = 14                    # road a bit lighter
    bg[road_bot:road_bot + int(4 * s), :] = 22       # kerb line
    bg[int(150 * s):road_top, :] += 4                # yard
    # house silhouette on the right with a dark window
    cv2.rectangle(bg, (int(470 * s), int(60 * s)), (int(620 * s), int(200 * s)), 18, -1)
    cv2.rectangle(bg, (int(500 * s), int(100 * s)), (int(530 * s), int(140 * s)), 9, -1)
    bg = cv2.GaussianBlur(bg, (0, 0), 1.2 * s)
    bg += rng.normal(0, 1.5, bg.shape).astype(np.float32)   # fixed texture
    col = np.stack([bg * 1.05, bg * 1.0, bg * 0.9], axis=-1)  # slight colour cast
    return col


def car(img, t, t0, t1, w, s, direction=+1, y=None):
    """Car with headlights/beam cone moving across the road."""
    if not (t0 <= t <= t1):
        return
    p = (t - t0) / (t1 - t0)
    margin = 120 * s
    x = -margin + p * (w + 2 * margin) if direction > 0 else w + margin - p * (w + 2 * margin)
    y = y if y is not None else 258 * s
    # dark body
    bx0 = int(x - direction * 60 * s)
    cv2.rectangle(img, (min(bx0, int(x)), int(y - 12 * s)), (max(bx0, int(x)), int(y + 8 * s)),
                  (26, 26, 26), -1)
    # beam cone on the road ahead + glare
    add_blob(img, x + direction * 70 * s, y + 6 * s, 70 * s, 14 * s, 55)
    add_blob(img, x, y, 22 * s, 14 * s, 70)
    # headlights
    for dy in (-5 * s, 5 * s):
        add_blob(img, x, y + dy, 3 * s, 2.5 * s, 240)
    # red tail lights at the back
    add_blob(img, bx0, y, 2.5 * s, 2.5 * s, 160, ch=2)


def render(path, w, h, dur, start_dt, events, seed, noise=1.0, rate=1.0, flicker=0.0):
    rng = np.random.default_rng(seed)
    s = w / 640.0
    bg = make_background(w, h, rng)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", str(FPS), "-i", "-",
           "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{int(450 * s * s * rate)}k",
           "-maxrate", f"{int(600 * s * s * rate)}k", "-bufsize", f"{int(1200 * s * s * rate)}k",
           "-g", "30", "-pix_fmt", "yuv420p", "-f", "mpegts", str(path)]
    ff = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    n = int(dur * FPS)
    for i in range(n):
        t = i / FPS
        img = bg.copy()
        # chronic distractor 1: branch swaying in the top-left corner
        sway = 4 * s * np.sin(2 * np.pi * 0.45 * t) + 2 * s * np.sin(2 * np.pi * 1.3 * t)
        for k in range(5):
            add_blob(img, (40 + 18 * k) * s + sway * (k / 4), (70 + 6 * k) * s, 9 * s, 3 * s, 10)
        # chronic distractor 2: blinking LED (1 Hz)
        if (t % 1.0) < 0.5:
            add_blob(img, 60 * s, 320 * s, 1.5 * s, 1.5 * s, 150, ch=1)
        for ev in events:
            kind, t0, t1 = ev["kind"], ev["t0"], ev["t1"]
            if kind == "car_lr":
                car(img, t, t0, t1, w, s, +1)
            elif kind == "car_rl":
                car(img, t, t0, t1, w, s, -1)
            elif kind == "walker" and t0 <= t <= t1:
                p = (t - t0) / (t1 - t0)
                x = (150 + 180 * p) * s
                y = (200 + 1.5 * np.sin(2 * np.pi * 1.6 * t)) * s
                add_blob(img, x, y, 5 * s, 14 * s, 9)
            elif kind == "porch_light" and t0 <= t <= t1:
                add_blob(img, 545 * s, 150 * s, 85 * s, 60 * s, 65)
                add_blob(img, 545 * s, 92 * s, 3 * s, 3 * s, 220)
            elif kind == "moth" and t0 <= t <= t1:
                mx = (300 + 60 * np.sin(17 * t) + 30 * np.cos(29 * t)) * s
                my = (150 + 40 * np.cos(13 * t)) * s
                add_blob(img, mx, my, 22 * s, 18 * s, 110)
        # sensor noise (luma + chroma)
        img += rng.normal(0, 3.5 * noise, (h, w, 1)).astype(np.float32)
        img += rng.normal(0, 2.0 * noise, (h, w, 3)).astype(np.float32)
        if flicker:
            gain = getattr(render, "_gain", 0.0) * 0.9 + rng.normal(0, flicker)
            render._gain = gain
            img += gain
        frame = np.clip(img, 0, 255).astype(np.uint8)
        # OSD clock
        stamp = (start_dt + timedelta(seconds=int(t))).strftime("%Y-%m-%d %H:%M:%S")
        org = (int(10 * s), int(25 * s))
        cv2.putText(frame, stamp, org, cv2.FONT_HERSHEY_SIMPLEX, 0.6 * s, (0, 0, 0), max(1, int(3 * s)), cv2.LINE_AA)
        cv2.putText(frame, stamp, org, cv2.FONT_HERSHEY_SIMPLEX, 0.6 * s, (235, 235, 235), max(1, int(1 * s)), cv2.LINE_AA)
        ff.stdin.write(frame.tobytes())
    ff.stdin.close()
    ff.wait()


HARSH = [dict(name="20261103_013000_harsh.ts", w=640, h=360, dur=90,
              start=datetime(2026, 11, 3, 1, 30, 0), seed=3, noise=2.0, rate=0.5, flicker=0.8,
              events=[dict(kind="car_lr", t0=30, t1=33), dict(kind="walker", t0=50, t1=65)])]


def main(outdir, which="normal"):
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    specs = [
        dict(name="2026-11-02_23-00-00-000000.ts", w=640, h=360, dur=180,
             start=datetime(2026, 11, 2, 23, 0, 0), seed=1,
             events=[dict(kind="car_lr", t0=20, t1=24),
                     dict(kind="walker", t0=55, t1=70),
                     dict(kind="porch_light", t0=95, t1=125),
                     dict(kind="car_rl", t0=140, t1=142.5),
                     dict(kind="moth", t0=160, t1=161.2)]),
        dict(name="2026-11-02_23-03-00-000000.ts", w=960, h=540, dur=120,
             start=datetime(2026, 11, 2, 23, 3, 0), seed=2,
             events=[dict(kind="car_lr", t0=80, t1=83.5)]),
    ]
    if which == "harsh":
        specs = HARSH
    truth = {}
    for sp in specs:
        print("rendering", sp["name"], file=sys.stderr)
        render(out / sp["name"], sp["w"], sp["h"], sp["dur"], sp["start"], sp["events"], sp["seed"],
               sp.get("noise", 1.0), sp.get("rate", 1.0), sp.get("flicker", 0.0))
        truth[sp["name"]] = sp["events"]
    (out / "ground_truth.json").write_text(json.dumps(truth, indent=2))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "testdata", sys.argv[2] if len(sys.argv) > 2 else "normal")
