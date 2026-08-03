"""重跑 7/1-7/3 复盘"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from stock_review_crew.main import run

for d in ["2026-07-01", "2026-07-02", "2026-07-03"]:
    print(f"\n{'='*60}")
    print(f"复盘 {d}")
    print(f"{'='*60}")
    result = run(d, max_rounds=2, verbose=True)
    print(f"\n✅ {d} 完成: {len(result['final_report'])}字, {len(result['analyses'])}位分析师")
