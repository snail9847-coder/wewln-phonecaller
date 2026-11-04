"""CI only: show log files of a job as GitHub annotations.

    annotate.py --notice FILE...   the test summary (or the last lines) of each file
    annotate.py FILE...            on failure: the first failure and the end of each file

Results and failures can then be read on the run page alone. Workflow commands (such as the
add-mask line) and the CI's random ARI password never reach an annotation.
"""

import os
import sys
from pathlib import Path

MAX_CHARS = 4000
MAX_ANNOTATIONS = 9
SUMMARY_LINES = 25


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


def summary(name: str, text: str) -> list[tuple[str, str]]:
    """pytest's short test summary (-rA lists every test), else the last lines."""
    start = text.find("short test summary info")
    if start != -1:
        body = text[text.rfind("\n", 0, start) + 1 :]
    else:
        body = "\n".join(text.splitlines()[-SUMMARY_LINES:])
    return [(name, body[-MAX_CHARS:])]


def main(args: list[str]) -> int:
    level, select = "error", chunks
    if args[:1] == ["--notice"]:
        level, select, args = "notice", summary, args[1:]
    emitted = 0
    for raw in args:
        path = Path(raw)
        if not path.is_file():
            continue
        text = clean(path.read_text(encoding="utf-8", errors="replace"))
        if not text:
            continue
        for title, body in select(path.name, text):
            if emitted >= MAX_ANNOTATIONS:
                return 0
            print(f"::{level} title={escape(title, prop=True)}::{escape(body)}")
            emitted += 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
