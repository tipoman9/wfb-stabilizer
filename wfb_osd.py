# uses input from a patched wfb_rx over the mavlink, needs mavlink port to be duplicated
# 

import os
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib, GdkPixbuf, Pango, PangoCairo, cairo
from pymavlink import mavutil
import struct
import socket
import threading
import json
from collections import defaultdict, deque
import math


# Config
HOST = '127.0.0.1'
#PORT = 8103
HISTORY_LEN = 10

class wfbOSDWindow(Gtk.Window):   
    # History storage
    antenna_history = defaultdict(lambda: {
        'pkt_recv': deque(maxlen=HISTORY_LEN),
        'pkt_lost': deque(maxlen=HISTORY_LEN),
        'rssi_avg': deque(maxlen=HISTORY_LEN)
    })

    packet_history = defaultdict(lambda: deque(maxlen=HISTORY_LEN))  # for all, dec_err, etc.
  

    def connect_and_track(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            print(f"Connecting to {HOST}:{self.wfb_port}...")
            sock.connect((HOST, self.wfb_port))
            sock_file = sock.makefile('r')

            print("Connected. Listening for JSON data...\n")
            for line in sock_file:
                line = line.strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)

                    # Process packet stats
                    packets = data.get('packets', {})
                    self.wfb_stat_id=data.get('id')
                    if self.wfb_stat_id!="video rx":
                        continue

                    for key in ['all','all_bytes', 'dec_err', 'dec_ok','uniq', 'fec_rec', 'lost','out_bytes']:
                        if key in packets:
                            value = packets[key][0] if isinstance(packets[key], list) else packets[key]
                            self.packet_history[key].append(value)
                    
                    print("--- Packet History (last 10) ---")
                    for key in ['all','uniq', 'dec_err', 'dec_ok', 'fec_rec', 'lost']:
                        print(f"{key}: {list(self.packet_history[key])}")

                    self.out_bytes = self.packet_history["out_bytes"][-1] if self.packet_history["out_bytes"] else None
                    self.uniq = self.packet_history["uniq"][-1] if self.packet_history["all"] else None
                    self.bad = self.packet_history["bad"][-1] if self.packet_history["bad"] else None
 
                    self.all_bytes = self.packet_history["all_bytes"][-1] if self.packet_history["all_bytes"] else None
                    self.lost = self.packet_history["lost"][-1] if self.packet_history["lost"] else None
                    self.fec_rec =  self.packet_history["fec_rec"][-1] if self.packet_history["fec_rec"] else None
                    self.RecoveredFrags =  0

                    for ant_id, stats in self.channel_stats.items():                        
                        stats["pkt_recv"] = 0

                    # Process antenna stats
                    for ant in data.get('rx_ant_stats', []):
                        ant_id = ant['ant']
                        
                        pkt_lost = (self.uniq - ant['pkt_recv']) + self.lost + self.fec_rec
                         
                        self.antenna_history[ant_id]['pkt_recv'].append(ant['pkt_recv'])
                        self.antenna_history[ant_id]['pkt_lost'].append(pkt_lost)
                        self.antenna_history[ant_id]['rssi_avg'].append(ant['rssi_avg'])
                        
                        self.channel_stats[ant_id] = {
                            "pkt_recv": ant['pkt_recv'],
                            "pkt_lost": pkt_lost,
                            "rssi_avg": abs(ant['rssi_avg']),
                            "link_health": 100
                        }

                    # Force the window to redraw
                    self.queue_draw()
                     # Optional: print latest state for debugging
                    print("--- Antenna History (last 10) ---")
                    for ant_id, stats in self.antenna_history.items():
                        print(f"Antenna {ant_id}: pkt_recv={list(stats['pkt_recv'])}, rssi_avg={list(stats['rssi_avg'])}")

                    print()

                except json.JSONDecodeError as e:
                    print(f"Invalid JSON: {e}")
                except Exception as e:
                    print(f"Error: {e}")

    def __init__(self, WFP_Port=8103):
        self.wfb_port = WFP_Port       

        self.channel_stats = {}
        self.out_bytes = 0
        self.uniq = 0
        self.bad = 0
        self.all_bytes = 0
        self.lost = 0
        self.fec_rec =  0
        self.RecoveredFrags =  0

        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        
        # Set up the transparent window
        self.set_title("Mavlink Overlay")
        self.set_default_size(380, 260)
        self.set_app_paintable(True)
        self.set_decorated(False)
        #self.set_keep_above(True)
        self.set_accept_focus(False)
        
        # Set the window to be fully transparent
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual and self.is_composited():
            self.set_visual(visual)
        
        # Connect the draw event
        self.connect("draw", self.on_draw)
        
        # Set the window to be always on top
        self.set_keep_above(True)
        
        # Make the window transparent
        self.set_opacity(1)

        # Move the window to the top-left corner
        self.move(0, 0)      
               
        try:# Load the image file as a GdkPixbuf
            script_dir = os.path.dirname(os.path.abspath(__file__))
            self.icon_pixbuf = GdkPixbuf.Pixbuf.new_from_file(script_dir +"/"+ "icons/dbm.png")
        except Exception as e:
            print(f"Error loading image: {e}")
            self.icon_pixbuf = None


        # Start a timer for updating OSD information
        # self.update_osd()
         # Start background thread
        threading.Thread(target=self.connect_and_track, daemon=True).start()
        

        # Show the window
        self.show_all()

    def outlined(self, cr, text, x, y, outline_color=(0, 0, 0, 0.9), outline_width=3):
        """
        Draw text with an outline.
        
        Parameters:
        - cr: The Cairo context.
        - text: The text to draw.
        - x, y: The coordinates to start drawing the text.
        - outline_color: A tuple representing the RGBA color for the outline. Default is semi-transparent black.
        - outline_width: The width of the outline. Default is 2.
        """

        original_color = cr.get_source()
                
        cr.set_source_rgba(*outline_color)  # Set outline color
        cr.move_to(x, y)
        cr.text_path(text)
        cr.set_line_width(outline_width)
        cr.stroke_preserve()  # Stroke the outline while preserving the path
                
        cr.set_source(original_color) # Restore the original color
        
        cr.fill()


    def on_draw(self, widget, cr):
        fontsize=20
        # Clear the background with full transparency
        cr.set_source_rgba(0, 0, 0, 0)
        cr.set_operator(cairo.Operator.CLEAR)
        cr.paint()
        cr.set_operator(cairo.Operator.OVER)

        fontname="Courier" # Ubuntu # Arial        
        #fontname = "Ubuntu Mono"
        fontname = "DejaVu Sans Mono"
        #fontname = "Liberation Mono"
        #fontname = "Liberation Sans"
        #fontname = "Noto Sans CJK JP"
        cr.set_source_rgb(1, 1, 1)  # White color
        cr.select_font_face(fontname, cairo.FontSlant.NORMAL, cairo.FontWeight.NORMAL) #BOLD
        cr.set_font_size(fontsize)
      
        #text = f"{self.uniq} "
        cr.move_to(80, 20)

        if self.lost>0:
                cr.set_source_rgb(1, 0.0, 0.0)  # Red color
        elif self.fec_rec>5:
                cr.set_source_rgb(1, 1, 0.5)  # Yellow color
        else:
             cr.set_source_rgb(1, 1, 1)  # White color

        self.outlined(cr, f"{self.uniq} {self.lost:2} {self.fec_rec:2}", 46, 20)        

        cr.select_font_face("Arial", cairo.FontSlant.ITALIC, cairo.FontWeight.NORMAL) #cairo.FontSlant.ITALIC
        cr.set_source_rgb(1, 1, 1)  # White color
        cr.set_font_size(fontsize-2)
        cr.move_to(210, 20)        
        
        self.outlined(cr, f"{8 * self.all_bytes/(1024*1024):.1f}→{8 * self.out_bytes/(1024*1024):.1f}Mb/s", 192, 19)

        row=0
        cr.select_font_face(fontname, cairo.FontSlant.NORMAL, cairo.FontWeight.NORMAL)
        cr.set_font_size(fontsize)
        for card_index, stats in self.channel_stats.items():            

            cr.set_source_rgb(0.7, 1, 0.7)  # Greenish color
            cr.move_to(10,(fontsize+4)*row + 42) # {card_index}:
            if stats['pkt_lost']>30:
                cr.set_source_rgb(1, 0, 0)  # Red color
            elif stats['pkt_lost']>2:
                cr.set_source_rgb(1, 1, 0.7)  # yellow color
            
            if stats['pkt_lost']>999:
                pcktlost="~999"
            else:
                pcktlost=f"{-stats['pkt_lost']:>4}"

            if stats['pkt_recv']==0:
                pcktlost="~~~~"
                stats['rssi_avg']="??"

            clr = cr.get_source()                        
            self.outlined(cr, f"{stats['rssi_avg']} {pcktlost}", 10,(fontsize+2)*row + 46)
           
            # =============================================================================================
            # ================ Mini Line Chart (updated per antenna iteration) ============================

            chart_x = 110
            chart_y = (fontsize + 0) * row + 32
            chart_height = fontsize - 4
            point_width = 6


            pkt_recv_history = self.antenna_history[card_index]['pkt_lost']
            #pckt_lost_values = [min(v, chart_height) for v in list(pkt_recv_history)]
            pckt_lost_values = [self.exp_scale(v, 50, chart_height-2,15) for v in list(pkt_recv_history)]
            if stats['pkt_recv']==0:
                pckt_lost_values = [chart_height-2 for v in list(pkt_recv_history)]
            
            while len(pckt_lost_values) < 10:
                pckt_lost_values.insert(0, 0)
            
            for i in range(len(pckt_lost_values) - 1):
                x1 = chart_x + i * point_width
                y1 = chart_y + chart_height - pckt_lost_values[i]
                x2 = chart_x + (i + 1) * point_width
                y2 = chart_y + chart_height - pckt_lost_values[i + 1]

                # 1. Black outline (thicker, behind the color)
                cr.set_source_rgb(0, 0, 0)
                cr.set_line_width(5)
                cr.move_to(x1, y1)
                cr.line_to(x2, y2)
                cr.stroke()

                # 2. Colored foreground based on average value of the segment
                avg_val = (pckt_lost_values[i] + pckt_lost_values[i + 1]) / 2
                if avg_val < 3:
                    cr.set_source_rgb(0.0, 1.0, 0.0)  # Green
                elif avg_val <= chart_height-2 -1:
                    cr.set_source_rgb(1.0, 1.0, 0.0)  # Yellow
                else:
                    cr.set_source_rgb(1.0, 0.0, 0.0)  # Red

                cr.set_line_width(3)
                cr.move_to(x1, y1)
                cr.line_to(x2, y2)
                cr.stroke()

            row=row+1
                        

        gdk_cairo_surface = Gdk.cairo_surface_create_from_pixbuf(self.icon_pixbuf, 1, widget.get_window())

        # Draw the image at the specified coordinates
        cr.set_source_surface(gdk_cairo_surface, 6, 4)
        cr.paint()
        self.set_keep_above(True)
        self.present()

        #PangoCairo.show_layout(cr, layout)

        return True        

    def exp_scale(self, val, max_input=50, max_output=20, steepness=15):
        val = max(0, min(val, max_input))  # clamp
        return max_output * (1 - math.exp(-val / steepness))


# Initialize GTK
if __name__ == "__main__":
    win = wfbOSDWindow()
    Gtk.main()
