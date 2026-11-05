"""
A small web front end for the sea network: pick an origin and a destination
port, draw denial areas, see K alternative sea routes on a map.

    python server.py          then open http://localhost:8100

Python 3. The data is read from Neo4j at start-up (neo4j_source.py; needs
pip install neo4j and the NEO4J_* settings). All routing lives in sea_routing.py.

    GET  /api/ports           every port: id, name, country, lat, lon
    POST /api/routes          {"from": port_id, "to": port_id, "k": 1-5,
                               "method": "yen" | "distinct",
                               "areas": [{"type": "circle", "center": [lon, lat], "radius_nm": r},
                                         {"type": "polygon", "points": [[lon, lat], ...]}]}
"""
# import csv    # CSV: passages now come from Neo4j, with the rest of the data
import json
import math
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from neo4j_source import SourceError
from sea_routing import DenialArea, SeaGraph
from validate_data import DataError

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("SEA_NETWORK_PORT", "8100"))
MAX_K = 5
MAX_AREAS = 20
MAX_POLYGON_POINTS = 200
MAX_RADIUS_NM = 3000
MAX_BODY_BYTES = 1_000_000
NOMINAL_KNOTS = 18          # only to turn extra distance into rough days

print("loading sea network from Neo4j ...")
try:
    GRAPH = SeaGraph.load()
except SourceError as e:
    raise SystemExit("Not starting: %s" % e)
except DataError as e:
    # CSV: the old message pointed at rows in the CSV files:
    # raise SystemExit("Not starting: %s\nFix the rows above, or rebuild the data (see MIGRATION.md)." % e)
    raise SystemExit("Not starting: %s\nReload Neo4j from the CSV files (neo4j-datamodel/START_HERE.md), "
                     "or rebuild the data (see MIGRATION.md)." % e)
GRAPH.links_in_areas([])    # build the area index now, not on the first request
LOCK = threading.Lock()     # the graph's lazy caches aren't built for concurrent first use
# CSV: passage names used to be read from passages.csv:
# with open(os.path.join(HERE, "neo4j-datamodel", "nodes", "passages.csv"), encoding="utf-8") as f:
#     PASSAGE_NAMES = {r["passage_id"]: r["name"] for r in csv.DictReader(f)}
PASSAGE_NAMES = GRAPH.passages      # passage id -> name, from the Passage nodes in Neo4j
PORT_LIST = sorted(({"id": pid, "name": p["name"], "country": p["country"] or "",
                     "lat": float(p["lat"]), "lon": float(p["lon"]), "size": p["harbor_size"] or ""}
                    for pid, p in GRAPH.ports.items()), key=lambda p: (p["name"], p["country"]))


class BadRequest(Exception):
    pass


def _lonlat(v):
    if not (isinstance(v, (list, tuple)) and len(v) == 2
            and all(isinstance(x, (int, float)) and math.isfinite(x) for x in v)):
        raise BadRequest("A point must be [longitude, latitude].")
    lon, lat = float(v[0]), float(v[1])
    if not -90 <= lat <= 90:
        raise BadRequest("Latitude must be between -90 and 90.")
    return ((lon + 180) % 360 - 180, lat)


def parse_areas(raw):
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_AREAS:
        raise BadRequest(f"Up to {MAX_AREAS} areas.")
    areas = []
    for a in raw:
        kind = a.get("type") if isinstance(a, dict) else None
        if kind == "circle":
            r = a.get("radius_nm")
            if not isinstance(r, (int, float)) or not 0 < r <= MAX_RADIUS_NM:
                raise BadRequest(f"A circle's radius must be between 0 and {MAX_RADIUS_NM} nm.")
            areas.append(DenialArea({"type": "circle", "center": _lonlat(a.get("center")), "radius_nm": r}))
        elif kind == "polygon":
            pts = a.get("points")
            if not isinstance(pts, list) or not 3 <= len(pts) <= MAX_POLYGON_POINTS:
                raise BadRequest(f"A polygon needs 3 to {MAX_POLYGON_POINTS} points.")
            areas.append(DenialArea({"type": "polygon", "points": [_lonlat(p) for p in pts]}))
        else:
            raise BadRequest("An area must be a circle or a polygon.")
    return areas


def described(route, rank):
    d = GRAPH.describe(route)
    d["passages"] = [PASSAGE_NAMES.get(p, p) for p in d["passages"]]
    return dict(rank=rank, **d)


def find_routes(body):
    origin, goal = body.get("from"), body.get("to")
    if origin not in GRAPH.ports or goal not in GRAPH.ports:
        raise BadRequest("Unknown origin or destination port.")
    if origin == goal:
        raise BadRequest("Origin and destination are the same port.")
    k = body.get("k", 3)
    if not isinstance(k, int) or not 1 <= k <= MAX_K:
        raise BadRequest(f"K must be a whole number from 1 to {MAX_K}.")
    method = body.get("method", "yen")
    if method not in ("yen", "distinct"):
        raise BadRequest("Method must be 'yen' or 'distinct'.")
    areas = parse_areas(body.get("areas"))

    started = time.perf_counter()
    with LOCK:
        original = GRAPH.shortest(origin, goal)
        if original is None:
            raise BadRequest("No sea route connects these ports.")
        result = {"method": method, "k": k, "areas": len(areas), "original": described(original, 0)}
        for role, pid in (("Origin", origin), ("Destination", goal)):
            if GRAPH.port_in_areas(pid, areas):
                result.update(routes=[], blocked=f"{role} port {GRAPH.ports[pid]['name']} is inside a denial area.")
                return result
        closed = GRAPH.links_in_areas(areas)
        routes = (GRAPH.k_shortest(origin, goal, k, closed=closed) if method == "yen"
                  else GRAPH.distinct_routes(origin, goal, k, closed=closed))
    result["closed_links"] = len(closed)
    result["affected"] = bool(closed & set(original.edges))
    result["routes"] = [described(r, i) for i, r in enumerate(routes, 1)]
    if not routes:
        result["blocked"] = "No sea route avoids the denial areas."
    elif areas:
        extra = routes[0].length_nm - original.length_nm
        result["extra_nm"] = round(extra, 1)
        result["extra_days_at_18kt"] = round(extra / NOMINAL_KNOTS / 24, 1)
    result["ms"] = round((time.perf_counter() - started) * 1000)
    return result


JS = "application/javascript"
STATIC = {"/": ("web/index.html", "text/html; charset=utf-8"),
          "/index.html": ("web/index.html", "text/html; charset=utf-8"),
          "/land-50m.json": ("web/land-50m.json", "application/json"),
          "/vendor/d3-array.min.js": ("web/vendor/d3-array.min.js", JS),
          "/vendor/d3-geo.min.js": ("web/vendor/d3-geo.min.js", JS),
          "/vendor/topojson-client.min.js": ("web/vendor/topojson-client.min.js", JS)}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, obj):
        self.send(code, json.dumps(obj), "application/json")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/ports":
            return self.send_json(200, PORT_LIST)
        if path in STATIC:
            name, ctype = STATIC[path]
            with open(os.path.join(HERE, name), "rb") as f:
                return self.send(200, f.read(), ctype)
        self.send(404, "not found", "text/plain")

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/api/routes":
            return self.send(404, "not found", "text/plain")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY_BYTES:
                raise BadRequest("Request body missing or too large.")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise BadRequest("Request body must be a JSON object.")
            self.send_json(200, find_routes(body))
        except (BadRequest, ValueError) as e:
            self.send_json(400, {"error": str(e)})
        except Exception as e:
            self.send_json(500, {"error": "%s: %s" % (type(e).__name__, e)})


if __name__ == "__main__":
    print("FALCON sea network  ->  http://localhost:%d   (Ctrl-C to stop)" % PORT)

    class Server(ThreadingHTTPServer):
        allow_reuse_address = False      # on Windows it would let a second copy share the port
        daemon_threads = True

    try:
        Server(("127.0.0.1", PORT), Handler).serve_forever()
    except OSError as e:
        raise SystemExit("Port %d is already in use (%s). Stop the other copy, or set "
                         "SEA_NETWORK_PORT to another port." % (PORT, e))
