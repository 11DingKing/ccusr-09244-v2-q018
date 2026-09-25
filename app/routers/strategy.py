from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ScoringStrategy
from app.schemas.strategy import (
    StrategyActivateRequest,
    StrategyActivateResponse,
    StrategyAuditResponse,
    StrategyComparisonCreate,
    StrategyCreate,
    StrategyResponse,
    StrategyUpdate,
)
from app.services import strategy as strategy_service

router = APIRouter()


@router.post("/strategies", response_model=StrategyResponse, status_code=201, tags=["评分策略管理"])
def create_strategy(payload: StrategyCreate, db: Session = Depends(get_db)):
    """创建策略草稿：保存权重、阈值、适用范围与生效时间。"""
    return strategy_service.create_strategy(db, payload)


@router.get("/strategies", response_model=List[StrategyResponse], tags=["评分策略管理"])
def list_strategies(
    name: Optional[str] = Query(None, description="按策略名称过滤"),
    status: Optional[str] = Query(None, description="按状态过滤：draft/active/retired"),
    db: Session = Depends(get_db),
):
    query = db.query(ScoringStrategy)
    if name:
        query = query.filter(ScoringStrategy.name == name)
    if status:
        query = query.filter(ScoringStrategy.status == status)
    return query.order_by(ScoringStrategy.name.asc(), ScoringStrategy.version.asc()).all()


@router.get("/strategies/{strategy_id}", response_model=StrategyResponse, tags=["评分策略管理"])
def get_strategy(strategy_id: int, db: Session = Depends(get_db)):
    return strategy_service.get_strategy(db, strategy_id)


@router.put("/strategies/{strategy_id}", response_model=StrategyResponse, tags=["评分策略管理"])
def update_strategy(
    strategy_id: int,
    payload: StrategyUpdate,
    actor: Optional[str] = Query(None, description="操作人"),
    db: Session = Depends(get_db),
):
    """编辑草稿；已发布或已下线的策略不可修改。"""
    return strategy_service.update_draft(db, strategy_id, payload, actor)


@router.post("/strategies/{strategy_id}/activate", response_model=StrategyActivateResponse, tags=["评分策略管理"])
def activate_strategy(
    strategy_id: int,
    payload: StrategyActivateRequest,
    db: Session = Depends(get_db),
):
    """发布（激活）草稿：范围重叠的生效版本会在同一事务内自动下线并留痕。"""
    strategy, retired_ids = strategy_service.activate_strategy(db, strategy_id, payload.actor)
    return StrategyActivateResponse(
        strategy=StrategyResponse.model_validate(strategy),
        retired_strategy_ids=retired_ids,
    )


@router.post("/strategies/{strategy_id}/rollback", response_model=StrategyActivateResponse, tags=["评分策略管理"])
def rollback_strategy(
    strategy_id: int,
    payload: StrategyActivateRequest,
    db: Session = Depends(get_db),
):
    """回滚到已下线版本：重新激活它，并下线当前范围重叠的生效版本。"""
    strategy, retired_ids = strategy_service.rollback_strategy(db, strategy_id, payload.actor)
    return StrategyActivateResponse(
        strategy=StrategyResponse.model_validate(strategy),
        retired_strategy_ids=retired_ids,
    )


@router.get("/strategy-audits", response_model=List[StrategyAuditResponse], tags=["评分策略管理"])
def list_strategy_audits(
    strategy_id: Optional[int] = Query(None, description="按策略ID过滤"),
    db: Session = Depends(get_db),
):
    return strategy_service.list_audits(db, strategy_id)


@router.post("/strategy-comparisons", status_code=201, tags=["策略对比"])
def create_strategy_comparison(payload: StrategyComparisonCreate, db: Session = Depends(get_db)):
    """在同一组作业上只读对比两个策略版本（草稿亦可），报告绑定输入快照。"""
    report = strategy_service.run_comparison(db, payload)
    return strategy_service.comparison_to_dict(report)


@router.get("/strategy-comparisons", tags=["策略对比"])
def list_strategy_comparisons(db: Session = Depends(get_db)):
    reports = strategy_service.list_comparisons(db)
    return [
        {
            "id": report.id,
            "created_at": report.created_at.isoformat() if report.created_at else None,
            "created_by": report.created_by,
            "note": report.note,
            "base_strategy_id": report.base_strategy_id,
            "candidate_strategy_id": report.candidate_strategy_id,
            "sample_count": report.sample_count,
            "input_digest": report.input_digest,
            "changed_count": report.result["migration"]["changed_count"],
        }
        for report in reports
    ]


@router.get("/strategy-comparisons/{report_id}", tags=["策略对比"])
def get_strategy_comparison(report_id: int, db: Session = Depends(get_db)):
    """读取已保存的对比报告；只返回落库内容，新增数据不会改变报告。"""
    report = strategy_service.get_comparison(db, report_id)
    return strategy_service.comparison_to_dict(report)
