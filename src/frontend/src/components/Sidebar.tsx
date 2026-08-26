import { useState } from 'react'
import { ophydApiUrl } from '../lib/config'

export interface SidebarProps {
  registryDevices: string[]
  registryError: string | null
  registryLoading: boolean
  onRefreshRegistry: () => void
  subscribedDevices: string[]
  onToggleDevice: (name: string) => void
  onSubscribeAllDevices: () => void
  subscribedPvs: string[]
  onAddPv: (pv: string) => void
}

/** Registry browser plus the arbitrary-PV subscription form. */
export default function Sidebar({
  registryDevices,
  registryError,
  registryLoading,
  onRefreshRegistry,
  subscribedDevices,
  onToggleDevice,
  onSubscribeAllDevices,
  subscribedPvs,
  onAddPv,
}: SidebarProps) {
  const [pvDraft, setPvDraft] = useState('')

  return (
    <aside className="flex w-80 shrink-0 flex-col gap-6 overflow-y-auto border-r border-slate-200 bg-white p-4">
      <section>
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
            Registry devices
          </h2>
          <button
            type="button"
            onClick={onRefreshRegistry}
            className="rounded px-2 py-0.5 text-xs text-sky-600 hover:bg-sky-50"
          >
            Refresh
          </button>
        </div>

        {registryLoading && <p className="mt-2 text-sm text-slate-400">Loading…</p>}

        {registryError && (
          <p className="mt-2 rounded bg-rose-50 p-2 text-xs text-rose-700">
            Could not reach <span className="font-mono">{ophydApiUrl}/devices</span> — {registryError}
          </p>
        )}

        {!registryLoading && !registryError && registryDevices.length === 0 && (
          <p className="mt-2 text-sm text-slate-400">
            Registry is empty. Start the server with <span className="font-mono">--startup-dir</span>.
          </p>
        )}

        {registryDevices.length > 0 && (
          <>
            <button
              type="button"
              onClick={onSubscribeAllDevices}
              className="mt-2 w-full rounded border border-slate-200 py-1 text-xs text-slate-600 hover:bg-slate-50"
            >
              Subscribe to all {registryDevices.length}
            </button>
            <ul className="mt-2 space-y-1">
              {registryDevices.map((name) => {
                const active = subscribedDevices.includes(name)
                return (
                  <li key={name}>
                    <button
                      type="button"
                      onClick={() => onToggleDevice(name)}
                      className={`w-full truncate rounded px-2 py-1 text-left font-mono text-sm transition
                        ${
                          active
                            ? 'bg-sky-100 text-sky-800'
                            : 'text-slate-600 hover:bg-slate-100'
                        }`}
                    >
                      {active ? '● ' : '○ '}
                      {name}
                    </button>
                  </li>
                )
              })}
            </ul>
          </>
        )}
      </section>

      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
          Arbitrary PVs
        </h2>
        <form
          className="mt-2 flex gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            const pv = pvDraft.trim()
            if (!pv) return
            onAddPv(pv)
            setPvDraft('')
          }}
        >
          <input
            value={pvDraft}
            onChange={(event) => setPvDraft(event.target.value)}
            placeholder="SIM:beam:Current"
            className="min-w-0 flex-1 rounded border border-slate-200 px-2 py-1 font-mono text-sm
              focus:border-sky-400 focus:outline-none"
          />
          <button
            type="submit"
            className="rounded bg-slate-800 px-3 py-1 text-sm font-medium text-white hover:bg-slate-900"
          >
            Add
          </button>
        </form>
        <p className="mt-2 text-xs text-slate-400">
          {subscribedPvs.length} PV{subscribedPvs.length === 1 ? '' : 's'} subscribed via{' '}
          <span className="font-mono">/pv-socket</span>.
        </p>
      </section>
    </aside>
  )
}
