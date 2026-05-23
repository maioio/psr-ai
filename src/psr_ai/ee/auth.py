"""Enterprise multi-tenant token authorization and secret scanning."""

from __future__ import annotations

import re


# Patterns that indicate a leaked secret in a payload before transmission.
_SECRET_PATTERNS: list[re.Pattern] = [
    re.compile(r"sk-ant-api\d{2}-[A-Za-z0-9_-]{20,}"),   # Anthropic API key
    re.compile(r"sk-[A-Za-z0-9]{40,}"),                    # OpenAI API key
    re.compile(r"ghp_[A-Za-z0-9]{36}"),                    # GitHub personal token
    re.compile(r"ghs_[A-Za-z0-9]{36}"),                    # GitHub app token
    re.compile(r"AKIA[0-9A-Z]{16}"),                        # AWS access key
]


class TeamToken:
    """Represents a scoped enterprise team token with permission boundaries."""

    def __init__(self, raw_token: str, team: str, scopes: list[str] | None = None) -> None:
        if not raw_token or not team:
            raise ValueError("Token and team name are required.")
        self.raw_token = raw_token
        self.team = team
        self.scopes = scopes or ["read", "write"]

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def __repr__(self) -> str:
        return f"TeamToken(team={self.team!r}, scopes={self.scopes})"


def scan_for_secrets(payload: str) -> list[str]:
    """Scan a string payload for patterns that look like API secrets.

    Returns a list of redacted match descriptions (never the secret itself).
    Used as a pre-transmission safety filter.
    """
    findings: list[str] = []
    for pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(payload):
            prefix = match.group()[:8]
            findings.append(f"Possible secret detected (prefix: {prefix!r}...)")
    return findings


def authorize(token: TeamToken, scope: str) -> None:
    """Assert that a token has the required scope.

    Raises:
        PermissionError: If the token lacks the requested scope.
    """
    if not token.has_scope(scope):
        raise PermissionError(
            f"Token for team {token.team!r} does not have scope {scope!r}. "
            f"Granted scopes: {token.scopes}"
        )
