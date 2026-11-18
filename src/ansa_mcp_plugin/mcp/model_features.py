"""MCP schema for controlled recognition of explicit ANSA Part Features."""

from typing import Annotated, Literal

from mcp.types import CallToolResult
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from ansa_mcp_shared.feature_recognition import (
    RecognitionInputKind,
    ModellingMode,
    RECOGNITION_OPTIONS,
    RecognizeFeatureType,
    validate_recognition_request,
)

from .schemas import ExpectedModel, ExpectedTarget, ToolError, ToolResult, _forbid_extra_arguments


OperationId = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
Deck = Annotated[int, Field(strict=True, ge=1)]
PartId = Annotated[int, Field(strict=True, ge=1)]
TTL = Annotated[int, Field(strict=True, ge=0, le=86400)]


def _unique_values(value):
    if len(set(value)) != len(value):
        raise ValueError("values must not contain duplicates")
    return value


def _nonblank_name(value):
    if not value.strip():
        raise ValueError("names must not be blank")
    return value


PartIds = Annotated[
    list[PartId],
    Field(strict=True, min_length=1, max_length=100),
    AfterValidator(_unique_values),
]
RecognizeFeatureTypes = Annotated[
    list[RecognizeFeatureType],
    Field(strict=True, min_length=1, max_length=len(RECOGNITION_OPTIONS)),
    AfterValidator(_unique_values),
]
RecognitionNumber = int | float
ExpectedPlanSha256 = Annotated[
    str,
    Field(strict=True, pattern=r"^[0-9a-f]{64}$"),
]
RecognizeBackupPath = Annotated[
    str,
    Field(strict=True, min_length=1),
    AfterValidator(_nonblank_name),
]


class RecognitionParameters(BaseModel):
    """Explicit native-recognition thresholds; no threshold has an operational default."""

    model_config = ConfigDict(extra="forbid", strict=True)

    fillet_max_radius: Annotated[
        RecognitionNumber, Field(gt=0, allow_inf_nan=False)
    ] | None = None
    fillet_min_angle: Annotated[
        RecognitionNumber, Field(ge=0, lt=180, allow_inf_nan=False)
    ] | None = None
    chamfer_max_width: Annotated[
        RecognitionNumber, Field(gt=0, allow_inf_nan=False)
    ] | None = None
    chamfer_min_angle: Annotated[
        RecognitionNumber, Field(ge=0, lt=180, allow_inf_nan=False)
    ] | None = None
    chamfer_max_angle: Annotated[
        RecognitionNumber, Field(gt=0, lt=180, allow_inf_nan=False)
    ] | None = None
    hole_max_diameter: Annotated[
        RecognitionNumber, Field(gt=0, allow_inf_nan=False)
    ] | None = None
    special_fe_break_angle: Annotated[
        RecognitionNumber, Field(ge=0, lt=180, allow_inf_nan=False)
    ] | None = None
    special_geom_break_angle: Annotated[
        RecognitionNumber, Field(ge=0, lt=180, allow_inf_nan=False)
    ] | None = None
    special_sharp: Annotated[bool, Field(strict=True)] | None = None
    stamp_max_height: Annotated[
        RecognitionNumber, Field(gt=0, allow_inf_nan=False)
    ] | None = None
    stamp_max_size: Annotated[
        RecognitionNumber, Field(gt=0, allow_inf_nan=False)
    ] | None = None
    flange_2d_min_width: Annotated[
        RecognitionNumber, Field(ge=0, allow_inf_nan=False)
    ] | None = None
    flange_2d_max_width: Annotated[
        RecognitionNumber, Field(gt=0, allow_inf_nan=False)
    ] | None = None
    logo_max_height: Annotated[
        RecognitionNumber, Field(gt=0, allow_inf_nan=False)
    ] | None = None
    logo_max_size: Annotated[
        RecognitionNumber, Field(gt=0, allow_inf_nan=False)
    ] | None = None
    logo_allow_base_fillets: Annotated[bool, Field(strict=True)] | None = None
    logo_base_fillet_min_angle: Annotated[
        RecognitionNumber, Field(ge=0, lt=180, allow_inf_nan=False)
    ] | None = None
    fastener_embedded_bolts: Annotated[bool, Field(strict=True)] | None = None
    fastener_embedded_clips: Annotated[bool, Field(strict=True)] | None = None


def register(server, submit):
    def reject_recognize(message, operation_id, expected_target, expected_model):
        return ToolResult(
            ok=False,
            status="failed",
            operation_id=operation_id,
            target=expected_target,
            model=expected_model.model_dump(mode="json"),
            error=ToolError(code="MODEL_FEATURES_RECOGNIZE_REJECTED", message=message),
        ).as_mcp()

    @server.tool(name="ansa_model_features_recognize", structured_output=True)
    def ansa_model_features_recognize(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck,
        part_ids: PartIds,
        feature_types: RecognizeFeatureTypes,
        parameters: RecognitionParameters,
        action: Literal["plan", "apply"] = "plan",
        expected_plan_sha256: ExpectedPlanSha256 | None = None,
        backup_path: RecognizeBackupPath | None = None,
        ttl_seconds: TTL = 300,
        modelling: ModellingMode | None = None,
        input_kind: RecognitionInputKind = "cad",
    ) -> Annotated[CallToolResult, ToolResult]:
        """受控规划或应用当前 NASTRAN 显式 Part 的原生特征识别。

        action=plan 默认执行只读识别计划，用于核对所选类型、阈值和计划 SHA；不得携带
        expected_plan_sha256 或 backup_path。action=apply 必须同时携带计划 SHA 和新备份路径，
        实际写入前核验备份，已有 Feature 保留。

        共享契约覆盖 FILLET、CHAMFER、HOLE 2D、SPECIAL PERIMETER、STAMP、FLANGE 2D、
        RIB 2D、LOGO、HOLE 3D、FLANGE 3D、FASTENER 共 11 类。未提供 modelling 时仅接受
        前三个兼容类别；其他类别必须显式指定 Shell 或 Solid，类别须符合所选模式。
        所选类别要求共享映射中的完整参数组并禁止多余参数，无参数类别可使用 parameters={}。

        默认识别 NASTRAN CAD Part。Shell 模式仅选 RIB 2D 时，可显式指定 input_kind=shell_mesh
        识别只含壳单元且无 CAD FACE 的 Part。保持已有 FE 周界锁定，ANSA 可新建所选范围内的
        FE 周界。FASTENER 依赖 Design Manager（DM）连接，公开端到端验收暂缓。应用会恢复临时识别配置；超时或断线后先查询原
        operation_id，不更换键重放。缺失或无效的跨字段组合会直接拒绝，不提交识别任务。
        """
        if action == "plan":
            if expected_plan_sha256 is not None or backup_path is not None:
                return reject_recognize(
                    "plan does not accept expected_plan_sha256 or backup_path",
                    operation_id,
                    expected_target,
                    expected_model,
                )
        elif expected_plan_sha256 is None or backup_path is None:
            return reject_recognize(
                "apply requires expected_plan_sha256 and backup_path",
                operation_id,
                expected_target,
                expected_model,
            )

        try:
            parameter_values = validate_recognition_request(
                feature_types,
                parameters.model_dump(exclude_none=True),
                modelling,
                input_kind=input_kind,
            )
        except ValueError as exc:
            return reject_recognize(
                str(exc),
                operation_id,
                expected_target,
                expected_model,
            )

        params = {
            "deck": deck,
            "part_ids": part_ids,
            "feature_types": feature_types,
            "parameters": parameter_values,
        }
        if modelling is not None:
            params["modelling"] = modelling
        if input_kind != "cad":
            params["input_kind"] = input_kind
        if action == "plan":
            return submit(
                "model_features_recognize_plan",
                params,
                operation_id,
                expected_target,
                expected_model,
                ttl_seconds,
            )
        return submit(
            "model_features_recognize",
            {
                **params,
                "expected_plan_sha256": expected_plan_sha256,
                "backup_path": backup_path,
            },
            operation_id,
            expected_target,
            expected_model,
            ttl_seconds,
        )

    _forbid_extra_arguments(server, "ansa_model_features_recognize")


__all__ = ["register"]
