"""Strip down Infinigen's USD-imported lighting so the editor stays interactive.

The Blender → USD export brings in every Infinigen light (ceiling lights,
pendants, wall lamps, etc.) as individual Spot/Point lights. With a few
hundred shadow-casting lights the editor runs into VSM MaxLightsPerPixel
warnings and ray-tracing geometry goes over budget. This script:

  1. Deletes every Spot/Point/Rect light actor in the level.
  2. Keeps (or re-creates) a single DirectionalLight (sun) and SkyLight.
  3. Saves via save_dirty_packages so World Partition external actors are
     persisted on disk.

Invoke:
    UnrealEditor-Cmd.exe <proj> \\
        -ExecutePythonScript="dim_lights.py --map=/Game/.../DefaultMap"
"""
from __future__ import annotations

import argparse
import sys

import unreal  # type: ignore[import-not-found]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map", default=None,
                        help="Map asset to load first")
    parser.add_argument("--sunIntensity", type=float, default=3.0,
                        help="DirectionalLight intensity (lux)")
    parser.add_argument("--skyIntensity", type=float, default=1.0,
                        help="SkyLight intensity")
    parser.add_argument("--keepPointLights", action="store_true",
                        help="Keep Point/Spot lights but disable shadows/RT")
    args, _ = parser.parse_known_args()
    return args


LIGHT_ACTOR_CLASSES = (
    "PointLight", "SpotLight", "RectLight",
)


def _is_stripable_light(actor: unreal.Actor) -> bool:
    class_name = actor.get_class().get_name()
    return class_name in LIGHT_ACTOR_CLASSES


def _disable_heavy_features(actor: unreal.Actor) -> None:
    for comp in actor.get_components_by_class(unreal.LightComponent):
        comp.set_editor_property("cast_shadows", False)
        comp.set_editor_property("cast_ray_traced_shadows", False)
        comp.set_editor_property("affects_indirect_lighting_while_hidden", False)
        comp.set_intensity(1.0)


def main() -> int:
    args = _parse_args()

    if args.map:
        level_subsys = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        if not level_subsys.load_level(args.map):
            unreal.log_error(f"Failed to load map: {args.map}")
            return 1

    actor_subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = actor_subsys.get_all_level_actors()

    stripped = 0
    kept_directional = None
    kept_sky = None
    for actor in actors:
        cls_name = actor.get_class().get_name()
        if cls_name == "DirectionalLight" and kept_directional is None:
            kept_directional = actor
            continue
        if cls_name == "SkyLight" and kept_sky is None:
            kept_sky = actor
            continue
        if _is_stripable_light(actor):
            if args.keepPointLights:
                _disable_heavy_features(actor)
            else:
                actor_subsys.destroy_actor(actor)
                stripped += 1

    # Ensure we have a sun + skylight
    if kept_directional is None:
        kept_directional = actor_subsys.spawn_actor_from_class(
            unreal.DirectionalLight,
            unreal.Vector(0, 0, 500),
            unreal.Rotator(-45, -30, 0),
        )
    if kept_sky is None:
        kept_sky = actor_subsys.spawn_actor_from_class(
            unreal.SkyLight, unreal.Vector(0, 0, 300), unreal.Rotator(0, 0, 0)
        )

    # Set intensities
    for comp in kept_directional.get_components_by_class(unreal.DirectionalLightComponent):
        comp.set_intensity(args.sunIntensity)
        comp.set_editor_property("cast_shadows", True)
    for comp in kept_sky.get_components_by_class(unreal.SkyLightComponent):
        comp.set_intensity(args.skyIntensity)
        comp.set_editor_property("real_time_capture", True)
        # recapture to pick up new lighting state
        comp.recapture_sky()

    # World Partition external actors only persist when we save dirty
    # packages. save_current_level() alone is not enough.
    unreal.EditorLoadingAndSavingUtils.save_dirty_packages(
        save_map_packages=True,
        save_content_packages=True,
    )

    unreal.log(f"[InfiniBench] stripped {stripped} lights; "
               f"kept DirectionalLight + SkyLight")
    return 0


if __name__ == "__main__":
    sys.exit(main())
