"""一次源面回读后的有界多块预检与合批；模型写入复用 mesh_sweep。"""
from collections import Counter
import copy
import hashlib
import math

from . import structured_native as native, volume_meshing


def normalize_blocks(deck, blocks):
    if type(blocks) is not list or not 1 <= len(blocks) <= 32:
        raise ValueError("blocks must contain 1..32 already-ready sweep blocks")
    normalized=[];seen=set();shells=set()
    required={"id","shell_ids","direction","distance","steps","property_id"}
    for block in blocks:
        if type(block) is not dict or set(block)!=required:
            raise ValueError("each block requires exactly id, shell_ids, direction, distance, steps, property_id")
        name=block["id"]
        if type(name) is not str or not name or name!=name.strip() or len(name)>128 or name in seen:
            raise ValueError("block IDs must be distinct nonempty strings of at most 128 characters")
        seen.add(name)
        clean=native._normalize_sweep({"deck":deck,**{k:v for k,v in block.items() if k!="id"},
                                     "expected_source_sha256":"0"*64,"backup_path":"unused.ansa"})
        if shells.intersection(clean["shell_ids"]):
            raise ValueError("a source SHELL cannot belong to multiple blocks")
        shells.update(clean["shell_ids"])
        normalized.append({"id":name,**clean})
    if len(shells)>200:raise ValueError("batch preparation is limited to 200 total source SHELLs")
    return sorted(normalized,key=lambda b:b["id"])


def source_subset(snapshot, ids):
    """切片保留与独立 capture_source 完全相同的内容和摘要，不读取模型。"""
    ids=set(ids)
    rows=[copy.deepcopy(row) for row in snapshot["elements"] if row["id"] in ids]
    if {row["id"] for row in rows}!=ids:raise ValueError("source IDs missing from captured snapshot")
    rows.sort(key=lambda row:row["id"])
    nodes={n for row in rows for n in row["nodes"]}
    body={"deck":snapshot["deck"],"complete":True,
          "nodes":sorted((copy.deepcopy(row) for row in snapshot["nodes"] if row["id"] in nodes),key=lambda row:row["id"]),
          "elements":rows,"integrity":{"complete":True,"requested_element_count":len(ids),
          "element_count":len(rows),"node_count":len(nodes),"missing_node_ids":[],
          "element_types":dict(sorted(Counter(row["type"] for row in rows).items()))},
          "ownership":copy.deepcopy(snapshot["ownership"])}
    if len(body["nodes"])!=len(nodes):raise ValueError("source snapshot has missing GRID coordinates")
    return {**body,"snapshot_sha256":hashlib.sha256(volume_meshing._encoded(body)).hexdigest()}


def _sweep_key(block):
    return (tuple(block["direction"]),block["distance"],block["steps"],block["property_id"])


def _component_groups(full,normalized,block_rows,issues):
    """先保留不可拆的共享节点组件，再按预算打包，块名不影响可行性。"""
    parents=list(range(len(normalized)))
    def root(i):
        while parents[i]!=i:
            parents[i]=parents[parents[i]];i=parents[i]
        return i
    owners={}
    for i,row in enumerate(block_rows):
        for node in row["source_node_ids"]:
            if node in owners:parents[root(i)]=root(owners[node])
            else:owners[node]=i
    components={}
    for i in range(len(normalized)):components.setdefault(root(i),[]).append(i)
    groups=[];packable=[]
    for indices in components.values():
        blocks=[normalized[i] for i in indices];block=blocks[0]
        ids=sorted(n for b in blocks for n in b["shell_ids"])
        names=sorted(b["id"] for b in blocks)
        group={"key":_sweep_key(block),"block_ids":names,"shell_ids":ids,"params":block}
        if len({_sweep_key(b) for b in blocks})!=1:
            issues.append({"code":"shared_GRID_has_incompatible_sweep_parameters","block_ids":names})
            groups.append(group);continue
        if len(ids)*block["steps"]>native._MAX_NEW_SOLIDS:
            issues.append({"code":"shared_GRID_component_exceeds_sweep_budget","block_ids":names})
            groups.append(group);continue
        try:native._source_preflight(source_subset(full,ids),{**block,"shell_ids":ids})
        except (ValueError,RuntimeError,NotImplementedError) as exc:
            issues.append({"code":"shared_GRID_component_preflight_failed","block_ids":names,"error":str(exc)})
            groups.append(group);continue
        packable.append(group)
    # First-fit decreasing 不是最少调用次数的全局优化；完整组件不可被拆开。
    packed=[]
    for component in sorted(packable,key=lambda g:(-len(g["shell_ids"])*g["params"]["steps"],g["shell_ids"])):
        for group in packed:
            if group["key"]!=component["key"]:continue
            ids=sorted(group["shell_ids"]+component["shell_ids"]);block=group["params"]
            if len(ids)*block["steps"]>native._MAX_NEW_SOLIDS:continue
            try:native._source_preflight(source_subset(full,ids),{**block,"shell_ids":ids})
            except (ValueError,RuntimeError,NotImplementedError):continue
            group["shell_ids"]=ids;group["block_ids"]=sorted(group["block_ids"]+component["block_ids"])
            break
        else:packed.append(component)
    return sorted(groups+packed,key=lambda g:g["shell_ids"])


def prepare(base, constants, deck, blocks, *, max_penta_fraction=1.0):
    """只对现有源 SHELL 预检；不能推导尚未生成的下游源面或证明目标域无交叠。"""
    normalized=normalize_blocks(deck,blocks)
    if type(max_penta_fraction) not in (int,float) or not math.isfinite(max_penta_fraction) or not 0<=max_penta_fraction<=1:
        raise ValueError("max_penta_fraction must be finite and in 0..1")
    full=native.capture_source(base,constants,deck,sorted(n for b in normalized for n in b["shell_ids"]))
    issues=native._source_interface_issues(full);block_rows=[];property_cache={}
    for block in normalized:
        source=source_subset(full,block["shell_ids"])
        row={"id":block["id"],"source_sha256":source["snapshot_sha256"],
             "shell_ids":block["shell_ids"],"source_node_ids":[n["id"] for n in source["nodes"]]}
        try:
            if block["property_id"] not in property_cache:
                try:native._psolid_preflight(base,deck,block["property_id"])
                except (ValueError,RuntimeError,NotImplementedError) as exc:property_cache[block["property_id"]]=str(exc)
                else:property_cache[block["property_id"]]=None
            if property_cache[block["property_id"]] is not None:
                raise ValueError(property_cache[block["property_id"]])
            audit=native._source_preflight(source,block)
            row.update(source_preflight_passed=True,source_audit=audit)
        except (ValueError,RuntimeError,NotImplementedError) as exc:
            row.update(source_preflight_passed=False,error=str(exc))
            issues.append({"code":"source_or_property_preflight_failed","block_ids":[block["id"]],"error":str(exc)})
        block_rows.append(row)
    groups=_component_groups(full,normalized,block_rows,issues)
    # Same coordinates with independent GRID IDs are also an unsafe interface.
    # Coincident IDs cannot be inferred as a conformal shared source boundary.
    positions={}
    for node in full["nodes"]:positions.setdefault(tuple(node["xyz"]),[]).append(node["id"])
    coincident=[sorted(ids) for ids in positions.values() if len(ids)>1]
    if coincident:issues.append({"code":"coincident_source_GRIDs_have_distinct_IDs","node_id_groups":coincident})
    facets={}
    for row in full["elements"]:facets.setdefault(tuple(sorted(row["nodes"])),[]).append(row["id"])
    repeated=[ids for ids in facets.values() if len(ids)>1]
    if repeated:issues.append({"code":"duplicate_source_facets","shell_id_groups":repeated})
    output=[];counts=Counter()
    for group in groups:
        source=source_subset(full,group["shell_ids"]);block=group["params"]
        types=Counter(row["type"] for row in source["elements"])
        estimated={"CHEXA":types["CQUAD4"]*block["steps"],"CPENTA":types["CTRIA3"]*block["steps"]}
        counts.update(estimated)
        output.append({"block_ids":group["block_ids"],"source_snapshot_sha256":source["snapshot_sha256"],
                       "estimated_solids":estimated,"sweep_params":{"deck":deck,"shell_ids":group["shell_ids"],
                       "direction":block["direction"],"distance":block["distance"],"steps":block["steps"],
                       "property_id":block["property_id"],"expected_source_sha256":source["snapshot_sha256"]}})
    total=sum(counts.values());fraction=counts["CPENTA"]/total if total else 0.0
    if fraction>max_penta_fraction:
        issues.append({"code":"estimated_CPENTA_fraction_exceeds_requested_limit","fraction":fraction,"limit":max_penta_fraction})
    if issues:
        for group in output:group.pop("sweep_params")
    return {"status":"blocked" if issues else "ready_for_existing_sweep","blocks":block_rows,"groups":output,"issues":issues,
            "source_snapshot_sha256":full["snapshot_sha256"],"usable_for_mutation":False,
            "summary":{"block_count":len(normalized),"source_shell_count":len(full["elements"]),"source_capture_calls":1,
                       "property_preflight_calls":len(property_cache),"proposed_sweep_calls":len(groups),
                       "separate_block_sweep_calls":len(normalized),"estimated_solids":dict(counts),"estimated_penta_fraction":fraction},
            "input_origin":"ANSA_main_thread_source_snapshot_and_caller_declared_ready_blocks",
            "target_geometry_verified":False,"overall_geometry_status":"incomplete",
            "limits":["Only already-existing planar transverse source SHELLs; no automatic partition or future source generation.",
                      "Existing mesh_sweep must independently recheck its source, property, context and unique backup path before writing.",
                      "Target occupancy, near-coincident nodes, CAD trim coverage and global solid overlaps are not checked here."]}
