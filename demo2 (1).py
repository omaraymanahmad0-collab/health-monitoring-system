from __future__ import annotations  # fixes 'type object is not subscriptable' on Python 3.8

import hashlib
import hmac
import json
import math
import os
import random
import secrets
import sqlite3
import threading
import time
import traceback
import urllib.request
import webbrowser
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# ============================================================================
# 1. ZERO-DEPENDENCY SQLITE, TELEMETRY & OPTIONAL GLB ASSET FETCHER
# ============================================================================
DB_PATH = os.getenv("ASTRO_DB", "astra_spatial.db")
MODEL_FILE = os.getenv("ASTRO_MODEL", "astronaut.glb")
DB_LOCK = threading.Lock()

LAST: dict[str, dict] = {}
PPG: dict[str, dict] = {}
OVERRIDES: dict[str, dict] = {}

# --- gradual physiology: the *true* state eases toward the scenario target instead of jumping.
#     This is what makes early warning possible (there is a drift to detect). 0 = old instant jumps.
RAMP_TAU_S = float(os.getenv("ASTRO_RAMP_TAU", "30"))
TRUE_STATE: dict[str, list] = {}

SCENARIOS = ("Nominal", "Solar Storm", "Suit Leak", "Thermal Spike")

CREW_ROLES = {
    "amira": "Mission Commander",
    "lukas": "Flight Engineer",
    "yuki": "Mission Specialist",
}

# ============================================================================
# 0b. AI CO-PILOT - crew HEALTH MONITOR (pure Python, zero dependencies)
#     * detects health problems from live vitals (hypoxia, suit leak, heat stress,
#       radiation, tachycardia/bradycardia, hypothermia, dose limit, sensor faults)
#     * gives feedback + recommended actions for every crew member
#     * predicts drift toward alert limits (early warning)
#     * raises an ERROR and writes an INCIDENT REPORT when something goes wrong
# ============================================================================
CAREER_LIMIT_MSV = 600.0
INCIDENT_LOG = os.getenv("ASTRO_INCIDENT_LOG", "astra_incidents.jsonl")

LEVEL_RANK = {"watch": 1, "warning": 2, "critical": 3}
RANK_NAME = {0: "ok", 1: "watch", 2: "warning", 3: "critical"}
SEVERITY_RANK = {v: k for k, v in RANK_NAME.items()}

LABELS = {
    "hr": ("Heart rate", "BPM"),
    "spo2": ("Blood oxygen (SpO2)", "%"),
    "temp": ("Skin temperature", "C"),
    "psi": ("Suit pressure", "PSI"),
    "neutron": ("Neutron dose rate", "uSv/h"),
}
# vital: (nominal value, alert limit) - used to predict how close a trend is to an alert
RISK_MODEL = {
    "hr": (64.0, 115.0),
    "spo2": (98.6, 92.0),
    "temp": (36.6, 37.8),
    "psi": (4.30, 3.5),
    "neutron": (6.2, 25.0),
}
PRIOR_STD = {"hr": 6.0, "spo2": 1.0, "temp": 0.25, "psi": 0.08, "neutron": 2.5}
PLAUSIBLE = {  # outside this range the reading is a sensor fault, not a patient problem
    "hr": (20.0, 240.0),
    "spo2": (50.0, 100.5),
    "temp": (30.0, 45.0),
    "psi": (-0.5, 6.0),
    "neutron": (0.0, 5000.0),
}
PRIORITY = [
    "low_pressure", "hypoxia", "radiation", "hyperthermia", "hypothermia",
    "bradycardia", "tachycardia", "dose", "signal", "sensor", "telemetry",
]
STATE_OF = {
    "low_pressure": "Suit Leak / Hypoxia", "hypoxia": "Suit Leak / Hypoxia",
    "radiation": "Radiation Exposure", "dose": "Radiation Dose Limit",
    "hyperthermia": "Heat Stress", "hypothermia": "Hypothermia",
    "tachycardia": "Cardiac Stress", "bradycardia": "Bradycardia",
    "signal": "PPG Signal Lost", "sensor": "Sensor Fault", "telemetry": "Telemetry Lost",
}
EXPECTED = {  # vitals that normally move together for a given condition
    "Suit Leak / Hypoxia": {"psi", "spo2", "hr"},
    "Heat Stress": {"temp", "hr"},
    "Radiation Exposure": {"neutron", "hr"},
}
PLAYBOOK = {
    "Suit Leak / Hypoxia": (
        "Falling suit pressure with dropping blood oxygen and compensatory tachycardia points to a "
        "pressure-garment breach or a PLSS oxygen supply fault.",
        [
            "Press 'O2 Pressure Purge' to raise suit O2 and restore pressure",
            "Locate and seal the breach (gloves, wrist rings, helmet seal, hose connections)",
            "Abort EVA and return to the airlock if pressure does not recover within 60 s",
        ],
    ),
    "Radiation Exposure": (
        "A sudden rise in neutron dose rate is consistent with a solar particle event or a shielding failure.",
        [
            "Press 'Deploy Shielding' and move to the storm shelter",
            "Terminate EVA and return to the airlock",
            "Log the accumulated dose and notify the flight surgeon",
        ],
    ),
    "Heat Stress": (
        "Rising skin temperature with an elevated heart rate is consistent with a cooling-garment (LCVG) "
        "shortfall or overexertion.",
        [
            "Press 'Max LCVG Cooling'",
            "Stop strenuous work and rest until temperature falls below 37.5 C",
            "Hydrate and abort EVA if temperature keeps rising",
        ],
    ),
    "Cardiac Stress": (
        "Sustained high heart rate without another cause suggests overexertion, anxiety or early cardiac strain.",
        ["Stop activity and rest", "Check cooling and O2 settings", "Ask flight surgeon to review the ECG/PPG trace"],
    ),
    "Bradycardia": (
        "Abnormally slow heart rate can indicate a cardiac rhythm problem or sensor error.",
        ["Verify with a second sensor", "Contact the flight surgeon", "Stop activity and monitor"],
    ),
    "Hypothermia": (
        "Low skin temperature indicates the cooling garment is over-cooling or insulation has failed.",
        ["Reduce LCVG cooling", "Check suit insulation", "Return to the airlock if it keeps falling"],
    ),
    "Radiation Dose Limit": (
        "The astronaut's cumulative dose is close to or beyond the career limit.",
        ["Restrict further EVA time", "Notify the flight surgeon / mission control"],
    ),
    "PPG Signal Lost": (
        "The pulse sensor has no usable signal, so heart rate and SpO2 shown are NOT real measurements.",
        ["Re-seat the ring/finger sensor", "Use a backup sensor or voice check-in"],
    ),
    "Sensor Fault": (
        "One or more readings are physically implausible, which indicates a failed or disconnected sensor.",
        ["Cross-check with redundant sensors", "Do a voice check-in with the crew member"],
    ),
    "Telemetry Lost": (
        "No data is arriving from the suit. The crew member's health is unknown.",
        ["Contact the crew member by voice", "Check the suit comms / sensor link"],
    ),
}


def _say(msg: str):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"), flush=True)


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _median3(xs):
    ys = sorted(xs[-3:])
    return ys[len(ys) // 2]


def _slope(points):
    n = len(points)
    if n < 2:
        return 0.0
    mt = sum(t for t, _ in points) / n
    my = sum(y for _, y in points) / n
    den = sum((t - mt) ** 2 for t, _ in points)
    return 0.0 if den < 1e-9 else sum((t - mt) * (y - my) for t, y in points) / den


def _finding(code, vital, level, title, detail, action, value=None):
    return {
        "code": code, "vital": vital, "level": level, "title": title,
        "detail": detail, "action": action, "value": value,
    }


def invalid_channels(v: dict) -> list:
    bad = []
    for k, (lo, hi) in PLAUSIBLE.items():
        x = v.get(k)
        if (
            not isinstance(x, (int, float)) or isinstance(x, bool)
            or math.isnan(x) or math.isinf(x) or not (lo <= x <= hi)
        ):
            bad.append(k)
    return bad


def assess(c: dict, skip=()) -> list:
    """Clinical rules: turns one (smoothed) set of vitals into a list of findings."""
    f = []
    hr, spo2, temp, psi, neu = c["hr"], c["spo2"], c["temp"], c["psi"], c["neutron"]
    cum = c.get("cumulative_msv", 0.0) or 0.0

    if "spo2" not in skip:
        if spo2 < 88:
            f.append(_finding("hypoxia", "spo2", "critical", "Severe hypoxia", f"SpO2 {spo2:.1f}% (normal 95-100%)",
                              "Raise suit O2 now (O2 Pressure Purge) and prepare to abort EVA", spo2))
        elif spo2 < 92:
            f.append(_finding("hypoxia", "spo2", "warning", "Hypoxia", f"SpO2 {spo2:.1f}% (normal 95-100%)",
                              "Increase suit O2 flow and reduce exertion", spo2))
        elif spo2 < 95:
            f.append(_finding("hypoxia", "spo2", "watch", "Low blood oxygen", f"SpO2 {spo2:.1f}%",
                              "Monitor closely and check the suit O2 supply", spo2))

    if psi < 3.0:
        f.append(_finding("low_pressure", "psi", "critical", "Suit pressure critically low",
                          f"{psi:.2f} PSI (nominal 4.30)", "Run O2 Pressure Purge, find the breach, return to airlock", psi))
    elif psi < 3.5:
        f.append(_finding("low_pressure", "psi", "warning", "Suit pressure dropping",
                          f"{psi:.2f} PSI (nominal 4.30)", "Run O2 Pressure Purge and inspect seals", psi))
    elif psi < 4.0:
        f.append(_finding("low_pressure", "psi", "watch", "Suit pressure below nominal",
                          f"{psi:.2f} PSI (nominal 4.30)", "Inspect seals and monitor", psi))

    if "hr" not in skip:
        if hr > 140:
            f.append(_finding("tachycardia", "hr", "critical", "Severe tachycardia", f"{hr:.0f} BPM (resting ~64)",
                              "Stop all activity; check cooling, O2 and pressure", hr))
        elif hr > 115:
            f.append(_finding("tachycardia", "hr", "warning", "Tachycardia", f"{hr:.0f} BPM (resting ~64)",
                              "Reduce workload and check cooling/O2", hr))
        elif hr > 100:
            f.append(_finding("tachycardia", "hr", "watch", "Elevated heart rate", f"{hr:.0f} BPM (resting ~64)",
                              "Ease off and monitor", hr))
        elif hr < 40:
            f.append(_finding("bradycardia", "hr", "critical", "Severe bradycardia", f"{hr:.0f} BPM",
                              "Contact the flight surgeon immediately", hr))
        elif hr < 50:
            f.append(_finding("bradycardia", "hr", "warning", "Bradycardia", f"{hr:.0f} BPM",
                              "Verify with a second sensor and notify the flight surgeon", hr))

    if temp >= 38.5:
        f.append(_finding("hyperthermia", "temp", "critical", "Severe hyperthermia", f"Skin {temp:.1f} C (normal ~36.6)",
                          "Max LCVG cooling now and abort EVA", temp))
    elif temp > 37.8:
        f.append(_finding("hyperthermia", "temp", "warning", "Hyperthermia", f"Skin {temp:.1f} C (normal ~36.6)",
                          "Max LCVG cooling and rest", temp))
    elif temp > 37.5:
        f.append(_finding("hyperthermia", "temp", "watch", "Rising skin temperature", f"Skin {temp:.1f} C",
                          "Increase cooling and monitor", temp))
    elif temp <= 35.0:
        f.append(_finding("hypothermia", "temp", "critical", "Severe hypothermia", f"Skin {temp:.1f} C",
                          "Reduce cooling and return to the airlock", temp))
    elif temp < 35.5:
        f.append(_finding("hypothermia", "temp", "warning", "Hypothermia", f"Skin {temp:.1f} C",
                          "Reduce LCVG cooling", temp))
    elif temp < 36.0:
        f.append(_finding("hypothermia", "temp", "watch", "Low skin temperature", f"Skin {temp:.1f} C",
                          "Check cooling settings", temp))

    if neu >= 100:
        f.append(_finding("radiation", "neutron", "critical", "Severe radiation exposure",
                          f"{neu:.1f} uSv/h (normal ~6)", "Deploy shielding and shelter immediately", neu))
    elif neu >= 25:
        f.append(_finding("radiation", "neutron", "warning", "High radiation exposure",
                          f"{neu:.1f} uSv/h (normal ~6)", "Deploy shielding and prepare to shelter", neu))
    elif neu >= 15:
        f.append(_finding("radiation", "neutron", "watch", "Elevated radiation",
                          f"{neu:.1f} uSv/h (normal ~6)", "Monitor space-weather alerts", neu))

    pct = cum / CAREER_LIMIT_MSV * 100.0
    if pct >= 100:
        f.append(_finding("dose", "dose", "critical", "Career dose limit reached",
                          f"{cum:.1f} of {CAREER_LIMIT_MSV:.0f} mSv", "No further EVA; notify flight surgeon", cum))
    elif pct >= 85:
        f.append(_finding("dose", "dose", "warning", "Approaching career dose limit",
                          f"{cum:.1f} of {CAREER_LIMIT_MSV:.0f} mSv ({pct:.0f}%)", "Limit further EVA time", cum))
    elif pct >= 70:
        f.append(_finding("dose", "dose", "watch", "Career dose rising",
                          f"{cum:.1f} of {CAREER_LIMIT_MSV:.0f} mSv ({pct:.0f}%)", "Plan remaining EVA carefully", cum))
    return f


def render_report(inc: dict) -> str:
    bar = "=" * 64
    L = [bar, "ASTRA // CREW HEALTH INCIDENT REPORT", bar]
    L.append(f"Report ID     : {inc['id']}")
    L.append(f"Crew member   : {inc['name']} ({inc['astronaut_id']})")
    L.append(f"Status        : {inc['status']}")
    L.append(f"Severity      : {inc['severity'].upper()} (peak: {inc['peak_severity'].upper()})")
    L.append(f"Condition     : {inc['state']}")
    L.append(f"Opened        : {inc['opened_at']}")
    L.append(f"Closed        : {inc['closed_at'] or '-'}   (duration {inc['duration_s']} s)")
    L += ["", "SUMMARY", f"  {inc['headline']}", "", "FINDINGS"]
    for f in inc["findings"]:
        L.append(f"  [{f['level'].upper():8}] {f['title']}: {f['detail']}")
        L.append(f"             -> {f['action']}")
    L += ["", f"{'VITALS':24}{'AT DETECTION':>14}{'LATEST':>12}"]
    for k, (label, unit) in LABELS.items():
        if k in inc["snapshot"]:
            L.append(f"  {label + ' (' + unit + ')':22}{inc['detected'].get(k, '-'):>14}{inc['snapshot'][k]:>12}")
    if "cumulative_msv" in inc["snapshot"]:
        L.append(f"  {'Career dose (mSv)':22}{inc['detected'].get('cumulative_msv', '-'):>14}"
                 f"{inc['snapshot']['cumulative_msv']:>12}")
    if inc["extremes"]:
        L += ["", "WORST VALUES DURING INCIDENT"]
        for k, v in inc["extremes"].items():
            L.append(f"  {LABELS[k][0]:22}: {v} {LABELS[k][1]}")
    if inc["trend"]:
        L += ["", f"TRENDS (at worst point, predicted {AstraAI.HORIZON_S} s ahead)"]
        for k, t in inc["trend"].items():
            L.append(f"  {LABELS[k][0]:22}: {t['slope_per_min']:+.2f} {LABELS[k][1]}/min -> {t['predicted']}")
    L += ["", "PROBABLE CAUSE", f"  {inc['probable_cause']}", "", "RECOMMENDED ACTIONS"]
    for i, a in enumerate(inc["recommended_actions"], 1):
        L.append(f"  {i}. {a}")
    L += ["", "TIMELINE"]
    for e in inc["timeline"]:
        L.append(f"  {e['t']}  {e['event']}")
    L.append(bar)
    return "\n".join(L)


class _Track:
    def __init__(self):
        self.valid = deque(maxlen=60)  # (ts, vitals) of plausible samples
        self.last_push = 0.0
        self.name = None
        self.baseline = {k: m[0] for k, m in RISK_MODEL.items()}
        self.bad_signal = 0
        self.stale = False
        self.last_error = None
        self.incident = None
        self.last_incident = None
        self.ok_streak = 0
        self.result = {"status": "loading"}


class AstraAI:
    HORIZON_S = 30       # how far ahead the early-warning looks
    WARMUP_N = 10        # samples before trend/risk is trusted
    WINDOW_N = 30        # samples used for trend regression
    STALE_S = 5.0        # no data for this long = telemetry lost
    RESOLVE_AFTER_S = 8  # seconds of healthy data before an incident is closed
    DAMP = 0.6           # extrapolation damping (physiology eases in, it does not run away linearly)

    def __init__(self, app_ramp_tau: float = 30.0, log_path: str | None = None):
        self.app_ramp_tau = app_ramp_tau
        self.log_path = log_path or INCIDENT_LOG
        self._lock = threading.RLock()
        self._tracks: dict[str, _Track] = {}
        self._reports: deque = deque(maxlen=200)
        self._counter = 0
        self._started = False
        self._warned: dict[str, float] = {}

    # ---------------------------------------------------------------- public API
    def start(self):
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._watchdog, daemon=True).start()

    def push(self, aid: str, vitals: dict):
        now = time.time()
        with self._lock:
            tr = self._tracks.get(aid)
            if tr is None:
                tr = self._tracks[aid] = _Track()
            tr.last_push, tr.stale, tr.last_error = now, False, None
            if vitals.get("name"):
                tr.name = vitals["name"]
            try:
                res = self._analyse(aid, tr, vitals, now)
                self._update_incident(aid, tr, res, now)
                res["health"]["incident_id"] = tr.incident["id"] if tr.incident else None
                tr.result = res
            except Exception as e:  # noqa: BLE001
                tr.result = {"status": "error", "reason": f"AI analysis failed: {type(e).__name__}: {e}"}
                self._warn_once(f"analyse-{aid}", f"[ASTRA][ERROR] AI analysis failed for {aid}: {e!r}")

    def note_error(self, aid: str, message: str):
        """Called by the simulator when a step fails, so the report can say why telemetry stopped."""
        with self._lock:
            tr = self._tracks.get(aid)
            if tr is not None:
                tr.last_error = message

    def get(self, aid: str) -> dict:
        with self._lock:
            tr = self._tracks.get(aid)
            return dict(tr.result) if tr else {"status": "loading"}

    def info(self) -> dict:
        with self._lock:
            open_n = sum(1 for t in self._tracks.values() if t.incident)
            return {
                "status": "ok", "engine": "ASTRA health AI (clinical rules + trend prediction + anomaly check)",
                "horizon_s": self.HORIZON_S, "crew_tracked": len(self._tracks),
                "open_incidents": open_n, "total_incidents": self._counter, "log_file": self.log_path,
            }

    def crew(self) -> list:
        with self._lock:
            out = []
            for aid, tr in self._tracks.items():
                h = (tr.result or {}).get("health")
                if not h:
                    continue
                out.append({
                    "id": aid, "name": tr.name or aid, "severity": h["severity"],
                    "rank": SEVERITY_RANK[h["severity"]], "score": h["score"], "state": h["state"],
                    "headline": h["headline"], "incident_id": h.get("incident_id"),
                })
            return out

    def reports(self, limit: int = 50) -> list:
        with self._lock:
            items = list(self._reports)[-limit:][::-1]
            return json.loads(json.dumps([self._public(i) for i in items]))

    def latest_report(self, aid: str):
        with self._lock:
            tr = self._tracks.get(aid)
            inc = tr.last_incident if tr else None
            if inc is None:
                return None
            pub = json.loads(json.dumps(self._public(inc)))
        pub["text"] = render_report(pub)
        return pub

    # ------------------------------------------------------------------ analysis
    @staticmethod
    def _public(inc: dict) -> dict:
        return {k: v for k, v in inc.items() if not k.startswith("_")}

    def _warn_once(self, key: str, msg: str, every: float = 15.0):
        now = time.time()
        if now - self._warned.get(key, 0.0) >= every:
            self._warned[key] = now
            _say(msg)

    @staticmethod
    def _smooth(tr: _Track) -> dict:
        vals = [s for _, s in list(tr.valid)[-3:]]
        cur = {k: _median3([s[k] for s in vals]) for k in RISK_MODEL}
        cur["cumulative_msv"] = vals[-1]["cumulative_msv"]
        return cur

    def _analyse(self, aid, tr, v, now):
        extra = []
        bad = invalid_channels(v)
        if bad:
            labels = ", ".join(LABELS[k][0] for k in bad)
            extra.append(_finding(
                "sensor", "sensor", "warning", "Sensor fault",
                f"{labels}: reading missing or physically implausible",
                "Cross-check with redundant sensors; treat these values as unreliable"))
        else:
            sample = {k: float(v[k]) for k in RISK_MODEL}
            sample["cumulative_msv"] = float(v.get("cumulative_msv") or 0.0)
            tr.valid.append((now, sample))

        # PPG quality: analyze_ppg() falls back to fake 'healthy' numbers when it finds no pulse
        tr.bad_signal = 0 if v.get("signal_ok", True) else tr.bad_signal + 1
        skip = set()
        if tr.bad_signal >= 3:
            skip |= {"hr", "spo2"}
            extra.append(_finding(
                "signal", "hr", "warning", "PPG signal lost",
                f"no pulse detected for {tr.bad_signal} s - heart rate and SpO2 are not real measurements",
                "Re-seat the ring/finger sensor or use a backup sensor"))

        n = len(tr.valid)
        if n == 0:
            return self._build(aid, tr, extra, None, {}, 0.0, [], 0)

        cur = self._smooth(tr)
        findings = assess(cur, skip) + extra

        # trend + early-warning risk
        trend, risk = {}, 0.0
        if n >= self.WARMUP_N:
            series = list(tr.valid)[-self.WINDOW_N:]
            for k, (nom, lim) in RISK_MODEL.items():
                if k in skip:
                    continue
                ys = [s[k] for _, s in series]
                sm = [_median3(ys[max(0, i - 2): i + 1]) for i in range(len(ys))]
                slope = _slope([(t, y) for (t, _), y in zip(series, sm)])
                lo, hi = PLAUSIBLE[k]
                pred = min(hi, max(lo, cur[k] + slope * self.HORIZON_S * self.DAMP))
                frac = (pred - nom) / (lim - nom)  # 1.0 == predicted to be at the alert limit
                rv = min(1.0, max(0.0, (frac - 0.25) / 0.75))
                trend[k] = {"slope_per_min": round(slope * 60, 2), "predicted": round(pred, 2), "risk": round(rv, 2)}
                risk = max(risk, rv)

        # anomaly vs. this astronaut's own baseline
        anomalies = [
            k for k in RISK_MODEL
            if k not in skip and abs(cur[k] - tr.baseline[k]) / PRIOR_STD[k] >= 4.0
        ]
        if not findings and not anomalies:  # learn what 'normal' looks like for this person
            for k in RISK_MODEL:
                tr.baseline[k] += 0.02 * (cur[k] - tr.baseline[k])

        return self._build(aid, tr, findings, cur, trend, risk, anomalies, n)

    def _build(self, aid, tr, findings, cur, trend, risk, anomalies, n):
        findings = sorted(findings, key=lambda f: (-LEVEL_RANK[f["level"]], PRIORITY.index(f["code"])))
        rank = LEVEL_RANK[findings[0]["level"]] if findings else 0
        primary = findings[0] if findings else None
        state = STATE_OF[primary["code"]] if primary else "Nominal"

        if rank >= 2:
            risk = 1.0
        elif rank == 1:
            risk = max(risk, 0.45)

        if primary:
            expected = EXPECTED.get(state, set())
            corr = sum(1 for f in findings[1:] if f["vital"] in expected and f["vital"] != primary["vital"])
            conf = min(0.99, 0.55 + 0.12 * rank + 0.08 * corr)
        else:
            conf = max(0.5, 0.99 - 0.5 * risk)
        level = "warn" if risk >= 0.6 else ("watch" if (risk >= 0.3 or anomalies) else "ok")

        tv = max(trend, key=lambda k: trend[k]["risk"]) if trend else None
        if primary:
            headline = f"{primary['title']} - {primary['detail']}"
        elif tv and trend[tv]["risk"] >= 0.3:
            headline = f"{LABELS[tv][0]} drifting toward its alert limit"
        else:
            headline = "All vitals normal" if cur else "Waiting for telemetry"

        # ---- feedback for the astronaut / flight surgeon
        fb = [f"{f['title']}: {f['detail']}. Action: {f['action']}." for f in findings]
        for k in sorted(trend, key=lambda k: -trend[k]["risk"]):
            t = trend[k]
            if t["risk"] >= 0.3 and not any(f["vital"] == k for f in findings):
                label, unit = LABELS[k]
                way = "rising" if t["slope_per_min"] > 0 else "falling"
                fb.append(f"{label} is {way} ({t['slope_per_min']:+.1f} {unit}/min); predicted {t['predicted']} {unit} "
                          f"in {self.HORIZON_S} s.")
        if anomalies and not findings:
            fb.append("Unusual reading compared with this crew member's baseline: "
                      + ", ".join(LABELS[k][0] for k in anomalies) + ".")
        if cur and rank == 0:
            fb.insert(0, f"All monitored vitals are within the normal range (HR {cur['hr']:.0f} BPM, "
                         f"SpO2 {cur['spo2']:.1f}%, skin {cur['temp']:.1f} C, suit {cur['psi']:.2f} PSI).")
            pct = cur["cumulative_msv"] / CAREER_LIMIT_MSV * 100
            fb.append(f"Career radiation dose: {cur['cumulative_msv']:.1f} of {CAREER_LIMIT_MSV:.0f} mSv ({pct:.0f}%).")

        # ---- recommended actions
        actions = []
        if rank >= 2 and state in PLAYBOOK:
            actions += PLAYBOOK[state][1]
        for f in findings:
            if f["action"] not in actions:
                actions.append(f["action"])
        if not actions:
            actions = ["Continue nominal EVA operations"]
        actions = actions[:5]

        # ---- health score 0-100
        pens = sorted((6 if f["level"] == "watch" else 20 if f["level"] == "warning" else 40 for f in findings),
                      reverse=True)
        pen = (pens[0] + 0.5 * sum(pens[1:])) if pens else 0.0
        score = int(max(0, min(100, round(100 - pen - 10 * risk))))

        health = {
            "severity": RANK_NAME[rank], "score": score, "state": state, "headline": headline,
            "feedback": fb, "actions": actions, "findings": findings, "error": rank >= 2,
            "name": tr.name or aid, "incident_id": None,
        }
        return {
            "status": "ok" if n >= self.WARMUP_N else "warming_up",
            "have": n, "need": self.WARMUP_N,
            "state": state, "confidence": round(conf, 2), "risk": round(risk, 2),
            "horizon_s": self.HORIZON_S, "anomaly": bool(anomalies), "anomaly_vitals": anomalies,
            "level": level, "trend": trend,
            "vitals": {k: round(x, 2) for k, x in cur.items()} if cur else {},
            "health": health,
        }

    # ----------------------------------------------------------------- incidents
    def _update_incident(self, aid, tr, res, now):
        h = res["health"]
        rank = SEVERITY_RANK[h["severity"]]
        inc = tr.incident
        if rank >= 2:
            tr.ok_streak = 0
            if inc is None:
                inc = self._open_incident(aid, tr, res, now)
                tr.incident = tr.last_incident = inc
                self._reports.append(inc)
                _say(f"[ASTRA][ERROR] {inc['name']}: {inc['headline']}  (report {inc['id']})")
                self._log(inc, "opened")
                log_alert(aid, inc["severity"], f"{inc['headline']} (report {inc['id']})")
            else:
                self._refresh_incident(inc, res, rank, now)
        elif inc is not None:
            tr.ok_streak += 1
            inc["duration_s"] = int(now - inc["_opened_ts"])
            if tr.ok_streak >= self.RESOLVE_AFTER_S:
                inc["status"] = "RESOLVED"
                inc["closed_at"] = _iso(now)
                inc["severity"] = "ok"
                inc["timeline"].append({"t": _iso(now), "event": "Vitals back in normal range - incident resolved"})
                _say(f"[ASTRA][INFO] {inc['name']}: incident {inc['id']} resolved after {inc['duration_s']} s")
                self._log(inc, "resolved")
                log_alert(aid, "ok", f"Incident {inc['id']} resolved after {inc['duration_s']} s")
                tr.incident = None

    def _open_incident(self, aid, tr, res, now):
        h = res["health"]
        self._counter += 1
        state = h["state"]
        cause = PLAYBOOK.get(state, ("Cause could not be determined automatically.", []))[0]
        inc = {
            "id": f"INC-{time.strftime('%Y%m%d', time.localtime(now))}-{self._counter:04d}",
            "astronaut_id": aid, "name": tr.name or aid, "status": "OPEN",
            "severity": h["severity"], "peak_severity": h["severity"], "state": state,
            "headline": h["headline"], "opened_at": _iso(now), "closed_at": None, "duration_s": 0,
            "findings": list(h["findings"]), "snapshot": dict(res["vitals"]),
            "detected": dict(res["vitals"]), "extremes": {},
            "trend": dict(res["trend"]), "probable_cause": cause,
            "recommended_actions": list(h["actions"]),
            "timeline": [{"t": _iso(now), "event": f"Incident opened ({h['severity']}): {h['headline']}"}],
            "_peak_rank": SEVERITY_RANK[h["severity"]], "_opened_ts": now,
        }
        self._track_extremes(inc, res["vitals"])
        return inc

    @staticmethod
    def _track_extremes(inc, vit):
        for k, fn in (("hr", max), ("temp", max), ("neutron", max), ("spo2", min), ("psi", min)):
            x = vit.get(k)
            if x is not None:
                inc["extremes"][k] = x if k not in inc["extremes"] else fn(inc["extremes"][k], x)

    def _refresh_incident(self, inc, res, rank, now):
        h = res["health"]
        inc["duration_s"] = int(now - inc["_opened_ts"])
        inc["severity"] = h["severity"]
        inc["snapshot"] = dict(res["vitals"])
        self._track_extremes(inc, res["vitals"])
        if rank >= inc["_peak_rank"]:  # the report describes the WORST moment, not the recovery
            inc["headline"], inc["findings"] = h["headline"], list(h["findings"])
            inc["recommended_actions"] = list(h["actions"])
            if res["trend"]:
                inc["trend"] = dict(res["trend"])
            if h["state"] != inc["state"]:
                inc["state"] = h["state"]
                inc["probable_cause"] = PLAYBOOK.get(h["state"], (inc["probable_cause"], []))[0]
                inc["timeline"].append({"t": _iso(now), "event": f"Condition changed to {h['state']}: {h['headline']}"})
        if rank > inc["_peak_rank"]:
            inc["_peak_rank"], inc["peak_severity"] = rank, h["severity"]
            inc["timeline"].append({"t": _iso(now), "event": f"ESCALATED to {h['severity'].upper()}: {h['headline']}"})
            _say(f"[ASTRA][ERROR] {inc['name']}: escalated to {h['severity'].upper()} - {h['headline']} "
                 f"(report {inc['id']})")
            self._log(inc, "escalated")
        del inc["timeline"][:-50]

    def _log(self, inc, event):
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"event": event, "report": self._public(inc)}, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            self._warn_once("log", f"[ASTRA][WARN] could not write incident log {self.log_path}: {e!r}")

    # ------------------------------------------------------------------ watchdog
    def _watchdog(self):
        while True:
            time.sleep(1.0)
            try:
                now = time.time()
                with self._lock:
                    for aid, tr in self._tracks.items():
                        if tr.last_push and now - tr.last_push > self.STALE_S:
                            tr.stale = True
                            res = self._stale_result(aid, tr, now)
                            self._update_incident(aid, tr, res, now)
                            res["health"]["incident_id"] = tr.incident["id"] if tr.incident else None
                            tr.result = res
            except Exception as e:  # noqa: BLE001
                self._warn_once("watchdog", f"[ASTRA][ERROR] AI watchdog error: {e!r}")

    def _stale_result(self, aid, tr, now):
        detail = f"no telemetry for {now - tr.last_push:.0f} s"
        if tr.last_error:
            detail += f" (last simulator error: {tr.last_error})"
        fnd = [_finding("telemetry", "telemetry", "critical", "Telemetry lost", detail,
                        "Contact the crew member by voice and check the suit comms link")]
        cur = self._smooth(tr) if tr.valid else None
        return self._build(aid, tr, fnd, cur, {}, 1.0, [], len(tr.valid))


AI = None
AI_ERR = "disabled (ASTRO_AI=0)"
if os.getenv("ASTRO_AI", "1") != "0":
    try:
        AI = AstraAI(app_ramp_tau=RAMP_TAU_S)
    except Exception as _e:  # noqa: BLE001
        AI_ERR = f"AI health monitor failed to start: {_e}"

# Public domain / CC-BY official NASA & Khronos high-detail spacesuit GLB mirrors
GLB_SOURCES = [
    "https://raw.githubusercontent.com/google/model-viewer/master/packages/shared-assets/models/Astronaut.glb",
    "https://modelviewer.dev/shared-assets/models/Astronaut.glb",
]


def ensure_glb_asset():
    """Downloads the high-detail photorealistic NASA Astronaut.glb locally if not already present."""
    if os.path.isfile(MODEL_FILE) and os.path.getsize(MODEL_FILE) > 100_000:
        return
    for url in GLB_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ASTRA-EVA-OS/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = resp.read()
                if len(data) > 100_000:
                    with open(MODEL_FILE, "wb") as f:
                        f.write(data)
                    print(f"[ASTRA] Cached high-detail 3D spacesuit asset -> {MODEL_FILE} ({len(data)//1024} KB)")
                    return
        except Exception:
            continue


@contextmanager
def db():
    with DB_LOCK:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()


def init_db():
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS astronauts (
                id TEXT PRIMARY KEY,
                name TEXT,
                role TEXT,
                scenario TEXT DEFAULT 'Nominal',
                cumulative_msv REAL DEFAULT 142.5
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                astronaut_id TEXT,
                ts REAL,
                level TEXT,
                message TEXT
            );
            """
        )
        # migrate older databases (e.g. astra_spatial.db) that have no 'role' column yet
        cols = [r[1] for r in con.execute("PRAGMA table_info(astronauts)")]
        if "role" not in cols:
            con.execute("ALTER TABLE astronauts ADD COLUMN role TEXT")
        if con.execute("SELECT COUNT(*) FROM astronauts").fetchone()[0] == 0:
            con.executemany(
                "INSERT INTO astronauts (id, name, role, scenario, cumulative_msv) VALUES (?, ?, ?, ?, ?)",
                [
                    ("amira", "Cdr. Amira Hassan", "Mission Commander", "Nominal", 184.2),
                    ("lukas", "FE. Lukas Berg", "Flight Engineer", "Nominal", 112.8),
                    ("yuki", "MS. Yuki Tanaka", "Mission Specialist", "Nominal", 96.4),
                ],
            )
        for aid, role in CREW_ROLES.items():
            con.execute("UPDATE astronauts SET role=? WHERE id=? AND (role IS NULL OR role='')", (role, aid))


def log_alert(aid: str, level: str, message: str):
    """Store an alert row in the alerts table (never raises)."""
    try:
        with db() as con:
            con.execute(
                "INSERT INTO alerts (astronaut_id, ts, level, message) VALUES (?, ?, ?, ?)",
                (aid, time.time(), level, message),
            )
    except Exception:  # noqa: BLE001
        pass


# ============================================================================
# 1b. ACCOUNTS: USER (astronaut), DOCTOR, ADMIN  -  salted PBKDF2 passwords + sessions
#     user   : sees ONLY their own vitals/reports/alerts, can use their own suit fixes
#     doctor : sees ALL crew vitals, AI reports and alerts (read-only, no simulation/suit control)
#     admin  : full access - scenarios, suit fixes, alerts, and user management
# ============================================================================
ACCOUNT_ROLES = ("user", "doctor", "admin")
SESSIONS: dict[str, dict] = {}
SESSION_TTL_S = 8 * 3600
SESSION_LOCK = threading.Lock()
DEFAULT_ACCOUNTS = [  # (username, password, role, astronaut_id, display name)
    ("admin", os.getenv("ASTRO_ADMIN_PASS", "admin123"), "admin", None, "Mission Admin"),
    ("doctor", os.getenv("ASTRO_DOCTOR_PASS", "doctor123"), "doctor", None, "Flight Surgeon"),
    ("amira", os.getenv("ASTRO_USER_PASS", "astro123"), "user", "amira", "Cdr. Amira Hassan"),
    ("lukas", os.getenv("ASTRO_USER_PASS", "astro123"), "user", "lukas", "FE. Lukas Berg"),
    ("yuki", os.getenv("ASTRO_USER_PASS", "astro123"), "user", "yuki", "MS. Yuki Tanaka"),
]


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 120_000)
    return f"{salt}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
        return hmac.compare_digest(hash_password(password, salt), stored)
    except Exception:  # noqa: BLE001
        return False


def init_accounts():
    with db() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                astronaut_id TEXT,
                display_name TEXT
            )
            """
        )
        if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            for username, pw, role, aid, disp in DEFAULT_ACCOUNTS:
                con.execute(
                    "INSERT INTO users (username, password_hash, role, astronaut_id, display_name) VALUES (?, ?, ?, ?, ?)",
                    (username, hash_password(pw), role, aid, disp),
                )
            _say("[ASTRA] Created default accounts (CHANGE THESE PASSWORDS):")
            for username, pw, role, _aid, _d in DEFAULT_ACCOUNTS:
                _say(f"         {role:7} {username} / {pw}")


def login_user(username: str, password: str):
    with db() as con:
        row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row or not check_password(password, row["password_hash"]):
        return None
    token = secrets.token_urlsafe(32)
    sess = {
        "username": row["username"], "role": row["role"], "astronaut_id": row["astronaut_id"],
        "display_name": row["display_name"] or row["username"], "exp": time.time() + SESSION_TTL_S,
    }
    with SESSION_LOCK:
        for t in [t for t, v in SESSIONS.items() if v["exp"] < time.time()]:
            SESSIONS.pop(t, None)
        SESSIONS[token] = sess
    return token, sess


def session_for(token: str | None):
    if not token:
        return None
    with SESSION_LOCK:
        sess = SESSIONS.get(token)
        if not sess or sess["exp"] < time.time():
            SESSIONS.pop(token, None)
            return None
        sess["exp"] = time.time() + SESSION_TTL_S
        return sess


def visible_crew_ids(sess: dict) -> list:
    """Which astronauts this account may see."""
    if sess["role"] in ("admin", "doctor"):
        with db() as con:
            return [r["id"] for r in con.execute("SELECT id FROM astronauts ORDER BY rowid")]
    return [sess["astronaut_id"]] if sess.get("astronaut_id") else []


def can_view(sess: dict, aid: str) -> bool:
    return sess["role"] in ("admin", "doctor") or sess.get("astronaut_id") == aid


def can_control(sess: dict, aid: str) -> bool:
    """Suit-fix overrides: admin on anyone, a user on their own suit. Doctors are read-only."""
    return sess["role"] == "admin" or (sess["role"] == "user" and sess.get("astronaut_id") == aid)


def get_overrides(aid: str) -> dict:
    return OVERRIDES.setdefault(
        aid, {"o2_boost": False, "cool_max": False, "shield_shelter": False}
    )


def analyze_ppg(red: list[float], ir: list[float], fs: int = 100):
    n = len(ir)
    win_s, win_f = max(3, int(fs * 0.6)), max(2, int(fs * 0.08))
    half_s, half_f = win_s // 2, win_f // 2

    detrended = [
        ir[i] - sum(ir[max(0, i - half_s) : min(n, i + half_s + 1)]) / (min(n, i + half_s + 1) - max(0, i - half_s))
        for i in range(n)
    ]
    filtered = [
        round(
            sum(detrended[max(0, i - half_f) : min(n, i + half_f + 1)])
            / (min(n, i + half_f + 1) - max(0, i - half_f)),
            2,
        )
        for i in range(n)
    ]

    mean_f = sum(filtered) / n
    std_f = math.sqrt(sum((x - mean_f) ** 2 for x in filtered) / n)
    if std_f < 1e-6:
        return 64.0, 98.6, filtered, False  # no usable pulse: values are placeholders

    min_dist = int(fs * 0.32)
    thresh = 0.45 * std_f
    peaks = []
    for i in range(1, n - 1):
        if filtered[i] > thresh and filtered[i] >= filtered[i - 1] and filtered[i] > filtered[i + 1]:
            if not peaks or (i - peaks[-1]) >= min_dist:
                peaks.append(i)

    if len(peaks) < 2:
        return 64.0, 98.6, filtered, False  # no usable pulse: values are placeholders

    rr_ms = [(peaks[i + 1] - peaks[i]) / fs * 1000.0 for i in range(len(peaks) - 1)]
    hr = max(42.0, min(205.0, 60000.0 / (sum(rr_ms) / len(rr_ms))))

    dc_r, dc_i = max(1.0, sum(red) / n), max(1.0, sum(ir) / n)
    ac_r = math.sqrt(sum((x - dc_r) ** 2 for x in red) / n)
    ac_i = math.sqrt(sum((x - dc_i) ** 2 for x in ir) / n)
    r_val = (ac_r / dc_r) / max(1e-6, (ac_i / dc_i))
    spo2 = max(72.0, min(99.8, 110.0 - 25.0 * r_val))
    return hr, spo2, filtered, True


def ease_state(aid: str, target):
    """First-order lag toward the scenario target (radiation reacts ~5x faster than physiology)."""
    cur = TRUE_STATE.get(aid)
    if cur is None or RAMP_TAU_S <= 0:
        cur = list(target)
    else:
        a = 1.0 - math.exp(-1.0 / RAMP_TAU_S)
        a_n = 1.0 - math.exp(-1.0 / max(1.0, RAMP_TAU_S / 5.0))
        cur = [c + (t - c) * (a_n if i == 4 else a) for i, (c, t) in enumerate(zip(cur, target))]
    TRUE_STATE[aid] = cur
    return tuple(cur)


def step_simulation(aid: str, scenario: str):
    ov = get_overrides(aid)
    targets = {
        "Nominal": (64.0, 98.6, 36.6, 4.30, 6.2),
        "Solar Storm": (134.0, 96.2, 37.3, 4.30, 148.0),
        "Suit Leak": (154.0, 86.0, 36.8, 2.75, 12.0),
        "Thermal Spike": (142.0, 96.5, 38.6, 4.30, 8.5),
    }
    hr_t, spo2_t, temp_t, psi_t, neut_t = targets.get(scenario, targets["Nominal"])

    if ov.get("shield_shelter"):
        neut_t, hr_t = 5.4, min(hr_t, 76.0)
    if ov.get("o2_boost"):
        spo2_t, psi_t, hr_t = 99.1, 4.30, min(hr_t, 78.0)
    if ov.get("cool_max"):
        temp_t, hr_t = 36.5, min(hr_t, 76.0)

    hr_t, spo2_t, temp_t, psi_t, neut_t = ease_state(aid, (hr_t, spo2_t, temp_t, psi_t, neut_t))

    hr_now = hr_t + random.gauss(0, 1.0)
    spo2_now = max(72.0, min(99.8, spo2_t + random.gauss(0, 0.16)))
    temp_now = temp_t + random.gauss(0, 0.04)
    psi_now = psi_t + random.gauss(0, 0.01)
    neut_now = max(0.5, neut_t + random.gauss(0, neut_t * 0.03))

    fs, n = 100, 260
    freq = hr_now / 60.0
    r_ratio = max(0.4, (110.0 - spo2_now) / 25.0)
    ir, red = [], []
    for i in range(n):
        t = i / fs
        pulse = math.sin(2 * math.pi * freq * t) + 0.35 * math.sin(4 * math.pi * freq * t - 0.6)
        ir.append(10000.0 + 180.0 * pulse + random.gauss(0, 5.5))
        red.append(10000.0 + (180.0 * r_ratio) * pulse + random.gauss(0, 5.5))

    hr_est, spo2_est, filtered, sig_ok = analyze_ppg(red, ir, fs)

    with db() as con:
        row = con.execute("SELECT name, role, cumulative_msv FROM astronauts WHERE id=?", (aid,)).fetchone()
        cum_msv = float(row["cumulative_msv"] if row else 142.5) + (neut_now / 1000.0) * (15.0 / 3600.0)
        con.execute("UPDATE astronauts SET cumulative_msv=? WHERE id=?", (cum_msv, aid))
        crew_name = row["name"] if row else aid
        crew_role = (row["role"] if row else None) or CREW_ROLES.get(aid, "")

    LAST[aid] = {
        "name": crew_name,
        "role": crew_role,
        "signal_ok": bool(sig_ok),
        "scenario": scenario,
        "hr": round(hr_est, 1),
        "spo2": round(spo2_est, 1),
        "temp": round(temp_now, 1),
        "psi": round(psi_now, 2),
        "neutron": round(neut_now, 1),
        "cumulative_msv": round(cum_msv, 2),
    }
    PPG[aid] = filtered
    if AI is not None:
        try:
            AI.push(aid, LAST[aid])
        except Exception:  # noqa: BLE001  the AI must never be able to stall the simulation
            pass


def worker():
    time.sleep(0.2)
    last_err: dict = {}
    while True:
        try:
            with db() as con:
                crew = [dict(r) for r in con.execute("SELECT id, scenario FROM astronauts")]
        except Exception as e:  # noqa: BLE001
            print(f"[ASTRA][ERROR] database read failed: {e!r}", flush=True)
            time.sleep(2.0)
            continue
        for m in crew:
            try:
                step_simulation(m["id"], m["scenario"])
            except Exception as e:  # noqa: BLE001  one bad step must not kill the telemetry thread
                msg = f"{type(e).__name__}: {e}"
                if AI is not None:
                    AI.note_error(m["id"], msg)
                key = (m["id"], type(e).__name__)
                if time.time() - last_err.get(key, 0.0) > 10:
                    last_err[key] = time.time()
                    print(f"[ASTRA][ERROR] simulation step failed for {m['id']}: {msg}", flush=True)
                    traceback.print_exc()
        time.sleep(1.0)


# ============================================================================
# 2. HTTP SERVER
# ============================================================================
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, data, status=200):
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        ln = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(ln).decode("utf-8")) if ln else {}

    def _guard(self, fn):
        try:
            fn()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except ValueError as e:  # bad JSON body etc.
            try:
                self._json({"error": f"bad request: {e}"}, 400)
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001
            print(f"[ASTRA][ERROR] HTTP {self.command} {self.path}: {e!r}", flush=True)
            traceback.print_exc()
            try:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            except Exception:  # noqa: BLE001
                pass

    def _session(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "astra_session":
                return session_for(v)
        return None

    def _require(self, *roles):
        sess = self._session()
        if sess is None:
            self._json({"error": "login required"}, 401)
            return None
        if roles and sess["role"] not in roles:
            self._json({"error": "your account type is not allowed to do this"}, 403)
            return None
        return sess

    def do_GET(self):
        self._guard(self._get)

    def do_PUT(self):
        self._guard(self._put)

    def do_POST(self):
        self._guard(self._post)

    def do_DELETE(self):
        self._guard(self._delete)

    def _post(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            body = self._body()
            res = login_user(str(body.get("username", "")).strip().lower(), str(body.get("password", "")))
            if res is None:
                return self._json({"error": "wrong username or password"}, 401)
            token, sess = res
            raw = json.dumps({"ok": True, "me": {k: sess[k] for k in ("username", "role", "astronaut_id", "display_name")}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", f"astra_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL_S}")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/api/logout":
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "astra_session":
                    with SESSION_LOCK:
                        SESSIONS.pop(v, None)
            raw = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "astra_session=; Path=/; HttpOnly; Max-Age=0")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/api/users":
            if self._require("admin") is None:
                return
            b = self._body()
            username = str(b.get("username", "")).strip().lower()
            password = str(b.get("password", ""))
            role = b.get("role")
            aid = (b.get("astronaut_id") or None) if role == "user" else None
            if not username or not username.replace("_", "").replace("-", "").replace(".", "").isalnum():
                return self._json({"error": "username must be letters/numbers only"}, 400)
            if len(password) < 6:
                return self._json({"error": "password must be at least 6 characters"}, 400)
            if role not in ACCOUNT_ROLES:
                return self._json({"error": "role must be user, doctor or admin"}, 400)
            with db() as con:
                if con.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                    return self._json({"error": "that username already exists"}, 409)
                if role == "user" and not con.execute("SELECT 1 FROM astronauts WHERE id=?", (aid,)).fetchone():
                    return self._json({"error": "a user account must be linked to an existing astronaut id"}, 400)
                con.execute(
                    "INSERT INTO users (username, password_hash, role, astronaut_id, display_name) VALUES (?, ?, ?, ?, ?)",
                    (username, hash_password(password), role, aid, str(b.get("display_name") or username)),
                )
            return self._json({"ok": True})
        self._json({"error": "not found"}, 404)

    def _delete(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "users"]:
            sess = self._require("admin")
            if sess is None:
                return
            target = parts[2]
            if target == sess["username"]:
                return self._json({"error": "you cannot delete your own account"}, 400)
            with db() as con:
                con.execute("DELETE FROM users WHERE username=?", (target,))
            with SESSION_LOCK:
                for t in [t for t, v in SESSIONS.items() if v["username"] == target]:
                    SESSIONS.pop(t, None)
            return self._json({"ok": True})
        self._json({"error": "not found"}, 404)

    def _get(self):
        path = urlparse(self.path).path
        if path == "/" or path.startswith("/static/"):
            rel = "index.html" if path == "/" else path[len("/static/"):]
            full = os.path.realpath(os.path.join(STATIC_DIR, rel))
            # block ../ path tricks: the file must live inside the static folder
            if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep) or not os.path.isfile(full):
                return self._json({"error": "not found"}, 404)
            with open(full, "rb") as f:
                raw = f.read()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES.get(os.path.splitext(full)[1].lower(), "application/octet-stream"))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if path == "/models/astronaut.glb":
            if os.path.isfile(MODEL_FILE) and os.path.getsize(MODEL_FILE) > 100_000:
                with open(MODEL_FILE, "rb") as f:
                    raw = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "model/gltf-binary")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            return self._json({"error": "No local astronaut.glb found"}, 404)

        if path == "/api/me":
            sess = self._session()
            if sess is None:
                return self._json({"error": "login required"}, 401)
            return self._json({"me": {k: sess[k] for k in ("username", "role", "astronaut_id", "display_name")},
                               "crew": visible_crew_ids(sess)})

        if path == "/api/users":
            if self._require("admin") is None:
                return
            with db() as con:
                rows = [dict(r) for r in con.execute(
                    "SELECT username, role, astronaut_id, display_name FROM users ORDER BY role, username")]
            return self._json({"users": rows})

        if path == "/api/alerts":
            sess = self._require()
            if sess is None:
                return
            with db() as con:
                if sess["role"] in ("admin", "doctor"):
                    rows = con.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT 100").fetchall()
                else:
                    rows = con.execute("SELECT * FROM alerts WHERE astronaut_id=? ORDER BY id DESC LIMIT 100",
                                       (sess.get("astronaut_id"),)).fetchall()
            return self._json({"alerts": [dict(r) for r in rows]})

        if path.startswith("/api/state/"):
            sess = self._require()
            if sess is None:
                return
            aid = path.split("/")[-1]
            if not can_view(sess, aid):
                return self._json({"error": "you can only view your own data"}, 403)
            ai = AI.get(aid) if AI is not None else {"status": "unavailable", "reason": AI_ERR}
            return self._json({"vitals": LAST.get(aid), "ppg": PPG.get(aid, []), "ai": ai})

        if path == "/api/ai/crew":
            sess = self._require()
            if sess is None:
                return
            ok = set(visible_crew_ids(sess))
            return self._json({"crew": [c for c in (AI.crew() if AI is not None else []) if c["id"] in ok]})

        if path == "/api/ai/reports":
            sess = self._require()
            if sess is None:
                return
            ok = set(visible_crew_ids(sess))
            return self._json({"reports": [r for r in (AI.reports() if AI is not None else [])
                                           if r.get("astronaut_id") in ok]})

        if path.startswith("/api/ai/report/"):
            sess = self._require()
            if sess is None:
                return
            aid = path.split("/")[-1]
            if not can_view(sess, aid):
                return self._json({"error": "you can only view your own data"}, 403)
            rep = AI.latest_report(aid) if AI is not None else None
            return self._json({"report": rep})

        if path == "/api/ai/status":
            if self._require() is None:
                return
            return self._json(AI.info() if AI is not None else {"status": "unavailable", "reason": AI_ERR})

        self._json({"error": "not found"}, 404)

    def _put(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "crew"] and parts[3] == "scenario":
            aid = parts[2]
            if self._require("admin") is None:  # only admins can inject simulated emergencies
                return
            sc = self._body().get("scenario", "Nominal")
            if sc not in SCENARIOS:
                return self._json({"error": f"unknown scenario {sc!r}", "allowed": list(SCENARIOS)}, 400)
            with db() as con:
                con.execute("UPDATE astronauts SET scenario=? WHERE id=?", (sc, aid))
            if sc == "Nominal":
                OVERRIDES[aid] = {"o2_boost": False, "cool_max": False, "shield_shelter": False}
            return self._json({"ok": True, "scenario": sc})

        if len(parts) == 4 and parts[:2] == ["api", "crew"] and parts[3] == "overrides":
            aid = parts[2]
            sess = self._require("admin", "user")
            if sess is None:
                return
            if not can_control(sess, aid):
                return self._json({"error": "you can only control your own suit"}, 403)
            body = self._body()
            ov = get_overrides(aid)
            for k in ("o2_boost", "cool_max", "shield_shelter"):
                if k in body:
                    ov[k] = bool(body[k])
            with db() as con:
                row = con.execute("SELECT scenario FROM astronauts WHERE id=?", (aid,)).fetchone()
                sc = row["scenario"] if row else "Nominal"
                resolved = (
                    (sc == "Solar Storm" and ov.get("shield_shelter"))
                    or (sc == "Suit Leak" and ov.get("o2_boost"))
                    or (sc == "Thermal Spike" and ov.get("cool_max"))
                )
                if resolved:
                    con.execute("UPDATE astronauts SET scenario='Nominal' WHERE id=?", (aid,))
                    OVERRIDES[aid] = {"o2_boost": False, "cool_max": False, "shield_shelter": False}
            return self._json({"ok": True, "resolved": resolved})

        self._json({"error": "not found"}, 404)


# ============================================================================
# 3. FRONT-END FILES (served from ./static next to this script)
#      static/index.html  -> page structure
#      static/style.css   -> all styling
#      static/app.js      -> all browser logic (3D scene, HUD, login, admin panel)
# ============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def main():
    ensure_glb_asset()
    init_db()
    init_accounts()
    threading.Thread(target=worker, daemon=True).start()
    if AI is not None:
        AI.start()  # own thread; imports sklearn there so the UI never waits for it
    port = int(os.getenv("PORT", "8050"))
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"[ASTRA][ERROR] cannot start server on port {port}: {e}. "
              f"Close the other copy of the app or run with PORT=8051", flush=True)
        raise SystemExit(1)
    print("[ASTRA] AI health monitor: " + ("ACTIVE" if AI is not None else f"OFF ({AI_ERR})"), flush=True)
    url = f"http://127.0.0.1:{port}"
    print(f"ASTRA Orbital EVA OS running at {url}")
    threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()