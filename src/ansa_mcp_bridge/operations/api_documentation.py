"""Read-only access to version-matched ANSA Python API help installed locally."""

import ast
import json
import os
import re
import sys
import textwrap
import warnings
from collections import OrderedDict
from html.parser import HTMLParser
from pathlib import Path


_VERSION_RE = re.compile(r"\d+(?:\.\d+){1,3}\Z")
_GENERATED = Path("reference") / "api_ref_ansa" / "generated"
_STUBS = Path("_downloads") / "autocomplete" / "py_dev" / "pydev_ansa" / "ansa"
_MAX_HTML_BYTES = 32 * 1024 * 1024
_MAX_STUB_BYTES = 8 * 1024 * 1024
_MAX_CAPTURE_CHARS = 128 * 1024
_MAX_DOCUMENTATION_CHARS = 16384
_MAX_SECTION_CHARS = 8192
_MAX_EXAMPLE_CHARS = 4096
_MAX_EXAMPLES = 4
_MAX_SEARCH_CANDIDATES = 500
_MAX_SEARCH_INDEX_BYTES = 8 * 1024 * 1024
_PAGE_INDEX_CACHE_SIZE = 16
_BLOCK_TAGS = {"br", "dd", "div", "dt", "li", "p", "pre", "tr"}
_API_ANCHOR_RE = re.compile(
    rb"<dt\b(?=[^>]*\bid\s*=\s*(['\"])(ansa\.[^'\"]+)\1)[^>]*>", re.IGNORECASE
)
_PAGE_INDEX_CACHE = OrderedDict()
_SEARCH_INDEX_CACHE = OrderedDict()
_STUB_CACHE = OrderedDict()


class _TextParts(list):
    def __init__(self):
        super().__init__()
        self.length = 0


def _within(root, path):
    try:
        return os.path.normcase(os.path.commonpath((str(root), str(path)))) == os.path.normcase(str(root))
    except ValueError:
        return False


def _inventory_version(docs_root):
    try:
        with (docs_root / "objects.inv").open("rb") as stream:
            for _ in range(4):
                line = stream.readline(4097)
                if len(line) > 4096:
                    return None
                decoded = line.decode("utf-8", "replace").strip()
                if decoded.casefold().startswith("# version:"):
                    return decoded.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def find_docs_root(version):
    """Find only the local installation whose version equals the running ANSA."""
    if type(version) is not str or not _VERSION_RE.fullmatch(version):
        return None

    installations = []
    executable = Path(sys.executable)
    for parent in (executable, *executable.parents):
        name = parent.name.casefold()
        if name == ("shared_" + version).casefold():
            installations.append(parent.parent / ("ansa_" + version))
        elif name == ("ansa_" + version).casefold():
            installations.append(parent)

    bases = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        bases.append(Path(local_app_data))
    bases.append(Path.home() / "AppData" / "Local")
    installations.extend(
        base / "Apps" / "BETA_CAE_Systems" / ("ansa_" + version)
        for base in bases
    )
    seen = set()
    for installation in installations:
        candidate = installation / "docs" / "extending" / "python_api" / "html"
        try:
            resolved_installation = installation.resolve(strict=True)
            resolved_docs = candidate.resolve(strict=True)
        except OSError:
            continue
        key = os.path.normcase(str(resolved_docs))
        if key in seen:
            continue
        seen.add(key)
        if (not resolved_docs.is_dir() or not _within(resolved_installation, resolved_docs)
                or _inventory_version(resolved_docs) != version):
            continue
        if not (resolved_docs / _GENERATED).is_dir():
            continue
        return resolved_docs
    return None


def _clean_text(parts):
    raw = "".join(parts)
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def _append_limited(parts, value):
    current = getattr(parts, "length", 0)
    remaining = _MAX_CAPTURE_CHARS - current
    if remaining <= 0:
        return len(value) > 0
    kept = value[:remaining]
    parts.append(kept)
    if hasattr(parts, "length"):
        parts.length += len(kept)
    return len(value) > remaining


def _signature_text(parts):
    value = re.sub(r"\s+", " ", "".join(parts)).strip()
    value = value.replace("\u2192", "->")
    value = re.sub(r"\s+([,.)\]])", r"\1", value)
    value = re.sub(r"([([{])\s+", r"\1", value)
    value = re.sub(r",\s*", ", ", value)
    return value


class _APIHTMLParser(HTMLParser):
    def __init__(self, *, target=None, terms=None, max_candidates=_MAX_SEARCH_CANDIDATES):
        super().__init__(convert_charrefs=True)
        self.target = target
        self.terms = tuple(term.casefold() for term in (terms or ()) if term)
        self.max_candidates = max_candidates
        self.current = None
        self.entries = []
        self.total_matches = 0
        self.matched_api_ids = set()
        self.matches_truncated = False
        self.source_truncated = False
        self.signature_active = False
        self.doc_active = False
        self.dd_depth = 0
        self.field_label_parts = None
        self.pending_field = None
        self.field_depth = None
        self.current_field = None
        self.rubric_parts = None
        self.example_pending = False
        self.highlight_depth = 0
        self.example_parts = None
        self.headerlink_active = False

    def _break(self, *, signature=False, field_label=False, rubric=False, example=False):
        targets = []
        if self.current is not None and self.doc_active:
            targets.append(self.current["documentation_parts"])
        if self.field_depth is not None and self.dd_depth >= self.field_depth:
            targets.append(self.current["field_parts"].setdefault(self.current_field, _TextParts()))
        if signature and self.signature_active:
            targets.append(self.current["signature_parts"])
        if field_label and self.field_label_parts is not None:
            targets.append(self.field_label_parts)
        if rubric and self.rubric_parts is not None:
            targets.append(self.rubric_parts)
        if example and self.example_parts is not None:
            targets.append(self.example_parts)
        for parts in targets:
            if parts and not parts[-1].endswith("\n"):
                self.source_truncated |= _append_limited(parts, "\n")

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        api_id = attrs.get("id")
        if tag == "dt" and api_id and api_id.startswith("ansa."):
            if self.current is not None:
                self._finish_current(closed=False)
            self.current = {
                "api": api_id,
                "signature_parts": _TextParts(),
                "documentation_parts": _TextParts(),
                "field_parts": {},
                "examples": [],
                "entry_truncated": False,
            }
            self.signature_active = True
            self.doc_active = False
            self.dd_depth = 0
            self.pending_field = None
            self.field_depth = None
            self.current_field = None
            return

        if self.current is None:
            return
        if tag == "a" and "headerlink" in classes:
            self.headerlink_active = True
            return
        if tag in _BLOCK_TAGS:
            self._break(signature=tag == "br", field_label=True, rubric=True, example=False)
        if tag == "dt" and self.doc_active and classes.intersection({"field-odd", "field-even"}):
            self.field_label_parts = _TextParts()
        if tag == "p" and "rubric" in classes:
            self.rubric_parts = _TextParts()
        if tag == "div":
            if self.example_pending and any(name.startswith("highlight") for name in classes):
                self.highlight_depth = 1
                self.example_pending = False
            elif self.highlight_depth:
                self.highlight_depth += 1
        if tag == "pre" and self.highlight_depth:
            self.example_parts = _TextParts()
        if tag == "dd":
            self.dd_depth += 1
            if not self.doc_active:
                self.doc_active = True
            elif self.pending_field and self.field_depth is None:
                self.current_field = self.pending_field
                self.pending_field = None
                self.field_depth = self.dd_depth
                self.current["field_parts"].setdefault(self.current_field, _TextParts())

    def handle_endtag(self, tag):
        if self.current is None:
            return
        if tag == "a" and self.headerlink_active:
            self.headerlink_active = False
            return
        if tag in _BLOCK_TAGS:
            self._break(signature=tag == "br", field_label=True, rubric=True, example=False)
        if tag == "dt":
            if self.signature_active:
                self.signature_active = False
            elif self.field_label_parts is not None:
                label = _clean_text(self.field_label_parts).rstrip(":").casefold()
                if label in {"parameters", "parameter"}:
                    self.pending_field = "parameters"
                elif label in {"returns", "return"}:
                    self.pending_field = "returns"
                self.field_label_parts = None
        if tag == "p" and self.rubric_parts is not None:
            self.example_pending = _clean_text(self.rubric_parts).casefold().rstrip(":") == "examples"
            self.rubric_parts = None
        if tag == "pre" and self.example_parts is not None:
            example = textwrap.dedent("".join(self.example_parts)).strip()
            if len(example) > _MAX_EXAMPLE_CHARS:
                example = example[:_MAX_EXAMPLE_CHARS]
                self.source_truncated = True
            if example:
                if len(self.current["examples"]) < _MAX_EXAMPLES:
                    self.current["examples"].append(example)
                else:
                    self.source_truncated = True
            self.example_parts = None
        if tag == "div" and self.highlight_depth:
            self.highlight_depth -= 1
        if tag == "dd" and self.dd_depth:
            if self.field_depth == self.dd_depth:
                self.field_depth = None
                self.current_field = None
            self.dd_depth -= 1
            if self.dd_depth == 0:
                self._finish_current(closed=True)

    def handle_data(self, data):
        if self.current is None or self.headerlink_active:
            return
        if self.signature_active:
            self.source_truncated |= _append_limited(self.current["signature_parts"], data)
        if self.doc_active:
            self.source_truncated |= _append_limited(self.current["documentation_parts"], data)
        if self.field_depth is not None and self.dd_depth >= self.field_depth:
            field_parts = self.current["field_parts"].setdefault(self.current_field, _TextParts())
            self.source_truncated |= _append_limited(field_parts, data)
        if self.field_label_parts is not None:
            self.source_truncated |= _append_limited(self.field_label_parts, data)
        if self.rubric_parts is not None:
            self.source_truncated |= _append_limited(self.rubric_parts, data)
        if self.example_parts is not None:
            self.source_truncated |= _append_limited(self.example_parts, data)

    def _finish_current(self, *, closed):
        entry = self.current
        if entry is None:
            return
        signature = _signature_text(entry["signature_parts"])
        qualified_prefix = entry["api"].rsplit(".", 1)[0] + "."
        if signature.casefold().startswith(qualified_prefix.casefold()):
            signature = signature[len(qualified_prefix):].lstrip()
        signature = re.sub(r"\s+\(", "(", signature)
        signature = re.sub(r"\)\s+([A-Za-z_][A-Za-z0-9_.\[\], |]*)$", r") -> \1", signature)
        documentation = _clean_text(entry["documentation_parts"])
        fields = {
            name: _clean_text(parts)
            for name, parts in entry["field_parts"].items()
        }
        record = {
            "api": entry["api"],
            "signature": signature,
            "parameters": fields.get("parameters", ""),
            "returns": fields.get("returns", ""),
            "examples": entry["examples"],
            "documentation": documentation,
            "complete": bool(closed and signature),
            "source_truncated": self.source_truncated or entry["entry_truncated"],
        }
        if self.terms:
            haystack = " ".join((record["api"], signature, documentation)).casefold()
            matched = all(term in haystack for term in self.terms)
        else:
            matched = self.target is None or entry["api"].casefold() == self.target.casefold()
        if matched:
            self.total_matches += 1
            self.matched_api_ids.add(entry["api"])
            if len(self.entries) < self.max_candidates:
                self.entries.append(record)
            else:
                self.matches_truncated = True
        self.current = None
        self.signature_active = False
        self.doc_active = False
        self.dd_depth = 0
        self.field_label_parts = None
        self.pending_field = None
        self.field_depth = None
        self.current_field = None
        self.rubric_parts = None
        self.example_parts = None
        if self.target and entry["api"].casefold() == self.target.casefold():
            self._target_found = True


def _safe_page(docs_root, module):
    if type(module) is not str or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", module):
        return None
    try:
        root = Path(docs_root).resolve(strict=True)
        candidate = (root / _GENERATED / ("ansa." + module + ".html")).resolve(strict=True)
    except OSError:
        return None
    if not _within(root, candidate) or not candidate.is_file():
        return None
    return candidate


def _page_index(docs_root, module):
    """Index module anchors once, so validation and lookups avoid repeated page scans."""
    page = _safe_page(docs_root, module)
    if page is None:
        return None
    try:
        stat = page.stat()
    except OSError:
        return None
    key = (os.path.normcase(str(page)), stat.st_mtime_ns, stat.st_size)
    cached = _PAGE_INDEX_CACHE.get(key)
    if cached is not None:
        _PAGE_INDEX_CACHE.move_to_end(key)
        return cached

    try:
        with page.open("rb") as stream:
            raw = stream.read(_MAX_HTML_BYTES + 1)
    except OSError:
        return None
    file_truncated = len(raw) > _MAX_HTML_BYTES or stat.st_size > _MAX_HTML_BYTES
    raw = raw[:_MAX_HTML_BYTES]
    anchors = []
    for match in _API_ANCHOR_RE.finditer(raw):
        anchors.append((match.group(2).decode("utf-8", "replace").casefold(), match.start()))
    result = {
        "page": page,
        "anchors": anchors,
        "complete": not file_truncated,
        "source_file": page.relative_to(Path(docs_root)).as_posix(),
        "size": min(stat.st_size, _MAX_HTML_BYTES),
    }
    _PAGE_INDEX_CACHE[key] = result
    _PAGE_INDEX_CACHE.move_to_end(key)
    while len(_PAGE_INDEX_CACHE) > _PAGE_INDEX_CACHE_SIZE:
        _PAGE_INDEX_CACHE.popitem(last=False)
    return result


def _search_index(docs_root):
    path = Path(docs_root) / "searchindex.js"
    try:
        path = path.resolve(strict=True)
        root = Path(docs_root).resolve(strict=True)
        if not _within(root, path) or not path.is_file():
            return None
        stat = path.stat()
    except OSError:
        return None
    key = (os.path.normcase(str(path)), stat.st_mtime_ns, stat.st_size)
    cached = _SEARCH_INDEX_CACHE.get(key)
    if cached is not None:
        _SEARCH_INDEX_CACHE.move_to_end(key)
        return cached
    if stat.st_size > _MAX_SEARCH_INDEX_BYTES:
        return None
    try:
        source = path.read_text(encoding="utf-8")
        start, end = source.find("{"), source.rfind("}")
        if start < 0 or end < start:
            return None
        data = json.loads(source[start:end + 1])
        docnames, terms = data["docnames"], data["terms"]
        if not isinstance(docnames, list) or not isinstance(terms, dict):
            return None
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return None
    result = {"docnames": docnames, "terms": terms}
    _SEARCH_INDEX_CACHE[key] = result
    _SEARCH_INDEX_CACHE.move_to_end(key)
    while len(_SEARCH_INDEX_CACHE) > 2:
        _SEARCH_INDEX_CACHE.popitem(last=False)
    return result


def _posting_ids(value):
    if type(value) is int:
        return {value}
    if isinstance(value, list):
        return {item for item in value if type(item) is int}
    return set()


def _indexed_modules(docs_root, modules, terms):
    """Return modules named by Sphinx postings; None means no usable index."""
    index = _search_index(docs_root)
    if index is None:
        return None
    folded_terms = {}
    for term, value in index["terms"].items():
        if isinstance(term, str):
            folded_terms.setdefault(term.casefold(), []).append(value)
    module_by_id = {
        index_id: module
        for index_id, docname in enumerate(index["docnames"])
        for module in modules
        if docname == "reference/api_ref_ansa/generated/ansa." + module
    }
    if not module_by_id:
        return None
    route_terms = [piece for term in terms for piece in re.findall(r"[A-Za-z0-9_]+", term)]
    if not route_terms:
        return None
    candidate_ids = None
    for term in route_terms:
        normalized = term.casefold()
        matched_keys = [key for key in folded_terms
                        if key == normalized or key.startswith(normalized) or normalized.startswith(key)]
        term_ids = set()
        for key in matched_keys:
            for value in folded_terms[key]:
                term_ids.update(_posting_ids(value))
        if not term_ids:
            return []
        candidate_ids = term_ids if candidate_ids is None else candidate_ids.intersection(term_ids)
        if not candidate_ids:
            return []
    if candidate_ids is None:
        return None
    return [module_by_id[index_id] for index_id in sorted(candidate_ids) if index_id in module_by_id]


def _parse_page(docs_root, module, *, target=None, terms=None, max_candidates=_MAX_SEARCH_CANDIDATES):
    page = _safe_page(docs_root, module)
    if page is None:
        return {"entries": [], "total": 0, "complete": False, "source_truncated": False,
                "matches_truncated": False, "source_file": None}
    if target is not None:
        index = _page_index(docs_root, module)
        if index is None:
            return {"entries": [], "total": 0, "complete": False, "source_truncated": False,
                    "matches_truncated": False, "source_file": None}
        folded_target = target.casefold()
        positions = [i for i, (name, _offset) in enumerate(index["anchors"]) if name == folded_target]
        if not positions:
            return {"entries": [], "total": 0, "complete": index["complete"],
                    "source_truncated": not index["complete"], "matches_truncated": False,
                    "source_file": index["source_file"]}
        anchor_index = positions[0]
        offset = index["anchors"][anchor_index][1]
        end = (index["anchors"][anchor_index + 1][1]
               if anchor_index + 1 < len(index["anchors"]) else index["size"])
        try:
            with page.open("rb") as stream:
                stream.seek(offset)
                raw = stream.read(max(0, end - offset))
        except OSError:
            return {"entries": [], "total": 0, "complete": False, "source_truncated": False,
                    "matches_truncated": False, "source_file": index["source_file"]}
        file_truncated = not index["complete"] and end >= _MAX_HTML_BYTES
        source_file = index["source_file"]
    else:
        try:
            size = page.stat().st_size
            if size < 0:
                raise OSError("invalid documentation file size")
            with page.open("rb") as stream:
                raw = stream.read(_MAX_HTML_BYTES + 1)
        except OSError:
            return {"entries": [], "total": 0, "complete": False, "source_truncated": False,
                    "matches_truncated": False, "source_file": page.relative_to(Path(docs_root)).as_posix()}
        file_truncated = len(raw) > _MAX_HTML_BYTES or size > _MAX_HTML_BYTES
        raw = raw[:_MAX_HTML_BYTES]
        source_file = page.relative_to(Path(docs_root)).as_posix()
    parser = _APIHTMLParser(target=target, terms=terms, max_candidates=max_candidates)
    try:
        parser.feed(raw.decode("utf-8", "replace"))
        parser.close()
        if parser.current is not None:
            parser._finish_current(closed=False)
    except Exception:
        return {"entries": parser.entries, "total": parser.total_matches, "complete": False,
                "source_truncated": True, "matches_truncated": parser.matches_truncated,
                "source_file": source_file, "api_ids": sorted(parser.matched_api_ids)}
    return {
        "entries": parser.entries,
        "total": parser.total_matches,
        "complete": not file_truncated,
        "source_truncated": file_truncated or parser.source_truncated,
        "matches_truncated": parser.matches_truncated,
        "source_file": source_file,
        "api_ids": sorted(parser.matched_api_ids),
    }


class _SourceLines:
    """AST offsets use UTF-8 byte columns; split the source exactly once."""
    def __init__(self, source):
        self.lines = source.encode("utf-8").splitlines(keepends=True)

    def piece(self, node):
        if node is None or not hasattr(node, "end_lineno"):
            return ""
        start, end = node.lineno - 1, node.end_lineno - 1
        if not 0 <= start <= end < len(self.lines):
            return ""
        if start == end:
            raw = self.lines[start][node.col_offset:node.end_col_offset]
        else:
            raw = (self.lines[start][node.col_offset:] + b"".join(self.lines[start + 1:end])
                   + self.lines[end][:node.end_col_offset])
        return raw.decode("utf-8")


def _source_piece(node, source):
    return source.piece(node)


def _argument_text(argument, default, source):
    value = argument.arg
    if argument.annotation is not None:
        value += ": " + _source_piece(argument.annotation, source)
    if default is not None:
        value += "=" + _source_piece(default, source)
    return value


def _format_stub_signature(node, source):
    args = node.args
    positional = list(args.posonlyargs) + list(args.args)
    defaults_start = len(positional) - len(args.defaults)
    values = []
    for index, argument in enumerate(positional):
        if args.posonlyargs and index == len(args.posonlyargs):
            values.append("/")
        default = args.defaults[index - defaults_start] if index >= defaults_start else None
        values.append(_argument_text(argument, default, source))
    if args.posonlyargs and len(args.posonlyargs) == len(positional):
        values.append("/")
    if args.vararg is not None:
        values.append("*" + _argument_text(args.vararg, None, source))
    elif args.kwonlyargs:
        values.append("*")
    for argument, default in zip(args.kwonlyargs, args.kw_defaults):
        values.append(_argument_text(argument, default, source))
    if args.kwarg is not None:
        values.append("**" + _argument_text(args.kwarg, None, source))
    signature = node.name + "(" + ", ".join(values) + ")"
    if node.returns is not None:
        signature += " -> " + _source_piece(node.returns, source)
    return signature


class _StubCollector(ast.NodeVisitor):
    def __init__(self, module, source):
        self.module = module
        self.source = _SourceLines(source)
        self.scopes = []
        self.entries = {}

    def _qualified(self, name):
        return "ansa." + self.module + ("." + ".".join(self.scopes) if self.scopes else "") + "." + name

    def visit_ClassDef(self, node):
        qualified = self._qualified(node.name)
        self.entries[qualified] = {
            "api": qualified, "signature": node.name + "(...) ",
            "parameters": "", "returns": "", "examples": [],
            "documentation": ast.get_docstring(node, clean=True) or "",
            "complete": True, "source_truncated": False,
        }
        self.scopes.append(node.name)
        for item in node.body:
            self.visit(item)
        self.scopes.pop()

    def visit_FunctionDef(self, node):
        qualified = self._qualified(node.name)
        signature = _format_stub_signature(node, self.source)
        self.entries[qualified] = {
            "api": qualified, "signature": signature,
            "parameters": signature[signature.find("(") + 1:signature.rfind(")")],
            "returns": _source_piece(node.returns, self.source) if node.returns is not None else "",
            "examples": [],
            "documentation": ast.get_docstring(node, clean=True) or "",
            "complete": True, "source_truncated": False,
        }

    visit_AsyncFunctionDef = visit_FunctionDef


def _stub_entries(docs_root, module):
    stub_root = Path(docs_root) / _STUBS
    candidates = (stub_root / (module + ".py"), stub_root / module / "__init__.py")
    for candidate in candidates:
        try:
            root = Path(docs_root).resolve(strict=True)
            path = candidate.resolve(strict=True)
        except OSError:
            continue
        if not _within(root, path) or not path.is_file():
            continue
        try:
            stat = path.stat()
            if stat.st_size > _MAX_STUB_BYTES:
                return {}, False
            key = (os.path.normcase(str(path)), stat.st_mtime_ns, stat.st_size)
            cached = _STUB_CACHE.get(key)
            if cached is not None:
                _STUB_CACHE.move_to_end(key)
                return cached
            source = path.read_text(encoding="utf-8")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(source, filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            return {}, False
        collector = _StubCollector(module, source)
        for item in tree.body:
            collector.visit(item)
        for entry in collector.entries.values():
            entry["source_file"] = path.relative_to(root).as_posix()
        result = (collector.entries, True)
        _STUB_CACHE[key] = result
        _STUB_CACHE.move_to_end(key)
        while len(_STUB_CACHE) > 4:
            _STUB_CACHE.popitem(last=False)
        return result
    return {}, False


def _decorate_html_record(record, docs_root, version, source_file):
    signature_truncated = len(record["signature"]) > 2048
    documentation, documentation_truncated = record["documentation"][:_MAX_DOCUMENTATION_CHARS], (
        len(record["documentation"]) > _MAX_DOCUMENTATION_CHARS
    )
    parameters, parameters_truncated = record["parameters"][:_MAX_SECTION_CHARS], (
        len(record["parameters"]) > _MAX_SECTION_CHARS
    )
    returns, returns_truncated = record["returns"][:_MAX_SECTION_CHARS], (
        len(record["returns"]) > _MAX_SECTION_CHARS
    )
    examples = []
    examples_truncated = len(record["examples"]) > _MAX_EXAMPLES
    for example in record["examples"][:_MAX_EXAMPLES]:
        examples.append(example[:_MAX_EXAMPLE_CHARS])
        examples_truncated |= len(example) > _MAX_EXAMPLE_CHARS
    return {
        "api": record["api"],
        "signature": record["signature"][:2048],
        "parameters": parameters,
        "returns": returns,
        "examples": examples,
        "documentation": documentation,
        "source": "BETA CAE official local ANSA Python HTML documentation",
        "source_type": "official_html",
        "source_file": source_file,
        "source_version": version,
        "complete": bool(record["complete"] and not record["source_truncated"]
                          and not documentation_truncated and not signature_truncated
                          and not parameters_truncated and not returns_truncated and not examples_truncated),
        "source_truncated": bool(record["source_truncated"] or documentation_truncated
                                  or signature_truncated or parameters_truncated or returns_truncated or examples_truncated),
    }


def lookup(path, version, modules):
    if type(path) is not str or not path.startswith("ansa."):
        return None
    parts = path.split(".")
    if len(parts) < 3 or parts[1] not in modules:
        return None
    docs_root = find_docs_root(version)
    if docs_root is None:
        return None
    module = parts[1]
    result = _parse_page(docs_root, module, target=path)
    record = next((item for item in result["entries"] if item["api"].casefold() == path.casefold()), None)
    if record is not None:
        return _decorate_html_record(record, docs_root, version, result["source_file"])
    if result["complete"]:
        return None
    stubs, _complete = _stub_entries(docs_root, module)
    record = next((item for name, item in stubs.items() if name.casefold() == path.casefold()), None)
    if record is None:
        return None
    return _decorate_stub_record(record, docs_root, module, version)


def api_exists(path, version, modules):
    """Return True/False for an authoritative local symbol, or None if unavailable."""
    if type(path) is not str or not path.startswith("ansa."):
        return None
    parts = path.split(".")
    if len(parts) < 3 or parts[1] not in modules:
        return None
    docs_root = find_docs_root(version)
    if docs_root is None:
        return None
    module = parts[1]
    index = _page_index(docs_root, module)
    if index is not None:
        if any(name == path.casefold() for name, _offset in index["anchors"]):
            return True
        if index["complete"]:
            return False
    stubs, stub_complete = _stub_entries(docs_root, module)
    if any(name.casefold() == path.casefold() for name in stubs):
        return True
    if index is not None and index["complete"]:
        return False
    if stub_complete:
        return False
    return None


def _decorate_stub_record(record, docs_root, module, version):
    record = dict(record)
    if not record["examples"]:
        examples = re.search(r"(?ms)^Examples?\s*\n[-=]+\s*\n(.*?)(?=\n[A-Z][^\n]+\n[-=]+\s*\n|\Z)", record["documentation"])
        if examples:
            record["examples"] = [examples.group(1).strip()]
    source_file = record.get("source_file", (_STUBS / (module + ".py")).as_posix())
    decorated = _decorate_html_record(record, docs_root, version, source_file)
    return {
        **decorated,
        "source": "BETA CAE official local ANSA Python autocomplete stub",
        "source_type": "official_static_stub",
        "complete": bool(decorated["complete"] and record["documentation"]),
    }


def search(query, modules, version, limit):
    docs_root = find_docs_root(version)
    if docs_root is None:
        return None
    terms = query.casefold().split()
    indexed_modules = _indexed_modules(docs_root, modules, terms)
    if indexed_modules == []:
        return {
            "total": 0, "matches": [],
            "source": "BETA CAE official local ANSA Python HTML/static documentation",
            "source_type": "official_help", "complete": True,
            "source_truncated": False, "matches_truncated": False,
        }
    if indexed_modules is not None:
        modules = tuple(indexed_modules)
    records = []
    matched_ids = set()
    total = 0
    complete = True
    source_truncated = False
    matches_truncated = False
    for module in modules:
        result = _parse_page(docs_root, module, terms=terms)
        complete &= result["complete"]
        source_truncated |= result["source_truncated"]
        matches_truncated |= result["matches_truncated"]
        total += result["total"]
        matched_ids.update(result.get("api_ids", ()))
        for record in result["entries"]:
            record["source_file"] = result["source_file"]
            records.append(_decorate_html_record(record, docs_root, version, result["source_file"]))
        if result["source_file"] is None:
            stubs, stub_complete = _stub_entries(docs_root, module)
            complete = False
            for record in stubs.values():
                haystack = " ".join((record["api"], record["signature"], record["documentation"])).casefold()
                if all(term in haystack for term in terms):
                    total += 1
                    matched_ids.add(record["api"])
                    records.append(_decorate_stub_record(record, docs_root, module, version))
                    if not stub_complete:
                        source_truncated = True
    records.sort(key=lambda item: (
        not all(term in item["api"].casefold() for term in terms), item["api"].casefold()
    ))
    matches_truncated |= total > limit
    return {
        "total": total,
        "matches": records[:limit],
        "source": "BETA CAE official local ANSA Python HTML/static documentation",
        "source_type": "official_help",
        "complete": bool(complete and not source_truncated),
        "source_truncated": source_truncated,
        "matches_truncated": matches_truncated,
        "_api_ids": sorted(matched_ids),
    }
