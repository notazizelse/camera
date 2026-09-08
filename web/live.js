/* Live camera panel.
 *
 * Three ways to show a picture, in descending order of quality, each falling
 * back to the next when the browser or the network will not cooperate:
 *
 *   1. HLS played natively (Safari, iOS - no library at all)
 *   2. HLS via hls.js (everything else)
 *   3. JPEG frames, either as a multipart stream or polled one at a time
 *
 * The third always works. That matters more than it sounds: it is the path
 * that survives a school proxy that mangles everything else, and it is what
 * the page lands on rather than showing a broken player.
 */

const HLS_LIB = [
  'vendor/hls.min.js',
  'https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.17/hls.min.js',
];

const live = {
  card: null, video: null, img: null,
  mode: null, hls: null, poll: null, blackTimer: null,
};

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
  live.img.removeAttribute('src');
  live.mode = null;
}

function showVideo(useVideoTag) {
  live.video.hidden = !useVideoTag;
  live.img.hidden = useVideoTag;
}

/* JPEG fallback. The multipart stream is one long-lived request; if the
   browser or a proxy refuses it, poll single frames instead. */
function startFrames(note) {
  showVideo(false);
  live.img.src = 'api/live.mjpg';
  live.img.onerror = () => {
    live.img.onerror = null;
    if (live.poll) return;
    live.poll = setInterval(() => {
      live.img.src = `api/live.jpg?t=${Date.now()}`;
    }, 2000);
    live.img.src = `api/live.jpg?t=${Date.now()}`;
    note.textContent = 'Refreshed every 2 seconds.';
  };
  note.textContent = 'Updating as frames arrive.';
}

async function startPlayer(mode) {
  if (live.mode === mode) return;
  teardownPlayer();
  live.mode = mode;
  const note = document.getElementById('live-note');

  if (mode === 'snapshot') {
    startFrames(note);
    return;
  }

  const src = 'live/stream.m3u8';
  const nativeClaim = live.video.canPlayType('application/vnd.apple.mpegurl');

  const useNative = () => {
    showVideo(true);
    live.video.src = src;
    live.video.onerror = () => { live.mode = null; startPlayer('snapshot'); };
    live.video.play().catch(() => { /* controls are there if autoplay is blocked */ });
    note.textContent = 'Live, about 8 seconds behind.';
    watchForBlackScreen(note);
  };

  // Chromium answers "maybe" to the HLS canPlayType question and then cannot
  // play it, so that answer is only trustworthy when Media Source Extensions
  // are missing - which is exactly the iOS Safari case where native playback
  // is genuinely the right path, and where loading a library would be waste.
  if (!window.MediaSource && nativeClaim) {
    useNative();
    return;
  }

  if (await ensureHlsLib() && window.Hls.isSupported()) {
    showVideo(true);
    live.hls = new window.Hls({ lowLatencyMode: true, backBufferLength: 10 });
    live.hls.loadSource(src);
    live.hls.attachMedia(live.video);
    live.hls.on(window.Hls.Events.ERROR, (_event, data) => {
      // The pusher stopped, or the playlist rolled away under us. Frames
      // still work, so drop to them rather than showing a black rectangle.
      if (data.fatal) {
        teardownPlayer();
        startPlayer('snapshot');
      }
    });
    live.video.play().catch(() => {});
    note.textContent = 'Live, about 8 seconds behind.';
    watchForBlackScreen(note);
    return;
  }

  if (nativeClaim) {
    useNative();
    return;
  }
  startFrames(note);
}

/* Every failure above still leaves the possibility of a player that reports
   no error and shows nothing. If no frame has decoded after 12 seconds, stop
   believing the player and switch to something that works. */
function watchForBlackScreen(note) {
  clearTimeout(live.blackTimer);
  live.blackTimer = setTimeout(() => {
    if (live.mode !== 'snapshot' && live.video.readyState < 2) {
      teardownPlayer();
      startFrames(note);
      live.mode = 'snapshot';
    }
  }, 12000);
}

async function refreshMedia() {
  let media;
  try {
    media = await getJSON('api/media-state');
  } catch {
    return;
  }

  // The promises at the bottom of the page have to match what the system
  // actually does, so they follow the mode rather than being hardcoded.
  document.getElementById('privacy-count').hidden = media.enabled;
  document.getElementById('privacy-video').hidden = !media.enabled;

  if (!media.enabled) {
    live.card.hidden = true;
    return;
  }
  live.card.hidden = false;
  document.getElementById('gate-note').textContent = media.note || '';

  const gate = document.getElementById('gate');
  const player = document.getElementById('player');
  const offline = document.getElementById('live-offline');
  const status = document.getElementById('live-status');

  if (!media.authorized) {
    gate.hidden = false;
    player.hidden = true;
    offline.hidden = true;
    status.textContent = media.available ? 'camera online' : 'camera offline';
    teardownPlayer();
    return;
  }

  gate.hidden = true;
  if (!media.available) {
    player.hidden = true;
    offline.hidden = false;
    status.textContent = 'offline';
    teardownPlayer();
    return;
  }

  offline.hidden = true;
  player.hidden = false;
  status.textContent = `${media.mode} · ${humanAge(media.age_seconds)}`;
  startPlayer(media.mode);
}

function initLive() {
  live.card = document.getElementById('live-card');
  live.video = document.getElementById('video');
  live.img = document.getElementById('video-img');
  if (!live.card) return;

  document.getElementById('gate').addEventListener('submit', async (event) => {
    event.preventDefault();
    const input = document.getElementById('gate-code');
    const error = document.getElementById('gate-error');
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

  refreshMedia();
  setInterval(refreshMedia, 15000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshMedia();
  });
}
