# Routing rules

The exact rules the reference version follows. A migrated version must follow
the same rules; `acceptance/check_server.py` checks that it does. Where this
page and the Python code disagree, the code (`sea_routing.py`, `server.py`)
is the reference, and this page has a bug: please report it.

## 1. The network

| Node | File | Count |
|---|---|---|
| Port | `nodes/ports.csv` | 3,806 |
| SeaNode, a junction on a shipping lane (never shown as a place) | `nodes/sea_nodes.csv` | 4,525 |

| Link | File | In Neo4j | Count |
|---|---|---|---|
| Lane segment, SeaNode to SeaNode | `relationships/sea_segments.csv` | `SEA_SEGMENT` | 9,686 |
| Port onto its lane, Port to SeaNode | `relationships/port_to_sea_node.csv` | `SEA_ACCESS` | 7,612 |
| Port to a neighbouring port on the same lane | `relationships/port_to_port.csv` | `SEA_ACCESS` | 2,507 |

- Every link is **undirected**: Neo4j stores it one way, routes use it both ways.
- A link's cost is its `length_nm`. Shortest always means least total `length_nm`.
- A link's `geometry_wkt` is the line it follows over the water, from its first
  node to its second. It is what areas of denial are tested against.

## 2. The port rule

**A port is a route's start or end, never a stop in between.**

- A Port-to-SeaNode link may be used **only** if its port is the route's own
  origin or destination.
- Port-to-port links may always be used. They let a short coastal hop run
  directly between neighbouring ports.

> **Neo4j trap:** `load.cypher` loads *both* kinds of port link as
> `SEA_ACCESS`. Tell them apart by the far end's label: Port-to-SeaNode links
> fall under the rule; Port-to-Port links don't.

For plain shortest routes, ignoring the rule happens to give the same answer:
300 random pairs were compared and all agreed, which is why `START_HERE.md`'s
simple `apoc.algo.dijkstra` query works. For alternative routes and closures it
can differ, so implement the rule.

## 3. Areas of denial

An area is a **circle** or a **polygon**:

- **Circle:** a centre `[lon, lat]` and a radius in nautical miles, measured
  along the earth's surface (great circle). Earth radius 6,371.0088 km;
  1 nm = 1.852 km.
- **Polygon:** 3 to 200 corners `[lon, lat]`. Its sides are **straight lines
  in longitude and latitude**, which is how they look on the map. Each corner
  is taken the short way round from the one before, so a polygon may cross the
  date line.
- Longitudes are first wrapped into −180 to 180, so a centre given as 180 is −180.

**A link is closed if any part of its line enters any area.** Test it like this:

1. Put points along the link's line, along great circles, **no more than 10 nm
   apart**, keeping every original vertex.
2. Take each pair of neighbouring points as one short piece. The link is closed
   if, for any piece and any area:
   - either end is inside the area, or
   - **for a circle:** the piece comes within the radius of the centre,
     measured flat around the centre. That means x = Δlon × cos(centre latitude) × 60
     and y = Δlat × 60, in nm, with Δlon taken the short way round.
   - **for a polygon:** the piece crosses any side. "Inside" uses the usual
     ray-crossing test on longitudes unwrapped near the polygon.

> **Pitfalls:** testing the straight line between two ports (it crosses land
> and misses real lanes), testing only stored vertices (a small area between
> two vertices is missed, as acceptance case A02 shows), and measuring a radius
> in screen pixels.

Then:

- Closed links are left out of **every** route search, in both directions.
- If the origin or destination port is inside any area, there is no route.
  The origin is checked first. The reason is `Origin port <name> is inside a
  denial area.` or `Destination port <name> is inside a denial area.`
- If the areas cut every path, the reason is `No sea route avoids the denial areas.`

## 4. One request, step by step

1. Check the request (see `docs/API.md`). A bad request gets HTTP 400.
2. **Original route:** the shortest route with **no** areas. If there is none,
   answer 400 with `No sea route connects these ports.` (Today every port is
   connected, so this doesn't happen.)
3. If an end port is inside an area, answer with `routes: []` and the blocked
   reason. Stop.
4. Close the links the areas reach (section 3).
5. Find up to K routes by the chosen method (sections 5 and 6), never using a
   closed link.
6. `affected` is true when any closed link is on the original route.
7. If areas were given and a route was found: `extra_nm` is the best route's
   length minus the original's, and `extra_days_at_18kt` is that over 18 knots.

## 5. Yen's K shortest

Standard Yen's algorithm: the K shortest **loopless** routes (no node twice),
each found by spur searches that obey the port rule and the closures. The
candidates are ranked by length.

On this mesh, the 2nd and 3rd routes are often the 1st nudged around one
junction. That is correct, and it is why distinct corridors exists.

## 6. Distinct corridors

The via-point method, with these fixed settings:

| Setting | Value |
|---|---|
| Separation between corridors | 10% of the shortest route, at least 50 nm |
| Free zone around each end port | 150 nm |
| Longest route considered | 3 × the shortest |

1. Compute the shortest distance from the origin to every node (`fwd`), and
   from the destination to every node (`bwd`), obeying the port rule (both
   ends count as the route's own ports) and the closures.
2. If the destination can't be reached, there are no routes.
3. For every node v reached both ways, `total(v) = fwd(v) + bwd(v)`. Take the
   nodes in order of total, cheapest first; ties go in order of node id.
4. Stop when total > 3 × shortest, or K routes are kept.
5. The route through v is the shortest path origin→v joined to v→destination.
   Skip it if it uses any node twice.
6. The first route is always kept. A later v is skipped if:
   - it lies within 150 nm (great circle) of the origin or destination, or
   - it lies within the separation distance of a kept route's line (that
     line sampled every 10 nm), or
   - the route is not "straight through" v. Take the stretch of the route
     from the separation distance before v to the separation distance after
     it, extending by whole links until each side is at least that long. The
     shortest path between the stretch's two ends must be no shorter than the
     stretch minus 1 nm or 0.5%, whichever is larger. This rejects routes that
     go out to v and back.
7. Keep the route and add its line to the kept corridors.

## 7. What a route reports

- `length_nm`: total of its links, rounded to 0.1 nm.
- `passages`: the names (from `passages.csv`) of the canals and straits its
  links are tagged with, in the order the route first meets them.
- `line`: `[lon, lat]` points from origin to destination, the links' lines
  joined end to end, each turned to run the route's way.
- `hops`: the number of links.
