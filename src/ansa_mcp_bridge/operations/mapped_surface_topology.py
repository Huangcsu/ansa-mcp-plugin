"""Independent lattice readback; this module does not generate a mesh."""

from collections import defaultdict


def four_sides(cons_ids, boundaries):
    """Order four native open CONS using HOT POINT identity, never coordinates."""
    if len(cons_ids) != 4 or len(set(cons_ids)) != 4:
        raise ValueError("mapped FACE requires exactly four distinct CONS")
    rows = {r["cons_id"]: r for r in boundaries}
    adjacency = defaultdict(list)
    for ident in cons_ids:
        row = rows[ident]
        if row["status"] != "supported":
            raise ValueError("mapped FACE requires supported first-order open CONS")
        fields = row["native_fields"]
        if any(fields.get(k) != v for k, v in
               (("Frozen", "NO"), ("Collapsed", "NO"), ("Biasing", "LINEAR"))):
            raise ValueError("mapped CONS must be unfrozen, uncollapsed and LINEAR")
        for vertex in row["hot_points"]:
            adjacency[vertex["id"]].append(ident)
    if len(adjacency) != 4 or any(len(v) != 2 for v in adjacency.values()):
        raise ValueError("mapped FACE boundary must be one simple four-corner loop")
    start = current = min(adjacency)
    used, ordered = set(), []
    for _ in range(4):
        candidates = sorted(set(adjacency[current]) - used)
        if not candidates:
            raise ValueError("disconnected four-sided boundary")
        ident = candidates[0]
        endpoints = [v["id"] for v in rows[ident]["hot_points"]]
        other = next(v for v in endpoints if v != current)
        ordered.append({"cons_id": ident, "start": current, "end": other,
                        "segments": rows[ident]["segments"]})
        used.add(ident)
        current = other
        if current == start and len(used) != 4:
            raise ValueError("four-sided boundary contains multiple loops")
    if current != start or len(used) != 4:
        raise ValueError("four-sided boundary is not closed")
    if any(ordered[i]["segments"] != ordered[i+2]["segments"] for i in (0, 1)):
        raise ValueError("opposite CONS must already have equal integer segment counts")
    return ordered


def verify_lattice(elements, sides):
    """Prove every quad belongs to the expected tensor grid using actual edges."""
    nx, ny = len(sides[0])-1, len(sides[1])-1
    if min(nx, ny) < 1 or len(sides[2]) != nx+1 or len(sides[3]) != ny+1:
        raise ValueError("inconsistent mapped side counts")
    if (any(sides[i][-1] != sides[(i+1) % 4][0] for i in range(4))
            or any(len(set(s)) != len(s) for s in sides)):
        raise ValueError("mapped boundary paths do not form a simple loop")
    if len(elements) != nx*ny:
        raise ValueError("mapped SHELL count differs from opposite-side product")
    by_edge = defaultdict(list)
    by_id = {}
    for row in elements:
        ident, nodes = row["id"], row["nodes"]
        if row["type"] != "CQUAD4" or len(nodes) != 4 or len(set(nodes)) != 4 or ident in by_id:
            raise ValueError("mapped source must contain distinct linear CQUAD4 elements")
        by_id[ident] = row
        for i in range(4):
            by_edge[tuple(sorted((nodes[i], nodes[(i+1) % 4])))].append(ident)
    if any(len(v) > 2 for v in by_edge.values()):
        raise ValueError("nonmanifold mapped mesh edge")
    expected_free = {tuple(sorted((s[i], s[i+1]))) for s in sides for i in range(len(s)-1)}
    actual_free = {edge for edge, ids in by_edge.items() if len(ids) == 1}
    if actual_free != expected_free:
        raise ValueError("mapped free edges differ from the original CONS paths")
    lattice, used, orientation = [list(sides[0])], set(), None
    for j in range(ny):
        top = []
        bottom = lattice[-1]
        for i in range(nx):
            a, b = bottom[i:i+2]
            candidates = set(by_edge.get(tuple(sorted((a, b))), [])) - used
            if len(candidates) != 1:
                raise ValueError("mapped cell is missing or duplicated")
            ident = candidates.pop()
            nodes = by_id[ident]["nodes"]
            k = nodes.index(a)
            if nodes[(k+1) % 4] == b:
                c, d, sign = nodes[(k+2) % 4], nodes[(k+3) % 4], 1
            elif nodes[(k-1) % 4] == b:
                c, d, sign = nodes[(k-2) % 4], nodes[(k-3) % 4], -1
            else:
                raise ValueError("mapped cell uses a diagonal as a grid edge")
            if orientation is None:
                orientation = sign
            if sign != orientation:
                raise ValueError("inconsistent CQUAD4 orientation in mapped lattice")
            if i == 0:
                if d != sides[3][-j-2]:
                    raise ValueError("mapped left boundary differs from native CONS")
                top.append(d)
            elif top[-1] != d:
                raise ValueError("mapped row has a hanging or mismatched node")
            top.append(c)
            used.add(ident)
        if top[-1] != sides[1][j+1]:
            raise ValueError("mapped right boundary differs from native CONS")
        lattice.append(top)
    flat = [n for line in lattice for n in line]
    actual_nodes = {n for row in elements for n in row["nodes"]}
    if (lattice[-1] != list(reversed(sides[2])) or len(used) != len(elements)
            or len(set(flat)) != (nx+1)*(ny+1) or set(flat) != actual_nodes):
        raise ValueError("mapped mesh is not the complete expected tensor lattice")
    return {"verified": True, "segments": [nx, ny], "shell_count": nx*ny,
            "node_count": len(flat), "lattice_node_ids": lattice,
            "free_edge_count": len(actual_free), "orientation": orientation}
