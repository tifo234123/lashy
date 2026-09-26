"""
Central state machine for Lashy Windows.

Orchestrates:
  hotkey / wake-word → ambient listener capture → STT → screen capture
  → web search → (optional Claude Computer Use pointing) → LLM → TTS
"""

import asyncio
import logging
import math
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from config import cfg
from ai.base_provider import BaseLLMProvider, Message
from audio.ambient_listener import AmbientListener
from screen.capture import capture_all_screens
from ui.panel import AppState
from tutor import (
    active_window_title, app_key,
    is_locate, is_multistep, is_next, is_stop, is_sensitive_window,
    is_repeat, is_journal_today, is_journal_week, is_quiz_review,
    is_identity_question,
)
from tutor_features import (
    journal, pdf_context, ocr, code_mode, lesson_recorder,
    multilang, workflow_capture, collab,
)
import skills as skills_pkg

_log = logging.getLogger("lashy.manager")


def _ensure_ollama_running():
    """Start Ollama if it isn't already running. Waits up to 8 s for it to be ready."""
    import subprocess
    import urllib.request

    url = "http://localhost:11434/api/tags"
    for _ in range(2):
        try:
            urllib.request.urlopen(url, timeout=2)
            return  # already up
        except Exception:
            pass

    # API down. If an ollama process already exists, don't spawn a second
    # `ollama serve` — duplicate instances fight over the port and wedge the
    # API entirely. Just wait for the existing one below.
    already_running = False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ollama.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        already_running = "ollama.exe" in out.lower()
    except Exception:
        pass

    if not already_running:
        try:
            subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        except FileNotFoundError:
            return  # ollama not installed, provider will fail gracefully

    # Wait up to 8 s for the server to come up
    for _ in range(16):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:
            pass


def _build_system_prompt(
    window_title: str = "",
    lesson_step: int = 0,
    total_steps: int = 0,
    quiz_mode: bool = False,
    detected_coord: Optional[tuple] = None,
    code_active: bool = False,
    language_code: str = "en",
    extra: str = "",
) -> str:
    today = datetime.now().strftime("%A, %B %d, %Y")
    ctx_lines = [f"TODAY'S DATE: {today}."]
    if window_title:
        ctx_lines.append(f'ACTIVE WINDOW: "{window_title}"')
    if detected_coord:
        x, y, label = detected_coord
        ctx_lines.append(
            f"DETECTED ELEMENT (pre-computed by the pointing engine — use "
            f"this coordinate verbatim in your [POINT] tag): x={x}, y={y}, "
            f"label='{label}'. (Already normalized 0-1000.)"
        )
    if total_steps > 1:
        ctx_lines.append(
            f"LESSON PROGRESS: step {lesson_step + 1} of {total_steps}. "
            "Explain ONLY this step, then end with \"Say 'next' when ready.\""
        )

    # ── Quiz mode: dominant prompt that completely replaces normal behaviour ──
    if quiz_mode:
        return f"""You are Lashy, an interactive QUIZ TUTOR. The user has
turned on Quiz Mode and wants to be tested, NOT explained to.

{chr(10).join(ctx_lines)}

ABSOLUTE QUIZ RULES (override everything else):
  • NEVER answer the user's question directly. NEVER point at UI elements.
    NEVER emit [POINT:...] tags. NEVER explain how things work.
  • If the user is greeting / starting ("hello", "what's on my screen", "begin",
    "quiz me", anything), START the quiz: ask ONE short, specific question
    about what's visible on screen — name a button, recognise an icon, predict
    what a click would do, identify the active app, etc.
  • If the user's last message looks like an ANSWER (a noun, a short phrase, a
    yes/no), evaluate it in ≤1 sentence ("Correct!" / "Close — actually..."),
    then immediately ask the NEXT question.
  • Questions should be progressively harder. Vary topic across UI literacy,
    keyboard shortcuts, what's currently visible, predicting outcomes.
  • Keep it warm and encouraging. Never lecture.
  • Format every turn as:  <one-line evaluation if applicable>  <one question>

STYLE: short, friendly, never more than 2 sentences. End every turn with a
question mark."""

    from config import _TECHNICAL_RULES
    base = cfg.custom_instructions.strip()
    base = base.replace("{{CONTEXT}}", chr(10).join(ctx_lines))
    base = base.replace("{{TODAY}}", today)
    from speech_policy import VOICE_RULES
    return base + _TECHNICAL_RULES + _code_addendum(code_active) + _lang_addendum(language_code) + extra + VOICE_RULES


def _code_addendum(active: bool) -> str:
    if not active:
        return ""
    from tutor_features.code_mode import code_system_prompt_addendum
    return code_system_prompt_addendum()


def _lang_addendum(code: str) -> str:
    from tutor_features.multilang import language_directive
    return language_directive(code)


def _guess_label(transcript: str) -> str:
    """Extract a 1-3 word label from a locate query for the speech bubble.
       'where is the search bar' → 'search bar' """
    t = transcript.lower().strip().rstrip("?.!")
    for kw in ("where is the ", "where's the ", "show me the ",
              "find the ", "locate the ", "click the ", "click on the ",
              "how do i click ", "how do i find ", "how do i open ",
              "point at the ", "point to the ", "highlight the "):
        if kw in t:
            tail = t.split(kw, 1)[1]
            words = tail.split()
            return " ".join(words[:3]) or "here"
    return "right here!"


def _split_steps(text: str) -> list[str]:
    """Parse a numbered list out of an LLM response. Returns [] if not a list."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    steps = []
    for ln in lines:
        m = re.match(r"^(?:\d+[\).]|[-*])\s+(.+)$", ln)
        if m:
            steps.append(m.group(1).strip())
    return steps


def _speakable(text: str) -> str:
    """Make LLM text safe for TTS: models emit LaTeX ("\\( a \\)",
    "\\[ a^2 + b^2 = c^2 \\]") and markdown that edge-tts reads aloud
    verbatim as gibberish. Convert to spoken math / plain words."""
    t = text
    t = re.sub(r'\\(?:left|right)\b', '', t)
    t = re.sub(r'\\sqrt\s*\{([^{}]*)\}', r'the square root of \1', t)
    t = re.sub(r'\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}', r'\1 over \2', t)
    t = (t.replace('\\times', ' times ').replace('\\cdot', ' times ')
          .replace('\\pi', ' pi ').replace('\\theta', ' theta ')
          .replace('\\alpha', ' alpha ').replace('\\beta', ' beta '))
    t = re.sub(r'\\[\[\(\]\)]', '', t)              # \( \) \[ \] delimiters
    t = re.sub(r'\^\s*\{?2\}?', ' squared', t)
    t = re.sub(r'\^\s*\{?3\}?', ' cubed', t)
    t = re.sub(r'\^\s*\{?(\d+)\}?', r' to the power \1', t)
    t = t.replace('²', ' squared').replace('³', ' cubed')
    t = re.sub(r'\\[a-zA-Z]+', ' ', t)              # any leftover \commands
    t = re.sub(r'[{}]', '', t)
    t = re.sub(r'[*_#`]+', '', t)                   # markdown emphasis/headers
    t = re.sub(r'\s+', ' ', t).strip()
    return t


POINT_RE = re.compile(r'\[POINT:(\d+),(\d+):([^:\]]+):screen(\d+)\]')
# A partial "[POINT..." prefix that hasn't closed yet — hold it back from display
# until the next chunk so we never leak a half tag.
POINT_PARTIAL_RE = re.compile(r'\[(?:P|PO|POI|POIN|POINT|POINT:[^\]]*)?$')

# ── Teaching / drawing tags ──────────────────────────────────────────────────
# ALL coordinates are normalized 0-1000 relative to the screenshot the model
# saw (x: 0=left edge, 1000=right edge; y: 0=top, 1000=bottom). The manager
# converts to logical screen pixels via _denorm(). Trailing :color is optional
# on every shape.
_C = r'(?::([a-z]+))?'                       # optional trailing color group
LINE_RE      = re.compile(r'\[LINE:(\d+),(\d+)->(\d+),(\d+)' + _C + r'\]')
ARROW_RE     = re.compile(r'\[ARROW:(\d+),(\d+)->(\d+),(\d+)' + _C + r'\]')
CIRCLE_RE    = re.compile(r'\[CIRCLE:(\d+),(\d+),(\d+)(?::([^:\]]*))?' + _C + r'\]')
RECT_RE      = re.compile(r'\[RECT:(\d+),(\d+),(\d+),(\d+)' + _C + r'\]')
POLY_RE      = re.compile(r'\[POLY:((?:\d+,\d+[ ]*)+)' + _C + r'\]')
TEXT_RE      = re.compile(r'\[TEXT:(\d+),(\d+):([^:\]]+)' + _C + r'(?::(s|m|l))?\]')
ANGLE_RE     = re.compile(r'\[ANGLE:(\d+),(\d+),(\d+)(?:,(-?\d+))?' + _C + r'\]')
UNDERLINE_RE = re.compile(r'\[UNDERLINE:(\d+),(\d+),(\d+)' + _C + r'\]')
LABEL_RE     = re.compile(r'\[LABEL:(\d+),(\d+):([^:\]]+)' + _C + r'\]')
CLEAR_RE     = re.compile(r'\[CLEAR\]')
# Anchor forms — element resolved by name via the hybrid pointer (UIA), so
# the model never guesses coordinates for real UI: [CIRCLE:@Save button]
CIRCLE_AT_RE    = re.compile(r'\[CIRCLE:@([^:\]]+?)' + _C + r'\]')
UNDERLINE_AT_RE = re.compile(r'\[UNDERLINE:@([^:\]]+?)' + _C + r'\]')

EXEC_RE        = re.compile(r'\[EXEC:([^\]]+)\]')
TYPE_RE        = re.compile(r'\[TYPE:([^\]]+)\]')
KEY_RE         = re.compile(r'\[KEY:([^\]]+)\]')
CLICK_RE       = re.compile(r'\[CLICK\]')
CLICK_COORD_RE = re.compile(r'\[CLICK:(\d+),(\d+)\]')
WAIT_RE        = re.compile(r'\[WAIT:(\d+(?:\.\d+)?)\]')
DONE_RE        = re.compile(r'\[DONE\]')
STEP_RE        = re.compile(r'\[STEP:([^\]]+)\]')

ANY_TAG_RE   = re.compile(
    r'\[(?:POINT|ARROW|CIRCLE|UNDERLINE|LABEL|LINE|RECT|POLY|TEXT|ANGLE|CLEAR|EXEC|TYPE|KEY|CLICK|WAIT|DONE|STEP)'
    r'(?::[^\]]*)?\]'
)
ANY_PARTIAL_RE = re.compile(r'\[[A-Z]{0,9}(?::[^\]]*)?$')

# Questions that ask Lashy to locate / click UI elements — triggers the
def _is_action_task(transcript: str) -> bool:
    from task_executor import is_task_followup
    t = transcript.lower().strip()
    if is_task_followup(t):
        return True
    t = re.sub(r"^(?:(?:please|can you|could you|would you|i want you to|help me)\s+)+", "", t)
    chain_words = [
        " then ", " and then ", " after that ", " next ",
        " followed by ", " once that's done ", " and also ",
        " then open ", " then go to ", " then send ", " then click ", " then type "
    ]
    if any(kw in t for kw in chain_words):
        return True

    action_starts = (
        "send ", "text ", "message ", "write ", "email ",
        "open ", "launch ", "start ", "go to ",
        "click ", "press ", "type ",
        "create ", "delete ", "remove ", "download ", "install ", "update ", "run ",
        "search for ", "find ", "research ", "browse ", "apply ", "fill ", "complete ", "organize ", "book ", "compare ",
        "do ", "do the ", "apply", "fill ", "complete ", "submit ", "solve ", "fix ",
        "handle ", "work on ", "proceed", "continue", "automate ", "execute "
    )
    if any(t.startswith(prefix) for prefix in action_starts):
        return True

    if re.search(r"\b(tell|say|send|message|text|write)\b.*\b(to|him|her|them|on whatsapp|on telegram|via whatsapp)\b", t):
        return True

    action_verbs = r"(open|launch|go to|start|take|run|create|make|write|find|search for|look for|type|send|click|message|apply|fill|solve|complete|do)"
    if re.search(rf"\b{action_verbs}\b.*\b(and|then)\b.*\b{action_verbs}\b", t):
        return True

    # Imperative task phrases
    if t in ("do the job", "do it", "apply", "apply now", "continue", "proceed", "go ahead", "fill form"):
        return True

    return False

_is_multistep_request = _is_action_task


class CompanionManager(QObject):
    sig_transcript = pyqtSignal(str)
    """Thread-safe signals for Qt UI updates from async/audio threads."""

    sig_state_changed       = pyqtSignal(object)          # AppState
    sig_status_text         = pyqtSignal(str)
    sig_response_chunk      = pyqtSignal(str)
    sig_response_done       = pyqtSignal(str)
    sig_audio_level         = pyqtSignal(float)
    sig_point_at            = pyqtSignal(float, float, str)
    sig_point_hold          = pyqtSignal(bool)            # True → dwell forever until release
    sig_point_release       = pyqtSignal()                # end dwell + fly buddy back
    sig_error               = pyqtSignal(str)
    sig_copilot_models_done = pyqtSignal(int)             # arg = model count
    sig_models_refreshed    = pyqtSignal(str, int)        # (provider, count)
    sig_ollama_models       = pyqtSignal(dict)            # {"vision": [...], "text": [...]}
    sig_ollama_pull_status  = pyqtSignal(str, str)        # (model_name, status_msg)
    sig_arrow               = pyqtSignal(float, float, float, float)
    sig_circle              = pyqtSignal(float, float, float)
    sig_underline           = pyqtSignal(float, float, float)
    sig_label               = pyqtSignal(float, float, str)
    sig_draw                = pyqtSignal(dict)            # generic teaching shape → overlay
    sig_clear_drawings      = pyqtSignal()                # wipe all teaching shapes
    sig_recording_state     = pyqtSignal(bool, str)       # (is_recording, output_dir)

    def __init__(self):
        super().__init__()
        self._state: AppState = AppState.IDLE
        self._history: List[Message] = []
        self._current_model: Optional[str] = cfg.selected_model() or None
        self._web_search_enabled = True
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Providers (lazy)
        self._llm: Optional[BaseLLMProvider] = None
        self._stt = None
        self._tts = None

        # Current in-flight generation — tracked so Esc / stop can cancel
        self._current_task: Optional[asyncio.Future] = None
        self._cancel_flag = False

        # Push-to-talk bookkeeping. A capture can be ended either by releasing
        # the hotkey (hold) or by the silence watchdog (tap) — whichever gets
        # there first claims it, so an utterance is never processed twice.
        self._hotkey_pressed_at: float = 0.0
        self._end_claimed = False
        self._end_lock = threading.Lock()
        # Starting a capture needs the same mutual exclusion as ending one.
        # The hotkey runs on the keyboard-hook thread and the wake word on the
        # sounddevice callback thread; both used to test `_state == IDLE` and
        # then call _begin_capture(), which only set LISTENING afterwards. Two
        # threads could pass that test on the same utterance and open two
        # captures, producing two LLM calls and two TTS streams playing over
        # each other with no gap between them.
        self._begin_lock = threading.Lock()

        # "next" / "repeat" are one or two words. Anything longer than this
        # was a real question that Whisper mangled, not a command.
        self._MAX_COMMAND_SECONDS = 2.5
        # Auto-detect stays on, but the reply voice only follows it once the
        # same language has been seen twice running. Romanised Hinglish makes
        # langdetect return a different answer almost every turn, and the
        # accent was changing mid-conversation as a result.
        self._voice_lang: str = "en"
        self._pending_lang: str = ""
        self._applied_voice_lang: str = "en"

        self._last_rms: float = 0.0

        # Per-app memory: { window_title: [Message, ...] }
        self._app_memory: dict[str, List[Message]] = {}
        # Screenshots from the current turn — needed to map the LLM's
        # normalized 0-1000 tag coordinates back to logical screen pixels.
        self._screens_ctx: list = []
        # Figures detected on screen this turn (normalized vertices) — used
        # for prompt injection and for snapping sloppy stroke endpoints.
        self._figures_ctx: list = []
        # Current lesson: sequence of pending steps for multi-step tutorials
        self._lesson_steps: list[str] = []
        self._lesson_step_idx: int = 0
        # Toggles
        self._slow_mode = False
        self._quiz_mode = False
        self._privacy_guard = True
        self._code_mode_auto = True       # auto-detect IDE windows
        self._multilang = True             # auto-reply in user's language
        self._journal_enabled = True       # log every Q&A to SQLite
        self._ocr_enabled = True           # use Tesseract for fine print
        self._last_response = ""           # for "say it again" voice command
        self._attached_docs: list[tuple[str, str]] = []   # (filename, text)

        # Optional subsystems (lazy-init to keep startup fast)
        self._recorder: Optional[lesson_recorder.LessonRecorder] = None
        self._collab: Optional[collab.CollabSession] = None
        self._workflow: Optional[workflow_capture.WorkflowCapture] = None

        # Linux target window tracking & Fast TTS toggle
        self._target_window_id: str = ""
        self._target_window_title: str = ""
        self._tts_enabled: bool = False   # Fast chat by default (no mic required)

        from PyQt6.QtCore import QTimer
        self._win_poll_timer = QTimer(self)
        self._win_poll_timer.timeout.connect(self._poll_active_window)
        self._win_poll_timer.start(500)

        # Load user-created skills from skills/ + ~/.lashy/skills/
        try:
            skills_pkg.load_all()
        except Exception:
            pass

        # Always-on ambient listener
        self._listener = AmbientListener(
            on_level=self._handle_level,
            on_wake=self._handle_wake,
            device=cfg.mic_device_index,
            ambient=cfg.ambient_mic(),
        )

        # Background asyncio loop
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _poll_active_window(self):
        try:
            from tutor import active_window_id, active_window_title
            wid = active_window_id()
            name = active_window_title()
            if wid and "lashy" not in name.lower() and "clicky" not in name.lower() and name.strip():
                self._target_window_id = wid
                self._target_window_title = name
        except Exception:
            pass

    def set_tts_enabled(self, enabled: bool):
        if enabled and (getattr(self, '_query_running', False) or self._state != AppState.IDLE):
            return  # unmute applies only before a new request
        self._tts_enabled = enabled
        if not enabled:
            self._cancel_narration()
            from audio.playback import stop_audio
            stop_audio()
        _log.info("TTS enabled set to %s", enabled)

    @property
    def tts_enabled(self) -> bool:
        return self._tts_enabled

    def _build_system_prompt_for_step(self, window_title: str = "", step_num: int = 1) -> str:
        from task_history import task_history
        hist_extra = "\n\n" + task_history.format_recent_for_prompt() if task_history.load_all() else ""
        return _build_system_prompt(
            window_title=window_title,
            lesson_step=0,
            total_steps=0,
            extra=hist_extra + "".join(
                pdf_context.format_for_prompt(name, text)
                for name, text in self._attached_docs
            ),
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        try:
            self._listener.start()
        except Exception as e:
            self.sig_error.emit(f"Mic error: {e}")
        # Sleep/wake watchdog — restarts mic + loop after system resume
        self._start_sleep_watchdog()
        # On startup, refresh any stale model cache in the background.
        # 30-day TTL means this is a once-a-month no-op for most launches.
        self._submit(self._refresh_stale_models())

    async def _refresh_stale_models(self):
        try:
            from ai.model_registry import refresh_all_stale
            results = await refresh_all_stale()
            for prov, count in results.items():
                if count > 0:
                    self.sig_models_refreshed.emit(prov, count)
        except Exception:
            pass   # silent — not user-facing on startup

    def shutdown(self):
        # Kill any audio that was playing when the user clicked Quit
        try:
            from audio.playback import stop_audio
            stop_audio()
        except Exception:
            pass
        self._listener.stop()
        # Close the provider's HTTP pool while the loop is still alive —
        # once it stops, the coroutine can never run. Bounded so a hung
        # socket cannot delay quitting.
        closer = getattr(self._llm, "close", None)
        if callable(closer) and self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._llm.close(), self._loop
                ).result(timeout=3)
            except Exception:
                _log.debug("closing provider on shutdown failed", exc_info=True)
        self._llm = None
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    # ── Sleep/wake watchdog ───────────────────────────────────────────────────

    def _start_sleep_watchdog(self):
        """Background thread that detects system resume after sleep/hibernate
        and restarts the mic stream + asyncio loop so the panel stays live."""
        def _watch():
            HEARTBEAT = 5.0          # check every 5 s
            DRIFT_THRESHOLD = 15.0   # if we wake and >15 s have passed, resume occurred
            last_tick = time.monotonic()
            while True:
                time.sleep(HEARTBEAT)
                now = time.monotonic()
                drift = now - last_tick - HEARTBEAT
                last_tick = now
                if drift > DRIFT_THRESHOLD:
                    # System was sleeping — restart subsystems
                    self._on_system_resume()

        t = threading.Thread(target=_watch, daemon=True)
        t.start()

    def _on_system_resume(self):
        """Called automatically after the laptop wakes from sleep."""
        # 1. Restart the mic stream (sounddevice handles become stale on resume)
        try:
            self._listener.stop()
        except Exception:
            pass
        time.sleep(1.0)   # give Windows audio stack time to reinit
        try:
            self._listener.start()
        except Exception as e:
            self.sig_error.emit(f"Mic restart after sleep failed: {e}")

        # 2. If the asyncio loop thread died, restart it
        if not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()

        # 3. Reset state to IDLE so the panel shows the correct status
        if self._state != AppState.IDLE:
            self._emit_state(AppState.IDLE)

    def _submit(self, coro):
        if not self._loop:
            return
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)

        def _observe(f):
            if f.cancelled():
                return
            try:
                f.result()
            except Exception as e:
                # A swallowed exception here used to leave the UI stuck on
                # "Listening..." forever (GitHub issue #6). Surface it and
                # always return to idle.
                _log.exception("background task failed: %s", e)
                try:
                    self.sig_error.emit(str(e))
                    self._emit_state(AppState.IDLE)
                except Exception:
                    pass
        fut.add_done_callback(_observe)

    def _retire_llm(self) -> None:
        """Drop the active provider and close its pooled HTTP client.

        Providers now keep a persistent httpx.AsyncClient (up to 8 pooled
        connections). Setting self._llm = None only drops the reference, so
        the pool stayed open until garbage collection got round to it.

        The close is deferred until nothing is being generated: aclose() tears
        down the connection pool, so running it mid-stream would abort the
        answer the user is currently listening to. Scheduled directly rather
        than through _submit(), which reports failures to the user and forces
        the UI back to idle — neither is wanted for background cleanup.
        """
        old, self._llm = self._llm, None
        closer = getattr(old, "close", None)
        if old is None or not callable(closer):
            return
        if not self._loop or not self._loop.is_running():
            return

        async def _close_when_idle():
            deadline = time.monotonic() + 30
            while self._state != AppState.IDLE and time.monotonic() < deadline:
                await asyncio.sleep(0.2)
            try:
                await old.close()
            except Exception:
                _log.debug("closing retired provider failed", exc_info=True)

        try:
            asyncio.run_coroutine_threadsafe(_close_when_idle(), self._loop)
        except Exception:
            pass

    # ── Provider lazy init ────────────────────────────────────────────────────

    def _get_llm(self) -> BaseLLMProvider:
        if self._llm is None:
            provider = cfg.llm_provider()
            if provider == "claude":
                from ai.claude_provider import ClaudeProvider
                self._llm = ClaudeProvider()
            elif provider == "openai":
                from ai.openai_provider import OpenAIProvider
                self._llm = OpenAIProvider()
            elif provider == "gemini":
                from ai.gemini_provider import GeminiProvider
                self._llm = GeminiProvider()
            elif provider == "copilot":
                from ai.github_copilot_provider import GitHubCopilotProvider
                self._llm = GitHubCopilotProvider()
            elif provider == "lmstudio":
                from ai.lmstudio_provider import LMStudioProvider
                self._llm = LMStudioProvider()
            else:
                _ensure_ollama_running()
                from ai.ollama_provider import OllamaProvider
                self._llm = OllamaProvider()
        return self._llm

    def _get_stt(self):
        if self._stt is None:
            provider = cfg.stt_provider()
            if provider == "deepgram":
                from audio.stt.deepgram_stt import DeepgramSTT
                self._stt = DeepgramSTT()
            elif provider == "openai":
                from audio.stt.openai_stt import OpenAISTT
                self._stt = OpenAISTT()
            elif provider == "whisper_cpp":
                try:
                    from audio.stt.whisper_cpp_stt import WhisperCppSTT
                    self._stt = WhisperCppSTT()
                except ImportError:
                    # pywhispercpp missing → fall back silently
                    from audio.stt.faster_whisper_stt import FasterWhisperSTT
                    self._stt = FasterWhisperSTT()
            else:
                from audio.stt.faster_whisper_stt import FasterWhisperSTT
                self._stt = FasterWhisperSTT()
        return self._stt

    def _get_tts(self):
        if self._tts is None:
            provider = cfg.tts_provider()
            if provider == "elevenlabs":
                from audio.tts.elevenlabs_provider import ElevenLabsProvider
                self._tts = ElevenLabsProvider()
            elif provider == "openai":
                from audio.tts.openai_tts_provider import OpenAITTSProvider
                self._tts = OpenAITTSProvider()
            else:
                from audio.tts.edge_tts_provider import EdgeTTSProvider
                self._tts = EdgeTTSProvider()
        return self._tts

    # ── Input sources ─────────────────────────────────────────────────────────

    def on_hotkey_press(self):
        if not self._claim_begin():
            return
        self._ptt_held = True
        self._hotkey_pressed_at = time.monotonic()
        self._begin_capture()
        # Arm the silence watchdog straight away. If this turns out to be a
        # hold rather than a tap, on_hotkey_release claims the end first.
        self._submit(self._auto_stop_on_silence(self._capture_id))

    def on_hotkey_release(self):
        self._ptt_held = False
        if self._state != AppState.LISTENING:
            return
        held = time.monotonic() - self._hotkey_pressed_at
        if held < cfg.tap_max_seconds:
            # A tap, not a hold: keep listening and let the user finish
            # speaking. The watchdog ends the capture on silence.
            return
        if self._claim_end():
            self._submit(self._end_capture_and_process())

    def cancel_tap_capture(self, max_seconds: float = 0.35) -> bool:
        """Quietly cancel a capture that a hotkey TAP just opened.

        A short press of Ctrl+Win now toggles the panel instead of talking,
        so the mic capture that on_hotkey_press() optimistically started has
        to be rolled back. Bumps the capture id so the silence watchdog
        (already submitted) returns without ever processing audio.

        Returns True when a LISTENING capture within the tap window was
        cancelled; False means the press wasn't ours to cancel.
        """
        if self._state != AppState.LISTENING:
            return False
        if time.monotonic() - getattr(self, "_hotkey_pressed_at", 0.0) >= max_seconds:
            return False
        self._ptt_held = False
        self._capture_id = getattr(self, "_capture_id", 0) + 1
        self._end_claimed = True
        try:
            self._listener.stop_recording()
        except Exception:
            pass
        self._emit_state(AppState.IDLE)
        return True

    def _handle_wake(self):
        """Triggered from ambient listener when wake-word is detected."""
        if not self._claim_begin():
            return
        self._begin_capture()
        self._submit(self._auto_stop_on_silence(self._capture_id))

    def _claim_begin(self) -> bool:
        """Exactly one of {hotkey press, wake word} may open a capture.

        Sets LISTENING inside the lock so a second caller on another thread
        cannot also observe IDLE and start a parallel capture.
        """
        with self._begin_lock:
            if self._state != AppState.IDLE:
                return False
            self._state = AppState.LISTENING
            return True

    def _claim_end(self) -> bool:
        """Exactly one of {hotkey release, silence watchdog} ends a capture."""
        with self._end_lock:
            if self._end_claimed:
                return False
            self._end_claimed = True
            return True

    def _handle_level(self, rms: float):
        self._last_rms = rms
        try:
            self.sig_audio_level.emit(rms)
        except Exception:
            pass   # never crash the sounddevice audio thread

    # ── Capture flow ──────────────────────────────────────────────────────────

    def _begin_capture(self):
        self._cancel_flag = False
        self._capture_id = getattr(self, "_capture_id", 0) + 1
        self._end_claimed = False
        self._last_rms = 0.0
        # Silence anything still playing from the previous turn. Without this
        # a leftover TTS stream keeps going while the next answer starts, and
        # the two are heard on top of one another.
        try:
            from audio.playback import stop_audio
            stop_audio()
        except Exception:
            pass
        try:
            self._listener.start_recording()
        except Exception as e:
            _log.exception("mic start failed")
            self.sig_error.emit(
                f"Couldn't open the microphone: {e}\n"
                "Check Tray → Setup & Diagnostics → Microphone."
            )
            self._emit_state(AppState.IDLE)
            return
        self._emit_state(AppState.LISTENING)
        self.sig_status_text.emit("Listening — release when you finish")

    # End-of-speech tuning. SPEECH_RMS matches the ambient listener's VAD
    # threshold so both agree on what counts as speech.
    _SPEECH_RMS        = 0.006
    _END_SILENCE_S     = 1.1    # quiet for this long after speaking = done
    _NO_SPEECH_S       = 6.0    # nothing said at all = give up
    _MAX_UTTERANCE_S   = 45.0   # hard cap so a stuck mic can't record forever

    async def _auto_stop_on_silence(self, capture_id=None):
        """End the capture once the user actually stops talking.

        The previous version just waited a flat 10 seconds, which cut long
        questions off mid-sentence and made short ones sit there in silence.
        This watches the live mic level instead: wait for speech to start,
        then end on a sustained pause.
        """
        capture_id = self._capture_id if capture_id is None else capture_id
        start_t = time.monotonic()
        speech_started = False
        silence_from: Optional[float] = None

        while self._state == AppState.LISTENING:
            await asyncio.sleep(0.05)
            now = time.monotonic()

            if capture_id != self._capture_id or self._state != AppState.LISTENING:
                return
            if now - start_t > self._MAX_UTTERANCE_S:
                break
            if getattr(self, "_ptt_held", False):
                continue
            if self._last_rms > self._SPEECH_RMS:
                speech_started = True
                silence_from = None
            elif speech_started:
                if silence_from is None:
                    silence_from = now
                elif now - silence_from >= self._END_SILENCE_S:
                    break

            if not speech_started and now - start_t > self._NO_SPEECH_S:
                break
            if now - start_t > self._MAX_UTTERANCE_S:
                break

        if capture_id == self._capture_id and self._state == AppState.LISTENING and self._claim_end():
            await self._end_capture_and_process()

    def ask_text(self, text: str):
        """Entry point for text/chat input from the panel or scripts."""
        if not text or not text.strip():
            return
        self._submit(self.process_query(text.strip(), audio_seconds=999.0, from_text=True))

    async def _end_capture_and_process(self):
        capture_id = self._capture_id
        try:
            pcm = self._listener.stop_recording()
        except Exception as e:
            _log.exception("mic stop failed")
            self.sig_error.emit(f"Microphone capture failed: {e}")
            self._emit_state(AppState.IDLE)
            return
        _log.info("captured %.1fs of audio", len(pcm) / 32000)
        if len(pcm) < 3200:  # < 0.1s of audio — ignore
            self._emit_state(AppState.IDLE)
            self.sig_status_text.emit("No audio captured — hold to talk and try again")
            return

        # 32000 bytes == 1s of 16 kHz mono int16.
        audio_seconds = len(pcm) / 32000.0

        self._emit_state(AppState.THINKING)
        self.sig_status_text.emit("Transcribing your voice…")
        try:
            # 1. Transcribe — bounded so a hung/downloading STT model can
            # never freeze the UI on "Thinking..." forever
            transcript = await asyncio.wait_for(
                self._get_stt().transcribe(pcm), timeout=90,
            )
            if capture_id != self._capture_id or self._cancel_flag:
                return
            _log.info("transcript: %r (provider=%s)",
                      transcript[:120], cfg.llm_provider())
            if not transcript.strip():
                self._emit_state(AppState.IDLE)
                self.sig_status_text.emit("No speech recognized — hold to talk and try again")
                return
            self.sig_transcript.emit(transcript.strip())
            await self.process_query(transcript, audio_seconds=audio_seconds, from_text=False)
        except Exception as e:
            _log.exception("audio transcription failed: %s", e)
            self.sig_error.emit(str(e))
            self._emit_state(AppState.IDLE)

    async def process_query(self, transcript: str, audio_seconds: float = 999.0, from_text: bool = False):
        if is_stop(transcript):
            self.stop()
            return
        if getattr(self, "_query_running", False):
            self.sig_status_text.emit("A task is still running. Press Esc to stop it first.")
            return
        self._query_running = True
        self._cancel_flag = False
        self._stop_pending = False
        self._current_task = asyncio.current_task()
        from audio.playback import begin_audio_session
        begin_audio_session()
        try:
            await self._process_query_impl(transcript, audio_seconds, from_text)
        except asyncio.CancelledError:
            pass
        finally:
            self._query_running = False
            self._current_task = None
            self._stop_pending = False

    async def _process_query_impl(self, transcript: str, audio_seconds: float = 999.0, from_text: bool = False):
        self._executed_cmds = set()
        self._executed_types = set()
        self._executed_keys = set()
        self._emit_state(AppState.THINKING)
        pointing_held = False  # track whether we told overlay to hold dwell
        narrator = None
        from response_policy import needs_web_search, is_greeting
        try:

            # ── Voice commands — short-circuit before LLM ──
            if is_stop(transcript):
                self.stop()
                return

            title = self._target_window_title or active_window_title()
            ak = app_key(title)

            # Voice commands are one or two words, so they cannot come from a
            # long recording. Whisper degrades to a short generic phrase
            # ("Continue.", "Repeat.") on unclear or code-mixed speech, and
            # without this length check a mis-transcribed sentence silently
            # replayed the previous answer instead of being asked.
            command_plausible = from_text or audio_seconds <= self._MAX_COMMAND_SECONDS
            if not command_plausible and (is_next(transcript) or is_repeat(transcript)):
                _log.info(
                    "ignoring %r as a voice command — %.1fs of audio is too "
                    "long for one; treating it as a question",
                    transcript[:40], audio_seconds,
                )

            if command_plausible and is_next(transcript) and self._lesson_steps:
                await self._advance_lesson_step(ak)
                return

            # "say it again" — replay the last response without a new LLM call
            if command_plausible and is_repeat(transcript) and self._last_response:
                self.sig_response_chunk.emit(self._last_response)
                self.sig_response_done.emit(self._last_response)
                self._emit_state(AppState.SPEAKING)
                try:
                    await self._get_tts().speak(self._last_response)
                except Exception:
                    pass
                self._emit_state(AppState.IDLE)
                return

            # Journal voice queries — answered locally, no LLM call needed
            if is_journal_today(transcript):
                msg = journal.summarise(journal.entries_today(),
                                        "Here's what you asked about today:\n")
                await self._reply_local(msg)
                return
            if is_journal_week(transcript):
                msg = journal.summarise(journal.entries_this_week(),
                                        "Here's the past week:\n")
                await self._reply_local(msg)
                return
            if is_quiz_review(transcript):
                await self._spaced_review()
                return

            # Multi-step task agentic executor: decomposes composite tasks into steps
            if _is_multistep_request(transcript):
                _log.info("Dispatching multi-step task to TaskExecutor: %r", transcript)
                from task_executor import TaskExecutor
                executor = TaskExecutor(self)
                history = self._app_memory.setdefault(ak, [])
                context = getattr(self, "_recent_conversation", None) or history
                result = await executor.execute_task(transcript, context=context)
                history.extend([Message(role="user", content=transcript),
                                Message(role="assistant", content=result)])
                self._app_memory[ak] = history[-20:]
                self._recent_conversation = history[-12:]
                return

            # User-created skills (run BEFORE the LLM, like built-ins above)
            try:
                skill = skills_pkg.match(transcript)
                if skill:
                    msg = await skill["handler"](self, transcript)
                    if msg:
                        await self._reply_local(msg)
                    return
            except Exception as e:
                self.sig_error.emit(f"Skill error: {e}")

            # 2. Screen capture — skipped if sensitive window (password manager etc.)
            #
            # ALSO skipped for "who is X" / "tell me about X" identity questions:
            # OpenAI + Claude refuse to identify people in screenshots even when
            # the answer is in their training data ("Sorry I can't identify the
            # person in images"). Stripping the screenshot lets the LLM answer
            # from training data + web search instead, which is what the user
            # actually wants when they ask "who is MrBeast" while on YouTube.
            sensitive = self._privacy_guard and is_sensitive_window(title)
            identity_q = is_identity_question(transcript)
            if sensitive or identity_q or is_greeting(transcript):
                screenshots = []
                images_b64 = []
            else:
                screenshots = capture_all_screens()
                images_b64 = [s.base64_jpeg for s in screenshots]
            # Fresh question → wipe the previous lesson's drawings and remember
            # this turn's screenshots for coordinate mapping.
            self._screens_ctx = screenshots
            self.sig_clear_drawings.emit()

            # Local figure detection (OpenCV) — finds triangles/rects/circles
            # with EXACT normalized vertices so any LLM (even small Ollama
            # models) can draw on them accurately by echoing the numbers.
            self._figures_ctx = []
            fig_extra = ""
            if screenshots:
                try:
                    from ai.figure_detector import detect_figures, figures_prompt
                    self._figures_ctx = await asyncio.to_thread(
                        detect_figures, screenshots[0].base64_jpeg,
                    )
                    fig_extra = figures_prompt(self._figures_ctx)
                except Exception:
                    self._figures_ctx = []

            # 3. Parallel side-work: web search + element locator
            #
            # Pointing now works for EVERY provider:
            #   • If ANTHROPIC_API_KEY is set → use Claude Computer Use
            #     (~5px accuracy, gold standard).
            #   • Otherwise → universal grid-based locator with the active
            #     vision LLM (Copilot GPT-4o, OpenAI, Gemini, Ollama llava).
            #     ~25-50px accuracy. Good enough for buttons/menus/icons.
            locate_triggered = is_locate(transcript)
            multistep = is_multistep(transcript)

            search_task = None
            locate_task = None
            if self._web_search_enabled and needs_web_search(transcript):
                from ai.web_search import search
                search_task = asyncio.create_task(search(transcript))

            if screenshots and locate_triggered:
                shot = screenshots[0]
                # Pointing accuracy upgrade: try the hybrid pointer first.
                # Tier 1 (UIA tree) is ~5ms and pixel-perfect; tier 2 (OCR)
                # handles canvas apps. Falls through to the vision LLM grid
                # below only when both whiff.
                try:
                    from ai.hybrid_pointer import find_target as _hybrid_find
                    target = _hybrid_find(
                        transcript,
                        screenshot=shot,
                        llm_provider=self._get_llm(),
                    )
                except Exception:
                    target = None

                if target is not None and target.source in ("uia", "ocr"):
                    # UIA / OCR coordinates are PHYSICAL pixels; the overlay
                    # draws in LOGICAL pixels — divide by the DPI scale.
                    # (Also: return an object with .x/.y — downstream code
                    # accesses attributes, a bare tuple would crash it.)
                    from types import SimpleNamespace
                    _scale = shot.dpi_scale or 1.0
                    _pt = SimpleNamespace(x=target.x / _scale,
                                          y=target.y / _scale)
                    async def _ready(pt=_pt):
                        return pt
                    locate_task = asyncio.create_task(_ready())
                elif cfg.anthropic_api_key:
                    # Path A — Anthropic Computer Use (best accuracy)
                    from ai.element_locator import detect_element
                    locate_task = asyncio.create_task(detect_element(
                        screenshot_jpeg_b64=shot.base64_jpeg,
                        original_width=shot.width,
                        original_height=shot.height,
                        physical_width=shot.physical_width,
                        physical_height=shot.physical_height,
                        physical_left=shot.physical_left,
                        physical_top=shot.physical_top,
                        dpi_scale=shot.dpi_scale,
                        screen_index=shot.index,
                        user_question=transcript,
                    ))
                else:
                    # Path B — Universal grid locator (any vision LLM)
                    try:
                        from ai.universal_locator import detect_element_universal
                        llm = self._get_llm()
                        locate_task = asyncio.create_task(detect_element_universal(
                            llm=llm,
                            screenshot_jpeg_b64=shot.base64_jpeg,
                            original_width=shot.width,
                            original_height=shot.height,
                            physical_width=shot.physical_width,
                            physical_height=shot.physical_height,
                            physical_left=shot.physical_left,
                            physical_top=shot.physical_top,
                            dpi_scale=shot.dpi_scale,
                            screen_index=shot.index,
                            user_question=transcript,
                            model=self._current_model,
                        ))
                    except Exception:
                        # Universal locator should never crash the main flow
                        locate_task = None

            search_results = ""
            if search_task:
                try:
                    search_results = await search_task or ""
                except Exception:
                    search_results = ""

            detected = None
            detected_coord = None
            if locate_task:
                try:
                    detected = await locate_task
                except Exception:
                    detected = None
            if detected:
                # Short label guess — first noun phrase after "the"/"where"
                label = _guess_label(transcript)
                # Prompt wants NORMALIZED 0-1000 coords (the model echoes them
                # into [POINT:...] which _parse_points denormalizes back).
                ndx, ndy = self._norm(detected.x, detected.y)
                detected_coord = (ndx, ndy, label)
                # Fire the overlay NOW so the buddy flies over while the LLM
                # still thinks. Hold dwell until TTS completes.
                self.sig_point_hold.emit(True)
                pointing_held = True
                self.sig_point_at.emit(
                    float(detected.x), float(detected.y), label,
                )

            # ── Per-turn enrichment: code mode, language, OCR, attached docs ──
            code_active = self._code_mode_auto and code_mode.is_code_window(title)
            if cfg.response_language:
                lang_code = cfg.response_language   # user-forced — always wins
            else:
                lang_code = (multilang.detect_language(transcript)
                             if self._multilang else "en")

            # OCR fallback for fine print (only if user actually asks to read)
            ocr_extra = ""
            if self._ocr_enabled and screenshots and ocr.needs_ocr(transcript):
                try:
                    import base64
                    jpeg = base64.b64decode(screenshots[0].base64_jpeg)
                    txt = ocr.run_ocr(jpeg)
                    if txt:
                        ocr_extra = ocr.format_for_prompt(txt)
                except Exception:
                    pass

            # Attached documents (drag-dropped PDFs etc.)
            doc_extra = ""
            for fname, text in self._attached_docs:
                doc_extra += pdf_context.format_for_prompt(fname, text)

            # 4. Build system prompt with all context
            system = _build_system_prompt(
                window_title=title,
                lesson_step=self._lesson_step_idx,
                total_steps=len(self._lesson_steps),
                quiz_mode=self._quiz_mode,
                detected_coord=detected_coord,
                code_active=code_active,
                language_code=lang_code,
                extra=ocr_extra + doc_extra + fig_extra,
            )
            if is_greeting(transcript):
                system += "\nThis is a greeting: answer naturally and briefly without a task plan or action tags."
            elif not needs_web_search(transcript):
                system += "\nThis request is about the visible interface. Give a direct explanation; plan further steps only if the user asked for them."
            if sensitive:
                system += (
                    "\n\nPRIVACY GUARD: the user's active window looks sensitive "
                    "(password manager, banking, login). I did NOT take a "
                    "screenshot. Answer from memory only, and tell the user you "
                    "skipped the screenshot for safety.\n"
                )
            if search_results:
                from ai.web_search import build_search_context
                system += build_search_context(search_results)

            # Use per-app history so context doesn't bleed between apps
            history = self._app_memory.setdefault(ak, [])

            narrator = self._start_narration(lang_code)
            generation_started = time.monotonic()
            first_chunk = True

            # 5. Stream LLM — buffer partial [POINT:...] tags so they never leak
            full_response = ""
            display_buf = ""
            self._cancel_flag = False
            async for chunk in self._get_llm().stream_response(
                user_text=transcript,
                screenshots_b64=images_b64,
                history=history,
                system_prompt=system,
                model=self._current_model,
            ):
                if self._cancel_flag:
                    break
                if first_chunk:
                    _log.info("Response first token: %.2fs", time.monotonic() - generation_started)
                    first_chunk = False
                full_response += chunk
                if narrator:
                    narrator.feed(chunk)
                display_buf += chunk
                self._parse_points(display_buf)
                display_buf = ANY_TAG_RE.sub("", display_buf)
                m = ANY_PARTIAL_RE.search(display_buf)
                if m:
                    flush = display_buf[: m.start()]
                    display_buf = display_buf[m.start():]
                else:
                    flush = display_buf
                    display_buf = ""
                if flush:
                    self.sig_response_chunk.emit(flush)
            if display_buf:
                self.sig_response_chunk.emit(ANY_TAG_RE.sub("", display_buf))
            # Safety net: parse full response for any tags
            self._parse_points(full_response)

            # 6. Update per-app history
            history.append(Message(role="user", content=transcript))
            history.append(Message(role="assistant", content=full_response))
            self._app_memory[ak] = history[-20:]
            self._recent_conversation = history[-12:]

            # Multistep: parse numbered steps for later "next" invocations
            if multistep and not self._lesson_steps:
                steps = _split_steps(full_response)
                if len(steps) > 1:
                    self._lesson_steps = steps
                    self._lesson_step_idx = 0

            clean = ANY_TAG_RE.sub("", full_response).strip()
            self.sig_response_done.emit(clean)
            self._last_response = clean   # for "say it again"

            # Log to knowledge journal (skipped in quiz mode — those Q&As aren't
            # study material)
            if self._journal_enabled and not self._quiz_mode:
                try:
                    journal.log_qa(
                        question=transcript, answer=clean,
                        app_key=ak, window_title=title,
                        provider=cfg.llm_provider(),
                        model=self._current_model or "",
                    )
                except Exception:
                    pass

            # Lesson recorder gets the Q&A in transcript.md
            if self._recorder and self._recorder.is_recording:
                self._recorder.log_question(transcript)
                self._recorder.log_answer(clean)

            # Live-collab broadcast
            if self._collab and self._collab.code:
                try:
                    await self._collab.send({
                        "type": "qa", "q": transcript, "a": clean,
                    })
                except Exception:
                    pass

            # Audio is already playing as sentences arrive. Drain only the
            # remaining speech; never replay the complete answer at the end.
            if narrator and not self._cancel_flag:
                self._emit_state(AppState.SPEAKING)
                await narrator.finish()

        except Exception as e:
            # Log the traceback, not just the message. The toast shows one
            # line, so discarding the stack here left bug reporters with
            # nothing to go on — issue #15 was reported as a bare
            # "'NoneType' object has no attribute 'write'" with no way to
            # tell which call produced it.
            _log.exception("request failed: %s", e)
            self.sig_error.emit(str(e))

        finally:
            if narrator:
                await narrator.abort()
                if getattr(self, '_narration', None) is narrator:
                    self._narration = None
            if pointing_held:
                self.sig_point_release.emit()
            self._emit_state(AppState.IDLE)

    def _start_narration(self, language_code=None):
        if not self._tts_enabled:
            return None
        from audio.streaming_narration import StreamingNarrator, clean_text
        from audio.playback import decode_mp3_to_pcm, play_pcm_async, drain_audio_async, stop_audio
        try:
            tts = self._get_tts()
        except Exception as exc:
            _log.warning('Voice initialization failed: %s', exc)
            self.sig_status_text.emit('Voice unavailable — continuing with text')
            return None
        if language_code and (self._multilang or cfg.response_language):
            stable = language_code if cfg.response_language else self._stable_voice_language(language_code)
            if stable != self._applied_voice_lang and hasattr(tts, 'set_voice'):
                tts.set_voice(multilang.voice_for(stable))
                self._applied_voice_lang = stable
        started = time.monotonic()
        first_audio = True

        async def prepare(raw):
            if self._cancel_flag or not self._tts_enabled:
                return None
            synthesize = getattr(tts, 'synthesize', None)
            if callable(synthesize) and not self._extract_shapes(raw):
                audio = await asyncio.wait_for(synthesize(_speakable(clean_text(raw))), 30)
                if not audio:
                    return None
                return await asyncio.to_thread(decode_mp3_to_pcm, audio)
            return None

        async def speak(raw, audio):
            nonlocal first_audio
            if self._cancel_flag or not self._tts_enabled:
                return
            if first_audio:
                _log.info("Response first audio ready: %.2fs", time.monotonic() - started)
                first_audio = False
            if audio is not None:
                await play_pcm_async(*audio, drain=False)
            else:
                await self._play_lesson(raw, clean_text(raw))

        def failed(exc):
            _log.warning('Streaming voice unavailable: %s', exc)
            self.sig_status_text.emit('Voice unavailable — continuing with text')

        self._narration = StreamingNarrator(prepare, speak, failed, stop_audio, drain_audio_async)
        return self._narration

    def _cancel_narration(self):
        narrator = getattr(self, '_narration', None)
        if narrator and self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(narrator.cancel)

    async def _reply_local(self, msg: str):
        """Show + speak a message that doesn't need an LLM round-trip."""
        self.sig_response_chunk.emit(msg)
        self.sig_response_done.emit(msg)
        self._last_response = msg
        if self._tts_enabled:
            self._emit_state(AppState.SPEAKING)
            try:
                await self._get_tts().speak(msg)
            except Exception:
                pass
        self._emit_state(AppState.IDLE)

    async def _spaced_review(self):
        """SR-style review: pick due entries from the journal, ask one back."""
        due = journal.due_for_review(limit=1)
        if not due:
            await self._reply_local(
                "Nothing due for review right now — keep learning, I'll quiz "
                "you in a few days."
            )
            return
        entry = due[0]
        msg = f"Review: {entry['question']}"
        # Mark "correct" optimistically — a real implementation would wait for
        # the user's answer and grade it. Stubbed: reschedule based on streak.
        try:
            journal.mark_reviewed(int(entry["id"]), correct=True)
        except Exception:
            pass
        await self._reply_local(msg)

    async def _advance_lesson_step(self, ak: str):
        """User said 'next' — re-render the stored next lesson step via TTS,
        no new LLM round-trip needed."""
        self._lesson_step_idx += 1
        if self._lesson_step_idx >= len(self._lesson_steps):
            msg = "That's the last step — you're done!"
            self._lesson_steps = []
            self._lesson_step_idx = 0
        else:
            step = self._lesson_steps[self._lesson_step_idx]
            total = len(self._lesson_steps)
            msg = f"Step {self._lesson_step_idx + 1} of {total}: {step}"

        self.sig_response_chunk.emit(msg)
        self.sig_response_done.emit(msg)
        self._emit_state(AppState.SPEAKING)
        try:
            await self._get_tts().speak(msg)
        except Exception:
            pass
        self._emit_state(AppState.IDLE)

    # ── Coordinate mapping ────────────────────────────────────────────────────
    #
    # The LLM emits NORMALIZED 0-1000 coordinates relative to the screenshot
    # it saw. The overlay draws in LOGICAL screen pixels. These helpers convert
    # between the two using the ScreenShot metadata captured this turn.

    def _shot(self, screen_idx: int = 1):
        for s in self._screens_ctx:
            if s.index == screen_idx:
                return s
        return self._screens_ctx[0] if self._screens_ctx else None

    def _denorm(self, nx: float, ny: float, screen_idx: int = 1):
        """Normalized 0-1000 (screenshot space) → logical screen pixels."""
        shot = self._shot(screen_idx)
        if shot is None:
            return float(nx), float(ny)
        log_w = shot.physical_width / shot.dpi_scale
        log_h = shot.physical_height / shot.dpi_scale
        # Legacy safety: values beyond 1000 are raw pixels in the downscaled
        # JPEG the model saw — scale by the JPEG dimensions instead.
        bx = 1000.0 if (nx <= 1000 and ny <= 1000) else float(max(shot.width, 1))
        by = 1000.0 if (nx <= 1000 and ny <= 1000) else float(max(shot.height, 1))
        x = shot.logical_left + (nx / bx) * log_w
        y = shot.logical_top + (ny / by) * log_h
        return x, y

    def _denorm_len(self, n: float, screen_idx: int = 1) -> float:
        """Normalized length (0-1000 x-units) → logical pixels."""
        shot = self._shot(screen_idx)
        if shot is None:
            return float(n)
        return (n / 1000.0) * (shot.physical_width / shot.dpi_scale)

    def _norm(self, x: float, y: float, screen_idx: int = 1):
        """Logical screen pixels → normalized 0-1000 (for prompt injection)."""
        shot = self._shot(screen_idx)
        if shot is None:
            return int(x), int(y)
        log_w = shot.physical_width / shot.dpi_scale
        log_h = shot.physical_height / shot.dpi_scale
        nx = (x - shot.logical_left) / max(log_w, 1) * 1000
        ny = (y - shot.logical_top) / max(log_h, 1) * 1000
        return int(round(nx)), int(round(ny))

    def _resolve_anchor(self, name: str):
        """Resolve '@element name' → logical bbox via UIA (fast tier only)."""
        try:
            from ai.hybrid_pointer import find_target
            t = find_target(name, skip_ocr=True, skip_vision=True)
            if t is None:
                return None
            shot = self._shot(1)
            scale = (shot.dpi_scale if shot else 1.0) or 1.0
            l, tp, r, b = t.bbox
            return (l / scale, tp / scale, r / scale, b / scale)
        except Exception:
            return None

    def _parse_points(self, text: str):
        """Live-during-stream tags: pointing, actions, and board-clear."""
        for match in POINT_RE.finditer(text):
            x, y, label, scr = match.groups()
            lx, ly = self._denorm(float(x), float(y), int(scr))
            self.sig_point_at.emit(lx, ly, label.strip())
        if CLEAR_RE.search(text):
            self.sig_clear_drawings.emit()
        for match in EXEC_RE.finditer(text):
            cmd = match.group(1).strip()
            self._execute_action_cmd(cmd)
        for match in TYPE_RE.finditer(text):
            typed_text = match.group(1)
            self._type_action_text(typed_text)
        for match in KEY_RE.finditer(text):
            combo = match.group(1).strip()
            self._key_action(combo)
        for match in CLICK_COORD_RE.finditer(text):
            x, y = match.groups()
            self._click_coord_action(float(x), float(y))
        if CLICK_RE.search(text):
            self._click_action()

    def _execute_action_cmd(self, cmd: str):
        from speech_policy import external_speech_command
        if external_speech_command(cmd):
            note = 'System speech was not launched. Put the reading text in your response once; Lashy handles voice and mute. Then finish the task.'
            self.sig_status_text.emit('Using built-in voice instead of system speech')
            return note
        if not hasattr(self, "_executed_cmds"):
            self._executed_cmds = set()
        if cmd in self._executed_cmds:
            return
        self._executed_cmds.add(cmd)
        _log.info("Executing action command: %s", cmd)
        try:
            import subprocess
            subprocess.Popen(cmd, shell=True, start_new_session=True)
        except Exception as e:
            _log.warning("Failed to execute action command '%s': %s", cmd, e)

    def _type_action_text(self, text: str):
        if not hasattr(self, "_executed_types"):
            self._executed_types = set()
        if text in self._executed_types:
            return
        self._executed_types.add(text)
        _log.info("Typing action text: %s", text)
        target_wid = getattr(self, "_target_window_id", "")
        if target_wid:
            try:
                from tutor import activate_window
                activate_window(target_wid)
                time.sleep(0.05)
            except Exception:
                pass
        try:
            import subprocess
            res = subprocess.run(["xdotool", "type", "--delay", "8", "--", text], timeout=3.0)
            if res.returncode == 0:
                return
        except Exception:
            pass
        try:
            from pynput.keyboard import Controller
            Controller().type(text)
        except Exception as e:
            _log.warning("Failed to type action text: %s", e)

    def _key_action(self, combo: str):
        if not hasattr(self, "_executed_keys"):
            self._executed_keys = set()
        if combo in self._executed_keys:
            return
        self._executed_keys.add(combo)
        _log.info("Sending key combo: %s", combo)
        target_wid = getattr(self, "_target_window_id", "")
        if target_wid:
            try:
                from tutor import activate_window
                activate_window(target_wid)
                time.sleep(0.05)
            except Exception:
                pass
        try:
            import subprocess
            subprocess.run(["xdotool", "key", combo], timeout=1.0)
        except Exception as e:
            _log.warning("Failed to send key '%s': %s", combo, e)

    def _click_action(self):
        try:
            import subprocess
            res = subprocess.run(["xdotool", "click", "1"], timeout=1.0)
            if res.returncode == 0:
                return
        except Exception:
            pass
        try:
            from pynput.mouse import Controller, Button
            Controller().click(Button.left)
        except Exception as e:
            _log.warning("Failed to click: %s", e)

    def _click_coord_action(self, nx: float, ny: float):
        if not hasattr(self, "_executed_clicks"):
            self._executed_clicks = set()
        coord_key = (round(float(nx), 1), round(float(ny), 1))
        if coord_key in self._executed_clicks:
            return
        self._executed_clicks.add(coord_key)
        try:
            lx, ly = self._denorm(nx, ny, 1)
            _log.info("Clicking at normalized (%.1f, %.1f) -> screen (%d, %d)", nx, ny, int(lx), int(ly))
            target_wid = getattr(self, "_target_window_id", "")
            if target_wid:
                try:
                    from tutor import activate_window
                    activate_window(target_wid)
                    time.sleep(0.04)
                except Exception:
                    pass
            import subprocess
            res = subprocess.run(["xdotool", "mousemove", str(int(lx)), str(int(ly)), "click", "1"], timeout=1.0)
            if res.returncode == 0:
                return
        except Exception:
            pass
        try:
            from pynput.mouse import Controller, Button
            lx, ly = self._denorm(nx, ny, 1)
            Controller().position = (lx, ly)
            Controller().click(Button.left)
        except Exception as e:
            _log.warning("Failed to click coord: %s", e)

    # ── Vertex snapping (figure-detector assisted accuracy) ─────────────────

    def _snap_pt(self, nx: float, ny: float, thresh: float = 35.0):
        """Snap a normalized point to the nearest detected-figure vertex."""
        best, bd = None, thresh
        for fig in self._figures_ctx:
            for (vx, vy) in fig.vertices:
                d = math.hypot(nx - vx, ny - vy)
                if d < bd:
                    bd, best = d, (float(vx), float(vy))
        return best if best is not None else (nx, ny)

    def _angle_rot_for_vertex(self, nx: float, ny: float):
        """Rotation (deg) that puts a right-angle marker INSIDE the detected
        polygon at vertex (nx,ny), aligned with its two edges. None if the
        point is not a detected vertex."""
        for fig in self._figures_ctx:
            verts = fig.vertices
            if len(verts) < 3:
                continue
            for i, (vx, vy) in enumerate(verts):
                if math.hypot(nx - vx, ny - vy) > 6:
                    continue
                P = self._denorm(vx, vy)
                A = self._denorm(*verts[i - 1])
                B = self._denorm(*verts[(i + 1) % len(verts)])
                a1 = math.degrees(math.atan2(A[1] - P[1], A[0] - P[0]))
                a2 = math.degrees(math.atan2(B[1] - P[1], B[0] - P[0]))
                rot = a1
                # Marker spans [rot, rot+90]; flip if the second edge sits on
                # the other side so the square lies between the two edges.
                if (a2 - rot) % 360 > 180:
                    rot -= 90
                return rot
        return None

    # ── Drawing-tag extraction (deferred, order-preserving) ──────────────────

    def _shape_from_tag(self, tag: str):
        m = CIRCLE_AT_RE.fullmatch(tag)
        if m:
            name, color = m.groups()
            bbox = self._resolve_anchor(name.strip())
            if not bbox:
                return None
            l, t, r, b = bbox
            return {"kind": "circle", "x": (l + r) / 2, "y": (t + b) / 2,
                    "r": max(r - l, b - t) / 2 + 10, "label": "",
                    "color": color or "blue"}
        m = UNDERLINE_AT_RE.fullmatch(tag)
        if m:
            name, color = m.groups()
            bbox = self._resolve_anchor(name.strip())
            if not bbox:
                return None
            l, t, r, b = bbox
            return {"kind": "underline", "x": l, "y": b + 3, "w": r - l,
                    "color": color or "blue"}
        m = LINE_RE.fullmatch(tag) or ARROW_RE.fullmatch(tag)
        if m:
            kind = "line" if tag.startswith("[LINE") else "arrow"
            x1, y1, x2, y2, color = m.groups()
            n1 = self._snap_pt(float(x1), float(y1))
            n2 = self._snap_pt(float(x2), float(y2))
            return {"kind": kind, "pts": [self._denorm(*n1), self._denorm(*n2)],
                    "color": color or "blue"}
        m = CIRCLE_RE.fullmatch(tag)
        if m:
            x, y, r, label, color = m.groups()
            cx, cy = self._denorm(float(x), float(y))
            return {"kind": "circle", "x": cx, "y": cy,
                    "r": max(12.0, self._denorm_len(float(r))),
                    "label": (label or "").strip(), "color": color or "blue"}
        m = RECT_RE.fullmatch(tag)
        if m:
            x1, y1, x2, y2, color = m.groups()
            p1 = self._denorm(*self._snap_pt(float(x1), float(y1)))
            p2 = self._denorm(*self._snap_pt(float(x2), float(y2)))
            return {"kind": "rect", "x1": p1[0], "y1": p1[1],
                    "x2": p2[0], "y2": p2[1], "color": color or "blue"}
        m = POLY_RE.fullmatch(tag)
        if m:
            pts_str, color = m.groups()
            pts = [self._denorm(*self._snap_pt(float(a), float(b)))
                   for a, b in re.findall(r'(\d+),(\d+)', pts_str)]
            if len(pts) < 3:
                return None
            return {"kind": "poly", "pts": pts, "color": color or "blue"}
        m = TEXT_RE.fullmatch(tag)
        if m:
            x, y, content, color, size = m.groups()
            lx, ly = self._denorm(float(x), float(y))
            return {"kind": "text", "x": lx, "y": ly, "text": content.strip(),
                    "color": color or "blue", "size": size or "m"}
        m = ANGLE_RE.fullmatch(tag)
        if m:
            x, y, s, rot, color = m.groups()
            nx, ny = self._snap_pt(float(x), float(y))
            auto_rot = self._angle_rot_for_vertex(nx, ny)
            lx, ly = self._denorm(nx, ny)
            return {"kind": "angle", "x": lx, "y": ly,
                    "s": max(10.0, self._denorm_len(float(s))),
                    "rot": auto_rot if auto_rot is not None else float(rot or 0),
                    "color": color or "blue"}
        m = UNDERLINE_RE.fullmatch(tag)
        if m:
            x, y, w, color = m.groups()
            lx, ly = self._denorm(float(x), float(y))
            return {"kind": "underline", "x": lx, "y": ly,
                    "w": max(8.0, self._denorm_len(float(w))),
                    "color": color or "blue"}
        m = LABEL_RE.fullmatch(tag)
        if m:
            x, y, txt, color = m.groups()
            lx, ly = self._denorm(float(x), float(y))
            return {"kind": "text", "x": lx, "y": ly, "text": txt.strip(),
                    "color": color or "blue", "size": "s"}
        return None

    def _extract_shapes(self, text: str) -> list:
        """All drawing shapes in a piece of text, in document order."""
        shapes = []
        for m in ANY_TAG_RE.finditer(text):
            try:
                sh = self._shape_from_tag(m.group(0))
            except Exception:
                sh = None
            if sh:
                shapes.append(sh)
        return shapes

    # ── Teacher-style narrated playback ──────────────────────────────────────

    def _segment_lesson(self, full_response: str) -> list:
        """Split a response into (sentence, [shapes]) pairs, preserving which
        sentence each drawing tag belongs to."""
        tags: list[str] = []

        def _stash(m):
            tags.append(m.group(0))
            return f"\x00{len(tags) - 1}\x00"

        masked = ANY_TAG_RE.sub(_stash, full_response)
        parts = re.split(r'(?<=[.!?])\s+', masked)
        out = []
        for part in parts:
            ids = [int(i) for i in re.findall(r'\x00(\d+)\x00', part)]
            shapes = self._extract_shapes("".join(tags[i] for i in ids))
            clean = re.sub(r'\s+', ' ', re.sub(r'\x00\d+\x00', ' ', part)).strip()
            if clean or shapes:
                out.append((clean, shapes))
        return out

    async def _play_lesson(self, full_response: str, clean: str):
        """Narrate sentence by sentence, drawing each sentence's shapes as it
        is spoken — the cadence of a teacher at a whiteboard. Falls back to
        plain TTS when the response contains no drawings."""
        segments = self._segment_lesson(full_response)
        if not any(shapes for _, shapes in segments):
            await self._get_tts().speak(_speakable(clean))
            return

        try:
            from ui.overlay import (
                _shape_length, STROKE_SPEED_PX_S,
                SHAPE_DRAW_MIN_S, SHAPE_DRAW_MAX_S, SHAPE_GAP_SECONDS,
            )
        except Exception:
            _shape_length = None

        draw_end = time.monotonic()
        for text, shapes in segments:
            if self._cancel_flag:
                break
            draw_end = max(draw_end, time.monotonic())
            for sh in shapes:
                self.sig_draw.emit(sh)
                if _shape_length is not None:
                    dur = _shape_length(sh) / STROKE_SPEED_PX_S
                    dur = max(SHAPE_DRAW_MIN_S, min(SHAPE_DRAW_MAX_S, dur))
                    draw_end += dur + SHAPE_GAP_SECONDS
                else:
                    draw_end += 1.0
            if text:
                try:
                    await self._get_tts().speak(_speakable(text))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
            # A real teacher finishes the stroke before the next sentence —
            # wait out any drawing time the narration didn't cover.
            remaining = draw_end - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(remaining, 4.0) + 0.1)

    def _stable_voice_language(self, detected: str) -> str:
        """Debounce language detection before it is allowed to change the voice.

        A pinned reply language always wins. Otherwise a new language has to
        show up twice in a row to take effect, so a single misdetection — very
        common on code-mixed speech like Hinglish — no longer swaps the accent
        for one answer and swaps it back on the next.
        """
        if cfg.response_language:
            return cfg.response_language
        if detected == self._voice_lang:
            self._pending_lang = ""
        elif self._pending_lang == detected:
            self._voice_lang = detected   # seen twice running — adopt it
            self._pending_lang = ""
        else:
            self._pending_lang = detected  # first sighting — hold the voice
        return self._voice_lang

    def _emit_state(self, state: AppState):
        self._state = state
        self.sig_state_changed.emit(state)

    # ── Settings ──────────────────────────────────────────────────────────────

    def set_model(self, model: str):
        self._current_model = None if model == "default" else model
        cfg.set_selected_model(model)

    def set_active_provider(self, name: str):
        """Runtime switch between claude / openai / copilot / gemini / ollama."""
        cfg.set_active_llm(name)
        self._retire_llm()         # force re-init on next query, close old pool
        self._current_model = cfg.selected_model(name) or None
        # If switching to Copilot and the cached model list is stale (or
        # missing), refresh it in the background so the panel shows the
        # *current* set of models GitHub offers — not stale hardcoded ones.
        if name == "copilot":
            try:
                from ai.github_copilot_provider import cache_is_stale
                if cache_is_stale():
                    self._submit(self._refresh_copilot_models())
            except Exception:
                pass
        elif name in ("claude", "openai", "gemini"):
            try:
                from ai.model_registry import cache_is_stale as _stale
                if _stale(name):
                    self._submit(self._refresh_one_model_list(name))
            except Exception:
                pass
        elif name == "ollama":
            # Surface installed models in the tray immediately
            self.refresh_ollama_models()

    async def _refresh_one_model_list(self, provider: str):
        try:
            from ai.model_registry import refresh
            ms = await refresh(provider)
            self.sig_models_refreshed.emit(provider, len(ms))
        except Exception as e:
            self.sig_error.emit(f"{provider} model refresh failed: {e}")

    def refresh_copilot_models(self):
        """Public — bound to the tray 'Refresh Copilot models' action."""
        self._submit(self._refresh_copilot_models())

    async def _refresh_copilot_models(self):
        try:
            from ai.github_copilot_provider import refresh_models_to_cache
            models = await refresh_models_to_cache()
            self.sig_copilot_models_done.emit(len(models))
        except Exception as e:
            self.sig_error.emit(f"Copilot model refresh failed: {e}")

    # ── Ollama model management ──────────────────────────────────────────────

    def refresh_ollama_models(self):
        """Public — kick off async poll of /api/tags. Result via sig_ollama_models."""
        self._submit(self._refresh_ollama_models())

    async def _refresh_ollama_models(self):
        try:
            from ai.ollama_provider import OllamaProvider
            classified = await OllamaProvider().list_models_classified()
            self.sig_ollama_models.emit(classified)
        except Exception as e:
            self.sig_error.emit(f"Ollama model list failed: {e}")

    def set_ollama_model(self, kind: str, name: str):
        """Tray callback — update the active vision/text model. No restart needed."""
        cfg.set_ollama_model(kind, name)
        # Force the provider instance to re-read cfg on next call
        if cfg.llm_provider() == "ollama":
            self._retire_llm()

    def set_custom_instructions(self, text: str):
        """Tray callback — restrict/steer what Lashy helps with. Persists
        to .env so it survives a restart, not just this session."""
        cfg.custom_instructions = text.strip()
        try:
            from dotenv import set_key
            env_path = Path(__file__).parent / ".env"
            if not env_path.exists():
                env_path.touch()
            escaped = text.strip().replace("\n", "\\n")
            set_key(str(env_path), "CUSTOM_INSTRUCTIONS", escaped)
        except Exception as e:
            self.sig_error.emit(f"Could not save instructions: {e}")

    def set_stt_language(self, code: str):
        """Pin the language Whisper transcribes in. "" = auto-detect.

        Auto-detect re-decides on every single utterance, which is unreliable
        for code-mixed speech: Hinglish flips between Hindi and English turn to
        turn, and the transcript quality swings with it. Pinning one language
        makes transcription consistent.
        """
        code = (code or "").strip()
        cfg.whisper_language = code
        try:
            from dotenv import set_key
            env_path = cfg.env_path()
            if not env_path.exists():
                env_path.touch()
            set_key(str(env_path), "WHISPER_LANGUAGE", code)
        except Exception:
            pass
        # Drop the cached model so the next transcription picks the new setting
        self._stt = None

    def set_response_language(self, code: str):
        """Tray callback — pin Lashy's reply language ('' = auto-detect)."""
        cfg.response_language = code
        try:
            from dotenv import set_key
            env_path = Path(__file__).parent / ".env"
            if not env_path.exists():
                env_path.touch()
            set_key(str(env_path), "RESPONSE_LANGUAGE", code)
        except Exception as e:
            self.sig_error.emit(f"Could not save language setting: {e}")

    def set_mic_device(self, device_index: int):
        """Tray callback — switch input device without restarting the app."""
        cfg.mic_device_index = device_index if device_index >= 0 else None
        try:
            self._listener.stop()
        except Exception:
            pass
        from audio.ambient_listener import AmbientListener
        self._listener = AmbientListener(
            on_level=self._handle_level,
            on_wake=self._handle_wake,
            device=cfg.mic_device_index,
            ambient=cfg.ambient_mic(),
        )
        try:
            self._listener.start()
        except Exception as e:
            self.sig_error.emit(f"Could not start mic: {e}")

    def pull_ollama_model(self, name: str):
        """Trigger `ollama pull <name>` in the background. Status via sig_ollama_pull_status."""
        self._submit(self._pull_ollama_model(name))

    async def _pull_ollama_model(self, name: str):
        from ai.ollama_models_registry import pull_model
        self.sig_ollama_pull_status.emit(name, f"Pulling {name}…")

        def _progress(msg: str):
            if msg:
                self.sig_ollama_pull_status.emit(name, msg)

        ok = await pull_model(name, cfg.ollama_host, on_progress=_progress)
        if ok:
            self.sig_ollama_pull_status.emit(name, f"✓ {name} ready")
            # Refresh the installed list so the tray menu picks it up
            await self._refresh_ollama_models()
        else:
            self.sig_ollama_pull_status.emit(name, f"✗ Pull failed for {name}")

    def set_web_search(self, enabled: bool):
        self._web_search_enabled = enabled

    def set_wake_word(self, enabled: bool):
        """Tray toggle — switches between always-listening and hotkey-only.

        ON keeps the mic open and runs a Whisper pass over everything it hears
        to catch "Lashy". OFF (the default) leaves the mic closed until you
        press the hotkey, which is both quieter on CPU and less invasive.
        """
        cfg.set_mic_mode("ambient" if enabled else "hotkey")
        self._listener.set_wake_word_enabled(enabled)
        self._listener.set_ambient(enabled)

    def set_slow_mode(self, enabled: bool):
        self._slow_mode = enabled

    def set_quiz_mode(self, enabled: bool):
        was = self._quiz_mode
        self._quiz_mode = enabled
        if enabled and not was:
            # Kick off the first question immediately so the user doesn't
            # have to ask "begin quiz". Uses the active screen as context.
            self._submit(self._kickoff_quiz())

    async def _kickoff_quiz(self):
        """Called when quiz mode flips ON — generates the first question
        without waiting for a user utterance."""
        if self._state != AppState.IDLE:
            return
        from audio.playback import begin_audio_session
        begin_audio_session()
        try:
            self._emit_state(AppState.THINKING)
            screenshots = capture_all_screens()
            images_b64 = [s.base64_jpeg for s in screenshots]
            title = active_window_title()
            system = _build_system_prompt(
                window_title=title, quiz_mode=True,
            )
            ak = app_key(title)
            history = self._app_memory.setdefault(ak, [])

            full = ""
            async for chunk in self._get_llm().stream_response(
                user_text="(quiz mode just enabled — start the quiz now)",
                screenshots_b64=images_b64,
                history=history,
                system_prompt=system,
                model=self._current_model,
            ):
                if self._cancel_flag:
                    break
                full += chunk
                self.sig_response_chunk.emit(chunk)
            self.sig_response_done.emit(full)
            self._emit_state(AppState.SPEAKING)
            try:
                await self._get_tts().speak(full)
            except Exception:
                pass
        except Exception as e:
            self.sig_error.emit(f"Quiz start failed: {e}")
        finally:
            self._emit_state(AppState.IDLE)

    def set_privacy_guard(self, enabled: bool):
        self._privacy_guard = enabled

    @property
    def slow_mode(self) -> bool:  return self._slow_mode
    @property
    def quiz_mode(self) -> bool:  return self._quiz_mode
    @property
    def privacy_guard(self) -> bool:  return self._privacy_guard

    def clear_history(self):
        self._history = []
        self._app_memory.clear()
        self._recent_conversation = []
        self._task_session = None
        self._lesson_steps = []
        self._lesson_step_idx = 0

    # ── Attached documents (drag-drop on panel) ──────────────────────────────

    def attach_document(self, path: str) -> bool:
        text = pdf_context.extract_text(path)
        if not text.strip():
            return False
        from pathlib import Path
        self._attached_docs.append((Path(path).name, text))
        # Cap context — most recent 3 docs
        self._attached_docs = self._attached_docs[-3:]
        return True

    def clear_attachments(self):
        self._attached_docs = []

    # ── Lesson recording ─────────────────────────────────────────────────────

    def start_recording(self) -> Optional[str]:
        if self._recorder is None:
            self._recorder = lesson_recorder.LessonRecorder()
        out = self._recorder.start()
        if out:
            self.sig_recording_state.emit(True, str(out))
            return str(out)
        return None

    def stop_recording(self) -> Optional[str]:
        if not self._recorder or not self._recorder.is_recording:
            return None
        out = self._recorder.stop()
        self.sig_recording_state.emit(False, str(out) if out else "")
        return str(out) if out else None

    @property
    def is_recording(self) -> bool:
        return bool(self._recorder and self._recorder.is_recording)

    # ── Workflow capture (record clicks/keystrokes) ──────────────────────────

    def workflow_start(self) -> bool:
        if self._workflow is None:
            self._workflow = workflow_capture.WorkflowCapture()
        return self._workflow.start()

    def workflow_stop(self) -> str:
        if not self._workflow:
            return ""
        events = self._workflow.stop()
        return self._workflow.summarise() if events else ""

    # ── Live collaboration ───────────────────────────────────────────────────

    def collab_start_host(self):
        """Live-session host. Disabled — see tutor_features/collab.py."""
        self.sig_error.emit(
            "Live Session: not available in this build. "
            "Requires a WebRTC signalling server (planned for a future release)."
        )

    def collab_join(self, code: str):
        """Live-session join. Disabled — see tutor_features/collab.py."""
        self.sig_error.emit(
            "Live Session: not available in this build. "
            "Requires a WebRTC signalling server (planned for a future release)."
        )

    # ── Voice picker (ElevenLabs / Edge) ─────────────────────────────────────

    def set_tts_voice(self, voice: str):
        try:
            tts = self._get_tts()
            if hasattr(tts, "set_voice"):
                tts.set_voice(voice)
        except Exception:
            pass

    # ── Toggle setters for the rest of the new features ──────────────────────

    def set_code_mode_auto(self, enabled: bool):
        self._code_mode_auto = enabled

    def set_multilang(self, enabled: bool):
        self._multilang = enabled

    def set_journal(self, enabled: bool):
        self._journal_enabled = enabled

    def set_ocr_enabled(self, enabled: bool):
        self._ocr_enabled = enabled

    # ── Stop / cancel ─────────────────────────────────────────────────────────

    def stop(self):
        """Cancel the current LLM stream + any in-flight TTS. Bound to Esc."""
        self._cancel_flag = True
        self._cancel_narration()
        self._ptt_held = False
        self._capture_id = getattr(self, "_capture_id", 0) + 1
        if self._state == AppState.LISTENING:
            self._end_claimed = True
            try:
                self._listener.stop_recording()
            except Exception:
                pass
        # Kill audio playback immediately — flips the global stop event so
        # the chunked PortAudio loop bails out within ~50 ms.
        try:
            from audio.playback import stop_audio
            stop_audio()
        except Exception:
            pass
        # Some TTS providers also have their own cancel hook
        tts = self._tts
        if tts and hasattr(tts, "stop"):
            try:
                tts.stop()
            except Exception:
                pass
        # Clear any stored lesson so "stop" really means "back to zero"
        self._lesson_steps = []
        self._lesson_step_idx = 0
        current = self._current_task
        if current is not None and not current.done() and self._loop and self._loop.is_running():
            if not getattr(self, '_stop_pending', False):
                self._stop_pending = True
                self._loop.call_soon_threadsafe(current.cancel)
            self.sig_status_text.emit('Stopping…')
        else:
            self._emit_state(AppState.IDLE)
