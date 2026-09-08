"""Stream cadence instrumentation for render_direct.py.

Answers two questions at runtime: what is the *real* decoded frame rate, and is
the arrival cadence jittery / gappy (a frame that shows up late, early, or not
at all).

Two measurement sources, both cheap enough to leave running:

  C) The rtpjitterbuffer's own bookkeeping -- its `stats` property polled once a
     second from the main loop (num-pushed / num-lost / num-late /
     num-duplicates / avg-jitter), plus an event probe that catches the custom
     GstRTPPacketLost event the jitterbuffer emits the moment it gives up on a
     packet.  Costs nothing per buffer.

  A) One BUFFER probe on the depayloader src pad.  The depay emits exactly one
     buffer per access unit, i.e. one call per frame (~30-120/s), and each
     buffer carries both clocks we need:

         arrival = Gst.util_get_timestamp()   monotonic wall clock
         sender  = buf.pts                    derived from the RTP timestamp,
                                              already unwrapped to 64 bit by the
                                              jitterbuffer (no 16-bit wrap to
                                              handle here)

     Real FPS comes from the arrival clock.  The per-frame deviation
     D = dt_arrival - dt_sender is the jitter: D > 0 late, D < 0 early (early
     is almost always bunching right after a stall).  A dt_sender much larger
     than the nominal period means the *camera* skipped, which is a different
     fault from the link dropping frames.

Caveat: the probe sits *after* the rtpjitterbuffer, which absorbs up to its
`latency` ms of jitter.  These numbers describe what the decoder and the display
actually experienced -- the right thing for FPS and freezes -- but they
understate raw over-the-air jitter.  Seeing that needs a probe on the udpsrc src
pad, before the jitterbuffer, at the cost of a Python callback per RTP packet.

Threading: the probes run on the GStreamer streaming thread and do nothing but
arithmetic under a short lock.  All formatting, percentiles, freeze detection
and file I/O happen in tick(), on the GLib main loop.
"""

import threading
from collections import deque

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

# A dt_arrival above GAP_FACTOR * nominal period counts as a gap.
GAP_FACTOR = 1.5
# No frame for this long is a freeze, reported separately from gaps.
FREEZE_NS = 250 * Gst.MSECOND
# Percentiles are reported over this much recent history. Trimmed by time, so
# the figures stay meaningful whatever the frame rate; WINDOW is only the hard
# memory bound (~10 s at 60 fps).
PCT_WINDOW_NS = 5 * Gst.SECOND
WINDOW = 600
# Cap on undrained CSV rows, so a stalled main loop cannot eat memory.
CSV_BACKLOG_MAX = 5000
# On-screen defect counts are reported over this many ticks (seconds). A
# per-second count blinks back to zero before you can read it in flight.
ROLL_TICKS = 10
# A frame whose arrival interval is off by more than this fraction of the
# nominal period is a visible stutter.
STUTTER_FACTOR = 0.5
# Jitter peak-hold depth, in ticks.
PEAK_TICKS = 30
# Frames smaller than this fraction of the median count as encoder filler.
TINY_FRAME_FRACTION = 0.05
# Per-frame D series published for the on-screen jitter graph: one entry per
# frame, one pixel per entry. Kept separate from `window` because that one is
# trimmed by time (5 s), which at 30 fps would not hold 200 frames.
GRAPH_FRAMES = 200
# Setting do-lost on the jitterbuffer makes it emit GstRTPPacketLost, which is
# the only way to get precise loss timestamps (the `evt` counter). It is off by
# default because it is the one thing here that changes what the pipeline
# *does* rather than just observing it: with it on, rtph265depay discards a
# partial access unit on loss instead of assembling what arrived. The
# jitterbuffer's own num-lost already gives the loss count, so `evt` is
# redundant -- not worth perturbing the video path for.
ENABLE_DO_LOST = False

_JB_FIELDS = ('num-pushed', 'num-lost', 'num-late', 'num-duplicates',
              'avg-jitter', 'rtx-count', 'rtx-success-count')


def _find_element(pipeline, suffix):
    """First element in the pipeline whose *factory* name ends with `suffix`.

    Matching on the factory rather than get_by_name() keeps this working across
    the several SRC variants in render_direct.py, only one of which names its
    jitterbuffer.
    """
    it = pipeline.iterate_elements()
    while True:
        result, element = it.next()
        if result == Gst.IteratorResult.RESYNC:
            it.resync()
            continue
        if result != Gst.IteratorResult.OK:
            return None
        factory = element.get_factory()
        if factory and factory.get_name().endswith(suffix):
            return element


def _sget(structure, name):
    """Read a field from a GstStructure, or None if this version lacks it."""
    if structure is None or not structure.has_field(name):
        return None
    try:
        return structure.get_value(name)
    except Exception:
        return None


def _pct(values, p):
    """Percentile of an unsorted list, nearest-rank. Empty list -> 0.0."""
    if not values:
        return 0.0
    s = sorted(values)
    k = int(round((p / 100.0) * (len(s) - 1)))
    return s[min(len(s) - 1, max(0, k))]


def _median(values):
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _sev(value, warn, bad):
    """0 ok / 1 warn / 2 bad."""
    if bad and value >= bad:
        return 2
    if warn and value >= warn:
        return 1
    return 0


class StreamStats:

    def __init__(self, csv_path=None):
        self._lock = threading.Lock()
        self._probes = []          # (pad, probe_id)
        self._jb = None            # rtpjitterbuffer element, if the SRC has one
        self._jb_prev = None       # previous stats snapshot, for per-second deltas
        self._jb_dumped = False
        self._csv = None
        self._csv_rows = []
        self.restarts = 0

        if csv_path:
            try:
                self._csv = open(csv_path, 'w', buffering=1 << 16)
                self._csv.write('t_ms,dt_arrival_us,dt_sender_us,d_us,discont\n')
                print(f"stats: logging per-frame rows to {csv_path}")
            except OSError as e:
                print(f"stats: cannot open {csv_path}: {e}")

        self._clear()
        self._last_tick = Gst.util_get_timestamp()

    # -- lifecycle -----------------------------------------------------------

    def _clear(self):
        """Zero everything except `restarts`, which must survive a restart --
        a pipeline that restarts every two seconds otherwise reads as a healthy
        30 fps."""
        self.window = deque(maxlen=WINDOW)
        self.d_series = deque(maxlen=GRAPH_FRAMES)   # ms, None where undefined
        self.key_series = deque(maxlen=GRAPH_FRAMES) # keyframe flag, aligned
        self.keyframes = 0
        self._last_arrival = None
        self._last_pts = None
        self._T = None             # nominal frame period in ns, set by tick()
        self._tiny_thresh = None   # frame-size floor, set by tick()
        self.jitter_ns = 0.0       # RFC 3550 interarrival jitter
        self.frames = 0
        self.gaps = 0
        self.gap_slots = 0
        self.lost_frames = 0
        self.cam_stalls = 0
        self.dup_pts = 0           # consecutive buffers sharing one PTS
        self.reorder = 0           # PTS went backwards
        self.tiny_frames = 0       # suspected encoder filler (heuristic)
        self.discont = 0
        self.no_pts = 0
        self.lost_events = 0       # GstRTPPacketLost
        self._prev_evt = 0
        self.gap_events = 0        # GST_EVENT_GAP
        self.qos_dropped = 0
        self.jb_dup_total = 0      # accumulated jitterbuffer num-duplicates
        self.freezes = 0
        self._freeze_start = None
        self._prev = {}            # counter snapshot at the last tick
        self._history = deque(maxlen=ROLL_TICKS + 1)   # per-tick snapshots
        self._j_hist = deque(maxlen=PEAK_TICKS)        # jitter peak-hold
        self.latest = None         # published snapshot, read by the OSD

    def reset(self):
        """Called on pipeline restart."""
        with self._lock:
            self.restarts += 1
            self._clear()
            self._jb_prev = None
        self._last_tick = Gst.util_get_timestamp()

    def attach(self, pipeline):
        self.detach()
        depay = _find_element(pipeline, 'depay')
        self._jb = _find_element(pipeline, 'rtpjitterbuffer')

        if self._jb is not None and ENABLE_DO_LOST:
            # Off by default: see the ENABLE_DO_LOST comment above. Without it
            # the jitterbuffer never emits GstRTPPacketLost, so the event probe
            # below stays installed but only ever sees GAP events, and the
            # `evt` counter reads 0.
            try:
                if not self._jb.get_property('do-lost'):
                    self._jb.set_property('do-lost', True)
            except Exception as e:
                print(f"stats: could not enable do-lost: {e}")

        if depay is not None:
            src = depay.get_static_pad('src')
            if src is not None:
                self._probes.append(
                    (src, src.add_probe(Gst.PadProbeType.BUFFER, self._on_buffer)))
            sink = depay.get_static_pad('sink')
            if sink is not None:
                self._probes.append(
                    (sink, sink.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM,
                                          self._on_event)))
        else:
            print("stats: no depayloader found -- FPS/jitter disabled")
        if self._jb is None:
            print("stats: no rtpjitterbuffer in the pipeline -- loss counters disabled")

    def detach(self):
        for pad, pid in self._probes:
            try:
                pad.remove_probe(pid)
            except Exception:
                pass
        self._probes = []
        self._jb = None

    def close(self):
        self.detach()
        if self._csv:
            try:
                self._csv.close()
            except Exception:
                pass
            self._csv = None

    # -- streaming thread ----------------------------------------------------

    def _on_buffer(self, pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK

        now = Gst.util_get_timestamp()
        pts = buf.pts
        has_pts = pts != Gst.CLOCK_TIME_NONE
        discont = buf.has_flags(Gst.BufferFlags.DISCONT)
        # GStreamer marks predicted frames DELTA_UNIT, so its absence is a
        # keyframe. rtph265depay sets this from the IRAP NAL types, which is
        # both cheaper and more reliable than parsing NALs or guessing from
        # frame size (rate control flattens I-frames to ~1.6x a P-frame).
        keyframe = not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
        size = buf.get_size()

        with self._lock:
            self.frames += 1
            if keyframe:
                self.keyframes += 1
            if discont:
                self.discont += 1
            if not has_pts:
                self.no_pts += 1
            # Heuristic: an access unit a fraction of the median size is almost
            # always an all-skip P-frame, i.e. the encoder repeating the picture
            # rather than coding a new one.
            if self._tiny_thresh and size < self._tiny_thresh:
                self.tiny_frames += 1

            prev_arrival, prev_pts = self._last_arrival, self._last_pts
            self._last_arrival = now
            self._last_pts = pts if has_pts else None

            if prev_arrival is None:
                return Gst.PadProbeReturn.OK

            dt_a = now - prev_arrival
            # dt_sender is only meaningful when the PTS advanced. Two buffers
            # sharing a PTS are a duplicate delivery (or a multi-AU timestamp);
            # a backwards PTS is a reorder or an encoder reset. Both would give
            # a nonsense D, so they are counted and excluded rather than folded
            # into the jitter figures.
            dt_s = None
            if has_pts and prev_pts is not None:
                if pts > prev_pts:
                    dt_s = pts - prev_pts
                elif pts == prev_pts:
                    self.dup_pts += 1
                else:
                    self.reorder += 1
            d = (dt_a - dt_s) if dt_s is not None else None

            if d is not None:
                self.jitter_ns += (abs(d) - self.jitter_ns) / 16.0

            # Gap classification needs the nominal period, which tick() supplies
            # incrementally -- counting it here rather than over the rolling
            # window avoids double-counting the same frame across ticks.
            if self._T:
                if dt_a > GAP_FACTOR * self._T:
                    self.gaps += 1
                    self.gap_slots += max(0, int(round(dt_a / float(self._T))) - 1)
                if dt_s is not None and dt_s > GAP_FACTOR * self._T:
                    # The sender's clock skipped a slot. That happens both when
                    # a frame is lost in transit and when the camera genuinely
                    # produced nothing, and dt_sender alone cannot tell them
                    # apart -- a lost frame moves both clocks together. DISCONT,
                    # which the depay sets after the jitterbuffer gives up on a
                    # packet, is the discriminator.
                    if discont:
                        self.lost_frames += 1
                    else:
                        self.cam_stalls += 1

            self.window.append((now, dt_a, dt_s, d, size))
            # One entry per frame, including the undefined ones -- a blank
            # column in the graph is honest about a frame we could not time.
            # Appended together with key_series so the two stay index-aligned
            # for the renderer (the very first frame exits above, so its
            # keyframe marker is the one that never reaches the chart).
            self.d_series.append(None if d is None else d / 1e6)
            self.key_series.append(keyframe)

            if self._csv is not None and len(self._csv_rows) < CSV_BACKLOG_MAX:
                self._csv_rows.append((now, dt_a, dt_s, d, discont))

        return Gst.PadProbeReturn.OK

    def _on_event(self, pad, info):
        event = info.get_event()
        if event is None:
            return Gst.PadProbeReturn.OK
        if event.type == Gst.EventType.GAP:
            with self._lock:
                self.gap_events += 1
        elif event.type == Gst.EventType.CUSTOM_DOWNSTREAM:
            st = event.get_structure()
            if st is not None and st.get_name() == 'GstRTPPacketLost':
                with self._lock:
                    self.lost_events += 1
        return Gst.PadProbeReturn.OK

    # -- main loop -----------------------------------------------------------

    def note_qos(self, message):
        """Record a QoS report from the bus. vaapih265dec is the only thing that
        will tell us it discarded frames, and this is how it does it."""
        try:
            _fmt, _processed, dropped = message.parse_qos_stats()
        except Exception:
            return
        if dropped and dropped != 0xFFFFFFFFFFFFFFFF:
            with self._lock:
                # Each element reports its own running total; keep the largest so
                # two reporters cannot drive the per-second delta negative.
                self.qos_dropped = max(self.qos_dropped, dropped)

    def tick(self):
        now = Gst.util_get_timestamp()
        elapsed = now - self._last_tick
        self._last_tick = now
        jb = self._jb_read()          # touches GStreamer, so outside the lock

        with self._lock:
            cutoff = now - PCT_WINDOW_NS
            while self.window and self.window[0][0] < cutoff:
                self.window.popleft()
            window = list(self.window)
            d_series = list(self.d_series)
            key_series = list(self.key_series)
            rows, self._csv_rows = self._csv_rows, []
            self.jb_dup_total += jb.get('dup', 0)

            # Nominal period: median of the *sender* deltas, robust to outliers.
            # RTP caps usually declare framerate 0/1, so the caps are no help.
            sender = [s for _t, _a, s, _d, _z in window if s]
            T = _median(sender) or _median([a for _t, a, _s, _d, _z in window])
            if T:
                self._T = T
            med_size = _median([z for _t, _a, _s, _d, z in window if z])
            if med_size:
                self._tiny_thresh = med_size * TINY_FRAME_FRACTION

            cur = dict(frames=self.frames, gaps=self.gaps, gap_slots=self.gap_slots,
                       lost_frames=self.lost_frames, cam_stalls=self.cam_stalls,
                       discont=self.discont, lost_events=self.lost_events,
                       gap_events=self.gap_events, qos_dropped=self.qos_dropped,
                       dup_pts=self.dup_pts, reorder=self.reorder,
                       tiny=self.tiny_frames, jb_dup=self.jb_dup_total,
                       keyframes=self.keyframes)
            prev, self._prev = self._prev, cur
            self._history.append(cur)
            oldest = self._history[0]
            jitter_ms = self.jitter_ns / 1e6
            self._j_hist.append(jitter_ms)
            peak_j = max(self._j_hist)
            last_arrival = self._last_arrival
            totals = dict(frames=self.frames, restarts=self.restarts,
                          miss=self.lost_frames + self.cam_stalls,
                          dup=self.dup_pts + self.jb_dup_total,
                          freezes=self.freezes)

            # Freeze detection lives here, not in the probe: when nothing
            # arrives the probe never runs.
            freeze_msg = None
            frozen_for = None
            if last_arrival is not None:
                if self._freeze_start is None:
                    if now - last_arrival > FREEZE_NS:
                        self._freeze_start = last_arrival
                        frozen_for = now - last_arrival
                elif last_arrival > self._freeze_start:
                    self.freezes += 1
                    freeze_msg = (last_arrival - self._freeze_start) / 1e6
                    self._freeze_start = None
                else:
                    frozen_for = now - self._freeze_start

        self._flush_csv(rows)

        if freeze_msg is not None:
            print(f"[stats] FREEZE {freeze_msg:.0f} ms -- recovered")

        if not prev:
            return True  # first tick only establishes the baseline

        snap = self._build(now, elapsed, window, T, cur, prev, oldest, totals,
                           jb, jitter_ms, peak_j, frozen_for, d_series,
                           key_series)
        self.latest = snap            # published for the OSD; never mutated
        print("[stats] " + self._console_line(snap))
        return True

    def _build(self, now, elapsed, window, T, cur, prev, oldest, totals, jb,
               jitter_ms, peak_j, frozen_for, d_series, key_series):
        """Assemble the published snapshot: values plus a severity per field.

        Thresholds live here and nowhere else, so the renderer stays dumb and
        console and screen can never disagree.
        """
        def delta(k):
            return cur[k] - prev.get(k, 0)

        def roll(k):
            return cur[k] - oldest.get(k, 0)

        secs = elapsed / float(Gst.SECOND) or 1.0
        fps = delta('frames') / secs
        T_ms = (T / 1e6) if T else 0.0
        nominal = (Gst.SECOND / float(T)) if T else 0.0

        # Sender clock vs wall clock. 1.00 means the transmitter's timestamps
        # track reality; a sustained value below 1 with no loss means they do
        # not, and the declared frame rate is fiction.
        sum_a = sum(a for _t, a, s, _d, _z in window if s)
        sum_s = sum(s for _t, _a, s, _d, _z in window if s)
        clk = (sum_s / float(sum_a)) if sum_a else None

        # Visible stutter: intervals that are off by more than half a period.
        stut = 0.0
        if T and window:
            off = sum(1 for _t, a, _s, _d, _z in window
                      if abs(a - T) > STUTTER_FACTOR * T)
            stut = 100.0 * off / len(window)

        d_ms = [d / 1e6 for _t, _a, _s, d, _z in window if d is not None]
        miss10 = roll('lost_frames') + roll('cam_stalls')
        dup10 = roll('dup_pts') + roll('jb_dup')

        snap = {
            'fps': fps,
            'fps_sev': _sev(abs(fps - nominal) / nominal, 0.05, 0.15) if nominal else 0,
            'nom': nominal,
            'jit': jitter_ms,
            'jit_sev': _sev(jitter_ms, 0.25 * T_ms, 0.75 * T_ms) if T_ms else 0,
            'jit_peak': peak_j,
            'stut': stut,
            'stut_sev': _sev(stut, 2.0, 10.0),
            'miss': miss10,
            'miss_sev': _sev(miss10, 1, 6),
            'dup': dup10,
            'dup_sev': _sev(dup10, 1, 6),
            'clk': clk,
            'clk_sev': 0 if clk is None or 0.98 <= clk <= 1.02
                       else (1 if 0.95 <= clk <= 1.05 else 2),
            'd50': _pct(d_ms, 50), 'd95': _pct(d_ms, 95), 'd05': _pct(d_ms, 5),
            'gap': delta('gaps'), 'gap_slots': delta('gap_slots'),
            'lost_frame': delta('lost_frames'), 'cam_stall': delta('cam_stalls'),
            'reorder': roll('reorder'), 'tiny': roll('tiny'),
            'dup_pts': roll('dup_pts'), 'jb_dup': roll('jb_dup'),
            'qos_drop': delta('qos_dropped'),
            'jb': jb,
            'evt': cur['lost_events'] - prev.get('lost_events', 0),
            'frozen': (frozen_for / 1e9) if frozen_for is not None else None,
            'tot': totals,
            'd_series': d_series,
            'key_series': key_series,
            # Keyframes per 10 s. Zero after startup means the encoder is using
            # intra-refresh or an effectively infinite GOP -- there are simply
            # no I-frames to mark, which is common on FPV links.
            'key': roll('keyframes'),
        }
        snap['sev'] = max(snap['fps_sev'], snap['jit_sev'], snap['stut_sev'],
                          snap['miss_sev'], snap['dup_sev'], snap['clk_sev'],
                          2 if snap['frozen'] is not None else 0)
        return snap

    @staticmethod
    def _console_line(s):
        jb = s['jb']
        jb_txt = ""
        if jb:
            jb_txt = (f"jb lost {jb.get('lost', 0)} late {jb.get('late', 0)} "
                      f"dup {jb.get('dup', 0)} jb-J {jb.get('jitter_ms', 0):.2f}ms "
                      f"evt {s['evt']}")
        clk = f"{s['clk']:.3f}" if s['clk'] is not None else " -- "
        parts = [
            f"fps {s['fps']:5.1f} (nom {s['nom']:4.1f})",
            f"clk {clk}",
            f"J {s['jit']:5.2f}ms pk {s['jit_peak']:5.2f}",
            f"stut {s['stut']:4.1f}%",
            f"D p50 {s['d50']:+6.1f} p95 {s['d95']:+6.1f} p05 {s['d05']:+6.1f}",
            f"gap {s['gap']} (slots {s['gap_slots']}) "
            f"lost-frame {s['lost_frame']} cam-stall {s['cam_stall']}",
            f"dup {s['dup_pts']}/{s['jb_dup']} reord {s['reorder']} tiny {s['tiny']}",
            f"key {s['key']}/10s",
            jb_txt,
            f"qos-drop {s['qos_drop']}",
            f"tot: f {s['tot']['frames']} miss {s['tot']['miss']} "
            f"dup {s['tot']['dup']} frz {s['tot']['freezes']} "
            f"rst {s['tot']['restarts']}",
        ]
        if s['frozen'] is not None:
            parts.append(f"FROZEN {s['frozen']:.1f}s")
        return " | ".join(p for p in parts if p)

    def _jb_read(self):
        """Per-tick deltas from the jitterbuffer's own counters.

        num-lost is what the jitterbuffer gave up on, not raw link loss -- late
        arrivals and rtx shift it.
        """
        if self._jb is None:
            return {}
        try:
            st = self._jb.get_property('stats')
        except Exception:
            return {}
        if st is None:
            return {}
        if not self._jb_dumped:
            # Field names drift between GStreamer versions; record what this one
            # actually offers, once.
            self._jb_dumped = True
            print(f"stats: jitterbuffer stats fields: {st.to_string()}")

        cur = {f: _sget(st, f) for f in _JB_FIELDS}
        prev, self._jb_prev = self._jb_prev, cur
        if prev is None:
            return {}

        out = {}
        for field, label in (('num-lost', 'lost'), ('num-late', 'late'),
                             ('num-duplicates', 'dup'), ('num-pushed', 'pushed')):
            if cur.get(field) is not None and prev.get(field) is not None:
                out[label] = cur[field] - prev[field]
        if cur.get('avg-jitter') is not None:
            out['jitter_ms'] = cur['avg-jitter'] / 1e6
        return out

    def _flush_csv(self, rows):
        if self._csv is None or not rows:
            return
        try:
            self._csv.writelines(
                "%d,%d,%s,%s,%d\n" % (
                    t // 1000000,
                    dt_a // 1000,
                    '' if dt_s is None else dt_s // 1000,
                    '' if d is None else d // 1000,
                    1 if disc else 0)
                for t, dt_a, dt_s, d, disc in rows)
        except Exception as e:
            print(f"stats: csv write failed: {e}")
            self._csv = None
