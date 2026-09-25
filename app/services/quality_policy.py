"""质量策略版本管理与只读对比的领域服务。

设计要点：
- 比较过程只读：计算结果与输入快照一起固化到 quality_comparison_reports，
  之后获取报告仅反序列化 JSON，绝不重算，后续新增数据不影响已保存报告。
- 激活/回滚在进程锁 + 单事务内完成；部分唯一索引兜底同 scope 并发激活。
- 策略范围用 (robot_model_id, scene_id, skill_id) 的子集约束表达：
  空范围为全局；两个范围在某维度均指定且取值不同时互不相交。
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Annotation,
    Dataset,
    DatasetItem,
    OperationData,
    QualityComparisonReport,
    QualityPolicy,
    QualityPolicyAuditLog,
    RobotModel,
    Scene,
    Skill,
)
from app.services.scoring import compute_operation_quality

GRADE_KEYS = ("grade_a", "grade_b", "grade_c")
GRADES = ("A", "B", "C", "D")
SCOPE_DIMENSIONS = ("robot_model_id", "scene_id", "skill_id")

# 单进程内串行化激活/回滚，配合数据库部分唯一索引处理并发。
_activation_lock = threading.RLock()
# 串行化自增版本号分配，避免并发创建草稿时 max(version)+1 撞号。
_version_lock = threading.RLock()


class PolicyError(ValueError):
    """策略领域错误基类。"""


class PolicyValidationFailure(PolicyError):
    """参数不合法，映射 HTTP 400。"""


class PolicyConflict(PolicyError):
    """版本冲突或范围重叠，映射 HTTP 409。"""


class PolicyNotFound(PolicyError):
    """策略或报告不存在，映射 HTTP 404。"""


# ---------------------------------------------------------------------------
# 纯函数：范围、哈希、校验
# ---------------------------------------------------------------------------

def normalize_scope(scope: Optional[Mapping[str, Any]]) -> Dict[str, int]:
    """仅保留三个维度上的非空约束，并按固定键序返回。"""
    if scope is None:
        return {}
    result: Dict[str, int] = {}
    for key in SCOPE_DIMENSIONS:
        value = scope.get(key)
        if value is not None:
            result[key] = int(value)
    return result


def validate_scope_references(db: Session, scope: Mapping[str, Any]) -> None:
    """范围中引用的机型/场景/技能必须存在。"""
    if "robot_model_id" in scope and db.get(RobotModel, scope["robot_model_id"]) is None:
        raise PolicyValidationFailure(f"机型 {scope['robot_model_id']} 不存在")
    if "scene_id" in scope and db.get(Scene, scope["scene_id"]) is None:
        raise PolicyValidationFailure(f"场景 {scope['scene_id']} 不存在")
    if "skill_id" in scope and db.get(Skill, scope["skill_id"]) is None:
        raise PolicyValidationFailure(f"技能 {scope['skill_id']} 不存在")


def scope_key(scope: Mapping[str, Any]) -> str:
    """范围的稳定字符串标识，空范围为空字符串。"""
    return "/".join(f"{key}={scope[key]}" for key in SCOPE_DIMENSIONS if key in scope)


def scopes_overlap(scope_a: Mapping[str, Any], scope_b: Mapping[str, Any]) -> bool:
    """两个范围是否可能同时适用同一作业（任一维度冲突即不相交）。"""
    for key in SCOPE_DIMENSIONS:
        if key in scope_a and key in scope_b and scope_a[key] != scope_b[key]:
            return False
    return True


def scope_covers(wide: Mapping[str, Any], narrow: Mapping[str, Any]) -> bool:
    """wide 是否完全覆盖 narrow：满足 narrow 的作业必然满足 wide。"""
    for key, value in wide.items():
        if narrow.get(key) != value:
            return False
    return True


def canonical_json(payload: Any) -> str:
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )


def compute_content_hash(
    name: str,
    weights: Mapping[str, float],
    thresholds: Mapping[str, float],
    scope: Mapping[str, int],
    effective_at: datetime,
) -> str:
    payload = canonical_json({
        "name": name,
        "weights": {k: round(float(weights[k]), 6) for k in sorted(weights)},
        "thresholds": {k: round(float(thresholds[k]), 6) for k in GRADE_KEYS},
        "scope": {k: scope[k] for k in SCOPE_DIMENSIONS if k in scope},
        "effective_at": _ensure_aware(effective_at).astimezone(timezone.utc).isoformat(),
    })
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ensure_aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise PolicyValidationFailure("生效时间必须携带时区信息")
    return moment


def _as_utc(moment: datetime) -> datetime:
    """SQLite 读回的时间不带时区，统一按 UTC 解释。"""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def validate_policy_payload(
    weights: Mapping[str, float],
    thresholds: Mapping[str, float],
    effective_at: datetime,
) -> Tuple[Dict[str, float], Dict[str, float]]:
    w_completeness = float(weights["completeness"])
    w_annotation = float(weights["annotation"])
    if abs((w_completeness + w_annotation) - 1.0) > 1e-6:
        raise PolicyValidationFailure("完整度权重与标注质量权重之和必须为1.0")
    if not (0.0 <= w_completeness <= 1.0 and 0.0 <= w_annotation <= 1.0):
        raise PolicyValidationFailure("权重必须位于0到1之间")

    clean_thresholds = {key: float(thresholds[key]) for key in GRADE_KEYS}
    a, b, c = clean_thresholds["grade_a"], clean_thresholds["grade_b"], clean_thresholds["grade_c"]
    if not (0.0 <= c < b < a <= 1.0):
        raise PolicyValidationFailure("阈值必须满足 0 <= grade_c < grade_b < grade_a <= 1")

    _ensure_aware(effective_at)
    return (
        {"completeness": w_completeness, "annotation": w_annotation},
        clean_thresholds,
    )


# ---------------------------------------------------------------------------
# 审计辅助
# ---------------------------------------------------------------------------

def _audit(db: Session, policy_id: int, action: str, detail: Optional[dict], actor: Optional[str]) -> None:
    db.add(QualityPolicyAuditLog(
        policy_id=policy_id, action=action, detail=detail, actor=actor
    ))


def _record_failure_and_raise(
    db: Session,
    policy: QualityPolicy,
    reason: str,
    conflicts: Sequence[QualityPolicy],
    actor: Optional[str],
) -> None:
    """激活失败也要落审计：回滚事务后单独写入失败记录。"""
    policy_id = policy.id
    conflict_versions = [item.version for item in conflicts]
    db.rollback()
    _audit(
        db, policy_id, "activation_failed",
        {"reason": reason, "conflicting_versions": conflict_versions},
        actor,
    )
    db.commit()
    raise PolicyConflict(reason)


# ---------------------------------------------------------------------------
# 策略版本生命周期
# ---------------------------------------------------------------------------

def create_policy(db: Session, payload: Any) -> QualityPolicy:
    scope = normalize_scope(payload.scope.model_dump(exclude_none=False) if payload.scope is not None else None)
    validate_scope_references(db, scope)
    weights, thresholds = validate_policy_payload(
        payload.weights.model_dump(), payload.thresholds.model_dump(), payload.effective_at
    )
    effective_at = payload.effective_at.astimezone(timezone.utc)

    with _version_lock:
        next_version = (db.query(func.max(QualityPolicy.version)).scalar() or 0) + 1

        policy = QualityPolicy(
            version=next_version,
            name=payload.name.strip(),
            status="draft",
            weights=weights,
            thresholds=thresholds,
            scope=scope,
            scope_key=scope_key(scope),
            effective_at=effective_at,
            content_hash="",
            created_by=payload.created_by,
            note=payload.note,
        )
        policy.content_hash = compute_content_hash(
            policy.name, weights, thresholds, scope, effective_at
        )
        db.add(policy)
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise PolicyConflict("并发创建导致版本号冲突，请重试") from exc
        _audit(db, policy.id, "created", {"version": next_version, "scope": scope}, payload.created_by)
        db.commit()
    db.refresh(policy)
    return policy


def update_draft(db: Session, policy_id: int, payload: Any) -> QualityPolicy:
    policy = db.get(QualityPolicy, policy_id)
    if policy is None:
        raise PolicyNotFound("策略版本不存在")
    if policy.status != "draft":
        raise PolicyValidationFailure(f"策略版本 {policy.version} 已{policy.status}，内容不可修改")

    if payload.expected_content_hash and payload.expected_content_hash != policy.content_hash:
        raise PolicyConflict("草稿已被他人修改，请基于最新内容重试")

    scope = normalize_scope(payload.scope.model_dump(exclude_none=False) if payload.scope is not None else None)
    validate_scope_references(db, scope)
    weights, thresholds = validate_policy_payload(
        payload.weights.model_dump(), payload.thresholds.model_dump(), payload.effective_at
    )
    effective_at = payload.effective_at.astimezone(timezone.utc)
    new_hash = compute_content_hash(payload.name.strip(), weights, thresholds, scope, effective_at)

    changes = {
        "name": policy.name != payload.name.strip(),
        "weights": policy.weights != weights,
        "thresholds": policy.thresholds != thresholds,
        "scope": policy.scope != scope,
        "effective_at": policy.effective_at != effective_at,
    }
    policy.name = payload.name.strip()
    policy.weights = weights
    policy.thresholds = thresholds
    policy.scope = scope
    policy.scope_key = scope_key(scope)
    policy.effective_at = effective_at
    policy.note = payload.note
    policy.content_hash = new_hash

    _audit(db, policy.id, "updated", {"changed_fields": [k for k, v in changes.items() if v]}, None)
    db.commit()
    db.refresh(policy)
    return policy


def _overlapping_actives(db: Session, scope: Mapping[str, Any]) -> List[QualityPolicy]:
    actives = db.query(QualityPolicy).filter(QualityPolicy.status == "active").all()
    return [item for item in actives if scopes_overlap(scope, item.scope)]


def _activate_locked(
    db: Session,
    policy: QualityPolicy,
    *,
    force_replace: bool,
    actor: Optional[str],
    action: str,
) -> QualityPolicy:
    """调用方必须持有 _activation_lock。"""
    target_scope = dict(policy.scope or {})
    now = datetime.now(timezone.utc)
    conflicts = _overlapping_actives(db, target_scope)

    displaced: List[QualityPolicy] = []
    if conflicts:
        fully_covered = all(scope_covers(target_scope, dict(item.scope or {})) for item in conflicts)
        if not force_replace or not fully_covered:
            reason = (
                "与现存激活版本范围重叠且新范围未完全覆盖旧版本，"
                "请缩小范围或确认后使用 force_replace"
                if force_replace else
                "与现存激活版本范围重叠，需使用 force_replace 完成覆盖式发布"
            )
            _record_failure_and_raise(db, policy, reason, conflicts, actor)

    policy_id = policy.id
    policy_version = policy.version
    for old in conflicts:
        displaced.append(old)
        old.status = "inactive"
        old.deactivated_at = now
        old.superseded_by = policy.id
        _audit(db, old.id, "superseded", {
            "by_version": policy_version,
            "reason": action,
        }, actor)
    try:
        # 先落库旧策略停用，避免同 scope 新旧 active 在提交瞬间违反部分唯一索引。
        if conflicts:
            db.flush()

        policy.status = "active"
        policy.activated_at = now
        policy.deactivated_at = None
        policy.superseded_by = None
        _audit(db, policy.id, action, {
            "effective_at": _as_utc(policy.effective_at).isoformat(),
            "force_replace": force_replace,
            "displaced_versions": [item.version for item in displaced],
            "scope": dict(policy.scope or {}),
        }, actor)
        db.commit()
    except IntegrityError:
        # 并发激活竞态：部分唯一索引兜底，记录失败审计后抛出。
        db.rollback()
        _audit(db, policy_id, "activation_failed", {
            "reason": "并发激活冲突",
        }, actor)
        db.commit()
        raise PolicyConflict("并发激活冲突，请重试")
    db.refresh(policy)
    return policy


def activate_policy(db: Session, policy_id: int, force_replace: bool, actor: Optional[str]) -> QualityPolicy:
    policy = db.get(QualityPolicy, policy_id)
    if policy is None:
        raise PolicyNotFound("策略版本不存在")
    if policy.status != "draft":
        raise PolicyValidationFailure(f"仅草稿可激活，当前状态为 {policy.status}")
    if _as_utc(policy.effective_at) > datetime.now(timezone.utc):
        raise PolicyValidationFailure("生效时间尚未到达，不能提前激活")

    with _activation_lock:
        return _activate_locked(db, policy, force_replace=force_replace, actor=actor, action="activated")


def rollback_policy(db: Session, policy_id: int, force_replace: bool, actor: Optional[str]) -> QualityPolicy:
    """重新激活历史版本，覆盖当前版本，并保留完整回滚审计。"""
    policy = db.get(QualityPolicy, policy_id)
    if policy is None:
        raise PolicyNotFound("策略版本不存在")
    if policy.status == "active":
        raise PolicyValidationFailure("该版本已处于激活状态，无需回滚")
    if policy.status == "draft":
        raise PolicyValidationFailure("草稿从未生效，不能作为回滚目标")

    with _activation_lock:
        return _activate_locked(db, policy, force_replace=force_replace, actor=actor, action="rollback")


def resolve_effective_policy(
    db: Session, at: datetime, attributes: Mapping[str, Any]
) -> Optional[QualityPolicy]:
    """按时间点与作业属性解析当前生效策略：范围最具体优先，其次生效时间/版本。"""
    _ensure_aware(at)
    moment = at.astimezone(timezone.utc)
    candidates = db.query(QualityPolicy).filter(
        QualityPolicy.status == "active",
    ).all()
    candidates = [item for item in candidates if _as_utc(item.effective_at) <= moment]
    matched = [
        item for item in candidates
        if all(attributes.get(key) == value for key, value in (item.scope or {}).items())
    ]
    if not matched:
        return None
    return max(
        matched,
        key=lambda item: (len(item.scope or {}), item.effective_at, item.activated_at or item.created_at, item.version),
    )


# ---------------------------------------------------------------------------
# 只读比较：输入快照 + 结果固化
# ---------------------------------------------------------------------------

def _policy_for_compare(db: Session, policy_id: int) -> QualityPolicy:
    policy = db.get(QualityPolicy, policy_id)
    if policy is None:
        raise PolicyNotFound(f"策略版本 {policy_id} 不存在")
    return policy


def _select_operations(
    db: Session, payload: Any
) -> Tuple[List[OperationData], Dict[str, Any]]:
    query = db.query(OperationData)
    selection: Dict[str, Any] = {"filters": {}, "dataset_id": None}

    if payload.operation_ids is not None:
        ids = list(payload.operation_ids)
        if ids:
            found = query.filter(OperationData.id.in_(ids)).all()
            missing = sorted(set(ids) - {item.id for item in found})
            if missing:
                raise PolicyValidationFailure(f"作业ID不存在：{missing[:10]}")
            found.sort(key=lambda item: ids.index(item.id))
        else:
            found = []
        selection["operation_ids"] = ids
        return found, selection

    if payload.dataset_id is not None:
        dataset = db.get(Dataset, payload.dataset_id)
        if dataset is None:
            raise PolicyValidationFailure(f"数据集 {payload.dataset_id} 不存在")
        selection["dataset_id"] = payload.dataset_id
        item_rows = db.query(DatasetItem.operation_data_id).filter(
            DatasetItem.dataset_id == payload.dataset_id
        ).all()
        ids = [row[0] for row in item_rows]
        operations = query.filter(OperationData.id.in_(ids)).all() if ids else []
        operations.sort(key=lambda item: item.id)
        return operations, selection

    filters = {}
    if payload.robot_model_id is not None:
        filters["robot_model_id"] = payload.robot_model_id
        query = query.filter(OperationData.robot_model_id == payload.robot_model_id)
    if payload.scene_id is not None:
        filters["scene_id"] = payload.scene_id
        query = query.filter(OperationData.scene_id == payload.scene_id)
    if payload.skill_id is not None:
        filters["skill_id"] = payload.skill_id
        query = query.filter(OperationData.skill_id == payload.skill_id)
    selection["filters"] = filters
    return query.order_by(OperationData.id).all(), selection


def _sample_snapshot(op: OperationData, annotation: Optional[Annotation]) -> Dict[str, Any]:
    """固化参与打分的全部输入；报告复算只依赖该快照。"""
    return {
        "operation_id": op.id,
        "robot_model_id": op.robot_model_id,
        "scene_id": op.scene_id,
        "skill_id": op.skill_id,
        "inputs": {
            "motion_trajectory": op.motion_trajectory,
            "perception_records": op.perception_records,
            "grasp_result": op.grasp_result,
            "environment_conditions": op.environment_conditions,
            "hardware_status": op.hardware_status,
            "duration_ms": op.duration_ms,
        },
        "annotation": None if annotation is None else {
            "review_status": annotation.review_status,
            "annotation_quality_score": annotation.annotation_quality_score,
            "failure_category": annotation.failure_category,
            "failure_description": annotation.failure_description,
        },
        "stored_grade": op.data_grade,
    }


def _operation_from_snapshot(snapshot: Mapping[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(**dict(snapshot["inputs"]))


def _annotation_from_snapshot(snapshot: Mapping[str, Any]) -> Optional[SimpleNamespace]:
    if snapshot["annotation"] is None:
        return None
    return SimpleNamespace(**dict(snapshot["annotation"]))


def _score_with_policy(policy: QualityPolicy, snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    """严格基于输入快照打分，不触碰实时 ORM 对象。"""
    scores = compute_operation_quality(
        operation=_operation_from_snapshot(snapshot),
        annotation=_annotation_from_snapshot(snapshot),
        completeness_weight=policy.weights["completeness"],
        annotation_weight=policy.weights["annotation"],
        thresholds=dict(policy.thresholds),
    )
    return {
        "completeness_score": scores.completeness_score,
        "annotation_quality_score": scores.annotation_quality_score,
        "quality_score": scores.quality_score,
        "grade": scores.data_grade,
    }


def _boundary_distance(quality_score: float, thresholds: Mapping[str, float]) -> float:
    return min(abs(quality_score - thresholds[key]) for key in GRADE_KEYS)


def _empty_matrix() -> Dict[str, Dict[str, int]]:
    return {left: {right: 0 for right in GRADES} for left in GRADES}


def create_comparison_report(db: Session, payload: Any) -> QualityComparisonReport:
    if payload.left_policy_id == payload.right_policy_id:
        raise PolicyValidationFailure("比较需要两个不同的策略版本")
    left = _policy_for_compare(db, payload.left_policy_id)
    right = _policy_for_compare(db, payload.right_policy_id)

    operations, selection = _select_operations(db, payload)

    samples: List[Dict[str, Any]] = []
    snapshot_samples: List[Dict[str, Any]] = []
    migration_matrix = _empty_matrix()
    left_distribution = {grade: 0 for grade in GRADES}
    right_distribution = {grade: 0 for grade in GRADES}
    left_quality_sum = right_quality_sum = 0.0
    changed_count = boundary_count = 0

    op_to_datasets: Dict[int, List[Tuple[int, str]]] = {}
    if operations:
        item_rows = (
            db.query(DatasetItem, Dataset)
            .join(Dataset, DatasetItem.dataset_id == Dataset.id)
            .filter(DatasetItem.operation_data_id.in_([op.id for op in operations]))
            .all()
        )
        for item, dataset in item_rows:
            op_to_datasets.setdefault(item.operation_data_id, []).append((dataset.id, dataset.name))

    for op in operations:
        annotation = db.query(Annotation).filter(
            Annotation.operation_data_id == op.id
        ).first()
        snapshot = _sample_snapshot(op, annotation)
        snapshot_samples.append(snapshot)

        left_result = _score_with_policy(left, snapshot)
        right_result = _score_with_policy(right, snapshot)
        distance = min(
            _boundary_distance(left_result["quality_score"], left.thresholds),
            _boundary_distance(right_result["quality_score"], right.thresholds),
        )
        is_boundary = distance <= payload.boundary_tolerance
        changed = left_result["grade"] != right_result["grade"]

        migration_matrix[left_result["grade"]][right_result["grade"]] += 1
        left_distribution[left_result["grade"]] += 1
        right_distribution[right_result["grade"]] += 1
        left_quality_sum += left_result["quality_score"]
        right_quality_sum += right_result["quality_score"]
        changed_count += 1 if changed else 0
        boundary_count += 1 if is_boundary else 0

        row = {
            "operation_id": op.id,
            "left": left_result,
            "right": right_result,
            "transition": f"{left_result['grade']}->{right_result['grade']}",
            "changed": changed,
            "boundary": is_boundary,
            "boundary_distance": round(distance, 4),
            "stored_grade": op.data_grade,
            "datasets": [
                {"dataset_id": ds_id, "name": name}
                for ds_id, name in op_to_datasets.get(op.id, [])
            ],
        }
        samples.append(row)

    total = len(operations)

    # 数据集汇总差异
    dataset_stats: Dict[int, Dict[str, Any]] = {}
    for row in samples:
        for ref in row["datasets"]:
            stat = dataset_stats.setdefault(ref["dataset_id"], {
                "dataset_id": ref["dataset_id"],
                "name": ref["name"],
                "sample_items": 0,
                "left_distribution": {grade: 0 for grade in GRADES},
                "right_distribution": {grade: 0 for grade in GRADES},
                "left_quality_sum": 0.0,
                "right_quality_sum": 0.0,
                "changed_count": 0,
            })
            stat["sample_items"] += 1
            stat["left_distribution"][row["left"]["grade"]] += 1
            stat["right_distribution"][row["right"]["grade"]] += 1
            stat["left_quality_sum"] += row["left"]["quality_score"]
            stat["right_quality_sum"] += row["right"]["quality_score"]
            stat["changed_count"] += 1 if row["changed"] else 0

    dataset_summaries = []
    for stat in sorted(dataset_stats.values(), key=lambda item: item["dataset_id"]):
        count = stat["sample_items"]
        left_dominant = max(stat["left_distribution"], key=stat["left_distribution"].get)
        right_dominant = max(stat["right_distribution"], key=stat["right_distribution"].get)
        dataset_summaries.append({
            "dataset_id": stat["dataset_id"],
            "name": stat["name"],
            "sample_items": count,
            "left": {
                "distribution": stat["left_distribution"],
                "dominant_grade": left_dominant,
                "average_quality_score": round(stat["left_quality_sum"] / count, 4),
            },
            "right": {
                "distribution": stat["right_distribution"],
                "dominant_grade": right_dominant,
                "average_quality_score": round(stat["right_quality_sum"] / count, 4),
            },
            "dominant_grade_changed": left_dominant != right_dominant,
            "changed_count": stat["changed_count"],
        })

    input_snapshot = {
        "selection": selection,
        "boundary_tolerance": payload.boundary_tolerance,
        "policies": {
            "left": {"id": left.id, "version": left.version, "content_hash": left.content_hash},
            "right": {"id": right.id, "version": right.version, "content_hash": right.content_hash},
        },
        "samples": snapshot_samples,
    }
    snapshot_hash = hashlib.sha256(canonical_json(input_snapshot).encode("utf-8")).hexdigest()

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policies": {
            "left": {"id": left.id, "version": left.version, "name": left.name,
                     "weights": left.weights, "thresholds": left.thresholds, "scope": left.scope},
            "right": {"id": right.id, "version": right.version, "name": right.name,
                      "weights": right.weights, "thresholds": right.thresholds, "scope": right.scope},
        },
        "selection": selection,
        "boundary_tolerance": payload.boundary_tolerance,
        "totals": {
            "sample_count": total,
            "changed_count": changed_count,
            "boundary_count": boundary_count,
            "left_distribution": left_distribution,
            "right_distribution": right_distribution,
            "left_average_quality_score": round(left_quality_sum / total, 4) if total else None,
            "right_average_quality_score": round(right_quality_sum / total, 4) if total else None,
        },
        "migration_matrix": migration_matrix,
        "boundary_samples": [
            {
                "operation_id": row["operation_id"],
                "transition": row["transition"],
                "boundary_distance": row["boundary_distance"],
                "left_quality_score": row["left"]["quality_score"],
                "right_quality_score": row["right"]["quality_score"],
            }
            for row in samples if row["boundary"]
        ],
        "dataset_summaries": dataset_summaries,
        "per_sample": samples,
    }

    report = QualityComparisonReport(
        left_policy_id=left.id,
        right_policy_id=right.id,
        baseline_label=f"v{left.version}",
        candidate_label=f"v{right.version}",
        sample_count=total,
        changed_count=changed_count,
        boundary_count=boundary_count,
        snapshot_hash=snapshot_hash,
        input_snapshot_json=input_snapshot,
        result_json=result,
        created_by=payload.created_by,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return report


def get_report(db: Session, report_id: int) -> QualityComparisonReport:
    report = db.get(QualityComparisonReport, report_id)
    if report is None:
        raise PolicyNotFound("比较报告不存在")
    return report


def list_reports(db: Session, limit: int, offset: int) -> List[QualityComparisonReport]:
    return (
        db.query(QualityComparisonReport)
        .order_by(QualityComparisonReport.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def report_detail(report: QualityComparisonReport) -> Dict[str, Any]:
    """只读返回固化内容，不触碰作业数据。"""
    return {
        "id": report.id,
        "left_policy_id": report.left_policy_id,
        "right_policy_id": report.right_policy_id,
        "baseline_label": report.baseline_label,
        "candidate_label": report.candidate_label,
        "sample_count": report.sample_count,
        "changed_count": report.changed_count,
        "boundary_count": report.boundary_count,
        "snapshot_hash": report.snapshot_hash,
        "created_by": report.created_by,
        "created_at": report.created_at,
        "input_snapshot": report.input_snapshot_json,
        "result": report.result_json,
    }
