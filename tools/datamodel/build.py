"""
Builds the Neo4j import package in neo4j-datamodel/ from three public sources:

  * NGA World Port Index, Pub 150 (data/Phase 2 Data/UpdatedPub150.csv)
  * OurAirports airports + runways (data/Phase 2 Data/Ourairports.csv,
    data/raw/ourairports/runways.csv)
  * Eurostat MARNET maritime network, as bundled in searoute-ts
    (data/raw/marnet/marnet_plus_<res>.geojson; regenerate with
    tools/sea-lane-geometry/export-marnet.cjs)

    python tools/datamodel/build.py [20km|50km|100km] [output_dir]

Stdlib only. Every threshold and default is a named constant below and is
explained in neo4j-datamodel/ASSUMPTIONS.md; change one here, rebuild, and
update that file.
"""
import csv
import json
import math
import os
import shutil
import sys
from collections import Counter, defaultdict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC_PORTS = os.path.join(ROOT, "data", "Phase 2 Data", "UpdatedPub150.csv")
SRC_AIRPORTS = os.path.join(ROOT, "data", "Phase 2 Data", "Ourairports.csv")
SRC_RUNWAYS = os.path.join(ROOT, "data", "raw", "ourairports", "runways.csv")
MARNET_DIR = os.path.join(ROOT, "data", "raw", "marnet")

DEFAULT_RESOLUTION = "100km"
AIRFIELD_TYPES = {"large_airport", "medium_airport"}
MIN_PAVED_RUNWAY_FT = 8000
PAVED_SURFACE_TOKENS = ("ASP", "CON", "PEM", "BIT", "TAR", "PAVED", "MAC")
NODE_KEY_TOLERANCE_DEG = 1e-5          # same vertex-joining rule searoute uses
ACCESS_GOOD_NM = 10.0                  # port to the nearest point on its lane
ACCESS_POOR_NM = 50.0
EARTH_RADIUS_KM = 6371.0088            # mean radius, as turf (searoute) uses
KM_PER_NM = 1.852

# Passages Eurostat tags directly on network lines ("pass" property).
NATIVE_PASSAGES = {
    "suez": ("Suez Canal", "canal"), "panama": ("Panama Canal", "canal"),
    "kiel": ("Kiel Canal", "canal"), "corinth": ("Corinth Canal", "canal"),
    "gibraltar": ("Strait of Gibraltar", "strait"),
    "babelmandeb": ("Bab-el-Mandeb", "strait"),
    "malacca": ("Strait of Malacca", "strait"), "dover": ("Strait of Dover", "strait"),
    "bering": ("Bering Strait", "strait"),
    "magellan": ("Strait of Magellan", "strait"),
    "northwest": ("Northwest Passage", "arctic route"),
    "northeast": ("Northeast Passage", "arctic route"),
}
# Untagged in the network; searoute-ts locates them by these boxes
# (lib/restrictions.js PASSAGE_BBOXES). [min_lon, min_lat, max_lon, max_lat].
LIBRARY_BOX_PASSAGES = {
    "bosporus": ("Bosporus", "strait", [28.9, 40.95, 29.25, 41.35]),
    "ormuz": ("Strait of Hormuz", "strait", [55.9, 26.0, 57.0, 27.2]),
    "sunda": ("Sunda Strait", "strait", [105.4, -6.7, 106.3, -5.7]),
    "cape_horn": ("Cape Horn", "route", [-68.0, -57.5, -65.0, -55.5]),
}
# Indo-Pacific straits FALCON cares about that neither source tags. Boxes are
# approximate, drawn for this model, and should be checked against charts.
FALCON_BOX_PASSAGES = {
    "luzon": ("Luzon Strait", "strait", [120.0, 18.8, 122.5, 22.0]),
    "san_bernardino": ("San Bernardino Strait", "strait", [124.0, 12.3, 124.6, 12.8]),
    "taiwan": ("Taiwan Strait", "strait", [117.5, 22.5, 120.5, 25.5]),
    "korea": ("Korea Strait", "strait", [128.3, 33.6, 130.4, 35.3]),
    "lombok": ("Lombok Strait", "strait", [115.55, -9.0, 116.0, -8.2]),
    "makassar": ("Makassar Strait", "strait", [116.8, -4.0, 119.5, 1.5]),
}
# Canal draft ceilings, from canal authorities via searoute-ts lib/drafts.js.
CANAL_MAX_DRAFT_M = {"panama": 15.2, "suez": 20.1, "kiel": 7.0, "corinth": 7.3}


# ---------------------------------------------------------------- geometry
def haversine_km(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def nm_between(p, q):
    return haversine_km(p[0], p[1], q[0], q[1]) / KM_PER_NM


def norm_lon(lon):
    # One meridian, one value: 180 and -180 are the same place.
    while lon >= 180:
        lon -= 360
    while lon < -180:
        lon += 360
    return lon


def gc_interpolate(a, b, t):
    """Point at fraction t along the great circle from a to b, as (lon, lat)."""
    l1, p1, l2, p2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    d = 2 * math.asin(min(1.0, math.sqrt(
        math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2)))
    if d == 0:
        return a
    s1, s2 = math.sin((1 - t) * d) / math.sin(d), math.sin(t * d) / math.sin(d)
    x = s1 * math.cos(p1) * math.cos(l1) + s2 * math.cos(p2) * math.cos(l2)
    y = s1 * math.cos(p1) * math.sin(l1) + s2 * math.cos(p2) * math.sin(l2)
    z = s1 * math.sin(p1) + s2 * math.sin(p2)
    return (round(norm_lon(math.degrees(math.atan2(y, x))), 6),
            round(math.degrees(math.atan2(z, math.hypot(x, y))), 6))


def segment_hits_box(a, b, box):
    """Liang-Barsky clip of segment a-b against an axis-aligned lon/lat box."""
    x0, y0 = a
    x1, y1 = b
    if x1 - x0 > 180:
        x1 -= 360
    elif x1 - x0 < -180:
        x1 += 360
    xmin, ymin, xmax, ymax = box
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - xmin), (dx, xmax - x0), (-dy, y0 - ymin), (dy, ymax - y0)):
        if p == 0:
            if q < 0:
                return False
        else:
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 > t1:
                return False
    return True


# ----------------------------------------------------------------- sea network
class SeaNetwork:
    """Nodes are (lon, lat). A segment is a path of node indices: two at
    first, more once pass-through points are merged into it."""

    def __init__(self, path):
        with open(path, encoding="utf-8") as f:
            features = json.load(f)["features"]
        self.feature_count = len(features)
        self.nodes, node_of = [], {}

        def node_id(lon, lat):
            lon = norm_lon(lon)
            key = (round(lon / NODE_KEY_TOLERANCE_DEG), round(lat / NODE_KEY_TOLERANCE_DEG))
            if key not in node_of:
                node_of[key] = len(self.nodes)
                self.nodes.append((round(lon, 6), round(lat, 6)))
            return node_of[key]

        box_passages = {**LIBRARY_BOX_PASSAGES, **FALCON_BOX_PASSAGES}
        self.segments, seen = [], {}
        self.dropped_duplicate = 0
        for feat in features:
            native = (feat.get("properties") or {}).get("pass")
            coords = feat["geometry"]["coordinates"]
            for i in range(1, len(coords)):
                a, b = node_id(*coords[i - 1][:2]), node_id(*coords[i][:2])
                if a == b:
                    continue
                if native:
                    passage, method = native, "native"
                else:
                    passage = method = None
                    for name, (_, _, box) in box_passages.items():
                        if segment_hits_box(self.nodes[a], self.nodes[b], box):
                            passage = name
                            method = "bbox_library" if name in LIBRARY_BOX_PASSAGES else "bbox_falcon"
                            break
                pair = (min(a, b), max(a, b))
                if pair in seen:
                    self.dropped_duplicate += 1
                    kept = self.segments[seen[pair]]
                    if not kept["passage"] and passage:
                        kept["passage"], kept["tag_method"] = passage, method
                    continue
                seen[pair] = len(self.segments)
                self.segments.append({"path": [a, b], "length_nm": nm_between(self.nodes[a], self.nodes[b]),
                                      "passage": passage, "tag_method": method, "alive": True})
        self.raw_nodes, self.raw_segments = len(self.nodes), len(self.segments)

    def live(self):
        return [i for i, s in enumerate(self.segments) if s["alive"]]

    def _adjacency(self):
        adj = defaultdict(set)
        for i in self.live():
            p = self.segments[i]["path"]
            adj[p[0]].add(i)
            adj[p[-1]].add(i)
        return adj

    def prune_spurs(self, keep_segments):
        """Drop dead-end segments, repeatedly, unless a port sits beside one."""
        adj = self._adjacency()
        work = [n for n, segs in adj.items() if len(segs) == 1]
        removed = 0
        while work:
            n = work.pop()
            if len(adj[n]) != 1:
                continue
            si = next(iter(adj[n]))
            if si in keep_segments:
                continue
            adj[n].discard(si)
            s = self.segments[si]
            s["alive"] = False
            removed += 1
            other = s["path"][-1] if s["path"][0] == n else s["path"][0]
            adj[other].discard(si)
            if len(adj[other]) == 1:
                work.append(other)
        return removed

    def merge_pass_through(self):
        """Fold every point with exactly two segments of the same passage into
        one longer segment. Shortest paths are unchanged; the shape survives
        in the merged path."""
        adj = self._adjacency()
        merged = 0
        for n in list(adj):
            if len(adj[n]) != 2:
                continue
            i, j = adj[n]
            s1, s2 = self.segments[i], self.segments[j]
            if s1["passage"] != s2["passage"]:
                continue
            p1 = s1["path"] if s1["path"][-1] == n else s1["path"][::-1]
            p2 = s2["path"] if s2["path"][0] == n else s2["path"][::-1]
            if p1[0] == p2[-1]:
                continue                        # would close a loop on itself
            s1["path"] = p1 + p2[1:]
            s1["length_nm"] += s2["length_nm"]
            s2["alive"] = False
            adj[p2[-1]].discard(j)
            adj[p2[-1]].add(i)
            adj[n] = set()
            merged += 1
        return merged

    def components(self):
        parent = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i in self.live():
            p = self.segments[i]["path"]
            ra, rb = find(p[0]), find(p[-1])
            if ra != rb:
                parent[ra] = rb
        return {n: find(n) for n in parent}


class SegmentIndex:
    """1-degree grid over every straight piece of every live segment, for
    nearest-point-on-network lookups. Returns (segment, piece, t, nm).

    Distances use a local flat projection centred on the query point, which is
    accurate to well under 1% at the few-hundred-nm range a lookup ever spans.
    """

    def __init__(self, net):
        self.net = net
        self.cells = defaultdict(list)
        for si in net.live():
            path = net.segments[si]["path"]
            for k in range(len(path) - 1):
                (x0, y0), (x1, y1) = net.nodes[path[k]], net.nodes[path[k + 1]]
                if x1 - x0 > 180:
                    x1 -= 360
                elif x1 - x0 < -180:
                    x1 += 360
                for cy in range(math.floor(min(y0, y1)), math.floor(max(y0, y1)) + 1):
                    for cxr in range(math.floor(min(x0, x1)), math.floor(max(x0, x1)) + 1):
                        self.cells[((cxr + 180) % 360 - 180, cy)].append((si, k))

    def _project(self, lon, lat, si, k):
        kx = math.cos(math.radians(lat)) * 60
        path = self.net.segments[si]["path"]

        def xy(q):
            dl = q[0] - lon
            if dl > 180:
                dl -= 360
            elif dl < -180:
                dl += 360
            return dl * kx, (q[1] - lat) * 60

        (ax, ay), (bx, by) = xy(self.net.nodes[path[k]]), xy(self.net.nodes[path[k + 1]])
        dx, dy = bx - ax, by - ay
        den = dx * dx + dy * dy
        t = 0.0 if den == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / den))
        return t, math.hypot(ax + t * dx, ay + t * dy)

    def _scan(self, lon, lat, dlat, dlon):
        best, seen = (None, 0, 0.0, float("inf")), set()
        for cy in range(math.floor(lat - dlat), math.floor(lat + dlat) + 1):
            for cxr in range(math.floor(lon - dlon), math.floor(lon + dlon) + 1):
                for piece in self.cells.get(((cxr + 180) % 360 - 180, cy), ()):
                    if piece not in seen:
                        seen.add(piece)
                        t, nm = self._project(lon, lat, *piece)
                        if nm < best[3]:
                            best = (piece[0], piece[1], t, nm)
        return best

    def nearest(self, lon, lat):
        span = 1.0
        while True:
            best = self._scan(lon, lat, span, span)
            if best[0] is not None or span >= 180:
                break
            span *= 2
        # Re-scan a window guaranteed to hold anything closer than the best.
        km = best[3] * KM_PER_NM
        dlat = km / 111.0 + 1
        cos = max(math.cos(math.radians(min(89.0, abs(lat) + dlat))), 0.01)
        return self._scan(lon, lat, dlat, min(180.0, km / (111.32 * cos) + 1))


def port_access(net, ports):
    """Connect each port to the lane it sits beside, without adding any
    network node: an access link to each end of that segment, and a link to
    each neighbouring port on the same segment. Lengths run along the lane
    to the point nearest the port, then straight in to the port."""
    index = SegmentIndex(net)
    by_segment = defaultdict(list)
    for p in ports:
        si, k, t, _ = index.nearest(p["lon"], p["lat"])
        path = net.segments[si]["path"]
        a, b = net.nodes[path[k]], net.nodes[path[k + 1]]
        proj = gc_interpolate(a, b, t)
        along = sum(nm_between(net.nodes[path[j]], net.nodes[path[j + 1]]) for j in range(k))
        along += nm_between(a, proj)
        p["_attach"] = {"seg": si, "piece": k, "proj": proj, "along": along,
                        "access": nm_between((p["lon"], p["lat"]), proj)}
        by_segment[si].append(p)

    to_node, to_port = [], []
    for si, group in by_segment.items():
        s = net.segments[si]
        path, total = s["path"], s["length_nm"]
        pts = [net.nodes[n] for n in path]
        group.sort(key=lambda p: p["_attach"]["along"])
        for p in group:
            at, here = p["_attach"], (p["lon"], p["lat"])
            k = at["piece"]
            to_node.append({"port": p, "node": path[0], "seg": si, "length_nm": at["along"] + at["access"],
                            "geometry": [here, at["proj"]] + pts[k::-1],
                            "passage": s["passage"], "tag_method": s["tag_method"]})
            to_node.append({"port": p, "node": path[-1], "seg": si,
                            "length_nm": (total - at["along"]) + at["access"],
                            "geometry": [here, at["proj"]] + pts[k + 1:],
                            "passage": s["passage"], "tag_method": s["tag_method"]})
        for p, q in zip(group, group[1:]):
            a1, a2 = p["_attach"], q["_attach"]
            to_port.append({"from": p, "to": q, "seg": si,
                            "length_nm": (a2["along"] - a1["along"]) + a1["access"] + a2["access"],
                            "geometry": [(p["lon"], p["lat"]), a1["proj"]]
                            + pts[a1["piece"] + 1:a2["piece"] + 1] + [a2["proj"], (q["lon"], q["lat"])],
                            "passage": s["passage"], "tag_method": s["tag_method"]})
    return to_node, to_port


# ----------------------------------------------------------------------- ports
def num(v, zero_is_unknown=True):
    v = (v or "").strip()
    if not v:
        return None
    x = float(v)
    if zero_is_unknown and x == 0:
        return None
    return x


def yes_no(v):
    return {"yes": True, "no": False}.get((v or "").strip().lower())


def text(v):
    v = (v or "").strip()
    return None if v in ("", "Unknown") else v


PORT_FLAGS = {
    "first_port_of_entry": "First Port of Entry",
    "pilotage_compulsory": "Pilotage - Compulsory",
    "tugs_assistance": "Tugs - Assistance",
    "fac_wharves": "Facilities - Wharves",
    "fac_anchorage": "Facilities - Anchorage",
    "fac_roro": "Facilities - Ro-Ro",
    "fac_container": "Facilities - Container",
    "fac_breakbulk": "Facilities - Breakbulk",
    "fac_solid_bulk": "Facilities - Solid Bulk",
    "fac_liquid_bulk": "Facilities - Liquid Bulk",
    "fac_oil_terminal": "Facilities - Oil Terminal",
    "fac_lng_terminal": "Facilities - LNG Terminal",
    "cranes_fixed": "Cranes - Fixed",
    "cranes_mobile": "Cranes - Mobile",
    "cranes_floating": "Cranes - Floating",
    "cranes_container": "Cranes Container",
    "lifts_100t_plus": "Lifts - 100+ Tons",
    "supply_fuel_oil": "Supplies - Fuel Oil",
    "supply_diesel": "Supplies - Diesel Oil",
    "supply_aviation_fuel": "Supplies - Aviation Fuel",
    "supply_potable_water": "Supplies - Potable Water",
    "supply_provisions": "Supplies - Provisions",
}
PORT_MEASURES = {
    "tidal_range_m": "Tidal Range (m)",
    "entrance_width_m": "Entrance Width (m)",
    "channel_depth_m": "Channel Depth (m)",
    "anchorage_depth_m": "Anchorage Depth (m)",
    "cargo_pier_depth_m": "Cargo Pier Depth (m)",
    "oil_terminal_depth_m": "Oil Terminal Depth (m)",
    "lng_terminal_depth_m": "Liquified Natural Gas Terminal Depth (m)",
    "max_vessel_length_m": "Maximum Vessel Length (m)",
    "max_vessel_beam_m": "Maximum Vessel Beam (m)",
    "max_vessel_draft_m": "Maximum Vessel Draft (m)",
}


def read_ports():
    """NGA reuses a few index numbers: the same port listed twice (same
    coordinates, spelling variants) is dropped; two different ports sharing a
    number keep the number and get the record's OID appended to their id."""
    rows, by_id, issues = [], {}, []
    with open(SRC_PORTS, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            wpi = int(float(r["World Port Index Number"]))
            port_id = f"WPI-{wpi}"
            lat, lon = float(r["Latitude"]), float(r["Longitude"])
            if port_id in by_id:
                first = by_id[port_id]
                if abs(first["lat"] - lat) < 0.01 and abs(first["lon"] - norm_lon(lon)) < 0.01:
                    issues.append(f"{port_id}: '{r['Main Port Name'].strip()}' duplicates "
                                  f"'{first['name']}' (same position) -- dropped")
                    continue
                port_id = f"WPI-{wpi}-OID{int(float(r['OID_']))}"
                issues.append(f"WPI-{wpi}: shared by '{first['name']}' and "
                              f"'{r['Main Port Name'].strip()}' -- second loaded as {port_id}")
            row = {
                "port_id": port_id, "wpi_number": wpi,
                "name": r["Main Port Name"].strip(),
                "alternate_name": text(r["Alternate Port Name"]),
                "unlocode": text(r["UN/LOCODE"]),
                # NGA's "Country Code" column holds country names, not ISO codes.
                "country": text(r["Country Code"]),
                "region_name": text(r["Region Name"]),
                "water_body": text(r["World Water Body"]),
                "lat": round(float(r["Latitude"]), 6),
                "lon": round(norm_lon(float(r["Longitude"])), 6),
                "harbor_size": text(r["Harbor Size"]),
                "harbor_type": text(r["Harbor Type"]),
                "harbor_use": text(r["Harbor Use"]),
                "shelter": text(r["Shelter Afforded"]),
                "repairs": text(r["Repairs"]),
                "dry_dock": text(r["Dry Dock"]),
                "railway": text(r["Railway"]),
                "sailing_directions": text(r["Sailing Direction or Publication"]),
            }
            for k, col in PORT_MEASURES.items():
                row[k] = num(r[col])
            chan, pier = row["channel_depth_m"], row["cargo_pier_depth_m"]
            known = [d for d in (chan, pier) if d is not None]
            row["controlling_depth_m"] = min(known) if known else None
            row["controlling_depth_basis"] = (
                "min(channel, cargo pier)" if len(known) == 2
                else "channel only" if chan is not None
                else "cargo pier only" if pier is not None else None)
            for k, col in PORT_FLAGS.items():
                row[k] = yes_no(r[col])
            rows.append(row)
            by_id[port_id] = row
    return rows, issues


# -------------------------------------------------------------------- airfields
def is_paved(surface):
    s = (surface or "").strip().upper()
    return "UNPAVED" not in s and any(t in s for t in PAVED_SURFACE_TOKENS)


def read_airfields():
    runways = defaultdict(list)
    with open(SRC_RUNWAYS, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            runways[r["airport_ref"]].append(r)

    fields, rws = [], []
    with open(SRC_AIRPORTS, newline="", encoding="utf-8") as f:
        for a in csv.DictReader(f):
            if a["type"] not in AIRFIELD_TYPES:
                continue
            open_paved = [r for r in runways.get(a["id"], [])
                          if r["closed"] != "1" and r["length_ft"].isdigit() and is_paved(r["surface"])]
            if not open_paved:
                continue
            best = max(open_paved, key=lambda r: int(r["length_ft"]))
            if int(best["length_ft"]) < MIN_PAVED_RUNWAY_FT:
                continue
            fields.append({
                "airfield_id": a["ident"], "ourairports_id": int(a["id"]),
                "icao_code": text(a["icao_code"]), "iata_code": text(a["iata_code"]),
                "gps_code": text(a["gps_code"]),
                "name": a["name"].strip(), "airport_type": a["type"],
                "country_code": text(a["iso_country"]), "region_code": text(a["iso_region"]),
                "municipality": text(a["municipality"]),
                "lat": round(float(a["latitude_deg"]), 6),
                "lon": round(norm_lon(float(a["longitude_deg"])), 6),
                "elevation_ft": num(a["elevation_ft"], zero_is_unknown=False),
                "scheduled_service": yes_no(a["scheduled_service"]),
                "longest_paved_runway_ft": int(best["length_ft"]),
                "longest_paved_runway_width_ft": num(best["width_ft"]),
                "longest_paved_runway_surface": best["surface"].strip(),
                "open_paved_runway_count": len(open_paved),
            })
            for r in runways[a["id"]]:
                rws.append({
                    "runway_id": f"RWY-{r['id']}", "airfield_id": a["ident"],
                    "designation": "/".join(x for x in (r["le_ident"].strip(), r["he_ident"].strip()) if x),
                    "length_ft": num(r["length_ft"]), "width_ft": num(r["width_ft"]),
                    "surface": r["surface"].strip() or None,
                    "paved": is_paved(r["surface"]),
                    "lighted": r["lighted"] == "1", "closed": r["closed"] == "1",
                })
    return fields, rws


# ----------------------------------------------------------------------- output
def write_csv(out, rel_path, rows, columns):
    path = os.path.join(out, rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    def cell(v):
        if v is None:
            return ""
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, float):
            return repr(round(v, 6))
        return v

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(columns)
        for r in rows:
            w.writerow([cell(r.get(c)) for c in columns])
    return len(rows)


def wkt(points):
    return "LINESTRING(" + ", ".join(f"{lon} {lat}" for lon, lat in points) + ")"


def set_clause(var, rows, columns, skip=()):
    """Typed SET lines for LOAD CSV, inferred from the Python values written.
    LOAD CSV reads an empty field as null, and toFloat/toInteger/toBoolean
    pass null through, so an unknown value never becomes a property."""
    lines = []
    for c in columns:
        if c in skip:
            continue
        types = {type(r.get(c)) for r in rows if r.get(c) is not None}
        conv = ("toBoolean" if types == {bool} else "toInteger" if types == {int}
                else "toFloat" if types and types <= {int, float} else None)
        lines.append(f"  {var}.{c} = {conv}(row.{c})" if conv else f"  {var}.{c} = row.{c}")
    return "SET\n" + ",\n".join(lines)


def write_load_cypher(out, ports, airfields, runways):
    port_cols, air_cols, rwy_cols = list(ports[0]), list(airfields[0]), list(runways[0])
    point = "point({longitude: toFloat(row.lon), latitude: toFloat(row.lat)})"
    rel_set = ("length_nm: toFloat(row.length_nm), segment_id: row.segment_id, passage: row.passage,\n"
               "  tag_method: row.tag_method, geometry_wkt: row.geometry_wkt")
    cypher = f"""// Generated by tools/datamodel/build.py -- do not edit by hand; rebuild instead.
// Copy the nodes/ and relationships/ folders into the Neo4j import directory,
// then run this file (cypher-shell -f load.cypher, or paste into Neo4j Browser).
// Loads into an empty database; run it once.

CREATE CONSTRAINT port_id IF NOT EXISTS FOR (n:Port) REQUIRE n.port_id IS UNIQUE;
CREATE CONSTRAINT airfield_id IF NOT EXISTS FOR (n:Airfield) REQUIRE n.airfield_id IS UNIQUE;
CREATE CONSTRAINT runway_id IF NOT EXISTS FOR (n:Runway) REQUIRE n.runway_id IS UNIQUE;
CREATE CONSTRAINT sea_node_id IF NOT EXISTS FOR (n:SeaNode) REQUIRE n.sea_node_id IS UNIQUE;
CREATE CONSTRAINT passage_id IF NOT EXISTS FOR (n:Passage) REQUIRE n.passage_id IS UNIQUE;
CREATE POINT INDEX port_location IF NOT EXISTS FOR (n:Port) ON (n.location);
CREATE POINT INDEX airfield_location IF NOT EXISTS FOR (n:Airfield) ON (n.location);
CREATE INDEX sea_segment_passage IF NOT EXISTS FOR ()-[r:SEA_SEGMENT]-() ON (r.passage);
CREATE INDEX sea_segment_id IF NOT EXISTS FOR ()-[r:SEA_SEGMENT]-() ON (r.segment_id);
CREATE INDEX sea_access_segment IF NOT EXISTS FOR ()-[r:SEA_ACCESS]-() ON (r.segment_id);

// ---- nodes
LOAD CSV WITH HEADERS FROM 'file:///nodes/ports.csv' AS row
CREATE (p:Port {{port_id: row.port_id}})
{set_clause("p", ports, port_cols, skip=("port_id",))},
  p.location = {point};

LOAD CSV WITH HEADERS FROM 'file:///nodes/airfields.csv' AS row
CREATE (a:Airfield {{airfield_id: row.airfield_id}})
{set_clause("a", airfields, air_cols, skip=("airfield_id",))},
  a.location = {point};

LOAD CSV WITH HEADERS FROM 'file:///nodes/runways.csv' AS row
MATCH (a:Airfield {{airfield_id: row.airfield_id}})
CREATE (a)-[:HAS_RUNWAY]->(r:Runway {{runway_id: row.runway_id}})
{set_clause("r", runways, rwy_cols, skip=("runway_id", "airfield_id"))};

LOAD CSV WITH HEADERS FROM 'file:///nodes/sea_nodes.csv' AS row
CREATE (:SeaNode {{sea_node_id: row.sea_node_id, lon: toFloat(row.lon), lat: toFloat(row.lat),
  location: {point}}});

LOAD CSV WITH HEADERS FROM 'file:///nodes/passages.csv' AS row
CREATE (:Passage {{passage_id: row.passage_id, name: row.name, kind: row.kind,
  tag_method: row.tag_method, max_draft_m: toFloat(row.max_draft_m), bbox: row.bbox,
  segment_count: toInteger(row.segment_count)}});

// ---- sea network: stored one way, traversed both ways
LOAD CSV WITH HEADERS FROM 'file:///relationships/sea_segments.csv' AS row
MATCH (a:SeaNode {{sea_node_id: row.from_sea_node_id}}), (b:SeaNode {{sea_node_id: row.to_sea_node_id}})
CREATE (a)-[:SEA_SEGMENT {{{rel_set}}}]->(b);

LOAD CSV WITH HEADERS FROM 'file:///relationships/port_to_sea_node.csv' AS row
MATCH (p:Port {{port_id: row.port_id}}), (s:SeaNode {{sea_node_id: row.sea_node_id}})
CREATE (p)-[:SEA_ACCESS {{{rel_set}}}]->(s);

LOAD CSV WITH HEADERS FROM 'file:///relationships/port_to_port.csv' AS row
MATCH (a:Port {{port_id: row.from_port_id}}), (b:Port {{port_id: row.to_port_id}})
CREATE (a)-[:SEA_ACCESS {{{rel_set}}}]->(b);

// ---- check: compare with build-report.json
MATCH (n) RETURN labels(n)[0] AS label, count(*) AS nodes ORDER BY label;
MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS relationships ORDER BY type;
"""
    with open(os.path.join(out, "load.cypher"), "w", encoding="utf-8") as f:
        f.write(cypher)


RAW_20KM_NODES = 16219


def main():
    res = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_RESOLUTION
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "neo4j-datamodel")
    net = SeaNetwork(os.path.join(MARNET_DIR, f"marnet_plus_{res}.geojson"))
    ports, port_issues = read_ports()

    # A port's lane must survive spur pruning, so find each port's nearest
    # segment on the raw network first. Merging never removes a segment's
    # pieces, only joins them, so the lookup after merging lands on the same piece.
    raw_index = SegmentIndex(net)
    port_segments = {raw_index.nearest(p["lon"], p["lat"])[0] for p in ports}
    spurs = net.prune_spurs(port_segments)
    merged = net.merge_pass_through()
    to_node, to_port = port_access(net, ports)

    comp = net.components()
    main_comp = Counter(comp.values()).most_common(1)[0][0]
    live_idx = net.live()
    live = [net.segments[i] for i in live_idx]
    seg_id = {si: f"SEG-{k}" for k, si in enumerate(live_idx)}
    used = sorted({n for s in live for n in (s["path"][0], s["path"][-1])})
    sea_id = {n: f"SN-{k}" for k, n in enumerate(used)}

    for p in ports:
        at = p.pop("_attach")
        p["sea_access_nm"] = round(at["access"], 2)
        p["access_quality"] = ("good" if at["access"] <= ACCESS_GOOD_NM
                               else "approximate" if at["access"] <= ACCESS_POOR_NM else "poor")
        p["reaches_open_ocean"] = comp.get(net.segments[at["seg"]]["path"][0]) == main_comp

    airfields, runways = read_airfields()

    tagged = Counter(s["passage"] for s in live if s["passage"])
    passages = []
    for pid, (name, kind) in NATIVE_PASSAGES.items():
        passages.append({"passage_id": pid, "name": name, "kind": kind, "tag_method": "native",
                         "max_draft_m": CANAL_MAX_DRAFT_M.get(pid), "segment_count": tagged.get(pid, 0)})
    for src, method in ((LIBRARY_BOX_PASSAGES, "bbox_library"), (FALCON_BOX_PASSAGES, "bbox_falcon")):
        for pid, (name, kind, box) in src.items():
            passages.append({"passage_id": pid, "name": name, "kind": kind, "tag_method": method,
                             "bbox": ";".join(str(x) for x in box), "segment_count": tagged.get(pid, 0)})

    rel_cols = ["length_nm", "passage", "tag_method", "geometry_wkt"]
    counts = {
        "nodes/ports.csv": write_csv(out, "nodes/ports.csv", ports, list(ports[0])),
        "nodes/airfields.csv": write_csv(out, "nodes/airfields.csv", airfields, list(airfields[0])),
        "nodes/runways.csv": write_csv(out, "nodes/runways.csv", runways, list(runways[0])),
        "nodes/sea_nodes.csv": write_csv(
            out, "nodes/sea_nodes.csv",
            [{"sea_node_id": sea_id[n], "lon": net.nodes[n][0], "lat": net.nodes[n][1]} for n in used],
            ["sea_node_id", "lon", "lat"]),
        "nodes/passages.csv": write_csv(
            out, "nodes/passages.csv", passages,
            ["passage_id", "name", "kind", "tag_method", "max_draft_m", "bbox", "segment_count"]),
        "relationships/sea_segments.csv": write_csv(
            out, "relationships/sea_segments.csv",
            [{"segment_id": f"SEG-{k}", "from_sea_node_id": sea_id[s["path"][0]],
              "to_sea_node_id": sea_id[s["path"][-1]], "length_nm": round(s["length_nm"], 3),
              "passage": s["passage"], "tag_method": s["tag_method"],
              "geometry_wkt": wkt(net.nodes[n] for n in s["path"])}
             for k, s in enumerate(live)],
            ["segment_id", "from_sea_node_id", "to_sea_node_id"] + rel_cols),
        "relationships/port_to_sea_node.csv": write_csv(
            out, "relationships/port_to_sea_node.csv",
            [{"port_id": r["port"]["port_id"], "sea_node_id": sea_id[r["node"]],
              "segment_id": seg_id[r["seg"]], "length_nm": round(r["length_nm"], 3),
              "passage": r["passage"], "tag_method": r["tag_method"],
              "geometry_wkt": wkt(r["geometry"])} for r in to_node],
            ["port_id", "sea_node_id", "segment_id"] + rel_cols),
        "relationships/port_to_port.csv": write_csv(
            out, "relationships/port_to_port.csv",
            [{"from_port_id": r["from"]["port_id"], "to_port_id": r["to"]["port_id"],
              "segment_id": seg_id[r["seg"]], "length_nm": round(r["length_nm"], 3),
              "passage": r["passage"], "tag_method": r["tag_method"],
              "geometry_wkt": wkt(r["geometry"])} for r in to_port],
            ["from_port_id", "to_port_id", "segment_id"] + rel_cols),
    }

    report = {
        "resolution": res,
        "rows_written": counts,
        "network": {
            "marnet_features": net.feature_count, "raw_nodes": net.raw_nodes,
            "raw_segments": net.raw_segments, "dropped_duplicate_segments": net.dropped_duplicate,
            "dead_end_segments_pruned": spurs, "pass_through_points_merged": merged,
            "final_nodes": len(used), "final_segments": len(live),
            "node_reduction_vs_raw_20km_pct": round(100 * (1 - len(used) / RAW_20KM_NODES), 1),
            "components": len({comp[n] for n in used}),
            "segments_by_passage": dict(tagged),
        },
        "ports": {
            "total": len(ports),
            "source_data_issues": port_issues,
            "by_access_quality": dict(Counter(p["access_quality"] for p in ports)),
            "poor_access_river_ports": sum(1 for p in ports if p["access_quality"] == "poor"
                                           and (p["harbor_type"] or "").startswith("River")),
            "not_reaching_open_ocean": sum(1 for p in ports if not p["reaches_open_ocean"]),
            "by_harbor_size": dict(Counter(p["harbor_size"] or "(blank)" for p in ports)),
            "controlling_depth_known": sum(1 for p in ports if p["controlling_depth_m"] is not None),
            "max_vessel_draft_known": sum(1 for p in ports if p["max_vessel_draft_m"] is not None),
        },
        "airfields": {"total": len(airfields),
                      "by_type": dict(Counter(a["airport_type"] for a in airfields)),
                      "runways": len(runways)},
        "settings": {"MIN_PAVED_RUNWAY_FT": MIN_PAVED_RUNWAY_FT,
                     "AIRFIELD_TYPES": sorted(AIRFIELD_TYPES),
                     "ACCESS_GOOD_NM": ACCESS_GOOD_NM, "ACCESS_POOR_NM": ACCESS_POOR_NM,
                     "NODE_KEY_TOLERANCE_DEG": NODE_KEY_TOLERANCE_DEG},
    }
    with open(os.path.join(out, "build-report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    write_load_cypher(out, ports, airfields, runways)
    shutil.copy(os.path.join(os.path.dirname(__file__), "check_routes.py"), out)
    n = report["network"]
    print(f"{res}: {n['final_nodes']} sea nodes ({n['node_reduction_vs_raw_20km_pct']}% fewer than "
          f"{RAW_20KM_NODES}), {n['final_segments']} segments, {n['components']} component(s); "
          f"pruned {n['dead_end_segments_pruned']} spurs, merged {n['pass_through_points_merged']}; "
          f"access {report['ports']['by_access_quality']}, "
          f"port links {counts['relationships/port_to_sea_node.csv']}+{counts['relationships/port_to_port.csv']}")


if __name__ == "__main__":
    main()
