import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import MessageBubble from './MessageBubble.vue';
import { useAppStore, type ChatMessage } from '../stores/appStore';

function userMessage(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return { role: 'user', content: '原始提问', ...overrides };
}

beforeEach(() => setActivePinia(createPinia()));

it('persisted user message exposes an edit button', () => {
  const wrapper = mount(MessageBubble, { props: { msg: userMessage({ _msg_id: 12 }) } });

  expect(wrapper.find('[data-test="user-edit-btn"]').exists()).toBe(true);
  wrapper.unmount();
});

it('message without _msg_id cannot be edited', () => {
  const wrapper = mount(MessageBubble, { props: { msg: userMessage() } });

  expect(wrapper.find('[data-test="user-edit-btn"]').exists()).toBe(false);
  wrapper.unmount();
});

it('editing then Enter submits the new text to the store', async () => {
  const store = useAppStore();
  const editSpy = vi.spyOn(store, 'editUserMessage').mockResolvedValue(true);
  const wrapper = mount(MessageBubble, { props: { msg: userMessage({ _msg_id: 12 }) } });

  await wrapper.get('[data-test="user-edit-btn"]').trigger('click');
  const textarea = wrapper.get('[data-test="user-edit-input"]');
  expect((textarea.element as HTMLTextAreaElement).value).toBe('原始提问');

  await textarea.setValue('改过的提问');
  await textarea.trigger('keydown', { key: 'Enter' });
  await flushPromises();

  expect(editSpy).toHaveBeenCalledWith(
    expect.objectContaining({ _msg_id: 12 }),
    '改过的提问',
    [],
  );
  wrapper.unmount();
});

it('Escape cancels editing without touching the store', async () => {
  const store = useAppStore();
  const editSpy = vi.spyOn(store, 'editUserMessage').mockResolvedValue(true);
  const wrapper = mount(MessageBubble, { props: { msg: userMessage({ _msg_id: 12 }) } });

  await wrapper.get('[data-test="user-edit-btn"]').trigger('click');
  const textarea = wrapper.get('[data-test="user-edit-input"]');
  await textarea.trigger('keydown', { key: 'Escape' });
  await flushPromises();

  expect(editSpy).not.toHaveBeenCalled();
  expect(wrapper.find('[data-test="user-edit-input"]').exists()).toBe(false);
  expect(wrapper.get('.user-text').text()).toBe('原始提问');
  wrapper.unmount();
});

it('blank text is not submitted', async () => {
  const store = useAppStore();
  const editSpy = vi.spyOn(store, 'editUserMessage').mockResolvedValue(true);
  const wrapper = mount(MessageBubble, { props: { msg: userMessage({ _msg_id: 12 }) } });

  await wrapper.get('[data-test="user-edit-btn"]').trigger('click');
  const textarea = wrapper.get('[data-test="user-edit-input"]');
  await textarea.setValue('   ');
  await textarea.trigger('keydown', { key: 'Enter' });
  await flushPromises();

  expect(editSpy).not.toHaveBeenCalled();
  expect(wrapper.find('[data-test="user-edit-input"]').exists()).toBe(true);
  wrapper.unmount();
});

it('editing exposes the message images with remove buttons', async () => {
  const wrapper = mount(MessageBubble, {
    props: { msg: userMessage({ _msg_id: 12, images: ['data:image/png;base64,AAA'] }) },
  });

  await wrapper.get('[data-test="user-edit-btn"]').trigger('click');

  expect(wrapper.findAll('[data-test="user-edit-image-remove"]').length).toBe(1);
  expect(wrapper.find('[data-test="user-edit-image-add"]').exists()).toBe(true);
  wrapper.unmount();
});

it('kept images are persisted to paths and passed to the store', async () => {
  const store = useAppStore();
  const saveSpy = vi.spyOn(store, 'saveClipboardImage').mockResolvedValue('C:/tmp/edit-1.png');
  const editSpy = vi.spyOn(store, 'editUserMessage').mockResolvedValue(true);
  const wrapper = mount(MessageBubble, {
    props: { msg: userMessage({ _msg_id: 12, images: ['data:image/png;base64,AAA'] }) },
  });

  await wrapper.get('[data-test="user-edit-btn"]').trigger('click');
  await wrapper.get('[data-test="user-edit-input"]').setValue('改过的提问');
  await wrapper.get('[data-test="user-edit-submit"]').trigger('click');
  await flushPromises();

  expect(saveSpy).toHaveBeenCalledWith('data:image/png;base64,AAA');
  expect(editSpy).toHaveBeenCalledWith(
    expect.objectContaining({ _msg_id: 12 }), '改过的提问', ['C:/tmp/edit-1.png'],
  );
  wrapper.unmount();
});

it('removed images are not resent', async () => {
  const store = useAppStore();
  const saveSpy = vi.spyOn(store, 'saveClipboardImage').mockResolvedValue('C:/tmp/edit-1.png');
  const editSpy = vi.spyOn(store, 'editUserMessage').mockResolvedValue(true);
  const wrapper = mount(MessageBubble, {
    props: { msg: userMessage({ _msg_id: 12, images: ['data:image/png;base64,AAA'] }) },
  });

  await wrapper.get('[data-test="user-edit-btn"]').trigger('click');
  await wrapper.get('[data-test="user-edit-image-remove"]').trigger('click');
  await wrapper.get('[data-test="user-edit-input"]').setValue('改过的提问');
  await wrapper.get('[data-test="user-edit-submit"]').trigger('click');
  await flushPromises();

  expect(saveSpy).not.toHaveBeenCalled();
  expect(editSpy).toHaveBeenCalledWith(
    expect.objectContaining({ _msg_id: 12 }), '改过的提问', [],
  );
  wrapper.unmount();
});

it('removing the only image submits even when the text is unchanged', async () => {
  const store = useAppStore();
  vi.spyOn(store, 'saveClipboardImage').mockResolvedValue('C:/tmp/edit-1.png');
  const editSpy = vi.spyOn(store, 'editUserMessage').mockResolvedValue(true);
  const wrapper = mount(MessageBubble, {
    props: { msg: userMessage({ _msg_id: 12, images: ['data:image/png;base64,AAA'] }) },
  });

  await wrapper.get('[data-test="user-edit-btn"]').trigger('click');
  await wrapper.get('[data-test="user-edit-image-remove"]').trigger('click');
  await wrapper.get('[data-test="user-edit-submit"]').trigger('click');
  await flushPromises();

  expect(editSpy).toHaveBeenCalledWith(
    expect.objectContaining({ _msg_id: 12 }), '原始提问', [],
  );
  wrapper.unmount();
});
