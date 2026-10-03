const MAX_SELECTED = 6;
const PLATFORM = { youtube: 'YouTube Shorts', tiktok: 'TikTok' };
const CLIP_STATE = {
  downloading: 'Downloading',
  spotting: 'Spotter agent is finding the product',
  rendering: 'Cutting and drawing the highlight',
  done: 'Done',
  skipped: 'Skipped',
  error: 'Failed',
};

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
const videos = new Map();
const selected = new Set();
let credits = [];

async function pollAgents() {
  try {
    const health = await (await fetch('/api/health')).json();
    if (health.ready) {
      $('agents').className = 'agents ready';
      $('agents-text').textContent = 'ZooWork agents ready: Scout, Spotter';
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

function reset() {
  if (source) source.close();
  videos.clear();
  selected.clear();
  credits = [];
  for (const id of ['steps', 'grid', 'clips', 'credits']) $(id).replaceChildren();
  for (const id of ['product-panel', 'summary', 'studio', 'final', 'bar']) $(id).hidden = true;
  $('count').textContent = '0';
  $('results-hint').textContent = 'Videos appear here as the agent finds them.';
  $('scout').hidden = false;
  document.body.classList.add('started');
}

async function startSearch(query) {
  reset();
  scouting = true;
  $('search-button').disabled = true;
  addStep('boot', 'running', 'Starting', 'Handing your product to the scout agent');
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
    case 'scout_done':
      scouting = false;
      addStep('boot', 'done');
      $('search-button').disabled = false;
      $('summary').hidden = false;
      $('summary').textContent = event.summary || `Found ${event.count} videos.`;
      $('results-hint').textContent = `Tick up to ${MAX_SELECTED}, then make your ad.`;
      updateBar();
      break;
    case 'clip':
      updateClip(event);
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
  $('count').textContent = videos.size;
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
    // Only Shorts have the vertical thumbnail.
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
  frame.append(thumb, play, el('span', `platform ${video.platform}`, PLATFORM[video.platform]), el('span', 'check', '✓'));

  const meta = el('div', 'meta');
  meta.append(el('div', 'handle', video.handle), el('div', 'title-text', video.title), el('div', 'reason', video.reason));
  card.append(frame, meta);
  card.onclick = () => toggle(video.key);
  $('grid').append(card);
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
  $('bar-text').textContent = scouting
    ? `${n} selected · the agent is still searching`
    : n ? `${n} of ${MAX_SELECTED} selected` : `Select up to ${MAX_SELECTED} videos`;
  $('go').disabled = !n || scouting || rendering;
  $('go').textContent = rendering ? 'Making your ad…' : 'Make my ad';
}

async function startRender() {
  rendering = true;
  updateBar();
  $('studio').hidden = false;
  $('final').hidden = true;
  $('studio-title').textContent = 'Making your ad';
  $('clips').replaceChildren();
  for (const key of selected) updateClip({ key, state: 'queued', detail: '' });
  $('studio').scrollIntoView({ behavior: 'smooth' });

  const res = await fetch(`/api/jobs/${jobId}/render`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ keys: [...selected] }),
  });
  if (!res.ok) fail((await res.json()).detail || 'Could not start rendering');
}

function updateClip({ key, state, detail }) {
  let row = document.querySelector(`#clips [data-key="${CSS.escape(key)}"]`);
  if (!row) {
    row = el('li');
    row.dataset.key = key;
    row.append(el('span', 'icon'), el('strong', '', videos.get(key)?.handle || key), el('span', 'state'));
    $('clips').append(row);
  }
  row.className = ['done', 'error', 'skipped'].includes(state) ? state : 'running';
  const label = CLIP_STATE[state] || 'Waiting';
  row.querySelector('.state').textContent = detail ? `${label} — ${detail}` : label;
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

  $('credits').replaceChildren(...credits.map((credit) => {
    const row = el('tr');
    const who = el('td');
    const link = el('a', '', credit.handle);
    link.href = credit.creator_url || credit.url;
    link.target = '_blank';
    link.rel = 'noreferrer';
    who.append(link, el('div', 'muted', PLATFORM[credit.platform]));
    const used = el('td');
    const videoLink = el('a', '', `${credit.start}s – ${credit.end}s`);
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
    .map((c) => `${c.handle} (${PLATFORM[c.platform]}) ${c.url} — used ${c.start}s to ${c.end}s`)
    .join('\n');
  await navigator.clipboard.writeText(text);
  $('copy-credits').textContent = 'Copied';
  setTimeout(() => { $('copy-credits').textContent = 'Copy creator list'; }, 1500);
};

pollAgents();
