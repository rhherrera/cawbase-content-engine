"""Documentation stays aligned with the code (AC-A06-55): links and anchors, permission names, error codes, versions."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

CONTRACT = (ROOT / "docs" / "text-protection.md").read_text(encoding="utf-8")


def test_every_relative_link_and_anchor_in_the_project_markdown_resolves():
    import check_doc_links

    assert check_doc_links.check() == []


def test_contract_permission_names_are_the_implemented_ones():
    from cawbase_content_engine import ScopePermission

    section = CONTRACT.split("### 12.1")[1].split("### 12.2")[0]
    named = set(re.findall(r"`((?:text|contexts):[a-z]+)`", section))
    implemented = {p.value for p in ScopePermission if p.value.startswith(("text:", "contexts:"))}
    assert named == implemented == {"text:protect", "text:restore", "contexts:read", "contexts:delete"}
    assert "17 a **21**" in section or "de **17** a **21**" in section


def test_contract_error_codes_are_the_implemented_ones():
    from cawbase_content_engine import errors

    section = CONTRACT.split("### 12.2")[1].split("## 13")[0]
    table = set(re.findall(r"^\| `([a-z_]+)` \|", section, re.M))
    codes = {c.code for c in vars(errors).values() if isinstance(c, type) and issubclass(c, errors.EngineError)}
    assert len(table) >= 13 and table <= codes
    for code in ("marker_collision", "marker_malformed", "new_sensitive_data", "mapping_integrity_failed"):
        assert code in table


def test_documented_version_and_levels_match_the_code():
    from cawbase_content_engine import __version__
    from cawbase_content_engine.constants import RUNTIME_LEVEL, SCHEMA_LEVEL

    assert (__version__, SCHEMA_LEVEL, RUNTIME_LEVEL) == ("0.7.0", 6, 1)
    for doc in ("docs/text-protection.md", "docs/features/A06-proteccion-y-restauracion-de-texto.md", "README.md"):
        text = (ROOT / doc).read_text(encoding="utf-8")
        assert "0.5.0" in text and "0004" in text, doc
