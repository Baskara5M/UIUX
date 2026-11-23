"""
This version still gives the reference answers in acceptance/golden.json.
The same comparison check_server.py makes over HTTP, made here in-process.

    python -m unittest tests.test_acceptance
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "acceptance"))
from check_server import CASES, GOLDEN, compare, summarize  # noqa: E402
# CSV: the reference server used to read the CSV files, so this always ran:
# from make_golden import answer  # noqa: E402
# It now reads Neo4j, so it runs only when Neo4j is configured (NEO4J_PASSWORD etc.).
# tests/test_neo4j_source.py checks the same answers without a Neo4j server.
NEEDS_NEO4J = "needs Neo4j: set NEO4J_URI, NEO4J_USER and NEO4J_PASSWORD"
answer = None
if os.environ.get("NEO4J_PASSWORD"):
    from make_golden import answer  # noqa: E402


class ReferenceAnswers(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("NEO4J_PASSWORD"), NEEDS_NEO4J)
    def test_every_case_matches_golden(self):
        with open(CASES, encoding="utf-8") as f:
            cases = json.load(f)["cases"]
        with open(GOLDEN, encoding="utf-8") as f:
            golden = json.load(f)["answers"]
        self.assertEqual(sorted(golden), sorted(c["id"] for c in cases), "golden.json is out of date with cases.json")
        for case in cases:
            with self.subTest(case=case["id"]):
                self.assertEqual(compare(golden[case["id"]], summarize(*answer(case["request"]))), [])

    def test_compare_catches_a_wrong_answer(self):
        want = {"status": 200, "blocked": None, "original_nm": 1449.6, "closed_links": 10, "affected": True,
                "extra_nm": 50.0, "routes": [{"length_nm": 1499.6, "passages": ["Luzon Strait"]}]}
        wrong = dict(want, closed_links=40, routes=[{"length_nm": 1530.0, "passages": ["Taiwan Strait"]}])
        problems = compare(want, wrong)
        self.assertEqual(len(problems), 3, problems)
        self.assertEqual(compare(want, dict(want, closed_links=11)), [], "small closed-link differences are allowed")


if __name__ == "__main__":
    unittest.main()
