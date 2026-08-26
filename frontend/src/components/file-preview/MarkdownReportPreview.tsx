import {
  Children,
  isValidElement,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type AnchorHTMLAttributes,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
  type TableHTMLAttributes,
} from 'react';
import { PanelLeftClose, PanelLeftOpen, X } from 'lucide-react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

import { apiService } from '../../services/api';
import { AuthenticatedImage } from '../AuthenticatedImage';
import { openBlobPreviewInWindow } from './htmlBlobPreview';

interface MarkdownReportPreviewProps {
  content: string;
  filePath?: string;
  buildSessionFileUrl?: (resolvedPath: string) => string;
}

interface MarkdownHeading {
  depth: number;
  text: string;
  id: string;
}

const EXTERNAL_TARGET_PATTERN = /^[a-z][a-z\d+.-]*:/i;
const MARKDOWN_LINK_BLOB_TTL_MS = 60_000;

function isSessionRelativeTarget(target: string): boolean {
  const trimmed = target.trim();
  return Boolean(trimmed)
    && !trimmed.startsWith('#')
    && !trimmed.startsWith('//')
    && !EXTERNAL_TARGET_PATTERN.test(trimmed);
}

/**
 * 将 Markdown 相对路径限制在当前 session 根目录内。
 * 合法的 `../` 可以返回上级目录，但不能越过根目录；编码后的 traversal 同样拒绝。
 */
export function resolveMarkdownSessionPath(
  markdownFilePath: string,
  target: string,
): string | null {
  if (!isSessionRelativeTarget(target)) return null;

  const targetPath = target.trim().split(/[?#]/, 1)[0];
  let decodedTarget: string;
  try {
    decodedTarget = decodeURIComponent(targetPath).replace(/\\/g, '/');
  } catch {
    return null;
  }
  if (!decodedTarget || decodedTarget.includes('\0')) return null;

  const fromRoot = decodedTarget.startsWith('/');
  const baseSegments = fromRoot
    ? []
    : markdownFilePath.replace(/\\/g, '/').replace(/^\/+/, '').split('/').slice(0, -1);
  const resolved: string[] = [];

  for (const segment of [...baseSegments, ...decodedTarget.split('/')]) {
    if (!segment || segment === '.') continue;
    if (segment === '..') {
      if (resolved.length === 0) return null;
      resolved.pop();
      continue;
    }
    resolved.push(segment);
  }

  return resolved.length > 0 ? resolved.join('/') : null;
}

function flattenText(node: ReactNode): string {
  return Children.toArray(node)
    .map((child) => {
      if (typeof child === 'string' || typeof child === 'number') return String(child);
      if (isValidElement<{ children?: ReactNode }>(child)) return flattenText(child.props.children);
      return '';
    })
    .join('');
}

function plainHeadingText(value: string): string {
  return value
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/[`*_~]/g, '')
    .trim();
}

function slugBase(value: string): string {
  const slug = value
    .normalize('NFKC')
    .toLowerCase()
    .replace(/[^\p{Letter}\p{Number}\s-]/gu, '')
    .trim()
    .replace(/[\s_-]+/g, '-');
  return slug || 'section';
}

function uniqueSlug(value: string, counts: Map<string, number>): string {
  const base = slugBase(value);
  const count = counts.get(base) || 0;
  counts.set(base, count + 1);
  return count === 0 ? base : `${base}-${count + 1}`;
}

export function extractMarkdownHeadings(content: string): MarkdownHeading[] {
  const headings: MarkdownHeading[] = [];
  const counts = new Map<string, number>();
  let fenced = false;

  content.split(/\r?\n/).forEach((line) => {
    if (/^\s*(```|~~~)/.test(line)) {
      fenced = !fenced;
      return;
    }
    if (fenced) return;

    const match = line.match(/^\s{0,3}(#{1,3})\s+(.+?)\s*#*\s*$/);
    if (!match) return;
    const text = plainHeadingText(match[2]);
    if (!text) return;
    headings.push({ depth: match[1].length, text, id: uniqueSlug(text, counts) });
  });

  return headings;
}

function createHeadingComponent(
  depth: 1 | 2 | 3,
  counts: Map<string, number>,
): NonNullable<Components[`h${typeof depth}`]> {
  const Heading = ({ children }: { children?: ReactNode }) => {
    const Tag = `h${depth}` as const;
    const id = uniqueSlug(flattenText(children), counts);
    return <Tag id={id}>{children}</Tag>;
  };
  return Heading;
}

interface AuthenticatedMarkdownLinkProps extends AnchorHTMLAttributes<HTMLAnchorElement> {
  previewUrl: string;
  previewTitle: string;
}

function AuthenticatedMarkdownLink({
  previewUrl,
  previewTitle,
  children,
  ...props
}: AuthenticatedMarkdownLinkProps) {
  const [error, setError] = useState('');

  const handleClick = async (event: ReactMouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    setError('');
    const popup = window.open('about:blank', '_blank');
    if (!popup) {
      setError('浏览器阻止了新标签页');
      return;
    }
    try {
      popup.opener = null;
      const response = await fetch(previewUrl, { headers: apiService.getAuthHeaders() });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      openBlobPreviewInWindow(
        await response.blob(),
        previewTitle || '会话文件预览',
        popup,
        MARKDOWN_LINK_BLOB_TTL_MS,
      );
    } catch (linkError) {
      popup.close();
      console.error('Failed to open Markdown session link:', linkError);
      setError('无法打开会话文件');
    }
  };

  return (
    <>
      <a {...props} href={previewUrl} onClick={(event) => void handleClick(event)}>{children}</a>
      {error && <span className="ml-1 text-xs text-claude-error" role="status">{error}</span>}
    </>
  );
}

export function MarkdownReportPreview({
  content,
  filePath = '',
  buildSessionFileUrl,
}: MarkdownReportPreviewProps) {
  const headings = useMemo(() => extractMarkdownHeadings(content), [content]);
  const [tocOpen, setTocOpen] = useState(false);
  const [activeHeadingId, setActiveHeadingId] = useState(headings[0]?.id || '');
  const tocId = useId();
  const tocToggleRef = useRef<HTMLButtonElement>(null);
  const reportViewportRef = useRef<HTMLDivElement>(null);
  const renderSlugCounts = new Map<string, number>();

  useEffect(() => {
    setTocOpen(false);
    setActiveHeadingId(headings[0]?.id || '');
  }, [headings]);

  useEffect(() => {
    const viewport = reportViewportRef.current;
    if (!viewport || headings.length === 0 || typeof IntersectionObserver === 'undefined') {
      return undefined;
    }

    const headingIds = new Set(headings.map((heading) => heading.id));
    const headingElements = Array.from(
      viewport.querySelectorAll<HTMLElement>('h1[id], h2[id], h3[id]'),
    ).filter((heading) => headingIds.has(heading.id));
    if (headingElements.length === 0) return undefined;

    const observer = new IntersectionObserver((entries) => {
      const visible = entries
        .filter((entry) => entry.isIntersecting)
        .sort((left, right) => left.boundingClientRect.top - right.boundingClientRect.top);
      const current = visible[0]?.target as HTMLElement | undefined;
      if (current?.id) setActiveHeadingId(current.id);
    }, {
      root: viewport,
      rootMargin: '-6% 0px -78% 0px',
      threshold: [0, 1],
    });
    headingElements.forEach((heading) => observer.observe(heading));
    return () => observer.disconnect();
  }, [headings]);

  const closeToc = (restoreFocus = false) => {
    setTocOpen(false);
    if (restoreFocus) {
      window.requestAnimationFrame(() => tocToggleRef.current?.focus());
    }
  };

  const components: Components = {
    h1: createHeadingComponent(1, renderSlugCounts),
    h2: createHeadingComponent(2, renderSlugCounts),
    h3: createHeadingComponent(3, renderSlugCounts),
    table: ({ children, ...props }: TableHTMLAttributes<HTMLTableElement>) => (
      <div className="file-preview-markdown-table" tabIndex={0} aria-label="可横向滚动的表格">
        <table {...props}>{children}</table>
      </div>
    ),
    a: ({ href = '', children, node: _node, ...props }) => {
      if (buildSessionFileUrl && isSessionRelativeTarget(href)) {
        const resolvedPath = resolveMarkdownSessionPath(filePath, href);
        if (!resolvedPath) {
          return (
            <span aria-disabled="true" title="相对路径超出会话目录">
              {children}
            </span>
          );
        }
        return (
          <AuthenticatedMarkdownLink
            {...props}
            previewUrl={buildSessionFileUrl(resolvedPath)}
            previewTitle={flattenText(children)}
          >
            {children}
          </AuthenticatedMarkdownLink>
        );
      }
      return (
        <a
          {...props}
          href={href}
          target={href.startsWith('#') ? undefined : '_blank'}
          rel={href.startsWith('#') ? undefined : 'noopener noreferrer'}
        >
          {children}
        </a>
      );
    },
    img: ({ src = '', alt = '', node: _node, ...props }) => {
      if (buildSessionFileUrl && isSessionRelativeTarget(src)) {
        const resolvedPath = resolveMarkdownSessionPath(filePath, src);
        if (!resolvedPath) {
          return <span role="img" aria-label={`${alt || '图片'}路径无效`}>[图片路径无效]</span>;
        }
        return (
          <AuthenticatedImage
            {...props}
            src={buildSessionFileUrl(resolvedPath)}
            alt={alt}
            fallback={<span role="img" aria-label={`${alt || '图片'}加载失败`}>[图片加载失败]</span>}
          />
        );
      }
      return <img {...props} src={src} alt={alt} />;
    },
  };

  return (
    <div className="file-preview-markdown-workspace">
      <div className="file-preview-markdown-toolbar" role="toolbar" aria-label="Markdown 阅读工具">
        <div className="file-preview-markdown-toolbar-actions">
          {headings.length > 1 ? (
            <button
              ref={tocToggleRef}
              type="button"
              className="file-preview-markdown-toolbar-button"
              aria-label={tocOpen ? '收起目录' : '展开目录'}
              title={tocOpen ? '收起目录' : '展开目录'}
              aria-expanded={tocOpen}
              aria-controls={tocId}
              onClick={() => setTocOpen((open) => !open)}
            >
              {tocOpen ? <PanelLeftClose size={16} aria-hidden /> : <PanelLeftOpen size={16} aria-hidden />}
            </button>
          ) : null}
          <span className="file-preview-markdown-toolbar-label">阅读视图</span>
        </div>
        <span className="file-preview-markdown-heading-count">
          {headings.length > 0 ? `${headings.length} 个章节` : '无目录'}
        </span>
      </div>

      <div className="file-preview-report-layout" data-toc-open={tocOpen ? 'true' : 'false'}>
        {tocOpen && headings.length > 1 ? (
          <>
            <div className="file-preview-report-toc-backdrop" aria-hidden="true" />
            <aside id={tocId} className="file-preview-report-toc">
              <div className="file-preview-report-toc-header">
                <span className="file-preview-report-toc-title">目录</span>
                <button
                  type="button"
                  className="file-preview-report-toc-close"
                  aria-label="收起目录"
                  title="收起目录"
                  onClick={() => closeToc(true)}
                >
                  <X size={15} aria-hidden />
                </button>
              </div>
              <nav className="file-preview-report-toc-scroll" aria-label="文档目录">
                <ol>
                  {headings.map((heading) => (
                    <li key={heading.id} data-depth={heading.depth}>
                      <a
                        href={`#${heading.id}`}
                        aria-current={activeHeadingId === heading.id ? 'location' : undefined}
                        onClick={() => setActiveHeadingId(heading.id)}
                      >
                        {heading.text}
                      </a>
                    </li>
                  ))}
                </ol>
              </nav>
            </aside>
          </>
        ) : null}

        <div ref={reportViewportRef} className="file-preview-report-viewport">
          <article className="file-preview-report prose">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
              {content}
            </ReactMarkdown>
          </article>
        </div>
      </div>
    </div>
  );
}
