import { useId, useMemo, useState } from 'react';
import { ChevronRight, Globe2, MoreHorizontal } from 'lucide-react';
import type { ToolGroupItem } from '../utils/displayBlocks';
import { getWebToolSources, type WebToolSource } from '../utils/webToolSources';
import { useActivityDisclosure } from './useActivityDisclosure';

function SiteIcon({ source }: { source: WebToolSource }) {
  const [failed, setFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);
  return <span className="chat-site-icon" aria-hidden="true">
    {(!loaded || failed || !source.icon) && <Globe2 size={14} />}
    {source.icon && !failed && <img src={source.icon} alt="" loading="lazy" referrerPolicy="no-referrer"
      className={loaded ? '' : 'opacity-0'} onLoad={() => setLoaded(true)} onError={() => setFailed(true)} />}
  </span>;
}

export function WebToolItem({ item }: { item: ToolGroupItem }) {
  const [open, setOpen] = useActivityDisclosure(item.id || item.toolName, false);
  const [showDetails, setShowDetails] = useState(false);
  const detailId = useId();
  const sources = useMemo(() => getWebToolSources(item.result?.content || ''), [item.result?.content]);
  const running = item.status === 'running';
  const failed = item.status === 'failed';
  const label = running ? '正在搜索网站' : sources.length ? `已搜索 ${sources.length} 个网站`
    : failed ? '网站搜索失败' : item.status === 'unknown' ? '网站搜索结果待确认' : '未找到网站';
  const queries = typeof item.input?.query === 'string' ? [item.input.query]
    : Array.isArray(item.input?.queries) ? item.input.queries.filter((q): q is string => typeof q === 'string') : [];
  return <div className="chat-web-tool">
    <button type="button" className="chat-tool-row" aria-expanded={open} aria-controls={detailId} onClick={() => setOpen(!open)}>
      <span className={`chat-search-orbit ${running ? 'is-running' : ''}`} aria-hidden="true">
        <Globe2 size={17} strokeWidth={1.5} />
      </span>
      <span className="chat-tool-caption">{label}</span>
      {failed && sources.length > 0 && <span className="chat-tool-status text-claude-error">部分失败</span>}
      <ChevronRight size={14} strokeWidth={1.75} aria-hidden="true" className={`shrink-0 transition-transform motion-reduce:transition-none ${open ? 'rotate-90' : ''}`} />
    </button>
    {open && <div id={detailId} className="chat-web-details">
      {!item.result && queries.length > 0 && <p className="chat-search-query">{queries.join(' · ')}</p>}
      <div className="chat-site-list" aria-label="搜索到的网站">
        {sources.map((source) => <a key={source.domain} className="chat-site-pill" href={source.url}
          target="_blank" rel="noopener noreferrer" title={`${source.title}\n${source.url}`}>
          <SiteIcon key={source.icon || source.domain} source={source} /><span>{source.domain}</span>
        </a>)}
        {item.result && <button type="button" className="chat-search-more" aria-expanded={showDetails}
          aria-controls={`${detailId}-raw`} aria-label={failed ? '查看搜索结果与错误' : '查看搜索详情'}
          title={failed ? '查看搜索结果与错误' : '查看搜索详情'} onClick={() => setShowDetails(!showDetails)}>
          <MoreHorizontal size={16} aria-hidden="true" />
        </button>}
      </div>
      {showDetails && item.result && <div id={`${detailId}-raw`} className="chat-tool-output">
        {queries.length > 0 && <p className="chat-search-query">{queries.join(' · ')}</p>}
        <pre tabIndex={0} aria-label="搜索原始输出" className="max-h-64 overflow-auto whitespace-pre-wrap break-words">{item.result.content || item.result.error || '没有返回内容'}</pre>
        {item.result.error && item.result.content && <p>{item.result.error}</p>}
      </div>}
    </div>}
  </div>;
}
