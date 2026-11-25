"""Apply QuantMind's label to the installed RD-Agent workspace, per subprocess."""

import os
import re
import hashlib
import json
from pathlib import Path

from backend.services.engine.alpha_agent.label_contract import horizon, label_spec


def purge_segment_ends(run_env: dict, calendar, prediction_horizon: int) -> dict:
    """Keep each segment's future outcome within that segment's date boundary."""
    import pandas as pd

    days = pd.DatetimeIndex(calendar).sort_values()
    h = horizon(prediction_horizon)
    result = dict(run_env)
    for segment in ("train", "valid", "test"):
        end_key, start_key = f"{segment}_end", f"{segment}_start"
        end = result.get(end_key) or (days[-1] if len(days) else None)
        if end is None:
            raise ValueError("RD-Agent 交易日历为空")
        position = days.searchsorted(pd.Timestamp(end), side="right") - 1 - h
        if position < 0 or (
            result.get(start_key) and days[position] < pd.Timestamp(result[start_key])
        ):
            raise ValueError(f"RD-Agent {segment} 区间不足以生成 {h} 日收益标签")
        result[end_key] = days[position].strftime("%Y-%m-%d")
    return result


def rewrite_label_template(template: str, prediction_horizon: int) -> str:
    expression = label_spec(prediction_horizon)["expression"]
    pattern = (
        r'(?m)^([ \t]*)label:[ \t]*\n[ \t]*- \["[^"]*"\][ \t]*\n[ \t]*- \["LABEL0"\]'
    )
    result, count = re.subn(
        pattern,
        lambda match: (
            f'{match[1]}label:\n{match[1]}    - ["{expression}"]\n'
            f'{match[1]}    - ["LABEL0"]'
        ),
        template,
    )
    if count != 1:
        raise ValueError("RD-Agent 模板的 LABEL0 结构不受支持，已停止以避免标签错配")
    return result


def install_label_contract():
    from rdagent.scenarios.qlib.experiment.workspace import QlibFBWorkspace

    if getattr(QlibFBWorkspace, "_qm_label_contract", False):
        return
    original = QlibFBWorkspace.__init__
    original_execute = QlibFBWorkspace.execute
    # FactorRunner's decorator captures this before importing FactorRDLoop.
    from rdagent.components.runner import CachedRunner

    original_key = CachedRunner.get_cache_key

    def cache_key(self, experiment):
        target = label_spec(int(os.getenv("QM_PREDICTION_HORIZON", "1")))
        context = {key: os.getenv(key, "") for key in (
            "QLIB_PROVIDER_URI", "QM_MINING_WINDOW_START", "QM_MINING_WINDOW_END",
            "QLIB_FACTOR_TRAIN_START", "QLIB_FACTOR_TRAIN_END", "QLIB_FACTOR_VALID_START",
            "QLIB_FACTOR_VALID_END", "QLIB_FACTOR_TEST_START", "QLIB_FACTOR_TEST_END",
        )}
        return hashlib.sha256(
            (original_key(self, experiment) + str(target) + json.dumps(context, sort_keys=True)).encode()
        ).hexdigest()

    CachedRunner.get_cache_key = cache_key

    def execute(self, qlib_config_name="conf.yaml", run_env=None, *args, **kwargs):
        import pandas as pd
        from backend.shared.qlib_paths import resolve_qlib_provider_uri

        calendar_path = Path(resolve_qlib_provider_uri("CN")) / "calendars/day.txt"
        days = pd.to_datetime(calendar_path.read_text().splitlines())
        h = horizon(int(os.getenv("QM_PREDICTION_HORIZON", "1")))
        frozen_env = purge_segment_ends(run_env or {}, days, h)
        return original_execute(self, qlib_config_name, frozen_env, *args, **kwargs)

    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        h = horizon(int(os.getenv("QM_PREDICTION_HORIZON", "1")))
        replacements = {
            name: rewrite_label_template(content, h)
            for name, content in self.file_dict.items()
            if name.endswith((".yaml", ".yml")) and '"LABEL0"' in content
        }
        if not replacements:
            raise ValueError("RD-Agent 工作区缺少受支持的标签模板")
        self.inject_files(**replacements)

    QlibFBWorkspace.__init__ = initialize
    QlibFBWorkspace.execute = execute
    QlibFBWorkspace._qm_label_contract = True
