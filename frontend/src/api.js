const BASE = 'http://127.0.0.1:8001';

async function req(path, opts = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`${res.status}: ${body}`);
  }
  return res.json();
}

export const api = {
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
