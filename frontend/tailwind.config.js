import colors from 'tailwindcss/colors'

/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['ui-sans-serif', '-apple-system', 'BlinkMacSystemFont', '"Segoe UI"', 'Helvetica', 'Arial', 'sans-serif'],
        mono: ['"Fira Code"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      colors: {
        // Claude 暖色体系
        claude: {
          bg: '#FAF9F6',           // 全局暖白背景
          surface: '#F3F1EB',      // 卡片/面板/用户消息背景
          input: '#FFFFFF',        // 输入框白底
          text: '#1A1915',         // 主文字（暖黑）
          secondary: '#6B6459',    // 次要文字
          muted: '#AEA89B',        // 占位符/淡文字
          accent: '#D4A574',       // 暖色强调
          border: '#E8E5DE',       // 统一边框色
          'border-strong': '#D5D1C8', // 加深边框（聚焦）
          hover: '#EDEAE3',        // 悬停背景
          success: '#16A34A',      // 功能色：成功
          error: '#DC2626',        // 功能色：错误
          warning: '#D97706',      // 功能色：警告
        },
        // 保留 primary 色系以兼容
        primary: {
          50: '#f0f9ff',
          100: '#e0f2fe',
          200: '#bae6fd',
          300: '#7dd3fc',
          400: '#38bdf8',
          500: '#0ea5e9',
          600: '#0284c7',
          700: '#0369a1',
          800: '#075985',
          900: '#0c4a6e',
        },
        gray: colors.gray,
      },
      borderRadius: {
        '2xl': '16px',
        '3xl': '24px',
        '4xl': '32px',
      },
      keyframes: {
        shimmer: {
          '0%': { transform: 'translateX(-100%)' },
          '100%': { transform: 'translateX(200%)' },
        },
        'fade-in': {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        'slide-in-from-bottom': {
          '0%': { transform: 'translateY(10px)', opacity: '0' },
          '100%': { transform: 'translateY(0)', opacity: '1' },
        },
        'slide-in-from-right': {
          '0%': { transform: 'translateX(100%)' },
          '100%': { transform: 'translateX(0)' },
        },
        'zoom-in': {
          '0%': { transform: 'scale(0.95)', opacity: '0' },
          '100%': { transform: 'scale(1)', opacity: '1' },
        },
        blink: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0' },
        },
        'dot-pulse': {
          '0%, 80%, 100%': { opacity: '0.3', transform: 'scale(0.8)' },
          '40%': { opacity: '1', transform: 'scale(1)' },
        },
      },
      animation: {
        shimmer: 'shimmer 1.5s infinite',
        'fade-in': 'fade-in 0.3s ease-out',
        'slide-in-bottom': 'slide-in-from-bottom 0.5s ease-out',
        'slide-in-right': 'slide-in-from-right 0.5s ease-out',
        'zoom-in': 'zoom-in 0.4s ease-out',
        blink: 'blink 1s step-end infinite',
        'dot-pulse': 'dot-pulse 1.4s ease-in-out infinite',
      },
      typography: {
        DEFAULT: {
          css: {
            maxWidth: 'none',
            color: '#1A1915',
            lineHeight: '1.7',
            fontSize: '15px',
            a: {
              color: '#2563EB',
              textDecoration: 'none',
              '&:hover': {
                textDecoration: 'underline',
              },
            },
            code: {
              color: '#C2410C',
              backgroundColor: '#F3F1EB',
              padding: '0.2rem 0.4rem',
              borderRadius: '0.375rem',
              fontWeight: '500',
              fontSize: '0.875em',
              border: '1px solid #E8E5DE',
            },
            'code::before': {
              content: '""',
            },
            'code::after': {
              content: '""',
            },
            blockquote: {
              borderLeftColor: '#D4A574',
              backgroundColor: 'rgba(250, 249, 246, 0.5)',
              borderRadius: '0 0.5rem 0.5rem 0',
              fontStyle: 'normal',
              color: '#6B6459',
              paddingTop: '0.5rem',
              paddingBottom: '0.5rem',
            },
            h1: { color: '#1A1915', fontWeight: '600' },
            h2: { color: '#1A1915', fontWeight: '600' },
            h3: { color: '#1A1915', fontWeight: '600' },
            strong: { color: '#1A1915', fontWeight: '600' },
            th: { color: '#6B6459' },
          },
        },
      },
    },
  },
  plugins: [
    require('@tailwindcss/typography'),
  ],
}
