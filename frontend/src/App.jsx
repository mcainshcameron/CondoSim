import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, apiBase } from './api.js';
import Login from './Login.jsx';

// Empty in production (single-origin deploy on Heroku); set to a non-empty
// VITE_API_BASE for `vite dev` against a remote backend. Same convention as
// api.js — see api.js for the env var.
const BACKEND = apiBase;

function formatItalianDateTime(fictionalStartIso, minutesSinceStart) {
  const base = new Date(fictionalStartIso);
  const dt = new Date(base.getTime() + minutesSinceStart * 60 * 1000);
  const giorni = ['Lunedì','Martedì','Mercoledì','Giovedì','Venerdì','Sabato','Domenica'];
  const mesi = ['gennaio','febbraio','marzo','aprile','maggio','giugno','luglio','agosto','settembre','ottobre','novembre','dicembre'];
  // JS getDay: 0=Sunday; we want Monday=0
  const day = (dt.getDay() + 6) % 7;
  const hh = String(dt.getHours()).padStart(2, '0');
  const mm = String(dt.getMinutes()).padStart(2, '0');
  return `${giorni[day]} ${dt.getDate()} ${mesi[dt.getMonth()]}, ${hh}:${mm}`;
}

// Deterministic pleasing color from any string (for avatar backgrounds).
function colorFor(id) {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) | 0;
  const hue = Math.abs(h) % 360;
  return `hsl(${hue}, 42%, 48%)`;
}

function initialsOf(name) {
  const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return '?';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

function Avatar({ id, name, size = 36, title }) {
  return (
    <div
      className="avatar"
      style={{
        width: size,
        height: size,
        background: colorFor(id || name || '?'),
        fontSize: Math.round(size * 0.4),
      }}
      title={title || name}
    >
      {initialsOf(name)}
    </div>
  );
}

function Setup({ onCreated }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [existing, setExisting] = useState([]);
  const [openingText, setOpeningText] = useState('');
  const [showRuns, setShowRuns] = useState(false);

  useEffect(() => {
    api.listRuns().then(r => setExisting(r.runs || [])).catch(() => {});
    api.defaultOpening().then(d => setOpeningText(d.text || '')).catch(() => {});
  }, []);

  const handleCreate = async () => {
    setLoading(true); setError('');
    try {
      const state = await api.createRun(openingText);
      onCreated(state);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  const handleLoad = async (id) => {
    setLoading(true); setError('');
    try {
      const state = await api.getRun(id);
      onCreated(state);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  // Atmospheric cast preview — hardcoded names match the v1 scenario (heating_crisis).
  const castPreview = [
    { id: 'conti',     name: 'Maria Conti',       sub: 'int. 2B · vedova, 40 anni qui' },
    { id: 'ferrari',   name: 'Marco Ferrari',     sub: 'int. 5A · consulente, spesso altrove' },
    { id: 'greco',     name: 'Valentina Greco',   sub: 'int. 7A · "consulente immobiliare"' },
    { id: 'marchetti', name: 'Davide Marchetti',  sub: 'int. 3B · cura la madre anziana' },
    { id: 'romano',    name: 'Giulia Romano',     sub: 'int. 4C · designer, appena comprato' },
  ];

  return (
    <div className="setup-page">
      <div className="setup-card">
        <div className="setup-hero">
          <div className="setup-hero-logo">🏢</div>
          <div className="setup-hero-text">
            <h1>Condominio Via Garibaldi</h1>
            <div className="setup-hero-sub">
              Sei il nuovo amministratore. Cinque residenti, una crisi,
              quattordici giorni per riuscire a convincerli.
            </div>
          </div>
          <div className="setup-hero-meta">
            <span>🇮🇹 Italiano</span>
            <span>·</span>
            <span>14 giorni</span>
            <span>·</span>
            <span>5 residenti</span>
          </div>
        </div>

        <div className="setup-body">
          <div className="setup-col">
            <div className="setup-section-title">Chi abita al palazzo</div>
            <div className="cast-preview">
              {castPreview.map(c => (
                <div key={c.id} className="cast-row">
                  <Avatar id={c.id} name={c.name} size={40} />
                  <div className="cast-row-text">
                    <div className="cast-name">{c.name}</div>
                    <div className="cast-sub">{c.sub}</div>
                  </div>
                </div>
              ))}
            </div>

            <div className="setup-section-title" style={{ marginTop: 18 }}>Come si gioca</div>
            <ul className="setup-rules">
              <li>Ogni residente ha una storia, interessi propri e qualche segreto.</li>
              <li>Parli con tutti nel gruppo principale, oppure in privato via DM.</li>
              <li>Deposita mozioni, chiudi votazioni, guida la crisi come credi.</li>
            </ul>
          </div>

          <div className="setup-col setup-col-compose">
            <div className="setup-section-title">Primo avviso al condominio</div>
            <div className="setup-compose-card">
              <div className="setup-compose-head">
                <Avatar id="admin" name="Amministratore" size={30} />
                <div>
                  <div className="setup-compose-head-title">Amministratore</div>
                  <div className="setup-compose-head-sub">→ Gruppo Condominio Via Garibaldi</div>
                </div>
              </div>
              <textarea
                className="setup-textarea"
                value={openingText}
                onChange={e => setOpeningText(e.target.value)}
                rows={12}
                placeholder="Il tuo messaggio di apertura al condominio…"
              />
              <div className="setup-compose-hint">
                Scrivi la crisi che preferisci: guasto caldaia, infiltrazioni, un nuovo
                preventivo che non quadra. La simulazione si adatta a quello che invii.
              </div>
            </div>

            <button
              className="setup-start"
              onClick={handleCreate}
              disabled={loading || !openingText.trim()}
            >
              {loading ? 'Un momento…' : 'Avvia partita ▶'}
            </button>

            {existing.length > 0 && (
              <div className="setup-runs">
                <button className="setup-runs-toggle" onClick={() => setShowRuns(v => !v)}>
                  {showRuns ? '▾' : '▸'} Riprendi una partita ({existing.length})
                </button>
                {showRuns && (
                  <div className="setup-runs-list">
                    {existing.map(id => (
                      <button
                        key={id}
                        className="setup-run-card"
                        onClick={() => handleLoad(id)}
                        disabled={loading}
                      >
                        <span className="setup-run-icon">📂</span>
                        <span className="setup-run-id">{id}</span>
                        <span className="setup-run-arrow">→</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}

            {error && <div className="setup-error">{error}</div>}
          </div>
        </div>
      </div>
    </div>
  );
}

function findChatForResident(state, residentId, godView) {
  const adminDm = state.chats.find(c => c.kind === 'dm'
    && c.member_ids.includes('admin') && c.member_ids.includes(residentId));
  if (adminDm) return adminDm.id;
  if (godView) {
    const dms = state.chats.filter(c => c.kind === 'dm' && c.member_ids.includes(residentId));
    if (dms.length) {
      const counts = dms.map(c => ({ c, n: state.messages.filter(m => m.chat_id === c.id).length }));
      counts.sort((a, b) => b.n - a.n);
      return counts[0].c.id;
    }
  }
  return null;
}

function findChatForPair(state, aId, bId) {
  const c = state.chats.find(ch => ch.kind === 'dm'
    && ch.member_ids.includes(aId) && ch.member_ids.includes(bId));
  return c?.id || null;
}

const OWNER_KIND_LABEL = {
  self: 'Proprietario che abita qui',
  absentee_landlord: 'Procuratore di un proprietario assente',
  family_proxy: 'Delegato di un familiare',
  commercial_stake: 'Rappresenta un interesse commerciale',
};

function ProfileModal({ state, agentId, onClose, onOpenChat, godView, onGoalSaved }) {
  const agent = state.agents.find(a => a.persona.id === agentId);
  const [goalDraft, setGoalDraft] = useState(agent?.admin_goal || '');
  const [goalSaving, setGoalSaving] = useState(false);
  const [goalStatus, setGoalStatus] = useState('');
  const [soulText, setSoulText] = useState(null);
  const [memoryText, setMemoryText] = useState(null);
  const [showSoul, setShowSoul] = useState(false);
  const [showMemory, setShowMemory] = useState(false);

  useEffect(() => {
    setGoalDraft(agent?.admin_goal || '');
    // Reset on agent switch; fetched lazily when user expands.
    setSoulText(null);
    setMemoryText(null);
    setShowSoul(false);
    setShowMemory(false);
  }, [agent?.admin_goal, agentId]);

  const toggleSoul = async () => {
    const next = !showSoul;
    setShowSoul(next);
    if (next && soulText === null) {
      try {
        const { content } = await api.getAgentSoul(state.run_id, agentId);
        setSoulText(content || '(vuoto)');
      } catch (e) {
        setSoulText('Errore nel caricamento: ' + String(e));
      }
    }
  };

  const toggleMemory = async () => {
    const next = !showMemory;
    setShowMemory(next);
    if (next && memoryText === null) {
      try {
        const { content } = await api.getAgentMemory(state.run_id, agentId);
        setMemoryText(content || '(vuoto)');
      } catch (e) {
        setMemoryText('Errore nel caricamento: ' + String(e));
      }
    }
  };

  const refreshMemory = async () => {
    try {
      const { content } = await api.getAgentMemory(state.run_id, agentId);
      setMemoryText(content || '(vuoto)');
    } catch (e) {
      setMemoryText('Errore nel caricamento: ' + String(e));
    }
  };

  if (!agent) return null;
  const p = agent.persona;
  const goalDirty = goalDraft !== (agent.admin_goal || '');

  const saveGoal = async () => {
    setGoalSaving(true);
    setGoalStatus('');
    try {
      await api.setAgentGoal(state.run_id, agentId, goalDraft);
      setGoalStatus('Obiettivo salvato — entrerà nel prossimo turno del residente.');
      if (onGoalSaved) onGoalSaved(agentId, goalDraft);
    } catch (e) {
      setGoalStatus('Errore: ' + String(e));
    } finally {
      setGoalSaving(false);
    }
  };

  const clearGoal = async () => {
    setGoalDraft('');
    setGoalSaving(true);
    setGoalStatus('');
    try {
      await api.setAgentGoal(state.run_id, agentId, '');
      setGoalStatus('Obiettivo rimosso.');
      if (onGoalSaved) onGoalSaved(agentId, '');
    } catch (e) {
      setGoalStatus('Errore: ' + String(e));
    } finally {
      setGoalSaving(false);
    }
  };

  const allMessages = state.messages.filter(m => m.sender_id === agentId);
  const todayMsgs = allMessages.filter(m => m.day === state.clock.day);

  // Chats the agent is in
  const theirChats = state.chats.filter(c => c.member_ids.includes(agentId));
  const chatSummary = theirChats.map(c => {
    const all = state.messages.filter(m => m.chat_id === c.id);
    const byThem = all.filter(m => m.sender_id === agentId).length;
    const others = c.member_ids.filter(id => id !== agentId).map(id => {
      if (id === 'admin') return 'Amministratore';
      const a = state.agents.find(x => x.persona.id === id);
      return a?.persona.display_name || id;
    }).join(', ');
    return { chat: c, total: all.length, byThem, others };
  }).sort((a, b) => b.total - a.total);

  // Trust sent/received summary
  const trustOut = state.trust?.[agentId] || {};
  const trustIn = {};
  for (const [other, row] of Object.entries(state.trust || {})) {
    if (row[agentId] !== undefined) trustIn[other] = row[agentId];
  }
  const nameOf = id => state.agents.find(a => a.persona.id === id)?.persona.display_name || id;

  // Vote history
  const voteHistory = (state.motions || [])
    .filter(m => m.votes && m.votes[agentId])
    .map(m => ({ motion: m, vote: m.votes[agentId] }));

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <div>
            <h2>{p.display_name}</h2>
            <div className="modal-sub">Interno {p.unit} · {p.millesimi}/1000 millesimi · Portafoglio ~€{agent.starting_wallet_eur.toLocaleString('it-IT')}</div>
          </div>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>

        <div className="modal-section">
          <h3>Profilo pubblico</h3>
          <p>{p.public_description}</p>
          <div className="modal-meta">
            <span>⏱️ {p.responsiveness} · {p.time_of_day}</span>
            <span>📮 {allMessages.length} msg totali · {todayMsgs.length} oggi</span>
          </div>
        </div>

        <div className="modal-section goal-section">
          <h3>🎯 Obiettivo aggiuntivo <span className="goal-hint">(entra nella testa di {p.display_name.split(' ').slice(-1)} al prossimo turno)</span></h3>
          <textarea
            className="goal-textarea"
            placeholder="Es: 'Ti è venuta voglia di vendere l'appartamento e lasciare il palazzo il prima possibile'; oppure 'Hai appena scoperto che stai aspettando un bambino, tutto ti sembra più urgente'. Scritto in seconda persona, come se fosse un pensiero tuo."
            value={goalDraft}
            onChange={e => setGoalDraft(e.target.value)}
            rows={3}
          />
          <div className="goal-actions">
            <button
              className="btn"
              onClick={saveGoal}
              disabled={!goalDirty || goalSaving}
              style={{ width: 'auto', padding: '6px 14px' }}
            >
              {goalSaving ? 'Salvo…' : goalDirty ? 'Salva obiettivo' : 'Salvato'}
            </button>
            {agent.admin_goal && (
              <button
                className="btn secondary"
                onClick={clearGoal}
                disabled={goalSaving}
                style={{ width: 'auto', padding: '6px 14px', fontSize: 12 }}
              >Rimuovi</button>
            )}
            {goalStatus && <span className="goal-status">{goalStatus}</span>}
          </div>
        </div>

        <div className="modal-section">
          <h3>Chat a cui partecipa ({theirChats.length})</h3>
          <div className="profile-chats">
            {chatSummary.map(({ chat, total, byThem, others }) => {
              const canOpen = godView || chat.member_ids.includes('admin') || chat.kind === 'main';
              return (
                <div
                  key={chat.id}
                  className={`profile-chat-row ${canOpen ? 'clickable' : ''}`}
                  onClick={() => canOpen && onOpenChat(chat.id)}
                >
                  <div>
                    <strong>{chat.display_name}</strong>
                    <span className="profile-chat-with"> con {others || '—'}</span>
                  </div>
                  <div className="profile-chat-meta">
                    {byThem}/{total} msg{canOpen && <span style={{color:'var(--brand)', marginLeft: 6}}>→</span>}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {voteHistory.length > 0 && (
          <div className="modal-section">
            <h3>Voti ({voteHistory.length})</h3>
            {voteHistory.map(({ motion, vote }) => (
              <div key={motion.id} className="profile-vote">
                <span>{vote === 'yes' ? '✅' : vote === 'no' ? '❌' : '⚪'}</span>
                <span>{motion.title}</span>
                <span className={`motion-status-tag ${motion.status}`}>{motion.status}</span>
              </div>
            ))}
          </div>
        )}

        <div className="modal-section">
          <h3>Relazioni (fiducia)</h3>
          <div className="profile-trust-grid">
            {state.agents.filter(a => a.persona.id !== agentId).map(other => {
              const out = trustOut[other.persona.id] ?? 0;
              const inc = trustIn[other.persona.id] ?? 0;
              return (
                <div key={other.persona.id} className="profile-trust-row">
                  <span>{other.persona.display_name.split(' ').slice(-1)}</span>
                  <span style={{ color: out >= 0 ? '#2e7d32' : '#c23c3c' }}>
                    → {out >= 0 ? '+' : ''}{out.toFixed(2)}
                  </span>
                  <span style={{ color: inc >= 0 ? '#2e7d32' : '#c23c3c' }}>
                    ← {inc >= 0 ? '+' : ''}{inc.toFixed(2)}
                  </span>
                </div>
              );
            })}
          </div>
        </div>

        {godView && (
          <div className="modal-section observer">
            <h3>🔒 Modalità osservatore</h3>
            <div className="observer-field">
              <div className="observer-label">Tipo di proprietario</div>
              <div>{OWNER_KIND_LABEL[agent.owner.kind] || agent.owner.kind}</div>
            </div>
            <div className="observer-field">
              <div className="observer-label">
                <button type="button" className="observer-toggle" onClick={toggleSoul}>
                  {showSoul ? '▾' : '▸'} SOUL.md (identità, immutabile)
                </button>
              </div>
              {showSoul && (
                <pre className="observer-markdown">{soulText ?? 'Caricamento…'}</pre>
              )}
            </div>
            <div className="observer-field">
              <div className="observer-label">
                <button type="button" className="observer-toggle" onClick={toggleMemory}>
                  {showMemory ? '▾' : '▸'} MEMORY.md (taccuino, cresce giorno per giorno)
                </button>
                {showMemory && (
                  <button type="button" className="observer-toggle-small" onClick={refreshMemory} title="Ricarica">
                    ↻
                  </button>
                )}
              </div>
              {showMemory && (
                <pre className="observer-markdown">{memoryText ?? 'Caricamento…'}</pre>
              )}
            </div>
            {agent.notes?.length > 0 && (
              <div className="observer-field">
                <div className="observer-label">Appunti privati ({agent.notes.length})</div>
                <ol className="observer-notes">
                  {agent.notes.map((n, i) => <li key={i}>{n}</li>)}
                </ol>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function truncate(s, n) {
  if (!s) return '';
  return s.length > n ? s.slice(0, n - 1).trimEnd() + '…' : s;
}

function LeftPanel({ state, selectedChatId, onSelectChat, onStartDm, unreadByChat, typingByChat, godView, onOpenProfile }) {
  // Compute DM partner counts + trust edges from current state
  const nameById = useMemo(() => {
    const m = {};
    for (const a of state.agents) m[a.persona.id] = a.persona.display_name;
    m['admin'] = 'Amministratore';
    return m;
  }, [state.agents]);

  // Chat list shows only admin-participating conversations (group + admin DMs).
  // Inter-resident DMs live in the "DM frequenti" section below and open in
  // the center column when clicked — godView doesn't widen this list.
  const visibleChats = useMemo(() =>
    state.chats.filter(c =>
      c.kind === 'main' || c.kind === 'assembly' || c.member_ids.includes('admin')
    ),
  [state.chats]);

  const lastMsgByChat = useMemo(() => {
    const m = new Map();
    for (const msg of state.messages) {
      const prev = m.get(msg.chat_id);
      if (!prev || msg.fictional_timestamp_minutes > prev.fictional_timestamp_minutes) {
        m.set(msg.chat_id, msg);
      }
    }
    return m;
  }, [state.messages]);

  const chatListItems = useMemo(() => {
    return visibleChats.map(c => {
      const last = lastMsgByChat.get(c.id) || null;
      return { chat: c, last };
    }).sort((a, b) => {
      // Main/assembly stay pinned near the top
      const pinA = (a.chat.kind === 'main' || a.chat.kind === 'assembly') ? 1 : 0;
      const pinB = (b.chat.kind === 'main' || b.chat.kind === 'assembly') ? 1 : 0;
      if (pinA !== pinB) return pinB - pinA;
      const at = a.last?.fictional_timestamp_minutes ?? -1;
      const bt = b.last?.fictional_timestamp_minutes ?? -1;
      return bt - at;
    });
  }, [visibleChats, lastMsgByChat]);

  // DM pair counts (bidirectional)
  const dmCounts = useMemo(() => {
    const pairs = {};
    for (const c of state.chats) {
      if (c.kind !== 'dm') continue;
      const [a, b] = [...c.member_ids].sort();
      if (!a || !b) continue;
      const key = `${a}|${b}`;
      const n = state.messages.filter(m => m.chat_id === c.id).length;
      if (n > 0) pairs[key] = (pairs[key] || 0) + n;
    }
    return Object.entries(pairs)
      .map(([k, n]) => {
        const [a, b] = k.split('|');
        return { a, b, n };
      })
      .sort((x, y) => y.n - x.n)
      .slice(0, 6);
  }, [state]);

  // Top trust edges (mutual average, positive first then negative)
  const trustEdges = useMemo(() => {
    const trust = state.trust || {};
    const seen = new Set();
    const edges = [];
    for (const a of state.agents) {
      for (const b of state.agents) {
        if (a.persona.id === b.persona.id) continue;
        const key = [a.persona.id, b.persona.id].sort().join('|');
        if (seen.has(key)) continue;
        seen.add(key);
        const t1 = trust[a.persona.id]?.[b.persona.id] ?? 0;
        const t2 = trust[b.persona.id]?.[a.persona.id] ?? 0;
        const avg = (t1 + t2) / 2;
        if (Math.abs(avg) >= 0.05) edges.push({ a: a.persona.id, b: b.persona.id, value: avg });
      }
    }
    edges.sort((x, y) => Math.abs(y.value) - Math.abs(x.value));
    return edges.slice(0, 8);
  }, [state.trust, state.agents]);

  // Vote-alignment tally across closed motions
  const alignedPairs = useMemo(() => {
    const pairs = {};
    for (const m of (state.motions || [])) {
      if (m.status === 'open') continue;
      const voters = Object.entries(m.votes || {}).filter(([_, v]) => v !== 'abstain');
      for (let i = 0; i < voters.length; i++) {
        for (let j = i + 1; j < voters.length; j++) {
          const [a, va] = voters[i]; const [b, vb] = voters[j];
          const k = [a, b].sort().join('|');
          if (!pairs[k]) pairs[k] = { aligned: 0, opposed: 0 };
          if (va === vb) pairs[k].aligned += 1; else pairs[k].opposed += 1;
        }
      }
    }
    return pairs;
  }, [state.motions]);

  return (
    <div className="panel">
      <div className="panel-title">Chat</div>
      <div className="chat-list">
        {chatListItems.length === 0 && (
          <div className="chat-list-empty">Nessuna chat ancora.</div>
        )}
        {chatListItems.map(({ chat, last }) => {
          const unread = unreadByChat?.[chat.id] || 0;
          const typing = (typingByChat?.[chat.id] || []).length > 0;
          const isActive = chat.id === selectedChatId;
          const isGroup = chat.kind === 'main' || chat.kind === 'assembly';
          const lastSender = last?.sender_kind === 'admin'
            ? 'Tu'
            : (last?.sender_display_name || '').split(' ').slice(-1)[0];
          const previewText = last
            ? (isGroup || last.sender_kind === 'admin'
                ? `${lastSender}: ${last.content}`
                : last.content)
            : (isGroup ? 'Gruppo condominiale' : 'Nessun messaggio');
          const timeLabel = last
            ? (last.day === state.clock.day
                ? (formatItalianDateTime(state.fictional_start_iso, last.fictional_timestamp_minutes).split(', ')[1] || '')
                : `g. ${last.day}`)
            : '';
          return (
            <div
              key={chat.id}
              className={`chat-list-row ${isActive ? 'active' : ''} ${unread > 0 ? 'has-unread' : ''}`}
              onClick={() => onSelectChat(chat.id)}
            >
              <Avatar id={chat.id} name={chat.display_name} size={42} />
              <div className="chat-list-main">
                <div className="chat-list-top">
                  <span className="chat-list-name">{chat.display_name}</span>
                  <span className="chat-list-time">{timeLabel}</span>
                </div>
                <div className="chat-list-bot">
                  <span className="chat-list-preview">
                    {typing
                      ? <span className="chat-list-typing">sta scrivendo…</span>
                      : truncate(previewText, 48)}
                  </span>
                  {unread > 0 && <span className="chat-list-unread">{unread}</span>}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      <div className="panel-title">Residenti</div>
      {state.agents.map(a => {
        const msgsToday = state.messages.filter(m => m.sender_id === a.persona.id && m.day === state.clock.day).length;
        const chatId = findChatForResident(state, a.persona.id, godView);
        return (
          <div
            className="resident-card clickable"
            key={a.persona.id}
            onClick={() => onOpenProfile(a.persona.id)}
            title="Apri profilo"
          >
            <Avatar id={a.persona.id} name={a.persona.display_name} size={42} />
            <div className="resident-main">
              <div className="resident-row">
                <div className="resident-name-line">
                  <span className="name">{a.persona.display_name}</span>
                  {a.admin_goal && <span className="goal-badge" title="Ha un obiettivo aggiuntivo">🎯</span>}
                </div>
                <button
                  className="resident-chat-btn"
                  onClick={(e) => {
                    e.stopPropagation();
                    if (chatId) onSelectChat(chatId);
                    else onStartDm(a.persona.id);
                  }}
                  title={chatId ? 'Apri chat' : 'Inizia DM'}
                >💬</button>
              </div>
              <div className="resident-unit-line">int. {a.persona.unit} · {a.persona.millesimi} mill.</div>
              <div className="desc">{a.persona.public_description}</div>
              {msgsToday > 0 && (
                <div className="resident-today">{msgsToday} msg oggi</div>
              )}
            </div>
          </div>
        );
      })}

      <div className="panel-title">Alleanze</div>
      {trustEdges.length === 0 && dmCounts.length === 0 ? (
        <div style={{ padding: '8px 14px', fontSize: 12, color: 'var(--muted)', fontStyle: 'italic' }}>
          Ancora nessuna dinamica visibile.
        </div>
      ) : (
        <>
          {trustEdges.length > 0 && (
            <div className="alliance-section">
              <div className="alliance-subtitle">Fiducia</div>
              {trustEdges.map((e, i) => (
                <div key={i} className="alliance-row">
                  <span className="alliance-pair">
                    {nameById[e.a].split(' ').slice(-1)} ↔ {nameById[e.b].split(' ').slice(-1)}
                  </span>
                  <span
                    className="alliance-value"
                    style={{ color: e.value >= 0 ? '#2e7d32' : '#c23c3c' }}
                  >
                    {e.value >= 0 ? '+' : ''}{e.value.toFixed(2)}
                  </span>
                </div>
              ))}
            </div>
          )}
          {dmCounts.length > 0 && (
            <div className="alliance-section">
              <div className="alliance-subtitle">DM frequenti</div>
              {dmCounts.map((p, i) => {
                const key = [p.a, p.b].sort().join('|');
                const v = alignedPairs[key];
                const chatId = findChatForPair(state, p.a, p.b);
                const canOpen = chatId && (godView || state.chats.find(c => c.id === chatId)?.member_ids.includes('admin'));
                return (
                  <div
                    key={i}
                    className={`alliance-row ${canOpen ? 'clickable' : ''}`}
                    onClick={() => canOpen && onSelectChat(chatId)}
                    title={canOpen ? 'Apri la chat' : 'Chat privata — visibile solo se trapela o con modalità osservatore'}
                  >
                    <span className="alliance-pair">
                      {(nameById[p.a] || p.a).split(' ').slice(-1)} ↔ {(nameById[p.b] || p.b).split(' ').slice(-1)}
                      {canOpen && <span style={{ color: 'var(--brand)', marginLeft: 4 }}>→</span>}
                    </span>
                    <span className="alliance-value" style={{ color: 'var(--muted)' }}>
                      {p.n} msg
                      {v && ` · ${v.aligned}✓ ${v.opposed}✗`}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function visibleChatsForAdmin(state, godView) {
  if (godView) return state.chats;
  // Fiction-respecting: admin sees main + any chat they're a member of.
  return state.chats.filter(c =>
    c.kind === 'main' || c.kind === 'assembly' || c.member_ids.includes('admin')
  );
}

function ChatComposer({ state, chat, suggestions, onSendAnnounce, onSendDm }) {
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');

  // Clear composer state whenever the active chat changes.
  useEffect(() => {
    setText('');
    setError('');
  }, [chat?.id]);

  if (!chat) return null;

  const isGroup = chat.kind === 'main' || chat.kind === 'assembly';
  const isAdminDm = chat.kind === 'dm' && chat.member_ids.includes('admin');
  const otherId = isAdminDm ? chat.member_ids.find(id => id !== 'admin') : null;
  const otherAgent = otherId ? state.agents.find(a => a.persona.id === otherId) : null;

  let mode = 'disabled';
  let placeholder = 'Non puoi scrivere in questa chat (visibile solo in modalità osservatore).';
  let label = '';
  if (isGroup) {
    mode = 'announce';
    placeholder = 'Scrivi un avviso al condominio…';
    label = 'Avviso al condominio';
  } else if (isAdminDm) {
    mode = 'dm';
    placeholder = `Scrivi a ${otherAgent?.persona.display_name || 'residente'}…`;
    label = `DM · ${otherAgent?.persona.display_name || ''}`;
  }

  const canSend = mode !== 'disabled' && text.trim() && !sending;

  const handleSend = async () => {
    if (!canSend) return;
    setSending(true);
    setError('');
    try {
      if (mode === 'announce') await onSendAnnounce(text.trim());
      else if (mode === 'dm') await onSendDm(otherId, text.trim());
      setText('');
    } catch (e) {
      setError('Errore: ' + String(e));
    } finally {
      setSending(false);
    }
  };

  const onKeyDown = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      handleSend();
    }
  };

  const speedChips = (mode === 'announce')
    ? (suggestions || []).filter(s => !s.action)
    : [];

  return (
    <div className={`chat-composer ${mode === 'disabled' ? 'disabled' : ''}`}>
      {label && (
        <div className="chat-composer-label">
          <span>{label}</span>
          <span className="chat-composer-hint">⌘↵ per inviare</span>
        </div>
      )}
      {speedChips.length > 0 && (
        <div className="chat-composer-chips">
          {speedChips.map(s => (
            <button
              key={s.id}
              type="button"
              className="template-chip"
              title={s.body || s.label}
              onClick={() => setText(s.body || '')}
            >{s.label}</button>
          ))}
        </div>
      )}
      <div className="chat-composer-row">
        <textarea
          className="chat-composer-input"
          placeholder={placeholder}
          value={text}
          onChange={e => setText(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={mode === 'disabled' || sending}
          rows={2}
        />
        <button
          className="chat-composer-send"
          onClick={handleSend}
          disabled={!canSend}
          title={mode === 'announce' ? 'Pubblica avviso' : mode === 'dm' ? 'Invia DM' : 'Non disponibile'}
        >
          {sending ? '…' : '➤'}
        </button>
      </div>
      {error && <div className="chat-composer-error">{error}</div>}
    </div>
  );
}

function ChatColumn({ state, selectedChatId, pendingChat, typingByChat, godView, suggestions, onSendAnnounce, onSendDm, onOpenProfile }) {
  const chats = visibleChatsForAdmin(state, godView);
  const msgsByChat = useMemo(() => {
    const m = new Map();
    for (const c of chats) m.set(c.id, []);
    for (const msg of state.messages) {
      if (m.has(msg.chat_id)) m.get(msg.chat_id).push(msg);
    }
    for (const arr of m.values()) arr.sort((a, b) => a.fictional_timestamp_minutes - b.fictional_timestamp_minutes);
    return m;
  }, [state, chats]);

  // Priority: explicit selection → pending placeholder → first available chat.
  const selected = chats.find(c => c.id === selectedChatId) || pendingChat || chats[0];
  const selectedMsgs = (selected && !selected._pending) ? (msgsByChat.get(selected.id) || []) : [];
  const scrollRef = useRef(null);

  const typingNames = selected ? (typingByChat[selected.id] || []) : [];

  // Auto-scroll to bottom on new messages / chat switch. The typing indicator
  // lives in the header now, so it no longer perturbs message layout.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [selectedMsgs.length, selected?.id]);

  // Build a WhatsApp-style chat header line for the current chat.
  const chatHeader = useMemo(() => {
    if (!selected) return null;
    const otherNames = (selected.member_ids || [])
      .filter(id => id !== 'admin')
      .map(id => {
        const a = state.agents.find(x => x.persona.id === id);
        return a?.persona.display_name || id;
      });
    if (selected.kind === 'main' || selected.kind === 'assembly') {
      return { title: selected.display_name, sub: `${otherNames.length} residenti · gruppo condominiale` };
    }
    if (selected.kind === 'dm') {
      return { title: selected.display_name, sub: 'Messaggio privato' };
    }
    return { title: selected.display_name, sub: '' };
  }, [selected, state.agents]);

  // Build render items with date separators between days.
  const renderedItems = useMemo(() => {
    const out = [];
    let lastDay = null;
    for (let i = 0; i < selectedMsgs.length; i++) {
      const m = selectedMsgs[i];
      if (m.day !== lastDay) {
        out.push({ type: 'day', day: m.day, ts: m.fictional_timestamp_minutes, key: `day-${m.day}-${i}` });
        lastDay = m.day;
      }
      out.push({ type: 'msg', msg: m, prev: selectedMsgs[i - 1], key: m.id });
    }
    return out;
  }, [selectedMsgs]);

  // DM chats: clicking the header opens the other resident's profile.
  const dmOtherId = selected?.kind === 'dm'
    ? selected.member_ids.find(id => id !== 'admin' && state.agents.some(a => a.persona.id === id))
    : null;
  const headerClickable = !!dmOtherId && !!onOpenProfile;

  return (
    <div className="chat-col">
      {chatHeader && (
        <div
          className={`chat-header ${headerClickable ? 'clickable' : ''}`}
          onClick={() => headerClickable && onOpenProfile(dmOtherId)}
          title={headerClickable ? 'Apri profilo' : undefined}
        >
          <Avatar id={selected.id} name={chatHeader.title} size={36} />
          <div className="chat-header-text">
            <div className="chat-header-title">{chatHeader.title}</div>
            <div className="chat-header-sub">
              {typingNames.length > 0 ? (
                <span className="chat-header-typing">
                  {typingNames.join(', ')} {typingNames.length === 1 ? 'sta scrivendo' : 'stanno scrivendo'}
                  <span className="dots"><span>.</span><span>.</span><span>.</span></span>
                </span>
              ) : chatHeader.sub}
            </div>
          </div>
        </div>
      )}
      <div className="chat-messages" ref={scrollRef}>
        {renderedItems.length === 0 ? (
          <div className="chat-empty">
            <div className="chat-empty-icon">💬</div>
            <div>Nessun messaggio ancora.</div>
            <div className="chat-empty-sub">Scrivi qui sotto o fai passare il giorno.</div>
          </div>
        ) : (
          renderedItems.map(item => {
            if (item.type === 'day') {
              const label = formatItalianDateTime(state.fictional_start_iso, item.ts)
                .split(', ')[0] || '';
              return (
                <div key={item.key} className="msg-day-separator">
                  <span>Giorno {item.day} · {label}</span>
                </div>
              );
            }
            const m = item.msg;
            const prev = item.prev;
            const isSelf = m.sender_kind === 'admin';
            const kindClass = isSelf
              ? 'self'
              : m.sender_kind === 'external'
                ? 'external'
                : 'other';
            const sameAsPrev = prev
              && prev.day === m.day
              && prev.sender_id === m.sender_id
              && prev.sender_kind === m.sender_kind
              && (m.fictional_timestamp_minutes - prev.fictional_timestamp_minutes) < 10;
            const ts = formatItalianDateTime(state.fictional_start_iso, m.fictional_timestamp_minutes)
              .split(', ')[1] || '';
            return (
              <div
                key={m.id}
                className={`msg-row ${kindClass} ${sameAsPrev ? 'grouped' : ''} ${m.isNew ? 'fade-in' : ''}`}
              >
                {!isSelf && (
                  <div className="msg-avatar-slot">
                    {!sameAsPrev && (
                      <Avatar
                        id={m.sender_id}
                        name={m.sender_display_name}
                        size={30}
                      />
                    )}
                  </div>
                )}
                <div className={`msg ${kindClass}`}>
                  {!sameAsPrev && !isSelf && (
                    <div className="sender">{m.sender_display_name}</div>
                  )}
                  <div className="content">{m.content}</div>
                  <div className="ts">{ts}</div>
                </div>
              </div>
            );
          })
        )}
      </div>
      <ChatComposer
        state={state}
        chat={selected}
        suggestions={suggestions}
        onSendAnnounce={onSendAnnounce}
        onSendDm={onSendDm}
      />
    </div>
  );
}

function AdminConsole({ state, onFileMotion, onCloseMotion, suggestions }) {
  const [motionTitle, setMotionTitle] = useState('');
  const [motionDesc, setMotionDesc] = useState('');
  const [status, setStatus] = useState('');
  const [templates, setTemplates] = useState({ actions: [], motion_templates: [] });

  useEffect(() => {
    fetch(`${BACKEND}/api/quick_actions`).then(r => r.json()).then(setTemplates).catch(() => {});
  }, []);

  const fillTemplate = (tpl) => {
    if (tpl.title) setMotionTitle(tpl.title);
    if (tpl.description) setMotionDesc(tpl.description);
  };

  const doAction = useCallback(async (fn, successMsg) => {
    try {
      setStatus('In corso…');
      await fn();
      setStatus(successMsg);
    } catch (e) {
      setStatus('Errore: ' + String(e));
    }
  }, []);

  const handleFileMotion = async () => {
    if (!motionTitle.trim() || !motionDesc.trim()) return;
    await doAction(async () => {
      await onFileMotion(motionTitle, motionDesc);
      setMotionTitle('');
      setMotionDesc('');
    }, 'Mozione depositata.');
  };

  const handleCloseFromChip = async (sug) => {
    if (sug.action !== 'close_motion' || !sug.motion_id) return;
    await doAction(async () => {
      await onCloseMotion(sug.motion_id);
    }, 'Votazione chiusa.');
  };

  const openMotions = (state.motions || []).filter(m => m.status === 'open');
  const closedMotions = (state.motions || []).filter(m => m.status !== 'open').slice(-5);

  const actionChips = (suggestions || []).filter(s => s.action === 'close_motion');

  return (
    <div className="console">
      {/* Motions */}
      <div className="console-section">
        <h2>Mozioni {openMotions.length > 0 && <span className="badge">{openMotions.length} aperte</span>}</h2>
        {openMotions.length === 0 && closedMotions.length === 0 && (
          <div style={{ fontSize: 12, color: 'var(--muted)', fontStyle: 'italic' }}>
            Nessuna mozione ancora depositata.
          </div>
        )}
        {openMotions.map(m => {
          const yes = Object.values(m.votes || {}).filter(v => v === 'yes').length;
          const no = Object.values(m.votes || {}).filter(v => v === 'no').length;
          const abs = Object.values(m.votes || {}).filter(v => v === 'abstain').length;
          return (
            <div key={m.id} className="motion-card">
              <div className="motion-title">{m.title}</div>
              <div className="motion-proposer">— {m.proposer_display_name}, g. {m.day_proposed}</div>
              <div className="motion-desc">{m.description}</div>
              <div className="motion-votes">✅ {yes} · ❌ {no} · ⚪ {abs}</div>
              <button className="btn danger" onClick={() => onCloseMotion(m.id)} style={{ fontSize: 11, padding: '4px 8px', marginTop: 6 }}>
                Chiudi votazione
              </button>
            </div>
          );
        })}
        {closedMotions.map(m => (
          <div key={m.id} className={`motion-card closed ${m.status}`}>
            <div className="motion-title">
              {m.status === 'passed' ? '✅' : '❌'} {m.title}
            </div>
            <div className="motion-desc" style={{ fontSize: 11 }}>{m.outcome_note}</div>
          </div>
        ))}

        <details className="motion-file-details">
          <summary>+ Deposita una mozione</summary>
          {templates.motion_templates?.length > 0 && (
            <div className="template-row">
              {templates.motion_templates.map(t => (
                <button
                  key={t.id}
                  className="template-chip"
                  title="Pre-compila gli effetti"
                  onClick={() => fillTemplate(t)}
                >{t.label}</button>
              ))}
            </div>
          )}
          <input
            placeholder="Titolo…"
            value={motionTitle}
            onChange={e => setMotionTitle(e.target.value)}
          />
          <textarea
            placeholder="Descrizione della mozione…"
            value={motionDesc}
            onChange={e => setMotionDesc(e.target.value)}
            style={{ minHeight: 44 }}
          />
          <button className="btn" onClick={handleFileMotion} disabled={!motionTitle.trim() || !motionDesc.trim()}>
            Deposita mozione
          </button>
        </details>
      </div>

      {actionChips.length > 0 && (
        <div className="console-section">
          <div className="quick-actions">
            {actionChips.map(s => (
              <button
                key={s.id}
                className="btn secondary quick-action-btn"
                onClick={() => handleCloseFromChip(s)}
                title={s.label}
              >{s.label}</button>
            ))}
          </div>
        </div>
      )}

      {status && (
        <div className="console-section" style={{ paddingTop: 6, borderBottom: 'none' }}>
          <div className="status">{status}</div>
        </div>
      )}
    </div>
  );
}

function HelpModal({ onClose }) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal help-modal" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <div>
            <h2>Come si gioca</h2>
            <div className="modal-sub">Sei l'amministratore. I residenti reagiscono ai tuoi messaggi in tempo reale.</div>
          </div>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>

        <div className="modal-section help-section">
          <h3>I tre pannelli</h3>
          <ul className="help-list">
            <li><strong>Sinistra</strong> — Chat, residenti e alleanze. Clicca una chat per aprirla, o un residente per vederne il profilo.</li>
            <li><strong>Centro</strong> — La conversazione attiva. Scrivi in basso per inviare un avviso al gruppo o un DM privato.</li>
            <li><strong>Destra</strong> — Console amministratore: mozioni e azioni rapide.</li>
          </ul>
        </div>

        <div className="modal-section help-section">
          <h3>Il ciclo di un giorno</h3>
          <ol className="help-list">
            <li>Scrivi un avviso nel gruppo, oppure un DM privato a un residente.</li>
            <li>I residenti iniziano a rispondere in tempo finzionale (li vedrai “stanno scrivendo…”).</li>
            <li>I giorni scorrono da soli: quando la giornata finisce, il prossimo comincia dopo una breve pausa.</li>
            <li>A fine giornata ogni residente aggiorna il proprio taccuino privato — ricorderà selettivamente quel che è successo.</li>
          </ol>
        </div>

        <div className="modal-section help-section">
          <h3>Mozioni e voti</h3>
          <p>Deposita una mozione dalla console a destra per sottoporre una decisione al condominio. I residenti votano nel tempo secondo i loro interessi; chiudi la votazione quando vuoi vederne l'esito.</p>
        </div>

        <div className="modal-section help-section">
          <h3>Obiettivi segreti 🎯</h3>
          <p>Clicca un residente per aprirne il profilo e iniettare un <em>obiettivo aggiuntivo</em> — un pensiero che gli entrerà in testa al prossimo turno (es. “stai pensando di vendere”). Utile per provocare reazioni mirate.</p>
        </div>

        <div className="modal-section help-section">
          <h3>Modalità osservatore 👁️</h3>
          <p>L'interruttore in alto a destra mostra anche le DM tra residenti e la loro memoria privata. Spegnilo per un'esperienza più realistica, lasciato acceso per vedere tutto.</p>
        </div>

        <div className="modal-section" style={{ textAlign: 'center', borderBottom: 'none' }}>
          <button className="btn help-dismiss" onClick={onClose}>Ho capito, si gioca</button>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  // Auth gate: { checked: have we asked the server yet?, ok: cookie valid?,
  // configured: is ADMIN_PASSWORD/SESSION_SECRET set on the server? }
  const [auth, setAuth] = useState({ checked: false, ok: false, configured: true });
  const [state, setState] = useState(null);
  const [selectedChatId, setSelectedChatId] = useState(null);
  const [working, setWorking] = useState(false);
  const [typingByChat, setTypingByChat] = useState({});
  const [typingAgents, setTypingAgents] = useState({});
  const [dayStatus, setDayStatus] = useState(null);
  // God-view: see all chats, including DMs between residents.
  // Default ON for gameplay clarity. Turn off for fiction-respecting play.
  const [godView, setGodView] = useState(true);
  const [profileAgentId, setProfileAgentId] = useState(null);
  const [unreadByChat, setUnreadByChat] = useState({});
  const [suggestions, setSuggestions] = useState([]);
  // A transient "chat doesn't exist yet" placeholder, used when the admin
  // wants to DM a resident they've never messaged before. Cleared once the
  // real chat lands via SSE.
  const [pendingDmRecipient, setPendingDmRecipient] = useState(null);
  const [showHelp, setShowHelp] = useState(false);
  // Auto-advance bookkeeping. dayStartedAt is wall-clock ms when day_start
  // fired; nextAdvanceAt is when the next auto-advance is scheduled to run.
  const [dayStartedAt, setDayStartedAt] = useState(null);
  const [nextAdvanceAt, setNextAdvanceAt] = useState(null);
  const [nowTick, setNowTick] = useState(() => Date.now());
  const [paused, setPaused] = useState(false);

  // pausedRef lets SSE handlers (registered in a useEffect closure) read the
  // current paused state without re-subscribing every time it changes.
  const pausedRef = useRef(paused);
  useEffect(() => { pausedRef.current = paused; }, [paused]);

  // On mount, ask the server whether we already have a valid session cookie
  // (and whether auth is configured at all). Drives the Login gate below.
  useEffect(() => {
    let cancelled = false;
    api.health()
      .then(h => {
        if (cancelled) return;
        setAuth({
          checked: true,
          ok: !!h.authenticated,
          configured: !!h.auth_configured,
        });
      })
      .catch(() => {
        if (!cancelled) setAuth({ checked: true, ok: false, configured: true });
      });
    return () => { cancelled = true; };
  }, []);

  // Auto-open the tutorial once per browser, after a run is loaded.
  useEffect(() => {
    if (!state?.run_id) return;
    try {
      if (!localStorage.getItem('condosim_tutorial_seen_v1')) setShowHelp(true);
    } catch { /* localStorage unavailable */ }
  }, [state?.run_id]);

  const dismissHelp = useCallback(() => {
    setShowHelp(false);
    try { localStorage.setItem('condosim_tutorial_seen_v1', '1'); } catch { /* ignore */ }
  }, []);

  // Ref lets the auto-advance timer call the latest onAdvance without
  // re-scheduling every time state changes underneath us.
  const onAdvanceRef = useRef(null);

  // One-second ticker drives the timer/countdown labels in the topbar.
  useEffect(() => {
    if (!state?.run_id) return;
    const id = setInterval(() => setNowTick(Date.now()), 1000);
    return () => clearInterval(id);
  }, [state?.run_id]);

  // Schedule the first auto-advance whenever a run is loaded (new or saved).
  // Subsequent advances are scheduled inside onAdvance's success path, once
  // the backend POST returns (and the per-run lock has been released).
  useEffect(() => {
    if (!state?.run_id || state.ended || working || paused) return;
    setNextAdvanceAt(Date.now() + 2500);
  }, [state?.run_id]);

  // Fire the auto-advance once its scheduled time elapses.
  useEffect(() => {
    if (!nextAdvanceAt) return;
    const remaining = nextAdvanceAt - Date.now();
    const fire = () => {
      setNextAdvanceAt(null);
      onAdvanceRef.current?.();
    };
    if (remaining <= 0) { fire(); return; }
    const id = setTimeout(fire, remaining);
    return () => clearTimeout(id);
  }, [nextAdvanceAt]);

  // Ref so SSE handlers always see the latest selected chat without needing
  // to re-subscribe when the selection changes.
  const selectedChatIdRef = useRef(null);
  useEffect(() => { selectedChatIdRef.current = selectedChatId; }, [selectedChatId]);

  // Clear unread count when a chat is opened.
  const handleSelectChat = useCallback((chatId) => {
    setSelectedChatId(chatId);
    setPendingDmRecipient(null);
    setUnreadByChat(prev => {
      if (!prev[chatId]) return prev;
      const next = { ...prev };
      delete next[chatId];
      return next;
    });
  }, []);

  // Start (or open, if it exists) a DM with a resident.
  const handleStartDm = useCallback((residentId) => {
    const existing = state?.chats.find(c =>
      c.kind === 'dm'
      && c.member_ids.includes('admin')
      && c.member_ids.includes(residentId)
    );
    if (existing) {
      handleSelectChat(existing.id);
    } else {
      setSelectedChatId(null);
      setPendingDmRecipient(residentId);
    }
  }, [state, handleSelectChat]);

  // When a pending DM's real chat shows up in state (via SSE after first send),
  // switch selection to it and clear the placeholder.
  useEffect(() => {
    if (!pendingDmRecipient || !state) return;
    const real = state.chats.find(c =>
      c.kind === 'dm'
      && c.member_ids.includes('admin')
      && c.member_ids.includes(pendingDmRecipient)
    );
    if (real) {
      setSelectedChatId(real.id);
      setPendingDmRecipient(null);
    }
  }, [state?.chats, pendingDmRecipient]);

  // Subscribe to SSE on run load
  useEffect(() => {
    if (!state?.run_id) return;
    const runId = state.run_id;
    const es = new EventSource(`${BACKEND}/api/runs/${runId}/events`);

    // Every time the stream (re)opens — initial connect AND after a browser
    // auto-reconnect following a network drop — refetch the run so we catch
    // any messages that were published while we were disconnected. Without
    // this, a 30s+ blip (slow LLM call, Heroku router hiccup) can leave the
    // UI permanently behind DB state until the page is manually refreshed.
    es.addEventListener('open', () => {
      api.getRun(runId).then(fresh => {
        setState(prev => {
          if (!prev) return fresh;
          const byId = new Map(fresh.messages.map(m => [m.id, m]));
          for (const m of prev.messages) if (!byId.has(m.id)) byId.set(m.id, m);
          return { ...fresh, messages: Array.from(byId.values()).sort(
            (a, b) => a.fictional_timestamp_minutes - b.fictional_timestamp_minutes
          ) };
        });
      }).catch(() => {});
    });

    es.addEventListener('message_sent', (e) => {
      const payload = JSON.parse(e.data).data;
      const msg = payload.message;
      let isNewToState = false;
      setState(prev => {
        if (!prev) return prev;
        if (prev.messages.some(m => m.id === msg.id)) return prev;
        isNewToState = true;
        // Also merge any new chat if this is a freshly-created DM/group
        const chats = payload.chat && !prev.chats.some(c => c.id === payload.chat.id)
          ? [...prev.chats, payload.chat]
          : prev.chats;
        return { ...prev, messages: [...prev.messages, { ...msg, isNew: true }], chats };
      });
      // Bump unread count if the message is in a chat the user isn't currently
      // looking at, and it isn't the admin's own message.
      if (isNewToState
          && msg.sender_kind !== 'admin'
          && msg.chat_id !== selectedChatIdRef.current) {
        setUnreadByChat(prev => ({
          ...prev,
          [msg.chat_id]: (prev[msg.chat_id] || 0) + 1,
        }));
      }
    });

    es.addEventListener('typing_start', (e) => {
      const d = JSON.parse(e.data).data;
      setTypingAgents(prev => ({ ...prev, [d.agent_id]: d.display_name }));
    });

    es.addEventListener('typing_stop', (e) => {
      const d = JSON.parse(e.data).data;
      setTypingAgents(prev => {
        const next = { ...prev };
        delete next[d.agent_id];
        return next;
      });
    });

    es.addEventListener('day_start', (e) => {
      const d = JSON.parse(e.data).data;
      setDayStatus(`Giorno ${d.day} in corso…`);
      setDayStartedAt(Date.now());
      setNextAdvanceAt(null);
    });

    es.addEventListener('day_end', (e) => {
      const d = JSON.parse(e.data).data;
      setDayStatus(`Giorno ${d.day} concluso: ${d.activations} attivazioni, ${d.total_messages} messaggi totali.`);
      setDayStartedAt(null);
      setTypingAgents({});
      // NB: do NOT chain the next day or clear `working` here. day_end fires
      // mid-lifecycle while the per-run lock is still held (memory
      // consolidation is still running). Chaining lives in the day_done
      // handler below, which fires AFTER the lock is released.
    });

    es.addEventListener('day_done', (e) => {
      const d = JSON.parse(e.data).data;
      setWorking(false);
      setDayStartedAt(null);
      // Refresh authoritative state (clock, trust, motions, agent.notes
      // cleared by memory consolidation). Preserve any messages we already
      // streamed in via SSE so we don't drop ones the server hasn't yet
      // round-tripped.
      api.getRun(state.run_id).then(fresh => {
        setState(prev => {
          if (!prev) return fresh;
          const seen = new Set(fresh.messages.map(m => m.id));
          const extras = prev.messages.filter(m => !seen.has(m.id));
          return { ...fresh, messages: [...fresh.messages, ...extras] };
        });
        if (!d.ok) {
          setDayStatus('Errore: il giorno non è terminato correttamente.');
          return;
        }
        const nextDay = (fresh.clock?.day ?? 0) + 1;
        if (!pausedRef.current && !fresh.ended && nextDay <= 14) {
          setNextAdvanceAt(Date.now() + 3000);
        }
      }).catch(() => {});
    });

    es.addEventListener('motion_filed', (e) => {
      const d = JSON.parse(e.data).data;
      setState(prev => {
        if (!prev) return prev;
        if ((prev.motions || []).some(m => m.id === d.motion.id)) return prev;
        return { ...prev, motions: [...(prev.motions || []), d.motion] };
      });
    });

    es.addEventListener('vote_cast', (e) => {
      const d = JSON.parse(e.data).data;
      setState(prev => {
        if (!prev) return prev;
        return {
          ...prev,
          motions: (prev.motions || []).map(m =>
            m.id === d.motion_id ? { ...m, votes: { ...(m.votes || {}), [d.agent_id]: d.choice } } : m
          ),
        };
      });
    });

    es.addEventListener('motion_closed', (e) => {
      const d = JSON.parse(e.data).data;
      setState(prev => {
        if (!prev) return prev;
        return {
          ...prev,
          motions: (prev.motions || []).map(m => m.id === d.motion.id ? d.motion : m),
        };
      });
      // Pull fresh state (trust matrix + dials updated server-side)
      api.getRun(state.run_id).then(fresh => {
        setState(prev => prev ? { ...fresh, messages: prev.messages } : fresh);
      }).catch(() => {});
    });

    es.addEventListener('trust_updated', () => {
      // Re-fetch run for the authoritative trust matrix
      api.getRun(state.run_id).then(fresh => {
        setState(prev => prev ? { ...prev, trust: fresh.trust } : prev);
      }).catch(() => {});
    });

    es.addEventListener('error', (e) => {
      console.error('SSE error event', e);
    });

    es.onerror = (err) => {
      console.warn('EventSource error; will reconnect', err);
    };

    return () => {
      es.close();
    };
  }, [state?.run_id]);

  // Typing indicator shows on the main chat for now (we can't know which chat
  // an agent is composing for until the message lands)
  useEffect(() => {
    const names = Object.values(typingAgents);
    setTypingByChat(names.length > 0 ? { main: names } : {});
  }, [typingAgents]);

  const onAdvance = useCallback(async () => {
    if (!state || working) return;
    setNextAdvanceAt(null);
    setWorking(true);
    setDayStatus(`Avvio giorno ${state.clock.day}…`);
    try {
      // POST returns 202 immediately; the day runs as a background task on
      // the server. The day_done SSE handler clears `working`, refreshes
      // state, and chains the next advance.
      await api.advanceDay(state.run_id);
    } catch (e) {
      // 409 means the backend thinks a day is already running. This can
      // happen if onAdvance fired twice (React state closure lag) or if a
      // day_done SSE was missed. Treat it as a no-op — the active day will
      // publish day_done on completion and chain the next advance.
      if (e?.status === 409) {
        // Leave the status alone; the in-flight day will update it via SSE.
        return;
      }
      setDayStatus('Errore: ' + String(e));
      setWorking(false);
    }
  }, [state, working]);

  useEffect(() => { onAdvanceRef.current = onAdvance; }, [onAdvance]);

  // Pause / resume: cancel any pending advance when pausing; when resuming an
  // idle run, kick off the next day right away.
  const togglePause = useCallback(() => {
    setPaused(prev => {
      const next = !prev;
      if (next) {
        setNextAdvanceAt(null);
      } else if (state && !state.ended && !working && state.clock.day < 14) {
        setNextAdvanceAt(Date.now() + 500);
      }
      return next;
    });
  }, [state, working]);

  const onFileMotion = useCallback(async (title, description) => {
    if (!state) return;
    await api.fileMotion(state.run_id, title, description);
  }, [state]);

  const onCloseMotion = useCallback(async (motionId) => {
    if (!state) return;
    await api.closeMotion(state.run_id, motionId);
  }, [state]);

  const onSendAnnounce = useCallback(async (text) => {
    if (!state) return;
    await api.announce(state.run_id, text);
  }, [state?.run_id]);

  const onSendDm = useCallback(async (recipientId, text) => {
    if (!state) return;
    await api.sendDm(state.run_id, recipientId, text);
  }, [state?.run_id]);

  // Fetch suggestion chips whenever run state meaningfully changes.
  useEffect(() => {
    if (!state?.run_id) return;
    let cancelled = false;
    fetch(`${BACKEND}/api/runs/${state.run_id}/suggestions`)
      .then(r => r.json())
      .then(d => { if (!cancelled) setSuggestions(d.suggestions || []); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [state?.run_id, state?.clock?.day, (state?.motions || []).length]);

  // Auth gate: wait for the health check, then show login if needed.
  if (!auth.checked) return <div className="auth-loading">Caricamento…</div>;
  if (!auth.ok) {
    return (
      <Login
        configured={auth.configured}
        onAuthed={() => setAuth(a => ({ ...a, ok: true }))}
      />
    );
  }
  if (!state) return <Setup onCreated={setState} />;

  const displayDate = formatItalianDateTime(state.fictional_start_iso, state.clock.minutes_since_start);
  const messagesToday = state.messages.filter(m => m.day === state.clock.day).length;
  const formatElapsed = (ms) => {
    const s = Math.max(0, Math.floor(ms / 1000));
    return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  };
  let topbarSub;
  if (state.ended) {
    topbarSub = 'Partita conclusa · 14 giorni sono passati';
  } else if (working) {
    const timer = dayStartedAt ? ` · ⏱️ ${formatElapsed(nowTick - dayStartedAt)}` : '';
    const pausedHint = paused ? ' · ⏸ pausa dopo il giorno' : '';
    topbarSub = `Giorno ${state.clock.day} in corso${timer}${pausedHint}`;
  } else if (paused) {
    topbarSub = `Giorno ${state.clock.day} · ⏸ in pausa`;
  } else if (nextAdvanceAt) {
    const secs = Math.max(0, Math.ceil((nextAdvanceAt - nowTick) / 1000));
    topbarSub = state.clock.minutes_since_start === 0
      ? `Giorno 1 inizia fra ${secs}s…`
      : `Giorno ${state.clock.day} concluso · prossimo giorno fra ${secs}s…`;
  } else {
    topbarSub = `Giorno ${state.clock.day} · ${messagesToday} messaggi oggi`;
  }

  // Build a placeholder chat object when the admin is starting a DM with a
  // resident they've never messaged. ChatColumn renders it like a real chat.
  const pendingDmChat = pendingDmRecipient
    ? (() => {
        const a = state.agents.find(x => x.persona.id === pendingDmRecipient);
        return {
          id: `__pending_dm__${pendingDmRecipient}`,
          kind: 'dm',
          member_ids: ['admin', pendingDmRecipient],
          display_name: `DM con ${a?.persona.display_name || pendingDmRecipient}`,
          _pending: true,
        };
      })()
    : null;

  return (
    <div className="app">
      <div className="topbar">
        <div className="topbar-left">
          <div className="topbar-logo">🏢</div>
          <div>
            <h1>Condominio Via Garibaldi</h1>
            <div className="topbar-sub">{topbarSub}</div>
          </div>
        </div>
        <div className="fictional-clock" title="Data e ora nel mondo del palazzo">
          <div className="fictional-clock-label">Ora nel palazzo</div>
          <div className="fictional-clock-value">🕒 {displayDate}</div>
        </div>
        <div className="topbar-right">
          {!state.ended && (
            <button
              className={`pause-btn ${paused ? 'paused' : ''}`}
              onClick={togglePause}
              title={paused ? 'Riprendi il ciclo automatico dei giorni' : 'Metti in pausa — i giorni smettono di avanzare'}
              aria-label={paused ? 'Riprendi' : 'Pausa'}
            >
              {paused ? '▶ Riprendi' : '⏸ Pausa'}
            </button>
          )}
          <button
            className="help-btn"
            onClick={() => setShowHelp(true)}
            title="Come si gioca"
            aria-label="Come si gioca"
          >?</button>
          <label className="god-view-toggle" title="Mostra tutte le chat (incluse le DM tra altri residenti). Spegni per gioco fedele alla finzione.">
            <input
              type="checkbox"
              checked={godView}
              onChange={e => setGodView(e.target.checked)}
            />
            <span>👁️ Osservatore</span>
          </label>
          <div className="day-badge">
            <span className="day-badge-n">{state.clock.day}</span>
            <span className="day-badge-sep">/</span>
            <span className="day-badge-tot">14</span>
          </div>
        </div>
      </div>
      {dayStatus && <div className="day-status-bar">{dayStatus}</div>}
      <div className="main">
        <LeftPanel
          state={state}
          selectedChatId={selectedChatId}
          onSelectChat={handleSelectChat}
          onStartDm={handleStartDm}
          unreadByChat={unreadByChat}
          typingByChat={typingByChat}
          godView={godView}
          onOpenProfile={setProfileAgentId}
        />
        <ChatColumn
          state={state}
          selectedChatId={selectedChatId}
          pendingChat={pendingDmChat}
          typingByChat={typingByChat}
          godView={godView}
          suggestions={suggestions}
          onSendAnnounce={onSendAnnounce}
          onSendDm={onSendDm}
          onOpenProfile={setProfileAgentId}
        />
        <AdminConsole
          state={state}
          onFileMotion={onFileMotion}
          onCloseMotion={onCloseMotion}
          suggestions={suggestions}
        />
      </div>
      {showHelp && <HelpModal onClose={dismissHelp} />}
      {profileAgentId && (
        <ProfileModal
          state={state}
          agentId={profileAgentId}
          onClose={() => setProfileAgentId(null)}
          onOpenChat={(chatId) => { setSelectedChatId(chatId); setProfileAgentId(null); }}
          godView={godView}
          onGoalSaved={(aid, goal) => {
            setState(prev => prev ? {
              ...prev,
              agents: prev.agents.map(a =>
                a.persona.id === aid ? { ...a, admin_goal: goal } : a
              ),
            } : prev);
          }}
        />
      )}
    </div>
  );
}
