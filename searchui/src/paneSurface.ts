import type { Theme } from '@mui/material/styles'
import type { SystemStyleObject } from '@mui/system'

/** Shared list-pane surfaces so history + results match in both color modes. */
export function paneSurfaceBg(theme: Theme, active: boolean): string {
  if (theme.palette.mode === 'dark') {
    // Active is only a slight lift from paper — avoid a strong green wash.
    return active ? '#131C2E' : '#10182A'
  }
  return active ? '#E5EEE9' : '#F3F7F4'
}

/** Parent rules: highlight panes via data-list-focus without re-rendering lists. */
export function listFocusPaneSx(theme: Theme): SystemStyleObject {
  return {
    '& .pane-history, & .pane-results': {
      backgroundColor: paneSurfaceBg(theme, false),
    },
    '&[data-list-focus="history"] .pane-history': {
      backgroundColor: paneSurfaceBg(theme, true),
    },
    '&[data-list-focus="results"] .pane-results': {
      backgroundColor: paneSurfaceBg(theme, true),
    },
  }
}
