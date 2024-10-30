import os
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib, GdkPixbuf, Pango, PangoCairo, cairo
from pymavlink import mavutil
import struct


class wfbOSDWindow(Gtk.Window):     

    def __init__(self, MavlinkPort=14550):
        self.MavlinkPort = MavlinkPort       

        self.channel_stats = {}
        self.bpsTtl = 0
        self.ppsTtl = 0
        self.pcktsDroppedTtl = 0
        self.BitrateTotal = 0
        self.LostPckts = 0
        self.RecoveredPckts =  0
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

        self.mavlink_connection = mavutil.mavlink_connection(f'udpin:127.0.0.1:{self.MavlinkPort}')
        self.start_time = None  # Variable to hold armed time

       # Load an icon (you can use a file path to an image instead)
       # icon_theme = Gtk.IconTheme.get_default()
       # self.icon_pixbuf = icon_theme.load_icon("icons\VTx.png", 32, 0)  # Load a standard icon, "gtk-open" for example
               
        try:# Load the image file as a GdkPixbuf
            script_dir = os.path.dirname(os.path.abspath(__file__))
            self.icon_pixbuf = GdkPixbuf.Pixbuf.new_from_file(script_dir +"/"+ "icons/dbm.png")
        except Exception as e:
            print(f"Error loading image: {e}")
            self.icon_pixbuf = None


        # Start a timer for updating OSD information
        self.update_osd()

        # Show the window
        self.show_all()


    def update_osd(self):
        # Retrieve OSD information
        msg = self.mavlink_connection.recv_match(blocking=False)
        if msg is not None:
            # Get the message ID
            message_id = msg.get_msgId()
            #print(f"Received {msg.get_type()} message ID: {message_id} seq:{msg.get_seq()}")                                       

        OPENHD_STATS_MONITOR_MODE_WIFI_CARD_ID = 1212
        if msg and msg.get_type() == "UNKNOWN_1212":                        
            buffer = msg.get_msgbuf()# msg.get_payload()
            # Skip the first 10 bytes (MAVLink message header) and get the remaining 24 bytes
            payload = buffer[10:]
            if len(payload) != 26:
                print(f"Unexpected payload length: {len(payload)}. Expected 24 bytes.")
                GLib.timeout_add(10, self.update_osd)
                return          
            message_data = struct.unpack('<IIihhBBbbbBBbh', payload)            
            # This is information per WiFi Card
            count_p_received = message_data[0] # total number
            count_p_injected = message_data[1]
            dummy2 = message_data[2]
            tx_power = message_data[3]
            dummy1 = message_data[4]
            card_index = message_data[5] # Card index
            card_type = message_data[6]
            rx_rssi_1 = message_data[7] # rssi
            rx_rssi_2 = message_data[8]
            rx_signal_quality = message_data[9]
            curr_rx_packet_loss_perc = message_data[10] # Not percent but actual number lost packets
                        
            self.channel_stats[card_index] = {
                "pckt_received": count_p_received,
                "pckt_lost": curr_rx_packet_loss_perc,
                "rssi": abs(rx_rssi_1),
                "link_health": 100
            }

        if msg and msg.get_type() == "UNKNOWN_1216":  # Replace UNKNOWN_1216 with the correct type if known
                # Get the raw message buffer
                buffer = msg.get_msgbuf()
                
                # Skip the first 10 bytes (MAVLink message header) and get the remaining 24 bytes
                payload = buffer[10:]

                if len(payload) != 28 + 2:
                    print(f"Unexpected payload length: {len(payload)}. Expected 19 bytes.")
                    GLib.timeout_add(10, self.update_osd)
                    return
                
                try:
                    # Unpack the remaining 24 bytes according to the structure
                    # Format: '<iIIIIihbB' corresponds to the order and types of the fields
                    message_data = struct.unpack('<iIIIIiibb', payload)

                    # Assign fields to variables for clarity
                    curr_incoming_bitrate = message_data[0] # Bitrate
                    count_blocks_total = message_data[1]# 500 fixed
                    count_blocks_lost = message_data[2]  # Lost even after FEC
                    count_blocks_recovered = message_data[3]  # Recovered by FEC !!!
                    count_fragments_recovered = message_data[4]  # Recovered fragemnts by FEC !!!
                    
                    self.BitrateTotal = curr_incoming_bitrate
                    self.LostPckts = count_blocks_lost
                    self.RecoveredPckts =  count_blocks_recovered
                    self.RecoveredFrags =  count_fragments_recovered

                except struct.error as e:
                    print(f"Error unpacking payload: {e}")
           
        if msg and msg.get_type() == "UNKNOWN_1211":  # Replace UNKNOWN_1211 with the correct type if known
                # Get the raw message buffer
                buffer = msg.get_msgbuf()
                
                # Skip the first 10 bytes (MAVLink message header) and get the remaining 44 bytes
                payload = buffer[10:]

                if len(payload) != 40+2:
                    print(f"Unexpected payload length: {len(payload)}. Expected 44 bytes.")
                    GLib.timeout_add(10, self.update_osd)
                    return
                
                try:
                    # Unpack the remaining 44 bytes according to the structure
                    # Format: '<iiIIihhhHHhbbbbbBbb' corresponds to the order and types of the fields
                    message_data = struct.unpack('<iiIIihhhHHhbbbbbBbbBB', payload)

                    # Assign fields to variables for clarity
                    curr_tx_bps = message_data[0]
                    curr_rx_bps = message_data[1]  # bits per second
                    count_tx_inj_error_hint = message_data[2] # dropped packets
                    count_tx_dropped_packets = message_data[3]
                    dummy2 = message_data[4]
                    curr_tx_pps = message_data[5]
                    curr_rx_pps = message_data[6]  # packets per second
                    curr_rx_big_gaps_counter = message_data[7]
                    curr_tx_channel_mhz = message_data[8]
                    curr_rate_kbits = message_data[9] 
                    dummy1 = message_data[10]
                    curr_rx_packet_loss_perc = message_data[11] # loss_percent calculated by wfb_rx
                    curr_tx_card_idx = message_data[12]
                    curr_tx_channel_w_mhz = message_data[13]
                    curr_tx_stbc_lpdc_shortguard_bitfield = message_data[14]
                    curr_tx_mcs_index = message_data[15]
                    tx_passive_mode_is_enabled = message_data[16]
                    curr_n_rate_adjustments = message_data[17]
                    dummy0 = message_data[18]

                    self.bpsTtl = curr_rx_bps
                    self.ppsTtl = curr_rx_pps
                    self.pcktsDroppedTtl = count_tx_inj_error_hint

               
                except struct.error as e:
                    print(f"Error unpacking payload: {e}")


        # if msg:
        #     #print(msg)
        #     if msg.get_type() == 'HEARTBEAT':                
        #         self.start_time = None  # Clear start time when disarmed
        #     elif msg.get_type() == 'SYS_STATUS':
        #         self.start_time = None  # Clear start time when disarmed                 
        #     elif msg.get_type() == 'RC_CHANNELS_RAW':
        #         self.start_time = None  # Clear start time when disarmed
        #     elif msg.get_type() == 'ATTITUDE':
        #         self.roll_label.set_text(f" {msg.roll * 180 / math.pi:.1f} °")
        #         self.pitch_label.set_text(f" {msg.pitch * 180 / math.pi:.1f} °")
        #         #self.yaw = msg.yaw*180/math.pi
             
        
        if self.start_time is not None:
            armed_time = time.time() - self.start_time
            self.armtime_label.set_text(' %02u:%02u' % (int(armed_time)/60, int(armed_time)%60))

        # Force the window to redraw
        self.queue_draw()
        # Update every 5ms
        GLib.timeout_add(10, self.update_osd)

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
        # Save the current color
        original_color = cr.get_source()
        
        # Draw the outline
        cr.set_source_rgba(*outline_color)  # Set outline color
        cr.move_to(x, y)
        cr.text_path(text)
        cr.set_line_width(outline_width)
        cr.stroke_preserve()  # Stroke the outline while preserving the path
        
        # Restore the original color
        cr.set_source(original_color)
        
        # Fill the text
        cr.fill()


    def on_draw(self, widget, cr):
        fontsize=24
        # Clear the background with full transparency
        cr.set_source_rgba(0, 0, 0, 0)
        cr.set_operator(cairo.Operator.CLEAR)
        cr.paint()
        cr.set_operator(cairo.Operator.OVER)


          # Create a Pango layout
        #layout = PangoCairo.create_layout(cr)        
        #font_desc = Pango.FontDescription("Droid Sans Mono 20")
        #layout.set_font_description(font_desc)

        fontname="Courier" # Ubuntu # Arial
        # Draw the text in white
        cr.set_source_rgb(1, 1, 1)  # White color
        cr.select_font_face(fontname, cairo.FontSlant.NORMAL, cairo.FontWeight.BOLD)
        cr.set_font_size(fontsize)
      
        text = f"{self.ppsTtl} "
        cr.move_to(80, 20)

        if self.LostPckts>0:
                cr.set_source_rgb(1, 0.5, 0.5)  # Red color
        elif self.RecoveredPckts>5:
                cr.set_source_rgb(1, 1, 0.5)  # Yellow color
        else:
             cr.set_source_rgb(1, 1, 1)  # White color

        self.outlined(cr, f"{self.LostPckts:3} {self.RecoveredPckts:3}", 80, 20)        

        cr.select_font_face("Arial", cairo.FontSlant.ITALIC, cairo.FontWeight.NORMAL) #cairo.FontSlant.ITALIC
        cr.set_source_rgb(1, 1, 1)  # White color
        cr.set_font_size(fontsize-4)
        cr.move_to(210, 20)        
        #cr.show_text(f"{self.BitrateTotal/(1024*1024):.1f}→{8*self.bpsTtl/(1024*1024):.1f}Mb/s")
        self.outlined(cr, f"{self.BitrateTotal/(1024*1024):.1f}→{8*self.bpsTtl/(1024*1024):.1f}Mb/s", 210, 20)

        row=0
        cr.select_font_face(fontname, cairo.FontSlant.NORMAL, cairo.FontWeight.NORMAL)
        cr.set_font_size(fontsize)
        for card_index, stats in self.channel_stats.items():
            cr.set_source_rgb(0.7, 1, 0.7)  # Greenish color
            cr.move_to(10,(fontsize+4)*row + 42) # {card_index}:
            if stats['pckt_lost']>30:
                cr.set_source_rgb(1, 0, 0)  # Red color
            elif stats['pckt_lost']>2:
                cr.set_source_rgb(1, 1, 0.7)  # yellow color
            
            if stats['pckt_lost']>250:
                pcktlost=" ~~"
            else:
                pcktlost=f"{-stats['pckt_lost']:>4}"

            clr = cr.get_source()
            #cr.show_text(f"-{stats['rssi']}㏈ {stats['pckt_received']:4}-{pcktlost}") #㏈
            #--﹣
            self.outlined(cr, f"{stats['rssi']}㏈ {stats['pckt_received']:4}{pcktlost}", 10,(fontsize+2)*row + 42)

            row=row+1
                        

        gdk_cairo_surface = Gdk.cairo_surface_create_from_pixbuf(self.icon_pixbuf, 1, widget.get_window())

        # Draw the image at the specified coordinates
        cr.set_source_surface(gdk_cairo_surface, 10, 3)
        cr.paint()
        self.set_keep_above(True)
        self.present()

        #PangoCairo.show_layout(cr, layout)

        return True

# Initialize GTK
if __name__ == "__main__":
    win = wfbOSDWindow()
    Gtk.main()
