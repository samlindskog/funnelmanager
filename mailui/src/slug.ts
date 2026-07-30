/** Kebab-case a free-form string into a stable testid qualifier (e.g. a
 * campaign owner "John Doe" → `john-doe`). Non-alphanumerics collapse to
 * hyphens so dynamic-list testids stay deterministic. NOT injective — distinct
 * inputs that differ only by punctuation collapse to the same slug, so prefer a
 * stable unique id (a numeric row id) for the qualifier where one exists. */
export function slug(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}
