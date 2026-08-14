import {
  Autocomplete,
  Chip,
  FormControl,
  FormHelperText,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from '@mui/material'
import type { CompanyOption, EmbedKind } from '../api'
import { EMBED_KINDS, SIMILARITY_LIMIT_MAX, selectedEmbedKinds, type TriState } from './similarity'

export interface SimilarityFormProps {
  query: string
  onQueryChange: (value: string) => void
  embeds: Record<EmbedKind, boolean>
  onEmbedsChange: (next: Record<EmbedKind, boolean>) => void
  /** Recent-company suggestions for the company picker dropdown. */
  companyOptions: CompanyOption[]
  /** Currently selected companies (OR filter / stage-3 chips). */
  companies: CompanyOption[]
  onCompaniesChange: (next: CompanyOption[]) => void
  /** Resolve a free-typed record/Apollo id to a named company and append it. */
  onAddCompany: (raw: string) => void
  resolvingCompany: boolean
  companyResolveError: string | null
  emailFilter: TriState
  onEmailFilterChange: (next: TriState) => void
  phoneFilter: TriState
  onPhoneFilterChange: (next: TriState) => void
  linkedinFilter: TriState
  onLinkedinFilterChange: (next: TriState) => void
  limit: number
  onLimitChange: (next: number) => void
  /** Hide the built-in "Result limit" field — the caller supplies its own limit
   * control (e.g. the top-people-per-company workflow's per-company X). */
  hideLimit?: boolean
}

/**
 * The saved-leads similarity search inputs (embed toggles, passage, company OR
 * filter, tri-state contact filters, result limit). Extracted from SearchPage so
 * both the search view and the Prospect workflow render the identical block; the
 * component is fully controlled (no internal search state) and preserves every
 * existing `data-testid`.
 */
export function SimilarityForm({
  query,
  onQueryChange,
  embeds,
  onEmbedsChange,
  companyOptions,
  companies,
  onCompaniesChange,
  onAddCompany,
  resolvingCompany,
  companyResolveError,
  emailFilter,
  onEmailFilterChange,
  phoneFilter,
  onPhoneFilterChange,
  linkedinFilter,
  onLinkedinFilterChange,
  limit,
  onLimitChange,
  hideLimit = false,
}: SimilarityFormProps) {
  const selectedEmbeds = selectedEmbedKinds(embeds)
  const hasEmbeds = selectedEmbeds.length > 0

  const filters: Array<{
    id: string
    label: string
    value: TriState
    set: (next: TriState) => void
  }> = [
    { id: 'email', label: 'Email', value: emailFilter, set: onEmailFilterChange },
    { id: 'phone', label: 'Phone', value: phoneFilter, set: onPhoneFilterChange },
    { id: 'linkedin', label: 'LinkedIn', value: linkedinFilter, set: onLinkedinFilterChange },
  ]

  return (
    <>
      <FormControl>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 0.75 }}>
          Rank by embeddings
        </Typography>
        <ToggleButtonGroup
          size="small"
          value={selectedEmbeds}
          onChange={(_, next: EmbedKind[]) =>
            onEmbedsChange({
              apollo: next.includes('apollo'),
              name: next.includes('name'),
              title: next.includes('title'),
            })
          }
          aria-label="Embedding set"
        >
          {EMBED_KINDS.map((kind) => (
            <ToggleButton
              key={kind.value}
              value={kind.value}
              data-testid={`similarity-embed-${kind.value}`}
              sx={{ textTransform: 'none' }}
            >
              {kind.label}
            </ToggleButton>
          ))}
        </ToggleButtonGroup>
        <FormHelperText>
          {hasEmbeds
            ? 'Ranked by mean similarity across the selected embeddings.'
            : 'No embeddings selected — pure filter search (query ignored).'}
        </FormHelperText>
      </FormControl>
      <TextField
        label={
          hasEmbeds
            ? 'Describe who you’re looking for'
            : 'Describe who you’re looking for (optional)'
        }
        placeholder="e.g. series A fintech CRO in NYC"
        value={query}
        onChange={(e) => onQueryChange(e.target.value)}
        fullWidth
        autoFocus
        multiline
        minRows={2}
        disabled={!hasEmbeds}
      />
      <Autocomplete
        multiple
        freeSolo
        options={companyOptions}
        value={companies}
        filterSelectedOptions
        getOptionLabel={(option) =>
          typeof option === 'string' ? option : option.name || option.mongo_id || ''
        }
        isOptionEqualToValue={(option, value) =>
          typeof option === 'string' || typeof value === 'string'
            ? option === value
            : option.mongo_id === value.mongo_id && option.apollo_id === value.apollo_id
        }
        onChange={(_, next) => {
          // Strings arrive from freeSolo Enter presses — resolve them to
          // named options; objects come from the dropdown.
          const objects = next.filter(
            (item): item is CompanyOption => typeof item !== 'string',
          )
          onCompaniesChange(objects)
          for (const item of next) {
            if (typeof item === 'string') onAddCompany(item)
          }
        }}
        renderValue={(tagValue, getItemProps) =>
          tagValue.map((option, index) => {
            const { key, ...tagProps } = getItemProps({ index })
            const label =
              typeof option === 'string'
                ? option
                : option.name || option.mongo_id || option.apollo_id
            return (
              <Chip
                key={key}
                {...tagProps}
                label={label}
                size="small"
                data-testid="similarity-company-chip"
                sx={{
                  '& .MuiChip-deleteIcon': { opacity: 0, transition: 'opacity 120ms' },
                  '&:hover .MuiChip-deleteIcon': { opacity: 1 },
                }}
              />
            )
          })
        }
        renderInput={(params) => (
          <TextField
            {...params}
            label="Companies"
            placeholder={
              resolvingCompany
                ? 'Resolving…'
                : 'Pick a recent company or paste an ID and press Enter'
            }
            helperText={
              companyResolveError ??
              'One or more companies (OR) — pick from recent companies, or paste a record id / Apollo organization ID.'
            }
            error={Boolean(companyResolveError)}
            data-testid="similarity-company-id"
          />
        )}
        fullWidth
      />
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
        {filters.map((filter) => (
          <FormControl key={filter.id} sx={{ minWidth: 140 }} fullWidth>
            <InputLabel id={`similarity-${filter.id}-filter-label`}>{filter.label}</InputLabel>
            <Select
              labelId={`similarity-${filter.id}-filter-label`}
              label={filter.label}
              value={filter.value}
              onChange={(e) => filter.set(e.target.value as TriState)}
              data-testid={`similarity-${filter.id}-filter`}
            >
              <MenuItem value="any">Any</MenuItem>
              <MenuItem value="has">Has</MenuItem>
              <MenuItem value="missing">Missing</MenuItem>
            </Select>
          </FormControl>
        ))}
      </Stack>
      <FormHelperText>
        Email, phone, and LinkedIn are revealed by enrichment, so “Has” filters select from
        enriched leads. Company linkage comes from enrichment or from a company-scoped people
        search.
      </FormHelperText>
      {!hideLimit && (
        <TextField
          label="Result limit"
          type="number"
          value={limit}
          onChange={(e) => {
            const next = Number(e.target.value)
            if (!Number.isFinite(next)) return
            onLimitChange(Math.min(SIMILARITY_LIMIT_MAX, Math.max(1, Math.round(next))))
          }}
          helperText="Results beyond 100 are paginated"
          slotProps={{ htmlInput: { min: 1, max: SIMILARITY_LIMIT_MAX } }}
          sx={{ maxWidth: 220 }}
        />
      )}
    </>
  )
}
