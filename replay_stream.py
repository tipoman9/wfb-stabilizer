"""Replay a recorded HEVC .mov/.mp4 as a fake live wfb-ng stream.

Sends RTP H.265 payload 97 to UDP 5600, matching the default SRC in
ejo_wfb_stabilizer.py and render_direct.py, so they can be tested without a
drone. The video is not re-encoded: qtdemux supplies real timestamps and
udpsink sync=true paces packets at the recorded frame rate (see "Test-rig
mistakes" in latency_meter_tests.md).

Looping uses segment seeks, so the RTP session (ssrc, seqnum, timestamps)
continues across passes like a real feed instead of restarting.

Only one file per run: switching between recordings with different encoder
settings mid-stream makes vaapih265dec abort with
"decode_ref_pic_set: assertion failed: (stRefPic != NULL)".

Usage:
    python3 replay_stream.py ~/Video/vid2209_1735_40_shaking.mov
    python3 replay_stream.py --once --host 192.168.1.20 ~/Video/vid2603_1323_30_zoom.mov
    python3 ejo_wfb_stabilizer.py        # in another terminal
"""
import argparse
import os
import sys

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

parser = argparse.ArgumentParser(description="Replay an HEVC recording as an RTP H.265 stream")
parser.add_argument("file")
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=5600)
parser.add_argument("--once", action="store_true", help="play once instead of looping")
args = parser.parse_args()

path = os.path.abspath(os.path.expanduser(args.file))
if not os.path.isfile(path):
	sys.exit(f"No such file: {path}")

Gst.init(None)
pipeline = Gst.parse_launch(
	f'filesrc location="{path}" ! qtdemux ! '
	'h265parse config-interval=-1 ! video/x-h265,stream-format=byte-stream,alignment=au ! '
	'rtph265pay pt=97 mtu=1400 config-interval=-1 ! '
	f'udpsink host={args.host} port={args.port} sync=true'
)
loop = GLib.MainLoop()
passes = 0

def seek_to_start(flags):
	pipeline.seek(1.0, Gst.Format.TIME, flags,
		Gst.SeekType.SET, 0, Gst.SeekType.NONE, -1)

def on_message(bus, msg):
	global passes
	if msg.type == Gst.MessageType.SEGMENT_DONE:
		passes += 1
		print(f"Pass {passes} done, looping")
		seek_to_start(Gst.SeekFlags.SEGMENT)
	elif msg.type == Gst.MessageType.EOS:
		print("Done")
		loop.quit()
	elif msg.type == Gst.MessageType.ERROR:
		err, dbg = msg.parse_error()
		print(f"Error: {err.message} ({dbg})")
		loop.quit()
	elif (msg.type == Gst.MessageType.ASYNC_DONE and not args.once
			and not getattr(on_message, "seeked", False)):
		# First preroll: switch to segment seeks so the end of the file posts
		# SEGMENT_DONE instead of EOS.
		on_message.seeked = True
		seek_to_start(Gst.SeekFlags.FLUSH | Gst.SeekFlags.SEGMENT)

bus = pipeline.get_bus()
bus.add_signal_watch()
bus.connect("message", on_message)

print(f"Streaming {path} -> {args.host}:{args.port}" + ("" if args.once else " (looping, Ctrl+C to stop)"))
pipeline.set_state(Gst.State.PAUSED)
pipeline.set_state(Gst.State.PLAYING)
try:
	loop.run()
except KeyboardInterrupt:
	pass
pipeline.set_state(Gst.State.NULL)
