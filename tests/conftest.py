import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 保证测试进程在缺少 .env 时也能通过 Settings 的 APP_SECRET_KEY 必填校验。
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-sqlpulse")
