"""Graphics-card acceleration with automatic CPU fallback.

Two places use the GPU:

* Analysis decode. Decoding H.264/H.265 is the expensive part of a scan; the
  detector itself works on tiny 320 px frames and gains nothing from a GPU.
  NVIDIA:  -hwaccel cuda + scale_cuda (decode and shrink on the card, copy back
           only the small frames)
  Linux AMD/Intel:  VAAPI decode + scale_vaapi
  Windows AMD/Intel:  D3D11VA decode
* Compilation encode: h264_nvenc / h264_amf / h264_qsv / h264_vaapi.

Nothing is trusted blindly: every decode chain is test-run on the actual file
(cached per codec+resolution) and every encoder is test-run once, falling back
down the list to the CPU. GPU problems therefore cost speed, never results.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import system

VENDORS = ("auto", "nvidia", "amd", "intel", "none")
VENDOR_LABELS = {"auto": "Auto-detect", "nvidia": "NVIDIA", "amd": "AMD", "intel": "Intel",
                 "none": "None (CPU only)"}
_PCI_VENDOR = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}

_lock = threading.Lock()
_decode_cache: dict = {}
_list_cache: dict = {}
_encoder_cache: dict = {}


# --------------------------------------------------------------------------- decode chains


@dataclass(frozen=True)
class DecodeChain:
    name: str
    input_args: tuple = ()
    gpu_scale: str = ""        # e.g. "scale_cuda={w}:{h}:format=nv12"; empty = scale on CPU

    @property
    def is_gpu(self) -> bool:
        return bool(self.input_args)

    def vf(self, afps: float, w: int, h: int) -> str:
        if self.gpu_scale:
            return (self.gpu_scale.format(w=w, h=h) +
                    f",hwdownload,format=nv12,fps={afps:g},format=bgr24")
        return f"fps={afps:g},scale={w}:{h}:flags=area,format=bgr24"


CPU_DECODE = DecodeChain("CPU")


def render_nodes(vendor: str | None = None) -> list[tuple[str, str]]:
    """Linux /dev/dri render nodes as (path, vendor), optionally filtered by vendor."""
    out = []
    for n in sorted(Path("/dev/dri").glob("renderD*")) if Path("/dev/dri").is_dir() else []:
        v = "unknown"
        try:
            v = _PCI_VENDOR.get((Path("/sys/class/drm") / n.name / "device" / "vendor").read_text().strip(), "unknown")
        except OSError:
            pass
        if vendor in (None, "auto") or v == vendor:
            out.append((str(n), v))
    return out


def decode_chains(vendor: str) -> list[DecodeChain]:
    """Candidate decode chains, fastest first, always ending with the CPU."""
    if vendor == "none":
        return [CPU_DECODE]
    chains: list[DecodeChain] = []
    if vendor in ("auto", "nvidia"):
        chains += [DecodeChain("NVIDIA decode + GPU resize", ("-hwaccel", "cuda", "-hwaccel_output_format", "cuda"),
                               "scale_cuda={w}:{h}:format=nv12"),
                   DecodeChain("NVIDIA decode", ("-hwaccel", "cuda"))]
    if vendor in ("auto", "amd", "intel"):
        if system.is_windows():
            chains.append(DecodeChain("D3D11VA decode", ("-hwaccel", "d3d11va")))
        else:
            for node, v in render_nodes(None if vendor == "auto" else vendor):
                if vendor == "auto" and v == "nvidia":
                    continue
                chains += [DecodeChain(f"VAAPI decode + GPU resize ({Path(node).name})",
                                       ("-hwaccel", "vaapi", "-hwaccel_device", node, "-hwaccel_output_format", "vaapi"),
                                       "scale_vaapi=w={w}:h={h}:format=nv12"),
                           DecodeChain(f"VAAPI decode ({Path(node).name})",
                                       ("-hwaccel", "vaapi", "-hwaccel_device", node))]
    chains.append(CPU_DECODE)
    return chains


def _ffmpeg_list(flag: str) -> set[str]:
    """Names from `ffmpeg -encoders` / `-filters` / `-hwaccels` (cached per ffmpeg binary)."""
    key = (flag, system.ffmpeg())
    if key in _list_cache:
        return _list_cache[key]
    names: set[str] = set()
    try:
        out = subprocess.run([system.ffmpeg(), "-hide_banner", flag], capture_output=True, timeout=20,
                             **system.TEXT, **system.popen_kw())
        for line in out.stdout.splitlines():
            parts = line.split()
            if flag == "-hwaccels":
                if len(parts) == 1 and parts[0] != "Hardware":
                    names.add(parts[0])
            elif len(parts) >= 2 and not line.startswith(" --") and parts[0] not in ("Encoders:", "Filters:"):
                names.add(parts[1])
    except (OSError, subprocess.TimeoutExpired):
        pass
    _list_cache[key] = names
    return names


def _missing_feature(chain: DecodeChain) -> str | None:
    if not chain.is_gpu:
        return None
    hw = chain.input_args[1]
    if hw not in _ffmpeg_list("-hwaccels"):
        return f"{hw} not in this ffmpeg build"
    flt = chain.gpu_scale.split("=")[0] if chain.gpu_scale else ""
    if flt and flt not in _ffmpeg_list("-filters"):
        return f"{flt} not in this ffmpeg build"
    return None


def _test_decode(path: str, chain: DecodeChain, afps: float, w: int, h: int) -> bool:
    return _test_decode_why(path, chain, afps, w, h)[0]


def _test_decode_why(path: str, chain: DecodeChain, afps: float, w: int, h: int) -> tuple[bool, str]:
    miss = _missing_feature(chain)
    if miss:
        return False, miss
    cmd = [system.ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", *chain.input_args,
           "-t", "4", "-i", path, "-map", "0:v:0", "-an", "-sn", "-dn",
           "-vf", chain.vf(afps, w, h), "-frames:v", "3", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=45, **system.popen_kw())
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    ok = out.returncode == 0 and len(out.stdout) >= w * h * 3
    return ok, "" if ok else _first_error(out.stderr.decode("utf-8", "replace"))


def choose_decoder(path: str, info, vendor: str, afps: float, w: int, h: int,
                   log=None) -> DecodeChain:
    """Fastest chain that actually works on this file (cached per codec/resolution)."""
    if vendor == "none":
        return CPU_DECODE
    key = (system.target(), vendor, system.ffmpeg(), info.codec, info.pix_fmt, info.width, info.height, w, h)
    with _lock:
        if key in _decode_cache:
            return _decode_cache[key]
        tried = []
        for chain in decode_chains(vendor):
            if not chain.is_gpu or _test_decode(path, chain, afps, w, h):
                break
            tried.append(chain.name)
        if log and tried:
            log(f"GPU decode not usable for {info.codec} {info.resolution} via {', '.join(tried)}; "
                f"using {chain.name}.")
        _decode_cache[key] = chain
        return chain


# --------------------------------------------------------------------------- encoders


@dataclass(frozen=True)
class Encoder:
    name: str
    label: str
    vendor: str
    global_args: tuple = ()            # placed before -i (device init)
    decode_args: tuple = ()            # hw decode for cutting clips (frames come back to RAM)
    out_filter: str = "format=yuv420p"  # last filter(s) before the encoder
    params: tuple = ()                 # with {q} for quality and {g} for GOP

    @property
    def is_gpu(self) -> bool:
        return self.name != "libx264"

    def codec_args(self, q: int, gop: int) -> list[str]:
        return ["-c:v", self.name] + [p.format(q=q, g=gop) for p in self.params]


X264 = Encoder("libx264", "CPU (libx264)", "none",
               params=("-preset", "veryfast", "-crf", "{q}", "-g", "{g}"))


def encoders(vendor: str) -> list[Encoder]:
    """Candidate encoders, preferred first, always ending with libx264."""
    if vendor == "none":
        return [X264]
    out: list[Encoder] = []
    if vendor in ("auto", "nvidia"):
        out.append(Encoder("h264_nvenc", "NVIDIA NVENC", "nvidia", decode_args=("-hwaccel", "cuda"),
                           params=("-preset", "p4", "-rc", "vbr", "-cq", "{q}", "-b:v", "0", "-g", "{g}")))
    if system.is_windows():
        if vendor in ("auto", "amd"):
            out.append(Encoder("h264_amf", "AMD AMF", "amd", decode_args=("-hwaccel", "d3d11va"),
                               params=("-quality", "balanced", "-rc", "cqp", "-qp_i", "{q}", "-qp_p", "{q}",
                                       "-qp_b", "{q}", "-g", "{g}")))
        if vendor in ("auto", "intel"):
            out.append(Encoder("h264_qsv", "Intel Quick Sync", "intel", decode_args=("-hwaccel", "d3d11va"),
                               out_filter="format=nv12",
                               params=("-preset", "medium", "-global_quality", "{q}", "-g", "{g}")))
    else:
        if vendor in ("auto", "amd", "intel"):
            for node, v in render_nodes(None if vendor == "auto" else vendor):
                if vendor == "auto" and v == "nvidia":
                    continue
                out.append(Encoder("h264_vaapi", f"VAAPI ({v}, {Path(node).name})", v,
                                   global_args=("-vaapi_device", node),
                                   decode_args=("-hwaccel", "vaapi", "-hwaccel_device", node),
                                   out_filter="format=nv12,hwupload",
                                   params=("-qp", "{q}", "-g", "{g}")))
    out.append(X264)
    return out


def _test_encode(enc: Encoder) -> tuple[bool, str]:
    if enc.is_gpu and _ffmpeg_list("-encoders") and enc.name not in _ffmpeg_list("-encoders"):
        return False, f"{enc.name} not in this ffmpeg build"
    with tempfile.TemporaryDirectory() as td:
        cmd = [system.ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", *enc.global_args,
               "-f", "lavfi", "-i", "testsrc2=s=640x360:r=15:d=1", "-vf", enc.out_filter,
               *enc.codec_args(23, 30), "-frames:v", "15", "-y", str(Path(td) / "t.mp4")]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=60, **system.TEXT, **system.popen_kw())
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)
        ok = out.returncode == 0 and (Path(td) / "t.mp4").is_file() and (Path(td) / "t.mp4").stat().st_size > 0
        return ok, "" if ok else _first_error(out.stderr)


def _first_error(stderr: str) -> str:
    """The first ffmpeg error line is the informative one ("Cannot load libcuda.so.1")."""
    for line in (stderr or "").splitlines():
        line = re.sub(r"^\[[^\]]*@ 0x[0-9a-f]+\]\s*", "", line.strip())
        if line:
            return line[:160]
    return "failed"


def choose_encoder(vendor: str, log=None) -> Encoder:
    key = (system.target(), vendor, system.ffmpeg())
    with _lock:
        if key in _encoder_cache:
            return _encoder_cache[key]
        tried = []
        for enc in encoders(vendor):
            if not enc.is_gpu:
                break
            ok, why = _test_encode(enc)
            if ok:
                break
            tried.append(f"{enc.label}: {why}")
        if log and tried and vendor != "none":
            log("GPU encoder unavailable (" + "; ".join(tried) + f"); using {enc.label}.")
        _encoder_cache[key] = enc
        return enc


# --------------------------------------------------------------------------- report


@dataclass
class Report:
    platform: str
    detected_platform: str
    ffmpeg: str
    version: str | None
    hwaccels: list = field(default_factory=list)
    render_nodes: list = field(default_factory=list)
    decoders: list = field(default_factory=list)    # (name, ok, why)
    encoders: list = field(default_factory=list)    # (label, ok, why)
    chosen_decoder: str = ""
    chosen_encoder: str = ""

    def lines(self) -> list[str]:
        L = [f"Platform:  {self.platform.title()}  (this computer: {self.detected_platform.title()})",
             f"ffmpeg:    {self.ffmpeg}",
             f"           {(self.version or 'NOT FOUND').split(' Copyright')[0]}"]
        if self.hwaccels:
            L.append("hwaccels:  " + ", ".join(self.hwaccels))
        if self.render_nodes:
            L.append("GPUs:      " + ", ".join(f"{Path(n).name} ({v})" for n, v in self.render_nodes))
        L.append("Decode (tested on a generated H.264 clip):")
        L += [f"   {'OK ' if ok else '-- '} {n}" + (f"   ({why})" if why and not ok else "")
              for n, ok, why in self.decoders]
        L.append("Encode:")
        L += [f"   {'OK ' if ok else '-- '} {n}" + (f"   ({why})" if why and not ok else "") for n, ok, why in self.encoders]
        L.append(f"Will use:  decode = {self.chosen_decoder};  encode = {self.chosen_encoder}")
        return L


def hwaccels() -> list[str]:
    try:
        out = subprocess.run([system.ffmpeg(), "-hide_banner", "-hwaccels"], capture_output=True, timeout=15,
                             **system.TEXT, **system.popen_kw())
        return [l.strip() for l in out.stdout.splitlines()[1:] if l.strip()]
    except (OSError, subprocess.TimeoutExpired):
        return []


def check(vendor: str) -> Report:
    """Full capability report (used by `nightwatch check` and the GUI Test button)."""
    rep = Report(system.target(), system.detected_platform(), system.ffmpeg(), system.ffmpeg_version(),
                 hwaccels(), render_nodes() if not system.is_windows() else [])
    if rep.version is None:
        return rep
    with tempfile.TemporaryDirectory() as td:
        clip = str(Path(td) / "probe.ts")
        subprocess.run([system.ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", "testsrc2=s=1280x720:r=15:d=2", "-c:v", "libx264", "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p", "-f", "mpegts", clip],
                       capture_output=True, timeout=60, **system.popen_kw())
        chosen = None
        for chain in decode_chains(vendor):
            if Path(clip).is_file():
                ok, why = _test_decode_why(clip, chain, 5, 640, 360)
            else:
                ok, why = (not chain.is_gpu), "could not create test clip"
            rep.decoders.append((chain.name, ok, why))
            if ok and chosen is None:
                chosen = chain.name
        rep.chosen_decoder = chosen or "CPU"
    chosen_enc = None
    for enc in encoders(vendor):
        ok, why = _test_encode(enc)
        rep.encoders.append((enc.label, ok, why))
        if ok and chosen_enc is None:
            chosen_enc = enc.label
    rep.chosen_encoder = chosen_enc or "none"
    return rep
