"""
Pycln AST utility.
"""
import ast
import os
from dataclasses import dataclass
from enum import Enum, unique
from importlib.util import find_spec
from pathlib import Path
from typing import List, Set, Tuple, Union

from _ast import AST

from . import pathu
from .exceptions import (
    ReadPermissionError,
    UnexpandableImportStar,
    UnparsableFile,
    WritePermissionError,
)

# Constants.
DOT = "."
STAR = "*"
EMPTY = ""


@dataclass
class ImportStats:

    """Import statements statistics."""

    import_: Set[ast.Import]
    from_: Set[ast.ImportFrom]

    def __iter__(self):
        return iter([self.import_, self.from_])


class ImportAnalyzer(ast.NodeVisitor):

    """AST import statements analyzer.

    >>> import ast
    >>> with open("source.py", "r") as source:
    >>>     tree = ast.parse(source.read())
    >>> analyzer = ImportAnalyzer()
    >>> analyzer.visit(tree)
    >>> stats = analyzer.get_stats()
    >>> analyzer.reset_stats()
    """

    def __init__(self, *args, **kwargs):
        super(ImportAnalyzer, self).__init__(*args, **kwargs)
        self.__stats = ImportStats(set(), set())

    def visit_Import(self, node: ast.Import):
        self.__stats.import_.add(node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        self.__stats.from_.add(node)
        self.generic_visit(node)

    def get_stats(self) -> ImportStats:
        return self.__stats

    def reset_stats(self):
        self.__stats = ImportStats(set(), set())


@dataclass
class SourceStats:

    """Source code (`ast.Name` & `ast.Attribute`) statistics."""

    name_: Set[str]
    attr_: Set[str]

    def __iter__(self):
        return iter([self.name_, self.attr_])


class SourceAnalyzer(ast.NodeVisitor):

    """AST souce code objects analyzer.

    >>> import ast
    >>> with open("source.py", "r") as source:
    >>>     tree = ast.parse(source.read())
    >>> analyzer = SourceAnalyzer()
    >>> analyzer.visit(tree)
    >>> stats = analyzer.get_stats()
    >>> analyzer.reset_stats()
    """

    def __init__(self, *args, **kwargs):
        super(SourceAnalyzer, self).__init__(*args, **kwargs)
        self.__stats = SourceStats(set(), set())

    def visit_Name(self, node: ast.Name):
        self.__stats.name_.add(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Name):
        self.__stats.attr_.add(node.attr)
        self.generic_visit(node)

    def get_stats(self) -> SourceStats:
        return self.__stats

    def reset_stats(self):
        self.__stats = SourceStats(set(), set())


class ImportablesAnalyzer(ast.NodeVisitor):

    """Get set of all importable names from given `ast.Module`.

    >>> import ast
    >>> source = "source.py"
    >>> with open(source, "r") as sourcef:
    >>>     tree = ast.parse(sourcef.read())
    >>> analyzer = ImportablesAnalyzer()
    >>> analyzer.source = source
    >>> analyzer.visit(tree)
    >>> stats = analyzer.get_stats()
    """

    @staticmethod
    def handle_c_libs_importables(node: ast.ImportFrom) -> Set[str]:
        """
        Handle libs written in C or built-in CPython.

        :param node: an `ast.ImportFrom` object.
        :returns: set of importables.
        :raises ModuleNotFoundError: when we can't find the spec of the module and/or can't create the module.
        """
        level_dots = DOT * node.level
        spec = find_spec(node.module, level_dots if level_dots else None)

        if spec:
            module = spec.loader.create_module(spec)

            if module:
                return set(dir(module))

        raise ModuleNotFoundError(name=node.module)

    def __init__(self, *args, **kwargs):
        super(ImportablesAnalyzer, self).__init__(*args, **kwargs)
        self.__not_importables: Set[ast.Name] = set()
        self.__stats = set()
        self.source = None

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            name = alias.asname if alias.asname else alias.name
            self.__stats.add(name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        try:
            if node.names[0].name == STAR:
                # Expand import star if possible.
                node = expand_import_star(node, self.source)
            for alias in node.names:
                name = alias.asname if alias.asname else alias.name
                self.__stats.add(name)
        except UnexpandableImportStar:
            # * We shouldn't do anything because it's not importable.
            pass
        finally:
            self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self.__stats.add(node.name)

        # Compute function not-importables.
        for node_ in ast.iter_child_nodes(node):

            if isinstance(node_, ast.Assign):

                for target in node_.targets:
                    self.__not_importables.add(target)

        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef):
        self.__stats.add(node.name)

        # Compute class not-importables.
        for node_ in ast.iter_child_nodes(node):

            if isinstance(node_, ast.Assign):

                for target in node_.targets:
                    self.__not_importables.add(target)

        self.generic_visit(node)

    def visit_Name(self, node: ast.Name):
        if isinstance(node.ctx, ast.Store):
            # Except not-importables.
            if node not in self.__not_importables:
                self.__stats.add(node.id)
        self.generic_visit(node)

    def get_stats(self) -> Set[str]:
        return self.__stats


@unique
class HasSideEffects(Enum):
    YES = 1
    MAYBE = 0.5
    NO = 0

    # Just in case an exception has raised
    # while parsing a file.
    NOT_KNOWN = -1


class SideEffectsAnalyzer(ast.NodeVisitor):

    """Get all side effects nodes from given `ast.Module`.

    >>> import ast
    >>> source = "source.py"
    >>> with open(source, "r") as sourcef:
    >>>     tree = ast.parse(sourcef.read())
    >>> analyzer = SideEffectsAnalyzer()
    >>> analyzer.visit(tree)
    >>> stats = analyzer.has_side_effects()
    >>> analyzer.reset()
    """

    def __init__(self, *args, **kwargs):
        super(SideEffectsAnalyzer, self).__init__(*args, **kwargs)
        self.__not_side_effect: Set[ast.Call] = set()
        self.__has_side_effects = HasSideEffects.NO

    def visit_FunctionDef(self, node: ast.FunctionDef):
        # Mark any call inside a function as not-side-effect.
        for node_ in ast.iter_child_nodes(node):

            if isinstance(node_, ast.Expr):
                if isinstance(node_.value, ast.Call):
                    self.__not_side_effect.add(node_.value)

        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef):
        # Mark any call inside a class as not-side-effect.
        for node_ in ast.iter_child_nodes(node):

            if isinstance(node_, ast.Expr):
                if isinstance(node_.value, ast.Call):
                    self.__not_side_effect.add(node_.value)

        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        if node not in self.__not_side_effect:
            self.__has_side_effects = HasSideEffects.YES
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import):
        for alias in node.names:

            if alias.name in pathu.get_standard_lib_names():
                continue

            if alias.name in pathu.IMPORTS_WITH_SIDE_EFFECTS:
                self.__has_side_effects = HasSideEffects.YES
                break

            self.__has_side_effects = HasSideEffects.MAYBE

        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        for alias in node.names:

            if alias.name in pathu.get_standard_lib_names():
                continue

            if alias.name in pathu.IMPORTS_WITH_SIDE_EFFECTS:
                self.__has_side_effects = HasSideEffects.YES
                break

            self.__has_side_effects = HasSideEffects.MAYBE

        self.generic_visit(node)

    def has_side_effects(self) -> HasSideEffects:
        return self.__has_side_effects

    def reset(self):
        self.__has_side_effects = HasSideEffects.NO

    def generic_visit(self, node):
        """Called if no explicit visitor function exists for a node.
        
        (Override)
        """
        # Continue visiting if only if there's no know side effect.
        if self.__has_side_effects != HasSideEffects.YES:
            for field, value in ast.iter_fields(node):
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, AST):
                            self.visit(item)
                elif isinstance(value, AST):
                    self.visit(value)


def expand_import_star(node: ast.ImportFrom, source: Path) -> ast.ImportFrom:
    """Expand import star statement, replace the `*` with a list of ast.alias.
    
    :param node: an `ast.ImportFrom` node to has a '*' as `alias.name`.
    :param source: where the node has imported.
    :returns: expanded `ast.ImportFrom`.
    :raises UnexpandableImportStar: when `ReadPermissionError` or `UnparsableFile` or `ModuleNotFoundError` raised.
    """
    module_path = pathu.get_import_from_path(source, STAR, node.module, node.level)

    importables: Set[str] = set()

    try:
        if module_path:
            tree = get_file_ast(module_path, permissions=(os.R_OK,))

            analyzer = ImportablesAnalyzer()
            analyzer.source = source
            analyzer.visit(tree)
            importables = analyzer.get_stats()

        else:
            importables = ImportablesAnalyzer.handle_c_libs_importables(node)
    except (ReadPermissionError, UnparsableFile, ModuleNotFoundError) as err:
        msg = err.msg if err.msg else "module not found!"
        raise UnexpandableImportStar(source, node.lineno, node.col_offset, msg)

    # Create `ast.alias` for each name.
    node.names.clear()
    for name in importables:
        node.names.append(ast.alias(name=name, asname=None))

    return node


def remove_useless_passes(source_lines: List[str]) -> List[str]:
    """Remove any useless `pass`.

    :param source_lines: source code lines to check.
    :returns: clean source lines.
    """
    tree = ast.parse(EMPTY.join(source_lines))

    for parent in ast.walk(tree):

        try:
            if parent.__dict__.get("body", None):
                body_len = len(parent.body)
            else:
                continue
        except TypeError:
            continue

        for child in ast.iter_child_nodes(parent):

            if isinstance(child, ast.Pass):

                if body_len > 1:
                    body_len -= 1
                    source_lines[child.lineno - 1] = EMPTY

    return source_lines


def get_file_ast(
    source: Path, get_lines: bool = False, permissions: tuple = (os.R_OK, os.W_OK)
) -> Union[ast.Module, Tuple[ast.Module, List[str]]]:
    """Parse source file AST.

    :param source: source to read.
    :param get_lines: if true the source file lines will be returned.
    :param permissions: tuple of permissions to check, supported permissions (os.R_OK, os.W_OK).
    :returns: source file AST (`ast.Module`). Also source code lines if get_lines.
    :raises ReadPermissionError: when `os.R_OK` in permissions and the source does not have read permission.
    :raises WritePermissionError: when `os.W_OK` in permissions and the source does not have write permission.
    :raises UnparsableFile: if the compiled source is invalid, or the source contains null bytes.
    """
    # Check these permissions before openinig the file.
    for permission in permissions:
        if not os.access(source, permission):
            if permission is os.R_OK:
                raise ReadPermissionError(13, "Permission denied [READ]", source)
            elif permission is os.W_OK:
                raise WritePermissionError(13, "Permission denied [WRITE]", source)

    with open(source, "r") as sfile:
        lines = sfile.readlines() if get_lines else []
        content = EMPTY.join(lines) if get_lines else sfile.read()

    try:
        try:
            # Include type_comments ~> Python >=3.8 .
            # For more information https://www.python.org/dev/peps/pep-0526/ .
            tree = ast.parse(content, type_comments=True)
        except TypeError:
            tree = ast.parse(content)
    except (SyntaxError, ValueError) as exception:
        raise UnparsableFile(source, exception)

    return (tree, lines) if get_lines else tree
