from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

WEIGHT_TOLERANCE = 1e-6


class StrategyScope(BaseModel):
    """策略适用范围；留空的维度表示不限制，全部留空即全局适用。"""

    robot_model_id: Optional[int] = Field(None, gt=0, description="机型ID")
    scene_id: Optional[int] = Field(None, gt=0, description="场景ID")
    skill_id: Optional[int] = Field(None, gt=0, description="技能ID")


class StrategyThresholds(BaseModel):
    """等级阈值：score >= A 得 A，依次类推，低于 C 得 D。"""

    A: float = Field(0.9, ge=0, le=1, description="A级下限")
    B: float = Field(0.7, ge=0, le=1, description="B级下限")
    C: float = Field(0.5, ge=0, le=1, description="C级下限")

    @model_validator(mode="after")
    def check_order(self):
        if not (self.A > self.B > self.C):
            raise ValueError("阈值必须满足 A > B > C")
        return self


def _validate_weights(completeness_weight: float, annotation_weight: float) -> None:
    if abs(completeness_weight + annotation_weight - 1.0) > WEIGHT_TOLERANCE:
        raise ValueError("完整度权重和标注质量权重之和必须为1.0")


def _validate_effective_at(effective_at: Optional[datetime]) -> None:
    if effective_at is not None and effective_at.tzinfo is None:
        raise ValueError("生效时间必须带时区")


class StrategyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="策略名称（同一名称下版本递增）")
    version: Optional[int] = Field(None, ge=1, description="版本号，缺省时自动取该名称下最大版本+1")
    completeness_weight: float = Field(0.5, ge=0, le=1, description="完整度权重")
    annotation_weight: float = Field(0.5, ge=0, le=1, description="标注质量权重")
    thresholds: StrategyThresholds = Field(default_factory=StrategyThresholds)
    scope: StrategyScope = Field(default_factory=StrategyScope, description="适用范围，空为全局")
    effective_at: Optional[datetime] = Field(None, description="生效时间，激活时缺省取当前时间")
    note: Optional[str] = Field(None, description="变更说明")
    created_by: Optional[str] = Field(None, max_length=100, description="创建人")

    @model_validator(mode="after")
    def check_payload(self):
        _validate_weights(self.completeness_weight, self.annotation_weight)
        _validate_effective_at(self.effective_at)
        return self


class StrategyUpdate(BaseModel):
    completeness_weight: Optional[float] = Field(None, ge=0, le=1)
    annotation_weight: Optional[float] = Field(None, ge=0, le=1)
    thresholds: Optional[StrategyThresholds] = None
    scope: Optional[StrategyScope] = None
    effective_at: Optional[datetime] = None
    note: Optional[str] = None

    @model_validator(mode="after")
    def check_payload(self):
        weights = [self.completeness_weight, self.annotation_weight]
        if all(item is not None for item in weights):
            _validate_weights(self.completeness_weight, self.annotation_weight)
        _validate_effective_at(self.effective_at)
        return self


class StrategyResponse(BaseModel):
    id: int
    name: str
    version: int
    status: str
    completeness_weight: float
    annotation_weight: float
    thresholds: Dict[str, float]
    scope: Dict[str, Any]
    effective_at: Optional[datetime] = None
    note: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    activated_at: Optional[datetime] = None
    retired_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class StrategyActivateRequest(BaseModel):
    actor: Optional[str] = Field(None, max_length=100, description="操作人")


class StrategyActivateResponse(BaseModel):
    strategy: StrategyResponse
    retired_strategy_ids: List[int] = Field(default_factory=list, description="因范围重叠被自动下线的策略")


class StrategyAuditResponse(BaseModel):
    id: int
    strategy_id: int
    action: str
    actor: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None
    created_at: datetime

    class Config:
        from_attributes = True


class StrategyComparisonCreate(BaseModel):
    base_strategy_id: int = Field(..., description="基准策略ID（草稿或已发布均可）")
    candidate_strategy_id: int = Field(..., description="候选策略ID")
    operation_ids: Optional[List[int]] = Field(
        None, description="显式指定作业ID列表；缺省时按过滤条件取样，空列表表示空样本"
    )
    robot_model_id: Optional[int] = Field(None, gt=0, description="按机型过滤取样")
    scene_id: Optional[int] = Field(None, gt=0, description="按场景过滤取样")
    skill_id: Optional[int] = Field(None, gt=0, description="按技能过滤取样")
    dataset_id: Optional[int] = Field(None, gt=0, description="只取某数据集包含的作业")
    boundary_margin: float = Field(0.05, ge=0, le=0.5, description="边界样本判定边距")
    note: Optional[str] = Field(None, description="报告备注")
    created_by: Optional[str] = Field(None, max_length=100, description="创建人")
