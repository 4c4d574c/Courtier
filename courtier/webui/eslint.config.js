import js from "@eslint/js";
import tseslint from "typescript-eslint";
import pluginVue from "eslint-plugin-vue";
import globals from "globals";

export default [
  {
    ignores: ["dist", ".tmp", "node_modules", "scripts/.tmp"],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs["flat/essential"],
  {
    languageOptions: {
      globals: globals.browser,
    },
  },
  {
    files: ["**/*.vue"],
    languageOptions: {
      parserOptions: {
        parser: tseslint.parser,
      },
    },
  },
  {
    rules: {
      // Single-word component names are the project convention (Terminal, MainPanel...).
      "vue/multi-word-component-names": "off",
      // Deliberate any usage at untyped boundaries (SSE payloads, marked tokens).
      "@typescript-eslint/no-explicit-any": "off",
      // Underscore prefix marks intentionally unused bindings.
      "@typescript-eslint/no-unused-vars": [
        "error",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
          caughtErrorsIgnorePattern: "^_",
        },
      ],
    },
  },
];
