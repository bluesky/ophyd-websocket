/**
 * Resolves the ophyd-websocket base URL.
 *
 * The browser -- not the docker network -- makes these requests, so the
 * default targets whatever host is serving the page on the ophyd-websocket
 * port. Override with VITE_OPHYD_API_URL when the server lives elsewhere.
 */
export const ophydApiUrl =
  import.meta.env.VITE_OPHYD_API_URL ??
  `http://${window.location.hostname}:8001/api/v1`

/** Same base, as a websocket scheme. finch derives this internally too. */
export const ophydWsUrl = ophydApiUrl.replace(/^http/, 'ws')
