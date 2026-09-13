/* Live camera panel.
 *
 * Playback degrades on purpose, in this order:
 *
 *   1. HLS through hls.js              (Chrome, Edge, Firefox, Android)
 *   2. HLS played natively             (iOS Safari, where MSE is absent)
 *   3. JPEG frames as a multipart stream
 *   4. JPEG frames polled one at a time
 *
 * The last one always works. That matters more than it sounds: it is the
 * path that survives a school proxy which mangles everything else, and it is
 * where the page lands instead of showing a black rectangle.
 */

const HLS_LIB = [
  'vendor/hls.min.js',
  'https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.17/hls.min.js',
];

const live = {
  card: null, video: null, img: null, stage: null,
  mode: null, hls: null, poll: null, blackTimer: null,
  cameras: [], current: null, authorized: false,
};

const el = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ chrome */

function toast(message) {
  const node = el('toast');
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, 2600);
}

/* Theme cycles light -> dark -> system. "System" is the absence of a stamp,
   which is what the CSS is written against, so clearing it is the reset. */
function applyTheme(theme) {
  if (theme === 'light' || theme === 'dark') {
    document.documentElement.setAttribute('data-theme', theme);
  } else {
    document.documentElement.removeAttribute('data-theme');
  }
  try { localStorage.setItem('theme', theme); } catch { /* private mode */ }
}

function initTheme() {
  let saved = 'system';
  try { saved = localStorage.getItem('theme') || 'system'; } catch { /* ignore */ }
  applyTheme(saved);
  el('theme-btn').addEventListener('click', () => {
    const order = ['light', 'dark', 'system'];
    let current = 'system';
    try { current = localStorage.getItem('theme') || 'system'; } catch { /* ignore */ }
    const next = order[(order.indexOf(current) + 1) % order.length];
    applyTheme(next);
    toast(`Theme: ${next}`);
    if (typeof redraw === 'function') redraw();   // charts read CSS tokens
  });
}

/* ------------------------------------------------------------------ player */

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const tag = document.createElement('script');
    tag.src = src;
    tag.onload = resolve;
    tag.onerror = () => reject(new Error(`could not load ${src}`));
    document.head.appendChild(tag);
  });
}

async function ensureHlsLib() {
  if (window.Hls) return true;
  for (const src of HLS_LIB) {
    try {
      await loadScript(src);
      if (window.Hls) return true;
    } catch { /* try the next source */ }
  }
  return false;
}

function teardownPlayer() {
  if (live.hls) { live.hls.destroy(); live.hls = null; }
  if (live.poll) { clearInterval(live.poll); live.poll = null; }
  clearTimeout(live.blackTimer);
  live.video.onerror = null;
  live.video.removeAttribute('src');
  live.video.load();
  live.img.onerror = null;
  live.img.removeAttribute('src');
  live.mode = null;
}

function showVideoTag(useVideo) {
  live.video.hidden = !useVideo;
  live.img.hidden = useVideo;
  el('pip-btn').hidden = !(useVideo && document.pictureInPictureEnabled);
}

function setMeta(text) { el('live-meta').textContent = text; }

function stageMessage(title, body) {
  el('stage-message').hidden = !title;
  if (title) {
    el('stage-message-title').textContent = title;
    el('stage-message-body').textContent = body || '';
  }
}

function startFrames(camera) {
  showVideoTag(false);
  stageMessage(null);

  let failures = 0;
  live.img.onerror = () => {
    failures += 1;
    if (failures === 1) {
      // The multipart stream was refused somewhere upstream. Poll instead.
      live.poll = setInterval(() => {
        live.img.src = `api/live/${camera}.jpg?t=${Date.now()}`;
      }, 2000);
      live.img.src = `api/live/${camera}.jpg?t=${Date.now()}`;
      setMeta('stills · refreshed every 2s');
      return;
    }
    if (failures >= 3) {
      // Stills are the last resort, so if they fail too there is nothing
      // left to try. Say so rather than leaving a broken image on screen.
      if (live.poll) { clearInterval(live.poll); live.poll = null; }
      live.img.onerror = null;
      live.img.removeAttribute('src');
      stageMessage('Live view unavailable',
        'The stream reached this page but could not be played. The queue count above still works.');
      setMeta('');
    }
  };

  live.img.src = `api/live/${camera}.mjpg`;
  setMeta('stills · updating as frames arrive');
}

/* A player can report no error and still show nothing. If no frame has
   decoded after 12 seconds, stop believing it and fall back. */
function watchForBlackScreen(camera) {
  clearTimeout(live.blackTimer);
  live.blackTimer = setTimeout(() => {
    if (live.mode !== 'frames' && live.video.readyState < 2) {
      teardownPlayer();
      live.mode = 'frames';
      startFrames(camera);
    }
  }, 12000);
}

async function startPlayer(camera, mode) {
  const key = `${camera}:${mode}`;
  if (live.mode === key) return;
  teardownPlayer();
  live.mode = key;
  stageMessage(null);

  if (mode === 'snapshot') {
    live.mode = 'frames';
    startFrames(camera);
    return;
  }

  const src = `live/${camera}/stream.m3u8`;
  const nativeClaim = live.video.canPlayType('application/vnd.apple.mpegurl');

  const useNative = () => {
    showVideoTag(true);
    live.video.src = src;
    live.video.onerror = () => { live.mode = null; startPlayer(camera, 'snapshot'); };
    live.video.play().catch(() => { /* autoplay blocked; the tap will start it */ });
    setMeta('HLS (native) · about 8s behind live');
    watchForBlackScreen(camera);
  };

  // Chromium answers "maybe" to this question and then cannot play it, so the
  // answer is only trustworthy where Media Source Extensions are missing -
  // exactly the iOS Safari case, where native really is the right path and
  // downloading a library would be waste.
  if (!window.MediaSource && nativeClaim) {
    useNative();
    return;
  }

  if (await ensureHlsLib() && window.Hls.isSupported()) {
    showVideoTag(true);
    live.hls = new window.Hls({ lowLatencyMode: true, backBufferLength: 10 });
    live.hls.loadSource(src);
    live.hls.attachMedia(live.video);
    live.hls.on(window.Hls.Events.ERROR, (_event, data) => {
      if (data.fatal) {
        teardownPlayer();
        live.mode = 'frames';
        startFrames(camera);
      }
    });
    live.video.play().catch(() => {});
    setMeta('HLS · about 8s behind live');
    watchForBlackScreen(camera);
    return;
  }

  if (nativeClaim) { useNative(); return; }
  live.mode = 'frames';
  startFrames(camera);
}

/* ------------------------------------------------------------------ tools */

function currentFrameSource() {
  return live.video.hidden ? live.img : live.video;
}

/* Save what is on screen. Canvas works here because everything is served
   from this origin, so the canvas never gets tainted. */
function saveStill() {
  const source = currentFrameSource();
  const width = source.videoWidth || source.naturalWidth;
  const height = source.videoHeight || source.naturalHeight;
  if (!width || !height) { toast('Nothing to save yet'); return; }

  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  canvas.getContext('2d').drawImage(source, 0, 0, width, height);
  canvas.toBlob((blob) => {
    if (!blob) { toast('Could not save the image'); return; }
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    link.href = url;
    link.download = `${live.current || 'camera'}-${stamp}.jpg`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    toast('Still saved');
  }, 'image/jpeg', 0.92);
}

async function toggleFullscreen() {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await live.stage.requestFullscreen();
  } catch { toast('Fullscreen not available'); }
}

async function togglePip() {
  try {
    if (document.pictureInPictureElement) await document.exitPictureInPicture();
    else await live.video.requestPictureInPicture();
  } catch { toast('Picture-in-picture not available'); }
}

/* ------------------------------------------------------------------ state */

function renderCameras() {
  const host = el('cameras');
  // A single camera needs no switcher; the card title already names it.
  host.hidden = live.cameras.length < 2;
  if (host.hidden) { host.innerHTML = ''; return; }

  host.innerHTML = '';
  live.cameras.forEach((camera, index) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip';
    chip.dataset.live = String(!!camera.available);
    chip.setAttribute('aria-pressed', String(camera.id === live.current));
    chip.title = `${camera.label}${index < 9 ? ` (${index + 1})` : ''}`;
    chip.innerHTML = '<span class="chip-dot" aria-hidden="true"></span>';
    chip.append(camera.label);
    chip.addEventListener('click', () => selectCamera(camera.id));
    host.appendChild(chip);
  });
}

function selectCamera(id) {
  if (id === live.current) return;
  live.current = id;
  try { localStorage.setItem('camera', id); } catch { /* ignore */ }
  teardownPlayer();
  renderCameras();
  refreshMedia();
}

function renderPlayerFor(camera) {
  el('badge-text').textContent = camera.available ? 'Live' : 'Offline';
  el('stage-badge').dataset.live = String(!!camera.available);
  el('live-status').textContent = camera.label;

  if (!camera.available) {
    teardownPlayer();
    showVideoTag(false);
    stageMessage('Camera offline',
      'Nothing is arriving from the cafeteria PC. The queue count above still works.');
    setMeta('');
    return;
  }
  startPlayer(camera.id, camera.mode);
}

async function refreshMedia() {
  let media;
  try {
    media = await getJSON('api/media-state');
  } catch {
    return;
  }

  // The promises at the bottom of the page must match what the system
  // actually does, so they follow the mode rather than being hardcoded.
  el('privacy-count').hidden = media.enabled;
  el('privacy-video').hidden = !media.enabled;

  if (!media.enabled) {
    live.card.hidden = true;
    return;
  }
  live.card.hidden = false;
  live.cameras = media.cameras || [];
  live.authorized = !!media.authorized;
  el('gate-note').textContent = media.note || '';

  if (!live.cameras.some((c) => c.id === live.current)) {
    let saved = null;
    try { saved = localStorage.getItem('camera'); } catch { /* ignore */ }
    live.current = (live.cameras.some((c) => c.id === saved) && saved)
      || media.default
      || (live.cameras[0] && live.cameras[0].id)
      || null;
  }

  const gate = el('gate');
  const player = el('player');

  if (!media.authorized) {
    gate.hidden = false;
    player.hidden = true;
    el('cameras').hidden = true;
    el('live-status').textContent = live.cameras.some((c) => c.available)
      ? 'camera online' : 'camera offline';
    teardownPlayer();
    return;
  }

  gate.hidden = true;
  player.hidden = false;
  renderCameras();

  const camera = live.cameras.find((c) => c.id === live.current);
  if (!camera) {
    stageMessage('No cameras', 'Nothing has been connected to this site yet.');
    return;
  }
  renderPlayerFor(camera);
}

/* ------------------------------------------------------------------- init */

function initLive() {
  live.card = el('live-card');
  live.video = el('video');
  live.img = el('video-img');
  live.stage = el('stage');
  if (!live.card) return;

  el('gate').addEventListener('submit', async (event) => {
    event.preventDefault();
    const input = el('gate-code');
    const error = el('gate-error');
    error.hidden = true;
    try {
      const res = await fetch('api/access', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: input.value }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        error.textContent = body.error || 'That did not work.';
        error.hidden = false;
        return;
      }
      input.value = '';
      refreshMedia();
    } catch {
      error.textContent = 'Could not reach the server.';
      error.hidden = false;
    }
  });

  el('snap-btn').addEventListener('click', saveStill);
  el('full-btn').addEventListener('click', toggleFullscreen);
  el('pip-btn').addEventListener('click', togglePip);

  document.addEventListener('keydown', (event) => {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const tag = (event.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea') return;

    const key = event.key.toLowerCase();
    if (key === 't') { el('theme-btn').click(); return; }
    if (!live.authorized || live.card.hidden) return;
    if (key === 'f') toggleFullscreen();
    else if (key === 's') saveStill();
    else if (key === 'p' && !el('pip-btn').hidden) togglePip();
    else if (/^[1-9]$/.test(key)) {
      const camera = live.cameras[Number(key) - 1];
      if (camera) selectCamera(camera.id);
    }
  });

  refreshMedia();
  setInterval(refreshMedia, 15000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshMedia();
  });
}
