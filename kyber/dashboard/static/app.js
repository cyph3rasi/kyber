/* ── Kyber Dashboard ── */
const API = '/api';
const TOKEN_KEY = 'kyber_dashboard_token';

// DOM refs
const $ = (s) => document.getElementById(s);
const loginModal = $('loginModal');
const tokenInput = $('tokenInput');
const tokenSubmit = $('tokenSubmit');
const statusPill = $('statusPill');
const statusText = $('statusText');
const savedAt = $('savedAt');
const pageTitle = $('pageTitle');
const pageDesc = $('pageDesc');
const contentBody = $('contentBody');
const saveBtn = $('saveBtn');
const restartGwBtn = $('restartGwBtn');
const restartDashBtn = $('restartDashBtn');
const toast = $('toast');

let config = null;
let configSnapshot = null;
let isDirty = false;
let activeSection = 'providers';
let toastTimer = null;

// Cache fetched models per provider to avoid re-fetching
const modelCache = {};

// ── Section metadata ──
const SECTIONS = {
  providers: {
    title: 'Providers',
    desc: 'Configure your LLM providers and select models.',
  },
  agents: {
    title: 'Agent',
    desc: 'Select your active provider and configure agent settings.',
  },
  channels: {
    title: 'Channels',
    desc: 'Enable and configure chat platform integrations.',
  },
  tools: {
    title: 'Tools',
    desc: 'Web search and shell execution settings.',
  },
  gateway: {
    title: 'Gateway',
    desc: 'Host and port for the Kyber gateway server.',
  },
  dashboard: {
    title: 'Dashboard',
    desc: 'Dashboard access, auth token, and allowed hosts.',
  },
  json: {
    title: 'Raw JSON',
    desc: 'View and edit the full configuration as JSON.',
  },
};

const BUILTIN_PROVIDERS = ['anthropic', 'openai', 'openrouter', 'deepseek', 'groq', 'gemini'];

// ── Helpers ──
function showToast(msg, type = 'info') {
  toast.textContent = msg;
  toast.className = 'toast ' + (type === 'error' ? 'error' : type === 'success' ? 'success' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.add('hidden'), 2500);
}

function getToken() { return sessionStorage.getItem(TOKEN_KEY) || ''; }
function setToken(t) { sessionStorage.setItem(TOKEN_KEY, t); }

function humanize(key) {
  return key
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function setPath(obj, path, val) {
  let t = obj;
  for (let i = 0; i < path.length - 1; i++) {
    if (t[path[i]] === undefined) t[path[i]] = {};
    t = t[path[i]];
  }
  t[path[path.length - 1]] = val;
}

function getPath(obj, path) {
  let t = obj;
  for (const k of path) {
    if (t == null) return undefined;
    t = t[k];
  }
  return t;
}

function isObj(v) { return v && typeof v === 'object' && !Array.isArray(v); }

function isSensitive(key) {
  const k = key.toLowerCase();
  return k.includes('token') || k.includes('key') || k.includes('secret');
}

function markDirty() {
  isDirty = true;
  saveBtn.disabled = false;
  saveBtn.classList.remove('disabled');
}

function markClean() {
  isDirty = false;
  configSnapshot = JSON.stringify(config);
  saveBtn.disabled = true;
  saveBtn.classList.add('disabled');
}

// ── API ──
async function apiFetch(path, opts = {}) {
  const headers = { ...opts.headers };
  const token = getToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;
  if (opts.body) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) {
    statusText.textContent = 'Locked';
    statusPill.className = 'status-pill error';
    showLogin();
    throw new Error('Unauthorized');
  }
  return res;
}

function showLogin() { loginModal.classList.remove('hidden'); tokenInput.value = ''; tokenInput.focus(); }
function hideLogin() { loginModal.classList.add('hidden'); }

async function loadConfig() {
  try {
    statusText.textContent = 'Connecting…';
    statusPill.className = 'status-pill';
    const res = await apiFetch(`${API}/config`);
    config = await res.json();
    statusText.textContent = 'Connected';
    statusPill.className = 'status-pill connected';
    markClean();
    renderSection();
  } catch (e) {
    console.error(e);
  }
}

async function saveConfig() {
  if (!config) return;
  let payload = config;

  if (activeSection === 'json') {
    const ta = contentBody.querySelector('.json-editor');
    if (ta) {
      try { payload = JSON.parse(ta.value); }
      catch { showToast('Invalid JSON', 'error'); return; }
    }
  }

  try {
    const res = await apiFetch(`${API}/config`, { method: 'PUT', body: JSON.stringify(payload) });
    config = await res.json();
    const gwRestarted = config._gatewayRestarted;
    const gwMessage = config._gatewayMessage;
    delete config._gatewayRestarted;
    delete config._gatewayMessage;
    savedAt.textContent = 'Saved ' + new Date().toLocaleTimeString();
    if (gwRestarted) {
      showToast('Saved — gateway restarted', 'success');
    } else {
      showToast('Saved — gateway restart failed: ' + (gwMessage || 'unknown'), 'error');
    }
    markClean();
    renderSection();
  } catch {
    showToast('Save failed', 'error');
  }
}

// ── Model fetching ──
async function fetchModels(providerName, apiKey, apiBase) {
  const cacheKey = `${providerName}:${apiKey}:${apiBase || ''}`;
  if (modelCache[cacheKey]) return modelCache[cacheKey];

  const params = new URLSearchParams({ apiKey });
  if (apiBase) params.set('apiBase', apiBase);

  const res = await apiFetch(`${API}/providers/${providerName}/models?${params}`);
  const data = await res.json();
  if (data.models && data.models.length > 0) {
    modelCache[cacheKey] = data.models;
    return data.models;
  }
  throw new Error(data.error || 'No models returned');
}

// ── Navigation ──
function switchSection(section) {
  activeSection = section;
  document.querySelectorAll('.nav-item').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.section === section);
  });
  const meta = SECTIONS[section] || {};
  pageTitle.textContent = meta.title || humanize(section);
  pageDesc.textContent = meta.desc || '';
  renderSection();
}

// ── Rendering ──
function renderSection() {
  if (!config) { contentBody.innerHTML = '<div class="empty-state">Loading configuration…</div>'; return; }
  contentBody.innerHTML = '';

  if (activeSection === 'json') { renderJSON(); return; }

  const data = config[activeSection];
  if (!data || !isObj(data)) {
    contentBody.innerHTML = '<div class="empty-state">No configuration for this section.</div>';
    return;
  }

  if (activeSection === 'providers') { renderProviders(data); return; }
  if (activeSection === 'channels') { renderChannels(data); return; }
  if (activeSection === 'agents') { renderAgents(data); return; }
  if (activeSection === 'tools') { renderTools(data); return; }
  if (activeSection === 'dashboard') { renderDashboard(data); return; }

  // Generic card
  const card = makeCard(humanize(activeSection));
  renderFields(card.body, data, [activeSection]);
  contentBody.appendChild(card.el);
}

// ── Card factory ──
function makeCard(title, badge) {
  const el = document.createElement('div');
  el.className = 'card';

  const header = document.createElement('div');
  header.className = 'card-header';
  const h = document.createElement('span');
  h.className = 'card-title';
  h.textContent = title;
  header.appendChild(h);

  if (badge !== undefined) {
    const b = document.createElement('span');
    b.className = 'card-badge' + (badge ? ' on' : '');
    b.textContent = badge ? 'Enabled' : 'Disabled';
    header.appendChild(b);
  }

  el.appendChild(header);
  const body = document.createElement('div');
  body.className = 'card-body';
  el.appendChild(body);
  contentBody.appendChild(el);
  return { el, body };
}

// ── Field rendering ──
function renderFields(container, obj, path) {
  for (const [key, value] of Object.entries(obj)) {
    const fullPath = [...path, key];

    if (isObj(value)) {
      const sub = document.createElement('div');
      sub.className = 'card';
      sub.style.marginTop = '12px';
      sub.style.border = '1px solid var(--border)';
      const sh = document.createElement('div');
      sh.className = 'card-header';
      sh.innerHTML = `<span class="card-title">${humanize(key)}</span>`;
      sub.appendChild(sh);
      const sb = document.createElement('div');
      sb.className = 'card-body';
      sub.appendChild(sb);
      renderFields(sb, value, fullPath);
      container.appendChild(sub);
      continue;
    }

    if (Array.isArray(value)) {
      renderArrayField(container, key, value, fullPath);
      continue;
    }

    renderField(container, key, value, fullPath);
  }
}

function renderField(container, key, value, path) {
  const row = document.createElement('div');
  row.className = 'field-row';

  const label = document.createElement('div');
  label.className = 'field-label';
  label.textContent = humanize(key);
  row.appendChild(label);

  const inputWrap = document.createElement('div');
  inputWrap.className = 'field-input';

  if (typeof value === 'boolean') {
    const wrap = document.createElement('div');
    wrap.className = 'checkbox-wrap';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = value;
    cb.id = 'cb-' + path.join('-');
    cb.addEventListener('change', () => {
      setPath(config, path, cb.checked);
      markDirty();
      if (key === 'enabled') renderSection();
    });
    wrap.appendChild(cb);
    const lbl = document.createElement('label');
    lbl.className = 'checkbox-label';
    lbl.htmlFor = cb.id;
    lbl.textContent = value ? 'Yes' : 'No';
    cb.addEventListener('change', () => { lbl.textContent = cb.checked ? 'Yes' : 'No'; });
    wrap.appendChild(lbl);
    inputWrap.appendChild(wrap);
  } else if (typeof value === 'number') {
    const inp = document.createElement('input');
    inp.type = 'number';
    inp.value = value;
    inp.addEventListener('input', () => {
      const n = Number(inp.value);
      setPath(config, path, Number.isNaN(n) ? 0 : n);
      markDirty();
    });
    inputWrap.appendChild(inp);
  } else {
    const inp = document.createElement('input');
    inp.type = isSensitive(key) ? 'password' : 'text';
    inp.value = value || '';
    inp.placeholder = isSensitive(key) ? '••••••••' : '';
    inp.addEventListener('input', () => { setPath(config, path, inp.value); markDirty(); });
    inputWrap.appendChild(inp);
  }

  row.appendChild(inputWrap);
  container.appendChild(row);
}

function renderArrayField(container, key, arr, path) {
  const row = document.createElement('div');
  row.className = 'field-row';
  row.style.alignItems = 'flex-start';

  const label = document.createElement('div');
  label.className = 'field-label';
  label.style.paddingTop = '8px';
  label.textContent = humanize(key);
  row.appendChild(label);

  const wrap = document.createElement('div');
  wrap.className = 'field-input array-field';

  const rebuild = () => {
    wrap.innerHTML = '';
    arr.forEach((item, i) => {
      const r = document.createElement('div');
      r.className = 'array-row';
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.value = item;
      inp.addEventListener('input', () => { arr[i] = inp.value; markDirty(); });
      r.appendChild(inp);

      const del = document.createElement('button');
      del.className = 'btn-icon danger';
      del.innerHTML = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
      del.addEventListener('click', () => { arr.splice(i, 1); markDirty(); rebuild(); });
      r.appendChild(del);
      wrap.appendChild(r);
    });

    const add = document.createElement('button');
    add.className = 'btn-add';
    add.innerHTML = '<svg width="10" height="10" viewBox="0 0 16 16" fill="none"><path d="M8 2v12M2 8h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg> Add';
    add.addEventListener('click', () => { arr.push(''); markDirty(); rebuild(); });
    wrap.appendChild(add);
  };

  rebuild();
  row.appendChild(wrap);
  container.appendChild(row);
}

// ── Provider card with API key + model dropdown ──

function renderProviderCard(name, provObj, configPath, opts = {}) {
  const apiKeyField = provObj.apiKey ?? provObj.api_key ?? '';
  const modelField = provObj.model ?? '';
  const hasKey = !!apiKeyField;
  const card = makeCard(opts.displayName || humanize(name), hasKey);

  // Keep a direct reference to the base input (avoids querySelector issues)
  let baseInpRef = null;

  // API Base (only for custom providers)
  if (opts.showApiBase) {
    const baseRow = document.createElement('div');
    baseRow.className = 'field-row';
    const baseLabel = document.createElement('div');
    baseLabel.className = 'field-label';
    baseLabel.textContent = 'API Base URL';
    baseRow.appendChild(baseLabel);
    const baseWrap = document.createElement('div');
    baseWrap.className = 'field-input';
    const baseInp = document.createElement('input');
    baseInp.type = 'text';
    baseInp.className = 'api-base-input';
    baseInp.value = provObj.apiBase ?? provObj.api_base ?? '';
    baseInp.placeholder = 'https://your-server.com/v1';
    baseInp.addEventListener('input', () => {
      setPath(config, [...configPath, 'apiBase'], baseInp.value);
      markDirty();
    });
    baseWrap.appendChild(baseInp);
    baseRow.appendChild(baseWrap);
    card.body.appendChild(baseRow);
    baseInpRef = baseInp;
  }

  // API Key row
  const keyRow = document.createElement('div');
  keyRow.className = 'field-row';
  const keyLabel = document.createElement('div');
  keyLabel.className = 'field-label';
  keyLabel.textContent = 'API Key';
  keyRow.appendChild(keyLabel);
  const keyWrap = document.createElement('div');
  keyWrap.className = 'field-input';
  const apiKeyInp = document.createElement('input');
  apiKeyInp.type = 'password';
  apiKeyInp.value = apiKeyField;
  apiKeyInp.placeholder = '••••••••';
  keyWrap.appendChild(apiKeyInp);
  keyRow.appendChild(keyWrap);
  card.body.appendChild(keyRow);

  // Model row
  const modelRow = document.createElement('div');
  modelRow.className = 'field-row';
  const modelLabel = document.createElement('div');
  modelLabel.className = 'field-label';
  modelLabel.textContent = 'Model';
  modelRow.appendChild(modelLabel);
  const modelWrap = document.createElement('div');
  modelWrap.className = 'field-input';
  modelRow.appendChild(modelWrap);
  card.body.appendChild(modelRow);

  // Determine the provider name to use for API calls
  const fetchName = opts.fetchName || name;

  function renderModelArea() {
    modelWrap.innerHTML = '';
    const currentKey = apiKeyInp.value.trim();
    // Read base URL directly from the captured input ref — no querySelector needed
    const currentBase = baseInpRef ? baseInpRef.value.trim() : null;

    if (!currentKey) {
      const hint = document.createElement('div');
      hint.className = 'model-hint';
      hint.textContent = 'Enter API key to see available models';
      modelWrap.appendChild(hint);
      return;
    }

    // Custom providers need a base URL too
    if (opts.showApiBase && !currentBase) {
      const hint = document.createElement('div');
      hint.className = 'model-hint';
      hint.textContent = 'Enter API base URL and API key to see available models';
      modelWrap.appendChild(hint);
      return;
    }

    // Show loading
    const loading = document.createElement('div');
    loading.className = 'model-hint';
    loading.textContent = 'Loading models…';
    modelWrap.appendChild(loading);

    fetchModels(fetchName, currentKey, currentBase)
      .then((models) => {
        modelWrap.innerHTML = '';
        const sel = document.createElement('select');
        // Add empty option
        const emptyOpt = document.createElement('option');
        emptyOpt.value = '';
        emptyOpt.textContent = '— Select a model —';
        sel.appendChild(emptyOpt);

        for (const m of models) {
          const opt = document.createElement('option');
          opt.value = m;
          opt.textContent = m;
          if (m === modelField) opt.selected = true;
          sel.appendChild(opt);
        }

        // If current model isn't in the list but is set, add it
        if (modelField && !models.includes(modelField)) {
          const opt = document.createElement('option');
          opt.value = modelField;
          opt.textContent = modelField + ' (current)';
          opt.selected = true;
          sel.appendChild(opt);
        }

        sel.addEventListener('change', () => {
          setPath(config, [...configPath, 'model'], sel.value);
          markDirty();
        });
        modelWrap.appendChild(sel);
      })
      .catch((err) => {
        modelWrap.innerHTML = '';
        const errEl = document.createElement('div');
        errEl.className = 'model-hint error-text';
        errEl.textContent = 'Failed to load models: ' + (err.message || err);
        modelWrap.appendChild(errEl);

        // Add retry button
        const retry = document.createElement('button');
        retry.className = 'btn-add';
        retry.textContent = 'Retry';
        retry.style.marginTop = '6px';
        retry.addEventListener('click', () => {
          // Clear cache for this combo
          const ck = `${fetchName}:${currentKey}:${currentBase || ''}`;
          delete modelCache[ck];
          renderModelArea();
        });
        modelWrap.appendChild(retry);
      });
  }

  // Wire up API key changes
  let keyDebounce = null;
  apiKeyInp.addEventListener('input', () => {
    setPath(config, [...configPath, 'apiKey'], apiKeyInp.value);
    markDirty();
    clearTimeout(keyDebounce);
    keyDebounce = setTimeout(renderModelArea, 600);
  });

  // Wire up API base changes via direct ref
  if (baseInpRef) {
    let baseDebounce = null;
    baseInpRef.addEventListener('input', () => {
      clearTimeout(baseDebounce);
      baseDebounce = setTimeout(renderModelArea, 600);
    });
  }

  // Initial render of model area
  renderModelArea();

  return card;
}

// ── Section-specific renderers ──

function renderProviders(data) {
  for (const name of BUILTIN_PROVIDERS) {
    const prov = data[name];
    if (!prov) continue;
    const opts = {};
    renderProviderCard(name, prov, ['providers', name], opts);
  }

  // Custom providers
  const customs = data.custom || [];
  customs.forEach((cp, i) => {
    const card = renderProviderCard(cp.name || `custom-${i}`, cp, ['providers', 'custom', i], {
      showApiBase: true,
      displayName: cp.name || `Custom Provider ${i + 1}`,
      fetchName: 'custom',
    });

    // Add name field at the top of the card body (before other fields)
    const nameRow = document.createElement('div');
    nameRow.className = 'field-row';
    const nameLabel = document.createElement('div');
    nameLabel.className = 'field-label';
    nameLabel.textContent = 'Provider Name';
    nameRow.appendChild(nameLabel);
    const nameWrap = document.createElement('div');
    nameWrap.className = 'field-input';
    const nameInp = document.createElement('input');
    nameInp.type = 'text';
    nameInp.value = cp.name || '';
    nameInp.placeholder = 'e.g. ollama, together, etc.';
    nameInp.addEventListener('input', () => {
      setPath(config, ['providers', 'custom', i, 'name'], nameInp.value);
      markDirty();
    });
    nameWrap.appendChild(nameInp);
    nameRow.appendChild(nameWrap);
    card.body.insertBefore(nameRow, card.body.firstChild);

    // Add delete button in card header
    const delBtn = document.createElement('button');
    delBtn.className = 'btn-icon danger';
    delBtn.title = 'Remove this provider';
    delBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
    delBtn.addEventListener('click', () => {
      config.providers.custom.splice(i, 1);
      markDirty();
      renderSection();
    });
    card.el.querySelector('.card-header').appendChild(delBtn);
  });

  // Add custom provider button
  const addBtn = document.createElement('button');
  addBtn.className = 'btn btn-ghost btn-full';
  addBtn.style.marginTop = '16px';
  addBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M8 2v12M2 8h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg> Add Custom Provider';
  addBtn.addEventListener('click', () => {
    if (!config.providers.custom) config.providers.custom = [];
    config.providers.custom.push({ name: '', apiBase: '', apiKey: '', model: '' });
    markDirty();
    renderSection();
  });
  contentBody.appendChild(addBtn);
}

function renderChannels(data) {
  const channelNames = ['discord', 'telegram', 'whatsapp'];
  for (const name of channelNames) {
    const ch = data[name];
    if (!ch) continue;
    const card = makeCard(humanize(name), ch.enabled);
    renderFields(card.body, ch, ['channels', name]);
  }
}

function renderAgents(data) {
  if (!data.defaults) {
    const card = makeCard('Agent');
    renderFields(card.body, data, ['agents']);
    return;
  }

  const defaults = data.defaults;
  const card = makeCard('Agent Defaults');

  // Provider dropdown — only show providers that have an API key and model configured
  const provRow = document.createElement('div');
  provRow.className = 'field-row';
  const provLabel = document.createElement('div');
  provLabel.className = 'field-label';
  provLabel.textContent = 'Provider';
  provRow.appendChild(provLabel);
  const provWrap = document.createElement('div');
  provWrap.className = 'field-input';

  const sel = document.createElement('select');
  const emptyOpt = document.createElement('option');
  emptyOpt.value = '';
  emptyOpt.textContent = '— Select a provider —';
  sel.appendChild(emptyOpt);

  const providers = config.providers || {};
  const currentProvider = (defaults.provider || '').toLowerCase();

  // Built-in providers with a key + model
  for (const name of BUILTIN_PROVIDERS) {
    const prov = providers[name];
    if (!prov) continue;
    const hasKey = !!(prov.apiKey || prov.api_key);
    const hasModel = !!prov.model;
    if (!hasKey) continue;
    const opt = document.createElement('option');
    opt.value = name;
    const modelInfo = hasModel ? ` (${prov.model})` : '';
    opt.textContent = humanize(name) + modelInfo;
    if (currentProvider === name) opt.selected = true;
    sel.appendChild(opt);
  }

  // Custom providers with a key + model
  const customs = providers.custom || [];
  for (const cp of customs) {
    if (!cp.name || !cp.apiKey) continue;
    const opt = document.createElement('option');
    opt.value = cp.name.toLowerCase();
    const modelInfo = cp.model ? ` (${cp.model})` : '';
    opt.textContent = cp.name + modelInfo;
    if (currentProvider === cp.name.toLowerCase()) opt.selected = true;
    sel.appendChild(opt);
  }

  // If current provider isn't in the list, add it
  if (currentProvider && !sel.querySelector(`option[value="${currentProvider}"]`)) {
    const opt = document.createElement('option');
    opt.value = currentProvider;
    opt.textContent = humanize(currentProvider) + ' (not configured)';
    opt.selected = true;
    sel.appendChild(opt);
  }

  sel.addEventListener('change', () => {
    setPath(config, ['agents', 'defaults', 'provider'], sel.value);
    markDirty();
  });

  provWrap.appendChild(sel);
  provRow.appendChild(provWrap);
  card.body.appendChild(provRow);

  // Render remaining agent defaults (excluding provider — we just rendered it)
  const otherFields = Object.fromEntries(
    Object.entries(defaults).filter(([k]) => k !== 'provider' && k !== 'model')
  );
  renderFields(card.body, otherFields, ['agents', 'defaults']);
}

function renderTools(data) {
  if (data.web) {
    if (data.web.search) {
      const card = makeCard('Brave Web Search');
      renderFields(card.body, data.web.search, ['tools', 'web', 'search']);
      card.body.querySelectorAll('.field-label').forEach((lbl) => {
        if (lbl.textContent === 'Api Key') lbl.textContent = 'Brave API Key';
      });
    }
  }
  if (data.exec) {
    const card = makeCard('Shell Execution');
    renderFields(card.body, data.exec, ['tools', 'exec']);
  }
}

function renderDashboard(data) {
  const card = makeCard('Dashboard Settings');
  const filtered = Object.fromEntries(
    Object.entries(data).filter(([k]) => k !== 'enabled')
  );
  renderFields(card.body, filtered, ['dashboard']);
}

function renderJSON() {
  const ta = document.createElement('textarea');
  ta.className = 'json-editor';
  ta.spellcheck = false;
  ta.value = JSON.stringify(config, null, 2);
  ta.addEventListener('input', () => {
    markDirty();
    try {
      JSON.parse(ta.value);
      ta.style.borderColor = '';
    } catch {
      ta.style.borderColor = 'var(--red)';
    }
  });
  contentBody.appendChild(ta);
}

// ── Event listeners ──
document.getElementById('sidebarNav').addEventListener('click', (e) => {
  const btn = e.target.closest('.nav-item');
  if (btn && btn.dataset.section) switchSection(btn.dataset.section);
});

saveBtn.addEventListener('click', saveConfig);

restartGwBtn.addEventListener('click', async () => {
  restartGwBtn.disabled = true;
  restartGwBtn.textContent = 'Restarting…';
  try {
    const res = await apiFetch(`${API}/restart-gateway`, { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      showToast('Gateway restarted', 'success');
    } else {
      showToast('Restart failed: ' + data.message, 'error');
    }
  } catch {
    showToast('Restart request failed', 'error');
  } finally {
    restartGwBtn.disabled = false;
    restartGwBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M2 2v5h5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M3.05 10A6 6 0 1 0 4 4.5L2 7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg> Restart Gateway';
  }
});

restartDashBtn.addEventListener('click', async () => {
  restartDashBtn.disabled = true;
  restartDashBtn.textContent = 'Restarting…';
  try {
    const res = await apiFetch(`${API}/restart-dashboard`, { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      showToast('Dashboard restarting — page will reload', 'success');
      // Give the service time to restart (1s delay + unload/load), then reload
      setTimeout(() => location.reload(), 4000);
    } else {
      showToast('Restart failed: ' + data.message, 'error');
    }
  } catch {
    showToast('Restart request failed', 'error');
  } finally {
    restartDashBtn.disabled = false;
    restartDashBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M2 2v5h5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M3.05 10A6 6 0 1 0 4 4.5L2 7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg> Restart Dashboard';
  }
});

tokenSubmit.addEventListener('click', async () => {
  const t = tokenInput.value.trim();
  if (!t) return;
  setToken(t);
  hideLogin();
  await loadConfig();
});

tokenInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') tokenSubmit.click();
});

// ── Init ──
window.addEventListener('load', async () => {
  if (!getToken()) { showLogin(); }
  else { await loadConfig(); }
});
