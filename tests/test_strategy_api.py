"""评分策略版本与只读对比的接口测试。

覆盖：草稿比较、空样本、版本冲突、激活失败、重叠范围与并发激活、
回滚审计、报告快照绑定与重启后重取。
"""

from concurrent.futures import ThreadPoolExecutor

import pytest

API = "/api/v1"


# ---------------------------------------------------------------------------
# 测试数据构造
# ---------------------------------------------------------------------------

def create_base_resources(client):
    robot_model = client.post(
        f"{API}/robot-models", json={"name": "R-100", "manufacturer": "ACME"}
    ).json()
    scene = client.post(
        f"{API}/scenes", json={"name": "装配线", "category": "生产制造"}
    ).json()
    skill = client.post(
        f"{API}/skills", json={"name": "抓取", "category": "操作"}
    ).json()
    return robot_model["id"], scene["id"], skill["id"]


def make_operation(client, rm_id, scene_id, skill_id, **overrides):
    payload = {
        "robot_model_id": rm_id,
        "scene_id": scene_id,
        "skill_id": skill_id,
        "motion_trajectory": {"waypoints": [[0, 0, 0]], "joint_angles": [[0.1, 0.2]]},
        "perception_records": {f"sensor_{i}": i for i in range(5)},
        "grasp_result": {"success": True},
        "environment_conditions": {"temperature": 25},
        "hardware_status": {"ok": True},
        "duration_ms": 1200,
        "timestamp_start": "2026-09-01T08:00:00Z",
        "timestamp_end": "2026-09-01T08:01:00Z",
    }
    payload.update(overrides)
    resp = client.post(f"{API}/operations", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def annotate(client, operation_id, quality_score):
    resp = client.post(
        f"{API}/annotations",
        json={"operation_data_id": operation_id, "is_success": True},
    )
    assert resp.status_code == 200, resp.text
    annotation_id = resp.json()["id"]
    resp = client.put(
        f"{API}/annotations/{annotation_id}",
        json={"annotation_quality_score": quality_score, "review_status": "approved"},
    )
    assert resp.status_code == 200, resp.text


def create_strategy(client, name="quality", version=None, completeness=0.5, annotation=0.5,
                    thresholds=None, scope=None, effective_at=None):
    payload = {
        "name": name,
        "completeness_weight": completeness,
        "annotation_weight": annotation,
    }
    if version is not None:
        payload["version"] = version
    if thresholds is not None:
        payload["thresholds"] = thresholds
    if scope is not None:
        payload["scope"] = scope
    if effective_at is not None:
        payload["effective_at"] = effective_at
    resp = client.post(f"{API}/strategies", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture()
def sample_operations(client):
    """三条作业：完整度 1.0 / 0.5 / 0.15，标注质量 0.8 / 0.9 / 无标注。"""
    rm_id, scene_id, skill_id = create_base_resources(client)

    op_full = make_operation(client, rm_id, scene_id, skill_id)
    annotate(client, op_full, 0.8)

    op_mid = make_operation(
        client, rm_id, scene_id, skill_id,
        environment_conditions=None, hardware_status=None, duration_ms=None,
    )
    annotate(client, op_mid, 0.9)

    op_low = make_operation(
        client, rm_id, scene_id, skill_id,
        motion_trajectory={"waypoints": [[0, 0, 0]]},
        perception_records={"sensor_0": 0, "sensor_1": 1},
        grasp_result=None, environment_conditions=None,
        hardware_status=None, duration_ms=None,
    )
    return {
        "robot_model_id": rm_id,
        "scene_id": scene_id,
        "skill_id": skill_id,
        "op_full": op_full,
        "op_mid": op_mid,
        "op_low": op_low,
    }


@pytest.fixture()
def two_drafts(client):
    base = create_strategy(client, name="quality", version=1)
    candidate = create_strategy(
        client, name="quality", version=2,
        completeness=0.7, annotation=0.3,
        thresholds={"A": 0.95, "B": 0.8, "C": 0.6},
    )
    return base, candidate


# ---------------------------------------------------------------------------
# 草稿比较（只读，不改写当前等级）
# ---------------------------------------------------------------------------

def test_draft_comparison_is_readonly(client, sample_operations, two_drafts):
    base, candidate = two_drafts
    ops = sample_operations

    # 先按现行口径给作业定级，作为「现有结果」
    resp = client.post(f"{API}/quality/grade-operations", json={})
    assert resp.status_code == 200
    before = {
        op_id: client.get(f"{API}/operations/{op_id}").json()
        for op_id in (ops["op_full"], ops["op_mid"], ops["op_low"])
    }
    assert all(item["data_grade"] is not None for item in before.values())

    # 数据集包含 op_full 与 op_mid
    dataset = client.post(f"{API}/datasets", json={
        "name": "装配数据集",
        "robot_model_id": ops["robot_model_id"],
        "scene_id": ops["scene_id"],
        "owner_team": "模型团队",
        "operation_data_ids": [ops["op_full"], ops["op_mid"]],
    })
    assert dataset.status_code == 200, dataset.text

    resp = client.post(f"{API}/strategy-comparisons", json={
        "base_strategy_id": base["id"],
        "candidate_strategy_id": candidate["id"],
        "note": "新口径影响评估",
        "created_by": "model-team",
    })
    assert resp.status_code == 201, resp.text
    report = resp.json()

    assert report["sample_count"] == 3
    assert report["base_strategy"]["status"] == "draft"
    assert report["candidate_strategy"]["status"] == "draft"

    # 等级迁移：A->B、B->C、D->D
    matrix = report["migration"]["matrix"]
    assert matrix["A"]["B"] == 1
    assert matrix["B"]["C"] == 1
    assert matrix["D"]["D"] == 1
    assert report["migration"]["changed_count"] == 2
    assert report["migration"]["unchanged_count"] == 1
    assert report["migration"]["change_rate"] == pytest.approx(2 / 3, abs=1e-4)

    # 边界样本：两条等级变化的样本，op_low 远离阈值不入选
    boundary = report["boundary_samples"]
    assert boundary["total"] == 2
    boundary_ids = [item["operation_id"] for item in boundary["items"]]
    assert boundary_ids == [ops["op_full"], ops["op_mid"]]
    first = boundary["items"][0]
    assert first["base"]["grade"] == "A" and first["candidate"]["grade"] == "B"
    assert first["changed"] is True

    # 整体与数据集汇总差异
    overall = report["overall"]
    assert overall["base_avg_score"] == pytest.approx(0.5583, abs=1e-4)
    assert overall["candidate_avg_score"] == pytest.approx(0.555, abs=1e-4)
    assert overall["delta_avg_score"] == pytest.approx(-0.0033, abs=1e-4)

    summaries = report["dataset_summaries"]
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["sample_count"] == 2
    assert summary["changed_count"] == 2
    assert summary["base"]["grade_distribution"] == {"A": 1, "B": 1, "C": 0, "D": 0}
    assert summary["candidate"]["grade_distribution"] == {"A": 0, "B": 1, "C": 1, "D": 0}
    assert summary["delta_avg_score"] == pytest.approx(-0.02, abs=1e-4)

    # 只读保证：作业的现有等级与分数不被改写
    after = {
        op_id: client.get(f"{API}/operations/{op_id}").json()
        for op_id in before
    }
    for op_id in before:
        assert after[op_id]["data_grade"] == before[op_id]["data_grade"]
        assert after[op_id]["quality_score"] == before[op_id]["quality_score"]


def test_comparison_with_explicit_operation_ids(client, sample_operations, two_drafts):
    base, candidate = two_drafts
    ops = sample_operations
    resp = client.post(f"{API}/strategy-comparisons", json={
        "base_strategy_id": base["id"],
        "candidate_strategy_id": candidate["id"],
        "operation_ids": [ops["op_full"]],
    })
    assert resp.status_code == 201, resp.text
    report = resp.json()
    assert report["sample_count"] == 1
    assert report["migration"]["matrix"]["A"]["B"] == 1
    assert report["input_snapshot"]["operations"][0]["operation_id"] == ops["op_full"]


def test_comparison_rejects_unknown_operation(client, sample_operations, two_drafts):
    base, candidate = two_drafts
    resp = client.post(f"{API}/strategy-comparisons", json={
        "base_strategy_id": base["id"],
        "candidate_strategy_id": candidate["id"],
        "operation_ids": [sample_operations["op_full"], 99999],
    })
    assert resp.status_code == 400
    assert "99999" in resp.json()["detail"]


def test_comparison_rejects_unknown_strategy(client, sample_operations):
    strategy = create_strategy(client, name="quality")
    resp = client.post(f"{API}/strategy-comparisons", json={
        "base_strategy_id": strategy["id"],
        "candidate_strategy_id": 99999,
    })
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 空样本
# ---------------------------------------------------------------------------

def test_comparison_with_empty_explicit_sample(client, two_drafts):
    base, candidate = two_drafts
    resp = client.post(f"{API}/strategy-comparisons", json={
        "base_strategy_id": base["id"],
        "candidate_strategy_id": candidate["id"],
        "operation_ids": [],
    })
    assert resp.status_code == 201, resp.text
    report = resp.json()
    assert report["sample_count"] == 0
    assert report["migration"]["changed_count"] == 0
    assert report["migration"]["change_rate"] == 0.0
    assert all(
        count == 0
        for row in report["migration"]["matrix"].values()
        for count in row.values()
    )
    assert report["boundary_samples"]["total"] == 0
    assert report["boundary_samples"]["items"] == []
    assert report["dataset_summaries"] == []
    assert report["overall"]["sample_count"] == 0
    assert report["overall"]["base_avg_score"] is None
    assert report["input_digest"]


def test_comparison_with_filter_matching_nothing(client, sample_operations, two_drafts):
    base, candidate = two_drafts
    resp = client.post(f"{API}/strategy-comparisons", json={
        "base_strategy_id": base["id"],
        "candidate_strategy_id": candidate["id"],
        "scene_id": 99999,  # 不存在的场景，匹配不到任何作业
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["sample_count"] == 0


# ---------------------------------------------------------------------------
# 版本冲突
# ---------------------------------------------------------------------------

def test_duplicate_version_conflicts(client):
    create_strategy(client, name="quality", version=1)
    resp = client.post(f"{API}/strategies", json={"name": "quality", "version": 1})
    assert resp.status_code == 409
    assert "已存在" in resp.json()["detail"]


def test_version_auto_increments_per_name(client):
    first = create_strategy(client, name="quality")
    second = create_strategy(client, name="quality")
    other = create_strategy(client, name="other")
    assert (first["version"], second["version"], other["version"]) == (1, 2, 1)


def test_update_non_draft_conflicts(client):
    strategy = create_strategy(client, name="quality")
    resp = client.post(f"{API}/strategies/{strategy['id']}/activate", json={})
    assert resp.status_code == 200

    resp = client.put(
        f"{API}/strategies/{strategy['id']}", json={"annotation_weight": 0.4}
    )
    assert resp.status_code == 409
    assert "草稿" in resp.json()["detail"]


def test_update_draft_revalidates_weights(client):
    strategy = create_strategy(client, name="quality")
    resp = client.put(
        f"{API}/strategies/{strategy['id']}",
        json={"completeness_weight": 0.4, "annotation_weight": 0.4},
    )
    assert resp.status_code == 422

    resp = client.put(
        f"{API}/strategies/{strategy['id']}",
        json={"completeness_weight": 0.4, "annotation_weight": 0.6},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["completeness_weight"] == 0.4


# ---------------------------------------------------------------------------
# 激活失败
# ---------------------------------------------------------------------------

def test_activate_missing_strategy_returns_404(client):
    resp = client.post(f"{API}/strategies/99999/activate", json={})
    assert resp.status_code == 404


def test_activate_active_or_retired_conflicts(client):
    first = create_strategy(client, name="quality", version=1)
    second = create_strategy(client, name="quality", version=2)

    resp = client.post(f"{API}/strategies/{first['id']}/activate", json={})
    assert resp.status_code == 200
    # 已生效策略不能重复激活
    resp = client.post(f"{API}/strategies/{first['id']}/activate", json={})
    assert resp.status_code == 409

    # 激活 v2 后 v1 下线，下线版本不能直接激活
    resp = client.post(f"{API}/strategies/{second['id']}/activate", json={})
    assert resp.status_code == 200
    resp = client.post(f"{API}/strategies/{first['id']}/activate", json={})
    assert resp.status_code == 409
    assert "回滚" in resp.json()["detail"]


def test_rollback_rejects_draft_and_active(client):
    draft = create_strategy(client, name="quality", version=1)
    resp = client.post(f"{API}/strategies/{draft['id']}/rollback", json={})
    assert resp.status_code == 409

    client.post(f"{API}/strategies/{draft['id']}/activate", json={})
    resp = client.post(f"{API}/strategies/{draft['id']}/rollback", json={})
    assert resp.status_code == 409


def test_invalid_strategy_payload_rejected(client):
    # 权重之和不为 1
    resp = client.post(f"{API}/strategies", json={
        "name": "bad-weights", "completeness_weight": 0.3, "annotation_weight": 0.3,
    })
    assert resp.status_code == 422

    # 阈值顺序非法
    resp = client.post(f"{API}/strategies", json={
        "name": "bad-thresholds", "thresholds": {"A": 0.6, "B": 0.8, "C": 0.5},
    })
    assert resp.status_code == 422

    # 生效时间缺时区
    resp = client.post(f"{API}/strategies", json={
        "name": "naive-time", "effective_at": "2026-10-01T00:00:00",
    })
    assert resp.status_code == 422

    # 适用范围引用不存在的场景
    resp = client.post(f"{API}/strategies", json={
        "name": "bad-scope", "scope": {"scene_id": 99999},
    })
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 重叠范围与并发激活
# ---------------------------------------------------------------------------

def test_activation_retires_overlapping_scope(client):
    _, scene_id, _ = create_base_resources(client)
    other_scene = client.post(
        f"{API}/scenes", json={"name": "仓储", "category": "物流"}
    ).json()

    global_v1 = create_strategy(client, name="global", version=1)
    resp = client.post(f"{API}/strategies/{global_v1['id']}/activate", json={})
    assert resp.status_code == 200

    # 场景级策略与全局策略范围重叠 -> 全局策略被自动下线
    scene_v1 = create_strategy(client, name="scene", version=1, scope={"scene_id": scene_id})
    resp = client.post(f"{API}/strategies/{scene_v1['id']}/activate", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["retired_strategy_ids"] == [global_v1["id"]]
    assert client.get(f"{API}/strategies/{global_v1['id']}").json()["status"] == "retired"

    # 不重叠的场景互不影响
    other = create_strategy(client, name="other-scene", version=1, scope={"scene_id": other_scene["id"]})
    resp = client.post(f"{API}/strategies/{other['id']}/activate", json={})
    assert resp.status_code == 200
    assert resp.json()["retired_strategy_ids"] == []

    actives = client.get(f"{API}/strategies?status=active").json()
    assert {item["id"] for item in actives} == {scene_v1["id"], other["id"]}


def test_concurrent_activation_of_same_draft(client_factory):
    client = client_factory()
    draft = create_strategy(client, name="quality", version=1)

    def activate(_):
        return client_factory().post(
            f"{API}/strategies/{draft['id']}/activate", json={"actor": "race"}
        ).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(activate, range(4)))

    assert statuses.count(200) == 1
    assert statuses.count(409) == 3
    actives = client.get(f"{API}/strategies?status=active").json()
    assert len(actives) == 1


def test_concurrent_activation_of_overlapping_drafts(client_factory):
    client = client_factory()
    drafts = [create_strategy(client, name=f"s{i}", version=1) for i in range(3)]

    def activate(strategy):
        return client_factory().post(
            f"{API}/strategies/{strategy['id']}/activate", json={"actor": "race"}
        ).status_code

    with ThreadPoolExecutor(max_workers=3) as pool:
        statuses = list(pool.map(activate, drafts))

    # 全部成功（串行化后依次顶替），最终全局范围只剩一个生效版本
    assert statuses == [200, 200, 200]
    actives = client.get(f"{API}/strategies?status=active").json()
    assert len(actives) == 1
    retired = client.get(f"{API}/strategies?status=retired").json()
    assert len(retired) == 2


# ---------------------------------------------------------------------------
# 回滚与审计
# ---------------------------------------------------------------------------

def test_rollback_restores_old_version_with_audit(client):
    v1 = create_strategy(client, name="quality", version=1)
    v2 = create_strategy(client, name="quality", version=2)

    client.post(f"{API}/strategies/{v1['id']}/activate", json={"actor": "ann"})
    client.post(f"{API}/strategies/{v2['id']}/activate", json={"actor": "bob"})
    assert client.get(f"{API}/strategies/{v1['id']}").json()["status"] == "retired"

    resp = client.post(f"{API}/strategies/{v1['id']}/rollback", json={"actor": "carol"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["strategy"]["status"] == "active"
    assert resp.json()["retired_strategy_ids"] == [v2["id"]]
    assert client.get(f"{API}/strategies/{v2['id']}").json()["status"] == "retired"

    audits = client.get(f"{API}/strategy-audits?strategy_id={v1['id']}").json()
    actions = [row["action"] for row in audits]
    assert actions == ["create", "activate", "retire", "rollback"]
    rollback_row = audits[-1]
    assert rollback_row["actor"] == "carol"
    assert rollback_row["detail"]["retired_strategy_ids"] == [v2["id"]]

    v2_audits = client.get(f"{API}/strategy-audits?strategy_id={v2['id']}").json()
    retire_row = v2_audits[-1]
    assert retire_row["action"] == "retire"
    assert retire_row["detail"]["reason"] == "rollback"
    assert retire_row["detail"]["replaced_by_strategy_id"] == v1["id"]


# ---------------------------------------------------------------------------
# 报告快照绑定与重启后重取
# ---------------------------------------------------------------------------

def test_report_survives_restart_and_ignores_new_data(client_factory):
    client = client_factory()
    rm_id, scene_id, skill_id = create_base_resources(client)
    op_id = make_operation(client, rm_id, scene_id, skill_id)
    annotate(client, op_id, 0.8)

    base = create_strategy(client, name="quality", version=1)
    candidate = create_strategy(
        client, name="quality", version=2, thresholds={"A": 0.95, "B": 0.8, "C": 0.6}
    )
    created = client.post(f"{API}/strategy-comparisons", json={
        "base_strategy_id": base["id"],
        "candidate_strategy_id": candidate["id"],
    })
    assert created.status_code == 201, created.text
    report_id = created.json()["id"]
    before = client.get(f"{API}/strategy-comparisons/{report_id}").json()
    assert before["sample_count"] == 1

    # 报告生成后新增作业与标注
    new_op = make_operation(client, rm_id, scene_id, skill_id)
    annotate(client, new_op, 0.1)

    # 模拟服务重启：同一数据库文件上重建引擎与客户端
    restarted_client = client_factory()
    after = restarted_client.get(f"{API}/strategy-comparisons/{report_id}")
    assert after.status_code == 200
    assert after.json() == before
    assert after.json()["input_digest"] == before["input_digest"]
    assert after.json()["sample_count"] == 1

    # 列表也能在重启后读到，且统计未被新数据污染
    listing = restarted_client.get(f"{API}/strategy-comparisons").json()
    assert [row["id"] for row in listing] == [report_id]
    assert listing[0]["sample_count"] == 1
