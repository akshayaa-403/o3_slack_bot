/* ==========================================================================
   Connect a source, then watch it get read into the knowledge base.

   Every content tile (Microsoft 365, Atlassian, G Suite) works the same way:
   one sign-in unlocks a set of products, and each product is its own
   on/off switch with its own ingest — no second sign-in to turn on a
   sibling product once the account is connected.

     click        → real provider authorize URL opens in a popup
     approve      → provider redirects back with a code, we store the grant
                    (backend.connect)
     per-product  → each product is enabled independently                (ingest)
     then         → poll and show progress                 (followIngest)
     finally      → the line goes live and reports what it found

   The "Answering model" tile is different on purpose: it has no sign-in, no
   crawl, and no feeds. Toggling Gemini or Claude on just flips whether that
   model is allowed to answer, gated by an API key entered once.

   Connecting and ingesting are separate on purpose: a large Confluence space
   takes minutes to read, so the browser kicks it off and polls rather than
   holding a request open.
   ========================================================================== */

import { SOURCES, sourceById, REDIRECT_URI, MODEL_SOURCE, MORE_CONNECTORS } from './sources.js';
import { backend, followIngest } from './backend.js';

/* --- theme --------------------------------------------------------------- */

const THEME_KEY = 'ivvy_theme';
const $theme = document.getElementById('theme');
const $themeLabel = document.getElementById('theme-label');

const systemDark = () => matchMedia('(prefers-color-scheme: dark)').matches;
const isDark = () => (document.documentElement.dataset.theme || (systemDark() ? 'dark' : 'light')) === 'dark';

function paintTheme() {
  const dark = isDark();
  $theme.setAttribute('aria-pressed', String(dark));
  $themeLabel.textContent = dark ? 'Dark' : 'Light';
  $theme.setAttribute('aria-label', `Theme: ${dark ? 'dark' : 'light'}. Switch to ${dark ? 'light' : 'dark'}.`);
}

$theme.addEventListener('click', () => {
  const next = isDark() ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem(THEME_KEY, next); } catch { /* private mode */ }
  paintTheme();
});

matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  if (!document.documentElement.dataset.theme) paintTheme();
});

paintTheme();

/* --- helpers ------------------------------------------------------------- */

const $lines = document.getElementById('lines');
const $inactiveArea = document.getElementById('inactive-area');
const $moreArea = document.getElementById('more-area');
const $summary = document.getElementById('summary');
const $finish = document.getElementById('finish');
const $footNote = document.getElementById('foot-note');
const $toasts = document.getElementById('toasts');

const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const tick = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"'
  + ' stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>';

const nf = new Intl.NumberFormat('en-GB');

// Backend/network errors carry developer-facing text (stack-trace-ish
// fetch messages, Lambda error bodies). A non-technical admin should never
// see that — just a plain, reassuring line with something to try next.
function friendlyError() {
  return "Something went wrong on our end. Please try again — if it keeps happening, contact support.";
}

function toast(message) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.innerHTML = `${tick}<span>${esc(message)}</span>`;
  $toasts.append(el);
  setTimeout(() => el.remove(), 3800);
}

/* --- state -----------------------------------------------------------------
   Every content source's state is one layer deep: `authorized` records
   whether the sign-in has happened, and `products` holds per-product state
   entries ({state, read, total, counts, error}).

   The model tile is simpler still: each product is just {enabled, hasKey}. */

function freshEntry(s) {
  return {
    authorized: false,
    products: new Map(s.products.map((p) => [p.id, { state: 'off' }])),
  };
}

const state = new Map(
  [...SOURCES, ...MORE_CONNECTORS].map((s) => [s.id, freshEntry(s)])
);

const modelState = new Map(MODEL_SOURCE.products.map((p) => [p.id, { enabled: false, hasKey: false }]));
const modelKeys = new Map(); // productId -> API key, kept in memory only

function productState(sourceId, productId) {
  return state.get(sourceId).products.get(productId);
}

const isLive = (id) => {
  const st = state.get(id);
  return [...st.products.values()].some((p) => p.state === 'indexed');
};

const liveCount = () => SOURCES.filter((s) => isLive(s.id)).length;

/* --- rendering ------------------------------------------------------------ */

function pill(st) {
  if (st.state === 'indexed') return '<span class="pill pill-live">Live</span>';
  if (st.state === 'reading') return '<span class="pill pill-reading">Reading</span>';
  if (st.state === 'failed') return '<span class="pill pill-failed">Failed</span>';
  return '<span class="pill pill-off">Not connected</span>';
}

function bodyFor(st, feeds) {
  if (st.state === 'reading') {
    const pct = st.total ? Math.round((st.read / st.total) * 100) : 0;
    return `
      <div class="reading">
        <div class="reading-row">
          <span>Reading into your knowledge base</span>
          <span class="reading-count">${nf.format(st.read || 0)} / ${nf.format(st.total || 0)}</span>
        </div>
        <div class="track" role="progressbar" aria-valuenow="${pct}"
             aria-valuemin="0" aria-valuemax="100">
          <div class="track-fill" style="width:${pct}%"></div>
        </div>
      </div>`;
  }

  if (st.state === 'indexed' && st.counts) {
    return `<div class="counts">${Object.entries(st.counts).map(([unit, n]) => `
      <span class="count"><b>${nf.format(n)}</b> ${esc(unit)}</span>`).join('')}</div>`;
  }

  if (st.state === 'failed') {
    return `<p class="line-feed" style="color:var(--stop)">${esc(st.error || 'Something went wrong.')}</p>`;
  }

  return `<div class="line-feeds">${feeds.map((f) => `
    <span class="line-feed">${tick}<span>${esc(f)}</span></span>`).join('')}</div>`;
}

function actionFor(id, st, { label = 'Enable' } = {}) {
  if (st.state === 'reading') return '<button class="btn" type="button" disabled>Reading…</button>';
  if (st.state === 'indexed') {
    return `
      <div class="action-group">
        <button class="btn btn-quiet" type="button" data-refresh="${esc(id)}">Read again</button>
        <button class="btn btn-quiet btn-danger" type="button" data-disconnect="${esc(id)}">Disconnect</button>
      </div>`;
  }
  if (st.state === 'failed') {
    return `<button class="btn" type="button" data-retry="${esc(id)}">Try again</button>`;
  }
  return `<button class="btn btn-solid" type="button" data-connect="${esc(id)}">${esc(label)}</button>`;
}

/* --- a content tile: sign in once, then a switch per product ------------
   Before sign-in, this is a normal full card. Once signed in, the card
   simplifies to a compact tree root (logo, name, status only — the blurb
   and feed list it needed to make its case are no longer useful once the
   decision is made) and each product renders as its own standalone child
   box underneath, connected back to the root by a branch line. */

function productNodeHtml(source, product) {
  const st = productState(source.id, product.id);
  return `
    <div class="branch" aria-hidden="true"></div>
    <article class="node" style="--line:${source.line}" data-product-line="${esc(product.id)}">
      <div class="product-head">
        <span class="product-mark" aria-hidden="true">${product.logo}</span>
        <span class="product-name">${esc(product.name)}</span>
        ${pill(st)}
      </div>
      ${bodyFor(st, product.feeds)}
      <div class="line-foot product-foot">
        ${actionFor(`${source.id}:${product.id}`, st, { label: 'Enable' })}
      </div>
    </article>`;
}

function contentRootHtml(s) {
  const st = state.get(s.id);

  if (!st.authorized) {
    return `
      <article class="line" style="--line:${s.line}" data-line="${esc(s.id)}">
        <div class="line-top">
          <span class="line-mark" aria-hidden="true">${s.logo}</span>
          <span class="line-kind"><span class="eyebrow">${esc(s.kind)}</span></span>
          <span class="pill pill-off">${s.comingSoon ? 'Coming soon' : 'Not connected'}</span>
        </div>

        <h2 class="line-name">${esc(s.name)}</h2>
        <p class="line-blurb">${esc(s.blurb)}</p>

        <div class="line-feeds">${s.products.flatMap((p) => p.feeds).map((f) => `
          <span class="line-feed">${tick}<span>${esc(f)}</span></span>`).join('')}</div>
        <div class="line-foot">
          ${s.comingSoon
            ? '<button class="btn" type="button" disabled>Coming soon</button>'
            : `<button class="btn btn-solid" type="button" data-connect="${esc(s.id)}">Sign in to ${esc(s.name)}</button>`}
        </div>
      </article>`;
  }

  const anyLive = isLive(s.id);
  return `
    <article class="line line-root${anyLive ? ' line-live' : ''}" style="--line:${s.line}" data-line="${esc(s.id)}">
      <div class="line-top">
        <span class="line-mark" aria-hidden="true">${s.logo}</span>
        <span class="line-kind"><span class="eyebrow">${esc(s.kind)}</span></span>
        <span class="pill pill-live">Signed in</span>
      </div>
      <h2 class="line-name">${esc(s.name)}</h2>
      <button class="btn btn-quiet btn-danger tree-disconnect" type="button" data-disconnect-account="${esc(s.id)}">
        Sign out of ${esc(s.name)}
      </button>
    </article>`;
}

// A signed-in source renders as a root node plus a row of child nodes below
// it, wrapped so the branch connector can be drawn in CSS between them.
function contentTreeHtml(s) {
  const st = state.get(s.id);
  if (!st.authorized) return contentRootHtml(s);

  return `
    <div class="tree" data-tree="${esc(s.id)}">
      ${contentRootHtml(s)}
      <div class="children">${s.products.map((p) => productNodeHtml(s, p)).join('')}</div>
    </div>`;
}

/* --- the Answering model tiles --------------------------------------------
   Row 1 has one plain "Answering model" tile, same as the other three.
   Row 2 has Claude and Gemini as their own tiles, together taking exactly
   the width of that one tile above them (see .model-node in app.css) — no
   sign-in, no branching, just a name, an API-key field, and a single
   Activate/Deactivate button. */

// Its own tile, same row as the other four: logo + name at the top, the
// API-key field centred below that, and the single Activate/Deactivate
// button pinned to the bottom.
function modelNodeHtml(product) {
  const st = modelState.get(product.id);
  const action = st.enabled
    ? `<button class="btn btn-quiet btn-danger" type="button" data-model-toggle="${esc(product.id)}">Deactivate</button>`
    : `<button class="btn btn-solid" type="button" data-model-toggle="${esc(product.id)}">Activate</button>`;

  return `
    <article class="line model-node${st.enabled ? ' model-node-live' : ''}" data-line="${esc(product.id)}">
      <div class="model-node-head">
        <span class="model-node-mark" aria-hidden="true">${product.logo}</span>
        <span class="model-node-name">${esc(product.name)}</span>
      </div>
      <input
        class="key-input"
        type="password"
        placeholder="Enter API Key"
        value="${st.hasKey ? '••••••••' : ''}"
        data-model-key="${esc(product.id)}"
        autocomplete="off"
        spellcheck="false"
      />
      <div class="model-node-action">${action}</div>
    </article>`;
}

// The Answering-model tile itself — same look as the other row-1 tiles.
function modelTileHtml() {
  return `
    <article class="line" style="--line:${MODEL_SOURCE.line}" data-line="${esc(MODEL_SOURCE.id)}">
      <div class="line-top">
        <span class="line-kind"><span class="eyebrow">${esc(MODEL_SOURCE.kind)}</span></span>
      </div>
      <h2 class="line-name">${esc(MODEL_SOURCE.name)}</h2>
      <p class="line-blurb">${esc(MODEL_SOURCE.blurb)}</p>
    </article>`;
}

// The Answering-model tile and the Claude/Gemini row below it are ONE grid
// item (.model-column), spanning both row tracks in the shared 4-column
// grid. This is deliberate: a grid track's height is set by the TALLEST
// item sharing it, so if the tile and the Claude/Gemini row were two
// separate items in that same grid, row 1's height would still be forced
// by Atlassian/MS365/G Suite regardless of any margin put on either of
// them, leaving unreachable dead space between them. Wrapping both in one
// item that spans rows 1-3 removes them from that height calculation
// entirely — the wrapper's own flex layout controls their spacing instead.
function modelTilesHtml() {
  return `
    <div class="model-column">
      ${modelTileHtml()}
      <div class="model-node-row">${MODEL_SOURCE.products.map(modelNodeHtml).join('')}</div>
    </div>`;
}

function renderSummary() {
  const live = SOURCES.filter((s) => isLive(s.id));
  if (!live.length) { $summary.innerHTML = ''; return; }

  const totals = {};
  for (const s of live) {
    const st = state.get(s.id);
    const counts = [...st.products.values()].flatMap((p) => Object.entries(p.counts || {}));
    for (const [unit, n] of counts) totals[unit] = (totals[unit] || 0) + n;
  }

  $summary.innerHTML = `
    <div class="summary-card">
      <span class="eyebrow">Your knowledge base</span>
      <p class="summary-head">I have read ${live.length} of ${SOURCES.length} sources.</p>
      <div class="counts">${Object.entries(totals).map(([unit, n]) => `
        <span class="count"><b>${nf.format(n)}</b> ${esc(unit)}</span>`).join('')}</div>
    </div>`;
}

let showMore = false;

function render() {
  // All four tiles always sit together in one row, signed in or not —
  // grouping active/inactive separately used to split them across rows.
  $lines.innerHTML = SOURCES.map(contentTreeHtml).join('') + modelTilesHtml();
  $inactiveArea.innerHTML = '';

  $moreArea.innerHTML = `
    <div class="more-toggle-row">
      <button class="btn btn-quiet" type="button" data-toggle-more>
        ${showMore ? 'Hide' : 'Show'} more connectors (${MORE_CONNECTORS.length})
      </button>
    </div>
    ${showMore ? `<div class="lines lines-more">${MORE_CONNECTORS.map(contentTreeHtml).join('')}</div>` : ''}`;

  renderSummary();

  const n = liveCount();
  const modelOn = [...modelState.values()].some((s) => s.enabled);
  $finish.disabled = n === 0 || !modelOn;
  if (n === 0) {
    $footNote.textContent = 'Connect at least one source and I will have something to answer from.';
  } else if (!modelOn) {
    $footNote.textContent = 'Turn on a model in Answering model before I can answer.';
  } else {
    $footNote.textContent = `${n} of ${SOURCES.length} sources live. You can add the rest later.`;
  }
}

/* --- the popup: real provider sign-in ------------------------------------
   The provider's own authorize page runs in this popup. It ends by
   redirecting to popup.html?code=...&state=..., which reads the `code` off
   the URL and posts it back — see popup.js.

   Note there is no `source` param on that redirect: the redirect_uri has to
   match what's registered with the provider byte-for-byte, so nothing can
   be appended to it per-source. Instead, the source id is smuggled inside
   `state` itself (every OAuth provider echoes `state` back verbatim) —
   popup.js splits it back out on the other side. */

function authorizeUrlFor(source) {
  const clientId = (window.IVVY_CLIENT_IDS && window.IVVY_CLIENT_IDS[source.id]) || '{client_id}';
  const st = `${source.id}.${crypto.randomUUID()}`;
  sessionStorage.setItem(`ivvy_state_${source.id}`, st);
  return source.authorizeUrl(clientId, st);
}

function signIn(source) {
  const url = authorizeUrlFor(source);
  if (!url) {
    toast(`${source.name} isn't built yet — coming soon.`);
    return Promise.resolve(null);
  }

  return new Promise((resolve) => {
    const w = 520;
    const h = 680;
    const left = window.screenX + (window.outerWidth - w) / 2;
    const top = window.screenY + (window.outerHeight - h) / 2;

    const popup = window.open(url, `ivvy_${source.id}`,
      `width=${w},height=${h},left=${Math.round(left)},top=${Math.round(top)}`);

    if (!popup) {
      toast('Your browser blocked the sign-in window. Allow popups and try again.');
      resolve(null);
      return;
    }

    let settled = false;
    const done = (result) => {
      if (settled) return;
      settled = true;
      clearInterval(watch);
      window.removeEventListener('storage', onStorage);
      resolve(result);
    };

    // The popup writes its result to localStorage under 'ivvy_signin_result'
    // rather than postMessage-ing window.opener directly — some browsers
    // sever window.opener once the popup navigates cross-origin to the
    // provider's own pages and back (see popup.js for the full reasoning),
    // which breaks postMessage silently. localStorage has no such
    // dependency: any window sharing this origin can read it.
    function checkResult() {
      let raw;
      try { raw = localStorage.getItem('ivvy_signin_result'); } catch (e) { return false; }
      if (!raw) return false;

      let d;
      try { d = JSON.parse(raw); } catch (e) { return false; }
      if (!d || d.type !== 'ivvy-signin' || d.source !== source.id) return false;

      try { localStorage.removeItem('ivvy_signin_result'); } catch (e) { /* ignore */ }

      const expected = sessionStorage.getItem(`ivvy_state_${source.id}`);
      if (!expected || d.state !== expected) {
        toast('That sign-in did not come from here. Try again.');
        done(null);
        return true;
      }
      sessionStorage.removeItem(`ivvy_state_${source.id}`);
      done(d.ok ? { code: d.code, redirect_uri: REDIRECT_URI } : null);
      return true;
    }

    function onStorage(event) {
      if (event.key === 'ivvy_signin_result') checkResult();
    }

    // Both signals are needed: `storage` fires promptly while the popup is
    // still open, but if it closes fast enough after writing the result,
    // the poll below is what catches it before falling back to "cancelled".
    const watch = setInterval(() => {
      if (checkResult()) return;
      if (popup.closed) done(null);
    }, 400);

    window.addEventListener('storage', onStorage);
  });
}

/* --- connect + ingest: a single (id, state-entry, feeds) product --- */

async function runIngest(id, st, label) {
  st.state = 'reading';
  st.read = 0;
  st.total = 0;
  render();

  try {
    const started = await backend.ingest(id);
    st.total = started.total || 0;
    render();

    const final = await followIngest(id, (p) => {
      st.read = p.read || 0;
      st.total = p.total || st.total;
      render();
    });

    if (final.state === 'indexed') {
      st.state = 'indexed';
      st.counts = final.counts;
      render();
      toast(`${label} is live.`);
    } else {
      st.state = 'failed';
      st.error = final.error || 'The crawl did not finish.';
      render();
    }
  } catch (err) {
    st.state = 'failed';
    st.error = friendlyError();
    render();
  }
}

async function connectSource(source) {
  const st = state.get(source.id);
  const grant = await signIn(source);
  if (!grant) return;

  try {
    await backend.connect(source.id, grant);
  } catch (err) {
    toast(friendlyError());
    return;
  }

  st.authorized = true;
  render();
  toast(`Signed in to ${source.name}. Choose what to read below.`);
}

async function enableProduct(sourceId, productId) {
  const source = sourceById(sourceId);
  const product = source.products.find((p) => p.id === productId);
  const st = productState(sourceId, productId);
  await runIngest(`${sourceId}:${productId}`, st, product.name);
}

/* --- disconnect: undo a connect, dropping any indexed knowledge --------- */

async function disconnect(id) {
  const [sourceId, productId] = id.split(':');
  const source = sourceById(sourceId);
  const label = productId ? source.products.find((p) => p.id === productId).name : source.name;

  try {
    await backend.disconnect(id);
  } catch (err) {
    toast(friendlyError());
    return;
  }

  if (productId) {
    const st = productState(sourceId, productId);
    st.state = 'off';
    delete st.counts;
    delete st.read;
    delete st.total;
  } else {
    // Disconnecting the account drops the sign-in and every product with it.
    state.set(sourceId, freshEntry(source));
  }

  render();
  toast(`${label} disconnected.`);
}

/* --- Answering model: plain toggle, gated by an API key on first use ---- */

async function toggleModel(productId) {
  const product = MODEL_SOURCE.products.find((p) => p.id === productId);
  const st = modelState.get(productId);

  if (st.enabled) {
    st.enabled = false;
    render();
    toast(`${product.name} deactivated.`);
    return;
  }

  if (!st.hasKey) {
    toast(`Enter your ${product.keyLabel} above, then activate ${product.name}.`);
    return;
  }

  st.enabled = true;
  render();
  toast(`${product.name} activated.`);
}

// Saved as soon as the field loses focus (or Enter is pressed) rather than
// gating it behind the toggle click — the key has to exist before "Turn on"
// can do anything, so asking for it inline is one fewer step, not a prompt
// popup interrupting the click.
async function saveModelKeyFromInput(input) {
  const productId = input.dataset.modelKey;
  const product = MODEL_SOURCE.products.find((p) => p.id === productId);
  const st = modelState.get(productId);
  const key = input.value.trim();

  if (!key || key === '••••••••') return;

  try {
    await backend.saveModelKey(productId, key);
  } catch (err) {
    toast(friendlyError());
    return;
  }

  modelKeys.set(productId, key);
  st.hasKey = true;
  render();
  toast(`${product.keyLabel} saved.`);
}

/* --- events -------------------------------------------------------------- */

function onTilesClick(e) {
  const toggleMore = e.target.closest('[data-toggle-more]');
  if (toggleMore) { showMore = !showMore; render(); return; }

  const modelToggle = e.target.closest('[data-model-toggle]');
  if (modelToggle) { toggleModel(modelToggle.dataset.modelToggle); return; }

  const connect = e.target.closest('[data-connect]');
  if (connect) {
    const id = connect.dataset.connect;
    const [sourceId, productId] = id.split(':');
    const source = sourceById(sourceId);
    if (productId) enableProduct(sourceId, productId);
    else connectSource(source);
    return;
  }

  const retry = e.target.closest('[data-retry]');
  if (retry) {
    const [sourceId, productId] = retry.dataset.retry.split(':');
    if (productId) enableProduct(sourceId, productId);
    return;
  }

  const refresh = e.target.closest('[data-refresh]');
  if (refresh) { reread(refresh.dataset.refresh); return; }

  // Signing out of the whole account drops every product under it — worth
  // a confirm, unlike disconnecting one product, which just pauses that one.
  const dropAccount = e.target.closest('[data-disconnect-account]');
  if (dropAccount) {
    const id = dropAccount.dataset.disconnectAccount;
    const source = sourceById(id);
    const ok = window.confirm(
      `Sign out of ${source.name}? This turns off all of its connected sources `
      + `(${source.products.map((p) => p.name).join(', ')}) — you can sign back in any time.`
    );
    if (ok) disconnect(id);
    return;
  }

  const drop = e.target.closest('[data-disconnect]');
  if (drop) disconnect(drop.dataset.disconnect);
}

$lines.addEventListener('click', onTilesClick);
$inactiveArea.addEventListener('click', onTilesClick);
$moreArea.addEventListener('click', onTilesClick);

$lines.addEventListener('focusout', (e) => {
  const input = e.target.closest('[data-model-key]');
  if (input) saveModelKeyFromInput(input);
});
$lines.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return;
  const input = e.target.closest('[data-model-key]');
  if (input) input.blur();
});

async function reread(id) {
  const [sourceId, productId] = id.split(':');
  const source = sourceById(sourceId);
  const label = source.products.find((p) => p.id === productId).name;
  const st = productState(sourceId, productId);

  st.state = 'reading';
  st.read = 0;
  render();
  try {
    const started = await backend.ingest(id);
    st.total = started.total || 0;
    const final = await followIngest(id, (p) => {
      st.read = p.read || 0;
      st.total = p.total || st.total;
      render();
    });
    st.state = final.state === 'indexed' ? 'indexed' : 'failed';
    st.counts = final.counts || st.counts;
    st.error = final.error;
    render();
    if (st.state === 'indexed') toast(`${label} re-read.`);
  } catch (err) {
    st.state = 'failed';
    st.error = friendlyError();
    render();
  }
}

$finish.addEventListener('click', () => {
  $finish.disabled = true;
  $finish.textContent = 'Ready';
  toast('Your bot is answering from these sources.');
});

render();
