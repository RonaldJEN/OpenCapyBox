import { useEffect, useMemo, useState } from 'react';
import { X, Download, AlertCircle, Code, Eye, Presentation, FileText, FileCode, FileImage, FileSpreadsheet, File } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism';
import mammoth from 'mammoth';
import DOMPurify from 'dompurify';
import { FileInfo } from '../types';
import { apiService } from '../services/api';
import { getFileExtLabel } from '../utils/fileUtils';

interface FilePreviewProps {
  file: FileInfo | null;
  sessionId: string;
  onClose: () => void;
}

export function FilePreview({ file, sessionId, onClose }: FilePreviewProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [textContent, setTextContent] = useState('');
  const [docxHtml, setDocxHtml] = useState('');
  const [tableData, setTableData] = useState<string[][]>([]);
  const [binaryPreviewUrl, setBinaryPreviewUrl] = useState('');
  const [viewMode, setViewMode] = useState<'rendered' | 'source'>('rendered');

  useEffect(() => {
    if (!file) {
      return;
    }

    setError('');
    setTextContent('');
    setDocxHtml('');
    setTableData([]);
    setBinaryPreviewUrl((prev) => {
      if (prev) URL.revokeObjectURL(prev);
      return '';
    });

    if (isTextFile(file.type) || isMarkdownFile(file.type) || isHtmlFile(file.type) || isCodeFile(file.type)) {
      void loadTextContent();
    } else if (isDocxFile(file.type)) {
      void loadDocxContent();
    } else if (isCsvFile(file.type)) {
      void loadCsvContent();
    } else if (isImageFile(file.type) || isPdfFile(file.type)) {
      void loadBinaryPreview();
    }
  }, [file]);

  useEffect(() => {
    return () => {
      if (binaryPreviewUrl) {
        URL.revokeObjectURL(binaryPreviewUrl);
      }
    };
  }, [binaryPreviewUrl]);

  const sanitizedHtmlContent = useMemo(() => {
    if (!textContent) {
      return '';
    }
    return DOMPurify.sanitize(textContent, { USE_PROFILES: { html: true } });
  }, [textContent]);

  const getPreviewApiUrl = () => {
    if (!file) return '';
    return `/api/sessions/${sessionId}/files/${encodeURIComponent(file.path)}?preview=true`;
  };

  const fetchPreviewResponse = async () => {
    const response = await fetch(getPreviewApiUrl(), {
      headers: {
        ...apiService.getAuthHeaders(),
      },
    });
    if (!response.ok) {
      throw new Error(`Failed to load file: ${response.status}`);
    }
    return response;
  };

  const loadTextContent = async () => {
    setLoading(true);
    setError('');
    try {
      const response = await fetchPreviewResponse();
      const text = await response.text();
      setTextContent(text);
    } catch (err) {
      console.error('Failed to load text content:', err);
      setError('加载文件内容失败');
    } finally {
      setLoading(false);
    }
  };

  const loadDocxContent = async () => {
    setLoading(true);
    setError('');
    try {
      const response = await fetchPreviewResponse();
      const arrayBuffer = await response.arrayBuffer();
      const result = await mammoth.convertToHtml({ arrayBuffer });
      setDocxHtml(DOMPurify.sanitize(result.value, { USE_PROFILES: { html: true } }));
      if (result.messages.length > 0) {
        console.warn('DOCX conversion warnings:', result.messages);
      }
    } catch (err) {
      console.error('Failed to load DOCX content:', err);
      setError('加载 DOCX 文件失败');
    } finally {
      setLoading(false);
    }
  };

  const loadCsvContent = async () => {
    setLoading(true);
    setError('');
    try {
      const response = await fetchPreviewResponse();
      const text = await response.text();
      const rows = text
        .split(/\r?\n/)
        .filter((line) => line.length > 0)
        .map((line) => line.split(','));
      setTableData(rows);
    } catch (err) {
      console.error('Failed to load CSV content:', err);
      setError('加载 CSV 文件失败');
    } finally {
      setLoading(false);
    }
  };

  const loadBinaryPreview = async () => {
    setLoading(true);
    setError('');
    try {
      const response = await fetchPreviewResponse();
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      setBinaryPreviewUrl((prev) => {
        if (prev) URL.revokeObjectURL(prev);
        return objectUrl;
      });
    } catch (err) {
      console.error('Failed to load binary content:', err);
      setError('加载文件预览失败');
    } finally {
      setLoading(false);
    }
  };

  const isImageFile = (type: string) => ['jpg', 'jpeg', 'png', 'gif', 'svg', 'webp', 'bmp'].includes(type.toLowerCase());
  const isPdfFile = (type: string) => type.toLowerCase() === 'pdf';
  const isMarkdownFile = (type: string) => ['md', 'markdown'].includes(type.toLowerCase());
  const isHtmlFile = (type: string) => ['html', 'htm'].includes(type.toLowerCase());
  const isCodeFile = (type: string) => {
    const codeTypes = ['js', 'ts', 'jsx', 'tsx', 'py', 'java', 'cpp', 'c', 'go', 'rs', 'sh', 'bash', 'sql', 'css', 'json', 'xml', 'yaml', 'yml', 'rb', 'php', 'swift', 'kt', 'scala', 'r', 'dart', 'lua'];
    return codeTypes.includes(type.toLowerCase());
  };
  const isTextFile = (type: string) => ['txt', 'log', 'ini', 'conf', 'cfg', 'toml'].includes(type.toLowerCase());
  const isDocxFile = (type: string) => ['docx', 'doc'].includes(type.toLowerCase());
  const isCsvFile = (type: string) => type.toLowerCase() === 'csv';
  const isSpreadsheetFile = (type: string) => ['xlsx', 'xls', 'csv'].includes(type.toLowerCase());
  const isPptxFile = (type: string) => ['pptx', 'ppt'].includes(type.toLowerCase());

  const getLanguage = (type: string): string => {
    const langMap: Record<string, string> = {
      'js': 'javascript', 'ts': 'typescript', 'jsx': 'jsx', 'tsx': 'tsx', 'py': 'python',
      'rb': 'ruby', 'sh': 'bash', 'yml': 'yaml', 'rs': 'rust', 'cpp': 'cpp', 'java': 'java',
      'go': 'go', 'sql': 'sql', 'json': 'json', 'xml': 'xml', 'html': 'html', 'css': 'css',
      'php': 'php', 'swift': 'swift', 'kt': 'kotlin', 'scala': 'scala',
    };
    return langMap[type.toLowerCase()] || type.toLowerCase();
  };

  const getFileIcon = (type: string, size: number = 20) => {
    if (isImageFile(type)) return <FileImage size={size} />;
    if (isSpreadsheetFile(type)) return <FileSpreadsheet size={size} />;
    if (isCodeFile(type) || isHtmlFile(type)) return <FileCode size={size} />;
    if (isTextFile(type) || isDocxFile(type) || isPdfFile(type) || isMarkdownFile(type)) return <FileText size={size} />;
    if (isPptxFile(type)) return <Presentation size={size} />;
    return <File size={size} />;
  };

  const handleDownload = async () => {
    if (!file) return;
    try {
      await apiService.downloadFile(sessionId, file.path);
    } catch (err) {
      console.error('Failed to download file:', err);
      setError('下载文件失败');
    }
  };

  if (!file) return null;

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center p-6 sm:p-12 animate-fade-in"
      onClick={onClose}
    >
      {/* 背景遮罩 */}
      <div className="absolute inset-0 bg-black/40 backdrop-blur-md" />

      {/* 预览卡片 */}
      <div
        className="relative w-full max-w-4xl h-full bg-white rounded-[32px] shadow-2xl flex flex-col overflow-hidden animate-zoom-in"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="h-16 border-b border-black/[0.06] flex items-center justify-between px-6 shrink-0 bg-white/80 backdrop-blur-xl">
          <div className="flex items-center space-x-4">
            <div className="w-10 h-10 bg-claude-surface rounded-xl flex items-center justify-center text-claude-accent">
              {getFileIcon(file.type, 20)}
            </div>
            <div>
              <h2 className="text-[15px] font-semibold tracking-tight text-claude-text">{file.name}</h2>
              <p className="text-[11px] text-claude-muted uppercase tracking-wider">
                {getFileExtLabel(file)} · {formatFileSize(file.size)}
              </p>
            </div>
          </div>
          <div className="flex items-center space-x-2">
            {/* HTML/MD 视图切换 */}
            {(isHtmlFile(file.type) || isMarkdownFile(file.type)) && (
              <div className="flex bg-claude-surface rounded-xl p-1 mr-2">
                <button
                  onClick={() => setViewMode('rendered')}
                  className={`p-2 rounded-lg transition-all ${
                    viewMode === 'rendered' ? 'bg-white text-claude-text shadow-sm' : 'text-claude-muted'
                  }`}
                  title="渲染视图"
                >
                  <Eye size={16} />
                </button>
                <button
                  onClick={() => setViewMode('source')}
                  className={`p-2 rounded-lg transition-all ${
                    viewMode === 'source' ? 'bg-white text-claude-text shadow-sm' : 'text-claude-muted'
                  }`}
                  title="源代码"
                >
                  <Code size={16} />
                </button>
              </div>
            )}
            <button
              onClick={handleDownload}
              className="p-2.5 hover:bg-claude-hover rounded-full transition-all text-claude-muted"
              title="下载文件"
            >
              <Download size={18} />
            </button>
            <button
              onClick={onClose}
              className="p-2.5 bg-black text-white rounded-full hover:opacity-80 transition-all active:scale-90 shadow-lg"
              title="关闭"
            >
              <X size={18} />
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-8 bg-claude-bg">
          {error ? (
            <div className="flex items-center gap-3 p-4 bg-red-50 border border-red-100 rounded-2xl">
              <AlertCircle className="w-5 h-5 text-claude-error" />
              <span className="text-claude-error font-medium">{error}</span>
            </div>
          ) : loading ? (
            <div className="flex items-center justify-center py-24">
              <div className="flex space-x-2">
                {[0, 1, 2].map(i => (
                  <div
                    key={i}
                    className="w-2 h-2 bg-claude-accent rounded-full animate-dot-pulse"
                    style={{ animationDelay: `${i * 200}ms` }}
                  />
                ))}
              </div>
            </div>
          ) : isImageFile(file.type) ? (
            <div className="h-full flex items-center justify-center">
              <div className="w-full max-w-2xl bg-white border border-black/[0.05] rounded-3xl shadow-xl p-4">
                <img
                  src={binaryPreviewUrl}
                  alt={file.name}
                  className="max-w-full max-h-[60vh] object-contain mx-auto rounded-2xl"
                  onError={() => setError('图片加载失败')}
                />
              </div>
            </div>
          ) : isPdfFile(file.type) ? (
            <div className="w-full h-full bg-white rounded-2xl overflow-hidden shadow-lg">
              <iframe
                src={binaryPreviewUrl}
                className="w-full h-full border-0"
                title={file.name}
                onError={() => setError('PDF 加载失败')}
              />
            </div>
          ) : isMarkdownFile(file.type) ? (
            viewMode === 'rendered' ? (
              <div className="max-w-2xl mx-auto bg-white p-12 rounded-2xl shadow-sm border border-black/[0.03]">
                <div className="prose max-w-none">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{textContent}</ReactMarkdown>
                </div>
              </div>
            ) : (
              <div className="bg-[#1C1C1E] rounded-2xl shadow-2xl ring-1 ring-white/10 overflow-hidden">
                <SyntaxHighlighter
                  language="markdown"
                  style={vscDarkPlus}
                  showLineNumbers
                  customStyle={{ margin: 0, borderRadius: 0, fontSize: '13px', background: 'transparent' }}
                >
                  {textContent}
                </SyntaxHighlighter>
              </div>
            )
          ) : isHtmlFile(file.type) ? (
            viewMode === 'rendered' ? (
              <div className="w-full h-full bg-white rounded-2xl overflow-hidden shadow-lg">
                <iframe srcDoc={sanitizedHtmlContent} className="w-full h-full border-0" title={file.name} sandbox="" />
              </div>
            ) : (
              <div className="bg-[#1C1C1E] rounded-2xl shadow-2xl ring-1 ring-white/10 overflow-hidden">
                <SyntaxHighlighter language="html" style={vscDarkPlus} showLineNumbers customStyle={{ margin: 0, borderRadius: 0, fontSize: '13px', background: 'transparent' }}>
                  {textContent}
                </SyntaxHighlighter>
              </div>
            )
          ) : isCodeFile(file.type) ? (
            <div className="bg-[#1C1C1E] rounded-2xl shadow-2xl ring-1 ring-white/10 overflow-hidden">
              <SyntaxHighlighter language={getLanguage(file.type)} style={vscDarkPlus} showLineNumbers customStyle={{ margin: 0, borderRadius: 0, fontSize: '13px', background: 'transparent' }}>
                {textContent}
              </SyntaxHighlighter>
            </div>
          ) : isTextFile(file.type) ? (
            <div className="max-w-2xl mx-auto bg-white p-8 rounded-2xl shadow-sm border border-black/[0.03]">
              <pre className="text-[13px] font-mono text-claude-text whitespace-pre-wrap break-words">{textContent}</pre>
            </div>
          ) : isDocxFile(file.type) ? (
            <div className="max-w-2xl mx-auto bg-white p-12 rounded-2xl shadow-sm border border-black/[0.03]">
              <div className="prose max-w-none" dangerouslySetInnerHTML={{ __html: docxHtml }} />
            </div>
          ) : isSpreadsheetFile(file.type) ? (
            isCsvFile(file.type) ? (
              <div className="bg-white rounded-2xl shadow-sm border border-black/[0.03] overflow-hidden">
                <div className="overflow-auto p-4 max-h-[60vh]">
                  <table className="min-w-full border-collapse">
                    <tbody>
                      {tableData.map((row, rowIndex) => (
                        <tr key={rowIndex} className={rowIndex === 0 ? 'bg-claude-surface font-medium' : ''}>
                          {row.map((cell, cellIndex) => (
                            <td key={cellIndex} className="border border-claude-border px-4 py-2 text-[13px] text-claude-text">
                              {cell}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ) : (
              <div className="text-center py-16 bg-white rounded-2xl shadow-sm border border-black/[0.03]">
                <AlertCircle className="w-10 h-10 mx-auto text-claude-border mb-3" />
                <p className="text-claude-text font-medium mb-2">出于安全原因，已禁用 XLS/XLSX 在线预览</p>
                <p className="text-claude-muted text-sm mb-5">请下载后使用本地办公软件打开。</p>
                <button
                  onClick={handleDownload}
                  className="inline-flex items-center gap-2 px-6 py-3 bg-black hover:bg-black/80 text-white rounded-xl transition-colors font-medium"
                >
                  <Download className="w-4 h-4" />
                  下载文件
                </button>
              </div>
            )
          ) : isPptxFile(file.type) ? (
            <div className="flex flex-col items-center justify-center py-12 px-6">
              <div className="mb-6 p-6 bg-[#FF9500]/10 rounded-full">
                <Presentation className="w-16 h-16 text-[#FF9500]" />
              </div>
              <div className="bg-white border border-black/[0.05] rounded-2xl p-6 max-w-md w-full mb-6">
                <h4 className="text-[16px] font-semibold text-claude-text mb-2">演示文稿文件</h4>
                <p className="text-[13px] text-claude-muted mb-4">
                  PowerPoint 演示文稿暂不支持在线预览，请下载后使用 Microsoft PowerPoint、WPS 或其他兼容软件打开。
                </p>
                <div className="space-y-2 text-[13px]">
                  <div className="flex justify-between items-center py-2 border-b border-claude-border">
                    <span className="text-claude-muted">文件名：</span>
                    <span className="font-medium text-claude-text truncate ml-2">{file.name}</span>
                  </div>
                  <div className="flex justify-between items-center py-2 border-b border-claude-border">
                    <span className="text-claude-muted">文件类型：</span>
                    <span className="font-medium text-claude-text">{getFileExtLabel(file)}</span>
                  </div>
                  <div className="flex justify-between items-center py-2">
                    <span className="text-claude-muted">文件大小：</span>
                    <span className="font-medium text-claude-text">{formatFileSize(file.size)}</span>
                  </div>
                </div>
              </div>
              <div className="flex flex-col sm:flex-row gap-3 w-full max-w-md">
                <button
                  onClick={handleDownload}
                  className="flex-1 inline-flex items-center justify-center gap-2 px-6 py-3 bg-black hover:bg-black/80 text-white rounded-xl transition-colors font-medium"
                >
                  <Download className="w-5 h-5" />
                  下载文件
                </button>
              </div>
            </div>
          ) : (
            <div className="text-center py-24">
              <AlertCircle className="w-12 h-12 mx-auto text-claude-border mb-3" />
              <p className="text-claude-muted mb-4">此文件类型不支持预览</p>
              <button
                onClick={handleDownload}
                className="inline-flex items-center gap-2 px-6 py-3 bg-black hover:bg-black/80 text-white rounded-xl transition-colors font-medium"
              >
                <Download className="w-4 h-4" />
                下载文件
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}
