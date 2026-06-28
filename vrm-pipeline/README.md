# vrm-pipeline

[![CI](https://github.com/yukineko/3d-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/yukineko/3d-pipeline/actions/workflows/ci.yml)

VRM 生成改善パイプライン — `prompt × 自分の傾向 × 結果評価` の蓄積で生成を改善する個人ツール。

## 概要

```
[VRoid Studio] → VRM 投下 → [watch] → [render.py on Blender] → [ledger] → 台帳
                                                  ↓
                                         eval-A: golden 距離チェック
```

- **render.py**: Blender headless で正準三面焼き（body3+face4）+ eval-A pHash
- **ledger**: SQLite 台帳 CLI（Rust）
- **dist**: pHash 距離・ノイズフロア測定（Rust）
- **vrm-watch**: VRM 投下を監視してパイプラインを自動起動（Rust）

---

## セットアップ

### 前提

- Blender 4.2.0 LTS + VRM Add-on for Blender 2.20.77
- Rust 1.79+
- Python 3.10+（render.py の依存ライブラリ用）

### Rust バイナリのビルド

```bash
cd vrm-pipeline
cargo build --release
# → target/release/{ledger,dist,vrm-watch}
```

### Python 依存（render.py の外部スクリプト連携用）

```bash
pip install -r vrm-pipeline/requirements.txt
```

### Docker（推奨：Blender 環境を完全 pin）

```bash
docker build -f vrm-pipeline/Dockerfile vrm-pipeline/ -t vrm-pipeline:latest
```

---

## Phase 0 — 最小台帳（価値検証）

### 1. 台帳を初期化

```bash
./target/release/ledger init
# → ~/.vrm-pipeline/ledger.db を作成
```

### 2. VRM を手動でレンダして台帳に登録

```bash
blender --background --python vrm-pipeline/render.py -- \
  --vrm /path/to/character.vrm \
  --output /tmp/vrm-out/

./target/release/ledger insert \
  --prompt "silver hair, twin-tail, blue eyes" \
  --r0-dir /tmp/vrm-out/
```

### 3. 採用を記録（任意）

```bash
./target/release/ledger adopt <record-id>
```

### 4. 台帳を確認

```bash
./target/release/ledger list
./target/release/ledger stats
```

---

## Phase 1 — 自動監視 + フロア較正 + eval-A

### 5. golden VRM を用意

```bash
mkdir -p ~/.vrm-pipeline/golden/my-char/
# character_golden.vrm を投下してレンダ済みディレクトリを golden として使用
blender --background --python vrm-pipeline/render.py -- \
  --vrm /path/to/golden.vrm \
  --output ~/.vrm-pipeline/golden/my-char/
```

### 6. ウォッチャー起動

```bash
./target/release/vrm-watch \
  --watch-dir ~/VRoidProjects/export/ \
  --output-dir /tmp/vrm-renders/ \
  --render-py vrm-pipeline/render.py \
  --ledger ~/.vrm-pipeline/ledger.db \
  --ledger-bin ./target/release/ledger \
  --golden-dir ~/.vrm-pipeline/golden/my-char/ \
  --noise-floor-threshold 12.0 \
  --prompt "silver hair, twin-tail"
```

VRoid Studio から VRM をエクスポートすると自動でレンダ → 台帳 INSERT が走ります。

### 7. 手直し後の edit_dist 計算（R0→R1）

最終 VRM を調整後に投下 → R1 ディレクトリができたら:

```bash
./target/release/dist measure \
  --r0 /tmp/vrm-renders/character_r0/ \
  --r1 /tmp/vrm-renders/character_r1/
```

---

## コマンドリファレンス

### render.py

```
blender --background --python render.py -- \
  --vrm <path>          VRM ファイル
  --output <dir>        出力ディレクトリ
  [--golden <dir>]      eval-A: golden レンダと比較するディレクトリ
  [--resolution 768]    出力解像度 (px, デフォルト 768)
```

出力: `body_front.webp`, `body_side.webp`, `body_back.webp`, `face_front.webp`, `face_L.webp`, `face_R.webp`, `face_34.webp`, `manifest.json`

### ledger

```
ledger init
ledger insert --prompt <text> [--r0-dir <dir>] [--generation-params <json>] [--parent-id <id>]
ledger adopt <id>
ledger list [--limit 20]
ledger stats
ledger get --id <id>            レコードを JSON で表示
ledger tree [--root <id>]       派生系統を樹形図で表示（--root で部分木のみ）
```

### derive.py — 既存レコードを起点にした派生生成

既存レコードの prompt を起点に、変更指示を Gemini で適用して新しい prompt を作る。
生成された子レコードは `--parent-id` で親に紐づくため、`ledger tree` に系統が残る。

```
# 派生プロンプトを標準出力に表示
python derive.py --parent-id <id> --change "脚を金属製に、座面を赤革に" \
  [--db-path ~/.vrm-pipeline/ledger.db] [--model gemini-2.5-flash] [--dry-run]

# drop-zone 用の <name>.prompt + <name>.params.json（parent_id 入り）を書き出す
python derive.py --parent-id <id> --change "脚を金属製に" \
  --emit-drop ./drop --name chair_v2
```

`pipeline.py` は `<stem>.params.json` の `parent_id` を読み取り、INSERT 時に親へリンクする。
つまり「起点を選んで変更 → 生成 → 系統が樹形図になる」が成立する。

### dist

```
dist phash <img1> <img2>
dist pixel <img1> <img2>
dist floor <img_path>
dist measure --r0 <dir> --r1 <dir>
```

### vrm-watch

```
vrm-watch \
  --watch-dir <dir>               監視ディレクトリ
  --output-dir <dir>              render 出力先
  --render-py <path>              render.py のパス
  [--blender <path>]              blender バイナリ (デフォルト: blender)
  [--ledger <path>]               DB パス (デフォルト: ~/.vrm-pipeline/ledger.db)
  [--ledger-bin <path>]           ledger バイナリ (デフォルト: ledger)
  [--golden-dir <dir>]            eval-A 用 golden ディレクトリ
  [--noise-floor-threshold <f>]   eval-A アラート閾値 (デフォルト: 10.0)
  [--prompt <text>]               台帳 INSERT 時の prompt
```

---

## 段階導入計画

| Phase | 内容 | 状態 |
|-------|------|------|
| 0 | 最小台帳（prompt + front 1枚 + asset_ref）| ✅ 実装済み |
| 1 | edit_dist (b) + eval-A golden フロア較正 | ✅ 実装済み |
| 2 | 三面化・DINOv2 埋め込み・VLM タグ | 未着手 |
| 3 | 傾向反映（prompt 自動補正）| 未着手 |
