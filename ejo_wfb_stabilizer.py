#!/usr/bin/python3
# Author: ejowerks
# Version 0.00000000001 Proof of Concept Released 4/3/2023
# Open Source -- Do what you wanna do
# Thanks to https://github.com/trongphuongpro/videostabilizer 

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


# Usage: python ejo_wfb_stabilizer.py [optional video file]
# press "Q" to quit

#################### USER VARS ######################################
# Decreases stabilization latency at the expense of accuracy. Set to 1 if no downsamping is desired. 
# Example: downSample = 0.5 is half resolution and runs faster but gets jittery
downSample = 1

#Zoom in so you don't see the frame bouncing around. zoomFactor = 1 for no zoom
zoomFactor = 0.9

# pV and mV can be increased for more smoothing #### start with pV = 0.01 and mV = 2 
processVar=0.03
measVar=2

# set to 1 to display full screen -- doesn't actually go full screen if your monitor rez is higher than stream rez which it probably is. TODO: monitor resolution detection
#showFullScreen = 1
showFullScreen = 0

# If test video plays too fast then increase this until it looks close enough. Varies with hardware. 
# LEAVE AT 1 if streaming live video from WFB (unless you like a delay in your stream for some weird reason)
#delay_time = 0
delay_time = 1


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

######################## Video Source ###############################

# Your stream source. Requires gstreamer libraries 
# Can be local or another source like a GS RPi
# Check the docs for your wifibroadcast variant and/or the Googles to figure out what to do. 

# Below should work on most PC's with gstreamer  -- ###  #### #### Without hardware acceleration you may need to reduce your stream to 920x540 ish #### #### ###
#SRC = 'udpsrc port=5600 caps = "application/x-rtp, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H264, payload=(int)96" ! rtph264depay ! decodebin ! videoconvert ! appsink sync=false'

#software decoding
SRC = 'udpsrc port=5600 buffer-size=65536 caps="application/x-rtp, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H265" ! rtph265depay ! avdec_h265 ! decodebin ! videoconvert ! appsink sync=false '

SRC = 'udpsrc port=5600 buffer-size=65536 caps="application/x-rtp, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H265" ! rtph265depay ! queue max-size-buffers=1 ! vaapih265dec ! videoconvert ! appsink sync=false '

# Below is for author's Ubuntu PC with nvidia/cuda stuff running WFB-NG locally (no groundstation RPi). Requires a lot of fiddling around compiling opencv w/ cuda support
#SRC = 'udpsrc port=5600 caps = "application/x-rtp, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H264, payload=(int)96" ! rtph264depay !  h264parse ! nvh264dec ! videoconvert ! appsink sync=false'

######################################################################

qOpenHD = 'gnome-terminal -e "/home/home/qopenhd25/build-QOpenHD-Desktop_Qt_5_15_2_GCC_64bit-Debug/debug/QOpenHD"'


# Command string with quotes
#command_string = 'gnome-terminal -e \'/home/home/qopenhd25/build-QOpenHD-Desktop_Qt_5_15_2_GCC_64bit-Debug/debug/QOpenHD\''
#command_string = '/home/home/qopenhd.sh transparent'

command_string = '/home/home/qopenhd25/build-QOpenHD-Desktop_Qt_5_15_2_GCC_64bit-Debug/debug/QOpenHD'

# Split and escape the command string using shlex
command_list = shlex.split(command_string)

#Set qOpenHD params in transparent mode
sed_commands = (
    "sed -i 's/^dev_force_show_full_screen=.*/dev_force_show_full_screen=true/' /home/home/.config/OpenHD/QOpenHD.conf && "
    "sed -i 's/^qopenhd_primary_video_rtp_input_port=.*/qopenhd_primary_video_rtp_input_port=5599/' /home/home/.config/OpenHD/QOpenHD.conf"
)

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
			
# Create an empty dictionary
perfs = {}

# Now you can use subprocess to run the combined sed commands
subprocess.run(sed_commands, shell=True)

lastticks=time.time()

showdebug=1
currentstep=0
procstart=time.time()

def i(str, step=0):
	global lastticks,currentstep,procstart, perfs
	suffix=""
	if showdebug==1 :
		if step==1 :
			if step in perfs and (time.time()-perfs[step].created)>1:
				print()
				for index, (key, value) in enumerate(perfs.items()):
					#print(f"Counter {index + 1}: {key}")
					print(f"{key}"+ " : " + value.name[:20].ljust(20) + " min:" + f"{value.min*1000:.1f}" 
					+ " max:" + f"{value.max*1000:.1f}".ljust(6) + " avg:" + f"{value.avg*1000:.1f}".ljust(6)) 
				perfs = {}				    

			currentstep=1			
			diff=time.time() - procstart * 1000
			suffix = f" | {(time.time() - procstart) * 1000:.1f}"
			procstart=time.time()			

		if step == 0 :
			currentstep=currentstep+1
		global lastticks
		elapsed=time.time()-lastticks
		lastticks=time.time()
		#print(f"{currentstep}"+ " : " + str + " = " + f"{elapsed*1000}") #//f"debug_step:8 : {time.time():.4f}")
		p = PerfCounter(str, round(elapsed * 1000))
		if currentstep in perfs:			
			perfs[currentstep].add(elapsed)						
		else:
			perfs[currentstep]=PerfCounter(str,elapsed)
		
		#print(f"{currentstep} : {str[:20].ljust(20)} = {elapsed * 1000:.1f}" + suffix)

def bring_to_foreground(process_id):
    try:
        subprocess.run(["wmctrl", "-ia", str(process_id)])
    except Exception as e:
        print(f"Error bringing window to foreground: {e}")

process = None
process_id = None 

# Start your process
# process = subprocess.Popen(command_list)
# process.wait() # Wait for a moment to ensure the window is created
# process_id = process.pid # Get the process ID (PID) of the last process
# bring_to_foreground(process_id) # Bring the window to the foreground

# try to keep CPU up
# try:
#     fd = os.open("/dev/cpu_dma_latency", os.O_WRONLY)
# except OSError as e:
#     print(f"Error opening /dev/cpu_dma_latency: {e}")     
# try:
#     #os.write(fd, struct.pack("I", 0))
#     print(f"SKIPPED...")     
# except OSError as e:
#     print(f"Error writing to /dev/cpu_dma_latency: {e}")    
# finally:
#     #os.close(fd) #keep file open
# 	print(f"/dev/cpu_dma_latency: Opened!")     

#set_cpu_affinity(1)


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

video = cv2.VideoCapture(SRC)

while True:	
	#grab, frame = video.read()
	i(f"Frame start",1)   #debug_step:1 : {time.time():.3f}
	while not video.grab():
		i("No frame grabbed, trying again...")

	i(f"Grabbed ")

	grab, frame = video.retrieve()

	i(f"retrieved")

	if grab is not True:
		exit() 
	res_w_orig = frame.shape[1]
	res_h_orig = frame.shape[0]
	res_w = int(res_w_orig * downSample)
	res_h = int(res_h_orig * downSample)
	top_left= [int(res_h/roiDiv),int(res_w/roiDiv)]
	bottom_right = [int(res_h - (res_h/roiDiv)),int(res_w - (res_w/roiDiv))]
	frameSize=(res_w,res_h)
	Orig = frame
	if downSample != 1:
		frame = cv2.resize(frame, frameSize) # downSample if applicable
	currFrame = frame
	currGray = cv2.cvtColor(currFrame, cv2.COLOR_BGR2GRAY)
	currGray = currGray[top_left[0]:bottom_right[0], top_left[1]:bottom_right[1]  ] #select ROI

	if prevFrame is None:
		prevOrig = frame
		prevFrame = frame
		prevGray = currGray
	
	if (grab == True) & (prevFrame is not None):
		if showrectROI == 1:
			cv2.rectangle(prevOrig,(top_left[1],top_left[0]),(bottom_right[1],bottom_right[0]),color = (211,211,211),thickness = 1)
		# Not in use, save for later
		#gfftmask = np.zeros_like(currGray)
		#gfftmask[top_left[0]:bottom_right[0], top_left[1]:bottom_right[1]] = 255
		i(f"converted to gray")
		#prevPts = cv2.goodFeaturesToTrack(prevGray,maxCorners=400,qualityLevel=0.01,minDistance=30,blockSize=3)
		prevPts = cv2.goodFeaturesToTrack(prevGray,maxCorners=400,qualityLevel=0.1,minDistance=7,blockSize=3)
		i(f"goodFeaturesToTrack")
		if prevPts is not None:
			currPts, status, err = cv2.calcOpticalFlowPyrLK(prevGray,currGray,prevPts,None,**lk_params)
			i(f"calcOpticalFlowPyrLK")
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

		fS = cv2.warpAffine(prevOrig, m, (res_w_orig,res_h_orig)) # apply magic stabilizer sauce to frame
		s = fS.shape
		T = cv2.getRotationMatrix2D((s[1]/2, s[0]/2), 0, zoomFactor)
		f_stabilized = cv2.warpAffine(fS, T, (s[1], s[0]))
		window_name=f'Stabilized:{res_w}x{res_h}'
		cv2.namedWindow(window_name,cv2.WINDOW_NORMAL)
		
		if maskFrame == 1:
			mask = np.zeros(f_stabilized.shape[:2], dtype="uint8")
			cv2.rectangle(mask, (100, 200), (1180, 620), 255, -1)
			f_stabilized = cv2.bitwise_and(f_stabilized, f_stabilized, mask=mask)
		if showFullScreen == 1:
			cv2.setWindowProperty(window_name, cv2.WND_PROP_FULLSCREEN,cv2.WINDOW_FULLSCREEN)
			if process_id != None: 
				bring_to_foreground(process_id) # Bring the window to the foreground
		
		i(f"Frame ready")
		cv2.imshow(window_name, f_stabilized)
		i(f"imshow completed")

		if showUnstabilized == 1:
			cv2.imshow("Unstabilized ROI",prevGray)

		#this will wait much more than 1 second and will cause CPU to go into idle state!!!	
		#if cv2.waitKey(delay_time) & 0xFF == ord('q'):
		if cv2.pollKey() & 0xFF == ord('q'):
			break

		i(f"Cycle completed")

		if process==None  and showFullScreen == 1:
			# Start your process
			process = subprocess.Popen(command_list)
			#process.wait() # Wait for a moment to ensure the window is created
			time.sleep(1) 
			process_id = process.pid # Get the process ID (PID) of the last process			
			bring_to_foreground(process_id) # Bring the window to the foreground
		
		prevOrig = Orig
		prevGray = currGray
		prevFrame = currFrame
		lastRigidTransform = m
		count += 1
	else:
		exit()
 
video.release()
 
cv2.destroyAllWindows()

if process is not None:
    process.terminate()
    # Wait for an additional 5 seconds for the process to respond to the terminate signal
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        # If the process is still running, forcefully kill it
        process.kill()
else:
    print("No qOpenHD to close!")

