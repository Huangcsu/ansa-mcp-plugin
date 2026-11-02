"""当前原生验收共用的实例回读、绑定、夹具与隔离 RPC 助手。"""
from collections import namedtuple
import importlib.util
import inspect
from pathlib import Path
import tempfile
import textwrap
import uuid

import ansa
import grpc

Details = namedtuple("Details", "method timeout metadata credentials wait_for_ready compression")


class DeadlineInterceptor(grpc.UnaryUnaryClientInterceptor):
    def __init__(self, seconds=15):
        self.seconds = seconds

    def intercept_unary_unary(self, continuation, details, request):
        timeout = self.seconds if details.timeout is None else min(details.timeout, self.seconds)
        bounded = Details(details.method, timeout, details.metadata, details.credentials,
                          details.wait_for_ready, getattr(details, "compression", None))
        return continuation(bounded, request)


def remote_basic():
    import os
    import threading
    from ansa import base, session
    return {"pid": os.getpid(), "deck": base.CurrentDeck(),
            "main_thread": threading.current_thread() is threading.main_thread(),
            "program_args": [str(item) for item in session.ProgramArguments()]}


def remote_start(project_root, runtime_home, marker_path):
    import builtins
    import sys
    from pathlib import Path
    Path(marker_path).write_text("entered\n", encoding="utf-8")
    sys.path.insert(0, str(Path(project_root) / "src"))
    from ansa_mcp_bridge.bootstrap import start
    Path(marker_path).write_text("imported\n", encoding="utf-8")
    bridge = start(runtime_home=runtime_home, show=False)
    Path(marker_path).write_text("started\n", encoding="utf-8")
    return {"pid": bridge.state.pid, "instance_id": bridge.state.instance_id,
            "session_id": bridge.state.session_id, "port": bridge.transport.port,
            "main_thread": bridge.state.probe()["main_thread"]}


def remote_click_bind():
    import builtins
    builtins._ansa_mcp_plugin_bridge.transport.request_bind()
    return True


def remote_verify_node(node_id):
    from ansa import base, constants
    entity = base.GetEntity(constants.NASTRAN, "GRID", node_id)
    if entity is None:
        return None
    return base.GetEntityCardValues(constants.NASTRAN, entity, ("X1", "X2", "X3"))


def remote_seed(path):
    from ansa import base, constants
    node = base.CreateEntity(constants.NASTRAN, "GRID", {"X1": 1., "X2": 2., "X3": 3.})
    assert node is not None
    assert base.SaveAs(path) == 0
    return {"id": node._id, "usable": node.is_usable()}


def remote_capabilities():
    from ansa import base, constants
    return {"version": constants.app_version, "version_id": constants.app_version_id,
            "windows": [{"name": base.GetAnsaWindowName(w), "models": len(base.GetAnsaModelsOfAnsaWindow(w))}
                        for w in base.GetAllAnsaWindows()]}


def rpc(port, fn, *args):
    # ANSA can cache an older script function table across repeated remote names.
    # A fresh source/module/function name forces an independent test invocation.
    name = "probe_" + uuid.uuid4().hex
    source = textwrap.dedent(inspect.getsource(fn)).replace("def " + fn.__name__ + "(", "def " + name + "(", 1)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=name + "-",
                                     suffix=".py", delete=False) as handle:
        handle.write(source)
        path = Path(handle.name)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        connection = ansa.connect(host="127.0.0.1", port=port)
        try:
            result = ansa.execute_function(getattr(module, name), *args)
        finally:
            ansa.disconnect(connection)
    except BaseException as exc:
        # Keep failed/uncertain invocation sources available for diagnosis.
        if hasattr(exc, "add_note"):
            exc.add_note(f"RPC source preserved at {path}")
        raise
    else:
        try:
            path.unlink()
        except OSError:
            pass  # Cleanup must not turn a completed remote call into a failure.
        return result
