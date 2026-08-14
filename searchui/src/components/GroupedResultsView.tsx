import DownloadIcon from '@mui/icons-material/Download'
import EmailOutlinedIcon from '@mui/icons-material/EmailOutlined'
import LinkedInIcon from '@mui/icons-material/LinkedIn'
import PhoneOutlinedIcon from '@mui/icons-material/PhoneOutlined'
import {
  Box,
  Button,
  Chip,
  CircularProgress,
  Divider,
  LinearProgress,
  Skeleton,
  Stack,
  Tooltip,
  Typography,
} from '@mui/material'
import type { ApolloRecord } from '../types'
import type { GroupedRunView, GroupedSlot } from '../workflows/useGroupedStream'
import {
  contactPresence,
  csvEscape,
  recordCompany,
  recordTitle,
  resolvedRecordEmail,
  resolvedRecordPhone,
  secondaryText,
} from './record'

/** Company display name for a slot: the hydrated record's name, else the request
 * chip's name, else the echoed id (so an unhydrated company is still labelled). */
function slotTitle(slot: GroupedSlot): string {
  const name = slot.company ? recordCompany(slot.company) : null
  return name || slot.companyName || slot.companyId
}

function slotDomain(slot: GroupedSlot): string | null {
  const company = slot.company
  if (company && company.entity_type === 'company') return (company.domain || '').trim() || null
  return null
}

/** Flat CSV with a leading company column — one row per hit across every done slot,
 * built client-side from the streamed slots (only enabled once ranking completes). */
function downloadGroupedCsv(view: GroupedRunView): void {
  const lines = ['company,company_domain,score,mongo_id,name,email,linkedin,phone,title']
  for (const slot of view.slots) {
    if (slot.status !== 'done') continue
    const company = slotTitle(slot)
    const domain = slotDomain(slot) || ''
    for (const hit of slot.hits) {
      const record = hit.record
      const score = hit.score != null ? String(hit.score) : ''
      const mongoId = (record.mongo_id || '').trim() || 'null'
      const name = (record.name || '').trim() || 'null'
      const email = resolvedRecordEmail(record) || 'null'
      const linkedin = (record.linkedin_url || '').trim() || 'null'
      const phone = resolvedRecordPhone(record) || 'null'
      const title = recordTitle(record) || 'null'
      lines.push(
        [company, domain, score, mongoId, name, email, linkedin, phone, title]
          .map(csvEscape)
          .join(','),
      )
    }
  }
  const blob = new Blob([`${lines.join('\n')}\n`], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  try {
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `top-people-${view.searchId ?? 'export'}.csv`
    anchor.rel = 'noopener'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  } finally {
    URL.revokeObjectURL(url)
  }
}

function HitRow({ score, record }: { score: number | null; record: ApolloRecord }) {
  const flags = contactPresence(record)
  const secondary = secondaryText(record)
  return (
    <Box
      sx={{
        display: 'flex',
        alignItems: 'flex-start',
        gap: 1,
        py: 0.75,
        px: 1,
        borderTop: '1px solid',
        borderColor: 'divider',
      }}
    >
      <Box sx={{ minWidth: 0, flex: 1 }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, minWidth: 0 }}>
          <Typography component="span" variant="body2" noWrap sx={{ fontWeight: 500, minWidth: 0 }}>
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
              {flags.linkedin && <LinkedInIcon sx={{ fontSize: 14 }} titleAccess="Has LinkedIn URL" />}
              {flags.email && <EmailOutlinedIcon sx={{ fontSize: 14 }} titleAccess="Has email" />}
              {flags.phone && <PhoneOutlinedIcon sx={{ fontSize: 14 }} titleAccess="Has phone" />}
            </Box>
          )}
        </Box>
        {secondary && (
          <Typography variant="caption" color="text.secondary" noWrap sx={{ display: 'block' }}>
            {secondary}
          </Typography>
        )}
      </Box>
      {score != null && (
        <Chip
          size="small"
          label={score.toFixed(3)}
          variant="outlined"
          sx={{ flexShrink: 0, fontVariantNumeric: 'tabular-nums' }}
        />
      )}
    </Box>
  )
}

function SlotSection({ slot }: { slot: GroupedSlot }) {
  const title = slotTitle(slot)
  const domain = slotDomain(slot)
  const hitCount = slot.hits.length
  return (
    <Box
      data-testid={`grouped-company-${slot.companyId}`}
      sx={{ border: '1px solid', borderColor: 'divider', borderRadius: 1, overflow: 'hidden' }}
    >
      <Box
        sx={{
          display: 'flex',
          alignItems: 'baseline',
          gap: 1,
          px: 1.25,
          py: 1,
          bgcolor: 'action.hover',
        }}
      >
        <Typography variant="subtitle2" sx={{ fontWeight: 700, minWidth: 0 }} noWrap>
          {title}
        </Typography>
        {domain && (
          <Typography variant="caption" color="text.secondary" noWrap>
            {domain}
          </Typography>
        )}
        <Box sx={{ flex: 1 }} />
        {slot.status === 'pending' && (
          <Stack direction="row" spacing={0.75} sx={{ alignItems: 'center' }}>
            <CircularProgress size={12} />
            <Typography variant="caption" color="text.secondary">
              Ranking…
            </Typography>
          </Stack>
        )}
        {slot.status === 'error' && (
          <Chip
            size="small"
            color="error"
            variant="outlined"
            label={slot.errorReason ? `Failed — ${slot.errorReason}` : 'Failed'}
          />
        )}
        {slot.status === 'done' && (
          <Typography variant="caption" color={hitCount ? 'text.secondary' : 'text.disabled'}>
            {hitCount ? `${hitCount} ${hitCount === 1 ? 'person' : 'people'}` : 'No matches'}
          </Typography>
        )}
      </Box>
      {slot.status === 'pending' && (
        <Box sx={{ px: 1, py: 0.75 }}>
          <Skeleton variant="text" width="55%" />
          <Skeleton variant="text" width="40%" />
        </Box>
      )}
      {slot.status === 'done' &&
        slot.hits.map((hit, index) => (
          <HitRow key={hit.record.mongo_id || hit.record.id || index} score={hit.score} record={hit.record} />
        ))}
    </Box>
  )
}

export function GroupedResultsView({ view }: { view: GroupedRunView }) {
  if (!view.active) return null
  const withHits = view.slots.filter((s) => s.status === 'done' && s.hits.length > 0).length
  return (
    <Stack spacing={1.5} data-testid="grouped-results">
      <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center', flexWrap: 'wrap', gap: 1 }}>
        {view.complete ? (
          <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
            {view.totalHits.toLocaleString()} {view.totalHits === 1 ? 'person' : 'people'} across{' '}
            {withHits}/{view.total} {view.total === 1 ? 'company' : 'companies'}
            {view.failedTotal > 0 ? ` · ${view.failedTotal} failed` : ''}
          </Typography>
        ) : (
          <Typography data-testid="grouped-progress" variant="subtitle1" sx={{ fontWeight: 700 }}>
            {view.ranked.toLocaleString()} of {view.total.toLocaleString()} companies ranked
          </Typography>
        )}
        <Box sx={{ flex: 1 }} />
        <Tooltip title={view.complete ? '' : 'Available when ranking completes'}>
          <span>
            <Button
              data-testid="grouped-export"
              size="small"
              variant="outlined"
              startIcon={<DownloadIcon />}
              disabled={!view.complete || view.totalHits === 0}
              onClick={() => downloadGroupedCsv(view)}
            >
              Export CSV
            </Button>
          </span>
        </Tooltip>
      </Stack>
      {view.streaming && (
        <LinearProgress
          variant={view.total > 0 ? 'determinate' : 'indeterminate'}
          value={view.total > 0 ? Math.round((view.ranked / view.total) * 100) : undefined}
        />
      )}
      <Divider />
      <Stack spacing={1.25}>
        {view.slots.map((slot) => (
          <SlotSection key={slot.companyId || slot.index} slot={slot} />
        ))}
      </Stack>
    </Stack>
  )
}
