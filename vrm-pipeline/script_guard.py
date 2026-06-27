"""
script_guard.py
===============
Static AST-based security gate for AI-generated Blender Python scripts.

Why this exists
---------------
``generate.py`` asks Gemini for a complete ``bpy`` Python script and then runs
it with ``blender --background --python <script>`` without any inspection.
That is arbitrary code execution at host privilege.  A prompt / image injection
could make Gemini emit ``import os; os.system(...)`` and own the machine.

This module inspects a script **without executing it** using only the ``ast``
standard library.  It must NEVER ``exec``/``eval``/``import`` the script under
analysis.

Violation kinds (stable values)
--------------------------------
``"import"``        – a dangerous top-level module is imported
                      (os, subprocess, socket, and their sub-modules /
                       ``from X import ...`` variants).
``"dynamic_exec"``  – a call to eval / exec / compile / __import__.
``"file_write"``    – an ``open()`` call in a write / append / exclusive /
                      update mode whose path cannot be statically proven to
                      reside inside the allowed ``output_dir``.
``"syntax_error"``  – the script cannot be parsed at all (fail-safe: reject).

Public API
----------
``GuardViolation``        – structured record (dataclass).
``UntrustedScriptError``  – Exception carrying the violation list.
``guard_script(text, output_dir=None) -> list[GuardViolation]``
``assert_script_safe(text, output_dir=None) -> None``
"""

import ast
import os
from dataclasses import dataclass, field
from typing import List, Optional


# ---------------------------------------------------------------------------
# Violation record
# ---------------------------------------------------------------------------

@dataclass
class GuardViolation:
    """A single policy violation detected in a script.

    Attributes
    ----------
    kind : str
        Stable category string.  One of ``"import"``, ``"dynamic_exec"``,
        ``"file_write"``, ``"syntax_error"``.
    detail : str
        Human-readable description of what was found and why it is rejected.
    lineno : int or None
        Source line number (1-based) from the AST node, or ``None`` when the
        location cannot be determined (e.g. parse failure).
    """

    kind: str
    detail: str
    lineno: Optional[int] = field(default=None)

    def __str__(self) -> str:
        loc = f"line {self.lineno}" if self.lineno is not None else "unknown line"
        return f"[{self.kind}] {loc}: {self.detail}"

    def __repr__(self) -> str:
        return (
            f"GuardViolation(kind={self.kind!r}, detail={self.detail!r}, "
            f"lineno={self.lineno!r})"
        )


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class UntrustedScriptError(Exception):
    """Raised by ``assert_script_safe`` when the script has policy violations.

    Attributes
    ----------
    violations : list[GuardViolation]
        All violations that were detected.
    """

    def __init__(self, violations: List[GuardViolation]) -> None:
        self.violations = violations
        lines = "\n".join(f"  {v}" for v in violations)
        super().__init__(
            f"{len(violations)} violation(s) found in script:\n{lines}"
        )


# ---------------------------------------------------------------------------
# Dangerous module names
# ---------------------------------------------------------------------------

_DANGEROUS_MODULES = frozenset({"os", "subprocess", "socket"})


def _top_level_module(name: str) -> str:
    """Return the top-level component of a dotted module name."""
    return name.split(".")[0]


# ---------------------------------------------------------------------------
# Write-mode detection helpers
# ---------------------------------------------------------------------------

# Any mode string that contains one of these characters is considered a write.
_WRITE_MODE_CHARS = frozenset("waxW")  # W for 'wb'-like forms; + is update
_WRITE_MODE_PLUS = "+"                 # r+ / a+ etc.


def _is_write_mode(mode_str: str) -> bool:
    """Return True if *mode_str* indicates a writable file open."""
    return bool(set(mode_str) & _WRITE_MODE_CHARS) or _WRITE_MODE_PLUS in mode_str


def _path_inside_output_dir(path_str: str, output_dir: str) -> bool:
    """Return True when *path_str* resolves to a path inside *output_dir*."""
    abs_out = os.path.abspath(output_dir)
    # Resolve relative paths relative to output_dir, absolute ones as-is.
    if os.path.isabs(path_str):
        abs_path = os.path.normpath(path_str)
    else:
        abs_path = os.path.normpath(os.path.join(abs_out, path_str))
    try:
        common = os.path.commonpath([abs_out, abs_path])
    except ValueError:
        # Happens on Windows when drives differ; treat as outside.
        return False
    return common == abs_out


# ---------------------------------------------------------------------------
# AST visitor
# ---------------------------------------------------------------------------

class _GuardVisitor(ast.NodeVisitor):
    """Walk the AST and collect ``GuardViolation`` instances."""

    def __init__(self, output_dir: Optional[str]) -> None:
        self._output_dir = output_dir
        self.violations: List[GuardViolation] = []

    # --- imports -----------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        """Detect ``import os``, ``import os.path``, ``import subprocess``, …"""
        for alias in node.names:
            top = _top_level_module(alias.name)
            if top in _DANGEROUS_MODULES:
                self.violations.append(GuardViolation(
                    kind="import",
                    detail=f"Dangerous import: '{alias.name}'",
                    lineno=node.lineno,
                ))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Detect ``from os import system``, ``from subprocess import run``, …"""
        module_name = node.module or ""
        top = _top_level_module(module_name)
        if top in _DANGEROUS_MODULES:
            names_str = ", ".join(
                alias.name for alias in node.names
            )
            self.violations.append(GuardViolation(
                kind="import",
                detail=(
                    f"Dangerous import: 'from {module_name} import {names_str}'"
                ),
                lineno=node.lineno,
            ))
        self.generic_visit(node)

    # --- dynamic exec ------------------------------------------------------

    _DYNAMIC_EXEC_NAMES = frozenset({"eval", "exec", "compile", "__import__"})

    def visit_Call(self, node: ast.Call) -> None:
        """Detect eval/exec/compile/__import__ calls and dangerous open() calls."""
        # --- dynamic exec builtins ---
        if isinstance(node.func, ast.Name):
            if node.func.id in self._DYNAMIC_EXEC_NAMES:
                self.violations.append(GuardViolation(
                    kind="dynamic_exec",
                    detail=f"Dangerous call: '{node.func.id}(...)'",
                    lineno=node.lineno,
                ))

        # --- open() write detection ---
        if self._is_open_call(node):
            self._check_open_call(node)

        self.generic_visit(node)

    @staticmethod
    def _is_open_call(node: ast.Call) -> bool:
        """Return True when the call looks like a plain ``open(...)`` builtin."""
        func = node.func
        if isinstance(func, ast.Name) and func.id == "open":
            return True
        # Also catch ``builtins.open(...)`` if someone does that.
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "open"
            and isinstance(func.value, ast.Name)
            and func.value.id == "builtins"
        ):
            return True
        return False

    def _check_open_call(self, node: ast.Call) -> None:
        """Analyse an ``open(path, mode, ...)`` call for write-mode violations."""
        args = node.args
        keywords = node.keywords

        # --- Determine mode -------------------------------------------------
        # Positional: open(path, mode, ...)
        if len(args) >= 2:
            mode_node = args[1]
        else:
            # Keyword: open(path, mode='w') or open(file=path, mode='w')
            mode_node = None
            for kw in keywords:
                if kw.arg == "mode":
                    mode_node = kw.value
                    break

        if mode_node is None:
            # No mode supplied → default is 'r' → read-only, safe.
            return

        if not isinstance(mode_node, ast.Constant) or not isinstance(mode_node.value, str):
            # Mode is a variable/expression; we cannot determine it statically.
            # Be conservative and treat as potentially writing.
            # But actually, if mode is not a literal we can't rule out write.
            # Flag it as a violation.
            self.violations.append(GuardViolation(
                kind="file_write",
                detail=(
                    "open() call with non-literal mode; cannot statically verify "
                    "it is read-only"
                ),
                lineno=node.lineno,
            ))
            return

        mode_str = mode_node.value
        if not _is_write_mode(mode_str):
            # Read-only mode, safe.
            return

        # It is a write-mode open.  Now check the path.
        if len(args) >= 1:
            path_node = args[0]
        else:
            # open(file=..., mode='w') keyword form
            path_node = None
            for kw in keywords:
                if kw.arg in ("file",):
                    path_node = kw.value
                    break

        if self._output_dir is None:
            # No output_dir given; cannot prove safety of any write.
            self.violations.append(GuardViolation(
                kind="file_write",
                detail=(
                    f"open() in write mode {mode_str!r} but no output_dir provided; "
                    "cannot verify path containment"
                ),
                lineno=node.lineno,
            ))
            return

        # output_dir is given; check static containment.
        if path_node is None or not isinstance(path_node, ast.Constant) or not isinstance(path_node.value, str):
            self.violations.append(GuardViolation(
                kind="file_write",
                detail=(
                    f"open() in write mode {mode_str!r} with non-literal path; "
                    "cannot statically verify containment in output_dir"
                ),
                lineno=node.lineno,
            ))
            return

        path_str = path_node.value
        if not _path_inside_output_dir(path_str, self._output_dir):
            self.violations.append(GuardViolation(
                kind="file_write",
                detail=(
                    f"open({path_str!r}, {mode_str!r}) writes outside output_dir "
                    f"{self._output_dir!r}"
                ),
                lineno=node.lineno,
            ))


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def guard_script(
    script_text: str,
    output_dir: Optional[str] = None,
) -> List[GuardViolation]:
    """Parse *script_text* and return all policy violations found.

    This function NEVER executes or imports the script.  It uses ``ast.parse``
    to build a parse tree and then walks it looking for dangerous constructs.

    Parameters
    ----------
    script_text : str
        Full source text of the script to analyse.
    output_dir : str or None
        If provided, ``open()`` calls in write mode are allowed only when the
        first argument is a string literal that resolves to a path inside this
        directory.  When ``None``, ALL write-mode ``open()`` calls are flagged
        because containment cannot be proved.

    Returns
    -------
    list[GuardViolation]
        Empty list means the script passes all checks.  Otherwise every entry
        describes one violation; all violations are collected before returning
        (no early exit).

    Notes
    -----
    The function is designed to be robust: it never raises on a syntactically
    valid (even malicious) script.  A ``SyntaxError`` from ``ast.parse`` is
    caught and returned as a ``GuardViolation`` with kind ``"syntax_error"``.
    """
    try:
        tree = ast.parse(script_text)
    except SyntaxError as exc:
        return [
            GuardViolation(
                kind="syntax_error",
                detail=f"Script could not be parsed: {exc}",
                lineno=getattr(exc, "lineno", None),
            )
        ]

    visitor = _GuardVisitor(output_dir=output_dir)
    visitor.visit(tree)
    return visitor.violations


def assert_script_safe(
    script_text: str,
    output_dir: Optional[str] = None,
) -> None:
    """Assert that *script_text* has no policy violations.

    Parameters
    ----------
    script_text : str
        Full source text of the script to analyse.
    output_dir : str or None
        Passed through to :func:`guard_script`.

    Raises
    ------
    UntrustedScriptError
        When one or more violations are detected.  The exception message
        enumerates all violations.
    """
    violations = guard_script(script_text, output_dir=output_dir)
    if violations:
        raise UntrustedScriptError(violations)
