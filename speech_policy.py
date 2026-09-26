"""Keep generated read-aloud actions inside Lashy's controllable voice path."""
import shlex
import re
from pathlib import PurePosixPath

SPEECH_TOOLS = {'spd-say', 'espeak', 'espeak-ng', 'festival', 'flite', 'pico2wave', 'say', 'edge-tts'}


def external_speech_command(command):
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    return any(PurePosixPath(token).name in SPEECH_TOOLS for token in tokens) or bool(
        re.search(r'(?<![\w-])(?:spd-say|espeak(?:-ng)?|festival|flite|pico2wave|edge-tts|pyttsx3|speechd|espeakng)\b', command)
    )

VOICE_RULES = """
READ-ALOUD AND VOICE OUTPUT:
The application speaks your response through its built-in neural voice. Never
launch spd-say, espeak, festival, system speech tools, or scripts to read text.
For a story or reading request, present the actual requested text naturally ONCE
in the response. Do not repeat it in later rounds. When research and writing are
finished, emit [DONE]; the application waits for narration to finish itself.
Audio playback is not observable in screenshots. Do not keep retrying a reading
because you cannot see speech on screen. If the speaker is muted, provide text
silently; never work around mute by launching another speech engine.
"""
