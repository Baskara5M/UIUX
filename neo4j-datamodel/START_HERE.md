# START HERE

This is the FALCON data model for Neo4j.
Please load it as it is. Do not change the CSV files by hand.
If something is wrong or missing, tell me. I will rebuild the package.

## 1. What is inside

| What | How many | From |
|---|---|---|
| Port | 3,806 | NGA World Port Index |
| Airfield | 2,285 | OurAirports (runway 8,000 ft or more) |
| Runway | 3,640 | OurAirports |
| SeaNode | 4,525 | Eurostat shipping-lane network |
| Passage | 22 | Canals and straits (Malacca, Panama, Luzon ...) |

## 2. The most important idea

There are two kinds of node.

- **Port and Airfield = places.** Ships and aircraft go here.
- **SeaNode = a point in the sea.** It is NOT a place. Ships pass through it.
  It is like a road intersection. A route goes:
  Port -> SeaNode -> SeaNode -> SeaNode -> Port.

Do not show SeaNode to the user as a place.
Do not draw SeaNode as a dot on the map.

## 3. Load it (5 minutes)

1. Use Neo4j 5. Start with an EMPTY database.
2. Copy the folders `nodes` and `relationships` into the Neo4j `import` folder.
3. Run the file `load.cypher`:

```
cypher-shell -u neo4j -p YOUR_PASSWORD -f load.cypher
```

4. At the end it prints counts. They must be:

```
Airfield 2285   Passage 22   Port 3806   Runway 3640   SeaNode 4525
HAS_RUNWAY 3640   SEA_ACCESS 10119   SEA_SEGMENT 9686
```

## 4. Test it (needs the APOC plugin)

Run this. The answer must be **1450** (nautical miles).

```
MATCH (a:Port {name:'Busan'}), (b:Port {name:'Subic Bay'})
CALL apoc.algo.dijkstra(a, b, 'SEA_SEGMENT|SEA_ACCESS', 'length_nm') YIELD path, weight
RETURN round(weight) AS nm
```

More test answers:

| From | To | Answer (nm) |
|---|---|---|
| San Diego | Apra Harbor | 5539 |
| Keppel - (East Singapore) | Colombo | 1644 |
| Kao-Hsiung | Manila | 603 |

## 5. Rules

1. **Sea route** = shortest path over `SEA_SEGMENT` and `SEA_ACCESS`.
   Weight = `length_nm`. Direction does not matter (both ways).
2. **Air route** = there are NO air relationships.
   Distance = great circle, computed when needed:
   `point.distance(a.location, b.location) / 1852` = nautical miles.
3. **Closure** = remove relationships before you search:
   - close a strait: remove every relationship where `passage` = the strait (example `malacca`)
   - close an area: remove every relationship where `geometry_wkt` goes through the area
   - close a port: remove the port's `SEA_ACCESS` relationships
4. **Empty value = unknown.** It is NOT zero. Do not change empty to 0.
5. **Distances are nautical miles.** Depths are metres. Runways are feet.
6. **Map:** places have `lat` and `lon`. Sea relationships have `geometry_wkt`
   = the real line shape (`LINESTRING(lon lat, ...)`, longitude first).
   - If a route uses a relationship backwards, reverse its points.
   - 39 lines cross the 180° line (lon jumps from +180 to -180). Handle this,
     or the line goes across the whole map.

## 6. Check closures

`check_routes.py` is the correct answer. Your code must give the same numbers.
Python 3. No libraries needed.

```
python check_routes.py "Keppel - (East Singapore)" "Colombo"
python check_routes.py --close malacca "Keppel - (East Singapore)" "Colombo"
```

Answers: 1,644 nm (open) and 2,390 nm (Malacca closed).

## 7. More detail

- `README.md`: all nodes, relationships and example queries
- `ASSUMPTIONS.md`: every filter, every rule, every known problem
