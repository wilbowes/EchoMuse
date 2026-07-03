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
      <div style={{ width: 'min(900px,95vw)', maxHeight: '90vh', background: 'linear-gradient(170deg,#e8e4de,#d8d4cc)', border: '1px solid #b8b4ac', borderRadius: 16, boxShadow: '0 24px 80px rgba(0,0,0,0.3),0 2px 0 rgba(255,255,255,0.8) inset', display: 'flex', flexDirection: 'column', overflow: 'hidden', animation: 'fadeIn 0.15s ease' }}>
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
                {device.ip} · {device.device_id} · {device.firmware_ver || 'unknown'}
                {needsUpdate && <span style={{ color: '#806010', marginLeft: 10 }}>Update available</span>}
              </div>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <div style={{ background: 'linear-gradient(160deg,#2a2e28,#1c1f18)', border: '1px solid #1a1c16', borderRadius: 6, padding: '5px 12px', boxShadow: 'inset 0 1px 3px rgba(0,0,0,0.5)' }}>
                <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: state.dot, textShadow: `0 0 8px ${state.dot}88`, letterSpacing: '0.05em' }}>{state.label.toUpperCase()}</span>
              </div>
              {isAdmin && !confirmDelete && (
                <button onClick={() => setConfirmDelete(true)} title="Delete device"
                  style={{ background: 'linear-gradient(180deg,#d0ccc4,#bab6ae)', border: '1px solid #a0a098', borderRadius: '50%', width: 28, height: 28, display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer', boxShadow: '0 1px 0 rgba(255,255,255,0.5) inset', color: '#a04848', fontSize: 13 }}>🗑</button>
              )}
              {isAdmin && confirmDelete && (
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <span style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: '#a04848' }}>Delete?</span>
                  <Pill small danger disabled={deleting} onClick={doDelete}>{deleting ? '…' : 'Confirm'}</Pill>
                  <Pill small onClick={() => setConfirmDelete(false)} disabled={deleting}>Cancel</Pill>
                </div>
              )}
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
              <div style={{ display:'flex', gap:16, marginBottom:12, alignItems:'flex-end' }}>
                <Lcd label="On device"  value={device.firmware_ver || '—'} color={needsUpdate ? 'var(--lcd-amber)' : 'var(--lcd-green)'}/>
                <Lcd label="Available"  value={release?.version || '—'} color="var(--lcd-dim)"/>
                {device.firmware_previous && (
                  <Lcd label="Rollback slot" value={device.firmware_previous} color="var(--lcd-dim)"/>
                )}
              </div>
              <div style={{ marginBottom:24 }}>
                <Pill small onClick={doCheckRelease} disabled={checkingRelease}>
                  {checkingRelease ? 'Checking…' : 'Check now'}
                </Pill>
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
              <div style={{ margin: '6px 0 10px', display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: '#5a9a5a', lineHeight: 1.7 }}>
                  Provisioning complete. The device has rebooted and will discover the controller via mDNS,
                  appearing in the dashboard as a pending device within ~30s.
                </div>
                <div><Pill accent onClick={onClose}>Done</Pill></div>
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

// ─── DeviceConfigForm ─────────────────────────────────────────────────────────
// Shared config form used by both the per-device config tab and the global
// settings panel. disabled=true renders all controls read-only.

function DeviceConfigForm({ config, onChange, disabled }) {
  const EQ_FREQS = ['125 Hz','250 Hz','500 Hz','1 kHz','2 kHz','3.5 kHz','5.5 kHz','8 kHz'];
  const EQ_DESCS = ['shelf','','','','','','','shelf'];
  const bands    = config.eqBands ?? [0,0,0,0,0,0,0,0];
  const fmtDb    = v => (v >= 0 ? '+' : '') + Number(v).toFixed(1) + ' dB';

  const set = disabled ? () => {} : onChange;
  const setEqBand = (i, v) => { const b=[...bands]; b[i]=v; set('eqBands', b); };

  const inputStyle = disabled ? { opacity: 0.45, pointerEvents: 'none' } : {};

  return (
    <div style={{ maxWidth: 440 }}>
      <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.15em', marginBottom: 20 }}>Microphone · 7-mic array</div>
      <div style={inputStyle}>
        <Slider label="Digital Gain" sub="ctl 89–143 all ADCs" value={config.adcDigitalGain} min={0} max={100} onChange={v => set('adcDigitalGain', v)}/>
        <Slider label="MICPGA" sub="ctl 92–146 all ADCs" value={config.adcMicpga} min={0} max={100} onChange={v => set('adcMicpga', v)}/>
        <Slider label="VAD Threshold" sub="RMS" value={config.vadThreshold} min={0.001} max={0.02} step={0.001} onChange={v => set('vadThreshold', v)}/>
        <Slider label="VAD Speech Ms" sub="min speech to open gate" value={config.vadSpeechMs} min={32} max={320} step={32} onChange={v => set('vadSpeechMs', v)}/>
        <Slider label="VAD Silence Ms" sub="silence to close gate" value={config.vadSilenceMs} min={200} max={2000} step={100} onChange={v => set('vadSilenceMs', v)}/>
      </div>
      <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.15em', margin: '28px 0 20px' }}>Beamforming · Delay and Sum</div>
      <div style={inputStyle}>
        <Toggle label="Enabled" sub="7-mic delay-and-sum" value={config.beamformingEnabled ?? true} onChange={v => set('beamformingEnabled', v)}/>
        <Slider label="Beam Angle" sub="-1 = auto" value={config.beamAngle ?? -1} min={-1} max={359} step={1} onChange={v => set('beamAngle', v)}/>
      </div>
      <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.15em', margin: '28px 0 20px' }}>Speaker · TLV320 · card 0 dev 23</div>
      <div style={inputStyle}>
        <Slider label="Startup Volume" sub="ctl 61" value={config.startupVolume} min={0} max={100} onChange={v => set('startupVolume', v)}/>
      </div>
      <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.15em', margin: '28px 0 12px' }}>EQ · 8-Band · controller-side</div>
      <div style={inputStyle}>
        <EqCurve bands={bands}/>
        <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:'0 20px' }}>
          <div>{[0,1,2,3].map(i => (
            <Slider key={i} label={EQ_FREQS[i]} sub={EQ_DESCS[i]}
              value={bands[i]??0} min={-12} max={12} step={0.5}
              formatValue={fmtDb} onChange={v => setEqBand(i,v)}/>
          ))}</div>
          <div>{[4,5,6,7].map(i => (
            <Slider key={i} label={EQ_FREQS[i]} sub={EQ_DESCS[i]}
              value={bands[i]??0} min={-12} max={12} step={0.5}
              formatValue={fmtDb} onChange={v => setEqBand(i,v)}/>
          ))}</div>
        </div>
        {!disabled && (
          <div style={{ display:'flex', gap:8, marginBottom:20, marginTop:4 }}>
            <Pill small onClick={() => set('eqBands',[0,0,0,0,0,0,0,0])}>Flat</Pill>
            <Pill small onClick={() => set('eqBands',[0,0,0,0,0,7,4,2])}>Clarity</Pill>
            <Pill small onClick={() => set('eqBands',[0,3,2,0,-2,0,0,0])}>Warmth</Pill>
          </div>
        )}
        <Toggle label="Loudness" sub="speech-range presence boost" value={config.eqLoudness??false} onChange={v => set('eqLoudness',v)}/>
      </div>
      <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.15em', margin: '28px 0 20px' }}>Wake Word · OpenWakeWord</div>
      <div style={inputStyle}>
        <Slider label="Detection Threshold" value={config.owwThreshold} min={0.1} max={0.9} step={0.05} onChange={v => set('owwThreshold', v)}/>
        <div style={{ marginBottom: 24 }}>
          <div style={{ fontFamily: "'DM Mono',monospace", fontSize: 11, color: 'var(--text2)', marginBottom: 8 }}>Model</div>
          <select value={config.owwModel} onChange={e => set('owwModel', e.target.value)} disabled={disabled}>
            <option value="hey_jarvis_v0.1">hey_jarvis_v0.1</option>
            <option value="alexa_v0.1">alexa_v0.1</option>
            <option value="hey_mycroft_v0.1">hey_mycroft_v0.1</option>
            <option value="hey_rhasspy_v0.1">hey_rhasspy_v0.1</option>
          </select>
        </div>
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

  function setConf(k, v) { setConfig(c => ({ ...c, [k]: v })); setDirty(true); }

  async function saveGlobalConfig() {
    setSaving(true);
    try {
      const res = await API.post('/api/global/config', config);
      onGlobalConfigChange(config);
      setDirty(false);
      const n = res.pushed_to?.length ?? 0;
      if (n > 0) alert(`Saved and pushed to ${n} device${n === 1 ? '' : 's'} on fleet config.`);
    } catch(e) {
      alert(e.error || 'Failed to save global config');
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
      <div style={{ width:'min(900px,95vw)', maxHeight:'90vh', background:'linear-gradient(170deg,#e8e4de,#d8d4cc)', border:'1px solid #b8b4ac', borderRadius:16, boxShadow:'0 24px 80px rgba(0,0,0,0.3),0 2px 0 rgba(255,255,255,0.8) inset', display:'flex', flexDirection:'column', overflow:'hidden', animation:'fadeIn 0.15s ease' }}>

        {/* Header */}
        <div style={{ background:'linear-gradient(180deg,#dedad2,#ccc8c0)', borderBottom:'1px solid #b0aca4', padding:'20px 24px 0', boxShadow:'0 1px 0 rgba(255,255,255,0.5) inset' }}>
          <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom:16 }}>
            <div style={{ fontFamily:"'DM Sans',sans-serif", fontSize:22, color:'var(--text)', fontWeight:600, letterSpacing:'-0.02em' }}>Settings</div>
            <button onClick={onClose} style={{ background:'none', border:'none', fontSize:20, color:'var(--muted)', cursor:'pointer', lineHeight:1, padding:'0 4px' }}>✕</button>
          </div>
          <div style={{ display:'flex', gap:0 }}>
            {TABS.map(t => (
              <div key={t} onClick={() => setTab(t)} style={{
                fontFamily:"'DM Mono',monospace", fontSize:10, textTransform:'uppercase', letterSpacing:'0.12em',
                padding:'8px 18px', cursor:'pointer', borderBottom: t === tab ? '2px solid #405878' : '2px solid transparent',
                color: t === tab ? '#405878' : 'var(--muted)', transition:'color 0.1s',
              }}>{TAB_LABELS[t]}</div>
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
                  <Pill onClick={() => { setConfig({...globalConfig}); setDirty(false); }}>Revert</Pill>
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
  const [token, setToken] = useState(() => sessionStorage.getItem('em_token'));
  const [role, setRole] = useState(() => sessionStorage.getItem('em_role'));
  const [devices, setDevices] = useState([]);
  const [selected, setSelected] = useState(null);
  const [release, setRelease] = useState(null);
  const [checkingRelease, setCheckingRelease] = useState(false);
  const [status, setStatus] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [showWizard, setShowWizard] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [globalConfig, setGlobalConfig] = useState(null);
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
          <button onClick={() => setShowSettings(true)} title="Settings" style={{
            background: 'none', border: 'none', cursor: 'pointer', padding: '2px 4px',
            color: 'var(--muted)', fontSize: 16, lineHeight: 1, opacity: 0.7,
            transition: 'opacity 0.15s',
          }} onMouseEnter={e => e.target.style.opacity=1} onMouseLeave={e => e.target.style.opacity=0.7}>⚙</button>
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
