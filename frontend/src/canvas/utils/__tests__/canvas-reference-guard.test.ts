// M3-T04 参考连线守卫单测：类型矩阵逐格 + 自连/重复/环 + 参考仓口径的违规返回形状
// + 第三批 utils（资源编号 / 生成入参）的直译行为。
// 环境：纯函数，无 store、无桥、不碰 DOM。
import { describe, expect, it } from 'vitest';

import { CanvasNodeType } from '../../types';
import type { CanvasConnection, CanvasNodeData } from '../../types';
import { buildNodeGenerationInputs } from '../canvas-generation-inputs';
import {
  CANVAS_REFERENCE_RULES,
  defaultDenyReason,
  referenceRoleFor,
  referenceRuleFor,
  validateCanvasConnection,
  validateConnection,
  willCreateCycle,
} from '../canvas-reference-guard';
import {
  buildCanvasResourceReferences,
  collectMentionedNodeIds,
} from '../canvas-resource-references';

function node(
  id: string,
  type: CanvasNodeType,
  metadata: CanvasNodeData['metadata'] = {},
): CanvasNodeData {
  return { id, type, title: id, position: { x: 0, y: 0 }, width: 100, height: 60, metadata };
}

function edge(id: string, fromNodeId: string, toNodeId: string): CanvasConnection {
  return { id, fromNodeId, toNodeId };
}

describe('类型兼容表：允许的格子', () => {
  it('image→image=改图参考、image→video=首帧、text→config=提示词、panorama→director=环境球', () => {
    const nodes = [
      node('img1', CanvasNodeType.Image),
      node('img2', CanvasNodeType.Image),
      node('vid1', CanvasNodeType.Video),
      node('txt1', CanvasNodeType.Text),
      node('cfg1', CanvasNodeType.Config),
      node('pano1', CanvasNodeType.Panorama),
      node('dir1', CanvasNodeType.Director),
    ];

    const allowed: Array<[string, string]> = [
      ['img1', 'img2'],
      ['img1', 'vid1'],
      ['txt1', 'cfg1'],
      ['txt1', 'vid1'],
      ['pano1', 'dir1'],
    ];
    for (const [fromNodeId, toNodeId] of allowed) {
      const check = validateConnection({ fromNodeId, toNodeId, nodes, connections: [] });
      expect(check.ok, `${fromNodeId}→${toNodeId} 应允许`).toBe(true);
      expect(check.reason).toBeUndefined();
    }

    // 语义（role）按表取：M4 提交时靠它分流参考图 / 首帧 / 提示词 / 环境球
    expect(referenceRoleFor('img1', 'img2', nodes)).toBe('edit-reference');
    expect(referenceRoleFor('img1', 'vid1', nodes)).toBe('first-frame');
    expect(referenceRoleFor('txt1', 'cfg1', nodes)).toBe('prompt');
    expect(referenceRoleFor('pano1', 'dir1', nodes)).toBe('panorama-environment');
  });

  it('矩阵外格子一律拒绝且给明确 reason（audio→video 有专门文案）', () => {
    const nodes = [
      node('img1', CanvasNodeType.Image),
      node('txt1', CanvasNodeType.Text),
      node('vid1', CanvasNodeType.Video),
      node('aud1', CanvasNodeType.Audio),
      node('cfg1', CanvasNodeType.Config),
      node('pano1', CanvasNodeType.Panorama),
      node('dir1', CanvasNodeType.Director),
      node('grp1', CanvasNodeType.Group),
    ];

    const denied: Array<[string, string]> = [
      ['img1', 'txt1'],
      ['img1', 'pano1'],
      ['img1', 'dir1'],
      ['img1', 'grp1'],
      ['img1', 'cfg1'],
      ['pano1', 'img1'],
      ['vid1', 'img1'],
      ['aud1', 'img1'],
      ['cfg1', 'img1'],
      ['dir1', 'img1'],
      ['grp1', 'img1'],
      ['txt1', 'grp1'],
    ];
    for (const [fromNodeId, toNodeId] of denied) {
      const check = validateConnection({ fromNodeId, toNodeId, nodes, connections: [] });
      expect(check.ok, `${fromNodeId}→${toNodeId} 应拒绝`).toBe(false);
      expect(check.reason, `${fromNodeId}→${toNodeId} 必须有 reason`).toBeTruthy();
    }

    // 音频参考有专门文案：不是笼统的「暂不支持」
    expect(validateConnection({ fromNodeId: 'aud1', toNodeId: 'vid1', nodes, connections: [] })).toEqual({
      ok: false,
      reason: '音频参考暂未接入',
    });
    // 表外组合的兜底文案说人话；表是白名单
    expect(defaultDenyReason(CanvasNodeType.Image, CanvasNodeType.Text)).toBe('图片暂不支持连到文本');
    expect(referenceRuleFor(CanvasNodeType.Image, CanvasNodeType.Text)).toBeUndefined();
    expect(defaultDenyReason(CanvasNodeType.Group, CanvasNodeType.Image)).toBe('分组节点不能作为连线起点');
    expect(defaultDenyReason(CanvasNodeType.Image, CanvasNodeType.Group)).toBe('分组节点不接收连线');
    expect(Object.keys(CANVAS_REFERENCE_RULES).sort()).toEqual(
      ['audio', 'config', 'director', 'group', 'image', 'panorama', 'text', 'video'].sort(),
    );
  });

  it('未知节点类型（脏数据）拒绝，并点名是源还是目标不支持', () => {
    const nodes = [node('weird', 'cloud-asset' as CanvasNodeType), node('img1', CanvasNodeType.Image)];
    expect(validateConnection({ fromNodeId: 'weird', toNodeId: 'img1', nodes, connections: [] })).toEqual({
      ok: false,
      reason: '不支持的源节点类型：cloud-asset',
    });
    expect(validateConnection({ fromNodeId: 'img1', toNodeId: 'weird', nodes, connections: [] })).toEqual({
      ok: false,
      reason: '不支持的目标节点类型：cloud-asset',
    });
  });
});

describe('自连 / 重复 / 环', () => {
  const nodes = [
    node('a', CanvasNodeType.Image),
    node('b', CanvasNodeType.Image),
    node('c', CanvasNodeType.Image),
    node('v', CanvasNodeType.Video),
  ];

  it('自连与空 id/缺节点都拒绝（文案直白，不留死胡同）', () => {
    expect(validateConnection({ fromNodeId: 'a', toNodeId: 'a', nodes, connections: [] })).toEqual({
      ok: false,
      reason: '不能把节点连到自己',
    });
    expect(validateConnection({ fromNodeId: '', toNodeId: 'a', nodes, connections: [] }).reason).toBe(
      '连线缺少起点或终点',
    );
    expect(validateConnection({ fromNodeId: 'a', toNodeId: 'missing', nodes, connections: [] }).reason).toBe(
      '目标节点不存在',
    );
  });

  it('重复连线拒绝：同一对 from→to 只允许一条', () => {
    const connections = [edge('c_1', 'a', 'b')];
    expect(validateConnection({ fromNodeId: 'a', toNodeId: 'b', nodes, connections })).toEqual({
      ok: false,
      reason: '这两个节点已经连线了',
    });
    // 反向（b→a）不是「重复」，但会闭合成环 → 拒绝理由是环而不是重复
    expect(validateConnection({ fromNodeId: 'b', toNodeId: 'a', nodes, connections }).reason).toBe(
      '不能连成环：目标节点已经在当前节点的上游',
    );
  });

  it('环检测：a→b→c 之后 c→a 被拒；汇聚与平行边不算环', () => {
    const connections = [edge('c_1', 'a', 'b'), edge('c_2', 'b', 'c')];
    expect(validateConnection({ fromNodeId: 'c', toNodeId: 'a', nodes, connections })).toEqual({
      ok: false,
      reason: '不能连成环：目标节点已经在当前节点的上游',
    });
    // 与图上无关的两点不成环
    expect(willCreateCycle('a', 'v', connections)).toBe(false);
    expect(validateConnection({ fromNodeId: 'a', toNodeId: 'v', nodes, connections }).ok).toBe(true);
    // a→c 是汇聚（已有 a→b→c），不构成环
    expect(validateConnection({ fromNodeId: 'a', toNodeId: 'c', nodes, connections }).ok).toBe(true);
  });
});

describe('参考仓口径的违规返回（红框 + blameNotes）', () => {
  it('连线被拒时返回 message/blameNodeIds/blameNotes，不返回待断连线', () => {
    const nodes = [node('a', CanvasNodeType.Audio), node('v', CanvasNodeType.Video)];
    const violation = validateCanvasConnection({ fromNodeId: 'a', toNodeId: 'v', nodes, connections: [] });
    expect(violation).not.toBeNull();
    expect(violation!.message).toContain('连线被拒绝');
    expect(violation!.message).toContain('音频参考暂未接入');
    expect(violation!.blameNodeIds).toEqual(['a', 'v']);
    expect(violation!.blameNotes).toEqual({ a: '音频参考暂未接入', v: '音频参考暂未接入' });
    expect(violation!.disconnectConnectionIds).toEqual([]);
    // 反过来（video→audio）也是拒绝（视频参考暂未接入），合法组合才返回 null
    expect(validateCanvasConnection({ fromNodeId: 'v', toNodeId: 'a', nodes, connections: [] })).not.toBeNull();
    expect(
      validateCanvasConnection({
        fromNodeId: 'a',
        toNodeId: 'a',
        nodes,
        connections: [],
      }),
    ).not.toBeNull();
  });
});

describe('资源编号与生成入参（第三批 utils 直译）', () => {
  it('buildCanvasResourceReferences：按节点顺序给图片/文本编号，空节点不进表', () => {
    const nodes = [
      node('img-a', CanvasNodeType.Image, { content: '/media/a.png' }),
      node('txt-a', CanvasNodeType.Text, { content: '提示词' }),
      node('img-b', CanvasNodeType.Image, { content: '/media/b.png' }),
      node('img-empty', CanvasNodeType.Image, {}),
    ];
    const references = buildCanvasResourceReferences(nodes, []);
    expect(references.map((reference) => reference.label)).toEqual(['图片1', '文本1', '图片2']);
    expect(references.map((reference) => reference.nodeId)).toEqual(['img-a', 'txt-a', 'img-b']);
    // 标签 → 节点：「图片1」不会误匹配「图片10」
    expect([...collectMentionedNodeIds('参考图片2 与 图片10', references)]).toEqual(['img-b']);
  });

  it('buildNodeGenerationInputs：只认入线，图片入参带 storageKey 与「标题-id.png」文件名', () => {
    const nodes = [
      node('img-a', CanvasNodeType.Image, { content: '/media/a.png', storageKey: 'k/a.png' }),
      node('txt-a', CanvasNodeType.Text, { content: '一只猫' }),
      node('vid-target', CanvasNodeType.Video),
    ];
    const connections = [edge('c_1', 'txt-a', 'vid-target'), edge('c_2', 'img-a', 'vid-target')];
    const inputs = buildNodeGenerationInputs('vid-target', nodes, connections);
    expect(inputs.map((input) => input.type)).toEqual(['text', 'image']);
    expect(inputs[0].text).toBe('一只猫');
    expect(inputs[1].image?.storageKey).toBe('k/a.png');
    expect(inputs[1].image?.name).toBe('img-a-img-a.png');
    // 反向（出线）不算参考：没有入线的节点产不出入参
    expect(buildNodeGenerationInputs('txt-a', nodes, connections)).toEqual([]);
  });
});
