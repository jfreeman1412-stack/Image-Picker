import ReactDOM from 'react-dom/client';
import { registerSW } from 'virtual:pwa-register';
import App from './App.jsx';
import { requestPersistentStorage } from './db.js';
import './styles.css';

// Phase B.3 — register the service worker that precaches the app shell so the
// app opens with no connection (Decision 4). `autoUpdate` (vite.config.js) means
// a newer build takes over on the next launch; `immediate` registers on load.
// The SW treats /api as NetworkOnly — it caches the shell, never API data.
registerSW({ immediate: true });

// Ask the browser to keep our IndexedDB queue from being evicted under storage
// pressure (Decision 1). Installed PWAs are usually granted this; fire-and-forget.
requestPersistentStorage();

// NOTE: no React.StrictMode here (unlike frontend/). useCamera opens the camera
// inside an effect, and StrictMode's dev double-invoke would call getUserMedia
// twice and leak a stream. Keep it off in this app.
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
