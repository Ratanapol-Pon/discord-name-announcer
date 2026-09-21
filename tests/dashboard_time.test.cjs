const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

// Load the real dashboard without a browser, login, timers, or network access.
const element = { addEventListener() {} };
const context = vm.createContext({
  document: {
    documentElement: { dataset: { theme: 'light' } },
    querySelector: () => element,
    querySelectorAll: () => [],
    addEventListener() {},
  },
  localStorage: { setItem() {} },
  location: { hash: '' },
  URLSearchParams,
  fetch: () => new Promise(() => {}),
  setInterval() {},
});
vm.runInContext(readFileSync(join(__dirname, '../web/app.js'), 'utf8'), context);
const evaluate = (source) => vm.runInContext(source, context);

test('durations retain seconds and never wrap at 24 hours', () => {
  for (const [input, expected] of [
    [0, '00:00:00'], [9, '00:00:09'], [59, '00:00:59'], [60, '00:01:00'],
    [3599, '00:59:59'], [3600, '01:00:00'], [5445, '01:30:45'],
    [86400, '24:00:00'], [450605, '125:10:05'], ['61', '00:01:01'],
    [61.9, '00:01:01'], [-1, '00:00:00'], [null, '00:00:00'], ['bad', '00:00:00'],
  ]) assert.equal(evaluate(`duration(${JSON.stringify(input)})`), expected);
  assert.equal(evaluate('duration(Infinity)'), '00:00:00');
  assert.equal(evaluate('duration(undefined)'), '00:00:00');
});

test('Bangkok timestamps and display-only schedules include seconds', () => {
  assert.match(evaluate('fmtDate("2026-09-21T10:15:09Z")'), /17:15:09/);
  assert.match(evaluate('fmtDate("2026-09-20T17:00:00Z")'), /00:00:00/);
  assert.equal(evaluate('fmtDate(null)'), '—');
  assert.equal(evaluate('clockTime("11:59")'), '11:59:00');
  assert.equal(evaluate('clockTime("17:00:09")'), '17:00:09');
  assert.equal(evaluate('clockTime("Flexible")'), 'Flexible');
});

test('cards, charts, member totals and session tables share the duration format', () => {
  evaluate(`dashboard = {
    stats: {responses: 4, voice_seconds: 450605, solo_seconds: 59, members: 1},
    settings: {poll_time:'11:59',report_time:'17:00',poll_enabled:true,report_enabled:true},
    members: [{name:'Rz',seconds:450605,solo_seconds:59,sessions:1}],
    live: [], solo: [], voice: [{name:'Rz',channel:'Games',seconds:5445,
      joined:'2026-09-21T10:15:09Z',left:'2026-09-21T11:45:54Z',active:false}]
  }`);
  assert.match(evaluate('stats()'), /125:10:05/);
  assert.match(evaluate('stats()'), /00:00:59/);
  assert.match(evaluate('memberChart()'), /125:10:05/);
  assert.match(evaluate('voicePage()'), /<td>125:10:05<\/td>/);
  assert.match(evaluate('voicePage()'), /<td>00:00:59<\/td>/);
  assert.match(evaluate('voiceTable(dashboard.voice)'), /01:30:45/);
  assert.match(evaluate('voiceTable(dashboard.voice)'), /17:15:09/);
  assert.match(evaluate('scheduleCard()'), /11:59:00/);
  assert.equal(evaluate('dashboard.settings.poll_time'), '11:59');
});
