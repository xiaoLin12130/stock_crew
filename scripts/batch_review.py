"""批量复盘脚本：遍历指定日期区间的所有交易日"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from stock_review_crew.main import run

START = date(2026, 7, 1)
END = date(2026, 7, 17)
MAX_ROUNDS = 2  # 历史复盘用较少轮次，加快速度


def is_trading_day(d: date) -> bool:
    """简单判断：跳过周六周日"""
    return d.weekday() < 5  # 0=Mon, 4=Fri


def main():
    total = 0
    success = 0
    failed_dates = []

    current = START
    while current <= END:
        if not is_trading_day(current):
            current += timedelta(days=1)
            continue

        date_str = current.strftime("%Y-%m-%d")
        total += 1
        print(f"\n{'='*60}")
        print(f"[{total}] 开始复盘 {date_str}")
        print(f"{'='*60}")

        try:
            result = run(date_str, max_rounds=MAX_ROUNDS, verbose=False)
            success += 1
            print(f"✅ {date_str} 完成 | 轮次:{result['round_count']} | 报告:{len(result['final_report'])}字")
        except Exception as e:
            failed_dates.append(date_str)
            print(f"❌ {date_str} 失败: {e}")

        current += timedelta(days=1)

    print(f"\n{'='*60}")
    print(f"批量复盘完成: {success}/{total} 成功")
    if failed_dates:
        print(f"失败日期: {', '.join(failed_dates)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
