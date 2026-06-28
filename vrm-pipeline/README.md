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

### パラメトリック人体生成 (MPFB2)

`render/generate_body.py` は MakeHuman のマクロ修飾子を使って人体ベースメッシュを
新規生成し、`game_engine` リグを付与・VRM humanoid ボーンを割り当て・モーフを
ジオメトリに焼き込んで `.vrm` をエクスポートする（`vrm_edit.py` が既存 VRM を
*編集* するのに対し、こちらは *生成* する）。

実行時要件:

- **MPFB2**（MakeHuman Plugin for Blender、CC0 ベースメッシュ）。`blender --addons mpfb`
  または `bpy.ops.preferences.addon_enable(module="mpfb")` で有効化する。
- **Blender 4.2 LTS** と **VRM_Addon_for_Blender**。
- HumGen3D は **使わない**（有料コンテンツパックが必要なため）。

正規モーフキーは `body_params.BODY_MORPH_KEYS`（gender / age / height / weight /
muscle / proportions / bodyfat / head_size、各 0.0..1.0）。これらは
`generate_body.MODIFIER_MAP` で MPFB2 の MakeHuman target 文字列へ対応づけるが、
**正確な target 名は MPFB2 のバージョンで変わる**ため、実行時に MPFB2 の target
名前空間に対して検証する必要がある（`bodyfat` は独立 target が無く universal の
`Weight` を `weight` と共有する点に注意）。

ホスト側からの利用:

```python
from render.generate_body import generate_body
from render.body_params import resolve_body_morphs

generate_body(resolve_body_morphs("背の高いアスリート体型の女性"), "/out/char.vrm")
```

Blender CLI から直接:

```
blender --background --python render/generate_body.py -- \
  --morphs-file morphs.json --out char.vrm [--report-file report.json]
```

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

## はじめてのアバター (prompt → VRM)

このプロジェクトの前提は「ユーザーの美的センスは低い」こと。だから凝った指定は不要で、
**プロンプトを書くだけ**でよい。良デフォルトは吟味済みの**プリセット**が供給し、Gemini は
その上に**まばらな上書き (sparse override)** を重ねるだけ。この層構造は
`vroid_params.resolve_vrm_adjustments(prompt, preset_name=..., image_path=..., model=...)`
が担い、プリセットの baseline に Gemini が明示した差分だけを載せて完全な調整 dict を返す。

調整 dict のスキーマ（`vroid_params.py` 参照）:

```jsonc
{
  "expressions": { "happy": 0..1, "angry": 0..1, "sad": 0..1,
                   "relaxed": 0..1, "surprised": 0..1, "blink": 0..1 },
  "materials":   { "hair": [r,g,b,a], "skin": [r,g,b,a],
                   "eye": [r,g,b,a], "outfit": [r,g,b,a] },  // 各成分 0..1
  "height_scale": 0.5..2.0   // 既定 1.0
}
```

### VRoid 編集フロー（おすすめ）

既存の VRoid VRM を**ベース**に、プロンプト由来の差分だけを Blender (VRM addon) で適用して
再エクスポートする経路。`pipeline.py` の `handle_prompt` が `<stem>.params.json` に `base_vrm`
を見つけると、この分岐を選ぶ。

1. **drop ファイルを作る** — `derive.py` で `<name>.prompt` + `<name>.params.json` を書き出す
   （`§ derive.py` 参照）。または手書きで `my_avatar.prompt`（変更指示テキスト）と
   `my_avatar.params.json`（最低限 `{"vroid_edit": true, "base_vrm": "/path/to/base.vrm"}`、
   任意で `preset` / `change` / `image_ref`）を用意する。

   ```bash
   python derive.py --parent-id <id> --change "笑顔多め、少し背を高く" \
     --emit-drop ./drop --name my_avatar
   ```

2. **ウォッチャーを起動** — `§6 ウォッチャー起動` と同じく `vrm-watch` を監視ディレクトリへ向ける。
3. **2 ファイルを drop-zone に投下** — `.prompt` と `.params.json` を監視ディレクトリへ置く。
4. **パイプラインが自動実行** — `resolve_vrm_adjustments` →
   `edit_vrm(base_vrm, out, adjustments, blender_path=...)` → VRM 品質ゲート検証 →
   レンダ → ledger INSERT。出力 VRM は render 出力先の
   `renders/<stem>/generated/<stem>.vrm` に生成される。

> 画像から作りたい場合は `params.json` に `image_ref` を入れると、Hyper3D で画像→GLB を生成し
> `glb_to_vrm` で VRM 化する別分岐が走る。

### 直接呼ぶ (host API)

watch を介さずホスト Python から直接呼ぶこともできる。

```python
from render.vrm_edit import edit_vrm
# edit_vrm(in_vrm, out_vrm, adjustments, blender_path=None) -> str
out = edit_vrm("base.vrm", "out.vrm", {"expressions": {"happy": 0.8}, "height_scale": 1.05})

# GLB → VRM:
from render.vrm_convert import glb_to_vrm
# glb_to_vrm(glb_path, vrm_path, blender_path=None) -> str
vrm = glb_to_vrm("model.glb", "model.vrm")
```

関連環境変数:

- `BLENDER_PATH` — blender バイナリの場所（`blender_path` 引数未指定時のフォールバック、既定 `blender`）。
- `VRM_SUBPROCESS_TIMEOUT` — Blender サブプロセスのタイムアウト秒数（既定 600）。

---

## 段階導入計画

| Phase | 内容 | 状態 |
|-------|------|------|
| 0 | 最小台帳（prompt + front 1枚 + asset_ref）| ✅ 実装済み |
| 1 | edit_dist (b) + eval-A golden フロア較正 | ✅ 実装済み |
| 2 | 三面化・DINOv2 埋め込み・VLM タグ | 未着手 |
| 3 | 傾向反映（prompt 自動補正）| 未着手 |
