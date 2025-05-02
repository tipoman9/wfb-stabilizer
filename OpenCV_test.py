import cv2
import threading
import time

# Global shared frame and lock
shared_frame = None
frame_lock = threading.Lock()
AbortNow = False
window_name = "Low-Latency Stream"
showFullScreen = 1
frames_ttl = 0

# === Display Thread ===
def display_frames():
    global shared_frame, frame_lock, AbortNow, frames_ttl

    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

    while not AbortNow:
        frame = None
        with frame_lock:
            if shared_frame is not None:
                frame = shared_frame.copy()

        if frame is not None:
            frames_ttl += 1
            if showFullScreen == 1 and frames_ttl % 16 == 0:
                cv2.setWindowProperty(window_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
            cv2.imshow(window_name, frame)

        if cv2.pollKey() & 0xFF == ord('q'):
            AbortNow = True
            break

        cv2.waitKey(1)

    cv2.destroyAllWindows()

# === Main Capture Thread ===
def main():
    global shared_frame, frame_lock, AbortNow

    # Example GStreamer pipeline — replace with your actual one
    SRC = (
        'udpsrc port=5600 caps="application/x-rtp, payload=97, media=(string)video, '
        'clock-rate=(int)90000, encoding-name=(string)H265" ! '
        'rtpjitterbuffer latency=50 mode=0 ! '
        'rtph265depay ! queue ! vaapih265dec ! videoconvert ! '
        'appsink sync=false drop=true max-buffers=1'
    )

    cap = cv2.VideoCapture(SRC, cv2.CAP_GSTREAMER)
    if not cap.isOpened():
        print("❌ Cannot open video stream.")
        return

    display_thread = threading.Thread(target=display_frames, daemon=True)
    display_thread.start()

    # while not AbortNow:
    #     ret, frame = cap.read()
    #     if not ret:
    #         continue

    #     # Optional processing
    #     f_stabilized = frame  # Replace this with actual processing if needed

    #     # Update shared frame with lock
    #     with frame_lock:
    #         shared_frame = f_stabilized
    while not AbortNow:
        # Grab the next frame
        if not cap.grab():
            continue

        # Retrieve (decode) the grabbed frame
        ret, frame = cap.retrieve()
        if not ret or frame is None:
            continue

        # Optional: process frame
        f_stabilized = frame  # Replace with your actual processing if needed

        # Thread-safe shared frame update
        with frame_lock:
            shared_frame = f_stabilized

    cap.release()
    print("Stream stopped.")

if __name__ == "__main__":
    main()
