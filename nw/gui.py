"""Qt (PySide6) interface."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QFontDatabase, QIcon, QPainter, QPalette, QPen,
                           QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox,
                               QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLayout,
                               QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QRadioButton, QSlider,
                               QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
                               QToolButton, QVBoxLayout, QWidget)

from . import __version__, gpu, system
from .detector import FileResult, Settings
from .export import clock_str
from .media import (MediaError, VIDEO_EXTS, clock_from_filename, fmt_hms, grab_frame,
                    probe, require_tools)
from .pipeline import Callbacks, default_jobs, expand_inputs, run

CONFIG_DIR = system.config_dir()
CONFIG_FILE = CONFIG_DIR / "settings.json"


# --------------------------------------------------------------------------- signals bridge


class Bridge(QObject):
    file_progress = Signal(int, float)
    file_done = Signal(int, object)
    stage = Signal(str)
    log = Signal(str)
    compile_progress = Signal(float)
    finished = Signal(object)
    failed = Signal(str)
    probed = Signal(str, object)


# --------------------------------------------------------------------------- ignore regions


class RegionCanvas(QWidget):
    changed = Signal()

    def __init__(self, pixmap: QPixmap | None, regions: list, parent=None):
        super().__init__(parent)
        self.pix = pixmap
        self.regions = [list(r) for r in regions]
        self._drag_start: QPointF | None = None
        self._drag_now: QPointF | None = None
        self.setMinimumSize(640, 360)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _img_rect(self) -> QRectF:
        if self.pix is None or self.pix.isNull():
            return QRectF(0, 0, self.width(), self.height())
        pw, ph = self.pix.width(), self.pix.height()
        s = min(self.width() / pw, self.height() / ph)
        w, h = pw * s, ph * s
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def _norm(self, p: QPointF) -> tuple[float, float]:
        r = self._img_rect()
        return (min(1, max(0, (p.x() - r.x()) / r.width())), min(1, max(0, (p.y() - r.y()) / r.height())))

    def _to_widget(self, reg) -> QRectF:
        r = self._img_rect()
        x, y, w, h = reg
        return QRectF(r.x() + x * r.width(), r.y() + y * r.height(), w * r.width(), h * r.height())

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(10, 11, 13))
        r = self._img_rect()
        if self.pix is not None and not self.pix.isNull():
            p.drawPixmap(r.toRect(), self.pix)
        else:
            p.setPen(QColor(160, 160, 160))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "No preview frame (add a file to the queue first).\nYou can still draw regions.")
        p.setPen(QPen(QColor(255, 80, 80), 2))
        p.setBrush(QColor(255, 60, 60, 70))
        for reg in self.regions:
            p.drawRect(self._to_widget(reg))
        if self._drag_start is not None and self._drag_now is not None:
            p.setPen(QPen(QColor(255, 200, 80), 2, Qt.PenStyle.DashLine))
            p.setBrush(QColor(255, 200, 80, 50))
            p.drawRect(QRectF(self._drag_start, self._drag_now).normalized())
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_start = e.position()
            self._drag_now = e.position()
        elif e.button() == Qt.MouseButton.RightButton:
            for i in range(len(self.regions) - 1, -1, -1):
                if self._to_widget(self.regions[i]).contains(e.position()):
                    del self.regions[i]
                    self.changed.emit()
                    break
        self.update()

    def mouseMoveEvent(self, e):
        if self._drag_start is not None:
            self._drag_now = e.position()
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._drag_start is not None:
            x0, y0 = self._norm(self._drag_start)
            x1, y1 = self._norm(e.position())
            x, y, w, h = min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)
            if w > 0.005 and h > 0.005:
                self.regions.append([round(x, 4), round(y, 4), round(w, 4), round(h, 4)])
                self.changed.emit()
            self._drag_start = self._drag_now = None
            self.update()


class RegionDialog(QDialog):
    def __init__(self, parent, frame_png: bytes | None, regions: list, source: str | None):
        super().__init__(parent)
        self.setWindowTitle("Ignore regions")
        pix = None
        if frame_png:
            pix = QPixmap()
            pix.loadFromData(frame_png, "PNG")
        self.canvas = RegionCanvas(pix, regions, self)
        lay = QVBoxLayout(self)
        tip = QLabel("Drag to mark areas to ignore: the on-screen clock, a tree that moves in the wind, "
                     "a neighbour's flickering light. Right-click a region to remove it. "
                     "Regions are stored as fractions of the frame, so they apply to every file "
                     "from this camera.")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        if source:
            src = QLabel(f"Preview: {Path(source).name} (brightened)")
            src.setStyleSheet("color:#8b9098")
            lay.addWidget(src)
        lay.addWidget(self.canvas, 1)
        row = QHBoxLayout()
        clear = QPushButton("Clear all")
        clear.clicked.connect(self._clear)
        row.addWidget(clear)
        self.count = QLabel()
        row.addWidget(self.count)
        row.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)
        lay.addLayout(row)
        self.canvas.changed.connect(self._upd)
        self._upd()
        self.resize(1000, 640)

    def _clear(self):
        self.canvas.regions.clear()
        self.canvas.update()
        self._upd()

    def _upd(self):
        n = len(self.canvas.regions)
        self.count.setText(f"{n} region{'s' if n != 1 else ''}")

    def regions(self) -> list:
        return self.canvas.regions


# --------------------------------------------------------------------------- platform / GPU setup


class _Done(QObject):
    done = Signal(object)


class SetupDialog(QDialog):
    """Shown at startup: pick the operating system, graphics card and (optionally) ffmpeg folder."""

    def __init__(self, parent, S: Settings):
        super().__init__(parent)
        self.setWindowTitle("Nightwatch setup")
        self.S = S
        lay = QVBoxLayout(self)
        lay.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        intro = QLabel("Choose the system you're on and the graphics card to use. The GPU speeds up "
                       "decoding during scans and encoding of the compilation. If it can't be used for "
                       "a file, Nightwatch quietly falls back to the CPU, so results never depend on it.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        form = QFormLayout()
        det = system.detected_platform()
        osrow = QHBoxLayout()
        self.os_win = QRadioButton("Windows" + ("  (detected)" if det == "windows" else ""))
        self.os_lin = QRadioButton("Linux" + ("  (detected)" if det == "linux" else ""))
        grp = QButtonGroup(self)
        grp.addButton(self.os_win)
        grp.addButton(self.os_lin)
        (self.os_win if (S.platform or det) == "windows" else self.os_lin).setChecked(True)
        osrow.addWidget(self.os_win)
        osrow.addWidget(self.os_lin)
        osrow.addStretch(1)
        form.addRow("Operating system", osrow)
        self.os_warn = QLabel()
        self.os_warn.setObjectName("warn")
        self.os_warn.setWordWrap(True)
        form.addRow("", self.os_warn)
        self.gpu_box = QComboBox()
        for v in gpu.VENDORS:
            self.gpu_box.addItem(gpu.VENDOR_LABELS[v], v)
        self.gpu_box.setCurrentIndex(max(0, list(gpu.VENDORS).index(S.gpu) if S.gpu in gpu.VENDORS else 0))
        form.addRow("Graphics card", self.gpu_box)
        frow = QHBoxLayout()
        self.ffdir = QLineEdit(S.ffmpeg_dir)
        self.ffdir.setPlaceholderText("found automatically (PATH, winget, scoop, …)")
        b = QPushButton("Browse…")
        b.clicked.connect(self._browse)
        frow.addWidget(self.ffdir, 1)
        frow.addWidget(b)
        form.addRow("ffmpeg folder", frow)
        self.ff_status = QLabel()
        self.ff_status.setObjectName("dim")
        self.ff_status.setWordWrap(True)
        self.ff_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("", self.ff_status)
        lay.addLayout(form)

        trow = QHBoxLayout()
        self.btn_test = QPushButton("Test graphics card")
        self.btn_test.clicked.connect(self._test)
        self.test_status = QLabel()
        self.test_status.setObjectName("dim")
        trow.addWidget(self.btn_test)
        trow.addWidget(self.test_status, 1)
        lay.addLayout(trow)
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.report.setMinimumHeight(190)
        self.report.setVisible(False)
        lay.addWidget(self.report, 1)

        self.show_again = QCheckBox("Show this window every time Nightwatch starts")
        self.show_again.setChecked(S.show_setup)
        lay.addWidget(self.show_again)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self._sig = _Done()
        self._sig.done.connect(self._test_done)
        self.os_win.toggled.connect(self._update)
        self.ffdir.editingFinished.connect(self._update)
        self.gpu_box.currentIndexChanged.connect(lambda *_: self.report.setVisible(False))
        self.resize(640, 360)
        self._update()

    def platform(self) -> str:
        return "windows" if self.os_win.isChecked() else "linux"

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Folder containing ffmpeg", self.ffdir.text() or str(Path.home()))
        if d:
            self.ffdir.setText(d)
            self._update()

    def _update(self, *_):
        warns = system.configure(self.platform(), self.ffdir.text())
        self.os_warn.setText(warns[0] if warns else "")
        self.os_warn.setVisible(bool(warns))
        path, ver = system.tool("ffmpeg"), system.ffmpeg_version()
        if path and ver:
            self.ff_status.setText(f"ffmpeg: {path}\n{ver.split(' Copyright')[0]}")
        else:
            self.ff_status.setText("ffmpeg was not found. " + system.install_hint())
        self.report.setVisible(False)

    def _test(self):
        self._update()
        vendor = self.gpu_box.currentData()
        self.btn_test.setEnabled(False)
        self.test_status.setText("Testing… (a few seconds)")
        threading.Thread(target=lambda: self._sig.done.emit(gpu.check(vendor)), daemon=True).start()

    def _test_done(self, rep):
        self.btn_test.setEnabled(True)
        self.report.setPlainText("\n".join(rep.lines()))
        self.report.setVisible(True)
        self.layout().activate()
        self.resize(max(self.width(), 680), max(self.height(), self.sizeHint().height() + 40))
        gpu_dec = rep.chosen_decoder not in ("", "CPU")
        gpu_enc = not rep.chosen_encoder.startswith("CPU")
        if gpu_dec or gpu_enc:
            self.test_status.setText("GPU will be used for: " + " and ".join(
                [x for x, on in (("decoding", gpu_dec), ("encoding", gpu_enc)) if on]))
        else:
            self.test_status.setText("No usable GPU path; everything will run on the CPU.")

    def apply(self):
        self.S.platform = self.platform()
        self.S.gpu = self.gpu_box.currentData()
        self.S.ffmpeg_dir = self.ffdir.text().strip()
        self.S.show_setup = self.show_again.isChecked()


# --------------------------------------------------------------------------- main window

Q_FILE, Q_START, Q_LEN, Q_RES, Q_STATUS, Q_EVENTS = range(6)
E_THUMB, E_WHEN, E_FILE, E_POS, E_LEN, E_KIND = range(6)

SENS_TEXT = {1: "only obvious things", 2: "only obvious things", 3: "low", 4: "balanced, conservative",
             5: "balanced", 6: "balanced", 7: "balanced, eager", 8: "catches faint movement",
             9: "catches faint movement (more false alarms)", 10: "maximum (expect false alarms)"}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Nightwatch {__version__}: CCTV event finder")
        self.setAcceptDrops(True)
        self.bridge = Bridge()
        self.files: list[str] = []
        self.infos: dict[str, object] = {}
        self.results: dict[int, FileResult] = {}
        self.event_rows: list = []
        self.cancel_ev: threading.Event | None = None
        self.worker: threading.Thread | None = None
        self.last_out = None
        self.probe_pool = ThreadPoolExecutor(max_workers=4)
        self.settings = self._load_settings()

        self.bridge.file_progress.connect(self._on_file_progress)
        self.bridge.file_done.connect(self._on_file_done)
        self.bridge.stage.connect(lambda s: self.status.setText(s))
        self.bridge.log.connect(self._log)
        self.bridge.compile_progress.connect(self._on_compile_progress)
        self.bridge.finished.connect(self._on_finished)
        self.bridge.failed.connect(self._on_failed)
        self.bridge.probed.connect(self._on_probed)

        self._build_ui()
        self._apply_settings_to_ui()
        system.configure(self.settings.platform or None, self.settings.ffmpeg_dir)
        self._update_system_label()
        self.resize(1180, 900)
        if self.settings.show_setup or not self.settings.platform:
            QTimer.singleShot(0, self._open_setup)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        # ---------------- left: settings + run controls
        left = QWidget()
        left.setFixedWidth(430)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self._build_system_box())
        ll.addWidget(self._build_detection_box())
        ll.addWidget(self._build_output_box())
        ll.addStretch(1)
        runbar = QHBoxLayout()
        self.btn_start = QPushButton("Start")
        self.btn_start.setObjectName("start")
        self.btn_start.clicked.connect(self._start)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel)
        runbar.addWidget(self.btn_start, 2)
        runbar.addWidget(self.btn_cancel, 1)
        ll.addLayout(runbar)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        ll.addWidget(self.progress)
        self.status = QLabel("Ready")
        self.status.setWordWrap(True)
        ll.addWidget(self.status)
        root.addWidget(left)

        # ---------------- right: queue + results
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.btn_add = QPushButton("Add files…")
        self.btn_add_dir = QPushButton("Add folder…")
        self.btn_remove = QPushButton("Remove")
        self.btn_clear = QPushButton("Clear")
        self.btn_add.clicked.connect(self._add_files_dialog)
        self.btn_add_dir.clicked.connect(self._add_folder_dialog)
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_clear.clicked.connect(self._clear_queue)
        for b in (self.btn_add, self.btn_add_dir, self.btn_remove, self.btn_clear):
            bar.addWidget(b)
        bar.addStretch(1)
        self.queue_info = QLabel("Drop .ts files or folders here")
        self.queue_info.setObjectName("dim")
        bar.addWidget(self.queue_info)
        rl.addLayout(bar)

        split = QSplitter(Qt.Orientation.Vertical)
        self.queue = QTableWidget(0, 6)
        self.queue.setHorizontalHeaderLabels(["File", "Starts", "Length", "Resolution", "Status", "Events"])
        self.queue.verticalHeader().setVisible(False)
        self.queue.verticalHeader().setDefaultSectionSize(26)
        self.queue.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.queue.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hh = self.queue.horizontalHeader()
        hh.setSectionResizeMode(Q_FILE, QHeaderView.ResizeMode.Stretch)
        for c, w in ((Q_START, 150), (Q_LEN, 72), (Q_RES, 90), (Q_STATUS, 150), (Q_EVENTS, 56)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
            self.queue.setColumnWidth(c, w)
        split.addWidget(self.queue)

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.events = QTableWidget(0, 6)
        self.events.setHorizontalHeaderLabels(["", "When", "File", "Position", "Length", "Kind"])
        self.events.verticalHeader().setVisible(False)
        self.events.setIconSize(QSize(160, 90))
        self.events.verticalHeader().setDefaultSectionSize(96)
        self.events.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.events.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        eh = self.events.horizontalHeader()
        self.events.setColumnWidth(E_THUMB, 168)
        eh.setSectionResizeMode(E_FILE, QHeaderView.ResizeMode.Stretch)
        for c, w in ((E_WHEN, 150), (E_POS, 84), (E_LEN, 66), (E_KIND, 150)):
            self.events.setColumnWidth(c, w)
        self.events.cellDoubleClicked.connect(lambda r, c: self._play_event(r, in_compilation=False))
        self.events.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.events.customContextMenuRequested.connect(self._event_menu)
        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.tabs.addTab(self.events, "Events")
        self.tabs.addTab(self.logbox, "Log")
        bl.addWidget(self.tabs, 1)
        rb = QHBoxLayout()
        hint = QLabel("Double-click an event to play it from the source file. Right-click for more.")
        hint.setObjectName("dim")
        rb.addWidget(hint)
        rb.addStretch(1)
        self.btn_folder = QPushButton("Open folder")
        self.btn_comp = QPushButton("Play compilation")
        self.btn_report = QPushButton("Open report")
        self.btn_folder.clicked.connect(self._open_folder)
        self.btn_comp.clicked.connect(self._play_compilation)
        self.btn_report.clicked.connect(self._open_report)
        for b in (self.btn_folder, self.btn_comp, self.btn_report):
            b.setEnabled(False)
            rb.addWidget(b)
        bl.addLayout(rb)
        split.addWidget(bottom)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        split.setSizes([200, 600])
        rl.addWidget(split, 1)
        root.addWidget(right, 1)
        self.setCentralWidget(central)

    def _build_system_box(self) -> QGroupBox:
        box = QGroupBox("System")
        row = QHBoxLayout(box)
        self.sys_lbl = QLabel()
        self.sys_lbl.setWordWrap(True)
        b = QPushButton("Change…")
        b.clicked.connect(lambda: self._open_setup())
        row.addWidget(self.sys_lbl, 1)
        row.addWidget(b)
        return box

    def _update_system_label(self):
        S = self.settings
        ok = system.tool("ffmpeg") is not None
        plat = (S.platform or system.detected_platform()).title()
        txt = f"{plat}  ·  GPU: {gpu.VENDOR_LABELS.get(S.gpu, S.gpu)}  ·  ffmpeg " + ("found" if ok else "MISSING")
        if S.platform and S.platform != system.detected_platform():
            txt += "\n(this computer looks like " + system.detected_platform().title() + ")"
        self.sys_lbl.setText(txt)
        self.sys_lbl.setStyleSheet("" if ok else "color:#ff8a80")

    def _open_setup(self):
        dlg = SetupDialog(self, self.settings)
        if dlg.exec():
            dlg.apply()
        elif not self.settings.platform:
            self.settings.platform = system.detected_platform()
        for w in system.configure(self.settings.platform, self.settings.ffmpeg_dir):
            self._log("WARNING: " + w)
        self._save_settings()
        self._update_system_label()

    def _build_detection_box(self) -> QGroupBox:
        box = QGroupBox("Detection")
        form = QFormLayout(box)
        srow = QVBoxLayout()
        srow.setSpacing(2)
        self.sens = QSlider(Qt.Orientation.Horizontal)
        self.sens.setRange(1, 10)
        self.sens.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.sens_lbl = QLabel()
        self.sens_lbl.setObjectName("dim")
        self.sens.valueChanged.connect(lambda v: self.sens_lbl.setText(f"{v}: {SENS_TEXT[v]}"))
        srow.addWidget(self.sens)
        srow.addWidget(self.sens_lbl)
        form.addRow("Sensitivity", srow)
        self.min_size = QDoubleSpinBox()
        self.min_size.setRange(0.01, 10.0)
        self.min_size.setDecimals(2)
        self.min_size.setSingleStep(0.02)
        self.min_size.setSuffix(" % of frame")
        self.min_size.setToolTip("Smallest blob that counts. Raise it if insects or rain trigger events.")
        form.addRow("Min object size", self.min_size)
        rrow = QHBoxLayout()
        self.btn_regions = QPushButton("Draw ignore regions…")
        self.btn_regions.clicked.connect(self._edit_regions)
        self.regions_lbl = QLabel()
        self.regions_lbl.setObjectName("dim")
        rrow.addWidget(self.btn_regions)
        rrow.addWidget(self.regions_lbl)
        rrow.addStretch(1)
        form.addRow("Ignore", rrow)
        self.dark_boost = QCheckBox("Dark boost (lift shadows before detecting)")
        self.auto_sup = QCheckBox("Auto-ignore constantly changing areas")
        self.auto_sup.setToolTip("Learns and ignores pixels that change most of the time: "
                                 "the on-screen clock, blinking LEDs, branches in wind, flicker.")
        self.night_only = QCheckBox("Night-time only (skip bright scenes)")
        for w in (self.dark_boost, self.auto_sup, self.night_only):
            form.addRow("", w)

        self.adv_btn = QToolButton()
        self.adv_btn.setText("Advanced ▸")
        self.adv_btn.setCheckable(True)
        self.adv_btn.setObjectName("link")
        form.addRow("", self.adv_btn)
        self.adv = QWidget()
        g = QGridLayout(self.adv)
        g.setContentsMargins(0, 0, 0, 0)
        self.afps = QDoubleSpinBox(); self.afps.setRange(1, 15); self.afps.setDecimals(1); self.afps.setSuffix(" fps")
        self.awidth = QSpinBox(); self.awidth.setRange(160, 960); self.awidth.setSingleStep(32); self.awidth.setSuffix(" px")
        self.min_ev = QDoubleSpinBox(); self.min_ev.setRange(0.2, 30); self.min_ev.setSuffix(" s")
        self.gap = QDoubleSpinBox(); self.gap.setRange(0, 120); self.gap.setSuffix(" s")
        self.jobs = QSpinBox(); self.jobs.setRange(1, 32)
        self.clock = QComboBox(); self.clock.addItems(["auto", "filename", "mtime", "none"])
        self.clock.setToolTip("Where the real time comes from: the file name (MediaMTX / most NVRs), "
                              "the file's modified time minus its length, or nothing.")
        self.debug = QCheckBox("Write debug videos")
        self.debug.setToolTip("Writes a side-by-side video per file: picture | detection mask | "
                              "learnt 'always changing' map. Useful for tuning.")
        items = [("Analysis rate", self.afps), ("Analysis width", self.awidth),
                 ("Min event length", self.min_ev), ("Merge events within", self.gap),
                 ("Files in parallel", self.jobs), ("Clock from", self.clock)]
        for i, (lbl, w) in enumerate(items):
            g.addWidget(QLabel(lbl), i, 0)
            g.addWidget(w, i, 1)
        g.addWidget(self.debug, len(items), 0, 1, 2)
        self.adv.setVisible(False)
        self.adv_btn.toggled.connect(lambda on: (self.adv.setVisible(on),
                                                 self.adv_btn.setText("Advanced ▾" if on else "Advanced ▸")))
        form.addRow(self.adv)
        return box

    def _build_output_box(self) -> QGroupBox:
        box = QGroupBox("Output")
        form = QFormLayout(box)
        orow = QHBoxLayout()
        self.outdir = QLineEdit()
        b = QPushButton("Browse…")
        b.clicked.connect(self._pick_outdir)
        orow.addWidget(self.outdir, 1)
        orow.addWidget(b)
        form.addRow("Save to", orow)
        self.mode_ts = QRadioButton("Timestamps + thumbnails report")
        self.mode_comp = QRadioButton("…plus one compilation video of all events")
        grp = QButtonGroup(self)
        grp.addButton(self.mode_ts)
        grp.addButton(self.mode_comp)
        form.addRow("Produce", self.mode_ts)
        form.addRow("", self.mode_comp)
        self.comp_opts = QFrame()
        cg = QGridLayout(self.comp_opts)
        cg.setContentsMargins(0, 0, 0, 0)
        self.pre = QDoubleSpinBox(); self.pre.setRange(0, 60); self.pre.setSuffix(" s before")
        self.post = QDoubleSpinBox(); self.post.setRange(0, 60); self.post.setSuffix(" s after")
        self.overlay = QCheckBox("Burn in name + clock")
        self.brighten = QCheckBox("Brighten")
        self.fast = QCheckBox("Fast (no re-encode)")
        self.fast.setToolTip("Stream-copies clips. Much faster, but cuts snap to keyframes and the "
                             "overlay/brighten options are skipped. Needs all files to share codec/resolution.")
        self.keep = QCheckBox("Keep single clips")
        cg.addWidget(QLabel("Padding"), 0, 0)
        cg.addWidget(self.pre, 0, 1)
        cg.addWidget(self.post, 0, 2)
        cg.addWidget(self.overlay, 1, 0, 1, 2)
        cg.addWidget(self.brighten, 1, 2)
        cg.addWidget(self.fast, 2, 0, 1, 2)
        cg.addWidget(self.keep, 2, 2)
        form.addRow(self.comp_opts)
        self.mode_comp.toggled.connect(self._sync_mode)
        self.fast.toggled.connect(self._sync_mode)
        return box

    def _sync_mode(self, *_):
        on = self.mode_comp.isChecked()
        self.comp_opts.setEnabled(on)
        self.overlay.setEnabled(on and not self.fast.isChecked())
        self.brighten.setEnabled(on and not self.fast.isChecked())

    # ------------------------------------------------------------------ settings
    def _load_settings(self) -> Settings:
        S = Settings(jobs=default_jobs())
        self._outdir_saved = str(system.default_output_dir())
        try:
            d = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            self._outdir_saved = d.pop("_outdir", self._outdir_saved)
            S = Settings.from_dict(d)
        except (OSError, ValueError):
            pass
        return S

    def _save_settings(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            d = self.settings.to_dict()
            d["_outdir"] = self.outdir.text()
            CONFIG_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _apply_settings_to_ui(self):
        S = self.settings
        self.sens.setValue(S.sensitivity)
        self.sens_lbl.setText(f"{S.sensitivity}: {SENS_TEXT[S.sensitivity]}")
        self.min_size.setValue(S.min_object_pct)
        self.dark_boost.setChecked(S.dark_boost)
        self.auto_sup.setChecked(S.auto_suppress)
        self.night_only.setChecked(S.night_only)
        self.afps.setValue(S.analysis_fps)
        self.awidth.setValue(S.analysis_width)
        self.min_ev.setValue(S.min_event_s)
        self.gap.setValue(S.merge_gap_s)
        self.jobs.setValue(S.jobs)
        self.clock.setCurrentText(S.clock_source)
        self.outdir.setText(self._outdir_saved)
        (self.mode_comp if S.mode == "compile" else self.mode_ts).setChecked(True)
        self.pre.setValue(S.pre_roll_s)
        self.post.setValue(S.post_roll_s)
        self.overlay.setChecked(S.overlay)
        self.brighten.setChecked(S.brighten_output)
        self.fast.setChecked(S.fast_copy)
        self.keep.setChecked(S.keep_clips)
        self._update_regions_label()
        self._sync_mode()

    def _ui_to_settings(self) -> Settings:
        S = self.settings
        S.sensitivity = self.sens.value()
        S.min_object_pct = self.min_size.value()
        S.dark_boost = self.dark_boost.isChecked()
        S.auto_suppress = self.auto_sup.isChecked()
        S.night_only = self.night_only.isChecked()
        S.analysis_fps = self.afps.value()
        S.analysis_width = self.awidth.value()
        S.min_event_s = self.min_ev.value()
        S.merge_gap_s = self.gap.value()
        S.jobs = self.jobs.value()
        S.clock_source = self.clock.currentText()
        S.mode = "compile" if self.mode_comp.isChecked() else "timestamps"
        S.pre_roll_s = self.pre.value()
        S.post_roll_s = self.post.value()
        S.overlay = self.overlay.isChecked()
        S.brighten_output = self.brighten.isChecked()
        S.fast_copy = self.fast.isChecked()
        S.keep_clips = self.keep.isChecked()
        return S

    def _update_regions_label(self):
        n = len(self.settings.ignore_regions)
        self.regions_lbl.setText("none" if n == 0 else f"{n} region{'s' if n != 1 else ''}")

    # ------------------------------------------------------------------ queue
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        self.add_paths(paths)

    def _add_files_dialog(self):
        start = getattr(self, "_last_dir", str(Path.home()))
        exts = " ".join(f"*{x}" for x in sorted(VIDEO_EXTS))
        files, _ = QFileDialog.getOpenFileNames(self, "Add recordings", start,
                                                f"Video files ({exts});;All files (*)")
        if files:
            self._last_dir = str(Path(files[0]).parent)
            self.add_paths(files)

    def _add_folder_dialog(self):
        start = getattr(self, "_last_dir", str(Path.home()))
        d = QFileDialog.getExistingDirectory(self, "Add folder (searched recursively)", start)
        if d:
            self._last_dir = d
            self.add_paths([d])

    def add_paths(self, paths):
        if self._running():
            return
        new = [f for f in expand_inputs(paths) if f not in self.files]
        for f in new:
            self.files.append(f)
            r = self.queue.rowCount()
            self.queue.insertRow(r)
            it = QTableWidgetItem(Path(f).name)
            it.setToolTip(f)
            self.queue.setItem(r, Q_FILE, it)
            clk = clock_from_filename(f)
            self.queue.setItem(r, Q_START, QTableWidgetItem(clock_str(clk) if clk else ""))
            for c in (Q_LEN, Q_RES, Q_EVENTS):
                self.queue.setItem(r, c, QTableWidgetItem(""))
            self.queue.setItem(r, Q_STATUS, QTableWidgetItem("queued"))
            self.probe_pool.submit(self._probe_bg, f)
        self._update_queue_info()

    def _probe_bg(self, f):
        try:
            self.bridge.probed.emit(f, probe(f))
        except MediaError as e:
            self.bridge.probed.emit(f, e)

    def _on_probed(self, f, info):
        if f not in self.files:
            return
        r = self.files.index(f)
        if isinstance(info, Exception):
            self.queue.item(r, Q_STATUS).setText("unreadable")
            self.queue.item(r, Q_STATUS).setToolTip(str(info))
            return
        self.infos[f] = info
        self.queue.item(r, Q_LEN).setText(fmt_hms(info.duration))
        self.queue.item(r, Q_RES).setText(info.resolution)
        self._update_queue_info()

    def _update_queue_info(self):
        n = len(self.files)
        tot = sum(getattr(self.infos.get(f), "duration", 0) for f in self.files)
        self.queue_info.setText("Drop .ts files or folders here" if n == 0 else
                                f"{n} file{'s' if n != 1 else ''} · {fmt_hms(tot)} of footage")

    def _remove_selected(self):
        if self._running():
            return
        rows = sorted({i.row() for i in self.queue.selectedIndexes()}, reverse=True)
        for r in rows:
            self.queue.removeRow(r)
            del self.files[r]
        self._update_queue_info()

    def _clear_queue(self):
        if self._running():
            return
        self.queue.setRowCount(0)
        self.files.clear()
        self._update_queue_info()

    def _edit_regions(self):
        src = None
        rows = sorted({i.row() for i in self.queue.selectedIndexes()})
        if rows:
            src = self.files[rows[0]]
        elif self.files:
            src = self.files[0]
        png = None
        if src:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                info = self.infos.get(src)
                t = min(10.0, (info.duration * 0.1) if info else 5.0)
                png = grab_frame(src, t)
            finally:
                QApplication.restoreOverrideCursor()
        dlg = RegionDialog(self, png, self.settings.ignore_regions, src)
        if dlg.exec():
            self.settings.ignore_regions = dlg.regions()
            self._update_regions_label()
            self._save_settings()

    def _pick_outdir(self):
        d = QFileDialog.getExistingDirectory(self, "Output folder", self.outdir.text() or str(Path.home()))
        if d:
            self.outdir.setText(d)

    # ------------------------------------------------------------------ run
    def _running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _set_busy(self, busy: bool):
        for w in (self.btn_add, self.btn_add_dir, self.btn_remove, self.btn_clear, self.btn_start,
                  self.btn_regions):
            w.setEnabled(not busy)
        self.btn_cancel.setEnabled(busy)
        for box in self.findChildren(QGroupBox):
            box.setEnabled(not busy)

    def _start(self):
        if not self.files:
            QMessageBox.information(self, "Nothing queued", "Add some .ts files or a folder first.")
            return
        try:
            require_tools()
        except MediaError as e:
            QMessageBox.critical(self, "ffmpeg missing", str(e))
            return
        S = self._ui_to_settings()
        outdir = self.outdir.text().strip() or str(system.default_output_dir())
        self.outdir.setText(outdir)
        self._save_settings()
        self.results.clear()
        self.event_rows.clear()
        self.events.setRowCount(0)
        self.logbox.clear()
        self._prog = [0.0] * len(self.files)
        self._comp = 0.0
        self._compiling = S.mode == "compile"
        for r in range(self.queue.rowCount()):
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(0)
            bar.setFormat("waiting")
            bar.setTextVisible(True)
            self.queue.setCellWidget(r, Q_STATUS, bar)
            self.queue.item(r, Q_EVENTS).setText("")
        for b in (self.btn_folder, self.btn_comp, self.btn_report):
            b.setEnabled(False)
        self.progress.setValue(0)
        self._set_busy(True)
        self.cancel_ev = threading.Event()
        files = list(self.files)
        debug = self.debug.isChecked()
        cb = Callbacks(file_progress=self.bridge.file_progress.emit, file_done=self.bridge.file_done.emit,
                       stage=self.bridge.stage.emit, log=self.bridge.log.emit,
                       compile_progress=self.bridge.compile_progress.emit)

        def work():
            try:
                out = run(files, S, outdir, cb, self.cancel_ev, debug_video=debug)
                self.bridge.finished.emit(out)
            except Exception as e:     # surface anything unexpected in the UI
                import traceback
                self.bridge.failed.emit(f"{e}\n\n{traceback.format_exc()}")

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _cancel(self):
        if self.cancel_ev is not None:
            self.cancel_ev.set()
            self.status.setText("Cancelling…")
            self.btn_cancel.setEnabled(False)

    def _overall(self):
        a = sum(self._prog) / max(1, len(self._prog))
        v = a * (0.8 if self._compiling else 1.0) + (0.2 * self._comp if self._compiling else 0.0)
        self.progress.setValue(int(v * 1000))

    def _on_file_progress(self, i, f):
        if i < len(self._prog):
            self._prog[i] = f
            bar = self.queue.cellWidget(i, Q_STATUS)
            if isinstance(bar, QProgressBar):
                bar.setValue(int(f * 1000))
                bar.setFormat(f"analysing {f * 100:.0f}%")
            self._overall()

    def _on_compile_progress(self, f):
        self._comp = f
        self._overall()

    def _on_file_done(self, i, r: FileResult):
        self.results[i] = r
        self._prog[i] = 1.0
        self.queue.removeCellWidget(i, Q_STATUS)
        st = self.queue.item(i, Q_STATUS)
        if r.error:
            st.setText("error")
            st.setToolTip(r.error)
            st.setForeground(QColor(255, 138, 128))
        else:
            spd = (r.info.duration / r.elapsed) if r.elapsed > 0 else 0
            st.setText(f"done · {spd:.0f}× realtime" + (" · GPU" if r.decoder != "CPU" else ""))
            st.setToolTip(f"Decoded with: {r.decoder}")
            st.setForeground(QColor(140, 200, 150))
        self.queue.item(i, Q_EVENTS).setText("!" if r.error else str(len(r.events)))
        for ev in r.events:
            self._add_event_row(r, ev)
        self._overall()

    def _add_event_row(self, r: FileResult, ev):
        row = self.events.rowCount()
        self.events.insertRow(row)
        icon_item = QTableWidgetItem()
        if ev.thumb_jpeg:
            pm = QPixmap()
            pm.loadFromData(ev.thumb_jpeg, "JPG")
            icon_item.setIcon(QIcon(pm.scaled(256, 144, Qt.AspectRatioMode.KeepAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation)))
        self.events.setItem(row, E_THUMB, icon_item)
        when = clock_str(ev.clock_start) if ev.clock_start else "-"
        self.events.setItem(row, E_WHEN, QTableWidgetItem(when))
        fi = QTableWidgetItem(Path(r.path).name)
        fi.setToolTip(r.path)
        self.events.setItem(row, E_FILE, fi)
        self.events.setItem(row, E_POS, QTableWidgetItem("+" + fmt_hms(ev.start)))
        self.events.setItem(row, E_LEN, QTableWidgetItem(f"{ev.duration:.1f} s"))
        self.events.setItem(row, E_KIND, QTableWidgetItem(", ".join(ev.kinds) or ev.kind))
        self.event_rows.append((r, ev))

    def _on_finished(self, out):
        self.worker = None
        self._set_busy(False)
        self.last_out = out
        self.progress.setValue(1000)
        n = sum(len(r.events) for r in out.results)
        errs = sum(1 for r in out.results if r.error)
        msg = ("Cancelled" if out.cancelled else "Done") + f": {n} event(s) in {len(out.results)} file(s)"
        if errs:
            msg += f", {errs} unreadable"
        self.status.setText(msg)
        self.btn_folder.setEnabled(True)
        self.btn_report.setEnabled(bool(out.reports))
        self.btn_comp.setEnabled(out.compilation is not None)

    def _on_failed(self, msg):
        self.worker = None
        self._set_busy(False)
        self.status.setText("Failed")
        self._log(msg)
        QMessageBox.critical(self, "Nightwatch error", msg.split("\n\n")[0])

    def _log(self, m):
        self.logbox.appendPlainText(m)

    # ------------------------------------------------------------------ playback
    def _launch(self, path: str, t: float = 0.0):
        cmd = system.player_command(path, t)
        if cmd is None:
            if system.open_with_default_app(path):
                self.status.setText("Opened in your default player. Install mpv or VLC to jump straight "
                                    "to the event.")
                return
            hint = ("winget install VideoLAN.VLC" if system.is_windows() else "sudo pacman -S mpv")
            QMessageBox.information(self, "No player", f"Install mpv or VLC to play clips:  {hint}")
            return
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **system.detached_kw())
        except OSError as e:
            QMessageBox.warning(self, "Could not start player", str(e))

    def _play_event(self, row: int, in_compilation: bool):
        if row < 0 or row >= len(self.event_rows):
            return
        r, ev = self.event_rows[row]
        if in_compilation and self.last_out and self.last_out.compilation:
            self._launch(str(self.last_out.compilation), ev.comp_t or 0.0)
        else:
            self._launch(r.path, max(0.0, ev.start - self.settings.pre_roll_s))

    def _event_menu(self, pos):
        row = self.events.rowAt(pos.y())
        if row < 0:
            return
        m = QMenu(self)
        a1 = QAction("Play from source file", self)
        a1.triggered.connect(lambda: self._play_event(row, False))
        m.addAction(a1)
        if self.last_out and self.last_out.compilation:
            a2 = QAction("Play in compilation", self)
            a2.triggered.connect(lambda: self._play_event(row, True))
            m.addAction(a2)
        a3 = QAction("Copy timestamp", self)
        r, ev = self.event_rows[row]
        txt = (clock_str(ev.clock_start) + "  " if ev.clock_start else "") + f"{Path(r.path).name} +{fmt_hms(ev.start)}"
        a3.triggered.connect(lambda: QApplication.clipboard().setText(txt))
        m.addAction(a3)
        m.exec(self.events.viewport().mapToGlobal(pos))

    def _open_folder(self):
        if self.last_out:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_out.run_dir.resolve())))

    def _open_report(self):
        if self.last_out and self.last_out.reports:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_out.reports["html"])))

    def _play_compilation(self):
        if self.last_out and self.last_out.compilation:
            self._launch(str(self.last_out.compilation))

    def closeEvent(self, e):
        if self._running():
            if QMessageBox.question(self, "Still running", "Cancel the current run and quit?") != \
                    QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.cancel_ev.set()
            self.worker.join(timeout=5)
        self._ui_to_settings()
        self._save_settings()
        self.probe_pool.shutdown(wait=False, cancel_futures=True)
        e.accept()


STYLE = """
QLabel#dim { color: #8b9098; }
QLabel#warn { color: #ffb74d; }
QToolButton#link { border: none; color: #8fb3ff; padding: 0; }
QGroupBox { border: 1px solid #33373e; border-radius: 6px; margin-top: 12px; padding: 8px 6px 6px 6px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #c8ccd2; font-weight: 600; }
QCheckBox::indicator, QRadioButton::indicator { width: 14px; height: 14px; }
QCheckBox::indicator { border: 1px solid #6b7079; border-radius: 3px; background: #16181b; }
QRadioButton::indicator { border: 1px solid #6b7079; border-radius: 8px; background: #16181b; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked { background: #d6a82e; border-color: #d6a82e; }
QRadioButton::indicator:checked { background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
    stop:0 #d6a82e, stop:0.42 #d6a82e, stop:0.5 #16181b, stop:1 #16181b); border: 1px solid #d6a82e; }
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled { border-color: #3a3e45; background: #1d1f23; }
QCheckBox::indicator:checked:disabled { background: #6b5a2a; border-color: #6b5a2a; }
QPushButton#start { font-weight: 600; padding: 7px 14px; background: #d6a82e; color: #16181b;
                    border: none; border-radius: 4px; }
QPushButton#start:disabled { background: #4a4330; color: #8b8572; }
QProgressBar { border: none; background: #24272c; border-radius: 4px; }
QProgressBar::chunk { background: #d6a82e; border-radius: 4px; }
QTableWidget QProgressBar { background: #24272c; color: #d0d3d8; text-align: center; border-radius: 3px; }
QTableWidget QProgressBar::chunk { background: #3f6aa3; }
QHeaderView::section { background: #26292e; color: #b8bcc3; border: none; border-right: 1px solid #1c1e22;
                       padding: 4px 6px; }
"""


def _dark_palette(app: QApplication):
    app.setStyle("Fusion")
    p = QPalette()
    base, alt, text = QColor(24, 26, 30), QColor(32, 35, 40), QColor(225, 227, 230)
    p.setColor(QPalette.ColorRole.Window, QColor(30, 32, 37))
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, alt)
    p.setColor(QPalette.ColorRole.ToolTipBase, alt)
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, QColor(42, 45, 51))
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.Highlight, QColor(214, 168, 46))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(20, 20, 20))
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(120, 124, 130))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(110, 113, 118))
    app.setPalette(p)
    app.setStyleSheet(STYLE)


def main(argv=None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Nightwatch")
    if os.environ.get("NIGHTWATCH_SYSTEM_THEME") != "1":
        _dark_palette(app)
    w = MainWindow()
    extra = [a for a in (argv or sys.argv)[1:] if a not in ("gui",) and Path(a).exists()]
    if extra:
        w.add_paths(extra)
    w.show()
    return app.exec()
