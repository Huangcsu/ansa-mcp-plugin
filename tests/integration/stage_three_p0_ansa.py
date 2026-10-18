"""P0 API, ABAQUS mass and surface readback in a uniquely owned ANSA fixture."""

import asyncio
import argparse
import json
import os
import shutil
import sys
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import stage_three_ansa as common
import stage_three_api_catalog_ansa as lifecycle


FIXTURE = """*HEADING
Independent P0 fixture
*NODE
1,0,0,0
2,20,0,0
3,20,10,0
4,0,10,0
5,0,0,10
6,20,0,10
7,20,10,10
8,0,10,10
11,40,0,0
12,50,0,0
13,50,10,0
14,40,10,0
15,40,0,10
16,50,0,10
17,50,10,10
18,40,10,10
*ELEMENT,TYPE=S4R,ELSET=SHELLS
1,1,2,3,4
2,5,6,7,8
*ELEMENT,TYPE=C3D8R,ELSET=SOLIDS
10,11,12,13,14,15,16,17,18
*MATERIAL,NAME=SHELL_MAT
*DENSITY
0.01
*ELASTIC
210000,0.3
*MATERIAL,NAME=SOLID_MAT
*DENSITY
0.002
*ELASTIC
2300,0.3
*SHELL SECTION,ELSET=SHELLS,MATERIAL=SHELL_MAT,OFFSET=SPOS
2
*SOLID SECTION,ELSET=SOLIDS,MATERIAL=SOLID_MAT
*SURFACE,NAME=MIXED_SHELL,TYPE=ELEMENT
1,SPOS
2,SNEG
*SURFACE,NAME=SOLID_FACES,TYPE=ELEMENT
10,S1
10,S2
*SURFACE,NAME=MIXED_TYPES,TYPE=ELEMENT
1,SPOS
10,S3
"""


def remote_refresh(expected_pid, expected_arguments):
    import builtins
    import importlib
    import os
    import threading
    from ansa import session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Owned ANSA refresh identity changed")
    bridge = builtins._ansa_mcp_plugin_bridge
    if bridge.state.journal.busy():
        raise RuntimeError("Cannot reload a busy isolated bridge")
    names = ("api_catalog", "api_documentation", "scripts", "abaqus_mass", "abaqus_surfaces", "inspection")
    for name in names:
        importlib.reload(importlib.import_module("ansa_mcp_bridge.operations." + name))
    return list(names)


def remote_oracle(expected_pid, expected_arguments):
    import os
    import threading
    from ansa import base, constants, session
    if (os.getpid() != expected_pid or list(session.ProgramArguments()) != expected_arguments
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("Owned ANSA oracle identity changed")
    deck = constants.ABAQUS
    rows = {}
    for kind, fields in (("NODE", ("X", "Y", "Z")), ("SHELL", ("PID", "G1", "G2", "G3", "G4")),
                         ("SOLID", ("PID", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8")),
                         ("MATERIAL", ("Name", "*DENSITY", "DENS")),
                         ("SHELL_SECTION", ("MID", "T", "OFFSET")), ("SOLID_SECTION", ("MID",))):
        rows[kind] = []
        for item in sorted(base.CollectEntities(deck, None, kind), key=lambda e: e._id):
            labels = item.card_fields(deck)
            requested = tuple(f for f in fields if f in labels)
            rows[kind].append({"id": item._id, "fields": base.GetEntityCardValues(deck, item, requested)})
    rows["sets"] = []
    for item in sorted(base.CollectEntities(deck, None, "SET"), key=lambda e: e._id):
        members = []
        for ent in base.CollectEntities(deck, item, "__ALL_ENTITIES__", recursive=False, no_expand_types=("SET",)):
            kind = ent.ansa_type(deck)
            fields = tuple(f for f in ent.card_fields(deck) if f.startswith("G") and f[1:].isdigit())
            members.append({"id": ent._id, "type": kind,
                "nodes": base.GetEntityCardValues(deck, ent, fields) if fields else {},
                "side": base.GetEntityAttributeFromSet(item, ent, "Oriented") if kind == "SHELL" else None})
        rows["sets"].append({"id": item._id, "name": item._name, "members": members})
    rows["visible"] = {kind: sorted(e._id for e in base.CollectEntities(deck, None, kind, filter_visible=True))
                       for kind in ("NODE", "SHELL", "SOLID")}
    return rows


async def verify(instructions, identity, helpers, run, report, report_path):
    one, two, _ = helpers
    report["isolated_module_refresh"] = two.rpc(identity["port"], remote_refresh, identity["pid"], identity["program_args"])
    fixture_path = run / "p0-fixture.inp"
    fixture_path.write_text(FIXTURE, encoding="utf-8")
    parameters = StdioServerParameters(command=sys.executable, args=["-B", "-m", "ansa_mcp_plugin",
        "--runtime-home", instructions["runtime_home"]], env={**os.environ,
        "PYTHONPATH": str(common.PROJECT_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})

    def oracle():
        return two.rpc(identity["port"], remote_oracle, identity["pid"], identity["program_args"])

    def record(name, evidence):
        lifecycle.record(report, report_path, name, evidence)

    def ref(kind, eid):
        return {"deck": 4, "type": kind, "id": eid}

    async with stdio_client(parameters, errlog=(run / "p0-backend.stderr.log").open("w", encoding="utf-8")) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=60)) as client:
            await client.initialize()
            h = common.McpHarness(client, report, run)
            await common._bind_for_run(h, identity, one, two, identity["port"])
            schemas = await h.list_schema()
            await h.direct_result("ansa_model_new", {"preservation": "discard"})
            await h.snapshot_context()
            script = "from ansa import base, constants\nbase.SetCurrentDeck(constants.ABAQUS)\nstatus = base.InputAbaqus(" + repr(str(fixture_path)) + ", model_action='merge_model')\nRESULT = {'status': status}\n"
            imported = await h.direct_result("ansa_script_execute", {"script": script, "allow_model_change": True})
            common._json_write(run / "p0-fixture-import.json", imported)
            assert imported["result"]["status"] == 1, imported
            await h.snapshot_context()
            await h.direct_result("ansa_model_save", {"path": str(run / "p0-before.ansa")})
            before = oracle()
            common._json_write(run / "p0-oracle-before.json", before)
            assert len(before["SHELL"]) == 2 and len(before["SOLID"]) == 1
            record("owned_fixture_schema_and_independent_cards", {"schemas": schemas, "oracle": before})

            validation = await h.direct_result("ansa_script_validate", {"script":
                "from ansa import kinetics\nkinetics.SetUnits('mm', 'tonne', 'N', 's')\nkinetics.AddJointToConfig(None, None)\n"})
            assert validation["valid"], validation
            doc = await h.direct_result("ansa_api_reference", {"query": "base.CalcElementMass"})
            assert "entities" in doc["documentation"] and "Returns" in doc["documentation"], doc
            assert "signature" in doc and doc["signature"]
            kin_doc = await h.direct_result("ansa_api_reference", {"query": "kinetics.AddJointToConfig"})
            assert len(kin_doc["documentation"]) > 60
            search = await h.direct_result("ansa_api_reference", {"action": "search", "module": "base", "query": "CalcElementMass", "limit": 1})
            assert search["matches"] and search["matches"][0]["api"] == "ansa.base.CalcElementMass", search
            modules = await h.direct_result("ansa_api_reference", {"action": "modules", "module": "kinetics"})
            assert modules["complete"] and modules["function_count"] > 0, modules
            wrong_case = await h.direct_result("ansa_script_validate", {"script": "from ansa import base\nbase.calcelementmass([])\n"})
            assert not wrong_case["valid"]
            record("kinetics_preflight_and_installed_complete_help", {"validation": validation, "mass_help": doc,
                "kinetics_help": kin_doc, "search": search, "modules": modules, "wrong_case": wrong_case})

            async def inspect(query_action, **options):
                return await h.direct_result("ansa_model_inspect", {"action": query_action, "deck": 4, "options": options})

            results = []
            for kind, eid, expected in (("MATERIAL", 1, 8.0), ("MATERIAL", 2, 2.0),
                    ("SHELL_SECTION", 1, 8.0), ("SOLID_SECTION", 2, 2.0)):
                value = await inspect("mass_properties", entities=[ref(kind, eid)])
                assert abs(value["mass"] - expected) < 1.e-10, value
                results.append(value)
            overlapping = await inspect("mass_properties", entities=[ref("MATERIAL", 1), ref("MATERIAL", 2), ref("SHELL_SECTION", 1)])
            assert abs(overlapping["mass"] - 10) < 1.e-10 and overlapping["scope"]["element_count"] == 3
            shell_only = await inspect("mass_properties", entities=[ref("MATERIAL", 1), ref("MATERIAL", 2)], element_types=["SHELL"])
            assert abs(shell_only["mass"] - 8) < 1.e-10 and shell_only["scope"]["counts"]["SOLID"] == 0
            old_action = await inspect("entity_metrics", action="mass", entity_type="MATERIAL", entity_id=1)
            assert old_action["value"] == 8.0 and old_action["value_kind"] == "scalar_mass"
            record("analytic_mass_material_section_dedup_and_shell_only_scope", {"scopes": results, "overlap": overlapping, "shell_only": shell_only, "legacy_action": old_action})

            surfaces = []
            for sid in (1, 2, 3):
                value = await inspect("surface_members", set_id=sid)
                assert value["complete"], value
                assert value["total"] == 2, value
                surfaces.append(value)
            page = await inspect("surface_members", set_id=2, offset=0, limit=1)
            next_page = await inspect("surface_members", set_id=2, offset=1, limit=1)
            assert len(page["members"]) == len(next_page["members"]) == 1
            assert page["members"][0] != next_page["members"][0]
            record("mixed_shell_sides_and_distinct_faces_of_one_solid", {"surfaces": surfaces, "page": page, "next_page": next_page})

            failures = []
            for action, options in (("mass_properties", {"entities": [ref("MATERIAL", 1)], "element_types": ["SOLID"]}),
                                    ("surface_members", {"set_id": 2, "limit": True})):
                _, result = await h.task("ansa_model_inspect", {"action": action, "deck": 4, "options": options}, expected="failed")
                failures.append(result)
            assert oracle() == before
            record("invalid_inputs_rejected_and_model_visibility_unchanged", failures)
            await h.direct_result("ansa_model_save", {"path": str(run / "p0-after.ansa")})
            await h.direct_result("ansa_model_open", {"path": str(run / "p0-after.ansa"), "preservation": "discard"})
            await h.snapshot_context()
            reopened = oracle()
            assert reopened == before
            value = await inspect("mass_properties", entities=[ref("MATERIAL", 1), ref("MATERIAL", 2)])
            surface = await inspect("surface_members", set_id=2)
            assert value["mass"] == 10.0 and surface["total"] == 2 and surface["complete"]
            record("save_reopen_and_independent_readback", {"mass": value, "surface": surface})

            # A copy of this chat's own generated model exercises the real mixed
            # surface/container representation; never open or write its original.
            own_model = common.PROJECT_ROOT / "runtime" / "verification" / "four_stage_rebuild_20261017_02.ansa"
            if own_model.is_file():
                large_copy = run / "p0-four-stage-copy.ansa"
                shutil.copy2(own_model, large_copy)
                await h.direct_result("ansa_model_open", {"path": str(large_copy), "preservation": "discard"})
                await h.snapshot_context()
                scopes = []
                for mid, expected in ((1, 1.358804812), (2, 0.8359955465), (3, 0.59071138225), (4, 0.76371064775)):
                    value = await inspect("mass_properties", entities=[ref("MATERIAL", mid)], element_types=["SHELL"])
                    assert abs(value["mass"] - expected) < 1.e-9, value
                    scopes.append(value)
                record("own_large_model_copy_material_masses", scopes)
                located = await h.direct_result("ansa_script_execute", {"script":
                    "from ansa import base, constants\n"
                    "d = constants.ABAQUS\n"
                    "ties = [e for e in base.CollectEntities(d, None, 'TIE') if 'BOOM1' in e._name.upper()]\n"
                    "assert len(ties) == 1\n"
                    "assert {'MSID', 'SSID'}.issubset(ties[0].card_fields(d))\n"
                    "v = ties[0].get_entity_values(d, ('MSID', 'SSID'))\n"
                    "names = {'BOOM1_PAD_SUPPORT': v['MSID']._id, 'BOOM1_PAD_TIE': v['SSID']._id}\n"
                    "def set_card(e):\n"
                    "    fields = tuple(k for k in ('ORIENTATION', 'Output as:', 'OUTPUT TYPE') if k in e.card_fields(d))\n"
                    "    return base.GetEntityCardValues(d, e, fields) if fields else {}\n"
                    "detail = {}\n"
                    "for name, sid in names.items():\n"
                    "    s = base.GetEntity(d, 'SET', sid)\n"
                    "    raw = base.CollectEntities(d, s, '__ALL_ENTITIES__', recursive=False, no_expand_types=('SET',))\n"
                    "    detail[name] = {'name': s._name, 'card': set_card(s), 'raw_count': len(raw), 'first': [{'type': e.ansa_type(d), 'id': e._id, 'name': e._name, 'card': set_card(e) if e.ansa_type(d) == 'SET' else {}, 'oriented': base.GetEntityAttributeFromSet(s, e, 'Oriented')} for e in raw[:8]]}\n"
                    "RESULT = {'names': names, 'detail': detail}\n"})
                common._json_write(run / "p0-large-native-surface-shape.json", located)
                names = located["result"]["names"]
                if os.environ.get("ANSA_MCP_P0_PAUSE_LARGE_SURFACES") == "1":
                    common._json_write(run / "p0-large-surface-checkpoint.json", {"waiting": True})
                    for _ in range(600):
                        if (run / "p0-continue.flag").is_file():
                            break
                        await asyncio.sleep(0.5)
                    else:
                        raise TimeoutError("large surface diagnostic checkpoint expired")
                report["final_product_module_refresh"] = two.rpc(identity["port"], remote_refresh, identity["pid"], identity["program_args"])
                surfaces = {name: await inspect("surface_members", set_id=sid, limit=20, expand_nested=True) for name, sid in names.items()}
                common._json_write(run / "p0-large-surface-results.json", surfaces)
                for name, expected in (("BOOM1_PAD_SUPPORT", 3816), ("BOOM1_PAD_TIE", 504)):
                    assert surfaces[name]["total"] == expected and surfaces[name]["all_members_readable"], surfaces[name]
                assert {row["oriented"] for row in surfaces["BOOM1_PAD_SUPPORT"]["members"]} == {"SNEG"}
                record("own_large_model_copy_real_mixed_surfaces", surfaces)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--report-name", default="p0-report.json")
    parser.add_argument("--pause-large-surfaces", action="store_true")
    options, remaining = parser.parse_known_args()
    if options.pause_large_surfaces:
        os.environ["ANSA_MCP_P0_PAUSE_LARGE_SURFACES"] = "1"
    raise SystemExit(lifecycle.main(remaining, report_name=options.report_name,
        scope="P0 API help/preflight and ABAQUS mass/surface readback", verify_callback=verify))
