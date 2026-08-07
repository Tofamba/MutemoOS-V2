"""
Pure helpers behind automatic client/matter numbering — no DB access, so
they're unit-testable directly (see tests/test_numbering.py). The DB-facing
wiring (reading current max sequence, persisting a user's initials) lives in
backend/main.py.

Format: client_number = "{initials}-{seq:03d}" (e.g. NGM-007)
        matter_number = "{client_number}-{seq:02d}" (e.g. NGM-007-02)
"""


def generate_initials(display_name: str) -> str:
    """
    Derives a lawyer's initials from their name: one letter per name token,
    e.g. "Nyaradzo Gilbertina Maphosa" -> "NGM", "Ostern Mutero" -> "OM".

    Single-token names are ambiguous — could be a real one-word name (use
    its first two letters) or already-initials (e.g. the synthetic
    AUTH_ENABLED=False dev user, whose display_name is literally "NGM"; or a
    firm that seeds user rows with initials as the name). A short, all-caps,
    all-alphabetic single token is treated as already being initials and
    passed through unchanged rather than mangled into first-two-letters.
    """
    if not display_name:
        return "X"
    tokens = [t.strip(".,") for t in display_name.split() if t.strip(".,")]
    if not tokens:
        return "X"
    if len(tokens) == 1:
        token = tokens[0]
        if token.isalpha() and token.isupper() and 2 <= len(token) <= 4:
            return token
        return token[:2].upper()
    return "".join(t[0] for t in tokens if t and t[0].isalpha()).upper()


def disambiguate_initials(base: str, existing: set) -> str:
    """
    Returns `base` if it's not already taken, otherwise appends the lowest
    unused integer suffix starting at 2 (NGM, NGM2, NGM3, ...).
    """
    if base not in existing:
        return base
    n = 2
    while f"{base}{n}" in existing:
        n += 1
    return f"{base}{n}"


def next_sequence(existing_numbers: list, prefix: str) -> int:
    """
    Given existing numbered ids sharing `prefix` (client_number values like
    "NGM-007" for prefix "NGM", or matter_number values like "NGM-007-02"
    for prefix "NGM-007"), returns the next sequence number: one past the
    highest existing suffix, or 1 if there are none.
    """
    max_seq = 0
    marker = f"{prefix}-"
    for value in existing_numbers:
        if not value or not value.startswith(marker):
            continue
        rest = value[len(marker):]
        digits = ""
        for ch in rest:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            max_seq = max(max_seq, int(digits))
    return max_seq + 1


def format_client_number(initials: str, seq: int) -> str:
    return f"{initials}-{seq:03d}"


def format_matter_number(client_number: str, seq: int) -> str:
    return f"{client_number}-{seq:02d}"
