"""On-screen rendering for StreamStats.

Kept out of stream_stats.py so that module stays measurement-only and importable
without GTK or a display (the headless test harness depends on that).

The host window owns the cairo context and the outlined() text helper; this
module only lays out a published snapshot. It reads StreamStats.latest, which
tick() replaces wholesale once a second -- a single attribute read of an
immutable dict, so it is safe to call from wfb_srv_osd's background GTK thread
without taking any lock.

Everything is formatted to a fixed width so fields never shift position as
values change; a line that jiggles is unreadable in flight.
"""

from gi.repository import cairo

# Matches the palette the surrounding OSD already uses.
SEV_RGB = {
    0: (0.75, 1.00, 0.75),   # ok      - green
    1: (1.00, 1.00, 0.45),   # warn    - yellow
    2: (1.00, 0.45, 0.45),   # bad     - red
}

FONT = "DejaVu Sans Mono"

# Jitter graph: one frame per pixel across, one millisecond per pixel down, so
# the height fixes the visible range at +/- GRAPH_H/2 ms. Values beyond that
# are clamped to the edge and drawn red, so a clipped sample never reads as an
# in-range one.
GRAPH_W = 200
GRAPH_H = 30           # +/- 15 ms
GRAPH_GAP = 6          # vertical space between the last text line and the plot

# The three detail-mode text lines (nom/clk/pk, the D percentiles, jb/tot).
# The jitter graph conveys most of the same information more directly, so they
# are off by default. Set True here, or pass detail_text=True, to bring them
# back -- the numbers are still computed and still printed to the console
# either way, this only controls what is drawn on screen.
SHOW_DETAIL_TEXT = False

# Jitter chart style. True (the default) fills the whole bar from the axis to
# the value, giving a solid envelope. False draws only a short stub at the tip
# -- GRAPH_STUB pixels of the bar, measured back towards the axis -- so the
# trace reads as a connected line while leaving the middle of the plot open.
# Set here, or pass graph_bars= per call.
GRAPH_BARS = True
GRAPH_STUB = 5          # stub length in pixels, used when GRAPH_BARS is False

# Receiver-side delay (rx / sck / jb / dec) is not drawn. It measures what it
# claims, but it is a few tenths of a millisecond and is not where the latency
# fault lives -- see latency_meter_tests.md. Kept here rather than deleted so it
# can be turned back on; stream_stats.ENABLE_LATENCY must be True as well, or
# the fields are never published in the first place.
SHOW_LATENCY = False


def _c3(v):
    """Count in exactly 3 columns. Fixed width matters more than the exact
    value once it is large enough to be alarming anyway."""
    v = int(v)
    if v < 0:
        return "  0"
    if v > 999:
        return "1k+"
    return f"{v:3d}"


def _k(v):
    """Compact total: 426, 1.2k, 15k, 1.2M."""
    v = int(v)
    if v < 1000:
        return str(v)
    if v < 10000:
        return f"{v / 1000.0:.1f}k"
    if v < 1000000:
        return f"{v // 1000}k"
    return f"{v / 1000000.0:.1f}M"


def _ms(v):
    """Millisecond value clamped to 5 columns."""
    return f"{min(float(v), 99.9):5.1f}"


def _ms4(v):
    """Whole milliseconds in exactly 4 columns, or '  --' when unmeasured.

    Whole ms rather than one decimal because this renders receiver-side delay,
    which is a fraction of a millisecond when healthy but must still fit a
    four-digit column when a stall backs the pipeline up.
    """
    if v is None:
        return "  --"
    return f"{min(float(v), 9999.0):4.0f}"


def _draw_graph(cr, series, x, top, t_ms, keys=None, bars=None,
                w=GRAPH_W, h=GRAPH_H):
    """Per-frame arrival deviation D, one pixel column per frame.

    Positive D (frame arrived later than the sender's cadence predicts) goes
    above the axis, negative below -- the same sign convention as the D p50/p95
    figures printed on the line above.

    `keys` is the aligned keyframe flag series; each true entry gets a small
    white arrow along the bottom edge, so I-frame bandwidth spikes can be lined
    up against the jitter they cause.

    `bars` selects the style: False draws a GRAPH_STUB-pixel stub at each
    sample's value, True fills from the axis to it. Defaults to GRAPH_BARS.

    Colour bands come from the frame period, not from the graph's own scale, so
    changing GRAPH_H changes the visible range without silently reclassifying
    samples. They match the thresholds used for the J field: green below
    0.25*T, yellow below 0.75*T, red at or beyond it (or clamped to the edge).
    """
    half = h / 2.0
    axis = top + half
    warn, bad = 0.25 * t_ms, 0.75 * t_ms

    # Slightly darker plate so the plot area reads as a chart, not as text.
    cr.set_source_rgba(0, 0, 0, 0.35)
    cr.rectangle(x, top, w, h)
    cr.fill()

    cr.set_source_rgba(1, 1, 1, 0.35)
    cr.set_line_width(1)
    cr.move_to(x, axis + 0.5)
    cr.line_to(x + w, axis + 0.5)
    cr.stroke()

    if bars is None:
        bars = GRAPH_BARS

    if not series:
        return
    # Grouped by severity so the whole plot takes three fills rather than one
    # per sample.
    buckets = ([], [], [])
    for i, d in enumerate(series[-w:]):
        if d is None:               # frame we could not time -- leave a blank
            continue
        v = max(-half, min(half, d))
        if abs(d) > half or abs(d) >= bad:
            sev = 2
        elif abs(d) >= warn:
            sev = 1
        else:
            sev = 0
        if bars:
            # Fill from the axis to the value; height >= 1 so a near-zero
            # sample is still a visible pixel on the axis.
            buckets[sev].append((x + i, axis - max(v, 0.0), 1, max(1.0, abs(v))))
        else:
            # The last GRAPH_STUB pixels of the bar, anchored at the tip and
            # measured back towards the axis. Never longer than the bar itself,
            # so it cannot reach past the plot edges.
            tip = axis - v
            seg = max(1.0, min(abs(v), float(GRAPH_STUB)))
            buckets[sev].append(
                (x + i, tip if v >= 0 else tip - seg, 1, seg))
    for sev, rects in enumerate(buckets):
        if not rects:
            continue
        cr.set_source_rgb(*SEV_RGB[sev])
        for r in rects:
            cr.rectangle(*r)
        cr.fill()

    if keys:
        # A small upward arrow per keyframe: three columns 2/4/2 px tall rising
        # from the bottom edge. Wider than a single line reads far better
        # against the horizontal noise of the trace. Drawn last so a marker is
        # never buried under a bar, and only 4 px tall, so it collides with a
        # sample only at the very bottom of the scale.
        cr.set_source_rgb(1, 1, 1)
        base = top + h
        for i, k in enumerate(keys[-w:]):
            if not k:
                continue
            for dx, tick in ((-1, 2), (0, 4), (1, 2)):
                col = x + i + dx
                if x <= col < x + w:        # keep the arrow inside the plot
                    cr.rectangle(col, base - tick, 1, tick)
        cr.fill()


def _row(cr, outlined, segments, x, y):
    """Draw (text, severity) segments left to right from x."""
    for text, sev in segments:
        cr.set_source_rgb(*SEV_RGB.get(sev, SEV_RGB[0]))
        outlined(cr, text, x, y)
        x += cr.text_extents(text).x_advance


def _compact_line(snap):
    if snap['frozen'] is not None:
        # Nothing is arriving, so jitter and stutter are frozen too and would
        # only mislead.
        segs = [
            (f"{min(snap['fps'], 999.9):5.1f}fps", 2),
            (f" FROZEN{min(snap['frozen'], 999.9):5.1f}s", 2),
        ]
    else:
        segs = [
            (f"{min(snap['fps'], 999.9):5.1f}fps", snap['fps_sev']),
            (f" J{_ms(snap['jit'])}", snap['jit_sev']),
            (f" st{min(snap['stut'], 100.0):3.0f}%", snap['stut_sev']),
        ]
        # End-to-end delay earns permanent space rather than appearing only
        # when bad: unlike miss/dup it is a level, not an event, and reading it
        # means comparing it against what it was a minute ago.
        # What this receiver is holding. Not end-to-end latency -- see
        # latency_meter_tests.md -- so it is labelled rx, and `clk` in detail
        # mode is what flags a sender whose timeline has stopped tracking
        # reality.
        if SHOW_LATENCY and snap.get('lat') is not None:
            segs.append((f" rx{_ms4(snap['lat'])}", snap['lat_sev']))
    # miss/dup appear only when non-zero -- their presence is itself the alarm,
    # and the box shrinks back once the fault ages out of the 10 s window.
    if snap['miss']:
        segs.append((f" miss{_c3(snap['miss'])}", snap['miss_sev']))
    if snap['dup']:
        segs.append((f" dup{_c3(snap['dup'])}", snap['dup_sev']))
    return segs


def _detail_lines(snap):
    clk = f"{snap['clk']:.3f}" if snap['clk'] is not None else " --  "
    jb = snap['jb']
    tot = snap['tot']
    # What the compact line's `rx` is made of. Appended to the first row rather
    # than given a row of its own so the detail block keeps the height
    # draw_stats() documents, and only when that figure is being shown at all.
    rx = []
    if SHOW_LATENCY:
        rx = [
            (f" sck{_ms4(snap.get('lat_sock'))}", 0),
            (f" jb{_ms4(snap.get('lat_jb'))}", 0),
            (f" dec{_ms4(snap.get('lat_dec'))}", 0),
        ]
    return [
        [
            (f"nom{min(snap['nom'], 999.9):5.1f} ", 0),
            (f"clk {clk}", snap['clk_sev']),
            (f" pk{_ms(snap['jit_peak'])}", 0),
        ] + rx,
        [
            (f"D{snap['d50']:+5.1f}/{snap['d95']:+5.1f}/{snap['d05']:+5.1f}", 0),
            (f" gap{_c3(snap['gap'])}", 0),
            (f" reo{_c3(snap['reorder'])}", 1 if snap['reorder'] else 0),
            (f" tny{_c3(snap['tiny'])}", 0),
        ],
        [
            (f"jb L{_k(jb.get('lost', 0))} la{_k(jb.get('late', 0))}"
             f" d{_k(jb.get('dup', 0))}", 0),
            (f" tot f{_k(tot['frames'])} m{_k(tot['miss'])} d{_k(tot['dup'])}"
             f" z{_k(tot['freezes'])} r{_k(tot['restarts'])}", 0),
        ],
    ]


def draw_stats(cr, outlined, snap, x=6, y=190, mode=1, font_size=15,
               line_h=17, bg_alpha=0.4, pad=5, detail_text=None,
               graph_bars=None):
    """Render the stats block over a semi-transparent backing rectangle.

    mode 0 hidden, 1 compact (one line), 2 detail (adds three lines).
    `outlined` is the host window's bound method of the same name.

    The default y clears six paired antenna rows (3 adapters x 2 antennas):
    wfb_srv_osd puts the last row's baseline at 22*5 + 46 + 8 = 164, its glyphs
    reaching ~169, and its pair separator at 170. The box top sits at
    y - font_size - pad + 1, so y must be at least 190 for it not to cover that
    row. Detail mode ends at 230 with SHOW_DETAIL_TEXT off and 281 with it on,
    which is why both OSD windows are 300 px tall rather than 260. Both call
    sites rely on these defaults, so this is the one place to adjust if the row
    count grows again.
    """
    if not snap or mode <= 0:
        return

    if detail_text is None:
        detail_text = SHOW_DETAIL_TEXT

    rows = [(font_size, _compact_line(snap))]
    if mode >= 2 and detail_text:
        rows.extend((font_size - 2, segs) for segs in _detail_lines(snap))

    cr.select_font_face(FONT, cairo.FontSlant.NORMAL, cairo.FontWeight.NORMAL)

    # Measure before drawing so the box is sized to the text rather than to a
    # guess; fixed-width formatting keeps it from resizing between ticks.
    widths = []
    for size, segs in rows:
        cr.set_font_size(size)
        widths.append(sum(cr.text_extents(t).x_advance for t, _ in segs))

    last_baseline = y + (len(rows) - 1) * line_h
    graph = snap.get('d_series') if mode >= 2 else None
    graph_top = last_baseline + GRAPH_GAP

    # The +1/-1 trims a pixel off each end of the box without moving the text.
    top = y - font_size - pad + 1
    bottom = last_baseline + pad + 4
    box_w = max(widths)
    if graph is not None:
        bottom = graph_top + GRAPH_H + pad - 1
        box_w = max(box_w, GRAPH_W)
    cr.set_source_rgba(0, 0, 0, bg_alpha)
    cr.rectangle(x - pad, top, box_w + 2 * pad, bottom - top)
    cr.fill()

    for i, (size, segs) in enumerate(rows):
        cr.set_font_size(size)
        _row(cr, outlined, segs, x, y + i * line_h)

    if graph is not None:
        nom = snap['nom']
        _draw_graph(cr, graph, x, graph_top, 1000.0 / nom if nom else 20.0,
                    snap.get('key_series'), graph_bars)
