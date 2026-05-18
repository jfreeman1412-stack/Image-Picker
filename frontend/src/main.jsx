import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import JobList from './pages/JobList.jsx';
import JobWizard from './pages/JobWizard.jsx';
import JobDetail from './pages/JobDetail.jsx';
import SessionDetail from './pages/SessionDetail.jsx';
import Settings from './pages/Settings.jsx';
import './styles.css';

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<JobList />} />
        <Route path="/job/new" element={<JobWizard />} />
        <Route path="/job/:id" element={<JobDetail />} />
        <Route path="/session/:id" element={<SessionDetail />} />
        <Route path="/settings" element={<Settings />} />
      </Routes>
    </BrowserRouter>
  </React.StrictMode>
);
