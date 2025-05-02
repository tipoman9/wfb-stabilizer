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
from osd_overlay import wfbOSDWindow 
from Xlib import display, X
from Xlib.protocol import request
from wfb_osd import wfb_srv_osd
from gi.repository import Gtk


#OPENCV_VIDEOIO_DEBUG=1

# Usage: python ejo_wfb_stabilizer.py [optional video file]
# press "Q" to quit

#################### USER VARS ######################################

# set to 1 to display full screen -- doesn't actually go full screen if your monitor rez is higher than stream rez which it probably is. TODO: monitor resolution detection
#showFullScreen = 1
showFullScreen = 1

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
#SRC = 'udpsrc port=5600 caps="application/x-rtp, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H265" ! rtph265depay ! avdec_h265 ! decodebin ! videoconvert ! appsink sync=false '

#Hardware decoding on a Intel CPU, video without audio
#SRC = 'udpsrc port=5600 buffer-size=65536 caps="application/x-rtp, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H265" ! rtph265depay ! queue max-size-buffers=1 ! vaapih265dec ! videoconvert ! appsink sync=false '
#Hardware decoding on a Intel CPU, video with audio
#SRC = 'udpsrc port=5600 buffer-size=65536 caps="application/x-rtp, payload=97, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H265" ! rtpjitterbuffer ! rtph265depay ! queue max-size-buffers=1 ! vaapih265dec ! videoconvert ! appsink sync=false '
SRC = 'udpsrc port=5600 caps="application/x-rtp, payload=97, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H265" ! rtpjitterbuffer latency=100 mode=0 max-misorder-time=200 max-dropout-time=100 max-rtcp-rtp-time-diff=100 ! rtph265depay ! queue max-size-buffers=1 ! vaapih265dec ! videoconvert ! appsink sync=false '
#this will drop frames when video fps is higher than supported
SRC = (
    'udpsrc port=5600 caps="application/x-rtp, payload=97, media=(string)video, '
    'clock-rate=(int)90000, encoding-name=(string)H265" ! '
    'rtpjitterbuffer latency=50 mode=0 ! '
    'rtph265depay ! queue ! vaapih265dec ! videoconvert ! '
    'appsink sync=false drop=true max-buffers=1'
)

# Below is for author's Ubuntu PC with nvidia/cuda stuff running WFB-NG locally (no groundstation RPi). Requires a lot of fiddling around compiling opencv w/ cuda support
#SRC = 'udpsrc port=5600 caps = "application/x-rtp, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H264, payload=(int)96" ! rtph264depay !  h264parse ! nvh264dec ! videoconvert ! appsink sync=false'

######################################################################


# Command string with quotes
#command_string = 'gnome-terminal -e \'/home/home/qopenhd25/build-QOpenHD-Desktop_Qt_5_15_2_GCC_64bit-Debug/debug/QOpenHD\''
#command_string = '/home/home/qopenhd.sh transparent'

#Path to qOpenHD to start it and bring it to front to get OSD , empty if not
OSDexecutable = '/home/home/qopenhd25/build-QOpenHD-Desktop_Qt_5_15_2_GCC_64bit-Debug/debug/QOpenHD'
#qOpenHDexecutable = ""
qOpenHDdir='/home/home/qopenhd25/build-QOpenHD-Desktop_Qt_5_15_2_GCC_64bit-Debug/debug/'

#set lower refresh rate of msposd to free some resources for OpenCV drawing
MSPOSDexecutable = [
    "/home/home/src/msposd/msposd",
    "--master", "127.0.0.1:14550",   	
    "--osd",
    "-r", "20",
    "--ahi", "3",
    "--matrix", "11"
    #,"-v"
]	


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
	global AbortNow, enableStabization, cropping_percent,ScaleModeRequest, count
	try:
		#print(f'Key {key.char} pressed')
		if key.char.lower() == 'q' or key == keyboard.Key.esc: 
			AbortNow = True
		if key.char.lower() == 's' or key == keyboard.Key.space: 
			enableStabization = not enableStabization
			count=0 #need to reset it 
		if key.char.lower() == 'b' or key == keyboard.Key.tab: 
			cropping_percent =  5 if cropping_percent == 0 else 0
			print("Crooping : {cropping_percent}")
	except AttributeError:
		print(f'Special key {key} pressed')
		if key == keyboard.Key.space: 
			count=0 #need to reset it 
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
				#print("Frame Queue size:" + f"{frame_queue.qsize()}")


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


def get_msp_window():
	window_id = os.environ.get('MSP_WINDOW_ID')
	if window_id:
		window_id = int(window_id)
		print(f"Window ID: {window_id}")
		return window_id
	else:
		print("Window ID not found in environment.")
		return None


def bring_window_to_front(window):    
    d = display.Display()
    
    window.configure(stack_mode=X.Above)

    # Set input focus to the window
    window.set_input_focus(X.RevertToParent, X.CurrentTime)

    # Flush the display to ensure the commands are applied immediately
    d.sync()

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
#if len(sys.argv) == 2:
#	SRC=sys.argv[1]

# SRC="/home/home/Videos/8mbit.mov"

video = cv2.VideoCapture(SRC, cv2.CAP_GSTREAMER) 

# Check if the VideoCapture object was successfully created
if not video.isOpened():
 # Get extended error information
	error_msg = video.get(cv2.CAP_PROP_POS_MSEC)
	print(f"Error: Unable to open video source. Extended error: {error_msg}")
	print(cv2.getBuildInformation())
    # Handle the error or exit the program if necessary
	exit()

#This will show link statistics window on top
if len(sys.argv) >= 2 and sys.argv[1].lower()=="noosd" :
	OSDexecutable="" # StopqOPenHD
	win = wfbOSDWindow() # Show my stats window

if len(sys.argv) >= 2 and sys.argv[1].lower()=="msposd" :
	#qOpenHDexecutable="/home/home/src/msposd/msposd  --master 127.0.0.1:14550 --baudrate 115200 --osd -r 50 --ahi 3 --matrix 11 -v"
	OSDexecutable=""
	OSDexecutable = MSPOSDexecutable    
	  
	if os.path.exists('/tmp/wfb_server_started'):
		#win = wfb_srv_osd()				
		current_dir = os.path.dirname(os.path.abspath(__file__))		
		osd_script_path = os.path.join(current_dir, "wfb_osd.py")
		subprocess.Popen(["python3", osd_script_path])	
	else:
		win = wfbOSDWindow(14551)
	

#MultiThread gives 30% performance increase !
SingleThread=False
#SingleThread=True

frames_ttl=0
# Global shared frame and lock
shared_frame = None
frame_lock = threading.Lock()

#vvvvv  =========>--- Displaying in separate thread! <===============---- vvvv
window_name=""
 
def display_frames():
	global shared_frame, frame_lock, window_name, process_id, AbortNow, frames_ttl
	
	while not AbortNow:
		#if not frame_queue.empty():
		frame = None
		with frame_lock:
			if shared_frame is not None:
				frame = shared_frame.copy()

		if frame is not None:			
			if True: #frames_ttl%1==16:
				cv2.namedWindow(window_name,cv2.WINDOW_NORMAL)					
				if showFullScreen == 1 and frames_ttl%64==0 :				
					cv2.setWindowProperty(window_name, cv2.WND_PROP_FULLSCREEN,cv2.WINDOW_FULLSCREEN)
				#if process_id != None and frames_ttl%16==0 : 
				#	bring_to_foreground(process_id) # Bring the window to the foreground	
				# 	MSP_window = get_msp_window()
				# 	if MSP_window: 
				# 		bring_window_to_front(MSP_window)						
							
			cv2.imshow(window_name, frame)
			frames_ttl+=1
		
		if cv2.pollKey() & 0xFF == ord('q') or AbortNow:
			break	 
		#key = cv2.waitKey(1)
		#if (key & 0xFF == ord('q')) or AbortNow :
		#	break


if not SingleThread:
	display_thread = threading.Thread(target=display_frames, daemon=True)
    
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
while True:	
	#grab, frame = video.read()
	i(f"Frame start",1)   #debug_step:1 : {time.time():.3f}
	startedwaiting4frame=time.time()
	overloaded=True;
	frames_ttl+=1

	# this will skip frames if we do not wait at least 1ms for them!
	# this way we wont get distorted video!
	while overloaded :
		overloaded=False;
		#while not video.grab():
		#	i("Skip every second frame!")
		# Even the above code will not help, OpenCV retrieving is the bottleneck, about 40fps on my system.
		if not video.grab():			
			i("No frame grabbed, trying again...")
			continue			

		if enableStabization:		 
			waited=(time.time()-startedwaiting4frame)*1000
			if waited<1 and frames_ttl>50 : # if frame awaits us, we are too slow			
				ttlwaited+=1
				if ttlwaited>2: # If we have three successive frames that we were not able to handle...
					overloaded=True	
					dropped_frames+=1
					print(f"Skipped frame {waited:.1f}")
			else:			
				#ttlwaited=0 # this way we can drop max FPS/3 frames.
				if ttlwaited>0:
					ttlwaited-=1
		# else:
		# 	#This won't help, usually the system can not retrieve frames so fast.
		# 	if frame_queue.qsize()>1:	#if the system can't display fast enough
		# 		dropped_frames+=1
		# 		video.grab() #This will get the next frame, so max 50% skipped frames in direct mode
			
		i(f"Grabbed ")	
		grab, frame = video.retrieve() # Receive or discard

			

	i(f"retrieved")

	if grab is not True:
		exit() 
	if enableStabization :
		SetScaleMode()
	res_w_orig = frame.shape[1]
	res_h_orig = frame.shape[0]
	res_w = int(res_w_orig * downSample)
	res_h = int(res_h_orig * downSample)
	top_left= [int(res_h/roiDiv),int(res_w/roiDiv)]
	bottom_right = [int(res_h - (res_h/roiDiv)),int(res_w - (res_w/roiDiv))]
	frameSize=(res_w,res_h)
	Orig = frame
	if enableStabization and downSample != 1:
		frame = cv2.resize(frame, frameSize) # downSample if applicable
		i(f"Scaled down")
	currFrame = frame
	
	if enableStabization :
		currGray = cv2.cvtColor(currFrame, cv2.COLOR_BGR2GRAY)
		currGray = currGray[top_left[0]:bottom_right[0], top_left[1]:bottom_right[1]  ] #select ROI
		i(f"converted to gray")

		if prevFrame is None:
			prevOrig = frame
			prevFrame = frame
			prevGray = currGray
	
		if (grab != True) | (prevFrame is None):
			exit()

	if enableStabization :
		if showrectROI == 1:
			cv2.rectangle(prevOrig,(top_left[1],top_left[0]),(bottom_right[1],bottom_right[0]),color = (211,211,211),thickness = 1)
		# Not in use, save for later
		#gfftmask = np.zeros_like(currGray)
		#gfftmask[top_left[0]:bottom_right[0], top_left[1]:bottom_right[1]] = 255
		DoFrameCalc=True 
		#DoFrameCalc= not DoFrameCalc
		if DoFrameCalc :
			#prevPts = cv2.goodFeaturesToTrack(prevGray,maxCorners=400,qualityLevel=0.01,minDistance=30,blockSize=3)
			prevPts = cv2.goodFeaturesToTrack(prevGray,maxCorners=400,qualityLevel=0.01,minDistance=30 * downSample,blockSize=3)			
			i(f"goodFeaturesToTrack")
			if prevPts is not None:
				currPts, status, err = cv2.calcOpticalFlowPyrLK(prevGray,currGray,prevPts,None,**lk_params)	
				i(f"calcOpticalFlowPyrLK")
				if downSample!=1:			
					currPts=Scale_Coordinates(currPts,downSample) # NEW !!!
					prevPts=Scale_Coordinates(prevPts,downSample) # NEW !!!
					i(f"Points_Scaled")
					
				assert prevPts.shape == currPts.shape
				idx = np.where(status == 1)[0]
				# Add orig video resolution pts to roi pts
				prevPts = prevPts[idx] + np.array([int(res_w_orig/roiDiv),int(res_h_orig/roiDiv)]) 
				currPts = currPts[idx] + np.array([int(res_w_orig/roiDiv),int(res_h_orig/roiDiv)])				

				if showTrackingPoints == 1:									
					for pT in prevPts:
						cv2.circle(prevOrig, (int(pT[0][0]),int(pT[0][1])) ,5,(211,211,211))

				if prevPts.size & currPts.size:
					m, inliers = cv2.estimateAffinePartial2D(prevPts, currPts)
				if m is None:
					m = lastRigidTransform
				# Smoothing
				dx = m[0, 2]
				dy = m[1, 2]
				da = np.arctan2(m[1, 0], m[0, 0])
			else:
				dx = 0
				dy = 0
				da = 0

			x += dx
			y += dy
			a += da

			#Trying to avoid screen going too far away :)
			if abs(dx)>res_w_orig * max_windows_offset or abs(dy)>res_h_orig * max_windows_offset:				
				print("Out of view : {dx}:{dy}") ; dx = 0 ; dy = 0 ; da = 0 ; x = 0 ; y = 0 ;a = 0 				
				X_estimate = np.zeros((1,3), dtype="float") ; P_estimate = np.ones((1,3), dtype="float") ;prevPts=None

			Z = np.array([[x, y, a]], dtype="float")
			if count == 0:
				X_estimate = np.zeros((1,3), dtype="float")
				P_estimate = np.ones((1,3), dtype="float")
			else:
				X_predict = X_estimate
				P_predict = P_estimate + Q
				K = P_predict / (P_predict + R)
				X_estimate = X_predict + K * (Z - X_predict)
				P_estimate = (np.ones((1,3), dtype="float") - K) * P_predict
				K_collect.append(K)
				P_collect.append(P_estimate)
			diff_x = X_estimate[0,0] - x
			diff_y = X_estimate[0,1] - y
			diff_a = X_estimate[0,2] - a
			dx += diff_x
			dy += diff_y
			da += diff_a
			m = np.zeros((2,3), dtype="float")
			m[0,0] = np.cos(da)
			m[0,1] = -np.sin(da)
			m[1,0] = np.sin(da)
			m[1,1] = np.cos(da)
			m[0,2] = dx
			m[1,2] = dy
			#DoFrameCalc

		fS = cv2.warpAffine(prevOrig, m, (res_w_orig,res_h_orig)) # apply magic stabilizer sauce to frame
		i(f"warpAffine passed")
		s = fS.shape
		T = cv2.getRotationMatrix2D((s[1]/2, s[0]/2), 0, zoomFactor)		
		f_stabilized = cv2.warpAffine(fS, T, (s[1], s[0]))

		if cropping_percent>0:
			f_stabilized = crop_and_overlay(f_stabilized,cropping_percent)

		i(f"warpAffine2 passed")
	else :
		f_stabilized=Orig

	window_name=f'Stabilized:{res_w_orig}x{res_h_orig}'
	offsetX=120
	drawtext(f_stabilized, f"FPS:"+fps,240 + offsetX,20)
	drawtext(f_stabilized, f"Dropped:{dropped_frames_screen}",320 + offsetX,20)
	drawtext(f_stabilized, f"Load: {stab_load_screen:.0f}%",440 + offsetX,20)
	
	
	drawtext(f_stabilized, f"Stab:"  + ("ON" if enableStabization == True else "OFF"),560 + offsetX,20)
	drawtext(f_stabilized, f"Mode:"+ ("Slow" if downSample == 1 else "Fast"),660 + offsetX,20)
	#frameslag=frame_queue.qsize()
	#if frameslag>0:
	#	drawtext(f_stabilized, f"FramesLag:"+ f"{frameslag}",770 + offsetX,20)
	 
	
	i(f"Frame ready")
	if SingleThread:		
		cv2.namedWindow(window_name,cv2.WINDOW_NORMAL)
	
		if maskFrame == 1:
			mask = np.zeros(f_stabilized.shape[:2], dtype="uint8")
			cv2.rectangle(mask, (100, 200), (1180, 620), 255, -1)
			f_stabilized = cv2.bitwise_and(f_stabilized, f_stabilized, mask=mask)
		if showFullScreen == 1:
			cv2.setWindowProperty(window_name, cv2.WND_PROP_FULLSCREEN,cv2.WINDOW_FULLSCREEN)
			if process_id != None: 
				bring_to_foreground(process_id) # Bring the window to the foreground						
			
		cv2.imshow(window_name, f_stabilized)
		i(f"imshow completed")		
		if showUnstabilized == 1:
			cv2.imshow("Unstabilized ROI",prevGray)

		
		#if cv2.waitKey(delay_time) & 0xFF == ord('q'):
		if count%2==1 and cv2.pollKey() & 0xFF == ord('q') or AbortNow:
			break
	else :		 
		# Update shared frame with lock
		with frame_lock:
			shared_frame = f_stabilized
		if not display_thread.is_alive():
			print(f"Exiting...")
			break

	i(f"Cycle completed")

	if process==None  and showFullScreen == 1:
		# Start your process
		if OSDexecutable!="":
			process = subprocess.Popen(OSDexecutable)
			# run qOpenHD as a local user so that config is in ~/.config/qOpenHD
			# process = subprocess.Popen(['sudo', '-u', "home", qOpenHDexecutable])
			#process.wait(100) # Wait for a moment to ensure the window is created
			
			time.sleep(1) 
			process_id = process.pid # Get the process ID (PID) of the last process			
			bring_to_foreground(process_id) # Bring the window to the foreground

	
	if enableStabization :
		prevOrig = Orig
		prevGray = currGray
		prevFrame = currFrame		
		lastRigidTransform = m

	count += 1
	#else:
	#	exit()
 
video.release()
 
cv2.destroyAllWindows()

if process is not None:
	process.terminate()
	# Wait for an additional 5 seconds for the process to respond to the terminate signal
	try:
		process.wait(timeout=2)
	except subprocess.TimeoutExpired:
		# If the process is still running, forcefully kill it
		#process.kill()
		#os.killpg(process.pid, signal.SIGTERM)  # Or signal.SIGKILL		
		# When you need to terminate the process
		kill_command = ['sudo', 'killall -9 qOpenHD']
		subprocess.run(kill_command)
		os.killpg(process.pid, signal.SIGKILL)  # Or signal.SIGKILL		

else:
    print("No qOpenHD to close!")

print("End.")

