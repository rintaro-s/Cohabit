// 妹: VRM の胸から上 (three.js + three-vrm)。まばたき・呼吸・ペンを見る・表情・口パク
// VRM が読めないときは絵 (SVG) で代わりをする。
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm';
import { audioCtx, unlockAudio, ensureRunning, decode } from './audio.js';

// 約束の表情 (Say.expression) → VRM の表情と重み (0..1)
const EXPRESSIONS = {
  neutral: {},
  happy: { happy: 0.85 },
  thinking: { relaxed: 0.25, sad: 0.12 },
  surprised: { surprised: 0.8 },
  worried: { sad: 0.55 },
  // 念のため VRM の名前そのままも受ける
  relaxed: { relaxed: 0.7 },
  sad: { sad: 0.7 },
  angry: { angry: 0.6 },
  joy: { happy: 0.85 },
};

export function placeholderSvg(mouthId = 'phMouth', eyesId = 'phEyes') {
  const u = mouthId;
  return `
<svg viewBox="0 0 300 330" xmlns="http://www.w3.org/2000/svg" aria-label="妹">
  <defs>
    <linearGradient id="${u}Hair" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#6b4a5e"/><stop offset="1" stop-color="#4a3245"/></linearGradient>
    <linearGradient id="${u}Skin" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff1ea"/><stop offset="1" stop-color="#ffe2d6"/></linearGradient>
  </defs>
  <path d="M60 330c4-62 40-92 90-92s86 30 90 92z" fill="#fff" />
  <path d="M60 330c4-62 40-92 90-92s86 30 90 92z" fill="#ffd3df" opacity=".55"/>
  <path d="M118 248l32 30 32-30" fill="none" stroke="#ff6f91" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M70 168c-8-70 30-118 80-118s88 48 80 118c-6 48-20 70-26 78H96c-6-8-20-30-26-78z" fill="url(#${u}Hair)"/>
  <rect x="134" y="206" width="32" height="40" rx="12" fill="#ffe2d6"/>
  <ellipse cx="150" cy="150" rx="66" ry="72" fill="url(#${u}Skin)"/>
  <path d="M84 132c10-46 40-62 66-62s58 14 68 62c-22-12-34-28-38-44-14 22-50 40-96 44z" fill="url(#${u}Hair)"/>
  <g id="${eyesId}" style="transform-origin:150px 156px">
    <ellipse cx="124" cy="156" rx="9" ry="12" fill="#3b2a3a"/><ellipse cx="176" cy="156" rx="9" ry="12" fill="#3b2a3a"/>
    <circle cx="127" cy="151" r="3.2" fill="#fff"/><circle cx="179" cy="151" r="3.2" fill="#fff"/>
  </g>
  <ellipse cx="108" cy="180" rx="12" ry="7" fill="#ff9fb3" opacity=".45"/><ellipse cx="192" cy="180" rx="12" ry="7" fill="#ff9fb3" opacity=".45"/>
  <ellipse id="${mouthId}" cx="150" cy="190" rx="9" ry="3" fill="#d9476b" style="transform-origin:150px 190px"/>
  <circle cx="98" cy="96" r="12" fill="#ff6f91"/><circle cx="110" cy="88" r="7" fill="#ffb3c6"/>
</svg>`;
}

export class Sister {
  constructor({ card, canvas, placeholder, loading }) {
    this.card = card;
    this.canvas = canvas;
    this.placeholderEl = placeholder;
    this.loadingEl = loading;
    this.vrm = null;
    this.mode = 'loading';           // loading | vrm | placeholder
    this.running = false;
    this.visible = true;
    this.exprTarget = {};
    this.exprNow = {};
    this.mouth = 0;
    this.mouthTarget = 0;
    this.blinkT = 2 + Math.random() * 3;
    this.blinkPhase = -1;
    this.look = { x: 0, y: 0, tx: 0, ty: 0 };
    this.lookIdleAt = 0;
    this.clock = new THREE.Clock();
    this.analyser = null;
    this.audioCtx = null;
    this.placeholderEl.innerHTML = placeholderSvg();
    this._tick = this._tick.bind(this);
  }

  // ---------- 読み込み ----------
  async load(url) {
    try {
      this.renderer = new THREE.WebGLRenderer({ canvas: this.canvas, alpha: true, antialias: true, powerPreference: 'low-power' });
    } catch (e) {
      console.warn('[sister] WebGL が使えない', e);
      this.usePlaceholder();
      return false;
    }
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(20, 1, 0.05, 20);
    const key = new THREE.DirectionalLight(0xffffff, 2.2);
    key.position.set(-0.6, 1.4, 1.6);
    this.scene.add(key);
    this.scene.add(new THREE.AmbientLight(0xffffff, 0.9));
    this.lookTarget = new THREE.Object3D();
    this.scene.add(this.lookTarget);
    this.resize();
    new ResizeObserver(() => this.resize()).observe(this.card);
    this.start();

    const loader = new GLTFLoader();
    loader.register((parser) => new VRMLoaderPlugin(parser));
    try {
      const gltf = await new Promise((resolve, reject) => {
        loader.load(url, resolve, (ev) => {
          if (ev.lengthComputable && this.loadingEl) this.loadingEl.textContent = `妹を呼んでるところ… ${Math.round((ev.loaded / ev.total) * 100)}%`;
        }, reject);
      });
      const vrm = gltf.userData.vrm;
      if (!vrm) throw new Error('VRM ではない');
      VRMUtils.removeUnnecessaryVertices(gltf.scene);
      VRMUtils.combineSkeletons?.(gltf.scene);
      VRMUtils.rotateVRM0(vrm);
      vrm.scene.traverse((o) => { o.frustumCulled = false; });
      this.scene.add(vrm.scene);
      this.vrm = vrm;
      if (vrm.lookAt) { vrm.lookAt.target = this.lookTarget; vrm.lookAt.autoUpdate = true; }
      this.pose();
      vrm.update(0);
      this.frame();
      this.mode = 'vrm';
      this.card.classList.add('ready');
      this.card.classList.remove('placeholder');
      return true;
    } catch (e) {
      console.warn('[sister] VRM が読めない', e);
      this.usePlaceholder();
      return false;
    }
  }

  usePlaceholder() {
    this.mode = 'placeholder';
    this.card.classList.add('placeholder');
    this.start();
  }

  /** 腕を下ろす (T ポーズにしない) */
  pose() {
    const h = this.vrm.humanoid;
    const set = (name, x, y, z) => { const b = h.getNormalizedBoneNode(name); if (b) b.rotation.set(x, y, z); };
    set('leftUpperArm', 0, 0, -1.22);
    set('rightUpperArm', 0, 0, 1.22);
    set('leftLowerArm', 0, -0.25, -0.12);
    set('rightLowerArm', 0, 0.25, 0.12);
    set('leftHand', 0, 0, -0.1);
    set('rightHand', 0, 0, 0.1);
    // 少しノートの方 (画面の左) を向く
    this.vrm.scene.rotation.y = -0.32;
  }

  /** 頭から胸の上が入るようにカメラを置く */
  frame() {
    const head = this.vrm.humanoid.getRawBoneNode('head') || this.vrm.humanoid.getNormalizedBoneNode('head');
    const chest = this.vrm.humanoid.getRawBoneNode('upperChest') || this.vrm.humanoid.getRawBoneNode('chest');
    this.vrm.scene.updateMatrixWorld(true);
    const hp = new THREE.Vector3(); head.getWorldPosition(hp);
    const cp = new THREE.Vector3(); (chest || head).getWorldPosition(cp);
    this.headPos = hp.clone();
    const span = Math.max(0.22, hp.y - cp.y);
    const topY = hp.y + span * 0.85;          // 頭のてっぺんの少し上
    const botY = cp.y - span * 0.6;           // 胸の上
    const centerY = (topY + botY) / 2;
    const height = topY - botY;
    const fov = this.camera.fov * Math.PI / 180;
    const dist = (height / 2) / Math.tan(fov / 2);
    this.camera.position.set(hp.x - 0.06, centerY + 0.01, hp.z + dist);
    this.camTarget = new THREE.Vector3(hp.x - 0.02, centerY, hp.z);
    this.camera.lookAt(this.camTarget);
    this.lookTarget.position.copy(this.camera.position);
  }

  resize() {
    if (!this.renderer) return;
    const r = this.card.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return;
    this.renderer.setSize(r.width, r.height, false);
    this.camera.aspect = r.width / r.height;
    // 縦長のときも胸から上が切れないように
    this.camera.fov = this.camera.aspect < 0.85 ? 22 / Math.max(0.6, this.camera.aspect / 0.85) : 22;
    this.camera.updateProjectionMatrix();
    if (this.vrm) this.frame();
  }

  // ---------- 動き ----------
  start() { if (!this.running) { this.running = true; this.clock.getDelta(); requestAnimationFrame(this._tick); } }
  setVisible(v) { this.visible = v; if (v) this.start(); }

  _tick() {
    if (!this.visible) { this.running = false; return; }
    requestAnimationFrame(this._tick);
    const dt = Math.min(0.1, this.clock.getDelta());
    const t = this.clock.elapsedTime;
    this.updateMouth(dt);
    this.updateBlink(dt);
    this.updateLook(dt, t);
    // 表情は滑らかに
    const names = new Set([...Object.keys(this.exprTarget), ...Object.keys(this.exprNow)]);
    for (const n of names) {
      const target = this.exprTarget[n] || 0;
      const now = this.exprNow[n] || 0;
      this.exprNow[n] = now + (target - now) * Math.min(1, dt * 6);
    }
    if (this.mode === 'vrm') this.renderVrm(dt, t);
    else if (this.mode === 'placeholder') this.renderPlaceholder();
  }

  renderVrm(dt, t) {
    const vrm = this.vrm;
    const h = vrm.humanoid;
    const em = vrm.expressionManager;
    // 呼吸と、ゆるい揺れ
    const breath = Math.sin(t * 1.7);
    const chest = h.getNormalizedBoneNode('chest') || h.getNormalizedBoneNode('spine');
    if (chest) chest.rotation.x = breath * 0.012;
    const spine = h.getNormalizedBoneNode('spine');
    if (spine) spine.rotation.z = Math.sin(t * 0.5) * 0.012;
    const neck = h.getNormalizedBoneNode('neck');
    const head = h.getNormalizedBoneNode('head');
    if (neck) { neck.rotation.y = this.look.x * 0.18; neck.rotation.x = -this.look.y * 0.08; }
    if (head) {
      head.rotation.y = this.look.x * 0.22;
      head.rotation.x = -this.look.y * 0.12 + Math.sin(t * 0.9) * 0.01;
      head.rotation.z = Math.sin(t * 0.6) * 0.02 + (this.exprTarget.relaxed ? 0.06 : 0);
    }
    const shoulderL = h.getNormalizedBoneNode('leftShoulder'), shoulderR = h.getNormalizedBoneNode('rightShoulder');
    if (shoulderL) shoulderL.rotation.z = breath * 0.01;
    if (shoulderR) shoulderR.rotation.z = -breath * 0.01;
    // 目線の先 (カメラから見てペンの方)
    if (this.camera && this.headPos) {
      this.lookTarget.position.set(
        this.camera.position.x + this.look.x * 0.9,
        this.camera.position.y + this.look.y * 0.5,
        this.camera.position.z,
      );
    }
    if (em) {
      for (const [n, v] of Object.entries(this.exprNow)) if (em.getExpression(n)) em.setValue(n, Math.max(0, Math.min(1, v)));
      // 笑っているときはまばたきを弱く (目が二重に閉じないように)
      const blinkScale = 1 - Math.min(0.8, (this.exprNow.happy || 0) * 0.9);
      em.setValue('blink', this.blinkValue() * blinkScale);
      em.setValue('aa', Math.min(1, this.mouth * 1.1));
      em.setValue('oh', Math.min(0.35, this.mouth * 0.25));
    }
    vrm.update(dt);
    this.renderer.render(this.scene, this.camera);
  }

  renderPlaceholder() {
    const m = this.placeholderEl.querySelector('#phMouth');
    const e = this.placeholderEl.querySelector('#phEyes');
    const happy = this.exprNow.happy || 0;
    if (m) m.style.transform = `scale(${1 + happy * 0.4}, ${1 + this.mouth * 2.6})`;
    if (e) e.style.transform = `scaleY(${Math.max(0.08, 1 - this.blinkValue() - happy * 0.5)})`;
  }

  updateBlink(dt) {
    if (this.blinkPhase < 0) {
      this.blinkT -= dt;
      if (this.blinkT <= 0) { this.blinkPhase = 0; this.blinkT = 2.2 + Math.random() * 3.5; }
    } else {
      this.blinkPhase += dt / 0.16;
      if (this.blinkPhase >= 1) this.blinkPhase = -1;
    }
  }
  blinkValue() { return this.blinkPhase < 0 ? 0 : Math.sin(this.blinkPhase * Math.PI); }

  updateLook(dt, t) {
    // しばらくペンが動かなければ、こちら (カメラ) を見る
    if (performance.now() - this.lookIdleAt > 2500) { this.look.tx = Math.sin(t * 0.3) * 0.08; this.look.ty = 0.02; }
    const k = Math.min(1, dt * 5);
    this.look.x += (this.look.tx - this.look.x) * k;
    this.look.y += (this.look.ty - this.look.y) * k;
  }

  /** 画面の座標 (ペン) の方を見る */
  lookAtScreen(clientX, clientY) {
    const r = this.card.getBoundingClientRect();
    if (!r.width) return;
    const cx = r.left + r.width / 2, cy = r.top + r.height * 0.35;
    const dx = (clientX - cx) / window.innerWidth;
    const dy = (cy - clientY) / window.innerHeight;
    this.look.tx = Math.max(-1, Math.min(0.6, dx * 1.6));
    this.look.ty = Math.max(-0.6, Math.min(0.6, dy * 1.2));
    this.lookIdleAt = performance.now();
  }

  setExpression(name) {
    this.exprTarget = { ...(EXPRESSIONS[name] || {}) };
  }

  // ---------- 声と口パク ----------
  unlockAudio() {
    unlockAudio();
    this.ensureAnalyser();
  }

  ensureAnalyser() {
    const ctx = audioCtx();
    if (!ctx || this.analyser) return;
    try {
      this.analyser = ctx.createAnalyser();
      this.analyser.fftSize = 1024;
      this.analyser.connect(ctx.destination);
      this.levelBuf = new Float32Array(this.analyser.fftSize);
    } catch (e) { console.warn('[sister] analyser', e); this.analyser = null; }
  }

  /**
   * 音声を鳴らして口を動かす。
   * 鳴らせたら true、鳴らせなかったら false、音が止められている (タップが要る) なら 'locked'。
   * まず Web Audio (取ってきて解いて鳴らす。止まっていなければ確実に鳴る) で、だめなら <audio> で。
   */
  async play(url) {
    this.stopAudio();
    const token = (this._playToken = (this._playToken || 0) + 1);
    const ctx = audioCtx();
    if (ctx) {
      try {
        const res = await fetch(url, { cache: 'no-store' });
        if (token !== this._playToken) return false;
        if (res.status === 204) return false; // 声のサーバーが無い (字幕だけ)
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const buf = await decode(await res.arrayBuffer());
        if (token !== this._playToken) return false;
        if (!(await ensureRunning())) return 'locked';
        this.ensureAnalyser();
        return await new Promise((resolve) => {
          const src = ctx.createBufferSource();
          src.buffer = buf;
          src.connect(this.analyser || ctx.destination);
          let done = false;
          const finish = (ok) => {
            if (done) return; done = true;
            this.speaking = false;
            this.mouthTarget = 0;
            this.card.classList.remove('talking');
            try { src.disconnect(); } catch { /* ignore */ }
            resolve(ok);
          };
          src.onended = () => finish(true);
          this._stopSource = () => { try { src.stop(); } catch { /* ignore */ } finish(false); };
          this.speaking = true;
          this.card.classList.add('talking');
          src.start(0);
        });
      } catch (e) {
        console.warn('[sister] web audio play failed, trying <audio>', e);
        if (token !== this._playToken) return false;
      }
    }
    return this.playElement(url, token);
  }

  /** 予備: <audio> で鳴らす (AudioContext は通さない。止まった AudioContext に通すと無音になるため) */
  playElement(url, token) {
    const audio = new Audio();
    audio.preload = 'auto';
    audio.src = url;
    this.audio = audio;
    return new Promise((resolve) => {
      let done = false;
      const finish = (ok) => {
        if (done) return; done = true;
        this.card.classList.remove('talking');
        this.fakeUntil = 0;
        resolve(ok);
      };
      audio.addEventListener('ended', () => finish(true));
      audio.addEventListener('error', () => finish(false));
      audio.addEventListener('playing', () => { this.fakeTalk(Math.max(800, (audio.duration || 3) * 1000)); });
      this._finish = finish;
      audio.play().catch((e) => finish(e && e.name === 'NotAllowedError' ? 'locked' : false));
      if (token !== this._playToken) finish(false);
    });
  }

  stopAudio() {
    this._playToken = (this._playToken || 0) + 1;
    if (this._stopSource) { const f = this._stopSource; this._stopSource = null; f(); }
    if (this.audio) { try { this.audio.pause(); } catch { /* ignore */ } this.audio = null; }
    this._finish?.(false);
    this._finish = null;
  }

  /** 音が無いとき (字幕だけ) の口パク */
  fakeTalk(ms) {
    this.fakeUntil = performance.now() + ms;
    this.card.classList.add('talking');
    clearTimeout(this._fakeTimer);
    this._fakeTimer = setTimeout(() => this.card.classList.remove('talking'), ms);
  }

  updateMouth(dt) {
    let level = 0;
    if (this.speaking && this.analyser) {
      this.analyser.getFloatTimeDomainData(this.levelBuf);
      let sum = 0;
      for (let i = 0; i < this.levelBuf.length; i++) sum += this.levelBuf[i] * this.levelBuf[i];
      const rms = Math.sqrt(sum / this.levelBuf.length);
      level = Math.min(1, Math.max(0, (rms - 0.015) * 7));
    } else if (this.fakeUntil && performance.now() < this.fakeUntil) {
      const t = performance.now() / 1000;
      level = Math.max(0, Math.sin(t * 13) * 0.5 + Math.sin(t * 7.3) * 0.3) * 0.8;
    }
    // 開くのは速く、閉じるのはゆっくり
    const k = level > this.mouth ? Math.min(1, dt * 28) : Math.min(1, dt * 12);
    this.mouth += (level - this.mouth) * k;
  }
}
