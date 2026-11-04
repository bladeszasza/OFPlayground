import { useEffect, useMemo, useState } from 'react'
import { apiFetch } from '../api'
import type { AgentStatus, StatusCell, StatusSnapshot } from '../hooks/useAgentStatus'
import './AgentStatusDock.css'

interface Transition { cursor: number; reporterId: string; receivedAt: number; applied: boolean; event: Record<string, unknown> }
const attention = (agent: AgentStatus) =>
  (agent.governance.value !== 'normal' ? 100 : 0) +
  (['unreachable', 'crashed'].includes(agent.lifecycle.value) ? 50 : 0) +
  (agent.health.value !== 'healthy' ? 20 : 0)
const age = (timestamp: number) => `${Math.max(0, Math.floor(Date.now() / 1000 - timestamp))}s ago`
const summarize = (cells: Record<string, StatusCell>, empty: string) => {
  const values = [...new Set(Object.values(cells).map(cell => cell.value))]
  return values.length ? values.join(', ') : empty
}

function Cell({ label, cell, stale }: { label: string; cell?: StatusCell; stale: boolean }) {
  return <div className="asp-dimension">
    <span>{label}</span><strong>{cell?.value ?? 'unknown'}{cell && !cell.known ? ' (custom)' : ''}</strong>
    <small>{cell ? `${cell.source} · ${age(cell.at)}` : 'No observation'}{stale || cell?.stale ? ' · STALE' : ''}</small>
    {cell?.reason && <small>Reason: {cell.reason}</small>}
  </div>
}

export function AgentStatusDock({ snapshot, streaming, onFocus }: {
  snapshot: StatusSnapshot; streaming: boolean; onFocus: (agentId: string) => void
}) {
  const [search, setSearch] = useState('')
  const [sort, setSort] = useState('attention')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [history, setHistory] = useState<{agentId: string; events: Transition[]; error: string} | null>(null)
  const [collapsed, setCollapsed] = useState(() => window.innerWidth < 900)
  const [operation, setOperation] = useState<{agentId: string; message: string} | null>(null)
  const [busy, setBusy] = useState(false)
  const [authority, setAuthority] = useState('operator')
  const selected = snapshot.agents.find(agent => agent.agentId === selectedId)
  const rows = useMemo(() => snapshot.agents.filter(agent =>
    [agent.name, agent.agentId, agent.lifecycle.value, agent.health.value, agent.governance.value,
      summarize(agent.tasks, ''), summarize(agent.floors, '')].join(' ').toLowerCase().includes(search.toLowerCase())
  ).sort((a, b) => sort === 'name' ? a.name.localeCompare(b.name) : sort === 'recent' ? b.lastSelfEventAt - a.lastSelfEventAt : attention(b) - attention(a) || a.name.localeCompare(b.name)), [snapshot.agents, search, sort])
  const unknown = snapshot.participants.filter(p => !snapshot.agents.some(agent => agent.agentId === p.agentId))
  const active = selected && snapshot.activeAgentIds?.includes(selected.agentId)
  const childSession = selected?.ext['local.ofp-playground']?.parentConversationId
  async function control(action: string) {
    if (!selected || busy) return
    const agentId = selected.agentId
    const report = (message: string) => setOperation({agentId, message})
    setBusy(true); report(`Requested: ${action}`)
    try {
      const removal = action === 'graceful' || action === 'force'
      const response = await apiFetch(removal
        ? `/session/agent/${encodeURIComponent(selected.name)}?mode=${action}&timeout=10`
        : `/session/governance/${encodeURIComponent(selected.agentId)}`, {
          method: removal ? 'DELETE' : 'POST',
          ...(!removal ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value: action, source: authority }) } : {}),
        })
      const body = await response.json()
      if (!response.ok) throw new Error(body.detail ?? 'Operation failed')
      const result = body.operation ?? body
      report(`Requested: ${action} · acknowledged: ${String(result.acknowledged ?? 'unknown')} · enforced: ${String(result.enforced ?? 'unknown')} · ${result.outcome ?? result.error ?? ''}`)
    } catch (error) { report(`Failed: ${error instanceof Error ? error.message : 'Control unavailable'}`) }
    finally { setBusy(false) }
  }
  useEffect(() => {
    let active = true
    if (!selectedId) { setHistory(null); return }
    apiFetch(`/status/history/${encodeURIComponent(selectedId)}`).then(async response => {
      if (!response.ok) throw new Error('Transition history unavailable')
      const body = await response.json()
      if (active) setHistory({agentId: selectedId, events: body.events, error: ''})
    }).catch(error => { if (active) setHistory(previous => ({agentId: selectedId, events: previous?.agentId === selectedId ? previous.events : [], error: String(error.message)})) })
    return () => { active = false }
  }, [selectedId, Math.floor(snapshot.cursor / 5)])

  return <aside className={`asp-dock ${collapsed ? 'asp-collapsed' : ''}`} aria-label="Agent Status">
    <header><div><h2>Agent Status <span>ASP 0.1</span></h2><p>{snapshot.available ? streaming ? 'Live observation' : 'Polling · stream reconnecting' : 'Observation unavailable'}</p></div>
      <button onClick={() => setCollapsed(!collapsed)} aria-expanded={!collapsed} aria-label={collapsed ? 'Open Agent Status' : 'Collapse Agent Status'}>{collapsed ? '◀' : '▶'}</button></header>
    {!collapsed && <>
      <div className="asp-summary" aria-live="polite">{snapshot.agents.length} observed · {snapshot.agents.filter(a => attention(a) > 0).length} need attention · {unknown.length} uninstrumented</div>
      {snapshot.problem && <p className="asp-notice" role="status">{snapshot.problem}</p>}
      <div className="asp-filters"><input aria-label="Search agent status" value={search} onChange={e => setSearch(e.target.value)} placeholder="Search agents or states…" />
        <select aria-label="Sort agent status" value={sort} onChange={e => setSort(e.target.value)}><option value="attention">Needs attention</option><option value="name">Name</option><option value="recent">Last seen</option></select></div>
      <div className="asp-agent-list" style={{maxHeight: selected ? '42%' : 'none'}}>
        {!rows.length && !unknown.length && <p className="asp-empty">Start a session to see instrumented agents. Status records contain state and provenance.</p>}
        {rows.map(agent => <button key={agent.agentId} className={`asp-agent ${attention(agent) ? 'asp-attention' : ''}`} aria-pressed={selectedId === agent.agentId}
          onClick={() => { setSelectedId(agent.agentId); onFocus(agent.agentId) }}>
          <div><strong>{agent.name}</strong><small>{!snapshot.available ? 'STALE' : age(agent.lastSelfEventAt)}</small></div>
          <dl><dt>Lifecycle</dt><dd>{agent.lifecycle.value}</dd><dt>Health</dt><dd>{agent.health.value}</dd>
            <dt>Task</dt><dd>{summarize(agent.tasks, 'No task reported')}</dd><dt>Floor</dt><dd>{summarize(agent.floors, 'Not joined')}</dd>
            <dt>Governance</dt><dd>{agent.governance.value}</dd></dl>
          {Object.keys(agent.observations).length > 0 && <span className="asp-disagreement">Independent observation available</span>}
          {snapshot.agentProblems?.[agent.agentId] && <span className="asp-disagreement">Telemetry error: {snapshot.agentProblems[agent.agentId]}</span>}
        </button>)}
        {unknown.map(p => <div key={p.agentId} className="asp-unknown"><strong>{p.name}</strong><p>Unknown · no trusted ASP self-report</p><small>{snapshot.agentProblems?.[p.agentId] ?? 'Uninstrumented or still initializing'}</small></div>)}
      </div>
      {selected && <section className="asp-detail" aria-label={`${selected.name} status details`}>
        <h3>{selected.name}</h3><p className="asp-id">{selected.agentId}</p>
        <div className="asp-controls" aria-label="Agent controls">
          <label>Authority <select aria-label="Governance authority" value={authority} onChange={e => setAuthority(e.target.value)}><option value="operator">Operator</option><option value="guardrail">Guardrail (independent)</option></select></label>
          <div><button disabled={busy || !active} onClick={() => void control('graceful')}>Drain & remove</button><button disabled={busy || !active} onClick={() => void control('force')}>Force stop</button></div>
          <div><button disabled={busy || !active} onClick={() => void control('quarantined')}>Quarantine & stop</button><button disabled={busy || !!childSession} onClick={() => void control('normal')}>Clear restriction</button></div>
          {childSession && <small>Nested participant: controls belong to its containing session. Stop the parent session to cancel its child work. This dock retains its scoped observations.</small>}
          <small>Drain waits up to 10 seconds for current work. Force/quarantine cancel local work and remove membership. An already submitted provider job may continue remotely. Clearing does not restart an agent.</small>
          {operation?.agentId === selected.agentId && <p role="status">{operation.message}</p>}
        </div>
        <Cell label="Lifecycle" cell={selected.lifecycle} stale={!snapshot.available} />
        <Cell label="Health" cell={selected.health} stale={!snapshot.available} />
        <Cell label="Governance" cell={selected.governance} stale={!snapshot.available} />
        {Object.entries(selected.observations).map(([reporter, cell]) => <div key={reporter} className="asp-independent"><Cell label="Independent health observation" cell={cell} stale={!snapshot.available} /><small>Reporter: {reporter}</small></div>)}
        <p>Activity: {selected.activity} · progress {selected.progressSeq}</p>
        <h4>Tasks</h4>{!Object.keys(selected.tasks).length && <p>No task reported.</p>}
        {Object.entries(selected.tasks).slice(-20).map(([id, cell]) => <div key={id}><code>{id}</code><Cell label="Task" cell={cell} stale={!snapshot.available} /></div>)}
        {Object.keys(selected.tasks).length > 20 && <small>Showing the latest 20 tasks.</small>}
        <h4>Conversations</h4>{Object.entries(selected.floors).map(([id, cell]) => <div key={id}><code>{id}</code><Cell label="Floor" cell={cell} stale={!snapshot.available} /></div>)}
        <h4>Transition history</h4>{history?.agentId !== selected.agentId ? <p role="status">Loading transition history…</p> : history.error && <p role="status">{history.error}</p>}
        <ol className="asp-history">{(history?.agentId === selected.agentId ? history.events : []).filter(record => record.event.type !== 'heartbeat').slice(-50).reverse().map(record => <li key={record.cursor}>
          <button onClick={() => onFocus(selected.agentId)}>{String(record.event.dimension ?? record.event.type)} → {String(record.event.to ?? record.event.type)}</button>
          <small>{String(record.event.source ?? 'self')} · {record.reporterId} · {age(record.receivedAt)}{record.applied ? '' : ' · ignored (ordering)'}</small>
        </li>)}</ol>
      </section>}
    </>}
  </aside>
}
