"""Web 表单解析：把 HTTP 参数转为 WorkflowOptions。"""

from __future__ import annotations

from typing import Any, Mapping

from roleswap.workflow_template import DEFAULT_NEGATIVE_PROMPT, WorkflowOptions


def _get_int(form: Mapping[str, Any], key: str, default: int) -> int:
    raw = form.get(key, default)
    if raw is None or str(raw).strip() == "":
        return default
    return int(raw)


def _get_float(form: Mapping[str, Any], key: str, default: float) -> float:
    raw = form.get(key, default)
    if raw is None or str(raw).strip() == "":
        return default
    return float(raw)


def _get_bool(form: Mapping[str, Any], key: str, default: bool = False) -> bool:
    if hasattr(form, "getlist"):
        values = form.getlist(key)  # type: ignore[attr-defined]
        if not values:
            return default
        return any(str(v).strip().lower() in {"1", "true", "on", "yes"} for v in values)
    raw = form.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "on", "yes"}


def parse_workflow_options(form: Mapping[str, Any]) -> WorkflowOptions:
    """从 multipart 表单解析工作流可调参数。"""
    seed_raw = str(form.get("seed", "")).strip()
    seed = int(seed_raw) if seed_raw else None

    return WorkflowOptions(
        mode=str(form.get("mode", "role_swap")),
        steps=_get_int(form, "steps", 6),
        cfg=_get_float(form, "cfg", 1.0),
        shift=_get_float(form, "shift", 5.0),
        seed=seed,
        frame_load_cap=_get_int(form, "frame_load_cap", 121),
        output_width=_get_int(form, "output_width", 896),
        fps=_get_int(form, "fps", 24),
        positive_prompt=str(form.get("positive_prompt", "")),
        negative_prompt=str(form.get("negative_prompt", DEFAULT_NEGATIVE_PROMPT)),
        pose_strength=_get_float(form, "pose_strength", 1.0),
        ref_strength=_get_float(form, "ref_strength", 0.9),
        context_overlap=_get_int(form, "context_overlap", 16),
        refine_foreground=_get_bool(form, "refine_foreground", False),
        rem_add_background=str(form.get("rem_add_background", "green")).strip() or "green",
        preserve_main_ref_background=_get_bool(
            form, "preserve_main_ref_background", False
        ),
        prefix_alpha_crop=_get_bool(form, "prefix_alpha_crop", False),
        detection_threshold=_get_float(form, "detection_threshold", 0.5),
        ref_background_color=str(form.get("ref_background_color", "#FFFFFF")).strip()
        or "#FFFFFF",
    )


def validate_workflow_options(opts: WorkflowOptions) -> str | None:
    """校验参数范围，返回错误信息或 None。"""
    if opts.mode not in {"role_swap", "motion_transfer"}:
        return "mode 应为 role_swap 或 motion_transfer"
    if not (1 <= opts.steps <= 30):
        return "steps 建议在 1~30 之间"
    if not (0.1 <= opts.cfg <= 5.0):
        return "cfg 建议在 0.1~5.0 之间"
    if not (0.0 <= opts.shift <= 20.0):
        return "shift 建议在 0~20 之间"
    if not (1 <= opts.frame_load_cap <= 121):
        return "frame_load_cap 建议在 1~121 之间（工作流硬上限）"
    if not (0 <= opts.output_width <= 4096):
        return "output_width 建议在 0~4096 之间"
    if not (1 <= opts.fps <= 60):
        return "fps 建议在 1~60 之间"
    if not (0.0 <= opts.pose_strength <= 2.0):
        return "pose_strength 建议在 0~2 之间"
    if not (0.0 <= opts.ref_strength <= 2.0):
        return "ref_strength 建议在 0~2 之间"
    if not (0 <= opts.context_overlap <= 32):
        return "context_overlap 建议在 0~32 之间"
    if not (0.0 <= opts.detection_threshold <= 1.0):
        return "detection_threshold 建议在 0~1 之间"
    allowed_bg = {"black", "white", "green", "none", "transparent"}
    if opts.rem_add_background not in allowed_bg:
        return f"rem_add_background 应为 {sorted(allowed_bg)} 之一"
    color = opts.ref_background_color.strip()
    if not (color.startswith("#") and len(color) in {4, 7}):
        return "ref_background_color 应为 #RGB 或 #RRGGBB 格式"
    return None
