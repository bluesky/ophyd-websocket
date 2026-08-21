import React from 'react'
import ReactDOM from 'react-dom/client'
import { FinchConfigProvider } from '@blueskyproject/finch'
import App from './App.tsx'
import { ophydApiUrl } from './lib/config.ts'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    {/* finch's ophyd hooks derive their ws:// URLs from ophydApiUrl. */}
    <FinchConfigProvider config={{ ophydApiUrl }}>
      <App />
    </FinchConfigProvider>
  </React.StrictMode>,
)
