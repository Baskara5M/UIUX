"""
Writes acceptance/golden.json: this reference version's answer to every
request in acceptance/cases.json.

    python acceptance/make_golden.py

Run it only when the reference behaviour is meant to change (or a case is
added), and say why in the commit. The golden answers are the target a
migrated version is checked against, so they must not drift by accident.
"""
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import server  # noqa: E402  (loads and validates the data model)
from check_server import CASES, GOLDEN, summarize  # noqa: E402

DATA_FILES = ["nodes/ports.csv", "nodes/sea_nodes.csv", "nodes/passages.csv", "relationships/sea_segments.csv",
              "relationships/port_to_sea_node.csv", "relationships/port_to_port.csv"]


def answer(request):
    """(status, body) from the reference route finder, as the web server would send it."""
    try:
        return 200, server.find_routes(request)
    except (server.BadRequest, ValueError) as e:
        return 400, {"error": str(e)}


def main():
    with open(CASES, encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    answers = {}
    for case in cases:
        status, body = answer(case["request"])
        if status != case["expect_status"]:
            raise SystemExit("%s: the reference answered %d, but cases.json expects %d" % (
                case["id"], status, case["expect_status"]))
        answers[case["id"]] = summarize(status, body)
    rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    hashes = {}
    for name in DATA_FILES:
        with open(os.path.join(ROOT, "neo4j-datamodel", name), "rb") as f:
            hashes[name] = hashlib.sha256(f.read()).hexdigest()[:16]
    with open(GOLDEN, "w", encoding="utf-8") as f:
        json.dump({"about": "Reference answers for acceptance/cases.json. Made by acceptance/make_golden.py; "
                            "do not edit by hand.",
                   "made_from": {"commit": rev or "unknown", "data_sha256": hashes},
                   "answers": answers}, f, indent=2)
    print("Wrote %d answers to %s" % (len(answers), os.path.relpath(GOLDEN, ROOT)))


if __name__ == "__main__":
    main()
