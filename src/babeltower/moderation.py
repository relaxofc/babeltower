import re

EMAIL_RE = re.compile(r"[\w._%+-]+@(?:[\w.-]+\.[a-zA-Z]{2,}|[A-Za-z][\w-]{2,})")
PHONE_RE = re.compile(
    r"""
    (?:
        \+?\d{1,3}[\s.-]?
        (?:\(?\d{2,4}\)?[\s.-]?)?
        \d{3,4}[\s.-]?\d{4}
    )
    |
    (?:
        \(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}
    )
    """,
    re.VERBOSE,
)
URL_RE = re.compile(
    r"https?://\S+|\b[\w-]+\.(?:com|net|org|io|ai|xyz|dev|co|me|app)\b",
    re.IGNORECASE,
)
HANDLE_RE = re.compile(r"\B@[A-Za-z][A-Za-z0-9_]{1,}|t\.me/\w+", re.IGNORECASE)


def scan_intent_text(text: str) -> list[str]:
    violations = []
    url_scan_text = text

    if EMAIL_RE.search(text):
        violations.append("email")
        url_scan_text = EMAIL_RE.sub(" ", url_scan_text)
    if PHONE_RE.search(text):
        violations.append("phone")
    if HANDLE_RE.search(text):
        violations.append("handle")
        url_scan_text = HANDLE_RE.sub(" ", url_scan_text)
    if URL_RE.search(url_scan_text):
        violations.append("url")

    return violations
