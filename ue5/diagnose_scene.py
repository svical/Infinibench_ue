"""Print a histogram of actor classes + light component inventory."""
from collections import Counter
import argparse
import sys
import unreal  # type: ignore[import-not-found]


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--map", default=None)
    args, _ = parser.parse_known_args()
    return args


def main() -> int:
    args = _parse_args()
    if args.map:
        level_subsys = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        level_subsys.load_level(args.map)

    actor_subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = actor_subsys.get_all_level_actors()

    unreal.log(f"[DIAG] total actors: {len(actors)}")

    cls_counts: Counter = Counter()
    light_components: Counter = Counter()
    for actor in actors:
        cls_counts[actor.get_class().get_name()] += 1
        for c in actor.get_components_by_class(unreal.LightComponentBase):
            light_components[c.get_class().get_name()] += 1

    unreal.log("[DIAG] actor class histogram:")
    for name, n in cls_counts.most_common():
        unreal.log(f"[DIAG]   {n:>6}  {name}")

    unreal.log(f"[DIAG] total LightComponentBase instances: {sum(light_components.values())}")
    for name, n in light_components.most_common():
        unreal.log(f"[DIAG]   {n:>6}  {name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
