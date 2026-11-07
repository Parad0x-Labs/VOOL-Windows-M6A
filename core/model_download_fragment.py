"""The Download buttons Settings and Setup show where a new user meets "no model" (job 52, D2).

One script, mounted by both pages through ``window.VoolModelDownload.mount(host, {onDone})``. It lists
what ``GET /api/models/local/pull`` offers (the machine-sized plan, exactly), names each model's size
and where it downloads from, and sends one ``POST /api/models/local/pull`` per click: the click is the
consent for that one pull (core/local_model_pull.py). Progress is polled from the same GET, with a
Cancel that posts ``/api/models/local/pull/cancel``. Strings resolve through the page's i18n bootstrap
(VOOLT / VOOLFMT) with the English catalog source as the fallback.
"""
from __future__ import annotations

_FRAGMENT = r"""<script>
(function () {
  function T(key, fallback, params) {
    let text = fallback;
    try { if (typeof VOOLT === 'function') { const t = VOOLT(key); if (t && t !== key) text = t; } } catch (e) {}
    try { if (typeof VOOLFMT === 'function') return VOOLFMT(text, params || {}); } catch (e) {}
    return String(text).replace(/\{(\w+)\}/g, (_, k) => (params && params[k] != null ? params[k] : ''));
  }
  function node(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
  async function call(url, body) {
    const init = body === undefined ? { headers: { 'Accept': 'application/json' } }
      : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    const r = await fetch(url, init);
    const j = await r.json().catch(() => ({}));
    return { ok: r.ok && j.ok !== false, status: r.status, json: j || {} };
  }
  function mount(host, opts) {
    const onDone = (opts && opts.onDone) || function () {};
    const box = node('div', 'model-download stack');
    box.id = 'modelDownload';
    host.appendChild(box);
    let timer = null;
    const say = (cls, text) => { const p = node('p', cls, text); p.setAttribute('role', 'status'); box.appendChild(p); return p; };
    async function draw() {
      box.textContent = '';
      const res = await call('/api/models/local/pull');
      const view = res.json || {};
      const pull = view.pull || null;
      if (pull && pull.status === 'pulling') { showProgress(pull); return; }
      if (pull && pull.status === 'ready') say('state ok', T('models.download.done', '{model} is downloaded and ready.', { model: pull.model }));
      if (pull && (pull.status === 'failed' || pull.status === 'cancelled')) say('state failed', T('models.download.failed', 'Download stopped: {reason}', { reason: pull.detail || pull.status }));
      (view.models || []).filter((m) => !m.installed).forEach((m) => {
        const b = node('button', 'btn primary', T('models.download.button', "Download {model} ({size} GB) from Ollama's registry", { model: m.model, size: m.size_gb }));
        b.type = 'button';
        b.addEventListener('click', async () => {
          b.disabled = true;
          const started = await call('/api/models/local/pull', { model: m.model });
          if (!started.ok) { b.disabled = false; say('state failed', T('models.download.failed', 'Download stopped: {reason}', { reason: started.json.message || started.json.error || ('HTTP ' + started.status) })); return; }
          if (started.json.status === 'ready') { onDone(m.model); draw(); return; }
          draw();
        });
        box.appendChild(b);
      });
    }
    function showProgress(pull) {
      box.textContent = '';
      say('subtle', T('models.download.progress', 'Downloading {model}… {percent}%', { model: pull.model, percent: pull.percent || 0 }));
      const c = node('button', 'btn', T('models.download.cancel', 'Cancel download'));
      c.type = 'button';
      c.addEventListener('click', async () => { c.disabled = true; await call('/api/models/local/pull/cancel', {}); });
      box.appendChild(c);
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const res = await call('/api/models/local/pull');
        const now = (res.json || {}).pull || null;
        if (now && now.status === 'pulling') { showProgress(now); return; }
        if (now && now.status === 'ready') onDone(now.model);
        draw();
      }, 1000);
    }
    draw();
  }
  window.VoolModelDownload = Object.freeze({ mount: mount });
})();
</script>"""


def render_model_download_fragment() -> str:
    return _FRAGMENT
