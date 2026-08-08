import type { Config } from 'tailwindcss'

const config: Config = {
  content: [
    './app/**/*.{ts,tsx}',
    './components/**/*.{ts,tsx}',
    './lib/**/*.{ts,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        ink: '#111827',
        muted: '#6b7280',
        line: '#e5e7eb',
        accent: '#1d4ed8',
      },
      maxWidth: {
        page: '1080px',
      },
    },
  },
  plugins: [],
}

export default config
