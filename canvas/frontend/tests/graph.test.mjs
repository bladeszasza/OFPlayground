import test from 'node:test'
import assert from 'node:assert/strict'
import { appendActivity, ACTIVITY_LIMIT, FLOW_LIMIT, floorAt, observedRoutes } from '../src/graph/activity.ts'
import { applyDagreLayout } from '../src/graph/layout.ts'

const record = (id, event) => ({ id, observedAt: id * 1000, event: { cursor: id, ...event } })
const participants = [{ speaker_uri: 'urn:a', name: 'A' }, { speaker_uri: 'urn:b', name: 'B' }]
const snapshot = (holder, queue = [], extra = {}) => ({ type: 'floor_snapshot', policy: 'SEQUENTIAL', conversation_id: 'main', holder, queue, participants, ...extra })

test('retention stays bounded over 5,000 events, cursor resume is idempotent', () => {
  let records = []
  for (let i = 1; i <= 5001; i++) records = appendActivity(records, record(i, { type: 'floor_request', from: 'A' }))
  assert.equal(records.length, ACTIVITY_LIMIT)
  assert.equal(records[0].event.cursor, 4002)
  assert.equal(appendActivity(records, record(5001, { type: 'floor_request', from: 'A', replay: true })), records)
})

test('queue and concurrent holders use authoritative observations, never request attempts', () => {
  const attempted = record(1, { type: 'floor_request', from: 'A' })
  assert.equal(floorAt([attempted]), null)
  const records = [attempted, record(2, snapshot('urn:a', ['urn:b']))]
  assert.deepEqual(floorAt(records).queue, ['urn:b'])
  assert.deepEqual(floorAt(records).holders, ['urn:a'])
  records.push(record(3, snapshot('urn:b', [], { holders: ['urn:a', 'urn:b'], policy: 'FREE_FOR_ALL' })))
  assert.deepEqual(floorAt(records).holders, ['urn:a', 'urn:b'])
})

test('scrubbing isolates earlier state while live observations continue', () => {
  const records = [record(1, snapshot('urn:a', ['urn:b'])), record(2, snapshot('urn:b'))]
  const replay = records.filter(item => item.id <= 1)
  const later = appendActivity(records, record(3, snapshot(null)))
  assert.equal(floorAt(replay).holder, 'urn:a')
  assert.equal(floorAt(later).holder, null)
  assert.deepEqual(floorAt(replay).queue, ['urn:b'])
})

test('flows require real routing outcomes and preserve dropped vs queued evidence', () => {
  const rows = [record(1, snapshot('urn:a')), record(2, { type: 'utterance', sender: 'A', sender_uri: 'urn:a', text: 'without route evidence' })]
  assert.deepEqual(observedRoutes(rows, 'all', ''), [])
  rows.push(record(3, { type: 'utterance', sender: 'A', sender_uri: 'urn:a', scope_id: 'child', delivery_outcomes: { 'urn:b': 'queued', 'urn:gone': 'unregistered' } }))
  assert.deepEqual(observedRoutes(rows, 'child', '').map(route => route.outcome), ['queued', 'unregistered'])
  assert.equal(observedRoutes(rows, 'other', '').length, 0)
  assert.equal(observedRoutes(rows, 'child', 'B').length, 1)
  assert.equal(observedRoutes(rows, 'all', '').some(route => route.outcome === 'processed'), false)
})

test('large fleet flow layer is aggregated and bounded', () => {
  const rows = Array.from({ length: 1200 }, (_, i) => record(i, { type: 'utterance', sender: `Agent ${i}`, sender_uri: `urn:${i}`, delivery_outcomes: { [`urn:${i + 1}`]: 'queued' } }))
  const routes = observedRoutes(rows, 'all', '')
  assert.equal(routes.length, FLOW_LIMIT)
  const repeated = [1, 2, 3].map(i => record(i, { type: 'utterance', sender: 'A', sender_uri: 'urn:a', delivery_outcomes: { 'urn:b': 'queued' } }))
  assert.equal(observedRoutes(repeated, 'all', '')[0].count, 3)
})

test('auto-layout retains manual pins with 100 agents and stale edges', () => {
  const nodes = Array.from({ length: 100 }, (_, i) => ({ id: String(i), position: { x: i + .5, y: i + 20 }, data: { pinned: i % 7 === 0 } }))
  const edges = nodes.slice(1).map((node, i) => ({ id: `e-${i}`, source: String(i), target: node.id }))
  const layout = applyDagreLayout(nodes, [...edges, { id: 'stale', source: 'gone', target: '0' }])
  for (const node of layout) {
    assert.equal(Number.isFinite(node.position.x) && Number.isFinite(node.position.y), true)
    if (node.data.pinned) assert.deepEqual(node.position, nodes.find(original => original.id === node.id).position)
  }
  assert.equal(nodes[1].position.x, 1.5)
  assert.notDeepEqual(layout[1].position, nodes[1].position)
})
