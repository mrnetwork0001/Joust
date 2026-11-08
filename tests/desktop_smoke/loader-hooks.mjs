// Node module-customization hooks, used by smoke.mjs through `module.registerHooks()` (sync,
// in-thread; Node >= 23.5 / 22.15) or, on older Node, `module.register()` (async, hooks thread).
//
// They do for Node what apps/desktop/src/contrib/runtime-loader.ts does for the Desktop:
//   * `@hermes/plugin-sdk`                    -> sdk-stub.mjs (the recording SDK stand-in)
//   * `react` (from the plugin file only)     -> react-shim.mjs (real React + seedable useState)
//   * `react*`, `react-dom*`, `scheduler`, `happy-dom` -> resolved from the scratch node_modules
//     (JOUST_SMOKE_NODE_MODULES), never from the repo
//   * anything else imported BY THE PLUGIN is refused, mirroring the runtime loader's
//     "runtime plugins may only import @hermes/plugin-sdk and react" rule
//     (sdk/runtime.ts maps exactly: @hermes/plugin-sdk, react, react/jsx-runtime, react/jsx-dev-runtime).

const PLUGIN_ALLOWLIST = new Set(['@hermes/plugin-sdk', 'react', 'react/jsx-runtime', 'react/jsx-dev-runtime'])
const SCRATCH_PACKAGES = /^(react|react-dom|scheduler|happy-dom)(\/|$)/

/** Build the hooks object for `module.registerHooks(createHooks(config))`. Synchronous on purpose. */
export function createHooks({ sdkStubUrl, reactShimUrl, anchorUrl, pluginUrl }) {
  return {
    resolve(specifier, context, nextResolve) {
      const fromPlugin = context.parentURL === pluginUrl

      if (fromPlugin && !PLUGIN_ALLOWLIST.has(specifier)) {
        throw new Error(
          `unsupported import: ${specifier} — runtime plugins may only import @hermes/plugin-sdk and react ` +
            '(apps/desktop/src/contrib/runtime-loader.ts)'
        )
      }

      if (specifier === '@hermes/plugin-sdk') {
        return { url: sdkStubUrl, shortCircuit: true }
      }

      if (fromPlugin && specifier === 'react') {
        return { url: reactShimUrl, shortCircuit: true }
      }

      if (SCRATCH_PACKAGES.test(specifier)) {
        // Resolve as if imported from a file beside the scratch node_modules folder.
        return nextResolve(specifier, { ...context, parentURL: anchorUrl })
      }

      return nextResolve(specifier, context)
    }
  }
}

// ── `module.register()` protocol (older Node): the hooks thread calls initialize(data) first.

let active = null

export async function initialize(data) {
  active = createHooks(data)
}

export function resolve(specifier, context, nextResolve) {
  return active ? active.resolve(specifier, context, nextResolve) : nextResolve(specifier, context)
}
