"""
Writes web/airfields.json, the airfield list the map's Air tab shows, from
neo4j-datamodel/nodes/airfields.csv (the same CSV Neo4j is loaded from).

    python tools/make_airfields_json.py

Run it again whenever the data model is rebuilt, so the map lists the same
airfields as Neo4j. Python 3, standard library only. One airfield per line,
sorted by airfield_id, so a rebuild shows up as a readable diff.
"""
import csv
import json
import math
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SOURCE = os.path.join(ROOT, "neo4j-datamodel", "nodes", "airfields.csv")
TARGET = os.path.join(ROOT, "web", "airfields.json")
REPORT = os.path.join(ROOT, "neo4j-datamodel", "build-report.json")


def number(v, kind=float):
    try:
        x = kind(float(v))
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def main():
    problems, seen, out = [], set(), []
    with open(SOURCE, encoding="utf-8") as f:
        for row_no, r in enumerate(csv.DictReader(f), start=2):
            aid = (r.get("airfield_id") or "").strip()
            lat, lon = number(r.get("lat")), number(r.get("lon"))
            if not aid:
                problems.append("row %d: airfield_id is blank" % row_no)
                continue
            if aid in seen:
                problems.append("row %d: airfield_id %s is used more than once" % (row_no, aid))
            seen.add(aid)
            if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
                problems.append("row %d (%s): lon %r / lat %r is not a valid position" % (
                    row_no, aid, r.get("lon"), r.get("lat")))
                continue
            if not (r.get("name") or "").strip():
                problems.append("row %d (%s): name is blank" % (row_no, aid))
            out.append({
                "id": aid,
                "icao": r.get("icao_code") or "",
                "iata": r.get("iata_code") or "",
                "name": (r.get("name") or "").strip(),
                "country": r.get("country_code") or "",
                "lat": round(lat, 5),            # 5 decimals is about 1 m
                "lon": round(lon, 5),
                "runway_ft": number(r.get("longest_paved_runway_ft"), int),
                "type": (r.get("airport_type") or "").replace("_airport", ""),   # large / medium
            })
    # The build report says how many rows the build wrote; a mismatch means a stale or partial CSV.
    if os.path.isfile(REPORT):
        with open(REPORT, encoding="utf-8") as f:
            want = json.load(f).get("rows_written", {}).get("nodes/airfields.csv")
        if want is not None and want != len(seen):
            problems.append("airfields.csv has %d airfields, but build-report.json says the build wrote %d"
                            % (len(seen), want))
    if problems:
        sys.exit("Not written, %d problem%s:\n  %s" % (len(problems), "" if len(problems) == 1 else "s",
                                                       "\n  ".join(problems)))
    out.sort(key=lambda a: a["id"])
    with open(TARGET, "w", encoding="utf-8", newline="\n") as f:
        f.write("[\n" + ",\n".join(json.dumps(a, ensure_ascii=False, separators=(",", ":")) for a in out) + "\n]\n")
    print("wrote %d airfields to %s (%d KB)" % (len(out), os.path.relpath(TARGET, ROOT),
                                                os.path.getsize(TARGET) // 1024))


if __name__ == "__main__":
    main()
