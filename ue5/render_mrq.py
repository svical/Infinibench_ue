"""Render a LevelSequence via Movie Render Queue, output an mp4.

Invoke headlessly via:

    UnrealEditor-Cmd.exe <path>.uproject \
        -ExecutePythonScript="render_mrq.py -seq=/Game/InfiniBench/S_Traj \
                              -map=/Game/InfiniBench/Maps/DefaultMap \
                              -out=C:\out\trajectory.mp4" \
        -RenderOffscreen -NoLoadingScreen -NoSplash -Unattended

Relies on these plugins (enabled in the .uproject):
- Movie Render Queue
- Movie Render Queue Additional Render Passes (for CLI encoder)

Run order in the pipeline:
    build_sequence.py  -->  render_mrq.py
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import PureWindowsPath, PurePosixPath

import unreal  # type: ignore[import-not-found]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seq", required=True,
                        help="Asset path to the LevelSequence, e.g. /Game/InfiniBench/S_Traj")
    parser.add_argument("--map", required=True,
                        help="Asset path to the Level/World that will be loaded for render")
    parser.add_argument("--out", required=True,
                        help="Output mp4 path on disk (absolute)")
    parser.add_argument("--resX", type=int, default=1920)
    parser.add_argument("--resY", type=int, default=1080)
    parser.add_argument("--samples", type=int, default=1,
                        help="MRQ TemporalSampleCount (1 = fastest, 8 = smooth)")
    parser.add_argument("--maxFrames", type=int, default=0,
                        help="If >0, render only the first N frames of the sequence "
                             "(verification mode). 0 = render the whole sequence.")
    args, _ = parser.parse_known_args()
    return args


def _out_components(abs_path: str) -> tuple[str, str]:
    """Split an absolute output path into (directory, basename-without-ext)."""
    # Handle both Windows and POSIX separators since we're driven from a shell
    if "\\" in abs_path and ":" in abs_path:
        p = PureWindowsPath(abs_path)
    else:
        p = PurePosixPath(abs_path)
    directory = str(p.parent)
    stem = p.stem
    return directory, stem


def main() -> int:
    args = _parse_args()

    seq_asset = unreal.load_asset(args.seq)
    if seq_asset is None:
        unreal.log_error(f"LevelSequence not found: {args.seq}")
        return 1
    world_asset = unreal.load_asset(args.map)
    if world_asset is None:
        unreal.log_error(f"Map not found: {args.map}")
        return 1

    subsys = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
    queue = subsys.get_queue()
    queue.delete_all_jobs()

    job = queue.allocate_new_job(unreal.MoviePipelineExecutorJob)
    job.sequence = unreal.SoftObjectPath(args.seq)
    job.map = unreal.SoftObjectPath(args.map)
    job.job_name = "InfiniBenchRender"

    cfg = job.get_configuration()

    # Deferred renderer
    cfg.find_or_add_setting_by_class(unreal.MoviePipelineDeferredPassBase)

    # PNG intermediate so the CLI encoder has something to stitch
    cfg.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_PNG)

    # Output resolution + directory
    output_setting = cfg.find_or_add_setting_by_class(unreal.MoviePipelineOutputSetting)
    out_dir, stem = _out_components(args.out)
    os.makedirs(out_dir, exist_ok=True)
    output_setting.output_directory = unreal.DirectoryPath(out_dir)
    output_setting.output_resolution = unreal.IntPoint(args.resX, args.resY)
    output_setting.file_name_format = f"{stem}.{{frame_number}}"

    # Verification mode: clamp MRQ's playback range to the first N frames.
    if args.maxFrames > 0:
        output_setting.use_custom_playback_range = True
        output_setting.custom_start_frame = 0
        output_setting.custom_end_frame = args.maxFrames
        unreal.log(f"[InfiniBench] custom playback range: 0..{args.maxFrames}")

    # Anti-aliasing / temporal samples
    aa = cfg.find_or_add_setting_by_class(unreal.MoviePipelineAntiAliasingSetting)
    aa.temporal_sample_count = args.samples
    aa.spatial_sample_count = 1

    # Note: we intentionally do NOT attach MoviePipelineCommandLineEncoder here.
    # In a fresh UE project the CLI encoder's ffmpeg path / codec / extension
    # settings are empty, which causes MRQ to fail with
    # "项目设置中未指定编码解码器可执行文件". The pipeline instead writes a
    # PNG sequence which we can stitch ourselves (ffmpeg on WSL, or configure
    # the CLI encoder in Project Settings later).

    # Headless pattern: hook the finished delegate and quit the editor there.
    # SystemLibrary.delay is a latent BP node and can't be used from sync
    # Python; blocking from Python is unnecessary anyway — once the executor
    # starts, the editor keeps ticking until it fires the delegate.
    executor = unreal.MoviePipelinePIEExecutor()

    def _on_finished(_pipeline, success):
        msg = f"[InfiniBench] MRQ finished, success={bool(success)}"
        unreal.log(msg)
        unreal.SystemLibrary.quit_editor()

    executor.on_executor_finished_delegate.add_callable_unique(_on_finished)
    subsys.render_queue_with_executor_instance(executor)
    unreal.log(f"[InfiniBench] MRQ started, output dir: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
