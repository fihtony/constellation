"""Import-compatible package namespace for log-store modules.

Keep package import side effects minimal so containers can import submodules
such as ``agents.log_store.log_aggregator`` without also requiring the full
legacy ``agents/log-store`` tree.
"""

__all__: list[str] = []
