/* Deploy-time settings. Plain script so it runs before the modules read it.
   Nothing here is secret — OAuth client IDs are public by design. Leave
   IVVY_USE_MOCK true and the ingest/crawl side runs entirely offline; the
   "Connect" buttons still send the browser to each provider's real
   authorize URL, so registering a client ID per provider is what's needed
   to take a source live, not swapping out a mock transport. */
window.IVVY_USE_MOCK = true;
window.IVVY_API_BASE = '/api';

// Fill these in from each provider's developer console. Left blank, the
// authorize link still opens with a "{client_id}" placeholder so the tile
// is honest about what's missing rather than pretending to work.
window.IVVY_CLIENT_IDS = {
  microsoft365: '',
  atlassian: 'kBPzIU2DKnPKsSA6gPLSfDg3dVR9NITf',
  gsuite: '',
};
