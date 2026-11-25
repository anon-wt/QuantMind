"""Generate and transform safe ASTs; no Python eval or user imports."""

import hashlib
import random

import pandas as pd

from ..factorminer import dsl
from ..factorminer.evaluator import evaluate

FIELDS = ("$open", "$high", "$low", "$close", "$volume", "$amt")
UNARY = ("Abs", "Neg", "Log")
BINARY = ("Add", "Sub", "Mul", "Div")
ROLLING = ("Mean", "Std", "Delay", "Delta", "Min", "Max")
OPERATORS = UNARY + BINARY + ROLLING + ("Corr",)
MAX_HISTORY = 250


def serialize(node):
    if isinstance(node, dsl.Feature):
        return node.name
    if isinstance(node, dsl.Num):
        return str(int(node.value))
    return f"{node.op}({','.join(serialize(arg) for arg in node.args)})"


def validate(expression: str):
    if not isinstance(expression, str) or len(expression) > 8192:
        raise ValueError("GP 公式缺失或过长")
    node = dsl.parse(expression)
    for item in dsl.walk(node):
        if isinstance(item, dsl.Feature) and item.name not in FIELDS:
            raise ValueError("GP 公式引用了不支持的字段")
        if isinstance(item, dsl.Call) and item.op not in OPERATORS:
            raise ValueError("GP 公式引用了不支持的算子")
        if isinstance(item, dsl.Call) and item.op in (*ROLLING, "Corr"):
            if not 1 <= item.args[-1].value <= 60:
                raise ValueError("GP 公式窗口超过限制")
    cx = dsl.complexity(node)
    if cx.depth > 7 or cx.node_count > 63 or dsl.min_history(node) > MAX_HISTORY:
        raise ValueError("GP 公式复杂度或回看长度超过限制")
    if not any(isinstance(item, dsl.Feature) for item in dsl.walk(node)):
        raise ValueError("GP 公式必须引用行情字段")
    return node


def qlib_expression(node):
    if isinstance(node, dsl.Feature):
        return "$amount" if node.name == "$amt" else node.name
    if isinstance(node, dsl.Num):
        return str(int(node.value))
    args = [qlib_expression(arg) for arg in node.args]
    if node.op in BINARY:
        op = {"Add": "+", "Sub": "-", "Mul": "*", "Div": "/"}[node.op]
        return f"({args[0]}{op}{args[1]})"
    if node.op == "Neg":
        return f"(-{args[0]})"
    name = "Ref" if node.op == "Delay" else node.op
    return f"{name}({','.join(args)})"


def random_tree(rng: random.Random, depth: int, windows: list[int]):
    if depth <= 1 or rng.random() < 0.25:
        return dsl.Feature(rng.choice(FIELDS))
    op = rng.choice(OPERATORS)
    left = random_tree(rng, depth - 1, windows)
    if op in UNARY:
        return dsl.Call(op, (left,))
    if op in ROLLING:
        return dsl.Call(op, (left, dsl.Num(rng.choice(windows))))
    right = random_tree(rng, depth - 1, windows)
    return dsl.Call(
        op,
        (left, right, dsl.Num(rng.choice(windows))) if op == "Corr" else (left, right),
    )


def paths(node, prefix=()):
    """Data-subtree positions only; window literals retain their typed slot."""
    result = [(prefix, node)]
    if isinstance(node, dsl.Call):
        for i, arg in enumerate(node.args):
            if not isinstance(arg, dsl.Num):
                result.extend(paths(arg, (*prefix, i)))
    return result


def replace(node, path, child):
    if not path:
        return child
    args = list(node.args)
    args[path[0]] = replace(args[path[0]], path[1:], child)
    return dsl.Call(node.op, tuple(args))


def candidate(expression: str):
    """Same formula implementation for mining, factor backtest and materialization."""
    from ..statistical.candidates import Candidate

    node = validate(expression)
    canonical = serialize(node)
    key = "gp_" + hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def compute(frame):
        fields = {
            name: frame[["amount" if name == "$amt" else name[1:]]].set_axis(
                ["value"], axis=1
            )
            for name in FIELDS
        }
        return evaluate(node, fields).iloc[:, 0]

    return Candidate(
        key=key,
        name=f"GP 因子 {key[3:11]}",
        family="gp",
        formulation=canonical,
        source="GP 遗传编程",
        expression=qlib_expression(node),
        min_history=dsl.min_history(node),
        compute=compute,
    )


def trusted_candidate(metadata, code):
    expression = metadata.get("gp_expression")
    item = candidate(expression)
    if item.key != metadata.get("candidate_key") or item.expression != code:
        raise ValueError("GP 因子公式与冻结元数据不一致")
    return item


def evaluate_panel(node, panel, frames=None):
    fields = frames or {
        name: panel["amount" if name == "$amt" else name[1:]].unstack("symbol")
        for name in FIELDS
    }
    wide = evaluate(node, fields)
    index = pd.MultiIndex.from_product(
        [wide.index, wide.columns], names=panel.index.names
    )
    values = pd.Series(wide.to_numpy().ravel(), index=index).reindex(panel.index)
    return values.where(
        panel.groupby(level="symbol").cumcount() >= dsl.min_history(node)
    )
