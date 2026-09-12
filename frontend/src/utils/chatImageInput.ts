import type { ChatFile, ModelInfo } from '../types';
import { isImageFile } from './fileUtils';

type ImageInputFile = Pick<ChatFile, 'name' | 'type' | 'source' | 'is_directory'>;

// Workspace files are tool inputs; only conversation images consume vision capacity.
export function isConversationImageInput(file: ImageInputFile): boolean {
  return file.source !== 'workspace' && isImageFile(file);
}

export function getImageInputError(
  files: readonly ImageInputFile[],
  model: ModelInfo | undefined,
  action: 'upload' | 'send' = 'send',
): string {
  const imageCount = files.filter(isConversationImageInput).length;
  if (!imageCount) return '';
  if (!model) return '正在加载模型能力，请稍后再添加或发送图片。';
  if (!model.supports_image) {
    return action === 'upload'
      ? '当前模型不支持图片输入，请切换至支持图片的模型后再上传。'
      : '当前模型不支持图片输入，请切换至支持图片的模型或移除图片。';
  }
  if (imageCount > model.max_images) {
    return `当前模型最多支持 ${model.max_images} 张图片，请移除多余图片或切换模型。`;
  }
  return '';
}
