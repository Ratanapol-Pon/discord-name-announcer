const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

function fixture(payload) {
  const nodes = new Map();
  const node = selector => {
    if(!nodes.has(selector))nodes.set(selector, {innerHTML:'',textContent:'',dataset:{},addEventListener(){},querySelectorAll(){return[];},showModal(){},insertAdjacentHTML(_where, html){this.innerHTML+=html;}});
    return nodes.get(selector);
  };
  const context=vm.createContext({
    document:{documentElement:{dataset:{theme:'light'}},querySelector:node,querySelectorAll:()=>[],addEventListener(){}},
    localStorage:{setItem(){}},location:{hash:''},URLSearchParams,setInterval(){},fetch:()=>new Promise(()=>{}),
    crypto:{randomUUID:()=> '12345678-1234-1234-1234-123456789abc'},
    payload,
  });
  for(const name of ['app.js','community.js'])vm.runInContext(readFileSync(join(__dirname,'../web',name),'utf8'),context);
  vm.runInContext(`api=async()=>payload;session={guild_id:'99',channels:[]};dashboard={events:[],polls:[{id:'p1',Status:'closed','Channel ID':'123','Poll Date':'2026-09-24'}],members:[]};`,context);
  return {nodes,run:s=>vm.runInContext(s,context)};
}

test('task history escapes errors and creates only scoped Discord links', async()=>{
  const f=fixture({tasks:[{key:'daily:1',action:'<img onerror=bad>',status:'review',error:'<script>bad</script>',channel_id:'123',message_id:'456'}]});
  await f.run(`page='tasks';renderCommunity()`);
  const html=f.nodes.get('#content').innerHTML;
  assert.ok(html.includes('&lt;img onerror=bad&gt;'));
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('discord.com/channels/99/123/456'));
  assert.ok(html.includes('Check delivery'));
});

test('templates render explicit enable and draft actions',async()=>{
  const f=fixture({templates:[{key:'template:abc',name:'Friday night',enabled:false,next_at:'2026-10-02T11:00:00Z',interval_days:7}]});
  await f.run(`page='templates';renderCommunity()`);
  const html=f.nodes.get('#content').innerHTML;
  assert.ok(html.includes('Save paused template'));
  assert.ok(html.includes('Enable recurring posts'));
  assert.ok(html.includes('Create draft now'));
});

test('privacy and backup controls render off-site limitation and no automatic restore',async()=>{
  const f=fixture({config:{public_reasons:false,backups:true},backups:[]});
  await f.run(`page='community';renderCommunity()`);
  const html=f.nodes.get('#content').innerHTML;
  assert.ok(html.includes('/teemo_preferences'));
  assert.ok(html.includes('not a separate disaster-recovery service'));
  assert.ok(html.includes('does not overwrite existing rows'));
  assert.ok(!html.includes('name="public_reasons" checked'));
});

test('summary preview is read-only and exposes distinct update/resend buttons',async()=>{
  const f=fixture({embed:{title:'Tonight',description:'<script>bad</script>',fields:[{name:'Players',value:'Rz'}],footer:{text:'Bangkok'}}});
  f.run(`summaryControls('p1')`);
  await f.run(`communityAction('summary-preview',{dataset:{key:'p1'}})`);
  const html=f.nodes.get('#summary-preview').innerHTML;
  assert.ok(html.includes('Update original message'));
  assert.ok(html.includes('Resend as a NEW message'));
  assert.ok(html.includes('&lt;script&gt;'));
  assert.ok(f.run('summaryOperation.request_id.length')>=16);
});
