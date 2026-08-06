import {
  Box,
  Dialog,
  DialogContent,
  DialogTitle,
  Divider,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from '@mui/material'
import type { StatsResponse, UsageStat } from './types'

function num(value: number | null | undefined): string {
  return typeof value === 'number' ? value.toLocaleString() : '—'
}

function cost(value: number | null | undefined): string {
  return typeof value === 'number' ? `$${value.toFixed(4)}` : '—'
}

function Stat({ label, value, testid }: { label: string; value: string; testid: string }) {
  return (
    <Stack spacing={0.25} data-testid={testid}>
      <Typography variant="caption" color="text.secondary">
        {label}
      </Typography>
      <Typography variant="h6" sx={{ fontFamily: 'monospace', fontWeight: 600 }}>
        {value}
      </Typography>
    </Stack>
  )
}

/** Token-usage view: per-response, current-context (last request input size), and
 * cumulative for THIS session, plus per-model "bots" stats from GET /stats
 * (cross-user, principle 1). */
export function UsagePanel({
  open,
  onClose,
  sessionUsage,
  lastUsage,
  stats,
}: {
  open: boolean
  onClose: () => void
  sessionUsage: UsageStat
  lastUsage: UsageStat | null
  stats: StatsResponse | null
}) {
  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle>Token usage</DialogTitle>
      <DialogContent dividers>
        <Typography variant="subtitle2" sx={{ mb: 1 }}>
          This session
        </Typography>
        <Stack direction="row" spacing={4} sx={{ flexWrap: 'wrap', mb: 1 }} useFlexGap>
          <Stat
            label="Context (last request input)"
            value={num(lastUsage?.input_tokens)}
            testid="agents-usage-context"
          />
          <Stat label="Last response output" value={num(lastUsage?.output_tokens)} testid="agents-usage-last-response" />
          <Stat label="Cumulative input" value={num(sessionUsage.input_tokens)} testid="agents-usage-cum-input" />
          <Stat label="Cumulative output" value={num(sessionUsage.output_tokens)} testid="agents-usage-cum-output" />
          <Stat label="Total tokens" value={num(sessionUsage.total_tokens)} testid="agents-usage-cum-total" />
          <Stat label="Cost" value={cost(sessionUsage.cost)} testid="agents-usage-cum-cost" />
        </Stack>

        <Divider sx={{ my: 2 }} />

        <Typography variant="subtitle2" sx={{ mb: 1 }}>
          By model (all users)
        </Typography>
        {stats == null ? (
          <Typography variant="body2" color="text.secondary">
            Loading…
          </Typography>
        ) : stats.by_model.length === 0 ? (
          <Typography variant="body2" color="text.secondary">
            No recorded usage yet.
          </Typography>
        ) : (
          <Box data-testid="agents-usage-bots">
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Model</TableCell>
                  <TableCell align="right">Turns</TableCell>
                  <TableCell align="right">Requests</TableCell>
                  <TableCell align="right">Tools</TableCell>
                  <TableCell align="right">Input</TableCell>
                  <TableCell align="right">Output</TableCell>
                  <TableCell align="right">Cost</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {stats.by_model.map((row) => (
                  <TableRow key={row.model} data-testid={`agents-usage-bot-${row.model}`}>
                    <TableCell sx={{ fontFamily: 'monospace' }}>{row.model}</TableCell>
                    <TableCell align="right">{num(row.turns)}</TableCell>
                    <TableCell align="right">{num(row.requests)}</TableCell>
                    <TableCell align="right">{num(row.tool_calls)}</TableCell>
                    <TableCell align="right">{num(row.input_tokens)}</TableCell>
                    <TableCell align="right">{num(row.output_tokens)}</TableCell>
                    <TableCell align="right">{cost(row.cost)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <Typography variant="caption" color="text.secondary" sx={{ mt: 1, display: 'block' }}>
              Total cost across all models: <strong>{cost(stats.total_cost)}</strong>
            </Typography>
          </Box>
        )}
      </DialogContent>
    </Dialog>
  )
}
