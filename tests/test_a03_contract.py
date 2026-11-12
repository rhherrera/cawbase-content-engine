"""A03 pure contract tests: exact numerics, ranking/resolution, strict output validation, locators, mock
configuration (AC-A03-20..25, 30..35, 40, 52..54). No database needed."""

from __future__ import annotations

import asyncio
import copy
import decimal
import json
from datetime import UTC, datetime

import pytest

from cawbase_content_engine import (
    InferenceFixtureNotFoundError,
    InferenceOutputInvalidError,
    InvalidConfigurationError,
    Limits,
    MockFixture,
    MockInferenceAdapter,
    ResolvedProfile,
)
from cawbase_content_engine.inference import build_request, definitions_digest, prepare, request_key
from cawbase_content_engine.inference.build import exact, rank_scores, resolve
from cawbase_content_engine.inference.output import validate_output

from .a03_helpers import CATS, TYPES

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def synthetic(*, mode="resolution", threshold=0.5, tolerance=0.05, cats=None, types=TYPES, language="es"):
    cats = CATS if cats is None else cats

    def element(i, id_key, extra, value):
        return {
            id_key: i,
            "name": i,
            "definition": f"def {i}",
            "examples": {"positive": [], "negative": []},
            "translations": None,
            extra: value,
            "source_revision": 1,
        }

    snapshot = {
        "snapshot_schema_version": 1,
        "profile": {"profile_id": "p", "display_name": "P", "description": None, "language": language},
        "catalog_provenance": None,
        "ranking": {"mode": mode, "eligibility_threshold": threshold, "tie_tolerance": tolerance},
        "capabilities": {"classification": bool(cats), "detection": bool(types)},
        "categories": [element(c, "category_id", "priority", p) for c, p in sorted(cats.items())],
        "sensitive_types": [element(t, "type_id", "origin_entry", None) for t in sorted(types)],
    }
    from cawbase_content_engine import ProfileVersion

    version = ProfileVersion(
        scope_id="s",
        profile_id="p",
        version_id="pvr_" + "0" * 32,
        version_number=1,
        source_draft_revision=1,
        content_hash="0" * 64,
        capabilities=snapshot["capabilities"],
        snapshot_bytes=1,
        published_at=NOW,
        published_by=None,
        snapshot=snapshot,
    )
    return prepare(ResolvedProfile(selection="explicit", version=version))


def request_and_prepared(text="Hola Ana Ejemplo", **kw):
    prepared = synthetic(**kw)
    caps = tuple(c for c in ("classification", "detection") if prepared.configured(c))
    req = build_request(prepared, request_id="r", text=text, context=None, capabilities=caps, limits=Limits())
    return prepared, req


def raw_for(text, prepared, *, scores=None, findings=()):
    out = {"contract_version": 1}
    cats = list(prepared.element_ids("classification"))
    if cats:
        scores = scores or {c: 0.5 for c in cats}
        out["classification"] = {
            "status": "completed",
            "reason": None,
            "coverage": {"groups": [{"ids": sorted(scores), "regions": [{"start": 0, "end": len(text)}]}]},
            "scores": [{"category_id": k, "score": v} for k, v in scores.items()],
        }
    types = list(prepared.element_ids("detection"))
    if types:
        out["detection"] = {
            "status": "completed",
            "reason": None,
            "coverage": {"groups": [{"ids": sorted(types), "regions": [{"start": 0, "end": len(text)}]}]},
            "findings": [{"type_id": t, "start": s, "end": e, "text": text[s:e]} for t, s, e in findings],
        }
    return out


def problems(raw, prepared, req):
    with pytest.raises(InferenceOutputInvalidError) as ei:
        validate_output(raw, prepared, req)
    return ei.value.problems


class TestExactNumerics:
    def test_decimal_boundary_09_07_02(self):  # precision E
        assert 0.9 - 0.7 > 0.2  # binary floating point would get this wrong
        assert exact(0.9) - exact(0.7) == exact(0.2)

    @pytest.mark.parametrize("prec", [1, 2, 5, 28, 100])
    def test_independent_of_the_host_decimal_context(self, prec):
        with decimal.localcontext() as ctx:
            ctx.prec = prec
            ctx.rounding = decimal.ROUND_DOWN
            assert exact(0.9) - exact(0.7) == exact(0.2)
            ranked = rank_scores((("a", 0.9), ("b", 0.7)), 0.7)
            assert [r.eligible for r in ranked] == [True, True]
            res = resolve(ranked, 0.2, synthetic(cats={"a": 2, "b": 1}))
            assert res.reason == "priority_tiebreak" and res.selected_category_id == "b"
        assert decimal.getcontext().prec == 28  # the global context is never touched

    @pytest.mark.parametrize("value", [5e-324, 1.1102230246251565e-16, 0.9999999999999999, 1e-05, 0.1, 1.0, 0.0, 1])
    def test_extreme_finite_values_are_exact_and_ordered(self, value):
        assert exact(value) == exact(float(value))
        assert 0 <= exact(value) <= 1
        ranked = rank_scores((("a", value), ("b", 0.5)), value)
        assert {r.category_id: r.eligible for r in ranked}["a"] is True  # score == threshold is eligible

    def test_one_step_below_threshold_is_not_eligible(self):  # AC-A03-21
        below = 0.5 - 2**-53 / 2  # the float just below 0.5
        assert below < 0.5
        ranked = rank_scores((("a", below), ("b", 0.5)), 0.5)
        assert {r.category_id: r.eligible for r in ranked} == {"a": False, "b": True}

    def test_threshold_and_score_bounds(self):  # AC-A03-21
        assert [r.eligible for r in rank_scores((("a", 0.0), ("b", 1.0)), 0.0)] == [True, True]
        assert [r.eligible for r in rank_scores((("a", 0.0), ("b", 1.0)), 1.0)] == [True, False]


class TestRankingAndResolution:
    def test_ranking_order_ties_and_technical_tiebreak(self):  # AC-A03-22
        ranked = rank_scores((("zeta", 0.7), ("alfa", 0.7), ("mid", 0.9), ("low", 0.1)), 0.5)
        assert [(r.rank, r.category_id) for r in ranked] == [(1, "mid"), (2, "alfa"), (2, "zeta"), (4, "low")]
        assert [r.eligible for r in ranked] == [True, True, True, False]  # not eligible rows are not dropped

    def prep(self, **cats):
        return synthetic(cats=cats)

    def test_boundaries_of_tolerance(self):  # AC-A03-23
        p = self.prep(a=2, b=1)
        r = lambda s_b, tol: resolve(rank_scores((("a", 0.9), ("b", s_b)), 0.0), tol, p)  # noqa: E731
        at = r(0.7, 0.2)  # distance == tolerance: candidate (decimal reading)
        assert (
            at.reason == "priority_tiebreak"
            and at.selected_category_id == "b"
            and at.candidate_category_ids == ("a", "b")
        )
        above = r(0.6999999999999999, 0.2)  # a hair beyond
        assert above.reason == "highest_score" and above.selected_category_id == "a"
        zero = r(0.9, 0.0)  # tolerance 0: exact tie only
        assert zero.reason == "priority_tiebreak" and zero.selected_category_id == "b"
        assert r(0.8999999999999999, 0.0).selected_category_id == "a"

    def test_no_transitive_chaining(self):  # AC-A03-23
        p = self.prep(a=3, b=2, c=1)
        res = resolve(rank_scores((("a", 0.90), ("b", 0.85), ("c", 0.80)), 0.0), 0.05, p)
        assert res.candidate_category_ids == ("a", "b") and res.selected_category_id == "b"  # c is 0.10 from max

    def test_priority_does_not_change_scores_or_order(self):  # AC-A03-23
        p = self.prep(a=9, b=1)
        ranked = rank_scores((("a", 0.8), ("b", 0.2)), 0.0)
        assert [r.category_id for r in ranked] == ["a", "b"]
        assert resolve(ranked, 0.05, p).selected_category_id == "a"  # b has the better priority but is out of tolerance

    def test_no_eligible_category(self):  # AC-A03-24
        res = resolve(rank_scores((("a", 0.2), ("b", 0.1)), 0.5), 0.05, self.prep(a=1, b=2))
        assert (res.selected_category_id, res.reason, res.candidate_category_ids) == (None, "no_eligible_category", ())

    def test_missing_priority_is_never_invented(self):
        from cawbase_content_engine import InternalError

        p = synthetic(cats={"a": None, "b": None})  # type: ignore[dict-item]
        with pytest.raises(InternalError):
            resolve(rank_scores((("a", 0.9), ("b", 0.9)), 0.0), 0.0, p)


class TestValidatorRejections:
    TEXT = "Hola Ana Ejemplo"

    @pytest.fixture
    def ctx(self):
        prepared, req = request_and_prepared(self.TEXT)
        return prepared, req, raw_for(self.TEXT, prepared, findings=[("personal_data", 5, 16)])

    def test_valid_baseline(self, ctx):
        prepared, req, raw = ctx
        out = validate_output(raw, prepared, req)
        assert out["detection"].findings == (("personal_data", 5, 16),) and out["classification"].coverage.complete

    @pytest.mark.parametrize(
        ("mutate", "code"),
        [
            (lambda o: o.update(contract_version=2), "contract_version_mismatch"),
            (lambda o: o.update(contract_version=True), "contract_version_mismatch"),
            (lambda o: o.update(extra=1), "unknown_field"),
            (lambda o: o.update(winner="finance"), "unknown_field"),
            (lambda o: o.pop("classification"), "capability_missing"),
            (lambda o: o["detection"].update(foo=1), "unknown_field"),
            (lambda o: o["detection"].pop("coverage"), "missing_field"),
            (lambda o: o["detection"].update(status="done"), "invalid_status"),
            (lambda o: o["detection"].update(reason="abstained"), "reason_invalid"),
            (lambda o: o["detection"].update(status="inconclusive", reason=None), "reason_invalid"),
            (lambda o: o["detection"].update(status="inconclusive", reason="free text"), "reason_invalid"),
            (
                lambda o: o["detection"].update(status="inconclusive", reason="partial_coverage"),
                "reason_coverage_mismatch",
            ),
            (lambda o: o["classification"]["scores"][0].update(score=1.5), "invalid_score"),
            (lambda o: o["classification"]["scores"][0].update(score=-0.1), "invalid_score"),
            (lambda o: o["classification"]["scores"][0].update(score=True), "invalid_score"),
            (lambda o: o["classification"]["scores"][0].update(score="0.5"), "invalid_score"),
            (lambda o: o["classification"]["scores"][0].update(score=float("nan")), "invalid_score"),
            (lambda o: o["classification"]["scores"][0].update(score=float("inf")), "invalid_score"),
            (lambda o: o["classification"]["scores"][0].update(category_id="ghost"), "unknown_id"),
            (lambda o: o["classification"]["scores"].append(dict(o["classification"]["scores"][0])), "duplicate_id"),
            (lambda o: o["classification"]["scores"].pop(), "scores_coverage_mismatch"),
            (lambda o: o["classification"]["coverage"]["groups"][0]["ids"].append("finance"), "duplicate_id"),
            (lambda o: o["detection"]["coverage"]["groups"][0]["ids"].append("ghost"), "unknown_id"),
            (lambda o: o["detection"]["coverage"]["groups"][0]["regions"][0].update(end=999), "region_out_of_range"),
            (lambda o: o["detection"]["coverage"]["groups"][0]["regions"][0].update(start=-1), "region_out_of_range"),
            (lambda o: o["detection"]["coverage"]["groups"][0].update(regions=[]), "empty_group"),
            (
                lambda o: o["detection"]["coverage"]["groups"].append({"ids": [], "regions": [{"start": 0, "end": 1}]}),
                "empty_group",
            ),
            (
                lambda o: o["detection"]["coverage"]["groups"][0]["regions"].extend([{"start": 3, "end": 5}]),
                "regions_not_canonical",
            ),
            (
                lambda o: o["detection"]["coverage"].update(
                    groups=[{"ids": ["password"], "regions": [{"start": 0, "end": 4}, {"start": 4, "end": 16}]}]
                ),
                "regions_not_canonical",
            ),
            (
                lambda o: o["detection"]["coverage"].update(
                    groups=[{"ids": ["password"], "regions": [{"start": 0, "end": 16}]}]
                ),
                "status_coverage_mismatch",
            ),
            (lambda o: o["detection"]["findings"][0].update(start=6), "fragment_mismatch"),
            (lambda o: o["detection"]["findings"][0].update(text="Ana"), "fragment_mismatch"),
            (lambda o: o["detection"]["findings"][0].update(type_id="ghost"), "unknown_id"),
            (lambda o: o["detection"]["findings"][0].update(end=99), "offset_out_of_range"),
            (lambda o: o["detection"]["findings"][0].update(start=5, end=5), "offset_out_of_range"),
            (lambda o: o["detection"]["findings"][0].update(start="5"), "invalid_offset"),
            (lambda o: o["detection"]["findings"][0].update(start=True), "invalid_offset"),
            (lambda o: o["detection"]["findings"].append(dict(o["detection"]["findings"][0])), "duplicate_finding"),
        ],
    )
    def test_rejected_whole(self, ctx, mutate, code):  # AC-A03-40
        prepared, req, raw = ctx
        raw = copy.deepcopy(raw)
        mutate(raw)
        assert code in {p["code"] for p in problems(raw, prepared, req)}

    @pytest.mark.parametrize(
        "score", [10**400, -(10**400), 10**30, -1, 2, 1.0000000000000002, -1e-300, 1e308, float("-inf")]
    )
    def test_out_of_range_numbers_are_rejected_safely(self, ctx, score):  # no OverflowError, whole output rejected
        prepared, req, raw = ctx
        raw = copy.deepcopy(raw)
        raw["classification"]["scores"][0]["score"] = score
        ps = problems(raw, prepared, req)
        assert {"code": "invalid_score", "capability": "classification", "index": "0"} in ps

    @pytest.mark.parametrize("score", [0, 1, 0.0, 1.0, 5e-324, 0.9999999999999999])
    def test_boundary_scores_still_accepted_unchanged(self, ctx, score):
        prepared, req, raw = ctx
        raw = copy.deepcopy(raw)
        raw["classification"]["scores"][0]["score"] = score
        out = validate_output(raw, prepared, req)
        assert out["classification"].scores[0][1] == float(score)

    @pytest.mark.parametrize("raw", [None, [], "x", 5, {1: 2}])
    def test_not_an_object(self, ctx, raw):
        prepared, req, _ = ctx
        assert problems(raw, prepared, req)

    def test_not_serializable_and_too_large(self, ctx):
        prepared, req, raw = ctx
        raw = copy.deepcopy(raw)
        raw["detection"]["findings"].append({"x": {1, 2}})
        assert problems(raw, prepared, req) == [{"code": "output_not_serializable"}]
        raw = copy.deepcopy(ctx[2])
        raw["junk"] = "x" * (req.limits.max_output_bytes + 1)
        assert problems(raw, prepared, req) == [{"code": "output_too_large"}]

    def test_capability_not_executed(self):
        prepared, req = request_and_prepared(self.TEXT)
        req = build_request(
            prepared, request_id="r", text=self.TEXT, context=None, capabilities=("detection",), limits=Limits()
        )
        raw = raw_for(self.TEXT, prepared)  # also includes classification, which was not sent
        assert "capability_not_executed" in {p["code"] for p in problems(raw, prepared, req)}

    def test_finding_outside_declared_coverage_of_its_type(self, ctx):  # AC-A03-34
        prepared, req, raw = ctx
        raw = copy.deepcopy(raw)
        det = raw["detection"]
        det["status"], det["reason"] = "inconclusive", "partial_coverage"
        det["coverage"]["groups"] = [
            {"ids": ["password"], "regions": [{"start": 0, "end": 16}]},
            {"ids": ["personal_data"], "regions": [{"start": 0, "end": 4}]},
        ]
        assert "finding_outside_coverage" in {p["code"] for p in problems(raw, prepared, req)}
        det["coverage"]["groups"] = [
            {"ids": ["password"], "regions": [{"start": 0, "end": 16}]}
        ]  # type never evaluated
        assert "finding_outside_coverage" in {p["code"] for p in problems(raw, prepared, req)}

    def test_regions_and_finding_length_limits(self):  # AC-A03-41
        prepared = synthetic()
        text = "a" * 40
        lim = Limits(max_analysis_regions=2, max_finding_chars=3)
        req = build_request(prepared, request_id="r", text=text, context=None, capabilities=("detection",), limits=lim)
        raw = raw_for(text, prepared, findings=[("password", 0, 10)])
        raw.pop("classification")
        assert "finding_too_long" in {p["code"] for p in problems(raw, prepared, req)}
        raw = raw_for(text, prepared)
        raw.pop("classification")
        raw["detection"]["status"], raw["detection"]["reason"] = "inconclusive", "partial_coverage"
        raw["detection"]["coverage"]["groups"] = [
            {"ids": ["password"], "regions": [{"start": 0, "end": 2}, {"start": 4, "end": 6}, {"start": 8, "end": 10}]}
        ]
        assert "regions_limit_exceeded" in {p["code"] for p in problems(raw, prepared, req)}

    def test_problems_are_bounded_and_contentless(self, ctx):
        prepared, req, raw = ctx
        raw = copy.deepcopy(raw)
        raw["detection"]["findings"] = [
            {"type_id": f"SECRET-{i}", "start": 0, "end": 1, "text": "SECRET"} for i in range(60)
        ]
        ps = problems(raw, prepared, req)
        assert len(ps) <= 20 and "SECRET" not in json.dumps(ps)
        assert all(set(p) <= {"code", "capability", "index"} for p in ps)

    def test_output_is_copied_before_use(self, ctx):  # AC-A03-16
        prepared, req, raw = ctx
        out = validate_output(raw, prepared, req)
        raw["detection"]["findings"].clear()
        raw["classification"]["scores"].clear()
        assert out["detection"].findings and out["classification"].scores


class TestLocators:
    def check(self, text, frag_start, frag_end, type_id="personal_data", fragment=None):
        prepared, req = request_and_prepared(text)
        raw = raw_for(text, prepared)
        raw["detection"]["findings"] = [
            {
                "type_id": type_id,
                "start": frag_start,
                "end": frag_end,
                "text": text[frag_start:frag_end] if fragment is None else fragment,
            }
        ]
        return validate_output(raw, prepared, req)["detection"].findings

    def test_emoji_is_one_code_point(self):  # AC-A03-30
        text = "😀 Ana Ejemplo\r\né ok"
        assert len(text) == 20 and text.index("Ana Ejemplo") == 2
        assert self.check(text, 2, 13) == (("personal_data", 2, 13),)

    def test_utf16_style_offsets_fail(self):  # AC-A03-30
        text = "😀 Ana Ejemplo"
        with pytest.raises(InferenceOutputInvalidError):
            self.check(text, 3, 14, fragment="Ana Ejemplo")  # UTF-16 index of the name

    def test_combining_sequences_and_crlf_and_zwj(self):  # AC-A03-30
        text = "a\r\nb é \U0001f468‍\U0001f469‍\U0001f467."
        assert len(text) == 14
        assert self.check(text, 1, 3)  # \r\n are two code points
        assert self.check(text, 5, 6)  # splitting base + combining mark is legal: unit = code point
        assert self.check(text, 8, 13)  # ZWJ family = 5 code points

    def test_repetitions_second_occurrence(self):  # precision D
        text = "Cuenta de Ana Ejemplo. Ana Ejemplo firma."
        first, second = text.index("Ana Ejemplo"), text.rindex("Ana Ejemplo")
        assert first != second
        assert self.check(text, second, second + 11)
        assert self.check(text, first, first + 11)
        # pointing at a different, identical occurrence than intended cannot be detected structurally:
        prepared, req = request_and_prepared(text)
        raw = raw_for(text, prepared)
        raw["detection"]["findings"] = [
            {"type_id": "personal_data", "start": first, "end": first + 11, "text": "Ana Ejemplo"}
        ]
        assert validate_output(raw, prepared, req)["detection"].findings == (("personal_data", first, first + 11),)
        with pytest.raises(InferenceOutputInvalidError):  # a shift onto different text does fail, no repair
            self.check(text, first + 1, first + 12, fragment="Ana Ejemplo")

    def test_overlaps_and_two_types_on_one_fragment(self):  # AC-A03-31
        text = "clave Secreto-123"
        prepared, req = request_and_prepared(text)
        raw = raw_for(text, prepared)
        frag = lambda t, s, e: {"type_id": t, "start": s, "end": e, "text": text[s:e]}  # noqa: E731
        raw["detection"]["findings"] = [
            frag("personal_data", 6, 17),
            frag("password", 6, 17),
            frag("password", 6, 12),
            frag("personal_data", 0, 10),
        ]
        found = validate_output(raw, prepared, req)["detection"].findings
        assert len(found) == 4  # overlapping findings and different types over the same fragment are all kept


class TestMockConfiguration:
    def fx(self, i="a", text="t"):
        prepared, req = request_and_prepared(text)
        return MockFixture.for_request(i, req, {"kind": "output", "output": raw_for(text, prepared)})

    def test_must_be_explicit(self):  # AC-A03-50
        with pytest.raises(InvalidConfigurationError):
            MockInferenceAdapter([self.fx()])
        with pytest.raises(InvalidConfigurationError):
            MockInferenceAdapter([self.fx()], acknowledge_simulated=1)  # type: ignore[arg-type]

    def test_duplicates_rejected(self):  # AC-A03-52
        with pytest.raises(InvalidConfigurationError):
            MockInferenceAdapter([self.fx("a"), self.fx("b")], acknowledge_simulated=True)  # same key
        with pytest.raises(InvalidConfigurationError):
            MockInferenceAdapter([self.fx("a", "x"), self.fx("a", "y")], acknowledge_simulated=True)  # same id

    @pytest.mark.parametrize(
        "bad",
        [
            {"outcome": {"kind": "nope"}},
            {"outcome": {"kind": "unavailable", "reason": "gone"}},
            {"outcome": {"kind": "output"}},
            {"capabilities": ()},
            {"capabilities": ("magic",)},
            {"definitions_digest": "short"},
        ],
    )
    def test_fixture_structure_validated(self, bad):  # AC-A03-52
        d = self.fx().to_dict()
        for k, v in bad.items():
            if k == "outcome":
                d["outcome"] = v
            elif k == "capabilities":
                d["match"]["capabilities"] = list(v)
            else:
                d["match"][k] = v
        with pytest.raises(InvalidConfigurationError):
            MockFixture.from_dict(d)

    def test_invalid_outputs_are_allowed_in_fixtures(self):  # to exercise the validator
        d = self.fx().to_dict()
        d["outcome"] = {"kind": "output", "output": {"garbage": True}}
        MockFixture.from_dict(d)

    def test_file_loading_and_descriptor(self, tmp_path):  # AC-A03-50, 51
        f = tmp_path / "fx.json"
        f.write_text(json.dumps({"fixtures": [self.fx().to_dict()]}))
        a = MockInferenceAdapter.from_file(f, acknowledge_simulated=True)
        d = a.descriptor
        assert (d.kind, d.adapter_id, d.model_id, d.model_version) == ("mock", "cce-mock-fixtures", None, None)
        assert d.configuration_id.startswith("fxs-") and len(d.configuration_id) == 20
        for bad in ("{", json.dumps({"fixtures": 3}), json.dumps([])):
            f.write_text(bad)
            with pytest.raises(InvalidConfigurationError):
                MockInferenceAdapter.from_file(f, acknowledge_simulated=True)
        with pytest.raises(InvalidConfigurationError):
            MockInferenceAdapter.from_file(tmp_path / "missing.json", acknowledge_simulated=True)

    async def test_fixtures_copied_on_load_and_per_call(self):  # AC-A03-54
        fx = self.fx()
        a = MockInferenceAdapter([fx], acknowledge_simulated=True)
        fx.outcome["output"]["contract_version"] = 99  # mutating the caller's object after load
        prepared, req = request_and_prepared("t")
        first = await a.infer(req)
        assert first["contract_version"] == 1
        first["classification"]["scores"].clear()
        second = await a.infer(req)
        assert second["classification"]["scores"]  # a per-call copy: calls cannot interfere

    async def test_no_heuristics_unknown_input_fails(self):  # AC-A03-52
        a = MockInferenceAdapter([self.fx()], acknowledge_simulated=True)
        prepared, req = request_and_prepared("contraseña: hunter2")  # looks sensitive; no fixture => error
        with pytest.raises(InferenceFixtureNotFoundError):
            await a.infer(req)

    def test_definitions_digest_is_canonical_and_covers_language(self):  # review point
        p_es = synthetic(language="es")
        p_en = synthetic(language="en")
        d = lambda p: definitions_digest(p.language, p.categories, p.sensitive_types)  # noqa: E731
        assert d(p_es) != d(p_en) and d(p_es) == d(synthetic(language="es"))
        # priorities, mode, threshold and tolerance do not take part
        assert d(p_es) == d(
            synthetic(cats={"finance": 9, "legal": 8, "hr": 7}, mode="ranking", threshold=0.9, tolerance=0.3)
        )
        # a changed definition does
        other = synthetic()
        changed = type(other)(
            **{
                **other.__dict__,
                "categories": tuple(type(c)(**{**c.__dict__, "definition": "other"}) for c in other.categories),
            }
        )
        assert d(changed) != d(other)
        assert request_key(request_and_prepared("t")[1])[3] == d(synthetic())

    async def test_fixture_keys_depend_on_capabilities_and_context(self):
        prepared = synthetic()
        both = build_request(
            prepared,
            request_id="r",
            text="t",
            context=None,
            capabilities=("classification", "detection"),
            limits=Limits(),
        )
        det = build_request(
            prepared, request_id="r", text="t", context=None, capabilities=("detection",), limits=Limits()
        )
        ctx = build_request(
            prepared, request_id="r", text="t", context="c", capabilities=("detection",), limits=Limits()
        )
        assert len({request_key(both), request_key(det), request_key(ctx)}) == 3
        assert request_key(both) == request_key(
            build_request(
                prepared,
                request_id="other",
                text="t",
                context=None,
                capabilities=("classification", "detection"),
                limits=Limits(),
            )
        )
        _ = asyncio
