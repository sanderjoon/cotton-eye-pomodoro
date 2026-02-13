import sys
import os
import json
import random
import ctypes
import winsound
import time
import threading
from ctypes import wintypes

from PySide6.QtCore import Qt, QTimer, QSize, QUrl, QPropertyAnimation
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QWidget, QDialog, QLineEdit, QDialogButtonBox,
    QSystemTrayIcon, QMenu, QGraphicsOpacityEffect
)
from PySide6.QtGui import QIcon, QAction, QPixmap, QFont, QColor, QPainter
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput

# ---------- Windows API setup (ctypes, no external deps) ----------

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi

GetForegroundWindow = user32.GetForegroundWindow
GetWindowTextLengthW = user32.GetWindowTextLengthW
GetWindowTextW = user32.GetWindowTextW
GetWindowThreadProcessId = user32.GetWindowThreadProcessId
OpenProcess = kernel32.OpenProcess
CloseHandle = kernel32.CloseHandle
GetModuleBaseNameW = psapi.GetModuleBaseNameW

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MAX_PATH = 260


def get_active_hwnd():
    hwnd = GetForegroundWindow()
    return hwnd or None


def get_active_window_title():
    hwnd = get_active_hwnd()
    if not hwnd:
        return ""
    length = GetWindowTextLengthW(hwnd)
    if length == 0:
        # Could be no title or error; still try
        buf = ctypes.create_unicode_buffer(512)
        GetWindowTextW(hwnd, buf, 512)
        return buf.value
    buf = ctypes.create_unicode_buffer(length + 1)
    GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def get_active_process_name():
    hwnd = get_active_hwnd()
    if not hwnd:
        return ""
    pid = wintypes.DWORD()
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    h_process = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid.value)
    if not h_process:
        return ""
    try:
        exe_name = (wintypes.WCHAR * MAX_PATH)()
        if GetModuleBaseNameW(h_process, None, exe_name, MAX_PATH) == 0:
            return ""
        return exe_name.value
    finally:
        CloseHandle(h_process)


# ---------- Settings storage ----------

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pomodoro_settings.json")

DEFAULT_SETTINGS = {
    "right_apps": ["blender", "houdini"],
    "work_minutes": 25,
    "break_minutes": 5
}


def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return DEFAULT_SETTINGS.copy()
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return DEFAULT_SETTINGS.copy()

    for k, v in DEFAULT_SETTINGS.items():
        if k not in data:
            data[k] = v
    return data


def save_settings(settings):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


# ---------- Garden logic ----------

PLANT = "🌱"
FLOWERS = ["🌸", "💮", "🪷", "🏵️", "🌹", "🥀", "🌺", "🌻", "🌼", "🌷", "🪻", "🐵", "🐰", "🦥", "🥚", "🐸", "🐼"]


# ---------- Settings dialog ----------

class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.settings = settings

        self.apps_edit = QLineEdit(", ".join(self.settings["right_apps"]))
        self.work_edit = QLineEdit(str(self.settings["work_minutes"]))
        self.break_edit = QLineEdit(str(self.settings["break_minutes"]))

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addWidget(QLabel("Right apps (title or process contains):"))
        layout.addWidget(self.apps_edit)
        layout.addWidget(QLabel("Work minutes:"))
        layout.addWidget(self.work_edit)
        layout.addWidget(QLabel("Break minutes:"))
        layout.addWidget(self.break_edit)
        layout.addWidget(buttons)

        self.setLayout(layout)

    def get_settings(self):
        apps_raw = self.apps_edit.text()
        right_apps = [a.strip() for a in apps_raw.split(",") if a.strip()]

        try:
            work_minutes = int(self.work_edit.text())
        except ValueError:
            work_minutes = DEFAULT_SETTINGS["work_minutes"]

        try:
            break_minutes = int(self.break_edit.text())
        except ValueError:
            break_minutes = DEFAULT_SETTINGS["break_minutes"]

        self.settings["right_apps"] = right_apps or DEFAULT_SETTINGS["right_apps"]
        self.settings["work_minutes"] = max(1, work_minutes)
        self.settings["break_minutes"] = max(1, break_minutes)
        return self.settings


# ---------- Main window ----------

class PomodoroWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.settings = load_settings()

        self.setWindowTitle("Cotton Eye Pomodoro")
        self.resize(320, 210)

        self.is_running = False
        self.is_on_break = False
        self.is_wrong_app = False
        self.is_stopped_counting = False
        self.break_elapsed_seconds = 0
        self.stopped_elapsed_seconds = 0
        self.idle_elapsed_seconds = 0
        self.use_minute_format = False
        self.wrong_app_start_time = None
        self.wrong_app_notified_time = None
        self.break_overtime_notified = False
        self.last_overtime_speech_time = None
        self.idle_annoying_song_playing = False

        self.work_total_seconds = self.settings["work_minutes"] * 60
        self.break_total_seconds = self.settings["break_minutes"] * 60
        self.remaining_seconds = self.work_total_seconds

        # Garden: always 5 slots
        self.session_count = 0
        self.garden = [PLANT] * 5

        self.label_plants = QLabel("".join(self.garden))
        self.label_plants.setAlignment(Qt.AlignCenter)
        self.label_plants.setStyleSheet("font-size: 24px;")

        self.label_time = QLabel(self.format_time(self.remaining_seconds))
        self.label_time.setAlignment(Qt.AlignCenter)
        self.label_time.setStyleSheet("font-size: 32px; color: red;")

        self.label_status = QLabel("Idle")
        self.label_status.setAlignment(Qt.AlignCenter)

        # Buttons side by side
        self.btn_start = QPushButton("Start")
        self.btn_start.clicked.connect(self.toggle_start)

        self.btn_settings = QPushButton("Settings")
        self.btn_settings.clicked.connect(self.open_settings)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_settings)

        layout = QVBoxLayout()
        layout.addWidget(self.label_plants)
        layout.addWidget(self.label_time)
        layout.addWidget(self.label_status)
        layout.addLayout(btn_row)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.on_tick)

        self.tray_icon = QSystemTrayIcon(self)
        # Set emoji icon for window and tray
        emoji_icon = self.create_emoji_icon("🌱")
        self.setWindowIcon(emoji_icon)
        self.tray_icon.setIcon(emoji_icon)
        self.tray_icon.setVisible(True)

        tray_menu = QMenu()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.instance().quit)
        tray_menu.addAction(quit_action)
        self.tray_icon.setContextMenu(tray_menu)

        # Initialize media player for MP3 playback
        self.media_player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.media_player.setAudioOutput(self.audio_output)
        
        # Initialize separate media player for annoying songs (looped)
        self.annoying_media_player = QMediaPlayer(self)
        self.annoying_audio_output = QAudioOutput(self)
        self.annoying_media_player.setAudioOutput(self.annoying_audio_output)
        self.annoying_media_player.mediaStatusChanged.connect(self.on_annoying_song_finished)
        self.annoying_song_playing = False

        # Start the idle timer immediately
        self.timer.start()
        
        # Start playing annoying song on app startup (idle mode)
        self.play_annoying_song_loop()
        self.idle_annoying_song_playing = True

    # ----- Core helpers -----

    def play_sound(self, event_type):
        """Play sound for different events."""
        try:
            if event_type == "work_complete":
                # Double beep for work session complete
                winsound.Beep(1000, 200)
                time.sleep(0.1)
                winsound.Beep(1000, 200)
            elif event_type == "break_finished":
                # Triple ascending beep for break finished
                winsound.Beep(800, 150)
                time.sleep(0.05)
                winsound.Beep(1000, 150)
                time.sleep(0.05)
                winsound.Beep(1200, 150)
            elif event_type == "break_overtime":
                # Fun playful beeps for break overtime
                winsound.Beep(1200, 100)
                time.sleep(0.1)
                winsound.Beep(900, 100)
                time.sleep(0.1)
                winsound.Beep(1200, 100)
            elif event_type == "wrong_app":
                # Low warning beep for wrong app
                winsound.Beep(600, 150)
        except Exception:
            pass  # Silently handle any audio errors



    def play_annoying_song_loop(self):
        """Play a random annoying song from the annoying_songs_mp3 folder on loop."""
        try:
            # Get the folder containing this script
            script_dir = os.path.dirname(os.path.abspath(__file__))
            songs_folder = os.path.join(script_dir, "annoying_songs_mp3")
            
            # Check if folder exists and has MP3 files
            if not os.path.exists(songs_folder):
                return  # Folder doesn't exist, silently skip
            
            # Get all MP3 files
            mp3_files = [f for f in os.listdir(songs_folder) if f.lower().endswith('.mp3')]
            
            if not mp3_files:
                return  # No MP3 files found, silently skip
            
            # Pick a random MP3 file
            random_mp3 = random.choice(mp3_files)
            mp3_path = os.path.join(songs_folder, random_mp3)
            
            # Play the MP3 file using QMediaPlayer (in-app playback) - will loop via mediaStatusChanged
            self.annoying_media_player.setSource(QUrl.fromLocalFile(mp3_path))
            self.annoying_media_player.play()
            self.annoying_song_playing = True
        except Exception:
            pass  # Silently handle any errors

    def on_annoying_song_finished(self, status):
        """Restart annoying song when it finishes (loop behavior)."""
        from PySide6.QtMultimedia import QMediaPlayer as MP
        if self.annoying_song_playing and status == MP.EndOfMedia:
            self.annoying_media_player.play()

    def stop_annoying_song(self):
        """Stop the annoying song playback."""
        self.annoying_media_player.stop()
        self.annoying_song_playing = False

    def create_emoji_icon(self, emoji):
        """Create a QIcon from an emoji character."""
        pixmap = QPixmap(64, 64)
        pixmap.fill(QColor(0, 0, 0, 0))  # Transparent background
        painter = QPainter(pixmap)
        font = QFont()
        font.setPointSize(48)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignCenter, emoji)
        painter.end()
        return QIcon(pixmap)

    def format_time(self, seconds):
        seconds = max(0, int(seconds))
        m, s = divmod(seconds, 60)
        return f"{m:02d}:{s:02d}"

    def format_duration_minutes(self, seconds):
        """Format time as minutes only (e.g., '15 min', '1 min')."""
        seconds = max(0, int(seconds))
        minutes = (seconds + 59) // 60  # Round up to next minute
        return f"{minutes} min"

    def format_negative_time(self, total_seconds, elapsed_seconds):
        """Format time as negative (elapsed) for break display."""
        negative_seconds = elapsed_seconds - total_seconds
        m, s = divmod(int(-negative_seconds), 60)
        return f"-{m:02d}:{s:02d}"

    def animate_fade_text(self, label, new_text):
        """Fade text out and in (1 second total) when text changes."""
        # Create or get opacity effect
        effect = label.graphicsEffect()
        if not effect or not isinstance(effect, QGraphicsOpacityEffect):
            effect = QGraphicsOpacityEffect()
            label.setGraphicsEffect(effect)
        
        # Store the new text to use in callback
        self._pending_text = new_text
        
        # Animation: fade out (500ms), change text, fade in (500ms)
        fade_out = QPropertyAnimation(effect, b"opacity")
        fade_out.setDuration(500)
        fade_out.setStartValue(1.0)
        fade_out.setEndValue(0.0)
        
        def on_fade_out():
            # Change text while faded out
            label.setText(self._pending_text)
            fade_in = QPropertyAnimation(effect, b"opacity")
            fade_in.setDuration(500)
            fade_in.setStartValue(0.0)
            fade_in.setEndValue(1.0)
            fade_in.start(QPropertyAnimation.DeleteWhenStopped)
        
        fade_out.finished.connect(on_fade_out)
        fade_out.start(QPropertyAnimation.DeleteWhenStopped)

    def toggle_start(self):
        """Start/stop/resume the timer. Click when stopped to go to break (skip work)."""
        if self.is_stopped_counting:
            # When stopped, clicking starts the break (skip to break)
            self.is_stopped_counting = False
            self.stopped_elapsed_seconds = 0
            self.idle_elapsed_seconds = 0
            self.break_elapsed_seconds = 0
            self.is_on_break = True
            self.break_overtime_notified = False
            self.label_status.setText("Break")
            self.btn_start.setText("Stop")
            if not self.timer.isActive():
                self.timer.start()
        elif not self.is_running and not self.is_on_break:
            # Start new session
            self.start_pomodoro()
        else:
            # Stop current session (work or break)
            if self.is_running:
                self.stop_work()
            elif self.is_on_break:
                self.stop_all()

    def start_pomodoro(self):
        self.is_running = True
        self.is_on_break = False
        self.is_stopped_counting = False
        self.stopped_elapsed_seconds = 0
        self.idle_elapsed_seconds = 0
        self.remaining_seconds = self.work_total_seconds
        self.label_status.setText("Working…")
        self.label_time.setText(self.format_duration_minutes(self.remaining_seconds))
        self.btn_start.setText("Stop")
        # Stop idle annoying song when starting work session
        if self.idle_annoying_song_playing:
            self.stop_annoying_song()
            self.idle_annoying_song_playing = False
        self.timer.start()

    def stop_work(self):
        """Stop the current work session and start counting elapsed time."""
        self.is_running = False
        self.is_wrong_app = False
        self.wrong_app_start_time = None
        self.wrong_app_notified_time = None
        self.is_stopped_counting = True
        self.stopped_elapsed_seconds = 0
        self.idle_elapsed_seconds = 0
        self.label_status.setText("Stopped")
        self.btn_start.setText("Resume")
        # Don't stop the timer - keep it running to count elapsed time

    def stop_all(self):
        """Fully stop everything (from break or stopped counting state)."""
        self.is_running = False
        self.is_on_break = False
        self.is_wrong_app = False
        self.is_stopped_counting = False
        self.wrong_app_start_time = None
        self.wrong_app_notified_time = None
        self.break_overtime_notified = False
        self.last_overtime_speech_time = None
        self.stopped_elapsed_seconds = 0
        self.idle_elapsed_seconds = 0
        self.timer.stop()
        self.label_status.setText("Idle")
        self.btn_start.setText("Start")
        self.label_time.setStyleSheet("font-size: 32px; color: red;")
        self.remaining_seconds = self.work_total_seconds
        self.label_time.setText(self.format_time(self.remaining_seconds))
        # Restart idle annoying song when returning to idle
        self.play_annoying_song_loop()
        self.idle_annoying_song_playing = True

    def start_break(self):
        self.is_on_break = True
        self.is_stopped_counting = False
        self.stopped_elapsed_seconds = 0
        self.idle_elapsed_seconds = 0
        self.break_elapsed_seconds = 0
        self.break_overtime_notified = False
        self.last_overtime_speech_time = None
        self.remaining_seconds = self.break_total_seconds
        self.label_status.setText("Break")
        # Stop idle annoying song when starting break session
        if self.idle_annoying_song_playing:
            self.stop_annoying_song()
            self.idle_annoying_song_playing = False
        if not self.timer.isActive():
            self.timer.start()

    def on_tick(self):
        # Always update idle timer if not in any active state
        if not self.is_running and not self.is_on_break and not self.is_stopped_counting:
            self.idle_elapsed_seconds += 1
            time_display = self.format_time(self.idle_elapsed_seconds)
            self.label_time.setText(time_display)
            self.label_time.setStyleSheet("font-size: 32px; color: red;")
            self.label_status.setText("Idle")
            return

        # Handle stopped work session counting elapsed time
        if self.is_stopped_counting:
            self.stopped_elapsed_seconds += 1
            time_display = self.format_time(self.stopped_elapsed_seconds)
            self.label_time.setText(time_display)
            self.label_time.setStyleSheet("font-size: 32px; color: red;")
            self.label_status.setText("Stopped")
            return

        if self.is_on_break:
            self.break_elapsed_seconds += 1
            remaining = self.break_total_seconds - self.break_elapsed_seconds
            
            if remaining >= 0:
                # Normal break time - show countdown in white
                time_display = self.format_time(remaining)
                self.label_time.setText(time_display)
                self.label_time.setStyleSheet("font-size: 32px; color: white;")
                self.label_status.setText("Break")
            else:
                # Break overtime - show overtime in red
                if not self.break_overtime_notified:
                    # First time going into overtime
                    self.break_overtime_notified = True
                    self.last_overtime_speech_time = time.time()
                    self.play_sound("break_overtime")
                    self.show_notification("Break Time's Up!", "You're on overtime 😄")
                    # Start playing annoying songs on break overtime
                    self.play_annoying_song_loop()
                
                overtime = self.break_elapsed_seconds - self.break_total_seconds
                time_display = self.format_time(overtime)
                self.label_time.setText(time_display)
                self.label_time.setStyleSheet("font-size: 32px; color: red;")
                self.label_status.setText("Break Overtime")
            return

        # Work phase
        active_title = get_active_window_title()
        proc_name = get_active_process_name()
        right_apps = self.settings["right_apps"]

        # Debug (keep for a bit while testing)
        # print(f"[DEBUG] title={active_title!r} proc={proc_name!r} right={right_apps}")

        is_right = any(
            frag.lower() in (active_title or "").lower()
            or frag.lower() in (proc_name or "").lower()
            for frag in right_apps
        )

        current_time = time.time()

        if is_right:
            self.remaining_seconds -= 1
            self.is_wrong_app = False
            self.wrong_app_start_time = None
            self.wrong_app_notified_time = None
            self.stop_annoying_song()  # Stop annoying song when back on right app
            self.label_status.setText("Working…")
            # White color when working correctly
            self.label_time.setStyleSheet("font-size: 32px; color: white;")
        else:
            self.remaining_seconds += 1
            self.remaining_seconds = min(self.remaining_seconds, self.work_total_seconds)
            self.label_status.setText("Wrong app – undoing progress")
            self.is_wrong_app = True
            
            # Track wrong app start time
            if self.wrong_app_start_time is None:
                self.wrong_app_start_time = current_time
                # Start playing annoying songs immediately on wrong app detection
                self.play_annoying_song_loop()
            
            # Show notification with 5-second initial delay, then every 10 seconds
            time_on_wrong_app = current_time - self.wrong_app_start_time
            should_notify = False
            
            if time_on_wrong_app >= 5.0 and self.wrong_app_notified_time is None:
                # First notification after 5 seconds
                should_notify = True
                self.wrong_app_notified_time = current_time
            elif self.wrong_app_notified_time is not None and (current_time - self.wrong_app_notified_time) >= 10.0:
                # Repeat notification every 10 seconds
                should_notify = True
                self.wrong_app_notified_time = current_time
            
            if should_notify:
                active_window = active_title if active_title else proc_name
                self.play_sound("wrong_app")
                self.show_notification("Wrong App Detected", f"You're on: {active_window}")
            
            # Apply red and bold styling when on wrong app
            self.label_time.setStyleSheet("font-size: 32px; color: red; font-weight: bold;")

        if self.remaining_seconds <= 0:
            self.is_running = False
            self.timer.stop()
            self.label_status.setText("Pomodoro complete")
            self.btn_start.setText("Start")
            self.play_sound("work_complete")
            self.show_notification("Pomodoro complete", "Take a break!")
            self.update_garden_after_session()
            self.start_break()

        # Display format: minute-based during correct work, MM:SS during wrong app
        if is_right:
            time_display = self.format_duration_minutes(self.remaining_seconds)
        else:
            time_display = self.format_time(self.remaining_seconds)
        
        # Update display with fade animation when text changes
        current_text = self.label_time.text()
        if current_text != time_display:
            # Check if animation is already running to avoid conflicts
            effect = self.label_time.graphicsEffect()
            if effect and isinstance(effect, QGraphicsOpacityEffect):
                # Only animate if effect exists, otherwise just set text directly
                self.animate_fade_text(self.label_time, time_display)
            else:
                self.label_time.setText(time_display)

    def update_garden_after_session(self):
        self.session_count += 1

        # From session 1 onward: replace one seedling; when none left, mutate a flower
        seedling_positions = [i for i, x in enumerate(self.garden) if x == PLANT]

        if seedling_positions:
            pos = random.choice(seedling_positions)
            self.garden[pos] = random.choice(FLOWERS)
        else:
            pos = random.randrange(5)
            self.garden[pos] = random.choice(FLOWERS)

        self.label_plants.setText("".join(self.garden))

    def show_notification(self, title, message):
        self.tray_icon.showMessage(title, message, QSystemTrayIcon.Information, 5000)

    def open_settings(self):
        dlg = SettingsDialog(self.settings.copy(), self)
        if dlg.exec() == QDialog.Accepted:
            self.settings = dlg.get_settings()
            save_settings(self.settings)
            self.work_total_seconds = self.settings["work_minutes"] * 60
            self.break_total_seconds = self.settings["break_minutes"] * 60
            
            # Apply settings immediately, even if a session is running
            if self.is_running:
                # Update remaining seconds for current work session
                self.remaining_seconds = self.work_total_seconds
                self.label_time.setText(self.format_time(self.remaining_seconds))
            elif self.is_on_break:
                # Update remaining seconds for current break session
                self.break_elapsed_seconds = 0
                self.label_time.setText(self.format_time(self.break_total_seconds))
            else:
                # Update idle display
                self.remaining_seconds = self.work_total_seconds
                self.label_time.setText(self.format_time(self.remaining_seconds))


# ---------- Entry point ----------

def main():
    app = QApplication(sys.argv)
    win = PomodoroWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
