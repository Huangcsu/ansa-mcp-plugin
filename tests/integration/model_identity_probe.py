"""Official-API object lifetime probe, only in a verified disposable ANSA GUI."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

import ansa
import grpc
from stage_one_ansa import remote_basic, DeadlineInterceptor

ROOT = Path(__file__).resolve().parents[2]


def inspect_retained(action, path):
    import builtins
    from ansa import base, constants, session
    if action == "seed":
        node = base.CreateEntity(constants.NASTRAN, "GRID", {"X1": 11., "X2": 22., "X3": 33.})
        base.SaveAs(path)
        builtins._guard_probe = (base.GetCurrentAnsaModel(), node, node._id)
    elif action == "new":
        session.New("discard")
    elif action == "open":
        base.Open(path)
    elif action == "reset":
        base.ClearIncludeConfiguration(2)
    elif action == "retain":
        node = base.GetEntity(constants.NASTRAN, "GRID", 1)
        builtins._guard_probe = (base.GetCurrentAnsaModel(), node, 1)
    elif action == "edit":
        node = base.GetEntity(constants.NASTRAN, "GRID", 1)
        base.SetEntityCardValues(constants.NASTRAN, node, {"X1": 44.})
    elif action == "save":
        base.SaveAs(path)
    old_model, old_node, node_id = builtins._guard_probe
    current = base.GetCurrentAnsaModel()
    fresh = base.GetEntity(constants.NASTRAN, "GRID", node_id)
    result = {"action": action, "model": repr(current), "model_equal": old_model == current,
              "old_node": repr(old_node), "fresh_node": repr(fresh), "node_equal": old_node == fresh,
              "node_dir": [x for x in dir(old_node) if not x.startswith("__")],
              "model_dir": [x for x in dir(current) if not x.startswith("__")]}
    for name, fn in (("retained_card", lambda: base.GetEntityCardValues(constants.NASTRAN, old_node, ("X1", "X2", "X3"))),
                     ("retained_position", lambda: old_node.position), ("retained_id", lambda: old_node._id),
                     ("retained_usable", lambda: old_node.is_usable())):
        try:
            result[name] = fn()
        except Exception as exc:
            result[name] = {"error": str(exc)}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ansa-root", type=Path, required=True)
    args = parser.parse_args()
    run = ROOT / "runtime/verification" / ("model-identity-" + uuid.uuid4().hex[:8])
    run.mkdir(parents=True)
    translator = run / "isolated_transl.py"
    translator.write_text("# Isolated model identity capability probe.\n")
    fixture = run / "fixture.ansa"
    report = {"cases": [], "run_dir": str(run)}
    original = grpc.insecure_channel
    def bounded(target, options=None, **kw):
        return grpc.intercept_channel(original(target, options=[*(options or []), ("grpc.enable_http_proxy", 0)], **kw), DeadlineInterceptor(20))
    grpc.insecure_channel = bounded
    command = [str(args.ansa_root / "ansa64.bat"), "-grpcport", "50051", "-grpchost", "127.0.0.1",
               "-changedir", str(Path.home()), "-translfp", str(translator), "-nolauncher", "--iconify", "--skip-release-highlights"]
    env = {**os.environ, "ANSA_SRV": "5280@localhost", "ANSA_MCP_SUM_HOME": str(run / "unused-legacy-runtime"), "ANSA_MCP_SUM_AUTO_POLL": "0"}
    connection = None
    verified = False
    with (run / "ansa.log").open("wb") as log:
        process = subprocess.Popen(command, cwd=run, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            ready = original("127.0.0.1:50051", options=[("grpc.enable_http_proxy", 0)])
            try:
                grpc.channel_ready_future(ready).result(timeout=75)
            finally:
                ready.close()
            connection = ansa.connect(port=50051, host="127.0.0.1")
            basic = ansa.execute_function(remote_basic)
            assert str(translator) in basic["program_args"] and basic["main_thread"]
            verified = True
            report["identity"] = basic
            for action in ("seed", "observe", "observe", "edit", "save", "new", "open", "retain", "open", "reset"):
                value = ansa.execute_function(inspect_retained, action, str(fixture))
                report["cases"].append(value)
                print(json.dumps(value, ensure_ascii=True), flush=True)
            report["success"] = True
        except Exception as exc:
            report["error"] = repr(exc)
        finally:
            if connection:
                if verified:
                    ansa.stop(connection)
                else:
                    ansa.disconnect(connection)
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                report["launcher_still_running"] = True
            grpc.insecure_channel = original
    (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(run / "report.json"), "success": report.get("success")}), flush=True)
    return 0 if report.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
