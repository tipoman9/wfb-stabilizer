#!/usr/bin/python3
# Author: ejowerks
# Version 0.00000000001 Proof of Concept Released 4/3/2023
# Open Source -- Do what you wanna do
# Thanks to https://github.com/trongphuongpro/videostabilizer 
# 2024  improved by TipoMan9

import cv2
import numpy as np
import sys
import subprocess
import shlex
import time

import sys
import os
import struct
import psutil
import threading
import queue
import os
import signal

from pynput import keyboard

from threading import Thread


#OPENCV_VIDEOIO_DEBUG=1

# Usage: python ejo_wfb_stabilizer.py [optional video file]
# press "Q" to quit

#################### USER VARS ######################################

# set to 1 to display full screen -- doesn't actually go full screen if your monitor rez is higher than stream rez which it probably is. TODO: monitor resolution detection
# showFullScreen = 1
showFullScreen = 0

# Decreases stabilization latency at the expense of accuracy. Set to 1 if no downsamping is desired. 
# Example: downSample = 0.5 is half resolution and runs faster but gets jittery
#downSample = 1
downSample = 0.5

#Zoom in so you don't see the frame bouncing around. zoomFactor = 1 for no zoom
zoomFactor = 1 #0.9

# pV and mV can be increased for more smoothing #### start with pV = 0.01 and mV = 2 
processVar=0.03
measVar=2

#for downSample = 0.5
#processVar=0.010
#measVar=8


# If test video plays too fast then increase this until it looks close enough. Varies with hardware. 
# LEAVE AT 1 if streaming live video from WFB (unless you like a delay in your stream for some weird reason)
delay_time = 0
#delay_time = 1


######################## Region of Interest (ROI) ###############################
# This is the portion of the frame actually being processed. Smaller ROI = faster processing = less latency
#
# roiDiv = ROI size divisor. Minimum functional divisor is about 3.0 at 720p input. 4.0 is best for solid stabilization.
# Higher FPS and lower resolution can go higher in ROI (and probably should)
# Set showrectROI and/or showUnstabilized vars to = 1 to see the area being processed. On slower PC's 3 might be required if 720p input
#roiDiv = 3.5
roiDiv = 3.5


# set to 1 to show the ROI rectangle 
showrectROI = 0

#showTrackingPoints # show tracking points found in frame. Useful to turn this on for troubleshooting or just for funzies. 
showTrackingPoints = 0

# set to 1 to show unstabilized B&W ROI in a window
showUnstabilized = 0

# maskFrame # Wide angle camera with stabilization warps things at extreme edges of frame. This helps mask them without zoom. 
# Feels more like a windshield. Set to 0 to disable or find the variable down in the code to adjust size of mask
maskFrame = 0

#raise to stop the program
AbortNow=False

#Switch on/off
enableStabization=False

#Max deflection of the image as a percentage of screen. Prevents screen going away when video suddenly drops. Usually between : 0.2 to 0.5
max_windows_offset = 0.3

#How much to crop and put a black border so that image bouncing is less visible
cropping_percent=0

######################## Video Source ###############################

# Your stream source. Requires gstreamer libraries 
# Can be local or another source like a GS RPi
# Check the docs for your wifibroadcast variant and/or the Googles to figure out what to do. 

# Below should work on most PC's with gstreamer  -- ###  #### #### Without hardware acceleration you may need to reduce your stream to 920x540 ish #### #### ###
#SRC = 'udpsrc port=5600 caps = "application/x-rtp, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H264, payload=(int)96" ! rtph264depay ! decodebin ! videoconvert ! appsink sync=false'

#software decoding
#SRC = 'udpsrc port=5600 buffer-size=65536 caps="application/x-rtp, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H265" ! rtph265depay ! avdec_h265 ! decodebin ! videoconvert ! appsink sync=false '

#Hardware decoding on a Intel CPU, video without audio
#SRC = 'udpsrc port=5600 buffer-size=65536 caps="application/x-rtp, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H265" ! rtph265depay ! queue max-size-buffers=1 ! vaapih265dec ! videoconvert ! appsink sync=false '
#Hardware decoding on a Intel CPU, video with audio
#SRC = 'udpsrc port=5600 buffer-size=65536 caps="application/x-rtp, payload=97, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H265" ! rtpjitterbuffer ! rtph265depay ! queue max-size-buffers=1 ! vaapih265dec ! videoconvert ! appsink sync=false '
SRC = 'udpsrc port=5600 buffer-size=65536 caps="application/x-rtp, payload=97, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H265" ! rtpjitterbuffer latency=100 mode=0 max-misorder-time=200 max-dropout-time=100 max-rtcp-rtp-time-diff=100 ! rtph265depay ! queue max-size-buffers=1 ! vaapih265dec ! videoconvert ! appsink sync=false '

# Below is for author's Ubuntu PC with nvidia/cuda stuff running WFB-NG locally (no groundstation RPi). Requires a lot of fiddling around compiling opencv w/ cuda support
#SRC = 'udpsrc port=5600 caps = "application/x-rtp, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H264, payload=(int)96" ! rtph264depay !  h264parse ! nvh264dec ! videoconvert ! appsink sync=false'

######################################################################


# Command string with quotes
#command_string = 'gnome-terminal -e \'/home/home/qopenhd25/build-QOpenHD-Desktop_Qt_5_15_2_GCC_64bit-Debug/debug/QOpenHD\''
#command_string = '/home/home/qopenhd.sh transparent'

#Path to qOpenHD to start it and bring it to front to get OSD , empty if not
qOpenHDexecutable = '/home/home/qopenhd25/build-QOpenHD-Desktop_Qt_5_15_2_GCC_64bit-Debug/debug/QOpenHD'
#qOpenHDexecutable = ""

qOpenHDdir='/home/home/qopenhd25/build-QOpenHD-Desktop_Qt_5_15_2_GCC_64bit-Debug/debug/'



#Set qOpenHD params to transparent mode, no video
sed_commands = (#set qOpenHD to h264 to free cpu
	"sed -i 's/^qopenhd_primary_video_codec=.*/qopenhd_primary_video_codec=0/' /home/home/.config/OpenHD/QOpenHD.conf &&"
    "sed -i 's/^dev_force_show_full_screen=.*/dev_force_show_full_screen=true/' /home/home/.config/OpenHD/QOpenHD.conf &&"
    "sed -i 's/^qopenhd_primary_video_rtp_input_port=.*/qopenhd_primary_video_rtp_input_port=5599/' /home/home/.config/OpenHD/QOpenHD.conf"
)
 
subprocess.run(sed_commands, shell=True)

#not needed , tested only
def set_cpu_affinity(core_number):
    pid = os.getpid()
    p = psutil.Process(pid)

    try:
        # Get the list of available CPU cores
        available_cores = list(range(psutil.cpu_count()))

        # Check if the specified core_number is valid
        if core_number not in available_cores:
            raise ValueError(f"Invalid core number. Available cores: {available_cores}")

        # Set the CPU affinity to the specified core
        p.cpu_affinity([core_number])
        print(f"CPU affinity set to core {core_number}")

    except Exception as e:
        print(f"Error: {e}")

ScaleModeRequest=downSample
#Global key hook handler
def on_press(key):
	global AbortNow, enableStabization, cropping_percent,ScaleModeRequest
	try:
		#print(f'Key {key.char} pressed')
		if key.char.lower() == 'q' or key == keyboard.Key.esc: 
			AbortNow = True
		if key.char.lower() == 's' or key == keyboard.Key.space: 
			enableStabization = not enableStabization
		if key.char.lower() == 'b' or key == keyboard.Key.tab: 
			cropping_percent =  5 if cropping_percent == 0 else 0
			print("Crooping : {cropping_percent}")
	except AttributeError:
		print(f'Special key {key} pressed')
		if key == keyboard.Key.space: 
			enableStabization = not enableStabization
		if key == keyboard.Key.esc:
			AbortNow = True
		if key == keyboard.Key.tab: 			
			ScaleModeRequest = 0.5 if ScaleModeRequest == 1 else 1
			

def on_release(key):
    if key == keyboard.Key.esc:
        # Stop listener
        return False

# Global key hook init
listener = keyboard.Listener(on_press=on_press, on_release=on_release)
listener.start()

# Statistics record	
class PerfCounter:
    def __init__(self, name, value):
        self.name = name        
        self.ttl = value                       
        self.min = value        
        self.max = value 
        self.avg = value
        self.count=1
        self.created=time.time()

    def add(self, value):
        self.count += 1
        if value<self.min:
            self.min=value
        if value>self.max:
            self.max=value
        self.ttl+=value
        self.avg=self.ttl/self.count
			
perfs = {}

lastticks=time.time()
dropped_frames=0
dropped_frames_screen=0
showdebug=1
currentstep=0
fps=""
procstart=time.time()
stab_load_screen=0


#display debug info in console
def i(str, step=0):
	global lastticks,currentstep,procstart, perfs, dropped_frames_screen, dropped_frames,stab_load_screen, fps
	suffix=""
	if showdebug==1 :
		if step==1 :
			if step in perfs and (time.time()-perfs[step].created)>1:
				print()
				for index, (key, value) in enumerate(perfs.items()):
					#print(f"Counter {index + 1}: {key}")
					print(f"{key}"+ " : " + value.name[:20].ljust(20) + " min:" + f"{value.min*1000:.1f}" 
					+ " max:" + f"{value.max*1000:.1f}".ljust(6) + " avg:" + f"{value.avg*1000:.1f}".ljust(6)) 
				 
				dropped_frames_screen=	dropped_frames
				dropped_frames=0
				stab_load_screen =  round(100*(30 - perfs[2].avg*1000) /30,0) # Frame_time - free time/Frame_time
				fps=f"{perfs[1].count}"
				perfs = {}
				print("Frame Queue size:" + f"{frame_queue.qsize()}")


			currentstep=1			
			diff=time.time() - procstart * 1000
			suffix = f" | {(time.time() - procstart) * 1000:.1f}"
			procstart=time.time()			

		if step == 0 :
			currentstep=currentstep+1
		global lastticks
		elapsed=time.time()-lastticks
		lastticks=time.time()
		#uncomment for details in console
		#print(f"{currentstep}"+ " : " + str + " = " + f"{elapsed*1000}") #//f"debug_step:8 : {time.time():.4f}")
		p = PerfCounter(str, round(elapsed * 1000))
		if currentstep in perfs:			
			perfs[currentstep].add(elapsed)						
		else:
			perfs[currentstep]=PerfCounter(str,elapsed)
		
		#print(f"{currentstep} : {str[:20].ljust(20)} = {elapsed * 1000:.1f}" + suffix)

#Draw simple text over image
def drawtext(surface, str, x, y):
	# Add text to the image
	text = "Hello, OpenCV!"
#	font = cv2.FONT_HERSHEY_SIMPLEX
	font = cv2.FONT_HERSHEY_DUPLEX
	position = (x, y)  # (x, y) coordinates of the top-left corner of the text
	font_scale = 0.6
	font_color = (0, 0, 255)  # BGR color (white in this case)
	thickness = 1

	cv2.putText(surface, str, position, font, font_scale, font_color, thickness)

# A basic attempt to do cropping, may slow down, needs optimization
def crop_and_overlay(frame, margin_percent=5):
    # Assuming 'frame' is your original frame    

    # Calculate dimensions for the margin crop
    margin_height = int(frame.shape[0] * (margin_percent / 100))
    margin_width = int(frame.shape[1] * (margin_percent / 100))

    # Calculate dimensions for the center region
    center_height = frame.shape[0] - 2 * margin_height
    center_width = frame.shape[1] - 2 * margin_width

    # Create a black background frame with the original dimensions
    black_frame = np.zeros((frame.shape[0], frame.shape[1], 3), dtype=np.uint8)

    # Crop the frame with a margin and center the result
    cropped_frame = frame[margin_height:margin_height + center_height,
                                     margin_width:margin_width + center_width]

    # Calculate the position to place the cropped frame in the center of the black frame
    position_y = (frame.shape[0] - center_height) // 2
    position_x = (frame.shape[1] - center_width) // 2

    # Overlay the cropped frame onto the black frame
    black_frame[position_y:position_y + center_height,
                position_x:position_x + center_width] = cropped_frame

    return black_frame


def bring_to_foreground(process_id):
    try:
        subprocess.run(["wmctrl", "-ia", str(process_id)])
    except Exception as e:
        print(f"Error bringing window to foreground: {e}")

process = None
process_id = None 

lk_params = dict( winSize  = (15,15),maxLevel = 3,criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 10, 0.03))
count = 0
a = 0
x = 0
y = 0
Q = np.array([[processVar]*3])
R = np.array([[measVar]*3])
K_collect = []
P_collect = []
prevFrame = None

# open local video file, warning no filetype validation 
if len(sys.argv) == 2:
	SRC=sys.argv[1]

# SRC="/home/home/Videos/8mbit.mov"

video = cv2.VideoCapture(SRC) 

# Check if the VideoCapture object was successfully created
if not video.isOpened():
 # Get extended error information
	error_msg = video.get(cv2.CAP_PROP_POS_MSEC)
	print(f"Error: Unable to open video source. Extended error: {error_msg}")
	print(cv2.getBuildInformation())
    # Handle the error or exit the program if necessary
	exit()

#MultiThread gives 30% performance increase !
#SingleThread=False
SingleThread=True

frames_ttl=0
#vvvvv  --- Displaying in separate thread! ---- vvvv
window_name=""
frame_queue = queue.Queue()
def display_frames(frame_queue):
	global window_name, process_id, AbortNow, frames_ttl
	
	while True:
		if not frame_queue.empty():
			frame = frame_queue.get()
			if frames_ttl%1==0:
				cv2.namedWindow(window_name,cv2.WINDOW_NORMAL)					
				if showFullScreen == 1:
					cv2.setWindowProperty(window_name, cv2.WND_PROP_FULLSCREEN,cv2.WINDOW_FULLSCREEN)
					if process_id != None and frames_ttl%16==0 : 
						bring_to_foreground(process_id) # Bring the window to the foreground		
			
			cv2.imshow(window_name, frame)
			frames_ttl+=1
		
		if frames_ttl%2==1 and cv2.pollKey() & 0xFF == ord('q') or AbortNow:
			break	 
#		if ((frames_ttl%16==0) and cv2.waitKey(1) & 0xFF == ord('q')) or AbortNow :
#			break

frame_queue = queue.Queue()
if not SingleThread:
	display_thread = threading.Thread(target=display_frames, args=(frame_queue,))
    
	display_thread.start()
# ^^^^^^^^ Displaying in separate thread!  ^^^^^^^^^
ttlwaited=0

DoFrameCalc = False

def Scale_Coordinates(showPts, multiplier):
    if multiplier==1:
        return showPts
    x_coords = showPts[:, 0, 0]
    y_coords = showPts[:, 0, 1]    
    x_coords *= 1/multiplier
    y_coords *= 1/multiplier
    showPts[:, 0, 0] = x_coords
    showPts[:, 0, 1] = y_coords

    return showPts

def SetScaleMode():	
	global dx, dy, da , x , y , a, X_estimate,P_estimate,prevPts,prevGray,currGray,downSample,ScaleModeRequest,downSample,Q,R,prevFrame
	#need to change these params to keep the same processing
	if ScaleModeRequest!= downSample :	
		downSample=	ScaleModeRequest
		Q = np.array([[processVar*downSample]*3])
		R = np.array([[measVar/downSample]*3])		
		dx = 0 ; dy = 0 ; da = 0 ; x = 0 ; y = 0 ;a = 0 				
		X_estimate = np.zeros((1,3), dtype="float") ; P_estimate = np.ones((1,3), dtype="float") ;prevPts=None
		prevGray=None; currGray=None ; prevFrame=None
print("Waiting for video stream...")
 

 
while False:
    ret, frame = video.read()
    if not ret:
        print("Empty frame")
        break

    # Display the frame
    cv2.imshow('Frame', frame)

    if cv2.pollKey() & 0xFF == ord('q'):
        break


class VideoCaptureAsync:
    def __init__(self, src=0):
        self.src = src
        self.cap = cv2.VideoCapture(self.src)
        self.q = queue.Queue()
        self.running = True

    def start(self):
        Thread(target=self.update, daemon=True, args=()).start()
        return self

    def update(self):
        while self.running:
            if not self.q.full():
                ret, frame = self.cap.read()
                if not ret:
                    self.running = False
                else:
                    self.q.put(frame)
            else:
                time.sleep(0.01)  # Tiny sleep to avoid locking

    def read(self):
        return self.q.get()

    def stop(self):
        self.running = False
        self.cap.release()

# Usage
video_stream = VideoCaptureAsync(SRC).start()

while True:
    frame = video_stream.read()
    cv2.imshow('Frame', frame)
    if cv2.pollKey() == ord('q'):
        break

video.release()
cv2.destroyAllWindows()
 
	 

print("End.")

