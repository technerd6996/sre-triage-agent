"""One-time UI codemod for TRIAGE-1 static files.
  python -m scripts.ui_codemod            # dry run, prints what would change
  python -m scripts.ui_codemod --write    # apply (keeps .bak of every changed file)
Does two things:
  1. font sizes: every px size in css/html/js goes up by 1 (rem sizes follow html{font-size:17px} in theme.css)
  2. colours: hard-coded hex/rgba in css, <style> blocks, inline styles and JS strings become theme tokens,
     so the theme button changes everything. Tailwind class names like bg-[#12161C] are left alone
     (theme.css overrides them). theme.css itself is never touched.
Refuses to run twice (marker file static/.ui_codemod_done).
"""
import argparse, colorsys, re, shutil, sys
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "static"
MARK = STATIC / ".ui_codemod_done"
EXTS = {".css", ".html", ".js"}

def _hsl(hexv):
    h = hexv.lstrip("#")
    if len(h) == 3: h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hh, l, s = colorsys.rgb_to_hls(r, g, b)
    return hh * 360, s, l

def role(hexv, ctx):
    """ctx: 'bg', 'border', 'text'. Returns a CSS value (var(...)) or None to leave unchanged."""
    if hexv.upper() in ("#FFF", "#FFFFFF"):
        return "var(--text)" if ctx == "text" else None
    h, s, l = _hsl(hexv)
    if s > 0.25:
        hv = ("var(--accent)" if 20 <= h <= 56 else "var(--accent-2)" if 185 <= h <= 235
              else "var(--green)" if 135 <= h <= 175 else "var(--danger)" if (h < 20 or h > 335) else None)
        if hv is None: return None
        if l < 0.25:                   # dark tinted panels and their borders
            if ctx == "bg": return f"color-mix(in srgb, {hv} 10%, var(--panel))"
            if ctx == "border": return f"color-mix(in srgb, {hv} 40%, transparent)"
            return None
        if ctx == "bg" and l < 0.5 and hv in ("var(--green)", "var(--danger)"):
            return None                # solid green/red buttons keep their colour in both themes
        if hv == "var(--accent-2)": return "var(--blue)" if l > 0.6 else hv
        return hv
    if ctx == "text":
        if l > 0.82: return "var(--text)"
        if l > 0.60: return "var(--muted)"
        if l > 0.38: return "var(--subtle)"
        return "var(--on-accent)" if l < 0.2 else None
    if ctx == "border":
        return "var(--border)" if l < 0.185 else "var(--border-strong)"
    if l < 0.06: return "var(--bg)"
    if l < 0.10: return "var(--panel)"
    if l < 0.115: return "var(--panel-soft)"
    if l < 0.20: return "var(--panel-raised)"
    if l < 0.30: return "var(--border)"
    return "var(--border-strong)" if l < 0.5 else None

def ctx_of(text, pos):
    start = max(text.rfind(";", 0, pos), text.rfind("{", 0, pos), text.rfind('"', 0, pos), text.rfind("'", 0, pos), text.rfind("`", 0, pos))
    seg = text[start + 1:pos]
    m = re.match(r"\s*([a-zA-Z-]+)\s*:", seg)
    prop = m.group(1).lower() if m else ""
    if prop.startswith("border") or prop in ("outline", "outline-color", "stroke"): return "border"
    if prop in ("color", "fill", "caret-color", "-webkit-text-fill-color", "text-decoration-color") or prop.endswith("-color") and not prop.startswith(("background", "border")): return "text"
    return "bg"

HEX = re.compile(r"(?<![\w&\[#-])#([0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})\b(?![\w-])")
RGBA = re.compile(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([0-9.]+)\s*\)")

def rgba_role(r, g, b):
    if (r, g, b) == (255, 255, 255): return "var(--text)"
    if (r, g, b) == (0, 0, 0): return None
    hx = "#%02X%02X%02X" % (r, g, b)
    return role(hx, "bg")

def colours(text):
    def hexsub(m):
        v = role("#" + m.group(1), ctx_of(text, m.start()))
        return v or m.group(0)
    text = HEX.sub(hexsub, text)
    def rgbasub(m):
        r, g, b, a = int(m[1]), int(m[2]), int(m[3]), float(m[4])
        v = rgba_role(r, g, b)
        if not v: return m.group(0)
        pct = round(a * 100)
        return f"color-mix(in srgb, {v} {pct}%, transparent)"
    return RGBA.sub(rgbasub, text)

PX1 = re.compile(r"(font-size\s*:\s*)(\d+(?:\.\d+)?)px")
PX2 = re.compile(r"(text-\[)(\d+(?:\.\d+)?)(px\])")
PX3 = re.compile(r"(\bfont\s*:\s*(?:[\w.-]+\s+){0,3}?)(\d+(?:\.\d+)?)(px)(?=[\s/])")

def _n(x):
    v = float(x) + 1
    return str(int(v)) if v == int(v) else str(v)

def fonts(text):
    text = PX1.sub(lambda m: f"{m[1]}{_n(m[2])}px", text)
    text = PX2.sub(lambda m: f"{m[1]}{_n(m[2])}{m[3]}", text)
    return PX3.sub(lambda m: f"{m[1]}{_n(m[2])}{m[3]}", text)

def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("--write", action="store_true"); a = ap.parse_args(argv)
    if MARK.exists():
        print("Already applied (static/.ui_codemod_done exists). Delete that file only if you restored the originals."); return 1
    nfiles = 0
    for f in sorted(STATIC.rglob("*")):
        if f.suffix not in EXTS or f.name == "theme.css" or f.name.endswith(".bak") or "v1" in f.parts: continue
        raw = f.read_bytes(); text = raw.decode("utf-8")
        new = colours(fonts(text))
        if new == text: continue
        nfiles += 1
        print(("FIXED " if a.write else "would change ") + str(f.relative_to(STATIC)))
        if a.write:
            shutil.copy2(f, f.with_name(f.name + ".bak")); f.write_bytes(new.encode("utf-8"))
    if a.write: MARK.write_text("done\n")
    print(f"{nfiles} files" + ("" if a.write else " (dry run, add --write)")); return 0

if __name__ == "__main__":
    sys.exit(main())