# infinibench_ue

End-to-end pipeline that turns a natural-language scene description into
an Infinigen-generated indoor scene, exports it to USD, plans a first-person
camera trajectory, and ingests both into Unreal Engine 5 as a renderable
LevelSequence.

```
NL prompt
   │  Gemini  (constraint emphasis overlay)
   ▼
build_constraints()  +  default home_furniture_constraints
   │  Blender + Infinigen solver
   ▼
scene.blend                       (every room furnished)
   │  blender/run_export.py       (UV unwrap + PBR bake + USD export)
   ▼
scene.usdc + textures/
   │  blender/extract_viewpoints.py (frontier-based trajectory, no Cycles render)
   ▼
viewpoints.json                   (Blender-frame, m + deg)
   │  ue5/build_sequence.py       (CineCameraActor + LevelSequence + keyframes)
   ▼
LevelSequence asset in UE         (open in editor + trigger Movie Render Queue)
```

## Layout

```
infinibench_ue/
├── README.md
├── run_pipeline.sh                # 4-stage driver
├── configs/
│   └── defaults.env.example       # copy to defaults.env, edit in place
├── blender/                       # runs inside Blender's Python (bpy)
│   ├── run_export.py
│   └── extract_viewpoints.py
└── ue5/                           # runs inside UnrealEditor-Cmd (unreal)
    ├── build_sequence.py          # consumes viewpoints.json, writes LevelSequence
    ├── render_mrq.py              # MRQ render (interactive — headless is flaky)
    ├── dim_lights.py              # one-shot utility: strip USD-imported lights
    └── diagnose_scene.py          # one-shot utility: actor / light histogram
```

## One-time setup

### Linux machine

This directory is an overlay on top of an
[InfiniBench](https://github.com/pittisl/infinibench) checkout. Drop
`infinibench_ue/` into the InfiniBench root (or symlink it there) so the
`infinigen` and `infinigen_examples` Python packages are importable from
this folder; the Blender-side scripts depend on them.

1. Install **Blender 4.2** somewhere; record the binary path.
2. Install **Infinigen** in the parent repo (already in place if you cloned
   it). Make sure the Blender embedded Python has `pyrender` and
   `google-generativeai` installed:
   ```bash
   <BLENDER_BIN_DIR>/4.2/python/bin/python3.11 -m pip install pyrender google-generativeai
   ```
3. Get a **Gemini API key** (https://aistudio.google.com).
4. `cp configs/defaults.env.example configs/defaults.env` and fill in.

### Unreal Engine project (one-time)

1. Install Unreal Engine 5.3+ (we tested 5.7).
2. Create a Blank project (`File → New Project → Blank → Python`). Record
   its `.uproject` path in `UE_PROJECT`.
3. Enable these plugins and restart the editor:
   - Python Editor Script Plugin
   - Movie Render Queue
   - Movie Render Queue Additional Render Passes
   - USD Importer
4. Run the pipeline once with `--skip-ue` so a `scene.usdc` is produced.
5. In the editor: `File → Import Into Level…` → pick the produced
   `scene.usdc`. Wait for the import to finish (slow on large scenes).
6. `File → Save All` (Ctrl+Shift+S). Save the resulting level under
   `/Content/InfiniBench/Maps/DefaultMap`. Record this asset path in
   `UE_MAP_ASSET`.
7. Optional but recommended: run `ue5/dim_lights.py` once to strip the
   hundreds of point/spot lights the USD import brought in:
   ```bash
   "${UE_EDITOR_CMD}" "${UE_PROJECT}" \
       -ExecutePythonScript="ue5/dim_lights.py --map=${UE_MAP_ASSET}" \
       -Unattended -NoSplash
   ```

After steps 1–6 every subsequent scene can swap in a new USD without
manual editor work, *provided you don't rename the map or asset paths.*
Step 5 (USD import) currently has to happen interactively in the editor;
we're tracking automated USD-importer invocation as a follow-up.

## Running

```bash
# Full pipeline up to LevelSequence build
./run_pipeline.sh --description "Small bedroom with high occupancy"

# Just Blender, no USD / no UE
./run_pipeline.sh --skip-usd --description "compact studio apartment"

# Stop after USD export
./run_pipeline.sh --skip-trajectory --description "..."

# Stop before UE (USD + viewpoints.json ready for hand-off)
./run_pipeline.sh --skip-ue --description "..."

# Different seed → different room layout
./run_pipeline.sh --seed 7 --description "..."
```

Outputs land under `${WORK_DIR}/run_<timestamp>_seed<N>/` with a `latest`
symlink kept fresh:

```
run_20260425_140123_seed42/
├── scene/
│   ├── scene.blend                 # 1-2 GB for full multi-room
│   ├── solve_state.json
│   └── …                           # Infinigen artefacts
├── export/
│   └── export_scene.blend/
│       ├── export_scene.usdc       # UE5 imports this
│       └── textures/*.png          # baked PBR
└── trajectory/
    ├── viewpoints.json             # ← UE consumes this
    ├── trajectory_data.csv
    └── visual.pdf                  # Dijkstra plan
```

## viewpoints.json schema

```json
{
  "fps": 24,
  "source_units": {"position": "meters", "rotation": "degrees"},
  "axis_convention": "blender",
  "viewpoints": [
    {
      "frame": 0,
      "location_m": [x, y, z],
      "rotation_euler_deg": [rx, ry, rz],
      "fov_deg": 60.0,
      "action": "translate"
    }
  ]
}
```

The JSON stays in Blender's frame; Blender→UE conversion happens inside
`ue5/build_sequence.py`.

## Notes

- MRQ rendering is performed interactively from the Movie Render Queue
  panel inside the UE editor; UE 5.7's `-ExecutePythonScript -Unattended`
  lifecycle exits before MRQ can complete a render, so Stage 4 stops at
  building the LevelSequence.
- `infinigen.tools.export.py` calls `zip` at the end. If `zip` is not on
  PATH the call fails with `FileNotFoundError`; the export itself is
  already complete by then.
- Multi-room USDC files can exceed 1 GB. UE5's USD Importer ingests them
  but uses a lot of RAM.
- The default Blender USD exporter does not write baked-texture references
  into the USD material network, so materials may show up unconnected in
  UE and need to be re-bound from the `textures/` folder.
- Each Gemini call may produce solver-invalid constraint domains.
  `agentic_max_iterations=3` retries usually converge; raise the cap if
  you see frequent "Agentic loop exhausted" errors.
