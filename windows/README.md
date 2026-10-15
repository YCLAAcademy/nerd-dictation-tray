# Whisper Dictation (Windows)

Offline voice typing from the system tray, using the same Whisper engine as nerd-dictation
(`nerd_dictation_whisper.py` in the folder above, keep both together).
Press **Ctrl+Alt+D**, speak, and each sentence is typed into the active window when you pause,
with punctuation and capitals. Press Ctrl+Alt+D again to stop.

It's written for Windows and also runs on Linux (X11). nerd-dictation and its tray app are Linux only.

## Install

In PowerShell or Command Prompt:

```bat
winget install Git.Git Python.Python.3.12
git clone -b whisper-engine https://github.com/YCLAAcademy/nerd-dictation-tray.git
cd nerd-dictation-tray
py -m pip install -r windows\requirements.txt
```

(Open a new terminal after `winget install` so `git` and `py` are found.)

## Test

```bat
py windows\whisper_dictation.pyw --list-mics
py windows\whisper_dictation.pyw --console --print
```

`--console --print` shows what it hears in the terminal instead of typing it: wait for
"Speak now", speak a sentence, pause, and press Enter to stop. The first run downloads the model
(`small` is ~480 MB, `large-v3-turbo` 1.6 GB), after that it works offline.

## Run

```bat
pyw windows\whisper_dictation.pyw
```

A grey microphone appears in the tray (it may be under the **^** arrow). Its colour shows the state:
grey stopped, orange loading the model, green listening. Left-click it, or press Ctrl+Alt+D, to start
and stop. Right-click for Settings, the log and Exit.

To start it when you log in: press Win+R, type `shell:startup`, and create a shortcut there to
`pyw.exe "C:\path\to\nerd-dictation-tray\windows\whisper_dictation.pyw"`.

## GPU or CPU

| Laptop | Model (`auto`) | Speed |
|---|---|---|
| NVIDIA GPU | `large-v3-turbo` | ~0.5 s per sentence |
| AMD or Intel graphics (CPU only) | `small` | about a second per sentence, depends on the CPU |

faster-whisper only uses NVIDIA GPUs, AMD and Intel GPUs aren't supported so the CPU is used.
Bigger models on a CPU are slow (`large-v3-turbo` took ~9 s per sentence on a laptop CPU),
use `small`, `base` or `distil-large-v3` (English only). For NVIDIA on Windows, the CUDA 12 cuBLAS and
cuDNN 9 libraries are also needed, without them it falls back to the CPU.

## Settings

Right-click → **Settings…** opens `%APPDATA%\whisper-dictation\config.json` in Notepad.
Save it and the changes apply (the model changes on the next dictation). Each setting is explained in the
file's `_help` section.

| Setting | Default | |
|---|---|---|
| `hotkey` | `<ctrl>+<alt>+d` | Start/stop key, e.g. `<ctrl>+<shift>+<space>` |
| `model` | `auto` | `large-v3-turbo` with an NVIDIA GPU, otherwise `small` |
| `device` | `auto` | `auto`, `cuda` or `cpu` |
| `language` | `en` | Language code, empty to detect it (slower) |
| `pause` | `0.6` | Seconds of silence that end a sentence |
| `keep_model_loaded` | `false` | `true`: instant start, but the model stays in memory |
| `ignore_words` | `the, a, uh, um, huh, hmm` | A sentence made up only of these is dropped |
| `input_device` | empty | Part of the microphone's name (see `--list-mics`), empty for the default |
| `sounds` | `true` | Beep when ready and when stopped |

Problems are written to `%APPDATA%\whisper-dictation\log.txt` (right-click → **Open log**).
Windows' own noise suppression (Settings → System → Sound → your microphone → Audio enhancements)
helps in noisy rooms.
