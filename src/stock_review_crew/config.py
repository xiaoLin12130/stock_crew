"""配置层：环境变量 + LLM 初始化（凭据一律只读 .env，无硬编码 token）"""
import os
from pathlib import Path

# 禁用 posthog 遥测（chroma/langfuse 匿名统计）：避免解释器退出时被
# atexit join 阻塞（服务与测试进程长时间不退出）。
try:
    import posthog

    posthog.disabled = True
except Exception:  # pragma: no cover - 环境无 posthog 时无需处理
    pass

# 禁用 py_mini_racer（akshare 空气/债券接口的内嵌 V8 引擎）：其后台 asyncio
# 线程在 Windows 上会与解释器退出形成 IOCP 死锁（构造即可能永久阻塞）。
# 本项目数据链路不需要 JS 引擎；若上游接口触发，将抛明确错误由数据层降级标注。
try:
    import py_mini_racer

    class _MiniRacerDisabled:
        def __init__(self, *args, **kwargs):
            raise RuntimeError(
                "py_mini_racer 已禁用（Windows IOCP 退出死锁）；"
                "相关 akshare 接口不可用，数据层已降级标注"
            )

    py_mini_racer.MiniRacer = _MiniRacerDisabled
except Exception:  # pragma: no cover - 环境无 py_mini_racer 时无需处理
    pass

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI


# 显式定位项目根 .env，并以 utf-8-sig 读取（.env 带 BOM 时普通 utf-8 会污染首个键名）
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"
if _ENV_FILE.exists():
    load_dotenv(dotenv_path=_ENV_FILE, encoding="utf-8-sig", override=False)


# ═══════════════════════════════════════
# 环境变量（数据源凭据只从 .env 读取；缺失时由数据层走降级标注，禁止抛异常）
# ═══════════════════════════════════════
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN")       # 缺失 → Tushare 相关数据源降级标注
KAIPANLA_COOKIE = os.getenv("KAIPANLA_COOKIE")   # 缺失 → 竞价数据源降级标注
TDX_PATH = os.getenv("TDX_PATH")                 # 缺失 → 通达信本地数据源降级标注

# LLM（保留原配置）
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
# 兼容两种 .env 键名：DEEPSEEK_API_BASE（旧）/ DEEPSEEK_BASE_URL（现用）
DEEPSEEK_BASE_URL = (
    os.getenv("DEEPSEEK_API_BASE")
    or os.getenv("DEEPSEEK_BASE_URL")
    or "https://api.deepseek.com"
)


# ═══════════════════════════════════════
# LLM 实例
# ═══════════════════════════════════════
llm = ChatOpenAI(
    model=DEEPSEEK_MODEL,
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL,
    temperature=0.7,
    timeout=120,
    max_retries=2,
)

# 低温度版本（做判断、总结时用）
llm_strict = ChatOpenAI(
    model=DEEPSEEK_MODEL,
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL,
    temperature=0.1,
    timeout=120,
    max_retries=2,
)
