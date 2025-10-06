// src/api.js

// Base URL for the API
const API_BASE = process.env.REACT_APP_API_URL || "http://38.242.200.197:8000";

// Upload a file
export async function uploadFile(file) {
  const formData = new FormData();
  formData.append("file", file);

  const response = await fetch(`${API_BASE}/upload`, {
    method: "POST",
    body: formData,
  });

  if (!response.ok) {
    throw new Error("Upload failed");
  }
  return response.json();
}

// Get job status
export async function getStatus(jobId) {
  const response = await fetch(`${API_BASE}/status/${jobId}`);
  if (!response.ok) {
    throw new Error("Failed to fetch status");
  }
  return response.json();
}

// Get job result
export async function getResult(jobId) {
  const response = await fetch(`${API_BASE}/result/${jobId}`);
  if (!response.ok) {
    throw new Error("Failed to fetch result");
  }
  return response.json();
}

// Get all results
export async function getResults() {
  const response = await fetch(`${API_BASE}/results`);
  if (!response.ok) {
    throw new Error("Failed to fetch results");
  }
  return response.json();
}

export async function getProcesses() {
  const response = await fetch(`${API_BASE}/admin/processes`);
  if (!response.ok) throw new Error('Failed to fetch processes');
  return response.json();
}

export async function startProcess(procId) {
  const response = await fetch(`${API_BASE}/admin/processes/${procId}/start`, { method: 'POST' });
  if (!response.ok) throw new Error('Failed to start process');
  return response.json();
}
