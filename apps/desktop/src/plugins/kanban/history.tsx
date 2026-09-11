/**
 * Task History — a read-only, per-profile run log. Mounted at `/kanban-history`
 * (a ROUTES_AREA contribution — kept a single flat segment, matching the
 * area's "one segment; no params" contract, rather than nesting under
 * `/kanban`). Lists every `task_runs` row a profile has ever
 * produced (`GET /profiles/:name/runs`, the plugin's own REST router — no new
 * backend surface), grouped by task id (a task can have several runs after a
 * retry/reclaim), most recent first. When a run's `worker_session_id` is
 * populated (backend card t_baebbaf5), an "Open transcript" affordance
 * resolves it against `session.list {sources: ['kanban'], include_hidden:
 * true}` (backend card t_a33d16d8) and opens it through the app's normal
 * session route — the same door every other session-open uses, not a new
 * transcript renderer.
 *
 * Purely additive: no complete/block/comment controls live here (those stay
 * on the board's own task detail drawer). Reachable via the command palette
 * ("Kanban: Task History") AND a "Task History" entry on the Bots roster's
 * row menu (bot-row.tsx), which navigates here with `?profile=<name>` in the
 * URL — a NAVIGATION, not a Bot Mode canonical-chat interaction, so it
 * doesn't touch the Bot Mode invariant.
 */

import {
  Badge,
  Button,
  Codicon,
  ErrorState,
  host,
  Loader,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect, useMemo, useState } from 'react'

import { fetchProfileRuns, fetchProfiles, profileRunsKey, PROFILES_KEY } from './api'
import type { KanbanProfileRun } from './types'
import { ago, duration, errText, FIELD_LABEL, Section, shortId, useKanban } from './ui'

const FAILED_OUTCOMES = new Set(['crashed', 'failed', 'timed_out', 'gave_up'])

/** Extract `?profile=` from the current hash route (`#/kanban-history?profile=x`).
 *  Plugins can't share nanostores atoms across the plugin fence (`no-restricted-
 *  imports` bans `../*` — see eslint.config.mjs), so a cross-plugin navigation
 *  preset (the Bots roster's "Task History" row action) rides the URL itself,
 *  exactly like any other deep link (`host.navigate('/kanban-history?profile=x')`),
 *  not a shared atom. */
function profileFromHash(): string {
  const hash = window.location.hash || ''
  const query = hash.includes('?') ? hash.slice(hash.indexOf('?') + 1) : ''

  return new URLSearchParams(query).get('profile') || ''
}

/** A `session.list` row, scoped to what this view reads. Mirrors Bot Mode's
 *  own local shape (`CanonicalChatRow` in canonical-chat.ts) — the plugin
 *  fence means this can't be shared, only mirrored. */
interface SessionListRow {
  id: string
  resolved_id?: string
}

/** Resolve a profile's full session list once (capped, hidden included — the
 *  join key lives on rows the human-facing listing hides by default) and
 *  index it by id for O(1) "does this worker_session_id have a transcript"
 *  lookups. `sources: ['kanban']` is the opt-in from backend card
 *  t_a33d16d8 — it bypasses the human-listing deny-list for exactly the
 *  kanban-sourced rows this view needs, nothing else. */
function useProfileSessionIndex(profile: string) {
  return useQuery({
    enabled: Boolean(profile),
    queryFn: async () => {
      const res = await host.request<{ sessions?: SessionListRow[] }>('session.list', {
        profile,
        sources: ['kanban'],
        include_hidden: true,
        limit: 200
      })

      const index = new Map<string, string>()

      for (const row of res?.sessions ?? []) {
        const openId = row.resolved_id || row.id

        if (row.id && openId) {
          index.set(row.id, openId)
        }
      }

      return index
    },
    queryKey: ['kanban', 'historySessions', profile],
    staleTime: 30_000
  })
}

function RunRow({ openId, profile, run }: { openId?: string; profile: string; run: KanbanProfileRun }) {
  const k = useKanban()
  const failed = FAILED_OUTCOMES.has(run.outcome ?? run.status)
  // Status and outcome are distinct lifecycle fields (a 'running' status has
  // no outcome yet; a terminal status like 'crashed' carries an outcome that
  // can differ from the raw status column) — show both when they diverge
  // instead of collapsing one into the other.
  const showOutcome = Boolean(run.outcome) && run.outcome !== run.status

  return (
    <li className="flex flex-col gap-1 rounded-md border border-(--ui-stroke-secondary) p-2.5 text-[0.75rem]">
      <div className="flex flex-wrap items-center gap-2">
        <span className="shrink-0 font-mono text-[0.65rem] text-(--ui-text-quaternary)">#{run.id}</span>
        <Badge size="xs" variant={failed ? 'destructive' : 'muted'}>
          {run.status}
        </Badge>
        {showOutcome && (
          <Badge size="xs" variant={failed ? 'destructive' : 'muted'}>
            {run.outcome}
          </Badge>
        )}
        {duration(run.started_at, run.ended_at) && (
          <span className="text-(--ui-text-quaternary)">{duration(run.started_at, run.ended_at)}</span>
        )}
        <span className="ml-auto shrink-0 text-(--ui-text-quaternary)">{ago(run.ended_at ?? run.started_at)}</span>
        {openId ? (
          <Button
            className="shrink-0"
            onClick={() => void host.openSession?.(openId, { intent: 'in-place', profile })}
            size="xs"
            variant="outline"
          >
            <Codicon name="link-external" size="0.7rem" />
            {k.historyOpenTranscript}
          </Button>
        ) : null}
      </div>
      {run.summary && (
        <p className="line-clamp-3 whitespace-pre-wrap text-(--ui-text-tertiary)">{run.summary}</p>
      )}
      {run.error && <p className="line-clamp-3 whitespace-pre-wrap text-destructive">{run.error}</p>}
    </li>
  )
}

interface TaskGroup {
  taskId: string
  taskStatus?: null | string
  taskTitle?: null | string
  runs: KanbanProfileRun[]
}

/** Group runs by task id (a task can have several runs after a retry/
 *  reclaim), preserving the backend's most-recent-first ordering both across
 *  groups and within one. */
function groupByTask(runs: KanbanProfileRun[]): TaskGroup[] {
  const order: string[] = []
  const byTask = new Map<string, TaskGroup>()

  for (const run of runs) {
    let group = byTask.get(run.task_id)

    if (!group) {
      group = { taskId: run.task_id, taskStatus: run.task_status, taskTitle: run.task_title, runs: [] }
      byTask.set(run.task_id, group)
      order.push(run.task_id)
    }

    group.runs.push(run)
  }

  return order.map(id => byTask.get(id)!)
}

function TaskGroupCard({
  group,
  profile,
  sessionIndex
}: {
  group: TaskGroup
  profile: string
  sessionIndex?: Map<string, string>
}) {
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary) p-3">
      <div className="flex items-center gap-2">
        <h3 className="min-w-0 truncate text-[0.8125rem] font-semibold text-foreground">
          {group.taskTitle || group.taskId}
        </h3>
        <span className="shrink-0 font-mono text-[0.65rem] text-(--ui-text-quaternary)">{shortId(group.taskId)}</span>
        {group.taskStatus && (
          <Badge className="shrink-0" size="xs" variant="muted">
            {group.taskStatus}
          </Badge>
        )}
      </div>
      <ul className="flex flex-col gap-1.5">
        {group.runs.map(run => (
          <RunRow
            key={run.id}
            openId={run.worker_session_id ? sessionIndex?.get(run.worker_session_id) : undefined}
            profile={profile}
            run={run}
          />
        ))}
      </ul>
    </div>
  )
}

function ProfilePicker({ onChange, value }: { onChange: (name: string) => void; value: string }) {
  const k = useKanban()
  const { data: roster } = useQuery({ queryFn: fetchProfiles, queryKey: PROFILES_KEY, staleTime: 60_000 })

  return (
    <label className="flex min-w-0 flex-col gap-1">
      <span className={FIELD_LABEL}>{k.assignee}</span>
      <Select onValueChange={onChange} value={value}>
        <SelectTrigger className="w-48">
          <SelectValue placeholder={k.historyPickProfile} />
        </SelectTrigger>
        <SelectContent>
          {(roster?.profiles ?? []).map(profile => (
            <SelectItem key={profile.name} value={profile.name}>
              {profile.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </label>
  )
}

export function TaskHistoryPage() {
  const k = useKanban()
  const activeProfile = useValue(host.state.profile) || ''
  const [profile, setProfile] = useState(() => profileFromHash() || activeProfile)

  // A deep-link navigation (e.g. from the Bots roster) can arrive while this
  // page is already mounted, in which case only the hash changes — no remount
  // to re-run the initializer above. `hashchange` re-derives the preset once,
  // deliberately not on every keystroke of a manual profile pick below.
  useEffect(() => {
    const onHashChange = () => {
      const requested = profileFromHash()

      if (requested) {
        setProfile(requested)
      }
    }

    window.addEventListener('hashchange', onHashChange)

    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  const { data, error, isLoading } = useQuery({
    enabled: Boolean(profile),
    queryFn: () => fetchProfileRuns(profile),
    queryKey: profileRunsKey('', profile)
  })

  const { data: sessionIndex } = useProfileSessionIndex(profile)

  const groups = useMemo(() => groupByTask(data?.runs ?? []), [data])
  const errorMessage = error ? errText(error) : null

  return (
    <div className="flex h-full flex-col overflow-hidden bg-(--ui-surface-background)">
      <header className="flex shrink-0 flex-wrap items-center gap-3 px-4 py-3">
        <h1 className="text-sm font-semibold text-foreground">{k.historyTitle}</h1>
        <ProfilePicker onChange={setProfile} value={profile} />
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-4">
        {!profile ? (
          <div className="grid h-32 place-items-center text-[0.8125rem] text-(--ui-text-quaternary)">
            {k.historyPickProfile}
          </div>
        ) : errorMessage ? (
          <ErrorState title={errorMessage} />
        ) : isLoading ? (
          <div className="grid h-32 place-items-center">
            <Loader type="lemniscate-bloom" />
          </div>
        ) : groups.length === 0 ? (
          <div className="grid h-32 place-items-center text-[0.8125rem] text-(--ui-text-quaternary)">
            {k.historyEmpty}
          </div>
        ) : (
          <Section label={k.historyRunsFor(profile)}>
            <div className="flex flex-col gap-2.5">
              {groups.map(group => (
                <TaskGroupCard group={group} key={group.taskId} profile={profile} sessionIndex={sessionIndex} />
              ))}
            </div>
          </Section>
        )}
      </div>
    </div>
  )
}
