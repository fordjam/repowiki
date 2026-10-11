"""The secret policy: files that never reach a model, a citation or the static site.

One policy is applied everywhere a repository path is used: planning context,
page excerpts, update context, citation validation and source export. Matching
is on path components, case-insensitive. Add your own patterns with
``exclude = [...]`` in ``.repowiki.toml``.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass

# File names (glob, matched against the last path component).
SECRET_FILES = (
    ".env", ".env.*", "*.env", ".envrc",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.kdbx", "*.ovpn", "*.asc", "*.gpg",
    "*_rsa", "*_dsa", "*_ecdsa", "*_ed25519",
    ".npmrc", ".yarnrc", ".yarnrc.yml", ".pypirc", ".netrc", "_netrc", ".pgpass", ".htpasswd",
    ".git-credentials", ".dockercfg", ".boto", ".s3cfg",
    "secrets", "secrets.*", ".secrets", ".secrets.*", "*.secret", "*.secrets",
    "credentials", "credentials*", "*-credentials.*", "*_credentials.*", "service-account*.json",
    "*.tfstate", "*.tfstate.*", "*.tfvars",
    "*transcript*.jsonl", "*transcript*.json", "*transcript*.md", "*transcript*.txt", "*transcript*.log",
    "*transcript*.html", ".aider*",
)  # fmt: skip
# SSH keys: id_rsa, id_ed25519, id_ecdsa.pub ... but not id_utils.py.
SSH_KEY_RE = re.compile(r"^id_[a-z0-9]+(\.pub)?$", re.I)
# Directories whose contents are never used (credential stores and AI assistant session data).
SECRET_DIRS = (
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store",
    ".claude", ".codex", ".opencode", ".specstory", ".aider*", "transcripts", "*-transcripts",
)  # fmt: skip


@dataclass(frozen=True)
class Policy:
    """The built-in secret patterns plus ``exclude`` globs (matched against the path and each name)."""

    exclude: tuple[str, ...] = ()
    allowlist: tuple[str, ...] | None = None

    def secret(self, path: str) -> bool:
        path = posixpath.normpath(path.strip("/")) if path else ""
        if not path or path == ".":
            return False
        parts = path.split("/")
        if self.allowlist is not None and not any(_match(path, p) for p in self.allowlist):
            return True
        name = parts[-1]
        if SSH_KEY_RE.match(name) or _any(name, SECRET_FILES):
            return True
        if any(_any(part, SECRET_DIRS) for part in parts):
            return True
        return any(_match(path, pat) or any(_match(p, pat) for p in parts) for pat in self.exclude)

    def filter(self, paths: Iterable[str]) -> list[str]:
        return [p for p in paths if not self.secret(p)]


def _match(name: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(name.lower(), pattern.lower().strip("/"))


def _any(name: str, patterns: tuple[str, ...]) -> bool:
    return any(_match(name, p) for p in patterns)


DEFAULT_POLICY = Policy()

REDACTED = "[redacted by repowiki]"
# High-confidence credential shapes. Matches are replaced in place; line count never changes.
TOKEN_RES = [
    re.compile(p)
    for p in (
        r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}",
        r"\bgh[pousr]_[A-Za-z0-9]{30,}",
        r"\bgithub_pat_[A-Za-z0-9_]{30,}",
        r"\bglpat-[A-Za-z0-9_-]{20,}",
        r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bAIza[0-9A-Za-z_-]{35}\b",
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
        r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*",
    )
]
# key = "literal" / "password": "literal" (quoted literals only, so code like get_password() is left alone)
ASSIGNED_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|secret|token|password|passwd)[\w-]{0,20}[\"']?\s{0,3}[:=]\s{0,3}[\"'])[^\"'\s]{12,}(?=[\"'])"
)
KEY_BLOCK_RE = re.compile(r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END[A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S)


def redact(text: str) -> str:
    """Replace credential-shaped strings and private-key blocks, keeping every line in place."""
    text = KEY_BLOCK_RE.sub(lambda m: "\n".join(REDACTED for _ in m.group(0).split("\n")), text)
    for pattern in TOKEN_RES:
        text = pattern.sub(REDACTED, text)
    return ASSIGNED_RE.sub(lambda m: m.group(1) + REDACTED, text)


def is_secret(path: str, policy: Policy | None = None) -> bool:
    return (policy or DEFAULT_POLICY).secret(path)
