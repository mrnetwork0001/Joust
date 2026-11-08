#!/usr/bin/env node
/**
 * Joust — Hermes Desktop plugin smoke harness (runs WITHOUT the Desktop app).
 *
 *   1. Registers Node module hooks (loader-hooks.mjs) that rewrite the plugin's bare
 *      specifiers the way apps/desktop/src/contrib/runtime-loader.ts does
 *      (`@hermes/plugin-sdk` -> sdk-stub.mjs, `react*` -> the scratch node_modules) and
 *      refuse any other import, exactly like the real loader.
 *   2. Imports desktop/plugin.js, calls register(ctx) with a recording ctx and asserts every
 *      contribution id / area / data shape against the Hermes 0.23.0 SDK contracts.
 *   3. Server-renders EVERY contributed render() with react-dom/server. useEffect never runs
 *      in renderToString, so the loaded / merged / sparse / busy / error branches are reached
 *      by seeding initial state through react-shim.mjs. React key/prop warnings fail the run.
 *   4. If happy-dom is installed, mounts each component with react-dom/client so the effects
 *      DO run against a fake ctx.rest (fixture | empty | reject), clicks the buttons, fires
 *      `plugin.joust.bout.updated` through the fake ctx.onEvent and checks listener disposal.
 *   5. Static "eslint-ish" checks on the source: hook placement, dangerous DOM APIs, list
 *      keys, import allowlist and every `(--ui-*)` Tailwind token against the variables the
 *      Desktop stylesheet actually defines.
 *
 * Fixture: fixture_bout.json was produced by running the REAL dashboard/plugin_api.py router
 * (POST /bouts/demo, GET /bouts, GET /bouts/{id}, POST evaluate, POST merge dry_run) through
 * fastapi's TestClient, plus one un-evaluated bout from create_bout(). Paths were neutralised.
 *
 * Exit codes: 0 every fatal check passed (warnings may be printed); 1 a check failed;
 * 2 prerequisites missing (react / react-dom not under JOUST_SMOKE_NODE_MODULES).
 * Env: JOUST_SMOKE_NODE_MODULES (default: $TMPDIR/joust-desktop-smoke/node_modules),
 *      JOUST_SMOKE_STRICT=1 (warnings become failures), JOUST_SMOKE_NO_DOM=1 (skip step 4),
 *      JOUST_PLUGIN_JS (alternate plugin file).
 */

import fs from 'node:fs'
import nodeModule from 'node:module'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(here, '..', '..')
const pluginPath = process.env.JOUST_PLUGIN_JS
  ? path.resolve(process.env.JOUST_PLUGIN_JS)
  : path.join(repoRoot, 'desktop', 'plugin.js')
const nodeModulesDir = process.env.JOUST_SMOKE_NODE_MODULES
  ? path.resolve(process.env.JOUST_SMOKE_NODE_MODULES)
  : path.join(os.tmpdir(), 'joust-desktop-smoke', 'node_modules')
const strict = process.env.JOUST_SMOKE_STRICT === '1'
const wantDom = process.env.JOUST_SMOKE_NO_DOM !== '1'

for (const dep of ['react', 'react-dom']) {
  if (!fs.existsSync(path.join(nodeModulesDir, dep, 'package.json'))) {
    console.error(`[desktop-smoke] missing ${dep} under ${nodeModulesDir} — see tests/desktop_smoke/README.md`)
    process.exit(2)
  }
}

if (!fs.existsSync(pluginPath)) {
  console.error(`[desktop-smoke] plugin not found: ${pluginPath}`)
  process.exit(2)
}

const pluginUrl = pathToFileURL(pluginPath).href
const anchorUrl = pathToFileURL(path.join(path.dirname(nodeModulesDir), 'package.json')).href

const hookConfig = {
  sdkStubUrl: new URL('./sdk-stub.mjs', import.meta.url).href,
  reactShimUrl: new URL('./react-shim.mjs', import.meta.url).href,
  anchorUrl,
  pluginUrl
}

if (typeof nodeModule.registerHooks === 'function') {
  // Node >= 23.5 / 22.15: synchronous in-thread hooks (module.register() is deprecated there, DEP0205).
  const { createHooks } = await import('./loader-hooks.mjs')
  nodeModule.registerHooks(createHooks(hookConfig))
} else {
  nodeModule.register('./loader-hooks.mjs', { parentURL: import.meta.url, data: hookConfig })
}

// Everything below must be a dynamic import: static imports are hoisted above the hook registration.
const React = (await import('react')).default
const { renderToString } = await import('react-dom/server')
const sdk = await import('./sdk-stub.mjs')
const shim = await import('./react-shim.mjs')

const source = fs.readFileSync(pluginPath, 'utf8')
const fixture = JSON.parse(fs.readFileSync(path.join(here, 'fixture_bout.json'), 'utf8'))

// ── tiny test runner ────────────────────────────────────────────────────────

const results = { pass: [], fail: [], warn: [], skip: [] }

function assert(cond, msg) {
  if (!cond) {
    throw new Error(msg)
  }
}

function eq(actual, expected, msg) {
  if (actual !== expected) {
    throw new Error(`${msg}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`)
  }
}

function pass(name) {
  results.pass.push(name)
  console.log('  ok   ' + name)
}

function fail(name, error) {
  const message = error && error.message ? error.message : String(error)
  results.fail.push(`${name} — ${message}`)
  console.log(`  FAIL ${name}\n       ${message}`)
}

function check(name, fn) {
  try {
    fn()
    pass(name)
  } catch (error) {
    fail(name, error)
  }
}

async function checkAsync(name, fn) {
  try {
    await fn()
    pass(name)
  } catch (error) {
    fail(name, error)
  }
}

/** Non-fatal unless JOUST_SMOKE_STRICT=1: the thing is wrong today but must not block CI. */
function warn(name, cond, detail) {
  if (cond) {
    pass(name)

    return
  }

  const entry = detail ? `${name} — ${detail}` : name

  if (strict) {
    results.fail.push(entry)
  } else {
    results.warn.push(entry)
  }

  console.log(`  ${strict ? 'FAIL' : 'WARN'} ${name}${detail ? `\n       ${detail}` : ''}`)
}

const consoleLines = []
const original = { error: console.error, warn: console.warn }

function captureConsole() {
  console.error = (...args) => consoleLines.push(args.map(String).join(' '))
  console.warn = console.error
}

function restoreConsole() {
  console.error = original.error
  console.warn = original.warn
}

const count = (haystack, needle) => haystack.split(needle).length - 1

/** What React's server renderer emits for a text node (`&` → `&amp;`, quotes, angle brackets). */
const esc = s =>
  String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#x27;')

/** React 19 warnings carry no "Warning:" prefix; match the messages themselves, never Node's own DeprecationWarnings. */
const REACT_WARNING_RE =
  /unique "key"|Each child in a list|not wrapped in act|Can't perform a React state update|unmounted component|Invalid DOM property|does not recognize the|Cannot update a component|Maximum update depth|validateDOMNesting|cannot appear as a child|cannot be a descendant/

// ── fixture-backed fake ctx ─────────────────────────────────────────────────

const fx = fixture.detail.bout
const launched = fixture.launched.bout
const merged = {
  ...fx,
  status: 'merged',
  merged_contender_id: fx.winner_id,
  contenders: fx.contenders.map(c => ({ ...c, is_merged: c.contender_id === fx.winner_id }))
}
const hostile = {
  ...fx,
  title: '<img src=x onerror=alert(1)>',
  goal: '</div><script>alert(1)</script>',
  contenders: fx.contenders.map((c, i) =>
    i
      ? c
      : {
          ...c,
          name: '<b>bold</b>',
          strategy: '"quoted" & <i>italic</i>',
          diff_preview: '+<script>x</script>\n-</pre>\n@@ hunk',
          test_output: '<svg onload=alert(1)>'
        }
  )
}

/**
 * mode 'fixture': the recorded API answers; 'empty': no bouts / un-evaluated bout;
 * 'reject': every call rejects like pluginRest does without the desktop bridge.
 */
function makeCtx({ mode = 'fixture', list = fixture.list } = {}) {
  const restLog = []
  const events = []
  const contributions = []
  const disposed = []

  const rest = (p, opts = {}) => {
    const method = String(opts.method || 'GET').toUpperCase()
    restLog.push({ path: p, method, body: opts.body })

    if (mode === 'reject') {
      return Promise.reject(new Error('Hermes desktop bridge unavailable'))
    }

    const pathname = p.split(/[?#]/)[0]
    const detail = pathname.match(/^\/bouts\/([^/]+)$/)
    const evaluate = pathname.match(/^\/bouts\/([^/]+)\/evaluate$/)
    const mergeCall = pathname.match(/^\/bouts\/([^/]+)\/merge$/)
    const idOf = m => decodeURIComponent(m[1])

    if (mode === 'empty') {
      if (pathname === '/bouts' && method === 'GET') {
        return Promise.resolve({ ok: true, bouts: [], total_bouts: 0, total_contenders: 0, merged_bouts: 0 })
      }

      if (pathname === '/bouts/demo' && method === 'POST') {
        return Promise.resolve({ ok: true, bout: launched })
      }

      if ((detail && method === 'GET') || (evaluate && method === 'POST')) {
        return Promise.resolve({ ok: true, bout: launched })
      }

      if (mergeCall && method === 'POST') {
        return Promise.reject(new Error('400: no contender has passed verification yet'))
      }

      return Promise.reject(new Error(`404: ${method} ${p}`))
    }

    if (pathname === '/bouts' && method === 'GET') {
      return Promise.resolve(list)
    }

    if (pathname === '/bouts/demo' && method === 'POST') {
      return Promise.resolve(fixture.demo)
    }

    if (detail && method === 'GET') {
      const want = idOf(detail)
      const found = list.bouts.find(b => b.bout_id === want)

      return found ? Promise.resolve({ ok: true, bout: found }) : Promise.reject(new Error(`404: Bout '${want}' not found`))
    }

    if (evaluate && method === 'POST') {
      return Promise.resolve(fixture.evaluate)
    }

    if (mergeCall && method === 'POST') {
      // Synthesised from the recorded bout: merge_winner() returns {ok, action, bout_id,
      // merged_contender_id, repo_path, backup_patch_path, bout} (worktree_engine.py).
      const body = opts.body || {}
      const cid = body.contender_id
      const action = body.action || 'apply'
      const isApply = action !== 'rollback'
      const base = list.bouts.find(b => b.bout_id === idOf(mergeCall)) || fx
      const next = {
        ...base,
        status: isApply ? 'merged' : 'evaluated',
        merged_contender_id: isApply ? cid : '',
        contenders: base.contenders.map(c => ({
          ...c,
          is_merged: isApply && c.contender_id === cid,
          status: isApply && c.contender_id === cid ? 'merged' : c.status
        }))
      }

      return Promise.resolve({ ok: true, action, bout_id: base.bout_id, merged_contender_id: cid, bout: next })
    }

    return Promise.reject(new Error(`404: ${method} ${p}`))
  }

  return {
    source: 'plugin:joust',
    register(c) {
      contributions.push(c)

      return () => disposed.push(c.id)
    },
    registerMany(cs) {
      cs.forEach(c => contributions.push(c))

      return () => cs.forEach(c => disposed.push(c.id))
    },
    registerSettingsPage() {
      return () => {}
    },
    onDispose() {},
    onEvent(type, listener) {
      const entry = { type, listener }
      events.push(entry)

      return () => {
        const index = events.indexOf(entry)

        if (index >= 0) {
          events.splice(index, 1)
        }
      }
    },
    setTimeout(fn, ms) {
      const id = setTimeout(fn, ms)

      return () => clearTimeout(id)
    },
    setInterval(fn, ms) {
      const id = setInterval(fn, ms)

      return () => clearInterval(id)
    },
    addEventListener(target, type, listener, options) {
      target.addEventListener(type, listener, options)

      return () => target.removeEventListener(type, listener, options)
    },
    rest,
    socket() {
      return () => {}
    },
    os: {
      notify() {},
      openExternal: async () => false,
      revealPath: async () => false,
      pickSavePath: async () => null,
      pickOpenPath: async () => null,
      writeClipboard: async () => false
    },
    pet: { say: () => () => {}, clear() {}, visible: { get: () => false, subscribe: () => () => {} } },
    runAction: () => ({ ok: false, error: 'stub' }),
    listActions: () => [],
    preview: { executeJavaScript: async () => ({ ok: false, error: 'stub' }) },
    storage: { get: (_key, fallback) => fallback, set() {}, remove() {} },
    files: {},
    i18n: { t: key => key, register() {} },
    _restLog: restLog,
    _events: events,
    _contributions: contributions,
    _disposed: disposed,
    _emit(type, payload) {
      for (const entry of [...events]) {
        if (entry.type === type || entry.type === '*') {
          entry.listener({ type, payload })
        }
      }
    }
  }
}

const contribMap = ctx => Object.fromEntries(ctx._contributions.map(c => [c.id, c]))

// ── [1] module shape + register(ctx) ────────────────────────────────────────

console.log('\n[1] module + register(ctx) against the SDK contracts')

const plugin = (await import(pluginUrl)).default
const ctx = makeCtx()

check('default export is a HermesPlugin { id: "joust", name, register }', () => {
  assert(plugin && typeof plugin === 'object', 'no default export')
  eq(plugin.id, 'joust', 'plugin.id (becomes the /api/plugins/<id> REST namespace; must equal the backend mount name)')
  assert(typeof plugin.register === 'function', 'register() missing')
  assert(typeof plugin.name === 'string' && plugin.name.length > 0, 'name missing')
})

check('register(ctx) does not throw', () => plugin.register(ctx))

const byId = contribMap(ctx)
const EXPECTED_AREAS = {
  'arena-chip': 'statusBar.right',
  'arena-nav': 'sidebar.nav',
  'arena-page': 'routes',
  'arena-pane': 'panes',
  'bout-directive': 'transcript.directives',
  'open-arena': 'palette'
}
const RESERVED_PATHS = new Set([
  '/', '/settings', '/command-center', '/session-import', '/capabilities', '/messaging', '/webhooks',
  '/artifacts', '/cron', '/profiles', '/agents', '/starmap'
])

check('exactly the 6 expected contributions (id -> area)', () => {
  const got = Object.fromEntries(ctx._contributions.map(c => [c.id, c.area]).sort())
  eq(JSON.stringify(got), JSON.stringify(EXPECTED_AREAS), 'id -> area map')
})

check('ids are plain slugs (the host namespaces them to joust:<id>) and nothing sets host-owned "source"', () => {
  for (const c of ctx._contributions) {
    assert(/^[a-z0-9][a-z0-9-]*$/.test(c.id), `bad id ${c.id}`)
    assert(!('source' in c), `${c.id} sets source`)
  }
})

check('routes: data.path "/joust" is absolute, one segment, not reserved, and render() is a function', () => {
  const c = byId['arena-page']
  eq(c.data?.path, '/joust', 'path')
  assert(/^\/[a-z0-9-]+$/.test(c.data.path), 'one segment')
  assert(!RESERVED_PATHS.has(c.data.path), 'reserved path')
  assert(typeof c.render === 'function', 'render')
})

warn(
  'routes: contribution carries a title (the page tab label; otherwise the Desktop humanises the path)',
  typeof byId['arena-page'].title === 'string',
  'arena-page has no title — app/chat/route-tile.tsx falls back to humanizePath("/joust")'
)

check('sidebar.nav: DATA contribution { path, label, codicon } pointing at the same route', () => {
  const c = byId['arena-nav']
  eq(c.data?.path, '/joust', 'path')
  assert(typeof c.data.label === 'string' && c.data.label, 'label')
  assert(typeof c.data.codicon === 'string' && /^[a-z0-9-]+$/.test(c.data.codicon), 'codicon')
  assert(c.render === undefined, 'sidebar.nav is data-only; a render would be ignored')
})

check('panes: title + data.placement (left|right|main|bottom|floating) + CSS width + render()', () => {
  const c = byId['arena-pane']
  assert(typeof c.title === 'string' && c.title, 'title')
  assert(['left', 'right', 'main', 'bottom', 'floating'].includes(c.data?.placement), 'placement')
  eq(c.data.placement, 'right', 'placement')
  assert(/^\d+(\.\d+)?(px|rem|em|vw|%)$/.test(c.data.width), 'width is a CSS length (track-model.ts PaneSizing)')
  assert(typeof c.render === 'function', 'render')
})

warn(
  'panes: right-sidebar pane declares collapsible:true like core Files/Review (leaves the grid on narrow viewports)',
  byId['arena-pane'].data.collapsible === true,
  'arena-pane sets no collapsible flag'
)

check('statusBar.right: finite order + render()', () => {
  const c = byId['arena-chip']
  assert(Number.isFinite(c.order), 'order')
  assert(typeof c.render === 'function', 'render')
})

check('transcript.directives: data.name is lowercase [a-z][a-z0-9-]* and data.render(props) is a function', () => {
  const d = byId['bout-directive'].data
  assert(/^[a-z][a-z0-9-]{0,63}$/.test(d?.name), 'name')
  eq(d.name, 'joust-bout', 'name')
  assert(typeof d.render === 'function', 'render')
})

check('transcript directive name matches what the backend emits, with quoted attrs (ATTR_RE needs key="value")', () => {
  const emitted = fx.directive
  assert(typeof emitted === 'string' && emitted.startsWith('::joust-bout{'), String(emitted))
  assert(/^::joust-bout\{id="[^"]+"\}$/.test(emitted), `unparseable directive: ${emitted}`)
})

check('palette: data { id, label, keywords[], run() }', () => {
  const d = byId['open-arena'].data
  assert(typeof d?.id === 'string' && d.id, 'id')
  assert(typeof d.label === 'string' && d.label, 'label')
  assert(Array.isArray(d.keywords) && d.keywords.length > 0, 'keywords')
  assert(typeof d.run === 'function', 'run')
})

check('palette run() navigates to /joust through host.navigate', () => {
  sdk.resetCalls()
  byId['open-arena'].data.run()
  eq(sdk.calls.navigate[0], '/joust', 'navigate target')
})

check('register() has no load-time side effects (no onEvent / rest before a component mounts)', () => {
  eq(ctx._events.length, 0, 'onEvent calls during register')
  eq(ctx._restLog.length, 0, 'rest calls during register')
})

// ── [2] renderToString of every render() ────────────────────────────────────

console.log('\n[2] renderToString of every render() — effects do NOT run; state seeded via react-shim')

const renders = {
  page: byId['arena-page'].render,
  pane: byId['arena-pane'].render,
  chip: byId['arena-chip'].render,
  directive: props => byId['bout-directive'].data.render(props)
}
const dprops = (over = {}) => ({ attrs: { id: fx.bout_id }, source: fx.directive, streaming: false, sessionId: 'sess-1', ...over })

function seeded(seeds, element) {
  shim.__seedState(seeds)

  try {
    const html = renderToString(element)
    eq(shim.__pendingSeeds(), 0, 'unconsumed state seeds (component hook order changed?)')

    return html
  } finally {
    shim.__seedState([])
  }
}

captureConsole()

check('page: initial render shows the empty state', () => {
  assert(renderToString(renders.page()).includes('No Joust bouts recorded yet'), 'empty state text')
})
check('pane: initial render shows "No active bouts"', () => assert(renderToString(renders.pane()).includes('No active bouts')))
check('chip: initial render shows "Joust (0)"', () => assert(renderToString(renders.chip()).includes('Joust (0)')))
check('directive: initial render shows the loading row with the bout id', () => {
  assert(renderToString(renders.directive(dprops())).includes('Loading Joust Arena bout ' + fx.bout_id), 'loading row')
})
check('directive: tolerates missing / empty attrs (model output is untrusted)', () => {
  renderToString(renders.directive({ attrs: undefined, source: '::joust-bout', streaming: true, sessionId: null }))
  renderToString(renders.directive({ attrs: {}, source: '::joust-bout{}', streaming: false, sessionId: null }))
})
check('directive: a hostile id attr is only ever text (escaped)', () => {
  const html = renderToString(renders.directive(dprops({ attrs: { id: '<script>alert(1)</script>' } })))
  assert(!html.includes('<script>'), 'raw <script> leaked')
  assert(html.includes('&lt;script&gt;'), 'expected escaped text')
})

check('page (fixture): leaderboard, inspector, coloured diff and benchmark output render', () => {
  const html = seeded([[fx], fx.bout_id, fx.contenders[0].contender_id, false], renders.page())
  const expected = [
    'Contender Leaderboard',
    'Worktree Inspector: ' + fx.contenders[0].name,
    'Merge Winner (' + fx.winner_id + ')',
    'Re-Judge All',
    'Unified Git Diff',
    'Verification &amp; Benchmark Output',
    'Tests Failed (exit 1)',
    'JOUST_BENCHMARK_MS',
    '🏆 WINNER',
    'text-emerald-400 bg-emerald-500/10',
    'text-rose-400 bg-rose-500/10'
  ]

  for (const s of expected) {
    assert(html.includes(s), 'missing: ' + s)
  }

  for (const c of fx.contenders) {
    assert(html.includes(c.name), 'missing contender ' + c.name)
  }
})

check('page (merged bout): Rollback button appears, Merge Winner disappears, MERGED badge shows', () => {
  const html = seeded([[merged], merged.bout_id, merged.contenders[0].contender_id, false], renders.page())
  assert(html.includes('Rollback Merge'), 'rollback button')
  assert(!html.includes('Merge Winner ('), 'merge button still shown')
  assert(html.includes('MERGED'), 'merged badge')
})

check('page (launched / un-evaluated bout): sparse values render without throwing', () => {
  const html = seeded([[launched], launched.bout_id, launched.contenders[0].contender_id, false], renders.page())

  for (const s of ['Tests Failed (exit -1)', 'C:0 S:0 M:0', 'Not evaluated yet.', 'No file changes recorded']) {
    assert(html.includes(s), 'missing: ' + s)
  }

  assert(!html.includes('Merge Winner ('), 'no winner yet, so no merge button')
})

check('page (busy): primary button reads "Running Arena..."', () => {
  assert(seeded([[fx], fx.bout_id, '', true], renders.page()).includes('Running Arena...'))
})

check('page (stale selection ids): falls back to the first bout / contender', () => {
  const html = seeded([[fx], 'bout-does-not-exist', 'nope', false], renders.page())
  assert(html.includes('Worktree Inspector: ' + fx.contenders[0].name), 'fallback contender')
})

check('page: user-controlled strings are escaped (title, goal, strategy, diff, test output)', () => {
  const html = seeded([[hostile], hostile.bout_id, hostile.contenders[0].contender_id, false], renders.page())
  // Only RAW tags count — the escaped text legitimately still contains the words onerror/onload.
  assert(!/<script|<img |<svg|<i>italic|<b>bold|<\/pre>\n/.test(html), 'raw markup leaked')
  assert(html.includes('&lt;script&gt;') && html.includes('&lt;img src=x onerror=alert(1)&gt;'), 'expected escaped markup')
})

check('pane (fixture list): latest bout title + ranked contenders with scores', () => {
  const html = seeded([fixture.list], renders.pane())
  assert(html.includes(esc(fx.title)), 'title')
  assert(html.includes('#1 ' + fx.contenders[0].name), 'rank line')
  assert(html.includes(fx.contenders[0].score.toFixed(1) + ' pts'), 'score')
})

check('pane (bout without contenders key): tolerated', () => {
  seeded([{ total_bouts: 1, bouts: [{ ...fx, contenders: undefined }] }], renders.pane())
})

check('chip (fixture): total count renders', () => {
  assert(seeded([fixture.list.total_bouts], renders.chip()).includes('Joust (' + fixture.list.total_bouts + ')'))
})

check('directive (fixture): card with contenders; Merge only for passed+unmerged; Steer for all', () => {
  const html = seeded([fx, false, ''], renders.directive(dprops()))
  assert(html.includes('⚔️ ' + esc(fx.title)), 'title')
  assert(html.includes(fx.bout_id), 'bout id chip')
  eq(count(html, '>Merge</button>'), fx.contenders.filter(c => c.tests_passed && !c.is_merged).length, 'Merge buttons')
  eq(count(html, '>Steer</button>'), fx.contenders.length, 'Steer buttons')
  eq(count(html, '>PASS<'), fx.contenders.filter(c => c.tests_passed).length, 'PASS chips')
  assert(html.includes('>FAILED<'), 'FAILED chip')
  assert(html.includes('🏆 WINNER'), 'winner badge')
  assert(html.includes('Status: ' + fx.status.toUpperCase()), 'status line')
})

check('directive (merged): ✓ MERGED badge and no Merge button for the merged contender', () => {
  const html = seeded([merged, false, ''], renders.directive(dprops()))
  assert(html.includes('✓ MERGED'), 'badge')
  eq(count(html, '>Merge</button>'), merged.contenders.filter(c => c.tests_passed && !c.is_merged).length, 'Merge buttons')
})

check('directive (launched / un-evaluated): READY chips and no Merge buttons', () => {
  const html = seeded([launched, false, ''], renders.directive(dprops()))
  assert(html.includes('>READY<'), 'READY chip')
  eq(count(html, '>Merge</button>'), 0, 'Merge buttons')
})

check('directive (busy): "Judging..." label with disabled button', () => {
  const html = seeded([fx, true, ''], renders.directive(dprops()))
  assert(html.includes('Judging...'), 'label')
  assert(/disabled=""/.test(html), 'disabled attribute')
})

check('directive (error state): inline error row with the message', () => {
  const html = seeded([null, false, 'Hermes desktop bridge unavailable'], renders.directive(dprops()))
  assert(html.includes('Joust Bout ' + fx.bout_id + ': Hermes desktop bridge unavailable'), 'error row')
})

check('directive: hostile bout fields are escaped', () => {
  const html = seeded([hostile, false, ''], renders.directive(dprops()))
  assert(!/<script|<img|<b>bold|<i>italic/.test(html), 'raw markup leaked')
})

restoreConsole()

check('no React key / prop warnings during server renders', () => {
  const bad = consoleLines.filter(l => REACT_WARNING_RE.test(l))
  eq(bad.length, 0, 'warnings: ' + bad.slice(0, 3).join(' || '))
})

// ── [3] DOM mount: effects run, buttons clicked, events fired ───────────────

console.log('\n[3] DOM mount with happy-dom — effects run, buttons clicked, events fired')

const domStart = consoleLines.length
const happyDomInstalled = fs.existsSync(path.join(nodeModulesDir, 'happy-dom', 'package.json'))
let demoListFetches = 0

if (!wantDom || !happyDomInstalled) {
  const why = !wantDom ? 'JOUST_SMOKE_NO_DOM=1' : `happy-dom not installed under ${nodeModulesDir}`
  results.skip.push(`DOM tier (${why})`)
  console.log(`  skip DOM tier — ${why}`)
} else {
  const { Window } = await import('happy-dom')
  const win = new Window({ url: 'http://localhost/' })
  const define = (key, value) => Object.defineProperty(globalThis, key, { value, configurable: true, writable: true })

  define('window', win)
  define('document', win.document)
  define('navigator', win.navigator)

  for (const key of [
    'HTMLElement', 'Element', 'Node', 'Event', 'MouseEvent', 'CustomEvent', 'Text', 'Comment', 'DocumentFragment',
    'HTMLIFrameElement', 'HTMLInputElement', 'HTMLSelectElement', 'HTMLButtonElement', 'getComputedStyle',
    'requestAnimationFrame', 'cancelAnimationFrame', 'MutationObserver'
  ]) {
    if (key in win && !(key in globalThis)) {
      define(key, win[key])
    }
  }

  globalThis.IS_REACT_ACT_ENVIRONMENT = true

  const { createRoot } = await import('react-dom/client')
  const { act } = React

  const flush = async () => {
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 0))
    })
  }

  async function mount(element) {
    const container = win.document.createElement('div')
    win.document.body.appendChild(container)
    const root = createRoot(container)

    await act(async () => {
      root.render(element)
    })
    await flush()

    return {
      container,
      text: () => container.textContent,
      unmount: async () => {
        await act(async () => {
          root.unmount()
        })
        container.remove()
      }
    }
  }

  const click = async node => {
    assert(node, 'node to click is missing')
    await act(async () => {
      node.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }))
    })
    await flush()
  }

  const buttons = container => [...container.querySelectorAll('button')]
  const buttonWith = (container, text) => buttons(container).find(b => b.textContent.includes(text))
  const changeSelect = async (select, value) => {
    await act(async () => {
      select.value = value
      select.dispatchEvent(new win.Event('change', { bubbles: true }))
    })
    await flush()
  }

  captureConsole()

  const twoBoutList = {
    ...fixture.list,
    bouts: [fx, launched],
    total_bouts: 2,
    total_contenders: fx.contenders.length + launched.contenders.length
  }

  await checkAsync(
    'page (fixture): GET /bouts on mount; select + card switch; Re-Judge All -> POST evaluate (needs_agent) + composer prompt + info toast; Merge Winner -> POST merge + success toast; event -> refetch; unmount disposes the listener',
    async () => {
      const c = makeCtx({ list: twoBoutList })
      plugin.register(c)
      const b = contribMap(c)
      sdk.resetCalls()

      const m = await mount(b['arena-page'].render())
      eq(c._restLog[0]?.method + ' ' + c._restLog[0]?.path, 'GET /bouts', 'first REST call')
      assert(m.text().includes('Contender Leaderboard'), 'leaderboard not rendered: ' + m.text().slice(0, 160))
      eq(c._events.length, 1, 'onEvent subscriptions')
      eq(c._events[0].type, 'plugin.joust.bout.updated', 'event type (plugin_events.py: plugin.<id>.<event>)')

      const select = m.container.querySelector('select')
      eq(m.container.querySelectorAll('select option').length, 2, 'bout <option>s')
      await changeSelect(select, launched.bout_id)
      assert(m.text().includes('Worktree Inspector: ' + launched.contenders[0].name), 'select change did not switch bout')
      await changeSelect(select, fx.bout_id)

      const cards = [...m.container.querySelectorAll('.cursor-pointer')]
      eq(cards.length, fx.contenders.length, 'leaderboard cards')
      await click(cards[1])
      assert(m.text().includes('Worktree Inspector: ' + fx.contenders[1].name), 'card click did not select contender')

      const before = c._restLog.length
      await click(buttonWith(m.container, 'Re-Judge All'))
      const evaluateCall = c._restLog.slice(before).find(r => r.method === 'POST' && r.path === '/bouts/' + encodeURIComponent(fx.bout_id) + '/evaluate')
      assert(evaluateCall, 'no POST /bouts/<id>/evaluate')
      assert(sdk.calls.notify.some(n => n.kind === 'info' && /Evaluated|re-judge/i.test(n.message)), 'no info toast: ' + JSON.stringify(sdk.calls.notify))
      assert(sdk.calls.submit.some(sub => /joust_evaluate/.test(sub.text)), 'judge request was not handed to the agent via the composer')

      await click(buttonWith(m.container, 'Merge Winner ('))
      const mergeCall = c._restLog.find(r => r.method === 'POST' && r.path.endsWith('/merge'))
      assert(mergeCall, 'no POST /bouts/<id>/merge')
      eq(mergeCall.body?.contender_id, fx.winner_id, 'merge contender_id')
      eq(mergeCall.body?.action, 'apply', 'merge action')
      assert(sdk.calls.notify.some(n => n.kind === 'success'), 'no success toast')

      const n = c._restLog.length
      c._emit('plugin.joust.bout.updated', { bout_id: fx.bout_id })
      await flush()
      assert(c._restLog.length > n && c._restLog[c._restLog.length - 1].path === '/bouts', 'bout.updated event did not refetch')

      await m.unmount()
      eq(c._events.length, 0, 'onEvent listener not disposed on unmount')
    }
  )

  await checkAsync('page (empty): empty state; Run Demo -> POST /bouts/demo, success toast, refetch', async () => {
    const c = makeCtx({ mode: 'empty' })
    plugin.register(c)
    const b = contribMap(c)
    sdk.resetCalls()

    const m = await mount(b['arena-page'].render())
    assert(m.text().includes('No Joust bouts recorded yet'), 'empty state')
    await click(buttonWith(m.container, 'Run Live 3-Way Demo Tournament'))
    assert(c._restLog.some(r => r.method === 'POST' && r.path === '/bouts/demo'), 'no POST /bouts/demo')
    assert(sdk.calls.notify.some(n => n.kind === 'success'), 'no success toast')
    demoListFetches = c._restLog.filter(r => r.method === 'GET' && r.path === '/bouts').length
    assert(demoListFetches >= 2, 'GET /bouts (mount + refresh)')
    await m.unmount()
  })

  warn(
    'page: one demo click costs exactly 2 GET /bouts (mount + explicit refresh) — no effect churn',
    demoListFetches === 2,
    `${demoListFetches} GET /bouts — refresh() is a useCallback keyed on selectedBoutId, so setSelectedBoutId() re-creates it, ` +
      'the useEffect([ctx, refresh]) re-runs (extra fetch) and the onEvent listener is torn down and re-subscribed on every selection change'
  )

  await checkAsync('page (ctx.rest rejects): mounts without crashing; demo click surfaces an error toast', async () => {
    const c = makeCtx({ mode: 'reject' })
    plugin.register(c)
    const b = contribMap(c)
    sdk.resetCalls()

    const m = await mount(b['arena-page'].render())
    assert(m.text().includes('No Joust bouts recorded yet'), 'fallback state')
    await click(buttonWith(m.container, 'Run Live 3-Way Demo Tournament'))
    assert(sdk.calls.notify.some(n => n.kind === 'error' && /bridge unavailable/.test(n.message)), 'no error toast: ' + JSON.stringify(sdk.calls.notify))
    await m.unmount()
  })

  await checkAsync(
    'directive (fixture): GET /bouts/<id>; Re-Judge -> POST evaluate; Merge -> POST merge + MERGED badge; Steer -> composer.submit(sessionId, text) or fallback toast; event filtered by bout_id; unmount disposes',
    async () => {
      const c = makeCtx()
      plugin.register(c)
      const b = contribMap(c)
      sdk.resetCalls()

      const m = await mount(b['bout-directive'].data.render(dprops()))
      eq(c._restLog[0]?.path, '/bouts/' + encodeURIComponent(fx.bout_id), 'detail GET')
      assert(m.text().includes(fx.contenders[0].name), 'contenders rendered: ' + m.text().slice(0, 160))

      await click(buttonWith(m.container, 'Re-Judge'))
      assert(c._restLog.some(r => r.method === 'POST' && r.path.endsWith('/evaluate')), 'no POST evaluate')
      assert(sdk.calls.notify.some(n => n.kind === 'info'), 'no info toast')

      const mergeButtons = buttons(m.container).filter(x => x.textContent.trim() === 'Merge')
      eq(mergeButtons.length, fx.contenders.filter(x => x.tests_passed && !x.is_merged).length, 'Merge buttons')
      await click(mergeButtons[0])
      const mergeCall = c._restLog.find(r => r.method === 'POST' && r.path.endsWith('/merge'))
      assert(mergeCall, 'no POST merge')
      eq(mergeCall.body?.action, 'apply', 'merge action')
      assert(sdk.calls.notify.some(n => n.kind === 'success'), 'no success toast')
      assert(m.text().includes('✓ MERGED'), 'MERGED badge after the merge response')

      sdk.resetCalls()
      sdk.behaviour.submitResult = true
      const steerButtons = buttons(m.container).filter(x => x.textContent.trim() === 'Steer')
      eq(steerButtons.length, fx.contenders.length, 'Steer buttons')
      await click(steerButtons[0])
      eq(sdk.calls.submit.length, 1, 'composer.submit calls')
      eq(sdk.calls.submit[0].sessionId, 'sess-1', 'sessionId forwarded from TranscriptDirectiveProps')
      assert(sdk.calls.submit[0].text.includes(fx.bout_id) && sdk.calls.submit[0].text.includes(fx.contenders[0].contender_id), 'steer prompt text')
      eq(sdk.calls.notify.length, 0, 'no toast when the composer accepted')

      sdk.resetCalls()
      sdk.behaviour.submitResult = false
      await click(steerButtons[1])
      assert(sdk.calls.notify.some(n => n.kind === 'info' && /Open chat composer/.test(n.message)), 'no fallback toast when composer refused')
      sdk.behaviour.submitResult = true

      let n = c._restLog.length
      c._emit('plugin.joust.bout.updated', { bout_id: 'bout-other' })
      await flush()
      eq(c._restLog.length, n, 'a foreign bout_id must not refetch')
      c._emit('plugin.joust.bout.updated', { bout_id: fx.bout_id })
      await flush()
      assert(c._restLog.length > n, 'matching bout_id did not refetch')
      n = c._restLog.length
      c._emit('plugin.joust.bout.updated', {})
      await flush()
      assert(c._restLog.length > n, 'payload without bout_id is treated as a wildcard refetch')

      await m.unmount()
      eq(c._events.length, 0, 'listener disposed')
    }
  )

  await checkAsync('directive (ctx.rest rejects): inline error row', async () => {
    const c = makeCtx({ mode: 'reject' })
    plugin.register(c)
    const m = await mount(contribMap(c)['bout-directive'].data.render(dprops()))
    assert(m.text().includes('Joust Bout ' + fx.bout_id + ': Hermes desktop bridge unavailable'), m.text())
    await m.unmount()
  })

  await checkAsync('directive (unknown bout id): a 404 rejection becomes the inline error row', async () => {
    const c = makeCtx()
    plugin.register(c)
    const m = await mount(contribMap(c)['bout-directive'].data.render(dprops({ attrs: { id: 'bout-missing' } })))
    assert(m.text().includes("Joust Bout bout-missing: 404: Bout 'bout-missing' not found"), m.text())
    await m.unmount()
  })

  await checkAsync('directive (launched / un-evaluated bout): READY chips, no Merge buttons', async () => {
    const c = makeCtx({ mode: 'empty' })
    plugin.register(c)
    const m = await mount(contribMap(c)['bout-directive'].data.render(dprops({ attrs: { id: launched.bout_id } })))
    assert(m.text().includes('READY'), m.text())
    eq(buttons(m.container).filter(x => x.textContent.trim() === 'Merge').length, 0, 'Merge buttons')
    await m.unmount()
  })

  {
    const c = makeCtx()
    plugin.register(c)
    let m = null

    try {
      m = await mount(contribMap(c)['bout-directive'].data.render(dprops({ attrs: {} })))
      warn(
        'directive without an id attr shows an error instead of a perpetual "Loading…" row',
        !m.text().includes('Loading Joust Arena bout'),
        `rendered: "${m.text().trim()}" and made ${c._restLog.length} REST calls`
      )
    } catch (error) {
      fail('directive without an id attr mounts', error)
    } finally {
      if (m) {
        await m.unmount()
      }
    }
  }

  await checkAsync('pane (fixture): GET /bouts?limit=5; latest bout rendered; "Full Studio" navigates; event refetches; unmount disposes', async () => {
    const c = makeCtx()
    plugin.register(c)
    sdk.resetCalls()
    const m = await mount(contribMap(c)['arena-pane'].render())
    eq(c._restLog[0]?.path, '/bouts?limit=5', 'REST path')
    assert(m.text().includes(fx.title), 'latest bout title')
    assert(m.text().includes('#1 ' + fx.contenders[0].name), 'ranked rows')
    await click(buttonWith(m.container, 'Full Studio'))
    eq(sdk.calls.navigate[0], '/joust', 'navigate target')
    const n = c._restLog.length
    c._emit('plugin.joust.bout.updated', { bout_id: fx.bout_id })
    await flush()
    assert(c._restLog.length > n, 'event did not refetch')
    await m.unmount()
    eq(c._events.length, 0, 'listener disposed')
  })

  await checkAsync('pane (empty + reject): both render the empty row', async () => {
    for (const mode of ['empty', 'reject']) {
      const c = makeCtx({ mode })
      plugin.register(c)
      const m = await mount(contribMap(c)['arena-pane'].render())
      assert(m.text().includes('No active bouts'), mode + ': ' + m.text())
      await m.unmount()
    }
  })

  await checkAsync('chip (fixture): GET /bouts?limit=10 -> "Joust (<total>)"; click navigates; event refetches; unmount disposes', async () => {
    const c = makeCtx()
    plugin.register(c)
    sdk.resetCalls()
    const m = await mount(contribMap(c)['arena-chip'].render())
    eq(c._restLog[0]?.path, '/bouts?limit=10', 'REST path')
    assert(m.text().includes('Joust (' + fixture.list.total_bouts + ')'), m.text())
    await click(m.container.querySelector('button'))
    eq(sdk.calls.navigate[0], '/joust', 'navigate target')
    const n = c._restLog.length
    c._emit('plugin.joust.bout.updated', { bout_id: fx.bout_id })
    await flush()
    assert(c._restLog.length > n, 'event did not refetch')
    await m.unmount()
    eq(c._events.length, 0, 'listener disposed')
  })

  await checkAsync('chip (reject): stays at "Joust (0)" without throwing', async () => {
    const c = makeCtx({ mode: 'reject' })
    plugin.register(c)
    const m = await mount(contribMap(c)['arena-chip'].render())
    assert(m.text().includes('Joust (0)'), m.text())
    await m.unmount()
  })

  restoreConsole()

  check('no React warnings during the DOM scenarios (keys, act(), updates on unmounted components)', () => {
    const bad = consoleLines.slice(domStart).filter(l => REACT_WARNING_RE.test(l))
    eq(bad.length, 0, 'warnings: ' + bad.slice(0, 3).join(' || '))
  })

  try {
    await win.happyDOM.close()
  } catch {
    // best effort
  }
}

// ── [4] static checks on the source ─────────────────────────────────────────

console.log('\n[4] static checks on desktop/plugin.js')

const lines = source.split('\n')

check('no dangerouslySetInnerHTML / innerHTML / eval / new Function / document.write', () => {
  assert(!/dangerouslySetInnerHTML|\.innerHTML|\beval\(|new Function\(|document\.write/.test(source), 'forbidden DOM/eval API')
})

check('imports limited to @hermes/plugin-sdk and react* (sdk/runtime.ts import map)', () => {
  const specs = [...source.matchAll(/^\s*import\s[^'"]*['"]([^'"]+)['"]/gm)].map(m => m[1])
  assert(specs.length >= 1, 'no imports found')

  for (const spec of specs) {
    assert(['@hermes/plugin-sdk', 'react', 'react/jsx-runtime', 'react/jsx-dev-runtime'].includes(spec), 'disallowed import ' + spec)
  }
})

check('hooks are called only at component top level, before any early return / branch', () => {
  const hookRe = /\buse(State|Effect|Callback|Memo|Ref|Context|Reducer|LayoutEffect)\s*\(/
  let component = null
  let sawBranch = false

  lines.forEach((line, i) => {
    const start = line.match(/^function ([A-Z]\w*)\(/)

    if (start) {
      component = start[1]
      sawBranch = false

      return
    }

    if (/^}/.test(line)) {
      component = null

      return
    }

    if (component && /^ {2}(if \(|return\b|for \(|while \(|switch \()/.test(line)) {
      sawBranch = true
    }

    if (hookRe.test(line)) {
      assert(component, `line ${i + 1}: hook outside a component function`)
      assert(/^ {2}\S/.test(line), `line ${i + 1}: hook not at the component's top indentation level`)
      assert(!sawBranch, `line ${i + 1}: hook after an early return / branch in ${component}`)
    }
  })
})

check('every .map() list render supplies a key', () => {
  const maps = (source.match(/\.map\(/g) || []).length
  const keyed = (source.match(/\}, (idx|c\.contender_id|b\.bout_id|ch\.key)\)/g) || []).length
  assert(maps > 0, 'no .map( calls found')
  eq(keyed, maps, 'keyed element closers vs .map( calls')
})

check('Tailwind CSS-variable shorthand matches core (text-(--var), never text-[var(--var)])', () => {
  assert(!/\[var\(--ui-/.test(source), 'bracket form used')
})

// Every `--ui-*` custom property defined in apps/desktop/src/styles.css (Hermes 0.23.0).
const CORE_UI_VARS = new Set([
  '--ui-accent', '--ui-accent-secondary', '--ui-base', '--ui-bg-card', '--ui-bg-chrome', '--ui-bg-editor',
  '--ui-bg-elevated', '--ui-bg-input', '--ui-bg-primary', '--ui-bg-quaternary', '--ui-bg-quinary', '--ui-bg-secondary',
  '--ui-bg-sidebar', '--ui-bg-tertiary', '--ui-blue', '--ui-chat-bubble-background', '--ui-chat-bubble-opaque-background',
  '--ui-chat-surface-background', '--ui-control-active-background', '--ui-control-hover-background', '--ui-cyan',
  '--ui-diff-add-background', '--ui-diff-add-border', '--ui-diff-add-foreground', '--ui-diff-remove-background',
  '--ui-diff-remove-border', '--ui-diff-remove-foreground', '--ui-editor-surface-background', '--ui-green',
  '--ui-inline-code-background', '--ui-inline-code-foreground', '--ui-orange', '--ui-purple', '--ui-red',
  '--ui-row-active-background', '--ui-row-hover-background', '--ui-row-open-background', '--ui-sash-hover-background',
  '--ui-sash-hover-border', '--ui-selection-background', '--ui-sidebar-surface-background', '--ui-stroke-primary',
  '--ui-stroke-quaternary', '--ui-stroke-secondary', '--ui-stroke-tertiary', '--ui-surface-background',
  '--ui-tab-hover-darken', '--ui-terminal-surface-background', '--ui-text-primary', '--ui-text-quaternary',
  '--ui-text-secondary', '--ui-text-tertiary', '--ui-warm', '--ui-widget-surface-background', '--ui-yellow'
])

const usedVars = [...new Set([...source.matchAll(/\((--ui-[a-z0-9-]+)(?:,[^)]*)?\)/g)].map(m => m[1]))]
const undefinedVars = usedVars.filter(v => !CORE_UI_VARS.has(v))

warn(
  'every (--ui-*) token used in class names is defined by the Desktop stylesheet',
  undefinedVars.length === 0,
  `not defined in apps/desktop/src/styles.css (0.23.0): ${undefinedVars.join(', ')} — e.g. bg-(--ui-surface-secondary) compiles to ` +
    'background-color: var(--ui-surface-secondary) and paints nothing. Use bg-(--ui-bg-quaternary) / bg-(--ui-bg-quinary) / ' +
    'bg-(--ui-control-hover-background), or add a fallback: bg-(--ui-surface-secondary,transparent).'
)

// ── summary ─────────────────────────────────────────────────────────────────

console.log(
  `\n[desktop-smoke] ${results.pass.length} passed, ${results.fail.length} failed, ${results.warn.length} warnings, ${results.skip.length} skipped`
)
results.skip.forEach(s => console.log('  skip ' + s))
results.warn.forEach(w => console.log('  WARN ' + w))
results.fail.forEach(f => console.log('  FAIL ' + f))

process.exit(results.fail.length ? 1 : 0)
