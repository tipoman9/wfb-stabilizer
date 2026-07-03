# Ground-station window stacking

The GS display is composed of four X11 windows layered on one screen. Three are
transparent overlays composited over a single opaque video layer.

## The four windows (bottom → top)

| Layer | Window | Type | Opaque? | Owner |
|------|--------|------|---------|-------|
| 1 | **Video** — `GStreamer Fullscreen` | managed, fullscreen | yes | `render_direct.py` |
| 2 | **wfb-ng stats** — `wfb-ng stats` | managed, `keep_above` | transparent | `wfb_osd.py` (`wfb_srv_osd`) |
| 3 | **OSD** — msposd telemetry | `override_redirect`, fullscreen | transparent | `msposd` (`Render_gs.c`) |
| 4 | **Map** — `msposd-map-*` | managed, focusable | yes | `gs/mapwin` (shown on demand) |

## Two states

**Map hidden (normal):** `video < wfb-stats < OSD`.
The video is fullscreen and covers the taskbar. The stats and OSD are transparent,
so both composite over the video. You see the video with the wfb stats (top-left)
and the OSD telemetry drawn on top.

```
   OSD        (transparent, on top — idx highest)
   wfb-stats  (transparent, above the video)
   video      (opaque, fullscreen — hides the taskbar)
```

**Map shown:** `video < OSD < map`, taskbar visible.
`render_direct.py` drops the video out of fullscreen (to a normal full-size
window, so the taskbar reappears) and restacks the OSD **just below the map**, so
the opaque map sits on top with the OSD/video behind it.

## Who controls the order

- **`render_direct.py` → `_restack()` (400 ms timer)** is the single authority.
  Map hidden: fullscreen + raise the video, then raise the OSD just above it.
  Map shown: unfullscreen the video, restack the OSD just below the map.
- **`msposd`** self-raises its OSD each frame, gated by the `/tmp/msposd-no-raise`
  flag. **`gs/map.sh`** creates that flag while the map is shown so msposd backs
  off and lets `render_direct.py` place the OSD below the map.
- **wfb-ng stats** only uses `set_keep_above(True)` + `present()`. That lifts it
  above the (managed) video but **not** above the OSD.

## Key window-manager facts

- A managed window **cannot** be stacked above an `override_redirect` window on
  this WM. So the map cannot raise itself above the OSD — instead the OSD is
  lowered below the map.
- The wfb-ng stats window looks like it is "on top of the OSD" but it is not: the
  OSD is above it (measured idx 125 vs 14). Because the OSD is transparent where
  it paints no telemetry, the stats below simply show through it.
- Covering the taskbar requires the fullscreen video (above the dock layer); a
  focusable map cannot sit above a fullscreen window, so while the map is shown
  the video is dropped from fullscreen and the taskbar is visible.
