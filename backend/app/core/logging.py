"""Logging with secret redaction.

Secrets must never reach logs, audit records, snapshots or error
messages (ARCHITECTURE.md Section 6.4). Redaction here is a safety net,
not a licence to log secrets.
"""

import logging
import re
import sys
from typing import Final

# Connection strings and bearer tokens are the realistic leak paths:
# a DSN carries a password, and a provider error can echo a key.
_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)(://[^:/\s]+:)([^@\s]+)(@)"),
    re.compile(r"(?i)(bearer\s+)(\S+)"),
    re.compile(r"(?i)((?:api[_-]?key|password|secret|token)[\"']?\s*[:=]\s*[\"']?)([^\s\"',}]+)"),
)

_REDACTED: Final[str] = "***REDACTED***"


class RedactSecretsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.redact(record.msg)
        # Only tuple args are rewritten. A mapping arg belongs to
        # %(name)s-style formatting and must keep its type.
        if isinstance(record.args, tuple):
            record.args = tuple(
                self.redact(a) if isinstance(a, str) else a for a in record.args
            )
        return True

    @staticmethod
    def redact(text: str) -> str:
        for pattern in _PATTERNS:
            text = pattern.sub(_replace, text)
        return text


def _replace(match: re.Match[str]) -> str:
    # Pattern 1 captures a trailing delimiter in group 3; patterns 2 and 3
    # have only two groups.
    tail = match.group(3) if match.re.groups >= 3 else ""
    return f"{match.group(1)}{_REDACTED}{tail}"


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
    )
    handler.addFilter(RedactSecretsFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
