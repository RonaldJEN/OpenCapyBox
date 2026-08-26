import {
  CommandType,
  CellValueType,
  LogLevel,
  LocaleType,
  Univer,
  mergeLocales,
  type ICellData,
  type IWorkbookData,
} from '@univerjs/core';
import { FUniver } from '@univerjs/core/facade';
import { UniverSheetsCorePreset } from '@univerjs/preset-sheets-core';
import UniverPresetSheetsCoreZhCN from '@univerjs/preset-sheets-core/locales/zh-CN';
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';
import * as XLSX from 'xlsx';
import '@univerjs/preset-sheets-core/lib/index.css';

export interface SpreadsheetEditorHandle {
  exportFile: () => ArrayBuffer;
}

interface SpreadsheetEditorProps {
  source: ArrayBuffer;
  fileName: string;
  fileType: 'csv' | 'xls' | 'xlsx' | 'et';
  readOnly?: boolean;
  onMutation?: () => void;
  onError?: (message: string) => void;
}

interface CsvSourceFormat {
  bom: boolean;
  fieldSeparator: string;
  rowSeparator: string;
  trailingRowSeparator: boolean;
  separatorDirective: boolean;
}

interface SpreadsheetSourceMetadata {
  fileType: SpreadsheetEditorProps['fileType'];
  source: Uint8Array;
  csvFormat?: CsvSourceFormat;
}

type SpreadsheetSnapshotSheet = Partial<NonNullable<IWorkbookData['sheets'][string]>>;

const spreadsheetSourceMetadata = new WeakMap<XLSX.WorkBook, SpreadsheetSourceMetadata>();
const OOXML_MAIN_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main';
const OOXML_DOCUMENT_REL_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';

function firstCsvRecord(source: string): string {
  let quoted = false;
  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];
    if (character === '"') {
      if (quoted && source[index + 1] === '"') index += 1;
      else quoted = !quoted;
    } else if (!quoted && (character === '\r' || character === '\n')) {
      return source.slice(0, index);
    }
  }
  return source;
}

function detectCsvSeparator(source: string): string {
  const record = firstCsvRecord(source);
  const candidates = [',', ';', '\t', '|'];
  let quoted = false;
  const counts = new Map(candidates.map((candidate) => [candidate, 0]));
  for (let index = 0; index < record.length; index += 1) {
    const character = record[index];
    if (character === '"') {
      if (quoted && record[index + 1] === '"') index += 1;
      else quoted = !quoted;
    } else if (!quoted && counts.has(character)) {
      counts.set(character, (counts.get(character) || 0) + 1);
    }
  }
  return candidates.reduce((best, candidate) => (
    (counts.get(candidate) || 0) > (counts.get(best) || 0) ? candidate : best
  ), ',');
}

function inspectCsvSource(source: string, bom: boolean): CsvSourceFormat {
  const rowSeparator = source.match(/\r\n|\n|\r/)?.[0] || '\n';
  const firstLine = firstCsvRecord(source);
  const directiveMatch = /^sep=(.)$/i.exec(firstLine);
  return {
    bom,
    fieldSeparator: directiveMatch?.[1] || detectCsvSeparator(source),
    rowSeparator,
    trailingRowSeparator: /(?:\r\n|\n|\r)$/.test(source),
    separatorDirective: Boolean(directiveMatch),
  };
}

export function readSpreadsheetSource(
  source: ArrayBuffer,
  fileType: SpreadsheetEditorProps['fileType'],
): XLSX.WorkBook {
  if (fileType === 'csv') {
    const bytes = new Uint8Array(source);
    const bom = bytes.length >= 3 && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf;
    let csv: string;
    try {
      csv = new TextDecoder('utf-8', { fatal: true }).decode(source).replace(/^\uFEFF/, '');
    } catch {
      throw new Error('CSV 不是有效的 UTF-8 文本，已拒绝编辑以避免损坏原文件');
    }
    // CSV 没有类型元数据；按文本读取，避免 00123、日期样式文本等在无编辑时被自动改写。
    const workbook = XLSX.read(csv, { type: 'string', cellDates: false, raw: true });
    spreadsheetSourceMetadata.set(workbook, {
      fileType,
      source: bytes.slice(),
      csvFormat: inspectCsvSource(csv, bom),
    });
    return workbook;
  }
  const workbook = XLSX.read(new Uint8Array(source), {
    type: 'buffer',
    cellDates: false,
    cellFormula: true,
    cellStyles: true,
  });
  spreadsheetSourceMetadata.set(workbook, {
    fileType,
    source: new Uint8Array(source.slice(0)),
  });
  return workbook;
}

export function isSpreadsheetReadOnly(
  fileType: SpreadsheetEditorProps['fileType'],
  requestedReadOnly = false,
): boolean {
  return requestedReadOnly || fileType === 'xls' || fileType === 'et';
}

interface SpreadsheetAccessWorkbook {
  getWorkbookPermission: () => {
    setReadOnly: () => Promise<void>;
    setEditable: () => Promise<void>;
    setPermissionDialogVisible: (visible: boolean) => void;
  };
}

export async function applySpreadsheetAccessMode(
  workbook: SpreadsheetAccessWorkbook,
  readOnly: boolean,
): Promise<void> {
  const permission = workbook.getWorkbookPermission();
  permission.setPermissionDialogVisible(!readOnly);
  if (readOnly) await permission.setReadOnly();
  else await permission.setEditable();
}

export function getSpreadsheetSheetsUiConfig(readOnly: boolean) {
  return {
    // 只读 ET 中常见数字/日期文本；viewer 无需弹出“文本转数字”提醒。
    // Univer 0.25.1 的提醒还引用了语言包中不存在的 forceStringInfo 键。
    disableForceStringAlert: readOnly,
  };
}

function createSpreadsheetUniver(container: HTMLElement, readOnly: boolean) {
  const univer = new Univer({
    locale: LocaleType.ZH_CN,
    locales: {
      [LocaleType.ZH_CN]: mergeLocales(UniverPresetSheetsCoreZhCN),
    },
    logLevel: LogLevel.WARN,
  });
  const preset = UniverSheetsCorePreset({
    container,
    header: true,
    // 当前保存协议只允许单元格值和公式。隐藏格式/结构入口，避免产生无法保真写回的修改。
    toolbar: false,
    formulaBar: !readOnly,
    contextMenu: false,
    footer: {
      sheetBar: true,
      statisticBar: false,
      menus: false,
      zoomSlider: false,
      addSheetButtonConfig: { show: false },
    },
    sheets: getSpreadsheetSheetsUiConfig(readOnly),
  });
  preset.plugins.forEach((plugin) => {
    if (Array.isArray(plugin)) univer.registerPlugin(plugin[0], plugin[1]);
    else univer.registerPlugin(plugin);
  });
  return { univerAPI: FUniver.newAPI(univer) };
}

function toUniverCell(cell: XLSX.CellObject): ICellData {
  const formula = typeof cell.f === 'string' && cell.f.length > 0
    ? `=${cell.f.replace(/^=/, '')}`
    : undefined;
  const value = cell.v instanceof Date ? cell.v.toISOString() : cell.v;
  if (formula) {
    return {
      f: formula,
      ...(typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean'
        ? { v: value }
        : {}),
    };
  }
  if (typeof value === 'number') return { v: value, t: CellValueType.NUMBER };
  if (typeof value === 'boolean') return { v: value, t: CellValueType.BOOLEAN };
  if (value == null) return {};
  return { v: String(value), t: CellValueType.STRING };
}

export function sheetJsToUniverWorkbook(workbook: XLSX.WorkBook, fileName: string): IWorkbookData {
  const sheetOrder: string[] = [];
  const sheets: IWorkbookData['sheets'] = {};

  workbook.SheetNames.forEach((sheetName, sheetIndex) => {
    const sheetId = `sheet-${sheetIndex + 1}`;
    const worksheet = workbook.Sheets[sheetName] || {};
    const cellData: NonNullable<IWorkbookData['sheets'][string]['cellData']> = {};
    let maxRow = 0;
    let maxColumn = 0;

    Object.entries(worksheet).forEach(([address, rawCell]) => {
      if (address.startsWith('!') || !rawCell) return;
      const position = XLSX.utils.decode_cell(address);
      maxRow = Math.max(maxRow, position.r);
      maxColumn = Math.max(maxColumn, position.c);
      cellData[position.r] ||= {};
      cellData[position.r][position.c] = toUniverCell(rawCell as XLSX.CellObject);
    });

    const range = worksheet['!ref'] ? XLSX.utils.decode_range(worksheet['!ref']) : null;
    if (range) {
      maxRow = Math.max(maxRow, range.e.r);
      maxColumn = Math.max(maxColumn, range.e.c);
    }

    sheetOrder.push(sheetId);
    sheets[sheetId] = {
      id: sheetId,
      name: sheetName,
      rowCount: Math.max(100, maxRow + 1),
      columnCount: Math.max(26, maxColumn + 1),
      cellData,
    };
  });

  if (sheetOrder.length === 0) {
    sheetOrder.push('sheet-1');
    sheets['sheet-1'] = {
      id: 'sheet-1',
      name: 'Sheet1',
      rowCount: 100,
      columnCount: 26,
      cellData: {},
    };
  }

  return {
    id: `preview-${Date.now().toString(36)}`,
    name: fileName,
    appVersion: '0.25.1',
    locale: LocaleType.ZH_CN,
    styles: {},
    sheetOrder,
    sheets,
  };
}

function normalizeFormula(formula: unknown): string | undefined {
  return typeof formula === 'string' && formula.length > 0 ? formula.replace(/^=/, '') : undefined;
}

function cellContentMatches(cell: ICellData | null | undefined, existing: XLSX.CellObject | undefined): boolean {
  const nextFormula = normalizeFormula(cell?.f);
  const existingFormula = normalizeFormula(existing?.f);
  if (nextFormula || existingFormula) {
    // 公式未变时保留原始 OOXML 节点和缓存结果，避免把共享/数组公式重建为普通公式。
    return nextFormula === existingFormula;
  }
  const nextValue = cell?.v;
  const existingValue = existing?.v;
  if (nextValue == null && existingValue == null) return true;
  return Object.is(nextValue, existingValue);
}

function applySnapshotCellToSheetJs(
  target: XLSX.WorkSheet,
  address: string,
  cell: ICellData | null | undefined,
) {
  const existing = target[address] as XLSX.CellObject | undefined;
  if (cellContentMatches(cell, existing)) return;

  const next = existing ? { ...existing } : {} as XLSX.CellObject;
  ['v', 'w', 't', 'f', 'F', 'D', 'r', 'h'].forEach((key) => {
    delete (next as unknown as Record<string, unknown>)[key];
  });

  const formula = normalizeFormula(cell?.f);
  const value = cell?.v;
  if (formula) next.f = formula;
  if (value != null) {
    next.v = value;
    next.t = typeof value === 'number' ? 'n' : typeof value === 'boolean' ? 'b' : 's';
  }

  if (Object.keys(next).length === 0) delete target[address];
  else target[address] = next;
}

function expectedSheetSize(worksheet: XLSX.WorkSheet | undefined) {
  let maxRow = 0;
  let maxColumn = 0;
  Object.keys(worksheet || {}).forEach((address) => {
    if (address.startsWith('!')) return;
    const position = XLSX.utils.decode_cell(address);
    maxRow = Math.max(maxRow, position.r);
    maxColumn = Math.max(maxColumn, position.c);
  });
  if (worksheet?.['!ref']) {
    const range = XLSX.utils.decode_range(worksheet['!ref']);
    maxRow = Math.max(maxRow, range.e.r);
    maxColumn = Math.max(maxColumn, range.e.c);
  }
  return {
    rowCount: Math.max(100, maxRow + 1),
    columnCount: Math.max(26, maxColumn + 1),
  };
}

function validateCellData(cell: ICellData | null | undefined) {
  if (cell == null) return;
  if (cell.ref != null) {
    throw new Error('当前不支持保存数组公式，原公式组未被改写');
  }
  const unsupportedKeys = Object.keys(cell).filter((key) => !['v', 't', 'f', 'si', 'xf'].includes(key));
  if (unsupportedKeys.length > 0) {
    throw new Error('当前仅支持修改单元格值和公式，格式修改未保存');
  }
  if (cell.v != null && !['string', 'number', 'boolean'].includes(typeof cell.v)) {
    throw new Error('当前单元格值类型无法安全保存');
  }
  if (typeof cell.v === 'number' && !Number.isFinite(cell.v)) {
    throw new Error('当前单元格包含非有限数值，无法安全保存');
  }
  if (cell.f != null && typeof cell.f !== 'string') {
    throw new Error('当前公式类型无法安全保存');
  }
}

function validateSnapshotStructure(snapshot: IWorkbookData, original: XLSX.WorkBook): SpreadsheetSnapshotSheet[] {
  if (snapshot.sheetOrder.length !== original.SheetNames.length) {
    throw new Error('当前仅支持修改单元格值和公式，工作表结构修改未保存');
  }

  return snapshot.sheetOrder.map((sheetId, index) => {
    const sheet = snapshot.sheets[sheetId];
    const originalName = original.SheetNames[index];
    if (!sheet || sheetId !== `sheet-${index + 1}` || sheet.name !== originalName) {
      throw new Error('当前仅支持修改单元格值和公式，工作表名称或顺序修改未保存');
    }
    const expected = expectedSheetSize(original.Sheets[originalName]);
    if (sheet.rowCount !== expected.rowCount || sheet.columnCount !== expected.columnCount) {
      throw new Error('当前仅支持修改单元格值和公式，行列结构修改未保存');
    }
    if (sheet.mergeData && sheet.mergeData.length > 0) {
      throw new Error('当前仅支持修改单元格值和公式，合并单元格修改未保存');
    }
    if (Object.keys(sheet.rowData || {}).length > 0 || Object.keys(sheet.columnData || {}).length > 0) {
      throw new Error('当前仅支持修改单元格值和公式，行高或列宽修改未保存');
    }
    Object.values(sheet.cellData || {}).forEach((columns) => {
      Object.values(columns || {}).forEach((cell) => validateCellData(cell as ICellData | null));
    });
    return sheet;
  });
}

function updateSheetFromSnapshot(
  source: XLSX.WorkSheet | undefined,
  sheet: SpreadsheetSnapshotSheet,
): XLSX.WorkSheet {
  const target: XLSX.WorkSheet = source ? { ...source } : XLSX.utils.aoa_to_sheet([]);
  const nextAddresses = new Set<string>();
  let maxRow = -1;
  let maxColumn = -1;

  if (source?.['!ref']) {
    const range = XLSX.utils.decode_range(source['!ref']);
    maxRow = range.e.r;
    maxColumn = range.e.c;
  }
  Object.entries(sheet.cellData || {}).forEach(([rowKey, columns]) => {
    const row = Number(rowKey);
    Object.entries(columns || {}).forEach(([columnKey, cell]) => {
      const column = Number(columnKey);
      const address = XLSX.utils.encode_cell({ r: row, c: column });
      nextAddresses.add(address);
      maxRow = Math.max(maxRow, row);
      maxColumn = Math.max(maxColumn, column);
      applySnapshotCellToSheetJs(target, address, cell as ICellData | null);
    });
  });

  Object.keys(target).forEach((address) => {
    if (!address.startsWith('!') && !nextAddresses.has(address)) {
      applySnapshotCellToSheetJs(target, address, undefined);
    }
  });
  if (maxRow >= 0 && maxColumn >= 0) {
    target['!ref'] = XLSX.utils.encode_range({ s: { r: 0, c: 0 }, e: { r: maxRow, c: maxColumn } });
  }
  return target;
}

interface CfbEntry {
  content: Uint8Array;
}

interface CfbArchive {
  FullPaths: string[];
  FileIndex: CfbEntry[];
}

function parseXml(source: string, label: string): XMLDocument {
  const document = new DOMParser().parseFromString(source, 'application/xml');
  if (document.getElementsByTagName('parsererror').length > 0) {
    throw new Error(`${label} XML 已损坏，无法安全保存`);
  }
  return document;
}

function xmlElements(parent: Document | Element, localName: string): Element[] {
  return Array.from(parent.getElementsByTagNameNS('*', localName));
}

function getCfbEntry(archive: CfbArchive, path: string): CfbEntry {
  const entry = XLSX.CFB.find(archive, path) as CfbEntry | null;
  if (!entry?.content) throw new Error(`XLSX 缺少必要文件 ${path}`);
  return entry;
}

function getCfbXml(archive: CfbArchive, path: string): string {
  return new TextDecoder('utf-8', { fatal: true }).decode(getCfbEntry(archive, path).content);
}

function setCfbXml(archive: CfbArchive, path: string, document: XMLDocument) {
  const xml = new XMLSerializer().serializeToString(document);
  XLSX.CFB.utils.cfb_add(archive, path, new TextEncoder().encode(xml));
}

function resolvePartPath(basePart: string, target: string): string {
  if (/^[a-z][a-z\d+.-]*:/i.test(target)) {
    throw new Error('XLSX 工作表关系指向了不支持的外部资源');
  }
  const raw = target.startsWith('/')
    ? target
    : `${basePart.slice(0, basePart.lastIndexOf('/') + 1)}${target}`;
  const parts: string[] = [];
  raw.replace(/\\/g, '/').split('/').forEach((part) => {
    if (!part || part === '.') return;
    if (part === '..') parts.pop();
    else parts.push(part);
  });
  return `/${parts.join('/')}`;
}

function workbookSheetParts(
  archive: CfbArchive,
  original: XLSX.WorkBook,
): { workbookDocument: XMLDocument; relationshipsDocument: XMLDocument; sheetPaths: string[] } {
  const workbookPath = '/xl/workbook.xml';
  const workbookDocument = parseXml(getCfbXml(archive, workbookPath), 'workbook.xml');
  const relationshipsDocument = parseXml(
    getCfbXml(archive, '/xl/_rels/workbook.xml.rels'),
    'workbook.xml.rels',
  );
  const relationshipTargets = new Map(xmlElements(relationshipsDocument, 'Relationship').map((relationship) => [
    relationship.getAttribute('Id') || '',
    relationship.getAttribute('Target') || '',
  ]));
  const sheetElements = xmlElements(workbookDocument, 'sheet');
  if (sheetElements.length !== original.SheetNames.length) {
    throw new Error('XLSX 工作表清单与解析结果不一致，已拒绝保存');
  }
  const sheetPaths = sheetElements.map((sheet, index) => {
    if (sheet.getAttribute('name') !== original.SheetNames[index]) {
      throw new Error('XLSX 工作表名称与解析结果不一致，已拒绝保存');
    }
    const relationshipId = sheet.getAttributeNS(OOXML_DOCUMENT_REL_NS, 'id') || sheet.getAttribute('r:id');
    const target = relationshipId ? relationshipTargets.get(relationshipId) : undefined;
    if (!target) throw new Error(`XLSX 工作表 ${original.SheetNames[index]} 缺少文件关系`);
    return resolvePartPath(workbookPath, target);
  });
  return { workbookDocument, relationshipsDocument, sheetPaths };
}

function snapshotCells(sheet: SpreadsheetSnapshotSheet): Map<string, ICellData | null> {
  const result = new Map<string, ICellData | null>();
  Object.entries(sheet.cellData || {}).forEach(([rowKey, columns]) => {
    Object.entries(columns || {}).forEach(([columnKey, cell]) => {
      result.set(XLSX.utils.encode_cell({ r: Number(rowKey), c: Number(columnKey) }), cell as ICellData | null);
    });
  });
  return result;
}

function worksheetCellElement(document: XMLDocument, address: string): Element | undefined {
  return xmlElements(document, 'c').find((cell) => cell.getAttribute('r') === address);
}

function getOrCreateWorksheetCell(document: XMLDocument, address: string): Element {
  const existing = worksheetCellElement(document, address);
  if (existing) return existing;
  const sheetData = xmlElements(document, 'sheetData')[0];
  if (!sheetData) throw new Error('XLSX 工作表缺少 sheetData，无法安全保存');
  const position = XLSX.utils.decode_cell(address);
  const rowNumber = position.r + 1;
  let row = xmlElements(sheetData, 'row').find((candidate) => Number(candidate.getAttribute('r')) === rowNumber);
  if (!row) {
    row = document.createElementNS(OOXML_MAIN_NS, 'row');
    row.setAttribute('r', String(rowNumber));
    const nextRow = xmlElements(sheetData, 'row').find((candidate) => Number(candidate.getAttribute('r')) > rowNumber);
    sheetData.insertBefore(row, nextRow || null);
  }
  const cell = document.createElementNS(OOXML_MAIN_NS, 'c');
  cell.setAttribute('r', address);
  const nextCell = xmlElements(row, 'c').find((candidate) => {
    const candidateAddress = candidate.getAttribute('r');
    return candidateAddress ? XLSX.utils.decode_cell(candidateAddress).c > position.c : false;
  });
  row.removeAttribute('spans');
  row.insertBefore(cell, nextCell || null);
  return cell;
}

function removeOoxmlCellContent(cell: Element) {
  Array.from(cell.children).forEach((child) => {
    if (['f', 'v', 'is'].includes(child.localName)) child.remove();
  });
  cell.removeAttribute('t');
}

function ooxmlTextElement(document: XMLDocument, localName: string, value: string): Element {
  const element = document.createElementNS(OOXML_MAIN_NS, localName);
  element.textContent = value;
  return element;
}

function setOoxmlCellContent(document: XMLDocument, address: string, cellData: ICellData | null | undefined) {
  const formula = normalizeFormula(cellData?.f);
  const value = cellData?.v;
  const existing = worksheetCellElement(document, address);
  if (!existing && !formula && value == null) return;
  const cell = existing || getOrCreateWorksheetCell(document, address);
  removeOoxmlCellContent(cell);

  if (!formula && value == null) {
    const onlyAddressAttribute = Array.from(cell.attributes).every((attribute) => attribute.name === 'r');
    if (onlyAddressAttribute && cell.children.length === 0) cell.remove();
    return;
  }

  const content: Element[] = [];
  if (formula) content.push(ooxmlTextElement(document, 'f', formula));
  if (value != null) {
    if (!formula && typeof value === 'string') {
      cell.setAttribute('t', 'inlineStr');
      const inlineString = document.createElementNS(OOXML_MAIN_NS, 'is');
      const text = ooxmlTextElement(document, 't', value);
      if (/^\s|\s$/.test(value)) {
        text.setAttributeNS('http://www.w3.org/XML/1998/namespace', 'xml:space', 'preserve');
      }
      inlineString.appendChild(text);
      content.push(inlineString);
    } else {
      if (typeof value === 'boolean') cell.setAttribute('t', 'b');
      else if (typeof value === 'string') cell.setAttribute('t', 'str');
      content.push(ooxmlTextElement(document, 'v', typeof value === 'boolean' ? (value ? '1' : '0') : String(value)));
    }
  }
  const firstRemainingChild = cell.firstElementChild;
  content.forEach((element) => cell.insertBefore(element, firstRemainingChild));
}

function extendWorksheetDimension(document: XMLDocument, sheet: SpreadsheetSnapshotSheet) {
  let startRow = Number.POSITIVE_INFINITY;
  let startColumn = Number.POSITIVE_INFINITY;
  let endRow = -1;
  let endColumn = -1;
  const dimension = xmlElements(document, 'dimension')[0];
  const currentRef = dimension?.getAttribute('ref');
  if (currentRef) {
    try {
      const range = XLSX.utils.decode_range(currentRef);
      startRow = range.s.r;
      startColumn = range.s.c;
      endRow = range.e.r;
      endColumn = range.e.c;
    } catch {
      throw new Error('XLSX 工作表 dimension 已损坏，无法安全保存');
    }
  }
  snapshotCells(sheet).forEach((cell, address) => {
    if (!normalizeFormula(cell?.f) && cell?.v == null) return;
    const position = XLSX.utils.decode_cell(address);
    startRow = Math.min(startRow, position.r);
    startColumn = Math.min(startColumn, position.c);
    endRow = Math.max(endRow, position.r);
    endColumn = Math.max(endColumn, position.c);
  });
  if (endRow < 0 || endColumn < 0) return;
  const nextDimension = dimension || document.createElementNS(OOXML_MAIN_NS, 'dimension');
  nextDimension.setAttribute('ref', XLSX.utils.encode_range({
    s: { r: startRow, c: startColumn },
    e: { r: endRow, c: endColumn },
  }));
  if (!dimension) {
    const sheetData = xmlElements(document, 'sheetData')[0];
    document.documentElement.insertBefore(nextDimension, sheetData || document.documentElement.firstChild);
  }
}

function patchWorksheetXml(
  source: string,
  originalSheet: XLSX.WorkSheet,
  snapshotSheet: SpreadsheetSnapshotSheet,
): { document: XMLDocument; changed: boolean } {
  const document = parseXml(source, 'worksheet');
  const cells = snapshotCells(snapshotSheet);
  const addresses = new Set([
    ...Object.keys(originalSheet).filter((address) => !address.startsWith('!')),
    ...cells.keys(),
  ]);
  let changed = false;
  addresses.forEach((address) => {
    const cell = cells.get(address);
    const originalCell = originalSheet[address] as XLSX.CellObject | undefined;
    if (cellContentMatches(cell, originalCell)) return;
    const cellElement = worksheetCellElement(document, address);
    const formulaElement = cellElement ? xmlElements(cellElement, 'f')[0] : undefined;
    const formulaType = formulaElement?.getAttribute('t');
    const dynamicArrayFormula = (originalCell as (XLSX.CellObject & { D?: boolean }) | undefined)?.D;
    if (originalCell?.F || dynamicArrayFormula || ['shared', 'array', 'dataTable'].includes(formulaType || '')) {
      throw new Error(`单元格 ${address} 属于共享或数组公式，已拒绝可能破坏公式组的修改`);
    }
    setOoxmlCellContent(document, address, cell);
    changed = true;
  });
  if (changed) extendWorksheetDimension(document, snapshotSheet);
  return { document, changed };
}

function requestFullCalculation(workbookDocument: XMLDocument) {
  let calculation = xmlElements(workbookDocument, 'calcPr')[0];
  if (!calculation) {
    calculation = workbookDocument.createElementNS(OOXML_MAIN_NS, 'calcPr');
    workbookDocument.documentElement.appendChild(calculation);
  }
  calculation.setAttribute('calcMode', 'auto');
  calculation.setAttribute('fullCalcOnLoad', '1');
  calculation.setAttribute('forceFullCalc', '1');
}

function removeCalculationChain(
  archive: CfbArchive,
  relationshipsDocument: XMLDocument,
) {
  const relationshipsPath = '/xl/_rels/workbook.xml.rels';
  const calculationChainPaths = new Set(['/xl/calcChain.xml']);
  let relationshipsChanged = false;
  xmlElements(relationshipsDocument, 'Relationship').forEach((relationship) => {
    const type = relationship.getAttribute('Type') || '';
    if (!type.endsWith('/calcChain')) return;
    const target = relationship.getAttribute('Target');
    if (target) calculationChainPaths.add(resolvePartPath('/xl/workbook.xml', target));
    relationship.remove();
    relationshipsChanged = true;
  });
  calculationChainPaths.forEach((path) => {
    if (XLSX.CFB.find(archive, path)) XLSX.CFB.utils.cfb_del(archive, path);
  });

  const contentTypesPath = '/[Content_Types].xml';
  const contentTypesDocument = parseXml(getCfbXml(archive, contentTypesPath), '[Content_Types].xml');
  let contentTypesChanged = false;
  xmlElements(contentTypesDocument, 'Override').forEach((override) => {
    if (!calculationChainPaths.has(override.getAttribute('PartName') || '')) return;
    override.remove();
    contentTypesChanged = true;
  });
  if (relationshipsChanged) setCfbXml(archive, relationshipsPath, relationshipsDocument);
  if (contentTypesChanged) setCfbXml(archive, contentTypesPath, contentTypesDocument);
}

function toArrayBuffer(output: ArrayBuffer | Uint8Array): ArrayBuffer {
  if (output instanceof ArrayBuffer) return output;
  const copy = new Uint8Array(output.byteLength);
  copy.set(output);
  return copy.buffer;
}

function patchXlsxSource(
  snapshotSheets: SpreadsheetSnapshotSheet[],
  original: XLSX.WorkBook,
  source: Uint8Array,
): ArrayBuffer {
  let archive: CfbArchive;
  try {
    archive = XLSX.CFB.read(source, { type: 'buffer' }) as CfbArchive;
  } catch {
    throw new Error('XLSX 原文件包已损坏，无法安全保存');
  }
  const { workbookDocument, relationshipsDocument, sheetPaths } = workbookSheetParts(archive, original);
  let changed = false;
  sheetPaths.forEach((path, index) => {
    const result = patchWorksheetXml(
      getCfbXml(archive, path),
      original.Sheets[original.SheetNames[index]] || {},
      snapshotSheets[index],
    );
    if (result.changed) {
      setCfbXml(archive, path, result.document);
      changed = true;
    }
  });
  if (changed) {
    removeCalculationChain(archive, relationshipsDocument);
    requestFullCalculation(workbookDocument);
    setCfbXml(archive, '/xl/workbook.xml', workbookDocument);
  }
  try {
    return toArrayBuffer(XLSX.CFB.write(archive, {
      type: 'array',
      fileType: 'zip',
      compression: true,
    }) as ArrayBuffer | Uint8Array);
  } catch {
    throw new Error('XLSX 重新封装失败，原文件未被覆盖');
  }
}

function encodeCsv(source: string, format: CsvSourceFormat): ArrayBuffer {
  let csv = source;
  if (format.separatorDirective) csv = `sep=${format.fieldSeparator}${format.rowSeparator}${csv}`;
  if (format.trailingRowSeparator && !csv.endsWith(format.rowSeparator)) csv += format.rowSeparator;
  if (format.bom) csv = `\uFEFF${csv}`;
  return new TextEncoder().encode(csv).buffer;
}

export function snapshotToArrayBuffer(
  snapshot: IWorkbookData,
  original: XLSX.WorkBook,
  fileType: SpreadsheetEditorProps['fileType'],
): ArrayBuffer {
  if (fileType === 'et') throw new Error('ET 文件仅支持只读预览');
  if (fileType === 'xls') throw new Error('XLS 文件仅支持只读预览，请转换为 XLSX 后编辑');
  const sheets = validateSnapshotStructure(snapshot, original);
  const sourceMetadata = spreadsheetSourceMetadata.get(original);
  if (fileType === 'csv') {
    const worksheet = updateSheetFromSnapshot(original.Sheets[original.SheetNames[0]], sheets[0]);
    const format = sourceMetadata?.csvFormat || {
      bom: false,
      fieldSeparator: ',',
      rowSeparator: '\n',
      trailingRowSeparator: false,
      separatorDirective: false,
    };
    const csv = XLSX.utils.sheet_to_csv(worksheet, {
      FS: format.fieldSeparator,
      RS: format.rowSeparator,
    });
    return encodeCsv(csv, format);
  }
  if (!sourceMetadata || sourceMetadata.fileType !== 'xlsx') {
    throw new Error('缺少 XLSX 原始内容，无法执行保真保存');
  }
  return patchXlsxSource(sheets, original, sourceMetadata.source);
}

export const SpreadsheetEditor = forwardRef<SpreadsheetEditorHandle, SpreadsheetEditorProps>(
  function SpreadsheetEditor({ source, fileName, fileType, readOnly = false, onMutation, onError }, ref) {
    const containerRef = useRef<HTMLDivElement>(null);
    const snapshotRef = useRef<(() => IWorkbookData) | null>(null);
    const originalWorkbookRef = useRef<XLSX.WorkBook | null>(null);
    const [accessReady, setAccessReady] = useState(false);
    const effectiveReadOnly = isSpreadsheetReadOnly(fileType, readOnly);
    const onMutationRef = useRef(onMutation);
    const onErrorRef = useRef(onError);
    onMutationRef.current = onMutation;
    onErrorRef.current = onError;

    useImperativeHandle(ref, () => ({
      exportFile: () => {
        const snapshot = snapshotRef.current?.();
        const original = originalWorkbookRef.current;
        if (!snapshot || !original) throw new Error('电子表格编辑器尚未准备好');
        return snapshotToArrayBuffer(snapshot, original, fileType);
      },
    }), [fileType]);

    useEffect(() => {
      if (!containerRef.current) return undefined;
      const container = containerRef.current;
      let disposeEditor: (() => void) | null = null;
      let disposed = false;
      setAccessReady(false);

      // Univer 自己维护一个 React root。推迟到外层 React 提交完成后初始化，
      // 避免 StrictMode 的探测性挂载同步销毁其尚在 render 的内部 root。
      const initializeTimer = window.setTimeout(() => {
        void (async () => {
          try {
            const workbook = readSpreadsheetSource(source, fileType);
            originalWorkbookRef.current = workbook;
            const data = sheetJsToUniverWorkbook(workbook, fileName);
            const { univerAPI } = createSpreadsheetUniver(container, effectiveReadOnly);
            const activeWorkbook = univerAPI.createWorkbook(data);
            let commandSubscription: { dispose: () => void } | null = null;
            let editorDisposed = false;
            disposeEditor = () => {
              if (editorDisposed) return;
              editorDisposed = true;
              snapshotRef.current = null;
              originalWorkbookRef.current = null;
              commandSubscription?.dispose();
              univerAPI.dispose();
            };

            await applySpreadsheetAccessMode(activeWorkbook, effectiveReadOnly);
            if (disposed) {
              disposeEditor();
              return;
            }

            snapshotRef.current = () => activeWorkbook.getSnapshot();
            commandSubscription = activeWorkbook.onCommandExecuted((command, options) => {
              if (!effectiveReadOnly && command.type === CommandType.MUTATION && !options?.fromChangeset) {
                onMutationRef.current?.();
              }
            });
            setAccessReady(true);
          } catch (error) {
            console.error('Failed to initialize spreadsheet editor:', error);
            if (!disposed) {
              setAccessReady(true);
              onErrorRef.current?.(error instanceof Error ? error.message : '无法打开电子表格编辑器');
            }
          }
        })();
      }, 0);

      return () => {
        disposed = true;
        window.clearTimeout(initializeTimer);
        const dispose = disposeEditor;
        if (dispose) {
          window.setTimeout(() => dispose(), 0);
        } else {
          snapshotRef.current = null;
          originalWorkbookRef.current = null;
        }
      };
    }, [effectiveReadOnly, fileName, fileType, source]);

    return (
      <div className="relative h-full min-h-[460px] w-full overflow-hidden bg-white" data-testid="spreadsheet-editor" aria-busy={!accessReady}>
        <div ref={containerRef} className="h-full w-full" />
        {!accessReady && (
          <div className="absolute inset-0 z-20 flex items-center justify-center bg-white text-sm text-claude-muted" data-testid="spreadsheet-access-loading">
            {effectiveReadOnly ? '正在设置只读视图…' : '正在打开电子表格…'}
          </div>
        )}
        {accessReady && fileType === 'xls' && (
          <div className="absolute right-3 top-3 z-20 rounded-md bg-amber-50 px-2 py-1 text-xs text-amber-800 shadow-sm" role="status">
            XLS 为只读预览；请转换为 XLSX 后编辑
          </div>
        )}
      </div>
    );
  },
);
