/** Toggle one key, or select the inclusive range from an anchor when shift-clicking. */
export function toggleOrRangeSelect<T>(
  orderedKeys: readonly T[],
  current: Set<T>,
  target: T,
  anchorIndex: number | null,
  shiftKey: boolean,
): { next: Set<T>; nextAnchor: number | null } {
  const index = orderedKeys.indexOf(target)
  if (index < 0) return { next: current, nextAnchor: anchorIndex }

  if (shiftKey && anchorIndex !== null) {
    const from = Math.min(anchorIndex, index)
    const to = Math.max(anchorIndex, index)
    const next = new Set(current)
    for (let i = from; i <= to; i++) next.add(orderedKeys[i])
    return { next, nextAnchor: anchorIndex }
  }

  const next = new Set(current)
  if (next.has(target)) next.delete(target)
  else next.add(target)
  return { next, nextAnchor: index }
}
