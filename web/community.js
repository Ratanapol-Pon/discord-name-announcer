'use strict';
let summaryOperation = null;
const newRequestId = () => crypto.randomUUID().replaceAll('-','');
const messageLink = t => t.message_id && t.channel_id ? `<a target="_blank" rel="noreferrer" href="https://discord.com/channels/${encodeURIComponent(session.guild_id)}/${encodeURIComponent(t.channel_id)}/${encodeURIComponent(t.message_id)}">Open message ↗</a>` : '—';

$('#period').addEventListener('change', () => {
  const custom = $('#period').value === 'custom';
  for (const id of ['#period-from','#period-to']) { $(id).disabled=!custom; $(id).required=custom; }
});
document.addEventListener('submit', async ev => {
  if(!['period-controls','template-form','community-form'].includes(ev.target.id))return;
  ev.preventDefault();if(busy)return;
  busy=true;
  try {
    const form=ev.target, data=Object.fromEntries(new FormData(form));
    if(form.id==='period-controls'){
      const query=new URLSearchParams(data).toString();
      const result=await api('dashboard?'+query);periodQuery=query;dashboard=result;render();
    }else if(form.id==='template-form'){
      data.interval_days=Number(data.interval_days);data.close_hours=Number(data.close_hours);data.event_hours=Number(data.event_hours);
      data.first_at=data.first_at+':00+07:00';
      await api('templates',data);toast('Template saved as paused. Review it, then enable recurring posts.');render();
    }else{
      await api('community/config',{public_reasons:form.public_reasons.checked,backups:form.backups.checked});
      toast('Privacy and backup settings saved. Existing Discord messages are unchanged.');render();
    }
  }catch(e){toast(e.message,true);}finally{busy=false;}
});

function summaryControls(key){
  const p=dashboard.polls.find(p=>p.id===key);
  const plans=(dashboard.responses||[]).filter(r=>r['Poll Key']===key&&r.Games);
  if(plans.length)$('#detail-content').insertAdjacentHTML('beforeend',`<h3>Game availability</h3><div class="table-wrap"><table><thead><tr><th>MEMBER</th><th>GAMES</th><th>FROM</th><th>UNTIL</th></tr></thead><tbody>${plans.map(r=>`<tr><td>${escape(r['Display Name'])}</td><td class="wrap">${escape(r.Games)}</td><td>${escape(r['Play Time'])}</td><td>${escape(r['Available Until'])}</td></tr>`).join('')}</tbody></table></div>`);
  if(p?.Status==='closed')$('#detail-content').insertAdjacentHTML('beforeend',`<hr class="divider"><h3>Saved summary</h3><p class="hint">Preview the saved answers with the current template before changing any Discord message.</p>${button('Preview summary','summary-preview',`data-key="${escape(key)}"`)}<div id="summary-preview"></div>`);
}

async function renderCommunity(){
  const requestedPage=page;$('#content').innerHTML='<div class="loading">Loading community tools…</div>';
  try{
    let html;
    if(page==='tasks'){
      const result=await api('tasks');
      html=heading('Task history','Expected times, delivery outcomes, and message links. Detailed daily logging starts with this upgrade.')+
        `<section class="panel"><p class="hint">A running task after a restart may have been delivered. Check delivery before creating a replacement. Summary tools are under Polls & posts → Responses.</p><div class="table-wrap"><table><thead><tr><th>TASK</th><th>EXPECTED · BANGKOK</th><th>ATTEMPTED</th><th>COMPLETED</th><th>STATUS</th><th>MESSAGE / ACTION</th></tr></thead><tbody>${result.tasks.map(t=>`<tr><td class="wrap">${escape(t.action)}<small>${escape(t.error||'')}</small></td><td>${fmtDate(t.expected_at)}</td><td>${fmtDate(t.attempted_at)}</td><td>${fmtDate(t.completed_at)}</td><td>${tag(t.status)}</td><td>${messageLink(t)}${['review','running'].includes(t.status)&&t.channel_id&&t.key.includes(':')?button('Check delivery','task-recover',`data-key="${escape(t.key)}"`):''}</td></tr>`).join('')}</tbody></table></div>${result.tasks.length?'':empty('No task history yet.')}</section>`;
    }else if(page==='templates'){
      const {templates}=await api('templates');
      html=heading('Recurring templates','Reuse an existing poll or post. New templates stay paused until you enable them.')+
      `<section class="panel"><form id="template-form"><div class="field"><label>Source poll / post<select name="event_key" required>${dashboard.events.map(e=>`<option value="${escape(e.key)}">${escape(e.title)}</option>`).join('')}</select></label></div><div class="form-row"><label>Name<input name="name" maxlength="100" required></label><label>First publication · Bangkok<input type="datetime-local" name="first_at" required></label></div><div class="form-row"><label>Repeat<select name="interval_days"><option value="7">Weekly</option><option value="14">Every two weeks</option><option value="1">Daily</option></select></label><label>Close poll after (hours)<input name="close_hours" type="number" min="1" max="168" value="12" required></label><label>Event starts after (hours; 0 = none)<input name="event_hours" type="number" min="0" max="720" value="24" required></label></div><button class="button" type="submit">Save paused template</button></form></section>`+
      `<section class="panel"><div class="table-wrap"><table><thead><tr><th>TEMPLATE</th><th>NEXT · BANGKOK</th><th>REPEAT</th><th>STATUS</th><th>ACTIONS</th></tr></thead><tbody>${templates.map(t=>`<tr><td>${escape(t.name)}<small>${escape(t.error||'')}</small></td><td>${fmtDate(t.next_at)}</td><td>${t.interval_days} days</td><td>${t.enabled?'Enabled':'Paused'}</td><td>${button(t.enabled?'Pause':'Enable recurring posts','template-toggle',`data-key="${escape(t.key)}" data-enable="${!t.enabled}"`)} ${button('Create draft now','template-draft',`data-key="${escape(t.key)}"`)}</td></tr>`).join('')}</tbody></table></div></section>`;
    }else{
      const data=await api('community');
      html=heading('Privacy & backups','Member controls, honest tracking quality, and recoverable records.')+
      `<section class="panel"><h2>Privacy defaults</h2><p>Members can use <code>/teemo_preferences</code> to pause future voice tracking, opt out of public yearly voice totals, and manage DM reminders and Bangkok quiet hours. Pausing tracking does not erase existing records.</p><form id="community-form"><label class="check"><input type="checkbox" name="public_reasons" ${data.config.public_reasons?'checked':''}> Include No-vote reasons in future daily summaries</label><label class="check"><input type="checkbox" name="backups" ${data.config.backups?'checked':''}> Daily backup on the existing server volume</label><button class="button" type="submit">Save settings</button></form><p class="hint">Voice presence is not microphone activity. Recorded = observed live; estimated = spans downtime; legacy / unknown = older records without quality metadata.</p></section>`+
      `<section class="panel"><h2>Backups</h2><p class="hint">Daily JSON snapshots; newest 14 per server retained. They contain private member data, but no bot credentials. Stored on the bot's volume, not a separate disaster-recovery service. Download a copy somewhere safe. Audio clips are not included.</p>${button('Back up now','backup-now')}<div class="table-wrap"><table><thead><tr><th>CREATED</th><th>RECORDS</th><th>ACTIONS</th></tr></thead><tbody>${data.backups.map(b=>`<tr><td>${fmtDate(b.created_at)}</td><td>${b.records}</td><td>${button('Download JSON','backup-download',`data-key="${escape(b.id)}"`)} ${button('Preview restore','backup-preview',`data-key="${escape(b.id)}"`)}</td></tr>`).join('')}</tbody></table></div><p class="hint">Restore only adds missing historical poll/response/report/session records. It does not overwrite existing rows, restart posts, or restore preferences and templates automatically.</p><div id="restore-preview"></div></section>`;
    }
    if(page===requestedPage)$('#content').innerHTML=html;
  }catch(e){if(page===requestedPage)$('#content').innerHTML=`<div class="alert">${escape(e.message)}</div>`;}
}

async function communityAction(action,target){
  const key=target.dataset.key;
  if(action==='member-detail'){
    const m=await api('members/'+encodeURIComponent(target.dataset.user)+'?'+periodQuery);
    const name=dashboard.members.find(x=>String(x.user_id)===m.user_id)?.name||'Member';
    $('#detail-content').innerHTML=`<h2>${escape(name)}</h2><p>${escape(periodLabel())} · Voice ${duration(m.voice.reduce((s,r)=>s+r.seconds,0))} · Solo ${duration(m.solo.reduce((s,r)=>s+r.seconds,0))}</p><h3>Voice sessions</h3>${voiceTable(m.voice)}<h3>Solo periods</h3>${voiceTable(m.solo)}<h3>Poll answers</h3><div class="table-wrap"><table><thead><tr><th>ANSWER</th><th>TIME</th><th>REASON</th><th>RESPONDED</th></tr></thead><tbody>${m.responses.map(r=>`<tr><td>${escape(r.Choice)}</td><td>${escape(r['Play Time']||'—')}</td><td class="wrap">${escape(r.Reason||'—')}</td><td>${fmtDate(r['Responded At'])}</td></tr>`).join('')}</tbody></table></div>`;
    $('#detail-dialog').showModal();
  }else if(action==='summary-preview'){
    const p=dashboard.polls.find(p=>p.id===key);
    summaryOperation={channel_id:p['Channel ID'],date:p['Poll Date'],request_id:newRequestId()};
    const {embed}=await api('reports/action',{...summaryOperation,action:'preview'});
    $('#summary-preview').innerHTML=`<div class="discord-embed"><h3>${escape(embed.title)}</h3><p>${escape(embed.description)}</p>${embed.fields.map(f=>`<h4>${escape(f.name)}</h4><p>${escape(f.value)}</p>`).join('')}<small>${escape(embed.footer.text)}</small></div><p class="hint">Text preview only; Discord renders the formatting. Saved votes are not changed.</p>${button('Update original message','summary-update')} ${button('Resend as a NEW message','summary-resend')}`;
  }else if(action==='summary-update'||action==='summary-resend'){
    if(!summaryOperation)throw Error('Preview the summary first.');
    if(!confirm(action==='summary-update'?'Replace the original summary with this template?':'Post a new summary? The original stays visible, so this creates another copy.'))return;
    const result=await api('reports/action',{...summaryOperation,action:action.slice(8),confirmed:true});
    toast(result.task.status==='sent'?'Summary delivered.':'Delivery needs review. Open Task history.',result.task.status!=='sent');
    $('#summary-preview').insertAdjacentHTML('beforeend',`<p>${messageLink(result.task)}</p>`);
    // Keep the same operation ID on retries; a fresh preview starts a new operation.
  }else if(action==='task-recover'){
    const {task}=await api('tasks/recover',{key});toast(task.status==='sent'?'Found the delivered message.':task.error,task.status!=='sent');render();
  }else if(action==='template-toggle'){
    const enable=target.dataset.enable==='true';
    if(enable&&!confirm('Enable automatic recurring posts for this template? Missed occurrences will be skipped.'))return;
    await api('templates/'+encodeURIComponent(key)+'/toggle',{enabled:enable});render();
  }else if(action==='template-draft'){
    const {event}=await api('templates/'+encodeURIComponent(key)+'/draft',{});await refresh();await details(event.key);
  }else if(action==='backup-now'){
    await api('backups/create',{});toast('Backup saved.');render();
  }else if(action==='backup-download'){
    const response=await fetch('/api/backups/'+encodeURIComponent(key));if(!response.ok)throw Error('Backup download failed.');
    const url=URL.createObjectURL(await response.blob()),a=document.createElement('a');a.href=url;a.download='teemo-backup-'+key+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),2000);
  }else if(action==='backup-preview'){
    const result=await api('backups/'+encodeURIComponent(key)+'/restore',{preview:true});
    $('#restore-preview').innerHTML=`<p>${result.missing} missing historical records can be restored. Existing records will be kept. Active sessions will be closed at the backup time and marked estimated.</p>${result.missing?button('Restore missing records','backup-restore',`data-key="${escape(key)}"`):''}`;
  }else if(action==='backup-restore'){
    if(!confirm('Restore missing historical records only? This does not change existing records or restart posts.'))return;
    const result=await api('backups/'+encodeURIComponent(key)+'/restore',{confirmed:true});toast(`Restored ${result.restored} records.`);render();
  }
}
