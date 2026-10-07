"use client";

import { useEffect, useState } from "react";

type BackendStatus = "checking" | "connected" | "unavailable";

interface HealthData {
  status: string;
  service: string;
}

interface ReadinessData {
  status: string;
  dependencies: {
    postgres: string;
    redis: string;
  };
}

export default function Home() {
  const [status, setStatus] = useState<BackendStatus>("checking");
  const [healthData, setHealthData] = useState<HealthData | null>(null);
  const [readinessData, setReadinessData] = useState<ReadinessData | null>(null);
  const [lastChecked, setLastChecked] = useState<string>("");

  const backendUrl =
    process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

  const checkHealth = async () => {
    setStatus("checking");
    try {
      const res = await fetch(`${backendUrl}/health`, { cache: "no-store" });
      if (!res.ok) {
        throw new Error(`Health check returned status ${res.status}`);
      }
      const data: HealthData = await res.json();
      setHealthData(data);
      setStatus("connected");

      // Attempt to fetch readiness probe details
      try {
        const readyRes = await fetch(`${backendUrl}/health/ready`, { cache: "no-store" });
        if (readyRes.ok) {
          const readyData: ReadinessData = await readyRes.json();
          setReadinessData(readyData);
        }
      } catch {
        // Readiness details are optional for basic liveness view
      }
    } catch {
      setStatus("unavailable");
      setHealthData(null);
      setReadinessData(null);
    } finally {
      setLastChecked(new Date().toLocaleTimeString());
    }
  };

  useEffect(() => {
    checkHealth();
  }, []);

  return (
    <main className="container">
      <div className="card">
        <h1 className="title">Conversational Analytics Agent</h1>
        <p className="subtitle">Production-oriented AI analytics platform</p>

        <div className="status-badge">
          <span className={`status-dot ${status}`} />
          <span>
            Backend status:{" "}
            {status === "checking" && "Checking..."}
            {status === "connected" && "Connected"}
            {status === "unavailable" && "Unavailable"}
          </span>
        </div>

        <div className="details-grid">
          <div className="detail-item">
            <span className="detail-label">Backend URL</span>
            <span className="detail-value">{backendUrl}</span>
          </div>

          <div className="detail-item">
            <span className="detail-label">Last Checked</span>
            <span className="detail-value">{lastChecked || "Pending"}</span>
          </div>

          {healthData && (
            <div className="detail-item">
              <span className="detail-label">Service</span>
              <span className="detail-value">{healthData.service}</span>
            </div>
          )}

          {readinessData && (
            <>
              <div className="detail-item">
                <span className="detail-label">PostgreSQL</span>
                <span className="detail-value">{readinessData.dependencies.postgres}</span>
              </div>
              <div className="detail-item">
                <span className="detail-label">Redis</span>
                <span className="detail-value">{readinessData.dependencies.redis}</span>
              </div>
            </>
          )}
        </div>

        <div>
          <button className="retry-button" onClick={checkHealth}>
            Re-check Health
          </button>
        </div>
      </div>
    </main>
  );
}
