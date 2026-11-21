// 声を鳴らすための AudioContext (1 つだけ)。
//
// iPad (Safari) や多くのブラウザは、タップ・ペンの「その場」で作って resume した AudioContext でないと音を出さない。
// 以前は「始める」の後に全画面や VRM の読み込みを待ってから作っていたので、止まったまま (= 無音) になっていた。
// unlockAudio() は必ず、タップ / ペンの処理の中で、await より前に呼ぶ。
// 以後もタップ・ペン・キーのたびに、止まっていれば動かし直す。

let ctx = null;

export function audioCtx() { return ctx; }

export function audioRunning() { return !!ctx && ctx.state === 'running'; }

/** タップ / ペンの処理の中で同期的に呼ぶ */
export function unlockAudio() {
  try {
    if (!ctx) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      ctx = new Ctx();
      // 無音を 1 つ鳴らして解錠 (古い Safari 用)
      const b = ctx.createBuffer(1, 1, 22050);
      const s = ctx.createBufferSource();
      s.buffer = b;
      s.connect(ctx.destination);
      s.start(0);
    }
    if (ctx.state !== 'running') ctx.resume().catch(() => {});
  } catch (e) {
    console.warn('[audio] unlock', e);
  }
}

/** 止まっていれば動かし直す (最大 ms 待つ)。動いていれば true */
export async function ensureRunning(ms = 400) {
  if (!ctx) return false;
  if (ctx.state === 'running') return true;
  try {
    await Promise.race([ctx.resume(), new Promise((r) => setTimeout(r, ms))]);
  } catch { /* ignore */ }
  return ctx.state === 'running';
}

/** WAV などを読み込んで AudioBuffer に。古い Safari のコールバック形式にも対応 */
export function decode(arrayBuffer) {
  return new Promise((resolve, reject) => {
    try {
      const p = ctx.decodeAudioData(arrayBuffer, resolve, reject);
      if (p && typeof p.then === 'function') p.then(resolve, reject);
    } catch (e) { reject(e); }
  });
}

for (const t of ['pointerdown', 'keydown', 'touchend']) {
  document.addEventListener(t, () => { if (ctx && ctx.state !== 'running') ctx.resume().catch(() => {}); }, { capture: true, passive: true });
}
