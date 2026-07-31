import BusinessIcon from '@mui/icons-material/Business'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import LanguageIcon from '@mui/icons-material/Language'
import LinkedInIcon from '@mui/icons-material/LinkedIn'
import PersonOutlinedIcon from '@mui/icons-material/PersonOutlined'
import PersonSearchIcon from '@mui/icons-material/PersonSearch'
import RefreshIcon from '@mui/icons-material/Refresh'
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Divider,
  IconButton,
  Link,
  Stack,
  Tooltip,
  Typography,
} from '@mui/material'
import { memo, useCallback, useEffect, useState, type ReactNode } from 'react'
import { getPersonLead } from '../api'
import type { ApolloEndpointResponse, ApolloRecord, CompanyRecord, PersonRecord } from '../types'

function locationLine(record: { city?: string | null; state?: string | null; country?: string | null }) {
  return [record.city, record.state, record.country].filter(Boolean).join(', ')
}

function parseApiDate(value: string): Date {
  const raw = value.trim()
  if (!raw) return new Date(Number.NaN)
  // Already timezone-aware (Z or ±HH:MM).
  if (/([zZ]|[+-]\d{2}:?\d{2})$/.test(raw)) {
    return new Date(raw)
  }
  // Backend stores UTC; naive ISO / "YYYY-MM-DD HH:mm:ss" → treat as UTC.
  const normalized = raw.includes('T') ? raw : raw.replace(' ', 'T')
  return new Date(`${normalized}Z`)
}

function formatReceivedAt(value: string | null | undefined) {
  if (!value) return null
  try {
    const date = parseApiDate(String(value))
    if (Number.isNaN(date.getTime())) return value
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: 'medium',
      timeStyle: 'short',
    }).format(date)
  } catch {
    return value
  }
}

function recordJsonPayload(record: ApolloRecord) {
  return record.raw ?? record
}

const CONTACT_SIGNAL_KEYS = new Set(['has_email', 'has_phone', 'has_direct_phone'])
const CONTACT_PENDING_FILLER = '••••••••••••'

function formatFlagLabel(key: string) {
  return key
    .replace(/^has_/, '')
    .replace(/^is_/, '')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
}

function personBooleanFlags(record: PersonRecord): Array<{ key: string; value: boolean }> {
  const source =
    record.raw && typeof record.raw === 'object' && !Array.isArray(record.raw)
      ? record.raw
      : (record as unknown as Record<string, unknown>)

  return Object.entries(source)
    .filter(([key, value]) => {
      if (key === 'organization' || CONTACT_SIGNAL_KEYS.has(key)) return false
      return typeof value === 'boolean'
    })
    .map(([key, value]) => ({ key, value: value as boolean }))
    .sort((a, b) => a.key.localeCompare(b.key))
}

function phoneNumbersFromValue(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value
    .map((item) => {
      if (typeof item === 'string') return item.trim()
      if (item && typeof item === 'object') {
        const row = item as Record<string, unknown>
        return String(row.sanitized_number || row.raw_number || row.number || '').trim()
      }
      return ''
    })
    .filter(Boolean)
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

function matchResponseData(record: PersonRecord): Record<string, unknown> | null {
  const entry = record.apollo_responses?.['/api/v1/people/match']
  const data = entry?.data
  return data && typeof data === 'object' && !Array.isArray(data)
    ? (data as Record<string, unknown>)
    : null
}

function resolvedPersonEmail(record: PersonRecord): string | null {
  const direct = emailFromValue(record.email) || emailFromValue(record.emails)
  if (direct) return direct
  const match = matchResponseData(record)
  if (!match) return null
  const person = match.person
  if (person && typeof person === 'object' && !Array.isArray(person)) {
    const nested = person as Record<string, unknown>
    return emailFromValue(nested.email) || emailFromValue(nested.emails)
  }
  return emailFromValue(match.email) || emailFromValue(match.emails)
}

function resolvedPersonPhones(record: PersonRecord): string[] {
  const direct = phoneNumbersFromValue(record.phone_numbers)
  if (direct.length) return direct
  const match = matchResponseData(record)
  if (!match) return []
  const person = match.person
  if (person && typeof person === 'object' && !Array.isArray(person)) {
    const nested = phoneNumbersFromValue((person as Record<string, unknown>).phone_numbers)
    if (nested.length) return nested
  }
  return phoneNumbersFromValue(match.phone_numbers)
}

function ContactPendingValue({ label }: { label: string }) {
  return (
    <Typography
      component="span"
      variant="body2"
      color="text.disabled"
      title={`${label} available in Apollo but not revealed yet`}
      aria-label={`${label} pending reveal`}
      sx={{ fontWeight: 600, letterSpacing: 1.5, userSelect: 'none' }}
    >
      {CONTACT_PENDING_FILLER}
    </Typography>
  )
}

function endpointEntries(record: ApolloRecord): Array<[string, ApolloEndpointResponse]> {
  const responses = record.apollo_responses
  if (!responses) return []
  return Object.entries(responses).sort(([a], [b]) => a.localeCompare(b))
}

function DetailHeader({
  icon,
  title,
  subtitle,
  action,
}: {
  icon: ReactNode
  title: string
  subtitle?: string | null
  action?: ReactNode
}) {
  return (
    <Box
      sx={{
        p: 2.5,
        borderRadius: 2,
        border: '1px solid',
        borderColor: 'divider',
        background: (t) =>
          t.palette.mode === 'dark'
            ? 'linear-gradient(135deg, rgba(92, 184, 154, 0.12), rgba(16, 24, 42, 0.4))'
            : 'linear-gradient(135deg, rgba(11, 61, 46, 0.06), rgba(196, 92, 38, 0.05))',
      }}
    >
      <Stack direction="row" spacing={1.5} sx={{ alignItems: 'flex-start' }}>
        <Box
          sx={{
            width: 44,
            height: 44,
            borderRadius: 1.5,
            display: 'grid',
            placeItems: 'center',
            bgcolor: (t) =>
              t.palette.mode === 'dark' ? 'rgba(92, 184, 154, 0.16)' : 'rgba(11, 61, 46, 0.1)',
            color: 'primary.main',
            flexShrink: 0,
          }}
        >
          {icon}
        </Box>
        <Box sx={{ minWidth: 0, flex: 1 }}>
          <Typography variant="h5" sx={{ fontWeight: 700, lineHeight: 1.2 }}>
            {title}
          </Typography>
          {subtitle && (
            <Typography variant="body1" color="text.secondary" sx={{ mt: 0.5 }}>
              {subtitle}
            </Typography>
          )}
        </Box>
        {action}
      </Stack>
    </Box>
  )
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  if (children == null || children === '') return null
  return (
    <Box
      sx={{
        p: 1.5,
        borderRadius: 1.5,
        border: '1px solid',
        borderColor: 'divider',
        bgcolor: (t) =>
          t.palette.mode === 'dark' ? 'rgba(16, 24, 42, 0.45)' : 'rgba(247, 250, 248, 0.9)',
        minWidth: 0,
      }}
    >
      <Typography
        variant="caption"
        color="text.secondary"
        sx={{ display: 'block', mb: 0.5, letterSpacing: 0.4, textTransform: 'uppercase' }}
      >
        {label}
      </Typography>
      <Typography variant="body2" sx={{ fontWeight: 600, wordBreak: 'break-word' }}>
        {children}
      </Typography>
    </Box>
  )
}

function FactGrid({ children }: { children: ReactNode }) {
  return (
    <Box
      sx={{
        display: 'grid',
        gap: 1.25,
        gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' },
      }}
    >
      {children}
    </Box>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Stack spacing={1.25}>
      <Typography variant="overline" color="text.secondary" sx={{ letterSpacing: 1 }}>
        {title}
      </Typography>
      {children}
    </Stack>
  )
}

function RawJsonAccordion({
  title,
  subtitle,
  value,
}: {
  title: string
  subtitle?: string | null
  value: unknown
}) {
  const [open, setOpen] = useState(false)
  let text: string
  try {
    text = JSON.stringify(value, null, 2)
  } catch {
    text = String(value)
  }

  return (
    <Accordion
      disableGutters
      elevation={0}
      expanded={open}
      onChange={(_, next) => setOpen(next)}
      sx={{
        border: '1px solid',
        borderColor: 'divider',
        borderRadius: '10px !important',
        bgcolor: 'transparent',
        '&:before': { display: 'none' },
      }}
    >
      <AccordionSummary data-testid="record-json-toggle" expandIcon={<ExpandMoreIcon />}>
        <Stack spacing={0.25} sx={{ minWidth: 0 }}>
          <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
            {title}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {subtitle ? `${subtitle} · ` : ''}
            {open ? 'Hide' : 'Show'} JSON
          </Typography>
        </Stack>
      </AccordionSummary>
      <AccordionDetails sx={{ pt: 0 }}>
        <Box
          component="pre"
          sx={{
            m: 0,
            p: 1.5,
            borderRadius: 1,
            overflow: 'auto',
            maxHeight: 360,
            fontSize: 12,
            lineHeight: 1.45,
            bgcolor: (t) =>
              t.palette.mode === 'dark' ? 'rgba(0,0,0,0.35)' : 'rgba(0,0,0,0.04)',
          }}
        >
          {text}
        </Box>
      </AccordionDetails>
    </Accordion>
  )
}

function EndpointResponsesSection({ record }: { record: ApolloRecord }) {
  const entries = endpointEntries(record)
  if (!entries.length) {
    return <RawJsonAccordion title="Raw Apollo JSON" value={recordJsonPayload(record)} />
  }

  return (
    <Stack spacing={1}>
      <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
        Apollo responses by endpoint
      </Typography>
      {entries.map(([endpoint, entry]) => (
        <RawJsonAccordion
          key={endpoint}
          title={endpoint}
          subtitle={formatReceivedAt(entry.received_at)}
          value={entry.data}
        />
      ))}
    </Stack>
  )
}

function PersonDetail({
  record,
  onRecordUpdated,
}: {
  record: PersonRecord
  onRecordUpdated?: (record: ApolloRecord) => void
}) {
  const org = record.organization
  const location = locationLine(record)
  const booleanFlags = personBooleanFlags(record)
  const email = resolvedPersonEmail(record)
  const phones = resolvedPersonPhones(record)
  const showEmailRow = Boolean(email) || Boolean(record.has_email)
  const showPhoneRow = phones.length > 0 || Boolean(record.has_phone)
  const showContactDetails = showEmailRow || showPhoneRow
  const [refreshing, setRefreshing] = useState(false)
  const [refreshError, setRefreshError] = useState<string | null>(null)

  const handleRefresh = useCallback(async () => {
    if (!record.id || refreshing) return
    setRefreshing(true)
    setRefreshError(null)
    try {
      const mongoId = record.mongo_id || String(record.id || '')
      if (!mongoId) throw new Error('Missing mongo lead id')
      const updated = await getPersonLead(mongoId)
      onRecordUpdated?.(updated)
    } catch (err) {
      setRefreshError(err instanceof Error ? err.message : 'Failed to refresh person')
    } finally {
      setRefreshing(false)
    }
  }, [onRecordUpdated, record.id, refreshing])

  return (
    <Stack spacing={2.5}>
      <DetailHeader
        icon={<PersonOutlinedIcon />}
        title={record.name}
        subtitle={record.title}
        action={
          <Tooltip title="Refresh from stored lead">
            <span>
              <IconButton
                data-testid="record-refresh"
                aria-label="Refresh person"
                size="small"
                onClick={() => void handleRefresh()}
                disabled={refreshing || !record.id}
                sx={{ mt: 0.25 }}
              >
                {refreshing ? <CircularProgress size={18} /> : <RefreshIcon fontSize="small" />}
              </IconButton>
            </span>
          </Tooltip>
        }
      />

      {refreshError && (
        <Alert severity="error" onClose={() => setRefreshError(null)}>
          {refreshError}
        </Alert>
      )}

      <Section title="Overview">
        <FactGrid>
          <Fact label="Company">
            {org?.name
              ? `${org.name}${org.domain ? ` · ${org.domain}` : ''}`
              : null}
          </Fact>
          <Fact label="Location">{location || null}</Fact>
          <Fact label="Apollo ID">{record.id || null}</Fact>
          <Fact label="Organization ID">{org?.id || null}</Fact>
        </FactGrid>
      </Section>

      <Section title="Contact signals">
        <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap' }} useFlexGap>
          <Chip
            size="small"
            label={record.has_email ? 'Has email' : 'No email'}
            color={record.has_email ? 'success' : 'default'}
            variant="outlined"
          />
          <Chip
            size="small"
            label={record.has_phone ? 'Has phone' : 'No phone'}
            color={record.has_phone ? 'success' : 'default'}
            variant="outlined"
          />
        </Stack>
      </Section>

      {showContactDetails && (
        <Section title="Contact details">
          <FactGrid>
            {showEmailRow && (
              <Fact label="Email">
                {email || <ContactPendingValue label="Email" />}
              </Fact>
            )}
            {showPhoneRow && (
              <Fact label="Phone numbers">
                {phones.length > 0 ? (
                  phones.join(', ')
                ) : (
                  <ContactPendingValue label="Phone" />
                )}
              </Fact>
            )}
          </FactGrid>
        </Section>
      )}

      {booleanFlags.length > 0 && (
        <Section title="Availability flags">
          <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap' }} useFlexGap>
            {booleanFlags.map(({ key, value }) => (
              <Chip
                key={key}
                size="small"
                label={`${formatFlagLabel(key)}: ${value ? 'Yes' : 'No'}`}
                color={value ? 'success' : 'default'}
                variant="outlined"
              />
            ))}
          </Stack>
        </Section>
      )}

      {(record.linkedin_url || org?.linkedin_url) && (
        <Section title="Links">
          <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap' }} useFlexGap>
            {record.linkedin_url && (
              <Button
                data-testid="record-person-linkedin"
                component={Link}
                href={record.linkedin_url}
                target="_blank"
                rel="noreferrer"
                size="small"
                variant="outlined"
                startIcon={<LinkedInIcon />}
                underline="none"
              >
                LinkedIn profile
              </Button>
            )}
            {org?.linkedin_url && (
              <Button
                data-testid="record-person-company-linkedin"
                component={Link}
                href={org.linkedin_url}
                target="_blank"
                rel="noreferrer"
                size="small"
                variant="outlined"
                startIcon={<BusinessIcon />}
                underline="none"
              >
                Company LinkedIn
              </Button>
            )}
          </Stack>
        </Section>
      )}

      {record.headline && (
        <Section title="Headline">
          <Typography variant="body2" color="text.secondary" sx={{ lineHeight: 1.6 }}>
            {record.headline}
          </Typography>
        </Section>
      )}

      <Divider />
      <EndpointResponsesSection record={record} />
    </Stack>
  )
}

function CompanyDetail({
  record,
  onUseForPeopleSearch,
}: {
  record: CompanyRecord
  onUseForPeopleSearch?: (
    organizationId: string,
    companyName: string,
    companyDomain?: string | null,
  ) => void
}) {
  const organizationId = record.organization_id || record.id
  const canUse = Boolean(organizationId)
  const location = locationLine(record)
  const website = record.website_url || (record.domain ? `https://${record.domain}` : null)

  return (
    <Stack spacing={2.5}>
      <DetailHeader
        icon={<BusinessIcon />}
        title={record.name}
        subtitle={record.industry}
      />

      <Section title="Overview">
        <FactGrid>
          <Fact label="Domain">{record.domain || null}</Fact>
          <Fact label="Employees">
            {record.employee_count != null ? `~${record.employee_count.toLocaleString()}` : null}
          </Fact>
          <Fact label="Location">{location || null}</Fact>
          <Fact label="Phone">{record.phone || null}</Fact>
          <Fact label="Organization ID">{organizationId || null}</Fact>
          <Fact label="Account ID">{record.account_id || null}</Fact>
        </FactGrid>
      </Section>

      {(website || record.linkedin_url) && (
        <Section title="Links">
          <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap' }} useFlexGap>
            {website && (
              <Button
                data-testid="record-company-website"
                component={Link}
                href={website}
                target="_blank"
                rel="noreferrer"
                size="small"
                variant="outlined"
                startIcon={<LanguageIcon />}
                underline="none"
              >
                {record.domain || 'Website'}
              </Button>
            )}
            {record.linkedin_url && (
              <Button
                data-testid="record-company-linkedin"
                component={Link}
                href={record.linkedin_url}
                target="_blank"
                rel="noreferrer"
                size="small"
                variant="outlined"
                startIcon={<LinkedInIcon />}
                underline="none"
              >
                LinkedIn
              </Button>
            )}
          </Stack>
        </Section>
      )}

      {record.short_description && (
        <Section title="About">
          <Typography variant="body2" color="text.secondary" sx={{ lineHeight: 1.65 }}>
            {record.short_description}
          </Typography>
        </Section>
      )}

      {!!record.keywords?.length && (
        <Section title="Keywords">
          <Stack direction="row" spacing={0.75} sx={{ flexWrap: 'wrap' }} useFlexGap>
            {record.keywords.map((kw) => (
              <Chip key={kw} size="small" label={kw} variant="outlined" />
            ))}
          </Stack>
        </Section>
      )}

      {onUseForPeopleSearch && (
        <Box>
          {!canUse ? (
            <Alert severity="warning">
              This company record has no Apollo organization ID, so people search cannot run.
            </Alert>
          ) : (
            <Button
              data-testid="record-company-search-people"
              variant="contained"
              startIcon={<PersonSearchIcon />}
              onClick={() =>
                onUseForPeopleSearch(organizationId!, record.name, record.domain)
              }
            >
              Search people at this company
            </Button>
          )}
        </Box>
      )}

      <Divider />
      <EndpointResponsesSection record={record} />
    </Stack>
  )
}

export const RecordDetail = memo(function RecordDetail({
  record,
  onUseForPeopleSearch,
  onRecordUpdated,
}: {
  record: ApolloRecord
  onUseForPeopleSearch?: (
    organizationId: string,
    companyName: string,
    companyDomain?: string | null,
  ) => void
  onRecordUpdated?: (record: ApolloRecord) => void
}) {
  const [key, setKey] = useState(0)

  useEffect(() => {
    setKey((n) => n + 1)
  }, [record.id, record.name, record.entity_type])

  if (record.entity_type === 'person') {
    return (
      <Box key={key}>
        <PersonDetail record={record} onRecordUpdated={onRecordUpdated} />
      </Box>
    )
  }

  return (
    <Box key={key}>
      <CompanyDetail record={record} onUseForPeopleSearch={onUseForPeopleSearch} />
    </Box>
  )
})
