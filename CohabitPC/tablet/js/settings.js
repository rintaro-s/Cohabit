// 設定 (この端末の localStorage にだけ残す)
const KEY = 'cohabit.tablet.settings.v1';

export const DEFAULTS = {
  address: '',        // 空 = このページと同じ PC
  token: '',
  fingerDraw: false,  // 指でも書く
  penOnly: 'auto',    // ペンだけ (指は全部無視): 'auto' = iPad でペンを一度使ったら / 'on' / 'off'
  rowHeight: 72,      // ページの単位 (ページ幅 1000)
  textSize: 'm',
  voice: true,
  sister: true,
  autoCheck: true,
};

let current = { ...DEFAULTS };
const listeners = new Set();

export function loadSettings() {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) current = { ...DEFAULTS, ...JSON.parse(raw) };
  } catch { /* 読めなければ既定 */ }
  // ?token= を URL に付けて開いたら覚える (QR から開く用)
  try {
    const q = new URLSearchParams(location.search);
    if (q.get('token')) current.token = q.get('token');
  } catch { /* ignore */ }
  return current;
}

export function settings() { return current; }

export function saveSettings(patch) {
  current = { ...current, ...patch };
  try { localStorage.setItem(KEY, JSON.stringify(current)); } catch { /* 保存できなくても動く */ }
  for (const fn of listeners) fn(current, patch);
}

export function onSettings(fn) { listeners.add(fn); return () => listeners.delete(fn); }

/** PC の URL (末尾の / なし)。空ならこのページと同じ所 */
export function baseUrl() {
  let a = (current.address || '').trim();
  if (!a) return '';
  if (!/^https?:\/\//i.test(a)) a = 'http://' + a;
  return a.replace(/\/+$/, '');
}

export function isCrossOrigin() {
  const b = baseUrl();
  if (!b) return false;
  try { return new URL(b).origin !== location.origin; } catch { return false; }
}

/** パス (/tablet/...) を絶対 URL にし、必要ならトークンを付ける (SSE・音声・VRM 用) */
export function resolveUrl(path, { withToken = false } = {}) {
  if (!path) return path;
  let url = /^(https?:|data:|blob:)/i.test(path) ? path : baseUrl() + (path.startsWith('/') ? path : '/' + path);
  if (withToken && current.token && !/^(data:|blob:)/i.test(url) && !/[?&]token=/.test(url)) {
    url += (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(current.token);
  }
  return url;
}
