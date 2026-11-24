"""Voice input with Windows' own speech recognizer (used by `ramai voice` and as the app's fallback).

The RAM AI window itself uses Edge's built-in speech recognition (no Windows setting needed). Windows' recognizer needs
'Online speech recognition' switched on (Settings > Privacy & security > Speech); RAM AI never changes that setting."""
from __future__ import annotations

import asyncio
import datetime as dt
import threading

PRIVACY_HELP = ("Windows speech recognition is switched off on this PC. To use voice from the terminal: Settings > Privacy & security > "
                "Speech > turn on 'Online speech recognition' (opens with: start ms-settings:privacy-speech). "
                "The 🎤 button in the RAM AI window works without it.")
_lock = threading.Lock()


class VoiceError(Exception):
    pass


async def _listen(lang: str | None, first_words: float, pause: float) -> str:
    from winrt.windows.globalization import Language
    from winrt.windows.media.speechrecognition import SpeechRecognitionResultStatus, SpeechRecognizer

    r = None
    if lang:
        try:
            r = SpeechRecognizer(Language(lang))
        except Exception:
            r = None  # that language is not installed for Windows speech: use the system default
    r = r or SpeechRecognizer()
    comp = await r.compile_constraints_async()
    if int(comp.status) != 0:
        raise VoiceError(f"Windows speech could not start (status {int(comp.status)}).")
    r.timeouts.initial_silence_timeout = dt.timedelta(seconds=first_words)
    r.timeouts.end_silence_timeout = dt.timedelta(seconds=pause)
    try:
        res = await r.recognize_async()
    except OSError as e:
        code = (getattr(e, "winerror", 0) or 0) & 0xFFFFFFFF
        if code == 0x80045509:
            raise VoiceError(PRIVACY_HELP) from None
        if code in (0x80070005, 0x80045508):
            raise VoiceError("Microphone access is blocked. Settings > Privacy & security > Microphone: allow desktop apps.") from None
        raise VoiceError(f"Speech recognition failed: {e}") from None
    if int(res.status) != int(SpeechRecognitionResultStatus.SUCCESS):
        raise VoiceError("I didn't catch that (no speech or the microphone is muted). Try again.")
    return (res.text or "").strip()


def listen(lang: str | None = "en-IN", first_words: float = 6.0, pause: float = 1.2) -> str:
    """Listen on the default microphone until the speaker pauses; return the words. One listener at a time."""
    if not _lock.acquire(blocking=False):
        raise VoiceError("Already listening.")
    try:
        return asyncio.run(_listen(lang, first_words, pause))
    finally:
        _lock.release()
