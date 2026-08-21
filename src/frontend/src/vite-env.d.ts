/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base HTTP URL of the ophyd-websocket API, e.g. http://localhost:8001/api/v1 */
  readonly VITE_OPHYD_API_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
