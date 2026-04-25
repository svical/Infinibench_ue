#!/usr/bin/env bash
# InfiniBench → UE5 pipeline driver (Linux).
#
# Stages:
#   1. NL prompt  ──Gemini──>  Infinigen constraint overlay  ──Blender solver──>  scene.blend
#   2. scene.blend  ──infinigen.tools.export──>  scene.usdc + textures/
#   3. scene.blend  ──trajectory_optimizer──>   viewpoints.json   (no rendering)
#   4. viewpoints.json + scene.usdc  ──UE5 Editor Python──>  LevelSequence asset
#
# Stage 4 expects you to have already imported scene.usdc into a UE5 project once
# (see README §"One-time UE setup"). Each subsequent run rebuilds the
# LevelSequence in place. Headless MRQ rendering is intentionally NOT part of
# the script — UE 5.7's `-ExecutePythonScript` lifecycle is unreliable for
# render jobs; trigger MRQ from the editor's Movie Render Queue panel.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
ENV_FILE="${SCRIPT_DIR}/configs/defaults.env"

if [[ -f "${ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
else
    echo "[pipeline] ${ENV_FILE} not found; relying on shell environment." >&2
fi

# ---- defaults / required ------------------------------------------------
: "${BLENDER_BIN:?set BLENDER_BIN in configs/defaults.env}"
: "${INFINIBENCH_ROOT:=$(realpath "${SCRIPT_DIR}/..")}"
: "${WORK_DIR:=/tmp/infinibench_run}"
: "${SEED:=42}"
: "${GIN_CONFIG:=fast_solve}"
: "${SCENE_DESCRIPTION:=}"

# ---- argument parsing ---------------------------------------------------
SKIP_UE=0
SKIP_TRAJ=0
SKIP_USD=0
DESCRIPTION="${SCENE_DESCRIPTION}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --description)        DESCRIPTION="$2"; shift 2;;
        --seed)               SEED="$2"; shift 2;;
        --work-dir)           WORK_DIR="$2"; shift 2;;
        --skip-usd)           SKIP_USD=1; SKIP_TRAJ=1; SKIP_UE=1; shift;;
        --skip-trajectory)    SKIP_TRAJ=1; SKIP_UE=1; shift;;
        --skip-ue)            SKIP_UE=1; shift;;
        -h|--help)            sed -n '2,15p' "${BASH_SOURCE[0]}"; exit 0;;
        *) echo "[pipeline] unknown arg: $1" >&2; exit 2;;
    esac
done

# ---- run dir layout -----------------------------------------------------
STAMP="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="${WORK_DIR}/run_${STAMP}_seed${SEED}"
SCENE_DIR="${RUN_DIR}/scene"
EXPORT_DIR="${RUN_DIR}/export"
TRAJ_DIR="${RUN_DIR}/trajectory"
mkdir -p "${RUN_DIR}"
ln -sfn "${RUN_DIR}" "${WORK_DIR}/latest"
echo "[pipeline] run dir: ${RUN_DIR}"

# ---- stage 1: scene generation -----------------------------------------
echo "[pipeline] (1/4) generating scene..."
mkdir -p "${SCENE_DIR}"

GIN_OVERRIDES=()
if [[ -n "${DESCRIPTION}" && "${INFINIBENCH_AGENTIC_LLM:-}" != "" ]]; then
    GIN_OVERRIDES+=(
        "compose_indoors.scene_description=\"${DESCRIPTION}\""
        "compose_indoors.use_agentic_constraints=True"
        "compose_indoors.agentic_max_iterations=3"
    )
    echo "[pipeline]   description : ${DESCRIPTION}"
    echo "[pipeline]   LLM provider: ${INFINIBENCH_AGENTIC_LLM}"
else
    echo "[pipeline]   no description / LLM not configured — using deterministic constraints"
fi

(
    cd "${INFINIBENCH_ROOT}"
    "${BLENDER_BIN}" --background \
        --python infinigen_examples/generate_indoors.py -- \
        --output_folder "${SCENE_DIR}" \
        -s "${SEED}" \
        -g base "${GIN_CONFIG}" \
        ${GIN_OVERRIDES:+-p "${GIN_OVERRIDES[@]}"}
)

BLEND="${SCENE_DIR}/scene.blend"
[[ -f "${BLEND}" ]] || { echo "[pipeline] scene.blend not produced" >&2; exit 3; }
echo "[pipeline]   scene.blend: $(du -h "${BLEND}" | cut -f1)"

if [[ "${SKIP_USD}" -eq 1 ]]; then
    echo "[pipeline] done (stopped after Blender): ${BLEND}"
    exit 0
fi

# ---- stage 2: USD export + PBR bake ------------------------------------
echo "[pipeline] (2/4) exporting USDC..."
(
    cd "${INFINIBENCH_ROOT}"
    "${BLENDER_BIN}" --background \
        --python "${SCRIPT_DIR}/blender/run_export.py" -- \
        --input_folder "${SCENE_DIR}" \
        --output_folder "${EXPORT_DIR}" \
        -f usdc -r 1024 --omniverse
)
# export.py races its own mkdir; copy solve_state.json explicitly.
cp -f "${SCENE_DIR}/solve_state.json" "${EXPORT_DIR}/solve_state.json" 2>/dev/null || true
USDC="$(find "${EXPORT_DIR}" -name '*.usdc' | head -n1)"
[[ -f "${USDC}" ]] || { echo "[pipeline] USDC not produced" >&2; exit 4; }
echo "[pipeline]   USDC: $(du -h "${USDC}" | cut -f1)"

# ---- stage 3: viewpoint planning (no rendering) ------------------------
if [[ "${SKIP_TRAJ}" -eq 1 ]]; then
    echo "[pipeline] done (skipped trajectory): ${EXPORT_DIR}"
    exit 0
fi

echo "[pipeline] (3/4) planning viewpoints..."
mkdir -p "${TRAJ_DIR}"
(
    cd "${INFINIBENCH_ROOT}"
    "${BLENDER_BIN}" --background \
        --python "${SCRIPT_DIR}/blender/extract_viewpoints.py" -- \
        --blend "${BLEND}" \
        --output "${TRAJ_DIR}" \
        --fps 24
)
VP="${TRAJ_DIR}/viewpoints.json"
[[ -f "${VP}" ]] || { echo "[pipeline] viewpoints.json not produced" >&2; exit 5; }
echo "[pipeline]   viewpoints: $(jq '.viewpoints|length' "${VP}" 2>/dev/null || echo '?') frames"

# ---- stage 4: UE LevelSequence build -----------------------------------
if [[ "${SKIP_UE}" -eq 1 ]]; then
    cat <<EOF
[pipeline] done (stopped before UE):
  scene.blend  : ${BLEND}
  scene.usdc   : ${USDC}
  viewpoints   : ${VP}
EOF
    exit 0
fi

: "${UE_EDITOR_CMD:?set UE_EDITOR_CMD to UnrealEditor-Cmd}"
: "${UE_PROJECT:?set UE_PROJECT to your .uproject}"
: "${UE_MAP_ASSET:?set UE_MAP_ASSET (e.g. /Game/InfiniBench/Maps/DefaultMap)}"
: "${UE_SEQ_ASSET:?set UE_SEQ_ASSET (e.g. /Game/InfiniBench/S_Traj)}"
: "${UE_FOCAL_LENGTH:=10}"

echo "[pipeline] (4/4) building UE LevelSequence..."
"${UE_EDITOR_CMD}" "${UE_PROJECT}" \
    -ExecutePythonScript="${SCRIPT_DIR}/ue5/build_sequence.py \
--viewpoints=${VP} \
--outSeq=${UE_SEQ_ASSET} \
--map=${UE_MAP_ASSET} \
--focalLength=${UE_FOCAL_LENGTH}" \
    -Unattended -NoSplash -stdout

echo "[pipeline] done."
echo "  Open ${UE_SEQ_ASSET} in the UE editor and trigger Movie Render Queue"
echo "  to render. (Headless MRQ rendering via -ExecutePythonScript is"
echo "  unreliable in UE 5.7 and is intentionally not automated here.)"
