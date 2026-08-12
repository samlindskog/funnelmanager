import type { EmbedKind } from '../api'

/** Contact exists filter: Any = omit, Has = require present, Missing = require absent. */
export type TriState = 'any' | 'has' | 'missing'

export const SIMILARITY_LIMIT_MAX = 10000

export const EMBED_KINDS: Array<{ value: EmbedKind; label: string }> = [
  { value: 'apollo', label: 'Apollo profile' },
  { value: 'name', label: 'Name' },
  { value: 'title', label: 'Title' },
]

/** Tri-state select value → the leads exists-filter boolean (Any omits). */
export function triToBool(state: TriState): boolean | undefined {
  return state === 'has' ? true : state === 'missing' ? false : undefined
}

/** Embed record → the ordered list of selected kinds. */
export function selectedEmbedKinds(embeds: Record<EmbedKind, boolean>): EmbedKind[] {
  return EMBED_KINDS.map((k) => k.value).filter((value) => embeds[value])
}
