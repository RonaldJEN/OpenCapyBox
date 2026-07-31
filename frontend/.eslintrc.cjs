module.exports = {
  root: true,
  env: { browser: true, es2020: true },
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
    'plugin:react-hooks/recommended',
  ],
  ignorePatterns: ['dist', '.eslintrc.cjs'],
  parser: '@typescript-eslint/parser',
  plugins: ['react-refresh'],
  rules: {
    // API payloads, SSE events, and test doubles intentionally cross dynamic
    // JSON boundaries. Type checking still validates their typed consumers.
    '@typescript-eslint/no-explicit-any': 'off',
    '@typescript-eslint/no-unused-vars': [
      'error',
      {
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^_',
        ignoreRestSiblings: true,
      },
    ],
    // Long-polling and streaming readers use deliberate `while (true)` loops.
    'no-constant-condition': ['error', { checkLoops: false }],
    // This project colocates hooks/constants with components by design.
    'react-refresh/only-export-components': 'off',
  },
}
