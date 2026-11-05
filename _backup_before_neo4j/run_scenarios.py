"""
Scenario tests for the sea network, with a report and a map snapshot of each.

    python run_scenarios.py                      every scenario in scenarios/
    python run_scenarios.py scenarios/02_*.json  just these
    python run_scenarios.py --out reports/demo   choose the report folder
    python run_scenarios.py --no-png             SVG snapshots only

Each scenario is one JSON file in scenarios/ (see scenarios/README.md). The
run asks the same route finder the web map uses, checks the answer against the
scenario's expectations and against rules every route must keep, and writes

    reports/<date>_<time>/report.html     open in a browser; one file, maps included
    reports/<date>_<time>/results.json    the same results, for other tools
    reports/<date>_<time>/snapshots/      one SVG (and PNG) map per scenario

The exit code is 0 when every check passes and 1 when any fails, so it can run
in CI. Python 3, stdlib only; PNGs need Edge or Chrome, and are skipped without.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from html import escape

import map_snapshot
import server
from sea_routing import DenialArea, densify, great_circle_nm

HERE = os.path.dirname(os.path.abspath(__file__))
SCENARIOS = os.path.join(HERE, "scenarios")
REPORTS = os.path.join(HERE, "reports")
GRAPH = server.GRAPH
END_TOLERANCE_NM = 1.0       # a route's line must start and end this close to its ports
LENGTH_TOLERANCE = 0.02      # the drawn line and the reported length may differ by 2%
AREA_SAMPLE_NM = 2.0         # routes are re-checked against the areas every 2 nm


class ScenarioError(Exception):
    pass


def resolve_port(ref):
    """A port id (WPI-16010) or an exact port name (San Diego)."""
    if ref in GRAPH.ports:
        return ref
    hits = [pid for pid, p in GRAPH.ports.items() if p["name"].lower() == str(ref).lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ScenarioError("No port is called %r. Use its exact WPI name or its port id." % ref)
    raise ScenarioError("%r matches %d ports; use one of these ids: %s" % (
        ref, len(hits), ", ".join("%s (%s)" % (h, GRAPH.ports[h]["country"]) for h in hits)))


def load_scenario(path):
    with open(path, encoding="utf-8") as f:
        s = json.load(f)
    for key in ("id", "name", "from", "to"):
        if key not in s:
            raise ScenarioError("%s has no %r." % (os.path.basename(path), key))
    s["file"] = os.path.relpath(path, HERE).replace(os.sep, "/")
    s.setdefault("purpose", "")
    s.setdefault("k", 3)
    s.setdefault("method", "distinct")
    s.setdefault("areas", [])
    s.setdefault("expect", {})
    return s


# ------------------------------------------------------------------ checks
def check(name, expected, actual, passed):
    yes_no = lambda v: ("yes" if v else "no") if isinstance(v, bool) else v
    return {"check": name, "expected": yes_no(expected), "actual": yes_no(actual), "passed": bool(passed)}


def line_nm(line):
    return sum(great_circle_nm(a, b) for a, b in zip(line, line[1:]))


def invariant_checks(result, origin, goal, areas):
    """Rules every answer must keep, whatever the scenario."""
    out, routes = [], result.get("routes", [])
    if not routes:
        return out
    gc = great_circle_nm(GRAPH.coords[origin], GRAPH.coords[goal])
    ends = max(max(great_circle_nm(r["line"][0], GRAPH.coords[origin]),
                   great_circle_nm(r["line"][-1], GRAPH.coords[goal])) for r in routes)
    out.append(check("Every route starts at the origin and ends at the destination",
                     "within %g nm" % END_TOLERANCE_NM, "%.2f nm off at worst" % ends, ends <= END_TOLERANCE_NM))
    shortest = min(r["length_nm"] for r in routes)
    out.append(check("No route is shorter than the great-circle distance",
                     ">= %s nm" % format(round(gc), ","), "%s nm shortest" % format(round(shortest), ","),
                     shortest >= gc - 0.5))
    lengths = [r["length_nm"] for r in routes]
    out.append(check("Routes are ranked shortest first", "lengths never decrease",
                     ", ".join(format(round(x), ",") for x in lengths),
                     all(a <= b + 0.05 for a, b in zip(lengths, lengths[1:]))))
    worst = max(abs(line_nm(r["line"]) - r["length_nm"]) / r["length_nm"] for r in routes)
    out.append(check("Each route's drawn line matches its reported length",
                     "within %g%%" % (LENGTH_TOLERANCE * 100), "%.2f%% at worst" % (worst * 100),
                     worst <= LENGTH_TOLERANCE))
    tracks = [tuple(map(tuple, r["line"])) for r in routes]
    out.append(check("No route is listed twice", "%d different routes" % len(tracks),
                     "%d different" % len(set(tracks)), len(set(tracks)) == len(tracks)))
    if areas:
        bad = []
        for r in routes:
            pts = densify([tuple(p) for p in r["line"]], AREA_SAMPLE_NM)
            if any(a.crosses(p, q) for a in areas for p, q in zip(pts, pts[1:])):
                bad.append("#%d" % r["rank"])
        out.append(check("No route enters an area of denial",
                         "none, checked every %g nm" % AREA_SAMPLE_NM,
                         ", ".join(bad) + " enter" if bad else "none enter", not bad))
    return out


def range_text(rng):
    lo, hi = rng.get("min"), rng.get("max")
    if lo is not None and hi is not None:
        return "%s to %s nm" % (format(lo, ","), format(hi, ","))
    return (">= %s nm" % format(lo, ",")) if lo is not None else ("<= %s nm" % format(hi, ","))


def in_range(value, rng):
    return value is not None and rng.get("min", value) <= value <= rng.get("max", value)


def expectation_checks(result, expect):
    """The scenario's own expectations."""
    out, routes = [], result.get("routes", [])
    best = routes[0] if routes else None
    known = {"route_found", "blocked_contains", "routes_at_least", "best_length_nm",
             "best_passes", "best_avoids", "affected", "extra_nm"}
    for key in sorted(set(expect) - known):
        out.append(check("Unknown expectation %r" % key, "one of: " + ", ".join(sorted(known)), key, False))
    if "route_found" in expect:
        want = expect["route_found"]
        out.append(check("A route is found" if want else "No route is possible", want,
                         "found" if best else "blocked: %s" % result.get("blocked", ""), bool(best) == want))
    if "blocked_contains" in expect:
        reason = result.get("blocked") or ""
        out.append(check("The blocked reason says why", "mentions %r" % expect["blocked_contains"],
                         reason or "not blocked", expect["blocked_contains"].lower() in reason.lower()))
    if "routes_at_least" in expect:
        out.append(check("Enough alternative routes", ">= %d" % expect["routes_at_least"],
                         len(routes), len(routes) >= expect["routes_at_least"]))
    if "best_length_nm" in expect:
        length = best["length_nm"] if best else None
        out.append(check("Best route length", range_text(expect["best_length_nm"]),
                         "%s nm" % format(round(length), ",") if best else "no route",
                         in_range(length, expect["best_length_nm"])))
    used = best["passages"] if best else []
    for name in expect.get("best_passes", []):
        out.append(check("Best route goes through %s" % name, "yes",
                         ", ".join(used) or "open water only", name in used))
    for name in expect.get("best_avoids", []):
        out.append(check("Best route avoids %s" % name, "yes",
                         ", ".join(used) or "open water only", bool(best) and name not in used))
    if "affected" in expect:
        out.append(check("The areas cut the original route" if expect["affected"]
                         else "The areas leave the original route alone",
                         expect["affected"], result.get("affected", False),
                         result.get("affected", False) == expect["affected"]))
    if "extra_nm" in expect:
        extra = result.get("extra_nm")
        out.append(check("Extra distance caused by the areas", range_text(expect["extra_nm"]),
                         "%s nm" % format(round(extra), ",") if extra is not None else "none",
                         in_range(extra, expect["extra_nm"])))
    return out


# ------------------------------------------------------------------- run
def run_one(s, snap_dir, browser):
    origin, goal = resolve_port(s["from"]), resolve_port(s["to"])
    body = {"from": origin, "to": goal, "k": s["k"], "method": s["method"], "areas": s["areas"]}
    started = time.perf_counter()
    try:
        result = server.find_routes(body)
    except server.BadRequest as e:
        raise ScenarioError("The route finder refused the scenario: %s" % e)
    seconds = time.perf_counter() - started
    areas = server.parse_areas(s["areas"])
    closed = GRAPH.links_in_areas(areas) if areas else frozenset()
    result.update({"from": origin, "to": goal, "from_name": GRAPH.ports[origin]["name"],
                   "to_name": GRAPH.ports[goal]["name"]})

    checks = expectation_checks(result, s["expect"]) + invariant_checks(result, origin, goal, areas)
    title = "%s  ·  %s → %s" % (s["id"], result["from_name"], result["to_name"])
    sub = "%s, K=%d%s" % ("Distinct corridors" if s["method"] == "distinct" else "Yen's K shortest", s["k"],
                          ", %d area%s of denial" % (len(areas), "" if len(areas) == 1 else "s") if areas else "")
    stem = os.path.splitext(os.path.basename(s["file"]))[0]
    snapshots = []
    for name, closeup, label in ((stem, False, "Whole route"),
                                 (stem + "_closeup", True, "Close-up of the areas" if areas else
                                  "Close-up of the destination")):
        svg = map_snapshot.render(GRAPH, result, s["areas"], closed, title,
                                  sub + ("  ·  close-up" if closeup else ""), closeup=closeup)
        svg_path = os.path.join(snap_dir, name + ".svg")
        with open(svg_path, "w", encoding="utf-8") as f:
            f.write(svg)
        png = bool(browser) and map_snapshot.svg_to_png(svg_path, os.path.join(snap_dir, name + ".png"), browser)
        snapshots.append({"label": label, "svg": "snapshots/" + name + ".svg",
                          "png": "snapshots/" + name + ".png" if png else None, "_svg": svg})

    return {"id": s["id"], "name": s["name"], "purpose": s["purpose"], "file": s["file"],
            "from": {"id": origin, "name": result["from_name"], "country": GRAPH.ports[origin]["country"]},
            "to": {"id": goal, "name": result["to_name"], "country": GRAPH.ports[goal]["country"]},
            "k": s["k"], "method": s["method"], "areas": s["areas"],
            "passed": all(c["passed"] for c in checks), "checks": checks,
            "original": {k: result["original"][k] for k in ("length_nm", "passages", "hops")},
            "routes": [{k: r[k] for k in ("rank", "length_nm", "passages", "hops")} for r in result["routes"]],
            "affected": result.get("affected"), "closed_links": result.get("closed_links", 0),
            "extra_nm": result.get("extra_nm"), "extra_days_at_18kt": result.get("extra_days_at_18kt"),
            "blocked": result.get("blocked"), "seconds": round(seconds, 3),
            "snapshots": snapshots}


def git_version():
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=HERE, capture_output=True,
                             text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=HERE,
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return rev + (" + local changes" if dirty else "") if rev else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def previous_results(out_dir):
    """The most recent earlier report's results, by scenario id, for comparison."""
    earlier = sorted(p for p in glob.glob(os.path.join(REPORTS, "*", "results.json"))
                     if os.path.dirname(os.path.abspath(p)) != os.path.abspath(out_dir))
    if not earlier:
        return None, {}
    with open(earlier[-1], encoding="utf-8") as f:
        data = json.load(f)
    return data.get("run", {}).get("started", os.path.basename(os.path.dirname(earlier[-1]))), \
        {s["id"]: s for s in data.get("scenarios", [])}


# ---------------------------------------------------------------- report
CSS = """
:root { --bg:#f6f7f9; --card:#fff; --ink:#1b2430; --dim:#5d6b7a; --line:#dde3ea; --pass:#18794e;
        --pass-bg:#e6f4ec; --fail:#b42318; --fail-bg:#fdecea; --chip:#eef1f5; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#11161c; --card:#172029; --ink:#e6edf3; --dim:#8b9bab; --line:#2b3742; --pass:#4ad4a0;
          --pass-bg:#173326; --fail:#f08a7c; --fail-bg:#35211f; --chip:#1d2833; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.5 "Segoe UI", system-ui, sans-serif; }
main { max-width: 1180px; margin: 0 auto; padding: 28px 16px 60px; }
h1 { font-size: 26px; margin: 0 0 4px; }
h2 { font-size: 20px; margin: 0; }
.meta { color: var(--dim); font-size: 13.5px; }
.tally { display: flex; gap: 12px; margin: 18px 0 22px; flex-wrap: wrap; }
.tally div { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 16px; }
.tally b { display: block; font-size: 24px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 18px; margin: 0 0 22px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--dim); font-weight: 600; font-size: 12.5px; text-transform: uppercase; letter-spacing: .03em; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.badge { display: inline-block; padding: 1px 9px; border-radius: 99px; font-weight: 700; font-size: 12.5px; }
.pass { color: var(--pass); background: var(--pass-bg); }
.fail { color: var(--fail); background: var(--fail-bg); }
.head { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; flex-wrap: wrap; }
.purpose { margin: 6px 0 14px; color: var(--dim); }
figure { margin: 0 0 14px; }
figcaption { font-size: 12.5px; color: var(--dim); text-transform: uppercase; letter-spacing: .03em; margin: 0 0 4px; }
.map { width: 100%; height: auto; display: block; border-radius: 8px; }
.chips span { display: inline-block; background: var(--chip); border-radius: 6px; padding: 1px 8px; margin: 0 6px 6px 0; font-size: 13px; }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
@media (max-width: 860px) { .cols { grid-template-columns: 1fr; } }
h3 { font-size: 14px; margin: 4px 0 6px; color: var(--dim); text-transform: uppercase; letter-spacing: .03em; }
.wrap { overflow-x: auto; }
a { color: inherit; }
"""


def _nm(x):
    return "—" if x is None else format(round(x), ",")


def _badge(ok):
    return '<span class="badge %s">%s</span>' % (("pass", "PASS") if ok else ("fail", "FAIL"))


def _area_text(a):
    if a["type"] == "circle":
        lon, lat = a["center"]
        return "Circle, %g nm radius, centred %.2f°%s %.2f°%s" % (
            a["radius_nm"], abs(lat), "N" if lat >= 0 else "S", abs(lon), "E" if lon >= 0 else "W")
    return "Polygon, %d corners" % len(a["points"])


def write_report(out_dir, run, results, prev_when, prev):
    passed = sum(r["passed"] for r in results)
    rows = []
    for r in results:
        best = r["routes"][0] if r["routes"] else None
        before = prev.get(r["id"])
        change = "new"
        if before:
            old = before["routes"][0]["length_nm"] if before.get("routes") else None
            new = best["length_nm"] if best else None
            change = ("same" if old == new else
                      "%+.1f nm" % (new - old) if old is not None and new is not None else "changed")
            if before.get("passed") != r["passed"]:
                change += ", was " + ("PASS" if before.get("passed") else "FAIL")
        rows.append("<tr><td><a href='#%s'>%s</a></td><td>%s</td><td>%s → %s</td><td class='num'>%d</td>"
                    "<td class='num'>%s</td><td>%s</td><td>%s</td></tr>" % (
                        escape(r["id"]), escape(r["id"]), escape(r["name"]), escape(r["from"]["name"]),
                        escape(r["to"]["name"]), len(r["areas"]), _nm(best["length_nm"]) if best else "blocked",
                        escape(change), _badge(r["passed"])))

    sections = []
    for r in results:
        route_rows = "".join(
            "<tr><td>#%d</td><td class='num'>%s</td><td class='num'>%.1f</td><td>%s</td></tr>" % (
                x["rank"], _nm(x["length_nm"]), x["length_nm"] / server.NOMINAL_KNOTS / 24,
                escape(", ".join(x["passages"]) or "open water"))
            for x in r["routes"]) or "<tr><td colspan='4'>No route: %s</td></tr>" % escape(r["blocked"] or "")
        if r["areas"]:
            o = r["original"]
            route_rows += ("<tr><td>Original</td><td class='num'>%s</td><td class='num'>%.1f</td>"
                           "<td>%s (no areas)</td></tr>" % (_nm(o["length_nm"]),
                                                            o["length_nm"] / server.NOMINAL_KNOTS / 24,
                                                            escape(", ".join(o["passages"]) or "open water")))
        check_rows = "".join(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                escape(c["check"]), escape(str(c["expected"])), escape(str(c["actual"])), _badge(c["passed"]))
            for c in r["checks"])
        chips = ["<span>%s</span>" % escape(t) for t in (
            "From %s (%s, %s)" % (r["from"]["name"], r["from"]["country"], r["from"]["id"]),
            "To %s (%s, %s)" % (r["to"]["name"], r["to"]["country"], r["to"]["id"]),
            "Distinct corridors" if r["method"] == "distinct" else "Yen's K shortest", "K = %d" % r["k"])]
        chips += ["<span>%s</span>" % escape(_area_text(a)) for a in r["areas"]]
        if r["areas"]:
            chips.append("<span>%d links closed</span>" % r["closed_links"])
            if r["extra_nm"] is not None:
                chips.append("<span>%+.0f nm, %+.1f days at %d kt</span>" % (
                    r["extra_nm"], r["extra_days_at_18kt"], server.NOMINAL_KNOTS))
        maps = "".join('<figure><figcaption>%s</figcaption>%s</figure>' % (
            escape(m["label"]), m["_svg"].replace('width="%d" height="%d"' % (map_snapshot.WIDTH, map_snapshot.HEIGHT),
                                                  'class="map" width="100%" height="auto"', 1))
            for m in r["snapshots"])
        files = " · ".join('<a href="%s">%s</a>' % (m[kind], m[kind].rsplit("/", 1)[1])
                           for m in r["snapshots"] for kind in ("svg", "png") if m[kind]) or "none"
        sections.append("""
<section class="card" id="%s">
  <div class="head"><h2>%s · %s</h2><div>%s</div></div>
  <p class="purpose">%s</p>
  <div class="chips">%s</div>
  %s
  <div class="cols">
    <div class="wrap"><h3>Routes</h3><table><tr><th>Rank</th><th class="num">nm</th><th class="num">Days at %d kt</th><th>Via</th></tr>%s</table></div>
    <div class="wrap"><h3>Checks</h3><table><tr><th>Check</th><th>Expected</th><th>Actual</th><th></th></tr>%s</table></div>
  </div>
  <p class="meta">Scenario file %s · snapshots %s · %.2f s</p>
</section>""" % (escape(r["id"]), escape(r["id"]), escape(r["name"]), _badge(r["passed"]), escape(r["purpose"]),
                 "".join(chips), maps, server.NOMINAL_KNOTS, route_rows, check_rows, escape(r["file"]), files, r["seconds"]))

    html = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sea Network Scenarios</title><style>%s</style></head>
<body><main>
<h1>FALCON sea network · scenario report</h1>
<div class="meta">Run on %s · code %s · %d scenarios · %s</div>
<div class="tally"><div><b>%d</b>scenarios</div><div><b class="%s" style="background:none">%d</b>passed</div>
<div><b class="%s" style="background:none">%d</b>failed</div><div><b>%d</b>checks</div></div>
<section class="card wrap"><table>
<tr><th>ID</th><th>Scenario</th><th>Move</th><th class="num">Areas</th><th class="num">Best nm</th><th>vs last run</th><th></th></tr>
%s</table></section>
%s
<p class="meta">Distances are along the Eurostat MARNET shipping-lane network (100 km), including the short legs
from each port onto its lane. Days use a nominal %d knots, for a rough sense only. The maps use the same
equirectangular projection as the web map, so polygon sides are straight.</p>
</main></body></html>""" % (
        CSS, escape(run["started"]), escape(run["code"]), len(results),
        "compared with the run of %s" % escape(prev_when) if prev_when else "no earlier run to compare with",
        len(results), "pass", passed, "fail" if passed < len(results) else "", len(results) - passed,
        sum(len(r["checks"]) for r in results), "\n".join(rows), "\n".join(sections), server.NOMINAL_KNOTS)
    with open(os.path.join(out_dir, "report.html"), "w", encoding="utf-8") as f:
        f.write(html)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run sea-network scenarios and write a report.")
    ap.add_argument("files", nargs="*", help="scenario files (default: every scenarios/*.json)")
    ap.add_argument("--out", help="report folder (default: reports/<date>_<time>)")
    ap.add_argument("--no-png", action="store_true", help="skip the PNG snapshots")
    args = ap.parse_args(argv)

    files = args.files or sorted(glob.glob(os.path.join(SCENARIOS, "*.json")))
    if not files:
        raise SystemExit("No scenario files found in %s." % SCENARIOS)
    started = datetime.now().astimezone()
    out_dir = args.out or os.path.join(REPORTS, started.strftime("%Y-%m-%d_%H%M%S"))
    snap_dir = os.path.join(out_dir, "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    browser = None if args.no_png else map_snapshot.find_browser()
    if not args.no_png and not browser:
        print("No Edge or Chrome found, so snapshots are SVG only (set FALCON_BROWSER to a browser to add PNGs).")

    results, seen = [], set()
    for path in files:
        try:
            s = load_scenario(path)
            if s["id"] in seen:
                raise ScenarioError("Scenario id %s is used twice." % s["id"])
            seen.add(s["id"])
            r = run_one(s, snap_dir, browser)
        except (ScenarioError, ValueError, server.BadRequest) as e:
            stem = os.path.splitext(os.path.basename(path))[0]
            print("%-6s ERROR  %s: %s" % ("", stem, e))
            r = {"id": stem, "name": stem, "purpose": "This scenario could not run.", "file": os.path.relpath(path, HERE),
                 "from": {"id": "", "name": "?", "country": ""}, "to": {"id": "", "name": "?", "country": ""},
                 "k": 0, "method": "", "areas": [], "passed": False,
                 "checks": [check("The scenario runs", "no errors", str(e), False)],
                 "original": {"length_nm": 0, "passages": [], "hops": 0}, "routes": [], "affected": None,
                 "closed_links": 0, "extra_nm": None, "extra_days_at_18kt": None, "blocked": str(e),
                 "seconds": 0, "snapshots": []}
            results.append(r)
            continue
        results.append(r)
        fails = [c["check"] for c in r["checks"] if not c["passed"]]
        best = r["routes"][0] if r["routes"] else None
        print("%-6s %s  %s -> %s  %s%s" % (r["id"], "PASS" if r["passed"] else "FAIL", r["from"]["name"],
                                         r["to"]["name"], "%s nm" % _nm(best["length_nm"]) if best else "blocked",
                                         "".join("\n         failed: " + f for f in fails)))

    run = {"started": started.strftime("%Y-%m-%d %H:%M:%S UTC%z"), "code": git_version(),
           "data": "neo4j-datamodel (MARNET 100 km)", "passed": sum(r["passed"] for r in results),
           "failed": sum(not r["passed"] for r in results)}
    prev_when, prev = previous_results(out_dir)
    write_report(out_dir, run, results, prev_when, prev)
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as f:
        json.dump({"run": run, "scenarios": [dict(r, snapshots=[{k: v for k, v in m.items() if k != "_svg"}
                                                                for m in r["snapshots"]]) for r in results]},
                  f, indent=2)
    print("\n%d of %d scenarios passed.  Report: %s" % (run["passed"], len(results),
                                                       os.path.join(out_dir, "report.html")))
    return 0 if run["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
