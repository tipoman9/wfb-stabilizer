import gi
gi.require_version('Gst', '1.0')
gi.require_version('Gtk', '3.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, Gtk, GdkX11, GstVideo, GLib
from pynput import keyboard

class VideoPlayer:
    def __init__(self):
        Gst.init(None)
        Gtk.init(None)

        self.loop = GLib.MainLoop()

        self.window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.window.set_title("GStreamer Fullscreen")
        self.window.set_default_size(800, 600)
        self.window.set_decorated(False)
        self.window.fullscreen()

        self.drawing_area = Gtk.DrawingArea()
        self.window.add(self.drawing_area)

        pipeline_str = (
            'udpsrc port=5600 buffer-size=65536 caps="application/x-rtp, media=(string)video, '
            'clock-rate=(int)90000, encoding-name=(string)H265" ! rtpjitterbuffer ! rtph265depay ! '
            'vaapih265dec ! videoconvert ! xvimagesink name=video_sink sync=false'
        )
        self.pipeline = Gst.parse_launch(pipeline_str)
        self.video_sink = self.pipeline.get_by_name("video_sink")

        self.window.connect('realize', self.on_realize_cb)
        self.window.connect('destroy', Gtk.main_quit)

        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect('message', self.on_bus_message)

        self.window.show_all()

        # Set up the keyboard listener
        self.listener = keyboard.Listener(on_press=self.on_key_press)
        self.listener.start()

    def on_realize_cb(self, widget):
        window = widget.get_window()
        if not window:
            print("Failed to get GdkWindow")
            return

        if not window.ensure_native():
            print("Can't create native window needed for GstVideoOverlay!")
            return

        window_handle = window.get_xid()
        self.video_sink.set_window_handle(window_handle)

    def on_bus_message(self, bus, message):
        if message.type == Gst.MessageType.EOS:
            self.loop.quit()
        elif message.type == Gst.MessageType.ERROR:
            err, debug_info = message.parse_error()
            print(f"Error received from element {message.src.get_name()}: {err.message}")
            print(f"Debugging information: {debug_info}")
            self.loop.quit()

    def on_key_press(self, key):
        try:
            if key.char and key.char.lower() == 'q':
                self.quit()
        except AttributeError:
            if key == keyboard.Key.esc:
                self.quit()

    def quit(self):
        self.listener.stop()
        self.loop.quit()

    def run(self):
        self.pipeline.set_state(Gst.State.PLAYING)
        self.loop.run()
        self.pipeline.set_state(Gst.State.NULL)

if __name__ == '__main__':
    player = VideoPlayer()
    player.run()
