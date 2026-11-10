"""Command-line interface."""
from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

from . import gpu, system
from .detector import Settings
from .media import MediaError, fmt_hms
from .pipeline import Callbacks, default_jobs, expand_inputs, run


def _region(s: str) -> list[float]:
    try:
        v = [float(x) for x in s.split(",")]
        assert len(v) == 4 and all(0 <= x <= 1 for x in v)
        return v
    except Exception:
        raise argparse.ArgumentTypeError("region must be x,y,w,h as fractions 0-1, e.g. 0,0,0.4,0.08")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="nightwatch", description="Find events in dark CCTV footage.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("gui", help="open the graphical interface (default)")
    sc = sub.add_parser("scan", help="analyse files/folders from the command line")
    sc.add_argument("inputs", nargs="+", help=".ts files and/or folders (searched recursively)")
    _platform_args(sc)
    sc.add_argument("-o", "--out", default=".", help="output folder (a timestamped run folder is created inside)")
    sc.add_argument("--compile", action="store_true", help="also cut events into one compilation video")
    sc.add_argument("--fast", action="store_true", help="stream-copy clips (no overlay, keyframe-aligned)")
    sc.add_argument("--keep-clips", action="store_true", help="keep individual event clips too")
    sc.add_argument("-s", "--sensitivity", type=int, help="1 (obvious only) .. 10 (very sensitive), default 6")
    sc.add_argument("--min-size", type=float, help="smallest object, %% of frame area (default 0.05)")
    sc.add_argument("--ignore", type=_region, action="append", default=None, metavar="x,y,w,h",
                    help="ignore a region (fractions of frame); repeatable")
    sc.add_argument("--pre", type=float, help="seconds before each event (default 2)")
    sc.add_argument("--post", type=float, help="seconds after each event (default 3)")
    sc.add_argument("--merge-gap", type=float, help="merge events closer than this many seconds (default 3)")
    sc.add_argument("--night-only", action="store_true", help="skip events in bright/daytime scenes")
    sc.add_argument("--no-overlay", action="store_true", help="don't burn file/time text into the compilation")
    sc.add_argument("--no-brighten", action="store_true", help="don't brighten the compilation")
    sc.add_argument("--no-auto-ignore", action="store_true", help="disable learning of constantly-changing areas")
    sc.add_argument("--clock", choices=["auto", "filename", "mtime", "none"], help="where wall-clock time comes from")
    sc.add_argument("-j", "--jobs", type=int, help=f"files analysed in parallel (default {default_jobs()})")
    sc.add_argument("--fps", type=float, help="analysis frame rate (default 5)")
    sc.add_argument("--config", help="load settings from a JSON file (e.g. saved by the GUI)")
    sc.add_argument("--debug-video", action="store_true", help="write a mask/heatmap debug video per file")
    ck = sub.add_parser("check", help="show which ffmpeg and GPU features will be used, then exit")
    _platform_args(ck)
    return ap


def _platform_args(p: argparse.ArgumentParser):
    g = p.add_argument_group("platform")
    g.add_argument("--platform", required=True, choices=system.PLATFORMS,
                   help=f"operating system you're running on (required; this one looks like {system.REAL_OS})")
    g.add_argument("--gpu", choices=gpu.VENDORS, default=None,
                   help="graphics card to use for decoding/encoding (default auto: try the GPU, fall back to CPU)")
    g.add_argument("--ffmpeg-dir", default=None, help="folder containing ffmpeg and ffprobe, if not on PATH")


def _check(a) -> int:
    for w in system.configure(a.platform, a.ffmpeg_dir or ""):
        print("WARNING: " + w)
    vendor = a.gpu or "auto"
    print(f"Testing ({gpu.VENDOR_LABELS[vendor]})...\n")
    rep = gpu.check(vendor)
    print("\n".join(rep.lines()))
    if rep.version is None:
        print("\nffmpeg was not found. " + system.install_hint())
        return 1
    return 0


def main(argv=None) -> int:
    ap = build_parser()
    args = list(sys.argv[1:] if argv is None else argv)
    # `nightwatch file1.ts folder/` (e.g. from the app-menu launcher) opens the GUI with those queued
    if args and args[0] not in ("scan", "gui") and not args[0].startswith("-") and Path(args[0]).exists():
        from .gui import main as gui_main
        return gui_main([sys.argv[0]] + args)
    a = ap.parse_args(args)
    if a.cmd in (None, "gui"):
        from .gui import main as gui_main
        return gui_main()
    for stream in (sys.stdout, sys.stderr):        # Windows consoles choke on non-ASCII file names
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    if a.cmd == "check":
        return _check(a)

    S = Settings(jobs=default_jobs(), platform=a.platform)
    if a.config:
        S = Settings.from_dict(json.loads(Path(a.config).read_text(encoding="utf-8")))
    S.platform = a.platform
    if a.gpu:
        S.gpu = a.gpu
    if a.ffmpeg_dir is not None:
        S.ffmpeg_dir = a.ffmpeg_dir
    for attr, val in [("sensitivity", a.sensitivity), ("min_object_pct", a.min_size),
                      ("pre_roll_s", a.pre), ("post_roll_s", a.post), ("merge_gap_s", a.merge_gap),
                      ("clock_source", a.clock), ("jobs", a.jobs), ("analysis_fps", a.fps)]:
        if val is not None:
            setattr(S, attr, val)
    if a.ignore:
        S.ignore_regions = a.ignore
    S.mode = "compile" if a.compile else "timestamps"
    S.fast_copy = a.fast or S.fast_copy
    S.keep_clips = a.keep_clips or S.keep_clips
    S.night_only = a.night_only or S.night_only
    if a.no_overlay:
        S.overlay = False
    if a.no_brighten:
        S.brighten_output = False
    if a.no_auto_ignore:
        S.auto_suppress = False

    files = expand_inputs(a.inputs)
    if not files:
        print("No video files found.", file=sys.stderr)
        return 2
    names = [Path(f).name for f in files]
    width = min(40, max(len(n) for n in names))
    prog = [0.0] * len(files)
    lock = threading.Lock()
    tty = sys.stderr.isatty()

    def show():
        if not tty:
            return
        done = sum(prog) / len(prog)
        sys.stderr.write(f"\r  analysing... {done * 100:5.1f}%  ")
        sys.stderr.flush()

    def fp(i, f):
        with lock:
            prog[i] = f
            show()

    def lg(m):
        with lock:
            if tty:
                sys.stderr.write("\r" + " " * 30 + "\r")
            print(m, file=sys.stderr)
            show()

    cb = Callbacks(file_progress=fp, log=lg, stage=lambda s: lg(f"-- {s}"),
                   compile_progress=lambda f: tty and sys.stderr.write(f"\r  cutting... {f * 100:5.1f}%  "))
    cancel = threading.Event()
    box: dict = {}

    def work():
        try:
            box["out"] = run(files, S, a.out, cb, cancel, debug_video=a.debug_video)
        except MediaError as e:
            box["err"] = e

    # Run in a thread and poll: Ctrl+C can't interrupt a blocking wait on Windows.
    t = threading.Thread(target=work, daemon=True)
    t.start()
    try:
        while t.is_alive():
            t.join(0.3)
    except KeyboardInterrupt:
        cancel.set()
        print("\ninterrupted, stopping ffmpeg...", file=sys.stderr)
        t.join(10)
        return 130
    if "err" in box:
        print(f"error: {box['err']}", file=sys.stderr)
        return 1
    out = box["out"]
    if tty:
        sys.stderr.write("\r" + " " * 30 + "\r")
    for r in out.results:
        print(f"{Path(r.path).name:<{width}}  {'ERROR ' + r.error if r.error else str(len(r.events)) + ' event(s)'}")
        for ev in r.events:
            when = ev.clock_start.strftime("%H:%M:%S") if ev.clock_start else ""
            print(f"    {when:>8}  +{fmt_hms(ev.start)}  {ev.duration:5.1f}s  {ev.kind}")
    print(f"\nResults: {out.run_dir}")
    if out.compilation:
        print(f"Compilation: {out.compilation}")
    return 0
