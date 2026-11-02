const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const template = fs.readFileSync(path.join(root, 'app/templates/monitor.html'), 'utf8');
const pageScript = template.match(/<script>\s*([\s\S]*?)<\/script>/)[1]
  .replace(/{{\s*\(run\.ended_at or ''\)\s*\|\s*tojson\s*}}/, '""')
  .replace(/{{\s*run\.id\s*}}/g, 'fixture')
  .replace(/{{\s*run\.started_at or ""\s*}}/g, '2026-11-02T12:00:00');

function fixture() {
  const elements = new Map();
  const calls = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      textContent: '', className: '', dataset: {}, style: {},
      getContext: () => ({id}), addEventListener() {},
      replaceChildren() {}, appendChild() {},
    });
    return elements.get(id);
  };
  let status = 'running';
  let points = [];
  const sandbox = {
    console, Date, setInterval: () => 1, clearInterval() {},
    requestAnimationFrame: callback => callback(),
    document: {getElementById: element, querySelectorAll: () => [], createElement: element},
    Chart: class {
      constructor(ctx, config) { Object.assign(this, config); }
      update() {}
    },
    EventSource: class { addEventListener() {} close() {} },
    async fetch(url) {
      calls.push(url);
      let payload;
      if (url.endsWith('/per-sql-metrics')) payload = {sql_1: [{ts: 100, p99_ms: 20}]};
      else if (url.endsWith('/events')) payload = {events: [], thresholds: {}};
      else if (url.endsWith('/metrics')) payload = {points};
      else payload = {status, ended_at: '2026-11-02T12:00:10', observability: {quality: 'valid', request_count: 120, latest_capacity: {}}};
      return {ok: true, json: async () => payload};
    },
  };
  const context = vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(root, 'app/static/monitor_timeline.js'), 'utf8'), context);
  vm.runInContext(pageScript, context);
  return {context, calls, elements, finish(newPoints) { status = 'finished'; points = newPoints; }};
}

test('SQL arrival and replay do not add resource points or duplicate source points', async () => {
  const {context} = fixture();
  await new Promise(setImmediate);
  vm.runInContext(`
    onPoint({ts: 105.5, metric_scope: 'resource', cpu_pct: 40, threads_connected: 2});
    onPoint({ts: 100, source_ts: 100, metric_scope: 'sql', qps: 10, avg_ms: 5, p95_ms: 10, p99_ms: 20, request_count: 100});
    onPoint({ts: 100, source_ts: 100, metric_scope: 'sql', qps: 10, request_count: 100});
  `, context);
  const result = vm.runInContext(`({sql: charts.qps.data.datasets[0].data, resource: charts.capacityCpu.data.datasets[0].data, axis: charts.qps.options.scales.x.type})`, context);
  assert.deepEqual(JSON.parse(JSON.stringify(result)), {
    sql: [{x: 100, y: 10}], resource: [{x: 105.5, y: 40}], axis: 'linear',
  });
  assert.equal(context.document.getElementById('obs-threads-connected').textContent, '2');
});

test('terminal refresh replaces all timelines and refreshes per-SQL and events', async () => {
  const f = fixture();
  await new Promise(setImmediate);
  f.finish([
    {ts: 100, source_ts: 100, metric_scope: 'sql', qps: 10, request_count: 100},
    {ts: 105.5, metric_scope: 'resource', cpu_pct: 40},
    {ts: 106, source_ts: 106, metric_scope: 'sql', qps: 12, request_count: 120},
  ]);
  const start = f.calls.length;
  await vm.runInContext('finishPoll()', f.context);
  await vm.runInContext('finishPoll()', f.context); // repeated end signals are harmless
  const result = vm.runInContext(`({sql: charts.qps.data.datasets[0].data.map(p=>p.x), resource: charts.capacityCpu.data.datasets[0].data.map(p=>p.x), done: finished})`, f.context);
  assert.deepEqual(JSON.parse(JSON.stringify(result)), {sql: [100, 106], resource: [105.5], done: true});
  const terminalCalls = f.calls.slice(start);
  assert.ok(terminalCalls.some(url => url.endsWith('/per-sql-metrics')));
  assert.ok(terminalCalls.some(url => url.endsWith('/events')));
  assert.equal(f.elements.get('collector-state').textContent, '任务已结束');
});
