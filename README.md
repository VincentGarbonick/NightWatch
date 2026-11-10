# Nightwatch

Finds events in dark CCTV recordings (`.ts`, also `.mp4`, `.mkv` and friends):
headlights going past, people or animals moving, and lights switching on or off.
For a batch of files it gives you either:

- **Timestamps**: `events.csv`, `events.json` and `events.txt`, plus `report.html`
  with a brightened thumbnail of every event (the thing that triggered it is boxed), or
- **Timestamps plus one compilation video**: every event cut into a single
  `compilation.mp4`, brightened, with the source file and the *real* clock time
  burned in, and **one chapter per event** (in mpv, PgUp/PgDn jumps between events).

Runs on **Linux and Windows**, and uses an **NVIDIA, AMD or Intel graphics card**
when one is available.

## Install

### Linux (Arch)

```bash
sudo pacman -S --needed ffmpeg python-numpy python-opencv pyside6 mpv
cd nightwatch
./nightwatch.py          # GUI
./install.sh             # optional: `nightwatch` command + app-menu entry
```

`mpv` is optional; VLC also works for double-click playback. For the GPU you also need
the usual drivers:

- **NVIDIA:** `nvidia-utils`
- **AMD:** Mesa's VA-API driver
- **Intel:** `intel-media-driver`

### Windows

1. Install Python 3 from python.org and tick **"Add python.exe to PATH"**
   (or run `winget install Python.Python.3.12`).
2. Double-click **`setup-windows.bat`**. It installs the Python packages and offers to
   install ffmpeg with winget.
3. Double-click **`Nightwatch.bat`** to start. You can also drop files or folders onto it.

For the GPU, a normal NVIDIA, AMD or Intel graphics driver is all you need. The winget
ffmpeg build already includes NVENC, NVDEC, AMF and QSV support.

## First start: platform and graphics card

The GUI opens a **setup window** at startup. In it you:

- Choose **Windows or Linux**. The detected system is marked, and you get a warning if
  you pick the other one.
- Choose a **graphics card**: Auto-detect, NVIDIA, AMD, Intel, or None.
- Optionally point it at an **ffmpeg folder**. It's normally found automatically:
  PATH, winget, scoop, chocolatey, `C:\ffmpeg\bin`.
- Click **Test graphics card** to see exactly what will run on the GPU.

Untick "Show this window every time" if you don't want it at startup. You can reopen it
from **System → Change…** in the sidebar.

On the command line `--platform` is required:

```bash
nightwatch scan /mnt/nvr/front --platform linux -o ~/Videos/nightwatch --compile
py nightwatch.py scan D:\nvr\front --platform windows -o D:\nightwatch --compile --gpu nvidia
nightwatch check --platform linux            # what will use the GPU on this machine?
```

## What the graphics card does (and doesn't)

| Step | GPU path | Effect |
|---|---|---|
| Decoding during a scan | NVIDIA: NVDEC decode **and resize on the card**, copying back only small frames. Linux AMD/Intel: VA-API decode + resize. Windows AMD/Intel: D3D11VA decode. | The big win for 1080p/4K and H.265, which is where scan time goes. |
| Encoding the compilation | NVENC / AMF / Quick Sync / VA-API | Faster cutting when there are many events. |
| Detection itself | stays on the CPU | It works on 320 px frames and is already cheap. A GPU wouldn't change *what* is found. |

**It never trusts the GPU blindly.**

- Each decode path is test-run on the actual file before use (once per codec and
  resolution), and each encoder is test-run once.
- If a GPU path is missing or fails, it falls back to the CPU and says so in the Log tab.
- If GPU decoding dies partway through a file, that file is re-scanned on the CPU.
- If the GPU encoder fails on any clip, the whole compilation is redone on the CPU, because
  clips from different encoders don't join cleanly.

So a GPU problem costs speed, never results. The queue shows `· GPU` next to files that
were decoded on the graphics card. Hover over the status to see the exact path.

## GUI

1. **Queue files.** Drag `.ts` files or whole folders onto the window, or use *Add files* /
   *Add folder*. Folders are searched recursively.
2. **(Optional) Draw ignore regions.** Use this for anything you never care about, like a tree that
   thrashes in the wind or a neighbour's flickering light. The on-screen clock is usually handled
   automatically. Regions are fractions of the frame, so one set covers every file from that camera.
3. **Pick output.** Choose *Timestamps + thumbnails report*, or add a compilation video.
4. **Start.** Events appear as each file finishes. Double-click one to play the source file in
   mpv or VLC at that moment. Right-click to jump to it in the compilation or to copy the timestamp.

Each run makes its own folder:

```
nightwatch_2026-11-03_011500/
  compilation.mp4      # if requested; one chapter per event
  report.html          # thumbnails; click one to jump to it in the compilation
  events.csv / .json / .txt
  thumbs/
  clips/               # only if "Keep single clips" is ticked
```

Settings are remembered in `~/.config/nightwatch/settings.json` on Linux and
`%APPDATA%\nightwatch\settings.json` on Windows. The CLI can reuse them with `--config`.

## Command line

```bash
nightwatch scan DIR --platform linux -o out --compile
nightwatch scan *.ts --platform linux -o out -s 8 --ignore 0,0,0.35,0.08 --night-only
nightwatch scan DIR --platform linux -o out --config ~/.config/nightwatch/settings.json
nightwatch scan --help
```

On Windows use `py nightwatch.py ...` with `--platform windows`.

## Tuning

| Problem | Try |
|---|---|
| Too many false alarms | Lower **sensitivity** (6 is the default). Draw ignore regions over trees or bushes. Tick *Night-time only* if daytime files are mixed in. |
| Insects, rain or snow trigger events | Raise **Min object size** (e.g. 0.2–0.5 %). |
| Missing faint stuff (a person in a dark yard) | Raise sensitivity to 8. Lower min object size. |
| Fast cars in a narrow view are missed | Advanced → **Analysis rate** 8–10 fps. |
| One long event that should be several | Advanced → lower **Merge events within**. |
| Scans slow on a big machine | Advanced → raise **Files in parallel**. With an NVIDIA card, 4–6 is reasonable. |
| Real times missing | Advanced → **Clock from**. Times are read from filenames like MediaMTX's `2026-11-02_23-00-00-123456.ts` or `20261102_230000.ts`. `mtime` uses the file's modified time minus its length. |

**Debug videos** (Advanced → *Write debug videos*, or `--debug-video`) show what the
detector sees, side by side:

- **Left:** the brightened picture. Yellow boxes are events; grey boxes were seen and rejected.
- **Middle:** what changed.
- **Right:** the learnt "always changing" map. Magenta areas are auto-ignored.

## How detection works

1. **Dark boost.** A gamma curve lifts the shadows so small changes in near-black areas count.
2. **Per-pixel noise model.** Grainy areas need a bigger change; clean areas trigger on small ones.
3. **Gain compensation.** Removes the frame-wide "breathing" of cheap night cameras. A big sudden
   whole-frame change (IR switching, a floodlight) is reported as a light-change event.
4. **Auto-ignore.** Pixels that change most of the time (clock digits, LEDs, branches) are learnt
   and ignored.
5. **Tracking gate.** A blob only counts if it *moves* a meaningful distance or is a big lighting
   change. This rejects clock digits rolling over and other one-off static changes.
6. Events closer than 3 s apart are merged and padded (2 s before, 3 s after) for the video.

Cuts are frame-accurate: clips seek early and trim exactly, because `.ts` seeking can land a
second or two late.

**Event kinds** are hints, not object recognition:

- `headlights`: a bright source that moves
- `motion`: something moving, not bright
- `light on` / `light off`: a sudden lighting change that doesn't travel
- `bright light`: a bright source that barely moves

## Limitations

- It's a motion and light detector, not a classifier: a cat and a person are both `motion`.
- The first ~2 s of each file are used to learn the scene.
- The compilation has no audio.
- Ignore regions apply to the whole batch. If you mix cameras, run them separately.
- "Fast (no re-encode)" cuts snap to keyframes, and the overlay and brighten options are skipped.

## Checking the install

`tools/make_synthetic.py OUTDIR` renders synthetic night footage with known events and a
`ground_truth.json`. At the default settings Nightwatch should find all 7 events with no
false alarms.
