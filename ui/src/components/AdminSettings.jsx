import React, { useEffect, useState } from "react";
import { getProcesses, startProcess } from "../api";

export default function AdminSettings() {
  const [idleTimeout, setIdleTimeout] = useState(1800);
  const [staleTicks, setStaleTicks] = useState(15);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const [processes, setProcesses] = useState([]);
  const [procLoading, setProcLoading] = useState(false);

  // Load current values from API
  useEffect(() => {
    fetch("http://localhost:8000/settings")
      .then(res => res.json())
      .then(data => {
        setIdleTimeout(data.IDLE_TIMEOUT);
        setStaleTicks(data.STALE_TICKS);
      })
      .catch(err => console.error("Failed to load settings", err));

    // load processes
    loadProcesses();
  }, []);

  async function loadProcesses() {
    setProcLoading(true);
    try {
      const data = await getProcesses();
      setProcesses(data.processes || []);
    } catch (err) {
      console.error("Failed to load processes", err);
    } finally {
      setProcLoading(false);
    }
  }

  const handleSave = async () => {
    setLoading(true);
    setMessage("");
    try {
      const res = await fetch(`http://localhost:8000/settings?idle_timeout=${idleTimeout}&stale_ticks=${staleTicks}`, {
        method: "POST"
      });
      const data = await res.json();
      setMessage("✅ Saved settings successfully");
    } catch (err) {
      console.error(err);
      setMessage("❌ Failed to save settings");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="p-4 max-w-md mx-auto bg-white rounded-xl shadow-md space-y-4">
      <h2 className="text-xl font-bold">Admin Settings</h2>

      <div>
        <label className="block mb-1 font-medium">Idle Timeout (seconds)</label>
        <input
          type="number"
          value={idleTimeout}
          onChange={(e) => setIdleTimeout(e.target.value)}
          className="border p-2 rounded w-full"
        />
      </div>

      <div>
        <label className="block mb-1 font-medium">Stale Ticks (20s each)</label>
        <input
          type="number"
          value={staleTicks}
          onChange={(e) => setStaleTicks(e.target.value)}
          className="border p-2 rounded w-full"
        />
      </div>

      <button
        onClick={handleSave}
        disabled={loading}
        className="bg-blue-600 text-white px-4 py-2 rounded hover:bg-blue-700"
      >
        {loading ? "Saving..." : "Save"}
      </button>

      {message && <p className="mt-2">{message}</p>}

      <div className="pt-4 border-t">
        <h3 className="text-lg font-semibold">Required Processes</h3>
        {procLoading ? (
          <div className="text-sm text-gray-500">Loading...</div>
        ) : (
          <ul className="mt-2 space-y-2">
            {processes.map((p) => (
              <li key={p.id} className="flex items-center justify-between bg-gray-50 p-2 rounded">
                <div className="flex items-center">
                  <span className="mr-3 text-lg">
                    {p.ok ? <span style={{color: 'green'}}>✔️</span> : <span style={{color: 'red'}}>❌</span>}
                  </span>
                  <div>
                    <div className="font-medium">{p.name}</div>
                    <div className="text-xs text-gray-500">{p.detail}</div>
                  </div>
                </div>
                {!p.ok && (
                  <button
                    onClick={async () => {
                      try {
                        await startProcess(p.id);
                        // reload statuses
                        await loadProcesses();
                      } catch (err) {
                        console.error('Failed to start process', err);
                      }
                    }}
                    className="bg-red-600 text-white px-2 py-1 rounded"
                  >
                    Start
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
