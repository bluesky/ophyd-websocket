import { TIFFCanvas, useOphydPVSocket } from '@blueskyproject/finch'
import SignalCard from './SignalCard'
import StreamSettingsForm, { type StreamField } from './StreamSettingsForm'
import { fileWriterPvs, stripColon, streamWsUrl, useStreamSettings } from '../lib/streamSettings'

export interface H5PanelProps {
  /** Default HDF5 detector PV prefix without the trailing colon, e.g. `SIMHDF1`. */
  defaultPrefix: string
}

const FIELDS: StreamField[] = [
  { name: 'prefix', label: 'Detector prefix', placeholder: '13PIL1' },
  { name: 'plugin', label: 'HDF5 plugin', placeholder: 'HDF1' },
  {
    name: 'pv',
    label: 'Exact file path PV (optional)',
    placeholder: '13PIL1:HDF1:FullFileName_RBV',
    hint: 'Overrides prefix + plugin',
  },
  {
    name: 'dataset',
    label: 'Dataset (optional)',
    placeholder: '/entry/data/data',
    hint: 'Blank = /entry/data/data, else largest image dataset',
  },
]

/**
 * Newest-frame viewer for an areaDetector HDF5 writer.
 *
 * Reuses finch's `TIFFCanvas` pointed at `/h5-socket`, with the PV settings on
 * the socket URL's query string. The server watches
 * `<prefix>:<plugin>:FullFileName_RBV` and `NumCaptured_RBV`, re-reads the file
 * whenever either changes, and pushes the last frame as JPEG.
 */
export default function H5Panel({ defaultPrefix }: H5PanelProps) {
  const { settings, generation, apply, reset } = useStreamSettings('h5', {
    prefix: defaultPrefix,
    plugin: 'HDF1',
    pv: '',
    dataset: '',
  })

  const statusPvs = fileWriterPvs(settings, ['ArrayCounter_RBV'])
  const { devices, handleSetValueRequest } = useOphydPVSocket(statusPvs)

  return (
    <div>
      <StreamSettingsForm fields={FIELDS} settings={settings} onApply={apply} onReset={reset} />

      <div className="flex flex-wrap items-start gap-6">
        <TIFFCanvas
          key={generation}
          prefix={stripColon(settings.prefix)}
          canvasSize="medium"
          wsUrl={streamWsUrl('h5-socket', settings)}
        />

        <ul className="grid min-w-80 flex-1 gap-4 sm:grid-cols-2">
          {statusPvs.map((pv) => (
            <SignalCard
              key={pv}
              name={pv}
              signal={devices[pv]}
              onSet={handleSetValueRequest}
              /* Read-only file-writer status, not an ad-hoc subscription list. */
              onRemove={() => {}}
              removable={false}
            />
          ))}
        </ul>
      </div>
    </div>
  )
}
