// The plugin's `import ... from 'react'` is routed here by loader-hooks.mjs (the harness and
// react-dom keep the REAL module, and this file re-exports that same instance, so hooks share
// one dispatcher). The only difference: `useState` can be pre-seeded. `renderToString` never
// runs effects, so without seeding the server render could only ever reach each component's
// initial "loading / empty" branch; seeding the initial state lets the harness render the
// loaded, merged, sparse, busy and error branches too.

import React from 'react'

export * from 'react'
export default React

const NO_SEED = Symbol('no-seed')
let queue = []

/** Values consumed, in order, by the next `useState` calls (one render). */
export function __seedState(values) {
  queue = [...values]
}

/** How many seeds were not consumed — non-zero means the component's hook order changed. */
export function __pendingSeeds() {
  return queue.length
}

export function useState(initial) {
  const seed = queue.length ? queue.shift() : NO_SEED

  return React.useState(seed === NO_SEED ? initial : seed)
}
