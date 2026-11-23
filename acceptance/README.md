# Acceptance test

Proves a FALCON server gives the reference answers, wherever it runs.

```bash
python acceptance/check_server.py http://localhost:8100
```

Give it the address of any server that implements `docs/API.md`: this
Python version, or the migrated one. It sends 28 requests plus one port-list
check, compares each answer with the reference, and prints PASS or FAIL with
what differs. Add `--report result.json` to keep a record. The exit code is 0
only when everything passes. It needs Python 3 and nothing else.

## The files

| File | What it is | Edit it? |
|---|---|---|
| `cases.json` | The requests, each with a plain description of what it tests | Yes, to add a case |
| `golden.json` | The reference version's answer to each request | No: `make_golden.py` writes it |
| `check_server.py` | Sends the requests to a server and compares | Only to change the rules below |
| `make_golden.py` | Asks this reference version for its answers and writes `golden.json` | |

## The cases

| Ids | What they cover |
|---|---|
| S01–S10 | The ten scenarios in `scenarios/`, sent exactly as `run_scenarios.py` sends them |
| R01–R04 | Plain routes: shortest only (R01 is `START_HERE.md`'s 1,450 nm), Yen's K shortest, distinct corridors with K=5 |
| A01–A07 | One case per area-of-denial rule in `docs/ROUTING_RULES.md`: a strait closed, a 5 nm area between two stored points, a circle and a polygon across the date line, empty ocean, and a port inside an area at each end |
| E01–E07 | Requests that must be refused with HTTP 400 and an `error` message |

## What counts as the same answer

| Compared | Rule | Why |
|---|---|---|
| HTTP status | Must match; a 400 must carry an `error` message | |
| Number of routes, and each route's passages in order | Must match exactly | They are what a planner reads |
| Every length (`original`, each route, `extra_nm`) | Within 1 nm or 0.1%, whichever is larger | Rounding and float order differ between languages; real mistakes are far bigger |
| `blocked` reason | Same text, ignoring case | The map shows it as is |
| `affected` | Must match | |
| `closed_links` | Within 2 links or 2%, whichever is larger | The count depends on how finely each line is sampled at the edges of an area. A different but correct sampler can close a link that only grazes an edge. The routes themselves must still match |

Error message wording and `ms` are not compared.

## When the answers should change

`golden.json` is the target, so it changes only on purpose. If the rules
change, or you add a case:

1. Change the code or `cases.json`.
2. Run `python acceptance/make_golden.py`.
3. Commit both, saying in the message why the answers changed.

`python -m unittest tests.test_acceptance` fails if the code and
`golden.json` disagree, so a change can't slip in unnoticed.

If you think a reference answer is **wrong**, raise it. Don't build a
version that quietly differs.
