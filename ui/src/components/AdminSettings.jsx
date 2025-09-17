import React, { useEffect, useState } from "react";

export default function AdminSettings() {
  const [idleTimeout, setIdleTimeout] = useState(1800);
  const [staleTicks, setStaleTicks] = useState(15);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");

  // Load current values from API
  useEffect(() => {
    fetch("http://localhost:8000/settings")
      .then(res => res.json())
      .then(data => {
        setIdleTimeout(data.IDLE_TIMEOUT);
        setStaleTicks(data.STALE_TICKS);
      })
      .catch(err => console.error("Failed to load settings", err));
  }, []);

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
    </div>
  );
}
