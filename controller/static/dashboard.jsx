const { useState, useEffect, useRef, useCallback, useMemo } = React;

// ─── Ingress ──────────────────────────────────────────────────────────────────

// Under Home Assistant Ingress the dashboard is mounted below a generated
// path (e.g. /api/hassio_ingress/<token>/), not at the site root — the
// server injects a matching <base href> (see em_api.py's
// _with_ingress_base). Every absolute "/api/..." path in this file has to
// become relative through this so it resolves under that base instead of
// bypassing it straight to the root. A no-op outside ingress, where the
// page's own URL already is the root.
function ingressPath(path) {
  return path.startsWith('/') ? `.${path}` : path;
}

// True when the page is being served through Home Assistant's ingress
// gateway. Read off the injected <base href> rather than /api/system/status's
// ha_ingress, so it is available synchronously and to code with no API token
// — the WebUSB check runs before any of that.
function isIngress() {
  return document.baseURI.includes('/hassio_ingress/');
}

function ingressWebSocketUrl(path) {
  const url = new URL(ingressPath(path), document.baseURI);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}

// ─── API ──────────────────────────────────────────────────────────────────────

const API = {
  token: null,
  role: null,

  // Set by App to its logout handler. Every 401 below routes through
  // `unauthorized()` rather than only the call sites that remember to check,
  // because the ones that forget do not fail visibly — they retry.
  //
  // A left-open tab did exactly that on the EA controller (#359): the 5s poll
  // caught its own 401 and discarded it, so an expired session produced 1262
  // consecutive 401s over 13 hours, every 5 seconds, with nothing on screen
  // saying the session had died. It also cost 21% of the controller's log
  // ring, which is the half that hurts someone else — a support bundle
  // collected in that state reaches back hours less far.
  onUnauthorized: null,
  _unauthFired: false,

  // Guarded, because every in-flight request 401s at once and the logout
  // handler's own POST /api/auth/logout would re-enter this.
  unauthorized() {
    if (this._unauthFired) return;
    this._unauthFired = true;
    if (this.onUnauthorized) this.onUnauthorized();
  },

  headers() {
    const h = { 'Content-Type': 'application/json' };
    if (this.token) h['Authorization'] = `Bearer ${this.token}`;
    return h;
  },

  async get(path) {
    const r = await fetch(ingressPath(path), { headers: this.headers() });
    if (r.status === 401) { API.unauthorized(); throw { code: 'not_authenticated', status: 401 }; }
    const data = await r.json();
    if (!r.ok) throw data;
    return data;
  },

  async post(path, body) {
    const r = await fetch(ingressPath(path), { method: 'POST', headers: this.headers(), body: JSON.stringify(body) });
    if (r.status === 401) { API.unauthorized(); throw { code: 'not_authenticated', status: 401 }; }
    const data = await r.json();
    if (!r.ok) throw data;
    return data;
  },

  async patch(path, body) {
    const r = await fetch(ingressPath(path), { method: 'PATCH', headers: this.headers(), body: JSON.stringify(body) });
    if (r.status === 401) { API.unauthorized(); throw { code: 'not_authenticated', status: 401 }; }
    const data = await r.json();
    if (!r.ok) throw data;
    return data;
  },

  async del(path) {
    const r = await fetch(ingressPath(path), { method: 'DELETE', headers: this.headers() });
    if (r.status === 401) { API.unauthorized(); throw { code: 'not_authenticated', status: 401 }; }
    const data = await r.json();
    if (!r.ok) throw data;
    return data;
  },

  // Binary GET. Sessions are Bearer-header-only (no cookie is ever set), so
  // anything the browser fetches for itself — an <a download>, an <audio
  // src> — would 401. Everything binary comes through here and is handed on
  // as an object URL, which also keeps the token out of the URL bar.
  async blob(path) {
    const h = {};
    if (this.token) h['Authorization'] = `Bearer ${this.token}`;
    const r = await fetch(ingressPath(path), { headers: h });
    if (r.status === 401) { API.unauthorized(); throw { code: 'not_authenticated', status: 401 }; }
    if (!r.ok) {
      let data = { code: 'error', status: r.status };
      try { data = await r.json(); } catch {}
      throw data;
    }
    return r.blob();
  },

  async upload(path, file, fieldName = 'binary') {
    const h = {};
    if (this.token) h['Authorization'] = `Bearer ${this.token}`;
    const form = new FormData();
    form.append(fieldName, file);
    const r = await fetch(ingressPath(path), { method: 'POST', headers: h, body: form });
    if (r.status === 401) { API.unauthorized(); throw { code: 'not_authenticated', status: 401 }; }
    const data = await r.json();
    if (!r.ok) throw data;
    return data;
  },
};

// ─── Helpers ──────────────────────────────────────────────────────────────────

// owwModel display label: stock names ("hey_jarvis_v0.1") and custom model
// file paths ("/app/data/oww_models/hey_clara.onnx") both prettify.
function wwModelLabel(v) {
  if (!v) return '—';
  if (v.endsWith('.onnx')) v = v.split('/').pop().replace(/\.onnx$/, '');
  return v.replace(/_v[\d.]+$/, '').replace(/_/g, ' ');
}

function uptime(s) {
  if (!s) return '—';
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function relTime(ts) {
  if (!ts) return '—';
  const d = Date.now() - ts * 1000;
  if (d < 60000) return `${Math.floor(d / 1000)}s ago`;
  if (d < 3600000) return `${Math.floor(d / 60000)}m ago`;
  if (d < 86400000) return `${Math.floor(d / 3600000)}h ago`;
  return `${Math.floor(d / 86400000)}d ago`;
}

// Controller-generated device log lines (`device_log` on the shared events
// socket) are the only progress channel a long shell-plane operation has —
// the POST that starts it does not return until it is finished. Components
// that need to watch them live subscribe here instead of having the whole
// stream drilled down through props.
const _logSubs = new Set();
function subscribeDeviceLog(fn) { _logSubs.add(fn); return () => _logSubs.delete(fn); }
function _emitDeviceLog(deviceId, entry) {
  _logSubs.forEach(fn => { try { fn(deviceId, entry); } catch (e) { console.error(e); } });
}

function deviceState(d) {
  if (!d.approved)  return { key: 'pending',   label: 'Pending',   color: 'var(--accent-hi)', dot: '#8ab0d0' };
  if (!d.connected) return { key: 'offline',   label: 'Offline',   color: 'var(--warn)', dot: '#d4703a' };
  if (d.muted)      return { key: 'muted',     label: 'Muted',     color: 'var(--error)', dot: '#c04040' };
  if (d.speaking)   return { key: 'speaking',  label: 'Speaking',  color: 'var(--accent)', dot: '#4080d0' };
  if (d.thinking)   return { key: 'thinking',  label: 'Thinking',  color: 'var(--warn)', dot: '#a08020' };
  if (d.listening)  return { key: 'listening', label: 'Listening', color: 'var(--ok)', dot: '#40906a' };
  return               { key: 'idle',      label: 'Idle',      color: 'var(--muted)', dot: '#aaaaaa' };
}

function eventAccent(level) {
  return { info: 'var(--ok)', warn: 'var(--warn)', error: 'var(--error)' }[level] || 'var(--muted)';
}

// ─── Components ───────────────────────────────────────────────────────────────

function Lcd({ label, value, color, size = 16 }) {
  return (
    <div className="em-lcd">
      {label && <div className="em-lcd__label">{label}</div>}
      {/* The glow is mixed, not hex-concatenated. It used to be `${color}88`,
          which worked only while every colour was a literal — the moment the
          call sites became var(--lcd-green) it produced `var(--lcd-green)88`,
          invalid CSS that drops the whole declaration. The glow silently
          disappeared. color-mix takes a var(); 0x88 is 53%. */}
      <div style={{ fontFamily: "'DM Mono',monospace", fontSize: size, color: color || 'var(--lcd-green)', lineHeight: 1,
                    textShadow: `0 0 8px color-mix(in srgb, ${color || 'var(--lcd-green)'} 53%, transparent)` }}>{value}</div>
    </div>
  );
}

function Pill({ children, accent, danger, disabled, onClick, small, big }) {
  // Variants as classes, not a ternary chain per property. :disabled is
  // handled in CSS, so it beats every variant without this having to know
  // the precedence.
  const cls = ['em-pill',
    small  && 'em-pill--small',
    big    && 'em-pill--big',
    accent && 'em-pill--accent',
    danger && 'em-pill--danger'].filter(Boolean).join(' ');
  return <button className={cls} onClick={onClick} disabled={disabled}>{children}</button>;
}

// ThemeToggle — light/dark, remembered per browser.
//
// The theme itself is applied by an inline script in dashboard.html so it is
// set before first paint; this only flips the attribute that script wrote and
// records the choice. Reading the attribute rather than holding the source of
// truth in React means the two can never disagree.
//
// Defaults to the OS preference until the user picks, and the pick then wins
// permanently — someone who chooses light at their desk does not want it
// flipping at sunset because their laptop does.
function ThemeToggle() {
  const [theme, setTheme] = useState(
    () => document.documentElement.getAttribute('data-theme') || 'light');

  function flip() {
    const next = theme === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('em-theme', next); } catch (e) { /* private mode */ }
    setTheme(next);
  }

  return (
    <IconButton onClick={flip}
      label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}>
      {theme === 'dark' ? '☾' : '☀'}
    </IconButton>
  );
}

// IconButton — the round icon control in the header. One shell so the theme
// toggle, settings and sign-out are the same object at three sizes of glyph
// rather than three near-identical buttons.
//
// `label` is required and does double duty: the tooltip and the accessible
// name. An icon-only control has no visible text, so without it the button is
// a mystery to a screen reader and a guess to everyone else.
function IconButton({ onClick, label, danger, accent, busy, disabled, children }) {
  const cls = ['em-iconbtn', accent && 'em-iconbtn--accent', busy && 'em-iconbtn--busy']
    .filter(Boolean).join(' ');
  return (
    <button className={cls} onClick={onClick} title={label} aria-label={label}
            aria-busy={busy || undefined} disabled={disabled || busy}
            style={danger ? { color: 'var(--error)' } : undefined}>
      {children}
    </button>
  );
}

// Check-for-updates: a refresh arc. Spins while the check is in flight, which
// is the only progress this action can show — it is a single request whose
// answer is "yes" or "no".
function RefreshIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true"
         stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9"/>
      <path d="M13.7 2.4v3.2h-3.2"/>
    </svg>
  );
}

// Deploy-to-fleet: an arrow rising out of a tray. Up rather than down because
// this pushes firmware out to the devices; a download arrow would say the
// opposite of what the button does.
function DeployIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true"
         stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 10.5V2.5"/>
      <path d="M5 5.5 8 2.5l3 3"/>
      <path d="M2.5 10.5v2a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-2"/>
    </svg>
  );
}

// Sign-out mark: a door with an arrow leaving it. Drawn rather than set in
// type because Unicode has no unambiguous glyph for it — the near misses are
// power (⏻), which reads as "shut the device down", and escape (⎋), which
// almost nobody recognises.
function SignOutIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true"
         stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M6 2.5H3.5A1.5 1.5 0 0 0 2 4v8a1.5 1.5 0 0 0 1.5 1.5H6"/>
      <path d="M10.5 11 14 8l-3.5-3"/>
      <path d="M14 8H6"/>
    </svg>
  );
}

// SectionLabel — the small uppercase mono heading used throughout. One
// definition instead of the same inline style repeated per call site.
function SectionLabel({ children, style }) {
  return (
    <div className="em-label" style={style}>
      {children}
    </div>
  );
}

// Panel — bordered card grouping related controls. Gives tab content a
// consistent visual structure instead of floating elements.
function Panel({ label, children, style }) {
  return (
    <div className="em-panel" style={style}>
      {label && <SectionLabel>{label}</SectionLabel>}
      {children}
    </div>
  );
}

// CircleButton — the round header button (close, delete). One treatment
// everywhere instead of per-modal variants.
function CircleButton({ onClick, title, color, children }) {
  return (
    <button onClick={onClick} title={title} style={{
      background: 'linear-gradient(180deg,var(--sunken),var(--border))', border: '1px solid var(--muted)',
      borderRadius: '50%', width: 28, height: 28, display: 'flex', alignItems: 'center',
      justifyContent: 'center', cursor: 'pointer', boxShadow: '0 1px 0 var(--sheen) inset',
      color: color || 'var(--text2)', fontSize: 15, fontWeight: 300, lineHeight: 1,
    }}>{children}</button>
  );
}

function Slider({ label, sub, value, min, max, step = 1, unit = '', formatValue, onChange, disabled = false }) {
  const display = formatValue ? formatValue(value) : `${value}${unit}`;
  // minWidth: 0 (root + label div) and width: 100% on the range input for
  // the same reason as Toggle below: a grid item's min-width defaults to
  // its content, so a fixed-min-width native range input (~129px) plus the
  // label row forced narrow grid columns wider than their track — the
  // controls leaked past the panel edge on phone widths.
  return (
    <div style={{ marginBottom: 20, minWidth: 0 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 7, minWidth: 0, gap: 8 }}>
        <div style={{ minWidth: 0 }}>
          <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: disabled ? 'var(--muted)' : 'var(--text2)' }}>{label}</span>
          {sub && <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--muted)', marginLeft: 8 }}>{sub}</span>}
        </div>
        <Lcd value={display} size={12} />
      </div>
      {/* A control whose feature the device lacks is shown disabled WITH the
          reason (in sub), never as one that silently does nothing. */}
      <input type="range" min={min} max={max} step={step} value={value} disabled={disabled}
        style={{ width: '100%', opacity: disabled ? 0.45 : 1 }}
        onChange={e => onChange(Number(e.target.value))} />
    </div>
  );
}

function Toggle({ label, sub, value, onChange, disabled = false }) {
  // minWidth: 0 on the flex container and label lets long label/sub text
  // shrink and wrap instead of forcing the row (and the switch with it)
  // wider than the grid column — which pushed the switch past the edge of
  // the config dialog. flexShrink: 0 keeps the switch at full size.
  //
  // `disabled` is honoured in the HANDLER, not only in the styling — the
  // capability rule ("shown disabled with the reason, never a control that
  // silently does nothing") is a claim about what a click does, and a switch
  // that greys itself while still writing is worse than one that does
  // nothing: the stored setting then disagrees with what the control shows.
  // Slider has always taken the same prop; Toggle did not take it at all, so
  // every caller that wanted it had to fake it by neutering `value` and
  // `onChange` at the call site.
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20, minWidth: 0, gap: 10 }}>
      <div style={{ minWidth: 0, flex: 1 }}>
        <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: disabled ? 'var(--muted)' : 'var(--text2)' }}>{label}</span>
        {sub && <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--muted)', marginLeft: 8 }}>{sub}</span>}
      </div>
      <div onClick={() => { if (!disabled) onChange(!value); }} style={{
        width: 36, height: 20, borderRadius: 10, cursor: disabled ? 'default' : 'pointer',
        position: 'relative', flexShrink: 0, opacity: disabled ? 0.45 : 1,
        background: value ? 'var(--accent)' : 'var(--muted)',
        border: value ? '1px solid var(--accent-deep)' : '1px solid var(--muted)',
        transition: 'background 0.15s',
      }}>
        <div style={{
          position: 'absolute', top: 2, left: value ? 17 : 2,
          width: 14, height: 14, borderRadius: 7,
          background: value ? 'var(--accent-tint)' : 'var(--bg)',
          transition: 'left 0.15s',
        }}/>
      </div>
    </div>
  );
}

// A segmented choice, for settings with more than two states. Written as
// buttons rather than a native <select> so the unavailable options stay
// VISIBLE and disabled: the capability rule is that a device lacking a feature
// shows the control with the reason, and a native select would hide the option
// entirely, which reads as the feature not existing at all.
function Select({ label, sub, value, options, onChange }) {
  return (
    <div style={{ marginBottom: 20, minWidth: 0 }}>
      <div style={{ marginBottom: 7, minWidth: 0 }}>
        <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: 'var(--text2)' }}>{label}</span>
      </div>
      <div style={{ display: 'flex', gap: 6, minWidth: 0 }}>
        {options.map(o => (
          <button
            key={o.value}
            className={'em-pill em-pill--small' + (o.value === value ? ' em-pill--accent' : '')}
            disabled={!!o.disabled}
            style={{ flex: 1, minWidth: 0 }}
            onClick={() => { if (!o.disabled) onChange(o.value); }}>
            {o.label}
          </button>
        ))}
      </div>
      {sub && (
        <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--muted)', marginTop: 7 }}>
          {sub}
        </div>
      )}
    </div>
  );
}

// ─── EQ frequency response curve ─────────────────────────────────────────────

function EqCurve({ bands, fs = 22050 }) {
  const FREQS = [125, 250, 500, 1000, 2000, 3500, 5500, 8000];
  const Q = 1.4, DB_RANGE = 14, N = 130, F_MIN = 60, F_MAX = 11000;
  const W = 380, H = 90, PT = 8, PB = 20, PL = 8, PR = 8;
  const IW = W - PL - PR, IH = H - PT - PB;

  function peakCoeffs(fc, g) {
    const A = Math.pow(10, g/40), w0 = 2*Math.PI*fc/fs;
    const cw = Math.cos(w0), alpha = Math.sin(w0)/(2*Q), a0 = 1+alpha/A;
    return { b:[(1+alpha*A)/a0,(-2*cw)/a0,(1-alpha*A)/a0], a:[1,(-2*cw)/a0,(1-alpha/A)/a0] };
  }
  function loShelfCoeffs(fc, g) {
    const A = Math.pow(10, g/40), w0 = 2*Math.PI*fc/fs;
    const cw = Math.cos(w0), sw = Math.sin(w0), sqA = Math.sqrt(A), al = sw/Math.SQRT2;
    const a0 = (A+1)+(A-1)*cw+2*sqA*al;
    return { b:[A*((A+1)-(A-1)*cw+2*sqA*al)/a0, 2*A*((A-1)-(A+1)*cw)/a0, A*((A+1)-(A-1)*cw-2*sqA*al)/a0],
             a:[1, -2*((A-1)+(A+1)*cw)/a0, ((A+1)+(A-1)*cw-2*sqA*al)/a0] };
  }
  function hiShelfCoeffs(fc, g) {
    const A = Math.pow(10, g/40), w0 = 2*Math.PI*fc/fs;
    const cw = Math.cos(w0), sw = Math.sin(w0), sqA = Math.sqrt(A), al = sw/Math.SQRT2;
    const a0 = (A+1)-(A-1)*cw+2*sqA*al;
    return { b:[A*((A+1)+(A-1)*cw+2*sqA*al)/a0, -2*A*((A-1)+(A+1)*cw)/a0, A*((A+1)+(A-1)*cw-2*sqA*al)/a0],
             a:[1, 2*((A-1)-(A+1)*cw)/a0, ((A+1)-(A-1)*cw-2*sqA*al)/a0] };
  }
  function biquadMag({b, a}, f) {
    const w = 2*Math.PI*f/fs, c1=Math.cos(w), s1=Math.sin(w), c2=Math.cos(2*w), s2=Math.sin(2*w);
    const nR=b[0]+b[1]*c1+b[2]*c2, nI=-(b[1]*s1+b[2]*s2);
    const dR=1+a[1]*c1+a[2]*c2,    dI=-(a[1]*s1+a[2]*s2);
    return Math.sqrt((nR*nR+nI*nI)/(dR*dR+dI*dI));
  }

  const pts = Array.from({length:N}, (_,i) => Math.exp(Math.log(F_MIN) + i/(N-1)*Math.log(F_MAX/F_MIN)));
  const dbs = pts.map(f => {
    let mag = 1;
    bands.forEach((g,i) => {
      mag *= biquadMag(i===0 ? loShelfCoeffs(FREQS[i],g) : i===7 ? hiShelfCoeffs(FREQS[i],g) : peakCoeffs(FREQS[i],g), f);
    });
    return 20*Math.log10(Math.max(mag, 1e-10));
  });

  const xOf = f  => PL + IW*(Math.log(f/F_MIN)/Math.log(F_MAX/F_MIN));
  const yOf = db => PT + IH*(1 - (Math.max(-DB_RANGE, Math.min(DB_RANGE, db))+DB_RANGE)/(2*DB_RANGE));

  const line = pts.map((f,i) => `${i===0?'M':'L'}${xOf(f).toFixed(1)},${yOf(dbs[i]).toFixed(1)}`).join(' ');
  const fill = `${line} L${xOf(F_MAX).toFixed(1)},${yOf(0).toFixed(1)} L${xOf(F_MIN).toFixed(1)},${yOf(0).toFixed(1)}Z`;

  const dbTicks = [-12,-6,0,6,12];
  const fTicks  = [{f:125,label:'125'},{f:500,label:'500'},{f:1000,label:'1k'},{f:4000,label:'4k'},{f:8000,label:'8k'}];

  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width:'100%', display:'block', marginBottom:4, borderRadius:4, overflow:'hidden' }}>
      <rect x={PL} y={PT} width={IW} height={IH} fill="var(--hairline)" rx="2"/>
      {dbTicks.map(db => (
        <line key={db} x1={PL} x2={PL+IW} y1={yOf(db)} y2={yOf(db)}
          stroke={db===0?'rgba(0,0,0,0.18)':'var(--hairline)'}
          strokeWidth={db===0?1:0.5} strokeDasharray={db===0?undefined:'2,3'}/>
      ))}
      {fTicks.map(({f}) => (
        <line key={f} x1={xOf(f)} x2={xOf(f)} y1={PT} y2={PT+IH}
          stroke="var(--hairline)" strokeWidth={0.5}/>
      ))}
      <path d={fill} fill="rgba(64,88,120,0.10)"/>
      <path d={line} fill="none" stroke="var(--accent)" strokeWidth="1.5"
        style={{filter:'drop-shadow(0 0 4px rgba(64,88,120,0.4))'}}/>
      {dbTicks.filter(d=>d!==0).map(db => (
        <text key={db} x={PL+2} y={yOf(db)+4}
          style={{fontFamily:"'DM Mono',monospace",fontSize:6,fill:'rgba(0,0,0,0.28)'}}>{db>0?'+':''}{db}</text>
      ))}
      {fTicks.map(({f,label}) => (
        <text key={f} x={xOf(f)} y={H-4} textAnchor="middle"
          style={{fontFamily:"'DM Mono',monospace",fontSize:6,fill:'rgba(0,0,0,0.28)'}}>{label}</text>
      ))}
    </svg>
  );
}

// ─── WiFi signal bars ─────────────────────────────────────────────────────────

// 2.4 or 5GHz from the frequency the device reports, or null when it has not
// reported one (old firmware, or wpa_cli unavailable when the stat was taken).
// Null rather than a guess: "2.4GHz" shown for a device we cannot actually
// see the band of is worse than showing nothing, since the whole point is
// spotting a device that has quietly landed on the slower radio.
function wifiBand(freqMhz) {
  if (!freqMhz) return null;
  return freqMhz >= 4900 ? '5GHz' : '2.4GHz';
}

function SignalBars({ rssi }) {
  // 0 bars = no signal / null, 4 bars = excellent
  const level = rssi == null ? 0
              : rssi > -60   ? 4
              : rssi > -70   ? 3
              : rssi > -80   ? 2
              : rssi > -90   ? 1
              :                0;
  const on  = level > 0 ? 'var(--ok)' : 'var(--track)';
  const off = 'var(--track)';
  const bars = [{h:4,y:11},{h:7,y:8},{h:10,y:5},{h:14,y:1}];
  return (
    <svg width={20} height={16} style={{ display:'block', flexShrink:0 }}>
      {bars.map((b,i) => (
        <rect key={i} x={i*5} y={b.y} width={4} height={b.h} rx={1}
          fill={i < level ? (level===1?'var(--error)':level===2?'var(--warn)':'var(--ok)') : off}/>
      ))}
    </svg>
  );
}

// Severity palette, shared with StatBar and SignalBars. The panel previously
// carried two: these desaturated tones and a brighter set (var(--error) and
// friends) that had crept in on the latency row, which is why the scalar
// metrics read as bolted on rather than designed.
const SEV = { ok: 'var(--ok)', warn: 'var(--warn)', bad: 'var(--error)', none: 'transparent' };

// MicroMeter is a 2px severity bar. It exists so the scalar metrics
// (link/latency/temp) share a visual grammar with the capacity bars above
// them, instead of being three bare numbers stacked at the end of the panel.
function MicroMeter({ pct, sev }) {
  return (
    <div style={{ height:2, borderRadius:1, background:'var(--track)', overflow:'hidden', marginTop:5 }}>
      {pct != null && <div style={{ height:'100%', width:`${Math.max(2, Math.min(100, pct))}%`,
        background:SEV[sev] ?? SEV.ok, borderRadius:1, transition:'width 0.6s' }}/>}
    </div>
  );
}

// StatTile is one cell of the scalar row: label, value, optional glyph, and a
// headroom meter. `note` carries an exception worth seeing (thermal
// throttling), which is the only thing here that should ever shout.
//
// `sub` is the quiet counterpart: context that qualifies the value without
// being a problem, like which WiFi band a link reading was taken on. It is
// deliberately a separate prop rather than a second use of `note`, because
// `note` is red — routing neutral information through it would make every
// device look like it was in trouble.
function StatTile({ label, value, unit, sev = 'ok', pct, glyph, note, sub }) {
  const dim = value == null;
  return (
    <div style={{ flex:'1 1 0', minWidth:0 }}>
      <div style={{ fontFamily:"'DM Mono',monospace", fontSize:9, color:'var(--muted)',
                    textTransform:'uppercase', letterSpacing:'0.08em', whiteSpace:'nowrap' }}>{label}</div>
      {/* FIXED height, and the glyph is centre-aligned rather than
          baseline-aligned.

          A flex item with no text baseline of its own — Link's SignalBars is
          a 16px SVG — has its BOTTOM EDGE aligned to the row's baseline, so
          it sits taller than the text and grew the whole row. That pushed the
          Link tile's meter several pixels below Latency's and Temp's, whose
          glyphs are ordinary text. Pinning the height decouples the meters'
          vertical position from whatever a tile puts in this row, so the row
          of meters lines up by construction rather than by coincidence. */}
      <div style={{ display:'flex', alignItems:'baseline', gap:4, marginTop:3, height:18 }}>
        <span style={{ fontFamily:"'DM Mono',monospace", fontSize:12,
                       color: dim ? 'var(--muted)' : SEV[sev] ?? 'var(--text2)' }}>
          {dim ? '—' : value}
        </span>
        {!dim && unit && <span style={{ fontFamily:"'DM Mono',monospace", fontSize:9, color:'var(--muted)' }}>{unit}</span>}
        {glyph && <span style={{ marginLeft:'auto', display:'flex', alignItems:'center',
                                 alignSelf:'center', flexShrink:0 }}>{glyph}</span>}
      </div>
      <MicroMeter pct={dim ? null : pct} sev={sev}/>
      {note && <div style={{ fontFamily:"'DM Mono',monospace", fontSize:9, color:SEV.bad, marginTop:3 }}>{note}</div>}
      {!note && sub && <div style={{ fontFamily:"'DM Mono',monospace", fontSize:9, color:'var(--muted)', marginTop:3 }}>{sub}</div>}
    </div>
  );
}

function StatBar({ label, pct, text }) {
  const color = pct == null ? 'transparent'
              : pct > 85   ? 'var(--error)'
              : pct > 65   ? 'var(--warn)'
              :               'var(--ok)';
  return (
    <div style={{ marginBottom: 13 }}>
      <div style={{ display:'flex', justifyContent:'space-between', marginBottom:5 }}>
        <span style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--muted)', textTransform:'uppercase', letterSpacing:'0.08em' }}>{label}</span>
        <span style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--text2)' }}>{text ?? '—'}</span>
      </div>
      <div style={{ height:3, borderRadius:2, background:'var(--track)', overflow:'hidden' }}>
        {pct != null && <div style={{ height:'100%', width:`${pct}%`, background:color, borderRadius:2, transition:'width 0.6s' }}/>}
      </div>
    </div>
  );
}



function LedRing({ state, size = 120 }) {
  const cx = size / 2, cy = size / 2, r = size * 0.38;
  const stateKey = state?.key || 'idle';
  const stateColor = state?.dot || '#aaaaaa';
  const isPending = stateKey === 'pending';
  const isOffline = stateKey === 'offline';

  const ledColor = isPending ? '#c8c8c8'
                 : isOffline ? '#d4703a'
                 : stateKey === 'muted' ? '#c04040'
                 : stateKey === 'speaking' ? '#4080d0'
                 : stateKey === 'listening' ? '#40906a'
                 : stateKey === 'thinking' ? '#a08020'
                 : '#3a4a30';

  const shouldPulse = isPending || isOffline;
  const circumference = 2 * Math.PI * (size * 0.38);
  const segLen = circumference / 12 * 0.72;
  const gapLen = circumference / 12 * 0.28;

  return (
    <svg width={size} height={size} style={{ display: 'block', flexShrink: 0 }}>
      <defs>
        <radialGradient id={`shell-${size}`} cx="38%" cy="32%" r="65%">
          <stop offset="0%" stopColor="#505050"/>
          <stop offset="55%" stopColor="#2c2c2c"/>
          <stop offset="100%" stopColor="#181818"/>
        </radialGradient>
        <radialGradient id={`inner-${size}`} cx="42%" cy="36%" r="58%">
          <stop offset="0%" stopColor="#383838"/>
          <stop offset="100%" stopColor="#202020"/>
        </radialGradient>
        <filter id={`glow-${size}`} x="-60%" y="-60%" width="220%" height="220%">
          <feGaussianBlur stdDeviation="2.5" result="blur"/>
          <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
        <clipPath id={`clip-${size}`}><circle cx={cx} cy={cy} r={size*0.47}/></clipPath>
      </defs>
      <circle cx={cx} cy={cy} r={size*0.49} fill="#0d0d0d"/>
      <circle cx={cx} cy={cy} r={size*0.47} fill={`url(#shell-${size})`}/>
      <circle cx={cx} cy={cy} r={size*0.47} fill="none" stroke="rgba(255,255,255,0.07)" strokeWidth="1.2"/>
      <g clipPath={`url(#clip-${size})`}>
        <circle cx={cx} cy={cy} r={r} fill="none" stroke="#0b0b0b" strokeWidth={size*0.065}/>
        <circle cx={cx} cy={cy} r={r} fill="none"
          stroke={ledColor} strokeWidth={size*0.045}
          strokeDasharray={`${segLen} ${gapLen}`}
          transform={`rotate(-90 ${cx} ${cy})`}
          filter={stateKey !== 'idle' ? `url(#glow-${size})` : undefined}
          style={shouldPulse ? { animation: 'ledpulse 1.8s ease-in-out infinite' } : undefined}
        />
        <circle cx={cx} cy={cy} r={r} fill="none"
          stroke="#141414" strokeWidth={size*0.065}
          strokeDasharray={`1.5 ${circumference/12 - 1.5}`}
          transform={`rotate(-90 ${cx} ${cy})`}
        />
      </g>
      <circle cx={cx} cy={cy} r={size*0.36} fill={`url(#inner-${size})`}/>
      <circle cx={cx} cy={cy} r={size*0.36} fill="none" stroke="rgba(255,255,255,0.04)" strokeWidth="0.8"/>
      <circle cx={cx} cy={cy} r={size*0.09} fill={stateColor} style={{ transition: 'fill 0.4s' }}
        filter={stateKey !== 'idle' ? `url(#glow-${size})` : undefined}/>
      <ellipse cx={cx - size*0.07} cy={cy - size*0.08} rx={size*0.09} ry={size*0.055} fill="rgba(255,255,255,0.06)"/>
    </svg>
  );
}

// ─── Shell terminal ───────────────────────────────────────────────────────────

// Real terminal (xterm.js) over the device shell WebSocket.
//
// Mode is decided by the controller's shell_meta message (sent before any
// shell bytes):
//   pty:true  — device attached sh to a pseudo-terminal. Keystrokes go
//               raw in framed binary messages (0x00 = stdin, 0x01 =
//               resize cols/rows u16 BE); mksh does echo, line editing,
//               prompts, and full-screen apps (top, vi) work.
//   pty:false — pre-PTY firmware: raw pipe, no echo, no framing. Local
//               echo + line buffering emulate the old input box.
function Shell({ deviceId, token, height = 320 }) {
  const containerRef = useRef(null);

  useEffect(() => {
    const term = new window.Terminal({
      fontSize: 12,
      fontFamily: "'DM Mono', monospace",
      cursorBlink: true,
      scrollback: 5000,
      theme: {
        background: '#1c1f18', foreground: '#c8d4b0',
        cursor: '#9aba80', cursorAccent: '#1c1f18',
        selectionBackground: '#3a4430',
      },
    });
    const fit = new window.FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(containerRef.current);
    fit.fit();
    // Take the caret straight away. Shell is mounted only while the Console
    // tab is selected (see `tab === 'console'`), so this component existing
    // IS the user asking for a terminal — there is nothing else on the tab
    // that could reasonably want focus, and without it every visit starts
    // with a click that does nothing except tell xterm you meant it.
    term.focus();

    const sock = new WebSocket(ingressWebSocketUrl(`/api/devices/${deviceId}/shell?token=${token}`));
    sock.binaryType = 'arraybuffer';

    let pty = null;    // null until shell_meta arrives
    let lineBuf = '';  // legacy-mode local line buffer

    const sendResize = () => {
      if (pty !== true || sock.readyState !== 1) return;
      const b = new Uint8Array(5);
      b[0] = 0x01;
      b[1] = term.cols >> 8; b[2] = term.cols & 0xff;
      b[3] = term.rows >> 8; b[4] = term.rows & 0xff;
      sock.send(b);
    };

    sock.onopen = () => term.write(`\x1b[2mshell — ${deviceId}\x1b[0m\r\n`);
    sock.onmessage = e => {
      if (typeof e.data === 'string') {
        try {
          const m = JSON.parse(e.data);
          if (m.type === 'shell_meta') {
            pty = !!m.pty;
            if (pty) sendResize();
            else term.write('\x1b[2m[firmware has no PTY support — line mode; update the device for a full terminal]\x1b[0m\r\n');
            return;
          }
        } catch {}
        term.write(e.data);
        return;
      }
      term.write(new Uint8Array(e.data));
    };
    sock.onclose = () => term.write('\r\n\x1b[2mdisconnected\x1b[0m\r\n');
    sock.onerror = () => term.write('\r\n\x1b[31mconnection error\x1b[0m\r\n');

    const dataSub = term.onData(d => {
      if (sock.readyState !== 1) return;
      if (pty === true) {
        const enc = new TextEncoder().encode(d);
        const b = new Uint8Array(enc.length + 1);
        b[0] = 0x00;
        b.set(enc, 1);
        sock.send(b);
      } else {
        // Legacy pipe: sh has no TTY, so echo and line editing happen here.
        for (const ch of d) {
          if (ch === '\r') { term.write('\r\n'); sock.send(lineBuf + '\n'); lineBuf = ''; }
          else if (ch === '\x7f') { if (lineBuf) { lineBuf = lineBuf.slice(0, -1); term.write('\b \b'); } }
          else if (ch === '\x03') { sock.send('\x03'); term.write('^C\r\n'); lineBuf = ''; }
          else if (ch >= ' ') { lineBuf += ch; term.write(ch); }
        }
      }
    });
    const resizeSub = term.onResize(() => sendResize());
    const ro = new ResizeObserver(() => { try { fit.fit(); } catch {} });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      dataSub.dispose();
      resizeSub.dispose();
      sock.close();
      term.dispose();
    };
  }, [deviceId]);

  return (
    <div style={{ background: '#1c1f18', border: '1px solid #1a1c16', borderRadius: 6, boxShadow: 'inset 0 2px 6px rgba(0,0,0,0.6)', padding: 10, height }}>
      <div ref={containerRef} style={{ height: '100%', width: '100%' }}/>
    </div>
  );
}

// ─── Turn observability (Status tab) ─────────────────────────────────────────
// Stat tiles + a stage-breakdown bar per recent turn. Colors validated
// (dataviz six-checks) against the panel surface #dfdbd3: CVD ΔE 26.3,
// contrast ≥3:1, chroma ≥0.1. Identity is never color-alone: legend + the
// tooltip name each stage.
const TURN_STAGES = [
  { key: 'listen',     label: 'Listening',  color: '#4468a8' },
  { key: 'transcribe', label: 'Transcribe', color: '#1f8a55' },
  { key: 'respond',    label: 'Respond',    color: '#96660a' },
];

function turnSegments(t) {
  // Stage durations from the trace timestamps; -1 = never reached.
  const vad = t.vad_end_ms >= 0 ? t.vad_end_ms : -1;
  const stt = t.stt_ms     >= 0 ? t.stt_ms     : -1;
  const tts = t.tts_url_ms >= 0 ? t.tts_url_ms : -1;
  const listen     = vad >= 0 ? vad : Math.max(t.total_ms || 0, 0);
  const transcribe = (stt >= 0 && vad >= 0) ? Math.max(stt - vad, 0) : 0;
  const respond    = (tts >= 0 && stt >= 0) ? Math.max(tts - stt, 0) : 0;
  return { listen, transcribe, respond, shown: listen + transcribe + respond };
}

function TurnObservability({ turns, deviceId, deviceLabel, recordingsOn, nearMisses, stateLabel, stateColor, isAdmin }) {
  const [hover, setHover] = useState(null); // index into `recent`
  const mono = "'DM Mono',monospace";

  // Saved utterances — play in place or download the WAV. Both go through
  // one fetched object URL per turn (API.blob; see the auth note there), so
  // playing then downloading costs one transfer, not two.
  const [playing, setPlaying] = useState(null);   // turn_id currently sounding
  const [gone, setGone]       = useState(() => new Set()); // 404 = pruned
  const audioRef = useRef(null);
  const urlsRef  = useRef({});    // turn_id -> object URL

  const stopAudio = () => {
    if (audioRef.current) { audioRef.current.pause(); audioRef.current = null; }
    setPlaying(null);
  };

  // Retention is a small per-device file count, far shorter than the turn
  // history, so a row naming a recording that no longer exists is ordinary.
  // Mark it gone and drop its controls rather than surfacing an error.
  const audioUrl = async t => {
    if (urlsRef.current[t.turn_id]) return urlsRef.current[t.turn_id];
    try {
      const url = URL.createObjectURL(await API.blob(
        `/api/devices/${deviceId}/turns/${t.turn_id}/audio`));
      urlsRef.current[t.turn_id] = url;
      return url;
    } catch {
      setGone(g => new Set(g).add(t.turn_id));
      return null;
    }
  };

  const toggleAudio = async t => {
    const wasPlaying = playing === t.turn_id;
    stopAudio();
    if (wasPlaying) return;
    const url = await audioUrl(t);
    if (!url) return;
    const el = new Audio(url);
    el.onended = el.onerror = () => setPlaying(p => (p === t.turn_id ? null : p));
    audioRef.current = el;
    setPlaying(t.turn_id);
    el.play().catch(() => setPlaying(p => (p === t.turn_id ? null : p)));
  };

  const downloadAudio = async t => {
    const url = await audioUrl(t);
    if (!url) return;
    const when = new Date(t.ts * 1000).toISOString().slice(0, 19).replace(/[:T]/g, '');
    const a = document.createElement('a');
    a.href = url;
    a.download = `${(deviceLabel || deviceId).replace(/[^A-Za-z0-9]+/g, '-').toLowerCase()}-${when}.wav`;
    a.click();
  };

  useEffect(() => () => {
    stopAudio();
    // Object URLs pin their blob in memory until revoked — a few minutes on
    // the Activity tab would otherwise leak every recording played.
    Object.values(urlsRef.current).forEach(URL.revokeObjectURL);
    urlsRef.current = {};
  }, []);

  // Recordings and transcripts are admin-only — the server enforces it
  // (require_admin on the audio route, stt_text stripped from /turns);
  // this just avoids offering controls that would 404.
  const anyAudio = isAdmin && turns.some(t => t.audio_file);

  const ok = turns.filter(t => t.outcome === 'ok');
  const successPct = turns.length ? Math.round(ok.length / turns.length * 100) : null;
  const replies = ok.map(t => t.tts_url_ms).filter(v => v >= 0).sort((a, b) => a - b);
  const medianReply = replies.length ? replies[Math.floor(replies.length / 2)] : null;
  const fmtS = ms => (ms / 1000).toFixed(1) + 's';

  // All buffered turns (up to 50), newest first — rendered inside their own
  // scrollable box so a long history never scrolls the stat tiles (or the
  // rest of the tab) out of view.
  const recent = turns.slice().reverse();
  const scale = Math.max(3000, ...recent.map(t => turnSegments(t).shown));

  return (
    <div>
      {/* Stat tiles */}
      <div style={{ display: 'flex', gap: 12, alignItems: 'flex-end', flexWrap: 'wrap', marginBottom: 16 }}>
        <Lcd label="State" value={stateLabel} color={stateColor} size={16}/>
        <Lcd label="Turns (last 50)" value={turns.length} color="var(--lcd-green)" size={16}/>
        <Lcd label="Success" value={successPct != null ? successPct + '%' : '—'}
             color={successPct == null ? 'var(--lcd-dim)' : successPct >= 80 ? 'var(--lcd-green)' : 'var(--lcd-amber)'} size={16}/>
        <Lcd label="Median reply" value={medianReply != null ? fmtS(medianReply) : '—'} color="var(--lcd-dim)" size={16}/>
        <Lcd label="Near-misses" value={nearMisses != null ? nearMisses : '—'}
             color={nearMisses > 0 ? 'var(--lcd-amber)' : 'var(--lcd-dim)'} size={16}/>
        <Lcd label="Underruns" value={turns.reduce((s, t) => s + (t.underruns || 0), 0)}
             color={turns.some(t => t.underruns > 0) ? 'var(--lcd-amber)' : 'var(--lcd-dim)'} size={16}/>
      </div>

      {recent.length === 0 ? (
        <div style={{ fontFamily: mono, fontSize: 11, color: 'var(--muted)' }}>
          No voice turns recorded yet — history starts when the device is next used.
        </div>
      ) : (
        <div style={{ position: 'relative' }}>
          {/* Legend */}
          <div style={{ display: 'flex', gap: 14, marginBottom: 10 }}>
            {TURN_STAGES.map(s => (
              <span key={s.key} style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontFamily: mono, fontSize: 9, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: '0.08em' }}>
                <span style={{ width: 8, height: 8, borderRadius: 2, background: s.color, display: 'inline-block' }}/>
                {s.label}
              </span>
            ))}
            {(recordingsOn || anyAudio) && (
              <span style={{ fontFamily: mono, fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.08em', marginLeft: 'auto' }}>
                ▶ hear the mic{anyAudio ? '' : ' — next turn'}
              </span>
            )}
          </div>

          {/* One stacked bar per turn, newest first — own scroll container */}
          <div style={{ maxHeight: 230, overflowY: 'auto', paddingRight: 4 }}>
          {recent.map((t, i) => {
            const seg = turnSegments(t);
            const time = new Date(t.ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            const failed = t.outcome !== 'ok';
            return (
              <div key={i}
                onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}
                style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '3px 0', cursor: 'default', background: hover === i ? 'var(--hairline)' : 'transparent', borderRadius: 4 }}>
                <span style={{ fontFamily: mono, fontSize: 9, color: 'var(--muted)', width: 38, flexShrink: 0, textAlign: 'right' }}>{time}</span>
                <div style={{ flex: 1, display: 'flex', height: 14, alignItems: 'stretch' }}>
                  {TURN_STAGES.map(s => seg[s.key] > 0 && (
                    <div key={s.key} style={{
                      width: `${seg[s.key] / scale * 100}%`, background: s.color,
                      borderRadius: 3, marginRight: 2, minWidth: 3,
                    }}/>
                  ))}
                </div>
                <span style={{ fontFamily: mono, fontSize: 9, color: 'var(--text2)', width: 34, flexShrink: 0 }}>{fmtS(seg.shown)}</span>
                <span style={{ fontFamily: mono, fontSize: 8, textTransform: 'uppercase', letterSpacing: '0.08em', width: 62, flexShrink: 0, color: failed ? 'var(--warn)' : 'var(--ok)' }}>
                  {t.outcome === 'ok' ? 'ok' : (t.outcome || '?').replace(/_/g, ' ')}
                </span>
                {/* Saved utterance: listen in place, or download the WAV.
                    The slot is reserved even when a turn has no recording so
                    the columns stay aligned as the retention window rolls. */}
                <span style={{ width: 34, flexShrink: 0, display: 'flex', gap: 4, justifyContent: 'flex-end' }}>
                  {isAdmin && t.audio_file && !gone.has(t.turn_id) && (<>
                    <button onClick={() => toggleAudio(t)}
                      title={playing === t.turn_id ? 'Stop' : 'Play the mic audio for this turn'}
                      style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', fontSize: 10, lineHeight: 1, color: playing === t.turn_id ? 'var(--warn)' : 'var(--text2)' }}>
                      {playing === t.turn_id ? '▮' : '▶'}
                    </button>
                    <button onClick={() => downloadAudio(t)} title="Download the WAV"
                      style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', fontSize: 10, lineHeight: 1, color: 'var(--muted)' }}>⤓</button>
                  </>)}
                </span>
              </div>
            );
          })}
          </div>

          {/* Hover detail */}
          {hover != null && recent[hover] && (() => {
            const t = recent[hover]; const seg = turnSegments(t);
            return (
              <div style={{ marginTop: 10, background: 'var(--hairline)', border: '1px solid var(--track)', borderRadius: 6, padding: '8px 12px', fontFamily: mono, fontSize: 10, color: 'var(--text2)', lineHeight: 1.7 }}>
                <span style={{ color: 'var(--muted)' }}>{t.trigger}</span>
                {' · '}listening {fmtS(seg.listen)} · transcribe {fmtS(seg.transcribe)} · respond {fmtS(seg.respond)} · total {fmtS(Math.max(t.total_ms, 0))}
                {t.wake_model ? <><br/>wake {t.wake_model.replace(/\.[a-z]+$/, '').split('/').pop()} score {t.wake_score?.toFixed(3)} (thr {t.wake_threshold?.toFixed(2)}) · noise floor {t.noise_floor?.toFixed(4)}</> : null}
                {t.underruns != null ? <>{t.wake_model ? ' · ' : <br/>}underruns <span style={{ color: t.underruns > 0 ? 'var(--warn)' : 'inherit' }}>{t.underruns}</span></> : null}
                {t.stt_text ? <><br/>“{t.stt_text.length > 90 ? t.stt_text.slice(0, 90) + '…' : t.stt_text}”</> : null}
              </div>
            );
          })()}
        </div>
      )}
    </div>
  );
}

// ─── Connectivity tab ─────────────────────────────────────────────────────────
// Per-device WiFi: shows the current connection and drives the safe network
// switch (device-side executor with auto-rollback — see internal/wifi in the
// firmware). The change is fire-and-forget from here: POST returns 202, the
// device drops off while it switches, and the outcome arrives as a
// device_update event carrying device.wifi.{pending,last_result}.

function ConnectivityTab({ device, row }) {
  const [networks, setNetworks]   = useState(null);   // null = never scanned
  const [scanning, setScanning]   = useState(false);
  const [scanError, setScanError] = useState('');
  const [ssid, setSsid]           = useState('');
  const [psk, setPsk]             = useState('');
  const [showPsk, setShowPsk]     = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [submitError, setSubmitError] = useState('');

  const s       = device.stats || null;
  const wifi    = device.wifi || {};
  const pending = wifi.pending || null;
  const result  = wifi.last_result || null;
  const currentSsid = s?.wifiSsid || null;

  async function doScan() {
    setScanning(true); setScanError('');
    try {
      const r = await API.post(`/api/devices/${device.device_id}/wifi/scan`, {});
      setNetworks(r.networks || []);
    } catch (e) {
      setScanError(e.error || e.message || 'Scan failed');
    }
    setScanning(false);
  }

  async function doSwitch() {
    setConfirming(false); setSubmitError('');
    try {
      await API.post(`/api/devices/${device.device_id}/wifi`, { ssid, psk });
      // Pending state arrives via the device_update push event.
    } catch (e) {
      setSubmitError(e.error || e.message || 'Request failed');
    }
  }

  const mono  = "'DM Mono',monospace";
  const busy  = !!pending;
  const valid = ssid && (!psk || (psk.length >= 8 && psk.length <= 63)) &&
                !/["\\]/.test(ssid) && !/["\\]/.test(psk);

  return (
    <div style={{ minHeight:'100%', display:'flex', flexDirection:'column', gap:16 }}>

      {/* Outcome banners — pending wins over last result */}
      {pending && (
        <div style={{ background:'rgba(64,88,120,0.10)', border:'1px solid rgba(64,88,120,0.25)', borderRadius:8, padding:'12px 16px' }}>
          <div style={{ fontFamily:mono, fontSize:11, color:'var(--accent)' }}>
            Switching to “{pending.ssid}” — the device will drop offline while it changes network.
          </div>
          <div style={{ fontFamily:mono, fontSize:10, color:'var(--muted)', marginTop:4 }}>
            If it can't associate, get an IP, or reach this controller, it rolls back to the previous
            network automatically and reports the failure here (allow ~2 minutes).
          </div>
        </div>
      )}
      {!pending && result && (
        <div style={{ background: result.ok ? 'rgba(40,96,64,0.08)' : 'rgba(192,96,26,0.08)', border:`1px solid ${result.ok ? 'rgba(40,96,64,0.25)' : 'rgba(192,96,26,0.3)'}`, borderRadius:8, padding:'12px 16px' }}>
          <div style={{ fontFamily:mono, fontSize:11, color: result.ok ? 'var(--ok)' : 'var(--warn)' }}>
            {result.ok
              ? `Switched to “${result.ssid}” successfully.`
              : `Change to “${result.ssid}” failed — previous network restored.`}
          </div>
          {!result.ok && result.error && (
            <div style={{ fontFamily:mono, fontSize:10, color:'var(--muted)', marginTop:4 }}>{result.error}</div>
          )}
        </div>
      )}

      <div className="em-grid2" style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:16, alignItems:'start' }}>
        <Panel label="Current connection">
          {row('Network', currentSsid || '—')}
          {row('IP', device.ip && device.ip !== '127.0.0.1' ? device.ip : '—')}
          <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center' }}>
            <span style={{ fontFamily:mono, fontSize:10, color:'var(--muted)', textTransform:'uppercase', letterSpacing:'0.08em' }}>Signal</span>
            <div style={{ display:'flex', alignItems:'center', gap:8 }}>
              <span style={{ fontFamily:mono, fontSize:10, color:'var(--text2)' }}>{s?.wifiRssi != null ? `${s.wifiRssi} dBm` : '—'}</span>
              <SignalBars rssi={s?.wifiRssi ?? null}/>
            </div>
          </div>
          {!s && <div style={{ fontFamily:mono, fontSize:9, color:'var(--muted)', marginTop:8 }}>waiting for device stats…</div>}
        </Panel>

        <Panel label="Visible networks">
          <div style={{ display:'flex', alignItems:'center', gap:10, marginBottom:8 }}>
            <Pill small disabled={scanning || !device.connected || busy} onClick={doScan}>
              {scanning ? 'Scanning…' : networks ? 'Rescan' : 'Scan'}
            </Pill>
            {scanError && <span style={{ fontFamily:mono, fontSize:10, color:'var(--warn)' }}>{scanError}</span>}
          </div>
          {networks && networks.length === 0 && (
            <div style={{ fontFamily:mono, fontSize:10, color:'var(--muted)' }}>No networks found.</div>
          )}
          {networks && networks.length > 0 && (
            <div style={{ maxHeight:170, overflowY:'auto' }}>
              {networks.map(n => (
                <div key={n.ssid} onClick={() => !busy && setSsid(n.ssid)}
                  style={{ display:'flex', justifyContent:'space-between', alignItems:'center', padding:'5px 8px', borderRadius:6, cursor: busy ? 'default' : 'pointer', background: ssid === n.ssid ? 'rgba(64,88,120,0.12)' : 'transparent' }}>
                  <span style={{ fontFamily:mono, fontSize:11, color: ssid === n.ssid ? 'var(--accent)' : 'var(--text)' }}>
                    {n.ssid}{n.ssid === currentSsid ? '  ← current' : ''}
                  </span>
                  <span style={{ fontFamily:mono, fontSize:10, color:'var(--muted)' }}>{n.signal} dBm</span>
                </div>
              ))}
            </div>
          )}
        </Panel>
      </div>

      <Panel label="Change network">
        <div style={{ fontFamily:mono, fontSize:10, color:'var(--muted)', marginBottom:12 }}>
          The device applies the change itself and rolls back automatically if the new network doesn't
          work out — including when it connects but can't reach this controller (wrong VLAN, isolated
          guest network). The previous network is only discarded once the device reports back here.
        </div>
        <div className="em-grid2" style={{ display:'grid', gridTemplateColumns:'1fr 1fr auto', gap:12, alignItems:'end' }}>
          <div>
            <div style={{ fontFamily:mono, fontSize:9, color:'var(--text2)', letterSpacing:'0.08em', marginBottom:4 }}>SSID</div>
            <input type="text" value={ssid} disabled={busy} onChange={e => setSsid(e.target.value)}
              placeholder="Network name" style={{ width:'100%', boxSizing:'border-box' }}/>
          </div>
          <div>
            <div style={{ fontFamily:mono, fontSize:9, color:'var(--text2)', letterSpacing:'0.08em', marginBottom:4 }}>Passphrase</div>
            <div style={{ display:'flex', gap:6 }}>
              <input type={showPsk ? 'text' : 'password'} value={psk} disabled={busy} onChange={e => setPsk(e.target.value)}
                placeholder="WPA passphrase (blank = open)" style={{ flex:1, boxSizing:'border-box' }}/>
              <Pill small onClick={() => setShowPsk(v => !v)}>{showPsk ? 'Hide' : 'Show'}</Pill>
            </div>
          </div>
          {!confirming ? (
            <Pill accent disabled={!valid || busy || !device.connected} onClick={() => setConfirming(true)}>Switch…</Pill>
          ) : (
            <div style={{ display:'flex', gap:8 }}>
              <Pill danger onClick={doSwitch}>Confirm switch</Pill>
              <Pill small onClick={() => setConfirming(false)}>Cancel</Pill>
            </div>
          )}
        </div>
        {ssid && !valid && (
          <div style={{ fontFamily:mono, fontSize:10, color:'var(--warn)', marginTop:8 }}>
            {/["\\]/.test(ssid + psk)
              ? 'SSID/passphrase cannot contain " or \\ characters.'
              : 'WPA passphrase must be 8–63 characters (leave blank for an open network).'}
          </div>
        )}
        {submitError && (
          <div style={{ fontFamily:mono, fontSize:10, color:'var(--warn)', marginTop:8 }}>{submitError}</div>
        )}
        {!device.connected && (
          <div style={{ fontFamily:mono, fontSize:10, color:'var(--warn)', marginTop:8 }}>Device offline — connect it before changing networks.</div>
        )}
      </Panel>
    </div>
  );
}

// ─── Device detail modal ──────────────────────────────────────────────────────

function Detail({ device, token, onClose, onApprove, isAdmin, globalConfig, onDeviceConfigChange }) {
  const [tab, setTab] = useState(() => device.approved ? 'status' : 'approve');
  // Seed from the EFFECTIVE config, not the raw stored one — see
  // effectiveConfig(). A migrated row's stored dict is not the truth.
  const [config, setConfig] = useState(() => effectiveConfig(globalConfig, device));
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  // Which config sections this device overrides (ids from CONFIG_SECTIONS).
  // Empty = follows the fleet entirely, which is what use_global_config
  // meant before per-section scoping.
  const [sections, setSections] = useState(device.config_sections ?? []);
  const [logs, setLogs] = useState([]);
  const [logsLoading, setLogsLoading] = useState(false);
  const [pushLog, setPushLog] = useState([]);
  const [pushing, setPushing] = useState(false);
  const [release, setRelease] = useState(null);
  const [checkingRelease, setCheckingRelease] = useState(false);
  // Whether the background release poll runs (#159). Defaults TRUE so a
  // failed status fetch shows nothing rather than claiming checks are off —
  // a wrong "disabled" notice sends someone to change a setting that is
  // already correct.
  const [autoChecks, setAutoChecks] = useState(true);
  const [approveLabel, setApproveLabel] = useState(device.label || '');
  const [approving, setApproving] = useState(false);
  const [localFile, setLocalFile] = useState(null);
  const [notesOpen, setNotesOpen] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState(device.label || '');
  const [renameSaving, setRenameSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [securing, setSecuring] = useState(false);
  const [debloating, setDebloating] = useState(false);
  const [assets, setAssets] = useState(null);
  const [installing, setInstalling] = useState(false);
  // Wake word asset install progress: bytes confirmed sent, the file in
  // flight, the controller's own narration, and the final outcome.
  const [assetSent, setAssetSent]     = useState(0);
  const [assetNow, setAssetNow]       = useState(null);
  const [assetLog, setAssetLog]       = useState([]);
  const [assetResult, setAssetResult] = useState(null);
  const fileInputRef = useRef(null);
  const [turns, setTurns] = useState([]);
  const state = deviceState(device);
  const needsUpdate = device.firmware_ver && release?.version && device.firmware_ver !== release.version;

  const TABS = device.approved
    ? (isAdmin ? ['status', 'activity', 'config', 'console', 'updates', 'logs'] : ['status', 'activity', 'config', 'logs'])
    : ['approve'];

  useEffect(() => {
    if (tab === 'logs') {
      setLogsLoading(true);
      API.get(`/api/devices/${device.device_id}/logs?limit=50`)
        .then(setLogs).catch(console.error)
        .finally(() => setLogsLoading(false));
    }
    if (tab === 'updates') {
      API.get('/api/releases/latest').then(setRelease).catch(() => {});
      // Same tab-entry pattern as the asset state below: this changes only
      // when someone edits system config, so polling it would be waste.
      API.get('/api/system/status')
        .then(s => setAutoChecks(s.update_checks_enabled !== false))
        .catch(() => {});
      // Asset state costs a device shell round trip, so it is fetched on tab
      // entry rather than polled — unlike a release, it only changes when
      // someone acts on it here.
      API.get(`/api/devices/${device.device_id}/oww_assets`)
        .then(setAssets).catch(() => setAssets(null));
    }
  }, [tab, device.device_id]);

  // Keep asking while the Updates tab is open.
  //
  // It used to fetch exactly once on tab entry, so a tab left open never
  // learned about a new release and "there's an update" appeared to require
  // pressing Check now. The Activity tab already refreshes on a timer for the
  // same reason; this is that pattern. 30s rather than Activity's 10s because
  // releases are hours apart, and the server side now returns fresh data
  // rather than a stale cache, so each poll is worth something.
  useEffect(() => {
    if (tab !== 'updates') return;
    let live = true;
    const iv = setInterval(() => {
      API.get('/api/releases/latest')
        .then(r => { if (live) setRelease(r); })
        .catch(() => {});
    }, 30000);
    return () => { live = false; clearInterval(iv); };
  }, [tab, device.device_id]);

  // Turn observability — fetch on Activity tab entry, refresh every 10s while
  // the tab is open (turn history is in-memory on the controller).
  useEffect(() => {
    if (tab !== 'activity') return;
    let live = true;
    const load = () => API.get(`/api/devices/${device.device_id}/turns`)
      .then(t => { if (live) setTurns(Array.isArray(t) ? t : []); })
      .catch(() => {});
    load();
    const iv = setInterval(load, 10000);
    return () => { live = false; clearInterval(iv); };
  }, [tab, device.device_id]);

  function setConf(k, v) { setConfig(c => ({ ...c, [k]: v })); setDirty(true); }

  async function doCheckRelease() {
    setCheckingRelease(true);
    try {
      // POST /api/releases/check force-polls GitHub directly, bypassing
      // both the 60s in-memory cache and the (default 1h) DB cache that
      // GET /api/releases/latest reads from. That route exists already
      // but nothing in the dashboard called it — this is the only place
      // that does.
      const rel = await API.post('/api/releases/check', {});
      setRelease(rel);
    } catch(e) {
      alert(e.error || 'Release check failed');
    }
    setCheckingRelease(false);
  }

  async function pushConfig() {
    setSaving(true);
    try {
      // Send the scoping plus the full effective config. The controller
      // keeps only the values belonging to overridden sections, so sending
      // everything is safe and keeps the clobber guard satisfied (it sees no
      // in-scope key going missing).
      const body = { config_sections: sections, ...config };
      const res = await API.post(`/api/devices/${device.device_id}/config`, body);
      setDirty(false);
      // Keep parent device list in sync so re-opening the modal is consistent
      if (onDeviceConfigChange) {
        onDeviceConfigChange(device.device_id, {
          config: res.config,
          config_sections: res.config_sections,
          use_global_config: res.use_global_config,
        });
      }
    } catch(e) { alert(e.error || 'Failed to push config'); }
    setSaving(false);
  }

  async function doSecureLink() {
    // Pushes CA + link token over the shell plane, then the controller
    // bounces the connection; the device redials over wss. The "Link" row
    // flips to wss (TLS) on the next device-list refresh after reconnect.
    setSecuring(true);
    try {
      await API.post(`/api/devices/${device.device_id}/secure_link`, {});
    } catch(e) { alert(e.error || 'Secure link failed'); }
    // Leave the button disabled briefly — transfer + reconnect takes ~10s.
    setTimeout(() => setSecuring(false), 15000);
  }

  async function doDebloat() {
    // Re-applies both debloat halves: syncs the boot script and hides any
    // package added to the list since this device was provisioned. Needed
    // because the OTA-time sync cannot reach a device already running the
    // latest firmware. Idempotent, and the daemon stops for PERSISTENT
    // packages only take hold on the next reboot — hence the wording.
    setDebloating(true);
    try {
      await API.post(`/api/devices/${device.device_id}/debloat`, {});
      alert('Debloat re-applied. Newly hidden packages stop at the next device reboot — '
            + 'watch the device log for details.');
    } catch(e) { alert(e.error || 'Debloat failed'); }
    setTimeout(() => setDebloating(false), 8000);
  }

  async function doInstallAssets() {
    // Up to ~15MB over the shell plane, which is minutes on a marginal link —
    // far too long to sit behind a spinner and then a browser alert().
    //
    // The POST does not return until the whole sync is done, so its own
    // response cannot drive a bar. The controller narrates each file to the
    // device log as it goes (`_sync_oww_assets.say`), and those lines arrive
    // on the events socket — so progress is derived by watching for its
    // "sending <name> (<n>MB)…" line and charging that file's bytes to the
    // total as it completes. Byte-weighted, not file-count: the runtime is
    // 12.3MB of a ~15MB job, so counting files would jump 0→50% for the
    // trivial half and then appear frozen for the long one.
    const sizes = Object.fromEntries((assets?.required || []).map(a => [a.name, a.size]));
    const queue = (assets?.missing || []).slice();
    const total = queue.reduce((n, name) => n + (sizes[name] || 0), 0);

    setInstalling(true);
    setAssetResult(null);
    setAssetLog([]);
    setAssetSent(0);
    setAssetNow(queue.length ? null : 'Checking what the device needs…');

    let sent = 0, current = null;
    const unsub = subscribeDeviceLog((id, entry) => {
      if (id !== device.device_id) return;
      const m = /^Wake word assets: (.*)$/.exec(entry.message || '');
      if (!m) return;
      const text = m[1];
      setAssetLog(l => [...l.slice(-40), { level: entry.level, text }]);
      const sending = /^sending (\S+)/.exec(text);
      if (sending) {
        // The previously-announced file is done once the next one starts.
        if (current) { sent += sizes[current] || 0; setAssetSent(sent); }
        current = sending[1];
        setAssetNow(current);
      }
    });

    try {
      const res = await API.post(`/api/devices/${device.device_id}/oww_assets`, {});
      const n = (res.pushed || []).length;
      setAssetSent(total);
      setAssetNow(null);
      setAssetResult({ ok: true, text: n === 0
        ? 'Already up to date — nothing needed installing.'
        : `Installed ${n} file${n === 1 ? '' : 's'}. Restart the device to start scoring on-device.` });
      setAssets(await API.get(`/api/devices/${device.device_id}/oww_assets`));
    } catch(e) {
      setAssetNow(null);
      // Partial progress is left on screen deliberately: which file it died on
      // is the useful part, and md5-then-rename means nothing half-written was
      // left behind to worry about.
      setAssetResult({ ok: false, text: e.error || 'Install failed.' });
    } finally {
      unsub();
      setInstalling(false);
    }
  }

  async function doUpdate() {
    setPushing(true); setPushLog(['Fetching latest release from GitHub…']);
    try {
      const res = await API.post(`/api/devices/${device.device_id}/update`, {});
      setPushLog(l => [...l, `Deploying ${res.version} — waiting for reconnect…`]);
      _pollReconnect(res.version, device.firmware_ver);
    } catch(e) {
      setPushLog([`Error: ${e.error || 'Update failed'}`]);
      setPushing(false);
    }
  }

  async function doLocalDeploy() {
    if (!localFile) return;
    setPushing(true); setUploading(true);
    setPushLog([`Uploading ${localFile.name} (${(localFile.size/1024).toFixed(0)} KB)…`]);
    try {
      const up = await API.upload('/api/releases/upload', localFile);
      setUploading(false);
      setPushLog(l => [...l, `✓ Upload complete${up.version ? ` — ${up.version}` : ''}`]);

      // Ask BEFORE spending a reboot and a slot on a binary the device is
      // already running. The server refuses this too, so declining here is a
      // convenience rather than the guard; what it buys is being told at the
      // point of deciding instead of after. Re-flashing the same version is a
      // real repair for a corrupt slot, so it is a question and not a wall.
      let force = false;
      if (up.version && up.version === device.firmware_ver) {
        if (!confirm(`${device.label || device.device_id} is already running ${up.version}.\n\nInstall it again anyway?`)) {
          setPushLog(l => [...l, 'Cancelled — device already running this build.']);
          setPushing(false);
          return;
        }
        force = true;
      }

      setPushLog(l => [...l, 'Deploying…']);
      const res = await API.post(`/api/devices/${device.device_id}/update`, { upload_token: up.upload_token, force });
      // The picker has done its job — the bytes are on the controller and the
      // deploy is running from the upload token, not from this File handle.
      // Leaving the filename sitting there invites a second Deploy click on a
      // build that is already installing, and the panel then reads as though
      // something is still pending when nothing is.
      setLocalFile(null);
      if (fileInputRef.current) fileInputRef.current.value = '';
      setPushLog(l => [...l, `Deploying ${res.version} — waiting for reconnect…`]);
      _pollReconnect(res.version, device.firmware_ver);
    } catch(e) {
      setUploading(false);
      setPushLog(l => [...l, `Error: ${e.error || 'Deploy failed'}`]);
      setPushing(false);
    }
  }

  // priorVersion is what the device was running BEFORE this push, and it is
  // what makes "rolled back" a claim rather than a guess. The old code
  // inferred a rollback from `firmware_ver !== targetVersion` alone, which is
  // only sound when the target is known to be exactly what the device will
  // report — true for a release, false for a local build.
  //
  // It was false in the way that matters: _extract_binary_version knew only
  // the old date-based scheme, so a clean-tree local build got a synthetic
  // `local-<timestamp>` label that the device could never report back. Every
  // such deploy therefore succeeded and announced "auto-rolled back", which
  // is worse than announcing nothing — the natural response to being told a
  // deploy reverted is to stop using the feature.
  //
  // With the prior version in hand the three outcomes are distinguishable:
  // back on the target is success, back on what it had before is a genuine
  // rollback, anything else is unexpected and says so rather than guessing.
  function _pollReconnect(targetVersion, priorVersion) {
    let attempts = 0;
    let wasDisconnected = false;
    const poll = setInterval(async () => {
      attempts++;
      try {
        const devices = await API.get('/api/devices');
        const d = devices.find(x => x.device_id === device.device_id);
        // Track when the device goes offline during the restart cycle.
        // The rollback check must only fire after observing a disconnect —
        // otherwise it triggers mid-transfer while the device is still
        // connected and running the old firmware.
        if (!d?.connected) wasDisconnected = true;
        if (d?.connected && d?.firmware_ver === targetVersion) {
          setPushLog(l => [...l, `✓ Running ${targetVersion}`, '✓ Update complete']);
          clearInterval(poll); setPushing(false); setLocalFile(null);
        } else if (d?.update_error && !d?.update_in_progress) {
          // Controller recorded a terminal failure (transfer failed, slot
          // detect failed, exception…) — report it now instead of letting
          // the poll run out its 2-minute timeout.
          setPushLog(l => [...l, `✗ ${d.update_error}`]);
          clearInterval(poll); setPushing(false);
        } else if (wasDisconnected && d?.connected && d?.firmware_ver && d.firmware_ver !== targetVersion) {
          // Back on what it was running before = the supervisor flipped the
          // symlink after three fast exits. That is the only case that
          // earns the word "rolled back".
          const rolledBack = priorVersion && d.firmware_ver === priorVersion;
          setPushLog(l => [...l, rolledBack
            ? `⚠ Device reconnected on ${d.firmware_ver} — auto-rolled back`
            // Not the target and not the old one either. Most often the
            // deploy WORKED and the label was wrong: a binary whose version
            // the controller could not read is labelled local-<timestamp>,
            // which the device will never report. Say what was seen and what
            // was expected, and let the reader judge — asserting a rollback
            // here is what made a successful deploy look like a failure.
            : `Device reconnected on ${d.firmware_ver} (expected ${targetVersion})`
              + (String(targetVersion).startsWith('local-')
                  ? ' — the label was generated because the binary carried no'
                    + ' readable version; if the version above is the build you'
                    + ' pushed, this succeeded'
                  : '')]);
          clearInterval(poll); setPushing(false);
        } else if (attempts > 40) {
          setPushLog(l => [...l, 'Timed out — check device logs']);
          clearInterval(poll); setPushing(false);
        }
      } catch(e) { clearInterval(poll); setPushing(false); }
    }, 3000);
  }

  async function doRollback() {
    setPushing(true); setPushLog([`Rolling back to ${device.firmware_previous}…`]);
    try {
      await API.post(`/api/devices/${device.device_id}/rollback`, {});
      _pollReconnect(device.firmware_previous, device.firmware_ver);
    } catch(e) {
      setPushLog([`Error: ${e.error || 'Rollback failed'}`]);
      setPushing(false);
    }
  }

  async function doApprove() {
    if (!approveLabel.trim()) { alert('Please enter a label'); return; }
    setApproving(true);
    try {
      await API.post(`/api/devices/${device.device_id}/approve`, { label: approveLabel });
      onApprove();
      onClose();
    } catch(e) { alert(e.error || 'Approval failed'); }
    setApproving(false);
  }

  async function doRename() {
    const trimmed = renameValue.trim();
    if (!trimmed) { alert('Label cannot be empty'); return; }
    if (trimmed === device.label) { setRenaming(false); return; }
    setRenameSaving(true);
    try {
      // PATCH /api/devices/{id} — confirmed against em_api.py: requires
      // {label}, broadcasts a device_update event over /api/events that
      // App's WebSocket listener already applies to live device state,
      // so no manual setDevices() needed here.
      await API.patch(`/api/devices/${device.device_id}`, { label: trimmed });
      setRenaming(false);
    } catch(e) {
      alert(e.error || 'Rename failed');
    }
    setRenameSaving(false);
  }

  async function doDelete() {
    setDeleting(true);
    try {
      // DELETE /api/devices/{id} — confirmed against em_api.py. Broadcasts
      // device_deleted, which App's WebSocket listener already filters out
      // of device state, so closing here is enough — no manual cleanup.
      await API.del(`/api/devices/${device.device_id}`);
      onClose();
    } catch(e) {
      alert(e.error || 'Delete failed');
      setDeleting(false);
      setConfirmDelete(false);
    }
  }

  const row = (k, v, c) => (
    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0', borderBottom: '1px solid var(--hairline)' }}>
      <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 12, color: 'var(--muted)' }}>{k}</span>
      <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 12, color: c || 'var(--text)', fontWeight: 600 }}>{v}</span>
    </div>
  );

  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(180,176,168,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100, backdropFilter: 'blur(8px)' }}
      onClick={e => e.target === e.currentTarget && onClose()}>
      {/* Fixed height (not maxHeight): every tab renders in an identical
          frame — content scrolls inside, the window never resizes as you
          move between tabs. */}
      <div className="em-modal" style={{ width: 'min(900px,95vw)', height: 'min(700px,90vh)', background: 'linear-gradient(170deg,var(--raised),var(--surface))', border: '1px solid var(--border)', borderRadius: 16, boxShadow: '0 24px 80px rgba(0,0,0,0.3),0 2px 0 var(--sheen) inset', display: 'flex', flexDirection: 'column', overflow: 'hidden', animation: 'fadeIn 0.15s ease' }}>
        {/* Header */}
        <div className="em-modal-head" style={{ background: 'linear-gradient(180deg,var(--card),var(--bg))', borderBottom: '1px solid var(--border-hard)', padding: '20px 24px 0', boxShadow: '0 1px 0 var(--sheen) inset' }}>
          <div className="em-modal-headrow" style={{ display: 'flex', alignItems: 'center', gap: 20, marginBottom: 16 }}>
            <LedRing state={state} size={72}/>
            <div style={{ flex: 1, minWidth: 0 }}>
              {renaming ? (
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <input
                    type="text" value={renameValue} autoFocus
                    onChange={e => setRenameValue(e.target.value)}
                    onKeyDown={e => {
                      if (e.key === 'Enter') doRename();
                      if (e.key === 'Escape') { setRenaming(false); setRenameValue(device.label || ''); }
                    }}
                    style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 20, fontWeight: 600, padding: '4px 8px', maxWidth: 280 }}
                  />
                  <Pill small onClick={doRename} disabled={renameSaving}>{renameSaving ? 'Saving…' : 'Save'}</Pill>
                  <Pill small onClick={() => { setRenaming(false); setRenameValue(device.label || ''); }}>Cancel</Pill>
                </div>
              ) : (
                <div
                  onClick={() => isAdmin && setRenaming(true)}
                  title={isAdmin ? 'Click to rename' : undefined}
                  style={{
                    fontFamily: "'DM Sans',sans-serif", fontSize: 26, color: 'var(--text)', fontWeight: 600,
                    letterSpacing: '-0.02em', lineHeight: 1, cursor: isAdmin ? 'pointer' : 'default',
                    display: 'inline-block',
                  }}>
                  {device.label || <span style={{ color: 'var(--muted)', fontSize: 20 }}>{device.device_id.slice(0,8)}…</span>}
                </div>
              )}
              <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--muted)', marginTop: 4, letterSpacing: '0.05em' }}>
                {(() => {
                  const ip = device.ip && device.ip !== '127.0.0.1' ? device.ip : null;
                  const ipStr = device.connected ? (ip || '—') : (ip ? `${ip} (last seen)` : '—');
                  return <>{ipStr} · {device.device_id} · {device.firmware_ver || 'unknown'}</>;
                })()}
                {needsUpdate && <span style={{ color: 'var(--warn)', marginLeft: 10 }}>Update available</span>}
              </div>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <div style={{ background: 'linear-gradient(160deg,var(--lcd-face),var(--lcd-deep))', border: '1px solid var(--lcd-line)', borderRadius: 6, padding: '5px 12px', boxShadow: 'inset 0 1px 3px rgba(0,0,0,0.5)' }}>
                <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: state.dot, textShadow: `0 0 8px ${state.dot}88`, letterSpacing: '0.05em' }}>{state.label.toUpperCase()}</span>
              </div>
              {isAdmin && !confirmDelete && (
                <CircleButton onClick={() => setConfirmDelete(true)} title="Delete device" color="var(--error)">🗑</CircleButton>
              )}
              {isAdmin && confirmDelete && (
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--error)' }}>Delete?</span>
                  <Pill small danger disabled={deleting} onClick={doDelete}>{deleting ? '…' : 'Confirm'}</Pill>
                  <Pill small onClick={() => setConfirmDelete(false)} disabled={deleting}>Cancel</Pill>
                </div>
              )}
              <CircleButton onClick={onClose} title="Close">×</CircleButton>
            </div>
          </div>
          {device.approved ? (
            <div className="em-tabs" style={{ display: 'flex', gap: 2 }}>
              {TABS.map(t => (
                <button key={t} onClick={() => setTab(t)} style={{ background: tab === t ? 'linear-gradient(180deg,var(--raised),var(--surface))' : 'transparent', border: tab === t ? '1px solid var(--border-hard)' : '1px solid transparent', borderBottom: tab === t ? '1px solid var(--surface)' : '1px solid transparent', borderRadius: '6px 6px 0 0', fontFamily: "'DM Mono',monospace", fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.1em', padding: '7px 14px', cursor: 'pointer', color: tab === t ? 'var(--text)' : 'var(--muted)', marginBottom: -1, transition: 'color 0.15s' }}>{t}</button>
              ))}
            </div>
          ) : (
            // One tab is not a tab bar — it reads as a label and the approval
            // form sat behind a click. Pending devices get a banner instead.
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: -1, padding: '9px 14px', background: 'linear-gradient(180deg,var(--accent-tint),var(--surface))', border: '1px solid var(--accent-line)', borderBottom: '1px solid var(--surface)', borderRadius: '6px 6px 0 0', fontFamily: "'DM Sans',sans-serif", fontSize: 12, color: 'var(--accent-deep)', lineHeight: 1.4 }}>
              <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.12em', color: 'var(--warn)', fontWeight: 600, flexShrink: 0 }}>Action required</span>
              <span>Name this device below, then approve it to add it to your fleet.</span>
            </div>
          )}
        </div>

        {/* Body */}
        <div className="em-modal-body" style={{ flex: 1, overflowY: 'auto', padding: 24 }}>

          {/* APPROVE */}
          {!device.approved && (
            <div style={{ maxWidth: 400 }}>
              <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.15em', marginBottom: 16 }}>New Device — Pending Approval</div>
              {row('Serial', device.device_id)}
              {row('IP', device.ip && device.ip !== '127.0.0.1' ? device.ip : '—')}
              {row('First seen', relTime(device.first_seen))}
              <div style={{ marginTop: 24, marginBottom: 8 }}>
                <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: 'var(--text2)', marginBottom: 8 }}>Label</div>
                <input type="text" value={approveLabel} onChange={e => setApproveLabel(e.target.value)} placeholder="e.g. Kitchen" onKeyDown={e => e.key === 'Enter' && doApprove()}/>
                <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 11, color: 'var(--muted)', marginTop: 6 }}>
                  Names the device everywhere — the dashboard, and “{approveLabel.trim() || '…'} Voice Assistant” in Home Assistant.
                </div>
              </div>
              {/* Approval is the consequential act on this screen, not a
                  formality: it admits the device to the fleet, opens a voice
                  satellite for it and starts accepting its microphone. The
                  button used to be a default-weight pill reading "Approve
                  Device", which read as an acknowledgement rather than a
                  decision — say what it does, then size it like it matters. */}
              <div style={{ marginTop: 24, background: 'linear-gradient(160deg,var(--text),var(--bg))', border: '1px solid var(--border)', borderRadius: 8, padding: '14px 16px' }}>
                <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 12, color: 'var(--text2)', lineHeight: 1.5, marginBottom: 14 }}>
                  Approving adds this device to your fleet: it receives the fleet
                  configuration, gets a voice satellite Home Assistant can drive,
                  and its microphone starts streaming to the controller when woken.
                </div>
                <Pill big accent disabled={approving || !approveLabel.trim()} onClick={doApprove}>
                  {approving ? 'Approving…' : 'Approve & Add to Fleet'}
                </Pill>
                {!approveLabel.trim() && (
                  <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 11, color: 'var(--muted)', marginTop: 10 }}>
                    Enter a label above to continue.
                  </div>
                )}
              </div>
            </div>
          )}

          {/* STATUS */}
          {tab === 'status' && (() => {
            const s = device.stats || null;
            // cpuPct comes from the aggregate /proc/stat line, so it is a
            // share of ONLINE capacity — and MTK parks 3 of this SoC's 4 cores
            // when idle. The same work reads as half the percentage once a
            // second core comes up, so the core count belongs next to it or
            // the number invites the wrong conclusion.
            const cpuText  = s?.cpuPct != null
              ? `${s.cpuPct.toFixed(0)}%` + (s.coresOnline ? ` · ${s.coresOnline}/${s.coresTotal ?? '?'} cores` : '')
              : null;
            // Thermals: mtktscpu is the CPU zone, maxTempC the hottest of all
            // 11 zones (the PMIC and board sensors can run warmer). Amber past
            // 70C, red past 85C — well below this SoC's limits, because the
            // point is early warning. thermalCoreLimit below coresTotal means
            // the thermal governor is already capping capacity, which is the
            // signal that actually matters and shows up before temperature
            // looks alarming.
            const tempC    = s?.cpuTempC ?? null;
            const tempHot  = s?.maxTempC ?? null;
            const throttled = s?.thermalCoreLimit != null && s?.coresTotal != null
                              && s.thermalCoreLimit < s.coresTotal;
            const ramText  = s?.memUsedMb != null ? `${s.memUsedMb} / ${s.memTotalMb} MB` : null;
            const ramPct   = s?.memTotalMb? s.memUsedMb/s.memTotalMb*100 : null;
            const stoPct   = s?.storageTotalMb ? s.storageUsedMb/s.storageTotalMb*100 : null;
            const stoText  = s?.storageTotalMb != null
              ? `${(s.storageUsedMb/1024).toFixed(1)} / ${(s.storageTotalMb/1024).toFixed(1)} GB` : null;
            const cfgEff = effectiveConfig(globalConfig, device);
            const wwLabel = wwModelLabel(cfgEff.owwModel);
            return (
              <div style={{ minHeight:'100%', display:'flex', flexDirection:'column', gap:16 }}>
                <div className="em-grid2" style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:16 }}>
                  <Panel label="Device">
                    {row('IP', (() => {
                      const ip = device.ip && device.ip !== '127.0.0.1' ? device.ip : null;
                      return device.connected ? (ip || '—') : (ip ? `${ip} (last seen)` : '—');
                    })())}
                    {row('Firmware', device.firmware_ver || '—')}
                    {row('WiFi network', s?.wifiSsid || '—')}
                    {/* Was a bare port number, which answered "which port" and
                        never the question anyone opens this panel with — is
                        Home Assistant actually on the other end of it (#349).
                        A device HA has never connected to is Online, idle and
                        completely unable to answer, and until this row said so
                        the only way to find out was to say the wake word and
                        watch nothing happen. Measured on prod: 23 wake events
                        in 14 hours that could not start a turn, with three
                        tiles reading healthy throughout.

                        The port stays, appended: it is what a stale HA config
                        entry is keyed on, so it is the first thing needed the
                        moment this row says Waiting. */}
                    {row('Voice assistant', (() => {
                      const vs = device.voiceSatellite;
                      const p  = vs?.port ?? device.esphome_port;
                      const at = p != null ? ` · port ${p}` : '';
                      if (!vs)            return 'No satellite server';
                      if (vs.haConnected) return `HA connected${at}`;
                      if (vs.listening)   return `Waiting for HA${at}`;
                      return `Port down${at}`;
                    })(), (() => {
                      const vs = device.voiceSatellite;
                      // Not-connected is amber rather than red: HA reconnects
                      // on its own from most of the ways this happens, and a
                      // row that shouts during an ordinary restart is one
                      // people learn to skip past — the #301 cry-wolf rule.
                      if (!vs)            return 'var(--error)';
                      if (vs.haConnected) return 'var(--ok)';
                      return 'var(--warn)';
                    })())}
                    {/* One row, not two. "Connected: Yes" plus "Last seen"
                        was redundant in both directions — while connected the
                        last-seen time says nothing, and while offline the
                        Yes/No says nothing the timestamp doesn't. Merging
                        them frees a row for Volume without the panel growing. */}
                    {row('Status',
                         device.connected ? 'Online' : `Offline · last seen ${relTime(device.last_seen)}`,
                         device.connected ? 'var(--ok)' : 'var(--warn)')}
                    {row('Volume', device.volume != null
                         ? `${Math.round(device.volume * 100)}%`
                         : (s?.volumePct != null ? `${s.volumePct}%` : '—'))}
                    {row('Link', device.connected ? (device.linkTls ? 'wss (TLS)' : 'plain ws') : '—',
                         device.connected ? (device.linkTls ? 'var(--ok)' : 'var(--warn)') : undefined)}
                    {row('Config', (() => {
                      const n = (device.config_sections ?? []).length;
                      const total = Object.keys(CONFIG_SECTIONS).length;
                      return n === 0 ? 'Fleet' : `Local override (${n} of ${total})`;
                    })())}
                    {isAdmin && device.connected && !device.linkTls && (
                      <div style={{ marginTop: 8 }}>
                        <Pill small accent disabled={securing} onClick={doSecureLink}>
                          {securing ? 'Securing…' : 'Secure link'}
                        </Pill>
                      </div>
                    )}
                  </Panel>
                  <Panel label="Resources">
                    <StatBar label="CPU"     pct={s?.cpuPct}    text={cpuText}/>
                    <StatBar label="RAM"     pct={ramPct}        text={ramText}/>
                    <StatBar label="Storage" pct={stoPct}        text={stoText}/>
                    {/* Scalar health metrics as one deliberate row rather than
                        three label/value lines stacked after the bars. Ordered
                        by how often they are the answer on this hardware: the
                        link first (the usual suspect — the RF counters are
                        useless, so RSSI and RTT are all there is), thermals
                        last because they are almost always boring, and loudly
                        when they are not. Each carries a headroom meter so the
                        row rhymes with the capacity bars above it. */}
                    <div style={{ display:'flex', gap:14, marginTop:2 }}>
                      {/* Band alongside RSSI, because one SSID spanning both
                          radios lets a device re-associate to the slower one
                          silently: measured on this fleet, 2.4GHz runs about
                          60 Mbps against 143 on 5GHz. A device knocked off
                          5GHz can then sit on 2.4 indefinitely with nothing
                          on screen to say so. Shown as a note rather than its
                          own tile — it qualifies the link reading, it is not
                          a separate health metric. */}
                      <StatTile
                        label="Link" value={s?.wifiRssi != null ? s.wifiRssi : null} unit="dBm"
                        sev={s?.wifiRssi == null ? 'ok' : s.wifiRssi > -70 ? 'ok' : s.wifiRssi > -80 ? 'warn' : 'bad'}
                        pct={s?.wifiRssi == null ? null : Math.max(0, Math.min(100, (s.wifiRssi + 95) / 35 * 100))}
                        glyph={<SignalBars rssi={s?.wifiRssi ?? null}/>}
                        sub={[wifiBand(s?.wifiFreqMhz),
                              s?.linkSpeedMbps ? `${s.linkSpeedMbps} Mbps` : null]
                             .filter(Boolean).join(' · ') || null}
                      />
                      {/* Amber past 200ms, red past 1s — the same thresholds the
                          RTT instrumentation counts excursions against. */}
                      <StatTile
                        label="Latency" value={device.rttMs != null ? device.rttMs : null} unit="ms"
                        sev={device.rttMs == null ? 'ok' : device.rttMs >= 1000 ? 'bad' : device.rttMs >= 200 ? 'warn' : 'ok'}
                        pct={device.rttMs == null ? null : Math.min(100, device.rttMs / 500 * 100)}
                      />
                      {/* Scaled 20-90C so the meter shows real headroom; this
                          SoC idles ~33C. thermalCoreLimit below the core count
                          means the governor is already capping capacity, which
                          bites before any temperature looks alarming — so that
                          is the one thing in this row allowed to shout. */}
                      <StatTile
                        label="Temp" value={tempC != null ? tempC.toFixed(1) : null} unit="°C"
                        sev={tempC == null ? 'ok' : tempC >= 85 ? 'bad' : tempC >= 70 ? 'warn' : 'ok'}
                        pct={tempC == null ? null : Math.max(0, Math.min(100, (tempC - 20) / 70 * 100))}
                        note={throttled ? `throttled ${s.thermalCoreLimit}/${s.coresTotal}` : null}
                        glyph={tempHot != null && tempC != null && tempHot > tempC + 1
                          ? <span style={{ fontFamily:"'DM Mono',monospace", fontSize:9, color:'var(--muted)' }}>{tempHot.toFixed(1)} max</span>
                          : null}
                      />
                    </div>
                    {!s && <div style={{ fontFamily:"'DM Mono',monospace", fontSize:9, color:'var(--muted)', marginTop:8 }}>waiting for device stats…</div>}
                  </Panel>
                </div>
                {device.bleProxy && (() => {
                  const b  = s?.ble || null;          // device-side scanner stats
                  const bp = device.bleProxy;         // controller-side proxy state
                  const haState = bp.haSubscribed ? 'Streaming to HA'
                    : bp.haConnected ? 'HA connected (not subscribed)'
                    : bp.listening ? 'Waiting for HA' : 'Port down (device offline)';
                  return (
                    <Panel label="Bluetooth proxy">
                      <div className="em-grid2" style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:'0 24px' }}>
                        <div>
                          {row('Scanner', b ? (b.scanning ? 'Scanning' : 'Stopped') : '—', b?.scanning ? 'var(--ok)' : undefined)}
                          {row('Adverts seen', b ? String(b.advertsSeen ?? 0) : '—')}
                          {row('Nearby devices (5 min)', b ? String(b.uniqueAddrs ?? 0) : '—')}
                          {row('BT address', b?.bdAddr || '—')}
                        </div>
                        <div>
                          {row('Home Assistant', haState, bp.haSubscribed ? 'var(--ok)' : undefined)}
                          {row('Forwarded to HA', String(bp.advertsForwarded ?? 0))}
                          {row('ESPHome port', String(bp.port))}
                          {/* Amber on any non-zero: reopening /dev/stpbt
                              re-initialises the combo radio WiFi shares, so
                              this is the first thing to check against an
                              unexplained link drop on this device. */}
                          {row('HCI errors / restarts',
                               b ? `${b.hciErrors ?? 0} / ${b.restarts ?? 0}` : '—',
                               b && ((b.hciErrors ?? 0) > 0 || (b.restarts ?? 0) > 0)
                                 ? 'var(--warn)' : undefined)}
                        </div>
                      </div>
                    </Panel>
                  );
                })()}
              </div>
            );
          })()}

          {/* ACTIVITY — voice-turn observability; its own tab (genuinely
              useful, was cramped at the bottom of Status). */}
          {tab === 'activity' && (() => {
            const cfgEff = effectiveConfig(globalConfig, device);
            const wwLabel = wwModelLabel(cfgEff.owwModel);
            return (
              <div style={{ minHeight:'100%', display:'flex', flexDirection:'column' }}>
                <Panel label={`Voice activity — ${wwLabel} @ ${cfgEff.owwThreshold != null ? cfgEff.owwThreshold.toFixed(2) : '—'}`} style={{ flex:1 }}>
                  <TurnObservability
                    turns={turns}
                    deviceId={device.device_id}
                    deviceLabel={device.label}
                    recordingsOn={cfgEff.saveUtterances}
                    nearMisses={device.owwNearMisses}
                    stateLabel={state.label.toUpperCase()}
                    stateColor={state.dot}
                    isAdmin={isAdmin}
                  />
                </Panel>
              </div>
            );
          })()}

          {/* CONFIG */}
          {tab === 'config' && (
            <div>
              {/* Network (WiFi) — always per-device, kept above and visually
                  separate from the fleet-inheritable config below. */}
              <div style={{ paddingBottom: 24, marginBottom: 24, borderBottom: '1px solid var(--line, var(--track))' }}>
                <ConnectivityTab device={device} row={row}/>
              </div>

              {/* Scoping summary. Each section below carries its own
                  Fleet/Device switch — this is just the roll-up plus a way
                  back to fully inheriting. */}
              {isAdmin && globalConfig && (
                <div style={{
                  display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12,
                  background: sections.length ? 'rgba(40,96,64,0.08)' : 'rgba(64,88,120,0.08)',
                  border: `1px solid ${sections.length ? 'rgba(40,96,64,0.2)' : 'rgba(64,88,120,0.2)'}`,
                  borderRadius: 8, padding: '12px 16px', marginBottom: 24, flexWrap: 'wrap',
                }}>
                  <div>
                    <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: 'var(--text2)' }}>
                      {sections.length
                        ? `Local override (${sections.length} of ${Object.keys(CONFIG_SECTIONS).length})`
                        : 'Following fleet config'}
                    </div>
                    <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--muted)', marginTop: 3 }}>
                      {sections.length
                        ? `Overriding: ${sections.map(s => SECTION_LABELS[s] || s).join(', ')} — everything else tracks the fleet`
                        : 'Switch any section below to Device to customise just that part'}
                    </div>
                  </div>
                  {sections.length > 0 && (
                    <Pill onClick={() => { setSections([]); setDirty(true); }}>
                      Revert all to fleet
                    </Pill>
                  )}
                </div>
              )}

              {/* Config form — each stage editable only when scoped to Device */}
              <DeviceConfigForm
                config={config}
                onChange={(k, v) => setConf(k, v)}
                disabled={!isAdmin}
                sections={sections}
                shadowCapable={!device.connected || !!device.owwShadowCapable}
                triggerCapable={!device.connected || !!device.owwTriggerCapable}
                mixCapable={!device.connected || !!device.audioMixCapable}
                holdCapable={!device.connected || !!device.buttonHoldCapable}
                hwEchoRef={device.connected && device.aecRef === 'hw'}
                hwRefCapable={!device.connected || !!device.aecHwRefCapable}
                onScopeChange={(id, local) => {
                  setSections(prev => local
                    ? [...prev, id]
                    : prev.filter(s => s !== id));
                  setDirty(true);
                }}
              />

              {isAdmin && dirty && (
                <div style={{ display: 'flex', gap: 10, marginTop: 24 }}>
                  <Pill accent disabled={saving} onClick={pushConfig}>
                    {saving ? 'Pushing…' : 'Push config'}
                  </Pill>
                  <Pill onClick={() => {
                    setConfig(effectiveConfig(globalConfig, device));
                    setSections(device.config_sections ?? []);
                    setDirty(false);
                  }}>Cancel</Pill>
                </div>
              )}
            </div>
          )}

          {/* CONSOLE — fills the whole tab frame */}
          {tab === 'console' && (
            device.connected
              ? <div style={{ height: '100%' }}><Shell deviceId={device.device_id} token={token} height="100%"/></div>
              : <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 12, color: 'var(--warn)' }}>Device offline — console unavailable</div>
          )}

          {/* UPDATES */}
          {tab === 'updates' && (
            <div style={{ minHeight:'100%', display:'flex', flexDirection:'column', gap:16 }}>

              {/* Firmware state */}
              <Panel label="Firmware">
                <div style={{ display:'flex', alignItems:'flex-end', justifyContent:'space-between', gap:16, flexWrap:'wrap' }}>
                  <div style={{ display:'flex', gap:16, alignItems:'flex-end' }}>
                    <Lcd label="On device"  value={device.firmware_ver || '—'} color={needsUpdate ? 'var(--lcd-amber)' : 'var(--lcd-green)'}/>
                    <Lcd label="Available"  value={release?.version || '—'} color="var(--lcd-dim)"/>
                    {device.firmware_previous && (
                      <Lcd label="Rollback slot" value={device.firmware_previous} color="var(--lcd-dim)"/>
                    )}
                  </div>
                  <div style={{ display:'flex', alignItems:'center', gap:12 }}>
                    <span style={{ fontFamily:"'DM Mono',monospace", fontSize:11, color: needsUpdate ? 'var(--warn)' : 'var(--ok)' }}>
                      {release?.version ? (needsUpdate ? `Update ${release.version} available` : 'Up to date') : 'No release info'}
                    </span>
                    {/* #159: with the background poll off, "No release info"
                        and a GitHub outage look identical from here — and
                        this tab is where someone comes to find out, so the
                        blank has to name its own cause or it reads as a
                        fault. Shown whatever the release state, because the
                        stale-by-design case ("Up to date", last checked in
                        March) is the one that misleads hardest. Inline
                        rather than a site-wide banner: it is a setting
                        somebody chose on purpose, and a persistent warning
                        for a deliberate choice trains people to ignore
                        warnings. */}
                    {!autoChecks && (
                      <span style={{ fontFamily:"'DM Mono',monospace", fontSize:11, color:'var(--lcd-dim)' }}
                            title="update_check_interval is 0 — the controller makes no background connection to GitHub. Check now still works.">
                        Auto-checks off
                      </span>
                    )}
                    <Pill small onClick={doCheckRelease} disabled={checkingRelease}>
                      {checkingRelease ? 'Checking…' : 'Check now'}
                    </Pill>
                  </div>
                </div>
                {/* Action bar, directly under the version it acts on.
                    "Push update" used to live in its own panel BELOW this one,
                    so reaching the button people come to this tab to press
                    meant scrolling past everything else — and putting release
                    notes above it made that worse. Reads top to bottom as
                    state, then act, then detail. */}
                <div style={{ display:'flex', alignItems:'center', gap:10, flexWrap:'wrap', marginTop:16 }}>
                  <Pill accent={device.connected && !pushing && needsUpdate}
                        disabled={!device.connected || pushing || !needsUpdate}
                        onClick={doUpdate}>
                    {pushing && !localFile ? 'Updating…'
                      : needsUpdate ? `Update to ${release?.version || 'latest'}` : 'Up to date'}
                  </Pill>
                  {device.firmware_previous && (
                    <Pill disabled={!device.connected || pushing} onClick={doRollback}>
                      Roll back to {device.firmware_previous}
                    </Pill>
                  )}
                  <span style={{ fontFamily:"'DM Mono',monospace", fontSize:9, color:'var(--muted)', lineHeight:1.5, flex:'1 1 220px', minWidth:0 }}>
                    A/B slots — the previous binary stays available, and the device
                    rolls itself back if an update fails to start.
                  </span>
                </div>

                {/* Release notes, collapsed to one line: a click from the
                    decision, never in front of the action. Same disclosure
                    idiom as the Advanced sections.

                    Preformatted rather than rendered markdown on purpose —
                    React and xterm are the only vendored libraries, and adding
                    a markdown renderer to style a release note is a poor
                    trade. Simply-written notes read fine as text, and the
                    GitHub link covers the rest. */}
                {needsUpdate && release?.notes && (
                  <div style={{ marginTop:14, borderTop:'1px solid var(--hairline)', paddingTop:10 }}>
                    <div onClick={() => setNotesOpen(o => !o)} style={{
                      fontFamily:"'DM Mono',monospace", fontSize:9, color:'var(--muted)',
                      textTransform:'uppercase', letterSpacing:'0.15em', cursor:'pointer',
                      userSelect:'none', display:'flex', alignItems:'center', gap:6,
                    }}>
                      <span>{notesOpen ? '▾' : '▸'}</span>
                      What&apos;s in {release.version}
                      {release.published_at && (
                        <span style={{ marginLeft:'auto', letterSpacing:0, textTransform:'none' }}>
                          {new Date(release.published_at).toLocaleDateString()}
                        </span>
                      )}
                    </div>
                    {notesOpen && (
                      <>
                        <pre style={{
                          fontFamily:"'DM Mono',monospace", fontSize:10, lineHeight:1.65,
                          color:'var(--text2)', whiteSpace:'pre-wrap', wordBreak:'break-word',
                          margin:'12px 0 0', maxHeight:320, overflowY:'auto',
                        }}>{release.notes}</pre>
                        {release.release_url && (
                          <a href={release.release_url} target="_blank" rel="noreferrer"
                             style={{ fontFamily:"'DM Mono',monospace", fontSize:9, color:'var(--muted)',
                                      display:'inline-block', marginTop:8 }}>
                            View release on GitHub →
                          </a>
                        )}
                      </>
                    )}
                  </div>
                )}
              </Panel>

              {/* The GitHub Release panel that used to sit here held one
                  button, which now lives beside the version state above.
                  Local Build remains as the developer path — correctly
                  secondary, and no longer competing for the top half. */}
              <div className="em-grid2" style={{ display:'grid', gridTemplateColumns:'1fr', gap:16 }}>
                <Panel label="Local Build">
                  <div style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--muted)', lineHeight:1.6, marginBottom:14 }}>
                    Deploy a binary compiled on your machine (device/build/server from compile.sh).
                  </div>
                  <input ref={fileInputRef} type="file" accept="*/*" style={{ display:'none' }}
                    onChange={e => setLocalFile(e.target.files[0] || null)}/>
                  <div style={{ display:'flex', gap:10, alignItems:'center', flexWrap:'wrap' }}>
                    <Pill small onClick={() => fileInputRef.current?.click()} disabled={pushing}>
                      {localFile ? '⇄ Change' : 'Choose file'}
                    </Pill>
                    {localFile && (
                      <>
                        <span style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--text2)', flex:1, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap', minWidth:0 }}>
                          {localFile.name} · {(localFile.size/1024).toFixed(0)} KB
                        </span>
                        <Pill small danger onClick={() => setLocalFile(null)} disabled={pushing}>✕</Pill>
                        <Pill small accent disabled={!device.connected || pushing} onClick={doLocalDeploy}>
                          {uploading ? 'Uploading…' : pushing ? 'Deploying…' : 'Deploy'}
                        </Pill>
                      </>
                    )}
                  </div>
                </Panel>
              </div>

              {/* Maintenance — device-side payloads that are not the firmware
                  binary. These used to sit on the Status tab beside Secure
                  link, which was the wrong home: Status describes what a device
                  IS, and re-applying a payload is something you DO. It belongs
                  next to deploy and rollback. */}
              {isAdmin && (
                <Panel label="Maintenance">
                  <div style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--muted)', lineHeight:1.6, marginBottom:14 }}>
                    Re-apply the debloat payloads: sync the boot script and hide any
                    Amazon package added to the list since this device was provisioned.
                    Runs automatically with every firmware update — this is for a device
                    already on the current firmware, which the automatic path never
                    reaches. Idempotent, and newly hidden packages stop at the next
                    device restart.
                  </div>
                  {/* Disabled WITH THE REASON on a device that is not running
                      Android, rather than offered as a control that quietly
                      achieves nothing. The payload is a package list and a
                      Magisk boot script; on a device with neither, the write
                      lands nowhere, the confirmation never comes, and the
                      transfer holds that device's shell lock until it times
                      out. `androidUserspace` is the server's own answer, not
                      a rule re-derived here — the endpoint refuses on the
                      same value. */}
                  <Pill small disabled={!device.connected || debloating || device.androidUserspace === false}
                        onClick={doDebloat}>
                    {debloating ? 'Applying…' : 'Re-apply debloat'}
                  </Pill>
                  {device.androidUserspace === false && (
                    <div style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--muted)', marginTop:10 }}>
                      Not available — this device is not running Android, so there is nothing to debloat.
                    </div>
                  )}
                </Panel>
              )}


              {/* On-device wake word assets.

                  Separate from firmware on purpose: the runtime is 12.3MB and
                  changes far less often than the binary, so it is not in the
                  OTA payload. That makes it invisible unless something says
                  so — a device can be configured for on-device scoring and
                  silently not be doing it, which is the failure this panel
                  exists to make legible. */}
              {isAdmin && device.owwShadowCapable && (
                <Panel label="On-device wake word">
                  <div style={{ display:'flex', alignItems:'center', gap:12, flexWrap:'wrap', marginBottom:12 }}>
                    <span style={{ fontFamily:"'DM Mono',monospace", fontSize:11,
                      color: !assets ? 'var(--muted)'
                           : assets.status === 'installed' ? 'var(--ok)'
                           : assets.status === 'blocked' ? 'var(--error)'
                           : assets.status === 'unknown' ? 'var(--muted)'
                           : 'var(--warn)' }}>
                      {!assets ? 'checking…'
                        : assets.status === 'installed' ? 'Installed'
                        : assets.status === 'outdated' ? 'Out of date'
                        : assets.status === 'blocked' ? 'Not enough space'
                        : assets.status === 'unknown' ? 'Device offline — state unknown'
                        : 'Not installed'}
                    </span>
                    {assets?.free_mb != null && (
                      <span style={{ fontFamily:"'DM Mono',monospace", fontSize:9, color:'var(--muted)' }}>
                        {assets.free_mb}MB free
                      </span>
                    )}
                  </div>
                  <div style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--muted)', lineHeight:1.6, marginBottom:14 }}>
                    The ONNX runtime and wake models the device needs to score
                    locally (~15MB). They are not part of the firmware, so they
                    install separately and survive updates. Scoring only starts
                    after a device restart.
                  </div>
                  {assets?.blocked && (
                    <div style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--error)', marginBottom:12 }}>
                      {assets.blocked}
                    </div>
                  )}
                  {(assets?.problems || []).map((p, i) => (
                    <div key={i} style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--warn)', marginBottom:8 }}>
                      {p}
                    </div>
                  ))}
                  <Pill small
                        disabled={!device.connected || installing || assets?.status === 'installed'}
                        onClick={doInstallAssets}>
                    {installing ? 'Installing…'
                      : assets?.status === 'installed' ? 'Installed'
                      : assets?.status === 'outdated' ? 'Update assets'
                      : 'Install assets'}
                  </Pill>
                  {!installing && assets?.missing?.length > 0 && (
                    <span style={{ fontFamily:"'DM Mono',monospace", fontSize:9, color:'var(--muted)', marginLeft:10 }}>
                      {assets.missing.length} file(s) to send
                    </span>
                  )}

                  {/* Progress. Determinate whenever the GET told us what is
                      missing; indeterminate only in the brief window before
                      the first file is announced. */}
                  {installing && (() => {
                    const sizes = Object.fromEntries((assets?.required || []).map(a => [a.name, a.size]));
                    const total = (assets?.missing || []).reduce((n, nm) => n + (sizes[nm] || 0), 0);
                    const pct   = total > 0 ? Math.min(100, Math.round(assetSent / total * 100)) : 0;
                    return (
                      <div style={{ marginTop: 14 }}>
                        <div style={{ display:'flex', justifyContent:'space-between', alignItems:'baseline', marginBottom:6 }}>
                          <span style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--text2)' }}>
                            {assetNow ? `Sending ${assetNow}…` : 'Working…'}
                          </span>
                          {total > 0 && (
                            <span style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--muted)' }}>
                              {(assetSent/1024/1024).toFixed(1)} / {(total/1024/1024).toFixed(1)} MB
                            </span>
                          )}
                        </div>
                        <div style={{ height:6, borderRadius:3, background:'var(--lcd-bg)', border:'1px solid var(--lcd-line)', overflow:'hidden', boxShadow:'inset 0 1px 3px rgba(0,0,0,0.5)' }}>
                          {/* No size known yet — fill it dimly rather than
                              animate, which would need a keyframe this
                              stylesheet does not have and would overstate
                              how much the bar knows. */}
                          <div style={{ height:'100%', width:`${total > 0 ? pct : 100}%`,
                                        background: total > 0 ? 'linear-gradient(90deg,var(--accent-hi),var(--accent-lit))' : 'var(--accent-deep)',
                                        transition:'width 0.3s ease' }}/>
                        </div>
                      </div>
                    );
                  })()}

                  {/* The controller's own narration, in place of an alert().
                      Kept after the run so a failure can be read, not just
                      acknowledged and lost. */}
                  {assetLog.length > 0 && (
                    <div style={{ marginTop:12, maxHeight:120, overflowY:'auto', background:'var(--lcd-bg)', border:'1px solid var(--lcd-line)', borderRadius:6, padding:'8px 10px', boxShadow:'inset 0 2px 4px rgba(0,0,0,0.5)' }}>
                      {assetLog.map((l, i) => (
                        <div key={i} style={{ fontFamily:"'DM Mono',monospace", fontSize:10, lineHeight:1.6,
                          color: l.level === 'error' ? 'var(--error)' : l.level === 'warn' ? 'var(--warn)' : 'var(--lcd-green)' }}>
                          {l.text}
                        </div>
                      ))}
                    </div>
                  )}

                  {assetResult && (
                    <div style={{ marginTop:12, display:'flex', gap:8, alignItems:'flex-start',
                                  border:`1px solid ${assetResult.ok ? 'var(--ok)' : 'var(--error)'}`,
                                  background: assetResult.ok ? 'rgba(40,96,64,0.10)' : 'rgba(154,48,32,0.10)',
                                  borderRadius:6, padding:'10px 12px' }}>
                      <span style={{ fontFamily:"'DM Mono',monospace", fontSize:12, color: assetResult.ok ? 'var(--ok)' : 'var(--error)' }}>
                        {assetResult.ok ? '●' : '✕'}
                      </span>
                      <span style={{ fontFamily:"'DM Sans',sans-serif", fontSize:12, color:'var(--text2)', lineHeight:1.5 }}>
                        {assetResult.text}
                      </span>
                    </div>
                  )}
                </Panel>
              )}

              {/* Activity console — always present so the layout never jumps
                  when a deploy starts */}
              <div className="em-inset" style={{ '--em-inset-pad':'14px', fontFamily:"'DM Mono',monospace", fontSize:12, minHeight:96, flex:1 }}>
                {pushLog.length === 0 && !pushing && (
                  <span style={{ color:'var(--lcd-faint)' }}>— no deploy activity this session —</span>
                )}
                {pushLog.map((line, i) => (
                  <div key={i} style={{
                    color: line.startsWith('✓') ? 'var(--lcd-green)'
                         : line.startsWith('⚠') ? 'var(--warn)'
                         : line.startsWith('Error') ? 'var(--error)'
                         : 'var(--lcd-faint)',
                    marginBottom:4,
                    textShadow: line.startsWith('✓') ? '0 0 8px rgba(140,200,100,0.4)' : 'none',
                  }}>{line}</div>
                ))}
                {pushing && <span style={{ color:'var(--lcd-faint)' }}>▌</span>}
              </div>
            </div>
          )}

          {/* LOGS */}
          {tab === 'logs' && (
            <div>
              <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.15em', marginBottom: 16 }}>Device logs</div>
              {logsLoading ? (
                <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 12, color: 'var(--muted)' }}>Loading…</div>
              ) : logs.length === 0 ? (
                <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 12, color: 'var(--muted)' }}>No logs</div>
              ) : logs.map((entry, i) => (
                <div key={i} style={{ display: 'flex', gap: 12, alignItems: 'baseline', padding: '8px 0', borderBottom: '1px solid var(--hairline)' }}>
                  <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--faint)', minWidth: 60, flexShrink: 0 }}>{new Date(entry.ts).toLocaleTimeString()}</span>
                  <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: eventAccent(entry.level), textTransform: 'uppercase', letterSpacing: '0.1em', minWidth: 48, flexShrink: 0 }}>{entry.level}</span>
                  <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: entry.source === 'device' ? 'var(--lcd-faint)' : 'var(--accent-deep)', textTransform: 'uppercase', letterSpacing: '0.08em', minWidth: 64, flexShrink: 0 }}>{entry.source}</span>
                  <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: 'var(--text2)' }}>{entry.message}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Device card ──────────────────────────────────────────────────────────────

function Card({ device, onClick }) {
  const state = deviceState(device);
  const isPending = !device.approved;

  return (
    <div onClick={onClick} style={{ background: 'linear-gradient(160deg,var(--card),var(--bg))', border: '1px solid var(--border)', borderRadius: 14, cursor: 'pointer', boxShadow: '0 4px 16px var(--track),0 1px 0 var(--sheen) inset', transition: 'box-shadow 0.15s,transform 0.1s', userSelect: 'none', opacity: isPending ? 0.85 : 1 }}
      onMouseEnter={e => { e.currentTarget.style.boxShadow = '0 8px 28px rgba(0,0,0,0.18),0 1px 0 var(--sheen) inset'; e.currentTarget.style.transform = 'translateY(-1px)'; }}
      onMouseLeave={e => { e.currentTarget.style.boxShadow = '0 4px 16px var(--track),0 1px 0 var(--sheen) inset'; e.currentTarget.style.transform = 'translateY(0)'; }}>
      <div style={{ background: 'linear-gradient(180deg,var(--sunken),var(--sunken))', borderBottom: '1px solid var(--border-hard)', borderRadius: '13px 13px 0 0', padding: '10px 16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', boxShadow: '0 1px 0 var(--sheen) inset' }}>
        <span style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 14, color: 'var(--text)', fontWeight: 600, letterSpacing: '-0.01em' }}>
          {device.label || <span style={{ color: 'var(--muted)', fontSize: 12 }}>{device.device_id.slice(0, 8)}…</span>}
        </span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          {isPending && (
            // Chrome sized this box off the DM Mono line box rather than the
            // glyphs, so 1px symmetric padding rendered visibly bottom-heavy
            // next to the 14px label. inline-flex + lineHeight:1 makes the
            // height the text's own; the trimmed paddingRight cancels the
            // trailing letter-space Chrome leaves after the final N, which is
            // what made the word look shunted left inside its own badge.
            <div style={{ display: 'inline-flex', alignItems: 'center', background: 'linear-gradient(160deg,var(--lcd-face),var(--lcd-deep))', border: '1px solid var(--lcd-line)', borderRadius: 3, padding: '3px 6px', paddingRight: 'calc(6px - 0.1em)', fontFamily: "'DM Mono',monospace", fontSize: 9, lineHeight: 1, color: 'var(--accent-lit)', letterSpacing: '0.1em' }}>PENDING</div>
          )}
          {!isPending && device.firmware_ver && (
            <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)' }}>{device.firmware_ver}</div>
          )}
        </div>
      </div>
      <div style={{ display: 'flex', justifyContent: 'center', padding: '20px 0 12px' }}>
        <LedRing state={state} size={120}/>
      </div>
      <div style={{ padding: '0 16px 16px' }}>
        <div className="em-inset" style={{ '--em-inset-radius':'6px', '--em-inset-pad':'7px 12px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: state.dot, letterSpacing: '0.12em', textShadow: `0 0 8px ${state.dot}88` }}>{state.label.toUpperCase()}</span>
          <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--lcd-dim)', letterSpacing: '0.08em' }}>{(() => {
            const ip = device.ip && device.ip !== '127.0.0.1' ? device.ip : null;
            return device.connected ? (ip || '—') : (ip ? `${ip} ↑` : '—');
          })()}</span>
        </div>
      </div>
    </div>
  );
}

// ─── Provisioning Wizard ──────────────────────────────────────────────────────

// ADB-over-WebUSB client — thin wrapper around @yume-chan/adb 2.1.0.
// Lazy-loads from esm.sh on first use (dynamic import works in classic scripts).
// Exposes the same interface the wizard step runners expect:
//   Client.requestDevice() -> client
//   client.connect()
//   client.shell(cmd)   -> string
//   client.push(path, Uint8Array, onProgress?)
//   client.pull(path)   -> Uint8Array
//   client.close()
const _ADB = (() => {
  // Module cache — loaded once on first requestDevice() call.
  let _mods = null;

  async function _load(logFn) {
    if (_mods) return _mods;
    logFn('Loading ADB library from esm.sh…');
    const [webUsbMod, adbMod] = await Promise.all([
      import('https://esm.sh/@yume-chan/adb-daemon-webusb@2.1.0?bundle&deps=@yume-chan/adb@2.1.0'),
      import('https://esm.sh/@yume-chan/adb@2.1.0?bundle'),
    ]);
    _mods = {
      manager:       webUsbMod.AdbDaemonWebUsbDeviceManager,
      Transport:     adbMod.AdbDaemonTransport,
      Adb:           adbMod.Adb,
      defaultAuths:  adbMod.ADB_DEFAULT_AUTHENTICATORS,
    };
    logFn('ADB library loaded.');
    return _mods;
  }

  // Drain a WHATWG ReadableStream<Uint8Array> into a single Uint8Array.
  async function _readAll(stream) {
    const reader = stream.getReader();
    const chunks = [];
    let total = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      total += value.length;
    }
    const out = new Uint8Array(total);
    let off = 0;
    for (const c of chunks) { out.set(c, off); off += c.length; }
    return out;
  }

  // Track the last usbDevice so we can release it before reconnecting.
  let _lastUsbDevice = null;

  class Client {
    constructor(adb, transport, banner, serial) {
      this._adb = adb;
      this._transport = transport;
      this.banner = banner;  // product name string, e.g. "omni_biscuit" or "csm_biscuit"
      // Carried so a WebUSB 'disconnect' event can be matched to this client
      // rather than to any other USB device the operator happens to unplug.
      this.serial = serial ?? null;
      this._log = () => {};
    }

    // Spawn a command and return its stdout as a trimmed string.
    // Must use noneProtocol — shellProtocol requires Android 7+.
    async shell(cmd) {
      const proc = await this._adb.subprocess.noneProtocol.spawn(cmd);
      const out = await _readAll(proc.output);
      return new TextDecoder().decode(out).replace(/\r\n/g, '\n').trim();
    }

    // Push bytes to a remote path via `cat >`.
    // stdin is a WritableStream<Uint8Array>; we write in 64 KB chunks.
    async push(remotePath, data, onProgress) {
      const bytes = data instanceof Uint8Array ? data : new Uint8Array(data);
      // The per-phase lines exist to localise a stall — which phase it hung in
      // is the whole diagnostic — but they are four lines per push, and a
      // provision makes eight sub-megabyte pushes that complete instantly.
      // Narrate the transfers that can actually stall; one line for the rest.
      const chatty = bytes.length >= 1024 * 1024;
      if (chatty) this._log(`push: opening cat > '${remotePath}' (${(bytes.length/1024/1024).toFixed(1)} MB)`);
      const proc  = await this._adb.subprocess.noneProtocol.spawn(`cat > '${remotePath}'`);
      if (chatty) this._log('push: stream open, writing chunks…');
      const writer = proc.stdin.getWriter();
      const SZ = 64 * 1024;
      for (let i = 0; i < bytes.length; i += SZ) {
        await writer.write(bytes.subarray(i, Math.min(i + SZ, bytes.length)));
        onProgress?.((i + SZ) / bytes.length);
      }
      if (chatty) this._log('push: all chunks written, closing stdin…');
      await writer.close();
      onProgress?.(1);
      this._log(chatty ? 'push: done.'
                       : `push: '${remotePath}' (${(bytes.length/1024).toFixed(0)} KB) done.`);
      // No drain — busybox cat on TWRP does not close stdout when stdin closes,
      // so _readAll would hang forever. The next shell command provides sequencing.
    }

    // Pull a remote file as a Uint8Array via `cat`.
    async pull(remotePath) {
      this._log(`pull: cat '${remotePath}'`);
      const proc = await this._adb.subprocess.noneProtocol.spawn(`cat '${remotePath}'`);
      this._log('pull: draining output…');
      const out = await _readAll(proc.output);
      this._log(`pull: done (${(out.length/1024/1024).toFixed(1)} MB)`);
      return out;
    }

    async close() {
      try { await this._transport.close(); } catch {}
    }

    // ── Static factory ──────────────────────────────────────────────────────

    // Open the browser USB picker, load the library, authenticate, return a
    // ready Client.  logFn is optional — wizard passes addLog.
    static async requestDevice(logFn = () => {}) {
      if (!navigator.usb) {
        // Name the origin. Chrome's insecure-origin allowlist is per-origin
        // and matches on scheme, host and port exactly, so someone who has
        // already allowlisted the standalone dashboard gets no benefit here
        // and has no way to tell why — the add-on's page is served from
        // Home Assistant's origin, not the controller's.
        const origin = window.location.origin;
        throw new Error(
          `WebUSB not available — requires a secure context (HTTPS or localhost). ` +
          `This page is on ${origin}. ` +
          (isIngress()
            // Under the add-on there is no localhost route to offer:
            // _ingress_only_middleware rejects anything that is not the
            // Supervisor gateway, so suggesting a direct port would send
            // the user to a 403.
            ? `Serve Home Assistant over HTTPS, or add exactly ${origin} to ` +
              `chrome://flags/#unsafely-treat-insecure-origin-as-secure and ` +
              `relaunch the browser. An allowlist entry for the controller's ` +
              `own address does not cover this one.`
            : `Open the dashboard at http://localhost:8768, or add exactly ` +
              `${origin} to chrome://flags/#unsafely-treat-insecure-origin-as-secure ` +
              `and relaunch the browser.`)
        );
      }

      const { manager, Transport, Adb, defaultAuths } = await _load(logFn);

      // Release any previous connection — calling connect() on an already-claimed
      // interface hangs indefinitely. This happens on retry after a reboot.
      if (_lastUsbDevice) {
        try { await _lastUsbDevice.disconnect(); } catch {}
        _lastUsbDevice = null;
      }

      logFn('Requesting USB device — select the Echo Dot from the picker…');
      const usbDevice = await manager.BROWSER.requestDevice();
      if (!usbDevice) throw new Error('No device selected.');
      logFn(`Device selected: ${usbDevice.name ?? usbDevice.serial ?? 'unknown'}`);
      _lastUsbDevice = usbDevice;

      logFn('Opening USB connection…');
      const connection = await usbDevice.connect();

      logFn('Authenticating ADB…');
      const transport = await Transport.authenticate({
        serial:         usbDevice.serial ?? 'echomuse',
        connection,
        authenticators: defaultAuths,
      });
      logFn('ADB authenticated.');

      const adb = new Adb(transport);
      const banner = adb.banner?.product ?? '(unknown)';
      logFn(`Connected. Banner: ${banner}`);

      return new Client(adb, transport, banner, usbDevice.serial ?? null);
    }
  }

  return { Client };
})();

// ── AddDeviceTile ──

function AddDeviceTile({ onClick }) {
  const [hover, setHover] = useState(false);
  return (
    <div
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        border: `2px dashed ${hover ? 'var(--text2)' : 'var(--border-hard)'}`,
        // 12 -> 14 to match Card's corner. minHeight is only the floor for an
        // empty fleet; with any device present the grid row sets the height.
        borderRadius: 14, minHeight: 244, display: 'flex', flexDirection: 'column',
        alignItems: 'center', justifyContent: 'center', gap: 8, cursor: 'pointer',
        transition: 'border-color 0.15s, opacity 0.15s', opacity: hover ? 1 : 0.6,
        userSelect: 'none',
      }}
    >
      <div style={{ fontSize: 28, color: hover ? 'var(--text2)' : 'var(--border-hard)', lineHeight: 1 }}>+</div>
      <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: hover ? 'var(--text2)' : 'var(--border-hard)', letterSpacing: '0.12em', textTransform: 'uppercase' }}>Provision Device</div>
    </div>
  );
}

// ── ProvisionWizard ──

const _ALEXA_PKGS = [
  'amazon.speech.davs.davcservice',
  'amazon.speech.sim',
  'com.amazon.alexa.beaconbroadcaster',
  'com.amazon.alexa.externalmediaplayer.fireos',
  'com.amazon.wha.mediabrowserservice',
  'com.amazon.whisperjoin.middleware',
  'com.amazon.whisperjoin.wss.wifiprovisioner',
  'com.amazon.device.smarthome.dshs.services',
  'com.amazon.mediaplayeragent',
  // Both proven on hardware to fight our manual wpa_supplicant.conf writes:
  // wifiprofilemanager re-asserts its own saved network profile through the
  // framework WifiManager path, silently overriding whatever we configure.
  'com.amazon.android.service.wifiprofilemanager',
  // smarthome's wifi adapter package — note pm disable alone does NOT stop
  // the native SmartHomeWifid binary (it's init-launched, not a Java
  // component), see persist.wifi.migrate.complete handling in
  // runDisableAlexa for the actual fix for that part.
  'com.amazon.device.smarthome.adapters.wifi',
];

// Steps that establish an ADB connection rather than needing one. They show
// "Retry Connection" on error, and they are the only steps runStep will enter
// without a live handle.
const CONNECT_STEPS = new Set([0, 1, 6]);

// Steps that carry operator-fixable input: a file picker or a field. These get
// different recovery copy ("Fix the input above") than a step that only needs
// a retry. Zero-indexed against _WIZARD_STEPS; keep them in sync if a step is
// ever inserted.
const INPUT_STEPS = new Set([3, 10, 11]);

// Which mode each step has to run in.
//
// This matters because the Dot's only power source is the same micro-USB port
// carrying data, so pulling the cable POWERS IT OFF, and `reboot recovery` is
// a one-shot BCB flag. A replug is therefore a cold boot into Android, whatever
// phase the wizard thinks it is in. Reconnecting during the TWRP phase hands
// back an Android device that looks perfectly healthy.
//
// It is not a cosmetic mismatch. In Android `/dev/block/other-boot` resolves to
// boot_b, which holds amonet's B-slot unlock payload rather than a kernel, so
// retrying Patch Boot Image there would write over the unlock. The guard in
// runPatchBoot refuses that, and this exists so the operator finds out before
// they get as far as being refused.
const _STEP_MODE = {
  0: 'android',
  1: 'twrp', 2: 'twrp', 3: 'twrp', 4: 'twrp', 5: 'twrp',
  6: 'android', 7: 'android', 8: 'android', 9: 'android',
  10: 'android', 11: 'android', 12: 'android',
};

// TWRP is checked first: its banner is "omni_biscuit", which also contains
// "biscuit", so an Android-first test would call every TWRP device Android.
function _bannerMode(banner) {
  const b = (banner || '').toLowerCase();
  if (b.includes('omni') || b.includes('twrp') || b.includes('recovery')) return 'twrp';
  if (b.includes('csm') || b.includes('biscuit')) return 'android';
  return 'unknown';
}

const _MODE_NAME = { twrp: 'TWRP recovery', android: 'Android' };

const _INIT_RC_APPEND = `
service mixer /system/bin/sh
    oneshot
    disabled
    user root

service echomuse /data/local/bin/start_server.sh
    user root
    group root system
    class late_start
`;

// Known-good Magisk release for this device/Android version. Checked
// against the uploaded file's SHA-256 before flashing — catches wrong-
// version uploads (e.g. a newer Magisk that doesn't support Android 5.1's
// non-namespaced su, or a corrupted download) before they hit TWRP.
// The one FireOS 5 build EchoMuse is developed and tested against. R0rt1z2's
// thread lists five older ones that also boot on an unlocked Dot, and nothing
// stops someone flashing those — but only this one has ever been through the
// wizard here, and firmware defaults differ between builds. A device on a
// different build is the first thing worth knowing when it behaves oddly, and
// until now the wizard never even looked (see #79).
//
// A warning, not a refusal: an older build may well provision fine, we just
// have no evidence either way, and blocking someone whose device works would
// be the worse error. Mirrored in docs/rooting.md, pinned by test.
const _TESTED_FIREOS_BUILD = '272.6.8.0_user_680767620';
const _TESTED_FIREOS_NAME  = 'Fire OS 5.5.5.4';

// WiFi security labels, used in the network picker and in error messages.
// Module scope so WifiPanel and the wizard's step runners share one set.
const _SECURITY_LABEL = {
  wpa2: 'WPA2', wpa3: 'WPA3', open: 'Open', wep: 'WEP', enterprise: 'Enterprise',
};

const _MAGISK_FILENAME = 'Magisk-v17.3.zip';
const _MAGISK_SHA256    = '18e46b16b25ebe691c282fe311beccd4811cd533848a64e2efbd754fb85efde7';

async function _sha256Hex(buf) {
  const digest = await crypto.subtle.digest('SHA-256', buf);
  return Array.from(new Uint8Array(digest)).map(b => b.toString(16).padStart(2, '0')).join('');
}

// Steps:
//  0  connect_android  — connect in Android mode, verify FireOS 5, reboot to recovery
//  1  connect_twrp     — reconnect once TWRP menu appears
//  2  patch_boot       — SELinux cmdline + init.rc in one boot image pass  [auto]
//  3  install_magisk   — flash Magisk 17.3 via twrp install                [file]
//  4  preseed_db       — push pre-seeded magisk.db                         [auto]
//  5  reboot           — reboot device to Android                          [button]
//  6  reconnect        — reconnect ADB after Android boots                 [button]
//  7  verify_root      — confirm su works                                  [auto]
//  8  disable_alexa    — silence OOBE + pm disable BEFORE wifi            [auto]
//  9  debloat          — pm hide bloat pkgs + service.d daemon-stop script [auto]
// 10  wifi             — configure WiFi network                            [inputs]
// 11  install_em       — push binary + startup script                      [file]
const _WIZARD_STEPS = [
  { id: 'connect_android', label: 'Connect Device',     desc: 'Connect the Echo Dot via USB. Device should be on and booted into Android. Appears as "AEOBC" in the USB picker.' },
  { id: 'connect_twrp',    label: 'Connect to TWRP',   desc: 'Wait for TWRP recovery to appear, then reconnect. Appears as "Echo" in the USB picker.' },
  { id: 'patch_boot',      label: 'Patch Boot Image',  desc: 'Apply SELinux permissive patch and add init.rc service entries.' },
  { id: 'install_magisk',  label: 'Install Magisk',    desc: 'Flash Magisk 17.3 for persistent root access.' },
  { id: 'preseed_db',      label: 'Pre-seed Root DB',  desc: 'Grant root to ADB shell without a screen prompt.' },
  { id: 'reboot',          label: 'Reboot to Android', desc: 'Reboot device to Android.' },
  { id: 'reconnect',       label: 'Reconnect',         desc: 'Re-connect ADB as soon as the device appears as "AEOBC" in the USB picker — no need to wait for it to finish booting, the next step does that.' },
  { id: 'verify_root',     label: 'Verify Root',       desc: 'Confirm Magisk root is working.' },
  { id: 'disable_alexa',   label: 'Disable Alexa',     desc: 'Silence the Amazon setup assistant and disable the Alexa voice pipeline, before the device ever reaches WiFi.' },
  { id: 'debloat',         label: 'Debloat',           desc: 'Hide non-essential Amazon packages and stop background daemons (~130MB RAM freed).' },
  { id: 'wifi',            label: 'Configure WiFi',    desc: 'Connect the device to your local WiFi network.' },
  { id: 'install_em',      label: 'Install EchoMuse',  desc: 'Push server binary and startup script to device.' },
  // Mandatory, not skippable. Every provisioned device carries the runtime,
  // which removes a whole class of "I enabled on-device wake word and nothing
  // happened" — the assets are not in the firmware, so without this step the
  // capability is advertised by a device that cannot actually use it. USB is
  // also much better suited to 15MB than the shell plane.
  { id: 'install_oww',     label: 'Wake Word Assets',  desc: 'Push the ONNX runtime and wake models (~15MB) used for on-device wake word detection.' },
];

// Transcript styling only — copy uses e.msg verbatim.
function _wizardLogClass(msg, type) {
  if (type === 'head') return 'em-console__line--head';
  if (type === 'error') return 'em-console__line--error';
  if (type === 'ok') return 'em-console__line--ok';
  if (type === 'warn') return 'em-console__line--warn';
  if (/^Waiting for|still waiting on|^\s+\[\d+s\]/.test(msg)) return 'em-console__line--wait';
  if (/^  adb:/.test(msg)) return 'em-console__line--adb';
  if (/^Error:/.test(msg)) return 'em-console__line--error';
  if (/^  →/.test(msg)) return 'em-console__line--detail';
  return 'em-console__line--info';
}

// ── WifiPanel ──

function WifiPanel({ adb, wifiSsid, setWifiSsid, wifiPsk, setWifiPsk, onScan, networks, onConnect, onSkip, onAbort }) {
  const [scanning, setScanning] = useState(false);
  const [showPsk, setShowPsk]   = useState(false);

  async function doScan() {
    setScanning(true);
    await onScan();
    setScanning(false);
  }

  return (
    <div style={{ marginBottom: 12, display: 'flex', flexDirection: 'column', gap: 10 }}>

      {/* Scan row */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <Pill small onClick={doScan} disabled={scanning || !adb}>
          {scanning ? 'Scanning…' : 'Scan for networks'}
        </Pill>
        {networks.length > 0 && (
          <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)' }}>
            {networks.length} network{networks.length !== 1 ? 's' : ''} found
          </span>
        )}
      </div>

      {/* Network list */}
      {networks.length > 0 && (
        <div style={{
          border: '1px solid var(--border-soft)', borderRadius: 6, overflow: 'hidden',
          maxHeight: 140, overflowY: 'auto',
        }}>
          {networks.map(n => {
            // A network the radio cannot join is shown, greyed, with the
            // reason. Hiding it would leave someone hunting for a network
            // they can see on their phone; offering it silently costs them
            // twenty seconds of SCANNING and no explanation.
            const blocked = n.blocker;
            return (
            <div key={n.ssid}
              onClick={() => { if (!blocked) setWifiSsid(n.ssid); }}
              title={blocked || ''}
              style={{
                padding: '6px 10px', display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                background: wifiSsid === n.ssid ? 'rgba(64,88,120,0.12)' : 'transparent',
                borderBottom: '1px solid var(--sunken)',
                cursor: blocked ? 'not-allowed' : 'pointer',
                opacity: blocked ? 0.5 : 1,
              }}>
              <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: wifiSsid === n.ssid ? 'var(--accent)' : 'var(--text)' }}>
                {n.ssid}
              </span>
              <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)' }}>
                {[n.securityLabel, (n.bands || []).join('+'), `${n.signal} dBm`]
                  .filter(Boolean).join(' · ')}
              </span>
            </div>
          );})}
        </div>
      )}

      {/* Manual SSID entry */}
      <div>
        <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--text2)', letterSpacing: '0.08em', marginBottom: 4 }}>SSID</div>
        <input
          type="text" value={wifiSsid} onChange={e => setWifiSsid(e.target.value)}
          placeholder="Select above or type network name"
          style={{ width: '100%', boxSizing: 'border-box' }}
        />
      </div>

      {/* Password */}
      <div>
        <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--text2)', letterSpacing: '0.08em', marginBottom: 4 }}>PASSWORD</div>
        <div style={{ display: 'flex', gap: 6 }}>
          <input
            type={showPsk ? 'text' : 'password'} value={wifiPsk} onChange={e => setWifiPsk(e.target.value)}
            placeholder="WPA passphrase" style={{ flex: 1, boxSizing: 'border-box' }}
            onKeyDown={e => e.key === 'Enter' && wifiSsid && onConnect()}
          />
          <button onClick={() => setShowPsk(v => !v)} style={{
            background: 'var(--hairline)', border: '1px solid var(--border-soft)', borderRadius: 6,
            fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)',
            padding: '0 8px', cursor: 'pointer', flexShrink: 0,
          }}>{showPsk ? 'hide' : 'show'}</button>
        </div>
      </div>

      {/* Actions */}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <Pill accent onClick={onConnect} disabled={!wifiSsid || !adb}>Connect</Pill>
        <Pill small onClick={onSkip}>Skip (already connected)</Pill>
        <Pill small danger onClick={onAbort}>Abort provisioning</Pill>
      </div>
    </div>
  );
}

function ProvisionWizard({ token, onClose, knownDevices }) {
  const [step, setStep]         = useState(0);
  const [stepState, setStepState] = useState(_WIZARD_STEPS.map(() => 'pending'));
  const [log, setLog]           = useState([]);
  const [running, setRunning]   = useState(false);
  const [adb, setAdb]           = useState(null);
  const [magiskFile, setMagiskFile] = useState(null);
  const [binaryFile, setBinaryFile] = useState(null);
  const [wifiSsid, setWifiSsid] = useState('');
  const [wifiPsk, setWifiPsk]   = useState('');
  const [wifiNetworks, setWifiNetworks] = useState([]);
  const [duplicateDeviceId, setDuplicateDeviceId] = useState(null);
  const [progress, setProgress] = useState(null);
  const [latestRelease, setLatestRelease] = useState(null);
  const [checkingRelease, setCheckingRelease] = useState(false);
  const [diagnostics, setDiagnostics] = useState(null);
  const [waiting, setWaiting] = useState(false);
  const logRef = useRef(null);
  // Bumped whenever the step in flight is abandoned — by the cable being
  // pulled, or by the operator cancelling. A step that later settles compares
  // this against the value it captured and drops its result on the floor
  // rather than marking a step done that nobody is waiting on any more.
  const stepEpoch = useRef(0);
  // Set immediately before a teardown WE asked for. Four steps end by rebooting
  // the device, which drops USB, and without this the disconnect listener races
  // the rest of the step: observed on a real run, Connect Device succeeded, the
  // event landed while `running` was still true, and a step that had just
  // worked was marked failed with "the step was abandoned".
  const expectDisconnect = useRef(false);

  // Errors thrown by an in-flight transfer when the device goes away. These
  // race the disconnect event and can arrive first, in which case the catch in
  // runStep would report a WebUSB internal as though it were a provisioning
  // failure and then go and probe the absent device for diagnostics.
  const _isDisconnectError = (e) =>
    /disconnect|transferOut|transferIn|NetworkError|device was lost/i.test(
      e?.message || '');

  function addLog(msg, type = 'info') {
    // 200 lines truncated a normal successful run — the transcript above the
    // fold is exactly the part you need when a late step fails for an early
    // reason, and it was being thrown away. A whole provision is ~300 lines,
    // so this holds several runs' worth; it is text in memory, not a cost
    // worth optimising.
    setLog(l => [...l, { msg, type }].slice(-5000));
    setTimeout(() => { if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight; }, 30);
  }
  function markStep(i, st) { setStepState(s => { const n = [...s]; n[i] = st; return n; }); }

  // Abandon whatever step is in flight.
  //
  // The ADB calls a step awaits do NOT reliably reject when the device goes
  // away: `shell` waits on a stream reader that simply never produces, so the
  // step neither resolves nor throws. `running` stays true, every button here
  // is gated on `!running`, and the wizard sits there looking busy with no
  // Retry, no Reconnect and no way out but reloading the page and losing the
  // transcript. Observed by pulling the cable during Configure WiFi.
  //
  // A timeout on `shell` would be the wrong instrument: waitForFramework
  // budgets ten minutes and `twrp install` takes thirty seconds or more, so
  // any timeout loose enough to be safe is too loose to be useful.
  // `idx` defaults to the step on screen, which is what the disconnect listener
  // wants. runStep passes its own index explicitly: the two are the same in
  // practice, but the caller that knows should say so rather than rely on it.
  function abandonStep(reason, idx = step) {
    stepEpoch.current++;
    setRunning(false);
    setWaiting(false);
    markStep(idx, 'error');
    addLog(reason, 'error');
  }

  // The browser knows the cable was pulled; nothing was listening.
  //
  // Matched on serial so unplugging an unrelated USB device does not abort a
  // provision. When either serial is unavailable the event is treated as ours:
  // a spurious abort costs a Retry, and a missed one costs the hang above.
  useEffect(() => {
    if (!navigator.usb) return;
    const onDisconnect = (e) => {
      if (expectDisconnect.current) {
        // A reboot we asked for. Consumed rather than left set, so the next
        // unexpected disconnect is still reported.
        expectDisconnect.current = false;
        return;
      }
      if (!adb && !running) return;
      const theirs = adb?.serial && e.device?.serialNumber
                   && e.device.serialNumber !== adb.serial;
      if (theirs) return;
      setAdb(null);
      if (running) {
        abandonStep('Device disconnected. The step was abandoned — reconnect and retry.');
      } else {
        addLog('Device disconnected.', 'warn');
      }
    };
    navigator.usb.addEventListener('disconnect', onDisconnect);
    return () => navigator.usb.removeEventListener('disconnect', onDisconnect);
  }, [adb, running, step]);

  // What to ask the device when a step fails (#87).
  //
  // Diagnosing #79 and #82 each took several rounds of asking the reporter to
  // run `getprop` and `wpa_cli` by hand, and by the time they answered the
  // device had usually been retried or rebooted, so the state at the moment of
  // failure was gone. This collects it while it is still true.
  //
  // Everything here is READ-ONLY and cheap. It runs on a device that has just
  // failed something, which is the worst moment to be issuing commands that
  // change anything, and it must not turn one failure into two.
  //
  // The keys must match `_PROVISION_PROBES` in em_support.py, which drops any
  // name it does not recognise — a probe added here and not there collects
  // output that is silently discarded. `tests/test_support.py` pins the pair.
  //
  // Sent RAW. The controller does the redaction, because those rules and their
  // tests live in em_support.py and a second copy here would drift from them
  // without anyone noticing until a file carried an SSID.
  const _PROVISION_PROBES = {
    props:         'getprop',
    root:          'su -c id 2>&1',
    selinux:       'getenforce 2>&1',
    // The two distinct shapes a too-early `pm` produces, which is what the
    // retry path keys off — worth capturing verbatim rather than as a verdict.
    pm_ready:      'pm path android 2>&1',
    storage:       'df /data 2>&1',
    wpa_status:    "su -c 'wpa_cli -p /data/misc/wifi/sockets -i wlan0 status' 2>&1",
    wpa_scan:      "su -c 'wpa_cli -p /data/misc/wifi/sockets -i wlan0 scan_results' 2>&1",
    wpa_caps:      "su -c 'wpa_cli -p /data/misc/wifi/sockets -i wlan0 get_capability key_mgmt' 2>&1",
    services:      'getprop | grep init.svc',
    processes:     "ps | grep -E 'wpa_supplicant|SmartHomeWifid' | grep -v grep",
    // Answers "did the package steps achieve anything", which is the case
    // that reports success having done nothing. It is also the exact question
    // #91 had to be asked by hand.
    packages:      "pm list packages 2>&1 | grep -i amazon",
    data_property: 'ls /data/property 2>&1',
    // TWRP steps only, and harmless elsewhere: which partition the boot image
    // write would have gone to.
    boot_target:   'readlink -f /dev/block/other-boot 2>&1',
  };

  async function collectProvisionDiagnostics(c, stepIdx, err) {
    const probes = {};
    for (const [name, cmd] of Object.entries(_PROVISION_PROBES)) {
      try {
        // Bounded per probe. The device has just failed something and may be
        // half gone; without this, one unanswered command hangs the whole
        // collection and the operator gets nothing at all.
        probes[name] = await Promise.race([
          c.shell(cmd),
          new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), 8000)),
        ]);
      } catch (e) {
        probes[name] = `<probe failed: ${e.message}>`;
      }
    }
    return API.post('/api/provision/diagnostics', {
      step:  _WIZARD_STEPS[stepIdx]?.id || String(stepIdx),
      error: err?.message || '',
      probes,
      transcript: log.map(l => l.msg),
      selected_ssid: wifiSsid || null,
    });
  }

  async function captureDiagnostics(stepIdx, err) {
    if (!adb) {
      // No connection means no probes, and a button that downloads a file
      // containing nothing but the error would be worse than no button.
      addLog('No ADB connection, so device state could not be captured.', 'warn');
      return;
    }
    addLog('Capturing device state for diagnostics…');
    try {
      setDiagnostics(await collectProvisionDiagnostics(adb, stepIdx, err));
      addLog('Device state captured — "Download diagnostics" below.', 'ok');
    } catch (e) {
      // Never let the diagnostic path bury the real failure.
      addLog(`Could not capture device state: ${e.error || e.message}`, 'warn');
    }
  }

  function downloadDiagnostics() {
    const blob = new Blob([JSON.stringify(diagnostics, null, 2)],
                          { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `echomuse-provision-${new Date().toISOString().slice(0,19).replace(/[:T]/g,'')}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function doCheckRelease() {
    setCheckingRelease(true);
    try {
      // Same force-check route as the dashboard's Updates tab — bypasses
      // the 60s in-memory cache and the (default 1h) DB cache that
      // /api/provision/latest_binary's underlying _get_cached_release()
      // would otherwise silently serve stale. This doesn't change what
      // "Install latest from GitHub" actually installs (that still goes
      // through the cache, now freshly populated by this call) — it just
      // shows the available version before committing to the install.
      const rel = await API.post('/api/releases/check', {});
      setLatestRelease(rel);
      addLog(`Latest GitHub release: ${rel.version}`, 'ok');
    } catch (e) {
      addLog(`Release check failed: ${e.error || e.message || 'unknown error'}`, 'error');
    }
    setCheckingRelease(false);
  }

  // ── Step runners ──

  async function runConnectAndroid() {
    // requestDevice() handles USB open + ADB auth in one call.
    const c = await _ADB.Client.requestDevice(addLog);
    c._log = msg => addLog(`  adb: ${msg}`);
    setAdb(c);
    const model   = await c.shell('getprop ro.product.model');
    const release = await c.shell('getprop ro.build.version.release');
    const name    = await c.shell('getprop ro.product.name');
    const serial  = await c.shell('getprop ro.serialno') || await c.shell('getprop ro.boot.serialno');
    const fwBuild = await c.shell('getprop ro.build.version.incremental');
    const fwName  = await c.shell('getprop ro.build.version.name');
    addLog(`Model: ${model || '(unknown)'}  Build: Android ${release}  Codename: ${name || '(unknown)'}  Serial: ${serial || '(unknown)'}`);
    addLog(`Firmware: ${fwName || '(unknown)'}  ${fwBuild || ''}`);
    if (!release.startsWith('5.')) {
      throw new Error(`Expected FireOS 5 (Android 5.x), got Android ${release}. Wrong device?`);
    }
    if (fwBuild && fwBuild !== _TESTED_FIREOS_BUILD) {
      addLog(`Untested firmware — EchoMuse is developed against ${_TESTED_FIREOS_NAME} `
           + `(${_TESTED_FIREOS_BUILD}). Other FireOS 5 builds may behave differently, `
           + `particularly around USB and ADB.`, 'warn');
    }
    if (model && !model.toLowerCase().includes('amazon') && !name.toLowerCase().includes('biscuit')) {
      addLog('Warning: device may not be an Echo Dot 2nd gen — proceeding anyway.', 'warn');
    }

    // Refuse to re-provision a device already known to the controller —
    // this flow reboots into recovery, flashes a patched boot image, and
    // is destructive to wipe through. Confirmed against em_api.py
    // _merge_device(): device_id is the only identifying field on the
    // device object, and it IS ro.serialno (set at registration time in
    // em_controller.py), not a separate serial/serial_number/id field.
    if (serial && knownDevices && knownDevices.length) {
      const match = knownDevices.find(d => d.device_id && d.device_id.includes(serial));
      if (match) {
        // Close the live ADB session before throwing — otherwise the
        // transport stays open and _lastUsbDevice keeps pointing at it.
        // On retry, requestDevice() disconnects the WebUSB interface but
        // the device-side adbd session was never told to close, so the
        // next Transport.authenticate() races a half-torn-down session
        // and hangs at "Authenticating ADB…". Mirrors the clean-exit
        // close()/setAdb(null) a few lines below.
        //
        // Closing the transport can surface as a USB disconnect, and this
        // path is about to throw a message the operator needs to read. The
        // listener must not overwrite it with "the step was abandoned".
        expectDisconnect.current = true;
        await c.close();
        setAdb(null);
        const err = new Error(
          `This device (serial ${serial}) appears to already be registered with the controller ` +
          `as "${match.label || match.device_id}". Delete it from the controller first ` +
          `if you want to re-provision, then retry.`
        );
        err.matchedDeviceId = match.device_id;
        throw err;
      }
    }

    addLog('FireOS 5 confirmed. Rebooting to TWRP recovery…');
    expectDisconnect.current = true;
    try { await c.shell('reboot recovery'); } catch {}
    await c.close();
    setAdb(null);
    addLog('Device is rebooting. Wait for the TWRP menu to appear, then click "Connect to TWRP".', 'warn');
    return null;
  }

  async function runConnectTwrp() {
    const c = await _ADB.Client.requestDevice(addLog);
    c._log = msg => addLog(`  adb: ${msg}`);
    setAdb(c);
    // TWRP on this device identifies itself via the ADB banner product name
    // ("omni_biscuit"), not via ro.bootmode or /sbin/recovery.
    // The banner is already logged by requestDevice; check it directly.
    const banner = c.banner ?? '';
    if (!banner.toLowerCase().includes('omni') && !banner.toLowerCase().includes('twrp') && !banner.toLowerCase().includes('recovery')) {
      throw new Error(`Device banner is "${banner}" — expected TWRP (omni_biscuit). Is TWRP showing on screen?`);
    }
    addLog('TWRP confirmed.', 'ok');
    return c;
  }

  // Where the patched kernel is allowed to land, decided from a probe of the
  // device rather than from trusting a symlink.
  //
  // This device has layers below FireOS that EchoMuse does not write: the
  // preloader, LK, and the partitions holding amonet's unlock payload. The
  // FireOS kernel and ramdisk live elsewhere, and a kernel written over the
  // payload costs the unlock and means running amonet again. So the one
  // partition write the wizard performs is checked against where it is about
  // to go rather than trusting the symlink to be pointing where it was last
  // time.
  //
  // THE BY-NAME MAP DIFFERS BETWEEN TWRP AND ANDROID, and a rule written
  // against one is wrong in the other. Measured on hardware 2026-08-08:
  //
  //            TWRP    Android
  //   boot_a          p10        p17
  //   boot_a_x        p10        p10
  //   boot_a_amonet   p17          -
  //
  // TWRP remaps the bare names onto the kernel partitions and exposes the
  // payload explicitly as *_amonet, which is the remapping R0rt1z2's thread
  // describes TWRP as doing for you. So under TWRP `boot_a` is SAFE and under
  // Android it is the payload. Match on the suffix, never on the bare name
  // alone, and read every alias of the target rather than one: p10 answers to
  // both boot_a and boot_a_x, so keeping a single name per partition makes the
  // verdict depend on glob order.
  //
  // An unresolvable target is refused rather than warned about: dd to a name
  // that is not a block device creates an ordinary file in TWRP's tmpfs, so
  // nothing reaches flash and the failure surfaces later as a confusing
  // magiskboot error on a zero-byte image.
  //
  // A target with no names at all is allowed through with a warning. Not being
  // able to read by-name is not evidence of danger, and refusing on it would
  // block any device whose TWRP lays that directory out differently — the same
  // reading the OTA free-space check applies to an unreadable df.
  function classifyBootTarget(probe) {
    const target = (probe.match(/TARGET=(\S*)/) || [])[1] || '';
    const isBlock = /ISBLK=yes/.test(probe);
    const names = [];
    for (const m of probe.matchAll(/^NAME (\S+) (\S+)$/gm)) {
      if (m[2] === target && !names.includes(m[1])) names.push(m[1]);
    }
    names.sort();
    const label = names.length ? `${names.join(', ')} (${target})` : target;

    if (!target) {
      return { ok: false, target, names, reason:
        '/dev/block/other-boot did not resolve to anything. TWRP normally creates it; '
        + 'without it there is nothing safe to write to.' };
    }
    if (!isBlock) {
      return { ok: false, target, names, reason:
        `/dev/block/other-boot resolves to "${target}", which is not a block device. `
        + 'Writing there would land in TWRP\'s tmpfs and never reach flash.' };
    }
    // The payload, named explicitly. Checked before the kernel test because a
    // partition carrying both names is not one we are willing to guess about.
    if (names.some(n => n.endsWith('_amonet'))) {
      return { ok: false, target, names, reason:
        `/dev/block/other-boot resolves to ${label}, which holds amonet's unlock payload, `
        + 'not the FireOS kernel. Writing there would cost you the unlock and mean running '
        + 'amonet again, so nothing has been written. The device is still in TWRP. Please '
        + 'report this with the line above: it is not a state the wizard has seen.' };
    }
    if (names.some(n => n.endsWith('_x'))) {
      return { ok: true, target, names, reason: label };
    }
    // No _x alias and no _amonet alias, but named boot_a/boot_b: this is the
    // Android-style map, where the bare name IS the payload. The wizard should
    // never see it, since it runs in TWRP, but being wrong here is expensive
    // and being cautious costs a re-run.
    if (names.some(n => n === 'boot_a' || n === 'boot_b')) {
      return { ok: false, target, names, reason:
        `/dev/block/other-boot resolves to ${label}, and there is no matching _x partition. `
        + 'Under that layout the bare name is amonet\'s payload rather than the FireOS '
        + 'kernel, so this is not somewhere to write a kernel. Refusing.' };
    }
    return { ok: true, warn: true, target, names, reason:
      `could not identify ${target} in by-name — continuing, but it is not a partition this `
      + 'has been checked against.' };
  }

  async function runPatchBoot(c) {
    addLog('Setting up work directories…');
    await c.shell('mkdir -p /tmp/work /tmp/bin');
    addLog('Extracting magiskboot from /sdcard/f1r30s.zip…');
    const unzipOut = await c.shell('unzip -o /sdcard/f1r30s.zip bin/magiskboot -d /tmp/ 2>&1');
    addLog(unzipOut || '(done)');
    await c.shell('chmod 755 /tmp/bin/magiskboot');

    addLog('Checking which partition the boot image lives in…');
    const probe = await c.shell(
      'd=$(readlink -f /dev/block/other-boot 2>/dev/null); echo "TARGET=$d"; '
      + 'if [ -b "$d" ]; then echo "ISBLK=yes"; else echo "ISBLK=no"; fi; '
      // Glob every boot_* rather than naming the four we expect: the payload
      // is only visible under TWRP as boot_a_amonet/boot_b_amonet, and a
      // fixed list cannot report a name it was not told to look for.
      + 'for n in /dev/block/platform/*/by-name/boot_*; do '
      + '[ -e "$n" ] && echo "NAME ${n##*/} $(readlink -f "$n" 2>/dev/null)"; done');
    const boot = classifyBootTarget(probe);
    if (!boot.ok) throw new Error(boot.reason);
    addLog(`  → ${boot.reason}`, boot.warn ? 'warn' : 'ok');

    addLog('Pulling boot image from device (10–20s)…');
    // stderr carried through rather than discarded: dd reports its record
    // counts there, and a silenced read failure used to reach magiskboot as
    // an empty file with nothing in the log to say why.
    const pullOut = await c.shell('dd if=/dev/block/other-boot of=/tmp/work/boot.img bs=1048576 2>&1');
    addLog(pullOut.trim() || '(done)');
    const bootImg = await c.pull('/tmp/work/boot.img');
    addLog(`Boot image: ${(bootImg.length / 1024 / 1024).toFixed(1)} MB`);
    // Refuse anything that is not an Android boot image before the byte-offset
    // cmdline patch below runs against it. That patch writes at a fixed header
    // offset and would happily corrupt whatever it was handed.
    const magic = new TextDecoder().decode(bootImg.slice(0, 8));
    if (magic !== 'ANDROID!') {
      throw new Error(
        `Read ${bootImg.length} bytes from ${boot.target} and it does not start with `
        + `"ANDROID!" (got "${magic.replace(/[^\x20-\x7e]/g, '.')}"). That is not a boot image, `
        + `so nothing is being patched or flashed.`);
    }

    // Check the CURRENT cmdline before touching anything — magiskboot's
    // own unpack log already echoes CMDLINE [...] for the unmodified
    // image, so use that as the source of truth instead of re-deriving
    // it from the manual byte-offset patch logic. If a previous wizard
    // run already flipped SELinux to permissive, re-running the blind
    // overwrite is unnecessary risk (another write to a device with no
    // real recovery path if it goes wrong) for zero benefit.
    addLog('Checking current boot image cmdline…');
    const probeOut = await c.shell('cd /tmp/work && /tmp/bin/magiskboot unpack boot.img 2>&1');
    addLog(probeOut || '(done)');
    const cmdlineAlreadyPermissive = probeOut.includes('androidboot.selinux=permissive');

    let workImg = 'boot.img';
    if (cmdlineAlreadyPermissive) {
      addLog('cmdline already has androidboot.selinux=permissive — skipping cmdline patch.', 'warn');
    } else {
      addLog('Patching cmdline for SELinux permissive…');
      const patched = new Uint8Array(bootImg);
      const newCmd  = new TextEncoder().encode('bootopt=64S3,32N2,64N2 androidboot.selinux=permissive');
      patched.fill(0, 64, 576);
      patched.set(newCmd, 64);

      addLog('Pushing patched image…');
      await c.push('/tmp/work/boot_patched.img', patched, pct => setProgress({ label: 'Pushing boot image', pct }));
      setProgress(null);
      workImg = 'boot_patched.img';

      addLog('Unpacking ramdisk…');
      const unpackOut = await c.shell(`cd /tmp/work && /tmp/bin/magiskboot unpack ${workImg} 2>&1`);
      addLog(unpackOut || '(done)');
    }
    // Either branch leaves /tmp/work/ramdisk.cpio in place — the probe
    // unpack above already extracted it from boot.img when cmdline was
    // already permissive, so no second unpack is needed in that case.
    await c.shell('mkdir -p /tmp/ramdisk && cd /tmp/ramdisk && cpio -id < /tmp/work/ramdisk.cpio 2>/dev/null');

    addLog('Patching init.csm.project.rc…');
    const rcBytes  = await c.pull('/tmp/ramdisk/init.csm.project.rc');
    const existing = new TextDecoder().decode(rcBytes);
    const rcAlreadyPatched = existing.includes('service echomuse');
    if (rcAlreadyPatched) {
      addLog('Service entries already present — skipping.', 'warn');
    } else {
      await c.push('/tmp/ramdisk/init.csm.project.rc', new TextEncoder().encode(existing + _INIT_RC_APPEND));
      await c.shell('chmod 750 /tmp/ramdisk/init.csm.project.rc');
    }

    if (cmdlineAlreadyPermissive && rcAlreadyPatched) {
      addLog('Boot image already fully patched — nothing to flash.', 'ok');
      return;
    }

    addLog('Repacking ramdisk…');
    await c.shell('cd /tmp/ramdisk && find . | cpio -o -H newc > /tmp/work/ramdisk.cpio 2>/dev/null');
    const repackOut = await c.shell(`cd /tmp/work && /tmp/bin/magiskboot repack ${workImg} 2>&1`);
    addLog(repackOut || '(done)');

    addLog(`Flashing patched boot image to ${boot.names.length ? `${boot.names.join(', ')} (${boot.target})` : boot.target}…`);
    const flashOut = await c.shell('dd if=/tmp/work/new-boot.img of=/dev/block/other-boot bs=1048576 2>&1');
    addLog(flashOut.trim() || '(done)');

    // Read the cmdline back off the partition rather than trusting dd's exit.
    // This is the write the device has to boot from next, and a bad one costs
    // a rollback and a boot attempt to discover — the same reasoning the OTA
    // path applies to md5 before it moves a symlink.
    const readback = await c.shell('dd if=/dev/block/other-boot bs=1 skip=64 count=512 2>/dev/null');
    if (!readback.includes('androidboot.selinux=permissive')) {
      throw new Error(
        `Flashed the patched image to ${boot.target} but reading it back does not show the `
        + `patched cmdline. The write did not take. Do not reboot the device: it is still in `
        + `TWRP and recoverable from here.`);
    }
    addLog('Boot image flashed and verified.', 'ok');
  }

  async function runInstallMagisk(c, file) {
    addLog(`Hashing ${file.name}…`);
    const buf = await file.arrayBuffer();
    const hash = await _sha256Hex(buf);
    addLog(`SHA256: ${hash}`);
    if (hash !== _MAGISK_SHA256) {
      throw new Error(
        `Hash mismatch — expected ${_MAGISK_SHA256.slice(0, 12)}… (${_MAGISK_FILENAME}), ` +
        `got ${hash.slice(0, 12)}… for "${file.name}". Wrong file or wrong Magisk version — ` +
        `not flashing. If you've intentionally updated the Magisk build, update _MAGISK_SHA256.`
      );
    }
    addLog('Hash verified.', 'ok');
    addLog(`Pushing ${file.name} to /sdcard/…`);
    await c.push(`/sdcard/${_MAGISK_FILENAME}`, new Uint8Array(buf),
      pct => setProgress({ label: 'Uploading Magisk', pct }));
    setProgress(null);
    addLog('Installing via TWRP (this takes ~30s)…');
    const out = await c.shell(`twrp install /sdcard/${_MAGISK_FILENAME} 2>&1`);
    addLog(out || '(done)');
    if (out.toLowerCase().includes('error') || out.toLowerCase().includes('failed')) {
      throw new Error('TWRP install reported an error — check the log.');
    }
    addLog('Magisk installed.', 'ok');
  }

  async function runPreseedDb(c) {
    // Clear any leftover Magisk state from a prior root install before
    // pushing the fresh DB. This device's own logs showed magiskd
    // rejecting every su call with "sqlite3_exec: no such table" against
    // a freshly-preseeded DB — but that exact preseed code has worked on
    // many prior FRESH-device provisions, so the DB content alone isn't
    // sufficient explanation. The actual differentiator on a re-provision
    // (boot image re-patched, Magisk re-flashed, but /data NOT wiped) is
    // that /data/adb/magisk.img — Magisk's own module/data image, separate
    // from magisk.db — survives from the old install. Per Magisk's own
    // docs, magisk.img gets merged/mounted at post-fs-data before the
    // daemon handles any su request; stale state there plausibly disrupts
    // magiskd's normal first-boot DB migration, leaving an incomplete
    // preseeded DB un-migrated. Rather than rely on that being the full
    // explanation, just clear both files unconditionally — a fresh
    // provision shouldn't inherit ANY prior Magisk state, full stop, same
    // principle as wiping server_a/server_b before a fresh EchoMuse
    // install. Scoped to magisk.db + magisk.img specifically, not the
    // whole /data/adb directory — TWRP's Magisk zip install (the previous
    // step) writes Magisk's own binaries/scripts under there too, and
    // there's no reason to risk interfering with that.
    //
    // NOTE: this step runs in the TWRP shell (no reconnect happens
    // between install_magisk and preseed_db — same session throughout),
    // where the shell is already root and there's no magiskd/su to broker
    // through yet (magiskd only starts once Android actually boots). Plain
    // rm, not `su -c rm` — matches every other command in runPatchBoot/
    // runInstallMagisk, which run in this identical TWRP context.
    addLog('Clearing any pre-existing Magisk state (magisk.db, magisk.img)…');
    await c.shell('mkdir -p /data/adb');
    const rmOut = (await c.shell('rm -f /data/adb/magisk.db /data/adb/magisk.img 2>&1')).trim();
    if (rmOut) addLog(`  → ${rmOut}`);
    addLog('Cleared.', 'ok');

    addLog('Downloading magisk.db from controller…');
    const resp = await fetch(ingressPath('/api/provision/magisk_db'), { headers: { Authorization: `Bearer ${token}` } });
    if (!resp.ok) throw new Error(`Controller returned ${resp.status}`);
    const dbBytes = new Uint8Array(await resp.arrayBuffer());
    addLog(`magisk.db: ${dbBytes.length} bytes`);
    await c.push('/tmp/magisk_preseed.db', dbBytes);
    await c.shell('cp /tmp/magisk_preseed.db /data/adb/magisk.db && chmod 600 /data/adb/magisk.db');
    addLog('magisk.db installed.', 'ok');
  }

  async function runReboot(c) {
    addLog('Sending reboot command…');
    expectDisconnect.current = true;
    try { await c.shell('reboot'); } catch {}
    await c.close();
    setAdb(null);
    // The old copy ("wait ~60s") made the operator responsible for guessing
    // when Android was ready, and a wrong guess used to sail straight past a
    // framework that wasn't. waitForFramework owns that question now, so the
    // only thing worth waiting for here is the device appearing over USB.
    addLog('Device rebooting to Android. Click Reconnect as soon as it appears in the USB picker — '
         + 'the wizard waits for Android to finish booting by itself.', 'warn');
    return null;
  }

  async function runReconnect() {
    const c = await _ADB.Client.requestDevice(addLog);
    c._log = msg => addLog(`  adb: ${msg}`);
    setAdb(c);
    addLog('ADB connected.', 'ok');
    return c;
  }

  // Re-establish ADB from ANY step, without running that step.
  //
  // Reconnecting used to be reachable only from the three connection steps, so
  // unplugging the cable anywhere else was unrecoverable inside the wizard:
  // the handle held in `adb` is dead, Retry hands that dead handle straight
  // back to the step, and the operator is left on a step that cannot succeed
  // with no way to get a working connection. Reported in #91 as being stuck on
  // step 9 with no way to press Retry or reach the USB picker.
  //
  // Unplugging a device is a normal thing to do when something has gone wrong,
  // so it must not be a state the wizard cannot leave.
  async function reconnectAdb() {
    setRunning(true);
    try {
      // Drop the old handle first. WebUSB will not hand out a second claim on
      // the same interface, so a stale client that still believes it is open
      // makes the picker fail with a permissions error that reads as though
      // the operator picked the wrong device.
      if (adb) { try { await adb.close(); } catch {} setAdb(null); }
      addLog('Reconnecting to the device…');
      const c = await runReconnect();
      // Say so when the device came back in the wrong mode. Without this the
      // reconnect looks like a success and the next Retry runs a TWRP step
      // against Android, or the reverse.
      const want = _STEP_MODE[step];
      const got  = _bannerMode(c.banner);
      if (want && got !== 'unknown' && got !== want) {
        addLog(`Reconnected in ${_MODE_NAME[got]}, but this step needs ${_MODE_NAME[want]} `
             + `(banner "${c.banner}").`, 'error');
        addLog('Pulling the cable powers the Dot off, and a cold boot comes up in Android.', 'warn');
        if (want === 'twrp') {
          addLog('To reach TWRP: unplug, plug back in, and hold the mute button for about '
               + '5 seconds as soon as the blue LED appears.', 'warn');
        }
      }
    } catch (e) {
      addLog(`Reconnect failed: ${e.message}`, 'error');
    }
    setRunning(false);
  }

  // The two distinct ways a too-early `pm` call fails on FireOS 5. The
  // friendly one is what you get before PMS is published; the NullPointer
  // is what you get once it IS published but has not finished initialising,
  // and it comes back as a raw stack trace out of the binder call rather
  // than anything resembling an error message. Matching only the first
  // string is why the retry path never fired on 2026-07-31.
  const _pmNotReady = (out) =>
    out.includes('Could not access the Package Manager')
    || out.includes('java.lang.NullPointerException');

  // What a `pm disable`/`pm hide` call actually said, in three outcomes.
  //
  // The distinction that matters is between pm REFUSING and pm ANSWERING that
  // the package is not there. `Unknown package: <name>` is an
  // IllegalArgumentException out of PackageManagerService: the call worked and
  // the package is simply not installed on this build. Treating that as a
  // failure is why a reporter on an image without these packages could never
  // finish the wizard, and was told to wait longer and retry, which could
  // never help (#91).
  //
  // Anything unrecognised counts as rejected rather than absent, deliberately:
  // the consequence of being wrong is continuing to WiFi with the Alexa stack
  // live, so an unfamiliar message is treated as the bad case.
  // Note the two success strings differ in shape, not just in word: `pm
  // disable` prints "new state: disabled" and `pm hide` prints "new hidden
  // state: true". Guessing at "new state: hidden" matches neither.
  const _pmVerdict = (out) => {
    if (out.includes('new state: disabled') || out.includes('hidden state: true')) return 'disabled';
    if (/Unknown package/i.test(out)) return 'absent';
    return 'rejected';
  };

  // Wait for the Android framework to be genuinely usable, and REFUSE to
  // continue if it isn't.
  //
  // The contract this owes the operator: Reconnect may be clicked the moment
  // the device shows up in the USB picker, and the wizard works out for
  // itself when Android is ready. No step downstream of here may require the
  // human to have guessed a long enough wait.
  //
  // Three lessons, the first two learned the expensive way on 2026-07-31:
  //
  //  1. `sys.boot_completed` can take far longer than 30s. The first boot
  //     after flashing a patched boot image and installing Magisk has to
  //     re-do work a normal boot doesn't, and adbd/magiskd both come up
  //     long before the system server does — so "adb connected" and even
  //     "su -c id works in 0.4s" say nothing about the framework. Poll for
  //     minutes, not seconds; the budget below assumes Reconnect was
  //     clicked at t=0 of the boot, because it is allowed to be.
  //  2. Timing out must be an ERROR, not a warning. The previous version
  //     logged "proceeding anyway" and carried on, which is how a run
  //     ended with all 11 `pm disable` and all 32 `pm hide` calls failing
  //     and both steps still reporting success. A step that cannot do its
  //     work must fail so the wizard shows Retry.
  //  3. A long wait must narrate itself. A silent poll is indistinguishable
  //     from a hung wizard, and the operator's reasonable response to a
  //     hung wizard — pull the cable, start over — is the worst available
  //     move mid-provision.
  //
  // boot_completed alone is also not sufficient evidence that the package
  // manager will answer, so probe it directly. A PMS that is published but
  // not yet initialised does not return the friendly "Could not access the
  // Package Manager" — it throws
  // `NullPointerException: ... ArrayList.size() on a null object reference`
  // out of the binder call, which no amount of retry-on-that-one-string
  // was catching.
  async function waitForFramework(c, what) {
    const TIMEOUT_MS = 600000;
    const started = Date.now();
    let boot = false, lastNote = -1, lastProbe = '', announced = false;
    setWaiting(true);
    try {
      while (Date.now() - started < TIMEOUT_MS) {
        if (!boot) boot = (await c.shell('getprop sys.boot_completed')).trim() === '1';
        if (boot) {
          // The package manager is the thing we actually need; ask it. It comes
          // up meaningfully after boot_completed on this hardware, so the flag
          // is a necessary condition, not the answer.
          // Deliberately NOT via su: this is a read, it works as the shell
          // user, and verify_root calls this before root is confirmed.
          lastProbe = (await c.shell('pm path android 2>&1')).trim();
          if (lastProbe.startsWith('package:')) {
            // Only worth a line if there was actually a wait. Every step after
            // the first re-gates (steps are individually retryable), and three
            // "Framework ready after 0s" banners per run is noise that trains
            // people to skim past the one time it matters.
            if (announced) addLog(`Framework ready after ${Math.round((Date.now() - started) / 1000)}s.`, 'ok');
            return;
          }
        }
        if (!announced) {
          announced = true;
          addLog('Waiting for Android to finish booting — safe to have clicked Reconnect early, this waits as long as it takes…');
        }
        // Heartbeat every 15s so a multi-minute wait reads as progress rather
        // than as a hang (lesson 3 above).
        const elapsed = Math.floor((Date.now() - started) / 1000);
        if (elapsed - lastNote >= 15) {
          lastNote = elapsed;
          addLog(`  [${elapsed}s] boot_completed=${boot ? '1' : '0'}`
               + (boot ? `, package manager not answering yet${lastProbe ? ` (${lastProbe.split('\n')[0]})` : ''}` : ', still booting'));
        }
        await new Promise(r => setTimeout(r, 2000));
      }
      throw new Error(
        `Android has not finished booting after 10 minutes, so ${what} cannot run. `
        + `Every pm command would fail and the step would silently do nothing. `
        + `This is long past a slow boot — suspect a bootloop rather than patience: `
        + `check the device's light ring, and click Retry once it settles.`);
    } finally {
      setWaiting(false);
    }
  }

  async function runVerifyRoot(c) {
    // Same lesson as runDisableAlexa: reconnecting over ADB just means the
    // USB/adbd link is up, not that Android has finished booting — and for
    // root specifically there's a second gate on top of that, magiskd
    // itself needs to attach and start granting su requests. A premature
    // `su -c id` here doesn't just fail cleanly: repeated permission-denied
    // calls against a magiskd that's still initialising have been observed
    // to corrupt the grant state from the preseeded magisk.db, leaving
    // root broken even on later, correctly-timed retries. Wait for both
    // gates explicitly rather than relying on a single timed attempt.
    await waitForFramework(c, 'root verification');

    // Mute the speaker before the su wait, not after.
    //
    // Amazon's OOBE announces "Hello, I'm Alexa, connect to me using the Alexa
    // app" as soon as the framework is up, and the earliest we can stop the
    // app itself is the Disable Alexa step — which is on the far side of
    // magiskd attaching, measured at 74s on a cold device. So the app cannot
    // be silenced in time, and this is an attempt to silence the speaker
    // instead: `input` needs only the shell user, and it runs the moment the
    // framework answers.
    //
    // IT DOES NOT RELIABLY WORK. Observed on hardware 2026-08-08: she talks
    // regardless. Two candidates, not yet separated — OOBE raises the volume
    // back after we lower it, or it plays on a stream `keyevent 25` does not
    // address (25 adjusts whichever stream is active, which with nothing
    // playing is not necessarily the one she uses). `dumpsys audio` while she
    // is talking settles it. It is left in because turning the volume down
    // costs nothing and may help, but the log line must not claim more than
    // that.
    //
    // Safe to leave muted. This moves Android's stream volume; EchoMuse drives
    // the codec itself and seeds its own level from `startupVolume` (85) on
    // the first config push after the reboot that ends provisioning, so the
    // device comes up audible without anything having to restore this.
    //
    // Volume-down keyevents rather than a volume API: `service call audio`
    // needs a transaction number that differs per Android release, and
    // `settings put system volume_music` is not read live by AudioService.
    // This is what pressing the button does, and it works on any release.
    addLog('Muting the speaker — Amazon\'s setup assistant starts talking here, and cannot be stopped until root lands…');
    try {
      await c.shell('i=0; while [ $i -lt 15 ]; do input keyevent 25; i=$((i+1)); done');
      // Measured on hardware 2026-08-08: the setup assistant talks anyway. It
      // either raises the volume back or plays on a stream these keyevents do
      // not touch, and which of those it is has not been established. Say what
      // was done, not what was achieved — claiming it is muted for the rest of
      // provisioning sends someone looking for a fault when they hear her.
      addLog('  → volume turned down; the setup assistant may raise it again and talk anyway.', 'warn');
    } catch (e) {
      addLog(`  → could not mute (${e.message}) — the setup prompt may talk over the wizard.`, 'warn');
    }

    addLog('Testing su -c id… (magiskd can take a while to attach after boot — retrying if needed)');
    let out = '';
    let rooted = false;
    const attemptStart = Date.now();
    setWaiting(true);
    try {
      for (let i = 0; i < 15; i++) {
        const callStart = Date.now();
        // The line below reports the call's duration, but only once it returns —
        // and a single su call has been observed blocking for 72s against a
        // magiskd that isn't listening yet. That was 72 seconds of a completely
        // silent wizard, which is indistinguishable from a hang. Tick while the
        // call is still in flight, so the wait is visibly a wait.
        const ticker = setInterval(
          () => addLog(`    still waiting on su (${Math.round((Date.now() - callStart) / 1000)}s) — magiskd has not answered yet`),
          15000);
        try {
          out = await c.shell('su -c id 2>&1');
        } finally {
          clearInterval(ticker);
        }
        const callMs = Date.now() - callStart;
        // Log every attempt with timing — the previous version of this loop
        // was silent inside the loop body, so a single su -c id call that's
        // unexpectedly slow (e.g. blocking on a magiskd socket that isn't
        // listening yet, rather than failing fast with permission-denied)
        // was indistinguishable from a true hang. This makes that visible:
        // if callMs is large, the call itself is slow, not the wizard stuck.
        addLog(`  attempt ${i + 1}/15 (${(callMs / 1000).toFixed(1)}s): ${out || '(empty)'}`);
        if (out.includes('uid=0')) { rooted = true; break; }
        // If a single su call already took a while, don't add the full 2s
        // sleep on top — just move to the next attempt.
        if (callMs < 2000) await new Promise(r => setTimeout(r, 2000 - callMs));
      }
    } finally {
      setWaiting(false);
    }
    addLog(`Total wait: ${((Date.now() - attemptStart) / 1000).toFixed(0)}s.`);
    if (!rooted) throw new Error('Root not working after waiting for boot + magiskd — check Magisk install and magisk.db.');
    addLog('Root confirmed.', 'ok');
  }

  async function scanWifi(c) {
    addLog('Scanning for WiFi networks…');
    await c.shell("su -c 'svc wifi enable'");
    await new Promise(r => setTimeout(r, 2000));
    // wpa_cli on this build needs BOTH -p (socket dir, since
    // ctrl_interface=/data/misc/wifi/sockets is non-default) AND -i wlan0
    // (interface) explicitly — without -p it sometimes silently works by
    // luck of default-selecting the only non-p2p interface, but once other
    // client sockets exist in the dir (e.g. from system/smarthome
    // processes) it mis-selects one of those instead and fails with
    // "Operation not permitted". -i alone without -p fails outright with
    // "Failed to connect to non-global ctrl_ifname". Always pass both.
    await c.shell("su -c 'wpa_cli -p /data/misc/wifi/sockets -i wlan0 scan'");
    await new Promise(r => setTimeout(r, 3000));
    const raw = await c.shell("su -c 'wpa_cli -p /data/misc/wifi/sockets -i wlan0 scan_results'");
    addLog('Scan complete.');
    return parseScanResults(raw);
  }

  // Parse wpa_cli scan_results: bssid / frequency / signal / flags / ssid.
  //
  // The frequency and flags columns used to be read and thrown away, which
  // is how a WPA3 network came to look identical to a WPA2 one in the picker
  // and produced twenty seconds of SCANNING with an error that named nothing
  // (#82). They are the two facts that explain most association failures on
  // this hardware, so they are kept.
  function parseScanResults(raw) {
    const networks = [];
    for (const line of (raw || '').split('\n')) {
      const parts = line.split('\t');
      if (parts.length < 5) continue;
      const ssid = parts[4].trim();
      if (!ssid || ssid === 'SSID') continue;
      const freq   = parseInt(parts[1], 10);
      const signal = parseInt(parts[2], 10);
      const flags  = parts[3] || '';
      const band   = freq >= 4900 ? '5GHz' : (freq > 0 ? '2.4GHz' : '');

      const existing = networks.find(n => n.ssid === ssid);
      if (!existing) {
        networks.push({ ssid, signal, freq, flags, bands: band ? [band] : [] });
      } else {
        // Same SSID on more than one AP or band. Keep the strongest for the
        // headline numbers, but remember every band it was seen on.
        if (band && !existing.bands.includes(band)) existing.bands.push(band);
        if (signal > existing.signal) {
          existing.signal = signal;
          existing.freq = freq;
          existing.flags = flags;
        }
      }
    }
    // Derived here rather than in the panel, so WifiPanel stays presentational
    // and the same objects can be reused by the failure diagnostics.
    for (const n of networks) {
      n.security = classifySecurity(n.flags);
      n.securityLabel = _SECURITY_LABEL[n.security] || n.security;
      n.blocker = securityBlocker(n.security);
    }
    networks.sort((a, b) => b.signal - a.signal);
    return networks;
  }

  // What can this device actually join?
  //
  // Measured on hardware (`wpa_cli get_capability key_mgmt`): NONE,
  // IEEE8021X, WPA-EAP, WPA-PSK. There is no SAE, so WPA3-Personal is not a
  // configuration problem, it is impossible on this radio — hence 'wpa3'
  // being refused outright rather than attempted. Mixed WPA2/WPA3 APs
  // advertise both and are joinable through the WPA2 half.
  function classifySecurity(flags) {
    const f = (flags || '').toUpperCase();
    const hasPsk = f.includes('WPA-PSK') || f.includes('WPA2-PSK') || f.includes('PSK');
    if (f.includes('EAP')) return 'enterprise';
    if (f.includes('SAE') && !hasPsk) return 'wpa3';
    if (hasPsk) return 'wpa2';
    if (f.includes('WEP')) return 'wep';
    return 'open';
  }

  // Why a network cannot be used, or null when it can.
  function securityBlocker(security) {
    if (security === 'wpa3') {
      return 'WPA3 only. This Dot has no SAE support, so it cannot join. '
           + 'Enable WPA2 compatibility mode on the router or hotspot.';
    }
    if (security === 'enterprise') {
      return 'Enterprise (802.1X). The wizard cannot configure this.';
    }
    if (security === 'wep') {
      return 'WEP. The wizard cannot configure this.';
    }
    return null;
  }

  // Quote a value for safe embedding inside a wpa_supplicant.conf network
  // block. SSIDs/PSKs containing a literal " or \ would break the file
  // format — reject rather than mis-escape, since this is config content,
  // not a shell string.
  function wpaConfEscape(value) {
    if (/["\\]/.test(value)) {
      throw new Error(`Value contains a double-quote or backslash character, which wpa_supplicant.conf cannot represent safely: "${value}"`);
    }
    return value;
  }

  // Diagnose a failed association from the device's own view of the air.
  //
  // Three outcomes worth telling apart, all of which look identical in the
  // wpa_state log: the network is not on the air at all, it is there but the
  // radio cannot join it (WPA3), or it is there and joinable, which points at
  // the password or the AP rather than at us.
  async function reportWhyNoAssociation(c, ssid) {
    try {
      await c.shell("su -c 'wpa_cli -p /data/misc/wifi/sockets -i wlan0 scan'");
      await new Promise(r => setTimeout(r, 3000));
      const raw = await c.shell("su -c 'wpa_cli -p /data/misc/wifi/sockets -i wlan0 scan_results'");
      const seen = parseScanResults(raw);
      const match = seen.find(n => n.ssid === ssid);

      if (!match) {
        addLog(`"${ssid}" was not seen in a fresh scan (${seen.length} other network(s) were).`, 'warn');
        addLog('Either it is out of range, or it is hidden and the SSID is not spelled exactly right.', 'warn');
        return;
      }
      addLog(`"${ssid}" IS on the air: ${_SECURITY_LABEL[match.security] || match.security}`
           + `, ${match.bands.join(' + ') || match.freq + ' MHz'}, ${match.signal} dBm`, 'warn');
      addLog(`  flags: ${match.flags}`);
      const blocker = securityBlocker(match.security);
      if (blocker) {
        addLog(`  ${blocker}`, 'error');
      } else {
        addLog('  The Dot can join this kind of network, so the likely cause is the '
             + 'password, or the AP refusing the client.', 'warn');
      }
    } catch (e) {
      addLog(`Could not scan to diagnose: ${e.message || e}`, 'warn');
    }
  }

  async function runConfigWifi(c, ssid, psk) {
    if (!ssid) throw new Error('No SSID selected.');
    wpaConfEscape(ssid);
    wpaConfEscape(psk);

    // What the scan said about this network, if it was picked from the list.
    // A typed SSID that no scan saw is treated as hidden, which needs
    // scan_ssid=1 or wpa_supplicant never probes for it and sits in SCANNING
    // indefinitely.
    const known  = (wifiNetworks || []).find(n => n.ssid === ssid);
    const hidden = !known;
    const security = known ? known.security : (psk ? 'wpa2' : 'open');

    const blocker = securityBlocker(security);
    if (blocker) throw new Error(`Cannot join "${ssid}": ${blocker}`);

    if (security === 'wpa2' && !psk) {
      throw new Error(`"${ssid}" needs a password.`);
    }
    if (known) {
      addLog(`Network: ${_SECURITY_LABEL[security] || security}`
           + `${known.bands.length ? ', ' + known.bands.join(' + ') : ''}`
           + `, ${known.signal} dBm`);
    } else {
      addLog(`"${ssid}" was not in the last scan — configuring it as a hidden network.`, 'warn');
    }

    addLog('Enabling WiFi radio…');
    await c.shell("su -c 'svc wifi enable'");
    await new Promise(r => setTimeout(r, 2000));

    // Read device identity fields from getprop rather than assuming any
    // existing wpa_supplicant.conf — this must work on a bare device that
    // never had the Alexa WiFi setup flow run.
    addLog('Reading device identity…');
    const deviceName   = await c.shell('getprop ro.product.name')          || 'echomuse';
    const manufacturer = await c.shell('getprop ro.product.manufacturer')  || 'Amazon';
    const model        = await c.shell('getprop ro.product.model')        || 'AEOBC';
    const serial       = await c.shell('getprop ro.serialno')             || await c.shell('getprop ro.boot.serialno') || 'unknown';

    // Full config replacement — single network only, no ambiguity about
    // which AP it joins. Deliberately drops any prior (e.g. Alexa-era)
    // network entries.
    const confLines = [
      'ctrl_interface=/data/misc/wifi/sockets',
      'driver_param=use_p2p_group_interface=1',
      'update_config=1',
      `device_name=${deviceName}`,
      `manufacturer=${manufacturer}`,
      `model_name=${model}`,
      `model_number=${model}`,
      `serial_number=${serial}`,
      'device_type=1-0050F204-9',
      'os_version=01020300',
      'config_methods=physical_display virtual_push_button',
      'p2p_no_group_iface=1',
      'external_sim=1',
      'wowlan_triggers=disconnect',
      'network={',
      `\tssid="${ssid}"`,
      // key_mgmt used to be hardcoded to WPA-PSK, which made an open network
      // unjoinable with no explanation. The device reports NONE among its
      // supported key_mgmt values, so open networks work, they were just
      // never configurable.
      ...(security === 'open'
            ? ['\tkey_mgmt=NONE']
            : [`\tpsk="${psk}"`, '\tkey_mgmt=WPA-PSK']),
      // Without this, wpa_supplicant only ever joins networks that appear in
      // a passive scan, so a hidden SSID never associates and reports nothing
      // more useful than SCANNING.
      ...(hidden ? ['\tscan_ssid=1'] : []),
      '\tpriority=1',
      '}',
      '',
    ].join('\n');

    addLog(`Writing config for "${ssid}"…`);
    // The full sequence below was hard-won on real hardware — do not
    // simplify without re-testing on device:
    //  1. chmod 770 the wifi dir — 666 strips the execute/traverse bit and
    //     makes every file inside unopenable even though file perms look fine.
    //  2. Never use a raw shell redirect (> or >>) on this mksh build —
    //     it silently fails ("can't create ... Permission denied") for
    //     reasons never fully root-caused. cp and `tee` (no -a) both work.
    //  3. rm any stale /tmp target first — tee can fail against a leftover
    //     file from a previous attempt even though it succeeds against a
    //     fresh path.
    //  4. cp from /tmp to the real path, then explicitly chown/chmod back —
    //     cp as root does not preserve the destination dir's expected
    //     wifi:wifi ownership.
    //  5. Reload via `svc wifi disable` + `svc wifi enable` (NOT raw
    //     stop/start wpa_supplicant — see the big comment further down for
    //     why). This goes through the proper Android-managed wpa_supplicant
    //     instance, which auto-associates and gets a DHCP lease on its own
    //     with no manual reconnect/dhcpcd needed.
    const b64 = btoa(unescape(encodeURIComponent(confLines)));
    await c.shell('su -c "chmod 770 /data/misc/wifi"');
    await c.shell('su -c "rm -f /tmp/wpa_supplicant.conf"');
    await c.shell(`su -c "echo ${b64} | busybox base64 -d | busybox tee /tmp/wpa_supplicant.conf"`);

    // Verify the staged file actually has the SSID we intended — catches
    // the b64-via-shell-arg path silently mangling content before we ever
    // touch the real config.
    const staged = await c.shell('su -c "cat /tmp/wpa_supplicant.conf"');
    if (!staged.includes(`ssid="${ssid}"`)) {
      throw new Error(`Staged config in /tmp does not contain ssid="${ssid}" — write failed before reaching the device. Staged content:\n${staged}`);
    }

    await c.shell('su -c "cp /tmp/wpa_supplicant.conf /data/misc/wifi/wpa_supplicant.conf"');
    await c.shell('su -c "chown wifi:wifi /data/misc/wifi/wpa_supplicant.conf"');
    await c.shell('su -c "chmod 660 /data/misc/wifi/wpa_supplicant.conf"');

    // Verify the final on-device file too — catches the cp step itself
    // failing or writing to the wrong place.
    const onDevice = await c.shell('su -c "cat /data/misc/wifi/wpa_supplicant.conf"');
    if (!onDevice.includes(`ssid="${ssid}"`)) {
      throw new Error(`Config at /data/misc/wifi/wpa_supplicant.conf does not contain ssid="${ssid}" after cp — the write did not take. On-device content:\n${onDevice}`);
    }
    addLog('Config written and verified on device.', 'ok');

    addLog('Reloading WiFi via the Android framework…');
    // These two rewrite wpa_supplicant.conf out from under us. runDisableAlexa
    // neutralises both, but SmartHomeWifid has been seen running again by the
    // time we get here (a later boot trigger, or init restarting it before the
    // persist property took) — and this used to just dump the raw `ps` row and
    // carry on, which reads as a diagnostic nobody has to act on. It isn't: the
    // run where it was present spent 9s cycling DISCONNECTED/SCANNING before
    // associating, against 1s on a clean one. Kill what's there, then say so in
    // words rather than in ps columns.
    const psOut = await c.shell("su -c 'ps' | grep -iE 'wifiprofilemanager|SmartHomeWifid'");
    const found = psOut.split('\n')
      .map(l => l.trim()).filter(Boolean)
      .map(l => { const f = l.split(/\s+/); return { pid: f[1], name: (f[f.length - 1] || '').split('/').pop() }; })
      .filter(p => /^\d+$/.test(p.pid || ''));
    if (found.length === 0) {
      addLog('No WiFi config interferers running — clean.', 'ok');
    } else {
      addLog(`${found.map(p => `${p.name} (pid ${p.pid})`).join(', ')} running — `
           + `rewrites wpa_supplicant.conf, stopping…`, 'warn');
      // `kill -9` alone is not enough and was observed not holding: these are
      // init services, so init restarts them within moments and the re-check
      // finds a fresh pid. init has to be told to stop the SERVICE. The
      // service name is not guessed — it is read out of init.svc.* at
      // runtime, which is init's own record of what it is running, so this
      // survives a name differing across SKUs.
      const svcProps = await c.shell("su -c 'getprop' | grep -iE 'init\\.svc\\.(.*smarthome.*|.*wifiprofile.*)'");
      // getprop prints `[init.svc.SmartHomeWifid]: [running]`.
      const svcNames = svcProps.split('\n').map(l => {
        const m = /^\[init\.svc\.([^\]]+)\]:\s*\[(\w+)\]/.exec(l.trim());
        return m && m[2] === 'running' ? m[1] : null;
      }).filter(Boolean);
      for (const svc of svcNames) {
        await c.shell(`su -c "stop ${svc}"`);
        addLog(`  stopped init service "${svc}"`);
      }
      // Then kill whatever is still up — a service init has been told to stop
      // does not die on its own.
      for (const p of found) await c.shell(`su -c "kill -9 ${p.pid}"`);

      const still = (await c.shell("su -c 'ps' | grep -iE 'wifiprofilemanager|SmartHomeWifid'")).trim();
      if (!still) {
        addLog('Interferers stopped.', 'ok');
      } else if (svcNames.length === 0) {
        addLog('Still running, and no matching init service was found to stop — '
             + 'it will respawn. Association may be slow or the config may be overwritten.', 'warn');
      } else {
        addLog('Still running after stop+kill — association may be slow or the config overwritten.', 'warn');
      }
    }

    // IMPORTANT — found the hard way on real hardware: this device runs
    // TWO independent things that can each launch /system/bin/wpa_supplicant:
    //  1. The bare init service (`start`/`stop wpa_supplicant`) — a minimal
    //     invocation with no p2p, no overlay config, no Android control
    //     socket. This is what `stop`/`start wpa_supplicant` controls, and
    //     what our earlier kill -9-based reload was fighting with.
    //  2. The proper Android-framework-managed instance, launched by
    //     `svc wifi enable` with the FULL correct flags (wlan0 + p2p0,
    //     overlay configs, entropy file, -g@android:wpa_wlan0 abstract
    //     socket for the framework's own WifiStateMachine/WifiNative).
    // If both end up running simultaneously (e.g. because something earlier
    // called `svc wifi enable` and we separately kill -9/start the bare
    // service), they fight over the wlan0 netdev and one disables the
    // interface out from under the other — symptom: wpa_state sits at
    // DISCONNECTED then flips to INTERFACE_DISABLED and never recovers.
    // The correct reload mechanism is `svc wifi disable` + `svc wifi
    // enable` — this manages the proper framework instance exclusively,
    // and on this device it auto-associates and gets an IP via the
    // framework's own DHCP handling with NO manual reconnect or dhcpcd
    // call needed. Do not reintroduce kill -9 / raw start wpa_supplicant /
    // manual wpa_cli reconnect / manual dhcpcd here — all proven
    // unnecessary and actively harmful (causes the dual-process conflict)
    // once `svc wifi enable` is already used earlier in this function.
    await c.shell('su -c "svc wifi disable"');
    await new Promise(r => setTimeout(r, 2000));
    await c.shell('su -c "svc wifi enable"');
    await new Promise(r => setTimeout(r, 3000));

    const psCheck = await c.shell("su -c 'ps | grep /system/bin/wpa_supplicant | while read user pid rest; do echo $pid; done'");
    const pidCount = psCheck.split('\n').map(s => s.trim()).filter(Boolean).length;
    if (pidCount === 0) throw new Error('wpa_supplicant did not start after svc wifi enable — check device logcat.');
    if (pidCount > 1) throw new Error(`Multiple wpa_supplicant processes running (${pidCount}) — the bare init service and the framework instance are both up and will conflict. Check for a stray "start wpa_supplicant" call.`);
    addLog(`wpa_supplicant running (1 process, pid ${psCheck.trim()}).`, 'ok');

    addLog('Waiting for association (up to 20s)…');
    let associated = false;
    let lastStatus = '';
    for (let i = 0; i < 20; i++) {
      await new Promise(r => setTimeout(r, 1000));
      lastStatus = await c.shell("su -c 'wpa_cli -p /data/misc/wifi/sockets -i wlan0 status'");
      const stateMatch = lastStatus.match(/wpa_state=(\S+)/);
      addLog(`  [${i+1}s] wpa_state=${stateMatch ? stateMatch[1] : '?'}`);
      if (lastStatus.includes('wpa_state=COMPLETED')) { associated = true; break; }
    }
    if (!associated) {
      // Say what the radio can actually see. Without this the log ends on
      // twenty identical SCANNING lines and a status block naming nothing
      // that would explain them, which is precisely how #82 arrived: correct
      // config, verified on device, no interferers, and no clue.
      await reportWhyNoAssociation(c, ssid);
      throw new Error(`Did not associate to "${ssid}" within 20s. Last status:\n${lastStatus}`);
    }
    addLog('Associated.', 'ok');

    addLog('Waiting for IP address (up to 20s)…');
    for (let i = 0; i < 20; i++) {
      await new Promise(r => setTimeout(r, 1000));
      const ip = await c.shell("su -c 'ip addr show wlan0 | grep \"inet \" | while read proto addr rest; do echo ${addr%/*}; done'");
      if (ip && /\d+\.\d+\.\d+\.\d+/.test(ip)) {
        addLog(`Connected! IP: ${ip}`, 'ok');
        return;
      }
    }
    throw new Error(`Associated to "${ssid}" but did not get an IP within 20s. Check device logcat for DHCP issues.`);
  }

  async function runDisableAlexa(c) {
    // `su -c id` succeeding (the previous step) only confirms Magisk/root
    // is up — it does NOT mean the Android framework has finished booting.
    // Found on hardware: pm disable calls made too early fail with
    // "Could not access the Package Manager. Is the system running?" for
    // the first several packages, then start succeeding once the system
    // server catches up mid-loop. sys.boot_completed=1 is the actual
    // readiness signal for the package manager being available — but it is
    // necessary, not sufficient, so waitForFramework also probes pm itself.
    await waitForFramework(c, 'disabling the Alexa stack');

    // Silence the out-of-box setup assistant FIRST, before the main loop.
    //
    // A fresh device boots straight into Amazon's OOBE: it announces "Hello,
    // I'm Alexa, connect to me using the Alexa app" out loud and spins an
    // amber ring, and it keeps doing both for the whole provisioning session.
    // It is loud, it is confusing next to a wizard that is clearly already
    // talking to the device, and it invites someone to go and complete Amazon
    // setup on a device being taken off Amazon.
    //
    // It was previously only `pm hide`d in the debloat step — which is both
    // later and insufficient, since hiding does not stop a running instance.
    // Killing it needs all three of these: stop it now, stop it being
    // relaunched by the framework, and stop it being re-triggered by the
    // "device not provisioned" flags it keys off.
    const OOBE = 'com.amazon.echo.csm.oobe';
    addLog('Silencing the Amazon setup assistant (the amber ring and the "connect using the Alexa app" prompt)…');
    // Order matters: mark setup done first, so nothing relaunches it in the
    // gap between force-stop and disable.
    for (const s of ['put global device_provisioned 1', 'put secure user_setup_complete 1']) {
      await c.shell(`su -c 'settings ${s}' 2>&1`);
    }
    await c.shell(`su -c 'am force-stop ${OOBE}' 2>&1`);
    const oobeDisable = (await c.shell(`su -c 'pm disable ${OOBE}' 2>&1`)).trim();
    // force-stop is a no-op against a PERSISTENT app (the same lesson whad
    // taught in the debloat list), so check and kill directly rather than
    // assuming it worked.
    const oobePids = (await c.shell(`su -c 'ps' | grep -F ${OOBE} | grep -v grep`))
      .split('\n').map(l => l.trim().split(/\s+/)[1]).filter(p => /^\d+$/.test(p || ''));
    for (const pid of oobePids) await c.shell(`su -c "kill -9 ${pid}"`);
    const oobeLeft = (await c.shell(`su -c 'ps' | grep -F ${OOBE} | grep -v grep`)).trim();
    const oobeVerdict = _pmVerdict(oobeDisable);
    addLog(`  → ${oobeVerdict === 'disabled' ? 'disabled'
                : oobeVerdict === 'absent'   ? 'not installed on this build'
                : (oobeDisable || 'no output')}`
         + `, ${oobePids.length} running process(es) killed`
         + (oobeLeft ? ', still running, it will stop at the reboot that ends provisioning' : ''),
           oobeLeft || oobeVerdict === 'rejected' ? 'warn' : 'ok');

    let disabled = 0, absent = 0, rejected = 0;
    for (const pkg of _ALEXA_PKGS) {
      addLog(`Disabling ${pkg}…`);
      let out = await c.shell(`su -c 'pm disable ${pkg}' 2>&1`);
      if (_pmNotReady(out)) {
        // Still not ready despite the gate above — give it a moment and retry once.
        addLog('  Package Manager not ready yet, waiting 3s and retrying…', 'warn');
        await new Promise(r => setTimeout(r, 3000));
        out = await c.shell(`su -c 'pm disable ${pkg}' 2>&1`);
      }
      const verdict = _pmVerdict(out);
      if (verdict === 'disabled') disabled++;
      else if (verdict === 'absent') absent++;
      else rejected++;
      addLog(`  → ${verdict === 'absent' ? 'not installed on this build' : (out.trim() || 'ok')}`,
             verdict === 'disabled' ? undefined : 'warn');
    }
    // Three outcomes, not two.
    //
    // The original check counted successes and nothing else, so "every package
    // is absent from this build" and "the package manager rejected every call"
    // produced the same fatal error and the same advice, which is to wait
    // longer and retry. On a device whose image genuinely lacks these packages
    // that advice can never work and the wizard can never be completed (#91).
    //
    // `Unknown package` is an IllegalArgumentException out of PackageManager:
    // pm answered, and the answer was that the package is not installed. That
    // is not a failure, it is a different SKU or build.
    if (rejected > 0 && disabled === 0) {
      throw new Error(
        `The package manager rejected ${rejected} of ${_ALEXA_PKGS.length} calls and disabled none. `
        + `Do NOT continue to WiFi: the Alexa stack may still be running and will phone home. `
        + `Give the device longer to boot and click Retry.`);
    }
    if (disabled === 0 && absent === _ALEXA_PKGS.length) {
      // Nothing to disable, and pm said so cleanly for every one. Continuing
      // is correct, but say plainly what was concluded rather than ticking
      // the step green in silence — this is an image nobody here has seen.
      addLog(`None of the ${_ALEXA_PKGS.length} Alexa packages are installed on this build, `
           + `so there was nothing to disable. If the device is silent and its ring is off, `
           + `that is the expected state and provisioning can continue.`, 'warn');
    } else {
      addLog(`${disabled} disabled, ${absent} not installed on this build.`,
             disabled ? 'ok' : 'warn');
    }

    // pm disable on com.amazon.device.smarthome.adapters.wifi does NOT stop
    // /system/bin/SmartHomeWifid — it's launched directly by init via
    // /init.smarthome.rc's property-trigger chain (wifi.launch reaching
    // "111"), independent of the Android package manager. That trigger
    // chain only fires once persist.wifi.migrate.complete=1 — clearing it
    // prevents wifi.launch from ever reaching "111", so SmartHomeWifid
    // never starts. This is a persist. property so it survives reboots;
    // proven on hardware to durably stop the interference.
    addLog('Clearing wifi migration flag to prevent SmartHomeWifid from starting…');
    await c.shell('su -c "setprop persist.wifi.migrate.complete 0"');
    const check = await c.shell('su -c "getprop persist.wifi.migrate.complete"');
    addLog(`  → persist.wifi.migrate.complete=${check.trim()}`);

    // SmartHomeWifid may already be running from this boot (started before
    // we cleared the property) — kill it now rather than waiting for next
    // reboot, since the wizard proceeds straight to WiFi config next.
    const smartHomeWifidPid = (await c.shell("su -c 'ps | grep /system/bin/SmartHomeWifid | while read user pid rest; do echo $pid; done'")).trim();
    if (smartHomeWifidPid) {
      addLog(`Killing already-running SmartHomeWifid (pid ${smartHomeWifidPid})…`);
      await c.shell(`su -c "kill -9 ${smartHomeWifidPid}"`);
    }

    addLog('Alexa stack disabled.', 'ok');
  }

  async function runDebloat(c) {
    // Two halves, mirroring the recipe proven on the Lounge device
    // (2026-07-15, −130MB RAM / cpu_avg −2-3pp, no voice regressions):
    //  1. `pm hide` the non-essential Amazon packages. Hide, NOT disable —
    //     FireOS 5 ignores `pm disable` for PERSISTENT system apps and
    //     starts them at boot anyway; hide sticks across reboots.
    //  2. Install a Magisk service.d boot script that re-stops the
    //     init-launched native daemons every boot (`stop` doesn't persist,
    //     and they aren't packages so pm can't touch them). It takes effect
    //     from the next boot — the wizard's final step reboots the device,
    //     so a fresh provision comes up fully debloated.
    // Both payloads come from the controller (device_payloads/) so the
    // package list and daemon set can be tuned without touching this code.
    const pkgResp = await fetch(ingressPath('/api/provision/debloat_packages'), { headers: { Authorization: `Bearer ${token}` } });
    if (!pkgResp.ok) throw new Error(`Controller returned ${pkgResp.status} fetching debloat package list.`);
    const { packages } = await pkgResp.json();

    // Re-gate rather than trusting the previous step: steps are individually
    // retryable, so this one can be entered on its own after a reconnect.
    await waitForFramework(c, 'debloat');

    addLog(`Hiding ${packages.length} packages…`);
    let hidden = 0, absentPkgs = 0, rejectedPkgs = 0;
    for (const pkg of packages) {
      let out = (await c.shell(`su -c 'pm hide ${pkg}' 2>&1`)).trim();
      if (_pmNotReady(out)) {
        await new Promise(r => setTimeout(r, 3000));
        out = (await c.shell(`su -c 'pm hide ${pkg}' 2>&1`)).trim();
      }
      // pm hide prints "Package <pkg> new hidden state: true" on success; a
      // package absent from this build answers `Unknown package`, which is pm
      // working, not pm refusing. The list spans SKU variants by design, so
      // absences are expected here even more than in the Alexa step.
      const verdict = _pmVerdict(out);
      if (verdict === 'disabled') hidden++;
      else if (verdict === 'absent') absentPkgs++;
      else rejectedPkgs++;
      addLog(`  ${pkg} → ${verdict === 'disabled' ? 'hidden'
                         : verdict === 'absent'   ? 'not installed on this build'
                         : (out || 'no output')}`,
             verdict === 'disabled' ? undefined : 'warn');
    }
    // Same three outcomes as the Alexa step, and the same reason: counting
    // only successes made "this build does not carry these packages"
    // indistinguishable from "pm is broken", and only the second is worth
    // stopping for (#91).
    if (rejectedPkgs > 0 && hidden === 0) {
      throw new Error(
        `The package manager rejected ${rejectedPkgs} of ${packages.length} calls and hid none. `
        + `Give the device longer to boot and click Retry.`);
    }
    addLog(`${hidden}/${packages.length} packages hidden`
         + (absentPkgs ? `, ${absentPkgs} not installed on this build.` : '.'),
           hidden ? 'ok' : 'warn');

    addLog('Installing boot-time daemon-stop script (Magisk service.d)…');
    const scrResp = await fetch(ingressPath('/api/provision/debloat_script'), { headers: { Authorization: `Bearer ${token}` } });
    if (!scrResp.ok) throw new Error(`Controller returned ${scrResp.status} fetching debloat script.`);
    const script = await scrResp.text();
    // Same push-then-cp pattern as start_server.sh: nothing executes the
    // script this boot, so push() is safe (no "Text file busy" risk).
    const svcDir = '/sbin/.core/img/.core/service.d';
    await c.push('/sdcard/echomuse-debloat.sh', new TextEncoder().encode(script));
    await c.shell(`su -c 'mkdir -p ${svcDir} && cp /sdcard/echomuse-debloat.sh ${svcDir}/echomuse-debloat.sh && chmod 755 ${svcDir}/echomuse-debloat.sh'`);
    const listing = (await c.shell(`su -c 'ls ${svcDir}' 2>&1`)).trim();
    if (!listing.includes('echomuse-debloat.sh')) {
      throw new Error(`Debloat script install verification failed — ${svcDir} contains: "${listing}". Is Magisk mounted (/sbin/.core present)?`);
    }
    addLog('Debloat applied — daemon stops take effect on the post-install reboot.', 'ok');
  }

  async function runInstallEchoMuse(c, file, useLatest) {
    let buf;
    if (useLatest) {
      addLog('Fetching latest EchoMuse build from controller…');
      // Confirmed against em_api.py: /api/provision/latest_binary streams
      // the binary itself (distinct from /api/releases/latest, which only
      // returns {version, url} metadata). Server-side download from
      // GitHub via the same _get_cached_release()/_fetch_binary() the OTA
      // pipeline uses — needed because a freshly-flashed device isn't in
      // _devices yet, so /api/devices/{id}/update (which requires a live
      // WebSocket session) isn't usable at this point in the wizard.
      const resp = await fetch(ingressPath('/api/provision/latest_binary'), { headers: { Authorization: `Bearer ${token}` } });
      if (!resp.ok) throw new Error(`Controller returned ${resp.status} fetching latest binary.`);
      buf = await resp.arrayBuffer();
      const ver = resp.headers.get('X-Release-Version');
      addLog(`Latest build${ver ? ` (${ver})` : ''}: ${(buf.byteLength/1024/1024).toFixed(1)} MB`);
    } else {
      addLog(`Pushing ${file.name} to /sdcard/server_new…`);
      buf = await file.arrayBuffer();
    }
    await c.push('/sdcard/server_new', new Uint8Array(buf),
      pct => setProgress({ label: 'Uploading binary', pct }));
    setProgress(null);

    // Wipe any pre-existing install before writing fresh. A device that's
    // been through OTA before (or a previous, possibly-failed, wizard run)
    // can have server, server_a, AND server_b all present — OTA's slot
    // logic deliberately keeps the inactive slot around for rollback, but
    // that's the wrong default for a fresh provision: there's no good
    // "previous version" here, and leaving stale state behind is exactly
    // what let the GitHub-install bug silently keep an old dev build in
    // place. Each step is checked individually rather than && chained —
    // that's what let the original bug stay silent in the first place.
    addLog('Clearing any pre-existing EchoMuse install…');
    await c.shell('su -c "mkdir -p /data/local/bin"');
    const rmOut = (await c.shell('su -c "rm -f /data/local/bin/server /data/local/bin/server_a /data/local/bin/server_b" 2>&1')).trim();
    if (rmOut) addLog(`  → ${rmOut}`);
    // Confirm the symlink itself is gone — readlink is already proven on
    // this device (the OTA pipeline's slot detection relies on it, always
    // with 2>/dev/null, never 2>&1). Confirmed on hardware: readlink on a
    // missing target prints an error message rather than returning truly
    // empty output, so capturing stderr here would corrupt the "empty
    // means gone" check below. Discard stderr instead, matching the
    // existing proven pattern in em_api.py exactly.
    const linkAfterClear = (await c.shell('su -c "readlink /data/local/bin/server" 2>/dev/null')).trim();
    if (linkAfterClear) {
      throw new Error(`Failed to clear pre-existing install — /data/local/bin/server still links to "${linkAfterClear}" after rm. Check permissions/mount state with "su -c mount" before retrying.`);
    }
    // Deliberately NOT separately checking that server_a/server_b are
    // gone via `ls`, `test -f`, or c.pull()/cat: readlink above just
    // demonstrated that this device's toolbox/mksh emits error TEXT for
    // a missing target rather than empty output, on a command this
    // codebase already trusted to behave the "normal" way. cat is a
    // strong candidate to do the same (`cat: ...: No such file`), which
    // would leak into c.pull()'s captured output and make this check
    // false-positive on a perfectly clean device — turning a working
    // provision into a hard abort, which is worse than the silent-stale
    // bug this whole block exists to fix. The rm output above is already
    // logged for visibility, and the install verification below checks
    // server_a's PRESENCE with correct content after the fresh write —
    // checking something exists with known content is safe to verify;
    // checking something doesn't exist, on this device, has already
    // proven not to be straightforward. If rm silently failed on a
    // locked/mounted-readonly server_a, the subsequent cp in the install
    // step would either overwrite it (fine) or fail loudly and get
    // caught by that verification anyway.
    addLog('Cleared.', 'ok');

    addLog('Installing to /data/local/bin/ (A slot)…');
    // Each step checked individually instead of && chained — the original
    // bug here was a chained mkdir/cp/chmod/ln with no stderr capture and
    // no output check, so a silent cp/ln failure (disk full, permission,
    // anything) would short-circuit the chain before ln -sf ran. With the
    // directory now guaranteed empty above, a partial failure here is
    // unambiguous: if cp fails, server_a simply won't exist, and the
    // verification below catches it precisely rather than guessing.
    const cpOut = (await c.shell('su -c "cp /sdcard/server_new /data/local/bin/server_a" 2>&1')).trim();
    if (cpOut) addLog(`  → cp: ${cpOut}`);
    const chmodOut = (await c.shell('su -c "chmod 755 /data/local/bin/server_a" 2>&1')).trim();
    if (chmodOut) addLog(`  → chmod: ${chmodOut}`);
    const lnOut = (await c.shell('su -c "ln -sf server_a /data/local/bin/server" 2>&1')).trim();
    if (lnOut) addLog(`  → ln: ${lnOut}`);

    // Verify the symlink actually points where we just told it to, and
    // that the bytes on disk match what we pushed. Deliberately NOT using
    // `wc -c` or any other shell tool here that hasn't already been
    // proven on this device — this device has burned multiple sessions on
    // assumed-present tools turning out missing (awk/cut/head/printf/
    // which all confirmed absent), and a verification step that throws a
    // false positive because of a missing tool is worse than no
    // verification at all. c.pull() is already proven (it's how every
    // other pull in this wizard works), so reuse it for the size check
    // instead of trusting a new shell command's availability.
    const linkTarget = (await c.shell('su -c "readlink /data/local/bin/server" 2>/dev/null')).trim();
    if (linkTarget !== 'server_a') {
      throw new Error(`Install verification failed: /data/local/bin/server points to "${linkTarget || '(empty — symlink missing)'}", expected "server_a". The cp/ln chain likely failed — check the install output above and free space on /data with "su -c df".`);
    }
    const installedBytes = await c.pull('/data/local/bin/server_a');
    if (installedBytes.length !== buf.byteLength) {
      throw new Error(`Install verification failed: /data/local/bin/server_a is ${installedBytes.length.toLocaleString()} bytes on device, expected ${buf.byteLength.toLocaleString()}. The copy likely failed or was truncated — check free space on /data.`);
    }
    addLog(`Verified: server → server_a (${installedBytes.length.toLocaleString()} bytes, matches pushed binary).`, 'ok');

    addLog('Fetching startup script from controller…');
    const resp2 = await fetch(ingressPath('/api/provision/start_script'), { headers: { Authorization: `Bearer ${token}` } });
    if (!resp2.ok) throw new Error(`Controller returned ${resp2.status}`);
    const script = await resp2.text();
    // Same "Text file busy" risk as wificfg.sh — push + immediate chmod/exec
    // can race with the cat process. start_server.sh isn't executed
    // immediately here (only copied), so push() is safe for this one.
    await c.push('/sdcard/start_server.sh', new TextEncoder().encode(script));
    await c.shell("su -c 'cp /sdcard/start_server.sh /data/local/bin/start_server.sh && chmod 755 /data/local/bin/start_server.sh'");
    addLog('EchoMuse installed.', 'ok');

    // Device-link TLS credentials — pushed pre-first-contact so the very
    // first connection this device ever makes to the controller is wss +
    // token-authenticated. The controller mints the token against the
    // serial (a pending device row is created if needed; approval flow is
    // unchanged). A 503 means this controller has no TLS listener
    // (cryptography package missing) — provision proceeds plain, and the
    // dashboard "Secure link" action can retrofit credentials later.
    addLog('Fetching device-link TLS credentials…');
    const serial = (await c.shell('getprop ro.serialno')).trim();
    if (!serial) {
      addLog('Could not read device serial — skipping TLS credential install.', 'warn');
    } else {
      const tlsResp = await fetch(ingressPath('/api/provision/tls_credentials'), {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id: serial }),
      });
      if (tlsResp.status === 503) {
        addLog('Controller has no TLS listener — device will connect over plain ws.', 'warn');
      } else if (!tlsResp.ok) {
        throw new Error(`Controller returned ${tlsResp.status} fetching TLS credentials.`);
      } else {
        const creds = await tlsResp.json();
        await c.push('/sdcard/em-ca.pem', new TextEncoder().encode(creds.ca_pem));
        await c.push('/sdcard/em-token', new TextEncoder().encode(creds.token));
        await c.shell(`su -c 'mkdir -p ${creds.dir} && cp /sdcard/em-ca.pem ${creds.dir}/ca.pem && cp /sdcard/em-token ${creds.dir}/token && chmod 644 ${creds.dir}/ca.pem && chmod 600 ${creds.dir}/token && rm -f /sdcard/em-ca.pem /sdcard/em-token'`);
        const tlsListing = (await c.shell(`su -c 'ls ${creds.dir}' 2>&1`)).trim();
        if (!tlsListing.includes('ca.pem') || !tlsListing.includes('token')) {
          throw new Error(`TLS credential install verification failed — ${creds.dir} contains: "${tlsListing}".`);
        }
        addLog('TLS credentials installed — device will connect over wss.', 'ok');
      }
    }
    // NO reboot here. This step used to end the wizard, so it rebooted, closed
    // the connection and cleared `adb` — and when the wake word asset step was
    // appended after it, that step's auto-run gate (`&& adb`) was false, so it
    // silently never fired and the wizard just sat on it. The finishing reboot
    // belongs to whichever step is genuinely last; it now lives in
    // runInstallOwwAssets. Anything added after that must move it again.
    addLog('Staying connected — the wake word assets install next, then the device reboots.');
  }

  // ── Step executor ──
  async function runInstallOwwAssets(c) {
    // Pushed over USB rather than through the shell plane: a freshly-flashed
    // device is not connected to the controller yet, and 15MB of base64
    // heredoc would be slow. Same bytes and same destination as the field
    // path — only the transport differs.
    addLog('Fetching wake word assets from controller…');
    const manifest = await API.get('/api/provision/oww_assets');
    (manifest.problems || []).forEach(p => addLog(`  ⚠ ${p}`, 'error'));
    if (!manifest.assets || manifest.assets.length === 0) {
      throw new Error('Controller has no wake word assets to install. '
        + 'Its image may predate them — rebuild or update the controller.');
    }

    await c.shell(`su -c "mkdir -p ${manifest.dir}"`);
    for (const a of manifest.assets) {
      addLog(`Pushing ${a.name} (${(a.size/1024/1024).toFixed(1)} MB)…`);
      const resp = await fetch(ingressPath(`/api/provision/oww_asset/${encodeURIComponent(a.name)}`),
                               { headers: { Authorization: `Bearer ${token}` } });
      if (!resp.ok) throw new Error(`Controller returned ${resp.status} fetching ${a.name}.`);
      const buf = await resp.arrayBuffer();
      // Staged in /sdcard because adb cannot write under /data directly, then
      // moved with su — the same two-step the binary install uses.
      await c.push('/sdcard/em_oww_asset', new Uint8Array(buf),
        pct => setProgress({ label: `Uploading ${a.name}`, pct }));
      setProgress(null);

      // md5 is the only definition of success: a truncated push produces a
      // file of plausible size that fails later at dlopen, with an error that
      // names nothing useful.
      const got = (await c.shell('su -c "busybox md5sum /sdcard/em_oww_asset" 2>/dev/null')).trim().split(/\s+/)[0];
      if (got !== a.md5) {
        await c.shell('su -c "rm -f /sdcard/em_oww_asset"');
        throw new Error(`${a.name} arrived corrupted (md5 ${got || 'unreadable'}, expected ${a.md5}).`);
      }
      await c.shell(`su -c "mv /sdcard/em_oww_asset ${manifest.dir}/${a.name} && chmod 644 ${manifest.dir}/${a.name}"`);
      addLog(`  ✓ ${a.name}`);
    }
    addLog('Wake word assets installed. On-device scoring is off by default — '
         + 'enable it per device under Config → Wake word.', 'ok');

    // The finishing reboot lives in the LAST step, deliberately — it closes
    // the ADB connection, so any step after it can never run. Keeping it on
    // the success path (not in a finally) means a failure above leaves the
    // connection alive and the Retry button usable.
    addLog('Rebooting device to finish provisioning…');
    expectDisconnect.current = true;
    try { await c.shell('su -c reboot'); } catch {}
    await c.close();
    setAdb(null);
    addLog('Device rebooting. It will appear in the controller dashboard within ~30s via mDNS.', 'ok');
  }

  async function runStep(stepIdx, useLatest) {
    // Captured up front. If the cable is pulled mid-step the handler above
    // bumps this and has already set the UI to a usable state, so everything
    // below must become a no-op — including the failure path, which would
    // otherwise spend ~100s probing a device that is not there.
    const epoch = stepEpoch.current;
    const abandoned = () => epoch !== stepEpoch.current;

    setRunning(true);
    markStep(stepIdx, 'running');
    // One banner per step. The transcript is the only record of a provision
    // and people paste it when something goes wrong — without these it is a
    // single 200-line stream with no way to tell which step a message
    // belongs to, or which one a failure happened in.
    addLog(`── ${stepIdx + 1}/${_WIZARD_STEPS.length}  ${_WIZARD_STEPS[stepIdx].label.toUpperCase()} ──`, 'head');
    let c = adb;
    try {
      // Every step but the three connection steps needs a live handle. Passing
      // a null one through produced an error naming a property of undefined,
      // which says nothing about the cable having been unplugged.
      if (!CONNECT_STEPS.has(stepIdx) && !c) {
        throw new Error('There is no ADB connection. Click Reconnect, pick the '
                      + 'device from the USB picker, then Retry this step.');
      }
      switch (stepIdx) {
        case  0: c = await runConnectAndroid(); break;
        case  1: c = await runConnectTwrp(); break;
        case  2: await runPatchBoot(c); break;
        case  3: await runInstallMagisk(c, magiskFile); break;
        case  4: await runPreseedDb(c); break;
        case  5: await runReboot(c); break;
        case  6: c = await runReconnect(); break;
        case  7: await runVerifyRoot(c); break;
        case  8: await runDisableAlexa(c); break;
        case  9: await runDebloat(c); break;
        case 10: await runConfigWifi(c, wifiSsid, wifiPsk); break;
        case 11: await runInstallEchoMuse(c, binaryFile, useLatest); break;
        case 12: await runInstallOwwAssets(c); break;
      }
      if (abandoned()) return;
      markStep(stepIdx, 'done');
      if (stepIdx < _WIZARD_STEPS.length - 1) setStep(stepIdx + 1);
    } catch (e) {
      // A step abandoned mid-flight may still throw on its way out, once the
      // transport notices. The UI already says what happened; saying it again
      // in the language of whatever call happened to fail is noise.
      if (abandoned()) return;
      // The in-flight transfer can also throw BEFORE the disconnect event
      // arrives — the two race, and on a real run the throw won. That put
      // "Failed to execute 'transferOut' on 'USBDevice'" in the transcript as
      // though it were a provisioning failure, and then sent the diagnostics
      // probes at a device that was no longer plugged in. Take the same exit
      // the listener would have.
      if (_isDisconnectError(e)) {
        abandonStep('Device disconnected. The step was abandoned — reconnect and retry.', stepIdx);
        setAdb(null);
        return;
      }
      addLog(`Error: ${e.message}`, 'error');
      markStep(stepIdx, 'error');
      if (e.matchedDeviceId) setDuplicateDeviceId(e.matchedDeviceId);
      // Collect device state while it is still the state that failed. A
      // duplicate-device stop is our own bookkeeping and has nothing to ask
      // the device about.
      if (!e.matchedDeviceId) await captureDiagnostics(stepIdx, e);
      // Clear the file selection on failure — forces a deliberate reselect
      // before retry rather than silently re-flashing whatever was picked
      // last time (which, on a hash-mismatch failure, is the wrong file).
      if (stepIdx === 3) setMagiskFile(null);
      if (stepIdx === 11) setBinaryFile(null);
    }
    if (!abandoned()) setRunning(false);
  }

  // Auto-advance steps that need no user input once adb is connected.
  // Step 8 (disable_alexa) is now before WiFi so Alexa can't phone home.
  // Step 9 (debloat) rides the same connected-and-rooted state.
  useEffect(() => {
    // Step 12 (wake word assets) auto-runs: it needs no input, and making it
    // a button people can leave unpressed defeats the point of it being
    // mandatory.
    const autoSteps = new Set([2, 4, 7, 8, 9, 12]);
    if (!autoSteps.has(step) || running || stepState[step] !== 'pending') return;
    if (adb) { runStep(step); return; }
    // An auto step with no connection used to be a no-op, so the wizard sat on
    // it looking busy forever — which is exactly how the wake word asset step
    // failed on 2026-07-31, reached only after the previous step had rebooted
    // the device out from under it. A step that cannot start must SAY so.
    addLog(`"${_WIZARD_STEPS[step].label}" needs an ADB connection and there isn't one — `
         + `the previous step disconnected the device. Reconnect and click Retry.`, 'error');
    markStep(step, 'error');
  }, [step, running, adb]);

  const cur    = _WIZARD_STEPS[step];
  const isDone = step === _WIZARD_STEPS.length - 1 && stepState[step] === 'done';
  const doneCount = stepState.filter(s => s === 'done').length;
  // +0.35 for a running step is deliberate: it nudges the bar off the
  // completed count so an in-flight step reads as progress rather than as a
  // stall, but stays well under a full step so a step that hangs is still an
  // obvious non-finish. It is a human-chosen fraction, not a derived figure.
  const progressPct = Math.min(100, ((doneCount + (running ? 0.35 : 0)) / _WIZARD_STEPS.length) * 100);
  const upcoming = _WIZARD_STEPS.slice(step + 1, step + 3);
  const stepFailed = !running && stepState[step] === 'error';
  const statusColors = { pending: 'var(--muted)', running: 'var(--accent)', done: 'var(--ok)', error: 'var(--warn)' };
  const statusIcons  = { pending: '○', running: '◌', done: '●', error: '✕' };

  function recoveryHint() {
    if (CONNECT_STEPS.has(step)) {
      return adb
        ? 'The USB link is up but this step failed — click Retry to run it again.'
        : 'Pick the device from the USB picker, then click Retry.';
    }
    if (INPUT_STEPS.has(step)) {
      return diagnostics
        ? 'Fix the input above, then retry. If it keeps failing, download diagnostics and attach them to your issue.'
        : 'Fix the input above and retry. Reconnect if the device was unplugged.';
    }
    if (diagnostics) {
      return 'Click Retry first. If it fails again, download diagnostics — the transcript is included.';
    }
    return 'Click Retry to run this step again. Use Reconnect if the cable was unplugged.';
  }

  return (
    /* Same overlay + frame treatment as the Detail and Settings modals —
       warm blurred backdrop, fixed 900×700 frame, gradient header band,
       circular close button. */
    <div style={{
      position: 'fixed', inset: 0, zIndex: 200,
      background: 'rgba(180,176,168,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center',
      backdropFilter: 'blur(8px)',
    }}>
      <div style={{
        background: 'linear-gradient(170deg,var(--raised),var(--surface))', border: '1px solid var(--border)',
        borderRadius: 16, width: 'min(900px,95vw)', height: 'min(700px,90vh)',
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
        boxShadow: '0 24px 80px rgba(0,0,0,0.3),0 2px 0 var(--sheen) inset',
        animation: 'fadeIn 0.15s ease',
      }}>

        {/* Header */}
        <div style={{ background: 'linear-gradient(180deg,var(--card),var(--bg))', borderBottom: '1px solid var(--border-hard)', padding: '20px 24px 16px', boxShadow: '0 1px 0 var(--sheen) inset', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
            <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 22, fontWeight: 600, color: 'var(--text)', letterSpacing: '-0.02em' }}>Provision Echo Dot</div>
            <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', letterSpacing: '0.12em', textTransform: 'uppercase', marginTop: 4 }}>Chrome/Edge only · USB-A cable · amonet-biscuit prerequisite</div>
          </div>
          <CircleButton onClick={onClose} title="Close">×</CircleButton>
        </div>

        <div style={{ display: 'flex', flex: 1, overflow: 'hidden', minHeight: 0 }}>

          {/* Step list + overall progress */}
          <div style={{ width: 196, borderRight: '1px solid var(--border)', background: 'var(--hairline)', display: 'flex', flexDirection: 'column', flexShrink: 0 }}>
            <div className="em-wizard-progress">
              <div className="em-wizard-progress__track">
                <div className="em-wizard-progress__fill" style={{ width: `${progressPct}%` }}/>
              </div>
              <div className="em-wizard-progress__label">
                {doneCount} of {_WIZARD_STEPS.length} complete · step {step + 1}
              </div>
            </div>
            <div style={{ flex: 1, overflowY: 'auto', padding: '6px 0 12px' }}>
            {_WIZARD_STEPS.map((s, i) => {
              const st = stepState[i]; const active = i === step;
              return (
                <div key={s.id}
                  className={[
                    'em-wizard-step',
                    active && 'em-wizard-step--active',
                    st === 'done' && 'em-wizard-step--done',
                    st === 'error' && 'em-wizard-step--error',
                    st === 'pending' && 'em-wizard-step--pending',
                  ].filter(Boolean).join(' ')}
                  style={{ opacity: running && !active && st !== 'done' ? 0.55 : undefined }}>
                  <span className="em-wizard-step__num">{i + 1}</span>
                  <span className="em-wizard-step__icon" style={{ color: statusColors[st] }}>{statusIcons[st]}</span>
                  <span className="em-wizard-step__label">{s.label}</span>
                </div>
              );
            })}
            </div>
          </div>

          {/* Content */}
          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', padding: '18px 22px 14px' }}>

            {/* Step title + desc */}
            <div style={{ marginBottom: 12 }}>
              <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 14, fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>
                {step + 1}. {cur.label}
              </div>
              <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--text2)', lineHeight: 1.6 }}>{cur.desc}</div>
            </div>

            {upcoming.length > 0 && !isDone && (
              <div className="em-wizard-upcoming">
                <div className="em-label" style={{ marginBottom: 6 }}>Up next</div>
                {upcoming.map(s => (
                  <div key={s.id} className="em-wizard-upcoming__item">
                    <span className="em-wizard-upcoming__name">{s.label}</span>
                    <span className="em-wizard-upcoming__desc">{s.desc}</span>
                  </div>
                ))}
              </div>
            )}

            {running && (
              <div className={'em-wizard-status' + (waiting ? ' em-wizard-status--wait' : '')}>
                <span className="em-wizard-status__dot"/>
                <span>
                  {waiting
                    ? 'Waiting on the device — this can take several minutes and is normal.'
                    : 'Working on this step…'}
                </span>
              </div>
            )}

            {/* ── Step-specific controls ── */}

            {/* Steps 0, 1, 6: connect / reconnect buttons */}
            {CONNECT_STEPS.has(step) && stepState[step] === 'pending' && !running && (
              <div style={{ marginBottom: 10 }}>
                <Pill onClick={() => runStep(step)}>
                  {step === 0 ? 'Connect Device' : step === 1 ? 'Connect to TWRP' : 'Reconnect Device'}
                </Pill>
              </div>
            )}

            {/* Step 5: reboot button */}
            {step === 5 && stepState[5] === 'pending' && !running && (
              <div style={{ marginBottom: 10 }}>
                <Pill onClick={() => runStep(5)}>Reboot to Android</Pill>
              </div>
            )}

            {/* Step 3: Magisk zip file picker — stays visible through error so a
                different file can be picked, not just gone after one attempt */}
            {step === 3 && stepState[3] !== 'done' && !running && (
              <div style={{ marginBottom: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
                <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--text2)', letterSpacing: '0.08em' }}>
                  {stepState[3] === 'error' ? 'SELECT A DIFFERENT FILE' : 'MAGISK-V17.3.ZIP'}
                </div>
                <input
                  type="file" accept=".zip"
                  onChange={e => setMagiskFile(e.target.files[0])}
                  style={{ fontFamily: "'DM Mono',monospace", fontSize: 11 }}
                />
                {!!magiskFile && <Pill onClick={() => runStep(3)}>Flash Magisk</Pill>}
              </div>
            )}

            {/* Step 11: EchoMuse binary — custom upload or latest from controller.
                Stays visible through error so a different file/source can be
                tried instead of being stuck retrying whatever failed. */}
            {step === 11 && stepState[11] !== 'done' && !running && (
              <div style={{ marginBottom: 12, display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                  <Pill accent onClick={() => runStep(11, true)}>Install latest from GitHub</Pill>
                  <Pill small onClick={doCheckRelease} disabled={checkingRelease}>
                    {checkingRelease ? 'Checking…' : 'Check for newer release'}
                  </Pill>
                  {latestRelease && (
                    <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)' }}>
                      Latest on GitHub: {latestRelease.version}
                    </span>
                  )}
                </div>
                <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', letterSpacing: '0.04em' }}>— or —</div>
                <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--text2)', letterSpacing: '0.08em' }}>
                  {stepState[11] === 'error' ? 'SELECT A DIFFERENT BUILD (ARMv7)' : 'CUSTOM ECHOMUSE SERVER BINARY (ARMv7)'}
                </div>
                <input
                  type="file"
                  onChange={e => setBinaryFile(e.target.files[0])}
                  style={{ fontFamily: "'DM Mono',monospace", fontSize: 11 }}
                />
                {!!binaryFile && <Pill onClick={() => runStep(11, false)}>Install Custom Build</Pill>}
              </div>
            )}

            {/* Step 10: WiFi configuration */}
            {step === 10 && stepState[10] !== 'done' && !running && (
              <WifiPanel
                adb={adb}
                wifiSsid={wifiSsid} setWifiSsid={setWifiSsid}
                wifiPsk={wifiPsk}   setWifiPsk={setWifiPsk}
                onScan={() => scanWifi(adb).then(nets => setWifiNetworks(nets)).catch(e => addLog(`Scan failed: ${e.message}`, 'error'))}
                networks={wifiNetworks}
                onConnect={() => { if (wifiSsid) runStep(10); }}
                onSkip={() => { markStep(10, 'done'); setStep(11); }}
                onAbort={() => { markStep(10, 'error'); addLog('WiFi skipped — provision incomplete.', 'warn'); }}
              />
            )}

            {/* The only control available while a step is in flight. The
                disconnect listener handles the cable being pulled, but a step
                can stall for reasons the browser does not report — a device
                that has stopped answering while still enumerated, say — and
                without this the only way out is reloading the page, which
                loses the transcript the operator would otherwise paste into
                an issue. */}
            {running && (
              <div style={{ marginBottom: 10, display: 'flex', gap: 8 }}>
                <Pill danger onClick={() => abandonStep('Step cancelled.')}>Cancel step</Pill>
              </div>
            )}

            {/* Retry / recovery — one panel so the primary action is obvious */}
            {stepFailed && !INPUT_STEPS.has(step) && (
              <div className="em-panel em-wizard-recovery">
                <div className="em-label">This step failed</div>
                <p className="em-wizard-recovery__hint">{recoveryHint()}</p>
                <div className="em-wizard-recovery__actions">
                  <Pill accent onClick={() => runStep(step)}>Retry</Pill>
                  {!CONNECT_STEPS.has(step) && (
                    <Pill onClick={reconnectAdb}>{adb ? 'Reconnect' : 'Reconnect device'}</Pill>
                  )}
                  {diagnostics && (
                    <Pill small onClick={downloadDiagnostics}>Download diagnostics</Pill>
                  )}
                  {step === 0 && duplicateDeviceId && (
                    <Pill danger onClick={async () => {
                      try {
                        await API.del(`/api/devices/${duplicateDeviceId}`);
                        addLog(`Deleted "${duplicateDeviceId}" from controller. You can retry now.`, 'ok');
                        setDuplicateDeviceId(null);
                        markStep(0, 'pending');
                      } catch (e) {
                        addLog(`Delete failed: ${e.error || e.message || 'unknown error'} — check /api/devices/{id} DELETE exists in em_api.py.`, 'error');
                      }
                    }}>Delete "{duplicateDeviceId}" from controller</Pill>
                  )}
                </div>
              </div>
            )}

            {stepFailed && [3, 10, 11].includes(step) && (
              <div className="em-panel em-wizard-recovery">
                <div className="em-label">This step failed</div>
                <p className="em-wizard-recovery__hint">{recoveryHint()}</p>
                <div className="em-wizard-recovery__actions">
                  <Pill onClick={reconnectAdb}>{adb ? 'Reconnect' : 'Reconnect device'}</Pill>
                  {diagnostics && (
                    <Pill small onClick={downloadDiagnostics}>Download diagnostics</Pill>
                  )}
                </div>
              </div>
            )}

            {/* Progress bar — accent slate, same as toggles/sliders */}
            {progress && (
              <div style={{ margin: '6px 0 10px' }}>
                <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', marginBottom: 4 }}>{progress.label}</div>
                <div style={{ height: 4, background: 'var(--sunken)', borderRadius: 2 }}>
                  <div style={{ height: '100%', width: `${Math.min(100, (progress.pct || 0) * 100).toFixed(0)}%`, background: 'var(--accent)', borderRadius: 2, transition: 'width 0.2s' }}/>
                </div>
              </div>
            )}

            {/* Done message */}
            {isDone && (
              <div style={{ margin: '6px 0 10px', display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: 'var(--ok)', lineHeight: 1.7 }}>
                  Provisioning complete. The device has rebooted and will discover the controller via mDNS,
                  appearing in the dashboard as a pending device within ~30s.
                </div>
                <div><Pill accent onClick={onClose}>Done</Pill></div>
              </div>
            )}

            {/* Log output — same console treatment as the Updates tab.
                The copy action matters more than it looks: this transcript is
                the entire record of a provision, and it is what gets pasted
                when something needs diagnosing. Selecting it by hand out of a
                scrolling box loses the top of it. */}
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginTop: 10 }}>
              <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.15em' }}>
                Output{log.length > 0 ? ` — ${log.length} lines` : ''}
              </span>
              {log.length > 0 && (
                <Pill small onClick={() => {
                  const text = log.map(e => e.msg).join('\n');
                  navigator.clipboard.writeText(text)
                    .then(() => addLog('(transcript copied to clipboard)'))
                    .catch(() => addLog('Clipboard blocked by the browser — select the text manually.', 'warn'));
                }}>Copy log</Pill>
              )}
            </div>
            <div
              ref={logRef}
              className="em-console"
              style={{ flex: 1, minHeight: 0, marginTop: 10 }}
            >
              {log.length === 0
                ? <span style={{ color: 'var(--lcd-faint)' }}>— no output yet —</span>
                : log.map((e, i) => (
                  <div key={i} className={_wizardLogClass(e.msg, e.type)}>
                    {e.msg}
                  </div>
                ))
              }
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── App ──────────────────────────────────────────────────────────────────────

// ─── DeviceConfigForm ─────────────────────────────────────────────────────────
// Shared config form used by both the per-device config tab and the global
// settings panel. disabled=true renders all controls read-only.

// ─── DeviceDiagram ────────────────────────────────────────────────────────────
// Top-down Echo Dot diagram. 0°=top (vol+ button / cable), clockwise.
// SVG coords: x=sin(deg)*r, y=-cos(deg)*r
// MK1=330° (-45,-78), MK2=30° (45,-78), MK3=90° (90,0),
// MK4=150° (45,78),   MK5=210° (-45,78), MK6=270° (-90,0)

function DeviceDiagram({ activeMics, patternType }) {
  const MIC_POS = {
    mk1: [-45,-78], mk2: [45,-78], mk3: [90,0],
    mk4: [45,78],   mk5: [-45,78], mk6: [-90,0],
  };
  const ALL = Object.keys(MIC_POS);

  return (
    <svg width="200" height="200" viewBox="-110 -110 220 220" style={{ display:'block', overflow:'visible' }}>
      <defs>
        <radialGradient id="dcfsg" cx="35%" cy="30%" r="70%">
          <stop offset="0%" stopColor="#3a3a3a"/>
          <stop offset="40%" stopColor="#242424"/>
          <stop offset="100%" stopColor="#161616"/>
        </radialGradient>
        <radialGradient id="dcfbg" cx="35%" cy="30%" r="65%">
          <stop offset="0%" stopColor="#323232"/>
          <stop offset="100%" stopColor="#1c1c1c"/>
        </radialGradient>
        <filter id="dcfmg" x="-100%" y="-100%" width="300%" height="300%">
          <feGaussianBlur stdDeviation="3.5" result="b"/>
          <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
        <filter id="dcfpg" x="-30%" y="-30%" width="160%" height="160%">
          <feGaussianBlur stdDeviation="5" result="b"/>
          <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
        <clipPath id="dcfsc"><circle cx="0" cy="0" r="104"/></clipPath>
        <pattern id="dcfgr" patternUnits="userSpaceOnUse" width="5" height="5">
          <circle cx="2.5" cy="2.5" r="0.8" fill="rgba(0,0,0,0.3)"/>
        </pattern>
      </defs>

      {/* Pickup pattern — behind shell */}
      {patternType === 'omni' && <>
        <circle cx="0" cy="0" r="116" fill="rgba(64,88,120,0.07)" stroke="rgba(64,88,120,0.25)" strokeWidth="5" filter="url(#dcfpg)"/>
        <circle cx="0" cy="0" r="116" fill="none" stroke="#405878" strokeWidth="1.5" strokeDasharray="5 3"/>
      </>}
      {patternType === 'front' && <>
        <path d="M-116,0 A116,116 0 0,0 116,0 Z" fill="rgba(64,88,120,0.07)" stroke="rgba(64,88,120,0.25)" strokeWidth="5" filter="url(#dcfpg)"/>
        <path d="M-116,0 A116,116 0 0,0 116,0 Z" fill="rgba(64,88,120,0.09)" stroke="#405878" strokeWidth="1.5"/>
      </>}
      {patternType === 'rear' && <>
        <path d="M-116,0 A116,116 0 0,1 116,0 Z" fill="rgba(64,88,120,0.07)" stroke="rgba(64,88,120,0.25)" strokeWidth="5" filter="url(#dcfpg)"/>
        <path d="M-116,0 A116,116 0 0,1 116,0 Z" fill="rgba(64,88,120,0.09)" stroke="#405878" strokeWidth="1.5"/>
      </>}

      {/* Shell */}
      <circle cx="0" cy="0" r="108" fill="#0a0a0a"/>
      <circle cx="0" cy="0" r="104" fill="url(#dcfsg)"/>

      {/* LED ring */}
      <circle cx="0" cy="0" r="96" fill="none" stroke="#0d0d0d" strokeWidth="11" clipPath="url(#dcfsc)"/>
      <circle cx="0" cy="0" r="96" fill="none"
        stroke={patternType === 'omni' ? '#40906a' : '#40906a'}
        strokeWidth="7" strokeDasharray="36.3 14.0"
        transform="rotate(-90)" clipPath="url(#dcfsc)"/>
      <circle cx="0" cy="0" r="96" fill="none" stroke="#161616" strokeWidth="11"
        strokeDasharray="1.5 49" transform="rotate(-90)" clipPath="url(#dcfsc)"/>

      {/* Inner disc */}
      <circle cx="0" cy="0" r="82" fill="#1a1a1a"/>
      <circle cx="0" cy="0" r="82" fill="url(#dcfgr)" clipPath="url(#dcfsc)"/>
      <circle cx="0" cy="0" r="82" fill="none" stroke="rgba(255,255,255,0.04)" strokeWidth="1"/>

      {/* Buttons */}
      <circle cx="0"   cy="-44" r="15" fill="url(#dcfbg)" stroke="rgba(255,255,255,0.05)" strokeWidth="0.5"/>
      <text x="0" y="-39" textAnchor="middle" fontSize="15" fill="var(--sheen)" fontFamily="sans-serif" fontWeight="300">+</text>
      <circle cx="44"  cy="0"   r="15" fill="url(#dcfbg)" stroke="rgba(255,255,255,0.05)" strokeWidth="0.5"/>
      <circle cx="44"  cy="0"   r="4.5" fill="var(--sheen)"/>
      <circle cx="0"   cy="44"  r="15" fill="url(#dcfbg)" stroke="rgba(255,255,255,0.05)" strokeWidth="0.5"/>
      <text x="0" y="50" textAnchor="middle" fontSize="15" fill="var(--sheen)" fontFamily="sans-serif" fontWeight="300">−</text>
      <circle cx="-44" cy="0"   r="15" fill="url(#dcfbg)" stroke="rgba(255,255,255,0.05)" strokeWidth="0.5"/>
      <g transform="translate(-44,0)">
        <rect x="-3.5" y="-7.5" width="7" height="10" rx="3.5" fill="var(--sheen)"/>
        <path d="M-6,1.5 Q-6,8 0,8 Q6,8 6,1.5" fill="none" stroke="var(--sheen)" strokeWidth="1.5" strokeLinecap="round"/>
        <line x1="0" y1="8" x2="0" y2="11" stroke="var(--sheen)" strokeWidth="1.5" strokeLinecap="round"/>
      </g>

      {/* Centre mic */}
      <circle cx="0" cy="0" r="2.5" fill="rgba(255,255,255,0.18)"/>

      {/* Perimeter mics */}
      {ALL.map(id => {
        const [cx, cy] = MIC_POS[id];
        const active = activeMics.includes(id);
        return (
          <g key={id}>
            <circle cx={cx} cy={cy} r="6"
              fill={active ? '#1a3a5a' : '#1e2020'}
              filter={active ? 'url(#dcfmg)' : undefined}/>
            <circle cx={cx} cy={cy} r="4" fill={active ? '#4a7ab8' : '#2a2a2a'}/>
          </g>
        );
      })}

      <ellipse cx="-14" cy="-20" rx="22" ry="13" fill="rgba(255,255,255,0.04)"/>
    </svg>
  );
}

// Mini version for preset cards
function DeviceDiagramMini({ activeMics, patternType }) {
  const MIC_POS = {
    mk1: [-45,-78], mk2: [45,-78], mk3: [90,0],
    mk4: [45,78],   mk5: [-45,78], mk6: [-90,0],
  };
  return (
    <svg width="52" height="52" viewBox="-110 -110 220 220">
      <circle cx="0" cy="0" r="108" fill="#0a0a0a"/>
      <circle cx="0" cy="0" r="104" fill="#222"/>
      <circle cx="0" cy="0" r="96" fill="none" stroke="#0d0d0d" strokeWidth="11"/>
      <circle cx="0" cy="0" r="96" fill="none"
        stroke={patternType === 'omni' ? '#40906a' : '#40906a'}
        strokeWidth="7" strokeDasharray="36.3 14" transform="rotate(-90)"/>
      <circle cx="0" cy="0" r="96" fill="none" stroke="#161616" strokeWidth="11"
        strokeDasharray="1.5 49" transform="rotate(-90)"/>
      <circle cx="0" cy="0" r="82" fill="#1a1a1a"/>
      {patternType === 'omni' && <circle cx="0" cy="0" r="68" fill="rgba(64,88,120,0.18)" stroke="#405878" strokeWidth="2"/>}
      {patternType === 'front' && <path d="M-68,0 A68,68 0 0,0 68,0 Z" fill="rgba(64,88,120,0.18)" stroke="#405878" strokeWidth="2"/>}
      {patternType === 'rear'  && <path d="M-68,0 A68,68 0 0,1 68,0 Z" fill="rgba(64,88,120,0.18)" stroke="#405878" strokeWidth="2"/>}
      {Object.entries(MIC_POS).map(([id,[cx,cy]]) => (
        <circle key={id} cx={cx} cy={cy} r="4"
          fill={activeMics.includes(id) ? '#4a7ab8' : '#2a2a2a'}/>
      ))}
    </svg>
  );
}


// ─── DeviceConfigForm ─────────────────────────────────────────────────────────
// The config rendered as the actual signal path: numbered stages from the
// microphones to the speaker, each labelled with WHERE it runs (device /
// controller) and WHAT it affects (wake stream / button turns / playback).
// Stage-specific advanced controls live inside their stage's disclosure, so
// "tucked away" never means "unclear what it belongs to".
// disabled=true = read-only (used when device is on global config).

// ScopeChip — small badge saying where a stage runs / what it affects.
function ScopeChip({ children, tone }) {
  const colors = {
    device:     { bg: 'rgba(40,96,64,0.12)',  border: 'rgba(40,96,64,0.35)',  text: 'var(--ok)' },
    controller: { bg: 'rgba(64,88,120,0.12)', border: 'rgba(64,88,120,0.35)', text: 'var(--accent)' },
    scope:      { bg: 'var(--hairline)',     border: 'rgba(0,0,0,0.15)',     text: 'var(--text2)' },
  }[tone || 'scope'];
  return (
    <span style={{
      fontFamily: "'DM Mono',monospace", fontSize: 8, textTransform: 'uppercase',
      letterSpacing: '0.1em', padding: '3px 8px', borderRadius: 4,
      background: colors.bg, border: `1px solid ${colors.border}`, color: colors.text,
      whiteSpace: 'nowrap',
    }}>{children}</span>
  );
}

// EqSliders — one vertical fader per band, ±12 dB. Live-updates eqBands so
// the curve above redraws as you drag.
function EqSliders({ bands, onChange, disabled }) {
  const FREQ_LABELS = ['125', '250', '500', '1k', '2k', '3.5k', '5.5k', '8k'];
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 2, ...(disabled ? { opacity: 0.45, pointerEvents: 'none' } : {}) }}>
      {bands.map((g, i) => (
        <div key={i} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', flex: 1, minWidth: 0 }}>
          <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 8, color: g !== 0 ? 'var(--accent)' : 'var(--muted)', marginBottom: 2, fontWeight: g !== 0 ? 600 : 400 }}>
            {(g > 0 ? '+' : '') + g}
          </div>
          {/* Native vertical slider via writing-mode — a rotate() transform
              renders fine but breaks drag gestures (pointer capture math
              stays in the untransformed axis, so only clicks land).
              orient="vertical" covers older Firefox. */}
          <input type="range" min={-12} max={12} step={1} value={g} orient="vertical"
            onChange={e => { const nb = [...bands]; nb[i] = Number(e.target.value); onChange(nb); }}
            style={{ writingMode: 'vertical-lr', direction: 'rtl', WebkitAppearance: 'slider-vertical', width: 20, height: 76, cursor: 'pointer' }}/>
          <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 8, color: 'var(--muted)', marginTop: 2 }}>{FREQ_LABELS[i]}</div>
        </div>
      ))}
    </div>
  );
}

// Stage / StageAdvanced — module-scope so React preserves component
// identity across DeviceConfigForm renders (inner definitions would remount
// the subtree every render, breaking slider drags mid-gesture).
const STAGE_MONO = "'DM Mono',monospace";

// Mirror of em_config_sections.SECTIONS — which config keys each stage owns,
// so a stage can be scoped to the fleet or to this device independently.
//
// Python is canonical. This literal is deliberately plain JSON (no comments,
// no trailing commas, double quotes) because tests/test_config_sections.py
// parses it straight out of this file and fails if the two ever drift — a
// control sitting under a toggle that does not govern it would look fine and
// be silently wrong.
const CONFIG_SECTIONS = {
  "playback": ["eqBands", "eqLoudness", "duckDb", "limiterEnabled", "limiterThreshold", "limiterRelease", "bassGuardEnabled", "bassGuardDb"],
  "wakeword": ["owwModel", "owwThreshold", "owwSpeexNs", "bargeInEnabled", "bargeInThreshold", "wakeArbitrationMs", "owwOnDevice"],
  "microphones": ["adcMicpga", "adcDigitalGain", "micGainDb", "beamformingEnabled", "beamAngle", "aecEnabled", "aecDelayMs", "aecTailMs", "aecRefSource", "nsAsr", "saveUtterances"],
  "ring": ["ledScene", "ledListenColor", "ledThinkColor", "meterAttack", "meterDecay", "meterFloor", "meterGamma", "meterRef", "meterCurve"],
  "advanced": ["agcEnabled", "vadThreshold", "vadSpeechMs", "vadSilenceMs", "buttonSingleTapEvent", "buttonMultiTapMs"],
  "bluetooth": ["bleProxyEnabled"]
};

// Display labels for the section ids, and the reverse key -> section index
// that lets a write be gated by the section owning the key it touches.
const SECTION_LABELS = {
  playback: 'Playback', wakeword: 'Wake word', microphones: 'Microphones',
  ring: 'Ring', advanced: 'Advanced', bluetooth: 'Bluetooth',
};
const KEY_SECTION = {};
Object.entries(CONFIG_SECTIONS).forEach(([sid, keys]) => {
  keys.forEach(k => { KEY_SECTION[k] = sid; });
});
// Mirror of em_config_sections.STATE_KEYS — always the device's own,
// never fleet-inherited, whatever the section scoping says.
const STATE_KEYS = ['startupVolume'];

// Effective config = fleet, with the device's own values layered over it for
// the sections it overrides. This must FILTER rather than blind-merge
// device.config: a row migrated from the pre-v8 boolean still carries values
// for every section, so a plain merge shows a device's stale settings while
// every stage claims to be following the fleet (observed on Office, which
// displayed hey_rhasspy/standard while actually running hey_mycroft/malevolent).
function effectiveConfig(globalConfig, device) {
  const secs = device.config_sections ?? [];
  const own  = device.config || {};
  const out  = { ...(globalConfig || {}) };
  Object.keys(own).forEach(k => {
    const sec = KEY_SECTION[k];
    if ((sec && secs.includes(sec)) || STATE_KEYS.includes(k)) out[k] = own[k];
  });
  return out;
}

// ScopeToggle — per-stage Fleet/Device switch. Shown only on a device's
// config (the fleet view has nothing to inherit from).
// The two states are filled SOLID rather than tinted. At the 0.12-0.16 alpha
// the rest of the page uses, this sat beside the ScopeChips in the same
// colours and read as another chip — a label, not something you could press.
// Solid fill plus a "SCOPE" caption makes it legible as a control and makes
// the current state obvious across a scrolling page.
const SCOPE_FLEET  = 'var(--accent)';  // same blue as the "controller" ScopeChip
const SCOPE_DEVICE = 'var(--ok)';  // same green as the "device" ScopeChip

function ScopeToggle({ local, onChange, disabled }) {
  const btn = (active, label, next, title) => (
    <button
      type="button"
      title={title}
      disabled={disabled}
      onClick={() => !disabled && onChange(next)}
      style={{
        fontFamily: STAGE_MONO, fontSize: 9, letterSpacing: '0.1em',
        textTransform: 'uppercase', padding: '4px 10px', border: 'none',
        cursor: disabled ? 'default' : 'pointer',
        background: active ? (next ? SCOPE_DEVICE : SCOPE_FLEET) : 'transparent',
        color: active ? 'var(--raised)' : 'var(--muted)',
        fontWeight: active ? 600 : 400,
        transition: 'background 0.15s, color 0.15s',
      }}>{label}</button>
  );
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 7, opacity: disabled ? 0.5 : 1 }}>
      <span style={{
        fontFamily: STAGE_MONO, fontSize: 8, letterSpacing: '0.12em',
        textTransform: 'uppercase', color: 'var(--muted)',
      }}>Scope</span>
      <span style={{
        display: 'inline-flex', borderRadius: 6, overflow: 'hidden',
        border: `1px solid ${local ? SCOPE_DEVICE : SCOPE_FLEET}`,
        background: 'var(--hairline)',
      }}>
        {btn(!local, 'Fleet', false, 'This section follows the fleet-wide setting')}
        {btn(local, 'Device', true, 'Override this section for this device only')}
      </span>
    </span>
  );
}

function Stage({ n, title, chips, desc, children, scope, dim }) {
  return (
    <Panel>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginBottom: 6, flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <span style={{ fontFamily: STAGE_MONO, fontSize: 10, color: 'var(--muted)' }}>{n}</span>
          <span style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 14, fontWeight: 600, color: 'var(--text)' }}>{title}</span>
        </div>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>{chips}{scope}</div>
      </div>
      <div style={{ fontFamily: STAGE_MONO, fontSize: 10, color: 'var(--muted)', lineHeight: 1.6, marginBottom: 14 }}>{desc}</div>
      {/* dim: a section following the fleet is shown read-only rather than
          hidden, so you can still see what it is inheriting. */}
      <div style={dim}>{children}</div>
    </Panel>
  );
}

function StageAdvanced({ open, onToggle, disabledStyle, children }) {
  return (
    <div style={{ marginTop: 14, borderTop: '1px solid var(--hairline)', paddingTop: 10 }}>
      <div onClick={onToggle} style={{
        fontFamily: STAGE_MONO, fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase',
        letterSpacing: '0.15em', cursor: 'pointer', userSelect: 'none',
        display: 'flex', alignItems: 'center', gap: 6,
      }}>
        <span>{open ? '▾' : '▸'}</span> Advanced
      </div>
      {open && <div style={{ marginTop: 14, ...disabledStyle }}>{children}</div>}
    </div>
  );
}

// Mirrors em_shadow.normalise_mode: an unrecognised stored value renders as
// "Controller" rather than leaving every segment unselected, which would look
// like a control that had lost its value.
function onDeviceMode(config) {
  const v = String(config.owwOnDevice ?? 'off').toLowerCase();
  return ['off', 'shadow', 'on'].includes(v) ? v : 'off';
}

function DeviceConfigForm({ config, onChange, disabled, sections, onScopeChange,
                            shadowCapable = true, mixCapable = true,
                            holdCapable = true, triggerCapable = true,
                            hwEchoRef = false, hwRefCapable = true }) {
  // hwEchoRef defaults FALSE while its neighbours default TRUE, because it
  // is the only one that DISABLES a control rather than enabling one. The
  // fleet view has no single device to ask, so it keeps the AEC delay
  // slider live — which is right either way: the value is still pushed, and
  // still used by any device that falls back to the software tap.
  // shadowCapable defaults TRUE because this form is also the fleet-config
  // view, where there is no single device whose capability could gate a
  // control. Referencing a `device` here is what blank-screened the Config
  // tab: the prop does not exist, so `device.connected` threw during render.
  // sections == null means the fleet-config view: nothing to inherit from, so
  // no per-section switches and every control is live.
  const scoped = Array.isArray(sections);
  const isLocal = id => !scoped || sections.includes(id);

  // Writes are gated by the OWNING SECTION rather than per control. Deriving
  // the section from the key means every existing and future control becomes
  // read-only automatically when its section follows the fleet — there is no
  // per-input plumbing to forget.
  const set = (k, v) => {
    if (disabled) return;
    if (scoped && !isLocal(KEY_SECTION[k])) return;
    onChange(k, v);
  };

  // Per-stage props: the Fleet/Device switch, and the dimming for a stage
  // that is currently inheriting.
  const scopeEl = id => scoped
    ? <ScopeToggle local={isLocal(id)} disabled={disabled}
        onChange={local => onScopeChange && onScopeChange(id, local)}/>
    : null;
  const secStyle = id => (disabled || !isLocal(id))
    ? { opacity: 0.45, pointerEvents: 'none' }
    : {};

  // Derive current mic preset from beamAngle
  const angle = config.beamAngle ?? -1;
  const currentPreset = angle === -1 ? 'omni' : (angle === 90 ? 'front' : angle === 270 ? 'rear' : 'omni');

  const PRESETS = {
    // omni = centre mic (ch6) for everything, beamforming genuinely off.
    // beamformingEnabled:true with beamAngle -1 is AUTO mode (onset-ratio
    // perimeter mic selection at turn start), which is not what this
    // preset's label or polar plot promise.
    omni:  { beamAngle: -1,  beamformingEnabled: false, activeMics: ['mk1','mk2','mk3','mk4','mk5','mk6'], patternType: 'omni'  },
    front: { beamAngle: 90,  beamformingEnabled: true,  activeMics: ['mk3','mk4','mk5','mk6'],             patternType: 'front' },
    rear:  { beamAngle: 270, beamformingEnabled: true,  activeMics: ['mk1','mk2','mk3','mk6'],             patternType: 'rear'  },
  };

  function selectPreset(key) {
    if (disabled) return;
    const p = PRESETS[key];
    onChange('beamAngle', p.beamAngle);
    onChange('beamformingEnabled', p.beamformingEnabled);
  }

  const WW_MODELS = [
    { value: 'hey_jarvis_v0.1',   label: 'Hey Jarvis'   },
    { value: 'alexa_v0.1',        label: 'Alexa'         },
    { value: 'hey_mycroft_v0.1',  label: 'Hey Mycroft'   },
    { value: 'hey_rhasspy_v0.1',  label: 'Hey Rhasspy'   },
  ];

  // Custom models discovered in the controller's data volume (oww_forge
  // output). Tile value = absolute file path — openwakeword accepts paths
  // in place of stock names, so no other plumbing is needed.
  const [customModels, setCustomModels] = useState([]);
  const wwFileRef = useRef(null);
  const loadCustomModels = useCallback(async () => {
    try { setCustomModels((await API.get('/api/oww_models')).models || []); }
    catch (e) { /* endpoint requires auth; list just stays empty */ }
  }, []);
  useEffect(() => { loadCustomModels(); }, [loadCustomModels]);

  async function uploadWakeModel(file) {
    if (!file) return;
    try {
      const resp = await API.upload('/api/oww_models/upload', file, 'model');
      await loadCustomModels();
      if (resp.model?.path) set('owwModel', resp.model.path);
    } catch (e) { alert(e.error || 'Model upload failed'); }
  }

  async function deleteWakeModel(m) {
    if (!confirm(`Delete wake model "${m.name}"?`)) return;
    try {
      await API.del(`/api/oww_models/${encodeURIComponent(m.file)}`);
      await loadCustomModels();
    } catch (e) { alert(e.error || 'Delete failed'); }
  }

  // A selected custom model that no longer exists on disk (or a bare-metal
  // path from before a Docker move) still needs a visible, selected tile.
  const orphanModel = (config.owwModel || '').endsWith('.onnx')
    && !customModels.some(m => m.path === config.owwModel)
    ? { name: wwModelLabel(config.owwModel), file: config.owwModel.split('/').pop(), path: config.owwModel, missing: true }
    : null;

  // Sensitivity: map owwThreshold (0.1–0.9) to 1–9 int, inverted (low threshold = eager)
  const sensitivityToThreshold = v => Number((1.0 - (v - 1) / 8 * 0.8).toFixed(2));
  const thresholdToSensitivity = t => Math.round((1.0 - t) / 0.8 * 8) + 1;
  const sensitivity = thresholdToSensitivity(config.owwThreshold ?? 0.5);

  const bands = config.eqBands ?? [0,0,0,0,0,0,0,0];
  const RING_SCENES = [
    { value: 'standard',   label: 'Standard',   swatches: ['#00b400'] },
    { value: 'airy',       label: 'Airy',       swatches: ['#5096c8', '#96cdff'] },
    { value: 'malevolent', label: 'Malevolent', swatches: ['#6e002d', '#d22d00'] },
    { value: 'pride',      label: 'Pride',      swatches: ['#bf0000', '#bf7700', '#a9bf00', '#00bf2c', '#0055bf', '#8b00bf'] },
    { value: 'custom',     label: 'Custom',     swatches: null },
  ];
  // Measured, not chosen (2026-08-29). The driver was swept in three
  // placements against the hardware echo reference, and the result agrees with
  // stock's own FIR to 0.1dB at 315Hz and 0.0dB at 630Hz — two methods sharing
  // no assumptions (#247). Relative to 1kHz the driver is ~18dB down at 250Hz
  // and peaks ~+8.9dB at 3150Hz.
  //
  // The old presets predate that measurement and one of them was backwards:
  // 'Clarity' put +7dB on band 5 (3500Hz), which is exactly where the driver
  // already peaks, landing around +16dB at 3150 — sibilance, not clarity.
  // 'Warmth' had the right shape and a fraction of the size.
  //
  // Bands are [125 shelf, 250, 500, 1000, 2000, 3500, 5500, 8000 shelf].
  // 125 stays 0 in every preset: it is a SHELF, so lifting it pushes
  // everything below into the bass guard, which then removes it — the boost
  // belongs at 250 where the band is a peaking filter. 5500 and 8000 stay 0
  // on evidence rather than omission: those bands moved up to 13.8dB between
  // placements, which is more than the whole ±12dB range, so anything set
  // there tunes one room.
  const EQ_PRESETS = [
    // The bypass, and the reference for any A/B. Keep it exactly zero.
    ['Flat',   [0, 0, 0, 0,  0,  0, 0, 0]],
    // Gentler low-mid lift than Music: speech carries little energy below
    // 300Hz, and the boost spends headroom the limiter then reclaims from the
    // midrange. Keeps most of the driver's natural presence — 2-4kHz carries
    // consonants — while taking the harsh edge off the 3150 peak.
    ['Speech', [0, 4, 2, 0, -2, -5, 0, 0]],
    // The full measured correction, bounded by what this driver will stand.
    // Stock puts +19.9dB at 250Hz; +8 is the honest fraction our ±12 range and
    // the limiter leave room for, and it is a value to walk up by ear.
    ['Music',  [0, 8, 3, 0, -3, -6, 0, 0]],
  ];
  const activeEqPreset = (EQ_PRESETS.find(([, vals]) => JSON.stringify(vals) === JSON.stringify(bands)) || [null])[0];

  const [advMics, setAdvMics] = useState(false);
  const [advRing, setAdvRing] = useState(false);
  const [advPlay, setAdvPlay] = useState(false);

  const inputStyle = disabled ? { opacity: 0.45, pointerEvents: 'none' } : {};
  const mono = "'DM Mono',monospace";

  // Small header for subsections inside the combined Advanced stage.
  const subHeader = (text, first) => (
    <div style={{ fontFamily: mono, fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase',
      letterSpacing: '0.15em', marginTop: first ? 0 : 18, marginBottom: 10 }}>{text}</div>
  );

  // Ordered by how often each section gets touched: playback and wake word
  // are everyday knobs, mic capture is set-and-forget, and the button-turn
  // internals live in one Advanced bucket at the end. (Stages used to be
  // ordered by signal flow with ▼ connectors — dropped when the order
  // switched to relevance.)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>

      {/* 01 PLAYBACK */}
      <Stage n="01" title="Playback"
        chips={<><ScopeChip tone="controller">Controller</ScopeChip><ScopeChip tone="device">Speaker</ScopeChip></>}
        desc="Response audio: Home Assistant TTS → parametric EQ → resample → device speaker. Presets set the faders; drag any fader for a custom curve."
        scope={scopeEl('playback')} dim={secStyle('playback')}>
        <div className="em-grid2" style={{ display: 'grid', gridTemplateColumns: '1.4fr 1fr', gap: 28, alignItems: 'start' }}>
          <div>
            <EqCurve bands={bands}/>
            <EqSliders bands={bands} onChange={nb => set('eqBands', nb)} disabled={disabled}/>
            <div style={{ display: 'flex', gap: 6, marginTop: 12, alignItems: 'center', ...inputStyle }}>
              {EQ_PRESETS.map(([label, vals]) => (
                <Pill key={label} small accent={activeEqPreset === label} onClick={() => set('eqBands', vals)}>{label}</Pill>
              ))}
              {!activeEqPreset && (
                <span style={{ fontFamily: mono, fontSize: 9, color: 'var(--accent)', textTransform: 'uppercase', letterSpacing: '0.1em' }}>· Custom</span>
              )}
            </div>
          </div>
          <div>
            <div style={inputStyle}>
              <Toggle label="Speech boost" sub="presence boost for voice" value={config.eqLoudness ?? false} onChange={v => set('eqLoudness', v)}/>
            </div>
            {/* Speaker protection: ONE toggle for the bass guard, and the
                limiter is not offered at all.

                These were four controls until 2026-08-20, and none of them
                can be judged by ear. The guard and the limiter cancel each
                other's most obvious cue — guard on/off is 7.7dB of overall
                level at a flat EQ and 0.2dB with the bands boosted, because
                the limiter gives back exactly what the guard takes. The depth
                moves the overall level 0.14dB across its ENTIRE range. And
                the guard mostly removes content this driver cannot radiate,
                so what remains is a second-order cleanliness gain.

                Four controls whose individual effects range from "large" to
                "nothing" depending on where the other three sit is not a
                tuning surface; it is a way to conclude the feature is broken,
                which is what happened. The limiter in particular must stay on
                — it is what stops the EQ hard-clipping what it boosts (#231),
                and that is not a preference. Every key still exists and is
                settable through the API. */}
            <div style={inputStyle}>
              <Slider label="Duck depth" disabled={!mixCapable}
                sub={mixCapable
                  ? "how far music drops under a voice response — it keeps playing instead of pausing"
                  : "needs firmware that mixes music and voice (v2.10.0+)"}
                value={config.duckDb ?? -18} min={-40} max={0} step={1} unit="dB"
                onChange={v => set('duckDb', v)}/>
            </div>
            {/* The startup-volume slider used to live here and was removed
                (2026-07-25): volume is persisted device STATE, not a setting.
                The controller writes it from every volume_state report and
                the device only re-applies it on the first config push per
                run — so moving this slider did nothing until the device
                restarted, and any real volume change overwrote it. Current
                volume is now shown read-only on the Status tab. */}
            <div style={{ marginTop: 8, fontFamily: mono, fontSize: 10, color: 'var(--muted)', lineHeight: 1.6 }}>
              Volume is remembered per device and restored after a reboot.
              Change it from Home Assistant or the device buttons; the current
              level is shown on the Status tab.
            </div>
          </div>
        </div>
        <StageAdvanced open={advPlay} onToggle={() => setAdvPlay(o => !o)} disabledStyle={inputStyle}>
          <div style={inputStyle}>
            <Toggle label="Speaker protection"
              sub="keeps bass the driver can't deliver from muddying the midrange — leave on"
              value={config.bassGuardEnabled ?? true} onChange={v => set('bassGuardEnabled', v)}/>
          </div>
          <div style={{ marginTop: 8, fontFamily: mono, fontSize: 10, color: 'var(--muted)', lineHeight: 1.6 }}>
            The speaker cannot reproduce the lowest frequencies, and feeding
            them to it costs cone movement that muddies everything above.
            Removing them is what keeps the midrange clean. The change is
            subtle by design and there is no reason to turn it off.
          </div>
        </StageAdvanced>
      </Stage>

      {/* 02 WAKE WORD */}
      <Stage n="02" title="Wake word"
        chips={<ScopeChip tone="controller">Controller</ScopeChip>}
        desc="openwakeword scores the continuous mic stream on the controller. Sensitivity sets the detection threshold — attempts that score close but miss are counted as near-misses (Status tab)."
        scope={scopeEl('wakeword')} dim={secStyle('wakeword')}>
        <div className="em-grid2" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24, alignItems: 'start' }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, ...inputStyle }}>
            {WW_MODELS.map(m => (
              <div key={m.value} onClick={() => set('owwModel', m.value)} style={{
                background: config.owwModel === m.value
                  ? 'linear-gradient(160deg,var(--accent-tint),var(--accent-line))'
                  : 'linear-gradient(160deg,var(--raised),var(--surface))',
                border: `1px solid ${config.owwModel === m.value ? 'var(--accent)' : 'var(--border-soft)'}`,
                borderRadius: 8, padding: '8px 10px',
                cursor: disabled ? 'default' : 'pointer',
                transition: 'border-color 0.15s, background 0.15s',
              }}>
                <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 12, fontWeight: 600, color: 'var(--lcd-line)' }}>{m.label}</div>
                <div style={{ fontFamily: mono, fontSize: 9, color: 'var(--muted)', marginTop: 2 }}>{m.value}</div>
              </div>
            ))}
            {[...customModels, ...(orphanModel ? [orphanModel] : [])].map(m => (
              <div key={m.path} onClick={() => set('owwModel', m.path)} style={{
                background: config.owwModel === m.path
                  ? 'linear-gradient(160deg,var(--accent-tint),var(--accent-line))'
                  : 'linear-gradient(160deg,var(--raised),var(--surface))',
                border: `1px solid ${config.owwModel === m.path ? 'var(--accent)' : 'var(--border-soft)'}`,
                borderRadius: 8, padding: '8px 10px', position: 'relative',
                cursor: disabled ? 'default' : 'pointer',
                transition: 'border-color 0.15s, background 0.15s',
              }}>
                <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 12, fontWeight: 600, color: 'var(--lcd-line)' }}>{wwModelLabel(m.path)}</div>
                <div style={{ fontFamily: mono, fontSize: 9, color: m.missing ? 'var(--error)' : 'var(--muted)', marginTop: 2 }}>
                  {m.missing ? 'missing file' : `custom · ${m.file}`}
                </div>
                {!disabled && !m.missing && config.owwModel !== m.path && (
                  <div onClick={e => { e.stopPropagation(); deleteWakeModel(m); }}
                    title="Delete model"
                    style={{ position: 'absolute', top: 4, right: 7, fontFamily: mono, fontSize: 11,
                      color: 'var(--muted)', cursor: 'pointer' }}>×</div>
                )}
              </div>
            ))}
            <div onClick={() => { if (!disabled) wwFileRef.current?.click(); }} style={{
              background: 'linear-gradient(160deg,var(--raised),var(--surface))',
              border: '1px dashed var(--border-hard)', borderRadius: 8, padding: '8px 10px',
              cursor: disabled ? 'default' : 'pointer', opacity: 0.85,
            }}>
              <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 12, fontWeight: 600, color: 'var(--text2)' }}>+ Custom model</div>
              <div style={{ fontFamily: mono, fontSize: 9, color: 'var(--muted)', marginTop: 2 }}>upload .onnx (oww_forge)</div>
              <input ref={wwFileRef} type="file" accept=".onnx" style={{ display: 'none' }}
                onChange={e => { uploadWakeModel(e.target.files[0]); e.target.value = ''; }}/>
            </div>
          </div>
          <div>
            <div style={inputStyle}>
              <div style={{ fontFamily: mono, fontSize: 11, color: 'var(--text2)', marginBottom: 6 }}>Sensitivity</div>
              <input type="range" min={1} max={9} step={1} value={sensitivity}
                style={{ width: '100%' }}
                onChange={e => set('owwThreshold', sensitivityToThreshold(Number(e.target.value)))}/>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 4 }}>
                <span style={{ fontFamily: mono, fontSize: 9, color: 'var(--muted)' }}>Precise</span>
                <span style={{ fontFamily: mono, fontSize: 9, color: 'var(--muted)' }}>Eager</span>
              </div>
            </div>
            <div style={{ marginTop: 16, ...inputStyle }}>
              <Toggle label="Speex denoise" sub="cleans audio before scoring — try in noisy rooms" value={config.owwSpeexNs ?? false} onChange={v => set('owwSpeexNs', v)}/>
              <Toggle label="Barge-in" sub="wake word interrupts playback — enable AEC first" value={config.bargeInEnabled ?? false} onChange={v => set('bargeInEnabled', v)}/>
              <Slider label="Barge threshold" sub="wake confidence needed during playback — raise it if a response cuts itself short" value={config.bargeInThreshold ?? 0.25} min={0.05} max={0.9} step={0.05} onChange={v => set('bargeInThreshold', v)}/>
              <Slider label="Arbitration window" sub="ms that the first Echo to hear you silences the others — no added delay; 0 disables" value={config.wakeArbitrationMs ?? 700} min={0} max={2000} step={50} unit="ms" onChange={v => set('wakeArbitrationMs', v)}/>
              {/* Three modes, so a select rather than a toggle. Each option is
                  offered only when the device says it can do it — capability,
                  not firmware version, because a control that silently does
                  nothing reads as a broken feature rather than an unsupported
                  one. "Trigger" needs oww_trigger on top of oww_shadow: shadow
                  shipped first and there is firmware in the field that scores
                  and reports without being able to act on it. */}
              <Select
                label="Wake word detection"
                sub={!shadowCapable
                  ? 'needs newer firmware on this Echo — the controller listens for now'
                  : (config.owwOnDevice ?? 'off') === 'on'
                    ? 'the Echo decides — no network hop before it hears you, and it keeps working through a controller restart. The controller still scores alongside it, so Activity shows whether they agreed'
                    : (config.owwOnDevice ?? 'off') === 'shadow'
                      ? 'the Echo scores alongside the controller and reports what it would have heard, without acting on it — compare in Activity before trusting it'
                      : 'the controller listens; the Echo just streams audio'}
                value={onDeviceMode(config)}
                options={[
                  { value: 'off',    label: 'Controller' },
                  { value: 'shadow', label: 'Both (compare)', disabled: !shadowCapable },
                  // Needs the runtime + models installed as well as the
                  // capability, which the Updates tab does — hence the hint
                  // rather than a hard block we cannot verify from here.
                  { value: 'on',     label: 'On device',      disabled: !triggerCapable },
                ]}
                onChange={v => set('owwOnDevice', v)}/>
              {(config.owwOnDevice ?? 'off') !== 'off' && shadowCapable && (
                <div className="em-label" style={{ marginTop: 6, color: 'var(--muted)' }}>
                  Needs the wake word runtime installed on this Echo (Updates tab) — costs ~0.4 of a core while it runs.
                </div>
              )}
            </div>
          </div>
        </div>
      </Stage>

      {/* 03 MICROPHONES */}
      <Stage n="03" title="Microphones"
        chips={<ScopeChip tone="device">Device</ScopeChip>}
        desc="Capture from the 7-mic array. Presets steer which perimeter mic is used during voice turns — wake-word listening always uses the centre mic. Gain here is the only gain in the wake path: it sets the level everything downstream hears."
        scope={scopeEl('microphones')} dim={secStyle('microphones')}>
        <div className="em-grid2" style={{ display: 'grid', gridTemplateColumns: 'auto 1fr', gap: 20, alignItems: 'center' }}>
          <DeviceDiagram
            activeMics={PRESETS[currentPreset].activeMics}
            patternType={PRESETS[currentPreset].patternType}
          />
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 6, ...inputStyle }}>
            {Object.entries(PRESETS).map(([key, p]) => (
              <div key={key} onClick={() => selectPreset(key)} style={{
                background: currentPreset === key
                  ? 'linear-gradient(160deg,var(--accent-tint),var(--accent-line))'
                  : 'linear-gradient(160deg,var(--raised),var(--surface))',
                border: `1px solid ${currentPreset === key ? 'var(--accent)' : 'var(--border-soft)'}`,
                borderRadius: 10, padding: '9px 6px 8px',
                cursor: disabled ? 'default' : 'pointer',
                display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6,
                transition: 'border-color 0.15s, background 0.15s',
              }}>
                <DeviceDiagramMini activeMics={p.activeMics} patternType={p.patternType}/>
                <div style={{ fontFamily: mono, fontSize: 10, color: 'var(--text2)' }}>
                  {key.charAt(0).toUpperCase() + key.slice(1)}
                </div>
              </div>
            ))}
          </div>
        </div>
        <StageAdvanced open={advMics} onToggle={() => setAdvMics(o => !o)} disabledStyle={inputStyle}>
          <div className="em-grid2" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '4px 24px' }}>
            <Slider label="MICPGA" sub="analog gain, before the ADC" value={config.adcMicpga ?? 40} min={0} max={59} onChange={v => set('adcMicpga', v)}/>
            <Slider label="Digital gain" sub="ADC digital gain — affects wake + turns" value={config.adcDigitalGain ?? 88} min={0} max={100} onChange={v => set('adcDigitalGain', v)}/>
            <Slider label="Mic gain" sub="fixed gain on the 24-bit capture, pre-16-bit stream" value={config.micGainDb ?? 24} min={0} max={42} unit="dB" onChange={v => set('micGainDb', v)}/>
            <Slider label="Beam angle" sub="-1 = auto (onset-ratio selection)" value={config.beamAngle ?? -1} min={-1} max={359} step={1} onChange={v => set('beamAngle', v)}/>
            <Toggle label="Beamforming" sub="perimeter mic lock during turns" value={config.beamformingEnabled ?? false} onChange={v => set('beamformingEnabled', v)}/>
            <Toggle label="Echo cancel (AEC)" sub="subtracts the device's own playback — wake + turns" value={config.aecEnabled ?? false} onChange={v => set('aecEnabled', v)}/>
            <Toggle label="Noise suppression" sub="DTLN denoise on speech-to-text audio only — helps fans/hum, not TV speech" value={config.nsAsr ?? false} onChange={v => set('nsAsr', v)}/>
            <Slider label="AEC delay"
              sub={hwEchoRef
                ? 'not used — this device has a hardware echo reference'
                : 'playback write-to-ear latency compensation'}
              disabled={hwEchoRef}
              value={config.aecDelayMs ?? 250} min={0} max={1000} step={10} unit="ms" onChange={v => set('aecDelayMs', v)}/>
            <Slider label="AEC tail" sub="filter length — residual delay error + room reverb" value={config.aecTailMs ?? 300} min={50} max={500} step={10} unit="ms" onChange={v => set('aecTailMs', v)}/>
            {/* Three values, so a select. "Auto" is right almost always —
                these exist so the two reference paths can be compared on one
                device without editing an init script on it and restarting
                the server, which is how that measurement stayed undone. */}
            <Select
              label="Echo reference"
              sub={!hwRefCapable
                ? 'needs newer firmware on this Echo — the software tap is the only source it has'
                : (config.aecRefSource ?? 'auto') === 'hw'
                  ? 'pinned to the playback loopback in the mic capture — no delay to compensate, but a board without one cancels nothing'
                  : (config.aecRefSource ?? 'auto') === 'sw'
                    ? 'pinned to the tap at the speaker write — uses the AEC delay above, and re-converges after every volume change'
                    : hwEchoRef
                      ? 'detected: using the hardware loopback on this Echo'
                      : 'detects the hardware loopback, falls back to the software tap'}
              value={String(config.aecRefSource ?? 'auto').toLowerCase()}
              options={[
                { value: 'auto', label: 'Auto' },
                { value: 'hw',   label: 'Hardware', disabled: !hwRefCapable },
                { value: 'sw',   label: 'Software tap' },
              ]}
              onChange={v => set('aecRefSource', v)}/>
            <Toggle label="Save utterances" sub="keeps the last 10 turns' mic audio on the server — play or download from Activity" value={config.saveUtterances ?? false} onChange={v => set('saveUtterances', v)}/>
          </div>
        </StageAdvanced>
      </Stage>

      {/* 04 RING */}
      <Stage n="04" title="Ring"
        chips={<ScopeChip tone="controller">Controller</ScopeChip>}
        desc="Colours for the LED ring during conversations — the solid listening ring and the thinking spinner. The red mute ring and cyan volume arc never change; red always means the mics are off."
        scope={scopeEl('ring')} dim={secStyle('ring')}>
        <div className="em-grid2" style={{ display: 'grid', gridTemplateColumns: '1.4fr 1fr', gap: 24, alignItems: 'start' }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, ...inputStyle }}>
            {RING_SCENES.map(sc => (
              <div key={sc.value} onClick={() => set('ledScene', sc.value)} style={{
                background: (config.ledScene ?? 'standard') === sc.value
                  ? 'linear-gradient(160deg,var(--accent-tint),var(--accent-line))'
                  : 'linear-gradient(160deg,var(--raised),var(--surface))',
                border: `1px solid ${(config.ledScene ?? 'standard') === sc.value ? 'var(--accent)' : 'var(--border-soft)'}`,
                borderRadius: 8, padding: '8px 10px',
                cursor: disabled ? 'default' : 'pointer',
                transition: 'border-color 0.15s, background 0.15s',
              }}>
                <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 12, fontWeight: 600, color: 'var(--lcd-line)' }}>{sc.label}</div>
                <div style={{ display: 'flex', gap: 3, marginTop: 4 }}>
                  {(sc.value === 'custom'
                    ? [config.ledListenColor ?? '#00b400', config.ledThinkColor ?? '#00c800']
                    : sc.swatches
                  ).map((c, i) => (
                    <span key={i} style={{ width: 10, height: 10, borderRadius: '50%', background: c, border: '1px solid rgba(0,0,0,0.15)' }}/>
                  ))}
                </div>
              </div>
            ))}
          </div>
          {(config.ledScene ?? 'standard') === 'custom' && (
            <div style={inputStyle}>
              <div style={{ fontFamily: mono, fontSize: 11, color: 'var(--text2)', marginBottom: 8 }}>Custom colours</div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
                <input type="color" value={config.ledListenColor ?? '#00b400'} disabled={disabled}
                  onChange={e => set('ledListenColor', e.target.value)}
                  style={{ width: 36, height: 28, padding: 0, border: '1px solid var(--border)', borderRadius: 6, background: 'none', cursor: 'pointer' }}/>
                <div>
                  <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 12, fontWeight: 600 }}>Listening</div>
                  <div style={{ fontFamily: mono, fontSize: 9, color: 'var(--muted)' }}>solid ring while recording</div>
                </div>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <input type="color" value={config.ledThinkColor ?? '#00c800'} disabled={disabled}
                  onChange={e => set('ledThinkColor', e.target.value)}
                  style={{ width: 36, height: 28, padding: 0, border: '1px solid var(--border)', borderRadius: 6, background: 'none', cursor: 'pointer' }}/>
                <div>
                  <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 12, fontWeight: 600 }}>Thinking</div>
                  <div style={{ fontFamily: mono, fontSize: 9, color: 'var(--muted)' }}>spinner while processing</div>
                </div>
              </div>
            </div>
          )}
        </div>
        <StageAdvanced open={advRing} onToggle={() => setAdvRing(o => !o)} disabledStyle={inputStyle}>
          <div style={{ fontFamily: mono, fontSize: 10, color: 'var(--text2)', lineHeight: 1.6, marginBottom: 12 }}>
            While a response plays, the ring throbs with the live speaker level. These shape how
            hard it throbs — the device renders it locally, so changes apply on the next response
            with no restart. Defaults are tuned for speech; raise Decay and Gamma for a punchier
            ring, lower them for a calmer one.
          </div>
          <div className="em-grid2" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '4px 24px' }}>
            <Slider label="Decay" sub="how fast it falls — higher tracks individual syllables"
              value={config.meterDecay ?? 0.30} min={0.02} max={1} step={0.02}
              onChange={v => set('meterDecay', v)}/>
            <Slider label="Attack" sub="how fast it rises on a peak"
              value={config.meterAttack ?? 0.6} min={0.05} max={1} step={0.05}
              onChange={v => set('meterAttack', v)}/>
            <Slider label="Gamma" sub="contrast — higher makes the swing more visible"
              value={config.meterGamma ?? 2.2} min={1} max={3.5} step={0.1}
              onChange={v => set('meterGamma', v)}/>
            <Slider label="Floor" sub="brightness during silence; 0 = fully dark between words"
              value={config.meterFloor ?? 0.06} min={0} max={0.6} step={0.02}
              onChange={v => set('meterFloor', v)}/>
            <Slider label="Reference" sub="speaker level mapped to full brightness — lower = more sensitive"
              value={config.meterRef ?? 0.22} min={0.02} max={1} step={0.02}
              onChange={v => set('meterRef', v)}/>
            <Slider label="Curve" sub="below 1 lifts quiet consonants into view"
              value={config.meterCurve ?? 0.7} min={0.3} max={2} step={0.05}
              onChange={v => set('meterCurve', v)}/>
          </div>
        </StageAdvanced>
      </Stage>

      {/* 05 ADVANCED — button-turn internals: processing + speech gate */}
      <Stage n="05" title="Advanced"
        chips={<><ScopeChip tone="device">Device</ScopeChip><ScopeChip>Button turns only</ScopeChip></>}
        desc="Everything here affects only bounded button-press turns — except the action button setting, which decides whether a tap starts one at all. Wake-word turns stream continuously — Home Assistant's VAD endpoints them, and the controller closes accidental wakes after 5s of silence relative to the room's measured noise floor — so none of these settings touch the wake path."
        scope={scopeEl('advanced')} dim={secStyle('advanced')}>
        {subHeader('Action button', true)}
        <div className="em-grid2" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 24px', ...inputStyle }}>
          {/* Offered only when the device says it can measure a hold. The
              value still reads through holdCapable so an incapable device
              shows the switch off rather than showing a stored true it
              cannot honour. */}
          <Toggle label="Tap sends an event" disabled={!holdCapable}
            sub={holdCapable
              ? "tap fires the HA action-button event instead of starting a turn — hold still fires 'long'; the button can no longer cancel a response. A tap is easy to trigger by accident and the button is unauthenticated — bind destructive automations to 'long' instead"
              : 'needs newer firmware on this Echo — it has no action-button event for a tap to fire'}
            value={holdCapable && (config.buttonSingleTapEvent ?? false)}
            onChange={v => set('buttonSingleTapEvent', v)}/>
          <Slider label="Multi-tap window" sub="0 = off. Coalesces quick taps into double/triple, at the cost of delaying every tap by this much. Needs 'Tap sends an event'" value={config.buttonMultiTapMs ?? 0} min={0} max={600} step={50} unit="ms" disabled={!(holdCapable && (config.buttonSingleTapEvent ?? false))} onChange={v => set('buttonMultiTapMs', v)}/>
        </div>
        {subHeader('Turn processing')}
        <div className="em-grid2" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 24px', ...inputStyle }}>
          <Toggle label="Auto gain (AGC)" sub="levels button-turn speech; never the wake stream" value={config.agcEnabled ?? true} onChange={v => set('agcEnabled', v)}/>
        </div>
        {subHeader('Speech gate')}
        <div className="em-grid2" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '4px 20px', ...inputStyle }}>
          <Slider label="Threshold" sub="RMS above this = speech (pre-gain units)" value={config.vadThreshold ?? 0.001} min={0.0001} max={0.02} step={0.0001} onChange={v => set('vadThreshold', v)}/>
          <Slider label="Speech gate" sub="speech needed to open" value={config.vadSpeechMs ?? 160} min={32} max={320} step={32} unit="ms" onChange={v => set('vadSpeechMs', v)}/>
          <Slider label="Silence gate" sub="silence needed to close" value={config.vadSilenceMs ?? 800} min={200} max={2000} step={100} unit="ms" onChange={v => set('vadSilenceMs', v)}/>
        </div>
      </Stage>

      {/* 06 BLUETOOTH */}
      <Stage n="06" title="Bluetooth"
        chips={<><ScopeChip tone="device">Device</ScopeChip><ScopeChip tone="controller">Controller</ScopeChip></>}
        desc="Turns the device into a Home Assistant Bluetooth proxy: it passively listens for BLE advertisements (presence beacons, temperature sensors) and forwards them to HA as a separate ESPHome device — independent of the voice assistant. Enabling permanently switches the Dot's Bluetooth chip away from Android's stack (Bluetooth speaker pairing, never used by EchoMuse, stops being possible)."
        scope={scopeEl('bluetooth')} dim={secStyle('bluetooth')}>
        <div className="em-grid2" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 24px', ...inputStyle }}>
          <Toggle label="Bluetooth proxy" sub="passive BLE scan → HA (Bermuda, BLE sensors)" value={config.bleProxyEnabled ?? false} onChange={v => set('bleProxyEnabled', v)}/>
        </div>
      </Stage>
    </div>
  );
}


// ─── Deploy-all modal ─────────────────────────────────────────────────────────
// Fleet-wide OTA from the main dashboard. Uses POST /api/releases/deploy
// (deploys the latest GitHub release to every connected, approved,
// non-current device). Progress is read live from the `devices` prop —
// the parent's WebSocket keeps it fresh, so each row updates as devices
// drop for reboot and reconnect on the new version.

function DeployAllModal({ release, devices, deployState, onStarted, onDismiss, onClose }) {
  const mono = "'DM Mono',monospace";
  const [running, setRunning] = useState(false);
  const [error, setError]     = useState('');
  // Guard against setState after the modal is closed mid-request — the deploy
  // POST returns fast (server backgrounds the work), but closing during that
  // window otherwise logs a React unmounted-update error.
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);

  // The view is driven by the persisted deployState (survives close/reopen),
  // not local state. Present → show progress; absent → show the confirm screen.
  const view = deployState;
  const target = view?.version || release?.version;
  const byId = Object.fromEntries(devices.map(d => [d.device_id, d]));
  const eligible = devices.filter(d =>
    d.approved && d.connected && d.firmware_ver !== release?.version);

  const SKIP_REASONS = {
    not_approved:       'not approved',
    already_current:    'already up to date',
    update_in_progress: 'update already running',
  };

  function statusFor(id) {
    const d = byId[id];
    if (!d)                              return { text: 'unknown',      color: 'var(--muted)' };
    if (d.connected && d.firmware_ver === target)
                                         return { text: '✓ updated',    color: 'var(--ok)' };
    // A recorded failure is terminal — without this the row (and the header
    // progress pill) sat at "updating…" forever after an aborted update.
    if (d.update_error)                  return { text: `✗ ${d.update_error}`, color: 'var(--error)' };
    // Queued outranks "rebooting…": a device waiting its turn has had nothing
    // sent to it, and a disconnected one in the queue is offline for its own
    // reasons, not because we restarted it.
    if (d.update_queued)                 return { text: 'queued',       color: 'var(--muted)' };
    if (!d.connected)                    return { text: 'rebooting…',   color: 'var(--warn)' };
    return { text: 'updating…', color: 'var(--accent)' };
  }

  const started = view?.started || [];
  // Failed counts as done — the deploy reached a terminal state for that
  // device, it just wasn't success.
  const terminal = id => {
    const d = byId[id];
    return d && ((d.connected && d.firmware_ver === target) || d.update_error);
  };
  const failedCount = started.filter(id => byId[id]?.update_error &&
    !(byId[id].connected && byId[id].firmware_ver === target)).length;
  const allDone = started.length > 0 && started.every(terminal);

  async function deploy() {
    setRunning(true); setError('');
    try {
      const res = await API.post('/api/releases/deploy', {});
      onStarted(res); // lift to App so it persists across close/reopen
    } catch (e) {
      if (mounted.current) setError(e.error || 'Deploy failed');
    }
    if (mounted.current) setRunning(false);
  }

  const label = d => d?.label || d?.device_id || '?';

  return (
    <div onClick={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(30,28,24,0.45)', zIndex: 60, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div onClick={e => e.stopPropagation()} style={{ background: 'linear-gradient(170deg,var(--raised),var(--surface))', border: '1px solid var(--border)', borderRadius: 14, padding: '28px 32px', width: 440, maxWidth: '92vw', boxShadow: '0 24px 80px rgba(0,0,0,0.3)' }}>
        <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 16, fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>
          Deploy to fleet
        </div>
        <div style={{ fontFamily: mono, fontSize: 10, color: 'var(--muted)', marginBottom: 18 }}>
          Target: {release?.version || '—'} · devices update over WiFi and auto-roll-back on failure
        </div>

        {!view ? (
          <>
            <div style={{ fontFamily: mono, fontSize: 11, color: 'var(--text2)', marginBottom: 16, lineHeight: 1.8 }}>
              {eligible.length === 0
                ? 'Every connected device is already on this version.'
                : <>Will update <b>{eligible.length}</b> device{eligible.length === 1 ? '' : 's'}:{' '}
                    {eligible.map(d => `${label(d)} (${d.firmware_ver || '?'})`).join(', ')}</>}
            </div>
            {error && <div style={{ fontFamily: mono, fontSize: 11, color: 'var(--error)', marginBottom: 12 }}>{error}</div>}
            <div style={{ display: 'flex', gap: 10 }}>
              <Pill accent disabled={running || eligible.length === 0} onClick={deploy}>
                {running ? 'Starting…' : `Deploy ${release?.version || ''}`}
              </Pill>
              <Pill onClick={onClose}>Cancel</Pill>
            </div>
          </>
        ) : (
          <>
            {(view.started || []).map(id => {
              const s = statusFor(id);
              return (
                <div key={id} style={{ display: 'flex', justifyContent: 'space-between', fontFamily: mono, fontSize: 11, padding: '5px 0', borderBottom: '1px solid var(--hairline)' }}>
                  <span style={{ color: 'var(--text2)' }}>{label(byId[id])}</span>
                  <span style={{ color: s.color }}>{s.text} {byId[id]?.firmware_ver ? `· ${byId[id].firmware_ver}` : ''}</span>
                </div>
              );
            })}
            {(view.skipped || []).map(s => (
              <div key={s.device_id} style={{ display: 'flex', justifyContent: 'space-between', fontFamily: mono, fontSize: 11, padding: '5px 0', borderBottom: '1px solid var(--hairline)' }}>
                <span style={{ color: 'var(--muted)' }}>{label(byId[s.device_id])}</span>
                <span style={{ color: 'var(--muted)' }}>skipped — {SKIP_REASONS[s.reason] || s.reason}</span>
              </div>
            ))}
            {(view.started || []).length === 0 && (view.skipped || []).length === 0 && (
              <div style={{ fontFamily: mono, fontSize: 11, color: 'var(--muted)' }}>Nothing to do.</div>
            )}
            <div style={{ fontFamily: mono, fontSize: 10, color: 'var(--muted)', marginTop: 14 }}>
              {allDone
                ? (failedCount > 0
                    ? `Finished — ${failedCount} device${failedCount === 1 ? '' : 's'} failed (see device logs).`
                    : 'All devices updated.')
                : 'Updates run in the background — you can close this and reopen it from the header to check progress.'}
            </div>
            <div style={{ marginTop: 14, display: 'flex', gap: 10 }}>
              {allDone
                ? <Pill accent onClick={() => { onDismiss(); onClose(); }}>Done</Pill>
                : <Pill onClick={onClose}>Close (keeps running)</Pill>}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ─── SettingsPanel ─────────────────────────────────────────────────────────────
// Gear icon → modal with two tabs: Fleet Config and Account.

function SettingsPanel({ globalConfig, onGlobalConfigChange, onClose, username, isAdmin }) {
  const [tab, setTab]             = useState('fleet');
  const [config, setConfig]       = useState({ ...globalConfig });
  const [dirty, setDirty]         = useState(false);
  const [saving, setSaving]       = useState(false);

  const [curPw, setCurPw]         = useState('');
  const [newPw, setNewPw]         = useState('');
  const [confirmPw, setConfirmPw] = useState('');
  const [pwSaving, setPwSaving]   = useState(false);
  const [pwMsg, setPwMsg]         = useState(null); // {ok, text}

  const [bundling, setBundling]   = useState(false);
  const [bundle, setBundle]       = useState(null);  // {url, name, bytes}
  const [bundleErr, setBundleErr] = useState(null);

  // #197: accounts and roles. The endpoints exist and are admin-only;
  // what was missing was any screen calling them, which left "the wrong
  // person opened the sidebar first" without an in-household recovery.
  const [users, setUsers]         = useState(null);
  const [usersMsg, setUsersMsg]   = useState(null);

  function loadUsers() {
    API.get('/api/users').then(setUsers)
       .catch(e => setUsersMsg({ ok: false, text: e.error || 'Failed to load users' }));
  }
  useEffect(() => { if (tab === 'users') loadUsers(); }, [tab]);

  async function setUserRole(id, role) {
    setUsersMsg(null);
    try {
      await API.patch(`/api/users/${id}`, { role });
      setUsers(prev => prev.map(u => u.id === id ? { ...u, role } : u));
      setUsersMsg({ ok: true, text: 'Role updated.' });
    } catch (e) {
      // The server's only refusal here is the last-admin demotion
      // (ha_linked is display-only — it never causes a refusal); pass its
      // reason through rather than a generic failure.
      setUsersMsg({ ok: false, text: e.error || 'Refused.' });
    }
  }

  // Object URLs pin their blob in memory until revoked; the panel closing is
  // the last moment we can still reach this one.
  useEffect(() => () => { if (bundle) URL.revokeObjectURL(bundle.url); }, [bundle]);

  async function collectBundle() {
    setBundling(true); setBundleErr(null);
    if (bundle) URL.revokeObjectURL(bundle.url);
    setBundle(null);
    try {
      // API.blob, not an <a href>: sessions are Bearer-header-only, so a
      // browser-initiated request would 401 (see the note on API.blob).
      const b = await API.blob('/api/support/bundle');
      const now = new Date();
      const p = n => String(n).padStart(2, '0');
      const stamp = `${now.getFullYear()}${p(now.getMonth()+1)}${p(now.getDate())}`
                  + `-${p(now.getHours())}${p(now.getMinutes())}${p(now.getSeconds())}`;
      setBundle({ url: URL.createObjectURL(b), name: `echomuse-support-${stamp}.json`, bytes: b.size });
    } catch(e) {
      setBundleErr(e.error || 'Failed to collect bundle');
    }
    setBundling(false);
  }

  function setConf(k, v) { setConfig(c => ({ ...c, [k]: v })); setDirty(true); setSaveMsg(null); }

  // Inline, non-blocking save feedback — was a browser alert(), which
  // demanded a click to dismiss for what is a routine success message.
  const [saveMsg, setSaveMsg] = useState(null); // {ok, text}

  async function saveGlobalConfig() {
    setSaving(true);
    try {
      const res = await API.post('/api/global/config', config);
      onGlobalConfigChange(config);
      setDirty(false);
      const n = res.pushed_to?.length ?? 0;
      setSaveMsg({ ok: true, text: n > 0
        ? `Saved — pushed live to ${n} device${n === 1 ? '' : 's'} on fleet config`
        : 'Saved' });
    } catch(e) {
      setSaveMsg({ ok: false, text: e.error || 'Failed to save global config' });
    }
    setSaving(false);
  }

  async function changePassword() {
    setPwMsg(null);
    if (newPw !== confirmPw) { setPwMsg({ ok: false, text: 'New passwords do not match' }); return; }
    if (newPw.length < 8)    { setPwMsg({ ok: false, text: 'Password must be at least 8 characters' }); return; }
    setPwSaving(true);
    try {
      await API.post('/api/auth/change-password', { current_password: curPw, new_password: newPw });
      setPwMsg({ ok: true, text: 'Password updated' });
      setCurPw(''); setNewPw(''); setConfirmPw('');
    } catch(e) {
      setPwMsg({ ok: false, text: e.error || 'Failed to change password' });
    }
    setPwSaving(false);
  }

  // Support is admin-only because the endpoint is: the bundle spans the whole
  // fleet, so a tab a non-admin can only be refused by is worse than no tab.
  const TABS = isAdmin ? ['fleet', 'users', 'account', 'support'] : ['fleet', 'account'];
  const TAB_LABELS = { fleet: 'Config', users: 'Users', account: 'Account', support: 'Support' };

  return (
    <div style={{ position:'fixed', inset:0, background:'rgba(180,176,168,0.5)', display:'flex', alignItems:'center', justifyContent:'center', zIndex:200, backdropFilter:'blur(8px)' }}
      onClick={e => e.target === e.currentTarget && onClose()}>
      {/* Same fixed frame as the device Detail modal — consistent window
          size across the whole dashboard. */}
      <div className="em-modal" style={{ width:'min(900px,95vw)', height:'min(700px,90vh)', background:'linear-gradient(170deg,var(--raised),var(--surface))', border:'1px solid var(--border)', borderRadius:16, boxShadow:'0 24px 80px rgba(0,0,0,0.3),0 2px 0 var(--sheen) inset', display:'flex', flexDirection:'column', overflow:'hidden', animation:'fadeIn 0.15s ease' }}>

        {/* Header */}
        <div className="em-modal-head" style={{ background:'linear-gradient(180deg,var(--card),var(--bg))', borderBottom:'1px solid var(--border-hard)', padding:'20px 24px 0', boxShadow:'0 1px 0 var(--sheen) inset' }}>
          <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom:16 }}>
            <div style={{ fontFamily:"'DM Sans',sans-serif", fontSize:22, color:'var(--text)', fontWeight:600, letterSpacing:'-0.02em' }}>Settings</div>
            <CircleButton onClick={onClose} title="Close">×</CircleButton>
          </div>
          {/* Same raised folder-tab treatment as the device Detail modal —
              one tab style across the dashboard. */}
          <div className="em-tabs" style={{ display:'flex', gap:2 }}>
            {TABS.map(t => (
              <button key={t} onClick={() => setTab(t)} style={{ background: tab === t ? 'linear-gradient(180deg,var(--raised),var(--surface))' : 'transparent', border: tab === t ? '1px solid var(--border-hard)' : '1px solid transparent', borderBottom: tab === t ? '1px solid var(--surface)' : '1px solid transparent', borderRadius: '6px 6px 0 0', fontFamily: "'DM Mono',monospace", fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.1em', padding: '7px 14px', cursor: 'pointer', color: tab === t ? 'var(--text)' : 'var(--muted)', marginBottom: -1, transition: 'color 0.15s' }}>{TAB_LABELS[t]}</button>
            ))}
          </div>
        </div>

        {/* Body */}
        <div className="em-modal-body" style={{ overflowY:'auto', padding:'24px 28px 32px', flex:1 }}>

          {tab === 'fleet' && (
            <>
              <div style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--muted)', marginBottom:20, lineHeight:1.6 }}>
                Default config applied to all devices unless overridden per-device.
              </div>
              <DeviceConfigForm config={config} onChange={setConf} disabled={false}/>
              {dirty && (
                <div style={{ display:'flex', gap:10, marginTop:24 }}>
                  <Pill accent disabled={saving} onClick={saveGlobalConfig}>{saving ? 'Saving…' : 'Save & push to fleet'}</Pill>
                  <Pill onClick={() => { setConfig({...globalConfig}); setDirty(false); setSaveMsg(null); }}>Revert</Pill>
                </div>
              )}
              {saveMsg && (
                <div style={{ marginTop: 14, fontFamily: "'DM Mono',monospace", fontSize: 11,
                  color: saveMsg.ok ? 'var(--ok)' : 'var(--error)' }}>
                  {saveMsg.ok ? '✓ ' : ''}{saveMsg.text}
                </div>
              )}
            </>
          )}

          {tab === 'users' && (
            <div style={{ maxWidth: 640 }}>
              <div style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--muted)', marginBottom:20, lineHeight:1.6 }}>
                Admins approve devices, change config and can open a root shell to any device.
                Read-only accounts see status only. An account with an HA badge gets its sign-in
                from Home Assistant; its ROLE is still set here and never overwritten by a login.
                The last admin cannot be demoted.
              </div>
              {(users || []).map(u => (
                <div key={u.id} style={{ display:'flex', alignItems:'center', gap:12,
                     padding:'10px 0', borderBottom:'1px solid var(--border)' }}>
                  <div style={{ flex:1 }}>
                    <div style={{ fontFamily:"'DM Sans',sans-serif", fontSize:14 }}>{u.username}</div>
                    <div style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--muted)' }}>
                      {u.role}{u.ha_linked ? ' · HA-linked' : ''}
                    </div>
                  </div>
                  {u.role === 'admin'
                    ? <Pill onClick={() => setUserRole(u.id, 'readonly')}>Demote to read-only</Pill>
                    : <Pill accent onClick={() => setUserRole(u.id, 'admin')}>Promote to admin</Pill>}
                </div>
              ))}
              {!users && !usersMsg && <div className="help">Loading…</div>}
              {usersMsg && (
                <div style={{ marginTop: 14, fontFamily: "'DM Mono',monospace", fontSize: 11,
                  color: usersMsg.ok ? 'var(--ok)' : 'var(--error)' }}>
                  {usersMsg.ok ? '✓ ' : ''}{usersMsg.text}
                </div>
              )}
            </div>
          )}

          {tab === 'account' && (
            <div style={{ maxWidth: 360 }}>
              <div style={{ fontFamily:"'DM Mono',monospace", fontSize:9, color:'var(--muted)', textTransform:'uppercase', letterSpacing:'0.15em', marginBottom:20 }}>Change Password · {username}</div>
              {[
                ['Current password', curPw, setCurPw],
                ['New password',     newPw, setNewPw],
                ['Confirm new',      confirmPw, setConfirmPw],
              ].map(([label, val, setter]) => (
                <div key={label} style={{ marginBottom:16 }}>
                  <div style={{ fontFamily:"'DM Mono',monospace", fontSize:11, color:'var(--text2)', marginBottom:6 }}>{label}</div>
                  <input type="password" value={val} onChange={e => setter(e.target.value)}
                    style={{ width:'100%', boxSizing:'border-box' }}/>
                </div>
              ))}
              {pwMsg && (
                <div style={{ fontFamily:"'DM Mono',monospace", fontSize:11, color: pwMsg.ok ? 'var(--ok)' : 'var(--error)', marginBottom:12 }}>
                  {pwMsg.text}
                </div>
              )}
              <Pill accent disabled={pwSaving || !curPw || !newPw || !confirmPw} onClick={changePassword}>
                {pwSaving ? 'Updating…' : 'Update password'}
              </Pill>
            </div>
          )}

          {tab === 'support' && (
            <div style={{ maxWidth: 520 }}>
              <div style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--muted)', marginBottom:20, lineHeight:1.6 }}>
                A single file describing the fleet's state, to attach to a GitHub issue.
              </div>

              <div className="em-panel" style={{ padding:'16px 18px', marginBottom:18 }}>
                <div className="em-label" style={{ marginBottom:10 }}>What it contains</div>
                <div style={{ fontFamily:"'DM Mono',monospace", fontSize:11, color:'var(--text2)', lineHeight:1.7 }}>
                  Controller and firmware versions, device capabilities, config,
                  and the last 24 hours of turns, metrics and logs.
                </div>
                <div className="em-label" style={{ margin:'16px 0 10px' }}>What it never contains</div>
                <div style={{ fontFamily:"'DM Mono',monospace", fontSize:11, color:'var(--text2)', lineHeight:1.7 }}>
                  Transcripts or recordings, device names, Wi-Fi networks,
                  addresses, tokens or passwords. Fields are allowlisted, so
                  anything new is left out until it is added deliberately.
                </div>
              </div>

              <div style={{ display:'flex', gap:10, alignItems:'center' }}>
                <Pill accent disabled={bundling} onClick={collectBundle}>
                  {bundling ? 'Collecting…' : bundle ? 'Collect again' : 'Collect bundle'}
                </Pill>
                {bundle && (
                  <a href={bundle.url} download={bundle.name} className="em-pill em-pill--accent"
                     style={{ textDecoration:'none' }}>
                    Download {(bundle.bytes / 1024).toFixed(0)} KB
                  </a>
                )}
              </div>

              {bundleErr && (
                <div style={{ marginTop:14, fontFamily:"'DM Mono',monospace", fontSize:11, color:'var(--error)' }}>
                  {bundleErr}
                </div>
              )}
              {bundle && (
                <div style={{ marginTop:14, fontFamily:"'DM Mono',monospace", fontSize:11, color:'var(--muted)', lineHeight:1.6 }}>
                  Worth opening before you post it — it is plain JSON.
                </div>
              )}
            </div>
          )}

        </div>
      </div>
    </div>
  );
}


function App() {
  const [token, setToken] = useState(() => localStorage.getItem('em_token'));
  const [role, setRole] = useState(() => localStorage.getItem('em_role'));
  // How this session was obtained — 'ingress' when Home Assistant
  // authenticated us, otherwise a password. Set by the landing page.
  const authVia = localStorage.getItem('em_auth_via');
  const [devices, setDevices] = useState([]);
  const [selected, setSelected] = useState(null);
  const [release, setRelease] = useState(null);
  const [ctrlRelease, setCtrlRelease] = useState(null);
  const [ctrlNotesOpen, setCtrlNotesOpen] = useState(false);
  const [checkingRelease, setCheckingRelease] = useState(false);
  const [status, setStatus] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [showWizard, setShowWizard] = useState(false);
  const [showDeployAll, setShowDeployAll] = useState(false);
  // Fleet deploy runs entirely server-side (per-device background tasks), so
  // it outlives the modal. deployState persists {version, started, skipped}
  // at the App level so you can close the modal and reopen it (via the header
  // pill) to see live progress — the per-device rows read from `devices`,
  // which the events WebSocket keeps fresh. null = no deploy tracked.
  const [deployState, setDeployState] = useState(null);
  const [showSettings, setShowSettings] = useState(false);
  const [globalConfig, setGlobalConfig] = useState(null);
  const wsRef = useRef(null);

  const isAdmin = role === 'admin';

  function handleLogout() {
    API.post('/api/auth/logout', {}).catch(() => {});
    API.token = null;
    localStorage.removeItem('em_token');
    localStorage.removeItem('em_role');
    localStorage.removeItem('em_auth_via');
    // The landing page owns sign-in — green-ring login form.
    location.replace('.');
  }

  // Restore token on mount, and give API somewhere to send a dead session.
  // handleLogout only clears storage and navigates, so binding the first
  // render's copy is safe.
  useEffect(() => {
    if (token) API.token = token;
    API.onUnauthorized = handleLogout;
  }, []);

  // Reconcile the cached role against the server's.
  //
  // `role` seeds from localStorage, written once at sign-in, and nothing used
  // to re-read it — so a role that changed server-side never reached the UI.
  // That is not an edge case: it is what every PATCH /api/users/{id}
  // promotion looks like to the promoted person, who stays read-only until
  // they happen to sign out. Under ingress there is deliberately no Sign out
  // at all, so the stale value had nothing to clear it and correcting the
  // database by hand still left the panel read-only (#235).
  //
  // The symptom is precise and confusing: the SERVER accepts the writes, so
  // saving config works, while the UI hides every admin-only control. It
  // reads as half-broken rather than as stale state.
  //
  // /api/auth/me is the authority and it is already there. A failure is
  // deliberately ignored — the periodic loads below surface a dead session,
  // and a transient blip must not silently demote a working dashboard.
  useEffect(() => {
    if (!token) return;
    API.get('/api/auth/me').then(me => {
      if (!me || !me.role || me.role === role) return;
      setRole(me.role);
      localStorage.setItem('em_role', me.role);
    }).catch(() => {});
  }, [token]);

  // Load initial data
  useEffect(() => {
    if (!token) return;
    Promise.all([
      API.get('/api/devices'),
      API.get('/api/system/status'),
      API.get('/api/releases/latest').catch(() => null),
      API.get('/api/global/config').catch(() => null),
      API.get('/api/releases/controller').catch(() => null),
    ]).then(([devs, stat, rel, gcfg, ctrl]) => {
      setDevices(devs);
      setStatus(stat);
      setRelease(rel);
      setCtrlRelease(ctrl);
      if (gcfg) setGlobalConfig(gcfg);
    }).catch(e => {
      if (e.code === 'not_authenticated') { handleLogout(); }
      else setLoadError(e.error || 'Failed to load');
    });
  }, [token]);

  // Live events WebSocket
  useEffect(() => {
    if (!token) return;
    const ws = new WebSocket(ingressWebSocketUrl(`/api/events?token=${token}`));
    wsRef.current = ws;

    ws.onmessage = e => {
      const msg = JSON.parse(e.data);
      switch(msg.type) {
        case 'snapshot':
          setDevices(msg.devices);
          break;
        case 'device_update':
          // Merge partial state directly — no API round trip needed
          if (msg.state) {
            setDevices(prev => prev.map(d =>
              d.device_id === msg.device_id ? { ...d, ...msg.state } : d
            ));
          }
          break;
        case 'device_log':
          _emitDeviceLog(msg.device_id, msg.entry);
          break;
        case 'device_connected':
          setDevices(prev => prev.map(d =>
            d.device_id === msg.device_id ? { ...d, connected: true } : d
          ));
          break;
        case 'device_disconnected':
          console.log('[ws] device_disconnected:', msg.device_id);
          setDevices(prev => prev.map(d =>
            d.device_id === msg.device_id
              ? { ...d, connected: false, speaking: false, listening: false, thinking: false }
              : d
          ));
          break;
        case 'device_updated':
        case 'device_rolled_back':
        case 'device_update_failed':
        case 'device_approved':
          // Full refresh for structural changes
          API.get('/api/devices').then(setDevices).catch(() => {});
          break;
        case 'controller_update':
          // The controller polls GitHub hourly; a dashboard left open should
          // learn about a new controller without a reload.
          setCtrlRelease(msg);
          break;
        case 'device_pending':
          API.get('/api/devices').then(setDevices).catch(() => {});
          break;
        case 'device_deleted':
          setDevices(prev => prev.filter(d => d.device_id !== msg.device_id));
          break;
      }
    };

    ws.onclose = () => {
      // Reconnect after 5s
      setTimeout(() => {
        if (token) setToken(t => t); // trigger re-run
      }, 5000);
    };

    // Polling fallback — catches anything the WebSocket misses.
    //
    // The catch stays broad: a blip must not tear the dashboard down. A dead
    // session is not a blip, and it is not handled here — API.unauthorized()
    // has already fired by the time this runs. Before that existed, this line
    // was where an expired session went to be forgotten, five seconds at a
    // time, indefinitely.
    const poll = setInterval(() => {
      API.get('/api/devices').then(setDevices).catch(() => {});
    }, 5000);

    return () => {
      ws.close();
      clearInterval(poll);
    };

  }, [token]);

  // No session (direct visit, expired token, logged out) — the landing
  // page owns auth: it validates any stored token and shows the right
  // form (login vs first-run setup).
  if (!token) { location.replace('.'); return null; }

  const online   = devices.filter(d => d.connected).length;
  const approved = devices.filter(d => d.approved);
  const pending  = devices.filter(d => !d.approved);
  const updates  = approved.filter(d => d.firmware_ver && release?.version && d.firmware_ver !== release.version).length;
  const active   = approved.filter(d => d.speaking || d.listening || d.thinking).length;

  const selectedDevice = selected ? devices.find(d => d.device_id === selected) : null;

  return (
    <div className="em-page" style={{ minHeight: '100vh', padding: '32px 36px 60px' }}>

      {/* Header */}
      <div className="em-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end', marginBottom: 36 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 14 }}>
          <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 28, color: 'var(--text)', fontWeight: 600, letterSpacing: '-0.02em' }}>EchoMuse</div>
          <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--muted)', letterSpacing: '0.12em', textTransform: 'uppercase' }}>Device Management</div>
          {status?.controller_version && (
            <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--muted)' }}>{status.controller_version}</div>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--muted)' }}>{role}</div>
          <ThemeToggle/>
          <IconButton onClick={() => setShowSettings(true)} label="Settings">⚙</IconButton>
          {/* No sign-out on a Home Assistant session: HA owns it, so signing
              out would land on the landing page and be re-authenticated
              immediately — a button that visibly does nothing. Keyed on how
              this session was obtained, not on whether the page is under
              ingress: someone who fell back to the password form (Supervisor
              forwarded no user) can and should still sign out. */}
          {authVia !== 'ingress' && (
            <IconButton onClick={handleLogout} label="Sign out" danger><SignOutIcon/></IconButton>
          )}
        </div>
      </div>


      {/* Controller update notice.
          Rendered ONLY when a newer controller-v* tag exists, so it is an
          alert rather than permanent chrome — a panel that is always present
          stops being read.

          There is deliberately no update button. The controller is a
          container the user owns and updates with their own docker tooling;
          an in-app "update" would have to restart the process serving the
          page, mid-request, with no way to report the outcome. So this shows
          the version, what changed, and the exact command — everything needed
          to decide — and leaves the doing to them. */}
      {ctrlRelease?.available && ctrlRelease?.version && (
        <div className="em-ctrl-update" style={{
          background: 'var(--notice-bg)',
          border: '1px solid var(--notice-line)', borderRadius: 8,
          padding: '14px 18px', marginBottom: 24,
        }}>
          <div style={{ display:'flex', alignItems:'center', gap:12, flexWrap:'wrap' }}>
            <span style={{ fontFamily:"'DM Mono',monospace", fontSize:8, color:'var(--warn)',
                           textTransform:'uppercase', letterSpacing:'0.15em' }}>
              Controller update
            </span>
            <span style={{ fontFamily:"'DM Mono',monospace", fontSize:14, color:'var(--warn)' }}>
              {ctrlRelease.version}
            </span>
            <span style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--muted)' }}>
              running {ctrlRelease.current || status?.controller_version || '—'}
            </span>
            {ctrlRelease.notes && (
              <span onClick={() => setCtrlNotesOpen(o => !o)} style={{
                fontFamily:"'DM Mono',monospace", fontSize:9, color:'var(--muted)',
                cursor:'pointer', userSelect:'none', marginLeft:'auto',
                textTransform:'uppercase', letterSpacing:'0.15em',
              }}>
                {ctrlNotesOpen ? '▾' : '▸'} What&apos;s in it
              </span>
            )}
          </div>
          {ctrlNotesOpen && (
            <div style={{ marginTop:12, borderTop:'1px solid rgba(255,255,255,0.06)', paddingTop:12 }}>
              <pre style={{
                fontFamily:"'DM Mono',monospace", fontSize:10, lineHeight:1.65,
                color:'var(--text2)', whiteSpace:'pre-wrap', wordBreak:'break-word',
                margin:0, maxHeight:320, overflowY:'auto',
              }}>{ctrlRelease.notes}</pre>
              <div style={{ fontFamily:"'DM Mono',monospace", fontSize:9, color:'var(--muted)',
                            marginTop:14, lineHeight:1.6 }}>
                Update it yourself, from wherever your compose file lives:
              </div>
              <pre style={{
                fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--lcd-green)',
                background:'rgba(0,0,0,0.35)', border:'1px solid rgba(0,0,0,0.5)',
                borderRadius:6, padding:'10px 12px', margin:'8px 0 0', overflowX:'auto',
              }}>docker compose pull &amp;&amp; docker compose up -d</pre>
              {ctrlRelease.release_url && (
                <a href={ctrlRelease.release_url} target="_blank" rel="noreferrer"
                   style={{ fontFamily:"'DM Mono',monospace", fontSize:9, color:'var(--muted)',
                            display:'inline-block', marginTop:10 }}>
                  View tag on GitHub →
                </a>
              )}
            </div>
          )}
        </div>
      )}

      {/* Summary */}
      <div className="em-summary" style={{ display: 'flex', gap: 10, marginBottom: 36 }}>
        {[
          ['Online', `${online}/${approved.length}`, online === approved.length ? 'var(--ok)' : 'var(--warn)'],
          ['Active', active, active > 0 ? 'var(--accent)' : 'var(--muted)'],
          ['Updates', updates, updates > 0 ? 'var(--warn)' : 'var(--muted)'],
          ['Pending', pending.length, pending.length > 0 ? 'var(--accent-hi)' : 'var(--muted)'],
        ].map(([label, val, c]) => (
          <div key={label} className="em-inset" style={{ flex: 1 }}>
            <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 8, color: 'var(--lcd-dim)', textTransform: 'uppercase', letterSpacing: '0.15em', marginBottom: 6 }}>{label}</div>
            <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 24, color: c, lineHeight: 1, textShadow: `0 0 12px ${c}66` }}>{val}</div>
          </div>
        ))}
        {release && (
          <div className="em-summary-release em-inset" style={{ flex: 2, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <div>
              <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 8, color: 'var(--lcd-dim)', textTransform: 'uppercase', letterSpacing: '0.15em', marginBottom: 6 }}>Latest Release</div>
              <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 18, color: 'var(--lcd-green)', lineHeight: 1 }}>{release.version}</div>
            </div>
            {/* Actions as ONE flex child, not three.
                space-between distributes across every child it has, so with
                the version block, the check button and the deploy button all
                as siblings it spread them evenly over a double-width panel —
                the buttons ended up marooned in the middle. Grouping them
                leaves two children: version left, actions right. */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
            {isAdmin && (
              <IconButton accent busy={checkingRelease}
                label={checkingRelease ? 'Checking for updates…' : 'Check for updates'}
                onClick={async () => {
                setCheckingRelease(true);
                try {
                  // Same force-check route used by the Updates tab and
                  // wizard (POST /api/releases/check) — bypasses the
                  // cache so this is a genuine live GitHub check, not
                  // just re-reading whatever was last polled.
                  const rel = await API.post('/api/releases/check', {});
                  setRelease(rel);
                } catch(e) {
                  alert(e.error || 'Release check failed');
                }
                setCheckingRelease(false);
              }}><RefreshIcon/></IconButton>
            )}
            {isAdmin && (() => {
              const byId = Object.fromEntries(devices.map(d => [d.device_id, d]));
              const started = deployState ? (deployState.started || []) : [];
              const done = started.filter(id => {
                const d = byId[id];
                return d && d.connected && d.firmware_ver === deployState.version;
              }).length;
              // Failures are terminal too — otherwise one aborted update
              // pinned the pill at "Deploying…" until the page was reloaded.
              const failed = started.filter(id => {
                const d = byId[id];
                return d && d.update_error &&
                  !(d.connected && d.firmware_ver === deployState.version);
              }).length;
              const complete = started.length > 0 && done + failed === started.length;
              // While a deploy is in flight the progress pill replaces the
              // Deploy all button — both open the same modal, and offering a
              // second deploy mid-run reads as a broken control. The button
              // returns once the fleet is done (next release needs it).
              const inFlight = deployState && !complete;
              return (<>
                {release && !inFlight && (
                  <IconButton accent onClick={() => setShowDeployAll(true)}
                              label="Deploy latest firmware to all devices"><DeployIcon/></IconButton>
                )}
                {deployState && (
                  <Pill small onClick={() => setShowDeployAll(true)}>
                    {complete
                      ? (failed > 0
                          ? `⚠ ${deployState.version}: ${done} ok, ${failed} failed`
                          : `✓ Fleet on ${deployState.version}`)
                      : `Deploying ${deployState.version} — ${done}/${started.length}`}
                  </Pill>
                )}
              </>);
            })()}
            </div>
          </div>
        )}
      </div>

      {/* Pending devices */}
      {pending.length > 0 && (
        <>
          <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--accent-hi)', textTransform: 'uppercase', letterSpacing: '0.15em', marginBottom: 14 }}>
            Pending Approval · {pending.length}
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(190px,1fr))', gap: 12, marginBottom: 36 }}>
            {pending.map(d => <Card key={d.device_id} device={d} onClick={() => setSelected(d.device_id)}/>)}
          </div>
        </>
      )}

      {/* Device grid */}
      {(approved.length > 0 || isAdmin) && (
        <>
          {approved.length > 0 && (
            <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.15em', marginBottom: 14 }}>
              Devices · {approved.length}
            </div>
          )}
          {/* gridAutoRows:1fr equalises every row to the tallest item, so the
              provisioning tile is the same size as a device card instead of
              collapsing to its own content when it wraps onto a row alone.
              A matching height rather than a matching magic number — the card
              can gain a row without this drifting. */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(190px,1fr))', gridAutoRows: '1fr', gap: 12, marginBottom: 48 }}>
            {approved.map(d => <Card key={d.device_id} device={d} onClick={() => setSelected(d.device_id)}/>)}
            {isAdmin && <AddDeviceTile onClick={() => setShowWizard(true)}/>}
          </div>
        </>
      )}

      {devices.length === 0 && !loadError && !isAdmin && (
        <div style={{ textAlign: 'center', padding: '60px 0', fontFamily: "'DM Mono',monospace", fontSize: 12, color: 'var(--muted)' }}>
          No devices yet — power on an EchoMuse device to see it appear here
        </div>
      )}

      {loadError && (
        <div style={{ textAlign: 'center', padding: '60px 0', fontFamily: "'DM Mono',monospace", fontSize: 12, color: 'var(--error)' }}>{loadError}</div>
      )}

      {/* Provisioning wizard */}
      {showWizard && (
        <ProvisionWizard token={token} onClose={() => setShowWizard(false)} knownDevices={devices}/>
      )}

      {/* Fleet-wide OTA — the deploy itself is server-side; this modal is
          just the view, backed by App-level deployState so it survives close. */}
      {showDeployAll && (
        <DeployAllModal
          release={release}
          devices={devices}
          deployState={deployState}
          onStarted={setDeployState}
          onDismiss={() => setDeployState(null)}
          onClose={() => setShowDeployAll(false)}
        />
      )}

      {/* Settings panel */}
      {showSettings && globalConfig && (
        <SettingsPanel
          globalConfig={globalConfig}
          onGlobalConfigChange={setGlobalConfig}
          onClose={() => setShowSettings(false)}
          username={role}
          isAdmin={isAdmin}
        />
      )}

      {/* Detail modal */}
      {selectedDevice && (
        <Detail
          device={selectedDevice}
          token={token}
          onClose={() => setSelected(null)}
          onApprove={() => API.get('/api/devices').then(setDevices).catch(() => {})}
          isAdmin={isAdmin}
          globalConfig={globalConfig}
          onDeviceConfigChange={(device_id, patch) =>
            setDevices(prev => prev.map(d =>
              d.device_id === device_id ? { ...d, ...patch } : d
            ))
          }
        />
      )}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
