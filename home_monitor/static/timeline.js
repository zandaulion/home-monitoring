// The graph uses event spans, never infers a person's identity across cameras.
export function groupActivity(list) {
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
export function dayBounds(value) {
  const start = new Date(value + 'T00:00:00');
  const end = new Date(start); end.setDate(end.getDate() + 1);
  return [start.getTime()/1000, end.getTime()/1000];
}
export function hourlyBins(events, start, end) {
  const bins = Array.from({length:Math.ceil((end-start)/3600)}, (_, i) => ({start:start+i*3600, end:Math.min(end,start+(i+1)*3600), person:0, known:0, unknown:0, unavailable:0}));
  for (const event of events) {
    const index = Math.floor((Math.max(start,event.first_seen)-start)/3600);
    if (bins[index]) bins[index][event.state]++;
  }
  return bins;
}
export function clippedSpan(first, last, start, end) {
  if (last < start || first >= end) return null;
  const left = Math.max(start, first), right = Math.min(end, Math.max(first,last));
  return {left:(left-start)/(end-start)*100, width:(right-left)/(end-start)*100};
}

export function createTimeline({api, renderEvents, cameraName, esc, fullDate, toast, getStatus}) {
  const $ = id => document.getElementById(id);
  const time = value => new Date(value*1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  const today = () => { const date = new Date(); return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`; };
  $('timeline-day').value = today();
  let data = null, groups = [], range = null, selectedId = null, details = [], snapshotIds = [], request = 0, selectionRequest = 0, fingerprint = '', loadingMore = false;
  const error = err => toast(err.message);
  function reset() {
    request++; selectionRequest++; data = null; groups = []; range = null; selectedId = null; details = []; snapshotIds = []; fingerprint = '';
    for (const id of ['timeline-overview','timeline-lanes','all-events']) $(id).replaceChildren();
    $('timeline-selection-title').textContent = 'Select an activity period'; $('timeline-selection-note').textContent = ''; $('timeline-more').hidden = true;
  }
  function setRange(center, duration) {
    const size = Math.min(duration, data.end-data.start);
    const start = Math.max(data.start, Math.min(data.end-size, center-size/2));
    range = [start, start+size];
  }
  function spanStyle(first,last,start=range[0],end=range[1]) {
    const span = clippedSpan(first,last,start,end);
    return span ? `data-left="${span.left}" data-width="${span.width}"` : null;
  }
  function applyPositions(root) {
    root.querySelectorAll('[data-left],[data-height]').forEach(mark=>{
      if(mark.dataset.left!==undefined)mark.style.left=mark.dataset.left+'%';
      if(mark.dataset.width!==undefined)mark.style.width=mark.dataset.width+'%';
      if(mark.dataset.height!==undefined)mark.style.height=mark.dataset.height+'%';
    });
  }
  function drawOverview() {
    const bins = hourlyBins(data.events,data.start,data.end), maximum = Math.max(1,...bins.map(bin=>bin.person+bin.known+bin.unknown+bin.unavailable));
    $('timeline-overview').innerHTML = `<div class="timeline-overview-heading"><strong>Day overview</strong><span>${data.events.length} events · ${maximum} in busiest hour</span></div><div class="timeline-histogram" role="img" aria-label="Hourly event counts for the selected day">${bins.map(bin=>`<div class="timeline-bin" title="${esc(time(bin.start))}: ${bin.person+bin.known+bin.unknown+bin.unavailable} events">${['person','known','unknown','unavailable'].map(state=>`<span class="${state}" data-height="${bin[state]/maximum*100}"></span>`).join('')}</div>`).join('')}<div class="timeline-window" ${spanStyle(range[0],range[1],data.start,data.end)}></div></div><div class="timeline-day-axis"><span>${esc(time(data.start))}</span><span>${esc(time((data.start+data.end)/2))}</span><span>${esc(time(data.end))}</span></div>`;
    applyPositions($('timeline-overview'));
    $('timeline-overview').querySelector('.timeline-histogram').addEventListener('click', event => {
      const rect = event.currentTarget.getBoundingClientRect();
      const center = data.start + Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width))*(data.end-data.start);
      if ($('timeline-zoom').value === 'day') $('timeline-zoom').value = '7200';
      setRange(center,Number($('timeline-zoom').value)); draw();
    });
  }
  function draw() {
    if (!data) return;
    const restoreFocus = $('timeline-lanes').contains(document.activeElement) && document.activeElement.matches('[data-period]');
    drawOverview();
    $('timeline-range').textContent = `${time(range[0])} – ${time(range[1])}`;
    $('timeline-prev').disabled = range[0] <= data.start; $('timeline-next').disabled = range[1] >= data.end;
    $('timeline-focus').max = (data.end-data.start)/3600;
    $('timeline-focus').value = ((range[0]+range[1])/2-data.start)/3600;
    $('timeline-focus-label').textContent = time((range[0]+range[1])/2);
    const camera = $('camera-filter').value;
    const cameras = getStatus().cameras.filter(item=>!camera || item.id === camera);
    const ticks = Array.from({length:5},(_,i)=>range[0]+(range[1]-range[0])*i/4);
    $('timeline-lanes').innerHTML = `<div class="timeline-axis"><div></div><div>${ticks.map((at,i)=>`<span class="${i%2?'optional-tick':''}" data-left="${i*25}">${esc(time(at))}</span>`).join('')}</div></div>` + cameras.map(item=>{
      const cameraGroups = groups.filter(group=>group.camera_id === item.id && clippedSpan(group.first_seen,group.last_seen,...range));
      const bands = data.coverage.filter(period=>period.camera_id===item.id).map(period=>{
        const style=spanStyle(period.start,period.end); return style===null?'':`<span class="timeline-coverage ${period.state}" ${style} title="${esc(period.state==='error'?'Camera problem':period.state==='paused'?'Monitoring paused':'Collector reports camera online')} · ${esc(time(period.start))}–${esc(time(period.end))}"></span>`;
      }).join('');
      const snoozes=data.snoozes.map(period=>{const style=spanStyle(period.start,period.end);return style===null?'':`<span class="timeline-snooze" ${style} title="Alerts snoozed · ${esc(time(period.start))}–${esc(time(period.end))}"></span>`;}).join('');
      return `<div class="timeline-lane"><div class="timeline-camera">${esc(item.name)}<small>${item.online?'Online now':'Offline now'}</small></div><div class="timeline-track">${bands}${snoozes}${cameraGroups.map(group=>{
        const index=groups.indexOf(group), style=spanStyle(group.first_seen,group.last_seen), unknown=group.events.filter(event=>event.state==='unknown').length;
        const active=group.events.some(event=>event.id===selectedId);
        return `<button class="timeline-period${active?' selected':''}" data-period="${index}" ${style} aria-pressed="${active}" aria-label="${esc(item.name)} · ${esc(time(group.first_seen))}–${esc(time(group.last_seen))} · ${group.events.length} events${unknown?`, ${unknown} with unrecognised faces`:''}"><span class="sr-only">Review snapshots</span></button>${group.events.map(event=>{
          const segment=spanStyle(event.first_seen,event.last_seen);
          return segment?`<span class="timeline-segment ${event.state}" data-segment-period="${index}" ${segment}></span>`:'';
        }).join('')}`;
      }).join('')}${data.notifications.filter(note=>data.events.some(event=>event.id===note.event_id&&event.camera_id===item.id)).map(note=>{const style=spanStyle(note.at,note.at);return style===null?'':`<span class="timeline-alert" ${style} title="Push service accepted an alert at ${esc(time(note.at))}" aria-label="Alert accepted at ${esc(time(note.at))}">◆</span>`;}).join('')}</div></div>`;
    }).join('');
    applyPositions($('timeline-lanes'));
    // Stack close periods so every hit target remains independently selectable.
    $('timeline-lanes').querySelectorAll('.timeline-track').forEach(track=>{
      const width=track.clientWidth, ends=[];
      const buttons=[...track.querySelectorAll('[data-period]')].sort((a,b)=>parseFloat(a.style.left)-parseFloat(b.style.left));
      buttons.forEach(button=>{
        const left=Math.min(parseFloat(button.style.left)/100*width,Math.max(0,width-button.offsetWidth));
        button.style.left=`${left}px`;
        let row=ends.findIndex(end=>end+6<=left); if(row<0)row=ends.length;
        ends[row]=left+button.offsetWidth; button.style.top=`${18+row*48}px`;
        track.querySelectorAll('[data-segment-period="'+button.dataset.period+'"]').forEach(segment=>segment.style.top=`${30+row*48}px`);
      });
      track.style.height=`${Math.max(70,24+ends.length*48)}px`;
    });
    if(restoreFocus)$('timeline-lanes').querySelector('.timeline-period.selected')?.focus({preventScroll:true});
    $('timeline-lanes').querySelectorAll('[data-period]').forEach(button=>button.addEventListener('click',()=>selectGroup(groups[Number(button.dataset.period)],true).catch(error)));
  }
  async function showMore() {
    const group=groups.find(item=>item.events.some(event=>event.id===selectedId));
    if (!group || loadingMore) return;
    loadingMore=true;
    const version=selectionRequest;
    const ids=snapshotIds.slice(details.length,details.length+12);
    $('timeline-more').disabled=true;
    try {
      const next=ids.length?await api('/timeline/events?ids='+ids.join(',')):[];
      if (version!==selectionRequest) return;
      // Deleted events can disappear between metadata and detail requests.
      details.push(...next);
      const missing = new Set(ids.filter(id=>!next.some(event=>event.id===id)));
      snapshotIds=snapshotIds.filter(id=>!missing.has(id));
      renderEvents($('all-events'),details);
      $('timeline-more').hidden=details.length>=snapshotIds.length;
      $('timeline-selection-note').textContent=`${details.length} of ${snapshotIds.length} snapshots from this selection. Refresh activity to include newer events. Gaps up to 10 minutes are grouped; identity may change.`;
    } finally { if(version===selectionRequest){loadingMore=false; $('timeline-more').disabled=false;} }
  }
  async function selectGroup(group, force=false) {
    const sameSelection=group.events.some(event=>event.id===selectedId);
    selectedId=group.events.some(event=>event.id===selectedId)?selectedId:group.events[0].id;
    $('timeline-selection-title').textContent=`${cameraName(group.camera_id)} · ${time(group.first_seen)}–${time(group.last_seen)} · ${group.events.length} events`;
    if (force || !sameSelection || !snapshotIds.length || (!details.length && !loadingMore)) {
      selectionRequest++; snapshotIds=group.events.map(event=>event.id); details=[]; loadingMore=false;
      $('all-events').replaceChildren(); $('timeline-selection-note').textContent='Loading snapshots…';
      await showMore();
    }
    draw();
  }
  async function load(force=false) {
    if (!$('timeline-day').value) return;
    const version=++request, [start,end]=dayBounds($('timeline-day').value);
    const params=new URLSearchParams({start,end});
    if ($('camera-filter').value) params.set('camera',$('camera-filter').value);
    if ($('state-filter').value) params.set('state',$('state-filter').value);
    const key=params.toString();
    const next=await api('/timeline?'+key);
    if (version!==request) return;
    if (fingerprint!==key) { selectedId=null; range=null; snapshotIds=[]; details=[]; selectionRequest++; $('all-events').replaceChildren(); $('timeline-more').hidden=true; }
    if(!getStatus().settings.recognition_enabled)next.events.forEach(event=>{event.state='person';});
    fingerprint=key; data=next; groups=groupActivity(data.events);
    if (!range) { const zoom=$('timeline-zoom').value; const latest=data.events[0]?.last_seen??(start+end)/2; setRange(latest,zoom==='day'?end-start:Number(zoom)); }
    $('timeline-status').textContent=data.events.length?`${data.events.length} events across ${new Set(data.events.map(event=>event.camera_id)).size} cameras · ${fullDate(start).split(',')[0]}`:'No activity recorded for this day and these filters.';
    const selected=groups.find(group=>group.events.some(event=>event.id===selectedId));
    if (selected) await selectGroup(selected,force);
    else { selectedId=null; details=[]; snapshotIds=[]; selectionRequest++; $('all-events').replaceChildren(); $('timeline-more').hidden=true; $('timeline-selection-title').textContent='Select an activity period'; $('timeline-selection-note').textContent='Choose a coloured period in a camera lane to review its snapshots.'; }
    draw();
  }
  $('timeline-day').addEventListener('change',()=>load().catch(error));
  $('timeline-zoom').addEventListener('change',()=>{if(!data)return; const zoom=$('timeline-zoom').value;setRange((range[0]+range[1])/2,zoom==='day'?data.end-data.start:Number(zoom));draw();});
  $('timeline-focus').addEventListener('input',()=>{if(!data)return;if($('timeline-zoom').value==='day')$('timeline-zoom').value='7200';setRange(data.start+Number($('timeline-focus').value)*3600,Number($('timeline-zoom').value));draw();});
  for (const [id,direction] of [['timeline-prev',-1],['timeline-next',1]]) $(id).addEventListener('click',()=>{if(data){setRange((range[0]+range[1])/2+direction*(range[1]-range[0]),range[1]-range[0]);draw();}});
  $('timeline-more').addEventListener('click',()=>showMore().catch(error));
  let lastWidth=0;
  new ResizeObserver(entries=>{const width=entries[0].contentRect.width;if(width!==lastWidth){lastWidth=width;if(data)draw();}}).observe($('timeline-lanes'));
  return {load,reset};
}
