# Source data

The raw public data the data model in `neo4j-datamodel/` is built from. These
files are inputs only: the app never reads them.

| File | What it is | SHA-256 (first 16) |
|---|---|---|
| `Phase 2 Data/UpdatedPub150.csv` | NGA World Port Index, Pub 150 | `315644f1e7796629` |
| `Phase 2 Data/Ourairports.csv` | OurAirports airports | `536d6de0dfeed92d` |
| `raw/ourairports/runways.csv` | OurAirports runways | `ca6ad4f9e86ee5c5` |
| `raw/marnet/marnet_plus_100km.geojson` | Eurostat MARNET shipping lanes, 100 km, as bundled in searoute-ts 2.3.1 | `d24d4d181814599a` |

Publishers, links, download dates and licences are in
`neo4j-datamodel/ASSUMPTIONS.md`, under Sources.

## Rebuild the data model

```bash
python tools/datamodel/build.py
```

This writes every generated file in `neo4j-datamodel/`: the CSVs,
`load.cypher`, `build-report.json` and `check_routes.py`. The hand-written
notes there (`START_HERE.md`, `README.md`, `ASSUMPTIONS.md`) are left alone.
It takes a few seconds and needs only Python 3.

`python -m unittest tests.test_build` rebuilds into a temporary folder and
checks the result is byte for byte what is committed. If it fails, the
committed data and the build have drifted apart.

The folder names (`Phase 2 Data`, `raw/`) are kept as the build script
expects them, so the script is unchanged from the version that made the
committed data. It came from the `Falcon_Model_Sept23` repository, commit
`188bd58`. The 20 km and 50 km lane networks the script can also use are not
included; only 100 km is used.
