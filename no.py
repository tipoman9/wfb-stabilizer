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

while False:
    frame = video_stream.read()
    cv2.imshow('Frame', frame)
    if cv2.pollKey() == ord('q'):
        break

#display every even frame

# Open the video capture
video = cv2.VideoCapture(SRC)

# Check if video opened successfully
if not video.isOpened():
    print("Error opening video stream or file")
    exit(1)

frame_index = 0  # Initialize frame index

while True:
    ret, frame = video.read()  # Read a frame
    if not ret:
        break  # Break the loop if there are no more frames

    # Process only even frames
    if frame_index % 2 == 0:
        cv2.imshow('Even Frame', frame)  # Display the frame
        if cv2.waitKey(1) & 0xFF == ord('q'):  # Wait for 'q' key to exit
            break

    frame_index += 1  # Increment frame index

# When everything done, release the video capture object
video.release()
cv2.destroyAllWindows()

video_stream.stop()
cv2.destroyAllWindows()
 
	 

print("End.")

