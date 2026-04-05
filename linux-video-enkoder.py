#!/usr/bin/env python3
# =======================================================================
# Titel:    Linux-Video-Enkoder (GTK3 Port)
# Version:  1.1.1 (Full Reset Feature)
# Autor:    Nightworker / Adaptive UI: Gemini
# =======================================================================
import sys
import os
sys.dont_write_bytecode = True
import shutil
import subprocess
import threading
import re
import urllib.parse
from pathlib import Path

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib

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
_encoder_cache = {}

def is_encoder_available(encoder: str) -> bool:
    if encoder in _encoder_cache: return _encoder_cache[encoder]
    try:
        cmd = ["ffmpeg", "-hide_banner", "-encoders"]
        out = subprocess.check_output(cmd).decode()
        res = encoder in out
        _encoder_cache[encoder] = res
        return res
    except: return False

_HW_ENCODER_PRIORITY = {
    "H.264": {"NVIDIA": ["h264_nvenc"], "AMD": ["h264_vaapi"], "INTEL": ["h264_vaapi"], "CPU": ["libx264"]},
    "H.265": {"NVIDIA": ["hevc_nvenc"], "AMD": ["hevc_vaapi"], "INTEL": ["hevc_vaapi"], "CPU": ["libx265"]},
    "AV1":   {"NVIDIA": ["av1_nvenc"], "AMD": ["av1_vaapi"], "INTEL": ["av1_vaapi"], "CPU": ["libsvtav1"]},
}

def _select_hw_encoder(fmt, gpu):
    candidates = _HW_ENCODER_PRIORITY.get(fmt, {}).get(gpu, [])
    for enc in candidates:
        if is_encoder_available(enc): return enc
    return {"H.264":"libx264", "H.265":"libx265", "AV1":"libsvtav1"}.get(fmt, "libx264")

def _codec_quality_args(codec, qmode, qval, preset, infile):
    args = ["-c:v", codec]
    p_map = {"ultrafast":"p1","superfast":"p2","veryfast":"p3","faster":"p4","fast":"p5","medium":"p6","slow":"p7"}
    p = p_map.get(preset, "p4") if "nvenc" in codec else preset

    if "CQ" in qmode:
        qn = qval if qval.isdigit() else "23"
        if "nvenc" in codec: args += ["-rc", "vbr", "-cq", qn, "-preset", p]
        elif "vaapi" in codec: args += ["-rc_mode", "CQP", "-qp", qn]
        else: args += ["-crf", qn, "-preset", p]
    elif "Bitrate" in qmode:
        args += ["-b:v", f"{qval}k", "-preset", p]
    else:
        vkbps = calculate_bitrate_for_target_size(infile, float(qval or 700)) or 5000
        args += ["-b:v", f"{vkbps}k", "-preset", p]
    return args

# -------------------- Hauptklasse --------------------

class VideoConverterWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Linux-Video-Enkoder")
        self.set_default_size(1050, 750)
        self.selected_files = []
        self.current_proc = None
        self.stop_event = threading.Event()

        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(b"""
            #btn-start { background-image: none; background-color: #27ae60; color: white; text-shadow: none; }
            #btn-start:hover { background-color: #2ecc71; }
            #btn-exit { background-image: none; background-color: #c0392b; color: white; text-shadow: none; }
            #btn-exit:hover { background-color: #e74c3c; }
        """)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        main_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        main_hbox.set_margin_start(12); main_hbox.set_margin_end(12)
        main_hbox.set_margin_top(12); main_hbox.set_margin_bottom(12)
        self.add(main_hbox)

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

        left_vbox.pack_start(Gtk.Separator(), False, False, 5)

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

        left_vbox.pack_start(Gtk.Separator(), False, False, 5)

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

        grid.attach(Gtk.Label(label="Normalisierung (LUFS):", xalign=0), 0, 2, 1, 1)
        self.volume_spin = Gtk.SpinButton.new_with_range(-30, -5, 1)
        self.volume_spin.set_value(-16)
        grid.attach(self.volume_spin, 1, 2, 1, 1)

        self.audio_copy_chk = Gtk.CheckButton(label="Audio kopieren (Kein Filter)")
        self.audio_copy_chk.connect("toggled", self.on_audio_copy_toggled)
        grid.attach(self.audio_copy_chk, 1, 3, 1, 1)

        grid.attach(Gtk.Label(label="Videoformat:", xalign=0), 0, 4, 1, 1)
        self.video_combo = Gtk.ComboBoxText()
        for s in ["H.264","H.265","AV1","Nur Audio ändern"]: self.video_combo.append_text(s)
        self.video_combo.set_active(0)
        grid.attach(self.video_combo, 1, 4, 1, 1)

        grid.attach(Gtk.Label(label="Qualität Modus:", xalign=0), 0, 5, 1, 1)
        self.quality_combo = Gtk.ComboBoxText()
        for s in ["CQ (Qualitätsbasiert)","Bitrate (kbit/s)","Zieldateigröße (MB)"]: self.quality_combo.append_text(s)
        self.quality_combo.set_active(0)
        self.quality_combo.connect("changed", self.on_quality_mode_changed)
        grid.attach(self.quality_combo, 1, 5, 1, 1)

        self.quality_label = Gtk.Label(label="CRF Wert (0-51):", xalign=0)
        grid.attach(self.quality_label, 0, 6, 1, 1)
        self.quality_entry = Gtk.Entry(text="23")
        grid.attach(self.quality_entry, 1, 6, 1, 1)

        grid.attach(Gtk.Label(label="Analyse-Stufe:", xalign=0), 0, 7, 1, 1)
        self.preset_combo = Gtk.ComboBoxText()
        for p in ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]: self.preset_combo.append_text(p)
        self.preset_combo.set_active(5)
        grid.attach(self.preset_combo, 1, 7, 1, 1)

        left_vbox.pack_start(Gtk.Label(label="Zielordner (leer -> auto):", xalign=0), False, False, 0)
        self.target_entry = Gtk.Entry()
        left_vbox.pack_start(self.target_entry, False, False, 0)

        self.save_in_source_chk = Gtk.CheckButton(label="Im Quellverzeichnis speichern")
        left_vbox.pack_start(self.save_in_source_chk, False, False, 0)

        action_grid = Gtk.Grid(column_spacing=6, row_spacing=6)
        action_grid.set_column_homogeneous(True)
        left_vbox.pack_end(action_grid, False, False, 0)

        self.start_btn = Gtk.Button(label="Konvertieren")
        self.start_btn.set_name("btn-start")
        self.start_btn.connect("clicked", self.start_conversion)
        action_grid.attach(self.start_btn, 0, 0, 1, 1)

        self.cancel_btn = Gtk.Button(label="Abbrechen", sensitive=False)
        self.cancel_btn.connect("clicked", self.cancel_conversion)
        action_grid.attach(self.cancel_btn, 1, 0, 1, 1)

        self.exit_btn = Gtk.Button(label="Programm beenden")
        self.exit_btn.set_name("btn-exit")
        self.exit_btn.connect("clicked", lambda w: self.close())
        action_grid.attach(self.exit_btn, 0, 1, 1, 1)

        self.reset_btn = Gtk.Button(label="Reset")
        self.reset_btn.connect("clicked", self.on_reset_all)
        action_grid.attach(self.reset_btn, 1, 1, 1, 1)

        right_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        main_hbox.pack_start(right_vbox, True, True, 0)
        self.liststore = Gtk.ListStore(str)
        self.treeview = Gtk.TreeView(model=self.liststore)
        self.treeview.append_column(Gtk.TreeViewColumn("Dateien", Gtk.CellRendererText(), text=0))
        self.treeview.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)

        self.treeview.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.treeview.drag_dest_add_uri_targets()
        self.treeview.connect("drag-data-received", self.on_drag_data_received)

        scroll_tree = Gtk.ScrolledWindow(min_content_height=150)
        scroll_tree.add(self.treeview)
        right_vbox.pack_start(scroll_tree, True, True, 0)

        self.file_progress = Gtk.ProgressBar()
        right_vbox.pack_start(self.file_progress, False, False, 0)
        self.total_progress = Gtk.ProgressBar()
        right_vbox.pack_start(self.total_progress, False, False, 0)

        self.log_view = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD)
        scroll_log = Gtk.ScrolledWindow(min_content_height=200)
        scroll_log.add(self.log_view)
        right_vbox.pack_start(scroll_log, True, True, 0)

        self.show_all()

    def on_audio_copy_toggled(self, btn):
        active = btn.get_active()
        self.audio_combo.set_sensitive(not active)
        self.volume_spin.set_sensitive(not active)

    def on_drag_data_received(self, widget, context, x, y, selection, info, time):
        uris = selection.get_uris()
        for uri in uris:
            path = urllib.parse.unquote(uri.replace('file://', ''))
            if os.path.exists(path) and path not in self.selected_files:
                self.selected_files.append(path)
                self.liststore.append([os.path.basename(path)])
        context.finish(True, False, time)

    def on_reset_all(self, btn):
        # 1. Dateien & Logs
        self.selected_files.clear(); self.liststore.clear()
        self.file_progress.set_fraction(0); self.total_progress.set_fraction(0)
        self.log_view.get_buffer().set_text("")

        # 2. Zeitfelder
        self.start_entry.set_text("00:00:00")
        self.duration_limit_entry.set_text("0")

        # 3. Dropdowns (Combo Boxes)
        self.gpu_combo.set_active(0)
        self.upscale_combo.set_active(0)
        self.audio_combo.set_active(0)
        self.video_combo.set_active(0)
        self.quality_combo.set_active(0)
        self.preset_combo.set_active(5) # Medium

        # 4. Audio & Video Einstellungen
        self.volume_spin.set_value(-16)
        self.audio_copy_chk.set_active(False)
        self.quality_entry.set_text("23")

        # 5. Zielordner
        self.target_entry.set_text("")
        self.save_in_source_chk.set_active(False)

    def on_quality_mode_changed(self, combo):
        m = combo.get_active_text()
        if not m: return
        if "CQ" in m: self.quality_label.set_text("CRF (0-51):"); self.quality_entry.set_text("23")
        elif "Bitrate" in m: self.quality_label.set_text("kbit/s:"); self.quality_entry.set_text("5000")
        else: self.quality_label.set_text("MB:"); self.quality_entry.set_text("700")

    def on_select_files(self, btn):
        dialog = Gtk.FileChooserDialog(title="Videos wählen", parent=self, action=Gtk.FileChooserAction.OPEN,
                                     buttons=(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OPEN, Gtk.ResponseType.OK))
        dialog.set_select_multiple(True)
        if dialog.run() == Gtk.ResponseType.OK:
            for f in dialog.get_filenames():
                if f not in self.selected_files:
                    self.selected_files.append(f); self.liststore.append([Path(f).name])
        dialog.destroy()

    def on_remove_selected(self, btn):
        model, paths = self.treeview.get_selection().get_selected_rows()
        for p in reversed(paths):
            idx = p.get_indices()[0]
            del self.selected_files[idx]
            model.remove(model.get_iter(p))

    def on_browse_target(self, btn):
        dialog = Gtk.FileChooserDialog(title="Ziel wählen", parent=self, action=Gtk.FileChooserAction.SELECT_FOLDER,
                                     buttons=(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OPEN, Gtk.ResponseType.OK))
        if dialog.run() == Gtk.ResponseType.OK: self.target_entry.set_text(dialog.get_filename())
        dialog.destroy()

    def on_open_preview(self, btn):
        if not self.selected_files or not VideoPreviewDialog: return
        dialog = VideoPreviewDialog(self, self.selected_files[0])
        if dialog.run() == Gtk.ResponseType.OK:
            s, e = dialog.get_range()
            self.start_entry.set_text(f"{int(s//3600):02d}:{int((s%3600)//60):02d}:{s%60:05.2f}")
            self.duration_limit_entry.set_text(f"{e-s:.2f}")
        dialog.destroy()

    def build_ffmpeg_args(self, infile, outfile):
        sel_gpu = self.gpu_combo.get_active_text()
        gpu = detect_gpu_short().upper() if "Auto" in sel_gpu else sel_gpu.upper()
        vchoice, achoice = self.video_combo.get_active_text(), self.audio_combo.get_active_text()
        qmode, qval = self.quality_combo.get_active_text(), self.quality_entry.get_text()
        upscale = self.upscale_combo.get_active_text()
        preset = self.preset_combo.get_active_text()
        audio_copy = self.audio_copy_chk.get_active()
        target_lufs = int(self.volume_spin.get_value())

        args = []
        if vchoice != "Nur Audio ändern":
            if "NVIDIA" in gpu:
                args += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
            elif "INTEL" in gpu or "AMD" in gpu:
                args += ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi", "-hwaccel_device", "/dev/dri/renderD128"]

        if self.start_entry.get_text() != "00:00:00": args += ["-ss", self.start_entry.get_text()]
        args += ["-i", infile]
        if self.duration_limit_entry.get_text() not in ["0", ""]: args += ["-t", self.duration_limit_entry.get_text()]

        if vchoice == "Nur Audio ändern":
            args += ["-c:v", "copy"]
        else:
            fmt = "H.264" if "H.264" in vchoice else ("H.265" if "H.265" in vchoice else "AV1")
            codec = _select_hw_encoder(fmt, gpu)
            args += _codec_quality_args(codec, qmode, qval, preset, infile)

            res_map = {"720p": "1280:720", "1080p": "1920:1080", "1440p": "2560:1440", "2160p": "3840:2160"}
            scale_res = next((v for k, v in res_map.items() if k in upscale), None)

            if "nvenc" in codec:
                if scale_res:
                    w, h = scale_res.split(':')
                    args += ["-vf", f"scale_cuda={w}:{h}"]
            elif "vaapi" in codec:
                if scale_res:
                    w, h = scale_res.split(':')
                    args += ["-vf", f"scale_vaapi={w}:{h},format=vaapi"]
                else:
                    args += ["-vf", "format=vaapi"]
            elif scale_res:
                args += ["-vf", f"scale={scale_res}:flags=lanczos"]

        if audio_copy:
            args += ["-c:a", "copy"]
        else:
            args += ["-c:a", {"AAC":"aac","PCM":"pcm_s16le","FLAC (mkv)":"flac"}.get(achoice, "aac")]
            args += ["-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"]

        return args

    def append_log(self, text):
        GLib.idle_add(self._safe_append_log, text)
    def _safe_append_log(self, text):
        buf = self.log_view.get_buffer()
        buf.insert(buf.get_end_iter(), text)
        self.log_view.scroll_to_mark(buf.create_mark(None, buf.get_end_iter(), False), 0.0, True, 0.0, 1.0)

    def start_conversion(self, btn):
        if not self.selected_files: return
        self.start_btn.set_sensitive(False); self.cancel_btn.set_sensitive(True)
        self.stop_event.clear()
        threading.Thread(target=self.run_conversion, daemon=True).start()

    def cancel_conversion(self, btn):
        self.stop_event.set()
        if self.current_proc: self.current_proc.terminate()

    def run_conversion(self):
        total = len(self.selected_files)
        audio_copy = self.audio_copy_chk.get_active()

        for idx, infile in enumerate(list(self.selected_files), 1):
            if self.stop_event.is_set(): break
            in_p = Path(infile)

            if audio_copy:
                ext = in_p.suffix if in_p.suffix in [".mp4", ".mkv", ".mov"] else ".mp4"
            else:
                ext = ".mkv" if "FLAC" in self.audio_combo.get_active_text() else ".mp4"

            out_dir = in_p.parent if self.save_in_source_chk.get_active() else Path(self.target_entry.get_text() or in_p.parent / "converted")
            out_dir.mkdir(parents=True, exist_ok=True)
            out_p = make_unique_path(out_dir / (in_p.stem + ext))

            dur = float(self.duration_limit_entry.get_text() or 0) or (probe_duration_seconds(in_p) or 1.0)
            cmd = ["ffmpeg"] + self.build_ffmpeg_args(str(in_p), str(out_p)) + ["-y", str(out_p)]

            self.append_log(f"\nSTART: {in_p.name}\n")
            try:
                self.current_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
                for line in self.current_proc.stdout:
                    self.append_log(line)
                    m = time_re.search(line)
                    if m:
                        pct = min(1.0, (int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))) / dur)
                        GLib.idle_add(self.file_progress.set_fraction, pct)
                        GLib.idle_add(self.total_progress.set_fraction, (idx-1+pct)/total)
                self.current_proc.wait()
            except Exception as e: self.append_log(f"FEHLER: {e}\n")

        self.append_log("\nFERTIG.\n")
        GLib.idle_add(self.start_btn.set_sensitive, True)
        GLib.idle_add(self.cancel_btn.set_sensitive, False)

if __name__ == "__main__":
    win = VideoConverterWindow()
    win.connect("destroy", Gtk.main_quit)
    Gtk.main()
