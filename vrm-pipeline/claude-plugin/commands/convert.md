---
description: GLB→VRM 変換 (headless Blender)
argument-hint: --glb <glb> --out <vrm> [--blender <path>]
allowed-tools: Bash(python:*)
---

GLB ファイルを Blender 経由でヘッドレスに VRM へ変換します。入力 GLB を読み込み、
VRM として書き出します。

```
python render/vrm_convert.py --glb <model.glb> --out <model.vrm> $ARGUMENTS
```

Blender の場所は `--blender <path>` または環境変数 `BLENDER_PATH` で指定できます。
サブプロセスのタイムアウトは `VRM_SUBPROCESS_TIMEOUT` (秒) で調整できます。

If no arguments were given, run `python render/vrm_convert.py --help`.
