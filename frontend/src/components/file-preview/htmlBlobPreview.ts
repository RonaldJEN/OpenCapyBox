export const HTML_BLOB_PREVIEW_TTL_MS = 30 * 60 * 1000;

interface RetainedPreviewUrls {
  contentUrl: string;
  wrapperUrl: string;
  timeoutId: ReturnType<typeof setTimeout>;
}

interface BlobPreviewUrls {
  contentUrl: string;
  wrapperUrl: string;
}

const retainedPreviewUrls = new Map<string, RetainedPreviewUrls>();
let pagehideCleanupRegistered = false;

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function createSandboxedBlobPreview(content: Blob, title: string): BlobPreviewUrls {
  const contentUrl = URL.createObjectURL(content);
  const safeTitle = escapeHtml(title || 'HTML 文件预览');
  const wrapper = `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; frame-src blob:; style-src 'unsafe-inline'">
  <title>${safeTitle}</title>
  <style>html,body,iframe{width:100%;height:100%;margin:0;border:0}body{overflow:hidden;background:#fff}</style>
</head>
<body>
  <iframe title="${safeTitle}" src="${contentUrl}" sandbox="allow-scripts" referrerpolicy="no-referrer"></iframe>
</body>
</html>`;
  const wrapperUrl = URL.createObjectURL(new Blob([wrapper], { type: 'text/html;charset=utf-8' }));
  return { contentUrl, wrapperUrl };
}

function discardBlobPreview({ contentUrl, wrapperUrl }: BlobPreviewUrls): void {
  URL.revokeObjectURL(contentUrl);
  URL.revokeObjectURL(wrapperUrl);
}

function disconnectOpener(openedWindow: Window): void {
  try {
    openedWindow.opener = null;
  } catch {
    // 某些浏览器会在导航开始后立即限制 WindowProxy；包装页仍由 CSP + sandbox 隔离。
  }
}

function retainBlobPreview(
  { contentUrl, wrapperUrl }: BlobPreviewUrls,
  ttlMs: number,
): BlobPreviewUrls {
  const timeoutId = setTimeout(() => {
    revokeHtmlPreviewPair(wrapperUrl);
  }, ttlMs);
  retainedPreviewUrls.set(wrapperUrl, { contentUrl, wrapperUrl, timeoutId });
  ensurePagehideCleanup();
  return { contentUrl, wrapperUrl };
}

/**
 * 构造一个同样以 Blob URL 打开的全屏查看页。
 * 待查看 HTML 始终位于不带 allow-same-origin 的 sandbox iframe 中，
 * 因而能运行自身脚本，但拿不到 OpenCapyBox 的 Cookie/localStorage。
 */
export function openHtmlBlobPreview(
  html: string,
  title: string,
  openWindow: (url?: string | URL, target?: string, features?: string) => Window | null = window.open,
): BlobPreviewUrls {
  const previewUrls = createSandboxedBlobPreview(
    new Blob([html], { type: 'text/html;charset=utf-8' }),
    title,
  );

  let openedWindow: Window | null;
  try {
    // `noopener` feature 会让部分浏览器即使成功打开也返回 null，无法区分弹窗被拦截。
    // 包装页本身没有脚本，取得句柄后立刻断开 opener，兼顾可检测性和隔离。
    openedWindow = openWindow(previewUrls.wrapperUrl, '_blank');
  } catch (error) {
    discardBlobPreview(previewUrls);
    throw error;
  }

  if (!openedWindow) {
    discardBlobPreview(previewUrls);
    throw new Error('浏览器阻止了新标签页，请允许弹出窗口后重试');
  }

  disconnectOpener(openedWindow);
  return retainBlobPreview(previewUrls, HTML_BLOB_PREVIEW_TTL_MS);
}

/**
 * 将已鉴权取得的文件导航到事先同步打开的窗口。
 * 不论 Blob 的 MIME 是 HTML、SVG 还是其他类型，都只会成为
 * 不带 allow-same-origin 的 sandbox iframe 子文档，不会被顶层同源导航。
 */
export function openBlobPreviewInWindow(
  content: Blob,
  title: string,
  openedWindow: Window,
  ttlMs = HTML_BLOB_PREVIEW_TTL_MS,
): BlobPreviewUrls {
  const previewUrls = createSandboxedBlobPreview(content, title);
  try {
    disconnectOpener(openedWindow);
    openedWindow.location.replace(previewUrls.wrapperUrl);
  } catch (error) {
    discardBlobPreview(previewUrls);
    throw error;
  }
  return retainBlobPreview(previewUrls, ttlMs);
}

/** 供页面卸载或测试显式回收由“单独查看”持有的 Blob URL。 */
export function revokeRetainedHtmlPreviewUrls(): void {
  [...retainedPreviewUrls.keys()].forEach(revokeHtmlPreviewPair);
}

function revokeHtmlPreviewPair(wrapperUrl: string): void {
  const retained = retainedPreviewUrls.get(wrapperUrl);
  if (!retained) return;
  clearTimeout(retained.timeoutId);
  URL.revokeObjectURL(retained.contentUrl);
  URL.revokeObjectURL(retained.wrapperUrl);
  retainedPreviewUrls.delete(wrapperUrl);
}

function ensurePagehideCleanup(): void {
  if (pagehideCleanupRegistered || typeof window === 'undefined') return;
  window.addEventListener('pagehide', revokeRetainedHtmlPreviewUrls);
  pagehideCleanupRegistered = true;
}
