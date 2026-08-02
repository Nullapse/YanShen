import eslint from "@eslint/js";
import globals from "globals";

export default [
  {
    ignores: [
      ".release-*/**",
      ".test-*/**",
      ".venv/**",
      ".visual-qa/**",
      "build*/**",
      "design/**",
      "dist*/**",
    ],
  },
  {
    files: ["static/**/*.js", "tests/frontend/**/*.mjs"],
    ...eslint.configs.recommended,
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
    rules: {
      ...eslint.configs.recommended.rules,
      "no-unused-vars": ["error", { argsIgnorePattern: "^_", caughtErrors: "none" }],
    },
  },
  {
    files: ["scripts/**/*.cjs"],
    ...eslint.configs.recommended,
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "commonjs",
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
  },
];
