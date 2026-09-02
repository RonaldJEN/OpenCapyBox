"""Shared bounded validation for editable CSV and XLSX payloads."""

from __future__ import annotations

import io
import posixpath
import zipfile
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree


MAX_XLSX_ENTRY_COUNT = 10_000
MAX_XLSX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_XLSX_REQUIRED_XML_BYTES = 5 * 1024 * 1024
XLSX_REQUIRED_XML_PARTS = (
    "[Content_Types].xml",
    "_rels/.rels",
    "xl/workbook.xml",
    "xl/_rels/workbook.xml.rels",
)
XLSX_WORKBOOK_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
)
XLSX_WORKSHEET_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
)


def validate_csv_edit_payload(content: bytes) -> None:
    """Require an editable CSV to be UTF-8 text without NUL bytes."""

    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV 不是有效的 UTF-8 文本") from exc
    if "\x00" in decoded:
        raise ValueError("CSV 文本包含 NUL 字符")


def validate_xlsx_archive_envelope(archive: zipfile.ZipFile) -> set[str]:
    """Reject unsafe or unbounded ZIP envelopes before OOXML parsing."""

    entries = archive.infolist()
    if not entries or len(entries) > MAX_XLSX_ENTRY_COUNT:
        raise ValueError("XLSX ZIP 条目数量无效")

    names = [entry.filename.replace("\\", "/") for entry in entries]
    if len(names) != len(set(names)):
        raise ValueError("XLSX 包含重复 ZIP 条目")
    for name in names:
        path = name[:-1] if name.endswith("/") else name
        if (
            not path
            or name.startswith("/")
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise ValueError("XLSX ZIP 路径无效")
    if any(entry.flag_bits & 0x1 for entry in entries):
        raise ValueError("加密 XLSX 不支持在线编辑")
    if sum(entry.file_size for entry in entries) > MAX_XLSX_UNCOMPRESSED_BYTES:
        raise ValueError("XLSX 解压后内容过大")

    name_set = set(names)
    if not set(XLSX_REQUIRED_XML_PARTS).issubset(name_set):
        raise ValueError("XLSX 缺少必要的 OOXML 结构")
    if not any(
        name.startswith("xl/worksheets/") and name.endswith(".xml")
        for name in names
    ):
        raise ValueError("XLSX 缺少工作表")
    for part in XLSX_REQUIRED_XML_PARTS:
        if archive.getinfo(part).file_size > MAX_XLSX_REQUIRED_XML_BYTES:
            raise ValueError(f"XLSX 必要结构过大: {part}")
    return name_set


def validate_xlsx_edit_payload(content: bytes) -> None:
    """Validate the complete bounded OOXML graph before a durable edit."""

    try:
        with zipfile.ZipFile(io.BytesIO(content), "r") as archive:
            name_set = validate_xlsx_archive_envelope(archive)
            parsed_parts = {
                part: ElementTree.fromstring(archive.read(part))
                for part in XLSX_REQUIRED_XML_PARTS
            }

            def local_name(tag: str) -> str:
                return tag.rsplit("}", 1)[-1]

            def resolve_relationship_target(source_part: str, target: str) -> str:
                parsed_target = urlsplit(target)
                if (
                    parsed_target.scheme
                    or parsed_target.netloc
                    or parsed_target.query
                    or parsed_target.fragment
                ):
                    raise ValueError("XLSX 关系目标无效")
                target_path = unquote(parsed_target.path).replace("\\", "/")
                if target_path.startswith("/"):
                    resolved = posixpath.normpath(target_path.lstrip("/"))
                else:
                    resolved = posixpath.normpath(
                        posixpath.join(posixpath.dirname(source_part), target_path)
                    )
                if not resolved or resolved == ".." or resolved.startswith("../"):
                    raise ValueError("XLSX 关系目标越界")
                return resolved

            content_types = parsed_parts["[Content_Types].xml"]
            root_relationships = parsed_parts["_rels/.rels"]
            workbook = parsed_parts["xl/workbook.xml"]
            workbook_relationships = parsed_parts["xl/_rels/workbook.xml.rels"]
            if local_name(content_types.tag) != "Types":
                raise ValueError("XLSX Content Types 结构无效")
            if local_name(root_relationships.tag) != "Relationships":
                raise ValueError("XLSX 根关系结构无效")
            if local_name(workbook.tag) != "workbook":
                raise ValueError("XLSX workbook 结构无效")
            if local_name(workbook_relationships.tag) != "Relationships":
                raise ValueError("XLSX workbook 关系结构无效")

            overrides = {
                element.attrib.get("PartName", "").lstrip("/"): element.attrib.get(
                    "ContentType", ""
                )
                for element in content_types
                if local_name(element.tag) == "Override"
            }
            if overrides.get("xl/workbook.xml") != XLSX_WORKBOOK_CONTENT_TYPE:
                raise ValueError("XLSX workbook Content Type 无效")

            office_document_targets = []
            for relationship in root_relationships:
                if local_name(relationship.tag) != "Relationship":
                    continue
                relationship_type = relationship.attrib.get("Type", "")
                if not relationship_type.endswith("/officeDocument"):
                    continue
                if relationship.attrib.get("TargetMode", "Internal") != "Internal":
                    raise ValueError("XLSX workbook 不能使用外部关系")
                office_document_targets.append(
                    resolve_relationship_target(
                        "", relationship.attrib.get("Target", "")
                    )
                )
            if office_document_targets != ["xl/workbook.xml"]:
                raise ValueError("XLSX 根关系未唯一指向 workbook")

            relationship_by_id: dict[str, ElementTree.Element] = {}
            for relationship in workbook_relationships:
                if local_name(relationship.tag) != "Relationship":
                    continue
                relationship_id = relationship.attrib.get("Id", "")
                if not relationship_id or relationship_id in relationship_by_id:
                    raise ValueError("XLSX workbook 关系 ID 无效")
                relationship_by_id[relationship_id] = relationship

            sheets = next(
                (
                    element
                    for element in workbook
                    if local_name(element.tag) == "sheets"
                ),
                None,
            )
            sheet_entries = (
                []
                if sheets is None
                else [
                    element
                    for element in sheets
                    if local_name(element.tag) == "sheet"
                ]
            )
            if not sheet_entries:
                raise ValueError("XLSX workbook 没有工作表")

            sheet_ids: set[str] = set()
            relationship_ids: set[str] = set()
            worksheet_targets: set[str] = set()
            for sheet in sheet_entries:
                sheet_name = sheet.attrib.get("name", "").strip()
                sheet_id = sheet.attrib.get("sheetId", "")
                relationship_id = next(
                    (
                        value
                        for attribute, value in sheet.attrib.items()
                        if local_name(attribute) == "id"
                    ),
                    "",
                )
                if not sheet_name or not sheet_id.isdigit() or int(sheet_id) < 1:
                    raise ValueError("XLSX 工作表声明无效")
                if sheet_id in sheet_ids or relationship_id in relationship_ids:
                    raise ValueError("XLSX 工作表声明重复")
                sheet_ids.add(sheet_id)
                relationship_ids.add(relationship_id)

                relationship = relationship_by_id.get(relationship_id)
                if relationship is None:
                    raise ValueError("XLSX 工作表关系缺失")
                if not relationship.attrib.get("Type", "").endswith("/worksheet"):
                    raise ValueError("XLSX 工作表关系类型无效")
                if relationship.attrib.get("TargetMode", "Internal") != "Internal":
                    raise ValueError("XLSX 工作表不能使用外部关系")
                worksheet_target = resolve_relationship_target(
                    "xl/workbook.xml", relationship.attrib.get("Target", "")
                )
                if worksheet_target in worksheet_targets or worksheet_target not in name_set:
                    raise ValueError("XLSX 工作表目标无效")
                if overrides.get(worksheet_target) != XLSX_WORKSHEET_CONTENT_TYPE:
                    raise ValueError("XLSX 工作表 Content Type 无效")
                worksheet_info = archive.getinfo(worksheet_target)
                if worksheet_info.file_size > MAX_XLSX_REQUIRED_XML_BYTES:
                    raise ValueError(f"XLSX 工作表结构过大: {worksheet_target}")
                worksheet = ElementTree.fromstring(archive.read(worksheet_info))
                if local_name(worksheet.tag) != "worksheet":
                    raise ValueError("XLSX 工作表 XML 结构无效")
                worksheet_targets.add(worksheet_target)

            corrupt_entry = archive.testzip()
            if corrupt_entry is not None:
                raise ValueError(f"XLSX ZIP 条目损坏: {corrupt_entry}")
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        KeyError,
        RuntimeError,
        NotImplementedError,
        ElementTree.ParseError,
    ) as exc:
        raise ValueError("XLSX 文件结构无效") from exc


__all__ = [
    "validate_csv_edit_payload",
    "validate_xlsx_archive_envelope",
    "validate_xlsx_edit_payload",
]
