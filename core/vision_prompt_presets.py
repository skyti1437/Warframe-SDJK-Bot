"""固定视觉方案：原版引用当前真源，候选仅覆盖识卡面板，不自动启用。

离线边界：合成 JSON 仅证明现有 parser 的协议兼容；不证明真实模型遵守提示词，
不证明识别准确率、MOD 等级、赋能条件或伤害重建。现 parser 仍可接收标题填数。
"""

from __future__ import annotations

from hashlib import sha256

from .commands.vision import original_vision_prompt
from .vision_prompts import TASKS

_SCAN_PANEL_JSON_V2_DRAFT = '只抄这张图左侧属性与伤害面板中可见的原文，只输出下面结构的JSON对象，不要Markdown或解释：\n{"panel":{"attack_speed":null,"crit_chance":null,"crit_damage":null,"status_chance":null,"damage_rows":[],"total":null,"heavy":null}}\nattack_speed对应攻击或射击速度，crit_chance对应暴击几率，crit_damage对应暴击伤害或倍率，status_chance对应触发几率。各字段抄对应行的数值原文，不抄行名、分区标题或装备标题；标题或行名不能代替数值。\n数值使用字符串，原样保留小数、千分位、正负号、单位，以及同一行左右两列的数值和箭头；不能只保留右列，不能把相邻行的数字移到本行。\ndamage_rows按面板从上到下的原顺序逐行记录，每项为[行名原文,该行数值原文]；保留属性行、伤害类型行、总计行和重击行，重复行分别保留，不合并。行中有左右两列时，两列和分隔符一起保留在该行的数值字符串中。\ntotal只抄总计行的数值原文，heavy只抄重击行的数值原文。没有对应行或数值看不清时填null；已看清行名但数值看不清的行仍保留，数值填null。没有读到的行不新增。\n只按图抄录，不计算、不推算、不换算、不补数值、不补行，也不依据其他字段修正原文。'

_PROTOCOL_BOUNDARY = (
    "离线检查仅验证parser协议；真实对照仅覆盖两Omni各三图的scan_panel单次读取，不验证稳定性、MOD等级、赋能条件或伤害重建。"
    "scan_panel的JSON数值字段误填标题仍会被现parser接收，不能视为防护通过。"
)


def preset_catalog() -> list[dict]:
    """管理页摘要；不暴露提示词全文，每次返回独立容器。"""
    return [
        {
            "id": "legacy_v1",
            "label": "原版方案",
            "status": "原版基线",
            "overridden_tasks": [],
            "model_evaluation": "existing_task_baseline",
            "offline_validation": "仅parser协议",
            "description": "各项任务引用当前原版提示词；沿用现有分任务验证记录，不提供跨模型保证。",
            "protocol_boundary": "沿用现有分任务协议与验证记录；不表示跨模型准确率保证。",
        },
        {
            "id": "scan_panel_json_v2_draft",
            "label": "识卡面板JSON草案",
            "status": "实验",
            "overridden_tasks": ["scan_panel"],
            "model_evaluation": "limited_scan_panel_samples",
            "evaluated_models": ["qwen3.5-omni-flash", "qwen3-omni-flash"],
            "samples_per_model": 3,
            "repeats_per_image_and_scheme": 1,
            "evaluation_date": "2026-11-02",
            "offline_validation": "仅parser协议",
            "description": "仅覆盖识卡面板，其余任务继承原版；已做两Omni各三图对照，仍有缺失和额外字段错放，保留实验状态。",
            "protocol_boundary": _PROTOCOL_BOUNDARY,
        },
    ]


def preset_prompt(preset_id: str, task: str) -> str:
    """取得可用于保存草稿的原文；本函数不写文件、不启用方案。"""
    if preset_id not in ("legacy_v1", "scan_panel_json_v2_draft"):
        raise ValueError(f"未知视觉方案：{preset_id}")
    if not isinstance(task, str) or task not in TASKS:
        raise ValueError(f"未知视觉任务：{task}")
    if preset_id == "scan_panel_json_v2_draft" and task == "scan_panel":
        return _SCAN_PANEL_JSON_V2_DRAFT
    return original_vision_prompt(task)


def preset_detail(preset_id: str, task: str) -> dict:
    """返回指定任务原文、UTF-8摘要及是否引用原版。"""
    prompt = preset_prompt(preset_id, task)
    return {
        "id": preset_id,
        "task": task,
        "prompt": prompt,
        "sha256": sha256(prompt.encode("utf-8")).hexdigest(),
        "inherited": not (preset_id == "scan_panel_json_v2_draft" and task == "scan_panel"),
        "status": "原版基线" if preset_id == "legacy_v1" else "实验",
    }
