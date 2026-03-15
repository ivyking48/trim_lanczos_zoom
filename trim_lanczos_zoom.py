#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trim + Lanczos Zoom: Load a video, trim to a frame range,
select a crop region, and export with lanczos-resampled zoom.
"""

from __future__ import annotations
import sys, os, shutil, subprocess, re, tempfile, uuid
from pathlib import Path
from typing import List, Optional, Tuple

# ------------------------------ Config ----------------------------------------

EXPORT_CRF = 16
EXPORT_PRESET = "slow"
PRINT_FFMPEG_CMDS = True

PROXY_LONG_SIDE = 720
PROXY_FPS = 30
PROXY_CRF = 24
PROXY_PRESET = "veryfast"

# ------------------------------ FFmpeg helpers --------------------------------

def _which_ffmpeg() -> str:
    return os.environ.get("IMAGEIO_FFMPEG_EXE") or shutil.which("ffmpeg") or "ffmpeg"

def _which_ffprobe() -> str:
    cand = shutil.which("ffprobe")
    if cand:
        return cand
    ff = _which_ffmpeg()
    if ff and "/ffmpeg" in ff:
        maybe = ff.replace("/ffmpeg", "/ffprobe")
        if os.path.exists(maybe):
            return maybe
    return "ffprobe"

def even(x) -> int:
    v = max(2, int(round(x)))
    return v if v % 2 == 0 else v + 1

def _run_ffmpeg(cmd: List[str], progress_cb=None) -> None:
    if PRINT_FFMPEG_CMDS:
        print("[FFMPEG]", " ".join(map(str, cmd)))
    run = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if run.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({run.returncode}):\n{run.stdout}")

def probe_size(p: Path) -> Tuple[int, int]:
    ffprobe = _which_ffprobe()
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0:s=x", str(p)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True
        ).stdout.strip()
        w, h = out.split("x")[:2]
        return int(w), int(h)
    except Exception:
        return (1920, 1080)

def probe_fps(p: Path) -> float:
    ffprobe = _which_ffprobe()
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate",
             "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True
        ).stdout.strip()
        if "/" in out:
            num, den = out.split("/")
            return float(num) / float(den)
        return float(out)
    except Exception:
        return 30.0

def probe_duration(p: Path) -> float:
    ffprobe = _which_ffprobe()
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True
        ).stdout.strip()
        return max(0.0, float(out))
    except Exception:
        return 0.0

def probe_has_audio(p: Path) -> bool:
    ffprobe = _which_ffprobe()
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(p)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True
        ).stdout.strip()
        return len(out) > 0
    except Exception:
        return False

_proxy_cache: dict[str, Path] = {}

def make_proxy(in_path: Path) -> Path:
    key = str(in_path.resolve())
    if key in _proxy_cache and _proxy_cache[key].exists():
        return _proxy_cache[key]

    w, h = probe_size(in_path)
    long_side = max(w, h)
    scale_factor = PROXY_LONG_SIDE / long_side if long_side > PROXY_LONG_SIDE else 1.0
    out_w = max(2, even(w * scale_factor))
    out_h = max(2, even(h * scale_factor))

    out = Path(tempfile.gettempdir()) / f"proxy_{uuid.uuid4().hex}_{out_w}x{out_h}.mp4"
    cmd = [
        _which_ffmpeg(), "-hide_banner", "-y",
        "-i", str(in_path),
        "-vf", f"scale={out_w}:{out_h}:flags=bicubic",
        "-r", str(PROXY_FPS),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-c:v", "libx264",
        "-preset", PROXY_PRESET,
        "-crf", str(PROXY_CRF),
        "-an",
        str(out)
    ]
    _run_ffmpeg(cmd)
    _proxy_cache[key] = out
    return out

def probe_codec(p: Path) -> str:
    ffprobe = _which_ffprobe()
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(p)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True
        ).stdout.strip()
        return out
    except Exception:
        return "unknown"

# ------------------------------ Qt imports ------------------------------------

from PySide6.QtCore import Qt, QRectF, QPointF, QUrl, QSizeF, Signal
from PySide6.QtGui import QBrush, QColor, QPen, QPainter, QPainterPath, QAction
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QApplication, QGraphicsScene, QGraphicsView, QGraphicsRectItem,
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
    QFileDialog, QLabel, QSlider, QSpinBox, QMessageBox, QGroupBox,
    QSizePolicy, QProgressDialog, QCheckBox
)

# ------------------------------ Trim Slider -----------------------------------

class TrimSlider(QWidget):
    """Custom widget with a scrub slider plus draggable trim start/end handles."""
    valueChanged = Signal(int)
    trim_start_changed = Signal(int)
    trim_end_changed = Signal(int)

    HANDLE_W = 10  # width of the draggable trim handles

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(40)
        self.setMouseTracking(True)
        self._min = 0
        self._max = 0
        self._value = 0  # current scrub position
        self._trim_start = 0
        self._trim_end = 0
        self._dragging: Optional[str] = None  # "start", "end", "scrub", or None

    def setMinimum(self, v: int): self._min = v; self.update()
    def setMaximum(self, v: int): self._max = v; self.update()
    def minimum(self) -> int: return self._min
    def maximum(self) -> int: return self._max
    def value(self) -> int: return self._value

    def setValue(self, v: int):
        v = max(self._min, min(self._max, v))
        if v != self._value:
            self._value = v
            self.update()
            self.valueChanged.emit(v)

    def blockSignals(self, block: bool):
        super().blockSignals(block)

    def set_trim_range(self, start: int, end: int):
        self._trim_start = max(self._min, start)
        self._trim_end = min(self._max, end)
        self.update()

    def _frame_to_x(self, frame: int) -> int:
        margin = self.HANDLE_W
        usable = self.width() - margin * 2
        ratio = (frame - self._min) / max(1, self._max - self._min)
        return int(margin + ratio * usable)

    def _x_to_frame(self, x: int) -> int:
        margin = self.HANDLE_W
        usable = self.width() - margin * 2
        ratio = max(0.0, min(1.0, (x - margin) / max(1, usable)))
        return int(self._min + ratio * (self._max - self._min))

    def paintEvent(self, event):
        if self._max <= self._min:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        h = self.height()
        groove_y = h // 2
        groove_h = 6

        x_start = self._frame_to_x(self._trim_start)
        x_end = self._frame_to_x(self._trim_end)
        x_scrub = self._frame_to_x(self._value)

        # Background groove
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(60, 60, 60))
        p.drawRoundedRect(self.HANDLE_W, groove_y - groove_h // 2,
                          self.width() - self.HANDLE_W * 2, groove_h, 3, 3)

        # Dimmed regions outside trim
        p.setBrush(QColor(0, 0, 0, 120))
        p.drawRect(self.HANDLE_W, groove_y - groove_h // 2,
                   x_start - self.HANDLE_W, groove_h)
        p.drawRect(x_end, groove_y - groove_h // 2,
                   self.width() - self.HANDLE_W - x_end, groove_h)

        # Active range highlight
        p.setBrush(QColor(0, 170, 255, 80))
        p.drawRect(x_start, groove_y - groove_h // 2, x_end - x_start, groove_h)

        # Trim start handle (green)
        p.setBrush(QColor(0, 220, 100))
        p.setPen(QPen(QColor(0, 180, 80), 1))
        p.drawRoundedRect(x_start - self.HANDLE_W // 2, 2, self.HANDLE_W, h - 4, 3, 3)

        # Trim end handle (red)
        p.setBrush(QColor(255, 80, 80))
        p.setPen(QPen(QColor(200, 60, 60), 1))
        p.drawRoundedRect(x_end - self.HANDLE_W // 2, 2, self.HANDLE_W, h - 4, 3, 3)

        # Scrub position (white line)
        p.setPen(QPen(QColor(255, 255, 255), 2))
        p.drawLine(x_scrub, 4, x_scrub, h - 4)
        # Small circle handle
        p.setBrush(QColor(0, 170, 255))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(x_scrub, groove_y), 7, 7)

        p.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        x = event.pos().x()
        x_start = self._frame_to_x(self._trim_start)
        x_end = self._frame_to_x(self._trim_end)
        grab_dist = self.HANDLE_W + 4

        if abs(x - x_start) < grab_dist:
            self._dragging = "start"
        elif abs(x - x_end) < grab_dist:
            self._dragging = "end"
        else:
            self._dragging = "scrub"
            self.setValue(self._x_to_frame(x))

    def mouseMoveEvent(self, event):
        if self._dragging is None:
            # Update cursor based on proximity to handles
            x = event.pos().x()
            x_start = self._frame_to_x(self._trim_start)
            x_end = self._frame_to_x(self._trim_end)
            if abs(x - x_start) < self.HANDLE_W + 4 or abs(x - x_end) < self.HANDLE_W + 4:
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        frame = self._x_to_frame(event.pos().x())
        if self._dragging == "start":
            frame = max(self._min, min(frame, self._trim_end - 1))
            self._trim_start = frame
            self.trim_start_changed.emit(frame)
            self.update()
        elif self._dragging == "end":
            frame = max(self._trim_start + 1, min(frame, self._max))
            self._trim_end = frame
            self.trim_end_changed.emit(frame)
            self.update()
        elif self._dragging == "scrub":
            self.setValue(frame)

    def mouseReleaseEvent(self, event):
        self._dragging = None

# ------------------------------ Video Preview ---------------------------------

class VideoPreview(QGraphicsView):
    crop_changed = Signal(QRectF)  # emits crop rect in native video coords

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("background: black; border: none;")

        self._video_item = QGraphicsVideoItem()
        self._scene.addItem(self._video_item)

        self._player = QMediaPlayer()
        self._audio = QAudioOutput()
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self._video_item)
        self._audio.setVolume(0.5)

        self._native_w = 1920
        self._native_h = 1080
        self._video_item.nativeSizeChanged.connect(self._on_native_size)

        # Crop overlay state
        self._crop_rect: Optional[QRectF] = None  # in native video coords
        self._crop_item: Optional[QGraphicsRectItem] = None
        self._drawing = False
        self._dragging_crop = False
        self._drag_offset = QPointF()
        self._draw_start = QPointF()
        self._crop_enabled = True
        self._crop_aspect = 9.0 / 16.0  # Instagram Story aspect ratio

    @property
    def player(self) -> QMediaPlayer:
        return self._player

    def load_video(self, path: Path, proxy_path: Optional[Path] = None):
        w, h = probe_size(path)
        self._native_w = w
        self._native_h = h
        self._remove_crop_item()
        play_path = proxy_path if proxy_path else path
        self._player.setSource(QUrl.fromLocalFile(str(play_path)))
        self._player.pause()
        self._fit_view()
        # Set default 9:16 crop centered on video
        self._set_default_crop()

    def _set_default_crop(self):
        # Fit a 9:16 rectangle as large as possible within the video
        crop_w = self._native_h * self._crop_aspect
        crop_h = self._native_h
        if crop_w > self._native_w:
            crop_w = self._native_w
            crop_h = self._native_w / self._crop_aspect
        x = (self._native_w - crop_w) / 2
        y = (self._native_h - crop_h) / 2
        self._crop_rect = QRectF(x, y, crop_w, crop_h)
        self._draw_crop_overlay()
        self.crop_changed.emit(self._crop_rect)

    def _on_native_size(self, size: QSizeF):
        if size.width() > 0 and size.height() > 0:
            self._native_w = int(size.width())
            self._native_h = int(size.height())
            self._video_item.setSize(size)
            self._scene.setSceneRect(0, 0, size.width(), size.height())
            self._fit_view()

    def _fit_view(self):
        self._scene.setSceneRect(0, 0, self._native_w, self._native_h)
        self._video_item.setSize(QSizeF(self._native_w, self._native_h))
        self.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_view()

    def get_crop_native(self) -> Optional[Tuple[int, int, int, int]]:
        if self._crop_rect is None:
            return None
        r = self._crop_rect
        x = max(0, int(r.x()))
        y = max(0, int(r.y()))
        w = min(int(r.width()), self._native_w - x)
        h = min(int(r.height()), self._native_h - y)
        return (x, y, even(w), even(h))

    def clear_crop(self):
        self._crop_rect = None
        self._remove_crop_item()
        self.crop_changed.emit(QRectF())

    def set_crop_enabled(self, enabled: bool):
        self._crop_enabled = enabled

    def _remove_crop_item(self):
        if self._crop_item:
            self._scene.removeItem(self._crop_item)
            self._crop_item = None
        # Remove dark overlays
        for item in self._scene.items():
            if isinstance(item, QGraphicsRectItem) and item is not self._video_item:
                if item.data(0) == "overlay":
                    self._scene.removeItem(item)

    def _draw_crop_overlay(self):
        self._remove_crop_item()
        if self._crop_rect is None:
            return
        r = self._crop_rect
        pen = QPen(QColor(0, 200, 255), 2)
        self._crop_item = self._scene.addRect(r, pen, QBrush(Qt.BrushStyle.NoBrush))

        dark = QColor(0, 0, 0, 140)
        scene_r = QRectF(0, 0, self._native_w, self._native_h)
        # Top
        top = self._scene.addRect(QRectF(0, 0, self._native_w, r.y()), QPen(Qt.PenStyle.NoPen), QBrush(dark))
        top.setData(0, "overlay")
        # Bottom
        bot = self._scene.addRect(QRectF(0, r.bottom(), self._native_w, self._native_h - r.bottom()), QPen(Qt.PenStyle.NoPen), QBrush(dark))
        bot.setData(0, "overlay")
        # Left
        left = self._scene.addRect(QRectF(0, r.y(), r.x(), r.height()), QPen(Qt.PenStyle.NoPen), QBrush(dark))
        left.setData(0, "overlay")
        # Right
        right = self._scene.addRect(QRectF(r.right(), r.y(), self._native_w - r.right(), r.height()), QPen(Qt.PenStyle.NoPen), QBrush(dark))
        right.setData(0, "overlay")

    def mousePressEvent(self, event):
        if not self._crop_enabled or event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        scene_pos = self.mapToScene(event.pos())
        # If clicking inside existing crop, drag it
        if self._crop_rect and self._crop_rect.contains(scene_pos):
            self._dragging_crop = True
            self._drag_offset = scene_pos - self._crop_rect.topLeft()
        else:
            # Start drawing a new crop (locked to 9:16)
            self._drawing = True
            self._draw_start = scene_pos
            self._remove_crop_item()

    def mouseMoveEvent(self, event):
        scene_pos = self.mapToScene(event.pos())
        if self._dragging_crop and self._crop_rect:
            new_x = scene_pos.x() - self._drag_offset.x()
            new_y = scene_pos.y() - self._drag_offset.y()
            # Clamp to video bounds
            new_x = max(0, min(new_x, self._native_w - self._crop_rect.width()))
            new_y = max(0, min(new_y, self._native_h - self._crop_rect.height()))
            self._crop_rect = QRectF(new_x, new_y, self._crop_rect.width(), self._crop_rect.height())
            self._draw_crop_overlay()
            return
        if not self._drawing:
            return super().mouseMoveEvent(event)
        # Draw new crop locked to 9:16
        dx = scene_pos.x() - self._draw_start.x()
        dy = scene_pos.y() - self._draw_start.y()
        # Determine size from the larger drag axis, lock aspect
        w = abs(dx)
        h = w / self._crop_aspect
        if h > abs(dy) and abs(dy) > 0:
            h = abs(dy)
            w = h * self._crop_aspect
        # Determine direction
        x1 = self._draw_start.x() - (w if dx < 0 else 0)
        y1 = self._draw_start.y() - (h if dy < 0 else 0)
        # Clamp
        x1 = max(0, min(x1, self._native_w - w))
        y1 = max(0, min(y1, self._native_h - h))
        w = min(w, self._native_w)
        h = min(h, self._native_h)
        self._crop_rect = QRectF(x1, y1, w, h)
        self._draw_crop_overlay()

    def mouseReleaseEvent(self, event):
        if self._dragging_crop:
            self._dragging_crop = False
            if self._crop_rect:
                self.crop_changed.emit(self._crop_rect)
            return
        if not self._drawing:
            return super().mouseReleaseEvent(event)
        self._drawing = False
        if self._crop_rect and self._crop_rect.width() > 10 and self._crop_rect.height() > 10:
            self.crop_changed.emit(self._crop_rect)
        else:
            self._set_default_crop()

# ------------------------------ Main Window -----------------------------------

class MainWindow(QMainWindow):
    def __init__(self, video_path: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("Trim + Lanczos Zoom")
        self.resize(1200, 800)
        self.setStyleSheet("""
            QMainWindow { background: #1e1e1e; }
            QLabel { color: #ddd; font-size: 13px; }
            QPushButton { background: #333; color: #ddd; border: 1px solid #555;
                          padding: 6px 14px; border-radius: 4px; font-size: 13px; }
            QPushButton:hover { background: #444; }
            QSpinBox { background: #2a2a2a; color: #ddd; border: 1px solid #555;
                       padding: 4px; font-size: 13px; }
            QGroupBox { color: #aaa; border: 1px solid #444; border-radius: 4px;
                        margin-top: 8px; padding-top: 14px; font-size: 13px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; }
            QSlider::groove:horizontal { background: #444; height: 6px; border-radius: 3px; }
            QSlider::handle:horizontal { background: #0af; width: 14px; margin: -5px 0;
                                         border-radius: 7px; }
        """)

        self._video_path: Optional[Path] = None
        self._fps = 30.0
        self._total_frames = 0
        self._duration = 0.0
        self._has_audio = False

        # Central layout
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)

        # Video preview
        self._preview = VideoPreview()
        main_layout.addWidget(self._preview, stretch=1)

        # Frame slider
        slider_layout = QHBoxLayout()
        self._frame_slider = TrimSlider()
        self._frame_slider.setMinimum(0)
        self._frame_slider.setMaximum(0)
        self._frame_slider.valueChanged.connect(self._on_slider)
        self._frame_slider.trim_start_changed.connect(self._on_slider_trim_start)
        self._frame_slider.trim_end_changed.connect(self._on_slider_trim_end)
        self._frame_label = QLabel("0 / 0")
        self._frame_label.setMinimumWidth(120)
        self._time_label = QLabel("00:00.000")
        self._time_label.setMinimumWidth(90)
        slider_layout.addWidget(self._frame_slider, stretch=1)
        slider_layout.addWidget(self._frame_label)
        slider_layout.addWidget(self._time_label)
        main_layout.addLayout(slider_layout)

        # Controls row
        controls = QHBoxLayout()

        # Trim group
        trim_group = QGroupBox("Trim (frames)")
        trim_layout = QHBoxLayout(trim_group)
        trim_layout.addWidget(QLabel("Start:"))
        self._trim_start = QSpinBox()
        self._trim_start.setMinimum(0)
        self._trim_start.setMaximum(0)
        trim_layout.addWidget(self._trim_start)
        trim_layout.addWidget(QLabel("End:"))
        self._trim_end = QSpinBox()
        self._trim_end.setMinimum(0)
        self._trim_end.setMaximum(0)
        trim_layout.addWidget(self._trim_end)
        self._set_start_btn = QPushButton("Set Start")
        self._set_start_btn.clicked.connect(lambda: self._trim_start.setValue(self._frame_slider.value()))
        trim_layout.addWidget(self._set_start_btn)
        self._set_end_btn = QPushButton("Set End")
        self._set_end_btn.clicked.connect(lambda: self._trim_end.setValue(self._frame_slider.value()))
        trim_layout.addWidget(self._set_end_btn)
        controls.addWidget(trim_group)

        # Crop info
        crop_group = QGroupBox("Crop / Zoom")
        crop_layout = QHBoxLayout(crop_group)
        self._crop_label = QLabel("No crop (draw on video)")
        crop_layout.addWidget(self._crop_label)
        self._clear_crop_btn = QPushButton("Clear Crop")
        self._clear_crop_btn.clicked.connect(self._clear_crop)
        crop_layout.addWidget(self._clear_crop_btn)
        controls.addWidget(crop_group)

        # Output size
        out_group = QGroupBox("Output")
        out_layout = QHBoxLayout(out_group)
        out_layout.addWidget(QLabel("W:"))
        self._out_w = QSpinBox()
        self._out_w.setRange(2, 7680)
        self._out_w.setValue(1920)
        out_layout.addWidget(self._out_w)
        out_layout.addWidget(QLabel("H:"))
        self._out_h = QSpinBox()
        self._out_h.setRange(2, 4320)
        self._out_h.setValue(1080)
        out_layout.addWidget(self._out_h)
        self._lock_aspect = QCheckBox("Lock AR")
        self._lock_aspect.setChecked(True)
        self._lock_aspect.setStyleSheet("color: #ddd;")
        out_layout.addWidget(self._lock_aspect)
        controls.addWidget(out_group)

        main_layout.addLayout(controls)

        # Bottom buttons
        btn_layout = QHBoxLayout()
        self._open_btn = QPushButton("Open Video")
        self._open_btn.clicked.connect(self._open_file)
        btn_layout.addWidget(self._open_btn)

        self._play_btn = QPushButton("Play / Pause")
        self._play_btn.clicked.connect(self._toggle_play)
        btn_layout.addWidget(self._play_btn)

        self._export_btn = QPushButton("Export")
        self._export_btn.clicked.connect(self._export)
        btn_layout.addWidget(self._export_btn)

        btn_layout.addStretch()
        self._info_label = QLabel("")
        btn_layout.addWidget(self._info_label)
        main_layout.addLayout(btn_layout)

        # Connect signals
        self._preview.crop_changed.connect(self._on_crop_changed)
        self._preview.player.positionChanged.connect(self._on_position_changed)
        self._out_w.valueChanged.connect(self._on_out_w_changed)
        self._trim_start.valueChanged.connect(self._on_trim_changed)
        self._trim_end.valueChanged.connect(self._on_trim_changed)

        # Load file if provided
        if video_path:
            self._load_video(Path(video_path))

    def _open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Video", "",
            "Video Files (*.mp4 *.mov *.avi *.mkv *.webm);;All Files (*)"
        )
        if path:
            self._load_video(Path(path))

    def _load_video(self, path: Path):
        self._video_path = path
        self._fps = probe_fps(path)
        self._duration = probe_duration(path)
        self._total_frames = max(1, int(self._duration * self._fps))
        self._has_audio = probe_has_audio(path)
        native_w, native_h = probe_size(path)

        self._frame_slider.setMaximum(self._total_frames - 1)
        self._trim_start.setMaximum(self._total_frames - 1)
        self._trim_end.setMaximum(self._total_frames - 1)
        self._trim_start.setValue(0)
        self._trim_end.setValue(self._total_frames - 1)
        self._frame_slider.setValue(0)

        self._out_w.setValue(native_w)
        self._out_h.setValue(native_h)

        self._frame_slider.set_trim_range(0, self._total_frames - 1)
        self._info_label.setText(f"Creating proxy for {path.name}...")
        QApplication.processEvents()
        proxy_path = make_proxy(path)
        self._preview.load_video(path, proxy_path)
        codec = probe_codec(path)
        self._info_label.setText(
            f"{path.name}  |  {native_w}x{native_h}  |  {self._fps:.2f} fps  |  "
            f"{self._total_frames} frames  |  {codec}"
        )
        self.setWindowTitle(f"Trim + Lanczos Zoom - {path.name}")

    def _on_slider(self, frame: int):
        if self._fps > 0:
            ms = int(frame / self._fps * 1000)
            self._preview.player.setPosition(ms)
        self._frame_label.setText(f"{frame} / {self._total_frames}")
        secs = frame / self._fps if self._fps > 0 else 0
        mins = int(secs // 60)
        s = secs % 60
        self._time_label.setText(f"{mins:02d}:{s:06.3f}")

    def _on_position_changed(self, ms: int):
        if self._fps > 0:
            frame = int(ms / 1000.0 * self._fps)
            self._frame_slider.blockSignals(True)
            self._frame_slider.setValue(frame)
            self._frame_slider.blockSignals(False)
            self._frame_label.setText(f"{frame} / {self._total_frames}")
            secs = frame / self._fps
            mins = int(secs // 60)
            s = secs % 60
            self._time_label.setText(f"{mins:02d}:{s:06.3f}")

    def _toggle_play(self):
        p = self._preview.player
        if p.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            p.pause()
        else:
            p.play()

    def _on_crop_changed(self, rect: QRectF):
        crop = self._preview.get_crop_native()
        if crop:
            x, y, w, h = crop
            self._crop_label.setText(f"{w}x{h} @ ({x}, {y})")
            if self._lock_aspect.isChecked():
                self._out_w.blockSignals(True)
                self._out_h.blockSignals(True)
                self._out_w.setValue(even(w))
                self._out_h.setValue(even(h))
                self._out_w.blockSignals(False)
                self._out_h.blockSignals(False)
        else:
            self._crop_label.setText("No crop (draw on video)")

    def _on_out_w_changed(self, val: int):
        if self._lock_aspect.isChecked():
            crop = self._preview.get_crop_native()
            if crop:
                _, _, cw, ch = crop
                aspect = ch / cw if cw > 0 else 1.0
            elif self._video_path:
                nw, nh = probe_size(self._video_path)
                aspect = nh / nw if nw > 0 else 1.0
            else:
                aspect = 9 / 16
            self._out_h.blockSignals(True)
            self._out_h.setValue(even(val * aspect))
            self._out_h.blockSignals(False)

    def _on_trim_changed(self):
        self._frame_slider.set_trim_range(self._trim_start.value(), self._trim_end.value())

    def _on_slider_trim_start(self, frame: int):
        self._trim_start.setValue(frame)

    def _on_slider_trim_end(self, frame: int):
        self._trim_end.setValue(frame)

    def _clear_crop(self):
        self._preview.clear_crop()
        self._crop_label.setText("No crop (draw on video)")

    def _export(self):
        if not self._video_path:
            QMessageBox.warning(self, "No video", "Please open a video first.")
            return

        start_frame = self._trim_start.value()
        end_frame = self._trim_end.value()
        if end_frame <= start_frame:
            QMessageBox.warning(self, "Invalid range", "End frame must be after start frame.")
            return

        default_name = self._video_path.stem + "_export.mp4"
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Export Video",
            str(self._video_path.parent / default_name),
            "MP4 Files (*.mp4);;All Files (*)"
        )
        if not out_path:
            return

        crop = self._preview.get_crop_native()
        out_w = even(self._out_w.value())
        out_h = even(self._out_h.value())

        try:
            cmd = self._build_export_cmd(
                self._video_path, Path(out_path),
                self._fps, start_frame, end_frame,
                crop, out_w, out_h
            )
            _run_ffmpeg(cmd)
            QMessageBox.information(self, "Done", f"Exported to:\n{out_path}")
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))

    def _build_export_cmd(
        self, input_path: Path, output_path: Path,
        fps: float, start_frame: int, end_frame: int,
        crop: Optional[Tuple[int, int, int, int]],
        out_w: int, out_h: int
    ) -> List[str]:
        ff = _which_ffmpeg()
        start_time = start_frame / fps
        end_time = (end_frame + 1) / fps

        cmd = [ff, "-hide_banner", "-y",
               "-i", str(input_path)]

        # Video filters
        vfilters = []
        vfilters.append(f"trim=start={start_time:.6f}:end={end_time:.6f}")
        vfilters.append("setpts=PTS-STARTPTS")

        if crop:
            x, y, w, h = crop
            vfilters.append(f"crop={w}:{h}:{x}:{y}")

        vfilters.append(f"scale={out_w}:{out_h}:flags=lanczos")

        cmd += ["-vf", ",".join(vfilters)]

        # Audio filters (trim to match)
        if self._has_audio:
            cmd += ["-af", f"atrim=start={start_time:.6f}:end={end_time:.6f},asetpts=PTS-STARTPTS"]
            cmd += ["-c:a", "aac", "-b:a", "256k"]
        else:
            cmd += ["-an"]

        cmd += [
            "-c:v", "libx264",
            "-crf", str(EXPORT_CRF),
            "-preset", EXPORT_PRESET,
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(output_path)
        ]
        return cmd

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Space:
            self._toggle_play()
        elif key == Qt.Key.Key_Left:
            self._frame_slider.setValue(max(0, self._frame_slider.value() - 1))
        elif key == Qt.Key.Key_Right:
            self._frame_slider.setValue(min(self._total_frames - 1, self._frame_slider.value() + 1))
        elif key == Qt.Key.Key_Home:
            self._trim_start.setValue(self._frame_slider.value())
        elif key == Qt.Key.Key_End:
            self._trim_end.setValue(self._frame_slider.value())
        elif key == Qt.Key.Key_Escape:
            self._clear_crop()
        else:
            super().keyPressEvent(event)


def main():
    app = QApplication(sys.argv)
    video = sys.argv[1] if len(sys.argv) > 1 else None
    win = MainWindow(video)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
