/* ==========================================================================
   The only place this app talks to AWS.

   Three calls, in the order they happen:

     connect(id, grant)  store the OAuth grant for this tenant, in Secrets
                         Manager under ivvy-bot/{tenant_id}/
     ingest(id)          start the crawl that reads that source into the
                         tenant's knowledge base
     status(id)          how far the crawl has got

   Ingestion is deliberately its own step rather than something `connect` does
   implicitly. Reading a large Confluence space takes minutes, so the browser
   starts it and then polls — it must never sit waiting on a request that long.

   USE_MOCK simulates all three locally. Everything below the seam is real
   already: the polling, the states, and the shapes the UI renders. Only the
   transport changes when the backend lands.
   ========================================================================== */

const CONFIG = {
  baseUrl: window.IVVY_API_BASE || '/api',
  useMock: window.IVVY_USE_MOCK !== false,
};

// Routes that have a real Lambda behind them today. Everything else still
// runs through the mock even once IVVY_USE_MOCK is false — ingest/status
// for these sources isn't built yet, so pretending otherwise would just
// trade one broken behaviour for another.
const LIVE_ROUTES = [
  /^\/sources\/slackhistory\/connect$/,
  /^\/sources\/atlassian\/connect$/,
];

const token = () => sessionStorage.getItem('ivvy_id_token') || '';

async function request(path, { method = 'GET', body } = {}) {
  const isLiveRoute = LIVE_ROUTES.some((re) => re.test(path));
  if (CONFIG.useMock && !isLiveRoute) return mock(path, method, body);

  const res = await fetch(CONFIG.baseUrl + path, {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...(token() ? { Authorization: token() } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const payload = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(payload.message || `Request failed (${res.status})`);
  return payload;
}

export const backend = {
  listSources: () => request('/sources'),
  connect: (id, grant) => request(`/sources/${id}/connect`, { method: 'POST', body: grant }),
  disconnect: (id) => request(`/sources/${id}/disconnect`, { method: 'POST' }),
  ingest: (id) => request(`/sources/${id}/ingest`, { method: 'POST' }),
  status: (id) => request(`/sources/${id}/status`),
  saveModelKey: (modelId, apiKey) => request(`/models/${modelId}/key`, { method: 'POST', body: { apiKey } }),
};

/* Poll until the crawl finishes, reporting progress as it goes. Backs off so a
   slow crawl doesn't hammer the API, and gives up rather than polling forever
   if something upstream has stalled. */
export async function followIngest(id, onProgress, { timeoutMs = 120000 } = {}) {
  const started = Date.now();
  let wait = 400;

  for (;;) {
    const s = await backend.status(id);
    onProgress(s);

    if (s.state === 'indexed' || s.state === 'failed') return s;
    if (Date.now() - started > timeoutMs) {
      return { state: 'failed', error: 'This took longer than expected. It may still finish — check back shortly.' };
    }

    await new Promise((r) => setTimeout(r, wait));
    wait = Math.min(wait * 1.3, 2500);
  }
}

/* --- mock ---------------------------------------------------------------- */

// Plausible corpus sizes, so the progress readout looks like real work rather
// than a spinner. Kept deterministic per source: a number that changes on every
// reload undermines the thing it is meant to demonstrate.
const CORPUS = {
  'microsoft365:workvivo': { posts: 356 },
  'microsoft365:sharepoint': { documents: 412 },
  'atlassian:jira': { tickets: 1204 },
  'atlassian:confluence': { pages: 148 },
  'gsuite:sites': { pages: 96 },
  'gsuite:docs': { documents: 231 },
  'gsuite:sheets': { sheets: 58 },
};

const runs = new Map();

async function mock(path, method, body) {
  await new Promise((r) => setTimeout(r, 90 + Math.random() * 140));

  let m = path.match(/^\/sources\/([^/]+)\/connect$/);
  if (m && method === 'POST') return { connected: true, source: m[1] };

  m = path.match(/^\/models\/([^/]+)\/key$/);
  if (m && method === 'POST') return { saved: true, model: m[1] };

  m = path.match(/^\/sources\/([^/]+)\/disconnect$/);
  if (m && method === 'POST') { runs.delete(m[1]); return { disconnected: true, source: m[1] }; }

  m = path.match(/^\/sources\/([^/]+)\/ingest$/);
  if (m && method === 'POST') {
    const total = Object.values(CORPUS[m[1]] || { items: 100 })
      .reduce((a, b) => a + b, 0);
    runs.set(m[1], { startedAt: Date.now(), total });
    return { state: 'reading', total };
  }

  m = path.match(/^\/sources\/([^/]+)\/status$/);
  if (m) {
    const run = runs.get(m[1]);
    if (!run) return { state: 'idle' };

    // Roughly four seconds of crawling, so the states are legible.
    const elapsed = Date.now() - run.startedAt;
    const pct = Math.min(elapsed / 4000, 1);

    if (pct >= 1) {
      return { state: 'indexed', total: run.total, read: run.total, counts: CORPUS[m[1]] };
    }
    return {
      state: 'reading',
      total: run.total,
      read: Math.floor(run.total * pct),
    };
  }

  throw new Error(`No mock for ${method} ${path}`);
}
