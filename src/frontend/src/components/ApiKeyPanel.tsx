import { useState } from 'react'
import { demoApiKeys, maskApiKey, setApiKey } from '../lib/apiKey'
import type { AuthStatus } from '../lib/useAuthPermissions'

export interface ApiKeyPanelProps {
  apiKey: string | null
  authStatus: AuthStatus
}

const tick = (allowed: boolean) => (allowed ? '✓' : '✗')

/** Paste, save (to localStorage) and clear the API key; shows what it allows. */
export default function ApiKeyPanel({ apiKey, authStatus }: ApiKeyPanelProps) {
  const [draft, setDraft] = useState('')

  return (
    <section>
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">API key</h2>

      <form
        className="mt-2 flex gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          if (!draft.trim()) return
          setApiKey(draft)
          setDraft('')
        }}
      >
        <input
          type="password"
          autoComplete="off"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder={apiKey ? 'replace key' : 'paste Queue Server API key'}
          className="min-w-0 flex-1 rounded border border-slate-200 px-2 py-1 font-mono text-sm
            focus:border-sky-400 focus:outline-none"
        />
        <button
          type="submit"
          className="rounded bg-slate-800 px-3 py-1 text-sm font-medium text-white hover:bg-slate-900"
        >
          Save
        </button>
      </form>

      {demoApiKeys.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2">
          {demoApiKeys.map((demo) => (
            <button
              key={demo.label}
              type="button"
              onClick={() => setApiKey(demo.key)}
              className={`rounded border px-2 py-1 text-xs transition
                ${
                  apiKey === demo.key
                    ? 'border-sky-500 bg-sky-50 text-sky-700'
                    : 'border-slate-200 text-slate-600 hover:bg-slate-50'
                }`}
            >
              {demo.label}
            </button>
          ))}
        </div>
      )}

      <div className="mt-2 flex items-center justify-between text-xs text-slate-500">
        <span>
          {apiKey ? (
            <>
              Saved: <span className="font-mono">{maskApiKey(apiKey)}</span>
            </>
          ) : (
            'No key saved'
          )}
        </span>
        {apiKey && (
          <button
            type="button"
            onClick={() => setApiKey(null)}
            className="rounded px-2 py-0.5 text-rose-600 hover:bg-rose-50"
          >
            Clear
          </button>
        )}
      </div>

      <AuthStatusLine status={authStatus} />
    </section>
  )
}

function AuthStatusLine({ status }: { status: AuthStatus }) {
  switch (status.state) {
    case 'loading':
      return <p className="mt-2 text-xs text-slate-400">Checking permissions…</p>
    case 'unauthorized':
      return (
        <p className="mt-2 rounded bg-amber-50 p-2 text-xs text-amber-800">
          Server requires a valid API key: {status.message}
        </p>
      )
    case 'error':
      return (
        <p className="mt-2 rounded bg-rose-50 p-2 text-xs text-rose-700">
          Could not check permissions: {status.message}
        </p>
      )
    case 'ok': {
      const { permissions } = status
      if (!permissions.authEnabled) {
        return (
          <p className="mt-2 text-xs text-slate-400">
            Auth is off on this server; the key is not needed.
          </p>
        )
      }
      return (
        <p className="mt-2 rounded bg-emerald-50 p-2 text-xs text-emerald-800">
          {permissions.roles.length > 0 && <>Roles: {permissions.roles.join(', ')} · </>}
          read {tick(permissions.read)} · write {tick(permissions.write)} · console{' '}
          {tick(permissions.console)}
        </p>
      )
    }
  }
}
