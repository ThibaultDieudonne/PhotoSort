"""
PhotoSort — keyboard-driven photo/video sorter
================================================
Dependencies:  pip install PySide6 Pillow pillow-heif

Controls
--------
K          keep current item  (moves to  ./_<folder>/...)
D          discard current item  (moves to  ./_discarded/...)
Left/Right browse unprocessed items

Progress is implicit: processed files are moved out of the source tree, so
re-opening the same folder continues from where you left off.
"""

import os
import sys
import queue
import shutil
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

# Must be set before QApplication is instantiated.
os.environ["QT_MEDIA_BACKEND"] = "ffmpeg"

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    _HEIF_SUPPORTED = True
except ImportError:
    _HEIF_SUPPORTED = False

from PIL import Image, ImageOps, UnidentifiedImageError  # noqa: E402

from PySide6.QtCore import Qt, QThread, Signal, QUrl, QTimer  # noqa: E402
from PySide6.QtGui import QImage, QPixmap, QKeySequence  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QStackedWidget, QSizePolicy,
    QDialog, QDialogButtonBox, QCheckBox, QGridLayout, QDoubleSpinBox,
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput  # noqa: E402
from PySide6.QtMultimediaWidgets import QVideoWidget  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRELOAD_LIMIT_BYTES: int = 5 * 1024 ** 3   # 5 GiB sliding cache ceiling
BACKWARD_WINDOW: int = 10                   # kept-loaded items behind cursor

IMAGE_EXTS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".bmp",
    ".tiff", ".tif", ".webp", ".heic", ".heif",
})
VIDEO_EXTS = frozenset({
    ".mp4", ".avi", ".mov", ".mkv", ".wmv",
    ".flv", ".m4v", ".webm", ".ts", ".mts",
})
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class AppSettings:
    """User-configurable preferences (in-process only, not persisted to disk)."""

    def __init__(self):
        self.key_keep: int = Qt.Key.Key_K
        self.key_discard: int = Qt.Key.Key_D
        self.auto_discard_mov_for_heic: bool = False
        self.video_playback_speed: float = 1.0

    def key_name(self, key) -> str:
        return QKeySequence(key).toString() or "?"


# ---------------------------------------------------------------------------
# Folder helpers
# ---------------------------------------------------------------------------


def setup_folder(root: Path) -> tuple[Path, Path]:
    """Create (if needed) the two output folders and return (keep, discard)."""
    keep = root / f"_{root.name}"
    discard = root / "_discarded"
    keep.mkdir(exist_ok=True)
    discard.mkdir(exist_ok=True)
    return keep, discard


def scan_media_files(root: Path, excluded: set[Path]) -> list[Path]:
    """Recursively collect media files, skipping *excluded* directories."""
    excluded_resolved = {p.resolve() for p in excluded}
    result: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        cur = Path(dirpath).resolve()
        # Prune excluded dirs in-place so os.walk won't descend into them.
        dirnames[:] = [
            d for d in dirnames
            if (cur / d).resolve() not in excluded_resolved
        ]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() in MEDIA_EXTS:
                result.append(p)
    result.sort(key=str)
    return result


# ---------------------------------------------------------------------------
# MediaPreloader  (background QThread)
# ---------------------------------------------------------------------------

class MediaPreloader(QThread):
    """
    Reads media files into a size-bounded in-memory cache in a background
    thread, emitting *media_ready(path_str)* each time an entry is cached.

    Cache entry format
    ------------------
    image : ('image', raw_bytes: bytes, width: int, height: int)
    video : ('video', raw_bytes: bytes, suffix: str)
    """

    media_ready = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cache: dict[str, tuple] = {}
        self._cache_size: int = 0
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    # ── Public API (call only from the main thread) ──────────────────────

    def update_queue(self, ordered: list[Path]) -> None:
        """Replace the preload queue with *ordered* (highest priority first)."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        for p in ordered:
            self._queue.put(str(p))

    def get(self, path) -> tuple | None:
        """Thread-safe cache lookup; returns the entry tuple or *None*."""
        with self._lock:
            return self._cache.get(str(path))

    def evict(self, keep_keys: set[str]) -> None:
        """Remove cached entries whose key is not in *keep_keys*."""
        with self._lock:
            for k in [k for k in self._cache if k not in keep_keys]:
                entry = self._cache.pop(k)
                self._cache_size -= len(entry[1])

    def stop(self) -> None:
        self._stop_event.set()
        self._queue.put(None)   # unblock get()

    # ── Worker loop ──────────────────────────────────────────────────────

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                path_str = self._queue.get(timeout=1)
            except queue.Empty:
                continue

            if path_str is None or self._stop_event.is_set():
                break

            with self._lock:
                if path_str in self._cache:
                    continue    # already loaded
                if self._cache_size >= PRELOAD_LIMIT_BYTES:
                    # Cache full; skip this item now.
                    # update_queue() + evict() from the main thread will
                    # free space and re-add pending items.
                    continue

            entry = self._load(path_str)
            if entry is None:
                continue

            with self._lock:
                if path_str not in self._cache:     # double-check
                    self._cache[path_str] = entry
                    self._cache_size += len(entry[1])

            self.media_ready.emit(path_str)

    def _load(self, path_str: str) -> tuple | None:
        path = Path(path_str)
        suffix = path.suffix.lower()
        try:
            if suffix in IMAGE_EXTS:
                img = Image.open(path)
                img = ImageOps.exif_transpose(img)
                img = img.convert("RGB")
                return ("image", img.tobytes(), img.width, img.height)
            elif suffix in VIDEO_EXTS:
                return ("video", path.read_bytes(), path.suffix)
        except (UnidentifiedImageError, OSError, Exception):
            pass
        return None


# ---------------------------------------------------------------------------
# MediaDisplayWidget
# ---------------------------------------------------------------------------

class MediaDisplayWidget(QWidget):
    """
    Stacked widget that can show either a scaled image (via QLabel) or a
    video (via QVideoWidget + QMediaPlayer).  Videos are written to a temp
    directory so the WMF/FFmpeg backend can open them by file path.
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        self._current_pixmap: QPixmap | None = None
        self._temp_dir: str = tempfile.mkdtemp(prefix="photosort_")
        self._active_temp: str | None = None
        self._pending_temps: list[str] = []

        # ── Stack ────────────────────────────────────────────────────────
        self._stack = QStackedWidget(self)
        self._stack.setStyleSheet("background: #111;")

        # Page 0 — images
        self._img_label = QLabel()
        self._img_label.setAlignment(Qt.AlignCenter)
        self._img_label.setStyleSheet("background: #111; color: #888; font-size: 18px;")
        self._stack.addWidget(self._img_label)

        # Page 1 — video
        self._video_widget = QVideoWidget()
        self._video_widget.setStyleSheet("background: #111;")
        self._stack.addWidget(self._video_widget)

        # ── Media player ─────────────────────────────────────────────────
        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self._video_widget)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)
        self._playback_speed: float = 1.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)

    # ── Public show methods ──────────────────────────────────────────────

    def show_image(self, raw_bytes: bytes, w: int, h: int) -> None:
        self._player.stop()
        self._current_pixmap = QPixmap.fromImage(
            QImage(raw_bytes, w, h, w * 3, QImage.Format.Format_RGB888)
        )
        self._render_pixmap()
        self._img_label.setStyleSheet("background: #111;")
        self._stack.setCurrentIndex(0)

    def show_video(self, video_bytes: bytes, suffix: str) -> None:
        self._player.stop()
        tmp = os.path.join(self._temp_dir, f"{uuid4().hex}{suffix}")
        with open(tmp, "wb") as fh:
            fh.write(video_bytes)
        if self._active_temp:
            self._pending_temps.append(self._active_temp)
        self._active_temp = tmp
        self._purge_old_temps()
        self._player.setSource(QUrl.fromLocalFile(tmp))
        self._player.setPlaybackRate(self._playback_speed)
        self._player.play()
        self._stack.setCurrentIndex(1)

    def set_playback_speed(self, speed: float) -> None:
        self._playback_speed = speed
        if self._stack.currentIndex() == 1:
            self._player.setPlaybackRate(speed)

    def show_placeholder(self, text: str) -> None:
        self._player.stop()
        self._current_pixmap = None
        self._img_label.setPixmap(QPixmap())
        self._img_label.setText(text)
        self._img_label.setStyleSheet(
            "background: #111; color: #888; font-size: 20px;"
        )
        self._stack.setCurrentIndex(0)

    def stop_media(self) -> None:
        self._player.stop()

    def cleanup(self) -> None:
        self._player.stop()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    # ── Qt overrides ─────────────────────────────────────────────────────

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._render_pixmap()

    # ── Internals ────────────────────────────────────────────────────────

    def _render_pixmap(self) -> None:
        if not self._current_pixmap or self._current_pixmap.isNull():
            return
        size = self.size()
        if size.isEmpty():
            return
        self._img_label.setPixmap(
            self._current_pixmap.scaled(
                size, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        )

    def _on_media_status_changed(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._player.setPosition(0)
            self._player.play()

    def _purge_old_temps(self) -> None:
        still_locked: list[str] = []
        for p in self._pending_temps:
            try:
                os.remove(p)
            except OSError:
                still_locked.append(p)
        self._pending_temps = still_locked


# ---------------------------------------------------------------------------
# SorterWidget
# ---------------------------------------------------------------------------

class SorterWidget(QWidget):
    """The main sorting UI: status bar, media display, keyboard navigation."""

    done = Signal()

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)

        self._settings = settings
        self._root: Path | None = None
        self._keep_folder: Path | None = None
        self._discard_folder: Path | None = None
        self._remaining: list[Path] = []
        self._current_idx: int = 0
        self._preloader: MediaPreloader | None = None
        self._waiting_for: str | None = None

        # ── Layout ───────────────────────────────────────────────────────
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self._status = QLabel()
        self._status.setAlignment(Qt.AlignCenter)
        self._status.setStyleSheet("font-size: 13px; color: #bbb; padding: 2px;")
        layout.addWidget(self._status)

        self._display = MediaDisplayWidget()
        self._display.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self._display)

        self._hint = QLabel()
        self._hint.setAlignment(Qt.AlignCenter)
        self._hint.setStyleSheet("font-size: 12px; color: #555; padding: 2px;")
        layout.addWidget(self._hint)

        self.setFocusPolicy(Qt.StrongFocus)
        self.update_hint()

    def update_hint(self) -> None:
        """Refresh the key-hint label from current settings."""
        k = self._settings.key_name(self._settings.key_keep)
        d = self._settings.key_name(self._settings.key_discard)
        self._hint.setText(f"← → navigate  ·  {k} keep  ·  {d} discard")

    def apply_playback_speed(self) -> None:
        """Push the current speed setting to the display widget immediately."""
        self._display.set_playback_speed(self._settings.video_playback_speed)

    # ── Public API ───────────────────────────────────────────────────────

    def load(
        self,
        root: Path,
        keep_folder: Path,
        discard_folder: Path,
        files: list[Path],
    ) -> None:
        if self._preloader:
            self._preloader.stop()
            self._preloader.wait()

        self._root = root
        self._keep_folder = keep_folder
        self._discard_folder = discard_folder
        self._remaining = list(files)

        auto_discarded = 0
        if self._settings.auto_discard_mov_for_heic:
            auto_discarded = self._auto_discard_mov_for_heic()

        self._current_idx = 0

        self._preloader = MediaPreloader(self)
        self._preloader.media_ready.connect(self._on_media_ready)
        self._preloader.start()

        self._reprioritize()
        self._show_current()
        if auto_discarded:
            self._status.setText(
                self._status.text() + f"  ·  auto-discarded {auto_discarded} .MOV"
            )
        self.setFocus()

    def cleanup(self) -> None:
        if self._preloader:
            self._preloader.stop()
            self._preloader.wait()
            self._preloader = None
        self._display.cleanup()

    def _auto_discard_mov_for_heic(self) -> int:
        """
        Move .MOV files to _discarded when a same-stem .HEIC/.HEIF exists in
        the same directory.  Returns the number of files moved.
        """
        heic_keys: set[tuple[str, str]] = set()
        for p in self._remaining:
            if p.suffix.lower() in {".heic", ".heif"}:
                heic_keys.add((str(p.parent.resolve()), p.stem.lower()))

        to_remove: list[int] = []
        for i, p in enumerate(self._remaining):
            if p.suffix.lower() == ".mov":
                if (str(p.parent.resolve()), p.stem.lower()) in heic_keys:
                    to_remove.append(i)

        count = 0
        for i in reversed(to_remove):
            p = self._remaining[i]
            try:
                rel = p.relative_to(self._root)
            except ValueError:
                rel = Path(p.name)
            dest = self._discard_folder / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(p), str(dest))
                self._remaining.pop(i)
                count += 1
            except OSError:
                pass
        return count

    # ── Display ──────────────────────────────────────────────────────────

    def _show_current(self) -> None:
        if not self._remaining:
            self._display.show_placeholder("All done!")
            self._status.setText("Complete")
            return

        path = self._remaining[self._current_idx]
        rel = path.relative_to(self._root) if self._root else path
        self._status.setText(
            f"{self._current_idx + 1} / {len(self._remaining)}  ·  {rel}"
        )

        entry = self._preloader.get(path) if self._preloader else None
        if entry is not None:
            self._render(entry)
            self._waiting_for = None
        else:
            self._display.show_placeholder("Loading…")
            self._waiting_for = str(path)

    def _render(self, entry: tuple) -> None:
        if entry[0] == "image":
            _, raw, w, h = entry
            self._display.show_image(raw, w, h)
        else:
            _, raw, suffix = entry
            self._display.show_video(raw, suffix)

    def _on_media_ready(self, path_str: str) -> None:
        if path_str == self._waiting_for:
            entry = self._preloader.get(path_str) if self._preloader else None
            if entry:
                self._render(entry)
                self._waiting_for = None

    # ── Keyboard input ───────────────────────────────────────────────────

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == self._settings.key_keep:
            self._process("keep")
        elif key == self._settings.key_discard:
            self._process("discard")
        elif key == Qt.Key_Left and self._current_idx > 0:
            self._current_idx -= 1
            self._show_current()
        elif key == Qt.Key_Right and self._current_idx < len(self._remaining) - 1:
            self._current_idx += 1
            self._show_current()
        else:
            super().keyPressEvent(event)

    def _process(self, action: str) -> None:
        if not self._remaining:
            return

        path = self._remaining[self._current_idx]
        self._display.stop_media()

        dest_root = self._keep_folder if action == "keep" else self._discard_folder
        try:
            rel = path.relative_to(self._root)
        except ValueError:
            rel = Path(path.name)
        dest = dest_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            shutil.move(str(path), str(dest))
        except OSError as exc:
            self._status.setText(f"Error moving file: {exc}")
            return

        self._remaining.pop(self._current_idx)

        if not self._remaining:
            self._display.show_placeholder("All done! Returning to home…")
            self._status.setText("Complete")
            QTimer.singleShot(1500, self.done.emit)
            return

        self._current_idx = min(self._current_idx, len(self._remaining) - 1)
        self._reprioritize()
        self._show_current()

    def _reprioritize(self) -> None:
        """Rebuild the preloader queue and evict items outside the active window."""
        if not self._preloader or not self._remaining:
            return

        idx = self._current_idx
        forward = self._remaining[idx:]
        bstart = max(0, idx - BACKWARD_WINDOW)
        backward = list(reversed(self._remaining[bstart:idx]))
        ordered = forward + backward

        self._preloader.update_queue(ordered)
        self._preloader.evict({str(p) for p in ordered})


# ---------------------------------------------------------------------------
# KeyCaptureButton
# ---------------------------------------------------------------------------

class KeyCaptureButton(QPushButton):
    """
    A button that enters 'press a key' mode when clicked, captures the next
    key press, and emits *key_captured(int)*.  Arrow keys, Escape and other
    reserved navigation keys are rejected.
    """

    _RESERVED = frozenset({
        Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
        Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter,
        Qt.Key.Key_Tab, Qt.Key.Key_Backtab,
    })
    _MODIFIERS = frozenset({
        Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt,
        Qt.Key.Key_Meta, Qt.Key.Key_AltGr,
    })

    key_captured = Signal(int)

    def __init__(self, key, parent=None):
        super().__init__(parent)
        self._key = key
        self._listening = False
        self.setFixedWidth(120)
        self.setFocusPolicy(Qt.StrongFocus)
        self._apply_normal_style()
        self._refresh_text()
        self.clicked.connect(self._start_listening)

    def set_key(self, key) -> None:
        self._key = key
        if not self._listening:
            self._refresh_text()

    def _start_listening(self) -> None:
        self._listening = True
        self.setText("Press a key…")
        self._apply_listen_style()
        self.setFocus()

    def keyPressEvent(self, event) -> None:
        if not self._listening:
            super().keyPressEvent(event)
            return
        key = event.key()
        if key in self._MODIFIERS or key in self._RESERVED:
            return
        self._listening = False
        self._key = key
        self._refresh_text()
        self._apply_normal_style()
        self.key_captured.emit(int(key))

    def focusOutEvent(self, event) -> None:
        if self._listening:
            self._listening = False
            self._refresh_text()
            self._apply_normal_style()
        super().focusOutEvent(event)

    def _refresh_text(self) -> None:
        self.setText(QKeySequence(self._key).toString() or "?")

    def _apply_normal_style(self) -> None:
        self.setStyleSheet(
            "QPushButton { background:#2a2a2a; color:#ddd; border:1px solid #444;"
            " border-radius:4px; font-size:13px; padding:4px 8px; }"
            "QPushButton:hover { background:#333; }"
        )

    def _apply_listen_style(self) -> None:
        self.setStyleSheet(
            "QPushButton { background:#1a3a5a; color:#7bc; border:1px solid #4af;"
            " border-radius:4px; font-size:13px; padding:4px 8px; }"
        )


# ---------------------------------------------------------------------------
# OptionsDialog
# ---------------------------------------------------------------------------

class OptionsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Options")
        self.setMinimumWidth(360)
        self.setModal(True)
        self.setStyleSheet("""
            QDialog { background: #1e1e1e; }
            QLabel  { background: transparent; color: #ddd; font-size: 13px; }
            QCheckBox { background: transparent; color: #ddd; font-size: 13px; }
            QCheckBox::indicator {
                width: 16px; height: 16px;
                border: 1px solid #666; border-radius: 3px;
                background: #2e2e2e;
            }
            QCheckBox::indicator:hover {
                border-color: #999; background: #383838;
            }
            QCheckBox::indicator:checked {
                background-color: #3a7bd5; border-color: #5a9bf5;
            }
        """)

        self._settings = settings
        # Work on temporaries; commit only on OK.
        self._tmp_keep = settings.key_keep
        self._tmp_discard = settings.key_discard

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(20, 20, 20, 16)

        # ── Key bindings ──────────────────────────────────────────────────
        lbl_bindings = QLabel("Key bindings")
        lbl_bindings.setStyleSheet(
            "font-weight: bold; color: #eee; font-size: 14px; background: transparent;"
        )
        layout.addWidget(lbl_bindings)

        grid = QGridLayout()
        grid.setColumnStretch(0, 1)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)

        grid.addWidget(QLabel("Keep:"), 0, 0)
        self._btn_keep = KeyCaptureButton(settings.key_keep)
        self._btn_keep.key_captured.connect(self._on_keep_captured)
        grid.addWidget(self._btn_keep, 0, 1)

        grid.addWidget(QLabel("Discard:"), 1, 0)
        self._btn_discard = KeyCaptureButton(settings.key_discard)
        self._btn_discard.key_captured.connect(self._on_discard_captured)
        grid.addWidget(self._btn_discard, 1, 1)

        layout.addLayout(grid)

        # ── Behaviour ─────────────────────────────────────────────────────
        lbl_behaviour = QLabel("Behaviour")
        lbl_behaviour.setStyleSheet(
            "font-weight: bold; color: #eee; font-size: 14px;"
            " margin-top: 4px; background: transparent;"
        )
        layout.addWidget(lbl_behaviour)

        self._chk_mov = QCheckBox("Discard MOV when HEIC is present")
        self._chk_mov.setChecked(settings.auto_discard_mov_for_heic)
        self._chk_mov.setToolTip(
            "When opening a folder, automatically move a .MOV file to _discarded\n"
            "if a file with the same name but a .HEIC extension exists in the same\n"
            "directory. Takes effect the next time you open a folder."
        )
        layout.addWidget(self._chk_mov)

        # ── Playback ──────────────────────────────────────────────────────
        lbl_playback = QLabel("Playback")
        lbl_playback.setStyleSheet(
            "font-weight: bold; color: #eee; font-size: 14px;"
            " margin-top: 4px; background: transparent;"
        )
        layout.addWidget(lbl_playback)

        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("Video speed:"))
        self._spin_speed = QDoubleSpinBox()
        self._spin_speed.setRange(1.0, 4.0)
        self._spin_speed.setSingleStep(0.25)
        self._spin_speed.setDecimals(2)
        self._spin_speed.setValue(settings.video_playback_speed)
        self._spin_speed.setSuffix("x")
        self._spin_speed.setFixedWidth(90)
        self._spin_speed.setStyleSheet(
            "QDoubleSpinBox { background:#2a2a2a; color:#ddd; border:1px solid #444;"
            " border-radius:4px; padding:3px 6px; font-size:13px; }"
            "QDoubleSpinBox::up-button, QDoubleSpinBox::down-button"
            " { background:#333; border:none; width:18px; }"
        )
        speed_row.addWidget(self._spin_speed)
        speed_row.addStretch()
        layout.addLayout(speed_row)

        # ── Buttons ───────────────────────────────────────────────────────
        _btn_style = """
            QPushButton {
                background: #2a2a2a; color: #ddd;
                border: 1px solid #444; border-radius: 4px;
                padding: 5px 18px; font-size: 13px;
            }
            QPushButton:hover   { background: #333; }
            QPushButton:default {
                background: #3a7bd5; color: white; border-color: #3a7bd5;
            }
            QPushButton:default:hover { background: #4e8fec; }
        """
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.setStyleSheet(_btn_style)
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ── Conflict resolution ───────────────────────────────────────────────

    def _on_keep_captured(self, key: int) -> None:
        self._tmp_keep = key
        if key == self._tmp_discard:
            fallback = int(Qt.Key.Key_D) if key != int(Qt.Key.Key_D) else int(Qt.Key.Key_X)
            self._tmp_discard = fallback
            self._btn_discard.set_key(fallback)

    def _on_discard_captured(self, key: int) -> None:
        self._tmp_discard = key
        if key == self._tmp_keep:
            fallback = int(Qt.Key.Key_K) if key != int(Qt.Key.Key_K) else int(Qt.Key.Key_Z)
            self._tmp_keep = fallback
            self._btn_keep.set_key(fallback)

    def _apply(self) -> None:
        self._settings.key_keep = self._tmp_keep
        self._settings.key_discard = self._tmp_discard
        self._settings.auto_discard_mov_for_heic = self._chk_mov.isChecked()
        self._settings.video_playback_speed = self._spin_speed.value()
        self.accept()


# ---------------------------------------------------------------------------
# WelcomePage
# ---------------------------------------------------------------------------

class WelcomePage(QWidget):
    """Initial screen shown on startup and after all items are processed."""

    folder_selected = Signal(Path)

    def __init__(self, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(20)

        title = QLabel("PhotoSort")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            "font-size: 38px; font-weight: bold; color: #e8e8e8; letter-spacing: 2px;"
        )
        layout.addWidget(title)

        sub = QLabel("Browse a media folder and keep or discard each file.")
        sub.setAlignment(Qt.AlignCenter)
        sub.setStyleSheet("font-size: 14px; color: #777;")
        layout.addWidget(sub)

        btn = QPushButton("Open Folder")
        btn.setFixedSize(176, 48)
        btn.setStyleSheet("""
            QPushButton {
                background: #3a7bd5; color: white;
                border: none; border-radius: 8px;
                font-size: 15px; font-weight: 600;
            }
            QPushButton:hover   { background: #4e8fec; }
            QPushButton:pressed { background: #2d6ab8; }
        """)
        btn.clicked.connect(self._pick_folder)
        layout.addWidget(btn, alignment=Qt.AlignCenter)

        self._error = QLabel()
        self._error.setAlignment(Qt.AlignCenter)
        self._error.setStyleSheet("color: #d06060; font-size: 12px;")
        layout.addWidget(self._error)

    def _pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select folder to sort")
        if not folder:
            return

        root = Path(folder)
        keep = root / f"_{root.name}"
        discard = root / "_discarded"
        excluded = {keep.resolve(), discard.resolve()}

        # Count unsorted media (excluding the two output dirs, even if they exist).
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            cur = Path(dirpath).resolve()
            dirnames[:] = [
                d for d in dirnames
                if (cur / d).resolve() not in excluded
            ]
            count += sum(
                1 for f in filenames if Path(f).suffix.lower() in MEDIA_EXTS
            )

        if count == 0:
            self._error.setText("No supported media files found in that folder.")
            return

        self._error.clear()
        self.folder_selected.emit(root)


# ---------------------------------------------------------------------------
# PhotoSortApp  (main window)
# ---------------------------------------------------------------------------

class PhotoSortApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PhotoSort")
        self.resize(1200, 800)
        self.setStyleSheet("QMainWindow, QWidget { background: #1a1a1a; }")

        self._settings = AppSettings()

        # ── Central wrapper: persistent top bar + page stack ─────────────
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        top_bar = QWidget()
        top_bar.setFixedHeight(34)
        top_bar.setStyleSheet("background: #141414; border-bottom: 1px solid #222;")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(8, 0, 8, 0)
        top_layout.addStretch()
        opts_btn = QPushButton("⚙  Options")
        opts_btn.setFixedHeight(24)
        opts_btn.setStyleSheet("""
            QPushButton {
                background: #2a2a2a; color: #aaa;
                border: 1px solid #333; border-radius: 4px;
                font-size: 12px; padding: 0 12px;
            }
            QPushButton:hover   { background: #333; color: #eee; }
            QPushButton:pressed { background: #222; }
        """)
        opts_btn.clicked.connect(self._open_options)
        top_layout.addWidget(opts_btn)
        root_layout.addWidget(top_bar)

        self._stack = QStackedWidget()
        root_layout.addWidget(self._stack)

        self._welcome = WelcomePage()
        self._welcome.folder_selected.connect(self._start_sorting)
        self._stack.addWidget(self._welcome)    # index 0

        self._sorter = SorterWidget(self._settings)
        self._sorter.done.connect(self._return_home)
        self._stack.addWidget(self._sorter)     # index 1

    def _open_options(self) -> None:
        dlg = OptionsDialog(self._settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._sorter.update_hint()
            self._sorter.apply_playback_speed()

    def _start_sorting(self, root: Path) -> None:
        keep, discard = setup_folder(root)
        files = scan_media_files(root, {keep, discard})
        if not files:
            return
        self._sorter.load(root, keep, discard, files)
        self._stack.setCurrentIndex(1)
        self._sorter.setFocus()

    def _return_home(self) -> None:
        self._stack.setCurrentIndex(0)

    def closeEvent(self, event) -> None:
        self._sorter.cleanup()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = PhotoSortApp()
    window.show()
    sys.exit(app.exec())
