# Timelapse Cutter

A small iMovie-style editor for a timelapse filmed with **two cameras**: put both on one timeline,
choose how to combine them (picture-in-picture, side by side, stacked, superposed), remove the parts
where nothing happens, and export a single video, keeping time order.

Requires Python 3 with `numpy` and `ffmpeg`/`ffprobe` on the PATH (`brew install ffmpeg`).

```bash
python3 cutter.py --project ~/Movies/robot-build --a ~/Movies/camA --b ~/Movies/camB
```

`--a` / `--b` take files or folders and are optional: you can also add videos from the UI
(**＋ Camera A / B** opens the macOS file picker, or paste paths). Camera sidecar files such as DJI `.LRF`
low-res proxies are ignored. The project (`project.json`), the
analysis cache (`.cutter/`) and the exports go in the `--project` folder (default `./project`).
Run the same command again to continue where you left off.

## How it works

- **Timeline.** Time is shown both as video time and as real time (1 photo every 2 s at 30 fps, so 1 s of
  video = 1 min of real time; change `settings.interval` in `project.json` if your interval differs).
  Empty time between recordings (nights, breaks) is squeezed to a thin dark spacer labelled with how long
  it was, so the timeline only shows footage.
  New clips are placed by the recording time in their metadata, so the two cameras line up. If the camera
  clocks disagree, drag a clip (⌥-drag moves a whole camera track), or use **Align clips** with another
  source (file modified time, date in the file name, back to back).
  **Sync A to B from footage** fixes camera clocks that are off by a few minutes on some days: both
  cameras film the same scene, so for each camera A clip it finds the shift that makes its activity graph
  match camera B's (in 25-min windows, median of the confident ones) and moves the clip. Clips with no
  overlap take the shift of the nearest measured clip of the same day. A report lists every move; ⌘Z undoes
  it. **Sync to other camera** in the Clip panel does the same for one clip.
- **Photo interval per clip.** If a camera was set to another interval for some clips (e.g. one photo every
  5 s instead of 2 s), those clips would run faster than the other camera. The app reads each clip's
  interval from its file dates (recording start to the time the camera closed the file; for a re-exported
  Insta360 file, the camera's original `.insv` next to it) and offers to stretch the ones that differ. Each
  photo is then held longer, and the clips are synced again from their footage. Set it by hand with *One
  photo every … s* in the Clip panel; the clip label on the timeline shows it when it differs.
- **Replaced files.** Re-export a video (e.g. another framing in Insta360 Studio) to the *same path* and the
  app picks it up within seconds: the clip keeps its place, cuts and layouts, and is re-analysed. A notice
  says if its length changed (check alignment) or if it has blur areas to check.
- **Missing files.** If a video file is moved, renamed or deleted, the app tells you right away (pop-up and
  a ⚠ in the top bar), marks the clip “file missing” on the timeline and in the preview, and refuses to
  export it. **Find moved files** relinks clips by file name and size (Spotlight, then the other clips'
  folders, Desktop, Downloads and Movies).
- **Layout.** Picture in picture (main camera, corner, size, border), side by side, stacked, superpose
  (B blended over A with an opacity), or one camera only. When only one camera has footage at a moment,
  it fills the frame. The preview shows what the export will look like.
- **Remove still parts (automatic).** Each clip is analysed once in the background (frame-to-frame
  difference on a small grey copy, with exposure changes ignored). The orange/grey graph under each clip
  is that activity; the dashed line is the threshold. With **Cut automatically** on (default), every
  stretch where *neither* camera changes for longer than the chosen duration is cut, keeping a little
  time around the activity. The cuts are recomputed whenever the analysis finishes, you move a clip or
  change a slider. Parts you restore (⌫ on a red cut, **Restore**, or shrinking a cut) are remembered
  (green on the ruler) and never auto-cut again; **Forget restored parts** clears that.
  **Delete still parts** cuts them and removes all cut parts from the timeline (**Hide cut parts** / H
  toggles that view; a red line marks where something was removed, click it and ⌫ to bring it back).
  Nothing is deleted from disk.
- **iMovie-style editing.** ⌘B (or ✂ / S) splits at the playhead. Click a clip to select the piece under
  it (yellow), ⌫ deletes it. You can also drag in an empty part of a track (or press I / O) to select any
  range. Double-click a split on the ruler to remove it. ⌘Z / ⇧⌘Z undo / redo.
- **Per-part layout.** With a piece or range selected, **Layout here** overrides the default layout for
  that part, e.g. *PiP — B main*, and **⇄ Swap PiP here** (W) flips which camera is the big one.
  **Hide A / B here** shows only the other camera. Overrides show in yellow on the Output row.
- **Transitions.** Cross-dissolves where the footage jumps: one length *between sessions* (a camera
  switches to another clip, e.g. a new day, where the camera may have moved a bit; default 12 frames) and
  one *at cuts* and layout changes (default 4 frames). Joins that get a dissolve are marked ◆ on the
  Output row; the preview shows them too. Each dissolve shortens the video by its length.
- **Blur.** To hide a computer screen (or anything else), click a clip, then **▭ Draw blur area** in the Clip
  panel and drag a box on the picture (that camera is shown alone while you draw; Esc cancels). Blur
  areas belong to the clip, since a camera may sit differently each day; **Copy to same day** / **Copy to
  all A/B** puts the same areas on the camera's other clips. Remove one with its ✕ chip. The preview and the
  export both blur them.
  **▭ Blur screens here (auto)** in the Edit panel finds computer, laptop and phone screens in the selected
  part (or the selected clip) on both cameras and blurs them for that part only (chips marked *auto*).
  It samples a few frames per clip with an open-vocabulary detector (OWLv2) and keeps what is found in
  most of them, then outlines each screen (segment-anything, SAM) so the blur follows its shape: a
  trapezoid for a screen seen at an angle, a few more corners when the lens bends its edges.
  Needs `python3 -m pip install torch transformers`; the models (~600 MB + ~375 MB) download on first use.
  To adjust an area, click its clip: its corners show as yellow dots on the preview, drag them (a drawn
  box becomes a four-corner shape this way). With nothing selected it looks through the whole video, a
  minute at a time, on the cameras that are shown. **Blur strength** makes every blur lighter or stronger
  (0 turns blurring off without removing the areas).
- **Speed per part.** **Speed here** (Edit panel) plays the selected part 1.5× to 8× faster in the preview and
  the export, on top of the export's overall speed; such parts show a blue mark and their speed on the Output row.
- **Titles.** The Titles panel adds an intro (title and subtitle over the first seconds, on a darkened frame),
  chapter titles (📍 starts a chapter at the playhead; its name shows at the bottom left for a few seconds) and
  end notes over the last seconds (one line each, the first one big). **Show the build time counter** adds the
  real time of work filmed so far in a corner (nights and breaks between recordings don't count), from 0 h 00
  at the first kept moment. The preview shows all of it; the export draws it in one last pass.
- **Readable text.** **🔍 Check for readable text** (Readable text panel) reads the text visible in the kept
  parts, or the selected part, with Apple Vision (built into macOS; a small Swift helper, `ocr.swift`, is
  compiled on first use, which needs the Xcode command-line tools). It takes a frame every 0.5–5 s of the final
  video, scaled to the size that camera has in the export (a picture-in-picture corner is small), so text
  too small for viewers to read is not reported. What looks private comes first: passwords, keys and tokens,
  emails, phone, card and IBAN numbers, web addresses; then other text. Labels and plain numbers are hidden
  unless you ask, and text already under a blur area is left out. Click a finding to see it in the preview
  (red box); ▭ blurs it for the time it is visible, **Blur all private ones** does them all (⌘Z undoes).
- **Music.** **＋ Add music…** in the Music panel picks songs (mp3, m4a, wav, flac…); they play back to
  back over the exported video, in list order (↑ to reorder), from *Start the music at* into the first song,
  looped if shorter than the video (or silent after, if *Loop* is off), with a volume and a fade in / fade
  out at the video's ends. The preview plays it in time with the output; 🔊 under the preview mutes it.
  The export copies the video and adds the music as AAC.
  **✨ Generate music…** makes songs with [ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5) (MIT
  license): describe the style, choose one song or one every ~4 min to fill the video; the songs are sized to
  the exported length and added to the list. It uses ACE-Step's local API server (`./start_api_server_macos.sh`
  in its folder, Apple Silicon via MLX; `ACESTEP_URL` changes the address, default `http://127.0.0.1:8001`),
  or optionally the online ACE-Step Space (needs `gradio_client`, often busy).
- **Export.** Each kept piece is rendered with ffmpeg at the chosen resolution and joined without
  re-encoding. Options: resolution (incl. 4K, wide, vertical), x264 (quality) or Apple hardware (fast)
  encoder, and a speed-up factor.

## Keys

| Key | Action |
| --- | --- |
| Space | play / pause |
| ← / → (⇧ = 1 s) | step one frame |
| ↑ / ↓ | previous / next kept part |
| I / O | selection in / out at playhead |
| X | delete selection |
| ⌘B or S | split at playhead |
| ⌫ | delete selected piece / range, or restore the selected red cut |
| W | swap picture-in-picture here |
| H | hide / show cut parts in the timeline |
| Esc | clear selection |
| + / − / 0 | zoom in / out / fit |
| ⌘Z / ⇧⌘Z | undo / redo |
