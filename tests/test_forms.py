"""Web 表单解析测试。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roleswap.workflow_template import DEFAULT_POSITIVE_PROMPT
from web.forms import parse_workflow_options, validate_workflow_options


def test_parse_workflow_options_defaults():
    opts = parse_workflow_options({})
    assert opts.mode == "role_swap"
    assert opts.steps == 6
    assert opts.frame_load_cap == 121
    assert opts.positive_prompt == DEFAULT_POSITIVE_PROMPT
    assert validate_workflow_options(opts) is None
    assert validate_workflow_options(opts, slice_mode="single") is None
    opts2 = parse_workflow_options({"frame_load_cap": "288"})
    assert validate_workflow_options(opts2, slice_mode="single") is None
    print("parse defaults OK")


def test_parse_workflow_options_motion_transfer():
    opts = parse_workflow_options({
        "mode": "motion_transfer",
        "steps": "8",
        "cfg": "1.2",
        "shift": "6",
        "seed": "42",
        "positive_prompt": "hello",
        "pose_strength": "0.8",
    })
    assert opts.mode == "motion_transfer"
    assert opts.steps == 8
    assert opts.seed == 42
    assert opts.positive_prompt == "hello"
    assert opts.pose_strength == 0.8
    print("parse motion_transfer OK")


def test_parse_matting_options():
    opts = parse_workflow_options({
        "refine_foreground": "1",
        "rem_add_background": "green",
        "ref_background_color": "#FFFFFF",
        "detection_threshold": "0.45",
        "ref_strength": "0.9",
    })
    assert opts.refine_foreground is True
    assert opts.rem_add_background == "green"
    assert opts.ref_background_color == "#FFFFFF"
    assert opts.detection_threshold == 0.45
    assert opts.ref_strength == 0.9
    assert validate_workflow_options(opts) is None
    print("parse matting OK")


def test_build_payload_matting_fields():
    from roleswap import workflow_template as wf

    opts = wf.WorkflowOptions(
        refine_foreground=True,
        rem_add_background="green",
        ref_background_color="#FFFFFF",
        detection_threshold=0.45,
    )
    payload = wf.build_payload(
        workflow_id="wf-1", video="v", image="i", seed=1, options=opts
    )
    values = payload["input_values"]
    assert values["104:refine_foreground"] is True
    assert values["104:add_background"] == "green"
    assert values["48:background_color"] == "#FFFFFF"
    assert values["91:detection_threshold"] == 0.45
    print("build_payload matting OK")


if __name__ == "__main__":
    test_parse_workflow_options_defaults()
    test_parse_workflow_options_motion_transfer()
    test_parse_matting_options()
    test_build_payload_matting_fields()
    print("\nALL FORM TESTS PASSED")
