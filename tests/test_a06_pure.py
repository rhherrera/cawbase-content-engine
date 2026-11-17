"""A06 pure logic (no database): parser classes, interval union, restoration classification, single-pass assembly,
cryptographic composition and the configuration types. AC-A06-06/07/08/14/30/31/32/36/57/58/59/60 (pure parts)."""

from __future__ import annotations

import itertools
import os
import random
import time

import pytest

from cawbase_content_engine.errors import (
    AnalysisSourceInvalidError,
    InvalidConfigurationError,
    MappingIntegrityError,
    ProtectionKeyUnavailableError,
    ValidationFailedError,
)
from cawbase_content_engine.protection import crypto
from cawbase_content_engine.protection.grouping import classify_restore, plan_protection, union_intervals
from cawbase_content_engine.protection.markers import (
    ALPHABET,
    MARKER_LEN,
    find_markers,
    is_strict,
    marker_of,
    markers_of_all,
    new_token,
    scan,
    token_of,
)
from cawbase_content_engine.protection.ops import assemble_restored
from cawbase_content_engine.protection.types import KeyRing, ProtectionLimits, ProtectionSetup

T16 = "7Q4K9M2XJ8WH3NV5"
S = f"[[CCE1:{T16}]]"


# ---- markers and the bounded parser (docs 6.1-6.3) -------------------------------------------------------
def test_marker_shape_and_token_properties():
    tok = new_token()
    m = marker_of(tok)
    assert len(m) == MARKER_LEN == 25 and m.isascii() and is_strict(m) and token_of(m) == tok
    assert set(tok) <= set(ALPHABET) and len(tok) == 16 and not set("ILOU") & set(tok)
    assert len({new_token() for _ in range(2000)}) == 2000  # 80 random bits: no clash in practice


def kinds(text: str) -> str:
    s = scan(text)
    return ("S" if s.strict else "") + ("ALT" if s.altered else "") or "none"


@pytest.mark.parametrize(
    "text,expected",
    [
        (S, "S"),  # strict
        (f"[[cce1:{T16.lower()}]]", "ALT"),  # R1: capitalization
        (f"[[CCE1: {T16}]]", "ALT"),  # R1: spacing
        (f"[CCE1:{T16}]", "ALT"),  # R1: one bracket
        (f"CCE1:{T16}", "ALT"),  # R1: no bracket
        ("[[CCE1:7Q4K9M2XJ8WH3NVI]]", "ALT"),  # R1: a letter outside the alphabet
        (f"[[CCE2:{T16}]]", "ALT"),  # R1: another version
        ("[[CCE1:7Q4K9M2X]]", "ALT"),  # R2: truncated token
        (f"[[CCE1:{T16}AB]]", "ALT"),  # R2: lengthened token
        ("[ [ cce1 : x", "ALT"),  # R2: spaced prefix
        ("[CCE report]", "none"),  # ordinary prose
        ("CCE1", "none"),
        ("[[CCE1]]", "none"),
        ("cce: nota", "none"),
        ("CCE1: nota breve", "none"),
        ("[ CCE ]", "none"),
        ("Informe CCE", "none"),
        ("[CCE1:7Q4K9M2X]", "none"),  # truncated with a single bracket: not recognized (documented limit)
        ("[[CCE 7Q4K9M2XJ8WH3NV5]]", "none"),  # missing version / colon
        (f"[[CCE1 {T16}]]", "none"),
        (f"{S}]", "S"),  # an extra bracket after a strict marker is just prose
        (f"[[{S}", "S"),
    ],
)
def test_parser_classes(text, expected):  # AC-A06-14, 30, 31, 32, 59, 60
    assert kinds(text) == expected


def test_strict_marker_wins_over_inner_classes():  # AC-A06-60 (a)
    s = scan(f"antes {S} despues")
    assert len(s.strict) == 1 and not s.altered
    assert scan(f"{S} y también [[cce1:{T16.lower()}]]").altered  # a second, altered one is still caught


def test_prefix_with_any_continuation_is_recognized():  # AC-A06-60 (b)
    for t in ("[[CCE1:", "[[cce9:lo que sea", "[ [ CCE12 : x"):
        assert scan(t).altered, t


def test_core_needs_exactly_sixteen_isolated_alphanumerics():  # AC-A06-60 (c)
    assert scan("CCE1:abcdefghijklmnop").altered  # 16: residual false positive, accepted and documented
    assert not scan("CCE1:abcdefghijklmno").altered  # 15 without a double-bracket prefix
    assert not scan("CCE1:abcdefghijklmnopq").altered  # 17
    assert not scan(f"xCCE1:{T16}").altered  # glued to a preceding alphanumeric: not the identifier


def test_ascii_only_no_unicode_equivalences():  # AC-A06-31, 60 (d)
    kelvin = "K"  # would match [k] under Unicode case folding
    token = T16.replace("K", kelvin)
    assert kelvin in token
    assert not scan(f"CCE1:{token}").altered and not scan(f"[CCE1:{token}]").altered
    assert not scan(f"［［CCE1：{T16}］］").altered  # full-width brackets and colon
    assert not scan("CCE1:" + "‍".join(T16)).altered  # ZWJ inside the token
    assert scan(f"[[CCE1:{T16[:8]}\n{T16[8:]}]]").altered  # ...because the double-bracket prefix survives


def test_linear_time_on_adversarial_maximum_inputs():  # AC-A06-60 (e): measured, not a threshold
    for text in ("[" * 8192, "[ " * 4096, "cce1" + " " * 8000, "[[cce1:" + "a" * 8000, "cce1:" * 1600):
        t0 = time.perf_counter()
        scan(text)
        assert time.perf_counter() - t0 < 2.0


def test_find_markers_order_and_duplicates():
    a, b = marker_of(new_token()), marker_of(new_token())
    assert find_markers(f"{b} x {a} y {b}") == (b, a)
    assert find_markers("[CCE report] [[CCE1]]") == ()
    with pytest.raises(ValidationFailedError):
        find_markers(None)  # type: ignore[arg-type]


def test_delivered_material_extracts_strict_markers_only():  # AC-A06-60 (f)
    alt = f"[[cce1:{T16.lower()}]]"
    assert markers_of_all([f"a {S}", alt, "[CCE report]"]) == {S}


# ---- interval union and protection groups (docs 5.2-5.3) --------------------------------------------------
def groups(findings, n=100, default="pseudonymize", actions=None):
    return [(g.start, g.end, g.action) for g in plan_protection(findings, n, default, actions or {})]


def test_union_transitive_closure_and_adjacency():  # AC-A06-07
    assert union_intervals([(0, 10), (8, 20), (19, 30)]) == [(0, 30)]
    assert union_intervals([(0, 5), (5, 9)]) == [(0, 5), (5, 9)]  # adjacent: not merged
    assert union_intervals([(0, 30), (5, 10)]) == [(0, 30)]  # nested


def test_groups_are_independent_of_input_order_for_every_permutation():  # AC-A06-07
    fs = [("a", 0, 10), ("b", 8, 20), ("a", 19, 30), ("b", 40, 45), ("a", 45, 50)]
    expected = groups(fs)
    assert expected == [(0, 30, "pseudonymize"), (40, 45, "pseudonymize"), (45, 50, "pseudonymize")]
    for perm in itertools.permutations(fs):
        assert groups(list(perm)) == expected


def test_duplicate_type_on_same_interval_and_nesting_do_not_change_the_group():
    base = groups([("a", 0, 10)])
    assert groups([("a", 0, 10), ("b", 0, 10), ("a", 2, 5)]) == base


def test_redaction_wins_for_the_whole_group_regardless_of_order():  # AC-A06-08
    fs = [("a", 0, 10), ("secret", 8, 20), ("a", 19, 30)]
    for perm in itertools.permutations(fs):
        g = groups(list(perm), actions={"secret": "redact"})
        assert g == [(0, 30, "redact")]
    assert groups([("a", 0, 10), ("a", 20, 30)], default="redact", actions={"a": "pseudonymize"}) == [
        (0, 10, "pseudonymize"),
        (20, 30, "pseudonymize"),
    ]


def test_offsets_outside_the_text_are_never_clipped():
    for bad in ([("a", -1, 3)], [("a", 3, 3)], [("a", 4, 2)], [("a", 0, 101)]):
        with pytest.raises(AnalysisSourceInvalidError):
            plan_protection(bad, 100, "redact", {})


def test_random_union_matches_a_reference_implementation():
    rnd = random.Random(7)  # noqa: S311 - reproducible test data, not security
    for _ in range(300):
        fs = []
        for _ in range(rnd.randint(1, 12)):
            s = rnd.randint(0, 60)
            fs.append((rnd.choice("ab"), s, rnd.randint(s + 1, 70)))
        covered: list[set[int]] = []
        for _t, s, e in sorted(fs, key=lambda x: (x[1], x[2])):
            span = set(range(s, e))
            hit = [c for c in covered if c & span]
            for c in hit:
                span |= c
                covered.remove(c)
            covered.append(span)
        want = sorted((min(c), max(c) + 1) for c in covered)
        got = [(a, b) for a, b, _ in groups(fs, 70)]
        assert got == want


# ---- restoration classification (docs 7.3) ----------------------------------------------------------------
M1, M2 = (10, 35), (35, 60)


def plan(markers, findings, n=120):
    p = classify_restore(markers, findings, n)
    return list(p.groups), sorted(p.consumed), [(s, e) for _t, s, e in p.new_findings], p.ignored


@pytest.mark.parametrize(
    "markers,findings,groups,consumed,new,ignored",
    [
        ([M1], [], [], [], [], 0),
        ([M1], [("x", 10, 35)], [], [], [], 1),  # exact match: ignored
        ([M1], [("x", 12, 20)], [], [], [], 1),  # inside: ignored
        ([M1], [("x", 50, 60)], [(50, 60)], [], [(50, 60)], 0),  # outside every marker
        ([M1], [("x", 35, 50)], [(35, 50)], [], [(35, 50)], 0),  # adjacent: does not touch
        ([M1], [("x", 0, 10)], [(0, 10)], [], [(0, 10)], 0),  # adjacent on the left
        ([M1], [("x", 5, 15)], [(5, 35)], [0], [(5, 15)], 0),  # crosses the left edge
        ([M1], [("x", 30, 50)], [(10, 50)], [0], [(30, 50)], 0),  # crosses the right edge
        ([M1], [("x", 5, 50)], [(5, 50)], [0], [(5, 50)], 0),  # covers it with extra text
        ([M1], [("x", 10, 36)], [(10, 36)], [0], [(10, 36)], 0),  # covers exactly it and one more code point
        ([M1, M2], [("x", 20, 50)], [(10, 60)], [0, 1], [(20, 50)], 0),  # spans two adjacent markers
        ([M1, M2], [("x", 10, 60)], [(10, 60)], [0, 1], [(10, 60)], 0),  # covers both exactly: within neither
        (
            [(10, 35), (60, 85)],
            [("x", 5, 15), ("x", 30, 45), ("x", 44, 70)],
            [(5, 85)],
            [0, 1],
            [(5, 15), (30, 45), (44, 70)],
            0,
        ),  # transitive chain across two markers
        (
            [(10, 35), (60, 85)],
            [("x", 30, 40), ("x", 62, 70)],
            [(10, 40)],
            [0],
            [(30, 40)],
            1,
        ),  # an ignored finding inside M2 takes no part: M2 is neither touched nor consumed
        ([M1], [("x", 0, 10), ("x", 8, 20)], [(0, 35)], [0], [(0, 10), (8, 20)], 0),
        ([], [("x", 0, 10), ("y", 8, 20)], [(0, 20)], [], [(0, 10), (8, 20)], 0),  # no markers at all
    ],
)
def test_restore_classification_table(markers, findings, groups, consumed, new, ignored):  # AC-A06-36, 57, 58
    assert plan(markers, findings) == (groups, consumed, new, ignored)


def test_restore_classification_is_order_independent():  # AC-A06-58
    markers = [(10, 35), (60, 85)]
    findings = [("x", 5, 15), ("y", 30, 45), ("x", 44, 70), ("z", 62, 70), ("x", 12, 20)]
    base = plan(markers, findings)
    assert base[0] == [(5, 85)] and base[3] == 2
    for perm in itertools.permutations(findings):
        assert plan(markers, list(perm)) == base


def test_restore_invalid_offsets_are_rejected():
    with pytest.raises(AnalysisSourceInvalidError):
        classify_restore([M1], [("x", 0, 500)], 120)


def test_assembly_is_a_single_pass_and_never_reinterprets_originals():  # AC-A06-33
    token = new_token()
    text = f"A {marker_of(token)} B"
    original = f"[[CCE1:{T16}]] y {marker_of(token)}"  # an original that LOOKS like markers
    out, restored = assemble_restored(text, [(2, 27, marker_of(token))], frozenset(), [], {token: original})
    assert out == f"A {original} B" and restored == 1


def test_assembly_with_redaction_and_consumed_markers():
    t1, t2 = new_token(), new_token()
    text = f"{marker_of(t1)} mid {marker_of(t2)}"
    markers = [(0, 25, marker_of(t1)), (30, 55, marker_of(t2))]
    out, restored = assemble_restored(text, markers, frozenset({1}), [(26, 55)], {t1: "ONE"})
    assert out == "ONE [REDACTED]" and restored == 1


# ---- cryptographic composition (docs 9) ---------------------------------------------------------------------
KW = dict(scope_id="a", context_id="ctx_" + "0" * 32, kind="conversation", token="T" * 16, version="v1")


def test_known_answer_self_test():
    crypto.self_test()


def test_record_round_trip_padding_and_every_aad_field():  # AC-A06-39, 42
    master = os.urandom(32)
    ik = crypto.new_index_key()
    eq = crypto.equality_index(ik, "Ana Ñ😀")
    salt, nonce, ct = crypto.seal_fragment(master, eq_index=eq, fragment="Ana Ñ😀", **KW)
    assert (len(ct) - 16) % 64 == 0  # padded plaintext (+16-byte tag): the exact length stays hidden
    sealed = dict(salt=salt, nonce=nonce, ciphertext=ct)
    assert crypto.open_fragment(master, eq_index=eq, **sealed, **KW).decode() == "Ana Ñ😀"
    for field_, other in [
        ("scope_id", "b"),
        ("context_id", "ctx_" + "1" * 32),
        ("kind", "job"),
        ("token", "U" * 16),
        ("version", "v2"),
    ]:
        with pytest.raises(MappingIntegrityError):
            crypto.open_fragment(master, eq_index=eq, **sealed, **{**KW, field_: other})
    with pytest.raises(MappingIntegrityError):  # the equality index is authenticated as part of the record
        crypto.open_fragment(master, eq_index=os.urandom(32), **sealed, **KW)
    with pytest.raises(MappingIntegrityError):
        crypto.open_fragment(os.urandom(32), eq_index=eq, **sealed, **KW)  # wrong key under the same label
    for name in ("salt", "nonce", "ciphertext"):
        bad = dict(sealed)
        b = bytearray(bad[name])
        b[0] ^= 1
        bad[name] = bytes(b)
        with pytest.raises(MappingIntegrityError):
            crypto.open_fragment(master, eq_index=eq, **bad, **KW)
    with pytest.raises(MappingIntegrityError):
        crypto.open_fragment(master, eq_index=eq, salt=b"x", nonce=nonce, ciphertext=ct, **KW)


def test_two_seals_of_the_same_fragment_differ_and_ciphertext_hides_the_value():
    master, eq = os.urandom(32), os.urandom(32)
    a = crypto.seal_fragment(master, eq_index=eq, fragment="Ana Ejemplo", **KW)
    b = crypto.seal_fragment(master, eq_index=eq, fragment="Ana Ejemplo", **KW)
    assert a != b and b"Ana Ejemplo" not in a[2]


def test_index_key_wrap_and_equality_index_are_keyed_per_context():
    master = os.urandom(32)
    ik1, ik2 = crypto.new_index_key(), crypto.new_index_key()
    w = crypto.wrap_index_key(master, scope_id="a", context_id="x", version="v1", raw=ik1)
    assert crypto.unwrap_index_key(master, scope_id="a", context_id="x", version="v1", wrapped=w) == ik1
    for bad in (dict(scope_id="b"), dict(context_id="y"), dict(version="v2")):
        with pytest.raises(MappingIntegrityError):
            crypto.unwrap_index_key(master, **{**dict(scope_id="a", context_id="x", version="v1"), **bad}, wrapped=w)
    assert crypto.equality_index(ik1, "v") == crypto.equality_index(ik1, "v") != crypto.equality_index(ik2, "v")
    assert (
        crypto.equality_index(ik1, "v") != crypto.equality_index(ik1, "w")
        and len(crypto.equality_index(ik1, "v")) == 32
    )


def test_key_access_failures_are_typed_and_never_echo_material():
    class P:
        def active_version(self):
            raise RuntimeError("boom")

        def key(self, v):
            return b"short"

    with pytest.raises(ProtectionKeyUnavailableError):
        crypto.active_version(P())
    with pytest.raises(ProtectionKeyUnavailableError):
        crypto.master_key(P(), "v1")
    with pytest.raises(ProtectionKeyUnavailableError):
        crypto.master_key(KeyRing("v1", {"v1": os.urandom(32)}), "not a valid label!")


# ---- configuration types (docs 9.1, 13, 15) -----------------------------------------------------------------
def test_keyring_validation_and_repr_hides_key_material():
    key = os.urandom(32)
    ring = KeyRing("v1", {"v1": key})
    assert key.hex() not in repr(ring) and str(key) not in repr(ring) and "keys" not in repr(ring)
    for bad in (
        lambda: KeyRing("v1", {"v1": b"x" * 31}),
        lambda: KeyRing("v2", {"v1": key}),
        lambda: KeyRing("bad label", {"bad label": key}),
        lambda: KeyRing("v1", {"v1": key, "ok": "string"}),  # type: ignore[dict-item]
    ):
        with pytest.raises(InvalidConfigurationError):
            bad()


def test_keyring_from_directory(tmp_path):
    (tmp_path / "v1.key").write_bytes(os.urandom(32))
    (tmp_path / "v2.key").write_bytes(os.urandom(32))
    (tmp_path / "notes.txt").write_text("ignored")
    ring = KeyRing.from_directory(tmp_path, active="v2")
    assert ring.active_version() == "v2" and ring.key("v1") is not None and ring.key("zz") is None


def test_setup_and_limits_validation():
    ring = KeyRing("v1", {"v1": os.urandom(32)})
    ok = ProtectionSetup(keys=ring)
    assert (
        ok.context_ttl_s == 7 * 86400 and ok.max_context_ttl_s == 30 * 86400 and ok.metadata_retention_s == 30 * 86400
    )
    for kw in (
        dict(context_ttl_s=30),
        dict(context_ttl_s=10 * 86400, max_context_ttl_s=5 * 86400),
        dict(max_context_ttl_s=3651 * 86400),
        dict(metadata_retention_s=0),
        dict(limits=object()),
    ):
        with pytest.raises(InvalidConfigurationError):
            ProtectionSetup(keys=ring, **kw)  # type: ignore[arg-type]
    with pytest.raises(InvalidConfigurationError):
        ProtectionSetup(keys=object())  # type: ignore[arg-type]
    with pytest.raises(InvalidConfigurationError):
        ProtectionLimits(max_mappings_per_context=0)
