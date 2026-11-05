"""完整类别说明的严格语法；不猜词、不改原文、不处理未闭合括号。"""

import re
import unicodedata


CATEGORY_ANNOTATIONS = {
    "重击时x2": "crit_chance",
    "弓类武器效果加倍": "fire_rate",
}
BRACKETS = "()（）"


def split_tail_annotation(text):
    """返回原文主体及说明；只认可一个完整、单层、已知的尾部括注。

    此处尚不判断类别；调用方必须再用完整主体的 exact SID 与 required_sid
    对拍。base 保留原字符，span 只定位说明，不能据此修改原始 OCR。
    """
    result = {
        "present": False,
        "valid": True,
        "base": text,
        "annotation": None,
        "required_sid": None,
        "span": None,
    }
    if not isinstance(text, str):
        return {**result, "valid": False}
    if not any(char in text for char in BRACKETS):
        return result
    result["present"] = True
    match = re.fullmatch(
        r"(?P<base>[^()（）]+?)(?P<annotation>[（(](?P<body>[^()（）]*)[)）])\s*", text
    )
    if not match:
        return {**result, "valid": False}
    body = re.sub(r"\s+", "", unicodedata.normalize("NFKC", match["body"]).casefold())
    sid = CATEGORY_ANNOTATIONS.get(body)
    if sid is None or not match["base"].strip():
        return {**result, "valid": False}
    return {
        **result,
        "base": match["base"].rstrip(),
        "annotation": match["annotation"],
        "required_sid": sid,
        "span": list(match.span("annotation")),
    }


def exact_annotated_name(name, exact):
    """有括注时只作类别绑定后的整名exact；失败绝不调用fuzzy。"""
    annotation = split_tail_annotation(name)
    if not annotation["present"] or not annotation["valid"]:
        return annotation, None, name
    base = annotation["base"]
    compact = re.sub(r"\s+", "", base)
    for candidate in (base, compact) if compact != base else (base,):
        sid = exact(candidate)
        if sid == annotation["required_sid"]:
            return annotation, sid, candidate
    return annotation, None, base
