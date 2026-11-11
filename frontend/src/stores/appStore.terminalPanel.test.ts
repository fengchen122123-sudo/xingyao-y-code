import { beforeEach, describe, expect, it } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import {
  TERMINAL_PANEL_DEFAULT_HEIGHT,
  TERMINAL_PANEL_HEIGHT_KEY,
  TERMINAL_PANEL_MAX_HEIGHT,
  TERMINAL_PANEL_MIN_HEIGHT,
  TERMINAL_PANEL_OPEN_KEY,
  useAppStore,
} from './appStore';
import { onPtyData, onPtyExit } from '../utils/ptyBus';

beforeEach(() => {
  setActivePinia(createPinia());
  localStorage.clear();
});

describe('terminal panel state', () => {
  it('toggles open state and persists it', () => {
    const store = useAppStore();
    expect(store.terminalPanelOpen).toBe(false);

    store.toggleTerminalPanel();
    expect(store.terminalPanelOpen).toBe(true);
    expect(localStorage.getItem(TERMINAL_PANEL_OPEN_KEY)).toBe('1');

    store.toggleTerminalPanel();
    expect(store.terminalPanelOpen).toBe(false);
    expect(localStorage.getItem(TERMINAL_PANEL_OPEN_KEY)).toBeNull();
  });

  it('clamps dragged height into bounds and persists it', () => {
    const store = useAppStore();

    store.setTerminalPanelHeight(99999);
    expect(store.terminalPanelHeight).toBe(TERMINAL_PANEL_MAX_HEIGHT);
    store.setTerminalPanelHeight(10);
    expect(store.terminalPanelHeight).toBe(TERMINAL_PANEL_MIN_HEIGHT);

    store.setTerminalPanelHeight(420);
    expect(store.terminalPanelHeight).toBe(420);
    expect(localStorage.getItem(TERMINAL_PANEL_HEIGHT_KEY)).toBe('420');
  });

  it('restores persisted open state and height on creation', () => {
    localStorage.setItem(TERMINAL_PANEL_OPEN_KEY, '1');
    localStorage.setItem(TERMINAL_PANEL_HEIGHT_KEY, '360');
    const store = useAppStore();
    expect(store.terminalPanelOpen).toBe(true);
    expect(store.terminalPanelHeight).toBe(360);
  });

  it('falls back to defaults for corrupt persisted values', () => {
    localStorage.setItem(TERMINAL_PANEL_HEIGHT_KEY, 'not-a-number');
    const store = useAppStore();
    expect(store.terminalPanelHeight).toBe(TERMINAL_PANEL_DEFAULT_HEIGHT);
  });

  it('forwards pty bridge events to the bus without touching reactive state', () => {
    const store = useAppStore();
    const seen: string[] = [];
    const exits: number[] = [];
    const offData = onPtyData((t) => seen.push(t));
    const offExit = onPtyExit((c) => exits.push(c));

    store.handleBridgeEvent({ type: 'pty_data', data: { text: '$ dir\r\n' } });
    store.handleBridgeEvent({ type: 'pty_exit', data: { code: 3 } });

    expect(seen).toEqual(['$ dir\r\n']);
    expect(exits).toEqual([3]);

    offData();
    offExit();
  });
});
