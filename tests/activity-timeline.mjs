import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('../home_monitor/static/app.js', import.meta.url), 'utf8');
const context = vm.createContext({});
vm.runInContext(source.slice(source.indexOf('function groupActivity('), source.indexOf('function renderTimeline(')), context);
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
