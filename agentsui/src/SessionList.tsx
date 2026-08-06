import AddIcon from '@mui/icons-material/Add'
import RefreshIcon from '@mui/icons-material/Refresh'
import {
  Box,
  Button,
  Chip,
  CircularProgress,
  Divider,
  IconButton,
  List,
  ListItemButton,
  ListItemText,
  MenuItem,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import { slug } from './slug'
import { attribution, formatDate, STATUS_COLOR } from './status'
import type { SessionSummary, User } from './types'

function StatusChip({ status }: { status: string }) {
  return <Chip label={status} size="small" color={STATUS_COLOR[status] ?? 'default'} variant="outlined" />
}

export function SessionList({
  user,
  sessions,
  owners,
  ownerFilter,
  onOwnerFilter,
  selectedId,
  onSelect,
  onNew,
  onRefresh,
  onLoadMore,
  hasMore,
  loadingMore,
}: {
  user: User
  sessions: SessionSummary[] | null
  owners: string[]
  ownerFilter: string
  onOwnerFilter: (owner: string) => void
  selectedId: string | null
  onSelect: (session: SessionSummary) => void
  onNew: () => void
  onRefresh: () => void
  onLoadMore: () => void
  hasMore: boolean
  loadingMore: boolean
}) {
  return (
    <Box
      sx={{
        width: 340,
        flexShrink: 0,
        borderRight: 1,
        borderColor: 'divider',
        display: 'flex',
        flexDirection: 'column',
        minHeight: 0,
      }}
    >
      <Stack spacing={1.5} sx={{ p: 2, pb: 1.5 }}>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
          <Typography variant="subtitle1" sx={{ flex: 1 }}>
            Sessions
          </Typography>
          <Tooltip title="Refresh">
            <IconButton data-testid="agents-refresh" size="small" onClick={onRefresh}>
              <RefreshIcon fontSize="small" />
            </IconButton>
          </Tooltip>
          <Button
            data-testid="agents-new-session"
            size="small"
            variant="contained"
            startIcon={<AddIcon />}
            onClick={onNew}
          >
            New
          </Button>
        </Stack>
        {/* Cross-user owner picker (principle 1) — mirrors mailui's campaigns
            owner dropdown. Empty value = every user's sessions. */}
        <TextField
          select
          size="small"
          label="Owner"
          data-testid="agents-owner-filter"
          value={ownerFilter}
          onChange={(event) => onOwnerFilter(event.target.value)}
        >
          <MenuItem data-testid="agents-owner-all" value="">
            All users
          </MenuItem>
          <MenuItem data-testid="agents-owner-me" value={user.username}>
            Me ({user.username})
          </MenuItem>
          {owners
            .filter((owner) => owner !== user.username)
            .map((owner) => (
              <MenuItem key={owner} data-testid={`agents-owner-${slug(owner)}`} value={owner}>
                {owner}
              </MenuItem>
            ))}
        </TextField>
      </Stack>
      <Divider />
      <Box sx={{ flex: 1, overflowY: 'auto' }}>
        {sessions == null ? (
          <Box sx={{ display: 'grid', placeItems: 'center', py: 6 }}>
            <CircularProgress size={28} />
          </Box>
        ) : sessions.length === 0 ? (
          <Typography color="text.secondary" sx={{ p: 3 }}>
            {ownerFilter ? 'No sessions for this owner.' : 'No sessions yet. Start a new chat with an agent.'}
          </Typography>
        ) : (
          <>
            <List dense disablePadding>
              {sessions.map((session) => (
                <ListItemButton
                  key={session.id}
                  data-testid={`agents-open-session-${session.id}`}
                  divider
                  selected={session.id === selectedId}
                  onClick={() => onSelect(session)}
                  sx={{ alignItems: 'flex-start', py: 1.25 }}
                >
                  <ListItemText
                    primary={
                      <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
                        <Typography variant="body2" sx={{ flex: 1, fontWeight: 500 }} noWrap>
                          {session.title}
                        </Typography>
                        <StatusChip status={session.status} />
                      </Stack>
                    }
                    secondary={
                      <Stack
                        component="span"
                        direction="row"
                        spacing={1}
                        sx={{ mt: 0.5, alignItems: 'center', justifyContent: 'space-between' }}
                      >
                        <Typography variant="caption" color="text.secondary" component="span" noWrap>
                          {attribution(session)}
                        </Typography>
                        <Typography variant="caption" color="text.secondary" component="span" sx={{ flexShrink: 0 }}>
                          {formatDate(session.updated_at)}
                        </Typography>
                      </Stack>
                    }
                    disableTypography
                  />
                </ListItemButton>
              ))}
            </List>
            {hasMore && (
              <Box sx={{ display: 'grid', placeItems: 'center', py: 1.5 }}>
                <Button
                  data-testid="agents-load-more"
                  size="small"
                  onClick={onLoadMore}
                  disabled={loadingMore}
                  startIcon={loadingMore ? <CircularProgress size={14} /> : undefined}
                >
                  Load more
                </Button>
              </Box>
            )}
          </>
        )}
      </Box>
    </Box>
  )
}
