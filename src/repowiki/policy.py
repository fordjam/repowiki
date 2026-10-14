"""The secret policy: files that never reach a model, a citation or the static site.

One policy is applied everywhere a repository path is used: planning context,
page excerpts, update context, citation validation and source export. Matching
is case-insensitive, with separate semantics for base names (the last path
component), directory components and whole paths. Add your own patterns with
``exclude = [...]`` in ``.repowiki.toml``.

Hard defaults (``SECRET_FILES``, ``SECRET_DIRS``) are never used anywhere.
Prompt-only defaults (``PROMPT_ONLY_FILES``, ``PROMPT_ONLY_DIRS`` and the wiki
directory) stay usable for local validation and rendering but never reach a
model unless the approved privacy configuration admits them (``prompt_policy``).
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass

# File names (glob, matched against the last path component).
SECRET_FILES = (
    ".env*", "*.env", ".envrc",
    "secrets*", ".secrets*", "*.secret", "*.secrets",
    "credentials*", "*-credentials.*", "*_credentials.*", "service-account*.json",
    "*session*", "*.db", "*.sqlite", "*.sqlite3", "*.pickle", "*.pkl", "*.joblib", "*.rdb", "*.cache",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.kdbx", "*.ovpn", "*.asc", "*.gpg",
    "*_rsa", "*_dsa", "*_ecdsa", "*_ed25519",
    ".npmrc", ".yarnrc", ".yarnrc.yml", ".pypirc", ".netrc", "_netrc", ".pgpass", ".htpasswd",
    ".git-credentials", ".dockercfg", ".boto", ".s3cfg",
    "*.tfstate", "*.tfstate.*", "*.tfvars",
    ".aider*",
    "*transcript*.jsonl", "*transcript*.json", "*transcript*.md", "*transcript*.txt", "*transcript*.log",
    "*transcript*.html",
)  # fmt: skip
# SSH keys: id_rsa, id_ed25519, id_ecdsa.pub ... but not id_utils.py.
SSH_KEY_RE = re.compile(r"^id_[a-z0-9]+(\.pub)?$", re.I)
# Directories whose contents are never used (credential stores, assistant session data, data and caches).
SECRET_DIRS = (
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store",
    ".claude", ".codex", ".opencode", ".specstory", ".aider*", "transcripts", "*-transcripts",
    "data", "databases", "sessions", "checkpoints", "caches",
)  # fmt: skip
# Prompt-only defaults: readable locally, withheld from every model prompt unless approved.
PROMPT_ONLY_FILES = ("README*",)  # lifted by the "readme" context class
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md", "SKILL.md")  # lifted by "instructions"
PROMPT_ONLY_DIRS = ("plans", "reports")  # lifted only by an approved allowlist pattern naming the directory


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
# key = "literal" / "password": "literal", any length (quoted literals only, so code like
# get_password() is left alone). The literal runs to its own closing quote: the other quote
# character and backslash escapes are part of it. It never crosses a line end.
ASSIGNED_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|secret|token|password|passwd)[\w-]{0,20}[\"']?\s{0,3}[:=]\s{0,3}([\"']))"
    r"(?:\\.|(?!\2)[^\\\r\n])+(?=\2)"
)
KEY_BLOCK_RE = re.compile(r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END[A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S)


def redact(text: str) -> str:
    """Replace credential-shaped strings and private-key blocks, keeping every line in place."""
    text = KEY_BLOCK_RE.sub(lambda m: "\n".join(REDACTED for _ in m.group(0).split("\n")), text)
    for pattern in TOKEN_RES:
        text = pattern.sub(REDACTED, text)
    return ASSIGNED_RE.sub(lambda m: m.group(1) + REDACTED, text)


def _names_dir(pattern: str, directory: str) -> bool:
    """True when an allowlist pattern names `directory` as an explicit path component."""
    parts = pattern.lower().strip("/").split("/")
    return directory in parts[:-1] or parts == [directory]


def prompt_policy(
    context: Policy, context_classes: Iterable[str], allowlist: Iterable[str], wiki_dir: str = "docs/wiki"
) -> Policy:
    """`context` plus the prompt-only defaults the approved configuration has not admitted.

    Hard exclusions stay in force whatever is approved (deny wins over the allowlist).
    """
    classes, allow = set(context_classes), list(allowlist)
    extra: list[str] = []
    if "readme" not in classes:
        extra += PROMPT_ONLY_FILES
    if "instructions" not in classes:
        extra += INSTRUCTION_FILES
    if "previous_wiki" not in classes:
        extra.append(wiki_dir.rstrip("/") + "/*")
    extra += [d for d in PROMPT_ONLY_DIRS if not any(_names_dir(p, d) for p in allow)]
    return Policy(tuple(dict.fromkeys(context.exclude + tuple(extra))), context.allowlist)


def is_secret(path: str, policy: Policy | None = None) -> bool:
    return (policy or DEFAULT_POLICY).secret(path)
