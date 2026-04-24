// Same-origin in production (Heroku serves the SPA from the same FastAPI
// app). In dev with `vite dev` on :5173 → backend on :8001, set
// VITE_API_BASE in `.env.local`, e.g. VITE_API_BASE=http://127.0.0.1:8001
const BASE = import.meta.env.VITE_API_BASE || '';

async function req(path, opts = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    ...opts,
  });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    const err = new Error(`${res.status}: ${body}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

export const apiBase = BASE;

export const api = {
  health: () => req('/api/health'),
  login: (password) => req('/api/login', {
    method: 'POST',
    body: JSON.stringify({ password }),
  }),
  logout: () => req('/api/logout', { method: 'POST' }),
  listRuns: () => req('/api/runs'),
  createRun: (opening_text) => req('/api/runs', {
    method: 'POST',
    body: JSON.stringify({ opening_text: opening_text ?? null }),
  }),
  defaultOpening: () => req('/api/default_opening'),
  getRun: (id) => req(`/api/runs/${id}`),
  advanceDay: (id) => req(`/api/runs/${id}/advance_day`, { method: 'POST' }),
  announce: (id, text) =>
    req(`/api/runs/${id}/admin/announce`, {
      method: 'POST',
      body: JSON.stringify({ text }),
    }),
  sendDm: (id, recipient_id, text) =>
    req(`/api/runs/${id}/admin/dm`, {
      method: 'POST',
      body: JSON.stringify({ recipient_id, text }),
    }),
  fileMotion: (id, title, description) =>
    req(`/api/runs/${id}/motions`, {
      method: 'POST',
      body: JSON.stringify({ title, description }),
    }),
  closeMotion: (id, motion_id) =>
    req(`/api/runs/${id}/motions/${motion_id}/close`, { method: 'POST' }),
  setAgentGoal: (id, agent_id, goal) =>
    req(`/api/runs/${id}/agents/${agent_id}/goal`, {
      method: 'PUT',
      body: JSON.stringify({ goal }),
    }),
  getAgentSoul: (id, agent_id) =>
    req(`/api/runs/${id}/agents/${agent_id}/soul`),
  getAgentMemory: (id, agent_id) =>
    req(`/api/runs/${id}/agents/${agent_id}/memory`),
};
