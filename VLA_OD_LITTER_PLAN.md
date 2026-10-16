# Plan: Roboflow OD + VLA litter picking on SO-101

Assumption for this plan: you want the robot to **pick the specific litter instance Roboflow detects**, not just “act like the demos.” That means **language + target bbox conditioning** (not language-only, and not overlay-in-pixels). ~50 episodes is enough for a first closed-loop attempt if demos are tight and consistent.

Your stack today:

| Piece | Role |
|--------|------|
| `so101_vr/` teleop + `RobotInterface` | 20 Hz joint control, safety |
| LeRobot dataset under `datasets/<repo>` | raw images + joints + `task` |
| `RoboflowInferenceService` (`so101_vr/core/roboflow_inference.py`) | hosted OD, async, JSONL + overlays |
| Model `litter-finder/4` | litter boxes on cam streams |

---

## Phase 0 — Lock the product loop (1 day)

**Goal:** one sentence for success and non-goals.

1. **Success metric (v0)**  
   In a fixed tabletop scene with 1–3 litter pieces: from a home pose, the arm reaches and grasps a detected piece in ≥40% of trials (20 trials), without operator intervention.

2. **Non-goals for v0**  
   Multi-room search, bagging, outdoor, novel classes, dual-arm, realtime local OD.

3. **Camera contract (freeze this)**  
   - Always record/deploy with the **same named slots** you train on (recommend **front + wrist**; add top only if demos use it).  
   - Train only on **raw** `observation.images.*`, never `*_overlay`.  
   - OD may run on front (search/select) and wrist (confirm near grasp).

4. **Task language contract**  
   Pick one template and use it in every demo and at deploy, e.g.  
   `pick up the litter`  
   Later you can specialize (`pick up the bottle`) once classes are reliable.

5. **Target signal contract (the knit)**  
   At train and deploy, also feed a fixed-size target vector derived from OD (or from teleop proxy during recording):

   ```text
   target = [
     cam_id_one_hot or scalar,   # which camera the box came from (e.g. 0=front, 1=wrist)
     cx_norm, cy_norm,           # box center in [0,1] image coords
     w_norm, h_norm,             # box size in [0,1]
     confidence,                 # 0..1
     valid                       # 1 if a target exists else 0
   ]
   ```

   Store as something like `observation.target` (float32, shape ~7–10). If the VLA recipe you choose cannot take extra vectors easily, fall back to a **256×256 crop** of the target as `observation.images.target` — same idea, heavier I/O.

---

## Phase 1 — Data collection protocol (50 episodes) (2–4 days)

**Goal:** demos the policy can imitate, aligned with deploy conditioning.

### 1.1 Dataset hygiene

1. Create a **new** `recording.repo_id` dedicated to this skill, e.g. `local/so101_litter_v0` (don’t mix with unrelated pick-and-place).  
2. Set `recording.task` to the locked phrase (`pick up the litter`).  
3. Keep `roboflow.enabled: true`, `save_predictions: true`, `save_overlays: true` for QC — overlays for humans, raw for training.  
4. Use a stable camera set (front+wrist). Disconnect unused cams so the LeRobot schema doesn’t include dead streams.

### 1.2 Scene and demo script (repeat every episode)

1. Reset arm to a **fixed home** (scripted or VR to home).  
2. Place 1–3 litter pieces in view of **front**.  
3. Start recording.  
4. Operator picks **one** piece end-to-end: approach → grasp → lift ~5–10 cm → (optional) place in bin.  
5. Stop recording. Discard if: lost tracking, collision, double grasp attempt, or camera glitch.

**Coverage targets across 50 episodes (approximate):**

| Factor | Spread |
|--------|--------|
| Object types | Match what `litter-finder` actually labels |
| Positions | Near / mid / far in front cam; left/center/right |
| Orientations | Flat vs upright if relevant |
| Distractors | ~30% episodes with 2–3 pieces (pick one) |
| Lighting | Same room, avoid wild changes for v0 |
| Failures | Do **not** save failed grasps into the train set |

### 1.3 Make demos “detection-aligned”

While recording, watch the Roboflow preview:

1. Only start the reach when the **intended** piece has a stable box (conf ≥ your threshold, e.g. 0.4).  
2. Prefer picking the highest-confidence / largest box so deploy’s selector matches your behavior.  
3. Spot-check `datasets/.../predictions/predictions_epXXXXXX.jsonl` after a few episodes: boxes should exist on front during approach and ideally on wrist near grasp.

### 1.4 (Recommended) Record the target feature during teleop

Today JSONL is a sidecar; the VLA won’t see it unless you add it to the frame.

**Implementation step:**

1. In `EpisodeRecorder.add_frame` (`so101_vr/core/episode_recorder.py`), after Roboflow cache update, run a small `select_target(predictions)` (same function you’ll use at deploy).  
2. Write `observation.target` into the LeRobot frame.  
3. Extend `_build_features()` with that vector.  
4. Use a **new** `repo_id` when you add this feature (schema change).

Until that’s built, you can **post-process** JSONL → per-frame `target` parquet/feature offline before training (Phase 2.2). Offline is fine for v0 if timestamps/`frame_index` line up.

### 1.5 QC gate before training

1. Replay 5 random episodes (LeRobot viz or your overlay videos).  
2. Confirm: home→grasp→lift is smooth; no long idle; task string consistent.  
3. Confirm ≥~80% of frames in the approach phase have a non-empty front detection in JSONL.  
4. If OD is weak on your litter, **improve/retrain `litter-finder` first** — a bad detector cannot be fixed by the VLA.

---

## Phase 2 — Offline dataset prep + VLA training (2–5 days)

**Goal:** a checkpoint that maps `(images, state, task, target) → action` at 20 Hz.

### 2.1 Choose the policy family

Given README mentions a `pi0` conda env, a practical default:

1. **Primary:** LeRobot π0 / π0-fast (or whatever your `pi0` env already trains) on SO-101.  
2. **Fallback if π0 is too heavy for 50 eps:** ACT or Diffusion Policy in LeRobot on the same dataset (often more sample-efficient for single-skill SO arms).

Freeze one choice before you spend GPU time.

### 2.2 Build the training dataset view

1. Root: `datasets/local_so101_litter_v0` (example).  
2. Features used for training:
   - `observation.images.front`, `observation.images.wrist` (raw)
   - `observation.state` (6,)
   - `action` (6,)
   - `task` (string)
   - `observation.target` (vector) — from live recording or JSONL backfill  
3. Explicitly **exclude** `*_overlay` from the policy input config.  
4. Optional filters: drop first N frames of idle; clip episodes after lift; discard episodes with `valid=0` for most of the reach.

**JSONL backfill (if target wasn’t in-frame):**

For each episode `e`, each frame `t`:

1. Load predictions for front (and wrist).  
2. `target = select_target(...)` with the **exact** deploy heuristics.  
3. Write aligned `observation.target[t]`.  
4. Unit-test: `frame_index` length matches episode length.

### 2.3 Implement `select_target` once and reuse

Put this in a shared module (e.g. future `so101_vr/core/target_select.py`) used by recorder, backfill, and deploy:

```text
inputs: dict[camera -> list[pred]], optional last_target
rules (v0):
  1. Filter conf >= 0.4, class in allowed set (or all litter classes)
  2. Prefer camera order: front for search; if wrist has a box and gripper is "close" (optional), prefer wrist
  3. Score = confidence * (0.5 + 0.5 * area_norm)  # prefer clear, large boxes
  4. Optional: stick to previous target if IoU > 0.3 (tracking stability)
  5. If none: valid=0, zeros elsewhere
outputs: target vector as in Phase 0
```

### 2.4 Train

Concrete checklist:

1. Activate the training env (`pi0` or `teleop` + LeRobot train deps).  
2. Launch LeRobot train with:
   - dataset repo/root = your litter dataset  
   - `fps=20` matching recording  
   - image keys = front, wrist (and target crop if used)  
   - action = joint positions (match how you recorded: commanded angles)  
   - batch size / steps appropriate for ~50 eps (don’t overtrain to memorization without eval)  
3. Save checkpoints every N steps; keep last + best-on-val.  
4. **Validation:** hold out 5 episodes (or 10%) — never train on them.

### 2.5 Offline eval gate

Before the robot:

1. Open-loop replay: plot predicted vs demo actions on holdout.  
2. Qualitative: predicted gripper closes near demo grasp frame.  
3. If open-loop is garbage, **do not** deploy — collect more / cleaner data or switch to ACT.

---

## Phase 3 — Deploy architecture in SO-101-VR-Control (3–5 days)

**Goal:** replace VR teleop actions with VLA actions, while OD selects the target.

### 3.1 New runtime mode (don’t overload teleop)

Add a mode flag, e.g. config:

```yaml
autonomy:
  enabled: false
  policy_path: /path/to/checkpoint
  device: cuda
  control_hz: 20
  require_target: true          # if valid=0, hold / go home
  max_episode_sec: 30
  success_lift_z_delta: 0.05    # optional heuristic
```

CLI idea: `so101-vr --autonomy` or dashboard “Start Policy”.

### 3.2 Component diagram

```text
OpenCV cams (same as EpisodeRecorder)
        │
        ├──────────────────────────────┐
        ▼                              ▼
 raw frames                      RoboflowInferenceService
        │                              │
        │                              ▼
        │                        select_target()
        │                              │
        ▼                              ▼
  PolicyRunner.obs  ←── images, state, task, target
        │
        ▼
  action (6 joints) + gripper
        │
        ▼
  RobotInterface (existing velocity limits / IK safety as applicable)
```

### 3.3 Implementation steps (specific to this repo)

1. **`PolicyRunner` module** (new): load LeRobot policy checkpoint; `predict(obs_dict) -> action`.  
2. **Reuse** camera read path from `EpisodeRecorder.read_images` (do not open a second `VideoCapture`).  
3. **Reuse** `RoboflowInferenceService`: `submit` + `get_latest` each tick; never block the 20 Hz loop on hosted HTTP.  
4. **Control loop branch** in `so101_vr/control_loop.py`:  
   - If autonomy: skip VR/keyboard arm resolution; every `send_interval` (0.05 s):  
     read images → build target → policy → `robot_interface` command.  
   - Keep engage/safety/E-stop paths identical.  
5. **Observation packing** must match training keys exactly (`observation.images.front`, etc., dtypes, image layout RGB, joint order `JOINT_NAMES`).  
6. **Action semantics:** you record **commanded** angles as `action` and **actual** as `observation.state`. At deploy, send policy outputs through the same path as teleop commands (with `max_relative_target` / velocity limiter already in `RobotInterface`).  
7. **Dashboard:** show selected target box (draw only the chosen detection), policy FPS, `target.valid`, last error.  
8. Optional: log autonomy episodes to a LeRobot dataset for DAgger later.

### 3.4 Hosted OD latency policy

1. Keep `max_fps_per_camera: 4` (or local server later).  
2. VLA uses **latest** target every 20 Hz tick (`valid` sticky with timeout ~1.0 s).  
3. If `valid=0` for >1 s at start: abort to home (don’t flail).  
4. Mid-grasp: if target flickers, keep last target while wrist motion is progressing (IoU sticky rule).

### 3.5 Safety checklist before first autonomous move

1. Motors engaged only after explicit UI confirm.  
2. Existing joint limits, 10°/step, Z-floor stay on.  
3. “Stop Policy” immediately returns to teleop / holds pose.  
4. First tests with **workspace foam / no fragile objects**, single piece only.

---

## Phase 4 — Closed-loop evaluation protocol (1–2 days)

**Goal:** measure whether the knit works.

### 4.1 Trial matrix (minimum 20 trials)

| Condition | Trials |
|-----------|--------|
| 1 object, center, seen type | 8 |
| 1 object, left/right | 6 |
| 2 objects, pick highest-conf | 6 |

For each trial record: success/fail, fail mode (no detect, wrong object, miss grasp, collision, timeout), video.

### 4.2 Pass/fail for v0

- ≥40% success on single-object center → proceed to iterate.  
- &lt;20% → data/policy problem; don’t add planner complexity yet.  
- Detects OK but grasp fails → more wrist demos + grasp-centric episodes.  
- Wrong object with 2 pieces → tighten `select_target` / add stickiness / require click-to-confirm temporarily.

### 4.3 Ablations (learn what mattered)

1. Policy with **target zeros** vs real target (does conditioning help?).  
2. Front-only vs front+wrist images.  
3. OD conf threshold 0.3 vs 0.5.

---

## Phase 5 — Iterate (ongoing)

Ordered upgrades when v0 plateaus:

1. **More demos** (50 → 100–200) focused on failure modes.  
2. **DAgger:** run policy, operator corrects, append episodes.  
3. **Local Roboflow Inference Server** (GPU) if hosted lag hurts tracking.  
4. **Cascade:** coarse IK toward bbox bearing (if you add camera extrinsics / depth later) + VLA for final 5 cm.  
5. **Multi-class language** once detector classes are stable (`pick up the can`).  
6. **Bin place** as a second skill / second dataset, not bolted onto v0.

---

## Suggested calendar (aggressive but realistic)

| Week | Deliverable |
|------|-------------|
| Week 1 | Phase 0–1: 50 clean episodes + QC; target vector path (live or JSONL backfill) |
| Week 2 | Phase 2: train + offline eval; fix data if needed |
| Week 3 | Phase 3–4: autonomy mode in this repo; 20 real trials; write failure taxonomy |
| Week 4 | Phase 5: DAgger / more data on top failures |

---

## Concrete “done” definition

You are done with the **knit** when:

1. Roboflow proposes litter boxes on the live cameras.  
2. `select_target` chooses one and fills `observation.target`.  
3. The VLA, trained on the same obs schema, outputs joint actions at 20 Hz through existing `RobotInterface`.  
4. On the fixed scene, success rate meets your Phase 0 bar without VR input.

---

## What to build in *this* repo vs outside

| In SO-101-VR-Control | Outside (training box / LeRobot scripts) |
|----------------------|------------------------------------------|
| Target select + write `observation.target` while recording | Train job, configs, GPU |
| Autonomy control-loop mode + PolicyRunner | Checkpoint artifacts |
| Reuse Roboflow service + cameras + safety | Dataset upload/backup |
| Eval logging / stop button | Hyperparameter sweeps |

---

## Critical path risks

1. **Schema mismatch** (train without `target`, deploy with it — or different cameras) → silent failure.  
2. **Training on overlays** → policy depends on drawn boxes that may vanish.  
3. **50 episodes of messy teleop** → autonomy won’t recover; QC ruthlessly.  
4. **Hosted OD + chasing moving targets** → OK for static litter; not for rolling cans.  
5. **Action = commanded joints** → policy must be trained on that same definition.
