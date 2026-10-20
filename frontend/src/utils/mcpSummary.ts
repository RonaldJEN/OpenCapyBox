// 能力摘要在发现提交后由后台生成：单次预算 20 秒，运行中再来的发现会再跑一次，
// 并发上限 2 时导入多条会排队。测试、启用或导入成功后按这几个延迟重新拉取列表，
// 让新摘要（含改名后的重算）自动出现在卡片上；更长的排队由用户手动刷新兜底。
export const SUMMARY_REFRESH_DELAYS_MS = [5000, 12000, 25000, 45000];

export function scheduleSummaryRefresh(
  timers: { current: number[] },
  refresh: () => void,
): void {
  timers.current.forEach((timer) => window.clearTimeout(timer));
  timers.current = SUMMARY_REFRESH_DELAYS_MS.map((delay) => window.setTimeout(refresh, delay));
}

export function clearSummaryRefresh(timers: { current: number[] }): void {
  timers.current.forEach((timer) => window.clearTimeout(timer));
  timers.current = [];
}
