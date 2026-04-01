import { Bot, Cpu, Zap, Sparkles, Brain } from 'lucide-react';
import type { ModelInfo } from '../types';

/**
 * 根據模型名稱 / tag 選擇對應圖標（統一實現，避免多處重複）
 */
export function getModelIcon(model: ModelInfo, size = 14) {
  const name = model.name.toLowerCase();
  if (name.includes('deepseek')) return <Cpu size={size} />;
  if (name.includes('glm') || name.includes('智谱')) return <Zap size={size} />;
  if (name.includes('qwen') || name.includes('千问')) return <Sparkles size={size} />;
  if (name.includes('kimi')) return <Sparkles size={size} />;
  if (model.supports_thinking) return <Brain size={size} />;
  return <Bot size={size} />;
}
