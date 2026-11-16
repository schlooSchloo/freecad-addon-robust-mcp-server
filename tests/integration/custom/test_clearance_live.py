"""Clearance tools against real FreeCAD, with fixtures that have known answers.

- check_overlap: boxes 2.5 mm apart, touching, overlapping by an exact
  volume, posed copies, faces only, a box inside a placed Body, and links.
- sweep_clash: an arm on a pin swinging into stop walls at angles worked out
  from the geometry; the same hinge with a non-identity placement and inside
  a placed container with the arm already turned; a ball gripped by its
  socket (intended interference) with a separate structural stop.
- find_sphere_centres: the ball and the socket.

Every test checks that the document is unchanged afterwards.
"""

import math
from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio

from freecad_mcp.bridge.xmlrpc import XmlRpcBridge
from freecad_mcp.tools.custom.clearance import register_clearance_tools
from freecad_mcp.tools.custom.runtime import run_operation
from tests.integration.auth_support import integration_credentials

pytestmark = pytest.mark.integration

_BOXES = """
import FreeCAD
V = FreeCAD.Vector
def box(name, x, y, z, size=10.0):
    obj = doc.addObject("Part::Box", name)
    obj.Length = obj.Width = obj.Height = size
    obj.Placement.Base = V(x, y, z)
    return obj
box("A", 0, 0, 0)
box("Gap", 12.5, 0, 0)
box("Touch", 10, 0, 0)
box("Over", 7, 3, 4)
body = doc.addObject("PartDesign::Body", "Body")
body.Placement.Base = V(100, 0, 0)
pbox = body.newObject("PartDesign::AdditiveBox", "BodyBox")
pbox.Length = pbox.Width = pbox.Height = 10
box("NearBody", 105, 0, 0)
link = doc.addObject("App::Link", "LinkA")
link.LinkedObject = doc.getObject("A")
link.Placement.Base = V(5, 0, 0)
group = doc.addObject("App::Part", "Group")
group.Placement.Base = V(0, 5, 0)
inner = doc.addObject("App::Link", "GroupLinkA")
inner.LinkedObject = doc.getObject("A")
group.addObject(inner)
"""

# Hinge: arm x -3..20, y -1.5..1.5, z 0..2 with a 1 mm hole on a 0.8 mm pin
# at the origin; walls at y >= 8 and y <= -12.
_HINGE = """
import FreeCAD, Part
V = FreeCAD.Vector
arm_shape = Part.makeBox(23, 3, 2, V(-3, -1.5, 0)).cut(
    Part.makeCylinder(1.0, 4, V(0, 0, -1)))
shapes = {
    "Arm": arm_shape,
    "Pin": Part.makeCylinder(0.8, 4, V(0, 0, -1)),
    "WallPos": Part.makeBox(30, 5, 4, V(0, 8, -1)),
    "WallNeg": Part.makeBox(30, 5, 4, V(0, -17, -1)),
}
objs = {}
for name, shape in shapes.items():
    obj = doc.addObject("Part::Feature", name)
    obj.Shape = shape
    objs[name] = obj
"""

# Ball r 5 with a neck r 2, 12 long, along +Z; socket cavity r 4.9 (0.1 mm
# grip) in a cylinder r 8 from z -8 to 1; a stop wall at y -11..-6, z 4..20.
_BALL = """
import FreeCAD, Part
V = FreeCAD.Vector
ball = Part.makeSphere(5).fuse(Part.makeCylinder(2, 12)).removeSplitter()
socket = Part.makeCylinder(8, 9, V(0, 0, -8)).cut(Part.makeSphere(4.9))
stop = Part.makeBox(10, 5, 16, V(-5, -11, 4))
place = FreeCAD.Placement(V(10, 20, 30), FreeCAD.Rotation())
for name, shape in (("Ball", ball), ("Socket", socket), ("Stop", stop)):
    obj = doc.addObject("Part::Feature", name)
    obj.Shape = shape
    obj.Placement = place
"""

_STATE = """
import FreeCAD
d = FreeCAD.getDocument({doc!r})
_result_ = {{
    "undo": d.UndoCount,
    "touched": bool(d.isTouched()),
    "objects": sorted(o.Name for o in d.Objects),
    "placements": {{o.Name: str(o.Placement) for o in d.Objects
                    if hasattr(o, "Placement")}},
}}
"""


def _hinge_angle(wall_y: float) -> float:
    """Angle (degrees) at which the arm's far corner reaches a wall at |y|."""
    reach = math.hypot(20.0, 1.5)
    return math.degrees(math.asin(wall_y / reach) - math.atan2(1.5, 20.0))


# Ball joint known answers.
STOP_ANGLE = math.degrees(math.asin(6 / math.hypot(12, 2)) - math.atan2(2, 12))
RIM_ANGLE = math.degrees(math.atan2(math.sqrt(24), 1) - math.asin(2 / 5))
# Ball r 5 minus cavity r 4.9, below the socket top (z <= 1).
GRIP_VOLUME = (108 - 5.9**2 * (3 * 4.9 - 5.9) / 3) * math.pi


@pytest_asyncio.fixture
async def clearance() -> AsyncIterator[Any]:
    """Connect, register the clearance tools, and clean up documents."""
    bridge = XmlRpcBridge(**integration_credentials())
    await bridge.connect()
    tools: dict[str, Callable[..., Any]] = {}
    server = MagicMock()

    def tool(**_kwargs: Any) -> Callable[[Any], Any]:
        def register(function: Any) -> Any:
            tools[function.__name__] = function
            return function

        return register

    server.tool = tool

    async def get_bridge() -> XmlRpcBridge:
        return bridge

    register_clearance_tools(server, get_bridge)
    docs: list[str] = []

    class Session:
        """Helpers shared by the tests."""

        def __init__(self) -> None:
            self.tools = tools

        async def make(self, setup: str, extra: str = "") -> str:
            doc = f"McpClearance_{uuid4().hex[:12]}"
            docs.append(doc)
            code = (
                f"import FreeCAD\ndoc = FreeCAD.newDocument({doc!r})\n"
                f"{setup}\n{extra}\ndoc.recompute()\n_result_ = True"
            )
            result = await bridge.execute_python(code)
            assert result.success, result.error_traceback or result.stderr
            return doc

        async def state(self, doc: str) -> dict[str, Any]:
            result = await bridge.execute_python(_STATE.format(doc=doc))
            assert result.success, result.error_traceback
            return result.result

    try:
        yield Session()
    finally:
        for doc in docs:
            await bridge.execute_python(
                f"import FreeCAD\nif {doc!r} in FreeCAD.listDocuments():\n"
                f"    FreeCAD.closeDocument({doc!r})\n_result_ = True"
            )
        await bridge.disconnect()


# --- check_overlap -----------------------------------------------------------


@pytest.mark.asyncio
async def test_check_overlap_known_answers(clearance: Any) -> None:
    """Separate, touching, overlapping and posed boxes give exact numbers."""
    doc = await clearance.make(_BOXES)
    before = await clearance.state(doc)
    check = clearance.tools["check_overlap"]

    gap = await check("A", "Gap", doc_name=doc)
    assert gap["status"] == "separate"
    assert gap["distance"] == pytest.approx(2.5, abs=1e-6)
    assert gap["overlap_volume"] == 0
    assert gap["closest_points"]["on_a"][0] == pytest.approx(10)
    assert gap["closest_points"]["on_b"][0] == pytest.approx(12.5)
    assert any("does not prove clearance" in note for note in gap["notes"])

    touch = await check("A", "Touch", doc_name=doc)
    assert touch["status"] == "touching"
    assert touch["distance"] == pytest.approx(0, abs=1e-9)
    assert touch["overlap_volume"] == pytest.approx(0, abs=1e-9)

    over = await check("A", "Over", doc_name=doc)
    assert over["status"] == "overlapping"
    assert over["overlap_volume"] == pytest.approx(3 * 7 * 6, rel=1e-9)
    assert over["overlap_region"]["centre"] == pytest.approx([8.5, 6.5, 7.0])
    assert over["overlap_region"]["bound_box"] == {
        "min": pytest.approx([7, 3, 4]),
        "max": pytest.approx([10, 10, 10]),
    }

    # Gap moved 2.5 mm towards A touches it.
    posed = await check("A", "Gap", pose_b={"offset": [-2.5, 0, 0]}, doc_name=doc)
    assert posed["status"] == "touching"
    turned = await check(
        "A",
        "Gap",
        pose_b={"axis": [0, 0, 1], "angle": 90, "pivot": [12.5, 0, 0]},
        doc_name=doc,
    )
    # Turned +90° about z through (12.5, 0): the box spans x 2.5..12.5,
    # y 0..10, overlapping A over x 2.5..10: 7.5 * 10 * 10.
    assert turned["status"] == "overlapping"
    assert turned["overlap_volume"] == pytest.approx(750, rel=1e-9)

    faces = await check(
        "A", "Gap", elements_a=["Face2"], elements_b=["Face1"], doc_name=doc
    )
    assert faces["status"] == "separate"
    assert faces["overlap_volume"] is None
    assert faces["distance"] == pytest.approx(2.5)

    # The Body is placed at x = 100; its box must be compared in global
    # coordinates: x 100..110 against 105..115 gives 5 * 10 * 10.
    body = await check("BodyBox", "NearBody", doc_name=doc)
    assert body["status"] == "overlapping"
    assert body["overlap_volume"] == pytest.approx(500, rel=1e-9)

    # Links: the link's own placement and its container's both apply.
    link = await check("A", "LinkA", doc_name=doc)
    assert link["overlap_volume"] == pytest.approx(500, rel=1e-9)
    grouped = await check("A", "GroupLinkA", doc_name=doc)
    assert grouped["overlap_volume"] == pytest.approx(500, rel=1e-9)

    assert await clearance.state(doc) == before


@pytest.mark.asyncio
async def test_check_overlap_rejects_unknown_objects(clearance: Any) -> None:
    """A missing object is a clear error, not a result."""
    doc = await clearance.make(_BOXES)
    with pytest.raises(ValueError, match="Object not found"):
        await clearance.tools["check_overlap"]("A", "Nope", doc_name=doc)


# --- sweep_clash: hinge -------------------------------------------------------


def _direction(result: dict[str, Any], name: str) -> dict[str, Any]:
    return next(d for d in result["directions"] if d["direction"] == name)


def _assert_bracket(report: dict[str, Any], expected: float, precision: float) -> None:
    """The refined bracket must hold the analytic contact angle."""
    assert report["status"] == "clash", report
    clear = abs(report["last_clear_angle"])
    clash = abs(report["first_clash_angle"])
    assert clash - clear <= precision + 1e-9
    # The threshold (0.001 mm³) delays detection very slightly.
    assert clear <= expected + 0.1
    assert clash >= expected - 1e-6


@pytest.mark.asyncio
async def test_sweep_hinge_stops(clearance: Any) -> None:
    """Both directions find the wall at the angle the geometry gives."""
    doc = await clearance.make(_HINGE)
    before = await clearance.state(doc)
    result = await clearance.tools["sweep_clash"](
        ["Arm"],
        ["Pin", "WallPos", "WallNeg"],
        pivot=[0, 0, 0],
        axis=[0, 0, 1],
        min_angle=-60,
        max_angle=60,
        step=5,
        precision=0.5,
        doc_name=doc,
    )
    assert result["status"] == "done"
    assert result["start_pose"]["clashes"] is False
    positive = _direction(result, "positive")
    negative = _direction(result, "negative")
    _assert_bracket(positive, _hinge_angle(8), 0.5)
    _assert_bracket(negative, _hinge_angle(12), 0.5)
    assert positive["limiting_pair"] == {"moving": "Arm", "fixed": "WallPos"}
    assert negative["limiting_pair"] == {"moving": "Arm", "fixed": "WallNeg"}
    assert negative["first_clash_angle"] < 0
    # The contact is at the arm's far corner, near x 18.5, y 8.
    centre = positive["contact_region"]["centre"]
    assert centre[0] == pytest.approx(18.5, abs=0.5)
    assert centre[1] == pytest.approx(8, abs=0.1)
    gap = positive["gap_at_last_clear"]
    assert gap["pair"] == ["Arm", "WallPos"]
    assert 0 < gap["distance"] < 0.5 * math.pi / 180 * 20.1
    assert result["inputs"]["axis"] == [0, 0, 1]
    assert result["sample_count"] <= 200
    assert result["log_truncated"] is (result["sample_count"] > 40)
    assert any("not as a continuous sweep" in note for note in result["notes"])
    assert await clearance.state(doc) == before


@pytest.mark.asyncio
async def test_sweep_hinge_with_non_identity_placement(clearance: Any) -> None:
    """A hinge placed off-origin and tilted gives the same limits."""
    extra = """
Q = FreeCAD.Placement(V(50, -20, 10), FreeCAD.Rotation(V(1, 1, 0), 30))
for obj in objs.values():
    obj.Placement = Q
axis = Q.Rotation.multVec(V(0, 0, 1))
"""
    doc = await clearance.make(_HINGE, extra)
    before = await clearance.state(doc)
    # Rodrigues: +Z turned 30° about (1, 1, 0) / sqrt(2).
    half = 0.5 / math.sqrt(2)
    q_axis = [half, -half, math.cos(math.radians(30))]
    result = await clearance.tools["sweep_clash"](
        ["Arm"],
        ["Pin", "WallPos", "WallNeg"],
        pivot=[50, -20, 10],
        axis=q_axis,
        min_angle=-60,
        max_angle=60,
        doc_name=doc,
    )
    _assert_bracket(_direction(result, "positive"), _hinge_angle(8), 0.5)
    _assert_bracket(_direction(result, "negative"), _hinge_angle(12), 0.5)
    assert await clearance.state(doc) == before


@pytest.mark.asyncio
async def test_sweep_hinge_in_container_with_turned_arm(clearance: Any) -> None:
    """Container placement applies, and the arm's own start angle is kept."""
    extra = """
group = doc.addObject("App::Part", "Assembly")
group.Placement = FreeCAD.Placement(V(0, 0, 25), FreeCAD.Rotation())
for obj in objs.values():
    group.addObject(obj)
objs["Arm"].Placement = FreeCAD.Placement(
    V(0, 0, 0), FreeCAD.Rotation(V(0, 0, 1), 10), V(0, 0, 0))
"""
    doc = await clearance.make(_HINGE, extra)
    before = await clearance.state(doc)
    result = await clearance.tools["sweep_clash"](
        ["Arm"],
        ["Pin", "WallPos", "WallNeg"],
        pivot=[0, 0, 25],
        axis=[0, 0, 1],
        min_angle=-60,
        max_angle=60,
        doc_name=doc,
    )
    # Starting 10° turned: 10° less travel one way, 10° more the other.
    _assert_bracket(_direction(result, "positive"), _hinge_angle(8) - 10, 0.5)
    _assert_bracket(_direction(result, "negative"), _hinge_angle(12) + 10, 0.5)
    assert await clearance.state(doc) == before


# --- sweep_clash: ball joint ----------------------------------------------------


@pytest.mark.asyncio
async def test_ball_joint_start_overlap_is_reported(clearance: Any) -> None:
    """Without an allowance the gripped ball stops the sweep at the start."""
    doc = await clearance.make(_BALL)
    result = await clearance.tools["sweep_clash"](
        ["Ball"],
        ["Socket", "Stop"],
        pivot=[10, 20, 30],
        axis=[1, 0, 0],
        min_angle=-70,
        max_angle=70,
        doc_name=doc,
    )
    assert result["status"] == "start_pose_overlaps"
    overlaps = result["start_pose"]["overlaps"]
    assert overlaps == [
        {
            "moving": "Ball",
            "fixed": "Socket",
            "overlap_volume": pytest.approx(GRIP_VOLUME, rel=1e-6),
        }
    ]
    assert "directions" not in result


@pytest.mark.asyncio
async def test_ball_joint_allow_start_overlap(clearance: Any) -> None:
    """Growth above the grip finds the stop one way and the rim the other."""
    doc = await clearance.make(_BALL)
    before = await clearance.state(doc)
    result = await clearance.tools["sweep_clash"](
        ["Ball"],
        ["Socket", "Stop"],
        pivot=[10, 20, 30],
        axis=[1, 0, 0],
        min_angle=-70,
        max_angle=70,
        allow_start_overlap=True,
        doc_name=doc,
    )
    assert result["status"] == "done"
    positive = _direction(result, "positive")
    negative = _direction(result, "negative")
    _assert_bracket(positive, STOP_ANGLE, 0.5)
    assert positive["limiting_pair"] == {"moving": "Ball", "fixed": "Stop"}
    assert positive["gap_at_last_clear"]["distance"] > 0
    # The rim contact grows from a cylinder meeting an edge, more slowly than
    # a corner, so allow a little more detection delay.
    assert negative["status"] == "clash"
    assert negative["limiting_pair"] == {"moving": "Ball", "fixed": "Socket"}
    assert abs(negative["last_clear_angle"]) <= RIM_ANGLE + 0.5
    assert abs(negative["first_clash_angle"]) >= RIM_ANGLE - 1e-6
    # The grip is reported as a total; the clash volume is only the growth.
    assert negative["overlap_volume_total"] > negative["overlap_volume"]
    region = negative["contact_region"]
    assert region["excludes_start_overlap"] is True
    # New contact is at the rim on the +y side (y > 20 + 4), near z 31.
    assert region["centre"][1] > 24
    assert region["centre"][2] == pytest.approx(31, abs=1)
    gap = negative["gap_at_last_clear"]
    assert gap["distance"] is None and "start" in gap["reason"]
    assert await clearance.state(doc) == before


@pytest.mark.asyncio
async def test_ball_joint_exclude_pairs(clearance: Any) -> None:
    """Excluding the socket still finds the separate stop, but not the rim."""
    doc = await clearance.make(_BALL)
    result = await clearance.tools["sweep_clash"](
        ["Ball"],
        ["Socket", "Stop"],
        pivot=[10, 20, 30],
        axis=[1, 0, 0],
        min_angle=-70,
        max_angle=70,
        exclude_pairs=[["Ball", "Socket"]],
        doc_name=doc,
    )
    assert result["start_pose"]["clashes"] is False
    positive = _direction(result, "positive")
    _assert_bracket(positive, STOP_ANGLE, 0.5)
    negative = _direction(result, "negative")
    assert negative["status"] == "clear_to_limit"
    assert negative["last_clear_angle"] == -70
    assert result["inputs"]["exclude_pairs"] == [["Ball", "Socket"]]
    assert any("Excluded pairs were not tested" in n for n in result["notes"])


# --- find_sphere_centres ------------------------------------------------------


@pytest.mark.asyncio
async def test_find_sphere_centres_ball_and_socket(clearance: Any) -> None:
    """The ball is convex r 5 and the socket concave r 4.9, both at the pivot."""
    doc = await clearance.make(_BALL)
    before = await clearance.state(doc)
    find = clearance.tools["find_sphere_centres"]

    ball = await find("Ball", doc_name=doc)
    assert len(ball["spheres"]) == 1
    sphere = ball["spheres"][0]
    assert sphere["centre"] == pytest.approx([10, 20, 30])
    assert sphere["radius"] == pytest.approx(5)
    assert sphere["kind"] == "convex"
    assert 0.9 < sphere["coverage"] < 1.0  # the neck takes a small cap

    socket = await find("Socket", doc_name=doc)
    cavity = socket["spheres"][0]
    assert cavity["centre"] == pytest.approx([10, 20, 30])
    assert cavity["radius"] == pytest.approx(4.9)
    assert cavity["kind"] == "concave"

    one = await find("Socket", faces=[cavity["faces"][0]], doc_name=doc)
    assert one["spheres"][0]["radius"] == pytest.approx(4.9)
    stop = await find("Stop", doc_name=doc)
    assert stop["spheres"] == [] and stop["truncated"] is False
    assert await clearance.state(doc) == before


@pytest.mark.asyncio
async def test_sweep_budget_limits_return_partial_results(clearance: Any) -> None:
    """Sample and time limits stop the sweep with a status, not an error."""
    doc = await clearance.make(_HINGE)
    bridge = XmlRpcBridge(**integration_credentials())
    await bridge.connect()

    async def get_bridge() -> XmlRpcBridge:
        return bridge

    base = {
        "moving": ["Arm"],
        "fixed": ["WallPos", "WallNeg"],
        "pivot": [0, 0, 0],
        "axis": [0, 0, 1],
        "min_angle": -60,
        "max_angle": 60,
        "doc_name": doc,
    }
    try:
        # Start pose + 2 coarse samples, then the cap is reached.
        capped = await run_operation(
            get_bridge, "sweep_clash", {**base, "max_samples": 3}
        )
        positive = _direction(capped, "positive")
        assert positive["status"] == "sample_limit"
        assert positive["last_clear_angle"] == 10
        assert _direction(capped, "negative")["status"] == "sample_limit"
        timed = await run_operation(
            get_bridge, "sweep_clash", {**base, "time_limit_s": 0}
        )
        assert {d["status"] for d in timed["directions"]} == {"time_limit"}
    finally:
        await bridge.disconnect()
