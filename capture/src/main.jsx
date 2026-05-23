import ReactDOM from 'react-dom/client';
import App from './App.jsx';
import './styles.css';

// NOTE: no React.StrictMode here (unlike frontend/). Later sections open the
// camera inside an effect, and StrictMode's dev double-invoke would call
// getUserMedia twice and leak a stream. Keep it off in this app.
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
