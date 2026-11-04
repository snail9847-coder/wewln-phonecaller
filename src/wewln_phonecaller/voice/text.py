"""Text between the models: sentences for speech, control marks from the dialogue model.

The dialogue model streams its reply in small pieces. Speech starts as soon as the first
sentence is complete (not after the whole reply), so the caller waits less. The model ends
the conversation with the mark [END] and reports a request not to be called again with
[OPT_OUT]; marks are never spoken. Text inside <think>...</think> (reasoning models) is
dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

END_MARK = "[END]"
OPT_OUT_MARK = "[OPT_OUT]"
_MARK = re.compile(
    r"[\[<]\s*(?P<name>end|end_call|конец|завершить|opt_out|optout|dnc|не_звонить)\s*[\]>]",
    re.IGNORECASE,
)
_END_NAMES = {"end", "end_call", "конец", "завершить"}
_OPT_OUT_NAMES = {"opt_out", "optout", "dnc", "не_звонить"}
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"»)\]]*\s+")
_MARKUP = re.compile(r"[*_#`~|<>\[\]{}]")
_EMOJI = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\ufe0f]")
_SPACES = re.compile(r"\s+")
# A sentence longer than this is spoken in parts (at a comma) so that speech starts early.
SOFT_LIMIT = 140
HARD_LIMIT = 260


@dataclass(frozen=True)
class Marks:
    end: bool = False
    opt_out: bool = False


def take_marks(text: str) -> tuple[str, Marks]:
    """The text without control marks, and which marks it had."""
    end = opt_out = False
    for match in _MARK.finditer(text):
        name = match["name"].lower()
        end = end or name in _END_NAMES
        opt_out = opt_out or name in _OPT_OUT_NAMES
    return _MARK.sub(" ", text), Marks(end=end, opt_out=opt_out)


def speakable(text: str) -> str:
    """Text a speech synthesizer can read: no markup, emoji or control marks."""
    text, _ = take_marks(_THINK.sub(" ", text))
    text = _EMOJI.sub(" ", text)
    text = _MARKUP.sub(" ", text)
    return _SPACES.sub(" ", text).strip(" -–—")


def normalize_transcript(text: str) -> str:
    return _SPACES.sub(" ", text or "").strip()


class SentenceSplitter:
    """Feeds on streamed text; returns finished sentences (marks still inside)."""

    def __init__(self) -> None:
        self._buffer = ""
        self._thinking = False
        self.marks = Marks()
        self.full_text = ""

    def _strip_thinking(self, chunk: str) -> str:
        out = []
        while chunk:
            if self._thinking:
                end = chunk.lower().find("</think>")
                if end < 0:
                    return "".join(out)
                chunk = chunk[end + len("</think>") :]
                self._thinking = False
            else:
                start = chunk.lower().find("<think>")
                if start < 0:
                    out.append(chunk)
                    break
                out.append(chunk[:start])
                chunk = chunk[start + len("<think>") :]
                self._thinking = True
        return "".join(out)

    def feed(self, chunk: str) -> list[str]:
        chunk = self._strip_thinking(chunk)
        if not chunk:
            return []
        self._buffer += chunk
        sentences: list[str] = []
        while True:
            match = _SENTENCE_END.search(self._buffer)
            if match is None:
                break
            sentences.append(self._buffer[: match.start()])
            self._buffer = self._buffer[match.end() :]
        if len(self._buffer) > SOFT_LIMIT:
            buffer = self._buffer
            cut = max(buffer.rfind(", ", 0, HARD_LIMIT), buffer.rfind("; ", 0, HARD_LIMIT))
            if cut > 40 or len(self._buffer) > HARD_LIMIT:
                cut = cut + 1 if cut > 40 else HARD_LIMIT
                sentences.append(self._buffer[:cut])
                self._buffer = self._buffer[cut:]
        return self._finish(sentences)

    def flush(self) -> list[str]:
        rest, self._buffer = self._buffer, ""
        return self._finish([rest])

    def _finish(self, sentences: list[str]) -> list[str]:
        out = []
        for sentence in sentences:
            clean, marks = take_marks(sentence)
            self.marks = Marks(
                end=self.marks.end or marks.end, opt_out=self.marks.opt_out or marks.opt_out
            )
            clean = _SPACES.sub(" ", clean).strip()
            if clean:
                self.full_text = f"{self.full_text} {clean}".strip()
                out.append(clean)
        return out


# Phrases Whisper is known to invent on silence or noise in Russian (subtitle credits).
_HALLUCINATIONS = (
    "продолжение следует",
    "субтитры сделал",
    "субтитры создавал",
    "субтитры подготовил",
    "редактор субтитров",
    "спасибо за просмотр",
    "подписывайтесь на канал",
    "dimatorzok",
)


def is_hallucination(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _HALLUCINATIONS)


_OPT_OUT_PHRASES = re.compile(
    r"(не\s+(?:звоните|звонить|надо\s+(?:мне\s+)?звонить|перезванивайте)|"
    r"(?:удалите|уберите|вычеркните)\s+(?:мой\s+)?(?:номер|меня)|"
    r"больше\s+не\s+(?:звоните|звонить|беспокойте))",
    re.IGNORECASE,
)


def asks_not_to_call(text: str) -> bool:
    """The caller clearly asks not to be called again (a safety net besides the model)."""
    return bool(_OPT_OUT_PHRASES.search(text))
