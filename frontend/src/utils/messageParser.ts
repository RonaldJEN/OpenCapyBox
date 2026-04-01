/**
 * 消息解析工具函数
 * 用于处理消息中的特殊标记，如文件附件
 */

export interface FileAttachmentInfo {
  filename: string;
  size: string;
}

export interface ParsedMessageContent {
  attachments: FileAttachmentInfo[];
  cleanContent: string;
}

/**
 * 解析消息内容，提取附件信息并移除附件标记
 *
 * @param content 原始消息内容
 * @returns 解析后的内容，包含附件列表和纯净的消息文本
 *
 * @example
 * ```typescript
 * const content = "[ATTACHMENT:report.pdf|2.5 MB]\n\n请分析这个文件";
 * const { attachments, cleanContent } = parseMessageContent(content);
 * // attachments: [{ filename: "report.pdf", size: "2.5 MB" }]
 * // cleanContent: "请分析这个文件"
 * ```
 */
export function parseMessageContent(content: string): ParsedMessageContent {
  const attachments: FileAttachmentInfo[] = [];

  // 匹配 [ATTACHMENT:filename|size] 格式
  // 支持文件名中包含路径、空格等字符
  const attachmentRegex = /\[ATTACHMENT:([^|]+)\|([^\]]+)\]/g;
  let match;

  // 提取所有附件信息
  while ((match = attachmentRegex.exec(content)) !== null) {
    attachments.push({
      filename: match[1].trim(),
      size: match[2].trim(),
    });
  }

  // 移除附件标记，保留纯净的消息内容
  // 同时移除附件标记后可能的多余空行
  const cleanContent = content
    .replace(attachmentRegex, '')
    .replace(/^\s+/, '') // 移除开头的空白
    .trim();

  return { attachments, cleanContent };
}
