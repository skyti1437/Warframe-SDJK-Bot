"""把 wiki 数据模块（Module:*/data，`return { ... }` 纯字面量）解析成 Python 对象。

只支持数据模块会用到的子集：表、字符串、数字、true/false/nil、math.huge、`--` 与 `--[[ ]]` 注释。
纯数组表（只有位置元素）返回 list，其余返回 dict（位置元素用 1 起的整数键）。
"""

from __future__ import annotations

import re

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<lcomment>--\[(?P<leq>=*)\[.*?\](?P=leq)\])
  | (?P<comment>--[^\n]*)
  | (?P<lstr>\[(?P<seq>=*)\[.*?\](?P=seq)\])
  | (?P<str>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
  | (?P<num>-?(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?))
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)
  | (?P<punct>[{}\[\]=,;])
    """,
    re.S | re.X,
)

_ESC = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "\\": "\\",
    '"': '"',
    "'": "'",
    "\n": "\n",
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "v": "\v",
}


def _unescape(body: str) -> str:
    out, i = [], 0
    while i < len(body):
        c = body[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        n = body[i + 1]
        if n.isdigit():
            m = re.match(r"\d{1,3}", body[i + 1 :])
            out.append(chr(int(m.group())))
            i += 1 + len(m.group())
        elif n == "u" and body[i + 2] == "{":
            j = body.index("}", i)
            out.append(chr(int(body[i + 3 : j], 16)))
            i = j + 1
        else:
            out.append(_ESC.get(n, n))
            i += 2
    return "".join(out)


def _tokens(src: str):
    pos = 0
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m:
            raise ValueError(f"无法解析的 Lua 字符 @{pos}: {src[pos : pos + 40]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind in ("leq", "seq"):
            kind = "lcomment" if m.group("lcomment") else "lstr"
        if kind in ("ws", "comment", "lcomment"):
            continue
        text = m.group(kind)
        if kind == "str":
            yield ("val", _unescape(text[1:-1]))
        elif kind == "lstr":
            eq = len(m.group("seq"))
            body = text[2 + eq : -2 - eq]
            yield ("val", body[1:] if body.startswith("\n") else body)
        elif kind == "num":
            yield (
                "val",
                # Lua 十进制允许前导零（0075 = 75），Python int(x, 0) 不认 ⇒ 十六进制才走 base 0
                int(text, 16)
                if re.fullmatch(r"-?0[xX][0-9a-fA-F]+", text)
                else int(text, 10)
                if re.fullmatch(r"-?\d+", text)
                else float(text),
            )
        elif kind == "name":
            if text == "true":
                yield ("val", True)
            elif text == "false":
                yield ("val", False)
            elif text == "nil":
                yield ("val", None)
            elif text == "math.huge":
                yield ("val", float("inf"))
            else:
                yield ("name", text)
        else:
            yield ("p", text)


def loads(src: str):
    toks = list(_tokens(src))
    i = 0
    if toks and toks[0] == ("name", "return"):
        i = 1

    def value(i):
        kind, v = toks[i]
        if kind == "val":
            return v, i + 1
        if (kind, v) == ("p", "{"):
            return table(i + 1)
        raise ValueError(f"意外的记号 {toks[i]!r} @tok{i}")

    def table(i):
        d, arr = {}, []
        while True:
            if toks[i] == ("p", "}"):
                i += 1
                break
            if toks[i] == ("p", "["):
                k, i = value(i + 1)
                assert toks[i] == ("p", "]") and toks[i + 1] == ("p", "="), toks[i : i + 2]
                v, i = value(i + 2)
                d[k] = v
            elif toks[i][0] == "name" and toks[i + 1] == ("p", "="):
                k = toks[i][1]
                v, i = value(i + 2)
                d[k] = v
            else:
                v, i = value(i)
                arr.append(v)
            if toks[i] in (("p", ","), ("p", ";")):
                i += 1
        if not d:
            return arr, i
        for n, v in enumerate(arr, 1):
            d[n] = v
        return d, i

    v, i = value(i)
    if i != len(toks):
        raise ValueError(f"末尾多余记号 @tok{i}: {toks[i : i + 5]!r}")
    return v
