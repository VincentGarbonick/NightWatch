# Nightwatch

Finds events in dark CCTV recordings (`.ts`, also `.mp4`, `.mkv` and friends):
headlights going past, people or animals moving, and lights switching on or off.
For a batch of files it gives you either:

- **Timestamps**: `events.csv`, `events.json` and `events.txt`, plus `report.html`
  with a brightened thumbnail of every event (the thing that triggered it is boxed), or
- **Timestamps plus one compilation video**: every event cut into a single
  `compilation.mp4`, brightened, with the source file and the *real* clock time
  burned in, and **one chapter per event** (in mpv, PgUp/PgDn jumps between events).

## Install (Arch)

```bash
sudo pacman -S --needed ffmpeg python-numpy python-opencv pyside6 mpv
```

`mpv` is optional. It's used for double-click playback, and VLC works too.

Unzip anywhere, then:

```bash
cd nightwatch
./nightwatch.py          # opens the GUI
./install.sh             # optional: adds a `nightwatch` command and an app-menu entry
```

If you'd rather use a venv (lighter than Arch's full OpenCV):
`python -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python nightwatch.py`

## GUI

1. **Queue files.** Drag `.ts` files or whole folders onto the window, or use *Add files* /
   *Add folder*. Folders are searched recursively.
2. **(Optional) Draw ignore regions.** Use this for anything you never care about, like a tree that
   thrashes in the wind, a busy road far away, or a neighbour's flickering light. The on-screen
   clock is usually handled automatically, but masking it doesn't hurt. Regions are stored as
   fractions of the frame, so one set covers every file from that camera.
3. **Pick output.** Choose *Timestamps + thumbnails report*, or add a compilation video.
4. **Start.** Events appear as each file finishes. Double-click one to play the source file
   in mpv at that moment. Right-click to jump to it in the compilation or to copy the timestamp.

Each run makes its own folder, so nothing gets overwritten:

```
nightwatch_2026-11-03_011500/
  compilation.mp4      # if requested; one chapter per event
  report.html          # thumbnails; click one to jump to it in the compilation
  events.csv / .json / .txt
  thumbs/
  clips/               # only if "Keep single clips" is ticked
```

Settings are remembered in `~/.config/nightwatch/settings.json`.

## Command line

Same engine, scriptable (cron, etc.):

```bash
nightwatch scan /mnt/nvr/recordings/front -o ~/Videos/nightwatch --compile
nightwatch scan *.ts -o out -s 8 --ignore 0,0,0.35,0.08 --night-only
nightwatch scan DIR -o out --config ~/.config/nightwatch/settings.json   # reuse GUI settings
nightwatch scan --help
```

## Tuning

| Problem | Try |
|---|---|
| Too many false alarms | Lower **sensitivity** (6 is the default). Draw ignore regions over trees or bushes. Tick *Night-time only* if daytime files are mixed in. |
| Insects, rain or snow trigger events | Raise **Min object size** (e.g. 0.2–0.5 %). Bugs near an IR light are big but brief. |
| Missing faint stuff (a person in a dark yard) | Raise sensitivity to 8. Lower min object size. |
| Fast cars in a narrow view are missed | Advanced → **Analysis rate** 8–10 fps. |
| One long event that should be several | Advanced → lower **Merge events within**. |
| Real times missing | Advanced → **Clock from**. Times are read from filenames like MediaMTX's `2026-11-02_23-00-00-123456.ts` or `20261102_230000.ts`. `mtime` uses the file's modified time minus its length. |

**Debug videos** (Advanced → *Write debug videos*, or `--debug-video`) show what the
detector sees, side by side:

- **Left:** the brightened picture. Yellow boxes are events; grey boxes were seen and rejected.
- **Middle:** what changed.
- **Right:** the learnt "always changing" map. Magenta areas are auto-ignored.

This is the fastest way to tune it for a particular camera.

## How it works

Each file is decoded once by ffmpeg at a low rate (5 fps) and small size (320 px), so it
runs at roughly 30–60× realtime per file, and files run in parallel.

1. **Dark boost.** A gamma curve lifts the shadows so small changes in near-black areas count.
2. **Per-pixel noise model.** Every pixel learns how noisy it normally is. Grainy areas need a
   bigger change to count, and clean areas can trigger on small ones.
3. **Gain compensation.** Cheap night cameras constantly "breathe" (auto-exposure, keyframe
   pops). The detector subtracts these frame-wide shifts. A *large* sudden shift (IR mode
   switching, a floodlight) is reported as a light-change event.
4. **Auto-ignore.** Pixels that are "changed" most of the time (clock digits, blinking LEDs,
   swaying branches) are learnt and ignored.
5. **Tracking gate.** A changed blob only becomes an event if it *moves* a meaningful distance
   (cars, people, animals) or is a big lighting change (porch light, headlights sweeping across
   a wall). This rejects clock digits rolling over and other one-off static changes.
6. Events closer than 3 s apart are merged and padded (2 s before, 3 s after) for the video.

Cuts are frame-accurate. `.ts` seeking can land a second or two late, so clips are cut by
seeking early and trimming exactly. The burned-in clock in the compilation was checked
against the camera's own OSD clock to the second.

**Event kinds** are heuristic hints, not object recognition:

- `headlights`: a bright light source that moves
- `motion`: something moving, not bright
- `light on` / `light off`: a sudden lighting change that doesn't travel
- `bright light`: a bright source that barely moves

## Limitations

- It's a motion and light detector, not a classifier. A cat and a person both come out as
  `motion`. Running an object detector on just the event thumbnails would be a natural next
  step.
- The first ~2 s of each file are used to learn the scene, so an event right at the start of a
  file can be missed. This rarely matters with long recording segments.
- The compilation has no audio.
- Ignore regions apply to the whole batch. If you mix cameras, run them separately.
- "Fast (no re-encode)" cuts snap to keyframes, and the overlay and brighten options are skipped.

## Checking the install

`tools/make_synthetic.py OUTDIR` renders synthetic night footage with known events
(cars, a faint walker, a porch light, a moth, plus an OSD clock, a blinking LED and a swaying
branch as distractors), with `ground_truth.json`. At the default settings Nightwatch should
find all 7 events with no false alarms.
