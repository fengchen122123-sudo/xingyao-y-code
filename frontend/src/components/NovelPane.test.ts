import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';

import NovelPane from './NovelPane.vue';
import { useAppStore } from '../stores/appStore';

// 后端契约字段（core/novel_service.novel_status）：只给章数不给逐章明细，
// 前端按章号兜底列章节、把 next_chapter 标成「待写」
const WORKDIR = 'D:/demo/长夜书';

function statusPayload(overrides: Record<string, unknown> = {}) {
  return {
    is_novel: true,
    title: '长夜书',
    chapters_written: 3,
    total_words: 12345,
    today_words: 800,
    next_chapter: 4,
    outline_ready: true,
    active_foreshadows: 2,
    last_commit: 3,
    ...overrides,
  };
}

// get_novel_workbench 契约快照（core.novel_service.novel_workbench）：伏笔 / 角色 /
// 卷纲三张设定表 + 近 7 天字数，四张表都是可选字段，缺省即空数组
function workbenchPayload(overrides: Record<string, unknown> = {}) {
  return {
    is_novel: true,
    foreshadows: [
      {
        id: 'F001',
        summary: '断剑的来历',
        status: '已埋',
        importance: '高',
        planted_chapter: 1,
        planned_resolution_chapter: 20,
        overdue: false,
        planned_overdue: false,
        stale_chapters: 2,
      },
    ],
    characters: [
      {
        name: '沈砚',
        identity: '主角',
        state: '重伤未愈',
        location: '洛阳',
        goal: '查清断剑来历',
        knowledge: ['断剑上的纹路'],
        abilities_resources: [],
        open_threads: ['断剑来历未回收'],
        relationships: [],
      },
    ],
    volumes: [{ no: 1, name: '卷纲_第001卷', chars: 1200, current: true }],
    recent_daily: [
      { date: '2026-10-20', words: 800 },
      { date: '2026-10-21', words: 0 },
      { date: '2026-10-22', words: 1200 },
    ],
    ...overrides,
  };
}

// 挂载面板：预置工作目录 + 桥接 mock，active=true 表示页签已激活。
// P3-C 新增的三个只读方法给「空数据」默认值：老用例不关心它们，
// 需要具体数据的用例用 extraApi 覆盖成自己的 vi.fn
function mountPane(impl: (...args: any[]) => any, extraApi: Record<string, any> = {}) {
  const getNovelStatus = vi.fn(impl);
  (window as any).pywebview = {
    api: {
      get_novel_status: getNovelStatus,
      get_novel_workbench: vi.fn(async () => ({
        is_novel: true,
        foreshadows: [],
        characters: [],
        volumes: [],
        recent_daily: [],
      })),
      run_novel_audit: vi.fn(async () => ({
        is_novel: true,
        ok: true,
        scope: 'chapter',
        summary: {},
        findings: [],
        skipped: [],
      })),
      read_novel_doc: vi.fn(async () => ({ ok: true, text: '', chars: 0 })),
      ...extraApi,
    },
  };
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.workdir = WORKDIR;
  const wrapper = mount(NovelPane, {
    props: { active: true },
    global: { plugins: [pinia] },
  });
  return { wrapper, getNovelStatus };
}

beforeEach(() => {
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

it('正常态：渲染书名、字数与章节列表（末章标「待写」）', async () => {
  const { wrapper, getNovelStatus } = mountPane(async () => statusPayload());

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-title"]').exists()).toBe(true);
  });

  expect(getNovelStatus).toHaveBeenCalledWith(WORKDIR);
  expect(wrapper.get('[data-test="novel-title"]').text()).toBe('长夜书');
  // 总字数上「万」口径，今日字数单独一块
  expect(wrapper.text()).toContain('1.23 万');
  expect(wrapper.text()).toContain('今日');
  expect(wrapper.text()).toContain('800');

  const chapters = wrapper.findAll('[data-test="novel-chapter"]');
  // 3 章已写 + next_chapter(4) 待写占位
  expect(chapters).toHaveLength(4);
  expect(chapters[0].text()).toContain('第 001 章');
  expect(chapters[2].text()).toContain('第 003 章');
  expect(chapters[3].classes()).toContain('pending');
  expect(chapters[3].text()).toContain('待写');
  // 后端没有逐章明细时给出说明，不假装字数已知
  expect(wrapper.find('[data-test="novel-chapter-note"]').exists()).toBe(true);

  // 状态卡：活跃伏笔 / 最近提交章号 / 下一章细纲
  const status = wrapper.get('[data-test="novel-status"]').text();
  expect(status).toContain('活跃伏笔');
  expect(status).toContain('2');
  expect(status).toContain('第 3 章');
  expect(status).toContain('已就绪');
  wrapper.unmount();
});

it('正常态：后端给出逐章明细时渲染章名与字数', async () => {
  const { wrapper } = mountPane(async () => statusPayload({
    chapters: [
      { number: 1, name: '第001章 夜雨', words: 3120 },
      { number: 2, name: '第002章 旧约', words: 2870 },
    ],
    chapters_written: 2,
    next_chapter: 3,
  }));

  await vi.waitFor(() => {
    expect(wrapper.findAll('[data-test="novel-chapter"]')).toHaveLength(3);
  });
  const rows = wrapper.findAll('[data-test="novel-chapter"]');
  expect(rows[0].text()).toContain('第001章 夜雨');
  expect(rows[0].text()).toContain('3120 字');
  expect(rows[2].text()).toContain('待写');
  expect(wrapper.find('[data-test="novel-chapter-note"]').exists()).toBe(false);
  wrapper.unmount();
});

it('空态：非小说项目显示「初始化小说项目」引导', async () => {
  const { wrapper } = mountPane(async () => ({ is_novel: false }));

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-empty"]').exists()).toBe(true);
  });
  const empty = wrapper.get('[data-test="novel-empty"]');
  expect(empty.text()).toContain('初始化小说项目');
  // 引导只预填输入框，不直接写文件
  expect(wrapper.find('[data-test="novel-init"]').exists()).toBe(true);
  wrapper.unmount();
});

it('错误态：渲染后端错误文本且不崩溃', async () => {
  const { wrapper } = mountPane(async () => ({ is_novel: false, error: '追踪状态文件损坏，请运行 story-import 重建' }));

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-error"]').exists()).toBe(true);
  });
  expect(wrapper.get('[data-test="novel-error"]').text()).toContain('追踪状态文件损坏');
  expect(wrapper.find('[data-test="novel-empty"]').exists()).toBe(false);
  // 重试按钮存在（失败后可手动再拉一次）
  expect(wrapper.find('[data-test="novel-retry"]').exists()).toBe(true);
  wrapper.unmount();
});

it('桥接异常：抛错时也落错误态而不是白屏', async () => {
  const { wrapper } = mountPane(async () => {
    throw new Error('桥接调用超时');
  });

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-error"]').exists()).toBe(true);
  });
  expect(wrapper.text()).toContain('桥接调用超时');
  wrapper.unmount();
});

it('只在激活时拉一次，不做轮询', async () => {
  const { wrapper, getNovelStatus } = mountPane(async () => statusPayload());

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-title"]').exists()).toBe(true);
  });
  // 等过一轮事件循环，确认没有定时轮询追加调用
  await new Promise((resolve) => setTimeout(resolve, 120));
  expect(getNovelStatus).toHaveBeenCalledTimes(1);
  wrapper.unmount();
});

// ---- 章节编辑器（P3-A）：点章节读正文、编辑后保存 ----

it('点击章节：拉取正文并展开编辑器', async () => {
  const getNovelChapter = vi.fn(async () => ({
    ok: true,
    chapter: 1,
    path: '正文/第001章_夜行.md',
    content: '# 第001章 夜行\n\n他推门而入。\n',
    visible_chars: 12,
  }));
  const { wrapper } = mountPane(async () => statusPayload(), { get_novel_chapter: getNovelChapter });

  await vi.waitFor(() => {
    expect(wrapper.findAll('[data-test="novel-chapter"]')).toHaveLength(4);
  });
  // 未点击前没有编辑器
  expect(wrapper.find('[data-test="novel-editor"]').exists()).toBe(false);

  await wrapper.findAll('[data-test="novel-chapter"]')[0].trigger('click');

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-editor"]').exists()).toBe(true);
  });
  expect(getNovelChapter).toHaveBeenCalledWith(WORKDIR, 1);
  const editor = wrapper.get('[data-test="novel-editor"]').element as HTMLTextAreaElement;
  expect(editor.value).toContain('他推门而入');
  // 已写入的字数来自后端 visible_chars
  expect(wrapper.text()).toContain('12 字');
  expect(wrapper.find('[data-test="novel-save"]').exists()).toBe(true);

  // 「待写」章节不可点开（不发请求）
  await wrapper.findAll('[data-test="novel-chapter"]')[3].trigger('click');
  expect(getNovelChapter).toHaveBeenCalledTimes(1);
  wrapper.unmount();
});

it('编辑后保存：回传字数更新该章并给出「已保存」提示', async () => {
  const getNovelChapter = vi.fn(async () => ({
    ok: true,
    chapter: 1,
    path: '正文/第001章_夜行.md',
    content: '# 第001章 夜行\n\n他推门而入。\n',
    visible_chars: 12,
  }));
  const saveNovelChapter = vi.fn(async () => ({
    ok: true,
    chapter: 1,
    path: '正文/第001章_夜行.md',
    visible_chars: 3210,
  }));
  const { wrapper, getNovelStatus } = mountPane(async () => statusPayload(), {
    get_novel_chapter: getNovelChapter,
    save_novel_chapter: saveNovelChapter,
  });

  await vi.waitFor(() => {
    expect(wrapper.findAll('[data-test="novel-chapter"]')).toHaveLength(4);
  });
  await wrapper.findAll('[data-test="novel-chapter"]')[0].trigger('click');
  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-editor"]').exists()).toBe(true);
  });

  const edited = '# 第001章 夜行\n\n他推门而入，风灌进来，带起一室尘埃。\n';
  await wrapper.get('[data-test="novel-editor"]').setValue(edited);
  await wrapper.get('[data-test="novel-save"]').trigger('click');

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-save-hint"]').exists()).toBe(true);
  });
  expect(saveNovelChapter).toHaveBeenCalledWith(WORKDIR, 1, edited);
  expect(wrapper.get('[data-test="novel-save-hint"]').text()).toContain('已保存');
  // 该章字数用后端回传值更新；保存后复用 load() 重拉整体状态
  expect(wrapper.get('[data-test="novel-chapter"]').text()).toContain('3210 字');
  await vi.waitFor(() => {
    expect(getNovelStatus).toHaveBeenCalledTimes(2);
  });
  expect(wrapper.find('[data-test="novel-save-error"]').exists()).toBe(false);
  wrapper.unmount();
});

// ---- 多书切换与导出全书（P3-B） ----

it('多书列表：渲染书切换下拉，切书后按新目录重拉快照', async () => {
  const WORKDIR_B = `${WORKDIR}/book-b`;
  const listNovelProjects = vi.fn(async () => ({
    ok: true,
    projects: [
      { path: '.', title: '长夜书', chapters_written: 3 },
      { path: 'book-b', title: '归墟', chapters_written: 5 },
    ],
  }));
  // 按目录返回不同快照，验证切书后标题确实换成了新书的
  const { wrapper, getNovelStatus } = mountPane(
    async (workdir: string) => (workdir === WORKDIR_B
      ? statusPayload({ title: '归墟', chapters_written: 5, next_chapter: 6 })
      : statusPayload()),
    { list_novel_projects: listNovelProjects },
  );

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-book-select"]').exists()).toBe(true);
  });
  expect(listNovelProjects).toHaveBeenCalledWith(WORKDIR);

  const select = wrapper.get('[data-test="novel-book-select"]');
  const options = select.findAll('option');
  expect(options).toHaveLength(2);
  // 选项 value 是绝对路径：'.' 就是工作区自身，其余拼成 工作区/path
  expect((options[0].element as HTMLOptionElement).value).toBe(WORKDIR);
  expect((options[1].element as HTMLOptionElement).value).toBe(WORKDIR_B);
  // 选项文案形如「书名（已写 N 章）」
  expect(options[0].text()).toContain('长夜书（已写 3 章）');
  expect(options[1].text()).toContain('归墟（已写 5 章）');

  await select.setValue(WORKDIR_B);

  // 切书后按新书绝对路径重拉一次状态
  await vi.waitFor(() => {
    expect(getNovelStatus).toHaveBeenCalledWith(WORKDIR_B);
  });
  await vi.waitFor(() => {
    expect(wrapper.get('[data-test="novel-title"]').text()).toBe('归墟');
  });
  wrapper.unmount();
});

it('只有一本小说时：不渲染书切换下拉', async () => {
  const listNovelProjects = vi.fn(async () => ({
    ok: true,
    projects: [{ path: '.', title: '长夜书', chapters_written: 3 }],
  }));
  const { wrapper } = mountPane(async () => statusPayload(), { list_novel_projects: listNovelProjects });

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-title"]').exists()).toBe(true);
  });
  // 等列表请求的 then 回调落定，再确认下拉确实没有渲染
  await vi.waitFor(() => {
    expect(listNovelProjects).toHaveBeenCalledTimes(1);
  });
  await new Promise((resolve) => setTimeout(resolve, 30));
  expect(wrapper.find('[data-test="novel-book-select"]').exists()).toBe(false);
  wrapper.unmount();
});

it('导出 MD：调 export_novel(workdir, "md") 并展示「已导出」与文件路径', async () => {
  const exportNovel = vi.fn(async () => ({ ok: true, path: '导出/长夜行.md', chapters: 2 }));
  const { wrapper } = mountPane(async () => statusPayload(), { export_novel: exportNovel });

  // 导出按钮只在 ready 态出现
  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-export-md"]').exists()).toBe(true);
  });
  expect(wrapper.find('[data-test="novel-export-txt"]').exists()).toBe(true);
  await wrapper.get('[data-test="novel-export-md"]').trigger('click');

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-export-hint"]').exists()).toBe(true);
  });
  expect(exportNovel).toHaveBeenCalledWith(WORKDIR, 'md');
  const hint = wrapper.get('[data-test="novel-export-hint"]').text();
  expect(hint).toContain('已导出');
  expect(hint).toContain('导出/长夜行.md');
  expect(hint).toContain('2 章');
  expect(wrapper.find('[data-test="novel-export-error"]').exists()).toBe(false);
  wrapper.unmount();
});

it('导出失败：展示后端错误文案且不出成功提示', async () => {
  const exportNovel = vi.fn(async () => ({ ok: false, error: '正文目录没有可导出的章节' }));
  const { wrapper } = mountPane(async () => statusPayload(), { export_novel: exportNovel });

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-export-txt"]').exists()).toBe(true);
  });
  await wrapper.get('[data-test="novel-export-txt"]').trigger('click');

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-export-error"]').exists()).toBe(true);
  });
  expect(exportNovel).toHaveBeenCalledWith(WORKDIR, 'txt');
  expect(wrapper.get('[data-test="novel-export-error"]').text()).toContain('正文目录没有可导出的章节');
  expect(wrapper.find('[data-test="novel-export-hint"]').exists()).toBe(false);
  wrapper.unmount();
});

// ---- 对话回合结束后的按需刷新（不做轮询） ----

it('回合结束：页签可见时按需重拉一次状态快照', async () => {
  const { wrapper, getNovelStatus } = mountPane(async () => statusPayload());
  const store = useAppStore();

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-title"]').exists()).toBe(true);
  });
  expect(getNovelStatus).toHaveBeenCalledTimes(1);

  // 回合开始（false→true）不刷新：只在「结束」这一刻取数
  store.running = true;
  await wrapper.vm.$nextTick();
  expect(getNovelStatus).toHaveBeenCalledTimes(1);

  // 回合结束（true→false）：重拉一次，让对话里写出的章节立刻反映到面板
  store.running = false;
  await vi.waitFor(() => {
    expect(getNovelStatus).toHaveBeenCalledTimes(2);
  });
  wrapper.unmount();
});

it('回合结束：同时重扫多书列表，新建的书能出现在下拉里', async () => {
  const listNovelProjects = vi.fn(async () => ({
    ok: true,
    projects: [{ path: '.', title: '长夜书', chapters_written: 3 }],
  }));
  const { wrapper } = mountPane(async () => statusPayload(), { list_novel_projects: listNovelProjects });
  const store = useAppStore();

  await vi.waitFor(() => {
    expect(listNovelProjects).toHaveBeenCalledTimes(1);
  });

  store.running = true;
  await wrapper.vm.$nextTick();
  store.running = false;

  await vi.waitFor(() => {
    expect(listNovelProjects).toHaveBeenCalledTimes(2);
  });
  wrapper.unmount();
});

it('回合结束：页签未激活时不刷新（省掉无意义的只读扫描）', async () => {
  const { wrapper, getNovelStatus } = mountPane(async () => statusPayload());
  const store = useAppStore();

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-title"]').exists()).toBe(true);
  });
  expect(getNovelStatus).toHaveBeenCalledTimes(1);

  await wrapper.setProps({ active: false });
  store.running = true;
  await wrapper.vm.$nextTick();
  store.running = false;
  await wrapper.vm.$nextTick();
  await new Promise((resolve) => setTimeout(resolve, 60));

  expect(getNovelStatus).toHaveBeenCalledTimes(1);
  wrapper.unmount();
});

// ---- 设定库 / 近 7 天 / 一致性审查 / 卷纲预览（P3-C） ----

it('设定库：伏笔/角色/卷纲按 tab 切换，tab 文案带数量', async () => {
  const getWorkbench = vi.fn(async () => workbenchPayload());
  const { wrapper } = mountPane(async () => statusPayload(), { get_novel_workbench: getWorkbench });

  // 默认停在「伏笔」：一次拉取后直接渲染伏笔行
  await vi.waitFor(() => {
    expect(wrapper.findAll('[data-test="novel-foreshadow-row"]')).toHaveLength(1);
  });
  expect(getWorkbench).toHaveBeenCalledWith(WORKDIR);

  const tabs = wrapper.findAll('[data-test="novel-setting-tab"]');
  expect(tabs).toHaveLength(3);
  expect(tabs[0].text()).toBe('伏笔 1');
  expect(tabs[1].text()).toBe('角色 1');
  expect(tabs[2].text()).toBe('卷纲 1');
  // 当前 tab 用 active 样式高亮
  expect(tabs[0].classes()).toContain('active');
  expect(tabs[1].classes()).not.toContain('active');

  // 切到「角色」：换渲染角色卡，伏笔行消失（纯前端切换，不再请求后端）
  await tabs[1].trigger('click');
  expect(wrapper.findAll('[data-test="novel-character-card"]')).toHaveLength(1);
  expect(wrapper.get('[data-test="novel-character-card"]').text()).toContain('沈砚');
  expect(wrapper.find('[data-test="novel-foreshadow-row"]').exists()).toBe(false);
  expect(getWorkbench).toHaveBeenCalledTimes(1);

  // 切到「卷纲」
  await wrapper.findAll('[data-test="novel-setting-tab"]')[2].trigger('click');
  expect(wrapper.findAll('[data-test="novel-volume-row"]')).toHaveLength(1);
  expect(wrapper.get('[data-test="novel-volume-row"]').text()).toContain('卷纲_第001卷');
  expect(wrapper.find('[data-test="novel-character-card"]').exists()).toBe(false);
  wrapper.unmount();
});

it('近 7 天：recent_daily 渲染柱状图并显示合计字数', async () => {
  const { wrapper } = mountPane(async () => statusPayload(), {
    get_novel_workbench: vi.fn(async () => workbenchPayload()),
  });

  await vi.waitFor(() => {
    expect(wrapper.findAll('[data-test="novel-daily-bar"]')).toHaveLength(3);
  });
  // 分区标题与合计字数（800 + 0 + 1200）
  expect(wrapper.text()).toContain('近 7 天');
  expect(wrapper.text()).toContain('合计 2000 字');
  wrapper.unmount();
});

it('一致性审查：点按钮调 run_novel_audit(workdir,"chapter") 并渲染汇总与结论', async () => {
  const runNovelAudit = vi.fn(async () => ({
    is_novel: true,
    ok: true,
    scope: 'chapter',
    summary: { S1: 1, S2: 0, S3: 0, S4: 0 },
    findings: [
      {
        level: 'S1',
        type: 'tracking_state',
        file: '追踪/追踪表.md',
        message: '第 3 章状态与追踪表不一致',
      },
    ],
    skipped: [],
  }));
  const { wrapper } = mountPane(async () => statusPayload(), { run_novel_audit: runNovelAudit });

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-audit-run"]').exists()).toBe(true);
  });
  await wrapper.get('[data-test="novel-audit-run"]').trigger('click');

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-audit-summary"]').exists()).toBe(true);
  });
  // 审最新已写章节：scope='chapter'，章号不传
  expect(runNovelAudit).toHaveBeenCalledWith(WORKDIR, 'chapter');
  const findings = wrapper.findAll('[data-test="novel-audit-finding"]');
  expect(findings).toHaveLength(1);
  expect(findings[0].text()).toContain('tracking_state');
  expect(findings[0].text()).toContain('第 3 章状态与追踪表不一致');
  expect(wrapper.find('[data-test="novel-audit-error"]').exists()).toBe(false);
  wrapper.unmount();
});

it('一致性审查：后端回 ok=false 时显示错误文案', async () => {
  const runNovelAudit = vi.fn(async () => ({ ok: false, is_novel: true, error: '追踪表损坏，无法体检' }));
  const { wrapper } = mountPane(async () => statusPayload(), { run_novel_audit: runNovelAudit });

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-audit-run"]').exists()).toBe(true);
  });
  await wrapper.get('[data-test="novel-audit-run"]').trigger('click');

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-audit-error"]').exists()).toBe(true);
  });
  expect(wrapper.get('[data-test="novel-audit-error"]').text()).toContain('追踪表损坏，无法体检');
  expect(wrapper.find('[data-test="novel-audit-summary"]').exists()).toBe(false);
  wrapper.unmount();
});

it('卷纲预览：点卷纲读「大纲/{卷名}.md」，浮层可关闭', async () => {
  const readNovelDoc = vi.fn(async () => ({
    ok: true,
    path: '大纲/卷纲_第001卷.md',
    text: '# 卷纲 第001卷\n\n开篇布局：断剑现世。\n',
    chars: 1200,
  }));
  const { wrapper } = mountPane(async () => statusPayload(), {
    get_novel_workbench: vi.fn(async () => workbenchPayload()),
    read_novel_doc: readNovelDoc,
  });

  await vi.waitFor(() => {
    expect(wrapper.findAll('[data-test="novel-setting-tab"]')).toHaveLength(3);
  });
  // 切到「卷纲」再点第一行
  await wrapper.findAll('[data-test="novel-setting-tab"]')[2].trigger('click');
  await wrapper.get('[data-test="novel-volume-row"]').trigger('click');

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-doc-text"]').exists()).toBe(true);
  });
  // 卷名已是 stem，父组件直接拼 大纲/{卷名}.md
  expect(readNovelDoc).toHaveBeenCalledWith(WORKDIR, '大纲/卷纲_第001卷.md');
  expect(wrapper.find('[data-test="novel-doc-viewer"]').exists()).toBe(true);
  expect(wrapper.get('[data-test="novel-doc-text"]').text()).toContain('断剑现世');

  await wrapper.get('[data-test="novel-doc-close"]').trigger('click');
  expect(wrapper.find('[data-test="novel-doc-viewer"]').exists()).toBe(false);
  wrapper.unmount();
});

it('容错：设定库读取失败时章节列表照常显示，只出分区内错误行', async () => {
  const getWorkbench = vi.fn(async () => {
    throw new Error('设定库扫描超时');
  });
  const { wrapper } = mountPane(async () => statusPayload(), { get_novel_workbench: getWorkbench });

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-workbench-error"]').exists()).toBe(true);
  });
  const err = wrapper.get('[data-test="novel-workbench-error"]');
  expect(err.text()).toContain('设定库数据读取失败');
  expect(err.text()).toContain('设定库扫描超时');
  // 主面板不退化成错误态：章节列表与字数快照都在
  expect(wrapper.find('[data-test="novel-error"]').exists()).toBe(false);
  expect(wrapper.get('[data-test="novel-title"]').text()).toBe('长夜书');
  expect(wrapper.find('[data-test="novel-chapter-list"]').exists()).toBe(true);
  expect(wrapper.findAll('[data-test="novel-chapter"]')).toHaveLength(4);
  wrapper.unmount();
});

it('切书：设定库按新目录重拉，上一本书的伏笔不残留', async () => {
  const WORKDIR_B = `${WORKDIR}/book-b`;
  const listNovelProjects = vi.fn(async () => ({
    ok: true,
    projects: [
      { path: '.', title: '长夜书', chapters_written: 3 },
      { path: 'book-b', title: '归墟', chapters_written: 5 },
    ],
  }));
  // 按目录返回不同的设定库内容，验证切书后确实换成了新书的表
  const getWorkbench = vi.fn(async (workdir: string) => (workdir === WORKDIR_B
    ? workbenchPayload({
      foreshadows: [
        {
          id: 'F900',
          summary: '归墟的旧案',
          status: '已埋',
          importance: '高',
          overdue: false,
          planned_overdue: false,
          stale_chapters: 0,
        },
      ],
    })
    : workbenchPayload()));
  const { wrapper } = mountPane(async () => statusPayload(), {
    list_novel_projects: listNovelProjects,
    get_novel_workbench: getWorkbench,
  });

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-book-select"]').exists()).toBe(true);
  });
  await vi.waitFor(() => {
    expect(getWorkbench).toHaveBeenCalledWith(WORKDIR);
  });

  await wrapper.get('[data-test="novel-book-select"]').setValue(WORKDIR_B);

  // 切书后按新书绝对路径重拉设定库，并渲染新书的伏笔
  await vi.waitFor(() => {
    expect(getWorkbench).toHaveBeenCalledWith(WORKDIR_B);
  });
  await vi.waitFor(() => {
    const rows = wrapper.findAll('[data-test="novel-foreshadow-row"]');
    expect(rows).toHaveLength(1);
    expect(rows[0].text()).toContain('归墟的旧案');
  });
  expect(wrapper.text()).not.toContain('断剑的来历');
  wrapper.unmount();
});

it('回合结束：设定库重新拉取，审查结论作废且不自动重跑', async () => {
  const getWorkbench = vi.fn(async () => workbenchPayload());
  const runNovelAudit = vi.fn(async () => ({
    is_novel: true,
    ok: true,
    scope: 'chapter',
    summary: { S1: 0, S2: 1, S3: 0, S4: 0 },
    findings: [
      { level: 'S2', type: 'foreshadow_overdue', file: '追踪/伏笔表.md', message: 'F001 已超期' },
    ],
    skipped: [],
  }));
  const { wrapper } = mountPane(async () => statusPayload(), {
    get_novel_workbench: getWorkbench,
    run_novel_audit: runNovelAudit,
  });
  const store = useAppStore();

  await vi.waitFor(() => {
    expect(getWorkbench).toHaveBeenCalledTimes(1);
  });
  await wrapper.get('[data-test="novel-audit-run"]').trigger('click');
  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-audit-summary"]').exists()).toBe(true);
  });

  // 回合结束（对话里刚写完一章）：设定库重拉，审查结论清空但不自动重跑
  store.running = true;
  await wrapper.vm.$nextTick();
  store.running = false;

  await vi.waitFor(() => {
    expect(getWorkbench).toHaveBeenCalledTimes(2);
  });
  expect(runNovelAudit).toHaveBeenCalledTimes(1);
  expect(wrapper.find('[data-test="novel-audit-summary"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="novel-audit-finding"]').exists()).toBe(false);
  wrapper.unmount();
});

// ---- 一致性审查修复入口：把 finding 拼成对话指令交回星瑶（只预填不发送） ----

// 多条 findings 的 fixture：覆盖三类检查项，单条修复与全部修复两条预填路径共用
function auditFindings() {
  return [
    { level: 'S1', type: 'tracking_state', file: '追踪/追踪表.md', message: '第 3 章状态与追踪表不一致' },
    { level: 'S2', type: 'foreshadow_overdue', file: '追踪/伏笔表.md', message: 'F001「断剑的来历」已超期 5 章' },
    { level: 'S3', type: 'chapter_wordcount', file: '细纲/第004章.md', message: '第 4 章字数 1200 未达目标 3000' },
  ];
}

function auditPayload(findings: Record<string, unknown>[]) {
  return {
    is_novel: true,
    ok: true,
    scope: 'chapter',
    summary: { S1: 1, S2: 1, S3: 1, S4: 0 },
    findings,
    skipped: [],
  };
}

// 点「运行审查」并等到结论渲染完成
async function runAuditAndWait(wrapper: any) {
  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-audit-run"]').exists()).toBe(true);
  });
  await wrapper.get('[data-test="novel-audit-run"]').trigger('click');
  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="novel-audit-summary"]').exists()).toBe(true);
  });
}

it('一致性审查修复：每条结论都有修复按钮，单条修复预填该条类型/文件/原文与最小改动要求', async () => {
  const findings = auditFindings();
  const runNovelAudit = vi.fn(async () => auditPayload(findings));
  const { wrapper } = mountPane(async () => statusPayload(), { run_novel_audit: runNovelAudit });
  const store = useAppStore();

  await runAuditAndWait(wrapper);
  // 每条 finding 一条修复按钮，且初始没有草稿
  const fixes = wrapper.findAll('[data-test="novel-audit-fix"]');
  expect(fixes).toHaveLength(findings.length);
  expect(store._getSessionRuntime().composerText || '').toBe('');

  await fixes[0].trigger('click');

  // 只预填输入框、不直接改文件也不自动发送
  const draft = store._getSessionRuntime().composerText || '';
  expect(draft).toContain(WORKDIR);
  expect(draft).toContain(findings[0].type);
  expect(draft).toContain(findings[0].file);
  expect(draft).toContain(findings[0].message);
  // 别的条目不该混进这一条指令
  expect(draft).not.toContain(findings[1].message);
  // 明确要求最小改动 + 改完重跑审查
  expect(draft).toContain('只做最小改动');
  expect(draft).toContain('重跑一致性审查');
  wrapper.unmount();
});

it('一致性审查修复：全部交给星瑶修按类型分组预填，含组标题、每组条数与重跑要求', async () => {
  const findings = auditFindings();
  const runNovelAudit = vi.fn(async () => auditPayload(findings));
  const { wrapper } = mountPane(async () => statusPayload(), { run_novel_audit: runNovelAudit });
  const store = useAppStore();

  await runAuditAndWait(wrapper);
  const fixAll = wrapper.get('[data-test="novel-audit-fix-all"]');
  expect(fixAll.text()).toContain('全部交给星瑶修');
  await fixAll.trigger('click');

  const draft = store._getSessionRuntime().composerText || '';
  // 总数 + 按 type 分组的标题与每组条数
  expect(draft).toContain('共 3 条');
  expect(draft).toContain('## tracking_state（1 条）');
  expect(draft).toContain('## foreshadow_overdue（1 条）');
  expect(draft).toContain('## chapter_wordcount（1 条）');
  // 分组顺序按后端检查项顺序，不按 findings 出现顺序乱排
  expect(draft.indexOf('## tracking_state')).toBeLessThan(draft.indexOf('## foreshadow_overdue'));
  for (const finding of findings) {
    expect(draft).toContain(finding.message);
    expect(draft).toContain(finding.file);
  }
  expect(draft).toContain('重跑一致性审查');
  wrapper.unmount();
});

it('一致性审查修复：修复中两个入口都 disabled，挡住连点重复预填', async () => {
  const runNovelAudit = vi.fn(async () => auditPayload(auditFindings()));
  const { wrapper } = mountPane(async () => statusPayload(), { run_novel_audit: runNovelAudit });

  await runAuditAndWait(wrapper);
  // 就绪态两个入口都可点
  expect(wrapper.get('[data-test="novel-audit-fix-all"]').attributes('disabled')).toBeUndefined();
  expect(wrapper.get('[data-test="novel-audit-fix"]').attributes('disabled')).toBeUndefined();

  // 点「全部修复」即上锁（fixing=true 短时窗口）：全部修复与每条的修复按钮一起禁用
  await wrapper.get('[data-test="novel-audit-fix-all"]').trigger('click');
  expect(wrapper.get('[data-test="novel-audit-fix-all"]').attributes('disabled')).toBeDefined();
  const fixes = wrapper.findAll('[data-test="novel-audit-fix"]');
  expect(fixes.length).toBeGreaterThan(0);
  for (const btn of fixes) expect(btn.attributes('disabled')).toBeDefined();
  wrapper.unmount();
});

it('一致性审查修复：findings 为空时不渲染「全部修复」，只显示没有发现问题', async () => {
  const runNovelAudit = vi.fn(async () => auditPayload([]));
  const { wrapper } = mountPane(async () => statusPayload(), { run_novel_audit: runNovelAudit });

  await runAuditAndWait(wrapper);
  expect(wrapper.findAll('[data-test="novel-audit-finding"]')).toHaveLength(0);
  expect(wrapper.find('[data-test="novel-audit-fix"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="novel-audit-fix-all"]').exists()).toBe(false);
  expect(wrapper.text()).toContain('没有发现问题');
  wrapper.unmount();
});