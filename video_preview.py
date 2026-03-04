#!/usr/bin/env python3
import gi
import subprocess
import threading
gi.require_version('Gtk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, GdkPixbuf, GLib

class VideoPreviewDialog(Gtk.Dialog):
    def __init__(self, parent, video_path):
        super().__init__(title="Schnittbereich wählen", transient_for=parent, modal=True)
        self.set_default_size(800, 550)
        self.add_button("_Abbrechen", Gtk.ResponseType.CANCEL)
        self.add_button("_Übernehmen", Gtk.ResponseType.OK)

        self.video_path = video_path
        self.duration = self.get_duration()
        self.start_time = 0.0
        self.end_time = self.duration
        self.is_updating = False

        vbox = self.get_content_area()
        vbox.set_spacing(10)
        vbox.set_property("margin", 12)

        # Bildanzeige
        self.image = Gtk.Image()
        vbox.pack_start(self.image, True, True, 0)

        # Zeit-Label
        self.time_label = Gtk.Label()
        self.time_label.set_markup("<b>Position: 00:00:00</b>")
        vbox.pack_start(self.time_label, False, False, 0)

        # Slider
        self.adj = Gtk.Adjustment(0, 0, self.duration, 0.1, 1.0, 0)
        self.slider = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.adj)
        self.slider.set_draw_value(False)
        self.slider.connect("value-changed", self.on_slider_moved)
        vbox.pack_start(self.slider, False, False, 0)

        # Buttons für In/Out
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        vbox.pack_start(hbox, False, False, 5)

        self.btn_in = Gtk.Button(label="Start hier (In)")
        self.btn_in.connect("clicked", self.set_in_point)
        hbox.pack_start(self.btn_in, True, True, 0)

        self.btn_out = Gtk.Button(label="Ende hier (Out)")
        self.btn_out.connect("clicked", self.set_out_point)
        hbox.pack_start(self.btn_out, True, True, 0)

        self.status_label = Gtk.Label(label=f"Bereich: 00:00:00 bis {self.format_time(self.duration)}")
        vbox.pack_start(self.status_label, False, False, 0)

        self.show_all()
        self.update_preview(0)

    def get_duration(self):
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", self.video_path]
        return float(subprocess.check_output(cmd).decode().strip())

    def format_time(self, seconds):
        h = int(seconds // 3600); m = int((seconds % 3600) // 60); s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:05.2f}"

    def on_slider_moved(self, widget):
        val = widget.get_value()
        self.time_label.set_markup(f"<b>Position: {self.format_time(val)}</b>")
        if not self.is_updating:
            threading.Thread(target=self.update_preview, args=(val,), daemon=True).start()

    def update_preview(self, seconds):
        self.is_updating = True
        # MJPEG via Pipe ist perfekt, da keine Schreibrechte benötigt werden!
        cmd = ["ffmpeg", "-ss", str(seconds), "-i", self.video_path, "-frames:v", "1", "-s", "640x360", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            output, _ = proc.communicate()
            if output:
                loader = GdkPixbuf.PixbufLoader.new_with_type("jpeg")
                loader.write(output)
                loader.close()
                pix = loader.get_pixbuf()
                # UI-Update muss immer im Main-Thread erfolgen
                GLib.idle_add(self.image.set_from_pixbuf, pix)
        except Exception as e:
            print(f"Preview Error: {e}")
        finally:
            self.is_updating = False

    def set_in_point(self, btn):
        self.start_time = self.slider.get_value()
        self.update_status()

    def set_out_point(self, btn):
        self.end_time = self.slider.get_value()
        self.update_status()

    def update_status(self):
        self.status_label.set_text(f"Bereich: {self.format_time(self.start_time)} bis {self.format_time(self.end_time)}")

    def get_range(self):
        return self.start_time, self.end_time
