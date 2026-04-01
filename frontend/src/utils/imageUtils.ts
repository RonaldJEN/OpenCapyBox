/**
 * 圖片壓縮工具
 *
 * 在前端使用 Canvas API 對圖片進行壓縮/縮放，降低發送給 LLM 的 base64 payload。
 * - 大於 MAX_SIZE_KB 的圖片會被縮放 + 質量壓縮至 ≤ 目標大小
 * - PNG 統一轉為 JPEG（透明背景填白色）以獲得更小體積
 * - 小於 SKIP_THRESHOLD_KB 的圖片直接返回，避免不必要的品質損失
 */

/** 壓縮選項 */
export interface CompressOptions {
  /** 最大寬度（px），超過會等比縮放。預設 2048 */
  maxWidth?: number;
  /** 最大高度（px），超過會等比縮放。預設 2048 */
  maxHeight?: number;
  /** JPEG 品質 0-1，預設 0.8 */
  quality?: number;
  /** 目標最大大小（KB），壓縮會迭代直到滿足。預設 500 */
  maxSizeKB?: number;
  /** 低於此大小（KB）的圖片不壓縮，直接返回原圖 Data URL。預設 100 */
  skipThresholdKB?: number;
}

const DEFAULT_OPTIONS: Required<CompressOptions> = {
  maxWidth: 2048,
  maxHeight: 2048,
  quality: 0.8,
  maxSizeKB: 500,
  skipThresholdKB: 100,
};

/**
 * 估算 Data URL 的實際 payload 大小（KB）。
 * data:image/xxx;base64, 前綴之後的內容按 base64 → binary 比例 ×0.75 計算。
 */
export function estimateDataUrlSizeKB(dataUrl: string): number {
  const commaIdx = dataUrl.indexOf(',');
  if (commaIdx < 0) return dataUrl.length / 1024;
  const base64Len = dataUrl.length - commaIdx - 1;
  return (base64Len * 0.75) / 1024;
}

/**
 * 將 File 讀成 HTMLImageElement
 */
function loadFileAsImage(file: File): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      resolve(img);
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error(`无法加载图片: ${file.name}`));
    };
    img.src = url;
  });
}

/**
 * 將 Data URL 載入為 HTMLImageElement
 */
function loadDataUrlAsImage(dataUrl: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error('无法加载图片'));
    img.src = dataUrl;
  });
}

/**
 * 在 Canvas 上繪製圖片並輸出 JPEG Data URL
 */
function drawAndExport(
  img: HTMLImageElement,
  targetWidth: number,
  targetHeight: number,
  quality: number,
): string {
  const canvas = document.createElement('canvas');
  canvas.width = targetWidth;
  canvas.height = targetHeight;
  const ctx = canvas.getContext('2d')!;

  // 填白色背景（處理 PNG 透明區域）
  ctx.fillStyle = '#FFFFFF';
  ctx.fillRect(0, 0, targetWidth, targetHeight);

  // 繪製圖片
  ctx.drawImage(img, 0, 0, targetWidth, targetHeight);

  return canvas.toDataURL('image/jpeg', quality);
}

/**
 * 計算等比縮放後的尺寸
 */
function fitDimensions(
  srcW: number,
  srcH: number,
  maxW: number,
  maxH: number,
): { width: number; height: number } {
  if (srcW <= maxW && srcH <= maxH) {
    return { width: srcW, height: srcH };
  }
  const ratio = Math.min(maxW / srcW, maxH / srcH);
  return {
    width: Math.round(srcW * ratio),
    height: Math.round(srcH * ratio),
  };
}

/**
 * 壓縮圖片 File → JPEG Data URL (≤ maxSizeKB)
 *
 * 1. 若原始檔案 < skipThresholdKB，直接用 FileReader 讀出原始 Data URL 返回
 * 2. 否則先等比縮放至 maxWidth×maxHeight，再用指定 quality 輸出 JPEG
 * 3. 若結果仍大於 maxSizeKB，迭代降低 quality（最低 0.3）和尺寸（最低 50%）
 *
 * @returns 壓縮後的 JPEG Data URL（`data:image/jpeg;base64,...`）
 */
export async function compressImage(
  file: File,
  options?: CompressOptions,
): Promise<string> {
  const opts = { ...DEFAULT_OPTIONS, ...options };

  // 小圖直接返回原始 Data URL
  if (file.size / 1024 < opts.skipThresholdKB) {
    return readFileAsDataUrl(file);
  }

  const img = await loadFileAsImage(file);
  const { width: fitW, height: fitH } = fitDimensions(
    img.width,
    img.height,
    opts.maxWidth,
    opts.maxHeight,
  );

  // 第一次嘗試
  let quality = opts.quality;
  let scaleW = fitW;
  let scaleH = fitH;
  let dataUrl = drawAndExport(img, scaleW, scaleH, quality);
  let sizeKB = estimateDataUrlSizeKB(dataUrl);

  // 迭代壓縮：最多 6 輪，每輪降低質量或尺寸
  let attempts = 0;
  while (sizeKB > opts.maxSizeKB && attempts < 6) {
    attempts++;
    if (quality > 0.35) {
      // 先降質量
      quality = Math.max(0.3, quality - 0.15);
    } else {
      // 質量無法再降，縮小尺寸
      scaleW = Math.round(scaleW * 0.7);
      scaleH = Math.round(scaleH * 0.7);
    }
    dataUrl = drawAndExport(img, scaleW, scaleH, quality);
    sizeKB = estimateDataUrlSizeKB(dataUrl);
  }

  console.log(
    `🖼️ 图片压缩: ${file.name} ${(file.size / 1024).toFixed(0)}KB → ${sizeKB.toFixed(0)}KB ` +
      `(${scaleW}×${scaleH}, quality=${quality.toFixed(2)})`,
  );

  return dataUrl;
}

/**
 * 生成縮略圖 Data URL（用於輸入框預覽）
 */
export async function createThumbnail(
  dataUrl: string,
  maxDim: number = 200,
): Promise<string> {
  const img = await loadDataUrlAsImage(dataUrl);
  const { width, height } = fitDimensions(img.width, img.height, maxDim, maxDim);
  return drawAndExport(img, width, height, 0.6);
}

/**
 * 原始讀取 File → Data URL（不壓縮）
 */
export function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(new Error(`读取文件失败: ${file.name}`));
    reader.readAsDataURL(file);
  });
}
