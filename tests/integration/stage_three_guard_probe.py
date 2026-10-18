"""Bounded model/other-entity lifetime probe in one disposable ANSA GUI."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import uuid

import ansa
import grpc
from stage_two_ansa import ROOT, rpc, remote_basic, DeadlineInterceptor


def remote_lifetime(action, fixture):
    import builtins
    from pathlib import Path
    from ansa import base, constants, session
    if not hasattr(builtins, "_third_guard"):
        model = base.GetCurrentAnsaModel()
        if model is None:
            windows = base.GetAllAnsaWindows()
            candidates = base.GetAnsaModelsOfAnsaWindow(windows[0]) if len(windows) == 1 else []
            model = candidates[0] if len(candidates) == 1 else None
        builtins._third_guard = (model, None, None)
    if action == "seed":
        session.New("discard")
        base.SetCurrentDeck(constants.NASTRAN)
        node = base.CreateEntity(constants.NASTRAN, "GRID", {"X1": 1., "X2": 2., "X3": 3.})
        material = base.CreateEntity(constants.NASTRAN, "MAT1", {"E": 210000., "NU": .3})
        assert node is not None and material is not None
        base.SaveAs(fixture)
        builtins._third_guard = (base.GetCurrentAnsaModel(), node, material)
    elif action in {"new", "initial_new"}:
        session.New("discard")
    elif action == "abaqus":
        base.SetCurrentDeck(constants.ABAQUS)
    elif action == "open":
        base.Open(fixture)
    elif action == "retain":
        builtins._third_guard = (base.GetCurrentAnsaModel(), base.GetEntity(constants.NASTRAN, "GRID", 1),
                                base.GetEntity(constants.NASTRAN, "MAT1", 1))
    elif action == "reset":
        base.ClearIncludeConfiguration(2)
    elif action == "empty_save":
        session.New("discard")
        base.SaveAs(str(Path(fixture).with_name("empty.ansa")))
        builtins._third_guard = (base.GetCurrentAnsaModel(), None, None)
    elif action == "empty_open":
        base.Open(str(Path(fixture).with_name("empty.ansa")))
    refs = builtins._third_guard
    def usable(ref):
        if ref is None:
            return None
        try:
            return ref.is_usable()
        except Exception as exc:
            return {"error": str(exc)}
    return {"action": action, "deck": base.CurrentDeck(), "model": repr(base.GetCurrentAnsaModel()), "database": base.DataBaseName(),
            "retained_model_usable": usable(refs[0]), "retained_grid_usable": usable(refs[1]),
            "retained_material_usable": usable(refs[2]),
            "model_counts": [len(base.GetAnsaModelsOfAnsaWindow(w)) for w in base.GetAllAnsaWindows()]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ansa-root", type=Path, required=True)
    args = parser.parse_args()
    run = ROOT / "runtime/verification" / ("stage-three-guard-" + uuid.uuid4().hex[:8])
    run.mkdir(parents=True)
    translator = run / "isolated_transl.py"
    translator.write_text("# Disposable third-stage lifetime probe.\n")
    report = {"run_dir": str(run), "cases": []}
    original = grpc.insecure_channel
    def bounded(target, options=None, **kwargs):
        return grpc.intercept_channel(original(target, options=[*(options or []), ("grpc.enable_http_proxy", 0)], **kwargs), DeadlineInterceptor(20))
    grpc.insecure_channel = bounded
    identity, process = None, None
    with (run / "ansa.log").open("wb") as log:
        try:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 50051))
            command = [str(args.ansa_root / "ansa64.bat"), "-grpcport", "50051", "-grpchost", "127.0.0.1",
                       "-changedir", str(Path.home()), "-translfp", str(translator), "-nolauncher", "--iconify", "--skip-release-highlights"]
            env = {**os.environ, "ANSA_SRV": "5280@localhost", "ANSA_MCP_SUM_HOME": str(run / "unused-legacy-runtime"), "ANSA_MCP_SUM_AUTO_POLL": "0", "PYTHONDONTWRITEBYTECODE": "1"}
            process = subprocess.Popen(command, cwd=run, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            ready = original("127.0.0.1:50051", options=[("grpc.enable_http_proxy", 0)])
            try:
                grpc.channel_ready_future(ready).result(timeout=75)
            finally:
                ready.close()
            identity = rpc(50051, remote_basic)
            assert str(translator) in identity["program_args"] and identity["main_thread"]
            report["identity"] = identity
            for action in ("initial", "initial_new", "seed", "observe", "new", "open", "retain", "open", "reset", "empty_save", "abaqus", "empty_open", "empty_open"):
                result = rpc(50051, remote_lifetime, action, str(run / "fixture.ansa"))
                report["cases"].append(result)
                print(json.dumps(result), flush=True)
            report["success"] = True
        except Exception as exc:
            report["success"], report["error"] = False, repr(exc)
        finally:
            if identity and "identity" in report:
                try:
                    fresh = rpc(50051, remote_basic)
                    assert fresh["pid"] == identity["pid"] and fresh["program_args"] == identity["program_args"]
                    connection = ansa.connect(host="127.0.0.1", port=50051)
                    ansa.stop(connection)
                    report["stopped_owned_process"] = True
                except Exception as exc:
                    report["cleanup_error"] = repr(exc)
            if process:
                try:
                    process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    report["launcher_still_running"] = True
            grpc.insecure_channel = original
    (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(run / "report.json"), "success": report["success"]}), flush=True)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
