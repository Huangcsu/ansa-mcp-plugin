"""可离线复用的 CAD 面域验收；CAD 内核由调用方注入，不在 ANSA 中加载 DLL。"""
import hashlib
import json
import math
from pathlib import Path


def _number(value, name, *, zero=False):
    if type(value) not in (int,float) or not math.isfinite(value) or value < 0 or (not zero and value==0):
        raise ValueError(name+" must be a finite positive number" )
    return float(value)


def _ids(value, name, *, empty=False):
    if (type(value) is not list or (not value and not empty) or len(value)>2000
            or any(type(i) is not int or i<=0 for i in value) or len(set(value))!=len(value)):
        raise ValueError(name+" must contain unique positive integer IDs")
    return value


def sha256(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _status(values):
    values=list(values)
    return "fail" if "fail" in values else ("incomplete" if "incomplete" in values else "pass")


def compare_domains(kernel, reference, candidates, *, area_tolerance=1e-3, relative_area_tolerance=1e-8):
    """比较完整裁剪面域；包含缺失、额外、重复域与布尔面积守恒核查。"""
    area_tolerance=_number(area_tolerance,"area_tolerance")
    relative_area_tolerance=_number(relative_area_tolerance,"relative_area_tolerance",zero=True)
    for name,faces in (("reference",reference),("candidates",candidates)):
        if (type(faces) not in (list,tuple) or len(faces)>2000
                or any(type(f) not in (list,tuple) or len(f)!=2 or f[0]!=2
                       or type(f[1]) is not int or f[1]<=0 for f in faces)
                or len({tuple(f) for f in faces})!=len(faces)):
            raise ValueError(name+" must contain distinct CAD FACE tags")
    if not reference: return {"status":"incomplete","reason":"empty_reference"}
    occ=kernel.model.occ
    def area(faces):
        value=sum(occ.getMass(*f) for f in faces)
        if not math.isfinite(value) or value<0: raise RuntimeError("invalid CAD area")
        return value
    def union(faces):
        if len(faces)<=1: return list(faces)
        out,_=occ.fuse(faces[:1],faces[1:],removeObject=False,removeTool=False)
        if any(f[0]!=2 for f in out): raise RuntimeError("union returned unexpected dimension")
        if not out: raise RuntimeError("nonempty FACE union disappeared")
        return out
    def difference(first,second):
        if not first: out=[]
        elif not second: out=first
        else: out=occ.cut(first,second,removeObject=False,removeTool=False)[0]
        if any(f[0]!=2 for f in out): raise RuntimeError("FACE difference returned unexpected dimension")
        return {"area":area(out),"face_count":len(out),
                "bounds":[list(occ.getBoundingBox(*f)) for f in out]}
    try:
        ref_sum,candidate_sum=area(reference),area(candidates)
        ref,candidate=union(reference),union(candidates)
        ref_area,candidate_area=area(ref),area(candidate)
        missing,excess=difference(ref,candidate),difference(candidate,ref)
        common=occ.intersect(ref,candidate,removeObject=False,removeTool=False)[0] if candidate else []
        common_area=area([f for f in common if f[0]==2])
        tolerance=area_tolerance+relative_area_tolerance*ref_area
        conservation=max(abs(missing["area"]+common_area-ref_area),abs(excess["area"]+common_area-candidate_area))
        duplicate=max(0.,candidate_sum-candidate_area)
        source_duplicate=max(0.,ref_sum-ref_area)
        state="pass" if max(missing["area"],excess["area"],duplicate)<=tolerance else "fail"
        if ref_area<=0 or conservation>tolerance or source_duplicate>tolerance: state="incomplete"
        return {"status":state,"reference_area":ref_area,"candidate_sum_area":candidate_sum,
                "candidate_union_area":candidate_area,"missing":missing,"excess":excess,
                "duplicate_area":duplicate,"source_duplicate_area":source_duplicate,
                "common_area":common_area,"boolean_conservation_error":conservation,
                "effective_area_tolerance":tolerance}
    except Exception as exc:
        return {"status":"incomplete","reason":"CAD_boolean_failed_or_inconsistent","error":repr(exc)}


def load_manifest(path):
    """在加载 CAD 前校验完整声明清单和每个 CAD 文件的内容哈希。"""
    path=Path(path).resolve()
    data=json.loads(path.read_text(encoding="utf-8"))
    if (type(data) is not dict or type(data.get("schema_version")) is not int
            or data.get("schema_version")!=1 or data.get("units")!="mm"):
        raise ValueError("schema_version=1 and explicit mm units are required")
    expected=_ids(data.get("expected_source_face_ids"),"expected_source_face_ids")
    groups=data.get("groups")
    if type(groups) is not list or not groups or len(groups)>1000:
        raise ValueError("groups must contain 1..1000 source groups")
    seen_source,seen_candidate=set(),set()
    for group in groups:
        if type(group) is not dict or type(group.get("label")) is not str or not group["label"]:
            raise ValueError("each group requires a label")
        source_ids=_ids(group.get("source_face_ids"),"source_face_ids")
        candidate_ids=_ids(group.get("candidate_face_ids"),"candidate_face_ids",empty=True)
        if seen_source.intersection(source_ids) or seen_candidate.intersection(candidate_ids):
            raise ValueError("a source or candidate FACE cannot appear in multiple groups; combine merged source faces into one group")
        seen_source.update(source_ids);seen_candidate.update(candidate_ids)
        for key,allow_empty in (("reference_files",False),("candidate_files",True)):
            files=group.get(key)
            if type(files) is not list or (not files and not allow_empty) or len(files)>1000:
                raise ValueError(key+" must be an explicit bounded file list")
            for entry in files:
                if (type(entry) is not dict or type(entry.get("path")) is not str
                        or type(entry.get("sha256")) is not str or len(entry["sha256"])!=64):
                    raise ValueError("CAD file path and SHA256 are required")
                file_path=(path.parent/entry["path"]).resolve()
                if sha256(file_path)!=entry["sha256"]: raise ValueError("stale CAD file: "+str(file_path))
                entry["resolved_path"]=str(file_path)
        if bool(candidate_ids)!=bool(group["candidate_files"]):
            raise ValueError("candidate FACE IDs and CAD files must both be present or both be empty")
    if seen_source!=set(expected): raise ValueError("source groups do not exactly cover expected source FACE inventory")
    _number(data.get("linear_tolerance",1e-5),"linear_tolerance")
    _number(data.get("area_tolerance",1e-3),"area_tolerance")
    _number(data.get("relative_area_tolerance",1e-8),"relative_area_tolerance",zero=True)
    return data


def audit_manifest(kernel, manifest_path, *, progress=None):
    """按参数化清单验收任意已声明分组；不会推断原生父子关系或修改 ANSA。"""
    manifest_digest=sha256(manifest_path)
    manifest=load_manifest(manifest_path)
    if sha256(manifest_path)!=manifest_digest: raise ValueError("manifest changed during loading")
    kernel.option.setNumber("General.Terminal",0)
    kernel.option.setNumber("Geometry.ToleranceBoolean",manifest.get("linear_tolerance",1e-5))
    rows=[]
    for group in manifest["groups"]:
        kernel.clear();kernel.model.add(group["label"])
        try:
            def imports(files):
                all_faces=[]
                for entry in files:
                    before=set(kernel.model.occ.getEntities(2))
                    kernel.model.occ.importShapes(entry["resolved_path"],False)
                    kernel.model.occ.synchronize()
                    all_faces.extend(sorted(set(kernel.model.occ.getEntities(2))-before))
                return all_faces
            refs,candidates=imports(group["reference_files"]),imports(group["candidate_files"])
            if len(refs)!=len(group["source_face_ids"]) or len(candidates)!=len(group["candidate_face_ids"]):
                value={"status":"incomplete","reason":"CAD_import_FACE_count_differs_from_native_declaration",
                       "imported_reference_count":len(refs),"imported_candidate_count":len(candidates)}
            else:
                value=compare_domains(kernel,refs,candidates,area_tolerance=manifest.get("area_tolerance",1e-3),
                                      relative_area_tolerance=manifest.get("relative_area_tolerance",1e-8))
        except Exception as exc:
            value={"status":"incomplete","reason":"CAD_import_failed","error":repr(exc)}
        rows.append({"label":group["label"],"source_face_ids":group["source_face_ids"],
                     "candidate_face_ids":group["candidate_face_ids"],"result":value})
        if progress: progress(rows[-1])
    changed=[entry["resolved_path"] for g in manifest["groups"] for key in ("reference_files","candidate_files")
             for entry in g[key] if sha256(entry["resolved_path"])!=entry["sha256"]]
    status=_status(row["result"]["status"] for row in rows)
    manifest_changed=sha256(manifest_path)!=manifest_digest
    if changed or manifest_changed: status="incomplete"
    return {"schema_version":1,"complete":True,"status":status,"units":"mm","rows":rows,
            "manifest_sha256":manifest_digest,"manifest_changed":manifest_changed,
            "kernel":"Gmsh "+str(getattr(kernel,"__version__","unknown")),
            "expected_source_face_count":len(manifest["expected_source_face_ids"]),"changed_input_files":changed,
            "linear_tolerance":manifest.get("linear_tolerance",1e-5),
            "input_origin":"hash-bound CAD exports plus caller-declared complete native FACE inventory",
            "native_model_binding_verified":False,"mesh_quality_or_continuous_FE_error_proven":False,
            "limits":["Native export provenance and complete source inventory must be independently verified by the MCP caller.",
                      "One declared native FACE must import as one CAD FACE; conversion or mapping ambiguity stays incomplete.",
                      "This checks trimmed CAD domains numerically, not FE interior approximation, automatic block planning, or solver accuracy."]}
