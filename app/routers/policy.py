"""质量策略版本管理与只读对比接口。"""

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import QualityPolicy
from app.schemas.policy import (
    ComparisonCreateRequest,
    ComparisonSummaryResponse,
    PolicyActivateRequest,
    PolicyAuditLogResponse,
    QualityPolicyCreate,
    QualityPolicyResponse,
    QualityPolicyUpdate,
)
from app.services import quality_policy as qp

router = APIRouter()


@router.post("/quality-policies", response_model=QualityPolicyResponse, tags=["质量策略版本"])
def create_quality_policy(payload: QualityPolicyCreate, db: Session = Depends(get_db)):
    try:
        return qp.create_policy(db, payload)
    except qp.PolicyValidationFailure as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except qp.PolicyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/quality-policies", response_model=List[QualityPolicyResponse], tags=["质量策略版本"])
def list_quality_policies(
    status: Optional[str] = Query(None, description="draft/active/inactive"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    query = db.query(QualityPolicy)
    if status:
        query = query.filter(QualityPolicy.status == status)
    return query.order_by(QualityPolicy.version.desc()).offset(skip).limit(limit).all()


@router.get("/quality-policies/effective", response_model=QualityPolicyResponse, tags=["质量策略版本"])
def get_effective_policy(
    robot_model_id: Optional[int] = Query(None),
    scene_id: Optional[int] = Query(None),
    skill_id: Optional[int] = Query(None),
    at: Optional[datetime] = Query(None, description="解析时点，缺省为当前时间"),
    db: Session = Depends(get_db),
):
    moment = at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    policy = qp.resolve_effective_policy(db, moment, {
        "robot_model_id": robot_model_id,
        "scene_id": scene_id,
        "skill_id": skill_id,
    })
    if policy is None:
        raise HTTPException(status_code=404, detail="该时点与范围内没有已生效策略")
    return policy


@router.get("/quality-policies/{policy_id}", response_model=QualityPolicyResponse, tags=["质量策略版本"])
def get_quality_policy(policy_id: int, db: Session = Depends(get_db)):
    policy = db.get(QualityPolicy, policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="策略版本不存在")
    return policy


@router.put("/quality-policies/{policy_id}", response_model=QualityPolicyResponse, tags=["质量策略版本"])
def update_quality_policy(policy_id: int, payload: QualityPolicyUpdate, db: Session = Depends(get_db)):
    try:
        return qp.update_draft(db, policy_id, payload)
    except qp.PolicyNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except qp.PolicyValidationFailure as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except qp.PolicyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/quality-policies/{policy_id}/activate", response_model=QualityPolicyResponse, tags=["质量策略版本"])
def activate_quality_policy(policy_id: int, payload: PolicyActivateRequest, db: Session = Depends(get_db)):
    try:
        return qp.activate_policy(db, policy_id, payload.force_replace, payload.actor)
    except qp.PolicyNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except qp.PolicyValidationFailure as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except qp.PolicyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/quality-policies/{policy_id}/rollback", response_model=QualityPolicyResponse, tags=["质量策略版本"])
def rollback_quality_policy(policy_id: int, payload: PolicyActivateRequest, db: Session = Depends(get_db)):
    try:
        return qp.rollback_policy(db, policy_id, payload.force_replace, payload.actor)
    except qp.PolicyNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except qp.PolicyValidationFailure as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except qp.PolicyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get(
    "/quality-policies/{policy_id}/audit-logs",
    response_model=List[PolicyAuditLogResponse],
    tags=["质量策略版本"],
)
def list_policy_audit_logs(policy_id: int, db: Session = Depends(get_db)):
    policy = db.get(QualityPolicy, policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="策略版本不存在")
    return sorted(policy.audit_logs, key=lambda item: item.id)


@router.post("/quality-comparisons", tags=["质量策略对比"])
def create_comparison(payload: ComparisonCreateRequest, db: Session = Depends(get_db)):
    try:
        report = qp.create_comparison_report(db, payload)
    except qp.PolicyNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except qp.PolicyValidationFailure as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return qp.report_detail(report)


@router.get("/quality-comparisons", response_model=List[ComparisonSummaryResponse], tags=["质量策略对比"])
def list_comparisons(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    return qp.list_reports(db, limit=limit, offset=skip)


@router.get("/quality-comparisons/{report_id}", tags=["质量策略对比"])
def get_comparison(report_id: int, db: Session = Depends(get_db)):
    try:
        report = qp.get_report(db, report_id)
    except qp.PolicyNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    # 仅反序列化已固化内容，不重新查询或计算任何作业数据。
    return qp.report_detail(report)
