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
  New clips are placed by the recording time in their metadata, so the two cameras line up. If the camera
  clocks disagree, drag a clip (⌥-drag moves a whole camera track), or use **Align clips** with another
  source (file modified time, date in the file name, back to back).
  **Sync A to B from footage** fixes camera clocks that are off by a few minutes on some days: both
  cameras film the same scene, so for each camera A clip it finds the shift that makes its activity graph
  match camera B's (in 25-min windows, median of the confident ones) and moves the clip. Clips with no
  overlap take the shift of the nearest measured clip of the same day. A report lists every move; ⌘Z undoes
  it. **Sync to other camera** in the Clip panel does the same for one clip.
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
