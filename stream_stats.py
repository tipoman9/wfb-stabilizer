"""Stream cadence instrumentation for render_direct.py.

Answers three questions at runtime: what is the *real* decoded frame rate, is
the arrival cadence jittery / gappy (a frame that shows up late, early, or not
at all), and how much delay this receiver itself is holding (`lat`, source B).

It deliberately does NOT report end-to-end latency; see the section below on why
that is not computable from this stream.

Three measurement sources, all cheap enough to leave running:

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

  B) Receiver-side delay (`lat`), from RTP timestamp *differences* read straight
     off the packets on the rtpjitterbuffer's two pads:

         jb  = (newest RTP ts arrived - RTP ts being released) / 90 kHz
         dec = monotonic time from the depayloader pushing a frame to that
               frame reaching the sink
         lat = jb + dec

     `jb` is how far behind the live edge of the stream the picture being
     handed to the display is: the media time the jitterbuffer is sitting on.
     It is the number that grows when the pipeline takes on backlog, and with
     `sync=false` on the sink and `drop-on-latency=false` on the jitterbuffer
     nothing ever gives that backlog back.

     Why a difference of RTP timestamps and not a PTS.  An earlier version of
     this computed

         clock.get_time() - sink.get_base_time() - to_running_time(buf.pts)

     and that is unsound here, in two independent ways.  It subtracts two
     unrelated epochs -- base_time is the receiver's clock at the moment the
     pipeline reached PLAYING, while the PTS counts media time from an origin
     the jitterbuffer picked out of the RTP stream -- so the result is the true
     delay plus an arbitrary constant, which is why it could read -200 ms on a
     link measured at 100 ms screen to screen.  And `mode=0` runs no skew
     correction, so the sender's and receiver's clock *rates* diverge freely
     and the difference integrates that divergence: 1000 ppm, ordinary for a
     camera oscillator, is 1 ms per second, i.e. 3.6 s per hour of pure
     fiction.  The `clk` field in source A is that very ratio.

     A difference of two RTP timestamps sampled at one instant has neither
     defect.  Both come from the same sender clock, so its rate cancels; both
     are offsets from the same origin, so the origin cancels; and a
     jitterbuffer resync re-origins both sides together, so a discontinuity
     does not shift it.

     What `lat` is not: glass-to-glass.  Camera exposure, encode and air time
     happen before the first byte reaches this machine and cannot be seen from
     here, and the display's own pipeline is after the sink.  Expect `lat` to
     read well below a stopwatch measurement -- roughly the jitterbuffer's
     configured `latency` plus a few ms -- and to be the part that moves when
     latency misbehaves.

WHY THERE IS NO LATENCY FIELD, and why one cannot be added.

An obvious thing to want here is "how old is the picture on screen".  It is not
computable from this stream, and three successive attempts to compute it all
failed in the same underlying way, so the reasoning is recorded rather than
repeated.

Measuring latency needs a time reference shared with the camera.  This stream
carries none: there is no RTCP, so no sender report ever maps an RTP timestamp
to a wall clock, and the two machines' clocks are not synchronised.  That leaves
only the RTP timestamps themselves -- and measurement shows they are a *frame
counter*, not a capture clock:

    RTP ts increment per frame: 1525 ticks = 16.944 ms  x2261  (100.0% of frames)
    frames arriving: 50.2 fps      seq_lost: 0
    media time advanced 38.31 s while wall advanced 45.03 s -> ratio 0.8509

Every frame advances the timestamp by exactly one 59 fps period no matter how
much real time passed.  So when the encoder drops frames -- above, 50.2 fps
emitted against a nominal 59, with zero packet loss, i.e. dropped before
transmission -- the media timeline simply runs slow, here at 0.85x.  Anything of
the form `arrival - rtp_timestamp` then climbs at 149 ms/s while the true
latency does not move at all: about 3 s of pure fiction every 20 s.  Forcing
frame drops was exactly how this was demonstrated.

The same defect sank the earlier attempts, which are worth naming so they are
not tried again:

    clock.get_time() - base_time - to_running_time(pts)
        Subtracts two unrelated epochs (the receiver's PLAYING moment, and an
        origin the jitterbuffer picked out of the RTP stream), so it is the true
        delay plus an arbitrary constant -- it read -200 ms on a link measured
        at 100 ms screen to screen -- and it integrates sender/receiver clock
        rate divergence on top.
    d - min(d) over the run, with a growth rate
        Same arrival-minus-timestamp quantity, so the frame-counter problem
        above applies directly; the all-time minimum also turns any slow drift
        into unbounded accumulation.

What remains, and is sound, is the split in B): the delay *this receiver* is
holding, measured from timestamp differences taken at one instant and from the
monotonic clock, with no cross-clock arithmetic anywhere.  On a healthy link it
reads a few tenths of a millisecond, and it is genuinely near zero -- this
pipeline adds almost nothing.  When the picture is late and `sock`, `jb` and
`dec` are all small, the delay is upstream, and `clk` (source A) is the field
that says so: it is the ratio of media time to arrival time, 1.00 when the
sender's timeline tracks reality and 0.85 in the dropping case above.  It
identifies the fault without pretending to put a millisecond figure on it.

Genuinely measuring end-to-end latency would need a shared reference: RTCP
sender reports plus NTP/PTP on both ends, or a timestamp burnt into the video
by the camera.  Neither exists here.

Caveat: the A) probe sits *after* the rtpjitterbuffer, which absorbs up to its
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
# Source B (the sock/jb/dec delay figures) is off. It measures what it claims --
# unlike the three attempts before it, which is the point of the write-up in
# latency_meter_tests.md -- but what it measures is only the delay this receiver
# holds, which is a few tenths of a millisecond and is not where the latency
# fault lives. Off rather than deleted so it can be turned back on: with this
# False no probes are installed, so there is no per-packet cost and no fields
# are published. stats_overlay.SHOW_LATENCY controls drawing separately.
ENABLE_LATENCY = False
# Receiver-side delay budget: the jitterbuffer's configured `latency` plus this
# many frame periods for depay, decode, convert and render. Delay beyond the
# budget is backlog the pipeline has taken on.
LAT_BUDGET_FRAMES = 2
# Delay in excess of the budget that counts as warn / bad, in ms. Deliberately
# well under one frame period at the low end: an extra 60 ms that does not come
# back is already a real fault, not jitter.
LAT_WARN_MS = 60.0
LAT_BAD_MS = 200.0
# RTP runs at 90 kHz for video, as the SRC caps declare.
RTP_CLOCK_HZ = 90000
# RTP timestamps are 32-bit and wrap every 13.25 hours at 90 kHz; a difference
# above the half-range is really a negative one.
RTP_WRAP = 1 << 32
RTP_HALF = RTP_WRAP >> 1
# Offset of the 32-bit timestamp in the RTP fixed header, and the length of that
# header. Header extensions and CSRCs come after both, so these hold for every
# packet.
RTP_TS_OFFSET = 4
RTP_HDR_BYTES = 12

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


def _find_sink(pipeline):
    """The video sink. Named `video_sink` in every SRC variant in
    render_direct.py, but fall back to the bin's own sink iterator so an unnamed
    or renamed sink still gets instrumented."""
    element = pipeline.get_by_name('video_sink')
    if element is not None:
        return element
    it = pipeline.iterate_sinks()
    while True:
        result, element = it.next()
        if result == Gst.IteratorResult.RESYNC:
            it.resync()
            continue
        if result != Gst.IteratorResult.OK:
            return None
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


def _sock_queue(port):
    """(rx_queue bytes, drops) for the UDP socket bound to `port`, or None.

    The kernel receive buffer is the one place receiver-side backlog can hide
    from a pad probe: packets sitting there have not been read yet, so nothing
    inside the pipeline has seen them. With no queue element in the SRC and the
    jitterbuffer not buffering under mode=0, it is also the only place a full
    second of backlog can actually accumulate -- which is why it is worth the
    once-a-second read of /proc.
    """
    for path in ('/proc/net/udp', '/proc/net/udp6'):
        try:
            with open(path) as fh:
                next(fh, None)          # header row
                for line in fh:
                    f = line.split()
                    if len(f) < 5:
                        continue
                    try:
                        if int(f[1].split(':')[1], 16) != port:
                            continue
                        tx_rx = f[4].split(':')
                        return int(tx_rx[1], 16), int(f[-1])
                    except (ValueError, IndexError):
                        continue
        except OSError:
            continue
    return None


def _rtp_hdr(buf):
    """(ssrc, timestamp) from the RTP fixed header, or None if the buffer is too
    short to hold one.

    The SSRC is not optional bookkeeping here. Measured on a live wfb feed, port
    5600 carries a *second* RTP stream (pt=98, ssrc=1494278274) alongside the
    video (pt=97, ssrc=623207795), and their timestamp origins are unrelated --
    mixing them produced differences of about -9735 s. Every reading is
    therefore confined to one SSRC.

    extract_dup() rather than map(): it copies the twelve bytes wanted instead
    of exposing the whole packet, which matters on a per-packet probe.
    """
    raw = buf.extract_dup(0, RTP_HDR_BYTES)
    if raw is None or len(raw) != RTP_HDR_BYTES:
        return None
    return (int.from_bytes(raw[8:12], 'big'),
            int.from_bytes(raw[RTP_TS_OFFSET:RTP_TS_OFFSET + 4], 'big'))


def _f(value, width=6, prec=1):
    """Fixed-width float, or a right-aligned '--' when there is no value yet."""
    if value is None:
        return "--".rjust(width)
    return f"{value:{width}.{prec}f}"


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
        self._jb_latency_ms = None # the jitterbuffer's configured latency
        self._sink = None          # video sink, for the end-to-end delay probe
        self._udp_port = None      # udpsrc's port, for the socket-queue read
        self._sock_drops0 = None   # drops at attach, so the count is per-run
        self._csv = None
        self._csv_rows = []
        self.restarts = 0

        if csv_path:
            try:
                self._csv = open(csv_path, 'w', buffering=1 << 16)
                self._csv.write(
                    't_ms,dt_arrival_us,dt_sender_us,d_us,discont,lat_us\n')
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
        # End-to-end delay at the sink, in ns. last/min/max describe the current
        # tick (min and max are reset by tick()); the floor is the lowest delay
        # seen since this pipeline started and must survive every tick, since
        # the whole diagnostic is current-versus-best.
        # Receiver-side delay, in ns. See source B in the module docstring.
        self.lat_last = None       # jb + dec for the most recent frame
        self.lat_min = None        # per tick, reset by tick()
        self.lat_max = None
        self.lat_jb = None         # jitterbuffer queue depth, media time
        self.lat_dec = None        # depayloader -> sink transit
        # Newest RTP timestamp to arrive, and the one the jitterbuffer is
        # currently releasing. Their difference is lat_jb; neither is ever used
        # on its own, which is the whole point (no epoch, no clock rate).
        self._rtp_in = None
        self._rtp_out = None
        self._ssrc = None          # the video SSRC, learned from the jb src pad
        self.rtp_alien = 0         # packets on some other SSRC (a second stream)
        self.ssrc_changes = 0      # a new sender, or the jitterbuffer re-locking
        self._depay_push = None    # monotonic time of the last depay push
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
        udpsrc = _find_element(pipeline, 'udpsrc') if ENABLE_LATENCY else None
        if udpsrc is not None:
            try:
                self._udp_port = int(udpsrc.get_property('port'))
                q = _sock_queue(self._udp_port)
                self._sock_drops0 = q[1] if q else None
            except Exception:
                self._udp_port = None
        depay = _find_element(pipeline, 'depay')
        self._jb = _find_element(pipeline, 'rtpjitterbuffer')
        self._sink = _find_sink(pipeline)

        if self._jb is not None:
            # Only read for the delay budget -- a jitterbuffer configured for
            # 100 ms is expected to cost 100 ms, and saying so keeps the `lat`
            # field interpretable without knowing the SRC line by heart.
            try:
                self._jb_latency_ms = float(self._jb.get_property('latency'))
            except Exception:
                self._jb_latency_ms = None

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
            print("stats: no rtpjitterbuffer in the pipeline -- loss counters "
                  "disabled")

        if not ENABLE_LATENCY:
            # Nothing below this point is installed, so the per-packet probes
            # never run. See the ENABLE_LATENCY comment and
            # latency_meter_tests.md.
            return

        if self._sink is not None:
            # The sink pad, not a src pad: this must be the last point in the
            # pipeline, after decode and colorspace conversion, or the delay it
            # reports excludes exactly the stages most likely to stall.
            pad = self._sink.get_static_pad('sink')
            if pad is not None:
                self._probes.append(
                    (pad, pad.add_probe(Gst.PadProbeType.BUFFER,
                                        self._on_sink_buffer)))
        else:
            print("stats: no sink found -- receiver-side delay disabled")

        if self._jb is not None:
            # Both jitterbuffer pads carry raw RTP, so the timestamp can be read
            # straight out of the header. These are the only two probes that run
            # per *packet* rather than per frame; they do nothing but read four
            # bytes and store an int, which is what makes that affordable.
            jbs = self._jb.get_static_pad('sink')
            if jbs is not None:
                self._probes.append(
                    (jbs, jbs.add_probe(Gst.PadProbeType.BUFFER,
                                        self._on_rtp_in)))
            jbo = self._jb.get_static_pad('src')
            if jbo is not None:
                self._probes.append(
                    (jbo, jbo.add_probe(Gst.PadProbeType.BUFFER,
                                        self._on_rtp_out)))
        else:
            print("stats: no rtpjitterbuffer -- receiver-side delay disabled")

    def detach(self):
        for pad, pid in self._probes:
            try:
                pad.remove_probe(pid)
            except Exception:
                pass
        self._probes = []
        self._jb = None
        self._sink = None
        self._udp_port = None
        self._sock_drops0 = None

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
        if ENABLE_LATENCY:
            # Start of the decode+render leg, closed by _on_sink_buffer. Set
            # before anything else so it cannot include this probe's own work.
            self._depay_push = now
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
                # lat_us is the most recent sink-pad reading, i.e. the *previous*
                # frame's -- this probe runs before the sink one for the frame in
                # hand. One frame period of skew is immaterial for a signal whose
                # whole point is that it steps by hundreds of ms and stays there.
                self._csv_rows.append((now, dt_a, dt_s, d, discont,
                                       self.lat_last))

        return Gst.PadProbeReturn.OK

    def _on_rtp_in(self, pad, info):
        """Newest RTP timestamp to arrive from the air, for the video SSRC only.

        Per RTP packet, so it does the least work of any probe here: read the
        header, keep the newest value. Packets can arrive reordered, hence the
        modular comparison rather than a plain `>`.
        """
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        hdr = _rtp_hdr(buf)
        if hdr is None:
            return Gst.PadProbeReturn.OK
        ssrc, ts = hdr
        if ssrc != self._ssrc:
            # Either a second stream sharing the port (see _rtp_hdr) or the
            # video SSRC before the jitterbuffer has emitted anything. Counted
            # so the `alien` figure can say the port is carrying two streams,
            # which is worth knowing: rtpjitterbuffer expects one.
            self.rtp_alien += 1
            return Gst.PadProbeReturn.OK
        cur = self._rtp_in
        if cur is None or 0 < ((ts - cur) % RTP_WRAP) < RTP_HALF:
            self._rtp_in = ts
        return Gst.PadProbeReturn.OK

    def _on_rtp_out(self, pad, info):
        """RTP timestamp the jitterbuffer is releasing right now.

        This pad also defines which SSRC counts as the video: it is whatever the
        jitterbuffer actually forwards to the decoder, so no caps parsing or
        payload-type guesswork is needed. A change of SSRC is a new sender, and
        invalidates the arrival side.
        """
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        hdr = _rtp_hdr(buf)
        if hdr is None:
            return Gst.PadProbeReturn.OK
        ssrc, ts = hdr
        if ssrc != self._ssrc:
            self._ssrc = ssrc
            self._rtp_in = None
            self.ssrc_changes += 1
        self._rtp_out = ts
        return Gst.PadProbeReturn.OK

    def _on_sink_buffer(self, pad, info):
        """Receiver-side delay for the frame now reaching the display.

        lat = jitterbuffer queue depth (media time, from the RTP timestamp
        difference) + the monotonic transit from the depayloader to here. See
        source B in the module docstring for why it is built this way and what
        it does not include.
        """
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK

        rin, rout = self._rtp_in, self._rtp_out
        if rin is None or rout is None:
            return Gst.PadProbeReturn.OK
        # Signed modular difference: normally positive (the newest arrival is
        # ahead of what is being released), but it goes slightly negative right
        # after a resync, and a negative delay is information, not an error.
        d = (rin - rout) % RTP_WRAP
        if d >= RTP_HALF:
            d -= RTP_WRAP
        jb = d * Gst.SECOND // RTP_CLOCK_HZ

        # Lower bound on decode+convert+render: the chain below the jitterbuffer
        # is one thread with no queue in it, so the frame the depayloader pushed
        # last is normally this one. If the decoder holds frames internally this
        # under-reports rather than drifting, which is the safer failure.
        push = self._depay_push
        dec = 0 if push is None else max(0, Gst.util_get_timestamp() - push)

        lat = jb + dec
        with self._lock:
            self.lat_jb, self.lat_dec, self.lat_last = jb, dec, lat
            if self.lat_min is None or lat < self.lat_min:
                self.lat_min = lat
            if self.lat_max is None or lat > self.lat_max:
                self.lat_max = lat

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
        # The socket is not bound until the pipeline is PLAYING, i.e. after
        # attach(), so the drops baseline is taken on the first read that works.
        sockq = (_sock_queue(self._udp_port)
                 if ENABLE_LATENCY and self._udp_port else None)
        if sockq and self._sock_drops0 is None:
            self._sock_drops0 = sockq[1]

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
            # min/max are per-tick, so they are read and cleared together; last
            # and floor persist. A tick with no frames leaves min/max at None,
            # which the formatting renders as '--' rather than as a stale value.
            lat = dict(last=self.lat_last, min=self.lat_min, max=self.lat_max,
                       jb=self.lat_jb, dec=self.lat_dec,
                       alien=self.rtp_alien, ssrc_changes=self.ssrc_changes)
            self.lat_min = self.lat_max = None
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
                           key_series, lat, sockq)
        self.latest = snap            # published for the OSD; never mutated
        print("[stats] " + self._console_line(snap))
        return True

    def _build(self, now, elapsed, window, T, cur, prev, oldest, totals, jb,
               jitter_ms, peak_j, frozen_for, d_series, key_series, lat,
               sockq):
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

        # Receiver-side delay, in ms. Absolute values are meaningful here (see
        # source B), so the budget is a straight comparison: what this pipeline
        # ought to cost is the jitterbuffer's own latency plus a couple of
        # frames of decode and render.
        ms = lambda v: None if v is None else v / 1e6
        lat_ms, lat_min, lat_max = ms(lat['last']), ms(lat['min']), ms(lat['max'])
        lat_jb, lat_dec = ms(lat['jb']), ms(lat['dec'])
        budget = None
        if self._jb_latency_ms is not None or T_ms:
            budget = (self._jb_latency_ms or 0.0) + LAT_BUDGET_FRAMES * T_ms

        # The kernel socket queue, converted to time at the stream's own byte
        # rate. Measured from the depayloader's output sizes, which is the
        # compressed payload and so ~1% under the wire rate -- immaterial next
        # to a queue that is either empty or holds a large fraction of a second.
        sock_b = sockq[0] if sockq else None
        sock_drops = None
        if sockq and self._sock_drops0 is not None:
            sock_drops = sockq[1] - self._sock_drops0
        byte_rate = None
        if window:
            span = window[-1][0] - window[0][0]
            if span > 0:
                byte_rate = sum(z for _t, _a, _s, _d, z in window) * (
                    Gst.SECOND / float(span))
        sock_ms = None
        if sock_b is not None:
            sock_ms = (1000.0 * sock_b / byte_rate) if byte_rate else 0.0

        # Total receiver-side delay: the socket queue nothing in the pipeline
        # can see, plus what the pipeline itself is holding.
        total = None if lat_ms is None else lat_ms + (sock_ms or 0.0)
        total_min = None if lat_min is None else lat_min + (sock_ms or 0.0)

        # Judged on the tick's minimum, not its last frame: a single late frame
        # is jitter, a minimum that has moved up is backlog the pipeline is not
        # giving back.
        over = None
        if total_min is not None and budget:
            over = total_min - budget

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
            'lat': total,
            'lat_min': total_min,
            'lat_max': None if lat_max is None else lat_max + (sock_ms or 0.0),
            'lat_jb': lat_jb,
            'lat_dec': lat_dec,
            'lat_sock': sock_ms,
            'lat_sock_b': sock_b,
            'sock_drops': sock_drops,
            'lat_budget': budget,
            'lat_over': over,
            'lat_sev': 0 if over is None else _sev(over, LAT_WARN_MS, LAT_BAD_MS),
            'alien': lat['alien'],
            'ssrc_changes': lat['ssrc_changes'],
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
                          snap['lat_sev'],
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
        # last / tick minimum / tick maximum, then the three legs it is made of
        # and the budget the minimum is judged against. sock is the kernel
        # receive queue and jb the jitterbuffer's own depth -- between them, the
        # only two places a full second of backlog can hide; dec is decode and
        # render, normally a fraction of a millisecond.
        lat_txt = ""
        if s['lat'] is not None:
            lat_txt = (f"lat {_f(s['lat'])}/{_f(s['lat_min'])}/{_f(s['lat_max'])}ms"
                       f" sock {_f(s['lat_sock'], 6)} jb {_f(s['lat_jb'], 6)}"
                       f" dec {_f(s['lat_dec'], 4)}"
                       f" bud {_f(s['lat_budget'], 5)}"
                       f" over {_f(s['lat_over'], 6)}")
            if s['sock_drops']:
                # The kernel dropped datagrams because the queue was full, i.e.
                # the reader stalled long enough to overflow it.
                lat_txt += f" SOCK-DROP {s['sock_drops']}"
            if s['alien']:
                # A second RTP stream on the same port. Not a latency figure,
                # but it belongs next to one: rtpjitterbuffer is built for a
                # single SSRC, and a foreign one makes it re-lock.
                lat_txt += f" alien {s['alien']}"
            if s['ssrc_changes'] > 1:
                lat_txt += f" ssrc-chg {s['ssrc_changes']}"
        parts = [
            f"fps {s['fps']:5.1f} (nom {s['nom']:4.1f})",
            f"clk {clk}",
            f"J {s['jit']:5.2f}ms pk {s['jit_peak']:5.2f}",
            lat_txt,
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
                "%d,%d,%s,%s,%d,%s\n" % (
                    t // 1000000,
                    dt_a // 1000,
                    '' if dt_s is None else dt_s // 1000,
                    '' if d is None else d // 1000,
                    1 if disc else 0,
                    '' if lat is None else lat // 1000)
                for t, dt_a, dt_s, d, disc, lat in rows)
        except Exception as e:
            print(f"stats: csv write failed: {e}")
            self._csv = None
