import { describe, expect, it } from 'vitest';

import {
  PANORAMA_IMAGE_SIZE,
  PANORAMA_NODE_SIZE,
  buildPanoramaPrompt,
  isCanvasImageNodeType,
  isPanoramaNodeType,
} from '../canvas-panorama';
import { CanvasNodeType } from '../../types';

describe('canvas-panorama', () => {
  it('全景尺寸常量：出图比例 2:1、节点默认 340x170', () => {
    expect(PANORAMA_IMAGE_SIZE).toBe('2:1');
    expect(PANORAMA_NODE_SIZE).toEqual({ width: 340, height: 170 });
  });

  it('isCanvasImageNodeType：image/panorama 为真，其余节点类型与空值均为假', () => {
    const table: Array<[CanvasNodeType | null | undefined, boolean]> = [
      [CanvasNodeType.Image, true],
      [CanvasNodeType.Panorama, true],
      [CanvasNodeType.Video, false],
      [CanvasNodeType.Audio, false],
      [CanvasNodeType.Text, false],
      [CanvasNodeType.Config, false],
      [CanvasNodeType.Director, false],
      [CanvasNodeType.Group, false],
      [null, false],
      [undefined, false],
    ];

    for (const [type, expected] of table) {
      expect(isCanvasImageNodeType(type), String(type)).toBe(expected);
    }
  });

  it('isPanoramaNodeType：只有 panorama 为真', () => {
    expect(isPanoramaNodeType(CanvasNodeType.Panorama)).toBe(true);
    expect(isPanoramaNodeType(CanvasNodeType.Image)).toBe(false);
    expect(isPanoramaNodeType(null)).toBe(false);
  });

  it('buildPanoramaPrompt：有参考图走「图生」模板并注入用户提示词', () => {
    const prompt = buildPanoramaPrompt('  画一座雪山湖泊  ', true);

    expect(prompt.startsWith('这是图生720度球形全景任务')).toBe(true);
    expect(prompt).toContain('画一座雪山湖泊');
    // 参考图分支要求保留场景主体、补齐四周环境
    expect(prompt).toContain('保留它的场景主体');
    expect(prompt).not.toContain('这是文字生成');
    // 用户提示词已在模板尾部，且被 trim
    expect(prompt.endsWith(', 画一座雪山湖泊')).toBe(true);
  });

  it('buildPanoramaPrompt：无参考图走「文生」模板，共同约束替换完整', () => {
    const prompt = buildPanoramaPrompt('一座海边灯塔', false);

    expect(prompt.startsWith('这是文字生成720度球形全景任务')).toBe(true);
    expect(prompt).toContain('一座海边灯塔');
    expect(prompt).not.toContain('这是图生');
    // 占位符全部替换干净，等距柱状投影与 2 比 1 约束进入输出
    expect(prompt).not.toContain('{{');
    expect(prompt).toContain('等距柱状投影的完整球形全景图，比例2比1');
    expect(prompt).toContain('不要水印，不要文字，不要界面元素');
  });
});
