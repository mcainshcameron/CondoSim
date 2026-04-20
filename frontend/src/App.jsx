import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from './api.js';

const BACKEND = 'http://127.0.0.1:8001';

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

function Setup({ onCreated }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [existing, setExisting] = useState([]);
  const [openingText, setOpeningText] = useState('');

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

  return (
    <div className="setup">
      <h1>Condominio Via Garibaldi</h1>
      <p>
        Sei l'amministratore. Scegli il messaggio di apertura che vuoi inviare
        al gruppo principale — puoi cambiarlo completamente, raccontare la crisi
        che vuoi tu, e da lì guidare la conversazione.
      </p>
      <textarea
        className="setup-textarea"
        value={openingText}
        onChange={e => setOpeningText(e.target.value)}
        rows={8}
        placeholder="Il tuo messaggio di apertura al condominio…"
      />
      <button className="btn" onClick={handleCreate} disabled={loading || !openingText.trim()}>
        {loading ? 'Un momento…' : 'Avvia partita'}
      </button>
      {existing.length > 0 && (
        <div style={{ marginTop: 16, textAlign: 'center' }}>
          <div style={{ color: '#667781', fontSize: 13, marginBottom: 6 }}>Oppure riprendi:</div>
          {existing.map(id => (
            <div key={id} style={{ marginBottom: 4 }}>
              <button className="btn" style={{ background: '#888', fontSize: 13 }} onClick={() => handleLoad(id)}>
                {id}
              </button>
            </div>
          ))}
        </div>
      )}
      {error && <div style={{ color: 'var(--danger)' }}>{error}</div>}
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

  useEffect(() => {
    setGoalDraft(agent?.admin_goal || '');
  }, [agent?.admin_goal, agentId]);

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
              <div className="observer-label">Brief del proprietario</div>
              <div className="observer-brief">{agent.owner.brief_text}</div>
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

function LeftPanel({ state, onSelectChat, godView, onOpenProfile }) {
  // Compute DM partner counts + trust edges from current state
  const nameById = useMemo(() => {
    const m = {};
    for (const a of state.agents) m[a.persona.id] = a.persona.display_name;
    m['admin'] = 'Amministratore';
    return m;
  }, [state.agents]);

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
            <div className="resident-row">
              <div>
                <span className="name">{a.persona.display_name}</span>
                {a.admin_goal && <span className="goal-badge" title="Ha un obiettivo aggiuntivo">🎯</span>}
                <span className="unit">int. {a.persona.unit} · {a.persona.millesimi}mill.</span>
              </div>
              {chatId && (
                <button
                  className="resident-chat-btn"
                  onClick={(e) => { e.stopPropagation(); onSelectChat(chatId); }}
                  title="Apri chat"
                >💬</button>
              )}
            </div>
            <div className="desc">{a.persona.public_description}</div>
            {msgsToday > 0 && (
              <div className="resident-today">{msgsToday} msg oggi</div>
            )}
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

function ChatColumn({ state, selectedChatId, onSelect, typingByChat, godView }) {
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

  const selected = chats.find(c => c.id === selectedChatId) || chats[0];
  const selectedMsgs = selected ? (msgsByChat.get(selected.id) || []) : [];
  const scrollRef = useRef(null);

  // Auto-scroll to bottom when new messages arrive
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [selectedMsgs.length, selected?.id]);

  const typingNames = selected ? (typingByChat[selected.id] || []) : [];

  return (
    <div className="chat-col">
      <div className="chat-tabs">
        {chats.map(c => {
          const count = msgsByChat.get(c.id)?.length || 0;
          const typing = (typingByChat[c.id] || []).length;
          return (
            <div
              key={c.id}
              className={`chat-tab ${selected?.id === c.id ? 'active' : ''}`}
              onClick={() => onSelect(c.id)}
            >
              {c.display_name}
              <span className="count">{count}</span>
              {typing > 0 && <span className="typing-dot" title={`${typing} sta scrivendo`}>•</span>}
            </div>
          );
        })}
      </div>
      <div className="chat-messages" ref={scrollRef}>
        {selectedMsgs.length === 0 ? (
          <div className="chat-empty">Nessun messaggio ancora.</div>
        ) : (
          selectedMsgs.map(m => (
            <div
              key={m.id}
              className={`msg ${m.sender_kind === 'admin' ? 'admin' : m.sender_kind === 'external' ? 'external' : ''} ${m.isNew ? 'fade-in' : ''}`}
            >
              <div className="sender">{m.sender_display_name}</div>
              <div className="content">{m.content}</div>
              <div className="ts">{formatItalianDateTime(state.fictional_start_iso, m.fictional_timestamp_minutes)}</div>
            </div>
          ))
        )}
        {typingNames.length > 0 && (
          <div className="typing-indicator">
            {typingNames.join(', ')} {typingNames.length === 1 ? 'sta scrivendo' : 'stanno scrivendo'}
            <span className="dots"><span>.</span><span>.</span><span>.</span></span>
          </div>
        )}
      </div>
    </div>
  );
}

function AdminConsole({ state, onAction, working, onAdvance, onFileMotion, onCloseMotion, dayComplete }) {
  const [announceText, setAnnounceText] = useState('');
  const [dmText, setDmText] = useState('');
  const [dmRecipient, setDmRecipient] = useState(state.agents[0]?.persona.id || '');
  const [motionTitle, setMotionTitle] = useState('');
  const [motionDesc, setMotionDesc] = useState('');
  const [status, setStatus] = useState('');
  const [templates, setTemplates] = useState({ actions: [], motion_templates: [] });
  const [suggestions, setSuggestions] = useState([]);

  useEffect(() => {
    fetch(`${BACKEND}/api/quick_actions`).then(r => r.json()).then(setTemplates).catch(() => {});
  }, []);

  // Re-fetch suggestions whenever day/state/motions change
  useEffect(() => {
    fetch(`${BACKEND}/api/runs/${state.run_id}/suggestions`)
      .then(r => r.json())
      .then(d => setSuggestions(d.suggestions || []))
      .catch(() => {});
  }, [state.run_id, state.clock.day, (state.motions || []).length]);

  const useSuggestion = (sug) => {
    if (sug.action === 'close_motion' && sug.motion_id) {
      onCloseMotion(sug.motion_id);
      setStatus('Votazione chiusa.');
      return;
    }
    setAnnounceText(sug.body || '');
    setStatus('Suggerimento caricato — puoi modificarlo prima di pubblicare.');
  };

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

  const handleAnnounce = async () => {
    if (!announceText.trim()) return;
    await doAction(async () => {
      await api.announce(state.run_id, announceText);
      setAnnounceText('');
    }, 'Avviso pubblicato.');
  };

  const handleDm = async () => {
    if (!dmText.trim() || !dmRecipient) return;
    await doAction(async () => {
      await api.sendDm(state.run_id, dmRecipient, dmText);
      setDmText('');
    }, 'Messaggio privato inviato.');
  };

  const handleFileMotion = async () => {
    if (!motionTitle.trim() || !motionDesc.trim()) return;
    await doAction(async () => {
      await onFileMotion(motionTitle, motionDesc);
      setMotionTitle('');
      setMotionDesc('');
    }, 'Mozione depositata.');
  };

  const handleAdvance = async () => {
    if (onAdvance) onAdvance();
  };

  const openMotions = (state.motions || []).filter(m => m.status === 'open');
  const closedMotions = (state.motions || []).filter(m => m.status !== 'open').slice(-5);

  const speedChips = suggestions.filter(s => !s.action); // pure text suggestions
  const actionChips = suggestions.filter(s => s.action === 'close_motion');

  return (
    <div className="console">
      {/* Manual day advance — prominent when the day is complete */}
      <div className={`console-section advance-panel ${dayComplete ? 'active' : ''}`}>
        <div className="advance-label">
          {dayComplete
            ? `✅ Giorno ${state.clock.day} concluso`
            : working
              ? `⏳ Giorno ${state.clock.day} in corso…`
              : `Giorno ${state.clock.day}`}
        </div>
        <button
          className="btn advance-btn"
          onClick={handleAdvance}
          disabled={working}
        >
          {working ? 'Un momento…' : `Fai passare il giorno →`}
        </button>
      </div>

      {/* Avviso + inline suggestions */}
      <div className="console-section">
        <h2>Avviso al condominio</h2>
        {speedChips.length > 0 && (
          <div className="template-row">
            {speedChips.map(s => (
              <button
                key={s.id}
                className="template-chip"
                title={s.body || s.label}
                onClick={() => useSuggestion(s)}
              >{s.label}</button>
            ))}
          </div>
        )}
        <textarea
          placeholder="Scrivi un avviso al gruppo principale…"
          value={announceText}
          onChange={e => setAnnounceText(e.target.value)}
        />
        <button className="btn" onClick={handleAnnounce} disabled={!announceText.trim()}>
          Pubblica avviso
        </button>
      </div>

      {/* DM — compact */}
      <div className="console-section">
        <h2>Messaggio privato</h2>
        <select value={dmRecipient} onChange={e => setDmRecipient(e.target.value)}>
          {state.agents.map(a => (
            <option key={a.persona.id} value={a.persona.id}>
              {a.persona.display_name} (int. {a.persona.unit})
            </option>
          ))}
        </select>
        <textarea
          placeholder="DM al residente…"
          value={dmText}
          onChange={e => setDmText(e.target.value)}
          style={{ minHeight: 44 }}
        />
        <button className="btn" onClick={handleDm} disabled={!dmText.trim()}>
          Invia DM
        </button>
      </div>

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
                onClick={() => useSuggestion(s)}
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

export default function App() {
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
  const [dayComplete, setDayComplete] = useState(false);

  // Subscribe to SSE on run load
  useEffect(() => {
    if (!state?.run_id) return;
    const es = new EventSource(`${BACKEND}/api/runs/${state.run_id}/events`);

    es.addEventListener('message_sent', (e) => {
      const payload = JSON.parse(e.data).data;
      const msg = payload.message;
      setState(prev => {
        if (!prev) return prev;
        if (prev.messages.some(m => m.id === msg.id)) return prev;
        // Also merge any new chat if this is a freshly-created DM/group
        const chats = payload.chat && !prev.chats.some(c => c.id === payload.chat.id)
          ? [...prev.chats, payload.chat]
          : prev.chats;
        return { ...prev, messages: [...prev.messages, { ...msg, isNew: true }], chats };
      });
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
      setDayComplete(false);
    });

    es.addEventListener('day_end', (e) => {
      const d = JSON.parse(e.data).data;
      setDayStatus(`Giorno ${d.day} concluso: ${d.activations} attivazioni, ${d.total_messages} messaggi totali.`);
      setDayComplete(true);
      api.getRun(state.run_id).then(setState).catch(() => {});
      setTypingAgents({});
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

  const refresh = useCallback(async () => {
    if (!state) return;
    const fresh = await api.getRun(state.run_id);
    setState(prev => ({ ...fresh, messages: fresh.messages })); // clear isNew flags
  }, [state]);

  const onAdvance = useCallback(async () => {
    if (!state || working) return;
    setWorking(true);
    setDayComplete(false);
    setDayStatus(`Avvio giorno ${state.clock.day}…`);
    try {
      await api.advanceDay(state.run_id);
    } catch (e) {
      setDayStatus('Errore: ' + String(e));
    } finally {
      setWorking(false);
    }
  }, [state, working]);

  const onAdminMessage = useCallback(async () => {
    if (!state) return;
    await refresh();
  }, [state, refresh]);

  const onFileMotion = useCallback(async (title, description) => {
    if (!state) return;
    await api.fileMotion(state.run_id, title, description);
  }, [state]);

  const onCloseMotion = useCallback(async (motionId) => {
    if (!state) return;
    await api.closeMotion(state.run_id, motionId);
  }, [state]);

  if (!state) return <Setup onCreated={setState} />;

  const displayDate = formatItalianDateTime(state.fictional_start_iso, state.clock.minutes_since_start);

  return (
    <div className="app">
      <div className="topbar">
        <div>
          <h1>Condominio Via Garibaldi</h1>
        </div>
        <div className="fictional-clock" title="Data e ora nel mondo del palazzo">
          <div className="fictional-clock-label">Ora nel palazzo</div>
          <div className="fictional-clock-value">🕒 {displayDate}</div>
        </div>
        <div className="topbar-right">
          <label className="god-view-toggle" title="Mostra tutte le chat (incluse le DM tra altri residenti). Spegni per gioco fedele alla finzione.">
            <input
              type="checkbox"
              checked={godView}
              onChange={e => setGodView(e.target.checked)}
            />
            <span>👁️ Osservatore</span>
          </label>
          <div className="day-badge">Giorno {state.clock.day}</div>
        </div>
      </div>
      {dayStatus && <div className="day-status-bar">{dayStatus}</div>}
      <div className="main">
        <LeftPanel
          state={state}
          onSelectChat={setSelectedChatId}
          godView={godView}
          onOpenProfile={setProfileAgentId}
        />
        <ChatColumn
          state={state}
          selectedChatId={selectedChatId}
          onSelect={setSelectedChatId}
          typingByChat={typingByChat}
          godView={godView}
        />
        <AdminConsole
          state={state}
          onAction={onAdminMessage}
          working={working}
          onAdvance={onAdvance}
          onFileMotion={onFileMotion}
          onCloseMotion={onCloseMotion}
          dayComplete={dayComplete}
        />
      </div>
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
