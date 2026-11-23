# FALCON data model — Neo4j import package

A worldwide graph of seaports, airfields and the shipping-lane network that
connects the ports. It is built from public, citable sources (NGA, OurAirports,
Eurostat), and every filter, default and known gap is listed in
[ASSUMPTIONS.md](ASSUMPTIONS.md).

| Node | Count | Source |
|---|---|---|
| `Port` | 3,806 | NGA World Port Index (Pub 150) |
| `Airfield` | 2,285 | OurAirports, filtered to airfields with an open paved runway of 8,000 ft or more |
| `Runway` | 3,640 | OurAirports, every runway at those airfields |
| `SeaNode` | 4,525 | Eurostat MARNET maritime network, 100 km resolution |
| `Passage` | 22 | Named canals and straits that can be closed |

| Relationship | Count | Meaning |
|---|---|---|
| `(:SeaNode)-[:SEA_SEGMENT]->(:SeaNode)` | 9,686 | A stretch of shipping lane |
| `(:Port)-[:SEA_ACCESS]->(:SeaNode)` | 7,612 | A port's link onto the lane it sits beside |
| `(:Port)-[:SEA_ACCESS]->(:Port)` | 2,507 | Neighbouring ports on the same lane |
| `(:Airfield)-[:HAS_RUNWAY]->(:Runway)` | 3,640 | |

## Loading

1. Copy the `nodes/` and `relationships/` folders into the Neo4j import directory.
2. Run `load.cypher` against an empty database:
   `cypher-shell -u neo4j -p <password> -f load.cypher`, or paste it into Neo4j Browser.
3. The last two queries print node and relationship counts; compare them with the tables above.

Tested on Neo4j 5.26 Community; the load takes about 5 seconds. Weighted
shortest-path queries need the APOC Core plugin (it ships inside the Neo4j
Docker image) or the Graph Data Science library.

## Two kinds of node: places and sea junctions

`Port` and `Airfield` are **places**: where cargo starts and ends. They are the
only nodes a planner works with.

`SeaNode` is a **junction in the shipping lanes**, a point in open water where
lanes meet or bend around land. It is never a destination. Think of a road map:
cities are where you go, intersections are where roads join. A sea route runs
port, then sea junctions, then port, and the junctions only shape the path in
between. Queries that list places should match `Port` and `Airfield` only.

## Sea routing

Relationships are stored in one direction but mean "both ways". Route over
`SEA_SEGMENT` and `SEA_ACCESS`, ignoring direction, weighted by `length_nm`.

```cypher
MATCH (a:Port {name: 'San Diego'}), (b:Port {name: 'Apra Harbor'})
CALL apoc.algo.dijkstra(a, b, 'SEA_SEGMENT|SEA_ACCESS', 'length_nm') YIELD path, weight
RETURN round(weight) AS nm, [n IN nodes(path) WHERE n:Port | n.name] AS ports;
// 5,539 nm
```

**Closures.** Every sea relationship carries the same three properties, so one rule
covers every kind of closure:

| To close | Drop every `SEA_SEGMENT` or `SEA_ACCESS` relationship where |
|---|---|
| A canal or strait | `passage` is that passage's `passage_id` (for example `malacca`, `luzon`) |
| One stretch of lane | `segment_id` matches, on `SEA_SEGMENT` and on any `SEA_ACCESS` that runs along it |
| An area of denial | `geometry_wkt` passes through the area |
| A port | Drop the `Port` node's own `SEA_ACCESS` links |

A port's access links carry the `passage` and `segment_id` of the lane they run
along. That means closing a strait also closes access along it, and a route can't
slip around a closure by hopping through a port.

`apoc.algo.dijkstra` cannot filter by property. For closures, either load the sea
network into the application and run Dijkstra there (about 8,300 port and sea
nodes and 19,800 links, which fit easily in memory), or use a filtered Graph Data Science
projection. `check_routes.py` (included; Python 3, no libraries) does the first in
about 60 lines and serves as the reference answer:

| Route | Open | Closed | Result |
|---|---|---|---|
| Singapore to Colombo | 1,644 nm | Malacca | 2,390 nm, via Sunda Strait |
| Singapore to Colombo | | Malacca + Sunda | 3,552 nm, via Lombok Strait |
| Busan to Subic Bay | 1,450 nm | Luzon | 1,450 nm (uses Taiwan Strait) |
| Busan to Subic Bay | | Luzon + Taiwan | 1,891 nm |

**Ships and ports.** Check a platform's draft against `Port.max_vessel_draft_m`
(NGA's own figure, known for 944 ports), falling back to
`Port.controlling_depth_m` with an under-keel margin. Canal limits are on
`Passage.max_draft_m` (Panama 15.2 m, Suez 20.1 m).

## Drawing on a map

Places have `lat` and `lon` (and a `location` point). Every sea link has its
real shape in `geometry_wkt`, as `LINESTRING(lon lat, lon lat, ...)` in
longitude-first order. To draw a route, join the shapes of the links along the
path, with two details to handle:

- **Direction.** A link's shape runs from its stored start node to its end node.
  When a route uses the link the other way round, reverse its coordinates
  before joining.
- **The 180° meridian.** 39 links cross the date line. Their coordinates jump
  from about +180 to -180, for example `LINESTRING(178.9 -20.27, -180 -20)`.
  A Pacific map should handle the wrap-around, by unwrapping longitudes or by
  using a projection centred on the Pacific. Otherwise the line will streak
  across the whole map.

Sea junctions (`SeaNode`) shape route lines; they are not places and should not
be drawn as markers.

## Air routing

There are no air links. Any two airfields are connected, and the distance is the
great-circle distance, computed when it's needed and filtered by aircraft range:

```cypher
MATCH (a:Airfield {airfield_id: 'PGUA'}), (b:Airfield)
WHERE b <> a AND point.distance(a.location, b.location) / 1852 <= 1500
RETURN b.name, round(point.distance(a.location, b.location) / 1852) AS nm;
// 61 airfields within 1,500 nm of Andersen AFB
```

Runway feasibility for a given aircraft: compare `Airfield.longest_paved_runway_ft`
(or the individual `Runway` nodes) with that aircraft's minimum.

## Files

| File | Becomes |
|---|---|
| `nodes/ports.csv` | `Port`, one per NGA port: 55 columns, plus a `location` point |
| `nodes/airfields.csv` | `Airfield` |
| `nodes/runways.csv` | `Runway`, linked by `HAS_RUNWAY` |
| `nodes/sea_nodes.csv` | `SeaNode` |
| `nodes/passages.csv` | `Passage`, a reference list of closable chokepoints |
| `relationships/sea_segments.csv` | `SEA_SEGMENT` |
| `relationships/port_to_sea_node.csv` | `SEA_ACCESS`, port to lane |
| `relationships/port_to_port.csv` | `SEA_ACCESS`, port to neighbouring port |
| `load.cypher` | Constraints, indexes and the load |
| `check_routes.py` | Reference sea routing over the CSVs, with closures |
| `build-report.json` | Counts, settings and source-data issues from the last build |

`geometry_wkt` on each sea relationship is the lane's real shape as WKT
(`LINESTRING(lon lat, ...)`). Draw it on the map, and use it for area-denial tests.
The `location` point property on `Port`, `Airfield` and `SeaNode` supports
`point.distance` and spatial indexes.

This folder is generated by a build script, apart from this file,
START_HERE.md and ASSUMPTIONS.md. Send change requests (a filter, a field, a
fix) back to the package owner rather than editing the CSVs by hand, so the next
rebuild doesn't undo them.
