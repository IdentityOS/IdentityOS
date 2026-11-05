const csrf = document.querySelector('meta[name="identityos-csrf"]').content;
const panels = ["welcome", "identity", "provider", "verification", "conversation"];
let providers = [];

function showPanel(name) {
  panels.forEach(panel => document.getElementById(`${panel}-panel`).classList.toggle("hidden", panel !== name));
  document.getElementById(`${name}-panel`).querySelector("h1, input, select, textarea, button")?.focus();
}

function notice(message, isError = true) {
  const element = document.getElementById("notice");
  element.textContent = message;
  element.classList.toggle("error", isError);
  element.classList.remove("hidden");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {"Content-Type": "application/json", "X-IdentityOS-CSRF": csrf, ...(options.headers || {})},
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.message || `Request failed (${response.status})`);
  return body;
}

function selectProvider() {
  const provider = providers.find(item => item.name === document.getElementById("provider-select").value);
  if (!provider) return;
  document.getElementById("provider-model").value = provider.default_model || "";
  document.getElementById("provider-base-url").value = provider.default_base_url || "";
  document.getElementById("base-url-row").classList.toggle("hidden", !provider.default_base_url);
  document.getElementById("api-key-row").classList.toggle("hidden", !provider.requires_secret);
  document.getElementById("provider-api-key").required = provider.requires_secret;
  document.getElementById("provider-note").textContent = provider.description + (provider.detected ? "" : " This provider was not detected on PATH.");
}

async function loadHistory() {
  const result = await api("/onboarding/api/history");
  const messages = document.getElementById("messages");
  messages.innerHTML = "";
  result.messages.forEach(item => addMessage(item.role, item.content, true));
}

function addMessage(role, content, history = false) {
  const element = document.createElement("div");
  element.className = `message ${role}${history ? " history" : ""}`;
  element.textContent = content;
  document.getElementById("messages").appendChild(element);
  element.scrollIntoView({block: "nearest"});
}

async function boot() {
  try {
    const [state, options] = await Promise.all([api("/onboarding/api/state"), api("/onboarding/api/providers")]);
    providers = options.providers;
    const select = document.getElementById("provider-select");
    providers.forEach(provider => {
      const option = document.createElement("option");
      option.value = provider.name;
      option.textContent = `${provider.display_name}${provider.local ? " — local" : " — cloud"}`;
      select.appendChild(option);
    });
    selectProvider();
    document.getElementById("storage-location").textContent = `Data: ${state.paths.identity_store}`;
    if (state.onboarding.ready) {
      showPanel("conversation");
      await loadHistory();
    } else if (state.provider) showPanel("verification");
    else if (state.identity) showPanel("provider");
  } catch (error) { notice(error.message); }
}

document.getElementById("begin-button").addEventListener("click", () => showPanel("identity"));
document.getElementById("provider-select").addEventListener("change", selectProvider);
document.getElementById("retry-provider-button").addEventListener("click", () => showPanel("provider"));
document.getElementById("change-provider-button").addEventListener("click", () => showPanel("provider"));

document.getElementById("identity-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.submitter; button.disabled = true;
  try {
    await api("/onboarding/api/identity", {method: "POST", body: JSON.stringify({name: document.getElementById("identity-name").value, purpose: document.getElementById("identity-purpose").value})});
    showPanel("provider");
  } catch (error) { notice(error.message); } finally { button.disabled = false; }
});

document.getElementById("provider-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.submitter; button.disabled = true;
  try {
    await api("/onboarding/api/provider", {method: "POST", body: JSON.stringify({provider: document.getElementById("provider-select").value, model: document.getElementById("provider-model").value, base_url: document.getElementById("provider-base-url").value, api_key: document.getElementById("provider-api-key").value})});
    document.getElementById("provider-api-key").value = "";
    showPanel("verification");
  } catch (error) { notice(error.message); } finally { button.disabled = false; }
});

document.getElementById("verify-button").addEventListener("click", async event => {
  const button = event.currentTarget; button.disabled = true; button.textContent = "Verifying…";
  const result = document.getElementById("verification-result");
  try {
    const data = await api("/onboarding/api/verify", {method: "POST", body: JSON.stringify({})});
    result.textContent = `Verified with ${data.provenance.provider} · ${data.provenance.model} · ${data.provenance.latency_ms ?? "unknown"} ms`;
    result.className = "verification-result";
    addMessage("assistant", data.output);
    showPanel("conversation");
  } catch (error) {
    result.textContent = error.message; result.className = "verification-result error";
    document.getElementById("retry-provider-button").classList.remove("hidden");
  } finally { button.disabled = false; button.textContent = "Run verification"; }
});

document.getElementById("chat-form").addEventListener("submit", async event => {
  event.preventDefault();
  const input = document.getElementById("chat-message");
  const message = input.value.trim(); if (!message) return;
  const button = event.submitter; button.disabled = true; addMessage("user", message); input.value = "";
  try {
    const data = await api("/onboarding/api/chat", {method: "POST", body: JSON.stringify({message})});
    addMessage("assistant", data.output);
    document.getElementById("last-provenance").textContent = `${data.provenance.provider} · ${data.provenance.model} · ${data.provenance.latency_ms ?? "unknown"} ms`;
  } catch (error) { notice(error.message); } finally { button.disabled = false; input.focus(); }
});

boot();
