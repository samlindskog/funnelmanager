import WarningAmberIcon from '@mui/icons-material/WarningAmber'
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  Typography,
} from '@mui/material'
import type { ConfirmationRequired } from './types'

/** Human-readable rendering of an estimate + its unit (Principle 4 dialogs). */
function formatEstimate(value: number, unit: string): string {
  if (unit === 'bytes') {
    if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GB`
    if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB`
    if (value >= 1024) return `${Math.round(value / 1024)} KB`
    return `${Math.round(value)} B`
  }
  const rounded = Number.isInteger(value) ? value : Math.round(value)
  return unit ? `${rounded.toLocaleString()} ${unit}` : rounded.toLocaleString()
}

/**
 * Principle-4 confirmation dialog: renders the backend's estimate for an
 * expensive action and, on confirm, re-invokes the guarded call with
 * `confirm=true` + the echoed `confirm_token` (the human confirm flow).
 */
export function ConfirmationDialog({
  detail,
  busy,
  confirmLabel = 'Confirm & proceed',
  onConfirm,
  onCancel,
}: {
  detail: ConfirmationRequired | null
  busy?: boolean
  confirmLabel?: string
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <Dialog open={detail != null} onClose={() => !busy && onCancel()} maxWidth="xs" fullWidth>
      {detail && (
        <>
          <DialogTitle>
            <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
              <WarningAmberIcon color="warning" />
              <span>Confirmation required</span>
            </Stack>
          </DialogTitle>
          <DialogContent>
            <Stack spacing={2}>
              <Typography variant="body2">{detail.message}</Typography>
              <Box
                sx={{
                  display: 'grid',
                  gridTemplateColumns: 'auto 1fr',
                  columnGap: 2,
                  rowGap: 0.5,
                  fontSize: 14,
                }}
              >
                <Typography variant="body2" color="text.secondary">
                  Estimate
                </Typography>
                <Typography variant="body2" sx={{ fontWeight: 600 }}>
                  {formatEstimate(detail.estimate, detail.unit)}
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  Threshold
                </Typography>
                <Typography variant="body2">
                  {formatEstimate(detail.threshold, detail.unit)}
                </Typography>
              </Box>
              <Alert severity="warning" variant="outlined">
                This exceeds the guarded threshold. It will only run once you confirm.
              </Alert>
            </Stack>
          </DialogContent>
          <DialogActions>
            <Button data-testid="mail-confirm-cancel" onClick={onCancel} disabled={busy}>
              Cancel
            </Button>
            <Button
              data-testid="mail-confirm"
              variant="contained"
              color="warning"
              onClick={onConfirm}
              disabled={busy}
              startIcon={busy ? <CircularProgress size={14} /> : undefined}
            >
              {confirmLabel}
            </Button>
          </DialogActions>
        </>
      )}
    </Dialog>
  )
}
