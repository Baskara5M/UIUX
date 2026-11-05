# API

The contract the map page uses, and the one a migrated version must keep.
`acceptance/check_server.py` tests it over HTTP. All bodies are JSON, UTF-8.

## GET /api/ports

Every port, sorted by name and then country:

```json
[{"id": "WPI-60390", "name": "Busan", "country": "South Korea", "lat": 35.1, "lon": 129.033333, "size": "Large"}, ...]
```

- 3,806 ports, each `id` once.
- `size` is the World Port Index harbor size (`Very Small`, `Small`, `Medium`,
  `Large`), or `""` when NGA doesn't give one.

## POST /api/routes

### Request

```json
{
  "from": "WPI-60390",
  "to": "WPI-58395",
  "k": 3,
  "method": "distinct",
  "areas": [
    {"type": "circle", "center": [124.2, 22.43], "radius_nm": 239},
    {"type": "polygon", "points": [[118.5, 26.5], [121.8, 25.6], [121.0, 21.8], [119.0, 22.2]]}
  ]
}
```

| Field | Required | Rule |
|---|---|---|
| `from`, `to` | yes | Port ids from `/api/ports`; must differ |
| `k` | no, default 3 | A whole number from 1 to 5 |
| `method` | no, default `yen` | `yen` or `distinct` (see `docs/ROUTING_RULES.md`) |
| `areas` | no | Up to 20 areas of denial |
| circle | | `center` is `[lon, lat]`; `radius_nm` is over 0 and at most 3,000 |
| polygon | | `points` is 3 to 200 `[lon, lat]` corners |

Points are `[longitude, latitude]`: finite numbers, latitude from −90 to 90.
Longitude may be any value; it is wrapped into −180 to 180. The body may be at
most 1,000,000 bytes.

### Response, HTTP 200

```json
{
  "method": "distinct", "k": 3, "areas": 2,
  "original": {"rank": 0, "length_nm": 1449.6, "passages": ["Korea Strait", "Taiwan Strait"], "hops": 16, "line": [[129.033333, 35.1], ...]},
  "routes": [{"rank": 1, "length_nm": 1969.1, "passages": ["Korea Strait", "San Bernardino Strait"], "hops": 13, "line": [...]}, ...],
  "closed_links": 92,
  "affected": true,
  "extra_nm": 519.5,
  "extra_days_at_18kt": 1.2,
  "ms": 41
}
```

(The real answer to the request above: closing the Luzon and Taiwan Straits
sends the route east of the Philippines and in through the San Bernardino Strait.)

| Field | Meaning |
|---|---|
| `original` | The best route with no areas, as rank 0 |
| `routes` | Up to `k` routes, ranked from 1, shortest first. Empty when there is no route |
| `closed_links` | How many links the areas closed |
| `affected` | Whether any closed link is on the original route |
| `extra_nm`, `extra_days_at_18kt` | Best route minus original, only when areas were given and a route was found |
| `blocked` | Present only when there is no route: the reason (see below) |
| `ms` | Time taken. Informational only; not compared |

Each route has `rank`, `length_nm` (0.1 nm), `passages` (names, in the order
the route meets them), `hops` (link count) and `line` (`[lon, lat]` from origin
to destination).

**No route** is still HTTP 200, with `routes: []` and one of these `blocked` reasons:

| When | `blocked` | Also present |
|---|---|---|
| The origin is inside an area | `Origin port <name> is inside a denial area.` | `method`, `k`, `areas`, `original` only |
| The destination is inside an area | `Destination port <name> is inside a denial area.` | the same |
| The areas cut every path | `No sea route avoids the denial areas.` | all fields except `extra_*` |

### Errors

| Status | When | Body |
|---|---|---|
| 400 | Any rule above broken, the body missing, too large or not a JSON object, or the ports not connected at all | `{"error": "<what is wrong, in plain words>"}` |
| 404 | Any other path | plain text |
| 500 | An unexpected failure | `{"error": "..."}`. See `MIGRATION.md`: don't copy how the reference fills this in |

The acceptance test checks the status and that a 400 carries an `error`
message. It doesn't check the wording of error messages, only of `blocked`
reasons.
