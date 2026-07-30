import ArrowBackIcon from '@mui/icons-material/ArrowBack'
import HistoryIcon from '@mui/icons-material/History'
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined'
import LogoutIcon from '@mui/icons-material/Logout'
import MenuOpenIcon from '@mui/icons-material/MenuOpen'
import SearchIcon from '@mui/icons-material/Search'
import {
  Alert,
  AppBar,
  Box,
  Button,
  CircularProgress,
  Divider,
  FormControl,
  IconButton,
  InputLabel,
  List,
  ListItem,
  ListItemText,
  MenuItem,
  Popover,
  Select,
  Stack,
  TextField,
  Toolbar,
  Typography,
} from '@mui/material'
import { alpha, useTheme } from '@mui/material/styles'
import useMediaQuery from '@mui/material/useMediaQuery'
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import {
  deleteSearch,
  fetchSearchPage,
  getApolloCredits,
  getSearch,
  listHistoryOwners,
  listSearches,
  runSearch,
  runSimilaritySearch,
  type RunSearchParams,
} from '../api'
import { useAuth } from '../auth'
import { ColorModeToggle } from '../components/ColorModeToggle'
import { SearchResultsView } from '../components/SearchResultsView'
import { SearchSidebar } from '../components/SearchSidebar'
import { isEditableTarget, type ListFocus } from '../keyboard'
import { listFocusPaneSx } from '../paneSurface'
import { useProgress } from '../progress'
import type {
  ApolloRecord,
  EntityType,
  HistoryOwner,
  SearchHistoryDetail,
  SearchHistorySummary,
} from '../types'

/** Owner-filter sentinel: browse every user's search history at once. */
const ALL_OWNERS = ''

type CompanyFilterMode = 'id' | 'name'
type MainView = 'search' | 'results'
type SearchSource = 'apollo' | 'similarity'

const SIMILARITY_LIMIT_MAX = 10000

export function SearchPage() {
  const theme = useTheme()
  const isMdUp = useMediaQuery(theme.breakpoints.up('md'))
  const { user, logout } = useAuth()

  useEffect(() => {
    document.title = 'Search — Funnel Manager'
  }, [])
  const [searchSource, setSearchSource] = useState<SearchSource>('apollo')
  const [query, setQuery] = useState('')
  const [similarityLimit, setSimilarityLimit] = useState(25)
  const [companyDomainQuery, setCompanyDomainQuery] = useState('')
  const [entityType, setEntityType] = useState<EntityType>('people')
  const [companyFilterMode, setCompanyFilterMode] = useState<CompanyFilterMode>('name')
  const [companyFilterValue, setCompanyFilterValue] = useState('')
  const [companyDisplayName, setCompanyDisplayName] = useState('')
  const [companyDomainForPeople, setCompanyDomainForPeople] = useState('')
  const [history, setHistory] = useState<SearchHistorySummary[]>([])
  const [owners, setOwners] = useState<HistoryOwner[]>([])
  // Which owner's history to browse; defaults to the current user (prior
  // behaviour), ALL_OWNERS shows every user's history (Principle 1).
  const [selectedOwner, setSelectedOwner] = useState<string>(user?.username ?? ALL_OWNERS)
  // Latest-value ref so refreshHistory (stable, so stream callbacks always
  // fetch the *current* selection, not the one bound at search-start) and the
  // monotonic guard below can read the live owner. Set during render.
  const selectedOwnerRef = useRef(selectedOwner)
  selectedOwnerRef.current = selectedOwner
  // Monotonic token: a slower earlier fetch (e.g. alice) must not overwrite a
  // newer one (bob) that already resolved. Bump before each await, discard
  // stale results.
  const historyRequestSeqRef = useRef(0)
  const [activeSearch, setActiveSearch] = useState<SearchHistoryDetail | null>(null)
  const [mainView, setMainView] = useState<MainView>('search')
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [listFocus, setListFocusState] = useState<ListFocus>('history')
  const listFocusRef = useRef<ListFocus>(listFocus)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [loadingHistory, setLoadingHistory] = useState(true)
  const [loadingResults, setLoadingResults] = useState(false)
  const [searching, setSearching] = useState(false)
  const [paging, setPaging] = useState(false)
  const [pageError, setPageError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [apolloCreditsRemaining, setApolloCreditsRemaining] = useState<number | null>(null)
  const [sessionTokensUsed, setSessionTokensUsed] = useState(0)
  const previousApolloCreditsRef = useRef<number | null>(null)
  const [hotkeysAnchor, setHotkeysAnchor] = useState<HTMLElement | null>(null)
  const progress = useProgress()
  // Latest active search, readable from stable callbacks (e.g. post-enrich refresh).
  const activeSearchRef = useRef<SearchHistoryDetail | null>(activeSearch)
  useEffect(() => {
    activeSearchRef.current = activeSearch
  }, [activeSearch])

  // Stable across owner changes and reads the live selection from the ref, so
  // the stream callbacks (onFirstPage/onComplete) that captured it at
  // search-start still fetch the *currently* selected owner. The seq guard
  // drops out-of-order responses from rapid owner switches.
  const refreshHistory = useCallback(async () => {
    const seq = ++historyRequestSeqRef.current
    const items = await listSearches(selectedOwnerRef.current || undefined)
    // A newer refresh started while we awaited — its result wins.
    if (seq !== historyRequestSeqRef.current) return
    setHistory(items)
  }, [])

  const refreshOwners = useCallback(async () => {
    try {
      setOwners(await listHistoryOwners())
    } catch {
      // Owner filter is a convenience; leave the last-known list on failure.
    }
  }, [])

  const refreshApolloCredits = useCallback(async () => {
    try {
      const credits = await getApolloCredits()
      const remaining = credits.credits_remaining
      if (remaining != null) {
        const previous = previousApolloCreditsRef.current
        if (previous != null && remaining < previous) {
          setSessionTokensUsed((used) => used + (previous - remaining))
        }
        previousApolloCreditsRef.current = remaining
      }
      setApolloCreditsRemaining(remaining)
      return credits
    } catch {
      // Keep last known balance if Apollo credits are unavailable.
      return null
    }
  }, [])

  // Loads on mount and whenever the selected owner changes. Don't flip
  // loadingHistory on owner switches: that swaps the whole sidebar (including
  // the owner dropdown) for a spinner mid-interaction. The initial full-sidebar
  // spinner comes from the initial loadingHistory=true; subsequent switches
  // keep the list mounted and swap in place when the new owner's history
  // arrives (out-of-order responses are dropped by refreshHistory's seq guard).
  useEffect(() => {
    refreshHistory()
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load history'))
      .finally(() => setLoadingHistory(false))
  }, [selectedOwner, refreshHistory])

  useEffect(() => {
    void refreshOwners()
  }, [refreshOwners])

  useEffect(() => {
    void refreshApolloCredits()
  }, [refreshApolloCredits])

  const setListFocus = useCallback((next: ListFocus) => {
    listFocusRef.current = next
    setListFocusState(next)
  }, [])

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return
      if (isEditableTarget(event.target)) return

      const key = event.key.toLowerCase()
      if (key !== 'h' && key !== 'l') return

      if (key === 'h') {
        if (!sidebarOpen) return
        event.preventDefault()
        setListFocus('history')
        return
      }

      if (mainView !== 'results' || !activeSearch) return
      event.preventDefault()
      setListFocus('results')
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [activeSearch, mainView, setListFocus, sidebarOpen])

  const companyValue = companyFilterValue.trim()
  const companyDomainValue = companyDomainQuery.trim()
  const isSimilaritySource = searchSource === 'similarity'
  const canSubmit = isSimilaritySource
    ? Boolean(query.trim()) && similarityLimit >= 1
    : entityType === 'companies'
      ? Boolean(companyDomainValue)
      : Boolean(companyValue)

  function showResults(detail: SearchHistoryDetail) {
    setActiveSearch(detail)
    setSelectedId(detail.id)
    setMainView('results')
    setListFocus('results')
    setPageError(null)
  }

  function backToSearch() {
    setMainView('search')
    setListFocus('history')
    setPageError(null)
  }

  async function executeSimilaritySearch() {
    const passage = query.trim()
    if (!passage) {
      setError('Enter a passage to search saved people')
      return
    }
    const limit = Math.min(
      SIMILARITY_LIMIT_MAX,
      Math.max(1, Math.round(Number(similarityLimit) || 1)),
    )
    setSimilarityLimit(limit)
    setError(null)
    setSearching(true)
    try {
      const response = await runSimilaritySearch({
        query: passage,
        limit,
      })
      showResults(response.history)
      await refreshHistory()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Similarity search failed')
      setMainView('search')
    } finally {
      setSearching(false)
    }
  }

  async function executeSearch(params: RunSearchParams) {
    setError(null)
    setSearching(true)
    const run = progress.beginRun()
    let shown = false
    let historyId: number | null = null

    function applyStoredCount(stored: number) {
      setActiveSearch((prev) =>
        prev && shown
          ? { ...prev, total_results: Math.max(prev.total_results, stored) }
          : prev,
      )
      if (historyId == null) return
      setHistory((prev) =>
        prev.map((item) =>
          item.id === historyId
            ? { ...item, total_results: Math.max(item.total_results, stored) }
            : item,
        ),
      )
    }

    try {
      const response = await runSearch(params, {
        onProgress: (event) => {
          if (event.total_pages > 0) {
            run.reportIngest({
              done: event.page,
              total: event.total_pages,
              streamId: event.ingest_stream_id,
            })
          }
          // Grow results header + sidebar count as more ids land.
          applyStoredCount(event.stored)
        },
        onEmbeddingProgress: (event) => {
          if (event.complete || event.error) {
            run.reportEmbed({ complete: true, streamId: event.embedding_stream_id })
            return
          }
          run.reportEmbed({
            done: event.done,
            total: event.total,
            streamId: event.embedding_stream_id,
          })
        },
        onFirstPage: (pageReady) => {
          shown = true
          historyId = pageReady.history.id
          showResults(pageReady.history)
          // Ensure the sidebar row exists immediately, then keep count in sync.
          setHistory((prev) => {
            const h = pageReady.history
            const idx = prev.findIndex((item) => item.id === h.id)
            if (idx >= 0) {
              return prev.map((item, i) =>
                i === idx
                  ? { ...item, total_results: Math.max(item.total_results, h.total_results) }
                  : item,
              )
            }
            return [
              {
                id: h.id,
                query: h.query,
                entity_type: h.entity_type,
                page: h.page,
                per_page: h.per_page,
                total_results: h.total_results,
                created_at: h.created_at,
                username: h.username,
                origin: h.origin,
                actor: h.actor,
              },
              ...prev,
            ]
          })
          // Unblock the results pane immediately; background ingest may continue.
          setSearching(false)
          setLoadingResults(false)
          void refreshHistory()
        },
        onComplete: (done) => {
          historyId = done.history.id
          showResults(done.history)
          applyStoredCount(done.history.total_results)
          setSearching(false)
          run.reportIngest({ complete: true })
          void refreshHistory()
          void refreshOwners()
        },
      })
      showResults(response.history)
      await refreshHistory()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Search failed')
      if (!shown) setMainView('search')
    } finally {
      setSearching(false)
      setLoadingResults(false)
      run.end()
    }
  }

  function buildSearchParams(): RunSearchParams | null {
    if (!canSubmit) return null
    if (entityType === 'companies') {
      return {
        query: '',
        entity_type: 'companies',
        page: 1,
        per_page: 100,
        ...(companyDomainValue ? { company_domain: companyDomainValue } : {}),
      }
    }
    return {
      query: '',
      entity_type: 'people',
      page: 1,
      per_page: 100,
      ...(companyValue
        ? companyFilterMode === 'id'
          ? {
              organization_id: companyValue,
              ...(companyDisplayName.trim()
                ? { organization_display_name: companyDisplayName.trim() }
                : {}),
              ...(companyDomainForPeople.trim()
                ? { organization_domain: companyDomainForPeople.trim() }
                : {}),
            }
          : { organization_name: companyValue }
        : {}),
    }
  }

  async function onSearch(e: FormEvent) {
    e.preventDefault()
    if (isSimilaritySource) {
      await executeSimilaritySearch()
      return
    }
    const params = buildSearchParams()
    if (!params) return
    await executeSearch(params)
  }

  const openHistoryItem = useCallback(async (id: number) => {
    setError(null)
    setLoadingResults(true)
    setSelectedId(id)
    try {
      const detail = await getSearch(id)
      setActiveSearch(detail)
      setSelectedId(detail.id)
      setMainView('results')
      // Keep list focus where it is (history j/k must not jump to results).
      setPageError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load search')
      setMainView('search')
    } finally {
      setLoadingResults(false)
    }
  }, [])

  const removeHistoryItems = useCallback(async (ids: number[]) => {
    if (!ids.length) return
    setError(null)
    try {
      await Promise.all(ids.map((id) => deleteSearch(id)))
      if (selectedId !== null && ids.includes(selectedId)) {
        setSelectedId(null)
        setActiveSearch(null)
        setMainView('search')
      }
      await refreshHistory()
      void refreshOwners()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete search')
    }
  }, [refreshHistory, refreshOwners, selectedId])

  const closeSidebar = useCallback(() => setSidebarOpen(false), [])

  const changeSearchPage = useCallback(async (page: number) => {
    if (!activeSearch) return
    setPageError(null)
    setPaging(true)
    try {
      const response = await fetchSearchPage(activeSearch.id, page)
      setActiveSearch(response.history)
      setSelectedId(response.history.id)
      await refreshHistory()
    } catch (err) {
      setPageError(err instanceof Error ? err.message : 'Failed to load page')
    } finally {
      setPaging(false)
    }
  }, [activeSearch, refreshHistory])

  async function useCompanyForPeopleSearch(
    organizationId: string,
    companyName: string,
    companyDomain?: string | null,
  ) {
    const domain = (companyDomain || '').trim()
    setEntityType('people')
    setCompanyFilterMode('id')
    setCompanyFilterValue(organizationId)
    setCompanyDisplayName(companyName)
    setCompanyDomainForPeople(domain)
    setQuery('')
    // Show a short loading state until first_page; executeSearch clears it there
    // so a large company ingest does not keep the results pane spinning.
    setLoadingResults(true)
    await executeSearch({
      query: '',
      entity_type: 'people',
      page: 1,
      per_page: 100,
      organization_id: organizationId,
      organization_display_name: companyName,
      ...(domain ? { organization_domain: domain } : {}),
    })
  }

  const showingResults = mainView === 'results'
  const activateHistory = useCallback(() => setListFocus('history'), [setListFocus])
  const activateResults = useCallback(() => setListFocus('results'), [setListFocus])

  const handleRecordsUpdated = useCallback((records: ApolloRecord[]) => {
    if (!records.length) return
    setActiveSearch((prev) => {
      if (!prev) return prev
      const byApolloId = new Map(
        records
          .filter((record) => record.id != null && String(record.id) !== '')
          .map((record) => [String(record.id), record]),
      )
      const byMongoId = new Map(
        records
          .filter((record) => record.mongo_id != null && String(record.mongo_id) !== '')
          .map((record) => [String(record.mongo_id), record]),
      )
      return {
        ...prev,
        results: prev.results.map((existing) => {
          const byId =
            existing.id != null ? byApolloId.get(String(existing.id)) : undefined
          const byMongo =
            existing.mongo_id != null
              ? byMongoId.get(String(existing.mongo_id))
              : undefined
          return byId ?? byMongo ?? existing
        }),
      }
    })
  }, [])

  const refreshCurrentResults = useCallback(async () => {
    const current = activeSearchRef.current
    if (!current) return
    try {
      const response = await fetchSearchPage(current.id, current.page)
      setActiveSearch(response.history)
      setSelectedId(response.history.id)
    } catch {
      // Best-effort refresh; enriched records already patched in-place via onRecordsUpdated.
    }
  }, [])

  // Pending timers for webhook-delayed waterfall refreshes; cleared on unmount.
  const refreshTimersRef = useRef<ReturnType<typeof setTimeout>[]>([])
  useEffect(
    () => () => {
      refreshTimersRef.current.forEach(clearTimeout)
      refreshTimersRef.current = []
    },
    [],
  )

  const handleAfterEnrichment = useCallback(
    async (info?: { waterfallPending?: boolean }) => {
      await Promise.allSettled([refreshCurrentResults(), refreshApolloCredits()])
      // Email/phone waterfall results arrive later via an async Apollo webhook, so
      // schedule a few follow-up refreshes to pick them up without a manual reload.
      if (info?.waterfallPending) {
        for (const delay of [8000, 20000, 45000]) {
          const timer = setTimeout(() => {
            void refreshCurrentResults()
          }, delay)
          refreshTimersRef.current.push(timer)
        }
      }
    },
    [refreshCurrentResults, refreshApolloCredits],
  )

  const ingestPercent = progress.ingest.visible ? progress.ingest.percent : null
  const searchingLabel =
    searching && ingestPercent != null
      ? `Fetching ${ingestPercent}%`
      : searching
        ? 'Searching…'
        : isSimilaritySource
          ? 'Search saved leads'
          : 'Search Apollo'

  const sidebar = !sidebarOpen ? null : loadingHistory ? (
    <Box
      sx={{
        width: isMdUp ? 300 : '100%',
        height: isMdUp ? '100%' : undefined,
        minHeight: 0,
        display: 'grid',
        placeItems: 'center',
        py: isMdUp ? 0 : 2,
      }}
    >
      <CircularProgress size={isMdUp ? 28 : 24} />
    </Box>
  ) : (
    <SearchSidebar
      items={history}
      selectedId={selectedId}
      onSelect={openHistoryItem}
      onDelete={removeHistoryItems}
      onClose={closeSidebar}
      listFocusRef={listFocusRef}
      onActivate={activateHistory}
      owners={owners}
      selectedOwner={selectedOwner}
      onSelectOwner={setSelectedOwner}
      currentUsername={user?.username ?? ''}
    />
  )

  return (
    <Box
      data-list-focus={listFocus}
      sx={(theme) => ({
        // Self-contained full-viewport layout: the hub's global CSS scrolls
        // (min-height), so the search app pins itself to the viewport instead
        // of inheriting height from html/body.
        height: '100dvh',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        ...listFocusPaneSx(theme),
      })}
    >
      <AppBar
        position="static"
        elevation={0}
        color="transparent"
        sx={{
          flexShrink: 0,
          borderBottom: '1px solid',
          borderColor: 'divider',
          bgcolor: alpha(theme.palette.background.paper, 0.92),
          backdropFilter: 'blur(8px)',
        }}
      >
        <Toolbar sx={{ gap: 2 }}>
          <Box sx={{ pr: 1 }}>
            <Typography
              variant="overline"
              color={theme.palette.mode === 'dark' ? 'primary' : 'secondary'}
              sx={{ letterSpacing: 1.4 }}
            >
              Funnel Manager
            </Typography>
            <Typography variant="h6" sx={{ lineHeight: 1.1 }}>
              Apollo inspector
            </Typography>
          </Box>
          <Divider
            orientation="vertical"
            flexItem
            sx={{ my: 1.25, borderColor: 'divider' }}
          />
          <Stack direction="row" spacing={1} sx={{ alignItems: 'center', minWidth: 0 }}>
            {showingResults && (
              <Button
                data-testid="search-back"
                startIcon={<ArrowBackIcon />}
                onClick={backToSearch}
                color="inherit"
                variant="outlined"
              >
                Back to search
              </Button>
            )}
            <Button
              data-testid="search-history-toggle"
              startIcon={sidebarOpen ? <MenuOpenIcon /> : <HistoryIcon />}
              onClick={() => setSidebarOpen((open) => !open)}
              color="inherit"
              variant={sidebarOpen ? 'outlined' : 'text'}
            >
              {sidebarOpen ? 'Hide history' : 'Show history'}
            </Button>
          </Stack>
          <Box sx={{ flex: 1 }} />
          <Stack
            direction="row"
            spacing={1.5}
            sx={{
              alignItems: 'center',
              display: { xs: 'none', sm: 'flex' },
              mr: 0.5,
            }}
          >
            <Typography variant="body2" color="text.secondary" noWrap title="Remaining Apollo lead credits">
              Apollo:{' '}
              {apolloCreditsRemaining === null
                ? '—'
                : apolloCreditsRemaining.toLocaleString()}
            </Typography>
            <Typography
              variant="body2"
              color="text.secondary"
              noWrap
              title="Apollo credits used this session (from balance changes after enrichment)"
            >
              Session: {sessionTokensUsed.toLocaleString()}
            </Typography>
          </Stack>
          <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
            <Typography variant="body2" color="text.secondary" sx={{ display: { xs: 'none', sm: 'block' } }}>
              {user?.username}
            </Typography>
            <IconButton
              data-testid="search-shortcuts"
              size="small"
              color="inherit"
              aria-label="Keyboard shortcuts"
              aria-describedby={hotkeysAnchor ? 'hotkeys-popover' : undefined}
              onClick={(event) => setHotkeysAnchor(event.currentTarget)}
            >
              <InfoOutlinedIcon fontSize="small" />
            </IconButton>
            <Popover
              id="hotkeys-popover"
              open={Boolean(hotkeysAnchor)}
              anchorEl={hotkeysAnchor}
              onClose={() => setHotkeysAnchor(null)}
              anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
              transformOrigin={{ vertical: 'top', horizontal: 'right' }}
            >
              <Box sx={{ px: 1.5, pt: 1.25, pb: 0.5, minWidth: 220 }}>
                <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 0.5 }}>
                  Keyboard shortcuts
                </Typography>
                <List dense disablePadding>
                  {(
                    [
                      ['h', 'Focus history'],
                      ['l', 'Focus results'],
                      ['j', 'Next item'],
                      ['k', 'Previous item'],
                      ['a', 'Toggle Actions'],
                      ['s', 'Toggle selection'],
                      ['d', 'Delete history entry'],
                    ] as const
                  ).map(([key, label]) => (
                    <ListItem key={key} disableGutters sx={{ py: 0.15, gap: 1.25 }}>
                      <Typography
                        component="kbd"
                        variant="caption"
                        sx={{
                          minWidth: 18,
                          textAlign: 'center',
                          fontWeight: 700,
                          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                          px: 0.5,
                          border: '1px solid',
                          borderColor: 'divider',
                          borderRadius: 0.5,
                          bgcolor: 'action.hover',
                        }}
                      >
                        {key}
                      </Typography>
                      <ListItemText
                        primary={label}
                        slotProps={{
                          primary: { variant: 'body2', color: 'text.secondary' },
                        }}
                      />
                    </ListItem>
                  ))}
                </List>
              </Box>
            </Popover>
            <ColorModeToggle />
            <Button data-testid="search-open-hub" component="a" href="/" color="inherit" size="small">
              Hub
            </Button>
            <Button data-testid="search-signout" startIcon={<LogoutIcon />} onClick={logout} color="inherit">
              Log out
            </Button>
          </Stack>
        </Toolbar>
      </AppBar>

      <Box sx={{ flex: 1, display: 'flex', minHeight: 0, overflow: 'hidden' }}>
        {isMdUp && sidebar}

        {showingResults ? (
          <Box
            component="main"
            sx={{
              flex: 1,
              minWidth: 0,
              minHeight: 0,
              display: 'flex',
              flexDirection: 'column',
              overflow: 'hidden',
              bgcolor: 'background.paper',
            }}
          >
            {error && (
              <Box sx={{ p: 2, flexShrink: 0 }}>
                <Alert severity="error">{error}</Alert>
              </Box>
            )}
            {loadingResults ? (
              <Box sx={{ flex: 1, display: 'grid', placeItems: 'center' }}>
                <CircularProgress />
              </Box>
            ) : activeSearch ? (
              <Box sx={{ flex: 1, minHeight: 0, overflow: 'hidden' }}>
                <SearchResultsView
                  search={activeSearch}
                  onUseCompanyForPeopleSearch={useCompanyForPeopleSearch}
                  onChangePage={changeSearchPage}
                  paging={paging}
                  pageError={pageError}
                  listFocusRef={listFocusRef}
                  onActivate={activateResults}
                  onRecordsUpdated={handleRecordsUpdated}
                  onAfterEnrichment={handleAfterEnrichment}
                />
              </Box>
            ) : (
              <Box sx={{ p: 3 }}>
                <Typography color="text.secondary">No search selected.</Typography>
              </Box>
            )}
            {!isMdUp && sidebar && (
              <Box
                sx={{
                  borderTop: '1px solid',
                  borderColor: 'divider',
                  maxHeight: '40%',
                  minHeight: 0,
                  overflow: 'hidden',
                  display: 'flex',
                  flexDirection: 'column',
                }}
              >
                {sidebar}
              </Box>
            )}
          </Box>
        ) : (
          <Box
            component="main"
            sx={{
              flex: 1,
              minWidth: 0,
              minHeight: 0,
              overflow: 'auto',
              p: { xs: 2, md: 4 },
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              background: (t) =>
                `radial-gradient(circle at 90% 0%, ${alpha(
                  t.palette.mode === 'dark' ? t.palette.primary.main : t.palette.secondary.main,
                  t.palette.mode === 'dark' ? 0.18 : 0.12,
                )}, transparent 35%), linear-gradient(180deg, ${t.palette.background.paper} 0%, ${t.palette.background.default} 100%)`,
            }}
          >
            <Stack spacing={3} sx={{ width: '100%', maxWidth: 760 }}>
              <Box>
                <Typography variant="h4">
                  {isSimilaritySource ? 'Search saved leads' : 'Search Apollo'}
                </Typography>
                {isSimilaritySource && (
                  <Typography variant="body2" color="text.secondary" sx={{ mt: 0.75 }}>
                    Semantic search over people already stored in Mongo (via Milvus embeddings).
                  </Typography>
                )}
              </Box>

              {error && <Alert severity="error">{error}</Alert>}

              <Box
                component="form"
                onSubmit={onSearch}
                sx={{
                  p: 2.5,
                  border: '1px solid',
                  borderColor: 'divider',
                  borderRadius: 2,
                  bgcolor: 'background.paper',
                }}
              >
                <Stack spacing={2}>
                  <FormControl fullWidth>
                    <InputLabel id="search-source-label">Search source</InputLabel>
                    <Select
                      labelId="search-source-label"
                      label="Search source"
                      value={searchSource}
                      onChange={(e) => setSearchSource(e.target.value as SearchSource)}
                    >
                      <MenuItem data-testid="search-source-apollo" value="apollo">Apollo (live)</MenuItem>
                      <MenuItem data-testid="search-source-similarity" value="similarity">Saved leads (similarity)</MenuItem>
                    </Select>
                  </FormControl>

                  {isSimilaritySource ? (
                    <>
                      <TextField
                        label="Describe who you’re looking for"
                        placeholder="e.g. series A fintech CRO in NYC"
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                        fullWidth
                        autoFocus
                        multiline
                        minRows={2}
                      />
                      <TextField
                        label="Result limit"
                        type="number"
                        value={similarityLimit}
                        onChange={(e) => {
                          const next = Number(e.target.value)
                          if (!Number.isFinite(next)) return
                          setSimilarityLimit(
                            Math.min(SIMILARITY_LIMIT_MAX, Math.max(1, Math.round(next))),
                          )
                        }}
                        helperText="Results beyond 100 are paginated"
                        slotProps={{ htmlInput: { min: 1, max: SIMILARITY_LIMIT_MAX } }}
                        sx={{ maxWidth: 220 }}
                      />
                    </>
                  ) : entityType === 'people' ? (
                    <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
                      <FormControl sx={{ minWidth: 180 }}>
                        <InputLabel id="company-filter-mode-label">Company by</InputLabel>
                        <Select
                          labelId="company-filter-mode-label"
                          label="Company by"
                          value={companyFilterMode}
                          onChange={(e) => {
                            setCompanyFilterMode(e.target.value as CompanyFilterMode)
                            setCompanyDisplayName('')
                          }}
                        >
                          <MenuItem data-testid="search-company-by-name" value="name">Company name</MenuItem>
                          <MenuItem data-testid="search-company-by-id" value="id">Apollo company ID</MenuItem>
                        </Select>
                      </FormControl>
                      <TextField
                        label="Company"
                        placeholder={
                          companyFilterMode === 'id'
                            ? 'e.g. 5e66b6381e05b4008c8331b8'
                            : 'e.g. Stripe'
                        }
                        value={companyFilterValue}
                        onChange={(e) => {
                          setCompanyFilterValue(e.target.value)
                          if (companyFilterMode === 'id') {
                            setCompanyDisplayName('')
                            setCompanyDomainForPeople('')
                          }
                        }}
                        fullWidth
                        autoFocus
                      />
                    </Stack>
                  ) : (
                    <TextField
                      label="Domain"
                      placeholder="e.g. attio.com"
                      value={companyDomainQuery}
                      onChange={(e) => setCompanyDomainQuery(e.target.value)}
                      fullWidth
                      autoFocus
                    />
                  )}
                  <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
                    {!isSimilaritySource && (
                      <FormControl sx={{ minWidth: 180 }}>
                        <InputLabel id="entity-type-label">Record type</InputLabel>
                        <Select
                          labelId="entity-type-label"
                          label="Record type"
                          value={entityType}
                          onChange={(e) => setEntityType(e.target.value as EntityType)}
                        >
                          <MenuItem data-testid="search-entity-people" value="people">People</MenuItem>
                          <MenuItem data-testid="search-entity-companies" value="companies">Companies</MenuItem>
                        </Select>
                      </FormControl>
                    )}
                    <Button
                      data-testid="search-run"
                      type="submit"
                      variant="contained"
                      size="large"
                      startIcon={
                        searching ? (
                          <CircularProgress
                            size={18}
                            color="inherit"
                            variant={ingestPercent == null ? 'indeterminate' : 'determinate'}
                            value={ingestPercent ?? undefined}
                          />
                        ) : (
                          <SearchIcon />
                        )
                      }
                      disabled={searching || !canSubmit}
                      sx={{ px: 3 }}
                    >
                      {searchingLabel}
                    </Button>
                  </Stack>
                </Stack>
              </Box>

              {!isMdUp && sidebar}
            </Stack>
          </Box>
        )}
      </Box>
    </Box>
  )
}
