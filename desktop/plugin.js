// Joust — Hermes Desktop Plugin (@hermes/plugin-sdk)
// Pure uncompiled ESM using react/jsx-runtime (stays strictly inside the Desktop SDK surface)

import * as sdk from '@hermes/plugin-sdk'
import { useState, useEffect, useCallback } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

function notifyUser(kind, message) {
  if (sdk.host && typeof sdk.host.notify === 'function') {
    sdk.host.notify({ kind, message })
  }
}

// Small status chips shared by the inline card and the arena page.
function signalChips(c) {
  const chips = []
  if (c.model) chips.push({ key: 'model', text: c.model, cls: 'bg-(--ui-bg-tertiary) text-(--ui-text-tertiary)' })
  if (c.flaky) chips.push({ key: 'flaky', text: '⚠ FLAKY', cls: 'bg-amber-500/15 text-amber-300' })
  const runs = Array.isArray(c.runs) ? c.runs : []
  if (runs.length > 1) {
    const ok = runs.filter(r => r && r.exit_code === 0).length
    chips.push({ key: 'runs', text: '🛡 ' + ok + '/' + runs.length + ' runs', cls: ok === runs.length ? 'bg-emerald-500/10 text-emerald-300' : 'bg-rose-500/10 text-rose-300' })
  }
  if (c.crossfire_total > 0) {
    const clean = c.crossfire_survived === c.crossfire_total
    chips.push({ key: 'xfire', text: '🎯 ' + c.crossfire_survived + '/' + c.crossfire_total + ' rivals', cls: clean ? 'bg-emerald-500/10 text-emerald-300' : 'bg-rose-500/10 text-rose-300' })
  }
  if (c.branch_ref) chips.push({ key: 'branch', text: '⎇ ' + c.branch_ref, cls: 'bg-(--ui-bg-tertiary) text-(--ui-text-secondary)' })
  return chips.map(ch => {
    return jsx('span', {
      className: 'rounded px-1.5 py-0.5 font-mono text-[10px] ' + ch.cls,
      title: ch.key === 'xfire' ? 'Crossfire: rivals\' test volleys survived' : ch.key === 'runs' ? 'Flake Shield: passing runs' : undefined,
      children: ch.text
    }, ch.key)
  })
}

function navigateTo(path) {
  if (sdk.host && typeof sdk.host.navigate === 'function') {
    sdk.host.navigate(path)
  }
}

function submitToComposer(sessionId, promptText) {
  if (sdk.host && sdk.host.composer && typeof sdk.host.composer.submit === 'function') {
    return sdk.host.composer.submit(sessionId ?? null, promptText)
  }
  return false
}

function DiffView({ diffText }) {
  if (!diffText || !diffText.trim()) {
    return jsx('div', {
      className: 'p-3 text-xs text-(--ui-text-tertiary) italic',
      children: 'No file changes recorded in this worktree yet.'
    })
  }
  const lines = diffText.split('\n')
  return jsx('pre', {
    className: 'overflow-x-auto rounded border border-(--ui-stroke-primary) bg-(--ui-surface-secondary) p-3 font-mono text-[11px] leading-relaxed',
    children: lines.map((line, idx) => {
      let colorClass = 'text-(--ui-text-secondary)'
      if (line.startsWith('+') && !line.startsWith('+++')) {
        colorClass = 'text-emerald-400 bg-emerald-500/10'
      } else if (line.startsWith('-') && !line.startsWith('---')) {
        colorClass = 'text-rose-400 bg-rose-500/10'
      } else if (line.startsWith('@@') || line.startsWith('diff --git')) {
        colorClass = 'text-amber-300 font-semibold'
      }
      return jsx('div', {
        className: 'px-1 whitespace-pre ' + colorClass,
        children: line || ' '
      }, idx)
    })
  })
}

function InlineBoutCard({ ctx, boutId, sessionId }) {
  const [bout, setBout] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const loadBout = useCallback(() => {
    if (!boutId) return
    ctx.rest('/bouts/' + encodeURIComponent(boutId))
      .then(res => {
        if (res && res.bout) setBout(res.bout)
      })
      .catch(err => setError(String(err?.message || err || 'Could not load bout')))
  }, [ctx, boutId])

  useEffect(() => {
    loadBout()
    const stop = ctx.onEvent('plugin.joust.bout.updated', evt => {
      if (!evt?.payload?.bout_id || evt.payload.bout_id === boutId) {
        loadBout()
      }
    })
    return () => {
      if (typeof stop === 'function') stop()
    }
  }, [ctx, boutId, loadBout])

  const handleEvaluate = () => {
    setBusy(true)
    ctx.rest('/bouts/' + encodeURIComponent(boutId) + '/evaluate', { method: 'POST', body: {} })
      .then(res => {
        if (res && res.bout) setBout(res.bout)
        notifyUser('info', 'Joust benchmark judge completed!')
      })
      .catch(err => notifyUser('error', String(err?.message || err)))
      .finally(() => setBusy(false))
  }

  const handleMerge = (contenderId) => {
    setBusy(true)
    ctx.rest('/bouts/' + encodeURIComponent(boutId) + '/merge', {
      method: 'POST',
      body: { contender_id: contenderId, action: 'apply' }
    })
      .then(res => {
        if (res && res.bout) setBout(res.bout)
        notifyUser('success', 'Merged contender ' + contenderId + ' into repository!')
      })
      .catch(err => notifyUser('error', String(err?.message || err)))
      .finally(() => setBusy(false))
  }

  if (error) {
    return jsx('div', {
      className: 'my-2 rounded-lg border border-(--ui-stroke-primary) bg-(--ui-surface-secondary) p-3 text-xs text-(--ui-text-secondary)',
      children: 'Joust Bout ' + boutId + ': ' + error
    })
  }

  if (!bout) {
    return jsx('div', {
      className: 'my-2 rounded-lg border border-(--ui-stroke-primary) bg-(--ui-surface-secondary) p-3 text-xs text-(--ui-text-tertiary)',
      children: 'Loading Joust Arena bout ' + boutId + '...'
    })
  }

  const contenders = bout.contenders || []
  return jsxs('div', {
    className: 'my-2 flex flex-col gap-2.5 rounded-xl border border-(--ui-stroke-primary) bg-(--ui-surface-secondary) p-3.5 text-xs',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-2 border-b border-(--ui-stroke-primary) pb-2',
        children: [
          jsxs('div', {
            className: 'flex flex-col',
            children: [
              jsxs('div', {
                className: 'flex items-center gap-2 font-semibold text-(--ui-text-primary)',
                children: [
                  jsx('span', { children: '⚔️ ' + bout.title }),
                  jsx('span', {
                    className: 'rounded bg-(--ui-surface-tertiary) px-1.5 py-0.5 font-mono text-[10px] text-(--ui-text-secondary)',
                    children: bout.bout_id
                  })
                ]
              }),
              jsx('div', {
                className: 'text-[11px] text-(--ui-text-tertiary)',
                children: 'Verify: ' + bout.test_command + ' • Status: ' + bout.status.toUpperCase()
              })
            ]
          }),
          jsxs('div', {
            className: 'flex items-center gap-1.5',
            children: [
              jsx('button', {
                type: 'button',
                disabled: busy,
                onClick: handleEvaluate,
                className: 'rounded border border-(--ui-stroke-primary) bg-(--ui-surface-primary) px-2 py-1 text-[11px] font-medium text-(--ui-text-primary) hover:opacity-90',
                children: busy ? 'Judging...' : '🏁 Re-Judge'
              }),
              jsx('button', {
                type: 'button',
                onClick: () => navigateTo('/joust'),
                className: 'rounded border border-(--ui-stroke-primary) bg-(--ui-surface-primary) px-2 py-1 text-[11px] font-medium text-(--ui-text-secondary) hover:text-(--ui-text-primary)',
                children: 'Arena Studio →'
              })
            ]
          })
        ]
      }),
      jsx('div', {
        className: 'flex flex-col gap-1.5',
        children: contenders.map(c =>
          jsxs('div', {
            className: 'flex items-center justify-between gap-2 rounded-lg border border-(--ui-stroke-primary) bg-(--ui-surface-primary) px-3 py-2',
            children: [
              jsxs('div', {
                className: 'flex flex-col gap-0.5',
                children: [
                  jsxs('div', {
                    className: 'flex items-center gap-1.5 font-medium text-(--ui-text-primary)',
                    children: [
                      jsx('span', {
                        className: 'font-mono text-[11px] text-(--ui-text-tertiary)',
                        children: '#' + c.rank
                      }),
                      jsx('span', { children: c.name }),
                      c.is_winner && jsx('span', {
                        className: 'rounded bg-amber-500/20 px-1.5 py-0.5 text-[10px] font-semibold text-amber-300',
                        children: '🏆 WINNER'
                      }),
                      c.is_merged && jsx('span', {
                        className: 'rounded bg-emerald-500/20 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-300',
                        children: '✓ MERGED'
                      }),
                      jsx('span', {
                        className: 'rounded px-1.5 py-0.5 text-[10px] font-medium ' + (
                          c.tests_passed
                            ? 'bg-emerald-500/15 text-emerald-400'
                            : c.status === 'failed'
                              ? 'bg-rose-500/15 text-rose-400'
                              : 'bg-(--ui-surface-tertiary) text-(--ui-text-tertiary)'
                        ),
                        children: c.tests_passed ? 'PASS' : c.status.toUpperCase()
                      })
                    ]
                  }),
                  jsx('div', {
                    className: 'text-[11px] text-(--ui-text-tertiary)',
                    children: c.strategy
                  })
                ]
              }),
              jsxs('div', {
                className: 'flex items-center gap-3',
                children: [
                  jsxs('div', {
                    className: 'text-right font-mono text-[11px]',
                    children: [
                      jsx('div', {
                        className: 'font-semibold text-(--ui-text-primary)',
                        children: c.score.toFixed(1) + ' pts'
                      }),
                      jsx('div', {
                        className: 'text-(--ui-text-tertiary)',
                        children: c.duration_ms.toFixed(1) + 'ms • +' + c.insertions + '/-' + c.deletions
                      })
                    ]
                  }),
                  c.tests_passed && !c.is_merged && jsx('button', {
                    type: 'button',
                    disabled: busy,
                    onClick: () => handleMerge(c.contender_id),
                    className: 'rounded bg-emerald-600 px-2 py-1 text-[11px] font-medium text-white hover:bg-emerald-500',
                    children: 'Merge'
                  }),
                  jsx('button', {
                    type: 'button',
                    onClick: () => {
                      const ok = submitToComposer(
                        sessionId,
                        'In Joust bout ' + bout.bout_id + ', steer contender ' + c.contender_id + ' to refine its implementation and re-evaluate.'
                      )
                      if (!ok) notifyUser('info', 'Open chat composer to steer contender ' + c.contender_id)
                    },
                    className: 'rounded border border-(--ui-stroke-primary) px-2 py-1 text-[11px] text-(--ui-text-secondary) hover:text-(--ui-text-primary)',
                    children: 'Steer'
                  })
                ]
              })
            ]
          }, c.contender_id)
        )
      })
    ]
  })
}

function JoustArenaPage({ ctx }) {
  const [bouts, setBouts] = useState([])
  const [selectedBoutId, setSelectedBoutId] = useState('')
  const [selectedContenderId, setSelectedContenderId] = useState('')
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(() => {
    ctx.rest('/bouts')
      .then(res => {
        const list = res?.bouts || []
        setBouts(list)
        if (list.length > 0 && !selectedBoutId) {
          setSelectedBoutId(list[0].bout_id)
          if (list[0].contenders?.length > 0) {
            setSelectedContenderId(list[0].contenders[0].contender_id)
          }
        }
      })
      .catch(() => {})
  }, [ctx, selectedBoutId])

  useEffect(() => {
    refresh()
    const stop = ctx.onEvent('plugin.joust.bout.updated', () => refresh())
    return () => {
      if (typeof stop === 'function') stop()
    }
  }, [ctx, refresh])

  const activeBout = bouts.find(b => b.bout_id === selectedBoutId) || bouts[0] || null
  const contenders = activeBout?.contenders || []
  const activeContender = contenders.find(c => c.contender_id === selectedContenderId) || contenders[0] || null

  const runDemo = () => {
    setBusy(true)
    ctx.rest('/bouts/demo', { method: 'POST', body: {} })
      .then(res => {
        if (res?.bout) {
          setSelectedBoutId(res.bout.bout_id)
          if (res.bout.contenders?.length > 0) {
            setSelectedContenderId(res.bout.contenders[0].contender_id)
          }
          notifyUser('success', 'Launched & judged 3-contender Joust Demo Tournament!')
        }
        refresh()
      })
      .catch(err => notifyUser('error', String(err?.message || err)))
      .finally(() => setBusy(false))
  }

  const runEvaluate = () => {
    if (!activeBout) return
    setBusy(true)
    ctx.rest('/bouts/' + encodeURIComponent(activeBout.bout_id) + '/evaluate', { method: 'POST', body: {} })
      .then(() => {
        notifyUser('info', 'Evaluated all contenders in ' + activeBout.bout_id)
        refresh()
      })
      .catch(err => notifyUser('error', String(err?.message || err)))
      .finally(() => setBusy(false))
  }

  const runMerge = (cid, action = 'apply') => {
    if (!activeBout) return
    setBusy(true)
    ctx.rest('/bouts/' + encodeURIComponent(activeBout.bout_id) + '/merge', {
      method: 'POST',
      body: { contender_id: cid, action }
    })
      .then(() => {
        notifyUser('success', action === 'rollback' ? 'Rolled back merge!' : 'Merged winning worktree diff!')
        refresh()
      })
      .catch(err => notifyUser('error', String(err?.message || err)))
      .finally(() => setBusy(false))
  }

  return jsxs('div', {
    className: 'flex h-full flex-col gap-4 overflow-y-auto p-5 text-sm',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center justify-between gap-3 border-b border-(--ui-stroke-primary) pb-3',
        children: [
          jsxs('div', {
            children: [
              jsx('h1', {
                className: 'text-lg font-semibold text-(--ui-text-primary)',
                children: '⚔️ Joust — Parallel Git-Worktree Subagent Arena'
              }),
              jsx('p', {
                className: 'text-xs text-(--ui-text-tertiary)',
                children: 'Race 2–4 isolated Git worktree contenders in parallel, score correctness & latency, and merge the winner with 1-click rollback safety.'
              })
            ]
          }),
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('button', {
                type: 'button',
                disabled: busy,
                onClick: runDemo,
                className: 'rounded-lg bg-(--ui-accent) px-3 py-1.5 text-xs font-semibold text-white hover:opacity-90',
                children: busy ? 'Running Arena...' : '⚡ Run Live 3-Way Demo Tournament'
              }),
              activeBout && jsx('button', {
                type: 'button',
                disabled: busy,
                onClick: runEvaluate,
                className: 'rounded-lg border border-(--ui-stroke-primary) bg-(--ui-surface-secondary) px-3 py-1.5 text-xs font-medium text-(--ui-text-primary)',
                children: '🏁 Re-Judge All'
              }),
              activeBout && activeBout.winner_id && activeBout.status !== 'merged' && jsx('button', {
                type: 'button',
                disabled: busy,
                onClick: () => runMerge(activeBout.winner_id, 'apply'),
                className: 'rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500',
                children: '🏆 Merge Winner (' + activeBout.winner_id + ')'
              }),
              activeBout && activeBout.status === 'merged' && jsx('button', {
                type: 'button',
                disabled: busy,
                onClick: () => runMerge(activeBout.merged_contender_id, 'rollback'),
                className: 'rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-xs font-medium text-amber-300',
                children: '↩ Rollback Merge'
              })
            ]
          })
        ]
      }),
      bouts.length === 0
        ? jsxs('div', {
            className: 'flex flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-(--ui-stroke-primary) p-10 text-center',
            children: [
              jsx('div', {
                className: 'text-base font-medium text-(--ui-text-primary)',
                children: 'No Joust bouts recorded yet'
              }),
              jsx('div', {
                className: 'max-w-md text-xs text-(--ui-text-tertiary)',
                children: 'Click "Run Live 3-Way Demo Tournament" above to provision a real Git repo, race 3 competing order-book algorithms in isolated Git worktrees, benchmark them, and inspect their diffs.'
              })
            ]
          })
        : jsxs('div', {
            className: 'grid grid-cols-1 gap-4 lg:grid-cols-12',
            children: [
              jsxs('div', {
                className: 'flex flex-col gap-3 lg:col-span-5',
                children: [
                  jsxs('div', {
                    className: 'flex items-center justify-between',
                    children: [
                      jsx('span', {
                        className: 'text-xs font-semibold uppercase tracking-wider text-(--ui-text-tertiary)',
                        children: 'Active & Recent Bouts'
                      }),
                      jsx('select', {
                        value: activeBout?.bout_id || '',
                        onChange: e => {
                          const id = e.target.value
                          setSelectedBoutId(id)
                          const b = bouts.find(x => x.bout_id === id)
                          if (b?.contenders?.length > 0) setSelectedContenderId(b.contenders[0].contender_id)
                        },
                        className: 'rounded border border-(--ui-stroke-primary) bg-(--ui-surface-secondary) px-2 py-1 text-xs text-(--ui-text-primary)',
                        children: bouts.map(b =>
                          jsx('option', {
                            value: b.bout_id,
                            children: b.bout_id + ' — ' + b.title
                          }, b.bout_id)
                        )
                      })
                    ]
                  }),
                  activeBout && jsxs('div', {
                    className: 'rounded-xl border border-(--ui-stroke-primary) bg-(--ui-surface-secondary) p-3 text-xs',
                    children: [
                      jsx('div', { className: 'font-semibold text-(--ui-text-primary)', children: activeBout.title }),
                      jsx('div', { className: 'mt-1 text-(--ui-text-secondary)', children: activeBout.goal }),
                      jsxs('div', {
                        className: 'mt-2 flex flex-wrap gap-2 font-mono text-[11px] text-(--ui-text-tertiary)',
                        children: [
                          jsx('span', { children: 'Commit: ' + activeBout.base_commit.slice(0, 8) }),
                          jsx('span', { children: '•' }),
                          jsx('span', { children: 'Cmd: ' + activeBout.test_command })
                        ]
                      })
                    ]
                  }),
                  jsx('div', {
                    className: 'text-xs font-semibold uppercase tracking-wider text-(--ui-text-tertiary)',
                    children: 'Contender Leaderboard'
                  }),
                  jsx('div', {
                    className: 'flex flex-col gap-2',
                    children: contenders.map(c => {
                      const isSelected = activeContender && activeContender.contender_id === c.contender_id
                      const bd = c.score_breakdown || {}
                      return jsxs('div', {
                        onClick: () => setSelectedContenderId(c.contender_id),
                        className: 'cursor-pointer rounded-xl border p-3 transition ' + (
                          isSelected
                            ? 'border-(--ui-accent) bg-(--ui-surface-secondary)'
                            : 'border-(--ui-stroke-primary) bg-(--ui-surface-primary) hover:bg-(--ui-surface-secondary)'
                        ),
                        children: [
                          jsxs('div', {
                            className: 'flex items-center justify-between gap-2',
                            children: [
                              jsxs('div', {
                                className: 'flex items-center gap-2 font-semibold text-(--ui-text-primary)',
                                children: [
                                  jsx('span', { className: 'font-mono text-xs text-(--ui-text-tertiary)', children: '#' + c.rank }),
                                  jsx('span', { children: c.name }),
                                  c.is_winner && jsx('span', {
                                    className: 'rounded bg-amber-500/20 px-1.5 py-0.5 text-[10px] text-amber-300',
                                    children: '🏆 WINNER'
                                  }),
                                  c.is_merged && jsx('span', {
                                    className: 'rounded bg-emerald-500/20 px-1.5 py-0.5 text-[10px] text-emerald-300',
                                    children: 'MERGED'
                                  })
                                ]
                              }),
                              jsx('span', {
                                className: 'font-mono text-sm font-bold text-(--ui-text-primary)',
                                children: c.score.toFixed(1) + ' / 100'
                              })
                            ]
                          }),
                          jsx('div', {
                            className: 'mt-1 text-xs text-(--ui-text-secondary)',
                            children: c.strategy
                          }),
                          jsxs('div', {
                            className: 'mt-2 flex flex-wrap items-center justify-between gap-2 font-mono text-[11px] text-(--ui-text-tertiary)',
                            children: [
                              jsx('span', {
                                className: c.tests_passed ? 'text-emerald-400' : 'text-rose-400',
                                children: c.tests_passed ? '● Tests Passed' : '● Tests Failed (exit ' + c.exit_code + ')'
                              }),
                              jsx('span', { children: c.duration_ms.toFixed(2) + ' ms' }),
                              jsx('span', { children: '+' + c.insertions + ' / -' + c.deletions + ' LOC' }),
                              jsx('span', {
                                children: 'C:' + (bd.correctness ?? 0) + ' S:' + (bd.speed ?? 0) + ' M:' + (bd.minimalism ?? 0)
                              })
                            ]
                          })
                        ]
                      }, c.contender_id)
                    })
                  })
                ]
              }),
              jsxs('div', {
                className: 'flex flex-col gap-3 lg:col-span-7',
                children: [
                  activeContender && jsxs('div', {
                    className: 'flex flex-col gap-3 rounded-xl border border-(--ui-stroke-primary) bg-(--ui-surface-secondary) p-4',
                    children: [
                      jsxs('div', {
                        className: 'flex items-center justify-between border-b border-(--ui-stroke-primary) pb-2',
                        children: [
                          jsxs('div', {
                            children: [
                              jsx('div', {
                                className: 'font-semibold text-(--ui-text-primary)',
                                children: 'Worktree Inspector: ' + activeContender.name
                              }),
                              jsx('div', {
                                className: 'font-mono text-[11px] text-(--ui-text-tertiary)',
                                children: activeContender.worktree_path
                              })
                            ]
                          }),
                          activeContender.tests_passed && !activeContender.is_merged && jsx('button', {
                            type: 'button',
                            disabled: busy,
                            onClick: () => runMerge(activeContender.contender_id, 'apply'),
                            className: 'rounded bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-emerald-500',
                            children: 'Apply Worktree Patch'
                          })
                        ]
                      }),
                      jsx('div', {
                        className: 'text-xs font-semibold uppercase tracking-wider text-(--ui-text-tertiary)',
                        children: 'Unified Git Diff (Isolated Worktree vs Base Commit)'
                      }),
                      jsx(DiffView, { diffText: activeContender.diff_preview }),
                      jsx('div', {
                        className: 'text-xs font-semibold uppercase tracking-wider text-(--ui-text-tertiary)',
                        children: 'Verification & Benchmark Output'
                      }),
                      jsx('pre', {
                        className: 'max-h-52 overflow-y-auto rounded border border-(--ui-stroke-primary) bg-(--ui-surface-primary) p-3 font-mono text-[11px] text-(--ui-text-secondary)',
                        children: activeContender.test_output || 'Not evaluated yet.'
                      })
                    ]
                  })
                ]
              })
            ]
          })
    ]
  })
}

function JoustRightPane({ ctx }) {
  const [summary, setSummary] = useState({ total_bouts: 0, bouts: [] })

  const load = useCallback(() => {
    ctx.rest('/bouts?limit=5')
      .then(res => {
        if (res) setSummary(res)
      })
      .catch(() => {})
  }, [ctx])

  useEffect(() => {
    load()
    const stop = ctx.onEvent('plugin.joust.bout.updated', () => load())
    return () => {
      if (typeof stop === 'function') stop()
    }
  }, [ctx, load])

  const latest = (summary.bouts || [])[0]
  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 p-3 text-xs',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between',
        children: [
          jsx('span', { className: 'font-semibold text-(--ui-text-primary)', children: '⚔️ Joust Worktree Arena' }),
          jsx('button', {
            type: 'button',
            onClick: () => navigateTo('/joust'),
            className: 'text-[11px] text-(--ui-accent) hover:underline',
            children: 'Full Studio →'
          })
        ]
      }),
      !latest
        ? jsx('div', {
            className: 'rounded border border-(--ui-stroke-primary) bg-(--ui-surface-secondary) p-3 text-(--ui-text-tertiary)',
            children: 'No active bouts. Open Full Studio to launch a 3-way demo tournament.'
          })
        : jsxs('div', {
            className: 'flex flex-col gap-2',
            children: [
              jsx('div', { className: 'font-medium text-(--ui-text-primary)', children: latest.title }),
              ...(latest.contenders || []).map(c =>
                jsxs('div', {
                  className: 'flex items-center justify-between rounded border border-(--ui-stroke-primary) bg-(--ui-surface-secondary) px-2.5 py-1.5',
                  children: [
                    jsx('span', { className: 'font-medium text-(--ui-text-primary)', children: '#' + c.rank + ' ' + c.name }),
                    jsx('span', {
                      className: 'font-mono text-[11px] ' + (c.tests_passed ? 'text-emerald-400' : 'text-(--ui-text-tertiary)'),
                      children: c.score.toFixed(1) + ' pts (' + c.duration_ms.toFixed(0) + 'ms)'
                    })
                  ]
                }, c.contender_id)
              )
            ]
          })
    ]
  })
}

function JoustStatusChip({ ctx }) {
  const [count, setCount] = useState(0)

  useEffect(() => {
    const load = () => {
      ctx.rest('/bouts?limit=10')
        .then(res => setCount(res?.total_bouts || 0))
        .catch(() => {})
    }
    load()
    const stop = ctx.onEvent('plugin.joust.bout.updated', load)
    return () => {
      if (typeof stop === 'function') stop()
    }
  }, [ctx])

  return jsx('button', {
    type: 'button',
    onClick: () => navigateTo('/joust'),
    className: 'px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) hover:text-(--ui-text-primary)',
    children: '⚔️ Joust (' + count + ')'
  })
}

export default {
  id: 'joust',
  name: 'Joust Worktree Arena',
  register(ctx) {
    if (sdk.ROUTES_AREA && sdk.SIDEBAR_NAV_AREA) {
      ctx.registerMany([
        {
          id: 'arena-page',
          area: sdk.ROUTES_AREA,
          data: { path: '/joust' },
          render: () => jsx(JoustArenaPage, { ctx })
        },
        {
          id: 'arena-nav',
          area: sdk.SIDEBAR_NAV_AREA,
          data: { path: '/joust', label: 'Joust Arena', codicon: 'law' }
        }
      ])
    }

    ctx.register({
      id: 'arena-pane',
      area: 'panes',
      title: 'Joust Arena',
      data: { placement: 'right', width: '280px' },
      render: () => jsx(JoustRightPane, { ctx })
    })

    if (sdk.STATUSBAR_AREAS && sdk.STATUSBAR_AREAS.right) {
      ctx.register({
        id: 'arena-chip',
        area: sdk.STATUSBAR_AREAS.right,
        order: 135,
        render: () => jsx(JoustStatusChip, { ctx })
      })
    }

    if (sdk.TRANSCRIPT_DIRECTIVE_AREA) {
      ctx.register({
        id: 'bout-directive',
        area: sdk.TRANSCRIPT_DIRECTIVE_AREA,
        data: {
          name: 'joust-bout',
          render: ({ attrs, sessionId }) =>
            jsx(InlineBoutCard, { ctx, boutId: attrs?.id || '', sessionId })
        }
      })
    }

    if (sdk.PALETTE_AREA) {
      ctx.register({
        id: 'open-arena',
        area: sdk.PALETTE_AREA,
        data: {
          id: 'joust.open',
          label: 'Joust: Open Parallel Worktree Arena',
          keywords: ['joust', 'worktree', 'arena', 'benchmark', 'subagent'],
          run: () => navigateTo('/joust')
        }
      })
    }
  }
}
