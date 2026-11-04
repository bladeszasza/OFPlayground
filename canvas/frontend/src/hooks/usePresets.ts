import { apiFetch } from '../api'
import { useEffect, useState } from 'react'

export interface PresetEntry {
  name: string
  title: string
  description: string
  policy: string
}

export function usePresets(): PresetEntry[] {
  const [presets, setPresets] = useState<PresetEntry[]>([])
  useEffect(() => {
    apiFetch('/presets/list')
      .then(r => r.ok ? r.json() : [])
      .then((data: PresetEntry[]) => {
        setPresets(data.filter(p => !p.name.startsWith('_')))
      })
      .catch(() => {})
  }, [])
  return presets
}
