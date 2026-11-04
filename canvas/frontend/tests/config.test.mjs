import test from 'node:test'
import assert from 'node:assert/strict'
import {serializeConfiguration,parseConfiguration,restoreConfigurationLayout,preferredConfiguration} from '../src/graph/config.ts'
import {applyDagreLayout} from '../src/graph/layout.ts'

test('saved configuration excludes runtime output and credentials while retaining authored settings and layout', () => {
  const nodes=[{id:'floor',type:'FloorNode',position:{x:10,y:20},data:{policy:'SEQUENTIAL',sessionId:'private-runtime'}},
    {id:'custom-worker',type:'AgentNode',position:{x:200,y:40},data:{name:'Worker',provider:'demo',systemPrompt:'Authored role',pinned:true,messages:['private output'],asp:{health:'healthy'},apiKey:'never-save-this'}}]
  const saved=serializeConfiguration(nodes,[{id:'membership',source:'floor',target:'custom-worker'}])
  const encoded=JSON.stringify(saved)
  assert.ok(!encoded.includes('private')&&!encoded.includes('never-save'))
  assert.ok(encoded.includes('Authored role'))
  const parsed=parseConfiguration(encoded)
  const hydrated=[{id:'agent-Worker',type:'AgentNode',position:{x:0,y:0},data:{name:'Worker'}}]
  assert.deepEqual(restoreConfigurationLayout(hydrated,parsed)[0].position,{x:200,y:40})
  assert.equal(restoreConfigurationLayout(hydrated,parsed)[0].data.pinned,true)
})

test('invalid or ambiguous imports fail before replacing the user configuration', () => {
  assert.throws(()=>parseConfiguration('{'))
  assert.throws(()=>parseConfiguration(JSON.stringify({nodes:[],edges:[]})),/exactly one floor/)
  assert.throws(()=>parseConfiguration(JSON.stringify({nodes:[{id:'a',type:'FloorNode',data:{}},{id:'a',type:'AgentNode',data:{name:'A',provider:'demo'}}],edges:[]})),/unique IDs/)
  assert.throws(()=>parseConfiguration(' '.repeat(1024*1024+1)),/1 MiB/)
})

test('membership layout tiles participants without moving pins', () => {
  const floor={id:'floor',type:'FloorNode',position:{x:0,y:0},data:{}}
  const nodes=[floor,...Array.from({length:6},(_,index)=>({id:String(index),type:'AgentNode',position:{x:5,y:6},data:{pinned:index===5}}))]
  const result=applyDagreLayout(nodes,[])
  assert.deepEqual(result[6].position,{x:5,y:6})
  assert.equal(new Set(result.slice(1,6).map(node=>JSON.stringify(node.position))).size,5)
  assert.ok(Math.max(...result.slice(1,6).map(node=>node.position.y))<=240)
  const fleet=applyDagreLayout([floor,...Array.from({length:100},(_,index)=>({id:`fleet-${index}`,type:'AgentNode',position:{x:0,y:0},data:{}}))],[])
  assert.equal(new Set(fleet.slice(1).map(node=>node.position.x)).size,10)
  assert.equal(new Set(fleet.slice(1).map(node=>node.position.y)).size,10)
})

test('reopening a stopped runner preserves newer imported settings; live runners and fresh CLI prepopulation win', () => {
  const previous={name:'old runner'}, imported={name:'new imported settings'}
  assert.equal(preferredConfiguration(previous,imported,false,true),imported)
  assert.equal(preferredConfiguration(previous,imported,true,true),previous)
  assert.equal(preferredConfiguration(previous,imported,false,false),previous)
})
