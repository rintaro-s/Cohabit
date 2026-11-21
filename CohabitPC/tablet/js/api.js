// PC (cohabit_pc.py) との窓口。形は TABLET_API.md のとおり
import { settings, resolveUrl } from './settings.js';

const listeners = new Set();
let online = null;

function setOnline(v, why) {
  if (online === v) return;
  online = v;
  for (const fn of listeners) fn(v, why);
}
export function onConnection(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function isOnline() { return online !== false; }

export class ApiError extends Error {
  constructor(message, { status = 0, network = false, code = null, say = null } = {}) {
    super(message);
    this.status = status;
    this.network = network;
    this.code = code;  // 例: "no_records" (苦手から出したいが学習記録が無い)・"llm_down"
    this.say = say;    // PC が付けた、妹が言えるひとこと (無ければ null)
  }
}

async function request(method, path, body, { timeout = 60000 } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  const headers = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const token = settings().token;
  if (token) headers['X-Cohabit-Token'] = token;
  let res;
  try {
    res = await fetch(resolveUrl(path), {
      method, headers, signal: ctrl.signal, cache: 'no-store',
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    clearTimeout(timer);
    setOnline(false, 'network');
    throw new ApiError(e?.name === 'AbortError' ? 'timeout' : 'network', { network: true });
  }
  clearTimeout(timer);
  setOnline(true);
  if (res.status === 204) return null;
  let data = null;
  const text = await res.text();
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!res.ok) {
    const err = data && data.error;
    const msg = (err && (typeof err === 'string' ? err : err.message)) || (data && data.message) || `HTTP ${res.status}`;
    if (res.status === 401) setOnline(false, 'token');
    throw new ApiError(msg, { status: res.status, code: (err && typeof err === 'object' && err.code) || null, say: (data && data.say) || null });
  }
  return data;
}

export const api = {
  session: () => request('GET', '/tablet/api/session', undefined, { timeout: 8000 }),
  /** { from: 'topic'|'photo'|'weak'|'next', topic?, image?, level? } → { problem, say }。weak で学習記録が無ければ 409 (code: no_records) */
  problem: (req) => request('POST', '/tablet/api/problem', req, { timeout: 120000 }),
  /** { problemId, trigger, page, rows, changedRows } → { marks, solved, say, stuck } */
  check: (req) => request('POST', '/tablet/api/check', req, { timeout: 120000 }),
  /** { problemId, question?, page?, level } → { say, marks? } */
  ask: (req) => request('POST', '/tablet/api/ask', req, { timeout: 120000 }),
};

/** 音声の URL。Say.audio が無ければ /tablet/api/speech を自分で組み立てる (無ければ 204 が返る) */
export function speechUrl(say) {
  const path = say.audio || `/tablet/api/speech?text=${encodeURIComponent(say.text || '')}&expression=${encodeURIComponent(say.expression || 'neutral')}`;
  return resolveUrl(path, { withToken: true });
}

/** Server-Sent Events。切れたら少し待ってつなぎ直す */
export function connectEvents(handlers) {
  let es = null;
  let retry = 1000;
  let closed = false;
  let timer = null;
  const open = () => {
    if (closed) return;
    try { es?.close(); } catch { /* ignore */ }
    es = new EventSource(resolveUrl('/tablet/api/events', { withToken: true }));
    es.onopen = () => { retry = 1000; setOnline(true); handlers.open?.(); };
    es.onerror = () => {
      // EventSource は自分でもつなぎ直すが、CLOSED になったら自分でやる
      if (es.readyState === EventSource.CLOSED) {
        clearTimeout(timer);
        timer = setTimeout(open, retry);
        retry = Math.min(retry * 2, 15000);
      }
      handlers.error?.();
    };
    for (const name of ['presence', 'say', 'problem']) {
      es.addEventListener(name, (ev) => {
        let data = null;
        try { data = JSON.parse(ev.data); } catch { return; }
        handlers[name]?.(data);
      });
    }
  };
  open();
  return {
    reconnect() { retry = 1000; open(); },
    close() { closed = true; clearTimeout(timer); try { es?.close(); } catch { /* ignore */ } },
  };
}
