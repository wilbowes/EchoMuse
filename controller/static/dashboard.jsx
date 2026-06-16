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
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
      <div>
        <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: 'var(--text2)' }}>{label}</span>
        {sub && <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--muted)', marginLeft: 8 }}>{sub}</span>}
      </div>
      <div onClick={() => onChange(!value)} style={{
        width: 36, height: 20, borderRadius: 10, cursor: 'pointer', position: 'relative',
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

function Shell({ deviceId, token }) {
  const [lines, setLines] = useState([{ type: 'sys', text: `shell — ${deviceId}` }]);
  const [input, setInput] = useState('');
  const [ws, setWs] = useState(null);
  const endRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const sock = new WebSocket(`${proto}://${location.host}/api/devices/${deviceId}/shell?token=${token}`);
    sock.binaryType = 'arraybuffer';
    sock.onopen = () => setLines(l => [...l, { type: 'sys', text: 'connected' }]);
    sock.onmessage = e => {
      const text = typeof e.data === 'string' ? e.data : new TextDecoder().decode(e.data);
      setLines(l => [...l, { type: 'out', text }]);
    };
    sock.onclose = () => setLines(l => [...l, { type: 'sys', text: 'disconnected' }]);
    sock.onerror = () => setLines(l => [...l, { type: 'err', text: 'connection error' }]);
    setWs(sock);
    return () => sock.close();
  }, [deviceId]);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [lines]);

  const send = () => {
    if (!input.trim() || !ws || ws.readyState !== 1) return;
    setLines(l => [...l, { type: 'in', text: input }]);
    ws.send(input + '\n');
    setInput('');
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter') {
      send();
    } else if (e.key === 'c' && e.ctrlKey) {
      e.preventDefault();
      if (ws && ws.readyState === 1) {
        ws.send('\x03');
        setLines(l => [...l, { type: 'in', text: '^C' }]);
      }
    }
  };

  const lineColor = t => ({ sys: '#2a3020', in: '#c8d4b0', out: '#8aaa70', err: '#c04040' }[t] || '#8aaa70');

  return (
    <div style={{ background: 'linear-gradient(160deg,#252820,#1c1f18)', border: '1px solid #1a1c16', borderRadius: 6, boxShadow: 'inset 0 2px 6px rgba(0,0,0,0.6)', padding: 16, height: 320, display: 'flex', flexDirection: 'column', fontFamily: "'DM Mono',monospace", fontSize: 12 }}
      onClick={() => inputRef.current?.focus()}>
      <div style={{ flex: 1, overflowY: 'auto', paddingBottom: 8 }}>
        {lines.map((line, i) => (
          <div key={i} style={{ marginBottom: 2, lineHeight: 1.65 }}>
            {line.type === 'in' && <span style={{ color: '#6a9a50' }}>% </span>}
            <span style={{ color: lineColor(line.type), whiteSpace: 'pre-wrap' }}>{line.text}</span>
          </div>
        ))}
        <div ref={endRef}/>
      </div>
      <div style={{ display: 'flex', gap: 8, borderTop: '1px solid #1e2218', paddingTop: 10, alignItems: 'center' }}>
        <span style={{ color: '#6a9a50' }}>%</span>
        <input ref={inputRef} value={input} onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="enter command..."
          style={{ flex: 1, background: 'transparent', border: 'none', outline: 'none', color: '#c8d4b0', fontFamily: "'DM Mono',monospace", fontSize: 12, caretColor: '#9aba80' }}
          autoFocus/>
      </div>
    </div>
  );
}

// ─── Device detail modal ──────────────────────────────────────────────────────

function Detail({ device, token, onClose, onApprove, isAdmin }) {
  const [tab, setTab] = useState('status');
  const [config, setConfig] = useState({ ...device.config });
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [logs, setLogs] = useState([]);
  const [logsLoading, setLogsLoading] = useState(false);
  const [pushLog, setPushLog] = useState([]);
  const [pushing, setPushing] = useState(false);
  const [release, setRelease] = useState(null);
  const [approveLabel, setApproveLabel] = useState(device.label || '');
  const [approving, setApproving] = useState(false);
  const [localFile, setLocalFile] = useState(null);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef(null);
  const state = deviceState(device);
  const needsUpdate = device.firmware_ver && release?.version && device.firmware_ver !== release.version;

  const TABS = device.approved
    ? (isAdmin ? ['status', 'config', 'console', 'updates', 'logs'] : ['status', 'config', 'logs'])
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

  function setConf(k, v) { setConfig(c => ({ ...c, [k]: v })); setDirty(true); }

  async function pushConfig() {
    setSaving(true);
    try {
      await API.post(`/api/devices/${device.device_id}/config`, config);
      setDirty(false);
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

  const row = (k, v, c) => (
    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0', borderBottom: '1px solid rgba(0,0,0,0.06)' }}>
      <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 12, color: 'var(--muted)' }}>{k}</span>
      <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 12, color: c || 'var(--text)', fontWeight: 600 }}>{v}</span>
    </div>
  );

  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(180,176,168,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100, backdropFilter: 'blur(8px)' }}
      onClick={e => e.target === e.currentTarget && onClose()}>
      <div style={{ width: 'min(900px,95vw)', maxHeight: '90vh', background: 'linear-gradient(170deg,#e8e4de,#d8d4cc)', border: '1px solid #b8b4ac', borderRadius: 16, boxShadow: '0 24px 80px rgba(0,0,0,0.3),0 2px 0 rgba(255,255,255,0.8) inset', display: 'flex', flexDirection: 'column', overflow: 'hidden', animation: 'fadeIn 0.15s ease' }}>
        {/* Header */}
        <div style={{ background: 'linear-gradient(180deg,#dedad2,#ccc8c0)', borderBottom: '1px solid #b0aca4', padding: '20px 24px 0', boxShadow: '0 1px 0 rgba(255,255,255,0.5) inset' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 20, marginBottom: 16 }}>
            <LedRing state={state} size={72}/>
            <div style={{ flex: 1 }}>
              <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 26, color: 'var(--text)', fontWeight: 600, letterSpacing: '-0.02em', lineHeight: 1 }}>
                {device.label || <span style={{ color: 'var(--muted)', fontSize: 20 }}>{device.device_id.slice(0,8)}…</span>}
              </div>
              <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--muted)', marginTop: 4, letterSpacing: '0.05em' }}>
                {device.ip} · {device.device_id} · {device.firmware_ver || 'unknown'}
                {needsUpdate && <span style={{ color: '#806010', marginLeft: 10 }}>Update available</span>}
              </div>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <div style={{ background: 'linear-gradient(160deg,#2a2e28,#1c1f18)', border: '1px solid #1a1c16', borderRadius: 6, padding: '5px 12px', boxShadow: 'inset 0 1px 3px rgba(0,0,0,0.5)' }}>
                <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: state.dot, textShadow: `0 0 8px ${state.dot}88`, letterSpacing: '0.05em' }}>{state.label.toUpperCase()}</span>
              </div>
              <button onClick={onClose} style={{ background: 'linear-gradient(180deg,#d0ccc4,#bab6ae)', border: '1px solid #a0a098', borderRadius: '50%', width: 28, height: 28, display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer', boxShadow: '0 1px 0 rgba(255,255,255,0.5) inset', color: '#5a5650', fontSize: 16, fontWeight: 300 }}>×</button>
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
              {row('IP', device.ip)}
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
            return (
              <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:24 }}>
                <div>
                  <div style={{ fontFamily:"'DM Mono',monospace", fontSize:9, color:'var(--muted)', textTransform:'uppercase', letterSpacing:'0.15em', marginBottom:12 }}>Device</div>
                  {row('IP', device.ip || '—')}
                  {row('Firmware', device.firmware_ver || '—')}
                  {row('Last seen', relTime(device.last_seen))}
                  {row('Connected', device.connected ? 'Yes' : 'No', device.connected ? '#286040' : '#c0601a')}
                </div>
                <div>
                  <div style={{ fontFamily:"'DM Mono',monospace", fontSize:9, color:'var(--muted)', textTransform:'uppercase', letterSpacing:'0.15em', marginBottom:14 }}>Resources</div>
                  <StatBar label="CPU"     pct={s?.cpuPct}    text={cpuText}/>
                  <StatBar label="RAM"     pct={ramPct}        text={ramText}/>
                  <StatBar label="Storage" pct={stoPct}        text={stoText}/>
                  <div style={{ marginBottom:13 }}>
                    <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:5 }}>
                      <span style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--muted)', textTransform:'uppercase', letterSpacing:'0.08em' }}>WiFi</span>
                      <div style={{ display:'flex', alignItems:'center', gap:8 }}>
                        <span style={{ fontFamily:"'DM Mono',monospace", fontSize:10, color:'var(--text2)' }}>{s?.wifiRssi != null ? `${s.wifiRssi} dBm` : '—'}</span>
                        <SignalBars rssi={s?.wifiRssi ?? null}/>
                      </div>
                    </div>
                  </div>
                  {!s && <div style={{ fontFamily:"'DM Mono',monospace", fontSize:9, color:'var(--muted)', marginTop:4 }}>waiting for device stats…</div>}
                </div>
              </div>
            );
          })()}

          {/* CONFIG */}
          {tab === 'config' && (
            <div style={{ maxWidth: 440 }}>
              <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.15em', marginBottom: 20 }}>Microphone · 7-mic array</div>
              <Slider label="Digital Gain" sub="ctl 89–143 all ADCs" value={config.adcDigitalGain} min={0} max={100} onChange={v => setConf('adcDigitalGain', v)}/>
              <Slider label="MICPGA" sub="ctl 92–146 all ADCs" value={config.adcMicpga} min={0} max={100} onChange={v => setConf('adcMicpga', v)}/>
              <Slider label="VAD Threshold" sub="RMS" value={config.vadThreshold} min={0.001} max={0.02} step={0.001} onChange={v => setConf('vadThreshold', v)}/>
              <Slider label="VAD Speech Ms" sub="min speech to open gate" value={config.vadSpeechMs} min={32} max={320} step={32} onChange={v => setConf('vadSpeechMs', v)}/>
              <Slider label="VAD Silence Ms" sub="silence to close gate" value={config.vadSilenceMs} min={200} max={2000} step={100} onChange={v => setConf('vadSilenceMs', v)}/>
              <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.15em', margin: '28px 0 20px' }}>Beamforming · Delay and Sum</div>
              <Toggle label="Enabled" sub="7-mic delay-and-sum" value={config.beamformingEnabled ?? true} onChange={v => setConf('beamformingEnabled', v)}/>
              <Slider label="Beam Angle" sub="-1 = auto" value={config.beamAngle ?? -1} min={-1} max={359} step={1} onChange={v => setConf('beamAngle', v)}/>
              <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.15em', margin: '28px 0 20px' }}>Speaker · TLV320 · card 0 dev 23</div>
              <Slider label="Startup Volume" sub="ctl 61" value={config.startupVolume} min={0} max={100} onChange={v => setConf('startupVolume', v)}/>
              <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.15em', margin: '28px 0 12px' }}>EQ · 8-Band · controller-side</div>
              {(() => {
                const EQ_FREQS = ['125 Hz','250 Hz','500 Hz','1 kHz','2 kHz','3.5 kHz','5.5 kHz','8 kHz'];
                const EQ_DESCS = ['shelf','','','','','','','shelf'];
                const bands = config.eqBands ?? [0,0,0,0,0,0,0,0];
                const fmtDb = v => (v >= 0 ? '+' : '') + Number(v).toFixed(1) + ' dB';
                const setEqBand = (i, v) => { const b=[...bands]; b[i]=v; setConf('eqBands',b); };
                const LEFT  = [0,1,2,3];
                const RIGHT = [4,5,6,7];
                return (
                  <div>
                    <EqCurve bands={bands}/>
                    <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:'0 20px' }}>
                      <div>{LEFT.map(i => (
                        <Slider key={i} label={EQ_FREQS[i]} sub={EQ_DESCS[i]}
                          value={bands[i]??0} min={-12} max={12} step={0.5}
                          formatValue={fmtDb} onChange={v => setEqBand(i,v)}/>
                      ))}</div>
                      <div>{RIGHT.map(i => (
                        <Slider key={i} label={EQ_FREQS[i]} sub={EQ_DESCS[i]}
                          value={bands[i]??0} min={-12} max={12} step={0.5}
                          formatValue={fmtDb} onChange={v => setEqBand(i,v)}/>
                      ))}</div>
                    </div>
                    <div style={{ display:'flex', gap:8, marginBottom:20, marginTop:4 }}>
                      <Pill small onClick={() => setConf('eqBands',[0,0,0,0,0,0,0,0])}>Flat</Pill>
                      <Pill small onClick={() => setConf('eqBands',[0,0,0,0,0,7,4,2])}>Clarity</Pill>
                      <Pill small onClick={() => setConf('eqBands',[0,3,2,0,-2,0,0,0])}>Warmth</Pill>
                    </div>
                    <Toggle label="Loudness" sub="speech-range presence boost" value={config.eqLoudness??false} onChange={v=>setConf('eqLoudness',v)}/>
                  </div>
                );
              })()}
              <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.15em', margin: '28px 0 20px' }}>Wake Word · OpenWakeWord</div>
              <Slider label="Detection Threshold" value={config.owwThreshold} min={0.1} max={0.9} step={0.05} onChange={v => setConf('owwThreshold', v)}/>
              <div style={{ marginBottom: 24 }}>
                <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: 'var(--text2)', marginBottom: 8 }}>Model</div>
                <select value={config.owwModel} onChange={e => setConf('owwModel', e.target.value)}>
                  <option value="hey_jarvis_v0.1">hey_jarvis_v0.1</option>
                  <option value="alexa_v0.1">alexa_v0.1</option>
                  <option value="hey_mycroft_v0.1">hey_mycroft_v0.1</option>
                  <option value="hey_rhasspy_v0.1">hey_rhasspy_v0.1</option>
                </select>
              </div>
              {isAdmin && dirty && (
                <div style={{ display: 'flex', gap: 10, marginTop: 24 }}>
                  <Pill accent disabled={saving} onClick={pushConfig}>{saving ? 'Pushing…' : 'Push config'}</Pill>
                  <Pill onClick={() => { setConfig({ ...device.config }); setDirty(false); }}>Revert</Pill>
                </div>
              )}
            </div>
          )}

          {/* CONSOLE */}
          {tab === 'console' && (
            device.connected
              ? <Shell deviceId={device.device_id} token={token}/>
              : <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 12, color: '#c0601a' }}>Device offline — console unavailable</div>
          )}

          {/* UPDATES */}
          {tab === 'updates' && (
            <div style={{ maxWidth: 440 }}>
              {/* Version LCDs */}
              <div style={{ display:'flex', gap:16, marginBottom:28 }}>
                <Lcd label="On device"  value={device.firmware_ver || '—'} color={needsUpdate ? 'var(--lcd-amber)' : 'var(--lcd-green)'}/>
                <Lcd label="Available"  value={release?.version || '—'} color="var(--lcd-dim)"/>
                {device.firmware_previous && (
                  <Lcd label="Rollback slot" value={device.firmware_previous} color="var(--lcd-dim)"/>
                )}
              </div>

              {/* GitHub release deploy */}
              <div style={{ fontFamily:"'DM Mono',monospace", fontSize:9, color:'var(--muted)', textTransform:'uppercase', letterSpacing:'0.15em', marginBottom:12 }}>GitHub Release</div>
              <div style={{ display:'flex', gap:10, marginBottom:24 }}>
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

              {/* Local binary deploy */}
              <div style={{ fontFamily:"'DM Mono',monospace", fontSize:9, color:'var(--muted)', textTransform:'uppercase', letterSpacing:'0.15em', marginBottom:12 }}>Local Build</div>
              <input ref={fileInputRef} type="file" accept="*/*" style={{ display:'none' }}
                onChange={e => setLocalFile(e.target.files[0] || null)}/>
              <div style={{ display:'flex', gap:10, alignItems:'center', marginBottom:24, flexWrap:'wrap' }}>
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

              {/* Activity log */}
              {pushLog.length > 0 && (
                <div style={{ background:'linear-gradient(160deg,#252820,#1e2219)', border:'1px solid #1a1c18', borderRadius:6, padding:14, fontFamily:"'DM Mono',monospace", fontSize:12, boxShadow:'inset 0 2px 6px rgba(0,0,0,0.5)' }}>
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
              )}
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
          <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--lcd-dim)', letterSpacing: '0.08em' }}>{device.ip || '—'}</span>
        </div>
      </div>
    </div>
  );
}

// ─── Login screen ─────────────────────────────────────────────────────────────

function Login({ onLogin }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function submit() {
    if (!username || !password) return;
    setLoading(true); setError('');
    try {
      const data = await API.post('/api/auth/login', { username, password });
      onLogin(data.token, data.role);
    } catch(e) {
      setError(e.error || 'Login failed');
    }
    setLoading(false);
  }

  return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div style={{ background: 'linear-gradient(170deg,#e8e4de,#d8d4cc)', border: '1px solid #b8b4ac', borderRadius: 16, padding: '48px 56px', maxWidth: 360, width: '90vw', boxShadow: '0 24px 80px rgba(0,0,0,0.2),0 2px 0 rgba(255,255,255,0.7) inset', animation: 'fadeIn 0.2s ease' }}>
        <div style={{ textAlign: 'center', marginBottom: 36 }}>
          <LedRing state={{ key: 'idle', dot: '#aaaaaa' }} size={80}/>
          <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 22, fontWeight: 600, color: 'var(--text)', letterSpacing: '-0.02em', marginTop: 20 }}>EchoMuse</div>
          <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', letterSpacing: '0.15em', textTransform: 'uppercase', marginTop: 4 }}>Device Management</div>
        </div>
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--text2)', marginBottom: 6, letterSpacing: '0.05em' }}>Username</div>
          <input type="text" value={username} onChange={e => setUsername(e.target.value)} onKeyDown={e => e.key === 'Enter' && submit()} autoFocus/>
        </div>
        <div style={{ marginBottom: 24 }}>
          <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--text2)', marginBottom: 6, letterSpacing: '0.05em' }}>Password</div>
          <input type="password" value={password} onChange={e => setPassword(e.target.value)} onKeyDown={e => e.key === 'Enter' && submit()}/>
        </div>
        {error && <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: '#c03030', marginBottom: 16, textAlign: 'center' }}>{error}</div>}
        <Pill accent disabled={loading || !username || !password} onClick={submit}>
          <span style={{ display: 'block', textAlign: 'center', width: '100%' }}>{loading ? 'Signing in…' : 'Sign in'}</span>
        </Pill>
      </div>
    </div>
  );
}

// ─── App ──────────────────────────────────────────────────────────────────────

function App() {
  const [token, setToken] = useState(() => sessionStorage.getItem('em_token'));
  const [role, setRole] = useState(() => sessionStorage.getItem('em_role'));
  const [devices, setDevices] = useState([]);
  const [selected, setSelected] = useState(null);
  const [release, setRelease] = useState(null);
  const [status, setStatus] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const wsRef = useRef(null);

  const isAdmin = role === 'admin';

  function handleLogin(tok, rol) {
    API.token = tok;
    setToken(tok);
    setRole(rol);
    sessionStorage.setItem('em_token', tok);
    sessionStorage.setItem('em_role', rol);
  }

  function handleLogout() {
    API.post('/api/auth/logout', {}).catch(() => {});
    API.token = null;
    setToken(null); setRole(null);
    sessionStorage.removeItem('em_token');
    sessionStorage.removeItem('em_role');
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
    ]).then(([devs, stat, rel]) => {
      setDevices(devs);
      setStatus(stat);
      setRelease(rel);
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

  if (!token) return <Login onLogin={handleLogin}/>;

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
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 10, color: 'var(--muted)' }}>{role}</div>
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
            {isAdmin && updates > 0 && (
              <Pill small accent onClick={async () => {
                try {
                  await API.post('/api/releases/deploy', {});
                } catch(e) { alert(e.error || 'Deploy failed'); }
              }}>Deploy all</Pill>
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
      {approved.length > 0 && (
        <>
          <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.15em', marginBottom: 14 }}>
            Devices · {approved.length}
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(190px,1fr))', gap: 12, marginBottom: 48 }}>
            {approved.map(d => <Card key={d.device_id} device={d} onClick={() => setSelected(d.device_id)}/>)}
          </div>
        </>
      )}

      {devices.length === 0 && !loadError && (
        <div style={{ textAlign: 'center', padding: '60px 0', fontFamily: "'DM Mono',monospace", fontSize: 12, color: 'var(--muted)' }}>
          No devices yet — power on an EchoMuse device to see it appear here
        </div>
      )}

      {loadError && (
        <div style={{ textAlign: 'center', padding: '60px 0', fontFamily: "'DM Mono',monospace", fontSize: 12, color: '#c03030' }}>{loadError}</div>
      )}

      {/* Detail modal */}
      {selectedDevice && (
        <Detail
          device={selectedDevice}
          token={token}
          onClose={() => setSelected(null)}
          onApprove={() => API.get('/api/devices').then(setDevices).catch(() => {})}
          isAdmin={isAdmin}
        />
      )}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
