import { describe, it, expect, beforeEach, vi } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import { useAppStore } from './appStore';

describe('appStore update functionality', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    (window as any).pywebview = {
      api: {
        check_update: vi.fn(),
        download_update: vi.fn(),
        cancel_download_update: vi.fn(),
        has_active_tasks: vi.fn(),
        launch_installer: vi.fn(),
      },
    };
  });

  it('updates state on checkUpdate when new version is available', async () => {
    const store = useAppStore();
    (window as any).pywebview.api.check_update.mockResolvedValue({
      ok: true,
      has_update: true,
      latest_version: '1.2.6',
      current_version: '1.2.5',
      release_title: 'Y-CODE v1.2.6',
      release_notes: '- 新增检查更新功能',
      download_url: 'https://gitee.com/download/setup.exe',
      asset_name: 'Y-CODE-Setup-v1.2.6.exe',
      asset_size: 52428800,
    });

    await store.checkUpdate(false);

    expect(store.updateState.hasUpdate).toBe(true);
    expect(store.updateState.latestVersion).toBe('1.2.6');
    expect(store.updateState.status).toBe('available');
    expect(store.updateState.showUpdateModal).toBe(false);

    // 手动检查时应打开弹窗
    await store.checkUpdate(true);
    expect(store.updateState.showUpdateModal).toBe(true);
  });

  it('notifies user on checkUpdate when already latest version', async () => {
    const store = useAppStore();
    store.version = '1.2.5';
    (window as any).pywebview.api.check_update.mockResolvedValue({
      ok: true,
      has_update: false,
      latest_version: '1.2.5',
      current_version: '1.2.5',
      msg: '当前已是最新版本',
    });

    await store.checkUpdate(true);

    expect(store.updateState.hasUpdate).toBe(false);
    expect(store.updateState.status).toBe('idle');
    expect(store.confirmModal.show).toBe(true);
    expect(store.confirmModal.title).toBe('检查更新');
    expect(store.confirmModal.message).toContain('当前已是最新版本');
  });

  it('handles download progress and completion events', async () => {
    const store = useAppStore();
    store.updateState.downloadUrl = 'https://gitee.com/download/setup.exe';
    store.updateState.latestVersion = '1.2.6';
    (window as any).pywebview.api.download_update.mockResolvedValue({ ok: true });

    await store.startDownloadUpdate();
    expect(store.updateState.status).toBe('downloading');

    // 模拟下载进度事件
    store.handleBridgeEvent({
      type: 'update_download_progress',
      data: { percent: 45, downloaded: 20000000, total: 50000000 },
    });
    expect(store.updateState.downloadProgress).toBe(45);
    expect(store.updateState.downloadedBytes).toBe(20000000);

    // 模拟下载完成事件
    store.handleBridgeEvent({
      type: 'update_download_complete',
      data: { installer_path: 'C:/temp/setup.exe' },
    });
    expect(store.updateState.status).toBe('downloaded');
    expect(store.updateState.installerPath).toBe('C:/temp/setup.exe');
  });

  it('warns user if tasks are active when installing', async () => {
    const store = useAppStore();
    store.updateState.installerPath = 'C:/temp/setup.exe';
    store.updateState.latestVersion = '1.2.6';
    store.running = true; // 模拟当前有任务在跑

    // 触发安装点击
    store.handleInstallClick();

    expect(store.confirmModal.show).toBe(true);
    expect(store.confirmModal.title).toContain('任务进行中警告');
    expect(store.confirmModal.message).toContain('中断正在执行的任务');
  });
});
