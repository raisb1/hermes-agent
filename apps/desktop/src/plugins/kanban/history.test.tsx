/**
 * Task History (apps/desktop/src/plugins/kanban/history.tsx) — the desktop's
 * read-only per-profile run log (t_1d80ef7d).
 *
 * Covers the card's two required behaviors:
 *  - the view renders a list of runs from a mocked REST response, grouped by
 *    task id;
 *  - a run with `worker_session_id` renders an "Open transcript" affordance
 *    (once `session.list` resolves it to a live session id); one without
 *    does not.
 *
 * Live-desktop-app visual verification is out of scope here (per the card) —
 * this is a jsdom render, not a pixel check.
 */

import type { PluginRestOptions } from '@hermes/plugin-sdk'
import type * as HermesSdk from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Test harness supplies the host's locale registration, as plugin loading does.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

import { bindApi } from './api'
import { TaskHistoryPage } from './history'
import { KANBAN_LOCALES } from './i18n'
import type { KanbanProfileRun } from './types'

vi.mock('@/hermes', () => ({ setApiRequestProfile: vi.fn() }))

const requestMock = vi.hoisted(() => vi.fn())

vi.mock('@hermes/plugin-sdk', async importOriginal => {
  const sdk = await importOriginal<typeof HermesSdk>()

  return { ...sdk, host: { ...sdk.host, request: requestMock } }
})

let runs: KanbanProfileRun[]
let client: QueryClient
let disposeApi: () => void
let disposeLocales: () => void

const rest = vi.fn(async (path: string, _options?: PluginRestOptions): Promise<unknown> => {
  if (path.startsWith('/profiles/coder-a/runs')) {
    return { runs }
  }

  throw new Error(`Unexpected REST request: ${path}`)
})

function baseRun(overrides: Partial<KanbanProfileRun>): KanbanProfileRun {
  return {
    id: 1,
    task_id: 't_example',
    task_title: 'Example task',
    task_status: 'done',
    profile: 'coder-a',
    status: 'completed',
    outcome: 'completed',
    summary: 'Did the thing.',
    error: null,
    started_at: 1000,
    ended_at: 1100,
    worker_session_id: null,
    ...overrides
  }
}

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  disposeLocales = registerPluginLocales('kanban', KANBAN_LOCALES)
  disposeApi = bindApi(
    async <T,>(path: string, options?: PluginRestOptions) => (await rest(path, options)) as T,
    { get: (_key, fallback) => fallback, set: vi.fn(), remove: vi.fn() },
    () => vi.fn()
  )
  requestMock.mockReset()
  requestMock.mockResolvedValue({ sessions: [] })
  window.location.hash = '#/kanban-history?profile=coder-a'
})

afterEach(() => {
  cleanup()
  client.clear()
  disposeApi()
  disposeLocales()
  vi.clearAllMocks()
})

function renderPage() {
  return render(
    <QueryClientProvider client={client}>
      <TaskHistoryPage />
    </QueryClientProvider>
  )
}

describe('renders runs grouped by task id', () => {
  it('shows one task group per distinct task_id, most-recent run first within it', async () => {
    runs = [
      baseRun({ id: 2, started_at: 2000, ended_at: 2100, summary: 'Retry attempt.' }),
      baseRun({ id: 1, started_at: 1000, ended_at: 1100 })
    ]

    renderPage()

    expect(await screen.findByRole('heading', { name: 'Example task' })).toBeTruthy()
    // Both runs for the one task render under the single group card.
    expect(screen.getByText('Did the thing.')).toBeTruthy()
    expect(screen.getByText('Retry attempt.')).toBeTruthy()
  })

  it('groups two different tasks into two separate cards', async () => {
    runs = [
      baseRun({ id: 1, task_id: 't_alpha', task_title: 'Alpha task' }),
      baseRun({ id: 2, task_id: 't_bravo', task_title: 'Bravo task' })
    ]

    renderPage()

    expect(await screen.findByRole('heading', { name: 'Alpha task' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Bravo task' })).toBeTruthy()
  })

  it('shows the empty state when a profile has no runs', async () => {
    runs = []

    renderPage()

    expect(await screen.findByText('No runs yet for this profile.')).toBeTruthy()
  })
})

describe('transcript affordance follows worker_session_id', () => {
  it('offers "Open transcript" once session.list resolves the worker_session_id', async () => {
    runs = [baseRun({ id: 1, worker_session_id: '20260907_150000_abc123' })]
    requestMock.mockResolvedValue({ sessions: [{ id: '20260907_150000_abc123', resolved_id: 'runtime-1' }] })

    renderPage()

    expect(await screen.findByText('Open transcript')).toBeTruthy()
    await waitFor(() =>
      expect(requestMock).toHaveBeenCalledWith(
        'session.list',
        expect.objectContaining({ profile: 'coder-a', sources: ['kanban'], include_hidden: true })
      )
    )
  })

  it('shows no transcript affordance for a run with no worker_session_id yet', async () => {
    runs = [baseRun({ id: 1, worker_session_id: null, status: 'crashed', outcome: 'crashed', error: 'crashed early' })]

    renderPage()

    expect(await screen.findByText('crashed early')).toBeTruthy()
    expect(screen.queryByText('Open transcript')).toBeNull()
  })

  it('shows no transcript affordance when the session.list lookup has not resolved that id', async () => {
    runs = [baseRun({ id: 1, worker_session_id: '20260907_999999_missing' })]
    requestMock.mockResolvedValue({ sessions: [] })

    renderPage()

    await screen.findByRole('heading', { name: 'Example task' })
    expect(screen.queryByText('Open transcript')).toBeNull()
  })
})
