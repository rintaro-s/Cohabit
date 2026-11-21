# Cohabit タブレット・Quest ↔ Cohabit PC の約束 (v1.1)

Cohabit v4.2 の土台 (案 A): **Android タブレットのペンで解く → PC の LLM が読んで教える → 妹が赤ペンを入れる**。
教科は決めない (汎用の先生)。問題の理解・計算・正誤・ヒント・説明はすべて LLM がする。PC はその窓口と記憶を持つ。
Quest をかぶったときの授業 (案 B)・部屋の物の問題 (案 C) は、同じ記憶と同じ API の上に後から足す。

- PC: `tmp/CohabitPC/cohabit_pc.py` + `tablet_api.py` (標準ライブラリだけ)。同じポート (既定 8000) で配る。トークンを設定していれば `X-Cohabit-Token` (SSE とファイルは `?token=`)
  - `/tablet/` の静的ファイル (HTML・JS・CSS など) は **token なし**で配る (秘密が無く、ページを開く URL にヘッダーは付けられないため)。
    token が要るのは `/tablet/api/*` と `/tablet/vrm`。PC の画面に出るタブレット用 URL は `http://<PC>:8000/tablet/?token=…` なので、
    タブレットは `location.search` の `token` を覚えて、API にはヘッダーで、`EventSource` と VRM には `?token=` で付ける
  - PC から返す `vrmUrl` と `Say.audio` には、token があれば `token=` が最初から入っている (そのまま `<audio src>` / ローダーに渡せる)
- タブレット: `tmp/CohabitPC/tablet/` の静的ファイル。Android の Chrome で `http://<PC>:8000/tablet/` を開く (ホーム画面に追加できる)

## 画面のもの (タブレット)

- **ノート**: 罫線のあるページ。罫 1 本ぶん (既定 72 px) が 1 行。行番号は左の余白に薄く出す。ペンで書き、指ではスクロール・パン (手のひらの誤反応を避ける)。消しゴム (ペンのボタン / 消しゴムの端 / 道具)、元に戻す、ページを足す
- **問題**: ページの上に問題の欄。問題は「話題を言う / 打つ」「教科書をタブレットのカメラで撮る」「苦手から出す」「続き」で決まる
- **妹**: 画面の端に VRM の胸から上 (まばたき・表情・話すと口が動く)、字幕、声
- **赤ペン**: 妹が間違えた行に手書きの赤い線・丸・一言を書き込む (書いていくように出る)。正解は花丸

## API (すべて JSON、パスは `/tablet/api/…`)

### `GET /tablet/api/session`
```json
{ "sister": { "name": "妹", "vrmUrl": "/tablet/vrm" }, "student": { "name": "…" },
  "problem": null | Problem, "presence": "tablet" | "room", "backend": { "chat": "クラウド / この PC", "vision": "…" },
  // 追加 (任意): 今の問題で最後に出した赤ペン・解けたか・苦手の要約 (まだ無ければ null)
  "marks": [Mark], "solved": false,
  "weak": null | { "t": "…", "basedOn": 3, "summary": "移項の符号でつまずきがち", "items": [ { "subject": "数学", "topic": "一次方程式の移項", "detail": "符号を変え忘れる" } ] } }
```

### `POST /tablet/api/problem`
```json
{ "from": "topic" | "photo" | "weak" | "next" | "text", "topic": "二次方程式の解の公式", "image": "data:image/jpeg;base64,…", "level": "easier" | "same" | "harder",
  "text": "3x − 5 = 10 を解け" /* 追加: from "text" のとき。問題文をそのまま使い、模範解答だけ作らせる (Quest の授業・手入力) */ }
```
- `photo` は `image` が要る (`topic` は希望として任意)。`next` は前の結果から難しさを決める (解けてヒントなし → harder、答えを見た / 解けなかった → easier、ほかは same。`level` を書けばそれが優先)。前の問題が無ければ 409
- `weak` は苦手の要約から 1 つ選ぶ (要約がまだ作れなければ学習記録の、間違えた・解けなかった問題から)。
  **話題を勝手に決めない**: 学習記録が無ければ 409 `{ "error": { "code": "no_records", "message": "まだ学習記録がありません。何を勉強するか教えてください" }, "say": Say }`
  (LLM には聞かない)。タブレットは話題の欄を開いて待つ。Quest は `say` を言って、何を勉強するか聞く
- `topic` が空なら 400。`next` で前の問題が無ければ 409 (どれも代わりの話題で問題を作らない)
- 手を付けた (見た・ヒントを使った) のに解かずに次の問題へ進むと、前の問題を `moved_on` として学習記録に残す
- 作った問題は SSE の `problem` でも流れる (頼んだタブレット自身にも届くので、`problem.id` で重ねて出さないようにする)
→ `{ "problem": Problem, "say": Say }`。PC は LLM に問題と模範解答 (タブレットには返さない) を作らせ、問題を覚えておく。
```ts
Problem = { id: string; subject: string; text: string /* 表示用の素の文字。数式は Unicode で書く (x² − 3x = 4、√2、½)。LaTeX は使わない */; goal: string /* 何ができればよいか */ }
Say = { text: string; expression?: 'neutral'|'happy'|'thinking'|'surprised'|'worried'; audio?: string /* /tablet/api/speech?… */ }
```

### `POST /tablet/api/check`
タブレットは書くのが止まって約 1.5 秒後 (`trigger: "pause"`)、「見て」を押したとき (`"request"`)、「できた」(`"done"`) に送る。
```json
{ "problemId": "…", "trigger": "pause" | "request" | "done",
  "page": "data:image/png;base64,…",          // ノート全体 (白地・黒インク・行番号入り)。長辺 1600 px まで
  "rows": [ { "row": 1, "top": 0, "bottom": 72, "hasInk": true } ],
  "changedRows": [3, 4] }
```
→
```json
{ "marks": [ { "row": 3, "kind": "ok" | "mistake" | "unclear" | "note", "text": "符号、逆", "x": 0.62 } ],
  "solved": false, "say": Say | null, "stuck": false }
```
- `mistake` は赤ペンで線と一言、`ok` は小さなチェック (毎回は出さない)、`unclear` は「読めない」、`note` はほめ・補足
- `solved: true` で花丸。PC は学習記録を残す
- `pause` では、間違いが無ければ marks を空にして黙る (集中を邪魔しない)。言うのは間違いを見つけたときだけ (PC が `mistake` 以外の印を落とす)
- `text` は 12 字まで (PC が切る)。`x` は分からなければ付かない。1 行に印は 1 つ (間違いを優先)。`hasInk: false` の行の印は落とす
- 同じページ・同じ問題・同じ trigger は LLM に聞き直さず前の答えを返す (追加: `"cached": true`)
- `stuck` は LLM の見立てか、書き足しが 2 分ない (解けていない) とき true

### `POST /tablet/api/ask`
「ヒント」「わからない」ボタンや、声の質問。`{ "problemId", "question"?: "…", "page"?: "data:…", "level": "hint" | "explain" | "answer" }`
→ `{ "say": Say, "marks"?: [...] }`。hint は次の 1 歩だけ、explain は考え方、answer は答えまで (押したときだけ)
- `marks` は `page` を送ったときだけ付く。問題が無くても `question` があれば答える (無ければ 409)。使った回数は学習記録の `hints` に入る

### `GET /tablet/api/speech?text=…&expression=…`
読み上げの WAV (VOICEVOX)。無ければ 204 (字幕だけ)

### `GET /tablet/api/events` (Server-Sent Events)
`presence` (`{ "where": "room" | "tablet" }`: Quest をかぶった / 外した)、`say` (Quest 側の妹の発言をタブレットにも出す)、`problem` (Quest の授業から問題が届く)
- つないだ直後に `retry: 3000` と今の `presence` が来る。何も無い間は 15 秒ごとに `: keep-alive` (コメント行)
- `say` の data は `Say` + `"from": "quest" | "dashboard" | …`。Quest が `/observe` に送る「言ったこと」(`type: "action"`, `payload.what: "say"`) は自動でここに流れる。
  ただし `payload.by: "pc_teacher"` (PC の `/ask`・`/answer`・`/check` の返事を Quest が言ったもの) は流さない (二重になるため)。`by: "quest"` だけ流れる
- `problem` の data は `{ "problem": Problem, "say": Say, "from": "topic" | … }` (`POST /tablet/api/problem` の返事と同じ形 + from)

### 追加: `POST /tablet/api/presence` (Quest・PC の画面から)
`{ "where": "room" | "tablet" }` か `{ "wearing": true | false }` → `{ "ok": true, "presence": "room", "changed": true }`。変わったら SSE の `presence` を流す

### 追加: `POST /tablet/api/say` (Quest・PC の画面から)
`{ "text": "…", "expression"?: "happy", "from"?: "quest" }` → `{ "ok": true, "say": Say }`。SSE の `say` を流す

### エラー
`{ "error": { "message": "…", "code"?: "llm_down" | "busy" | "no_records" | … } }` と状態コード: 400 (形が違う)、401 (token)、403 (PC の設定で画像を断っている: `privacy.allowImages`)、
409 (`problemId` が今の問題と違う / 問題がまだ無い → `/session` を読み直す)、429 (LLM の回数・トークンの上限。少し待ってやり直す)、
502 (LLM が JSON を返さない)、503 (**どの LLM にも届かない** — 第一候補も予備もだめ)、504 (LLM の返事が遅すぎる)
- LLM を使う口 (`problem` / `check` / `ask` / `answer`) が 429 / 502 / 503 / 504 のときは、エラーと一緒に **`"say": Say`** (妹が言えるひとこと。
  例「ごめん、今ちょっと頭が回らない。少ししたらもう一回言って。」、`audio` 付き) が来る。Quest・タブレットはそれを言えばよい

### `GET /tablet/vrm`
妹の VRM (設定 `tablet.vrmPath`、既定 `../Cohabit/Assets/Models/Sis_3b1fdc3d.vrm`)。`model/gltf-binary`、約 16 MB。`ETag` を付けるので 2 回目からは 304

## Quest から

Quest (`tmp/Cohabit`) も同じ PC・同じ口を使う。token を設定していれば Quest も `X-Cohabit-Token` を付ける (`/health` だけは要らない)。
`Say.audio` は `/tablet/api/speech?…` のような**相対 URL** なので、Quest は前に `http://<PC>:<port>` を付けて読む (token があれば `token=` は入っている)。

### `GET /health` — この PC を LAN から探す
token なしで答える (秘密は入っていない)。LLM には聞かないので速い (数 ms)。Quest は LAN を順に叩いて `"app": "cohabit-pc"` を返す相手を探す。
```json
{ "ok": true, "app": "cohabit-pc", "version": "1.1.0", "port": 8000, "time": "…",
  "llm": { "backend": "lan_ollama", "ok": true | false | null, "label": "LAN の Ollama (192.168.0.40)", "where": "lan", "model": "…" },
  "inference": { "parts": { … }, "text": "会話・ノート: … / 声: … / 判断: …" },
  "backends": { "llm": { "ok": true, "detail": "…", "ms": 12 }, "stt": …, "tts": …, "lev": … },
  "allowImages": true }
```
- `llm.backend` は**今いちばん先に頼む** LLM (第一候補に届かず予備で動いているときは予備の名前)。`ok: null` は起動直後でまだ確かめていない
- 自己チェックは 30 秒ごと。`ok: false` でも Quest は頼んでよい (PC が予備に回す / 503 と `say` を返す)

### `POST /tablet/api/answer` — 声で言った答えを判定する
```json
{ "problemId": "…", "answer": "マイナス 4", "from": "quest" }
```
→
```json
{ "correct": true | false | null, "solved": true | false, "say": Say, "heard"?: "−4" }
```
- `answer` は音声認識の文字そのまま。「マイナス 4」「まいなすよん」「2 分の 1」「x イコール 3」「答えは 3.5 です」のような言い方を読む。
  教科は問わない (LLM が模範解答と比べる。値が同じなら形が違っても正解)
- 数 1 つの答えがそのまま合っていれば LLM に聞かずにすぐ `correct: true` (速い・LLM が落ちていても答えられる)。
  数として違うときは LLM が確かめる (LLM が使えなければ `false`)。数でない答えで LLM が使えなければ 503 + `say`
- `null` = 聞き取れない・答えになっていない (`say` が聞き返す)。`false` のとき `say` は答えを言わずに見直しを促す
- `correct: true` で学習記録に残す (`check` で解けたときと同じ: `solved` / `solved_with_answer`)。`false` は間違いの種類を問題の `mistakes` に足す
- 同じ問題・同じ答えは聞き直さない (`"cached": true`)。`problemId` が今の問題と違えば 409

### `POST /tablet/api/check` — Quest のカメラの机の写真
タブレットのノートの代わりに、Quest のカメラで撮った机の写真を送ってよい。
```json
{ "problemId": "…", "trigger": "pause" | "request" | "done", "page": "data:image/jpeg;base64,…", "source": "quest" }
```
→
```json
{ "marks": [], "solved": false, "say": Say | null, "stuck": false, "mistake": true, "readable": true }
```
- `rows` / `changedRows` は要らない (送っても使わない)。行番号が無いので **`marks` はいつも `[]`**
- **`pause` は後回しにできる仕事**: LLM がほかの仕事 (人が待っている問題・ヒント・声の答え・会話など) をしていたら、待たずにすぐ
  **503 `{ "error": { "code": "busy" } }`** (`say` なし) を返す。Quest は「飛ばした」と扱い、次の pause でまた送ればよい。
  `request` / `done` は人が待っているので順番に並ぶ
- `say` が来るのは: 間違いを見つけたとき・`request` (見たことを言う。ノートが読めなければ「よく見えない」)・`done`・解けたとき (ほめる)。
  `pause` で間違いが無ければ `say: null` (黙る)
- `mistake` (はっきりした間違いがあるか)・`readable` (ノートが読めたか) は Quest 向けの追加。`stuck` はタブレットと同じ
- `solved: true` で学習記録に残す。同じ写真・同じ trigger は聞き直さない (`"cached": true`)

### `POST /tablet/api/ask` / `POST /tablet/api/problem`
タブレットと同じ。Quest は `page` を付けずに呼んでよい (付けなければ `marks` は返らない)。
`ask` の `question` は声の質問 (音声認識の文字) をそのまま入れる。`problem` は `from: "next" | "weak" | "topic" | "text"` が使える。
作った問題は SSE でタブレットにも届く

### `POST /tablet/api/presence`
Quest をかぶった / 外したときに `{ "wearing": true | false }`。上と同じ

### LLM の行き先 (PC の中のこと。Quest は気にしなくてよい)
第一候補 `llm.backend` (既定 `lan_ollama` = LAN の Ollama `http://192.168.0.40:11434/v1`、`hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL`、考えさせない) に頼み、
つながらない・時間切れ (つなぐ 4 秒 / 返事 60 秒)・5xx・429 なら、予備 (`cloud_api` はキーがあるときだけ → `local_qwen`) に回す。
予備が答えたら 2 分はそれを先に使う。1 つの LLM には 1 度に 1 つだけ頼む。
**人が待っている要求** (問題・ヒント / 説明・声の答え・会話 `/v1/chat/completions`・タブレットの赤ペン・Quest の `request` / `done`) は順番に並ぶ
(45 秒待っても空かなければ予備へ)。**後回しにできる要求** (Quest の机の写真の `pause`・苦手の要約) は並ばない: LLM が空いていなければすぐ 503 `busy`
(予備にも回さない。苦手の要約は少しあとにやり直す)。だから人の要求が待つのは、多くても今走っている 1 つと、先に並んだ人の要求だけ。
まったく同じ要求 (Quest の再送など) は 1 回だけ頼み、20 秒は同じ答えを返す。返事の `$x^2$` のような LaTeX は素の式 (x²) に直す

## PC が持つもの

- **今の問題** (模範解答つき) と、そのページで出した赤ペン
- **学習記録** `data/learning.jsonl`: 問題・教科・正誤・どこで間違えたか (LLM の短い言葉)・ヒントを使ったか・時間
  (1 行 = 1 問: `t, problemId, subject, problem, goal, from, level, result: "solved" | "solved_with_answer" | "moved_on", mistakes[], hints {hint, explain, answer}, hintsUsed, checks, seconds`。
  設定 `tablet.keepLearning: false` で残さない。PC の画面の「データ」→「学習記録を消す」/「全部消す」で消える)
- **苦手の要約** `data/weak_points.json`: LLM が学習記録から作る短い一覧。「苦手から出す」と、Quest の授業 (案 B) の材料。新しい記録が付くたびに裏で作り直す
- 会話の口調 (妹) はシステムプロンプト 1 か所。中学生以上向け、タメ口、短く、答えは聞かれるまで言わない
