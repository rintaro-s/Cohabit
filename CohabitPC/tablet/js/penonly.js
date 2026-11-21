// 「ペンだけ」: Apple Pencil (やスタイラス) 以外の入力 = 指を、書くのもボタンのタップもスクロールも、全部無視する。
// 手のひらや指の誤タップ対策。iPad では Pencil を一度使うと自動でこうなる (設定で変えられる)。
//
// iPad の Safari では Pencil も touch イベントを出す (touchType が 'stylus')。指は 'direct'。
// Pointer Events では Pencil が pointerType 'pen'、指が 'touch'。どちらも capture で先に受けて、指のものだけ止める。
// 逃げ道: 指 4 本を 2 秒押し続けると、指を使えるように戻す (Pencil の電池が切れたとき用)。

export const isIPad = () =>
  /iPad/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

/**
 * @param {() => boolean} isOn  今「ペンだけ」か
 * @param {() => void} onEscape 指 4 本の長押しで呼ぶ (設定を「指も使う」に戻す)
 */
export function installPenOnly(isOn, onEscape) {
  let lastType = '';
  let escapeTimer = null;
  const fingersOnly = (e) => e.changedTouches && Array.from(e.changedTouches).every((t) => t.touchType !== 'stylus');
  const block = (e) => { if (e.cancelable) e.preventDefault(); e.stopImmediatePropagation(); };
  // 指でも押せる所 (全画面に戻す・声を出す・始める画面)。ここまで止めると、指しか使えないときに戻れなくなる
  const fingerOk = (e) => {
    const t = e.target;
    return !!(t && t.closest && t.closest('[data-finger-ok]'));
  };
  const opts = { capture: true, passive: false };

  document.addEventListener('pointerdown', (e) => {
    lastType = e.pointerType;
    if (isOn() && e.pointerType === 'touch' && !fingerOk(e)) block(e);
  }, opts);
  for (const t of ['pointermove', 'pointerup', 'pointercancel', 'pointerover', 'pointerenter']) {
    document.addEventListener(t, (e) => { if (isOn() && e.pointerType === 'touch' && !fingerOk(e)) e.stopImmediatePropagation(); }, opts);
  }

  document.addEventListener('touchstart', (e) => {
    if (!isOn() || !fingersOnly(e) || fingerOk(e)) return;
    // 逃げ道: 指 4 本以上を 2 秒
    const fingers = Array.from(e.touches).filter((t) => t.touchType !== 'stylus').length;
    if (fingers >= 4 && !escapeTimer) escapeTimer = setTimeout(() => { escapeTimer = null; onEscape(); }, 2000);
    block(e);
  }, opts);
  const cancelEscape = (e) => {
    if (escapeTimer && Array.from(e.touches || []).filter((t) => t.touchType !== 'stylus').length < 4) {
      clearTimeout(escapeTimer);
      escapeTimer = null;
    }
  };
  document.addEventListener('touchmove', (e) => { if (isOn() && fingersOnly(e) && !fingerOk(e)) block(e); }, opts);
  document.addEventListener('touchend', (e) => { cancelEscape(e); if (isOn() && fingersOnly(e) && !fingerOk(e)) block(e); }, opts);
  document.addEventListener('touchcancel', cancelEscape, opts);

  // 指から出た click (ボタン・入力欄) も止める
  for (const t of ['click', 'dblclick', 'contextmenu']) {
    document.addEventListener(t, (e) => { if (isOn() && lastType === 'touch' && !fingerOk(e)) block(e); }, opts);
  }
  // 指でのピンチ拡大 (iOS の gesture イベント)
  for (const t of ['gesturestart', 'gesturechange']) {
    document.addEventListener(t, (e) => { if (isOn()) block(e); }, opts);
  }
}
