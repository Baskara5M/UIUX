"""
Reads the sea network from Neo4j, in place of the CSV files in neo4j-datamodel/.

    import neo4j_source
    data = neo4j_source.load()      # fetch everything once, check it, or raise DataError

Neo4j must already be loaded from the CSV files with neo4j-datamodel/load.cypher
(see neo4j-datamodel/START_HERE.md). The CSV files stay the source of truth:
Neo4j can be thrown away and reloaded from them at any time.

Connection settings come from the environment:

    NEO4J_URI        bolt://localhost:7687
    NEO4J_USER       neo4j
    NEO4J_PASSWORD   neo4jtest
    NEO4J_DATABASE   neo4j

Needs the official Neo4j Python driver:  pip install neo4j

Everything is read once, at start-up; routing then runs in memory in
sea_routing.py, exactly as it did with the CSV files. Reload Neo4j -> restart.
"""
import json
import os
import re

import validate_data
from validate_data import (END_TOLERANCE_NM, HARBOR_SIZES, LENGTH_TOLERANCE, DataError, _gc_nm, _line,
                           _number, _point, _Problems)

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD_REPORT = os.path.join(validate_data.DATA, "build-report.json")


class SourceError(Exception):
    """Neo4j can't be reached, or the driver or a setting is missing."""


_LINK = ("r.segment_id AS segment_id, r.length_nm AS length_nm, "
         "r.passage AS passage, r.geometry_wkt AS geometry_wkt")
QUERIES = {
    "ports": "MATCH (p:Port) RETURN p.port_id AS port_id, p.name AS name, p.country AS country, "
             "p.harbor_size AS harbor_size, p.lat AS lat, p.lon AS lon",
    "sea_nodes": "MATCH (s:SeaNode) RETURN s.sea_node_id AS sea_node_id, s.lon AS lon, s.lat AS lat",
    "passages": "MATCH (x:Passage) RETURN x.passage_id AS passage_id, x.name AS name",
    # One query per kind of link, each matched ONE way (->), as load.cypher stored it,
    # so every link comes back once and its geometry runs from a to b.
    # Both kinds of port link are SEA_ACCESS in Neo4j; the far end's label tells them
    # apart (docs/ROUTING_RULES.md section 2, "the Neo4j trap").
    "segment": "MATCH (x:SeaNode)-[r:SEA_SEGMENT]->(y:SeaNode) "
               "RETURN x.sea_node_id AS a, y.sea_node_id AS b, " + _LINK,
    "port_access": "MATCH (x:Port)-[r:SEA_ACCESS]->(y:SeaNode) "
                   "RETURN x.port_id AS a, y.sea_node_id AS b, " + _LINK,
    "port_to_port": "MATCH (x:Port)-[r:SEA_ACCESS]->(y:Port) "
                    "RETURN x.port_id AS a, y.port_id AS b, " + _LINK,
    "label_counts": "MATCH (n) UNWIND labels(n) AS label RETURN label, count(*) AS n",
    "type_counts": "MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS n",
}
LINK_KINDS = ("segment", "port_access", "port_to_port")   # the order links are added to the graph

# Neo4j label / link kind -> the CSV file build-report.json counts it under
LABEL_FILES = {"Port": "nodes/ports.csv", "SeaNode": "nodes/sea_nodes.csv", "Passage": "nodes/passages.csv",
               "Airfield": "nodes/airfields.csv", "Runway": "nodes/runways.csv"}
KIND_FILES = {"segment": "relationships/sea_segments.csv",
              "port_access": "relationships/port_to_sea_node.csv",
              "port_to_port": "relationships/port_to_port.csv"}
KIND_NAMES = {"segment": "SEA_SEGMENT", "port_access": "SEA_ACCESS", "port_to_port": "SEA_ACCESS"}


# ------------------------------------------------------------------ connection
def settings():
    return {"uri": os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
            "user": os.environ.get("NEO4J_USER", "neo4j"),
            "password": os.environ.get("NEO4J_PASSWORD"),
            "database": os.environ.get("NEO4J_DATABASE", "neo4j")}


def _driver_runner(cfg):
    """A function query -> list of row dicts, and a close() for it."""
    try:
        from neo4j import GraphDatabase
    except ImportError:
        raise SourceError("The Neo4j Python driver isn't installed. Run:  pip install neo4j")
    if not cfg["password"]:
        raise SourceError("NEO4J_PASSWORD is not set. Set NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD "
                          "(and NEO4J_DATABASE if it isn't 'neo4j').")
    driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))
    try:
        driver.verify_connectivity()
    except Exception as e:
        driver.close()
        raise SourceError("Can't connect to Neo4j at %s as %s: %s" % (cfg["uri"], cfg["user"], e))

    def run(query):
        with driver.session(database=cfg["database"]) as session:
            return session.run(query).data()
    return run, driver.close


# ------------------------------------------------------------------ reading
def _seq(rid):
    """SEG-123 / SN-123 -> (123, ''): numeric order, which is the CSV files' row
    order for sea segments and sea nodes. Any other id sorts after, by text."""
    m = re.fullmatch(r"[A-Z]+-(\d+)", rid or "")
    return (int(m.group(1)), "") if m else (float("inf"), str(rid or ""))


def fetch(run=None):
    """Every row the app needs, as {name: [row dicts]}, in a fixed order.

    Neo4j returns rows in no promised order, and the order links are added to the
    graph decides which of two equally long routes wins. So the rows are sorted:
    sea segments by segment number (exactly the CSV order), port links by their
    two port / sea node ids (unique for every port link)."""
    close = None
    if run is None:
        run, close = _driver_runner(settings())
    try:
        data = {name: run(q) for name, q in QUERIES.items()}
    except SourceError:
        raise
    except Exception as e:
        raise SourceError("A Neo4j query failed: %s: %s" % (type(e).__name__, e))
    finally:
        if close:
            close()
    data["ports"].sort(key=lambda r: str(r["port_id"]))
    data["sea_nodes"].sort(key=lambda r: _seq(r["sea_node_id"]))
    data["passages"].sort(key=lambda r: str(r["passage_id"]))
    data["segment"].sort(key=lambda r: (_seq(r["segment_id"]), str(r["a"]), str(r["b"])))
    for kind in ("port_access", "port_to_port"):
        data[kind].sort(key=lambda r: (str(r["a"]), str(r["b"]), str(r["segment_id"])))
    return data


# ------------------------------------------------------------------ checking
def validate(data, build_report=BUILD_REPORT):
    """Every problem in what Neo4j holds, as a list of messages ([] if none).

    The same checks as validate_data.py makes on the CSV files, plus the ones a
    database needs: LOAD CSV skips a link whose end node is missing, and
    toFloat('abc') quietly gives null, so counts and nulls are checked here."""
    problems = _Problems()
    coords = {}

    def node_rows(name, label, key, has_coords, has_name):
        seen = set()
        for r in data[name]:
            rid = r.get(key)
            where = "%s %s" % (label, rid if rid else "(no %s)" % key)
            if not rid:
                problems.add("blank id", "%s: %s is blank" % (where, key))
            elif rid in seen:
                problems.add("duplicate id", "%s: %s is used more than once (loaded twice?)" % (where, key))
            seen.add(rid)
            if has_coords:
                p = _point(r.get("lon"), r.get("lat"))
                if p is None:
                    problems.add("bad coordinates", "%s: lon %r / lat %r is not a valid position" % (
                        where, r.get("lon"), r.get("lat")))
                elif rid:
                    coords[rid] = p
            if has_name and not (r.get("name") or "").strip():
                problems.add("blank name", "%s: name is blank" % where)
        return seen

    port_ids = node_rows("ports", "Port", "port_id", True, True)
    node_rows("sea_nodes", "SeaNode", "sea_node_id", True, False)
    passage_ids = node_rows("passages", "Passage", "passage_id", False, True)
    for r in data["ports"]:
        if (r.get("harbor_size") or "") not in HARBOR_SIZES:
            problems.add("bad value", "Port %s: harbor_size %r is not one of %s" % (
                r.get("port_id"), r.get("harbor_size"), ", ".join(repr(s) for s in sorted(HARBOR_SIZES))))

    joined = set()
    for kind in LINK_KINDS:
        for r in data[kind]:
            where = "%s %s -> %s (%s)" % (KIND_NAMES[kind], r.get("a"), r.get("b"), r.get("segment_id") or "no segment_id")
            if r.get("passage") and r["passage"] not in passage_ids:
                problems.add("unknown passage", "%s: passage %r is not a Passage node" % (where, r["passage"]))
            length = _number(r.get("length_nm"))
            if length is None or length < 0:
                problems.add("bad length", "%s: length_nm %r is not a number of 0 or more" % (where, r.get("length_nm")))
            line = _line(r.get("geometry_wkt") or "")
            if line is None:
                problems.add("bad geometry", "%s: geometry_wkt is not a LINESTRING of valid lon lat points" % where)
                continue
            ends = coords.get(r.get("a")), coords.get(r.get("b"))
            if all(ends):
                off = max(_gc_nm(line[0], ends[0]), _gc_nm(line[-1], ends[1]))
                if off > END_TOLERANCE_NM:
                    problems.add("line off its nodes", "%s: the line starts or ends %.1f nm from its nodes" % (where, off))
            if length is not None and length >= 0:
                drawn = sum(_gc_nm(p, q) for p, q in zip(line, line[1:]))
                if abs(drawn - length) > max(1.0, LENGTH_TOLERANCE * length):
                    problems.add("length mismatch", "%s: length_nm is %.1f but the line is %.1f nm long" % (
                        where, length, drawn))
            if kind == "port_access":
                joined.add(r.get("a"))
            elif kind == "port_to_port":
                joined.update((r.get("a"), r.get("b")))
    for pid in sorted(port_ids - joined):
        problems.add("port not joined", "Port %s has no SEA_ACCESS link to the sea network" % pid)

    # Counts against build-report.json: catches links LOAD CSV skipped, a partial load,
    # and load.cypher run twice (every count doubles).
    if os.path.isfile(build_report):
        with open(build_report, encoding="utf-8") as f:
            want = json.load(f).get("rows_written", {})
        labels = {r["label"]: r["n"] for r in data["label_counts"]}
        types = {r["type"]: r["n"] for r in data["type_counts"]}
        for label, name in LABEL_FILES.items():
            if name in want and labels.get(label, 0) != want[name]:
                problems.add("count", "Neo4j has %d %s nodes, but the build wrote %d rows to %s" % (
                    labels.get(label, 0), label, want[name], name))
        for kind, name in KIND_FILES.items():
            if name in want and len(data[kind]) != want[name]:
                problems.add("count", "Neo4j has %d %s links of this kind, but the build wrote %d rows to %s" % (
                    len(data[kind]), KIND_NAMES[kind], want[name], name))
        expected_types = {"SEA_SEGMENT": want.get(KIND_FILES["segment"]),
                          "SEA_ACCESS": (want.get(KIND_FILES["port_access"], 0) + want.get(KIND_FILES["port_to_port"], 0)
                                         if KIND_FILES["port_access"] in want else None),
                          "HAS_RUNWAY": want.get(LABEL_FILES["Runway"])}
        for t, n in expected_types.items():
            if n is not None and types.get(t, 0) != n:
                problems.add("count", "Neo4j has %d %s relationships, but START_HERE.md expects %d" % (
                    types.get(t, 0), t, n))
    else:
        problems.add("missing file", "build-report.json: file is missing, so Neo4j's counts can't be checked")
    return problems.lines()


def load(run=None):
    """Fetch everything from Neo4j and check it. Raises SourceError if Neo4j
    can't be read, DataError (listing every problem) if the data is bad."""
    data = fetch(run)
    problems = validate(data)
    if problems:
        raise DataError("The data in Neo4j (%s, database %s) failed %d check%s:\n  %s" % (
            settings()["uri"], settings()["database"], len(problems), "" if len(problems) == 1 else "s",
            "\n  ".join(problems)))
    return data


def main():
    """python neo4j_source.py  ->  checks what Neo4j holds; exit code 1 on any problem."""
    try:
        data = fetch()
    except SourceError as e:
        print("FAILED: %s" % e)
        return 1
    problems = validate(data)
    cfg = settings()
    if problems:
        print("FAILED: %d problem%s in Neo4j at %s (database %s)\n" % (
            len(problems), "" if len(problems) == 1 else "s", cfg["uri"], cfg["database"]))
        print("\n".join(problems))
        return 1
    print("OK: Neo4j at %s (database %s) passed every check: %d ports, %d sea nodes, %d links." % (
        cfg["uri"], cfg["database"], len(data["ports"]), len(data["sea_nodes"]),
        sum(len(data[k]) for k in LINK_KINDS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
