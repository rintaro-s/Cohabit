"""
タブレットの口 (tablet_api.py / TABLET_API.md) の確認。偽の LLM (OpenAI 互換) と偽の VOICEVOX を立てて、タブレットと同じ形で呼ぶ。
    python -m unittest test_tablet_api.py      (標準ライブラリだけ。ネットにも出ない)
"""

import base64
import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cohabit_pc as pc
import tablet_api as ta

DEAD = "http://127.0.0.1:9/v1"  # 誰も聞いていないポート (すぐ断られる)
SEEN = []      # LLM に届いた要求 (JSON)
REPLIES = []   # 次に LLM が返す文字列 (空なら仕事ごとの既定)

PROBLEM = {"subject": "数学", "text": "2x + 3 = 11 を解け", "goal": "x を求める", "solution": "2x = 8\nx = 4",
           "answer": "x = 4", "say": {"text": "これ、やってみて", "expression": "happy"}}
CHECK = {"read": [{"row": 1, "text": "2x+3=11"}, {"row": 2, "text": "2x=14"}],
         "marks": [{"row": 1, "kind": "ok", "text": "いいね", "x": None},
                   {"row": 2, "kind": "mistake", "text": "移項の符号、もう一回見て", "x": 1.7}],
         "solved": False, "stuck": False, "mistakeTypes": ["移項の符号"], "say": {"text": "2 行目、見直して", "expression": "worried"}}
ASK = {"say": {"text": "まず 3 を右に移してみて", "expression": "thinking"}}
DESK = {"readable": True, "read": "2x+3=11 / 2x=14", "mistake": True, "where": "2 行目", "solved": False, "stuck": False,
        "mistakeTypes": ["移項の符号"], "say": {"text": "2 行目、符号見て", "expression": "worried"}}
JUDGE = {"verdict": "wrong", "heard": "x = 7", "mistakeType": "移項の符号", "say": {"text": "おしい、もう一回", "expression": "worried"}}
WEAK = {"items": [{"subject": "数学", "topic": "一次方程式の移項", "detail": "符号を変え忘れる"}], "summary": "移項の符号でつまずきがち"}

JPG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8desk").decode()
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


def task_of(req) -> str:
    content = req["messages"][-1]["content"]
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if p.get("type") == "text")
    return content


class FakeUpstream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, status, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply(200, {"data": []})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/v1/chat/completions":
            req = json.loads(body)
            SEEN.append(req)
            task = task_of(req)
            if REPLIES:
                text = REPLIES.pop(0)
            elif "問題を 1 問出す" in task:
                text = json.dumps(PROBLEM, ensure_ascii=False)
            elif "机の上のノートを見る" in task:
                text = json.dumps(DESK, ensure_ascii=False)
            elif "声で言った答え" in task:
                text = json.dumps(JUDGE, ensure_ascii=False)
            elif "赤ペンを入れる" in task:
                text = json.dumps(CHECK, ensure_ascii=False)
            elif "相手を助ける" in task:
                text = json.dumps(ASK, ensure_ascii=False)
            elif "苦手をまとめる" in task:
                text = json.dumps(WEAK, ensure_ascii=False)
            else:
                text = "?"
            return self.reply(200, {"choices": [{"message": {"role": "assistant", "content": text}}]})
        if self.path.startswith("/audio_query"):
            return self.reply(200, {"speedScale": 1.0, "pitchScale": 0.0})
        if self.path.startswith("/synthesis"):
            return self.reply(200, b"RIFFsister", "audio/wav")
        self.reply(404, {})


def reset_router(app):
    with app.llm.lock:
        app.llm.cache.clear()
        app.llm.errors.clear()
        app.llm.sticky = None
        app.llm.last = None


def start(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class TabletTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream))
        cls.up = f"http://127.0.0.1:{cls.upstream.server_address[1]}"
        cls.tmp = tempfile.mkdtemp()
        # タブレットの静的ファイル (本物の tablet/ は別の担当が書くので、ここでは仮のもの)
        cls.static = os.path.join(cls.tmp, "tablet")
        os.makedirs(os.path.join(cls.static, "js"))
        files = {"index.html": "<!doctype html><title>t</title>", "js/app.js": "export {}", "js/m.mjs": "export {}",
                 "style.css": "body{}", "x.wasm": "\0asm", "manifest.webmanifest": "{}", "data.json": "{}"}
        for name, text in files.items():
            with open(os.path.join(cls.static, name), "w", encoding="utf-8") as f:
                f.write(text)
        with open(os.path.join(cls.tmp, "secret.txt"), "w") as f:
            f.write("SECRET")
        cls.vrm = os.path.join(cls.tmp, "sis.vrm")
        with open(cls.vrm, "wb") as f:
            f.write(b"glTF" + bytes(1000))
        cls.cfg_path = os.path.join(cls.tmp, "config.json")
        with open(cls.cfg_path, "w", encoding="utf-8") as f:
            json.dump({
                # 予備には回さない (既定の lan_ollama は本物の LAN の PC。テストはネットに出ない)
                "llm": {"backend": "cloud_api", "fallback": False, "lan_ollama": {"baseUrl": DEAD}, "local_qwen": {"baseUrl": DEAD},
                        "cloud_api": {"baseUrl": cls.up + "/v1", "apiKey": "sk-test", "chatModel": "chat-m",
                                      "visionModel": "vision-m", "thinking": False}},
                "tts": {"engine": "voicevox", "voicevoxUrl": cls.up, "speaker": 8},
                "lev": {"url": ""},
                "privacy": {"logEvents": False},
                "tablet": {"vrmPath": cls.vrm, "keepAliveSeconds": 0.3},
            }, f)
        pc.DATA_DIR = os.path.join(cls.tmp, "data")
        ta.WEAK_RETRY_SECONDS = 0.2  # 苦手の要約が後回しにされたら、すぐやり直す (テストを待たせない)
        cls.old_dir = ta.TABLET_DIR
        ta.TABLET_DIR = cls.static
        cls.server, cls.app = pc.serve(0, "127.0.0.1", cls.cfg_path, background=False)
        start(cls.server)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.app.stop.set()
        for s in (cls.server, cls.upstream):
            s.shutdown()
            s.server_close()
        ta.TABLET_DIR = cls.old_dir
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        t = self.app.tablet
        if t.weak_thread:
            t.weak_thread.join(5)
        t.delete_learning()
        t.problem = None
        t.presence = "tablet"
        t.speech_cache.clear()
        reset_router(self.app)
        REPLIES.clear()
        SEEN.clear()

    def call(self, path, payload=None, headers=None, method=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method or ("POST" if data is not None else "GET"),
                                     headers={**({"Content-Type": "application/json"} if data else {}), **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers, e.read()

    def jcall(self, path, payload=None, **kw):
        status, _, body = self.call(path, payload, **kw)
        return status, (json.loads(body) if body else None)

    def raw_get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("GET", path, skip_accept_encoding=True)
        conn.endheaders()
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, body

    def new_problem(self):
        status, body = self.jcall("/tablet/api/problem", {"from": "topic", "topic": "一次方程式"})
        self.assertEqual(status, 200, body)
        return body["problem"]["id"]

    # ---- JSON を読む ----

    def test_parse_json_loose(self):
        cases = [
            '<think>hmm {"no": 1}</think>{"a": 1}',
            '```json\n{"a": 1}\n```',
            'はい！\n{"a": 1}\nどうぞ',
            '{"a": 1,}',
            "{'a': 1}".replace("'", '"'),
            '{a: 1}',
            '{"a": 1, "b": [1, 2,], "c": True, "d": None}',
            '{"a": 1, "b": {"x": "途中で切れ',
            'thinking...</think>{"a": 1}',
            '{“a”: 1}',
        ]
        for text in cases:
            got = ta.parse_json_loose(text)
            self.assertIsInstance(got, dict, text)
            self.assertEqual(got.get("a"), 1, text)
        self.assertIs(ta.parse_json_loose('{"a": 1, "c": True}')["c"], True)
        self.assertEqual(ta.parse_json_loose('{"a": 1, "b": {"x": "途中')["b"], {"x": "途中"})
        self.assertIsNone(ta.parse_json_loose("JSON はありません"))
        self.assertIsNone(ta.parse_json_loose("<think>{\"a\": 1}"))

    def test_clean_marks(self):
        marks = ta.clean_marks([{"row": "2", "kind": "mistake", "text": "とても長い赤ペンの一言はここで切れる", "x": 3},
                                {"row": 2, "kind": "ok"}, {"row": 9, "kind": "ok"}, {"row": 1, "kind": "bad"}, "x"], {1, 2})
        self.assertEqual(marks, [{"row": 2, "kind": "mistake", "text": "とても長い赤ペンの一言は", "x": 1.0}])

    # ---- session・問題 ----

    def test_session(self):
        status, s = self.jcall("/tablet/api/session")
        self.assertEqual(status, 200)
        self.assertEqual(s["sister"], {"name": "妹", "vrmUrl": "/tablet/vrm"})
        self.assertIsNone(s["problem"])
        self.assertEqual(s["presence"], "tablet")
        self.assertIn("chat", s["backend"])
        self.assertIn("vision", s["backend"])

    def test_problem_from_topic_hides_solution(self):
        status, body = self.jcall("/tablet/api/problem", {"from": "topic", "topic": "一次方程式", "level": "same"})
        self.assertEqual(status, 200)
        self.assertEqual(set(body["problem"]), {"id", "subject", "text", "goal"})
        self.assertNotIn("x = 4", json.dumps(body, ensure_ascii=False))
        self.assertEqual(body["say"]["expression"], "happy")
        self.assertTrue(body["say"]["audio"].startswith("/tablet/api/speech?"))
        req = SEEN[-1]
        self.assertEqual(req["model"], "chat-m")
        self.assertEqual(req["reasoning_effort"], "none")
        self.assertIn("妹", req["messages"][0]["content"])
        self.assertIn("一次方程式", task_of(req))
        self.assertEqual(self.app.tablet.problem["solution"], "2x = 8\nx = 4")
        status, s = self.jcall("/tablet/api/session")
        self.assertEqual(s["problem"]["id"], body["problem"]["id"])
        status, body = self.jcall("/tablet/api/problem", {"from": "topic", "topic": ""})
        self.assertEqual(status, 400)
        status, body = self.jcall("/tablet/api/problem", {"from": "nope"})
        self.assertEqual(status, 400)

    def test_problem_from_photo_uses_vision(self):
        status, body = self.jcall("/tablet/api/problem", {"from": "photo", "image": PNG})
        self.assertEqual(status, 200)
        req = SEEN[-1]
        self.assertEqual(req["model"], "vision-m")
        self.assertEqual(req["messages"][1]["content"][1]["image_url"]["url"], PNG)
        self.assertEqual(self.jcall("/tablet/api/problem", {"from": "photo", "image": "http://x"})[0], 400)
        self.app.config.data["privacy"]["allowImages"] = False
        try:
            self.assertEqual(self.jcall("/tablet/api/problem", {"from": "photo", "image": PNG})[0], 403)
        finally:
            self.app.config.data["privacy"]["allowImages"] = True

    def test_problem_from_text_keeps_text(self):
        REPLIES.append(json.dumps({**PROBLEM, "text": "書き換えた問題"}, ensure_ascii=False))
        status, body = self.jcall("/tablet/api/problem", {"from": "text", "text": "3x − 5 = 10 を解け"})
        self.assertEqual(status, 200)
        self.assertEqual(body["problem"]["text"], "3x − 5 = 10 を解け")

    def test_problem_next_and_weak(self):
        self.assertEqual(self.jcall("/tablet/api/problem", {"from": "next"})[0], 409)
        pid = self.new_problem()
        REPLIES.append(json.dumps({**CHECK, "marks": [{"row": 1, "kind": "ok"}], "solved": True, "mistakeTypes": [], "say": None}))
        status, r = self.jcall("/tablet/api/check", {"problemId": pid, "trigger": "done", "page": PNG, "rows": [{"row": 1, "top": 0, "bottom": 72, "hasInk": True}]})
        self.assertTrue(r["solved"])
        self.app.tablet.weak_thread.join(5)
        status, body = self.jcall("/tablet/api/problem", {"from": "next"})
        self.assertEqual(status, 200)
        self.assertIn("前の問題", task_of(SEEN[-1]))
        self.assertEqual(self.app.tablet.problem["level"], "harder")  # 解けてヒントなし → 少し難しく
        status, body = self.jcall("/tablet/api/problem", {"from": "weak"})
        self.assertEqual(status, 200)
        self.assertIn("一次方程式の移項", task_of(SEEN[-1]))

    def test_problem_weak_without_records_asks_what_to_study(self):
        # 話題を勝手に決めない (前は記録が無いと中学数学の問題を作っていた)
        status, body = self.jcall("/tablet/api/problem", {"from": "weak"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "no_records")
        self.assertEqual(body["error"]["message"], "まだ学習記録がありません。何を勉強するか教えてください")
        self.assertTrue(body["say"]["text"])
        self.assertEqual(SEEN, [])                      # LLM には聞いていない
        self.assertIsNone(self.app.tablet.problem)
        self.assertEqual(self.jcall("/tablet/api/problem", {"from": "topic"})[0], 400)
        self.assertEqual(self.jcall("/tablet/api/problem", {"from": "next"})[0], 409)

    def test_problem_weak_uses_records_when_summary_is_missing(self):
        pid = self.new_problem()
        REPLIES.append(json.dumps({"marks": [], "solved": True, "mistakeTypes": []}))
        self.jcall("/tablet/api/check", {"problemId": pid, "trigger": "done", "page": PNG})
        self.app.tablet.weak_thread.join(5)
        os.remove(os.path.join(pc.DATA_DIR, "weak_points.json"))
        t = self.app.tablet

        def fail(**kw):
            raise pc.UpstreamError(502, "LLM did not return JSON")
        t.refresh_weak = fail  # 苦手の要約が作れない
        try:
            status, body = self.jcall("/tablet/api/problem", {"from": "weak"})
        finally:
            del t.refresh_weak
        self.assertEqual(status, 200, body)
        self.assertIn("2x + 3 = 11", task_of(SEEN[-1]))  # 記録の問題から作る

    # ---- 赤ペン ----

    def test_check_marks_cache_and_validation(self):
        pid = self.new_problem()
        rows = [{"row": 1, "top": 0, "bottom": 72, "hasInk": True}, {"row": 2, "top": 72, "bottom": 144, "hasInk": True},
                {"row": 3, "top": 144, "bottom": 216, "hasInk": False}]
        req = {"problemId": pid, "trigger": "request", "page": PNG, "rows": rows, "changedRows": [2]}
        status, r = self.jcall("/tablet/api/check", req)
        self.assertEqual(status, 200)
        self.assertEqual([m["kind"] for m in r["marks"]], ["ok", "mistake"])
        self.assertEqual(r["marks"][1], {"row": 2, "kind": "mistake", "text": "移項の符号、もう一回見て"[:12], "x": 1.0})
        self.assertFalse(r["solved"])
        self.assertEqual(r["say"]["text"], "2 行目、見直して")
        sent = SEEN[-1]
        self.assertEqual(sent["model"], "vision-m")
        task = task_of(sent)
        self.assertIn("x = 4", task)                 # 模範解答は LLM には渡す
        self.assertIn("1 行の高さ 72 px", task)
        n = len(SEEN)
        status, r2 = self.jcall("/tablet/api/check", req)  # 同じページ → 覚えた答え
        self.assertEqual(len(SEEN), n)
        self.assertTrue(r2.get("cached"))
        self.assertEqual(self.app.tablet.problem["mistakes"], ["移項の符号"])
        # 別の問題の id → 409
        self.assertEqual(self.jcall("/tablet/api/check", {**req, "problemId": "old"})[0], 409)
        self.assertEqual(self.jcall("/tablet/api/check", {**req, "page": "nope", "trigger": "done"})[0], 400)

    def test_pause_is_quiet_unless_mistake(self):
        pid = self.new_problem()
        REPLIES.append(json.dumps({"marks": [{"row": 1, "kind": "ok", "text": "いいね"}], "solved": False, "say": {"text": "いい感じ"}}, ensure_ascii=False))
        status, r = self.jcall("/tablet/api/check", {"problemId": pid, "trigger": "pause", "page": PNG, "changedRows": [1]})
        self.assertEqual(status, 200)
        self.assertEqual(r["marks"], [])
        self.assertIsNone(r["say"])
        status, r = self.jcall("/tablet/api/check", {"problemId": pid, "trigger": "pause", "page": PNG + "AA", "changedRows": [2]})
        self.assertEqual([m["kind"] for m in r["marks"]], ["mistake"])
        self.assertIsNotNone(r["say"])

    def test_check_repairs_messy_json_and_retries_once(self):
        pid = self.new_problem()
        REPLIES.append('<think>読む…</think>\n```json\n{"marks": [{"row": 1, "kind": "mistake", "text": "符号", "x": 0.5},], "solved": False, "say": null,}\n```')
        status, r = self.jcall("/tablet/api/check", {"problemId": pid, "trigger": "request", "page": PNG})
        self.assertEqual(status, 200, r)
        self.assertEqual(r["marks"][0]["text"], "符号")
        # 1 回目が JSON でなければ、画像なしで直させる
        REPLIES.extend(["ごめん、読めなかった", '{"marks": [], "solved": false, "say": {"text": "読めた"}}'])
        n = len(SEEN)
        status, r = self.jcall("/tablet/api/check", {"problemId": pid, "trigger": "request", "page": PNG + "B"})
        self.assertEqual(status, 200)
        self.assertEqual(len(SEEN), n + 2)
        self.assertEqual(SEEN[-1]["model"], "chat-m")
        self.assertIsInstance(SEEN[-1]["messages"][1]["content"], str)
        REPLIES.extend(["だめ", "まだだめ"])
        status, r = self.jcall("/tablet/api/check", {"problemId": pid, "trigger": "request", "page": PNG + "C"})
        self.assertEqual(status, 502)

    def test_solved_writes_learning_and_weak_points_then_delete(self):
        pid = self.new_problem()
        self.jcall("/tablet/api/ask", {"problemId": pid, "level": "hint"})
        self.jcall("/tablet/api/check", {"problemId": pid, "trigger": "request", "page": PNG})
        REPLIES.append(json.dumps({"marks": [], "solved": True, "mistakeTypes": []}))
        status, r = self.jcall("/tablet/api/check", {"problemId": pid, "trigger": "done", "page": PNG + "D"})
        self.assertTrue(r["solved"])
        self.assertEqual(r["say"]["expression"], "happy")
        self.app.tablet.weak_thread.join(5)
        with open(os.path.join(pc.DATA_DIR, "learning.jsonl"), encoding="utf-8") as f:
            rec = [json.loads(line) for line in f]
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["result"], "solved")
        self.assertEqual(rec[0]["subject"], "数学")
        self.assertEqual(rec[0]["mistakes"], ["移項の符号"])
        self.assertEqual(rec[0]["hints"]["hint"], 1)
        self.assertTrue(rec[0]["hintsUsed"])
        self.assertIn("seconds", rec[0])
        status, s = self.jcall("/tablet/api/session")
        self.assertEqual(s["weak"]["summary"], "移項の符号でつまずきがち")
        self.assertEqual(s["weak"]["basedOn"], 1)
        # 画面の状態にも出る
        status, st = self.jcall("/api/state")
        self.assertEqual(st["tablet"]["learningCount"], 1)
        self.assertEqual(st["tablet"]["problem"]["solution"], "2x = 8\nx = 4")
        self.assertTrue(st["tablet"]["urls"] == [] or "/tablet/" in st["tablet"]["urls"][0])
        # 消す
        status, body = self.jcall("/api/delete", {"what": "learning"})
        self.assertEqual(body["removed"], ["learning"])
        self.assertFalse(os.path.exists(os.path.join(pc.DATA_DIR, "learning.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(pc.DATA_DIR, "weak_points.json")))
        self.assertIsNone(self.jcall("/tablet/api/session")[1]["weak"])

    def test_delete_all_includes_learning(self):
        pid = self.new_problem()
        REPLIES.append(json.dumps({"marks": [], "solved": True}))
        self.jcall("/tablet/api/check", {"problemId": pid, "trigger": "done", "page": PNG})
        self.app.tablet.weak_thread.join(5)
        self.assertTrue(os.path.exists(os.path.join(pc.DATA_DIR, "learning.jsonl")))
        status, body = self.jcall("/api/delete", {"what": "all"})
        self.assertIn("learning", body["removed"])
        self.assertFalse(os.path.exists(os.path.join(pc.DATA_DIR, "learning.jsonl")))

    def test_moving_on_records_unsolved_problem(self):
        pid = self.new_problem()
        self.jcall("/tablet/api/check", {"problemId": pid, "trigger": "request", "page": PNG})
        self.new_problem()
        rec = self.app.tablet.read_learning()
        self.assertEqual(rec[-1]["result"], "moved_on")

    def test_keep_learning_off(self):
        self.app.config.data["tablet"]["keepLearning"] = False
        try:
            pid = self.new_problem()
            REPLIES.append(json.dumps({"marks": [], "solved": True}))
            self.jcall("/tablet/api/check", {"problemId": pid, "trigger": "done", "page": PNG})
            self.assertFalse(os.path.exists(os.path.join(pc.DATA_DIR, "learning.jsonl")))
        finally:
            self.app.config.data["tablet"]["keepLearning"] = True


    # ---- Quest から ----

    def test_quest_answer_by_voice(self):
        pid = self.new_problem()   # 答えは x = 4
        n = len(SEEN)
        # 数がそのまま合っていれば LLM に聞かない (「マイナス」「よん」「x イコール」も読む)
        status, r = self.jcall("/tablet/api/answer", {"problemId": pid, "answer": "x イコール 4", "from": "quest"})
        self.assertEqual(status, 200, r)
        self.assertEqual((r["correct"], r["solved"]), (True, True))
        self.assertEqual(r["say"]["expression"], "happy")
        self.assertTrue(r["say"]["audio"].startswith("/tablet/api/speech?"))
        self.assertFalse(any("声で言った答え" in task_of(r) for r in SEEN[n:]))
        rec = self.app.tablet.read_learning()
        self.assertEqual(rec[-1]["result"], "solved")
        self.assertEqual(self.app.tablet.last_check["trigger"], "answer")

    def test_quest_answer_wrong_and_unclear_ask_the_llm(self):
        pid = self.new_problem()
        status, r = self.jcall("/tablet/api/answer", {"problemId": pid, "answer": "なな", "from": "quest"})
        self.assertEqual(status, 200, r)
        self.assertEqual((r["correct"], r["solved"]), (False, False))
        self.assertEqual(r["say"]["text"], "おしい、もう一回")
        task = task_of(SEEN[-1])
        self.assertIn("「なな」", task)
        self.assertIn("x = 4", task)                      # 模範解答は LLM にだけ
        self.assertIn("移項の符号", self.app.tablet.problem["mistakes"])
        self.assertEqual(self.app.tablet.read_learning(), [])  # まだ解けていない
        REPLIES.append(json.dumps({"verdict": "unclear", "say": None}))
        status, r = self.jcall("/tablet/api/answer", {"problemId": pid, "answer": "えーっと", "from": "quest"})
        self.assertIsNone(r["correct"])
        self.assertIn("もう一回", r["say"]["text"])
        # 同じ答えは聞き直さない
        n = len(SEEN)
        self.assertTrue(self.jcall("/tablet/api/answer", {"problemId": pid, "answer": "なな"})[1].get("cached"))
        self.assertEqual(len(SEEN), n)
        self.assertEqual(self.jcall("/tablet/api/answer", {"problemId": "old", "answer": "4"})[0], 409)
        self.assertEqual(self.jcall("/tablet/api/answer", {"problemId": pid, "answer": " "})[0], 400)

    def test_quest_answer_when_llm_is_down(self):
        pid = self.new_problem()
        self.app.config.data["llm"]["cloud_api"]["baseUrl"] = "http://127.0.0.1:9/v1"
        try:
            self.assertTrue(self.jcall("/tablet/api/answer", {"problemId": pid, "answer": "4"})[1]["correct"])  # LLM なしで分かる
            status, r = self.jcall("/tablet/api/answer", {"problemId": pid, "answer": "5"})
            self.assertEqual((status, r["correct"]), (200, False))           # 数として違う: これも分かる
            status, r = self.jcall("/tablet/api/answer", {"problemId": pid, "answer": "わからない"})
            self.assertEqual(status, 503)
            self.assertTrue(r["say"]["text"])                                 # 言えるひとこと付き
        finally:
            self.app.config.data["llm"]["cloud_api"]["baseUrl"] = self.up + "/v1"

    def test_quest_desk_photo_check(self):
        pid = self.new_problem()
        req = {"problemId": pid, "trigger": "pause", "page": JPG, "source": "quest"}
        status, r = self.jcall("/tablet/api/check", req)
        self.assertEqual(status, 200, r)
        self.assertEqual(r["marks"], [])                 # 行番号が無いので印は無し
        self.assertTrue(r["mistake"])
        self.assertEqual(r["say"]["text"], "2 行目、符号見て")
        self.assertFalse(r["solved"])
        sent = SEEN[-1]
        self.assertEqual(sent["model"], "vision-m")
        self.assertIn("Quest", task_of(sent))
        self.assertEqual(sent["messages"][1]["content"][1]["image_url"]["url"], JPG)
        self.assertEqual(self.app.tablet.problem["mistakes"], ["移項の符号"])
        self.assertTrue(self.jcall("/tablet/api/check", req)[1].get("cached"))
        # 間違いが無い pause は黙る、request は何か言う
        REPLIES.append(json.dumps({"readable": True, "mistake": False, "solved": False, "say": {"text": "いい感じ"}}, ensure_ascii=False))
        status, r = self.jcall("/tablet/api/check", {**req, "page": JPG + "A"})
        self.assertIsNone(r["say"])
        REPLIES.append(json.dumps({"readable": False, "say": None}))
        status, r = self.jcall("/tablet/api/check", {**req, "page": JPG + "B", "trigger": "request"})
        self.assertIn("見えない", r["say"]["text"])
        REPLIES.append(json.dumps({"readable": True, "mistake": False, "solved": True, "say": None}))
        status, r = self.jcall("/tablet/api/check", {**req, "page": JPG + "C", "trigger": "request"})
        self.assertTrue(r["solved"])
        self.assertEqual(self.app.tablet.read_learning()[-1]["result"], "solved")

    def test_quest_pause_photo_is_skipped_while_llm_is_busy(self):
        pid = self.new_problem()
        slot = self.app.llm._slot("cloud_api")
        self.assertTrue(slot.acquire({"route": "tablet.ask", "priority": "interactive"}, 1))  # 人の要求が走っている
        try:
            n = len(SEEN)
            status, r = self.jcall("/tablet/api/check", {"problemId": pid, "trigger": "pause", "page": JPG + "busy", "source": "quest"})
            self.assertEqual(status, 503)
            self.assertEqual(r["error"]["code"], "busy")
            self.assertNotIn("say", r)          # Quest は「飛ばした」と扱う。何も言わない
            self.assertEqual(len(SEEN), n)
            # 「見て」(request) は人が待っているので並ぶ (ここでは空くまで待つ)
            done = {}
            t = threading.Thread(target=lambda: done.setdefault("r", self.jcall(
                "/tablet/api/check", {"problemId": pid, "trigger": "request", "page": JPG + "req", "source": "quest"})))
            t.start()
            time.sleep(0.3)
            self.assertNotIn("r", done)
        finally:
            slot.release()
        t.join(10)
        self.assertEqual(done["r"][0], 200)
        self.assertNotIn("/tablet/api/check", [k for k, v in self.app.metrics.snapshot()["routes"].items() if v["errors"]])

    def test_quest_ask_and_problem_without_page(self):
        status, body = self.jcall("/tablet/api/problem", {"from": "topic", "topic": "連立方程式"})
        self.assertEqual(status, 200)
        pid = body["problem"]["id"]
        for level in ("hint", "explain", "answer"):
            status, r = self.jcall("/tablet/api/ask", {"problemId": pid, "level": level, "question": "どうすればいい？"})
            self.assertEqual(status, 200)
            self.assertTrue(r["say"]["audio"].startswith("/tablet/api/speech?"))
            self.assertNotIn("marks", r)
        self.assertEqual(self.jcall("/tablet/api/problem", {"from": "next"})[0], 200)

    def test_latex_is_cleaned_in_say(self):
        pid = self.new_problem()
        REPLIES.append(json.dumps({"say": {"text": "$x^2$ と $\\frac{1}{2}$ を見て"}}, ensure_ascii=False))
        status, r = self.jcall("/tablet/api/ask", {"problemId": pid, "level": "hint"})
        self.assertEqual(r["say"]["text"], "x² と 1/2 を見て")

    def test_spoken_numbers(self):
        cases = [("マイナス 4", "x = −4", True), ("まいなすよん", "-4", True), ("2 分の 1", "½", True), ("答えは 3.5 です", "7/2", True),
                 ("十二", "12", True), ("5", "x = -5", False), ("わからない", "3", None), ("x = 2, y = 3", "x = 2, y = 3", None)]
        for spoken, answer, want in cases:
            self.assertEqual(ta.quick_compare(spoken, answer), want, (spoken, answer))

    # ---- 質問 ----

    def test_ask_levels(self):
        pid = self.new_problem()
        for level in ("hint", "explain", "answer"):
            status, r = self.jcall("/tablet/api/ask", {"problemId": pid, "level": level})
            self.assertEqual(status, 200)
            self.assertEqual(r["say"]["text"], "まず 3 を右に移してみて")
            self.assertNotIn("marks", r)
            self.assertIn(f"({level})", task_of(SEEN[-1]))
        self.assertEqual(self.app.tablet.problem["hints"], {"hint": 1, "explain": 1, "answer": 1})
        REPLIES.append(json.dumps({"say": {"text": "ここ"}, "marks": [{"row": 2, "kind": "note", "text": "ここ"}]}, ensure_ascii=False))
        status, r = self.jcall("/tablet/api/ask", {"problemId": pid, "level": "hint", "question": "2 行目あってる？", "page": PNG})
        self.assertEqual(r["marks"], [{"row": 2, "kind": "note", "text": "ここ"}])
        self.assertEqual(SEEN[-1]["model"], "vision-m")
        self.assertIn("2 行目あってる？", task_of(SEEN[-1]))

    def test_ask_without_problem(self):
        self.assertEqual(self.jcall("/tablet/api/ask", {"level": "hint"})[0], 409)
        status, r = self.jcall("/tablet/api/ask", {"level": "explain", "question": "光合成って何？"})
        self.assertEqual(status, 200)

    # ---- 声 ----

    def test_speech_and_204(self):
        status, headers, body = self.call("/tablet/api/speech?text=" + urllib.parse.quote("できたじゃん") + "&expression=happy")
        self.assertEqual((status, headers.get("Content-Type"), body), (200, "audio/wav", b"RIFFsister"))
        self.assertEqual(self.call("/tablet/api/speech")[0], 400)
        self.app.config.data["tts"]["voicevoxUrl"] = "http://127.0.0.1:9"
        try:
            status, _, body = self.call("/tablet/api/speech?text=x")
            self.assertEqual((status, body), (204, b""))
        finally:
            self.app.config.data["tts"]["voicevoxUrl"] = self.up

    # ---- SSE ----

    def read_event(self, r):
        lines = []
        while True:
            line = r.readline().decode("utf-8")
            if line in ("\n", ""):
                if lines:
                    return lines
                continue
            lines.append(line.rstrip("\n"))

    def test_events_stream(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/tablet/api/events")
        r = conn.getresponse()
        self.assertEqual(r.status, 200)
        self.assertTrue(r.getheader("Content-Type").startswith("text/event-stream"))
        self.assertEqual(self.read_event(r), ["retry: 3000"])
        self.assertEqual(self.read_event(r), ["event: presence", 'data: {"where": "tablet"}'])
        self.assertEqual(self.read_event(r), [": keep-alive"])  # keepAliveSeconds 0.3
        self.assertEqual(self.jcall("/tablet/api/presence", {"wearing": True})[1]["presence"], "room")
        self.assertEqual(self.read_event(r), ["event: presence", 'data: {"where": "room"}'])
        self.assertEqual(self.jcall("/tablet/api/session")[1]["presence"], "room")
        self.assertEqual(self.jcall("/tablet/api/presence", {"where": "kitchen"})[0], 400)
        # Quest の発言 (/observe) と画面からの言葉
        self.call("/observe", {"events": [{"type": "action", "payload": {"what": "say", "text": "おかえり"}}]})
        ev = self.read_event(r)
        self.assertEqual(ev[0], "event: say")
        say = json.loads(ev[1][len("data: "):])
        self.assertEqual((say["text"], say["from"]), ("おかえり", "quest"))
        self.assertIn("/tablet/api/speech?", say["audio"])
        # PC の先生の返事 (by: pc_teacher) は流さない (二重になる)。次に来るのは下の dashboard の言葉
        self.call("/observe", {"events": [{"type": "action", "payload": {"what": "say", "by": "pc_teacher", "text": "ヒントだよ"}}]})
        self.jcall("/tablet/api/say", {"text": "がんばって", "expression": "happy", "from": "dashboard"})
        self.assertEqual(json.loads(self.read_event(r)[1][6:])["from"], "dashboard")
        self.new_problem()
        ev = self.read_event(r)
        self.assertEqual(ev[0], "event: problem")
        self.assertNotIn("solution", ev[1])
        self.assertEqual(self.app.tablet.dashboard_state(self.port)["sse"], 1)
        conn.close()

    # ---- token ----

    def test_token_from_lan(self):
        handler = self.server.RequestHandlerClass
        self.app.config.data["token"] = "s3cret"
        original = handler.is_local
        handler.is_local = lambda self: False  # LAN のタブレットのふり
        try:
            self.assertEqual(self.call("/tablet/api/session")[0], 401)
            self.assertEqual(self.call("/tablet/api/session", headers={"X-Cohabit-Token": "nope"})[0], 401)
            status, s = self.jcall("/tablet/api/session", headers={"X-Cohabit-Token": "s3cret"})
            self.assertEqual(status, 200)
            self.assertEqual(s["sister"]["vrmUrl"], "/tablet/vrm?token=s3cret")
            self.assertEqual(self.call("/tablet/api/session?token=s3cret")[0], 200)
            self.assertEqual(self.call("/tablet/vrm")[0], 401)
            self.assertEqual(self.call("/tablet/vrm?token=s3cret")[0], 200)
            self.assertEqual(self.call("/tablet/api/problem", {"from": "topic", "topic": "x"})[0], 401)
            # 静的ファイルは token なし (ページを開く URL にヘッダーは付けられない)
            self.assertEqual(self.call("/tablet/")[0], 200)
            # 声の URL には token が入る (<audio> はヘッダーを付けられない)
            status, body = self.jcall("/tablet/api/say?token=s3cret", {"text": "やあ"})
            self.assertIn("token=s3cret", body["say"]["audio"])
            # 画面の API は LAN からは使えないまま
            self.assertEqual(self.call("/api/state")[0], 403)
            self.assertIsNotNone(self.app.tablet.client["lastSeen"])
        finally:
            handler.is_local = original
            self.app.config.data["token"] = ""

    # ---- 静的ファイル・VRM ----

    def test_static_files_and_mime(self):
        status, headers, body = self.call("/tablet/")
        self.assertEqual((status, body), (200, b"<!doctype html><title>t</title>"))
        self.assertEqual(headers.get("Content-Type"), "text/html; charset=utf-8")
        expect = {"/tablet/js/app.js": "text/javascript", "/tablet/js/m.mjs": "text/javascript", "/tablet/style.css": "text/css",
                  "/tablet/x.wasm": "application/wasm", "/tablet/manifest.webmanifest": "application/manifest+json",
                  "/tablet/data.json": "application/json", "/tablet/index.html": "text/html"}
        for path, mime in expect.items():
            status, headers, _ = self.call(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers.get("Content-Type").startswith(mime), (path, headers.get("Content-Type")))
        status, headers, _ = self.call("/tablet/style.css", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(self.call("/tablet/missing.js")[0], 404)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/tablet?x=1")
        r = conn.getresponse()
        self.assertEqual((r.status, r.getheader("Location")), (301, "/tablet/?x=1"))
        conn.close()

    def test_no_path_traversal(self):
        for path in ("/tablet/../secret.txt", "/tablet/%2e%2e/secret.txt", "/tablet/..%2fsecret.txt", "/tablet/js/../../secret.txt",
                     "/tablet/%2e%2e%2f%2e%2e%2fconfig.json", "/tablet/..%5csecret.txt", "/tablet//etc/passwd",
                     "/tablet/" + os.path.join(self.tmp, "secret.txt"), "/tablet/%00x"):
            status, body = self.raw_get(path)
            self.assertIn(status, (400, 403, 404), path)
            self.assertNotIn(b"SECRET", body, path)
            self.assertNotIn(b"sk-test", body, path)
        # 中の .. は外に出なければ 403 で断る (安全側)
        self.assertEqual(self.raw_get("/tablet/js/../index.html")[0], 403)

    def test_vrm_with_etag(self):
        status, headers, body = self.call("/tablet/vrm")
        self.assertEqual(status, 200)
        self.assertEqual(body[:4], b"glTF")
        self.assertEqual(headers.get("Content-Type"), "model/gltf-binary")
        status, _, body = self.call("/tablet/vrm", headers={"If-None-Match": headers.get("ETag")})
        self.assertEqual((status, body), (304, b""))
        self.app.config.data["tablet"]["vrmPath"] = "nope/none.vrm"
        try:
            self.assertEqual(self.call("/tablet/vrm")[0], 404)
        finally:
            self.app.config.data["tablet"]["vrmPath"] = self.vrm

    def test_default_vrm_path_points_at_quest_project(self):
        self.assertTrue(ta.DEFAULT_VRM.endswith("Sis_3b1fdc3d.vrm"))
        self.assertEqual(pc.DEFAULT_CONFIG["tablet"]["vrmPath"], ta.DEFAULT_VRM)

    def test_llm_down_is_503_with_spoken_fallback(self):
        self.app.config.data["llm"]["cloud_api"]["baseUrl"] = "http://127.0.0.1:9/v1"
        try:
            status, body = self.jcall("/tablet/api/problem", {"from": "topic", "topic": "x"})
            self.assertEqual(status, 503)
            self.assertIn("error", body)
            self.assertTrue(body["say"]["text"])
            self.assertTrue(body["say"]["audio"].startswith("/tablet/api/speech?"))
        finally:
            self.app.config.data["llm"]["cloud_api"]["baseUrl"] = self.up + "/v1"


if __name__ == "__main__":
    unittest.main()
