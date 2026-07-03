# `--above-osd` map mode (removed) — how it worked

This documents the **above-OSD** map mode that used to exist in `gs/map.sh`,
`gs/mapwin`, `wfb-stabilizer/render_direct.py` and (indirectly) `msposd`
(`osd/util/Render_gs.c`). It was removed to simplify the code; the default
**below-OSD** mode is the only one that remains. Keep this so the mode can be
reconstructed if needed.

## The two modes

The GS shows four X11 windows layered on one screen (bottom → top):

```
default "below-OSD":   video (fullscreen, opaque)  <  map  <  OSD (transparent)
above-osd:             video (normal layer)        <  OSD  <  map
```

- **below-OSD (default, kept):** the fullscreen video covers the taskbar; the
  transparent OSD is on top drawing telemetry; the map rides between them.
- **above-OSD (removed):** the map is drawn *on top of* the OSD as a normal,
  focusable window. Because a focusable managed window cannot be stacked above a
  fullscreen window on this WM, the video was dropped out of fullscreen (so the
  taskbar became visible) and the OSD was restacked just below the map.

## Selection signal

The mode was selected by the presence of the file **`/tmp/msposd-no-raise`**
(an existing msposd flag, reused — not a new file):

- `map.sh --above-osd` created it on show and removed it on hide/kill.
- Default mode never created it.

Three processes read it:

1. **msposd** — `osd_raise_enabled()` in `Render_gs.c`:
   ```c
   return getenv("MSPOSD_NO_RAISE") == NULL && access("/tmp/msposd-no-raise", F_OK) != 0;
   ```
   The OSD self-raises to the top every frame *only* when this returns true.
   With the flag present, msposd stopped self-raising so render_direct.py could
   push the OSD below the map. (This helper is baseline msposd behaviour and is
   still present; nothing creates the flag anymore.)

2. **render_direct.py** — `_restack()` branched on `os.path.exists("/tmp/msposd-no-raise")`.

3. (map.sh created it; mapwin was told via the `--above-osd` arg.)

## What each component did in above-OSD mode

### `gs/map.sh` (`--above-osd`)
- Parsed `--above-osd` → `ABOVE_OSD=1`, and passed `--above-osd` through to
  `mapwin` via `EXTRA`.
- `show_window` / `launch_mapwin`: `touch /tmp/msposd-no-raise` when `ABOVE_OSD=1`.
- `hide_window` / `force_restart`: `rm -f /tmp/msposd-no-raise`.
- `show_window`: **focused/activated** the map (`xdotool windowfocus` +
  `windowactivate`) — the map is meant to be the top, active window here.

### `gs/mapwin` (`--above-osd`)
- With `--above-osd`: self-focus on launch (`focus_map()` + a 150 ms re-assert),
  i.e. the map grabbed focus and raised itself.
- Without it (below-OSD): `win.set_focus_on_map(False)` and no self-focus, so the
  map never became the active window (which is what keeps the fullscreen video
  active and the taskbar covered).

### `wfb-stabilizer/render_direct.py` — `_restack()` above-OSD branch
```python
above_osd = os.path.exists("/tmp/msposd-no-raise")
...
map_frame = self._find_map() if above_osd else None       # xdotool search 'msposd-map-*'
if above_osd and map_frame is not None:
    # A focusable map cannot sit above a fullscreen window on this WM, so drop the
    # video to a normal full-size window (taskbar shows) and put the OSD just
    # below the map.
    if self._fs_state is not False:
        self.window.unfullscreen()
        self.window.move(0, 0)
        self.window.resize(sw, sh)          # default size == full screen so it stays full-size
        self._fs_state = False
    wobj(osd).configure(sibling=wobj(map_frame), stack_mode=_X.Below)
else:
    # DEFAULT below-OSD (kept): video fullscreen + active, OSD on top, map between.
    ...
```

Supporting pieces that existed only for this mode and were removed with it:
- `_find_map()` — located the map's top-level frame via `xdotool search --name '^msposd-map-'`.
- `_fs_state` — tracked whether the video was currently fullscreen, so the
  fullscreen/un-fullscreen transition ran only on change.
- The video window's `set_default_size(full-screen)` — so `unfullscreen()` restored
  it to full size instead of a small default.

## Key WM facts behind the design (Xfwm + compositor)

- A **managed** window cannot be stacked above an **override_redirect** window
  (the OSD). So the map cannot raise itself over the OSD; the OSD must be
  *lowered* below the map instead — done from render_direct.py with
  `XConfigureWindow(osd, sibling=map_frame, stack_mode=Below)` (the OSD is
  override_redirect, so this direct restack is honoured).
- A managed window also cannot be stacked above the **xfce panel (dock)**. And a
  fullscreen window sits above the dock, so a fullscreen video would cover the
  map. Hence above-OSD had to un-fullscreen the video — which is why the taskbar
  became visible in that mode.
- The fullscreen video only covers the taskbar while it is the **active** window
  (compositor un-redirect). This is the invariant the default mode relies on and
  is why the default mode keeps re-asserting the video as active.

## To reconstruct

Re-add: the `--above-osd` arg in map.sh (+ `ABOVE_OSD`, the `NO_RAISE_FLAG`
touch/rm, focusing the map, passing `--above-osd` to mapwin); the `--above-osd`
arg in mapwin (self-focus vs `set_focus_on_map(False)`); and in render_direct.py
the `above_osd` flag read, `_find_map()`, `_fs_state`, and the above-OSD branch
of `_restack()`.
