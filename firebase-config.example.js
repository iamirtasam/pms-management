// ═══════════════════════════════════════════════════════════════
//  EMS PORTAL — Firebase Configuration (TEMPLATE)
//
//  1. Copy this file to  firebase-config.js
//  2. Fill in your Firebase project's web app credentials
//     (Firebase console → Project settings → Your apps)
//  3. firebase-config.js is gitignored and will never be committed.
// ═══════════════════════════════════════════════════════════════

export const FIREBASE_CONFIG = {
  apiKey:            "YOUR_API_KEY",
  authDomain:        "YOUR_PROJECT.firebaseapp.com",
  projectId:         "YOUR_PROJECT_ID",
  storageBucket:     "YOUR_PROJECT.firebasestorage.app",
  messagingSenderId: "YOUR_SENDER_ID",
  appId:             "YOUR_APP_ID"
};

// ── Firestore Collection Names ──────────────────────────────────
export const COLLECTIONS = {
  DOCTORS:    "doctors",       // doctor accounts
  ADMINS:     "admins",        // sub-admin accounts
  BLACKLIST:  "blacklist",     // blacklisted patients
  ATTENDANCE: "attendance",    // attendance records
  BONUSES:    "bonuses",       // bonus records
  STRIKES:    "strikes",       // strike records
  CONFIG:          "config",           // master admin credentials
  BOT_CONFIG:      "bot_config",       // selfbot runtime config (managed bot admins)
  LOGS:            "logs",             // activity logs
  RULES_CATEGORIES:"rules_categories", // rules category headings
  RULES_ITEMS:     "rules_items",      // individual rule entries
  TRAINING_CATEGORIES: "training_categories", // training category headings
  TRAINING_ITEMS:      "training_items",      // individual training entries
};

// ── Log action types ────────────────────────────────────────────
export const LOG_ACTIONS = {
  LOGIN_SUCCESS:   "LOGIN_SUCCESS",
  LOGIN_FAILED:    "LOGIN_FAILED",
  LOGOUT:          "LOGOUT",
  BLACKLIST_ADD:   "BLACKLIST_ADD",
  BLACKLIST_REMOVE:"BLACKLIST_REMOVE",
  DOCTOR_CREATE:   "DOCTOR_CREATE",
  DOCTOR_DELETE:   "DOCTOR_DELETE",
  DOCTOR_TOGGLE:   "DOCTOR_TOGGLE",
  BONUS_ADD:       "BONUS_ADD",
  BONUS_DELETE:    "BONUS_DELETE",
  BONUS_PAID:      "BONUS_PAID",
  BONUS_EDIT:      "BONUS_EDIT",
  STRIKE_ADD:      "STRIKE_ADD",
  STRIKE_DELETE:   "STRIKE_DELETE",
  ATTENDANCE_MARK: "ATTENDANCE_MARK",
  ADMIN_CREATE:    "ADMIN_CREATE",
  ADMIN_DELETE:    "ADMIN_DELETE",
  RULE_CREATE:     "RULE_CREATE",
  RULE_DELETE:     "RULE_DELETE",
  RULE_REORDER:    "RULE_REORDER",
  RULE_EDIT:       "RULE_EDIT",
  DOCTOR_ROLE_CHANGE: "DOCTOR_ROLE_CHANGE",
};

// ── Bonus Categories ────────────────────────────────────────────
export const BONUS_CATEGORIES = [
  "Weekly Bonus",
  "Event Prize",
  "Performance Bonus",
  "Attendance Bonus",
  "Other",
];

// ── Attendance Status Options ───────────────────────────────────
export const ATTENDANCE_STATUS = {
  PRESENT:  "present",
  ABSENT:   "absent",
  LATE:     "late",
  OFF:      "off",
};

// ── Week helpers (Fri → Thu) ───────────────────────────────────
export function getCurrentWeekRange() {
  const now = new Date();
  const day = now.getDay(); // 0=Sun,1=Mon,...,5=Fri,6=Sat
  // Days since last Friday
  const diffToFri = (day + 2) % 7; // Fri=5 → 0, Sat=6 → 1, Sun=0 → 2, ...
  const friday = new Date(now);
  friday.setDate(now.getDate() - diffToFri);
  friday.setHours(0, 0, 0, 0);
  const thursday = new Date(friday);
  thursday.setDate(friday.getDate() + 6);
  thursday.setHours(23, 59, 59, 999);
  return { start: friday, end: thursday };
}

export function getWeekDays(weekStart) {
  const days = [];
  for (let i = 0; i < 7; i++) {
    const d = new Date(weekStart);
    d.setDate(weekStart.getDate() + i);
    days.push(d);
  }
  return days;
}

export function dateKey(date) {
  // Build YYYY-MM-DD from LOCAL date parts. Never use toISOString() here:
  // it converts to UTC, which shifts the day back for UTC+ timezones (e.g.
  // Pakistan, UTC+5) and made every grid cell display records one day late.
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}
