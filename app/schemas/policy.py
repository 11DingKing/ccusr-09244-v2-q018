"""质量策略版本与只读比较接口的请求/响应模型。"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 策略版本
# ---------------------------------------------------------------------------

class PolicyWeights(BaseModel):
    completeness: float = Field(..., ge=0, le=1, description="完整度权重")
    annotation: float = Field(..., ge=0, le=1, description="标注质量权重")


class PolicyThresholds(BaseModel):
    grade_a: float = Field(..., ge=0, le=1, description="A级下界（含）")
    grade_b: float = Field(..., ge=0, le=1, description="B级下界（含）")
    grade_c: float = Field(..., ge=0, le=1, description="C级下界（含）")


class PolicyScope(BaseModel):
    robot_model_id: Optional[int] = Field(None, description="机型限定，缺省表示全部机型")
    scene_id: Optional[int] = Field(None, description="场景限定，缺省表示全部场景")
    skill_id: Optional[int] = Field(None, description="技能限定，缺省表示全部技能")


class QualityPolicyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="策略名称")
    weights: PolicyWeights
    thresholds: PolicyThresholds
    scope: Optional[PolicyScope] = Field(None, description="适用范围，缺省表示全局")
    effective_at: datetime = Field(..., description="生效时间（需带时区）")
    note: Optional[str] = Field(None, description="版本说明")
    created_by: Optional[str] = Field(None, max_length=100, description="创建人")


class QualityPolicyUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    weights: PolicyWeights
    thresholds: PolicyThresholds
    scope: Optional[PolicyScope] = None
    effective_at: datetime
    note: Optional[str] = None
    expected_content_hash: Optional[str] = Field(
        None, description="乐观锁：提交时携带的草稿内容哈希，不匹配返回409"
    )


class QualityPolicyResponse(BaseModel):
    id: int
    version: int
    name: str
    status: str
    weights: Dict[str, float]
    thresholds: Dict[str, float]
    scope: Dict[str, Any]
    effective_at: datetime
    content_hash: str
    activated_at: Optional[datetime] = None
    deactivated_at: Optional[datetime] = None
    superseded_by: Optional[int] = None
    created_by: Optional[str] = None
    note: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class PolicyActivateRequest(BaseModel):
    force_replace: bool = Field(
        False, description="新范围完全覆盖现存策略时，是否强制取代旧版本；部分重叠仍会拒绝"
    )
    actor: Optional[str] = Field(None, max_length=100, description="操作人")


class PolicyAuditLogResponse(BaseModel):
    id: int
    policy_id: int
    action: str
    detail: Optional[Dict[str, Any]] = None
    actor: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# 只读比较报告
# ---------------------------------------------------------------------------

class ComparisonCreateRequest(BaseModel):
    left_policy_id: int = Field(..., description="基线策略版本ID（通常为现网版本）")
    right_policy_id: int = Field(..., description="候选策略版本ID（通常为新草稿）")
    operation_ids: Optional[List[int]] = Field(
        None, description="显式指定作业集合；传空列表表示空样本；缺省则按过滤条件选取"
    )
    dataset_id: Optional[int] = Field(None, description="按数据集选取作业")
    robot_model_id: Optional[int] = None
    scene_id: Optional[int] = None
    skill_id: Optional[int] = None
    boundary_tolerance: float = Field(
        0.02, ge=0, le=0.2, description="边界样本判定：质量分距任一版本阈值的容忍带"
    )
    created_by: Optional[str] = Field(None, max_length=100)


class ComparisonSummaryResponse(BaseModel):
    id: int
    left_policy_id: int
    right_policy_id: int
    baseline_label: str
    candidate_label: str
    sample_count: int
    changed_count: int
    boundary_count: int
    snapshot_hash: str
    created_by: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True
