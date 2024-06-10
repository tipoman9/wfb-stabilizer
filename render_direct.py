import gi
gi.require_version('Gst', '1.0')
gi.require_version('Gtk', '3.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, Gtk, GdkX11, GstVideo

class VideoPlayer:
    def __init__(self):
        Gst.init(None)
        Gtk.init(None)

        self.window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.window.set_title("GStreamer Fullscreen")
        self.window.set_decorated(False)
        self.window.fullscreen()

        self.drawing_area = Gtk.DrawingArea()
        self.window.add(self.drawing_area)

        pipeline_str = (
            "udpsrc port=5600 buffer-size=65536 caps=\"application/x-rtp, media=(string)video, "
            "clock-rate=(int)90000, encoding-name=(string)H265\" ! rtpjitterbuffer ! rtph265depay ! "
            "avdec_h265 ! videoconvert ! xvimagesink name=video_sink sync=false"
        )
        self.pipeline = Gst.parse_launch(pipeline_str)
        self.video_sink = self.pipeline.get_by_name("video_sink")

        self.window.connect('realize', self.on_realize)
        self.window.connect('destroy', Gtk.main_quit)

        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect('message', self.on_bus_message)

        self.window.show_all()

    def on_realize(self, widget):
        window = self.drawing_area.get_window()
        xid = window.get_xid()
        self.video_sink.set_window_handle(xid)

    def on_bus_message(self, bus, message):
        if message.type == Gst.MessageType.EOS:
            Gtk.main_quit()
        elif message.type == Gst.MessageType.ERROR:
            err, debug_info = message.parse_error()
            print(f"Error received from element {message.src.get_name()}: {err.message}")
            print(f"Debugging information: {debug_info}")
            Gtk.main_quit()

    def run(self):
        self.pipeline.set_state(Gst.State.PLAYING)
        Gtk.main()
        self.pipeline.set_state(Gst.State.NULL)

if __name__ == '__main__':
    player = VideoPlayer()
    player.run()
