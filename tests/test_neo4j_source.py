"""
Checks the Neo4j loader without a Neo4j server.

A stand-in answers neo4j_source's queries from the CSV files, the way Neo4j
would after load.cypher: rows in a random order, empty values as null, numbers
as floats. The graph built from it must equal the one the CSV files give, and
every acceptance case must still match acceptance/golden.json.

    python -m unittest tests.test_neo4j_source
"""
import csv
import json
import os
import random
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "acceptance"))
import neo4j_source  # noqa: E402
from sea_routing import SeaGraph, parse_wkt  # noqa: E402
from validate_data import DataError  # noqa: E402

DATA = os.path.join(ROOT, "neo4j-datamodel")


def _rows(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _text(v):
    return v if v != "" else None          # LOAD CSV reads an empty field as null


def _float(v):
    return float(v) if v != "" else None   # toFloat('') is null


def fake_neo4j(seed=1):
    """What Neo4j returns for each of neo4j_source.QUERIES, built from the CSVs."""
    rng = random.Random(seed)

    def links(name, a, b):
        return [{"a": r[a], "b": r[b], "segment_id": r["segment_id"], "length_nm": _float(r["length_nm"]),
                 "passage": _text(r["passage"]), "geometry_wkt": r["geometry_wkt"]} for r in _rows(name)]

    answers = {
        "ports": [{"port_id": r["port_id"], "name": _text(r["name"]), "country": _text(r["country"]),
                   "harbor_size": _text(r["harbor_size"]), "lat": _float(r["lat"]), "lon": _float(r["lon"])}
                  for r in _rows("nodes/ports.csv")],
        "sea_nodes": [{"sea_node_id": r["sea_node_id"], "lon": _float(r["lon"]), "lat": _float(r["lat"])}
                      for r in _rows("nodes/sea_nodes.csv")],
        "passages": [{"passage_id": r["passage_id"], "name": _text(r["name"])} for r in _rows("nodes/passages.csv")],
        "segment": links("relationships/sea_segments.csv", "from_sea_node_id", "to_sea_node_id"),
        "port_access": links("relationships/port_to_sea_node.csv", "port_id", "sea_node_id"),
        "port_to_port": links("relationships/port_to_port.csv", "from_port_id", "to_port_id"),
    }
    answers["label_counts"] = [
        {"label": label, "n": len(_rows(name))} for label, name in neo4j_source.LABEL_FILES.items()]
    answers["type_counts"] = [
        {"type": "SEA_SEGMENT", "n": len(answers["segment"])},
        {"type": "SEA_ACCESS", "n": len(answers["port_access"]) + len(answers["port_to_port"])},
        {"type": "HAS_RUNWAY", "n": len(_rows("nodes/runways.csv"))}]
    for rows in answers.values():
        rng.shuffle(rows)                  # Neo4j promises no order
    by_query = {q: name for name, q in neo4j_source.QUERIES.items()}

    def run(query):
        return [dict(r) for r in answers[by_query[query]]]
    return run, answers


def csv_graph():
    """The graph exactly as the old CSV loader built it (kept here as the yardstick)."""
    coords, ports, edges = {}, {}, []
    for r in _rows("nodes/ports.csv"):
        ports[r["port_id"]] = (r["name"], r["country"], r["harbor_size"])
        coords[r["port_id"]] = (float(r["lon"]), float(r["lat"]))
    for r in _rows("nodes/sea_nodes.csv"):
        coords[r["sea_node_id"]] = (float(r["lon"]), float(r["lat"]))
    for name, a, b, kind in (("sea_segments.csv", "from_sea_node_id", "to_sea_node_id", "segment"),
                             ("port_to_sea_node.csv", "port_id", "sea_node_id", "port_access"),
                             ("port_to_port.csv", "from_port_id", "to_port_id", "port_to_port")):
        for r in _rows("relationships/" + name):
            edges.append((r[a], r[b], float(r["length_nm"]), r["passage"] or None, kind,
                          tuple(parse_wkt(r["geometry_wkt"]))))
    return coords, ports, sorted(edges)


class LoadFromNeo4j(unittest.TestCase):
    def test_same_graph_as_the_csv_files(self):
        run, _ = fake_neo4j()
        g = SeaGraph.load(run)
        coords, ports, edges = csv_graph()
        self.assertEqual(g.coords, coords)
        self.assertEqual({pid: (p["name"], p["country"], p["harbor_size"]) for pid, p in g.ports.items()}, ports)
        self.assertEqual(sorted(e[:5] + (tuple(e[5]),) for e in g.edges.values()), edges)
        self.assertEqual(g.passages, {r["passage_id"]: r["name"] for r in _rows("nodes/passages.csv")})

    def test_order_does_not_depend_on_neo4j(self):
        a, b = SeaGraph.load(fake_neo4j(1)[0]), SeaGraph.load(fake_neo4j(2)[0])
        self.assertEqual(a.edges, b.edges)
        self.assertEqual(dict(a.adj), dict(b.adj))

    def test_sea_segments_keep_the_csv_order(self):
        g = SeaGraph.load(fake_neo4j()[0])
        stored = [(r["from_sea_node_id"], r["to_sea_node_id"]) for r in _rows("relationships/sea_segments.csv")]
        self.assertEqual([g.edges["segment:%d" % i][:2] for i in range(len(stored))], stored)


class ChecksOnNeo4j(unittest.TestCase):
    def problems(self, change):
        run, answers = fake_neo4j()
        change(answers)
        return neo4j_source.validate(neo4j_source.fetch(run))

    def assertProblem(self, change, *words):
        found = self.problems(change)
        self.assertTrue(any(all(w in p for w in words) for p in found), "no %s in %s" % (words, found))

    def test_clean_data_passes(self):
        self.assertEqual(self.problems(lambda a: None), [])

    def test_null_length(self):
        self.assertProblem(lambda a: a["segment"][0].update(length_nm=None), "SEA_SEGMENT", "length_nm")

    def test_bad_coordinates(self):
        self.assertProblem(lambda a: a["ports"][0].update(lat=None), "Port", "not a valid position")

    def test_link_skipped_by_load_csv(self):
        self.assertProblem(lambda a: a["port_to_port"].pop(), "2506", "port_to_port.csv")

    def test_loaded_twice(self):
        def twice(a):
            a["ports"].extend(dict(r) for r in list(a["ports"]))
            for r in a["label_counts"]:
                r["n"] *= 2
        self.assertProblem(twice, "duplicate id")
        self.assertProblem(twice, "7612 Port nodes", "build wrote 3806")

    def test_port_not_joined(self):
        def cut(a):
            pid = "WPI-60390"
            for kind in ("port_access", "port_to_port"):
                a[kind][:] = [r for r in a[kind] if pid not in (r["a"], r["b"])]
        self.assertProblem(cut, "WPI-60390", "no SEA_ACCESS")

    def test_load_raises_data_error(self):
        run, answers = fake_neo4j()
        answers["segment"][0]["geometry_wkt"] = "POINT(1 2)"
        with self.assertRaises(DataError):
            neo4j_source.load(run)

    def test_no_password_is_a_clear_error(self):
        cfg = dict(neo4j_source.settings(), password=None)
        with self.assertRaises(neo4j_source.SourceError):
            neo4j_source._driver_runner(cfg)


class SameAnswers(unittest.TestCase):
    """Every acceptance case, answered from the stand-in Neo4j, matches golden.json."""

    def test_every_case_matches_golden(self):
        if "server" not in sys.modules:          # build the server's graph from the stand-in
            run, _ = fake_neo4j()
            real = neo4j_source._driver_runner
            neo4j_source._driver_runner = lambda cfg: (run, lambda: None)
            try:
                import server  # noqa: F401
            finally:
                neo4j_source._driver_runner = real
        from check_server import CASES, GOLDEN, compare, summarize
        from make_golden import answer
        with open(CASES, encoding="utf-8") as f:
            cases = json.load(f)["cases"]
        with open(GOLDEN, encoding="utf-8") as f:
            golden = json.load(f)["answers"]
        for case in cases:
            with self.subTest(case=case["id"]):
                self.assertEqual(compare(golden[case["id"]], summarize(*answer(case["request"]))), [])


if __name__ == "__main__":
    unittest.main()
