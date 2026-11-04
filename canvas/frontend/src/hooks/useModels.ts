import { apiFetch } from '../api'
// canvas/frontend/src/hooks/useModels.ts
import { useEffect, useState } from 'react'

export type ProviderConfiguration = {tasks: string[]; defaults: Record<string, string>; credentialConfigured: boolean}

export function useProviderConfiguration() {
  const [providers, setProviders] = useState<Record<string, ProviderConfiguration>>({})
  const [problem, setProblem] = useState('')
  useEffect(() => {
    apiFetch('/session/configuration').then(async response => {
      if (!response.ok) throw new Error('Provider settings could not be loaded. Reopen Canvas to retry.')
      setProviders((await response.json()).providers)
    }).catch(error => setProblem(error instanceof Error ? error.message : 'Provider settings unavailable'))
  }, [])
  return {providers, problem}
}

export function useModels(): Record<string, string[]> {
  const [models, setModels] = useState<Record<string, string[]>>({
    anthropic: [], openai: [], google: [], huggingface: [],
  })

  useEffect(() => {
    apiFetch('/models/list')
      .then(r => r.json())
      .then((data: Record<string, string[]>) => setModels(data))
      .catch(() => { /* silent fail */ })
  }, [])

  return models
}
