import { useCallback, useState } from 'react'
import { ophydWsUrl } from './config'

/** Field values for one stream panel, keyed by field name. */
export type StreamSettings = Record<string, string>

const STORAGE_PREFIX = 'ophyd-ws-harness:stream:'

function readStored(key: string): StreamSettings | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_PREFIX + key)
    return raw ? (JSON.parse(raw) as StreamSettings) : null
  } catch {
    return null
  }
}

function writeStored(key: string, value: StreamSettings | null) {
  try {
    if (value === null) window.localStorage.removeItem(STORAGE_PREFIX + key)
    else window.localStorage.setItem(STORAGE_PREFIX + key, JSON.stringify(value))
  } catch {
    /* Private window or blocked storage: settings just won't persist. */
  }
}

/**
 * Applied settings for a stream panel, remembered per browser so a beamline
 * user doesn't retype their detector's PVs on every reload.
 */
export function useStreamSettings(key: string, defaults: StreamSettings) {
  const [settings, setSettings] = useState<StreamSettings>(() => ({
    ...defaults,
    ...readStored(key),
  }))

  // Bumped on every apply/reset so the canvas remounts (and so reconnects)
  // even when the values didn't change.
  const [generation, setGeneration] = useState(0)

  const apply = useCallback(
    (next: StreamSettings) => {
      setSettings(next)
      setGeneration((g) => g + 1)
      writeStored(key, next)
    },
    [key],
  )

  const reset = useCallback(() => {
    setSettings(defaults)
    setGeneration((g) => g + 1)
    writeStored(key, null)
    // `defaults` is a module-level constant in every caller.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  return { settings, generation, apply, reset }
}

/**
 * `ws://…/<path>?k=v` for the non-empty settings. The file-image sockets read
 * the query string because finch's canvases always send their own fixed first
 * message.
 */
export function streamWsUrl(path: string, params: StreamSettings): string {
  const query = new URLSearchParams()
  for (const [name, value] of Object.entries(params)) {
    if (value.trim()) query.set(name, value.trim())
  }
  const qs = query.toString()
  return `${ophydWsUrl}/${path}${qs ? `?${qs}` : ''}`
}

const FILE_PATH_SUFFIX = 'FullFileName_RBV'

/**
 * Status PVs for an areaDetector file plugin, from either an exact
 * `FullFileName_RBV` PV or a prefix + plugin name.
 */
export function fileWriterPvs(
  { prefix, plugin, pv }: StreamSettings,
  extraSuffixes: string[] = [],
): string[] {
  const exact = pv?.trim()
  let base: string
  if (exact) {
    if (!exact.endsWith(FILE_PATH_SUFFIX)) return [exact]
    base = exact.slice(0, -FILE_PATH_SUFFIX.length)
  } else {
    base = `${stripColon(prefix)}:${stripColon(plugin)}:`
  }
  return [FILE_PATH_SUFFIX, 'FileNumber_RBV', 'NumCaptured_RBV', ...extraSuffixes].map(
    (suffix) => base + suffix,
  )
}

/** Detector prefix without surrounding colons, e.g. `13PIL1:` → `13PIL1`. */
export function stripColon(value: string | undefined): string {
  return (value ?? '').trim().replace(/^:+|:+$/g, '')
}
