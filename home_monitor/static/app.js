import { installUpdates } from '/pwa-update.js';
'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let status, events = [], shownEvents = [], page = '', busy = false, toastTimer, modalTimer;
let authorized = false, pendingWrites = 0, installPrompt = null;
let pushState = null, notificationBusy = false;
const modal = $('#modal');
const labels = {known:'Recognised', unknown:'Unrecognised face', unavailable:'Face unavailable'};

async function api(path, options = {}) {
  const write = options.method && !['GET','HEAD'].includes(options.method);
  if (write) pendingWrites++;
  try {
  const response = await fetch('/api' + path, {cache:'no-store', ...options});
  if (!response.ok) {
    let message = 'The request could not be completed.';
    try { const body = await response.json(); message = typeof body.detail === 'string' ? body.detail : 'Please check the values and try again.'; } catch {}
    if (response.status === 401) showActivation('This device needs an invitation, or its access has been revoked.');
    const error = new Error(message); error.status = response.status; throw error;
  }
  return response.json();
  } finally { if (write) pendingWrites--; }
}
const json = (method, body) => ({method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
function toast(message) { clearTimeout(toastTimer); $('#toast').textContent = message; $('#toast').hidden = false; toastTimer = setTimeout(() => $('#toast').hidden = true, 4500); }
function relative(timestamp) { if (!timestamp) return 'Waiting for first snapshot'; const n = Math.max(0, Date.now()/1000 - timestamp); return n < 10 ? 'Just now' : n < 60 ? `${Math.floor(n)}s ago` : n < 3600 ? `${Math.floor(n/60)}m ago` : n < 86400 ? `${Math.floor(n/3600)}h ago` : `${Math.floor(n/86400)}d ago`; }
function fullDate(timestamp) { return new Date(timestamp*1000).toLocaleString([], {dateStyle:'medium', timeStyle:'short'}); }
function bytes(value) { return value < 1024*1024 ? `${(value/1024).toFixed(0)} KB` : value < 1024**3 ? `${(value/1024**2).toFixed(1)} MB` : `${(value/1024**3).toFixed(2)} GB`; }
const cameraName = id => status?.cameras.find(camera => camera.id === id)?.name || id;
function empty(title, text, action = '') { return `<div class="empty-state"><div class="empty-icon">◎</div><h3>${esc(title)}</h3><p>${esc(text)}</p>${action}</div>`; }
function openModal(title, html) { clearInterval(modalTimer); $('#modal-title').textContent = title; $('#modal-body').innerHTML = html; if (!modal.open) modal.showModal(); }
function closeModal() { clearInterval(modalTimer); modal.close(); }

function captureInvitation() {
  const code = new URLSearchParams(location.hash.slice(1)).get('invite');
  if (!code || !/^[A-Za-z2-9-]{12,16}$/.test(code)) return false;
  $('#invite-code').value = code;
  sessionStorage.setItem('home-monitor:invite', code);
  history.replaceState(null, '', '/#overview');
  return true;
}

function showActivation(message = '') {
  authorized = false; status = null; events = []; shownEvents = [];
  closeModal(); $('#dashboard').hidden = true; $('#activation').hidden = false;
  for (const id of ['camera-grid','recent-events','all-events','people-grid','camera-settings']) $('#'+id).replaceChildren();
  $('#activation-description').textContent = message || 'Activate this device with an invitation from your administrator to view your home.';
}

async function boot() {
  const invitation = captureInvitation();
  if (!$('#invite-code').value) $('#invite-code').value = sessionStorage.getItem('home-monitor:invite') || '';
  try {
    const session = await api('/session');
    $('#cancel-activation').hidden = !(session.owner && session.authorized);
    // The kit may reload on first install. Keep an explicit invitation open
    // across that reload instead of silently returning to owner access.
    if (invitation || $('#invite-code').value || !session.authorized) { showActivation(); return; }
    authorized = true; $('#activation').hidden = true; $('#dashboard').hidden = false;
    await refresh(); await navigate();
  } catch { showActivation('Home is currently unreachable. Check your internet connection and try again. Camera recording continues independently.'); }
}

$('#activation-form').addEventListener('submit', async event => {
  event.preventDefault(); const form = event.currentTarget, button = $('button[type=submit]', form);
  button.disabled = true; $('#activation-error').hidden = true;
  try {
    await api('/invites/redeem', json('POST', {code:$('#invite-code').value.trim()}));
    sessionStorage.removeItem('home-monitor:invite'); $('#invite-code').value = ''; delete form.dataset.dirty;
    authorized = true; $('#activation').hidden = true; $('#dashboard').hidden = false;
    await refresh(); await navigate(); toast('This device is activated. Welcome home.');
  } catch (error) { $('#activation-error').textContent = error.message; $('#activation-error').hidden = false; }
  finally { button.disabled = false; }
});
$('#cancel-activation').addEventListener('click', () => {
  sessionStorage.removeItem('home-monitor:invite'); $('#invite-code').value = '';
  delete $('#activation-form').dataset.dirty; boot();
});

window.addEventListener('beforeinstallprompt', event => { event.preventDefault(); installPrompt = event; });
window.addEventListener('appinstalled', () => { installPrompt = null; $$('.install-trigger').forEach(button => button.hidden = true); toast('Home Monitor is installed.'); });
$$('.install-trigger').forEach(button => button.addEventListener('click', async () => {
  if (installPrompt) {
    const prompt = installPrompt; installPrompt = null; await prompt.prompt(); await prompt.userChoice;
  } else {
    openModal('Install Home Monitor', '<p class="modal-note"><strong>iPhone or iPad:</strong> open this address in Safari, tap Share, then Add to Home Screen.</p><p class="modal-note"><strong>Android:</strong> open the browser menu and choose Install app or Add to Home screen.</p><p class="modal-note"><strong>Desktop:</strong> use the install icon in your browser’s address bar.</p><p class="modal-note">If asked to activate the installed app, enter the invitation code there. Enable notifications in Settings to receive alerts with the app closed.</p>');
  }
}));
if (matchMedia('(display-mode: standalone)').matches || navigator.standalone) $$('.install-trigger').forEach(button => button.hidden = true);
document.addEventListener('input', event => { const form = event.target.closest('form'); if (form) form.dataset.dirty = 'true'; });
document.addEventListener('change', event => { const form = event.target.closest('form'); if (form) form.dataset.dirty = 'true'; });
$('#close-modal').addEventListener('click', closeModal);
modal.addEventListener('close', () => clearInterval(modalTimer));
modal.addEventListener('click', event => { if (event.target === modal) { const r = modal.getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) closeModal(); } });

function eventTitle(event) {
  const names = [...new Set(event.faces.map(face => face.name).filter(Boolean))];
  if (names.length) return names.join(', ') + (event.state === 'unknown' ? ' + unrecognised face' : event.state === 'unavailable' ? ' · another identity unavailable' : ' seen');
  return event.state === 'unknown' ? 'An unrecognised face was seen' : 'A person was detected';
}
// Group presentation only: recognition results and original snapshots stay separate.
function groupActivity(list) {
  const groups = [], latest = new Map();
  for (const event of [...list].sort((a, b) => b.last_seen - a.last_seen)) {
    let group = latest.get(event.camera_id);
    if (!group || group.first_seen - event.last_seen > 600) {
      group = {camera_id:event.camera_id, first_seen:event.first_seen, last_seen:event.last_seen, events:[]};
      groups.push(group); latest.set(event.camera_id, group);
    }
    group.first_seen = Math.min(group.first_seen, event.first_seen);
    group.events.push(event);
  }
  return groups;
}
function renderTimeline(target, list) {
  const opened = new Set($$('details[open]', target).map(item => item.dataset.group));
  if (!list.length) { renderEvents(target, list); return; }
  target.innerHTML = groupActivity(list).map(group => {
    const key = group.events[0].id;
    const unknown = group.events.filter(event => event.state === 'unknown').length;
    const start = fullDate(group.first_seen), end = new Date(group.last_seen*1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    return `<details class="activity-period" data-group="${key}" ${group.events.some(event => opened.has(event.id)) ? 'open' : ''}><summary><span><strong>${esc(cameraName(group.camera_id))}</strong><span class="event-meta">${esc(start)} – ${esc(end)}</span></span><span>${group.events.length} ${group.events.length === 1 ? 'event' : 'events'}${unknown ? ` · ${unknown} with unrecognised faces` : ''}</span></summary><div class="period-events"></div></details>`;
  }).join('');
  const groups = groupActivity(list);
  $$('.activity-period', target).forEach((details, index) => {
    const fill = () => { if (details.open && !details.dataset.loaded) { renderEvents($('.period-events', details), groups[index].events); details.dataset.loaded = 'true'; } };
    details.addEventListener('toggle', fill); fill();
  });
}

function renderEvents(target, list, overview = false) {
  target.innerHTML = list.length ? list.map(event => `<button class="event-row" data-event="${event.id}"><img class="event-thumb" src="/api/events/${event.id}/image?v=${event.image_at}" alt="Event snapshot" loading="lazy"><div class="event-main"><div class="event-title">${esc(eventTitle(event))}</div><div class="event-meta">${esc(cameraName(event.camera_id))} · ${event.person_count} ${event.person_count === 1 ? 'person' : 'people'}${event.corrected ? ' · Reviewed' : ''}</div></div><span class="pill ${event.state}">${labels[event.state]}</span><time class="event-time" datetime="${new Date(event.last_seen*1000).toISOString()}" title="${esc(fullDate(event.last_seen))}">${relative(event.last_seen)}</time></button>`).join('') : empty(overview ? 'Home, in the quiet moments.' : 'No activity here yet.', overview ? 'When a person is detected, their snapshot and recognition result will appear here.' : 'New person detections will appear here. Try another filter if you are looking for an older event.');
  $$('[data-event]', target).forEach(button => button.addEventListener('click', () => showEvent(list.find(event => event.id === button.dataset.event))));
}

function renderOverview() {
  const counts = status.last_24h;
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const stats = [
    ['Cameras online', `${status.cameras.filter(c => c.online).length} <small>/ ${status.cameras.length}</small>`, 'Connected through the collector', '▣'],
    ['Activity events', total, 'In the last 24 hours', '◷'],
    ['Recognised events', counts.known || 0, 'Matched to your face library', '♙'],
    ['Unrecognised events', counts.unknown || 0, 'Clear faces without a match', '◎']
  ];
  $('#stats').innerHTML = stats.map(([name, value, note, icon]) => `<div class="stat"><div class="stat-top"><span>${name}</span><span class="stat-icon">${icon}</span></div><div class="stat-number">${value}</div><div class="stat-note">${note}</div></div>`).join('');
  $('#camera-total').textContent = status.cameras.length;
  $('#enrollment-banner').hidden = status.recognition_ready;
  $('#paused-banner').hidden = status.settings.monitoring_enabled;
  $('#sampling-label').textContent = status.settings.monitoring_enabled ? 'Motion-aware snapshots' : 'Paused';
  const existing = $$('.camera-card', $('#camera-grid'));
  if (existing.length !== status.cameras.length) {
    $('#camera-grid').innerHTML = status.cameras.map(camera => `<article class="camera-card" data-camera="${camera.id}"><button class="camera-view icon-button-reset" aria-label="Open ${esc(camera.name)}"><span class="camera-placeholder">Waiting for a snapshot…</span><span class="camera-status"></span></button><div class="camera-info"><div><div class="camera-name"></div><div class="camera-meta"></div></div><button class="camera-open" aria-label="Enlarge snapshot">↗</button></div></article>`).join('');
    $$('.camera-card').forEach(card => { $('.camera-view', card).addEventListener('click', () => showCamera(card.dataset.camera)); $('.camera-open', card).addEventListener('click', () => showCamera(card.dataset.camera)); });
  }
  status.cameras.forEach(camera => {
    const card = $(`.camera-card[data-camera="${camera.id}"]`);
    $('.camera-name', card).textContent = camera.name;
    $('.camera-meta', card).textContent = camera.error ? (camera.error === 'camera_auth' ? 'Check camera credentials' : 'Connection interrupted · retrying') : camera.last_seen ? `${camera.width || '—'} × ${camera.height || '—'} · ${relative(camera.image_at || camera.last_seen)}` : 'Connecting to camera';
    const badge = $('.camera-status', card);
    badge.textContent = !status.settings.monitoring_enabled ? 'PAUSED' : camera.online ? '● CONNECTED' : '○ OFFLINE';
    badge.classList.toggle('offline', !camera.online);
    let image = $('img', card);
    if (camera.width && status.settings.monitoring_enabled) {
      if (!image) { image = document.createElement('img'); image.alt = `${camera.name} snapshot`; image.addEventListener('error', () => { image.hidden = true; }); image.addEventListener('load', () => { image.hidden = false; }); $('.camera-view', card).prepend(image); }
      const version = String(camera.image_at || Math.floor(camera.last_seen/15));
      if (image.dataset.version !== version) { image.src = `/api/cameras/${camera.id}/image?v=${encodeURIComponent(version)}`; image.dataset.version = version; }
    } else if (image) image.remove();
  });
  renderEvents($('#recent-events'), events.slice(0, 6), true);
}

function renderPeople() {
  $('#people-count').textContent = status.people.length;
  $('#people-grid').innerHTML = status.people.length ? status.people.map(person => `<article class="person-card">${person.cover ? `<img class="person-photo" src="/api/samples/${person.cover}/image" alt="${esc(person.name)}">` : '<div class="person-photo">♙</div>'}<h3>${esc(person.name)}</h3><p>${person.sample_count} ${person.sample_count === 1 ? 'face sample' : 'face samples'}${person.sample_count === 0 ? ' · Needs a photo' : ''}</p><button class="button secondary" data-person="${person.id}">Manage photos</button></article>`).join('') : empty('Who makes your house a home?', 'Add a person and a few clear photos. Their name can then appear in activity events.', '<button class="button" id="empty-add-person">＋ Add person</button>');
  $$('[data-person]').forEach(button => button.addEventListener('click', () => managePerson(button.dataset.person)));
  $('#empty-add-person')?.addEventListener('click', addPerson);
}

function renderSettings() {
  const form = $('#settings-form');
  Object.entries(status.settings).forEach(([key, value]) => { const input = form.elements[key]; if (input) { if (input.type === 'checkbox') input.checked = value; else input.value = value; } });
  $('#camera-settings').innerHTML = status.cameras.map(camera => `<form class="camera-rename" data-camera="${camera.id}"><label>Display name<input name="name" value="${esc(camera.name)}" maxlength="60" required></label><button type="submit" class="button secondary">Rename</button></form>`).join('');
  $$('.camera-rename').forEach(form => form.addEventListener('submit', async event => { event.preventDefault(); try { await api(`/cameras/${form.dataset.camera}`, json('PATCH', {name:form.elements.name.value.trim()})); delete form.dataset.dirty; await refresh(); toast('Camera name saved.'); } catch (error) { toast(error.message); } }));
  $('#storage-bar').style.width = `${Math.min(100, status.storage_bytes/(status.settings.storage_limit_mb*1024**2)*100)}%`;
  $('#storage-detail').textContent = `${bytes(status.storage_bytes)} of ${bytes(status.settings.storage_limit_mb*1024**2)} event storage · ${bytes(status.disk_free_bytes)} disk space free on the server`;
  renderNotifications();
}

function pushSupported() {
  return 'Notification' in window && 'PushManager' in window && 'serviceWorker' in navigator;
}

async function renderNotifications() {
  const button = $('#notifications-button');
  button.disabled = notificationBusy || !pushSupported();
  if (!pushSupported()) {
    $('#notifications-status').textContent = 'Install Home Monitor on your Home Screen and use a browser that supports Web Push.';
    $('#test-notifications').hidden = true;
    return;
  }
  try {
    pushState = await api('/push/status');
    button.textContent = pushState.subscribed ? 'Disable notifications' : 'Enable notifications';
    $('#test-notifications').hidden = !pushState.subscribed;
    $('#notifications-status').textContent = pushState.subscribed ? (pushState.last_error ? 'Delivery is temporarily unavailable. Try a test notification.' : 'Enabled for this device, including when the app is closed.') : Notification.permission === 'denied' ? 'Notifications are blocked in your browser settings.' : 'Not enabled on this device yet.';
  } catch (error) { $('#notifications-status').textContent = error.message; }
}

function publicKeyBytes(value) {
  const raw = atob(value.replace(/-/g,'+').replace(/_/g,'/') + '='.repeat((4-value.length%4)%4));
  return Uint8Array.from(raw, character => character.charCodeAt(0));
}

async function loadEvents(append = false) {
  const params = new URLSearchParams();
  if ($('#camera-filter').value) params.set('camera', $('#camera-filter').value);
  if ($('#state-filter').value) params.set('state', $('#state-filter').value);
  if (append && shownEvents.length) params.set('before', shownEvents.at(-1).last_seen);
  const list = await api('/events?' + params);
  shownEvents = append ? [...shownEvents, ...list] : list;
  renderTimeline($('#all-events'), shownEvents);
  $('#load-more').hidden = list.length < 60;
}

async function refresh() {
  if (busy || !authorized) return;
  busy = true;
  try {
    [status, events] = await Promise.all([api('/status'), api('/events?limit=6')]);
    renderAlertMode();
    $('#connection').textContent = '● Private & connected';
    $('#error-banner').hidden = true;
    renderOverview();
    $('#people-count').textContent = status.people.length;
    if (page === 'people') renderPeople();
    if (page === 'events' && shownEvents.length <= 60) await loadEvents();
  } catch (error) {
    if (error.status === 401) return;
    $('#connection').textContent = 'Connection interrupted';
    $('#error-banner').textContent = error.message + ' Retrying automatically.';
    $('#error-banner').hidden = false;
  } finally { busy = false; }
}

async function navigate() {
  if (!authorized) return;
  page = ['overview','events','people','settings'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview';
  $$('.page').forEach(section => section.hidden = section.id !== 'page-' + page);
  $$('nav a').forEach(link => link.classList.toggle('active', link.dataset.page === page));
  const name = {overview:'Overview',events:'Activity',people:'People',settings:'Settings'}[page];
  $('#breadcrumb').textContent = `Your home / ${name}`;
  document.title = `${name} · Home Monitor`;
  if (!status) return;
  if (page === 'people') renderPeople();
  if (page === 'settings') renderSettings();
  if (page === 'events') {
    const filter = $('#camera-filter'), selected = filter.value;
    filter.innerHTML = '<option value="">All cameras</option>' + status.cameras.map(camera => `<option value="${camera.id}">${esc(camera.name)}</option>`).join('');
    filter.value = selected;
    try { await loadEvents(); } catch (error) { toast(error.message); }
  }
}

function showCamera(cameraId) {
  const camera = status.cameras.find(c => c.id === cameraId);
  if (!camera.width || !status.settings.monitoring_enabled) { toast(status.settings.monitoring_enabled ? 'Waiting for this camera’s first snapshot.' : 'Monitoring is paused.'); return; }
  openModal(camera.name, `<img class="modal-image" id="camera-modal-image" src="/api/cameras/${camera.id}/image?v=${Date.now()}" alt="${esc(camera.name)}"><p class="modal-note">Sampled snapshots, refreshed every 5 seconds. Open your camera app for continuous live video and SD recordings.</p>`);
  modalTimer = setInterval(() => { const image = $('#camera-modal-image'); if (image && document.visibilityState === 'visible') image.src = `/api/cameras/${camera.id}/image?v=${Date.now()}`; }, 5000);
}

function showEvent(event) {
  const options = '<option value="">Choose a person…</option>' + status.people.map(person => `<option value="${person.id}">${esc(person.name)}</option>`).join('');
  openModal('A moment at home', `<img class="modal-image" src="/api/events/${event.id}/image?v=${event.image_at}" alt="Event snapshot"><div class="modal-details"><span class="pill ${event.state}">${labels[event.state]}</span><span>${esc(cameraName(event.camera_id))}</span><span>${esc(fullDate(event.first_seen))}</span><span>${event.person_count} ${event.person_count === 1 ? 'person' : 'people'}</span></div>${event.faces.length ? '<h3>Faces in this moment</h3>' : '<p class="modal-note">A person was detected, but no face was clear enough to identify. This does not mean the person is unfamiliar.</p>'}${event.faces.map((face, index) => `<div class="face-row"><img src="/api/events/${event.id}/faces/${index}" alt="Detected face"><div><strong>${esc(face.name || (face.usable ? 'Unrecognised face' : 'Face not clear enough'))}</strong><p>${face.name ? 'Verify the name before using this as a training sample.' : face.usable ? 'Assign a person to improve future recognition.' : 'Use a clearer portrait to enrol this person.'}</p>${status.people.length ? `<div class="face-actions"><select aria-label="Identify face ${index+1}" data-face-select="${index}">${options}</select><button class="button secondary" data-assign="${index}">Correct label</button>${face.usable ? `<button class="button" data-train="${index}">Label & learn</button>` : ''}</div>` : '<a class="text-link" href="#people" id="event-add-person">Add a person to your library ↗</a>'}</div></div>`).join('')}<p class="modal-note">“Label & learn” adds this face to the selected person’s library. Only use clear, correctly identified faces.</p><div class="modal-actions"><button class="button danger" id="delete-event">Delete this event</button><button class="button secondary" id="done-event">Done</button></div>`);
  $('#event-add-person')?.addEventListener('click', closeModal);
  $('#done-event').addEventListener('click', closeModal);
  const assign = async (index, train, button) => {
    const personId = $(`[data-face-select="${index}"]`).value;
    if (!personId) { toast('Choose a person first.'); return; }
    button.disabled = true;
    try { await api(`/events/${event.id}/assign`, json('POST', {person_id:personId,face_index:Number(index),add_sample:train,image_at:event.image_at})); closeModal(); await refresh(); toast(train ? 'Face labelled and added to the library.' : 'Event label corrected.'); } catch (error) { toast(error.message); button.disabled = false; }
  };
  $$('[data-assign]').forEach(button => button.addEventListener('click', () => assign(button.dataset.assign, false, button)));
  $$('[data-train]').forEach(button => button.addEventListener('click', () => assign(button.dataset.train, true, button)));
  $('#delete-event').addEventListener('click', async () => { if (!confirm('Delete this event and its images?')) return; try { await api(`/events/${event.id}`, {method:'DELETE'}); closeModal(); await refresh(); toast('Event deleted.'); } catch (error) { toast(error.message); } });
}

function addPerson() {
  openModal('Add a familiar face', '<form id="person-form"><label>Name<input name="name" required maxlength="60" placeholder="How should this person appear?" autocomplete="off"></label><label class="file-label">Face photos (optional)<input name="photos" type="file" accept="image/jpeg,image/png" multiple><small>Choose clear photos with exactly one face. You can also label a clear face from an activity event later.</small></label><p class="modal-note">Add people who agree to be enrolled. Their face samples stay on your recognition server.</p><div class="modal-actions"><button class="button" type="submit">Add person</button></div></form>');
  const form = $('#person-form');
  form.addEventListener('submit', async event => {
    event.preventDefault(); const button = $('button[type=submit]', form); button.disabled = true;
    let person;
    try {
      person = await api('/people', json('POST', {name:form.elements.name.value.trim()}));
      let added = 0;
      for (const file of form.elements.photos.files) { button.textContent = `Adding photo ${++added}…`; await api(`/people/${person.id}/samples`, {method:'POST',headers:{'Content-Type':file.type},body:file}); }
      closeModal(); await refresh(); toast('Person added. Add a few varied photos for better recognition.');
    } catch (error) { if (person) { closeModal(); await refresh(); toast('Person saved. ' + error.message); } else { toast(error.message); button.disabled = false; button.textContent = 'Add person'; } }
  });
}

async function managePerson(id) {
  try {
    const person = status.people.find(p => p.id === id), samples = await api(`/people/${id}/samples`);
    openModal(person.name, `<form id="person-rename"><label>Name<input name="name" value="${esc(person.name)}" maxlength="60" required></label><div class="modal-actions"><button class="button secondary" type="submit">Save name</button></div></form><h3>Face samples · ${samples.length}/50</h3><div class="sample-grid">${samples.map(sample => `<div class="sample-item"><img src="/api/samples/${sample.id}/image" alt="Enrolled face sample"><button data-remove-sample="${sample.id}" aria-label="Delete sample">×</button></div>`).join('')}</div><form id="sample-form"><label class="file-label">Add clear face photos<input type="file" name="photos" accept="image/jpeg,image/png" multiple required><small>One face per photo. Try different lighting and slight changes in angle.</small></label><div class="modal-actions"><button class="button" type="submit">Add photos</button></div></form><div class="modal-actions"><button class="button danger" id="delete-person">Remove person & face samples</button></div>`);
    $('#person-rename').addEventListener('submit', async event => { event.preventDefault(); try { await api(`/people/${id}`, json('PATCH', {name:event.currentTarget.elements.name.value.trim()})); await refresh(); toast('Name saved.'); } catch (error) { toast(error.message); } });
    $$('[data-remove-sample]').forEach(button => button.addEventListener('click', async () => { try { await api(`/samples/${button.dataset.removeSample}`, {method:'DELETE'}); await refresh(); await managePerson(id); toast('Face sample removed.'); } catch (error) { toast(error.message); } }));
    $('#sample-form').addEventListener('submit', async event => {
      event.preventDefault(); const form = event.currentTarget, button = $('button', form); button.disabled = true;
      try { let n = 0; for (const file of form.elements.photos.files) { button.textContent = `Adding photo ${++n}…`; await api(`/people/${id}/samples`, {method:'POST',headers:{'Content-Type':file.type},body:file}); } await refresh(); await managePerson(id); toast('Face samples added.'); } catch (error) { await refresh(); await managePerson(id); toast(error.message); }
    });
    $('#delete-person').addEventListener('click', async () => { if (!confirm(`Remove ${person.name} and all their enrolled face samples?`)) return; try { await api(`/people/${id}`, {method:'DELETE'}); closeModal(); await refresh(); toast('Person and enrolled face data removed.'); } catch (error) { toast(error.message); } });
  } catch (error) { toast(error.message); }
}

$('#add-person').addEventListener('click', addPerson);
$('#notifications-button').addEventListener('click', async () => {
  if (!pushSupported() || notificationBusy) return;
  notificationBusy = true; $('#notifications-button').disabled = true;
  try {
    if (pushState?.subscribed) {
      await api('/push/subscription', {method:'DELETE'});
      const registration = await navigator.serviceWorker.getRegistration();
      const subscription = await registration?.pushManager.getSubscription();
      await subscription?.unsubscribe();
      toast('Notifications disabled on this device.');
    } else {
      // Ask directly from the user's click, before any asynchronous network work.
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') throw new Error('Allow notifications in your browser to enable alerts.');
      await api('/push/device', json('POST', {}));
      const state = await api('/push/status');
      const registration = await Promise.race([navigator.serviceWorker.ready, new Promise((_, reject) => setTimeout(() => reject(new Error('The app is not ready yet. Reopen it and try again.')),10000))]);
      let subscription = await registration.pushManager.getSubscription();
      if (subscription && !state.subscribed) { await subscription.unsubscribe(); subscription = null; }
      subscription ||= await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:publicKeyBytes(state.public_key)});
      await api('/push/subscription', json('POST', subscription.toJSON()));
      toast('Notifications enabled. You can close the app and still receive alerts.');
    }
  } catch (error) { toast(error.message); }
  finally { notificationBusy = false; await renderNotifications(); }
});
$('#test-notifications').addEventListener('click', async () => {
  const button = $('#test-notifications'); button.disabled = true;
  try { await api('/push/test', json('POST', {})); toast('Test queued. Close the app to check background delivery.'); }
  catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});
function renderAlertMode() {
  const until = status.settings.alerts_snoozed_until;
  const snoozed = until > Date.now()/1000;
  $('#alert-mode').textContent = snoozed ? `Alerts snoozed until ${fullDate(until)}` : status.settings.monitoring_enabled ? 'Alerts active · unrecognised faces only' : 'Monitoring paused';
  $('#resume-alerts').hidden = !snoozed;
}
async function snoozeAlerts(minutes) {
  const buttons = [$('#snooze-alerts'), $('#resume-alerts')];
  buttons.forEach(button => button.disabled = true);
  try {
    await api('/alerts/snooze?minutes=' + minutes, {method:'POST'});
    await refresh();
    toast(minutes ? 'Alerts snoozed on all devices. Activity recording continues.' : 'Alerts resumed on all devices.');
  } catch (error) { toast(error.message); }
  finally { buttons.forEach(button => button.disabled = false); }
}
$('#snooze-alerts').addEventListener('click', () => snoozeAlerts(Number($('#snooze-duration').value)));
$('#resume-alerts').addEventListener('click', () => snoozeAlerts(0));
$('#refresh-button').addEventListener('click', refresh);
$('#discard-settings').addEventListener('click', () => { delete $('#settings-form').dataset.dirty; renderSettings(); toast('Unsaved settings discarded.'); });
$('#camera-filter').addEventListener('change', () => loadEvents().catch(error => toast(error.message)));
$('#state-filter').addEventListener('change', () => loadEvents().catch(error => toast(error.message)));
$('#refresh-activity').addEventListener('click', () => loadEvents().catch(error => toast(error.message)));
$('#load-more').addEventListener('click', () => loadEvents(true).catch(error => toast(error.message)));
$('#settings-form').addEventListener('submit', async event => {
  event.preventDefault(); const form = event.currentTarget, button = $('button[type=submit]', form); button.disabled = true;
  try {
    const values = {monitoring_enabled:form.elements.monitoring_enabled.checked,retention_days:Number(form.elements.retention_days.value),storage_limit_mb:Number(form.elements.storage_limit_mb.value),recognition_threshold:Number(form.elements.recognition_threshold.value),alert_cooldown_minutes:Number(form.elements.alert_cooldown_minutes.value)};
    if (values.retention_days < status.settings.retention_days || values.storage_limit_mb < status.settings.storage_limit_mb) { if (!confirm('Apply the smaller retention limit? Older events may be permanently deleted.')) return; }
    await api('/settings', json('PUT', values)); delete form.dataset.dirty; await refresh(); renderSettings(); toast('Settings saved.');
  } catch (error) { toast(error.message); } finally { button.disabled = false; }
});
window.addEventListener('hashchange', () => { if (captureInvitation()) showActivation(); else navigate(); });
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') refresh(); });
window.addEventListener('online', () => { if (authorized) refresh(); else boot(); });
window.addEventListener('offline', () => { if (authorized) { $('#connection').textContent = 'Offline'; $('#error-banner').textContent = 'You’re offline. Reconnect to load current cameras and activity.'; $('#error-banner').hidden = false; } });
navigator.serviceWorker?.addEventListener('message', event => { if (event.data?.type === 'open-activity' && authorized) location.hash = 'events'; });
$('#today').textContent = new Date().toLocaleDateString([], {weekday:'long',day:'numeric',month:'long'}).toUpperCase();
boot();
installUpdates({appName:'Home Monitor',toast,isBusy:() => pendingWrites > 0 || modal.open || $$('form[data-dirty="true"]').some(form => !form.closest('dialog'))});
setInterval(() => { if (document.visibilityState === 'visible' && !modal.open) refresh(); }, 5000);
