"""A 股本地读取源选择。同步目录独立，所有消费者共用同一优先级。"""

from backend.shared.data_source_config import get_enabled_sources

SOURCE_PRIORITY = ("quantdb", "datayes", "doris")


def selected_source() -> str:
    enabled = get_enabled_sources("A")
    return next(
        (name for name in SOURCE_PRIORITY if enabled.get(name, name == "quantdb")),
        "quantdb",
    )


def read_data_root():
    source = selected_source()
    if source == "doris":
        from .doris_source import data_root
    elif source == "datayes":
        from .datayes_sync import data_root
    else:
        from .quantdb_hub import _resolve_data_dir as data_root
    return data_root()
