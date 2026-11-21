"""
Cohabit タブレット (TABLET_API.md) の PC 側。cohabit_pc.py が同じポートで配る。標準ライブラリだけ。

タブレット (Android の Chrome) は LAN から来る。/api/* と違って LAN に公開し、設定の token があれば
X-Cohabit-Token ヘッダーか ?token= を求める (この PC からは要らない)。静的ファイル (/tablet/ の HTML・JS) には
秘密が無いので token なしで配る (ページを開く URL にヘッダーは付けられないため)。

  GET  /tablet/                    tablet/ の静的ファイル (index.html)。.. で外に出られない
  GET  /tablet/vrm                 妹の VRM (設定 tablet.vrmPath)
  GET  /tablet/api/session         妹・今の問題・居場所・推論先・苦手の要約
  POST /tablet/api/problem         問題を作る (topic / photo / weak / next / text)。模範解答は PC にだけ置く
  POST /tablet/api/check           ノートの画像に赤ペン (行ごとの印・解けたか・ひとこと)。source: "quest" なら机の写真 (印なし)
  POST /tablet/api/ask             hint / explain / answer
  POST /tablet/api/answer          Quest から: 声で言った答えを模範解答と比べる → { correct, solved, say }
  GET  /tablet/api/speech          読み上げ WAV (VOICEVOX)。使えなければ 204
  GET  /tablet/api/events          SSE: presence / say / problem
  POST /tablet/api/presence        Quest・画面から { where: "room"|"tablet" } か { wearing: bool }
  POST /tablet/api/say             Quest・画面から妹のひとことをタブレットへ { text, expression? }

問題の理解・計算・正誤・ヒント・説明はすべて LLM がする (教科ごとのコードは持たない)。
学習記録は data/learning.jsonl、苦手の要約は data/weak_points.json (画面の「データ」で消せる)。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import re
import threading
import time
import unicodedata
import urllib.parse
import uuid
from collections import OrderedDict
from fractions import Fraction

HERE = os.path.dirname(os.path.abspath(__file__))
TABLET_DIR = os.path.join(HERE, "tablet")  # 別の担当が書く。ここでは読むだけ
DEFAULT_VRM = "../Cohabit/Assets/Models/Sis_3b1fdc3d.vrm"
LEARNING_FILE = "learning.jsonl"
WEAK_FILE = "weak_points.json"

MIME = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8", ".map": "application/json; charset=utf-8",
    ".wasm": "application/wasm", ".vrm": "model/gltf-binary", ".glb": "model/gltf-binary", ".gltf": "model/gltf+json",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".svg": "image/svg+xml", ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf",
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".fbx": "application/octet-stream",
    ".vrma": "model/gltf-binary", ".bin": "application/octet-stream",
}

EXPRESSIONS = ("neutral", "happy", "thinking", "surprised", "worried")
# LLM を使う口。LLM が使えないとき (429 / 502 / 503 / 504) はエラーに "say" を付ける
LLM_PATHS = ("/tablet/api/problem", "/tablet/api/check", "/tablet/api/ask", "/tablet/api/answer")
WEAK_RETRY_SECONDS = 20  # 苦手の要約を後回しにされたとき、やり直すまで
# 「苦手から」なのに学習記録が無いとき (話題を勝手に決めない)
NO_RECORDS_MESSAGE = "まだ学習記録がありません。何を勉強するか教えてください"
FALLBACK_SAY = {
    429: {"text": "ちょっと待って、考えすぎた。少ししたらもう一回。", "expression": "thinking"},
    502: {"text": "ごめん、うまく考えられなかった。もう一回やってみて。", "expression": "worried"},
    503: {"text": "ごめん、今ちょっと頭が回らない。少ししたらもう一回言って。", "expression": "worried"},
    504: {"text": "ごめん、考えるのに時間がかかりすぎた。もう一回言って。", "expression": "worried"},
}
MARK_KINDS = ("ok", "mistake", "unclear", "note")
LEVELS = ("easier", "same", "harder")

# ---------------------------------------------------------------------------
# 妹の口調 (1 か所)
# ---------------------------------------------------------------------------

SISTER = """あなたは「{sister}」。勉強している相手 (中学生以上) のそばにいる年下の妹で、教科を問わず何でもわかる賢い子。
話し方: タメ口。短く (1〜2 文)。です・ます は使わない。絵文字・顔文字・記号の飾りは使わない。{call}
教え方: 答えは聞かれるまで言わない。間違いは「どこが」だけ指して、直し方は本人に考えさせる。できたら素直にほめる。
自分の仕組みや決まりのことは口に出さない。
数式は Unicode で書く (x²、√2、½、×、÷、−)。LaTeX や $ は使わない。
返事は指定された JSON だけ。前後に説明や ``` を付けない。"""


def sister_prompt(cfg: dict) -> str:
    t = cfg.get("tablet") or {}
    name = (t.get("studentName") or "").strip()
    call = f"相手のことは「{name}」と呼ぶ。" if name else "相手を名前で呼ばない。"
    return SISTER.format(sister=t.get("sisterName") or "妹", call=call)


# ---------------------------------------------------------------------------
# LLM の JSON を読む (考えた跡・コードの囲い・壊れた JSON)
# ---------------------------------------------------------------------------


def strip_noise(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S | re.I)
    text = re.sub(r"^.*?</think>", "", text, flags=re.S | re.I)  # 開きタグが無い考えた跡
    text = re.sub(r"<think>.*$", "", text, flags=re.S | re.I)    # 閉じずに切れた考えた跡
    fence = re.search(r"```(?:json|JSON)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        text = re.sub(r"```(?:json|JSON)?", "", text)
    return text.strip()


def repair_json(text: str) -> str:
    """よくある壊れ方を直す: 全角の引用符・末尾のカンマ・Python の True/None・引用符の無いキー・途中で切れた終わり。"""
    s = text.replace("“", '"').replace("”", '"').replace("＂", '"')
    out, stack, in_str, esc = [], [], False, False
    i = 0
    while i < len(s):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            elif c == "\n":
                c = "\\n"
            out.append(c)
            i += 1
            continue
        if c == '"':
            in_str = True
        elif c in "{[":
            stack.append("}" if c == "{" else "]")
        elif c in "}]":
            # 閉じる前のカンマを消す
            j = len(out) - 1
            while j >= 0 and out[j].isspace():
                j -= 1
            if j >= 0 and out[j] == ",":
                del out[j]
            if stack:
                stack.pop()
        else:
            m = re.match(r"(True|False|None)\b", s[i:])
            if m and (not out or not (out[-1].isalnum() or out[-1] == "_")):
                out.append({"True": "true", "False": "false", "None": "null"}[m.group(1)])
                i += len(m.group(1))
                continue
            m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)(\s*):", s[i:])
            if m and m.group(1) not in ("true", "false", "null") and "".join(out).rstrip()[-1:] in ("{", ","):
                out.append(f'"{m.group(1)}"{m.group(2)}:')
                i += len(m.group(0))
                continue
        out.append(c)
        i += 1
    if in_str:
        if esc:
            out.pop()
        out.append('"')
    tail = "".join(out).rstrip()
    tail = re.sub(r",\s*$", "", tail)
    tail = re.sub(r'(,|\{)\s*"[^"]*"\s*:?\s*$', r"\1", tail)  # 値の無いキーで切れた
    tail = re.sub(r",\s*$", "", tail)
    return tail + "".join(reversed(stack))


def parse_json_loose(text: str):
    """LLM の返事から JSON (dict) を取り出す。読めなければ None。"""
    s = strip_noise(text)
    start = s.find("{")
    if start < 0:
        return None
    s = s[start:]
    try:
        return json.loads(s)
    except ValueError:
        pass
    # 最初のオブジェクトだけ (後ろにおまけの文があるとき)
    try:
        obj, _ = json.JSONDecoder().raw_decode(s)
        return obj
    except ValueError:
        pass
    try:
        return json.loads(repair_json(s))
    except ValueError:
        pass
    end = s.rfind("}")
    if end > 0:
        try:
            return json.loads(repair_json(s[: end + 1]))
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# LaTeX を素の文字に (モデルが「$x$」「\frac{1}{2}」と書くことがある。プロンプトでも Unicode を頼んでいる)
# ---------------------------------------------------------------------------

_LATEX_SYMBOLS = {
    r"\times": "×", r"\div": "÷", r"\cdot": "·", r"\pm": "±", r"\mp": "∓", r"\leq": "≤", r"\le": "≤", r"\geq": "≥",
    r"\ge": "≥", r"\neq": "≠", r"\ne": "≠", r"\approx": "≈", r"\pi": "π", r"\theta": "θ", r"\alpha": "α", r"\beta": "β",
    r"\infty": "∞", r"\to": "→", r"\rightarrow": "→", r"\Rightarrow": "⇒", r"\degree": "°", r"\circ": "°",
    r"\angle": "∠", r"\triangle": "△", r"\perp": "⊥", r"\parallel": "∥", r"\left": "", r"\right": "", r"\,": " ",
    r"\;": " ", r"\!": "", r"\quad": " ",
}
_SUPERSCRIPT = str.maketrans("0123456789+-n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻ⁿ")


def _latex_inner(m: re.Match) -> str:
    s = m.group(1)
    for _ in range(3):  # 入れ子の \frac{\sqrt{2}}{2} も
        s = re.sub(r"\\[dt]?frac\{([^{}]*)\}\{([^{}]*)\}", lambda f: f"{f.group(1)}/{f.group(2)}"
                   if len(f.group(1)) <= 1 and len(f.group(2)) <= 1 else f"({f.group(1)})/({f.group(2)})", s)
        s = re.sub(r"\\sqrt\{([^{}]*)\}", lambda r: "√" + (r.group(1) if len(r.group(1)) <= 1 else f"({r.group(1)})"), s)
    s = re.sub(r"\^\{([0-9+\-n]+)\}|\^([0-9n])", lambda e: (e.group(1) or e.group(2)).translate(_SUPERSCRIPT), s)
    for k in sorted(_LATEX_SYMBOLS, key=len, reverse=True):
        s = re.sub(re.escape(k) + r"(?![A-Za-z])", _LATEX_SYMBOLS[k], s)
    s = re.sub(r"\\text\{([^{}]*)\}|\\mathrm\{([^{}]*)\}", lambda t: t.group(1) or t.group(2) or "", s)
    return s.replace("{", "").replace("}", "")


def strip_latex(text: str) -> str:
    """簡単な $…$ / $$…$$ / \\( … \\) を Unicode の素の式にする。値段の「$5」のような 1 つだけの $ は残す。"""
    if not text or ("$" not in text and "\\" not in text):
        return text
    text = re.sub(r"\$\$([^$]{1,200})\$\$", _latex_inner, text)
    text = re.sub(r"\$([^$\n]{1,120})\$", _latex_inner, text)
    text = re.sub(r"\\\((.{1,120}?)\\\)", _latex_inner, text)
    return text


def strip_latex_content(content: str) -> str:
    """LLM の返事全体に。JSON なら文字列の値だけを直す (JSON の \\ を壊さない)。読めない JSON は触らない。"""
    if not content or ("$" not in content and "\\" not in content):
        return content
    head = content.lstrip()[:3]
    if head[:1] in ("{", "[") or head == "```":
        try:
            data = json.loads(content)
        except ValueError:
            return content

        def walk(v):
            if isinstance(v, str):
                return strip_latex(v)
            if isinstance(v, list):
                return [walk(x) for x in v]
            if isinstance(v, dict):
                return {k: walk(x) for k, x in v.items()}
            return v
        return json.dumps(walk(data), ensure_ascii=False)
    return strip_latex(content)


# ---------------------------------------------------------------------------
# 返す形を整える (LLM が何を返しても約束の形にする)
# ---------------------------------------------------------------------------


def clean_text(v, limit: int) -> str:
    s = re.sub(r"\s+", " ", strip_latex(str(v or ""))).strip()
    s = s.replace("$", "")
    return s[:limit]


def clean_say(v, limit: int = 120) -> dict | None:
    if isinstance(v, str):
        v = {"text": v}
    if not isinstance(v, dict):
        return None
    text = clean_text(v.get("text"), limit)
    if not text:
        return None
    expr = v.get("expression") if v.get("expression") in EXPRESSIONS else "neutral"
    return {"text": text, "expression": expr}


def clean_marks(v, ink_rows: set | None) -> list:
    out = []
    for m in v if isinstance(v, list) else []:
        if not isinstance(m, dict):
            continue
        try:
            row = int(m.get("row"))
        except (TypeError, ValueError):
            continue
        if row < 1 or (ink_rows and row not in ink_rows):
            continue
        kind = m.get("kind") if m.get("kind") in MARK_KINDS else None
        if not kind:
            continue
        mark = {"row": row, "kind": kind, "text": clean_text(m.get("text"), 12)}
        try:
            x = m.get("x")
            if x is not None:
                mark["x"] = round(min(1.0, max(0.0, float(x))), 3)
        except (TypeError, ValueError):
            pass
        out.append(mark)
    # 1 行に 1 つ (間違いを優先)
    best: dict[int, dict] = {}
    rank = {"mistake": 0, "unclear": 1, "note": 2, "ok": 3}
    for m in out:
        if m["row"] not in best or rank[m["kind"]] < rank[best[m["row"]]["kind"]]:
            best[m["row"]] = m
    return [best[r] for r in sorted(best)]


# ---------------------------------------------------------------------------
# 声で言った数 (音声認識の文字) を値にする。合っているときだけ LLM を待たずに「正解」と言うため
# ---------------------------------------------------------------------------

_KANA_DIGITS = {"ぜろ": 0, "れい": 0, "ゼロ": 0, "いち": 1, "に": 2, "さん": 3, "よん": 4, "し": 4, "ご": 5, "ろく": 6,
                "なな": 7, "しち": 7, "はち": 8, "きゅう": 9, "く": 9, "じゅう": 10}
_KANJI_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_KANJI_UNITS = {"十": 10, "百": 100, "千": 1000}


def _kanji_number(s: str) -> int | None:
    if not s or any(c not in _KANJI_DIGITS and c not in _KANJI_UNITS for c in s):
        return None
    total, cur = 0, 0
    for c in s:
        if c in _KANJI_DIGITS:
            cur = cur * 10 + _KANJI_DIGITS[c] if cur else _KANJI_DIGITS[c]
        else:
            total += (cur or 1) * _KANJI_UNITS[c]
            cur = 0
    return total + cur


def normalize_spoken(text: str) -> str:
    s = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    s = re.sub(r"[\s、。,!?！？]+", "", s)
    s = re.sub(r"^(答えは|こたえは|答え|こたえ)", "", s)
    s = re.sub(r"(です|だよ|かな|だと思う|だ|になった|になる)$", "", s)
    s = re.sub(r"^(x|えっくす|エックス)(=|イコール|いこーる|は)", "", s)
    return s


def spoken_value(text: str) -> Fraction | None:
    """「マイナス 4」→ −4、「2 分の 1」→ 1/2、「3 てん 5」→ 3.5、「x = -4」→ −4。数 1 つでなければ None。"""
    s = normalize_spoken(text)
    s = s.replace("−", "-").replace("ー", "-").replace("―", "-")
    s = re.sub(r"^(マイナス|まいなす|ひく)", "-", s)
    s = re.sub(r"(てん|点)", ".", s)
    neg = s.startswith("-")
    s = s.lstrip("-+")

    def num(part: str) -> Fraction | None:
        if re.fullmatch(r"\d+(\.\d+)?", part):
            return Fraction(part)
        if part in _KANA_DIGITS:
            return Fraction(_KANA_DIGITS[part])
        k = _kanji_number(part)
        return Fraction(k) if k is not None else None

    m = re.fullmatch(r"(.+?)分の(.+)", s)  # 「2 分の 1」= 1/2
    if m:
        den, nume = num(m.group(1)), num(m.group(2))
        v = nume / den if den not in (None, 0) and nume is not None else None
    else:
        m = re.fullmatch(r"(\d+)/(\d+)", s)
        v = Fraction(int(m.group(1)), int(m.group(2))) if m and int(m.group(2)) else num(s)
    if v is None:
        return None
    return -v if neg else v


def answer_value(text: str) -> Fraction | None:
    """模範解答の「最終の答え」が数 1 つなら、その値 (「x = −4」「−4」「½」)。"""
    s = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    s = re.sub(r"\s+", "", s).replace("−", "-").replace("–", "-")
    s = re.sub(r"^[a-z]=", "", s)
    s = s.replace("½", "1/2").replace("⁄", "/").replace("÷", "/")
    m = re.fullmatch(r"(-?)(\d+(?:\.\d+)?)(?:/(\d+(?:\.\d+)?))?", s)
    if not m:
        return None
    v = Fraction(m.group(2)) / (Fraction(m.group(3)) if m.group(3) else 1) if m.group(3) != "0" else None
    return None if v is None else (-v if m.group(1) else v)


def quick_compare(spoken: str, answer: str) -> bool | None:
    """True = 同じ数、False = 違う数、None = 数として比べられない (LLM に任せる)。"""
    try:
        a, b = spoken_value(spoken), answer_value(answer)
    except (ValueError, ZeroDivisionError):
        return None
    if a is None or b is None:
        return None
    return a == b


def public_problem(p: dict | None) -> dict | None:
    return {k: p[k] for k in ("id", "subject", "text", "goal")} if p else None


# ---------------------------------------------------------------------------
# タブレット
# ---------------------------------------------------------------------------


class Tablet:
    def __init__(self, app, pc):
        self.app = app
        self.pc = pc  # cohabit_pc モジュール (python cohabit_pc.py では __main__)
        self.lock = threading.Lock()
        self.problem: dict | None = None
        self.last_check: dict | None = None
        self.last_result: dict | None = None
        self.presence = "tablet"
        self.client = {"lastSeen": None, "address": None, "userAgent": None}
        self.subscribers: list[queue.Queue] = []
        self.check_cache: OrderedDict = OrderedDict()
        self.answer_cache: OrderedDict = OrderedDict()
        self.learning_count_cache: tuple | None = None
        self.speech_cache: OrderedDict = OrderedDict()
        self.weak_lock = threading.Lock()
        self.weak_thread: threading.Thread | None = None

    # ---- 設定・場所 ----

    def cfg(self) -> dict:
        return self.app.config.get()

    def data_dir(self) -> str:
        return self.pc.DATA_DIR

    def path_of(self, name: str) -> str:
        return os.path.join(self.data_dir(), name)

    def vrm_path(self) -> str:
        p = (self.cfg().get("tablet") or {}).get("vrmPath") or DEFAULT_VRM
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(HERE, p))

    def token_query(self) -> str:
        token = self.cfg().get("token") or ""
        return "?" + urllib.parse.urlencode({"token": token}) if token else ""

    # ---- SSE ----

    def broadcast(self, event: str, data: dict):
        with self.lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait((event, data))
            except queue.Full:
                with self.lock:
                    if q in self.subscribers:
                        self.subscribers.remove(q)

    def set_presence(self, where: str):
        with self.lock:
            changed = self.presence != where
            self.presence = where
        if changed:
            self.broadcast("presence", {"where": where})
        return changed

    def say_payload(self, say: dict | None) -> dict | None:
        if not say:
            return None
        q = {"text": say["text"], "expression": say.get("expression") or "neutral"}
        token = self.cfg().get("token") or ""
        if token:
            q["token"] = token
        return {**say, "audio": "/tablet/api/speech?" + urllib.parse.urlencode(q)}

    def push_say(self, text: str, expression: str = "neutral", source: str = "quest"):
        say = self.say_payload(clean_say({"text": text, "expression": expression}, 200))
        if say:
            self.broadcast("say", {**say, "from": source})
        return say

    # ---- LLM ----

    def llm(self, route: str, task: str, images: list[str] | None = None, max_tokens: int = 700,
            temperature: float = 0.3, cache: bool = True, priority: str = "interactive") -> dict:
        """妹の口調で LLM に頼み、JSON (dict) を返す。行き先 (第一候補 → 予備)・1 つずつ・同じ要求をまとめるのは
        cohabit_pc の LLMRouter。どこにも届かなければ UpstreamError(503, kind="llm_down")。"""
        pc = self.pc
        cfg = self.cfg()
        content: list | str = task
        if images:
            content = [{"type": "text", "text": task}] + [{"type": "image_url", "image_url": {"url": u}} for u in images]
        body = {
            "messages": [{"role": "system", "content": sister_prompt(cfg)}, {"role": "user", "content": content}],
            "max_tokens": max_tokens, "temperature": temperature,
        }
        t0 = time.time()
        attempts = []
        parsed = None
        r = None
        for attempt in range(2):
            try:
                r = self.app.llm.complete(cfg, body, vision=bool(images) and attempt == 0, cache=cache,
                                          priority=priority, route=route)
            except pc.UpstreamError as e:
                if e.kind == "busy":
                    raise  # 後回し (飛ばした) — 失敗として記録しない
                self.log_request(route, False, None, t0, task, str(e), status=e.status, images=images if attempt == 0 else None)
                raise
            if r["status"] != 200:
                self.log_request(route, False, r, t0, task, r.get("error") or "", status=r["status"])
                raise pc.UpstreamError(502, r.get("error") or f"LLM answered {r['status']}")
            text = r["content"] or ""
            attempts.append(text)
            parsed = parse_json_loose(text)
            if isinstance(parsed, dict):
                break
            # 1 回だけ直させる (画像は付けない)
            body = {**body, "messages": [
                {"role": "system", "content": "Return only one valid JSON object. No prose, no code fences."},
                {"role": "user", "content": "次の返事を、同じ中身の正しい JSON オブジェクト 1 つに直して。JSON だけを返す。\n\n" + (text[:3000] or "(空)")
                 + "\n\n元の指示:\n" + task[-1500:]}]}
        ok = isinstance(parsed, dict)
        self.log_request(route, ok, r, t0, task, "\n---\n".join(attempts)[:1500], images=images)
        if not ok:
            raise pc.UpstreamError(502, "LLM did not return JSON")
        return parsed

    def log_request(self, route, ok, r, t0, prompt, reply, status=200, images=None):
        cfg = self.cfg()
        image_ids = []
        if images and cfg["privacy"].get("previewImages"):
            image_ids = [i for i in (self.app.state.add_image(u, route) for u in images) if i]
        entry = {"route": route, "ok": ok, "status": status, "ms": round((time.time() - t0) * 1000),
                 "prompt": prompt[-1200:], "reply": reply, "images": image_ids}
        if r:
            entry.update({"model": r.get("model"), "where": r.get("where"), "backend": r.get("backend"),
                          "label": r.get("label"), "cached": bool(r.get("cached")), "tried": r.get("tried") or []})
        else:
            entry["error"] = reply
        self.app.state.add_request(entry)

    # ---- 学習記録 ----

    def keep_learning(self) -> bool:
        return bool((self.cfg().get("tablet") or {}).get("keepLearning", True))

    def read_learning(self, limit: int = 200) -> list:
        path = self.path_of(LEARNING_FILE)
        if not os.path.exists(path):
            return []
        out = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out[-limit:]

    def record(self, p: dict, result: str):
        """問題 1 つの終わり (解けた / 次へ進んだ) を記録する。"""
        if p.get("recorded"):
            return
        p["recorded"] = True
        entry = {
            "t": self.pc.now_iso(), "problemId": p["id"], "subject": p["subject"], "problem": p["text"], "goal": p["goal"],
            "from": p.get("from"), "level": p.get("level"), "result": result,
            "mistakes": p["mistakes"][:8], "hints": dict(p["hints"]), "hintsUsed": sum(p["hints"].values()) > 0,
            "checks": p["checks"], "seconds": round(time.time() - p["startedAt"]),
        }
        with self.lock:
            self.last_result = entry
        if not self.keep_learning():
            return
        os.makedirs(self.data_dir(), exist_ok=True)
        with self.lock:
            with open(self.path_of(LEARNING_FILE), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.refresh_weak_later()

    def learning_count(self) -> int:
        """学習記録の件数 (画面は毎秒読むので、ファイルが変わったときだけ数え直す)。"""
        path = self.path_of(LEARNING_FILE)
        try:
            st = os.stat(path)
        except OSError:
            return 0
        sig = (st.st_mtime_ns, st.st_size)
        cached = self.learning_count_cache
        if cached and cached[0] == sig:
            return cached[1]
        n = len(self.read_learning(10 ** 9))
        self.learning_count_cache = (sig, n)
        return n

    def weak(self) -> dict | None:
        path = self.path_of(WEAK_FILE)
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            return None

    def refresh_weak(self, priority: str = "background") -> dict | None:
        """学習記録から苦手の要約を LLM に作らせ、data/weak_points.json に置く。"""
        with self.weak_lock:
            records = self.read_learning(40)
            if not records:
                return None
            current = self.weak()
            if current and current.get("basedOn") == len(self.read_learning(100000)):
                return current
            lines = []
            for r in reversed(records):
                lines.append(f"- [{r.get('subject')}] {clean_text(r.get('problem'), 80)} / 結果: {r.get('result')} / "
                             f"間違い: {'、'.join(r.get('mistakes') or []) or 'なし'} / ヒント {sum((r.get('hints') or {}).values())} 回 / {r.get('seconds')} 秒")
            task = ("# 仕事: 学習記録から苦手をまとめる\n記録 (新しい順):\n" + "\n".join(lines) +
                    "\n\n記録に出てくることだけから、相手の苦手を多くても 5 つにまとめる。記録に無いことは書かない。\n"
                    'JSON だけを返す: {"items": [{"subject": "数学", "topic": "一次方程式の移項", "detail": "符号を変え忘れる (12 字くらい)"}], '
                    '"summary": "全体を 60 字以内で"}')
            data = self.llm("tablet.weak", task, max_tokens=350, temperature=0.2, priority=priority)
            items = []
            for it in data.get("items") or []:
                if isinstance(it, dict) and it.get("topic"):
                    items.append({"subject": clean_text(it.get("subject"), 20), "topic": clean_text(it.get("topic"), 40),
                                  "detail": clean_text(it.get("detail"), 60)})
            weak = {"t": self.pc.now_iso(), "basedOn": len(self.read_learning(100000)), "items": items[:5],
                    "summary": clean_text(data.get("summary"), 120)}
            os.makedirs(self.data_dir(), exist_ok=True)
            tmp = self.path_of(WEAK_FILE + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(weak, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path_of(WEAK_FILE))
            return weak

    def refresh_weak_later(self):
        def run():
            for _ in range(4):  # LLM が他の仕事中 (busy) なら、少し待ってやり直す (後回しの仕事)
                try:
                    self.refresh_weak()
                    return
                except self.pc.UpstreamError as e:
                    if e.kind != "busy":
                        self.app.state.add_request({"route": "tablet.weak", "ok": False, "error": str(e)})
                        return
                except Exception as e:  # 要約が作れなくても学習は止めない
                    self.app.state.add_request({"route": "tablet.weak", "ok": False, "error": f"{type(e).__name__}: {e}"})
                    return
                if self.app.stop.wait(WEAK_RETRY_SECONDS):
                    return
        t = threading.Thread(target=run, daemon=True)
        self.weak_thread = t
        t.start()

    def delete_learning(self) -> list:
        removed = []
        for name in (LEARNING_FILE, WEAK_FILE):
            p = self.path_of(name)
            if os.path.exists(p):
                os.remove(p)
                removed.append(name)
        with self.lock:
            self.last_result = None
            self.last_check = None
            self.check_cache.clear()
            self.answer_cache.clear()
            self.learning_count_cache = None
        return removed

    # ---- 問題 ----

    def make_problem(self, body: dict) -> dict:
        source = body.get("from") or "topic"
        level = body.get("level") if body.get("level") in LEVELS else None
        images = []
        with self.lock:
            prev = self.problem
            last = self.last_result
        if prev and not prev.get("recorded") and (prev["checks"] or sum(prev["hints"].values())):
            self.record(prev, "moved_on")  # 途中でやめた問題も、手を付けていれば記録する
            with self.lock:
                last = self.last_result
        if source == "topic":
            topic = clean_text(body.get("topic"), 200)
            if not topic:
                raise self.pc.UpstreamError(400, "topic is empty")
            what = f"話題: 「{topic}」。この話題を練習できる問題を作る。"
        elif source == "photo":
            image = body.get("image") or ""
            if not str(image).startswith("data:image/"):
                raise self.pc.UpstreamError(400, "image must be a data:image/... URL")
            self.require_images()
            images = [image]
            topic = clean_text(body.get("topic"), 200)
            what = ("画像は教科書や問題集のページの写真。写っている問題から 1 問選び、問題文をそのまま書き写して出す"
                    "(図が要るものは避ける)。読み取れなければ、写っている内容の似た問題を作る。" + (f"相手の希望: 「{topic}」。" if topic else ""))
        elif source == "weak":
            # 話題を勝手に決めない: 学習記録が無ければ 409 (何を勉強するか聞く)
            records = self.read_learning(12)
            if not records:
                raise self.pc.UpstreamError(409, NO_RECORDS_MESSAGE, "no_records")
            weak = self.weak()
            if not (weak and weak.get("items")):
                try:
                    weak = self.refresh_weak(priority="interactive")  # 人が「苦手から」を押して待っている
                except self.pc.UpstreamError:
                    weak = None
            if weak and weak.get("items"):
                lst = "\n".join(f"- [{w['subject']}] {w['topic']}: {w['detail']}" for w in weak["items"])
                what = f"相手の苦手 (学習記録から):\n{lst}\nこの中から 1 つ選び、それを練習できる問題を作る。"
            else:
                # 苦手の要約がまだ作れない: 記録そのものから (間違えた・解けなかったものを優先)
                picked = sorted(records, key=lambda r: (r.get("result") == "solved" and not r.get("mistakes"),))[:6]
                lst = "\n".join(f"- [{r.get('subject')}] {clean_text(r.get('problem'), 80)} / 結果: {r.get('result')} / "
                                f"間違い: {'、'.join(r.get('mistakes') or []) or 'なし'}" for r in picked)
                what = f"相手の学習記録 (間違えた・解けなかったものが先):\n{lst}\nこの中で一番つまずいていそうなところを練習できる問題を作る。"
        elif source == "next":
            base = last or (prev and {"problem": prev["text"], "subject": prev["subject"], "result": "unknown",
                                      "mistakes": prev["mistakes"], "hints": prev["hints"]})
            if not base:
                raise self.pc.UpstreamError(409, "no previous problem")
            if not level:
                used = base.get("hints") or {}
                if base.get("result") == "solved" and not base.get("mistakes") and not sum(used.values()):
                    level = "harder"
                elif used.get("answer") or base.get("result") != "solved":
                    level = "easier"
                else:
                    level = "same"
            what = (f"前の問題: [{base.get('subject')}] {base.get('problem')}\n結果: {base.get('result')} / 間違い: "
                    f"{'、'.join(base.get('mistakes') or []) or 'なし'}\nこれと同じ考え方を使う、次の問題を作る (数字や設定は変える)。")
        elif source == "text":
            text = clean_text(body.get("text"), 600)
            if not text:
                raise self.pc.UpstreamError(400, "text is empty")
            what = f"この問題をそのまま出す (text は一字も変えない):\n{text}"
        else:
            raise self.pc.UpstreamError(400, f"unknown from: {source}")
        level_ja = {"easier": "前より少しやさしく。", "same": "前と同じくらいの難しさで。", "harder": "前より少し難しく。"}.get(level or "", "")
        task = (f"# 仕事: 問題を 1 問出す\n{what}\n対象は中学生以上。{level_ja}\n"
                "ノートに手で書いて解ける問題にする (数行〜十数行で解ける)。選択肢だけの問題・図が要る問題は避ける。\n"
                "solution は途中の式や考えを 1 行ずつ書いた模範解答 (相手には見せない)。自分で検算して確かめてから書く。\n"
                "say は問題を出すときの妹のひとこと (答えやヒントは言わない)。\n"
                'JSON だけを返す: {"subject": "数学", "text": "問題文 (表示用の素の文字。数式は Unicode)", '
                '"goal": "何ができればよいか (1 文)", "solution": "模範解答", "answer": "最終の答え", '
                '"say": {"text": "…", "expression": "happy"}}')
        data = self.llm("tablet.problem", task, images=images, max_tokens=700, temperature=0.6, cache=False)
        text = clean_text(body.get("text"), 600) if source == "text" else clean_text(data.get("text"), 600)
        if not text:
            raise self.pc.UpstreamError(502, "LLM did not write a problem")
        p = {
            "id": uuid.uuid4().hex[:10], "subject": clean_text(data.get("subject"), 20) or "勉強", "text": text,
            "goal": clean_text(data.get("goal"), 120), "solution": str(data.get("solution") or "")[:3000],
            "answer": clean_text(data.get("answer"), 300), "from": source, "level": level,
            "startedAt": time.time(), "lastProgress": time.time(), "checks": 0, "mistakes": [],
            "hints": {"hint": 0, "explain": 0, "answer": 0}, "marks": [], "recorded": False,
        }
        say = self.say_payload(clean_say(data.get("say")) or {"text": "これ、やってみて。", "expression": "happy"})
        with self.lock:
            self.problem = p
            self.last_check = None
        out = {"problem": public_problem(p), "say": say}
        self.broadcast("problem", {**out, "from": source})
        return out

    def require_images(self):
        if not self.cfg()["privacy"].get("allowImages"):
            raise self.pc.UpstreamError(403, "images are switched off on the PC (privacy.allowImages)")

    def current(self, problem_id) -> dict:
        with self.lock:
            p = self.problem
        if not p:
            raise self.pc.UpstreamError(409, "no problem yet")
        if problem_id and problem_id != p["id"]:
            raise self.pc.UpstreamError(409, "the problem has changed; read /tablet/api/session")
        return p

    # ---- 赤ペン ----

    def check(self, body: dict) -> dict:
        p = self.current(body.get("problemId"))
        trigger = body.get("trigger") if body.get("trigger") in ("pause", "request", "done") else "request"
        page = str(body.get("page") or "")
        if not page.startswith("data:image/"):
            raise self.pc.UpstreamError(400, "page must be a data:image/... URL")
        self.require_images()
        if body.get("source") == "quest" or body.get("from") == "quest":
            return self.check_desk_photo(p, trigger, page)
        rows = [r for r in body.get("rows") or [] if isinstance(r, dict)]
        ink = {int(r["row"]) for r in rows if r.get("hasInk") and isinstance(r.get("row"), (int, float))}
        changed = [int(r) for r in body.get("changedRows") or [] if isinstance(r, (int, float))]
        key = hashlib.sha256(f"{p['id']}|{trigger}|".encode() + page.encode()).hexdigest()
        with self.lock:
            cached = self.check_cache.get(key)
            if changed:
                p["lastProgress"] = time.time()
        if cached:
            return {**cached, "cached": True}
        height = None
        if rows:
            try:
                height = round(float(rows[0]["bottom"]) - float(rows[0]["top"]))
            except (KeyError, TypeError, ValueError):
                height = None
        trig = {
            "pause": ("相手は書くのが少し止まっただけ。集中を邪魔しないこと。間違いが無ければ marks は [] で say は null。"
                      "はっきりした間違いを見つけたときだけ、その行に mistake と短い say。"),
            "request": "相手が「見て」と頼んだ。見たことを短く言う (間違いがあればどの行か。無ければ合っていると)。",
            "done": ("相手が「できた」と言った。最終の答えまで正しいか判定する。正しければほめる。"
                     "違えば、どの行がおかしいかだけ言う (答えは言わない)。"),
        }[trigger]
        task = (
            "# 仕事: ノートを見て赤ペンを入れる\n"
            f"問題: {p['text']}\nできればよいこと: {p['goal']}\n"
            f"模範解答 (相手には見せない。答えをそのまま言わない):\n{p['solution']}\n最終の答え: {p['answer']}\n\n"
            "画像は相手のノート。白い紙に黒いインク。左の余白の薄い数字が行番号"
            + (f" (1 行の高さ {height} px)" if height else "") + "。\n"
            f"書いてある行: {', '.join(map(str, sorted(ink))) or '(わからない。画像で判断)'} / 今回書き足した行: {', '.join(map(str, changed)) or 'なし'}\n"
            f"きっかけ: {trig}\n\n"
            "やること:\n"
            "1. 書いてある行をそれぞれ読む (read)。読めない字は無理に読まない。\n"
            "2. 各行が正しいかを、自分で計算し直して確かめる。模範解答と違うやり方・途中の書き方の違い・まだ途中なだけ、は間違いにしない。\n"
            "3. marks: 行ごとの印。kind は ok (正しい) / mistake (間違い) / unclear (字が読めない) / note (ほめる・補足)。"
            "text は赤ペンの一言 (日本語 12 字以内。答えそのものは書かない。例「符号、逆」「÷2 忘れ」「いいね」)。"
            "x はその行の問題のある場所 (左端 0〜右端 1)。わからなければ null。印の要らない行は入れない。\n"
            "4. solved: 最終の答えまで正しく書けていれば true。stuck: 手が止まって困っていそうなら true。\n"
            "5. mistakeTypes: 間違いの種類を短い言葉で (例「移項の符号」)。無ければ []。\n"
            "6. say: 妹のひとこと (タメ口・40 字くらいまで) か null。\n"
            'JSON だけを返す: {"read": [{"row": 1, "text": "読んだ式"}], "marks": [{"row": 2, "kind": "mistake", "text": "符号、逆", "x": 0.6}], '
            '"solved": false, "stuck": false, "mistakeTypes": [], "say": {"text": "…", "expression": "worried"}}'
        )
        data = self.llm("tablet.check", task, images=[page], max_tokens=600, temperature=0.1)
        marks = clean_marks(data.get("marks"), ink or None)
        solved = data.get("solved") is True
        say = clean_say(data.get("say"), 80)
        if trigger == "pause":
            marks = [m for m in marks if m["kind"] == "mistake"]
            if not marks:
                say = None
        if solved and not say:
            say = {"text": "できたじゃん！", "expression": "happy"}
        stuck = bool(data.get("stuck") is True or (not solved and time.time() - p["lastProgress"] > 120))
        result = {"marks": marks, "solved": solved, "say": self.say_payload(say), "stuck": stuck}
        mistakes = [clean_text(m, 20) for m in data.get("mistakeTypes") or [] if isinstance(m, str) and m.strip()]
        mistakes += [m["text"] for m in marks if m["kind"] == "mistake" and m["text"] and not mistakes]
        with self.lock:
            p["checks"] += 1
            p["marks"] = marks
            for m in mistakes:
                if m and m not in p["mistakes"]:
                    p["mistakes"].append(m)
            self.check_cache[key] = result
            while len(self.check_cache) > 64:
                self.check_cache.popitem(last=False)
            self.last_check = {"t": self.pc.now_iso(), "trigger": trigger, "problemId": p["id"], **result,
                               "read": [r for r in data.get("read") or [] if isinstance(r, dict)][:30]}
        if solved:
            self.record(p, "solved_with_answer" if p["hints"]["answer"] else "solved")
        return result

    def check_desk_photo(self, p: dict, trigger: str, page: str) -> dict:
        """Quest のカメラで撮った机の写真を見る。行番号は無いので marks は返さない。
        言うのは、間違いを見つけたとき・「見て」(request)・「できた」(done)・解けたときだけ。"""
        key = hashlib.sha256(f"quest|{p['id']}|{trigger}|".encode() + page.encode()).hexdigest()
        with self.lock:
            cached = self.check_cache.get(key)
        if cached:
            return {**cached, "cached": True}
        trig = {
            "pause": "相手は書くのが少し止まっただけ。集中を邪魔しないこと。はっきりした間違いが無ければ say は null。",
            "request": "相手が「見て」と頼んだ。見たことを短く言う (間違いがあればどこか。無ければ合っていると)。",
            "done": "相手が「できた」と言った。最終の答えまで正しいか判定する。正しければほめる。違えばどこがおかしいかだけ言う。",
        }[trigger]
        task = (
            "# 仕事: 机の上のノートを見る\n"
            f"問題: {p['text']}\nできればよいこと: {p['goal']}\n"
            f"模範解答 (相手には見せない。答えをそのまま言わない):\n{p['solution']}\n最終の答え: {p['answer']}\n\n"
            "画像は、相手の頭につけたカメラ (Quest) から見た机の写真。ノートや紙は斜め・小さく・暗く写っていることがある。\n"
            f"きっかけ: {trig}\n\n"
            "やること:\n"
            "1. この問題の途中式や答えが書いてあれば読む (read)。読めない字は無理に読まない。ノートが写っていない・読めないなら readable は false。\n"
            "2. 自分で計算し直して、書いてあることが正しいか確かめる。模範解答と違うやり方・途中なだけ、は間違いにしない。\n"
            "3. mistake: はっきりした間違いがあれば true。where: その場所を短く (例「2 行目の移項」)。\n"
            "4. solved: 最終の答えまで正しく書けていれば true。stuck: 手が止まって困っていそうなら true。\n"
            "5. mistakeTypes: 間違いの種類を短い言葉で (例「移項の符号」)。無ければ []。\n"
            "6. say: 妹のひとこと (タメ口・40 字くらいまで。答えそのものは言わない) か null。\n"
            'JSON だけを返す: {"readable": true, "read": "読めた式 (行ごとに / で区切る)", "mistake": false, "where": null, '
            '"solved": false, "stuck": false, "mistakeTypes": [], "say": {"text": "…", "expression": "worried"}}'
        )
        # pause (手が止まっただけ) は後回しにできる: LLM が他の仕事中なら待たずに 503 busy (Quest は「飛ばした」と扱う)
        data = self.llm("quest.check", task, images=[page], max_tokens=500, temperature=0.1,
                        priority="background" if trigger == "pause" else "interactive")
        readable = data.get("readable") is not False
        solved = readable and data.get("solved") is True
        mistake = readable and not solved and data.get("mistake") is True
        say = clean_say(data.get("say"), 80)
        if solved:
            say = say or {"text": "できたじゃん！", "expression": "happy"}
        elif mistake:
            where = clean_text(data.get("where"), 20)
            say = say or {"text": f"{where}、もう一回見て。" if where else "どこか違うかも。もう一回見て。", "expression": "worried"}
        elif trigger == "pause":
            say = None  # 間違いが無ければ黙る (集中を邪魔しない)
        elif not readable:
            say = {"text": "ごめん、ノートがよく見えない。もう少し近くで見せて。", "expression": "worried"}
        else:
            say = say or {"text": "今のところ合ってると思う。そのまま続けて。", "expression": "happy"}
        stuck = bool(data.get("stuck") is True or (not solved and time.time() - p["lastProgress"] > 120))
        result = {"marks": [], "solved": solved, "say": self.say_payload(say), "stuck": stuck,
                  "mistake": mistake, "readable": readable}
        mistakes = [clean_text(m, 20) for m in data.get("mistakeTypes") or [] if isinstance(m, str) and m.strip()] if mistake else []
        with self.lock:
            p["checks"] += 1
            for m in mistakes:
                if m and m not in p["mistakes"]:
                    p["mistakes"].append(m)
            self.check_cache[key] = result
            while len(self.check_cache) > 64:
                self.check_cache.popitem(last=False)
            read = data.get("read")
            self.last_check = {"t": self.pc.now_iso(), "trigger": trigger, "problemId": p["id"], "source": "quest", **result,
                               "read": [{"row": 0, "text": clean_text(read, 200)}] if isinstance(read, str) and read.strip() else []}
        if solved:
            self.record(p, "solved_with_answer" if p["hints"]["answer"] else "solved")
        return result

    # ---- 声で答える (Quest) ----

    def answer(self, body: dict) -> dict:
        """声で言った答え (音声認識の文字) を、PC にだけある模範解答と比べる。
        → { correct: true / false / null (聞き取れない・答えになっていない), solved, say }"""
        p = self.current(body.get("problemId"))
        spoken = clean_text(body.get("answer"), 200)
        if not spoken:
            raise self.pc.UpstreamError(400, "answer is empty")
        source = clean_text(body.get("from") or "quest", 20)
        key = f"{p['id']}|{normalize_spoken(spoken)}"
        with self.lock:
            cached = self.answer_cache.get(key)
        if cached:
            return {**cached, "cached": True}
        quick = quick_compare(spoken, p["answer"])
        heard, mistake_type, judge = None, None, "llm"
        say = None
        if quick is True:
            verdict, judge = "correct", "local"  # 数がそのまま合っている: LLM に聞かない (速い・LLM が落ちていても動く)
        else:
            task = (
                "# 仕事: 声で言った答えが合っているか判定する\n"
                f"問題: {p['text']}\n模範解答 (相手には見せない):\n{p['solution']}\n最終の答え: {p['answer']}\n\n"
                f"相手が声で言った答え (音声認識の文字。聞き間違い・ひらがな・カタカナ・漢数字がある): 「{spoken}」\n\n"
                "読み方の例: 「マイナス 4」「まいなすよん」= −4、「2 分の 1」= ½、「ルート 2」= √2、「x イコール 3」= x = 3、"
                "「さんてんご」= 3.5、「にじょう」= ²。\n"
                "値が同じなら形が違っても正解 (0.5 と ½、x = 4 と 4)。答えが複数あるなら全部そろって正解。\n"
                "答えになっていない・聞き取れない・問題と関係ない言葉なら unclear。\n"
                "say: correct ならほめる。wrong なら答えは言わずに、もう一度考えるよう短く促す (どこを見直すか)。unclear なら聞き返す。\n"
                'JSON だけを返す: {"verdict": "correct" | "wrong" | "unclear", "heard": "数式としての答え", '
                '"mistakeType": "間違いの種類 (短く) か null", "say": {"text": "…", "expression": "happy"}}'
            )
            try:
                data = self.llm("quest.answer", task, max_tokens=250, temperature=0.1)
            except self.pc.UpstreamError:
                if quick is False:
                    data = {"verdict": "wrong"}  # 数として違う: LLM が使えなくても「違う」とは言える
                    judge = "local"
                else:
                    raise
            verdict = data.get("verdict") if data.get("verdict") in ("correct", "wrong", "unclear") else "unclear"
            heard = clean_text(data.get("heard"), 60) or None
            mistake_type = clean_text(data.get("mistakeType"), 20) if isinstance(data.get("mistakeType"), str) else None
            say = clean_say(data.get("say"), 80)
        correct = {"correct": True, "wrong": False}.get(verdict)
        if say and correct is False and say["expression"] == "happy":
            say["expression"] = "worried"  # 違うのに笑顔にしない
        say = say or {
            "correct": {"text": "正解！やったじゃん。", "expression": "happy"},
            "wrong": {"text": "うーん、違うかも。もう一回見直してみて。", "expression": "worried"},
        }.get(verdict, {"text": "ごめん、よく聞こえなかった。もう一回言って。", "expression": "thinking"})
        result = {"correct": correct, "solved": correct is True, "say": self.say_payload(say)}
        if heard:
            result["heard"] = heard
        with self.lock:
            if correct is not None:
                p["checks"] += 1
            p["lastProgress"] = time.time()
            if correct is False and mistake_type and mistake_type not in p["mistakes"]:
                p["mistakes"].append(mistake_type)
            self.answer_cache[key] = result
            while len(self.answer_cache) > 32:
                self.answer_cache.popitem(last=False)
            self.last_check = {"t": self.pc.now_iso(), "trigger": "answer", "problemId": p["id"], "source": source,
                               "marks": [], "solved": correct is True, "say": result["say"], "stuck": False,
                               "spoken": spoken, "correct": correct, "judge": judge}
        if correct is True:
            self.record(p, "solved_with_answer" if p["hints"]["answer"] else "solved")
        return result

    # ---- 質問 ----

    def ask(self, body: dict) -> dict:
        level = body.get("level") if body.get("level") in ("hint", "explain", "answer") else "hint"
        with self.lock:
            p = self.problem
        if body.get("problemId"):
            p = self.current(body.get("problemId"))
        question = clean_text(body.get("question"), 400)
        page = str(body.get("page") or "")
        images = []
        if page.startswith("data:image/"):
            self.require_images()
            images = [page]
        if not p and not question:
            raise self.pc.UpstreamError(409, "no problem and no question")
        rule = {
            "hint": "次の 1 歩だけを言う。答えや途中の結果の数字は言わない。40 字くらいまで。",
            "explain": "考え方を説明する (何に注目して、どの順で何をするか)。最終の答えは言わない。150 字くらいまで。",
            "answer": "相手が答えを頼んだ。答えまで教える。大事な途中の式も短く入れる。200 字くらいまで。",
        }[level]
        ctx = ""
        if p:
            ctx = (f"問題: {p['text']}\nできればよいこと: {p['goal']}\n模範解答 (相手には見せない):\n{p['solution']}\n"
                   f"最終の答え: {p['answer']}\nこれまでの間違い: {'、'.join(p['mistakes']) or 'なし'}\n")
        task = (f"# 仕事: 相手を助ける ({level})\n{ctx}"
                + (f"相手の質問: 「{question}」\n" if question else "相手が「{0}」ボタンを押した。\n".format({"hint": "ヒント", "explain": "わからない", "answer": "答え"}[level]))
                + ("画像は相手のノート (左の余白の数字が行番号)。相手が今どこまで書いたかに合わせる。\n" if images else "")
                + f"言うこと: {rule}\n"
                + ("marks: ノートに赤ペンで書き足すものがあれば (row, kind: mistake/note, text 12 字以内, x 0〜1 か null)。無ければ []。\n" if images else "")
                + 'JSON だけを返す: {"say": {"text": "…", "expression": "thinking"}' + (', "marks": []}' if images else "}"))
        # もう一度押したら別の言い方が欲しいはず: 同じ要求をまとめない
        data = self.llm("tablet.ask", task, images=images, max_tokens={"hint": 200, "explain": 400, "answer": 600}[level],
                        temperature=0.3, cache=False)
        say = clean_say(data.get("say") or data.get("text"), {"hint": 100, "explain": 300, "answer": 400}[level])
        if not say:
            raise self.pc.UpstreamError(502, "LLM did not say anything")
        if p:
            with self.lock:
                p["hints"][level] += 1
                p["lastProgress"] = time.time()
        out = {"say": self.say_payload(say)}
        if images:
            out["marks"] = clean_marks(data.get("marks"), None)
        return out

    # ---- 声 ----

    def speech(self, text: str) -> bytes | None:
        pc = self.pc
        cfg = self.cfg()
        tts = cfg["tts"]
        key = f"{tts.get('engine')}|{tts.get('speaker')}|{text}"
        with self.lock:
            if key in self.speech_cache:
                self.speech_cache.move_to_end(key)
                return self.speech_cache[key]
        try:
            if tts["engine"] == "voicevox":
                wav = pc.speak_voicevox(cfg, text)
            else:
                status, _, wav = pc.http("POST", tts["baseUrl"].rstrip("/") + "/audio/speech",
                                         json.dumps({"model": tts["model"], "voice": tts["voice"], "input": text, "response_format": "wav"}).encode(),
                                         {"Content-Type": "application/json", **pc.bearer(pc.key_for(tts, cfg))}, timeout=60)
                if status != 200:
                    return None
        except pc.UpstreamError:
            return None
        with self.lock:
            self.speech_cache[key] = wav
            while len(self.speech_cache) > 24:
                self.speech_cache.popitem(last=False)
        return wav

    # ---- 画面・session ----

    def session(self) -> dict:
        cfg = self.cfg()
        t = cfg.get("tablet") or {}
        inf = self.pc.inference_summary(cfg)["parts"]
        with self.lock:
            p, presence, last = self.problem, self.presence, self.last_check
        return {
            "sister": {"name": t.get("sisterName") or "妹", "vrmUrl": "/tablet/vrm" + self.token_query()},
            "student": {"name": t.get("studentName") or ""},
            "problem": public_problem(p), "presence": presence,
            "backend": {k: f"{inf[k]['label']} ({self.pc.WHERE_JA.get(inf[k]['where'], inf[k]['where'])})" for k in ("chat", "vision")},
            "marks": (p or {}).get("marks") or [],
            "solved": bool(p and p.get("recorded") and last and last.get("solved")),
            "weak": self.weak(),
        }

    def dashboard_state(self, port: int) -> dict:
        with self.lock:
            p = self.problem
            client = dict(self.client)
            out = {
                "presence": self.presence, "sse": len(self.subscribers),
                "problem": {**public_problem(p), "solution": p["solution"], "answer": p["answer"], "checks": p["checks"],
                            "hints": p["hints"], "mistakes": p["mistakes"]} if p else None,
                "lastCheck": self.last_check, "lastResult": self.last_result,
            }
        client["secondsSinceSeen"] = round(time.time() - client["lastSeen"], 1) if client["lastSeen"] else None
        tq = self.token_query()
        out.update({
            "client": client, "weak": self.weak(), "learningCount": self.learning_count(),
            "tabletDir": os.path.isdir(TABLET_DIR), "vrm": os.path.isfile(self.vrm_path()),
            "urls": [f"http://{ip}:{port}/tablet/{tq}" for ip in self.pc.lan_addresses()],
        })
        return out

    # -----------------------------------------------------------------------
    # HTTP (cohabit_pc の Handler から呼ばれる)
    # -----------------------------------------------------------------------

    def allowed(self, h, query: dict) -> bool:
        token = self.cfg().get("token") or ""
        if token and not h.is_local():
            given = h.headers.get("X-Cohabit-Token") or (query.get("token") or [""])[0]
            if not hmac.compare_digest(str(given).encode(), token.encode()):
                h.error_json(401, "missing or wrong token (X-Cohabit-Token or ?token=)")
                return False
        with self.lock:
            self.client.update({"lastSeen": time.time(), "address": h.client_address[0],
                                "userAgent": (h.headers.get("User-Agent") or "")[:160]})
        return True

    def handle(self, h, method: str):
        path, _, query = h.path.partition("?")
        q = urllib.parse.parse_qs(query)
        t0 = time.time()
        ok = False
        try:
            if path == "/tablet":
                h.send_response(301)
                h.send_header("Location", "/tablet/" + (("?" + query) if query else ""))
                h.send_header("Content-Length", "0")
                h.end_headers()
                return
            if path.startswith("/tablet/api/") or path == "/tablet/vrm":
                if not self.allowed(h, q):
                    return
                ok = self.route(h, method, path, q)
                return
            if method not in ("GET", "HEAD"):
                return h.error_json(405, "static files are GET only")
            ok = self.static(h, path[len("/tablet/"):])
        except self.pc.UpstreamError as e:
            if e.kind == "busy":
                # 後回しにした (飛ばした)。失敗ではない・言うことも無い
                ok = True
                self.app.state.add_request({"route": path, "ok": True, "skipped": True,
                                            "reply": "飛ばした (LLM が人の待っている仕事をしている)"})
                h.json(503, {"error": {"message": str(e), "type": "cohabit_pc", "code": "busy"}})
                return
            self.app.state.add_request({"route": path, "ok": False, "error": str(e)})
            say = FALLBACK_SAY.get(e.status) if path in LLM_PATHS else None
            if e.kind == "no_records":
                say = {"text": "まだ記録が無いよ。何を勉強するか教えて。", "expression": "thinking"}
            if say:
                # LLM が使えない: エラーと一緒に、妹が言えるひとことを返す (Quest・タブレットはそれを言えばよい)
                h.json(e.status, {"error": {"message": str(e), "type": "cohabit_pc", "code": e.kind or "llm_error"},
                                  "say": self.say_payload(say)})
            else:
                h.error_json(e.status, str(e))
        except Exception as e:  # 1 つの要求で PC アプリを落とさない
            self.app.state.add_request({"route": path, "ok": False, "error": f"{type(e).__name__}: {e}"})
            h.error_json(500, f"{type(e).__name__}: {e}")
        finally:
            if path.startswith("/tablet/api/") and path != "/tablet/api/events":
                self.app.metrics.record(path, (time.time() - t0) * 1000, ok)

    def route(self, h, method: str, path: str, q: dict) -> bool:
        name = path[len("/tablet/api/"):] if path.startswith("/tablet/api/") else "vrm"
        if method in ("GET", "HEAD"):
            if name == "vrm":
                return self.send_file(h, self.vrm_path(), "model/gltf-binary", cache=True)
            if name == "session":
                h.json(200, self.session())
                return True
            if name == "speech":
                text = clean_text((q.get("text") or [""])[0], 300)
                if not text:
                    h.error_json(400, "no text")
                    return False
                wav = self.speech(text)
                if wav is None:
                    h.send_response(204)
                    h.send_header("Content-Length", "0")
                    h.end_headers()
                    return True
                h.send(200, wav, "audio/wav", {"Cache-Control": "max-age=600"})
                return True
            if name == "events":
                self.events(h)
                return True
            h.error_json(404 if name not in ("problem", "check", "ask", "answer", "presence", "say") else 405, f"no GET {path}")
            return False
        body = h.body_json()
        if not isinstance(body, dict):
            raise self.pc.UpstreamError(400, "body must be a JSON object")
        if name == "problem":
            h.json(200, self.make_problem(body))
            return True
        if name == "check":
            h.json(200, self.check(body))
            return True
        if name == "ask":
            h.json(200, self.ask(body))
            return True
        if name == "answer":
            h.json(200, self.answer(body))
            return True
        if name == "presence":
            if "wearing" in body:
                where = "room" if body.get("wearing") else "tablet"
            else:
                where = body.get("where")
            if where not in ("room", "tablet"):
                raise self.pc.UpstreamError(400, 'where must be "room" or "tablet" (or wearing: true/false)')
            changed = self.set_presence(where)
            h.json(200, {"ok": True, "presence": where, "changed": changed})
            return True
        if name == "say":
            say = self.push_say(str(body.get("text") or ""), body.get("expression") or "neutral", body.get("from") or "quest")
            if not say:
                raise self.pc.UpstreamError(400, "no text")
            h.json(200, {"ok": True, "say": say})
            return True
        h.error_json(404, f"no POST {path}")
        return False

    def events(self, h):
        q: queue.Queue = queue.Queue(maxsize=200)
        with self.lock:
            self.subscribers.append(q)
            presence = self.presence
        try:
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream; charset=utf-8")
            h.send_header("Cache-Control", "no-cache")
            h.send_header("Connection", "close")
            h.send_header("X-Accel-Buffering", "no")
            h.end_headers()
            h.close_connection = True
            h.wfile.write(b"retry: 3000\n\n")
            h.wfile.write(self.sse("presence", {"where": presence}))
            h.wfile.flush()
            keepalive = float((self.cfg().get("tablet") or {}).get("keepAliveSeconds") or 15)
            while not self.app.stop.is_set():
                try:
                    event, data = q.get(timeout=keepalive)
                    chunk = self.sse(event, data)
                except queue.Empty:
                    chunk = b": keep-alive\n\n"
                h.wfile.write(chunk)
                h.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            with self.lock:
                if q in self.subscribers:
                    self.subscribers.remove(q)

    @staticmethod
    def sse(event: str, data: dict) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")

    def static(self, h, rel: str) -> bool:
        rel = urllib.parse.unquote(rel)
        if "\x00" in rel or "\\" in rel:
            h.error_json(400, "bad path")
            return False
        parts = [s for s in rel.split("/") if s not in ("", ".")]
        if any(s == ".." for s in parts):
            h.error_json(403, "bad path")
            return False
        root = os.path.realpath(TABLET_DIR)
        full = os.path.realpath(os.path.join(root, *parts)) if parts else root
        if full != root and not full.startswith(root + os.sep):
            h.error_json(403, "bad path")
            return False
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            h.error_json(404, "no such file in tablet/" if os.path.isdir(root) else "tablet/ is not there yet")
            return False
        ext = os.path.splitext(full)[1].lower()
        return self.send_file(h, full, MIME.get(ext, "application/octet-stream"), cache=False)

    def send_file(self, h, path: str, ctype: str, cache: bool) -> bool:
        if not os.path.isfile(path):
            h.error_json(404, f"file not found: {os.path.basename(path)}")
            return False
        st = os.stat(path)
        etag = f'"{int(st.st_mtime)}-{st.st_size}"'
        if h.headers.get("If-None-Match") == etag:
            h.send_response(304)
            h.send_header("ETag", etag)
            h.send_header("Content-Length", "0")
            h.end_headers()
            return True
        h.send_response(200)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(st.st_size))
        h.send_header("ETag", etag)
        h.send_header("Cache-Control", "max-age=3600" if cache else "no-cache")
        h.end_headers()
        if h.command != "HEAD":
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(256 * 1024)
                    if not chunk:
                        break
                    h.wfile.write(chunk)
        return True
