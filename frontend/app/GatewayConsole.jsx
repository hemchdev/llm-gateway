"use client";

import { useEffect, useState } from "react";

const defaultProvider = {
  name: "my-inference",
  model: "openai/my-model",
  api_base: "https://your-inference.example.com/v1",
  api_key: "",
  request_classes: "classification",
  priority: 1,
  enabled: true
};

const grafanaUrl = process.env.NEXT_PUBLIC_GRAFANA_URL || "http://localhost:3000/d/llm-gateway/llm-gateway";
const prometheusUrl = process.env.NEXT_PUBLIC_PROMETHEUS_URL || "http://localhost:9090";
const docsUrl = process.env.NEXT_PUBLIC_DOCS_URL || "http://localhost:8000/docs";
const storageKey = "llm-gateway-console";

function requestId() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return `req-${Date.now()}`;
}

async function gatewayFetch(path, options = {}) {
  const response = await fetch(`/api/gateway${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {})
    }
  });
  const text = await response.text();
  let payload = text;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = text;
  }
  if (!response.ok) {
    const message = payload?.error?.message || payload?.detail || response.statusText;
    throw new Error(`${response.status}: ${message}`);
  }
  return payload;
}

export default function GatewayConsole() {
  const [adminKey, setAdminKey] = useState("");
  const [tenantId, setTenantId] = useState("demo-tenant");
  const [feature, setFeature] = useState("classification");
  const [requestClass, setRequestClass] = useState("classification");
  const [provider, setProvider] = useState(defaultProvider);
  const [runtimeProviders, setRuntimeProviders] = useState([]);
  const [providerStatus, setProviderStatus] = useState([]);
  const [prompt, setPrompt] = useState("Classify this message: I was charged twice this month.");
  const [model, setModel] = useState("gpt-4o-mini");
  const [temperature, setTemperature] = useState(0.2);
  const [chatResult, setChatResult] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  function metadataHeaders() {
    return {
      "X-Tenant-Id": tenantId,
      "X-Feature": feature,
      "X-Request-Id": requestId()
    };
  }

  async function loadProviderStatus() {
    const status = await gatewayFetch("/v1/providers", {
      headers: metadataHeaders()
    });
    setProviderStatus(status.providers || []);
  }

  async function loadRuntimeProviders() {
    if (!adminKey) {
      setRuntimeProviders([]);
      return;
    }
    const data = await gatewayFetch("/admin/providers", {
      headers: { "X-Admin-Key": adminKey }
    });
    setRuntimeProviders(data.providers || []);
  }

  async function refreshAll() {
    setError("");
    try {
      await Promise.all([loadProviderStatus(), loadRuntimeProviders()]);
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    try {
      const saved = JSON.parse(window.localStorage.getItem(storageKey) || "{}");
      if (saved.adminKey) setAdminKey(saved.adminKey);
      if (saved.tenantId) setTenantId(saved.tenantId);
      if (saved.feature) setFeature(saved.feature);
      if (saved.requestClass) setRequestClass(saved.requestClass);
      if (saved.model) setModel(saved.model);
      if (saved.provider) setProvider({ ...defaultProvider, ...saved.provider, api_key: "" });
    } catch {
      window.localStorage.removeItem(storageKey);
    }
    loadProviderStatus().catch((err) => setError(err.message));
  }, []);

  useEffect(() => {
    window.localStorage.setItem(
      storageKey,
      JSON.stringify({
        adminKey,
        tenantId,
        feature,
        requestClass,
        model,
        provider: { ...provider, api_key: "" }
      })
    );
  }, [adminKey, tenantId, feature, requestClass, model, provider]);

  useEffect(() => {
    if (adminKey) {
      refreshAll();
    }
  }, [adminKey]);

  async function saveProvider(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const payload = {
        ...provider,
        priority: Number(provider.priority),
        request_classes: provider.request_classes
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean)
      };
      if (!payload.api_key) {
        delete payload.api_key;
      }
      const saved = await gatewayFetch("/admin/providers", {
        method: "POST",
        headers: { "X-Admin-Key": adminKey },
        body: JSON.stringify(payload)
      });
      setNotice(`Saved ${saved.name}. API key is stored encrypted in Redis and shown only as ${saved.api_key || "masked"}.`);
      await refreshAll();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function deleteProvider(name) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await gatewayFetch(`/admin/providers/${encodeURIComponent(name)}`, {
        method: "DELETE",
        headers: { "X-Admin-Key": adminKey }
      });
      setNotice(`Deleted ${name}.`);
      await refreshAll();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function sendChat(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    setChatResult(null);
    try {
      const payload = {
        model,
        messages: [{ role: "user", content: prompt }],
        temperature: Number(temperature),
        max_tokens: 256,
        metadata: { request_class: requestClass }
      };
      const response = await gatewayFetch("/v1/chat/completions", {
        method: "POST",
        headers: metadataHeaders(),
        body: JSON.stringify(payload)
      });
      setChatResult(response);
      setNotice(`Chat completed via ${response.model}.`);
      await loadProviderStatus();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function injectChaos(name) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await gatewayFetch("/admin/chaos", {
        method: "POST",
        headers: { "X-Admin-Key": adminKey },
        body: JSON.stringify({
          provider: name,
          duration_seconds: 60,
          rate: 1,
          error_type: "server_error",
          latency_ms: 0
        })
      });
      setNotice(`Injected server_error chaos into ${name} for 60 seconds.`);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const savedProviderCount = runtimeProviders.length;
  const closedProviderCount = providerStatus.filter((item) => item.circuit_state === "closed").length;
  const openProviderCount = providerStatus.filter((item) => item.circuit_state === "open").length;
  const halfOpenProviderCount = providerStatus.filter((item) => item.circuit_state === "half_open").length;
  const activeRoute = providerStatus.find((item) => item.circuit_state !== "open")?.name || "No active route";
  const lastResponse = chatResult?.choices?.[0]?.message?.content || "Send a request to see the routed response here.";

  return (
    <main className="shell">
      <header className="hero">
        <div className="heroCopy">
          <div className="brandLine">
            <span className="brandMark">LG</span>
            <span>LLM Gateway Console</span>
          </div>
          <p className="eyebrow">Self-healing LLM routing</p>
          <h1>Operate OpenAI-compatible providers from one clean console.</h1>
          <p className="lede">
            Save encrypted provider credentials, send chat traffic, inject failures, and jump straight into the monitoring stack when a route changes.
          </p>
          <nav className="links" aria-label="Monitoring links">
            <a href={grafanaUrl}>Grafana</a>
            <a href={prometheusUrl}>Prometheus</a>
            <a href={docsUrl}>API Docs</a>
          </nav>
        </div>
        <aside className="heroPanel" aria-label="Gateway snapshot">
          <div className="routeStrip">
            <span>Current route</span>
            <strong>{activeRoute}</strong>
          </div>
          <div className="miniConsole">
            <div>
              <span>Tenant</span>
              <strong>{tenantId || "Not set"}</strong>
            </div>
            <div>
              <span>Feature</span>
              <strong>{feature || "Not set"}</strong>
            </div>
            <div>
              <span>Class</span>
              <strong>{requestClass || "Not set"}</strong>
            </div>
          </div>
          <p>{lastResponse}</p>
        </aside>
      </header>

      <section className="statGrid" aria-label="Gateway summary">
        <div className="statCard">
          <span>Saved providers</span>
          <strong>{savedProviderCount}</strong>
        </div>
        <div className="statCard">
          <span>Closed circuits</span>
          <strong>{closedProviderCount}</strong>
        </div>
        <div className="statCard">
          <span>Half open</span>
          <strong>{halfOpenProviderCount}</strong>
        </div>
        <div className="statCard warn">
          <span>Open circuits</span>
          <strong>{openProviderCount}</strong>
        </div>
      </section>

      <section className="controls commandBar">
        <label>
          Admin key
          <input value={adminKey} onChange={(event) => setAdminKey(event.target.value)} type="password" placeholder="X-Admin-Key" />
        </label>
        <label>
          Tenant
          <input value={tenantId} onChange={(event) => setTenantId(event.target.value)} />
        </label>
        <label>
          Feature
          <input value={feature} onChange={(event) => setFeature(event.target.value)} />
        </label>
        <button type="button" onClick={refreshAll} disabled={busy}>
          Refresh
        </button>
      </section>

      {error ? <p className="alert">{error}</p> : null}
      {notice ? <p className="notice">{notice}</p> : null}

      <section className="layout">
        <form className="panel" onSubmit={saveProvider}>
          <div className="panelHead">
            <h2>Custom OpenAI-compatible API</h2>
            <span>Stored in Redis</span>
          </div>
          <label>
            Provider name
            <input value={provider.name} onChange={(event) => setProvider({ ...provider, name: event.target.value })} />
          </label>
          <label>
            Model
            <input value={provider.model} onChange={(event) => setProvider({ ...provider, model: event.target.value })} placeholder="openai/my-model" />
          </label>
          <label>
            API base URL
            <input value={provider.api_base} onChange={(event) => setProvider({ ...provider, api_base: event.target.value })} placeholder="https://host/v1" />
          </label>
          <label>
            API key
            <input value={provider.api_key} onChange={(event) => setProvider({ ...provider, api_key: event.target.value })} type="password" placeholder="Stored encrypted in Redis" />
          </label>
          <div className="split">
            <label>
              Request classes
              <input value={provider.request_classes} onChange={(event) => setProvider({ ...provider, request_classes: event.target.value })} />
            </label>
            <label>
              Priority
              <input value={provider.priority} onChange={(event) => setProvider({ ...provider, priority: event.target.value })} type="number" min="1" />
            </label>
          </div>
          <label className="toggle">
            <input checked={provider.enabled} onChange={(event) => setProvider({ ...provider, enabled: event.target.checked })} type="checkbox" />
            Enabled
          </label>
          <button type="submit" disabled={busy || !adminKey}>
            Save provider
          </button>
        </form>

        <form className="panel chat" onSubmit={sendChat}>
          <div className="panelHead">
            <h2>Chat</h2>
            <span>OpenAI-compatible request</span>
          </div>
          <div className="split">
            <label>
              Request class
              <input value={requestClass} onChange={(event) => setRequestClass(event.target.value)} />
            </label>
            <label>
              Model
              <input value={model} onChange={(event) => setModel(event.target.value)} />
            </label>
          </div>
          <label>
            Temperature
            <input value={temperature} onChange={(event) => setTemperature(event.target.value)} type="number" min="0" max="2" step="0.1" />
          </label>
          <label>
            Message
            <textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={7} />
          </label>
          <button type="submit" disabled={busy}>
            Send chat
          </button>
          {chatResult ? (
            <output>
              <strong>{chatResult.model}</strong>
              <span>{chatResult.choices?.[0]?.message?.content || "No assistant content returned."}</span>
            </output>
          ) : null}
        </form>
      </section>

      <section className="tables">
        <div className="panel">
          <div className="panelHead">
            <h2>Runtime providers</h2>
            <span>{runtimeProviders.length} saved</span>
          </div>
          <ProviderTable providers={runtimeProviders} adminKey={adminKey} onDelete={deleteProvider} onChaos={injectChaos} />
        </div>
        <div className="panel">
          <div className="panelHead">
            <h2>Routing status</h2>
            <span>{providerStatus.length} visible</span>
          </div>
          <StatusTable providers={providerStatus} />
        </div>
      </section>
    </main>
  );
}

function ProviderTable({ providers, adminKey, onDelete, onChaos }) {
  if (!adminKey) {
    return <p className="empty">Enter the admin key and refresh to view Redis-backed custom providers.</p>;
  }
  if (!providers.length) {
    return <p className="empty">No custom providers saved yet.</p>;
  }
  return (
    <div className="tableWrap">
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Model</th>
            <th>Classes</th>
            <th>Key</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {providers.map((item) => (
            <tr key={item.name}>
              <td>{item.name}</td>
              <td>{item.model}</td>
              <td>{(item.request_classes || []).join(", ")}</td>
              <td>{item.api_key || "none"}</td>
              <td className="actions">
                <button type="button" onClick={() => onChaos(item.name)}>
                  Chaos
                </button>
                <button type="button" onClick={() => onDelete(item.name)}>
                  Delete
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StatusTable({ providers }) {
  if (!providers.length) {
    return <p className="empty">No provider status loaded.</p>;
  }
  return (
    <div className="tableWrap">
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>State</th>
            <th>Model</th>
            <th>API base</th>
          </tr>
        </thead>
        <tbody>
          {providers.map((item) => (
            <tr key={item.name}>
              <td>{item.name}</td>
              <td>
                <span className={`state ${item.circuit_state}`}>{item.circuit_state}</span>
              </td>
              <td>{item.model}</td>
              <td>{item.api_base || "default"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
