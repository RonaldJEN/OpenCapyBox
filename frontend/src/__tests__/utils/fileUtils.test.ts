import { describe, it, expect } from 'vitest';
import { isImageFile, getFileIcon, inferFileType, getFileExtLabel, getFileBadgeClass, getFileIconClass, normalizeFileType } from '../../utils/fileUtils';
import { File, FileArchive, FileCode, FileImage, FileSpreadsheet, FileText, Folder, Presentation } from 'lucide-react';

describe('fileUtils.isImageFile', () => {
  it('MIME 為 image/* 時應判斷為圖片', () => {
    expect(isImageFile({ name: 'x.bin', type: 'image/png' })).toBe(true);
  });

  it('type 為副檔名時也能判斷圖片', () => {
    expect(isImageFile({ name: 'photo.png', type: 'png' })).toBe(true);
    expect(isImageFile({ name: 'photo.jpeg', type: 'jpeg' })).toBe(true);
  });

  it('無 MIME 時依檔名副檔名判斷圖片', () => {
    expect(isImageFile({ name: 'diagram.webp' })).toBe(true);
    expect(isImageFile({ name: 'report.pdf' })).toBe(false);
  });
});

describe('fileUtils.getFileIcon', () => {
  it('圖片檔應回傳 FileImage icon', () => {
    const icon = getFileIcon({ name: 'avatar.png', type: 'png' });
    expect(icon).toBe(FileImage);
  });

  it('一般文字檔應回傳 FileText icon', () => {
    const icon = getFileIcon({ name: 'notes.md', type: 'md' });
    expect(icon).toBe(FileText);
  });

  it('按格式族区分文档、表格、演示、代码、图片、压缩包和未知文件', () => {
    expect(getFileIcon({ name: 'proposal.docx' })).toBe(FileText);
    expect(getFileIcon({ name: 'manual.pdf' })).toBe(FileText);
    expect(getFileIcon({ name: 'budget.xlsx' })).toBe(FileSpreadsheet);
    expect(getFileIcon({ name: 'slides.pptx' })).toBe(Presentation);
    expect(getFileIcon({ name: 'config.json' })).toBe(FileCode);
    expect(getFileIcon({ name: 'photo.png' })).toBe(FileImage);
    expect(getFileIcon({ name: 'bundle.zip' })).toBe(FileArchive);
    expect(getFileIcon({ name: 'README' })).toBe(File);
  });

  it('文件夹应使用 Folder icon，而不是按名称误判文件类型', () => {
    expect(getFileIcon({ name: '图片.png', is_directory: true })).toBe(Folder);
    expect(isImageFile({ name: '图片.png', is_directory: true })).toBe(false);
  });
});

describe('fileUtils.inferFileType', () => {
  it('優先從 MIME 子類型推斷', () => {
    expect(inferFileType('a.any', 'image/jpeg')).toBe('jpeg');
  });

  it('無 MIME 時從副檔名推斷', () => {
    expect(inferFileType('archive.tar.gz')).toBe('gz');
  });
});

describe('fileUtils.normalizeFileType', () => {
  it('無副檔名時應從 MIME 類型推斷', () => {
    expect(normalizeFileType('screenshot', 'image/png')).toBe('png');
  });

  it('WPS ET 檔應回傳試算表 icon', () => {
    expect(getFileIcon({ name: 'budget.et', type: 'application/octet-stream' })).toBe(FileSpreadsheet);
  });

  it('有副檔名時優先使用副檔名', () => {
    expect(normalizeFileType('report.docx', 'application/octet-stream')).toBe('docx');
  });
});

describe('fileUtils.readable labels and classes', () => {
  it('應優先顯示檔名副檔名，而不是 MIME 子類型', () => {
    expect(getFileExtLabel({
      name: 'report.xlsx',
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    })).toBe('XLSX');
  });

  it('應為常見文檔類型返回人類可讀標籤', () => {
    expect(getFileExtLabel({ name: 'proposal.docx' })).toBe('DOCX');
    expect(getFileExtLabel({ name: 'slides.pptx' })).toBe('PPTX');
    expect(getFileExtLabel({ name: 'manual.pdf' })).toBe('PDF');
    expect(getFileExtLabel({ name: 'budget.et' })).toBe('ET');
    expect(getFileExtLabel({ name: '研究', is_directory: true })).toBe('文件夹');
  });

  it('應為不同類型返回對應 token 類別', () => {
    expect(getFileBadgeClass({ name: 'report.xlsx' })).toContain('claude-success');
    expect(getFileBadgeClass({ name: 'manual.pdf' })).toContain('claude-error');
    expect(getFileIconClass({ name: 'proposal.docx' })).toContain('claude-accent');
  });
});
