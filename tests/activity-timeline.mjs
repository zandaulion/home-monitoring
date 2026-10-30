import assert from 'node:assert/strict';
import fs from 'node:fs';
const source = fs.readFileSync(new URL('../home_monitor/static/timeline.js', import.meta.url), 'utf8');
const {groupActivity, hourlyBins, clippedSpan, dayBounds} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const context = {groupActivity};
const event = (id, camera_id, first_seen, last_seen, state='known') => ({id, camera_id, first_seen, last_seen, state});
const input = [event('older', 'a', 100, 200), event('new', 'a', 800, 900, 'unknown'), event('other', 'b', 850, 950), event('separate', 'a', 1501, 1600)];
const groups = context.groupActivity(input);
assert.equal(groups.length, 3);
assert.equal(groups[2].events.length, 2); // Exactly ten minutes, regardless of recognition result.
assert.equal(groups[2].first_seen, 100);
assert.equal(groups[2].last_seen, 900);
assert.equal(input[0].id, 'older'); // Original ordering and events remain intact.
assert.equal(context.groupActivity([]).length, 0);
const longEvent = context.groupActivity([event('latest','a',1000,1200),event('long','a',100,999),event('early','a',0,50)]);
assert.equal(longEvent.length,1);
assert.equal(longEvent[0].events.length,3);
console.log('Timeline grouping: camera separation, gaps, recognition changes, long events, and ordering passed');

const bins = hourlyBins(input, 0, 7200);
assert.equal(bins.reduce((sum,bin)=>sum+bin.known+bin.unknown+bin.unavailable,0),4);
assert.equal(bins[0].unknown,1);
assert.deepEqual(clippedSpan(0,150,100,200),{left:0,width:50});
assert.equal(clippedSpan(200,220,100,200),null);
assert.deepEqual(clippedSpan(150,150,100,200),{left:50,width:0});
process.env.TZ = 'Europe/Bucharest';
const [fallStart,fallEnd] = dayBounds('2026-10-25');
assert.equal(fallEnd-fallStart,25*3600);
assert.equal(hourlyBins([],fallStart,fallEnd).length,25);
const [springStart,springEnd] = dayBounds('2026-03-29');
assert.equal(springEnd-springStart,23*3600);
console.log('Timeline clipping, hourly counts, and 23/25-hour daylight-saving days passed');
