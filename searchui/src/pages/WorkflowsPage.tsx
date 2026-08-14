import AutoGraphIcon from '@mui/icons-material/AutoGraph'
import FormatListNumberedIcon from '@mui/icons-material/FormatListNumbered'
import SearchIcon from '@mui/icons-material/Search'
import {
  Alert,
  Box,
  Button,
  Card,
  CardActionArea,
  CardContent,
  Chip,
  CircularProgress,
  FormControlLabel,
  Stack,
  Step,
  StepContent,
  StepLabel,
  Stepper,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { runSimilaritySearch, GROUPED_MAX_RESULTS, type CompanyOption } from '../api'
import { GroupedResultsView } from '../components/GroupedResultsView'
import { SimilarityForm } from '../components/SimilarityForm'
import { SIMILARITY_LIMIT_MAX, triToBool } from '../components/similarity'
import { parseCsv } from '../csv'
import type { SearchHistoryDetail } from '../types'
import { useGroupedStream } from '../workflows/useGroupedStream'
import { useProspectRun } from '../workflows/useProspectRun'
import {
  useResolveIngest,
  type ResolveIngestRow,
  type ResolveIngestStatus,
  type ResolveIngestSummary,
  type UseResolveIngest,
} from '../workflows/useResolveIngest'
import { useSimilarityFormState } from '../workflows/useSimilarityFormState'

export interface WorkflowsPageProps {
  /** Render a completed search in the normal results view (SearchPage.showResults). */
  onShowResults: (detail: SearchHistoryDetail) => void
  /** Refresh the history sidebar after a search lands. */
  onHistoryRefresh: () => Promise<void> | void
}

type WorkflowId = 'prospect' | 'top-people'

const STATUS_META: Record<
  ResolveIngestStatus,
  { label: string; color: 'default' | 'info' | 'primary' | 'success' | 'warning' | 'error' }
> = {
  pending: { label: 'Pending', color: 'default' },
  resolving: { label: 'Resolving…', color: 'info' },
  probing: { label: 'Checking…', color: 'info' },
  'not-found': { label: 'Not found', color: 'warning' },
  'already-ingested': { label: 'Already ingested', color: 'success' },
  ingesting: { label: 'Ingesting…', color: 'primary' },
  done: { label: 'Done', color: 'success' },
  failed: { label: 'Failed', color: 'error' },
}

/** Split a paste box (one domain per line, commas tolerated) into raw entries;
 * normalization + dedupe happen in the hook's setDomains. */
function splitDomainsInput(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((value) => value.trim())
    .filter(Boolean)
}

export function WorkflowsPage({ onShowResults, onHistoryRefresh }: WorkflowsPageProps) {
  const [selectedWorkflow, setSelectedWorkflow] = useState<WorkflowId | null>(null)

  if (selectedWorkflow === 'prospect') {
    return (
      <ProspectRunner
        onExit={() => setSelectedWorkflow(null)}
        onShowResults={onShowResults}
        onHistoryRefresh={onHistoryRefresh}
      />
    )
  }
  if (selectedWorkflow === 'top-people') {
    return (
      <TopPeopleRunner
        onExit={() => setSelectedWorkflow(null)}
        onShowResults={onShowResults}
        onHistoryRefresh={onHistoryRefresh}
      />
    )
  }

  return (
    <Stack spacing={3} sx={{ width: '100%', maxWidth: 960, mx: 'auto' }}>
      <Box>
        <Typography variant="h4">Workflows</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mt: 0.75 }}>
          Multi-step automations built on the search tools you already use.
        </Typography>
      </Box>
      <Box
        sx={{
          display: 'grid',
          gap: 2,
          gridTemplateColumns: { xs: '1fr', sm: 'repeat(auto-fill, minmax(280px, 1fr))' },
        }}
      >
        <Card data-testid="workflow-prospect-card" variant="outlined" sx={{ borderRadius: 2 }}>
          <CardActionArea onClick={() => setSelectedWorkflow('prospect')} sx={{ height: '100%' }}>
            <CardContent>
              <Stack direction="row" spacing={1.25} sx={{ alignItems: 'center', mb: 1 }}>
                <AutoGraphIcon color="primary" />
                <Typography variant="h6">Prospect</Typography>
              </Stack>
              <Typography variant="body2" color="text.secondary">
                Domains → people ingest → semantic search over the whole set.
              </Typography>
            </CardContent>
          </CardActionArea>
        </Card>

        <Card data-testid="workflow-top-people-card" variant="outlined" sx={{ borderRadius: 2 }}>
          <CardActionArea onClick={() => setSelectedWorkflow('top-people')} sx={{ height: '100%' }}>
            <CardContent>
              <Stack direction="row" spacing={1.25} sx={{ alignItems: 'center', mb: 1 }}>
                <FormatListNumberedIcon color="primary" />
                <Typography variant="h6">Top people per company</Typography>
              </Stack>
              <Typography variant="body2" color="text.secondary">
                Domains → people ingest → top X ranked people from each company.
              </Typography>
            </CardContent>
          </CardActionArea>
        </Card>
      </Box>
    </Stack>
  )
}

// --------------------------------------------------------------------------
// Shared building blocks (both runners): header, domains step, resolve+ingest step
// --------------------------------------------------------------------------

function WorkflowHeader({
  idPrefix,
  title,
  subtitle,
  onExit,
  onStartOver,
  resetDisabled,
}: {
  idPrefix: string
  title: string
  subtitle: string
  onExit: () => void
  onStartOver: () => void
  resetDisabled: boolean
}) {
  return (
    <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center' }}>
      <Button data-testid={`${idPrefix}-back`} onClick={onExit} size="small" color="inherit">
        ← Workflows
      </Button>
      <Box sx={{ flex: 1 }}>
        <Typography variant="h5">{title}</Typography>
        <Typography variant="body2" color="text.secondary">
          {subtitle}
        </Typography>
      </Box>
      <Button data-testid={`${idPrefix}-reset`} onClick={onStartOver} size="small" disabled={resetDisabled}>
        Start over
      </Button>
    </Stack>
  )
}

function DomainsStepContent({
  idPrefix,
  domainsText,
  onDomainsText,
  csvError,
  csvInputRef,
  onImportCsv,
  onResolve,
  resolving,
  skipIfIngested,
  onSkipChange,
}: {
  idPrefix: string
  domainsText: string
  onDomainsText: (value: string) => void
  csvError: string | null
  csvInputRef: React.RefObject<HTMLInputElement | null>
  onImportCsv: (file: File) => void
  onResolve: () => void
  resolving: boolean
  skipIfIngested: boolean
  onSkipChange: (next: boolean) => void
}) {
  return (
    <Stack spacing={1.5} sx={{ pt: 1 }}>
      <TextField
        data-testid={`${idPrefix}-domains-input`}
        label="Company domains"
        placeholder={'attio.com\nstripe.com\nramp.com'}
        value={domainsText}
        onChange={(e) => onDomainsText(e.target.value)}
        fullWidth
        multiline
        minRows={4}
        helperText="One domain per line (commas tolerated). Scheme/www/paths are stripped."
      />
      {csvError && <Alert severity="error">{csvError}</Alert>}
      <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
        <Button
          data-testid={`${idPrefix}-start`}
          variant="contained"
          startIcon={resolving ? <CircularProgress size={16} color="inherit" /> : undefined}
          disabled={resolving || !splitDomainsInput(domainsText).length}
          onClick={onResolve}
        >
          {resolving ? 'Resolving…' : 'Resolve companies'}
        </Button>
        <Button data-testid={`${idPrefix}-csv`} variant="outlined" component="label">
          Import CSV
          <input
            ref={csvInputRef}
            hidden
            type="file"
            accept=".csv,text/csv"
            onChange={(e) => {
              const file = e.target.files?.[0]
              if (file) onImportCsv(file)
            }}
          />
        </Button>
        <FormControlLabel
          control={
            <Switch
              data-testid={`${idPrefix}-skip-toggle`}
              checked={skipIfIngested}
              onChange={(_, checked) => onSkipChange(checked)}
            />
          }
          label="Skip already-ingested companies"
        />
      </Stack>
    </Stack>
  )
}

/** The Resolve & ingest step content: the per-company status table (with the
 * throttle/backpressure/partial/embed-failure chips), the confirm gate, and the
 * running summary. Shared verbatim by both workflows. */
function ResolveIngestStepContent({ run, idPrefix }: { run: UseResolveIngest; idPrefix: string }) {
  const { rows, summary, phase } = run
  const ingesting = phase === 'ingesting'
  return (
    <Stack spacing={1.5} sx={{ pt: 1 }}>
      {rows.length > 0 && (
        <Box sx={{ border: '1px solid', borderColor: 'divider', borderRadius: 1, overflow: 'auto', maxHeight: 360 }}>
          <Table size="small" stickyHeader>
            <TableHead>
              <TableRow>
                <TableCell>Domain</TableCell>
                <TableCell>Company</TableCell>
                <TableCell align="right">People</TableCell>
                <TableCell>Status</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {rows.map((row) => (
                <ResolveIngestRow key={row.domain} row={row} idPrefix={idPrefix} />
              ))}
            </TableBody>
          </Table>
        </Box>
      )}

      {phase === 'confirm' && (
        <Alert
          severity={run.readyCount > 0 ? 'info' : 'warning'}
          action={
            <Button
              data-testid={`${idPrefix}-confirm`}
              color="inherit"
              size="small"
              variant="outlined"
              onClick={run.startIngest}
            >
              {run.readyCount > 0 ? 'Start ingest' : 'Continue to search'}
            </Button>
          }
        >
          {run.readyCount > 0 ? (
            <>
              {run.readyCount} company{run.readyCount === 1 ? '' : 's'} to ingest, ~
              {run.estimatedPeople.toLocaleString()}
              {run.estimateHasUnknowns ? '+' : ''} people estimated. This spends Apollo credits.
            </>
          ) : (
            'No new companies to ingest — all resolved companies are already ingested or not found. Continue to search over them.'
          )}
        </Alert>
      )}

      {(ingesting || phase === 'done') && (
        <Typography variant="body2" color="text.secondary">
          {summary.resolved} resolved · {summary.ingested} ingested · {summary.skipped} already
          ingested · {summary.notFound} not found · ~{summary.peopleLinked.toLocaleString()} people
          linked
          {ingesting ? ' (running…)' : ''}
        </Typography>
      )}
    </Stack>
  )
}

function ResolveIngestRow({ row, idPrefix }: { row: ResolveIngestRow; idPrefix: string }) {
  const meta = STATUS_META[row.status]
  const embedFailed = row.embedFailed ?? 0
  return (
    <TableRow data-testid={`${idPrefix}-row-${row.domain}`}>
      <TableCell sx={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>
        {row.domain}
      </TableCell>
      <TableCell>{row.companyName || '—'}</TableCell>
      <TableCell align="right">
        {row.people != null ? row.people.toLocaleString() : '—'}
        {embedFailed > 0 && (
          <Typography variant="caption" color="warning.main" sx={{ display: 'block', lineHeight: 1.2 }}>
            {embedFailed.toLocaleString()} embed failed
            {row.embedReason ? ` (${row.embedReason})` : ''}
          </Typography>
        )}
      </TableCell>
      <TableCell>
        <Stack direction="row" spacing={0.5} sx={{ flexWrap: 'wrap', gap: 0.5 }}>
          <Chip size="small" label={meta.label} color={meta.color} variant="outlined" />
          {row.throttled && (
            <Chip
              size="small"
              label="Throttled"
              color="warning"
              variant="outlined"
              data-testid={`${idPrefix}-throttled-${row.domain}`}
            />
          )}
          {row.embeddingDetached && (
            <Tooltip title="Embedding cancelled — leads still collecting; embeddings can be backfilled later">
              <Chip
                size="small"
                label="Embed cancelled"
                color="info"
                variant="outlined"
                data-testid={`${idPrefix}-embed-detached-${row.domain}`}
              />
            </Tooltip>
          )}
          {row.partial && (
            <Chip
              size="small"
              label={
                row.partialReason === 'apollo_page_cap'
                  ? 'Page cap'
                  : row.partialReason === 'max_entries'
                    ? 'Max entries'
                    : 'Partial'
              }
              color="warning"
              variant="outlined"
            />
          )}
        </Stack>
      </TableCell>
    </TableRow>
  )
}

function resolveIngestSummaryLabel(summary: ResolveIngestSummary): ReactNode {
  return (
    <Typography variant="caption" color="text.secondary">
      {summary.resolved} resolved · {summary.ingested} ingested · {summary.skipped} skipped
      {summary.notFound ? ` · ${summary.notFound} not found` : ''}
      {summary.failed ? ` · ${summary.failed} failed` : ''}
    </Typography>
  )
}

/** The shell shared by both runners: header + resumed banner + the two shared
 * steps, with the workflow-specific final step passed as `finalStep`. The domains
 * paste box + CSV import live here (identical for both workflows); each runner
 * supplies its own `run`, its final step, and its extra start-over cleanup. */
function WorkflowShell({
  run,
  idPrefix,
  title,
  subtitle,
  finalStepLabel,
  finalStep,
  onExit,
  onStartOverExtra,
}: {
  run: UseResolveIngest
  idPrefix: string
  title: string
  subtitle: string
  finalStepLabel: string
  finalStep: ReactNode
  onExit: () => void
  onStartOverExtra: () => void
}) {
  const { phase, rows, summary } = run
  const [domainsText, setDomainsText] = useState('')
  const [csvError, setCsvError] = useState<string | null>(null)
  const csvInputRef = useRef<HTMLInputElement | null>(null)

  // On an idle resume the rows survive in the checkpoint but the paste box (local
  // state) does not — refill it once from the restored rows so the domains stay
  // visible and re-resolvable.
  const resumeFilledRef = useRef(false)
  useEffect(() => {
    if (run.resumed && !resumeFilledRef.current && phase === 'idle' && rows.length > 0) {
      resumeFilledRef.current = true
      setDomainsText(rows.map((row) => row.domain).join('\n'))
    }
  }, [run.resumed, phase, rows])

  const startResolve = useCallback(() => {
    const domains = splitDomainsInput(domainsText)
    if (!domains.length) return
    setCsvError(null)
    run.setDomains(domains)
    run.resolve()
  }, [domainsText, run])

  const handleCsv = useCallback(async (file: File) => {
    setCsvError(null)
    try {
      const text = await file.text()
      const parsed = parseCsv(text)
      if (!parsed.length) throw new Error('Empty CSV')
      const header = parsed[0].map((cell) => cell.trim().toLowerCase())
      const column = header.indexOf('domain')
      if (column === -1) throw new Error('CSV needs a domain column')
      const values = parsed
        .slice(1)
        .map((row) => (row[column] || '').trim())
        .filter(Boolean)
      if (!values.length) throw new Error('No domain values found in the CSV')
      setDomainsText((prev) => [prev.trim(), values.join('\n')].filter(Boolean).join('\n'))
    } catch (err) {
      setCsvError(err instanceof Error ? err.message : 'CSV import failed')
    } finally {
      if (csvInputRef.current) csvInputRef.current.value = ''
    }
  }, [])

  const handleStartOver = useCallback(() => {
    run.reset()
    setDomainsText('')
    setCsvError(null)
    onStartOverExtra()
  }, [run, onStartOverExtra])

  const activeStep = phase === 'idle' ? 0 : phase === 'done' ? 2 : 1
  const resolving = phase === 'resolving'

  return (
    <Stack spacing={3} sx={{ width: '100%', maxWidth: 960, mx: 'auto' }}>
      <WorkflowHeader
        idPrefix={idPrefix}
        title={title}
        subtitle={subtitle}
        onExit={onExit}
        onStartOver={handleStartOver}
        resetDisabled={phase === 'idle' && rows.length === 0}
      />

      {run.resumed && (
        <Alert severity="info" onClose={() => undefined}>
          Resumed an in-progress run from this browser.
        </Alert>
      )}

      <Stepper activeStep={activeStep} orientation="vertical">
        <Step expanded={activeStep === 0}>
          <StepLabel>Domains</StepLabel>
          <StepContent>
            <DomainsStepContent
              idPrefix={idPrefix}
              domainsText={domainsText}
              onDomainsText={setDomainsText}
              csvError={csvError}
              csvInputRef={csvInputRef}
              onImportCsv={(file) => void handleCsv(file)}
              onResolve={startResolve}
              resolving={resolving}
              skipIfIngested={run.skipIfIngested}
              onSkipChange={run.setSkipIfIngested}
            />
          </StepContent>
        </Step>

        <Step expanded={activeStep >= 1}>
          <StepLabel optional={rows.length > 0 ? resolveIngestSummaryLabel(summary) : undefined}>
            Resolve &amp; ingest
          </StepLabel>
          <StepContent>
            <ResolveIngestStepContent run={run} idPrefix={idPrefix} />
          </StepContent>
        </Step>

        <Step expanded={activeStep >= 2}>
          <StepLabel>{finalStepLabel}</StepLabel>
          <StepContent>{finalStep}</StepContent>
        </Step>
      </Stepper>
    </Stack>
  )
}

// --------------------------------------------------------------------------
// Prospect: domains → ingest → flat saved-leads similarity search
// --------------------------------------------------------------------------

function ProspectRunner({
  onExit,
  onShowResults,
  onHistoryRefresh,
}: { onExit: () => void } & WorkflowsPageProps) {
  const run = useProspectRun()
  const form = useSimilarityFormState({ resolvedCompanies: run.resolvedCompanies, phase: run.phase })
  const [similarityLimit, setSimilarityLimit] = useState(25)
  const [searching, setSearching] = useState(false)
  const [stageError, setStageError] = useState<string | null>(null)

  const runStageSearch = useCallback(async () => {
    const passage = form.query.trim()
    if (form.hasEmbeds && !passage) {
      setStageError('Enter a passage to search saved people')
      return
    }
    if (!form.hasEmbeds && !form.hasFilter) {
      setStageError('Select at least one filter for a pure filter search')
      return
    }
    const limit = Math.min(SIMILARITY_LIMIT_MAX, Math.max(1, Math.round(Number(similarityLimit) || 1)))
    setSimilarityLimit(limit)
    setStageError(null)
    setSearching(true)
    try {
      const response = await runSimilaritySearch({
        query: form.hasEmbeds ? passage : '',
        limit,
        embeds: form.selEmbeds,
        companyIds: form.companyValues,
        emailExists: triToBool(form.emailFilter),
        phoneExists: triToBool(form.phoneFilter),
        linkedinExists: triToBool(form.linkedinFilter),
      })
      onShowResults(response.history)
      await onHistoryRefresh()
    } catch (err) {
      setStageError(err instanceof Error ? err.message : 'Similarity search failed')
    } finally {
      setSearching(false)
    }
  }, [form, similarityLimit, onShowResults, onHistoryRefresh])

  const canRunSearch =
    similarityLimit >= 1 && (form.hasEmbeds ? Boolean(form.query.trim()) : form.hasFilter)

  const finalStep = (
    <Stack spacing={2} sx={{ pt: 1 }}>
      <Typography variant="body2" color="text.secondary">
        Semantic search over the people you just ingested. Company chips are pre-filled from the
        run; edit them freely.
      </Typography>
      {stageError && <Alert severity="error">{stageError}</Alert>}
      <SimilarityForm
        query={form.query}
        onQueryChange={form.setQuery}
        embeds={form.embeds}
        onEmbedsChange={form.setEmbeds}
        companyOptions={form.companyOptions}
        companies={form.companies}
        onCompaniesChange={form.setCompanies}
        onAddCompany={form.addCompanyValue}
        resolvingCompany={form.resolvingCompany}
        companyResolveError={form.companyResolveError}
        emailFilter={form.emailFilter}
        onEmailFilterChange={form.setEmailFilter}
        phoneFilter={form.phoneFilter}
        onPhoneFilterChange={form.setPhoneFilter}
        linkedinFilter={form.linkedinFilter}
        onLinkedinFilterChange={form.setLinkedinFilter}
        limit={similarityLimit}
        onLimitChange={setSimilarityLimit}
      />
      <Box>
        <Button
          data-testid="prospect-run-search"
          variant="contained"
          size="large"
          startIcon={searching ? <CircularProgress size={18} color="inherit" /> : <SearchIcon />}
          disabled={searching || !canRunSearch}
          onClick={() => void runStageSearch()}
          sx={{ px: 3 }}
        >
          {searching ? 'Searching…' : 'Search saved leads'}
        </Button>
      </Box>
    </Stack>
  )

  return (
    <WorkflowShell
      run={run}
      idPrefix="prospect"
      title="Prospect"
      subtitle="Domains → people ingest → semantic search over the whole set."
      finalStepLabel="Search the set"
      finalStep={finalStep}
      onExit={onExit}
      onStartOverExtra={() => {
        setStageError(null)
        form.reset()
      }}
    />
  )
}

// --------------------------------------------------------------------------
// Top people per company: domains → ingest → top-N ranked people per company
// --------------------------------------------------------------------------

const TOP_PEOPLE_DEFAULT = 10
const TOP_PEOPLE_MAX = 100
/** Backend compute budget: companies × embed_kinds ≤ this (embed_kinds = max(1, selected)).
 * Streaming makes big runs legitimate, so this hard cap is the only latency-related gate
 * (the earlier soft "may time out" warning is gone). Mirrors the backend's 6000. */
const COMPUTE_BUDGET = 6000

function clampPerCompany(n: number): number {
  if (!Number.isFinite(n)) return TOP_PEOPLE_DEFAULT
  return Math.min(TOP_PEOPLE_MAX, Math.max(1, Math.round(n)))
}

function TopPeopleRunner({
  onExit,
  onHistoryRefresh,
}: { onExit: () => void } & WorkflowsPageProps) {
  const run = useResolveIngest({ checkpointKey: 'searchui.top-people.v1' })
  const form = useSimilarityFormState({ resolvedCompanies: run.resolvedCompanies, phase: run.phase })
  const grouped = useGroupedStream()
  const [perCompany, setPerCompany] = useState(TOP_PEOPLE_DEFAULT)
  const [stageError, setStageError] = useState<string | null>(null)

  const streaming = grouped.view.streaming
  // Two input domains can resolve to the SAME org; those survive the domain-keyed
  // dedupe but the backend dedupes company_ids before indexing, so the UI must send
  // — and build slots from — a list deduped by RESOLVED org id (mongo_id||apollo_id),
  // preserving first-seen order. This keeps request ids ↔ slots 1:1.
  const uniqueCompanies = useMemo<CompanyOption[]>(() => {
    const seen = new Set<string>()
    const out: CompanyOption[] = []
    for (const c of form.companies) {
      const key = (c.mongo_id || c.apollo_id || '').trim()
      if (!key || seen.has(key)) continue
      seen.add(key)
      out.push(c)
    }
    return out
  }, [form.companies])
  const companyIds = useMemo(
    () => uniqueCompanies.map((c) => (c.mongo_id || c.apollo_id || '').trim()),
    [uniqueCompanies],
  )
  const companyCount = companyIds.length
  // embed_kinds = max(1, selected) — omitted/pure-filter both count as 1 (matches backend).
  const embedKinds = Math.max(1, form.selEmbeds.length)
  const estimated = companyCount * perCompany
  const computeUnits = companyCount * embedKinds
  const overCap = estimated > GROUPED_MAX_RESULTS
  const overComputeCap = computeUnits > COMPUTE_BUDGET

  const runGrouped = useCallback(async () => {
    const passage = form.query.trim()
    if (companyCount === 0) {
      setStageError('Add at least one company (resolve domains first, or paste company ids).')
      return
    }
    if (form.hasEmbeds && !passage) {
      setStageError('Enter a passage, or clear all embeddings for a pure-filter ranking.')
      return
    }
    const per = clampPerCompany(perCompany)
    setPerCompany(per)
    if (companyCount * per > GROUPED_MAX_RESULTS) {
      setStageError(
        `${companyCount} companies × ${per} = ${(companyCount * per).toLocaleString()} exceeds the ${GROUPED_MAX_RESULTS.toLocaleString()} results cap. Lower the per-company count or the company list.`,
      )
      return
    }
    if (companyCount * embedKinds > COMPUTE_BUDGET) {
      setStageError(
        `${companyCount} companies × ${embedKinds} embed kind${embedKinds === 1 ? '' : 's'} = ${(companyCount * embedKinds).toLocaleString()} exceeds the ${COMPUTE_BUDGET.toLocaleString()} compute budget. Reduce companies or embed kinds.`,
      )
      return
    }
    setStageError(null)
    // The workflow always streams (one path, all sizes); the hook owns the
    // incremental state, error handling, and abort.
    const { completed } = await grouped.run(
      {
        query: form.hasEmbeds ? passage : null,
        embeds: form.selEmbeds,
        companyIds,
        perCompanyLimit: per,
        entityType: 'person',
        emailExists: triToBool(form.emailFilter),
        phoneExists: triToBool(form.phoneFilter),
        linkedinExists: triToBool(form.linkedinFilter),
      },
      uniqueCompanies,
    )
    if (completed) await onHistoryRefresh()
  }, [form, companyIds, uniqueCompanies, companyCount, embedKinds, perCompany, grouped, onHistoryRefresh])

  const canRun =
    companyCount > 0 &&
    !overCap &&
    !overComputeCap &&
    perCompany >= 1 &&
    (form.hasEmbeds ? Boolean(form.query.trim()) : true)

  const finalStep = (
    <Stack spacing={2} sx={{ pt: 1 }}>
      <Typography variant="body2" color="text.secondary">
        Rank the ingested people per company and keep the top few of each. Company chips are
        pre-filled from the run; edit them freely.
      </Typography>
      {(stageError || grouped.error) && (
        <Alert severity="error">{stageError || grouped.error}</Alert>
      )}
      <SimilarityForm
        query={form.query}
        onQueryChange={form.setQuery}
        embeds={form.embeds}
        onEmbedsChange={form.setEmbeds}
        companyOptions={form.companyOptions}
        companies={form.companies}
        onCompaniesChange={form.setCompanies}
        onAddCompany={form.addCompanyValue}
        resolvingCompany={form.resolvingCompany}
        companyResolveError={form.companyResolveError}
        emailFilter={form.emailFilter}
        onEmailFilterChange={form.setEmailFilter}
        phoneFilter={form.phoneFilter}
        onPhoneFilterChange={form.setPhoneFilter}
        linkedinFilter={form.linkedinFilter}
        onLinkedinFilterChange={form.setLinkedinFilter}
        limit={perCompany}
        onLimitChange={setPerCompany}
        hideLimit
      />
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} sx={{ alignItems: { sm: 'center' } }}>
        <TextField
          data-testid="top-people-per-company"
          label="Top per company"
          type="number"
          value={perCompany}
          onChange={(e) => {
            const next = Number(e.target.value)
            if (!Number.isFinite(next)) return
            setPerCompany(clampPerCompany(next))
          }}
          slotProps={{ htmlInput: { min: 1, max: TOP_PEOPLE_MAX } }}
          sx={{ maxWidth: 200 }}
        />
        <Stack data-testid="top-people-estimate" spacing={0.25}>
          <Typography variant="body2" color={overCap ? 'error' : 'text.secondary'}>
            {companyCount.toLocaleString()} {companyCount === 1 ? 'company' : 'companies'} ×{' '}
            {perCompany} = {estimated.toLocaleString()} results (max{' '}
            {GROUPED_MAX_RESULTS.toLocaleString()})
          </Typography>
          <Typography
            data-testid="top-people-compute"
            variant="body2"
            color={overComputeCap ? 'error' : 'text.secondary'}
          >
            {companyCount.toLocaleString()} × {embedKinds} embed kind{embedKinds === 1 ? '' : 's'} ={' '}
            {computeUnits.toLocaleString()} compute (max {COMPUTE_BUDGET.toLocaleString()})
          </Typography>
        </Stack>
      </Stack>
      <Box>
        <Button
          data-testid="top-people-run"
          variant="contained"
          size="large"
          startIcon={streaming ? <CircularProgress size={18} color="inherit" /> : <FormatListNumberedIcon />}
          disabled={streaming || !canRun}
          onClick={() => void runGrouped()}
          sx={{ px: 3 }}
        >
          {streaming ? 'Ranking…' : 'Rank people'}
        </Button>
        {streaming && (
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.75 }}>
            Ranking company by company — results stream in as they're ready.
          </Typography>
        )}
      </Box>
      {grouped.view.active && <GroupedResultsView view={grouped.view} />}
    </Stack>
  )

  return (
    <WorkflowShell
      run={run}
      idPrefix="top-people"
      title="Top people per company"
      subtitle="Domains → people ingest → top X ranked people from each company."
      finalStepLabel="Pick the people"
      finalStep={finalStep}
      onExit={onExit}
      onStartOverExtra={() => {
        setStageError(null)
        grouped.reset()
      }}
    />
  )
}
