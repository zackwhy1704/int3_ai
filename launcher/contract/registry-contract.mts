// Contract test: the registries the launcher writes must survive STOCK Hermes's own normalizer
// unchanged in meaning (apps/desktop/electron/connection-registry.ts normalizeRegistry, :1206 at
// v0.22.6): nothing new quarantined, our entry intact with its plain token envelope, the user's
// other connections and quarantine kept, primary = ours after launch and "local" after sign-out.
//
//   cd src-tauri && CB_CONTRACT_OUT=<dir> cargo test emit_contract_fixtures
//   HERMES_DESKTOP_SRC=<hermes-agent>/apps/desktop CB_CONTRACT_OUT=<dir> \
//     <hermes-agent>/node_modules/.bin/tsx contract/registry-contract.mts
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const src = process.env.HERMES_DESKTOP_SRC
const out = process.env.CB_CONTRACT_OUT
if (!src || !out) throw new Error('set HERMES_DESKTOP_SRC and CB_CONTRACT_OUT')

const { normalizeRegistry, REGISTRY_VERSION } = await import(
  pathToFileURL(join(src, 'electron', 'connection-registry.ts')).href
)
const load = (name: string) => JSON.parse(readFileSync(join(out, `${name}.json`), 'utf8'))
const OURS = 'cb-brindlewood-priya'
let passed = 0
const check = (name: string, fn: () => void) => {
  fn()
  passed++
  console.log(`ok - ${name}`)
}

check('REGISTRY_VERSION is still 2', () => assert.equal(REGISTRY_VERSION, 2))

check('fresh launch: ours is primary, nothing quarantined', () => {
  const r = normalizeRegistry(load('launch-fresh'))
  assert.equal(r.primary, OURS)
  assert.equal(r.lastUsed, OURS)
  assert.equal(r.quarantined, undefined)
  const ours = r.connections.find((c: { id: string }) => c.id === OURS)
  assert.ok(ours, 'our entry survives normalization')
  assert.equal(ours.kind, 'remote')
  assert.equal(ours.authMode, 'token')
  assert.equal(ours.url, 'http://127.0.0.1:9432')
  assert.deepEqual(ours.token, { encoding: 'plain', value: 'cb_dt_test' })
  assert.deepEqual(r.connections.map((c: { id: string }) => c.id), ['local', OURS])
})

check('launch over a mixed registry: user data kept, old cb- user gone', () => {
  const r = normalizeRegistry(load('launch-mixed'))
  assert.deepEqual(r.connections.map((c: { id: string }) => c.id), ['local', 'home', OURS])
  assert.equal(r.connections[0].label, 'My PC')
  assert.equal(r.launchMode, 'last-used')
  assert.equal(r.primary, OURS)
  assert.deepEqual(r.quarantined, [{ reason: 'entry-malformed', entry: 'junk' }], 'only the pre-existing quarantine')
  assert.ok(!JSON.stringify(r).includes('cb_dt_ada'))
})

check('sign-out: back to local, no cb- entry, user data kept', () => {
  const r = normalizeRegistry(load('signout'))
  assert.equal(r.primary, 'local')
  assert.equal(r.lastUsed, 'local')
  assert.deepEqual(r.connections.map((c: { id: string }) => c.id), ['local', 'home'])
  assert.ok(!JSON.stringify(r).includes('cb_dt_'))
})

console.log(`# ${passed} passed`)
