"""
Repair for free-text metadata that arrived (or was stored) with UTF-8
characters spelled out as literal backslash-x-hex escape sequences -- the
input-side counterpart to main.py's output-side _pdf_safe(), which is a
different thing: _pdf_safe() lossily downgrades correct Unicode for a
latin-1 PDF font at export time, whereas this restores text that was
already wrong before it ever reached storage.

Found via a real incident (2026-09-20): a validity_flag whose em dash was
stored as the literal characters \\xe2\\x80\\x94 (the escape spelling of the
dash's UTF-8 bytes, produced when a caller's shell passed escapes through
uninterpreted) and reached the model prompt that way, inside a caveat the
model is instructed to act on.

Deliberately scoped to exactly that confirmed failure and nothing broader.
In particular it does NOT try to repair UTF-8-read-as-latin-1 "mojibake":
a scan of the whole shared corpus found zero examples of that class, so a
repair path for it would be speculative scope, not evidence-based.

Strict by construction: a run of escapes is only replaced when it contains
at least one non-ASCII byte and decodes as *valid* UTF-8. A lone or
truncated escape, ASCII-only escapes, or a real backslash-x in some other
context is left exactly as it was. Idempotent, and None/empty pass through
unchanged.
"""

import re
from typing import Optional

_ESCAPE_RUN = re.compile(r"(?:\\x[0-9A-Fa-f]{2})+")


def _repair_escape_run(match: "re.Match") -> str:
    run = match.group(0)
    raw = bytes(int(run[i + 2:i + 4], 16) for i in range(0, len(run), 4))
    if all(b < 0x80 for b in raw):
        return run  # only ASCII escapes: not this bug, don't touch
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return run


def repair_text_encoding(text: Optional[str]) -> Optional[str]:
    if not text or not isinstance(text, str):
        return text  # None/empty, or a non-string (e.g. an unresolved FastAPI Form() default) -- nothing to repair
    return _ESCAPE_RUN.sub(_repair_escape_run, text)
