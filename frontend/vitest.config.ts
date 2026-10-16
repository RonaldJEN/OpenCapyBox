import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/__tests__/setup.ts'],
    include: ['src/**/*.{test,spec}.{js,mjs,cjs,ts,mts,cts,jsx,tsx}'],
    // 真实 pdf.js（Node legacy build）加载时会 require 原生模块 @napi-rs/canvas；
    // 原生模块在 worker thread 中退出时会让整个测试进程 segfault，必须放到子进程执行。
    poolMatchGlobs: [['**/pdfJsDocumentIsolation.test.ts', 'forks']],
  },
});
