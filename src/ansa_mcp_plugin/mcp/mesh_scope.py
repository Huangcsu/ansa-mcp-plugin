"""注册显式有限元与 CAD 网格范围读取及冻结工具。"""

from typing import Annotated, Literal

from mcp.types import CallToolResult
from pydantic import AfterValidator, Field

from .schemas import ExpectedModel, ExpectedTarget, ToolResult, _forbid_extra_arguments


OperationId = Annotated[str, Field(min_length=1, max_length=128)]
Deck = Annotated[int, Field(strict=True, ge=1)]
EntityId = Annotated[int, Field(strict=True, ge=1)]


def _unique_entity_ids(value):
    if len(set(value)) != len(value):
        raise ValueError("entity_ids must not contain duplicate IDs")
    return value


EntityIds = Annotated[
    list[EntityId], Field(min_length=1, max_length=200), AfterValidator(_unique_entity_ids)
]
ReadEntityType = Literal["FACE", "CONS", "SHELL", "SOLID", "VOLUME"]
FreezeEntityType = Literal["FACE", "SHELL", "SOLID"]
Action = Literal["freeze", "unfreeze"]
ScopeSha256 = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
BackupPath = Annotated[str, Field(strict=True, min_length=1)]
TTL = Annotated[int, Field(strict=True, ge=0, le=86400)]


def register(server, submit):
    @server.tool(name="ansa_mesh_scope_read", structured_output=True)
    def ansa_mesh_scope_read(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck,
        entity_type: ReadEntityType,
        entity_ids: EntityIds,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """只读读取当前NASTRAN显式网格关系并返回scope_sha256；后续冻结须使用同一deck/type/IDs的指纹。

        SHELL 和 SOLID 的 ID 属于有限元（FE）层；FACE、CONS 和 VOLUME 的 ID 属于 CAD 拓扑层，不能互换。
        FACE 通过原生 Macro 表达面范围，Macro 边界可引用 CONS；同一 CONS 可以由多个 FACE Macro 共享，
        因此共享边界仍是同一个拓扑实体，不应重复解释为多个 FACE。所有 ID 均是 entity_type 对应的原生实体 ID。
        展开完整根Macro及一层CONS邻域，返回freeze_targets、冻结状态、FE/节点及Volume归属；不实例化种子。
        大结果使用既有mesh_evidence_page分页，缺失状态不当作未冻结。
        """
        return submit(
            "mesh_scope_read",
            {"deck": deck, "entity_type": entity_type, "entity_ids": entity_ids},
            operation_id,
            expected_target,
            expected_model,
            ttl_seconds,
        )

    @server.tool(name="ansa_mesh_freeze", structured_output=True)
    def ansa_mesh_freeze(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck,
        entity_type: FreezeEntityType,
        entity_ids: EntityIds,
        action: Action,
        expected_scope_sha256: ScopeSha256,
        backup_path: BackupPath,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """按新鲜指纹冻结/解冻当前NASTRAN显式范围；实际变化前验证新的backup_path备份及独立回读。

        SHELL 和 SOLID 的 ID 属于有限元（FE）层；FACE 的 ID 属于 CAD 拓扑层，并按其原生 Macro 范围处理。
        Macro 边界可以共享同一 CONS；共享 CONS 是拓扑连接关系，不会把 CONS ID 当成 FE 单元或 FACE ID。
        expected_scope_sha256 必须来自同一 deck、entity_type 和 entity_ids 清单的 mesh_scope_read 结果。
        FACE操作修改回读freeze_targets中的Macro代表FACE标志；freeze_effects列完整Macro及周界的影响范围。
        CONS的原始Frozen字段与由Macro形成的有效保护分别回读；不修改范围外Macro或CONS原始标志。
        目标已达请求状态则no_op且不创建备份。失败保留possible_effects/recovery_required，不自动重放。
        """
        return submit(
            "mesh_freeze",
            {
                "deck": deck,
                "entity_type": entity_type,
                "entity_ids": entity_ids,
                "action": action,
                "expected_scope_sha256": expected_scope_sha256,
                "backup_path": backup_path,
            },
            operation_id,
            expected_target,
            expected_model,
            ttl_seconds,
        )

    for name in ("ansa_mesh_scope_read", "ansa_mesh_freeze"):
        _forbid_extra_arguments(server, name)


__all__ = ["register"]
