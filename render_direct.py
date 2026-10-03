import gi
import os
import subprocess
import time

gi.require_version('Gst', '1.0')
gi.require_version('Gtk', '3.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, Gtk, GdkX11, GstVideo, GLib,GObject
from gi.repository import GstRtp
from pynput import keyboard
import sys
import pdb

from osd_overlay import wfbOSDWindow
from wfb_osd import wfb_srv_osd
from stream_stats import StreamStats

# Single-authority window stacking (video / msposd OSD / map) lives here; see
# VideoPlayer._restack(). python-xlib is used for precise sibling restacking.
try:
    from Xlib import display as _xlib_display, X as _X
    _HAVE_XLIB = True
except Exception:
    _HAVE_XLIB = False

#show wfbstats
wfbstats = False

#measure real fps / arrival jitter, 'stats' on the command line ('statscsv' also
#logs a per-frame row for offline analysis). See stream_stats.py.
streamstats = False
streamstats_csv = None

StartOSDApp=False
# for Intel HW acceleration
SRC = 'udpsrc port=5600 caps="application/x-rtp, payload=97, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H265" ! rtpjitterbuffer name=rtpjitterbuffer0 latency=100 mode=0 max-misorder-time=200 max-dropout-time=100 max-rtcp-rtp-time-diff=100 ! rtph265depay ! vaapih265dec ! videoconvert ! xvimagesink name=video_sink sync=false'

#No SOUND no rtpjitterbuffer needed
#SRC = 'udpsrc port=5600 caps="application/x-rtp, payload=(int)97, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H265" ! rtph265depay ! vaapih265dec ! videoconvert ! xvimagesink name=video_sink sync=false'

#simple Intel HW when there is no audio in the stream
#SRC = 'udpsrc port=5600 caps="application/x-rtp, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H265" ! rtpjitterbuffer ! rtph265depay ! vaapih265dec ! videoconvert ! xvimagesink name=video_sink sync=false'

#Path to qOpenHD to start it and bring it to front to get OSD , empty if not
OSDexecutable = '/home/home/qopenhd25/build-QOpenHD-Desktop_Qt_5_15_2_GCC_64bit-Debug/debug/QOpenHD'
#qOpenHDexecutable = ""
qOpenHDdir='/home/home/qopenhd25/build-QOpenHD-Desktop_Qt_5_15_2_GCC_64bit-Debug/debug/'
#Set qOpenHD params to transparent mode, no video, change as needed
sed_commands = (#set qOpenHD to h264 to free cpu
	"sed -i 's/^qopenhd_primary_video_codec=.*/qopenhd_primary_video_codec=0/' /home/home/.config/OpenHD/QOpenHD.conf &&"
    "sed -i 's/^dev_force_show_full_screen=.*/dev_force_show_full_screen=true/' /home/home/.config/OpenHD/QOpenHD.conf &&"
    "sed -i 's/^qopenhd_primary_video_rtp_input_port=.*/qopenhd_primary_video_rtp_input_port=5599/' /home/home/.config/OpenHD/QOpenHD.conf"
)

subprocess.run(sed_commands, shell=True)

#log = open("/tmp/msposd.log", "w")

MSPOSDexecutable = [
    "/home/home/src/msposd/msposd",
    "--master", "127.0.0.1:14550",   	
    "--osd",
    "-r", "150",
    "--out", "127.0.0.1:14560",    
    "--ahi", "4",
    "--matrix", "11"
#    ,"-v"
]	
wfbstatPort=14550

# --- stall watchdog -------------------------------------------------------
# udpsrc never posts EOS and a decoder that outputs nothing is not an error, so
# a stream that stops and resumes can leave the pipeline silently dead with
# neither of the conditions on_bus_message() restarts on ever firing.
#
# The discriminator is "packets are arriving but no frames are coming out".
# Frames out are counted by a probe on the sink pad; packets in are inferred
# from udpsrc's own `timeout` message, which costs nothing per packet -- it is
# posted only while the socket is idle. No signal at all is therefore NOT a
# restart condition, so the watchdog stays quiet with the transmitter off.
#
# Seconds without a frame at the sink, while packets keep arriving, before the
# pipeline is rebuilt.
WATCHDOG_STALL_S = 3.0
# udpsrc posts its idle message at this interval; a message newer than
# WATCHDOG_QUIET_S means the network side, not the decoder, has gone quiet.
WATCHDOG_UDP_TIMEOUT_S = 1.0
WATCHDOG_QUIET_S = 2.5
# Shortest gap between rebuilds, used for the first attempt.
WATCHDOG_MIN_INTERVAL_S = 10.0
# If rebuilding does not bring frames back, the interval doubles each time up to
# this. A stall a restart cannot fix -- an encoder sending something the decoder
# will not take, say -- must not turn into a rebuild every ten seconds for the
# rest of the flight, so retries thin out to a background check instead.
WATCHDOG_MAX_INTERVAL_S = 120.0
# Consecutive ticks with frames arriving before the pipeline counts as healthy
# again and the interval drops back to the minimum. More than one, so a couple
# of stray frames after a failed rebuild do not reset the backoff.
WATCHDOG_HEALTHY_TICKS = 5
# Grace after a rebuild before the watchdog may judge it again, so the decoder
# has time to find a keyframe.
WATCHDOG_GRACE_S = 5.0

def bring_to_foreground(process_id):
    try:
        subprocess.run(["wmctrl", "-ia", str(process_id)])
    except Exception as e:
        print(f"Error bringing window to foreground: {e}")

process_id=-1
def StartOpenHD():
    global process_id
    # Start your process, only once
    if OSDexecutable!="" and process_id==-1:
        process = subprocess.Popen(OSDexecutable)
        # run qOpenHD as a local user so that config is in ~/.config/qOpenHD                        
        time.sleep(1) 
        process_id = process.pid # Get the process ID (PID) of the last process			
        #bring_to_foreground(process_id) # Bring the window to the foreground

class VideoPlayer:
    global  SRC,StartOpenHD
    
    def __init__(self):
        self.last_seq_num=-1
        self.window_handle=-1
        Gst.init(None)
        Gtk.init(None)
    
        # Must exist before create_pipeline(), which attaches the probes, and
        # before the OSD window, which renders its snapshots.
        self.stats = StreamStats(streamstats_csv) if streamstats else None
        self.osd_win = None

        #Start simple mavlink stats if no qOpenHD
        if wfbstats:
            if os.path.exists('/tmp/wfb_server_started'):
                self.osd_win = wfb_srv_osd()
            else:
                self.osd_win = wfbOSDWindow(wfbstatPort)
            self.osd_win.stats = self.stats
        elif self.stats:
            print("stats: no OSD window (add 'wfbstats' or 'msposd') -- console only")

        self.loop = GLib.MainLoop()

        self.window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.window.set_title("GStreamer Fullscreen")
        self.window.set_decorated(False)
        self.window.fullscreen()

        self.drawing_area = Gtk.DrawingArea()
        self.window.add(self.drawing_area)
        
        self.window.connect('realize', self.on_realize_cb)
        self.window.connect('destroy', Gtk.main_quit)

        # Watchdog bookkeeping. Must exist before create_pipeline(), which
        # installs the frame counter probe.
        self._wd_frames = 0          # frames that reached the sink
        self._wd_seen = -1           # count at the last watchdog tick
        self._wd_since = None        # when the count last changed
        self._wd_udp_quiet = None    # when udpsrc last reported an idle socket
        self._wd_last_restart = None
        self._wd_restarts = 0
        self._wd_pending = False     # a rebuild is already queued
        self._wd_has_udpsrc = False
        self._wd_interval = WATCHDOG_MIN_INTERVAL_S   # grows while rebuilds fail
        self._wd_good = 0            # consecutive ticks with frames arriving

        self.create_pipeline()
        self.window.show_all()

        # Window stacking (see _restack): re-assert every 400 ms so it self-heals
        # whenever the WM reorders things (e.g. when the map window appears).
        self._xdpy = None
        GLib.timeout_add(400, self._restack)

        GLib.timeout_add(1000, self._watchdog_tick)

        if self.stats:
            GLib.timeout_add(1000, self.stats.tick)

        # Set up the keyboard listener
        self.listener = keyboard.Listener(on_press=self.on_key_press)
        self.listener.start()

    # --- window stacking (single authority) ---------------------------------
    def _xroot(self):
        if self._xdpy is None:
            self._xdpy = _xlib_display.Display()
            self._xdpy.set_error_handler(lambda *a: 0)  # never die on a stale window
        return self._xdpy, self._xdpy.screen().root

    def _toplevel(self, xid):
        """Resolve an X window id to its direct child-of-root ancestor (WM frame)."""
        d, root = self._xroot()
        w = d.create_resource_object('window', xid)
        for _ in range(32):
            t = w.query_tree()
            if t.parent.id == root.id:
                return w.id
            w = t.parent
        return xid

    def _find_osd(self):
        """The msposd OSD: a fullscreen override_redirect window at 0,0."""
        d, root = self._xroot()
        sw, sh = d.screen().width_in_pixels, d.screen().height_in_pixels
        for c in root.query_tree().children:
            try:
                a, g = c.get_attributes(), c.get_geometry()
            except Exception:
                continue
            if a.override_redirect and g.x == 0 and g.y == 0 \
                    and g.width >= sw - 2 and g.height >= sh - 2:
                return c.id
        return None

    def _active_window(self):
        """Id of the WM's active window (_NET_ACTIVE_WINDOW), or None."""
        d, root = self._xroot()
        try:
            p = root.get_full_property(
                d.intern_atom('_NET_ACTIVE_WINDOW'), _X.AnyPropertyType)
            if p and p.value:
                return p.value[0]
        except Exception:
            pass
        return None

    def _restack(self):
        """Keep the fullscreen video active (so it covers the taskbar), the OSD
        raised on top, and the map between them -> video < map < OSD.

        3-part contract with gs/mapwin and gs/map.sh: the taskbar is covered only
        while the video is the *active* window (Xfwm compositor un-redirect), so
        we keep re-asserting it as active here; the map deliberately never takes
        focus (mapwin declines it and feeds the map its keys via a global grab —
        see mapwin's header)."""
        if not _HAVE_XLIB or self.window_handle == -1:
            return True
        try:
            d, _ = self._xroot()
            osd = self._find_osd()
            if osd is None:
                return True   # OSD not up yet
            video = self._toplevel(self.window_handle)
            wobj = lambda i: d.create_resource_object('window', i)
            # Re-assert the video as the active window whenever it is not, else
            # Xfwm drops the fullscreen video below the taskbar. Edge-triggered:
            # only present() when it is NOT already active, else re-presenting
            # every tick flickers the sink.
            if self._active_window() not in (self.window_handle, video):
                self.window.present()
            wobj(video).configure(stack_mode=_X.Above)
            wobj(osd).configure(stack_mode=_X.Above)
            d.sync()
        except Exception as e:
            print(f"restack error: {e}")
        return True

    def on_realize_cb(self, widget):
        window = widget.get_window()
        if not window:
            print("Failed to get GdkWindow")
            return

        if not window.ensure_native():
            print("Can't create native window needed for GstVideoOverlay!")
            return

        self.window_handle = window.get_xid()
        self.video_sink.set_window_handle(self.window_handle)

    def print_pipeline_elements(self):
        elements = self.pipeline.iterate_elements()
        while True:
            result, element = elements.next()
            if result != Gst.IteratorResult.OK:
                break
            print(f"Element: {element.get_name()}")
        
    def _find_factory(self, suffix):
        """First element whose factory name ends with `suffix`. Matching on the
        factory rather than the name keeps this working across the SRC variants
        above, which do not all name their elements."""
        it = self.pipeline.iterate_elements()
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

    def _arm_watchdog(self):
        """Count frames reaching the sink, and ask udpsrc to say when its socket
        is idle. Together these separate a wedged decoder from a dead link."""
        self._wd_seen = -1
        self._wd_since = None
        self._wd_udp_quiet = None

        pad = self.video_sink.get_static_pad('sink') if self.video_sink else None
        if pad is not None:
            # One increment per frame (~30-120/s) and nothing else, so this is
            # safe to leave on the display path permanently.
            def count(pad, info):
                self._wd_frames += 1
                return Gst.PadProbeReturn.OK
            pad.add_probe(Gst.PadProbeType.BUFFER, count)
        else:
            print("watchdog: no sink pad -- stall detection disabled")

        udpsrc = self._find_factory('udpsrc')
        if udpsrc is not None:
            try:
                udpsrc.set_property(
                    'timeout', int(WATCHDOG_UDP_TIMEOUT_S * Gst.SECOND))
            except Exception as e:
                print(f"watchdog: could not set udpsrc timeout: {e}")
        else:
            # Without it, an idle socket is indistinguishable from a wedged
            # decoder, and restarting on no-signal would loop. Fail safe: the
            # watchdog stays armed but will never fire.
            print("watchdog: no udpsrc -- cannot tell no-signal from a stall, "
                  "stall restarts disabled")
        self._wd_has_udpsrc = udpsrc is not None

    def create_pipeline(self):
        pipeline_str = SRC
        self.pipeline = Gst.parse_launch(pipeline_str)
        self.video_sink = self.pipeline.get_by_name("video_sink")
        self.print_pipeline_elements()
        if self.stats:
            self.stats.attach(self.pipeline)
        self._arm_watchdog()

        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self._bus_handler = self.bus.connect('message', self.on_bus_message)
        if self.window_handle!=-1:
            self.video_sink.set_window_handle(self.window_handle)
        
    def on_bus_message(self, bus, message):
        if message.type == Gst.MessageType.ELEMENT:
            st = message.get_structure()
            if st is not None and st.get_name() == 'GstUDPSrcTimeout':
                # The socket has been idle. Note when, so the watchdog can tell
                # "nothing is being transmitted" from "the decoder is wedged"
                # and only restart for the latter. Handled before the trace
                # below and returned from: udpsrc repeats this every
                # WATCHDOG_UDP_TIMEOUT_S for as long as the link is down, which
                # would otherwise be a line of log per second.
                self._wd_udp_quiet = time.monotonic()
                return

        if not self.stats:
            print(f"Stream message: {message.type}")
        if message.type == Gst.MessageType.STREAM_START:
            if StartOSDApp:
                StartOpenHD()
                print(f"Starting : {OSDexecutable}")
            else:
                print(f"Skipping qOpenHD: {message.type}")

        if message.type == Gst.MessageType.EOS:
            #self.loop.quit()
            self._queue_restart('EOS')
        if message.type == Gst.MessageType.QOS:
            # QoS reports are routine flow control and carry the decoder's own
            # processed/dropped counters -- record them, never restart on them.
            if self.stats:
                self.stats.note_qos(message)
        elif message.type == Gst.MessageType.ERROR:
            err, debug_info = message.parse_error()
            print(f"Error received from element {message.src.get_name()}: {err.message}")
            print(f"Debugging information: {debug_info}")
            #self.loop.quit()
            self._queue_restart(f"error from {message.src.get_name()}")

    def on_key_press(self, key):
        try:
            if key.char and key.char.lower() == 'q':
                #self.restart_pipeline()
                self.quit()
            elif key.char and key.char.lower() == 's' and self.osd_win is not None:
                # off -> compact -> detail. Redraw is driven by the OSD's own
                # timer, so there is nothing to queue from here.
                self.osd_win.stats_mode = (self.osd_win.stats_mode + 1) % 3
        except AttributeError:
            if key == keyboard.Key.esc:
                self.restart_pipeline()
                #self.quit()

    def _watchdog_tick(self):
        """Rebuild the pipeline when frames stop arriving at the sink while the
        network side is still delivering.

        Only that combination is a stall worth acting on. No frames *and* an
        idle socket is simply no signal, and rebuilding then would loop for as
        long as the transmitter is off.
        """
        now = time.monotonic()

        if self._wd_frames != self._wd_seen:
            self._wd_seen = self._wd_frames
            self._wd_since = now
            # Sustained output means whatever was wrong is over, so the next
            # stall gets a prompt rebuild rather than inheriting a long backoff.
            self._wd_good += 1
            if (self._wd_good >= WATCHDOG_HEALTHY_TICKS
                    and self._wd_interval != WATCHDOG_MIN_INTERVAL_S):
                print(f"watchdog: frames flowing again, retry interval back to "
                      f"{WATCHDOG_MIN_INTERVAL_S:.0f}s")
                self._wd_interval = WATCHDOG_MIN_INTERVAL_S
            return True
        self._wd_good = 0
        if self._wd_since is None:
            # First tick, or just after a rebuild: start the clock rather than
            # treating "no frames yet" as a stall.
            self._wd_since = now
            return True

        stalled_for = now - self._wd_since
        if stalled_for < WATCHDOG_STALL_S or not self._wd_has_udpsrc:
            return True
        if (self._wd_udp_quiet is not None
                and now - self._wd_udp_quiet < WATCHDOG_QUIET_S):
            return True     # no signal, not a stall
        if self._wd_last_restart is not None:
            since = now - self._wd_last_restart
            if since < max(self._wd_interval, WATCHDOG_GRACE_S):
                return True

        self._queue_restart(
            f"stalled {stalled_for:.1f}s with packets still arriving")
        # Back off for the next one. Reset by a spell of healthy output above.
        nxt = min(self._wd_interval * 2.0, WATCHDOG_MAX_INTERVAL_S)
        if nxt != self._wd_interval:
            print(f"watchdog: next retry in {nxt:.0f}s if this does not help")
        self._wd_interval = nxt
        return True

    def _queue_restart(self, reason):
        """Restart from the main loop rather than from here.

        on_bus_message() is one of the callers, and restart_pipeline() removes
        the very bus watch whose callback would be running -- tearing that down
        underneath itself. Deferring to an idle callback keeps the rebuild out
        of any handler it dismantles.
        """
        if self._wd_pending:
            return
        self._wd_pending = True
        self._wd_restarts += 1
        self._wd_reason = reason
        print(f"Restarting decoder (#{self._wd_restarts}): {reason}")

        def go():
            self._wd_pending = False
            self._wd_last_restart = time.monotonic()
            self.restart_pipeline()
            return False
        GLib.idle_add(go)

    def restart_pipeline(self):
        if self.stats:
            self.stats.detach()
            self.stats.reset()
        self.pipeline.set_state(Gst.State.NULL)
        # Drop the old bus watch: its GSource holds a ref to the bus, which
        # holds the old pipeline alive for the rest of the process.
        self.bus.disconnect(self._bus_handler)
        self.bus.remove_signal_watch()
        time.sleep(0.1)
        self.create_pipeline()
        self.pipeline.set_state(Gst.State.PLAYING)

    def quit(self):
        self.listener.stop()
        self.loop.quit()

    def run(self):
        self.pipeline.set_state(Gst.State.PLAYING)
        #while True :
        self.loop.run()
        #    print(f"Restarting Decoder")
        self.pipeline.set_state(Gst.State.NULL)
        if self.stats:
            self.stats.close()

if __name__ == '__main__':  
        # Check if 'NoOSD' is in the command-line arguments
    if 'qopenhd' in sys.argv:
        StartOSDApp=True
        wfbstats=False    

    if 'msposd' in sys.argv:    
        StartOSDApp=True
        wfbstats=True
        wfbstatPort=14551
        OSDexecutable = MSPOSDexecutable

    if 'wfbstats' in sys.argv:
        StartOSDApp=False
        wfbstats=True

    if 'stats' in sys.argv or 'statscsv' in sys.argv:
        streamstats=True
    if 'statscsv' in sys.argv:
        streamstats_csv='/tmp/stream_stats.csv'


        #StartOpenHD()      
    while True :
        player = VideoPlayer()    
        player.run()
        exit()

