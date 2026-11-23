"""
Checks the airfield loader (air_routing.py) without a Neo4j server.

A stand-in answers air_routing.AIRFIELD_QUERY from nodes/airfields.csv, the way
Neo4j would after load.cypher: rows in a random order, empty values as null,
numbers as numbers.

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


def fake(rows):
    def run(query):
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


if __name__ == "__main__":
    unittest.main()
