/* OAuth redirect landing page.

   The popup opened on the provider's real authorize URL (see app.js:
   authorizeUrlFor). Every provider used here (Microsoft, Atlassian, Google)
   is registered with this page as its redirect_uri, so once the customer
   approves access there, the provider sends the browser back here with
   `code` and `state` in the query string — never through this file's own
   UI, which only ever runs if something went wrong before the redirect (no
   client_id registered yet, or the user landed here directly).

   There is no `source` param on that redirect — the redirect_uri must
   match the provider's registered value exactly, so nothing can be
   appended to it per-source. app.js smuggles the source id inside `state`
   instead (as "sourceId.uuid"), since every provider echoes `state` back
   verbatim; split it back out here.

   Delivery back to the opener is via localStorage + the `storage` event,
   not window.opener.postMessage. Chromium-family browsers (Chrome, Opera,
   new Edge) can null out window.opener after this popup navigates
   cross-origin to the provider's own auth pages and back — a security
   hardening default, not a bug in either window — which silently breaks
   postMessage with no error and no server-side trace. localStorage has no
   such dependency: it's plain same-origin storage, and the `storage`
   event fires in every OTHER window sharing that origin whenever it
   changes, which is exactly the "popup tells opener" relationship needed
   here, without relying on the two windows still holding references to
   each other. */

const q = new URLSearchParams(location.search);
const returnedState = q.get('state') || '';
const sourceId = returnedState.split('.')[0] || '';
const code = q.get('code');
const errorParam = q.get('error');

function reply(ok, payload) {
  try {
    localStorage.setItem(
      'ivvy_signin_result',
      JSON.stringify({ type: 'ivvy-signin', source: sourceId, state: returnedState, ok, code: payload || null, at: Date.now() })
    );
  } catch (e) { /* private mode: localStorage may be unavailable */ }
  window.close();
}

if (code) {
  // Real success: the provider already showed its own consent screen.
  reply(true, code);
} else if (errorParam) {
  reply(false, null);
} else {
  // Landed here with neither — most likely no client_id is registered yet,
  // so the provider redirected straight back with nothing to hand over.
  document.getElementById('sheet').innerHTML = `
    <p class="ask">No authorization code came back. This provider needs a
    client ID registered (see js/config.js) before sign-in can complete.</p>
    <div class="acts"><button class="btn btn-quiet" id="close" type="button">Close</button></div>`;
  document.getElementById('close').addEventListener('click', () => reply(false, null));
}
