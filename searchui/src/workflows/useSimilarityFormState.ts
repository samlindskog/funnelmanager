import { useCallback, useEffect, useRef, useState } from 'react'
import { listRecentCompanies, resolveCompany, type CompanyOption, type EmbedKind } from '../api'
import { selectedEmbedKinds, type TriState } from '../components/similarity'
import type { ResolveIngestPhase } from './useResolveIngest'

/** The stage-3 similarity-form state shared by the domain-driven workflows'
 * final step (Prospect's "Search the set", Top-people's "Pick the people"):
 * passage, embed toggles, company OR chips (with add-by-id resolution), and the
 * tri-state contact filters. It also owns the "pre-fill company chips from the
 * completed run once" behavior. Each workflow adds its own limit/submit on top. */

export interface UseSimilarityFormState {
  query: string
  setQuery: (value: string) => void
  embeds: Record<EmbedKind, boolean>
  setEmbeds: (next: Record<EmbedKind, boolean>) => void
  companies: CompanyOption[]
  setCompanies: (next: CompanyOption[]) => void
  companyOptions: CompanyOption[]
  resolvingCompany: boolean
  companyResolveError: string | null
  /** Resolve a free-typed record/Apollo id to a named company and append it. */
  addCompanyValue: (raw: string) => void
  emailFilter: TriState
  setEmailFilter: (next: TriState) => void
  phoneFilter: TriState
  setPhoneFilter: (next: TriState) => void
  linkedinFilter: TriState
  setLinkedinFilter: (next: TriState) => void
  /** Derived: ordered selected embed kinds and whether any is selected. */
  selEmbeds: EmbedKind[]
  hasEmbeds: boolean
  /** Derived: resolved company ids (mongo id preferred, else Apollo id). */
  companyValues: string[]
  /** Derived: any company chip or non-"any" contact filter is set. */
  hasFilter: boolean
  /** Clear the company chips + resolve error (query/embeds/filters persist). */
  reset: () => void
}

export function useSimilarityFormState(opts: {
  resolvedCompanies: CompanyOption[]
  phase: ResolveIngestPhase
}): UseSimilarityFormState {
  const { resolvedCompanies, phase } = opts

  const [query, setQuery] = useState('')
  const [embeds, setEmbeds] = useState<Record<EmbedKind, boolean>>({
    apollo: true,
    name: true,
    title: true,
  })
  const [companies, setCompaniesRaw] = useState<CompanyOption[]>([])
  const [companyOptions, setCompanyOptions] = useState<CompanyOption[]>([])
  // Once the user edits the company chips, their set wins — the auto-derive below
  // stops overwriting it. Reset whenever a new run starts (phase leaves `done`).
  const companiesTouchedRef = useRef(false)
  const setCompanies = useCallback((next: CompanyOption[]) => {
    companiesTouchedRef.current = true
    setCompaniesRaw(next)
  }, [])
  const [resolvingCompany, setResolvingCompany] = useState(false)
  const [companyResolveError, setCompanyResolveError] = useState<string | null>(null)
  const [emailFilter, setEmailFilter] = useState<TriState>('any')
  const [phoneFilter, setPhoneFilter] = useState<TriState>('any')
  const [linkedinFilter, setLinkedinFilter] = useState<TriState>('any')

  // Load the recent-companies dropdown for the company picker.
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

  // DERIVE the company chips from the run's resolved orgs while the final step is
  // unlocked (phase==='done') and the user hasn't edited the set. This re-runs on
  // every resolvedCompanies change, so even a mis-timed done→partial-set transition
  // self-corrects as the rest of the companies settle (belt-and-suspenders with the
  // hook's drain-until-settled advance). A new run (phase leaves `done`) re-arms it.
  useEffect(() => {
    if (phase === 'done') {
      if (!companiesTouchedRef.current) setCompaniesRaw(resolvedCompanies)
    } else {
      companiesTouchedRef.current = false
    }
  }, [phase, resolvedCompanies])

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
        // Explicit user action — their set wins from here on.
        companiesTouchedRef.current = true
        setCompaniesRaw((current) =>
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

  const reset = useCallback(() => {
    companiesTouchedRef.current = false
    setCompaniesRaw([])
    setCompanyResolveError(null)
  }, [])

  const selEmbeds = selectedEmbedKinds(embeds)
  const hasEmbeds = selEmbeds.length > 0
  const companyValues = companies
    .map((c) => (c.mongo_id || c.apollo_id || '').trim())
    .filter(Boolean)
  const hasFilter =
    companyValues.length > 0 ||
    emailFilter !== 'any' ||
    phoneFilter !== 'any' ||
    linkedinFilter !== 'any'

  return {
    query,
    setQuery,
    embeds,
    setEmbeds,
    companies,
    setCompanies,
    companyOptions,
    resolvingCompany,
    companyResolveError,
    addCompanyValue: (raw: string) => void addCompanyValue(raw),
    emailFilter,
    setEmailFilter,
    phoneFilter,
    setPhoneFilter,
    linkedinFilter,
    setLinkedinFilter,
    selEmbeds,
    hasEmbeds,
    companyValues,
    hasFilter,
    reset,
  }
}
