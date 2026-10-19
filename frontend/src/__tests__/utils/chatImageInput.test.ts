import { describe, expect, it } from 'vitest';

import type { ModelInfo, VisionFallbackInfo } from '../../types';
import { getImageInputError, getImageInputNotice } from '../../utils/chatImageInput';

const textOnlyModel = {
  id: 'glm',
  name: '智谱 GLM-5',
  supports_image: false,
  max_images: 0,
} as ModelInfo;

const visionFallback: VisionFallbackInfo = { id: 'qwen', name: 'qwen3.6-plus', max_images: 1 };

const image = (name: string) => ({ name, type: 'image/png', source: 'session' as const });

describe('getImageInputError with vision fallback', () => {
  it('accepts images for a text-only model when fallback is configured', () => {
    expect(getImageInputError([image('a.png')], textOnlyModel, 'upload', visionFallback)).toBe('');
  });

  it('applies the fallback image limit', () => {
    expect(getImageInputError([image('a.png'), image('b.png')], textOnlyModel, 'send', visionFallback))
      .toBe('一次最多识别 1 张图片，请移除多余图片。');
  });
});

describe('getImageInputNotice', () => {
  it('explains that the fallback reads images for a text-only model', () => {
    expect(getImageInputNotice([image('a.png')], textOnlyModel, visionFallback))
      .toBe('当前模型不能直接看图，发送后会先自动识别图片内容。');
  });
});
