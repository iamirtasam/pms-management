// ═══════════════════════════════════════════════════════════════
//  EMS PORTAL — Shared Auth & Firebase Helpers
// ═══════════════════════════════════════════════════════════════

import { FIREBASE_CONFIG, COLLECTIONS, LOG_ACTIONS } from './firebase-config.js';
import { initializeApp }       from "https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js";
import {
  getFirestore,
  collection, doc,
  getDoc, getDocs, addDoc, setDoc, updateDoc, deleteDoc,
  query, where, orderBy
} from "https://www.gstatic.com/firebasejs/10.12.0/firebase-firestore.js";

// ── Firebase Init (singleton) ─────────────────────────────────
let _app, _db;
export function getDB() {
  if (!_db) {
    _app = initializeApp(FIREBASE_CONFIG);
    _db  = getFirestore(_app);
  }
  return _db;
}

// ── Session ────────────────────────────────────────────────────
// Two modes:
//   • Normal  → sessionStorage (clears when the tab/browser closes)
//   • Remember→ localStorage with a 10-day expiry (survives restarts)
const SESSION_KEY = 'ems_session';
const REMEMBER_DAYS = 10;

export function saveSession(data) {
  // Persist to localStorage with an expiry when "remember me" was checked,
  // otherwise keep it tab-scoped in sessionStorage. Only one is ever set.
  if (data && data.remember) {
    const payload = { ...data, expiry: Date.now() + REMEMBER_DAYS * 24 * 60 * 60 * 1000 };
    localStorage.setItem(SESSION_KEY, JSON.stringify(payload));
    sessionStorage.removeItem(SESSION_KEY);
  } else {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify(data));
    localStorage.removeItem(SESSION_KEY);
  }
}

export function getSession() {
  // 1. Current-tab session takes priority.
  try {
    const s = sessionStorage.getItem(SESSION_KEY);
    if (s) return JSON.parse(s);
  } catch { /* ignore */ }

  // 2. Fall back to a remembered (localStorage) session if not expired.
  try {
    const l = localStorage.getItem(SESSION_KEY);
    if (l) {
      const p = JSON.parse(l);
      if (!p.expiry || Date.now() < p.expiry) return p;
      localStorage.removeItem(SESSION_KEY); // expired → discard
    }
  } catch { localStorage.removeItem(SESSION_KEY); }

  return null;
}

export function clearSession() {
  sessionStorage.removeItem(SESSION_KEY);
  localStorage.removeItem(SESSION_KEY);
  setSwitchAlt(null); // drop any linked account-switch slot on full logout
}

// ── Account switching (admin ⇄ their doctor account) ───────────
// The "alt" slot holds the *inactive* counterpart session so an admin can
// toggle between their admin and doctor accounts without re-logging-in.
const SWITCH_KEY = 'ems_switch';

export function getSwitchAlt() {
  try {
    const s = sessionStorage.getItem(SWITCH_KEY) || localStorage.getItem(SWITCH_KEY);
    return s ? JSON.parse(s) : null;
  } catch { return null; }
}

export function setSwitchAlt(data) {
  if (!data) {
    sessionStorage.removeItem(SWITCH_KEY);
    localStorage.removeItem(SWITCH_KEY);
    return;
  }
  // Mirror the active session's persistence so the link survives (or not) the
  // same way the login does.
  if (data.remember) {
    localStorage.setItem(SWITCH_KEY, JSON.stringify(data));
    sessionStorage.removeItem(SWITCH_KEY);
  } else {
    sessionStorage.setItem(SWITCH_KEY, JSON.stringify(data));
    localStorage.removeItem(SWITCH_KEY);
  }
}

// Swap active ⇄ alt. Returns the new active session, or null if there's no alt.
export function switchAccount() {
  const current = getSession();
  const alt     = getSwitchAlt();
  if (!alt) return null;
  setSwitchAlt(current);
  saveSession(alt);
  return alt;
}

// ── Guard helpers ─────────────────────────────────────────────
export function requireDoctor() {
  const s = getSession();
  if (!s || (s.role !== 'doctor' && s.role !== 'trainer')) {
    window.location.href = 'login.html';
    return null;
  }
  return s;
}

export function requireAdmin() {
  const s = getSession();
  if (!s || s.role !== 'admin') {
    window.location.href = 'login.html';
    return null;
  }
  return s;
}

// ── Doctor Auth ───────────────────────────────────────────────
export async function loginDoctor(username, password) {
  const db = getDB();
  const q  = query(
    collection(db, COLLECTIONS.DOCTORS),
    where('username', '==', username)
  );
  const snap = await getDocs(q);
  if (snap.empty) return { ok: false, msg: 'Username not found.' };
  const docSnap = snap.docs[0];
  const data = docSnap.data();
  if (data.password !== password) return { ok: false, msg: 'Incorrect password.' };
  if (data.disabled) return { ok: false, msg: 'Account is disabled. Contact admin.' };
  return { ok: true, session: { role: data.role === 'trainer' ? 'trainer' : 'doctor', id: docSnap.id, username: data.username, name: data.name } };
}

// ── Admin Auth (master + sub-admins) ─────────────────────────
export async function loginAdmin(username, password) {
  const db = getDB();

  // 1. Check master admin first (config/admin document)
  const masterSnap = await getDoc(doc(db, COLLECTIONS.CONFIG, 'admin'));
  if (!masterSnap.exists()) return { ok: false, msg: 'Admin config not set up in Firebase.' };
  const master = masterSnap.data();
  if (master.adminUser === username && master.adminPassword === password) {
    return { ok: true, session: { role: 'admin', isMaster: true, username, name: username } };
  }

  // 2. Check sub-admins collection
  const q    = query(collection(db, COLLECTIONS.ADMINS), where('username', '==', username));
  const snap = await getDocs(q);
  if (!snap.empty) {
    const subDoc  = snap.docs[0];
    const subData = subDoc.data();
    if (subData.password !== password) return { ok: false, msg: 'Incorrect credentials.' };
    if (subData.disabled) return { ok: false, msg: 'This admin account has been disabled.' };
    return { ok: true, session: { role: 'admin', isMaster: false, id: subDoc.id, username: subData.username, name: subData.name || subData.username } };
  }

  return { ok: false, msg: 'Incorrect credentials.' };
}

// ── Firestore CRUD helpers ─────────────────────────────────────
export async function fsGetAll(col) {
  const db   = getDB();
  const snap = await getDocs(collection(db, col));
  return snap.docs.map(d => ({ id: d.id, ...d.data() }));
}

export async function fsGetDoc(col, id) {
  const db   = getDB();
  const snap = await getDoc(doc(db, col, id));
  return snap.exists() ? { id: snap.id, ...snap.data() } : null;
}

export async function fsAdd(col, data) {
  const db  = getDB();
  const ref = await addDoc(collection(db, col), data);
  return ref.id;
}

export async function fsSet(col, id, data) {
  const db = getDB();
  await setDoc(doc(db, col, id), data);
}

export async function fsUpdate(col, id, data) {
  const db = getDB();
  await updateDoc(doc(db, col, id), data);
}

export async function fsDelete(col, id) {
  const db = getDB();
  await deleteDoc(doc(db, col, id));
}

export async function fsQuery(col, field, op, value) {
  const db   = getDB();
  const q    = query(collection(db, col), where(field, op, value));
  const snap = await getDocs(q);
  return snap.docs.map(d => ({ id: d.id, ...d.data() }));
}

// ── Toast (global) ─────────────────────────────────────────────
export function showToast(msg, type = 'info') {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg;
  t.className = `toast ${type} show`;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove('show'), 3600);
}

// ── Escape HTML ────────────────────────────────────────────────
export function esc(s) {
  return String(s ?? '')
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

// ── Date formatting ────────────────────────────────────────────
export function fmtDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('en-US', { year:'numeric', month:'short', day:'numeric' });
}

export function fmtMoney(n) {
  return '$' + Number(n || 0).toLocaleString();
}

// ── Activity Logger ────────────────────────────────────────────
// Gets IP via a free public API (best-effort, may be approximate)
let _cachedIP = null;
async function getIP() {
  if (_cachedIP) return _cachedIP;
  try {
    const r = await fetch('https://api.ipify.org?format=json');
    const d = await r.json();
    _cachedIP = d.ip;
    return _cachedIP;
  } catch { return 'unknown'; }
}

export async function writeLog(action, details = {}) {
  try {
    const db      = getDB();
    const session = getSession();
    const ip      = await getIP();
    const entry   = {
      action,
      ip,
      userAgent:  navigator.userAgent,
      timestamp:  new Date().toISOString(),
      actorRole:  session?.role  || details.role  || 'unknown',
      actorName:  session?.name  || details.name  || session?.username || details.username || 'unknown',
      actorId:    session?.id    || details.id     || 'unknown',
      ...details,
    };
    await addDoc(collection(db, COLLECTIONS.LOGS), entry);
  } catch(e) {
    // Logging should never break the app
    console.warn('Log write failed:', e);
  }
}

export { LOG_ACTIONS };
