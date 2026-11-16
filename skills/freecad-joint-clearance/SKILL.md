---
name: freecad-joint-clearance
description: Measure how far an articulated joint can move in FreeCAD through the FreeCAD MCP tools, find what collides first, and compare designs. Use for joint clearance, range-of-motion and before/after motion checks.
---

# FreeCAD joint clearance

Give a defensible motion estimate without confusing a CAD obstruction with
usable printed movement.

## Define the motion

- Identify the moving and fixed parts. Parts that move together (a waist and
  its joint, say) are one moving group.
- Find the pivot from the geometry, never from a bounding box:
  `find_sphere_centres` for ball joints, or the sphere and cylinder data in
  `get_shape_geometry`.
- State the axis, frame and sign for each motion (forward bend, side lean,
  twist), and the neutral pose you start from. Ask only if the model can't
  tell you.
- Note intended contact: a ball gripped by its socket overlaps at rest on
  purpose. It is not a collision.

## Measure

- Use `sweep_clash` for each axis. It rotates copies, so the user's parts
  never move. Pick a coarse `step` and the `precision` the question needs
  (5° and 0.5° suit most figure joints), and a range wide enough to find a
  limit in both directions.
- For gripped joints use `allow_start_overlap`, which counts only overlap
  growth above the resting grip. Use `exclude_pairs` only with a reason: it
  hides any stop between that pair, including a real one.
- Use `check_overlap` with `pose_a`/`pose_b` for single poses and for
  combined poses (bend plus twist). Single-axis limits don't define a safe
  region for combined motion; test the combinations that matter.
- When comparing designs, rerun the same sweep (same pivot, axis, step,
  precision, threshold) on each.
- Without these tools, pose copies with `set_placement` (pose =
  T(pivot)·R(angle)·T(−pivot)·start placement) and intersect with temporary
  booleans. Delete every temporary object afterwards.

## Interpret

- Zero overlap doesn't prove clearance: report the gap at the last clear
  angle, and "touching" as contact.
- The overlap threshold filters numerical noise. It is not a print
  allowance; state any allowance separately.
- A stepped sweep brackets the limit; report the bracket (last clear, first
  clash), not an exact angle.
- Name the limiting pair and contact region from the result, rather than
  assuming it from the shape of the parts.

## Report

Keep a short table for the user, and include:

- Design revision and object names; moving and fixed parts.
- Pivot (mm), axis and frame, sign convention, neutral pose, and the order
  of combined rotations.
- Intended contact allowed or excluded, and why.
- Step, precision and threshold used.
- Per direction: last clear and first clash angle, limiting pair, contact
  region, gap at the last clear angle.
- Differences from the baseline design.
- Which statements come from CAD and which from the user's printed parts.
  CAD has no print tolerance, friction or flex.

Change the design only within the scope the user gave. A request to measure
clearance is not a request to remove the obstruction.
