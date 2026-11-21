"""
Cohabit PC の確認。偽の上流 (OpenAI 互換・VOICEVOX・Lev) を立てて、Quest と同じ形で呼ぶ。
    python -m unittest test_cohabit_pc.py      (標準ライブラリだけ。ネットにも出ない)
自動起動・ファイアウォール・VOICEVOX は、偽の HOME と偽のコマンドで確かめる (本物の systemctl / pkexec / VOICEVOX は動かさない)。
"""

import base64
import json
import os
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cohabit_pc as pc

DEAD = "http://127.0.0.1:9/v1"  # 誰も聞いていないポート (すぐ断られる)
SEEN = []  # 上流に届いたもの


class FakeUpstream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, status, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/v1/models":
            return self.reply(200, {"data": []})
        if self.path == "/version":
            return self.reply(200, b'"0.21.1"')
        if self.path == "/health":
            return self.reply(200, {"status": "ok", "model": "Qwen/Qwen3.5-4B-Base"})
        self.reply(404, {})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        SEEN.append({"path": self.path, "headers": dict(self.headers), "body": body})
        if self.path == "/v1/chat/completions":
            req = json.loads(body)
            return self.reply(200, {"choices": [{"message": {"role": "assistant", "content": f"model={req['model']}"}}]})
        if self.path == "/v1/audio/transcriptions":
            return self.reply(200, {"text": "勉強始める"})
        if self.path == "/v1/audio/speech":
            return self.reply(200, b"RIFFfakewav", "audio/wav")
        if self.path.startswith("/audio_query"):
            return self.reply(200, {"speedScale": 1.0, "pitchScale": 0.0, "accent_phrases": []})
        if self.path.startswith("/synthesis"):
            return self.reply(200, b"RIFFvoicevox", "audio/wav")
        if self.path == "/v1/systemone":
            req = json.loads(body)
            options = list(req["questions"]["action"]["criteria"].keys())
            probs = {o: (0.7 if o == "ASK" else 0.3 / (len(options) - 1)) for o in options}
            return self.reply(200, {"model": "lev", "answers": {"action": {"type": "choice", "choice": "ASK", "probabilities": probs, "confidence": 0.7}},
                                    "usage": {"input_tokens": 100}})
        self.reply(404, {})


def start(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class CohabitPcTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream))
        up = f"http://127.0.0.1:{cls.upstream.server_address[1]}"
        cls.tmp = tempfile.mkdtemp()
        cfg_path = os.path.join(cls.tmp, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({
                # 予備には回さない (既定の lan_ollama は本物の LAN の PC。テストはネットに出ない)
                "llm": {"backend": "cloud_api", "fallback": False, "lan_ollama": {"baseUrl": DEAD}, "local_qwen": {"baseUrl": DEAD},
                        "cloud_api": {"baseUrl": up + "/v1", "apiKey": "sk-test", "chatModel": "chat-m", "visionModel": "vision-m"}},
                "stt": {"engine": "openai", "baseUrl": up + "/v1", "model": "stt-m"},
                "tts": {"engine": "voicevox", "voicevoxUrl": up, "speaker": 8},
                "lev": {"url": up},
                "privacy": {"logEvents": False},
            }, f)
        pc.DATA_DIR = os.path.join(cls.tmp, "data")
        cls.server, cls.app = pc.serve(0, "127.0.0.1", cfg_path, background=False)
        start(cls.server)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        for s in (cls.server, cls.upstream):
            s.shutdown()
            s.server_close()

    def call(self, path, payload=None, data=None, ctype=None):
        if payload is not None:
            data, ctype = json.dumps(payload).encode(), "application/json"
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET",
                                     headers={"Content-Type": ctype} if ctype else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers.get("Content-Type"), e.read()

    def test_chat_uses_pc_models_and_key(self):
        status, _, body = self.call("/v1/chat/completions", {"model": "cohabit-chat", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "model=chat-m")
        self.assertEqual(SEEN[-1]["headers"].get("Authorization"), "Bearer sk-test")

    def test_local_llm_is_asked_not_to_think(self):
        # クラウドには送らない (gpt-4.1-mini は reasoning_effort "none" を断る)
        self.call("/v1/chat/completions", {"model": "cohabit-chat", "messages": [{"role": "user", "content": "hi"}]})
        self.assertNotIn("reasoning_effort", json.loads(SEEN[-1]["body"]))
        llm = self.app.config.data["llm"]
        llm["backend"] = "local_qwen"
        llm["local_qwen"] = {**llm["cloud_api"], "label": "local"}
        try:
            self.call("/v1/chat/completions", {"model": "cohabit-chat", "messages": [{"role": "user", "content": "hi"}]})
            sent = json.loads(SEEN[-1]["body"])
            self.assertEqual(sent["reasoning_effort"], "none")
            self.assertEqual(sent["chat_template_kwargs"], {"enable_thinking": False})
        finally:
            llm["backend"] = "cloud_api"
        # クラウドでも "thinking": false なら reasoning_effort だけ (Groq の Qwen など)
        llm["cloud_api"]["thinking"] = False
        try:
            self.call("/v1/chat/completions", {"model": "cohabit-chat", "messages": [{"role": "user", "content": "hi"}]})
            sent = json.loads(SEEN[-1]["body"])
            self.assertEqual(sent["reasoning_effort"], "none")
            self.assertNotIn("chat_template_kwargs", sent)
        finally:
            llm["cloud_api"].pop("thinking")

    def test_image_goes_to_vision_model_and_is_previewed_in_memory(self):
        img = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8jpeg").decode()
        status, _, body = self.call("/v1/chat/completions", {"model": "cohabit-vision", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "read"}, {"type": "image_url", "image_url": {"url": img}}]}]})
        self.assertEqual(status, 200)
        self.assertIn("model=vision-m", body.decode())
        self.assertTrue(any(i["data"] == b"\xff\xd8jpeg" for i in self.app.state.images))

    def test_images_can_be_refused(self):
        self.app.config.data["privacy"]["allowImages"] = False
        try:
            img = "data:image/jpeg;base64,AAAA"
            status, _, _ = self.call("/v1/chat/completions", {"model": "cohabit-vision", "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": img}}]}]})
            self.assertEqual(status, 403)
        finally:
            self.app.config.data["privacy"]["allowImages"] = True

    def test_transcription_is_relayed_as_multipart(self):
        payload, ctype = pc.encode_multipart({"model": "whisper-1", "language": "ja",
                                              "file": {"filename": "speech.wav", "type": "audio/wav", "data": b"RIFF" + bytes(64)}})
        status, _, body = self.call("/v1/audio/transcriptions", data=payload, ctype=ctype)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["text"], "勉強始める")
        relayed = pc.parse_multipart(SEEN[-1]["body"], SEEN[-1]["headers"]["Content-Type"])
        self.assertEqual(relayed["model"], "stt-m")
        self.assertEqual(relayed["file"]["data"], b"RIFF" + bytes(64))

    def test_stt_activity_is_shown(self):
        st = json.loads(self.call("/api/state")[2])
        before = st["stt"]["count"]
        payload, ctype = pc.encode_multipart({"file": {"filename": "speech.wav", "type": "audio/wav", "data": b"RIFF" + bytes(64)}})
        self.call("/v1/audio/transcriptions", data=payload, ctype=ctype)
        st = json.loads(self.call("/api/state")[2])["stt"]
        self.assertEqual(st["count"], before + 1)
        self.assertEqual((st["lastOk"], st["lastText"], st["inFlight"]), (True, "勉強始める", 0))
        self.assertIsNotNone(st["secondsSince"])
        # 届いたが文字にできなかった
        self.call("/v1/audio/transcriptions", data=b"x", ctype="multipart/form-data; boundary=zz")
        st = json.loads(self.call("/api/state")[2])["stt"]
        self.assertEqual((st["count"], st["lastOk"]), (before + 2, False))
        self.assertTrue(st["lastError"])
        # Quest の段は /observe の status から
        self.call("/observe", {"events": [{"type": "status", "payload": {"stage": "thinking:hint"}}]})
        q = json.loads(self.call("/api/state")[2])["quest"]
        self.assertEqual(q["stage"], "thinking:hint")

    def test_speech_through_voicevox(self):
        status, ctype, body = self.call("/v1/audio/speech", {"model": "tts-1", "input": "こんにちは", "response_format": "wav"})
        self.assertEqual((status, ctype, body), (200, "audio/wav", b"RIFFvoicevox"))
        synth = [s for s in SEEN if s["path"].startswith("/synthesis")][-1]
        self.assertIn("speaker=8", synth["path"])
        self.assertEqual(json.loads(synth["body"])["speedScale"], 1.1)

    def test_lev_scores(self):
        status, _, body = self.call("/decision/evaluate", {"state": {"stuck": 0.8}, "candidates": ["SILENT", "OBSERVE", "ASK"]})
        self.assertEqual(status, 200)
        r = json.loads(body)
        self.assertEqual(r["source"], "lev")
        self.assertEqual(max(r["scores"], key=r["scores"].get), "ASK")
        sent = json.loads([s for s in SEEN if s["path"] == "/v1/systemone"][-1]["body"])
        self.assertEqual(sent["questions"]["action"]["type"], "choice")
        self.assertEqual(set(sent["questions"]["action"]["criteria"]), {"SILENT", "OBSERVE", "ASK"})

    def test_lev_off_is_503(self):
        self.app.config.data["lev"]["url"] = ""
        try:
            status, _, _ = self.call("/decision/evaluate", {"state": {}, "candidates": ["SILENT", "ASK"]})
            self.assertEqual(status, 503)
        finally:
            self.app.config.data["lev"]["url"] = f"http://127.0.0.1:{self.upstream.server_address[1]}"

    def test_observe_strips_images(self):
        status, _, _ = self.call("/observe", {"events": [
            {"type": "decision", "payload": {"chosen": "ASK", "image": "A" * 5000}},
            {"type": "status", "payload": {"mode": "study"}},
        ]})
        self.assertEqual(status, 200)
        decision = [e for e in self.app.state.events if e["type"] == "decision"][-1]
        self.assertNotIn("image", decision["payload"])
        self.assertEqual(self.app.state.quest["status"], {"mode": "study"})

    def test_removed_routes_are_quiet_404(self):
        # 消した道 (古い Quest が呼ぶかもしれない): 404 を返すだけで、記録にも遅延の数にも残さない
        n = len(self.app.state.requests)
        for path, payload in (("/control/next?after=0", None), ("/study/verify", {"equation": "x = 1"}),
                              ("/api/control", {"command": "study.start"})):
            self.assertEqual(self.call(path, payload)[0], 404, path)
        self.assertEqual(len(self.app.state.requests), n)
        routes = self.app.metrics.snapshot()["routes"]
        self.assertNotIn("/study/verify", routes)
        self.assertNotIn("/control/next", routes)

    def test_health_says_where_inference_runs(self):
        status, _, body = self.call("/health")
        self.assertEqual(status, 200)
        h = json.loads(body)
        self.assertEqual(h["inference"]["parts"]["tts"]["where"], "this_pc")
        self.assertIn("会話・ノート", h["inference"]["text"])

    def test_dashboard_and_state(self):
        status, ctype, body = self.call("/")
        self.assertEqual(status, 200)
        self.assertIn("Cohabit PC", body.decode())
        status, _, body = self.call("/api/state")
        state = json.loads(body)
        self.assertEqual(state["config"]["llm"]["cloud_api"]["apiKey"], pc.MASK)
        self.assertIn("bootId", state)

    def test_config_keeps_masked_key(self):
        status, _, body = self.call("/api/config", {"llm": {"cloud_api": {"apiKey": pc.MASK, "chatModel": "chat-m"}}})
        self.assertEqual(status, 200)
        self.assertEqual(self.app.config.get()["llm"]["cloud_api"]["apiKey"], "sk-test")

    def test_where_of(self):
        self.assertEqual(pc.where_of("http://127.0.0.1:8001"), "this_pc")
        self.assertEqual(pc.where_of("http://192.168.0.5:8080/v1"), "lan")
        self.assertEqual(pc.where_of("https://api.openai.com/v1"), "cloud")

    def test_upstream_down_is_503_with_say_not_crash(self):
        self.app.config.data["llm"]["cloud_api"]["baseUrl"] = "http://127.0.0.1:9/v1"
        try:
            status, _, body = self.call("/v1/chat/completions", {"model": "cohabit-chat", "messages": []})
            self.assertEqual(status, 503)
            r = json.loads(body)
            self.assertEqual(r["error"]["code"], "llm_down")
            self.assertIn("cloud_api", r["error"]["message"])
            self.assertTrue(r["say"]["text"])   # Quest はこれを言えばよい
            self.assertEqual(self.call("/health")[0], 200)  # サーバーは生きている
        finally:
            self.app.config.data["llm"]["cloud_api"]["baseUrl"] = f"http://127.0.0.1:{self.upstream.server_address[1]}/v1"


# ---------------------------------------------------------------------------
# LLM の行き先 (第一候補 → 予備)・1 つずつ・同じ要求をまとめる
# ---------------------------------------------------------------------------


class Flaky(BaseHTTPRequestHandler):
    """第一候補のふり。mode で振る舞いを変える。"""
    mode = "ok"
    delay = 0.0
    content = "flaky"
    hits = 0
    active = 0
    max_active = 0
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def reply(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass

    def do_GET(self):
        self.reply(200, {"data": [{"id": "lan-m"}]})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        cls = Flaky
        with cls.lock:
            cls.hits += 1
            cls.active += 1
            cls.max_active = max(cls.max_active, cls.active)
        try:
            if cls.delay:
                time.sleep(cls.delay)
            if cls.mode == "ok":
                return self.reply(200, {"choices": [{"message": {"role": "assistant", "content": cls.content}}]})
            self.reply({"503": 503, "400": 400, "429": 429, "500": 500}[cls.mode], {"error": cls.mode})
        finally:
            with cls.lock:
                cls.active -= 1


class RouterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream))
        cls.flaky = start(ThreadingHTTPServer(("127.0.0.1", 0), Flaky))
        cls.up = f"http://127.0.0.1:{cls.upstream.server_address[1]}"
        cls.lan = f"http://127.0.0.1:{cls.flaky.server_address[1]}/v1"
        cls.tmp = tempfile.mkdtemp()
        cfg_path = os.path.join(cls.tmp, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({
                "llm": {"backend": "lan_ollama", "fallback": True, "connectTimeout": 1, "readTimeout": 5, "queueWait": 5,
                        "lan_ollama": {"baseUrl": cls.lan, "chatModel": "lan-m", "visionModel": "lan-m"},
                        "cloud_api": {"baseUrl": cls.up + "/v1", "apiKey": "sk-test", "chatModel": "chat-m", "visionModel": "vision-m"},
                        "local_qwen": {"baseUrl": DEAD}},
                "tts": {"engine": "voicevox", "voicevoxUrl": cls.up},
                "lev": {"url": ""}, "privacy": {"logEvents": False},
            }, f)
        pc.DATA_DIR = os.path.join(cls.tmp, "data")
        cls.server, cls.app = pc.serve(0, "127.0.0.1", cfg_path, background=False)
        start(cls.server)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        for s in (cls.server, cls.upstream, cls.flaky):
            s.shutdown()
            s.server_close()

    def setUp(self):
        Flaky.mode, Flaky.delay, Flaky.content = "ok", 0.0, "flaky"
        Flaky.hits = Flaky.active = Flaky.max_active = 0
        r = self.app.llm
        with r.lock:
            r.cache.clear()
            r.errors.clear()
            r.sticky = None
            r.last = None
        llm = self.app.config.data["llm"]
        llm["lan_ollama"]["baseUrl"] = self.lan
        llm["readTimeout"] = 5
        llm["fallback"] = True
        SEEN.clear()

    def chat(self, text="hi", model="cohabit-chat", headers=None):
        data = json.dumps({"model": model, "messages": [{"role": "user", "content": text}]}).encode()
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=data, method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, dict(r.headers), json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), json.loads(e.read() or b"null")

    def content(self, body):
        return body["choices"][0]["message"]["content"]

    def test_primary_answers_without_thinking(self):
        status, headers, body = self.chat()
        self.assertEqual((status, self.content(body), headers.get("X-Cohabit-Backend")), (200, "flaky", "lan_ollama"))
        self.assertEqual(SEEN, [])  # 予備には行かない

    def test_refused_primary_fails_over_and_sticks(self):
        self.app.config.data["llm"]["lan_ollama"]["baseUrl"] = DEAD
        status, headers, body = self.chat()
        self.assertEqual(status, 200)
        self.assertEqual(self.content(body), "model=chat-m")   # 予備 (cloud_api) のモデルに置き換わる
        self.assertEqual(headers.get("X-Cohabit-Backend"), "cloud_api")
        self.assertEqual(self.app.llm.sticky[0], "cloud_api")
        # しばらくは予備を先に (落ちた第一候補を毎回待たない)
        self.assertEqual(self.app.llm.chain(self.app.config.get())[0]["backend"], "cloud_api")
        st = json.loads(urllib.request.urlopen(self.base + "/api/state", timeout=5).read())
        self.assertEqual(st["llm"]["lastAnswered"]["backend"], "cloud_api")
        self.assertIn("lan_ollama", st["llm"]["errors"])
        # 第一候補が戻ったら (自己チェック) 戻す
        self.app.llm.primary_recovered("lan_ollama")
        self.assertEqual(self.app.llm.chain(self.app.config.get())[0]["backend"], "lan_ollama")

    def test_5xx_is_retried_once_then_fails_over(self):
        Flaky.mode = "503"
        status, headers, body = self.chat()
        self.assertEqual((status, headers.get("X-Cohabit-Backend")), (200, "cloud_api"))
        self.assertEqual(Flaky.hits, 2)

    def test_bad_request_is_not_failed_over(self):
        Flaky.mode = "400"
        status, _, _ = self.chat()
        self.assertEqual(status, 400)
        self.assertEqual(SEEN, [])

    def test_read_timeout_fails_over(self):
        Flaky.delay = 1.5
        self.app.config.data["llm"]["readTimeout"] = 0.5
        t0 = time.time()
        status, headers, _ = self.chat("slow")
        self.assertEqual((status, headers.get("X-Cohabit-Backend")), (200, "cloud_api"))
        self.assertLess(time.time() - t0, 1.4)
        self.assertIn("返事が来ない", self.app.llm.errors["lan_ollama"]["message"])

    def test_one_request_at_a_time_per_backend(self):
        Flaky.delay = 0.25
        results = []
        threads = [threading.Thread(target=lambda i=i: results.append(self.chat(f"q{i}")[0])) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(results, [200, 200, 200])
        self.assertEqual(Flaky.hits, 3)
        self.assertEqual(Flaky.max_active, 1)  # 27B のサーバーには 1 つずつ

    def bg(self, text="bg"):
        return self.app.llm.complete(self.app.config.get(), {"messages": [{"role": "user", "content": text}]},
                                     cache=False, priority="background", route="quest.check")

    def test_background_runs_when_idle_but_is_rejected_when_busy(self):
        self.assertEqual(self.bg()["status"], 200)
        Flaky.delay = 0.6
        t = threading.Thread(target=lambda: self.chat("talking"))
        t.start()
        time.sleep(0.2)
        t0 = time.time()
        with self.assertRaises(pc.UpstreamError) as cm:
            self.bg("pause photo")
        self.assertEqual((cm.exception.status, cm.exception.kind), (503, "busy"))
        self.assertLess(time.time() - t0, 0.2)       # 待たずにすぐ断る
        t.join(10)
        self.assertEqual(SEEN, [])                     # 予備 (クラウド) にも回さない
        self.assertEqual(Flaky.hits, 2)

    def test_interactive_waits_only_for_the_running_call(self):
        Flaky.delay = 0.6
        results = {}
        bg = threading.Thread(target=lambda: results.setdefault("bg", self.bg("long pause")["status"]))
        bg.start()
        time.sleep(0.15)
        t0 = time.time()
        it = threading.Thread(target=lambda: results.setdefault("chat", (self.chat("question")[0], time.time() - t0)))
        it.start()
        time.sleep(0.15)
        # 走っている 1 つ (後回し可) と、並んでいる人の要求が見える
        jobs = json.loads(urllib.request.urlopen(self.base + "/api/state", timeout=5).read())["llm"]["jobs"]["lan_ollama"]
        self.assertEqual(jobs["running"]["priority"], "background")
        self.assertEqual([q["route"] for q in jobs["queue"]], ["chat"])
        # 人の要求が並んでいる間に来た後回しの要求は前に入らない
        with self.assertRaises(pc.UpstreamError):
            self.bg("another pause")
        bg.join(10)
        it.join(10)
        self.assertEqual(results["bg"], 200)
        self.assertEqual(results["chat"][0], 200)
        self.assertLess(results["chat"][1], 1.6)  # 走っていた 1 つ (残り約 0.45 秒) + 自分 (0.6 秒)
        self.assertEqual(Flaky.max_active, 1)

    def test_identical_requests_are_asked_once(self):
        Flaky.delay = 0.3
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.chat("same")[0])) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(results, [200, 200])
        self.assertEqual(Flaky.hits, 1)
        self.assertEqual(self.chat("same")[0], 200)  # 少しの間は覚えた答え
        self.assertEqual(Flaky.hits, 1)

    def test_all_down_is_503(self):
        llm = self.app.config.data["llm"]
        llm["lan_ollama"]["baseUrl"] = DEAD
        old = llm["cloud_api"]["baseUrl"]
        llm["cloud_api"]["baseUrl"] = DEAD
        try:
            status, _, body = self.chat("down")
            self.assertEqual(status, 503)
            self.assertIn("lan_ollama", body["error"]["message"])
            self.assertIn("cloud_api", body["error"]["message"])
            self.assertTrue(body["say"]["text"])
        finally:
            llm["cloud_api"]["baseUrl"] = old

    def test_cloud_without_key_is_not_a_spare(self):
        llm = self.app.config.data["llm"]
        llm["cloud_api"]["baseUrl"], old = "https://api.example.com/v1", llm["cloud_api"]["baseUrl"]
        llm["cloud_api"]["apiKey"] = ""
        try:
            self.assertEqual([p["backend"] for p in self.app.llm.chain(self.app.config.get())], ["lan_ollama", "local_qwen"])
        finally:
            llm["cloud_api"]["baseUrl"], llm["cloud_api"]["apiKey"] = old, "sk-test"

    def test_named_model_goes_to_primary_only(self):
        self.app.config.data["llm"]["lan_ollama"]["baseUrl"] = DEAD
        status, _, _ = self.chat(model="some-exact-model")
        self.assertEqual(status, 503)
        self.assertEqual(SEEN, [])

    def test_latex_is_stripped_from_replies(self):
        Flaky.content = "答えは $x = \\frac{1}{2}$ と $x^2$"
        status, _, body = self.chat("latex")
        self.assertEqual(self.content(body), "答えは x = 1/2 と x²")

    def test_health_contract_without_token(self):
        handler = self.server.RequestHandlerClass
        self.app.config.data["token"] = "s3cret"
        original = handler.is_local
        handler.is_local = lambda self: False  # LAN の Quest のふり (token なし)
        try:
            t0 = time.time()
            h = json.loads(urllib.request.urlopen(self.base + "/health", timeout=5).read())
            self.assertLess(time.time() - t0, 1.0)
            self.assertEqual(h["app"], "cohabit-pc")
            self.assertEqual(h["version"], pc.VERSION)
            self.assertEqual(h["port"], self.server.server_address[1])
            self.assertEqual(h["llm"]["backend"], "lan_ollama")
            self.assertIn("ok", h["llm"])
            self.assertNotIn("s3cret", json.dumps(h))
            self.assertNotIn("sk-test", json.dumps(h))
            # ほかの Quest の口は token が要るまま
            req = urllib.request.Request(self.base + "/metrics")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(cm.exception.code, 401)
            cm.exception.close()
        finally:
            handler.is_local = original
            self.app.config.data["token"] = ""

    def test_dashboard_state_has_self_check_and_helpers(self):
        self.app.refresh_health = lambda: None  # 本物の確認はしない
        st = json.loads(urllib.request.urlopen(self.base + "/api/state", timeout=5).read())
        for k in ("llm", "selfCheck", "autostart", "firewall", "voicevox", "port", "ips"):
            self.assertIn(k, st)
        self.assertEqual(st["llm"]["primary"], "lan_ollama")
        self.assertEqual(st["config"]["llm"]["cloud_api"]["apiKey"], pc.MASK)


# ---------------------------------------------------------------------------
# 部品
# ---------------------------------------------------------------------------


class PartsTest(unittest.TestCase):
    def test_default_is_lan_ollama_without_thinking(self):
        llm = pc.DEFAULT_CONFIG["llm"]
        self.assertEqual(llm["backend"], "lan_ollama")
        lan = llm["lan_ollama"]
        self.assertEqual(lan["baseUrl"], "http://192.168.0.40:11434/v1")
        self.assertEqual(lan["chatModel"], "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL")
        self.assertEqual(lan["visionModel"], lan["chatModel"])
        self.assertIs(lan["thinking"], False)
        prof = pc.llm_profile(pc.DEFAULT_CONFIG)
        body = pc.no_thinking({}, prof)
        self.assertEqual(body, {"reasoning_effort": "none"})  # Ollama の /v1: これで考えない (chat_template_kwargs は送らない)
        self.assertEqual(pc.DEFAULT_CONFIG["tts"]["speaker"], 58)  # 妹の声は 猫使ビィ (ノーマル)
        self.assertEqual(pc.llm_backend_ids(pc.DEFAULT_CONFIG)[:3], ["lan_ollama", "cloud_api", "local_qwen"])
        # クラウドは書いたときだけ
        self.assertEqual(pc.no_thinking({}, pc.profile_of(pc.DEFAULT_CONFIG, "cloud_api")), {})

    def test_strip_latex(self):
        cases = {
            "$x = \\frac{1}{2}$": "x = 1/2", "$$2 \\times 3$$": "2 × 3", "$x^2 + y^{10}$": "x² + y¹⁰",
            "$\\sqrt{2}$": "√2", "\\(a \\div b\\)": "a ÷ b", "$\\frac{x+1}{2}$": "(x+1)/(2)",
            "値段は $5 です": "値段は $5 です", "ふつうの文": "ふつうの文", "$-4$": "-4",
        }
        for src, want in cases.items():
            self.assertEqual(pc.strip_latex(src), want, src)
        # JSON の返事は値だけ直す (\\ のエスケープを壊さない)
        got = pc.strip_latex_content('{"say": {"text": "$\\\\frac{1}{2}$ だよ"}, "n": 1}')
        self.assertEqual(json.loads(got), {"say": {"text": "1/2 だよ"}, "n": 1})
        self.assertEqual(pc.strip_latex_content('{"broken": "$x$'), '{"broken": "$x$')

    def test_bad_config_json_still_starts(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "config.json")
        with open(path, "w") as f:
            f.write("{ broken")
        c = pc.Config(path)
        self.assertIsNotNone(c.load_error)
        self.assertEqual(c.get()["llm"]["backend"], "lan_ollama")
        with open(path) as f:
            self.assertEqual(f.read(), "{ broken")  # 勝手に書き換えない

    def test_http_error_kinds(self):
        with self.assertRaises(pc.UpstreamError) as cm:
            pc.http("GET", DEAD + "/models", timeout=2)
        self.assertEqual(cm.exception.kind, "refused")
        with self.assertRaises(pc.UpstreamError) as cm:
            pc.http("GET", "ftp://x/y")
        self.assertEqual(cm.exception.status, 502)

    def test_llm_probe_hints(self):
        up = start(ThreadingHTTPServer(("127.0.0.1", 0), Flaky))
        try:
            base = f"http://127.0.0.1:{up.server_address[1]}/v1"
            ok, detail, hint = pc.llm_probe({"baseUrl": base, "chatModel": "lan-m", "visionModel": "lan-m"})
            self.assertTrue(ok, detail)
            ok, detail, hint = pc.llm_probe({"baseUrl": base, "chatModel": "other-m", "visionModel": "other-m"})
            self.assertFalse(ok)
            self.assertIn("ollama pull other-m", hint)
        finally:
            up.shutdown()
            up.server_close()
        ok, detail, hint = pc.llm_probe({"baseUrl": DEAD, "chatModel": "m"})
        self.assertFalse(ok)
        self.assertIn("動いていない", hint)
        ok, _, hint = pc.llm_probe({"baseUrl": "https://api.example.com/v1", "apiKey": ""})
        self.assertFalse(ok)
        self.assertIn("API キー", hint)

    def test_check_backends_overall_llm_uses_spare(self):
        up = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream))
        try:
            base = f"http://127.0.0.1:{up.server_address[1]}"
            cfg = pc.deep_merge(pc.DEFAULT_CONFIG, {
                "llm": {"backend": "lan_ollama", "lan_ollama": {"baseUrl": DEAD},
                        "cloud_api": {"baseUrl": base + "/v1", "apiKey": "k", "chatModel": "", "visionModel": ""},
                        "local_qwen": {"baseUrl": ""}},
                "stt": {"baseUrl": base + "/v1"}, "tts": {"voicevoxUrl": base}, "lev": {"url": ""}})
            h = pc.check_backends(cfg)
            self.assertFalse(h["llmBackends"]["lan_ollama"]["ok"])
            self.assertTrue(h["llmBackends"]["lan_ollama"]["hint"])
            self.assertTrue(h["llmBackends"]["cloud_api"]["ok"])
            self.assertTrue(h["llm"]["ok"])
            self.assertTrue(h["llm"]["degraded"])
            self.assertTrue(h["tts"]["ok"])
            self.assertEqual(h["lev"]["detail"], "使わない設定")
        finally:
            up.shutdown()
            up.server_close()

    def test_autostart_systemd_unit_with_fake_home(self):
        home = tempfile.mkdtemp()
        env = {"HOME": home, "XDG_CONFIG_HOME": os.path.join(home, "xdg")}
        ran = []
        fake_run = lambda args, timeout=0: (ran.append(args) or (True, 0, ""))
        if os.name == "nt" or not os.sys.platform.startswith("linux"):
            self.skipTest("Linux だけ")
        st = pc.autostart_status(env)
        self.assertEqual((st["kind"], st["installed"]), ("systemd-user", False))
        self.assertTrue(st["path"].startswith(os.path.join(home, "xdg", "systemd", "user")))
        r = pc.autostart_install(env, run=fake_run, here="/opt/Cohabit PC")
        self.assertTrue(r["ok"], r)
        with open(st["path"], encoding="utf-8") as f:
            unit = f.read()
        self.assertIn('ExecStart=/bin/sh "/opt/Cohabit PC/start.sh"', unit)
        self.assertIn("WorkingDirectory=/opt/Cohabit PC", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertIn(["systemctl", "--user", "enable", "cohabit-pc.service"], ran)
        self.assertNotIn("start", [a[2] for a in ran if len(a) > 2])  # 今のアプリとポートを取り合わない
        self.assertTrue(pc.autostart_status(env)["installed"])
        r = pc.autostart_remove(env, run=fake_run)
        self.assertTrue(r["ok"])
        self.assertFalse(os.path.exists(st["path"]))
        self.assertIn(["systemctl", "--user", "disable", "cohabit-pc.service"], ran)
        self.assertIn("100%%", pc.systemd_unit("/x/100%"))
        with self.assertRaises(ValueError):
            pc.systemd_unit('/x/"q"')

    def test_windows_startup_bat_text(self):
        bat = pc.windows_startup_bat(r"C:\Users\me\CohabitPC")
        self.assertIn('cd /d "C:\\Users\\me\\CohabitPC"', bat)
        self.assertIn("start.bat", bat)
        self.assertTrue(bat.endswith("\r\n"))

    def test_firewall_with_fake_tools(self):
        ran = []

        def fake_run(args, timeout=0):
            ran.append(args)
            return (True, 0, "active") if args[:2] == ["systemctl", "is-active"] and args[2] == "ufw" else (True, 0, "")
        which = lambda name: "/usr/bin/" + name if name in ("ufw", "pkexec") else None
        if os.name == "nt":
            self.skipTest("Linux だけ")
        self.assertEqual(pc.firewall_detect(fake_run, which), {"kind": "ufw", "active": True})
        r = pc.firewall_allow(8000, ["192.168.0.23"], fake_run, which)
        self.assertTrue(r["ok"], r)
        self.assertEqual(ran[-1], ["pkexec", "ufw", "allow", "proto", "tcp", "from", "192.168.0.0/24", "to", "any",
                                   "port", "8000", "comment", "Cohabit PC"])
        self.assertEqual(pc.firewall_allow("x", [], fake_run, which)["ok"], False)
        self.assertEqual(pc.subnet24("10.1.2.3"), "10.1.2.0/24")
        self.assertIn("sudo ufw allow", pc.firewall_manual("ufw", 8000, ["10.1.2.0/24"]))
        no_fw = pc.firewall_detect(lambda a, t=0: (False, 3, "inactive"), which)
        self.assertEqual(no_fw["kind"], None)

    def test_voicevox_found_and_launched_with_fake_popen(self):
        if os.name == "nt":
            self.skipTest("Linux だけ")
        home = tempfile.mkdtemp()
        env = {"HOME": home, "PATH": ""}
        self.assertIsNone(pc.find_voicevox({"tts": {}}, env))
        run = os.path.join(home, "voicevox_engine", "run")
        os.makedirs(os.path.dirname(run))
        with open(run, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(run, os.stat(run).st_mode | stat.S_IXUSR)
        self.assertEqual(pc.find_voicevox({"tts": {}}, env), run)
        started = []

        class P:
            pid = 4242
        r = pc.launch_voicevox(run, "http://127.0.0.1:50099", popen=lambda args, **kw: (started.append((args, kw)), P())[1])
        self.assertTrue(r["ok"])
        self.assertEqual(started[0][0], [run, "--host", "127.0.0.1", "--port", "50099"])
        self.assertTrue(started[0][1]["start_new_session"])
        # 設定の場所が先
        other = os.path.join(home, "VOICEVOX.AppImage")
        with open(other, "w") as f:
            f.write("x")
        os.chmod(other, 0o755)
        self.assertEqual(pc.find_voicevox({"tts": {"voicevoxPath": other}}, env), other)

    def test_metrics_routes_are_bounded(self):
        m = pc.Metrics()
        for i in range(200):
            m.record(f"/x{i}", 1, True)
        self.assertLessEqual(len(m.routes), 65)


if __name__ == "__main__":
    unittest.main()
