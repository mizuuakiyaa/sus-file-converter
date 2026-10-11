#!/usr/bin/env python3
"""
main.py - convert USC charts (.usc, .json, .json.gz) to SUS (.sus)

Run:
    python3 main.py

It asks for the file type, then the file location, converts the chart and
saves the .sus next to the original with the same name.

Assumptions: 4/4 time, 480 ticks per beat, 12 lanes (SUS lanes 2..d).
The SUS channel/type numbers follow the Sekai-style SUS dialect
(MikuMikuWorld / Ched-sekai). If your target tool disagrees about any of
them, tweak the constants in the "SUS dialect" block.
"""

import os
import gzip
import json
import sys
from math import gcd
from pathlib import Path

TPB = 480            # ticks per beat
TPM = TPB * 4        # ticks per measure (4/4)

# ----------------------------- SUS dialect --------------------------------
TAP, CRIT_TAP, FLICK, DAMAGE, TRACE, CRIT_TRACE = 1, 2, 3, 4, 5, 6   # channel 1x
LONG_START, LONG_END, LONG_STEP, LONG_HIDDEN = 1, 2, 3, 5            # channel 3x / 9x
DIRECTION = {"up": 1, "left": 3, "right": 4}                         # channel 5x
EASE = {"in": 2, "out": 5}                                           # channel 5x
CH_SLIDE = "3"
CH_GUIDE = "9"
# --------------------------------------------------------------------------

B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


class ConvertError(Exception):
    pass


# ------------------------------- helpers ----------------------------------
def b36(n, width):
    s = ""
    while n:
        n, r = divmod(n, 36)
        s = B36[r] + s
    if len(s) > width:
        raise ConvertError("too many unique ids")
    return s.rjust(width, "0")


def hexd(n):
    return format(n, "x")


def tick_of(beat):
    return int(round(float(beat) * TPB))


def fmt_num(x):
    return f"{float(x):.6f}".rstrip("0").rstrip(".") or "0"


def span(lane, size):
    """USC (center lane, half-width) -> SUS (hex left lane 2..d, width 1..c)."""
    left = int(round(lane - size + 6))
    width = int(round(size * 2))
    left = max(0, min(11, left))
    width = max(1, min(12 - left, width))
    return left + 2, width


def norm_dir(d):
    if d in (None, False, "", "none"):
        return None
    if d in (True, "up", "default", "flick"):
        return "up"
    if d in ("left", "right"):
        return d
    return "up"


def ease_curve(ease, u):
    if ease == "in":
        return u * u
    if ease == "out":
        return 1 - (1 - u) ** 2
    return u


# ------------------------------- loading ----------------------------------
def load_usc(path):
    raw = Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ConvertError(f"not valid JSON: {e}")
    if isinstance(data, dict):
        if isinstance(data.get("usc"), dict):
            return data["usc"]
        if "objects" in data:
            return data
        if "entities" in data:
            raise ConvertError(
                "this looks like Sonolus level data (entities), not a USC chart; "
                "only USC is supported"
            )
    raise ConvertError("unrecognised file: expected a USC chart")


# ------------------------------- SUS writer -------------------------------
class SusLines:
    """Collects (measure, channel) -> data lines, packing collisions into extra lines."""

    def __init__(self):
        self.lines = {}

    def add(self, tick, channel, token):
        measure, off = divmod(tick, TPM)
        layers = self.lines.setdefault((measure, channel), [])
        for layer in layers:
            if off not in layer:
                layer[off] = token
                return
        layers.append({off: token})

    def render(self):
        out = []
        for (m, ch), layers in sorted(self.lines.items()):
            for layer in layers:
                g = TPM
                for off in layer:
                    g = gcd(g, off)
                slots = TPM // g
                arr = ["00"] * slots
                for off, tok in layer.items():
                    arr[off // g] = tok
                out.append(f"#{m:03d}{ch}:{''.join(arr)}")
        return out


class IdPool:
    """Hands out single-char chain ids; an id is reused once its chain has ended."""

    def __init__(self):
        self.busy_until = [-1] * 36

    def take(self, start, end):
        for i, t in enumerate(self.busy_until):
            if t < start:
                self.busy_until[i] = end
                return B36[i]
        raise ConvertError("more than 36 slides/guides overlap in time")


def add_short(sus, tick, lane, size, critical=False, trace=False,
              direction=None, damage=False):
    left, width = span(lane, size)
    direction = norm_dir(direction)
    if damage:
        t = DAMAGE
    elif trace:
        t = CRIT_TRACE if critical else TRACE
    elif direction:
        t = CRIT_TAP if critical else FLICK
    else:
        t = CRIT_TAP if critical else TAP
    sus.add(tick, f"1{hexd(left)}", f"{t}{hexd(width)}")
    if direction:
        sus.add(tick, f"5{hexd(left)}", f"{DIRECTION[direction]}{hexd(width)}")


def fill_attach(conns):
    """Give 'attach' connections (which have no lane) an interpolated position."""
    anchors = [c for c in conns if c.get("type") != "attach"]
    for c in conns:
        if c.get("type") != "attach":
            continue
        t = tick_of(c["beat"])
        prev = [a for a in anchors if tick_of(a["beat"]) <= t]
        nxt = [a for a in anchors if tick_of(a["beat"]) >= t]
        if not prev or not nxt:
            raise ConvertError("attach note outside its slide's span")
        a, b = prev[-1], nxt[0]
        ta, tb = tick_of(a["beat"]), tick_of(b["beat"])
        u = 0.0 if tb == ta else (t - ta) / (tb - ta)
        f = ease_curve(a.get("ease", "linear"), u)
        c["lane"] = a["lane"] + (b["lane"] - a["lane"]) * f
        c["size"] = a["size"] + (b["size"] - a["size"]) * f


def emit_chain(sus, channel, ident, nodes, kinds, critical, with_shorts):
    """nodes: list of dicts (lane,size,beat,ease,...); kinds: parallel list of
    LONG_* types."""
    for n, kind in zip(nodes, kinds):
        t = tick_of(n["beat"])
        left, width = span(n["lane"], n["size"])
        sus.add(t, f"{channel}{hexd(left)}{ident}", f"{kind}{hexd(width)}")
        if kind != LONG_END and n.get("ease") in EASE:
            sus.add(t, f"5{hexd(left)}", f"{EASE[n['ease']]}{hexd(width)}")
        if not with_shorts:
            continue
        if kind == LONG_START:
            jt = n.get("judgeType", "normal")
            if jt == "trace":
                add_short(sus, t, n["lane"], n["size"], critical, trace=True)
            elif jt != "none" and critical:
                add_short(sus, t, n["lane"], n["size"], critical=True)
        elif kind == LONG_END:
            direction = norm_dir(n.get("direction") or n.get("flick"))
            if n.get("judgeType") == "trace":
                add_short(sus, t, n["lane"], n["size"], critical, trace=True,
                          direction=direction)
            elif direction:
                add_short(sus, t, n["lane"], n["size"], critical, direction=direction)


# ------------------------------ conversion --------------------------------
def convert(usc, meta, include_guides=False, flip_offset=False):
    warnings = []
    objs = usc.get("objects", [])
    sus = SusLines()

    # --- BPM ---
    bpm_map = {}
    for o in objs:
        if o.get("type") == "bpm":
            bpm_map[tick_of(o["beat"])] = float(o["bpm"])
    if 0 not in bpm_map:
        warnings.append("no BPM at beat 0; assuming 120")
        bpm_map[0] = 120.0
    bpm_ids, bpm_defs = {}, []
    for t in sorted(bpm_map):
        v = fmt_num(bpm_map[t])
        if v not in bpm_ids:
            bpm_ids[v] = b36(len(bpm_ids) + 1, 2)
            bpm_defs.append(f"#BPM{bpm_ids[v]}: {v}")
        sus.add(t, "08", bpm_ids[v])

    # --- time scale (hi-speed) groups ---
    groups, legacy = [], []
    for o in objs:
        if o.get("type") == "timeScaleGroup":
            groups.append([(tick_of(c["beat"]), float(c["timeScale"]))
                           for c in o.get("changes", [])])
        elif o.get("type") == "timeScale":
            legacy.append((tick_of(o["beat"]), float(o["timeScale"])))
    if legacy and not groups:
        groups = [legacy]
    til_defs = []
    for i, g in enumerate(groups):
        if not g:
            continue
        entries = ", ".join(f"{t // TPM}'{t % TPM}:{fmt_num(s)}" for t, s in sorted(g))
        til_defs.append(f'#TIL{i:02d}: "{entries}"')
    if len(groups) > 1:
        warnings.append("multiple time-scale groups: definitions written, but "
                        "per-note group assignment is not preserved")

    # --- tap / flick / trace / damage ---
    for o in objs:
        t = o.get("type")
        if t == "single":
            add_short(sus, tick_of(o["beat"]), o["lane"], o["size"],
                      critical=bool(o.get("critical")),
                      trace=bool(o.get("trace")),
                      direction=o.get("direction") or o.get("flick") or o.get("flickType"))
        elif t == "damage":
            add_short(sus, tick_of(o["beat"]), o["lane"], o["size"], damage=True)

    # --- slides ---
    slides = [o for o in objs if o.get("type") == "slide" and o.get("connections")]
    slides.sort(key=lambda s: min(float(c["beat"]) for c in s["connections"]))
    pool = IdPool()
    skipped = 0
    for s in slides:
        conns = sorted((dict(c) for c in s["connections"]), key=lambda c: float(c["beat"]))
        if len(conns) < 2 or conns[0].get("type") != "start" or conns[-1].get("type") != "end":
            skipped += 1
            continue
        fill_attach(conns)
        critical = bool(s.get("critical") or conns[0].get("critical"))
        kinds = []
        for c in conns:
            ty = c.get("type")
            if ty == "start":
                kinds.append(LONG_START)
            elif ty == "end":
                kinds.append(LONG_END)
            elif ty == "tick" and c.get("judgeType") in ("none", "hidden"):
                kinds.append(LONG_HIDDEN)
            else:
                kinds.append(LONG_STEP)
        ident = pool.take(tick_of(conns[0]["beat"]), tick_of(conns[-1]["beat"]))
        emit_chain(sus, CH_SLIDE, ident, conns, kinds, critical, with_shorts=True)
    if skipped:
        warnings.append(f"skipped {skipped} malformed slide(s)")

    # --- guides (optional) ---
    guides = [o for o in objs if o.get("type") == "guide"]
    if guides and not include_guides:
        warnings.append(f"{len(guides)} guide(s) skipped (use --guides to export them "
                        "on channel 9; colour/fade are not preserved)")
    elif guides:
        gpool = IdPool()
        for g in sorted(guides, key=lambda g: float(g["midpoints"][0]["beat"])):
            mids = sorted((dict(m) for m in g.get("midpoints", [])),
                          key=lambda m: float(m["beat"]))
            if len(mids) < 2:
                continue
            kinds = [LONG_START] + [LONG_STEP] * (len(mids) - 2) + [LONG_END]
            ident = gpool.take(tick_of(mids[0]["beat"]), tick_of(mids[-1]["beat"]))
            emit_chain(sus, CH_GUIDE, ident, mids, kinds, False, with_shorts=False)

    # --- assemble file ---
    offset = float(usc.get("offset", 0) or 0)
    if flip_offset:
        offset = -offset
    q = lambda s: str(s).replace('"', "'")
    head = [
        "This file was generated by main.py",
        "",
        f'#TITLE "{q(meta["title"])}"',
        f'#ARTIST "{q(meta["artist"])}"',
        f'#DESIGNER "{q(meta["designer"])}"',
        f"#WAVEOFFSET {fmt_num(offset)}",
        f'#REQUEST "ticks_per_beat {TPB}"',
        "",
    ]
    if til_defs:
        head += til_defs + ["#HISPEED 00", "#MEASUREHS 00", ""]
    head += bpm_defs + [""]
    return "\n".join(head + sus.render()) + "\n", warnings


# --------------------------------- CLI -------------------------------------
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"

FILE_TYPES = ("usc", "json", "json.gz")


def out_path_for(p):
    name = p.name
    for suf in (".json.gz", ".usc.gz", ".usc", ".json", ".gz"):
        if name.lower().endswith(suf):
            name = name[: -len(suf)]
            break
    return p.with_name(name + ".sus")


def ask_file_type():
    print("file type: (need to answer)\n")
    for t in FILE_TYPES:
        print(f"* {t}")
    print()
    while True:
        answer = input("> ").strip().lower().lstrip(".")
        if answer in FILE_TYPES:
            return answer
        print(f"Please type one of: {', '.join(FILE_TYPES)}")


def ask_location(file_type):
    while True:
        answer = input(f"{file_type} file location (mandatory): ").strip()
        # drag-and-drop in a terminal often wraps the path in quotes
        answer = answer.strip("'\"")
        if answer:
            return Path(os.path.expanduser(answer))
        print("A file location is required.")


def main():
    # enable coloured output on Windows (colorama is optional)
    try:
        import colorama
        colorama.just_fix_windows_console()
    except (ImportError, AttributeError):
        os.system("")

    file_type = ask_file_type()
    path = ask_location(file_type)

    try:
        usc = load_usc(path)
        title = out_path_for(path).stem
        text, warns = convert(
            usc, {"title": title, "artist": "", "designer": ""}
        )
        out = out_path_for(path)
        out.write_text(text, encoding="utf-8", newline="\n")
    except Exception:
        print(f"{RED}Something was wrong, please try again{RESET}")
        sys.exit(1)

    for w in warns:
        print(f"{YELLOW}warning: {w}{RESET}")
    print(f"Saved to: {out}")
    print(f"{GREEN}Done{RESET}")


if __name__ == "__main__":
    main()
