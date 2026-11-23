# Scenarios

Each `.json` file here is one test scenario: a move from one port to another,
any areas of denial, and what the answer should look like. Run them all with

```bash
python run_scenarios.py
```

and open the `report.html` it names. Files run in name order, so number them
(`03_...json`, `04_...json`).

## A scenario file

```json
{
  "id": "S02",
  "name": "San Diego to Subic Bay, San Bernardino Strait denied",
  "purpose": "One or two sentences on what this scenario shows. It goes in the report.",
  "from": "San Diego",
  "to": "Subic Bay",
  "k": 3,
  "method": "distinct",
  "areas": [
    {"type": "circle", "center": [124.3, 12.6], "radius_nm": 150}
  ],
  "expect": {
    "route_found": true,
    "affected": true,
    "best_avoids": ["San Bernardino Strait"],
    "best_passes": ["Luzon Strait"],
    "extra_nm": {"min": 0, "max": 300}
  }
}
```

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Short, unique label, such as `S03` |
| `name` | yes | One line for the report |
| `purpose` | no | What the scenario is meant to show |
| `from`, `to` | yes | A port's exact name in the World Port Index (`San Diego`) or its id (`WPI-16010`). If a name belongs to more than one port, the run lists the ids to choose from. |
| `k` | no | Number of routes, 1 to 5 (default 3) |
| `method` | no | `distinct` for distinct corridors (default) or `yen` for Yen's K shortest |
| `areas` | no | Areas of denial, the same as the web map's API. Points are `[longitude, latitude]`. Circle: `{"type": "circle", "center": [lon, lat], "radius_nm": r}`. Polygon: `{"type": "polygon", "points": [[lon, lat], ...]}`. |
| `expect` | no | What must be true; see below |

## Expectations

Leave out any you don't need. An unknown key fails the scenario, so a typo
can't make a check silently disappear.

| Key | Example | Passes when |
|---|---|---|
| `route_found` | `true` | A route is found (`false`: no route is possible) |
| `blocked_contains` | `"inside a denial area"` | The reason no route is possible contains this text |
| `routes_at_least` | `3` | At least this many alternative routes |
| `best_length_nm` | `{"min": 6600, "max": 6900}` | The best route's length is in range (either end may be left out) |
| `best_passes` | `["Luzon Strait"]` | The best route goes through each of these |
| `best_avoids` | `["San Bernardino Strait"]` | The best route goes through none of these |
| `affected` | `true` | The areas cut the route that was best with no areas |
| `extra_nm` | `{"min": 0, "max": 300}` | The areas add this much to the best route |

Passage names are the ones in `neo4j-datamodel/nodes/passages.csv`, such as
Luzon Strait, Taiwan Strait, Korea Strait, San Bernardino Strait, Malacca
Strait, Panama Canal.

## Checks every scenario gets

These run on every answer, whatever the scenario expects:

- Every route starts at the origin port and ends at the destination port.
- No route is shorter than the great-circle distance between the two ports.
- Routes are ranked shortest first.
- Each route's drawn line matches its reported length (within 2%).
- No route is listed twice.
- With areas of denial: no route enters any area, re-checked every 2 nm along
  the route, independently of how the route finder closed the lanes.
