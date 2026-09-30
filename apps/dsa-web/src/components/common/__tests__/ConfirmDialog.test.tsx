import { fireEvent, render, screen } from '@testing-library/react';
import type React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { ConfirmDialog } from '../ConfirmDialog';

function renderDialog(overrides: Partial<React.ComponentProps<typeof ConfirmDialog>> = {}) {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  const result = render(
    <UiLanguageProvider>
      <ConfirmDialog
        isOpen
        title="确认操作"
        message="确认继续吗？"
        confirmText="确定"
        cancelText="取消"
        onConfirm={onConfirm}
        onCancel={onCancel}
        {...overrides}
      />
    </UiLanguageProvider>,
  );
  return { onConfirm, onCancel, ...result };
}

describe('ConfirmDialog', () => {
  it('disables confirm and cancel actions independently', () => {
    const { onConfirm, onCancel } = renderDialog({
      confirmDisabled: true,
      cancelDisabled: true,
    });

    fireEvent.click(screen.getByRole('button', { name: '确定' }));
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    fireEvent.click(document.body.lastElementChild as HTMLElement);

    expect(screen.getByRole('button', { name: '确定' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '取消' })).toBeDisabled();
    expect(onConfirm).not.toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('keeps the default confirm and cancel behavior when not disabled', () => {
    const { onConfirm, onCancel } = renderDialog();

    fireEvent.click(screen.getByRole('button', { name: '确定' }));
    fireEvent.click(screen.getByRole('button', { name: '取消' }));

    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});

describe('ConfirmDialog keyboard and screen-reader contract', () => {
  it('is a labelled modal dialog that starts on Cancel, keeps Tab inside and closes on Escape', () => {
    const onCancel = vi.fn();
    const opener = document.createElement('button');
    document.body.appendChild(opener);
    opener.focus();
    const { rerender } = render(<ConfirmDialog isOpen title="Delete it?" message="Gone for good." confirmText="Delete" cancelText="Cancel" onConfirm={() => undefined} onCancel={onCancel} />);
    const dialog = screen.getByRole('dialog', { name: 'Delete it?' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleDescription('Gone for good.');
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(screen.getByRole('button', { name: 'Delete' })).toHaveFocus();
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(1);
    rerender(<ConfirmDialog isOpen={false} title="Delete it?" message="Gone for good." onConfirm={() => undefined} onCancel={onCancel} />);
    expect(opener).toHaveFocus();
    opener.remove();
  });
});
