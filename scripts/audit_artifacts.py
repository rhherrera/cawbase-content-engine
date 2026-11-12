"""Audit built artifacts (wheel/sdist) for accidental files and secret-looking content.

    python scripts/audit_artifacts.py dist/*.whl dist/*.tar.gz

Exit code 1 on any finding. Patterns are conservative: they look for local/accidental files and for
the product's own credential format, never for generic words.
"""

from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from pathlib import Path

FORBIDDEN_NAMES = (".DS_Store", ".env", "STAGE_3_PLAYBOOK.md")
FORBIDDEN_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key", ".pyc")
FORBIDDEN_PARTS = ("__pycache__", ".venv", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "evidence-local")
SECRET_PATTERNS = {
    "cce1 API key": re.compile(rb"cce1\.k_[0-9a-f]{24}\.[A-Za-z0-9_-]{43}"),
    "private key block": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "connection URL with password": re.compile(
        rb"(?:postgres(?:ql)?(?:\+\w+)?|mysql)://[^\s:@/]+:(?!pass\b|password\b|pw\b|\.\.\.|\*+)[^\s@/]{4,}@"
    ),
    "AWS access key id": re.compile(rb"AKIA[0-9A-Z]{16}"),
}
#: Test credentials that are *intentionally* public (disposable local containers).
ALLOWED_SECRET_SUBSTRINGS = (
    b"cce_test_pw",
    b"user:pass@",
    b":pw@",
    b":rt@",
    b"Hunter2Pw",
    b"sup3r-s3cret-pw",
    b"hunter2",
    b"{secret}",  # synthetic canaries used by tests to prove secrets never leak
)


def members(path: Path):
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                yield name, z.read(name)
    else:
        with tarfile.open(path) as t:
            for m in t.getmembers():
                if m.isfile():
                    f = t.extractfile(m)
                    yield m.name, f.read() if f else b""


def audit(path: Path) -> list[str]:
    findings = []
    for name, data in members(path):
        parts = Path(name).parts
        base = parts[-1]
        if base in FORBIDDEN_NAMES or base.startswith("._") or base.endswith(FORBIDDEN_SUFFIXES):
            findings.append(f"{path.name}: forbidden file {name}")
        if any(p in FORBIDDEN_PARTS for p in parts):
            findings.append(f"{path.name}: forbidden directory in {name}")
        for label, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(data):
                if any(ok in match.group(0) for ok in ALLOWED_SECRET_SUBSTRINGS):
                    continue
                findings.append(f"{path.name}: {label} in {name}")
    return findings


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv]
    if not paths:
        print("usage: audit_artifacts.py ARTIFACT...", file=sys.stderr)
        return 2
    findings = [f for p in paths for f in audit(p)]
    for f in findings:
        print("FINDING:", f)
    print(f"audited {len(paths)} artifact(s): {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
