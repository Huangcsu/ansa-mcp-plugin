"""独立测试实例的 Python 回调跟踪；不读取模型或敏感参数。"""

import json
from pathlib import Path
import sys

import grpc
import stage_three_ansa as core


def install_trace(expected_pid, expected_translator, trace_path):
    import builtins
    import json
    import os
    import sys
    import threading
    import time
    from pathlib import Path
    from ansa import session

    if (os.getpid() != expected_pid or expected_translator not in list(session.ProgramArguments())
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Trace ownership mismatch")
    path = Path(trace_path).resolve()
    if path.parent != Path(expected_translator).resolve().parent or path.exists():
        raise RuntimeError("Trace requires a new file in the owned fixture directory")
    handle = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    import faulthandler
    fault_file = path.with_name("ribbon-faulthandler.log").open("x", encoding="ascii")
    faulthandler.enable(file=fault_file, all_threads=True)
    builtins._ansa_mcp_ribbon_fault_file = fault_file
    saved = sys.getprofile()
    counter = [0]
    selected = {"show_panel", "_on_timer", "_registered_window", "_refresh", "_hide_context",
                "_on_window_close", "_on_window_reject", "_on_window_accept", "_forget_destroyed_context"}
    native = {"BCShow", "BCTimerStop", "BCWindowReject", "BCGetTopWidget", "BCGetTopLevelWidget",
              "BCTextEditSetText", "BCLabelSetText"}

    def trace(frame, event, arg):
        module = frame.f_globals.get("__name__", "")
        name = frame.f_code.co_name
        wanted = module == "ansa_mcp_bridge.ui.native_panel" and name in selected
        c_name = getattr(arg, "__name__", None) if event.startswith("c_") else None
        if wanted and (event in {"call", "return", "exception"} or c_name in native) and counter[0] < 10000:
            counter[0] += 1
            record = {"t": time.monotonic(), "event": event, "function": name, "native": c_name}
            os.write(handle, (json.dumps(record) + "\n").encode("ascii"))
        if saved is not None:
            saved(frame, event, arg)

    builtins._ansa_mcp_ribbon_trace = {"callback": trace, "handle": handle, "prior": saved}
    sys.setprofile(trace)
    return {"pid": os.getpid(), "trace_path": str(path), "prior_profile_present": saved is not None}


if __name__ == "__main__":
    run = Path(sys.argv[1]).resolve()
    run.relative_to(core.VERIFICATION_ROOT.resolve())
    instructions = json.loads((run / "launch_instructions.json").read_text(encoding="utf-8"))
    owned = json.loads((run / "normal-user-process-before.json").read_text(encoding="utf-8-sig"))
    if isinstance(owned, list):
        if len(owned) != 1:
            raise RuntimeError("Owned process is not unique")
        owned = owned[0]
    first, second = core._import_runtime_helpers()
    original = core._bounded_grpc(grpc, first.DeadlineInterceptor)
    try:
        result = second.rpc(instructions["port"], install_trace, owned["ProcessId"],
                            instructions["translator"], str(run / "ribbon-python-trace.jsonl"))
        core._json_write(run / "trace-installed.json", result)
        print(json.dumps(result))
    finally:
        grpc.insecure_channel = original
