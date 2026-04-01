// CodeBlock.tsx
import { useState } from 'react';
import { Check, Copy } from 'lucide-react';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism';

interface CodeBlockProps {
  language: string;
  value: string;
}

export function CodeBlock({ language, value }: CodeBlockProps) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(value);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="relative my-4 rounded-2xl overflow-hidden border border-claude-border group">
      {/* Header with Language & Copy Button */}
      <div className="flex items-center justify-between px-4 py-2 bg-claude-surface border-b border-claude-border">
        <span className="text-xs font-medium text-claude-muted font-mono uppercase tracking-wider">
          {language}
        </span>
        <button
          type="button"
          onClick={handleCopy}
          className="p-1.5 rounded-md text-claude-muted hover:text-claude-text hover:bg-claude-hover transition-all opacity-0 group-hover:opacity-100 focus:opacity-100"
          title="复制"
        >
          {copied ? <Check size={14} className="text-claude-success" /> : <Copy size={14} />}
        </button>
      </div>

      {/* Code Container */}
      <div className="text-sm font-mono overflow-auto select-text cursor-text">
        <SyntaxHighlighter
          language={language || 'text'}
          style={vscDarkPlus}
          customStyle={{
            margin: 0,
            padding: '1.5rem',
            background: '#1e1e1e', // Dark theme background
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
