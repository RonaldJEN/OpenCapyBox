import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '../utils/test-utils';
import { SessionList } from '../../components/SessionList';
import { apiService } from '../../services/api';
import { SessionStatus } from '../../types';

// Mock apiService
vi.mock('../../services/api', () => ({
  apiService: {
    getSessions: vi.fn(),
    deleteSession: vi.fn(),
    logout: vi.fn(),
    getUserId: vi.fn(() => 'mock-session'),
    getRunningSession: vi.fn().mockResolvedValue({ running_session_id: null }),
  },
}));

// Mock useNavigate
const mockNavigate = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  };
});

describe('SessionList 組件', () => {
  const mockSessions = [
    {
      id: 'session-1',
      user_id: 'user-1',
      status: SessionStatus.ACTIVE,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      title: '測試會話 1',
    },
    {
      id: 'session-2',
      user_id: 'user-1',
      status: SessionStatus.COMPLETED,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      title: '測試會話 2',
    },
  ];

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(apiService.getSessions).mockResolvedValue({
      sessions: mockSessions,
    });
  });

  it('應該顯示載入狀態', () => {
    vi.mocked(apiService.getSessions).mockImplementation(
      () => new Promise(() => {})
    );

    render(<SessionList onSessionSelect={vi.fn()} />);

    // 檢查是否有載入動畫
    expect(document.querySelector('.animate-spin')).toBeInTheDocument();
  });

  it('應該顯示會話列表', async () => {
    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('測試會話 1')).toBeInTheDocument();
      expect(screen.getByText('測試會話 2')).toBeInTheDocument();
    });
  });

  it('點擊會話應該觸發 onSessionSelect', async () => {
    const mockOnSelect = vi.fn();
    render(<SessionList onSessionSelect={mockOnSelect} />);

    await waitFor(() => {
      expect(screen.getByText('測試會話 1')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText('測試會話 1'));
    expect(mockOnSelect).toHaveBeenCalledWith('session-1');
  });

  it('點擊登出應該調用 logout 並導航', async () => {
    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('退出登录')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText('退出登录'));

    expect(apiService.logout).toHaveBeenCalled();
    expect(mockNavigate).toHaveBeenCalledWith('/login');
  });

  it('應該顯示品牌名稱 OpenCapyBox', async () => {
    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('OpenCapyBox')).toBeInTheDocument();
    });
  });

  it('應該顯示 HISTORY 標籤', async () => {
    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('History')).toBeInTheDocument();
    });
  });

  it('應該顯示新建對話按鈕並觸發 onNewChat', async () => {
    const mockOnNewChat = vi.fn();
    render(<SessionList onSessionSelect={vi.fn()} onNewChat={mockOnNewChat} />);

    await waitFor(() => {
      expect(screen.getByTitle('新建对话')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTitle('新建对话'));
    expect(mockOnNewChat).toHaveBeenCalledTimes(1);
  });

  it('未傳入 onNewChat 時不應顯示新建對話按鈕', async () => {
    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('OpenCapyBox')).toBeInTheDocument();
    });

    expect(screen.queryByTitle('新建对话')).not.toBeInTheDocument();
  });
});
