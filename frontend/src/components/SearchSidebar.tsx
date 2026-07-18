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
  FormControlLabel,
  IconButton,
  List,
  ListItem,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Typography,
} from '@mui/material'
import { useCallback, useEffect, useRef, useState, memo, type MutableRefObject } from 'react'
import { isEditableTarget } from '../keyboard'
import type { ListFocus } from '../keyboard'
import { toggleOrRangeSelect } from '../rangeSelect'
import type { SearchHistorySummary } from '../types'

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

const HistoryRow = memo(function HistoryRow({
  item,
  selected,
  checked,
  onSelect,
  onToggleChecked,
  onShiftSelect,
}: {
  item: SearchHistorySummary
  selected: boolean
  checked: boolean
  onSelect: (id: number) => void
  onToggleChecked: (id: number, shiftKey: boolean) => void
  onShiftSelect: (id: number) => void
}) {
  return (
    <ListItem disablePadding>
      <Checkbox
        className="history-checkbox"
        edge="start"
        size="small"
        checked={checked}
        tabIndex={-1}
        disableRipple
        slotProps={{
          input: { 'aria-label': `Select search “${item.query}”` },
        }}
        onClick={(event) => {
          event.preventDefault()
          event.stopPropagation()
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
          secondary={`${item.entity_type} · ${item.total_results.toLocaleString()} · ${formatWhen(item.created_at)}`}
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
        return next
      })
    },
    [items],
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

  function requestDelete(ids: number[]) {
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
    pendingDeleteIds,
    selectedId,
  ])

  const checkedCount = checkedIds.size

  function toggleSelectAll() {
    if (checkedCount > 0) setCheckedIds(new Set())
    else setCheckedIds(new Set(items.map((item) => item.id)))
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
            size="small"
            variant={actionsOpen ? 'outlined' : 'text'}
            onClick={() => (actionsOpen ? closeActions() : setActionsOpen(true))}
            sx={{ textTransform: 'none', minWidth: 0 }}
          >
            {actionsOpen ? 'Done' : 'Actions'}
          </Button>
        )}
        {onClose && (
          <IconButton aria-label="Hide search history" size="small" onClick={onClose}>
            <CloseIcon fontSize="small" />
          </IconButton>
        )}
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
          <Button size="small" onClick={toggleSelectAll} sx={{ textTransform: 'none', minWidth: 0 }}>
            {checkedCount > 0 ? 'Deselect all' : 'Select all'}
          </Button>
          {checkedCount > 0 && (
            <>
              <Typography variant="body2" sx={{ flex: 1, fontWeight: 600 }}>
                {checkedCount} selected
              </Typography>
              <IconButton
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
          <Button onClick={cancelPendingDelete} sx={{ textTransform: 'none' }}>
            Cancel
          </Button>
          <Button
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
