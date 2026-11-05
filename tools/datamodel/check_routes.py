"""
Shortest sea route between ports, computed straight from the import package's
CSVs -- the same answer a Neo4j weighted shortest path over
SEA_SEGMENT | SEA_ACCESS gives. Use it to check a load, or as a reference for
how the graph is meant to be traversed and closed.

    python tools/datamodel/check_routes.py [package_dir] [--close malacca,luzon] "San Diego" "Apra Harbor" ...

Names are matched against ports.csv `name` (case-insensitive) and passed in
pairs; with none, a fixed set of Indo-Pacific pairs runs. --close takes
passage ids (nodes/passages.csv): every link tagged with one is left out,
port links included, so a closed strait cannot be bypassed through a port.
"""
import csv
import heapq
import os
import sys
from collections import defaultdict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_PAIRS = [
    ("San Diego", "Apra Harbor"), ("Busan", "Subic Bay"), ("Yokosuka Ko", "Keppel - (East Singapore)"),
    ("Apra Harbor", "Diego Garcia"), ("Los Angeles", "Busan"), ("Darwin", "Keppel - (East Singapore)"),
    ("Tacoma", "Yokosuka Ko"), ("Kao-Hsiung", "Manila"), ("Keppel - (East Singapore)", "Colombo"),
    ("Honolulu", "Apra Harbor"),
]


def load(pkg, closed):
    graph = defaultdict(list)
    for name, a, b in (("sea_segments.csv", "from_sea_node_id", "to_sea_node_id"),
                       ("port_to_sea_node.csv", "port_id", "sea_node_id"),
                       ("port_to_port.csv", "from_port_id", "to_port_id")):
        with open(os.path.join(pkg, "relationships", name), encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["passage"] in closed:
                    continue
                nm = float(r["length_nm"])
                graph[r[a]].append((r[b], nm))
                graph[r[b]].append((r[a], nm))
    with open(os.path.join(pkg, "nodes", "ports.csv"), encoding="utf-8") as f:
        ports = {}
        for r in csv.DictReader(f):
            ports.setdefault(r["name"].lower(), r["port_id"])
    return graph, ports


def shortest_nm(graph, start, goal):
    dist, heap = {start: 0.0}, [(0.0, start)]
    while heap:
        d, n = heapq.heappop(heap)
        if n == goal:
            return d
        if d > dist[n]:
            continue
        for m, w in graph[n]:
            nd = d + w
            if nd < dist.get(m, float("inf")):
                dist[m] = nd
                heapq.heappush(heap, (nd, m))
    return None


def main():
    args = sys.argv[1:]
    here = os.path.dirname(os.path.abspath(__file__))
    # Run from inside the package (where the build copies it) or from the repo.
    pkg = here if os.path.isdir(os.path.join(here, "nodes")) else os.path.join(ROOT, "neo4j-datamodel")
    if args and os.path.isdir(args[0]):
        pkg, args = args[0], args[1:]
    closed = set()
    if args and args[0] == "--close":
        closed, args = set(args[1].split(",")), args[2:]
    pairs = list(zip(args[::2], args[1::2])) or DEFAULT_PAIRS
    graph, ports = load(pkg, closed)
    for a, b in pairs:
        pa, pb = ports.get(a.lower()), ports.get(b.lower())
        if not pa or not pb:
            print(f"{a} -> {b}: port name not found")
            continue
        nm = shortest_nm(graph, pa, pb)
        print(f"{a} -> {b}: {'no route' if nm is None else f'{nm:,.0f} nm'}")


if __name__ == "__main__":
    main()
