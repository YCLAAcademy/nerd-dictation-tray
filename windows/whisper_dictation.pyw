# SPDX-License-Identifier: GPL-3.0-or-later
"""
Whisper Dictation: offline voice typing from the system tray, for Windows (also runs on Linux).

Press the hotkey (default Ctrl+Alt+D), speak, and each sentence is typed into the active window
when you pause. Uses the Whisper engine from ``nerd_dictation_whisper.py`` (in the folder above).

    pythonw whisper_dictation.pyw            # tray app
    python whisper_dictation.pyw --console   # no tray: dictate until Enter is pressed
"""

import argparse
import gc
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time

APP_NAME = "Whisper Dictation"
HERE = os.path.dirname(os.path.realpath(__file__))
CONFIG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~/.config"), "whisper-dictation")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
LOG_FILE = os.path.join(CONFIG_DIR, "log.txt")

# `pythonw` has no console: keep messages (including the engine's) in a log file instead.
if sys.stderr is None or sys.stdout is None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    _log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    sys.stderr = sys.stderr or _log
    sys.stdout = sys.stdout or _log

# The Whisper engine is shared with nerd-dictation, in the repository root.
sys.path.insert(0, os.path.dirname(HERE))
import nerd_dictation_whisper as engine  # noqa: E402

SAMPLE_RATE = engine.WHISPER_SAMPLE_RATE
# Any free local port, held only to stop a second copy of the app running.
SINGLE_INSTANCE_PORT = 47321

DEFAULT_CONFIG = {
    "hotkey": "<ctrl>+<alt>+d",
    "model": "auto",
    "device": "auto",
    "language": "en",
    "pause": 0.6,
    "keep_model_loaded": False,
    "ignore_words": "the, a, uh, um, huh, hmm",
    "input_device": "",
    "sounds": True,
}

CONFIG_HELP = {
    "hotkey": "Starts and stops dictation, e.g. <ctrl>+<alt>+d or <ctrl>+<shift>+<space>.",
    "model": "auto (large-v3-turbo with an NVIDIA GPU, otherwise small), or large-v3-turbo, "
             "distil-large-v3 (English only), medium, small, base, tiny.",
    "device": "auto (NVIDIA GPU when available), cuda or cpu. AMD and Intel GPUs aren't supported, they use the CPU.",
    "language": "Language code like en, hi, es, or empty to detect it (slower).",
    "pause": "Seconds of silence that end a sentence, which is then typed.",
    "keep_model_loaded": "true keeps the model in memory between dictations: instant start, but uses memory.",
    "ignore_words": "A sentence made up only of these words is dropped (noise).",
    "input_device": "Part of the microphone's name, or empty for the default microphone.",
    "sounds": "Beep when dictation is ready and when it stops.",
}


# -----------------------------------------------------------------------------
# Settings


def load_config():
    config = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            config.update({k: v for k, v in json.load(fh).items() if k in DEFAULT_CONFIG})
    except (OSError, ValueError) as ex:
        if os.path.exists(CONFIG_FILE):
            sys.stderr.write("Can't read {:s} ({:s}), using the defaults.\n".format(CONFIG_FILE, str(ex)))
    return config


def save_config(config):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        json.dump(dict(config, _help=CONFIG_HELP), fh, indent=2)


def config_mtime():
    try:
        return os.path.getmtime(CONFIG_FILE)
    except OSError:
        return None


def resolve_model(config):
    if config["model"] != "auto":
        return config["model"]
    try:
        import ctranslate2

        has_gpu = config["device"] != "cpu" and ctranslate2.get_cuda_device_count() > 0
    except Exception:
        has_gpu = False
    return "large-v3-turbo" if has_gpu else "small"


def open_in_editor(path):
    if sys.platform == "win32":
        subprocess.Popen(["notepad.exe", path])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-t", path])
    else:
        subprocess.Popen(["xdg-open", path])


# -----------------------------------------------------------------------------
# Sounds


def play_sound(started):
    def play():
        tones = (660, 880) if started else (880, 660)
        if sys.platform == "win32":
            import winsound

            for tone in tones:
                winsound.Beep(tone, 90)
            return
        path = "/usr/share/sounds/freedesktop/stereo/{:s}.oga".format("device-added" if started else "device-removed")
        if os.path.exists(path):
            subprocess.call(["paplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    threading.Thread(target=play, daemon=True).start()


# -----------------------------------------------------------------------------
# Dictation


def is_noise(text, ignore_words):
    words = {w.strip(".,!?;:\"'").lower() for w in text.split()}
    ignore = {w.strip().lower() for w in ignore_words.replace(",", " ").split()}
    return bool(ignore) and words <= ignore


def find_input_device(name):
    """Return the index of the first microphone whose name contains ``name``, otherwise None (the default)."""
    if not name:
        return None
    import sounddevice

    for index, device in enumerate(sounddevice.query_devices()):
        if device["max_input_channels"] > 0 and name.lower() in device["name"].lower():
            return index
    sys.stderr.write("No microphone named {!r}, using the default.\n".format(name))
    return None


class Dictation:
    """Records the microphone and types what's said, on a background thread."""

    def __init__(self, type_text, on_state, on_error):
        self.type_text = type_text
        self.on_state = on_state
        self.on_error = on_error
        self.state = "idle"  # idle, loading, listening, stopping
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._rec = None
        self._rec_key = None

    def _set_state(self, state):
        self.state = state
        self.on_state(state)

    def start(self, config):
        if self.state != "idle":
            return
        self._stop.clear()
        self._set_state("loading")
        threading.Thread(target=self._run, args=(dict(config),), daemon=True).start()

    def stop(self):
        if self.state in ("loading", "listening"):
            self._stop.set()
            self._set_state("stopping")

    def toggle(self, config):
        if self.state == "idle":
            self.start(config)
        else:
            self.stop()

    def preload(self, config):
        """Load the model in the background ("keep model loaded")."""
        threading.Thread(target=self._recognizer, args=(dict(config),), daemon=True).start()

    def unload(self):
        with self._lock:
            self._rec = self._rec_key = None
        gc.collect()

    def _recognizer(self, config):
        model = resolve_model(config)
        key = (model, config["device"], config["language"], float(config["pause"]))
        with self._lock:
            if self._rec is not None and self._rec_key == key:
                self._rec.Reset()
                return self._rec
            self._rec = self._rec_key = None
            gc.collect()
            options = dict(model=model, language=config["language"], pause=float(config["pause"]), verbose=1)
            try:
                rec = engine.WhisperRecognizer(SAMPLE_RATE, device=config["device"], **options)
            except Exception as ex:
                if config["device"] != "auto":
                    raise
                # e.g. NVIDIA libraries missing: the CPU always works.
                sys.stderr.write("Whisper failed on the GPU ({:s}), using the CPU.\n".format(str(ex)))
                rec = engine.WhisperRecognizer(SAMPLE_RATE, device="cpu", **options)
            self._rec, self._rec_key = rec, key
            return rec

    def _run(self, config):
        import sounddevice

        audio = queue.Queue()
        typed_any = False

        def on_audio(data, _frames, _time, _status):
            audio.put(bytes(data))

        def emit(json_text):
            nonlocal typed_any
            text = json.loads(json_text)["text"]
            if not text:
                return
            if is_noise(text, config["ignore_words"]):
                sys.stderr.write("Ignored: {:s}\n".format(text))
                return
            self.type_text((" " if typed_any else "") + text)
            typed_any = True

        try:
            stream = sounddevice.RawInputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=SAMPLE_RATE // 10,
                device=find_input_device(config["input_device"]), callback=on_audio,
            )
            # Record while the model loads, so nothing said is lost.
            stream.start()
            try:
                rec = self._recognizer(config)
                if not self._stop.is_set():
                    self._set_state("listening")
                    if config["sounds"]:
                        play_sound(True)
                while not self._stop.is_set():
                    try:
                        data = audio.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if rec.AcceptWaveform(data):
                        emit(rec.FinalResult())
            finally:
                stream.stop()
                stream.close()
            # Finish what was said before stopping.
            while not audio.empty():
                if rec.AcceptWaveform(audio.get_nowait()):
                    emit(rec.FinalResult())
            emit(rec.FinalResult())
        except Exception as ex:
            sys.stderr.write("Dictation failed: {:s}\n".format(str(ex)))
            self.on_error(str(ex))
        finally:
            if not config["keep_model_loaded"]:
                # Free the (GPU) memory until the next dictation.
                self.unload()
            if config["sounds"]:
                play_sound(False)
            self._set_state("idle")


# -----------------------------------------------------------------------------
# Tray app

STATE_COLORS = {"idle": (128, 128, 128), "loading": (230, 159, 0), "listening": (46, 173, 75), "stopping": (230, 159, 0)}
STATE_TEXT = {
    "idle": "stopped ({hotkey} to start)",
    "loading": "loading the speech model…",
    "listening": "listening ({hotkey} to stop)",
    "stopping": "typing the last words…",
}


def make_icon(state):
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((2, 2, 62, 62), fill=STATE_COLORS[state])
    white = (255, 255, 255)
    draw.rounded_rectangle((24, 12, 40, 38), radius=8, fill=white)  # Microphone.
    draw.arc((17, 20, 47, 46), start=0, end=180, fill=white, width=4)
    draw.line((32, 46, 32, 53), fill=white, width=4)
    draw.line((23, 53, 41, 53), fill=white, width=4)
    return image


def hotkey_label(hotkey):
    return "+".join(part.strip("<>").capitalize() for part in hotkey.split("+"))


class TrayApp:
    def __init__(self):
        import pystray
        from pynput import keyboard

        self.pystray = pystray
        self.keyboard = keyboard
        self.typer = keyboard.Controller()
        self.config = load_config()
        if not os.path.exists(CONFIG_FILE):
            save_config(self.config)
        self.config_mtime = config_mtime()
        self.hotkeys = None
        self.dictation = Dictation(self.typer.type, self.on_state, self.on_error)

        menu = pystray.Menu(
            pystray.MenuItem(
                lambda _item: "Stop dictation" if self.dictation.state != "idle" else "Start dictation",
                lambda: self.dictation.toggle(self.config), default=True,
            ),
            pystray.MenuItem("Settings…", lambda: open_in_editor(CONFIG_FILE)),
            pystray.MenuItem("Open log", lambda: open_in_editor(LOG_FILE)),
            pystray.MenuItem("Exit", self.quit),
        )
        self.icon = pystray.Icon("whisper-dictation", make_icon("idle"), APP_NAME, menu)
        self.on_state("idle")

    def run(self):
        self.bind_hotkey()
        if self.config["keep_model_loaded"]:
            self.dictation.preload(self.config)
        threading.Thread(target=self.watch_config, daemon=True).start()
        self.icon.run()

    def on_state(self, state):
        self.icon.icon = make_icon(state)
        self.icon.title = "{:s}: {:s}".format(APP_NAME, STATE_TEXT[state].format(hotkey=hotkey_label(self.config["hotkey"])))
        self.icon.update_menu()

    def on_error(self, message):
        try:
            self.icon.notify(message, APP_NAME)
        except Exception:
            pass

    def bind_hotkey(self):
        if self.hotkeys is not None:
            self.hotkeys.stop()
        try:
            self.hotkeys = self.keyboard.GlobalHotKeys({self.config["hotkey"]: lambda: self.dictation.toggle(self.config)})
            self.hotkeys.start()
        except Exception as ex:
            self.hotkeys = None
            self.on_error("Can't use the hotkey {!r}: {:s}".format(self.config["hotkey"], str(ex)))

    def watch_config(self):
        """Apply settings when config.json is saved."""
        while True:
            time.sleep(1.0)
            mtime = config_mtime()
            if mtime == self.config_mtime:
                continue
            self.config_mtime = mtime
            old, self.config = self.config, load_config()
            if self.config["hotkey"] != old["hotkey"]:
                self.bind_hotkey()
            if self.dictation.state == "idle":
                if self.config["keep_model_loaded"]:
                    self.dictation.preload(self.config)
                else:
                    self.dictation.unload()
            self.on_state(self.dictation.state)

    def quit(self):
        self.dictation.stop()
        if self.hotkeys is not None:
            self.hotkeys.stop()
        self.icon.stop()


# -----------------------------------------------------------------------------
# Console mode (no tray)


def run_console(print_only):
    config = load_config()
    if print_only:
        type_text = lambda text: print(text, end="", flush=True)  # noqa: E731
    else:
        from pynput import keyboard

        type_text = keyboard.Controller().type
    done = threading.Event()

    def on_state(state):
        sys.stderr.write("[{:s}]\n".format(state))
        if state == "listening":
            sys.stderr.write("Speak now, press Enter to stop.\n")
        elif state == "idle":
            done.set()

    dictation = Dictation(type_text, on_state, lambda message: None)
    dictation.start(config)
    try:
        input()
    except EOFError:
        pass
    dictation.stop()
    done.wait()
    print()


def main():
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--console", action="store_true", help="No tray icon: dictate until Enter is pressed.")
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="With --console, print the text instead of typing it.")
    parser.add_argument("--list-mics", action="store_true", help="List the microphones and exit.")
    args = parser.parse_args()

    if args.list_mics:
        import sounddevice

        print(sounddevice.query_devices())
        return
    if args.console:
        run_console(args.print_only)
    else:
        lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            lock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        except OSError:
            sys.stderr.write("{:s} is already running.\n".format(APP_NAME))
            return
        TrayApp().run()
    sys.stdout.flush()
    sys.stderr.flush()
    # Skip interpreter shutdown, the speech engine's threads can abort it ("terminate called...").
    os._exit(0)


if __name__ == "__main__":
    main()
