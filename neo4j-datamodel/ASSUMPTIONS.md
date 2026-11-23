# Assumptions, filters and known gaps

Every place this data model goes beyond copying its sources is listed here.
Each item gives the choice made, the reason, and its effect. Numbers are from the
build of 2026-10-29; `build-report.json` holds the live figures.

## Sources

| Data | Publisher and link | File used | Retrieved | Licence |
|---|---|---|---|---|
| Ports | NGA World Port Index, Pub 150 ([msi.nga.mil](https://msi.nga.mil/Publications/WPI)) | `UpdatedPub150.csv`, 3,807 rows, 109 columns | 2026-10-15 | U.S. government public data |
| Airfields | OurAirports ([ourairports.com/data](https://ourairports.com/data/)) | `airports.csv` | 2026-10-15 | Public domain |
| Runways | OurAirports (same; [mirror](https://davidmegginson.github.io/ourairports-data/runways.csv)) | `runways.csv` | 2026-10-29 | Public domain |
| Shipping lanes | Eurostat MARNET network ([eurostat/searoute](https://github.com/eurostat/searoute)), as bundled in [searoute-ts](https://github.com/mayurrawte/searoute-ts) 2.3.1 | `marnet_plus_100km` | 2026-10-29 | EUPL-1.2 |
| Canal drafts and strait boxes | searoute-ts `lib/drafts.js` and `lib/restrictions.js`, which cite the canal authorities | | 2026-10-29 | MIT |

The airports and runways files were downloaded two weeks apart. OurAirports
updates nightly, so a handful of runways may have changed between them.

## Ports

- **All 3,806 NGA ports are loaded; there is no size filter.** A "Large harbor"
  filter would drop most FALCON ports: Apra Harbor (Guam), Yokosuka, San Diego
  and Subic Bay are "Medium" in NGA, and Diego Garcia and Kwajalein are "Very
  Small". Ports are a small share of the graph, so keeping them all costs little.
  Filter on `harbor_size` at query time instead.
- **NGA writes "0.0" for unknown.** 638 channel depths, 643 cargo pier depths and
  2,862 maximum vessel drafts are 0.0 in the source, and those ports aren't
  unusable. They are loaded as blank. The same rule applies to every measured
  column: depths, vessel length, beam and draft, tidal range and entrance width.
- **Most depths are band floors, not surveys.** 92% of channel depths and 91% of
  cargo pier depths are exactly the bottom of one of NGA's 5 ft depth bands,
  converted to metres. For example, 23.2 m means "76 ft or more" and 9.4 m means
  "31–35 ft". They are conservative "at least" figures. `max_vessel_draft_m` is
  mostly specific values (88%), but is known for only 944 ports.
- **`controlling_depth_m` is derived:** the shallower of channel depth and cargo
  pier depth, because a ship must both get in and work cargo. When only one is
  known, that one is used. `controlling_depth_basis` records which case applies.
- **Yes/No/Unknown** facility fields load as true, false or blank. The text
  "Unknown" loads as blank everywhere.
- **`country` holds country names.** NGA's column is called "Country Code" but
  contains names such as "Japan".
- **49 of NGA's 109 columns are loaded**, plus 6 derived here: identity, location, harbor
  characteristics, depths and vessel limits, the main facilities, cranes, supplies,
  repairs, dry dock and rail. Not loaded: communications, quarantine, chart and
  publication references, pilotage details beyond "compulsory", and ice and
  swell entrance restrictions. Adding one is a one-line change to the builder.
- **Source errors found:** WPI number 400 appears twice for the same Icelandic
  port (two spellings, same position), so the second copy is dropped. WPI number
  46135 is shared by two different Nigerian ports, so Lekki is loaded as
  `WPI-46135-OID3579`.
- **Ports missing from NGA:** the World Port Index lists civilian facilities, so
  closed naval anchorages are absent. Pearl Harbor is the known FALCON case;
  Honolulu (a separate harbor about 8 nm away) is in the data, but is not a
  stand-in for it.

## Airfields

- **Included:** OurAirports types `large_airport` and `medium_airport` with at
  least one open, paved runway of **8,000 ft or more**, which gives 2,285
  airfields. This threshold sets the size of the mock-up; it is not an
  aircraft limit. A C-130 or C-17 can use far shorter strips, so lower
  `MIN_PAVED_RUNWAY_FT` in the builder to widen the set (7,000 ft gives 2,814;
  9,000 ft gives 1,598).
- **Why not "large airports only":** OurAirports rates most military airfields
  "medium". Andersen, Travis, Diego Garcia, Wake, Yokota and Osan would all
  disappear.
- **"Paved"** means the runway's surface code contains ASP, CON, PEM, BIT, TAR,
  PAVED or MAC. OurAirports surface codes are free text, so anything else
  (turf, gravel, blank, unknown) counts as unpaved.
- **Runways:** every runway at an included airfield is loaded, with `closed` and
  `paved` flags. Only open, paved runways count toward the 8,000 ft filter and
  `longest_paved_runway_ft`.
- **Shared runways:** Hickam AFB has no runway of its own in OurAirports (it
  shares Honolulu's), so it isn't loaded and Honolulu (PHNL) is. Other joint-use
  fields may behave the same way.
- **Not in OurAirports:** runway strength (PCN), ramp space (MOG) and fuel
  availability. "Can it land" is answerable from this data; "can it sustain
  airlift operations" is not.

## Sea network

- **Resolution: 100 km.** Eurostat publishes the network at 20, 50 and 100 km.
  The 100 km version gives 4,525 sea junctions, compared with 13,561 at 20 km after
  the same clean-up. Ten Indo-Pacific routes stay within 1.5% of searoute's own
  20 km answers, apart from one short route (below):

  | Route | searoute 20 km | This model |
  |---|---|---|
  | San Diego to Apra Harbor | 5,538 nm | 5,539 nm |
  | Busan to Subic Bay | 1,439 nm | 1,450 nm |
  | Yokosuka to Singapore | 2,957 nm | 2,951 nm |
  | Apra Harbor to Diego Garcia | 4,854 nm | 4,862 nm |
  | Los Angeles to Busan | 5,509 nm | 5,486 nm |
  | Darwin to Singapore | 1,893 nm | 1,919 nm |
  | Tacoma to Yokosuka | 4,637 nm | 4,662 nm |
  | Kaohsiung to Manila | 536 nm | 603 nm |
  | Singapore to Colombo | 1,648 nm | 1,644 nm |
  | Honolulu to Apra Harbor | 3,337 nm | 3,353 nm |

- **Short routes come out longer, by design.** searoute counts distance on the
  network only; this model also counts the leg from each port to its lane. On a
  short route those legs are a large share: Kaohsiung to Manila is 12.5% longer.
- **Clean-up that changes no route:** points closer than 0.00001° are one junction,
  the same rule searoute uses; 180°E and 180°W are the same meridian; 8
  duplicate segments are dropped; dead-end lanes that serve no port are removed
  (172); and junctions that only join two lanes of the same passage are merged
  into one longer segment (544). Each merged segment keeps its full shape in
  `geometry_wkt`.
- **Distances** are great-circle (haversine) on a sphere of mean radius
  6,371.0088 km, the radius searoute uses, at 1.852 km to the nautical mile.
- **Connecting ports:** each port links to the nearest point on any lane. It gets
  a link to both ends of that segment, measured along the lane plus the straight
  leg in. Ports beside the same segment also link to their neighbours. No sea
  junction is added for a port. A route between two coastal ports that passes a
  third adds that port's access leg twice, which slightly overstates very short
  coastal hops.
- **`access_quality`** is the straight leg from port to lane: good up to 10 nm
  (1,429 ports), approximate up to 50 nm (1,877), poor above 50 nm (500). The
  network doesn't model rivers, so for inland river ports the leg runs over land.
  177 of the "poor" ports are river ports, such as Manaus, 607 nm up the Amazon.
  Treat their sea distances as unreliable.
- **Passages** are tagged three ways, recorded in `tag_method`:
  - `native`: Eurostat's own tags on 12 canals, straits and Arctic routes.
  - `bbox_library`: searoute-ts's boxes for Bosporus, Hormuz, Sunda and Cape Horn.
  - `bbox_falcon`: six boxes drawn for this model, for the Luzon, San
    Bernardino, Taiwan, Korea, Lombok and Makassar straits. They are
    approximate and should be checked against charts before anyone relies on
    them.

  A box tags every segment that crosses it, whole.
- **Arctic routes are open.** searoute closes the Northwest and Northeast
  Passages by default because they are ice-bound for much of the year. To
  match that behaviour, close passages `northwest` and `northeast`.
- **Not modelled:** traffic separation schemes, seasonal ice, weather, piracy
  risk, pilotage or transit times at canals, and inland waterways.

## Air network

- **No air links are stored.** Any two airfields connect, and the distance is
  the great-circle distance between them (`point.distance`, WGS-84), computed when
  it's needed and filtered by the aircraft's range. Within 1 nm it matches a
  hand-computed haversine; for example, Andersen to Kadena is 1,234 nm.
- **Not modelled:** airspace, overflight permission, winds, routing around
  restricted areas, and air refuelling.

## Not yet connected

- **Port to airfield (road) links.** An earlier prototype modelled these by
  hand as "line-haul"; this model has none yet. A natural rule is "airfield within N nm
  of a port", but N is a planning choice that should be made deliberately.
- **Platforms and cargo classes** are not part of this package. They join
  through draft (`Port.max_vessel_draft_m`, `controlling_depth_m`), runway
  length, and aircraft range.
