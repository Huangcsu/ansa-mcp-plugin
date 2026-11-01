"""Explicit contracts for bounded structured-mesh planning and evidence."""

from pathlib import Path
import math
from typing import Annotated, Any, Literal

from mcp.types import CallToolResult
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from .schemas import ExpectedModel, ExpectedTarget, ToolResult, _forbid_extra_arguments


OperationId = Annotated[str, Field(min_length=1, max_length=128)]
Deck = Annotated[int, Field(strict=True, ge=1)]
EntityId = Annotated[int, Field(strict=True, ge=1)]
Ids = Annotated[list[EntityId], Field(min_length=1, max_length=5000)]
SmallIds = Annotated[list[EntityId], Field(min_length=1, max_length=200)]
FaceIds = Annotated[list[EntityId], Field(min_length=1, max_length=100)]
OptionalIds = Annotated[list[EntityId], Field(max_length=200)]
Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Point = Annotated[list[Number], Field(min_length=3, max_length=3)]
Positive = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
OpaqueId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
TTL = Annotated[int, Field(strict=True, ge=0, le=86400)]
Facet = Annotated[list[EntityId], Field(min_length=3, max_length=4)]
Facets = Annotated[list[Facet], Field(max_length=50000)]
FeatureFaceIds = Annotated[list[EntityId], Field(max_length=20)]
FeatureCurveIds = Annotated[list[EntityId], Field(max_length=50)]
FeatureSegments = Annotated[int, Field(strict=True, ge=1, le=10000)]
BoundaryFaceIds = Annotated[list[EntityId], Field(min_length=1, max_length=20)]
BoundaryConsIds = Annotated[list[EntityId], Field(min_length=1, max_length=32)]

def _unique_ids(values):
    if len(values) != len(set(values)):
        raise ValueError("IDs must not contain duplicates")
    return values


SeedFaceIds = Annotated[
    list[EntityId], Field(min_length=1, max_length=20), AfterValidator(_unique_ids)
]
SeedConsIds = Annotated[
    list[EntityId], Field(min_length=1, max_length=32), AfterValidator(_unique_ids)
]
CadMapFaceIds = Annotated[
    list[EntityId],
    Field(min_length=6, max_length=6, json_schema_extra={"uniqueItems": True}),
    AfterValidator(_unique_ids),
]
CandidateFaceIds = Annotated[
    list[EntityId], Field(min_length=1, max_length=100), AfterValidator(_unique_ids)
]
MapBlockVolumeIds = Annotated[
    list[EntityId], Field(min_length=1, max_length=20), AfterValidator(_unique_ids)
]
MapBlockCapFaceIds = Annotated[
    list[EntityId], Field(min_length=1, max_length=100, json_schema_extra={"uniqueItems": True}),
    AfterValidator(_unique_ids),
]
AnnularPrepareFaceIds = Annotated[
    list[EntityId], Field(min_length=4, max_length=64, json_schema_extra={"uniqueItems": True}),
    AfterValidator(_unique_ids),
]
AnnularSourceFaceIds = Annotated[
    list[EntityId], Field(min_length=4, max_length=4, json_schema_extra={"uniqueItems": True}),
    AfterValidator(_unique_ids),
]


def _absolute_backup_path(value):
    if not Path(value).is_absolute():
        raise ValueError("backup_path must be absolute")
    return value


BackupPath = Annotated[
    str,
    Field(strict=True, min_length=1),
    AfterValidator(_absolute_backup_path),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ConsSeedAssignment(StrictModel):
    cons_id: EntityId
    segments: FeatureSegments


def _unique_cons_assignments(values):
    cons_ids = [assignment.cons_id for assignment in values]
    if len(cons_ids) != len(set(cons_ids)):
        raise ValueError("assignments must not contain duplicate cons_id values")
    return values


ConsSeedAssignments = Annotated[
    list[ConsSeedAssignment],
    Field(min_length=1, max_length=32),
    AfterValidator(_unique_cons_assignments),
]


class SeedCountAssignment(StrictModel):
    cons_id: EntityId
    count: FeatureSegments


class SeedAlignTarget(StrictModel):
    cons_id: EntityId
    start_hot_point_id: EntityId


def _unique_seed_count_assignments(values):
    cons_ids = [assignment.cons_id for assignment in values]
    if len(cons_ids) != len(set(cons_ids)):
        raise ValueError("assignments must not contain duplicate cons_id values")
    return values


def _unique_seed_align_targets(values):
    cons_ids = [target.cons_id for target in values]
    if len(cons_ids) != len(set(cons_ids)):
        raise ValueError("targets must not contain duplicate cons_id values")
    return values


SeedCountAssignments = Annotated[
    list[SeedCountAssignment],
    Field(min_length=1, max_length=32),
    AfterValidator(_unique_seed_count_assignments),
]
SeedAlignTargets = Annotated[
    list[SeedAlignTarget],
    Field(min_length=1, max_length=31),
    AfterValidator(_unique_seed_align_targets),
]


class NumberSeedEdit(StrictModel):
    action: Literal["number"]
    expected_scope_sha256: Digest
    count_type: Literal["edges", "nodes"]
    assignments: SeedCountAssignments

    @model_validator(mode="after")
    def _nodes_require_two_points(self):
        if self.count_type == "nodes" and any(
            assignment.count < 2 for assignment in self.assignments
        ):
            raise ValueError("node counts must be at least 2")
        return self


class LengthSeedEdit(StrictModel):
    action: Literal["length"]
    expected_scope_sha256: Digest
    cons_ids: SeedConsIds
    target_length: Positive


class AlignSeedEdit(StrictModel):
    action: Literal["align"]
    expected_scope_sha256: Digest
    source_cons_id: EntityId
    source_start_hot_point_id: EntityId
    targets: SeedAlignTargets

    @model_validator(mode="after")
    def _source_not_targeted(self):
        if any(target.cons_id == self.source_cons_id for target in self.targets):
            raise ValueError("source_cons_id must not appear in targets")
        return self


SeedEdit = Annotated[
    NumberSeedEdit | LengthSeedEdit | AlignSeedEdit,
    Field(discriminator="action"),
]


class Interface(StrictModel):
    id: Annotated[str, Field(min_length=1, max_length=128)]
    blocks: Annotated[list[str], Field(min_length=2, max_length=2)]
    facets: Facets


class Cylinder(StrictModel):
    label: Annotated[str, Field(min_length=1, max_length=128)]
    center: Point
    axis: Point
    radius: Positive
    tolerance: Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
    facets: Facets


class Plane(StrictModel):
    origin: Point
    normal: Point


class FaceCylinder(StrictModel):
    label: Annotated[str, Field(min_length=1, max_length=128)]
    face_ids: FaceIds
    center: Point
    axis: Point
    radius: Positive
    tolerance: Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]


class CadBoundary(StrictModel):
    face_ids: FaceIds
    tolerance: Positive
    cylinders: Annotated[list[FaceCylinder], Field(max_length=100)] = Field(default_factory=list)


class SweepBlock(StrictModel):
    id: Annotated[str, Field(min_length=1, max_length=128)]
    shell_ids: SmallIds
    direction: Point
    distance: Positive
    steps: Annotated[int, Field(strict=True, ge=1, le=100)]
    property_id: EntityId


class AnnularSourceOptions(StrictModel):
    center: Point
    axis: Point
    inner_radius: Positive
    outer_radius: Positive
    max_chord_error: Positive
    tolerance: Annotated[float, Field(strict=True, gt=0, le=0.001, allow_inf_nan=False)]


class GeometrySeedOptions(StrictModel):
    target_length: Positive
    max_chord_error: Positive = 0.02
    min_segments: FeatureSegments = 1
    multiple: FeatureSegments = 1
    max_segments: FeatureSegments = 10000


class GeometrySweepOptions(StrictModel):
    source_face_id: EntityId
    target_face_id: EntityId
    direction: Point
    distance: Positive
    tolerance: Annotated[float, Field(strict=True, gt=0, le=0.001, allow_inf_nan=False)] = 1e-7


def register(server, submit):
    @server.tool(structured_output=True)
    def ansa_mesh_seed_plan(
        operation_id: OperationId, expected_target: ExpectedTarget,
        expected_model: ExpectedModel, deck: Deck, plan: dict[str, Any],
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """根据显式边长/目标尺寸、圆弧半径及弦误差建议整数分段，协调共享边等分组。

        源面网格生成前使用；输出可复用的seed_constraints，不读取或修改原生边分段。
        几何尺寸来自调用方声明，不自动识别任意几何；超预算保留incomplete而不截断。
        """
        return submit("mesh_seed_plan", {"deck":deck,"plan":plan},
                      operation_id,expected_target,expected_model,ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_mesh_sweep_prepare(
        operation_id: OperationId, expected_target: ExpectedTarget,
        expected_model: ExpectedModel, deck: Deck,
        blocks: Annotated[list[SweepBlock], Field(min_length=1,max_length=32)],
        max_penta_fraction: Annotated[float, Field(strict=True,ge=0,le=1,allow_inf_nan=False)] = 1.0,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """一次读取最多200个源SHELL，预检多个已就绪块并合并兼容的直线扫掠。

        拒绝共享GRID被分开扫掠、重复源面和CPENTA比例超限。通过后逐组调用mesh_sweep，
        为每组提供独立备份路径和operation_id；写前指纹核验仍由mesh_sweep重新执行。
        不创建未来源面，不证明目标域无交叠，不自动分割CAD或执行模型写入。
        """
        return submit("mesh_sweep_prepare", {"deck":deck,"blocks":[b.model_dump() for b in blocks],
                      "max_penta_fraction":max_penta_fraction},
                      operation_id,expected_target,expected_model,ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_mesh_interface_audit(
        operation_id: OperationId, expected_target: ExpectedTarget,
        expected_model: ExpectedModel, deck: Deck, entity_ids: Ids,
        interfaces: Annotated[list[Interface], Field(max_length=500)] | None = None,
        protected_cylinders: Annotated[list[Cylinder], Field(max_length=100)] | None = None,
        expected_boundary: Facets | None = None,
        expected_components: Annotated[int, Field(strict=True, ge=1, le=50000)] = 1,
        cad_boundary: CadBoundary | None = None,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """只读审计明确 NASTRAN CHEXA8/CPENTA6。孔壁由显式面片归属后全部测量。

        期望边界为整个显式范围的外皮全集；块归属或CAD覆盖未知时返回incomplete。
        拓扑、几何、质量与再生状态分别报告；不会由连接组件数推断完整几何合格。
        完整证据存于所属私有会话，使用ansa_mesh_evidence_page分页；历史证据不授权修改。
        cad_boundary 从原生 FACE 宏的 SHELL 归属取得成员，再测量全部节点；不可与调用方
        expected_boundary/protected_cylinders 混用。节点投影通过不等于连续 CAD 覆盖证明。
        """
        return submit("mesh_interface_audit", {
            "deck": deck, "entity_ids": entity_ids,
            "interfaces": None if interfaces is None else [x.model_dump() for x in interfaces],
            "protected_cylinders": None if protected_cylinders is None else [x.model_dump() for x in protected_cylinders],
            "expected_boundary": expected_boundary, "expected_components": expected_components,
            "cad_boundary": None if cad_boundary is None else cad_boundary.model_dump(),
        }, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_mesh_block_plan(
        operation_id: OperationId, expected_target: ExpectedTarget,
        expected_model: ExpectedModel, deck: Deck, plan: dict[str, Any],
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """只读检查显式直线扫掠块、依赖图与整数边分段约束，输出确定性执行顺序。

        plan结构见structured-meshing契约；不自动识别任意几何，不修改模型或生成体单元。
        调用方提供的数据会标记来源，计划通过不能代替当前源面及几何的原生核验。
        """
        return submit("mesh_block_plan", {"deck": deck, "plan": plan},
                      operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_mesh_source_audit(
        operation_id: OperationId, expected_target: ExpectedTarget,
        expected_model: ExpectedModel, deck: Deck, source_ids: SmallIds,
        direction: Point, layers: Annotated[int, Field(strict=True, ge=1, le=10000)],
        target_length: Positive, target_ids: OptionalIds | None = None,
        annular_options: AnnularSourceOptions | None = None,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """读取明确NASTRAN CQUAD4/CTRIA3源面及可选目标面，检查截面和直线扫掠前提。

        返回源面指纹、短边/角度/对应关系和未验证项。annular_options额外检查
        显式同轴环带的FE源面拓扑和弦误差，不证明原始CAD解析等价或授予写权限。
        """
        return submit("mesh_source_audit", {"deck": deck, "source_ids": source_ids,
                      "target_ids": target_ids, "direction": direction,
                      "layers": layers, "target_length": target_length,
                      **({"annular_options": annular_options.model_dump()} if annular_options is not None else {})},
                      operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_geometry_partition_plan(
        operation_id: OperationId, expected_target: ExpectedTarget,
        expected_model: ExpectedModel, deck: Deck, face_ids: FaceIds, plane: Plane,
        protected_face_ids: OptionalIds | None = None,
        produce_plane_faces: Annotated[bool, Field(strict=True)] = False,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """只读预览明确FACE和平面；输出当前几何指纹、保护约束和计划hash。

        静态预览不执行试切，不承诺真实切后拓扑或任意复杂体自动分块。
        """
        return submit("geometry_partition_plan", {"deck": deck, "face_ids": face_ids,
                      "plane": plane.model_dump(), "protected_face_ids": protected_face_ids,
                      "produce_plane_faces": produce_plane_faces},
                      operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_geometry_partition_apply(
        operation_id: OperationId, expected_target: ExpectedTarget,
        expected_model: ExpectedModel, deck: Deck, face_ids: FaceIds, plane: Plane,
        expected_plan_sha256: Digest, backup_path: str,
        protected_face_ids: OptionalIds | None = None,
        produce_plane_faces: Annotated[bool, Field(strict=True)] = False,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """备份并执行一次明确NASTRAN FACE平面切分；核对计划和范围外状态。

        失败可能已有副作用，须查询原任务并核对备份；不自动重试或把日志视为模型事务。
        """
        return submit("geometry_partition_apply", {"deck": deck, "face_ids": face_ids,
                      "plane": plane.model_dump(), "protected_face_ids": protected_face_ids,
                      "produce_plane_faces": produce_plane_faces,
                      "expected_plan_sha256": expected_plan_sha256, "backup_path": backup_path},
                      operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_mesh_sweep(
        operation_id: OperationId, expected_target: ExpectedTarget,
        expected_model: ExpectedModel, deck: Deck, shell_ids: SmallIds, direction: Point,
        distance: Positive, steps: Annotated[int, Field(strict=True, ge=1, le=100)],
        property_id: EntityId, expected_source_sha256: Digest, backup_path: str,
        ttl_seconds: TTL = 300,
        freeze_source: Annotated[bool, Field(strict=True)] = False,
    ) -> Annotated[CallToolResult, ToolResult]:
        """受控NASTRAN直线扫掠：四边形源面生成CHEXA8，三角形源面生成CPENTA6。

        只接受明确源SHELL、PSOLID及新鲜源面指纹，先备份；检查新FE和范围外不变性。
        不自动粘接既有目标，不使用TETRA/Hexa Fit；完整CAD覆盖和求解器精度另验。
        """
        return submit("mesh_sweep", {"deck": deck, "shell_ids": shell_ids,
                      "direction": direction, "distance": distance, "steps": steps,
                      "property_id": property_id, "expected_source_sha256": expected_source_sha256,
                      "backup_path": backup_path,
                      **({"freeze_source": True} if freeze_source else {})},
                      operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_mesh_provenance(
        operation_id: OperationId, expected_target: ExpectedTarget,
        expected_model: ExpectedModel, deck: Deck,
        action: Literal["create", "read", "record_stage", "validate_resume"],
        ledger_id: OpaqueId | None = None, payload: dict[str, Any] | None = None,
        expected_hash: Digest | None = None, ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """在所属私有会话维护来源/阶段记录；不修改ANSA模型，不代替原生再生验收。

        变更需匹配当前ledger hash；unknown阶段进入recovery_required，不能直接续写模型。
        调用方声明与原生核验分别保留，历史记录不能授权新的模型修改。
        """
        return submit("mesh_provenance", {"deck": deck, "action": action,
                      "ledger_id": ledger_id, "payload": payload, "expected_hash": expected_hash},
                      operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_mesh_evidence_page(
        operation_id: OperationId, expected_target: ExpectedTarget,
        expected_model: ExpectedModel, deck: Deck, report_id: OpaqueId,
        section: Annotated[str, Field(min_length=1, max_length=65536)],
        offset: Annotated[int, Field(strict=True, ge=0)] = 0,
        limit: Annotated[int, Field(strict=True, ge=1, le=200)] = 100,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """分页读取本模型/会话的完整结构化网格证据；不授权模型修改。"""
        return submit("mesh_evidence_page", {"deck": deck, "report_id": report_id,
                      "section": section, "offset": offset, "limit": limit},
                      operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(name="ansa_mesh_map_blocks_prepare", structured_output=True)
    def ansa_mesh_map_blocks_prepare(
        action: Literal["plan", "apply"], operation_id: OperationId,
        expected_target: ExpectedTarget, expected_model: ExpectedModel, deck: Deck,
        face_groups: Annotated[list[Annotated[list[EntityId], Field(min_length=6, max_length=6)]], Field(min_length=2, max_length=20)],
        target_length: Positive, property_id: EntityId,
        expected_plan_sha256: Digest | None = None, backup_path: BackupPath | None = None,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """将显式六面矩形CAD分块组一次交给原生MapBlock，统一生成共享面网格。

        限已保存NASTRAN毫米、完整单Part未网格模型，2..20个轴对齐矩形块、
        同一PSHELL/既有PSOLID。face_groups须覆盖全部CAD，共面接口须共用同一FACE。
        plan绑定有限CAD域/材料/配置；apply先备份，再核对实际块、边界、来源与保护范围。
        仅准备待填充块，不生成SOLID；随后用ansa_mesh_map_block_fill(volume_ids=...)。
        """
        params = {"deck": deck, "face_groups": face_groups, "target_length": target_length, "property_id": property_id}
        if action == "plan":
            if expected_plan_sha256 is not None or backup_path is not None:
                raise ValueError("plan does not accept apply-only fields")
            tool = "mesh_map_blocks_plan"
        else:
            if expected_plan_sha256 is None or backup_path is None:
                raise ValueError("apply requires expected_plan_sha256 and backup_path")
            params.update(expected_plan_sha256=expected_plan_sha256, backup_path=backup_path)
            tool = "mesh_map_blocks_apply"
        return submit(tool, params, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(name="ansa_mesh_map_block_fill", structured_output=True)
    def ansa_mesh_map_block_fill(
        action: Literal["plan", "apply"], operation_id: OperationId,
        expected_target: ExpectedTarget, expected_model: ExpectedModel,
        deck: Deck, volume_id: EntityId | None = None, expected_plan_sha256: Digest | None = None,
        backup_path: BackupPath | None = None, ttl_seconds: TTL = 300,
        volume_ids: Annotated[list[EntityId], Field(min_length=1, max_length=20)] | None = None,
        annular_options: dict[str, Any] | None = None,
    ) -> Annotated[CallToolResult, ToolResult]:
        """通过原生VolumesRemesh填充已有MapBlock，保留原边界网格。

        volume_id沿用单块契约；volume_ids选择完整单Part矩形块组中的待填充块，
        允许保留其他已填充块。二者须且只能给一个。组模式检查共享FACE/GRID、
        全部CAD归属及矩形域不交叠，最多20块且完整边界不超过400个CQUAD4。
        annular_options仅用于四个volume_ids组成的局部孔环块组，不可与volume_id同用；
        键为4个source_face_ids、3个center坐标、3分量单位axis、inner_radius、outer_radius、distance、max_chord_error，
        由后端严格校验。省略时保持现有单块/矩形组行为。
        孔环分支限完整单Part、64FACE、2000边界SHELL和每端盖400SHELL，允许CHEXA/CPENTA；
        材料从当前原生块读取，不另建体。矩形分支另验证端盖、层数和逐层占位。
        plan只读并绑定完整模型和导出配置；apply另须新备份路径与计划哈希。
        保留原SHELL/GRID和材料，验证完整外皮、共享接口、正体积及配置状态。
        孔环分支不证明源层映射，项目质量须另行检查。
        不重划已填充体，不代表工业整件或项目质量通过；失败后先查原任务，禁止盲重试。
        """
        if (volume_id is None) == (volume_ids is None):
            raise ValueError("provide exactly one of volume_id or volume_ids")
        if annular_options is not None and volume_ids is None:
            raise ValueError("annular_options requires volume_ids and cannot be used with volume_id")
        params = {"deck": deck, **({"volume_id": volume_id} if volume_ids is None else {"volume_ids": volume_ids})}
        if annular_options is not None:
            params["annular_options"] = annular_options
        if action == "plan":
            if expected_plan_sha256 is not None or backup_path is not None:
                raise ValueError("plan does not accept apply-only fields")
            tool = "mesh_map_block_fill_plan"
        else:
            if expected_plan_sha256 is None or backup_path is None:
                raise ValueError("apply requires expected_plan_sha256 and backup_path")
            params.update(expected_plan_sha256=expected_plan_sha256, backup_path=backup_path)
            tool = "mesh_map_block_fill_apply"
        return submit(tool, params, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(name="ansa_mesh_map_block_caps", structured_output=True)
    def ansa_mesh_map_block_caps(
        action: Literal["plan", "apply"], operation_id: OperationId,
        expected_target: ExpectedTarget, expected_model: ExpectedModel, deck: Deck,
        volume_id: EntityId, master_face_ids: MapBlockCapFaceIds,
        slave_face_ids: MapBlockCapFaceIds, expected_plan_sha256: Digest | None = None,
        backup_path: BackupPath | None = None, ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """为一个已有、未填充的 MapBlock 设置明确的 master/slave caps。

        限已保存的 NASTRAN 毫米模型；模型内所有 VOLUME 均须为 MapBlock 且无 SOLID，
        目标须为 Ready to Mesh、Unmeshed、未冻结。
        每组可含多个 FACE。最多读取20个 MapBlock、100个边界 FACE、5000个 SHELL、
        10000个 GRID/模型实体，完整快照最多8 MiB；只写 volume_id 并保护其他 caps。
        plan 绑定模型、CAD/FE、caps 与网格配置；apply 先备份再调用原生 DefineCaps，
        由 GetMapVolumeBoundaries 独立验证两端盖及 round 剩余面。None 返回值不作为失败。
        不生成或重划网格；失败后查询原任务，不自动回滚或重放。
        """
        if set(master_face_ids) & set(slave_face_ids):
            raise ValueError("master_face_ids and slave_face_ids must be disjoint")
        params = {
            "deck": deck,
            "volume_id": volume_id,
            "master_face_ids": master_face_ids,
            "slave_face_ids": slave_face_ids,
        }
        if action == "plan":
            if expected_plan_sha256 is not None or backup_path is not None:
                raise ValueError("plan does not accept apply-only fields")
            tool = "mesh_map_block_caps_plan"
        else:
            if expected_plan_sha256 is None or backup_path is None:
                raise ValueError("apply requires expected_plan_sha256 and backup_path")
            params.update(expected_plan_sha256=expected_plan_sha256, backup_path=backup_path)
            tool = "mesh_map_block_caps_apply"
        return submit(tool, params, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(name="ansa_mesh_annular_source_prepare", structured_output=True)
    def ansa_mesh_annular_source_prepare(
        action: Literal["plan", "apply"], operation_id: OperationId,
        expected_target: ExpectedTarget, expected_model: ExpectedModel, deck: Deck,
        face_ids: AnnularPrepareFaceIds, source_face_ids: AnnularSourceFaceIds,
        center: Point, axis: Point, inner_radius: Positive, outer_radius: Positive,
        distance: Positive, target_length: Positive, max_chord_error: Positive,
        property_id: EntityId, expected_plan_sha256: Digest | None = None,
        backup_path: BackupPath | None = None, ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """准备已分区孔环的原生源面；限已保存NASTRAN毫米、完整单Part未网格局部域。

        face_ids必须显式覆盖当前模型全部FACE（最多64），source_face_ids恰四个。
        不识别孔或切分CAD。plan只读返回模型/参数/完整配置绑定SHA；apply匹配SHA并
        创建新备份，只临时设置原生target_length并调用一次显式FACE范围MapBlock。
        写前按周向/径向及扫掠分段估计200源面/5000边界SHELL预算，超限先拒绝；
        估计进入source_mesh_budget及计划SHA，不作为原生实际数量上界。
        绝对长度模式会更新CONS局部分段控制（允许原生细化Elem.Length、Distort.Angle为0），
        不保留原局部种子；实际perimeter GRID、完整CAD和范围外定义均独立核验。
        独立回读四个Ready块、完整边界/源成员和孔环审计；源面最多200个线性SHELL，
        固定几何审计容差1e-6毫米，记录于返回值annular_tolerance_mm及审计options。
        新体绑定已有PSOLID/已定义MAT1，严格清理本轮新增未使用属性材料，恢复完整配置。
        成功返回source_ids/shell_ids、source_sha256和source_evidence；证据字段路径为
        /source_ids、/source_sha256、/source_snapshot/nodes和/source_snapshot/elements。
        后续ansa_mesh_sweep仍须独立核验源SHA并显式给定steps；失败后查询原任务和备份。
        """
        if not set(source_face_ids) <= set(face_ids):
            raise ValueError("source_face_ids must belong to face_ids")
        if inner_radius >= outer_radius or not math.isclose(math.hypot(*axis), 1., rel_tol=0., abs_tol=1e-12):
            raise ValueError("radii must be ordered and axis must be a unit vector")
        params = {"deck": deck, "face_ids": face_ids, "source_face_ids": source_face_ids,
                  "center": center, "axis": axis, "inner_radius": inner_radius, "outer_radius": outer_radius,
                  "distance": distance, "target_length": target_length, "max_chord_error": max_chord_error,
                  "property_id": property_id}
        if action == "plan":
            if expected_plan_sha256 is not None or backup_path is not None:
                raise ValueError("plan does not accept apply-only fields")
            tool = "mesh_annular_source_plan"
        else:
            if expected_plan_sha256 is None or backup_path is None:
                raise ValueError("apply requires expected_plan_sha256 and backup_path")
            params.update(expected_plan_sha256=expected_plan_sha256, backup_path=backup_path)
            tool = "mesh_annular_source_apply"
        return submit(tool, params, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(name="ansa_mesh_map_block_prepare", structured_output=True)
    def ansa_mesh_map_block_prepare(
        action: Literal["plan", "apply"],
        operation_id: OperationId, expected_target: ExpectedTarget,
        expected_model: ExpectedModel, deck: Deck,
        face_ids: CadMapFaceIds, source_face_id: EntityId, target_face_id: EntityId,
        direction: Point, distance: Positive, target_length: Positive,
        property_id: EntityId, expected_plan_sha256: Digest | None = None,
        backup_path: BackupPath | None = None, ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """对完整六面直扫域执行原生MapBlock准备：生成规则面网格和未填充体。

        首版限已保存NASTRAN毫米模型、单Part、单PSHELL、六个独立平面FACE，
        全模型无SHELL/SOLID/VOLUME/CURVE；必须通过完整有限域导出证明。
        显式使用已有PSOLID/MAT1并清除本次新增的未使用默认属性材料。
        目标长度不得超过最短边，每边最多20个名义分段。保存并恢复完整已导出
        MeshParams、QualityCriteria和所有已导出F11行；不产生最终SOLID或宣称质量验收。
        plan只读，apply必须提供新备份路径和匹配计划哈希；失败后查询原任务，禁止盲重试。
        """
        if source_face_id == target_face_id or not {source_face_id, target_face_id} <= set(face_ids):
            raise ValueError("distinct caps must belong to face_ids")
        params = {"deck": deck, "face_ids": face_ids, "source_face_id": source_face_id,
                  "target_face_id": target_face_id, "direction": direction, "distance": distance,
                  "target_length": target_length, "property_id": property_id}
        if action == "plan":
            if expected_plan_sha256 is not None or backup_path is not None:
                raise ValueError("plan does not accept apply-only fields")
            tool = "mesh_map_block_plan"
        else:
            if expected_plan_sha256 is None or backup_path is None:
                raise ValueError("apply requires expected_plan_sha256 and backup_path")
            params.update(expected_plan_sha256=expected_plan_sha256, backup_path=backup_path)
            tool = "mesh_map_block_apply"
        return submit(tool, params, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_mesh_map_block_inspect(
        operation_id: OperationId, expected_target: ExpectedTarget,
        expected_model: ExpectedModel, deck: Deck, volume_ids: MapBlockVolumeIds,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """只读回读现有Map Block体的原生状态、端盖/侧壁和完整边界面网格。

        最多20体、100 FACE、5000 SHELL、10000 GRID。区分Ready to Mesh与Non Mappable，
        不调用MapBlock或生成实体；原生ready不代表几何覆盖、共形或质量通过。
        分页重新绑定CAD及面网格，首版限已保存NASTRAN毫米模型。
        """
        return submit("mesh_map_block_inspect", {"deck": deck, "volume_ids": volume_ids},
                      operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_geometry_sweep_candidates(
        operation_id: OperationId, expected_target: ExpectedTarget,
        expected_model: ExpectedModel, deck: Deck,
        face_ids: CandidateFaceIds, target_length: Positive,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """只读分类显式最多100 FACE，返回T1六面直扫及T2矩形分块组候选。

        原生拓扑预筛后检查有限IGES完整导出域；不自动切分，不创建VOLUME/网格。
        T2识别完整共形矩形块组，输出face_groups准备建议；共享FACE在本次读取中仅导出一次。
        开放、域外邻接、曲面及复杂组件保留拒绝原因；候选不能代替Map前的种子/面网格预检。
        首版限已保存NASTRAN毫米模型，来源证据分页需重新核验原生范围。
        """
        return submit("geometry_sweep_candidates", {
            "deck": deck, "face_ids": face_ids, "target_length": target_length,
        }, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_geometry_features(
        operation_id: OperationId, expected_target: ExpectedTarget,
        expected_model: ExpectedModel, deck: Deck,
        face_ids: FeatureFaceIds = Field(default_factory=list),
        curve_ids: FeatureCurveIds = Field(default_factory=list),
        seed_options: GeometrySeedOptions | None = None,
        sweep_options: GeometrySweepOptions | None = None,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """只读提取显式几何：最多 20 个 FACE 和 50 个 CURVE；FACE 仅报告局部曲率。
        seed 建议只用于显式 IGES 100/110 曲线。初版限 NASTRAN 毫米单位，要求数据库已保存且实体已归属 Part。
        CONS 使用原生 HOT POINT 编号/坐标/双向邻接检查闭环；闭环仅证明连通，CONS 长度仍为近似值。
        每次缓存访问和特征证据分页都重新导出范围并核对原生 CAD 指纹与拓扑；不自动识别孔。
        sweep_options 可诊断显式六面直扫拓扑对应；不证明连续 CAD 几何，不开放实体扫掠写入。
        """
        return submit("geometry_features", {
            "deck": deck,
            "face_ids": face_ids,
            "curve_ids": curve_ids,
            "seed_options": None if seed_options is None else seed_options.model_dump(),
            "sweep_options": None if sweep_options is None else sweep_options.model_dump(),
        }, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_geometry_boundary_prepare(
        operation_id: OperationId, expected_target: ExpectedTarget,
        expected_model: ExpectedModel, deck: Deck,
        face_ids: BoundaryFaceIds, cons_ids: BoundaryConsIds,
        auxiliary_part_id: EntityId, expected_source_snapshot_sha256: Digest,
        backup_path: BackupPath, ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """从 face-only geometry_features 快照创建持久辅助 CURVE。

        执行前必须已有空辅助 Part，并提供新的绝对备份路径。来源必须是指定 FACE/CONS
        范围的 geometry_features 快照及其 SHA-256；此转换不等价于原始 IGES/CAD 解析。
        转换调用原生 ConsToCurves，随后以原生 ProjectPointToCons 记录九点偏差；
        只报告采样观测，不证明连续等价、反向覆盖或自动授予网格分段权限。
        """
        return submit("geometry_boundary_prepare", {
            "deck": deck,
            "face_ids": face_ids,
            "cons_ids": cons_ids,
            "auxiliary_part_id": auxiliary_part_id,
            "expected_source_snapshot_sha256": expected_source_snapshot_sha256,
            "backup_path": backup_path,
        }, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_mesh_seed_read(
        operation_id: OperationId, expected_target: ExpectedTarget,
        expected_model: ExpectedModel, deck: Deck,
        face_ids: SeedFaceIds, cons_ids: SeedConsIds,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """只读读取明确 FACE/CONS 范围的原生边分段快照，用于后续带指纹的受控修改。"""
        return submit("mesh_seed_read", {
            "deck": deck,
            "face_ids": face_ids,
            "cons_ids": cons_ids,
        }, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_mesh_seed_apply(
        operation_id: OperationId, expected_target: ExpectedTarget,
        expected_model: ExpectedModel, deck: Deck,
        face_ids: SeedFaceIds,
        expected_seed_snapshot_sha256: Digest, backup_path: BackupPath,
        assignments: ConsSeedAssignments | None = None,
        edit: SeedEdit | None = None,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """备份后应用 CONS 分段编辑并核验 FACE/CONS 快照；失败可能需要恢复检查。"""
        if (assignments is None) == (edit is None):
            raise ValueError("exactly one of assignments or edit must be provided")
        if assignments is not None:
            return submit("mesh_seed_apply", {
                "deck": deck,
                "face_ids": face_ids,
                "assignments": [assignment.model_dump() for assignment in assignments],
                "expected_seed_snapshot_sha256": expected_seed_snapshot_sha256,
                "backup_path": backup_path,
            }, operation_id, expected_target, expected_model, ttl_seconds)
        return submit("mesh_seed_edit", {
            "deck": deck,
            "face_ids": face_ids,
            "edit": edit.model_dump(),
            "expected_seed_snapshot_sha256": expected_seed_snapshot_sha256,
            "backup_path": backup_path,
        }, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(name="ansa_mesh_cad_map", structured_output=True)
    def ansa_mesh_cad_map(
        action: Literal["plan", "apply"],
        operation_id: OperationId, expected_target: ExpectedTarget,
        expected_model: ExpectedModel, deck: Deck,
        face_ids: CadMapFaceIds, source_face_id: EntityId, target_face_id: EntityId,
        direction: Point, distance: Positive,
        steps: Annotated[int, Field(strict=True, ge=1, le=100)],
        property_id: EntityId, output_part_id: EntityId,
        expected_plan_sha256: Digest | None = None,
        backup_path: BackupPath | None = None,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """对毫米制、已保存的 NASTRAN 六面直扫域执行计划或原生 Map 六面体生成。

        六个 FACE ID 必须唯一且为正整数；source_face_id 与 target_face_id 必须不同，
        且都包含在 face_ids 中。六面须已有相容规则 CQUAD4；首版拒绝模型中已有 SOLID/VOLUME。
        output_part_id 必须是已有、独立且为空的 Part；有限平面导出域和网格须通过独立核验。
        plan 只读取并生成计划，不授权写入，且拒绝 expected_plan_sha256 或 backup_path；
        apply 必须同时提供计划 SHA-256 和新的绝对 backup_path。失败后查询原 operation_id，
        不会自动重试。仅限 NASTRAN 毫米模型和该六面映射范围，不代表其他 deck、单位或几何已支持。
        """
        if source_face_id == target_face_id:
            raise ValueError("source_face_id and target_face_id must differ")
        if source_face_id not in face_ids or target_face_id not in face_ids:
            raise ValueError("source_face_id and target_face_id must be in face_ids")
        params = {
            "deck": deck,
            "face_ids": face_ids,
            "source_face_id": source_face_id,
            "target_face_id": target_face_id,
            "direction": direction,
            "distance": distance,
            "steps": steps,
            "property_id": property_id,
            "output_part_id": output_part_id,
        }
        if action == "plan":
            if expected_plan_sha256 is not None or backup_path is not None:
                raise ValueError("plan does not accept expected_plan_sha256 or backup_path")
            return submit("mesh_cad_map_plan", params,
                          operation_id, expected_target, expected_model, ttl_seconds)
        if expected_plan_sha256 is None or backup_path is None:
            raise ValueError("apply requires expected_plan_sha256 and backup_path")
        params.update({
            "expected_plan_sha256": expected_plan_sha256,
            "backup_path": backup_path,
        })
        return submit("mesh_cad_map_apply", params,
                      operation_id, expected_target, expected_model, ttl_seconds)

    for name in ("ansa_mesh_seed_plan", "ansa_mesh_sweep_prepare",
                 "ansa_mesh_interface_audit", "ansa_mesh_block_plan", "ansa_mesh_source_audit",
                 "ansa_geometry_partition_plan", "ansa_geometry_partition_apply", "ansa_mesh_sweep",
                 "ansa_mesh_provenance", "ansa_mesh_evidence_page", "ansa_geometry_features",
                 "ansa_geometry_boundary_prepare", "ansa_mesh_seed_read", "ansa_mesh_seed_apply",
                 "ansa_mesh_cad_map", "ansa_geometry_sweep_candidates", "ansa_mesh_map_block_inspect",
                 "ansa_mesh_map_block_prepare", "ansa_mesh_map_block_fill", "ansa_mesh_map_block_caps",
                 "ansa_mesh_annular_source_prepare"):
        _forbid_extra_arguments(server, name)
