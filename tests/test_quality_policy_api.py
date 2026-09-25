"""质量策略版本与只读对比的接口测试。"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.database import engine, SessionLocal
from app.models import QualityPolicy
from main import app

API = "/api/v1"

PAST = "2026-01-01T00:00:00+00:00"
FUTURE = "2099-01-01T00:00:00+00:00"

V1_PAYLOAD = {
    "name": "现行口径",
    "weights": {"completeness": 0.5, "annotation": 0.5},
    "thresholds": {"grade_a": 0.9, "grade_b": 0.7, "grade_c": 0.5},
    "effective_at": PAST,
    "created_by": "质量团队",
}

V2_PAYLOAD = {
    "name": "收紧口径",
    "weights": {"completeness": 0.5, "annotation": 0.5},
    "thresholds": {"grade_a": 0.95, "grade_b": 0.8, "grade_c": 0.6},
    "effective_at": PAST,
    "created_by": "质量团队",
}


# ---------------------------------------------------------------------------
# 辅助构造
# ---------------------------------------------------------------------------

def _make_resources(client: TestClient) -> tuple[int, int, int]:
    rm = client.post(f"{API}/robot-models", json={
        "name": "RM-TEST", "manufacturer": "ACME"
    }).json()["id"]
    scene = client.post(f"{API}/scenes", json={
        "name": "测试场景", "category": "测试"
    }).json()["id"]
    skill = client.post(f"{API}/skills", json={
        "name": "测试技能", "category": "测试"
    }).json()["id"]
    return rm, scene, skill


def _make_operation(client: TestClient, rm: int, scene: int, skill: int, *, full: bool = True) -> int:
    if full:
        payload = {
            "motion_trajectory": {"waypoints": [{"x": 1}], "joint_angles": [[0.1]]},
            "perception_records": {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5},
            "grasp_result": {"success": True},
            "environment_conditions": {"light": 1},
            "hardware_status": {"cpu": 10},
            "duration_ms": 1000,
        }
    else:
        payload = {
            "motion_trajectory": {"waypoints": [{"x": 1}], "joint_angles": [[0.1]]},
            "perception_records": {"a": 1, "b": 2},
            "grasp_result": {"success": True},
            "environment_conditions": {"light": 1},
            "hardware_status": {"cpu": 10},
            "duration_ms": 1000,
        }
    body = {
        "robot_model_id": rm,
        "scene_id": scene,
        "skill_id": skill,
        "timestamp_start": "2026-03-01T00:00:00+00:00",
        "timestamp_end": "2026-03-01T00:00:01+00:00",
        **payload,
    }
    return client.post(f"{API}/operations", json=body).json()["id"]


def _annotate(client: TestClient, op_id: int, quality_score: float) -> None:
    client.post(f"{API}/annotations", json={
        "operation_data_id": op_id,
        "is_success": True,
        "annotator": "tester",
    })
    ann_id = client.get(f"{API}/operations/{op_id}/annotation").json()["id"]
    client.put(f"{API}/annotations/{ann_id}", json={
        "review_status": "approved",
        "annotation_quality_score": quality_score,
    })


def _create_policy(client: TestClient, payload: dict, scope: dict | None = None):
    body = dict(payload)
    if scope is not None:
        body["scope"] = scope
    resp = client.post(f"{API}/quality-policies", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 策略生命周期
# ---------------------------------------------------------------------------

def test_policy_crud_and_draft_immutable_after_activation(client: TestClient):
    p1 = _create_policy(client, V1_PAYLOAD)
    assert p1["status"] == "draft"
    assert p1["version"] == 1
    assert p1["content_hash"]

    # 权重和不为1 → 400
    bad = client.post(f"{API}/quality-policies", json={
        **V1_PAYLOAD,
        "weights": {"completeness": 0.4, "annotation": 0.4},
    })
    assert bad.status_code == 400

    # 阈值非递减 → 400
    bad = client.post(f"{API}/quality-policies", json={
        **V1_PAYLOAD,
        "thresholds": {"grade_a": 0.5, "grade_b": 0.7, "grade_c": 0.5},
    })
    assert bad.status_code == 400

    # 激活
    resp = client.post(f"{API}/quality-policies/{p1['id']}/activate", json={"actor": "a"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"

    # 已激活内容不可改
    resp = client.put(f"{API}/quality-policies/{p1['id']}", json={
        "name": "x",
        "weights": {"completeness": 0.3, "annotation": 0.7},
        "thresholds": V1_PAYLOAD["thresholds"],
        "effective_at": PAST,
    })
    assert resp.status_code == 400


def test_future_effective_at_cannot_activate(client: TestClient):
    p = _create_policy(client, {**V1_PAYLOAD, "effective_at": FUTURE})
    resp = client.post(f"{API}/quality-policies/{p['id']}/activate", json={})
    assert resp.status_code == 400
    assert "生效时间" in resp.json()["detail"]
    # 失败时仍为草稿
    assert client.get(f"{API}/quality-policies/{p['id']}").json()["status"] == "draft"


# ---------------------------------------------------------------------------
# 草稿比较
# ---------------------------------------------------------------------------

def test_draft_comparison_migration_boundary_and_dataset_summary(client: TestClient):
    rm, scene, skill = _make_resources(client)
    # A: 完整度1.0 + 标注0.8 → 质量0.9（v1=A, v2=B），贴 v1 的 A 阈值
    op_a = _make_operation(client, rm, scene, skill, full=True)
    _annotate(client, op_a, 0.8)
    # B: 完整度0.9 + 标注0.3 → 质量0.6（两版本均为C，贴 v2 的 C 阈值）
    op_b = _make_operation(client, rm, scene, skill, full=False)
    _annotate(client, op_b, 0.3)
    # C: 完整度1.0 + 标注0.6 → 质量0.8（两版本均为B，贴 v2 的 B 阈值）
    op_c = _make_operation(client, rm, scene, skill, full=True)
    _annotate(client, op_c, 0.6)

    dataset = client.post(f"{API}/datasets", json={
        "name": "对比用数据集",
        "robot_model_id": rm,
        "scene_id": scene,
        "skill_id": skill,
        "owner_team": "t",
        "operation_data_ids": [op_a, op_b, op_c],
    }).json()
    dataset_id = dataset["id"]

    p1 = _create_policy(client, V1_PAYLOAD)
    p2 = _create_policy(client, V2_PAYLOAD)
    # 两个草稿即可比较，无需激活
    assert p1["status"] == "draft" and p2["status"] == "draft"

    resp = client.post(f"{API}/quality-comparisons", json={
        "left_policy_id": p1["id"],
        "right_policy_id": p2["id"],
        "dataset_id": dataset_id,
        "boundary_tolerance": 0.02,
        "created_by": "analyst",
    })
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["sample_count"] == 3
    assert report["changed_count"] == 1
    assert report["boundary_count"] >= 2

    result = report["result"]
    # 迁移矩阵：A->B 与 C->C（op_b 质量0.6 在 v2 恰为 C），B->B
    assert result["migration_matrix"]["A"]["B"] == 1
    assert result["migration_matrix"]["C"]["C"] == 1
    assert result["migration_matrix"]["B"]["B"] == 1
    transitions = {row["operation_id"]: row["transition"] for row in result["per_sample"]}
    assert transitions[op_a] == "A->B"
    assert transitions[op_b] == "C->C"
    assert transitions[op_c] == "B->B"

    # 数据集汇总差异
    summaries = result["dataset_summaries"]
    assert len(summaries) == 1
    assert summaries[0]["dataset_id"] == dataset_id
    assert summaries[0]["left"]["dominant_grade"] != summaries[0]["right"]["dominant_grade"] or summaries[0]["changed_count"] == 1

    # 边界样本里包含贴阈值的 op
    boundary_ids = {row["operation_id"] for row in result["boundary_samples"]}
    assert op_a in boundary_ids and op_c in boundary_ids

    # 关键约束：比较是只读的，作业当前等级未被改写
    for op_id in (op_a, op_b, op_c):
        quality = client.get(f"{API}/quality/operation/{op_id}").json()
        assert quality["data_grade"] is None


def test_comparison_with_empty_samples(client: TestClient):
    rm, scene, skill = _make_resources(client)
    p1 = _create_policy(client, V1_PAYLOAD)
    p2 = _create_policy(client, V2_PAYLOAD)

    # 显式空列表
    resp = client.post(f"{API}/quality-comparisons", json={
        "left_policy_id": p1["id"],
        "right_policy_id": p2["id"],
        "operation_ids": [],
    })
    assert resp.status_code == 200
    report = resp.json()
    assert report["sample_count"] == 0
    assert report["changed_count"] == 0
    assert report["boundary_count"] == 0
    assert report["result"]["migration_matrix"] == {
        left: {right: 0 for right in ("A", "B", "C", "D")}
        for left in ("A", "B", "C", "D")
    }
    assert report["result"]["totals"]["left_average_quality_score"] is None
    assert report["result"]["boundary_samples"] == []
    assert report["result"]["dataset_summaries"] == []
    assert report["snapshot_hash"]

    # 过滤条件无匹配
    resp = client.post(f"{API}/quality-comparisons", json={
        "left_policy_id": p1["id"],
        "right_policy_id": p2["id"],
        "robot_model_id": rm,
    })
    assert resp.status_code == 200
    assert resp.json()["sample_count"] == 0


def test_comparison_rejects_invalid_inputs(client: TestClient):
    p1 = _create_policy(client, V1_PAYLOAD)
    # 同一版本
    resp = client.post(f"{API}/quality-comparisons", json={
        "left_policy_id": p1["id"], "right_policy_id": p1["id"], "operation_ids": [],
    })
    assert resp.status_code == 400
    # 版本不存在
    resp = client.post(f"{API}/quality-comparisons", json={
        "left_policy_id": p1["id"], "right_policy_id": 9999, "operation_ids": [],
    })
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 版本冲突 / 乐观锁
# ---------------------------------------------------------------------------

def test_draft_optimistic_lock_conflict(client: TestClient):
    p1 = _create_policy(client, V1_PAYLOAD)
    stale_hash = p1["content_hash"]

    # 第一次修改成功
    ok = client.put(f"{API}/quality-policies/{p1['id']}", json={
        "name": "现行口径-修订",
        "weights": {"completeness": 0.6, "annotation": 0.4},
        "thresholds": V1_PAYLOAD["thresholds"],
        "effective_at": PAST,
        "expected_content_hash": stale_hash,
    })
    assert ok.status_code == 200
    assert ok.json()["weights"] == {"completeness": 0.6, "annotation": 0.4}

    # 用旧哈希再改 → 409
    conflict = client.put(f"{API}/quality-policies/{p1['id']}", json={
        "name": "现行口径-二次修订",
        "weights": {"completeness": 0.7, "annotation": 0.3},
        "thresholds": V1_PAYLOAD["thresholds"],
        "effective_at": PAST,
        "expected_content_hash": stale_hash,
    })
    assert conflict.status_code == 409


# ---------------------------------------------------------------------------
# 激活失败：范围重叠 + 审计
# ---------------------------------------------------------------------------

def test_activation_overlap_failures_are_audited(client: TestClient):
    rm, scene, skill = _make_resources(client)
    scene2 = client.post(f"{API}/scenes", json={
        "name": "另一场景", "category": "测试"
    }).json()["id"]

    # 场景1 专用策略先激活
    scoped_v1 = _create_policy(client, V1_PAYLOAD, scope={"scene_id": scene})
    resp = client.post(f"{API}/quality-policies/{scoped_v1['id']}/activate", json={"actor": "a"})
    assert resp.status_code == 200

    # 全局策略与已激活的场景策略重叠，未带 force_replace → 409
    global_v2 = _create_policy(client, V2_PAYLOAD)
    resp = client.post(f"{API}/quality-policies/{global_v2['id']}/activate", json={"actor": "a"})
    assert resp.status_code == 409
    assert client.get(f"{API}/quality-policies/{global_v2['id']}").json()["status"] == "draft"

    # 失败也留审计
    logs = client.get(f"{API}/quality-policies/{global_v2['id']}/audit-logs").json()
    assert any(row["action"] == "activation_failed" for row in logs)

    # 全局范围完全覆盖场景策略：force_replace 允许，旧场景策略被取代
    resp = client.post(f"{API}/quality-policies/{global_v2['id']}/activate", json={
        "actor": "a", "force_replace": True,
    })
    assert resp.status_code == 200
    old_scoped = client.get(f"{API}/quality-policies/{scoped_v1['id']}").json()
    assert old_scoped["status"] == "inactive"
    assert old_scoped["superseded_by"] == global_v2["id"]

    # 部分重叠：新策略只覆盖"场景1+机型1"，无法完全覆盖现存全局策略 → 即便 force 也拒绝
    partial = _create_policy(
        client, {**V2_PAYLOAD, "name": "部分重叠"},
        scope={"scene_id": scene, "robot_model_id": rm},
    )
    resp = client.post(f"{API}/quality-policies/{partial['id']}/activate", json={
        "actor": "a", "force_replace": True,
    })
    assert resp.status_code == 409
    logs = client.get(f"{API}/quality-policies/{partial['id']}/audit-logs").json()
    assert any(row["action"] == "activation_failed" for row in logs)
    assert client.get(f"{API}/quality-policies/{partial['id']}").json()["status"] == "draft"

    # 引用不存在的机型 → 400
    resp = client.post(f"{API}/quality-policies", json={
        **V1_PAYLOAD, "scope": {"robot_model_id": 999999},
    })
    assert resp.status_code == 400

    # 当前仅有全局一个 active
    actives = client.get(f"{API}/quality-policies", params={"status": "active"}).json()
    assert len(actives) == 1 and actives[0]["id"] == global_v2["id"]


def test_disjoint_scopes_coexist_and_resolve_specificity(client: TestClient):
    rm, scene, skill = _make_resources(client)
    scene2 = client.post(f"{API}/scenes", json={
        "name": "另一场景", "category": "测试"
    }).json()["id"]

    p_scene1 = _create_policy(client, V1_PAYLOAD, scope={"scene_id": scene})
    p_scene2 = _create_policy(client, V2_PAYLOAD, scope={"scene_id": scene2})
    assert client.post(f"{API}/quality-policies/{p_scene1['id']}/activate", json={}).status_code == 200
    assert client.post(f"{API}/quality-policies/{p_scene2['id']}/activate", json={}).status_code == 200

    # 两个范围互不相交，并发共存
    actives = client.get(f"{API}/quality-policies", params={"status": "active"}).json()
    assert {p["version"] for p in actives} == {p_scene1["version"], p_scene2["version"]}

    # 生效解析按属性命中
    hit1 = client.get(f"{API}/quality-policies/effective", params={"scene_id": scene}).json()
    hit2 = client.get(f"{API}/quality-policies/effective", params={"scene_id": scene2}).json()
    assert hit1["id"] == p_scene1["id"]
    assert hit2["id"] == p_scene2["id"]
    # 未覆盖范围没有生效策略
    assert client.get(f"{API}/quality-policies/effective", params={
        "scene_id": 999999,
    }).status_code == 404


def test_force_replace_supersede_and_rollback_audit(client: TestClient):
    rm, scene, skill = _make_resources(client)
    # 全局 v1 激活
    p1 = _create_policy(client, V1_PAYLOAD)
    client.post(f"{API}/quality-policies/{p1['id']}/activate", json={"actor": "a"})

    # 全局 v2 覆盖式发布
    p2 = _create_policy(client, V2_PAYLOAD)
    resp = client.post(f"{API}/quality-policies/{p2['id']}/activate", json={
        "actor": "a", "force_replace": True,
    })
    assert resp.status_code == 200
    old = client.get(f"{API}/quality-policies/{p1['id']}").json()
    assert old["status"] == "inactive"
    assert old["superseded_by"] == p2["id"]
    assert any(row["action"] == "superseded" for row in
               client.get(f"{API}/quality-policies/{p1['id']}/audit-logs").json())

    # 回滚到 v1：重新激活，v2 被取代，回滚动作有审计
    resp = client.post(f"{API}/quality-policies/{p1['id']}/rollback", json={
        "actor": "a", "force_replace": True,
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
    assert resp.json()["superseded_by"] is None
    v2 = client.get(f"{API}/quality-policies/{p2['id']}").json()
    assert v2["status"] == "inactive"
    assert v2["superseded_by"] == p1["id"]
    logs = client.get(f"{API}/quality-policies/{p1['id']}/audit-logs").json()
    actions = [row["action"] for row in logs]
    assert "activated" in actions and "superseded" in actions and "rollback" in actions


def test_concurrent_activation_only_one_wins(client: TestClient):
    """同 scope 并发激活：进程锁串行化后后到者收到 409 并留失败审计。"""
    import threading

    p1 = _create_policy(client, V1_PAYLOAD)
    p2 = _create_policy(client, V2_PAYLOAD)
    results = {}

    def activate(policy_id, key):
        worker = TestClient(app)
        resp = worker.post(f"{API}/quality-policies/{policy_id}/activate", json={"actor": "t"})
        results[key] = resp.status_code
        worker.close()

    t1 = threading.Thread(target=activate, args=(p1["id"], "p1"))
    t2 = threading.Thread(target=activate, args=(p2["id"], "p2"))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert sorted(results.values()) == [200, 409]
    db = SessionLocal()
    try:
        assert db.query(QualityPolicy).filter(QualityPolicy.status == "active").count() == 1
        assert db.query(QualityPolicy).filter(
            QualityPolicy.status == "draft"
        ).count() == 1
    finally:
        db.close()


def test_database_partial_unique_index_blocks_duplicate_active_scope(client: TestClient):
    """数据库层兜底：同一 scope_key 不允许两条 active 记录。"""
    p1 = _create_policy(client, V1_PAYLOAD)
    client.post(f"{API}/quality-policies/{p1['id']}/activate", json={})
    db = SessionLocal()
    try:
        duplicate = QualityPolicy(
            version=999, name="dup", status="active",
            weights={"completeness": 0.5, "annotation": 0.5},
            thresholds=V1_PAYLOAD["thresholds"],
            scope={}, scope_key="",
            effective_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            content_hash="x",
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 快照固化：重启重取 + 新增数据不影响报告
# ---------------------------------------------------------------------------

def test_report_survives_restart_and_ignores_new_data(client: TestClient):
    rm, scene, skill = _make_resources(client)
    op = _make_operation(client, rm, scene, skill, full=True)
    _annotate(client, op, 0.9)

    p1 = _create_policy(client, V1_PAYLOAD)
    p2 = _create_policy(client, V2_PAYLOAD)
    created = client.post(f"{API}/quality-comparisons", json={
        "left_policy_id": p1["id"],
        "right_policy_id": p2["id"],
        "operation_ids": [op],
        "boundary_tolerance": 0.05,
    }).json()
    report_id = created["id"]
    original_hash = created["snapshot_hash"]
    original_result = created["result"]

    # 模拟进程重启：释放全部连接后使用全新客户端读取
    engine.dispose()
    restarted = TestClient(app)
    fetched = restarted.get(f"{API}/quality-comparisons/{report_id}")
    assert fetched.status_code == 200
    again = fetched.json()
    assert again["snapshot_hash"] == original_hash
    assert again["sample_count"] == 1
    assert again["result"]["migration_matrix"] == original_result["migration_matrix"]
    assert again["input_snapshot"]["samples"][0]["operation_id"] == op
    assert again["input_snapshot"]["policies"]["left"]["content_hash"] == p1["content_hash"]

    # 重启后新增作业，已保存报告的样本集合与内容不变
    new_op = _make_operation(restarted, rm, scene, skill, full=False)
    _annotate(restarted, new_op, 0.2)
    refetched = restarted.get(f"{API}/quality-comparisons/{report_id}").json()
    assert refetched["sample_count"] == 1
    assert refetched["snapshot_hash"] == original_hash

    # 修改旧作业的标注，已固化报告仍保持原值
    ann_id = restarted.get(f"{API}/operations/{op}/annotation").json()["id"]
    restarted.put(f"{API}/annotations/{ann_id}", json={"annotation_quality_score": 0.1})
    refetched = restarted.get(f"{API}/quality-comparisons/{report_id}").json()
    assert refetched["snapshot_hash"] == original_hash
    assert refetched["result"] == original_result
    restarted.close()


def test_report_not_found_and_listing(client: TestClient):
    assert client.get(f"{API}/quality-comparisons/4242").status_code == 404
    p1 = _create_policy(client, V1_PAYLOAD)
    p2 = _create_policy(client, V2_PAYLOAD)
    client.post(f"{API}/quality-comparisons", json={
        "left_policy_id": p1["id"], "right_policy_id": p2["id"], "operation_ids": [],
    })
    listing = client.get(f"{API}/quality-comparisons").json()
    assert len(listing) == 1
    assert listing[0]["sample_count"] == 0
