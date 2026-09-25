"""接口测试夹具：每个用例使用独立的临时 SQLite 文件，便于模拟进程重启。"""

import os
import tempfile

import pytest

# 必须在导入应用模块之前指定数据库路径。
_DB_FD, _DB_PATH = tempfile.mkstemp(prefix="policy_test_", suffix=".db")
os.close(_DB_FD)
os.unlink(_DB_PATH)  # 让 create_all 自行建文件
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from main import app  # noqa: E402


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def db_path():
    return _DB_PATH
