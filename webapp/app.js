// Test web app: calls every service endpoint and renders status + body.
// Served by `make webapp` (port 3000); the service runs on port 8080 with
// CORS for this origin enabled by default.

// Default matches `make serve`; override for a non-default port with
// ?base=http://localhost:9000
const PARAMS = new URLSearchParams(location.search);
const BASE = PARAMS.get("base") || "http://localhost:8080";

// The service authenticates every /v1 call. `make all` links here with
// ?key=..., and the field below stays editable so a wrong key can be tried
// against the real 401 path.
const KEY_FIELD = document.getElementById("apikey");
KEY_FIELD.value = PARAMS.get("key") || "local-dev-key";

function authHeaders() {
  const key = KEY_FIELD.value.trim();
  return key ? { authorization: `Bearer ${key}` } : {};
}

const CALLS = [
  { label: "GET /healthz", method: "GET", path: "/healthz" },
  { label: "GET /v1/models", method: "GET", path: "/v1/models" },
  {
    label: "POST /v1/completions",
    method: "POST",
    path: "/v1/completions",
    body: { engine: "claude", prompt: "Reply with exactly: OK" },
  },
  {
    label: "POST /v1/completions (stream)",
    method: "POST",
    path: "/v1/completions",
    body: { engine: "claude", prompt: "Count to 5", stream: true },
  },
  {
    label: "POST /v1/structured",
    method: "POST",
    path: "/v1/structured",
    body: {
      engine: "openai",
      prompt: "Extract the name: Jane Doe",
      response_schema: { type: "object", properties: { name: { type: "string" } } },
    },
  },
  {
    label: "POST /v1/conversations/turn",
    method: "POST",
    path: "/v1/conversations/turn",
    body: { engine: "claude", system_prompt: "You are terse.", messages: [] },
  },
  {
    label: "POST /v1/embeddings",
    method: "POST",
    path: "/v1/embeddings",
    body: { engine: "google-gemini", inputs: ["hello world"] },
  },
  {
    label: "POST /v1/tokens/count",
    method: "POST",
    path: "/v1/tokens/count",
    body: { engine: "claude", prompt: "How many tokens is this?" },
  },
  {
    label: "GET /v1/models (no key — expect 401)",
    method: "GET",
    path: "/v1/models",
    anonymous: true,
  },
  {
    label: "POST /v1/completions (invalid body — expect 422)",
    method: "POST",
    path: "/v1/completions",
    body: { prompt: "missing engine" },
  },
];

const log = document.getElementById("log");
const buttons = document.getElementById("buttons");
document.getElementById("base").textContent = BASE;

function cssClass(status) {
  if (status >= 200 && status < 300) return "ok";
  if (status === 501 || status === 422) return "warn";
  return "err";
}

function hint(status) {
  if (status === 401) {
    return "\n\nThe API key was missing or wrong. `make serve` uses "
      + "local-dev-key unless DEV_API_KEY is set.";
  }
  return "";
}

async function call(spec) {
  const started = performance.now();
  log.textContent = `${spec.label} ...`;
  try {
    const response = await fetch(BASE + spec.path, {
      method: spec.method,
      headers: {
        ...(spec.anonymous ? {} : authHeaders()),
        ...(spec.body ? { "content-type": "application/json" } : {}),
      },
      body: spec.body ? JSON.stringify(spec.body) : undefined,
    });
    const ms = Math.round(performance.now() - started);
    const text = await response.text();
    let pretty = text;
    try { pretty = JSON.stringify(JSON.parse(text), null, 2); } catch { /* keep raw */ }
    log.innerHTML = `<span class="${cssClass(response.status)}">${spec.label} → HTTP ${response.status} (${ms} ms)</span>\n${pretty}${hint(response.status)}`;
  } catch (error) {
    log.innerHTML = `<span class="err">${spec.label} → ${error}</span>\nIs the service running? Start it with: make serve`;
  }
}

for (const spec of CALLS) {
  const button = document.createElement("button");
  button.textContent = spec.label;
  button.onclick = () => call(spec);
  buttons.appendChild(button);
}
