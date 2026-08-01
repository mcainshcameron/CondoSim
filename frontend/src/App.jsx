import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, apiBase } from './api.js';
import Login from './Login.jsx';

// Empty in production (single-origin deploy on Heroku); set to a non-empty
// VITE_API_BASE for `vite dev` against a remote backend. Same convention as
// api.js — see api.js for the env var.
const BACKEND = apiBase;

// Resident messages render at a wall-clock pace derived from their
// fictional-time delta — the agents already produce a natural rhythm
// (1–4 fic min within an activation, 5–360 between activations), so we
// honour it. Bursts feel rapid, lulls breathe. Admin sends still render
// instantly. Floor + cap keep both extremes humane.
const REAL_MS_PER_FIC_MIN = 300;
const MIN_RENDER_GAP_MS = 1400;
const MAX_RENDER_GAP_MS = 6500;
const AUTO_ADVANCE_DELAY_MS = 1200;

// Hard ceiling on how far behind the server the render queue may fall.
// Without it the queue is unbounded: a busy day emits ~15 messages, each
// waiting up to MAX_RENDER_GAP_MS, so the UI could sit a minute behind and
// the *next* day would start streaming while the previous one was still
// flushing — two days' messages interleaving in the queue. Capping the lag
// keeps pacing pleasant without ever letting it become the bottleneck.
const MAX_QUEUE_LAG_MS = 7000;

// Playback speeds offered in the topbar. `Infinity` renders instantly.
const SPEEDS = [
  { id: 'calma',   label: '0.5×', factor: 0.5 },
  { id: 'normale', label: '1×',   factor: 1 },
  { id: 'veloce',  label: '2×',   factor: 2 },
  { id: 'subito',  label: '⚡',    factor: Infinity },
];

// Treat the stream as stalled after this long with no SSE traffic while a
// day is supposedly running.
const SSE_STALL_MS = 25000;

const END_REASON_TEXT = {
  cost_cap_exceeded:
    'Partita conclusa: raggiunto il tetto di spesa impostato per questa partita.',
  monthly_budget_exhausted:
    'Partita conclusa: raggiunto il tetto di spesa mensile. Riprende il mese prossimo, o alza MONTHLY_COST_CAP_USD.',
};

function compareMessages(a, b) {
  if (a.fictional_timestamp_minutes !== b.fictional_timestamp_minutes) {
    return a.fictional_timestamp_minutes - b.fictional_timestamp_minutes;
  }
  // `seq` is the backend's per-run monotonic counter (backend/timeline.py).
  // Together with the timestamp it is a TOTAL order, so re-sorting after a
  // refetch or an out-of-order SSE arrival can never reshuffle messages the
  // player has already read — which is exactly what used to happen.
  const aSeq = a.seq || 0;
  const bSeq = b.seq || 0;
  if (aSeq !== bSeq) return aSeq - bSeq;
  return String(a.id || '').localeCompare(String(b.id || ''));
}

function mergeMessagesChronologically(existing, incoming) {
  const byId = new Map((existing || []).map(m => [m.id, m]));
  for (const msg of incoming || []) {
    if (!msg?.id) continue;
    byId.set(msg.id, { ...(byId.get(msg.id) || {}), ...msg });
  }
  return Array.from(byId.values()).sort(compareMessages);
}

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

// Residents for whom we have a photoreal portrait PNG in /avatars/.
const RESIDENT_PORTRAITS = new Set([
  'conti', 'ferrari', 'greco', 'marchetti', 'romano',
]);

// For a DM chat, resolve the "other" member id if it's one of ours — lets
// the chat list / header render that resident's face instead of a generic
// initials circle for opaque chat ids like `dm_admin_conti_a1b2`.
function residentIdForChat(chat) {
  if (!chat || chat.kind !== 'dm' || !chat.member_ids) return null;
  const other = chat.member_ids.find(
    mid => mid !== 'admin' && RESIDENT_PORTRAITS.has(mid),
  );
  return other || null;
}

function Avatar({ id, name, size = 36, title }) {
  const label = title || name;
  if (RESIDENT_PORTRAITS.has(id)) {
    return (
      <img
        className="avatar avatar-img"
        src={`/avatars/${id}.webp`}
        alt={name || id}
        title={label}
        width={size}
        height={size}
        style={{ width: size, height: size }}
      />
    );
  }
  return (
    <div
      className="avatar"
      style={{
        width: size,
        height: size,
        background: colorFor(id || name || '?'),
        fontSize: Math.round(size * 0.4),
      }}
      title={label}
    >
      {initialsOf(name)}
    </div>
  );
}

function Setup({ onCreated }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [openingText, setOpeningText] = useState('');
  const [openingTemplates, setOpeningTemplates] = useState([]);
  const textareaRef = useRef(null);

  useEffect(() => {
    api.defaultOpening().then(d => setOpeningText(d.text || '')).catch(() => {});
    api.openingTemplates().then(d => setOpeningTemplates(d.templates || [])).catch(() => {});
  }, []);

  // Auto-grow the opening textarea with its content (capped by CSS max-height).
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = 'auto';
    ta.style.height = `${ta.scrollHeight}px`;
  }, [openingText]);

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

  // Atmospheric cast preview — hardcoded names match the v1 scenario (heating_crisis).
  const castPreview = [
    { id: 'conti',     name: 'Maria Conti',       sub: 'int. 2B · vedova, 40 anni qui' },
    { id: 'ferrari',   name: 'Marco Ferrari',     sub: 'int. 5A · consulente, spesso altrove' },
    { id: 'greco',     name: 'Valentina Greco',   sub: 'int. 7A · "consulente immobiliare"' },
    { id: 'marchetti', name: 'Davide Marchetti',  sub: 'int. 3B · cura la madre anziana' },
    { id: 'romano',    name: 'Giulia Romano',     sub: 'int. 4C · designer, appena comprato' },
  ];

  const scrollToSetup = () => {
    const el = document.getElementById('setup-begin');
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  return (
    <div className="setup-page">
      <section className="landing-hero">
        <img
          className="landing-bg"
          src="/hero/skyline.webp"
          alt=""
          fetchPriority="high"
        />
        <div className="landing-dim" aria-hidden="true" />

        <div className="landing-title-wrap">
          <h1 className="landing-title">CondoSim</h1>
          <div className="landing-subtitle">Condominio Via Garibaldi</div>
          <div className="landing-tagline">
            Cinque residenti. Una chat. Un amministratore.
          </div>
        </div>

        <div className="landing-cutouts" aria-hidden="true">
          <img className="cutout cutout-conti"
               src="/hero/conti_cutout.webp" alt="" loading="lazy" />
          <img className="cutout cutout-marchetti"
               src="/hero/marchetti_cutout.webp" alt="" loading="lazy" />
          <img className="cutout cutout-greco"
               src="/hero/greco_cutout.webp" alt="" loading="lazy" />
          <img className="cutout cutout-ferrari"
               src="/hero/ferrari_cutout.webp" alt="" loading="lazy" />
          <img className="cutout cutout-romano"
               src="/hero/romano_cutout.webp" alt="" />
        </div>

        <button className="landing-cta" onClick={scrollToSetup}>
          Entra nel palazzo
        </button>
        <div
          className="landing-scroll-hint"
          onClick={scrollToSetup}
          aria-hidden="true"
        >
          <span>▾</span>
        </div>
      </section>

      <section id="setup-begin" className="setup-break">
        <div className="setup-break-line" />
        <div className="setup-break-label">Nuova partita</div>
        <div className="setup-break-line" />
      </section>

      <div className="setup-card">

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
            {openingTemplates.length > 0 && (
              <div className="opening-template-grid" aria-label="Modelli di apertura">
                {openingTemplates.map(t => (
                  <button
                    key={t.id}
                    type="button"
                    className="opening-template"
                    onClick={() => setOpeningText(t.text || '')}
                    title={t.text}
                  >
                    <span className="opening-template-label">{t.label}</span>
                    <span className="opening-template-difficulty">{t.difficulty}</span>
                  </button>
                ))}
              </div>
            )}
            <div className="setup-compose-card">
              <div className="setup-compose-head">
                <Avatar id="admin" name="Amministratore" size={30} />
                <div>
                  <div className="setup-compose-head-title">Amministratore</div>
                  <div className="setup-compose-head-sub">→ Gruppo Condominio Via Garibaldi</div>
                </div>
              </div>
              <textarea
                ref={textareaRef}
                className="setup-textarea"
                value={openingText}
                onChange={e => setOpeningText(e.target.value)}
                rows={2}
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

            {error && <div className="setup-error">{error}</div>}
          </div>
        </div>
      </div>
    </div>
  );
}

function findChatForResident(state, residentId) {
  const adminDm = state.chats.find(c => c.kind === 'dm'
    && c.member_ids.includes('admin') && c.member_ids.includes(residentId));
  return adminDm?.id || null;
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

function ProfileModal({ state, agentId, onClose, onOpenChat, onGoalSaved, memoryRefreshSeq }) {
  const agent = state.agents.find(a => a.persona.id === agentId);
  const [goalDraft, setGoalDraft] = useState(agent?.admin_goal || '');
  const [goalSaving, setGoalSaving] = useState(false);
  const [goalStatus, setGoalStatus] = useState('');
  const [soulText, setSoulText] = useState(null);
  const [memoryText, setMemoryText] = useState(null);
  const [showSoul, setShowSoul] = useState(false);
  const [showMemory, setShowMemory] = useState(false);
  const lastMemoryRefreshSeqRef = useRef(memoryRefreshSeq);

  useEffect(() => {
    setGoalDraft(agent?.admin_goal || '');
    // Reset on agent switch; fetched lazily when user expands.
    setSoulText(null);
    setMemoryText(null);
    setShowSoul(false);
    setShowMemory(false);
    lastMemoryRefreshSeqRef.current = memoryRefreshSeq;
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

  const refreshMemory = useCallback(async () => {
    try {
      const { content } = await api.getAgentMemory(state.run_id, agentId);
      setMemoryText(content || '(vuoto)');
    } catch (e) {
      setMemoryText('Errore nel caricamento: ' + String(e));
    }
  }, [state.run_id, agentId]);

  useEffect(() => {
    if (!showMemory || memoryText === null) return;
    if (memoryRefreshSeq <= lastMemoryRefreshSeqRef.current) return;
    lastMemoryRefreshSeqRef.current = memoryRefreshSeq;
    refreshMemory();
  }, [memoryRefreshSeq, memoryText, refreshMemory, showMemory]);

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

  const lastName = p.display_name.split(' ').slice(-1)[0] || p.display_name;
  const ownerLabel = OWNER_KIND_LABEL[agent.owner.kind] || agent.owner.kind;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <div>
            <h2>{p.display_name}</h2>
            <div className="modal-sub">
              Interno {p.unit} · {p.millesimi}/1000 millesimi
              {' · ~€'}{agent.starting_wallet_eur.toLocaleString('it-IT')}
              {' · ⏱️ '}{p.responsiveness}/{p.time_of_day}
              {' · 📮 '}{allMessages.length} msg ({todayMsgs.length} oggi)
              {ownerLabel && ` · ${ownerLabel}`}
            </div>
          </div>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>

        <div className="modal-section goal-section">
          <h3>🎯 Obiettivo aggiuntivo <span className="goal-hint">(entra nella testa di {lastName} al prossimo turno)</span></h3>
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
          <p>{p.public_description}</p>
        </div>

        <div className="modal-section">
          <h3>Chat a cui partecipa ({theirChats.length})</h3>
          <div className="profile-chats">
            {chatSummary.map(({ chat, total, byThem, others }) => (
              <div
                key={chat.id}
                className="profile-chat-row clickable"
                onClick={() => onOpenChat(chat.id)}
              >
                <div>
                  <strong>{chat.display_name}</strong>
                  <span className="profile-chat-with"> con {others || '—'}</span>
                </div>
                <div className="profile-chat-meta">
                  {byThem}/{total} msg<span style={{color:'var(--brand)', marginLeft: 6}}>→</span>
                </div>
              </div>
            ))}
          </div>
        </div>

        <details className="modal-section observer">
          <summary className="observer-summary">
            <span>🔒 Modalità osservatore</span>
          </summary>
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
          <div className="observer-field">
            <div className="observer-label">Relazioni (fiducia)</div>
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
          {voteHistory.length > 0 && (
            <div className="observer-field">
              <div className="observer-label">Voti ({voteHistory.length})</div>
              {voteHistory.map(({ motion, vote }) => (
                <div key={motion.id} className="profile-vote">
                  <span>{vote === 'yes' ? '✅' : vote === 'no' ? '❌' : '⚪'}</span>
                  <span>{motion.title}</span>
                  <span className={`motion-status-tag ${motion.status}`}>{motion.status}</span>
                </div>
              ))}
            </div>
          )}
          {agent.notes?.length > 0 && (
            <div className="observer-field">
              <div className="observer-label">Appunti privati ({agent.notes.length})</div>
              <ol className="observer-notes">
                {agent.notes.map((n, i) => <li key={i}>{n}</li>)}
              </ol>
            </div>
          )}
        </details>
      </div>
    </div>
  );
}

function truncate(s, n) {
  if (!s) return '';
  return s.length > n ? s.slice(0, n - 1).trimEnd() + '…' : s;
}

function searchText(...parts) {
  return parts.filter(Boolean).join(' ').toLowerCase();
}

function runPulse(state) {
  const day = state.clock?.day || 1;
  const todayMessages = state.messages.filter(m => m.day === day);
  const residentMessages = todayMessages.filter(m => m.sender_kind === 'resident');
  const adminMessages = todayMessages.filter(m => m.sender_kind === 'admin');
  const externalMessages = todayMessages.filter(m => m.sender_kind === 'external');
  const openMotions = (state.motions || []).filter(m => m.status === 'open');
  const questions = residentMessages.reduce((sum, m) => sum + ((m.content || '').match(/\?/g) || []).length, 0);
  const reactions = state.messages.reduce((sum, m) =>
    sum + Object.values(m.reactions || {}).reduce((n, bucket) => n + bucket.length, 0), 0);
  const trustValues = [];
  for (const row of Object.values(state.trust || {})) {
    for (const v of Object.values(row || {})) trustValues.push(Number(v) || 0);
  }
  const trustMovement = trustValues.reduce((sum, v) => sum + Math.abs(v), 0);
  const score = Math.min(100, Math.round(
    residentMessages.length * 5 +
    questions * 6 +
    openMotions.length * 14 +
    Math.min(30, trustMovement * 18)
  ));
  const level = score >= 70 ? 'Alta tensione' : score >= 35 ? 'Attivo' : 'Calmo';
  return { day, todayMessages, residentMessages, adminMessages, externalMessages, openMotions, questions, reactions, trustMovement, score, level };
}

function residentStatusTags(state, agent) {
  const aid = agent.persona.id;
  const day = state.clock?.day || 1;
  const sentToday = state.messages.filter(m => m.sender_id === aid && m.day === day).length;
  const receivedToday = state.messages.filter(m => m.day === day && (m.audience || []).includes(aid)).length;
  const trustRow = Object.values(state.trust?.[aid] || {});
  const avgTrust = trustRow.length
    ? trustRow.reduce((sum, v) => sum + Number(v || 0), 0) / trustRow.length
    : 0;
  const tags = [];
  if (sentToday >= 3) tags.push('molto attivo');
  else if (sentToday > 0) tags.push('presente');
  else if (receivedToday > 0) tags.push('silenzioso');
  if (avgTrust >= 0.12) tags.push('ben visto');
  if (avgTrust <= -0.12) tags.push('isolato');
  if (agent.admin_goal) tags.push('obiettivo');
  return tags.slice(0, 3);
}

function LeftPanel({ state, selectedChatId, onSelectChat, onStartDm, unreadByChat, typingByChat, queuedByChat, onOpenProfile }) {
  const [panelQuery, setPanelQuery] = useState('');
  const [chatFilter, setChatFilter] = useState('all');

  // Compute DM partner counts + trust edges from current state
  const nameById = useMemo(() => {
    const m = {};
    for (const a of state.agents) m[a.persona.id] = a.persona.display_name;
    m['admin'] = 'Amministratore';
    return m;
  }, [state.agents]);
  const normalizedQuery = panelQuery.trim().toLowerCase();

  // Chat list shows only admin-participating conversations (group + admin DMs).
  // Inter-resident DMs live in the "DM frequenti" section below and open in
  // the center column when clicked.
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

  const filteredChatListItems = useMemo(() => {
    return chatListItems.filter(({ chat, last }) => {
      if (chatFilter === 'unread' && !(unreadByChat?.[chat.id] > 0)) return false;
      if (chatFilter === 'dm' && chat.kind !== 'dm') return false;
      if (!normalizedQuery) return true;
      const memberNames = (chat.member_ids || []).map(id => nameById[id] || id).join(' ');
      return searchText(
        chat.display_name,
        memberNames,
        last?.sender_display_name,
        last?.content,
      ).includes(normalizedQuery);
    });
  }, [chatListItems, chatFilter, nameById, normalizedQuery, unreadByChat]);

  const filteredAgents = useMemo(() => {
    if (!normalizedQuery) return state.agents;
    return state.agents.filter(a => searchText(
      a.persona.display_name,
      a.persona.unit,
      a.persona.public_description,
      a.persona.responsiveness,
      a.persona.time_of_day,
    ).includes(normalizedQuery));
  }, [normalizedQuery, state.agents]);

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
      <div className="panel-search">
        <input
          type="search"
          value={panelQuery}
          onChange={e => setPanelQuery(e.target.value)}
          placeholder="Cerca chat o residenti"
          aria-label="Cerca chat o residenti"
        />
        {panelQuery && (
          <button
            type="button"
            className="panel-search-clear"
            onClick={() => setPanelQuery('')}
            aria-label="Cancella ricerca"
            title="Cancella"
          >×</button>
        )}
      </div>
      <div className="chat-filter-tabs" role="tablist" aria-label="Filtro chat">
        <button
          type="button"
          className={chatFilter === 'all' ? 'active' : ''}
          onClick={() => setChatFilter('all')}
        >Tutte</button>
        <button
          type="button"
          className={chatFilter === 'unread' ? 'active' : ''}
          onClick={() => setChatFilter('unread')}
        >Non lette</button>
        <button
          type="button"
          className={chatFilter === 'dm' ? 'active' : ''}
          onClick={() => setChatFilter('dm')}
        >DM</button>
      </div>
      <div className="chat-list">
        {filteredChatListItems.length === 0 && (
          <div className="chat-list-empty">Nessuna chat corrispondente.</div>
        )}
        {filteredChatListItems.map(({ chat, last }) => {
          const unread = unreadByChat?.[chat.id] || 0;
          const typing = (typingByChat?.[chat.id] || []).length > 0;
          const queued = (queuedByChat?.[chat.id] || []).length > 0;
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
              <Avatar id={residentIdForChat(chat) || chat.id} name={chat.display_name} size={42} />
              <div className="chat-list-main">
                <div className="chat-list-top">
                  <span className="chat-list-name">{chat.display_name}</span>
                  <span className="chat-list-time">{timeLabel}</span>
                </div>
                <div className="chat-list-bot">
                  <span className="chat-list-preview">
                    {typing
                      ? <span className="chat-list-typing">sta scrivendo…</span>
                      : queued
                        ? <span className="chat-list-queued">in arrivo...</span>
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
      {filteredAgents.length === 0 && (
        <div className="resident-empty">Nessun residente corrispondente.</div>
      )}
      {filteredAgents.map(a => {
        const msgsToday = state.messages.filter(m => m.sender_id === a.persona.id && m.day === state.clock.day).length;
        const chatId = findChatForResident(state, a.persona.id);
        const tags = residentStatusTags(state, a);
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
              {(msgsToday > 0 || tags.length > 0) && (
                <div className="resident-status-row">
                  {msgsToday > 0 && <span className="resident-today">{msgsToday} msg oggi</span>}
                  {tags.map(tag => <span key={tag} className="resident-status-chip">{tag}</span>)}
                </div>
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
                const canOpen = !!chatId;
                return (
                  <div
                    key={i}
                    className={`alliance-row ${canOpen ? 'clickable' : ''}`}
                    onClick={() => canOpen && onSelectChat(chatId)}
                    title={canOpen ? 'Apri la chat' : 'Chat non ancora aperta'}
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

function ChatComposer({ state, chat, suggestions, onSendAnnounce, onSendDm }) {
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  const textareaRef = useRef(null);
  const softLimit = 700;

  // Clear composer state whenever the active chat changes.
  useEffect(() => {
    setText('');
    setError('');
  }, [chat?.id]);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
  }, [text, chat?.id]);

  if (!chat) return null;

  const isGroup = chat.kind === 'main' || chat.kind === 'assembly';
  const isAdminDm = chat.kind === 'dm' && chat.member_ids.includes('admin');
  const otherId = isAdminDm ? chat.member_ids.find(id => id !== 'admin') : null;
  const otherAgent = otherId ? state.agents.find(a => a.persona.id === otherId) : null;

  let mode = 'disabled';
  let placeholder = 'Chat privata tra residenti — puoi solo osservare.';
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
  const countState = text.length > softLimit
    ? 'over'
    : text.length > softLimit * 0.8
      ? 'warn'
      : '';

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
          <span className="chat-composer-hint dynamic">Ctrl+Enter per inviare</span>
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
          ref={textareaRef}
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
      {(error || text.length > 0) && (
        <div className="chat-composer-meta">
          <span className="chat-composer-error">{error}</span>
          {text.length > 0 && (
            <span className={`chat-composer-count ${countState}`}>
              {text.length}/{softLimit}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function ChatColumn({ state, selectedChatId, pendingChat, typingByChat, queuedByChat, suggestions, onSendAnnounce, onSendDm, onOpenProfile, onMobileBack, onOpenConsole }) {
  const chats = state.chats;
  const msgsByChat = useMemo(() => {
    const m = new Map();
    for (const c of chats) m.set(c.id, []);
    for (const msg of state.messages) {
      if (m.has(msg.chat_id)) m.get(msg.chat_id).push(msg);
    }
    for (const arr of m.values()) arr.sort(compareMessages);
    return m;
  }, [state, chats]);

  // Priority: explicit selection → pending placeholder → first available chat.
  const selected = chats.find(c => c.id === selectedChatId) || pendingChat || chats[0];
  const selectedMsgs = (selected && !selected._pending) ? (msgsByChat.get(selected.id) || []) : [];
  const scrollRef = useRef(null);
  const pinnedToBottomRef = useRef(true);
  const previousChatIdRef = useRef(null);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);

  const typingNames = selected ? (typingByChat[selected.id] || []) : [];
  const queuedNames = selected ? (queuedByChat?.[selected.id] || []) : [];
  const nameById = useMemo(() => {
    const out = { admin: 'Amministratore' };
    for (const a of state.agents) out[a.persona.id] = a.persona.display_name;
    return out;
  }, [state.agents]);

  const scrollToLatest = useCallback((behavior = 'smooth') => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior });
    pinnedToBottomRef.current = true;
    setShowJumpToLatest(false);
  }, []);

  const onMessageScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 96;
    pinnedToBottomRef.current = nearBottom;
    if (nearBottom) setShowJumpToLatest(false);
  }, []);

  // Auto-scroll only when the user is already reading the latest messages.
  // If they scrolled up, preserve their place and offer a small jump button.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const chatChanged = previousChatIdRef.current !== selected?.id;
    previousChatIdRef.current = selected?.id;
    if (chatChanged) {
      requestAnimationFrame(() => scrollToLatest('auto'));
      return;
    }
    if (pinnedToBottomRef.current) {
      requestAnimationFrame(() => scrollToLatest('smooth'));
    } else {
      setShowJumpToLatest(true);
    }
  }, [selectedMsgs.length, selected?.id, scrollToLatest]);

  // Build a WhatsApp-style chat header line for the current chat.
  const chatHeader = useMemo(() => {
    if (!selected) return null;
    const todayCount = selectedMsgs.filter(m => m.day === state.clock.day).length;
    const msgMeta = todayCount > 0
      ? `${todayCount} msg oggi`
      : selectedMsgs.length > 0
        ? `${selectedMsgs.length} msg totali`
        : 'nessun messaggio';
    const otherNames = (selected.member_ids || [])
      .filter(id => id !== 'admin')
      .map(id => {
        const a = state.agents.find(x => x.persona.id === id);
        return a?.persona.display_name || id;
      });
    if (selected.kind === 'main' || selected.kind === 'assembly') {
      return { title: selected.display_name, sub: `${otherNames.length} residenti - ${msgMeta}` };
    }
    if (selected.kind === 'dm') {
      return { title: selected.display_name, sub: `Messaggio privato - ${msgMeta}` };
    }
    return { title: selected.display_name, sub: '' };
  }, [selected, selectedMsgs, state.agents, state.clock.day]);

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
          {onMobileBack && (
            <button
              type="button"
              className="mobile-back"
              onClick={(e) => { e.stopPropagation(); onMobileBack(); }}
              aria-label="Torna alle chat"
              title="Torna alle chat"
            >‹</button>
          )}
          <Avatar id={dmOtherId || selected.id} name={chatHeader.title} size={36} />
          <div className="chat-header-text">
            <div className="chat-header-title">{chatHeader.title}</div>
            <div className="chat-header-sub">
              {typingNames.length > 0 ? (
                <span className="chat-header-typing">
                  {typingNames.length === 1
                    ? `${typingNames[0]} sta scrivendo`
                    : typingNames.length === 2
                      ? `${typingNames[0]} e ${typingNames[1]} stanno scrivendo`
                      : `${typingNames.length} stanno scrivendo`}
                  <span className="dots"><span>.</span><span>.</span><span>.</span></span>
                </span>
              ) : queuedNames.length > 0 ? (
                <span className="chat-header-queued">
                  {queuedNames.length === 1
                    ? `${queuedNames[0]} sta inviando...`
                    : `${queuedNames.length} messaggi in arrivo...`}
                </span>
              ) : chatHeader.sub}
            </div>
          </div>
          {onOpenConsole && (
            <button
              type="button"
              className="mobile-console-btn"
              onClick={(e) => { e.stopPropagation(); onOpenConsole(); }}
              aria-label="Apri console amministratore"
              title="Mozioni e azioni"
            >📋</button>
          )}
        </div>
      )}
      <div className="chat-messages" ref={scrollRef} onScroll={onMessageScroll}>
        {renderedItems.length === 0 ? (
          <div className="chat-empty">
            <div className="chat-empty-icon">💬</div>
            <div>Nessun messaggio ancora.</div>
            <div className="chat-empty-sub">Scrivi qui sotto: i residenti risponderanno.</div>
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
            const reactionEntries = Object.entries(m.reactions || {})
              .filter(([, ids]) => Array.isArray(ids) && ids.length > 0);
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
                <div className={`msg ${kindClass} ${reactionEntries.length ? 'has-reactions' : ''}`}>
                  {!sameAsPrev && !isSelf && (
                    <div className="sender">{m.sender_display_name}</div>
                  )}
                  <div className="content">{m.content}</div>
                  {reactionEntries.length > 0 && (
                    <div className="msg-reactions">
                      {reactionEntries.map(([emoji, ids]) => (
                        <span
                          key={emoji}
                          className="msg-reaction"
                          title={ids.map(id => nameById[id] || id).join(', ')}
                        >
                          {emoji}{ids.length > 1 ? ` ${ids.length}` : ''}
                        </span>
                      ))}
                    </div>
                  )}
                  <div className="ts">{ts}</div>
                </div>
              </div>
            );
          })
        )}
        {showJumpToLatest && (
          <button
            type="button"
            className="new-message-jump"
            onClick={() => scrollToLatest('smooth')}
          >
            Nuovi messaggi
          </button>
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

function AdminConsole({ state, onFileMotion, onCloseMotion, suggestions, onMobileBack }) {
  const [motionTitle, setMotionTitle] = useState('');
  const [motionDesc, setMotionDesc] = useState('');
  const [status, setStatus] = useState('');
  const [tab, setTab] = useState('azioni');

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
  const pulse = runPulse(state);
  const metrics = state.metrics || {};
  const promptChips = (suggestions || []).filter(s => !s.action);
  const actionChips = (suggestions || []).filter(s => s.action === 'close_motion');
  const topSpeakers = [...state.agents]
    .map(a => ({
      id: a.persona.id,
      name: a.persona.display_name,
      count: state.messages.filter(m => m.sender_id === a.persona.id && m.day === state.clock.day).length,
    }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 3);

  return (
    <div className="console">
      {onMobileBack && (
        <div className="console-mobile-bar">
          <button
            type="button"
            className="mobile-back"
            onClick={onMobileBack}
            aria-label="Indietro"
            title="Indietro"
          >‹</button>
          <span className="console-mobile-title">Console amministratore</span>
        </div>
      )}
      <div className="console-tabs" role="tablist" aria-label="Console amministratore">
        <button type="button" className={tab === 'azioni' ? 'active' : ''} onClick={() => setTab('azioni')}>Azioni</button>
        <button type="button" className={tab === 'mozioni' ? 'active' : ''} onClick={() => setTab('mozioni')}>
          Mozioni{openMotions.length ? ` ${openMotions.length}` : ''}
        </button>
        <button type="button" className={tab === 'stats' ? 'active' : ''} onClick={() => setTab('stats')}>Stats</button>
      </div>
      {tab === 'azioni' && (
        <div className="console-section">
          <h2>Prossima mossa</h2>
          <div className="pulse-card">
            <div className="pulse-card-top">
              <span>{pulse.level}</span>
              <strong>{pulse.score}/100</strong>
            </div>
            <div className="pulse-bar"><span style={{ width: `${pulse.score}%` }} /></div>
            <div className="pulse-meta">
              {pulse.residentMessages.length} msg residenti oggi · {pulse.questions} domande · {openMotions.length} mozioni aperte
            </div>
          </div>
          {actionChips.length > 0 ? (
            <div className="quick-actions">
              {actionChips.map(s => (
                <button
                  key={s.id}
                  className="btn secondary quick-action-btn next-action"
                  onClick={() => handleCloseFromChip(s)}
                  title={s.label}
                >{s.label}</button>
              ))}
            </div>
          ) : promptChips.length > 0 ? (
            <div className="quick-actions compact">
              {promptChips.slice(0, 3).map(s => (
                <div key={s.id} className="suggestion-card" title={s.body || s.label}>
                  <strong>{s.label}</strong>
                  <span>{truncate(s.body || '', 92)}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="empty-note">Nessuna azione urgente. Osserva il prossimo giro o scrivi nel gruppo.</div>
          )}
        </div>
      )}
      {/* Motions */}
      {tab === 'mozioni' && (
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
      )}

      {tab === 'stats' && (
        <div className="console-section">
          <h2>Statistiche</h2>
          <div className="stats-grid">
            <div><strong>{pulse.todayMessages.length}</strong><span>msg oggi</span></div>
            <div><strong>{pulse.adminMessages.length}</strong><span>tuoi avvisi</span></div>
            <div><strong>{pulse.externalMessages.length}</strong><span>eventi</span></div>
            <div><strong>${Number(metrics.estimated_cost_usd || 0).toFixed(4)}</strong><span>spesa stim.</span></div>
          </div>
          <div className="console-mini-list">
            <div className="mini-list-title">Residenti piu attivi oggi</div>
            {topSpeakers.map(s => (
              <div key={s.id} className="mini-list-row">
                <span>{s.name}</span>
                <strong>{s.count}</strong>
              </div>
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
          <h3>Cosa hai a disposizione</h3>
          <ul className="help-list">
            <li><strong>Chat</strong> — La lista delle conversazioni e dei residenti. Tocca una chat per aprirla, o un residente per vederne il profilo.</li>
            <li><strong>Conversazione</strong> — Lo scambio attivo. Scrivi in basso per inviare un avviso al gruppo o un DM privato. Da telefono, la freccia ‹ in alto a sinistra ti riporta alla lista.</li>
            <li><strong>Console amministratore</strong> — Mozioni e azioni rapide. Da telefono raggiungila col bottone 📋 nell'intestazione della chat (o dal banner verde sopra la lista).</li>
          </ul>
        </div>

        <div className="modal-section help-section">
          <h3>La conversazione</h3>
          <ol className="help-list">
            <li>Scrivi un avviso nel gruppo, oppure un DM privato a un residente.</li>
            <li>I residenti iniziano a rispondere in tempo finzionale (li vedrai “stanno scrivendo…”).</li>
            <li>Il tempo scorre da solo. Tu non devi avanzare o fermare i giorni.</li>
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
          <p>Vedi tutte le chat (incluse le DM tra residenti) e, dal profilo di un residente, la sua identità (SOUL) e il taccuino privato (MEMORY) che cresce ogni giorno.</p>
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
  const [typingAgents, setTypingAgents] = useState({});
  const [dayError, setDayError] = useState(null);
  const [profileAgentId, setProfileAgentId] = useState(null);
  const [unreadByChat, setUnreadByChat] = useState({});
  const [queuedMessages, setQueuedMessages] = useState([]);
  const [memoryRefreshSeq, setMemoryRefreshSeq] = useState(0);
  const [suggestions, setSuggestions] = useState([]);
  // A transient "chat doesn't exist yet" placeholder, used when the admin
  // wants to DM a resident they've never messaged before. Cleared once the
  // real chat lands via SSE.
  const [pendingDmRecipient, setPendingDmRecipient] = useState(null);
  const [showHelp, setShowHelp] = useState(false);
  // Auto-advance bookkeeping. Days chain themselves after each day_done;
  // the player steers with the pause + speed controls in the topbar.
  const [nextAdvanceAt, setNextAdvanceAt] = useState(null);
  const [paused, setPaused] = useState(false);
  const [speedId, setSpeedId] = useState('normale');

  // SSE handlers are created once per run and close over stale state, so
  // the live values they need are mirrored into refs.
  const pausedRef = useRef(false);
  useEffect(() => { pausedRef.current = paused; }, [paused]);
  const speedFactor = (SPEEDS.find(s => s.id === speedId) || SPEEDS[1]).factor;
  const speedRef = useRef(1);
  useEffect(() => { speedRef.current = speedFactor; }, [speedFactor]);

  // Wall-clock of the last SSE event, used to tell "the day is just slow"
  // apart from "the stream died".
  const lastEventAtRef = useRef(Date.now());
  // Active "view" on mobile (≤768px). On desktop the CSS ignores this and
  // shows all three columns. On mobile we only render one at a time and
  // use back/console buttons in ChatColumn + AdminConsole to navigate.
  const [mobileView, setMobileView] = useState('chats');

  // Wall-clock ms when the next resident message is allowed to render, and
  // the fictional-minute timestamp of the most recently scheduled render.
  // The message_sent SSE handler reads/writes both to derive each new
  // render's gap from the fictional-time delta — bursts stay tight, lulls
  // breathe — instead of pacing every message at a fixed cadence.
  const nextRenderSlotRef = useRef(0);
  const lastRenderedFicMinRef = useRef(null);
  const renderTimersRef = useRef(new Set());
  const queuedMessagesRef = useRef([]);
  useEffect(() => { queuedMessagesRef.current = queuedMessages; }, [queuedMessages]);

  useEffect(() => {
    return () => {
      for (const id of renderTimersRef.current) clearTimeout(id);
      renderTimersRef.current.clear();
    };
  }, [state?.run_id]);

  useEffect(() => {
    setQueuedMessages([]);
    nextRenderSlotRef.current = 0;
    lastRenderedFicMinRef.current = null;
  }, [state?.run_id]);

  // Watchdog: while a day is in progress, poll the API every 5s and merge
  // anything we don't yet have. SSE is the fast path, but it dies silently
  // on mobile Safari (tab backgrounded, device sleeps, network blip) and
  // also under Heroku H18 / corporate proxies. Without this, the UI sits
  // frozen on the last event we received until the user refreshes.
  // Returning prev unchanged when nothing's new avoids wasted re-renders.
  useEffect(() => {
    if (!state?.run_id || !working) return;
    const runId = state.run_id;
    const id = setInterval(() => {
      api.getRun(runId).then(fresh => {
        setState(prev => {
          if (!prev) return fresh;
          const have = new Set(prev.messages.map(m => m.id));
          const queuedIds = new Set(queuedMessagesRef.current.map(q => q.id));
          const extras = fresh.messages.filter(m => !have.has(m.id) && !queuedIds.has(m.id));
          const motionsChanged =
            (prev.motions?.length || 0) !== (fresh.motions?.length || 0);
          const clockChanged =
            prev.clock.minutes_since_start !== fresh.clock.minutes_since_start;
          if (extras.length === 0 && !motionsChanged && !clockChanged) return prev;
          const merged = extras.length
            ? mergeMessagesChronologically(prev.messages, extras)
            : prev.messages;
          return { ...fresh, messages: merged };
        });

        if (fresh.ended) {
          setWorking(false);
          setNextAdvanceAt(null);
          return;
        }
        // Only force-unstick on a genuinely dead stream.
        //
        // The old check was "clock >= day_end_minutes → the day is over".
        // But the scheduler sets the clock to day-end *before* memory
        // consolidation and *before* releasing the run lock, so that test
        // fired every time — clearing `working` mid-day and POSTing an
        // advance that the backend answered with 409. Waiting for real SSE
        // silence keeps the recovery path without the false positives.
        const silentFor = Date.now() - lastEventAtRef.current;
        if (silentFor > SSE_STALL_MS) {
          setWorking(false);
          if (!pausedRef.current) {
            setNextAdvanceAt(Date.now() + AUTO_ADVANCE_DELAY_MS);
          }
        }
      }).catch(() => {});
    }, 5000);
    return () => clearInterval(id);
  }, [state?.run_id, working]);

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

  // Kick off day 1 immediately when a run is loaded. Later days also chain
  // immediately after the backend publishes day_done.
  useEffect(() => {
    if (!state?.run_id || state.ended || working || paused) return;
    setNextAdvanceAt(Date.now());
  }, [state?.run_id]);

  // Pausing cancels a pending advance; resuming re-arms it. A day already
  // running on the server finishes either way — pause governs whether the
  // NEXT day starts, which is what stops the clock (and the spend).
  useEffect(() => {
    if (paused) {
      setNextAdvanceAt(null);
      return;
    }
    if (!state?.run_id || state.ended || working) return;
    setNextAdvanceAt(Date.now() + AUTO_ADVANCE_DELAY_MS);
  }, [paused]);

  // Fire the auto-advance once its scheduled time elapses.
  useEffect(() => {
    if (!nextAdvanceAt || paused) return;
    const remaining = nextAdvanceAt - Date.now();
    const fire = () => {
      setNextAdvanceAt(null);
      onAdvanceRef.current?.();
    };
    if (remaining <= 0) { fire(); return; }
    const id = setTimeout(fire, remaining);
    return () => clearTimeout(id);
  }, [nextAdvanceAt, paused]);

  // Ref so SSE handlers always see the latest selected chat without needing
  // to re-subscribe when the selection changes.
  const selectedChatIdRef = useRef(null);
  useEffect(() => { selectedChatIdRef.current = selectedChatId; }, [selectedChatId]);

  // Clear unread count when a chat is opened.
  const handleSelectChat = useCallback((chatId) => {
    setSelectedChatId(chatId);
    setPendingDmRecipient(null);
    setMobileView('messages');
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
      setMobileView('messages');
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
    let es = null;

    // Refetch the run and merge any messages we missed. Called both on
    // every SSE (re)open and whenever the tab regains visibility.
    const refetchAndMerge = () => {
      api.getRun(runId).then(fresh => {
        setState(prev => {
          if (!prev) return fresh;
          const queuedIds = new Set(queuedMessagesRef.current.map(q => q.id));
          const visibleFresh = fresh.messages.filter(m =>
            !queuedIds.has(m.id) || prev.messages.some(pm => pm.id === m.id)
          );
          return { ...fresh, messages: mergeMessagesChronologically(prev.messages, visibleFresh) };
        });
      }).catch(() => {});
    };

    // Render a message_sent payload into state. Pure side-effect — same
    // logic for both the immediate path (admin) and the delayed path
    // (residents). Re-checking dedup inside setState is important because
    // the optimistic-merge or a watchdog refetch may have already inserted
    // this message id between event arrival and our scheduled render.
    const renderIncomingMessage = (payload) => {
      const msg = payload.message;
      let isNewToState = false;
      setQueuedMessages(prev => prev.filter(q => q.id !== msg.id));
      setState(prev => {
        if (!prev) return prev;
        if (prev.messages.some(m => m.id === msg.id)) return prev;
        isNewToState = true;
        const chats = payload.chat && !prev.chats.some(c => c.id === payload.chat.id)
          ? [...prev.chats, payload.chat]
          : prev.chats;
        return { ...prev, messages: mergeMessagesChronologically(prev.messages, [{ ...msg, isNew: true }]), chats };
      });
      if (isNewToState
          && msg.sender_kind !== 'admin'
          && msg.chat_id !== selectedChatIdRef.current) {
        setUnreadByChat(prev => ({
          ...prev,
          [msg.chat_id]: (prev[msg.chat_id] || 0) + 1,
        }));
      }
    };

    // Wire all listeners onto a fresh EventSource. We close+recreate (vs.
    // letting the browser auto-reconnect) on visibilitychange and on hard
    // errors, because mobile Safari's native auto-reconnect is unreliable
    // — readyState often stays OPEN on a dead socket after sleep/wake.
    const connect = () => {
      if (es) es.close();
      es = new EventSource(`${BACKEND}/api/runs/${runId}/events`);

      // Every listener stamps the last-traffic clock, so the watchdog can
      // distinguish "this day is just slow" from "the stream is dead"
      // instead of force-unsticking the UI mid-day.
      const on = (name, fn) => es.addEventListener(name, (e) => {
        lastEventAtRef.current = Date.now();
        fn(e);
      });

      // Every (re)open refetches the run so we catch anything the stream
      // missed while disconnected.
      es.addEventListener('open', () => {
        lastEventAtRef.current = Date.now();
        refetchAndMerge();
      });

      on('message_sent', (e) => {
        const payload = JSON.parse(e.data).data;
        const msg = payload.message;
        // Admin's own send: render instantly (their action, no need to pace).
        if (msg.sender_kind === 'admin') {
          lastRenderedFicMinRef.current = msg.fictional_timestamp_minutes;
          renderIncomingMessage(payload);
          return;
        }
        // Resident sends: pace each render by the fictional-time delta
        // since the last rendered message. Tight bursts (1–5 fic min apart)
        // hit the floor; long lulls (30+ fic min) hit the cap. The slot ref
        // absorbs real wall-clock delays — if SSE was idle long enough that
        // the gap already elapsed, the next message renders immediately.
        const now = Date.now();
        const speed = speedRef.current;
        if (!Number.isFinite(speed)) {
          // ⚡ instant: skip pacing entirely.
          lastRenderedFicMinRef.current = msg.fictional_timestamp_minutes;
          nextRenderSlotRef.current = now;
          renderIncomingMessage(payload);
          return;
        }
        const lastFic = lastRenderedFicMinRef.current;
        const ficDelta = lastFic == null
          ? 0
          : Math.max(0, msg.fictional_timestamp_minutes - lastFic);
        const gap = Math.max(
          MIN_RENDER_GAP_MS,
          Math.min(ficDelta * REAL_MS_PER_FIC_MIN, MAX_RENDER_GAP_MS),
        ) / speed;
        // Clamp the queue: never schedule further than MAX_QUEUE_LAG_MS out.
        // Pacing is a nicety; falling minutes behind the server is not.
        const renderAt = Math.min(
          Math.max(now, nextRenderSlotRef.current),
          now + MAX_QUEUE_LAG_MS,
        );
        nextRenderSlotRef.current = renderAt + gap;
        lastRenderedFicMinRef.current = msg.fictional_timestamp_minutes;
        const delay = renderAt - now;
        if (delay <= 0) {
          renderIncomingMessage(payload);
        } else {
          setQueuedMessages(prev => {
            if (prev.some(q => q.id === msg.id)) return prev;
            return [...prev, {
              id: msg.id,
              chat_id: msg.chat_id,
              sender_id: msg.sender_id,
              display_name: msg.sender_display_name,
              render_at: renderAt,
            }];
          });
          const timerId = setTimeout(() => {
            renderTimersRef.current.delete(timerId);
            renderIncomingMessage(payload);
          }, delay);
          renderTimersRef.current.add(timerId);
        }
      });

      on('typing_start', (e) => {
        const d = JSON.parse(e.data).data;
        setTypingAgents(prev => ({ ...prev, [d.agent_id]: d.display_name }));
      });

      on('typing_stop', (e) => {
        const d = JSON.parse(e.data).data;
        setTypingAgents(prev => {
          const next = { ...prev };
          delete next[d.agent_id];
          return next;
        });
      });

      on('day_start', (e) => {
        setDayError(null);
        setNextAdvanceAt(null);
      });

      on('day_end', (e) => {
        setTypingAgents({});
        // NB: do NOT chain the next day or clear `working` here. day_end fires
        // mid-lifecycle while the per-run lock is still held (memory
        // consolidation is still running). Chaining lives in the day_done
        // handler below, which fires AFTER the lock is released.
      });

      on('day_done', (e) => {
        const d = JSON.parse(e.data).data;
        setWorking(false);
        // Refresh authoritative state (clock, trust, motions, agent.notes
        // cleared by memory consolidation). Preserve any messages we already
        // streamed in via SSE so we don't drop ones the server hasn't yet
        // round-tripped.
        api.getRun(runId).then(fresh => {
          setState(prev => {
            if (!prev) return fresh;
            const queuedIds = new Set(queuedMessagesRef.current.map(q => q.id));
            const visibleFresh = fresh.messages.filter(m =>
              !queuedIds.has(m.id) || prev.messages.some(pm => pm.id === m.id)
            );
            return { ...fresh, messages: mergeMessagesChronologically(prev.messages, visibleFresh) };
          });
          if (!d.ok) {
            setDayError('Errore: il giorno non è terminato correttamente.');
            return;
          }
          // Don't chain while paused — resuming re-schedules it.
          if (!fresh.ended && !pausedRef.current) {
            setNextAdvanceAt(Date.now() + AUTO_ADVANCE_DELAY_MS);
          }
        }).catch(() => {});
      });

      // The backend hit a spend cap and stopped the run on purpose.
      on('run_ended', (e) => {
        const d = JSON.parse(e.data).data;
        setWorking(false);
        setNextAdvanceAt(null);
        setState(prev => prev
          ? { ...prev, ended: true, ended_reason: d.reason }
          : prev);
      });

      on('motion_filed', (e) => {
        const d = JSON.parse(e.data).data;
        setState(prev => {
          if (!prev) return prev;
          if ((prev.motions || []).some(m => m.id === d.motion.id)) return prev;
          return { ...prev, motions: [...(prev.motions || []), d.motion] };
        });
      });

      on('vote_cast', (e) => {
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

      on('motion_closed', (e) => {
        const d = JSON.parse(e.data).data;
        setState(prev => {
          if (!prev) return prev;
          return {
            ...prev,
            motions: (prev.motions || []).map(m => m.id === d.motion.id ? d.motion : m),
          };
        });
        // Pull fresh state (trust matrix + dials updated server-side)
        api.getRun(runId).then(fresh => {
          setState(prev => {
            if (!prev) return fresh;
            const queuedIds = new Set(queuedMessagesRef.current.map(q => q.id));
            const visibleFresh = fresh.messages.filter(m =>
              !queuedIds.has(m.id) || prev.messages.some(pm => pm.id === m.id)
            );
            return { ...fresh, messages: mergeMessagesChronologically(prev.messages, visibleFresh) };
          });
        }).catch(() => {});
      });

      on('reaction_added', (e) => {
        const d = JSON.parse(e.data).data;
        setState(prev => {
          if (!prev) return prev;
          return {
            ...prev,
            messages: prev.messages.map(m =>
              m.id === d.message_id ? { ...m, reactions: d.reactions || {} } : m
            ),
          };
        });
      });

      on('memory_consolidation_done', () => {
        setMemoryRefreshSeq(n => n + 1);
      });

      on('trust_updated', () => {
        // Re-fetch run for the authoritative trust matrix
        api.getRun(runId).then(fresh => {
          setState(prev => prev ? { ...prev, trust: fresh.trust } : prev);
        }).catch(() => {});
      });

      es.addEventListener('error', (e) => {
        console.error('SSE error event', e);
      });

      es.onerror = (err) => {
        // Browser will auto-retry transient drops. If the socket has been
        // CLOSED (no auto-reconnect coming), recreate explicitly so we
        // don't sit on a dead stream.
        console.warn('EventSource error', err);
        if (es && es.readyState === 2 /* CLOSED */) {
          setTimeout(connect, 1000);
        }
      };
    };

    // When the tab becomes visible again, refetch immediately and force a
    // fresh SSE connection. Mobile Safari frequently kills the socket
    // silently when the device sleeps or the tab backgrounds; readyState
    // can still report OPEN on a dead stream, so explicit reconnect is
    // the only reliable recovery.
    const onVisibility = () => {
      if (document.visibilityState !== 'visible') return;
      refetchAndMerge();
      connect();
    };

    connect();
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
      if (es) es.close();
    };
  }, [state?.run_id]);

  // Typing indicator shows on the main chat for now (we can't know which chat
  // an agent is composing for until the message lands).
  const typingByChat = useMemo(() => {
    const names = Object.values(typingAgents);
    return names.length > 0 ? { main: names } : {};
  }, [typingAgents]);

  const queuedByChat = useMemo(() => {
    const out = {};
    for (const q of queuedMessages) {
      if (!q.chat_id) continue;
      if (!out[q.chat_id]) out[q.chat_id] = [];
      out[q.chat_id].push(q.display_name || 'Qualcuno');
    }
    return out;
  }, [queuedMessages]);

  const onAdvance = useCallback(async () => {
    if (!state || working || paused || state.ended) return;
    setNextAdvanceAt(null);
    setWorking(true);
    setDayError(null);
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
      if (e?.status === 409) return;
      setDayError('Errore: ' + String(e));
      setWorking(false);
    }
  }, [state, working, paused]);

  useEffect(() => { onAdvanceRef.current = onAdvance; }, [onAdvance]);

  const onFileMotion = useCallback(async (title, description) => {
    if (!state) return;
    await api.fileMotion(state.run_id, title, description);
  }, [state]);

  const onCloseMotion = useCallback(async (motionId) => {
    if (!state) return;
    await api.closeMotion(state.run_id, motionId);
  }, [state]);

  // Optimistically merge the POST response's message into state so the
  // admin's own send always appears in the UI, even if the message_sent SSE
  // event is dropped (Heroku H18, mid-reconnect, queue removed during gap).
  const mergeOwnMessage = useCallback((result) => {
    const msg = result?.message;
    if (!msg) return;
    const chat = result?.chat;
    setState(prev => {
      if (!prev) return prev;
      if (prev.messages.some(m => m.id === msg.id)) return prev;
      const chats = chat && !prev.chats.some(c => c.id === chat.id)
        ? [...prev.chats, chat]
        : prev.chats;
      return { ...prev, messages: mergeMessagesChronologically(prev.messages, [msg]), chats };
    });
  }, []);

  const onSendAnnounce = useCallback(async (text) => {
    if (!state) return;
    const result = await api.announce(state.run_id, text);
    mergeOwnMessage(result);
  }, [state?.run_id, mergeOwnMessage]);

  const onSendDm = useCallback(async (recipientId, text) => {
    if (!state) return;
    const result = await api.sendDm(state.run_id, recipientId, text);
    mergeOwnMessage(result);
  }, [state?.run_id, mergeOwnMessage]);

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

  const messagesToday = state.messages.filter(m => m.day === state.clock.day).length;
  const spentUsd = Number(state.metrics?.estimated_cost_usd || 0);
  let topbarSub;
  if (state.ended) {
    topbarSub = 'Partita conclusa';
  } else if (paused) {
    topbarSub = 'In pausa · il tempo è fermo, nessuna spesa in corso';
  } else if (working) {
    topbarSub = 'Residenti attivi · puoi scrivere quando vuoi';
  } else if (nextAdvanceAt) {
    topbarSub = 'Residenti in arrivo...';
  } else {
    topbarSub = `${messagesToday} messaggi oggi · scrivi nel gruppo o in privato`;
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
            <div className="topbar-sub">
              <span className="topbar-day">Giorno {state.clock.day}</span>
              {' · '}{topbarSub}
            </div>
          </div>
        </div>
        <div className="topbar-right">
          {!state.ended && (
            <>
              <button
                className={`playback-btn ${paused ? 'resumed' : ''}`}
                onClick={() => setPaused(p => !p)}
                title={paused ? 'Riprendi il tempo' : 'Metti in pausa il tempo'}
                aria-label={paused ? 'Riprendi' : 'Pausa'}
              >{paused ? '▶ Riprendi' : '⏸ Pausa'}</button>
              <div className="speed-group" role="group" aria-label="Velocità">
                {SPEEDS.map(s => (
                  <button
                    key={s.id}
                    type="button"
                    className={`speed-btn ${speedId === s.id ? 'active' : ''}`}
                    onClick={() => setSpeedId(s.id)}
                    title={s.id === 'subito' ? 'Mostra i messaggi appena arrivano' : `Velocità ${s.label}`}
                  >{s.label}</button>
                ))}
              </div>
            </>
          )}
          <span className="topbar-cost" title="Spesa stimata per questa partita">
            ${spentUsd.toFixed(3)}
          </span>
          <button
            className="help-btn"
            onClick={() => setShowHelp(true)}
            title="Come si gioca"
            aria-label="Come si gioca"
          >?</button>
        </div>
      </div>
      {state.ended && (
        <div className="run-ended-bar">
          {END_REASON_TEXT[state.ended_reason]
            || 'Partita conclusa. Ricarica la pagina per iniziarne una nuova.'}
        </div>
      )}
      {dayError && <div className="day-status-bar">{dayError}</div>}
      <div className="main" data-mobile-view={mobileView}>
        <LeftPanel
          state={state}
          selectedChatId={selectedChatId}
          onSelectChat={handleSelectChat}
          onStartDm={handleStartDm}
          unreadByChat={unreadByChat}
          typingByChat={typingByChat}
          queuedByChat={queuedByChat}
          onOpenProfile={setProfileAgentId}
        />
        <ChatColumn
          state={state}
          selectedChatId={selectedChatId}
          pendingChat={pendingDmChat}
          typingByChat={typingByChat}
          queuedByChat={queuedByChat}
          suggestions={suggestions}
          onSendAnnounce={onSendAnnounce}
          onSendDm={onSendDm}
          onOpenProfile={setProfileAgentId}
          onMobileBack={() => setMobileView('chats')}
          onOpenConsole={() => setMobileView('admin')}
        />
        <AdminConsole
          state={state}
          onFileMotion={onFileMotion}
          onCloseMotion={onCloseMotion}
          suggestions={suggestions}
          onMobileBack={() =>
            setMobileView(selectedChatId ? 'messages' : 'chats')
          }
        />
      </div>
      {showHelp && <HelpModal onClose={dismissHelp} />}
      {profileAgentId && (
        <ProfileModal
          state={state}
          agentId={profileAgentId}
          onClose={() => setProfileAgentId(null)}
          onOpenChat={(chatId) => { setSelectedChatId(chatId); setProfileAgentId(null); }}
          onGoalSaved={(aid, goal) => {
            setState(prev => prev ? {
              ...prev,
              agents: prev.agents.map(a =>
                a.persona.id === aid ? { ...a, admin_goal: goal } : a
              ),
            } : prev);
          }}
          memoryRefreshSeq={memoryRefreshSeq}
        />
      )}
    </div>
  );
}
