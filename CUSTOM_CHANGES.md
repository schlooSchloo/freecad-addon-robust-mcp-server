# Custom changes (schlooSchloo fork)

This file tracks how the `custom` branch differs from upstream
([spkane/freecad-addon-robust-mcp-server](https://github.com/spkane/freecad-addon-robust-mcp-server)),
plus the backlog of fixes and features we have planned but not started.

Upstream's `RELEASE_NOTES.md` and `CHANGELOG.md` are left alone so they merge
cleanly. Our notes go here instead. Issues are not enabled on the fork, so
this file is also the bug and feature tracker.

## Branch workflow

- `main` is an untouched mirror of upstream `main`.
- `custom` is our version. The workbench is installed from it.
- New work goes on short-lived feature branches, merged into `custom` by PR.
- Upstream changes come into `main` first, then get merged from `main` into `custom`.
- Anything offered upstream is rebuilt on its own branch from `main`, one fix
  per PR, with upstream's tests and release-notes conventions. Nothing from
  `custom` (this file, `tools/custom/`) goes upstream. Once a PR is accepted,
  the next merge from `main` brings in upstream's version and we drop ours.

## Status key

| Status    | Meaning                                                                     |
| --------- | --------------------------------------------------------------------------- |
| Done      | Merged into `custom`                                                        |
| Proposed  | Agreed in principle, design still being settled                             |
| Backlog   | Known need, not started                                                     |
| Upstream? | Good candidate to offer upstream as a PR, which would remove the difference |

---

## 1. Merged into `custom`

| Change                                                                                                                                                                                                                                                                                     | Files                                            | Status          |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------ | --------------- |
| **Bind sketch dimensions to spreadsheet cells.** `spreadsheet_bind_property` accepts `Constraints[N]` and `Constraints.<Name>` paths, and `add_sketch_constraint` takes an optional `name` so a dimension can be bound by name. Named bindings survive constraints being added or deleted. | `tools/partdesign.py`, `tools/spreadsheet.py`    | Done, Upstream? |
| **Lazy connect (`FREECAD_LAZY_CONNECT`).** The server starts without FreeCAD running and connects on the first tool call instead of exiting.                                                                                                                                               | `server.py`, `config.py`, `resources/freecad.py` | Done, Upstream? |
| **Show expected tool errors to the client.** `ValueError`, `ConnectionError`, `TimeoutError` and file errors reach the client with their real message instead of the MCP SDK's generic "Error executing tool". Messages are capped at 4,000 characters.                                    | `tools/_registration.py`, `tools/__init__.py`    | Done, Upstream? |

### Known merge friction

A trial merge of upstream `main` (Nov 2026, upstream at workbench v0.7.2)
into `custom` stopped on two files:

- `tools/_registration.py`: upstream added tool risk annotations (`file_tool`,
  annotations on `unsafe_tool`) in the same area as our error-reporting
  wrapper. Small, easy to resolve.
- `RELEASE_NOTES.md`: both sides added notes at the top. We stop editing that
  file from now on (see this file).

`tools/partdesign.py` and `tools/spreadsheet.py` merged cleanly even though
both sides had changed them.

---

## 2. Proposed: structure and safety net

These come first, before new tools are added.

### 2.1 `tools/custom/` package for our own tools (Proposed)

- New tools live in `src/freecad_mcp/tools/custom/`, split into modules by
  topic (e.g. `binders.py`, `datums.py`, `clash.py`), not one large file.
- A single `register_custom_tools(mcp, get_bridge)` entry point, hooked into
  `register_all_tools` with one line. That line is the only edit to upstream's
  registration code.
- Follow the CAM pattern rather than code-in-strings: thin MCP tool functions
  that pass JSON arguments to real Python functions running inside FreeCAD.
  That code can then be linted, type-checked and tested like any other module.
- Any names we inject into FreeCAD's execution namespace use a `_p3d_` prefix
  so they can't clash with upstream helpers such as
  `_robust_mcp_cam_dispatch`.
- Tests go in `tests/unit/custom/` and `tests/integration/custom/`.
- Decided: the FreeCAD-side code ships inside the workbench addon, like
  `cam_runtime.py`, in its own module (e.g.
  `freecad/RobustMCPBridge/freecad_mcp_bridge/custom/`). This adds a second
  place we touch upstream's layout, so keep the hook into the bridge to a
  single line there too.
- Use a versioned dispatcher like CAM's (`CAM_API_VERSION`), so a server
  newer than the installed workbench gives a clear "update the workbench"
  error instead of a confusing failure. Changing this code means
  reinstalling the workbench and restarting FreeCAD.

### 2.2 Safe module registration (Proposed)

At the moment, a Python error in any tool module (syntax error, bad import, or
an exception during registration) stops the whole server from starting.
Instead:

- Register each module inside its own `try`/`except`, log the failure clearly,
  and carry on with the rest.
- Report skipped modules somewhere visible, e.g. in `get_connection_status` or
  a startup log line, so a missing toolset is never silent.

### 2.3 Tool-name collision check (Proposed)

The MCP SDK (v2.3.0) keeps the first tool registered under a name and quietly
drops any later one, logging only a warning. If upstream adds a tool with the
same name as one of ours, upstream's version would silently win.

- A unit test that registers upstream and custom tools separately and fails
  on any shared name. The merge then fails loudly in tests and we decide
  whether to drop ours, rename it, or keep it.
- At startup, `register_custom_tools` also checks names and logs an error for
  any duplicate.
- Similar tools under different names (e.g. upstream `create_link` alongside
  our `make_link`) can't be caught automatically. Check for them when
  reviewing each upstream merge.

### 2.4 "Every tool generates valid Python" test (Proposed)

- Call every registered tool with sample arguments against a mock bridge, and
  check that the code it sends to FreeCAD compiles (`compile(code, ...)`).
- Catches the whole class of broken code-in-string bugs without needing
  FreeCAD. Today only the PartDesign, CAM and object-inspection unit tests do
  this, for a handful of tools.
- Sample arguments come from each tool's signature, with an override table for
  tools that need specific values.

---

## 3. Backlog: bugs

| Bug                                                           | Notes                                                                                                                                                                                                                                                                                                                            | Status             |
| ------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------ |
| **Zoom tools need `pivy` and ignore `doc_name`.**             | `pivy` isn't available in a standard FreeCAD install, so the zoom tools fail. They also act on the active view regardless of the `doc_name` passed.                                                                                                                                                                              | Backlog, Upstream? |
| **`spreadsheet_set_cell` reports failure when it succeeded.** | Setting a value with units, e.g. `=40mm`, sets the cell correctly but returns a failure. A false failure could make the AI retry and apply a change twice.                                                                                                                                                                       | Backlog, Upstream? |
| **`uv.lock` points at a private package mirror.**             | Every package in the lockfile resolves from `pkgs.safetycli.com` (upstream's Safety mirror), so `uv sync` may fail for anyone without access to it. Workaround: install into a fresh environment from public PyPI. Regenerating the lockfile on `custom` would conflict with every upstream merge, so raise it upstream instead. | Backlog, Upstream? |

---

## 4. Backlog: features

Found while working on PartDesign models with master sketches, SubShapeBinders
and spreadsheet-driven dimensions.

### 4.1 PartDesign gaps

| Feature                                                | Why                                                                                                                                                                                                                                              | Status                 |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------- |
| **Datum planes attached to edges and vertices.**       | `create_datum_plane` can only offset from XY, XZ or YZ. Needs attachment modes such as normal-to-edge and attachment offsets with rotation, e.g. angled planes along a curved path.                                                              | Backlog                |
| **Sweep and pipe orientation modes.**                  | `sweep_sketch` and `subtractive_pipe` take one profile and one path. Need the orientation mode (Frenet, Binormal, auxiliary spine) and extra cross-sections for variable-section sweeps.                                                         | Backlog                |
| **ShapeBinder and SubShapeBinder.**                    | Needed for master-sketch setups and for using another body's geometry.                                                                                                                                                                           | Backlog                |
| **Projecting edges and faces from another body.**      | `add_external_geometry` works within a body, but a sketch linked straight into a different body hits FreeCAD's "out of allowed scope" error. The usual fix is a SubShapeBinder first, then project from that, so this depends on the item above. | Backlog                |
| **Create `App::Link` objects.**                        | No tool creates links. They currently have to be made by hand in FreeCAD (Make Link), then pointed at the right target.                                                                                                                          | Backlog                |
| **MultiTransform.**                                    | Not covered.                                                                                                                                                                                                                                     | Backlog                |
| **PartDesign primitives and PartDesign helix.**        | Additive and subtractive box, cylinder and so on. A Part-workbench helix exists, but not the PartDesign feature.                                                                                                                                 | Backlog                |
| **Check `create_sketch` can attach to a datum plane.** | Not yet tested. May only need a fix to the attachment code.                                                                                                                                                                                      | Backlog (verify first) |

### 4.2 Clash sweep tool

A tool that rotates one solid around a pivot and reports when it starts to
overlap another. Prototyped with `execute_python` as a dry run. Design notes
from that:

- **Inputs:** moving object, fixed object(s), pivot point, axis, angle range
  and step, both directions.
- **Works on in-memory shape copies** (`Shape.copy()` then `rotate()`), so the
  document is never changed.
- **Speed:** check bounding boxes first and only run the full boolean
  (`common`) when the boxes intersect. Treat overlap below ~0.01 mm³ as noise.
- **Output per direction:** first clash angle, last clear angle, overlap
  volume, and the contact region's centre and bounding box. Optionally, the
  minimum gap and closest point pairs near the limit (`distToShape`).
- **Runtime:** a fine sweep on detailed parts can take minutes, so it needs a
  generous timeout or should report progress. Coarse-then-fine stepping would
  help.
- **Helper wanted:** a way to measure ball-joint centres or pivot points
  (e.g. from a spherical face), so pivots don't have to be entered by hand.
- **Caveat to document:** rigid geometry only. Print tolerances and joint
  friction aren't modelled.

---

## 5. Backlog: testing

Integration tests that call the actual MCP tool functions against a real
FreeCAD, as of Nov 2026:

| Area                                                   | Covered |
| ------------------------------------------------------ | ------- |
| CAM                                                    | 23 / 23 |
| PartDesign                                             | 3 / 49  |
| Draft                                                  | 2 / 6   |
| Documents                                              | 1 / 7   |
| Execution                                              | 1 / 5   |
| Objects, View, Spreadsheet, Export, Macros, Validation | 0       |

The larger "workflow" integration tests send hand-written FreeCAD code
straight to the bridge, so they test the bridge and FreeCAD, not the tools.

- **Smoke test per tool (Backlog).** Call each tool at least once against real
  FreeCAD, starting with sketch, constraints, spreadsheet binding, pad and
  pocket, and export.
- **Local test run on Windows (Backlog).** The `just` test recipes are bash
  scripts that look for `freecad`/`freecadcmd` on the PATH and use `lsof` to
  free ports, so they don't work as-is on Windows. Write down a manual
  Windows procedure, or add a PowerShell equivalent.
- **CI on `custom` (Deferred).** The test workflows only run for pushes and
  PRs to `main`/`master`, so work merged into `custom` isn't tested by CI.

---

## 6. Ideas: later

- **Toolsets.** Let groups of tools be switched on or off, e.g.
  `FREECAD_TOOLSETS=partdesign,spreadsheet,documents,view`. All 170 default
  tool definitions come to roughly 210,000 characters (about 50–65k tokens)
  that the model receives on every turn. PartDesign alone is about 60,000
  characters and CAM about 39,000. Trimming the list matters most for
  smaller local models, and helps any model choose the right tool.
- **Load toolsets on demand.** Start with a small core (documents, view,
  validation) plus `list_toolsets` and `enable_toolset(name)`.
  `enable_toolset` returns a short guide to that workbench with example
  workflows, registers its tools, and tells the client the tool list has
  changed (the MCP `tools/list_changed` notification, which SDK v2.3.0
  supports). The tools keep their real names and schemas, so per-tool
  approval settings in the client still work. Depends on the client
  refreshing its tool list when notified; check which clients do before
  relying on it.
- **Avoid a single "call any tool" dispatcher.** One tool that takes a tool
  name and arguments would be smallest, but it hides which tool runs from
  the client's approval settings (including the unsafe Python tools) and
  loses argument checking. Only consider it as an opt-in fallback for
  clients that can't refresh their tool list.
- **Shorter tool descriptions.** Descriptions account for about half of that
  total. Moving long examples out of descriptions and into MCP prompts or
  resources would cut the per-turn cost.

---

## 7. Housekeeping

- [ ] Decide which "Upstream?" items to offer to spkane as PRs, and which to
      raise as issues only.
- [ ] Add each new custom change to section 1 when it merges.
