"""Deterministic three-way merges for user-editable Workspace formats.

The current formal file wins when both the human and the proposal changed the
same logical location.  Non-overlapping proposal edits are applied.  The
original proposal remains stored by ``WorkspaceChangeSet`` for audit/recovery.
"""

from __future__ import annotations

import csv
import io
import posixpath
import zipfile
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import PurePosixPath
from xml.etree import ElementTree

from src.api.services.spreadsheet_edit_validation import (
    validate_xlsx_archive_envelope,
)


_SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
ElementTree.register_namespace("", _SHEET_NS)
ElementTree.register_namespace("r", _DOC_REL_NS)


@dataclass(frozen=True)
class AutoMergeResult:
    content: bytes
    strategy: str
    applied_changes: int
    preserved_conflicts: int


@dataclass(frozen=True)
class _LinePatch:
    start: int
    end: int
    replacement: tuple[str, ...]


@dataclass(frozen=True)
class _CellValue:
    formula: str | None
    kind: str | None
    value: str | None


def merge_workspace_bytes(
    filename: str,
    *,
    base: bytes,
    current: bytes,
    proposal: bytes,
) -> AutoMergeResult | None:
    extension = PurePosixPath(filename).suffix.lower()
    if extension in {".md", ".markdown", ".txt"}:
        return _merge_text(base, current, proposal)
    if extension == ".csv":
        return _merge_csv(base, current, proposal)
    if extension == ".xlsx":
        return _merge_xlsx(base, current, proposal)
    return None


def _line_patches(base: list[str], changed: list[str]) -> list[_LinePatch]:
    matcher = SequenceMatcher(a=base, b=changed, autojunk=False)
    return [
        _LinePatch(start, end, tuple(changed[replacement_start:replacement_end]))
        for tag, start, end, replacement_start, replacement_end in matcher.get_opcodes()
        if tag != "equal"
    ]


def _patches_overlap(left: _LinePatch, right: _LinePatch) -> bool:
    left_insert = left.start == left.end
    right_insert = right.start == right.end
    if left_insert and right_insert:
        return left.start == right.start
    if left_insert:
        return right.start <= left.start < right.end
    if right_insert:
        return left.start <= right.start < left.end
    return max(left.start, right.start) < min(left.end, right.end)


def _map_base_boundary(position: int, human_patches: list[_LinePatch]) -> int:
    offset = 0
    for patch in human_patches:
        if patch.end <= position and not (patch.start == patch.end == position):
            offset += len(patch.replacement) - (patch.end - patch.start)
        elif patch.start < position < patch.end:
            raise ValueError("boundary falls inside a human change")
    return position + offset


def _merge_text(base: bytes, current: bytes, proposal: bytes) -> AutoMergeResult | None:
    try:
        base_text = base.decode("utf-8")
        current_text = current.decode("utf-8")
        proposal_text = proposal.decode("utf-8")
    except UnicodeDecodeError:
        return None

    base_lines = base_text.splitlines(keepends=True)
    current_lines = current_text.splitlines(keepends=True)
    proposal_lines = proposal_text.splitlines(keepends=True)
    human_patches = _line_patches(base_lines, current_lines)
    proposal_patches = _line_patches(base_lines, proposal_lines)
    accepted: list[_LinePatch] = []
    conflicts = 0

    for proposal_patch in proposal_patches:
        overlapping = [patch for patch in human_patches if _patches_overlap(proposal_patch, patch)]
        if overlapping:
            identical = any(
                patch.start == proposal_patch.start
                and patch.end == proposal_patch.end
                and patch.replacement == proposal_patch.replacement
                for patch in overlapping
            )
            if not identical:
                conflicts += 1
            continue
        accepted.append(proposal_patch)

    merged = list(current_lines)
    mapped: list[tuple[int, int, tuple[str, ...]]] = []
    for patch in accepted:
        try:
            start = _map_base_boundary(patch.start, human_patches)
            end = _map_base_boundary(patch.end, human_patches)
        except ValueError:
            conflicts += 1
            continue
        mapped.append((start, end, patch.replacement))
    for start, end, replacement in sorted(mapped, reverse=True):
        merged[start:end] = replacement

    return AutoMergeResult(
        content="".join(merged).encode("utf-8"),
        strategy="text-lines",
        applied_changes=len(mapped),
        preserved_conflicts=conflicts,
    )


def _csv_shape(rows: list[list[str]]) -> tuple[int, tuple[int, ...]]:
    return len(rows), tuple(len(row) for row in rows)


def _csv_format(content: bytes) -> tuple[str, str, bool, bool]:
    text = content.decode("utf-8-sig")
    first_line = text.splitlines()[0] if text.splitlines() else ""
    delimiter = max((",", ";", "\t", "|"), key=first_line.count)
    newline = "\r\n" if "\r\n" in text else "\r" if "\r" in text else "\n"
    return delimiter, newline, content.startswith(b"\xef\xbb\xbf"), text.endswith(("\r", "\n"))


def _read_csv(content: bytes) -> tuple[list[list[str]], tuple[str, str, bool, bool]]:
    format_info = _csv_format(content)
    return list(csv.reader(io.StringIO(content.decode("utf-8-sig")), delimiter=format_info[0])), format_info


def _merge_csv(base: bytes, current: bytes, proposal: bytes) -> AutoMergeResult | None:
    try:
        base_rows, _ = _read_csv(base)
        current_rows, current_format = _read_csv(current)
        proposal_rows, _ = _read_csv(proposal)
    except (UnicodeDecodeError, csv.Error):
        return None
    if _csv_shape(base_rows) != _csv_shape(current_rows) or _csv_shape(base_rows) != _csv_shape(proposal_rows):
        return None

    merged = [list(row) for row in current_rows]
    applied = 0
    conflicts = 0
    for row_index, base_row in enumerate(base_rows):
        for column_index, base_value in enumerate(base_row):
            current_value = current_rows[row_index][column_index]
            proposal_value = proposal_rows[row_index][column_index]
            if proposal_value == base_value:
                continue
            if current_value != base_value and current_value != proposal_value:
                conflicts += 1
                continue
            if current_value != proposal_value:
                merged[row_index][column_index] = proposal_value
                applied += 1

    delimiter, newline, bom, trailing_newline = current_format
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=delimiter, lineterminator=newline)
    writer.writerows(merged)
    text = output.getvalue()
    if not trailing_newline:
        text = text.removesuffix(newline)
    encoded = text.encode("utf-8")
    if bom:
        encoded = b"\xef\xbb\xbf" + encoded
    return AutoMergeResult(encoded, "csv-cells", applied, conflicts)


def _resolve_part_path(base_part: str, target: str) -> str:
    if "://" in target:
        raise ValueError("external relationship")
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_part), target)).lstrip("/")


def _xlsx_sheet_parts(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook_path = "xl/workbook.xml"
    workbook = ElementTree.fromstring(archive.read(workbook_path))
    relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        relationship.attrib["Id"]: relationship.attrib["Target"]
        for relationship in relationships.findall(f"{{{_REL_NS}}}Relationship")
    }
    result: list[tuple[str, str]] = []
    for sheet in workbook.findall(f".//{{{_SHEET_NS}}}sheet"):
        relationship_id = sheet.attrib.get(f"{{{_DOC_REL_NS}}}id")
        if not relationship_id or relationship_id not in targets:
            raise ValueError("missing worksheet relationship")
        result.append((sheet.attrib.get("name", ""), _resolve_part_path(workbook_path, targets[relationship_id])))
    return result


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(node.text or "" for node in item.findall(f".//{{{_SHEET_NS}}}t"))
        for item in root.findall(f"{{{_SHEET_NS}}}si")
    ]


def _cell_value(cell: ElementTree.Element, shared: list[str]) -> _CellValue | None:
    formula_node = cell.find(f"{{{_SHEET_NS}}}f")
    value_node = cell.find(f"{{{_SHEET_NS}}}v")
    inline_node = cell.find(f"{{{_SHEET_NS}}}is")
    formula = formula_node.text if formula_node is not None else None
    cell_type = cell.attrib.get("t")
    if inline_node is not None:
        value = "".join(node.text or "" for node in inline_node.findall(f".//{{{_SHEET_NS}}}t"))
        return _CellValue(formula, "string", value)
    raw = value_node.text if value_node is not None else None
    if formula is None and raw is None:
        return None
    if cell_type == "s" and raw is not None:
        try:
            return _CellValue(formula, "string", shared[int(raw)])
        except (ValueError, IndexError):
            raise ValueError("invalid shared string") from None
    if cell_type == "b":
        return _CellValue(formula, "boolean", "1" if raw == "1" else "0")
    if cell_type in {"str", "inlineStr"}:
        return _CellValue(formula, "string", raw or "")
    if cell_type == "e":
        return _CellValue(formula, "error", raw)
    return _CellValue(formula, "number", raw)


def _cell_equal(left: _CellValue | None, right: _CellValue | None) -> bool:
    if left is None or right is None:
        return left is right
    if left.formula is not None or right.formula is not None:
        return left.formula == right.formula
    return left.kind == right.kind and left.value == right.value


def _sheet_cells(root: ElementTree.Element, shared: list[str]) -> dict[str, _CellValue | None]:
    return {
        cell.attrib["r"]: _cell_value(cell, shared)
        for cell in root.findall(f".//{{{_SHEET_NS}}}c")
        if cell.attrib.get("r")
    }


def _cell_position(address: str) -> tuple[int, int]:
    column = 0
    index = 0
    while index < len(address) and address[index].isalpha():
        column = column * 26 + ord(address[index].upper()) - 64
        index += 1
    return int(address[index:]), column


def _expand_sheet_dimension(root: ElementTree.Element, address: str) -> None:
    """Include an inserted cell without shrinking the original formatted range."""
    dimension = root.find(f"{{{_SHEET_NS}}}dimension")
    if dimension is None:
        # dimension is optional; readers infer the range when it is absent.
        return
    bounds = dimension.attrib["ref"].split(":")
    first_row, first_column = _cell_position(bounds[0])
    last_row, last_column = _cell_position(bounds[-1])
    row, column = _cell_position(address)

    def cell_reference(row: int, column: int) -> str:
        letters = ""
        while column:
            column, remainder = divmod(column - 1, 26)
            letters = chr(65 + remainder) + letters
        return f"{letters}{row}"

    first = cell_reference(min(first_row, row), min(first_column, column))
    last = cell_reference(max(last_row, row), max(last_column, column))
    dimension.set("ref", first if first == last else f"{first}:{last}")


def _get_or_create_cell(root: ElementTree.Element, address: str) -> ElementTree.Element:
    existing = root.find(f".//{{{_SHEET_NS}}}c[@r='{address}']")
    if existing is not None:
        return existing
    sheet_data = root.find(f"{{{_SHEET_NS}}}sheetData")
    if sheet_data is None:
        sheet_data = ElementTree.SubElement(root, f"{{{_SHEET_NS}}}sheetData")
    row_number, column_number = _cell_position(address)
    row = next((item for item in sheet_data.findall(f"{{{_SHEET_NS}}}row") if int(item.attrib.get("r", "0")) == row_number), None)
    if row is None:
        row = ElementTree.Element(f"{{{_SHEET_NS}}}row", {"r": str(row_number)})
        rows = list(sheet_data)
        insert_at = next((index for index, item in enumerate(rows) if int(item.attrib.get("r", "0")) > row_number), len(rows))
        sheet_data.insert(insert_at, row)
    cell = ElementTree.Element(f"{{{_SHEET_NS}}}c", {"r": address})
    cells = list(row)
    insert_at = next(
        (index for index, item in enumerate(cells) if _cell_position(item.attrib.get("r", "A1"))[1] > column_number),
        len(cells),
    )
    row.insert(insert_at, cell)
    _expand_sheet_dimension(root, address)
    return cell


def _set_cell_value(root: ElementTree.Element, address: str, value: _CellValue | None) -> None:
    cell = root.find(f".//{{{_SHEET_NS}}}c[@r='{address}']")
    if cell is None and value is None:
        return
    cell = cell if cell is not None else _get_or_create_cell(root, address)
    for child in list(cell):
        if child.tag in {f"{{{_SHEET_NS}}}f", f"{{{_SHEET_NS}}}v", f"{{{_SHEET_NS}}}is"}:
            cell.remove(child)
    cell.attrib.pop("t", None)
    if value is None:
        if set(cell.attrib) == {"r"}:
            parent = next((row for row in root.findall(f".//{{{_SHEET_NS}}}row") if cell in list(row)), None)
            if parent is not None:
                parent.remove(cell)
        return
    if value.formula is not None:
        formula = ElementTree.SubElement(cell, f"{{{_SHEET_NS}}}f")
        formula.text = value.formula
    if value.value is None:
        return
    if value.kind == "string" and value.formula is None:
        cell.attrib["t"] = "inlineStr"
        inline = ElementTree.SubElement(cell, f"{{{_SHEET_NS}}}is")
        text = ElementTree.SubElement(inline, f"{{{_SHEET_NS}}}t")
        text.text = value.value
        return
    if value.kind == "string":
        cell.attrib["t"] = "str"
    elif value.kind == "boolean":
        cell.attrib["t"] = "b"
    elif value.kind == "error":
        cell.attrib["t"] = "e"
    cached = ElementTree.SubElement(cell, f"{{{_SHEET_NS}}}v")
    cached.text = value.value


def _merge_xlsx(base: bytes, current: bytes, proposal: bytes) -> AutoMergeResult | None:
    try:
        with zipfile.ZipFile(io.BytesIO(base)) as base_archive, zipfile.ZipFile(io.BytesIO(current)) as current_archive, zipfile.ZipFile(io.BytesIO(proposal)) as proposal_archive:
            for archive in (base_archive, current_archive, proposal_archive):
                validate_xlsx_archive_envelope(archive)
            base_parts = _xlsx_sheet_parts(base_archive)
            current_parts = _xlsx_sheet_parts(current_archive)
            proposal_parts = _xlsx_sheet_parts(proposal_archive)
            if [name for name, _ in base_parts] != [name for name, _ in current_parts] or [name for name, _ in base_parts] != [name for name, _ in proposal_parts]:
                return None
            base_shared = _shared_strings(base_archive)
            current_shared = _shared_strings(current_archive)
            proposal_shared = _shared_strings(proposal_archive)
            replacements: dict[str, bytes] = {}
            applied = 0
            conflicts = 0
            for (_, base_path), (_, current_path), (_, proposal_path) in zip(base_parts, current_parts, proposal_parts, strict=True):
                base_root = ElementTree.fromstring(base_archive.read(base_path))
                current_root = ElementTree.fromstring(current_archive.read(current_path))
                proposal_root = ElementTree.fromstring(proposal_archive.read(proposal_path))
                base_cells = _sheet_cells(base_root, base_shared)
                current_cells = _sheet_cells(current_root, current_shared)
                proposal_cells = _sheet_cells(proposal_root, proposal_shared)
                changed = False
                for address in sorted(set(base_cells) | set(proposal_cells)):
                    base_value = base_cells.get(address)
                    proposal_value = proposal_cells.get(address)
                    if _cell_equal(base_value, proposal_value):
                        continue
                    current_value = current_cells.get(address)
                    if not _cell_equal(current_value, base_value) and not _cell_equal(current_value, proposal_value):
                        conflicts += 1
                        continue
                    if not _cell_equal(current_value, proposal_value):
                        _set_cell_value(current_root, address, proposal_value)
                        applied += 1
                        changed = True
                if changed:
                    replacements[current_path] = ElementTree.tostring(current_root, encoding="utf-8", xml_declaration=True)

            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as merged_archive:
                for info in current_archive.infolist():
                    merged_archive.writestr(info, replacements.get(info.filename, current_archive.read(info.filename)))
    except (KeyError, OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
        return None
    return AutoMergeResult(output.getvalue(), "xlsx-cells", applied, conflicts)
