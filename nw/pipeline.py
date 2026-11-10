"""Batch pipeline shared by the CLI and the GUI."""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .detector import Cancelled, FileResult, Settings, analyze
from .export import build_compilation, new_run_dir, write_reports
from .media import VIDEO_EXTS, require_tools


def expand_inputs(paths) -> list[str]:
    """Files are kept as given; folders are searched recursively for video files."""
    out, seen = [], set()
    for p in paths:
        p = Path(p).expanduser()
        if p.is_dir():
            cands = sorted(q for q in p.rglob("*") if q.is_file() and q.suffix.lower() in VIDEO_EXTS)
        elif p.is_file():
            cands = [p]
        else:
            continue
        for q in cands:
            k = str(q.resolve())
            if k not in seen:
                seen.add(k)
                out.append(str(q))
    return out


@dataclass
class Callbacks:
    file_progress: Callable[[int, float], None] = lambda i, f: None
    file_done: Callable[[int, FileResult], None] = lambda i, r: None
    stage: Callable[[str], None] = lambda s: None
    log: Callable[[str], None] = lambda s: None
    compile_progress: Callable[[float], None] = lambda f: None


@dataclass
class RunOutput:
    run_dir: Path
    results: list = field(default_factory=list)
    reports: dict = field(default_factory=dict)
    compilation: Path | None = None
    cancelled: bool = False


def run(files: list[str], S: Settings, outdir: str, cb: Callbacks | None = None,
        cancel: threading.Event | None = None, debug_video: bool = False) -> RunOutput:
    require_tools()
    cb = cb or Callbacks()
    cancel = cancel or threading.Event()
    run_dir = new_run_dir(outdir)
    out = RunOutput(run_dir)
    results: list[FileResult | None] = [None] * len(files)
    cb.stage(f"Analysing {len(files)} file(s)")
    cb.log(f"Output folder: {run_dir}")

    def job(i: int, path: str) -> tuple[int, FileResult]:
        dbg = str(run_dir / f"debug_{Path(path).stem}.mp4") if debug_video else None
        r = analyze(path, S, progress=lambda f: cb.file_progress(i, f), cancel=cancel, debug_video=dbg)
        return i, r

    try:
        with ThreadPoolExecutor(max_workers=max(1, int(S.jobs))) as ex:
            futs = [ex.submit(job, i, p) for i, p in enumerate(files)]
            for fut in as_completed(futs):
                try:
                    i, r = fut.result()
                except Cancelled:
                    raise
                results[i] = r
                if r.error:
                    cb.log(f"{Path(r.path).name}: ERROR {r.error}")
                else:
                    speed = (r.info.duration / r.elapsed) if r.elapsed > 0 and r.info else 0
                    cb.log(f"{Path(r.path).name}: {len(r.events)} event(s)  "
                           f"[{r.elapsed:.1f}s, {speed:.0f}x realtime"
                           + (f", auto-ignored {r.suppressed_pct:.1f}% of frame" if r.suppressed_pct >= 0.5 else "")
                           + "]")
                cb.file_done(i, r)
    except Cancelled:
        for f in futs:
            f.cancel()
        out.cancelled = True
        out.results = [r for r in results if r is not None]
        cb.log("Cancelled.")
        return out

    out.results = [r for r in results if r is not None]
    segs = []
    if S.mode == "compile":
        cb.stage("Building compilation")
        try:
            out.compilation, segs = build_compilation(out.results, run_dir, S, cb.compile_progress, cancel, cb.log)
        except Cancelled:
            out.cancelled = True
            cb.log("Cancelled during compilation; writing reports for what was analysed.")
    cb.stage("Writing reports")
    out.reports = write_reports(out.results, run_dir, S, out.compilation, segs)
    n = sum(len(r.events) for r in out.results)
    cb.stage(f"Done: {n} event(s)")
    cb.log(f"Report: {out.reports['html']}")
    return out


def default_jobs() -> int:
    return max(1, min(4, (os.cpu_count() or 2) // 2))
