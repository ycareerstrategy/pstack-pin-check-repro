# pstack-pin-check-repro

Zenn記事（pstack実践ガイド）の再現用リポジトリ。
記事Markdown内のコミット固定画像URLが、図の描き直し後も古いshaを指す問題を題材にする。

## pin-check ツール（記事の設計に準拠）

`tools/pin-check/check_pins.py` — Python標準ライブラリと git だけで動く。

- 固定した sha の時点のファイルと HEAD のファイルを **git の blob ID** で比べる
  （`git cat-file --batch-check` にまとめて問い合わせ）
- 状態は6つ：`CURRENT` / `STALE` / `MISSING_AT_HEAD` / `MISSING_AT_PIN` /
  `UNKNOWN_COMMIT` / `NOT_A_FILE`。CIで失敗にするか・付け替えてよいかは
  コード内の `POLICY` 表1つで決める（`NOT_A_FILE` だけは報告のみで通る）
- 終了コード：失敗する状態がなければ `0`、あれば `1`、引数の誤りか git のエラーなら `2`
- `--fix` の付け替え先は HEAD ではなく、そのファイルを最後に変えたコミット
  （そのコミットの blob が HEAD と同じことも確かめる）。書き換えは一時ファイル＋
  `os.replace` で行い、書き換え後にもう一度検査する。2回目の `--fix` は何も変えない

使い方：

```
python3 tools/pin-check/check_pins.py          # 読むだけ
python3 tools/pin-check/check_pins.py --fix    # STALE を付け替え
python3 -m unittest discover -s tests -v       # テスト
```

CI（`.github/workflows/pin-check.yml`）は PR と main への push で動き、
権限は `contents: read` のみ。古いコミットと比べるため `fetch-depth: 0` で
全履歴を取得する。
