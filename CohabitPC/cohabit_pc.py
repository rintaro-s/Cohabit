#!/usr/bin/env python3
"""
Cohabit PC — Quest 3 の Cohabit が使う、同じ LAN の PC で動かすアプリ (仕様 8.1 の「外部 PC」)。

Python の標準ライブラリだけで動く (pip で何も入れなくてよい)。重いもの (Lev・音声合成・音声認識・ローカル LLM) は
それぞれのサーバー / ライブラリに任せ、このアプリは「Quest からの窓口」と「中で何が起きているかを見る画面」をする。

Quest から来るもの (LAN に公開):
  POST /v1/chat/completions        会話・ノートを読む (画像入り)。LAN の Ollama (既定) → 予備 (クラウド API・この PC) へ中継する (仕様 8.2)
  POST /v1/audio/transcriptions    音声認識。OpenAI 互換のサーバーか、この PC の faster-whisper (入っていれば)
  POST /v1/audio/speech            読み上げ。VOICEVOX (この PC) か OpenAI 互換のサーバー
  POST /decision/evaluate          候補の順位づけ。Lev (`lev serve`、/v1/systemone) に問い合わせる (仕様 6.4 / 19.5)
  POST /observe                    Quest の観測・判断・行動の記録 (仕様 8.4 / 24.1)。画面の「妹の頭の中」に出る
  GET  /health  /metrics           つながっているか・どこで推論しているか (仕様 11.3) / 遅延 P50・P95 (仕様 26.2)。
                                   /health は token なしで答える (Quest が LAN からこの PC を探す。app: "cohabit-pc")

タブレット (TABLET_API.md、tablet_api.py。LAN に公開。token があれば X-Cohabit-Token か ?token=):
  GET  /tablet/                    tablet/ の静的ファイル (ペンで解くノート。token なしで配る)
  GET  /tablet/vrm                 妹の VRM (設定 tablet.vrmPath)
  GET  /tablet/api/session         妹・今の問題・居場所・推論先・苦手の要約
  POST /tablet/api/problem         問題を作る (topic / photo / weak / next / text)。模範解答は PC にだけ置く
  POST /tablet/api/check           ノートの画像に赤ペン (行ごとの印・解けたか・ひとこと)。同じページは覚えた答えを返す
  POST /tablet/api/ask             hint / explain / answer
  POST /tablet/api/answer          Quest から: 声で言った答えを判定する。check は Quest の机の写真 (source: "quest") も受ける
  GET  /tablet/api/speech?text=…   読み上げ WAV (VOICEVOX)。使えなければ 204
  GET  /tablet/api/events          SSE (presence / say / problem)。15 秒ごとに keep-alive
  POST /tablet/api/presence        { where: "room"|"tablet" } か { wearing: bool } (Quest・画面から)
  POST /tablet/api/say             { text, expression? } 妹のひとことをタブレットへ (Quest の say も /observe から流す)

この PC のブラウザだけが使えるもの (127.0.0.1 から以外は断る):
  GET  /                           画面 (dashboard.html)
  /api/...                         画面が使う。設定の保存・つながりの確認・データの削除 (学習記録も)・
                                   自己チェック・ログイン時の自動起動・VOICEVOX の起動・ファイアウォール (どれもボタンを押したときだけ)

画像はメモリに少しだけ置いて画面に見せるだけで、ディスクには書かない (仕様 11.2)。
ディスクに書くのは data/events-日付.jsonl (記録) と data/learning.jsonl・data/weak_points.json (タブレットの学習記録・苦手の要約) だけ。
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import http.client as httpclient
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import tablet_api

VERSION = "1.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
CONFIG_PATH = os.path.join(HERE, "config.json")
MASK = "********"

# Quest のスクリプトが送るモデル名。PC 側の設定のモデルに置き換える (Quest を作り直さずにモデルを変えられる)
MODEL_ALIASES = {"cohabit-chat": "chatModel", "cohabit-vision": "visionModel"}
# Quest から来る POST の道 (これ以外は 404。数えない・記録しない)
QUEST_POST_ROUTES = ("/v1/chat/completions", "/v1/audio/transcriptions", "/v1/audio/speech", "/decision/evaluate", "/observe")
# どの LLM にも届かないとき、Quest・タブレットに言わせるひとこと (エラーの返事の "say")
LLM_DOWN_SAY = {"text": "ごめん、今ちょっと頭が回らない。少ししたらもう一回言って。", "expression": "worried"}

DEFAULT_CONFIG = {
    "port": 8000,
    # 空でなければ、Quest は X-Cohabit-Token ヘッダーにこれを付けないと断られる
    "token": "",
    "llm": {
        # 第一候補。lan_ollama (LAN の Ollama) | cloud_api (Groq など) | local_qwen (この PC の OpenAI 互換サーバー)
        "backend": "lan_ollama",
        # 第一候補に届かない (つながらない・時間切れ・5xx・429) とき、次を試す: lan_ollama → cloud_api (キーがあれば) → local_qwen
        "fallback": True,
        "connectTimeout": 4,   # 秒。つながらない相手はすぐ見切って次へ
        "readTimeout": 60,     # 秒。返事を待つ長さ
        "queueWait": 45,       # 秒。同じ LLM への要求は 1 つずつ。前が終わるのをこれだけ待ったら次の候補へ
        "lan_ollama": {
            "label": "LAN の Ollama (192.168.0.40)",
            "baseUrl": "http://192.168.0.40:11434/v1",
            "apiKey": "",
            "chatModel": "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL",
            "visionModel": "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL",
            # 考えさせない (Ollama の /v1 は reasoning_effort "none" で考えずに答える。約 2 秒)
            "thinking": False,
        },
        "cloud_api": {
            "label": "クラウド API",
            "baseUrl": "https://api.openai.com/v1",
            "apiKey": "",
            "chatModel": "gpt-4.1-mini",
            "visionModel": "gpt-4.1-mini",
        },
        "local_qwen": {
            "label": "この PC の LLM",
            "baseUrl": "http://127.0.0.1:8080/v1",
            "apiKey": "",
            "chatModel": "qwen3.5-9b",
            "visionModel": "qwen3.5-9b",
            # Qwen3.5 などの「考えてから答える」モデルは、考える分で max_tokens を使い切って返事が空になる
            # (Quest は 120〜400 トークンで頼む)。false なら考えさせない (Ollama: reasoning_effort、llama.cpp / vLLM: enable_thinking)
            "thinking": False,
        },
    },
    "stt": {
        # openai (OpenAI 互換の /audio/transcriptions) | faster-whisper (この PC。pip install faster-whisper してあれば)
        "engine": "openai",
        "baseUrl": "https://api.openai.com/v1",
        "apiKey": "",  # 空ならクラウド API のキーを使う
        "model": "whisper-1",
        "fasterWhisperModel": "large-v3-turbo",
        "device": "cuda",
    },
    "tts": {
        # voicevox (この PC の VOICEVOX ENGINE) | openai (OpenAI 互換の /audio/speech)
        "engine": "voicevox",
        "voicevoxUrl": "http://127.0.0.1:50021",
        "speaker": 58,  # 猫使ビィ (ノーマル)
        "speed": 1.1,
        "pitch": 0.0,
        "baseUrl": "https://api.openai.com/v1",
        "apiKey": "",
        "model": "gpt-4o-mini-tts",
        "voice": "nova",
        # VOICEVOX が動いていなければ起動時に立ち上げる。null = 実行ファイルが見つかったときだけ。
        # voicevoxPath が空なら決まった場所を探す (VOICEVOX ENGINE の run / VOICEVOX 本体)
        "autoLaunch": None,
        "voicevoxPath": "",
    },
    "lev": {
        # `lev serve --checkpoint interfaze-ai/lev --port 8001` の場所。空なら使わない (Quest は自分の決まりで決める)
        "url": "http://127.0.0.1:8001",
        "timeout": 10,
    },
    "privacy": {
        "allowImages": True,     # 画像 (ノート・机) を受け付けるか。切ると Quest からの画像は断る
        "previewImages": True,   # 受けた画像を画面に見せるか (メモリだけ。ディスクには書かない)
        "logEvents": True,       # 観測・判断の記録を data/events-日付.jsonl に残すか (画像は入れない)
    },
    "tablet": {
        # タブレット (tablet_api.py)。VRM は CohabitPC からの相対パスか絶対パス
        "vrmPath": tablet_api.DEFAULT_VRM,
        "sisterName": "妹",
        "studentName": "",       # 空なら妹は名前で呼ばない
        "keepLearning": True,    # 学習記録を data/learning.jsonl に残すか (苦手から出す・苦手の要約に使う)
        "keepAliveSeconds": 15,  # SSE の keep-alive の間隔
    },
}

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------


def deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.data = copy.deepcopy(DEFAULT_CONFIG)
        self.load_error = None
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if not isinstance(loaded, dict):
                    raise ValueError("not a JSON object")
                self.data = deep_merge(DEFAULT_CONFIG, loaded)
            except (OSError, ValueError) as e:
                # 壊れた config.json で起動できなくならないように: 既定で動く (ファイルは画面で保存するまで触らない)
                self.load_error = f"{type(e).__name__}: {e}"
        # 環境変数のキーは、設定に無いときだけ使う
        env_key = os.environ.get("OPENAI_API_KEY", "")
        if env_key and not self.data["llm"]["cloud_api"]["apiKey"]:
            self.data["llm"]["cloud_api"]["apiKey"] = env_key

    def get(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.data)

    def update(self, change: dict) -> dict:
        """画面からの変更。MASK のままのキーは前の値を残す。"""
        with self.lock:
            merged = deep_merge(self.data, unmask(change, self.data))
            self.data = merged
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            return copy.deepcopy(merged)


def unmask(change, current):
    if isinstance(change, dict):
        return {k: unmask(v, (current or {}).get(k) if isinstance(current, dict) else None) for k, v in change.items()}
    if change == MASK:
        return current
    return change


def masked(cfg: dict) -> dict:
    def walk(node):
        if isinstance(node, dict):
            return {k: (MASK if (k in ("apiKey", "token") and v) else walk(v)) for k, v in node.items()}
        return node
    return walk(cfg)


LLM_ORDER = ("lan_ollama", "cloud_api", "local_qwen")  # 第一候補がだめなときに試す順


def llm_backend_ids(cfg: dict) -> list[str]:
    """設定にある LLM の行き先 (llm の下の dict で baseUrl を持つもの)。"""
    llm = cfg.get("llm") or {}
    ids = [i for i in LLM_ORDER if isinstance(llm.get(i), dict)]
    ids += [k for k, v in llm.items() if isinstance(v, dict) and k not in ids and "baseUrl" in v]
    return ids


def profile_of(cfg: dict, backend: str) -> dict:
    sec = (cfg.get("llm") or {}).get(backend) or {}
    return {**sec, "label": sec.get("label") or backend, "baseUrl": sec.get("baseUrl") or "",
            "apiKey": sec.get("apiKey") or "", "chatModel": sec.get("chatModel") or "",
            "visionModel": sec.get("visionModel") or sec.get("chatModel") or "", "backend": backend}


def llm_profile(cfg: dict) -> dict:
    """第一候補の LLM (設定の llm.backend)。無い名前なら最初に見つかったもの。"""
    ids = llm_backend_ids(cfg)
    backend = cfg["llm"].get("backend") or "lan_ollama"
    if backend not in ids and ids:
        backend = ids[0]
    return profile_of(cfg, backend)


def thinking_of(prof: dict):
    """False = 考えさせない、None = 何も送らない (相手の既定のまま)。
    書いていなければ、この PC / LAN の LLM (local_qwen / lan_ollama) は考えさせない、クラウドは何も送らない
    (Groq の Qwen は reasoning_effort "none" を受けるが、OpenAI の gpt-4.1-mini は断るので、クラウドは書いたときだけ)。"""
    if prof.get("thinking") is not None:
        return bool(prof["thinking"])
    return False if prof.get("backend") in ("local_qwen", "lan_ollama") else None


def no_thinking(body: dict, prof: dict) -> dict:
    """考えさせない。Ollama (/v1)・Groq: reasoning_effort "none"。llama.cpp / vLLM (local_qwen): enable_thinking も。"""
    if thinking_of(prof) is False:
        body.setdefault("reasoning_effort", "none")
        if prof["backend"] == "local_qwen":
            body.setdefault("chat_template_kwargs", {"enable_thinking": False})
    return body


def key_for(section: dict, cfg: dict) -> str:
    return section.get("apiKey") or cfg["llm"]["cloud_api"].get("apiKey", "")


def where_of(url: str) -> str:
    """推論の実行先 (仕様 11.3)。this_pc / lan / cloud。"""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.startswith("127."):
        return "this_pc"
    if re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", host) or host.endswith(".local"):
        return "lan"
    return "cloud"


WHERE_JA = {"this_pc": "この PC", "lan": "LAN の別の PC", "cloud": "クラウド"}

# ---------------------------------------------------------------------------
# 記録 (メモリ) と計測
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.routes: dict[str, dict] = {}
        self.started = time.time()

    def record(self, route: str, ms: float, ok: bool):
        with self.lock:
            if route not in self.routes and len(self.routes) >= 64:
                route = "(other)"  # 知らない道がいくらでも増えないように
            r = self.routes.setdefault(route, {"count": 0, "errors": 0, "lat": deque(maxlen=500)})
            r["count"] += 1
            if not ok:
                r["errors"] += 1
            r["lat"].append(ms)

    def snapshot(self) -> dict:
        def pct(values, p):
            if not values:
                return None
            s = sorted(values)
            return round(s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))], 1)
        with self.lock:
            return {
                "uptimeSeconds": round(time.time() - self.started),
                "routes": {
                    k: {"count": v["count"], "errors": v["errors"], "p50": pct(v["lat"], 50), "p95": pct(v["lat"], 95)}
                    for k, v in self.routes.items()
                },
            }

    def reset(self):
        with self.lock:
            self.routes.clear()


class State:
    """画面に見せるもの。全部メモリ。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.events: deque = deque(maxlen=2000)
        self.event_seq = 0
        self.requests: deque = deque(maxlen=200)
        self.request_seq = 0
        self.images: deque = deque(maxlen=8)
        self.quest = {"lastSeen": None, "address": None, "status": None}
        # 音声認識: Quest から音声が届いたか (マイクが動いているか) を画面で分かるように
        self.stt = {"count": 0, "inFlight": 0, "lastAt": None, "lastOk": None, "lastText": None, "lastError": None,
                    "lastSeconds": None, "lastMs": None}
        self.health: dict = {}

    def add_event(self, ev: dict) -> dict:
        with self.lock:
            self.event_seq += 1
            ev = {**ev, "seq": self.event_seq}
            self.events.append(ev)
            if ev.get("type") == "status":
                self.quest["status"] = ev.get("payload")
                self.quest["statusAt"] = time.time()
            return ev

    def add_request(self, entry: dict) -> dict:
        with self.lock:
            self.request_seq += 1
            entry = {**entry, "seq": self.request_seq, "t": now_iso()}
            self.requests.append(entry)
            return entry

    def add_image(self, data_url: str, purpose: str) -> str | None:
        m = re.match(r"^data:(image/[\w.+-]+);base64,(.*)$", data_url, re.S)
        if not m:
            return None
        try:
            raw = base64.b64decode(m.group(2), validate=False)
        except Exception:
            return None
        image_id = uuid.uuid4().hex[:12]
        with self.lock:
            self.images.append({"id": image_id, "mime": m.group(1), "data": raw, "t": now_iso(), "bytes": len(raw), "purpose": purpose})
        return image_id

    def note_stt(self, started: bool = False, ok: bool | None = None, text: str | None = None, error: str | None = None,
                 seconds: float | None = None, ms: int | None = None):
        with self.lock:
            st = self.stt
            if started:
                st["count"] += 1
                st["inFlight"] += 1
                st["lastAt"] = time.time()
                return
            st["inFlight"] = max(0, st["inFlight"] - 1)
            st.update({"lastOk": ok, "lastMs": ms})
            if ok:
                st.update({"lastText": text, "lastError": None, "lastSeconds": seconds})
            else:
                st["lastError"] = error

    def seen_quest(self, address: str):
        with self.lock:
            self.quest["lastSeen"] = time.time()
            self.quest["address"] = address


# ---------------------------------------------------------------------------
# HTTP の小物
# ---------------------------------------------------------------------------


class UpstreamError(Exception):
    """上流に届かない / 上流が断った。kind: refused / timeout / connect_timeout / reset / dns / unreachable /
    llm_down (どの LLM にも届かない) / busy (順番待ちで時間切れ) / None。"""

    def __init__(self, status: int, message: str, kind: str | None = None):
        super().__init__(message)
        self.status = status
        self.kind = kind


def _redact(url: str) -> str:
    """エラーの文に URL を入れるとき、クエリ (token などが入りうる) は落とす。"""
    return url.split("?", 1)[0]


def http(method: str, url: str, body: bytes | None = None, headers: dict | None = None, timeout: float = 60,
         connect_timeout: float | None = None) -> tuple[int, dict, bytes]:
    """1 回の HTTP。つなぐまでは connect_timeout (既定 min(5, timeout)) 秒、返事は timeout 秒まで待つ。
    届かなければ UpstreamError(502, kind=…)。HTTP のエラー (4xx / 5xx) は例外にせず状態コードで返す。"""
    # Python の既定の User-Agent (Python-urllib/…) は Groq の前段 (Cloudflare) に 403 で断られる (2026-10-12)
    h = {"User-Agent": f"CohabitPC/{VERSION}", **(headers or {})}
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise UpstreamError(502, f"bad URL: {_redact(url)}", "unreachable")
    connect_timeout = min(float(timeout), float(connect_timeout or 5))
    proxy = urllib.request.getproxies().get(u.scheme)
    if proxy and not urllib.request.proxy_bypass(u.hostname):
        # プロキシの設定があるときは urllib に任せる (つなぐ時間と待つ時間を分けられない)
        req = urllib.request.Request(url, data=body, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return res.status, dict(res.headers), res.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read() if e.fp else b""
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            raise UpstreamError(502, f"{_redact(url)}: {getattr(e, 'reason', e)}", "unreachable") from e
    cls = httpclient.HTTPSConnection if u.scheme == "https" else httpclient.HTTPConnection
    conn = cls(u.hostname, u.port, timeout=connect_timeout)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    stage = "connect"
    try:
        conn.connect()
        conn.sock.settimeout(float(timeout))
        stage = "read"
        conn.request(method, path, body=body, headers=h)
        res = conn.getresponse()
        data = res.read()
        return res.status, dict(res.headers), data
    except (socket.timeout, TimeoutError) as e:
        kind = "connect_timeout" if stage == "connect" else "timeout"
        what = f"つながらない ({connect_timeout:g} 秒)" if stage == "connect" else f"返事が来ない ({float(timeout):g} 秒)"
        raise UpstreamError(502 if stage == "connect" else 504, f"{_redact(url)}: {what}", kind) from e
    except ConnectionRefusedError as e:
        raise UpstreamError(502, f"{_redact(url)}: 断られた (動いていない?)", "refused") from e
    except (ConnectionResetError, BrokenPipeError, httpclient.RemoteDisconnected, httpclient.IncompleteRead) as e:
        raise UpstreamError(502, f"{_redact(url)}: 途中で切れた ({type(e).__name__})", "reset") from e
    except socket.gaierror as e:
        raise UpstreamError(502, f"{_redact(url)}: 名前が引けない ({e})", "dns") from e
    except (httpclient.HTTPException, OSError, ValueError) as e:
        raise UpstreamError(502, f"{_redact(url)}: {type(e).__name__}: {e}", "unreachable") from e
    finally:
        conn.close()


def http_json(method: str, url: str, payload=None, headers: dict | None = None, timeout: float = 60) -> tuple[int, dict | None, bytes]:
    h = {"Content-Type": "application/json", **(headers or {})}
    status, _, raw = http(method, url, json.dumps(payload).encode() if payload is not None else None, h, timeout)
    try:
        return status, json.loads(raw.decode("utf-8")) if raw else None, raw
    except (ValueError, UnicodeDecodeError):
        return status, None, raw


def bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"} if key else {}


def parse_multipart(body: bytes, content_type: str) -> dict:
    """multipart/form-data → {名前: str | {"filename", "type", "data"}}。(cgi は 3.13 で無くなったので自前)"""
    m = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not m:
        raise ValueError("multipart boundary missing")
    boundary = b"--" + m.group(1).encode()
    fields: dict = {}
    for part in body.split(boundary):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        head, _, data = part.partition(b"\r\n\r\n")
        if data.endswith(b"\r\n"):
            data = data[:-2]
        headers = {}
        for line in head.decode("utf-8", "replace").split("\r\n"):
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        disp = headers.get("content-disposition", "")
        name = re.search(r'name="([^"]*)"', disp)
        if not name:
            continue
        filename = re.search(r'filename="([^"]*)"', disp)
        if filename:
            fields[name.group(1)] = {"filename": filename.group(1), "type": headers.get("content-type", "application/octet-stream"), "data": data}
        else:
            fields[name.group(1)] = data.decode("utf-8", "replace")
    return fields


def encode_multipart(fields: dict) -> tuple[bytes, str]:
    boundary = "cohabit" + uuid.uuid4().hex
    out = io.BytesIO()
    for name, value in fields.items():
        out.write(f"--{boundary}\r\n".encode())
        if isinstance(value, dict):
            out.write(f'Content-Disposition: form-data; name="{name}"; filename="{value["filename"]}"\r\n'.encode())
            out.write(f'Content-Type: {value.get("type", "application/octet-stream")}\r\n\r\n'.encode())
            out.write(value["data"])
        else:
            out.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            out.write(str(value).encode())
        out.write(b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def lan_addresses() -> list[str]:
    found = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # 送らない。どの口から出るかを知るだけ
        found.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in found:
                found.append(ip)
    except OSError:
        pass
    return found


# ---------------------------------------------------------------------------
# Lev (仕様 6.4 / 19.5)。候補は Quest が決まりで絞ったものだけが来る。Lev は順位をつけるだけ
# ---------------------------------------------------------------------------

CANDIDATE_TEXT = {
    "SILENT": "stay silent and let the user keep working",
    "OBSERVE": "keep watching the notebook quietly without speaking",
    "ASK": "briefly ask whether the user would like a hint",
    "HINT": "give one short hint about the next step only",
    "ENCOURAGE": "say a short word of encouragement",
    "ask_user": "point at the unfamiliar object and ask the user what it is",
    "look_at": "just look at the object for a moment without speaking",
    "wait": "keep the interest for later and do nothing now",
    "drop_interest": "lose interest in the object",
}

INSTRUCTIONS = {
    "study": (
        "You decide what a study-companion character in mixed reality does next while the user studies math on paper. "
        "Interrupting a user who is focused or making progress is bad. Offer help only when the observations show the user is stuck. "
        "The candidates were already filtered by safety rules; pick the most appropriate one."
    ),
    "curiosity": (
        "A mixed-reality companion character noticed an unfamiliar object on the user's desk. The user is not studying right now. "
        "Asking about it is fine if it is new and interesting, but repeated or untimely questions are annoying."
    ),
}


def lev_evaluate(cfg: dict, body: dict) -> dict:
    url = (cfg["lev"].get("url") or "").rstrip("/")
    if not url:
        raise UpstreamError(503, "Lev is not configured on the PC")
    candidates = [c for c in body.get("candidates") or [] if isinstance(c, str)]
    if len(candidates) < 2:
        raise UpstreamError(422, "at least two candidates are needed")
    kind = body.get("kind") or ("curiosity" if "ask_user" in candidates else "study")
    request = {
        "state": body.get("state") or {},
        "questions": {
            "action": {
                "type": "choice",
                "instructions": INSTRUCTIONS.get(kind, INSTRUCTIONS["study"]),
                "criteria": {c: CANDIDATE_TEXT.get(c) for c in candidates},
            }
        },
    }
    status, data, raw = http_json("POST", url + "/v1/systemone", request, timeout=float(cfg["lev"].get("timeout") or 10))
    if status != 200 or not data:
        raise UpstreamError(502 if status < 500 else status, f"Lev answered {status}: {raw[:200].decode('utf-8', 'replace')}")
    answer = (data.get("answers") or {}).get("action") or {}
    return {
        "scores": answer.get("probabilities") or {},
        "choice": answer.get("choice"),
        "confidence": answer.get("confidence"),
        "source": "lev",
        "model": data.get("model"),
    }


# ---------------------------------------------------------------------------
# 音声
# ---------------------------------------------------------------------------

_whisper_lock = threading.Lock()
_whisper_model = None


def transcribe_local(cfg: dict, wav: bytes, language: str) -> str:
    global _whisper_model
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as e:
        raise UpstreamError(503, "faster-whisper is not installed on this PC (pip install faster-whisper)") from e
    with _whisper_lock:
        if _whisper_model is None:
            stt = cfg["stt"]
            device = stt.get("device") or "cuda"
            _whisper_model = WhisperModel(stt.get("fasterWhisperModel") or "large-v3-turbo", device=device,
                                          compute_type="float16" if device == "cuda" else "int8")
        segments, _ = _whisper_model.transcribe(io.BytesIO(wav), language=language or "ja", vad_filter=True)
        return "".join(s.text for s in segments).strip()


def speak_voicevox(cfg: dict, text: str) -> bytes:
    tts = cfg["tts"]
    base = (tts.get("voicevoxUrl") or "http://127.0.0.1:50021").rstrip("/")
    speaker = int(tts.get("speaker") or 58)
    q = urllib.parse.urlencode({"text": text, "speaker": speaker})
    status, query, raw = http_json("POST", f"{base}/audio_query?{q}", None, timeout=20)
    if status != 200 or not isinstance(query, dict):
        raise UpstreamError(502, f"VOICEVOX audio_query answered {status}: {raw[:200].decode('utf-8', 'replace')}")
    query["speedScale"] = float(tts.get("speed") or 1.0)
    query["pitchScale"] = float(tts.get("pitch") or 0.0)
    status, _, wav = http("POST", f"{base}/synthesis?speaker={speaker}", json.dumps(query).encode(),
                          {"Content-Type": "application/json", "Accept": "audio/wav"}, timeout=60)
    if status != 200:
        raise UpstreamError(502, f"VOICEVOX synthesis answered {status}")
    return wav


strip_latex = tablet_api.strip_latex  # LaTeX を素の式に (tablet_api.py にある。タブレットの文にも使う)
strip_latex_content = tablet_api.strip_latex_content  # 返事全体に (JSON なら値だけ)


# ---------------------------------------------------------------------------
# LLM の行き先を選ぶ (第一候補 → 予備)。1 つの LLM には 1 度に 1 つずつ。同じ要求はまとめる
# ---------------------------------------------------------------------------

FAILOVER_KINDS = {"refused", "timeout", "connect_timeout", "reset", "dns", "unreachable", "busy"}
NO_FAILOVER_STATUS = {400, 413, 422}  # 要求の形が悪い: 予備に回しても同じ


class BackendSlot:
    """1 つの LLM の「今やっている 1 つ」と順番待ち。interactive (人が待っている) が先。
    background (Quest の pause の机の写真・苦手の要約) は待たない: 空いていなければすぐ断る (kind="busy")。
    だから interactive が待つのは、多くても今走っている 1 つと、先に並んだ interactive だけ。"""

    def __init__(self, name: str):
        self.name = name
        self.cond = threading.Condition()
        self.running: dict | None = None
        self.queue: list[dict] = []   # 並んでいる interactive

    def acquire(self, job: dict, timeout: float) -> bool:
        with self.cond:
            if job["priority"] == "background":
                if self.running is not None or self.queue:
                    return False
                self.running = {**job, "started": time.time()}
                return True
            job = {**job, "queuedAt": time.time()}
            self.queue.append(job)
            deadline = time.time() + timeout
            try:
                while self.running is not None or self.queue[0] is not job:
                    left = deadline - time.time()
                    if left <= 0:
                        return False
                    self.cond.wait(left)
                self.running = {**job, "started": time.time()}
                return True
            finally:
                if job in self.queue:
                    self.queue.remove(job)
                self.cond.notify_all()

    def release(self):
        with self.cond:
            self.running = None
            self.cond.notify_all()

    def snapshot(self) -> dict:
        now = time.time()
        with self.cond:
            run = self.running
            return {"running": {"route": run["route"], "priority": run["priority"], "seconds": round(now - run["started"], 1)}
                    if run else None,
                    "queue": [{"route": j["route"], "priority": j["priority"], "seconds": round(now - j["queuedAt"], 1)}
                              for j in self.queue]}


BACKGROUND_BUSY = "LLM が他の仕事をしているので、後回しにできる要求は飛ばした (busy)"


class LLMRouter:
    STICKY_SECONDS = 120   # 予備が答えたら、この間はそれを先に使う (落ちた第一候補を毎回待たない)
    CACHE_SECONDS = 20     # まったく同じ要求 (Quest の再送など) は覚えた答えを返す
    CACHE_MAX = 32

    def __init__(self, app=None):
        self.app = app
        self.lock = threading.Lock()
        self.slots: dict[str, BackendSlot] = {}
        self.sticky: tuple[str, float] | None = None
        self.last: dict | None = None
        self.errors: dict[str, dict] = {}
        self.waiting: dict[str, int] = {}
        self.cache: OrderedDict = OrderedDict()
        self.inflight: dict[str, threading.Event] = {}

    # ---- 順番 ----

    def chain(self, cfg: dict, only_primary: bool = False) -> list[dict]:
        primary = llm_profile(cfg)
        if only_primary or cfg["llm"].get("fallback") is False:
            return [primary] if primary["baseUrl"] else []
        ids = [primary["backend"]] + [i for i in llm_backend_ids(cfg) if i != primary["backend"]]
        out = []
        for i in ids:
            prof = profile_of(cfg, i)
            if not prof["baseUrl"] or prof.get("enabled") is False:
                continue
            if i != primary["backend"] and where_of(prof["baseUrl"]) == "cloud" and not prof["apiKey"]:
                continue  # キーの無いクラウドは予備にしない
            out.append(prof)
        now = time.time()
        with self.lock:
            sticky = self.sticky if self.sticky and self.sticky[1] > now else None
            recent_bad = {k for k, v in self.errors.items() if now - v["t"] < 60}
        health = self._health()
        bad = recent_bad | {k for k, v in health.items() if v.get("ok") is False}

        def rank(p):
            if sticky and p["backend"] == sticky[0]:
                return 0
            return 2 if p["backend"] in bad else 1
        return sorted(out, key=rank)  # sorted は安定: 同じ順位なら設定の順

    def _health(self) -> dict:
        if self.app is None:
            return {}
        with self.app.state.lock:
            return dict(self.app.state.health.get("llmBackends") or {})

    def current(self, cfg: dict) -> dict:
        """今いちばん先に頼む LLM (画面と /health 用。LLM には聞かない)。"""
        chain = self.chain(cfg)
        prof = chain[0] if chain else llm_profile(cfg)
        health = self._health().get(prof["backend"]) or {}
        with self.lock:
            err = self.errors.get(prof["backend"])
            last = dict(self.last) if self.last else None
        ok = health.get("ok")
        if last and last["backend"] == prof["backend"] and (not err or err["t"] < last["at"]):
            ok = True
        elif err and time.time() - err["t"] < 60:
            ok = False
        return {"backend": prof["backend"], "label": prof["label"], "model": prof["chatModel"],
                "where": where_of(prof["baseUrl"]), "ok": bool(ok) if ok is not None else None,
                "primary": cfg["llm"].get("backend"), "lastAnswered": last}

    def status(self, cfg: dict) -> dict:
        with self.lock:
            errors = {k: {**v, "secondsAgo": round(time.time() - v["t"])} for k, v in self.errors.items()}
            waiting = dict(self.waiting)
            sticky = self.sticky[0] if self.sticky and self.sticky[1] > time.time() else None
            slots = dict(self.slots)
        jobs = {k: v.snapshot() for k, v in slots.items()}
        return {**self.current(cfg), "errors": errors, "waiting": waiting, "sticky": sticky,
                "chain": [p["backend"] for p in self.chain(cfg)],
                # 今 AI がしていること: LLM ごとに走っている 1 つと、並んでいるもの
                "jobs": {k: v for k, v in jobs.items() if v["running"] or v["queue"]}}

    # ---- 頼む ----

    def complete(self, cfg: dict, body: dict, vision: bool = False, explicit_model: bool = False,
                 cache: bool = True, priority: str = "interactive", route: str = "chat") -> dict:
        """OpenAI 互換の /chat/completions を、使える LLM に順に頼む。
        → {status, raw, data, content, backend, label, where, model, ms, cached?, tried}。
        どこにも届かなければ UpstreamError(503 / 429, kind="llm_down")。400 などはそのまま status で返す。"""
        chain = self.chain(cfg, only_primary=explicit_model)
        ident = [[p["backend"], p["baseUrl"], p["chatModel"], p["visionModel"], thinking_of(p)] for p in chain]
        key = hashlib.sha256(json.dumps([body, vision, explicit_model, ident], sort_keys=True,
                                        ensure_ascii=False).encode()).hexdigest() if cache else None
        owner = False
        if key:
            for _ in range(2):
                with self.lock:
                    hit = self.cache.get(key)
                    if hit and time.time() - hit[0] < self.CACHE_SECONDS:
                        return {**hit[1], "cached": True}
                    ev = self.inflight.get(key)
                    if ev is None:
                        self.inflight[key] = threading.Event()
                        owner = True
                        break
                ev.wait(float(cfg["llm"].get("readTimeout") or 60) + 5)  # 同じ要求が走っている: 終わるのを待つ
        try:
            result = self._complete(cfg, body, vision, explicit_model, chain,
                                    {"route": route, "priority": "background" if priority == "background" else "interactive"})
            if key and result["status"] == 200:
                with self.lock:
                    self.cache[key] = (time.time(), result)
                    while len(self.cache) > self.CACHE_MAX:
                        self.cache.popitem(last=False)
            return result
        finally:
            if owner:
                with self.lock:
                    ev = self.inflight.pop(key, None)
                if ev:
                    ev.set()

    def _slot(self, backend: str) -> BackendSlot:
        with self.lock:
            return self.slots.setdefault(backend, BackendSlot(backend))

    def _complete(self, cfg: dict, body: dict, vision: bool, explicit_model: bool, chain: list,
                  job: dict | None = None) -> dict:
        job = job or {"route": "chat", "priority": "interactive"}
        llm = cfg["llm"]
        connect_t = float(llm.get("connectTimeout") or 4)
        read_t = float(llm.get("readTimeout") or 60)
        queue_wait = float(llm.get("queueWait") or 45)
        if not chain:
            raise UpstreamError(503, "LLM の行き先が設定されていない (設定の「会話・ノートを読む」)", "llm_down")
        tried, saw_429 = [], False
        for i, prof in enumerate(chain):
            b = copy.deepcopy(body)
            if not explicit_model:
                b["model"] = prof["visionModel"] if vision else prof["chatModel"]
            no_thinking(b, prof)
            payload = json.dumps(b).encode()
            url = prof["baseUrl"].rstrip("/") + "/chat/completions"
            headers = {"Content-Type": "application/json", **bearer(prof.get("apiKey", ""))}
            slot = self._slot(prof["backend"])
            last_one = i == len(chain) - 1
            with self.lock:
                self.waiting[prof["backend"]] = self.waiting.get(prof["backend"], 0) + 1
            got = slot.acquire({**job, "backend": prof["backend"]}, timeout=queue_wait if not last_one else queue_wait + read_t)
            with self.lock:
                self.waiting[prof["backend"]] -= 1
            if not got and job["priority"] == "background":
                # 後回しにできる要求は待たせない (人が待っている要求の前に並ばない)。予備にも回さない
                raise UpstreamError(503, f"{BACKGROUND_BUSY}: {prof['backend']}", "busy")
            if not got:
                tried.append(f"{prof['backend']}: 順番待ちで時間切れ ({queue_wait:g} 秒)")
                self._note_error(prof["backend"], "busy", remember=False)
                continue
            t0 = time.time()
            try:
                status, raw, err = None, b"", None
                for attempt in range(2):  # 一時的なもの (途中で切れた・502/503) は 1 回だけやり直す
                    try:
                        status, _, raw = http("POST", url, payload, headers, timeout=float(prof.get("timeout") or read_t),
                                              connect_timeout=connect_t)
                        err = None
                    except UpstreamError as e:
                        status, err = None, e
                    retry = (err is not None and err.kind == "reset") or status in (502, 503)
                    if not retry or attempt == 1:
                        break
                    time.sleep(0.5)
            finally:
                slot.release()
            ms = round((time.time() - t0) * 1000)
            if err is not None:
                tried.append(f"{prof['backend']}: {err}")
                self._note_error(prof["backend"], str(err))
                continue
            if status == 200:
                data, content = None, None
                try:
                    data = json.loads(raw.decode("utf-8"))
                    content = data["choices"][0]["message"].get("content")
                except (ValueError, KeyError, IndexError, TypeError, AttributeError, UnicodeDecodeError):
                    pass
                if data is None:
                    tried.append(f"{prof['backend']}: 200 だが JSON でない")
                    self._note_error(prof["backend"], "200 but not JSON")
                    continue
                if isinstance(content, str):
                    cleaned = strip_latex_content(content)
                    if cleaned != content:
                        data["choices"][0]["message"]["content"] = cleaned
                        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
                        content = cleaned
                self._note_ok(prof, ms, primary=prof["backend"] == llm_profile(cfg)["backend"])
                return {"status": 200, "raw": raw, "data": data, "content": content or "", "backend": prof["backend"],
                        "label": prof["label"], "where": where_of(prof["baseUrl"]), "model": b.get("model"), "ms": ms,
                        "tried": tried}
            snippet = raw[:200].decode("utf-8", "replace")
            if status in NO_FAILOVER_STATUS:
                return {"status": status, "raw": raw, "data": None, "content": None, "backend": prof["backend"],
                        "label": prof["label"], "where": where_of(prof["baseUrl"]), "model": b.get("model"), "ms": ms,
                        "tried": tried, "error": f"LLM answered {status}: {snippet}"}
            saw_429 = saw_429 or status == 429
            tried.append(f"{prof['backend']}: HTTP {status} {snippet[:120]}")
            self._note_error(prof["backend"], f"HTTP {status}")
        if saw_429 and len(tried) == 1:
            raise UpstreamError(429, "LLM の回数・トークンの上限 (429)。少し待ってやり直す: " + tried[0], "llm_down")
        raise UpstreamError(503, "どの LLM にも届かない — " + " / ".join(tried), "llm_down")

    def _note_error(self, backend: str, message: str, remember: bool = True):
        with self.lock:
            if remember:
                self.errors[backend] = {"t": time.time(), "message": message[:300]}
            if self.sticky and self.sticky[0] == backend:
                self.sticky = None

    def _note_ok(self, prof: dict, ms: int, primary: bool):
        with self.lock:
            self.errors.pop(prof["backend"], None)
            self.last = {"backend": prof["backend"], "label": prof["label"], "where": where_of(prof["baseUrl"]),
                         "ms": ms, "t": now_iso(), "at": time.time()}
            # 第一候補でないものが答えた → しばらくはそれを先に。第一候補が答えた → 戻す
            self.sticky = None if primary else (prof["backend"], time.time() + self.STICKY_SECONDS)

    def primary_recovered(self, backend: str):
        """自己チェックで第一候補が戻った: 予備に張り付くのをやめる。"""
        with self.lock:
            self.errors.pop(backend, None)
            if self.sticky and self.sticky[0] != backend:
                self.sticky = None


# ---------------------------------------------------------------------------
# 自己チェック (起動直後から裏で 30 秒ごと。LLM は生成せず一覧を読むだけ)。起動は待たせない
# ---------------------------------------------------------------------------

SELF_CHECK_SECONDS = 30


def llm_probe(prof: dict) -> tuple[bool, str, str]:
    """→ (ok, detail, hint)。GET {baseUrl}/models (OpenAI 互換。Ollama も答える) で、モデルがあるかまで見る。"""
    if not prof["baseUrl"]:
        return False, "URL がない", "設定の「会話・ノートを読む」で URL を入れる"
    if where_of(prof["baseUrl"]) == "cloud" and not prof.get("apiKey"):
        return False, "API キーがない", "設定で API キーを入れる (無ければ予備にはしない)"
    try:
        status, data, _ = http_json("GET", prof["baseUrl"].rstrip("/") + "/models", None, bearer(prof.get("apiKey", "")),
                                    timeout=6)
    except UpstreamError as e:
        host = urllib.parse.urlsplit(prof["baseUrl"]).netloc
        if where_of(prof["baseUrl"]) == "cloud":
            return False, str(e), "インターネットにつながっているか確かめる"
        if e.kind == "refused":
            return False, str(e), f"{host} で Ollama / LLM サーバーが動いていない。起動する (ollama serve)"
        return False, str(e), f"{host} の PC の電源・LAN・ファイアウォール (11434 番) を確かめる。OLLAMA_HOST=0.0.0.0 で起動しているか"
    if status in (401, 403):
        return False, f"HTTP {status}", "API キーが違う"
    if status != 200:
        return False, f"HTTP {status}", "URL が違うかもしれない (…/v1 で終わる OpenAI 互換の URL)"
    names = [m.get("id") for m in (data or {}).get("data") or [] if isinstance(m, dict)]
    want = [m for m in {prof.get("chatModel"), prof.get("visionModel")} if m]
    missing = [m for m in want if names and m not in names]
    if missing:
        return False, f"モデルが無い: {', '.join(missing)}", f"その PC で ollama pull {missing[0]}"
    return True, f"OK · {prof.get('chatModel')}", ""


def check_backends(cfg: dict, voicevox_path: str | None = None) -> dict:
    out = {"llmBackends": {}}
    lock = threading.Lock()

    def probe(name, fn, sub=None):
        t0 = time.time()
        hint = ""
        try:
            r = fn()
            ok, detail = r[0], r[1]
            hint = r[2] if len(r) > 2 else ""
        except UpstreamError as e:
            ok, detail = False, str(e)
        except Exception as e:  # 確認で落ちない
            ok, detail = False, f"{type(e).__name__}: {e}"
        entry = {"ok": ok, "detail": detail, "hint": "" if ok else hint, "ms": round((time.time() - t0) * 1000)}
        with lock:
            (out["llmBackends"] if sub else out)[name] = entry

    def stt():
        s = cfg["stt"]
        if s["engine"] == "faster-whisper":
            try:
                import faster_whisper  # noqa: F401  # type: ignore
                return True, "faster-whisper (この PC)"
            except ImportError:
                return False, "faster-whisper が入っていない", "pip install faster-whisper か、設定で「OpenAI 互換の API」に"
        if where_of(s["baseUrl"]) == "cloud" and not key_for(s, cfg):
            return False, "API キーがない", "設定の「音声認識」か「会話」の API キーを入れる"
        try:
            status, _, _ = http_json("GET", s["baseUrl"].rstrip("/") + "/models", None, bearer(key_for(s, cfg)), timeout=6)
        except UpstreamError as e:
            return False, str(e), "インターネット / 音声認識サーバーにつながるか確かめる"
        return status == 200, f"HTTP {status}", "API キーか URL を確かめる"

    def tts():
        t = cfg["tts"]
        if t["engine"] == "voicevox":
            try:
                status, _, raw = http("GET", t["voicevoxUrl"].rstrip("/") + "/version", None, {}, timeout=4)
            except UpstreamError as e:
                hint = ("VOICEVOX が動いていない。下の「VOICEVOX を起動」を押す" if voicevox_path
                        else "VOICEVOX (か VOICEVOX ENGINE) を起動する。無ければ声なし (字幕だけ) で動く")
                return False, str(e), hint
            return (status == 200, f"VOICEVOX {raw.decode('utf-8', 'replace').strip(chr(34))}" if status == 200 else f"HTTP {status}",
                    "VOICEVOX の URL を確かめる")
        if where_of(t["baseUrl"]) == "cloud" and not key_for(t, cfg):
            return False, "API キーがない", "設定の「読み上げ」の API キーを入れる"
        status, _, _ = http_json("GET", t["baseUrl"].rstrip("/") + "/models", None, bearer(key_for(t, cfg)), timeout=6)
        return status == 200, f"HTTP {status}", "API キーか URL を確かめる"

    def lev():
        url = (cfg["lev"].get("url") or "").rstrip("/")
        if not url:
            return False, "使わない設定", "使わなくても動く (Quest が自分の決まりで判断する)"
        try:
            status, data, _ = http_json("GET", url + "/health", None, timeout=4)
        except UpstreamError as e:
            return False, str(e), "使うなら lev serve --checkpoint interfaze-ai/lev --port 8001 を起動する。無くても動く"
        if status == 200 and isinstance(data, dict):
            return data.get("status") == "ok", f"{data.get('status')} · {data.get('model', '')}", "lev serve の様子を見る"
        return False, f"HTTP {status}", "Lev の URL を確かめる"

    jobs = [("stt", stt, None), ("tts", tts, None), ("lev", lev, None)]
    for bid in llm_backend_ids(cfg):
        prof = profile_of(cfg, bid)
        if not prof["baseUrl"] or (bid != llm_profile(cfg)["backend"] and where_of(prof["baseUrl"]) == "cloud" and not prof["apiKey"]):
            continue  # 使わない予備は確かめない
        jobs.append((bid, (lambda p=prof: llm_probe(p)), True))
    threads = [threading.Thread(target=probe, args=a, daemon=True) for a in jobs]
    for t in threads:
        t.start()
    deadline = time.time() + 12
    for t in threads:
        t.join(max(0.1, deadline - time.time()))
    # 全体の「会話・ノート」: 第一候補が OK ならそれ、だめでも予備が OK なら OK (予備で動く)
    primary = llm_profile(cfg)["backend"]
    backends = out["llmBackends"]
    p = backends.get(primary) or {"ok": False, "detail": "未確認", "hint": ""}
    spare = next((k for k, v in backends.items() if k != primary and v.get("ok")), None)
    if p.get("ok"):
        out["llm"] = {**p, "backend": primary}
    elif spare and cfg["llm"].get("fallback") is not False:
        out["llm"] = {"ok": True, "detail": f"第一候補 ({primary}) に届かない — 予備の {spare} で動く · {p.get('detail')}",
                      "hint": p.get("hint", ""), "ms": p.get("ms"), "backend": spare, "degraded": True}
    else:
        out["llm"] = {**p, "backend": primary}
    return out


def inference_summary(cfg: dict, prof: dict | None = None) -> dict:
    """Quest の HUD と画面に出す「どこで推論しているか」(仕様 11.3)。prof = 今頼んでいる LLM (予備に替わっていればそれ)。"""
    prof = prof or llm_profile(cfg)
    stt, tts = cfg["stt"], cfg["tts"]
    stt_where = "this_pc" if stt["engine"] == "faster-whisper" else where_of(stt["baseUrl"])
    tts_where = "this_pc" if tts["engine"] == "voicevox" and where_of(tts["voicevoxUrl"]) == "this_pc" else where_of(
        tts["voicevoxUrl"] if tts["engine"] == "voicevox" else tts["baseUrl"])
    parts = {
        "chat": {"label": prof["label"], "model": prof["chatModel"], "where": where_of(prof["baseUrl"])},
        "vision": {"label": prof["label"], "model": prof["visionModel"], "where": where_of(prof["baseUrl"])},
        "stt": {"label": "faster-whisper" if stt["engine"] == "faster-whisper" else "音声認識 API", "model": stt.get("fasterWhisperModel") if stt["engine"] == "faster-whisper" else stt["model"], "where": stt_where},
        "tts": {"label": "VOICEVOX" if tts["engine"] == "voicevox" else "読み上げ API", "model": str(tts.get("speaker")) if tts["engine"] == "voicevox" else tts["model"], "where": tts_where},
        "lev": {"label": "Lev", "model": "interfaze-ai/lev", "where": where_of(cfg["lev"].get("url") or "http://127.0.0.1") if cfg["lev"].get("url") else "off"},
    }
    text = (f"会話・ノート: {prof['label']} ({WHERE_JA.get(parts['chat']['where'], '')}) / "
            f"声: {parts['tts']['label']} ({WHERE_JA.get(tts_where, '')}) / "
            f"判断: {'Lev (' + WHERE_JA.get(parts['lev']['where'], '') + ')' if cfg['lev'].get('url') else 'Quest の決まり'}")
    return {"parts": parts, "text": text}


# ---------------------------------------------------------------------------
# 楽にする: ログイン時の自動起動・VOICEVOX を立ち上げる・ファイアウォール
# (どれも画面のボタンを押したときだけ。VOICEVOX だけは設定 tts.autoLaunch で起動時にも)
# ---------------------------------------------------------------------------


def _run(args: list[str], timeout: float = 120) -> tuple[bool, int, str]:
    """決まった引数だけを渡す (shell を通さない)。→ (ok, 終了コード, 出力の要約)。"""
    try:
        r = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()[:500]
    except FileNotFoundError:
        return False, 127, f"{args[0]} が無い"
    except subprocess.TimeoutExpired:
        return False, 124, f"{args[0]}: 時間切れ"
    except OSError as e:
        return False, 1, f"{args[0]}: {e}"


AUTOSTART_UNIT = "cohabit-pc.service"
AUTOSTART_BAT = "Cohabit PC.bat"


def autostart_target(env: dict | None = None) -> dict:
    env = os.environ if env is None else env
    home = env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")
    if os.name == "nt":
        roaming = env.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        return {"kind": "windows-startup",
                "path": os.path.join(roaming, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", AUTOSTART_BAT)}
    if sys.platform.startswith("linux"):
        base = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        return {"kind": "systemd-user", "path": os.path.join(base, "systemd", "user", AUTOSTART_UNIT)}
    return {"kind": None, "path": None}


def systemd_unit(here: str = HERE) -> str:
    if any(c in here for c in '"\n\\'):
        raise ValueError("このフォルダの名前 (\" や \\ を含む) では systemd の設定を書けない")
    esc = here.replace("%", "%%")
    return (
        "# Cohabit PC をログイン時に起動する (画面の「ログイン時に自動で起動する」で作った。\n"
        "# やめるには画面の「自動起動をやめる」か: systemctl --user disable --now cohabit-pc.service)\n"
        "[Unit]\n"
        "Description=Cohabit PC (the PC side of Cohabit for Quest)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={esc}\n"
        f'ExecStart=/bin/sh "{esc}/start.sh"\n'
        "Environment=PYTHONUNBUFFERED=1\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def windows_startup_bat(here: str = HERE) -> str:
    return (
        "@echo off\r\n"
        "rem Cohabit PC: start at login (made by the dashboard button; remove it there or delete this file)\r\n"
        "chcp 65001 >NUL\r\n"
        f'cd /d "{here}"\r\n'
        f'start "Cohabit PC" /min "{os.path.join(here, "start.bat")}"\r\n'
    )


def autostart_status(env: dict | None = None) -> dict:
    t = autostart_target(env)
    return {"supported": bool(t["kind"]), "kind": t["kind"], "installed": bool(t["path"] and os.path.isfile(t["path"])),
            "path": t["path"]}


def autostart_install(env: dict | None = None, run=_run, here: str = HERE) -> dict:
    t = autostart_target(env)
    if not t["kind"]:
        return {"ok": False, "message": "この OS では自動起動を作れない (Linux / Windows だけ)"}
    os.makedirs(os.path.dirname(t["path"]), exist_ok=True)
    text = systemd_unit(here) if t["kind"] == "systemd-user" else windows_startup_bat(here)
    tmp = t["path"] + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, t["path"])
    if t["kind"] == "systemd-user":
        # 今動いているこのアプリと同じポートを取り合わないよう、start はしない (次のログインから)
        ok1, _, out1 = run(["systemctl", "--user", "daemon-reload"], 30)
        ok2, _, out2 = run(["systemctl", "--user", "enable", AUTOSTART_UNIT], 30)
        if not (ok1 and ok2):
            return {"ok": False, "path": t["path"], "message": f"設定は書いたが有効にできなかった: {out2 or out1}。"
                                                               f"端末で: systemctl --user enable {AUTOSTART_UNIT}"}
        return {"ok": True, "path": t["path"], "message": "次のログインから自動で起動します (落ちても 5 秒後に立ち上げ直す)"}
    return {"ok": True, "path": t["path"], "message": "次のログインから自動で起動します (スタートアップに入れた)"}


def autostart_remove(env: dict | None = None, run=_run) -> dict:
    t = autostart_target(env)
    if not t["kind"]:
        return {"ok": False, "message": "この OS では自動起動を作れない"}
    if t["kind"] == "systemd-user" and os.path.isfile(t["path"]):
        run(["systemctl", "--user", "disable", AUTOSTART_UNIT], 30)  # 止めはしない (今動いているのはこのアプリ)
    if os.path.isfile(t["path"]):
        os.remove(t["path"])
    if t["kind"] == "systemd-user":
        run(["systemctl", "--user", "daemon-reload"], 30)
    return {"ok": True, "path": t["path"], "message": "自動起動をやめました"}


def voicevox_candidates(env: dict | None = None) -> list[str]:
    """VOICEVOX を探す場所。ENGINE (run) を先に (窓が出ない)、無ければ VOICEVOX 本体。"""
    import glob
    env = os.environ if env is None else env
    home = env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")
    if os.name == "nt":
        local = env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        pf = env.get("ProgramFiles") or r"C:\Program Files"
        roots = [os.path.join(local, "Programs", "VOICEVOX"), os.path.join(pf, "VOICEVOX")]
        out = [os.path.join(r, "vv-engine", "run.exe") for r in roots] + [os.path.join(r, "run.exe") for r in roots]
        out += [os.path.join(r, "VOICEVOX.exe") for r in roots]
        return out
    out = []
    for root in (os.path.join(home, "voicevox_engine"), os.path.join(home, ".voicevox"), "/opt/voicevox_engine", "/opt/voicevox"):
        out += [os.path.join(root, "run"), os.path.join(root, "vv-engine", "run")]
        out += sorted(glob.glob(os.path.join(root, "linux-*", "run")))
    for name in ("voicevox_engine", "voicevox-engine"):
        found = shutil.which(name, path=env.get("PATH"))
        if found:
            out.append(found)
    out += [os.path.join(home, ".voicevox", "VOICEVOX.AppImage")]
    out += sorted(glob.glob(os.path.join(home, "Applications", "VOICEVOX*.AppImage")))
    out += sorted(glob.glob(os.path.join(home, ".voicevox", "*.AppImage")))
    found = shutil.which("voicevox", path=env.get("PATH"))
    if found:
        out.append(found)
    return out


def find_voicevox(cfg: dict, env: dict | None = None) -> str | None:
    configured = ((cfg.get("tts") or {}).get("voicevoxPath") or "").strip()
    for p in ([configured] if configured else []) + voicevox_candidates(env):
        if p and os.path.isfile(p) and (os.name == "nt" or os.access(p, os.X_OK)):
            return p
    return None


def launch_voicevox(path: str, url: str, popen=subprocess.Popen) -> dict:
    """VOICEVOX を、このアプリとは別のプロセスとして立ち上げる (このアプリが終わっても残る)。"""
    port = urllib.parse.urlsplit(url or "http://127.0.0.1:50021").port or 50021
    base = os.path.basename(path).lower()
    args = [path, "--host", "127.0.0.1", "--port", str(port)] if base in ("run", "run.exe") else [path]
    kw = {"cwd": os.path.dirname(path) or None, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
          "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kw["start_new_session"] = True
    try:
        proc = popen(args, **kw)
    except OSError as e:
        return {"ok": False, "message": f"起動できなかった: {e}"}
    return {"ok": True, "pid": getattr(proc, "pid", None), "message": f"VOICEVOX を起動しています ({os.path.basename(path)})。10〜30 秒で使えます"}


def subnet24(address: str) -> str | None:
    """192.168.0.12 → 192.168.0.0/24"""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)\.\d+$", address or "")
    return f"{m.group(1)}.{m.group(2)}.{m.group(3)}.0/24" if m else None


def firewall_detect(run=_run, which=shutil.which) -> dict:
    """管理者の権限なしで分かることだけ: どのファイアウォールが有効か (規則は読めない)。"""
    if os.name == "nt":
        return {"kind": "windows", "active": True}
    if not sys.platform.startswith("linux"):
        return {"kind": None, "active": False}
    for kind, tool in (("ufw", "ufw"), ("firewalld", "firewall-cmd")):
        if not which(tool):
            continue
        ok, _, out = run(["systemctl", "is-active", kind], 5)
        if out.strip().splitlines()[:1] == ["active"]:
            return {"kind": kind, "active": True}
    return {"kind": None, "active": False}


def firewall_manual(kind: str | None, port: int, nets: list[str]) -> str:
    if kind == "ufw":
        return f"sudo ufw allow proto tcp from {nets[0] if nets else 'any'} to any port {port}"
    if kind == "firewalld":
        return f"sudo firewall-cmd --add-port={port}/tcp && sudo firewall-cmd --permanent --add-port={port}/tcp"
    if kind == "windows":
        return (f"New-NetFirewallRule -DisplayName 'Cohabit PC {port}' -Direction Inbound -Protocol TCP "
                f"-LocalPort {port} -Action Allow -Profile Private")
    return ""


def firewall_allow(port: int, lans: list[str], run=_run, which=shutil.which) -> dict:
    """LAN (同じ /24) からこのポートへの接続を許す。管理者の確認 (pkexec の窓 / UAC) が出る。"""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return {"ok": False, "message": "bad port"}
    if not 1 <= port <= 65535:
        return {"ok": False, "message": "bad port"}
    fw = firewall_detect(run, which)
    nets = list(dict.fromkeys(n for n in (subnet24(a) for a in lans) if n))
    manual = firewall_manual(fw["kind"], port, nets)
    if fw["kind"] in ("ufw", "firewalld"):
        if not which("pkexec"):
            return {"ok": False, "message": "pkexec が無い。端末で次を実行する", "command": manual}
        if fw["kind"] == "ufw":
            # 1 回の確認で済むように最初の LAN だけ (Quest のつながる Wi-Fi はたいていこれ)
            args = ["pkexec", "ufw", "allow", "proto", "tcp", "from", nets[0] if nets else "any", "to", "any",
                    "port", str(port), "comment", "Cohabit PC"]
        else:
            args = ["pkexec", "sh", "-c", f"firewall-cmd --add-port={port}/tcp && firewall-cmd --permanent --add-port={port}/tcp"]
        ok, code, out = run(args, 120)
        if ok:
            return {"ok": True, "message": f"{fw['kind']}: {port}/tcp を LAN から許可しました"}
        return {"ok": False, "message": "取り消された" if code == 126 else (out or f"exit {code}"),
                "command": manual}
    if fw["kind"] == "windows":
        rule = firewall_manual("windows", port, nets)
        ok, code, out = run(["powershell.exe", "-NoProfile", "-Command",
                             f"Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-Command',\"{rule}\""], 120)
        return {"ok": ok, "message": "Windows のファイアウォールに許可を足しました" if ok else (out or f"exit {code}"), "command": manual}
    return {"ok": True, "message": "有効なファイアウォールは見つからない (何もしなくてよい)"}


# ---------------------------------------------------------------------------
# アプリ
# ---------------------------------------------------------------------------


class App:
    def __init__(self, config: Config):
        self.config = config
        self.metrics = Metrics()
        self.state = State()
        self.stop = threading.Event()
        self.boot_id = uuid.uuid4().hex[:8]
        self.port = None
        self.llm = LLMRouter(self)
        self.extras = {"firewall": {"kind": None, "active": False}, "voicevox": {"path": None, "launched": None}}
        self.extras_lock = threading.Lock()
        self.tablet = tablet_api.Tablet(self, sys.modules[__name__])

    def refresh_health(self):
        cfg = self.config.get()
        vv = find_voicevox(cfg)
        result = check_backends(cfg, vv)
        with self.state.lock:
            self.state.health = {**result, "checkedAt": now_iso()}
        primary = llm_profile(cfg)["backend"]
        if (result.get("llmBackends") or {}).get(primary, {}).get("ok"):
            self.llm.primary_recovered(primary)
        try:
            fw = firewall_detect()
        except Exception:  # 確認で落ちない
            fw = {"kind": None, "active": False}
        with self.extras_lock:
            self.extras["firewall"] = fw
            self.extras["voicevox"]["path"] = vv

    def maybe_launch_voicevox(self):
        """VOICEVOX を使う設定で、この PC の VOICEVOX が動いておらず、実行ファイルが見つかれば立ち上げる (tts.autoLaunch)。"""
        cfg = self.config.get()
        tts = cfg["tts"]
        if tts.get("engine") != "voicevox" or tts.get("autoLaunch") is False or where_of(tts.get("voicevoxUrl") or "") != "this_pc":
            return None
        path = find_voicevox(cfg)
        if not path:
            return None
        try:
            status, _, _ = http("GET", tts["voicevoxUrl"].rstrip("/") + "/version", None, {}, timeout=3)
            if status == 200:
                return None  # もう動いている
        except UpstreamError:
            pass
        r = launch_voicevox(path, tts["voicevoxUrl"])
        with self.extras_lock:
            self.extras["voicevox"]["launched"] = {**r, "t": now_iso(), "auto": True}
        self.state.add_request({"route": "voicevox", "ok": r["ok"], "where": "this_pc", "reply": r["message"]})
        return r

    def start_background(self):
        def loop():
            try:
                self.maybe_launch_voicevox()
            except Exception as e:  # 起動を止めない
                self.state.add_request({"route": "voicevox", "ok": False, "error": f"{type(e).__name__}: {e}"})
            while not self.stop.is_set():
                try:
                    self.refresh_health()
                except Exception as e:  # 自己チェックが落ちても続ける
                    self.state.add_request({"route": "selfcheck", "ok": False, "error": f"{type(e).__name__}: {e}"})
                self.stop.wait(SELF_CHECK_SECONDS)
        threading.Thread(target=loop, daemon=True, name="self-check").start()

    def log_event_to_disk(self, ev: dict):
        if not self.config.get()["privacy"].get("logEvents"):
            return
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            day = datetime.now().strftime("%Y-%m-%d")
            with open(os.path.join(DATA_DIR, f"events-{day}.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except OSError:
            pass  # ディスクに書けなくても Quest への返事は止めない

    def health_payload(self) -> dict:
        """GET /health。速く (LLM には聞かない)、秘密は入れない。Quest が LAN を探すときにも使う。"""
        cfg = self.config.get()
        with self.state.lock:
            health = copy.deepcopy(self.state.health)
        cur = self.llm.current(cfg)
        return {"ok": True, "app": "cohabit-pc", "version": VERSION, "port": self.port, "time": now_iso(),
                "llm": {"backend": cur["backend"], "ok": cur["ok"], "label": cur["label"], "where": cur["where"],
                        "model": cur["model"]},
                "inference": inference_summary(cfg, profile_of(cfg, cur["backend"])),
                "backends": {k: {kk: vv for kk, vv in v.items() if kk in ("ok", "detail", "ms")}
                             for k, v in health.items() if isinstance(v, dict) and k != "llmBackends"},
                "allowImages": bool(cfg["privacy"].get("allowImages"))}

    def self_check_items(self, cfg: dict) -> list:
        """画面の「自己チェック」: 緑 / 赤と、直し方の 1 行。"""
        with self.state.lock:
            h = copy.deepcopy(self.state.health)
        with self.extras_lock:
            extras = copy.deepcopy(self.extras)
        items = []
        names = {"lan_ollama": "LLM", "cloud_api": "LLM", "local_qwen": "LLM"}
        primary = llm_profile(cfg)["backend"]
        found = h.get("llmBackends") or {}
        for bid in [primary] + [i for i in llm_backend_ids(cfg) if i != primary]:
            v = found.get(bid)
            if v is None:
                continue
            prof = profile_of(cfg, bid)
            role = "第一候補" if bid == primary else "予備"
            items.append({"id": "llm." + bid, "name": f"{names.get(bid, 'LLM')} ({role}): {prof['label']}", "ok": v.get("ok"),
                          "detail": v.get("detail"), "hint": v.get("hint") or "", "where": where_of(prof["baseUrl"])})
        for key, name in (("tts", "読み上げ (VOICEVOX)" if cfg["tts"]["engine"] == "voicevox" else "読み上げ"),
                          ("stt", "音声認識"), ("lev", "判断 (Lev)")):
            v = h.get(key)
            if v is None:
                continue
            item = {"id": key, "name": name, "ok": v.get("ok"), "detail": v.get("detail"), "hint": v.get("hint") or ""}
            if key == "lev" and not (cfg["lev"].get("url") or ""):
                item["ok"] = None  # 使わない設定は赤にしない
            if key == "tts" and not v.get("ok") and extras["voicevox"]["path"] and cfg["tts"]["engine"] == "voicevox":
                item["action"] = "voicevox"
            items.append(item)
        fw = extras["firewall"]
        if fw.get("active"):
            items.append({"id": "firewall", "name": f"ファイアウォール ({fw['kind']})", "ok": None,
                          "detail": "有効。Quest・タブレットが届かないときは LAN からの許可を足す",
                          "hint": firewall_manual(fw["kind"], self.port or 8000,
                                                  [n for n in (subnet24(a) for a in lan_addresses()) if n]),
                          "action": "firewall"})
        return items


def strip_large(value, depth=0):
    """記録に画像や音声 (長い base64) を入れない。"""
    if depth > 6:
        return None
    if isinstance(value, dict):
        return {k: strip_large(v, depth + 1) for k, v in value.items() if k not in ("image", "wav", "base64")}
    if isinstance(value, list):
        return [strip_large(v, depth + 1) for v in value[:100]]
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "…"
    return value


def text_of_messages(messages) -> tuple[str, list[str]]:
    texts, images = [], []
    for m in messages or []:
        content = m.get("content")
        if isinstance(content, str):
            texts.append(f"[{m.get('role')}] {content}")
        elif isinstance(content, list):
            for part in content:
                if part.get("type") == "text":
                    texts.append(f"[{m.get('role')}] {part.get('text', '')}")
                elif part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url", "")
                    images.append(url)
    return "\n".join(texts), images


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"CohabitPC/{VERSION}"
        protocol_version = "HTTP/1.1"
        timeout = 300  # 何も来ない keep-alive の接続は 5 分で閉じる (スレッドを溜めない)

        # ---- 返す ----

        def log_message(self, fmt, *args):
            pass  # 画面とメトリクスに出すので、端末には出さない

        def send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
            try:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionError, socket.timeout, OSError):
                self.close_connection = True  # 相手が先に切った (Quest の時間切れなど)。こちらは何もしない

        def json(self, status: int, payload):
            self.send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def error_json(self, status: int, message: str):
            self.json(status, {"error": {"message": message, "type": "cohabit_pc"}})

        def body(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError as e:
                raise UpstreamError(400, "bad Content-Length") from e
            if length < 0 or length > 40 * 1024 * 1024:
                raise UpstreamError(413, "request too large")
            return self.rfile.read(length) if length else b""

        def body_json(self):
            raw = self.body()
            try:
                return json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError as e:
                raise UpstreamError(400, f"invalid JSON: {e}") from e

        # ---- 誰からか ----

        def is_local(self) -> bool:
            host = self.client_address[0]
            return host in ("127.0.0.1", "::1") or host.startswith("127.") or host == "::ffff:127.0.0.1"

        def quest_allowed(self) -> bool:
            token = app.config.get().get("token") or ""
            if token and self.headers.get("X-Cohabit-Token") != token and not self.is_local():
                self.error_json(401, "missing or wrong X-Cohabit-Token")
                return False
            if not self.is_local():
                app.state.seen_quest(self.client_address[0])
            return True

        # ---- 振り分け ----

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path, _, query = self.path.partition("?")
            q = urllib.parse.parse_qs(query)
            if path == "/tablet" or path.startswith("/tablet/"):
                return app.tablet.handle(self, self.command)
            try:
                if path == "/":
                    if not self.is_local():
                        return self.send(200, "Cohabit PC is running. Open the dashboard on the PC itself.\n".encode(), "text/plain; charset=utf-8")
                    with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                        return self.send(200, f.read(), "text/html; charset=utf-8")
                if path == "/health":
                    # token が無くても答える (Quest が LAN からこの PC を探すため)。秘密は入っていない
                    if not self.is_local():
                        app.state.seen_quest(self.client_address[0])
                    return self.json(200, app.health_payload())
                if path == "/metrics":
                    if not self.quest_allowed():
                        return
                    return self.json(200, app.metrics.snapshot())
                if path.startswith("/api/"):
                    if not self.is_local():
                        return self.error_json(403, "the dashboard API is only for this PC")
                    return self.api_get(path, q)
                self.error_json(404, f"no {path}")
            except UpstreamError as e:
                self.error_json(e.status, str(e))
            except Exception as e:  # 1 つの要求で PC アプリを落とさない
                self.error_json(500, f"{type(e).__name__}: {e}")

        def do_POST(self):
            path = self.path.partition("?")[0]
            if path == "/tablet" or path.startswith("/tablet/"):
                return app.tablet.handle(self, "POST")
            t0 = time.time()
            ok = False
            known = path in QUEST_POST_ROUTES
            try:
                if path.startswith("/api/"):
                    if not self.is_local():
                        return self.error_json(403, "the dashboard API is only for this PC")
                    return self.api_post(path)
                if not self.quest_allowed():
                    return
                if path == "/v1/chat/completions":
                    ok = self.chat()
                elif path == "/v1/audio/transcriptions":
                    ok = self.transcriptions()
                elif path == "/v1/audio/speech":
                    ok = self.speech()
                elif path == "/decision/evaluate":
                    ok = self.decision()
                elif path == "/observe":
                    ok = self.observe()
                else:
                    return self.error_json(404, f"no {path}")
            except UpstreamError as e:
                app.state.add_request({"route": path, "ok": False, "error": str(e)})
                if e.kind == "llm_down":
                    # Quest はこの say をそのまま言えばよい (どの LLM にも届かないとき)
                    self.json(e.status, {"error": {"message": str(e), "type": "cohabit_pc", "code": "llm_down"},
                                         "say": dict(LLM_DOWN_SAY)})
                else:
                    self.error_json(e.status, str(e))
            except Exception as e:  # 1 つの要求で PC アプリを落とさない
                app.state.add_request({"route": path, "ok": False, "error": f"{type(e).__name__}: {e}"})
                self.error_json(500, f"{type(e).__name__}: {e}")
            finally:
                if known and path != "/observe":  # 無い道 (古い Quest の呼び出しなど) は数えない
                    app.metrics.record(path, (time.time() - t0) * 1000, ok)

        # ---- Quest から ----

        def chat(self) -> bool:
            cfg = app.config.get()
            body = self.body_json()
            if not isinstance(body, dict):
                raise UpstreamError(400, "body must be a JSON object")
            prompt_text, images = text_of_messages(body.get("messages"))
            if images and not cfg["privacy"].get("allowImages"):
                app.state.add_request({"route": "vision", "ok": False, "error": "images are switched off on the PC", "prompt": prompt_text[:400]})
                self.error_json(403, "images are switched off on the PC (privacy.allowImages)")
                return False
            requested = body.get("model") or "cohabit-chat"
            explicit = requested not in MODEL_ALIASES  # 名前を指定されたら第一候補だけ (予備には同じモデルが無い)
            if explicit:
                body["model"] = requested
            body.pop("stream", None)
            image_ids = []
            if images and cfg["privacy"].get("previewImages"):
                image_ids = [i for i in (app.state.add_image(u, "vision") for u in images) if i]
            t0 = time.time()
            try:
                r = app.llm.complete(cfg, body, vision=bool(images), explicit_model=explicit)
            except UpstreamError as e:
                app.state.add_request({"route": "vision" if images else "chat", "ok": False, "status": e.status,
                                       "ms": round((time.time() - t0) * 1000), "prompt": prompt_text[-1200:], "error": str(e),
                                       "images": image_ids})
                raise
            app.state.add_request({
                "route": "vision" if images else "chat", "ok": r["status"] == 200, "status": r["status"],
                "model": r["model"], "where": r["where"], "backend": r["backend"], "label": r["label"],
                "ms": round((time.time() - t0) * 1000), "cached": bool(r.get("cached")),
                "prompt": prompt_text[-1200:], "reply": (r["content"] or r["raw"][:300].decode("utf-8", "replace"))[:1200],
                "images": image_ids, "tried": r.get("tried") or [],
            })
            self.send(r["status"], r["raw"], "application/json; charset=utf-8", {"X-Cohabit-Backend": r["backend"]})
            return r["status"] == 200

        def transcriptions(self) -> bool:
            """音声認識。画面の「今 AI がしていること」に、届いた回数・最後に聞こえた文字か失敗の理由を出す。"""
            t0 = time.time()
            app.state.note_stt(started=True)
            try:
                ok = self._transcriptions()
            except Exception as e:
                app.state.note_stt(ok=False, error=str(e)[:200], ms=round((time.time() - t0) * 1000))
                raise
            return ok

        def _transcriptions(self) -> bool:
            cfg = app.config.get()
            stt = cfg["stt"]
            fields = parse_multipart(self.body(), self.headers.get("Content-Type", ""))
            audio = fields.get("file")
            if not isinstance(audio, dict):
                raise UpstreamError(400, "no file field")
            language = fields.get("language") if isinstance(fields.get("language"), str) else "ja"
            t0 = time.time()
            if stt["engine"] == "faster-whisper":
                text = transcribe_local(cfg, audio["data"], language)
                status = 200
                where = "this_pc"
            else:
                fields["model"] = stt["model"]
                fields.setdefault("language", language)
                payload, ctype = encode_multipart(fields)
                status, _, raw = http("POST", stt["baseUrl"].rstrip("/") + "/audio/transcriptions", payload,
                                      {"Content-Type": ctype, **bearer(key_for(stt, cfg))}, timeout=60)
                where = where_of(stt["baseUrl"])
                text = None
                if status == 200:
                    try:
                        text = json.loads(raw.decode("utf-8")).get("text", "")
                    except (ValueError, UnicodeDecodeError):
                        text = raw.decode("utf-8", "replace")
                else:
                    app.state.add_request({"route": "stt", "ok": False, "status": status, "where": where, "error": raw[:300].decode("utf-8", "replace")})
                    app.state.note_stt(ok=False, error=f"HTTP {status}: {raw[:160].decode('utf-8', 'replace')}",
                                       ms=round((time.time() - t0) * 1000))
                    self.send(status, raw, "application/json; charset=utf-8")
                    return False
            seconds = max(0.0, (len(audio["data"]) - 44) / 32000)  # 16 kHz・16 bit・モノラル
            app.state.add_request({"route": "stt", "ok": True, "where": where, "ms": round((time.time() - t0) * 1000),
                                   "prompt": f"音声 {seconds:.1f} 秒 (保存しない)", "reply": text})
            app.state.note_stt(ok=True, text=text or "", seconds=round(seconds, 1), ms=round((time.time() - t0) * 1000))
            self.json(200, {"text": text or ""})
            return True

        def speech(self) -> bool:
            cfg = app.config.get()
            tts = cfg["tts"]
            body = self.body_json()
            text = str(body.get("input") or "").strip()
            if not text:
                raise UpstreamError(400, "no input text")
            t0 = time.time()
            if tts["engine"] == "voicevox":
                wav = speak_voicevox(cfg, text)
                where = where_of(tts["voicevoxUrl"])
                ctype = "audio/wav"
                status = 200
            else:
                payload = {**body, "model": tts["model"], "voice": tts["voice"], "response_format": body.get("response_format") or "wav"}
                status, headers, wav = http("POST", tts["baseUrl"].rstrip("/") + "/audio/speech", json.dumps(payload).encode(),
                                            {"Content-Type": "application/json", **bearer(key_for(tts, cfg))}, timeout=60)
                where = where_of(tts["baseUrl"])
                ctype = headers.get("Content-Type", "audio/wav")
            app.state.add_request({"route": "tts", "ok": status == 200, "status": status, "where": where,
                                   "ms": round((time.time() - t0) * 1000), "prompt": text})
            self.send(status, wav, ctype)
            return status == 200

        def decision(self) -> bool:
            cfg = app.config.get()
            body = self.body_json()
            t0 = time.time()
            result = lev_evaluate(cfg, body)
            result["ms"] = round((time.time() - t0) * 1000)
            app.state.add_request({"route": "lev", "ok": True, "where": where_of(cfg["lev"]["url"]), "ms": result["ms"],
                                   "prompt": json.dumps({"state": body.get("state"), "candidates": body.get("candidates")}, ensure_ascii=False)[:1200],
                                   "reply": json.dumps({k: round(float(v), 3) for k, v in result["scores"].items()}, ensure_ascii=False)})
            self.json(200, result)
            return True

        def observe(self) -> bool:
            body = self.body_json()
            events = body.get("events") if isinstance(body, dict) and "events" in body else [body]
            stored = 0
            for raw in events or []:
                if not isinstance(raw, dict):
                    continue
                ev = strip_large({
                    "event_id": raw.get("event_id") or f"evt_{uuid.uuid4().hex[:10]}",
                    "session_id": raw.get("session_id"),
                    "observed_at": raw.get("observed_at"),
                    "received_at": now_iso(),
                    "sensor": raw.get("sensor"),
                    "type": raw.get("type") or "event",
                    "payload": raw.get("payload"),
                    "confidence": raw.get("confidence"),
                    "uncertainty": raw.get("uncertainty"),
                    "ttl_ms": raw.get("ttl_ms"),
                    "source_frame_id": raw.get("source_frame_id"),
                    "consent_scope": raw.get("consent_scope"),
                })
                ev = app.state.add_event(ev)
                payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
                # by: "pc_teacher" は PC 自身の /ask・/answer・/check の返事を Quest が言ったもの: タブレットには流さない (二重になる)
                if (ev["type"] == "action" and payload.get("what") == "say" and payload.get("text")
                        and payload.get("by") != "pc_teacher"):
                    app.tablet.push_say(str(payload["text"]), payload.get("expression") or "neutral", "quest")
                if ev["type"] != "status":
                    app.log_event_to_disk(ev)
                stored += 1
            self.json(200, {"ok": True, "stored": stored})
            return True

        # ---- 画面から ----

        def api_get(self, path: str, q: dict):
            if path == "/api/state":
                after_event = int((q.get("afterEvent") or ["0"])[0] or 0)
                after_request = int((q.get("afterRequest") or ["0"])[0] or 0)
                cfg = app.config.get()
                with app.state.lock:
                    events = [e for e in app.state.events if e["seq"] > after_event][-300:]
                    requests = [r for r in app.state.requests if r["seq"] > after_request][-100:]
                    images = [{k: v for k, v in i.items() if k != "data"} for i in app.state.images]
                    quest = copy.deepcopy(app.state.quest)
                    health = copy.deepcopy(app.state.health)
                quest["secondsSinceSeen"] = round(time.time() - quest["lastSeen"], 1) if quest["lastSeen"] else None
                with app.state.lock:
                    stt_state = dict(app.state.stt)
                stt_state["secondsSince"] = round(time.time() - stt_state["lastAt"], 1) if stt_state["lastAt"] else None
                # Quest の今の段 (status.stage: listening / transcribing / thinking:<目的> / speaking / error)
                st = quest.get("status") if isinstance(quest.get("status"), dict) else {}
                quest["stage"] = st.get("stage")
                quest["stageSecondsAgo"] = round(time.time() - quest["statusAt"], 1) if quest.get("statusAt") else None
                port = self.server.server_address[1]
                llm_status = app.llm.status(cfg)
                with app.extras_lock:
                    extras = copy.deepcopy(app.extras)
                ips = lan_addresses()
                return self.json(200, {
                    "version": VERSION, "bootId": app.boot_id, "config": masked(cfg), "health": health,
                    "inference": inference_summary(cfg, profile_of(cfg, llm_status["backend"])),
                    "metrics": app.metrics.snapshot(), "quest": quest, "events": events, "requests": requests, "images": images,
                    "port": port, "ips": ips, "addresses": [f"http://{ip}:{port}" for ip in ips],
                    "logFiles": sorted(os.listdir(DATA_DIR)) if os.path.isdir(DATA_DIR) else [],
                    "tablet": app.tablet.dashboard_state(port),
                    "llm": llm_status, "stt": stt_state, "selfCheck": app.self_check_items(cfg), "checkedAt": health.get("checkedAt"),
                    "autostart": autostart_status(), "firewall": extras["firewall"],
                    "voicevox": {"path": extras["voicevox"]["path"], "launched": extras["voicevox"]["launched"]},
                })
            m = re.match(r"^/api/image/([0-9a-f]+)$", path)
            if m:
                with app.state.lock:
                    found = next((i for i in app.state.images if i["id"] == m.group(1)), None)
                if not found:
                    return self.error_json(404, "image is gone (only the last few are kept, in memory)")
                return self.send(200, found["data"], found["mime"])
            self.error_json(404, f"no {path}")

        def api_post(self, path: str):
            body = self.body_json()
            if path == "/api/config":
                cfg = app.config.update(body or {})
                threading.Thread(target=app.refresh_health, daemon=True).start()
                return self.json(200, {"ok": True, "config": masked(cfg)})
            if path == "/api/test":
                return self.json(200, self.run_test(str(body.get("target") or "")))
            if path == "/api/selfcheck":
                app.refresh_health()
                return self.json(200, {"ok": True, "selfCheck": app.self_check_items(app.config.get())})
            if path == "/api/autostart":
                action = body.get("action")
                if action == "install":
                    return self.json(200, {**autostart_install(), "status": autostart_status()})
                if action == "remove":
                    return self.json(200, {**autostart_remove(), "status": autostart_status()})
                return self.error_json(400, 'action must be "install" or "remove"')
            if path == "/api/voicevox/launch":
                cfg = app.config.get()
                vv = find_voicevox(cfg)
                if not vv:
                    return self.json(200, {"ok": False, "message": "VOICEVOX が見つからない。設定の「VOICEVOX の場所」に実行ファイルを入れる"})
                r = launch_voicevox(vv, cfg["tts"].get("voicevoxUrl") or "")
                with app.extras_lock:
                    app.extras["voicevox"]["launched"] = {**r, "t": now_iso(), "auto": False}
                return self.json(200, r)
            if path == "/api/firewall/allow":
                return self.json(200, firewall_allow(self.server.server_address[1], lan_addresses()))
            if path == "/api/delete":
                what = body.get("what")
                removed = []
                with app.state.lock:
                    if what in ("events", "all"):
                        app.state.events.clear()
                        app.state.requests.clear()
                        removed.append("events")
                    if what in ("images", "all"):
                        app.state.images.clear()
                        removed.append("images")
                if what in ("logs", "all") and os.path.isdir(DATA_DIR):
                    for name in os.listdir(DATA_DIR):
                        if name.startswith("events-") and name.endswith(".jsonl"):
                            os.remove(os.path.join(DATA_DIR, name))
                    removed.append("logs")
                if what in ("learning", "all"):
                    app.tablet.delete_learning()
                    removed.append("learning")
                if what == "all":
                    app.metrics.reset()
                return self.json(200, {"ok": True, "removed": removed})
            self.error_json(404, f"no {path}")

        def run_test(self, target: str) -> dict:
            cfg = app.config.get()
            t0 = time.time()
            try:
                if target == "chat":
                    r = app.llm.complete(cfg, {"max_tokens": 30, "messages": [{"role": "user", "content": "「つながったよ」とだけ言って"}]},
                                         cache=False)
                    detail = r["content"] if r["status"] == 200 else r["raw"][:300].decode("utf-8", "replace")
                    return {"ok": r["status"] == 200, "detail": f"{r['label']}: {detail}", "backend": r["backend"],
                            "ms": round((time.time() - t0) * 1000)}
                if target == "tts":
                    if cfg["tts"]["engine"] == "voicevox":
                        wav = speak_voicevox(cfg, "つながったよ")
                    else:
                        t = cfg["tts"]
                        status, _, wav = http("POST", t["baseUrl"].rstrip("/") + "/audio/speech",
                                              json.dumps({"model": t["model"], "voice": t["voice"], "input": "つながったよ", "response_format": "wav"}).encode(),
                                              {"Content-Type": "application/json", **bearer(key_for(t, cfg))}, timeout=60)
                        if status != 200:
                            return {"ok": False, "detail": wav[:300].decode("utf-8", "replace"), "ms": round((time.time() - t0) * 1000)}
                    return {"ok": True, "detail": f"{len(wav)} bytes", "audio": base64.b64encode(wav).decode(), "ms": round((time.time() - t0) * 1000)}
                if target == "lev":
                    r = lev_evaluate(cfg, {"kind": "study", "candidates": ["SILENT", "ASK", "HINT"],
                                           "state": {"studying": True, "observed": True, "writing": False, "stillSeconds": 40,
                                                     "secondsWithoutProgress": 90, "problemKnown": True, "stuck": 0.8}})
                    return {"ok": True, "detail": r, "ms": round((time.time() - t0) * 1000)}
                if target == "stt":
                    result = check_backends(cfg).get("stt", {})
                    return {"ok": result.get("ok", False), "detail": result.get("detail"), "ms": round((time.time() - t0) * 1000)}
                return {"ok": False, "detail": f"unknown test {target}"}
            except UpstreamError as e:
                return {"ok": False, "detail": str(e), "ms": round((time.time() - t0) * 1000)}
            except Exception as e:
                return {"ok": False, "detail": f"{type(e).__name__}: {e}", "ms": round((time.time() - t0) * 1000)}

    return Handler


def serve(port: int | None = None, host: str = "0.0.0.0", config_path: str = CONFIG_PATH, background: bool = True):
    config = Config(config_path)
    app = App(config)
    port = port if port is not None else int(config.get().get("port") or 8000)
    server = Server((host, port), make_handler(app))
    app.port = server.server_address[1]
    if background:
        app.start_background()
    return server, app


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64  # タブレット・Quest・画面が同時に来ても受け口があふれない

    def handle_error(self, request, client_address):
        # 相手が切っただけのものは端末に出さない。ほかは 1 行だけ (スレッドは要求ごと。サーバーは止まらない)
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionError, socket.timeout)):
            return
        print(f"[cohabit-pc] {client_address[0]}: {type(exc).__name__}: {exc}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Cohabit PC — the Quest app's PC side (LLM / voice / Lev / verification relay + dashboard)")
    parser.add_argument("--port", type=int, default=None, help="port (default: config.json port, 8000)")
    parser.add_argument("--host", default="0.0.0.0", help="address to listen on (default 0.0.0.0 = the whole LAN)")
    parser.add_argument("--config", default=CONFIG_PATH, help="config file (created when you save in the dashboard)")
    args = parser.parse_args()

    try:
        server, app = serve(args.port, args.host, args.config)
    except OSError as e:
        port = args.port or 8000
        print(f"Cohabit PC: ポート {port} を開けない ({e})。", file=sys.stderr)
        print(f"  もう動いているかもしれない (自動起動など)。この PC のブラウザで http://127.0.0.1:{port}/ を開いてみる。", file=sys.stderr)
        return 1
    port = server.server_address[1]
    print(f"Cohabit PC {VERSION}")
    print(f"  画面 (この PC のブラウザで開く): http://127.0.0.1:{port}/")
    for ip in lan_addresses():
        print(f"  Quest に入れるアドレス: http://{ip}:{port}   (Quest は LAN から自動で探す)")
        print(f"  タブレットで開く: http://{ip}:{port}/tablet/")
    prof = llm_profile(app.config.get())
    print(f"  LLM: {prof['label']} ({prof['chatModel']})。届かなければ予備へ。様子は画面の「自己チェック」")
    if app.config.load_error:
        print(f"  注意: config.json が読めなかったので既定の設定で動いています ({app.config.load_error})", file=sys.stderr)
    print("  止めるには Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop.set()
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
