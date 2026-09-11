import { createContext, memo, useContext, useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { CodeBlock } from './CodeBlock';
import { AuthenticatedImage } from './AuthenticatedImage';
import type { AssistantFileReference, FileInfo } from '../types';
import { assistantFileReferenceToFileInfo, resolveAssistantFileReferenceFromHref } from '../utils/assistantFileRefs';

function readingPaths() {
  return (tree: any) => {
    const visit = (node: any, path: string) => {
      if (node.type === 'element' && !node.position?.start) {
        node.data = { ...node.data, readingPath: path };
      }
      node.children?.forEach((child: any, index: number) => visit(child, `${path}.${index}`));
    };
    visit(tree, '0');
  };
}

const FileLinkContext = createContext<{
  fileReferences: AssistantFileReference[];
  onOpenFile?: (file: FileInfo) => void;
}>({ fileReferences: [] });

function MarkdownFileLink({ children, href, ...props }: any) {
  const { fileReferences, onOpenFile } = useContext(FileLinkContext);
          const reference = typeof href === 'string'
            ? resolveAssistantFileReferenceFromHref(href, fileReferences)
            : null;
          if (reference && onOpenFile) {
            return (
              <a
                href={href}
                className="text-blue-600 hover:underline underline-offset-2 cursor-pointer"
                onClick={(event) => {
                  event.preventDefault();
                  onOpenFile(assistantFileReferenceToFileInfo(reference));
                }}
                {...props}
              >
                {children}
              </a>
            );
          }
          const externalHref = typeof href === 'string'
            && /^(?:https?:|mailto:|#|\/api\/)/i.test(href);
          if (!externalHref) {
            return (
              <span
                className="text-claude-secondary"
                title="没有可验证的文件版本，无法打开"
              >
                {children}
              </span>
            );
          }
          return (
            <a
              href={href}
              className="text-blue-600 hover:underline underline-offset-2"
              target="_blank"
              rel="noopener noreferrer"
              {...props}
            >
              {children}
            </a>
          );
}

function AssistantMarkdownView({
  content,
  readingNodeId,
  fileReferences,
  onOpenFile,
}: {
  content: string;
  readingNodeId: string;
  fileReferences: AssistantFileReference[];
  onOpenFile?: (file: FileInfo) => void;
}) {
  const components = useMemo(() => {
    const readingId = (node: any) => `answer:${readingNodeId}:${node.tagName}:${node.position?.start?.offset ?? node.data?.readingPath}`;
    return {
        code: ({ node, className, children, ...props }: any) => {
          const match = /language-(\w+)/.exec(className || '');
          const language = match ? match[1] : '';
          const isInline = !match && !children?.toString().includes('\n');

          if (isInline) {
            return (
              <code
                className="px-1.5 py-0.5 bg-claude-surface text-orange-700 rounded-md text-[0.875em] font-mono border border-claude-border"
                {...props}
              >
                {children}
              </code>
            );
          }

          return (
            <CodeBlock
              language={language}
              value={String(children).replace(/\n$/, '')}
              readingBlockId={readingId(node)}
            />
          );
        },
        pre: ({ children, ...props }: any) => {
          if (children && typeof children === 'object' && 'props' in children) {
            return <>{children}</>;
          }
          return (
            <pre className="bg-[#1e1e1e] text-gray-300 rounded-2xl overflow-x-auto p-4 my-4 whitespace-pre-wrap break-words font-mono text-[13px]" {...props}>
              {children}
            </pre>
          );
        },
        img: ({ node, src: imgSrc, alt: imgAlt, ...imgRest }: any) => {
          if (typeof imgSrc !== 'string') {
            return null;
          }
          if (imgSrc.startsWith('/api/')) {
            return (
              <AuthenticatedImage
                data-reading-block={readingId(node)}
                src={imgSrc}
                alt={imgAlt || ''}
                className="max-w-full rounded-lg my-2"
                {...imgRest}
              />
            );
          }
          if (/^(https?:|data:image\/|blob:)/i.test(imgSrc)) {
            return <img data-reading-block={readingId(node)} src={imgSrc} alt={imgAlt || ''} className="max-w-full rounded-lg my-2" {...imgRest} />;
          }
          return null;
        },
        a: MarkdownFileLink,
        blockquote: ({ children, ...props }: any) => (
          <blockquote
            className="border-l-[3px] border-claude-border pl-4 my-5 not-italic text-claude-text"
            {...props}
          >
            {children}
          </blockquote>
        ),
        ul: ({ children, ...props }: any) => (
          <ul className="space-y-1 my-2" {...props}>{children}</ul>
        ),
        ol: ({ children, ...props }: any) => (
          <ol className="space-y-1 my-2" {...props}>{children}</ol>
        ),
        p: ({ node, children, ...props }: any) => (
          <p data-reading-block={readingId(node)} {...props}>{children}</p>
        ),
        li: ({ node, children, ...props }: any) => (
          <li data-reading-block={readingId(node)} className="text-claude-text" {...props}>{children}</li>
        ),
        h1: ({ node, children, ...props }: any) => (
          <h1 data-reading-block={readingId(node)} className="text-[1.5em] font-semibold text-claude-text tracking-tight mt-6 mb-3" {...props}>{children}</h1>
        ),
        h2: ({ node, children, ...props }: any) => (
          <h2 data-reading-block={readingId(node)} className="text-[1.25em] font-semibold text-claude-text tracking-tight mt-5 mb-2" {...props}>{children}</h2>
        ),
        h3: ({ node, children, ...props }: any) => (
          <h3 data-reading-block={readingId(node)} className="text-[1.1em] font-semibold text-claude-text tracking-tight mt-4 mb-2" {...props}>{children}</h3>
        ),
        h4: ({ node, children, ...props }: any) => <h4 data-reading-block={readingId(node)} {...props}>{children}</h4>,
        h5: ({ node, children, ...props }: any) => <h5 data-reading-block={readingId(node)} {...props}>{children}</h5>,
        h6: ({ node, children, ...props }: any) => <h6 data-reading-block={readingId(node)} {...props}>{children}</h6>,
        table: ({ children, ...props }: any) => (
          <div className="chat-markdown-table">
            <table className="min-w-full" {...props}>{children}</table>
          </div>
        ),
        thead: ({ children, ...props }: any) => (
          <thead {...props}>{children}</thead>
        ),
        th: ({ node, children, ...props }: any) => (
          <th data-reading-block={readingId(node)} {...props}>{children}</th>
        ),
        td: ({ node, children, ...props }: any) => (
          <td data-reading-block={readingId(node)} {...props}>{children}</td>
        ),
      };
  }, [readingNodeId]);
  const fileLinks = useMemo(() => ({ fileReferences, onOpenFile }), [fileReferences, onOpenFile]);
  return (
    <FileLinkContext.Provider value={fileLinks}>
    <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[readingPaths]} components={components}
    >
      {content}
    </ReactMarkdown>
    </FileLinkContext.Provider>
  );
}


export const AssistantMarkdown = memo(AssistantMarkdownView);
