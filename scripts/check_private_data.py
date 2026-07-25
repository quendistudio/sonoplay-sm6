#!/usr/bin/env python3
"""Fail if tracked source files contain likely private deployment data."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git",
    ".cursor",
    ".pytest_cache",
    "__pycache__",
    "static",
    "website",
    ".temp",
    "config",
    "data",
    "tests/fixtures/private",
}

SKIP_FILES = {
    "scripts/check_private_data.py",
}

TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".yaml",
    ".yml",
    ".json",
    ".html",
    ".js",
    ".css",
    ".txt",
    ".env",
    ".example",
    ".sh",
    ".toml",
}

# Patterns that should never appear in published repository content.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private hostname (jacquouille)", re.compile(r"\bjacquouille\b", re.I)),
    ("QNAP cloud hostname", re.compile(r"\bmyqnapcloud\b", re.I)),
    ("RFC1918 IP (192.168.x.x)", re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b")),
    ("RFC1918 IP (10.x.x.x)", re.compile(r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")),
    (
        "RFC1918 IP (172.16-31.x.x)",
        re.compile(r"\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"),
    ),
    ("Plex token in URL", re.compile(r"X-Plex-Token=[A-Za-z0-9_-]{10,}")),
    ("Plex token assignment", re.compile(r"PLEX_PMS_TOKEN\s*=\s*[A-Za-z0-9_-]{10,}")),
    ("capture from local .temp", re.compile(r"plexupnp/\.temp|/\.temp/[a-z]+\.txt", re.I)),
]

# Allowlisted substrings (placeholders, docs, standard ports).
ALLOW_SUBSTRINGS = (
    "<your host IP>",
    "your-server",
    "<pms-host>",
    "example.com",
    "32400",
    "32469",
    "32488",
    "FAKE_HOST_IP",  # tests/fixtures/network.py — fictional lab IP
    "192.168.50.99",  # same fixture IP when inlined in assertions
)


def _git_tracked_files() -> list[Path]:
    """Return paths tracked by git (matches what CI / a push would publish)."""
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    out: list[Path] = []
    for rel in result.stdout.decode("utf-8", errors="replace").split("\0"):
        if not rel:
            continue
        path = ROOT / rel
        if path.is_file():
            out.append(path)
    return out


def iter_files() -> list[Path]:
    files: list[Path] = []
    for path in _git_tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in SKIP_FILES:
            continue
        if any(part in SKIP_DIRS for part in Path(rel).parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {".env.example"}:
            continue
        files.append(path)
    return sorted(files)


def _match_allowed(text: str, match: re.Match[str]) -> bool:
    """True when the pattern match lies entirely inside an allowlisted substring."""
    start, end = match.span()
    for token in ALLOW_SUBSTRINGS:
        pos = 0
        while True:
            idx = text.find(token, pos)
            if idx == -1:
                break
            if idx <= start and end <= idx + len(token):
                return True
            pos = idx + 1
    return False


def scan_file(path: Path) -> list[str]:
    rel = path.relative_to(ROOT).as_posix()
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [f"{rel}: binary or non-UTF-8 file"]

    issues: list[str] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for label, pattern in PATTERNS:
            for match in pattern.finditer(line):
                if _match_allowed(line, match):
                    continue
                snippet = line.strip()
                if len(snippet) > 120:
                    snippet = snippet[:117] + "..."
                issues.append(f"{rel}:{line_no}: {label} -> {snippet}")
                break
    return issues


def main() -> int:
    issues: list[str] = []
    for path in iter_files():
        issues.extend(scan_file(path))

    if issues:
        print("Private data check failed:\n")
        for issue in issues:
            print(f"  - {issue}")
        print(
            "\nUse fictional fixtures (tests/fixtures/) and placeholders in docs. "
            "Never commit tokens, private IPs, or real library metadata."
        )
        return 1

    print("Private data check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
