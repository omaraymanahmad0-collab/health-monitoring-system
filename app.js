  window.addEventListener('error', () => {
    const t = document.getElementById('modelTag');
    if (t && typeof THREE === 'undefined') t.textContent = '3D engine could not load (no internet / CDN blocked) - HUD still running';
  });
  let currentAid = 'amira';
  let ppgData = [];
  let isAlert = false;
  let authed = false;
  let ME = null;

  function showLogin(msg) {
    authed = false; ME = null;
    document.getElementById('loginScreen').classList.remove('hide');
    document.getElementById('loginErr').textContent = msg || '';
  }

  async function applyMe(me, crewIds) {
    ME = me; authed = true;
    document.getElementById('loginScreen').classList.add('hide');
    document.getElementById('whoName').textContent = me.display_name;
    const badge = document.getElementById('whoRole');
    badge.textContent = me.role === 'user' ? 'astronaut' : me.role;
    badge.className = 'role-badge ' + me.role;
    document.getElementById('usersBtn').style.display = me.role === 'admin' ? '' : 'none';
    // crew dropdown: show only the astronauts this account may see
    const sel = document.getElementById('crewSelect');
    Array.from(sel.options).forEach(o => { o.hidden = o.disabled = !crewIds.includes(o.value); });
    if (!crewIds.includes(currentAid) && crewIds.length) currentAid = crewIds[0];
    sel.value = currentAid;
    sel.style.pointerEvents = crewIds.length > 1 ? '' : 'none';
    // permissions on the control dock
    document.querySelectorAll('[id^="sc-"]').forEach(b => b.classList.toggle('locked', me.role !== 'admin'));
    document.querySelectorAll('[id^="fix-"]').forEach(b => b.classList.toggle('locked', me.role === 'doctor'));
    poll();
  }

  async function checkSession() {
    try {
      const res = await fetch('/api/me');
      if (!res.ok) return showLogin('');
      const d = await res.json();
      applyMe(d.me, d.crew || []);
    } catch (e) { showLogin('Cannot reach the ASTRA server'); }
  }

  async function doLogin() {
    const username = document.getElementById('loginUser').value;
    const password = document.getElementById('loginPass').value;
    const res = await fetch('/api/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password })
    });
    const d = await res.json();
    if (!res.ok) { document.getElementById('loginErr').textContent = d.error || 'Login failed'; return; }
    document.getElementById('loginPass').value = '';
    checkSession();
  }

  async function doLogout() {
    await fetch('/api/logout', { method: 'POST' });
    showLogin('Signed out');
  }

  async function openAlerts() {
    const res = await fetch('/api/alerts');
    if (!res.ok) return;
    const d = await res.json();
    const body = document.getElementById('alertsBody');
    body.textContent = '';
    (d.alerts || []).forEach(a => {
      const tr = document.createElement('tr');
      [new Date(a.ts * 1000).toLocaleString(), a.astronaut_id, a.level, a.message].forEach(v => {
        const td = document.createElement('td'); td.textContent = v; tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    if (!(d.alerts || []).length) {
      const tr = document.createElement('tr'); const td = document.createElement('td');
      td.colSpan = 4; td.textContent = 'No alerts yet'; tr.appendChild(td); body.appendChild(tr);
    }
    document.getElementById('alertsModal').classList.add('show');
  }

  async function openUsers() {
    const res = await fetch('/api/users');
    if (!res.ok) return;
    const d = await res.json();
    const body = document.getElementById('usersBody');
    body.textContent = '';
    (d.users || []).forEach(u => {
      const tr = document.createElement('tr');
      [u.username, u.role, u.astronaut_id || '-', u.display_name || ''].forEach(v => {
        const td = document.createElement('td'); td.textContent = v; tr.appendChild(td);
      });
      const td = document.createElement('td');
      if (ME && u.username !== ME.username) {
        const b = document.createElement('button');
        b.className = 'mini-btn'; b.textContent = 'Delete';
        b.onclick = async () => { await fetch('/api/users/' + encodeURIComponent(u.username), { method: 'DELETE' }); openUsers(); };
        td.appendChild(b);
      }
      tr.appendChild(td);
      body.appendChild(tr);
    });
    document.getElementById('usersModal').classList.add('show');
  }

  async function createUser() {
    const role = document.getElementById('nuRole').value;
    const res = await fetch('/api/users', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: document.getElementById('nuUser').value,
        password: document.getElementById('nuPass').value,
        display_name: document.getElementById('nuName').value,
        role: role,
        astronaut_id: role === 'user' ? document.getElementById('nuAid').value : null
      })
    });
    const d = await res.json();
    const msg = document.getElementById('nuMsg');
    msg.style.color = res.ok ? 'var(--mint)' : 'var(--coral)';
    msg.textContent = res.ok ? 'Account created' : (d.error || 'Failed');
    if (res.ok) { ['nuUser', 'nuPass', 'nuName'].forEach(i => document.getElementById(i).value = ''); openUsers(); }
  }

  async function setScenario(sc) {
    await fetch(`/api/crew/${currentAid}/scenario`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scenario: sc })
    });
    poll();
  }

  async function resolveWith(key) {
    const btn = document.getElementById(`fix-${key}`);
    if (!btn.classList.contains('ready')) return;
    await fetch(`/api/crew/${currentAid}/overrides`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [key]: true })
    });
    poll();
  }

  function updateAI(ai) {
    const node = document.getElementById('node-ai');
    const tag = document.getElementById('aiTag');
    const val = document.getElementById('valAi');
    const foot = document.getElementById('valAiFoot');
    const bar = document.getElementById('aiBarFill');
    node.classList.remove('ai-warn', 'ai-watch');
    bar.style.width = '0%';
    bar.style.background = 'var(--mint)';
    if (!ai || ai.status === 'loading') {
      tag.textContent = 'LOADING'; val.textContent = '—'; foot.textContent = 'Loading models…'; return;
    }
    if (ai.status === 'warming_up') {
      tag.textContent = 'WARM-UP'; val.textContent = '—';
      foot.textContent = `Collecting data ${ai.have || 0}/${ai.need || 10} s`; return;
    }
    if (ai.status !== 'ok') {
      tag.textContent = 'OFFLINE'; val.textContent = 'AI offline';
      foot.textContent = ai.reason || 'AI unavailable'; return;
    }
    tag.textContent = 'LIVE';
    val.textContent = `${ai.state} ${Math.round(ai.confidence * 100)}%`;
    const pct = Math.round(ai.risk * 100);
    foot.textContent = `Alert risk, next ${ai.horizon_s} s: ${pct}%` + (ai.anomaly ? ' · anomaly' : '');
    bar.style.width = pct + '%';
    if (ai.level === 'warn') { node.classList.add('ai-warn'); bar.style.background = 'var(--coral)'; }
    else if (ai.level === 'watch') { node.classList.add('ai-watch'); bar.style.background = 'var(--amber)'; }
  }

  const CREW_NAMES = {};
  let reportFor = null;
  let reportText = '';

  function updateHealth(ai) {
    const node = document.getElementById('node-health');
    const tag = document.getElementById('healthTag');
    const val = document.getElementById('valHealth');
    const fb = document.getElementById('healthFeedback');
    const act = document.getElementById('healthAction');
    const bar = document.getElementById('healthBar');
    const btn = document.getElementById('reportBtn');
    node.classList.remove('health-watch', 'health-warn', 'health-crit');
    const h = ai && ai.health;
    if (!h) {
      tag.textContent = (ai && (ai.status === 'unavailable' || ai.status === 'error')) ? 'OFFLINE' : '...';
      val.textContent = '--';
      fb.textContent = (ai && ai.reason) ? ai.reason : 'Waiting for telemetry...';
      act.textContent = '';
      bar.style.width = '0%';
      btn.style.display = 'none';
      return;
    }
    const names = { ok: 'HEALTHY', watch: 'WATCH', warning: 'WARNING', critical: 'CRITICAL' };
    const cols = { ok: 'var(--mint)', watch: 'var(--amber)', warning: 'var(--amber)', critical: 'var(--coral)' };
    tag.textContent = names[h.severity] || h.severity;
    val.textContent = `${h.score} / 100`;
    fb.textContent = h.headline;
    act.textContent = h.severity === 'ok' ? '' : ('Next: ' + ((h.actions && h.actions[0]) || 'check crew status'));
    node.title = (h.feedback || []).join(' | ');
    bar.style.width = h.score + '%';
    bar.style.background = cols[h.severity] || 'var(--mint)';
    if (h.severity === 'watch') node.classList.add('health-watch');
    if (h.severity === 'warning') node.classList.add('health-warn');
    if (h.severity === 'critical') node.classList.add('health-crit');
    btn.style.display = h.incident_id ? 'block' : 'none';
  }

  function showBanner(c) {
    const b = document.getElementById('errBanner');
    if (!c) { b.className = 'err-banner'; return; }
    b.className = 'err-banner show ' + (c.severity === 'critical' ? 'crit' : 'warn');
    b.dataset.aid = c.id;
    document.getElementById('errText').textContent =
      `${c.severity === 'critical' ? 'HEALTH ERROR' : 'HEALTH WARNING'} - ${c.name}: ${c.headline}`;
  }

  async function updateCrew() {
    try {
      const res = await fetch('/api/ai/crew');
      if (!res.ok) return;
      const d = await res.json();
      const icon = { ok: '🟢', watch: '🟡', warning: '🟠', critical: '🔴' };
      const sel = document.getElementById('crewSelect');
      let worst = null;
      (d.crew || []).forEach(c => {
        const opt = Array.from(sel.options).find(o => o.value === c.id);
        if (opt) {
          if (!(c.id in CREW_NAMES)) CREW_NAMES[c.id] = opt.textContent;
          opt.textContent = `${icon[c.severity] || ''} ${CREW_NAMES[c.id]}`;
        }
        if (c.rank >= 2 && (!worst || c.rank > worst.rank)) worst = c;
      });
      showBanner(worst);
    } catch (e) {}
  }

  async function openReport(aid) {
    aid = aid || currentAid;
    reportFor = aid;
    const body = document.getElementById('rptBody');
    const title = document.getElementById('rptTitle');
    document.getElementById('reportModal').classList.add('show');
    body.textContent = 'Loading report...';
    try {
      const res = await fetch(`/api/ai/report/${aid}`);
      const d = await res.json();
      if (d.report) {
        reportText = d.report.text;
        title.textContent = `Incident ${d.report.id} - ${d.report.status}`;
        body.textContent = reportText;
      } else {
        reportText = '';
        title.textContent = 'Incident report';
        body.textContent = 'No incident recorded for this crew member.';
      }
    } catch (e) {
      body.textContent = 'Could not load report: ' + e;
    }
  }
  function closeReport() { document.getElementById('reportModal').classList.remove('show'); }
  async function copyReport() {
    try { await navigator.clipboard.writeText(reportText); } catch (e) {}
  }
  function downloadReport() {
    if (!reportText) return;
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([reportText], { type: 'text/plain' }));
    a.download = `astra_incident_${reportFor || 'report'}.txt`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  function updateHUD(v, ai) {
    if (!v) return;
    const sc = v.scenario || 'Nominal';

    document.querySelectorAll('.pill-btn').forEach(b => b.classList.remove('active'));
    const activePill = document.getElementById(`sc-${sc}`);
    if (activePill) activePill.classList.add('active');

    const fixMap = {
      'Solar Storm': 'shield_shelter',
      'Suit Leak': 'o2_boost',
      'Thermal Spike': 'cool_max'
    };
    const requiredFix = fixMap[sc] || null;
    ['shield_shelter', 'o2_boost', 'cool_max'].forEach(k => {
      document.getElementById(`fix-${k}`).classList.toggle('ready', k === requiredFix);
    });

    const radAlert = v.neutron >= 25;
    const o2Alert = v.spo2 < 92 || v.psi < 3.5;
    const tempAlert = v.temp > 37.8;
    const hrAlert = v.hr > 115;

    document.getElementById('valNeut').textContent = `${v.neutron.toFixed(1)} µSv/h`;
    document.getElementById('valCum').textContent = `Career: ${v.cumulative_msv.toFixed(1)} / 600 mSv`;
    document.getElementById('valHr').textContent = `${Math.round(v.hr)} BPM`;
    document.getElementById('valO2Psi').textContent = `${v.spo2.toFixed(1)}% · ${v.psi.toFixed(2)}`;
    document.getElementById('valTemp').textContent = `Skin Temp: ${v.temp.toFixed(1)} °C`;

    document.getElementById('node-neut').classList.toggle('alert', radAlert);
    document.getElementById('node-hr').classList.toggle('alert', hrAlert);
    document.getElementById('node-o2').classList.toggle('alert', o2Alert || tempAlert);

    const toast = document.getElementById('aiToast');
    const badge = document.getElementById('toastBadge');
    const msg = document.getElementById('toastMsg');
    const dot = document.getElementById('statusDot');

    let state = 'NORMAL';
    if (radAlert) {
      state = 'CRITICAL';
      msg.textContent = `Solar Storm (${v.neutron.toFixed(1)} µSv/h) — Click "Deploy Shielding" to auto-resolve.`;
    } else if (o2Alert) {
      state = 'CRITICAL';
      msg.textContent = `Suit Leak (${v.psi.toFixed(2)} PSI) — Click "O2 Pressure Purge" to auto-resolve.`;
    } else if (tempAlert || hrAlert) {
      state = 'WARNING';
      msg.textContent = `Thermal Spike (${v.temp.toFixed(1)} °C) — Click "Max LCVG Cooling" to auto-resolve.`;
    } else {
      msg.textContent = 'All suit life-support and neutron sensors balanced.';
    }

    // AI health monitor: catches problems the simple alarms miss (bradycardia, hypothermia, dose, sensor/telemetry faults)
    let aiHealth = false;
    if (state === 'NORMAL' && ai && ai.health && (ai.health.severity === 'warning' || ai.health.severity === 'critical')) {
      aiHealth = true;
      state = ai.health.severity === 'critical' ? 'CRITICAL' : 'WARNING';
      msg.textContent = `${ai.health.headline} - ${(ai.health.actions && ai.health.actions[0]) || 'check crew status'}.`;
    }

    // AI early warning: only speaks up while the rule-based alarms are still quiet
    let predictive = false;
    if (state === 'NORMAL' && ai && ai.status === 'ok' && ai.level === 'warn') {
      state = 'WARNING';
      predictive = true;
      const what = ai.state === 'Nominal' ? 'vitals drifting toward alert limits' : `${ai.state} pattern detected`;
      msg.textContent = `AI early warning: ${Math.round(ai.risk * 100)}% risk of an alert within ${ai.horizon_s} s — ${what}.`;
    }

    isAlert = state !== 'NORMAL';
    toast.className = 'ai-toast' + (state === 'CRITICAL' ? ' crit' : (state === 'WARNING' ? ' warn' : ''));
    dot.className = 'status-dot' + (state === 'CRITICAL' ? ' crit' : (state === 'WARNING' ? ' warn' : ''));
    badge.textContent = predictive ? 'PREDICTIVE' : (aiHealth ? 'AI HEALTH' : state);

    const orb = document.getElementById('ringOrb');
    const rLbl = document.getElementById('ringLabel');
    orb.classList.toggle('vibe', isAlert);
    rLbl.textContent = isAlert ? 'Haptic Alert Active' : 'Standby · Nominal';
    rLbl.style.color = state === 'CRITICAL' ? '#FB7185' : (state === 'WARNING' ? '#FBBF24' : '#2DD4BF');

    try {  // 3D objects may not exist yet (or at all, if the 3D engine failed to load) - never break the HUD for it
      const hex = state === 'CRITICAL' ? 0xFB7185 : (state === 'WARNING' ? 0xFBBF24 : 0x2DD4BF);
      suitRingMat.color.setHex(hex);
      ringWaveMat.color.setHex(hex);
      innerRingMat.color.setHex(hex);
      ringLight.color.setHex(hex);
      hullBeaconMat.color.setHex(isAlert ? 0xFB7185 : 0x2DD4BF);
      wristDialMat.color.setHex(hex);
    } catch (e) {}
  }

  async function poll() {
    if (!authed) return;
    try {
      const res = await fetch(`/api/state/${currentAid}`);
      if (res.status === 401) return showLogin('Session expired - please sign in again');
      if (!res.ok) return;
      const d = await res.json();
      if (d.ppg) ppgData = d.ppg;
      try { updateAI(d.ai); } catch (e) { console.error(e); }
      try { updateHealth(d.ai); } catch (e) { console.error(e); }
      try { if (d.vitals) updateHUD(d.vitals, d.ai); } catch (e) { console.error(e); }
      updateCrew();
    } catch (e) {
      document.getElementById('toastMsg').textContent = 'Connection to ASTRA server lost - retrying...';
    }
  }

  function switchCrew(aid) {
    currentAid = aid;
    poll();
  }

  let metSec = 15720;
  setInterval(() => {
    metSec++;
    const h = String(Math.floor(metSec / 3600)).padStart(2, '0');
    const m = String(Math.floor((metSec % 3600) / 60)).padStart(2, '0');
    const s = String(metSec % 60).padStart(2, '0');
    document.getElementById('clock').textContent = `MET ${h}:${m}:${s}`;
    poll();
  }, 1000);

  // =========================================================================
  // THREE.JS PHOTOREALISTIC PBR RENDERER + PMREM STUDIO ENVIRONMENT
  // =========================================================================
  const canvas = document.getElementById('stage3d');
  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x02040A, 0.011);

  const camera = new THREE.PerspectiveCamera(35, window.innerWidth / window.innerHeight, 0.1, 200);
  camera.position.set(0.32, 0.22, 4.45);

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'high-performance' });
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.outputEncoding = THREE.sRGBEncoding;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.15;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;

  // Generate Realistic PBR Environment Map for Helmet Visor & Metallic Reflections
  const pmremGenerator = new THREE.PMREMGenerator(renderer);
  const envTex = pmremGenerator.fromScene(new THREE.RoomEnvironment(), 0.04).texture;
  scene.environment = envTex;

  // Cinematic 3-Point Orbital Lighting with Soft Shadows
  scene.add(new THREE.HemisphereLight(0xF8FAFC, 0x1E293B, 0.72));

  const sunLight = new THREE.DirectionalLight(0xFFFDF8, 1.55);
  sunLight.position.set(4.5, 6.5, 5.2);
  sunLight.castShadow = true;
  sunLight.shadow.mapSize.width = 2048;
  sunLight.shadow.mapSize.height = 2048;
  sunLight.shadow.bias = -0.0005;
  scene.add(sunLight);

  const rimLight = new THREE.DirectionalLight(0x7DD3FC, 0.65);
  rimLight.position.set(-4.5, 2.5, -3.5);
  scene.add(rimLight);

  const fillLight = new THREE.DirectionalLight(0xCBD5E1, 0.42);
  fillLight.position.set(-2.5, -1.5, 4.0);
  scene.add(fillLight);

  // -------------------------------------------------------------------------
  // A. PRESERVED SPACE BACKDROP (STARFIELD, BLUE PLANET, ORBITING SPACESHIP)
  // -------------------------------------------------------------------------
  const starCount = 2000;
  const starPos = new Float32Array(starCount * 3);
  const starColors = new Float32Array(starCount * 3);
  const starPalette = [new THREE.Color(0xFFFFFF), new THREE.Color(0xBAE6FD), new THREE.Color(0xFDE68A), new THREE.Color(0xC7D2FE)];
  for (let i = 0; i < starCount; i++) {
    starPos[i * 3] = (Math.random() - 0.5) * 100;
    starPos[i * 3 + 1] = (Math.random() - 0.5) * 70;
    starPos[i * 3 + 2] = -Math.random() * 95 + 10;
    const c = starPalette[i % starPalette.length];
    starColors[i * 3] = c.r; starColors[i * 3 + 1] = c.g; starColors[i * 3 + 2] = c.b;
  }
  const starGeo = new THREE.BufferGeometry();
  starGeo.setAttribute('position', new THREE.BufferAttribute(starPos, 3));
  starGeo.setAttribute('color', new THREE.BufferAttribute(starColors, 3));
  scene.add(new THREE.Points(starGeo, new THREE.PointsMaterial({ size: 0.14, vertexColors: true, transparent: true, opacity: 0.92 })));

  const planetGroup = new THREE.Group();
  planetGroup.position.set(-12.0, -5.5, -27.0);
  scene.add(planetGroup);
  const planetMesh = new THREE.Mesh(
    new THREE.SphereGeometry(7.5, 48, 48),
    new THREE.MeshStandardMaterial({ color: 0x1E3A8A, roughness: 0.72, metalness: 0.15, envMapIntensity: 0.25 })
  );
  planetGroup.add(planetMesh);
  planetGroup.add(new THREE.Mesh(
    new THREE.SphereGeometry(7.78, 48, 48),
    new THREE.MeshBasicMaterial({ color: 0x38BDF8, transparent: true, opacity: 0.16, side: THREE.BackSide })
  ));

  const spaceshipGroup = new THREE.Group();
  spaceshipGroup.position.set(5.2, 1.55, -15.5);
  spaceshipGroup.rotation.set(0.18, -0.52, -0.14);
  scene.add(spaceshipGroup);

  const hullMetal = new THREE.MeshStandardMaterial({ color: 0x94A3B8, roughness: 0.3, metalness: 0.85, envMapIntensity: 0.6 });
  const goldFoil = new THREE.MeshStandardMaterial({ color: 0xD97706, roughness: 0.25, metalness: 0.92, envMapIntensity: 0.7 });
  const solarMat = new THREE.MeshStandardMaterial({ color: 0x0F294A, emissive: 0x071933, roughness: 0.22, metalness: 0.85 });
  const thrusterGlowMat = new THREE.MeshBasicMaterial({ color: 0x38BDF8, transparent: true, opacity: 0.85 });
  const hullBeaconMat = new THREE.MeshBasicMaterial({ color: 0x2DD4BF });

  const mainHull = new THREE.Mesh(new THREE.CylinderGeometry(0.85, 0.95, 5.4, 32), hullMetal);
  mainHull.rotation.z = Math.PI / 2;
  spaceshipGroup.add(mainHull);

  const forwardModule = new THREE.Mesh(new THREE.CylinderGeometry(0.55, 0.85, 1.6, 32), goldFoil);
  forwardModule.rotation.z = Math.PI / 2;
  forwardModule.position.x = -3.3;
  spaceshipGroup.add(forwardModule);

  const centrifugeRing = new THREE.Group();
  centrifugeRing.position.x = 0.4;
  spaceshipGroup.add(centrifugeRing);
  const ringTorus = new THREE.Mesh(new THREE.TorusGeometry(2.15, 0.16, 16, 48), hullMetal);
  ringTorus.rotation.y = Math.PI / 2;
  centrifugeRing.add(ringTorus);

  [-1, 1].forEach(dir => {
    const wing = new THREE.Mesh(new THREE.BoxGeometry(1.4, 0.04, 4.6), solarMat);
    wing.position.set(1.6, 0, dir * 3.2);
    spaceshipGroup.add(wing);
  });

  const thrusterPlume = new THREE.Mesh(new THREE.ConeGeometry(0.62, 3.4, 24, 1, true), thrusterGlowMat);
  thrusterPlume.rotation.z = -Math.PI / 2;
  thrusterPlume.position.x = 5.1;
  spaceshipGroup.add(thrusterPlume);

  const hullBeacon = new THREE.Mesh(new THREE.SphereGeometry(0.09, 12, 12), hullBeaconMat);
  hullBeacon.position.set(-3.9, 0.65, 0);
  spaceshipGroup.add(hullBeacon);

  // -------------------------------------------------------------------------
  // B. HIGH-RESOLUTION PBR FABRIC NORMAL, ROUGHNESS & AO TEXTURE GENERATORS
  // -------------------------------------------------------------------------
  function createPBRFabricMaps() {
    const size = 1024;
    const diffC = document.createElement('canvas');
    const bumpC = document.createElement('canvas');
    const roughC = document.createElement('canvas');
    diffC.width = bumpC.width = roughC.width = size;
    diffC.height = bumpC.height = roughC.height = size;

    const dCtx = diffC.getContext('2d');
    const bCtx = bumpC.getContext('2d');
    const rCtx = roughC.getContext('2d');

    // Off-white woven ortho-fabric base with subtle weathering & AO
    dCtx.fillStyle = '#E2E4E8';
    dCtx.fillRect(0, 0, size, size);
    bCtx.fillStyle = '#808080';
    bCtx.fillRect(0, 0, size, size);
    rCtx.fillStyle = '#C8C8C8';
    rCtx.fillRect(0, 0, size, size);

    // Micro ripstop weave
    for (let i = 0; i < size; i += 4) {
      dCtx.fillStyle = i % 8 === 0 ? 'rgba(90, 98, 110, 0.09)' : 'rgba(255, 255, 255, 0.14)';
      dCtx.fillRect(0, i, size, 1.5);
      dCtx.fillRect(i, 0, 1.5, size);

      bCtx.fillStyle = i % 8 === 0 ? '#6A6A6A' : '#969696';
      bCtx.fillRect(0, i, size, 1.5);
      bCtx.fillRect(i, 0, 1.5, size);
    }

    // Deep organic pressure-suit creases & folds (matching IMAGE 2)
    for (let i = 0; i < 160; i++) {
      const y = (i / 160) * size;
      const freq = 0.012 + (i % 5) * 0.003;
      const phase = i * 2.37;
      const amp = 14 + (i % 7) * 3.5;

      // Ambient Occlusion valley shadow in diffuse
      const dg = dCtx.createLinearGradient(0, y - 9, 0, y + 9);
      dg.addColorStop(0, 'rgba(226,228,232,0)');
      dg.addColorStop(0.42, 'rgba(72, 78, 88, 0.44)');
      dg.addColorStop(0.58, 'rgba(252, 253, 255, 0.55)');
      dg.addColorStop(1, 'rgba(226,228,232,0)');
      dCtx.fillStyle = dg;

      // Height ridge in bump map
      const bg = bCtx.createLinearGradient(0, y - 9, 0, y + 9);
      bg.addColorStop(0, 'rgba(128,128,128,0)');
      bg.addColorStop(0.42, 'rgba(28, 28, 28, 0.78)');
      bg.addColorStop(0.58, 'rgba(235, 235, 235, 0.85)');
      bg.addColorStop(1, 'rgba(128,128,128,0)');
      bCtx.fillStyle = bg;

      [dCtx, bCtx].forEach(ctx => {
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.bezierCurveTo(
          size * 0.33, y + Math.sin(phase) * amp,
          size * 0.66, y - Math.cos(phase * freq * 60) * amp,
          size, y
        );
        ctx.lineTo(size, y + 14);
        ctx.lineTo(0, y + 14);
        ctx.closePath();
        ctx.fill();
      });
    }

    // Stitched vertical & horizontal seam lines
    dCtx.strokeStyle = 'rgba(65, 72, 82, 0.45)';
    dCtx.setLineDash([6, 4]);
    dCtx.lineWidth = 2;
    [256, 512, 768].forEach(pos => {
      dCtx.beginPath(); dCtx.moveTo(pos, 0); dCtx.lineTo(pos, size); dCtx.stroke();
    });

    const map = new THREE.CanvasTexture(diffC);
    const bumpMap = new THREE.CanvasTexture(bumpC);
    const roughnessMap = new THREE.CanvasTexture(roughC);
    [map, bumpMap, roughnessMap].forEach(t => {
      t.wrapS = t.wrapT = THREE.RepeatWrapping;
      t.repeat.set(2, 2);
    });
    return { map, bumpMap, roughnessMap };
  }

  function createQuiltedPadMaps(cols, rows) {
    const c = document.createElement('canvas');
    const b = document.createElement('canvas');
    c.width = b.width = 512;
    c.height = b.height = 512;
    const ctx = c.getContext('2d');
    const btx = b.getContext('2d');

    ctx.fillStyle = '#B8BEC7'; ctx.fillRect(0, 0, 512, 512);
    btx.fillStyle = '#303030'; btx.fillRect(0, 0, 512, 512);

    const cw = 512 / cols, ch = 512 / rows;
    for (let r = 0; r < rows; r++) {
      for (let col = 0; col < cols; col++) {
        const cx = col * cw + cw * 0.5, cy = r * ch + ch * 0.5;
        const rad = Math.max(cw, ch) * 0.68;

        const g = ctx.createRadialGradient(cx, cy, 2, cx, cy, rad);
        g.addColorStop(0, '#F1F4F8');
        g.addColorStop(0.75, '#D4D9E0');
        g.addColorStop(1, '#7A828E');
        ctx.fillStyle = g;
        ctx.fillRect(col * cw + 2, r * ch + 2, cw - 4, ch - 4);

        const bg = btx.createRadialGradient(cx, cy, 2, cx, cy, rad);
        bg.addColorStop(0, '#FFFFFF');
        bg.addColorStop(0.78, '#B0B0B0');
        bg.addColorStop(1, '#181818');
        btx.fillStyle = bg;
        btx.fillRect(col * cw + 2, r * ch + 2, cw - 4, ch - 4);
      }
    }
    return { map: new THREE.CanvasTexture(c), bumpMap: new THREE.CanvasTexture(b) };
  }

  const fabricPBR = createPBRFabricMaps();
  const waistPadPBR = createQuiltedPadMaps(22, 1);
  const shinPadPBR = createQuiltedPadMaps(3, 9);

  // Sculpted Vertex Displacement Helper (converts smooth geometry into realistic creased fabric)
  function sculptOrganicFolds(geo, amp = 0.014, freqY = 26.0, freqX = 9.0) {
    const pos = geo.attributes.position;
    const norm = geo.attributes.normal;
    for (let i = 0; i < pos.count; i++) {
      const x = pos.getX(i), y = pos.getY(i), z = pos.getZ(i);
      const fold =
        Math.sin(y * freqY + Math.sin(x * freqX) * 2.2) * amp +
        Math.cos(y * (freqY * 1.9) + z * 11.0) * (amp * 0.45);
      pos.setXYZ(
        i,
        x + norm.getX(i) * fold,
        y + norm.getY(i) * fold,
        z + norm.getZ(i) * fold
      );
    }
    geo.computeVertexNormals();
    return geo;
  }

  const evaClothMat = new THREE.MeshStandardMaterial({
    map: fabricPBR.map,
    bumpMap: fabricPBR.bumpMap,
    bumpScale: 0.055,
    roughnessMap: fabricPBR.roughnessMap,
    roughness: 0.84,
    metalness: 0.04,
    envMapIntensity: 0.35
  });

  const hardShellMat = new THREE.MeshStandardMaterial({
    color: 0xDCE0E6,
    roughness: 0.38,
    metalness: 0.22,
    envMapIntensity: 0.55
  });

  const brushedMetalMat = new THREE.MeshStandardMaterial({
    color: 0x717A87,
    roughness: 0.32,
    metalness: 0.85,
    envMapIntensity: 0.75
  });

  const waistCummerbundMat = new THREE.MeshStandardMaterial({
    map: waistPadPBR.map,
    bumpMap: waistPadPBR.bumpMap,
    bumpScale: 0.05,
    roughness: 0.78,
    metalness: 0.05,
    envMapIntensity: 0.35
  });

  const shinArmorMat = new THREE.MeshStandardMaterial({
    map: shinPadPBR.map,
    bumpMap: shinPadPBR.bumpMap,
    bumpScale: 0.05,
    roughness: 0.72,
    metalness: 0.08,
    envMapIntensity: 0.4
  });

  const wristDialMat = new THREE.MeshBasicMaterial({ color: 0x2DD4BF });
  const suitRingMat = new THREE.MeshBasicMaterial({ color: 0x2DD4BF });
  const ringWaveMat = new THREE.MeshBasicMaterial({ color: 0x2DD4BF, transparent: true, opacity: 0.8, side: THREE.DoubleSide });

  // -------------------------------------------------------------------------
  // C. BUILD PHOTOREALISTIC IMAGE-2 SPACESUIT (AND AUTO-LOAD GLB IF PRESENT)
  // -------------------------------------------------------------------------
  const suit = new THREE.Group();
  scene.add(suit);
  let ringWaveMesh = null, leftArm = null, rightArm = null;

  function buildImage2PhotorealisticAstronaut() {
    // 1. REALISTIC SPHERICAL HELMET + CROWN FIN + SIDE COMM PODS + DOUBLE COLLAR + GOLD SUN VISOR
    const helmet = new THREE.Group();
    helmet.position.set(0, 1.43, 0.02);
    suit.add(helmet);

    // Outer Composite Helmet Shell
    const shell = new THREE.Mesh(
      new THREE.SphereGeometry(0.415, 64, 64, -Math.PI * 0.64, Math.PI * 1.28, 0, Math.PI * 0.90),
      hardShellMat
    );
    shell.rotation.y = Math.PI;
    shell.castShadow = true;
    helmet.add(shell);

    // Top Center Fin & Crown Vent (distinctive detail from IMAGE 2)
    const crownFin = new THREE.Mesh(new THREE.BoxGeometry(0.018, 0.055, 0.22), brushedMetalMat);
    crownFin.position.set(0, 0.41, 0.02);
    helmet.add(crownFin);
    const crownCap = new THREE.Mesh(new THREE.CylinderGeometry(0.032, 0.022, 0.03, 16), brushedMetalMat);
    crownCap.position.set(0, 0.415, 0.13);
    helmet.add(crownCap);

    // Side Metallic Comm & Ear Hinges (IMAGE 2)
    [-1, 1].forEach(side => {
      const earPod = new THREE.Mesh(new THREE.CylinderGeometry(0.078, 0.086, 0.055, 24), brushedMetalMat);
      earPod.rotation.z = Math.PI / 2;
      earPod.position.set(side * 0.41, 0.01, -0.02);
      helmet.add(earPod);

      const bolt = new THREE.Mesh(new THREE.CylinderGeometry(0.024, 0.024, 0.07, 16), hardShellMat);
      bolt.rotation.z = Math.PI / 2;
      bolt.position.set(side * 0.425, 0.02, -0.02);
      helmet.add(bolt);
    });

    // Thick Sculpted Visor Rim Gasket & Double Neck Bearing Collar (IMAGE 2)
    const rimGasket = new THREE.Mesh(new THREE.TorusGeometry(0.335, 0.034, 24, 64), hardShellMat);
    rimGasket.position.set(0, 0.01, 0.145);
    helmet.add(rimGasket);

    const upperCollar = new THREE.Mesh(new THREE.TorusGeometry(0.32, 0.042, 24, 64), hardShellMat);
    upperCollar.rotation.x = Math.PI / 2;
    upperCollar.position.set(0, -0.29, -0.01);
    helmet.add(upperCollar);

    const lowerCollar = new THREE.Mesh(new THREE.TorusGeometry(0.345, 0.046, 24, 64), brushedMetalMat);
    lowerCollar.rotation.x = Math.PI / 2;
    lowerCollar.position.set(0, -0.36, -0.01);
    helmet.add(lowerCollar);

    // Realistic Human Astronaut Head & Comm Cap Inside Helmet (IMAGE 2)
    const headGroup = new THREE.Group();
    headGroup.position.set(0, -0.02, 0.04);
    helmet.add(headGroup);

    const cranium = new THREE.Mesh(
      new THREE.SphereGeometry(0.195, 48, 48),
      new THREE.MeshStandardMaterial({ color: 0x8D5B46, roughness: 0.58, metalness: 0.02 })
    );
    cranium.scale.set(0.84, 1.06, 0.94);
    headGroup.add(cranium);

    // Nose & Brow Ridge for realistic facial silhouette through visor
    const nose = new THREE.Mesh(
      new THREE.ConeGeometry(0.026, 0.065, 16),
      new THREE.MeshStandardMaterial({ color: 0x85533E, roughness: 0.58 })
    );
    nose.rotation.x = 0.35;
    nose.position.set(0, -0.015, 0.185);
    headGroup.add(nose);

    const snoopyCap = new THREE.Mesh(
      new THREE.SphereGeometry(0.204, 48, 32, 0, Math.PI * 2, 0, Math.PI * 0.55),
      new THREE.MeshStandardMaterial({ color: 0x262B34, roughness: 0.85 })
    );
    headGroup.add(snoopyCap);

    // Half-Lowered Translucent Gold Sun-Shield Visor (signature detail in IMAGE 2)
    const goldShield = new THREE.Mesh(
      new THREE.SphereGeometry(0.352, 48, 32, -Math.PI * 0.35, Math.PI * 0.70, 0.24, 0.34),
      new THREE.MeshPhysicalMaterial({
        color: 0xD97706,
        emissive: 0x451A03,
        emissiveIntensity: 0.22,
        metalness: 0.92,
        roughness: 0.12,
        transparent: true,
        opacity: 0.86,
        envMapIntensity: 1.2
      })
    );
    goldShield.rotation.set(0.08, Math.PI * 0.5, 0);
    goldShield.position.set(0, 0.02, 0.09);
    helmet.add(goldShield);

    // Outer Clear/Smoked Polycarbonate Pressure Bubble with Environment Reflections
    const bubble = new THREE.Mesh(
      new THREE.SphereGeometry(0.345, 48, 48),
      new THREE.MeshPhysicalMaterial({
        color: 0xCBD5E1,
        metalness: 0.1,
        roughness: 0.05,
        transparent: true,
        opacity: 0.34,
        clearcoat: 1.0,
        clearcoatRoughness: 0.04,
        envMapIntensity: 1.35
      })
    );
    bubble.position.set(0, 0.01, 0.085);
    helmet.add(bubble);

    // 2. ANATOMICALLY SCULPTED UPPER TORSO + CHEST TELEMETRY MODULE + SINGLE CURVED HOSE
    const torsoPts = [
      new THREE.Vector2(0.33, -0.34),
      new THREE.Vector2(0.36, -0.18),
      new THREE.Vector2(0.38,  0.00),
      new THREE.Vector2(0.41,  0.18),
      new THREE.Vector2(0.42,  0.30),
      new THREE.Vector2(0.35,  0.36)
    ];
    const torsoGeo = sculptOrganicFolds(new THREE.LatheGeometry(torsoPts, 64), 0.012, 28.0, 10.0);
    const torso = new THREE.Mesh(torsoGeo, evaClothMat);
    torso.scale.set(1.16, 1.0, 0.82);
    torso.position.set(0, 0.76, 0);
    torso.castShadow = true;
    suit.add(torso);

    // Chest Control & Telemetry Box (matching IMAGE 2's gray housing, blue/amber panels & dials)
    const chestBox = new THREE.Group();
    chestBox.position.set(-0.02, 0.82, 0.33);
    suit.add(chestBox);

    const housing = new THREE.Mesh(new THREE.BoxGeometry(0.34, 0.24, 0.11), hardShellMat);
    housing.castShadow = true;
    chestBox.add(housing);

    // Left vertical array of 4 rotary switches
    for (let k = 0; k < 4; k++) {
      const dial = new THREE.Mesh(new THREE.CylinderGeometry(0.016, 0.018, 0.025, 16), brushedMetalMat);
      dial.rotation.x = Math.PI / 2;
      dial.position.set(-0.11, 0.06 - k * 0.042, 0.06);
      chestBox.add(dial);
    }

    // Blue & Amber Instrument Readout Panels (IMAGE 2)
    const blueGauge = new THREE.Mesh(
      new THREE.BoxGeometry(0.14, 0.075, 0.012),
      new THREE.MeshStandardMaterial({ color: 0x3B82F6, roughness: 0.35, metalness: 0.2 })
    );
    blueGauge.position.set(-0.01, -0.04, 0.056);
    chestBox.add(blueGauge);

    const amberStrip = new THREE.Mesh(
      new THREE.BoxGeometry(0.065, 0.045, 0.012),
      new THREE.MeshStandardMaterial({ color: 0xD97706, roughness: 0.35, metalness: 0.3 })
    );
    amberStrip.position.set(0.02, 0.035, 0.056);
    chestBox.add(amberStrip);

    // Curved Life-Support Hose looping from Chest Box into Lower Torso Port (IMAGE 2)
    const chestHoseCurve = new THREE.CatmullRomCurve3([
      new THREE.Vector3(0.09, 0.71, 0.36),
      new THREE.Vector3(0.14, 0.59, 0.39),
      new THREE.Vector3(0.08, 0.54, 0.37),
      new THREE.Vector3(0.03, 0.56, 0.34)
    ]);
    const chestHose = new THREE.Mesh(new THREE.TubeGeometry(chestHoseCurve, 32, 0.016, 14, false), evaClothMat);
    suit.add(chestHose);

    // Sculpted PLSS Life-Support Backpack
    const plss = new THREE.Mesh(new THREE.BoxGeometry(0.68, 0.96, 0.30), hardShellMat);
    plss.position.set(0, 0.72, -0.34);
    plss.castShadow = true;
    suit.add(plss);

    // 3. SCULPTED SHOULDER PAULDRONS WITH RAISED DOME ARMOR PADS (SIGNATURE FEATURE IN IMAGE 2)
    [-1, 1].forEach(side => {
      const pauldron = new THREE.Group();
      pauldron.position.set(side * 0.50, 0.98, -0.01);
      pauldron.rotation.set(0.04, 0, side * -0.46);
      suit.add(pauldron);

      const capGeo = sculptOrganicFolds(
        new THREE.SphereGeometry(0.245, 40, 28, 0, Math.PI * 2, 0, Math.PI * 0.56),
        0.008, 22.0, 8.0
      );
      const cap = new THREE.Mesh(capGeo, evaClothMat);
      cap.scale.set(1.16, 0.74, 1.16);
      cap.castShadow = true;
      pauldron.add(cap);

      // Raised Molded Dome Nodes on Shoulder Armor (exact match to IMAGE 2)
      const domeCoords = [
        [0, 0.16, 0],
        [-0.09, 0.13, 0.08], [0.09, 0.13, 0.08],
        [-0.09, 0.13, -0.08], [0.09, 0.13, -0.08],
        [0, 0.12, 0.13], [0, 0.12, -0.13]
      ];
      domeCoords.forEach(([dx, dy, dz]) => {
        const node = new THREE.Mesh(new THREE.SphereGeometry(0.038, 20, 14, 0, Math.PI * 2, 0, Math.PI * 0.55), hardShellMat);
        node.position.set(dx, dy, dz);
        pauldron.add(node);
      });
    });

    // 4. VERTICAL-RIBBED QUILTED WAIST CUMMERBUND & PELVIS HARNESS STRAPS (IMAGE 2)
    const waistGeo = new THREE.CylinderGeometry(0.365, 0.385, 0.30, 56, 8);
    const waist = new THREE.Mesh(waistGeo, waistCummerbundMat);
    waist.scale.set(1.15, 1.0, 0.83);
    waist.position.set(0, 0.28, 0);
    suit.add(waist);

    // Belt Clasp & Buckle Detail (IMAGE 2)
    const beltStrap = new THREE.Mesh(new THREE.TorusGeometry(0.36, 0.018, 12, 48), hardShellMat);
    beltStrap.rotation.x = Math.PI / 2;
    beltStrap.scale.set(1.06, 0.82, 1.0);
    beltStrap.position.set(0, 0.42, 0.01);
    suit.add(beltStrap);

    // Sculpted Pelvis & Thighs with Natural Fabric Creases
    const pelvisGeo = sculptOrganicFolds(new THREE.CylinderGeometry(0.385, 0.36, 0.38, 48, 16), 0.015, 24.0, 9.0);
    const pelvis = new THREE.Mesh(pelvisGeo, evaClothMat);
    pelvis.scale.set(1.14, 1.0, 0.83);
    pelvis.position.set(0, -0.06, 0);
    suit.add(pelvis);

    // Hip & Crotch Suspension Harness Straps (distinctive detail from IMAGE 2)
    [-1, 1].forEach(side => {
      const strapCurve = new THREE.CatmullRomCurve3([
        new THREE.Vector3(side * 0.24, 0.22, 0.31),
        new THREE.Vector3(side * 0.43, 0.02, 0.14),
        new THREE.Vector3(side * 0.38, -0.22, 0.18),
        new THREE.Vector3(side * 0.12, -0.34, 0.22),
        new THREE.Vector3(0.0, -0.36, 0.21)
      ]);
      const strap = new THREE.Mesh(new THREE.TubeGeometry(strapCurve, 36, 0.022, 10, false), hardShellMat);
      suit.add(strap);
    });

    // 5. ANATOMICALLY PROPORTIONED ARMS, GREEN WRIST DIAL, ORANGE HAND MODULE & ASTRA-RING
    [-1, 1].forEach(side => {
      const arm = new THREE.Group();
      arm.position.set(side * 0.54, 0.86, -0.01);
      arm.rotation.set(-0.10, 0, side * 0.44);
      suit.add(arm);
      if (side === -1) rightArm = arm;
      if (side === 1) leftArm = arm;

      const upperGeo = sculptOrganicFolds(new THREE.CylinderGeometry(0.145, 0.126, 0.44, 36, 18), 0.012, 32.0, 8.0);
      const upper = new THREE.Mesh(upperGeo, evaClothMat);
      upper.position.y = -0.22;
      upper.castShadow = true;
      arm.add(upper);

      const fore = new THREE.Group();
      fore.position.set(0, -0.45, 0);
      fore.rotation.set(-0.26, 0, side * -0.10);
      arm.add(fore);

      const foreGeo = sculptOrganicFolds(new THREE.CylinderGeometry(0.126, 0.098, 0.42, 36, 18), 0.011, 34.0, 8.0);
      const forearm = new THREE.Mesh(foreGeo, evaClothMat);
      forearm.position.y = -0.21;
      forearm.castShadow = true;
      fore.add(forearm);

      // Wrist Disconnect Cuff
      const cuff = new THREE.Mesh(new THREE.TorusGeometry(0.098, 0.018, 16, 32), hardShellMat);
      cuff.rotation.x = Math.PI / 2;
      cuff.position.y = -0.42;
      fore.add(cuff);

      // Left Wrist Green Biometric Watch/Dial (from IMAGE 2)
      if (side === 1) {
        const dialHousing = new THREE.Mesh(new THREE.CylinderGeometry(0.044, 0.048, 0.032, 24), hardShellMat);
        dialHousing.rotation.z = Math.PI / 2;
        dialHousing.position.set(0.105, -0.38, 0.02);
        fore.add(dialHousing);

        const dialFace = new THREE.Mesh(new THREE.CircleGeometry(0.034, 24), wristDialMat);
        dialFace.rotation.y = Math.PI / 2;
        dialFace.position.set(0.122, -0.38, 0.02);
        fore.add(dialFace);
      }

      // Sculpted EVA Gloves with Articulated Fingers
      const hand = new THREE.Group();
      hand.position.set(0, -0.49, 0.01);
      fore.add(hand);

      const palm = new THREE.Mesh(new THREE.BoxGeometry(0.118, 0.122, 0.068), hardShellMat);
      hand.add(palm);

      // Left Glove Copper/Orange Back-of-Hand Sensor Pad (from IMAGE 2)
      if (side === 1) {
        const copperPad = new THREE.Mesh(
          new THREE.BoxGeometry(0.022, 0.065, 0.055),
          new THREE.MeshStandardMaterial({ color: 0xC2410C, roughness: 0.3, metalness: 0.75 })
        );
        copperPad.position.set(0.062, -0.01, 0.0);
        hand.add(copperPad);
      }

      // Thumb + 4 Anatomically Curved Fingers
      const thumb = new THREE.Mesh(new THREE.CylinderGeometry(0.015, 0.018, 0.072, 14), hardShellMat);
      thumb.position.set(-side * 0.062, -0.035, 0.028);
      thumb.rotation.set(0.35, 0, -side * 0.45);
      hand.add(thumb);

      [{ x: -0.042, len: 0.088 }, { x: -0.014, len: 0.098 }, { x: 0.014, len: 0.092 }, { x: 0.041, len: 0.076 }].forEach(f => {
        const finger = new THREE.Mesh(new THREE.CylinderGeometry(0.013, 0.016, f.len, 14), hardShellMat);
        finger.position.set(side * f.x, -0.058 - f.len / 2, 0.014);
        finger.rotation.x = 0.28;
        hand.add(finger);
      });

      // Right Index Finger Glowing ASTRA-Ring + Expanding Radial Pulse Wave
      if (side === -1) {
        const rMesh = new THREE.Mesh(new THREE.TorusGeometry(0.022, 0.006, 16, 28), suitRingMat);
        rMesh.position.set(0.042, -0.092, 0.020);
        rMesh.rotation.x = Math.PI / 2 + 0.28;
        hand.add(rMesh);

        ringWaveMesh = new THREE.Mesh(new THREE.RingGeometry(0.035, 0.052, 32), ringWaveMat);
        ringWaveMesh.position.copy(rMesh.position);
        hand.add(ringWaveMesh);
      }
    });

    // 6. HEAVY LEGS + GRID-QUILTED SHIN ARMOR PADS + SCULPTED EVA BOOTS (IMAGE 2)
    [-1, 1].forEach(side => {
      const leg = new THREE.Group();
      leg.position.set(side * 0.205, -0.24, 0);
      suit.add(leg);

      const thighGeo = sculptOrganicFolds(new THREE.CylinderGeometry(0.195, 0.172, 0.68, 40, 22), 0.016, 28.0, 9.0);
      const thigh = new THREE.Mesh(thighGeo, evaClothMat);
      thigh.position.y = -0.34;
      thigh.castShadow = true;
      leg.add(thigh);

      const calfGeo = sculptOrganicFolds(new THREE.CylinderGeometry(0.172, 0.152, 0.74, 40, 22), 0.014, 30.0, 9.0);
      const calf = new THREE.Mesh(calfGeo, evaClothMat);
      calf.position.y = -1.02;
      calf.castShadow = true;
      leg.add(calf);

      // Grid-Quilted Rectangular Shin Guard (signature armor from IMAGE 2)
      const shinPad = new THREE.Mesh(new THREE.BoxGeometry(0.215, 0.62, 0.065), shinArmorMat);
      shinPad.position.set(0, -1.04, 0.145);
      shinPad.castShadow = true;
      leg.add(shinPad);

      // Heavy Contoured Lunar/EVA Boots with Ribbed Toe Cap & Sole (IMAGE 2)
      const bootUpper = new THREE.Mesh(new THREE.CylinderGeometry(0.145, 0.165, 0.24, 28), hardShellMat);
      bootUpper.position.set(0, -1.50, 0.02);
      leg.add(bootUpper);

      const toeCap = new THREE.Mesh(new THREE.SphereGeometry(0.165, 28, 20, 0, Math.PI * 2, 0, Math.PI * 0.52), hardShellMat);
      toeCap.scale.set(0.95, 0.85, 1.48);
      toeCap.position.set(0, -1.61, 0.09);
      leg.add(toeCap);

      const sole = new THREE.Mesh(new THREE.BoxGeometry(0.245, 0.046, 0.44), brushedMetalMat);
      sole.position.set(0, -1.63, 0.08);
      leg.add(sole);
    });
  }

  // Attempt to load cached official GLB model first; fallback to sculpted PBR suit
  const gltfLoader = new THREE.GLTFLoader();
  gltfLoader.load(
    '/models/astronaut.glb',
    (gltf) => {
      const model = gltf.scene;
      model.traverse((node) => {
        if (node.isMesh) {
          node.castShadow = true;
          node.receiveShadow = true;
          if (node.material) {
            node.material.envMapIntensity = 1.1;
            node.material.needsUpdate = true;
          }
        }
      });
      const box = new THREE.Box3().setFromObject(model);
      const size = box.getSize(new THREE.Vector3());
      const center = box.getCenter(new THREE.Vector3());
      const scale = 3.35 / Math.max(size.x, size.y, size.z);
      model.scale.setScalar(scale);
      model.position.sub(center.multiplyScalar(scale));
      suit.add(model);

      // Attach live ASTRA-Ring beacon on right hand
      const rMesh = new THREE.Mesh(new THREE.TorusGeometry(0.03, 0.008, 16, 32), suitRingMat);
      rMesh.position.set(-0.76, -0.06, 0.14);
      suit.add(rMesh);
      ringWaveMesh = new THREE.Mesh(new THREE.RingGeometry(0.045, 0.068, 32), ringWaveMat);
      ringWaveMesh.position.copy(rMesh.position);
      suit.add(ringWaveMesh);

      document.getElementById('modelTag').textContent = 'Photorealistic NASA PBR GLB Asset Active';
    },
    undefined,
    () => {
      buildImage2PhotorealisticAstronaut();
    }
  );

  // -------------------------------------------------------------------------
  // D. MINI 3D RING ORB
  // -------------------------------------------------------------------------
  const ringMini = document.getElementById('ringMini');
  const rScene = new THREE.Scene();
  const rCam = new THREE.PerspectiveCamera(38, ringMini.width / ringMini.height, 0.1, 20);
  rCam.position.set(0, 1.05, 2.5);
  rCam.lookAt(0, 0, 0);

  const rRenderer = new THREE.WebGLRenderer({ canvas: ringMini, antialias: true, alpha: true });
  rRenderer.setSize(ringMini.width, ringMini.height);

  rScene.add(new THREE.AmbientLight(0xffffff, 1.2));
  const rDir = new THREE.DirectionalLight(0xffffff, 1.7);
  rDir.position.set(3, 4, 4);
  rScene.add(rDir);
  const ringLight = new THREE.PointLight(0x2DD4BF, 2.2, 8);
  ringLight.position.set(-2, -1, 2);
  rScene.add(ringLight);

  const ringModel = new THREE.Group();
  rScene.add(ringModel);

  const pts = [
    new THREE.Vector2(0.56, -0.22), new THREE.Vector2(0.70, -0.22),
    new THREE.Vector2(0.74, -0.17), new THREE.Vector2(0.74, 0.17),
    new THREE.Vector2(0.70, 0.22),  new THREE.Vector2(0.56, 0.22),
    new THREE.Vector2(0.54, 0.18),  new THREE.Vector2(0.54, -0.18),
    new THREE.Vector2(0.56, -0.22)
  ];
  ringModel.add(new THREE.Mesh(new THREE.LatheGeometry(pts, 64), new THREE.MeshStandardMaterial({ color: 0x334155, metalness: 0.9, roughness: 0.2 })));

  const innerRingMat = new THREE.MeshBasicMaterial({ color: 0x2DD4BF });
  const innerRing = new THREE.Mesh(new THREE.TorusGeometry(0.545, 0.015, 12, 64), innerRingMat);
  innerRing.rotation.x = Math.PI / 2;
  ringModel.add(innerRing);

  // Orbit & Camera Views
  let targetRotY = -0.24, targetCam = new THREE.Vector3(0.32, 0.22, 4.45), targetLook = new THREE.Vector3(0, -0.05, 0);
  let dragging = false, px = 0, py = 0;

  canvas.addEventListener('mousedown', e => { dragging = true; px = e.clientX; py = e.clientY; });
  window.addEventListener('mouseup', () => dragging = false);
  window.addEventListener('mousemove', e => {
    if (!dragging) return;
    targetRotY += (e.clientX - px) * 0.012;
    targetCam.y = Math.max(-1.2, Math.min(1.8, targetCam.y + (e.clientY - py) * 0.008));
    px = e.clientX; py = e.clientY;
  });
  canvas.addEventListener('wheel', e => {
    e.preventDefault();
    targetCam.z = Math.max(1.25, Math.min(6.6, targetCam.z + e.deltaY * 0.003));
  }, { passive: false });

  window.addEventListener('resize', () => {
    renderer.setSize(window.innerWidth, window.innerHeight);
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
  });

  function focusView(v) {
    if (v === 'FULL') { targetRotY = -0.24; targetCam.set(0.32, 0.22, 4.45); targetLook.set(0, -0.05, 0); }
    else if (v === 'HELMET') { targetRotY = -0.22; targetCam.set(0.32, 1.45, 1.42); targetLook.set(0, 1.42, 0.05); }
    else if (v === 'CHEST') { targetRotY = -0.22; targetCam.set(0.28, 0.72, 1.55); targetLook.set(0, 0.68, 0.22); }
    else if (v === 'RING') { targetRotY = -0.35; targetCam.set(-1.12, 0.05, 1.35); targetLook.set(-0.74, -0.05, 0.12); }
  }

  let anim = 0;
  function loop() {
    anim += 0.03;

    const posArr = starGeo.attributes.position.array;
    for (let i = 0; i < starCount; i++) {
      posArr[i * 3 + 2] += 0.045;
      if (posArr[i * 3 + 2] > 12) {
        posArr[i * 3 + 2] = -85;
        posArr[i * 3] = (Math.random() - 0.5) * 100;
        posArr[i * 3 + 1] = (Math.random() - 0.5) * 70;
      }
    }
    starGeo.attributes.position.needsUpdate = true;

    planetMesh.rotation.y += 0.0008;
    centrifugeRing.rotation.x += 0.015;
    spaceshipGroup.position.x = 5.0 + Math.sin(anim * 0.18) * 1.1;
    spaceshipGroup.position.y = 1.5 + Math.cos(anim * 0.25) * 0.28;
    thrusterGlowMat.opacity = 0.65 + Math.sin(anim * 6.0) * 0.25;
    hullBeacon.visible = Math.sin(anim * 2.5) > 0;

    suit.rotation.y += (targetRotY - suit.rotation.y) * 0.08;
    suit.position.y = Math.sin(anim * 1.0) * 0.032;
    suit.rotation.z = Math.sin(anim * 0.7) * 0.015;
    if (rightArm) rightArm.rotation.x = -0.10 + Math.sin(anim * 1.3) * 0.02;

    camera.position.lerp(targetCam, 0.08);
    camera.lookAt(targetLook);

    if (ringWaveMesh) {
      const s = 1 + ((anim * 1.8) % 1.8);
      ringWaveMesh.scale.set(s, s, 1);
      ringWaveMat.opacity = Math.max(0, (isAlert ? 0.85 : 0.35) * (1 - ((anim * 1.8) % 1.8) / 1.8));
    }

    ringModel.rotation.y += 0.024;
    ringModel.rotation.x = 0.42 + Math.sin(anim) * 0.1;
    if (isAlert) {
      ringModel.position.set((Math.random() - 0.5) * 0.07, (Math.random() - 0.5) * 0.07, 0);
    } else {
      ringModel.position.set(0, 0, 0);
    }

    renderer.render(scene, camera);
    rRenderer.render(rScene, rCam);
    drawWave();
    requestAnimationFrame(loop);
  }

  function drawWave() {
    const c = document.getElementById('ppgWave');
    const ctx = c.getContext('2d');
    const w = c.width = c.offsetWidth || 180;
    const h = c.height;
    ctx.clearRect(0, 0, w, h);
    if (ppgData.length < 10) return;

    const slice = ppgData.slice(-160);
    const minV = Math.min(...slice), maxV = Math.max(...slice);
    const span = Math.max(1e-3, maxV - minV);

    ctx.strokeStyle = isAlert ? '#FB7185' : '#2DD4BF';
    ctx.lineWidth = 2;
    ctx.beginPath();
    slice.forEach((val, idx) => {
      const x = (idx / (slice.length - 1)) * w;
      const y = h * 0.84 - ((val - minV) / span) * (h * 0.68);
      if (idx === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  checkSession();
  loop();
