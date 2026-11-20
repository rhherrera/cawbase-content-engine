"""Bounded reader of TrueType glyph bounding boxes (``glyf``/``loca``/``cmap``) over raw font-program bytes.

Pure (bytes in, numbers out) and dependency-free. A08 uses it ONLY to know where the INK of a glyph is, so that a
zone's typographic band is clipped against the ink of other lines (C-2B-01) and not against their ascent. Shared by
the planner (``pdf_extract``) and the verifier (``pdf_verify``): it is not a PDF parser; each side extracts the font
program with its own PDF parser. Anything unreadable or out of bounds returns ``None`` and the caller falls back to
the typographic box, which is larger (conservative).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

MAX_GLYPHS = 65536


@dataclass(frozen=True)
class TTInfo:
    upm: int
    boxes: tuple[tuple[int, int, int, int] | None, ...]  # per glyph id; None = empty glyph
    cmaps: dict[tuple[int, int], dict[int, int]]


def _tables(data: bytes) -> dict[bytes, tuple[int, int]]:
    if len(data) < 12:
        raise ValueError
    n = struct.unpack(">H", data[4:6])[0]
    if n > 64 or 12 + 16 * n > len(data):
        raise ValueError
    out: dict[bytes, tuple[int, int]] = {}
    for i in range(n):
        tag, _cs, off, ln = struct.unpack(">4sIII", data[12 + 16 * i : 28 + 16 * i])
        if off + ln > len(data):
            raise ValueError
        out[tag] = (off, ln)
    return out


def _cmap(data: bytes, off: int, ln: int) -> dict[tuple[int, int], dict[int, int]]:
    out: dict[tuple[int, int], dict[int, int]] = {}
    end = off + ln
    nsub = struct.unpack(">H", data[off + 2 : off + 4])[0]
    for i in range(min(nsub, 32)):
        pid, eid, sub = struct.unpack(">HHI", data[off + 4 + 8 * i : off + 12 + 8 * i])
        s = off + sub
        if s + 4 > end:
            continue
        fmt = struct.unpack(">H", data[s : s + 2])[0]
        m: dict[int, int] = {}
        if fmt == 0:
            m = {c: data[s + 6 + c] for c in range(256) if s + 6 + c < end}
        elif fmt == 4:
            segx2 = struct.unpack(">H", data[s + 6 : s + 8])[0]
            seg = segx2 // 2
            ends = s + 14
            starts = ends + segx2 + 2
            deltas = starts + segx2
            ranges = deltas + segx2
            if ranges + segx2 > end:
                continue
            for k in range(seg):
                e = struct.unpack(">H", data[ends + 2 * k : ends + 2 * k + 2])[0]
                st = struct.unpack(">H", data[starts + 2 * k : starts + 2 * k + 2])[0]
                dl = struct.unpack(">h", data[deltas + 2 * k : deltas + 2 * k + 2])[0]
                ro = struct.unpack(">H", data[ranges + 2 * k : ranges + 2 * k + 2])[0]
                if e < st or e - st > 4096:
                    continue
                for c in range(st, e + 1):
                    if ro == 0:
                        g = (c + dl) & 0xFFFF
                    else:
                        p = ranges + 2 * k + ro + 2 * (c - st)
                        if p + 2 > end:
                            continue
                        g = struct.unpack(">H", data[p : p + 2])[0]
                        if g:
                            g = (g + dl) & 0xFFFF
                    if g:
                        m[c] = g
        elif fmt == 6:
            first, count = struct.unpack(">HH", data[s + 6 : s + 10])
            for k in range(min(count, 4096)):
                p = s + 10 + 2 * k
                if p + 2 <= end:
                    m[first + k] = struct.unpack(">H", data[p : p + 2])[0]
        elif fmt == 12:
            ng = struct.unpack(">I", data[s + 12 : s + 16])[0]
            for k in range(min(ng, 4096)):
                p = s + 16 + 12 * k
                if p + 12 > end:
                    break
                a, b, g0 = struct.unpack(">III", data[p : p + 12])
                if b < a or b - a > 4096:
                    continue
                for c in range(a, b + 1):
                    m[c] = g0 + c - a
        else:
            continue
        out[(pid, eid)] = m
    return out


def read(data: bytes) -> TTInfo | None:
    """Parses the program; ``None`` if it is not a readable TrueType (``glyf``) font."""
    try:
        t = _tables(data)
        if b"head" not in t or b"loca" not in t or b"glyf" not in t or b"maxp" not in t:
            return None
        ho = t[b"head"][0]
        upm = struct.unpack(">H", data[ho + 18 : ho + 20])[0]
        long_loca = struct.unpack(">h", data[ho + 50 : ho + 52])[0] == 1
        n = struct.unpack(">H", data[t[b"maxp"][0] + 4 : t[b"maxp"][0] + 6])[0]
        if not (16 <= upm <= 16384) or n > MAX_GLYPHS:
            return None
        lo, ll = t[b"loca"]
        go, gl = t[b"glyf"]
        boxes: list[tuple[int, int, int, int] | None] = []
        for g in range(n):
            if long_loca:
                if lo + 4 * g + 8 > lo + ll:
                    return None
                a, b = struct.unpack(">II", data[lo + 4 * g : lo + 4 * g + 8])
            else:
                if lo + 2 * g + 4 > lo + ll:
                    return None
                a, b = (2 * x for x in struct.unpack(">HH", data[lo + 2 * g : lo + 2 * g + 4]))
            if b <= a:
                boxes.append(None)
                continue
            if b > gl or a + 10 > gl:
                return None
            nc, x0, y0, x1, y1 = struct.unpack(">hhhhh", data[go + a : go + a + 10])
            boxes.append(None if nc == 0 else (x0, y0, x1, y1))
        cm = _cmap(data, *t[b"cmap"]) if b"cmap" in t else {}
        return TTInfo(upm, tuple(boxes), cm)
    except (ValueError, struct.error, IndexError):
        return None


def gid_simple(info: TTInfo, code: int, text: str) -> int | None:
    """Glyph id of a code of a simple TrueType font: (3,0) symbolic, (1,0) Mac, then (3,1) by its Unicode text."""
    for key, cands in (((3, 0), (0xF000 + code, code)), ((1, 0), (code,))):
        m = info.cmaps.get(key)
        if m:
            for c in cands:
                if c in m:
                    return m[c]
    m = info.cmaps.get((3, 1)) or info.cmaps.get((0, 3)) or info.cmaps.get((0, 4))
    if m and len(text) == 1 and ord(text) in m:
        return m[ord(text)]
    return None


def gid_cid(cid: int, cid_to_gid: bytes | None) -> int:
    if cid_to_gid is None:
        return cid
    p = 2 * cid
    return int.from_bytes(cid_to_gid[p : p + 2], "big") if p + 2 <= len(cid_to_gid) else 0


def ink_box(info: TTInfo, gid: int | None) -> tuple[float, float, float, float] | None:
    """Ink bounding box of a glyph in 1/1000 em (xMin, yMin, xMax, yMax); ``None`` when unknown or empty."""
    if gid is None or not (0 < gid < len(info.boxes)):
        return None
    b = info.boxes[gid]
    if b is None:
        return None
    k = 1000.0 / info.upm
    return b[0] * k, b[1] * k, b[2] * k, b[3] * k


def ink_y(info: TTInfo, gid: int | None) -> tuple[float, float] | None:
    """Vertical ink extent of a glyph in 1/1000 em; ``None`` when unknown or empty."""
    if gid is None or not (0 < gid < len(info.boxes)):
        return None
    b = info.boxes[gid]
    if b is None:
        return None
    return b[1] * 1000.0 / info.upm, b[3] * 1000.0 / info.upm
