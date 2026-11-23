"""
Acceptance test: does a FALCON server give the reference answers?

    python acceptance/check_server.py http://localhost:8100
    python acceptance/check_server.py https://falcon.corp.example --report result.json

Sends every request in acceptance/cases.json to the server's API over HTTP
and compares each answer with acceptance/golden.json, the answers of this
reference version. It works against any implementation of the API in
docs/API.md: this Python server, or a rebuilt one on other infrastructure.
Prints PASS or FAIL per case with what differs; the exit code is 0 only when
every case passes. Python 3, stdlib only; it needs nothing but the URL.

What counts as the same answer (see acceptance/README.md for why):

  * the same HTTP status; a refused request carries an "error" message
  * the same number of routes, through the same named passages, in order
  * every length within 1 nm or 0.1%, whichever is larger
  * the same blocked reason, ignoring case
  * the same "affected" answer, and a closed-link count within 2 links or 2%
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CASES = os.path.join(HERE, "cases.json")
GOLDEN = os.path.join(HERE, "golden.json")
PORT_COUNT = 3806
SPOT_PORTS = {"WPI-60390": (129.033333, 35.1)}   # Busan, a fixed point to check coordinates


def length_ok(want, got):
    return got is not None and abs(got - want) <= max(1.0, 0.001 * want)


def summarize(status, body):
    """The parts of a response the acceptance test compares."""
    if status != 200:
        return {"status": status, "has_error": isinstance(body, dict) and bool(body.get("error"))}
    return {"status": 200,
            "blocked": body.get("blocked"),
            "original_nm": (body.get("original") or {}).get("length_nm"),
            "closed_links": body.get("closed_links"),
            "affected": body.get("affected"),
            "extra_nm": body.get("extra_nm"),
            "routes": [{"length_nm": r.get("length_nm"), "passages": r.get("passages")}
                       for r in body.get("routes") or []]}


def compare(want, got):
    """What differs between two summaries: [] when the answer is acceptable."""
    if got["status"] != want["status"]:
        return ["HTTP status %s, expected %s" % (got["status"], want["status"])]
    if want["status"] != 200:
        return [] if got["has_error"] else ['the refusal has no "error" message']
    out = []
    wb, gb = want["blocked"], got["blocked"]
    if bool(wb) != bool(gb):
        out.append("blocked: %r, expected %r" % (gb, wb))
    elif wb and wb.lower() != gb.lower():
        out.append("blocked reason %r, expected %r" % (gb, wb))
    if want["original_nm"] is not None and not length_ok(want["original_nm"], got["original_nm"]):
        out.append("original route %s nm, expected %s" % (got["original_nm"], want["original_nm"]))
    if want["affected"] is not None and got["affected"] != want["affected"]:
        out.append("affected %r, expected %r" % (got["affected"], want["affected"]))
    wc, gc = want["closed_links"], got["closed_links"]
    if wc is not None and (gc is None or abs(gc - wc) > max(2, 0.02 * wc)):
        out.append("closed_links %s, expected %s (within 2 links or 2%%)" % (gc, wc))
    if want["extra_nm"] is not None and not length_ok(abs(want["extra_nm"]), abs(got["extra_nm"] or 0)):
        out.append("extra_nm %s, expected %s" % (got["extra_nm"], want["extra_nm"]))
    if len(got["routes"]) != len(want["routes"]):
        out.append("%d routes, expected %d" % (len(got["routes"]), len(want["routes"])))
    for i, (w, g) in enumerate(zip(want["routes"], got["routes"]), 1):
        if not length_ok(w["length_nm"], g["length_nm"]):
            out.append("route #%d is %s nm, expected %s" % (i, g["length_nm"], w["length_nm"]))
        if g["passages"] != w["passages"]:
            out.append("route #%d passes %s, expected %s" % (i, g["passages"] or "open water",
                                                              w["passages"] or "open water"))
    return out


def http(url, body=None, timeout=60):
    """(status, parsed JSON or None) for a GET, or a POST when body is given."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def check_ports(base):
    """GET /api/ports: every port, once, with a position."""
    status, ports = http(base + "/api/ports")
    if status != 200 or not isinstance(ports, list):
        return ["GET /api/ports gave HTTP %s" % status]
    out = []
    ids = [p.get("id") for p in ports]
    if len(ports) != PORT_COUNT:
        out.append("%d ports, expected %d" % (len(ports), PORT_COUNT))
    if len(set(ids)) != len(ids):
        out.append("some port ids appear more than once")
    missing = [k for k in ("id", "name", "country", "lat", "lon") if ports and k not in ports[0]]
    if missing:
        out.append("ports have no %s" % ", ".join(missing))
    by_id = {p.get("id"): p for p in ports}
    for pid, (lon, lat) in SPOT_PORTS.items():
        p = by_id.get(pid)
        if not p or abs(p.get("lon", 999) - lon) > 1e-3 or abs(p.get("lat", 999) - lat) > 1e-3:
            out.append("%s is missing or not at %s, %s" % (pid, lon, lat))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check a FALCON server against the reference answers.")
    ap.add_argument("url", help="the server's address, such as http://localhost:8100")
    ap.add_argument("--report", help="also write the results to this JSON file")
    args = ap.parse_args(argv)
    base = args.url.rstrip("/")
    with open(CASES, encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    with open(GOLDEN, encoding="utf-8") as f:
        golden = json.load(f)["answers"]

    results = []
    try:
        problems = check_ports(base)
    except OSError as e:
        print("Cannot reach %s: %s" % (base, e))
        return 2
    results.append({"id": "PORTS", "what": "GET /api/ports lists every port", "problems": problems, "seconds": 0})
    for case in cases:
        started = time.perf_counter()
        try:
            status, body = http(base + "/api/routes", case["request"])
            problems = compare(golden[case["id"]], summarize(status, body))
        except OSError as e:
            problems = ["no answer: %s" % e]
        results.append({"id": case["id"], "what": case["what"], "problems": problems,
                        "seconds": round(time.perf_counter() - started, 2)})

    for r in results:
        print("%-6s %s  %s" % (r["id"], "PASS" if not r["problems"] else "FAIL", r["what"][:90]))
        for p in r["problems"]:
            print("         - " + p)
    failed = sum(1 for r in results if r["problems"])
    slow = [r["id"] for r in results if r["seconds"] > 5]
    print("\n%d of %d checks passed against %s.%s" % (len(results) - failed, len(results), base,
                                                       "  Slow (over 5 s): " + ", ".join(slow) if slow else ""))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump({"url": base, "checked": time.strftime("%Y-%m-%d %H:%M:%S %z"),
                       "passed": len(results) - failed, "failed": failed, "results": results}, f, indent=2)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
