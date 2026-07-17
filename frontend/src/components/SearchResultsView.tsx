import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome'
import DownloadIcon from '@mui/icons-material/Download'
import EmailOutlinedIcon from '@mui/icons-material/EmailOutlined'
import LinkedInIcon from '@mui/icons-material/LinkedIn'
import NavigateBeforeIcon from '@mui/icons-material/NavigateBefore'
import NavigateNextIcon from '@mui/icons-material/NavigateNext'
import PhoneOutlinedIcon from '@mui/icons-material/PhoneOutlined'
import {
  Alert,
  Box,
  Button,
  Checkbox,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  Divider,
  FormControlLabel,
  List,
  ListItem,
  ListItemButton,
  ListItemText,
  Stack,
  Tooltip,
  Typography,
} from '@mui/material'
import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MutableRefObject,
  type ReactNode,
} from 'react'
import {
  enrichPeopleStream,
  getPersonLead,
  matchPeopleStream,
  type EmbeddingProgress,
  type SearchProgress,
} from '../api'
import { isEditableTarget, type ListFocus } from '../keyboard'
import { useProgress, type ProgressRun } from '../progress'
import { toggleOrRangeSelect } from '../rangeSelect'
import type { ApolloEnrichedFlags, ApolloRecord, PersonRecord, SearchHistoryDetail } from '../types'
import { RecordDetail } from './RecordDetail'

/** Session-scoped: skip enrich confirmation after the user opts out. */
let skipEnrichConfirm = false

type EnrichChannels = {
  linkedin: boolean
  email: boolean
  phone: boolean
}

const DEFAULT_CHANNELS: EnrichChannels = {
  linkedin: true,
  email: false,
  phone: false,
}

function enrichedFlags(record: ApolloRecord): ApolloEnrichedFlags {
  const raw = record.apollo_enriched
  if (raw && typeof raw === 'object') {
    return {
      linkedin: Boolean(raw.linkedin),
      email: Boolean(raw.email),
      phone: Boolean(raw.phone),
    }
  }
  return { linkedin: false, email: false, phone: false }
}

function recordKey(record: ApolloRecord) {
  return record.id ?? record.name
}

function isSelectablePerson(record: ApolloRecord): record is PersonRecord & { id: string } {
  return record.entity_type === 'person' && Boolean(record.id)
}

function channelCount(channels: EnrichChannels): number {
  return Number(channels.linkedin) + Number(channels.email) + Number(channels.phone)
}

function channelLabels(channels: EnrichChannels): string[] {
  const labels: string[] = []
  if (channels.linkedin) labels.push('LinkedIn / profile')
  if (channels.email) labels.push('email')
  if (channels.phone) labels.push('phone')
  return labels
}


function secondaryText(record: ApolloRecord): string {
  if (record.entity_type === 'person') {
    const org = record.organization?.name
    return [record.title, org].filter(Boolean).join(' · ')
  }
  return [record.industry, record.domain].filter(Boolean).join(' · ')
}

function emailFromValue(value: unknown): string | null {
  if (typeof value === 'string' && value.trim()) return value.trim()
  if (Array.isArray(value)) {
    for (const item of value) {
      if (typeof item === 'string' && item.trim()) return item.trim()
      if (item && typeof item === 'object') {
        const email = (item as Record<string, unknown>).email
        if (typeof email === 'string' && email.trim()) return email.trim()
      }
    }
  }
  return null
}

function resolvedRecordEmail(record: ApolloRecord): string | null {
  if (record.entity_type !== 'person') return null
  const direct = emailFromValue(record.email) || emailFromValue(record.emails)
  if (direct) return direct
  const entry = record.apollo_responses?.['/api/v1/people/match']
  const data = entry?.data
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null
  const match = data as Record<string, unknown>
  const person = match.person
  if (person && typeof person === 'object' && !Array.isArray(person)) {
    const nested = person as Record<string, unknown>
    const fromPerson = emailFromValue(nested.email) || emailFromValue(nested.emails)
    if (fromPerson) return fromPerson
  }
  return emailFromValue(match.email) || emailFromValue(match.emails)
}

function csvEscape(value: string): string {
  if (/[",\r\n]/.test(value)) return `"${value.replace(/"/g, '""')}"`
  return value
}

function downloadSelectedRecordsCsv(records: ApolloRecord[], filename: string): void {
  const lines = ['name,email,linkedin']
  for (const record of records) {
    const name = (record.name || '').trim() || 'null'
    const email = resolvedRecordEmail(record) || 'null'
    const linkedin = (record.linkedin_url || '').trim() || 'null'
    lines.push(`${csvEscape(name)},${csvEscape(email)},${csvEscape(linkedin)}`)
  }
  const blob = new Blob([`${lines.join('\n')}\n`], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  try {
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename
    anchor.rel = 'noopener'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  } finally {
    URL.revokeObjectURL(url)
  }
}

const MemoRecordDetail = memo(RecordDetail)

const ResultRow = memo(function ResultRow({
  record,
  selected,
  checked,
  onSelect,
  onToggleChecked,
  onShiftSelect,
}: {
  record: ApolloRecord
  selected: boolean
  checked: boolean
  onSelect: (key: string) => void
  onToggleChecked: (key: string, shiftKey?: boolean) => void
  onShiftSelect: (key: string) => void
}) {
  const key = recordKey(record)
  const secondary = secondaryText(record)
  const flags = enrichedFlags(record)
  const selectable = isSelectablePerson(record)

  return (
    <ListItem disablePadding sx={{ alignItems: 'flex-start' }}>
      <Checkbox
        className="result-checkbox"
        edge="start"
        size="small"
        checked={checked}
        disabled={!selectable}
        tabIndex={-1}
        disableRipple
        slotProps={{
          input: {
            'aria-label': selectable
              ? `Select “${record.name}”`
              : `“${record.name}” cannot be enriched`,
          },
        }}
        onClick={(event) => {
          event.preventDefault()
          event.stopPropagation()
          if (!selectable) return
          onToggleChecked(key, event.shiftKey)
        }}
        sx={{ ml: 1, mr: 0, mt: 1 }}
      />
      <ListItemButton
        data-result-key={key}
        selected={selected}
        disableRipple
        onClick={(event) => {
          if (event.shiftKey && selectable) onShiftSelect(key)
          onSelect(key)
        }}
        sx={{ alignItems: 'flex-start', py: 1.25, flex: 1 }}
      >
        <ListItemText
          primary={
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, minWidth: 0 }}>
              <Typography
                component="span"
                variant="body1"
                noWrap
                sx={{ fontWeight: selected ? 700 : 500, minWidth: 0 }}
              >
                {record.name}
              </Typography>
              {(flags.linkedin || flags.email || flags.phone) && (
                <Box
                  sx={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: 0.35,
                    flexShrink: 0,
                    color: 'primary.main',
                  }}
                >
                  {flags.linkedin && (
                    <LinkedInIcon sx={{ fontSize: 14 }} titleAccess="LinkedIn enrich run" />
                  )}
                  {flags.email && (
                    <EmailOutlinedIcon sx={{ fontSize: 14 }} titleAccess="Email enrich run" />
                  )}
                  {flags.phone && (
                    <PhoneOutlinedIcon sx={{ fontSize: 14 }} titleAccess="Phone enrich run" />
                  )}
                </Box>
              )}
            </Box>
          }
          secondary={secondary}
        />
      </ListItemButton>
    </ListItem>
  )
})

const ResultsListPane = memo(function ResultsListPane({
  results,
  searchId,
  page,
  selectedKey,
  onSelect,
  onActivate,
  onRecordsUpdated,
  onAfterEnrichment,
  listFocusRef,
}: {
  results: ApolloRecord[]
  searchId: number | null
  page: number
  selectedKey: string | null
  onSelect: (key: string) => void
  onActivate?: () => void
  onRecordsUpdated?: (records: ApolloRecord[]) => void
  onAfterEnrichment?: (info?: { waterfallPending?: boolean }) => Promise<void> | void
  listFocusRef: MutableRefObject<ListFocus>
}) {
  const progress = useProgress()
  const [actionsOpen, setActionsOpen] = useState(false)
  const [checkedKeys, setCheckedKeys] = useState<Set<string>>(() => new Set())
  const [channels, setChannels] = useState<EnrichChannels>(DEFAULT_CHANNELS)
  const [enriching, setEnriching] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [pendingEnrichCount, setPendingEnrichCount] = useState<number | null>(null)
  const [dontAskAgain, setDontAskAgain] = useState(false)
  const [actionMessage, setActionMessage] = useState<{
    severity: 'success' | 'error' | 'info'
    text: string
  } | null>(null)
  const checkAnchorRef = useRef<number | null>(null)
  const pendingEnrichCountRef = useRef<number | null>(null)
  const actionsOpenRef = useRef(actionsOpen)
  pendingEnrichCountRef.current = pendingEnrichCount
  actionsOpenRef.current = actionsOpen

  useEffect(() => {
    setActionsOpen(false)
    setCheckedKeys(new Set())
    checkAnchorRef.current = null
    setPendingEnrichCount(null)
    setActionMessage(null)
    setChannels(DEFAULT_CHANNELS)
  }, [searchId, page])

  useEffect(() => {
    const selectableKeys = new Set(
      results.filter(isSelectablePerson).map((record) => recordKey(record)),
    )
    setCheckedKeys((prev) => {
      const next = new Set([...prev].filter((key) => selectableKeys.has(key)))
      if (next.size === prev.size) return prev
      return next
    })
  }, [results])

  useEffect(() => {
    if (!actionMessage) return
    const timer = window.setTimeout(() => setActionMessage(null), 1500)
    return () => window.clearTimeout(timer)
  }, [actionMessage])

  const handleSelect = useCallback(
    (key: string) => {
      onActivate?.()
      onSelect(key)
    },
    [onActivate, onSelect],
  )

  const toggleChecked = useCallback(
    (key: string, shiftKey = false) => {
      const orderedKeys = results.map(recordKey)
      const selectableKeys = new Set(
        results.filter(isSelectablePerson).map((record) => recordKey(record)),
      )
      if (!selectableKeys.has(key) && !shiftKey) return

      setCheckedKeys((prev) => {
        const { next, nextAnchor } = toggleOrRangeSelect(
          orderedKeys,
          prev,
          key,
          checkAnchorRef.current,
          shiftKey,
        )
        checkAnchorRef.current = nextAnchor
        for (const selectedKey of [...next]) {
          if (!selectableKeys.has(selectedKey)) next.delete(selectedKey)
        }
        return next
      })
    },
    [results],
  )

  const onShiftSelect = useCallback(
    (key: string) => {
      if (!actionsOpenRef.current) return
      toggleChecked(key, true)
    },
    [toggleChecked],
  )

  const closeActions = useCallback(() => {
    setActionsOpen(false)
    setCheckedKeys(new Set())
    checkAnchorRef.current = null
    setPendingEnrichCount(null)
  }, [])

  const openActions = useCallback(() => {
    setActionsOpen(true)
  }, [])

  const toggleSelectAll = useCallback(() => {
    setCheckedKeys((prev) => {
      if (prev.size > 0) return new Set()
      return new Set(results.filter(isSelectablePerson).map((record) => recordKey(record)))
    })
  }, [results])

  const toggleChannel = useCallback((key: keyof EnrichChannels) => {
    setChannels((prev) => ({ ...prev, [key]: !prev[key] }))
  }, [])

  const getSelectedRecords = useCallback(() => {
    return results.filter((record) => checkedKeys.has(recordKey(record)))
  }, [checkedKeys, results])

  const getSelectedPeople = useCallback(() => {
    return getSelectedRecords().filter(isSelectablePerson)
  }, [getSelectedRecords])

  const handleExportCsv = useCallback(() => {
    if (exporting) return
    const selected = getSelectedRecords()
    if (!selected.length) {
      setActionMessage({
        severity: 'info',
        text: 'Select one or more results to export.',
      })
      return
    }
    setExporting(true)
    setActionMessage(null)
    try {
      const filename = searchId != null ? `search-${searchId}-selected.csv` : 'search-selected.csv'
      downloadSelectedRecordsCsv(selected, filename)
    } catch (err) {
      setActionMessage({
        severity: 'error',
        text: err instanceof Error ? err.message : 'Failed to export CSV',
      })
    } finally {
      setExporting(false)
    }
  }, [exporting, getSelectedRecords, searchId])

  const selectedChannelCount = useMemo(() => channelCount(channels), [channels])
  const estimatedTokens = useMemo(() => {
    const people = getSelectedPeople().length
    return people * selectedChannelCount
  }, [getSelectedPeople, selectedChannelCount])

  const runEnrich = useCallback(async () => {
    const selected = getSelectedPeople()
    if (!selected.length) {
      setActionMessage({
        severity: 'info',
        text: 'Select one or more people to enrich.',
      })
      return
    }
    if (!channelCount(channels)) {
      setActionMessage({
        severity: 'info',
        text: 'Choose LinkedIn, email, and/or phone enrichment.',
      })
      return
    }

    setEnriching(true)
    setActionMessage(null)

    const apolloIds = selected.map((person) => String(person.id))
    const hydratedMongoIds = new Set<string>()
    const hydrateTasks: Promise<void>[] = []
    let phonePending = 0

    const hydrateIds = (mongoIds: string[]) => {
      const unique = [
        ...new Set(mongoIds.map((id) => id.trim()).filter(Boolean)),
      ]
      if (!unique.length) return
      for (const id of unique) hydratedMongoIds.add(id)
      const task = (async () => {
        const records: ApolloRecord[] = []
        await Promise.all(
          unique.map(async (mongoId) => {
            try {
              records.push(await getPersonLead(mongoId))
            } catch {
              // Best-effort refresh; stream progress still advances.
            }
          }),
        )
        if (records.length) onRecordsUpdated?.(records)
      })()
      hydrateTasks.push(task)
    }

    const refreshHydrated = async () => {
      await Promise.all(hydrateTasks.splice(0, hydrateTasks.length))
      if (!hydratedMongoIds.size) return
      const records: ApolloRecord[] = []
      await Promise.all(
        [...hydratedMongoIds].map(async (mongoId) => {
          try {
            records.push(await getPersonLead(mongoId))
          } catch {
            /* ignore */
          }
        }),
      )
      if (records.length) onRecordsUpdated?.(records)
    }

    // Match (email/phone) and complete-info (LinkedIn) share one NDJSON shape;
    // each stream is its own progress run so the two global circles aggregate.
    const makeHandlers = (run: ProgressRun) => ({
      signal: run.signal,
      onProgress: (event: SearchProgress) => {
        // Only ingest stream ids here so cancelling fetching never touches embedding.
        const ingestStreamIds = event.active_ingest_stream_ids
        if (event.total_pages <= 0) {
          run.reportIngest({
            complete: true,
            streamId: event.ingest_stream_id,
            streamIds: ingestStreamIds,
          })
        } else {
          run.reportIngest({
            done: event.page,
            total: event.total_pages,
            streamId: event.ingest_stream_id,
            streamIds: ingestStreamIds,
          })
        }
        phonePending = Math.max(
          phonePending,
          event.phone_reveal_pending_count ?? 0,
          event.waterfall_pending_count ?? 0,
        )
      },
      onEmbeddingProgress: (event: EmbeddingProgress) => {
        // Only embedding stream ids here so cancelling embedding never touches ingest.
        const embedStreamIds = event.active_embedding_stream_ids
        if (event.complete || event.error) {
          run.reportEmbed({
            complete: true,
            streamId: event.embedding_stream_id,
            streamIds: embedStreamIds,
          })
          return
        }
        run.reportEmbed({
          done: event.done,
          total: event.total,
          streamId: event.embedding_stream_id,
          streamIds: embedStreamIds,
        })
      },
      onIds: hydrateIds,
    })

    let matchOk = !(channels.email || channels.phone)
    let matchCancelled = false
    let matchError: string | null = null

    if (channels.email || channels.phone) {
      const run = progress.beginRun()
      try {
        await matchPeopleStream(
          apolloIds,
          {
            run_waterfall_email: channels.email || undefined,
            run_waterfall_phone: channels.phone || undefined,
          },
          makeHandlers(run),
        )
        matchCancelled = run.signal.aborted
        matchOk = !matchCancelled
      } catch (err) {
        matchOk = false
        matchError = err instanceof Error ? err.message : 'email/phone enrich failed'
      } finally {
        run.end()
        await refreshHydrated()
      }
    }

    let linkedinOk = !channels.linkedin
    let linkedinCancelled = false
    let linkedinError: string | null = null
    if (channels.linkedin && !matchCancelled) {
      const run = progress.beginRun()
      try {
        await enrichPeopleStream(apolloIds, makeHandlers(run))
        linkedinCancelled = run.signal.aborted
        linkedinOk = !linkedinCancelled
      } catch (err) {
        linkedinOk = false
        linkedinError = err instanceof Error ? err.message : 'profile enrich failed'
      } finally {
        run.end()
        await refreshHydrated()
      }
    } else if (channels.linkedin && matchCancelled) {
      linkedinCancelled = true
      linkedinOk = false
    }

    try {
      await onAfterEnrichment?.({ waterfallPending: phonePending > 0 })
    } catch {
      // Credit refresh is best-effort; enrich results already applied.
    }

    const cancelled = matchCancelled || linkedinCancelled
    const failures = [
      ...(matchError ? [matchError] : []),
      ...(linkedinError ? [linkedinError] : []),
    ]
    const channelOk =
      (channels.email || channels.phone ? matchOk : true) &&
      (channels.linkedin ? linkedinOk : true)
    const succeeded = channelOk ? selected.length : 0

    setEnriching(false)
    if (cancelled && succeeded === 0 && !failures.length) {
      setActionMessage({ severity: 'info', text: 'Enrichment cancelled.' })
      return
    }
    if (failures.length && succeeded === 0) {
      setActionMessage({ severity: 'error', text: failures[0] })
      return
    }

    const pendingNote =
      phonePending > 0
        ? ` Waterfall/phone results pending for ${phonePending} (async webhook).`
        : ''

    if (failures.length) {
      setActionMessage({
        severity: 'error',
        text: `Enriched ${succeeded}, failed ${failures.length}. ${failures[0]}${pendingNote}`,
      })
      return
    }
    setActionMessage({
      severity: 'success',
      text: `Enriched ${succeeded} lead${succeeded === 1 ? '' : 's'}.${pendingNote}`,
    })
  }, [
    channels,
    getSelectedPeople,
    onAfterEnrichment,
    progress,
    onRecordsUpdated,
  ])

  const requestEnrich = useCallback(() => {
    const selected = getSelectedPeople()
    if (!selected.length) {
      setActionMessage({
        severity: 'info',
        text: 'Select one or more people to enrich.',
      })
      return
    }
    if (!channelCount(channels)) {
      setActionMessage({
        severity: 'info',
        text: 'Choose LinkedIn, email, and/or phone enrichment.',
      })
      return
    }
    if (skipEnrichConfirm) {
      void runEnrich()
      return
    }
    setDontAskAgain(false)
    setPendingEnrichCount(selected.length)
  }, [channels, getSelectedPeople, runEnrich])

  const confirmEnrich = useCallback(() => {
    if (pendingEnrichCountRef.current === null) return
    pendingEnrichCountRef.current = null
    if (dontAskAgain) skipEnrichConfirm = true
    setPendingEnrichCount(null)
    setDontAskAgain(false)
    void runEnrich()
  }, [dontAskAgain, runEnrich])

  const cancelEnrich = useCallback(() => {
    setPendingEnrichCount(null)
    setDontAskAgain(false)
  }, [])

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (listFocusRef.current !== 'results') return
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return

      if (pendingEnrichCount !== null) {
        if (event.key === 'Enter') {
          event.preventDefault()
          confirmEnrich()
        }
        return
      }

      if (isEditableTarget(event.target)) return
      if (results.length === 0) return

      const key = event.key.toLowerCase()
      if (key === 'a') {
        event.preventDefault()
        if (actionsOpen) closeActions()
        else openActions()
        return
      }
      if (key === 's') {
        if (!actionsOpen || !selectedKey) return
        event.preventDefault()
        toggleChecked(selectedKey)
        return
      }
      if (key !== 'j' && key !== 'k') return

      const currentIndex = selectedKey
        ? results.findIndex((record) => recordKey(record) === selectedKey)
        : 0
      const safeIndex = currentIndex < 0 ? 0 : currentIndex
      const nextIndex =
        key === 'j'
          ? Math.min(safeIndex + 1, results.length - 1)
          : Math.max(safeIndex - 1, 0)

      if (nextIndex === safeIndex && selectedKey) return

      event.preventDefault()
      const nextKey = recordKey(results[nextIndex])
      handleSelect(nextKey)

      const item = document.querySelector<HTMLElement>(
        `[data-result-key="${CSS.escape(nextKey)}"]`,
      )
      item?.scrollIntoView({ block: 'nearest' })
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [
    actionsOpen,
    closeActions,
    handleSelect,
    listFocusRef,
    openActions,
    pendingEnrichCount,
    results,
    selectedKey,
    toggleChecked,
    confirmEnrich,
  ])

  const checkedCount = checkedKeys.size
  // Not gated on `enriching`: multiple enrich batches may run at once (their
  // progress is aggregated into the two global circles).
  const canSubmitEnrich =
    checkedCount > 0 && selectedChannelCount > 0 && estimatedTokens > 0

  return (
    <Box
      className="pane-results"
      onPointerDown={() => onActivate?.()}
      sx={{
        position: 'relative',
        borderRight: { md: '1px solid' },
        borderBottom: { xs: '1px solid', md: 'none' },
        borderColor: 'divider',
        minHeight: 0,
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        // Hide checkboxes via CSS so Actions/Done does not re-render every row.
        '& .result-checkbox': {
          display: actionsOpen ? 'inline-flex' : 'none',
        },
      }}
    >
      <Box
        sx={{
          px: 1.5,
          py: 1,
          display: 'flex',
          alignItems: 'center',
          gap: 1,
          flexShrink: 0,
          borderBottom: '1px solid',
          borderColor: 'divider',
        }}
      >
        <Typography variant="subtitle2" sx={{ fontWeight: 700, flex: 1 }}>
          Results
        </Typography>
        {actionsOpen && checkedCount > 0 && selectedChannelCount > 0 && (
          <Typography variant="caption" color="text.secondary" sx={{ flexShrink: 0 }}>
            Est. {estimatedTokens} Apollo token{estimatedTokens === 1 ? '' : 's'}
          </Typography>
        )}
        {results.length > 0 && (
          <Button
            size="small"
            variant={actionsOpen ? 'outlined' : 'text'}
            disableRipple
            onClick={() => (actionsOpen ? closeActions() : openActions())}
            sx={{ textTransform: 'none', minWidth: 0 }}
          >
            {actionsOpen ? 'Done' : 'Actions'}
          </Button>
        )}
      </Box>

      {actionsOpen && results.length > 0 && (
        <Box
          sx={{
            px: 1.5,
            py: 0.75,
            mx: 1,
            mt: 1,
            mb: 0.5,
            flexShrink: 0,
            display: 'flex',
            flexDirection: 'column',
            gap: 0.75,
            borderRadius: 1,
            bgcolor: (t) =>
              checkedCount > 0
                ? t.palette.mode === 'dark'
                  ? 'rgba(92, 184, 154, 0.12)'
                  : 'rgba(11, 61, 46, 0.08)'
                : 'transparent',
          }}
        >
          <Box
            sx={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: 1,
            }}
          >
            <Button
              size="small"
              disableRipple
              onClick={toggleSelectAll}
              sx={{ textTransform: 'none', minWidth: 0 }}
            >
              {checkedCount > 0 ? 'Deselect all' : 'Select all'}
            </Button>
            <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
              <Button
                size="small"
                variant="outlined"
                disableRipple
                startIcon={
                  exporting ? (
                    <CircularProgress size={14} color="inherit" />
                  ) : (
                    <DownloadIcon fontSize="small" />
                  )
                }
                disabled={checkedCount === 0 || exporting}
                onClick={handleExportCsv}
                sx={{ textTransform: 'none' }}
              >
                Export CSV{checkedCount ? ` (${checkedCount})` : ''}
              </Button>
              {selectedChannelCount > 0 && (
                <Button
                  size="small"
                  variant="contained"
                  disableRipple
                  startIcon={
                    enriching ? (
                      <CircularProgress size={14} color="inherit" />
                    ) : (
                      <AutoAwesomeIcon fontSize="small" />
                    )
                  }
                  disabled={!canSubmitEnrich}
                  onClick={requestEnrich}
                  sx={{ textTransform: 'none' }}
                >
                  Enrich{checkedCount ? ` (${checkedCount})` : ''}
                </Button>
              )}
            </Stack>
          </Box>

          <Stack direction="row" spacing={0.25} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
            <Tooltip title="LinkedIn / complete profile">
              <FormControlLabel
                sx={{ mr: 0.5, ml: 0 }}
                control={
                  <Checkbox
                    size="small"
                    checked={channels.linkedin}
                    onChange={() => toggleChannel('linkedin')}
                  />
                }
                label={<LinkedInIcon fontSize="small" color={channels.linkedin ? 'primary' : 'action'} />}
              />
            </Tooltip>
            <Tooltip title="Waterfall email enrich">
              <FormControlLabel
                sx={{ mr: 0.5, ml: 0 }}
                control={
                  <Checkbox
                    size="small"
                    checked={channels.email}
                    onChange={() => toggleChannel('email')}
                  />
                }
                label={
                  <EmailOutlinedIcon
                    fontSize="small"
                    color={channels.email ? 'primary' : 'action'}
                  />
                }
              />
            </Tooltip>
            <Tooltip title="Waterfall phone enrich">
              <FormControlLabel
                sx={{ mr: 0.5, ml: 0 }}
                control={
                  <Checkbox
                    size="small"
                    checked={channels.phone}
                    onChange={() => toggleChannel('phone')}
                  />
                }
                label={
                  <PhoneOutlinedIcon
                    fontSize="small"
                    color={channels.phone ? 'primary' : 'action'}
                  />
                }
              />
            </Tooltip>
            {checkedCount > 0 && (
              <Typography variant="body2" sx={{ fontWeight: 600, ml: 0.5 }}>
                {checkedCount} selected
              </Typography>
            )}
          </Stack>
        </Box>
      )}

      {actionMessage && (
        <Alert
          severity={actionMessage.severity}
          onClose={() => setActionMessage(null)}
          sx={{
            position: 'absolute',
            top: 8,
            left: 8,
            right: 8,
            zIndex: 2,
            py: 0,
            boxShadow: 2,
          }}
        >
          {actionMessage.text}
        </Alert>
      )}

      <List dense disablePadding sx={{ overflow: 'auto', flex: 1, minHeight: 0 }}>
        {results.map((record) => {
          const key = recordKey(record)
          return (
            <ResultRow
              key={key}
              record={record}
              selected={selectedKey === key}
              checked={checkedKeys.has(key)}
              onSelect={handleSelect}
              onToggleChecked={toggleChecked}
              onShiftSelect={onShiftSelect}
            />
          )
        })}
        {!results.length && (
          <Box sx={{ p: 3 }}>
            <Typography color="text.secondary">No records stored for this search.</Typography>
          </Box>
        )}
      </List>

      <Dialog
        open={pendingEnrichCount !== null}
        onClose={cancelEnrich}
        aria-labelledby="enrich-confirm-title"
      >
        <DialogTitle id="enrich-confirm-title">Enrich selected people?</DialogTitle>
        <DialogContent>
          <DialogContentText>
            {pendingEnrichCount === 1
              ? `Enrich 1 selected person (${channelLabels(channels).join(', ')})? Estimated ${estimatedTokens} Apollo token${estimatedTokens === 1 ? '' : 's'}.`
              : `Enrich ${pendingEnrichCount} selected people (${channelLabels(channels).join(', ')})? Estimated ${estimatedTokens} Apollo tokens.`}
            {channels.phone || channels.email
              ? ' Email/phone via waterfall arrive asynchronously via webhook after Apollo finishes.'
              : ''}
          </DialogContentText>
          <FormControlLabel
            sx={{ mt: 1.5, ml: 0 }}
            control={
              <Checkbox
                size="small"
                checked={dontAskAgain}
                onChange={(_, checked) => setDontAskAgain(checked)}
              />
            }
            label="Don't ask again this session"
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={cancelEnrich} sx={{ textTransform: 'none' }}>
            Cancel
          </Button>
          <Button
            variant="contained"
            onClick={confirmEnrich}
            autoFocus
            sx={{ textTransform: 'none' }}
          >
            Enrich
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  )
})

export interface SearchResultsViewProps {
  search: SearchHistoryDetail | null
  headerAction?: ReactNode
  onUseCompanyForPeopleSearch?: (
    organizationId: string,
    companyName: string,
    companyDomain?: string | null,
  ) => void
  onChangePage?: (page: number) => Promise<void> | void
  paging?: boolean
  pageError?: string | null
  listFocusRef: MutableRefObject<ListFocus>
  onActivate?: () => void
  onRecordsUpdated?: (records: ApolloRecord[]) => void
  onAfterEnrichment?: (info?: { waterfallPending?: boolean }) => Promise<void> | void
}

export const SearchResultsView = memo(
  function SearchResultsView({
  search,
  headerAction,
  onUseCompanyForPeopleSearch,
  onChangePage,
  paging = false,
  pageError = null,
  listFocusRef,
  onActivate,
  onRecordsUpdated,
  onAfterEnrichment,
}: SearchResultsViewProps) {
  const [selectedId, setSelectedId] = useState<string | null>(null)

  useEffect(() => {
    setSelectedId(null)
  }, [search?.id, search?.page])

  const results = search?.results ?? []
  const selected =
    results.find((r) => recordKey(r) === selectedId) ?? results[0] ?? null
  const selectedKey = selected ? recordKey(selected) : null
  const showPeoplePrefill =
    search?.entity_type === 'companies' && Boolean(onUseCompanyForPeopleSearch)

  const page = search?.page ?? 1
  const perPage = search?.per_page || results.length || 100
  const total = search?.total_results ?? results.length
  const totalPages = Math.max(1, Math.ceil(total / Math.max(perPage, 1)))
  const canPrev = Boolean(onChangePage) && page > 1 && !paging
  const canNext = Boolean(onChangePage) && page < totalPages && !paging

  const handleSelect = useCallback((key: string) => {
    setSelectedId(key)
  }, [])

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (listFocusRef.current !== 'results') return
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return
      if (isEditableTarget(event.target)) return
      if (!onChangePage) return

      const key = event.key.toLowerCase()
      if (key === 'p' && canPrev) {
        event.preventDefault()
        void onChangePage(page - 1)
      } else if (key === 'n' && canNext) {
        event.preventDefault()
        void onChangePage(page + 1)
      }
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [canNext, canPrev, listFocusRef, onChangePage, page])

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0, overflow: 'hidden' }}>
      <Box sx={{ px: 2.5, py: 1.5, display: 'flex', alignItems: 'center', gap: 1, flexShrink: 0 }}>
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography variant="overline" color="text.secondary">
            Previous search
          </Typography>
          <Typography variant="h6" noWrap>
            {search ? `“${search.query}”` : 'Results'}
          </Typography>
          {search && (
            <Typography variant="body2" color="text.secondary">
              {search.entity_type} · {total.toLocaleString()} matches · page {page} of{' '}
              {totalPages} · showing {results.length}
            </Typography>
          )}
        </Box>
        {onChangePage && search && (
          <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
            <Button
              size="small"
              disableRipple
              startIcon={paging ? <CircularProgress size={14} color="inherit" /> : <NavigateBeforeIcon />}
              disabled={!canPrev}
              onClick={() => onChangePage(page - 1)}
            >
              Prev
            </Button>
            <Button
              size="small"
              disableRipple
              endIcon={paging ? <CircularProgress size={14} color="inherit" /> : <NavigateNextIcon />}
              disabled={!canNext}
              onClick={() => onChangePage(page + 1)}
            >
              Next
            </Button>
          </Stack>
        )}
        {headerAction}
      </Box>
      {pageError && (
        <Box sx={{ px: 2.5, pb: 1, flexShrink: 0 }}>
          <Typography variant="body2" color="error">
            {pageError}
          </Typography>
        </Box>
      )}
      <Divider sx={{ flexShrink: 0 }} />
      <Box
        sx={{
          flex: 1,
          minHeight: 0,
          display: 'grid',
          gridTemplateColumns: { xs: '1fr', md: '340px 1fr' },
          gridTemplateRows: { xs: 'minmax(0, 1fr) minmax(0, 1fr)', md: 'minmax(0, 1fr)' },
          overflow: 'hidden',
        }}
      >
        <ResultsListPane
          results={results}
          searchId={search?.id ?? null}
          page={page}
          selectedKey={selectedKey}
          onSelect={handleSelect}
          onActivate={onActivate}
          onRecordsUpdated={onRecordsUpdated}
          onAfterEnrichment={onAfterEnrichment}
          listFocusRef={listFocusRef}
        />
        <Box sx={{ p: 3, minHeight: 0, overflow: 'auto' }}>
          {selected ? (
            <MemoRecordDetail
              record={selected}
              onRecordUpdated={(updated) => onRecordsUpdated?.([updated])}
              onUseForPeopleSearch={
                showPeoplePrefill && selected.entity_type === 'company'
                  ? onUseCompanyForPeopleSearch
                  : undefined
              }
            />
          ) : (
            <Stack spacing={1} sx={{ justifyContent: 'center', height: '100%' }}>
              <Typography color="text.secondary">Select a record to inspect.</Typography>
            </Stack>
          )}
        </Box>
      </Box>
    </Box>
  )
  },
  (prev, next) =>
    prev.search === next.search &&
    prev.search?.results === next.search?.results &&
    prev.paging === next.paging &&
    prev.pageError === next.pageError &&
    prev.headerAction === next.headerAction &&
    prev.listFocusRef === next.listFocusRef &&
    prev.onActivate === next.onActivate &&
    prev.onRecordsUpdated === next.onRecordsUpdated &&
    prev.onAfterEnrichment === next.onAfterEnrichment,
)
