/** @type {import('tailwindcss').Config} */
export default {
  corePlugins: {
    preflight: false, // 保留 antd 的 reset，避免样式冲突
  },
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        primary: '#1890ff',
      },
    },
  },
  plugins: [],
}
