import DarkModeOutlinedIcon from '@mui/icons-material/DarkModeOutlined'
import LightModeOutlinedIcon from '@mui/icons-material/LightModeOutlined'
import { IconButton, Tooltip } from '@mui/material'
import { useColorScheme } from '@mui/material/styles'

export function ColorModeToggle() {
  const { mode, setMode, systemMode } = useColorScheme()

  if (!mode) return null

  const resolved = mode === 'system' ? systemMode : mode
  const isDark = resolved === 'dark'

  return (
    <Tooltip title={isDark ? 'Switch to light mode' : 'Switch to dark mode'}>
      <IconButton
        color="inherit"
        size="small"
        aria-label={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
        onClick={() => setMode(isDark ? 'light' : 'dark')}
      >
        {isDark ? <LightModeOutlinedIcon fontSize="small" /> : <DarkModeOutlinedIcon fontSize="small" />}
      </IconButton>
    </Tooltip>
  )
}
