#include <gst/gst.h>
#include <gst/video/videooverlay.h>
#include <gtk/gtk.h>
#include <gdk/gdkx.h>  // Ensure this header is included for X11

//works !!!
//gcc
//gcc -o play_video play_video.c `pkg-config --cflags --libs gstreamer-1.0 gstreamer-video-1.0 gtk+-3.0 gdk-x11-3.0`

static gboolean bus_call(GstBus *bus, GstMessage *msg, gpointer data) {
    GMainLoop *loop = (GMainLoop *)data;

    switch (GST_MESSAGE_TYPE(msg)) {
        case GST_MESSAGE_EOS:
            g_print("End of stream\n");
            g_main_loop_quit(loop);
            break;
        case GST_MESSAGE_ERROR: {
            gchar *debug;
            GError *error;

            gst_message_parse_error(msg, &error, &debug);
            g_free(debug);

            g_printerr("Error: %s\n", error->message);
            g_error_free(error);

            g_main_loop_quit(loop);
            break;
        }
        default:
            break;
    }

    return TRUE;
}

static void realize_cb(GtkWidget *widget, gpointer data) {
    GstElement *video_sink = GST_ELEMENT(data);
    GdkWindow *window = gtk_widget_get_window(widget);
    guintptr window_handle;

#if defined(GDK_WINDOWING_X11)
    window_handle = GDK_WINDOW_XID(window);
#elif defined(GDK_WINDOWING_WIN32)
    window_handle = GDK_WINDOW_HWND(window);
#elif defined(GDK_WINDOWING_QUARTZ)
    window_handle = gdk_quartz_window_get_nsview(window);
#endif

    gst_video_overlay_set_window_handle(GST_VIDEO_OVERLAY(video_sink), window_handle);
}

int main(int argc, char *argv[]) {
    GMainLoop *loop;
    GstElement *pipeline, *video_sink;
    GstBus *bus;
    GtkWidget *window;
    GtkWidget *video_area;

    gtk_init(&argc, &argv);
    gst_init(&argc, &argv);

    loop = g_main_loop_new(NULL, FALSE);

    window = gtk_window_new(GTK_WINDOW_TOPLEVEL);
    gtk_window_set_default_size(GTK_WINDOW(window), 800, 600);
    gtk_window_set_title(GTK_WINDOW(window), "GStreamer Fullscreen");
    gtk_window_set_decorated(GTK_WINDOW(window), FALSE);
    gtk_window_fullscreen(GTK_WINDOW(window));

    video_area = gtk_drawing_area_new();
    gtk_container_add(GTK_CONTAINER(window), video_area);

    //const gchar *pipeline_str = "udpsrc port=5600 buffer-size=65536 caps=\"application/x-rtp, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H265\" ! rtpjitterbuffer ! rtph265depay ! avdec_h265 ! videoconvert ! xvimagesink name=video_sink sync=false";
    //                  'udpsrc port=5600 buffer-size=65536 caps="application/x-rtp, payload=97, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H265" ! rtpjitterbuffer latency=100 mode=0 max-misorder-time=200 max-dropout-time=100 max-rtcp-rtp-time-diff=100 ! rtph265depay ! queue max-size-buffers=1 ! vaapih265dec ! videoconvert ! appsink sync=false '
    const gchar *pipeline_str = "udpsrc port=5600 buffer-size=65536 caps=\"application/x-rtp, media=(string)video, clock-rate=(int)90000, encoding-name=(string)H265\" ! rtpjitterbuffer ! rtph265depay ! vaapih265dec ! videoconvert ! xvimagesink name=video_sink sync=false";

    pipeline = gst_parse_launch(pipeline_str, NULL);

    if (!pipeline) {
        g_printerr("Parse error\n");
        return -1;
    }

    // Get the video sink element from the pipeline
    video_sink = gst_bin_get_by_name(GST_BIN(pipeline), "video_sink");
    if (!video_sink) {
        g_printerr("Video sink not found\n");
        return -1;
    }

    g_signal_connect(window, "realize", G_CALLBACK(realize_cb), video_sink);
    g_signal_connect(window, "destroy", G_CALLBACK(gtk_main_quit), NULL);

    gtk_widget_show_all(window);

    bus = gst_pipeline_get_bus(GST_PIPELINE(pipeline));
    gst_bus_add_watch(bus, bus_call, loop);
    gst_object_unref(bus);

    gst_element_set_state(pipeline, GST_STATE_PLAYING);

    gtk_main();

    gst_element_set_state(pipeline, GST_STATE_NULL);
    gst_object_unref(GST_OBJECT(pipeline));
    g_main_loop_unref(loop);

    return 0;
}
