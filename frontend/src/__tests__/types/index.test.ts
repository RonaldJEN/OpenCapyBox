import { describe, it, expect } from 'vitest';
import {
  SessionStatus,
  type Session,
  type StepData,
  type FileInfo,
} from '../../types';

describe('類型定義', () => {
  describe('SessionStatus 枚舉', () => {
    it('應該包含正確的狀態值', () => {
      expect(SessionStatus.ACTIVE).toBe('active');
      expect(SessionStatus.PAUSED).toBe('paused');
      expect(SessionStatus.COMPLETED).toBe('completed');
    });
  });

  describe('Session 類型', () => {
    it('應該正確定義會話結構', () => {
      const session: Session = {
        id: 'session-1',
        user_id: 'user-1',
        status: SessionStatus.ACTIVE,
        created_at: '2024-01-01T00:00:00Z',
        updated_at: '2024-01-01T00:00:00Z',
        title: '測試會話',
      };

      expect(session.id).toBe('session-1');
      expect(session.status).toBe(SessionStatus.ACTIVE);
    });
  });

  describe('StepData 類型', () => {
    it('應該正確定義步驟數據結構', () => {
      const step: StepData = {
        step_number: 1,
        thinking: '思考中...',
        assistant_content: '回覆內容',
        tool_calls: [{ name: 'read_file', input: { path: '/test' } }],
        tool_results: [{ success: true, content: '文件內容' }],
        status: 'completed',
      };

      expect(step.step_number).toBe(1);
      expect(step.tool_calls).toHaveLength(1);
    });
  });

  describe('FileInfo 類型', () => {
    it('應該正確定義文件信息結構', () => {
      const file: FileInfo = {
        name: 'test.txt',
        path: '/path/to/test.txt',
        size: 1024,
        modified: '2024-01-01T00:00:00Z',
        type: 'text/plain',
      };

      expect(file.name).toBe('test.txt');
      expect(file.size).toBe(1024);
    });
  });
});
