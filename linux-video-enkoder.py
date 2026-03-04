#!/usr/bin/env python3
# =======================================================================
# Titel:  Linux-Video-Enkoder (GTK3 Port)
# Version: 1.0.5 (Layout-Optimierung & Visueller Schnitt)
# Autor: Nightworker / Adaptive UI: Gemini
# =======================================================================
import sys
import os
# Verhindert die Erstellung von __pycache__ Ordnern komplett
sys.dont_write_bytecode = True
import shutil
import subprocess
import threading
import re
from pathlib import Path
from datetime import datetime

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib, Pango

# --- Import der Vorschau ---
try:
    from video_preview import VideoPreviewDialog
except ImportError:
    VideoPreviewDialog = None

# -------------------- Hilfsfunktionen --------------------
def which_bin(name):
    return shutil.which(name) is not None

def detect_gpu_short():
    try:
        out = subprocess.getoutput(r"lspci | grep -i 'vga\|3d' || true")
    except Exception:
        return "CPU"
    s = out.lower()
    if "nvidia" in s: return "NVIDIA"
    if "amd" in s or "ati" in s: return "AMD"
    if "intel" in s: return "INTEL"
    return "CPU"

def probe_duration_seconds(path: Path):
    if not which_bin("ffprobe"): return None
    try:
        out = subprocess.check_output([
            "ffprobe","-v","error","-show_entries","format=duration",
            "-of","default=noprint_wrappers=1:nokey=1", str(path)
        ], stderr=subprocess.DEVNULL).decode().strip()
        return float(out) if out else None
    except Exception: return None

def calculate_bitrate_for_target_size(filepath, target_size_mb, audio_bitrate_kbps=192):
    dur = probe_duration_seconds(Path(filepath))
    if not dur or dur <= 0: return None
    total_kbps = (target_size_mb * 8192) / dur
    video_kbps = max(total_kbps - audio_bitrate_kbps, 300)
    return int(video_kbps)

def make_unique_path(path: Path) -> Path:
    if not path.exists(): return path
    parent, stem, suffix = path.parent, path.stem, path.suffix
    new_stem = f"{stem}_converted"
    candidate = parent / f"{new_stem}{suffix}"
    if not candidate.exists(): return candidate
    i = 1
    while True:
        candidate = parent / f"{new_stem}({i}){suffix}"
        if not candidate.exists(): return candidate
        i += 1

time_re = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")

# -------------------- Hauptklasse --------------------

class VideoConverterWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Linux-Video-Enkoder")
        self.set_default_size(1050, 750)
        self.selected_files = []
        self.current_proc = None
        self.stop_event = threading.Event()

        main_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        main_hbox.set_margin_start(12)
        main_hbox.set_margin_end(12)
        main_hbox.set_margin_top(12)
        main_hbox.set_margin_bottom(12)
        self.add(main_hbox)

        # --- Linke Seite ---
        left_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        main_hbox.pack_start(left_vbox, False, False, 0)

        left_vbox.pack_start(Gtk.Label(label="Erkannte Grafikkarte:", xalign=0), False, False, 0)
        self.gpu_entry = Gtk.Entry(editable=False, text=detect_gpu_short())
        left_vbox.pack_start(self.gpu_entry, False, False, 0)

        left_vbox.pack_start(Gtk.Label(label="GPU / CPU Auswahl:", xalign=0), False, False, 0)
        self.gpu_combo = Gtk.ComboBoxText()
        for opt in ["Automatisch (empfohlen)", "NVIDIA", "AMD", "Intel", "CPU"]:
            self.gpu_combo.append_text(opt)
        self.gpu_combo.set_active(0)
        left_vbox.pack_start(self.gpu_combo, False, False, 0)

        self.btn_files = Gtk.Button(label="Dateien auswählen")
        self.btn_files.connect("clicked", self.on_select_files)
        left_vbox.pack_start(self.btn_files, False, False, 0)

        self.btn_remove = Gtk.Button(label="Ausgewählte entfernen")
        self.btn_remove.connect("clicked", self.on_remove_selected)
        left_vbox.pack_start(self.btn_remove, False, False, 0)

        self.btn_target = Gtk.Button(label="Zielverzeichnis wählen")
        self.btn_target.connect("clicked", self.on_browse_target)
        left_vbox.pack_start(self.btn_target, False, False, 0)

        left_vbox.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 5)

        # --- Schnittbereich Sektion (Wunschplatzierung) ---
        self.btn_preview = Gtk.Button(label="Schnittbereich festlegen (Vorschau)")
        self.btn_preview.connect("clicked", self.on_open_preview)
        left_vbox.pack_start(self.btn_preview, False, False, 0)

        grid_time = Gtk.Grid(column_spacing=10, row_spacing=5)
        left_vbox.pack_start(grid_time, False, False, 0)
        grid_time.attach(Gtk.Label(label="Startzeit:", xalign=0), 0, 0, 1, 1)
        self.start_entry = Gtk.Entry(text="00:00:00")
        grid_time.attach(self.start_entry, 1, 0, 1, 1)
        grid_time.attach(Gtk.Label(label="Dauer (sek):", xalign=0), 0, 1, 1, 1)
        self.duration_limit_entry = Gtk.Entry(text="0")
        grid_time.attach(self.duration_limit_entry, 1, 1, 1, 1)

        left_vbox.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 5)

        # --- Video Parameter Grid ---
        grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        left_vbox.pack_start(grid, False, False, 0)

        grid.attach(Gtk.Label(label="Upscaling:", xalign=0), 0, 0, 1, 1)
        self.upscale_combo = Gtk.ComboBoxText()
        for s in ["Original","720p (1280x720)","1080p (1920x1080)","1440p (2560x1440)","2160p (3840x2160)"]:
            self.upscale_combo.append_text(s)
        self.upscale_combo.set_active(0)
        grid.attach(self.upscale_combo, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="Audioformat:", xalign=0), 0, 1, 1, 1)
        self.audio_combo = Gtk.ComboBoxText()
        for s in ["AAC","PCM","FLAC (mkv)"]: self.audio_combo.append_text(s)
        self.audio_combo.set_active(0)
        grid.attach(self.audio_combo, 1, 1, 1, 1)

        grid.attach(Gtk.Label(label="Videoformat:", xalign=0), 0, 2, 1, 1)
        self.video_combo = Gtk.ComboBoxText()
        for s in ["H.264","H.265","AV1","Nur Audio ändern"]: self.video_combo.append_text(s)
        self.video_combo.set_active(0)
        grid.attach(self.video_combo, 1, 2, 1, 1)

        grid.attach(Gtk.Label(label="Qualität Modus:", xalign=0), 0, 3, 1, 1)
        self.quality_combo = Gtk.ComboBoxText()
        for s in ["CQ (Qualitätsbasiert)","Bitrate (kbit/s)","Zieldateigröße (MB)"]:
            self.quality_combo.append_text(s)
        self.quality_combo.set_active(0)
        self.quality_combo.connect("changed", self.on_quality_mode_changed)
        grid.attach(self.quality_combo, 1, 3, 1, 1)

        self.quality_label = Gtk.Label(label="CRF Wert (0-51):", xalign=0)
        grid.attach(self.quality_label, 0, 4, 1, 1)
        self.quality_entry = Gtk.Entry(text="23")
        grid.attach(self.quality_entry, 1, 4, 1, 1)

        grid.attach(Gtk.Label(label="Analyse-Stufe (Preset):", xalign=0), 0, 5, 1, 1)
        self.preset_combo = Gtk.ComboBoxText()
        presets = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]
        for p in presets:
            self.preset_combo.append_text(p)
        self.preset_combo.set_active(5)
        grid.attach(self.preset_combo, 1, 5, 1, 1)

        left_vbox.pack_start(Gtk.Label(label="Zielordner (leer -> auto):", xalign=0), False, False, 0)
        self.target_entry = Gtk.Entry()
        left_vbox.pack_start(self.target_entry, False, False, 0)

        self.save_in_source_chk = Gtk.CheckButton(label="Im Quellverzeichnis speichern")
        left_vbox.pack_start(self.save_in_source_chk, False, False, 0)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        left_vbox.pack_end(btn_box, False, False, 0)
        self.start_btn = Gtk.Button(label="Konvertierung starten")
        self.start_btn.get_style_context().add_class("suggested-action")
        self.start_btn.connect("clicked", self.start_conversion)

        self.cancel_btn = Gtk.Button(label="Abbrechen")
        self.cancel_btn.set_sensitive(False)
        self.cancel_btn.connect("clicked", self.cancel_conversion)

        btn_box.pack_start(self.cancel_btn, True, True, 0)
        btn_box.pack_start(self.start_btn, True, True, 0)

        # --- Rechte Seite ---
        right_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        main_hbox.pack_start(right_vbox, True, True, 0)

        self.liststore = Gtk.ListStore(str)
        self.treeview = Gtk.TreeView(model=self.liststore)
        renderer = Gtk.CellRendererText()
        column = Gtk.TreeViewColumn("Ausgewählte Dateien", renderer, text=0)
        self.treeview.append_column(column)
        self.treeview.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)

        scroll_tree = Gtk.ScrolledWindow()
        scroll_tree.set_min_content_height(150)
        scroll_tree.add(self.treeview)
        right_vbox.pack_start(scroll_tree, True, True, 0)

        self.treeview.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.treeview.drag_dest_add_uri_targets()
        self.treeview.connect("drag-data-received", self.on_drag_data_received)

        self.file_label = Gtk.Label(label="Aktueller Dateifortschritt:", xalign=0)
        right_vbox.pack_start(self.file_label, False, False, 0)
        self.file_progress = Gtk.ProgressBar()
        right_vbox.pack_start(self.file_progress, False, False, 0)

        self.total_label = Gtk.Label(label="Gesamtfortschritt:", xalign=0)
        right_vbox.pack_start(self.total_label, False, False, 0)
        self.total_progress = Gtk.ProgressBar()
        right_vbox.pack_start(self.total_progress, False, False, 0)

        self.log_view = Gtk.TextView(editable=False, cursor_visible=False)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD)
        scroll_log = Gtk.ScrolledWindow()
        scroll_log.set_min_content_height(200)
        scroll_log.add(self.log_view)
        right_vbox.pack_start(scroll_log, True, True, 0)

        self.show_all()

    # -------------------- UI Handlers --------------------
    def on_open_preview(self, btn):
        selection = self.treeview.get_selection()
        model, paths = selection.get_selected_rows()
        if not paths:
            if self.selected_files:
                video_path = self.selected_files[0]
            else:
                return
        else:
            idx = paths[0].get_indices()[0]
            video_path = self.selected_files[idx]

        if VideoPreviewDialog:
            dialog = VideoPreviewDialog(self, video_path)
            if dialog.run() == Gtk.ResponseType.OK:
                start, end = dialog.get_range()
                h = int(start // 3600); m = int((start % 3600) // 60); s = start % 60
                self.start_entry.set_text(f"{h:02d}:{m:02d}:{s:05.2f}")
                self.duration_limit_entry.set_text(f"{end - start:.2f}")
            dialog.destroy()

    def append_log(self, text):
        GLib.idle_add(self._safe_append_log, text)

    def _safe_append_log(self, text):
        buf = self.log_view.get_buffer()
        buf.insert(buf.get_end_iter(), text)
        mark = buf.create_mark(None, buf.get_end_iter(), False)
        self.log_view.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)

    def on_quality_mode_changed(self, combo):
        mode = combo.get_active_text()
        if "CQ" in mode:
            self.quality_label.set_text("CRF Wert (0-51):")
            self.quality_entry.set_text("23")
        elif "Bitrate" in mode:
            self.quality_label.set_text("Bitrate (kbit/s):")
            self.quality_entry.set_text("5000")
        elif "Zieldateigröße" in mode:
            self.quality_label.set_text("Zieldateigröße (MB):")
            self.quality_entry.set_text("700")

    def on_select_files(self, btn):
        dialog = Gtk.FileChooserDialog(title="Wähle Videodateien", parent=self, action=Gtk.FileChooserAction.OPEN,
                                     buttons=(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OPEN, Gtk.ResponseType.OK))
        dialog.set_select_multiple(True)
        if dialog.run() == Gtk.ResponseType.OK:
            for f in dialog.get_filenames():
                if f not in self.selected_files:
                    self.selected_files.append(f)
                    self.liststore.append([Path(f).name])
        dialog.destroy()

    def on_remove_selected(self, btn):
        selection = self.treeview.get_selection()
        model, paths = selection.get_selected_rows()
        for path in reversed(paths):
            idx = path.get_indices()[0]
            del self.selected_files[idx]
            self.liststore.remove(model.get_iter(path))

    def on_browse_target(self, btn):
        dialog = Gtk.FileChooserDialog(title="Zielverzeichnis", parent=self, action=Gtk.FileChooserAction.SELECT_FOLDER,
                                     buttons=(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OPEN, Gtk.ResponseType.OK))
        if dialog.run() == Gtk.ResponseType.OK:
            self.target_entry.set_text(dialog.get_filename())
        dialog.destroy()

    def on_drag_data_received(self, widget, context, x, y, selection, info, time):
        uris = selection.get_uris()
        for uri in uris:
            path = GLib.filename_from_uri(uri)[0]
            if Path(path).exists():
                if path not in self.selected_files:
                    self.selected_files.append(path)
                    self.liststore.append([Path(path).name])
        context.finish(True, False, time)

    # -------------------- Logik (BUILD ARGS) --------------------
    def build_ffmpeg_args(self, infile, outfile):
        sel_gpu_mode = self.gpu_combo.get_active_text()
        gpu = detect_gpu_short().upper() if "Auto" in sel_gpu_mode else sel_gpu_mode.upper()

        vchoice = self.video_combo.get_active_text()
        achoice = self.audio_combo.get_active_text()
        qmode = self.quality_combo.get_active_text()
        qval = self.quality_entry.get_text().strip()
        upscale = self.upscale_combo.get_active_text()
        selected_preset = self.preset_combo.get_active_text()

        # Zeit-Parameter
        start_t = self.start_entry.get_text().strip()
        dur_t = self.duration_limit_entry.get_text().strip()

        # Encoder Mapping
        if gpu == "NVIDIA": h264, h265, av1, hw = "h264_nvenc", "hevc_nvenc", "av1_nvenc", []
        elif gpu == "AMD": h264, h265, av1, hw = "h264_amf", "hevc_amf", "av1_amf", ["-hwaccel","vaapi"]
        elif gpu == "INTEL": h264, h265, av1, hw = "h264_vaapi", "hevc_vaapi", "av1_vaapi", ["-hwaccel","vaapi"]
        else: h264, h265, av1, hw = "libx264", "libx265", "libaom-av1", []

        args = hw.copy()

        # -ss vor -i für schnelles Seeking
        if start_t != "00:00:00":
            args += ["-ss", start_t]

        args += ["-i", infile]

        if dur_t != "0" and dur_t != "":
            args += ["-t", dur_t]

        if vchoice == "Nur Audio ändern":
            args += ["-c:v", "copy"]
        else:
            codec = h264 if "H.264" in vchoice else (h265 if "H.265" in vchoice else av1)
            is_hw = any(x in codec for x in ["nvenc", "amf", "vaapi"])

            current_preset = selected_preset
            if "nvenc" in codec:
                mapping = {"ultrafast":"p1", "superfast":"p2", "veryfast":"p3", "faster":"p4",
                           "fast":"p5", "medium":"p6", "slow":"p7", "slower":"p7", "veryslow":"p7"}
                current_preset = mapping.get(selected_preset, "p4")

            if "CQ" in qmode:
                qn = int(qval) if qval.isdigit() else 23
                if is_hw:
                    args += ["-c:v", codec, "-rc", "vbr", "-cq", str(qn), "-preset", current_preset]
                else:
                    args += ["-c:v", codec, "-crf", str(qn), "-preset", current_preset]
            elif "Bitrate" in qmode:
                kb = qval if qval.isdigit() else "5000"
                args += ["-c:v", codec, "-b:v", f"{kb}k", "-preset", current_preset]
            else:
                mb = float(qval) if qval.replace('.','').isdigit() else 700.0
                vkbps = calculate_bitrate_for_target_size(infile, mb) or 5000
                args += ["-c:v", codec, "-b:v", f"{vkbps}k", "-preset", current_preset]

        # Audio
        if achoice == "AAC": args += ["-c:a", "aac", "-b:a", "192k"]
        elif achoice == "PCM": args += ["-c:a", "pcm_s16le"]
        elif achoice == "FLAC (mkv)": args += ["-c:a", "flac"]
        else: args += ["-c:a", "copy"]

        # Scaling
        res_map = {"720p": "1280:720", "1080p": "1920:1080", "1440p": "2560:1440", "2160p": "3840:2160"}
        for k, v in res_map.items():
            if k in upscale:
                args += ["-vf", f"scale={v}:flags=lanczos"]
                break
        return args

    # -------------------- Threads --------------------
    def start_conversion(self, btn):
        if not self.selected_files: return
        self.start_btn.set_sensitive(False)
        self.cancel_btn.set_sensitive(True)
        self.stop_event.clear()
        threading.Thread(target=self.run_conversion, daemon=True).start()

    def cancel_conversion(self, btn):
        self.stop_event.set()
        if self.current_proc: self.current_proc.terminate()
        self.append_log("\nAbbruch angefordert...\n")

    def run_conversion(self):
        total = len(self.selected_files)
        target_dir_str = self.target_entry.get_text().strip()

        for idx, infile in enumerate(list(self.selected_files), 1):
            if self.stop_event.is_set(): break
            in_p = Path(infile)
            ext = ".mkv" if self.audio_combo.get_active_text() == "FLAC (mkv)" else ".mp4"

            if self.save_in_source_chk.get_active():
                out_dir = in_p.parent
            elif target_dir_str:
                out_dir = Path(target_dir_str)
                out_dir.mkdir(parents=True, exist_ok=True)
            else:
                out_dir = in_p.parent / f"converted_{datetime.now().strftime('%Y-%m-%d')}"
                out_dir.mkdir(parents=True, exist_ok=True)

            out_p = make_unique_path(out_dir / (in_p.stem + ext))

            # Berechne Dauer für Fortschritt
            dur_limit = float(self.duration_limit_entry.get_text() or 0)
            total_duration = dur_limit if dur_limit > 0 else (probe_duration_seconds(in_p) or 1.0)

            args = self.build_ffmpeg_args(str(in_p), str(out_p)) + ["-y", str(out_p)]

            self.append_log(f"\n--- Starte Datei {idx}/{total}: {in_p.name} ---\n")
            self.append_log(f"Kommando: ffmpeg {' '.join(args[:15])}...\n")

            try:
                self.current_proc = subprocess.Popen(
                    ["ffmpeg"] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    universal_newlines=True, bufsize=1
                )
                for line in self.current_proc.stdout:
                    self.append_log(line)
                    m = time_re.search(line)
                    if m:
                        hh, mm, ss = int(m.group(1)), int(m.group(2)), float(m.group(3))
                        secs = hh*3600 + mm*60 + ss
                        pct = min(1.0, secs / total_duration)
                        GLib.idle_add(self.file_progress.set_fraction, pct)
                        GLib.idle_add(self.total_progress.set_fraction, ((idx-1) + pct) / total)
                self.current_proc.wait()
            except Exception as e:
                self.append_log(f"Fehler: {e}\n")

        self.append_log("\n--- Alle Aufgaben abgeschlossen. ---\n")
        GLib.idle_add(self.start_btn.set_sensitive, True)
        GLib.idle_add(self.cancel_btn.set_sensitive, False)

if __name__ == "__main__":
    win = VideoConverterWindow()
    win.connect("destroy", Gtk.main_quit)
    Gtk.main()
