"""Reports (CSV / JSON / TXT / HTML) and the single-file event compilation."""
from __future__ import annotations

import csv
import html
import json
import os
import shutil
import subprocess
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .detector import Cancelled, Event, FileResult, Settings
from .media import ProbeInfo, drawtext_available, fmt_hms

# --------------------------------------------------------------------------- helpers


def new_run_dir(outdir: str | os.PathLike) -> Path:
    base = Path(outdir).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run = base / f"nightwatch_{stamp}"
    i = 2
    while run.exists():
        run = base / f"nightwatch_{stamp}_{i}"
        i += 1
    run.mkdir()
    return run


def sort_results(results: list[FileResult]) -> list[FileResult]:
    return sorted(results, key=lambda r: (r.clock is None, r.clock or datetime.min, r.path))


def clock_str(dt: datetime | None, with_date: bool = True) -> str:
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S")


@dataclass
class Segment:
    result: FileResult
    start: float
    end: float
    events: list = field(default_factory=list)
    comp_start: float | None = None     # where it lands in the compilation
    comp_dur: float | None = None
    file: Path | None = None

    @property
    def info(self) -> ProbeInfo:
        return self.result.info

    @property
    def clock(self) -> datetime | None:
        return (self.result.clock + timedelta(seconds=self.start)) if self.result.clock else None

    @property
    def kind(self) -> str:
        order = ["headlights", "motion", "bright light", "light on", "light off"]
        ks = {k for e in self.events for k in (e.kinds or [e.kind])}
        ks = sorted(ks, key=lambda k: order.index(k) if k in order else 99)
        return ", ".join(ks) if ks else "event"


def plan_segments(results: list[FileResult], S: Settings) -> list[Segment]:
    segs: list[Segment] = []
    for r in sort_results(results):
        if r.error or not r.events or r.info is None:
            continue
        dur = r.info.duration or max(e.end for e in r.events) + S.post_roll_s
        cur: Segment | None = None
        for ev in sorted(r.events, key=lambda e: e.start):
            s = max(0.0, ev.start - S.pre_roll_s)
            e = min(dur, ev.end + S.post_roll_s) if dur > 0 else ev.end + S.post_roll_s
            if cur is not None and s <= cur.end + 0.5:
                cur.end = max(cur.end, e)
                cur.events.append(ev)
            else:
                cur = Segment(r, s, e, [ev])
                segs.append(cur)
    return segs


# --------------------------------------------------------------------------- compilation


def _probe_duration(p: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(p)], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def _dt_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "%%")


def _target_format(segs: list[Segment]) -> tuple[int, int, int]:
    res = Counter((s.info.width, s.info.height) for s in segs).most_common(1)[0][0]
    w, h = res
    if w > 1920:
        h = int(round(h * 1920 / w / 2)) * 2
        w = 1920
    fps = Counter(int(round(s.info.fps or 15)) for s in segs).most_common(1)[0][0]
    return w, h, max(5, min(30, fps))


def _run(cmd: list[str], cancel: threading.Event | None, procs: list) -> tuple[int, str]:
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    procs.append(p)
    try:
        while True:
            try:
                _, err = p.communicate(timeout=0.5)
                return p.returncode, err or ""
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set():
                    p.kill()
                    p.communicate()
                    raise Cancelled()
    finally:
        procs.remove(p)


def _encode_segment(seg: Segment, i: int, n: int, tmp: Path, S: Settings, W: int, H: int, FPS: int,
                    use_text: bool, cancel, procs) -> tuple[Segment, str | None]:
    src = seg.result.path
    dur = max(0.2, seg.end - seg.start)
    pre = max(0.0, seg.start - 15.0)        # .ts seeking can land late; seek early, trim precisely
    out = tmp / f"seg_{i:05d}.mp4"
    vf = [f"trim=start={seg.start - pre:.3f}:duration={dur:.3f}", "setpts=PTS-STARTPTS",
          f"scale={W}:{H}:force_original_aspect_ratio=decrease",
          f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2", "setsar=1", f"fps={FPS}"]
    if S.brighten_output:
        vf.append("eq=gamma=1.6")
    if use_text:
        tf = tmp / f"seg_{i:05d}.txt"
        name = Path(src).name
        line1 = f"{name}   event {i}/{n}   [{seg.kind}]"
        if seg.clock is not None:
            epoch = (seg.clock - datetime(1970, 1, 1)).total_seconds()
            line2 = (f"%{{pts:gmtime:{epoch:.3f}:%Y-%m-%d %H\\:%M\\:%S}}"
                     f"     file pos%{{pts:hms:{seg.start:.3f}}}")
        else:
            line2 = f"file pos%{{pts:hms:{seg.start:.3f}}}"
        tf.write_text(_dt_escape(line1) + "\n" + line2)
        fs = max(14, H // 30)
        vf.append(f"drawtext=textfile={tf}:fontcolor=white:fontsize={fs}:box=1:boxcolor=black@0.55:"
                  f"boxborderw={max(4, fs // 3)}:line_spacing={max(2, fs // 5)}:x={fs // 2}:y=h-th-{fs}")
    vf.append("format=yuv420p")
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if S.hwaccel:
        cmd += ["-hwaccel", "auto"]
    if pre > 0:
        cmd += ["-ss", f"{pre:.3f}"]
    cmd += ["-fflags", "+discardcorrupt", "-i", src, "-map", "0:v:0", "-an", "-sn", "-dn",
            "-vf", ",".join(vf), "-c:v", "libx264", "-preset", "veryfast", "-crf", str(S.crf),
            "-g", str(FPS * 2), "-video_track_timescale", "90000", str(out)]
    rc, err = _run(cmd, cancel, procs)
    if rc != 0 or not out.exists():
        return seg, f"{Path(src).name} @ {fmt_hms(seg.start)}: {err.strip()[-300:]}"
    seg.file = out
    return seg, None


def _copy_segment(seg: Segment, i: int, tmp: Path, cancel, procs) -> tuple[Segment, str | None]:
    out = tmp / f"seg_{i:05d}.ts"
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{seg.start:.3f}", "-i", seg.result.path, "-t", f"{seg.end - seg.start:.3f}",
           "-map", "0:v:0", "-an", "-sn", "-dn", "-c", "copy", "-avoid_negative_ts", "make_zero",
           "-f", "mpegts", str(out)]
    rc, err = _run(cmd, cancel, procs)
    if rc != 0 or not out.exists():
        return seg, f"{Path(seg.result.path).name} @ {fmt_hms(seg.start)}: {err.strip()[-300:]}"
    seg.file = out
    return seg, None


def _meta_escape(s: str) -> str:
    for ch in ("\\", "=", ";", "#", "\n"):
        s = s.replace(ch, "\\" + ch)
    return s


def build_compilation(results: list[FileResult], run_dir: Path, S: Settings,
                      progress: Callable[[float], None] | None = None,
                      cancel: threading.Event | None = None,
                      log: Callable[[str], None] | None = None) -> tuple[Path | None, list[Segment]]:
    log = log or (lambda m: None)
    segs = plan_segments(results, S)
    if not segs:
        log("No events, so no compilation was made.")
        return None, []
    fast = S.fast_copy
    if fast:
        sig = {(s.info.codec, s.info.width, s.info.height) for s in segs}
        if len(sig) > 1:
            log("Fast mode needs every file to share codec and resolution; "
                f"found {len(sig)} variants, re-encoding instead.")
            fast = False
    use_text = S.overlay and not fast and drawtext_available()
    if S.overlay and not fast and not use_text:
        log("ffmpeg drawtext is unavailable (no freetype/fonts?), so the compilation has no text overlay.")
    W, H, FPS = _target_format(segs)
    tmp = run_dir / ".tmp_segments"
    tmp.mkdir(exist_ok=True)
    procs: list = []
    errors = []
    n = len(segs)
    done = 0
    log(f"Cutting {n} clip(s) " + ("(stream copy)" if fast else f"(re-encode {W}x{H}@{FPS})") + "...")
    workers = max(1, min(3, (os.cpu_count() or 2) // 2))
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = []
            for i, seg in enumerate(segs, 1):
                if fast:
                    futs.append(ex.submit(_copy_segment, seg, i, tmp, cancel, procs))
                else:
                    futs.append(ex.submit(_encode_segment, seg, i, n, tmp, S, W, H, FPS, use_text, cancel, procs))
            for f in as_completed(futs):
                seg, err = f.result()
                if err:
                    errors.append(err)
                    log("Clip failed: " + err)
                done += 1
                if progress:
                    progress(0.9 * done / n)
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
    except Cancelled:
        for p in list(procs):
            p.kill()
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    good = [s for s in segs if s.file is not None]
    if not good:
        shutil.rmtree(tmp, ignore_errors=True)
        log("Every clip failed; no compilation was written.")
        return None, segs

    # chapters + concat
    t = 0.0
    chap = [";FFMETADATA1", "title=Nightwatch event compilation", ""]
    for i, s in enumerate(good, 1):
        d = _probe_duration(s.file) or (s.end - s.start)
        s.comp_start, s.comp_dur = t, d
        label = (clock_str(s.clock) + "  " if s.clock else "") + \
            f"{s.kind}  ({Path(s.result.path).name} +{fmt_hms(s.start)})"
        chap += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(t * 1000)}",
                 f"END={int((t + d) * 1000)}", f"title={_meta_escape(label)}", ""]
        t += d
    (tmp / "chapters.txt").write_text("\n".join(chap))
    with open(tmp / "list.txt", "w") as fh:
        for s in good:
            fh.write("file '" + str(s.file).replace("'", "'\\''") + "'\n")
    out = run_dir / ("compilation.mkv" if fast else "compilation.mp4")
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", str(tmp / "list.txt"),
           "-i", str(tmp / "chapters.txt"), "-map", "0:v", "-map_metadata", "1", "-map_chapters", "1",
           "-c", "copy"]
    if not fast:
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(out))
    rc, err = _run(cmd, cancel, procs)
    if rc != 0:
        log("Joining clips failed: " + err.strip()[-400:])
        out = None
    if S.keep_clips:
        cd = run_dir / "clips"
        cd.mkdir(exist_ok=True)
        for s in good:
            stem = (s.clock.strftime("%Y-%m-%d_%H-%M-%S") if s.clock
                    else f"{Path(s.result.path).stem}_{fmt_hms(s.start).replace(':', '-')}")
            kind = s.kind.split(",")[0].replace(" ", "-")
            dest = cd / f"{stem}_{kind}{s.file.suffix}"
            shutil.move(str(s.file), dest)
            s.file = dest
    shutil.rmtree(tmp, ignore_errors=True)
    if progress:
        progress(1.0)
    if out is not None:
        log(f"Compilation: {out.name}  ({len(good)} clips, {fmt_hms(t)} long"
            + (f", {len(errors)} failed" if errors else "") + ")")
    return out, segs


# --------------------------------------------------------------------------- reports


def _event_comp_time(ev: Event, segs: list[Segment]) -> float | None:
    for s in segs:
        if s.comp_start is not None and ev in s.events:
            return s.comp_start + max(0.0, ev.start - s.start)
    return None


def write_reports(results: list[FileResult], run_dir: Path, S: Settings,
                  compilation: Path | None = None, segs: list[Segment] | None = None) -> dict:
    segs = segs or []
    results = sort_results(results)
    thumbs = run_dir / "thumbs"
    thumbs.mkdir(exist_ok=True)
    rows = []
    for r in results:
        for ev in r.events:
            if ev.thumb_jpeg and not ev.thumb_path:
                p = thumbs / f"{Path(r.path).stem}_e{ev.index:03d}.jpg"
                p.write_bytes(ev.thumb_jpeg)
                ev.thumb_path = str(p)
            ct = _event_comp_time(ev, segs)
            ev.comp_t = ct
            rows.append(dict(
                file=r.path, event=ev.index,
                clock_start=clock_str(ev.clock_start), clock_end=clock_str(ev.clock_at(ev.end)),
                offset_start=fmt_hms(ev.start, ms=True), offset_end=fmt_hms(ev.end, ms=True),
                start_s=round(ev.start, 2), end_s=round(ev.end, 2), duration_s=round(ev.duration, 2),
                kind=ev.kind, kinds=";".join(ev.kinds), peak_area_pct=round(ev.peak_area_pct, 3),
                thumbnail=os.path.relpath(ev.thumb_path, run_dir) if ev.thumb_path else "",
                compilation_time=fmt_hms(ct, ms=True) if ct is not None else "",
                compilation_s=round(ct, 2) if ct is not None else ""))

    csv_p = run_dir / "events.csv"
    with open(csv_p, "w", newline="") as fh:
        cols = list(rows[0].keys()) if rows else ["file", "event", "clock_start", "offset_start", "kind"]
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    summary = []
    for r in results:
        summary.append(dict(file=r.path, error=r.error, events=len(r.events),
                            duration_s=round(r.info.duration, 2) if r.info else None,
                            resolution=r.info.resolution if r.info else None,
                            clock_start=clock_str(r.clock), analysed_frames=r.frames,
                            dark_fraction=round(r.dark_fraction, 3),
                            auto_ignored_pct=round(r.suppressed_pct, 2), seconds=round(r.elapsed, 1)))
    json_p = run_dir / "events.json"
    json_p.write_text(json.dumps(dict(settings=S.to_dict(), files=summary, events=rows,
                                      compilation=compilation.name if compilation else None), indent=2))

    lines = [f"Nightwatch report: {datetime.now():%Y-%m-%d %H:%M}",
             f"{sum(len(r.events) for r in results)} event(s) in {len(results)} file(s)", ""]
    for r in results:
        lines.append(f"== {Path(r.path).name}" + (f"  (starts {clock_str(r.clock)})" if r.clock else ""))
        if r.error:
            lines.append(f"   ERROR: {r.error}")
        for ev in r.events:
            when = clock_str(ev.clock_start, with_date=False)
            lines.append(f"   #{ev.index:<3} {when + '  ' if when else ''}+{fmt_hms(ev.start)}  "
                         f"{ev.duration:5.1f}s  {ev.kind}")
        if not r.error and not r.events:
            lines.append("   (nothing detected)")
        lines.append("")
    txt_p = run_dir / "events.txt"
    txt_p.write_text("\n".join(lines))

    html_p = run_dir / "report.html"
    html_p.write_text(_html(results, rows, compilation, run_dir))
    return dict(csv=csv_p, json=json_p, txt=txt_p, html=html_p)


def _html(results, rows, compilation, run_dir) -> str:
    esc = html.escape
    by_file: dict[str, list] = {}
    for row in rows:
        by_file.setdefault(row["file"], []).append(row)
    parts = []
    for r in results:
        evs = by_file.get(r.path, [])
        head = esc(Path(r.path).name)
        if r.clock:
            head += f' <span class="dim">starts {esc(clock_str(r.clock))}</span>'
        head += f' <span class="dim">· {len(evs)} event(s)</span>'
        cards = []
        for row in evs:
            when = row["clock_start"][11:] if row["clock_start"] else "+" + row["offset_start"][:8]
            img = f'<img src="{esc(row["thumbnail"])}" loading="lazy">' if row["thumbnail"] else ""
            attr = f' data-t="{row["compilation_s"]}"' if row["compilation_s"] != "" else ""
            cards.append(f'<div class="card"{attr}>{img}<div class="meta"><b>{esc(when)}</b> '
                         f'<span class="kind">{esc(row["kind"])}</span><br>'
                         f'<span class="dim">{row["duration_s"]}s · file +{esc(row["offset_start"][:8])}</span>'
                         f'</div></div>')
        err = f'<p class="err">{esc(r.error)}</p>' if r.error else ""
        parts.append(f"<h2>{head}</h2>{err}<div class='grid'>{''.join(cards) or '<p class=dim>Nothing detected.</p>'}</div>")
    video = ""
    if compilation:
        video = (f'<video id="v" src="{esc(compilation.name)}" controls preload="metadata"></video>'
                 '<p class="dim">Click an event to jump to it in the compilation.</p>')
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Nightwatch report</title>
<style>
body{{background:#111316;color:#e6e6e6;font:14px/1.4 system-ui,sans-serif;margin:24px;}}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:15px;margin:28px 0 10px;font-weight:600}}
.dim{{color:#8b9098;font-weight:400}} .err{{color:#ff8a80}}
video{{width:100%;max-width:1100px;background:#000;border-radius:6px;display:block}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}}
.card{{background:#1b1e23;border-radius:6px;overflow:hidden;border:1px solid #262a31}}
.card[data-t]{{cursor:pointer}} .card[data-t]:hover{{border-color:#f5c542}}
.card img{{width:100%;display:block}} .meta{{padding:8px 10px}}
.kind{{background:#2b3038;border-radius:3px;padding:1px 6px;font-size:12px;margin-left:4px}}
</style></head><body>
<h1>Nightwatch report</h1><p class="dim">Generated {datetime.now():%Y-%m-%d %H:%M} ·
{len(rows)} event(s) across {len(results)} file(s)</p>
{video}{''.join(parts)}
<script>
const v=document.getElementById('v');
document.querySelectorAll('.card[data-t]').forEach(c=>c.onclick=()=>{{
  if(!v)return; v.currentTime=parseFloat(c.dataset.t); v.play(); window.scrollTo({{top:0,behavior:'smooth'}});}});
</script></body></html>"""
