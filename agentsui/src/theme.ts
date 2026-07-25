import { createTheme } from '@mui/material/styles'

const typography = {
  fontFamily: '"IBM Plex Sans", "Segoe UI", sans-serif',
  h4: { fontFamily: '"Fraunces", Georgia, serif', fontWeight: 600 },
  h5: { fontFamily: '"Fraunces", Georgia, serif', fontWeight: 600 },
  h6: { fontFamily: '"Fraunces", Georgia, serif', fontWeight: 600 },
  button: { textTransform: 'none' as const, fontWeight: 600 },
}

export const theme = createTheme({
  cssVariables: {
    colorSchemeSelector: 'data',
  },
  colorSchemes: {
    light: {
      palette: {
        primary: {
          main: '#0B3D2E',
          contrastText: '#F4F7F5',
        },
        secondary: {
          main: '#C45C26',
        },
        background: {
          default: '#E8EEE9',
          paper: '#F7FAF8',
        },
        text: {
          primary: '#14201B',
          secondary: '#3F534A',
        },
        divider: 'rgba(20, 32, 27, 0.08)',
      },
    },
    dark: {
      palette: {
        primary: {
          main: '#5CB89A',
          contrastText: '#061018',
        },
        secondary: {
          main: '#E07A3A',
        },
        background: {
          default: '#070B14',
          paper: '#10182A',
        },
        text: {
          primary: '#E8EEF8',
          secondary: '#9AA8C2',
        },
        divider: 'rgba(232, 238, 248, 0.08)',
      },
    },
  },
  typography,
  shape: { borderRadius: 10 },
  components: {
    MuiButton: {
      styleOverrides: {
        root: { boxShadow: 'none' },
      },
    },
    MuiPaper: {
      styleOverrides: {
        root: { backgroundImage: 'none' },
      },
    },
    MuiDivider: {
      styleOverrides: {
        root: {
          borderColor: 'var(--mui-palette-divider)',
          borderBottomWidth: '1px',
        },
        vertical: {
          borderRightWidth: '1px',
          borderBottomWidth: 0,
        },
      },
    },
  },
})
