import DownloadIcon from '@mui/icons-material/Download'
import EmailOutlinedIcon from '@mui/icons-material/EmailOutlined'
import LinkedInIcon from '@mui/icons-material/LinkedIn'
import PhoneOutlinedIcon from '@mui/icons-material/PhoneOutlined'
import { Box, Button, Chip, Divider, Stack, Typography } from '@mui/material'
import type { SimilarityGroup, SimilarityGroupedResponse } from '../api'
import type { ApolloRecord } from '../types'
import {
  contactPresence,
  csvEscape,
  recordCompany,
  recordTitle,
  resolvedRecordEmail,
  resolvedRecordPhone,
  secondaryText,
} from './record'

/** Company display name for a group: the hydrated company record's name, else the
 * echoed id (so a company that failed to hydrate is still labelled). */
function groupTitle(group: SimilarityGroup): string {
  const name = group.company ? recordCompany(group.company) : null
  return name || group.company_id
}

function groupDomain(group: SimilarityGroup): string | null {
  const company = group.company
  if (company && company.entity_type === 'company') return (company.domain || '').trim() || null
  return null
}

/** Flat CSV with a leading company column — one row per hit across every group,
 * built client-side from the grouped payload. Zero-hit companies contribute no rows. */
function downloadGroupedCsv(response: SimilarityGroupedResponse): void {
  const lines = ['company,company_domain,score,mongo_id,name,email,linkedin,phone,title']
  for (const group of response.groups) {
    const company = groupTitle(group)
    const domain = groupDomain(group) || ''
    for (const hit of group.hits) {
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
    anchor.download = `top-people-${response.search_id}.csv`
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

function GroupSection({ group }: { group: SimilarityGroup }) {
  const title = groupTitle(group)
  const domain = groupDomain(group)
  const hitCount = group.hits.length
  return (
    <Box
      data-testid={`grouped-company-${group.company_id}`}
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
        <Typography variant="caption" color={hitCount ? 'text.secondary' : 'text.disabled'}>
          {hitCount ? `${hitCount} ${hitCount === 1 ? 'person' : 'people'}` : 'No matches'}
        </Typography>
      </Box>
      {group.hits.map((hit, index) => (
        <HitRow key={hit.record.mongo_id || hit.record.id || index} score={hit.score} record={hit.record} />
      ))}
    </Box>
  )
}

export function GroupedResultsView({ response }: { response: SimilarityGroupedResponse }) {
  const companyCount = response.groups.length
  const withHits = response.groups.filter((g) => g.hits.length > 0).length
  return (
    <Stack spacing={1.5} data-testid="grouped-results">
      <Stack
        direction="row"
        spacing={1.5}
        sx={{ alignItems: 'center', flexWrap: 'wrap', gap: 1 }}
      >
        <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
          {response.total.toLocaleString()} {response.total === 1 ? 'person' : 'people'} across{' '}
          {withHits}/{companyCount} {companyCount === 1 ? 'company' : 'companies'}
        </Typography>
        <Box sx={{ flex: 1 }} />
        <Button
          data-testid="grouped-export"
          size="small"
          variant="outlined"
          startIcon={<DownloadIcon />}
          disabled={response.total === 0}
          onClick={() => downloadGroupedCsv(response)}
        >
          Export CSV
        </Button>
      </Stack>
      <Divider />
      <Stack spacing={1.25}>
        {response.groups.map((group) => (
          <GroupSection key={group.company_id} group={group} />
        ))}
      </Stack>
    </Stack>
  )
}
