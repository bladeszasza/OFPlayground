import type { WSEvent } from '../types'

export const ACTIVITY_LIMIT = 1000
export const FLOW_LIMIT = 80

export interface ActivityRecord {
  id: number
  observedAt: number
  event: WSEvent
}

export interface FloorObservation {
  conversationId: string
  policy: string
  holder: string | null
  holders: string[]
  queue: string[]
  participants: { speaker_uri: string; name: string }[]
  restricted: string[]
  authoritative: boolean
}

export interface ObservedRoute {
  source: string
  target: string
  type: string
  outcome: string
  scope: string
  count: number
  observedAt: number
  replay: boolean
}

export interface ScopeGroup {
  id: string
  parentId: string | null
  kind: string
  count: number
  agents: string[]
}

export function groupScopes(records: ActivityRecord[]): ScopeGroup[] {
  const groups = new Map<string, ScopeGroup>()
  for (const { event } of records) {
    const id = scopeOf(event)
    if (!groups.has(id) && groups.size >= 100) continue
    const group = groups.get(id) ?? { id, parentId: null, kind: event.scope_kind ?? 'main', count: 0, agents: [] }
    if (event.parent_conversation_id && event.parent_conversation_id !== id) group.parentId = event.parent_conversation_id
    group.count++
    const name = actor(event)
    if (name && !group.agents.includes(name)) group.agents.push(name)
    if (event.type === 'floor_snapshot') event.participants.forEach(participant => {
      if (!group.agents.includes(participant.name)) group.agents.push(participant.name)
    })
    groups.set(id, group)
  }
  return [...groups.values()]
}

export function actor(event: WSEvent): string {
  if ('sender' in event) return event.sender
  if ('agent' in event && typeof event.agent === 'string') return event.agent
  if ('from' in event) return event.from
  if ('to' in event) return event.to
  if ('name' in event) return event.name
  return ''
}

export function scopeOf(event: WSEvent): string {
  return event.scope_id || event.conversation_id || 'main'
}

export function appendActivity(records: ActivityRecord[], next: ActivityRecord): ActivityRecord[] {
  // A resumed stream may repeat a cursor. A replay must not create duplicate trails.
  if (next.event.cursor !== undefined && records.some(item => item.event.cursor === next.event.cursor)) return records
  return [...records.slice(-(ACTIVITY_LIMIT - 1)), next]
}

export function floorAt(records: ActivityRecord[], scope = 'all'): FloorObservation | null {
  let latest: FloorObservation | null = null
  for (const { event } of records) {
    if (scope !== 'all' && scopeOf(event) !== scope) continue
    if (event.type === 'floor_snapshot') {
      latest = {
        conversationId: event.conversation_id || 'main', policy: event.policy,
        holder: event.holder, holders: event.holders ?? (event.holder ? [event.holder] : []), queue: [...event.queue], participants: event.participants,
        restricted: event.restricted ?? [], authoritative: true,
      }
    }
  }
  return latest
}

export function observedRoutes(records: ActivityRecord[], scope: string, focus: string): ObservedRoute[] {
  const routes = new Map<string, ObservedRoute>()
  const names = new Map<string, string>()
  records.forEach(({ event }) => {
    if (event.type === 'floor_snapshot') event.participants.forEach(participant => names.set(participant.speaker_uri, participant.name))
  })
  // The last 120 actual transport events are aggregated; arbitrary graph links never create traffic.
  for (const record of records.slice(-120)) {
    const { event, observedAt } = record
    if (scope !== 'all' && scopeOf(event) !== scope) continue
    if (!event.delivery_outcomes || !('sender_uri' in event)) continue
    for (const [target, outcome] of Object.entries(event.delivery_outcomes)) {
      if (focus && actor(event) !== focus && names.get(target) !== focus) continue
      const key = JSON.stringify([event.sender_uri, target, event.type, outcome, scopeOf(event)])
      const previous = routes.get(key)
      routes.set(key, {
        source: event.sender_uri, target, outcome, type: event.type, scope: scopeOf(event),
        count: (previous?.count ?? 0) + 1, observedAt, replay: !!event.replay,
      })
    }
  }
  return [...routes.values()].slice(-FLOW_LIMIT)
}

export function describeActivity(event: WSEvent): string {
  if (event.type === 'floor_snapshot') return `Floor observed · ${event.holder_name || event.holder || 'no holder'} · ${event.queue.length} queued`
  if (event.type === 'utterance') {
    const outcomes = Object.values(event.delivery_outcomes ?? {})
    const queued = outcomes.filter(value => value === 'queued').length
    const dropped = outcomes.length - queued
    return `${event.sender} · message${outcomes.length ? ` · ${queued} queued${dropped ? `, ${dropped} dropped` : ''}` : ' · routing unknown'}`
  }
  if (event.type === 'error') return `${event.agent} · runtime error`
  return `${actor(event)}${actor(event) ? ' · ' : ''}${event.type.split('_').join(' ')}`
}
