from __future__ import annotations


def default_engine_adapters():
    # Imports stay local so adding an optional engine never changes common modules.
    from hgalgame.engines.advhd import AdvHdAdapter

    return (AdvHdAdapter(),)


__all__ = ["default_engine_adapters"]
