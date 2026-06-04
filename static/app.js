'use strict';

// ─── State ─────────────────────────────────────────────────
let ws = null;
let isStreaming = false;
let currentAssistantBubble = null;
let rawMarkdown = '';
let settings = {};
let latestScreenshotUrl = null;
let activityCount = 0;

// ─── Init ───────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  connectWebSocket();
  loadSettings();
  setMcpState('init', 'Initialising…');
  refreshBrowserStatus();
  setInterval(refreshBrowserStatus, 20000);
});

// ─── MCP Status Helpers ─────────────────────────────────────
function setMcpState(state, text) {
  // state: 'init' | 'ready' | 'busy' | 'error'
  const pill  = document.getElementById('mcp-pill');
  const label = document.getElementById('mcp-state');
  if (!pill || !label) return;
  pill.className = 'mcp-pill ' + state;
  label.textContent = text;
}

function setActiveTool(toolName) {
  const flash = document.getElementById('active-tool-flash');
  const name  = document.getElementById('active-tool-name');
  if (!flash) return;
  if (toolName) {
    flash.classList.remove('hidden');
    name.textContent = toolName;
    setMcpState('busy', `Running: ${toolName}`);
  } else {
    flash.classList.add('hidden');
    setMcpState('ready', 'Ready — browser connected');
  }
}

// ─── Activity Log ────────────────────────────────────────────
function addActivityEntry(toolName, detail, status) {
  // status: 'run' | 'ok' | 'err'
  const body  = document.getElementById('activity-log-body');
  const empty = document.getElementById('activity-empty');
  if (!body) return;
  if (empty) empty.style.display = 'none';

  const now  = new Date();
  const time = now.toTimeString().slice(0, 8);

  const entry = document.createElement('div');
  entry.className = 'activity-entry';

  const icons = { navigate: '🔗', click: '🖱', fill: '⌨️', submit_form: '📤',
                  find_pdfs: '🔍', download_pdf: '⬇️', print_to_pdf: '🖨',
                  screenshot: '📸', get_text: '📋', find_links: '🔍',
                  get_form_fields: '📋', execute_js: '⚡', scroll: '↕', go_back: '←',
                  type_text: '⌨️', select_option: '☑', wait_for_selector: '⏳', get_page_info: 'ℹ️' };
  const icon = icons[toolName] || '🔧';

  const statusClass = { run: 'ae-status-run', ok: 'ae-status-ok', err: 'ae-status-err' }[status] || '';
  const statusChar  = { run: '⟳', ok: '✓', err: '✗' }[status] || '';

  entry.innerHTML = `
    <span class="ae-time">${time}</span>
    <span class="ae-tool">${icon} ${toolName}</span>
    <span class="ae-detail">${escapeHtml(String(detail || '').slice(0, 80))}</span>
    <span class="${statusClass}">${statusChar}</span>`;

  body.appendChild(entry);
  body.scrollTop = body.scrollHeight;
  activityCount++;
}

function clearActivityLog() {
  const body  = document.getElementById('activity-log-body');
  const empty = document.getElementById('activity-empty');
  if (body) body.innerHTML = '';
  if (empty) { empty.style.display = ''; body.appendChild(empty); }
  activityCount = 0;
}

// ─── WebSocket ──────────────────────────────────────────────
function connectWebSocket() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${protocol}//${location.host}/ws/chat`);

  ws.onopen = () => {
    setMcpState('init', 'Connected — waiting for task…');
    refreshBrowserStatus();
  };

  ws.onmessage = (ev) => handleServerMessage(JSON.parse(ev.data));

  ws.onclose = () => {
    setMcpState('error', 'Disconnected — reconnecting…');
    setTimeout(connectWebSocket, 2500);
  };

  ws.onerror = () => setMcpState('error', 'Connection error');
}

function wsSend(data) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(data));
}

// ─── Server Message Handler ─────────────────────────────────
function handleServerMessage(msg) {
  switch (msg.type) {

    case 'mcp_ready':
      setMcpState('ready', `Ready — ${msg.tools} tools available`);
      break;

    case 'start':
      startAssistantMessage();
      break;

    case 'chunk':
      appendChunk(msg.content);
      break;

    case 'end':
      finalizeMessage();
      setActiveTool(null);
      break;

    case 'tool_status':
      appendToolStatus(msg.content);
      if (msg.tool) setActiveTool(msg.tool);
      break;

    case 'tool_call':
      setActiveTool(msg.tool);
      const argSummary = Object.entries(msg.args || {}).map(([k, v]) => `${k}=${String(v).slice(0,30)}`).join(', ');
      addActivityEntry(msg.tool, argSummary, 'run');
      updateThinking(`🔧 ${msg.tool}…`);
      break;

    case 'tool_result':
      // Update last activity entry to show result
      const success = msg.result?.success !== false;
      const resultDetail = msg.result?.url || msg.result?.title || msg.result?.value || (success ? 'OK' : msg.result?.error || 'Error');
      addActivityEntry(msg.tool, resultDetail, success ? 'ok' : 'err');

      if (msg.result?.screenshot) {
        showBrowserScreenshot(msg.result.screenshot);
      }
      break;

    case 'screenshot':
      showBrowserScreenshot(msg.url);
      break;

    case 'pdf_ready':
      appendPdfCard(msg.url, msg.filename);
      break;

    case 'error':
      appendError(msg.content);
      finalizeMessage();
      setActiveTool(null);
      setMcpState('ready', 'Ready — browser connected');
      break;

    case 'history_cleared':
    case 'settings_ack':
      break;
  }
}

// ─── Screenshot display ──────────────────────────────────────
function showBrowserScreenshot(url) {
  latestScreenshotUrl = url;
  const img   = document.getElementById('live-screenshot');
  const empty = document.getElementById('screenshot-empty');
  const label = document.getElementById('screenshot-label');
  const urlEl = document.getElementById('browser-info-url');

  if (img) {
    img.src = url + '?t=' + Date.now();
    img.classList.remove('hidden');
  }
  if (empty) empty.style.display = 'none';
  if (label) { label.classList.remove('hidden'); label.textContent = new Date().toLocaleTimeString(); }

  // Also show small screenshot in chat
  if (currentAssistantBubble) {
    appendScreenshot(url);
  }
  refreshBrowserStatus();
}

// ─── Chat helpers ────────────────────────────────────────────
function startAssistantMessage() {
  hideWelcome();
  isStreaming = true;
  rawMarkdown = '';
  document.getElementById('send-btn').disabled = true;
  document.getElementById('thinking-bar').classList.remove('hidden');

  const container = document.getElementById('chat-messages');
  const wrapper   = document.createElement('div');
  wrapper.className = 'message assistant';
  wrapper.innerHTML = `
    <div class="message-avatar">🤖</div>
    <div class="message-body" style="flex:1;min-width:0;">
      <div class="message-content" id="cur-assistant-content"></div>
    </div>`;
  container.appendChild(wrapper);
  currentAssistantBubble = document.getElementById('cur-assistant-content');
  scrollToBottom();
}

function appendChunk(chunk) {
  if (!currentAssistantBubble) startAssistantMessage();
  rawMarkdown += chunk;
  currentAssistantBubble.innerHTML = renderMarkdown(rawMarkdown);
  scrollToBottom();
}

function finalizeMessage() {
  isStreaming = false;
  currentAssistantBubble = null;
  rawMarkdown = '';
  document.getElementById('send-btn').disabled = false;
  document.getElementById('thinking-bar').classList.add('hidden');
  refreshFiles();
  refreshBrowserStatus();
}

function appendToolStatus(content) {
  const container = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = 'tool-status-msg';
  div.innerHTML = `<div class="tool-spinner"></div><span>${escapeHtml(content)}</span>`;
  container.appendChild(div);
  scrollToBottom();
}

function appendScreenshot(url) {
  if (!currentAssistantBubble) return;
  const img = document.createElement('img');
  img.src = url;
  img.className = 'chat-screenshot';
  img.alt = 'Browser screenshot';
  img.loading = 'lazy';
  img.onclick = () => openLightbox(url);
  currentAssistantBubble.parentElement.appendChild(img);
  scrollToBottom();
}

function appendPdfCard(url, filename) {
  const container = document.getElementById('chat-messages');
  const a = document.createElement('a');
  a.className = 'pdf-card';
  a.href = url; a.target = '_blank'; a.rel = 'noopener';
  a.innerHTML = `
    <div class="pdf-icon">📄</div>
    <div style="flex:1;min-width:0;">
      <div class="pdf-name">${escapeHtml(filename)}</div>
      <div class="pdf-meta">PDF Document · click to open</div>
    </div>
    <span class="pdf-open-btn">⬇ Open</span>`;
  container.appendChild(a);
  scrollToBottom();
}

function appendError(content) {
  const container = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = 'message assistant';
  div.innerHTML = `
    <div class="message-avatar">⚠️</div>
    <div class="message-body" style="flex:1;min-width:0;">
      <div class="message-content" style="border-color:hsla(0,85%,63%,.3);background:hsla(0,85%,63%,.07);">
        ${escapeHtml(content)}
      </div>
    </div>`;
  container.appendChild(div);
  scrollToBottom();
}

function updateThinking(text) {
  const el = document.getElementById('thinking-label');
  if (el) el.textContent = text;
}

function hideWelcome() {
  const wc = document.getElementById('welcome-card');
  if (wc) wc.style.display = 'none';
}

function scrollToBottom() {
  const el = document.getElementById('chat-messages');
  if (el) el.scrollTop = el.scrollHeight;
}

// ─── Sending ─────────────────────────────────────────────────
function sendMessage() {
  const input   = document.getElementById('chat-input');
  const content = input.value.trim();
  if (!content || isStreaming) return;

  hideWelcome();

  const container = document.getElementById('chat-messages');
  const userMsg   = document.createElement('div');
  userMsg.className = 'message user';
  userMsg.innerHTML = `
    <div class="message-avatar">👤</div>
    <div class="message-body" style="flex:1;min-width:0;">
      <div class="message-content">${escapeHtml(content)}</div>
    </div>`;
  container.appendChild(userMsg);
  scrollToBottom();

  wsSend({ type: 'chat', content });
  input.value = '';
  autoResize(input);
}

function handleKeyDown(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}

function sendExample(btn) {
  document.getElementById('chat-input').value = btn.textContent;
  sendMessage();
}

function clearChat() {
  if (isStreaming) return;
  document.getElementById('chat-messages').innerHTML = `
    <div class="welcome-card" id="welcome-card">
      <div class="welcome-icon">🎭</div>
      <h2>Playwright MCP Ready</h2>
      <p>The browser automation engine is connected. Just tell me what to do.</p>
      <div class="mcp-tool-chips">
        <span class="tool-chip">🔗 navigate</span><span class="tool-chip">🖱 click</span>
        <span class="tool-chip">⌨️ fill form</span><span class="tool-chip">📤 submit</span>
        <span class="tool-chip">📄 find PDFs</span><span class="tool-chip">⬇️ download PDF</span>
        <span class="tool-chip">🖨 print to PDF</span><span class="tool-chip">📸 screenshot</span>
      </div>
      <div class="example-prompts">
        <button class="example-prompt" onclick="sendExample(this)">Go to google.com and search for "Playwright automation"</button>
        <button class="example-prompt" onclick="sendExample(this)">Navigate to httpbin.org/forms/post, fill all fields and submit</button>
        <button class="example-prompt" onclick="sendExample(this)">Find and download all PDFs from https://www.w3.org/TR/</button>
        <button class="example-prompt" onclick="sendExample(this)">Take a screenshot of https://example.com</button>
      </div>
    </div>`;
  clearActivityLog();
  wsSend({ type: 'clear_history' });
}

// ─── Panel Navigation ─────────────────────────────────────────
function showPanel(name) {
  document.querySelectorAll('.right-panel').forEach(p => p.classList.remove('active'));
  const target = document.getElementById(`panel-${name}`);
  if (target) target.classList.add('active');
  if (name === 'files')   refreshFiles();
  if (name === 'browser') refreshBrowserStatus();
}

// ─── Settings ────────────────────────────────────────────────
function openSettings()  {
  document.getElementById('settings-panel').classList.add('active');
  document.getElementById('settings-overlay').classList.add('active');
  populateSettingsForm();
}

function closeSettings() {
  document.getElementById('settings-panel').classList.remove('active');
  document.getElementById('settings-overlay').classList.remove('active');
}

function switchProvider(p) {
  document.querySelectorAll('.provider-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.provider-config').forEach(c => c.classList.add('hidden'));
  document.querySelector(`[data-provider="${p}"]`)?.classList.add('active');
  document.getElementById(`config-${p}`)?.classList.remove('hidden');
}

async function loadSettings() {
  try {
    const resp = await fetch('/api/settings');
    settings = await resp.json();
    applySettings(settings);
  } catch (e) { console.error('Settings load failed:', e); }
}

function applySettings(s) {
  updateProviderBadge(s.active_provider || 'openai');
  const h = document.getElementById('headless-toggle');
  if (h) h.checked = s.playwright?.headless || false;
  const t = document.getElementById('browser-timeout');
  if (t) t.value = s.playwright?.timeout || 30000;
}

function populateSettingsForm() {
  if (!settings.providers) return;
  const p = settings.providers;
  setValue('openai-key',        p.openai?.api_key  || '');
  setValue('openai-base-url',   p.openai?.base_url || 'https://api.openai.com/v1');
  setSelectValue('openai-model', p.openai?.model   || 'gpt-4o');
  setValue('claude-key',        p.claude?.api_key  || '');
  setSelectValue('claude-model', p.claude?.model   || 'claude-opus-4-5');
  setValue('gemini-key',        p.gemini?.api_key  || '');
  setSelectValue('gemini-model', p.gemini?.model   || 'gemini-2.0-flash');
  setValue('openrouter-key',    p.openrouter?.api_key  || '');
  setValue('openrouter-model',  p.openrouter?.model    || 'openai/gpt-4o');
  setValue('openrouter-base-url', p.openrouter?.base_url || 'https://openrouter.ai/api/v1');
  const orReasoning = document.getElementById('openrouter-reasoning');
  if (orReasoning) orReasoning.checked = p.openrouter?.reasoning !== false;  // default true
  setValue('local-base-url',    p.local?.base_url  || 'http://localhost:11434/v1');
  setValue('local-model',       p.local?.model     || 'llama3');
  setValue('local-key',         p.local?.api_key   || 'ollama');
  setValue('browser-timeout',   settings.playwright?.timeout || 30000);
  const h = document.getElementById('headless-toggle');
  if (h) h.checked = settings.playwright?.headless || false;
  switchProvider(settings.active_provider || 'openai');
}

function setValue(id, val) { const el = document.getElementById(id); if (el) el.value = val; }
function setSelectValue(id, val) {
  const el = document.getElementById(id); if (!el) return;
  let found = false;
  for (const opt of el.options) { if (opt.value === val) { found = true; break; } }
  if (!found) { const o = document.createElement('option'); o.value = o.textContent = val; el.appendChild(o); }
  el.value = val;
}

function getActiveTab() {
  const a = document.querySelector('.provider-tab.active');
  return a ? a.dataset.provider : 'openai';
}

async function saveSettings() {
  const prov = getActiveTab();
  const ns = {
    active_provider: prov,
    providers: {
      openai:     { api_key: v('openai-key'),     model: v('openai-model'),     base_url: v('openai-base-url') },
      claude:     { api_key: v('claude-key'),     model: v('claude-model') },
      gemini:     { api_key: v('gemini-key'),     model: v('gemini-model') },
      openrouter: { api_key: v('openrouter-key'), model: v('openrouter-model'), base_url: v('openrouter-base-url'), reasoning: document.getElementById('openrouter-reasoning')?.checked !== false },
      local:      { api_key: v('local-key'),      model: v('local-model'),      base_url: v('local-base-url') },
    },
    playwright: {
      headless: document.getElementById('headless-toggle')?.checked || false,
      timeout:  parseInt(v('browser-timeout') || '30000'),
    },
  };
  try {
    const r = await fetch('/api/settings', { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(ns) });
    if (r.ok) {
      settings = ns;
      updateProviderBadge(prov);
      document.getElementById('settings-save-status').textContent = '✓ Saved!';
      setTimeout(() => { document.getElementById('settings-save-status').textContent = ''; closeSettings(); }, 900);
      wsSend({ type: 'settings_update' });
    } else {
      document.getElementById('settings-save-status').textContent = '✗ Error';
    }
  } catch { document.getElementById('settings-save-status').textContent = '✗ Network error'; }
}

function v(id) { return document.getElementById(id)?.value || ''; }

function updateProviderBadge(provider) {
  const labels = { openai:'OpenAI', claude:'Claude', gemini:'Gemini', openrouter:'OpenRouter', local:'Local AI' };
  const el = document.getElementById('ai-provider-name');
  if (el) el.textContent = labels[provider] || provider;
}

// ─── Browser Status ───────────────────────────────────────────
async function refreshBrowserStatus() {
  try {
    const resp = await fetch('/api/browser/status');
    const data = await resp.json();

    const statusEl = document.getElementById('browser-info-status');
    const urlEl    = document.getElementById('browser-info-url');
    const titleEl  = document.getElementById('browser-info-title');
    const badge    = document.getElementById('browser-status-badge');

    if (statusEl) statusEl.textContent = data.running ? '🟢 Running' : '⚫ Stopped';
    if (urlEl)    urlEl.textContent    = data.url   || 'No page loaded';
    if (titleEl)  titleEl.textContent  = data.title || '—';
    if (badge)    badge.textContent    = data.running ? '🟢 Running' : '⚫ Stopped';

    // Update MCP pill if not busy
    if (!isStreaming) {
      if (data.running) {
        setMcpState('ready', 'Ready — browser connected');
      } else {
        setMcpState('init', 'Browser not started');
      }
    }
  } catch { /* ignore */ }
}

async function startBrowser()   { await fetch('/api/browser/start', {method:'POST'}); await refreshBrowserStatus(); }
async function stopBrowser()    { await fetch('/api/browser/stop',  {method:'POST'}); await refreshBrowserStatus(); }
async function restartBrowser() {
  await fetch('/api/browser/stop', {method:'POST'});
  await new Promise(r => setTimeout(r, 600));
  await fetch('/api/browser/start', {method:'POST'});
  await refreshBrowserStatus();
}

// ─── Files ───────────────────────────────────────────────────
async function refreshFiles() {
  try {
    const r = await fetch('/api/files');
    renderFiles(await r.json());
  } catch { /* ignore */ }
}

function renderFiles(data) {
  const grid = document.getElementById('files-grid');
  if (!grid) return;
  const all = [
    ...data.screenshots.map(f => ({...f, ftype:'screenshot'})),
    ...data.downloads.map(f   => ({...f, ftype:'pdf'})),
  ].sort((a,b) => b.modified - a.modified);

  if (!all.length) { grid.innerHTML = '<div class="files-empty">No files yet. Automation tasks will save files here.</div>'; return; }

  grid.innerHTML = all.map(f => {
    const url  = f.ftype === 'screenshot' ? `/files/screenshots/${f.name}` : `/files/downloads/${f.name}`;
    const size = fmtBytes(f.size);
    if (f.ftype === 'screenshot') {
      return `<div class="file-card" onclick="openLightbox('${url}')">
        <img src="${url}" class="screenshot-thumb" alt="${escapeHtml(f.name)}" loading="lazy" />
        <div class="file-card-name">${escapeHtml(f.name)}</div>
        <div class="file-card-meta">${size}</div></div>`;
    }
    return `<a class="file-card" href="${url}" target="_blank" rel="noopener">
      <div class="file-card-icon">📄</div>
      <div class="file-card-name">${escapeHtml(f.name)}</div>
      <div class="file-card-meta">PDF · ${size}</div></a>`;
  }).join('');
}

function fmtBytes(b) {
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b/1024).toFixed(1) + ' KB';
  return (b/1048576).toFixed(1) + ' MB';
}

// ─── Lightbox ─────────────────────────────────────────────────
function openLightbox(url) {
  const lb = document.getElementById('lightbox');
  const img = document.getElementById('lightbox-img');
  if (lb && img) { img.src = url; lb.classList.remove('hidden'); document.body.style.overflow = 'hidden'; }
}
function closeLightbox() {
  document.getElementById('lightbox')?.classList.add('hidden');
  document.body.style.overflow = '';
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeLightbox(); });

// ─── Markdown ─────────────────────────────────────────────────
function renderMarkdown(text) {
  let h = escapeHtml(text);
  h = h.replace(/```[\w]*\n?([\s\S]*?)```/g, (_,c) => `<pre><code>${c.trim()}</code></pre>`);
  h = h.replace(/`([^`]+)`/g, '<code>$1</code>');
  h = h.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  h = h.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  h = h.replace(/~~([^~]+)~~/g, '<del>$1</del>');
  h = h.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  h = h.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  h = h.replace(/^# (.+)$/gm, '<h1>$1</h1>');
  h = h.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');
  h = h.replace(/^---$/gm, '<hr>');
  h = h.replace(/^[•\-\*] (.+)$/gm, '<li>$1</li>');
  h = h.replace(/(<li>.*<\/li>\n?)+/g, m => `<ul>${m}</ul>`);
  h = h.replace(/\[([^\]]+)\]\((https?:\/\/[^\)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  h = h.replace(/(https?:\/\/[^\s<>"]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
  h = h.split(/\n\n+/).map(para => {
    para = para.trim();
    if (!para) return '';
    if (/^<(h[1-6]|ul|ol|pre|blockquote|hr)/.test(para)) return para;
    return `<p>${para.replace(/\n/g, '<br>')}</p>`;
  }).join('\n');
  return h;
}

function escapeHtml(t) {
  if (typeof t !== 'string') t = String(t);
  return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#039;');
}
