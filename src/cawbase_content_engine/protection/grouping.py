"""Pure interval logic: protection groups (docs 5.2-5.3) and the restoration classification (docs 7.3).

No I/O, no state, independent of the order in which findings arrive (everything is sorted first).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..errors import AnalysisSourceInvalidError
from .types import Action

Finding = tuple[str, int, int]  # (type_id, start, end) in code points, half-open


@dataclass(frozen=True)
class Group:
    start: int
    end: int
    action: Action
    findings: int


def union_intervals(intervals: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Strict-overlap union with transitive closure. Adjacent intervals ([0,5) and [5,9)) are NOT merged."""
    out: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if out and start < out[-1][1]:
            if end > out[-1][1]:
                out[-1] = (out[-1][0], end)
        else:
            out.append((start, end))
    return out


def _checked(findings: Sequence[Finding], text_len: int) -> list[Finding]:
    for _type_id, start, end in findings:
        if not (0 <= start < end <= text_len):
            raise AnalysisSourceInvalidError()  # offsets that do not fit the text: never silently clipped
    return sorted(findings, key=lambda f: (f[1], f[2], f[0]))


def plan_protection(
    findings: Sequence[Finding], text_len: int, default_action: Action, actions: Mapping[str, Action]
) -> list[Group]:
    """Groups of overlapping findings. If ANY member resolves to ``redact`` the whole group is redacted."""
    ordered = _checked(findings, text_len)
    groups: list[Group] = []
    cur_start = cur_end = 0
    cur_redact = False
    count = 0
    for type_id, start, end in ordered:
        redact = actions.get(type_id, default_action) == "redact"
        if count and start < cur_end:
            cur_end = max(cur_end, end)
            cur_redact = cur_redact or redact
            count += 1
            continue
        if count:
            groups.append(Group(cur_start, cur_end, "redact" if cur_redact else "pseudonymize", count))
        cur_start, cur_end, cur_redact, count = start, end, redact, 1
    if count:
        groups.append(Group(cur_start, cur_end, "redact" if cur_redact else "pseudonymize", count))
    return groups


@dataclass(frozen=True)
class RestorePlan:
    groups: tuple[tuple[int, int], ...]  # redacted groups over the received response
    consumed: frozenset[int]  # indexes (in order of appearance) of the marker occurrences a group consumed
    new_findings: tuple[Finding, ...]  # only the NEW findings, sorted
    ignored: int  # findings equal to, or entirely inside, one marker


def classify_restore(markers: Sequence[tuple[int, int]], findings: Sequence[Finding], text_len: int) -> RestorePlan:
    """Docs 7.3. A finding is ignored iff ONE marker contains it; every other finding is new. A new finding
    touches a marker when they overlap strictly. Groups = transitive union of the new findings and the markers
    they touch (ignored findings take no part; untouched markers never enter)."""
    ordered = _checked(findings, text_len)
    new: list[Finding] = []
    ignored = 0
    for f in ordered:
        if any(ms <= f[1] and f[2] <= me for ms, me in markers):
            ignored += 1
        else:
            new.append(f)
    touched = {i for i, (ms, me) in enumerate(markers) if any(f[1] < me and ms < f[2] for f in new)}
    intervals = [(f[1], f[2]) for f in new] + [markers[i] for i in touched]
    groups = union_intervals(intervals)
    consumed = frozenset(i for i in touched)
    return RestorePlan(tuple(groups), consumed, tuple(new), ignored)
