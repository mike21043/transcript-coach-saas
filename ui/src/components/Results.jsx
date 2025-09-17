// src/components/Results.jsx
import React, { useEffect, useState } from "react";
import { getResults } from "../api";

function Results() {
  const [results, setResults] = useState([]);
  const [expandedId, setExpandedId] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    async function fetchResults() {
      try {
        const data = await getResults();
        // Sort newest first by datetime if available
        const sorted = [...data].sort((a, b) => {
          return new Date(b.datetime) - new Date(a.datetime);
        });
        setResults(sorted);
      } catch (err) {
        setError("Failed to load results");
        console.error(err);
      }
    }
    fetchResults();
  }, []);

  const toggleExpand = (id) => {
    setExpandedId(expandedId === id ? null : id);
  };

  if (error) {
    return <div className="p-4 text-red-600">{error}</div>;
  }

  if (results.length === 0) {
    return <div className="p-4">No transcripts found.</div>;
  }

  return (
    <div className="p-4">
      <h2 className="text-xl font-semibold mb-4">Transcripts</h2>
      <ul className="space-y-2">
        {results.map((result) => (
          <li
            key={result.id}
            className="border rounded-lg shadow-sm bg-white"
          >
            <button
              className="w-full flex justify-between items-center px-4 py-2 text-left"
              onClick={() => toggleExpand(result.id)}
            >
              <span className="font-medium">{result.filename}</span>
              <span className="text-sm text-gray-500">
                {new Date(result.datetime).toLocaleString()}
              </span>
            </button>

            {expandedId === result.id && (
              <div className="px-4 pb-4 text-sm text-gray-700 whitespace-pre-wrap">
                {result.transcript || "No transcript details available."}
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

export default Results;
