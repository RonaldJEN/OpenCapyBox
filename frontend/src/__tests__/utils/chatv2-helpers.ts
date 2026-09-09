/**
 * ChatV2 测试共享常量 — 消除 ChatV2.test.tsx 与 ChatV2.preview-wiring.test.tsx 之间的重复
 *
 * 注意：vi.mock() 必须在每个测试文件顶层调用（Vitest 会自动提升），
 *       无法提取到共享模块。只提取 props / data 等纯数据。
 */
import { vi } from 'vitest';

/** ChatV2 组件所需的默认 props */
export const makeChatV2DefaultProps = () => ({
  onTitleUpdated: vi.fn(),
  onExecutionStart: vi.fn(),
  onExecutionEnd: vi.fn(),
  catalogDefaultModelId: 'test-model',
  availableModels: [{ id: 'test-model', name: 'Test Model', provider: 'openai',
    supports_thinking: false, supports_image: false, max_images: 0,
    supports_video: false, max_videos: 0, max_tokens: 8192, enabled: true, tags: [] }] as any[],
  onCreateSession: vi.fn(),
});
