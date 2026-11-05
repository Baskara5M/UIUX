# Migrating FALCON Sea Network

Read this first. It says what to build, the rules it must follow, and how
both of us can tell when it's done, without arguing about it.

## What this is

A web map that finds sea routes between any two of 3,806 ports along real
shipping lanes. An analyst can draw **areas of denial** (circles or polygons)
and see the routes go around them, with up to five alternatives.

This repository is the **reference version**. It is plain Python (standard
library only) and reads the data from CSV files in `neo4j-datamodel/`. It
**does not use Neo4j while running**. The same CSVs come with `load.cypher`,
which loads them into Neo4j.

## The job

Rebuild this on the corporate side so it gives **the same answers** as this
version. Use whatever stack is approved, for example Neo4j for the graph and
any language for the server. Don't add features; this is a migration.

**Done** means `acceptance/check_server.py` passes against your deployed
server, plus the checklist at the end.

## Start here, in this order

| Step | Read or run | What it gives you |
|---|---|---|
| 1 | `README.md` | How the reference works, and how to run it (`python server.py`, then http://localhost:8100) |
| 2 | `docs/API.md` | The exact requests and responses to keep |
| 3 | `docs/ROUTING_RULES.md` | The exact routing rules, written to implement from. **Read section 2, "the port rule", carefully: it's the easiest one to get wrong in Neo4j** |
| 4 | `neo4j-datamodel/START_HERE.md` | Loading the data into Neo4j, with counts to check |
| 5 | `acceptance/README.md` | The test your version has to pass |

## The three checks

```bash
python validate_data.py                                 # 1. the data is whole and consistent
python -m unittest discover tests                       # 2. the reference still agrees with itself
python acceptance/check_server.py http://YOUR-SERVER    # 3. your version gives the same answers
```

1. **Data.** `validate_data.py` checks every CSV: ids, coordinates, links
   pointing at real nodes, lengths, lines, and row counts against
   `build-report.json`. Each problem names the file, row and field. The
   reference server runs it at start-up and refuses to start on bad data.
   **Your version must do the same.** After loading Neo4j, the counts must
   match `START_HERE.md`.
2. **Rebuild.** The data is generated, never edited by hand.
   `tools/datamodel/build.py` rebuilds it from the public sources in `data/`
   (see `data/README.md`), and `tests/test_build.py` proves the rebuild matches
   what's committed. If the data needs a fix, it goes in the build.
3. **Answers.** `acceptance/check_server.py` sends 28 requests to your server
   and compares with the reference answers in `acceptance/golden.json`:
   - the ten scenarios,
   - plain and alternative routes,
   - every area-of-denial rule, including the date line and a small area
     between two points on a lane,
   - seven bad requests.

   It only needs your server's address. It prints what differs, so a failure
   tells you where to look.

## Must stay true

- **No outbound calls at runtime.** Everything the app needs is in this
  repository. The map's scripts are in `web/vendor/`; keep them local, with no
  CDNs.
- **The CSVs are the source of truth.** Neo4j is loaded from them and can be
  thrown away and reloaded at any time.
- **Areas of denial are tested on each link's real line**
  (`geometry_wkt`), sampled every 10 nm. Never use the straight line between
  two ports.
- **Same answers** as `acceptance/golden.json`, within the tolerances in
  `acceptance/README.md`. If you think a reference answer is wrong, say so;
  don't ship a version that differs.

## Known weaknesses in the reference: don't copy these

The reference is a prototype. Fix these in your version, don't carry them over:

| In the reference | In yours |
|---|---|
| Request logging is switched off, errors included (`server.py`, `log_message`) | Log every request and every error |
| A 500 response sends the Python error text to the browser | Log the detail; send a plain message |
| One request at a time (a global lock) | Handle requests at the same time |
| Listens only on `127.0.0.1`, port fixed in code unless `SEA_NETWORK_PORT` is set | Address and port from configuration |
| The map page has no error handling when a request fails, and puts text into the page without escaping it | Show the error; escape everything that comes from data |
| No restart if the process stops | Runs as a service and comes back after a reboot |

## Suggested month

| Week | Finish line, something you can show |
|---|---|
| 1 | The data loads into Neo4j, the counts match `START_HERE.md`, and the start-up data check is in place. `START_HERE.md`'s test query gives 1,450 nm |
| 2 | The API answers `/api/ports`, plus shortest routes and Yen's K shortest. `check_server.py` passes PORTS, R01–R03 and E01–E07 |
| 3 | Areas of denial and distinct corridors work. `check_server.py` passes everything |
| 4 | The map page is served, it's deployed where analysts can reach it, it restarts on its own, and logging is on. `check_server.py` passes against the deployed address |

Run `check_server.py` against your version every week and share the
`--report` file. That's the progress report.

## Done when

- [ ] `python acceptance/check_server.py <deployed address>` passes, 29 of 29
- [ ] Neo4j counts match `START_HERE.md`
- [ ] The app refuses to start on a broken data file, naming the file, row and field
- [ ] The map works from an analyst's machine: pick two ports, draw an area, see the routes change
- [ ] It makes no outbound calls while answering
- [ ] It comes back by itself after the server restarts
- [ ] Requests and errors are logged
