"""通达信本地数据只读解析（F:\\tdx\\vipdoc）

仅用于读取：
  - vipdoc/{sh,sz,bj}/lday/{market}{code}.day   日线（32 字节/条）
  - vipdoc/{sh,sz,bj}/minline/{market}{code}.lc1/.lc5  1/5 分钟线（32 字节/条）

铁律：
  - 只读（open "rb"），绝不写入通达信目录；
  - 文件缺失/为空/损坏 → 抛出 TdxError（中文信息），由上层降级；
  - None = 无数据；比率由上层统一处理为小数。
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Iterator, Optional

from .. import config


class TdxError(Exception):
    """通达信本地数据读取错误（中文信息）。"""


_DAY_RECORD = struct.Struct("<IIIIIfII")   # date,o,h,l,c(×100),amount,volume,reserved = 32B
_MIN_RECORD = struct.Struct("<HHfffffII")  # date,time,o,h,l,c,amount,volume,reserved = 32B
_RECORD_SIZE = 32

# 沪市常见指数代码（纯代码无法区分股票/指数时，按通达信习惯归入 sh/lday）
_SH_INDEX_CODES = {
    "000001", "000002", "000003", "000009", "000010", "000011", "000012",
    "000015", "000016", "000017", "000018", "000019", "000020", "000300",
    "000688", "000852", "000905", "000906",
}


def resolve_market(code: str) -> tuple[str, str]:
    """解析市场与 6 位代码。

    支持 "sh000001"/"sz399001" 前缀写法；纯代码按规则推断
    （6/5/9 开头 → 沪；0/2/3 开头 → 深；4/8/92 开头 → 北证；
    000001/000300/000688 等指数代码 → 沪）。个股 000001（平安银行）请显式传 "sz000001"。
    """
    raw = str(code).strip().lower()
    if raw.startswith(("sh", "sz", "bj")):
        market, num = raw[:2], raw[2:]
    else:
        num = raw
        if num in _SH_INDEX_CODES or num.startswith(("5", "6", "9")):
            market = "sh"
        elif num.startswith(("0", "2", "3")):
            market = "sz"
        elif num.startswith(("4", "8", "92")):
            market = "bj"
        else:
            raise TdxError(f"无法识别代码 {code} 所属市场")
    if len(num) != 6 or not num.isdigit():
        raise TdxError(f"无效股票代码：{code}")
    return market, num


def vipdoc_path(tdx_path: Optional[str] = None) -> Path:
    """通达信 vipdoc 根目录；未配置 TDX_PATH 时给出中文错误。"""
    root = tdx_path or config.TDX_PATH
    if not root:
        raise TdxError("未配置通达信路径（.env 中 TDX_PATH），通达信本地数据源不可用")
    path = Path(root) / "vipdoc"
    if not path.is_dir():
        raise TdxError(f"通达信 vipdoc 目录不存在：{path}")
    return path


def _file_path(kind: str, code: str, tdx_path: Optional[str], period: Optional[int] = None) -> Path:
    market, num = resolve_market(code)
    vipdoc = vipdoc_path(tdx_path)
    if kind == "lday":
        return vipdoc / market / "lday" / f"{market}{num}.day"
    if kind == "minline":
        if period not in (1, 5):
            raise TdxError(f"不支持的分钟周期：{period}（仅支持 1/5 分钟）")
        return vipdoc / market / "minline" / f"{market}{num}.lc{period}"
    raise TdxError(f"未知的文件类型：{kind}")


def day_file_path(code: str, tdx_path: Optional[str] = None) -> Path:
    return _file_path("lday", code, tdx_path)


def minline_file_path(code: str, period: int = 1, tdx_path: Optional[str] = None) -> Path:
    return _file_path("minline", code, tdx_path, period)


def _read_bytes(path: Path) -> bytes:
    if not path.exists():
        raise TdxError(f"通达信文件不存在：{path}")
    try:
        with open(path, "rb") as f:  # 只读，绝不写回通达信目录
            data = f.read()
    except OSError as exc:
        raise TdxError(f"通达信文件读取失败：{path}（{exc}）") from exc
    if not data:
        raise TdxError(f"通达信文件为空：{path}")
    if len(data) % _RECORD_SIZE != 0:
        raise TdxError(f"通达信文件损坏：{path}（大小 {len(data)} 字节，不是 {_RECORD_SIZE} 字节记录的整数倍）")
    return data


def _check_date_plausible(yyyymmdd: int, path: Path) -> None:
    if not (19900101 <= yyyymmdd <= 21001231):
        raise TdxError(f"通达信文件损坏：{path}（记录日期非法 {yyyymmdd}）")


def read_day(code: str, tdx_path: Optional[str] = None, max_records: Optional[int] = None) -> dict:
    """读取通达信日线（lday）。

    返回：{"code", "market", "path", "volume_unit": "手", "record_count",
           "date_start", "date_end", "records": [{date, open, high, low, close, amount, volume}, ...]}
    amount 单位：元；volume 单位：手；价格单位：元。
    """
    path = day_file_path(code, tdx_path)
    data = _read_bytes(path)
    count = len(data) // _RECORD_SIZE
    records: list[dict] = []
    for i in range(count):
        raw = _DAY_RECORD.unpack_from(data, i * _RECORD_SIZE)
        yyyymmdd = int(raw[0])
        _check_date_plausible(yyyymmdd, path)
        records.append({
            "date": f"{yyyymmdd // 10000:04d}-{yyyymmdd % 10000 // 100:02d}-{yyyymmdd % 100:02d}",
            "open": round(raw[1] / 100.0, 4),
            "high": round(raw[2] / 100.0, 4),
            "low": round(raw[3] / 100.0, 4),
            "close": round(raw[4] / 100.0, 4),
            "amount": float(raw[5]),
            "volume": int(raw[6]),
        })
    if max_records is not None and max_records > 0:
        records = records[-max_records:]
    return {
        "code": code,
        "market": resolve_market(code)[0],
        "path": str(path),
        "volume_unit": "手",
        "record_count": count,
        "date_start": records[0]["date"] if records else None,
        "date_end": records[-1]["date"] if records else None,
        "records": records,
    }


def _decode_min_datetime(dt: int, tm: int) -> tuple[str, str]:
    # 通达信 lc1/lc5 日期编码：(year-2004)*2048 + month*100 + day（非位域，须除/模解码）
    year = (dt // 2048) + 2004
    base = dt % 2048
    month = base // 100
    day = base % 100
    if not (1990 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        raise TdxError(f"通达信分钟线记录日期非法：date={dt}")
    date_s = f"{year:04d}-{month:02d}-{day:02d}"
    time_s = f"{tm // 60:02d}:{tm % 60:02d}"
    return date_s, time_s


def read_minline(code: str, period: int = 1, tdx_path: Optional[str] = None) -> dict:
    """读取通达信分钟线（minline/.lc1/.lc5）。

    返回：{"code", "market", "path", "period", "volume_unit": "手", "record_count",
           "records": [{date, time, datetime, open, high, low, close, amount, volume}, ...]}
    """
    path = minline_file_path(code, period, tdx_path)
    data = _read_bytes(path)
    count = len(data) // _RECORD_SIZE
    records: list[dict] = []
    for i in range(count):
        raw = _MIN_RECORD.unpack_from(data, i * _RECORD_SIZE)
        date_s, time_s = _decode_min_datetime(int(raw[0]), int(raw[1]))
        records.append({
            "date": date_s,
            "time": time_s,
            "datetime": f"{date_s} {time_s}",
            "open": round(float(raw[2]), 4),
            "high": round(float(raw[3]), 4),
            "low": round(float(raw[4]), 4),
            "close": round(float(raw[5]), 4),
            "amount": float(raw[6]),
            "volume": int(raw[7]),
        })
    return {
        "code": code,
        "market": resolve_market(code)[0],
        "path": str(path),
        "period": period,
        "volume_unit": "手",
        "record_count": count,
        "records": records,
    }


def iter_day_codes(tdx_path: Optional[str] = None, markets: tuple[str, ...] = ("sh", "sz")) -> Iterator[tuple[str, str, Path]]:
    """遍历通达信日线文件，产出 (market, code6, path)。默认排除北证。"""
    vipdoc = vipdoc_path(tdx_path)
    for market in markets:
        lday = vipdoc / market / "lday"
        if not lday.is_dir():
            continue
        try:
            entries = sorted(os.scandir(lday), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file():
                continue
            name = entry.name.lower()
            if not name.endswith(".day") or len(name) != 12:
                continue
            code6 = name[2:8]
            if len(code6) == 6 and code6.isdigit():
                yield market, code6, Path(entry.path)


def read_day_tail(code: str, n: int = 3, tdx_path: Optional[str] = None) -> list[dict]:
    """读取最近 n 条日线记录（用于情绪计算等轻量场景）。"""
    return read_day(code, tdx_path=tdx_path, max_records=n)["records"]
