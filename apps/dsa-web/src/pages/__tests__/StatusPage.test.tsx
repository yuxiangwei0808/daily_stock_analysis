import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import StatusPage from '../StatusPage';

const api = vi.hoisted(() => ({ getStatus: vi.fn() }));
vi.mock('../../api/tradeDesk', () => ({ tradeDeskApi: api }));

describe('StatusPage', () => {
  it('lists each component with its state and detail', async () => {
    api.getStatus.mockResolvedValue({
      checkedAt: '2026-09-30T15:00:00Z', overall: 'error',
      components: [
        { key: 'opend', label: 'moomoo OpenD quotes', state: 'ok', detail: 'connected' },
        { key: 'pulse', label: 'Market pulse', state: 'error', detail: 'OperationalError since 9 min ago' },
        { key: 'social', label: 'Social scan', state: 'off', detail: 'SOCIAL_SCAN_ENABLED is off' },
      ],
    });
    render(<MemoryRouter><StatusPage /></MemoryRouter>);
    expect(await screen.findByTestId('status-overall')).toHaveTextContent('Something is failing');
    const rows = within(screen.getByTestId('status-components')).getAllByRole('listitem');
    expect(rows.map((row) => row.textContent)).toEqual([
      'Working:moomoo OpenD quotesconnected', 'Failing:Market pulseOperationalError since 9 min ago',
      'Off:Social scanSOCIAL_SCAN_ENABLED is off']);
  });
});
