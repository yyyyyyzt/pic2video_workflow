"""Web 表单解析测试。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.forms import parse_workflow_options, validate_workflow_options


def test_parse_workflow_options_defaults():
    opts = parse_workflow_options({})
    assert opts.mode == "role_swap"
    assert opts.steps == 6
    assert opts.frame_load_cap == 121
    assert validate_workflow_options(opts) is None
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


if __name__ == "__main__":
    test_parse_workflow_options_defaults()
    test_parse_workflow_options_motion_transfer()
    print("\nALL FORM TESTS PASSED")
