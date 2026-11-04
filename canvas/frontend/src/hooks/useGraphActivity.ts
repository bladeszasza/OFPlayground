import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { WSEvent } from '../types'
import { appendActivity, floorAt, observedRoutes, groupScopes, scopeOf, type ActivityRecord } from '../graph/activity'

export function useGraphActivity(sessionId: string | null) {
  const [records, setRecords] = useState<ActivityRecord[]>([])
  const [cursor, setCursor] = useState<number | null>(null)
  const [playing, setPlaying] = useState(false)
  const [scope, setScope] = useState('all')
  const [focus, setFocus] = useState('')
  const [typeFilter, setTypeFilter] = useState('all')
  const [now, setNow] = useState(Date.now())
  const nextId = useRef(0)
  useEffect(() => {
    setRecords([]); setCursor(null); setPlaying(false); setScope('all'); setFocus('')
    nextId.current = 0
  }, [sessionId])
  const observe = useCallback((event: WSEvent) => {
    const id = ++nextId.current
    setRecords(previous => appendActivity(previous, { id, event, observedAt: Date.now() }))
  }, [])
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 700)
    return () => clearInterval(timer)
  }, [])
  useEffect(() => {
    if (!playing) return
    const timer = setInterval(() => setCursor(previous => {
      const index = records.findIndex(record => record.id === previous)
      if (index < 0 || index >= records.length - 1) { setPlaying(false); return previous }
      return records[index + 1].id
    }), 650)
    return () => clearInterval(timer)
  }, [playing, records])
  const visible = useMemo(() => cursor === null ? records : records.filter(record => record.id <= cursor), [records, cursor])
  const scopes = useMemo(() => [...new Set(records.map(record => scopeOf(record.event)))], [records])
  const agents = useMemo(() => {
    const names = new Set<string>()
    records.forEach(({ event }) => {
      if ('name' in event) names.add(event.name)
      if ('sender' in event) names.add(event.sender)
      if (event.type === 'floor_snapshot') event.participants.forEach(agent => names.add(agent.name))
    })
    return [...names].sort()
  }, [records])
  const floor = useMemo(() => floorAt(visible, scope), [visible, scope])
  const routes = useMemo(() => observedRoutes(visible, scope, focus), [visible, scope, focus])
  const groups = useMemo(() => groupScopes(visible), [visible])
  return { records, visible, cursor, setCursor, playing, setPlaying, scope, setScope, focus, setFocus,
    typeFilter, setTypeFilter, scopes, agents, floor, routes, groups, observe, now,
    live: () => { setCursor(null); setPlaying(false) } }
}
