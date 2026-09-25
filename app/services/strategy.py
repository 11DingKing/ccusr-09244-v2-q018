"""评分策略版本管理与只读对比的领域逻辑。

策略生命周期：draft -> active -> retired（可回滚重新激活）。
对比报告在创建时固化输入快照与结果，之后只读，不随新数据变化。
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Annotation,
    Dataset,
    DatasetItem,
    OperationData,
    RobotModel,
    Scene,
    ScoringStrategy,
    Skill,
    StrategyAudit,
    StrategyComparison,
)
from app.services.scoring import (
    calculate_annotation_quality_score,
    calculate_completeness_score,
    determine_grade,
)

GRADE_ORDER: Tuple[str, ...] = ("A", "B", "C", "D")
SCOPE_KEYS: Tuple[str, ...] = ("robot_model_id", "scene_id", "skill_id")
BOUNDARY_SAMPLE_LIMIT = 200

# 激活/回滚属于全局互斥的临界区：先串行化，再在单事务内完成
# 「读当前生效 -> 下线重叠 -> 激活新版本」，保证并发激活结果确定。
_activation_lock = threading.Lock()


class StrategyError(ValueError):
    """请求内容不合法（HTTP 400）。"""


class StrategyNotFoundError(LookupError):
    """目标资源不存在（HTTP 404）。"""


class StrategyConflictError(RuntimeError):
    """版本或状态冲突（HTTP 409）。"""


# ---------------------------------------------------------------------------
# 校验与快照
# ---------------------------------------------------------------------------

def normalize_scope(scope: Optional[Dict[str, Any]]) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for key, value in (scope or {}).items():
        if key not in SCOPE_KEYS:
            raise StrategyError(f"适用范围包含不支持的维度: {key}")
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise StrategyError(f"适用范围维度 {key} 必须是正整数")
        result[key] = value
    return result


def scopes_overlap(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    """两个范围在每个维度上一致或至少一方不限制时视为重叠。"""
    for key in SCOPE_KEYS:
        lv, rv = left.get(key), right.get(key)
        if lv is not None and rv is not None and lv != rv:
            return False
    return True


def validate_scope_references(db: Session, scope: Dict[str, int]) -> None:
    checks = (
        ("robot_model_id", RobotModel, "机型"),
        ("scene_id", Scene, "场景"),
        ("skill_id", Skill, "技能"),
    )
    for key, model, label in checks:
        value = scope.get(key)
        if value is not None and db.query(model).filter(model.id == value).first() is None:
            raise StrategyError(f"适用范围引用的{label}不存在: {value}")


def validate_thresholds(thresholds: Dict[str, Any]) -> Dict[str, float]:
    try:
        a = float(thresholds["A"])
        b = float(thresholds["B"])
        c = float(thresholds["C"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StrategyError("阈值必须包含 A/B/C 三个数值") from exc
    for value in (a, b, c):
        if not 0.0 <= value <= 1.0:
            raise StrategyError("阈值必须位于 0 到 1 之间")
    if not (a > b > c):
        raise StrategyError("阈值必须满足 A > B > C")
    return {"A": a, "B": b, "C": c}


def strategy_snapshot(strategy: ScoringStrategy) -> Dict[str, Any]:
    """策略内容的不可变描述，随报告一起保存。"""
    effective_at = strategy.effective_at
    return {
        "id": strategy.id,
        "name": strategy.name,
        "version": strategy.version,
        "status": strategy.status,
        "completeness_weight": strategy.completeness_weight,
        "annotation_weight": strategy.annotation_weight,
        "thresholds": dict(strategy.thresholds or {}),
        "scope": dict(strategy.scope or {}),
        "effective_at": effective_at.isoformat() if effective_at else None,
    }


def _add_audit(
    db: Session,
    strategy_id: int,
    action: str,
    actor: Optional[str],
    detail: Optional[Dict[str, Any]] = None,
) -> StrategyAudit:
    audit = StrategyAudit(
        strategy_id=strategy_id,
        action=action,
        actor=actor,
        detail=detail or {},
    )
    db.add(audit)
    return audit


# ---------------------------------------------------------------------------
# 策略生命周期
# ---------------------------------------------------------------------------

def create_strategy(db: Session, payload) -> ScoringStrategy:
    scope = normalize_scope(payload.scope.model_dump() if hasattr(payload.scope, "model_dump") else payload.scope)
    validate_scope_references(db, scope)
    thresholds = validate_thresholds(
        payload.thresholds.model_dump() if hasattr(payload.thresholds, "model_dump") else payload.thresholds
    )

    with _activation_lock:
        if payload.version is not None:
            version = payload.version
            existing = (
                db.query(ScoringStrategy)
                .filter(ScoringStrategy.name == payload.name, ScoringStrategy.version == version)
                .first()
            )
            if existing is not None:
                raise StrategyConflictError(f"策略 {payload.name} 的版本 {version} 已存在")
        else:
            current_max = (
                db.query(ScoringStrategy.version)
                .filter(ScoringStrategy.name == payload.name)
                .order_by(ScoringStrategy.version.desc())
                .first()
            )
            version = (current_max[0] if current_max else 0) + 1

        strategy = ScoringStrategy(
            name=payload.name.strip(),
            version=version,
            status=ScoringStrategy.STATUS_DRAFT,
            completeness_weight=payload.completeness_weight,
            annotation_weight=payload.annotation_weight,
            thresholds=thresholds,
            scope=scope,
            effective_at=payload.effective_at,
            note=payload.note,
            created_by=payload.created_by,
        )
        db.add(strategy)
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise StrategyConflictError(f"策略 {payload.name} 的版本 {version} 已存在") from exc
        _add_audit(db, strategy.id, StrategyAudit.ACTION_CREATE, payload.created_by,
                   {"name": strategy.name, "version": strategy.version})
        db.commit()
        db.refresh(strategy)
        return strategy


def get_strategy(db: Session, strategy_id: int) -> ScoringStrategy:
    strategy = db.query(ScoringStrategy).filter(ScoringStrategy.id == strategy_id).first()
    if strategy is None:
        raise StrategyNotFoundError("策略不存在")
    return strategy


def update_draft(db: Session, strategy_id: int, payload, actor: Optional[str] = None) -> ScoringStrategy:
    strategy = get_strategy(db, strategy_id)
    if strategy.status != ScoringStrategy.STATUS_DRAFT:
        raise StrategyConflictError("只有草稿状态的策略可以编辑")

    update = payload.model_dump(exclude_unset=True)
    changes: Dict[str, Any] = {}

    new_completeness = update.get("completeness_weight", strategy.completeness_weight)
    new_annotation = update.get("annotation_weight", strategy.annotation_weight)
    if abs(new_completeness + new_annotation - 1.0) > 1e-6:
        raise StrategyError("完整度权重和标注质量权重之和必须为1.0")

    if "completeness_weight" in update:
        strategy.completeness_weight = update["completeness_weight"]
        changes["completeness_weight"] = update["completeness_weight"]
    if "annotation_weight" in update:
        strategy.annotation_weight = update["annotation_weight"]
        changes["annotation_weight"] = update["annotation_weight"]
    if "thresholds" in update and update["thresholds"] is not None:
        raw = update["thresholds"]
        strategy.thresholds = validate_thresholds(raw.model_dump() if hasattr(raw, "model_dump") else raw)
        changes["thresholds"] = strategy.thresholds
    if "scope" in update and update["scope"] is not None:
        raw = update["scope"]
        scope = normalize_scope(raw.model_dump() if hasattr(raw, "model_dump") else raw)
        validate_scope_references(db, scope)
        strategy.scope = scope
        changes["scope"] = scope
    if "effective_at" in update:
        effective_at = update["effective_at"]
        if effective_at is not None and effective_at.tzinfo is None:
            raise StrategyError("生效时间必须带时区")
        strategy.effective_at = effective_at
        changes["effective_at"] = effective_at.isoformat() if effective_at else None
    if "note" in update:
        strategy.note = update["note"]
        changes["note"] = update["note"]

    _add_audit(db, strategy.id, StrategyAudit.ACTION_UPDATE, actor, {"changes": changes})
    db.commit()
    db.refresh(strategy)
    return strategy


def _overlapping_actives(db: Session, strategy: ScoringStrategy) -> List[ScoringStrategy]:
    actives = (
        db.query(ScoringStrategy)
        .filter(
            ScoringStrategy.status == ScoringStrategy.STATUS_ACTIVE,
            ScoringStrategy.id != strategy.id,
        )
        .all()
    )
    return [item for item in actives if scopes_overlap(item.scope or {}, strategy.scope or {})]


def _retire(db: Session, strategy: ScoringStrategy, actor: Optional[str], detail: Dict[str, Any]) -> None:
    strategy.status = ScoringStrategy.STATUS_RETIRED
    strategy.retired_at = datetime.now(timezone.utc)
    _add_audit(db, strategy.id, StrategyAudit.ACTION_RETIRE, actor, detail)


def activate_strategy(db: Session, strategy_id: int, actor: Optional[str]) -> Tuple[ScoringStrategy, List[int]]:
    """激活草稿：同一事务内自动下线范围重叠的生效版本。"""
    with _activation_lock:
        strategy = get_strategy(db, strategy_id)
        if strategy.status == ScoringStrategy.STATUS_ACTIVE:
            raise StrategyConflictError("策略已处于生效状态")
        if strategy.status == ScoringStrategy.STATUS_RETIRED:
            raise StrategyConflictError("已下线策略不能直接激活，请使用回滚接口")

        now = datetime.now(timezone.utc)
        retired_ids: List[int] = []
        for other in _overlapping_actives(db, strategy):
            _retire(db, other, actor, {
                "reason": "scope_overlap",
                "replaced_by_strategy_id": strategy.id,
                "replaced_by_version": f"{strategy.name}@{strategy.version}",
            })
            retired_ids.append(other.id)

        strategy.status = ScoringStrategy.STATUS_ACTIVE
        strategy.activated_at = now
        strategy.retired_at = None
        if strategy.effective_at is None:
            strategy.effective_at = now
        _add_audit(db, strategy.id, StrategyAudit.ACTION_ACTIVATE, actor, {
            "name": strategy.name,
            "version": strategy.version,
            "effective_at": strategy.effective_at.isoformat(),
            "retired_strategy_ids": retired_ids,
        })
        db.commit()
        db.refresh(strategy)
        return strategy, retired_ids


def rollback_strategy(db: Session, strategy_id: int, actor: Optional[str]) -> Tuple[ScoringStrategy, List[int]]:
    """回滚到已下线版本：重新激活它并下线当前重叠的生效版本，全程留痕。"""
    with _activation_lock:
        strategy = get_strategy(db, strategy_id)
        if strategy.status == ScoringStrategy.STATUS_DRAFT:
            raise StrategyConflictError("草稿策略无需回滚，请直接激活")
        if strategy.status == ScoringStrategy.STATUS_ACTIVE:
            raise StrategyConflictError("策略当前已生效，无需回滚")

        now = datetime.now(timezone.utc)
        retired_ids: List[int] = []
        for other in _overlapping_actives(db, strategy):
            _retire(db, other, actor, {
                "reason": "rollback",
                "replaced_by_strategy_id": strategy.id,
                "replaced_by_version": f"{strategy.name}@{strategy.version}",
            })
            retired_ids.append(other.id)

        strategy.status = ScoringStrategy.STATUS_ACTIVE
        strategy.activated_at = now
        strategy.retired_at = None
        _add_audit(db, strategy.id, StrategyAudit.ACTION_ROLLBACK, actor, {
            "name": strategy.name,
            "version": strategy.version,
            "rollback_to_version": strategy.version,
            "retired_strategy_ids": retired_ids,
        })
        db.commit()
        db.refresh(strategy)
        return strategy, retired_ids


def list_audits(db: Session, strategy_id: Optional[int] = None) -> List[StrategyAudit]:
    query = db.query(StrategyAudit)
    if strategy_id is not None:
        query = query.filter(StrategyAudit.strategy_id == strategy_id)
    return query.order_by(StrategyAudit.id.asc()).all()


# ---------------------------------------------------------------------------
# 只读对比
# ---------------------------------------------------------------------------

def _score_with(snapshot: Dict[str, Any], completeness: float, annotation_quality: float) -> Dict[str, Any]:
    score = round(
        completeness * snapshot["completeness_weight"]
        + annotation_quality * snapshot["annotation_weight"],
        4,
    )
    thresholds = snapshot["thresholds"]
    grade = determine_grade(score, {
        "grade_a": thresholds["A"],
        "grade_b": thresholds["B"],
        "grade_c": thresholds["C"],
    })
    distance = min(abs(score - value) for value in thresholds.values())
    return {"score": score, "grade": grade, "threshold_distance": round(distance, 4)}


def _collect_operations(db: Session, payload) -> List[OperationData]:
    if payload.operation_ids is not None:
        if not payload.operation_ids:
            return []
        operations = (
            db.query(OperationData)
            .filter(OperationData.id.in_(payload.operation_ids))
            .order_by(OperationData.id.asc())
            .all()
        )
        found = {op.id for op in operations}
        missing = [op_id for op_id in payload.operation_ids if op_id not in found]
        if missing:
            raise StrategyError(f"作业数据不存在: {missing}")
        return operations

    query = db.query(OperationData)
    if payload.robot_model_id is not None:
        query = query.filter(OperationData.robot_model_id == payload.robot_model_id)
    if payload.scene_id is not None:
        query = query.filter(OperationData.scene_id == payload.scene_id)
    if payload.skill_id is not None:
        query = query.filter(OperationData.skill_id == payload.skill_id)
    if payload.dataset_id is not None:
        dataset = db.query(Dataset).filter(Dataset.id == payload.dataset_id).first()
        if dataset is None:
            raise StrategyError(f"数据集不存在: {payload.dataset_id}")
        query = query.join(DatasetItem, DatasetItem.operation_data_id == OperationData.id).filter(
            DatasetItem.dataset_id == payload.dataset_id
        )
    return query.order_by(OperationData.id.asc()).all()


def _build_result(
    db: Session,
    base: Dict[str, Any],
    candidate: Dict[str, Any],
    inputs: List[Dict[str, Any]],
    margin: float,
) -> Dict[str, Any]:
    matrix: Dict[str, Dict[str, int]] = {grade: {g: 0 for g in GRADE_ORDER} for grade in GRADE_ORDER}
    per_sample: List[Dict[str, Any]] = []
    changed_count = 0
    base_score_sum = 0.0
    candidate_score_sum = 0.0

    for item in inputs:
        base_eval = _score_with(base, item["completeness_score"], item["annotation_quality_score"])
        candidate_eval = _score_with(candidate, item["completeness_score"], item["annotation_quality_score"])
        matrix[base_eval["grade"]][candidate_eval["grade"]] += 1
        changed = base_eval["grade"] != candidate_eval["grade"]
        if changed:
            changed_count += 1
        base_score_sum += base_eval["score"]
        candidate_score_sum += candidate_eval["score"]
        per_sample.append({
            "operation_id": item["operation_id"],
            "base": base_eval,
            "candidate": candidate_eval,
            "changed": changed,
        })

    total = len(inputs)

    boundary_items = [
        sample for sample in per_sample
        if sample["changed"]
        or min(sample["base"]["threshold_distance"], sample["candidate"]["threshold_distance"]) <= margin
    ]
    boundary_items.sort(
        key=lambda sample: (
            not sample["changed"],
            min(sample["base"]["threshold_distance"], sample["candidate"]["threshold_distance"]),
            sample["operation_id"],
        )
    )
    boundary = {
        "margin": margin,
        "total": len(boundary_items),
        "items": boundary_items[:BOUNDARY_SAMPLE_LIMIT],
    }

    migration = {
        "matrix": matrix,
        "changed_count": changed_count,
        "unchanged_count": total - changed_count,
        "change_rate": round(changed_count / total, 4) if total else 0.0,
    }

    overall = {
        "sample_count": total,
        "base_avg_score": round(base_score_sum / total, 4) if total else None,
        "candidate_avg_score": round(candidate_score_sum / total, 4) if total else None,
        "delta_avg_score": round((candidate_score_sum - base_score_sum) / total, 4) if total else None,
    }

    dataset_summaries = _dataset_summaries(db, per_sample)

    return {
        "migration": migration,
        "boundary_samples": boundary,
        "dataset_summaries": dataset_summaries,
        "overall": overall,
    }


def _dataset_summaries(db: Session, per_sample: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not per_sample:
        return []

    operation_ids = [sample["operation_id"] for sample in per_sample]
    items = (
        db.query(DatasetItem, Dataset)
        .join(Dataset, Dataset.id == DatasetItem.dataset_id)
        .filter(DatasetItem.operation_data_id.in_(operation_ids))
        .all()
    )

    by_dataset: Dict[int, Dict[str, Any]] = {}
    for item, dataset in items:
        entry = by_dataset.setdefault(dataset.id, {
            "dataset_id": dataset.id,
            "dataset_name": dataset.name,
            "operation_ids": [],
        })
        entry["operation_ids"].append(item.operation_data_id)

    sample_by_operation = {sample["operation_id"]: sample for sample in per_sample}
    summaries: List[Dict[str, Any]] = []
    for entry in by_dataset.values():
        samples = [sample_by_operation[op_id] for op_id in entry["operation_ids"] if op_id in sample_by_operation]
        if not samples:
            continue
        base_dist = {grade: 0 for grade in GRADE_ORDER}
        candidate_dist = {grade: 0 for grade in GRADE_ORDER}
        base_sum = 0.0
        candidate_sum = 0.0
        changed = 0
        for sample in samples:
            base_dist[sample["base"]["grade"]] += 1
            candidate_dist[sample["candidate"]["grade"]] += 1
            base_sum += sample["base"]["score"]
            candidate_sum += sample["candidate"]["score"]
            if sample["changed"]:
                changed += 1
        count = len(samples)
        base_avg = round(base_sum / count, 4)
        candidate_avg = round(candidate_sum / count, 4)
        summaries.append({
            "dataset_id": entry["dataset_id"],
            "dataset_name": entry["dataset_name"],
            "sample_count": count,
            "changed_count": changed,
            "base": {"avg_score": base_avg, "grade_distribution": base_dist},
            "candidate": {"avg_score": candidate_avg, "grade_distribution": candidate_dist},
            "delta_avg_score": round(candidate_avg - base_avg, 4),
        })

    summaries.sort(key=lambda row: (-abs(row["delta_avg_score"]), row["dataset_id"]))
    return summaries


def run_comparison(db: Session, payload) -> StrategyComparison:
    """创建只读对比报告：计算并固化输入快照与结果，不修改任何业务数据。"""
    base_strategy = get_strategy(db, payload.base_strategy_id)
    candidate_strategy = get_strategy(db, payload.candidate_strategy_id)
    base = strategy_snapshot(base_strategy)
    candidate = strategy_snapshot(candidate_strategy)

    operations = _collect_operations(db, payload)

    annotation_map: Dict[int, Annotation] = {}
    if operations:
        annotations = db.query(Annotation).filter(
            Annotation.operation_data_id.in_([op.id for op in operations])
        ).all()
        annotation_map = {item.operation_data_id: item for item in annotations}

    inputs: List[Dict[str, Any]] = []
    for op in operations:
        inputs.append({
            "operation_id": op.id,
            "completeness_score": calculate_completeness_score(op),
            "annotation_quality_score": calculate_annotation_quality_score(annotation_map.get(op.id)),
        })

    input_snapshot = {"operations": inputs, "count": len(inputs)}
    digest = hashlib.sha256(
        json.dumps(input_snapshot, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()

    result = _build_result(db, base, candidate, inputs, payload.boundary_margin)

    sample_filter = {
        "operation_ids": list(payload.operation_ids) if payload.operation_ids is not None else None,
        "robot_model_id": payload.robot_model_id,
        "scene_id": payload.scene_id,
        "skill_id": payload.skill_id,
        "dataset_id": payload.dataset_id,
    }

    report = StrategyComparison(
        base_strategy_id=base_strategy.id,
        candidate_strategy_id=candidate_strategy.id,
        base_snapshot=base,
        candidate_snapshot=candidate,
        sample_filter=sample_filter,
        input_snapshot=input_snapshot,
        input_digest=digest,
        result=result,
        sample_count=len(inputs),
        note=payload.note,
        created_by=payload.created_by,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return report


def get_comparison(db: Session, report_id: int) -> StrategyComparison:
    report = db.query(StrategyComparison).filter(StrategyComparison.id == report_id).first()
    if report is None:
        raise StrategyNotFoundError("对比报告不存在")
    return report


def list_comparisons(db: Session) -> List[StrategyComparison]:
    return db.query(StrategyComparison).order_by(StrategyComparison.id.desc()).all()


def comparison_to_dict(report: StrategyComparison) -> Dict[str, Any]:
    """报告响应完全来自已保存的快照与结果，不做任何重算。"""
    return {
        "id": report.id,
        "created_at": report.created_at.isoformat() if report.created_at else None,
        "created_by": report.created_by,
        "note": report.note,
        "base_strategy": report.base_snapshot,
        "candidate_strategy": report.candidate_snapshot,
        "sample_filter": report.sample_filter,
        "sample_count": report.sample_count,
        "input_digest": report.input_digest,
        "input_snapshot": report.input_snapshot,
        "migration": report.result["migration"],
        "boundary_samples": report.result["boundary_samples"],
        "dataset_summaries": report.result["dataset_summaries"],
        "overall": report.result["overall"],
    }
