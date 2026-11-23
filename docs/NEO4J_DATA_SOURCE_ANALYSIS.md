# Reading the data from Neo4j instead of CSV: analysis and logic

Status: **carried out on 2026-11-04** (driver: official `neo4j` package; order: option B).
The old CSV code is kept as comments marked `CSV`; originals are in `_backup_before_neo4j/`.
Originally written as analysis only. Written 2026-11-04 from the code in
`C:\pycsvneo4j\testing`. Use this as the reference before changing any script.

Goal: the Python scripts read the data model from a Neo4j database, loaded from the
current CSV files, and no longer open these files at run time:

- Nodes: `testing/neo4j-datamodel/nodes/` (`ports.csv`, `sea_nodes.csv`, `passages.csv`, `airfields.csv`, `runways.csv`)
- Relationships: `testing/neo4j-datamodel/relationships/` (`sea_segments.csv`, `port_to_sea_node.csv`, `port_to_port.csv`)

The CSV files stay the source of truth. Neo4j is loaded from them with `load.cypher`
and can be thrown away and reloaded at any time (`MIGRATION.md`).

---

## 1. How it works today

Only three scripts open the data CSVs. Everything else gets the data from them.

| Script | Where | Reads from CSV | Builds |
|---|---|---|---|
| `sea_routing.py` | `SeaGraph.load()`, lines ~169–192 | `ports.csv`, `sea_nodes.csv`, `sea_segments.csv`, `port_to_sea_node.csv`, `port_to_port.csv` | The whole graph, in memory |
| `server.py` | lines ~43–44 | `passages.csv` | `PASSAGE_NAMES` (passage id → name) |
| `validate_data.py` | `validate()`, line ~126 | Every CSV, plus `build-report.json` | The start-up data check; the server won't start if it fails |

The graph `SeaGraph.load()` builds in memory:

| Structure | Contents |
|---|---|
| `coords` | Node id (port or sea node) → `(lon, lat)` |
| `ports` | Port id → its CSV row. Only `name`, `country`, `harbor_size`, `lat` and `lon` are ever used |
| `edges` | Edge id → `(a, b, length_nm, passage, kind, geometry)`. Edge id is `kind:row_index`; `kind` is `segment`, `port_access` or `port_to_port`; `geometry` is the parsed `geometry_wkt`, from `a` to `b` |
| `adj` | Node → `[(neighbour, edge id)]`. Each link is added in both directions |

These scripts read the data indirectly and need no data-access changes:

- `run_scenarios.py` uses `server.GRAPH`.
- `map_snapshot.py` uses `graph.edges`, `graph.coords` and the `kind` names.
- `acceptance/make_golden.py` and `tests/test_acceptance.py` go through `server.find_routes`.
- `acceptance/check_server.py` talks to a server over HTTP only.

The routing itself works entirely on the in-memory graph: A* (`shortest`), Yen's K
shortest (`k_shortest`), distinct corridors (`distinct_routes`) and the
area-of-denial test (`links_in_areas`). **So the switch only touches how the graph is
loaded. The routing logic doesn't change.**

---

## 2. Recommended approach: load from Neo4j once, keep routing in Python

1. At start-up, the scripts run a few Cypher queries in place of opening CSV files.
2. They build exactly the same `coords`, `ports`, `edges` and `adj` structures from the results.
3. All the routing code stays as it is.

This gives the same answers as `acceptance/golden.json`, and routes still take milliseconds.

### Why not route inside Neo4j (APOC or GDS)? Leave it for a later phase

- **The port rule** (`docs/ROUTING_RULES.md` §2): a Port→SeaNode link can only be used by
  the route's own origin or destination. `apoc.algo.dijkstra` can't apply a condition like that.
- **Distinct corridors and the straight-through check** need two full Dijkstra trees plus
  custom logic: many round trips to the database.
- **Areas of denial** test each link's real line every 10 nm. That's geometry work, which
  would still be done in Python.
- **Same answers:** tie-breaking would differ, so matching `golden.json` exactly would be hard.

---

## 3. Mapping the data back to the existing structures

| Structure | Query |
|---|---|
| `ports` and port `coords` | `MATCH (p:Port) RETURN p.port_id, p.name, p.country, p.harbor_size, p.lat, p.lon` |
| Sea node `coords` | `MATCH (s:SeaNode) RETURN s.sea_node_id, s.lon, s.lat` |
| `kind = segment` | `MATCH (a:SeaNode)-[r:SEA_SEGMENT]->(b:SeaNode)` |
| `kind = port_access` | `MATCH (a:Port)-[r:SEA_ACCESS]->(b:SeaNode)` |
| `kind = port_to_port` | `MATCH (a:Port)-[r:SEA_ACCESS]->(b:Port)` |
| `PASSAGE_NAMES` | `MATCH (x:Passage) RETURN x.passage_id, x.name` |

Each relationship query returns `a`'s id, `b`'s id, `r.length_nm`, `r.passage` and
`r.geometry_wkt`. Use `sea_node_id` or `port_id` for the ids, matching the end's label.

Rules the new loader must follow:

1. **Match one way, with the arrow (`->`).** An undirected match returns every link twice.
   The direction also matters because:
   - `geometry_wkt` runs from the first node to the second (`describe()` reverses it when a route uses a link backwards).
   - The port rule (`_usable`) expects the port to be the first end (`a`) of a `port_access` link.
2. **Tell the two kinds of `SEA_ACCESS` apart by the far end's label.** `load.cypher` loads
   Port→SeaNode and Port→Port links both as `SEA_ACCESS`. Port→Port links are always usable;
   Port→SeaNode links fall under the port rule. Mixing them up lets routes run through other
   ports' access links. `ROUTING_RULES.md` §2 calls this the "Neo4j trap."
3. **Empty values come back as null** (`LOAD CSV` reads an empty field as null).
   - `passage` null → `None` already works with the existing `r["passage"] or None`.
   - `country` and `harbor_size` null → turn them into `""`. Otherwise `run_scenarios.py`
     and its `results.json` show `null` where they show `""` today.
4. **Numbers come back as floats, not strings.** The existing `float()` calls still work.
   `toFloat()` in Neo4j and `float()` in Python give the same value, so route lengths are unchanged.
5. **Reuse `parse_wkt()` for the geometry.** Neo4j holds the same WKT strings as the CSVs.
6. **Keep the `kind` names** (`segment`, `port_access`, `port_to_port`). `_usable()` and
   `map_snapshot.py` depend on them.

---

## 4. Load order matters for identical answers

Today the edge ids and the neighbour order follow the row order in the CSV files. When two
routes are equally long, A*, Dijkstra and Yen's keep the first one they find. So the load
order decides which route wins a tie. Neo4j doesn't promise any return order.

How unique each link is (checked against the current CSVs):

| File | Rows | Unique key |
|---|---|---|
| `sea_segments.csv` | 9,686 | `segment_id` is unique. There are 72 parallel segments between the same two sea nodes, so the node pair alone isn't unique |
| `port_to_sea_node.csv` | 7,612 | The `(port_id, sea_node_id)` pair is unique. `segment_id` is shared (only 1,299 distinct values) |
| `port_to_port.csv` | 2,507 | The `(from_port_id, to_port_id)` pair is unique. `segment_id` is shared (706 distinct values) |

Two options:

- **A (recommended):** store each row's number as a property at load time, using
  `linenumber()` in `LOAD CSV`, then `ORDER BY` it. Because `load.cypher` is generated,
  this means changing `tools/datamodel/build.py` and regenerating, not editing the file.
  Ties then break exactly as they do today.
- **B (no data change):** order by the unique keys above. Results are deterministic but in
  a different order from the CSVs. Equal-length ties are rare, so answers will probably
  still match, but the acceptance test has to confirm it. Exact passage names are where a
  difference would show up.

---

## 5. Start-up data check against Neo4j

`MIGRATION.md` requires that the app refuse to start on bad data and say where the problem
is. Two things change in Neo4j:

- `LOAD CSV` with `MATCH` **silently skips** a relationship whose end node is missing.
- `toFloat('abc')` **silently becomes null**.

So the database needs its own check:

| Check | How |
|---|---|
| Counts match `START_HERE.md` and `build-report.json` | Count each label and relationship type. Expected: Port 3,806, SeaNode 4,525, Passage 22, Airfield 2,285, Runway 3,640, SEA_SEGMENT 9,686, SEA_ACCESS 10,119 (7,612 + 2,507), HAS_RUNWAY 3,640. Running `load.cypher` twice doubles the counts, so this catches that too |
| Unique ids | Already covered by the constraints in `load.cypher`. Check that they exist |
| No nulls in required fields | `lat`, `lon`, `name`, `harbor_size`, `length_nm` and `geometry_wkt` must not be null. Name the label or type, the id and the field |
| Allowed values | `harbor_size` in the allowed set, coordinates in range, `length_nm` ≥ 0 |
| Every port reaches the sea | `MATCH (p:Port) WHERE NOT (p)-[:SEA_ACCESS]-()` must return no rows |
| Every passage tag is a real passage | Every `r.passage` matches a `Passage.passage_id` |
| Geometry | Do these in Python after loading, reusing the current logic: the WKT parses, the line ends near its nodes, and its length matches `length_nm` |

Messages name the label or type, the id and the field (for example
`SEA_SEGMENT SEG-123: length_nm is null`). A database has no file row numbers to point to.

---

## 6. Changes by file (plan only)

| File | Change |
|---|---|
| `sea_routing.py` | Add a Neo4j loader next to the CSV one. A setting such as `SEA_DATA_SOURCE=neo4j\|csv` chooses between them. Keep the CSV loader for checking the two against each other |
| `server.py` | Load `PASSAGE_NAMES` from `:Passage`. If Neo4j is down or its data fails the check, refuse to start with a clear message |
| `validate_data.py` | Add the Neo4j checks from section 5. Keep the CSV checks, because the CSVs stay the source of truth |
| New config | `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` from the environment. Never hard-code the password |
| `run_scenarios.py` | No data-access change. Only update the `"data"` label in `results.json` to say Neo4j |
| `map_snapshot.py` | No change, as long as the `kind` names stay the same |
| `acceptance/make_golden.py` | Keep the CSV hashes. Optionally also record the Neo4j counts |
| `check_routes.py` (both copies are identical) | Keep reading the CSVs. It's the independent "correct answer" to compare Neo4j results against |
| `tools/datamodel/build.py` | Only for option A in section 4: add the row-number property to the generated `load.cypher`. Then regenerate, since `tests/test_build.py` compares the output |
| `tests/` | Keep the CSV tests. Add Neo4j-mode tests that skip when no database is configured |

### Start-up sequence in Neo4j mode

1. Read the config (`SEA_DATA_SOURCE`, `NEO4J_*`).
2. Connect and verify the connection. Fail with a clear message if Neo4j can't be reached.
3. Run the data check (section 5). Refuse to start if it fails.
4. Fetch ports, sea nodes, passages and the three relationship sets, in a fixed order (section 4).
5. Build `coords`, `ports`, `edges`, `adj` exactly as the CSV loader does.
6. Build the area-of-denial index (`links_in_areas([])`) and start serving.

---

## 7. Decisions still open

1. **How to connect to Neo4j.**
   - The official `neo4j` Python driver (Bolt) is the cleanest, but adds a pip dependency.
     Today the project uses only the standard library.
   - Neo4j's HTTP API through `urllib` keeps it standard-library only, with a little more code.
2. **Row order:** option A (exact tie-breaking, needs a `build.py` change) or option B
   (no data change, rely on the acceptance test).
3. **Picking up data changes.** The graph is loaded once, so reloading Neo4j means
   restarting the server. That's simplest and matches the "throw it away and reload" model.
4. **Which CSVs are in Neo4j?** Neo4j could be loaded from a different version of the CSVs
   than the repo has. Matching counts won't always catch that. A small "dataset version"
   node, written by the build, would.

---

## 8. How to show it gives the same answers

1. Load Neo4j from the current CSVs and check the counts against `START_HERE.md`.
   `START_HERE.md`'s test query (Busan → Subic Bay) must give 1,450 nm.
2. Build the graph both ways, from the CSVs and from Neo4j, and compare: same nodes and
   coordinates, same set of edges (by the keys in section 4), same lengths, passages and geometry.
3. Start the server in Neo4j mode and run
   `python acceptance/check_server.py http://localhost:8100`. It must pass 29 of 29.
4. Run `python run_scenarios.py` and compare with the last CSV report. Every distance
   should be the same.
