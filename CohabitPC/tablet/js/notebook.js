// ノート: 罫線のページ・ペンのインク・消しゴム・元に戻す・スクロール・PNG 書き出し
//
// 座標は「ページの単位」: ページの幅がいつも 1000。画面の幅に合わせて拡大縮小するので、
// 縦横を回してもインクと赤ペンの位置は変わらない。行 n (1 から) は y = (n-1)*rowH 〜 n*rowH。

export const PAGE_W = 1000;
export const MARGIN_X = 64;          // 行番号の余白
const BASE_W = 3.1;                  // 筆圧 0.5 のときの太さ (単位)
const ERASER_R = 16;
const STORE_KEY = 'cohabit.tablet.notebook.v1';
const INK_SCREEN = '#1c2433';
const PAPER = '#fffefb';
const RULE = '#d5e2ef';
const ROWNUM = '#b3bfce';

let seq = 0;
const newId = () => Date.now().toString(36) + (seq++).toString(36);

function makePage(rowH) {
  return { id: newId(), rowH, rows: 24, strokes: [], marks: [], solved: null, problemId: null, undo: [], redo: [] };
}

/** ストロークの点 [x, y, w, x, y, w, ...] から、太さごとにまとめた Path2D を作る */
function buildPaths(p) {
  const n = p.length / 3;
  const buckets = new Map();
  const bucket = (w) => {
    const k = Math.round(w * 4) / 4;
    let b = buckets.get(k);
    if (!b) { b = { w: k, path: new Path2D(), lastEnd: null }; buckets.set(k, b); }
    return b;
  };
  if (n === 1) {
    const b = bucket(p[2]);
    b.path.moveTo(p[0], p[1]);
    b.path.lineTo(p[0] + 0.01, p[1]);
    return [...buckets.values()];
  }
  forEachSegment(p, (seg) => {
    const b = bucket(seg.w);
    if (!b.lastEnd || b.lastEnd[0] !== seg.sx || b.lastEnd[1] !== seg.sy) b.path.moveTo(seg.sx, seg.sy);
    if (seg.cx === undefined) b.path.lineTo(seg.ex, seg.ey);
    else b.path.quadraticCurveTo(seg.cx, seg.cy, seg.ex, seg.ey);
    b.lastEnd = [seg.ex, seg.ey];
  });
  return [...buckets.values()];
}

/** 中点を結ぶ 2 次曲線で滑らかにつなぐ。from 番目の区間から (未完の最後の区間は final のときだけ) */
function forEachSegment(p, fn, from = 0, final = true) {
  const n = p.length / 3;
  const mid = (i) => [(p[i * 3] + p[i * 3 + 3]) / 2, (p[i * 3 + 1] + p[i * 3 + 4]) / 2];
  for (let i = from; i <= n - 1; i++) {
    if (i === 0) {
      const m = mid(0);
      fn({ sx: p[0], sy: p[1], ex: m[0], ey: m[1], w: p[2] });
    } else if (i < n - 1) {
      const a = mid(i - 1), b = mid(i);
      fn({ sx: a[0], sy: a[1], cx: p[i * 3], cy: p[i * 3 + 1], ex: b[0], ey: b[1], w: p[i * 3 + 2] });
    } else if (final) {
      const a = mid(n - 2);
      fn({ sx: a[0], sy: a[1], ex: p[i * 3], ey: p[i * 3 + 1], w: p[i * 3 + 2] });
    }
  }
}

function bboxOf(p) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity, wm = 0;
  for (let i = 0; i < p.length; i += 3) {
    if (p[i] < x0) x0 = p[i]; if (p[i] > x1) x1 = p[i];
    if (p[i + 1] < y0) y0 = p[i + 1]; if (p[i + 1] > y1) y1 = p[i + 1];
    if (p[i + 2] > wm) wm = p[i + 2];
  }
  return { x0: x0 - wm, y0: y0 - wm, x1: x1 + wm, y1: y1 + wm };
}

/** このストロークが「書かれた」行 (中心の行と、しっかり重なる行) */
export function rowsOfBox(b, rowH) {
  const out = [];
  const h = b.y1 - b.y0;
  const need = Math.min(14, h * 0.3);
  const cy = (b.y0 + b.y1) / 2;
  const r0 = Math.max(1, Math.floor(b.y0 / rowH) + 1);
  const r1 = Math.max(1, Math.floor(b.y1 / rowH) + 1);
  for (let r = r0; r <= r1; r++) {
    const top = (r - 1) * rowH, bot = r * rowH;
    const ov = Math.min(bot, b.y1) - Math.max(top, b.y0);
    if (ov >= need || (cy >= top && cy < bot)) out.push(r);
  }
  return out;
}

function distToSeg(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay;
  const l2 = dx * dx + dy * dy;
  let t = l2 ? ((px - ax) * dx + (py - ay) * dy) / l2 : 0;
  t = Math.max(0, Math.min(1, t));
  const x = ax + t * dx - px, y = ay + t * dy - py;
  return Math.sqrt(x * x + y * y);
}

export class Notebook {
  constructor({ stage, canvas, marksLayer, getSettings }) {
    this.stage = stage;
    this.canvas = canvas;
    this.marksLayer = marksLayer;
    this.getSettings = getSettings;
    this.ctx = canvas.getContext('2d', { desynchronized: true }) || canvas.getContext('2d');
    this.dpr = 1;
    this.w = 0; this.h = 0;
    this.scale = 1; this.ox = 0; this.oy = 18; this.scrollY = 0;
    this.reserveRight = 0;
    this.bottomPad = 40;
    this.tool = 'pen';
    this.active = null;          // 書いている / 消している最中
    this.touches = new Map();    // パンに使っている指
    this.pan = null;
    this.inertia = null;
    this.penSeen = false;
    this.penHoverAt = 0;
    this.eraserAt = null;
    this.svgs = new Map();
    this.handlers = {};
    this.pages = [];
    this.index = 0;
    this._raf = 0;
    this._saveTimer = 0;
    this.load();
    if (!this.pages.length) this.pages.push(makePage(getSettings().rowHeight || 72));
    this.bind();
    this.layout();
  }

  on(name, fn) { (this.handlers[name] ||= []).push(fn); }
  emit(name, ...a) { for (const fn of this.handlers[name] || []) fn(...a); }

  get page() { return this.pages[this.index]; }
  pageHeight(page = this.page) { return page.rows * page.rowH; }

  // ---------- 配置 ----------
  layout() {
    const r = this.stage.getBoundingClientRect();
    this.rect = r;
    this.w = r.width; this.h = r.height;
    this.dpr = Math.min(window.devicePixelRatio || 1, 3);
    this.canvas.width = Math.round(this.w * this.dpr);
    this.canvas.height = Math.round(this.h * this.dpr);
    const gutter = this.w < 700 ? 8 : 14;
    const avail = Math.max(200, this.w - gutter * 2 - this.reserveRight);
    this.scale = Math.max(0.3, Math.min(1.4, avail / PAGE_W));
    this.ox = gutter + (avail - PAGE_W * this.scale) / 2;
    this.oy = 14;
    for (const [pageId, svg] of this.svgs) {
      const page = this.pages.find((p) => p.id === pageId);
      if (page) this.sizeSvg(svg, page);
    }
    this.clampScroll();
    this.updateMarksTransform();
    this.drawAll();
    this.emit('view');
  }

  setReserveRight(px) {
    if (Math.abs(px - this.reserveRight) < 1) return;
    this.reserveRight = px;
    this.layout();
  }

  maxScroll() {
    const visible = (this.h - this.oy - this.bottomPad) / this.scale;
    return Math.max(0, this.pageHeight() - visible);
  }
  clampScroll() { this.scrollY = Math.max(0, Math.min(this.maxScroll(), this.scrollY)); }

  scrollBy(dyUnits) {
    const before = this.scrollY;
    this.scrollY += dyUnits;
    this.clampScroll();
    if (this.scrollY !== before) { this.updateMarksTransform(); this.requestDraw(); this.emit('view'); }
    return this.scrollY !== before;
  }

  /** 行 row が画面に入るようにスクロールする */
  revealRow(row) {
    const rh = this.page.rowH;
    const top = (row - 1) * rh, bot = row * rh;
    const visible = (this.h - this.oy - this.bottomPad) / this.scale;
    if (top < this.scrollY) this.scrollBy(top - this.scrollY - rh);
    else if (bot > this.scrollY + visible) this.scrollBy(bot - (this.scrollY + visible) + rh);
  }

  toPage(clientX, clientY) {
    const r = this.rect;
    return { x: (clientX - r.left - this.ox) / this.scale, y: (clientY - r.top - this.oy) / this.scale + this.scrollY };
  }
  /** ページの単位 → stage 内の CSS px */
  toStage(x, y) { return { x: this.ox + x * this.scale, y: this.oy + (y - this.scrollY) * this.scale }; }

  updateMarksTransform() {
    this.marksLayer.style.transform = `translate(${this.ox}px, ${this.oy - this.scrollY * this.scale}px)`;
  }

  /** 赤ペンを描く SVG (ページごと) */
  marksSvg(page = this.page) {
    let svg = this.svgs.get(page.id);
    if (!svg) {
      svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.dataset.page = page.id;
      this.marksLayer.appendChild(svg);
      this.svgs.set(page.id, svg);
      this.sizeSvg(svg, page);
    }
    for (const [id, s] of this.svgs) s.style.display = id === this.page.id ? '' : 'none';
    return svg;
  }
  sizeSvg(svg, page) {
    const h = this.pageHeight(page);
    svg.setAttribute('viewBox', `0 0 ${PAGE_W} ${h}`);
    svg.setAttribute('width', String(PAGE_W * this.scale));
    svg.setAttribute('height', String(h * this.scale));
  }

  // ---------- 描画 ----------
  requestDraw() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => { this._raf = 0; this.drawAll(); });
  }

  pageTransform(ctx) {
    const k = this.dpr * this.scale;
    ctx.setTransform(k, 0, 0, k, this.dpr * this.ox, this.dpr * (this.oy - this.scrollY * this.scale));
  }

  drawAll() {
    const ctx = this.ctx, page = this.page;
    const ph = this.pageHeight(page);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    // 紙
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    const px = this.ox, py = this.oy - this.scrollY * this.scale, pw = PAGE_W * this.scale, phs = ph * this.scale;
    ctx.save();
    ctx.shadowColor = 'rgba(31, 38, 51, 0.10)';
    ctx.shadowBlur = 22; ctx.shadowOffsetY = 6;
    ctx.fillStyle = PAPER;
    roundRect(ctx, px, py, pw, phs, 16);
    ctx.fill();
    ctx.restore();

    ctx.save();
    roundRect(ctx, px, py, pw, phs, 16);
    ctx.clip();
    this.pageTransform(ctx);
    const rh = page.rowH;
    const visTop = this.scrollY - 20, visBot = this.scrollY + this.h / this.scale + 20;
    const r0 = Math.max(1, Math.floor(visTop / rh)), r1 = Math.min(page.rows, Math.ceil(visBot / rh) + 1);
    // 余白の線
    ctx.strokeStyle = 'rgba(240, 128, 150, 0.28)';
    ctx.lineWidth = 1.4;
    ctx.beginPath(); ctx.moveTo(MARGIN_X, 0); ctx.lineTo(MARGIN_X, ph); ctx.stroke();
    // 罫線と行番号
    ctx.strokeStyle = RULE;
    ctx.lineWidth = 1.3;
    ctx.beginPath();
    for (let r = r0; r <= r1; r++) {
      const y = r * rh;
      if (r < page.rows) { ctx.moveTo(14, y); ctx.lineTo(PAGE_W - 14, y); }
    }
    ctx.stroke();
    ctx.fillStyle = ROWNUM;
    ctx.font = `500 ${Math.round(Math.min(20, rh * 0.28))}px ${getComputedStyle(document.body).fontFamily}`;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (let r = r0; r <= r1; r++) ctx.fillText(String(r), MARGIN_X - 14, (r - 0.5) * rh);
    // インク
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    ctx.strokeStyle = INK_SCREEN;
    for (const s of page.strokes) {
      const b = s.box || (s.box = bboxOf(s.p));
      if (b.y1 < visTop || b.y0 > visBot) continue;
      this.strokePaths(ctx, s);
    }
    if (this.active && !this.active.erase && this.active.p.length) {
      for (const b of buildPaths(this.active.p)) { ctx.lineWidth = b.w; ctx.stroke(b.path); }
      this.active.drawn = Math.max(0, this.active.p.length / 3 - 1);
    }
    // 消しゴムの輪
    if (this.eraserAt) {
      ctx.strokeStyle = 'rgba(108, 118, 135, 0.6)';
      ctx.lineWidth = 1.5;
      ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.arc(this.eraserAt.x, this.eraserAt.y, ERASER_R, 0, Math.PI * 2); ctx.stroke();
      ctx.setLineDash([]);
    }
    ctx.restore();
  }

  strokePaths(ctx, s) {
    const paths = s.paths || (s.paths = buildPaths(s.p));
    for (const b of paths) { ctx.lineWidth = b.w; ctx.stroke(b.path); }
  }

  /** 書いている最中: 新しくできた区間だけ足す (待ちが少ない) */
  drawLive() {
    const a = this.active;
    const n = a.p.length / 3;
    if (n < 2) return;
    const ctx = this.ctx;
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    roundRect(ctx, this.ox * this.dpr, (this.oy - this.scrollY * this.scale) * this.dpr, PAGE_W * this.scale * this.dpr, this.pageHeight() * this.scale * this.dpr, 16 * this.dpr);
    ctx.clip();
    this.pageTransform(ctx);
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    ctx.strokeStyle = INK_SCREEN;
    forEachSegment(a.p, (seg) => {
      ctx.lineWidth = seg.w;
      ctx.beginPath();
      ctx.moveTo(seg.sx, seg.sy);
      if (seg.cx === undefined) ctx.lineTo(seg.ex, seg.ey); else ctx.quadraticCurveTo(seg.cx, seg.cy, seg.ex, seg.ey);
      ctx.stroke();
    }, a.drawn, false);
    a.drawn = n - 1;
    ctx.restore();
  }

  // ---------- 入力 ----------
  bind() {
    const c = this.stage;
    c.addEventListener('pointerdown', (e) => this.onDown(e));
    c.addEventListener('pointermove', (e) => this.onMove(e));
    c.addEventListener('pointerup', (e) => this.onUp(e));
    c.addEventListener('pointercancel', (e) => this.onUp(e, true));
    c.addEventListener('lostpointercapture', (e) => { if (this.active?.id === e.pointerId) this.onUp(e); });
    c.addEventListener('pointerleave', (e) => { if (e.pointerType === 'pen' && !this.active) { this.eraserAt = null; this.requestDraw(); } });
    c.addEventListener('wheel', (e) => { e.preventDefault(); this.stopInertia(); this.scrollBy(e.deltaY / this.scale); }, { passive: false });
    c.addEventListener('contextmenu', (e) => e.preventDefault());
    new ResizeObserver(() => this.layout()).observe(this.stage);
  }

  isUiTarget(e) { return e.target.closest && e.target.closest('#sister, .banner, .toast, button'); }

  onDown(e) {
    if (this.isUiTarget(e)) return;
    this.rect = this.stage.getBoundingClientRect();
    const type = e.pointerType;
    if (type === 'pen' && !this.penSeen) { this.penSeen = true; this.scheduleSave(); this.emit('pen-detected'); }
    // 手のひら: 大きな接触は無視
    if (type === 'touch' && (e.width > 70 || e.height > 70)) return;
    // ペンが書いている / 浮いている間の指は無視 (手のひら)
    if (type === 'touch' && (this.active?.type === 'pen' || performance.now() - this.penHoverAt < 350)) return;

    const fingerDraws = type === 'touch' && (this.getSettings().fingerDraw || !this.penSeen);
    if (type === 'touch' && (!fingerDraws || this.touches.size > 0 || this.active)) {
      // 指でスクロール (書いている指があれば、2 本目でスクロールに切り替える)
      if (this.active && this.active.type === 'touch') this.cancelActive();
      this.touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
      this.startPan();
      e.preventDefault();
      return;
    }
    if (type === 'mouse' && e.button === 1) {
      this.touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
      this.startPan();
      return;
    }
    if (type === 'mouse' && e.button !== 0) return;
    if (this.active) return;
    e.preventDefault();
    this.stopInertia();
    try { this.stage.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    const eraserButton = type === 'pen' && (e.button === 5 || (e.buttons & 32) !== 0);
    const erase = this.tool === 'eraser' || eraserButton;
    this.active = { id: e.pointerId, type, erase, p: [], drawn: 0, removed: [], last: null, rows: new Set() };
    this.emit('stroke-start');
    this.addEvent(e);
  }

  onMove(e) {
    const a = this.active;
    if (a && e.pointerId === a.id) {
      const list = typeof e.getCoalescedEvents === 'function' ? e.getCoalescedEvents() : null;
      const evs = list && list.length ? list : [e];
      for (const ev of evs) this.addEvent(ev);
      if (!a.erase) this.drawLive(); else this.requestDraw();
      this.emit('pen', e.clientX, e.clientY);
      return;
    }
    if (this.touches.has(e.pointerId)) {
      this.touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
      this.movePan();
      return;
    }
    if (e.pointerType === 'pen') {
      this.penHoverAt = performance.now();
      this.emit('pen', e.clientX, e.clientY);
      const showEraser = this.tool === 'eraser' || (e.buttons & 32) !== 0;
      if (showEraser) { this.rect = this.stage.getBoundingClientRect(); this.eraserAt = this.toPage(e.clientX, e.clientY); this.requestDraw(); }
      else if (this.eraserAt) { this.eraserAt = null; this.requestDraw(); }
    }
  }

  onUp(e, cancelled = false) {
    if (this.touches.has(e.pointerId)) {
      this.touches.delete(e.pointerId);
      if (this.touches.size === 0) this.endPan(); else this.startPan();
      return;
    }
    const a = this.active;
    if (!a || e.pointerId !== a.id) return;
    if (cancelled && a.type === 'touch') { this.cancelActive(); return; }
    this.active = null;
    const page = this.page;
    if (a.erase) {
      this.eraserAt = e.pointerType === 'pen' && this.tool === 'eraser' ? this.eraserAt : null;
      if (a.removed.length) {
        this.pushOp(page, { type: 'erase', items: a.removed });
        this.changed(page, a.removed.map((it) => it.stroke), 'erase');
      }
      this.requestDraw();
      this.emit('stroke-end');
      return;
    }
    if (!a.p.length) { this.emit('stroke-end'); return; }
    const s = { id: newId(), p: a.p.map((v) => Math.round(v * 10) / 10) };
    s.box = bboxOf(s.p);
    page.strokes.push(s);
    this.pushOp(page, { type: 'add', stroke: s });
    // 下の方まで書いたら行を足す
    const rows = rowsOfBox(s.box, page.rowH);
    const maxRow = Math.max(...rows);
    if (maxRow >= page.rows - 3) { page.rows = maxRow + 10; this.sizeSvg(this.marksSvg(page), page); }
    this.drawAll();
    this.changed(page, [s], 'add');
    this.emit('stroke-end');
  }

  cancelActive() {
    this.active = null;
    this.requestDraw();
    this.emit('stroke-end');
  }

  addEvent(ev) {
    const a = this.active;
    const { x, y } = this.toPage(ev.clientX, ev.clientY);
    if (a.erase) {
      const from = a.last || { x, y };
      this.eraseAlong(from.x, from.y, x, y, a);
      a.last = { x, y };
      this.eraserAt = { x, y };
      return;
    }
    let pr = a.type === 'pen' ? ev.pressure : 0.5;
    if (!(pr > 0)) pr = a.type === 'pen' ? 0.35 : 0.5;
    const targetW = BASE_W * (0.5 + 0.95 * Math.min(1, pr));
    const n = a.p.length;
    if (!n) { a.p.push(x, y, targetW); return; }
    const lx = a.p[n - 3], ly = a.p[n - 2], lw = a.p[n - 1];
    const dx = x - lx, dy = y - ly;
    if (dx * dx + dy * dy < 0.64) return;
    // 軽い追従平滑化 (手ぶれを抑える。遅れが出ない程度)
    const k = 0.62;
    a.p.push(lx + dx * k, ly + dy * k, lw + (targetW - lw) * 0.35);
  }

  eraseAlong(x0, y0, x1, y1, a) {
    const page = this.page;
    const len = Math.hypot(x1 - x0, y1 - y0);
    const steps = Math.max(1, Math.ceil(len / (ERASER_R * 0.5)));
    for (let i = page.strokes.length - 1; i >= 0; i--) {
      const s = page.strokes[i];
      const b = s.box || (s.box = bboxOf(s.p));
      const R = ERASER_R + 2;
      if (Math.max(x0, x1) < b.x0 - R || Math.min(x0, x1) > b.x1 + R || Math.max(y0, y1) < b.y0 - R || Math.min(y0, y1) > b.y1 + R) continue;
      let hit = false;
      for (let k = 0; k <= steps && !hit; k++) {
        const ex = x0 + (x1 - x0) * (k / steps), ey = y0 + (y1 - y0) * (k / steps);
        const p = s.p;
        if (p.length === 3) { hit = Math.hypot(p[0] - ex, p[1] - ey) < R + p[2]; break; }
        for (let j = 3; j < p.length; j += 3) {
          if (distToSeg(ex, ey, p[j - 3], p[j - 2], p[j], p[j + 1]) < R + p[j + 2] / 2) { hit = true; break; }
        }
      }
      if (hit) {
        page.strokes.splice(i, 1);
        a.removed.push({ stroke: s, index: i });
      }
    }
  }

  // ---------- 指でスクロール ----------
  centroid() {
    let x = 0, y = 0;
    for (const t of this.touches.values()) { x += t.x; y += t.y; }
    const n = this.touches.size || 1;
    return { x: x / n, y: y / n };
  }
  startPan() {
    this.stopInertia();
    const c = this.centroid();
    this.pan = { y: c.y, t: performance.now(), v: 0 };
  }
  movePan() {
    if (!this.pan) return;
    const c = this.centroid();
    const now = performance.now();
    const dy = c.y - this.pan.y;
    this.scrollBy(-dy / this.scale);
    const dt = Math.max(1, now - this.pan.t);
    this.pan.v = 0.8 * (-dy / this.scale / dt) + 0.2 * this.pan.v;
    this.pan.y = c.y; this.pan.t = now;
  }
  endPan() {
    if (!this.pan) return;
    let v = this.pan.v;
    this.pan = null;
    if (Math.abs(v) < 0.05) return;
    let last = performance.now();
    const step = (now) => {
      const dt = now - last; last = now;
      if (!this.scrollBy(v * dt)) { this.inertia = null; return; }
      v *= Math.pow(0.94, dt / 16);
      if (Math.abs(v) < 0.02) { this.inertia = null; return; }
      this.inertia = requestAnimationFrame(step);
    };
    this.inertia = requestAnimationFrame(step);
  }
  stopInertia() { if (this.inertia) cancelAnimationFrame(this.inertia); this.inertia = null; }

  // ---------- 編集 ----------
  setTool(t) { this.tool = t; if (t !== 'eraser') { this.eraserAt = null; this.requestDraw(); } this.emit('tool', t); }

  pushOp(page, op) {
    page.undo.push(op);
    if (page.undo.length > 200) page.undo.shift();
    page.redo.length = 0;
    this.emit('history');
  }

  changed(page, strokes, kind) {
    const rows = new Set();
    for (const s of strokes) for (const r of rowsOfBox(s.box || (s.box = bboxOf(s.p)), page.rowH)) rows.add(r);
    this.scheduleSave();
    this.emit('change', { page, rows: [...rows].sort((a, b) => a - b), kind });
  }

  canUndo() { return this.page.undo.length > 0; }
  canRedo() { return this.page.redo.length > 0; }

  undo() {
    const page = this.page, op = page.undo.pop();
    if (!op) return;
    const touched = this.applyOp(page, op, true);
    page.redo.push(op);
    this.drawAll();
    this.emit('history');
    this.changed(page, touched, 'undo');
  }
  redo() {
    const page = this.page, op = page.redo.pop();
    if (!op) return;
    const touched = this.applyOp(page, op, false);
    page.undo.push(op);
    this.drawAll();
    this.emit('history');
    this.changed(page, touched, 'redo');
  }
  applyOp(page, op, reverse) {
    if (op.type === 'add') {
      if (reverse) page.strokes = page.strokes.filter((s) => s !== op.stroke);
      else page.strokes.push(op.stroke);
      return [op.stroke];
    }
    if (op.type === 'erase') {
      if (reverse) {
        for (const it of [...op.items].reverse()) page.strokes.splice(Math.min(it.index, page.strokes.length), 0, it.stroke);
      } else {
        const gone = new Set(op.items.map((it) => it.stroke));
        page.strokes = page.strokes.filter((s) => !gone.has(s));
      }
      return op.items.map((it) => it.stroke);
    }
    if (op.type === 'clear') {
      page.strokes = reverse ? op.strokes.slice() : [];
      return op.strokes;
    }
    return [];
  }

  clearPage() {
    const page = this.page;
    if (!page.strokes.length) return;
    const op = { type: 'clear', strokes: page.strokes.slice() };
    page.strokes = [];
    this.pushOp(page, op);
    this.drawAll();
    this.changed(page, op.strokes, 'clear');
  }

  // ---------- ページ ----------
  addPage() {
    const page = makePage(this.getSettings().rowHeight || 72);
    this.pages.push(page);
    this.goto(this.pages.length - 1);
    this.scheduleSave();
    return page;
  }
  goto(i) {
    if (i < 0 || i >= this.pages.length) return;
    this.index = i;
    this.scrollY = 0;
    this.marksSvg(this.page);
    this.clampScroll();
    this.updateMarksTransform();
    this.drawAll();
    this.scheduleSave();
    this.emit('page', this.page);
    this.emit('history');
  }
  /** 空のページなら行の高さを変えられる */
  setRowHeight(h) {
    const page = this.page;
    if (!page.strokes.length && !page.marks.length) {
      page.rowH = h;
      this.sizeSvg(this.marksSvg(page), page);
      this.clampScroll();
      this.drawAll();
      this.scheduleSave();
    }
  }

  // ---------- 行の情報 ----------
  strokesInRow(row, page = this.page) {
    return page.strokes.filter((s) => rowsOfBox(s.box || (s.box = bboxOf(s.p)), page.rowH).includes(row));
  }
  /** 行のインクの左右の端 (無ければ null) */
  inkExtent(row, page = this.page) {
    let x0 = Infinity, x1 = -Infinity;
    for (const s of this.strokesInRow(row, page)) { x0 = Math.min(x0, s.box.x0); x1 = Math.max(x1, s.box.x1); }
    return x0 === Infinity ? null : { x0, x1 };
  }
  inkRows(page = this.page) {
    const set = new Set();
    for (const s of page.strokes) for (const r of rowsOfBox(s.box || (s.box = bboxOf(s.p)), page.rowH)) set.add(r);
    return set;
  }

  /** PC に送るページの絵: 白地・黒インク・行番号。長辺 maxSide px まで */
  exportPage({ changedRows = [], maxSide = 1600, page = this.page } = {}) {
    const ink = this.inkRows(page);
    const last = Math.min(page.rows, Math.max(3, ...ink, ...changedRows) + 1);
    const rh = page.rowH;
    const W = PAGE_W, H = last * rh;
    const k = Math.min(1.6, maxSide / W, maxSide / H);
    const cw = Math.round(W * k), ch = Math.round(H * k);
    const c = document.createElement('canvas');
    c.width = cw; c.height = ch;
    const ctx = c.getContext('2d');
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, cw, ch);
    ctx.setTransform(k, 0, 0, k, 0, 0);
    ctx.strokeStyle = '#d4d8de'; ctx.lineWidth = 1.2;
    ctx.beginPath();
    for (let r = 1; r < last; r++) { ctx.moveTo(8, r * rh); ctx.lineTo(W - 8, r * rh); }
    ctx.moveTo(MARGIN_X, 0); ctx.lineTo(MARGIN_X, H);
    ctx.stroke();
    ctx.fillStyle = '#8a8f98';
    ctx.font = `600 ${Math.round(Math.min(22, rh * 0.32))}px sans-serif`;
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    for (let r = 1; r <= last; r++) ctx.fillText(String(r), MARGIN_X - 12, (r - 0.5) * rh);
    ctx.strokeStyle = '#000000'; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    for (const s of page.strokes) {
      if (s.box.y0 > H) continue;
      for (const b of (s.paths || (s.paths = buildPaths(s.p)))) { ctx.lineWidth = Math.max(b.w, 2.2); ctx.stroke(b.path); }
    }
    const rows = [];
    for (let r = 1; r <= last; r++) rows.push({ row: r, top: Math.round((r - 1) * rh * k), bottom: Math.round(r * rh * k), hasInk: ink.has(r) });
    return { page: c.toDataURL('image/png'), rows, width: cw, height: ch };
  }

  // ---------- 保存 (この端末だけ) ----------
  scheduleSave() {
    clearTimeout(this._saveTimer);
    this._saveTimer = setTimeout(() => this.save(), 800);
  }
  save() {
    try {
      const data = {
        v: 1, index: this.index, penSeen: this.penSeen,
        pages: this.pages.map((p) => ({ id: p.id, rowH: p.rowH, rows: p.rows, problemId: p.problemId, marks: p.marks, solved: p.solved, strokes: p.strokes.map((s) => s.p) })),
      };
      localStorage.setItem(STORE_KEY, JSON.stringify(data));
    } catch { /* いっぱいなら諦める (画面のインクは消えない) */ }
  }
  load() {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      if (!raw) return;
      const d = JSON.parse(raw);
      this.penSeen = !!d.penSeen;
      this.pages = (d.pages || []).map((p) => ({
        ...makePage(p.rowH || 72), id: p.id || newId(), rows: p.rows || 24, problemId: p.problemId || null,
        marks: p.marks || [], solved: p.solved || null,
        strokes: (p.strokes || []).map((pts) => ({ id: newId(), p: pts })),
      }));
      this.index = Math.min(Math.max(0, d.index || 0), Math.max(0, this.pages.length - 1));
    } catch { this.pages = []; }
  }
  resetAll() {
    for (const svg of this.svgs.values()) svg.remove();
    this.svgs.clear();
    this.pages = [makePage(this.getSettings().rowHeight || 72)];
    this.index = 0;
    this.save();
    this.goto(0);
  }
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
