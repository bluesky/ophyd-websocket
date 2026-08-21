import { CameraCanvas, useOphydPVSocket } from '@blueskyproject/finch'
import SignalCard from './SignalCard'

export interface CameraPanelProps {
  /** Detector PV prefix without the trailing colon, e.g. `SIMDET1`. */
  prefix: string
}

/**
 * Live image stream from the simulated areaDetector.
 *
 * `CameraCanvas` opens its own `/camera-socket` connection and negotiates the
 * frame geometry from `<prefix>:cam1:{MinX,MinY,SizeX,SizeY,ColorMode,DataType}`,
 * so all it needs is the prefix. The cards beside it drive the detector over
 * the ordinary PV socket.
 */
export default function CameraPanel({ prefix }: CameraPanelProps) {
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
    <div className="flex flex-wrap items-start gap-6">
      <CameraCanvas prefix={prefix} canvasSize="medium" />

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
  )
}
