import { CameraCanvas, useOphydPVSocket } from '@blueskyproject/finch'
import SignalCard from './SignalCard'
import StreamSettingsForm, { type StreamField } from './StreamSettingsForm'
import { streamWsUrl, stripColon, useStreamSettings } from '../lib/streamSettings'

export type CameraPanelProps =
  | {
      /** Default detector PV prefix without the trailing colon, e.g. `SIMDET1`. */
      defaultPrefix: string
      device?: undefined
    }
  | {
      /** Name of a camera in the device registry, e.g. a BCS `ccd_camera`. */
      device: string
      defaultPrefix?: undefined
    }

const FIELDS: StreamField[] = [
  { name: 'prefix', label: 'Detector prefix', placeholder: '13SIM1', hint: 'Uses <prefix>:cam1:* for frame geometry' },
  {
    name: 'imageArrayPV',
    label: 'Image array PV (optional)',
    placeholder: '<prefix>:image1:ArrayData',
    hint: 'Override for a plugin other than image1',
  },
]

/**
 * Live image stream, from an areaDetector's PVs or from a registry camera.
 */
export default function CameraPanel(props: CameraPanelProps) {
  return props.device !== undefined ? (
    <RegistryCamera device={props.device} />
  ) : (
    <AreaDetectorCamera defaultPrefix={props.defaultPrefix} />
  )
}

/**
 * A registry camera over `/camera-socket-shared?device=<name>`. The server
 * polls the camera at its own fixed rate and shares the frames with every
 * viewer, and finch's opening PV message is ignored there.
 */
function RegistryCamera({ device }: { device: string }) {
  return (
    <div>
      <p className="mb-2 font-mono text-sm text-slate-600">{device}</p>
      <CameraCanvas wsUrl={streamWsUrl('camera-socket-shared', { device })} canvasSize="medium" />
    </div>
  )
}

/**
 * Live image stream from an areaDetector.
 *
 * `CameraCanvas` opens its own `/camera-socket` connection and negotiates the
 * frame geometry from `<prefix>:cam1:{MinX,MinY,SizeX,SizeY,ColorMode,DataType}`,
 * so all it needs is the prefix (plus, optionally, the image array PV). The
 * cards beside it drive the detector over the ordinary PV socket.
 */
function AreaDetectorCamera({ defaultPrefix }: { defaultPrefix: string }) {
  const { settings, generation, apply, reset } = useStreamSettings('camera', {
    prefix: defaultPrefix,
    imageArrayPV: '',
  })
  const prefix = stripColon(settings.prefix)

  const controlPvs = [
    `${prefix}:cam1:Acquire`,
    `${prefix}:cam1:SimMode`,
    `${prefix}:cam1:ColorMode`,
    `${prefix}:cam1:AcquirePeriod`,
    `${prefix}:cam1:ArrayRate_RBV`,
    `${prefix}:cam1:ArrayCounter_RBV`,
  ]

  const { devices, handleSetValueRequest } = useOphydPVSocket(controlPvs)

  return (
    <div>
      <StreamSettingsForm fields={FIELDS} settings={settings} onApply={apply} onReset={reset} />

      <div className="flex flex-wrap items-start gap-6">
        <CameraCanvas
          key={generation}
          prefix={prefix}
          imageArrayPV={settings.imageArrayPV || undefined}
          canvasSize="medium"
        />

        <ul className="grid min-w-80 flex-1 gap-4 sm:grid-cols-2">
          {controlPvs.map((pv) => (
            <SignalCard
              key={pv}
              name={pv}
              signal={devices[pv]}
              onSet={handleSetValueRequest}
              /* These cards are the detector's fixed controls, not an
                 ad-hoc subscription list, so removal is a no-op. */
              onRemove={() => {}}
              removable={false}
            />
          ))}
        </ul>
      </div>
    </div>
  )
}
