/* Numeric epoch seconds throughout: delayed batches and SSE replay are upserts. */
(function (root) {
  function upsert(series, ts, value, limit = 600) {
    if (ts == null || ts === '') return;
    const x = Number(ts);
    if (!Number.isFinite(x)) return;
    const y = value == null ? null : Number(value);
    const point = {x, y: y == null || !Number.isFinite(y) ? null : y};
    const index = series.findIndex(item => item.x >= x);
    if (index < 0) series.push(point);
    else if (series[index].x === x) series[index] = point;
    else series.splice(index, 0, point);
    if (series.length > limit) series.splice(0, series.length - limit);
  }

  function formatTime(ts) {
    return new Date(Number(ts) * 1000).toLocaleTimeString('zh-CN', {hour12: false});
  }

  root.SqlPulseTimeline = {upsert, formatTime};
})(globalThis);
