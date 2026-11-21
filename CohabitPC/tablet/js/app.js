// Cohabit タブレット: ノート・問題・妹・赤ペンをつなぐ
import { loadSettings, settings, saveSettings, onSettings, resolveUrl, isCrossOrigin } from './settings.js';
import { api, ApiError, connectEvents, onConnection, speechUrl } from './api.js';
import { Notebook } from './notebook.js';
import { RedPen } from './redpen.js';
import { placeholderSvg } from './sister.js';
import { installPenOnly, isIPad } from './penonly.js';
import { unlockAudio, audioRunning } from './audio.js';

const $ = (s) => document.querySelector(s);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

loadSettings();

const state = {
  session: null,
  problem: null,
  seenProblems: new Set(),
  presence: 'tablet',
  dirty: new Set(),          // 前に見てもらってから書き変えた行
  checking: false,
  pendingTrigger: null,
  idleTimer: 0,
  retryTimer: 0,
  started: false,
};

// ---------- ノートと赤ペン ----------
const nb = new Notebook({ stage: $('#stage'), canvas: $('#ink'), marksLayer: $('#marksLayer'), getSettings: settings });
// ペンだけ (指は書くのもタップも無視)。iPad では Pencil を一度使ったら自動で
const penOnly = () => {
  if (!state.started) return false; // 始める前 (最初の画面) は指でも押せる
  const v = settings().penOnly;
  return v === 'on' || (v === 'auto' && isIPad() && nb.penSeen);
};
installPenOnly(penOnly, () => {
  saveSettings({ penOnly: 'off' });
  toast('指も使えるようにしたよ (設定の「ペンだけ」で戻せる)', 4000);
});
const pen = new RedPen(nb, $('#fx'));
let sister = null;           // three.js は始めるときに読む (失敗しても絵で代わる)
const sisterReady = import('./sister.js').then(({ Sister }) => {
  sister = new Sister({ card: $('#sisCard'), canvas: $('#sisCanvas'), placeholder: $('#sisPlaceholder'), loading: $('#sisLoading') });
  return sister;
}).catch((e) => {
  console.warn('[app] sister module', e);
  $('#sisCard').classList.add('placeholder');
  $('#sisPlaceholder').innerHTML = placeholderSvg();
  return null;
});

$('#startArt').innerHTML = placeholderSvg('stMouth', 'stEyes');

// ---------- 画面の配置 ----------
function updateLayout() {
  const stage = $('#stage').getBoundingClientRect();
  const sideBySide = stage.width >= 1000 && window.innerWidth > window.innerHeight && settings().sister;
  const sisW = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--sis-w')) || 300;
  nb.bottomPad = sideBySide ? 40 : 40 + (settings().sister ? 0 : 0);
  nb.setReserveRight(sideBySide ? sisW + 20 : 0);
}
window.addEventListener('resize', () => requestAnimationFrame(updateLayout));
updateLayout();

function applySettings() {
  const s = settings();
  document.documentElement.style.setProperty('--problem-size', { s: '24px', m: '30px', l: '38px' }[s.textSize] || '30px');
  if (window.innerWidth < window.innerHeight) document.documentElement.style.setProperty('--problem-size', { s: '22px', m: '26px', l: '32px' }[s.textSize] || '26px');
  $('#sister').classList.toggle('off', !s.sister);
  sister?.setVisible(s.sister && state.presence === 'tablet');
  updateLayout();
}
onSettings(applySettings);
applySettings();

// ---------- 道具 ----------
function syncTools() {
  $('#toolPen').classList.toggle('on', nb.tool === 'pen');
  $('#toolEraser').classList.toggle('on', nb.tool === 'eraser');
  $('#undoBtn').disabled = !nb.canUndo();
  $('#redoBtn').disabled = !nb.canRedo();
  $('#pageNo').textContent = `${nb.index + 1} / ${nb.pages.length}`;
  $('#pagePrev').disabled = nb.index === 0;
  $('#pageNext').disabled = nb.index >= nb.pages.length - 1;
}
nb.on('history', syncTools);
nb.on('tool', syncTools);
nb.on('page', () => { state.dirty.clear(); syncTools(); });
syncTools();

$('#toolPen').onclick = () => nb.setTool('pen');
$('#toolEraser').onclick = () => nb.setTool(nb.tool === 'eraser' ? 'pen' : 'eraser');
$('#undoBtn').onclick = () => nb.undo();
$('#redoBtn').onclick = () => nb.redo();
$('#pagePrev').onclick = () => nb.goto(nb.index - 1);
$('#pageNext').onclick = () => nb.goto(nb.index + 1);
$('#pageAdd').onclick = () => { nb.addPage(); nb.page.problemId = state.problem?.id || null; toast('新しいページ'); };
armedButton($('#clearBtn'), $('#clearLabel'), '全部消す', 'もう一度で消す', () => {
  nb.clearPage();
  pen.clearAll();
});

window.addEventListener('keydown', (e) => {
  if (e.target.closest('input, textarea')) return;
  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.key.toLowerCase() === 'z') { e.preventDefault(); e.shiftKey ? nb.redo() : nb.undo(); }
  else if (mod && e.key.toLowerCase() === 'y') { e.preventDefault(); nb.redo(); }
  else if (e.key === 'e') nb.setTool(nb.tool === 'eraser' ? 'pen' : 'eraser');
});

/** 1 回目で「本当に？」、もう一度で実行 */
function armedButton(btn, label, normal, armedText, fn, cls = 'armed') {
  let timer = 0;
  btn.addEventListener('click', () => {
    if (btn.classList.contains(cls)) {
      clearTimeout(timer); btn.classList.remove(cls); if (label) label.textContent = normal;
      fn();
      return;
    }
    btn.classList.add(cls); if (label) label.textContent = armedText;
    timer = setTimeout(() => { btn.classList.remove(cls); if (label) label.textContent = normal; }, 3000);
  });
}

// ---------- 書いた → 見てもらう ----------
nb.on('change', ({ page, rows, kind }) => {
  if (page !== nb.page) return;
  for (const r of rows) state.dirty.add(r);
  // 書き直した行の赤ペンは消す (消しゴム・書き足し)
  if (rows.length) pen.clearRows(rows);
  if (kind === 'clear') pen.clearAll();
  scheduleIdleCheck();
});
nb.on('stroke-start', () => { clearTimeout(state.idleTimer); });
nb.on('pen', (x, y) => sister?.lookAtScreen(x, y));
nb.on('pen-detected', () => toast(penOnly() ? 'Apple Pencil を見つけたよ。指はさわっても反応しないよ (設定で変えられる)' : 'ペンを見つけたよ。指はスクロールになるよ', 4000));

function scheduleIdleCheck() {
  clearTimeout(state.idleTimer);
  if (!settings().autoCheck || !state.problem) return;
  state.idleTimer = setTimeout(() => {
    if (nb.active) return scheduleIdleCheck();
    if (state.dirty.size) runCheck('pause');
  }, 1500);
}

const TRIGGER_RANK = { pause: 0, request: 1, done: 2 };

async function runCheck(trigger) {
  if (state.checking) {
    // 重ねて聞かない。終わったら一番強いものを 1 回だけ
    if (state.pendingTrigger == null || TRIGGER_RANK[trigger] > TRIGGER_RANK[state.pendingTrigger]) state.pendingTrigger = trigger;
    return;
  }
  if (trigger === 'pause' && !state.dirty.size) return;
  if (!nb.page.strokes.length && trigger !== 'pause') {
    speak({ text: 'まだ何も書いてないよ？', expression: 'surprised' }, { local: true });
    return;
  }
  const changed = [...state.dirty].sort((a, b) => a - b);
  state.dirty.clear();
  state.checking = true;
  const page = nb.page;
  setWatching(true, trigger === 'pause' ? '妹が見てる' : trigger === 'done' ? '答え合わせ中' : '見てるね');
  const busyBtn = trigger === 'done' ? $('#doneBtn') : trigger === 'request' ? $('#lookBtn') : null;
  busyBtn?.classList.add('busy');
  try {
    const img = nb.exportPage({ changedRows: changed, page });
    const res = await api.check({ problemId: state.problem?.id ?? null, trigger, page: img.page, rows: img.rows, changedRows: changed });
    if (nb.page !== page) return;               // ページを変えた
    handleCheck(res, trigger);
  } catch (e) {
    for (const r of changed) state.dirty.add(r);
    handleError(e, { retry: () => runCheck(trigger), quiet: trigger === 'pause' });
  } finally {
    state.checking = false;
    busyBtn?.classList.remove('busy');
    setWatching(false);
    const next = state.pendingTrigger;
    state.pendingTrigger = null;
    if (next) setTimeout(() => runCheck(next), 300);
  }
}

function handleCheck(res, trigger) {
  if (!res) return;
  const marks = Array.isArray(res.marks) ? res.marks : [];
  if (marks.length) pen.apply(marks);
  if (res.solved) {
    const d = pen.hanamaru();
    sister?.setExpression('happy');
    setTimeout(() => sister?.setExpression('neutral'), 6000);
    if (!res.say) speak({ text: 'できた！ 花丸あげる', expression: 'happy' }, { local: true, delay: d * 400 });
  }
  if (res.say && res.say.text) speak(res.say);
  else if (trigger !== 'pause' && !marks.length && !res.solved) speak({ text: 'うん、いまのところ大丈夫そう', expression: 'neutral' }, { local: true });
  $('#hintBtn').classList.toggle('nudge', !!res.stuck);
  if (res.stuck) toast('ヒント、いる？');
}

$('#lookBtn').onclick = () => { clearTimeout(state.idleTimer); runCheck('request'); };
$('#doneBtn').onclick = () => { clearTimeout(state.idleTimer); runCheck('done'); };

// ヒント / わからない / 答え
for (const b of document.querySelectorAll('[data-ask]')) {
  const level = b.dataset.ask;
  if (level === 'answer') {
    armedButton(b, b.querySelector('span'), '答え', 'ほんとに？', () => ask(level), 'confirm');
  } else {
    b.addEventListener('click', () => ask(level));
  }
}

async function ask(level, question) {
  if (!state.problem && !question) { speak({ text: '先に問題をもらってね', expression: 'thinking' }, { local: true }); return; }
  const btn = document.querySelector(`[data-ask="${level}"]`);
  btn?.classList.add('busy');
  $('#hintBtn').classList.remove('nudge');
  setWatching(true, level === 'hint' ? 'ヒントを考えてる' : level === 'explain' ? '説明を考えてる' : '答えを書いてる');
  sister?.setExpression('thinking');
  try {
    const page = nb.page;
    const req = { problemId: state.problem?.id ?? null, level };
    if (question) req.question = question;
    if (page.strokes.length) req.page = nb.exportPage({ page }).page;
    const res = await api.ask(req);
    if (res?.marks?.length && nb.page === page) pen.apply(res.marks);
    if (res?.say) speak(res.say);
  } catch (e) {
    handleError(e, { retry: () => ask(level, question) });
  } finally {
    btn?.classList.remove('busy');
    setWatching(false);
  }
}

function setWatching(on, text) {
  const w = $('#watching');
  if (on) { $('#watchingText').textContent = text || '妹が見てる'; w.hidden = false; }
  else w.hidden = true;
}

// ---------- エラー ----------
function handleError(e, { retry, quiet = false } = {}) {
  if (!(e instanceof ApiError)) { console.error(e); toast('うまくいかなかった…'); return; }
  if (e.network) return; // 帯が出る
  switch (e.status) {
    case 409: refreshSession(); if (!quiet) toast('問題が変わったみたい。読み直したよ'); break;
    case 429:
      if (!quiet) speak({ text: 'ちょっと待ってね、考えすぎて息切れ…', expression: 'worried' }, { local: true });
      clearTimeout(state.retryTimer);
      state.retryTimer = setTimeout(() => retry?.(), quiet ? 12000 : 8000);
      break;
    case 403: toast('PC の設定で、ノートの絵を送らないことになってるよ'); break;
    case 401: showBanner('トークンが違うみたい。設定を見てね', false); break;
    case 502: toastAction('先生 (AI) に届かなかった', 'もう一度', retry); break;
    default: if (!quiet) toast(`うまくいかなかった (${e.message})`);
  }
}

// ---------- 問題 ----------
function setProblem(problem, { fresh = true } = {}) {
  if (!problem) return;
  state.seenProblems.add(problem.id);
  const prev = state.problem;
  state.problem = problem;
  const page = nb.page;
  if (prev?.id !== problem.id && page.problemId && page.problemId !== problem.id && (page.strokes.length || page.marks.length || page.solved)) {
    nb.addPage();
  } else if (page.problemId !== problem.id && !page.strokes.length) {
    pen.clearAll(page);
  }
  nb.page.problemId = problem.id;
  nb.scheduleSave();
  state.dirty.clear();
  $('#problem').dataset.state = 'ready';
  $('#subject').textContent = problem.subject || '問題';
  $('#goal').textContent = problem.goal || '';
  const t = $('#problemText');
  t.textContent = problem.text || '';
  if (fresh) { t.classList.remove('fresh'); void t.offsetWidth; t.classList.add('fresh'); }
}

function problemLoading(on, text = '妹が問題を考えてる…') {
  if (on) { $('#problemLoadingText').textContent = text; $('#problem').dataset.state = 'loading'; }
  else $('#problem').dataset.state = state.problem ? 'ready' : 'empty';
}

async function requestProblem(req, loadingText) {
  problemLoading(true, loadingText);
  sister?.setExpression('thinking');
  try {
    const res = await api.problem(req);
    if (res?.problem) setProblem(res.problem);
    if (res?.say) speak(res.say);
  } catch (e) {
    problemLoading(false);
    sister?.setExpression('neutral');
    if (e instanceof ApiError && e.status === 409 && (req.from === 'next' || e.code === 'no_records')) {
      // 話題を勝手に決めない: 何を勉強するか聞く (話題の欄を開いて待つ)
      if (e.say) speak(e.say);
      toast(e.code === 'no_records' ? (e.message || 'まだ学習記録がありません。何を勉強するか教えてください') : 'まだ前の問題が無いよ。話題を入れてね');
      openSheet('#topicSheet', () => $('#topicInput').focus());
      return;
    }
    handleError(e, { retry: () => requestProblem(req, loadingText) });
    return;
  }
  problemLoading(false);
}

document.querySelector('[data-act="topic"]').onclick = () => openSheet('#topicSheet', () => $('#topicInput').focus());
document.querySelector('[data-act="photo"]').onclick = () => $('#photoInput').click();
document.querySelector('[data-act="weak"]').onclick = () => requestProblem({ from: 'weak' }, '苦手なところから選んでる…');
document.querySelector('[data-act="next"]').onclick = () => openSheet('#nextSheet');

for (const b of document.querySelectorAll('#nextSheet [data-level]')) {
  b.onclick = () => { closeSheets(); requestProblem({ from: 'next', level: b.dataset.level }, '次の問題を考えてる…'); };
}
// 話題の候補は、学習記録から作った苦手の要約だけ (決まった候補は出さない)。無ければ欄ごと隠す
function renderTopicChips(weak) {
  const box = $('#topicChips');
  const topics = [...new Set((weak?.items || []).map((w) => w?.topic).filter(Boolean))].slice(0, 6);
  box.replaceChildren(...topics.map((t) => {
    const b = document.createElement('button');
    b.textContent = t;
    b.onclick = () => { $('#topicInput').value = t; $('#topicInput').focus(); };
    return b;
  }));
  box.hidden = topics.length === 0;
}
const sendTopic = () => {
  const topic = $('#topicInput').value.trim();
  if (!topic) { $('#topicInput').focus(); return; }
  closeSheets();
  requestProblem({ from: 'topic', topic }, `「${topic}」の問題を考えてる…`);
};
$('#topicGo').onclick = sendTopic;
$('#topicInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') sendTopic(); });

// 声で話題を入れる (Web Speech API があれば)
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
if (!Recognition) $('#micBtn').hidden = true;
let recog = null;
$('#micBtn').onclick = () => {
  if (recog) { recog.stop(); return; }
  try {
    recog = new Recognition();
    recog.lang = 'ja-JP';
    recog.interimResults = true;
    recog.onresult = (ev) => {
      let text = '';
      for (const r of ev.results) text += r[0].transcript;
      $('#topicInput').value = text;
    };
    recog.onend = () => { recog = null; $('#micBtn').classList.remove('listening'); };
    recog.onerror = () => { recog = null; $('#micBtn').classList.remove('listening'); toast('声が聞き取れなかった'); };
    recog.start();
    $('#micBtn').classList.add('listening');
  } catch { recog = null; toast('このブラウザは声で入れられないみたい'); }
};

// 教科書を撮る
$('#photoInput').addEventListener('change', async (e) => {
  const file = e.target.files?.[0];
  e.target.value = '';
  if (!file) return;
  try {
    const image = await downscale(file, 1600, 0.85);
    requestProblem({ from: 'photo', image }, '写真を読んでる…');
  } catch { toast('写真が読めなかった'); }
});

async function downscale(file, maxSide, quality) {
  const bmp = await (window.createImageBitmap ? createImageBitmap(file, { imageOrientation: 'from-image' }) : loadImage(file));
  const k = Math.min(1, maxSide / Math.max(bmp.width, bmp.height));
  const c = document.createElement('canvas');
  c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
  const ctx = c.getContext('2d');
  ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, c.width, c.height);
  ctx.drawImage(bmp, 0, 0, c.width, c.height);
  return c.toDataURL('image/jpeg', quality);
}
function loadImage(file) {
  return new Promise((res, rej) => { const img = new Image(); img.onload = () => res(img); img.onerror = rej; img.src = URL.createObjectURL(file); });
}

// ---------- シート ----------
function openSheet(sel, after) {
  closeSheets();
  $(sel).hidden = false;
  after?.();
}
function closeSheets() { for (const s of document.querySelectorAll('.sheet')) s.hidden = true; }
for (const s of document.querySelectorAll('.sheet')) {
  s.addEventListener('pointerdown', (e) => { if (e.target === s) closeSheets(); });
  for (const b of s.querySelectorAll('[data-close]')) b.onclick = closeSheets;
}

// ---------- 妹のことば ----------
const sayQueue = [];
let speaking = false;

/** Say を順番に出す (字幕・表情・声) */
function speak(say, { local = false, delay = 0 } = {}) {
  if (!say || !say.text) return;
  lastSpoken = say;
  sayQueue.push({ say, local, delay });
  if (sayQueue.length > 4) sayQueue.splice(0, sayQueue.length - 4);
  if (!speaking) drainSay();
}

async function drainSay() {
  speaking = true;
  while (sayQueue.length) {
    const { say, local, delay } = sayQueue.shift();
    if (delay) await sleep(delay);
    await sayOne(say, local);
  }
  speaking = false;
  hideBubble();
}

let bubbleTimer = 0;
async function sayOne(say, local) {
  clearTimeout(bubbleTimer);
  const expr = say.expression || 'neutral';
  sister?.setExpression(expr);
  const b = $('#bubble');
  b.hidden = false;
  b.classList.remove('leaving', 'mood-happy', 'mood-worried');
  if (expr === 'happy') b.classList.add('mood-happy');
  if (expr === 'worried') b.classList.add('mood-worried');
  b.style.animation = 'none'; void b.offsetWidth; b.style.animation = '';
  typeText($('#bubbleText'), say.text);
  const readMs = Math.min(9000, 1400 + say.text.length * 110);
  const canVoice = settings().voice && state.presence === 'tablet' && state.started && sister;
  let played = false;
  if (canVoice && (!local || say.audio)) {
    played = await sister.play(speechUrl(say));
    if (played === 'locked') { showSoundButton(); played = false; }
  }
  if (!played) {
    if (state.presence === 'tablet') sister?.fakeTalk(Math.min(readMs, 400 + say.text.length * 90));
    await sleep(readMs);
  } else {
    await sleep(900);
  }
  setTimeout(() => { if (!speaking) sister?.setExpression('neutral'); }, 1500);
  bubbleTimer = setTimeout(hideBubble, 4500);
}

function typeText(el, text) {
  clearInterval(el._t);
  el.textContent = '';
  const chars = [...text];
  let i = 0;
  el._t = setInterval(() => {
    i = Math.min(chars.length, i + 2);
    el.textContent = chars.slice(0, i).join('');
    if (i >= chars.length) clearInterval(el._t);
  }, 28);
}

function hideBubble() {
  if (speaking) return;
  const b = $('#bubble');
  if (b.hidden) return;
  b.classList.add('leaving');
  setTimeout(() => { if (!speaking) b.hidden = true; }, 380);
}

// ---------- 居場所 (タブレット / 部屋) ----------
function setPresence(where) {
  state.presence = where === 'room' ? 'room' : 'tablet';
  $('#sister').dataset.presence = state.presence;
  if (state.presence === 'room') sister?.stopAudio();
  sister?.setVisible(settings().sister && state.presence === 'tablet');
  updateLayout();
}

// ---------- 接続 ----------
let bannerPoll = 0;
function showBanner(text, retrying = true) {
  $('#bannerText').textContent = text;
  $('#banner').hidden = false;
  $('#conn').className = 'conn bad';
  clearInterval(bannerPoll);
  if (retrying) bannerPoll = setInterval(() => refreshSession(true), 5000);
}
function hideBanner() {
  $('#banner').hidden = true;
  $('#conn').className = 'conn ok';
  clearInterval(bannerPoll);
}
onConnection((ok, why) => {
  if (ok) hideBanner();
  else if (why === 'token') showBanner('トークンが違うみたい。設定を見てね', false);
  else showBanner('PC につながらない。書いたものはこのまま残るよ — つなぎ直してる…');
});
$('#bannerRetry').onclick = () => { refreshSession(); events?.reconnect(); };

async function refreshSession(silent = false) {
  try {
    const s = await api.session();
    state.session = s;
    if (s?.sister?.name) $('#sisName').textContent = s.sister.name;
    if (s?.backend) $('#backendInfo').textContent = `先生: ${s.backend.chat || '—'} / 画像: ${s.backend.vision || '—'}`;
    if (s?.presence) setPresence(s.presence);
    renderTopicChips(s?.weak);
    if (s?.problem && s.problem.id !== state.problem?.id) {
      setProblem(s.problem, { fresh: !silent });
      // PC が覚えている赤ペンを、このページにまだ無ければ出す
      if (Array.isArray(s.marks) && s.marks.length && !nb.page.marks.length) pen.apply(s.marks, { animate: false });
      if (s.solved && !nb.page.solved) pen.hanamaru({ animate: false });
    } else if (!s?.problem && !state.problem) {
      problemLoading(false);
    }
    return s;
  } catch { return null; }
}

let events = null;
function startEvents() {
  events = connectEvents({
    open: () => $('#conn').className = 'conn ok',
    presence: (d) => setPresence(d?.where),
    say: (d) => speak(d),
    problem: (d) => {
      const p = d?.problem;
      if (!p || state.seenProblems.has(p.id)) return;
      setProblem(p);
      problemLoading(false);
      if (d.say) speak(d.say);
    },
  });
}

// ---------- 設定 ----------
function openDrawer() {
  const s = settings();
  $('#setAddress').value = s.address;
  $('#setToken').value = s.token;
  $('#setRowH').value = s.rowHeight;
  $('#rowHVal').textContent = s.rowHeight;
  $('#setFinger').checked = s.fingerDraw;
  $('#setPenOnly').checked = penOnly();
  $('#setVoice').checked = s.voice;
  $('#setSister').checked = s.sister;
  $('#setAuto').checked = s.autoCheck;
  for (const b of $('#setTextSize').children) b.classList.toggle('on', b.dataset.v === s.textSize);
  $('#drawer').classList.add('open');
  $('#drawer').setAttribute('aria-hidden', 'false');
  $('#drawerScrim').hidden = false;
}
function closeDrawer() {
  const s = settings();
  const address = $('#setAddress').value.trim(), token = $('#setToken').value.trim();
  const reconnect = address !== s.address || token !== s.token;
  saveSettings({ address, token });
  $('#drawer').classList.remove('open');
  $('#drawer').setAttribute('aria-hidden', 'true');
  $('#drawerScrim').hidden = true;
  if (reconnect && state.started) { events?.close(); startEvents(); refreshSession(); }
}
$('#settingsBtn').onclick = openDrawer;
$('#drawerClose').onclick = closeDrawer;
$('#drawerScrim').onclick = closeDrawer;
$('#setRowH').oninput = (e) => { $('#rowHVal').textContent = e.target.value; };
$('#setRowH').onchange = (e) => { const v = +e.target.value; saveSettings({ rowHeight: v }); nb.setRowHeight(v); };
$('#setFinger').onchange = (e) => saveSettings({ fingerDraw: e.target.checked });
$('#setPenOnly').onchange = (e) => saveSettings({ penOnly: e.target.checked ? 'on' : 'off' });
$('#setVoice').onchange = (e) => { saveSettings({ voice: e.target.checked }); if (!e.target.checked) sister?.stopAudio(); };
$('#setSister').onchange = (e) => saveSettings({ sister: e.target.checked });
$('#setAuto').onchange = (e) => saveSettings({ autoCheck: e.target.checked });
for (const b of $('#setTextSize').children) b.onclick = () => { saveSettings({ textSize: b.dataset.v }); for (const o of $('#setTextSize').children) o.classList.toggle('on', o === b); };
armedButton($('#resetNotebook'), null, '', '', () => { nb.resetAll(); pen.render(); toast('ノートを白紙にしたよ'); }, 'confirm');
$('#resetNotebook').addEventListener('click', () => {
  $('#resetNotebook').textContent = $('#resetNotebook').classList.contains('confirm') ? 'もう一度押すと消えるよ' : 'ノートを全部消す';
});

// ---------- トースト ----------
let toastTimer = 0;
function toast(text, ms = 2600) {
  const t = $('#toast');
  t.textContent = text;
  t.hidden = false;
  t.style.animation = 'none'; void t.offsetWidth; t.style.animation = '';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}
function toastAction(text, label, fn) {
  const t = $('#toast');
  t.textContent = text + '  ';
  if (fn) {
    const b = document.createElement('button');
    b.textContent = label;
    b.style.cssText = 'color:#ffb3c6;font-weight:700;margin-left:8px;pointer-events:auto';
    b.onclick = () => { t.hidden = true; fn(); };
    t.appendChild(b);
  }
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 6000);
}

// ---------- 声が止められているとき ----------
// ブラウザが音を止めている (タップ / ペンでの解錠が要る)。押すと解錠して、最後の一言を言い直す
let lastSpoken = null;
function showSoundButton() {
  $('#soundBtn').hidden = false;
}
$('#soundBtn').onclick = () => {
  unlockAudio();
  $('#soundBtn').hidden = true;
  if (lastSpoken) speak(lastSpoken);
};

// ---------- 全画面から外れた / 表示がずれたとき ----------
// 全画面が解けた (スワイプ・通知・別のアプリ) か、拡大・スクロールでずれたら、右上に「戻す」を出す。押すと元に戻す
const standalone = () => window.matchMedia?.('(display-mode: standalone)').matches || window.navigator.standalone === true;
const canFullscreen = () => !!(document.documentElement.requestFullscreen || document.documentElement.webkitRequestFullscreen);
const isFullscreen = () => !!(document.fullscreenElement || document.webkitFullscreenElement);
function viewShifted() {
  const vv = window.visualViewport;
  return (vv && Math.abs(vv.scale - 1) > 0.02) || window.scrollX !== 0 || window.scrollY !== 0
    || document.documentElement.scrollTop !== 0 || document.body.scrollTop !== 0;
}
function checkView() {
  if (!state.started) { $('#fsBtn').hidden = true; return; }
  const needFs = !standalone() && canFullscreen() && !isFullscreen();
  const shifted = viewShifted();
  $('#fsBtn').hidden = !(needFs || shifted);
  $('#fsBtn').querySelector('span').textContent = needFs ? '全画面に戻す' : '表示を戻す';
}
async function restoreView() {
  window.scrollTo(0, 0);
  document.documentElement.scrollTop = 0;
  document.body.scrollTop = 0;
  if (!standalone() && !isFullscreen()) {
    const el = document.documentElement;
    try {
      if (el.requestFullscreen) await el.requestFullscreen({ navigationUI: 'hide' });
      else if (el.webkitRequestFullscreen) el.webkitRequestFullscreen();
    } catch { /* 全画面にならなくても使える */ }
  }
  unlockAudio();
  setTimeout(() => { updateLayout(); checkView(); }, 300);
}
$('#fsBtn').onclick = restoreView;
for (const t of ['fullscreenchange', 'webkitfullscreenchange', 'resize', 'orientationchange', 'scroll']) {
  window.addEventListener(t, () => requestAnimationFrame(checkView));
  document.addEventListener(t, () => requestAnimationFrame(checkView));
}
window.visualViewport?.addEventListener('resize', () => requestAnimationFrame(checkView));
window.visualViewport?.addEventListener('scroll', () => requestAnimationFrame(checkView));
document.addEventListener('visibilitychange', () => { if (!document.hidden) setTimeout(checkView, 300); });
setInterval(checkView, 2000);

// ---------- 始める ----------
async function start() {
  if (state.started) return;
  unlockAudio(); // タップ / ペンの処理の中で、await より前に (後だと iPad では声が出ない)
  state.started = true;
  $('#start').classList.add('gone');
  try { await document.documentElement.requestFullscreen?.({ navigationUI: 'hide' }); } catch { /* 全画面にならなくても使える */ }
  try { await navigator.wakeLock?.request('screen'); } catch { /* ignore */ }
  const sis = await sisterReady;
  sis?.unlockAudio();
  startEvents();
  const s = await refreshSession();
  updateLayout();
  if (sis && settings().sister) {
    const url = s?.sister?.vrmUrl || '/tablet/vrm';
    sis.load(resolveUrl(url, { withToken: true })).then(() => updateLayout());
  } else if (sis) {
    sis.usePlaceholder();
  }
  setPresence(s?.presence || state.presence);
  if (!state.problem) problemLoading(false);
  checkView();
}
$('#startBtn').onclick = start;
$('#start').addEventListener('pointerup', (e) => { if (e.target === $('#start')) start(); });
document.addEventListener('visibilitychange', async () => {
  if (document.visibilityState === 'visible' && state.started) {
    try { await navigator.wakeLock?.request('screen'); } catch { /* ignore */ }
    refreshSession(true);
  }
});
if (penOnly()) $('#startHint').textContent = 'ペンで書く・ボタンもペンで (指は反応しない)';
else if (nb.penSeen) $('#startHint').textContent = 'ペンで書く・指でスクロール';
else $('#startHint').textContent = 'ペンで書く・指でスクロール (ペンが無ければ指でも書けるよ)';

// テストと調べもの用
window.__cohabit = { nb, pen, state, speak, setPresence, setProblem, runCheck, get sister() { return sister; }, start, toast };
