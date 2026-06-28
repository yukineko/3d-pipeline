---
description: headless VRoid VRM 調整 (expression / material color / height)
argument-hint: --in <vrm> --out <vrm> --adjustments-file <json> [--blender <path>]
allowed-tools: Bash(python:*)
---

既存の VRM を Blender の VRM アドオン経由でヘッドレスに編集します。新しい VRM を
生成するのではなく、入力 VRM をベースに表情・マテリアル色・身長スケールを上書き
した派生 VRM を書き出します。

```
python render/vrm_edit.py --in <input.vrm> --out <output.vrm> --adjustments-file <adjustments.json> $ARGUMENTS
```

`--adjustments-file` には調整内容を表す JSON ファイルを渡します。スキーマ:

- `expressions`: `happy` / `angry` / `sad` / `relaxed` / `surprised` / `blink`
  をそれぞれ `0..1` の強度で指定 (shape key にベイクされます)。
- `materials`: `hair` / `skin` / `eye` / `outfit` をそれぞれ `[r, g, b, a]`
  (各成分 `0..1`) で指定。
- `height_scale`: `0.5..2.0` の身長スケール (transform_apply で焼き込まれます)。

例:

```json
{
  "expressions": {"happy": 0.8, "blink": 0.2},
  "materials": {"hair": [0.1, 0.05, 0.02, 1.0]},
  "height_scale": 1.1
}
```

Blender の場所は `--blender <path>` または環境変数 `BLENDER_PATH` で指定できます。
サブプロセスのタイムアウトは `VRM_SUBPROCESS_TIMEOUT` (秒) で調整できます。

If no arguments were given, run `python render/vrm_edit.py --help`.
