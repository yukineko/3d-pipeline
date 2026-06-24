"""render.py - Blender headless render entry point for vrm-pipeline.

Invoked by vrm-watch or directly via:
    blender --background --python render.py -- [args]
"""
import bpy
import sys
import argparse


def parse_args():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    parser = argparse.ArgumentParser(description="VRM pipeline render script")
    parser.add_argument("--input", required=True, help="Input .vrm or .vroid file path")
    parser.add_argument("--output", required=True, help="Output image path")
    return parser.parse_args(argv)


def main():
    args = parse_args()
    print(f"render.py: input={args.input} output={args.output}")
    # TODO: implement VRM import and render logic


if __name__ == "__main__":
    main()
