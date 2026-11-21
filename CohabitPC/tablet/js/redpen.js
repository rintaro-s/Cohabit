// 赤ペン: 妹がノートに書き込む線・丸・一言・花丸。インクとは別の層 (SVG、ページの単位)
import { PAGE_W, MARGIN_X } from './notebook.js';

const NS = 'http://www.w3.org/2000/svg';
const RED = '#e2343f';
let measureCtx = null;

function handFont() {
  return getComputedStyle(document.documentElement).getPropertyValue('--hand').trim() || 'sans-serif';
}

function hashSeed(str) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}
function rng(seed) {
  let s = seed || 1;
  return () => { s ^= s << 13; s ^= s >>> 17; s ^= s << 5; return ((s >>> 0) % 100000) / 100000; };
}
/** ゆっくり揺れるずれ (手で書いた線のふらつき) */
function wobble(r, amp) {
  const a1 = r() * 6.28, a2 = r() * 6.28, f1 = 0.6 + r() * 0.8, f2 = 1.7 + r() * 1.5;
  return (t) => amp * (0.65 * Math.sin(t * f1 * 6.28 + a1) + 0.35 * Math.sin(t * f2 * 6.28 + a2));
}
function pathFrom(pts) {
  if (!pts.length) return '';
  let d = `M${pts[0][0].toFixed(1)} ${pts[0][1].toFixed(1)}`;
  for (let i = 1; i < pts.length - 1; i++) {
    const mx = (pts[i][0] + pts[i + 1][0]) / 2, my = (pts[i][1] + pts[i + 1][1]) / 2;
    d += ` Q${pts[i][0].toFixed(1)} ${pts[i][1].toFixed(1)} ${mx.toFixed(1)} ${my.toFixed(1)}`;
  }
  const l = pts[pts.length - 1];
  d += ` L${l[0].toFixed(1)} ${l[1].toFixed(1)}`;
  return d;
}

function el(name, attrs = {}, parent) {
  const e = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v));
  if (parent) parent.appendChild(e);
  return e;
}

export class RedPen {
  constructor(notebook, fxLayer) {
    this.nb = notebook;
    this.fx = fxLayer;
    notebook.on('page', (page) => this.render(page));
    this.render(notebook.page);
  }

  /** ページの赤ペンを描き直す (動きなし) */
  render(page = this.nb.page) {
    const svg = this.nb.marksSvg(page);
    svg.textContent = '';
    for (const m of page.marks) this.drawMark(svg, page, m, { animate: false });
    if (page.solved) this.drawHanamaru(svg, page.solved, { animate: false });
  }

  /** PC からの marks を書き込む。書いていくように順番に出す */
  apply(marks, { animate = true } = {}) {
    const page = this.nb.page;
    const svg = this.nb.marksSvg(page);
    let delay = 0.05;
    let firstRow = null;
    for (const raw of marks || []) {
      if (!raw || !Number.isFinite(+raw.row)) continue;
      const m = { row: Math.max(1, Math.round(+raw.row)), kind: raw.kind || 'note', text: String(raw.text || '').slice(0, 60), x: raw.x == null ? null : Math.max(0, Math.min(1, +raw.x)), id: Math.random().toString(36).slice(2, 9) };
      if (m.row > page.rows) continue;
      // 同じ行の同じ種類の古い書き込みは置き換える
      this.removeWhere(page, (o) => o.row === m.row && (o.kind === m.kind || m.kind === 'mistake'));
      page.marks.push(m);
      delay = this.drawMark(svg, page, m, { animate, delay }) + 0.15;
      if (firstRow == null && (m.kind === 'mistake' || m.kind === 'unclear')) firstRow = m.row;
    }
    if (firstRow != null) this.nb.revealRow(firstRow);
    this.nb.scheduleSave();
    return delay;
  }

  /** 書き直した行の赤ペンを消す (花丸は残す) */
  clearRows(rows, kinds = ['mistake', 'unclear', 'ok', 'note']) {
    const page = this.nb.page;
    const set = new Set(rows);
    this.removeWhere(page, (m) => set.has(m.row) && kinds.includes(m.kind));
  }

  clearAll(page = this.nb.page) {
    page.marks = [];
    page.solved = null;
    this.render(page);
    this.nb.scheduleSave();
  }

  removeWhere(page, pred) {
    const gone = page.marks.filter(pred);
    if (!gone.length) return;
    page.marks = page.marks.filter((m) => !pred(m));
    const svg = this.nb.marksSvg(page);
    for (const m of gone) {
      const g = svg.querySelector(`g[data-id="${m.id}"]`);
      if (g) { g.classList.add('rp-fade'); setTimeout(() => g.remove(), 460); }
    }
    this.nb.scheduleSave();
  }

  // ---------- いろいろな印 ----------
  drawMark(svg, page, m, { animate, delay = 0 }) {
    const g = el('g', { 'data-id': m.id, 'data-row': m.row, class: 'rp-mark' }, svg);
    const r = rng(hashSeed(m.id + m.kind));
    const rh = page.rowH;
    const top = (m.row - 1) * rh, bot = m.row * rh, mid = top + rh * 0.5;
    const ext = this.nb.inkExtent(m.row, page);
    const xAt = m.x != null ? Math.max(MARGIN_X + 20, Math.min(PAGE_W - 30, m.x * PAGE_W)) : null;
    const size = Math.min(34, rh * 0.47);
    let t = delay;
    const stroke = (d, dur, w = 3.4) => {
      const p = el('path', { d, class: 'rp ' + (animate ? 'rp-draw' : 'rp-still'), 'stroke-width': w, pathLength: 1 }, g);
      if (animate) { p.style.setProperty('--delay', t.toFixed(2) + 's'); p.style.setProperty('--d', dur + 's'); }
      t += dur * 0.92;
      return p;
    };
    const write = (text, x, y, sz) => {
      t = this.text(g, text, x, y, sz, r, animate, t);
    };

    if (m.kind === 'mistake') {
      // 丸 (x があれば、その辺りを囲む)
      if (xAt != null) {
        const rx = Math.min(70, Math.max(40, rh * 0.75)), ry = rh * 0.44;
        stroke(this.ellipse(xAt, mid, rx, ry, r), 0.45, 3.2);
      }
      // 波線 (行のインクの下)
      const x0 = ext ? ext.x0 - 4 : (xAt ?? (MARGIN_X + 40)) - 110;
      const x1 = ext ? ext.x1 + 6 : (xAt ?? (MARGIN_X + 40)) + 110;
      stroke(this.wavy(Math.max(MARGIN_X + 6, x0), Math.min(PAGE_W - 16, x1), bot - 7, r), 0.55, 3);
      if (m.text) this.placeText(g, m.text, { ext, xAt, top, bot, mid, size, r, animate, tRef: () => t, setT: (v) => { t = v; }, page, leaderFrom: xAt });
    } else if (m.kind === 'ok') {
      const x = ext ? Math.min(PAGE_W - 50, ext.x1 + 26) : (xAt ?? PAGE_W - 80);
      stroke(this.check(x, mid + 4, Math.min(34, rh * 0.5), r), 0.35, 3.6);
      if (m.text) write(m.text, x + 40, mid + size * 0.36, size * 0.8);
    } else if (m.kind === 'unclear') {
      const x = xAt ?? (ext ? Math.min(PAGE_W - 60, ext.x1 + 24) : PAGE_W - 120);
      if (xAt != null) stroke(this.ellipse(xAt, mid, Math.min(60, rh * 0.7), rh * 0.42, r), 0.4, 2.6);
      const qx = xAt != null ? xAt + Math.min(60, rh * 0.7) + 6 : x;
      write('？', qx, mid + size * 0.5, size * 1.35);
      this.placeText(g, m.text || '読めないよ', { ext: ext && { x0: ext.x0, x1: Math.max(ext.x1, qx + size * 1.2) }, xAt, top, bot, mid, size: size * 0.85, r, animate, tRef: () => t, setT: (v) => { t = v; }, page });
    } else { // note
      this.placeText(g, m.text || 'いいね', { ext, xAt, top, bot, mid, size: size * 0.9, r, animate, tRef: () => t, setT: (v) => { t = v; }, page, flourish: true });
    }
    return t;
  }

  /** 一言の置き場所: 行のインクの右が空いていればそこ、無ければ下の行の上の方 */
  placeText(g, text, o) {
    const font = `600 ${o.size}px ${handFont()}`;
    const width = this.measure(text, font) * 1.04;
    const right = (o.ext ? o.ext.x1 : (o.xAt ?? MARGIN_X)) + 30;
    let x, y;
    let lines = [text];
    if (right + width < PAGE_W - 18) {
      x = right; y = o.mid + o.size * 0.36;
      if (!o.ext && o.leaderFrom != null && x - o.leaderFrom > 140) {
        // 丸から一言へ、細い線でつなぐ
        const p = el('path', { d: this.leader(o.leaderFrom + 30, o.top + 10, x - 10, y - o.size * 0.4, o.r), class: 'rp ' + (o.animate ? 'rp-draw' : 'rp-still'), 'stroke-width': 2.2, pathLength: 1 }, g);
        if (o.animate) { p.style.setProperty('--delay', o.tRef().toFixed(2) + 's'); p.style.setProperty('--d', '0.3s'); }
        o.setT(o.tRef() + 0.28);
      }
    } else {
      const maxW = PAGE_W - MARGIN_X - 60;
      if (width > maxW) lines = this.wrap(text, font, maxW);
      const lw = Math.min(width, maxW);
      const anchor = o.xAt ?? (o.ext ? (o.ext.x0 + o.ext.x1) / 2 : PAGE_W / 2);
      x = Math.max(MARGIN_X + 16, Math.min(PAGE_W - 24 - lw, anchor - lw * 0.3));
      y = o.bot + o.size * 0.98;
    }
    let t = o.tRef();
    lines.forEach((line, i) => { t = this.text(g, line, x, y + i * o.size * 1.15, o.size, o.r, o.animate, t); });
    if (o.flourish) {
      const w = Math.min(this.measure(lines[0], font), PAGE_W - x - 20);
      const p = el('path', { d: this.wavy(x - 2, x + w + 4, y + o.size * 0.3, o.r, 2.2, 30), class: 'rp ' + (o.animate ? 'rp-draw' : 'rp-still'), 'stroke-width': 2, pathLength: 1 }, g);
      if (o.animate) { p.style.setProperty('--delay', t.toFixed(2) + 's'); p.style.setProperty('--d', '0.35s'); }
      t += 0.3;
    }
    o.setT(t);
  }

  measure(text, font) {
    measureCtx ||= document.createElement('canvas').getContext('2d');
    measureCtx.font = font;
    return measureCtx.measureText(text).width;
  }
  wrap(text, font, maxW) {
    const lines = [];
    let cur = '';
    for (const ch of text) {
      if (this.measure(cur + ch, font) > maxW && cur) { lines.push(cur); cur = ch; } else cur += ch;
    }
    if (cur) lines.push(cur);
    return lines.slice(0, 3);
  }

  /** 1 文字ずつ、少し傾けて書いていく */
  text(g, str, x, y, size, r, animate, t) {
    const font = `600 ${size}px ${handFont()}`;
    let cx = x;
    for (const ch of str) {
      const w = this.measure(ch, font);
      const e = el('text', {
        x: cx.toFixed(1), y: (y + (r() - 0.5) * size * 0.08).toFixed(1),
        class: 'rp-text rp-char' + (animate ? '' : ' still'),
        'font-size': size.toFixed(1),
        transform: `rotate(${((r() - 0.5) * 7).toFixed(1)} ${(cx + w / 2).toFixed(1)} ${(y - size * 0.35).toFixed(1)})`,
      }, g);
      e.textContent = ch;
      if (animate) e.style.setProperty('--delay', t.toFixed(2) + 's');
      t += ch.trim() ? 0.075 : 0.03;
      cx += w * (0.98 + r() * 0.06);
    }
    return t;
  }

  wavy(x0, x1, y, r, amp = 4.2, wl = 22) {
    const pts = [];
    const n = Math.max(8, Math.round((x1 - x0) / 3));
    const drift = wobble(r, 2.2);
    const ampW = wobble(r, 0.25);
    for (let i = 0; i <= n; i++) {
      const t = i / n, x = x0 + (x1 - x0) * t;
      pts.push([x, y + drift(t) + amp * (1 + ampW(t)) * Math.sin(((x - x0) / wl) * Math.PI * 2)]);
    }
    return pathFrom(pts);
  }

  ellipse(cx, cy, rx, ry, r) {
    const pts = [];
    const start = -2.4 + (r() - 0.5) * 0.5;
    const turns = 1.12 + r() * 0.08;
    const wob = wobble(r, 0.06);
    const n = 64;
    for (let i = 0; i <= n; i++) {
      const t = i / n;
      const a = start - t * turns * Math.PI * 2;    // 反時計回り
      const grow = 1 + (t - 0.5) * 0.1 + wob(t);
      pts.push([cx + Math.cos(a) * rx * grow, cy + Math.sin(a) * ry * grow + (t - 0.5) * 3]);
    }
    return pathFrom(pts);
  }

  check(x, y, s, r) {
    const j = () => (r() - 0.5) * s * 0.06;
    const a = [x - s * 0.42 + j(), y - s * 0.08 + j()];
    const b = [x - s * 0.08 + j(), y + s * 0.32 + j()];
    const c = [x + s * 0.55 + j(), y - s * 0.62 + j()];
    const pts = [];
    for (let i = 0; i <= 6; i++) { const t = i / 6; pts.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t + Math.sin(t * Math.PI) * s * 0.04]); }
    for (let i = 1; i <= 10; i++) { const t = i / 10; pts.push([b[0] + (c[0] - b[0]) * t - Math.sin(t * Math.PI) * s * 0.08, b[1] + (c[1] - b[1]) * t]); }
    return pathFrom(pts);
  }

  leader(x0, y0, x1, y1, r) {
    const mx = (x0 + x1) / 2, my = Math.min(y0, y1) - 18 - r() * 8;
    const pts = [];
    for (let i = 0; i <= 16; i++) {
      const t = i / 16, u = 1 - t;
      pts.push([u * u * x0 + 2 * u * t * mx + t * t * x1, u * u * y0 + 2 * u * t * my + t * t * y1]);
    }
    return pathFrom(pts);
  }

  // ---------- 花丸 ----------
  hanamaru({ animate = true } = {}) {
    const page = this.nb.page;
    const svg = this.nb.marksSvg(page);
    svg.querySelectorAll('g.hanamaru').forEach((g) => g.remove());
    const ink = [...this.nb.inkRows(page)].sort((a, b) => a - b);
    const rh = page.rowH;
    const R = Math.min(125, Math.max(95, rh * 1.6));
    // 空いている所 (インクと赤ペンに重ならない、見えている範囲の右上から) を探す
    const occupied = page.strokes.map((s) => s.box).filter(Boolean);
    for (const g of svg.querySelectorAll('g.rp-mark')) {
      try { const b = g.getBBox(); occupied.push({ x0: b.x, y0: b.y, x1: b.x + b.width, y1: b.y + b.height }); } catch { /* 描く前 */ }
    }
    const visH = (this.nb.h - this.nb.oy - this.nb.bottomPad) / this.nb.scale;
    const yTop = Math.max(this.nb.scrollY, ink.length ? (ink[0] - 1) * rh : 0) + R + 6;
    const yBot = Math.max(yTop, this.nb.scrollY + visH - R - 6);
    let best = null;
    for (let cy = yTop; cy <= yBot; cy += 24) {
      for (let cx = PAGE_W - R - 26; cx >= MARGIN_X + R + 10; cx -= 30) {
        const k = R * 0.92;
        let ov = 0;
        for (const b of occupied) {
          const w = Math.min(cx + k, b.x1) - Math.max(cx - k, b.x0);
          const h = Math.min(cy + k, b.y1) - Math.max(cy - k, b.y0);
          if (w > 0 && h > 0) ov += w * h;
        }
        if (!best || ov < best.ov - 1) best = { cx, cy, ov };
        if (ov === 0) break;
      }
      if (best && best.ov === 0) break;
    }
    const { cx, cy } = best || { cx: PAGE_W - R - 30, cy: yTop };
    const solved = { cx, cy, R, id: Math.random().toString(36).slice(2, 8) };
    page.solved = solved;
    const dur = this.drawHanamaru(svg, solved, { animate });
    this.nb.scheduleSave();
    if (animate) setTimeout(() => this.sparkle(cx, cy, R), dur * 1000 - 300);
    return dur;
  }

  drawHanamaru(svg, { cx, cy, R, id }, { animate }) {
    const g = el('g', { class: 'hanamaru' }, svg);
    const r = rng(hashSeed('hana' + id));
    const wob = wobble(r, 0.035);
    // 真ん中のうずまき
    const spiral = [];
    const sTurns = 2.3, rIn = R * 0.05, rOut = R * 0.42;
    const a0 = r() * 6.28;
    for (let i = 0; i <= 120; i++) {
      const t = i / 120;
      const a = a0 + t * sTurns * Math.PI * 2;
      const rr = (rIn + (rOut - rIn) * Math.pow(t, 0.85)) * (1 + wob(t));
      spiral.push([cx + Math.cos(a) * rr, cy + Math.sin(a) * rr * 0.94]);
    }
    // 花びら (外向きの輪を 8 つ): z = Rb e^{it} + d e^{-i(n-1)t}
    const n = 10, dk = 0.3, rk = 1.3;
    const Rb = R / rk, d = Rb * dk;
    const petals = [];
    const pStart = a0 + sTurns * Math.PI * 2;
    const N = 360;
    for (let i = 0; i <= N; i++) {
      const t = (i / N) * (Math.PI * 2 + 0.06);
      const a = pStart + t;
      const k = 1 + wob(i / N) * 1.2;
      const x = cx + (Rb * Math.cos(a) + d * Math.cos(-(n - 1) * t + a0)) * k;
      const y = cy + (Rb * Math.sin(a) + d * Math.sin(-(n - 1) * t + a0)) * k * 0.94;
      petals.push([x, y]);
    }
    const mk = (pts, delay, dur, w) => {
      const p = el('path', { d: pathFrom(pts), class: 'rp ' + (animate ? 'rp-draw' : 'rp-still'), 'stroke-width': w, pathLength: 1 }, g);
      if (animate) { p.style.setProperty('--delay', delay + 's'); p.style.setProperty('--d', dur + 's'); }
    };
    mk(spiral, 0.1, 0.7, 5);
    mk(petals, 0.75, 1.25, 4.6);
    return 2.0;
  }

  sparkle(cx, cy, R) {
    const s = this.nb.toStage(cx, cy);
    const colors = ['#ff6f91', '#ffb3c6', '#ffd166', '#e2343f', '#a0c4ff'];
    for (let i = 0; i < 26; i++) {
      const d = document.createElement('i');
      d.className = 'spark' + (i % 3 === 0 ? ' star' : '');
      const a = Math.random() * Math.PI * 2;
      const dist = (R * this.nb.scale) * (0.9 + Math.random() * 0.9);
      d.style.left = s.x - 5 + 'px';
      d.style.top = s.y - 5 + 'px';
      d.style.background = colors[i % colors.length];
      d.style.setProperty('--dx', Math.cos(a) * dist + 'px');
      d.style.setProperty('--dy', Math.sin(a) * dist + 'px');
      d.style.animationDelay = Math.random() * 0.2 + 's';
      const sz = 6 + Math.random() * 8;
      d.style.width = d.style.height = sz + 'px';
      this.fx.appendChild(d);
      setTimeout(() => d.remove(), 1600);
    }
  }
}

export { RED };
