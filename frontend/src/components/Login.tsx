import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiService } from '../services/api';
import { AlertCircle } from 'lucide-react';

export function Login() {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      await apiService.login(username, password);
      navigate('/');
    } catch (err) {
      console.error('Login error:', err);
      setError('登录失败，请检查用户名和密码');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-claude-bg">
      <div className="bg-white rounded-2xl border border-claude-border p-8 w-full max-w-md">
        {/* Logo */}
        <div className="flex justify-center mb-6">
          <img src="/logo.jpg" alt="OpenCapyBox" className="w-20 h-20 rounded-2xl object-cover" />
        </div>

        {/* Title */}
        <h1 className="text-3xl font-medium text-center text-claude-text mb-2 tracking-tight">
          OpenCapyBox
        </h1>
        <p className="text-center text-claude-secondary mb-8">
          你的 AI 助手，住在安全的盒子里 🛁
        </p>

        {/* Error Message */}
        {error && (
          <div className="mb-6 p-4 bg-red-50 border border-red-100 rounded-lg flex items-center gap-3 text-claude-error">
            <AlertCircle className="w-5 h-5 flex-shrink-0" />
            <span className="text-sm">{error}</span>
          </div>
        )}

        {/* Login Form */}
        <form onSubmit={handleSubmit} className="space-y-5">
          <div>
            <label htmlFor="username" className="block text-sm font-medium text-claude-text mb-2">
              用户名
            </label>
            <input
              id="username"
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              className="w-full px-4 py-3 bg-white border border-claude-border rounded-xl focus:ring-2 focus:ring-claude-accent/20 focus:border-claude-accent transition-all outline-none text-claude-text placeholder-claude-muted"
              placeholder="请输入用户名"
              disabled={loading}
            />
          </div>

          <div>
            <label htmlFor="password" className="block text-sm font-medium text-claude-text mb-2">
              密码
            </label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              className="w-full px-4 py-3 bg-white border border-claude-border rounded-xl focus:ring-2 focus:ring-claude-accent/20 focus:border-claude-accent transition-all outline-none text-claude-text placeholder-claude-muted"
              placeholder="请输入密码"
              disabled={loading}
            />
          </div>

          <button
            type="submit"
            disabled={loading}
            className="w-full py-3 text-base bg-claude-text text-white rounded-xl hover:bg-claude-text/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {loading ? '正在登录...' : '登 录'}
          </button>
        </form>

        {/* Footer */}
        <p className="mt-8 text-center text-xs text-claude-muted uppercase tracking-widest">
          Powered by Ryx
        </p>
      </div>
    </div>
  );
}
