import { useCallback, useState } from 'react'
import { useOphydDeviceSocket, useOphydPVSocket } from '@blueskyproject/finch'
import Sidebar from './components/Sidebar'
import SignalCard from './components/SignalCard'
import { ophydApiUrl } from './lib/config'
import { useDeviceRegistry } from './lib/useDeviceRegistry'

/** A few PVs from caproto/sim_ioc.py, offered as a one-click starting point. */
const SUGGESTED_PVS = [
  'SIM:beam:Current',
  'SIM:beam:Energy_RBV',
  'SIM:temp:Readback',
  'SIM:det:Counts',
  'SIM:m1.RBV',
  'SIM:sample:Filter',
]

export default function App() {
  const registry = useDeviceRegistry()
  const [subscribedDevices, setSubscribedDevices] = useState<string[]>([])
  const [subscribedPvs, setSubscribedPvs] = useState<string[]>([])

  const {
    devices: deviceStates,
    handleSetValueRequest: setDeviceValue,
  } = useOphydDeviceSocket(subscribedDevices)

  const { devices: pvStates, handleSetValueRequest: setPvValue } =
    useOphydPVSocket(subscribedPvs)

  const toggleDevice = useCallback((name: string) => {
    setSubscribedDevices((current) =>
      current.includes(name) ? current.filter((entry) => entry !== name) : [...current, name],
    )
  }, [])

  const addPv = useCallback((pv: string) => {
    setSubscribedPvs((current) => (current.includes(pv) ? current : [...current, pv]))
  }, [])

  const removePv = useCallback((pv: string) => {
    setSubscribedPvs((current) => current.filter((entry) => entry !== pv))
  }, [])

  return (
    <div className="flex h-full bg-slate-50 text-slate-900">
      <Sidebar
        registryDevices={registry.devices}
        registryError={registry.error}
        registryLoading={registry.loading}
        onRefreshRegistry={() => void registry.refresh()}
        subscribedDevices={subscribedDevices}
        onToggleDevice={toggleDevice}
        onSubscribeAllDevices={() => setSubscribedDevices(registry.devices)}
        subscribedPvs={subscribedPvs}
        onAddPv={addPv}
      />

      <main className="flex-1 overflow-y-auto">
        <header className="border-b border-slate-200 bg-white px-6 py-4">
          <h1 className="text-lg font-semibold">Ophyd WebSocket test harness</h1>
          <p className="font-mono text-xs text-slate-400">{ophydApiUrl}</p>
        </header>

        <div className="space-y-8 p-6">
          <section>
            <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">
              Devices — /device-socket
            </h2>
            {subscribedDevices.length === 0 ? (
              <p className="text-sm text-slate-400">
                Pick devices from the registry on the left to subscribe.
              </p>
            ) : (
              <ul className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
                {subscribedDevices.map((name) => (
                  <SignalCard
                    key={name}
                    name={name}
                    signal={deviceStates[name]}
                    onSet={setDeviceValue}
                    onRemove={toggleDevice}
                  />
                ))}
              </ul>
            )}
          </section>

          <section>
            <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">
              PVs — /pv-socket
            </h2>
            {subscribedPvs.length === 0 ? (
              <div className="text-sm text-slate-400">
                <p>Add any PV name on the left, or start with one of these:</p>
                <div className="mt-2 flex flex-wrap gap-2">
                  {SUGGESTED_PVS.map((pv) => (
                    <button
                      key={pv}
                      type="button"
                      onClick={() => addPv(pv)}
                      className="rounded border border-slate-200 bg-white px-2 py-1 font-mono text-xs
                        text-slate-600 hover:bg-slate-100"
                    >
                      {pv}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              <ul className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
                {subscribedPvs.map((pv) => (
                  <SignalCard
                    key={pv}
                    name={pv}
                    signal={pvStates[pv]}
                    onSet={setPvValue}
                    onRemove={removePv}
                  />
                ))}
              </ul>
            )}
          </section>
        </div>
      </main>
    </div>
  )
}
