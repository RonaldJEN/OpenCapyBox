import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { estimateDataUrlSizeKB, compressImage, readFileAsDataUrl, createThumbnail } from '../../utils/imageUtils';

// =====================================================================
// Mock DOM APIs (jsdom 不提供 Canvas / Image 完整實現)
// =====================================================================

// toDataURL 回傳一個小型 JPEG Data URL（模擬壓縮結果 ~200 bytes base64）
const SMALL_JPEG_DATA_URL =
  'data:image/jpeg;base64,' + 'A'.repeat(300); // ~225 bytes payload

let mockCtx: {
  fillStyle: string;
  fillRect: ReturnType<typeof vi.fn>;
  drawImage: ReturnType<typeof vi.fn>;
};

let mockCanvasElement: {
  width: number;
  height: number;
  getContext: ReturnType<typeof vi.fn>;
  toDataURL: ReturnType<typeof vi.fn>;
};

// Mock Image 構造器
class MockImage {
  width = 4000;
  height = 3000;
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  private _srcValue = '';

  set src(val: string) {
    this._srcValue = val;
    setTimeout(() => this.onload?.(), 0);
  }

  get src() {
    return this._srcValue;
  }
}

function setupDomMocks() {
  mockCtx = {
    fillStyle: '',
    fillRect: vi.fn(),
    drawImage: vi.fn(),
  };

  mockCanvasElement = {
    width: 0,
    height: 0,
    getContext: vi.fn().mockReturnValue(mockCtx),
    toDataURL: vi.fn().mockReturnValue(SMALL_JPEG_DATA_URL),
  };

  const originalCreateElement = document.createElement.bind(document);
  vi.spyOn(document, 'createElement').mockImplementation((tagName: string) => {
    if (tagName === 'canvas') return mockCanvasElement as any;
    return originalCreateElement(tagName as any);
  });

  vi.stubGlobal('Image', MockImage);

  vi.stubGlobal('URL', {
    ...URL,
    createObjectURL: vi.fn().mockReturnValue('blob:mock-url'),
    revokeObjectURL: vi.fn(),
  });

  vi.stubGlobal('FileReader', MockFileReader);
}

// Mock FileReader
class MockFileReader {
  result: string | null = null;
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readAsDataURL(_file: File) {
    this.result = 'data:image/png;base64,' + 'B'.repeat(50);
    setTimeout(() => this.onload?.(), 0);
  }
}

beforeEach(() => {
  setupDomMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// =====================================================================
// Tests
// =====================================================================

describe('estimateDataUrlSizeKB', () => {
  it('正確估算 base64 payload 大小', () => {
    // 1000 chars base64 ≈ 750 bytes ≈ 0.732 KB
    const dataUrl = 'data:image/jpeg;base64,' + 'A'.repeat(1000);
    const sizeKB = estimateDataUrlSizeKB(dataUrl);
    expect(sizeKB).toBeCloseTo(0.732, 1);
  });

  it('沒有逗號分隔時回退到整體長度', () => {
    const raw = 'noseparator';
    const sizeKB = estimateDataUrlSizeKB(raw);
    expect(sizeKB).toBeCloseTo(raw.length / 1024, 2);
  });

  it('空字串回傳 0', () => {
    expect(estimateDataUrlSizeKB('')).toBe(0);
  });
});

describe('compressImage', () => {
  let largeFile: File;
  let smallFile: File;

  beforeEach(() => {
    // 模擬大檔案（2MB）
    largeFile = new File([new ArrayBuffer(2 * 1024 * 1024)], 'big.png', { type: 'image/png' });
    // 模擬小檔案（50KB），低於 skipThreshold
    smallFile = new File([new ArrayBuffer(50 * 1024)], 'tiny.png', { type: 'image/png' });
  });

  it('小於 skipThresholdKB 的圖片不壓縮，直接返回原始 DataURL', async () => {
    const result = await compressImage(smallFile, { skipThresholdKB: 100 });
    // 應該走 readFileAsDataUrl 路徑（MockFileReader 返回的值）
    expect(result).toContain('data:image/png;base64,');
  });

  it('大圖片會經過 Canvas 壓縮，返回 JPEG DataURL', async () => {
    const result = await compressImage(largeFile);
    // 應該走 Canvas 路徑，返回 mockCanvasElement.toDataURL 的值
    expect(result).toBe(SMALL_JPEG_DATA_URL);
    expect(mockCanvasElement.getContext).toHaveBeenCalledWith('2d');
  });

  it('Canvas context 繪製時填充白色背景', async () => {
    await compressImage(largeFile);
    expect(mockCtx.fillRect).toHaveBeenCalled();
    expect(mockCtx.drawImage).toHaveBeenCalled();
  });

  it('自定義 maxWidth/maxHeight 會反映到 canvas 尺寸', async () => {
    await compressImage(largeFile, { maxWidth: 1024, maxHeight: 768 });
    // MockImage 是 4000×3000，等比縮到 1024×768 → ratio = min(1024/4000, 768/3000) = 0.256
    // 結果: 1024×768
    expect(mockCanvasElement.width).toBeLessThanOrEqual(1024);
    expect(mockCanvasElement.height).toBeLessThanOrEqual(768);
  });
});

describe('readFileAsDataUrl', () => {
  it('讀取檔案返回 data URL', async () => {
    const file = new File(['test'], 'test.txt', { type: 'text/plain' });
    const result = await readFileAsDataUrl(file);
    expect(result).toContain('data:');
  });
});

describe('createThumbnail', () => {
  it('生成縮略圖（走 Canvas 路徑）', async () => {
    const result = await createThumbnail(SMALL_JPEG_DATA_URL, 200);
    expect(result).toBe(SMALL_JPEG_DATA_URL); // mock 返回固定值
    expect(mockCanvasElement.getContext).toHaveBeenCalled();
  });
});
