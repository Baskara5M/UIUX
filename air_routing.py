"""
The air side of FALCON: the airfields, and routes between them along airways.

    import air_routing
    airfields = air_routing.load_airfields()            # fetch from Neo4j, check, or raise
    graph = air_routing.AirGraph.load(airfields=airfields)
    graph.route("PHNL", "PHTO")                         # shortest route along the airways

Everything comes from Neo4j; no CSV file is read here.

  * The airfields are the Airfield nodes. server.py serves them as
    GET /api/airports, and web/air_routing.html draws them.
  * The airway network is the Waypoint nodes joined by AIRWAY relationships
    (AIRWAY_QUERY below). Every airway is usable in both directions, and its
    length is the great-circle distance between its two waypoints.
  * Neo4j holds no link between an airfield and the airways, so each airfield
    joins the network at its nearest waypoint, if one lies within ACCESS_NM.
    An airfield with no waypoint that close can't be routed to or from.

The connection is the same one the sea side uses (NEO4J_URI, NEO4J_USER,
NEO4J_PASSWORD, NEO4J_DATABASE; see neo4j_source.py). Everything is read once,
at start-up. Reload Neo4j -> restart the server.

    python air_routing.py        checks what Neo4j holds and prints a summary
"""
import heapq
import json
import math
import os
import re
import sys

import neo4j_source
import validate_data
from neo4j_source import SourceError
from validate_data import DataError, _Problems, _number, _point

BUILD_REPORT = os.path.join(validate_data.DATA, "build-report.json")

# One row per Airfield node. The names on the right are the fields /api/airports returns.
AIRFIELD_QUERY = ("MATCH (a:Airfield) "
                  "RETURN a.airfield_id AS id, a.icao_code AS icao, a.iata_code AS iata, a.name AS name, "
                  "a.country_code AS country, a.airport_type AS type, a.lat AS lat, a.lon AS lon, "
                  "a.longest_paved_runway_ft AS runway_ft")


# One row per AIRWAY relationship, matched one way (->) so each comes back once.
AIRWAY_QUERY = ("MATCH (a:Waypoint)-[r:AIRWAY]->(b:Waypoint) "
                "RETURN a.waypoint_id AS from_id, a.name AS from_name, a.lat AS from_lat, a.lon AS from_lon, "
                "b.waypoint_id AS to_id, b.name AS to_name, b.lat AS to_lat, b.lon AS to_lon, "
                "r.segment_id AS segment_id, r.airway AS airway, r.airway_level AS airway_level, "
                "r.lower_fl AS lower_fl, r.upper_fl AS upper_fl, r.distance_nm AS distance_nm")

NM_RADIUS = 6371.0088 / 1.852    # Earth's mean radius in nm, the same value the sea side uses
ACCESS_NM = 25.0                 # an airfield joins the airways at its nearest waypoint within this distance


def _query(query, what, run=None):
    """The rows Neo4j returns for one query. `run` (a function: Cypher query ->
    list of row dicts) is only for tests; normally the connection comes from
    the NEO4J_* settings."""
    close = None
    if run is None:
        run, close = neo4j_source._driver_runner(neo4j_source.settings())
    try:
        return run(query)
    except SourceError:
        raise
    except Exception as e:
        raise SourceError("The Neo4j %s query failed: %s: %s" % (what, type(e).__name__, e))
    finally:
        if close:
            close()


def fetch_airfields(run=None):
    """The rows for AIRFIELD_QUERY, sorted by airfield id (Neo4j promises no order)."""
    return sorted(_query(AIRFIELD_QUERY, "airfield", run), key=lambda r: str(r.get("id") or ""))


def validate_airfields(rows, build_report=BUILD_REPORT):
    """Every problem in the airfield rows, as a list of messages ([] if none)."""
    problems = _Problems()
    seen = set()
    if not rows:
        problems.add("empty", "Neo4j has no Airfield nodes. Load them with neo4j-datamodel/load.cypher")
    for r in rows:
        aid = r.get("id")
        where = "Airfield %s" % (aid if aid else "(no airfield_id)")
        if not aid:
            problems.add("blank id", "%s: airfield_id is blank" % where)
        elif aid in seen:
            problems.add("duplicate id", "%s: airfield_id is used more than once (loaded twice?)" % where)
        seen.add(aid)
        if _point(r.get("lon"), r.get("lat")) is None:
            problems.add("bad coordinates", "%s: lon %r / lat %r is not a valid position" % (
                where, r.get("lon"), r.get("lat")))
        if not r.get("name"):
            problems.add("blank name", "%s: name is blank" % where)
        if r.get("runway_ft") is not None and _number(r.get("runway_ft")) is None:
            problems.add("bad runway", "%s: longest_paved_runway_ft %r is not a number" % (where, r.get("runway_ft")))
    # The build report says how many airfields the build wrote: catches a partial or doubled load.
    if rows and os.path.isfile(build_report):
        with open(build_report, encoding="utf-8") as f:
            want = json.load(f).get("rows_written", {}).get("nodes/airfields.csv")
        if want is not None and want != len(rows):
            problems.add("count", "Neo4j has %d Airfield nodes, but the build wrote %d rows to nodes/airfields.csv" % (
                len(rows), want))
    return problems.lines()


def _tidy(r):
    """One airfield as /api/airports returns it. Neo4j gives null for an empty
    value; the page expects "" for text. airport_type loses its "_airport"."""
    runway = _number(r.get("runway_ft"))
    return {"id": r["id"], "icao": r.get("icao") or "", "iata": r.get("iata") or "", "name": r["name"],
            "country": r.get("country") or "", "type": (r.get("type") or "").replace("_airport", ""),
            "lat": float(r["lat"]), "lon": float(r["lon"]),
            "runway_ft": None if runway is None else int(runway)}


def load_airfields(run=None):
    """Every airfield in Neo4j, checked, sorted by name and then id.

    Raises neo4j_source.SourceError if Neo4j can't be read, and
    validate_data.DataError, listing every problem, if the airfields fail
    their checks."""
    rows = fetch_airfields(run)
    problems = validate_airfields(rows)
    if problems:
        raise DataError("The airfields in Neo4j failed %d check%s:\n  %s" % (
            len(problems), "" if len(problems) == 1 else "s", "\n  ".join(problems)))
    return sorted((_tidy(r) for r in rows), key=lambda a: (a["name"], a["id"]))


# ------------------------------------------------------------------ airways
def great_circle_nm(a, b):
    """Great-circle distance in nm between two (lat, lon) points."""
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * NM_RADIUS * math.asin(min(1.0, math.sqrt(h)))


def _natural(v):
    """SEG_2 before SEG_10: text and number parts compared in turn."""
    return [(0, int(x), "") if x.isdigit() else (1, 0, x) for x in re.findall(r"\d+|\D+", str(v or ""))]


def _waypoint_key(row, end):
    """The waypoint's id; if Neo4j has none for it, its name and position."""
    wid = row.get(end + "_id")
    if wid not in (None, ""):
        return str(wid)
    return "%s@%s,%s" % (row.get(end + "_name"), row.get(end + "_lat"), row.get(end + "_lon"))


def fetch_airways(run=None):
    """The rows for AIRWAY_QUERY, in a fixed order (by segment id, then by the
    two waypoints), so that equally long routes always come out the same."""
    return sorted(_query(AIRWAY_QUERY, "airway", run),
                  key=lambda r: (_natural(r.get("segment_id")), _waypoint_key(r, "from"), _waypoint_key(r, "to")))


def validate_airways(rows):
    """Every problem in the airway rows, as a list of messages ([] if none)."""
    problems = _Problems()
    if not rows:
        problems.add("empty", "Neo4j has no AIRWAY relationships between Waypoint nodes. Load the airways first")
    where_seen = {}
    for r in rows:
        seg = "Airway segment %s" % (r.get("segment_id") or "(no segment_id)")
        for end in ("from", "to"):
            name = r.get(end + "_name") or r.get(end + "_id")
            p = _point(r.get(end + "_lon"), r.get(end + "_lat"))
            if not name:
                problems.add("blank waypoint", "%s: its %s waypoint has no name and no waypoint_id" % (seg, end))
            if p is None:
                problems.add("bad coordinates", "%s: %s waypoint %s has lon %r / lat %r, not a valid position" % (
                    seg, end, name, r.get(end + "_lon"), r.get(end + "_lat")))
                continue
            key = _waypoint_key(r, end)
            first = where_seen.setdefault(key, p)
            if abs(first[0] - p[0]) > 1e-6 or abs(first[1] - p[1]) > 1e-6:
                problems.add("moved waypoint", "Waypoint %s has two different positions: %r and %r" % (key, first, p))
    return problems.lines()


class RouteError(ValueError):
    """The request can't be routed. str() says why, in words for the map page."""


class AirGraph:
    """The airway network in memory, with each airfield joined to its nearest waypoint."""

    def __init__(self, airfields, airway_rows):
        self.airfields = {a["id"]: a for a in airfields}
        self.waypoints = {}      # waypoint key -> {"id", "name", "lat", "lon"}
        self.segments = []       # {"a", "b", "nm", "airway", "segment_id", "lower_fl", "upper_fl", "airway_level"}
        self.adj = {}            # waypoint key -> [(neighbour key, index into self.segments)]
        for r in airway_rows:
            a, b = _waypoint_key(r, "from"), _waypoint_key(r, "to")
            for key, end in ((a, "from"), (b, "to")):
                if key not in self.waypoints:
                    self.waypoints[key] = {"id": key, "name": r.get(end + "_name") or key,
                                           "lat": float(r[end + "_lat"]), "lon": float(r[end + "_lon"])}
                    self.adj[key] = []
            if a == b:
                continue         # a link from a waypoint to itself goes nowhere
            i = len(self.segments)
            self.segments.append({"a": a, "b": b, "nm": great_circle_nm(self._at(a), self._at(b)),
                                  "airway": r.get("airway") or "", "segment_id": r.get("segment_id") or "",
                                  "lower_fl": _number(r.get("lower_fl")), "upper_fl": _number(r.get("upper_fl")),
                                  "airway_level": r.get("airway_level")})
            self.adj[a].append((b, i))      # every airway is usable both ways
            self.adj[b].append((a, i))
        # Each airfield's way onto the network: (waypoint key, distance in nm).
        self.access = {}
        for aid, a in self.airfields.items():
            best = min(((great_circle_nm((a["lat"], a["lon"]), self._at(k)), k) for k in self.waypoints), default=None)
            if best is not None and best[0] <= ACCESS_NM:
                self.access[aid] = (best[1], best[0])

    def _at(self, key):
        w = self.waypoints[key]
        return (w["lat"], w["lon"])

    @classmethod
    def load(cls, run=None, airfields=None):
        """Reads the airways from Neo4j and builds the graph. Pass `airfields`
        (from load_airfields) to avoid reading them a second time.

        Raises neo4j_source.SourceError if Neo4j can't be read, and
        validate_data.DataError, listing every problem, if the airways fail
        their checks."""
        if airfields is None:
            airfields = load_airfields(run)
        rows = fetch_airways(run)
        problems = validate_airways(rows)
        if problems:
            raise DataError("The airways in Neo4j failed %d check%s:\n  %s" % (
                len(problems), "" if len(problems) == 1 else "s", "\n  ".join(problems)))
        return cls(airfields, rows)

    def _need(self, airfield_id, role):
        a = self.airfields.get(airfield_id)
        if a is None:
            raise RouteError("Unknown %s airfield." % role)
        if airfield_id not in self.access:
            raise RouteError("%s airfield %s is not on the airway network: it has no waypoint within %d nm."
                             % (role.capitalize(), a["name"], ACCESS_NM))
        return a

    def route(self, origin, goal):
        """The shortest route from one airfield to another along the airways,
        as the map needs it. Raises RouteError if there is none."""
        o, g = self._need(origin, "origin"), self._need(goal, "destination")
        if origin == goal:
            raise RouteError("Origin and destination are the same airfield.")
        (start, out_nm), (end, in_nm) = self.access[origin], self.access[goal]
        # Dijkstra over the waypoints. The counter keeps equal distances in a fixed order.
        dist, came, heap, n = {start: 0.0}, {}, [(0.0, 0, start)], 0
        while heap:
            d, _, u = heapq.heappop(heap)
            if u == end:
                break
            if d > dist[u]:
                continue
            for v, i in self.adj[u]:
                nd = d + self.segments[i]["nm"]
                if nd < dist.get(v, float("inf")):
                    dist[v], came[v] = nd, (u, i)
                    n += 1
                    heapq.heappush(heap, (nd, n, v))
        if end not in dist:
            raise RouteError("No airway route connects %s and %s." % (o["name"], g["name"]))
        keys, used = [end], []
        while keys[-1] != start:
            u, i = came[keys[-1]]
            keys.append(u)
            used.append(i)
        keys.reverse()
        used.reverse()
        legs, airways = [], []
        for u, v, i in zip(keys, keys[1:], used):
            s = self.segments[i]
            legs.append({"from": self.waypoints[u]["name"], "to": self.waypoints[v]["name"], "airway": s["airway"],
                         "segment_id": s["segment_id"], "nm": round(s["nm"], 1),
                         "lower_fl": s["lower_fl"], "upper_fl": s["upper_fl"]})
            if s["airway"] and (not airways or airways[-1] != s["airway"]):
                airways.append(s["airway"])
        points = [self.waypoints[k] for k in keys]
        line = [[o["lon"], o["lat"]]] + [[w["lon"], w["lat"]] for w in points] + [[g["lon"], g["lat"]]]
        return {"length_nm": round(out_nm + dist[end] + in_nm, 1),
                "access_nm": [round(out_nm, 1), round(in_nm, 1)],     # airfield to first waypoint, last waypoint to airfield
                "airways": airways,
                "waypoints": [{"name": w["name"], "lat": w["lat"], "lon": w["lon"]} for w in points],
                "legs": legs, "hops": len(legs), "line": line}


def main():
    try:
        airfields = load_airfields()
    except (SourceError, DataError) as e:
        sys.exit(str(e))
    large = sum(a["type"] == "large" for a in airfields)
    print("%d airfields in Neo4j: %d large, %d medium or other. All checks passed." % (
        len(airfields), large, len(airfields) - large))
    try:
        graph = AirGraph.load(airfields=airfields)
    except (SourceError, DataError) as e:
        sys.exit(str(e))
    print("%d waypoints joined by %d airway segments. %d airfields are within %d nm of a waypoint." % (
        len(graph.waypoints), len(graph.segments), len(graph.access), ACCESS_NM))


if __name__ == "__main__":
    main()
