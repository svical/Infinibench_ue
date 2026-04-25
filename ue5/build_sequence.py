"""Build a LevelSequence from an Infinigen viewpoints.json inside UE5.

Invoke via the UnrealEditor-Cmd.exe headless pipe:

    UnrealEditor-Cmd.exe <path>.uproject \
        -ExecutePythonScript="build_sequence.py -viewpoints=<path>\viewpoints.json \
                              -outSeq=/Game/InfiniBench/S_Traj"

What it does:
1. Load viewpoints.json (Blender-convention, meters + degrees)
2. Convert to UE units/axes:
     Blender XYZ (right-handed, +Z up, cam -Z) -> UE (left-handed, +Z up, cam +X)
     position: (bx, by, bz)  ->  (bx*100, -by*100, bz*100)  [cm]
     rotation: apply a camera-frame correction so the UE camera ends up
               pointing where the Blender camera pointed
3. Spawn a CineCameraActor if needed, then create a LevelSequence asset and
   a MovieScene3DTransformTrack with one keyframe per viewpoint.
4. Save the sequence asset so render_mrq.py can pick it up.

Assumes the target level already has the Infinigen USD imported and visible.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Iterable

import unreal  # type: ignore[import-not-found]


# ---------------------------------------------------------------------------
# argv handling. UE passes args via -Key=Value flags inside -ExecutePythonScript.
# ---------------------------------------------------------------------------

def _parse_ue_script_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--viewpoints", required=True,
                        help="Path to viewpoints.json produced on the Blender side")
    parser.add_argument("--outSeq", default="/Game/InfiniBench/S_Traj",
                        help="UE asset path for the generated LevelSequence")
    parser.add_argument("--map", default=None,
                        help="UE map asset to load before spawning the camera, "
                             "e.g. /Game/InfiniBench/Maps/DefaultMap. If omitted, "
                             "the camera is spawned into whatever map is currently open.")
    parser.add_argument("--cameraLabel", default="InfiniBenchCamera",
                        help="Label of the CineCameraActor to drive")
    parser.add_argument("--fps", type=int, default=None,
                        help="Override the FPS from viewpoints.json")
    parser.add_argument("--focalLength", type=float, default=10.0,
                        help="CineCamera focal length in mm")
    # UE forwards extra garbage in sys.argv; use parse_known_args
    args, _ = parser.parse_known_args()
    return args


# ---------------------------------------------------------------------------
# coordinate conversion
# ---------------------------------------------------------------------------

def blender_to_unreal_location(xyz_m: Iterable[float]) -> unreal.Vector:
    x, y, z = xyz_m
    # Blender meters -> UE centimeters; mirror Y to swap handedness
    return unreal.Vector(x * 100.0, -y * 100.0, z * 100.0)


def blender_to_unreal_rotation(euler_deg: Iterable[float]) -> unreal.Rotator:
    """Blender XYZ-Euler camera to UE rotator.

    Infinigen writes rotation as (yaw_around_X, 0, pitch_around_Z) in Blender.
    A Blender camera with zero rotation looks down -Z. A UE camera with zero
    rotation looks down +X. The conversion applies two corrections:
      (a) pre-rotate by -90deg on pitch so 'forward' aligns with UE's +X
      (b) flip sign on yaw + pitch because Y is mirrored
    """
    rx, _, rz = euler_deg
    # Derivation (see notes in README):
    # Blender camera forward = -Z by default. Infinigen stores its frame as
    # Euler((yaw_around_X, 0, pitch_around_Z), "XYZ") so:
    #   Blender forward = R_z(rz) * R_x(rx) * (0,0,-1)
    # Convert to UE (mirror Y for handedness) then solve for UE Rotator.
    ue_pitch = rx - 90.0          # vertical tilt (Blender yaw=90 → UE pitch=0)
    ue_yaw = -rz - 90.0           # azimuth
    ue_roll = 0.0
    # Use kwargs — positional Rotator(a,b,c) is (pitch, yaw, roll) in UE 5.x
    # and passing them out of order silently rolls the camera.
    return unreal.Rotator(pitch=ue_pitch, yaw=ue_yaw, roll=ue_roll)


# ---------------------------------------------------------------------------
# asset helpers
# ---------------------------------------------------------------------------

def find_or_spawn_camera(label: str) -> unreal.CineCameraActor:
    subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for actor in subsys.get_all_level_actors():
        if actor.get_actor_label() == label and isinstance(actor, unreal.CineCameraActor):
            return actor
    cam: unreal.CineCameraActor = subsys.spawn_actor_from_class(
        unreal.CineCameraActor, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0)
    )
    cam.set_actor_label(label)
    return cam


def create_level_sequence(asset_path: str) -> unreal.LevelSequence:
    """Return a blank LevelSequence at asset_path, reusing the existing asset
    if one is there (and wiping its contents). delete_asset+create_asset does
    not work under -Unattended because CanCreateAsset refuses to prompt.
    """
    pkg_path, asset_name = asset_path.rsplit("/", 1)
    existing = unreal.EditorAssetLibrary.load_asset(asset_path)
    if existing is not None:
        # Strip every master/global track and every binding so the asset is
        # blank. UE 5 removed get_master_tracks; use get_tracks()/remove_track.
        for track in existing.get_tracks():
            existing.remove_track(track)
        for binding in existing.get_bindings():
            binding.remove()
        return existing

    tools = unreal.AssetToolsHelpers.get_asset_tools()
    return tools.create_asset(
        asset_name=asset_name,
        package_path=pkg_path,
        asset_class=unreal.LevelSequence,
        factory=unreal.LevelSequenceFactoryNew(),
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    args = _parse_ue_script_args()
    with open(args.viewpoints, "r") as f:
        data = json.load(f)

    fps = args.fps or int(data.get("fps", 24))
    viewpoints = data["viewpoints"]
    if not viewpoints:
        unreal.log_error("viewpoints.json is empty")
        return 1

    # Load the target map; the possessable camera must live in the same
    # world the renderer will use.
    if args.map:
        level_subsys = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        if not level_subsys.load_level(args.map):
            unreal.log_error(f"Failed to load map: {args.map}")
            return 1
        unreal.log(f"[InfiniBench] loaded map: {args.map}")

    camera = find_or_spawn_camera(args.cameraLabel)

    # Set focal length on the CineCameraComponent. Default lens preset clamps
    # to 30-300mm so we widen the lens range to allow 10mm wide-angle.
    cine_comp = camera.get_cine_camera_component()
    lens = cine_comp.get_editor_property("lens_settings")
    lens.min_focal_length = min(1.0, args.focalLength)
    lens.max_focal_length = max(1000.0, args.focalLength)
    cine_comp.set_editor_property("lens_settings", lens)
    cine_comp.set_editor_property("current_focal_length", args.focalLength)
    unreal.log(f"[InfiniBench] focal length set to {args.focalLength}mm")

    seq = create_level_sequence(args.outSeq)
    seq.set_display_rate(unreal.FrameRate(fps, 1))
    seq.set_playback_start(0)
    seq.set_playback_end(len(viewpoints))

    binding = seq.add_possessable(camera)

    binding_id = unreal.MovieSceneObjectBindingID()
    binding_id.set_editor_property("Guid", binding.get_id())
    cut_track = seq.add_track(unreal.MovieSceneCameraCutTrack)
    cut_section = cut_track.add_section()
    cut_section.set_camera_binding_id(binding_id)
    cut_section.set_start_frame(0)
    cut_section.set_end_frame(len(viewpoints))

    transform_track = binding.add_track(unreal.MovieScene3DTransformTrack)
    if transform_track is None:
        transform_track = unreal.MovieSceneBindingExtensions.add_track(
            binding, unreal.MovieScene3DTransformTrack
        )
    if transform_track is None:
        raise RuntimeError("Failed to add transform track to camera binding")
    section = transform_track.add_section()
    section.set_start_frame(0)
    section.set_end_frame(len(viewpoints))

    # UE 5.1+ Sequencer API: channels are MovieSceneScripting{Double,Float}Channel.
    # MovieScene3DTransformSection exposes 9 double channels in order
    # Tx, Ty, Tz, Rx, Ry, Rz, Sx, Sy, Sz.
    channels = section.get_all_channels()
    if len(channels) < 6:
        raise RuntimeError(f"expected >= 6 transform channels, got {len(channels)}")

    for vp in viewpoints:
        frame_number = unreal.FrameNumber(vp["frame"])
        loc = blender_to_unreal_location(vp["location_m"])
        rot = blender_to_unreal_rotation(vp["rotation_euler_deg"])
        values = [loc.x, loc.y, loc.z, rot.roll, rot.pitch, rot.yaw]
        for ch, value in zip(channels[:6], values):
            # channel.add_key signature (UE 5.x):
            #   add_key(time: FrameNumber, new_value: float/double,
            #           sub_frame: float = 0.0,
            #           time_unit: SequenceTimeUnit = DISPLAY_RATE,
            #           interpolation: MovieSceneKeyInterpolation = AUTO)
            ch.add_key(frame_number, float(value))

    # Persist the sequence asset.
    unreal.EditorAssetLibrary.save_asset(args.outSeq)
    # Persist the level so the camera actor survives to render time.
    if args.map:
        level_subsys = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        level_subsys.save_current_level()
    unreal.log(f"[InfiniBench] LevelSequence saved: {args.outSeq} "
               f"({len(viewpoints)} keyframes @ {fps} fps)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
