<<<<<<< HEAD
// Sea routes between ports with searoute-js, exported to CSV.
//
// Ports come from a CSV file, for example the World Port Index (NGA). Columns are
// detected by name: port name, latitude, longitude, and optionally country code,
// UN/LOCODE, harbor size and port number.
//
// Usage:
//   node test.js                              built-in sample ports, every pair
//   node test.js ports.csv --from "ROTTERDAM" one port to every other port
//   node test.js ports.csv --pairs pairs.csv  only the pairs listed (origin,destination)
//   node test.js ports.csv --country US,NL    every pair among ports in these countries
//   node test.js ports.csv --size L           every pair among large harbors (WPI: L, M, S, V)
//
// Options:
//   --limit N      keep only the first N ports after filtering
//   --speed 20     ship speed in knots for the travel-time estimate
//   --waypoints    always write route_waypoints.csv (on by default for 500 routes or fewer)
//   --network      also export the whole searoute shipping-lane network
//   --yes          run jobs over 5,000 routes without stopping to ask
//
// Output (next to this file): routes_summary.csv, route_waypoints.csv, ports_used.csv,
// and with --network, searoute_network.csv. Coordinates are [longitude, latitude].

const fs = require('fs');
const path = require('path');

const OUT_DIR = __dirname;
const BIG_JOB = 5000;
const AUTO_WAYPOINTS_MAX = 500;
const KM_PER_NM = 1.852;
const EARTH_RADIUS_KM = 6371.0088;

const SAMPLE_PORTS = [
  { name: 'SEATTLE', country: 'US', lng: -122.3393, lat: 47.6026 },
  { name: 'LOS ANGELES', country: 'US', lng: -118.276, lat: 33.7406 },
  { name: 'MIAMI', country: 'US', lng: -80.1794, lat: 25.7781 },
  { name: 'NEW YORK', country: 'US', lng: -74.0445, lat: 40.6681 },
  { name: 'ROTTERDAM', country: 'NL', lng: 4.1, lat: 51.95 },
  { name: 'SHANGHAI', country: 'CN', lng: 121.8, lat: 31.35 },
];

// ---------- command line ----------

function parseArgs(argv) {
  const opts = { portsFile: null, speed: 20, flags: new Set() };
  const valueOptions = ['--from', '--pairs', '--country', '--size', '--limit', '--speed'];
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (valueOptions.includes(arg)) {
      opts[arg.slice(2)] = argv[i + 1];
      i += 1;
    } else if (arg.startsWith('--')) {
      opts.flags.add(arg.slice(2));
    } else if (!opts.portsFile) {
      opts.portsFile = arg;
    }
  }
  opts.speed = Number(opts.speed) || 20;
  return opts;
}

// ---------- CSV ----------

// Parses CSV text, including quoted fields with commas, quotes or line breaks.
function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = '';
  let quoted = false;
  text = text.replace(/^﻿/, ''); // Excel byte-order mark
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') { field += '"'; i += 1; }
      else if (ch === '"') quoted = false;
      else field += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ',') { row.push(field); field = ''; }
    else if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i += 1;
      row.push(field); rows.push(row); row = []; field = '';
    } else field += ch;
  }
  if (field !== '' || row.length) { row.push(field); rows.push(row); }
  return rows.filter((r) => r.some((v) => v.trim() !== ''));
}

function cell(value) {
  if (value === null || value === undefined) return '';
  const text = typeof value === 'object' ? JSON.stringify(value) : String(value);
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}
const csvLine = (values) => values.map(cell).join(',') + '\r\n';

// Writes rows as they are produced, so a long run never holds everything in memory
// and a stopped run keeps what it finished.
function csvWriter(fileName, header) {
  const file = path.join(OUT_DIR, fileName);
  const fd = fs.openSync(file, 'w');
  fs.writeSync(fd, csvLine(header));
  let count = 0;
  return {
    write(values) { fs.writeSync(fd, csvLine(values)); count += 1; },
    close() { fs.closeSync(fd); console.log(`  wrote ${file} (${count} rows)`); },
  };
}

// ---------- ports ----------

const norm = (s) => String(s).toLowerCase().replace(/[^a-z0-9]/g, '');

// Column names accepted for each field (compared without spaces, case or punctuation).
const COLUMN_NAMES = {
  name: ['mainportname', 'portname', 'name', 'port'],
  lat: ['latitude', 'lat', 'y'],
  lng: ['longitude', 'lon', 'lng', 'long', 'x'],
  country: ['countrycode', 'country', 'iso2'],
  locode: ['unlocode', 'locode'],
  size: ['harborsize', 'harboursize', 'size'],
  number: ['worldportindexnumber', 'indexnumber', 'portnumber', 'id'],
};

function loadPorts(file) {
  const rows = parseCsv(fs.readFileSync(file, 'utf8'));
  const header = rows.shift().map(norm);
  const col = {};
  for (const [field, names] of Object.entries(COLUMN_NAMES)) {
    col[field] = header.findIndex((h) => names.includes(h));
  }
  for (const required of ['name', 'lat', 'lng']) {
    if (col[required] < 0) {
      throw new Error(`${file}: no ${required} column found. Header is: ${header.join(', ')}`);
    }
  }
  const get = (r, field) => (col[field] >= 0 ? (r[col[field]] ?? '').trim() : '');

  const ports = [];
  let skipped = 0;
  for (const r of rows) {
    const latText = get(r, 'lat');
    const lngText = get(r, 'lng');
    const lat = Number(latText);
    const lng = Number(lngText);
    if (!get(r, 'name') || latText === '' || lngText === '' || !Number.isFinite(lat) || !Number.isFinite(lng) || Math.abs(lat) > 90 || Math.abs(lng) > 180) {
      skipped += 1;
      continue;
    }
    ports.push({
      number: get(r, 'number'),
      name: get(r, 'name'),
      country: get(r, 'country'),
      locode: get(r, 'locode'),
      size: get(r, 'size'),
      lat,
      lng,
    });
  }
  console.log(`Loaded ${ports.length} ports from ${file}${skipped ? ` (skipped ${skipped} rows without a name or valid coordinates)` : ''}`);
  return ports;
}

const listOption = (value) => (value ? value.split(',').map((v) => v.trim().toLowerCase()).filter(Boolean) : null);

function filterPorts(ports, opts) {
  let result = ports;
  const countries = listOption(opts.country);
  const sizes = listOption(opts.size);
  if (countries) result = result.filter((p) => countries.includes(p.country.toLowerCase()));
  if (sizes) result = result.filter((p) => sizes.includes(p.size.toLowerCase()));
  if (opts.limit) result = result.slice(0, Number(opts.limit));
  result.forEach((p, i) => { p.key = p.number || String(i + 1); });
  return result;
}

// Finds a port by UN/LOCODE, port number or name (case-insensitive).
function findPort(ports, query) {
  const q = norm(query);
  const matches = ports.filter((p) => norm(p.locode) === q || norm(p.number) === q || norm(p.name) === q);
  if (matches.length === 0) return null;
  if (matches.length > 1) {
    console.warn(`  "${query}" matches ${matches.length} ports; using ${describe(matches[0])}. Use the UN/LOCODE or port number to choose another.`);
  }
  return matches[0];
}

const describe = (p) => `${p.name}${p.country ? ` (${p.country})` : ''}`;

// ---------- which routes ----------

function planRoutes(ports, opts) {
  if (opts.pairs) {
    const rows = parseCsv(fs.readFileSync(opts.pairs, 'utf8'));
    if (rows.length && /origin|from|source/i.test(rows[0][0])) rows.shift(); // header row
    const pairs = [];
    for (const [a, b] of rows) {
      const from = findPort(ports, a);
      const to = findPort(ports, b);
      if (!from || !to) console.warn(`  skipping "${a}" -> "${b}": port not found`);
      else pairs.push([from, to]);
    }
    return { pairs, label: `${pairs.length} pairs from ${opts.pairs}` };
  }
  if (opts.from) {
    const origin = findPort(ports, opts.from);
    if (!origin) throw new Error(`--from "${opts.from}": no port with that name, UN/LOCODE or number`);
    const pairs = ports.filter((p) => p !== origin).map((p) => [origin, p]);
    return { pairs, label: `${describe(origin)} to ${pairs.length} other ports` };
  }
  // Every pair once (sea distance is the same both ways).
  const pairs = [];
  for (let i = 0; i < ports.length; i += 1) {
    for (let j = i + 1; j < ports.length; j += 1) pairs.push([ports[i], ports[j]]);
  }
  return { pairs, label: `every pair of ${ports.length} ports` };
}

// ---------- routing ----------

function haversineNm([lng1, lat1], [lng2, lat2]) {
  const r = Math.PI / 180;
  const dLat = (lat2 - lat1) * r;
  const dLng = (lng2 - lng1) * r;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin(dLng / 2) ** 2;
  return (2 * EARTH_RADIUS_KM * Math.asin(Math.sqrt(h))) / KM_PER_NM;
}

const point = (p) => ({ type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: [p.lng, p.lat] } });
const round = (n, d = 3) => (Number.isFinite(n) ? Number(n.toFixed(d)) : n);

function seaRoute(searoute, from, to) {
  try {
    const route = searoute(point(from), point(to)); // default units: nautical miles
    return route && route.geometry && route.geometry.coordinates ? route : null;
  } catch (err) {
    return { error: err.message };
  }
}

function formatDuration(ms) {
  const s = Math.round(ms / 1000);
  if (s < 90) return `${s} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}

// ---------- network export (optional) ----------

function findJsonFiles(dir, found = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== 'node_modules') findJsonFiles(full, found);
    } else if (/\.(geo)?json$/i.test(entry.name) && entry.name !== 'package.json') {
      found.push(full);
    }
  }
  return found;
}

function exportNetwork() {
  const packageDir = path.dirname(require.resolve('searoute-js/package.json'));
  const out = csvWriter('searoute_network.csv', ['source_file', 'feature_id', 'geometry_type', 'seq', 'lng', 'lat', 'properties']);
  for (const jsonFile of findJsonFiles(packageDir)) {
    let geojson;
    try { geojson = JSON.parse(fs.readFileSync(jsonFile, 'utf8')); } catch { continue; }
    if (!geojson || geojson.type !== 'FeatureCollection' || !Array.isArray(geojson.features)) continue;
    const source = path.relative(packageDir, jsonFile);
    geojson.features.forEach((feature, fi) => {
      const g = feature.geometry;
      if (!g) return;
      const lines = g.type === 'Point' ? [[g.coordinates]]
        : g.type === 'LineString' ? [g.coordinates]
          : g.type === 'MultiLineString' ? g.coordinates : [];
      const props = feature.properties && Object.keys(feature.properties).length ? feature.properties : '';
      let seq = 0;
      for (const line of lines) {
        for (const [lng, lat] of line) {
          seq += 1;
          out.write([source, feature.id ?? fi, g.type, seq, lng, lat, seq === 1 ? props : '']);
        }
      }
    });
  }
  out.close();
}

// ---------- main ----------

function main() {
  const opts = parseArgs(process.argv.slice(2));

  const allPorts = opts.portsFile ? loadPorts(opts.portsFile) : SAMPLE_PORTS.map((p) => ({ ...p }));
  if (!opts.portsFile) console.log('No ports file given: using the 6 built-in sample ports.');
  const ports = filterPorts(allPorts, opts);
  console.log(`Using ${ports.length} ports after filters.`);

  const { pairs, label } = planRoutes(ports, opts);
  console.log(`Planned ${pairs.length.toLocaleString()} routes: ${label}`);
  if (pairs.length === 0) return;

  console.log('Loading searoute-js (the first load can take a while)...');
  const loadStart = Date.now();
  const searoute = require('searoute-js');
  console.log(`  loaded in ${formatDuration(Date.now() - loadStart)}`);

  // Big job: time a few routes first and stop unless --yes was given.
  if (pairs.length > BIG_JOB && !opts.flags.has('yes')) {
    const sample = pairs.slice(0, 5);
    const t0 = Date.now();
    sample.forEach(([a, b]) => seaRoute(searoute, a, b));
    const perRoute = (Date.now() - t0) / sample.length;
    console.log(`\nThis is a big job: ${pairs.length.toLocaleString()} routes at about ${Math.round(perRoute)} ms each`
      + ` = roughly ${formatDuration(perRoute * pairs.length)}.`);
    console.log('Narrow it with --from, --pairs, --country, --size or --limit, or add --yes to run it anyway.');
    return;
  }

  const withWaypoints = opts.flags.has('waypoints') || pairs.length <= AUTO_WAYPOINTS_MAX;

  console.log('Writing CSV files:');
  const portsOut = csvWriter('ports_used.csv', ['port_key', 'name', 'country', 'unlocode', 'harbor_size', 'lat', 'lng']);
  ports.forEach((p) => portsOut.write([p.key, p.name, p.country, p.locode, p.size, p.lat, p.lng]));
  portsOut.close();

  const summary = csvWriter('routes_summary.csv', [
    'route_id', 'origin_key', 'origin', 'origin_country', 'destination_key', 'destination', 'destination_country',
    'sea_nm', 'sea_km', 'searoute_length', 'searoute_units', 'origin_snap_distance', 'destination_snap_distance',
    'waypoints', 'great_circle_nm', 'sea_vs_great_circle', 'eta_hours', 'eta_days', 'status',
  ]);
  const waypoints = withWaypoints
    ? csvWriter('route_waypoints.csv', ['route_id', 'origin', 'destination', 'seq', 'lng', 'lat', 'segment_nm', 'cumulative_nm', 'cumulative_km'])
    : null;
  if (!withWaypoints) console.log(`  skipping route_waypoints.csv for ${pairs.length.toLocaleString()} routes (add --waypoints to include it)`);

  const start = Date.now();
  let found = 0;
  pairs.forEach(([from, to], index) => {
    const routeId = index + 1;
    const greatCircleNm = haversineNm([from.lng, from.lat], [to.lng, to.lat]);
    const ends = [routeId, from.key, from.name, from.country, to.key, to.name, to.country];
    const route = seaRoute(searoute, from, to);

    if (!route || route.error) {
      summary.write([...ends, '', '', '', '', '', '', 0, round(greatCircleNm, 1), '', '', '',
        route?.error ? `error: ${route.error}` : 'no route found']);
    } else {
      const coords = route.geometry.coordinates;
      const props = route.properties || {};
      let cumulative = 0;
      coords.forEach(([lng, lat], i) => {
        const segment = i === 0 ? 0 : haversineNm(coords[i - 1], coords[i]);
        cumulative += segment;
        waypoints?.write([routeId, from.name, to.name, i + 1, round(lng, 6), round(lat, 6),
          round(segment), round(cumulative), round(cumulative * KM_PER_NM)]);
      });
      const hours = cumulative / opts.speed;
      summary.write([...ends, round(cumulative, 1), round(cumulative * KM_PER_NM, 1),
        round(props.length), props.units ?? '', round(props.originSnapDistance), round(props.destinationSnapDistance),
        coords.length, round(greatCircleNm, 1), round(cumulative / greatCircleNm, 2),
        round(hours, 1), round(hours / 24, 2), 'ok']);
      found += 1;
    }

    if (routeId % 100 === 0 || routeId === pairs.length) {
      const elapsed = Date.now() - start;
      const left = (elapsed / routeId) * (pairs.length - routeId);
      console.log(`  ${routeId.toLocaleString()} / ${pairs.length.toLocaleString()} routes`
        + ` (${formatDuration(elapsed)} elapsed${routeId < pairs.length ? `, about ${formatDuration(left)} left` : ''})`);
    }
  });

  summary.close();
  waypoints?.close();
  console.log(`Routes found: ${found} of ${pairs.length}. Speed used for ETA: ${opts.speed} knots.`);

  if (opts.flags.has('network')) exportNetwork();
  console.log('Done.');
}

try {
  main();
} catch (err) {
  console.error(`Error: ${err.message}`);
  process.exitCode = 1;
}
=======
   const searoute = require('searoute-js');

   // GeoJSON points: coordinates are [longitude, latitude]
   const origin = {
     type: 'Feature', properties: {},
     geometry: { type: 'Point', coordinates: [-122.3393, 47.6026] }, // Seattle Port
   };
   const destination = {
     type: 'Feature', properties: {},
     geometry: { type: 'Point', coordinates: [-118.276, 33.7406] }, // LA Port
   };

   // Third argument is optional: 'nm' (default), 'miles', 'kilometers', 'degrees', 'radians'
   const route = searoute(origin, destination, 'miles');

   console.log('Length:', route.properties.length, route.properties.units);
   console.log('Points:', route.geometry.coordinates.length);
   console.log(JSON.stringify(route).slice(0, 300));
>>>>>>> 8b2991f33a733295130685330644be58b18be74b
