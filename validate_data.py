"""
Checks the data model in neo4j-datamodel/ before anything uses it.

    python validate_data.py [data_dir]      prints every problem; exit code 1 if any

Run it on the CSV files before loading them into Neo4j. (The web server used
to run these checks at start-up; it now reads from Neo4j, and neo4j_source.py
runs the same checks on what Neo4j holds, reusing the helpers in this file.) Each message names the file, the row (numbered as a spreadsheet
numbers it: the header is row 1) and the field, so a bad row can be found and
fixed. Python 3, stdlib only.

What is checked:

  * every file is there, with the columns the app and load.cypher use
  * ids are present and unique; every link points at a node that exists
  * coordinates are numbers within range; lengths are numbers, not negative
  * each link's line starts and ends on its two nodes, and its drawn length
    matches length_nm within 2%
  * passage tags name a passage in passages.csv; runways name an airfield
  * row counts match build-report.json, so a partial copy is caught
  * every port is joined to the sea network
"""
import csv
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "neo4j-datamodel")
NM_RADIUS = 6371.0088 / 1.852
END_TOLERANCE_NM = 0.5        # a link's line must start and end this close to its nodes
LENGTH_TOLERANCE = 0.02       # drawn length vs length_nm, for links over 1 nm
MAX_PER_CHECK = 20            # messages shown per kind of problem; the rest are counted

HARBOR_SIZES = {"", "Very Small", "Small", "Medium", "Large"}
NODE_FILES = {
    "nodes/ports.csv": ("port_id", ["name", "country", "lat", "lon", "harbor_size"]),
    "nodes/sea_nodes.csv": ("sea_node_id", ["lon", "lat"]),
    "nodes/passages.csv": ("passage_id", ["name"]),
    "nodes/airfields.csv": ("airfield_id", ["name", "lat", "lon", "longest_paved_runway_ft"]),
    "nodes/runways.csv": ("runway_id", ["airfield_id"]),
}
# file: (from column, from nodes, to column, to nodes)
LINK_FILES = {
    "relationships/sea_segments.csv": ("from_sea_node_id", "sea_node", "to_sea_node_id", "sea_node"),
    "relationships/port_to_sea_node.csv": ("port_id", "port", "sea_node_id", "sea_node"),
    "relationships/port_to_port.csv": ("from_port_id", "port", "to_port_id", "port"),
}
LINK_COLUMNS = ["segment_id", "length_nm", "passage", "geometry_wkt"]


class DataError(Exception):
    """The data model failed validation. str() lists the problems."""


def _gc_nm(a, b):
    p1, p2 = math.radians(a[1]), math.radians(b[1])
    dp, dl = p2 - p1, math.radians(b[0] - a[0])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * NM_RADIUS * math.asin(min(1.0, math.sqrt(h)))


def _number(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _point(lon, lat):
    lon, lat = _number(lon), _number(lat)
    if lon is None or lat is None or not -180 <= lon <= 180 or not -90 <= lat <= 90:
        return None
    return lon, lat


def _line(wkt):
    """LINESTRING(lon lat, lon lat, ...) as points, or None if it isn't one."""
    if not (wkt.startswith("LINESTRING(") and wkt.endswith(")")):
        return None
    pts = []
    for pair in wkt[len("LINESTRING("):-1].split(", "):
        xy = pair.split()
        p = _point(*xy) if len(xy) == 2 else None
        if p is None:
            return None
        pts.append(p)
    return pts if len(pts) >= 2 else None


class _Problems:
    def __init__(self):
        self.by_kind = {}

    def add(self, kind, message):
        self.by_kind.setdefault(kind, []).append(message)

    def lines(self):
        out = []
        for kind, msgs in self.by_kind.items():
            out.extend(msgs[:MAX_PER_CHECK])
            if len(msgs) > MAX_PER_CHECK:
                out.append("... and %d more like that (%s)" % (len(msgs) - MAX_PER_CHECK, kind))
        return out

    def __bool__(self):
        return bool(self.by_kind)


def _read(data_dir, name, columns, problems):
    path = os.path.join(data_dir, name)
    if not os.path.isfile(path):
        problems.add("missing file", "%s: file is missing" % name)
        return None
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in columns if c not in (reader.fieldnames or [])]
        if missing:
            problems.add("missing column", "%s: missing column%s %s" % (
                name, "" if len(missing) == 1 else "s", ", ".join(missing)))
            return None
        return list(reader)


def validate(data_dir=DATA):
    """Every problem in the data model, as a list of messages ([] if none)."""
    problems = _Problems()
    coords = {"port": {}, "sea_node": {}}
    ids = {}

    for name, (key, columns) in NODE_FILES.items():
        rows = _read(data_dir, name, [key] + columns, problems)
        if rows is None:
            continue
        seen = set()
        for i, r in enumerate(rows, 2):
            rid = (r[key] or "").strip()
            where = "%s row %d (%s)" % (name, i, rid or "no id")
            if not rid:
                problems.add("blank id", "%s: %s is blank" % (where, key))
            elif rid in seen:
                problems.add("duplicate id", "%s: %s %s is used more than once" % (where, key, rid))
            seen.add(rid)
            if "lat" in columns:
                p = _point(r["lon"], r["lat"])
                if p is None:
                    problems.add("bad coordinates", "%s: lon %r / lat %r is not a valid position" % (
                        where, r["lon"], r["lat"]))
                elif name == "nodes/ports.csv":
                    coords["port"][rid] = p
                elif name == "nodes/sea_nodes.csv":
                    coords["sea_node"][rid] = p
            if name in ("nodes/ports.csv", "nodes/airfields.csv", "nodes/passages.csv") and not r["name"].strip():
                problems.add("blank name", "%s: name is blank" % where)
            if name == "nodes/ports.csv" and r["harbor_size"] not in HARBOR_SIZES:
                problems.add("bad value", "%s: harbor_size %r is not one of %s" % (
                    where, r["harbor_size"], ", ".join(repr(s) for s in sorted(HARBOR_SIZES))))
            if name == "nodes/runways.csv" and "airfield" in ids and r["airfield_id"] not in ids["airfield"]:
                problems.add("unknown node", "%s: airfield_id %s is not in nodes/airfields.csv" % (
                    where, r["airfield_id"]))
        ids[key[:-3]] = seen      # port, sea_node, passage, airfield, runway

    joined = set()
    for name, (a_col, a_kind, b_col, b_kind) in LINK_FILES.items():
        rows = _read(data_dir, name, [a_col, b_col] + LINK_COLUMNS, problems)
        if rows is None:
            continue
        for i, r in enumerate(rows, 2):
            where = "%s row %d (%s)" % (name, i, r["segment_id"] or "no segment_id")
            ends = []
            for col, kind in ((a_col, a_kind), (b_col, b_kind)):
                if r[col] not in ids.get(kind, ()):
                    problems.add("unknown node", "%s: %s %r is not in nodes/%ss.csv" % (where, col, r[col], kind))
                ends.append(coords[kind].get(r[col]))
            if r["passage"] and r["passage"] not in ids.get("passage", ()):
                problems.add("unknown passage", "%s: passage %r is not in nodes/passages.csv" % (where, r["passage"]))
            length = _number(r["length_nm"])
            if length is None or length < 0:
                problems.add("bad length", "%s: length_nm %r is not a number of 0 or more" % (where, r["length_nm"]))
            line = _line(r["geometry_wkt"])
            if line is None:
                problems.add("bad geometry", "%s: geometry_wkt is not a LINESTRING of valid lon lat points" % where)
                continue
            if all(ends):
                off = max(_gc_nm(line[0], ends[0]), _gc_nm(line[-1], ends[1]))
                if off > END_TOLERANCE_NM:
                    problems.add("line off its nodes", "%s: the line starts or ends %.1f nm from its nodes" % (where, off))
            if length is not None and length >= 0:
                drawn = sum(_gc_nm(p, q) for p, q in zip(line, line[1:]))
                if abs(drawn - length) > max(1.0, LENGTH_TOLERANCE * length):
                    problems.add("length mismatch", "%s: length_nm is %.1f but the line is %.1f nm long" % (
                        where, length, drawn))
            if name == "relationships/port_to_sea_node.csv":
                joined.add(r[a_col])
            elif name == "relationships/port_to_port.csv":
                joined.update((r[a_col], r[b_col]))

    for pid in sorted(set(coords["port"]) - joined):
        problems.add("port not joined", "nodes/ports.csv: port %s has no link to the sea network" % pid)

    report = os.path.join(data_dir, "build-report.json")
    if os.path.isfile(report):
        with open(report, encoding="utf-8") as f:
            expected = json.load(f).get("rows_written", {})
        for name, want in expected.items():
            path = os.path.join(data_dir, name)
            if os.path.isfile(path):
                with open(path, newline="", encoding="utf-8-sig") as f:
                    got = sum(1 for _ in csv.DictReader(f))
                if got != want:
                    problems.add("row count", "%s: %d rows, but build-report.json says the build wrote %d" % (
                        name, got, want))
    else:
        problems.add("missing file", "build-report.json: file is missing, so row counts can't be checked")
    return problems.lines()


def check(data_dir=DATA):
    """Raise DataError listing every problem, or return quietly."""
    problems = validate(data_dir)
    if problems:
        raise DataError("The data model in %s failed %d check%s:\n  %s" % (
            data_dir, len(problems), "" if len(problems) == 1 else "s", "\n  ".join(problems)))


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    data_dir = args[0] if args else DATA
    problems = validate(data_dir)
    if problems:
        print("FAILED: %d problem%s in %s\n" % (len(problems), "" if len(problems) == 1 else "s", data_dir))
        print("\n".join(problems))
        return 1
    print("OK: %s passed every check." % data_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
