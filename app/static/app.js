const MAX_SELECTED = 6;
const MAX_VIDEOS = 15;
const PLATFORM = { youtube: 'YouTube Shorts', tiktok: 'TikTok' };
const CLIP_STATE = {
  queued: 'Waiting',
  downloading: 'Downloading',
  downloaded: 'Downloaded',
  spotting: 'Spotter agent is finding the product',
  handed: 'Handed to the director',
  planned: 'Segment chosen',
  rendering: 'Drawing the overlay',
  done: 'In the ad',
  skipped: 'Skipped',
  error: 'Failed',
};
const SETTLED = ['done', 'skipped', 'error'];
// One colour per clip, shared by its segment window and its block in the final cut.
const CLIP_COLORS = ['#ffd60a', '#25f4ee', '#ff6b9d', '#9b8cff', '#7dff8a', '#ff9f43'];

const $ = (id) => document.getElementById(id);
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

let jobId = null;
let source = null;
let scouting = false;
let rendering = false;
let style = 'spotlight';
let styles = [];
const videos = new Map();
const selected = new Set();
const clips = new Map(); // key -> { state, detail, duration, filmstrip, segment, candidate }
let renderOrder = [];
let credits = [];

function compact(n) {
  if (n === null || n === undefined) return '';
  for (const [limit, suffix] of [[1e9, 'B'], [1e6, 'M'], [1e3, 'K']]) {
    if (n >= limit) {
      const value = n / limit;
      return (value >= 100 ? value.toFixed(0) : value.toFixed(1).replace(/\.0$/, '')) + suffix;
    }
  }
  return String(n);
}

function reach(video) {
  const unit = video.platform === 'youtube' ? 'subscribers' : 'followers';
  const parts = [];
  if (video.followers) parts.push(`${compact(video.followers)} ${unit}`);
  if (video.views) parts.push(`${compact(video.views)} views`);
  return parts.join(' · ');
}

function clock(seconds) {
  const whole = Math.max(0, Math.round(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
}

async function pollAgents() {
  try {
    const health = await (await fetch('/api/health')).json();
    if (health.ready) {
      $('agents').className = 'agents ready';
      $('agents-text').textContent = 'ZooWork agents ready: Scout, Spotter, Director';
      return;
    }
    if (health.error) {
      $('agents').className = 'agents failed';
      $('agents-text').textContent = `ZooWork agents failed: ${health.error}`;
      return;
    }
  } catch {
    // The server may still be starting.
  }
  setTimeout(pollAgents, 1500);
}

async function loadStyles() {
  styles = await (await fetch('/api/styles')).json();
  $('styles').replaceChildren(...styles.map((item) => {
    const button = el('button', `style ${item.kind}`, item.label);
    button.type = 'button';
    button.dataset.key = item.key;
    button.title = item.description;
    if (item.kind === 'agent') button.append(el('span', 'tag', 'Opus 5.5'));
    button.onclick = () => {
      if (rendering) return;
      style = item.key;
      updateBar();
    };
    return button;
  }));
  updateBar();
}

function reset() {
  if (source) source.close();
  videos.clear();
  selected.clear();
  clips.clear();
  credits = [];
  for (const id of ['steps', 'grid', 'clips', 'credits', 'cut', 'log']) $(id).replaceChildren();
  for (const id of ['product-panel', 'summary', 'studio', 'final', 'bar', 'cut-panel', 'log-panel']) $(id).hidden = true;
  $('count').textContent = '0';
  $('results-hint').textContent = 'Videos appear here as the agents find them.';
  $('scout').hidden = false;
  document.body.classList.add('started');
}

async function startSearch(query) {
  reset();
  scouting = true;
  $('search-button').disabled = true;
  addStep('boot', 'running', 'Starting', 'Handing your product to the scout agents');
  const res = await fetch('/api/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query }),
  });
  if (!res.ok) return fail((await res.json()).detail || 'Could not start the search');
  jobId = (await res.json()).id;
  source = new EventSource(`/api/jobs/${jobId}/events`);
  source.onmessage = (message) => handle(JSON.parse(message.data));
}

function handle(event) {
  switch (event.type) {
    case 'status':
      if (event.stage === 'scout') addStep('boot', 'running', 'Starting', event.text);
      if (event.stage === 'render') $('studio-title').textContent = event.text;
      break;
    case 'step':
      addStep('boot', 'done');
      addStep(event.id, event.state, event.title, event.detail);
      break;
    case 'product':
      $('product-panel').hidden = false;
      $('product-name').textContent = event.product.name;
      $('product-brand').textContent = [event.product.brand, event.product.category].filter(Boolean).join(' · ');
      $('product-look').textContent = event.product.visual_description;
      break;
    case 'video':
      addVideo(event.video);
      break;
    case 'video_update':
      updateVideo(event);
      break;
    case 'scout_done':
      scouting = false;
      addStep('boot', 'done');
      $('search-button').disabled = false;
      $('summary').hidden = false;
      $('summary').textContent = event.summary;
      $('results-hint').textContent = `Sorted by reach. Tick up to ${MAX_SELECTED}, pick a style, then make your ad.`;
      sortByReach();
      updateBar();
      break;
    case 'clip':
      updateClip(event);
      break;
    case 'director':
      addLog(event.text);
      break;
    case 'final':
      showFinal(event);
      break;
    case 'error':
      fail(event.message);
      break;
  }
}

function fail(message) {
  scouting = false;
  rendering = false;
  $('search-button').disabled = false;
  addStep('boot', 'error', 'Something went wrong', message);
  if (!$('studio').hidden) $('studio-title').textContent = `Could not finish: ${message}`;
  updateBar();
}

function addStep(id, state, title, detail) {
  let item = document.querySelector(`#steps [data-id="${CSS.escape(id)}"]`);
  if (!item) {
    if (!title) return;
    item = el('li');
    item.dataset.id = id;
    item.append(el('span', 'icon'), el('div'));
    item.lastChild.append(el('div', 'title'), el('div', 'detail'));
    $('steps').append(item);
    $('steps').scrollTop = $('steps').scrollHeight;
  }
  item.className = state;
  if (title) item.querySelector('.title').textContent = title;
  if (detail !== undefined) item.querySelector('.detail').textContent = detail;
}

function embedUrl(video) {
  return video.platform === 'youtube'
    ? `https://www.youtube.com/embed/${video.video_id}?autoplay=1&playsinline=1&rel=0`
    : `https://www.tiktok.com/player/v1/${video.video_id}?autoplay=1&music_info=0&description=0&rel=0`;
}

function addVideo(video) {
  videos.set(video.key, video);
  $('count').textContent = `${videos.size} of ${MAX_VIDEOS}`;
  $('bar').hidden = false;

  const card = el('article', 'card');
  card.dataset.key = video.key;

  const frame = el('div', 'frame');
  const thumb = el('img');
  thumb.referrerPolicy = 'no-referrer';
  thumb.loading = 'lazy';
  thumb.alt = '';
  thumb.src = video.thumbnail;
  if (video.platform === 'youtube') {
    thumb.onerror = () => {
      thumb.onerror = null;
      thumb.src = `https://i.ytimg.com/vi/${video.video_id}/hqdefault.jpg`;
    };
  }
  const play = el('button', 'play', '▶');
  play.type = 'button';
  play.title = 'Play';
  play.onclick = (e) => {
    e.stopPropagation();
    const player = el('iframe');
    player.src = embedUrl(video);
    player.allow = 'autoplay; encrypted-media; fullscreen; picture-in-picture';
    player.allowFullscreen = true;
    thumb.replaceWith(player);
    play.remove();
  };
  frame.append(
    thumb, play,
    el('span', `platform ${video.platform}`, PLATFORM[video.platform]),
    el('span', 'official', 'Official'),
    el('span', 'check', '✓'),
  );

  const meta = el('div', 'meta');
  meta.append(
    el('div', 'handle', video.handle),
    el('div', 'reach', ''),
    el('div', 'title-text', video.title),
    el('div', 'reason', video.reason),
  );
  card.append(frame, meta);
  card.onclick = () => toggle(video.key);
  $('grid').append(card);
  paintVideo(video);
}

function updateVideo(update) {
  const video = videos.get(update.key);
  if (!video) return;
  for (const field of ['followers', 'views', 'likes', 'duration', 'verified', 'official']) {
    if (update[field] !== undefined) video[field] = update[field];
  }
  paintVideo(video);
  if (clips.has(video.key)) paintClip(video.key);
  if (!scouting) sortByReach();
}

function paintVideo(video) {
  const card = document.querySelector(`.card[data-key="${CSS.escape(video.key)}"]`);
  if (!card) return;
  card.classList.toggle('is-official', Boolean(video.official));
  const line = reach(video);
  const node = card.querySelector('.reach');
  node.textContent = line || 'Checking reach…';
  node.classList.toggle('pending', !line);
}

// Official accounts first, then the most-viewed videos. Uses CSS order so playing embeds are not reloaded.
function sortByReach() {
  const ranked = [...videos.values()].sort(
    (a, b) => Number(b.official) - Number(a.official) || (b.views || 0) - (a.views || 0),
  );
  ranked.forEach((video, index) => {
    const card = document.querySelector(`.card[data-key="${CSS.escape(video.key)}"]`);
    if (card) card.style.order = index;
  });
}

function toggle(key) {
  if (rendering) return;
  if (selected.has(key)) selected.delete(key);
  else if (selected.size < MAX_SELECTED) selected.add(key);
  for (const card of document.querySelectorAll('.card')) {
    card.classList.toggle('selected', selected.has(card.dataset.key));
  }
  updateBar();
}

function updateBar() {
  const n = selected.size;
  const current = styles.find((item) => item.key === style);
  $('bar-text').textContent = scouting
    ? `${n} selected · the agents are still searching`
    : n ? `${n} of ${MAX_SELECTED} selected` : `Select up to ${MAX_SELECTED} videos`;
  $('style-note').textContent = current ? current.description : '';
  for (const button of document.querySelectorAll('.style')) {
    button.classList.toggle('active', button.dataset.key === style);
    button.disabled = rendering;
  }
  $('go').disabled = !n || scouting || rendering;
  $('go').textContent = rendering ? 'Making your ad…' : 'Make my ad';
}

async function startRender() {
  rendering = true;
  renderOrder = [...selected];
  clips.clear();
  updateBar();
  $('studio').hidden = false;
  $('final').hidden = true;
  $('cut-panel').hidden = true;
  $('log-panel').hidden = style !== 'director';
  $('log').replaceChildren();
  $('studio-eyebrow').textContent = `Studio · ${styles.find((item) => item.key === style)?.label || style}`;
  $('studio-title').textContent = 'Making your ad';
  $('clips').replaceChildren();
  for (const key of renderOrder) updateClip({ key, state: 'queued', detail: '' });
  $('studio').scrollIntoView({ behavior: 'smooth' });

  const res = await fetch(`/api/jobs/${jobId}/render`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ keys: renderOrder, style }),
  });
  if (!res.ok) fail((await res.json()).detail || 'Could not start rendering');
}

function updateClip(event) {
  const clip = clips.get(event.key) || {};
  clip.state = event.state;
  clip.detail = event.detail || '';
  for (const field of ['duration', 'filmstrip', 'segment', 'candidate']) {
    if (event[field] !== undefined) clip[field] = event[field];
  }
  clips.set(event.key, clip);
  paintClip(event.key);
  paintCut();
}

function paintClip(key) {
  const clip = clips.get(key);
  const video = videos.get(key) || { handle: key };
  let row = document.querySelector(`#clips [data-key="${CSS.escape(key)}"]`);
  if (!row) {
    row = el('li', 'clip');
    row.dataset.key = key;
    row.style.setProperty('--clip', CLIP_COLORS[renderOrder.indexOf(key) % CLIP_COLORS.length]);
    const head = el('div', 'clip-head');
    head.append(el('span', 'icon'), el('strong', 'who'), el('span', 'reach muted'), el('span', 'state'));
    const strip = el('div', 'strip');
    strip.append(
      el('img'), el('div', 'shade left'), el('div', 'shade right'),
      el('div', 'window'), el('div', 'scan'),
    );
    strip.querySelector('.window').append(el('span', 'window-label'));
    const scale = el('div', 'scale');
    scale.append(el('span', '', '0:00'), el('span', 'used'), el('span', 'end'));
    row.append(head, strip, scale);
    $('clips').append(row);
  }

  const settled = SETTLED.includes(clip.state);
  row.className = `clip ${settled ? clip.state : 'running'}`;
  row.querySelector('.who').textContent = video.handle;
  row.querySelector('.reach').textContent = [PLATFORM[video.platform], reach(video)].filter(Boolean).join(' · ');
  const label = CLIP_STATE[clip.state] || clip.state;
  row.querySelector('.state').textContent = clip.detail ? `${label} — ${clip.detail}` : label;

  const strip = row.querySelector('.strip');
  const img = strip.querySelector('img');
  if (clip.filmstrip && img.getAttribute('src') !== clip.filmstrip) img.src = clip.filmstrip;
  strip.classList.toggle('empty', !clip.filmstrip);

  // The chosen segment, or the one the spotter is currently checking, as a window on the strip.
  const span = clip.segment || clip.candidate;
  const known = Boolean(span && clip.duration);
  strip.classList.toggle('has-window', known);
  strip.classList.toggle('tentative', known && !clip.segment);
  strip.classList.toggle('scanning', !settled && !clip.segment);
  if (known) {
    const left = (span.start / clip.duration) * 100;
    const width = Math.max(1.5, ((span.end - span.start) / clip.duration) * 100);
    strip.style.setProperty('--left', `${left}%`);
    strip.style.setProperty('--width', `${Math.min(width, 100 - left)}%`);
    strip.querySelector('.window-label').textContent = `${clock(span.start)} – ${clock(span.end)}`;
  }
  row.querySelector('.end').textContent = clip.duration ? clock(clip.duration) : '';
  row.querySelector('.used').textContent = clip.segment
    ? `uses ${(clip.segment.end - clip.segment.start).toFixed(1)}s of ${clock(clip.duration)}`
    : '';
}

// The final cut: every chosen segment in order, sized by how long it runs.
function paintCut() {
  const parts = renderOrder
    .map((key) => ({ key, clip: clips.get(key) }))
    .filter(({ clip }) => clip?.segment && clip.state !== 'skipped' && clip.state !== 'error');
  $('cut-panel').hidden = !parts.length;
  if (!parts.length) return;
  let total = 0;
  $('cut').replaceChildren(...parts.map(({ key, clip }) => {
    const seconds = clip.segment.end - clip.segment.start;
    total += seconds;
    const block = el('div', 'block');
    block.style.flexGrow = seconds;
    block.style.setProperty('--clip', CLIP_COLORS[renderOrder.indexOf(key) % CLIP_COLORS.length]);
    block.append(
      el('strong', '', videos.get(key)?.handle || key),
      el('span', '', `${clock(clip.segment.start)}–${clock(clip.segment.end)} · ${seconds.toFixed(1)}s`),
    );
    return block;
  }));
  $('cut-total').textContent = `${parts.length} clip${parts.length === 1 ? '' : 's'} · ${total.toFixed(1)}s of creator footage`;
}

function addLog(text) {
  const line = el('li', text.startsWith('$') ? 'command' : 'say', text);
  $('log').append(line);
  $('log').scrollTop = $('log').scrollHeight;
}

function showFinal(event) {
  rendering = false;
  credits = event.credits;
  updateBar();
  $('studio-title').textContent = `Ad made from ${credits.length} creator clip${credits.length === 1 ? '' : 's'}`;
  $('final').hidden = false;
  $('final-video').src = event.url;
  $('final-video').play().catch(() => {});
  $('download').href = event.url;

  const followers = credits.reduce((sum, c) => sum + (c.followers || 0), 0);
  const views = credits.reduce((sum, c) => sum + (c.views || 0), 0);
  $('final-reach').textContent = [
    followers && `${compact(followers)} combined followers`,
    views && `${compact(views)} views`,
  ].filter(Boolean).join(' · ');

  $('credits').replaceChildren(...credits.map((credit) => {
    const row = el('tr');
    const who = el('td');
    const link = el('a', '', credit.handle);
    link.href = credit.creator_url || credit.url;
    link.target = '_blank';
    link.rel = 'noreferrer';
    who.append(link);
    if (credit.official) who.append(el('span', 'official-tag', 'Official'));
    who.append(el('div', 'muted', [PLATFORM[credit.platform], reach(credit)].filter(Boolean).join(' · ')));
    const used = el('td');
    const videoLink = el('a', '', `${clock(credit.start)} – ${clock(credit.end)}`);
    videoLink.href = credit.url;
    videoLink.target = '_blank';
    videoLink.rel = 'noreferrer';
    used.append(videoLink, el('div', 'muted', credit.caption));
    row.append(who, used);
    return row;
  }));
  $('final').scrollIntoView({ behavior: 'smooth' });
}

$('search-form').onsubmit = (e) => {
  e.preventDefault();
  const query = $('query').value.trim();
  if (query && !scouting && !rendering) startSearch(query);
};
for (const chip of document.querySelectorAll('.chip')) {
  chip.onclick = () => {
    $('query').value = chip.textContent;
    $('query').focus();
  };
}
$('go').onclick = startRender;
$('copy-credits').onclick = async () => {
  const text = credits
    .map((c) => `${c.handle} (${[PLATFORM[c.platform], reach(c)].filter(Boolean).join(', ')}) ${c.url} — used ${clock(c.start)} to ${clock(c.end)}`)
    .join('\n');
  await navigator.clipboard.writeText(text);
  $('copy-credits').textContent = 'Copied';
  setTimeout(() => { $('copy-credits').textContent = 'Copy creator list'; }, 1500);
};

pollAgents();
loadStyles();
