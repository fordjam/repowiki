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

    def secret(self, path: str) -> bool:
        path = posixpath.normpath(path.strip("/")) if path else ""
        if not path or path == ".":
            return False
        parts = path.split("/")
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


def is_secret(path: str, policy: Policy | None = None) -> bool:
    return (policy or DEFAULT_POLICY).secret(path)
