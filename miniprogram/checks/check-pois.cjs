const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const pois = require('./campus_poi.json');
assert.equal(pois.length, 18);
assert.equal(new Set(pois.map(poi => poi.id)).size, 18);
assert.equal(new Set(pois.map(poi => poi.name)).size, 18);
for (const poi of pois) {
  assert.ok(poi.name && Number.isFinite(poi.x) && Number.isFinite(poi.y));
  assert.ok(Number.isInteger(poi.floors) && poi.floors > 0);
  assert.deepEqual(poi.aliases, []);
}
for (let number = 1; number <= 10; number += 1) {
  assert.equal(pois.find(poi => poi.name === `博雅居${number}号楼`).floors, 6);
}
assert.equal(pois.some(poi => poi.name === '博雅居' || poi.aliases.includes('博雅居')), false);
assert.equal(pois.some(poi => poi.name === '图文信息楼' || poi.aliases.includes('图文信息楼')), false);
assert.deepEqual(pois.find(poi => poi.name === '立功楼'), { id: 'amap_ligong', name: '立功楼', aliases: [], x: 140.8, y: -287.3, floors: 6 });
// Optional source file verifies every retained coordinate and floor without copying source into the repo.
if (process.argv[2]) {
  const bytes = fs.readFileSync(path.resolve(process.argv[2]));
  assert.equal(require('node:crypto').createHash('sha256').update(bytes).digest('hex'), '55069ab63d9f2f87d839584eac73dd09c2fd7ca41fe311f566bf0491087f05fc');
  const named = JSON.parse(bytes.toString('utf8')).buildings.filter(building => building.n);
  assert.equal(named.length, pois.length);
  for (const building of named) {
    const poi = pois.find(item => item.name === building.n);
    assert.ok(poi, building.n);
    assert.deepEqual([poi.x, poi.y], building.c);
    assert.equal(poi.floors, building.f);
  }
}
console.log('PASS: 18 named POIs, 10 distinct Boya buildings, floors, no invented aliases or missing-building coordinates');
