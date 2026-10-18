"""仅提取本任务 ANSA dump 的异常地址、模块与栈地址；不输出进程内存内容。"""
import json
import mmap
from pathlib import Path
import struct
import sys


def inspect(path):
    with Path(path).open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
        if data[:4] != b"MDMP":
            raise ValueError("Not a Windows minidump")
        count, directory = struct.unpack_from("<II", data, 8)
        streams = {}
        for i in range(count):
            kind, size, rva = struct.unpack_from("<III", data, directory + 12 * i)
            streams[kind] = (rva, size)
        modules = []
        if 4 in streams:
            rva = streams[4][0]
            module_count = struct.unpack_from("<I", data, rva)[0]
            for i in range(module_count):
                base, size, _, _, name_rva = struct.unpack_from("<QIIII", data, rva + 4 + i * 108)
                length = struct.unpack_from("<I", data, name_rva)[0]
                name = data[name_rva + 4:name_rva + 4 + length].decode("utf-16-le")
                modules.append((base, size, Path(name).name))
        def locate(address):
            for base, size, name in modules:
                if base <= address < base + size:
                    return name + "+0x" + format(address - base, "x")
            return "unmapped:0x" + format(address, "x")
        if 6 not in streams:
            raise ValueError("No exception stream")
        rva = streams[6][0]
        thread_id, _, code, flags, record, address, params = struct.unpack_from("<IIIIQQI", data, rva)
        information = struct.unpack_from("<15Q", data, rva + 40)
        ctx_size, ctx_rva = struct.unpack_from("<II", data, rva + 160)
        result = {"dump": Path(path).name, "thread_id": thread_id, "exception_code": hex(code),
                  "fault_module": locate(address), "exception_parameter_count": params}
        if code == 0xC0000005 and params >= 2:
            result.update(access={0: "read", 1: "write", 8: "execute"}.get(information[0], str(information[0])),
                          invalid_address=hex(information[1]))
        if ctx_size >= 256:
            rsp = struct.unpack_from("<Q", data, ctx_rva + 152)[0]
            rip = struct.unpack_from("<Q", data, ctx_rva + 248)[0]
            result["rip_module"] = locate(rip)
            if 3 in streams:
                threads_rva = streams[3][0]
                thread_count = struct.unpack_from("<I", data, threads_rva)[0]
                for i in range(thread_count):
                    pos = threads_rva + 4 + 48 * i
                    tid = struct.unpack_from("<I", data, pos)[0]
                    if tid != thread_id:
                        continue
                    start, stack_size, stack_rva = struct.unpack_from("<QII", data, pos + 24)
                    offset = rsp - start
                    if 0 <= offset < stack_size:
                        hints = []
                        for j in range(min(96, (stack_size - offset) // 8)):
                            value = struct.unpack_from("<Q", data, stack_rva + offset + j * 8)[0]
                            name = locate(value)
                            if not name.startswith("unmapped:"):
                                hints.append({"slot": j, "module": name})
                        result["stack_module_hints"] = hints
                    break
        return result


if __name__ == "__main__":
    print(json.dumps(inspect(sys.argv[1]), indent=2))
