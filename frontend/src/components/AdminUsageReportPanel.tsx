import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Download, ArrowDown, ArrowUp } from 'lucide-react';
import {
  exportAdminUsageReport, getAdminUsageReport,
  type UsageReportData, type UsageReportParams, type UsageReportView,
} from '../services/adminApi';
import FeedbackMessage from './FeedbackMessage';
import './AdminUsageReportPanel.css';

const VIEWS: Array<[UsageReportView, string]> = [['accounts', '按账号汇总'], ['models', '按模型汇总'], ['details', '账号×模型明细']];
const METRICS: Array<[string, string]> = [
  ['open_accounts', '当前开放账号'], ['active_accounts', '期间活跃账号'], ['unused_accounts', '期间零使用账号'],
  ['calls', '调用次数'], ['error_calls', '错误调用'], ['input_tokens', '输入 Token'],
  ['output_tokens', '输出 Token'], ['total_tokens', '总 Token'],
];
const COMMON: Array<[string, string]> = [
  ['calls', '调用次数'], ['error_calls', '错误调用'], ['input_tokens', '输入 Token'],
  ['output_tokens', '输出 Token'], ['total_tokens', '总 Token'], ['missing_usage_calls', 'Token 不完整调用'],
];
const COLUMNS: Record<UsageReportView, Array<[string, string]>> = {
  accounts: [['account', '账号'], ['model_count', '使用模型数'], ...COMMON, ['share', '期间占比'], ['usage_status', '使用状态']],
  models: [['model', '模型'], ['active_accounts', '活跃账号'], ...COMMON, ['share', '期间占比']],
  details: [['account', '账号'], ['model', '模型'], ...COMMON, ['first_call_at', '期间首次调用'], ['last_call_at', '期间末次调用']],
};
const number = new Intl.NumberFormat('zh-CN');
const timestamp = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
});

function preset(days: number) {
  const today = new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10);
  return { start_date: new Date(Date.parse(today) - (days - 1) * 86400000).toISOString().slice(0, 10), end_date: today };
}

export default function AdminUsageReportPanel({ refreshToken = 0 }: { refreshToken?: number }) {
  const [query, setQuery] = useState<UsageReportParams>(() => ({ ...preset(7), view: 'accounts', page: 1, page_size: 20 }));
  const [startDate, setStartDate] = useState(() => preset(7).start_date);
  const [endDate, setEndDate] = useState(() => preset(7).end_date);
  const [shortcut, setShortcut] = useState<number | null>(7);
  const [data, setData] = useState<UsageReportData | null>(null);
  const [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState('');
  const [accountFilter, setAccountFilter] = useState('');
  const [modelFilter, setModelFilter] = useState('');
  const previousRefresh = useRef(refreshToken);
  const exportInFlight = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    const params = previousRefresh.current === refreshToken ? query : { ...query, as_of: undefined };
    previousRefresh.current = refreshToken;
    setLoading(true);
    setError('');
    getAdminUsageReport(params, controller.signal).then((result) => {
      if (!controller.signal.aborted) setData(result);
    }).catch(() => {
      if (!controller.signal.aborted) setError('查询失败，请重试。下方保留上一次成功查询的结果。');
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [query, refreshToken]);

  function changePeriod(range: { start_date: string; end_date: string }) {
    setQuery({ ...range, view: data?.view || 'accounts', sort_by: 'total_tokens', direction: 'desc', page: 1, page_size: data?.page_size || 20 });
    setAccountFilter('');
    setModelFilter('');
  }

  function submitDates(event: FormEvent) {
    event.preventDefault();
    if (!startDate || !endDate || startDate > endDate) {
      setError('请选择有效日期，开始日期不能晚于截止日期。');
      return;
    }
    if ((Date.parse(endDate) - Date.parse(startDate)) / 86400000 >= 366) {
      setError('查询时间范围过长，请缩小范围（最多366天）。');
      return;
    }
    if (endDate > preset(7).end_date) {
      setError('截止日期不能晚于今天。');
      return;
    }
    changePeriod({ start_date: startDate, end_date: endDate });
  }

  function changeTable(changes: UsageReportParams) {
    if (!data) return;
    setQuery({ ...data.period, view: data.view, sort_by: data.sort_by, direction: data.direction,
      account_filter: data.account_filter, model_filter: data.model_filter,
      page: 1, page_size: data.page_size, ...changes });
  }

  async function download() {
    if (!data || exportInFlight.current) return;
    exportInFlight.current = true;
    setExporting(true);
    setError('');
    try {
      const blob = await exportAdminUsageReport(data.period);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `OpenCapyBox使用数据报表_${data.period.start_date.replace(/-/g, '')}_${data.period.end_date.replace(/-/g, '')}.xlsx`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch {
      setError('导出失败，请重试。');
    } finally {
      exportInFlight.current = false;
      setExporting(false);
    }
  }

  function cell(row: UsageReportData['rows'][number], key: string) {
    const value = row[key];
    if (value === null || value === undefined) return '—';
    if (key === 'share') return `${(Number(value) * 100).toFixed(2)}%`;
    if (key.endsWith('_at')) return timestamp.format(new Date(String(value)));
    if (key === 'account') return <><span>{String(value)}</span>{!row.enabled && <span className="admin-status disabled usage-disabled">停用或已不存在</span>}<div className="admin-subline">{row.user_id !== value ? String(row.user_id) : ''}</div></>;
    if (key === 'model') return <><span>{String(value)}</span><div className="admin-subline">{row.model_id && row.model_id !== value ? String(row.model_id) : ''}</div></>;
    return typeof value === 'number' ? number.format(value) : String(value);
  }

  return <section className="usage-report" aria-label="使用数据报表">
    <div className="admin-card usage-controls">
      <form className="usage-date-form" onSubmit={submitDates}>
        <div className="usage-presets" aria-label="快捷日期">
          {[7, 30].map(days => <button type="button" key={days} className={`admin-button ${shortcut === days ? 'admin-primary-button' : ''}`}
            aria-pressed={shortcut === days} onClick={() => {
              const range = preset(days);
              setShortcut(days); setStartDate(range.start_date); setEndDate(range.end_date); changePeriod(range);
            }}>近 {days} 天</button>)}
        </div>
        <label className="admin-field">开始日期<input type="date" className="admin-input" value={startDate} required
          max={preset(7).end_date} onChange={event => { setStartDate(event.target.value); setShortcut(null); }} /></label>
        <label className="admin-field">截止日期<input type="date" className="admin-input" value={endDate} required
          max={preset(7).end_date} onChange={event => { setEndDate(event.target.value); setShortcut(null); }} /></label>
        <button className="admin-button admin-primary-button" type="submit">查询</button>
      </form>
      <div className="usage-export">
        <button className="admin-button" onClick={() => void download()} disabled={!data || loading || exporting}>
          <Download size={15} aria-hidden="true" />{exporting ? '导出中…' : '导出完整报表'}
        </button>
        <span className="admin-subline">按下方已查询日期导出三个 Sheet，不受分页或明细筛选影响</span>
      </div>
    </div>
    {error && <FeedbackMessage tone="error" onDismiss={() => setError('')}>{error}</FeedbackMessage>}
    {loading && <div role="status" className="usage-loading">正在查询使用数据…</div>}
    {data && <>
      <div className="usage-result-heading">
        <h2>{data.period.start_date} 至 {data.period.end_date}</h2>
        <span>查询截止：{timestamp.format(new Date(data.period.as_of))}（北京时间）</span>
      </div>
      <div className="usage-metrics">
        {METRICS.map(([key, label]) => <div className="admin-card admin-metric" key={key}>
          <div className="admin-metric-label">{label}</div><div className="admin-metric-value">{number.format(data.summary[key])}</div>
        </div>)}
      </div>
      <div className="usage-notes">
        <p>{data.note}</p>
        {data.summary.missing_usage_calls > 0 && <p className="usage-incomplete">所选期间有 {number.format(data.summary.missing_usage_calls)} 条调用缺少完整 Token 数据，汇总仅包含已取得的数值。</p>}
      </div>
      <div className="admin-card usage-table-card" aria-busy={loading}>
        <div className="usage-table-toolbar">
          <div className="usage-views" aria-label="统计维度">
            {VIEWS.map(([view, label]) => <button key={view} className={`admin-button ${data.view === view ? 'admin-primary-button' : ''}`}
              aria-pressed={data.view === view} disabled={loading} onClick={() => changeTable({ view, sort_by: 'total_tokens', direction: 'desc' })}>{label}</button>)}
          </div>
          <span className="admin-subline">{data.view === 'models' ? '活跃账号含停用账号；有调用即计入，期间内去重。' : '默认按总 Token 降序，可点击列头排序。'}</span>
        </div>
        {data.view === 'details' && <form className="usage-detail-filters" onSubmit={event => {
          event.preventDefault(); changeTable({ account_filter: accountFilter, model_filter: modelFilter });
        }}>
          <label className="admin-field">账号筛选<input className="admin-input" value={accountFilter} maxLength={100} placeholder="账号名称或标识" onChange={event => setAccountFilter(event.target.value)} /></label>
          <label className="admin-field">模型筛选<input className="admin-input" value={modelFilter} maxLength={100} placeholder="模型名称或标识" onChange={event => setModelFilter(event.target.value)} /></label>
          <button className="admin-button" disabled={loading}>筛选明细</button>
          <button className="admin-button" type="button" disabled={loading} onClick={() => {
            setAccountFilter(''); setModelFilter(''); changeTable({ account_filter: '', model_filter: '' });
          }}>清空筛选</button>
        </form>}
        {data.view === 'details' && (data.account_filter || data.model_filter) && <div className="usage-applied-filters">当前结果筛选：账号「{data.account_filter || '全部'}」、模型「{data.model_filter || '全部'}」。顶部总览不受明细筛选影响。</div>}
        <div className="admin-table-wrap usage-table-scroll" tabIndex={0} aria-label="使用统计表格，可横向滚动">
          <table className="admin-table usage-table">
            <caption className="usage-sr-only">{VIEWS.find(([key]) => key === data.view)?.[1]}，北京时间 {data.period.start_date} 至 {data.period.end_date}</caption>
            <thead><tr>{COLUMNS[data.view].map(([key, label]) => <th key={key} scope="col" aria-sort={data.sort_by === key ? (data.direction === 'asc' ? 'ascending' : 'descending') : 'none'}>
              <button className="usage-sort" disabled={loading} onClick={() => changeTable({ sort_by: key, direction: data.sort_by === key && data.direction === 'desc' ? 'asc' : 'desc' })}>
                {label}{data.sort_by === key && (data.direction === 'desc' ? <ArrowDown size={13} aria-hidden="true" /> : <ArrowUp size={13} aria-hidden="true" />)}
              </button>
            </th>)}</tr></thead>
            <tbody>{data.rows.map((row) => <tr key={`${row.user_id || ''}:${row.model_id || ''}`}>
              {COLUMNS[data.view].map(([key]) => <td key={key}>{cell(row, key)}</td>)}
            </tr>)}{!data.rows.length && <tr><td colSpan={COLUMNS[data.view].length} className="usage-empty">暂无数据{data.view === 'details' && (data.account_filter || data.model_filter) ? '，请调整筛选条件' : ''}</td></tr>}</tbody>
          </table>
        </div>
        <div className="usage-pagination">
          <span>共 {number.format(data.total)} 条 · 第 {data.page} / {Math.max(1, Math.ceil(data.total / data.page_size))} 页</span>
          <label>每页 <select className="admin-select" value={data.page_size} disabled={loading} onChange={event => changeTable({ page_size: Number(event.target.value) })}>
            {[20, 50, 100].map(size => <option value={size} key={size}>{size}</option>)}
          </select> 条</label>
          <button className="admin-button" disabled={loading || data.page <= 1} onClick={() => changeTable({ page: data.page - 1 })}>上一页</button>
          <button className="admin-button" disabled={loading || data.page * data.page_size >= data.total} onClick={() => changeTable({ page: data.page + 1 })}>下一页</button>
        </div>
      </div>
    </>}
  </section>;
}
