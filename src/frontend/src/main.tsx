import ReactDOM from 'react-dom/client'
import { FinchConfigProvider } from '@blueskyproject/finch'
import App from './App.tsx'
import { ophydApiUrl } from './lib/config.ts'
import './index.css'

// Deliberately not wrapped in <React.StrictMode>. finch's camera hook opens its
// websocket behind a one-shot "already initialised" ref, so StrictMode's
// simulated unmount closes the socket and the remount refuses to reopen it,
// leaving the canvas permanently disconnected in dev.
ReactDOM.createRoot(document.getElementById('root')!).render(
  <FinchConfigProvider config={{ ophydApiUrl }}>
    <App />
  </FinchConfigProvider>,
)
