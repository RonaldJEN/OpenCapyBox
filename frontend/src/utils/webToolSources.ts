export interface WebToolSource {
  url: string;
  domain: string;
  title: string;
  icon?: string;
}

// These tools own the Query / [n] / URL / Source / Content result format.
// Memory search and tool discovery must never be presented as web browsing.
export function isWebSearchTool(name: string): boolean {
  return name === 'glm_search' || name === 'glm_batch_search';
}

export function safeWebUrl(value: string): string | undefined {
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : undefined;
  } catch { return undefined; }
}

export function getWebToolSources(content: string): WebToolSource[] {
  const sources = new Map<string, WebToolSource>();
  // Only the formatter's result header is parsed, never URLs inside snippets.
  const records = content.matchAll(/^\[\d+\] ([^\r\n]*)\r?\nURL: ([^\r\n]+)\r?\nSource: [^\r\n]*(?:\r?\nIcon: ([^\r\n]+))?/gm);
  for (const record of records) {
    const url = safeWebUrl(record[2].trim());
    if (!url) continue;
    const domain = new URL(url).hostname;
    if (!sources.has(domain)) sources.set(domain, {
      url, domain, title: record[1].trim() || domain,
      icon: record[3] ? safeWebUrl(record[3].trim()) : undefined,
    });
  }
  return [...sources.values()];
}
