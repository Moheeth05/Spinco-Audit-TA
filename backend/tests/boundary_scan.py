"""Static protection of the Drive storage boundary (Phase 3A.2-E.1): which parts of the application may reach the unchecked metadata view, the
client-level reads and the raw downloads - and how a change that breaks that is noticed.

The rules (all purely syntactic, applied to source text with `ast`; nothing is imported or executed):

  R1  `.info(...)` on anything that is not a logger: the old unchecked-metadata alias no longer exists, and nobody may bring it back by name.
  R2  attribute names that lead to the unchecked view or to the Drive client's reads/writes (`inspect_unchecked`, `get_file`, `get_raw`, `_request`,
      `open_download`, `download_bytes`, ...) outside the Drive package.
  R3  the same names as STRING constants - including constant-folded concatenations such as 'inspect_' + 'unchecked' and f-strings of constants - which
      is what `getattr`, `__dict__[...]` and `operator.attrgetter` need.
  R4  an IMPORT ALLOWLIST: outside the Drive package only `api/storage.py` and `services/central_storage.py` may import from `app.services.drive`, and nobody may import the storage
      module, `GoogleDriveStorage`, `DriveFolders` or `DriveClient`; `importlib` / `__import__` strings that mention the Drive package are refused.
  R5  dynamic-access primitives (`getattr`, `setattr`, `vars`, `eval`, `exec`, `importlib`, `__dict__`, ...) in the one file that may import the Drive
      package.
  R6  `getattr/setattr/hasattr(<something called storage/drive/manager/client>, 'info')`: the one name that cannot be a plain string rule (it is also a logger
      method). A best-effort heuristic; the hard guarantee for `info` is that the method does not exist (a separate test).

Inside the Drive package the same names are FENCED to the files that really need them (INSIDE_FENCE).

This is protection against ACCIDENTAL bypass by an ordinary change. It cannot stop determined indirection (code built at run time from data, a C
extension, ...); the layers that do not depend on syntax are: the unchecked accessor is not an alias of anything else, the application's Drive client is
always guarded, and every serving path re-checks the boundary itself."""

import ast
from pathlib import Path

DRIVE_PACKAGE_PREFIX = "services/drive/"
IMPORT_ALLOWLIST = {"api/storage.py", "services/central_storage.py"}  # the two application modules that talk to the Drive package: the admin storage router and the central storage service (document workflows). Both use only the checked interface and are held to R2/R3/R5 like every other file.

# Names that must not appear (as attributes or as strings) in application code outside the Drive package.
FORBIDDEN_OUTSIDE = {
    "inspect_unchecked", "_require_in_drive", "_download_verified", "_tie_to_record", "confirm_adoptable",
    "get_file", "get_raw", "_request", "open_download", "download_bytes", "list_files", "find_children", "update_file", "upload_file", "create_folder",
    "generate_ids", "get_start_page_token", "list_changes", "get_shared_drive", "get_about", "list_permissions", "write_guard",
}
FORBIDDEN_IMPORT_MODULES = {"app.services.drive.storage"}
FORBIDDEN_IMPORT_NAMES = {"GoogleDriveStorage", "DriveFolders", "DriveClient", "VerifiedDownload"}
DYNAMIC_PRIMITIVES = {"getattr", "setattr", "delattr", "vars", "eval", "exec", "compile", "__import__", "import_module", "attrgetter", "methodcaller"}
DYNAMIC_ATTRIBUTES = {"__dict__", "__getattribute__", "__subclasses__", "__mro__", "__globals__", "__builtins__"}
LOGGER_NAMES = {"logger", "log", "logging", "_logger", "_log", "LOGGER", "LOG"}
STORAGEISH = ("storage", "drive", "manager", "client")

# Inside the Drive package: where each name may be used (everything else is a violation).
INSIDE_FENCE = {
    "inspect_unchecked": {"storage.py", "lifecycle.py"},
    "get_raw": {"client.py", "guard.py"},
    "_request": {"client.py"},
    "get_file": {"client.py", "storage.py"},
    "open_download": {"client.py", "storage.py"},
    "download_bytes": {"client.py", "storage.py"},
}


def _fold(node) -> str | None:
    """The string a constant-only expression evaluates to ('a' + 'b', f'a{...}' with only constants), else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        parts = [_fold(value) if isinstance(value, ast.Constant) else None for value in node.values]
        return "".join(parts) if all(p is not None for p in parts) else None
    return None


def _receiver_name(node: ast.AST) -> str:
    """The right-most plain name of a receiver expression (`self.logger` -> 'logger', `x` -> 'x'); '' when there is none."""
    while isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _is_logger(name: str) -> bool:
    return name in LOGGER_NAMES or name.lower().endswith(("logger", "_log"))


def _text(node: ast.AST) -> str:
    try:
        return ast.unparse(node).lower()
    except Exception:  # noqa: BLE001
        return ""


def scan_source(source: str, relative: str) -> list[str]:
    """Violations in one module of the application (`relative` = its path under app/, with forward slashes). Empty list = clean."""
    relative = relative.replace("\\", "/")
    if relative.startswith(DRIVE_PACKAGE_PREFIX):
        return scan_inside(source, relative)
    tree = ast.parse(source)
    found: list[str] = []
    importer_allowed = relative in IMPORT_ALLOWLIST

    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        where = f"{relative}:{line}"
        # ---- R1 / R2: attributes
        if isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_OUTSIDE:
                found.append(f"{where} R2 attribute .{node.attr}")
            if node.attr == "info" and not _is_logger(_receiver_name(node.value)):
                found.append(f"{where} R1 .info on a non-logger")
            if node.attr in DYNAMIC_ATTRIBUTES and importer_allowed:
                found.append(f"{where} R5 dynamic attribute .{node.attr}")
        # ---- R3: names as strings (also folded)
        folded = _fold(node) if isinstance(node, (ast.Constant, ast.BinOp, ast.JoinedStr)) else None
        if folded is not None:
            if folded in FORBIDDEN_OUTSIDE:
                found.append(f"{where} R3 string {folded!r}")
            if "app.services.drive" in folded or "services/drive" in folded:
                if not importer_allowed:
                    found.append(f"{where} R4 string mentions the Drive package")
        # ---- R4: imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app.services.drive") and (not importer_allowed or alias.name in FORBIDDEN_IMPORT_MODULES):
                    found.append(f"{where} R4 import {alias.name}")
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0 and module in ("app.services", "app") and any(a.name == "drive" for a in node.names) and not importer_allowed:
                found.append(f"{where} R4 from {module} import drive")
            if module.startswith("app.services.drive"):
                if not importer_allowed or module in FORBIDDEN_IMPORT_MODULES:
                    found.append(f"{where} R4 from {module} import ...")
                for alias in node.names:
                    if alias.name in FORBIDDEN_IMPORT_NAMES:
                        found.append(f"{where} R4 imports {alias.name}")
        # ---- R5 / R6: calls
        if isinstance(node, ast.Call):
            fn = node.func
            called = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
            if called in DYNAMIC_PRIMITIVES and importer_allowed:
                found.append(f"{where} R5 dynamic call {called}()")
            if called in ("getattr", "setattr", "hasattr") and len(node.args) >= 2:
                name = _fold(node.args[1])
                if name == "info" and any(word in _text(node.args[0]) for word in STORAGEISH):
                    found.append(f"{where} R6 {called}(<storage-like>, 'info')")
    return sorted(set(found))


def scan_inside(source: str, relative: str) -> list[str]:
    """Violations inside the Drive package: a name used from a file that is not allowed to use it (INSIDE_FENCE) or a call of `.info` on a non-logger."""
    relative = relative.replace("\\", "/")
    filename = relative.rsplit("/", 1)[-1]
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute):
            allowed = INSIDE_FENCE.get(node.attr)
            if allowed is not None and filename not in allowed:
                found.append(f"{relative}:{node.lineno} fence .{node.attr} (allowed only in {sorted(allowed)})")
            if node.attr == "info" and not _is_logger(_receiver_name(node.value)):
                found.append(f"{relative}:{node.lineno} R1 .info on a non-logger")
    return sorted(set(found))


def scan_tree(app_dir: Path) -> list[str]:
    """Every violation in the real application tree (app/)."""
    found: list[str] = []
    for path in sorted(app_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        found.extend(scan_source(path.read_text(encoding="utf-8"), path.relative_to(app_dir).as_posix()))
    return found
