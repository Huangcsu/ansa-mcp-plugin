"""将未解决的实体对打包为有界、互不重叠的两组 native 检查输入。"""


def plan_native_overlap_batches(rows, *, max_group_size=200, max_batches=1000):
    """只规划两组交叉检查；每一候选对必须在某一批次跨组出现。"""
    if type(rows) is not list:
        raise ValueError("rows must be a list")
    if type(max_group_size) is not int or not 1 <= max_group_size <= 200:
        raise ValueError("max_group_size must be in 1..200")
    if type(max_batches) is not int or not 0 <= max_batches <= 10000:
        raise ValueError("max_batches must be in 0..10000")
    pairs, unresolved_elements = set(), set()
    for row in rows:
        if type(row) is not dict:
            raise ValueError("candidate row must be a dictionary")
        if row.get("kind") == "element":
            value = row.get("element_id")
            if type(value) is not int or value <= 0:
                raise ValueError("invalid unresolved element ID")
            unresolved_elements.add(value)
            continue
        ids = row.get("element_ids")
        if (type(ids) is not list or len(ids) != 2
                or any(type(i) is not int or i <= 0 for i in ids) or ids[0] == ids[1]):
            raise ValueError("candidate pair must contain two distinct positive element IDs")
        pairs.add(tuple(sorted(ids)))
    pending = set(pairs)
    batches = []
    while pending and len(batches) < max_batches:
        first, second = set(), set()
        for a,b in sorted(pending):
            if a in first:
                if b not in first and (b in second or len(second)<max_group_size): second.add(b)
            elif a in second:
                if b not in second and (b in first or len(first)<max_group_size): first.add(b)
            elif b in first:
                if len(second)<max_group_size: second.add(a)
            elif b in second:
                if len(first)<max_group_size: first.add(a)
            elif len(first)<max_group_size and len(second)<max_group_size:
                first.add(a);second.add(b)
        covered = {p for p in pending if (p[0] in first and p[1] in second)
                                        or (p[1] in first and p[0] in second)}
        if not covered: raise RuntimeError("batch planner made no progress")
        batches.append({"first_ids":sorted(first),"second_ids":sorted(second),
                        "covered_pairs":[list(p) for p in sorted(covered)]})
        pending.difference_update(covered)
    return {"status":"pass" if not pending and not unresolved_elements else "incomplete",
            "candidate_pair_count":len(pairs),"covered_pair_count":len(pairs)-len(pending),
            "remaining_pairs":[list(p) for p in sorted(pending)],
            "unresolved_element_ids":sorted(unresolved_elements),"batches":batches,
            "limits":{"max_group_size":max_group_size,"max_batches":max_batches},
            "semantics":"A zero native result resolves only cross-group candidates; no within-group clearance is inferred."}
