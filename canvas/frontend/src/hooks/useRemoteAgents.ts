import { apiFetch } from '../api'
// canvas/frontend/src/hooks/useRemoteAgents.ts
import { useEffect, useState } from 'react'

export interface RemoteAgentEntry {
  slug: string
  name: string
  description: string
}

export function useRemoteAgents(): RemoteAgentEntry[] {
  const [entries, setEntries] = useState<RemoteAgentEntry[]>([])
  useEffect(() => {
    apiFetch('/remote-agents/list')
      .then(r => r.ok ? r.json() : [])
      .then(setEntries)
      .catch(() => {})
  }, [])
  return entries
}
