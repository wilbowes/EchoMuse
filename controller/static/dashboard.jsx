const { useState, useEffect, useRef, useCallback, useMemo } = React;

// ─── API ──────────────────────────────────────────────────────────────────────

const API = {
  token: null,
  role: null,

  headers() {
    const h = { 'Content-Type': 'application/json' };
    if (this.token) h['Authorization'] = `Bearer ${this.token}`;
    return h;
  },

  async get(path) {
    const r = await fetch(path, { headers: this.headers() });
    if (r.status === 401) throw { code: 'not_authenticated', status: 401 };
    const data = await r.json();
    if (!r.ok) throw data;
    return data;
  },

  async post(path, body) {
    const r = await fetch(path, { method: 'POST', headers: this.headers(), body: JSON.stringify(body) });
    if (r.status === 401) throw { code: 'not_authenticated', status: 401 };
    const data = await r.json();
    if (!r.ok) throw data;
    return data;
  },

  async patch(path, body) {
    const r = await fetch(path, { method: 'PATCH', headers: this.headers(), body: JSON.stringify(body) });
    if (r.status === 401) throw { code: 'not_authenticated', status: 401 };
    const data = await r.json();
    if (!r.ok) throw data;
    return data;
  },

  async del(path) {
    const r = await fetch(path, { method: 'DELETE', headers: this.headers() });
    if (r.status === 401) throw { code: 'not_authenticated', status: 401 };
    const data = await r.json();
    if (!r.ok) throw data;
    return data;
  },

  async upload(path, file) {
    const h = {};
    if (this.token) h['Authorization'] = `Bearer ${this.token}`;
    const form = new FormData();
    form.append('binary', file);
    const r = await fetch(path, { method: 'POST', headers: h, body: form });
    if (r.status === 401) throw { code: 'not_authenticated', status: 401 };
    const data = await r.json();
    if (!r.ok) throw data;
    return data;
  },
};

// ─── Helpers ──────────────────────────────────────────────────────────────────

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

function deviceState(d) {
  if (!d.approved)  return { key: 'pending',   label: 'Pending',   color: '#6080a8', dot: '#8ab0d0' };
  if (!d.connected) return { key: 'offline',   label: 'Offline',   color: '#c0601a', dot: '#d4703a' };
  if (d.muted)      return { key: 'muted',     label: 'Muted',     color: '#b03030', dot: '#c04040' };
  if (d.speaking)   return { key: 'speaking',  label: 'Speaking',  color: '#2060b0', dot: '#4080d0' };
  if (d.thinking)   return { key: 'thinking',  label: 'Thinking',  color: '#806010', dot: '#a08020' };
  if (d.listening)  return { key: 'listening', label: 'Listening', color: '#286040', dot: '#40906a' };
  return               { key: 'idle',      label: 'Idle',      color: '#8a8a8a', dot: '#aaaaaa' };
}

function eventAccent(level) {
  return { info: '#286040', warn: '#806010', error: '#b03030' }[level] || '#8a8680';
}

// ─── Components ───────────────────────────────────────────────────────────────

function Lcd({ label, value, color, size = 16 }) {
  return (
    <div style={{ background: 'linear-gradient(160deg,#2a2e28,#1e2219)', border: '1px solid #1a1c18', borderRadius: 5, padding: '5px 10px', boxShadow: 'inset 0 1px 3px rgba(0,0,0,0.5)', minWidth: 54 }}>
      {label && <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 8, color: 'var(--lcd-dim)', textTransform: 'uppercase', letterSpacing: '0.15em', marginBottom: 3 }}>{label}</div>}
      <div style={{ fontFamily: "'DM Mono',monospace", fontSize: size, color: color || 'var(--lcd-green)', lineHeight: 1, textShadow: `0 0 8px ${color || 'var(--lcd-green)'}88` }}>{value}</div>
    </div>
  );
}

function Pill({ children, accent, danger, disabled, onClick, small }) {
  const bg = disabled ? 'linear-gradient(180deg,#d0ccc4,#bab6ae)'
           : danger   ? 'linear-gradient(180deg,#a83030,#782020)'
           : accent   ? 'linear-gradient(180deg,#6080a8,#405878)'
           :            'linear-gradient(180deg,#d8d4cc,#c0bdb6)';
  const color = disabled ? '#8a8680' : danger ? '#f0d8d8' : accent ? '#dde8f0' : '#2a2822';
  const border = disabled ? '1px solid #aca8a0' : danger ? '1px solid #602020' : accent ? '1px solid #304860' : '1px solid #a8a49c';
  return (
    <button onClick={onClick} disabled={disabled} style={{
      background: bg, color, border, borderRadius: 20,
      fontFamily: "'DM Sans',sans-serif", fontSize: small ? 11 : 12, fontWeight: 500,
      padding: small ? '5px 14px' : '7px 20px',
      cursor: disabled ? 'not-allowed' : 'pointer',
      boxShadow: disabled ? 'none' : '0 1px 0 rgba(255,255,255,0.15) inset, 0 2px 4px rgba(0,0,0,0.2)',
      transition: 'all 0.1s', whiteSpace: 'nowrap',
    }}>{children}</button>
  );
}

// SectionLabel — the small uppercase mono heading used throughout. One
// definition instead of the same inline style repeated per call site.
function SectionLabel({ children, style }) {
  return (
    <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.15em', marginBottom: 12, ...style }}>
      {children}
    </div>
  );
}

// Panel — bordered card grouping related controls. Gives tab content a
// consistent visual structure instead of floating elements.
function Panel({ label, children, style }) {
  return (
    <div style={{ background: 'linear-gradient(170deg,#e4e0d8,#dad6ce)', border: '1px solid #b8b4ac', borderRadius: 10, padding: '14px 16px 16px', boxShadow: '0 1px 0 rgba(255,255,255,0.6) inset', ...style }}>
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
      background: 'linear-gradient(180deg,#d0ccc4,#bab6ae)', border: '1px solid #a0a098',
      borderRadius: '50%', width: 28, height: 28, display: 'flex', alignItems: 'center',
      justifyContent: 'center', cursor: 'pointer', boxShadow: '0 1px 0 rgba(255,255,255,0.5) inset',
      color: color || '#5a5650', fontSize: 15, fontWeight: 300, lineHeight: 1,
    }}>{children}</button>
  );
}

function Slider({ label, sub, value, min, max, step = 1, unit = '', formatValue, onChange }) {
  const display = formatValue ? formatValue(value) : `${value}${unit}`;
  return (
    <div style={{ marginBottom: 20 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 7 }}>
        <div>
          <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: 'var(--text2)' }}>{label}</span>
          {sub && <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--muted)', marginLeft: 8 }}>{sub}</span>}
        </div>
        <Lcd value={display} size={12} />
      </div>
      <input type="range" min={min} max={max} step={step} value={value} onChange={e => onChange(Number(e.target.value))} />
    </div>
  );
}

function Toggle({ label, sub, value, onChange }) {
  // minWidth: 0 on the flex container and label lets long label/sub text
  // shrink and wrap instead of forcing the row (and the switch with it)
  // wider than the grid column — which pushed the switch past the edge of
  // the config dialog. flexShrink: 0 keeps the switch at full size.
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20, minWidth: 0, gap: 10 }}>
      <div style={{ minWidth: 0, flex: 1 }}>
        <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: 'var(--text2)' }}>{label}</span>
        {sub && <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--muted)', marginLeft: 8 }}>{sub}</span>}
      </div>
      <div onClick={() => onChange(!value)} style={{
        width: 36, height: 20, borderRadius: 10, cursor: 'pointer', position: 'relative', flexShrink: 0,
        background: value ? '#405878' : '#888480',
        border: value ? '1px solid #304860' : '1px solid #686460',
        transition: 'background 0.15s',
      }}>
        <div style={{
          position: 'absolute', top: 2, left: value ? 17 : 2,
          width: 14, height: 14, borderRadius: 7,
          background: value ? '#dde8f0' : '#ccc8c4',
          transition: 'left 0.15s',
        }}/>
      </div>
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
      <rect x={PL} y={PT} width={IW} height={IH} fill="rgba(0,0,0,0.07)" rx="2"/>
      {dbTicks.map(db => (
        <line key={db} x1={PL} x2={PL+IW} y1={yOf(db)} y2={yOf(db)}
          stroke={db===0?'rgba(0,0,0,0.18)':'rgba(0,0,0,0.07)'}
          strokeWidth={db===0?1:0.5} strokeDasharray={db===0?undefined:'2,3'}/>
      ))}
      {fTicks.map(({f}) => (
        <line key={f} x1={xOf(f)} x2={xOf(f)} y1={PT} y2={PT+IH}
          stroke="rgba(0,0,0,0.06)" strokeWidth={0.5}/>
      ))}
      <path d={fill} fill="rgba(64,88,120,0.10)"/>
      <path d={line} fill="none" stroke="#405878" strokeWidth="1.5"
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

function SignalBars({ rssi }) {
  // 0 bars = no signal / null, 4 bars = excellent
  const level = rssi == null ? 0
              : rssi > -60   ? 4
              : rssi > -70   ? 3
              : rssi > -80   ? 2
              : rssi > -90   ? 1
              :                0;
  const on  = level > 0 ? '#3a6a50' : 'rgba(0,0,0,0.13)';
  const off = 'rgba(0,0,0,0.13)';
  const bars = [{h:4,y:11},{h:7,y:8},{h:10,y:5},{h:14,y:1}];
  return (
    <svg width={20} height={16} style={{ display:'block', flexShrink:0 }}>
      {bars.map((b,i) => (
        <rect key={i} x={i*5} y={b.y} width={4} height={b.h} rx={1}
          fill={i < level ? (level===1?'#9a3020':level===2?'#8a6010':'#3a6a50') : off}/>
      ))}
    </svg>
  );
}

function StatBar({ label, pct, text }) {
  const color = pct == null ? 'transparent'
              : pct > 85   ? '#9a3020'
              : pct > 65   ? '#8a6010'
              :               '#3a6a50';
  return (
    <div style={{ marginBottom: 13 }}>
      <div style={{ display:'flex', justifyContent:'space-between', marginBottom:5 }}>
        <span style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--muted)', textTransform:'uppercase', letterSpacing:'0.08em' }}>{label}</span>
        <span style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--text2)' }}>{text ?? '—'}</span>
      </div>
      <div style={{ height:3, borderRadius:2, background:'rgba(0,0,0,0.10)', overflow:'hidden' }}>
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

    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const sock = new WebSocket(`${proto}://${location.host}/api/devices/${deviceId}/shell?token=${token}`);
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

function TurnObservability({ turns, nearMisses, stateLabel, stateColor }) {
  const [hover, setHover] = useState(null); // index into `recent`
  const mono = "'DM Mono',monospace";

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
                style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '3px 0', cursor: 'default', background: hover === i ? 'rgba(0,0,0,0.04)' : 'transparent', borderRadius: 4 }}>
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
                <span style={{ fontFamily: mono, fontSize: 8, textTransform: 'uppercase', letterSpacing: '0.08em', width: 62, flexShrink: 0, color: failed ? '#a04010' : '#286040' }}>
                  {t.outcome === 'ok' ? 'ok' : (t.outcome || '?').replace(/_/g, ' ')}
                </span>
              </div>
            );
          })}
          </div>

          {/* Hover detail */}
          {hover != null && recent[hover] && (() => {
            const t = recent[hover]; const seg = turnSegments(t);
            return (
              <div style={{ marginTop: 10, background: 'rgba(0,0,0,0.05)', border: '1px solid rgba(0,0,0,0.1)', borderRadius: 6, padding: '8px 12px', fontFamily: mono, fontSize: 10, color: 'var(--text2)', lineHeight: 1.7 }}>
                <span style={{ color: 'var(--muted)' }}>{t.trigger}</span>
                {' · '}listening {fmtS(seg.listen)} · transcribe {fmtS(seg.transcribe)} · respond {fmtS(seg.respond)} · total {fmtS(Math.max(t.total_ms, 0))}
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
          <div style={{ fontFamily:mono, fontSize:11, color:'#405878' }}>
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
          <div style={{ fontFamily:mono, fontSize:11, color: result.ok ? '#286040' : '#c0601a' }}>
            {result.ok
              ? `Switched to “${result.ssid}” successfully.`
              : `Change to “${result.ssid}” failed — previous network restored.`}
          </div>
          {!result.ok && result.error && (
            <div style={{ fontFamily:mono, fontSize:10, color:'var(--muted)', marginTop:4 }}>{result.error}</div>
          )}
        </div>
      )}

      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:16, alignItems:'start' }}>
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
            {scanError && <span style={{ fontFamily:mono, fontSize:10, color:'#c0601a' }}>{scanError}</span>}
          </div>
          {networks && networks.length === 0 && (
            <div style={{ fontFamily:mono, fontSize:10, color:'var(--muted)' }}>No networks found.</div>
          )}
          {networks && networks.length > 0 && (
            <div style={{ maxHeight:170, overflowY:'auto' }}>
              {networks.map(n => (
                <div key={n.ssid} onClick={() => !busy && setSsid(n.ssid)}
                  style={{ display:'flex', justifyContent:'space-between', alignItems:'center', padding:'5px 8px', borderRadius:6, cursor: busy ? 'default' : 'pointer', background: ssid === n.ssid ? 'rgba(64,88,120,0.12)' : 'transparent' }}>
                  <span style={{ fontFamily:mono, fontSize:11, color: ssid === n.ssid ? '#405878' : 'var(--text)' }}>
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
        <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr auto', gap:12, alignItems:'end' }}>
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
          <div style={{ fontFamily:mono, fontSize:10, color:'#c0601a', marginTop:8 }}>
            {/["\\]/.test(ssid + psk)
              ? 'SSID/passphrase cannot contain " or \\ characters.'
              : 'WPA passphrase must be 8–63 characters (leave blank for an open network).'}
          </div>
        )}
        {submitError && (
          <div style={{ fontFamily:mono, fontSize:10, color:'#c0601a', marginTop:8 }}>{submitError}</div>
        )}
        {!device.connected && (
          <div style={{ fontFamily:mono, fontSize:10, color:'#c0601a', marginTop:8 }}>Device offline — connect it before changing networks.</div>
        )}
      </Panel>
    </div>
  );
}

// ─── Device detail modal ──────────────────────────────────────────────────────

function Detail({ device, token, onClose, onApprove, isAdmin, globalConfig, onDeviceConfigChange }) {
  const [tab, setTab] = useState('status');
  const [config, setConfig] = useState({ ...device.config });
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [useGlobalConfig, setUseGlobalConfig] = useState(device.use_global_config ?? true);
  const [logs, setLogs] = useState([]);
  const [logsLoading, setLogsLoading] = useState(false);
  const [pushLog, setPushLog] = useState([]);
  const [pushing, setPushing] = useState(false);
  const [release, setRelease] = useState(null);
  const [checkingRelease, setCheckingRelease] = useState(false);
  const [approveLabel, setApproveLabel] = useState(device.label || '');
  const [approving, setApproving] = useState(false);
  const [localFile, setLocalFile] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState(device.label || '');
  const [renameSaving, setRenameSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const fileInputRef = useRef(null);
  const [turns, setTurns] = useState([]);
  const state = deviceState(device);
  const needsUpdate = device.firmware_ver && release?.version && device.firmware_ver !== release.version;

  const TABS = device.approved
    ? (isAdmin ? ['status', 'config', 'wifi', 'console', 'updates', 'logs'] : ['status', 'config', 'logs'])
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
    }
  }, [tab, device.device_id]);

  // Turn observability — fetch on Status tab entry, refresh every 10s while
  // the tab is open (turn history is in-memory on the controller).
  useEffect(() => {
    if (tab !== 'status') return;
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
      const body = useGlobalConfig
        ? { use_global_config: true }
        : { use_global_config: false, ...config };
      const res = await API.post(`/api/devices/${device.device_id}/config`, body);
      setDirty(false);
      // Keep parent device list in sync so re-opening the modal is consistent
      if (onDeviceConfigChange) {
        onDeviceConfigChange(device.device_id, {
          config: res.config,
          use_global_config: res.use_global_config,
        });
      }
    } catch(e) { alert(e.error || 'Failed to push config'); }
    setSaving(false);
  }

  async function doUpdate() {
    setPushing(true); setPushLog(['Fetching latest release from GitHub…']);
    try {
      const res = await API.post(`/api/devices/${device.device_id}/update`, {});
      setPushLog(l => [...l, `Deploying ${res.version} — waiting for reconnect…`]);
      _pollReconnect(res.version);
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
      setPushLog(l => [...l, '✓ Upload complete — deploying…']);
      const res = await API.post(`/api/devices/${device.device_id}/update`, { upload_token: up.upload_token });
      setPushLog(l => [...l, `Deploying ${res.version} — waiting for reconnect…`]);
      _pollReconnect(res.version);
    } catch(e) {
      setUploading(false);
      setPushLog(l => [...l, `Error: ${e.error || 'Deploy failed'}`]);
      setPushing(false);
    }
  }

  function _pollReconnect(targetVersion) {
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
        } else if (wasDisconnected && d?.connected && d?.firmware_ver && d.firmware_ver !== targetVersion) {
          setPushLog(l => [...l, `⚠ Device reconnected on ${d.firmware_ver} — auto-rolled back`]);
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
      _pollReconnect(device.firmware_previous);
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
    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0', borderBottom: '1px solid rgba(0,0,0,0.06)' }}>
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
      <div style={{ width: 'min(900px,95vw)', height: 'min(700px,90vh)', background: 'linear-gradient(170deg,#e8e4de,#d8d4cc)', border: '1px solid #b8b4ac', borderRadius: 16, boxShadow: '0 24px 80px rgba(0,0,0,0.3),0 2px 0 rgba(255,255,255,0.8) inset', display: 'flex', flexDirection: 'column', overflow: 'hidden', animation: 'fadeIn 0.15s ease' }}>
        {/* Header */}
        <div style={{ background: 'linear-gradient(180deg,#dedad2,#ccc8c0)', borderBottom: '1px solid #b0aca4', padding: '20px 24px 0', boxShadow: '0 1px 0 rgba(255,255,255,0.5) inset' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 20, marginBottom: 16 }}>
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
                {needsUpdate && <span style={{ color: '#806010', marginLeft: 10 }}>Update available</span>}
              </div>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <div style={{ background: 'linear-gradient(160deg,#2a2e28,#1c1f18)', border: '1px solid #1a1c16', borderRadius: 6, padding: '5px 12px', boxShadow: 'inset 0 1px 3px rgba(0,0,0,0.5)' }}>
                <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: state.dot, textShadow: `0 0 8px ${state.dot}88`, letterSpacing: '0.05em' }}>{state.label.toUpperCase()}</span>
              </div>
              {isAdmin && !confirmDelete && (
                <CircleButton onClick={() => setConfirmDelete(true)} title="Delete device" color="#a04848">🗑</CircleButton>
              )}
              {isAdmin && confirmDelete && (
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: '#a04848' }}>Delete?</span>
                  <Pill small danger disabled={deleting} onClick={doDelete}>{deleting ? '…' : 'Confirm'}</Pill>
                  <Pill small onClick={() => setConfirmDelete(false)} disabled={deleting}>Cancel</Pill>
                </div>
              )}
              <CircleButton onClick={onClose} title="Close">×</CircleButton>
            </div>
          </div>
          <div style={{ display: 'flex', gap: 2 }}>
            {TABS.map(t => (
              <button key={t} onClick={() => setTab(t)} style={{ background: tab === t ? 'linear-gradient(180deg,#e8e4de,#d8d4cc)' : 'transparent', border: tab === t ? '1px solid #b0aca4' : '1px solid transparent', borderBottom: tab === t ? '1px solid #d8d4cc' : '1px solid transparent', borderRadius: '6px 6px 0 0', fontFamily: "'DM Mono',monospace", fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.1em', padding: '7px 14px', cursor: 'pointer', color: tab === t ? 'var(--text)' : 'var(--muted)', marginBottom: -1, transition: 'color 0.15s' }}>{t}</button>
            ))}
          </div>
        </div>

        {/* Body */}
        <div style={{ flex: 1, overflowY: 'auto', padding: 24 }}>

          {/* APPROVE */}
          {tab === 'approve' && (
            <div style={{ maxWidth: 400 }}>
              <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.15em', marginBottom: 16 }}>New Device — Pending Approval</div>
              {row('Serial', device.device_id)}
              {row('IP', device.ip && device.ip !== '127.0.0.1' ? device.ip : '—')}
              {row('First seen', relTime(device.first_seen))}
              <div style={{ marginTop: 24, marginBottom: 8 }}>
                <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: 'var(--text2)', marginBottom: 8 }}>Label</div>
                <input type="text" value={approveLabel} onChange={e => setApproveLabel(e.target.value)} placeholder="e.g. Kitchen" onKeyDown={e => e.key === 'Enter' && doApprove()}/>
              </div>
              <div style={{ marginTop: 20 }}>
                <Pill accent disabled={approving} onClick={doApprove}>{approving ? 'Approving…' : 'Approve Device'}</Pill>
              </div>
            </div>
          )}

          {/* STATUS */}
          {tab === 'status' && (() => {
            const s = device.stats || null;
            const cpuText  = s?.cpuPct    != null ? `${s.cpuPct.toFixed(0)}%` : null;
            const ramText  = s?.memUsedMb != null ? `${s.memUsedMb} / ${s.memTotalMb} MB` : null;
            const ramPct   = s?.memTotalMb? s.memUsedMb/s.memTotalMb*100 : null;
            const stoPct   = s?.storageTotalMb ? s.storageUsedMb/s.storageTotalMb*100 : null;
            const stoText  = s?.storageTotalMb != null
              ? `${(s.storageUsedMb/1024).toFixed(1)} / ${(s.storageTotalMb/1024).toFixed(1)} GB` : null;
            const cfgEff = (device.use_global_config ?? true) ? (globalConfig || device.config || {}) : (device.config || {});
            const wwLabel = (cfgEff.owwModel || '—').replace(/_v[\d.]+$/, '').replace(/_/g, ' ');
            return (
              <div style={{ minHeight:'100%', display:'flex', flexDirection:'column', gap:16 }}>
                <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:16 }}>
                  <Panel label="Device">
                    {row('IP', (() => {
                      const ip = device.ip && device.ip !== '127.0.0.1' ? device.ip : null;
                      return device.connected ? (ip || '—') : (ip ? `${ip} (last seen)` : '—');
                    })())}
                    {row('Firmware', device.firmware_ver || '—')}
                    {row('Last seen', relTime(device.last_seen))}
                    {row('Connected', device.connected ? 'Yes' : 'No', device.connected ? '#286040' : '#c0601a')}
                    {row('Config', (device.use_global_config ?? true) ? 'Fleet' : 'Device override')}
                  </Panel>
                  <Panel label="Resources">
                    <StatBar label="CPU"     pct={s?.cpuPct}    text={cpuText}/>
                    <StatBar label="RAM"     pct={ramPct}        text={ramText}/>
                    <StatBar label="Storage" pct={stoPct}        text={stoText}/>
                    <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center' }}>
                      <span style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--muted)', textTransform:'uppercase', letterSpacing:'0.08em' }}>WiFi</span>
                      <div style={{ display:'flex', alignItems:'center', gap:8 }}>
                        <span style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--text2)' }}>{s?.wifiRssi != null ? `${s.wifiRssi} dBm` : '—'}</span>
                        <SignalBars rssi={s?.wifiRssi ?? null}/>
                      </div>
                    </div>
                    {!s && <div style={{ fontFamily:"'DM Mono',monospace", fontSize:9, color:'var(--muted)', marginTop:8 }}>waiting for device stats…</div>}
                  </Panel>
                </div>
                <Panel label={`Voice activity — ${wwLabel} @ ${cfgEff.owwThreshold != null ? cfgEff.owwThreshold.toFixed(2) : '—'}`} style={{ flex:1 }}>
                  <TurnObservability
                    turns={turns}
                    nearMisses={device.owwNearMisses}
                    stateLabel={state.label.toUpperCase()}
                    stateColor={state.dot}
                  />
                </Panel>
              </div>
            );
          })()}

          {/* CONFIG */}
          {tab === 'config' && (
            <div>
              {/* Global override toggle */}
              {isAdmin && globalConfig && (
                <div style={{
                  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                  background: useGlobalConfig ? 'rgba(64,88,120,0.08)' : 'rgba(40,96,64,0.08)',
                  border: `1px solid ${useGlobalConfig ? 'rgba(64,88,120,0.2)' : 'rgba(40,96,64,0.2)'}`,
                  borderRadius: 8, padding: '12px 16px', marginBottom: 24,
                }}>
                  <div>
                    <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: 'var(--text2)' }}>
                      {useGlobalConfig ? 'Using fleet config' : 'Device-specific config'}
                    </div>
                    <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--muted)', marginTop: 3 }}>
                      {useGlobalConfig
                        ? 'Enable override to customise settings for this device only'
                        : 'Disable override to revert this device to fleet defaults'}
                    </div>
                  </div>
                  <Toggle
                    label="" sub=""
                    value={!useGlobalConfig}
                    onChange={enabled => {
                      if (enabled) {
                        // Enabling per-device: seed from current global config
                        setConfig({ ...(globalConfig || device.config) });
                        setUseGlobalConfig(false);
                        setDirty(true);
                      } else {
                        // Reverting to global
                        setUseGlobalConfig(true);
                        setDirty(true);
                      }
                    }}
                  />
                </div>
              )}

              {/* Config form — read-only when on global, editable when overridden */}
              <DeviceConfigForm
                config={useGlobalConfig ? (globalConfig || config) : config}
                onChange={(k, v) => setConf(k, v)}
                disabled={useGlobalConfig}
              />

              {isAdmin && dirty && (
                <div style={{ display: 'flex', gap: 10, marginTop: 24 }}>
                  <Pill accent disabled={saving} onClick={pushConfig}>
                    {saving ? 'Pushing…' : useGlobalConfig ? 'Revert to fleet config' : 'Push config'}
                  </Pill>
                  <Pill onClick={() => {
                    setConfig({ ...device.config });
                    setUseGlobalConfig(device.use_global_config ?? true);
                    setDirty(false);
                  }}>Cancel</Pill>
                </div>
              )}
            </div>
          )}

          {/* WIFI — per-device connectivity */}
          {tab === 'wifi' && <ConnectivityTab device={device} row={row}/>}

          {/* CONSOLE — fills the whole tab frame */}
          {tab === 'console' && (
            device.connected
              ? <div style={{ height: '100%' }}><Shell deviceId={device.device_id} token={token} height="100%"/></div>
              : <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 12, color: '#c0601a' }}>Device offline — console unavailable</div>
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
                    <span style={{ fontFamily:"'DM Mono',monospace", fontSize:11, color: needsUpdate ? '#806010' : '#286040' }}>
                      {release?.version ? (needsUpdate ? `Update ${release.version} available` : 'Up to date') : 'No release info'}
                    </span>
                    <Pill small onClick={doCheckRelease} disabled={checkingRelease}>
                      {checkingRelease ? 'Checking…' : 'Check now'}
                    </Pill>
                  </div>
                </div>
              </Panel>

              {/* Deploy sources, side by side */}
              <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:16 }}>
                <Panel label="GitHub Release">
                  <div style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--muted)', lineHeight:1.6, marginBottom:14 }}>
                    Deploy the latest tagged release build to this device. A/B slots — the previous binary stays available for rollback.
                  </div>
                  <div style={{ display:'flex', gap:10 }}>
                    <Pill accent={device.connected && !pushing && needsUpdate}
                          disabled={!device.connected || pushing || !needsUpdate}
                          onClick={doUpdate}>
                      {pushing && !localFile ? 'Updating…' : 'Push update'}
                    </Pill>
                    {device.firmware_previous && (
                      <Pill disabled={!device.connected || pushing} onClick={doRollback}>
                        Roll back
                      </Pill>
                    )}
                  </div>
                </Panel>

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

              {/* Activity console — always present so the layout never jumps
                  when a deploy starts */}
              <div style={{ background:'linear-gradient(160deg,#252820,#1e2219)', border:'1px solid #1a1c18', borderRadius:8, padding:14, fontFamily:"'DM Mono',monospace", fontSize:12, boxShadow:'inset 0 2px 6px rgba(0,0,0,0.5)', minHeight:96, flex:1 }}>
                {pushLog.length === 0 && !pushing && (
                  <span style={{ color:'#3a4a30' }}>— no deploy activity this session —</span>
                )}
                {pushLog.map((line, i) => (
                  <div key={i} style={{
                    color: line.startsWith('✓') ? '#9aba80'
                         : line.startsWith('⚠') ? '#c09040'
                         : line.startsWith('Error') ? '#c04040'
                         : '#5a6a50',
                    marginBottom:4,
                    textShadow: line.startsWith('✓') ? '0 0 8px rgba(140,200,100,0.4)' : 'none',
                  }}>{line}</div>
                ))}
                {pushing && <span style={{ color:'#3a4a30' }}>▌</span>}
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
                <div key={i} style={{ display: 'flex', gap: 12, alignItems: 'baseline', padding: '8px 0', borderBottom: '1px solid rgba(0,0,0,0.06)' }}>
                  <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: '#a8a49c', minWidth: 60, flexShrink: 0 }}>{new Date(entry.ts).toLocaleTimeString()}</span>
                  <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: eventAccent(entry.level), textTransform: 'uppercase', letterSpacing: '0.1em', minWidth: 48, flexShrink: 0 }}>{entry.level}</span>
                  <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: entry.source === 'device' ? '#4a6a40' : '#3a4a60', textTransform: 'uppercase', letterSpacing: '0.08em', minWidth: 64, flexShrink: 0 }}>{entry.source}</span>
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
    <div onClick={onClick} style={{ background: 'linear-gradient(160deg,#e0dcd4,#ccc8c0)', border: '1px solid #b8b4ac', borderRadius: 14, cursor: 'pointer', boxShadow: '0 4px 16px rgba(0,0,0,0.12),0 1px 0 rgba(255,255,255,0.7) inset', transition: 'box-shadow 0.15s,transform 0.1s', userSelect: 'none', opacity: isPending ? 0.85 : 1 }}
      onMouseEnter={e => { e.currentTarget.style.boxShadow = '0 8px 28px rgba(0,0,0,0.18),0 1px 0 rgba(255,255,255,0.7) inset'; e.currentTarget.style.transform = 'translateY(-1px)'; }}
      onMouseLeave={e => { e.currentTarget.style.boxShadow = '0 4px 16px rgba(0,0,0,0.12),0 1px 0 rgba(255,255,255,0.7) inset'; e.currentTarget.style.transform = 'translateY(0)'; }}>
      <div style={{ background: 'linear-gradient(180deg,#d0ccc4,#c4c0b8)', borderBottom: '1px solid #b0aca4', borderRadius: '13px 13px 0 0', padding: '10px 16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', boxShadow: '0 1px 0 rgba(255,255,255,0.4) inset' }}>
        <span style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 14, color: 'var(--text)', fontWeight: 600, letterSpacing: '-0.01em' }}>
          {device.label || <span style={{ color: 'var(--muted)', fontSize: 12 }}>{device.device_id.slice(0, 8)}…</span>}
        </span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          {isPending && (
            <div style={{ background: 'linear-gradient(160deg,#2a2e28,#1c1f18)', border: '1px solid #1a1c16', borderRadius: 3, padding: '1px 6px', fontFamily: "'DM Mono',monospace", fontSize: 8, color: '#8ab0d0', letterSpacing: '0.1em' }}>PENDING</div>
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
        <div style={{ background: 'linear-gradient(160deg,#2a2e28,#1e2219)', border: '1px solid #1a1c18', borderRadius: 6, padding: '7px 12px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', boxShadow: 'inset 0 2px 4px rgba(0,0,0,0.5)' }}>
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
    constructor(adb, transport, banner) {
      this._adb = adb;
      this._transport = transport;
      this.banner = banner;  // product name string, e.g. "omni_biscuit" or "csm_biscuit"
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
      this._log(`push: opening cat > '${remotePath}' (${(bytes.length/1024/1024).toFixed(1)} MB)`);
      const proc  = await this._adb.subprocess.noneProtocol.spawn(`cat > '${remotePath}'`);
      this._log('push: stream open, writing chunks…');
      const writer = proc.stdin.getWriter();
      const SZ = 64 * 1024;
      for (let i = 0; i < bytes.length; i += SZ) {
        await writer.write(bytes.subarray(i, Math.min(i + SZ, bytes.length)));
        onProgress?.((i + SZ) / bytes.length);
      }
      this._log('push: all chunks written, closing stdin…');
      await writer.close();
      onProgress?.(1);
      this._log('push: done.');
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
        throw new Error(
          'WebUSB not available — requires a secure context (HTTPS or localhost). ' +
          'Access the dashboard at http://localhost:8768, or enable ' +
          'chrome://flags/#unsafely-treat-insecure-origin-as-secure for this origin.'
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

      return new Client(adb, transport, banner);
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
        border: `2px dashed ${hover ? 'var(--text2)' : '#b0aa9f'}`,
        borderRadius: 12, minHeight: 160, display: 'flex', flexDirection: 'column',
        alignItems: 'center', justifyContent: 'center', gap: 8, cursor: 'pointer',
        transition: 'border-color 0.15s, opacity 0.15s', opacity: hover ? 1 : 0.6,
        userSelect: 'none',
      }}
    >
      <div style={{ fontSize: 28, color: hover ? 'var(--text2)' : '#b0aa9f', lineHeight: 1 }}>+</div>
      <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: hover ? 'var(--text2)' : '#b0aa9f', letterSpacing: '0.12em', textTransform: 'uppercase' }}>Provision Device</div>
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
//  8  disable_alexa    — pm disable x9 BEFORE wifi — stops phoning home    [auto]
//  9  wifi             — configure WiFi network                            [inputs]
// 10  install_em       — push binary + startup script                      [file]
const _WIZARD_STEPS = [
  { id: 'connect_android', label: 'Connect Device',     desc: 'Connect the Echo Dot via USB. Device should be on and booted into Android. Appears as "AEOBC" in the USB picker.' },
  { id: 'connect_twrp',    label: 'Connect to TWRP',   desc: 'Wait for TWRP recovery to appear, then reconnect. Appears as "Echo" in the USB picker.' },
  { id: 'patch_boot',      label: 'Patch Boot Image',  desc: 'Apply SELinux permissive patch and add init.rc service entries.' },
  { id: 'install_magisk',  label: 'Install Magisk',    desc: 'Flash Magisk 17.3 for persistent root access.' },
  { id: 'preseed_db',      label: 'Pre-seed Root DB',  desc: 'Grant root to ADB shell without a screen prompt.' },
  { id: 'reboot',          label: 'Reboot to Android', desc: 'Reboot device to Android.' },
  { id: 'reconnect',       label: 'Reconnect',         desc: 'Re-connect ADB after Android finishes booting. Appears as "AEOBC" in the USB picker.' },
  { id: 'verify_root',     label: 'Verify Root',       desc: 'Confirm Magisk root is working.' },
  { id: 'disable_alexa',   label: 'Disable Alexa',     desc: 'Disable all 9 Alexa voice pipeline packages before connecting to WiFi.' },
  { id: 'wifi',            label: 'Configure WiFi',    desc: 'Connect the device to your local WiFi network.' },
  { id: 'install_em',      label: 'Install EchoMuse',  desc: 'Push server binary and startup script to device.' },
];

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
          border: '1px solid #c0bcb4', borderRadius: 6, overflow: 'hidden',
          maxHeight: 140, overflowY: 'auto',
        }}>
          {networks.map(n => (
            <div key={n.ssid}
              onClick={() => setWifiSsid(n.ssid)}
              style={{
                padding: '6px 10px', display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                background: wifiSsid === n.ssid ? 'rgba(64,88,120,0.12)' : 'transparent',
                borderBottom: '1px solid #d0ccc4', cursor: 'pointer',
              }}>
              <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: wifiSsid === n.ssid ? '#405878' : 'var(--text)' }}>
                {n.ssid}
              </span>
              <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)' }}>
                {n.signal} dBm
              </span>
            </div>
          ))}
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
            background: 'rgba(0,0,0,0.06)', border: '1px solid #c0bcb4', borderRadius: 6,
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
  const logRef = useRef(null);

  function addLog(msg, type = 'info') {
    setLog(l => [...l, { msg, type }].slice(-200));
    setTimeout(() => { if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight; }, 30);
  }
  function markStep(i, st) { setStepState(s => { const n = [...s]; n[i] = st; return n; }); }

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
    addLog(`Model: ${model || '(unknown)'}  Build: Android ${release}  Codename: ${name || '(unknown)'}  Serial: ${serial || '(unknown)'}`);
    if (!release.startsWith('5.')) {
      throw new Error(`Expected FireOS 5 (Android 5.x), got Android ${release}. Wrong device?`);
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

  async function runPatchBoot(c) {
    addLog('Setting up work directories…');
    await c.shell('mkdir -p /tmp/work /tmp/bin');
    addLog('Extracting magiskboot from /sdcard/f1r30s.zip…');
    const unzipOut = await c.shell('unzip -o /sdcard/f1r30s.zip bin/magiskboot -d /tmp/ 2>&1');
    addLog(unzipOut || '(done)');
    await c.shell('chmod 755 /tmp/bin/magiskboot');

    addLog('Pulling boot image from device (10–20s)…');
    await c.shell('dd if=/dev/block/other-boot of=/tmp/work/boot.img bs=1048576 2>/dev/null');
    const bootImg = await c.pull('/tmp/work/boot.img');
    addLog(`Boot image: ${(bootImg.length / 1024 / 1024).toFixed(1)} MB`);

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

    addLog('Flashing patched boot image…');
    await c.shell('dd if=/tmp/work/new-boot.img of=/dev/block/other-boot bs=1048576 2>/dev/null');
    addLog('Boot image flashed.', 'ok');
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
    const resp = await fetch('/api/provision/magisk_db', { headers: { Authorization: `Bearer ${token}` } });
    if (!resp.ok) throw new Error(`Controller returned ${resp.status}`);
    const dbBytes = new Uint8Array(await resp.arrayBuffer());
    addLog(`magisk.db: ${dbBytes.length} bytes`);
    await c.push('/tmp/magisk_preseed.db', dbBytes);
    await c.shell('cp /tmp/magisk_preseed.db /data/adb/magisk.db && chmod 600 /data/adb/magisk.db');
    addLog('magisk.db installed.', 'ok');
  }

  async function runReboot(c) {
    addLog('Sending reboot command…');
    try { await c.shell('reboot'); } catch {}
    await c.close();
    setAdb(null);
    addLog('Device rebooting to Android. Wait ~60s, then click Reconnect.', 'warn');
    return null;
  }

  async function runReconnect() {
    const c = await _ADB.Client.requestDevice(addLog);
    c._log = msg => addLog(`  adb: ${msg}`);
    setAdb(c);
    addLog('ADB connected.', 'ok');
    return c;
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
    addLog('Waiting for Android framework to finish booting…');
    let bootReady = false;
    for (let i = 0; i < 30; i++) {
      const boot = (await c.shell('getprop sys.boot_completed')).trim();
      if (boot === '1') { bootReady = true; break; }
      await new Promise(r => setTimeout(r, 1000));
    }
    addLog(bootReady ? 'Framework ready.' : 'Timed out waiting for boot_completed — proceeding anyway.', bootReady ? 'ok' : 'warn');

    addLog('Testing su -c id… (magiskd can take a while to attach after boot — retrying if needed)');
    let out = '';
    let rooted = false;
    const attemptStart = Date.now();
    for (let i = 0; i < 15; i++) {
      const callStart = Date.now();
      out = await c.shell('su -c id 2>&1');
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
    // Parse wpa_cli scan_results: bssid / frequency / signal / flags / ssid
    const networks = [];
    for (const line of raw.split('\n')) {
      const parts = line.split('\t');
      if (parts.length < 5) continue;
      const ssid = parts[4].trim();
      if (!ssid || ssid === 'SSID') continue;
      const signal = parseInt(parts[2], 10);
      const existing = networks.find(n => n.ssid === ssid);
      if (!existing) {
        networks.push({ ssid, signal });
      } else if (signal > existing.signal) {
        existing.signal = signal; // keep strongest AP's signal for duplicate SSIDs (multiple APs/bands)
      }
    }
    networks.sort((a, b) => b.signal - a.signal);
    return networks;
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

  async function runConfigWifi(c, ssid, psk) {
    if (!ssid) throw new Error('No SSID selected.');
    wpaConfEscape(ssid);
    wpaConfEscape(psk);

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
      `\tpsk="${psk}"`,
      '\tkey_mgmt=WPA-PSK',
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
    // Diagnostic: confirm the known interferers are actually gone right
    // before we touch wpa_supplicant — if either shows up here despite
    // runDisableAlexa having run, that's the smoking gun for the clobber.
    const interferers = await c.shell("su -c 'ps' | grep -iE 'wifiprofilemanager|SmartHomeWifid'");
    addLog(`Interferer check: ${interferers.trim() || '(none running — clean)'}`);

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
    // readiness signal for the package manager being available — poll for
    // it explicitly rather than guessing a fixed delay.
    addLog('Waiting for Android framework to finish booting…');
    let bootReady = false;
    for (let i = 0; i < 30; i++) {
      const boot = (await c.shell('getprop sys.boot_completed')).trim();
      if (boot === '1') { bootReady = true; break; }
      await new Promise(r => setTimeout(r, 1000));
    }
    addLog(bootReady ? 'Framework ready.' : 'Timed out waiting for boot_completed — proceeding anyway, some pm disable calls may fail.', bootReady ? 'ok' : 'warn');

    for (const pkg of _ALEXA_PKGS) {
      addLog(`Disabling ${pkg}…`);
      const out = await c.shell(`su -c 'pm disable ${pkg}' 2>&1`);
      addLog(`  → ${out || 'ok'}`);
      if (out.includes('Could not access the Package Manager')) {
        // Still not ready despite boot_completed — give it a moment and retry once.
        addLog('  Package Manager not ready yet, waiting 3s and retrying…', 'warn');
        await new Promise(r => setTimeout(r, 3000));
        const retry = await c.shell(`su -c 'pm disable ${pkg}' 2>&1`);
        addLog(`  → retry: ${retry || 'ok'}`);
      }
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
      const resp = await fetch('/api/provision/latest_binary', { headers: { Authorization: `Bearer ${token}` } });
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
    const resp2 = await fetch('/api/provision/start_script', { headers: { Authorization: `Bearer ${token}` } });
    if (!resp2.ok) throw new Error(`Controller returned ${resp2.status}`);
    const script = await resp2.text();
    // Same "Text file busy" risk as wificfg.sh — push + immediate chmod/exec
    // can race with the cat process. start_server.sh isn't executed
    // immediately here (only copied), so push() is safe for this one.
    await c.push('/sdcard/start_server.sh', new TextEncoder().encode(script));
    await c.shell("su -c 'cp /sdcard/start_server.sh /data/local/bin/start_server.sh && chmod 755 /data/local/bin/start_server.sh'");
    addLog('EchoMuse installed.', 'ok');
    addLog('Rebooting device to finish provisioning…');
    try { await c.shell('su -c reboot'); } catch {}
    await c.close();
    setAdb(null);
    addLog('Device rebooting. It will appear in the controller dashboard within ~30s via mDNS.', 'ok');
  }

  // ── Step executor ──
  async function runStep(stepIdx, useLatest) {
    setRunning(true);
    markStep(stepIdx, 'running');
    let c = adb;
    try {
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
        case  9: await runConfigWifi(c, wifiSsid, wifiPsk); break;
        case 10: await runInstallEchoMuse(c, binaryFile, useLatest); break;
      }
      markStep(stepIdx, 'done');
      if (stepIdx < _WIZARD_STEPS.length - 1) setStep(stepIdx + 1);
    } catch (e) {
      addLog(`Error: ${e.message}`, 'error');
      markStep(stepIdx, 'error');
      if (e.matchedDeviceId) setDuplicateDeviceId(e.matchedDeviceId);
      // Clear the file selection on failure — forces a deliberate reselect
      // before retry rather than silently re-flashing whatever was picked
      // last time (which, on a hash-mismatch failure, is the wrong file).
      if (stepIdx === 3) setMagiskFile(null);
      if (stepIdx === 10) setBinaryFile(null);
    }
    setRunning(false);
  }

  // Auto-advance steps that need no user input once adb is connected.
  // Step 8 (disable_alexa) is now before WiFi so Alexa can't phone home.
  useEffect(() => {
    const autoSteps = new Set([2, 4, 7, 8]);
    if (autoSteps.has(step) && !running && stepState[step] === 'pending' && adb) {
      runStep(step);
    }
  }, [step, running]);

  const cur    = _WIZARD_STEPS[step];
  const isDone = step === _WIZARD_STEPS.length - 1 && stepState[step] === 'done';

  // Buttons are shown for manual steps; auto steps start themselves.
  // CONNECT_STEPS: step is a connection step — show "Retry Connection" on error.
  const CONNECT_STEPS = new Set([0, 1, 6]);

  // Dashboard-palette step states — same tones the rest of the UI uses
  // (accent slate for activity, deep green for done, rust for error).
  const statusColors = { pending: 'var(--muted)', running: '#405878', done: '#286040', error: '#a04010' };
  const statusIcons  = { pending: '○', running: '◌', done: '●', error: '✕' };

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
        background: 'linear-gradient(170deg,#e8e4de,#d8d4cc)', border: '1px solid #b8b4ac',
        borderRadius: 16, width: 'min(900px,95vw)', height: 'min(700px,90vh)',
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
        boxShadow: '0 24px 80px rgba(0,0,0,0.3),0 2px 0 rgba(255,255,255,0.8) inset',
        animation: 'fadeIn 0.15s ease',
      }}>

        {/* Header */}
        <div style={{ background: 'linear-gradient(180deg,#dedad2,#ccc8c0)', borderBottom: '1px solid #b0aca4', padding: '20px 24px 16px', boxShadow: '0 1px 0 rgba(255,255,255,0.5) inset', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
            <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 22, fontWeight: 600, color: 'var(--text)', letterSpacing: '-0.02em' }}>Provision Echo Dot</div>
            <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', letterSpacing: '0.12em', textTransform: 'uppercase', marginTop: 4 }}>Chrome/Edge only · USB-A cable · amonet-biscuit prerequisite</div>
          </div>
          <CircleButton onClick={onClose} title="Close">×</CircleButton>
        </div>

        <div style={{ display: 'flex', flex: 1, overflow: 'hidden', minHeight: 0 }}>

          {/* Step list */}
          <div style={{ width: 176, borderRight: '1px solid #b8b4ac', background: 'rgba(0,0,0,0.025)', padding: '12px 0', overflowY: 'auto', flexShrink: 0 }}>
            {_WIZARD_STEPS.map((s, i) => {
              const st = stepState[i]; const active = i === step;
              return (
                <div key={s.id}
                  style={{
                    padding: '6px 14px', display: 'flex', alignItems: 'center', gap: 7,
                    background: active ? 'rgba(0,0,0,0.06)' : 'transparent',
                    cursor: 'default',
                    opacity: running && !active ? 0.5 : 1,
                  }}>
                  <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: statusColors[st], flexShrink: 0 }}>{statusIcons[st]}</span>
                  <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: active ? 'var(--text)' : 'var(--muted)', letterSpacing: '0.04em', lineHeight: 1.4 }}>{s.label}</span>
                </div>
              );
            })}
          </div>

          {/* Content */}
          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', padding: '18px 22px 14px' }}>

            {/* Step title + desc */}
            <div style={{ marginBottom: 14 }}>
              <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 14, fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>{cur.label}</div>
              <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--muted)' }}>{cur.desc}</div>
            </div>

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

            {/* Step 10: EchoMuse binary — custom upload or latest from controller.
                Stays visible through error so a different file/source can be
                tried instead of being stuck retrying whatever failed. */}
            {step === 10 && stepState[10] !== 'done' && !running && (
              <div style={{ marginBottom: 12, display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                  <Pill accent onClick={() => runStep(10, true)}>Install latest from GitHub</Pill>
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
                  {stepState[10] === 'error' ? 'SELECT A DIFFERENT BUILD (ARMv7)' : 'CUSTOM ECHOMUSE SERVER BINARY (ARMv7)'}
                </div>
                <input
                  type="file"
                  onChange={e => setBinaryFile(e.target.files[0])}
                  style={{ fontFamily: "'DM Mono',monospace", fontSize: 11 }}
                />
                {!!binaryFile && <Pill onClick={() => runStep(10, false)}>Install Custom Build</Pill>}
              </div>
            )}

            {/* Step 9: WiFi configuration */}
            {step === 9 && stepState[9] !== 'done' && !running && (
              <WifiPanel
                adb={adb}
                wifiSsid={wifiSsid} setWifiSsid={setWifiSsid}
                wifiPsk={wifiPsk}   setWifiPsk={setWifiPsk}
                onScan={() => scanWifi(adb).then(nets => setWifiNetworks(nets)).catch(e => addLog(`Scan failed: ${e.message}`, 'error'))}
                networks={wifiNetworks}
                onConnect={() => { if (wifiSsid) runStep(9); }}
                onSkip={() => { markStep(9, 'done'); setStep(10); }}
                onAbort={() => { markStep(9, 'error'); addLog('WiFi skipped — provision incomplete.', 'warn'); }}
              />
            )}

            {/* Retry button — re-runs the step directly (runStep marks it running).
                Excludes steps with their own dedicated retry UI above (file
                pickers for 3/10, WifiPanel for 9) — those already give a
                complete retry path with fresh input, so a second generic
                "Retry" here would just compete with it and, for the file
                steps, retry with no file selected (since failure clears it). */}
            {!running && stepState[step] === 'error' && ![3, 9, 10].includes(step) && (
              <div style={{ marginBottom: 10, display: 'flex', gap: 8 }}>
                <Pill onClick={() => runStep(step)}>Retry</Pill>
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
            )}

            {/* Progress bar — accent slate, same as toggles/sliders */}
            {progress && (
              <div style={{ margin: '6px 0 10px' }}>
                <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', marginBottom: 4 }}>{progress.label}</div>
                <div style={{ height: 4, background: '#c8c4bc', borderRadius: 2 }}>
                  <div style={{ height: '100%', width: `${Math.min(100, (progress.pct || 0) * 100).toFixed(0)}%`, background: '#405878', borderRadius: 2, transition: 'width 0.2s' }}/>
                </div>
              </div>
            )}

            {/* Done message */}
            {isDone && (
              <div style={{ margin: '6px 0 10px', display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: '#286040', lineHeight: 1.7 }}>
                  Provisioning complete. The device has rebooted and will discover the controller via mDNS,
                  appearing in the dashboard as a pending device within ~30s.
                </div>
                <div><Pill accent onClick={onClose}>Done</Pill></div>
              </div>
            )}

            {/* Log output — same console treatment as the Updates tab */}
            <div
              ref={logRef}
              style={{
                flex: 1, minHeight: 0, overflowY: 'auto',
                background: 'linear-gradient(160deg,#252820,#1e2219)',
                border: '1px solid #1a1c18', borderRadius: 8,
                boxShadow: 'inset 0 2px 6px rgba(0,0,0,0.5)',
                padding: '10px 14px',
                fontFamily: "'DM Mono',monospace", fontSize: 10, lineHeight: 1.7,
                marginTop: 10,
              }}
            >
              {log.length === 0
                ? <span style={{ color: '#3a4a30' }}>— no output yet —</span>
                : log.map((e, i) => (
                  <div key={i} style={{ color: e.type === 'error' ? '#c08080' : e.type === 'ok' ? '#7ab87a' : e.type === 'warn' ? '#c0a060' : '#a8c8a0' }}>
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
      <text x="0" y="-39" textAnchor="middle" fontSize="15" fill="rgba(255,255,255,0.55)" fontFamily="sans-serif" fontWeight="300">+</text>
      <circle cx="44"  cy="0"   r="15" fill="url(#dcfbg)" stroke="rgba(255,255,255,0.05)" strokeWidth="0.5"/>
      <circle cx="44"  cy="0"   r="4.5" fill="rgba(255,255,255,0.5)"/>
      <circle cx="0"   cy="44"  r="15" fill="url(#dcfbg)" stroke="rgba(255,255,255,0.05)" strokeWidth="0.5"/>
      <text x="0" y="50" textAnchor="middle" fontSize="15" fill="rgba(255,255,255,0.55)" fontFamily="sans-serif" fontWeight="300">−</text>
      <circle cx="-44" cy="0"   r="15" fill="url(#dcfbg)" stroke="rgba(255,255,255,0.05)" strokeWidth="0.5"/>
      <g transform="translate(-44,0)">
        <rect x="-3.5" y="-7.5" width="7" height="10" rx="3.5" fill="rgba(255,255,255,0.5)"/>
        <path d="M-6,1.5 Q-6,8 0,8 Q6,8 6,1.5" fill="none" stroke="rgba(255,255,255,0.5)" strokeWidth="1.5" strokeLinecap="round"/>
        <line x1="0" y1="8" x2="0" y2="11" stroke="rgba(255,255,255,0.5)" strokeWidth="1.5" strokeLinecap="round"/>
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
    device:     { bg: 'rgba(40,96,64,0.12)',  border: 'rgba(40,96,64,0.35)',  text: '#286040' },
    controller: { bg: 'rgba(64,88,120,0.12)', border: 'rgba(64,88,120,0.35)', text: '#405878' },
    scope:      { bg: 'rgba(0,0,0,0.05)',     border: 'rgba(0,0,0,0.15)',     text: 'var(--text2)' },
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
          <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 8, color: g !== 0 ? '#405878' : 'var(--muted)', marginBottom: 2, fontWeight: g !== 0 ? 600 : 400 }}>
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

function Stage({ n, title, chips, desc, children }) {
  return (
    <Panel>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginBottom: 6, flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <span style={{ fontFamily: STAGE_MONO, fontSize: 10, color: 'var(--muted)' }}>{n}</span>
          <span style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 14, fontWeight: 600, color: 'var(--text)' }}>{title}</span>
        </div>
        <div style={{ display: 'flex', gap: 6 }}>{chips}</div>
      </div>
      <div style={{ fontFamily: STAGE_MONO, fontSize: 10, color: 'var(--muted)', lineHeight: 1.6, marginBottom: 14 }}>{desc}</div>
      {children}
    </Panel>
  );
}

function StageAdvanced({ open, onToggle, disabledStyle, children }) {
  return (
    <div style={{ marginTop: 14, borderTop: '1px solid rgba(0,0,0,0.08)', paddingTop: 10 }}>
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

function DeviceConfigForm({ config, onChange, disabled }) {
  const set = disabled ? () => {} : onChange;

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
  const EQ_PRESETS = [['Flat',[0,0,0,0,0,0,0,0]], ['Clarity',[0,0,0,0,0,7,4,2]], ['Warmth',[0,3,2,0,-2,0,0,0]]];
  const activeEqPreset = (EQ_PRESETS.find(([, vals]) => JSON.stringify(vals) === JSON.stringify(bands)) || [null])[0];

  const [advMics, setAdvMics] = useState(false);

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
        desc="Response audio: Home Assistant TTS → parametric EQ → resample → device speaker. Presets set the faders; drag any fader for a custom curve.">
        <div style={{ display: 'grid', gridTemplateColumns: '1.4fr 1fr', gap: 28, alignItems: 'start' }}>
          <div>
            <EqCurve bands={bands}/>
            <EqSliders bands={bands} onChange={nb => set('eqBands', nb)} disabled={disabled}/>
            <div style={{ display: 'flex', gap: 6, marginTop: 12, alignItems: 'center', ...inputStyle }}>
              {EQ_PRESETS.map(([label, vals]) => (
                <Pill key={label} small accent={activeEqPreset === label} onClick={() => set('eqBands', vals)}>{label}</Pill>
              ))}
              {!activeEqPreset && (
                <span style={{ fontFamily: mono, fontSize: 9, color: '#405878', textTransform: 'uppercase', letterSpacing: '0.1em' }}>· Custom</span>
              )}
            </div>
          </div>
          <div>
            <div style={inputStyle}>
              <Toggle label="Speech boost" sub="presence boost for voice" value={config.eqLoudness ?? false} onChange={v => set('eqLoudness', v)}/>
            </div>
            <div style={{ marginTop: 8 }}>
              <div style={{ fontFamily: mono, fontSize: 11, color: 'var(--text2)', marginBottom: 6 }}>Startup volume</div>
              <div style={{ fontFamily: mono, fontSize: 26, color: '#405878', textAlign: 'center', marginBottom: 6, textShadow: '0 0 12px rgba(64,88,120,0.3)', ...inputStyle }}>
                {config.startupVolume ?? 70}%
              </div>
              <div style={inputStyle}>
                <input type="range" min={0} max={100} step={1} value={config.startupVolume ?? 70}
                  style={{ width: '100%' }}
                  onChange={e => set('startupVolume', Number(e.target.value))}/>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 4 }}>
                  <span style={{ fontFamily: mono, fontSize: 9, color: 'var(--muted)' }}>Silent</span>
                  <span style={{ fontFamily: mono, fontSize: 9, color: 'var(--muted)' }}>Full</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </Stage>

      {/* 02 WAKE WORD */}
      <Stage n="02" title="Wake word"
        chips={<ScopeChip tone="controller">Controller</ScopeChip>}
        desc="openwakeword scores the continuous mic stream on the controller. Sensitivity sets the detection threshold — attempts that score close but miss are counted as near-misses (Status tab).">
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24, alignItems: 'start' }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, ...inputStyle }}>
            {WW_MODELS.map(m => (
              <div key={m.value} onClick={() => set('owwModel', m.value)} style={{
                background: config.owwModel === m.value
                  ? 'linear-gradient(160deg,#dde8f4,#ccd8ec)'
                  : 'linear-gradient(160deg,#e4e0d8,#d4d0c8)',
                border: `1px solid ${config.owwModel === m.value ? '#405878' : '#c0bdb6'}`,
                borderRadius: 8, padding: '8px 10px',
                cursor: disabled ? 'default' : 'pointer',
                transition: 'border-color 0.15s, background 0.15s',
              }}>
                <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 12, fontWeight: 600, color: '#1a1c18' }}>{m.label}</div>
                <div style={{ fontFamily: mono, fontSize: 9, color: '#888480', marginTop: 2 }}>{m.value}</div>
              </div>
            ))}
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
              <Slider label="Barge threshold" sub="wake confidence needed during playback — speech-over-TTS scores low, ~0.10 is typical with AEC" value={config.bargeInThreshold ?? 0.10} min={0.05} max={0.9} step={0.05} onChange={v => set('bargeInThreshold', v)}/>
            </div>
          </div>
        </div>
      </Stage>

      {/* 03 MICROPHONES */}
      <Stage n="03" title="Microphones"
        chips={<ScopeChip tone="device">Device</ScopeChip>}
        desc="Capture from the 7-mic array. Presets steer which perimeter mic is used during voice turns — wake-word listening always uses the centre mic. Gain here is the only gain in the wake path: it sets the level everything downstream hears.">
        <div style={{ display: 'grid', gridTemplateColumns: 'auto 1fr', gap: 20, alignItems: 'center' }}>
          <DeviceDiagram
            activeMics={PRESETS[currentPreset].activeMics}
            patternType={PRESETS[currentPreset].patternType}
          />
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 6, ...inputStyle }}>
            {Object.entries(PRESETS).map(([key, p]) => (
              <div key={key} onClick={() => selectPreset(key)} style={{
                background: currentPreset === key
                  ? 'linear-gradient(160deg,#dde8f4,#ccd8ec)'
                  : 'linear-gradient(160deg,#e4e0d8,#d4d0c8)',
                border: `1px solid ${currentPreset === key ? '#405878' : '#c0bdb6'}`,
                borderRadius: 10, padding: '9px 6px 8px',
                cursor: disabled ? 'default' : 'pointer',
                display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6,
                transition: 'border-color 0.15s, background 0.15s',
              }}>
                <DeviceDiagramMini activeMics={p.activeMics} patternType={p.patternType}/>
                <div style={{ fontFamily: mono, fontSize: 10, color: '#3a3830' }}>
                  {key.charAt(0).toUpperCase() + key.slice(1)}
                </div>
              </div>
            ))}
          </div>
        </div>
        <StageAdvanced open={advMics} onToggle={() => setAdvMics(o => !o)} disabledStyle={inputStyle}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '4px 24px' }}>
            <Slider label="MICPGA" sub="analog gain, before the ADC" value={config.adcMicpga ?? 40} min={0} max={59} onChange={v => set('adcMicpga', v)}/>
            <Slider label="Digital gain" sub="ADC digital gain — affects wake + turns" value={config.adcDigitalGain ?? 88} min={0} max={100} onChange={v => set('adcDigitalGain', v)}/>
            <Slider label="Mic gain" sub="fixed gain on the 24-bit capture, pre-16-bit stream" value={config.micGainDb ?? 24} min={0} max={42} unit="dB" onChange={v => set('micGainDb', v)}/>
            <Slider label="Beam angle" sub="-1 = auto (onset-ratio selection)" value={config.beamAngle ?? -1} min={-1} max={359} step={1} onChange={v => set('beamAngle', v)}/>
            <Toggle label="Beamforming" sub="perimeter mic lock during turns" value={config.beamformingEnabled ?? false} onChange={v => set('beamformingEnabled', v)}/>
            <Toggle label="Echo cancel (AEC)" sub="subtracts the device's own playback — wake + turns" value={config.aecEnabled ?? false} onChange={v => set('aecEnabled', v)}/>
            <Toggle label="Noise suppression" sub="DTLN denoise on speech-to-text audio only — helps fans/hum, not TV speech" value={config.nsAsr ?? false} onChange={v => set('nsAsr', v)}/>
            <Slider label="AEC delay" sub="playback write-to-ear latency compensation" value={config.aecDelayMs ?? 250} min={0} max={1000} step={10} unit="ms" onChange={v => set('aecDelayMs', v)}/>
            <Slider label="AEC tail" sub="filter length — residual delay error + room reverb" value={config.aecTailMs ?? 300} min={50} max={500} step={10} unit="ms" onChange={v => set('aecTailMs', v)}/>
          </div>
        </StageAdvanced>
      </Stage>

      {/* 04 RING */}
      <Stage n="04" title="Ring"
        chips={<ScopeChip tone="controller">Controller</ScopeChip>}
        desc="Colours for the LED ring during conversations — the solid listening ring and the thinking spinner. The red mute ring and cyan volume arc never change; red always means the mics are off.">
        <div style={{ display: 'grid', gridTemplateColumns: '1.4fr 1fr', gap: 24, alignItems: 'start' }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, ...inputStyle }}>
            {RING_SCENES.map(sc => (
              <div key={sc.value} onClick={() => set('ledScene', sc.value)} style={{
                background: (config.ledScene ?? 'standard') === sc.value
                  ? 'linear-gradient(160deg,#dde8f4,#ccd8ec)'
                  : 'linear-gradient(160deg,#e4e0d8,#d4d0c8)',
                border: `1px solid ${(config.ledScene ?? 'standard') === sc.value ? '#405878' : '#c0bdb6'}`,
                borderRadius: 8, padding: '8px 10px',
                cursor: disabled ? 'default' : 'pointer',
                transition: 'border-color 0.15s, background 0.15s',
              }}>
                <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 12, fontWeight: 600, color: '#1a1c18' }}>{sc.label}</div>
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
                  style={{ width: 36, height: 28, padding: 0, border: '1px solid #b8b4ac', borderRadius: 6, background: 'none', cursor: 'pointer' }}/>
                <div>
                  <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 12, fontWeight: 600 }}>Listening</div>
                  <div style={{ fontFamily: mono, fontSize: 9, color: '#888480' }}>solid ring while recording</div>
                </div>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <input type="color" value={config.ledThinkColor ?? '#00c800'} disabled={disabled}
                  onChange={e => set('ledThinkColor', e.target.value)}
                  style={{ width: 36, height: 28, padding: 0, border: '1px solid #b8b4ac', borderRadius: 6, background: 'none', cursor: 'pointer' }}/>
                <div>
                  <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 12, fontWeight: 600 }}>Thinking</div>
                  <div style={{ fontFamily: mono, fontSize: 9, color: '#888480' }}>spinner while processing</div>
                </div>
              </div>
            </div>
          )}
        </div>
      </Stage>

      {/* 05 ADVANCED — button-turn internals: processing + speech gate */}
      <Stage n="05" title="Advanced"
        chips={<><ScopeChip tone="device">Device</ScopeChip><ScopeChip>Button turns only</ScopeChip></>}
        desc="Everything here affects only bounded button-press turns. Wake-word turns stream continuously — Home Assistant's VAD endpoints them, and the controller closes accidental wakes after 5s of silence relative to the room's measured noise floor — so none of these settings touch the wake path.">
        {subHeader('Turn processing', true)}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 24px', ...inputStyle }}>
          <Toggle label="Auto gain (AGC)" sub="levels button-turn speech; never the wake stream" value={config.agcEnabled ?? true} onChange={v => set('agcEnabled', v)}/>
        </div>
        {subHeader('Speech gate')}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '4px 20px', ...inputStyle }}>
          <Slider label="Threshold" sub="RMS above this = speech (pre-gain units)" value={config.vadThreshold ?? 0.001} min={0.0001} max={0.02} step={0.0001} onChange={v => set('vadThreshold', v)}/>
          <Slider label="Speech gate" sub="speech needed to open" value={config.vadSpeechMs ?? 160} min={32} max={320} step={32} unit="ms" onChange={v => set('vadSpeechMs', v)}/>
          <Slider label="Silence gate" sub="silence needed to close" value={config.vadSilenceMs ?? 800} min={200} max={2000} step={100} unit="ms" onChange={v => set('vadSilenceMs', v)}/>
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

function DeployAllModal({ release, devices, onClose }) {
  const mono = "'DM Mono',monospace";
  const [result, setResult]   = useState(null); // {version, started, skipped}
  const [running, setRunning] = useState(false);
  const [error, setError]     = useState('');

  const target = result?.version || release?.version;
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
                                         return { text: '✓ updated',    color: '#286040' };
    if (!d.connected)                    return { text: 'rebooting…',   color: '#96660a' };
    return { text: 'updating…', color: '#405878' };
  }

  async function deploy() {
    setRunning(true); setError('');
    try {
      setResult(await API.post('/api/releases/deploy', {}));
    } catch (e) {
      setError(e.error || 'Deploy failed');
    }
    setRunning(false);
  }

  const label = d => d?.label || d?.device_id || '?';

  return (
    <div onClick={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(30,28,24,0.45)', zIndex: 60, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div onClick={e => e.stopPropagation()} style={{ background: 'linear-gradient(170deg,#e8e4de,#d8d4cc)', border: '1px solid #b8b4ac', borderRadius: 14, padding: '28px 32px', width: 440, maxWidth: '92vw', boxShadow: '0 24px 80px rgba(0,0,0,0.3)' }}>
        <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 16, fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>
          Deploy to fleet
        </div>
        <div style={{ fontFamily: mono, fontSize: 10, color: 'var(--muted)', marginBottom: 18 }}>
          Target: {release?.version || '—'} · devices update over WiFi and auto-roll-back on failure
        </div>

        {!result ? (
          <>
            <div style={{ fontFamily: mono, fontSize: 11, color: 'var(--text2)', marginBottom: 16, lineHeight: 1.8 }}>
              {eligible.length === 0
                ? 'Every connected device is already on this version.'
                : <>Will update <b>{eligible.length}</b> device{eligible.length === 1 ? '' : 's'}:{' '}
                    {eligible.map(d => `${label(d)} (${d.firmware_ver || '?'})`).join(', ')}</>}
            </div>
            {error && <div style={{ fontFamily: mono, fontSize: 11, color: '#c03030', marginBottom: 12 }}>{error}</div>}
            <div style={{ display: 'flex', gap: 10 }}>
              <Pill accent disabled={running || eligible.length === 0} onClick={deploy}>
                {running ? 'Starting…' : `Deploy ${release?.version || ''}`}
              </Pill>
              <Pill onClick={onClose}>Cancel</Pill>
            </div>
          </>
        ) : (
          <>
            {(result.started || []).map(id => {
              const s = statusFor(id);
              return (
                <div key={id} style={{ display: 'flex', justifyContent: 'space-between', fontFamily: mono, fontSize: 11, padding: '5px 0', borderBottom: '1px solid rgba(0,0,0,0.06)' }}>
                  <span style={{ color: 'var(--text2)' }}>{label(byId[id])}</span>
                  <span style={{ color: s.color }}>{s.text} {byId[id]?.firmware_ver ? `· ${byId[id].firmware_ver}` : ''}</span>
                </div>
              );
            })}
            {(result.skipped || []).map(s => (
              <div key={s.device_id} style={{ display: 'flex', justifyContent: 'space-between', fontFamily: mono, fontSize: 11, padding: '5px 0', borderBottom: '1px solid rgba(0,0,0,0.06)' }}>
                <span style={{ color: 'var(--muted)' }}>{label(byId[s.device_id])}</span>
                <span style={{ color: 'var(--muted)' }}>skipped — {SKIP_REASONS[s.reason] || s.reason}</span>
              </div>
            ))}
            {(result.started || []).length === 0 && (result.skipped || []).length === 0 && (
              <div style={{ fontFamily: mono, fontSize: 11, color: 'var(--muted)' }}>Nothing to do.</div>
            )}
            <div style={{ marginTop: 18 }}>
              <Pill onClick={onClose}>Close</Pill>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ─── SettingsPanel ─────────────────────────────────────────────────────────────
// Gear icon → modal with two tabs: Fleet Config and Account.

function SettingsPanel({ globalConfig, onGlobalConfigChange, onClose, username }) {
  const [tab, setTab]             = useState('fleet');
  const [config, setConfig]       = useState({ ...globalConfig });
  const [dirty, setDirty]         = useState(false);
  const [saving, setSaving]       = useState(false);

  const [curPw, setCurPw]         = useState('');
  const [newPw, setNewPw]         = useState('');
  const [confirmPw, setConfirmPw] = useState('');
  const [pwSaving, setPwSaving]   = useState(false);
  const [pwMsg, setPwMsg]         = useState(null); // {ok, text}

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

  const TABS = ['fleet', 'account'];
  const TAB_LABELS = { fleet: 'Fleet Config', account: 'Account' };

  return (
    <div style={{ position:'fixed', inset:0, background:'rgba(180,176,168,0.5)', display:'flex', alignItems:'center', justifyContent:'center', zIndex:200, backdropFilter:'blur(8px)' }}
      onClick={e => e.target === e.currentTarget && onClose()}>
      {/* Same fixed frame as the device Detail modal — consistent window
          size across the whole dashboard. */}
      <div style={{ width:'min(900px,95vw)', height:'min(700px,90vh)', background:'linear-gradient(170deg,#e8e4de,#d8d4cc)', border:'1px solid #b8b4ac', borderRadius:16, boxShadow:'0 24px 80px rgba(0,0,0,0.3),0 2px 0 rgba(255,255,255,0.8) inset', display:'flex', flexDirection:'column', overflow:'hidden', animation:'fadeIn 0.15s ease' }}>

        {/* Header */}
        <div style={{ background:'linear-gradient(180deg,#dedad2,#ccc8c0)', borderBottom:'1px solid #b0aca4', padding:'20px 24px 0', boxShadow:'0 1px 0 rgba(255,255,255,0.5) inset' }}>
          <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom:16 }}>
            <div style={{ fontFamily:"'DM Sans',sans-serif", fontSize:22, color:'var(--text)', fontWeight:600, letterSpacing:'-0.02em' }}>Settings</div>
            <CircleButton onClick={onClose} title="Close">×</CircleButton>
          </div>
          {/* Same raised folder-tab treatment as the device Detail modal —
              one tab style across the dashboard. */}
          <div style={{ display:'flex', gap:2 }}>
            {TABS.map(t => (
              <button key={t} onClick={() => setTab(t)} style={{ background: tab === t ? 'linear-gradient(180deg,#e8e4de,#d8d4cc)' : 'transparent', border: tab === t ? '1px solid #b0aca4' : '1px solid transparent', borderBottom: tab === t ? '1px solid #d8d4cc' : '1px solid transparent', borderRadius: '6px 6px 0 0', fontFamily: "'DM Mono',monospace", fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.1em', padding: '7px 14px', cursor: 'pointer', color: tab === t ? 'var(--text)' : 'var(--muted)', marginBottom: -1, transition: 'color 0.15s' }}>{TAB_LABELS[t]}</button>
            ))}
          </div>
        </div>

        {/* Body */}
        <div style={{ overflowY:'auto', padding:'24px 28px 32px', flex:1 }}>

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
                  color: saveMsg.ok ? '#286040' : '#c03030' }}>
                  {saveMsg.ok ? '✓ ' : ''}{saveMsg.text}
                </div>
              )}
            </>
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
                <div style={{ fontFamily:"'DM Mono',monospace", fontSize:11, color: pwMsg.ok ? '#286040' : '#b03030', marginBottom:12 }}>
                  {pwMsg.text}
                </div>
              )}
              <Pill accent disabled={pwSaving || !curPw || !newPw || !confirmPw} onClick={changePassword}>
                {pwSaving ? 'Updating…' : 'Update password'}
              </Pill>
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
  const [devices, setDevices] = useState([]);
  const [selected, setSelected] = useState(null);
  const [release, setRelease] = useState(null);
  const [checkingRelease, setCheckingRelease] = useState(false);
  const [status, setStatus] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [showWizard, setShowWizard] = useState(false);
  const [showDeployAll, setShowDeployAll] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [globalConfig, setGlobalConfig] = useState(null);
  const wsRef = useRef(null);

  const isAdmin = role === 'admin';

  function handleLogout() {
    API.post('/api/auth/logout', {}).catch(() => {});
    API.token = null;
    localStorage.removeItem('em_token');
    localStorage.removeItem('em_role');
    // The landing page (/) owns sign-in — green-ring login form.
    location.replace('/');
  }

  // Restore token on mount
  useEffect(() => { if (token) API.token = token; }, []);

  // Load initial data
  useEffect(() => {
    if (!token) return;
    Promise.all([
      API.get('/api/devices'),
      API.get('/api/system/status'),
      API.get('/api/releases/latest').catch(() => null),
      API.get('/api/global/config').catch(() => null),
    ]).then(([devs, stat, rel, gcfg]) => {
      setDevices(devs);
      setStatus(stat);
      setRelease(rel);
      if (gcfg) setGlobalConfig(gcfg);
    }).catch(e => {
      if (e.code === 'not_authenticated') { handleLogout(); }
      else setLoadError(e.error || 'Failed to load');
    });
  }, [token]);

  // Live events WebSocket
  useEffect(() => {
    if (!token) return;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/api/events?token=${token}`);
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

    // Polling fallback — catches anything the WebSocket misses
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
  if (!token) { location.replace('/'); return null; }

  const online   = devices.filter(d => d.connected).length;
  const approved = devices.filter(d => d.approved);
  const pending  = devices.filter(d => !d.approved);
  const updates  = approved.filter(d => d.firmware_ver && release?.version && d.firmware_ver !== release.version).length;
  const active   = approved.filter(d => d.speaking || d.listening || d.thinking).length;

  const selectedDevice = selected ? devices.find(d => d.device_id === selected) : null;

  return (
    <div style={{ minHeight: '100vh', padding: '32px 36px 60px' }}>

      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end', marginBottom: 36 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 14 }}>
          <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 28, color: 'var(--text)', fontWeight: 600, letterSpacing: '-0.02em' }}>EchoMuse</div>
          <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--muted)', letterSpacing: '0.12em', textTransform: 'uppercase' }}>Device Management</div>
          {status?.controller_version && (
            <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--muted)' }}>{status.controller_version}</div>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--muted)' }}>{role}</div>
          <Pill small onClick={() => setShowSettings(true)}>
            <span style={{ fontSize: 14, verticalAlign: '-1px', marginRight: 6 }}>⚙</span>Settings
          </Pill>
          <Pill small onClick={handleLogout}>Sign out</Pill>
        </div>
      </div>

      {/* Summary */}
      <div style={{ display: 'flex', gap: 10, marginBottom: 36 }}>
        {[
          ['Online', `${online}/${approved.length}`, online === approved.length ? '#286040' : '#806010'],
          ['Active', active, active > 0 ? '#2060b0' : 'var(--muted)'],
          ['Updates', updates, updates > 0 ? '#806010' : 'var(--muted)'],
          ['Pending', pending.length, pending.length > 0 ? '#6080a8' : 'var(--muted)'],
        ].map(([label, val, c]) => (
          <div key={label} style={{ background: 'linear-gradient(160deg,#2a2e28,#1e2219)', border: '1px solid #1a1c18', borderRadius: 8, padding: '12px 18px', flex: 1, boxShadow: 'inset 0 2px 6px rgba(0,0,0,0.5)' }}>
            <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 8, color: 'var(--lcd-dim)', textTransform: 'uppercase', letterSpacing: '0.15em', marginBottom: 6 }}>{label}</div>
            <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 24, color: c, lineHeight: 1, textShadow: `0 0 12px ${c}66` }}>{val}</div>
          </div>
        ))}
        {release && (
          <div style={{ background: 'linear-gradient(160deg,#2a2e28,#1e2219)', border: '1px solid #1a1c18', borderRadius: 8, padding: '12px 18px', flex: 2, boxShadow: 'inset 0 2px 6px rgba(0,0,0,0.5)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <div>
              <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 8, color: 'var(--lcd-dim)', textTransform: 'uppercase', letterSpacing: '0.15em', marginBottom: 6 }}>Latest Release</div>
              <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 18, color: 'var(--lcd-green)', lineHeight: 1 }}>{release.version}</div>
            </div>
            {isAdmin && (
              <Pill small accent={!checkingRelease} disabled={checkingRelease} onClick={async () => {
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
              }}>
                {checkingRelease ? 'Checking…' : 'Check for updates'}
              </Pill>
            )}
            {isAdmin && release && (
              <Pill small accent onClick={() => setShowDeployAll(true)}>Deploy all</Pill>
            )}
          </div>
        )}
      </div>

      {/* Pending devices */}
      {pending.length > 0 && (
        <>
          <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: '#6080a8', textTransform: 'uppercase', letterSpacing: '0.15em', marginBottom: 14 }}>
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
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(190px,1fr))', gap: 12, marginBottom: 48 }}>
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
        <div style={{ textAlign: 'center', padding: '60px 0', fontFamily: "'DM Mono',monospace", fontSize: 12, color: '#c03030' }}>{loadError}</div>
      )}

      {/* Provisioning wizard */}
      {showWizard && (
        <ProvisionWizard token={token} onClose={() => setShowWizard(false)} knownDevices={devices}/>
      )}

      {/* Fleet-wide OTA */}
      {showDeployAll && (
        <DeployAllModal release={release} devices={devices} onClose={() => setShowDeployAll(false)}/>
      )}

      {/* Settings panel */}
      {showSettings && globalConfig && (
        <SettingsPanel
          globalConfig={globalConfig}
          onGlobalConfigChange={setGlobalConfig}
          onClose={() => setShowSettings(false)}
          username={role}
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
