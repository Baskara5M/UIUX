"""
Checks the air side (air_routing.py) without a Neo4j server.

A stand-in answers air_routing's two queries the way Neo4j would: rows in a
random order, empty values as null, numbers as numbers. The airfields come
from nodes/airfields.csv. The airways come from nodes/awy.csv, the sample
around Hawaii, with the waypoint's name as its waypoint_id. The app itself
never reads these files; only this stand-in does.

    python -m unittest tests.test_air_routing
"""
import csv
import os
import random
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import air_routing  # noqa: E402
from neo4j_source import SourceError  # noqa: E402
from validate_data import DataError  # noqa: E402

CSV = os.path.join(ROOT, "neo4j-datamodel", "nodes", "airfields.csv")


def neo4j_rows():
    """What Neo4j returns for AIRFIELD_QUERY, built from airfields.csv."""
    with open(CSV, encoding="utf-8") as f:
        rows = [{"id": r["airfield_id"], "icao": r["icao_code"] or None, "iata": r["iata_code"] or None,
                 "name": r["name"] or None, "country": r["country_code"] or None, "type": r["airport_type"] or None,
                 "lat": float(r["lat"]), "lon": float(r["lon"]),
                 "runway_ft": int(float(r["longest_paved_runway_ft"])) if r["longest_paved_runway_ft"] else None}
                for r in csv.DictReader(f)]
    random.Random(1).shuffle(rows)             # Neo4j promises no order
    return rows


AWY = os.path.join(ROOT, "neo4j-datamodel", "nodes", "awy.csv")


def airway_rows():
    """What Neo4j returns for AIRWAY_QUERY, built from awy.csv."""
    with open(AWY, encoding="utf-8-sig") as f:
        rows = [{"from_id": r["from_name"], "from_name": r["from_name"],
                 "from_lat": float(r["from_lat"]), "from_lon": float(r["from_lon"]),
                 "to_id": r["to_name"], "to_name": r["to_name"], "to_lat": float(r["to_lat"]), "to_lon": float(r["to_lon"]),
                 "segment_id": r["segmenet_id"], "airway": r["airway"], "airway_level": None,
                 "lower_fl": int(r["lower_ft"]), "upper_fl": int(r["upper_ft"]), "distance_nm": None}
                for r in csv.DictReader(f)]
    random.Random(2).shuffle(rows)             # Neo4j promises no order
    return rows


def fake(rows, airways=None):
    def run(query):
        if query == air_routing.AIRWAY_QUERY:
            return [dict(r) for r in (airways if airways is not None else airway_rows())]
        assert query == air_routing.AIRFIELD_QUERY
        return [dict(r) for r in rows]
    return run


class LoadAirfields(unittest.TestCase):
    def test_every_airfield_once_in_name_order(self):
        rows = neo4j_rows()
        got = air_routing.load_airfields(fake(rows))
        self.assertEqual(len(got), len(rows))
        self.assertEqual(len({a["id"] for a in got}), len(rows))
        self.assertEqual(got, sorted(got, key=lambda a: (a["name"], a["id"])))

    def test_fields_are_what_the_page_expects(self):
        by_id = {a["id"]: a for a in air_routing.load_airfields(fake(neo4j_rows()))}
        self.assertEqual(by_id["RODN"], {"id": "RODN", "icao": "RODN", "iata": "DNA", "name": "Kadena Air Base",
                                         "country": "JP", "type": "large", "lat": 26.351667, "lon": 127.769444,
                                         "runway_ft": 12100})
        self.assertEqual(by_id["PGUA"]["type"], "medium")
        for a in by_id.values():                 # null codes become "", never None
            self.assertIsInstance(a["icao"], str)
            self.assertIsInstance(a["iata"], str)

    def test_same_answer_whatever_order_neo4j_uses(self):
        rows = neo4j_rows()
        again = rows[::-1]
        self.assertEqual(air_routing.load_airfields(fake(rows)), air_routing.load_airfields(fake(again)))

    def test_bad_data_is_refused(self):
        def broken(change):
            rows = neo4j_rows()
            change(rows)
            with self.assertRaises(DataError) as e:
                air_routing.load_airfields(fake(rows))
            return str(e.exception)
        self.assertIn("no Airfield nodes", broken(lambda rows: rows.clear()))
        self.assertIn("more than once", broken(lambda rows: rows.extend([dict(r) for r in rows])))
        self.assertIn("not a valid position", broken(lambda rows: rows[0].update(lat=None)))
        self.assertIn("name is blank", broken(lambda rows: rows[0].update(name=None)))
        self.assertIn("build wrote", broken(lambda rows: rows.pop()))

    def test_a_failed_query_is_a_source_error(self):
        def run(query):
            raise RuntimeError("connection lost")
        with self.assertRaises(SourceError):
            air_routing.load_airfields(run)


@unittest.skipUnless(os.path.isfile(AWY), "the sample airways file, nodes/awy.csv, is not here")
class Airways(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = air_routing.AirGraph.load(fake(neo4j_rows()))

    def test_graph_is_what_the_rows_describe(self):
        rows = airway_rows()
        names = {r["from_name"] for r in rows} | {r["to_name"] for r in rows}
        self.assertEqual(set(self.g.waypoints), names)
        self.assertEqual(len(self.g.segments), len(rows))
        self.assertEqual(sum(len(v) for v in self.g.adj.values()), 2 * len(rows))   # both ways

    def test_airfields_join_at_their_nearest_waypoint(self):
        self.assertEqual(self.g.access["PHNL"][0], "HNL")
        self.assertLess(self.g.access["PHNL"][1], 1)
        self.assertNotIn("RODN", self.g.access)          # Kadena is nowhere near Hawaii's airways
        for wp, nm in self.g.access.values():
            self.assertLessEqual(nm, air_routing.ACCESS_NM)

    def test_route_adds_up_and_joins_end_to_end(self):
        r = self.g.route("PHNL", "PHTO")
        self.assertEqual(r["waypoints"][0]["name"], "HNL")
        self.assertEqual(r["waypoints"][-1]["name"], "ITO")
        self.assertEqual(r["hops"], len(r["legs"]))
        self.assertEqual(len(r["line"]), len(r["waypoints"]) + 2)
        for a, b in zip(r["legs"], r["legs"][1:]):
            self.assertEqual(a["to"], b["from"])
        total = sum(leg["nm"] for leg in r["legs"]) + sum(r["access_nm"])
        self.assertAlmostEqual(r["length_nm"], total, delta=0.1 * (len(r["legs"]) + 2))
        direct = air_routing.great_circle_nm((21.318681, -157.922429), (19.721399, -155.048004))
        self.assertGreaterEqual(r["length_nm"], direct - 1)     # never shorter than the straight line

    def test_same_length_both_ways_and_whatever_order_neo4j_uses(self):
        back = self.g.route("PHTO", "PHNL")
        self.assertEqual(back["length_nm"], self.g.route("PHNL", "PHTO")["length_nm"])
        other = air_routing.AirGraph.load(fake(neo4j_rows()[::-1], airway_rows()[::-1]))
        self.assertEqual(other.route("PHNL", "PHTO"), self.g.route("PHNL", "PHTO"))

    def test_requests_that_cannot_be_routed(self):
        for origin, goal, text in (("PHNL", "PHNL", "same airfield"), ("PHNL", "NOPE", "Unknown destination"),
                                   ("NOPE", "PHNL", "Unknown origin"), ("PHNL", "RODN", "not on the airway network")):
            with self.assertRaises(air_routing.RouteError) as e:
                self.g.route(origin, goal)
            self.assertIn(text, str(e.exception))

    def test_bad_airways_are_refused(self):
        def broken(change):
            rows = airway_rows()
            change(rows)
            with self.assertRaises(DataError) as e:
                air_routing.AirGraph.load(fake(neo4j_rows(), rows))
            return str(e.exception)
        self.assertIn("no AIRWAY relationships", broken(lambda rows: rows.clear()))
        self.assertIn("not a valid position", broken(lambda rows: rows[0].update(to_lat=None)))
        self.assertIn("two different positions", broken(lambda rows: rows[0].update(from_lat=rows[0]["from_lat"] + 1)))


if __name__ == "__main__":
    unittest.main()
