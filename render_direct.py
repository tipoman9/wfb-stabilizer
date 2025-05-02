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

#show wfbstats
wfbstats = False

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

MSPOSDexecutable = [
    "/home/home/src/msposd/msposd",
    "--master", "127.0.0.1:14550",   	
    "--osd",
    "-r", "50",
    "--ahi", "3",
    "--matrix", "11"
    #,"-v"
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
    
        #Start simple mavlink stats if no qOpenHD        
        if wfbstats:  
            if os.path.exists('/tmp/wfb_server_started'):
                win = wfb_srv_osd()
            else:
                win = wfbOSDWindow(wfbstatPort)
            

        self.loop = GLib.MainLoop()

        self.window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.window.set_title("GStreamer Fullscreen")
        self.window.set_default_size(800, 600)
        self.window.set_decorated(False)
        self.window.fullscreen()

        self.drawing_area = Gtk.DrawingArea()
        self.window.add(self.drawing_area)
        
        self.window.connect('realize', self.on_realize_cb)
        self.window.connect('destroy', Gtk.main_quit)

        self.create_pipeline()
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

        self.window_handle = window.get_xid()
        self.video_sink.set_window_handle(self.window_handle)

    def pad_probe_callback(self, pad, info):
        if info.type & Gst.PadProbeType.BUFFER:
            buffer = info.get_buffer()
            try:
                #print("pad_probe_callback  STEP 1")                        
                success, rtp_buffer = GstRtp.RTPBuffer.map(buffer, Gst.MapFlags.READ)
                if not success:
                    raise RuntimeError("Failed to map RTP buffer")
                
                current_seq_num = rtp_buffer.get_seq()
                #print(f"pad_probe_callback  STEP 2 {current_seq_num}")        
                if self.last_seq_num is not None and current_seq_num < self.last_seq_num:
                    print(f"Out-of-order packet detected! Current sequence: {current_seq_num}, Last sequence: {self.last_seq_num}")


                self.last_seq_num = current_seq_num

            except Exception as e:                
                print(f"Error processing RTP buffer: {str(e)}")

            finally:
                # Always unmap the RTP buffer, even if an error occurs
                if 'rtp_buffer' in locals():
                    GstRtp.RTPBuffer.unmap(rtp_buffer)

        return Gst.PadProbeReturn.OK     

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
        # HOOK TO rtpjitterbuffer element to inspect
        # try:                   
        #     rtpjitterbuffer = self.pipeline.get_by_name('rtpjitterbuffer0')     
        #     if rtpjitterbuffer is not None:
        #         src_pad = rtpjitterbuffer.get_static_pad('src')
        #         if src_pad is not None:
        #             src_pad.add_probe(Gst.PadProbeType.BUFFER, self.pad_probe_callback)
        # except Exception as e:
        #      print(f"Error  HOOK TO rtpjitterbuffer: {str(e)}")

        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect('message', self.on_bus_message)
        if self.window_handle!=-1:
            self.video_sink.set_window_handle(self.window_handle)
        
    def on_bus_message(self, bus, message):
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
            self.restart_pipeline()
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
        except AttributeError:
            if key == keyboard.Key.esc:
                self.restart_pipeline()
                #self.quit()

    def restart_pipeline(self):
        
        self.pipeline.set_state(Gst.State.NULL)
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
        

        #StartOpenHD()      
    while True :
        player = VideoPlayer()    
        player.run()
        exit()

