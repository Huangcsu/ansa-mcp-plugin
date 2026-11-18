"""受控识别前后复用的 Part Feature 状态与库存读取。"""

from collections import Counter

from . import _params, entity_catalog, structured_evidence


_MAX_FEATURES = 10000
_CARD_BYTES = 32768
_PART_FIELDS = (
    "Name", "Module Id", "Features Status", "Mesh Parameter Name",
    "Treatment", "Treatment Parameters",
)


def _error(exc):
    return (type(exc).__name__ + ": " + str(exc))[:1000]


def _validate(tool, params):
    if tool != "model_features_status":
        raise ValueError("unsupported recognition inventory contract")
    _params(params, ("deck", "part_ids"))
    if type(params["deck"]) is not int or params["deck"] <= 0:
        raise ValueError("deck must be a positive integer")
    ids = params["part_ids"]
    if (type(ids) is not list or not 1 <= len(ids) <= 100
            or any(type(i) is not int or i <= 0 for i in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError("part_ids must contain 1 to 100 unique positive integer IDs")


def _card(base, deck, entity, requested):
    """先核对卡片定义；保留缺字段、读取和序列化失败，绝不补零。"""
    result = {"card_fields": None, "fields": {}, "unavailable_fields": [], "errors": {}}
    try:
        labels = entity_catalog._entity_card_labels(entity, deck)
        if len(labels) > 512 or any(not v or len(v) > 128 for v in labels):
            raise ValueError("card_fields exceeds the field-name budget")
        result["card_fields"] = sorted(labels)
    except Exception as exc:
        result["errors"]["card_fields"] = _error(exc)
        return result
    wanted = requested
    result["unavailable_fields"] = [name for name in wanted if name not in labels]
    selected = [name for name in wanted if name in labels]
    if not selected:
        return result
    try:
        values = base.GetEntityCardValues(deck, entity, tuple(selected))
        if type(values) is not dict:
            raise RuntimeError("GetEntityCardValues did not return a mapping")
    except Exception as exc:
        result["errors"]["card"] = _error(exc)
        return result
    for name in selected:
        try:
            if name not in values:
                raise ValueError("requested card field was not returned")
            value = entity_catalog._json_value(values[name], base, deck)
            candidate = dict(result["fields"], **{name: value})
            if len(structured_evidence._encoded(candidate)) > _CARD_BYTES:
                raise ValueError("card value exceeds the 32 KiB card budget; value was not returned")
            result["fields"][name] = value
        except Exception as exc:
            result["errors"][name] = _error(exc)
    return result


def _inventory(base, deck, parts, handler):
    rows, entries = [], []
    observed_count = 0
    for part, reference in parts:
        row = {"part": reference, **_card(base, deck, part, _PART_FIELDS),
               "features_status": None, "query_status": "failed",
               "feature_count": None, "counts_by_type": None}
        row["features_status"] = row["fields"].get("Features Status")
        if row["features_status"] is None:
            row["errors"]["features_status"] = "Features Status is unavailable"
        try:
            features = handler.get_features_of_part(part)
            if type(features) not in (list, tuple):
                raise RuntimeError("get_features_of_part did not return a feature list")
            observed_count += len(features)
            if observed_count > _MAX_FEATURES:
                raise OverflowError("Feature inventory exceeds 10000 Part/Feature associations")
            found, seen = [], set()
            for feature in features:
                ref = entity_catalog._entity_reference(base, deck, feature)
                key = (ref["type"], ref["id"])
                if key in seen:
                    raise RuntimeError("duplicate Feature identity within one Part")
                seen.add(key)
                found.append((reference["id"], ref, feature))
            entries.extend(found)
            row.update(query_status="ok", feature_count=len(found),
                       counts_by_type=dict(sorted(Counter(ref["type"] for _, ref, _ in found).items())))
        except OverflowError:
            raise
        except Exception as exc:
            row["errors"]["features"] = _error(exc)
        rows.append(row)
    entries.sort(key=lambda item: (item[0], item[1]["type"], item[1]["id"]))
    return rows, entries
