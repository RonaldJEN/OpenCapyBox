// CodeBlock.tsx
import { useState } from 'react';
import { Check, Copy } from 'lucide-react';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism';
import FeedbackMessage from './FeedbackMessage';

interface CodeBlockProps {
  language: string;
  value: string;
  readingBlockId?: string;
}

export function CodeBlock({ language, value, readingBlockId }: CodeBlockProps) {
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);

  const handleCopy = async () => {
    setCopyFailed(false);
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch { setCopyFailed(true); }
  };

  return (
    <div className="chat-code-block not-prose relative my-4 rounded-2xl overflow-hidden bg-[#242424] text-[#ededed]">
      {/* Header with Language & Copy Button */}
      <div className="flex items-center justify-between px-5 pt-3 pb-1">
        <span className="text-sm font-medium">
          {language === 'bash' ? 'Bash' : language || '文本'}
        </span>
        <button
          type="button"
          onClick={handleCopy}
          className="p-2 rounded-md text-[#c9c9c9] hover:text-white hover:bg-white/10 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2"
          title={copyFailed ? '复制失败，请重试' : copied ? '已复制' : '复制代码'}
          aria-label={copyFailed ? '复制失败，请重试' : copied ? '已复制' : '复制代码'}
        >
          {copied ? <Check size={14} className="text-claude-success" /> : <Copy size={14} />}
        </button>
      </div>
      {copyFailed && <FeedbackMessage tone="error" onDismiss={() => setCopyFailed(false)} className="mx-5 my-1 text-xs text-red-300">复制失败，请重试</FeedbackMessage>}

      {/* Code Container */}
      <div data-reading-block={readingBlockId} className="text-sm font-mono overflow-auto select-text cursor-text">
        <SyntaxHighlighter
          language={language || 'text'}
          style={vscDarkPlus}
          customStyle={{
            margin: 0,
            padding: '1rem 1.25rem 1.25rem',
            background: 'transparent',
            border: 0,
            borderRadius: 0,
            fontSize: '13px',
            lineHeight: '1.6',
          }}
          wrapLongLines={true}
        >
          {value}
        </SyntaxHighlighter>
      </div>
    </div>
  );
}
