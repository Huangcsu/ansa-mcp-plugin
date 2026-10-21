"""MCP schemas for explicitly scoped mesh-quality report, locate and repair."""

import math
from pathlib import Path
from typing import Annotated, Literal

from mcp.types import CallToolResult
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from ansa_mcp_shared.quality_templates import get_template

from .schemas import ExpectedModel, ExpectedTarget, ToolResult, _forbid_extra_arguments


OperationId = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
Deck = Annotated[int, Field(strict=True, ge=1)]
EntityId = Annotated[int, Field(strict=True, ge=1)]
TTL = Annotated[int, Field(strict=True, ge=0, le=86400)]
ScopeSha256 = Annotated[str, Field(strict=True, pattern=r"^[0-9a-fA-F]{64}$")]

CriterionName = Literal[
    "ASPECT",
    "WARP",
    "SKEW",
    "JACOB",
    "MINL",
    "MAXL",
    "CRASH",
    "TETRA_COLLAPSE",
]


def _unique_entity_ids(value):
    if len(value) != len(set(value)):
        raise ValueError("entity_ids must be unique")
    return value


def _finite_float(value):
    if type(value) not in (int, float):
        raise ValueError("must be a finite number")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ValueError("must be a finite number") from None
    if not math.isfinite(number):
        raise ValueError("must be a finite number")
    return number


def _positive_float(value):
    value = _finite_float(value)
    if value <= 0:
        raise ValueError("must be a positive finite float")
    return value


FiniteFloat = Annotated[float, BeforeValidator(_finite_float)]
PositiveFloat = Annotated[float, BeforeValidator(_positive_float)]


def _unique_criterion_names(value):
    names = [item.name for item in value]
    if len(names) != len(set(names)):
        raise ValueError("criteria names must be unique")
    return value


def _absolute_path(value):
    if not Path(value).is_absolute():
        raise ValueError("backup_path must be an absolute path")
    return value


EntityIds = Annotated[
    list[EntityId],
    Field(strict=True, min_length=1),
    AfterValidator(_unique_entity_ids),
]
LocateEntityIds = Annotated[
    list[EntityId], Field(strict=True, min_length=1, max_length=2000), AfterValidator(_unique_entity_ids),
]
RepairEntityIds = Annotated[
    list[EntityId],
    Field(strict=True, min_length=1, max_length=200),
    AfterValidator(_unique_entity_ids),
]
MovableNodeIds = Annotated[
    list[EntityId],
    Field(strict=True, min_length=1, max_length=2000),
    AfterValidator(_unique_entity_ids),
]
BackupPath = Annotated[
    str,
    Field(strict=True, min_length=1),
    AfterValidator(_absolute_path),
]


class QualityCriterion(BaseModel):
    """One explicit quality bound; omitted bounds stay absent from the bridge payload."""

    model_config = ConfigDict(extra="forbid")

    name: CriterionName
    min: FiniteFloat | None = None
    max: FiniteFloat | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null(cls, value):
        if isinstance(value, dict):
            for key in ("min", "max"):
                if key in value and value[key] is None:
                    raise ValueError(f"{key} must be a finite number when provided")
        return value

    @model_validator(mode="after")
    def _require_bound(self):
        if self.min is None and self.max is None:
            raise ValueError("criterion must define min, max, or both")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min must not be greater than max")
        return self


Criteria = Annotated[
    list[QualityCriterion],
    Field(strict=True, min_length=1, max_length=16),
    AfterValidator(_unique_criterion_names),
]


class QualityProfile(BaseModel):
    """Explicit analysis profile; thresholds come only from the caller."""

    model_config = ConfigDict(extra="forbid")

    analysis_type: Literal["linear", "explicit_crash"]
    element_family: Literal["shell", "solid"]
    criteria: Criteria
    minimum_length: PositiveFloat | None = None
    wave_speed: PositiveFloat | None = None
    target_time_step: PositiveFloat | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null(cls, value):
        if isinstance(value, dict):
            for key in ("minimum_length", "wave_speed", "target_time_step"):
                if key in value and value[key] is None:
                    raise ValueError(f"{key} must be a positive finite number when provided")
        return value

    @model_validator(mode="after")
    def _analysis_parameters(self):
        if self.analysis_type == "linear":
            if any(v is not None for v in (self.minimum_length, self.wave_speed, self.target_time_step)):
                raise ValueError("linear profiles use an explicit MINL criterion")
        elif self.minimum_length is None:
            raise ValueError("explicit_crash requires minimum_length")
        if (self.wave_speed is None) != (self.target_time_step is None):
            raise ValueError("wave_speed and target_time_step must be provided together")
        return self


NativeTemplate = Literal[
    "linear_shell",
    "linear_solid",
    "explicit_crash_shell",
    "explicit_crash_solid",
]


def _native_criteria_text(value):
    if not value.strip() or len(value.encode("utf-8")) > 65536:
        raise ValueError("quality_criteria must be non-empty UTF-8 within 65536 bytes")
    return value


NativeQualityCriteria = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        max_length=65536,
        description="完整的 ANSA .ansa_qual 文本；省略时沿用当前会话准则。",
    ),
    AfterValidator(_native_criteria_text),
]
CriteriaLabel = Annotated[str, Field(strict=True, max_length=128)]
Formulation = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        max_length=64,
        description="调用方提供的元数据，不代表已核验该单元公式。",
    ),
]


class NativeQualityProfile(BaseModel):
    """引用原生 ANSA 准则的模板 profile，不接收 legacy 阈值字段。"""

    model_config = ConfigDict(extra="forbid")

    template: NativeTemplate
    quality_criteria: NativeQualityCriteria | None = None
    criteria_label: CriteriaLabel = "current_session"
    target_time_step: PositiveFloat | None = None
    formulation: Formulation | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null(cls, value):
        if isinstance(value, dict):
            for key in ("quality_criteria", "criteria_label", "target_time_step", "formulation"):
                if key in value and value[key] is None:
                    raise ValueError(f"{key} must be omitted instead of null")
        return value

    @model_validator(mode="after")
    def _analysis_parameters(self):
        analysis_type = get_template(self.template)["analysis_type"]
        if analysis_type == "linear" and self.target_time_step is not None:
            raise ValueError("target_time_step is forbidden for linear templates")
        if analysis_type == "explicit_crash" and self.target_time_step is None:
            raise ValueError("explicit_crash templates require target_time_step")
        return self


def register(server, submit):
    @server.tool(name="ansa_mesh_quality_report", structured_output=True)
    def ansa_mesh_quality_report(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck,
        profile: QualityProfile | NativeQualityProfile,
        entity_ids: EntityIds | None = None,
        ttl_seconds: TTL = 300,
        scope: Literal["all", "visible"] | None = None,
        paged: bool = False,
        batch_size: Annotated[int, Field(strict=True, ge=1, le=2000)] = 1000,
        max_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)] = 120,
        max_report_megabytes: Annotated[int, Field(strict=True, ge=1, le=4096)] = 256,
    ) -> Annotated[CallToolResult, ToolResult]:
        """只读检查网格质量；总单元数不设固定上限。

        原生 profile 选择四模板，以当前会话或 quality_criteria 中的 ANSA 原生准则
        执行 MeshQuality；缺必检项不通过。legacy profile 继续读取调用方显式上下界。
        缺失、None 或无法读取的质量指标只记入 unknown，不能按 0 处理，也不能据此
        判定通过。操作不调用 try_fix、不改变选择或可见性、不写入模型文件。
        即使原生返回 0 个失败实体，也不代表全模型或未检查准则通过。
        entity_ids 与 scope 二选一。scope=all/visible、paged=true 或超过2000个单元时，
        内部按 batch_size 分批，返回 report_id、覆盖范围与摘要；使用
        ansa_mesh_quality_page 分页读取明细。时间或存储预算耗尽返回 complete=false。
        大范围报告不能直接用作修改许可；修复前须对最多200个目标重新取得报告。
        """
        if (entity_ids is None) == (scope is None):
            raise ValueError("provide exactly one of entity_ids or scope")
        params = {"deck": deck, "profile": profile.model_dump(exclude_none=True),
                  "paged": paged, "batch_size": batch_size, "max_seconds": max_seconds,
                  "max_report_megabytes": max_report_megabytes}
        params.update({"entity_ids": entity_ids} if entity_ids is not None else {"scope": scope})
        return submit(
            "quality_report",
            params,
            operation_id,
            expected_target,
            expected_model,
            ttl_seconds,
        )

    _forbid_extra_arguments(server, "ansa_mesh_quality_report")

    @server.tool(name="ansa_mesh_quality_page", structured_output=True)
    def ansa_mesh_quality_page(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck,
        report_id: Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{32}$")],
        offset: Annotated[int, Field(strict=True, ge=0)] = 0,
        limit: Annotated[int, Field(strict=True, ge=1, le=200)] = 100,
        status: Literal["all", "failed", "unknown"] = "failed",
        criterion: Annotated[str, Field(strict=True, min_length=1, max_length=128)] | None = None,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """只读分页查询同一会话／模型代次的质量报告快照；不代表当前模型已重新检查。"""
        params = {"deck": deck, "report_id": report_id, "offset": offset, "limit": limit, "status": status}
        if criterion is not None:
            params["criterion"] = criterion
        return submit("quality_page", params, operation_id, expected_target, expected_model, ttl_seconds)

    _forbid_extra_arguments(server, "ansa_mesh_quality_page")

    @server.tool(name="ansa_mesh_quality_locate", structured_output=True)
    def ansa_mesh_quality_locate(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck,
        entity_ids: LocateEntityIds,
        profile: QualityProfile | NativeQualityProfile,
        expected_scope_sha256: ScopeSha256,
        ttl_seconds: TTL = 300,
        criterion: Annotated[str, Field(strict=True, min_length=1, max_length=128)] | None = None,
    ) -> Annotated[CallToolResult, ToolResult]:
        """按质量报告定位失败实体；这是显示写入，不修改模型或质量阈值。

        expected_scope_sha256 必须来自同一显式范围的最新质量报告，范围变化时拒绝
        定位。缺失或未知指标不能按 0 处理，也不能被定位为通过。操作只改变当前
        显示/选择状态，并在完成后回读定位范围；失败时先查询原任务再决定是否重试。
        criterion 可选，须精确匹配报告中的激活准则名（原生如 aspect ratio，旧配置如
        ASPECT）；省略则定位所有失败单元。该准则无失败时保持当前显示。
        LS-DYNA 单窗口单模型仅开放 explicit_crash 原生模板的质量定位／受控修复；
        其他 LS-DYNA 写操作继续关闭。
        """
        return submit(
            "quality_locate",
            {
                "deck": deck,
                "entity_ids": entity_ids,
                "profile": profile.model_dump(exclude_none=True),
                "expected_scope_sha256": expected_scope_sha256,
                **({"criterion": criterion} if criterion is not None else {}),
            },
            operation_id,
            expected_target,
            expected_model,
            ttl_seconds,
        )

    _forbid_extra_arguments(server, "ansa_mesh_quality_locate")

    @server.tool(name="ansa_mesh_quality_repair", structured_output=True)
    def ansa_mesh_quality_repair(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck,
        entity_ids: RepairEntityIds,
        profile: QualityProfile | NativeQualityProfile,
        expected_scope_sha256: ScopeSha256,
        backup_path: BackupPath,
        maximum_displacement: PositiveFloat,
        movable_node_ids: MovableNodeIds | None = None,
        method: Literal["smooth", "fix_quality"] | None = None,
        ttl_seconds: TTL = 300,
        max_iterations: Annotated[int, Field(strict=True, ge=1, le=10)] = 1,
        maximum_total_displacement: PositiveFloat | None = None,
    ) -> Annotated[CallToolResult, ToolResult]:
        """对最多 200 个显式实体执行受控质量修复，并先创建独立模型备份。

        expected_scope_sha256 必须与最新质量报告一致。backup_path 是必须由 bridge
        核验的绝对路径；movable_node_ids 限定可移动节点，省略则排除范围外FE共用节点后
        自动推导。maximum_displacement 是
        单次允许的最大节点位移。method 显式指定时可选 smooth 或 fix_quality；省略时
        由 bridge 按 profile 类型选用兼容默认（legacy 为 smooth，native 为 fix_quality）。
        smooth 调用显式壳/实体平滑，先恢复范围内固定节点再检查质量，实体另冻结外表皮；
        fix_quality 使用自定义准则与 try_fix(fix_method=2)。核对拓扑、节点移动与
        质量复查；不自动切换修复方式或扩大范围。任务失败或结果未知时必须先
        查询原 operation_id/task_id 并独立回读，不能换键重放。
        LS-DYNA 仅支持 explicit_crash 原生模板的 QUAD4/HEXA8 受控修复，
        其他 LS-DYNA 写操作继续关闭；通用写入门控仍只支持 NASTRAN/ABAQUS。
        max_iterations>1 允许有界续修；每轮备份并复查，累计位移以本次开始坐标为准。
        maximum_total_displacement 缺省等于单次位移上限；无改善、异常或预算耗尽时停止。
        """
        params = {
            "deck": deck,
            "entity_ids": entity_ids,
            "profile": profile.model_dump(exclude_none=True),
            "expected_scope_sha256": expected_scope_sha256,
            "backup_path": backup_path,
            "maximum_displacement": maximum_displacement,
            "method": method or ("fix_quality" if isinstance(profile, NativeQualityProfile) else "smooth"),
            "max_iterations": max_iterations,
        }
        if movable_node_ids is not None:
            params["movable_node_ids"] = movable_node_ids
        if maximum_total_displacement is not None:
            params["maximum_total_displacement"] = maximum_total_displacement
        return submit(
            "quality_repair",
            params,
            operation_id,
            expected_target,
            expected_model,
            ttl_seconds,
        )

    _forbid_extra_arguments(server, "ansa_mesh_quality_repair")


__all__ = ["register", "QualityProfile", "NativeQualityProfile"]
