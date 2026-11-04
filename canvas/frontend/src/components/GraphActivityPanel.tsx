import { useMemo, useState } from 'react'
import type { useGraphActivity } from '../hooks/useGraphActivity'
import { actor, describeActivity, scopeOf, type ActivityRecord, type ScopeGroup } from '../graph/activity'
import './GraphActivityPanel.css'

type Activity = ReturnType<typeof useGraphActivity>
export function GraphActivityPanel({ activity, connected, onInspect }: {
  activity: Activity; connected: boolean; onInspect: (record: ActivityRecord) => void
}) {
  const [expanded, setExpanded] = useState(true)
  const rows = useMemo(() => activity.visible.filter(({ event }) =>
    (activity.scope === 'all' || scopeOf(event) === activity.scope) &&
    (!activity.focus || actor(event) === activity.focus || event.type === 'floor_snapshot' && event.participants.some(p => p.name === activity.focus)) &&
    (activity.typeFilter === 'all' || event.type === activity.typeFilter)
  ).slice(-35).reverse(), [activity.visible, activity.scope, activity.focus, activity.typeFilter])
  const types = [...new Set(activity.records.map(({ event }) => event.type))].sort()
  const index = activity.cursor === null ? activity.records.length - 1 : Math.max(0, activity.records.findIndex(row => row.id === activity.cursor))
  const floor = activity.floor
  const name = (uri: string) => floor?.participants.find(p => p.speaker_uri === uri)?.name ?? uri
  const renderGroup = (group: ScopeGroup, ancestors: string[] = []): React.ReactNode => <li key={group.id}>
    <button onClick={() => activity.setScope(group.id)} aria-pressed={activity.scope === group.id}>
      {group.kind} · {group.id.slice(0, 24)} · {group.count} events · {group.agents.length} observed agents
    </button>
    {ancestors.length < 8 && <ul>{activity.groups.filter(child => child.parentId === group.id && ![...ancestors, group.id].includes(child.id)).map(child => renderGroup(child, [...ancestors, group.id]))}</ul>}
  </li>
  return <section className="graph-activity" aria-label="Observed communication and replay">
    <div className="graph-activity-heading">
      <button aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? '▾' : '▸'} Communication</button>
      <strong>{activity.cursor === null ? connected ? 'LIVE' : 'DISCONNECTED' : 'REPLAY'}</strong>
      <span>{activity.records.length} retained events · ASP dock stays live</span>
      <button onClick={activity.live} disabled={activity.cursor === null}>Return to live</button>
    </div>
    {expanded && <>
      <div className="graph-floor-summary">
        {floor ? <><span>{floor.policy.toLowerCase() === 'free_for_all' ? `Open floor · holders: ${floor.holders.length ? floor.holders.map(name).join(', ') : 'none'}` : `Floor holder: ${floor.holder ? name(floor.holder) : 'none'}`}</span>
          <span>Queue: {floor.queue.length ? floor.queue.map((uri, i) => `${i + 1}. ${name(uri)}`).join(' → ') : 'empty'}</span>
          {floor.restricted.length > 0 && <span>Restricted: {floor.restricted.map(name).join(', ')}</span>}</>
          : <span>Floor and queue: awaiting runtime observation</span>}
      </div>
      <div className="graph-activity-filters">
        <label>Session <select aria-label="Conversation scope" value={activity.scope} onChange={e => activity.setScope(e.target.value)}>
          <option value="all">All sessions</option>{activity.scopes.map(scope => <option key={scope} value={scope}>{scope === 'main' ? 'Main' : scope}</option>)}
        </select></label>
        <label>Agent <select aria-label="Focus communication agent" value={activity.focus} onChange={e => activity.setFocus(e.target.value)}>
          <option value="">All agents</option>{activity.agents.map(name => <option key={name}>{name}</option>)}
        </select></label>
        <label>Events <select aria-label="Communication event type" value={activity.typeFilter} onChange={e => activity.setTypeFilter(e.target.value)}>
          <option value="all">All types</option>{types.map(type => <option key={type}>{type}</option>)}
        </select></label>
        <button disabled={!activity.records.length} aria-label={activity.playing ? 'Pause replay' : 'Play replay'} onClick={() => {
          if (activity.cursor === null) activity.setCursor(activity.records[0]?.id ?? null)
          activity.setPlaying(!activity.playing)
        }}>{activity.playing ? 'Pause' : 'Play'}</button>
        <input aria-label="Scrub observed communication" type="range" min={0} max={Math.max(0, activity.records.length - 1)}
          disabled={!activity.records.length} value={Math.max(0, index)} onChange={e => {
            activity.setPlaying(false); activity.setCursor(activity.records[Number(e.target.value)]?.id ?? null)
          }} />
      </div>
      {activity.groups.length > 1 && <details className="graph-scope-groups"><summary>Session groups ({activity.groups.length})</summary><ul>
        {activity.groups.filter(group => !group.parentId || !activity.groups.some(parent => parent.id === group.parentId)).map(group => renderGroup(group))}
      </ul><small>Grouping follows the runtime's parent conversation IDs. Agents can participate in several sessions.</small></details>}
      <div className="graph-activity-list" role="log" aria-label="Recent observed events">
        {rows.length ? rows.map(record => <button key={record.id} aria-pressed={activity.cursor === record.id} onClick={() => { activity.setCursor(record.id); activity.setPlaying(false); onInspect(record) }}>
          <time>{new Date(typeof record.event.ts === 'number' ? record.event.ts : record.event.ts || record.observedAt).toLocaleTimeString()}</time>
          <span>{describeActivity(record.event)}</span>
          <small>{record.event.replay ? 'resumed' : 'observed'} · {scopeOf(record.event).slice(0, 22)}</small>
        </button>) : <p>{activity.records.length ? 'No events match these filters.' : 'Start a session to see actual communication. Replay uses the last 1,000 observations.'}</p>}
      </div>
      <small className="graph-activity-note">Arrow = observed route. “Queued” means accepted by the local inbox; processing is unconfirmed. Timestamps use server time when present, otherwise browser observation time.</small>
    </>}
  </section>
}
