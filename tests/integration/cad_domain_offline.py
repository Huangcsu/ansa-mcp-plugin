"""独立带孔平面验证 CAD 通用清单；需要单独安装 Gmsh，不连接 ANSA。"""
import argparse
import json
from pathlib import Path
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir",type=Path,required=True)
    args=parser.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=False)
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"src"))
    from ansa_mcp_bridge.operations.cad_domain_audit import audit_manifest,sha256
    import gmsh
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal",0)
        def save(name,box,hole=None):
            gmsh.clear();gmsh.model.add(name)
            occ=gmsh.model.occ
            face=occ.addRectangle(*box)
            if hole:
                occ.cut([(2,face)],[(2,occ.addRectangle(*hole))])
            occ.synchronize()
            assert len(gmsh.model.getEntities(2))==1
            path=args.output_dir/(name+".brep");gmsh.write(str(path))
            return {"path":path.name,"sha256":sha256(path)}
        reference=save("reference",(0,0,0,12,8),(5,3,0,2,2))
        left=save("left",(0,0,0,6,8),(5,3,0,1,2))
        right=save("right",(6,0,0,6,8),(6,3,0,1,2))
        extra=save("extra",(14,0,0,1,1))
        rows=[]
        for name,files,ids,expected in (
            ("complete",[left,right],[701,702],"pass"),
            ("missing",[left],[701],"fail"),
            ("duplicate",[left,right,left],[701,702,703],"fail"),
            ("excess",[left,right,extra],[701,702,704],"fail"),
        ):
            manifest={"schema_version":1,"units":"mm","expected_source_face_ids":[501],
                      "groups":[{"label":name,"source_face_ids":[501],"candidate_face_ids":ids,
                                 "reference_files":[reference],"candidate_files":files}]}
            path=args.output_dir/(name+".json")
            path.write_text(json.dumps(manifest,indent=2)+"\n",encoding="utf-8")
            result=audit_manifest(gmsh,path)
            (args.output_dir/(name+"-result.json")).write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
            assert result["status"]==expected,(name,result)
            rows.append({"case":name,"expected":expected,"actual":result["status"],
                         "metrics":result["rows"][0]["result"]})
        report={"complete":True,"passed":True,"kernel":"Gmsh "+gmsh.__version__,
                "scope":"independent planar trimmed domain with a hole; synthetic declared IDs, no native ANSA binding",
                "cases":rows}
        (args.output_dir/"report.json").write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
        print(json.dumps({"passed":True,"cases":len(rows),"output_dir":str(args.output_dir)}))
    finally:
        gmsh.finalize()


if __name__=="__main__":main()
