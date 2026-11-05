"""
Map snapshots for the scenario reports: one SVG picture of a scenario's routes,
drawn the way the web map draws them. Python 3, stdlib only.

    svg = render(graph, result, areas, closed, title)       # an SVG string
    svg_to_png(svg_path, png_path)                          # optional, needs Edge or Chrome

The projection is plain equirectangular (longitude and latitude straight
across), like the web map, so a polygon's straight sides look straight here
too. The view fits the routes and the denial areas, and may cross 180 degrees.
"""
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from html import escape

from sea_routing import NM_RADIUS, _unwrap

HERE = os.path.dirname(os.path.abspath(__file__))
LAND = os.path.join(HERE, "web", "land-50m.json")

WIDTH, HEIGHT = 1400, 788                # 16:9
OCEAN, LAND_FILL, COAST = "#0a1a26", "#27343f", "#445a6b"
GRID, LANE, INK, DIM, BG = "#16283a", "#2a4357", "#e6edf3", "#8b9bab", "#11161c"
DENY, ORIGINAL = "#e8604c", "#9aa8b6"
ORIGIN_DOT, DEST_DOT = "#4ad4a0", "#e8604c"
COLORS = ["#4ad4a0", "#e8b04c", "#c58bdd", "#4a9eda", "#f08a7c"]   # same as the web map

_land = None
_lanes = {}


# --------------------------------------------------------------- geometry
def _continuous(line, ref):
    """The line with each longitude moved the short way from the one before,
    starting within 180 degrees of ref, so it never jumps across the map."""
    out, prev = [], ref
    for lon, lat in line:
        prev = _unwrap(lon, prev)
        out.append((prev, lat))
    return out


def _xbounds(pts):
    xs = [p[0] for p in pts]
    return min(xs), max(xs)


def _offsets(x0, x1, view):
    """Whole turns to shift something spanning x0..x1 so it shows in the view."""
    return [o for o in (-360, 0, 360) if x0 + o <= view[2] and x1 + o >= view[0]]


def circle_points(center, radius_nm, step_deg=3):
    """A true circle on the globe: points radius_nm along the surface from center."""
    lon0, lat0 = map(math.radians, center)
    d = radius_nm / NM_RADIUS
    out = []
    for i in range(0, 360, step_deg):
        b = math.radians(i)
        lat = math.asin(math.sin(lat0) * math.cos(d) + math.cos(lat0) * math.sin(d) * math.cos(b))
        lon = lon0 + math.atan2(math.sin(b) * math.sin(d) * math.cos(lat0),
                                math.cos(d) - math.sin(lat0) * math.sin(lat))
        out.append((math.degrees(lon), math.degrees(lat)))
    return out


def _land_polygons():
    """Natural Earth land as polygons, each a list of rings in lon/lat."""
    global _land
    if _land is None:
        with open(LAND, encoding="utf-8") as f:
            topo = json.load(f)
        (sx, sy), (tx, ty) = topo["transform"]["scale"], topo["transform"]["translate"]
        arcs = []
        for arc in topo["arcs"]:
            x = y = 0
            pts = []
            for dx, dy in arc:
                x, y = x + dx, y + dy
                pts.append((x * sx + tx, y * sy + ty))
            arcs.append(pts)

        def ring(ids):
            out = []
            for i in ids:
                pts = arcs[i] if i >= 0 else arcs[~i][::-1]
                out.extend(pts if not out else pts[1:])
            return out

        _land = []
        for geom in topo["objects"]["land"]["geometries"]:
            polys = geom["arcs"] if geom["type"] == "MultiPolygon" else [geom["arcs"]]
            for poly in polys:
                rings = [_continuous(ring(r), ring(r)[0][0]) for r in poly]
                ys = [p[1] for p in rings[0]]
                _land.append((rings, _xbounds(rings[0]), (min(ys), max(ys))))
    return _land


def _lane(graph, eid):
    if eid not in _lanes:
        geom = graph.edges[eid][5]
        line = _continuous(geom, geom[0][0])
        ys = [p[1] for p in line]
        _lanes[eid] = (line, _xbounds(line), (min(ys), max(ys)))
    return _lanes[eid]


# ------------------------------------------------------------------ view
def fit_view(lines, extra_points=(), pad=0.12, min_span=12.0, aspect=WIDTH / HEIGHT):
    """(x0, y0, x1, y1) in continuous degrees around the lines and points,
    padded and widened to the picture's shape."""
    pts = [p for line in lines for p in line] + list(extra_points)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    w, h = max(x1 - x0, min_span), max(y1 - y0, min_span / aspect)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    w, h = w * (1 + 2 * pad), h * (1 + 2 * pad)
    if w / h > aspect:
        h = w / aspect
    else:
        w = h * aspect
    w, h = min(w, 360.0), min(h, 170.0)
    cy = max(-85 + h / 2, min(85 - h / 2, cy)) if h < 170 else 0
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


class _Canvas:
    def __init__(self, view):
        self.view = view
        self.k = WIDTH / (view[2] - view[0])
        self.parts = []

    def xy(self, p):
        return ((p[0] - self.view[0]) * self.k, (self.view[3] - p[1]) * self.k)

    def d(self, line, offset=0.0, close=False):
        pts = ["%.1f %.1f" % self.xy((x + offset, y)) for x, y in line]
        return "M" + "L".join(pts) + ("Z" if close else "")

    def add(self, s):
        self.parts.append(s)


def _lon_label(x):
    lon = (x + 180) % 360 - 180
    if abs(lon) < 1e-9 or abs(abs(lon) - 180) < 1e-9:
        return "%d°" % abs(round(lon))
    return "%d°%s" % (abs(round(lon)), "E" if lon > 0 else "W")


def _lat_label(y):
    return "%d°%s" % (abs(round(y)), "" if y == 0 else ("N" if y > 0 else "S"))


# ---------------------------------------------------------------- render
def render(graph, result, areas_spec, closed, title, subtitle="", closeup=False):
    """SVG of one scenario. result is the server's /api/routes answer,
    areas_spec the scenario's areas, closed the link ids the areas close.
    closeup zooms in on the areas of denial, or on the destination if
    there are none, instead of fitting the whole route."""
    routes = result.get("routes", [])
    origin, goal = graph.coords[result["from"]], graph.coords[result["to"]]
    ref = origin[0]
    route_lines = [_continuous(r["line"], ref) for r in routes]
    orig_line = _continuous(result["original"]["line"], ref) if result.get("original") else []
    drawn = route_lines or ([orig_line] if orig_line else [[origin, goal]])
    mid = sum(_xbounds(line)[0] + _xbounds(line)[1] for line in drawn) / (2 * len(drawn))

    area_rings = []
    for a in areas_spec:
        ring = circle_points(a["center"], a["radius_nm"]) if a["type"] == "circle" else a["points"]
        area_rings.append(_continuous(ring, mid))
    fit_lines = drawn + ([orig_line] if orig_line else []) + area_rings
    if closeup and area_rings:
        view = fit_view(area_rings, pad=0.9, min_span=16.0)
    elif closeup:
        g = (_unwrap(goal[0], mid), goal[1])
        view = fit_view([[(g[0] - 9, g[1] - 5), (g[0] + 9, g[1] + 5)]], pad=0.0)
    else:
        view = fit_view(fit_lines, [(_unwrap(origin[0], mid), origin[1]), (_unwrap(goal[0], mid), goal[1])])
    c = _Canvas(view)
    c.add('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
          'font-family="Segoe UI, system-ui, -apple-system, sans-serif">' % (WIDTH, HEIGHT, WIDTH, HEIGHT))
    c.add('<rect width="%d" height="%d" fill="%s"/>' % (WIDTH, HEIGHT, OCEAN))

    # graticule
    span = view[2] - view[0]
    step = next(s for s in (2, 5, 10, 15, 20, 30) if span / s <= 12)
    grid, labels = [], []
    x = math.ceil(view[0] / step) * step
    while x <= view[2]:
        X = (x - view[0]) * c.k
        grid.append("M%.1f 0V%d" % (X, HEIGHT))
        labels.append('<text x="%.1f" y="%d" text-anchor="middle">%s</text>' % (X, HEIGHT - 8, _lon_label(x)))
        x += step
    y = math.ceil(view[1] / step) * step
    while y <= view[3]:
        Y = (view[3] - y) * c.k
        grid.append("M0 %.1fH%d" % (Y, WIDTH))
        labels.append('<text x="%d" y="%.1f">%s</text>' % (WIDTH - 40, Y - 4, _lat_label(y)))
        y += step
    c.add('<path d="%s" stroke="%s" stroke-width="1" fill="none"/>' % ("".join(grid), GRID))

    # land
    paths = []
    for rings, (x0, x1), (y0, y1) in _land_polygons():
        if y0 > view[3] or y1 < view[1]:
            continue
        for o in _offsets(x0, x1, view):
            paths.append("".join(c.d(r, o, close=True) for r in rings))
    c.add('<path d="%s" fill="%s" fill-rule="evenodd" stroke="%s" stroke-width="0.7"/>'
          % ("".join(paths), LAND_FILL, COAST))

    # the shipping-lane network, faint, and the lanes the areas close, in red
    def lanes(eids):
        out = []
        for eid in eids:
            line, (x0, x1), (y0, y1) = _lane(graph, eid)
            if y0 > view[3] or y1 < view[1]:
                continue
            out.extend(c.d(line, o) for o in _offsets(x0, x1, view))
        return "".join(out)
    segments = [e for e, v in graph.edges.items() if v[4] == "segment"]
    c.add('<path d="%s" stroke="%s" stroke-width="0.8" stroke-opacity="0.7" fill="none"/>'
          % (lanes(segments), LANE))
    closed = [e for e in closed if graph.edges[e][4] != "port_access"]   # lanes, not port connectors
    if closed:
        c.add('<path d="%s" stroke="%s" stroke-width="2.2" stroke-opacity="0.9" fill="none"/>'
              % (lanes(closed), DENY))
    c.add('<g fill="%s" font-size="12">%s</g>' % (DIM, "".join(labels)))

    # denial areas
    for ring in area_rings:
        x0, x1 = _xbounds(ring)
        for o in _offsets(x0, x1, view):
            c.add('<path d="%s" fill="%s" fill-opacity="0.14" stroke="%s" stroke-width="1.6" '
                  'stroke-dasharray="6 4"/>' % (c.d(ring, o, close=True), DENY, DENY))

    # the original route (no areas), dashed, when the areas moved it
    show_original = bool(areas_spec) and orig_line and (result.get("affected") or not routes)
    if show_original:
        c.add('<path d="%s" fill="none" stroke="%s" stroke-width="2.4" stroke-dasharray="7 5" '
              'stroke-opacity="0.85"/>' % (c.d(orig_line), ORIGINAL))

    # routes, the best on top
    for r, line in reversed(list(zip(routes, route_lines))):
        c.add('<path d="%s" fill="none" stroke="%s" stroke-width="%s" stroke-linecap="round" '
              'stroke-linejoin="round" stroke-opacity="0.95"/>'
              % (c.d(line), COLORS[(r["rank"] - 1) % len(COLORS)], 4.5 if r["rank"] == 1 else 2.8))

    # origin and destination
    for p, colour, name in ((origin, ORIGIN_DOT, result["from_name"]), (goal, DEST_DOT, result["to_name"])):
        X, Y = c.xy((_unwrap(p[0], mid), p[1]))
        c.add('<circle cx="%.1f" cy="%.1f" r="7" fill="%s" stroke="%s" stroke-width="2.5"/>' % (X, Y, colour, BG))
        anchor, dx = ("end", -12) if X > WIDTH - 220 else ("start", 12)
        c.add('<text x="%.1f" y="%.1f" text-anchor="%s" font-size="16" font-weight="600" fill="%s" '
              'stroke="%s" stroke-width="4" paint-order="stroke">%s</text>'
              % (X + dx, Y + 5, anchor, INK, BG, escape(name)))

    # title and legend
    c.add('<rect x="14" y="14" width="%d" height="%d" rx="8" fill="%s" fill-opacity="0.88"/>'
          % (min(WIDTH - 28, 22 + 10.5 * max(len(title), len(subtitle) * 0.8)), 62 if subtitle else 40, BG))
    c.add('<text x="28" y="41" font-size="19" font-weight="700" fill="%s">%s</text>' % (INK, escape(title)))
    if subtitle:
        c.add('<text x="28" y="64" font-size="13.5" fill="%s">%s</text>' % (DIM, escape(subtitle)))

    rows = []
    for r in routes:
        via = ", ".join(r["passages"]) or "open water"
        rows.append(("line", COLORS[(r["rank"] - 1) % len(COLORS)], "#%d  %s nm  ·  %s" % (
            r["rank"], format(round(r["length_nm"]), ","), via)))
    if show_original:
        o = result["original"]
        rows.append(("dash", ORIGINAL, "Original, no areas  %s nm" % format(round(o["length_nm"]), ",")))
    if areas_spec:
        rows.append(("area", DENY, "Area of denial"))
        if closed:
            rows.append(("line", DENY, "Lanes closed by the areas"))
    if result.get("blocked"):
        rows.append(("text", DENY, "Blocked: " + result["blocked"]))
    if rows:
        lh, top = 24, HEIGHT - 34 - 24 * len(rows)
        wid = min(WIDTH - 28, 70 + 7.6 * max(len(t) for _, _, t in rows))
        c.add('<rect x="14" y="%d" width="%d" height="%d" rx="8" fill="%s" fill-opacity="0.88"/>'
              % (top - 14, wid, lh * len(rows) + 16, BG))
        for i, (kind, colour, text) in enumerate(rows):
            y = top + i * lh
            if kind == "line":
                c.add('<path d="M28 %dh32" stroke="%s" stroke-width="4" stroke-linecap="round"/>' % (y + 4, colour))
            elif kind == "dash":
                c.add('<path d="M28 %dh32" stroke="%s" stroke-width="2.4" stroke-dasharray="7 5"/>' % (y + 4, colour))
            elif kind == "area":
                c.add('<rect x="30" y="%d" width="28" height="14" fill="%s" fill-opacity="0.25" stroke="%s" '
                      'stroke-dasharray="4 3"/>' % (y - 3, colour, colour))
            c.add('<text x="%d" y="%d" font-size="14" fill="%s">%s</text>'
                  % (28 if kind == "text" else 72, y + 9, colour if kind == "text" else INK, escape(text)))
    c.add("</svg>")
    return "\n".join(c.parts)


# ------------------------------------------------------------------- PNG
def find_browser():
    """Edge or Chrome, for turning SVG into PNG. FALCON_BROWSER overrides."""
    names = [os.environ.get("FALCON_BROWSER", "")]
    for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""),
                 os.environ.get("LOCALAPPDATA", "")):
        if base:
            names += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                      os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    names += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    names += [shutil.which(n) or "" for n in ("msedge", "microsoft-edge", "google-chrome", "chromium",
                                               "chromium-browser", "chrome")]
    return next((n for n in names if n and os.path.isfile(n)), None)


def svg_to_png(svg_path, png_path, browser, wait_s=30):
    """Screenshot the SVG with a headless browser. True if the PNG was made.
    On Windows the browser's launcher can exit before the file is written,
    so this waits for the file to appear."""
    if os.path.exists(png_path):
        os.remove(png_path)
    profile = tempfile.mkdtemp(prefix="falcon-snapshot-")
    try:
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
                        "--force-device-scale-factor=1", "--user-data-dir=" + profile,
                        "--window-size=%d,%d" % (WIDTH, HEIGHT), "--screenshot=" + os.path.abspath(png_path),
                        "file:///" + os.path.abspath(svg_path).replace(os.sep, "/")],
                       capture_output=True, timeout=90)
        deadline = time.time() + wait_s
        while time.time() < deadline and not (os.path.isfile(png_path) and os.path.getsize(png_path) > 0):
            time.sleep(0.25)
        time.sleep(0.25)           # let the last bytes land
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    return os.path.isfile(png_path) and os.path.getsize(png_path) > 0
