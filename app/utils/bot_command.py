from __future__ import annotations


def normalize_bot_command(text: str) -> str:
    command = text.strip().lower()
    if command.startswith("/"):
        command = command[1:]
    if "@" in command:
        command = command.split("@", 1)[0]
    return command.strip()
