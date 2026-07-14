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

        self.set_resizable(True)

        # Buttons hinzufügen
        self.add_button("_Abbrechen", Gtk.ResponseType.CANCEL)
        self.add_button("_Übernehmen", Gtk.ResponseType.OK)

        self.video_path = video_path
        self.duration = self.get_duration()
        self.start_time = 0.0
        self.end_time = self.duration
        self.is_updating = False

        # 1. Das exakte Seitenverhältnis (Aspect Ratio) des Videos ermitteln
        self.video_aspect_ratio = self.get_video_aspect_ratio()

        # Wir steuern die Qualität über die Ziel-Höhe.
        self.current_target_height = 720

        vbox = self.get_content_area()
        vbox.set_spacing(10)
        vbox.set_property("margin", 12)

        # Auswahl-Leiste
        button_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=15)
        vbox.pack_start(button_hbox, False, False, 5)
        button_hbox.add(Gtk.Label(label="Vorschauqualität (Höhe):"))

        # Die Radio-Buttons schalten die Zielhöhe um
        self.radio_small = Gtk.RadioButton.new_with_label(None, "Klein (360p)")
        self.radio_small.connect("toggled", self.on_res_toggled, 360)
        button_hbox.pack_start(self.radio_small, False, False, 0)

        self.radio_med = Gtk.RadioButton.new_with_label_from_widget(self.radio_small, "Mittel (720p)")
        self.radio_med.set_active(True)
        self.radio_med.connect("toggled", self.on_res_toggled, 720)
        button_hbox.pack_start(self.radio_med, False, False, 0)

        self.radio_large = Gtk.RadioButton.new_with_label_from_widget(self.radio_small, "Groß (1080p)")
        self.radio_large.connect("toggled", self.on_res_toggled, 1080)
        button_hbox.pack_start(self.radio_large, False, False, 0)

        # ScrolledWindow für das Bild
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        # 2. Fenstergröße dynamisch basierend auf dem Seitenverhältnis berechnen
        self.update_window_dimensions()

        self.image = Gtk.Image()
        self.scroll.add(self.image)
        vbox.pack_start(self.scroll, True, True, 0)

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

        # Buttons In/Out
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
        self.trigger_preview_update()

    def get_video_aspect_ratio(self):
        """Ermittelt die echten Pixel-Dimensionen und errechnet das Seitenverhältnis (W/H)."""
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0:s=x",
            self.video_path
        ]
        try:
            output = subprocess.check_output(cmd).decode().strip()
            dimensions = output.split('\n')[0]
            width, height = map(int, dimensions.split('x'))
            return width / height
        except Exception as e:
            print(f"Fehler bei der Seitenverhältnis-Ermittlung: {e}")
            return 16.0 / 9.0  # Fallback auf Standard-Querformat

    def update_window_dimensions(self):
        """Berechnet die optimalen Maße für das Widget und weist das GTK-Fenster an, sich anzupassen."""
        # Gewünschte Breite basierend auf Seitenverhältnis berechnen
        calculated_width = int(self.current_target_height * self.video_aspect_ratio)
        calculated_height = self.current_target_height

        # Begrenzung für sehr große Höhen auf Desktop-Monitoren (z.B. bei 1080p Hochkant)
        # Wenn die Höhe 800px überschreitet, skalieren wir das Widget auf maximal 800px Höhe runter,
        # behalten aber die korrekte Breite bei.
        max_widget_height = 800
        if calculated_height > max_widget_height:
            scale_factor = max_widget_height / calculated_height
            widget_width = int(calculated_width * scale_factor)
            widget_height = max_widget_height
        else:
            widget_width = calculated_width
            widget_height = calculated_height

        # Setze die Mindestgröße für den Bild-Container
        self.scroll.set_size_request(widget_width, widget_height)

    def on_res_toggled(self, button, target_height):
        if button.get_active():
            self.current_target_height = target_height
            self.update_window_dimensions()

            # Das Fenster zwingen, das Layout sofort neu zu berechnen und sich zusammenzuziehen/zu strecken
            self.show_all()
            self.resize(1, 1)

            self.trigger_preview_update()

    def trigger_preview_update(self):
        val = self.slider.get_value()
        if not self.is_updating:
            threading.Thread(target=self.update_preview, args=(val,), daemon=True).start()

    def get_duration(self):
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", self.video_path]
        return float(subprocess.check_output(cmd).decode().strip())

    def format_time(self, seconds):
        h = int(seconds // 3600); m = int((seconds % 3600) // 60); s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:05.2f}"

    def on_slider_moved(self, widget):
        val = widget.get_value()
        self.time_label.set_markup(f"<b>Position: {self.format_time(val)}</b>")
        self.trigger_preview_update()

    def update_preview(self, seconds):
        self.is_updating = True

        # FFmpeg skaliert das Bild auf die Zielhöhe, Breite wird automatisch angepasst
        video_filter = f"scale=-1:{self.current_target_height}"

        cmd = [
            "ffmpeg", "-ss", str(seconds), "-i", self.video_path, "-frames:v", "1",
            "-vf", video_filter, "-f", "image2pipe", "-vcodec", "mjpeg", "-"
        ]

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            output, _ = proc.communicate()
            if output:
                loader = GdkPixbuf.PixbufLoader.new_with_type("jpeg")
                loader.write(output)
                loader.close()
                pix = loader.get_pixbuf()

                def set_image(p):
                    self.image.set_from_pixbuf(p)
                    return False

                GLib.idle_add(set_image, pix)
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
