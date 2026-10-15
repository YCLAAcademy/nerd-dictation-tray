# SPDX-License-Identifier: GPL-3.0-or-later
"""
Whisper speech engine for nerd-dictation (``--engine=WHISPER``), using faster-whisper.

``WhisperRecognizer`` has the same methods nerd-dictation uses on VOSK's ``KaldiRecognizer``
(``AcceptWaveform``, ``PartialResult``, ``FinalResult``, ``Reset``), so the main loop doesn't change.

Whisper transcribes whole utterances rather than word by word: audio is buffered until
a pause in speech, then transcribed and returned as one final result (with punctuation).
There is no partial (word by word) text.
"""

import json
import sys
from typing import List, Optional

import numpy as np

WHISPER_SAMPLE_RATE = 16000

# Energy based pause detection, on 30 ms frames.
FRAME_SECONDS = 0.03
# Never treat audio quieter than this (int16 RMS, about -47 dBFS) as speech.
SPEECH_RMS_MIN = 150.0
# Speech must be this many times louder than the background noise.
SPEECH_RMS_NOISE_RATIO = 3.0
# Utterances with less speech than this are dropped (clicks, bumps).
SPEECH_SECONDS_MIN = 0.2
# Audio kept from just before speech starts, so the first syllable isn't cut.
PRE_ROLL_SECONDS = 0.3
# Transcribe long monologues in pieces even without a pause (Whisper's window is 30 s).
UTTERANCE_SECONDS_MAX = 25.0


def _load_model(model: str, device: str, compute_type: str, verbose: int) -> "WhisperModel":
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.stderr.write(
            "The Whisper engine needs faster-whisper, install it with: pip install faster-whisper\n"
            "(using the same Python that runs nerd-dictation: {:s})\n".format(sys.executable)
        )
        sys.exit(1)

    if compute_type == "default" and device == "cpu":
        # The models are stored as float16, which is slow on a CPU.
        compute_type = "int8"

    def create(local_files_only: bool) -> "WhisperModel":
        return WhisperModel(model, device=device, compute_type=compute_type, local_files_only=local_files_only)

    try:
        # Use the downloaded model without contacting the network (works offline).
        return create(True)
    except Exception:
        pass
    # Not downloaded yet (or a local folder that failed to load, which reports its own error below).
    if verbose >= 1:
        sys.stderr.write("Downloading Whisper model {!r} (only needed once)...\n".format(model))
    try:
        return create(False)
    except Exception as ex:
        if device == "auto":
            # e.g. CUDA libraries missing: fall back to the CPU rather than failing.
            sys.stderr.write("Whisper failed to load on the GPU ({:s}), using the CPU.\n".format(str(ex)))
            return WhisperModel(model, device="cpu", compute_type="int8")
        raise


class WhisperRecognizer:
    def __init__(
        self,
        sample_rate: int,
        *,
        model: str = "large-v3-turbo",
        device: str = "auto",
        compute_type: str = "default",
        language: str = "",
        pause: float = 0.6,
        verbose: int = 0,
    ) -> None:
        self._model = _load_model(model, device, compute_type, verbose)
        self._sample_rate = sample_rate
        self._language = language or None
        self._verbose = verbose

        self._frame_len = max(1, int(sample_rate * FRAME_SECONDS))
        self._pause_frames = max(1, round(pause / FRAME_SECONDS))
        self._pre_roll_frames = round(PRE_ROLL_SECONDS / FRAME_SECONDS)
        self._speech_frames_min = round(SPEECH_SECONDS_MIN / FRAME_SECONDS)
        self._utterance_frames_max = round(UTTERANCE_SECONDS_MAX / FRAME_SECONDS)

        self._noise_rms: Optional[float] = None
        # Counts frames of speech heard, reported in `PartialResult` so time-outs see activity.
        self._speech_total = 0
        self.Reset()

        # The first transcription is slow (GPU warm-up), do it now rather than on the first words.
        # Once without speech detection (which would skip silence) to warm up the decoder, once to load it.
        silence = np.zeros(WHISPER_SAMPLE_RATE, dtype=np.float32)
        self._transcribe(silence, vad_filter=False, language=self._language or "en")
        self._transcribe(silence)

    # -------------------------------
    # VOSK `KaldiRecognizer` interface

    def Reset(self) -> None:
        self._remainder = b""
        self._samples = np.zeros(0, dtype=np.int16)
        self._frames: List[np.ndarray] = []
        self._speech_frames = 0
        self._silent_frames = 0
        self._result = ""

    def AcceptWaveform(self, data: bytes) -> bool:
        """Add recorded audio (16 bit mono), return True when an utterance has been transcribed."""
        data = self._remainder + data
        # Keep an odd trailing byte for the next call.
        cut = len(data) - (len(data) % 2)
        self._remainder = data[cut:]
        self._samples = np.concatenate((self._samples, np.frombuffer(data[:cut], dtype=np.int16)))

        done = False
        while len(self._samples) >= self._frame_len:
            frame, self._samples = self._samples[: self._frame_len], self._samples[self._frame_len :]
            if self._add_frame(frame):
                done = True
        return done

    def PartialResult(self) -> str:
        return json.dumps({"partial": "", "speech": self._speech_total})

    def FinalResult(self) -> str:
        """Return the last transcribed utterance, transcribing any speech still buffered."""
        if not self._result:
            self._finish_utterance()
        text, self._result = self._result, ""
        return json.dumps({"text": text})

    # -------
    # Private

    def _is_speech(self, frame: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
        if self._noise_rms is None:
            self._noise_rms = rms
        is_speech = rms > max(SPEECH_RMS_MIN, self._noise_rms * SPEECH_RMS_NOISE_RATIO)
        if rms < self._noise_rms:
            # Follow the noise floor down quickly...
            self._noise_rms = rms
        elif not is_speech:
            # ... and up slowly, ignoring speech.
            self._noise_rms += (rms - self._noise_rms) * 0.02
        return is_speech

    def _add_frame(self, frame: np.ndarray) -> bool:
        self._frames.append(frame)
        if self._is_speech(frame):
            self._speech_frames += 1
            self._speech_total += 1
            self._silent_frames = 0
        elif self._speech_frames:
            self._silent_frames += 1
        else:
            # No speech yet, only keep the pre-roll.
            del self._frames[: -self._pre_roll_frames or len(self._frames)]
            return False

        if self._silent_frames >= self._pause_frames or len(self._frames) >= self._utterance_frames_max:
            return self._finish_utterance()
        return False

    def _finish_utterance(self) -> bool:
        frames, speech_frames = self._frames, self._speech_frames
        self._frames, self._speech_frames, self._silent_frames = [], 0, 0
        if speech_frames < self._speech_frames_min:
            return False

        audio = np.concatenate(frames).astype(np.float32) / 32768.0
        if self._sample_rate != WHISPER_SAMPLE_RATE:
            positions = np.arange(0, len(audio), self._sample_rate / WHISPER_SAMPLE_RATE)
            audio = np.interp(positions, np.arange(len(audio)), audio).astype(np.float32)

        text = self._transcribe(audio)
        if self._verbose >= 2:
            sys.stderr.write("Whisper: {!r}\n".format(text))
        if not text:
            return False
        self._result = (self._result + " " + text).strip()
        return True

    def _transcribe(self, audio: np.ndarray, vad_filter: bool = True, language: Optional[str] = None) -> str:
        segments, _info = self._model.transcribe(
            audio,
            language=language or self._language,
            beam_size=5,
            # Whisper's own speech detection, stops it inventing text for noise.
            vad_filter=vad_filter,
            # Each utterance stands alone, avoids repeating earlier text.
            condition_on_previous_text=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()
