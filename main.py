#!/usr/bin/env python3
"""
main.py - convert USC charts and Sonolus level data (.usc, .json, .json.gz) to SUS (.sus)

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
            return data["usc"], []
        if "objects" in data:
            return data, []
        if "entities" in data:
            return sonolus_to_usc(data)
    raise ConvertError("unrecognised file: expected a USC chart or Sonolus level data")


# --------------------------- Sonolus level data ----------------------------
# Reads Sonolus LevelData ({"bgmOffset":..,"entities":[...]}) as used by
# PJSekai-style engines (incl. Next SEKAI) and rebuilds a USC-like structure
# from it, so the normal USC -> SUS code below can be reused.
# Archetype names are matched by keywords (tap / flick / trace / slide start /
# slide tick / slide end / attached / connector / damage) so small naming
# differences between engines are tolerated.

def _classify(arch):
    a = arch.lower().replace("_", "").replace(" ", "")
    if a == "#bpmchange":
        return "bpm"
    if "timescale" in a and "change" in a:
        return "timescale"
    if "connector" in a:
        return "damage_connector" if "damage" in a else "connector"
    if "guide" in a:
        return "guide"
    if "damage" in a:
        return "damage"
    if "note" not in a and "tick" not in a:
        return None
    if "slide" in a or "tick" in a:
        if "attached" in a:
            return "slide_attach"
        if "start" in a:
            return "slide_start"
        if "end" in a:
            return "slide_end"
        return "slide_tick"
    if "flick" in a or "trace" in a or "tap" in a:
        return "single"
    return None


def _direction(value, style):
    """Sonolus flick direction value -> 'up' / 'left' / 'right'."""
    if isinstance(value, str):
        s = value.lower()
        return "left" if "left" in s else "right" if "right" in s else "up"
    if value is None:
        return "up"
    if style == "pjsekai":                       # -1 left, 0 up, 1 right
        return "left" if value < 0 else "right" if value > 0 else "up"
    # "next" style: 0 up, 1 up-left, 2 up-right, 3 down, 4 down-left, 5 down-right
    if value in (1, 4):
        return "left"
    if value in (2, 5):
        return "right"
    return "up"


def _ease(value):
    """Sonolus ease value -> 'in' / 'out' / None (linear or unsupported)."""
    if isinstance(value, str):
        s = value.lower()
        if s in ("in", "easein"):
            return "in"
        if s in ("out", "easeout"):
            return "out"
        return None
    if value == 1:
        return "in"
    if value == -1:
        return "out"
    return None


def sonolus_to_usc(level):
    raw = level.get("entities")
    if not isinstance(raw, list):
        raise ConvertError("level data has no entity list")

    by_name = {}
    ents = []
    for i, e in enumerate(raw):
        vals, refs = {}, {}
        for d in e.get("data") or []:
            n = d.get("name")
            if "ref" in d:
                refs[n] = d["ref"]
            elif "value" in d:
                vals[n] = d["value"]
        ents.append({
            "arch": str(e.get("archetype", "")),
            "vals": vals,
            "refs": refs,
            "kind": _classify(str(e.get("archetype", ""))),
        })
        if e.get("name") is not None:
            by_name[e["name"]] = i

    def target(ent, key):
        ref = ent["refs"].get(key)
        return by_name.get(ref) if ref is not None else None

    def targets(ent):
        return [(k, by_name[r]) for k, r in ent["refs"].items() if r in by_name]

    def beat_of(i):
        return float(ents[i]["vals"].get("#BEAT", 0) or 0)

    def is_slide_note(i):
        return ents[i]["kind"] in ("slide_start", "slide_tick", "slide_end")

    # flick-direction numbering style (negative numbers => pjsekai style)
    dirs = [e["vals"]["direction"] for e in ents
            if isinstance(e["vals"].get("direction"), (int, float))]
    style = "pjsekai" if any(d < 0 for d in dirs) else "next"

    objects, warnings, unknown = [], [], {}

    # --- BPM / time scale ---
    groups = {}
    for e in ents:
        v = e["vals"]
        if e["kind"] == "bpm":
            objects.append({"type": "bpm", "beat": v.get("#BEAT", 0), "bpm": v.get("#BPM", 120)})
        elif e["kind"] == "timescale":
            g = 0
            for k, x in list(v.items()) + list(e["refs"].items()):
                if "group" in k.lower():
                    g = str(x)
            groups.setdefault(g, []).append(
                {"beat": v.get("#BEAT", 0), "timeScale": v.get("#TIMESCALE", 1)})
        elif e["kind"] is None and "#BEAT" in v:
            unknown[e["arch"]] = unknown.get(e["arch"], 0) + 1
    for g in sorted(groups, key=str):
        objects.append({"type": "timeScaleGroup", "changes": groups[g]})

    # --- standalone notes ---
    n_single = n_damage = 0
    for e in ents:
        v = e["vals"]
        a = e["arch"].lower()
        if e["kind"] == "single":
            flick = "flick" in a
            objects.append({
                "type": "single", "beat": v.get("#BEAT", 0),
                "lane": v.get("lane", 0), "size": v.get("size", 1),
                "critical": "critical" in a, "trace": "trace" in a,
                "direction": _direction(v.get("direction"), style) if flick else "none",
            })
            n_single += 1
        elif e["kind"] == "damage":
            objects.append({"type": "damage", "beat": v.get("#BEAT", 0),
                            "lane": v.get("lane", 0), "size": v.get("size", 1)})
            n_damage += 1

    # --- slides: group slide notes into chains ---
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    ease_after = {}      # note index -> ease of the segment that starts at it
    conn_note = {}       # connector index -> one of its notes
    for i, e in enumerate(ents):
        if is_slide_note(i):
            find(i)
            for _, t in targets(e):
                if is_slide_note(t):
                    union(i, t)
        if e["kind"] != "connector":
            continue
        a, b = target(e, "start"), target(e, "end")
        if a is None or b is None:
            a, b = target(e, "head"), target(e, "tail")
        if a is None or b is None:
            cand = sorted((t for _, t in targets(e) if is_slide_note(t)), key=beat_of)
            if len(cand) < 2:
                continue
            a, b = cand[0], cand[-1]
        if not (is_slide_note(a) and is_slide_note(b)):
            continue
        union(a, b)
        conn_note[i] = a
        for k, x in e["vals"].items():
            if "ease" in k.lower():
                ease_after[a] = _ease(x)

    chains, attaches = {}, []
    for i, e in enumerate(ents):
        if is_slide_note(i):
            chains.setdefault(find(i), []).append(i)
        elif e["kind"] == "slide_attach":
            attaches.append(i)

    attached_to = {}
    lost_attach = 0
    for i in attaches:
        root = None
        for _, t in targets(ents[i]):
            if is_slide_note(t):
                root = find(t)
            elif t in conn_note:
                root = find(conn_note[t])
        if root is None or root not in chains:
            lost_attach += 1
        else:
            attached_to.setdefault(root, []).append(i)

    n_slides = bad = 0
    for root, notes in chains.items():
        notes = sorted(notes, key=beat_of)
        if len(notes) < 2:
            bad += 1
            continue
        first = ents[notes[0]]
        critical = "critical" in first["arch"].lower()
        conns = []
        for k, i in enumerate(notes):
            e = ents[i]
            v, a = e["vals"], e["arch"].lower()
            role = "start" if k == 0 else "end" if k == len(notes) - 1 else "tick"
            c = {"type": role, "beat": v.get("#BEAT", 0),
                 "lane": v.get("lane", 0), "size": v.get("size", 1)}
            if i in ease_after and ease_after[i]:
                c["ease"] = ease_after[i]
            if role == "start":
                c["critical"] = critical
                c["judgeType"] = "trace" if "trace" in a else "normal"
            elif role == "tick" and "hidden" in a:
                c["judgeType"] = "none"
            elif role == "end":
                if "flick" in a:
                    c["direction"] = _direction(v.get("direction"), style)
                c["judgeType"] = "trace" if "trace" in a else "normal"
            conns.append(c)
        for i in attached_to.get(root, []):
            conns.append({"type": "attach", "beat": ents[i]["vals"].get("#BEAT", 0)})
        objects.append({"type": "slide", "critical": critical, "connections": conns})
        n_slides += 1
    if bad:
        warnings.append(f"skipped {bad} slide chain(s) with fewer than 2 notes")
    if lost_attach:
        warnings.append(f"skipped {lost_attach} attached tick(s) that could not be linked to a slide")
    if unknown:
        names = ", ".join(f"{k} x{c}" for k, c in sorted(unknown.items()))
        warnings.append(f"unrecognised note-like archetypes were ignored: {names}")

    if n_single + n_damage + n_slides == 0:
        seen = {}
        for e in ents:
            seen.setdefault(e["arch"], set()).update(e["vals"])
            seen[e["arch"]].update(e["refs"])
        listing = "; ".join(f"{a} [{', '.join(sorted(map(str, f)))}]" for a, f in seen.items())
        raise ConvertError("no notes recognised in this level data. Archetypes found: " + listing)

    warnings.insert(0, f"read {n_single} single notes, {n_slides} slides, {n_damage} damage notes")
    usc = {"offset": level.get("bgmOffset", 0), "objects": objects}
    return usc, warnings


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
    print("file type: (required)\n")
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
        answer = input(f"{file_type} file location (required): ").strip()
        # drag-and-drop in a terminal often wraps the path in quotes
        answer = answer.strip("'\"")
        if answer:
            path = Path(os.path.expanduser(answer))
            if not path.exists() and "\\ " in answer:
                # macOS/Linux drag-and-drop escapes spaces as "\ "
                path = Path(os.path.expanduser(answer.replace("\\ ", " ")))
            return path
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
        if not path.is_file():
            raise ConvertError(f"file not found: {path}")
        usc, load_warns = load_usc(path)
        title = out_path_for(path).stem
        text, warns = convert(
            usc, {"title": title, "artist": "", "designer": ""}
        )
        out = out_path_for(path)
        out.write_text(text, encoding="utf-8", newline="\n")
    except Exception as e:
        print(f"{RED}Something was wrong, please try again{RESET}")
        if isinstance(e, KeyError):
            reason = f"the chart is missing the field {e}"
        elif isinstance(e, ConvertError):
            reason = str(e)
        else:
            reason = f"{type(e).__name__}: {e}"
        print(f"Reason: {reason}")
        sys.exit(1)

    for w in load_warns + warns:
        print(f"{YELLOW}note: {w}{RESET}")
    print(f"Saved to: {out}")
    print(f"{GREEN}Done{RESET}")


if __name__ == "__main__":
    main()
