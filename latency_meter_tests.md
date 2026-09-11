# Latency metering attempts — what was tried and why it does not work

Goal: show, on the stats overlay, how far behind reality the displayed picture
is, so that the "latency creeps past a second and stays there until the decoder
is restarted" fault is visible while it happens.

**Outcome: no working latency figure.** The stream does not carry the
information needed. The code from the attempts is still in `stream_stats.py`
and `stats_overlay.py` but is switched off — see *Current state* at the end.

---

## The blocker

Measuring latency needs a time reference shared with the camera. This stream has
none:

* no RTCP, so no sender report ever maps an RTP timestamp to a wall clock;
* the camera's and the ground station's clocks are not synchronised;
* the RTP timestamps are **a frame counter, not a capture clock**.

The last point is the fatal one and was measured directly on the live feed:

```
RTP ts increment per frame: 1525 ticks = 16.944 ms  x2261  (100.0% of frames)
frames arriving: 50.2 fps      seq_lost: 0
media time advanced 38.31 s while wall advanced 45.03 s -> ratio 0.8509
```

Every frame advances the timestamp by exactly one 59 fps period no matter how
much real time passed. So when the encoder drops frames — above, 50.2 fps
emitted against a nominal 59, with **zero packet loss**, i.e. dropped before
transmission — the media timeline simply runs slow, here at 0.85x. Any quantity
of the form `arrival − rtp_timestamp` then climbs at ~149 ms/s while true
latency does not move: roughly 3 s of fiction every 20 s.

---

## Attempt 1 — PTS against the pipeline clock

```python
delay = clock.get_time() - sink.get_base_time()
        - segment.to_running_time(TIME, buf.pts)
```

Two independent defects:

* **Unrelated epochs.** `base_time` is the receiver's clock at the moment the
  pipeline reached PLAYING; the PTS counts media time from an origin the
  jitterbuffer picked out of the RTP stream. Nothing ties them, so the result is
  the true delay plus an arbitrary constant. It read **−200 ms** on a link
  measured at 100 ms screen-to-screen.
* **No skew correction under `mode=0`**, so sender/receiver clock-rate
  divergence integrates into it without bound.

Also found on the way: the segment step is not optional. `buf.pts` on an RTP
path carries the payloader's random timestamp offset (measured once at
~3600 s), and the matching SEGMENT event cancels it. Subtracting a raw PTS
reports about −3600 s.

### `rtpjitterbuffer mode=0` makes it worse

Feeding the repo's own clip over UDP and blacking the link out for 3 s, with the
receiver left in PLAYING throughout:

| `mode` | after the blackout |
|---|---|
| `0` (none) — as configured | climbs **exactly 1000 ms/s** for ~10 s, then latches at **13937 ms** forever, while `fps` stays 30.0 |
| `1` (slave) — GStreamer default | returns to its 0.4 ms floor |
| `4` (synced) | returns to its 0.1 ms floor |

In `mode=0` the PTS stops advancing while the clock does not, so the "delay" is
just elapsed wall time — a counter of seconds since the video started.

## Attempt 2 — validity checking on top of attempt 1

Added a check that the PTS timeline still advances with the clock (ratio 1.00
healthy, 0.00 in the failure) and reported `REF-LOST` instead of a number when
it did not, re-baselining afterwards. This correctly suppressed the fake climb,
but it was scaffolding over an unsound base — the underlying quantity still had
an arbitrary constant in it.

## Attempt 3 — receiver-side queue depths (`rx`)

Dropped PTS entirely. Measures only what this machine is holding:

```
sock = kernel UDP receive queue (/proc/net/udp), converted at the stream byte rate
jb   = (newest RTP ts arrived − RTP ts being released) / 90 kHz, at one instant
dec  = monotonic depayloader→sink transit
```

No epoch and no clock rate: `jb` is a difference of two timestamps from the same
sender clock sampled at the same moment, so origin and rate both cancel.

**This part is sound**, and is the only thing here that measures what it claims.
On the live feed it reads **0.1–0.9 ms**, flat over 33 s, no negative readings —
a true result: this pipeline adds almost nothing. An injected 1.5 s sink stall
was reported as **1457 ms** and recovered.

Known gap: `dec` uses the *most recent* depayloader push rather than the push of
the frame actually arriving, so **decoder-internal buffering is invisible** —
`vaapih265dec` declares 66.67 ms (4 frame periods) while `dec` reads 0.2 ms. The
obvious fix, pairing pushes to arrivals with a FIFO, drifts permanently whenever
the decoder fails to output a frame, which is the same class of bug as
everything else in this list.

## Attempt 4 — one-way delay (`owd` / `beh`)

`d = arrival − rtp_timestamp`, sampled before any receiver queue; reported as
growth since the run's minimum plus a least-squares rate. Absolute value never
shown, since the clock offset is unknown.

Killed by the frame-counter problem at the top of this file: forcing the encoder
to drop frames produced **3000 ms+** while real added latency was ~200 ms. The
all-time minimum also turns any slow drift into unbounded accumulation.

---

## Test-rig mistakes worth not repeating

Two synthetic senders produced false reproductions before the live feed was
used:

* `filesrc ! h265parse` on a raw byte-stream emits `pts=NONE`, so `udpsink
  sync=true` cannot pace it and it blasts the file — 16584 packets in 12 s
  instead of 6910. That floods the receiver and produces garbage PTS that looks
  exactly like a pipeline fault.
* `multifilesrc loop=true` restarts timestamps every pass.

Muxing to MP4 so `qtdemux` supplies real timestamps fixes both.

---

## What does work

* **`clk`** (already in `stream_stats.py`, predates all of this) — media time ÷
  arrival time. 1.00 when the sender's timeline tracks reality, **0.85** in the
  frame-dropping case above, and its severity already flags anything outside
  0.95–1.05. It identifies the fault without pretending to put a millisecond
  figure on it.
* **`GST_MESSAGE_LATENCY` + a LATENCY query** — the decoder declares its own
  buffering and announces changes. Verified: `vaapih265dec` reports
  `min_latency = 66666666` ns = 66.67 ms = exactly 4 frame periods. No clock
  arithmetic, event-driven, cannot drift. `render_direct.py` does not currently
  handle this message.
* **The `latency` tracer**, present in this GStreamer 1.22.2 build:

  ```bash
  GST_DEBUG=GST_TRACER:7 GST_TRACERS="latency(flags=element+reported)" \
      python3 render_direct.py 2>&1 | grep -E "element-reported-latency|element-latency"
  ```

  `element-reported-latency` logs every declared-latency change with a
  timestamp; `element-latency` gives actual per-buffer transit per element.
  Measured on a local file:

  ```
  element              samples    mean_ms     max_ms
  vaapidecode_h265-0      1818      0.288      3.045
  videoconvert0           1818      0.013      0.786
  h265parse0              1818      0.023      2.100
  ```

  Note `element-latency` is per-buffer *processing* time, not frames held across
  time — a decoder holding 4 frames still processes each in 0.288 ms. Use
  `reported` for buffering depth and `element` for a stalling element.

Genuinely measuring end-to-end latency would need a shared reference that does
not exist here: RTCP sender reports plus NTP/PTP at both ends, or a timestamp
burnt into the video by the camera. A stopwatch against the screen remains the
only ground truth.

## Unrelated finding

Port 5600 carries **a second RTP stream** alongside the video: `pt=98`,
`ssrc=1494278274`, ~35 packets/s, on an unrelated timestamp origin (the video is
`pt=97`, `ssrc=623207795`). `rtpjitterbuffer` is built for a single SSRC.

## Current state

The attempt-3 code (`sock` / `jb` / `dec`, plus the `alien` SSRC counter) is
still present in `stream_stats.py` and `stats_overlay.py` but is **disabled and
not drawn**:

* `stream_stats.ENABLE_LATENCY = False` — the delay probes are not installed, so
  there is no per-packet cost and no fields are published.
* `stats_overlay.SHOW_LATENCY = False` — nothing is rendered.

Everything that predates this work — fps, jitter, `clk`, the D percentiles,
gaps, loss counters, freezes, the jitter graph — is unchanged and still active.
Setting both flags to `True` restores the attempt-3 figures.
