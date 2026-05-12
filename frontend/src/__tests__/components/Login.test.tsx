import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '../utils/test-utils';
import { Login } from '../../components/Login';
import { apiService } from '../../services/api';

// Mock apiService
vi.mock('../../services/api', () => ({
  apiService: {
    login: vi.fn(),
    getUserId: vi.fn(() => null),
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

describe('Login 組件', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('應該渲染登錄表單', () => {
    render(<Login />);

    expect(screen.getByText('OpenCapyBox')).toBeInTheDocument();
    expect(screen.getByLabelText('用户名')).toBeInTheDocument();
    expect(screen.getByLabelText('密码')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /登\s*录/ })).toBeInTheDocument();
  });

  it('應該允許輸入用戶名和密碼', () => {
    render(<Login />);

    const usernameInput = screen.getByLabelText('用户名');
    const passwordInput = screen.getByLabelText('密码');

    fireEvent.change(usernameInput, { target: { value: 'testuser' } });
    fireEvent.change(passwordInput, { target: { value: 'testpass' } });

    expect(usernameInput).toHaveValue('testuser');
    expect(passwordInput).toHaveValue('testpass');
  });

  it('登錄成功應該導航到首頁', async () => {
    vi.mocked(apiService.login).mockResolvedValue({
      user_id: 'test-session',
      access_token: 'mock-token',
      token_type: 'bearer',
      expires_in: 3600,
      role: 'user',
      is_admin: false,
      message: 'success',
    });

    render(<Login />);

    fireEvent.change(screen.getByLabelText('用户名'), {
      target: { value: 'testuser' },
    });
    fireEvent.change(screen.getByLabelText('密码'), {
      target: { value: 'testpass' },
    });
    fireEvent.click(screen.getByRole('button', { name: /登\s*录/ }));

    await waitFor(() => {
      expect(apiService.login).toHaveBeenCalledWith('testuser', 'testpass');
      expect(mockNavigate).toHaveBeenCalledWith('/');
    });
  });

  it('管理員登錄成功應該導航到管理後台', async () => {
    vi.mocked(apiService.login).mockResolvedValue({
      user_id: 'admin',
      access_token: 'mock-token',
      token_type: 'bearer',
      expires_in: 3600,
      role: 'admin',
      is_admin: true,
      message: 'success',
    });

    render(<Login />);

    fireEvent.change(screen.getByLabelText('用户名'), {
      target: { value: 'admin' },
    });
    fireEvent.change(screen.getByLabelText('密码'), {
      target: { value: 'admin-test-pass' },
    });
    fireEvent.click(screen.getByRole('button', { name: /登\s*录/ }));

    await waitFor(() => {
      expect(apiService.login).toHaveBeenCalledWith('admin', 'admin-test-pass');
      expect(mockNavigate).toHaveBeenCalledWith('/admin');
    });
  });

  it('登錄失敗應該顯示錯誤訊息', async () => {
    vi.mocked(apiService.login).mockRejectedValue(new Error('Login failed'));

    render(<Login />);

    fireEvent.change(screen.getByLabelText('用户名'), {
      target: { value: 'wronguser' },
    });
    fireEvent.change(screen.getByLabelText('密码'), {
      target: { value: 'wrongpass' },
    });
    fireEvent.click(screen.getByRole('button', { name: /登\s*录/ }));

    await waitFor(() => {
      expect(screen.getByText('登录失败，请检查用户名和密码')).toBeInTheDocument();
    });
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it('账号被禁用时应显示明确提示', async () => {
    vi.mocked(apiService.login).mockRejectedValue({
      response: { data: { detail: '账户已被禁用' } },
    });

    render(<Login />);

    fireEvent.change(screen.getByLabelText('用户名'), {
      target: { value: 'disabled-user' },
    });
    fireEvent.change(screen.getByLabelText('密码'), {
      target: { value: 'pass123' },
    });
    fireEvent.click(screen.getByRole('button', { name: /登\s*录/ }));

    await waitFor(() => {
      expect(screen.getByText('账户已被禁用')).toBeInTheDocument();
    });
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it('企业登录细节不应显示给普通登录页用户', async () => {
    vi.mocked(apiService.login).mockRejectedValue({
      response: { data: { detail: '请使用企业统一登录' } },
    });

    render(<Login />);

    fireEvent.change(screen.getByLabelText('用户名'), {
      target: { value: 'ldap-user' },
    });
    fireEvent.change(screen.getByLabelText('密码'), {
      target: { value: 'anypass' },
    });
    fireEvent.click(screen.getByRole('button', { name: /登\s*录/ }));

    await waitFor(() => {
      expect(screen.getByText('登录失败，请检查用户名和密码')).toBeInTheDocument();
    });
    expect(screen.queryByText('请使用企业统一登录')).not.toBeInTheDocument();
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it('登錄中應該禁用按鈕並顯示載入狀態', async () => {
    // 創建一個永不 resolve 的 Promise 來模擬載入狀態
    vi.mocked(apiService.login).mockImplementation(
      () => new Promise(() => {})
    );

    render(<Login />);

    fireEvent.change(screen.getByLabelText('用户名'), {
      target: { value: 'testuser' },
    });
    fireEvent.change(screen.getByLabelText('密码'), {
      target: { value: 'testpass' },
    });
    fireEvent.click(screen.getByRole('button', { name: /登\s*录/ }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '正在登录...' })).toBeDisabled();
    });
  });
});
