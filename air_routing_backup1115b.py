"""
The air side of FALCON. For now it reads the airfields; air route search
(AirGraph) is not built yet.

    import air_routing
    airfields = air_routing.load_airfields()   # fetch from Neo4j, check, or raise

The airfields are the Airfield nodes that neo4j-datamodel/load.cypher creates
from nodes/airfields.csv. server.py serves this list as GET /api/airports, and
web/air_routing.html draws it.

The connection is the same one the sea side uses (NEO4J_URI, NEO4J_USER,
NEO4J_PASSWORD, NEO4J_DATABASE; see neo4j_source.py). Everything is read once,
at start-up. Reload Neo4j -> restart the server.

    python air_routing.py        checks the airfields Neo4j holds and prints a summary
"""
import json
import os
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


def fetch_airfields(run=None):
    """The rows Neo4j returns for AIRFIELD_QUERY, sorted by airfield id (Neo4j
    promises no order). `run` (a function: Cypher query -> list of row dicts)
    is only for tests; normally the connection comes from the NEO4J_* settings."""
    close = None
    if run is None:
        run, close = neo4j_source._driver_runner(neo4j_source.settings())
    try:
        rows = run(AIRFIELD_QUERY)
    except SourceError:
        raise
    except Exception as e:
        raise SourceError("The Neo4j airfield query failed: %s: %s" % (type(e).__name__, e))
    finally:
        if close:
            close()
    return sorted(rows, key=lambda r: str(r.get("id") or ""))


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


def main():
    try:
        airfields = load_airfields()
    except (SourceError, DataError) as e:
        sys.exit(str(e))
    large = sum(a["type"] == "large" for a in airfields)
    print("%d airfields in Neo4j: %d large, %d medium or other. All checks passed." % (
        len(airfields), large, len(airfields) - large))


if __name__ == "__main__":
    main()
