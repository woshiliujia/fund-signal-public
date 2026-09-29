#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基金信号 CLI(与后台共用核心逻辑)
用法:
  python3 fund_signal.py            # 正常运行并推送
  python3 fund_signal.py --dry      # 只打印结果，不推送
  python3 fund_signal.py --test     # 非交易时间也强制推送
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fund_core


def main():
    dry = "--dry" in sys.argv
    force = "--test" in sys.argv
    cfg = fund_core.load_config()
    funds_out, errors, push_results, content = fund_core.run_all(cfg, force=force)
    print(content)
    if dry:
        print("\n[dry-run] 未推送")
        return 0
    if not funds_out:
        print("无任何基金数据，跳过推送")
        return 1
    for ch, r in push_results:
        print("\n[%s] 推送结果: %s" % (ch, r))
    if not push_results:
        print("\n今日非交易时段，跳过推送。可用 --test 强制推送。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
