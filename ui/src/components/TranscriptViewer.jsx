// src/components/TranscriptViewer.jsx
import React, { useState } from "react";
import { uploadFile, getStatus, getResult } from "../api";

function TranscriptViewer() {
  const [file, setFile] = useState(null);
  const [jobId, setJobId] = useState(null);
  const [status, setStatus] = useState(null);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  const handleFileChange = (e) => {
    setFile(e.target.files[0]);
    setJobId(null);
    setStatus(null);
    setResult(null);
    setError(null);
  };

  const handleUpload = async () => {
    if (!file) return;
    try {
      const data = await uploadFile(file);
      setJobId(data.job_id);
      setStatus("queued");
      // store saved filename if API provided it (job uses saved_filename on server)
      if (data.saved_filename) {
        setResult({ _meta: { saved_filename: data.saved_filename } });
      }
      pollStatus(data.job_id);
    } catch (err) {
      setError("Upload failed");
      console.error(err);
    }
  };

  const pollStatus = async (id) => {
    let attempts = 0;
    const maxAttempts = 30;

    const interval = setInterval(async () => {
      try {
        const res = await getStatus(id);
        setStatus(res.status);
        if (res.status === "done") {
          clearInterval(interval);
          const resultData = await getResult(id);
          setResult(resultData);
        }
      } catch (err) {
        clearInterval(interval);
        setError("Error checking status");
      }
      attempts++;
      if (attempts > maxAttempts) {
        clearInterval(interval);
        setError("Timed out waiting for result");
      }
    }, 5000);
  };

  return (
    <div className="p-4">
      <h2 className="text-xl font-semibold mb-4">Upload Audio</h2>

      <input type="file" onChange={handleFileChange} className="mb-2" />
      <button
        onClick={handleUpload}
        disabled={!file}
        className="bg-blue-600 text-white px-4 py-2 rounded disabled:opacity-50"
      >
        Upload
      </button>

      {status && (
        <div className="mt-4">
          <p>Status: {status}</p>
        </div>
      )}

      {result && (
        <div className="mt-4">
          <h3 className="font-semibold">Transcript Result</h3>
          <div className="text-sm text-gray-600 mb-2">
            {result._meta && result._meta.saved_filename && (
              <div>Saved filename: {result._meta.saved_filename}</div>
            )}
          </div>
          <pre className="bg-gray-100 p-2 rounded whitespace-pre-wrap">
            {JSON.stringify(result, null, 2)}
          </pre>
        </div>
      )}

      {error && <div className="text-red-600 mt-4">{error}</div>}
    </div>
  );
}

export default TranscriptViewer;
