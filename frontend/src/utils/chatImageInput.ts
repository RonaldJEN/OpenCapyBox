import type { ChatFile, ModelInfo, VisionFallbackInfo } from '../types';
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
  visionFallback: VisionFallbackInfo | null = null,
): string {
  const imageCount = files.filter(isConversationImageInput).length;
  if (!imageCount) return '';
  if (!model) return '正在加载模型能力，请稍后再添加或发送图片。';
  if (!model.supports_image && visionFallback) {
    return imageCount > visionFallback.max_images
      ? `一次最多识别 ${visionFallback.max_images} 张图片，请移除多余图片。`
      : '';
  }
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

// 主模型不支持图片、由视觉挂件代读时，提示用户图片会先被识别再交给模型。
export function getImageInputNotice(
  files: readonly ImageInputFile[],
  model: ModelInfo | undefined,
  visionFallback: VisionFallbackInfo | null,
): string {
  if (!model || model.supports_image || !visionFallback) return '';
  if (!files.some(isConversationImageInput)) return '';
  return '当前模型不能直接看图，发送后会先自动识别图片内容。';
}
