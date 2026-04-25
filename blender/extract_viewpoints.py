"""Run trajectory_optimizer in Blender to plan a first-person trajectory,
but skip Cycles rendering. Output: viewpoints.json (plus trajectory_data.csv
that the upstream code writes).

Must be launched via `blender --background --python` so `bpy` is available.

Example:
    blender --background <scene.blend> --python extract_viewpoints.py -- \\
        --blend /tmp/full_scene/scene.blend \\
        --output /tmp/full_scene/trajectory/ \\
        --fps 24

The emitted viewpoints.json schema:
{
  "fps": 24,
  "source_units": {"position": "meters", "rotation": "degrees"},
  "axis_convention": "blender",   # +Z up, camera looks -Z, Euler XYZ
  "viewpoints": [
    {
      "frame": 0,
      "location_m": [x, y, z],
      "rotation_euler_deg": [rx, ry, rz],
      "fov_deg": 60.0,
      "action": "translate" | "rotate"
    },
    ...
  ]
}
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path


def _strip_blender_argv() -> list[str]:
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1:]
    # Allow running outside blender as well (for dry tests of the arg parser)
    return sys.argv[1:]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--blend", type=Path, required=True,
                        help="Input .blend scene (with a placed camera)")
    parser.add_argument("--output", type=Path, required=True,
                        help="Output directory for trajectory CSV + viewpoints.json")
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--target-limit", type=int, default=None,
                        help="Max number of viewpoint targets to visit")
    parser.add_argument("--samples", type=int, default=2000)
    return parser.parse_args(_strip_blender_argv())


def patch_out_rendering(output_dir: Path) -> None:
    """Short-circuit camera_traj right after trajectory_data.csv is flushed.

    `bpy.ops` is an RNA wrapper and cannot be reassigned, so we wrap
    csv.DictWriter.writerows — camera_traj only calls it once (for the
    trajectory rows) and the call sits immediately before the keyframe
    loop + Cycles render. We raise SystemExit(0) as soon as the CSV
    hits disk.
    """
    import csv as _csv

    target = output_dir / "trajectory_data.csv"
    orig_writerows = _csv.DictWriter.writerows

    def _early_exit_writerows(self, rows):
        result = orig_writerows(self, rows)
        if target.exists() and target.stat().st_size > 0:
            print(f"[extract_viewpoints] trajectory CSV written ({target.stat().st_size} bytes); skipping render")
            raise SystemExit(0)
        return result

    _csv.DictWriter.writerows = _early_exit_writerows  # type: ignore[assignment]


def convert_csv_to_json(csv_path: Path, out_path: Path, fps: int, fov_deg: float) -> int:
    """Translate trajectory_data.csv -> viewpoints.json in our stable schema."""
    viewpoints: list[dict] = []
    with csv_path.open() as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            # Blender camera: rot_euler = Euler((yaw, 0.0, pitch), "XYZ")
            yaw_rad = float(row["yaw"])
            pitch_rad = float(row["pitch"])
            viewpoints.append({
                "frame": i,
                "location_m": [float(row["x"]), float(row["y"]), float(row["z"])],
                "rotation_euler_deg": [
                    math.degrees(yaw_rad),
                    0.0,
                    math.degrees(pitch_rad),
                ],
                "fov_deg": fov_deg,
                "action": row.get("action", "translate"),
            })

    payload = {
        "fps": fps,
        "source_units": {"position": "meters", "rotation": "degrees"},
        "axis_convention": "blender",
        "viewpoints": viewpoints,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    return len(viewpoints)


def main() -> int:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    import bpy  # deferred import so `--help` works without bpy

    bpy.ops.wm.open_mainfile(filepath=str(args.blend))

    # Grab active camera FOV before planning (Blender stores it in radians)
    cam_obj = bpy.context.scene.camera
    if cam_obj is None:
        # camera_traj also looks up "camera_0_0"; fall back there
        cam_obj = bpy.data.objects.get("camera_0_0")
    fov_deg = 60.0
    if cam_obj is not None and cam_obj.type == "CAMERA":
        fov_deg = math.degrees(cam_obj.data.angle)

    patch_out_rendering(args.output)

    from infinigen_examples.trajectory_optimizer import (
        BatchTrajectoryConfig,
        camera_traj,
    )

    cfg = BatchTrajectoryConfig()
    # BatchTrajectoryConfig holds a handful of render-oriented knobs; the
    # fields actually consumed by the planning phase are set here if present.
    for attr, value in (("target_limit", args.target_limit),
                        ("samples_per_target", args.samples),
                        ("samples", args.samples)):
        if value is not None and hasattr(cfg, attr):
            setattr(cfg, attr, value)

    csv_path = args.output / "trajectory_data.csv"
    try:
        camera_traj(args.blend, args.output, cfg)
    except SystemExit as exc:
        # Expected: our csv hook short-circuits once trajectory_data.csv exists.
        if exc.code not in (0, None):
            raise

    if not csv_path.exists():
        raise RuntimeError(f"trajectory CSV missing: {csv_path}")

    json_path = args.output / "viewpoints.json"
    n = convert_csv_to_json(csv_path, json_path, fps=args.fps, fov_deg=fov_deg)
    print(f"[extract_viewpoints] wrote {n} viewpoints -> {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
