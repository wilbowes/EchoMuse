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

// ─── Provisioning Wizard ──────────────────────────────────────────────────────

// ADB-over-WebUSB client — no external dependencies.
// Implements: CNXN/AUTH(RSA)/OPEN/OKAY/WRTE/CLSE + exec: streams.
const _ADB = (() => {
  const CMD = {
    CNXN: 0x4e584e43, AUTH: 0x48545541, OPEN: 0x4e45504f,
    OKAY: 0x59414b4f, CLSE: 0x45534c43, WRTE: 0x45545257,
  };

  // ── CRC32 ──
  const _T = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let j = 0; j < 8; j++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    _T[i] = c;
  }
  function crc32(d) {
    let c = 0xFFFFFFFF;
    for (const b of d) c = (c >>> 8) ^ _T[(c ^ b) & 0xFF];
    return (c ^ 0xFFFFFFFF) >>> 0;
  }

  // ── BigInt RSA helpers ──
  function b64uToBigInt(s) {
    const b64 = s.replace(/-/g, '+').replace(/_/g, '/');
    const bin = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
    let v = 0n;
    for (const b of bin) v = (v << 8n) | BigInt(b);
    return v;
  }
  function bytesBigInt(arr) {
    let v = 0n;
    for (const b of arr) v = (v << 8n) | BigInt(b);
    return v;
  }
  function bigIntBytes(v, len) {
    const out = new Uint8Array(len);
    for (let i = len - 1; i >= 0 && v > 0n; i--, v >>= 8n) out[i] = Number(v & 0xFFn);
    return out;
  }
  function modPow(b, e, m) {
    let r = 1n; b %= m;
    for (; e > 0n; e >>= 1n) { if (e & 1n) r = r * b % m; b = b * b % m; }
    return r;
  }

  // ── RSA key (generated once, stored in localStorage) ──
  const _LS = 'em_adb_key';
  async function getKey() {
    const s = localStorage.getItem(_LS);
    if (s) return JSON.parse(s);
    const kp = await crypto.subtle.generateKey(
      { name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-1' },
      true, ['sign']
    );
    const jwk = await crypto.subtle.exportKey('jwk', kp.privateKey);
    localStorage.setItem(_LS, JSON.stringify(jwk));
    return jwk;
  }

  // ADB AUTH: PKCS#1 v1.5 with SHA-1 DigestInfo; token is the raw digest.
  // Web Crypto always pre-hashes, so we implement the RSA private op with BigInt.
  async function signToken(jwk, token) {
    const n = b64uToBigInt(jwk.n), d = b64uToBigInt(jwk.d);
    const DI = new Uint8Array([0x30,0x21,0x30,0x09,0x06,0x05,0x2b,0x0e,0x03,0x02,0x1a,0x05,0x00,0x04,0x14]);
    const T = new Uint8Array(DI.length + token.length);
    T.set(DI); T.set(token, DI.length);
    const k = 256;
    const EM = new Uint8Array(k);
    EM[1] = 0x01;
    EM.fill(0xFF, 2, k - T.length - 1);
    EM.set(T, k - T.length);
    return bigIntBytes(modPow(bytesBigInt(EM), d, n), k);
  }

  // ── AdbStream ──
  class Stream {
    constructor(client, localId) {
      this._c = client; this.localId = localId; this.remoteId = 0;
      this._q = []; this._closed = false;
      this._openRes = null; this._openRej = null;
      this._dataNote = null; this._writeNote = null;
    }
    _onOkay(rid) {
      if (!this.remoteId) {
        this.remoteId = rid; this._openRes?.(); this._openRes = this._openRej = null;
      } else {
        const n = this._writeNote; this._writeNote = null; n?.();
      }
    }
    _onData(data) {
      this._q.push(data);
      const n = this._dataNote; this._dataNote = null; n?.();
      this._c._sendMsg(CMD.OKAY, this.localId, this.remoteId);
    }
    _onClose() {
      this._closed = true;
      this._dataNote?.(); this._dataNote = null;
      this._writeNote?.(); this._writeNote = null;
      this._c._streams.delete(this.localId);
    }
    async _next() {
      if (this._q.length) return this._q.shift();
      if (this._closed) return null;
      return new Promise(r => { this._dataNote = () => r(this._q.length ? this._q.shift() : null); });
    }
    async readAll() {
      const parts = [];
      for (;;) { const c = await this._next(); if (!c) break; parts.push(c); }
      let len = 0; for (const p of parts) len += p.length;
      const out = new Uint8Array(len); let off = 0;
      for (const p of parts) { out.set(p, off); off += p.length; }
      return out;
    }
    async write(data) {
      const SZ = 64 * 1024;
      for (let i = 0; i < data.length; i += SZ) {
        const sl = data.subarray(i, i + SZ);
        await new Promise(r => { this._writeNote = r; this._c._sendMsg(CMD.WRTE, this.localId, this.remoteId, sl); });
      }
    }
    close() { if (!this._closed) this._c._sendMsg(CMD.CLSE, this.localId, this.remoteId); }
  }

  // ── AdbClient ──
  class Client {
    constructor() {
      this._dev = null; this._epIn = null; this._epOut = null; this._iface = null;
      this._streams = new Map(); this._nextId = 1; this._running = false;
      this._connRes = null; this._connRej = null;
    }

    static async requestDevice() {
      if (!navigator.usb) {
        throw new Error(
          'WebUSB not available. This requires a secure context (HTTPS or localhost). ' +
          'Access the dashboard via http://localhost:8768, or enable chrome://flags/#unsafely-treat-insecure-origin-as-secure for this origin.'
        );
      }
      const dev = await navigator.usb.requestDevice({
        filters: [{ classCode: 0xFF, subclassCode: 0x42, protocolCode: 0x01 }],
      });
      const c = new Client(); c._dev = dev; return c;
    }

    async connect(timeoutMs = 20000) {
      const dev = this._dev;
      await dev.open();
      const dbgIfaces = [];
      for (const cfg of dev.configurations) {
        for (const iface of cfg.interfaces) {
          for (const alt of iface.alternates) {
            const epStr = alt.endpoints.map(e =>
              `${e.direction[0].toUpperCase()}${e.endpointNumber}(${e.type})`).join(',');
            dbgIfaces.push(
              `cfg=${cfg.configurationValue} if=${iface.interfaceNumber} ` +
              `cls=${alt.interfaceClass}/${alt.interfaceSubclass}/${alt.interfaceProtocol} [${epStr}]`
            );
            if (alt.interfaceClass !== 0xFF || alt.interfaceSubclass !== 0x42 || alt.interfaceProtocol !== 0x01) continue;
            this._iface = iface.interfaceNumber;
            for (const ep of alt.endpoints) {
              if (ep.type === 'bulk') {
                if (ep.direction === 'in') this._epIn = ep.endpointNumber;
                else this._epOut = ep.endpointNumber;
              }
            }
          }
        }
      }
      this._dbgIfaces = dbgIfaces;
      if (this._iface === null) throw new Error(`No ADB interface on device. Interfaces: ${dbgIfaces.join(' | ')}`);
      await dev.selectConfiguration(1);
      try {
        await dev.claimInterface(this._iface);
      } catch (e) {
        throw new Error(
          'Could not claim USB interface — the ADB daemon on this machine has already claimed it. ' +
          'Run: adb kill-server  — then try connecting again.'
        );
      }
      // Clear any endpoint stall left by the previous ADB session.
      try { await dev.clearHalt('in',  this._epIn);  } catch {}
      try { await dev.clearHalt('out', this._epOut); } catch {}
      // Brief pause — adbd needs a moment to re-initialise its USB stack after
      // we claimed the interface away from the previous host.
      await new Promise(r => setTimeout(r, 300));
      // Send CNXN before the read loop so adbd has the host banner before
      // we issue any IN tokens — some implementations open the IN endpoint
      // only after receiving CNXN.
      await this._sendMsg(CMD.CNXN, 0x01000000, 1 << 20, new TextEncoder().encode('host::EchoMuse\0'));
      this._running = true;
      this._readLoop();
      return new Promise((res, rej) => {
        this._connRes = res; this._connRej = rej;
        setTimeout(() => rej(new Error('ADB connect timeout — no response from device')), timeoutMs);
      });
    }

    _readLoop() {
      (async () => {
        while (this._running) {
          try {
            // Use a large buffer: some ADB gadget drivers combine the 24-byte
            // header and data payload into a single USB bulk packet. Requesting
            // only 24 bytes causes a transfer error if the packet is larger.
            const hr = await this._dev.transferIn(this._epIn, 65536);
            if (!hr.data || hr.data.byteLength < 24) continue;
            const v = new DataView(hr.data.buffer, hr.data.byteOffset);
            const cmd = v.getUint32(0, true), arg0 = v.getUint32(4, true);
            const arg1 = v.getUint32(8, true), len = v.getUint32(12, true);
            let data = new Uint8Array(0);
            if (len > 0) {
              const inlined = hr.data.byteLength - 24;
              if (inlined >= len) {
                // Header and data arrived in the same USB packet
                data = new Uint8Array(hr.data.buffer, hr.data.byteOffset + 24, len);
              } else {
                // Data arrives in subsequent packets
                const buf = new Uint8Array(len);
                if (inlined > 0) buf.set(new Uint8Array(hr.data.buffer, hr.data.byteOffset + 24, inlined));
                let off = inlined;
                while (off < len) {
                  const r = await this._dev.transferIn(this._epIn, Math.min(65536, len - off));
                  const c = new Uint8Array(r.data.buffer, r.data.byteOffset, r.data.byteLength);
                  buf.set(c, off); off += c.length;
                }
                data = buf;
              }
            }
            this._dispatch(cmd, arg0, arg1, data);
          } catch (e) {
            if (this._running) {
              this._connRej?.(new Error(e.message || String(e)));
              this._connRej = null;
            }
            break;
          }
        }
      })();
    }

    _dispatch(cmd, arg0, arg1, data) {
      switch (cmd) {
        case CMD.CNXN: this._connRes?.(); this._connRes = this._connRej = null; break;
        case CMD.AUTH:
          if (arg0 === 1) getKey().then(jwk => signToken(jwk, data)).then(sig => this._sendMsg(CMD.AUTH, 2, 0, sig)).catch(e => this._connRej?.(e));
          break;
        case CMD.OKAY: this._streams.get(arg1)?._onOkay(arg0); break;
        case CMD.WRTE: this._streams.get(arg1)?._onData(data); break;
        case CMD.CLSE: this._streams.get(arg1)?._onClose(); break;
      }
    }

    async _open(svc) {
      const id = this._nextId++; const s = new Stream(this, id); this._streams.set(id, s);
      await this._sendMsg(CMD.OPEN, id, 0, new TextEncoder().encode(svc + '\0'));
      await new Promise((res, rej) => {
        s._openRes = res; s._openRej = rej;
        setTimeout(() => rej(new Error(`Stream open timeout: ${svc}`)), 15000);
      });
      return s;
    }

    async shell(cmd) {
      const s = await this._open(`shell:${cmd}`);
      return new TextDecoder().decode(await s.readAll()).replace(/\r\n/g, '\n').trim();
    }

    async exec(cmd) {
      const s = await this._open(`exec:${cmd}`);
      return new TextDecoder().decode(await s.readAll()).trim();
    }

    async push(remotePath, data, onProgress) {
      const bytes = data instanceof Uint8Array ? data : new Uint8Array(data);
      const s = await this._open(`exec:cat > '${remotePath}'`);
      const SZ = 64 * 1024;
      for (let i = 0; i < bytes.length; i += SZ) {
        await s.write(bytes.subarray(i, i + SZ));
        onProgress?.((i + SZ) / bytes.length);
      }
      s.close(); onProgress?.(1);
      await new Promise(r => setTimeout(r, 300));
    }

    async pull(remotePath) {
      const s = await this._open(`exec:cat '${remotePath}'`);
      return s.readAll();
    }

    async _sendMsg(cmd, arg0 = 0, arg1 = 0, data = new Uint8Array(0)) {
      const d = data instanceof Uint8Array ? data : new Uint8Array(data);
      const h = new ArrayBuffer(24); const v = new DataView(h);
      v.setUint32(0, cmd, true); v.setUint32(4, arg0, true); v.setUint32(8, arg1, true);
      v.setUint32(12, d.length, true); v.setUint32(16, crc32(d), true);
      v.setUint32(20, (cmd ^ 0xFFFFFFFF) >>> 0, true);
      await this._dev.transferOut(this._epOut, h);
      if (d.length > 0) await this._dev.transferOut(this._epOut, d.buffer.slice(d.byteOffset, d.byteOffset + d.byteLength));
    }

    async close() {
      this._running = false;
      try { await this._dev?.releaseInterface(this._iface); } catch {}
      try { await this._dev?.close(); } catch {}
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

// Steps:
//  0  connect_android  — connect in Android mode, verify FireOS 5, reboot to recovery
//  1  connect_twrp     — reconnect once TWRP menu appears
//  2  patch_boot       — SELinux cmdline + init.rc in one boot image pass  [auto]
//  3  install_magisk   — flash Magisk 17.3 via twrp install                [file]
//  4  preseed_db       — push pre-seeded magisk.db                         [auto]
//  5  reboot           — reboot device to Android                          [button]
//  6  reconnect        — reconnect ADB after Android boots                 [button]
//  7  verify_root      — confirm su works                                  [auto]
//  8  wifi             — configure WiFi network                            [inputs]
//  9  disable_alexa    — pm disable x9                                     [auto]
// 10  install_em       — push binary + startup script                      [file]
const _WIZARD_STEPS = [
  { id: 'connect_android', label: 'Connect Device',     desc: 'Connect the Echo Dot via USB. Device should be on and booted into Android.' },
  { id: 'connect_twrp',    label: 'Connect to TWRP',   desc: 'Wait for TWRP recovery to appear, then reconnect.' },
  { id: 'patch_boot',      label: 'Patch Boot Image',  desc: 'Apply SELinux permissive patch and add init.rc service entries.' },
  { id: 'install_magisk',  label: 'Install Magisk',    desc: 'Flash Magisk 17.3 for persistent root access.' },
  { id: 'preseed_db',      label: 'Pre-seed Root DB',  desc: 'Grant root to ADB shell without a screen prompt.' },
  { id: 'reboot',          label: 'Reboot to Android', desc: 'Reboot device to Android.' },
  { id: 'reconnect',       label: 'Reconnect',         desc: 'Re-connect ADB after Android finishes booting.' },
  { id: 'verify_root',     label: 'Verify Root',       desc: 'Confirm Magisk root is working.' },
  { id: 'wifi',            label: 'Configure WiFi',    desc: 'Connect the device to your local WiFi network.' },
  { id: 'disable_alexa',   label: 'Disable Alexa',     desc: 'Disable all 9 Alexa voice pipeline packages.' },
  { id: 'install_em',      label: 'Install EchoMuse',  desc: 'Push server binary and startup script to device.' },
];

function ProvisionWizard({ token, onClose }) {
  const [step, setStep]         = useState(0);
  const [stepState, setStepState] = useState(_WIZARD_STEPS.map(() => 'pending'));
  const [log, setLog]           = useState([]);
  const [running, setRunning]   = useState(false);
  const [adb, setAdb]           = useState(null);
  const [magiskFile, setMagiskFile] = useState(null);
  const [binaryFile, setBinaryFile] = useState(null);
  const [wifiSsid, setWifiSsid] = useState('');
  const [wifiPsk, setWifiPsk]   = useState('');
  const [progress, setProgress] = useState(null);
  const logRef = useRef(null);

  function addLog(msg, type = 'info') {
    setLog(l => [...l, { msg, type }].slice(-200));
    setTimeout(() => { if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight; }, 30);
  }
  function markStep(i, st) { setStepState(s => { const n = [...s]; n[i] = st; return n; }); }

  // ── Step runners ──

  async function runConnectAndroid() {
    addLog('Requesting USB device — select the Echo Dot from the picker…');
    const c = await _ADB.Client.requestDevice();
    addLog('Connecting ADB…');
    try {
      await c.connect();
    } catch (err) {
      (c._dbgIfaces || []).forEach(l => addLog(`  USB: ${l}`));
      throw err;
    }
    (c._dbgIfaces || []).forEach(l => addLog(`  USB: ${l}`));
    setAdb(c);
    const model   = await c.shell('getprop ro.product.model');
    const release = await c.shell('getprop ro.build.version.release');
    const name    = await c.shell('getprop ro.product.name');
    addLog(`Model: ${model || '(unknown)'}  Build: Android ${release}  Codename: ${name || '(unknown)'}`);
    if (!release.startsWith('5.')) {
      throw new Error(`Expected FireOS 5 (Android 5.x), got Android ${release}. Wrong device?`);
    }
    if (model && !model.toLowerCase().includes('amazon') && !name.toLowerCase().includes('biscuit')) {
      addLog('Warning: device may not be an Echo Dot 2nd gen — proceeding anyway.', 'warn');
    }
    addLog('FireOS 5 confirmed. Rebooting to TWRP recovery…');
    try { await c.shell('reboot recovery'); } catch {}
    await c.close();
    setAdb(null);
    addLog('Device is rebooting. Wait for the TWRP menu to appear, then click "Connect to TWRP".', 'warn');
    return null;
  }

  async function runConnectTwrp() {
    addLog('Requesting USB device…');
    const c = await _ADB.Client.requestDevice();
    addLog('Connecting ADB…');
    await c.connect();
    setAdb(c);
    addLog('Verifying TWRP…');
    const bootmode = await c.shell('getprop ro.bootmode 2>/dev/null || echo recovery');
    const hasTwrp  = await c.shell('ls /sbin/recovery 2>/dev/null && echo YES || echo NO');
    if (!bootmode.includes('recovery') && hasTwrp.trim() !== 'YES') {
      throw new Error('Device does not appear to be in TWRP. Is the TWRP menu showing on the device?');
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

    addLog('Patching cmdline for SELinux permissive…');
    const patched = new Uint8Array(bootImg);
    const newCmd  = new TextEncoder().encode('bootopt=64S3,32N2,64N2 androidboot.selinux=permissive');
    patched.fill(0, 64, 576);
    patched.set(newCmd, 64);

    addLog('Pushing patched image…');
    await c.push('/tmp/work/boot_patched.img', patched, pct => setProgress({ label: 'Pushing boot image', pct }));
    setProgress(null);

    addLog('Unpacking ramdisk…');
    const unpackOut = await c.shell('cd /tmp/work && /tmp/bin/magiskboot unpack boot_patched.img 2>&1');
    addLog(unpackOut || '(done)');
    await c.shell('mkdir -p /tmp/ramdisk && cd /tmp/ramdisk && cpio -id < /tmp/work/ramdisk.cpio 2>/dev/null');

    addLog('Patching init.csm.project.rc…');
    const rcBytes  = await c.pull('/tmp/ramdisk/init.csm.project.rc');
    const existing = new TextDecoder().decode(rcBytes);
    if (existing.includes('service echomuse')) {
      addLog('Service entries already present — skipping.', 'warn');
    } else {
      await c.push('/tmp/ramdisk/init.csm.project.rc', new TextEncoder().encode(existing + _INIT_RC_APPEND));
      await c.shell('chmod 750 /tmp/ramdisk/init.csm.project.rc');
    }

    addLog('Repacking ramdisk…');
    await c.shell('cd /tmp/ramdisk && find . | cpio -o -H newc > /tmp/work/ramdisk.cpio 2>/dev/null');
    const repackOut = await c.shell('cd /tmp/work && /tmp/bin/magiskboot repack boot_patched.img 2>&1');
    addLog(repackOut || '(done)');

    addLog('Flashing patched boot image…');
    await c.shell('dd if=/tmp/work/new-boot.img of=/dev/block/other-boot bs=1048576 2>/dev/null');
    addLog('Boot image flashed.', 'ok');
  }

  async function runInstallMagisk(c, file) {
    addLog(`Pushing ${file.name} to /sdcard/…`);
    const buf = await file.arrayBuffer();
    await c.push('/sdcard/Magisk-v17.3.zip', new Uint8Array(buf),
      pct => setProgress({ label: 'Uploading Magisk', pct }));
    setProgress(null);
    addLog('Installing via TWRP (this takes ~30s)…');
    const out = await c.shell('twrp install /sdcard/Magisk-v17.3.zip 2>&1');
    addLog(out || '(done)');
    if (out.toLowerCase().includes('error') || out.toLowerCase().includes('failed')) {
      throw new Error('TWRP install reported an error — check the log.');
    }
    addLog('Magisk installed.', 'ok');
  }

  async function runPreseedDb(c) {
    addLog('Downloading magisk.db from controller…');
    const resp = await fetch('/api/provision/magisk_db', { headers: { Authorization: `Bearer ${token}` } });
    if (!resp.ok) throw new Error(`Controller returned ${resp.status}`);
    const dbBytes = new Uint8Array(await resp.arrayBuffer());
    addLog(`magisk.db: ${dbBytes.length} bytes`);
    await c.shell('mkdir -p /data/adb');
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
    addLog('Requesting USB device…');
    const c = await _ADB.Client.requestDevice();
    await c.connect();
    setAdb(c);
    addLog('ADB connected.', 'ok');
    return c;
  }

  async function runVerifyRoot(c) {
    addLog('Testing su -c id…');
    const out = await c.shell('su -c id 2>&1');
    addLog(out);
    if (!out.includes('uid=0')) throw new Error('Root not working — check Magisk install and magisk.db.');
    addLog('Root confirmed.', 'ok');
  }

  async function runConfigWifi(c, ssid, psk) {
    addLog('Enabling WiFi radio…');
    await c.shell("su -c 'svc wifi enable' 2>&1");
    await new Promise(r => setTimeout(r, 2000));

    addLog('Checking existing connectivity…');
    const existIp = await c.shell("su -c \"ip addr show wlan0 2>/dev/null | grep 'inet ' | awk '{print $2}' | cut -d/ -f1\"");
    if (existIp && /\d+\.\d+\.\d+\.\d+/.test(existIp)) {
      addLog(`Already connected (${existIp}) — skipping network add.`, 'ok');
      return;
    }

    addLog(`Adding network "${ssid}"…`);
    // Write a helper script to avoid shell quoting complexity
    const script = `#!/system/bin/sh\nnetid=$(wpa_cli add_network | tail -1)\nwpa_cli set_network "$netid" ssid '"${ssid}"'\nwpa_cli set_network "$netid" psk '"${psk}"'\nwpa_cli enable_network "$netid"\nwpa_cli reconnect\nwpa_cli save_config\necho "NETID:$netid"`;
    await c.push('/tmp/wificfg.sh', new TextEncoder().encode(script));
    const out = await c.shell('su -c "chmod 755 /tmp/wificfg.sh && /tmp/wificfg.sh" 2>&1');
    addLog(out || '(done)');

    addLog('Waiting for IP address (up to 20s)…');
    for (let i = 0; i < 20; i++) {
      await new Promise(r => setTimeout(r, 1000));
      const ip = await c.shell("su -c \"ip addr show wlan0 2>/dev/null | grep 'inet ' | awk '{print $2}' | cut -d/ -f1\"");
      if (ip && /\d+\.\d+\.\d+\.\d+/.test(ip)) {
        addLog(`Connected! IP: ${ip}`, 'ok');
        return;
      }
    }
    throw new Error(`WiFi did not associate within 20s. Check SSID "${ssid}" and password.`);
  }

  async function runDisableAlexa(c) {
    for (const pkg of _ALEXA_PKGS) {
      addLog(`Disabling ${pkg}…`);
      const out = await c.shell(`su -c 'pm disable ${pkg}' 2>&1`);
      addLog(`  → ${out || 'ok'}`);
    }
    addLog('Alexa stack disabled.', 'ok');
  }

  async function runInstallEchoMuse(c, file) {
    addLog(`Pushing ${file.name} to /sdcard/server_new…`);
    const buf = await file.arrayBuffer();
    await c.push('/sdcard/server_new', new Uint8Array(buf),
      pct => setProgress({ label: 'Uploading binary', pct }));
    setProgress(null);
    addLog('Installing to /data/local/bin/ (A slot)…');
    await c.shell("su -c 'mkdir -p /data/local/bin && cp /sdcard/server_new /data/local/bin/server_a && chmod 755 /data/local/bin/server_a && ln -sf server_a /data/local/bin/server'");
    addLog('Fetching startup script from controller…');
    const resp = await fetch('/api/provision/start_script', { headers: { Authorization: `Bearer ${token}` } });
    if (!resp.ok) throw new Error(`Controller returned ${resp.status}`);
    await c.push('/sdcard/start_server.sh', new TextEncoder().encode(await resp.text()));
    await c.shell("su -c 'cp /sdcard/start_server.sh /data/local/bin/start_server.sh && chmod 755 /data/local/bin/start_server.sh'");
    addLog('EchoMuse installed.', 'ok');
  }

  // ── Step executor ──
  async function runStep(stepIdx) {
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
        case  8: await runConfigWifi(c, wifiSsid, wifiPsk); break;
        case  9: await runDisableAlexa(c); break;
        case 10: await runInstallEchoMuse(c, binaryFile); break;
      }
      markStep(stepIdx, 'done');
      if (stepIdx < _WIZARD_STEPS.length - 1) setStep(stepIdx + 1);
    } catch (e) {
      addLog(`Error: ${e.message}`, 'error');
      markStep(stepIdx, 'error');
    }
    setRunning(false);
  }

  // Auto-advance steps that need no user input once adb is connected
  useEffect(() => {
    const autoSteps = new Set([2, 4, 7, 9]);
    if (autoSteps.has(step) && !running && stepState[step] === 'pending' && adb) {
      runStep(step);
    }
  }, [step, running]);

  const cur    = _WIZARD_STEPS[step];
  const isDone = step === _WIZARD_STEPS.length - 1 && stepState[step] === 'done';

  // Buttons are shown for manual steps; auto steps start themselves.
  // CONNECT_STEPS: step is a connection step — show "Retry Connection" on error.
  const CONNECT_STEPS = new Set([0, 1, 6]);
  // FILE_STEPS: step needs a file before running.
  const FILE_STEP_BUTTON = step === 3 && !!magiskFile ? 'Flash Magisk'
                         : step === 10 && !!binaryFile ? 'Install EchoMuse'
                         : null;

  const statusColors = { pending: '#888', running: '#8ab0d0', done: '#7ab87a', error: '#c05050' };
  const statusIcons  = { pending: '○', running: '◌', done: '●', error: '✕' };

  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 200,
      background: 'rgba(20,18,14,0.82)', display: 'flex', alignItems: 'center', justifyContent: 'center',
      backdropFilter: 'blur(4px)',
    }}>
      <div style={{
        background: 'linear-gradient(160deg,#e8e4de,#d8d4cc)', border: '1px solid #b8b4ac',
        borderRadius: 16, width: '92vw', maxWidth: 860, maxHeight: '90vh',
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
        boxShadow: '0 32px 96px rgba(0,0,0,0.3)',
      }}>

        {/* Header */}
        <div style={{ padding: '20px 24px 16px', borderBottom: '1px solid #c8c4bc', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
            <div style={{ fontFamily: "'DM Sans',sans-serif", fontSize: 16, fontWeight: 600, color: 'var(--text)', letterSpacing: '-0.01em' }}>Provision Echo Dot</div>
            <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', letterSpacing: '0.12em', textTransform: 'uppercase', marginTop: 2 }}>Chrome/Edge only · USB-A cable · amonet-biscuit prerequisite</div>
          </div>
          <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer', fontSize: 18, color: 'var(--muted)', padding: '4px 8px', lineHeight: 1 }}>✕</button>
        </div>

        <div style={{ display: 'flex', flex: 1, overflow: 'hidden', minHeight: 0 }}>

          {/* Step list */}
          <div style={{ width: 176, borderRight: '1px solid #c8c4bc', padding: '12px 0', overflowY: 'auto', flexShrink: 0 }}>
            {_WIZARD_STEPS.map((s, i) => {
              const st = stepState[i]; const active = i === step;
              return (
                <div key={s.id} style={{ padding: '6px 14px', display: 'flex', alignItems: 'center', gap: 7, background: active ? 'rgba(0,0,0,0.06)' : 'transparent' }}>
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

            {/* Steps 3 and 10: file pickers */}
            {(step === 3 || step === 10) && stepState[step] === 'pending' && (
              <div style={{ marginBottom: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
                <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--text2)', letterSpacing: '0.08em' }}>
                  {step === 3 ? 'MAGISK-V17.3.ZIP' : 'ECHOMUSE SERVER BINARY (ARMv7)'}
                </div>
                <input
                  type="file" accept={step === 3 ? '.zip' : undefined}
                  onChange={e => step === 3 ? setMagiskFile(e.target.files[0]) : setBinaryFile(e.target.files[0])}
                  style={{ fontFamily: "'DM Mono',monospace", fontSize: 11 }}
                />
                {FILE_STEP_BUTTON && <Pill onClick={() => runStep(step)}>{FILE_STEP_BUTTON}</Pill>}
              </div>
            )}

            {/* Step 8: WiFi configuration */}
            {step === 8 && stepState[8] === 'pending' && !running && (
              <div style={{ marginBottom: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
                <div style={{ display: 'flex', gap: 10, alignItems: 'flex-end' }}>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--text2)', letterSpacing: '0.08em', marginBottom: 5 }}>SSID</div>
                    <input type="text" value={wifiSsid} onChange={e => setWifiSsid(e.target.value)} placeholder="Network name" style={{ width: '100%', boxSizing: 'border-box' }}/>
                  </div>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--text2)', letterSpacing: '0.08em', marginBottom: 5 }}>PASSWORD</div>
                    <input type="password" value={wifiPsk} onChange={e => setWifiPsk(e.target.value)} placeholder="WPA password" style={{ width: '100%', boxSizing: 'border-box' }}/>
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 8 }}>
                  <Pill onClick={() => { if (wifiSsid) runStep(8); }} disabled={!wifiSsid}>Configure WiFi</Pill>
                  <Pill onClick={() => { markStep(8, 'done'); setStep(9); }} small>Skip (already connected)</Pill>
                </div>
              </div>
            )}

            {/* Retry button — re-runs the step directly (runStep marks it running) */}
            {!running && stepState[step] === 'error' && (
              <div style={{ marginBottom: 10 }}>
                <Pill onClick={() => runStep(step)}>Retry</Pill>
              </div>
            )}

            {/* Progress bar */}
            {progress && (
              <div style={{ margin: '6px 0 10px' }}>
                <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', marginBottom: 4 }}>{progress.label}</div>
                <div style={{ height: 4, background: '#c8c4bc', borderRadius: 2 }}>
                  <div style={{ height: '100%', width: `${Math.min(100, (progress.pct || 0) * 100).toFixed(0)}%`, background: '#7ab87a', borderRadius: 2, transition: 'width 0.2s' }}/>
                </div>
              </div>
            )}

            {/* Done message */}
            {isDone && (
              <div style={{ margin: '6px 0 10px', fontFamily: "'DM Mono',monospace", fontSize: 11, color: '#5a9a5a', lineHeight: 1.7 }}>
                Device provisioned. It will discover the controller via mDNS and appear in the dashboard within ~30s.
              </div>
            )}

            {/* Log output */}
            <div
              ref={logRef}
              style={{
                flex: 1, minHeight: 0, overflowY: 'auto',
                background: 'linear-gradient(160deg,#2a2e28,#1e2219)',
                border: '1px solid #1a1c18', borderRadius: 6,
                padding: '10px 12px',
                fontFamily: "'DM Mono',monospace", fontSize: 10, lineHeight: 1.7,
                marginTop: 10,
              }}
            >
              {log.length === 0
                ? <span style={{ color: '#556050' }}>No output yet.</span>
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

function App() {
  const [token, setToken] = useState(() => sessionStorage.getItem('em_token'));
  const [role, setRole] = useState(() => sessionStorage.getItem('em_role'));
  const [devices, setDevices] = useState([]);
  const [selected, setSelected] = useState(null);
  const [release, setRelease] = useState(null);
  const [status, setStatus] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [showWizard, setShowWizard] = useState(false);
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
        <ProvisionWizard token={token} onClose={() => setShowWizard(false)}/>
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
