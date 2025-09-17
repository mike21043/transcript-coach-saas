// src/App.js
import React from "react";
import { BrowserRouter as Router, Routes, Route, Link } from "react-router-dom";
import TranscriptViewer from "./components/TranscriptViewer";
import AdminSettings from "./components/AdminSettings";
import Results from "./components/Results";

function App() {
  return (
    <Router>
      <div>
        <nav style={{ padding: "1rem", background: "#eee" }}>
          <Link to="/" style={{ marginRight: "1rem" }}>Upload</Link>
          <Link to="/results" style={{ marginRight: "1rem" }}>Results</Link>
          <Link to="/admin">Admin</Link>
        </nav>
        <div style={{ padding: "1rem" }}>
          <Routes>
            <Route path="/" element={<TranscriptViewer />} />
            <Route path="/results" element={<Results />} />
            <Route path="/admin" element={<AdminSettings />} />
          </Routes>
        </div>
      </div>
    </Router>
  );
}

export default App;
