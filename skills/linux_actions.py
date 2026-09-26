"""
Linux Mint Desktop Action Skill.
Instantly launches native tools and applications on voice or text commands.
"""

import subprocess
import re

async def handle_linux_action(manager, transcript: str) -> str:
    t = transcript.lower().strip()
    if re.search(r"\b(terminal|bash|console)\b", t):
        subprocess.Popen(["x-terminal-emulator"])
        return "Opening a new terminal for you! *(Shortcut: Ctrl+Alt+T)*"
    if re.search(r"\b(downloads)\b", t):
        subprocess.Popen(["nemo", f"{Path.home()}/Downloads"])
        return "Opening your Downloads folder in Nemo!"
    if re.search(r"\b(files|nemo|file manager)\b", t):
        subprocess.Popen(["nemo"])
        return "Opening the Nemo file manager! *(Shortcut: Super+E)*"
    if re.search(r"\b(system monitor|task manager|processes|cpu usage)\b", t):
        subprocess.Popen(["gnome-system-monitor"])
        return "Opening System Monitor!"
    if re.search(r"\b(software manager|app store|mintinstall)\b", t):
        subprocess.Popen(["mintinstall"])
        return "Opening Software Manager!"
    if re.search(r"\b(update manager|mintupdate|system update)\b", t):
        subprocess.Popen(["mintupdate"])
        return "Opening Update Manager!"
    if re.search(r"\b(settings|system settings)\b", t):
        subprocess.Popen(["cinnamon-settings"])
        return "Opening System Settings!"
    if re.search(r"\b(firefox|browser|web browser)\b", t):
        subprocess.Popen(["firefox"])
        return "Opening Firefox!"
    if re.search(r"\b(text editor|editor|xed)\b", t):
        subprocess.Popen(["xed"])
        return "Opening Xed text editor!"
    if re.search(r"\b(screenshot|capture screen)\b", t):
        subprocess.Popen(["gnome-screenshot"])
        return "Opening screenshot tool!"
    if re.search(r"\b(calculator|calc)\b", t):
        subprocess.Popen(["gnome-calculator"])
        return "Opening Calculator!"
    if re.search(r"\b(disks|disk utility)\b", t):
        subprocess.Popen(["gnome-disks"])
        return "Opening Disks utility!"
    if re.search(r"\b(lock screen|lock pc)\b", t):
        subprocess.Popen(["cinnamon-screensaver-command", "-l"])
        return "Locking screen! *(Shortcut: Ctrl+Alt+L)*"
    return ""

from pathlib import Path

SKILL = {
    "name": "Linux Desktop Actions",
    "trigger": (
        r"\b(open|launch|start|run|show)\s+(a\s+)?(new\s+)?"
        r"(terminal|bash|console|files|file manager|nemo|downloads|settings|system settings|"
        r"firefox|browser|editor|text editor|xed|screenshot|system monitor|task manager|"
        r"software manager|update manager|calculator|calc|disks|lock screen)\b|"
        r"^(terminal|nemo|firefox|settings|xed|screenshot|calculator)$"
    ),
    "description": "Instantly opens native Linux Mint applications on command.",
    "handler": handle_linux_action,
}
