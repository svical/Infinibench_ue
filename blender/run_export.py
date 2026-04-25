"""Wrapper around infinigen.tools.export so it can be driven from
`blender --background --python`. Infinigen's CLI uses argparse which
does not tolerate Blender's `--` separator, so we strip it here.
"""
import sys

if "--" in sys.argv:
    sys.argv = [sys.argv[0]] + sys.argv[sys.argv.index("--") + 1:]

from infinigen.tools.export import main, make_args  # noqa: E402

main(make_args())
