import AutoGraphIcon from '@mui/icons-material/AutoGraph'
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
  Typography,
} from '@mui/material'
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  listRecentCompanies,
  resolveCompany,
  runSimilaritySearch,
  type CompanyOption,
  type EmbedKind,
} from '../api'
import { SimilarityForm } from '../components/SimilarityForm'
import {
  SIMILARITY_LIMIT_MAX,
  selectedEmbedKinds,
  triToBool,
  type TriState,
} from '../components/similarity'
import { parseCsv } from '../csv'
import type { SearchHistoryDetail } from '../types'
import { useProspectRun, type ProspectStatus } from '../workflows/useProspectRun'

export interface WorkflowsPageProps {
  /** Render a completed search in the normal results view (SearchPage.showResults). */
  onShowResults: (detail: SearchHistoryDetail) => void
  /** Refresh the history sidebar after a search lands. */
  onHistoryRefresh: () => Promise<void> | void
}

type WorkflowId = 'prospect'

const STATUS_META: Record<
  ProspectStatus,
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
        <Card
          data-testid="workflow-prospect-card"
          variant="outlined"
          sx={{ borderRadius: 2 }}
        >
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
      </Box>
    </Stack>
  )
}

function ProspectRunner({
  onExit,
  onShowResults,
  onHistoryRefresh,
}: {
  onExit: () => void
} & WorkflowsPageProps) {
  const run = useProspectRun()
  const { phase, rows, summary } = run

  const [domainsText, setDomainsText] = useState('')
  const [csvError, setCsvError] = useState<string | null>(null)
  const csvInputRef = useRef<HTMLInputElement | null>(null)

  // Stage-3 similarity form state (mirrors SearchPage's saved-leads search).
  const [query, setQuery] = useState('')
  const [simEmbeds, setSimEmbeds] = useState<Record<EmbedKind, boolean>>({
    apollo: true,
    name: true,
    title: true,
  })
  const [simCompanies, setSimCompanies] = useState<CompanyOption[]>([])
  const [companyOptions, setCompanyOptions] = useState<CompanyOption[]>([])
  const [resolvingCompany, setResolvingCompany] = useState(false)
  const [companyResolveError, setCompanyResolveError] = useState<string | null>(null)
  const [simEmailFilter, setSimEmailFilter] = useState<TriState>('any')
  const [simPhoneFilter, setSimPhoneFilter] = useState<TriState>('any')
  const [simLinkedinFilter, setSimLinkedinFilter] = useState<TriState>('any')
  const [similarityLimit, setSimilarityLimit] = useState(25)
  const [searching, setSearching] = useState(false)
  const [stageError, setStageError] = useState<string | null>(null)

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

  // Load the recent-companies dropdown for the stage-3 company picker.
  useEffect(() => {
    let cancelled = false
    void listRecentCompanies(25)
      .then((options) => {
        if (!cancelled) setCompanyOptions(options.filter((o) => o.mongo_id && o.name))
      })
      .catch(() => {
        // The dropdown is a convenience; verbatim ids still work without it.
      })
    return () => {
      cancelled = true
    }
  }, [])

  // Pre-fill the stage-3 company chips from the run's resolved orgs once ingest
  // completes; clear the guard if the run restarts so a later run re-fills.
  const prefilledRef = useRef(false)
  useEffect(() => {
    if (phase === 'done' && !prefilledRef.current) {
      prefilledRef.current = true
      setSimCompanies(run.resolvedCompanies)
    }
    if (phase !== 'done') prefilledRef.current = false
  }, [phase, run.resolvedCompanies])

  const addCompanyValue = useCallback(
    async (raw: string) => {
      const value = raw.trim()
      if (!value) return
      setCompanyResolveError(null)
      const already = (c: CompanyOption) => c.mongo_id === value || c.apollo_id === value
      setResolvingCompany(true)
      try {
        const known = companyOptions.find(already)
        const resolved = known ?? (await resolveCompany(value))
        setSimCompanies((current) =>
          current.some(
            (c) => c.mongo_id === resolved.mongo_id && c.apollo_id === resolved.apollo_id,
          )
            ? current
            : [...current, resolved],
        )
      } catch {
        setCompanyResolveError(`No company found for “${value}”`)
      } finally {
        setResolvingCompany(false)
      }
    },
    [companyOptions],
  )

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

  const runStageSearch = useCallback(async () => {
    const passage = query.trim()
    const selEmbeds = selectedEmbedKinds(simEmbeds)
    const hasEmbeds = selEmbeds.length > 0
    const companyValues = simCompanies
      .map((c) => (c.mongo_id || c.apollo_id || '').trim())
      .filter(Boolean)
    const hasFilter =
      companyValues.length > 0 ||
      simEmailFilter !== 'any' ||
      simPhoneFilter !== 'any' ||
      simLinkedinFilter !== 'any'
    if (hasEmbeds && !passage) {
      setStageError('Enter a passage to search saved people')
      return
    }
    if (!hasEmbeds && !hasFilter) {
      setStageError('Select at least one filter for a pure filter search')
      return
    }
    const limit = Math.min(
      SIMILARITY_LIMIT_MAX,
      Math.max(1, Math.round(Number(similarityLimit) || 1)),
    )
    setSimilarityLimit(limit)
    setStageError(null)
    setSearching(true)
    try {
      const response = await runSimilaritySearch({
        query: hasEmbeds ? passage : '',
        limit,
        embeds: selEmbeds,
        companyIds: companyValues,
        emailExists: triToBool(simEmailFilter),
        phoneExists: triToBool(simPhoneFilter),
        linkedinExists: triToBool(simLinkedinFilter),
      })
      onShowResults(response.history)
      await onHistoryRefresh()
    } catch (err) {
      setStageError(err instanceof Error ? err.message : 'Similarity search failed')
    } finally {
      setSearching(false)
    }
  }, [
    query,
    simEmbeds,
    simCompanies,
    simEmailFilter,
    simPhoneFilter,
    simLinkedinFilter,
    similarityLimit,
    onShowResults,
    onHistoryRefresh,
  ])

  const handleStartOver = useCallback(() => {
    run.reset()
    setDomainsText('')
    setCsvError(null)
    setStageError(null)
    setSimCompanies([])
  }, [run])

  const activeStep = phase === 'idle' ? 0 : phase === 'done' ? 2 : 1
  const resolving = phase === 'resolving'
  const ingesting = phase === 'ingesting'
  const selEmbeds = selectedEmbedKinds(simEmbeds)
  const hasEmbeds = selEmbeds.length > 0
  const hasSimFilter =
    simCompanies.length > 0 ||
    simEmailFilter !== 'any' ||
    simPhoneFilter !== 'any' ||
    simLinkedinFilter !== 'any'
  const canRunSearch =
    similarityLimit >= 1 && (hasEmbeds ? Boolean(query.trim()) : hasSimFilter)

  return (
    <Stack spacing={3} sx={{ width: '100%', maxWidth: 960, mx: 'auto' }}>
      <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center' }}>
        <Button data-testid="prospect-back" onClick={onExit} size="small" color="inherit">
          ← Workflows
        </Button>
        <Box sx={{ flex: 1 }}>
          <Typography variant="h5">Prospect</Typography>
          <Typography variant="body2" color="text.secondary">
            Domains → people ingest → semantic search over the whole set.
          </Typography>
        </Box>
        <Button
          data-testid="prospect-reset"
          onClick={handleStartOver}
          size="small"
          disabled={phase === 'idle' && rows.length === 0}
        >
          Start over
        </Button>
      </Stack>

      {run.resumed && (
        <Alert severity="info" onClose={() => undefined}>
          Resumed an in-progress run from this browser.
        </Alert>
      )}

      <Stepper activeStep={activeStep} orientation="vertical">
        <Step expanded={activeStep === 0}>
          <StepLabel>Domains</StepLabel>
          <StepContent>
            <Stack spacing={1.5} sx={{ pt: 1 }}>
              <TextField
                data-testid="prospect-domains-input"
                label="Company domains"
                placeholder={'attio.com\nstripe.com\nramp.com'}
                value={domainsText}
                onChange={(e) => setDomainsText(e.target.value)}
                fullWidth
                multiline
                minRows={4}
                helperText="One domain per line (commas tolerated). Scheme/www/paths are stripped."
              />
              {csvError && <Alert severity="error">{csvError}</Alert>}
              <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
                <Button
                  data-testid="prospect-start"
                  variant="contained"
                  startIcon={resolving ? <CircularProgress size={16} color="inherit" /> : undefined}
                  disabled={resolving || !splitDomainsInput(domainsText).length}
                  onClick={startResolve}
                >
                  {resolving ? 'Resolving…' : 'Resolve companies'}
                </Button>
                <Button data-testid="prospect-csv" variant="outlined" component="label">
                  Import CSV
                  <input
                    ref={csvInputRef}
                    hidden
                    type="file"
                    accept=".csv,text/csv"
                    onChange={(e) => {
                      const file = e.target.files?.[0]
                      if (file) void handleCsv(file)
                    }}
                  />
                </Button>
                <FormControlLabel
                  control={
                    <Switch
                      data-testid="prospect-skip-toggle"
                      checked={run.skipIfIngested}
                      onChange={(_, checked) => run.setSkipIfIngested(checked)}
                    />
                  }
                  label="Skip already-ingested companies"
                />
              </Stack>
            </Stack>
          </StepContent>
        </Step>

        <Step expanded={activeStep >= 1}>
          <StepLabel
            optional={
              rows.length > 0 ? (
                <Typography variant="caption" color="text.secondary">
                  {summary.resolved} resolved · {summary.ingested} ingested · {summary.skipped} skipped
                  {summary.notFound ? ` · ${summary.notFound} not found` : ''}
                  {summary.failed ? ` · ${summary.failed} failed` : ''}
                </Typography>
              ) : undefined
            }
          >
            Resolve &amp; ingest
          </StepLabel>
          <StepContent>
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
                      {rows.map((row) => {
                        const meta = STATUS_META[row.status]
                        return (
                          <TableRow key={row.domain} data-testid={`prospect-row-${row.domain}`}>
                            <TableCell sx={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>
                              {row.domain}
                            </TableCell>
                            <TableCell>{row.companyName || '—'}</TableCell>
                            <TableCell align="right">
                              {row.people != null ? row.people.toLocaleString() : '—'}
                            </TableCell>
                            <TableCell>
                              <Chip size="small" label={meta.label} color={meta.color} variant="outlined" />
                            </TableCell>
                          </TableRow>
                        )
                      })}
                    </TableBody>
                  </Table>
                </Box>
              )}

              {phase === 'confirm' && (
                <Alert
                  severity={run.readyCount > 0 ? 'info' : 'warning'}
                  action={
                    <Button
                      data-testid="prospect-confirm"
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
                      {run.estimateHasUnknowns ? '+' : ''} people estimated. This spends Apollo
                      credits.
                    </>
                  ) : (
                    'No new companies to ingest — all resolved companies are already ingested or not found. Continue to search over them.'
                  )}
                </Alert>
              )}

              {(ingesting || phase === 'done') && (
                <Typography variant="body2" color="text.secondary">
                  {summary.resolved} resolved · {summary.ingested} ingested · {summary.skipped}{' '}
                  already ingested · {summary.notFound} not found · ~
                  {summary.peopleLinked.toLocaleString()} people linked
                  {ingesting ? ' (running…)' : ''}
                </Typography>
              )}
            </Stack>
          </StepContent>
        </Step>

        <Step expanded={activeStep >= 2}>
          <StepLabel>Search the set</StepLabel>
          <StepContent>
            <Stack spacing={2} sx={{ pt: 1 }}>
              <Typography variant="body2" color="text.secondary">
                Semantic search over the people you just ingested. Company chips are pre-filled from
                the run; edit them freely.
              </Typography>
              {stageError && <Alert severity="error">{stageError}</Alert>}
              <SimilarityForm
                query={query}
                onQueryChange={setQuery}
                embeds={simEmbeds}
                onEmbedsChange={setSimEmbeds}
                companyOptions={companyOptions}
                companies={simCompanies}
                onCompaniesChange={setSimCompanies}
                onAddCompany={(raw) => void addCompanyValue(raw)}
                resolvingCompany={resolvingCompany}
                companyResolveError={companyResolveError}
                emailFilter={simEmailFilter}
                onEmailFilterChange={setSimEmailFilter}
                phoneFilter={simPhoneFilter}
                onPhoneFilterChange={setSimPhoneFilter}
                linkedinFilter={simLinkedinFilter}
                onLinkedinFilterChange={setSimLinkedinFilter}
                limit={similarityLimit}
                onLimitChange={setSimilarityLimit}
              />
              <Box>
                <Button
                  data-testid="prospect-run-search"
                  variant="contained"
                  size="large"
                  startIcon={
                    searching ? <CircularProgress size={18} color="inherit" /> : <SearchIcon />
                  }
                  disabled={searching || !canRunSearch}
                  onClick={() => void runStageSearch()}
                  sx={{ px: 3 }}
                >
                  {searching ? 'Searching…' : 'Search saved leads'}
                </Button>
              </Box>
            </Stack>
          </StepContent>
        </Step>
      </Stepper>
    </Stack>
  )
}
