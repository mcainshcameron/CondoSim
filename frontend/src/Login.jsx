import { useState } from 'react';
import { api } from './api.js';

export default function Login({ configured, onAuthed }) {
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!password) return;
    setLoading(true);
    setError('');
    try {
      await api.login(password);
      onAuthed();
    } catch (err) {
      const status = err?.status;
      if (status === 401) setError('Password errata.');
      else if (status === 429) setError('Troppi tentativi. Riprova tra un minuto.');
      else if (status === 503) setError('Auth non configurata sul server.');
      else setError('Errore: ' + String(err?.message || err));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-screen">
      <form className="login-card" onSubmit={submit}>
        <div className="login-logo">🏢</div>
        <h1>Condominio Via Garibaldi</h1>
        <p className="login-sub">Accesso amministratore</p>
        {!configured && (
          <div className="login-warn">
            ⚠️ Auth non configurata: imposta <code>ADMIN_PASSWORD</code> e
            <code> SESSION_SECRET</code> nelle variabili d'ambiente del server.
          </div>
        )}
        <input
          type="password"
          placeholder="Password"
          value={password}
          onChange={e => setPassword(e.target.value)}
          autoFocus
          autoComplete="current-password"
          disabled={loading || !configured}
        />
        <button type="submit" disabled={loading || !configured || !password}>
          {loading ? 'Accesso…' : 'Entra'}
        </button>
        {error && <div className="login-error">{error}</div>}
      </form>
    </div>
  );
}
