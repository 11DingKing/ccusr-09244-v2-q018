import os
import tempfile

# 在导入 main 之前把默认数据库指向临时目录，避免测试在仓库根目录生成 robot_data.db
_TMP_DIR = tempfile.mkdtemp(prefix="robot_data_test_")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP_DIR}/conftest_default.db")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import get_db
from app.models import Base
from main import app


@pytest.fixture()
def client_factory(tmp_path):
    """返回一个可重复调用的 TestClient 工厂。

    每次调用都在同一个 SQLite 文件上新建引擎与会话工厂，
    模拟「重启进程后重连同一数据库」的场景。
    """
    db_file = tmp_path / "test.db"
    engines = []

    def factory():
        engine = create_engine(
            f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(engine)
        engines.append(engine)
        testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db():
            db = testing_session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        return TestClient(app)

    yield factory

    app.dependency_overrides.clear()
    for engine in engines:
        engine.dispose()


@pytest.fixture()
def client(client_factory):
    return client_factory()
