import json
import sys
from pathlib import Path

first = json.loads(Path(sys.argv[1]).read_text())
replay = json.loads(Path(sys.argv[2]).read_text())
required = ['analysis_id', 'matched_products', 'scores', 'calibration', 'revision_gains', 'sample_support']
missing = [field for field in required if field not in first]
if missing:
    raise SystemExit("缺少分析字段：" + ", ".join(missing))
if not first['analysis_id'] or first['analysis_id'] != replay.get('analysis_id'):
    raise SystemExit("同一输入重放未引用同一分析记录")
print("分析输出结构和重放标识核验完成")
