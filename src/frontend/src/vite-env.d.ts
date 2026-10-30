/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base HTTP URL of the ophyd-websocket API, e.g. http://localhost:8001/api/v1 */
  readonly VITE_OPHYD_API_URL?: string
  /** DEMO operator API key, set by docker-compose-auth.yml from qserver/demo-api-keys.env */
  readonly VITE_DEMO_OPERATOR_API_KEY?: string
  /** DEMO observer (read-only) API key, set the same way */
  readonly VITE_DEMO_OBSERVER_API_KEY?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
