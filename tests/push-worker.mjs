// Exercise the real worker source with no page, DOM, or origin network available.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const handlers = new Map(), shown = [], opened = [];
let windows = [];
const self = {
  location: {origin:'https://monitor.example.invalid'},
  addEventListener: (name, handler) => handlers.set(name, handler),
  registration: {showNotification: async (title, options) => shown.push({title, options})},
  clients: {matchAll:async () => windows,openWindow:async url => opened.push(url)}
};
vm.runInNewContext(fs.readFileSync(new URL('../home_monitor/static/sw.js', import.meta.url),'utf8'), {
  self,URL,Set,Date,importScripts:()=>{},fetch:()=>{throw new Error('Push must not fetch private origin data');}
});
let task;
handlers.get('push')({data:{json:()=>({title:'Synthetic test',body:'Generic alert',expires_at:Date.now()/1000+120})},waitUntil:promise=>task=promise});
await task;
assert.equal(shown.length,1);
assert.equal(shown[0].title,'Synthetic test');
assert.equal(shown[0].options.body,'Generic alert');
handlers.get('push')({data:{json:()=>({expires_at:1})},waitUntil:()=>{throw new Error('Expired alert must not display');}});
assert.equal(shown.length,1);
handlers.get('notificationclick')({notification:{close(){}},waitUntil:promise=>task=promise});
await task;
assert.equal(opened[0],'/#events');
const focused = [];
windows = [{url:'https://monitor.example.invalid/#people',postMessage:value=>focused.push(value.type),focus:async()=>focused.push('focused')}];
handlers.get('notificationclick')({notification:{close(){}},waitUntil:promise=>task=promise});
await task;
assert.deepEqual(focused,['open-activity','focused']);
assert.equal(opened.length,1);
console.log('Worker push display, expiry, closed-app click, and existing-window focus: passed (simulated worker environment)');
