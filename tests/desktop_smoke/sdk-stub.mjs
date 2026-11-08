// Stand-in for '@hermes/plugin-sdk' — ONLY the surface desktop/plugin.js touches.
//
// Area constants mirror Hermes 0.23.0: apps/desktop/src/sdk/areas.ts (PANES_AREA,
// STATUSBAR_AREAS, TITLEBAR_AREAS), app/routes.ts (ROUTES_AREA, SIDEBAR_NAV_AREA),
// lib/transcript-directives.ts (TRANSCRIPT_DIRECTIVE_AREA) and
// app/command-palette/contrib.ts (PALETTE_AREA). Host verbs record their calls so the
// harness can assert on them; shapes follow sdk/index.ts (`host.notify(NotificationInput)`,
// `host.navigate(path)`) and sdk/composer.ts (`composer.submit(sessionId, text): boolean`).

export const PANES_AREA = 'panes'
export const STATUSBAR_AREAS = Object.freeze({ left: 'statusBar.left', right: 'statusBar.right' })
export const TITLEBAR_AREAS = Object.freeze({ center: 'titleBar.center', left: 'titleBar.left', right: 'titleBar.right' })
export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const TRANSCRIPT_DIRECTIVE_AREA = 'transcript.directives'
export const PALETTE_AREA = 'palette'

/** Recorded host calls, reset with `resetCalls()`. */
export const calls = { notify: [], navigate: [], submit: [] }

/** Knobs the harness flips between scenarios. */
export const behaviour = { submitResult: true }

export function resetCalls() {
  calls.notify.length = 0
  calls.navigate.length = 0
  calls.submit.length = 0
}

export const host = {
  notify(input) {
    calls.notify.push(input)
    return `notification-${calls.notify.length}`
  },
  notifyError(message, error) {
    calls.notify.push({ kind: 'error', message, error })
  },
  navigate(path) {
    calls.navigate.push(path)
  },
  composer: {
    submit(sessionId, text) {
      calls.submit.push({ sessionId, text })
      return behaviour.submitResult
    },
    insertText: async () => true,
    setDraft: async () => true,
    getDraft: async () => null,
    focus() {}
  },
  state: {}
}
