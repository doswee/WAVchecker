import os
import sys
import csv
import shutil
import subprocess
import wave
from tinytag import TinyTag

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QFileDialog, QTableWidget, QTableWidgetItem,
    QHeaderView, QLabel, QFrame, QAbstractItemView, QProgressBar,
    QMessageBox, QDialog, QComboBox, QLineEdit
)
from PySide6.QtCore import (
    Qt, QRunnable, Slot, Signal, QObject, QThreadPool
)
from PySide6.QtGui import (
    QFont, QKeyEvent, QColor, QPalette, QPainter
)

# -----------------------------
# FFmpeg Setup
# -----------------------------
FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"

# -----------------------------
# Utility / Sorting
# -----------------------------
class NumberSortItem(QTableWidgetItem):
    def __init__(self, text, value):
        super().__init__(text)
        self.value = value

    def __lt__(self, other):
        return self.value < other.value

def format_size(size_bytes):
    if not size_bytes: return "0 MB"
    size_mb = size_bytes / (1024 * 1024)
    if size_mb >= 1024:
        return f"{round(size_mb / 1024, 2)} GB"
    return f"{round(size_mb, 2)} MB"

def format_duration(seconds):
    if not seconds: return "00:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0: return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

# -----------------------------
# Workers & Threading
# -----------------------------
class WorkerSignals(QObject):
    discovery_finished = Signal(list)
    analysis_result = Signal(int, dict)
    analysis_finished = Signal()
    conversion_result = Signal(int, str, dict)
    conversion_finished = Signal()

class FileDiscoveryWorker(QRunnable):
    def __init__(self, paths):
        super().__init__()
        self.paths = paths
        self.signals = WorkerSignals()
        self.valid_exts = (".wav", ".mp3", ".flac", ".aif", ".aiff", ".m4a", ".ogg", ".wma", ".aac")

    @Slot()
    def run(self):
        found_files = []
        for path in self.paths:
            if os.path.isdir(path):
                for root_dir, _, files in os.walk(path):
                    for f in files:
                        if f.lower().endswith(self.valid_exts) and not f.startswith("."):
                            found_files.append(os.path.join(root_dir, f))
            elif path.lower().endswith(self.valid_exts):
                if not os.path.basename(path).startswith("."):
                    found_files.append(path)
        found_files.sort()
        self.signals.discovery_finished.emit(found_files)

class AnalysisWorker(QRunnable):
    def __init__(self, start_row, files, stop_func):
        super().__init__()
        self.start_row = start_row
        self.files = files
        self.stop_func = stop_func
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        for i, path in enumerate(self.files):
            if self.stop_func(): break
            row = self.start_row + i
            data = analyze_file(path)
            self.signals.analysis_result.emit(row, data)
        self.signals.analysis_finished.emit()

class ConversionWorker(QRunnable):
    def __init__(self, row, input_path, target_sr, target_bd, original_data, out_dir, stop_func):
        super().__init__()
        self.row = row
        self.input_path = input_path
        self.target_sr = target_sr
        self.target_bd = target_bd
        self.original_data = original_data
        self.out_dir = out_dir
        self.stop_func = stop_func
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        if self.stop_func(): return
        
        filename = os.path.basename(self.input_path)
        name_no_ext = os.path.splitext(filename)[0]
        out_path = os.path.join(self.out_dir, f"{name_no_ext}.wav")
        
        # Avoid overwriting
        if os.path.abspath(self.input_path) == os.path.abspath(out_path):
            out_path = os.path.join(self.out_dir, f"{name_no_ext}_converted.wav")

        # 1. Determine Actual Target Parameters
        actual_sr = self.original_data['sample_rate'] if self.target_sr == "Original" else int(self.target_sr)
        
        channels = self.original_data['channels']
        if channels < 1: channels = 2 # Safey fallback

        actual_bd_str = self.target_bd
        if actual_bd_str == "Original":
            detected_bd = self.original_data['bit_depth']
            if detected_bd <= 16: actual_bd_str = "16-bit"
            elif detected_bd == 32: actual_bd_str = "32-bit Float"
            else: actual_bd_str = "24-bit"

        is_float = (actual_bd_str == "32-bit Float")

        # Hide terminal window on Windows
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        try:
            # ----------------------------------------------------------------------------------
            # METHOD A: Python `wave` module wrapper (Guarantees macOS Finder metadata visibility)
            # ----------------------------------------------------------------------------------
            if not is_float:
                sampwidth = 2 if actual_bd_str == "16-bit" else 3
                raw_fmt = "s16le" if sampwidth == 2 else "s24le"
                raw_codec = "pcm_s16le" if sampwidth == 2 else "pcm_s24le"

                # Instruct FFmpeg to output pure raw bytes with no headers to stdout
                cmd = [
                    FFMPEG_BIN, "-v", "error", "-y", "-i", self.input_path,
                    "-map", "0:a:0",           # Audio only (no album art)
                    "-ar", str(actual_sr),     # Resample
                    "-ac", str(channels),      # Mixdown/Up channels
                    "-f", raw_fmt,             # Raw output format
                    "-c:a", raw_codec,         # Codec
                    "-"                        # Pipe out
                ]

                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo)
                
                # Use python's wave module to write the most basic, vanilla header possible
                with wave.open(out_path, 'wb') as wf:
                    wf.setnchannels(channels)
                    wf.setsampwidth(sampwidth)
                    wf.setframerate(actual_sr)
                    
                    # Read the raw stream in chunks and package it into the WAV file
                    while True:
                        chunk = process.stdout.read(8192)
                        if not chunk: break
                        wf.writeframes(chunk)
                
                process.wait()
                if process.returncode != 0:
                    err_msg = process.stderr.read().decode()
                    raise Exception(err_msg)

            # ----------------------------------------------------------------------------------
            # METHOD B: FFmpeg direct write (For 32-bit float only, since `wave` lacks float support)
            # ----------------------------------------------------------------------------------
            else:
                cmd = [
                    FFMPEG_BIN, "-y", "-i", self.input_path,
                    "-map", "0:a:0",
                    "-ar", str(actual_sr),
                    "-c:a", "pcm_f32le",
                    "-map_metadata", "-1",   # Strip metadata completely
                    "-fflags", "+bitexact",  # Strip FFmpeg signature
                    "-write_bext", "0",      # Remove Broadcast chunk
                    "-write_id3v2", "0",     # Remove ID3 tags
                    out_path
                ]
                process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo)
                if process.returncode != 0:
                    raise Exception(process.stderr.decode())

            # Verification
            if os.path.exists(out_path):
                new_data = analyze_file(out_path)
                self.signals.conversion_result.emit(self.row, "Converted", new_data)
            else:
                raise Exception("Output file was not created.")

        except Exception as e:
            if os.path.exists(out_path): os.remove(out_path) # Cleanup broken file
            self.signals.conversion_result.emit(self.row, f"Error: {str(e)}", self.original_data)


def analyze_file(path):
    file_size = os.path.getsize(path) if os.path.exists(path) else 0
    try:
        tag = TinyTag.get(path)
        bitrate = tag.bitrate or 0
        samplerate = tag.samplerate or 0
        channels = tag.channels or 0
        
        bit_depth = 0
        ext = os.path.splitext(path)[1].lower()
        if ext in ['.wav', '.aif', '.aiff', '.flac'] and samplerate > 0 and channels > 0 and bitrate > 0:
            bit_depth = round((bitrate * 1000) / (samplerate * channels))
        
        return {
            "path": path, "name": os.path.basename(path), "size": file_size,
            "duration": tag.duration or 0.0, "sample_rate": samplerate,
            "bitrate": bitrate, "bit_depth": bit_depth, "channels": channels, "status": "OK"
        }
    except Exception as e:
        return {
            "path": path, "name": os.path.basename(path), "size": file_size, "duration": 0, 
            "sample_rate": 0, "bitrate": 0, "bit_depth": 0, "channels": 0, "status": f"Error: {str(e)}"
        }

# -----------------------------
# Custom Dialog for Conversion
# -----------------------------
class ConvertDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Convert Format")
        self.setFixedSize(400, 250)
        self.setStyleSheet("""
            QDialog { background-color: #252525; border: 1px solid #444; color: #ddd;}
            QLabel { color: #e0e0e0; font-size: 13px; font-weight: bold; }
            QComboBox, QLineEdit { background-color: #333; color: white; border: 1px solid #555; padding: 5px; border-radius: 3px; font-size: 12px; }
            QPushButton { background-color: #444; color: white; border: 1px solid #555; padding: 6px 15px; border-radius: 4px; font-size: 12px; font-weight: bold; }
            QPushButton:hover { background-color: #505050; border-color: #666; }
            QPushButton#ConvertBtn { background-color: #58A39C; border: none; }
            QPushButton#ConvertBtn:hover { background-color: #68B3AC; }
        """)
        
        layout = QVBoxLayout(self)
        
        layout.addWidget(QLabel("Target Sample Rate:"))
        self.cb_sr = QComboBox()
        self.cb_sr.addItems(["Original", "44100", "48000", "88200", "96000", "192000"])
        layout.addWidget(self.cb_sr)
        
        layout.addWidget(QLabel("Target Bit Depth:"))
        self.cb_bd = QComboBox()
        self.cb_bd.addItems(["Original", "16-bit", "24-bit", "32-bit Float"])
        layout.addWidget(self.cb_bd)
        
        layout.addWidget(QLabel("Output Folder:"))
        folder_layout = QHBoxLayout()
        self.le_folder = QLineEdit()
        self.le_folder.setReadOnly(True)
        btn_browse = QPushButton("Browse")
        btn_browse.clicked.connect(self.browse_folder)
        folder_layout.addWidget(self.le_folder)
        folder_layout.addWidget(btn_browse)
        layout.addLayout(folder_layout)
        
        layout.addStretch()
        
        btn_layout = QHBoxLayout()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        self.btn_convert = QPushButton("Convert")
        self.btn_convert.setObjectName("ConvertBtn")
        self.btn_convert.clicked.connect(self.accept)
        
        btn_layout.addStretch()
        btn_layout.addWidget(btn_cancel)
        btn_layout.addWidget(self.btn_convert)
        layout.addLayout(btn_layout)

    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if folder:
            self.le_folder.setText(folder)

    def get_settings(self):
        return {
            "sr": self.cb_sr.currentText(),
            "bd": self.cb_bd.currentText(),
            "folder": self.le_folder.text()
        }

# -----------------------------
# Custom Table
# -----------------------------
class ModernTable(QTableWidget):
    files_dropped = Signal(list)
    delete_signal = Signal()

    def __init__(self):
        super().__init__(0, 9) 
        self.setHorizontalHeaderLabels([
            "File Name", "Status", "Duration", "Sample Rate", 
            "Bit Depth", "Bitrate", "Channels", "Size", "File Path"
        ])
        self.verticalHeader().setVisible(False)
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSortingEnabled(True)
        self.setAcceptDrops(True)
        
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True) 
        
        self.setColumnWidth(0, 320) # Name
        self.setColumnWidth(1, 90)  # Status
        self.setColumnWidth(2, 80)  # Duration
        self.setColumnWidth(3, 90)  # Sample Rate
        self.setColumnWidth(4, 80)  # Bit Depth
        self.setColumnWidth(5, 80)  # Bitrate
        self.setColumnWidth(6, 70)  # Channels
        self.setColumnWidth(7, 80)  # Size
        
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.setFixedHeight(30)
        self.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.verticalHeader().setDefaultSectionSize(30)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.rowCount() == 0:
            painter = QPainter(self.viewport())
            painter.save()
            font = painter.font()
            font.setWeight(QFont.DemiBold) 
            font.setPointSize(24) 
            painter.setFont(font)
            painter.setPen(QColor(80, 80, 80))
            painter.drawText(self.viewport().rect(), Qt.AlignCenter, "DRAG & DROP FOLDERS/FILES HERE")
            painter.restore()
            
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls(): event.acceptProposedAction()
        else: event.ignore()
    
    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls(): event.acceptProposedAction()
        else: event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        paths = [url.toLocalFile() for url in urls]
        self.files_dropped.emit(paths)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_signal.emit()
            event.accept()
        else:
            super().keyPressEvent(event)

# -----------------------------
# Main Application Window
# -----------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Audio Metadata Analyzer & Converter")
        self.resize(1100, 700)
        self.setAcceptDrops(True)
        self.setup_dark_theme()
        
        self.threadpool = QThreadPool()
        self.threadpool.setMaxThreadCount(max(1, os.cpu_count() - 1))
        
        self.stop_flag = False
        self.total_size_bytes = 0
        self.total_duration_sec = 0
        self.operations_total = 0
        self.operations_done = 0

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QHBoxLayout(main_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.sidebar_frame = QFrame()
        self.sidebar_frame.setObjectName("Sidebar")
        self.sidebar_frame.setFixedWidth(280)
        sidebar_layout = QVBoxLayout(self.sidebar_frame)
        sidebar_layout.setContentsMargins(15, 20, 15, 20)
        sidebar_layout.setSpacing(10)

        lbl_logo = QLabel()
        lbl_logo.setAlignment(Qt.AlignCenter)
        lbl_logo.setText("""
            <html><head/><body>
            <p align="center" style="margin-bottom:0px; margin-top:0px; line-height:1.0;">
                <span style="font-size:28px; font-weight:600; color:#5ba49d;">AUDIO DATA</span><br/>
                <span style="font-size:16px; font-weight:600; color:#808080;">ANALYZER & CONVERTER</span>
            </p>
            </body></html>
        """)
        sidebar_layout.addWidget(lbl_logo)
        sidebar_layout.addSpacing(20)

        sidebar_layout.addWidget(self.create_header_label("INPUT & ACTIONS"))
        
        btn_select = QPushButton("Select Files / Folder")
        btn_select.setCursor(Qt.PointingHandCursor)
        btn_select.clicked.connect(self.select_input)
        sidebar_layout.addWidget(btn_select)
        
        btn_clear = QPushButton("Clear List")
        btn_clear.setCursor(Qt.PointingHandCursor)
        btn_clear.clicked.connect(self.clear_table)
        sidebar_layout.addWidget(btn_clear)
        
        self.btn_convert = QPushButton("Convert Selected")
        self.btn_convert.setObjectName("ActionBtn")
        self.btn_convert.setCursor(Qt.PointingHandCursor)
        self.btn_convert.clicked.connect(self.open_convert_dialog)
        self.btn_convert.setEnabled(False)
        sidebar_layout.addWidget(self.btn_convert)

        self.btn_export = QPushButton("Export to CSV")
        self.btn_export.setCursor(Qt.PointingHandCursor)
        self.btn_export.clicked.connect(self.export_csv)
        self.btn_export.setEnabled(False)
        sidebar_layout.addWidget(self.btn_export)

        sidebar_layout.addSpacing(10)

        sidebar_layout.addWidget(self.create_header_label("STATISTICS"))
        self.stats_frame = QFrame()
        self.stats_frame.setObjectName("StatsFrame")
        stats_layout = QVBoxLayout(self.stats_frame)
        
        self.lbl_stat_files = QLabel("Total Files: 0")
        self.lbl_stat_size = QLabel("Total Size: 0 MB")
        self.lbl_stat_dur = QLabel("Total Duration: 00:00")
        
        stats_layout.addWidget(self.lbl_stat_files)
        stats_layout.addWidget(self.lbl_stat_size)
        stats_layout.addWidget(self.lbl_stat_dur)
        sidebar_layout.addWidget(self.stats_frame)
        
        sidebar_layout.addStretch()

        self.lbl_status = QLabel("Ready")
        self.lbl_status.setAlignment(Qt.AlignCenter)
        self.lbl_status.setStyleSheet("color: #888;") 
        sidebar_layout.addWidget(self.lbl_status)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(5)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        sidebar_layout.addWidget(self.progress_bar)

        table_frame = QFrame()
        table_layout = QVBoxLayout(table_frame)
        table_layout.setContentsMargins(0, 0, 0, 0)
        
        self.table = ModernTable()
        self.table.files_dropped.connect(self.start_file_discovery)
        self.table.delete_signal.connect(self.delete_selected_files)
        self.table.itemSelectionChanged.connect(self.on_selection_changed)
        
        table_layout.addWidget(self.table)
        
        main_layout.addWidget(self.sidebar_frame)
        main_layout.addWidget(table_frame)

    def create_header_label(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet("color: #DDD; font-weight: 600; font-size: 16px; margin-top: 5px;")
        return lbl

    def setup_dark_theme(self):
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(30, 30, 30))
        palette.setColor(QPalette.WindowText, QColor(220, 220, 220))
        palette.setColor(QPalette.Base, QColor(25, 25, 25))
        palette.setColor(QPalette.AlternateBase, QColor(35, 35, 35))
        palette.setColor(QPalette.Text, QColor(220, 220, 220))
        palette.setColor(QPalette.Button, QColor(45, 45, 45))
        palette.setColor(QPalette.ButtonText, QColor(220, 220, 220))
        palette.setColor(QPalette.Highlight, QColor(88, 163, 156))
        palette.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
        self.setPalette(palette)
        
        self.setStyleSheet("""
            QMainWindow { background-color: #1E1E1E; }
            QFrame#Sidebar { background-color: #252525; border-right: 1px solid #333; }
            QPushButton { background-color: #333; border: 1px solid #444; border-radius: 4px; padding: 6px; color: #ddd; }
            QPushButton:hover { background-color: #3E3E3E; border-color: #555; }
            QPushButton:pressed { background-color: #222; }
            QPushButton:disabled { background-color: #2a2a2a; color: #555; border-color: #333; }
            QPushButton#ActionBtn { background-color: #58A39C; color: white; border: none; font-weight: bold; font-size: 14px;}
            QPushButton#ActionBtn:hover { background-color: #68B3AC; }
            QPushButton#ActionBtn:disabled { background-color: #333; color: #555; }
            QTableWidget { background-color: #202020; alternate-background-color: #2A2A2A; color: #E0E0E0; border: none; outline: 0; }
            QTableWidget::item:selected { background-color: #3D605D; color: white; }
            QHeaderView::section { background-color: #252525; color: #aaa; border: none; padding: 5px; font-weight: bold; font-size: 12px; border-right: 1px solid #2e2e2e; }
            QFrame#StatsFrame { background-color: #2A2A2A; border-radius: 5px; padding: 10px; }
            QFrame#StatsFrame QLabel { color: #BBBBBB; font-size: 13px; margin: 2px 0px; }
            QProgressBar { border: 1px solid #444; background-color: #222; }
            QProgressBar::chunk { background-color: #58A39C; }
        """)

    def select_input(self):
        dialog = QFileDialog(self, "Select audio files")
        dialog.setFileMode(QFileDialog.ExistingFiles)
        if dialog.exec():
            paths = dialog.selectedFiles()
            self.start_file_discovery(paths)

    def start_file_discovery(self, paths):
        if not paths: return
        self.lbl_status.setText("Scanning directories...")
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        
        worker = FileDiscoveryWorker(paths)
        worker.signals.discovery_finished.connect(self.on_discovery_finished)
        self.threadpool.start(worker)

    def on_discovery_finished(self, found_files):
        if not found_files:
            self.lbl_status.setText("No compatible audio files found.")
            self.progress_bar.setVisible(False)
            return

        start_row = self.table.rowCount()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(start_row + len(found_files))
        
        for i, path in enumerate(found_files):
            row = start_row + i
            name = os.path.basename(path)
            self.table.setItem(row, 0, QTableWidgetItem(name))
            self.table.setItem(row, 1, QTableWidgetItem("Pending..."))
            self.table.setItem(row, 2, NumberSortItem("", 0))
            self.table.setItem(row, 3, NumberSortItem("", 0))
            self.table.setItem(row, 4, NumberSortItem("", 0)) 
            self.table.setItem(row, 5, NumberSortItem("", 0)) 
            self.table.setItem(row, 6, NumberSortItem("", 0))
            self.table.setItem(row, 7, NumberSortItem("", 0))
            self.table.setItem(row, 8, QTableWidgetItem(path))
            
        self.operations_total = len(found_files)
        self.operations_done = 0
        self.progress_bar.setRange(0, self.operations_total)
        self.progress_bar.setValue(0)
        self.lbl_status.setText(f"Analyzing {self.operations_total} files...")

        worker = AnalysisWorker(start_row, found_files, lambda: self.stop_flag)
        worker.signals.analysis_result.connect(self.update_row_data)
        worker.signals.analysis_finished.connect(self.on_analysis_finished)
        self.threadpool.start(worker)

    def update_row_data(self, row, data, status_text=None):
        status_str = status_text if status_text else data["status"]
        status_item = QTableWidgetItem(status_str)
        
        if "Error" in status_str: status_item.setForeground(QColor("#FF6B6B"))
        elif status_str == "Converted": status_item.setForeground(QColor("#58A39C"))
        
        self.table.setItem(row, 1, status_item)
        self.table.setItem(row, 2, NumberSortItem(format_duration(data["duration"]), data["duration"]))
        
        sr_text = f"{data['sample_rate']} Hz" if data['sample_rate'] else "-"
        self.table.setItem(row, 3, NumberSortItem(sr_text, data["sample_rate"]))
        
        bd_text = f"{data['bit_depth']}-bit" if data['bit_depth'] else "-"
        self.table.setItem(row, 4, NumberSortItem(bd_text, data["bit_depth"]))
        
        br_text = f"{int(data['bitrate'])} kbps" if data['bitrate'] else "-"
        self.table.setItem(row, 5, NumberSortItem(br_text, data["bitrate"]))
        
        ch_text = str(data['channels']) if data['channels'] else "-"
        self.table.setItem(row, 6, NumberSortItem(ch_text, data["channels"]))
        
        self.table.setItem(row, 7, NumberSortItem(format_size(data["size"]), data["size"]))
        self.table.setItem(row, 8, QTableWidgetItem(data["path"]))
        
        if status_str != "Converted":
            self.total_size_bytes += data["size"]
            self.total_duration_sec += data["duration"]
            self.operations_done += 1
            self.progress_bar.setValue(self.operations_done)
            self.update_stats_ui()

    def on_analysis_finished(self):
        self.lbl_status.setText("Ready")
        self.progress_bar.setVisible(False)
        self.table.setSortingEnabled(True)
        if self.table.rowCount() > 0:
            self.btn_export.setEnabled(True)

    def on_selection_changed(self):
        has_selection = len(self.table.selectionModel().selectedRows()) > 0
        self.btn_convert.setEnabled(has_selection)

    def open_convert_dialog(self):
        try:
            subprocess.run([FFMPEG_BIN, "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception:
            QMessageBox.critical(self, "FFmpeg Missing", 
                "FFmpeg is required for format conversion.\n\n"
                "Please install FFmpeg and ensure it is added to your system PATH."
            )
            return

        rows = [i.row() for i in self.table.selectionModel().selectedRows()]
        if not rows: return

        dialog = ConvertDialog(self)
        if dialog.exec():
            settings = dialog.get_settings()
            if not settings["folder"]:
                QMessageBox.warning(self, "Missing Output", "Please select an output folder.")
                return
            self.start_conversion(rows, settings)

    def start_conversion(self, rows, settings):
        self.table.setSortingEnabled(False)
        self.operations_total = len(rows)
        self.operations_done = 0
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, self.operations_total)
        self.progress_bar.setValue(0)
        self.lbl_status.setText(f"Converting 0 / {self.operations_total}")

        for row in rows:
            input_path = self.table.item(row, 8).text()
            
            # Fetch original info to pass to the Worker
            sr_text = self.table.item(row, 3).text().replace(" Hz", "")
            orig_sr = int(sr_text) if sr_text.isdigit() else 44100
            
            bd_text = self.table.item(row, 4).text().replace("-bit", "")
            orig_bd = int(bd_text) if bd_text.isdigit() else 24
            
            ch_text = self.table.item(row, 6).text()
            orig_ch = int(ch_text) if ch_text.isdigit() else 2
            
            original_data = {
                "sample_rate": orig_sr,
                "bit_depth": orig_bd,
                "channels": orig_ch
            }

            self.table.setItem(row, 1, QTableWidgetItem("Converting..."))
            
            worker = ConversionWorker(
                row=row,
                input_path=input_path,
                target_sr=settings["sr"],
                target_bd=settings["bd"],
                original_data=original_data,
                out_dir=settings["folder"],
                stop_func=lambda: self.stop_flag
            )
            worker.signals.conversion_result.connect(self.on_conversion_result)
            self.threadpool.start(worker)

    def on_conversion_result(self, row, status, data):
        try:
            old_size = self.table.item(row, 7).value
            self.total_size_bytes -= old_size
            self.total_size_bytes += data["size"]
        except: pass

        self.update_row_data(row, data, status_text=status)
        self.update_stats_ui()

        self.operations_done += 1
        self.progress_bar.setValue(self.operations_done)
        self.lbl_status.setText(f"Converting {self.operations_done} / {self.operations_total}")

        if self.operations_done >= self.operations_total:
            self.lbl_status.setText("Conversion Complete")
            self.progress_bar.setVisible(False)
            self.table.setSortingEnabled(True)

    def update_stats_ui(self):
        self.lbl_stat_files.setText(f"Total Files: {self.table.rowCount()}")
        self.lbl_stat_size.setText(f"Total Size: {format_size(self.total_size_bytes)}")
        self.lbl_stat_dur.setText(f"Total Duration: {format_duration(self.total_duration_sec)}")

    def clear_table(self):
        self.stop_flag = True 
        self.threadpool.clear()
        self.table.setRowCount(0)
        self.total_size_bytes = 0
        self.total_duration_sec = 0
        self.update_stats_ui()
        self.btn_export.setEnabled(False)
        self.btn_convert.setEnabled(False)
        self.stop_flag = False

    def delete_selected_files(self):
        rows = sorted([i.row() for i in self.table.selectionModel().selectedRows()], reverse=True)
        for r in rows:
            try:
                self.total_duration_sec -= self.table.item(r, 2).value
                self.total_size_bytes -= self.table.item(r, 7).value
            except: pass
            self.table.removeRow(r)
            
        self.update_stats_ui()
        if self.table.rowCount() == 0:
            self.btn_export.setEnabled(False)
            self.btn_convert.setEnabled(False)

    def export_csv(self):
        if self.table.rowCount() == 0: return
        path, _ = QFileDialog.getSaveFileName(self, "Export to CSV", "", "CSV Files (*.csv)")
        if not path: return
        
        try:
            with open(path, mode='w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    "File Name", "Status", "Duration", "Sample Rate (Hz)", 
                    "Bit Depth", "Bitrate (kbps)", "Channels", "Size", "File Path"
                ])
                
                for row in range(self.table.rowCount()):
                    row_data = []
                    for col in range(self.table.columnCount()):
                        item = self.table.item(row, col)
                        row_data.append(item.text() if item else "")
                    writer.writerow(row_data)
                    
            QMessageBox.information(self, "Success", f"Data exported successfully to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to export CSV:\n{str(e)}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())