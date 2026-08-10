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

        self.create_pipeline()
        self.window.show_all()

        # Window stacking (see _restack): re-assert every 400 ms so it self-heals
        # whenever the WM reorders things (e.g. when the map window appears).
        self._xdpy = None
        GLib.timeout_add(400, self._restack)

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
        
    def create_pipeline(self):
        pipeline_str = SRC
        self.pipeline = Gst.parse_launch(pipeline_str)
        self.video_sink = self.pipeline.get_by_name("video_sink")        
        self.print_pipeline_elements()
        if self.stats:
            self.stats.attach(self.pipeline)

        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self._bus_handler = self.bus.connect('message', self.on_bus_message)
        if self.window_handle!=-1:
            self.video_sink.set_window_handle(self.window_handle)
        
    def on_bus_message(self, bus, message):
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
            self.restart_pipeline()
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
            self.restart_pipeline()

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

