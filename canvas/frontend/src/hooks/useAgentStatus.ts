import { useEffect, useState } from 'react'
import { apiFetch } from '../api'

export interface StatusCell {
  value: string; source: string; reporterId: string; at: number
  eventId: string; stale: boolean; known: boolean; reason?: string
  ext?: Record<string, Record<string, string>>
}
export interface AgentStatus {
  agentId: string; name: string; lifecycle: StatusCell; health: StatusCell; governance: StatusCell
  tasks: Record<string, StatusCell>; floors: Record<string, StatusCell>
  observations: Record<string, StatusCell>; lastSelfEventAt: number; progressSeq: number
  heartbeatIntervalSec: number; activity: string
  ext: Record<string, Record<string, string>>
}
export interface Participant { agentId: string; name: string; speakerUri: string }
export interface StatusSnapshot {
  available: boolean; problem: string | null; agents: AgentStatus[]; cursor: number
  observedAt?: number; participants: Participant[]; agentProblems?: Record<string, string>
  activeAgentIds?: string[]
  operations?: Array<Record<string, unknown>>
}

export function useAgentStatus(sessionId: string | null) {
  const [snapshot, setSnapshot] = useState<StatusSnapshot>({ available: false, problem: null, agents: [], cursor: 0, participants: [] })
  const [streaming, setStreaming] = useState(false)
  useEffect(() => {
    if (!sessionId) {
      setSnapshot({available:false,problem:'Run a session to observe agents',agents:[],cursor:0,participants:[]})
      setStreaming(false)
      return
    }
    if (sessionId) setSnapshot({ available: false, problem: 'Initializing status observation…', agents: [], cursor: 0, participants: [] })
    let disposed = false
    let source: EventSource | null = null
    let refreshing = false
    let debounce: ReturnType<typeof setTimeout> | null = null
    const notify = () => {
      if (!debounce) debounce = setTimeout(() => { debounce = null; void refresh() }, 350)
    }
    const openStream = () => {
      if (source || disposed) return
      source = new EventSource('/status/stream', { withCredentials: true })
      source.onopen = () => { if (!disposed) setStreaming(true) }
      source.onerror = () => { if (!disposed) setStreaming(false) }
      for (const type of ['register', 'heartbeat', 'state_change', 'floor_change', 'deregister', 'snapshot', 'gap']) {
        source.addEventListener(type, notify)
      }
      source.addEventListener('unavailable', () => {
        source?.close(); source = null
        if (!disposed) setStreaming(false)
        notify()
      })
    }
    async function refresh() {
      if (refreshing || disposed) return
      refreshing = true
      try {
        const response = await apiFetch('/status/snapshot')
        if (!response.ok) throw new Error('Status observation unavailable')
        const next = await response.json() as StatusSnapshot
        if (disposed) return
        // Retain evidence during an outage and mark it stale in the view.
        setSnapshot(previous => next.available ? next : { ...next, agents: previous.agents })
        if (next.available) openStream()
      } catch {
        if (!disposed) setSnapshot(previous => ({ ...previous, available: false, problem: 'Observation disconnected. Retained state is stale.' }))
      } finally { refreshing = false }
    }
    void refresh()
    const timer = setInterval(() => { void refresh() }, 2000)
    return () => {
      disposed = true; clearInterval(timer)
      if (debounce) clearTimeout(debounce)
      source?.close(); setStreaming(false)
    }
  }, [sessionId])
  return { snapshot, streaming }
}
