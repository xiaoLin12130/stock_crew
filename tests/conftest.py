# 退出挂起修复：chroma/langfuse 的 posthog 遥测线程会在解释器退出时被
# atexit join 阻塞（最长约 2 分钟）。在导入任何库之前全局禁用。
try:
    import posthog

    posthog.disabled = True
except Exception:  # pragma: no cover - 环境无 posthog 时无需处理
    pass

# 禁用 py_mini_racer（Windows IOCP 退出死锁），必须在导入 akshare 之前生效
try:
    import py_mini_racer

    class _MiniRacerDisabled:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("py_mini_racer 已禁用（Windows IOCP 退出死锁）")

    py_mini_racer.MiniRacer = _MiniRacerDisabled
except Exception:  # pragma: no cover
    pass
