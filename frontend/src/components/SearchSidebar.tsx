import BusinessIcon from '@mui/icons-material/Business'
import CloseIcon from '@mui/icons-material/Close'
import DeleteIcon from '@mui/icons-material/Delete'
import HistoryIcon from '@mui/icons-material/History'
import PersonSearchIcon from '@mui/icons-material/PersonSearch'
import {
  Box,
  Button,
  Checkbox,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  FormControl,
  FormControlLabel,
  IconButton,
  InputLabel,
  List,
  ListItem,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  MenuItem,
  Select,
  Typography,
} from '@mui/material'
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  memo,
  type MutableRefObject,
} from 'react'
import { isEditableTarget } from '../keyboard'
import type { ListFocus } from '../keyboard'
import { toggleOrRangeSelect } from '../rangeSelect'
import type { HistoryOwner, SearchHistorySummary } from '../types'

/** Owner-filter sentinel matching SearchPage: browse every user's history. */
const ALL_OWNERS = ''

/** Kebab-case a username into a stable testid qualifier (e.g. owner "alice" →
 * `search-owner-alice`). Usernames are admin-controlled and effectively
 * punctuation-free, so slug collisions are not a concern here; there is no
 * stable owner id to prefer (HistoryOwner carries only username + count). */
function slug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}

/** Session-scoped: skip history delete confirmation after the user opts out. */
let skipHistoryDeleteConfirm = false

interface SearchSidebarProps {
  items: SearchHistorySummary[]
  selectedId: number | null
  onSelect: (id: number) => void
  onDelete: (ids: number[]) => void
  onClose?: () => void
  listFocusRef: MutableRefObject<ListFocus>
  onActivate?: () => void
  /** Distinct history owners (with counts) for the owner filter. */
  owners: HistoryOwner[]
  /** Selected owner username, or ALL_OWNERS ('') for every user. */
  selectedOwner: string
  onSelectOwner: (owner: string) => void
  /** Current session user — only their own rows are deletable (server owner-gate). */
  currentUsername: string
}

function formatWhen(iso: string) {
  try {
    return new Intl.DateTimeFormat(undefined, {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    }).format(new Date(iso))
  } catch {
    return iso
  }
}

function deleteConfirmMessage(items: SearchHistorySummary[], ids: number[]) {
  if (ids.length === 1) {
    const item = items.find((entry) => entry.id === ids[0])
    const label = item?.query ? `“${item.query}”` : 'this search'
    return `Delete ${label} from history? This cannot be undone.`
  }
  return `Delete ${ids.length} searches from history? This cannot be undone.`
}

/** Owner/origin annotation for a row's secondary text. Shows the owner name
 * when the row isn't the current user's, and "(via agent)" for agent-run
 * searches — so rows read "alice" or "alice (via agent)". */
function ownerAnnotation(item: SearchHistorySummary, currentUsername: string): string | null {
  const isAgent = item.origin === 'agent'
  const isOwn = item.username === currentUsername
  if (isOwn && !isAgent) return null
  return `${item.username}${isAgent ? ' (via agent)' : ''}`
}

const HistoryRow = memo(function HistoryRow({
  item,
  selected,
  checked,
  canDelete,
  currentUsername,
  onSelect,
  onToggleChecked,
  onShiftSelect,
}: {
  item: SearchHistorySummary
  selected: boolean
  checked: boolean
  /** False for rows owned by another user — the server would 404 a delete. */
  canDelete: boolean
  currentUsername: string
  onSelect: (id: number) => void
  onToggleChecked: (id: number, shiftKey: boolean) => void
  onShiftSelect: (id: number) => void
}) {
  const owner = ownerAnnotation(item, currentUsername)
  const secondary = [
    owner,
    item.entity_type,
    item.total_results.toLocaleString(),
    formatWhen(item.created_at),
  ]
    .filter(Boolean)
    .join(' · ')
  return (
    <ListItem disablePadding>
      <Checkbox
        className="history-checkbox"
        edge="start"
        size="small"
        checked={checked}
        disabled={!canDelete}
        tabIndex={-1}
        disableRipple
        slotProps={{
          input: {
            'aria-label': canDelete
              ? `Select search “${item.query}”`
              : `Cannot delete “${item.query}” — owned by ${item.username}`,
          },
        }}
        onClick={(event) => {
          event.preventDefault()
          event.stopPropagation()
          if (!canDelete) return
          onToggleChecked(item.id, event.shiftKey)
        }}
        sx={{ ml: 1, mr: 0 }}
      />
      <ListItemButton
        data-history-id={item.id}
        selected={selected}
        onClick={(event) => {
          if (event.shiftKey) onShiftSelect(item.id)
          onSelect(item.id)
        }}
        sx={{ flex: 1 }}
      >
        <ListItemIcon sx={{ minWidth: 36 }}>
          {item.entity_type === 'companies' ? (
            <BusinessIcon fontSize="small" />
          ) : (
            <PersonSearchIcon fontSize="small" />
          )}
        </ListItemIcon>
        <ListItemText
          primary={item.query}
          secondary={secondary}
          slotProps={{
            primary: { noWrap: true, sx: { fontWeight: 600 } },
            secondary: { noWrap: true },
          }}
        />
      </ListItemButton>
    </ListItem>
  )
})

export const SearchSidebar = memo(function SearchSidebar({
  items,
  selectedId,
  onSelect,
  onDelete,
  onClose,
  listFocusRef,
  onActivate,
  owners,
  selectedOwner,
  onSelectOwner,
  currentUsername,
}: SearchSidebarProps) {
  const [actionsOpen, setActionsOpen] = useState(false)
  const [checkedIds, setCheckedIds] = useState<Set<number>>(new Set())
  const [pendingDeleteIds, setPendingDeleteIds] = useState<number[] | null>(null)
  const [dontAskAgain, setDontAskAgain] = useState(false)
  const listRef = useRef<HTMLUListElement | null>(null)
  const checkAnchorRef = useRef<number | null>(null)
  const actionsOpenRef = useRef(actionsOpen)
  const pendingDeleteIdsRef = useRef<number[] | null>(null)
  pendingDeleteIdsRef.current = pendingDeleteIds
  actionsOpenRef.current = actionsOpen

  // Only the current user's own rows are deletable (the server owner-gates
  // DELETE and 404s otherwise), so gate the delete affordances to them.
  const ownedIdSet = useMemo(
    () => new Set(items.filter((item) => item.username === currentUsername).map((item) => item.id)),
    [items, currentUsername],
  )
  const ownedCount = ownedIdSet.size

  // Owner filter options: backend owners (with counts) plus the current user
  // and the active selection, so the Select value is always in range.
  const ownerOptions = useMemo(() => {
    const counts = new Map<string, number>()
    for (const owner of owners) counts.set(owner.username, owner.search_count)
    if (currentUsername && !counts.has(currentUsername)) counts.set(currentUsername, 0)
    if (selectedOwner && !counts.has(selectedOwner)) counts.set(selectedOwner, 0)
    return [...counts.entries()].map(([username, count]) => ({ username, count }))
  }, [owners, currentUsername, selectedOwner])

  useEffect(() => {
    const validIds = new Set(items.map((item) => item.id))
    setCheckedIds((prev) => {
      const next = new Set([...prev].filter((id) => validIds.has(id)))
      if (next.size === prev.size) return prev
      return next
    })
  }, [items])

  useEffect(() => {
    if (!items.length && actionsOpen) {
      setActionsOpen(false)
      setCheckedIds(new Set())
      checkAnchorRef.current = null
    }
  }, [items.length, actionsOpen])

  function closeActions() {
    setActionsOpen(false)
    setCheckedIds(new Set())
    checkAnchorRef.current = null
  }

  const toggleChecked = useCallback(
    (id: number, shiftKey = false) => {
      const orderedIds = items.map((item) => item.id)
      setCheckedIds((prev) => {
        const { next, nextAnchor } = toggleOrRangeSelect(
          orderedIds,
          prev,
          id,
          checkAnchorRef.current,
          shiftKey,
        )
        checkAnchorRef.current = nextAnchor
        // A shift-range can span non-owned rows between two owned endpoints;
        // keep only deletable ids checked so the count and checkbox state match
        // what can actually be deleted (delete is already owned-gated).
        return new Set([...next].filter((checkedId) => ownedIdSet.has(checkedId)))
      })
    },
    [items, ownedIdSet],
  )

  const shiftSelect = useCallback(
    (id: number) => {
      if (!actionsOpenRef.current) return
      toggleChecked(id, true)
    },
    [toggleChecked],
  )

  const handleRowSelect = useCallback(
    (id: number) => {
      onActivate?.()
      onSelect(id)
    },
    [onActivate, onSelect],
  )

  function requestDelete(requested: number[]) {
    // Drop rows the current user doesn't own — the server would 404 them.
    const ids = requested.filter((id) => ownedIdSet.has(id))
    if (!ids.length) return
    if (skipHistoryDeleteConfirm) {
      onDelete(ids)
      return
    }
    setDontAskAgain(false)
    setPendingDeleteIds(ids)
  }

  function confirmPendingDelete() {
    const ids = pendingDeleteIdsRef.current
    if (!ids?.length) return
    pendingDeleteIdsRef.current = null
    if (dontAskAgain) skipHistoryDeleteConfirm = true
    setPendingDeleteIds(null)
    setDontAskAgain(false)
    onDelete(ids)
  }

  function cancelPendingDelete() {
    setPendingDeleteIds(null)
    setDontAskAgain(false)
  }

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (listFocusRef.current !== 'history') return
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return

      if (pendingDeleteIds) {
        if (event.key === 'Enter') {
          event.preventDefault()
          confirmPendingDelete()
        }
        return
      }

      if (isEditableTarget(event.target)) return
      if (items.length === 0) return

      const key = event.key.toLowerCase()
      if (key === 'a') {
        event.preventDefault()
        if (actionsOpen) closeActions()
        else setActionsOpen(true)
        return
      }
      if (key === 's') {
        if (!actionsOpen || selectedId === null) return
        event.preventDefault()
        if (!ownedIdSet.has(selectedId)) return
        toggleChecked(selectedId)
        return
      }
      if (key === 'd' || (key === 'delete' && !actionsOpen)) {
        const ids =
          actionsOpen && checkedIds.size > 0
            ? [...checkedIds]
            : selectedId !== null
              ? [selectedId]
              : []
        if (!ids.length) return
        event.preventDefault()
        requestDelete(ids)
        return
      }
      if (key !== 'j' && key !== 'k') return

      const currentIndex = selectedId
        ? items.findIndex((item) => item.id === selectedId)
        : 0
      const safeIndex = currentIndex < 0 ? 0 : currentIndex
      const nextIndex =
        key === 'j'
          ? Math.min(safeIndex + 1, items.length - 1)
          : Math.max(safeIndex - 1, 0)

      if (nextIndex === safeIndex && selectedId === items[nextIndex]?.id) return

      event.preventDefault()
      const nextId = items[nextIndex].id
      onSelect(nextId)

      const item = listRef.current?.querySelector<HTMLElement>(
        `[data-history-id="${nextId}"]`,
      )
      item?.scrollIntoView({ block: 'nearest' })
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [
    actionsOpen,
    checkedIds,
    items,
    listFocusRef,
    onSelect,
    ownedIdSet,
    pendingDeleteIds,
    selectedId,
  ])

  const checkedCount = checkedIds.size

  function toggleSelectAll() {
    // Select-all only applies to rows the current user can actually delete.
    if (checkedCount > 0) setCheckedIds(new Set())
    else setCheckedIds(new Set(ownedIdSet))
  }

  return (
    <Box
      className="pane-history"
      onPointerDown={() => onActivate?.()}
      sx={{
        width: { xs: '100%', md: 300 },
        height: '100%',
        flexShrink: 0,
        borderRight: { md: '1px solid' },
        borderColor: 'divider',
        display: 'flex',
        flexDirection: 'column',
        minHeight: 0,
        overflow: 'hidden',
        '& .history-checkbox': {
          display: actionsOpen ? 'inline-flex' : 'none',
        },
      }}
    >
      <Box sx={{ px: 2, py: 2, display: 'flex', alignItems: 'center', gap: 1, flexShrink: 0 }}>
        <HistoryIcon fontSize="small" color="primary" />
        <Typography variant="subtitle1" sx={{ fontWeight: 700, flex: 1 }}>
          Search history
        </Typography>
        {items.length > 0 && (
          <Button
            data-testid="search-history-actions-toggle"
            size="small"
            variant={actionsOpen ? 'outlined' : 'text'}
            onClick={() => (actionsOpen ? closeActions() : setActionsOpen(true))}
            sx={{ textTransform: 'none', minWidth: 0 }}
          >
            {actionsOpen ? 'Done' : 'Actions'}
          </Button>
        )}
        {onClose && (
          <IconButton data-testid="search-history-close" aria-label="Hide search history" size="small" onClick={onClose}>
            <CloseIcon fontSize="small" />
          </IconButton>
        )}
      </Box>

      <Box sx={{ px: 2, pb: 1.5, flexShrink: 0 }}>
        <FormControl size="small" fullWidth>
          <InputLabel id="history-owner-label">Owner</InputLabel>
          <Select
            labelId="history-owner-label"
            label="Owner"
            value={selectedOwner}
            onChange={(event) => onSelectOwner(event.target.value)}
          >
            <MenuItem data-testid="search-owner-all" value={ALL_OWNERS}>All users</MenuItem>
            {ownerOptions.map((owner) => (
              <MenuItem data-testid={`search-owner-${slug(owner.username)}`} key={owner.username} value={owner.username}>
                {owner.username === currentUsername ? `${owner.username} (you)` : owner.username}
                {` · ${owner.count.toLocaleString()}`}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
      </Box>

      {actionsOpen && items.length > 0 && (
        <Box
          sx={{
            px: 1.5,
            py: 0.75,
            mx: 1.5,
            mb: 1,
            flexShrink: 0,
            display: 'flex',
            alignItems: 'center',
            gap: 1,
            borderRadius: 1,
            bgcolor: (t) =>
              checkedCount > 0
                ? t.palette.mode === 'dark'
                  ? 'rgba(92, 184, 154, 0.12)'
                  : 'rgba(11, 61, 46, 0.08)'
                : 'transparent',
          }}
        >
          <Button
            data-testid="search-history-select-all"
            size="small"
            onClick={toggleSelectAll}
            disabled={checkedCount === 0 && ownedCount === 0}
            sx={{ textTransform: 'none', minWidth: 0 }}
          >
            {checkedCount > 0 ? 'Deselect all' : 'Select all'}
          </Button>
          {checkedCount > 0 && (
            <>
              <Typography variant="body2" sx={{ flex: 1, fontWeight: 600 }}>
                {checkedCount} selected
              </Typography>
              <IconButton
                data-testid="search-history-delete"
                aria-label="Delete selected searches"
                size="small"
                color="error"
                onClick={() => requestDelete([...checkedIds])}
              >
                <DeleteIcon fontSize="small" />
              </IconButton>
            </>
          )}
        </Box>
      )}

      <List dense sx={{ overflow: 'auto', flex: 1, minHeight: 0, pb: 2 }} ref={listRef}>
        {items.map((item) => (
          <HistoryRow
            key={item.id}
            item={item}
            selected={selectedId === item.id}
            checked={checkedIds.has(item.id)}
            canDelete={ownedIdSet.has(item.id)}
            currentUsername={currentUsername}
            onSelect={handleRowSelect}
            onToggleChecked={toggleChecked}
            onShiftSelect={shiftSelect}
          />
        ))}
        {!items.length && (
          <Box sx={{ px: 2, py: 1 }}>
            <Typography variant="body2" color="text.secondary">
              Past Apollo searches will appear here. Click one to open the results.
            </Typography>
          </Box>
        )}
      </List>

      <Dialog
        open={pendingDeleteIds !== null}
        onClose={cancelPendingDelete}
        aria-labelledby="delete-history-title"
      >
        <DialogTitle id="delete-history-title">Delete from history?</DialogTitle>
        <DialogContent>
          <DialogContentText>
            {pendingDeleteIds ? deleteConfirmMessage(items, pendingDeleteIds) : null}
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
          <Button data-testid="search-history-delete-cancel" onClick={cancelPendingDelete} sx={{ textTransform: 'none' }}>
            Cancel
          </Button>
          <Button
            data-testid="search-history-delete-confirm"
            color="error"
            variant="contained"
            onClick={confirmPendingDelete}
            autoFocus
            sx={{ textTransform: 'none' }}
          >
            Delete
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  )
})
