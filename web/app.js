/* Kitchen Queue - front end.
 *
 * Live state arrives over Server-Sent Events (plain HTTP, auto-reconnecting,
 * no library). Everything else is polled on a slow timer. Charts are hand-built
 * inline SVG sized to their container, so there is no chart library to load on
 * a phone sitting on school wifi.
 */

const DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
const $ = (id) => document.getElementById(id);
const tooltip = $('tooltip');

const css = (name) => getComputedStyle(document.body).getPropertyValue(name).trim();

let config = null;
let latestState = null;
let patternBuckets = [];
let historySamples = [];

/* ------------------------------------------------------------------ utils */

async function getJSON(path) {
  const res = await fetch(path, { cache: 'no-store' });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

const pad2 = (n) => String(n).padStart(2, '0');
const hhmm = (date) => `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
const minuteLabel = (m) => `${pad2(Math.floor(m / 60))}:${pad2(m % 60)}`;

function humanWait(seconds) {
  if (seconds == null) return '–';
  if (seconds < 45) return 'No real wait';
  const mins = Math.round(seconds / 60);
  return `About ${mins} min${mins === 1 ? '' : 's'} to be served`;
}

function humanAge(seconds) {
  if (seconds == null) return 'never';
  if (seconds < 10) return 'just now';
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  const mins = Math.round(seconds / 60);
  if (mins < 60) return `${mins} min ago`;
  return `${Math.round(mins / 60)}h ago`;
}

function showTip(event, html) {
  tooltip.innerHTML = html;
  tooltip.hidden = false;
  const box = tooltip.getBoundingClientRect();
  let x = event.clientX + 14;
  let y = event.clientY - box.height - 12;
  if (x + box.width > window.innerWidth - 8) x = event.clientX - box.width - 14;
  if (y < 8) y = event.clientY + 18;
  tooltip.style.left = `${x}px`;
  tooltip.style.top = `${y}px`;
}
const hideTip = () => { tooltip.hidden = true; };

/* -------------------------------------------------------------- rendering */

function renderState(state) {
  latestState = state;
  const hero = $('hero');
  const conn = $('conn');

  // Age is recomputed from the browser's clock rather than trusted from the
  // payload, so "updated 4s ago" keeps counting up between server pushes -
  // and a silently dead connection visibly goes stale instead of freezing.
  const age = state.updated_at == null
    ? null
    : Math.max(0, Date.now() / 1000 - state.updated_at);
  if (age != null && config) {
    if (age > config.offline_after_seconds) state.status = 'offline';
    else if (age > config.stale_after_seconds) state.status = 'stale';
  }

  conn.dataset.status = state.status;
  conn.querySelector('.conn-text').textContent =
    { live: 'Live', stale: 'Delayed', offline: 'Offline' }[state.status] || 'Connecting…';

  hero.dataset.tone = state.level.tone;
  $('level-name').textContent = state.level.name;
  $('count').textContent = state.count == null ? '–' : state.count;
  $('wait').textContent = state.status === 'offline' && state.count == null
    ? 'Counter unavailable'
    : humanWait(state.wait_seconds);

  const trendText = { 1: 'Getting busier', '-1': 'Clearing up' }[String(state.trend)] || '';
  $('trend').textContent = trendText;

  const fresh = $('freshness');
  if (state.updated_at == null) {
    fresh.textContent = 'No reading yet from the counter.';
  } else if (state.status === 'offline') {
    fresh.textContent = `The counter stopped reporting ${humanAge(age)}. `
      + 'This number is not current.';
  } else {
    const rate = state.service_rate_effective
      ? ` · serving ~${state.service_rate_effective}/min`
      : '';
    fresh.textContent = `Updated ${humanAge(age)}${rate}`;
  }

  $('foot-device').textContent = state.device ? `Counter: ${state.device}` : '';

  if (state.has_snapshot) {
    $('snapshot-card').hidden = false;
    refreshSnapshot();
  }
  renderAdvice();
}

let snapshotTimer = null;
function refreshSnapshot() {
  $('snapshot').src = `api/snapshot.jpg?t=${Date.now()}`;
  if (!snapshotTimer) snapshotTimer = setInterval(refreshSnapshot, 30000);
}

/* The single most useful thing on the page: not "how long is it now" but
 * "is it worth waiting fifteen minutes". Comes from the weekday pattern. */
function renderAdvice() {
  if (!patternBuckets.length || !latestState || latestState.count == null) return;
  const now = new Date();
  const nowMinute = now.getHours() * 60 + now.getMinutes();
  const ahead = patternBuckets.filter(
    (b) => b.minute > nowMinute && b.minute <= nowMinute + 75 && b.n >= 3,
  );
  if (!ahead.length) return;

  const best = ahead.reduce((a, b) => (b.avg < a.avg ? b : a));
  const current = latestState.count;
  const secs = latestState.service_seconds_per_person || 11;
  const line = $('advice-line');

  if (current <= 3) {
    line.innerHTML = 'Barely a line right now — <b>just go</b>.';
  } else if (best.avg < current - 3) {
    const saved = Math.round(((current - best.avg) * secs) / 60);
    line.innerHTML = `It usually eases off around <b>${minuteLabel(best.minute)}</b> `
      + `(typically ~${Math.round(best.avg)} people). Waiting could save you `
      + `roughly <b>${saved} min</b> of queueing.`;
  } else {
    line.innerHTML = 'It normally stays about this busy for the next hour — '
      + '<b>waiting probably won’t help</b>.';
  }
  $('advice').hidden = false;
}

/* ------------------------------------------------------------------ charts */

function svgEl(tag, attrs) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function emptyChart(slot, message) {
  slot.innerHTML = `<div class="chart-empty">${message}</div>`;
}

function median(values) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.floor(sorted.length / 2)];
}

function niceMax(value) {
  const target = Math.max(5, value * 1.18);
  const step = target > 40 ? 10 : target > 15 ? 5 : 2;
  return Math.ceil(target / step) * step;
}

/** Line + area of the last N minutes, with a crosshair tooltip. */
function drawLive(slot, samples) {
  slot.innerHTML = '';
  if (samples.length < 2) {
    emptyChart(slot, 'Not enough readings yet.');
    return;
  }

  const width = Math.max(280, slot.clientWidth);
  const height = 172;
  const pad = { l: 30, r: 10, t: 14, b: 22 };
  const plotW = width - pad.l - pad.r;
  const plotH = height - pad.t - pad.b;

  const t0 = samples[0].ts;
  const t1 = samples[samples.length - 1].ts;
  const span = Math.max(1, t1 - t0);
  const yMax = niceMax(Math.max(...samples.map((s) => s.count)));

  const x = (ts) => pad.l + ((ts - t0) / span) * plotW;
  const y = (v) => pad.t + plotH - (Math.min(v, yMax) / yMax) * plotH;

  const svg = svgEl('svg', {
    width, height, viewBox: `0 0 ${width} ${height}`, role: 'img',
    'aria-label': `Line chart of people in line over the last ${Math.round(span / 60)} minutes.`,
  });

  // recessive grid + y labels
  for (let i = 0; i <= 2; i += 1) {
    const v = (yMax / 2) * i;
    svg.appendChild(svgEl('line', {
      x1: pad.l, x2: width - pad.r, y1: y(v), y2: y(v),
      stroke: css('--grid'), 'stroke-width': 1,
    }));
    const label = svgEl('text', {
      x: pad.l - 7, y: y(v) + 4, 'text-anchor': 'end',
      fill: css('--text-muted'), 'font-size': 11,
    });
    label.textContent = String(Math.round(v));
    svg.appendChild(label);
  }

  const points = samples.map((s) => ({ ...s, px: x(s.ts), py: y(s.count) }));

  // A gap means the counter was down. Drawing straight through it would
  // invent readings that were never taken, so break the line into runs
  // whenever two samples are further apart than a few normal intervals.
  const typicalGap = median(points.slice(1).map((p, i) => p.ts - points[i].ts)) || 10;
  const runs = [[points[0]]];
  for (let i = 1; i < points.length; i += 1) {
    if (points[i].ts - points[i - 1].ts > Math.max(60, typicalGap * 4)) runs.push([]);
    runs[runs.length - 1].push(points[i]);
  }

  for (const run of runs) {
    if (run.length < 2) continue;
    const line = run.map((p, i) => `${i ? 'L' : 'M'}${p.px.toFixed(1)},${p.py.toFixed(1)}`).join('');
    svg.appendChild(svgEl('path', {
      d: `${line}L${run[run.length - 1].px.toFixed(1)},${pad.t + plotH}`
        + `L${run[0].px.toFixed(1)},${pad.t + plotH}Z`,
      fill: css('--series-1'), opacity: 0.12,
    }));
    svg.appendChild(svgEl('path', {
      d: line, fill: 'none', stroke: css('--series-1'),
      'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round',
    }));
  }

  // x labels at the ends plus the *temporal* midpoint - using the middle
  // sample instead would bunch the labels up wherever readings are dense.
  [
    { at: t0, anchor: 'start' },
    { at: t0 + span / 2, anchor: 'middle' },
    { at: t1, anchor: 'end' },
  ].forEach(({ at, anchor }) => {
    const label = svgEl('text', {
      x: x(at), y: height - 5, 'text-anchor': anchor,
      fill: css('--text-muted'), 'font-size': 11,
    });
    label.textContent = hhmm(new Date(at * 1000));
    svg.appendChild(label);
  });

  // latest value, direct-labelled - the one number worth annotating
  const last = points[points.length - 1];
  svg.appendChild(svgEl('circle', {
    cx: last.px, cy: last.py, r: 4.5,
    fill: css('--series-1'), stroke: css('--surface-1'), 'stroke-width': 2,
  }));

  const crosshair = svgEl('line', {
    y1: pad.t, y2: pad.t + plotH, stroke: css('--border-strong'),
    'stroke-width': 1, 'stroke-dasharray': '3 3', opacity: 0,
  });
  const marker = svgEl('circle', {
    r: 5, fill: css('--series-1'), stroke: css('--surface-1'), 'stroke-width': 2, opacity: 0,
  });
  svg.appendChild(crosshair);
  svg.appendChild(marker);

  const hit = svgEl('rect', {
    x: pad.l, y: pad.t, width: plotW, height: plotH, fill: 'transparent',
  });
  hit.addEventListener('pointermove', (event) => {
    const rect = svg.getBoundingClientRect();
    const px = event.clientX - rect.left;
    let nearest = points[0];
    for (const p of points) {
      if (Math.abs(p.px - px) < Math.abs(nearest.px - px)) nearest = p;
    }
    crosshair.setAttribute('x1', nearest.px);
    crosshair.setAttribute('x2', nearest.px);
    crosshair.setAttribute('opacity', 1);
    marker.setAttribute('cx', nearest.px);
    marker.setAttribute('cy', nearest.py);
    marker.setAttribute('opacity', 1);
    showTip(event, `<b>${Math.round(nearest.count)}</b> in line<br>${hhmm(new Date(nearest.ts * 1000))}`);
  });
  hit.addEventListener('pointerleave', () => {
    crosshair.setAttribute('opacity', 0);
    marker.setAttribute('opacity', 0);
    hideTip();
  });
  svg.appendChild(hit);

  slot.appendChild(svg);
}

function roundedTopBar(x, yTop, w, h, r) {
  const radius = Math.min(r, w / 2, h);
  return `M${x},${yTop + h}L${x},${yTop + radius}Q${x},${yTop} ${x + radius},${yTop}`
    + `L${x + w - radius},${yTop}Q${x + w},${yTop} ${x + w},${yTop + radius}`
    + `L${x + w},${yTop + h}Z`;
}

/** Average count by time of day for this weekday, with a "you are here" mark. */
function drawPattern(slot, buckets) {
  slot.innerHTML = '';
  if (buckets.length < 4) {
    emptyChart(slot, 'Not enough history yet — this fills in after a week or so.');
    return;
  }

  const width = Math.max(280, slot.clientWidth);
  const height = 190;
  const pad = { l: 30, r: 10, t: 14, b: 26 };
  const plotW = width - pad.l - pad.r;
  const plotH = height - pad.t - pad.b;

  const minMinute = buckets[0].minute;
  const maxMinute = buckets[buckets.length - 1].minute + 10;
  const span = Math.max(1, maxMinute - minMinute);
  const yMax = niceMax(Math.max(...buckets.map((b) => b.avg)));

  const x = (m) => pad.l + ((m - minMinute) / span) * plotW;
  const y = (v) => pad.t + plotH - (Math.min(v, yMax) / yMax) * plotH;
  const barW = Math.max(2, (plotW / (span / 10)) - 2);

  const svg = svgEl('svg', {
    width, height, viewBox: `0 0 ${width} ${height}`, role: 'img',
    'aria-label': 'Bar chart of the average number of people in line by time of day.',
  });

  for (let i = 0; i <= 2; i += 1) {
    const v = (yMax / 2) * i;
    svg.appendChild(svgEl('line', {
      x1: pad.l, x2: width - pad.r, y1: y(v), y2: y(v),
      stroke: css('--grid'), 'stroke-width': 1,
    }));
    const label = svgEl('text', {
      x: pad.l - 7, y: y(v) + 4, 'text-anchor': 'end',
      fill: css('--text-muted'), 'font-size': 11,
    });
    label.textContent = String(Math.round(v));
    svg.appendChild(label);
  }

  const now = new Date();
  const nowMinute = now.getHours() * 60 + now.getMinutes();
  const nowSlot = Math.floor(nowMinute / 10) * 10;

  for (const bucket of buckets) {
    const barY = y(bucket.avg);
    const h = pad.t + plotH - barY;
    const isNow = bucket.minute === nowSlot;
    const bar = svgEl('path', {
      d: roundedTopBar(x(bucket.minute) + 1, barY, barW, Math.max(h, 1.5), 4),
      fill: isNow ? css('--series-1-deep') : css('--series-1-mid'),
    });
    bar.addEventListener('pointerenter', (event) => showTip(
      event,
      `<b>${bucket.avg}</b> average · peak ${bucket.peak}<br>`
      + `${minuteLabel(bucket.minute)} · ${bucket.n} readings`,
    ));
    bar.addEventListener('pointerleave', hideTip);
    svg.appendChild(bar);
  }

  // Hour ticks, thinned on narrow screens so the labels never collide.
  const tickStep = width < 520 ? 120 : 60;
  for (let m = Math.ceil(minMinute / 60) * 60; m <= maxMinute; m += tickStep) {
    const label = svgEl('text', {
      x: x(m), y: height - 8, 'text-anchor': 'middle',
      fill: css('--text-muted'), 'font-size': 11,
    });
    label.textContent = minuteLabel(m);
    svg.appendChild(label);
  }

  if (nowMinute >= minMinute && nowMinute <= maxMinute) {
    svg.appendChild(svgEl('line', {
      x1: x(nowMinute), x2: x(nowMinute), y1: pad.t - 6, y2: pad.t + plotH,
      stroke: css('--text-secondary'), 'stroke-width': 1.5, 'stroke-dasharray': '4 3',
    }));
    const label = svgEl('text', {
      x: Math.min(x(nowMinute) + 5, width - 30), y: pad.t - 2,
      fill: css('--text-secondary'), 'font-size': 11, 'font-weight': 600,
    });
    label.textContent = 'now';
    svg.appendChild(label);
  }

  slot.appendChild(svg);
}

function renderTables() {
  $('table-live').innerHTML = historySamples.length
    ? `<table><thead><tr><th>Time</th><th>People in line</th></tr></thead><tbody>${
      historySamples.slice(-40).reverse().map(
        (s) => `<tr><td>${hhmm(new Date(s.ts * 1000))}</td><td>${Math.round(s.count)}</td></tr>`,
      ).join('')}</tbody></table>`
    : '<p class="muted small">No readings yet.</p>';

  $('table-pattern').innerHTML = patternBuckets.length
    ? `<table><thead><tr><th>Time</th><th>Average</th><th>Peak</th><th>Readings</th></tr></thead><tbody>${
      patternBuckets.map(
        (b) => `<tr><td>${minuteLabel(b.minute)}</td><td>${b.avg}</td><td>${b.peak}</td><td>${b.n}</td></tr>`,
      ).join('')}</tbody></table>`
    : '<p class="muted small">No history yet.</p>';
}

function redraw() {
  drawLive($('chart-live'), historySamples);
  drawPattern($('chart-pattern'), patternBuckets);
}

/* -------------------------------------------------------------- data flow */

async function loadSlowData() {
  try {
    const [history, pattern] = await Promise.all([
      getJSON('api/history?minutes=90'),
      getJSON('api/pattern'),
    ]);
    historySamples = history.samples;
    patternBuckets = pattern.buckets;
    $('pattern-title').textContent = `Typical ${DAYS[pattern.weekday]}`;
    redraw();
    renderTables();
    renderAdvice();
  } catch (err) {
    // Still draw, so the charts show their empty state rather than a blank
    // box that looks like a broken page.
    console.warn('slow data failed', err);
    redraw();
  }
}

function connect() {
  const source = new EventSource('api/stream');
  source.onmessage = (event) => {
    try {
      renderState(JSON.parse(event.data));
    } catch (err) {
      console.warn('bad state payload', err);
    }
  };
  source.onerror = () => {
    // EventSource retries on its own; just reflect reality in the UI.
    $('conn').dataset.status = 'offline';
    $('conn').querySelector('.conn-text').textContent = 'Reconnecting…';
  };
}

async function init() {
  try {
    config = await getJSON('api/config');
    $('site-name').textContent = config.site_name;
    $('queue-name').textContent = config.queue_name;
    document.title = `${config.site_name} — queue`;
  } catch (err) {
    console.warn('config failed', err);
  }

  await loadSlowData();
  connect();
  initLive();

  setInterval(loadSlowData, 60000);
  // Keep the "updated Xs ago" line honest between pushes.
  setInterval(() => latestState && renderState(latestState), 5000);

  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(redraw, 150);
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) loadSlowData();
  });
}

init();
