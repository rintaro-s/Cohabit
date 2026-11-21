# Cohabit

Quest 3 の中で、キャラクター(VRM)と同じ部屋で暮らす MR アプリ。会話・声・判断は PC 側のアプリが受け持つ。

| フォルダ | 中身 |
|---|---|
| [`Cohabit/`](Cohabit/README.md) | Quest 3 側。[Arsist](https://github.com/rintaro-s/Arsist-EN) のプロジェクト(エディタで開いてビルドする) |
| [`CohabitPC/`](CohabitPC/README.md) | PC 側(Python)。クラウドの API・VOICEVOX・Lev への中継と、タブレット用の画面 |
| [`Cohabit_v4.1.md`](Cohabit_v4.1.md) | 仕様書 |

## 動かす

1. PC: `CohabitPC/config.example.json` を `config.json` にコピーし、`CohabitPC/start.sh`(Windows は `start.bat`)で起動する。API キーなどは画面の「設定」で入れる。
2. Quest: Arsist のエディタで `Cohabit/` を開いてビルドする。拡張機能 `quest-room` / `quest-input` / `quest-hands`(Arsist の `samples/extensions`)と `vrm10`(公式)が必要。

`config.json` と `data/`(API キーと会話の記録)はリポジトリに入れない(`CohabitPC/.gitignore`)。
