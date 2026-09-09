'use strict';
const $ = (selector) => document.querySelector(selector);
const escape = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let session, dashboard, page = 'overview', csrf = '', editKey = null, busy = false, toastTimer;
const labels = {overview:'Overview',events:'Polls & posts',create:'Create something',voice:'Voice activity',schedule:'Daily schedule'};
const fmtDate = (x) => x ? new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Bangkok',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(x)) : '—';
const hours = (n) => ((Number(n)||0)/3600).toFixed(1);
const duration = (n) => n >= 3600 ? `${hours(n)} h` : `${Math.floor((n||0)/60)} min`;
const tag = (status) => `<span class="tag ${escape(status)}">${escape(status)}</span>`;
const empty = (text) => `<div class="empty"><span class="empty-symbol">❋</span>${escape(text)}</div>`;
const button = (text, action, extra = '', cls = 'secondary small') => `<button class="button ${cls}" data-action="${action}" ${extra}>${text}</button>`;
function toast(message, error = false) { clearTimeout(toastTimer); $('#toast').textContent=message; $('#toast').className=error?'error':''; $('#toast').hidden=false; toastTimer=setTimeout(()=>$('#toast').hidden=true,6500); }
async function api(path, data) {
  const options = {credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf}};
  if(data!==undefined){ options.method='POST'; options.body=JSON.stringify(data); }
  const response = await fetch('/api/'+path, options);
  const result = await response.json();
  if(!response.ok){ if(response.status===401 && session){session=null;$('#shell').hidden=true;$('#login').hidden=false;} throw new Error(result.error||'Request failed.'); }
  return result;
}
function heading(title, subtitle, action = '') {
  return `<div class="page-heading"><div><p class="eyebrow">YOUR COMMUNITY AT A GLANCE</p><h1>${title}</h1><p>${subtitle}</p></div>${action}</div>`;
}
function stats() {
  const s=dashboard.stats;
  return `<div class="stats">${[
    ['Poll responses',s.responses,'Across daily polls','▤'],['Time together',hours(s.voice_seconds)+' h','Total member voice time','◉'],
    ['Time spent solo',hours(s.solo_seconds)+' h','Already included in voice time','◌'],['Active members',s.members,'People who used voice','♧']
  ].map(([label,value,note,icon])=>`<article class="stat"><div class="stat-label">${label}<span>${icon}</span></div><span class="stat-value">${value}</span><small>${note}</small></article>`).join('')}</div>`;
}
function scheduleCard() {
  const s=dashboard.settings;
  return `<article class="panel"><div class="panel-head"><h2>The daily rhythm</h2><button class="text-button" data-page="schedule">Manage ↗</button></div>
  <div class="schedule-item"><span class="schedule-clock">${escape(s.poll_time)}</span><div><strong>Tonight's game poll</strong><small>Ask who's joining the evening</small></div>${tag(s.poll_enabled?'enabled':'paused')}</div>
  <hr class="divider"><div class="schedule-item"><span class="schedule-clock">${escape(s.report_time)}</span><div><strong>Daily summary</strong><small>Votes, start times & reasons</small></div>${tag(s.report_enabled?'enabled':'paused')}</div>
  <p class="hint">Every day · Asia/Bangkok<br>Voice and solo activity stay in your private dashboard.</p></article>`;
}
function liveCard() {
  return `<article class="panel"><div class="panel-head"><h2>In voice right now</h2><span class="tag"><span class="status-dot"></span> Live</span></div>${dashboard.live.length?dashboard.live.map(room=>`<div class="room"><span class="room-icon">◉</span><div><strong>${escape(room.channel)}</strong><p>${room.people.map(escape).join(', ')}</p></div>${tag(room.solo?'solo':room.people.length+' together')}</div>`).join(''):empty('The rooms are quiet. Your next game is a poll away.')}</article>`;
}
function eventRows(events) {
  return events.map(e=>`<tr><td><span class="table-title">${escape(e.title)}</span><span class="muted">${escape(e.kind)} · #${escape(channelName(e.channel_id))}</span></td><td>${tag(e.status)}</td><td>${fmtDate(e.publish_at||e.published_at||e.created_at)}</td><td>${button('View ↗','detail',`data-key="${escape(e.key)}"`)}</td></tr>`).join('');
}
function channelName(id){return session.channels.find(c=>c.id===id)?.name||id;}
function eventTable(events){ return events.length ? `<div class="table-wrap"><table><thead><tr><th>POLL / POST</th><th>STATUS</th><th>WHEN · BANGKOK</th><th></th></tr></thead><tbody>${eventRows(events)}</tbody></table></div>` : empty('No events yet. Create your first poll or announcement.'); }
function overview() {
  const scheduled=dashboard.events.filter(e=>e.status==='scheduled').length;
  const failed=dashboard.events.filter(e=>e.status==='review').length;
  const today=new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Bangkok',weekday:'long',day:'numeric',month:'long'}).format(new Date());
  return heading('A good night starts here.',escape(today)+' · Here’s what’s happening in your server.',`<button class="button" data-page="create">＋ Create a poll or post</button>`)
  +(failed?`<div class="alert">${failed} post(s) need a delivery check. Open Polls & posts to review them.</div>`:'')
  +(dashboard.health.error?`<div class="alert">${escape(dashboard.health.error)}</div>`:'')
  +`<section class="welcome-card"><div><p class="eyebrow">MAKE SOMETHING HAPPEN</p><h2>A plan for everyone to look forward to.</h2><p>Create a one-time poll for a game night, a weekend event, or your next get-together.</p></div><span class="banner-sprout" aria-hidden="true">❋</span><button class="button" data-page="create">Plan an event ↗</button></section>
  <div class="period-label">LAST 30 DAYS <span class="inline-note">${scheduled} scheduled post${scheduled===1?'':'s'}</span></div>${stats()}
  <div class="grid-two">${liveCard()}${scheduleCard()}</div>
  <div class="grid-two"><article class="panel"><div class="panel-head"><h2>Recent polls & posts</h2><button class="text-button" data-page="events">View all ↗</button></div>${eventTable(dashboard.events.slice(0,5))}</article>
  <article class="panel"><div class="panel-head"><h2>Time well spent</h2><small>Voice hours · 30 days</small></div>${memberChart()}<p class="hint">Per-person totals include solo time. A room with two people records time for both.</p></article></div>`;
}
function memberChart(){ const people=dashboard.members.slice(0,5);const max=people[0]?.seconds||1;return people.length?`<div class="chart">${people.map(p=>`<div class="chart-row"><span>${escape(p.name)}</span><meter min="0" max="${max}" value="${p.seconds}" aria-label="${escape(p.name)} voice time"></meter><span>${hours(p.seconds)} h</span></div>`).join('')}</div>`:empty('Voice history will appear after members join a room.'); }
function eventsPage(){
  return heading('Plans worth showing up for.','One-time polls, news, and announcements — all in one place.',`<button class="button" data-page="create">＋ Create new</button>`)
  +`<section class="panel"><div class="filterbar"><input type="search" id="event-search" placeholder="Search polls and posts…" aria-label="Search polls and posts"><select id="event-filter" aria-label="Filter by status"><option value="all">All statuses</option>${['draft','scheduled','open','published','closed','review','cancelled'].map(s=>`<option>${s}</option>`).join('')}</select></div><div id="event-table">${eventTable(dashboard.events)}</div></section>
  <section class="panel grid-two-panel"><div class="panel-head"><h2>Daily game polls</h2><small>Last 30 days</small></div>${dailyTable()}</section>`;
}
function dailyTable(){return dashboard.polls.length?`<div class="table-wrap"><table><thead><tr><th>DATE</th><th>STATUS</th><th>YES / MAYBE / NO</th><th>RESPONSES</th><th></th></tr></thead><tbody>${dashboard.polls.map(p=>{const answers=dashboard.responses.filter(r=>r['Poll Key']===p.id);return `<tr><td>${escape(p['Poll Date'])}</td><td>${tag(p.Status||'open')}</td><td>${['yes','maybe','no'].map(c=>answers.filter(a=>a.Choice===c).length).join(' / ')}</td><td>${answers.length}</td><td>${button('Responses ↗','daily-detail',`data-key="${escape(p.id)}"`)}</td></tr>`;}).join('')}</tbody></table></div>`:empty('Your daily game polls will appear here.');}
function channelOptions(selected){return session.channels.map(c=>`<option value="${c.id}" ${selected===c.id?'selected':''}>#${escape(c.name)}</option>`).join('');}
function localValue(iso){if(!iso)return '';return new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Bangkok',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(iso)).replace(' ','T');}
function createPage(data={}) {
  const defaultClose=localValue(new Date(Date.now()+24*3600*1000).toISOString());
  return heading(editKey?'Give your draft a little polish.':'Bring your next idea to life.','Create a draft, check the preview, then choose when to publish.')
  +`<div class="composer-grid"><section class="panel"><form id="event-form"><p class="section-label">01 / THE ESSENTIALS</p>
  <div class="form-row"><div class="field"><label for="kind">What are you creating?</label><select id="kind" name="kind">${['poll','announcement','news'].map(k=>`<option value="${k}" ${data.kind===k?'selected':''}>${k==='poll'?'One-time event poll':k==='news'?'News post':'Announcement'}</option>`).join('')}</select></div>
  <div class="field"><label for="channel">Destination channel</label><select id="channel" name="channel_id" required>${channelOptions(data.channel_id||session.settings.announcement_channel_id)}</select></div></div>
  <div class="field"><label for="title">Title or poll question</label><input id="title" name="title" maxlength="200" placeholder="Who’s up for a Friday game night?" value="${escape(data.title||'')}" required></div>
  <div class="field"><label for="body">Message / event details</label><textarea id="body" name="body" maxlength="3500" placeholder="The game, the plan, and anything everyone should know…">${escape(data.body||'')}</textarea><small>Discord formatting is supported when published. The preview shows your text.</small></div>
  <div id="poll-fields"><div class="field"><label for="options">Poll choices · one per line</label><textarea id="options" name="options" rows="3">${escape((data.options||['Yes','Maybe','No']).join('\n'))}</textarea><small>2–10 choices. One answer per member; they can change it before closing.</small></div><label class="check"><input type="checkbox" name="no_reason" ${data.no_reason?'checked':''}> Ask for a reason when someone chooses No</label></div>
  <hr class="divider"><p class="section-label">02 / PICK THE MOMENT</p>
  <div class="form-row"><div class="field"><label for="publish-at">Publish at · optional</label><input type="datetime-local" id="publish-at" name="publish_at" value="${localValue(data.publish_at)}"><small>Leave empty to publish when you approve the draft.</small></div><div class="field" id="close-field"><label for="closes-at">Voting closes</label><input type="datetime-local" id="closes-at" name="closes_at" value="${localValue(data.closes_at)||defaultClose}"><small>The poll closes automatically.</small></div></div>
  <div class="field"><label for="event-at">Event starts · optional</label><input type="datetime-local" id="event-at" name="event_at" value="${localValue(data.event_at)}"><small>All times on this form use Bangkok (GMT+7).</small></div>
  <div class="form-footer"><small>Saving a draft does not send a message to Discord.</small><button type="submit" class="button">${editKey?'Save changes':'Save draft'} →</button></div></form></section>
  <aside class="preview-wrap"><div class="preview-label">A LOOK BEFORE YOU POST</div><div class="discord-preview"><div class="discord-profile"><span class="brand-mark">t.</span><strong>Teemo <span class="tag">APP</span></strong><small>Today</small></div><div class="discord-embed"><h3 id="preview-title"></h3><p id="preview-body"></p><div class="preview-options" id="preview-options"></div><small id="preview-time"></small></div></div><div class="preview-note"><strong>A little peace of mind</strong><br>Your draft stays private until you publish it. Scheduled posts are saved across restarts. Mentions are quiet by default.</div></aside></div>`;
}
function preview(){if(!$('#event-form'))return;const f=$('#event-form').elements;const poll=f.kind.value==='poll';$('#poll-fields').hidden=!poll;$('#close-field').hidden=!poll;f.closes_at.required=poll;f.body.required=!poll;$('#preview-title').textContent=f.title.value||'Your next great plan';$('#preview-body').textContent=f.body.value||'Add the details and watch your post take shape.';$('#preview-options').innerHTML=poll?f.options.value.split('\n').filter(x=>x.trim()).slice(0,10).map(x=>`<span>${escape(x)}</span>`).join(''):'';$('#preview-time').textContent=poll?`Voting closes ${f.closes_at.value.replace('T',' ')} · Bangkok`:'Teemo · '+f.kind.value;}
function voicePage(){
  return heading('A pulse on your people.','Voice time and solo periods over the last 30 days.',button('↓ Export voice CSV','export','data-kind="voice"','secondary'))+stats()
  +`<div class="grid-two">${liveCard()}<article class="panel"><div class="panel-head"><h2>Most time in voice</h2><small>30 days</small></div>${memberChart()}</article></div>
  <section class="panel grid-two-panel"><div class="panel-head"><h2>Member activity</h2><span class="muted">Solo time is part of total voice time</span></div>${dashboard.members.length?`<div class="table-wrap"><table><thead><tr><th>MEMBER</th><th>VOICE TIME</th><th>SOLO TIME</th><th>VOICE SESSIONS</th></tr></thead><tbody>${dashboard.members.map(m=>`<tr><td><span class="row-avatar">${escape(m.name[0])}</span>${escape(m.name)}</td><td>${duration(m.seconds)}</td><td>${duration(m.solo_seconds)}</td><td>${m.sessions}</td></tr>`).join('')}</tbody></table></div>`:empty('No voice history in this period.')}</section>
  <section class="panel grid-two-panel"><div class="panel-head"><h2>Recent voice sessions</h2><small>Latest 250 · Export for all records in period</small></div>${voiceTable(dashboard.voice)}</section>
  <section class="panel grid-two-panel"><div class="panel-head"><h2>Time spent alone</h2>${button('↓ Export solo CSV','export','data-kind="solo"')}</div>${voiceTable(dashboard.solo)}<p class="hint">Tracking measures channel presence, not speaking or microphone activity. Sessions spanning bot downtime may include an estimated end time.</p></section>`;
}
function voiceTable(rows){return rows.length?`<div class="table-wrap"><table><thead><tr><th>MEMBER</th><th>CHANNEL</th><th>STARTED · BANGKOK</th><th>ENDED</th><th>DURATION IN PERIOD</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${escape(r.name)}</td><td>${escape(r.channel)}</td><td>${fmtDate(r.joined)}</td><td>${r.active?tag('active'):fmtDate(r.left)}</td><td>${duration(r.seconds)}</td></tr>`).join('')}</tbody></table></div>`:empty('No sessions in this period.');}
function schedulePage(){const s=dashboard.settings;return heading('Set it. Let Teemo handle it.','The everyday routine for your server, in Bangkok time.')
  +`<div class="composer-grid"><section class="panel"><form id="schedule-form"><p class="section-label">YOUR DAILY TASKS</p><div class="form-row"><div class="field"><label for="poll-time">Daily game poll</label><input id="poll-time" type="time" name="poll_time" value="${escape(s.poll_time)}" required><label class="check"><input type="checkbox" name="poll_enabled" ${s.poll_enabled?'checked':''}> Send every day</label></div><div class="field"><label for="report-time">Daily summary</label><input id="report-time" type="time" name="report_time" value="${escape(s.report_time)}" required><label class="check"><input type="checkbox" name="report_enabled" ${s.report_enabled?'checked':''}> Send every day</label></div></div>
  <div class="field"><label>Daily poll channels</label>${session.channels.map(c=>`<label class="check"><input type="checkbox" name="poll_channels" value="${c.id}" ${s.poll_channel_ids.includes(c.id)?'checked':''}> #${escape(c.name)}</label>`).join('')}<small>Close today's poll before removing its destination channel.</small></div>
  <div class="field"><label for="post-channel">Default news & announcement channel</label><select id="post-channel" name="announcement_channel_id">${channelOptions(s.announcement_channel_id)}</select></div>
  <div class="form-footer"><small>Changes take effect for future runs. Use Run now for a time that has already passed today.</small><button class="button" type="submit">Save schedule →</button></div></form></section>
  <aside><section class="panel"><div class="panel-head"><h2>Need it right now?</h2></div><p class="hint">Run today's task once. Teemo checks for an existing poll or summary before sending.</p><div class="detail-actions">${button('Run daily poll','run-poll','','secondary')}${button('Run summary','run-report','','secondary')}</div><p class="hint">Run summary closes today's daily poll. Pausing these daily tasks does not pause event polls or voice tracking.</p></section><div class="preview-note"><strong>Always in the background</strong><br>Voice sessions and solo periods continue to be recorded. Attendance is kept out of Discord's daily summary.</div></aside></div>`;}
function render(data){
  $('#breadcrumb').textContent='Workspace / '+labels[page];
  document.querySelectorAll('.nav-item').forEach(el=>{el.classList.toggle('active',el.dataset.page===page);el.setAttribute('aria-current',el.dataset.page===page?'page':'false');});
  $('#content').innerHTML=page==='overview'?overview():page==='events'?eventsPage():page==='voice'?voicePage():page==='schedule'?schedulePage():createPage(data);
  if(page==='create')preview();
}
async function refresh(){dashboard=await api('dashboard');session.settings=dashboard.settings;$('#updated').textContent='History updated '+fmtDate(dashboard.fetched_at);$('#connection').textContent=dashboard.health.discord?'Teemo connected':'Teemo reconnecting';if(page!=='create'&&page!=='schedule')render();}
async function details(key){$('#detail-content').innerHTML='<div class="loading">Loading details…</div>';$('#detail-dialog').showModal();const {event:e,votes}=await api('events/'+key);const counts=e.options.map((_,i)=>votes.filter(v=>v.choice===i).length);const max=Math.max(...counts,1);$('#detail-content').innerHTML=`<h2 class="detail-title">${escape(e.title)}</h2><div class="detail-meta">${tag(e.status)}<span class="muted">${escape(e.kind)} · #${escape(channelName(e.channel_id))}</span></div><p class="detail-body">${escape(e.body)}</p>
  <p class="hint">Publish: ${e.publish_at?fmtDate(e.publish_at):'When approved'}${e.closes_at?' · Voting closes: '+fmtDate(e.closes_at):''}${e.event_at?'<br>Event starts: '+fmtDate(e.event_at):''}</p>
  ${e.error?`<div class="alert">${escape(e.error)}</div>`:''}
  <div class="detail-actions">${e.status==='draft'?button(e.publish_at?'Schedule this post':'Publish to Discord','event-publish',`data-key="${e.key}"`,''):''}${e.status==='draft'?button('Edit draft','edit',`data-key="${e.key}"`):''}${e.status==='open'?button('Close voting','event-close',`data-key="${e.key}"`,'danger'):''}${['draft','scheduled','review'].includes(e.status)?button('Cancel','event-cancel',`data-key="${e.key}"`):''}${e.status==='review'?button('Check delivery','event-recover',`data-key="${e.key}"`):''}${button('Duplicate','duplicate',`data-key="${e.key}"`)}${e.message_id?`<a class="button secondary small" target="_blank" rel="noreferrer" href="https://discord.com/channels/${e.guild_id}/${e.channel_id}/${e.message_id}">Open in Discord ↗</a>`:''}</div>
  ${e.kind==='poll'?`<hr class="divider"><h3>Responses (${votes.length})</h3><div class="detail-result chart">${e.options.map((o,i)=>`<div class="chart-row"><span>${escape(o)}</span><meter value="${counts[i]}" max="${max}" min="0" aria-label="${escape(o)} votes"></meter><span>${counts[i]}</span></div>`).join('')}</div>${votes.length?`<div class="table-wrap"><table><thead><tr><th>MEMBER</th><th>ANSWER</th><th>REASON</th><th>TIME</th></tr></thead><tbody>${votes.map(v=>`<tr><td>${escape(v.display_name)}</td><td>${escape(v.answer)}</td><td class="wrap">${escape(v.reason||'—')}</td><td>${fmtDate(v.at)}</td></tr>`).join('')}</tbody></table></div>`:empty('Answers will appear here after the poll is published.')}`:''}<p class="hint">Created ${fmtDate(e.created_at)}${e.history?.length?' · Last action: '+escape(e.history.at(-1).action)+' at '+fmtDate(e.history.at(-1).at):''}</p>`;}
function dailyDetails(key){const poll=dashboard.polls.find(p=>p.id===key);const responses=dashboard.responses.filter(r=>r['Poll Key']===key);$('#detail-content').innerHTML=`<h2 class="detail-title">Daily game poll · ${escape(poll['Poll Date'])}</h2>${tag(poll.Status)}<p class="hint">Response details and attendance are visible only to administrators.</p><div class="detail-actions">${button('↓ Export daily responses','export','data-kind="responses"')}</div>${responses.length?`<div class="table-wrap"><table><thead><tr><th>MEMBER</th><th>ANSWER</th><th>PLAY TIME</th><th>REASON</th><th>JOINED VOICE</th></tr></thead><tbody>${responses.map(r=>`<tr><td>${escape(r['Display Name'])}</td><td>${escape(r.Choice)}</td><td>${escape(r['Play Time']||'—')}</td><td class="wrap">${escape(r.Reason||'—')}</td><td>${escape(r['Joined Voice Chat']||'no')}</td></tr>`).join('')}</tbody></table></div>`:empty('No responses yet.')}`;$('#detail-dialog').showModal();}
async function start(){
  const params=new URLSearchParams(location.hash.slice(1));const token=params.get('login');if(token){history.replaceState(null,'',location.pathname);$('#login-state').textContent='Signing you in securely…';await api('login',{token});}
  session=await api('session');csrf=session.csrf;$('#guild-name').textContent=session.guild;$('#user-name').textContent=session.name;$('#user-initial').textContent=session.name[0];$('#login').hidden=true;$('#shell').hidden=false;$('#content').innerHTML='<div class="loading">Gathering the latest from your community…</div>';await refresh();render();
}
document.addEventListener('click',async ev=>{
  const nav=ev.target.closest('[data-page]');if(nav){page=nav.dataset.page;editKey=null;render();return;}
  const target=ev.target.closest('[data-action]');if(!target||busy)return;const action=target.dataset.action,key=target.dataset.key;
  busy=true;target.disabled=true;
  try{
    if(action==='detail')await details(key);
    else if(action==='daily-detail')dailyDetails(key);
    else if(action==='export'){const response=await fetch('/api/export?kind='+target.dataset.kind);if(!response.ok)throw new Error('Export failed. Refresh and try again.');const blob=await response.blob();const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`teemo-${target.dataset.kind}-30days.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),2000);}
    else if(action==='edit'||action==='duplicate'){const {event}=await api('events/'+key);editKey=action==='edit'?key:null;if(!editKey){event.publish_at=null;event.closes_at=null;event.title+=' (copy)';}page='create';$('#detail-dialog').close();render(event);}
    else if(action.startsWith('event-')){
      const op=action.slice(6);
      if(op==='close'&&!confirm('Close this poll now? Members will no longer be able to vote.'))return;
      const {event}=await api(`events/${key}/${op}`,{});await refresh();await details(key);toast(event.status==='review'?'Delivery needs a check. See the event details.':'Saved. Status: '+event.status,event.status==='review');
    }else if(action==='run-poll'||action==='run-report'){
      if(action==='run-report'&&!confirm("Close today's daily poll and publish its summary now?"))return;
      const result=await api('daily/'+(action==='run-poll'?'poll':'report'),{});toast(result.message);await refresh();
    }
  }catch(error){toast(error.message,true);}finally{busy=false;target.disabled=false;}
});
document.addEventListener('input',ev=>{if(ev.target.closest('#event-form'))preview();if(['event-search','event-filter'].includes(ev.target.id))filterEvents();});
document.addEventListener('change',ev=>{if(ev.target.closest('#event-form'))preview();if(ev.target.id==='event-filter')filterEvents();});
function filterEvents(){const query=$('#event-search').value.toLowerCase(),status=$('#event-filter').value;$('#event-table').innerHTML=eventTable(dashboard.events.filter(e=>e.title.toLowerCase().includes(query)&&(status==='all'||e.status===status)));}
document.addEventListener('submit',async ev=>{
  ev.preventDefault();if(busy)return;const form=ev.target;const submit=form.querySelector('[type="submit"]');busy=true;submit.disabled=true;
  try{
    if(form.id==='event-form'){
      const data=Object.fromEntries(new FormData(form));data.options=form.elements.options.value.split('\n').map(s=>s.trim()).filter(Boolean);data.no_reason=form.elements.no_reason.checked;
      for(const k of ['publish_at','closes_at','event_at'])data[k]=data[k]?data[k]+':00+07:00':null;
      if(data.kind!=='poll'){data.options=[];data.closes_at=null;}
      const result=await api(editKey?`events/${editKey}/edit`:'events',data);editKey=null;page='events';await refresh();render();await details(result.event.key);toast('Draft saved. Review it, then publish when you’re ready.');
    }else if(form.id==='schedule-form'){
      const data=Object.fromEntries(new FormData(form));data.poll_channel_ids=[...form.querySelectorAll('[name="poll_channels"]:checked')].map(x=>x.value);data.poll_enabled=form.elements.poll_enabled.checked;data.report_enabled=form.elements.report_enabled.checked;
      const result=await api('settings',data);session.settings=result.settings;dashboard.settings=result.settings;toast('Daily schedule saved.');render();
    }
  }catch(error){toast(error.message,true);}finally{busy=false;submit.disabled=false;}
});
$('#close-dialog').addEventListener('click',()=>$('#detail-dialog').close());
$('#logout').addEventListener('click',async()=>{try{await api('logout',{});location.reload();}catch(e){toast(e.message,true);}});
start().catch(error=>{if(!session){$('#login-state').innerHTML=`<strong>Sign in through Discord</strong><p>${escape(error.message)}</p><span class="muted">Run <code>/teemo_web</code> to get a private sign-in link.</span>`;}else{$('#content').innerHTML=`<div class="alert">${escape(error.message)} Refresh the page to retry.</div>`;}});
setInterval(()=>{if(session&&!busy&&!document.hidden&&page!=='create'&&page!=='schedule'&&!$('#detail-dialog').open)refresh().catch(e=>toast(e.message,true));},60000);
