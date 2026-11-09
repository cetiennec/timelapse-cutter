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
- **Light preview copies.** Every photo of a timelapse is different, so the original files are heavy to
  decode at 2–8× speed, two at a time. After the analysis, the app makes a 540p copy of each clip in the
  background (in `.cutter/`, ~3 Mbps) and the preview plays those; the export always uses the originals.
  A camera that isn't on screen is paused.
- **Several windows.** The project can be open in several tabs (or driven by Claude): an edit saved in one
  shows up in the others within a second, so they don't overwrite each other.
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
  the export, on top of the export's overall speed; such parts show a blue mark and their speed on the Output row. Drag a part's
  edge there to extend or shorten it, its middle to move it, and click it to select it (Speed here then
  changes or removes it). A part edited by hand is kept when Auto speed runs again.
  Where the speed changes it doesn't jump: it ramps smoothly over a few seconds of footage (*Smooth speed
  changes over*, 3 s by default, 0 for instant changes). The export follows the ramp photo by photo, and the
  preview, titles and music follow it too.
- **Auto speed.** In the Edit panel: set a target length and the fastest speed allowed, and **⚡ Auto speed**
  plays the quiet parts faster and keeps the busy ones at normal speed, from the activity graphs: each few
  seconds of kept footage is ranked from quietest to busiest and gets a speed along a curve, which is adjusted
  until the video has the target length. Speeds held only a few seconds are smoothed out. Each chapter's first
  20 s stays at normal speed, and so does a whole chapter marked **1×** in the Titles panel. The result is
  ordinary *Speed here* parts (change any by hand); running it again redoes them, **Remove auto speeds** clears
  them, ⌘Z undoes.
- **Titles.** The Titles panel adds an intro (title and subtitle over the first seconds, on a darkened frame),
  chapter titles (📍 starts a chapter at the playhead; its name shows big at the bottom left for a few
  seconds, then small until the next chapter, unless that option is off) and
  end notes over the last seconds (one line each, the first one big). **Show the build time counter** adds the
  real time of the footage shown so far in a corner: only what is kept in the video counts (not the cut still
  parts, nights or breaks), from 0 h 00 at the start. The preview shows all of it; the export draws it in one last pass.
- **Opening sequence and end card.** In the Titles panel, *Intro* can instead be a 10 s opening before the
  video, in a cream, coral and serif look: one or two videos (e.g. the first walk) playing as rounded cards,
  then a photo (e.g. all the parts on the floor) with a question appearing word by word, then the title and
  subtitle (left empty, the subtitle says the build time and the video's length), fading into the timelapse.
  *End notes* can be an end card after the video: "Thanks for watching" (the first line), up to three logos
  and the other lines small. **▶ Preview** renders either one alone to watch it; the export adds them, with
  the music running under both. They are drawn by `intro.py`.
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
  out at the video's ends, and each song cross-fading into the next (*Crossfade between songs*, 3 s by default).
  While music plays, the preview follows the music's clock, so the sound stays smooth and the picture keeps up
  with it; 🔊 under the preview mutes it.
  The export copies the video and adds the music as AAC.
  **✨ Generate music…** makes songs with [ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5) (MIT
  license): describe the style, choose one song or one every ~4 min to fill the video; the songs are sized to
  the exported length and added to the list. It uses ACE-Step's local API server (`./start_api_server_macos.sh`
  in its folder, Apple Silicon via MLX; `ACESTEP_URL` changes the address, default `http://127.0.0.1:8001`),
  or optionally the online ACE-Step Space (needs `gradio_client`, often busy).
- **Export.** Each kept piece is rendered with ffmpeg at the chosen resolution and joined without
  re-encoding. Options: resolution (incl. 4K, wide, vertical), x264 (quality) or Apple hardware (fast)
  encoder, a speed-up factor, and a frame rate. A sped-up part has more photos than frames to show them:
  at **60 fps** a part played 2× still shows every photo (twice the footage's 30), and 4× half of them
  instead of a quarter. With **Blend the photos a frame skips**, a frame standing for several photos shows
  their average: still things stay sharp, moving ones get a soft motion blur instead of jumping.

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
