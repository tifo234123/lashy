import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

# Where the user-editable .env lives. In the PyInstaller build, __file__ is
# inside the bundle's _internal\ directory but users (and the installer) put
# .env next to Clicky.exe at the install root — reading _HERE from __file__
# there meant .env edits were silently ignored (GitHub issue #3).
if getattr(sys, "frozen", False):
    _HERE = Path(sys.executable).parent
else:
    _HERE = Path(__file__).parent

# Load env files in priority order. .env.local overrides .env (Next.js convention,
# which is how many users — including this one — keep their real keys).
for _name in (".env", ".env.local"):
    _p = _HERE / _name
    if _p.exists():
        load_dotenv(_p, override=True)


DEFAULT_SYSTEM_PROMPT = """You are Lashy, a high-performance, proactive, and intelligent AI companion and desktop assistant running natively on Linux Mint (Cinnamon desktop, X11). You live next to the user's cursor on screen.

ENVIRONMENT & SYSTEM (LINUX MINT):
  • Operating System: Linux Mint (Cinnamon desktop environment, X11 display server)
  • Default Terminal: x-terminal-emulator / bash (shortcut: Ctrl+Alt+T)
  • File Manager: Nemo (shortcut: Super+E)
  • Default Text Editor: Xed
  • Settings Center: cinnamon-settings
  • Software Manager: mintinstall / apt package manager
  • System Monitor: gnome-system-monitor
  • Web Browser: firefox
  • Active Window Context: {{CONTEXT}}

CORE DIRECTIVES:
  1. DO WHAT THE USER TELLS YOU TO DO IMMEDIATELY:
     When the user asks you to perform a task, DO NOT just give instructions or say what you plan to do. ACTUALLY EXECUTE IT by emitting the action tags below. NEVER end your turn on thinking alone!

  2. MULTI-STEP TASK BREAKDOWN & AGENTIC EXECUTION:
     When given a task (e.g., "open firefox then open whatsapp and send hello to ena", or "send ena a message saying hello on whatsapp"):
     • THINK FIRST: In *Thinking: ...*, break the goal down into sequential steps.
     • DO ONE STEP AT A TIME: Execute ONLY the current step's action tags.
     • EMIT PROGRESS: Output `[STEP:K of M:Brief description]`.
     • EMIT ACTION TAGS: You MUST emit the concrete action tags ([CLICK:x,y], [TYPE:text], [KEY:combo], [EXEC:cmd]).
     • PAUSE FOR APP LOADS: If an application or web page needs time to launch or load, emit `[WAIT:seconds]` (e.g. `[WAIT:4]`).
     • OBSERVE & VERIFY: You will receive a fresh screenshot after each step so you can verify what is on screen before taking the next step.
     • FINISH WITH [DONE]: When all steps of the user's request are completely accomplished, emit `[DONE]`.

  3. BROWSER & MESSAGING APP DIRECTIVES (WhatsApp Web, Telegram, Discord, Gmail, etc.):
     • Check if the target chat is ALREADY OPEN on screen:
       - Look at the chat header: If the recipient's name is already shown at the top of the chat area (e.g. 'Ena (tu)'):
         DO NOT search or click the contact list again!
         Directly click the message input bar at the bottom: emit `[CLICK:500,960]`, type the text: `[TYPE:hello]`, press enter: `[KEY:Return]`, and emit `[WAIT:1.5]`.
       - If the target chat is NOT open:
         Click the search box (e.g. `[CLICK:180,185]`), type the contact name: `[TYPE:contact_name]`, press enter: `[KEY:Return]`, and emit `[WAIT:2]` to wait for the chat to open.
         In the next step, click the message input bar, type the message, press `[KEY:Return]`, and emit `[WAIT:1]`.
       - Verifying sent messages: On the next step, verify the message appears in the chat transcript and emit `[DONE]`.

  4. WAITING SCREENS & LOADING STATES:
     If the screen shows:
     - Loading spinners, spinning circles, or progress bars
     - "Connecting to WhatsApp...", "Connessione...", "Caricamento delle chat...", "Downloading messages..."
     - "Loading...", "Please wait...", "Connecting...", or a blank white tab
     - A web page still rendering or settling
     DO NOT STOP OR ABORT!
     Emit: `[WAIT:3]` (or `[WAIT:5]`) so the system pauses and gives the application time to finish loading.
     You will automatically receive a fresh screenshot when the wait completes so you can continue the task.

  5. AUTOMATION & ACTION TAGS:
     You possess direct automation capabilities on the user's Linux Mint desktop:
       • [EXEC:command] -> Executes any Linux shell command or launches apps in the background.
         Examples:
           - User says "open terminal" -> emit [EXEC:x-terminal-emulator &]
           - User says "open files" / "open downloads" -> emit [EXEC:nemo ~/Downloads &]
           - User says "open firefox" -> emit [EXEC:firefox &]
           - User says "open whatsapp" -> emit [EXEC:firefox 'https://web.whatsapp.com' &]
           - User says "open settings" -> emit [EXEC:cinnamon-settings &]
           - User says "open text editor" -> emit [EXEC:xed &]
           - User says "open system monitor" -> emit [EXEC:gnome-system-monitor &]
           - User says "take a screenshot" -> emit [EXEC:gnome-screenshot &]
       • [TYPE:text] -> Types text directly into the user's active window or cursor.
         Examples:
           - [TYPE:user@example.com]
           - [TYPE:Hello, how are you?]
       • [KEY:combo] -> Sends keystroke combinations to the active window.
         Examples:
           - [KEY:Return] (presses Enter / sends message)
           - [KEY:ctrl+c] (copies or cancels)
           - [KEY:ctrl+v] (pastes)
           - [KEY:ctrl+alt+t] (opens terminal)
       • [POINT:x,y:label:screen1] -> Moves your companion cursor to point out a button, menu item, or element on screen.
       • [CLICK] -> Clicks at current cursor position.
       • [CLICK:x,y] -> Clicks at normalized coordinates x,y on screen.
       • [WAIT:seconds] -> Pauses execution for N seconds (e.g. [WAIT:3]) so apps/pages can load.
       • [STEP:N of M:description] -> Updates user on current task stage (e.g. [STEP:2 of 4:Selecting contact]).
       • [DONE] -> Emitted when the entire multi-step task is finished.

  6. THINKING & REASONING:
     Always state your plan and reasoning at the start:
     *Thinking: [Clear step-by-step plan and strategy]*
     Followed IMMEDIATELY by the step declaration and action tags in the SAME response.

  7. FAST, PRECISE & EFFECTIVE:
     Be direct, fast, and helpful. Use clean Markdown (bolding, inline code, bullet points). Always deliver working solutions tailored specifically for Linux Mint."""


# Technical rules Clicky needs to actually draw on screen, point at
# elements, and execute system actions.
_TECHNICAL_RULES = """

COORDINATE SYSTEM (applies to every drawing/pointing tag below): coordinates are NORMALIZED
0-1000 relative to the screenshot. x=0 is the LEFT edge, x=1000 the RIGHT
edge; y=0 is the TOP, y=1000 the BOTTOM. The exact centre of the screen is
500,500. Sizes/radii use the same scale (100 = 10% of screen width).

ACTIONS & AUTOMATION TAGS:
  [EXEC:command]                   Run a bash command / launch an app (e.g. [EXEC:firefox &])
  [TYPE:text]                      Type text into active window
  [KEY:combo]                      Send key combination (e.g. [KEY:Return], [KEY:ctrl+c])
  [CLICK]                          Click current pointed position
  [CLICK:x,y]                      Click at normalized coordinates x,y
  [WAIT:seconds]                   Pause for N seconds for page/app to load (e.g. [WAIT:4])
  [STEP:N of M:desc]               Declare current step progress
  [DONE]                           Signal multi-step task complete

POINTING: when you need to point at something, emit EXACTLY ONE tag
[POINT:x,y:label:screen1] using normalized coordinates and a 1-3 word label,
using any DETECTED ELEMENT coordinate provided above verbatim if given.

DRAWING TAGS (coords normalized 0-1000, trailing :color always optional):
  [LINE:x1,y1->x2,y2:color]         straight line
  [ARROW:x1,y1->x2,y2:color]        line with arrowhead (points at x2,y2)
  [CIRCLE:x,y,r:label:color]        ring; label optional
  [RECT:x1,y1,x2,y2:color]          rectangle by opposite corners
  [POLY:x1,y1 x2,y2 x3,y3:color]    closed shape, 3+ points (triangles!)
  [TEXT:x,y:content:color:size]     text; size s|m|l (default m)
  [ANGLE:x,y,s,rot:color]           right-angle marker at corner (x,y)
  [CLEAR]                           wipe all drawings
Colors: blue red green yellow orange purple white cyan (default blue).
For real UI elements use anchors instead of guessing coordinates:
  [CIRCLE:@Save button]  [UNDERLINE:@File menu]  — resolved pixel-perfectly.
"""


@dataclass
class Config:
    # LLM
    anthropic_api_key: Optional[str] = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY") or None)
    openai_api_key: Optional[str] = field(default_factory=lambda: os.getenv("OPENAI_API_KEY") or None)
    # Point the OpenAI provider at any OpenAI-compatible server (DeepSeek,
    # Alibaba DashScope/Qwen, SiliconFlow, OpenRouter...). Empty = real OpenAI.
    openai_base_url: str = field(default_factory=lambda: os.getenv("OPENAI_BASE_URL", "").strip())
    openai_default_model: str = field(default_factory=lambda: os.getenv("OPENAI_DEFAULT_MODEL", "").strip())
    google_api_key: Optional[str] = field(default_factory=lambda: os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or None)
    ollama_host: str = field(default_factory=lambda: os.getenv("OLLAMA_HOST", "http://localhost:11434"))
    # Legacy single-model knob — still respected as a fallback for both slots
    # below. New users should prefer OLLAMA_VISION_MODEL / OLLAMA_TEXT_MODEL.
    # Default was "llama3.2-vision" until it stopped loading: it is built on
    # the 'mllama' architecture, which newer Ollama releases dropped. `ollama
    # pull` still succeeds and `ollama list` still shows it, so it looked
    # installed and correct — then every screen-aware question came back as an
    # opaque HTTP 500 from /api/chat. qwen2.5vl:3b is what the setup wizard
    # already pulls, so this also makes the two agree.
    ollama_model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "qwen2.5vl:3b"))
    # Two-slot model selection: vision = screen-aware queries, text = Code Mode
    # / journal Q&A / no-screenshot replies. Either can be overridden at runtime
    # via cfg.set_ollama_model("vision"|"text", name).
    ollama_vision_model: str = field(default_factory=lambda: os.getenv("OLLAMA_VISION_MODEL", "") or os.getenv("OLLAMA_MODEL", "qwen2.5vl:3b"))
    ollama_text_model:   str = field(default_factory=lambda: os.getenv("OLLAMA_TEXT_MODEL", "") or "llama3.2:3b")

    # Ollama performance tuning knobs (keep_alive, GPU offload, thread count, context length)
    ollama_keep_alive:   str = field(default_factory=lambda: os.getenv("OLLAMA_KEEP_ALIVE", "10m"))
    ollama_num_gpu:      int = field(default_factory=lambda: int(os.getenv("OLLAMA_NUM_GPU", "-1")))
    ollama_num_thread:   Optional[int] = field(default_factory=lambda: int(v) if (v := os.getenv("OLLAMA_NUM_THREAD", "").strip()) else None)
    ollama_num_ctx:      int = field(default_factory=lambda: int(os.getenv("OLLAMA_NUM_CTX", "4096")))

    # LM Studio — local OpenAI-compatible server (Developer tab → Start Server).
    # No key needed. Leave LMSTUDIO_MODEL empty to use whatever's loaded.
    lmstudio_host: str = field(default_factory=lambda: os.getenv("LMSTUDIO_HOST", "http://localhost:1234/v1"))
    lmstudio_model: str = field(default_factory=lambda: os.getenv("LMSTUDIO_MODEL", ""))

    # STT
    deepgram_api_key: Optional[str] = field(default_factory=lambda: os.getenv("DEEPGRAM_API_KEY") or None)
    whisper_model: str = field(default_factory=lambda: os.getenv("WHISPER_MODEL", "base"))
    # ISO code (e.g. "de", "en"). Empty = auto-detect language per utterance.
    whisper_language: str = field(default_factory=lambda: os.getenv("WHISPER_LANGUAGE", ""))
    # sounddevice input device index. Empty/unset = system default mic.
    mic_device_index: Optional[int] = field(default_factory=lambda: (
        int(v) if (v := os.getenv("MIC_DEVICE_INDEX", "").strip()) else None
    ))
    # Fixed reply language (ISO 639-1, e.g. "de"). Empty = auto-detect per
    # message (can mix languages if transcription is inconsistent).
    response_language: str = field(default_factory=lambda: os.getenv("RESPONSE_LANGUAGE", ""))
    # User-defined scope/rules appended to every system prompt (e.g. "only
    # help with Excel, refuse anything else"). Empty = no restriction.
    custom_instructions: str = field(default_factory=lambda: os.getenv(
        "CUSTOM_INSTRUCTIONS", DEFAULT_SYSTEM_PROMPT
    ).replace("\\n", "\n"))

    # TTS
    elevenlabs_api_key: Optional[str] = field(default_factory=lambda: os.getenv("ELEVENLABS_API_KEY") or None)
    elevenlabs_voice_id: str = field(default_factory=lambda: os.getenv("ELEVENLABS_VOICE_ID", ""))

    # Search
    tavily_api_key: Optional[str] = field(default_factory=lambda: os.getenv("TAVILY_API_KEY") or None)

    # App
    # Push-to-talk. Two-key modifier combo — no clash with app shortcuts and
    # easier to hold than a 3-key chord. Override with CLICKY_HOTKEY in .env.
    hotkey: str = field(default_factory=lambda: os.getenv("CLICKY_HOTKEY", "ctrl+win"))

    # Microphone mode:
    #   "hotkey"  (default) — mic opens only while you're asking something.
    #             Tap the hotkey and speak; Clicky answers when you stop
    #             talking. Holding it also works and ends on release.
    #   "ambient" — legacy always-on mic with "Clicky" wake-word detection.
    #             Costs a continuous Whisper pass over everything it hears.
    mic_mode: str = field(default_factory=lambda: (
        os.getenv("CLICKY_MIC_MODE", "hotkey").strip().lower() or "hotkey"
    ))

    # How long a tap may last before it counts as a hold. Under this, releasing
    # the key does NOT stop the recording — silence detection does.
    tap_max_seconds: float = field(default_factory=lambda: float(
        os.getenv("CLICKY_TAP_MAX_SECONDS", "0.6") or 0.6
    ))

    def ambient_mic(self) -> bool:
        """True when the mic should stay open and scan for the wake word."""
        return self.mic_mode == "ambient"

    def set_mic_mode(self, mode: str) -> None:
        """Persisted switch between hotkey-only and always-listening."""
        mode = "ambient" if mode == "ambient" else "hotkey"
        self.mic_mode = mode
        os.environ["CLICKY_MIC_MODE"] = mode
        self._write_env("CLICKY_MIC_MODE", mode)

    def llm_provider(self) -> str:
        """Returns the active LLM provider (runtime override > priority chain).

        Priority chain: Claude > OpenAI > GitHub Copilot > Gemini > Ollama.
        """
        override = os.environ.get("CLICKY_ACTIVE_LLM", "").strip().lower()
        if override in self.available_llm_providers():
            return override
        if self.anthropic_api_key:
            return "claude"
        if self.openai_api_key:
            return "openai"
        try:
            from ai.github_copilot_provider import is_authenticated as _gh_ok
            if _gh_ok():
                return "copilot"
        except Exception:
            pass
        if self.google_api_key:
            return "gemini"
        return "ollama"

    def available_llm_providers(self) -> list[str]:
        """All providers the user can switch to right now."""
        out = []
        if self.anthropic_api_key:
            out.append("claude")
        if self.openai_api_key:
            out.append("openai")
        try:
            from ai.github_copilot_provider import is_authenticated as _gh_ok
            if _gh_ok():
                out.append("copilot")
        except Exception:
            pass
        if self.google_api_key:
            out.append("gemini")
        out.append("ollama")     # always available if the daemon is running
        out.append("lmstudio")   # always available if the local server is running
        return out

    def selected_model(self, provider: str | None = None) -> str:
        """Last selected model for each provider, restored from the environment."""
        provider = provider or self.llm_provider()
        return os.getenv(f"LASHY_MODEL_{provider.upper()}", "").strip()

    def set_selected_model(self, model: str, provider: str | None = None) -> None:
        provider = provider or self.llm_provider()
        model = model.strip()
        if not model or model == "default":
            return
        key = f"LASHY_MODEL_{provider.upper()}"
        if os.environ.get(key) != model:
            os.environ[key] = model
            self._write_env(key, model)

    def set_active_llm(self, name: str) -> None:
        """Runtime switch — next query uses this provider. Persisted to .env."""
        name = name.lower()
        os.environ["CLICKY_ACTIVE_LLM"] = name
        # Write to .env so the choice survives restarts
        env_path = _HERE / ".env"
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True) if env_path.exists() else []
            key = "CLICKY_ACTIVE_LLM"
            found = False
            for i, line in enumerate(lines):
                if line.startswith(key + "=") or line.startswith(key + " ="):
                    lines[i] = f"{key}={name}\n"
                    found = True
                    break
            if not found:
                lines.append(f"\n{key}={name}\n")
            env_path.write_text("".join(lines), encoding="utf-8")
        except Exception:
            pass  # non-fatal — runtime switch still works via os.environ

    # ── API key management ───────────────────────────────────────────────
    #
    # Keys used to be .env-only, which meant a packaged install had no way to
    # enter one short of hand-editing a file next to Clicky.exe. These helpers
    # back the Settings → API Keys dialog.

    # env var name → (dataclass attribute, human label)
    API_KEY_FIELDS = {
        "ANTHROPIC_API_KEY":  ("anthropic_api_key",  "Anthropic (Claude)"),
        "OPENAI_API_KEY":     ("openai_api_key",     "OpenAI (GPT)"),
        "GOOGLE_API_KEY":     ("google_api_key",     "Google (Gemini)"),
        "ELEVENLABS_API_KEY": ("elevenlabs_api_key", "ElevenLabs (voice)"),
        "DEEPGRAM_API_KEY":   ("deepgram_api_key",   "Deepgram (speech-to-text)"),
        "TAVILY_API_KEY":     ("tavily_api_key",     "Tavily (web search)"),
    }

    @staticmethod
    def env_path() -> Path:
        """The .env Clicky reads and writes — next to Clicky.exe when frozen."""
        return _HERE / ".env"

    def get_api_key(self, env_var: str) -> str:
        attr = self.API_KEY_FIELDS.get(env_var, (None, None))[0]
        return (getattr(self, attr, None) or "") if attr else ""

    def set_api_key(self, env_var: str, value: str) -> None:
        """Set (or clear) one key: live in this process + persisted to .env."""
        if env_var not in self.API_KEY_FIELDS:
            return
        value = (value or "").strip()
        attr = self.API_KEY_FIELDS[env_var][0]

        setattr(self, attr, value or None)
        if value:
            os.environ[env_var] = value
        else:
            os.environ.pop(env_var, None)

        self._write_env(env_var, value)

    def _write_env(self, key: str, value: str) -> None:
        """Upsert KEY=value in .env, removing the line entirely when cleared."""
        path = self.env_path()
        try:
            lines = (
                path.read_text(encoding="utf-8").splitlines(keepends=True)
                if path.exists() else []
            )
            out, found = [], False
            for line in lines:
                stripped = line.lstrip()
                if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
                    found = True
                    if value:
                        out.append(f"{key}={value}\n")
                    # cleared → drop the line
                    continue
                out.append(line)

            if value and not found:
                if out and not out[-1].endswith("\n"):
                    out.append("\n")
                out.append(f"{key}={value}\n")

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(out), encoding="utf-8")
        except Exception:
            pass  # runtime value still applied; persistence is best-effort

    def clear_active_llm(self) -> None:
        """Unpin the provider so the key-priority chain decides again.

        Used when the key behind a pinned provider is removed — otherwise
        llm_provider() keeps naming a provider that can no longer answer.
        """
        os.environ.pop("CLICKY_ACTIVE_LLM", None)
        self._write_env("CLICKY_ACTIVE_LLM", "")

    def stt_provider(self) -> str:
        # Allow explicit override via env (so users can force whisper_cpp etc.)
        forced = os.getenv("CLICKY_STT", "").strip().lower()
        if forced in ("deepgram", "openai", "whisper_cpp", "faster_whisper"):
            return forced
        if self.deepgram_api_key:
            return "deepgram"
        if self.openai_api_key:
            return "openai"
        # Prefer whisper.cpp (GPU-accelerated, same engine as Handy) when the
        # pywhispercpp package is installed; otherwise fall back to faster-whisper.
        try:
            import pywhispercpp  # noqa: F401
            return "whisper_cpp"
        except ImportError:
            return "faster_whisper"

    def tts_provider(self) -> str:
        if self.elevenlabs_api_key:
            return "elevenlabs"
        if self.openai_api_key:
            return "openai"
        return "edge_tts"

    def search_provider(self) -> str:
        if self.tavily_api_key:
            return "tavily"
        return "duckduckgo"

    def describe(self) -> dict:
        """Human-readable summary of active providers for the setup panel."""
        return {
            "llm": self.llm_provider(),
            "stt": self.stt_provider(),
            "tts": self.tts_provider(),
            "search": self.search_provider(),
            "ollama_model": self.ollama_model,
            "ollama_vision_model": self.get_ollama_model("vision"),
            "ollama_text_model":   self.get_ollama_model("text"),
            "lmstudio_host": self.lmstudio_host,
            "lmstudio_model": self.lmstudio_model or "(auto — whatever's loaded)",
        }

    # ── Ollama runtime model selection ───────────────────────────────────

    def get_ollama_model(self, kind: str = "vision") -> str:
        """Return the active model for the given kind ("vision" | "text").

        Reads runtime override from CLICKY_OLLAMA_VISION_MODEL /
        CLICKY_OLLAMA_TEXT_MODEL first, then the dataclass field, then the
        legacy single-model knob.
        """
        env_key = "CLICKY_OLLAMA_VISION_MODEL" if kind == "vision" else "CLICKY_OLLAMA_TEXT_MODEL"
        runtime = os.environ.get(env_key, "").strip()
        if runtime:
            return runtime
        return self.ollama_vision_model if kind == "vision" else self.ollama_text_model

    def set_ollama_model(self, kind: str, name: str) -> None:
        """Runtime switch for vision/text Ollama model. Persists for the session."""
        if kind not in ("vision", "text"):
            return
        env_key = "CLICKY_OLLAMA_VISION_MODEL" if kind == "vision" else "CLICKY_OLLAMA_TEXT_MODEL"
        os.environ[env_key] = (name or "").strip()
        # Mirror onto the dataclass so describe() picks it up immediately
        if kind == "vision":
            self.ollama_vision_model = name
        else:
            self.ollama_text_model = name


# Singleton
cfg = Config()
