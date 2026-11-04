"""CI only: show the end of the given log files as GitHub annotations.

A failed `asterisk-live` job can then be understood from the run page alone. Workflow commands
(such as the add-mask line) and the CI's random ARI password never reach an annotation.
"""

import os
import sys
from pathlib import Path

MAX_CHARS = 4000
MAX_ANNOTATIONS = 9


def escape(text: str, *, prop: bool = False) -> str:
    text = text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if prop else text


def clean(text: str) -> str:
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("::")]
    text = "\n".join(lines).strip()
    secret = os.environ.get("ASTERISK_LIVE_ARI_PASSWORD", "")
    return text.replace(secret, "***") if secret else text


def chunks(name: str, text: str) -> list[tuple[str, str]]:
    """The first failure traceback (pytest) and the end of the file."""
    parts = []
    start = text.find("= FAILURES =")
    if start == -1:
        start = text.find("= ERRORS =")
    if start != -1:
        parts.append((f"{name} (first failure)", text[start : start + MAX_CHARS]))
    parts.append((f"{name} (end)", text[-MAX_CHARS:]))
    return parts


def main(paths: list[str]) -> int:
    emitted = 0
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            continue
        text = clean(path.read_text(encoding="utf-8", errors="replace"))
        if not text:
            continue
        for title, body in chunks(path.name, text):
            if emitted >= MAX_ANNOTATIONS:
                return 0
            print(f"::error title={escape(title, prop=True)}::{escape(body)}")
            emitted += 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
