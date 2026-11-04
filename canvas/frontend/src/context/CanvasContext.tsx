// canvas/frontend/src/context/CanvasContext.tsx
import { createContext, useContext } from 'react'
import type { FloorNodeData, FloorEventEntry } from '../types'

export interface CanvasCallbacks {
  onRun: () => void
  onStop: () => void
  onFloorChange: (patch: Partial<FloorNodeData>) => void
  onKick: (name: string) => Promise<void>
  onSend: (text: string) => Promise<void>
  onSelectEvent: (event: FloorEventEntry | null) => void
  sessionRunning: boolean
}

const CanvasContext = createContext<CanvasCallbacks | null>(null)

export function useCanvas(): CanvasCallbacks {
  const ctx = useContext(CanvasContext)
  if (!ctx) throw new Error('useCanvas must be used inside CanvasContext.Provider')
  return ctx
}

export { CanvasContext }
