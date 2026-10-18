"""只读回读独立 ANSA 实例的原生入口状态；面板关闭后使用。"""

import json
from pathlib import Path
import sys

import grpc
import stage_three_ansa as core


def remote_entry_state():
    import builtins
    import os
    import sys
    import threading
    from ansa import session
    bridge = getattr(builtins, "_ansa_mcp_plugin_bridge", None)
    result = {"pid": os.getpid(), "program_args": [str(arg) for arg in session.ProgramArguments()],
              "main_thread": threading.current_thread() is threading.main_thread(),
              "bridge_present": bridge is not None}
    if bridge is not None:
        result.update(instance_id=bridge.state.instance_id, session_id=bridge.state.session_id,
                      runtime_home=str(bridge.runtime_home), closed=bridge.closed,
                      timer_id=bridge.timer._timer_id, ticks=bridge.executor.ticks,
                      ui=dict(bridge.state.panel_info), error=str(bridge.state.last_error).replace(bridge.token, "[redacted]"),
                      bootstrap_file=getattr(sys.modules.get("ansa_mcp_bridge.bootstrap"), "__file__", None))
    return result


if __name__ == "__main__":
    run = Path(sys.argv[1]).resolve()
    run.relative_to(core.VERIFICATION_ROOT)
    phase = sys.argv[2]
    if phase not in {"baseline", "cold_start", "reopen", "stopped", "reloaded"}:
        raise ValueError("未知原生入口验证阶段")
    instructions = json.loads((run / "launch_instructions.json").read_text(encoding="utf-8"))
    first, second = core._import_runtime_helpers()
    original = core._bounded_grpc(grpc, first.DeadlineInterceptor)
    try:
        state = second.rpc(instructions["port"], remote_entry_state)
        core._verify_program_arguments(state, Path(instructions["translator"]), instructions["port"])
        (run / ("native-entry-" + phase + ".json")).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(state, ensure_ascii=False))
    finally:
        grpc.insecure_channel = original
