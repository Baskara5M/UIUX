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