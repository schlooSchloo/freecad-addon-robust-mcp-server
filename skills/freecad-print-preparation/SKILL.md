---
name: freecad-print-preparation
description: Prepare FreeCAD parts for FDM printing through the FreeCAD MCP tools - orientation, bed contact, ball-joint interfaces, meshes and export checks. Use for print preparation and deliverable checks, not for operating a printer.
---

# FreeCAD print preparation

Prepare the requested part and files for FDM printing, keeping model
evidence separate from physical print results.

## Establish the print intent

- Read the project's settings: material, nozzle, layer height, supports,
  finish, orientation and protected interfaces. Ask only for what changes
  the decision.
- An export or printability request is not a request to redesign a joint or
  add supports. Keep geometry changes within scope.

## Orientation and bed contact

- Orient a separate print copy, so the assembly keeps its neutral pose. Put
  the chosen face down and its lowest point at Z = 0; check the bounds.
- Keep three contact measures apart, and say which you used:
  - flat bed-contact area: faces on the build plane (`get_shape_geometry`);
  - first-layer section: `get_section_outline` one layer height above the bed;
  - slicer contact: paths, supports and brim, which only the slicer knows.
- Compare candidates with the same measure. Area alone doesn't decide it:
  check continuity of contact, narrow webs and new unsupported regions.

## Ball-joint interfaces

- A sphere of radius R cut flat at distance d from its centre (0 ≤ d < R)
  leaves a flat of area π(R² − d²) and a height of R + d along the cut's
  normal.
- When scaling a part that must still fit the same socket or stand, keep R
  and d fixed. Scale the part and the cut plane, then place the sphere
  centre at p − d·n (p on the scaled plane, n its outward normal). Scaling
  the whole part would scale the joint too.
- For a plain `Part::Sphere` with local Z along n: Angle1 = −90°,
  Angle3 = 360°, Angle2 = asin(d / R).
- Check R, d and the cap plane in every variant and in the exported meshes.

## Meshes and export

- Make meshes with `create_mesh_from_shape`, choosing the deflection for the
  part's size (0.01 mm suits small figure parts). Meshes are static
  snapshots: regenerate after edits.
- Check every mesh with `check_mesh`: components, open and non-manifold
  edges, winding and volume. A successful export or import doesn't prove a
  mesh is watertight. Intentional separate parts are fine; say so.
- Export only the named objects. STL has no units: check the dimensions in
  mm. A pre-scaled file should be imported at 100%; say so to the recipient.
- File exports may need the user's approval in FreeCAD.
- After saving into a new folder, check linked files with
  `get_document_dependencies`.

## Report

Name the candidates, orientation, files, and the checks actually run. Record
print feedback against the exact candidate printed, and keep unreported
outcomes unknown. Put printer- and project-specific lessons in the project
profile, not in this skill.
