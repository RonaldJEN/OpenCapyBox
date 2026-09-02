import { CellValueType, CommandType } from '@univerjs/core';
import { SetRangeValuesMutation } from '@univerjs/sheets';
import { describe, expect, it, vi } from 'vitest';
import * as XLSX from 'xlsx';
import {
  applySpreadsheetAccessMode,
  createSpreadsheetContentTracker,
  getSpreadsheetSheetsUiConfig,
  isSpreadsheetReadOnly,
  readSpreadsheetSource,
  sheetJsToUniverWorkbook,
  shouldMarkSpreadsheetDirty,
  snapshotToArrayBuffer,
} from '../../../components/file-preview/SpreadsheetEditor';

function workbookWithTwoSheets(): XLSX.WorkBook {
  const workbook = XLSX.utils.book_new();
  const summary = XLSX.utils.aoa_to_sheet([
    ['项目', '数值', '未编辑公式', '待清空'],
    ['收入', 12, 24, '删除我'],
  ]);
  summary.B2.z = '#,##0.00';
  summary.B2.l = { Target: 'https://example.com/report', Tooltip: '查看报告' };
  summary.B2.c = [{ a: '测试员', t: '保留这条批注' }];
  summary.B3 = { t: 'n', f: 'SUM(B2:B2)', v: 12 };
  summary.C2 = { t: 'n', f: 'B2*2', v: 24, z: '0.00' };
  summary['!ref'] = 'A1:D3';
  XLSX.utils.book_append_sheet(workbook, summary, '汇总');
  XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet([['ID'], [1]]), '明细');
  return workbook;
}

function serializeWorkbook(bookType: 'xls' | 'xlsx'): ArrayBuffer {
  return XLSX.write(workbookWithTwoSheets(), {
    type: 'array',
    bookType,
    cellStyles: true,
  }) as ArrayBuffer;
}

function zipEntryWithSuffix(source: ArrayBuffer, suffix: string): Uint8Array {
  const archive = XLSX.CFB.read(new Uint8Array(source), { type: 'buffer' }) as { FullPaths: string[] };
  const path = archive.FullPaths.find((candidate) => candidate.replace(/\\/g, '/').endsWith(suffix));
  if (!path) throw new Error(`missing zip entry: ${suffix}`);
  const entry = XLSX.CFB.find(archive, path) as { content?: Uint8Array } | null;
  if (!entry?.content) throw new Error(`empty zip entry: ${suffix}`);
  return new Uint8Array(entry.content);
}

function writeZipArchive(archive: unknown): ArrayBuffer {
  const output = XLSX.CFB.write(archive, {
    type: 'array',
    fileType: 'zip',
    compression: true,
  }) as Uint8Array;
  const copy = new Uint8Array(output.byteLength);
  copy.set(output);
  return copy.buffer;
}

function replaceZipXml(source: ArrayBuffer, suffix: string, replace: (xml: string) => string): ArrayBuffer {
  const archive = XLSX.CFB.read(new Uint8Array(source), { type: 'buffer' }) as { FullPaths: string[] };
  const path = archive.FullPaths.find((candidate) => candidate.replace(/\\/g, '/').endsWith(suffix));
  if (!path) throw new Error(`missing zip entry: ${suffix}`);
  const xml = new TextDecoder().decode(zipEntryWithSuffix(source, suffix));
  XLSX.CFB.utils.cfb_add(archive, path, new TextEncoder().encode(replace(xml)));
  return writeZipArchive(archive);
}

function addCalculationChain(source: ArrayBuffer): ArrayBuffer {
  const archive = XLSX.CFB.read(new Uint8Array(source), { type: 'buffer' }) as { FullPaths: string[] };
  const relationshipsPath = archive.FullPaths.find((path) => path.endsWith('xl/_rels/workbook.xml.rels'))!;
  const contentTypesPath = archive.FullPaths.find((path) => path.endsWith('[Content_Types].xml'))!;
  const relationships = new TextDecoder().decode(
    (XLSX.CFB.find(archive, relationshipsPath) as { content: Uint8Array }).content,
  ).replace(
    '</Relationships>',
    '<Relationship Id="rId99" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/calcChain" Target="calcChain.xml"/></Relationships>',
  );
  const contentTypes = new TextDecoder().decode(
    (XLSX.CFB.find(archive, contentTypesPath) as { content: Uint8Array }).content,
  ).replace(
    '</Types>',
    '<Override PartName="/xl/calcChain.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.calcChain+xml"/></Types>',
  );
  XLSX.CFB.utils.cfb_add(archive, relationshipsPath, new TextEncoder().encode(relationships));
  XLSX.CFB.utils.cfb_add(archive, contentTypesPath, new TextEncoder().encode(contentTypes));
  XLSX.CFB.utils.cfb_add(
    archive,
    '/xl/calcChain.xml',
    new TextEncoder().encode(
      '<?xml version="1.0"?><calcChain xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><c r="C2" i="1"/></calcChain>',
    ),
  );
  return writeZipArchive(archive);
}

describe('SpreadsheetEditor 文件格式桥接', () => {
  it('内容 tracker 忽略初始化重复灌入和公式缓存重算，只报告真实值/公式变化', () => {
    const snapshot = sheetJsToUniverWorkbook(workbookWithTwoSheets(), '模型.xlsx');
    const tracker = createSpreadsheetContentTracker(snapshot);

    expect(tracker.apply({ subUnitId: 'sheet-1', cellValue: { 1: { 1: { v: 12 } } } })).toBe(false);
    expect(tracker.apply({ subUnitId: 'sheet-1', cellValue: { 1: { 2: { v: 25 } } } })).toBe(false);
    expect(tracker.apply({ subUnitId: 'sheet-1', cellValue: { 1: { 1: { v: 13 } } } })).toBe(true);
    expect(tracker.apply({ subUnitId: 'sheet-1', cellValue: { 1: { 2: { f: 'B2*3', v: 39 } } } })).toBe(true);
  });

  it('远端 changeset 只更新 tracker 基线，不触发工作区自动写回', () => {
    const snapshot = sheetJsToUniverWorkbook(workbookWithTwoSheets(), '模型.xlsx');
    const tracker = createSpreadsheetContentTracker(snapshot);
    const command = (value: number) => ({
      id: SetRangeValuesMutation.id,
      type: CommandType.MUTATION,
      params: {
        unitId: snapshot.id,
        subUnitId: 'sheet-1',
        cellValue: { 1: { 1: { v: value } } },
      },
    });

    expect(shouldMarkSpreadsheetDirty(command(13), { fromChangeset: true }, tracker, snapshot.id, false)).toBe(false);
    expect(shouldMarkSpreadsheetDirty(command(13), undefined, tracker, snapshot.id, false)).toBe(false);
    expect(shouldMarkSpreadsheetDirty(command(14), undefined, tracker, snapshot.id, false)).toBe(true);
    expect(shouldMarkSpreadsheetDirty(command(15), undefined, tracker, 'other-workbook', false)).toBe(false);
  });

  it('viewer 关闭数字文本提醒，避免 ET 点击单元格时泄露缺失语言键', () => {
    expect(getSpreadsheetSheetsUiConfig(true)).toEqual({ disableForceStringAlert: true });
    expect(getSpreadsheetSheetsUiConfig(false)).toEqual({ disableForceStringAlert: false });
  });

  it('只读模式等待 viewer 权限完成，不再使用仅拒绝提交的旧开关', async () => {
    const setReadOnly = vi.fn().mockResolvedValue(undefined);
    const setEditable = vi.fn().mockResolvedValue(undefined);
    const setPermissionDialogVisible = vi.fn();
    const workbook = {
      getWorkbookPermission: () => ({ setReadOnly, setEditable, setPermissionDialogVisible }),
    };

    await applySpreadsheetAccessMode(workbook, true);

    expect(setReadOnly).toHaveBeenCalledOnce();
    expect(setEditable).not.toHaveBeenCalled();
    expect(setPermissionDialogVisible).toHaveBeenCalledWith(false);
  });

  it('按 UTF-8 文本读取无 BOM 的中文 CSV', () => {
    const source = new TextEncoder().encode('名称,数值\n甲,1\n').buffer;
    const workbook = readSpreadsheetSource(source, 'csv');

    expect(workbook.Sheets.Sheet1.A1.v).toBe('名称');
    expect(workbook.Sheets.Sheet1.A2.v).toBe('甲');
  });

  it('CSV 无编辑字段保持文本语义，不把前导零、日期和小数格式自动转型', () => {
    const source = new TextEncoder().encode('编号,日期,金额\n00123,1/2/2024,1.00\n').buffer;
    const workbook = readSpreadsheetSource(source, 'csv');
    const row = workbook.Sheets.Sheet1;

    expect(row.A2).toMatchObject({ t: 's', v: '00123' });
    expect(row.B2).toMatchObject({ t: 's', v: '1/2/2024' });
    expect(row.C2).toMatchObject({ t: 's', v: '1.00' });
  });

  it('拒绝把非法 UTF-8 CSV 静默替换后覆盖原文件', () => {
    const source = new Uint8Array([0xc3, 0x28]).buffer;

    expect(() => readSpreadsheetSource(source, 'csv')).toThrow('CSV 不是有效的 UTF-8 文本');
  });

  it('XLS 和 ET 强制只读，二进制工作簿不会经 SheetJS 重写', () => {
    const source = serializeWorkbook('xls');
    const workbook = readSpreadsheetSource(source, 'xls');
    const snapshot = sheetJsToUniverWorkbook(workbook, '旧模型.xls');

    expect(isSpreadsheetReadOnly('xls')).toBe(true);
    expect(isSpreadsheetReadOnly('et')).toBe(true);
    expect(isSpreadsheetReadOnly('xlsx')).toBe(false);
    expect(() => snapshotToArrayBuffer(snapshot, workbook, 'xls')).toThrow(
      'XLS 文件仅支持只读预览，请转换为 XLSX 后编辑',
    );
  });

  it('读取 ET 路径并禁止按 ET 原格式导出', () => {
    const source = serializeWorkbook('xls');
    const workbook = readSpreadsheetSource(source, 'et');
    const snapshot = sheetJsToUniverWorkbook(workbook, '预算.et');

    expect(snapshot.sheets['sheet-1'].cellData?.[0]?.[0]?.v).toBe('项目');
    expect(() => snapshotToArrayBuffer(snapshot, workbook, 'et')).toThrow('ET 文件仅支持只读预览');
  });

  it('把多工作表、数值和公式转换为 Univer snapshot', () => {
    const snapshot = sheetJsToUniverWorkbook(workbookWithTwoSheets(), '模型.xlsx');

    expect(snapshot.sheetOrder).toEqual(['sheet-1', 'sheet-2']);
    expect(snapshot.sheets['sheet-1'].name).toBe('汇总');
    expect(snapshot.sheets['sheet-2'].name).toBe('明细');
    expect(snapshot.sheets['sheet-1'].cellData?.[1]?.[1]).toEqual({
      v: 12,
      t: CellValueType.NUMBER,
      s: { n: { pattern: '#,##0.00' } },
    });
    expect(snapshot.sheets['sheet-1'].cellData?.[2]?.[1]).toEqual(expect.objectContaining({
      f: '=SUM(B2:B2)',
      v: 12,
    }));
    expect(snapshot.sheets['sheet-1'].cellData?.[1]?.[2]?.s).toEqual({ n: { pattern: '0.00' } });
  });

  it('在原 OOXML 上只补丁值/公式/清空，并保留链接、批注、数字格式和未编辑公式', () => {
    const source = serializeWorkbook('xlsx');
    const original = readSpreadsheetSource(source, 'xlsx');
    const snapshot = sheetJsToUniverWorkbook(original, '模型.xlsx');
    snapshot.sheets['sheet-1'].cellData![1]![1] = {
      ...snapshot.sheets['sheet-1'].cellData![1]![1],
      v: 99,
      t: CellValueType.NUMBER,
    };
    snapshot.sheets['sheet-1'].cellData![2]![1] = {
      f: '=SUM(B2:B2)+1',
      v: 100,
      si: 'formula-runtime-id',
    };
    delete snapshot.sheets['sheet-1'].cellData![1]![3];

    const output = snapshotToArrayBuffer(snapshot, original, 'xlsx');
    const reopened = readSpreadsheetSource(output, 'xlsx');
    const summary = reopened.Sheets['汇总'];

    expect(new Uint8Array(output).slice(0, 2)).toEqual(new Uint8Array([0x50, 0x4b]));
    expect(reopened.SheetNames).toEqual(['汇总', '明细']);
    expect(summary.B2.v).toBe(99);
    expect(summary.B2.z).toBe('#,##0.00');
    expect(summary.B2.l?.Target).toBe('https://example.com/report');
    expect(summary.B2.c?.[0]?.t).toBe('保留这条批注');
    expect(summary.B3.f).toBe('SUM(B2:B2)+1');
    expect(summary.C2.f).toBe('B2*2');
    expect(summary.C2.z).toBe('0.00');
    expect(summary.D2).toBeUndefined();
    expect(zipEntryWithSuffix(output, '/xl/styles.xml')).toEqual(zipEntryWithSuffix(source, '/xl/styles.xml'));
    expect(zipEntryWithSuffix(output, '/xl/comments1.xml')).toEqual(zipEntryWithSuffix(source, '/xl/comments1.xml'));
  });

  it('空白 XLSX 首次输入的 null 编辑器字段不会被误判为格式修改', () => {
    const workbook = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet([]), 'Sheet1');
    const source = XLSX.write(workbook, { type: 'array', bookType: 'xlsx' }) as ArrayBuffer;
    const original = readSpreadsheetSource(source, 'xlsx');
    const snapshot = sheetJsToUniverWorkbook(original, '未命名.xlsx');
    snapshot.sheets['sheet-1'].cellData = {
      0: {
        0: {
          v: 'luna-diagnose-1',
          t: CellValueType.STRING,
          f: null,
          si: null,
          p: null,
          ref: null,
          custom: null,
        },
      },
    };

    const output = snapshotToArrayBuffer(snapshot, original, 'xlsx');

    expect(readSpreadsheetSource(output, 'xlsx').Sheets.Sheet1.A1.v).toBe('luna-diagnose-1');
  });

  it('拒绝改写数组/共享公式，并在普通值变化后移除陈旧 calcChain', () => {
    const arrayFormulaSource = replaceZipXml(serializeWorkbook('xlsx'), '/xl/worksheets/sheet1.xml', (xml) => (
      xml.replace('<f>SUM(B2:B2)</f>', '<f t="array" ref="B3:B3">SUM(B2:B2)</f>')
    ));
    const arrayFormulaWorkbook = readSpreadsheetSource(arrayFormulaSource, 'xlsx');
    const arrayFormulaSnapshot = sheetJsToUniverWorkbook(arrayFormulaWorkbook, '数组公式.xlsx');
    arrayFormulaSnapshot.sheets['sheet-1'].cellData![2]![1] = { f: '=B2+1', v: 13 };

    expect(() => snapshotToArrayBuffer(arrayFormulaSnapshot, arrayFormulaWorkbook, 'xlsx')).toThrow(
      '属于共享或数组公式',
    );

    const calcChainSource = addCalculationChain(serializeWorkbook('xlsx'));
    const calcChainWorkbook = readSpreadsheetSource(calcChainSource, 'xlsx');
    const calcChainSnapshot = sheetJsToUniverWorkbook(calcChainWorkbook, '计算链.xlsx');
    calcChainSnapshot.sheets['sheet-1'].cellData![1]![1] = {
      ...calcChainSnapshot.sheets['sheet-1'].cellData![1]![1],
      v: 99,
      t: CellValueType.NUMBER,
    };
    const output = snapshotToArrayBuffer(calcChainSnapshot, calcChainWorkbook, 'xlsx');
    const archive = XLSX.CFB.read(new Uint8Array(output), { type: 'buffer' }) as { FullPaths: string[] };

    expect(archive.FullPaths.some((path) => path.endsWith('/xl/calcChain.xml'))).toBe(false);
    expect(new TextDecoder().decode(zipEntryWithSuffix(output, '/xl/_rels/workbook.xml.rels'))).not.toContain('calcChain');
    expect(new TextDecoder().decode(zipEntryWithSuffix(output, '/[Content_Types].xml'))).not.toContain('calcChain');
    expect(readSpreadsheetSource(output, 'xlsx').Sheets['汇总'].B2.v).toBe(99);
  });

  it('忽略编辑器附带样式但拒绝工作表结构 mutation', () => {
    const source = serializeWorkbook('xlsx');
    const original = readSpreadsheetSource(source, 'xlsx');
    const styledSnapshot = sheetJsToUniverWorkbook(original, '模型.xlsx');
    styledSnapshot.sheets['sheet-1'].cellData![1]![1] = {
      ...styledSnapshot.sheets['sheet-1'].cellData![1]![1],
      s: 'unsupported-style',
    };
    const styledOutput = snapshotToArrayBuffer(styledSnapshot, original, 'xlsx');
    expect(readSpreadsheetSource(styledOutput, 'xlsx').Sheets['汇总'].B2.z).toBe('#,##0.00');

    const resizedSnapshot = sheetJsToUniverWorkbook(original, '模型.xlsx');
    resizedSnapshot.sheets['sheet-1'].rowData = { 1: { h: 48 } };
    expect(() => snapshotToArrayBuffer(resizedSnapshot, original, 'xlsx')).toThrow(
      '行高或列宽修改未保存',
    );

    const renamedSnapshot = sheetJsToUniverWorkbook(original, '模型.xlsx');
    renamedSnapshot.sheets['sheet-1'].name = '已改名';
    expect(() => snapshotToArrayBuffer(renamedSnapshot, original, 'xlsx')).toThrow(
      '工作表名称或顺序修改未保存',
    );
  });

  it('CSV 保存保留 BOM、sep 指令、分号、CRLF 和尾换行', () => {
    const source = new TextEncoder().encode('\uFEFFsep=;\r\n项目;数值\r\n收入;12\r\n').buffer;
    const original = readSpreadsheetSource(source, 'csv');
    const snapshot = sheetJsToUniverWorkbook(original, '模型.csv');
    snapshot.sheets['sheet-1'].cellData![1]![1] = {
      v: 88,
      t: CellValueType.NUMBER,
    };

    const output = snapshotToArrayBuffer(snapshot, original, 'csv');
    const bytes = new Uint8Array(output);
    const csv = new TextDecoder().decode(bytes.slice(3));

    expect(bytes.slice(0, 3)).toEqual(new Uint8Array([0xef, 0xbb, 0xbf]));
    expect(csv).toBe('sep=;\r\n项目;数值\r\n收入;88\r\n');
  });
});
