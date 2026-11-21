# Cohabit PC — Quest の Cohabit が使う PC 側のアプリ

[`Cohabit_v4.1.md`](../Cohabit_v4.1.md) 8.1 の「外部 PC」にあたるもの。Quest 3 の Cohabit (`Cohabit`) は、会話・ノートを読む・声を聞く・
話す・判断 (Lev) をすべてこのアプリに頼む。このアプリは、それぞれを本当にやるサーバー (クラウドの API・この PC の
VOICEVOX・`lev serve` など) に中継し、中で何が起きているかを画面に出す。

- **Python の標準ライブラリだけで動く。** `pip install` は要らない (Python 3.9 以上)
- **API キーはこの PC にだけ置く。** Quest には入れない (`config.json`。画面から保存する)
- **画像は保存しない。** 画面に見せるため、最後の数枚をメモリに置くだけ (仕様 11.2)
- **「妹の頭の中」で、妹が何を見て何を決めたかを確かめられる** (観測 → 候補と Lev の点 → 選んだ行動。動きを調べるための画面)

## 起動

```
Windows:  start.bat をダブルクリック
Linux:    ./start.sh
```

起動すると、この PC のブラウザで開くアドレスと、Quest に入れるアドレスが出る。

```
Cohabit PC 1.1.0
  画面 (この PC のブラウザで開く): http://127.0.0.1:8000/
  Quest に入れるアドレス: http://192.168.0.23:8000   (Quest は LAN から自動で探す)
  タブレットで開く: http://192.168.0.23:8000/tablet/
  LLM: LAN の Ollama (192.168.0.40) (hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL)。届かなければ予備へ。様子は画面の「自己チェック」
```

Quest は同じ LAN の `GET /health` を探して自動でつなぐ (`"app": "cohabit-pc"` を返す相手)。見つからないときは、Cohabit の
左コントローラーのメニューボタン (または「設定」と言う) → 「アドレスを変える」で `192.168.0.23` を打って Enter (ポートを書かなければ `:8000`)。
HUD の右上が「● PC: つながっています」になればよい。

**楽にするもの** (画面の上の「つなぐ」「自己チェック」):

- **自己チェック** — 起動直後から裏で 30 秒ごとに、LLM (第一候補と予備。生成はせず一覧を読むだけ)・VOICEVOX・音声認識・Lev を確かめ、
  緑 / 赤と直し方の 1 行を出す。起動はこれを待たない。「今すぐ確かめる」でもう一度
- **ログイン時に自動で起動する** — ボタンを押したときだけ、Linux は systemd --user のサービス (`~/.config/systemd/user/cohabit-pc.service`、
  落ちたら 5 秒後に立ち上げ直す)、Windows はスタートアップに `Cohabit PC.bat` を置く (中身は `start.sh` / `start.bat` を動かすだけ)。
  同じボタンでやめられる。自動起動で動いているときに `./start.sh` を重ねると「ポートを開けない」と出て終わる (画面はもう開ける)
- **VOICEVOX** — 動いていなくて実行ファイルが見つかれば、起動時に立ち上げる (設定 `tts.autoLaunch`: 既定は「見つかれば」、`false` で立ち上げない)。
  探す場所: 設定 `tts.voicevoxPath` → `~/voicevox_engine/run` (`linux-*/run` も) → `~/.voicevox/` → `voicevox_engine` コマンド → `~/Applications/VOICEVOX*.AppImage`
  (Windows は `%LOCALAPPDATA%\Programs\VOICEVOX\vv-engine\run.exe` → `VOICEVOX.exe`)。自己チェックの「VOICEVOX を起動」でも
- **ファイアウォール** — ufw / firewalld が有効なら画面に出し、ボタンで LAN (同じ /24) からこのポートだけを許可する
  (`pkexec ufw allow proto tcp from 192.168.0.0/24 to any port 8000`。管理者の確認が出る)。Windows は初回に聞いてきたら「プライベート ネットワーク」を許可する

## 画面

| 欄 | 何が出るか |
|---|---|
| 上の帯 | Quest がつながっているか (IP と何秒前か)・Quest に入れるアドレス |
| タブレット | タブレットがつながっているか・タブレットで開く URL・今の問題 (模範解答はこの画面にだけ)・最後の赤ペン・苦手の要約・「Quest をかぶった / 外した」「タブレットの妹に言わせる」 |
| 妹の今 | 勉強の状態・今の問題・手が見えているか・迷っていそう (%)・手が止まった秒数・待つ時間 (断られた回数で延びる)・写真の同意・関係 |
| 妹の頭の中 | Quest が送ってくる記録。**判断**は候補ごとの Lev の点 (棒) と選んだもの、決まりで外した候補。観測 (ノート・机の上・聞こえた言葉)・行動 (言ったこと・指差し・問題)・記憶・学習 |
| つなぐ | この PC の LAN のアドレスとポート (大きく)・タブレットの URL・ファイアウォール・「ログイン時に自動で起動する」 |
| 自己チェック | 今答えている LLM (第一候補か予備か・最後に答えたもの・順番待ち)、LLM ごと・VOICEVOX・音声認識・Lev の緑 / 赤と直し方、「VOICEVOX を起動」 |
| 推論の実行先 | 会話・ノート・音声認識・読み上げ・Lev が**どこで**動いているか (クラウド / この PC / LAN)、つながっているか、P50 / P95 の遅延、「確かめる」ボタン |
| 送られてきたもの | Quest から来た画像 (メモリだけ) と、各要求の中身と返事 |
| 設定 | 下の「つなぐ先」。保存すると `config.json` に書く |
| データ | この PC 側の記録を消す (仕様 11.4)。妹の記憶は Quest の中にあり、Quest の設定パネルから消す |

画面はこの PC (127.0.0.1) からしか開けない。LAN の別の機械からは Quest 用の口しか使えない。

## タブレット (ペンで解く → LLM が読む → 妹が赤ペン)

Android タブレットのペンで問題を解くと、この PC の LLM (設定の「会話・ノートを読む」と同じもの。ノートは画像のモデル) がノートを読んで、
妹が赤ペンを入れる。教科は決めない — 問題を作る・読む・計算して確かめる・ヒント・説明はすべて LLM がする。約束は [TABLET_API.md](TABLET_API.md)。

1. このアプリを起動する (上と同じ)。画面の「タブレット」の欄に `http://192.168.0.23:8000/tablet/` のような URL が出る
2. タブレットの Chrome でその URL を開く (同じ LAN。ホーム画面に追加できる)。token を設定しているときは URL に `?token=…` が付いている
3. 問題は「話題を言う / 打つ」「教科書を撮る」「苦手から出す」「続き」で決まる。書くのが止まる・「見て」・「できた」で PC が読む

- タブレットの画面のファイルは `tablet/` (このアプリはそれを `/tablet/` で配るだけ)。妹の VRM は設定 `tablet.vrmPath`
  (既定 `../Cohabit/Assets/Models/Sis_3b1fdc3d.vrm` — Quest のプロジェクトのもの)
- 妹の口調 (タメ口・短く・答えは聞かれるまで言わない) は `tablet_api.py` の `SISTER` 1 か所。相手の呼び方は設定 `tablet.studentName`
- **模範解答はタブレットに渡さない。** LLM が問題と一緒に作り、この PC のメモリにだけ置いて、ノートを読むときに LLM に渡す
- 同じページを何度送られても LLM には 1 回だけ聞く (ページ・問題・きっかけで覚える)。「書くのが止まった」では間違いが無ければ黙る
- 声は VOICEVOX (`/tablet/api/speech`)。VOICEVOX が無ければ 204 を返し、タブレットは字幕だけ出す
- Quest が `/observe` に送る妹の発言はタブレットにも流れる。Quest は `POST /tablet/api/presence { "wearing": true }` で「かぶった」を知らせる
- Groq の無料枠は出力 1000 トークン/分 (`max_tokens` を枠に数える)。このアプリは小さめの `max_tokens` で頼むが、続けて押すと 429 になる —
  タブレットには 429 を返すので、少し待ってやり直す

| タブレットの口 (LAN に公開。token があれば `X-Cohabit-Token` か `?token=`) | |
|---|---|
| `GET /tablet/` | 静的ファイル (`tablet/`)。token なし。`..` で外には出られない |
| `GET /tablet/vrm` | 妹の VRM (ETag つき) |
| `GET /tablet/api/session` | 妹・今の問題・居場所・推論先・苦手の要約 |
| `POST /tablet/api/problem` | `{ from: topic / photo / weak / next / text, … }` → 問題と妹のひとこと |
| `POST /tablet/api/check` | ノートの画像 (行番号入り) → 行ごとの印・解けたか・ひとこと。`source: "quest"` なら Quest の机の写真 (印なし) |
| `POST /tablet/api/ask` | hint / explain / answer |
| `POST /tablet/api/answer` | Quest から: 声で言った答え → `{ correct: true / false / null, solved, say }` |
| `GET /tablet/api/speech?text=…` | WAV。無ければ 204 |
| `GET /tablet/api/events` | SSE (`presence` / `say` / `problem`、15 秒ごとに keep-alive) |
| `POST /tablet/api/presence` / `POST /tablet/api/say` | Quest・この画面から (かぶった / 外した、妹に言わせる) |

## つなぐ先 (設定)

| 役目 | 既定 | 変えるなら |
|---|---|---|
| 会話・ノートを読む (Quest のヒント・タブレットも) | **LAN の Ollama** (`http://192.168.0.40:11434/v1`、`hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL`、考えさせない = `reasoning_effort: "none"`。画像も読める) | 「第一候補」をクラウド API (Groq など) にもできる。「この PC の LLM」にすると、`llama.cpp` の `llama-server` / vLLM / Ollama など OpenAI 互換のサーバー (`http://127.0.0.1:8080/v1`、Ollama なら `http://127.0.0.1:11434/v1` とモデル名 `qwen3.5:9b`) へ。ノートを読むには**画像入力に対応したモデル**を選ぶ (仕様 8.2) |
| 音声認識 | OpenAI 互換の API (`whisper-1`) | `faster-whisper` を入れてあれば「この PC の faster-whisper」(GPU) |
| 読み上げ | VOICEVOX (`http://127.0.0.1:50021`、話者 58 = 猫使ビィ ノーマル) | VOICEVOX を起動しておく。OpenAI 互換の API にもできる |
| 判断 (Lev) | `http://127.0.0.1:8001` | `lev serve --checkpoint interfaze-ai/lev --host 127.0.0.1 --port 8001` (Lev の README の方法で入れる。RTX 5070 Ti で動かす想定)。空にすると使わない — Quest は自分の決まりで判断する |

**LLM は止まらないことを優先する** (`llm.*`):

- 第一候補に**つながらない・時間切れ (つなぐ 4 秒 / 返事 60 秒)・5xx・429** なら予備へ: `lan_ollama` → `cloud_api` (API キーがあるときだけ) → `local_qwen`。
  予備が答えたら 2 分はそれを先に使い、自己チェックで第一候補が戻ったら戻す。途中で切れた・502 / 503 は同じ相手に 1 回だけやり直す。
  400 (要求の形が悪い) は予備に回さない。Quest が `cohabit-chat` / `cohabit-vision` 以外のモデル名を指定したら第一候補だけ。`llm.fallback: false` で予備を使わない
- **1 つの LLM には 1 度に 1 つ**。人が待っている要求 (問題・ヒント・声の答え・会話・赤ペン・「見て」「できた」) は順番待ち
  (45 秒 (`queueWait`) 空かなければ予備へ)。後回しにできる要求 (Quest の机の写真の pause・苦手の要約) は並ばず、空いていなければすぐ 503 `busy`
  (Quest は飛ばす)。人の要求が待つのは、多くても今走っている 1 つだけ
- 画面の「**今 AI がしていること**」: LLM が今している仕事 (何を・何秒・どの LLM)・順番待ち・Quest の段 (`/observe` の status の `stage`:
  listening / transcribing / thinking:目的 / speaking / error)・音声認識 (届いた回数・何秒前・聞こえた文字か失敗の理由。0 回なら
  「Quest から音声が 0 回届いています」と赤で出る — マイクが動いていない)
- まったく同じ要求 (Quest の再送など) は 1 回だけ頼み、20 秒は同じ答えを返す
- 返事の簡単な LaTeX (`$x^2$`・`\frac{1}{2}`) は素の式 (x²・1/2) に直す (JSON の返事は値だけ)。プロンプトでも Unicode で書くよう頼んでいる
- どこにも届かなければ 503 と `"say"` (妹が言えるひとこと) を返す。画面の「自己チェック」に、どれが落ちているかと直し方が出る

Lev には、Quest が**決まりで絞った候補だけ**が来る (勉強中に話しかけてよいか・物について聞くか)。Lev は順位をつけるだけで、
候補に無い行動は選べない (仕様 6.4 / 19.5)。Lev の `/v1/systemone` に `choice` の質問として渡し、確率をそのまま Quest に返す。

## Quest からの口

| | |
|---|---|
| `POST /v1/chat/completions` | OpenAI 互換。モデル名 `cohabit-chat` / `cohabit-vision` は今答える LLM のモデルに置き換える。画像入りなら画像のモデル。答えた LLM はヘッダー `X-Cohabit-Backend`。どこにも届かなければ 503 `{ error: { code: "llm_down" }, say }` |
| `POST /v1/audio/transcriptions` | OpenAI 互換 (multipart) |
| `POST /v1/audio/speech` | OpenAI 互換。返すのは WAV |
| `POST /decision/evaluate` | `{ kind, state, candidates }` → `{ scores, choice, confidence, source: "lev" }`。Lev が無ければ 503 |
| `POST /observe` | `{ events: [...] }` (仕様 8.4 の形)。画像・音声の欄は捨てる |
| (消した口) | `/study/verify` (一次方程式の検算) と `/control/next`・`/api/control` (画面からの操作ボタン) は無くした。古い Quest が呼んでも 404 を返すだけで、記録にも遅延の数にも残らない |
| `GET /health` / `GET /metrics` | つながり・推論の実行先 (Quest の HUD の一番下に出る) / 遅延。`/health` は token なしで答え、`app: "cohabit-pc"`・`version`・`port`・`llm: { backend, ok }` を含む (Quest が LAN から探す。LLM には聞かない) |
| `/tablet/api/answer` ほか | Quest もタブレットの口 (問題・机の写真・声の答え・ヒント) を使う。[TABLET_API.md](TABLET_API.md) の「Quest から」 |

設定の `token` を決めると、Quest は `X-Cohabit-Token` を付けないと断られる (Quest 側は `Cohabit` のスクリプト「設定」の `CONFIG.token`)。

## データ

- `config.json` — 設定 (API キーを含む。`.gitignore` 済み)
- `data/events-日付.jsonl` — 観測・判断・行動の記録 (画像・音声なし)。評価 (仕様 12 / 26) に使う。設定で止められる。画面の「データ」で消せる
- `data/learning.jsonl` — タブレットの学習記録 (1 行 = 1 問: 問題・教科・結果・間違いの種類・ヒントの回数・かかった秒数。画像なし)。
  `tablet.keepLearning: false` で止められる。画面の「データ」→「学習記録を消す」/「全部消す」で消せる
- `data/weak_points.json` — 学習記録から LLM が作った苦手の要約 (「苦手から出す」に使う)。学習記録と一緒に消える
- ノートや教科書の写真は保存しない (画面に見せるため最後の数枚をメモリに置くだけ)

## 確かめたこと / 確かめていないこと

- **LAN の Ollama で確かめた (2026-10-27、Ollama 0.30.5、`hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL`、考えさせない)**: 自己チェックは緑
  (`/v1/models` にモデルがある)。「2x + 3 = 11 を解け」から模範解答 x = 4 (16 秒。最初の 1 回は遅い / 2 回目 5 秒)。声の答え「なな」→ `correct: false`
  (答えを言わずに見直しを促す、3.9 秒)、「わかんない」→ `null` (聞き返す)、「エックスは よん です」→ LLM なしで `true` (0 秒)。
  机の写真 (ImageMagick で作った、斜めの紙に 2x = 11 + 3 → x = 7 と書いたもの) を `pause` で → `mistake: true`「2行目、3を右に移すとき符号は？」(9 秒)、
  正しい写真を `done` で → `solved: true` (7.5 秒)。本物の Quest のカメラの写真・手書きはまだ
- `python3 -m unittest test_cohabit_pc test_tablet_api` — 上のほかに: 第一候補が断る / 時間切れ / 503 → 予備へ (503 は 1 回やり直してから)、
  400 は回さない、予備に張り付いて戻る、キーの無いクラウドは予備にしない、1 つの LLM に同時に 1 つ、同じ要求は 1 回、全部だめなら 503 と say、
  `/health` は token なしで秘密を出さない、LaTeX を直す (JSON は壊さない)、声の数の読み方、Quest の机の写真と声の答え、壊れた config.json でも起動、
  自動起動 (偽の HOME と偽の systemctl で unit を書く・消す)・ファイアウォール (偽の ufw / pkexec)・VOICEVOX (偽の HOME と偽の Popen)。
  本物の systemctl / pkexec / VOICEVOX は動かしていない

- `python3 -m unittest test_tablet_api` — 偽の LLM・偽の VOICEVOX で、タブレットの口を全部呼ぶ (模範解答を返さない・
  画像は画像のモデルへ・考えた跡 / コードの囲い / 末尾のカンマ / 途中で切れた JSON を読む・読めなければ 1 回だけ直させる・
  pause で黙る・同じページは聞き直さない・学習記録と苦手の要約・消す・token・`..` で外に出られない・MIME・SSE・ETag・VOICEVOX が無いと 204)
- **タブレットの口を本物で確かめた (2026-10-25、Groq `qwen/qwen3.8-27b`)**: 「2x + 3 = 11 を解け」を渡すと模範解答 (x = 4) を作り (0.5 秒)、
  わざと 2 行目を間違えた (2x = 14 → x = 7) 行番号入りのノートの画像 (Python で描いた PNG) を「見て」で送ると、3 行を正しく読み、
  2 行目だけに `mistake`「11-3、足してない？」と妹のひとことを返した (1.0 秒)。ヒントは「定数項を右辺に移項する時は符号を反転させてね。」(0.3 秒)。
  「二次方程式の解の公式」からは x² − 5x + 6 = 0 と正しい模範解答 (0.7 秒)。4 回続けたところで Groq の出力トークン/分の枠に当たった (→ `max_tokens` を下げ、429 をそのまま返すようにした)
- 手書きの字 (本物のペン) と、タブレット自身・VOICEVOX をつないだ通しは、まだ確かめていない
- `python3 -m unittest test_cohabit_pc` — 偽の上流 (OpenAI 互換・VOICEVOX・Lev) を立てて、全部の口を Quest と同じ形で呼ぶ
  (モデル名の置き換え・API キーを PC 側で付ける・画像を断る設定・multipart の中継・VOICEVOX の 2 段の呼び出し・
  Lev への質問の形・記録から画像を落とす・消した口が静かに 404・上流が落ちたら 503 (と say) で返してアプリは落ちない)
- **本物で確かめた (2026-10-12、RTX 5070 Ti 16 GB)**: Ollama 0.33 の `qwen3.5:9b`・VOICEVOX 0.25.2・`lev serve --checkpoint interfaze-ai/lev`
  (Lev は `uv venv --python 3.12` の中に `pip install "lev[serve] @ git+https://github.com/InterfazeAI/lev#subdirectory=packages/lev"`)。
  Quest が送るのと同じ要求で:
  - ノートを読む: 3 行の式を正しく読んだ (0.8 秒)。問題を作る (0.3 秒)。読み上げ: WAV (0.3 秒)
  - Lev: 1 回 0.1 秒。書いている → 黙る、頼まれた → ヒント (確率 0.88)、一度聞いた物 → 聞かない、で筋が通っていた
  - **見つけて直したこと**: Qwen3.5 は「考えてから答える」ので、考える分で max_tokens を使い切り**返事が空**だった。
    「この PC の LLM」のときは考えさせない (`thinking: false`。Ollama には `reasoning_effort: "none"` を送る)
  - **ヒントは LLM に任せない** (Quest 側で式から決める): 9B に考えさせると半分ほど数学が間違い、言い直させるだけでも中身が変わった
  - **自由な会話の質は足りない**: 決まりを口に出す・記録に無い時刻を言う・です・ます が混ざる。
    会話は仕様どおりクラウド API (第一候補) か、より大きいモデルで確かめたい
- **Groq (2026-10-12)**: 会話・ノート・音声認識を Groq に (`config.json` の `cloud_api`: `baseUrl` `https://api.groq.com/openai/v1`、
  モデル `qwen/qwen3.8-27b`、`"thinking": false`。音声認識は `stt.baseUrl` を同じにして `whisper-large-v3-turbo`)。
  会話 0.3 秒 (タメ口・決まりを口に出さない)、ノート 0.7 秒 (正しく読めた)、問題作り 0.2 秒 (模範解答が正しい)、音声認識 0.4 秒 (VOICEVOX の声を正しく聞き取れた)。
  Groq は Python の既定の User-Agent を 403 で断るので、このアプリは `CohabitPC/版` を名乗る。
  会話を Groq にすると GPU は Lev だけになり、下のメモリの問題は起きない
- **GPU のメモリ**: `qwen3.5:9b` (6.5 GB) と Lev (BF16 で約 8.6 GB) と画面で 16 GB を超える。両方を載せると Lev は起動時にメモリ不足で落ち、
  先に Lev を載せると Ollama の一部が CPU に回って会話が 2〜8 秒になる。会話をクラウド API にするか、Lev を 4bit にする (仕様の目標) と両立する
- 音声認識 (OpenAI の API / faster-whisper) は、まだ本物で試していない (API キーが無く、faster-whisper も入っていない)
- Lev の要求の形は Lev のリポジトリの `packages/lev/src/lev/types.py` (2026-10-12 時点) に合わせた
