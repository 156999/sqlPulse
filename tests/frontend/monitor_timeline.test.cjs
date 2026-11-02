const assert = require('node:assert/strict');
const test = require('node:test');
require('../../app/static/monitor_timeline.js');
const {upsert} = globalThis.SqlPulseTimeline;

test('delayed batches sort by source time and SSE replay replaces a point', () => {
  const data = [];
  upsert(data, 105, 50);
  upsert(data, 100, 10);
  upsert(data, 102, 20);
  upsert(data, 102, 21);
  assert.deepEqual(data, [{x: 100, y: 10}, {x: 102, y: 21}, {x: 105, y: 50}]);
  assert.equal(data[2].x - data[1].x, 3); // elapsed time is retained, not a category index
});

test('null values remain gaps and invalid timestamps cannot corrupt axes', () => {
  const data = [];
  upsert(data, 100, null);
  upsert(data, NaN, 40);
  assert.deepEqual(data, [{x: 100, y: null}]);
});

test('out-of-order old replay cannot evict the most recent window', () => {
  const data = [];
  for (const ts of [100, 101, 102, 90, 103]) upsert(data, ts, ts, 3);
  assert.deepEqual(data.map(p => p.x), [101, 102, 103]);
});

test('bundled Chart.js keeps elapsed spacing and breaks a long sampling gap', () => {
  const Chart = require('../../app/static/vendor/chart.umd.min.js');
  const canvas = {width: 900, height: 300};
  const context = new Proxy({
    canvas,
    measureText: text => ({width: String(text).length * 6}),
    getLineDash: () => [],
  }, {get: (target, key) => key in target ? target[key] : () => {}});
  canvas.getContext = () => context;
  const chart = new Chart(canvas, {
    type: 'line',
    data: {datasets: [{data: [{x: 100, y: 10}, {x: 101, y: 20}, {x: 105, y: 30}], spanGaps: 2.5}]},
    options: {responsive: false, animation: false, scales: {x: {type: 'linear'}}, plugins: {legend: false}},
  });
  try {
    const points = chart.getDatasetMeta(0).data;
    assert.equal(points[2].stop, true);
    assert.ok(Math.abs((points[2].x - points[1].x) / (points[1].x - points[0].x) - 4) < 0.001);
  } finally {
    chart.destroy();
  }
});
