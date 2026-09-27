#!/usr/bin/env python3
# =======================================================================
# Titel:     Linux Video Enkoder (PyQt6 Tabbed GUI with Layout Switcher)
# Version:   1.2.1 (Integration of External Help Files)
# Autor:     Nightworker / Adaptive UI: Gemini
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

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QLineEdit, QComboBox, QPushButton, QCheckBox,
    QSpinBox, QProgressBar, QTextEdit, QListWidget, QAbstractItemView,
    QFileDialog, QFrame, QToolBar, QSizePolicy, QMessageBox, QStackedWidget
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QAction, QIcon

# --- Import der Vorschau ---
try:
    from video_preview import VideoPreviewDialog
except ImportError:
    VideoPreviewDialog = None

# -------------------- Hilfsfunktionen & Sicherheit --------------------
def which_bin(name):
    return shutil.which(name) is not None

def detect_gpu_short():
    """Sichere GPU-Erkennung ohne Shell-Interpreter (Schutz vor Shell-Injection)."""
    try:
        res = subprocess.run(["lspci"], capture_output=True, text=True, check=False)
        s = res.stdout.lower()
        if "nvidia" in s: return "NVIDIA"
        if "amd" in s or "ati" in s: return "AMD"
        if "intel" in s: return "INTEL"
    except Exception:
        pass
    return "CPU"

def probe_duration_seconds(path: Path):
    if not which_bin("ffprobe"): return None
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path.resolve())
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
    path = path.resolve()
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

def sanitize_time_str(time_str: str, default: str = "00:00:00") -> str:
    """Prüft, ob der Zeit-String das Format HH:MM:SS oder SS(.ms) einhält."""
    time_str = time_str.strip()
    if re.match(r"^(\d{2}:)?\d{2}:\d{2}(\.\d+)?$", time_str) or re.match(r"^\d+(\.\d+)?$", time_str):
        return time_str
    return default

def sanitize_int(val_str: str, default: int = 0) -> int:
    """Extrahiert sicher Ganzzahlen aus Benutzereingaben."""
    try:
        return abs(int(re.sub(r"[^\d]", "", val_str)))
    except ValueError:
        return default

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

_ENCODER_MAP = {
    "H.264": {"NVIDIA": ["h264_nvenc"], "AMD": ["h264_vaapi"], "INTEL": ["h264_vaapi"], "CPU": ["libx264"]},
    "H.265": {"NVIDIA": ["hevc_nvenc"], "AMD": ["hevc_vaapi"], "INTEL": ["hevc_vaapi"], "CPU": ["libx265"]},
    "VP9":   {"NVIDIA": [], "AMD": ["vp9_vaapi"], "INTEL": ["vp9_vaapi"], "CPU": ["libvpx-vp9"]},
    "AV1":   {"NVIDIA": ["av1_nvenc"], "AMD": ["av1_vaapi"], "INTEL": ["av1_vaapi"], "CPU": ["libsvtav1"]},
}

def _select_encoder(fmt, mode):
    candidates = _ENCODER_MAP.get(fmt, {}).get(mode, [])
    for enc in candidates:
        if is_encoder_available(enc): return enc
    return {"H.264":"libx264", "H.265":"libx265", "VP9":"libvpx-vp9", "AV1":"libsvtav1"}.get(fmt, "libx264")

def _codec_quality_args(codec, qmode, qval_raw, preset, infile, force_cbr=False):
    args = ["-c:v", codec]

    if "nvenc" in codec:
        p_map = {"ultrafast":"p1","superfast":"p2","veryfast":"p3","faster":"p4","fast":"p5","medium":"p6","slow":"p7"}
        p = p_map.get(preset, "p5")
    elif "libsvtav1" in codec:
        svt_map = {
            "ultrafast": "12", "superfast": "11", "veryfast": "10",
            "faster": "8", "fast": "7", "medium": "6",
            "slow": "4", "slower": "3", "veryslow": "2"
        }
        p = svt_map.get(preset, "7")
    else:
        p = preset

    if "CQ" in qmode:
        qn = str(sanitize_int(qval_raw, default=23))
        if "nvenc" in codec: args += ["-rc", "vbr", "-cq", qn, "-preset", p]
        elif "vaapi" in codec: args += ["-rc_mode", "CQP", "-qp", qn]
        elif "libvpx-vp9" in codec: args += ["-crf", qn, "-b:v", "0"]
        else: args += ["-crf", qn, "-preset", p]
    elif "Bitrate" in qmode:
        kbps = str(sanitize_int(qval_raw, default=5000))
        args += ["-b:v", f"{kbps}k"]

        # CBR vs. VBR Steuerung
        if force_cbr:
            if "nvenc" in codec:
                args += ["-rc", "cbr", "-preset", p]
            elif "vaapi" in codec:
                args += ["-rc_mode", "CBR"]
            else:
                args += ["-minrate", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{int(kbps)*2}k"]
                if "libvpx-vp9" not in codec: args += ["-preset", p]
        else:
            if "nvenc" in codec:
                args += ["-rc", "vbr", "-preset", p]
            elif "vaapi" in codec:
                args += ["-rc_mode", "VBR"]
            elif "libvpx-vp9" not in codec:
                args += ["-preset", p]
    else:
        target_mb = sanitize_int(qval_raw, default=700)
        vkbps = calculate_bitrate_for_target_size(infile, target_mb) or 5000
        args += ["-b:v", f"{vkbps}k"]
        if "libvpx-vp9" not in codec: args += ["-preset", p]
    return args


# -------------------- Drag and Drop ListWidget --------------------
class FileListWidget(QListWidget):
    """Custom ListWidget mit Drag & Drop Unterstützung für Videodateien."""
    def __init__(self, parent_window):
        super().__init__()
        self.parent_window = parent_window
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                file_path = url.toLocalFile()
                if os.path.exists(file_path) and file_path not in self.parent_window.selected_files:
                    self.parent_window.selected_files.append(file_path)
                    self.addItem(os.path.basename(file_path))
            event.acceptProposedAction()

# -------------------- Worker Signals für Threading --------------------
class ConversionSignals(QObject):
    log_signal = pyqtSignal(str)
    file_label_signal = pyqtSignal(str)
    file_progress_signal = pyqtSignal(float)
    total_progress_signal = pyqtSignal(float)
    finished_signal = pyqtSignal()

# -------------------- Hauptfenster --------------------
class VideoConverterWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GuideOS Videokonverter")
        self.resize(870, 810)

        self.selected_files = []
        self.current_proc = None
        self.stop_event = threading.Event()
        self.signals = ConversionSignals()

        # Signal-Verbindungen (Threadsicher)
        self.signals.log_signal.connect(self._safe_append_log)
        self.signals.file_label_signal.connect(self._safe_set_file_label)
        self.signals.file_progress_signal.connect(self._safe_set_file_progress)
        self.signals.total_progress_signal.connect(self._safe_set_total_progress)
        self.signals.finished_signal.connect(self._on_conversion_finished)

        self._init_ui()
        self._apply_styles()

    def change_layout(self):
        """Öffnet den Starter-Dialog zum Auswählen des Layouts und beendet das aktuelle Skript."""
        starter_path = Path("/usr/lib/guideos-videokonverter/guideos-videokonverter-start.py")

        if not starter_path.exists():
            starter_path = Path(__file__).parent / "starter.py"

        subprocess.Popen([sys.executable, str(starter_path), "--select"])
        self.close()

    def open_help_dialog(self):
        """Lädt den Hilfetext aus hilfe.html und zeigt ihn als RichText (HTML) an."""
        possible_paths = [
            Path(__file__).parent / "hilfe.html",
            Path("/usr/lib/guideos-videokonverter/hilfe.html")
        ]

        help_text = None
        for p in possible_paths:
            if p.exists():
                try:
                    help_text = p.read_text(encoding="utf-8")
                    break
                except Exception as e:
                    help_text = f"Fehler beim Lesen der Hilfedatei: {e}"

        if not help_text:
            help_text = "<b>Fehler:</b> Die Hilfedatei (hilfe.html) konnte nicht gefunden werden."

        help_msg = QMessageBox(self)
        help_msg.setWindowTitle("Hilfe & Skriptbeschreibung")
        help_msg.setIcon(QMessageBox.Icon.Information)

        # Aktiviert sauberes HTML-Rendering für den Nachrichtentext
        help_msg.setTextFormat(Qt.TextFormat.RichText)
        help_msg.setText(help_text)

        help_msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        help_msg.exec()

    def _apply_styles(self):
        self.setStyleSheet("""
            #btn-start { background-color: #27ae60; color: white; border-radius: 4px; padding: 6px; font-weight: bold; }
            #btn-start:hover { background-color: #2ecc71; }
            #btn-exit { background-color: #c0392b; color: white; border-radius: 4px; padding: 6px; font-weight: bold; }
            #btn-exit:hover { background-color: #e74c3c; }
            .prog-label { font-weight: bold; margin-top: 5px; }
            QToolBar {border: none; background: transparent; }
        """)

    def _init_ui(self):
        toolbar = QToolBar()
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        # Hilfe-Button
        action_help = QAction(QIcon.fromTheme("help-browser"), " Hilfe", self)
        action_help.setToolTip("Öffnet die Hilfedatei")
        action_help.triggered.connect(self.open_help_dialog)
        toolbar.addAction(action_help)

        # Layout-Wechsel Button
        action_layout = QAction(QIcon.fromTheme("preferences-desktop"), " Layout wechseln", self)
        action_layout.setToolTip("Öffnet die Layout-Auswahl und startet die Anwendung neu")
        action_layout.triggered.connect(self.change_layout)
        toolbar.addAction(action_layout)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_hbox = QHBoxLayout(central_widget)
        main_hbox.setContentsMargins(12, 12, 12, 12)
        main_hbox.setSpacing(12)

        # --- Linke Spalte ---
        left_vbox = QVBoxLayout()
        main_hbox.addLayout(left_vbox, stretch=0)

        left_vbox.addWidget(QLabel("Erkannte Grafikkarte:"))
        self.gpu_entry = QLineEdit(detect_gpu_short())
        self.gpu_entry.setReadOnly(True)
        left_vbox.addWidget(self.gpu_entry)

        left_vbox.addWidget(QLabel("GPU / CPU Auswahl:"))
        self.gpu_combo = QComboBox()
        self.gpu_combo.addItems(["Automatisch (empfohlen)", "NVIDIA", "AMD", "Intel", "Software (CPU)"])
        self.gpu_combo.currentIndexChanged.connect(self._check_codec_hardware_support)
        left_vbox.addWidget(self.gpu_combo)

        self.btn_files = QPushButton("Dateien auswählen")
        self.btn_files.clicked.connect(self.on_select_files)
        left_vbox.addWidget(self.btn_files)

        self.btn_remove = QPushButton("Ausgewählte entfernen")
        self.btn_remove.clicked.connect(self.on_remove_selected)
        left_vbox.addWidget(self.btn_remove)

        self.btn_target = QPushButton("Zielverzeichnis wählen")
        self.btn_target.clicked.connect(self.on_browse_target)
        left_vbox.addWidget(self.btn_target)

        line1 = QFrame()
        line1.setFrameShape(QFrame.Shape.HLine)
        left_vbox.addWidget(line1)

        self.btn_preview = QPushButton("Schnittbereich festlegen (Vorschau)")
        self.btn_preview.clicked.connect(self.on_open_preview)
        left_vbox.addWidget(self.btn_preview)

        grid_time = QGridLayout()
        grid_time.setSpacing(5)
        grid_time.addWidget(QLabel("Startzeit:"), 0, 0)
        self.start_entry = QLineEdit("00:00:00")
        grid_time.addWidget(self.start_entry, 0, 1)
        grid_time.addWidget(QLabel("Dauer (sek):"), 1, 0)
        self.duration_limit_entry = QLineEdit("0")
        grid_time.addWidget(self.duration_limit_entry, 1, 1)
        left_vbox.addLayout(grid_time)

        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.HLine)
        left_vbox.addWidget(line2)

        grid = QGridLayout()
        grid.setSpacing(8)

        grid.addWidget(QLabel("Container-Format:"), 0, 0)
        self.format_combo = QComboBox()
        self.format_combo.addItems(["MP4 (.mp4)", "Matroska (.mkv)", "WebM (.webm)"])
        self.format_combo.currentIndexChanged.connect(self.on_format_changed)
        grid.addWidget(self.format_combo, 0, 1)

        grid.addWidget(QLabel("Dimension:"), 1, 0)
        self.dimension_combo = QComboBox()
        self.dimension_combo.addItems(["Original", "720p (1280x720)", "1080p (1920x1080)", "1440p (2560x1440)", "2160p (3840x2160)"])
        grid.addWidget(self.dimension_combo, 1, 1)

        sharp_label = QLabel("Nachschärfung (Lanczos):")
        sharp_label.setToolTip("Schärft skaliertes Videomaterial mit dem Unsharp-Filter nach.")
        grid.addWidget(sharp_label, 2, 0)
        self.sharpness_combo = QComboBox()
        self.sharpness_combo.addItems(["Keine", "Leicht", "Mittel", "Stark"])
        self.sharpness_combo.setCurrentIndex(0)
        grid.addWidget(self.sharpness_combo, 2, 1)

        grid.addWidget(QLabel("Audioformat:"), 3, 0)
        self.audio_combo = QComboBox()
        self.audio_combo.addItems(["Original kopieren (Kein Filter)", "AAC", "Opus (WebM/MKV)", "PCM", "FLAC (mkv)"])
        self.audio_combo.setCurrentIndex(0)
        self.audio_combo.currentIndexChanged.connect(self.on_audio_format_changed)
        grid.addWidget(self.audio_combo, 3, 1)

        norm_label = QLabel("Normalisierung (LUFS):")
        norm_label.setToolTip("Passt die Lautstärke auf einen Standardwert an (Loudness Normalization).\nEmpfehlung: -16 für Web, -23 für Fernsehnorm.")
        grid.addWidget(norm_label, 4, 0)
        self.volume_spin = QSpinBox()
        self.volume_spin.setRange(-30, -5)
        self.volume_spin.setValue(-16)
        self.volume_spin.setEnabled(False)
        grid.addWidget(self.volume_spin, 4, 1)

        grid.addWidget(QLabel("Video-Codec:"), 5, 0)
        self.video_combo = QComboBox()
        self.video_combo.addItems(["H.264", "H.265", "VP9", "AV1", "Nur Audio ändern"])
        self.video_combo.setToolTip(
            "• H.264 / H.265: Fast überall per Hardware beschleunigt\n"
            "• VP9: HW-Beschleunigung primär auf Intel QuickSync / AMD\n"
            "• AV1: HW-Beschleunigung nur auf neueren GPUs (RTX 40xx, RX 7000, Intel Arc)"
        )
        self.video_combo.currentIndexChanged.connect(self._check_codec_hardware_support)
        grid.addWidget(self.video_combo, 5, 1)

        grid.addWidget(QLabel("Farbtiefe:"), 6, 0)
        self.bit_combo = QComboBox()
        self.bit_combo.addItems(["8-Bit (Standard)", "10-Bit (HDR/High)"])
        grid.addWidget(self.bit_combo, 6, 1)

        grid.addWidget(QLabel("Qualität Modus:"), 7, 0)
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(["CQ (Qualitätsbasiert)", "Bitrate (kbit/s)", "Zieldateigröße (MB)"])
        self.quality_combo.currentIndexChanged.connect(self.on_quality_mode_changed)
        grid.addWidget(self.quality_combo, 7, 1)

        # Umschaltung dynamisches Label / Bitratenmodus (VBR / CBR)
        self.quality_mode_label = QLabel("Bitratenmodus:")
        grid.addWidget(self.quality_mode_label, 8, 0)

        # Dynamic Stack für Dropdown (VBR/CBR) oder leeres Widget
        self.mode_stack = QStackedWidget()
        self.empty_widget = QWidget()
        self.bitrate_mode_combo = QComboBox()
        self.bitrate_mode_combo.addItems(["VBR (Variable Bitrate)", "CBR (Konstante Bitrate)"])

        self.mode_stack.addWidget(self.empty_widget)
        self.mode_stack.addWidget(self.bitrate_mode_combo)
        grid.addWidget(self.mode_stack, 8, 1)

        self.quality_label = QLabel("CRF (0-51):")
        self.quality_label.setToolTip("Der CRF Wert bestimmt die Qualität.\nEin kleinerer Wert bedeutet höhere Qualität, aber auch eine größere Ausgabedatei.")
        grid.addWidget(self.quality_label, 9, 0)
        self.quality_entry = QLineEdit("23")
        grid.addWidget(self.quality_entry, 9, 1)

        preset_label = QLabel("Analyse-Stufe:")
        preset_label.setToolTip("Wählt das Codierungs-Preset (Encoder-Aufwand).\nHöhere Stufen (slow/slower) analysieren das Video gründlicher, das optimiert das Video-File, erhöht jedoch die Renderzeit")
        grid.addWidget(preset_label, 10, 0)
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"])
        self.preset_combo.setCurrentIndex(4)  # Standard: fast
        grid.addWidget(self.preset_combo, 10, 1)

        left_vbox.addLayout(grid)

        # Initiale Einstellung des Qualitätsmodus
        self.on_quality_mode_changed(0)

        self.hw_warning_label = QLabel("")
        self.hw_warning_label.setWordWrap(True)
        left_vbox.addWidget(self.hw_warning_label)

        left_vbox.addWidget(QLabel("Zielordner (leer -> auto):"))
        self.target_entry = QLineEdit()
        left_vbox.addWidget(self.target_entry)

        self.save_in_source_chk = QCheckBox("Im Quellverzeichnis speichern")
        left_vbox.addWidget(self.save_in_source_chk)

        self.keep_rotation_chk = QCheckBox("Metadaten-Rotation (9:16) beibehalten")
        self.keep_rotation_chk.setChecked(True)
        self.keep_rotation_chk.setToolTip("Verhindert, dass FFmpeg das Video fälschlicherweise in ein 16:9 Querformat zwingt.\nPerfekt für Clips von Smartphones, die ein 90°-Flag besitzen.")
        left_vbox.addWidget(self.keep_rotation_chk)

        action_grid = QGridLayout()
        self.start_btn = QPushButton("Konvertieren")
        self.start_btn.setObjectName("btn-start")
        self.start_btn.clicked.connect(self.start_conversion)
        action_grid.addWidget(self.start_btn, 0, 0)

        self.cancel_btn = QPushButton("Abbrechen")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_conversion)
        action_grid.addWidget(self.cancel_btn, 0, 1)

        self.exit_btn = QPushButton("Programm beenden")
        self.exit_btn.setObjectName("btn-exit")
        self.exit_btn.clicked.connect(self.close)
        action_grid.addWidget(self.exit_btn, 1, 0)

        self.reset_btn = QPushButton("Reset")
        self.reset_btn.clicked.connect(self.on_reset_all)
        action_grid.addWidget(self.reset_btn, 1, 1)

        left_vbox.addLayout(action_grid)

        # --- Rechte Spalte ---
        right_vbox = QVBoxLayout()
        main_hbox.addLayout(right_vbox, stretch=1)

        self.file_list = FileListWidget(self)
        right_vbox.addWidget(self.file_list, stretch=1)

        self.file_label = QLabel("Fortschritt: Keine Datei aktiv")
        self.file_label.setProperty("class", "prog-label")
        right_vbox.addWidget(self.file_label)

        self.file_progress = QProgressBar()
        self.file_progress.setRange(0, 100)
        self.file_progress.setValue(0)
        right_vbox.addWidget(self.file_progress)

        self.total_label = QLabel("Gesamtfortschritt")
        self.total_label.setProperty("class", "prog-label")
        right_vbox.addWidget(self.total_label)

        self.total_progress = QProgressBar()
        self.total_progress.setRange(0, 100)
        self.total_progress.setValue(0)
        right_vbox.addWidget(self.total_progress)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        right_vbox.addWidget(self.log_view, stretch=1)

    # -------------------- Slots & Events --------------------

    def _check_codec_hardware_support(self, *args):
        codec = self.video_combo.currentText() or ""
        gpu_sel = self.gpu_combo.currentText() or ""

        detected_gpu = detect_gpu_short()
        gpu = detected_gpu if "Automatisch" in gpu_sel else gpu_sel.upper()

        warning_text = ""

        if "VP9" in codec and "NVIDIA" in gpu:
            warning_text = "⚠️ <b>Hinweis (VP9):</b> NVIDIA bietet kein HW-Encoding für VP9.<br>Für Enkodierung bitte oben <b>Software (CPU)</b> wählen."
        elif "AV1" in codec and "NVIDIA" in gpu:
            warning_text = "💡 <b>Hinweis (AV1):</b> HW-Encoding benötigt eine <b>RTX 40xx+</b>.<br>Falls du eine ältere Karte nutzt, bitte auf <b>Software (CPU)</b> ausweichen."
        elif "AV1" in codec and "AMD" in gpu:
            warning_text = "💡 <b>Hinweis (AV1):</b> HW-Encoding erfordert eine <b>Radeon RX 7000+</b>.<br>Falls du eine ältere Karte nutzt, bitte auf <b>Software (CPU)</b> ausweichen."
        elif "AV1" in codec and "INTEL" in gpu:
            warning_text = "💡 <b>Hinweis (AV1):</b> HW-Encoding benötigt eine <b>Intel Arc / QuickSync AV1</b> GPU.<br>Falls nicht vorhanden, bitte auf <b>Software (CPU)</b> ausweichen."

        if warning_text:
            self.hw_warning_label.setText(f'<span style="color: #d35400;"><small>{warning_text}</small></span>')
        else:
            self.hw_warning_label.setText("")

    def _update_video_codecs_for_container(self):
        container = self.format_combo.currentText() or ""
        current_codec = self.video_combo.currentText() or ""

        self.video_combo.blockSignals(True)
        self.video_combo.clear()

        if "WebM" in container:
            valid_codecs = ["VP9", "AV1", "Nur Audio ändern"]
            self.video_combo.addItems(valid_codecs)
            if current_codec in valid_codecs:
                self.video_combo.setCurrentIndex(valid_codecs.index(current_codec))
            else:
                self.video_combo.setCurrentIndex(0)
        else:
            all_codecs = ["H.264", "H.265", "VP9", "AV1", "Nur Audio ändern"]
            self.video_combo.addItems(all_codecs)
            if current_codec in all_codecs:
                self.video_combo.setCurrentIndex(all_codecs.index(current_codec))
            else:
                self.video_combo.setCurrentIndex(0)

        self.video_combo.blockSignals(False)
        self._check_codec_hardware_support()

    def on_format_changed(self, index):
        fmt = self.format_combo.currentText()
        self._update_video_codecs_for_container()

        if fmt and "WebM" in fmt:
            self.audio_combo.setCurrentIndex(2)
        elif fmt and ("MP4" in fmt or "Matroska" in fmt):
            if self.audio_combo.currentIndex() == 2:
                self.audio_combo.setCurrentIndex(0)

    def on_audio_format_changed(self, index):
        is_copy = "Original kopieren" in self.audio_combo.currentText()
        self.volume_spin.setEnabled(not is_copy)

    def on_reset_all(self):
        self.selected_files.clear()
        self.file_list.clear()
        self.file_progress.setValue(0)
        self.total_progress.setValue(0)
        self.file_label.setText("Fortschritt: Keine Datei aktiv")
        self.log_view.clear()
        self.start_entry.setText("00:00:00")
        self.duration_limit_entry.setText("0")
        self.gpu_combo.setCurrentIndex(0)
        self.format_combo.setCurrentIndex(0)
        self._update_video_codecs_for_container()
        self.dimension_combo.setCurrentIndex(0)
        self.sharpness_combo.setCurrentIndex(0)
        self.audio_combo.setCurrentIndex(0)
        self.video_combo.setCurrentIndex(0)
        self.bit_combo.setCurrentIndex(0)
        self.quality_combo.setCurrentIndex(0)
        self.preset_combo.setCurrentIndex(4)
        self.volume_spin.setValue(-16)
        self.quality_entry.setText("23")
        self.target_entry.setText("")
        self.save_in_source_chk.setChecked(False)
        self.keep_rotation_chk.setChecked(True)
        self._check_codec_hardware_support()

    def on_quality_mode_changed(self, index):
        m = self.quality_combo.currentText()
        if not m: return

        if "Bitrate" in m:
            self.quality_mode_label.show()
            self.quality_mode_label.setText("Bitratenmodus:")
            self.mode_stack.setCurrentIndex(1)
            self.quality_label.setText("kbit/s:")
            self.quality_entry.setText("5000")
        elif "CQ" in m:
            self.quality_mode_label.hide()
            self.mode_stack.setCurrentIndex(0)
            self.quality_label.setText("CRF (0-51):")
            self.quality_entry.setText("23")
        else:
            self.quality_mode_label.hide()
            self.mode_stack.setCurrentIndex(0)
            self.quality_label.setText("MB:")
            self.quality_entry.setText("700")

    def on_select_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Videos wählen", "", "Video Files (*.mp4 *.mkv *.avi *.mov *.webm *.flv *.wmv)")
        if files:
            for f in files:
                if f not in self.selected_files:
                    self.selected_files.append(f)
                    self.file_list.addItem(Path(f).name)

    def on_remove_selected(self):
        selected_items = self.file_list.selectedItems()
        if not selected_items: return
        for item in selected_items:
            row = self.file_list.row(item)
            del self.selected_files[row]
            self.file_list.takeItem(row)

    def on_browse_target(self):
        folder = QFileDialog.getExistingDirectory(self, "Ziel wählen")
        if folder:
            self.target_entry.setText(folder)

    def on_open_preview(self):
        if not self.selected_files or not VideoPreviewDialog:
            return

        dialog = VideoPreviewDialog(self, self.selected_files[0])
        if dialog.exec() == VideoPreviewDialog.DialogCode.Accepted:
            s, e = dialog.get_range()
            start_formatted = f"{int(s//3600):02d}:{int((s%3600)//60):02d}:{s%60:05.2f}"
            self.start_entry.setText(start_formatted)
            duration_diff = max(0.0, e - s)
            self.duration_limit_entry.setText(f"{duration_diff:.2f}")

    def build_ffmpeg_args(self, infile, outfile):
        sel_text = self.gpu_combo.currentText()
        keep_rotation = self.keep_rotation_chk.isChecked()
        container_choice = self.format_combo.currentText()
        is_webm = "WebM" in container_choice

        if "NVIDIA" in sel_text: hw_mode = "NVIDIA"
        elif "AMD" in sel_text: hw_mode = "AMD"
        elif "Intel" in sel_text: hw_mode = "INTEL"
        elif "Software" in sel_text: hw_mode = "CPU"
        else: hw_mode = detect_gpu_short().upper()

        vchoice, achoice = self.video_combo.currentText(), self.audio_combo.currentText()
        qmode, qval_raw = self.quality_combo.currentText(), self.quality_entry.text()

        force_cbr = "CBR" in self.bitrate_mode_combo.currentText() if "Bitrate" in qmode else False

        upscale = self.dimension_combo.currentText()
        sharp_mode = self.sharpness_combo.currentText()
        preset = self.preset_combo.currentText()
        target_lufs = int(self.volume_spin.value())
        is_10bit = "10-Bit" in self.bit_combo.currentText()

        if is_webm:
            if vchoice not in ["VP9", "AV1"]:
                vchoice = "VP9"

        args = []

        if keep_rotation:
            args += ["-noautorotate"]

        if vchoice != "Nur Audio ändern" and hw_mode != "CPU":
            if "NVIDIA" in hw_mode:
                args += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
            elif "INTEL" in hw_mode or "AMD" in hw_mode:
                args += ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi", "-hwaccel_device", "/dev/dri/renderD128"]

        start_time = sanitize_time_str(self.start_entry.text(), "00:00:00")
        if start_time != "00:00:00":
            args += ["-ss", start_time]

        args += ["-i", str(Path(infile).resolve())]

        raw_dur = self.duration_limit_entry.text().strip().replace(',', '.')
        try:
            dur_float = float(raw_dur)
            if dur_float > 0:
                args += ["-t", f"{dur_float:.2f}"]
        except ValueError:
            pass

        if vchoice == "Nur Audio ändern":
            args += ["-c:v", "copy"]
        else:
            if "H.264" in vchoice: fmt = "H.264"
            elif "H.265" in vchoice: fmt = "H.265"
            elif "VP9" in vchoice: fmt = "VP9"
            else: fmt = "AV1"

            codec = _select_encoder(fmt, hw_mode)
            args += _codec_quality_args(codec, qmode, qval_raw, preset, infile, force_cbr=force_cbr)

            if is_10bit and "vaapi" not in codec and "nvenc" not in codec:
                args += ["-pix_fmt", "yuv420p10le"]
            elif not is_10bit and "vaapi" not in codec and "nvenc" not in codec:
                args += ["-pix_fmt", "yuv420p"]

            res_map = {"720p": "1280", "1080p": "1920", "1440p": "2560", "2160p": "3840"}
            target_w = next((v for k, v in res_map.items() if k in upscale), None)

            # Unsharp-Filter Parameter
            unsharp_cmd = ""
            if "Leicht" in sharp_mode:
                unsharp_cmd = "unsharp=3:3:0.5:3:3:0.0"
            elif "Mittel" in sharp_mode:
                unsharp_cmd = "unsharp=5:5:1.0:5:5:0.0"
            elif "Stark" in sharp_mode:
                unsharp_cmd = "unsharp=7:7:1.5:7:7:0.0"

            # Filter-Pipeline
            vf_filters = []

            if "nvenc" in codec:
                if target_w:
                    vf_filters.append(f"scale_cuda={target_w}:-1")
                if unsharp_cmd:
                    vf_filters.append("hwdownload,format=nv12")
                    vf_filters.append(unsharp_cmd)
                    vf_filters.append("hwupload_cuda")
            elif "vaapi" in codec:
                vfmt = "p010le" if is_10bit else "nv12"
                if target_w:
                    vf_filters.append(f"scale_vaapi={target_w}:-2")
                if unsharp_cmd:
                    vf_filters.append("hwdownload")
                    vf_filters.append(unsharp_cmd)
                    vf_filters.append("hwupload")
                vf_filters.append(f"format=vaapi|{vfmt}")
            else:
                if target_w:
                    vf_filters.append(f"scale={target_w}:-2:flags=lanczos")
                if unsharp_cmd:
                    vf_filters.append(unsharp_cmd)

            if vf_filters:
                args += ["-vf", ",".join(vf_filters)]

        if keep_rotation:
            args += ["-metadata:s:v:0", "rotate=90"]

        # Audio-Logik
        if "Original kopieren" in achoice and not is_webm:
            args += ["-c:a", "copy"]
        else:
            a_codec_map = {
                "Opus (WebM/MKV)": "libopus",
                "AAC": "aac",
                "PCM": "pcm_s16le",
                "FLAC (mkv)": "flac"
            }
            if is_webm:
                a_codec = "libopus"
            else:
                a_codec = a_codec_map.get(achoice, "aac")

            args += ["-c:a", a_codec]

            audio_filters = []
            if achoice == "PCM" or vchoice in ["AV1", "VP9"] or is_webm:
                args += ["-ar", "48000"]
                audio_filters.append("aresample=48000")

            audio_filters.append(f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11")
            args += ["-af", ",".join(audio_filters)]

        return args

    # -------------------- Threadsichere GUI Updates --------------------
    def _safe_append_log(self, text):
        self.log_view.append(text)

    def _safe_set_file_label(self, text):
        self.file_label.setText(text)

    def _safe_set_file_progress(self, val):
        self.file_progress.setValue(int(val * 100))

    def _safe_set_total_progress(self, val):
        self.total_progress.setValue(int(val * 100))

    def _on_conversion_finished(self):
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

    # -------------------- Konvertierungs-Thread --------------------
    def start_conversion(self):
        if not self.selected_files: return
        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.stop_event.clear()
        threading.Thread(target=self.run_conversion, daemon=True).start()

    def cancel_conversion(self):
        self.stop_event.set()
        if self.current_proc:
            self.current_proc.terminate()

    def run_conversion(self):
        total = len(self.selected_files)
        container_choice = self.format_combo.currentText()
        audio_format = self.audio_combo.currentText()

        for idx, infile in enumerate(list(self.selected_files), 1):
            if self.stop_event.is_set(): break
            in_p = Path(infile).resolve()
            self.signals.file_label_signal.emit(f"Fortschritt: {in_p.name}")

            if container_choice and "WebM" in container_choice:
                ext = ".webm"
            elif audio_format and "FLAC" in audio_format:
                ext = ".mkv"
            elif container_choice and "MP4" in container_choice:
                ext = ".mp4"
            else:
                ext = ".mkv"

            target_val = self.target_entry.text().strip()
            if self.save_in_source_chk.isChecked():
                out_dir = in_p.parent
            elif target_val:
                out_dir = Path(target_val).resolve()
            else:
                out_dir = in_p.parent / "converted"

            out_dir.mkdir(parents=True, exist_ok=True)
            out_p = make_unique_path(out_dir / (in_p.stem + ext))

            dur_str = sanitize_time_str(self.duration_limit_entry.text(), "0")
            dur = float(dur_str) if dur_str != "0" else (probe_duration_seconds(in_p) or 1.0)

            cmd = ["ffmpeg"] + self.build_ffmpeg_args(str(in_p), str(out_p)) + ["-y", str(out_p)]

            self.signals.log_signal.emit(f"\nSTART: {in_p.name}\n")
            try:
                self.current_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
                for line in self.current_proc.stdout:
                    self.signals.log_signal.emit(line.strip())
                    m = time_re.search(line)
                    if m:
                        pct = min(1.0, (int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))) / dur)
                        self.signals.file_progress_signal.emit(pct)
                        self.signals.total_progress_signal.emit((idx-1+pct)/total)

                return_code = self.current_proc.wait()

                if return_code != 0 and not self.stop_event.is_set():
                    self.signals.log_signal.emit("FEHLER: Konvertierung fehlgeschlagen.\n")
            except Exception as e:
                self.signals.log_signal.emit(f"FEHLER: {e}\n")

        self.signals.log_signal.emit("\nFERTIG.\n")
        self.signals.file_label_signal.emit("Konvertierung abgeschlossen")
        self.signals.finished_signal.emit()


if __name__ == "__main__":
    os.environ["QT_QPA_PLATFORM_APP_ID"] = "guideos-videokonverter"

    app = QApplication(sys.argv)
    app.setDesktopFileName("guideos-videokonverter")

    icon_path = "/usr/share/pixmaps/guideos-videokonverter.png"
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    window = VideoConverterWindow()
    window.show()
    sys.exit(app.exec())
