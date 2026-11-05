"""
Sea routing over the data-model CSVs in neo4j-datamodel/. Python 3, stdlib only.

    from sea_routing import SeaGraph
    g = SeaGraph.load()
    g.shortest(origin, goal)                 # one route
    g.k_shortest(origin, goal, k=3)          # Yen's K shortest loopless routes
    g.distinct_routes(origin, goal, k=3)     # K routes through different waters

Every SEA_SEGMENT and SEA_ACCESS row is an undirected link weighted by
length_nm. A port is a start or an end, never a waypoint: only a route's own
origin and destination may use their links onto the sea network. The links
between neighbouring ports on one lane stay usable, so a short coastal hop
doesn't have to run out to the lane's end and back.
"""
import csv
import heapq
import math
import os
from collections import defaultdict

import validate_data

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "neo4j-datamodel")
NM_RADIUS = 6371.0088 / 1.852        # Earth's mean radius in nm, as the build used


def great_circle_nm(a, b):
    """a, b = (lon, lat). Never more than the network distance, so A* stays exact."""
    p1, p2 = math.radians(a[1]), math.radians(b[1])
    dp, dl = p2 - p1, math.radians(b[0] - a[0])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * NM_RADIUS * math.asin(min(1.0, math.sqrt(h)))


def _interpolate(a, b, t):
    l1, p1, l2, p2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    d = 2 * math.asin(min(1.0, math.sqrt(
        math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2)))
    if d == 0:
        return a
    s1, s2 = math.sin((1 - t) * d) / math.sin(d), math.sin(t * d) / math.sin(d)
    x = s1 * math.cos(p1) * math.cos(l1) + s2 * math.cos(p2) * math.cos(l2)
    y = s1 * math.cos(p1) * math.sin(l1) + s2 * math.cos(p2) * math.sin(l2)
    z = s1 * math.sin(p1) + s2 * math.sin(p2)
    return (math.degrees(math.atan2(y, x)), math.degrees(math.atan2(z, math.hypot(x, y))))


def densify(line, step_nm=10.0):
    """Points along a line no more than step_nm apart, along great circles."""
    out = [line[0]]
    for a, b in zip(line, line[1:]):
        n = int(great_circle_nm(a, b) // step_nm)
        out.extend(_interpolate(a, b, i / (n + 1)) for i in range(1, n + 1))
        out.append(b)
    return out


class PointSet:
    """Points in a 1-degree grid, for 'is anything within r nm of p?'."""

    def __init__(self, points):
        self.grid = defaultdict(list)
        for q in points:
            self.grid[(math.floor(q[0]), math.floor(q[1]))].append(q)

    def near(self, p, radius_nm):
        dlat = int(radius_nm / 60) + 1
        dlon = min(180, int(radius_nm / (60 * max(math.cos(math.radians(p[1])), 0.05))) + 1)
        cx, cy = math.floor(p[0]), math.floor(p[1])
        return any(great_circle_nm(p, q) <= radius_nm
                   for gy in range(cy - dlat, cy + dlat + 1)
                   for gx in range(cx - dlon, cx + dlon + 1)
                   for q in self.grid.get(((gx + 180) % 360 - 180, gy), ()))


def _unwrap(lon, ref):
    """lon shifted by whole turns to lie within 180 degrees of ref."""
    return ref + (lon - ref + 180) % 360 - 180


class DenialArea:
    """A circle {"type": "circle", "center": [lon, lat], "radius_nm": r} or a
    polygon {"type": "polygon", "points": [[lon, lat], ...]}.

    A circle is a true circle on the globe (great-circle radius). A polygon's
    sides are straight in longitude/latitude, which is how they look when
    drawn on an equirectangular map."""

    def __init__(self, spec):
        self.kind = spec["type"]
        if self.kind == "circle":
            self.c = tuple(spec["center"])
            self.r = float(spec["radius_nm"])
            dlat = self.r / 60
            cos = max(math.cos(math.radians(min(89.0, abs(self.c[1]) + dlat))), 0.01)
            dlon = min(180.0, self.r / (60 * cos))
            self.box = (self.c[0] - dlon, self.c[1] - dlat, self.c[0] + dlon, self.c[1] + dlat)
        else:
            pts, ref = [], spec["points"][0][0]
            for lon, lat in spec["points"]:
                ref = _unwrap(lon, ref)       # each vertex the short way from the last
                pts.append((ref, lat))
            self.poly = pts
            self.ref = sum(p[0] for p in pts) / len(pts)
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            self.box = (min(xs), min(ys), max(xs), max(ys))

    def cells(self):
        x0, y0, x1, y1 = self.box
        for cy in range(math.floor(y0) - 1, math.floor(y1) + 2):
            for cx in range(math.floor(x0) - 1, math.floor(x1) + 2):
                yield ((cx + 180) % 360 - 180, cy)

    def contains(self, p):
        if self.kind == "circle":
            return great_circle_nm(p, self.c) <= self.r
        x, y = _unwrap(p[0], self.ref), p[1]
        inside = False
        for (x1, y1), (x2, y2) in zip(self.poly, self.poly[1:] + self.poly[:1]):
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
        return inside

    def crosses(self, p, q):
        """Does the short straight piece p-q enter the area?"""
        if self.contains(p) or self.contains(q):
            return True
        if self.kind == "circle":
            kx = math.cos(math.radians(self.c[1])) * 60
            ax, ay = (_unwrap(p[0], self.c[0]) - self.c[0]) * kx, (p[1] - self.c[1]) * 60
            bx, by = (_unwrap(q[0], self.c[0]) - self.c[0]) * kx, (q[1] - self.c[1]) * 60
            dx, dy = bx - ax, by - ay
            den = dx * dx + dy * dy
            t = 0.0 if den == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / den))
            return math.hypot(ax + t * dx, ay + t * dy) <= self.r
        a = (_unwrap(p[0], self.ref), p[1])
        b = (_unwrap(q[0], a[0]), q[1])
        return any(_segments_cross(a, b, e1, e2)
                   for e1, e2 in zip(self.poly, self.poly[1:] + self.poly[:1]))


def _segments_cross(a, b, c, d):
    def side(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    return side(a, b, c) * side(a, b, d) <= 0 and side(c, d, a) * side(c, d, b) <= 0


def parse_wkt(wkt):
    return [tuple(map(float, p.split())) for p in wkt[len("LINESTRING("):-1].split(", ")]


class Route:
    __slots__ = ("nodes", "edges", "length_nm")

    def __init__(self, nodes, edges, length_nm):
        self.nodes, self.edges, self.length_nm = nodes, edges, length_nm


class SeaGraph:
    def __init__(self):
        self.coords = {}                 # node id -> (lon, lat)
        self.ports = {}                  # port id -> port row
        self.adj = defaultdict(list)     # node -> [(neighbour, edge id)]
        self.edges = {}                  # edge id -> (a, b, length_nm, passage, kind, geometry a->b)

    @classmethod
    def load(cls, data_dir=DATA):
        """Raises validate_data.DataError, listing every problem, if the data
        model fails its checks."""
        validate_data.check(data_dir)
        g = cls()
        with open(os.path.join(data_dir, "nodes", "ports.csv"), encoding="utf-8") as f:
            for r in csv.DictReader(f):
                g.ports[r["port_id"]] = r
                g.coords[r["port_id"]] = (float(r["lon"]), float(r["lat"]))
        with open(os.path.join(data_dir, "nodes", "sea_nodes.csv"), encoding="utf-8") as f:
            for r in csv.DictReader(f):
                g.coords[r["sea_node_id"]] = (float(r["lon"]), float(r["lat"]))
        for name, a, b, kind in (("sea_segments.csv", "from_sea_node_id", "to_sea_node_id", "segment"),
                                 ("port_to_sea_node.csv", "port_id", "sea_node_id", "port_access"),
                                 ("port_to_port.csv", "from_port_id", "to_port_id", "port_to_port")):
            with open(os.path.join(data_dir, "relationships", name), encoding="utf-8") as f:
                for i, r in enumerate(csv.DictReader(f)):
                    eid = f"{kind}:{i}"
                    g.edges[eid] = (r[a], r[b], float(r["length_nm"]), r["passage"] or None,
                                    kind, parse_wkt(r["geometry_wkt"]))
                    g.adj[r[a]].append((r[b], eid))
                    g.adj[r[b]].append((r[a], eid))
        return g

    # ------------------------------------------------------------------ search
    def _usable(self, eid, ends):
        a, _, _, _, kind, _ = self.edges[eid]
        return kind != "port_access" or a in ends

    def _route(self, nodes, edges):
        return Route(nodes, edges, sum(self.edges[e][2] for e in edges))

    def shortest(self, origin, goal, banned_nodes=frozenset(), banned_edges=frozenset(),
                 start=None, ends=None):
        """A* from `start` (default: origin) to goal. `ends` is the
        (origin, goal) of the whole route, which decides whose port access
        links are usable when this is a partial search inside another one."""
        start, ends = start or origin, ends or (origin, goal)
        target = self.coords[goal]
        dist, prev = {start: 0.0}, {}
        heap = [(great_circle_nm(self.coords[start], target), 0.0, start)]
        while heap:
            _, d, n = heapq.heappop(heap)
            if n == goal:
                nodes, edges = [goal], []
                while n != start:
                    n, eid = prev[n]
                    nodes.append(n)
                    edges.append(eid)
                return self._route(nodes[::-1], edges[::-1])
            if d > dist[n]:
                continue
            for m, eid in self.adj[n]:
                if m in banned_nodes or eid in banned_edges or not self._usable(eid, ends):
                    continue
                nd = d + self.edges[eid][2]
                if nd < dist.get(m, float("inf")):
                    dist[m], prev[m] = nd, (n, eid)
                    heapq.heappush(heap, (nd + great_circle_nm(self.coords[m], target), nd, m))
        return None

    def k_shortest(self, origin, goal, k=3, closed=frozenset()):
        """Yen's algorithm: the k shortest routes with no repeated node,
        never using a link in `closed`."""
        best = self.shortest(origin, goal, banned_edges=closed)
        if best is None:
            return []
        found, candidates, seen = [best], [], {tuple(best.edges)}
        while len(found) < k:
            last = found[-1]
            for i in range(len(last.nodes) - 1):
                spur, root_nodes, root_edges = last.nodes[i], last.nodes[:i + 1], last.edges[:i]
                banned_edges = closed | {r.edges[i] for r in found
                                         if len(r.edges) > i and r.nodes[:i + 1] == root_nodes}
                tail = self.shortest(origin, goal, frozenset(root_nodes[:-1]), banned_edges,
                                     start=spur, ends=(origin, goal))
                if tail is None:
                    continue
                edges = root_edges + tail.edges
                if tuple(edges) not in seen:
                    seen.add(tuple(edges))
                    r = self._route(root_nodes[:-1] + tail.nodes, edges)
                    heapq.heappush(candidates, (r.length_nm, len(seen), r))
            if not candidates:
                break
            found.append(heapq.heappop(candidates)[2])
        return found

    def _tree(self, source, ends, closed=frozenset()):
        """Dijkstra from source to every reachable node: {node: (dist, parent, edge)}."""
        tree, heap = {source: (0.0, None, None)}, [(0.0, source)]
        done = set()
        while heap:
            d, n = heapq.heappop(heap)
            if n in done:
                continue
            done.add(n)
            for m, eid in self.adj[n]:
                if eid in closed or not self._usable(eid, ends):
                    continue
                nd = d + self.edges[eid][2]
                if m not in tree or nd < tree[m][0]:
                    tree[m] = (nd, n, eid)
                    heapq.heappush(heap, (nd, m))
        return tree

    @staticmethod
    def _walk(tree, n):
        """Nodes and edges from n back to the tree's source."""
        nodes, edges = [n], []
        while tree[n][1] is not None:
            edges.append(tree[n][2])
            n = tree[n][1]
            nodes.append(n)
        return nodes, edges

    def distinct_routes(self, origin, goal, k=3, separation_nm=None, endpoint_free_nm=150.0,
                        max_stretch=3.0, closed=frozenset()):
        """Up to k routes, each the shortest route through waters the others
        don't use. Yen's alternatives on a mesh like this one are mostly the
        same route nudged around a junction; these are not.

        Via-point method: with the shortest distance from the origin to every
        point and from every point to the destination, the best route through
        any point X is known at once. Points are taken cheapest first; a route
        is kept when X lies at least separation_nm from every route kept so far
        (away from the two ports, which every route shares) and the route goes
        straight through X rather than out and back. Stops at k routes or once
        routes exceed max_stretch times the shortest.

        separation_nm defaults to 10% of the shortest route, at least 50 nm: 50 nm
        apart matters on a 1,000 nm route but is noise on a 5,000 nm crossing."""
        ends = (origin, goal)
        fwd, bwd = self._tree(origin, ends, closed), self._tree(goal, ends, closed)
        if goal not in fwd:
            return []
        best = fwd[goal][0]
        if separation_nm is None:
            separation_nm = max(50.0, 0.1 * best)
        o, d = self.coords[origin], self.coords[goal]
        kept, corridors = [], []
        for total, v in sorted((fwd[v][0] + bwd[v][0], v) for v in fwd if v in bwd):
            if total > max_stretch * best or len(kept) == k:
                break
            p = self.coords[v]
            if kept and (great_circle_nm(p, o) <= endpoint_free_nm or great_circle_nm(p, d) <= endpoint_free_nm
                         or any(c.near(p, separation_nm) for c in corridors)):
                continue
            head_nodes, head_edges = self._walk(fwd, v)
            tail_nodes, tail_edges = self._walk(bwd, v)
            nodes = head_nodes[::-1] + tail_nodes[1:]
            if len(set(nodes)) != len(nodes):
                continue                          # the two halves overlap: not a route
            route = self._route(nodes, head_edges[::-1] + tail_edges)
            if kept and not self._straight_through(route, v, separation_nm, ends, closed):
                continue
            kept.append(route)
            corridors.append(PointSet(densify(self.describe(route)["line"])))
        return kept

    def _straight_through(self, route, v, window_nm, ends, closed=frozenset()):
        """True unless the route makes a detour around v: the stretch from
        window_nm before v to window_nm after must itself be a shortest path."""
        lengths = [self.edges[e][2] for e in route.edges]
        i = route.nodes.index(v)
        a, run = i, 0.0
        while a > 0 and run < window_nm:
            a -= 1
            run += lengths[a]
        b, run = i, 0.0
        while b < len(route.nodes) - 1 and run < window_nm:
            run += lengths[b]
            b += 1
        stretch = sum(lengths[a:b])
        direct = self.shortest(route.nodes[a], route.nodes[b], banned_edges=closed,
                               start=route.nodes[a], ends=ends)
        return direct is not None and direct.length_nm >= stretch - max(1.0, 0.005 * stretch)

    # ----------------------------------------------------------- denial areas
    def links_in_areas(self, areas):
        """Every link whose real path enters any of the areas (DenialArea)."""
        if not hasattr(self, "_samples"):
            self._samples, self._sample_grid = {}, defaultdict(set)
            for eid, e in self.edges.items():
                pts = densify(e[5])
                self._samples[eid] = pts
                for q in pts:
                    self._sample_grid[(math.floor(q[0]), math.floor(q[1]))].add(eid)
        closed = set()
        for area in areas:
            candidates = set()
            for cell in area.cells():
                candidates |= self._sample_grid.get(cell, set())
            for eid in candidates - closed:
                pts = self._samples[eid]
                if any(area.crosses(p, q) for p, q in zip(pts, pts[1:])):
                    closed.add(eid)
        return frozenset(closed)

    def port_in_areas(self, port_id, areas):
        return any(a.contains(self.coords[port_id]) for a in areas)

    # ---------------------------------------------------------------- output
    def describe(self, route):
        """Route as the map needs it: one continuous line, oriented origin to
        destination, plus the named straits and canals it passes through."""
        line, passages = [], []
        for node, eid in zip(route.nodes, route.edges):
            a, b, _, passage, _, geom = self.edges[eid]
            pts = geom if a == node else geom[::-1]
            line.extend(pts if not line else pts[1:])
            if passage and passage not in passages:
                passages.append(passage)
        return {"length_nm": round(route.length_nm, 1), "passages": passages, "line": line,
                "hops": len(route.edges)}
